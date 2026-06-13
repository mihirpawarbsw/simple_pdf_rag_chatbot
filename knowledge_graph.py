"""
knowledge_graph.py — Nexora AI Knowledge Graph
================================================
Extracts entities and relationships from indexed Chroma documents
for the logged-in user and returns a D3-ready nodes/edges payload.

Register in app.py:
    from knowledge_graph import knowledge_graph_bp
    app.register_blueprint(knowledge_graph_bp)
"""

from __future__ import annotations

import json
import os
import re

import chromadb
from flask import Blueprint, jsonify, request, session

from rag_logic import CHROMA_PATH, CHROMA_COLLECTION, get_llm

knowledge_graph_bp = Blueprint("knowledge_graph_bp", __name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_llm_settings() -> dict:
    return {
        "model_name":  "llama-3.3-70b-versatile",
        "temperature": 0.0,
    }


def _fetch_chunks_for_user(username: str, filenames: list[str] | None = None) -> list[dict]:
    """
    Pull text chunks from Chroma for the user, optionally filtered by filenames.
    Returns list of dicts with 'text' and 'source' keys for traceability.
    """
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col    = client.get_collection(CHROMA_COLLECTION)

        # Always filter by username; optionally also by source filenames
        if filenames and len(filenames) >= 1:
            where: dict = {"$and": [
                {"username": username},
                {"source": {"$in": filenames}}
            ]}
        else:
            where = {"username": username}

        results   = col.get(where=where, limit=120, include=["documents", "metadatas"])
        docs      = results.get("documents", []) or []
        metadatas = results.get("metadatas", []) or []

        chunks = []
        for doc, meta in zip(docs, metadatas):
            if doc and len(doc.strip()) > 40:
                chunks.append({
                    "text":   doc,
                    "source": meta.get("source", "Unknown") if meta else "Unknown",
                })
        return chunks
    except Exception as e:
        print(f"[KG] Chroma fetch error: {e}")
        return []


def _extract_triples_with_llm(chunks: list[dict], session_id: str) -> list[dict]:
    if not chunks:
        return []

    from collections import defaultdict
    from token_utils  import trim_to_budget
    from api_router   import call_llm_with_fallback

    by_source: dict[str, list[str]] = defaultdict(list)
    for ch in chunks:
        by_source[ch["source"]].append(ch["text"])

    all_triples: list[dict] = []

    for source, texts in list(by_source.items())[:6]:   # cap at 6 sources
        # ← KEY CHANGE: 1 400 tokens per source instead of raw 6 000 chars
        combined = trim_to_budget(" ".join(texts[:8]), 1_400)

        prompt = f"""Extract up to 15 factual triples from "{source}".
Each triple: short subject, verb-phrase relation, short object (all 2-6 words).
Output ONLY a JSON array with keys "subject", "relation", "object". No markdown.

Text: \\"\\"\\"{combined}\\"\\"\\"

JSON:"""

        try:
            raw = call_llm_with_fallback(
                prompt,
                {"model_name": "llama-3.3-70b-versatile", "temperature": 0.0, "max_tokens": 800}
            )
            raw = re.sub(r"^```[a-z]*\\n?|```$", "", raw, flags=re.MULTILINE).strip()
            match = re.search(r"\\[.*\\]", raw, re.DOTALL)
            if match:
                for t in json.loads(match.group()):
                    if isinstance(t, dict) and t.get("subject") and t.get("relation") and t.get("object"):
                        t["source"] = source
                        all_triples.append(t)
        except Exception as e:
            print(f"[KG] LLM error for {source}: {e}")

    return all_triples


def _build_graph_payload(triples: list[dict], filenames: list[str]) -> dict:
    """Convert triples to D3 force-graph nodes/edges payload.
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
        s      = triple.get("subject", "").strip()
        r      = triple.get("relation", "").strip()
        o      = triple.get("object", "").strip()
        src    = triple.get("source", "Unknown")        # ← PDF traceability
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

        # Deduplicate edges (same triple from same source → increment weight)
        edge_key = f"{sid}→{oid}→{r.lower()}"
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
                "sources":  [src],          # ← which PDFs this edge came from
            })

    # Add source-document nodes
    for fname in filenames:
        fkey = f"doc:{fname}"
        node_set[fkey] = {
            "id":      fkey,
            "label":   fname,
            "type":    "document",
            "size":    3,
            "sources": [fname],
        }

    nodes = list(node_set.values())
    clean_edges = [{k: v for k, v in e.items() if k != "_key"} for e in edges]

    return {
        "nodes": nodes,
        "edges": clean_edges,
        "stats": {
            "node_count":   len(nodes),
            "edge_count":   len(clean_edges),
            "triple_count": len(triples),
        }
    }


# ─── Route ───────────────────────────────────────────────────────────────────

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

    # Support both GET (query params) and POST (JSON body)
    if request.method == "POST":
        body       = request.json or {}
        session_id = body.get("session_id", "kg-session")
        filenames  = body.get("files") or []           # list from frontend
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
        return jsonify({"nodes": [], "edges": [], "stats": {"node_count": 0, "edge_count": 0, "triple_count": 0}})

    triples = _extract_triples_with_llm(chunks, session_id)
    payload = _build_graph_payload(triples, filenames or [])

    return jsonify(payload)
