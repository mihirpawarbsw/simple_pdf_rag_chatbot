"""
knowledge_graph.py — Nexora AI Knowledge Graph
================================================
Extracts entities and relationships from indexed Chroma documents
for the logged-in user and returns a D3-ready nodes/edges payload.

Register in app.py:
    from knowledge_graph import knowledge_graph_bp
    app.register_blueprint(knowledge_graph_bp)

FIXES (v2):
    1. Double-escaped regex (searched for literal backslash-bracket) fixed
       to a correct bracket pattern via the _extract_json_array() helper.
    2. Escaped triple-quote prompt fixed to use real triple-quote delimiters.
    3. Chroma flat $in + limit=120 replaced with per-file loop (same fix as
       nlp_analytics) so every document contributes chunks and edges.
    4. Shared _extract_json_array() helper with partial-parse salvage for
       truncated LLM responses (same pattern as nlp_analytics).
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict

import chromadb
from flask import Blueprint, jsonify, request, session

from rag_logic import CHROMA_PATH, CHROMA_COLLECTION, get_llm

knowledge_graph_bp = Blueprint("knowledge_graph_bp", __name__)


# ─── LLM settings ─────────────────────────────────────────────────────────────

def _get_llm_settings() -> dict:
    return {
        "model_name":  "llama-3.3-70b-versatile",
        "temperature": 0.0,
    }


# ─── Safe JSON array extractor (with partial-parse for truncated responses) ───

def _extract_json_array(text: str) -> list | None:
    """
    Robustly extract a JSON array from an LLM response.

    Strategy:
    1. Strip markdown fences.
    2. Try json.loads on the first [...] match (happy path).
    3. If that fails (e.g. truncated output), walk character-by-character and
       salvage every complete {...} object found before the cut-off point.
    Returns None only when zero valid objects can be recovered.
    """
    text = re.sub(r"^```[a-z]*\n?|```$", "", text, flags=re.MULTILINE).strip()

    # Happy path — complete array present
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass  # fall through to partial-parse

    # Partial-parse — response was truncated before closing ]
    start = text.find("[")
    if start == -1:
        return None

    salvaged: list = []
    depth = 0
    obj_start: int | None = None

    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    salvaged.append(json.loads(text[obj_start: i + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None

    return salvaged if salvaged else None


# ─── Chroma fetch — per-file loop to guarantee all docs are represented ───────

def _fetch_chunks_for_user(username: str, filenames: list[str] | None = None) -> list[dict]:
    """
    Pull text chunks from Chroma for the user, optionally filtered by filenames.

    BUG FIX: The original used col.get(where={$in: filenames}, limit=120).
    Chroma applies the limit across the whole result set with no per-source
    balancing — whichever file's chunks appeared first in the index filled the
    ceiling, and all subsequent files returned zero chunks, producing zero triples
    for those documents and therefore zero edges on the graph.

    FIX: fetch per-file in a loop (30 chunks each) so every document is
    guaranteed representation regardless of index ordering.
    """
    CHUNKS_PER_FILE = 30

    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col    = client.get_collection(CHROMA_COLLECTION)
        all_chunks: list[dict] = []

        if filenames:
            for fname in filenames:
                where: dict = {"$and": [{"username": username}, {"source": fname}]}
                try:
                    results   = col.get(where=where, limit=CHUNKS_PER_FILE,
                                        include=["documents", "metadatas"])
                    docs      = results.get("documents", []) or []
                    metadatas = results.get("metadatas", []) or []
                    for doc, meta in zip(docs, metadatas):
                        if doc and len(doc.strip()) > 40:
                            all_chunks.append({
                                "text":   doc,
                                "source": (meta or {}).get("source", fname),
                            })
                except Exception as e:
                    print(f"[KG] Chroma fetch error for '{fname}': {e}")
        else:
            # No specific files — pull up to 300 chunks for the whole user
            where = {"username": username}
            results   = col.get(where=where, limit=300, include=["documents", "metadatas"])
            docs      = results.get("documents", []) or []
            metadatas = results.get("metadatas", []) or []
            for doc, meta in zip(docs, metadatas):
                if doc and len(doc.strip()) > 40:
                    all_chunks.append({
                        "text":   doc,
                        "source": (meta or {}).get("source", "Unknown"),
                    })

        return all_chunks

    except Exception as e:
        print(f"[KG] Chroma client error: {e}")
        return []


# ─── Triple extraction ────────────────────────────────────────────────────────

def _extract_triples_with_llm(chunks: list[dict], session_id: str) -> list[dict]:
    """
    Extract (subject, relation, object) triples from document chunks via LLM.

    BUG FIXES:
    1. Prompt previously used escaped triple-quotes which sent the literal
       string backslash-quote-quote-quote to the LLM. The model interpreted
       this as a formatting instruction and returned prose instead of JSON,
       causing the regex to find nothing.
       FIX: Use real triple-quote delimiters in the prompt string.

    2. re.search with double-escaped brackets was searching for the literal
       characters backslash-[ and backslash-] rather than JSON array brackets.
       It could NEVER match any LLM output, so all_triples was always empty,
       producing an empty edges list on the graph.
       FIX: Use the shared _extract_json_array() helper with the correct pattern.
    """
    if not chunks:
        return []

    from token_utils import trim_to_budget
    from api_router  import call_llm_with_fallback

    by_source: dict[str, list[str]] = defaultdict(list)
    for ch in chunks:
        by_source[ch["source"]].append(ch["text"])

    all_triples: list[dict] = []

    for source, texts in list(by_source.items())[:6]:   # cap at 6 sources
        combined = trim_to_budget(" ".join(texts[:8]), 1_400)

        # Clean prompt — real """ delimiters, explicit JSON-only instruction
        prompt = (
            f'Extract up to 15 factual triples from the document "{source}".\n'
            "Each triple must have: a short subject (2-6 words), a verb-phrase "
            "relation (2-5 words), and a short object (2-6 words).\n"
            "Return ONLY a valid JSON array of objects with keys: "
            '"subject", "relation", "object". No markdown. No extra text.\n\n'
            f'Text:\n"""\n{combined}\n"""\n\nJSON:'
        )

        try:
            raw = call_llm_with_fallback(
                prompt,
                {**_get_llm_settings(), "max_tokens": 1000},
            )
            arr = _extract_json_array(raw)
            if arr is None:
                print(f"[KG] No JSON array in LLM response for '{source}': {raw[:200]!r}")
                continue
            for t in arr:
                if (isinstance(t, dict)
                        and t.get("subject")
                        and t.get("relation")
                        and t.get("object")):
                    t["source"] = source
                    all_triples.append(t)
        except Exception as e:
            print(f"[KG] LLM error for '{source}': {e}")

    return all_triples


# ─── Graph payload builder ────────────────────────────────────────────────────

def _build_graph_payload(triples: list[dict], filenames: list[str]) -> dict:
    """
    Convert triples to D3 force-graph nodes/edges payload.
    Each edge carries a 'sources' list (which PDFs this relation came from).
    """
    node_set: dict[str, dict] = {}
    edges: list[dict]         = []

    def _add_node(label: str, ntype: str = "concept") -> str:
        key = label.strip().lower()
        if key not in node_set:
            node_set[key] = {
                "id":      key,
                "label":   label.strip(),
                "type":    ntype,
                "size":    1,
                "sources": [],
            }
        else:
            node_set[key]["size"] += 1
        return key

    for triple in triples:
        s   = triple.get("subject",  "").strip()
        r   = triple.get("relation", "").strip()
        o   = triple.get("object",   "").strip()
        src = triple.get("source",   "Unknown")
        if not s or not r or not o:
            continue

        s_type = "entity"  if (s[0].isupper() and " " in s) else "concept"
        o_type = "entity"  if (o[0].isupper() and " " in o) else "concept"

        sid = _add_node(s, s_type)
        oid = _add_node(o, o_type)

        # Track which PDFs each node appears in
        if src not in node_set[sid]["sources"]:
            node_set[sid]["sources"].append(src)
        if src not in node_set[oid]["sources"]:
            node_set[oid]["sources"].append(src)

        # Deduplicate edges (same triple from same source -> increment weight)
        edge_key = f"{sid}|{oid}|{r.lower()}"
        existing = next((e for e in edges if e.get("_key") == edge_key), None)
        if existing:
            existing["weight"] += 1
            if src not in existing["sources"]:
                existing["sources"].append(src)
        else:
            edges.append({
                "_key":     edge_key,
                "source":   sid,
                "target":   oid,
                "relation": r,
                "weight":   1,
                "sources":  [src],
            })

    # Add source-document nodes (only when there are actual triples to connect)
    for fname in filenames:
        fkey = f"doc:{fname}"
        node_set[fkey] = {
            "id":      fkey,
            "label":   fname,
            "type":    "document",
            "size":    3,
            "sources": [fname],
        }

    nodes       = list(node_set.values())
    clean_edges = [{k: v for k, v in e.items() if k != "_key"} for e in edges]

    return {
        "nodes": nodes,
        "edges": clean_edges,
        "stats": {
            "node_count":   len(nodes),
            "edge_count":   len(clean_edges),
            "triple_count": len(triples),
        },
    }


# ─── Route ────────────────────────────────────────────────────────────────────

@knowledge_graph_bp.route("/knowledge_graph", methods=["GET", "POST"])
def knowledge_graph():
    """
    GET  /knowledge_graph?files=file1.pdf,file2.pdf&session_id=...
    POST /knowledge_graph  body: { "files": ["file1.pdf", ...], "session_id": "..." }

    Returns D3-compatible { nodes, edges, stats } JSON.
    Defaults to ALL of the user's indexed documents when no files are specified.
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = session.get("username")

    if request.method == "POST":
        body       = request.json or {}
        session_id = body.get("session_id", "kg-session")
        filenames  = body.get("files") or []
        if isinstance(filenames, str):
            filenames = [f.strip() for f in filenames.split(",") if f.strip()]
    else:
        session_id  = request.args.get("session_id", "kg-session")
        files_param = request.args.get("files", "")
        filenames   = [f.strip() for f in files_param.split(",") if f.strip()] if files_param else []

    # Default: use ALL documents indexed for this user
    if not filenames:
        import sqlite3 as _sqlite3
        try:
            from app import DB_NAME
            conn   = _sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT filename FROM uploaded_files WHERE username = ? ORDER BY filename",
                (username,)
            )
            filenames = [r[0] for r in cursor.fetchall()]
            conn.close()
        except Exception as e:
            print(f"[KG] Could not fetch user file list: {e}")
            filenames = []

    chunks  = _fetch_chunks_for_user(username, filenames if filenames else None)
    if not chunks:
        return jsonify({
            "nodes": [], "edges": [],
            "stats": {"node_count": 0, "edge_count": 0, "triple_count": 0},
        })

    triples = _extract_triples_with_llm(chunks, session_id)
    payload = _build_graph_payload(triples, filenames or [])

    return jsonify(payload)
