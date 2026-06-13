"""
mindmap.py — Nexora AI  |  Interactive Mind Map
================================================
Builds a hierarchical mind-map from indexed Chroma documents using the same
chunk-fetch pattern as knowledge_graph.py.

Two routes:
  POST /mindmap
       body: { "session_id": "...", "files": [...] }
       Returns the root mind-map tree (3 levels: root → themes → concepts)

  POST /mindmap/drill
       body: { "session_id": "...", "node_id": "...", "label": "...", "files": [...] }
       Deep-dives one node — returns sub-tree children for that concept.

Register in app.py:
    from mindmap import mindmap_bp
    app.register_blueprint(mindmap_bp)

Tree node schema:
    {
      "id":       "<unique string>",
      "label":    "<display text>",
      "type":     "root" | "theme" | "concept" | "detail",
      "summary":  "<1-2 sentence description>",
      "keywords": ["...", ...],
      "children": [ ... ],          # only populated at root/theme level
      "has_children": true/false,   # hint for JS lazy-load
      "sources":  ["file.pdf", ...]
    }
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict

from token_utils import trim_to_budget
from api_router  import call_llm_with_fallback

import chromadb
from flask import Blueprint, jsonify, request, session

from rag_logic import CHROMA_PATH, CHROMA_COLLECTION, get_llm

mindmap_bp = Blueprint("mindmap_bp", __name__)

_LLM_SETTINGS = {"model_name": "llama-3.3-70b-versatile", "temperature": 0.0}

# ─── Chroma helpers (same pattern as knowledge_graph.py) ─────────────────────

def _fetch_chunks(username: str, filenames: list[str] | None) -> list[dict]:
    """Pull up to 150 text chunks from Chroma for the user."""
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col    = client.get_collection(CHROMA_COLLECTION)

        if filenames:
            where: dict = {"$and": [
                {"username": username},
                {"source":   {"$in": filenames}},
            ]}
        else:
            where = {"username": username}

        results   = col.get(where=where, limit=150, include=["documents", "metadatas"])
        docs      = results.get("documents", []) or []
        metadatas = results.get("metadatas", []) or []

        return [
            {"text": d, "source": (m or {}).get("source", "Unknown")}
            for d, m in zip(docs, metadatas)
            if d and len(d.strip()) > 40
        ]
    except Exception as e:
        print(f"[Mindmap] Chroma fetch error: {e}")
        return []


def _fetch_concept_chunks(username: str, concept: str,
                           filenames: list[str] | None) -> list[dict]:
    """
    Fetch chunks most relevant to a specific concept for drill-down.
    Since Chroma doesn't support full-text search on documents directly,
    we fetch all chunks and filter by keyword presence (fast, no extra dep).
    """
    all_chunks = _fetch_chunks(username, filenames)
    keywords   = set(re.findall(r"[a-zA-Z]{3,}", concept.lower()))

    scored: list[tuple[int, dict]] = []
    for ch in all_chunks:
        text_lower = ch["text"].lower()
        hits = sum(1 for kw in keywords if kw in text_lower)
        if hits > 0:
            scored.append((hits, ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    # Return top-20 most relevant chunks
    return [ch for _, ch in scored[:20]] or all_chunks[:15]


def _all_filenames(username: str) -> list[str]:
    try:
        from app import DB_NAME
        conn   = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT filename FROM uploaded_files WHERE username = ? ORDER BY filename",
            (username,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        print(f"[Mindmap] DB error: {e}")
        return []


# ─── LLM helpers ─────────────────────────────────────────────────────────────
def _llm_extract_themes(chunks: list[dict], session_id: str) -> list[dict]:
    from collections import defaultdict
    by_source: dict[str, list[str]] = defaultdict(list)
    for ch in chunks:
        by_source[ch["source"]].append(ch["text"])

    sample_parts = []
    for src, texts in list(by_source.items())[:6]:
        snippet = trim_to_budget(" ".join(texts[:3]), 400)   # 400 tok/source
        sample_parts.append(f"[{src}]\\n{snippet}")
    combined = trim_to_budget("\\n\\n".join(sample_parts), 3_000)  # 3k total

    prompt = f"""Identify 5-7 major themes in these document excerpts.
For each return JSON with keys: label (2-4 words), summary (1-2 sentences),
keywords (5 terms), sources (list of filenames).
Output ONLY a valid JSON array. No markdown.

Documents:
\\"\\"\\"{combined}\\"\\"\\"

JSON:"""

    try:
        raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 700})
        raw = re.sub(r"^```[a-z]*\\n?|```$", "", raw, flags=re.MULTILINE).strip()
        arr = json.loads(re.search(r"\\[.*\\]", raw, re.DOTALL).group())
        return [
            {
                "label":    t.get("label", f"Theme {i+1}"),
                "summary":  t.get("summary", ""),
                "keywords": t.get("keywords", [])[:5],
                "sources":  t.get("sources", []) if isinstance(t.get("sources"), list) else [],
            }
            for i, t in enumerate(arr) if isinstance(t, dict) and t.get("label")
        ][:7]
    except Exception as e:
        print(f"[Mindmap] Theme extraction error: {e}")
        return []


def _llm_extract_concepts(theme_label: str, theme_summary: str,
                           chunks: list[dict], session_id: str) -> list[dict]:
    combined = trim_to_budget(" ".join(c["text"] for c in chunks[:8]), 2_000)

    prompt = f"""Theme: "{theme_label}".
Extract 4-6 concrete concepts under this theme.
Return JSON: label (2-5 words), summary (1 sentence), keywords (4 terms), has_children (true).
Output ONLY a valid JSON array. No markdown.

Text: \\"\\"\\"{combined}\\"\\"\\"

JSON:"""

    try:
        raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 600})
        raw = re.sub(r"^```[a-z]*\\n?|```$", "", raw, flags=re.MULTILINE).strip()
        arr = json.loads(re.search(r"\\[.*\\]", raw, re.DOTALL).group())
        return [
            {
                "label":        c.get("label", f"Concept {i+1}"),
                "summary":      c.get("summary", ""),
                "keywords":     c.get("keywords", [])[:4],
                "has_children": True,
            }
            for i, c in enumerate(arr) if isinstance(c, dict) and c.get("label")
        ][:6]
    except Exception as e:
        print(f"[Mindmap] Concept extraction error: {e}")
        return []


def _llm_extract_concepts(theme_label: str, theme_summary: str,
                           chunks: list[dict], session_id: str) -> list[dict]:
    """
    For a given theme, extract 4-6 concrete concepts/sub-topics.
    Returns list of {label, summary, keywords, has_children, sources}.
    """
    combined = " ".join(c["text"] for c in chunks[:12])[:4000]

    prompt = f"""You are a mind-map expert assistant.

Theme: "{theme_label}"
Theme description: {theme_summary}

From the document text below, extract 4 to 6 concrete CONCEPTS or SUB-TOPICS that belong under this theme.
These should be more specific than the theme itself — real ideas, processes, entities, or findings.

For each concept return:
  - "label"        : 2-5 word concept name (title case)
  - "summary"      : 1 sentence explanation
  - "keywords"     : list of 4 key terms
  - "has_children" : true (always — they can all be explored deeper)

Output ONLY a valid JSON array. No markdown.

Document text:
\"\"\"{combined}\"\"\"

JSON:"""

    try:
        llm  = get_llm(session_id, _LLM_SETTINGS)
        raw  = llm.invoke(prompt).content.strip()
        raw  = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
        arr  = json.loads(re.search(r"\[.*\]", raw, re.DOTALL).group())
        return [
            {
                "label":        c.get("label", f"Concept {i+1}"),
                "summary":      c.get("summary", ""),
                "keywords":     c.get("keywords", [])[:4],
                "has_children": True,
            }
            for i, c in enumerate(arr)
            if isinstance(c, dict) and c.get("label")
        ][:6]
    except Exception as e:
        print(f"[Mindmap] Concept extraction error: {e}")
        return []


def _llm_drill_down(concept_label: str, parent_label: str,
                    chunks: list[dict], session_id: str) -> list[dict]:
    combined = trim_to_budget(" ".join(c["text"] for c in chunks[:6]), 1_800)

    prompt = f"""Deep-dive on "{concept_label}" (parent: "{parent_label}").
Produce 4-6 detailed insights from the text.
Return JSON: label (3-6 words), summary (2-3 sentences), keywords (3 terms), has_children (false).
Output ONLY a valid JSON array. No markdown.

Text: \\"\\"\\"{combined}\\"\\"\\"

JSON:"""

    try:
        raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 700})
        raw = re.sub(r"^```[a-z]*\\n?|```$", "", raw, flags=re.MULTILINE).strip()
        arr = json.loads(re.search(r"\\[.*\\]", raw, re.DOTALL).group())
        return [
            {
                "label":        d.get("label", f"Detail {i+1}"),
                "summary":      d.get("summary", ""),
                "keywords":     d.get("keywords", [])[:3],
                "has_children": False,
            }
            for i, d in enumerate(arr) if isinstance(d, dict) and d.get("label")
        ][:6]
    except Exception as e:
        print(f"[Mindmap] Drill-down error: {e}")
        return []


# ─── Tree builder ─────────────────────────────────────────────────────────────

def _build_tree(chunks: list[dict], filenames: list[str], session_id: str,
                username: str) -> dict:
    """
    Build a 3-level mind-map tree:
      Root → Themes (5-7) → Concepts (4-6 each)
    """
    root_sources = list({c["source"] for c in chunks})
    doc_label    = filenames[0] if len(filenames) == 1 else f"{len(filenames)} Documents"

    themes      = _llm_extract_themes(chunks, session_id)
    theme_nodes = []

    for i, theme in enumerate(themes):
        tid = f"theme-{i}"

        # Fetch theme-relevant chunks for concept extraction
        theme_chunks = _fetch_concept_chunks(username, theme["label"], filenames or None)

        concepts      = _llm_extract_concepts(theme["label"], theme["summary"],
                                              theme_chunks, session_id)
        concept_nodes = []
        for j, concept in enumerate(concepts):
            concept_nodes.append({
                "id":           f"{tid}-concept-{j}",
                "label":        concept["label"],
                "type":         "concept",
                "summary":      concept["summary"],
                "keywords":     concept["keywords"],
                "has_children": True,
                "children":     [],
                "sources":      theme.get("sources", root_sources[:2]),
            })

        theme_nodes.append({
            "id":           tid,
            "label":        theme["label"],
            "type":         "theme",
            "summary":      theme["summary"],
            "keywords":     theme["keywords"],
            "has_children": True,
            "children":     concept_nodes,
            "sources":      theme.get("sources", root_sources[:2]),
        })

    return {
        "id":           "root",
        "label":        doc_label,
        "type":         "root",
        "summary":      f"Mind map generated from {len(chunks)} chunks across {len(set(c['source'] for c in chunks))} source(s).",
        "keywords":     [],
        "has_children": True,
        "children":     theme_nodes,
        "sources":      root_sources,
    }


# ─── Routes ──────────────────────────────────────────────────────────────────

@mindmap_bp.route("/mindmap", methods=["POST"])
def mindmap():
    """
    POST /mindmap
    Body: { "session_id": "...", "files": ["file.pdf", ...] }
    Returns the full root mind-map tree (root → themes → concepts).
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username   = session.get("username")
    body       = request.json or {}
    session_id = body.get("session_id", "mm-session")
    filenames  = body.get("files") or []

    if isinstance(filenames, str):
        filenames = [f.strip() for f in filenames.split(",") if f.strip()]

    if not filenames:
        filenames = _all_filenames(username)

    chunks = _fetch_chunks(username, filenames or None)
    if not chunks:
        return jsonify({
            "id": "root", "label": "No Documents", "type": "root",
            "summary": "No indexed documents found.", "keywords": [],
            "has_children": False, "children": [], "sources": []
        })

    tree = _build_tree(chunks, filenames, session_id, username)
    return jsonify(tree)


@mindmap_bp.route("/mindmap/drill", methods=["POST"])
def mindmap_drill():
    """
    POST /mindmap/drill
    Body: { "session_id": "...", "node_id": "...", "label": "...",
            "parent_label": "...", "files": [...] }
    Returns children list for one node (lazy deep-dive).
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username     = session.get("username")
    body         = request.json or {}
    session_id   = body.get("session_id", "mm-session")
    node_id      = body.get("node_id", "")
    label        = body.get("label", "")
    parent_label = body.get("parent_label", "Document")
    filenames    = body.get("files") or []

    if isinstance(filenames, str):
        filenames = [f.strip() for f in filenames.split(",") if f.strip()]
    if not filenames:
        filenames = _all_filenames(username)

    chunks = _fetch_concept_chunks(username, label, filenames or None)
    if not chunks:
        return jsonify({"children": []})

    details = _llm_drill_down(label, parent_label, chunks, session_id)

    children = [
        {
            "id":           f"{node_id}-detail-{i}",
            "label":        d["label"],
            "type":         "detail",
            "summary":      d["summary"],
            "keywords":     d["keywords"],
            "has_children": False,
            "children":     [],
            "sources":      [filenames[0]] if filenames else [],
        }
        for i, d in enumerate(details)
    ]

    return jsonify({"children": children, "node_id": node_id})
