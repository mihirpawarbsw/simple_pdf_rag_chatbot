"""
cluster_universe.py — Nexora AI Cluster Universe
=================================================
Groups a user's indexed Chroma documents into topic clusters using
the LLM, following the same Chroma-fetch and LLM-invocation patterns
as knowledge_graph.py.

Register in app.py:
    from cluster_universe import cluster_bp
    app.register_blueprint(cluster_bp)

Route:
    GET  /cluster_universe?files=file1.pdf,file2.pdf&session_id=...
    POST /cluster_universe  body: { "files": [...], "session_id": "..." }

Response:
    {
        "clusters": [
            {
                "id":       "cluster_0",
                "label":    "Machine Learning",
                "summary":  "Covers supervised and unsupervised learning techniques...",
                "keywords": ["neural networks", "training data", "gradient descent"],
                "sources":  ["doc_a.pdf", "doc_b.pdf"],
                "chunks":   3          ← number of chunks that belong here
            },
            ...
        ],
        "stats": {
            "total_chunks":   42,
            "cluster_count":  5,
            "sources_scanned": 3
        }
    }
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

import chromadb
from flask import Blueprint, jsonify, request, session

from token_utils import trim_to_budget
from api_router import call_llm_with_fallback

from rag_logic import CHROMA_PATH, CHROMA_COLLECTION, get_llm

cluster_bp = Blueprint("cluster_bp", __name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_llm_settings() -> dict:
    """Same model / temperature as knowledge_graph.py for consistency."""
    return {
        "model_name":  "llama-3.3-70b-versatile",
        "temperature": 0.0,
    }


def _fetch_chunks_for_user(username: str, filenames: list[str] | None = None) -> list[dict]:
    """
    Identical Chroma fetch pattern to knowledge_graph.py.
    Returns list of {"text": str, "source": str}.
    """
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col    = client.get_collection(CHROMA_COLLECTION)

        if filenames and len(filenames) >= 1:
            where: dict = {"$and": [
                {"username": username},
                {"source":   {"$in": filenames}},
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
        print(f"[CU] Chroma fetch error: {e}")
        return []


def _group_chunks_by_source(chunks: list[dict]) -> dict[str, list[str]]:
    """Group chunk text by source filename."""
    grouped = defaultdict(list)

    for chunk in chunks:
        grouped[chunk["source"]].append(chunk["text"])

    return grouped


def _build_cluster_prompt(chunks: list[dict]) -> str:
    """Create clustering prompt from source summaries."""

    grouped = _group_chunks_by_source(chunks)

    source_summaries = []

    for source, texts in list(grouped.items())[:8]:
        snippet = trim_to_budget(
            " ".join(texts[:5]),
            500
        )

        source_summaries.append(
            f'["{source}"]: {snippet}'
        )

    full_text = trim_to_budget(
        "\n\n".join(source_summaries),
        3500
    )

    return f"""
Group the documents below into 3-8 topic clusters.

For each cluster provide:
- label (2-5 words)
- summary (1 sentence)
- keywords (4-7 terms)
- sources (list of filenames)

Output ONLY a valid JSON array.
No markdown.

Documents:
\"\"\"{full_text}\"\"\"

JSON:
""".strip()


def _extract_json_array(raw_response: str) -> list[dict]:
    """Extract and parse JSON array from LLM response."""

    raw_response = re.sub(
        r"^```[a-z]*\n?|```$",
        "",
        raw_response,
        flags=re.MULTILINE,
    ).strip()

    match = re.search(r"\[.*\]", raw_response, re.DOTALL)

    if not match:
        return []

    return json.loads(match.group())


def _validate_clusters(
    clusters: list[dict],
    chunks: list[dict]
) -> list[dict]:
    """Validate and normalize cluster output."""

    validated = []

    for idx, cluster in enumerate(clusters):

        if not isinstance(cluster, dict):
            continue

        label = cluster.get("label", "").strip()

        if not label:
            continue

        keywords = cluster.get("keywords") or []
        sources = cluster.get("sources") or []

        if isinstance(sources, str):
            sources = [
                s.strip()
                for s in sources.split(",")
                if s.strip()
            ]

        keywords_lower = [
            kw.lower()
            for kw in keywords
        ]

        chunk_count = sum(
            1
            for chunk in chunks
            if any(
                kw in chunk["text"].lower()
                for kw in keywords_lower
            )
        )

        validated.append({
            "id": f"cluster_{idx}",
            "label": label,
            "summary": cluster.get("summary", "").strip(),
            "keywords": keywords,
            "sources": sources,
            "chunks": chunk_count,
        })

    return validated


def _cluster_chunks_with_llm(
    chunks: list[dict],
    session_id: str,
) -> list[dict]:
    """
    Generate topic clusters from retrieved chunks using LLM.
    """

    if not chunks:
        return []

    try:
        prompt = _build_cluster_prompt(chunks)

        response = call_llm_with_fallback(
            prompt,
            {
                "model_name": "llama-3.3-70b-versatile",
                "temperature": 0.0,
                "max_tokens": 800,
            },
        )

        clusters = _extract_json_array(response)

        if not clusters:
            return []

        return _validate_clusters(
            clusters=clusters,
            chunks=chunks,
        )

    except Exception as exc:
        print(f"[CU] LLM clustering error: {exc}")
        return []


# ─── Route ───────────────────────────────────────────────────────────────────

@cluster_bp.route("/cluster_universe", methods=["GET", "POST"])
def cluster_universe():
    """
    GET  /cluster_universe?files=file1.pdf,file2.pdf&session_id=...
    POST /cluster_universe  body: { "files": ["file1.pdf", ...], "session_id": "..." }

    Returns { clusters: [...], stats: {...} }.
    Defaults to ALL of the user's indexed documents when no files are specified.
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = session.get("username")

    # Support both GET and POST — mirrors knowledge_graph.py
    if request.method == "POST":
        body       = request.json or {}
        session_id = body.get("session_id", "cu-session")
        filenames  = body.get("files") or []
        if isinstance(filenames, str):
            filenames = [f.strip() for f in filenames.split(",") if f.strip()]
    else:
        session_id  = request.args.get("session_id", "cu-session")
        files_param = request.args.get("files", "")
        filenames   = [f.strip() for f in files_param.split(",") if f.strip()] if files_param else []

    # Default: use ALL documents indexed for this user (same DB query as KG)
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
            print(f"[CU] Could not fetch user file list: {e}")
            filenames = []

    chunks = _fetch_chunks_for_user(username, filenames if filenames else None)

    if not chunks:
        return jsonify({
            "clusters": [],
            "stats": {"total_chunks": 0, "cluster_count": 0, "sources_scanned": 0},
        })

    clusters = _cluster_chunks_with_llm(chunks, session_id)

    return jsonify({
        "clusters": clusters,
        "stats": {
            "total_chunks":    len(chunks),
            "cluster_count":   len(clusters),
            "sources_scanned": len(set(ch["source"] for ch in chunks)),
        },
    })
