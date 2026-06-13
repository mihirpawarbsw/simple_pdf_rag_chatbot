"""
action_tracker.py — Smart Action Items & Decision Tracker
==========================================================
Standalone Flask Blueprint.
Register in app.py with:
    from action_tracker import action_tracker_bp
    app.register_blueprint(action_tracker_bp)

Routes:
    POST /extract_action_items   — run LLM extraction over user docs
    GET  /get_action_items       — return stored items (all or by doc)
    POST /update_item_status     — update kanban status for one item
    POST /delete_action_item     — remove one item
    POST /clear_action_items     — wipe all items for user
"""

import json
import os
import re
import sqlite3
from datetime import datetime

import chromadb
from flask import Blueprint, jsonify, request, session

# ── reuse the same DB and ChromaDB constants as app.py ──────────────────────
from rag_logic import CHROMA_PATH, CHROMA_COLLECTION
DB_NAME = os.getenv("DB_NAME", "chat_history.db")

action_tracker_bp = Blueprint("action_tracker", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# DB initialisation — called once at import time
# ─────────────────────────────────────────────────────────────────────────────

def _init_tracker_db():
    conn   = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS extracted_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL,
            item_type   TEXT    NOT NULL,          -- action | deadline | decision
            text        TEXT    NOT NULL,
            owner       TEXT,
            due_date    TEXT,
            source_doc  TEXT,
            source_page INTEGER,
            status      TEXT    DEFAULT 'todo',    -- todo | inprogress | done
            extracted_at TEXT   NOT NULL
        )
    """)
    conn.commit()
    conn.close()


_init_tracker_db()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_logged_in():
    return session.get("logged_in")


def _get_llm_response(prompt: str, model: str = "llama-3.3-70b-versatile") -> str:
    """Key-rotating replacement for direct ChatGroq call."""
    from api_router import call_llm_with_fallback
    return call_llm_with_fallback(prompt, {"model_name": model, "temperature": 0.0, "max_tokens": 600})


def _fetch_user_chunks(username: str, selected_docs: list) -> list:
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col    = client.get_collection(CHROMA_COLLECTION)

        if selected_docs and len(selected_docs) >= 1:
            where: dict = {"$and": [
                {"username": username},
                {"source": {"$in": selected_docs}}
            ]}
        else:
            where = {"username": username}

        results   = col.get(where=where, limit=500, include=["documents", "metadatas"])
        docs      = results.get("documents", []) or []
        metadatas = results.get("metadatas",  []) or []

        chunks = []
        for doc, meta in zip(docs, metadatas):
            if doc and doc.strip():
                chunks.append({
                    "text":   doc,
                    "source": meta.get("source", "unknown") if meta else "unknown",
                    "page":   meta.get("page", 0)           if meta else 0,
                })
        return chunks
    except Exception as e:
        print(f"[ActionTracker] ChromaDB fetch error: {e}")
        return []


_EXTRACTION_PROMPT = """You are a precise enterprise document analyst.
Analyse the following document chunk and extract every:
  1. ACTION ITEM — a task someone must do (e.g. "Team must submit report by March 15")
  2. DEADLINE — a specific date or time-bound obligation (with surrounding context)
  3. DECISION — a resolved choice or approval (e.g. "Approved budget increase of $50K")

Rules:
- Output ONLY a valid JSON array. No markdown. No preamble.
- Each element must have these exact keys:
    item_type  : "action" | "deadline" | "decision"
    text       : concise description of the item (max 200 chars)
    owner      : person or team responsible — null if not mentioned
    due_date   : ISO date string YYYY-MM-DD if a date is present, else null
    source_page: the page number supplied below (integer)
- If nothing relevant is found, return an empty array [].

Source document: {source}
Page: {page}

DOCUMENT CHUNK:
\"\"\"
{chunk}
\"\"\"

JSON array:"""


def _extract_from_chunk(chunk: dict) -> list:
    prompt = _EXTRACTION_PROMPT.format(
        source=chunk["source"],
        page=chunk["page"],
        chunk=chunk["text"][:2000],  # ← reduced from 3000
    )
    try:
        raw   = _get_llm_response(prompt)
        raw   = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        items = json.loads(raw)
        return items if isinstance(items, list) else []
    except Exception as e:
        print(f"[ActionTracker] Extraction parse error: {e}")
        return []


def _dedupe_items(new_items: list, existing_texts: set) -> list:
    """Simple deduplication: skip items whose text is already stored."""
    seen   = set(existing_texts)
    result = []
    for item in new_items:
        key = item.get("text", "").strip().lower()[:120]
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _get_existing_texts(username: str) -> set:
    conn   = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT LOWER(SUBSTR(text,1,120)) FROM extracted_items WHERE username = ?",
        (username,)
    )
    rows = cursor.fetchall()
    conn.close()
    return {r[0] for r in rows}


def _store_items(username: str, items: list, source_doc: str):
    conn   = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in items:
        cursor.execute("""
            INSERT INTO extracted_items
              (username, item_type, text, owner, due_date,
               source_doc, source_page, status, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'todo', ?)
        """, (
            username,
            item.get("item_type", "action"),
            item.get("text", ""),
            item.get("owner"),
            item.get("due_date"),
            source_doc,
            item.get("source_page", 0),
            now,
        ))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@action_tracker_bp.route("/extract_action_items", methods=["POST"])
def extract_action_items():
    """
    POST body (JSON):
    {
        "selected_docs": ["file1.pdf", "file2.pdf"],   # empty = all user docs
        "force_refresh": false                          # re-run even if already extracted
    }
    """
    if not _is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data          = request.json or {}
    selected_docs = data.get("selected_docs", [])
    force_refresh = data.get("force_refresh", False)
    username      = session.get("username")

    chunks = _fetch_user_chunks(username, selected_docs)
    if not chunks:
        return jsonify({"status": "error", "message": "No document chunks found. Upload documents first."}), 400

    existing_texts = set() if force_refresh else _get_existing_texts(username)

    total_new   = 0
    docs_seen   = set()

    for chunk in chunks:
        items = _extract_from_chunk(chunk)
        if not items:
            continue
        # Attach source info from the chunk (LLM may not fill source_doc)
        for item in items:
            item["source_doc"]  = chunk["source"]
            item.setdefault("source_page", chunk["page"])

        deduped = _dedupe_items(items, existing_texts)
        if deduped:
            _store_items(username, deduped, chunk["source"])
            # Update existing_texts so we don't re-insert in the same run
            for d in deduped:
                existing_texts.add(d.get("text", "").strip().lower()[:120])
            total_new += len(deduped)
        docs_seen.add(chunk["source"])

    return jsonify({
        "status":      "success",
        "new_items":   total_new,
        "docs_scanned": len(docs_seen),
        "message":     f"Extracted {total_new} new items from {len(docs_seen)} document(s).",
    })


@action_tracker_bp.route("/get_action_items", methods=["GET"])
def get_action_items():
    """
    Query params:
        source_doc  — filter by document (optional)
        status      — filter by status: todo | inprogress | done (optional)
        item_type   — filter by type: action | deadline | decision (optional)
    """
    if not _is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username   = session.get("username")
    source_doc = request.args.get("source_doc")
    status     = request.args.get("status")
    item_type  = request.args.get("item_type")

    query  = "SELECT id, item_type, text, owner, due_date, source_doc, source_page, status, extracted_at FROM extracted_items WHERE username = ?"
    params = [username]

    if source_doc:
        query += " AND source_doc = ?"
        params.append(source_doc)
    if status:
        query += " AND status = ?"
        params.append(status)
    if item_type:
        query += " AND item_type = ?"
        params.append(item_type)

    query += " ORDER BY due_date ASC NULLS LAST, id DESC"

    conn   = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    items = []
    today = datetime.now().date()
    for row in rows:
        item_id, itype, text, owner, due_date, src_doc, src_page, st, ext_at = row
        urgency = "none"
        if due_date:
            try:
                d = datetime.strptime(due_date, "%Y-%m-%d").date()
                if d < today:
                    urgency = "overdue"
                elif (d - today).days <= 7:
                    urgency = "soon"
                else:
                    urgency = "upcoming"
            except ValueError:
                pass
        items.append({
            "id":           item_id,
            "item_type":    itype,
            "text":         text,
            "owner":        owner,
            "due_date":     due_date,
            "source_doc":   src_doc,
            "source_page":  src_page,
            "status":       st,
            "urgency":      urgency,
            "extracted_at": ext_at,
        })

    # Summary counts for the alert banner
    overdue_count = sum(1 for i in items if i["urgency"] == "overdue" and i["status"] != "done")
    return jsonify({
        "status":        "success",
        "items":         items,
        "total":         len(items),
        "overdue_count": overdue_count,
    })


@action_tracker_bp.route("/update_item_status", methods=["POST"])
def update_item_status():
    """
    POST body (JSON):
    { "item_id": 42, "status": "inprogress" }
    """
    if not _is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data     = request.json or {}
    item_id  = data.get("item_id")
    status   = data.get("status")
    username = session.get("username")

    if not item_id or status not in ("todo", "inprogress", "done"):
        return jsonify({"status": "error", "message": "Invalid item_id or status"}), 400

    conn   = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE extracted_items SET status = ? WHERE id = ? AND username = ?",
        (status, item_id, username)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Status updated"})


@action_tracker_bp.route("/delete_action_item", methods=["POST"])
def delete_action_item():
    """POST body (JSON): { "item_id": 42 }"""
    if not _is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    item_id  = (request.json or {}).get("item_id")
    username = session.get("username")

    if not item_id:
        return jsonify({"status": "error", "message": "item_id required"}), 400

    conn   = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM extracted_items WHERE id = ? AND username = ?",
        (item_id, username)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Item deleted"})


@action_tracker_bp.route("/clear_action_items", methods=["POST"])
def clear_action_items():
    """Wipe all extracted items for the logged-in user."""
    if not _is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = session.get("username")
    conn     = sqlite3.connect(DB_NAME)
    cursor   = conn.cursor()
    cursor.execute("DELETE FROM extracted_items WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "All items cleared"})
