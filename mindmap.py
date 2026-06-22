"""
mindmap.py — Nexora AI  |  Interactive Mind Map
================================================
Builds a hierarchical, fully-expanded RADIAL mind-map from indexed Chroma
documents using the same chunk-fetch pattern as knowledge_graph.py.

Two routes:
  POST /mindmap
       body: { "session_id": "...", "files": [...] }
       Returns the COMPLETE tree, already expanded, 4 levels deep:
       root → themes → concepts → details
       (Drill is still available for going one level deeper on any
       leaf node that still reports has_children = true.)

  POST /mindmap/drill
       body: { "session_id": "...", "node_id": "...", "label": "...",
               "parent_label": "...", "files": [...] }
       Deep-dives one node — returns extra grandchildren for that node.

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
      "children": [ ... ],
      "has_children": true/false,   # hint for JS — more can still be drilled
      "sources":  ["file.pdf", ...]
    }

── Multi-document fix ────────────────────────────────────────────────────────
Chroma's `$in` operator misbehaves with a single-item list on some Chroma
versions (see rag_logic.hybrid_search for the same workaround). The previous
version of this file always built `{"source": {"$in": filenames}}`, which
silently returned zero rows whenever exactly one file was selected, and any
theme/concept extraction that re-filtered by a *single* source out of a
multi-doc batch hit the same wall. `_chroma_where()` below centralizes the
fix: single source → equality filter, multiple sources → `$in`, no filter at
all → user-only filter. Themes are now synthesized across *all* selected
documents together (as intended) but each theme still carries the real list
of source files it actually drew from, so attribution survives the merge.
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

# How deep to eagerly expand on initial generation (root counts as level 0).
# root(0) -> theme(1) -> concept(2) -> detail(3)
_EAGER_DEPTH = 3


# ─── Chroma helpers ───────────────────────────────────────────────────────────

def _chroma_where(username: str, filenames: list[str] | None) -> dict:
    """
    Build a Chroma `where` filter that is safe for 0, 1, or N filenames.

    Chroma's `$in` operator requires >= 2 items on some Chroma versions, so a
    single selected file must use a plain equality filter instead — this is
    the same fix already applied in rag_logic.hybrid_search.
    """
    if not filenames:
        return {"username": username}
    if len(filenames) == 1:
        return {"$and": [
            {"username": username},
            {"source":   filenames[0]},
        ]}
    return {"$and": [
        {"username": username},
        {"source":   {"$in": filenames}},
    ]}


def _fetch_chunks(username: str, filenames: list[str] | None) -> list[dict]:
    """Pull up to 150 text chunks per file from Chroma for the user.

    Multi-doc fix: previously this issued a single `col.get(... limit=150)`
    across the combined filter, which meant a 5-document selection could
    come back as 150 chunks almost entirely from one or two files (whichever
    Chroma happened to return first), starving the rest of representation in
    the generated themes. We now fetch up to 150 chunks PER file when
    multiple files are selected, so every document gets a fair sample.
    """
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col    = client.get_collection(CHROMA_COLLECTION)

        if filenames and len(filenames) > 1:
            chunks: list[dict] = []
            for fname in filenames:
                where   = _chroma_where(username, [fname])
                results = col.get(where=where, limit=150, include=["documents", "metadatas"])
                docs      = results.get("documents", []) or []
                metadatas = results.get("metadatas", []) or []
                chunks.extend(
                    {"text": d, "source": (m or {}).get("source", fname)}
                    for d, m in zip(docs, metadatas)
                    if d and len(d.strip()) > 40
                )
            return chunks

        where     = _chroma_where(username, filenames)
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


def _fetch_concept_chunks(chunks: list[dict], concept: str,
                           restrict_sources: list[str] | None = None) -> list[dict]:
    """
    Rank already-fetched chunks by relevance to `concept` via keyword overlap.
    Optionally restrict to a subset of source files first (used so a theme's
    own concept-extraction only looks at the documents that theme actually
    came from, rather than bleeding in unrelated docs from the batch).
    """
    pool = chunks
    if restrict_sources:
        src_set = set(restrict_sources)
        narrowed = [c for c in chunks if c["source"] in src_set]
        if narrowed:
            pool = narrowed

    keywords = set(re.findall(r"[a-zA-Z]{3,}", concept.lower()))

    scored: list[tuple[int, dict]] = []
    for ch in pool:
        text_lower = ch["text"].lower()
        hits = sum(1 for kw in keywords if kw in text_lower)
        if hits > 0:
            scored.append((hits, ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [ch for _, ch in scored[:20]]
    return top or pool[:15]


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


def _extract_json_array_block(raw: str) -> str | None:
    """
    Find the first top-level [...] block by tracking bracket depth while
    respecting string boundaries, so brackets that appear inside a label or
    summary string don't get mistaken for the array's own delimiters.
    """
    start = raw.find("[")
    if start == -1:
        return None

    depth, in_str, escape = 0, False, False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
    return raw[start:]  # unterminated — repair pass below may still salvage it


def _repair_json_strings(block: str) -> str:
    """
    Re-walk the JSON text fixing the two mistakes raw LLM output makes most
    often inside string literals:
      1. Literal newline/tab/control characters instead of escaped \\n \\t —
         these otherwise trip Python's json module with
         "Invalid control character".
      2. An unescaped " in the middle of a string's own text (e.g. the model
         writes a "quoted phrase" inside a summary without escaping it) —
         these otherwise trip json with "Expecting ',' delimiter" or
         "Expecting ':' delimiter" a few tokens later, once the parser loses
         sync. A quote is only treated as the real end of the string if the
         next non-whitespace character is one JSON would expect there
         (`,` `]` `}` `:`); otherwise it's escaped as part of the text.
    """
    out: list[str] = []
    n = len(block)
    in_str = escape = False

    def next_significant(idx: int) -> str:
        j = idx
        while j < n and block[j] in " \t\r\n":
            j += 1
        return block[j] if j < n else ""

    i = 0
    while i < n:
        ch = block[i]
        if in_str:
            if escape:
                out.append(ch); escape = False; i += 1; continue
            if ch == "\\":
                out.append(ch); escape = True; i += 1; continue
            if ch == '"':
                nxt = next_significant(i + 1)
                if nxt in ",]}:" or nxt == "":
                    out.append(ch)
                    in_str = False
                else:
                    out.append('\\"')
                i += 1; continue
            if ch == "\n":
                out.append("\\n"); i += 1; continue
            if ch == "\t":
                out.append("\\t"); i += 1; continue
            if ch == "\r":
                i += 1; continue
            if ord(ch) < 0x20:
                i += 1; continue
            out.append(ch); i += 1
        else:
            if ch == '"':
                in_str = True
            out.append(ch); i += 1
    return "".join(out)


def _parse_json_array(raw: str) -> list:
    """
    Parse the first JSON array found in a raw LLM response. Tries a fast
    straight parse first; if that fails (which raw LLM output does fairly
    often — unescaped quotes/newlines inside summary text, trailing commas,
    markdown fences, a stray preamble sentence), falls back to a repair pass
    rather than dropping the whole batch of themes/concepts/details.
    """
    cleaned = re.sub(r"^```[a-z]*\n?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    block = _extract_json_array_block(cleaned)
    if not block:
        return []

    try:
        return json.loads(block)
    except json.JSONDecodeError:
        pass

    repaired = _repair_json_strings(block)
    repaired = re.sub(r",\s*([\]}])", r"\1", repaired)  # trailing commas
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as e:
        print(f"[Mindmap] JSON repair failed ({e}); raw head: {raw[:200]!r}")
        return []


# ─── LLM helpers ─────────────────────────────────────────────────────────────

def _llm_extract_themes(chunks: list[dict], doc_count: int, session_id: str) -> list[dict]:
    """
    Synthesize 5-7 themes across ALL selected documents together.
    Each theme reports back which source file(s) it actually drew from, so
    multi-doc attribution survives even though themes are unified.
    """
    by_source: dict[str, list[str]] = defaultdict(list)
    for ch in chunks:
        by_source[ch["source"]].append(ch["text"])

    # Sample from every source present, not just the first 6, so a large
    # multi-doc batch doesn't starve later documents of representation.
    sample_parts = []
    per_source_budget = 3_000 // max(len(by_source), 1) if doc_count > 1 else 400
    per_source_budget = max(per_source_budget, 250)
    for src, texts in by_source.items():
        snippet = trim_to_budget(" ".join(texts[:4]), per_source_budget)
        sample_parts.append(f"[{src}]\n{snippet}")
    combined = trim_to_budget("\n\n".join(sample_parts), 3_500)

    multi_doc_hint = (
        f"These excerpts come from {doc_count} different documents (each "
        f"prefixed with its filename in brackets). Identify themes that may "
        f"span multiple documents where topics overlap, and keep themes "
        f"distinct from one another.\n"
        if doc_count > 1 else ""
    )

    prompt = f"""Identify 5-7 major themes across these document excerpts.
{multi_doc_hint}For each theme return JSON with keys:
  - "label"    : 2-4 words
  - "summary"  : 1-2 sentences
  - "keywords" : 5 terms
  - "sources"  : list of the exact filenames (from the brackets) this theme draws from

Output ONLY a valid JSON array. No markdown, no preamble, no trailing commas. Keep every string on a single line (no literal line breaks) and escape any double quotes that appear inside a string value as \".

Documents:
\"\"\"{combined}\"\"\"

JSON:"""

    try:
        raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 800})
        arr = _parse_json_array(raw)
        known_sources = set(by_source.keys())
        out = []
        for i, t in enumerate(arr):
            if not isinstance(t, dict) or not t.get("label"):
                continue
            raw_sources = t.get("sources", [])
            sources = [s for s in raw_sources if s in known_sources] if isinstance(raw_sources, list) else []
            out.append({
                "label":    t.get("label", f"Theme {i+1}"),
                "summary":  t.get("summary", ""),
                "keywords": (t.get("keywords") or [])[:5],
                "sources":  sources or list(known_sources)[:2],
            })
        return out[:7]
    except Exception as e:
        print(f"[Mindmap] Theme extraction error: {e}")
        return []


def _llm_extract_concepts(theme_label: str, theme_summary: str,
                           chunks: list[dict], session_id: str) -> list[dict]:
    """
    For a given theme, extract 4-6 concrete concepts/sub-topics.
    Returns list of {label, summary, keywords, has_children}.
    """
    combined = trim_to_budget(" ".join(c["text"] for c in chunks[:12]), 2_500)

    prompt = f"""You are a mind-map expert assistant.

Theme: "{theme_label}"
Theme description: {theme_summary}

From the document text below, extract 4 to 6 concrete CONCEPTS or SUB-TOPICS
that belong under this theme. These should be more specific than the theme
itself — real ideas, processes, entities, or findings.

For each concept return:
  - "label"        : 2-5 word concept name (title case)
  - "summary"      : 1 sentence explanation
  - "keywords"     : list of 4 key terms
  - "has_children" : true (always — they can all be explored deeper)

Output ONLY a valid JSON array. No markdown, no preamble, no trailing commas. Keep every string on a single line (no literal line breaks) and escape any double quotes that appear inside a string value as \".

Document text:
\"\"\"{combined}\"\"\"

JSON:"""

    try:
        raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 700})
        arr = _parse_json_array(raw)
        return [
            {
                "label":        c.get("label", f"Concept {i+1}"),
                "summary":      c.get("summary", ""),
                "keywords":     (c.get("keywords") or [])[:4],
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
    """Deep-dive on one concept, producing 4-6 leaf-level detail nodes."""
    combined = trim_to_budget(" ".join(c["text"] for c in chunks[:8]), 2_000)

    prompt = f"""Deep-dive on "{concept_label}" (parent theme: "{parent_label}").
Produce 4-6 detailed insights grounded in the text below.
For each return JSON with keys:
  - "label"        : 3-6 words
  - "summary"      : 2-3 sentences
  - "keywords"     : 3 terms
  - "has_children" : false

Output ONLY a valid JSON array. No markdown, no preamble, no trailing commas. Keep every string on a single line (no literal line breaks) and escape any double quotes that appear inside a string value as \".

Text:
\"\"\"{combined}\"\"\"

JSON:"""

    try:
        raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 800})
        arr = _parse_json_array(raw)
        return [
            {
                "label":        d.get("label", f"Detail {i+1}"),
                "summary":      d.get("summary", ""),
                "keywords":     (d.get("keywords") or [])[:3],
                "has_children": False,
            }
            for i, d in enumerate(arr)
            if isinstance(d, dict) and d.get("label")
        ][:6]
    except Exception as e:
        print(f"[Mindmap] Drill-down error: {e}")
        return []


# ─── Tree builder ─────────────────────────────────────────────────────────────

def _build_tree(chunks: list[dict], filenames: list[str], session_id: str,
                 username: str) -> dict:
    """
    Build a FULLY EXPANDED radial mind-map tree, 4 levels deep:
      Root → Themes (5-7) → Concepts (4-6 each) → Details (4-6 each)

    Every level is populated eagerly (unlike the old lazy-only version), so
    the frontend can render the whole radial tree expanded on first paint.
    Drill-down remains available client-side for going one level further
    past details if the model judges has_children worth re-checking.
    """
    root_sources = sorted({c["source"] for c in chunks})
    doc_count    = len(filenames) if filenames else len(root_sources)
    doc_label    = filenames[0] if len(filenames) == 1 else f"{doc_count} Documents"

    themes      = _llm_extract_themes(chunks, doc_count, session_id)
    theme_nodes = []

    for i, theme in enumerate(themes):
        tid = f"theme-{i}"
        theme_sources = theme.get("sources") or root_sources[:2]

        # Concept extraction for this theme only looks at chunks from the
        # documents that theme actually drew from (multi-doc fix — prevents
        # a theme from one file leaking concepts pulled from another file).
        theme_chunks = _fetch_concept_chunks(chunks, theme["label"], theme_sources)

        concepts      = _llm_extract_concepts(theme["label"], theme["summary"],
                                               theme_chunks, session_id)
        concept_nodes = []

        for j, concept in enumerate(concepts):
            cid = f"{tid}-concept-{j}"

            # Eagerly fetch one more level (details) so the radial tree opens
            # fully expanded instead of requiring a click-to-drill per node.
            concept_chunks = _fetch_concept_chunks(theme_chunks, concept["label"], theme_sources)
            details        = _llm_drill_down(concept["label"], theme["label"],
                                              concept_chunks, session_id)
            detail_nodes = [
                {
                    "id":           f"{cid}-detail-{k}",
                    "label":        d["label"],
                    "type":         "detail",
                    "summary":      d["summary"],
                    "keywords":     d["keywords"],
                    "has_children": False,
                    "children":     [],
                    "sources":      theme_sources,
                }
                for k, d in enumerate(details)
            ]

            concept_nodes.append({
                "id":           cid,
                "label":        concept["label"],
                "type":         "concept",
                "summary":      concept["summary"],
                "keywords":     concept["keywords"],
                "has_children": len(detail_nodes) == 0,  # only offer drill if we got nothing yet
                "children":     detail_nodes,
                "sources":      theme_sources,
            })

        theme_nodes.append({
            "id":           tid,
            "label":        theme["label"],
            "type":         "theme",
            "summary":      theme["summary"],
            "keywords":     theme["keywords"],
            "has_children": len(concept_nodes) == 0,
            "children":     concept_nodes,
            "sources":      theme_sources,
        })

    return {
        "id":           "root",
        "label":        doc_label,
        "type":         "root",
        "summary":      (
            f"Mind map generated from {len(chunks)} chunks across "
            f"{len(root_sources)} source document(s)."
        ),
        "keywords":     [],
        "has_children": False,
        "children":     theme_nodes,
        "sources":      root_sources,
    }


# ─── Routes ──────────────────────────────────────────────────────────────────

@mindmap_bp.route("/mindmap", methods=["POST"])
def mindmap():
    """
    POST /mindmap
    Body: { "session_id": "...", "files": ["file.pdf", ...] }
    Returns the full, already-expanded radial tree
    (root → themes → concepts → details).
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
    Returns children list for one node (used when a leaf still reports
    has_children = true and the user wants to go one level deeper).
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

    all_chunks = _fetch_chunks(username, filenames or None)
    if not all_chunks:
        return jsonify({"children": []})

    chunks  = _fetch_concept_chunks(all_chunks, label, filenames or None)
    details = _llm_drill_down(label, parent_label, chunks, session_id)

    relevant_sources = sorted({c["source"] for c in chunks}) or filenames[:1]

    children = [
        {
            "id":           f"{node_id}-detail-{i}",
            "label":        d["label"],
            "type":         "detail",
            "summary":      d["summary"],
            "keywords":     d["keywords"],
            "has_children": False,
            "children":     [],
            "sources":      relevant_sources,
        }
        for i, d in enumerate(details)
    ]

    return jsonify({"children": children, "node_id": node_id})
