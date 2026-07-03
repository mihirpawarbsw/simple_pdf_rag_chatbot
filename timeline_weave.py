"""
timeline_weave.py — Nexora AI  |  TimelineWeave
================================================================
Plots your document's topics against real-world events on a live
timeline — "what happened, when" — sourced fresh from the web.

Token-efficiency design (vs. web_augmentor's 1 + N + 1 LLM calls):
  1. Extract 3-5 CLUSTERS from doc chunks   (1 LLM call, tiny schema:
     title + doc_context + a single date-scoped search query — NOT
     3 queries + doc_claim like web_augmentor)
  2. Tavily search — ONE query per cluster  (no LLM involved)
  3. ALL clusters' search snippets are batched into a SINGLE final
     LLM call that returns the whole timeline + summary at once
     (1 LLM call total, vs. N separate per-topic synthesis calls)
  4. Build structured JSON report object  (single source of truth)
  5a. Return JSON for in-app viewer       (GET /timeline_weave/view)
  5b. Render PDF server-side via WeasyPrint (POST /timeline_weave/export_pdf)

Total LLM calls per report: 2 (fixed), regardless of cluster count.
web_augmentor.py by comparison makes 1 + N + 1 (up to 9) calls.

Register in app.py:
    from timeline_weave import timeline_weave_bp
    app.register_blueprint(timeline_weave_bp)

Routes:
    POST /timeline_weave/generate
         Body: { "files": [...], "session_id": "..." }
         Returns: { "report": <ReportJSON>, "status": "ok" }

    POST /timeline_weave/export_pdf
         Body: { "report": <ReportJSON> }
         Returns: PDF file download

    GET  /timeline_weave/history
         Returns: [ list of saved reports for user ]

    GET  /timeline_weave/history/<report_id>
         Returns: full ReportJSON

    DELETE /timeline_weave/history/<report_id>
         Deletes a saved report

ReportJSON schema:
    {
        "id":           "tlw_<uuid>",
        "title":        "TimelineWeave Report",
        "doc_names":    ["file.pdf", ...],
        "generated_at": "2026-07-03 12:00:00",
        "freshness_label": "Live as of …",
        "summary":      "…",
        "clusters": [
            { "id": "c0", "title": "RAG Architecture", "doc_context": "…" },
            …
        ],
        "timeline": [
            {
                "id":            "e0",
                "cluster_id":    "c0",
                "cluster_title": "RAG Architecture",
                "event":         "…",
                "date":          "2025-11",
                "source_title":  "…",
                "source_url":    "…",
                "source_type":   "article|news|tweet|review|research"
            },
            …
        ],
        "sources": [ { "title":"…", "url":"…", "type":"…" }, … ]
    }
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from collections import defaultdict

import chromadb
from flask import Blueprint, jsonify, request, session, Response

from token_utils import trim_to_budget
from api_router import call_llm_with_fallback
from rag_logic import CHROMA_PATH, CHROMA_COLLECTION

timeline_weave_bp = Blueprint("timeline_weave_bp", __name__, url_prefix="/timeline_weave")

# ── DB (reuse app's DB_NAME via env or default) ───────────────────────────────
DB_NAME = os.getenv("DB_NAME", "chat_history.db")

# ── Web-search provider config ────────────────────────────────────────────────
# Tavily only.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ── LLM config ────────────────────────────────────────────────────────────────
# Cluster extraction: small schema (title + doc_context + ONE query),
# so max_tokens stays low even at 5 clusters.
_CLUSTER_CFG = {
    "model_name":  "llama-3.3-70b-versatile",
    "temperature": 0.0,
    "max_tokens":  700,
}

# Final synthesis: ONE call covers every cluster's timeline events +
# the executive summary, so this is the only "big" call in the pipeline.
_SYNTH_CFG = {
    "model_name":  "llama-3.3-70b-versatile",
    "temperature": 0.2,
    "max_tokens":  1400,
}

MAX_CLUSTERS = 5
RESULTS_PER_CLUSTER = 4  # keep the batched prompt small


# ═════════════════════════════════════════════════════════════════════════════
# DB helpers
# ═════════════════════════════════════════════════════════════════════════════

def _init_tlw_table() -> None:
    """Create timeline_weave_reports table if it doesn't exist."""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS timeline_weave_reports (
            id          TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _save_report(username: str, report: dict) -> None:
    _init_tlw_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO timeline_weave_reports (id, username, report_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (report["id"], username, json.dumps(report), report["generated_at"])
    )
    conn.commit()
    conn.close()


def _get_reports(username: str) -> list[dict]:
    _init_tlw_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, report_json, created_at FROM timeline_weave_reports "
        "WHERE username = ? ORDER BY created_at DESC LIMIT 20",
        (username,)
    )
    rows = cur.fetchall()
    conn.close()
    reports = []
    for row in rows:
        try:
            r = json.loads(row[1])
            reports.append({
                "id":           r.get("id", row[0]),
                "title":        r.get("title", "TimelineWeave Report"),
                "doc_names":    r.get("doc_names", []),
                "generated_at": r.get("generated_at", row[2]),
                "event_count":  len(r.get("timeline", [])),
                "cluster_count": len(r.get("clusters", [])),
            })
        except Exception:
            pass
    return reports


def _get_report_by_id(username: str, report_id: str) -> dict | None:
    _init_tlw_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT report_json FROM timeline_weave_reports WHERE id = ? AND username = ?",
        (report_id, username)
    )
    row = cur.fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None


def _delete_report(username: str, report_id: str) -> bool:
    _init_tlw_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM timeline_weave_reports WHERE id = ? AND username = ?",
        (report_id, username)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# ═════════════════════════════════════════════════════════════════════════════
# Chroma helpers  (same pattern as web_augmentor.py)
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_chunks(username: str, filenames: list[str]) -> list[dict]:
    """Return list of {"text": str, "source": str} from ChromaDB."""
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col = client.get_collection(CHROMA_COLLECTION)

        where: dict = (
            {"$and": [{"username": username}, {"source": {"$in": filenames}}]}
            if filenames else {"username": username}
        )

        results = col.get(where=where, limit=150, include=["documents", "metadatas"])
        docs = results.get("documents", []) or []
        metadatas = results.get("metadatas", []) or []

        return [
            {"text": doc, "source": meta.get("source", "Unknown") if meta else "Unknown"}
            for doc, meta in zip(docs, metadatas)
            if doc and len(doc.strip()) > 40
        ]
    except Exception as e:
        print(f"[TLW] Chroma error: {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Step 1 — Cluster extraction  (cheap: title + doc_context + ONE query)
# ═════════════════════════════════════════════════════════════════════════════

def _extract_clusters(chunks: list[dict]) -> list[dict]:
    """
    Ask the LLM to pull 3-5 topic CLUSTERS from the document chunks.
    Unlike web_augmentor's 5-8 topics × 3 queries, this asks for a
    minimal schema per cluster to keep both the prompt and the
    response small.
    Returns list of {"title": str, "doc_context": str, "search_query": str}
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        grouped[c["source"]].append(c["text"])

    source_blobs = []
    for src, texts in list(grouped.items())[:6]:
        blob = trim_to_budget(" ".join(texts[:5]), 500)
        source_blobs.append(f'[{src}]: {blob}')

    context = trim_to_budget("\n\n".join(source_blobs), 3000)

    prompt = f"""You are a research analyst building a timeline.

Read the document excerpts below and extract 3-5 KEY CLUSTERS (themes/topics)
central to the documents, each of which has a real-world timeline of events.

For each cluster provide ONLY:
- title        : 3-6 word cluster label
- doc_context  : 1 short sentence (max 15 words) on what the doc says about it
- search_query : ONE targeted web search query to find dated news/events about
                 this cluster over time (include a phrase like "timeline" or
                 "history 2023 2024 2025 2026" to bias results toward dated events)

Output ONLY a valid JSON array. No markdown. No preamble. Be terse.

Documents:
\"\"\"
{context}
\"\"\"

JSON:"""

    raw = call_llm_with_fallback(prompt, _CLUSTER_CFG)
    clusters = _parse_json_array(raw, fallback=[])

    out = []
    for i, c in enumerate(clusters[:MAX_CLUSTERS]):
        out.append({
            "id":           f"c{i}",
            "title":        c.get("title", f"Cluster {i+1}"),
            "doc_context":  c.get("doc_context", ""),
            "search_query": c.get("search_query", c.get("title", "") + " timeline"),
        })
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — Web search  (Tavily only, ONE query per cluster)
# ═════════════════════════════════════════════════════════════════════════════

def _web_search(query: str, num_results: int = RESULTS_PER_CLUSTER) -> list[dict]:
    """
    Search the web for a query using Tavily. Returns list of:
    { "title": str, "snippet": str, "url": str, "source_type": str }
    """
    if not TAVILY_API_KEY:
        print(
            "[TLW] _web_search: TAVILY_API_KEY is empty/unset — skipping search. "
            "Set the TAVILY_API_KEY environment variable (and restart the app "
            "process so it picks up the new env var) to enable web search."
        )
        return []

    try:
        import requests
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key":             TAVILY_API_KEY,
                "query":               query,
                "search_depth":        "advanced",
                "max_results":         num_results,
                "include_answer":      False,
                "include_raw_content": False,
            },
            timeout=10,
        )

        if resp.status_code != 200:
            print(f"[TLW] Tavily HTTP {resp.status_code} for query={query!r}: {resp.text[:500]}")
            return []

        data = resp.json()

        if "error" in data:
            print(f"[TLW] Tavily API error for query={query!r}: {data['error']}")
            return []

        raw_results = data.get("results", [])
        if not raw_results:
            print(f"[TLW] Tavily returned 0 results for query={query!r}.")
            return []

        results = []
        for item in raw_results:
            results.append({
                "title":       item.get("title", ""),
                "snippet":     item.get("content", "")[:220],
                "url":         item.get("url", ""),
                "source_type": _classify_source(item.get("url", "")),
            })

        print(f"[TLW] Tavily returned {len(results)} result(s) for query={query!r}")
        return results[:num_results]

    except requests.exceptions.Timeout:
        print(f"[TLW] Tavily timeout for query={query!r}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[TLW] Tavily request error for query={query!r}: {e}")
        return []
    except Exception as e:
        print(f"[TLW] Tavily unexpected error for query={query!r}: {e}")
        return []


def _classify_source(url: str) -> str:
    """Heuristically label a URL as article / news / tweet / review / blog."""
    url_l = url.lower()
    if "twitter.com" in url_l or "x.com" in url_l:
        return "tweet"
    if any(x in url_l for x in ["reddit.com", "hackernews", "ycombinator"]):
        return "forum"
    if any(x in url_l for x in ["review", "g2.com", "capterra", "trustpilot", "producthunt"]):
        return "review"
    if any(x in url_l for x in [
        "bbc.com", "cnn.com", "reuters.com", "techcrunch.com", "theverge.com",
        "wired.com", "zdnet.com", "venturebeat.com", "arstechnica.com",
        "bloomberg.com", "ft.com", "wsj.com", "economist.com", "forbes.com",
        "businessinsider.com", "cnbc.com", "nytimes.com", "guardian.com",
    ]):
        return "news"
    if any(x in url_l for x in ["medium.com", "substack.com", "dev.to", "hashnode"]):
        return "blog"
    if any(x in url_l for x in ["arxiv.org", "semanticscholar", "researchgate", "springer", "acm.org"]):
        return "research"
    return "article"


# ═════════════════════════════════════════════════════════════════════════════
# Step 3 — Single batched synthesis  (ALL clusters, ONE LLM call)
# ═════════════════════════════════════════════════════════════════════════════

def _synthesise_timeline(clusters: list[dict], results_by_cluster: dict[str, list[dict]]) -> dict:
    """
    Feed every cluster's search snippets into a SINGLE LLM call and get
    back the full timeline (all clusters) + one executive summary.
    This is the main token-saving move vs. web_augmentor's per-topic loop.

    Snippets are indexed per-cluster (idx 0,1,2…) so the model references
    "idx" instead of repeating full titles/urls in its output — python
    maps idx back to the real source dict afterwards.
    """
    has_any_results = any(results_by_cluster.get(c["id"]) for c in clusters)
    if not has_any_results:
        return {"summary": "No web data could be retrieved for any cluster.", "timeline": []}

    blocks = []
    for c in clusters:
        hits = results_by_cluster.get(c["id"], [])
        if not hits:
            blocks.append(f'CLUSTER {c["id"]} — "{c["title"]}" (doc says: {c["doc_context"]})\n  No web results.')
            continue
        lines = "\n".join(
            f'  [{i}] ({r["source_type"]}) {r["title"]} — {r["snippet"][:160]}'
            for i, r in enumerate(hits)
        )
        blocks.append(f'CLUSTER {c["id"]} — "{c["title"]}" (doc says: {c["doc_context"]})\n{lines}')

    web_block = "\n\n".join(blocks)

    prompt = f"""You are building a timeline that plots document topics against real-world events.

{web_block}

Your task, across ALL clusters above, in one pass:
1. For each numbered [idx] result that represents a datable event, emit ONE
   timeline entry: {{ "cluster_id", "event" (max 12 words), "date" (best guess,
   format "YYYY" or "YYYY-MM" or "recent" if unclear), "idx" }}.
   Skip results that aren't really an "event" (e.g. generic overviews).
   Cap at 3 entries per cluster, prioritising the most recent/significant.
2. Write ONE overall "summary": 2-4 sentences on how the doc's topics are
   trending in the real world right now, referencing specific clusters.

Output ONLY valid JSON, no markdown, no preamble:
{{
  "summary": "…",
  "timeline": [
    {{ "cluster_id": "c0", "event": "…", "date": "2025-11", "idx": 0 }}
  ]
}}"""

    raw = call_llm_with_fallback(prompt, _SYNTH_CFG)
    return _parse_json_object(raw, fallback={"summary": "", "timeline": []})


# ═════════════════════════════════════════════════════════════════════════════
# Step 4 — Orchestrator
# ═════════════════════════════════════════════════════════════════════════════

def run_timeline_weave(
    username:   str,
    filenames:  list[str],
    session_id: str,
) -> dict:
    """
    Full pipeline. Returns a ReportJSON dict.
    Designed to be called from the Flask route.
    """
    report_id = f"tlw_{uuid.uuid4().hex[:12]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Fetch doc chunks
    chunks = _fetch_chunks(username, filenames)
    if not chunks:
        return {
            "id": report_id, "title": "TimelineWeave Report",
            "doc_names": filenames, "generated_at": now_str,
            "freshness_label": f"Generated {now_str}",
            "summary": "No document content found. Please upload and index files first.",
            "clusters": [], "timeline": [], "sources": [],
        }

    # 2. Extract clusters  (1 LLM call)
    clusters: list[dict] = _extract_clusters(chunks)
    if not clusters:
        clusters = [{
            "id": "c0", "title": "General Overview",
            "doc_context": "Content from uploaded documents.",
            "search_query": f"{filenames[0] if filenames else 'topic'} timeline history 2025 2026",
        }]

    # 3. Web search — ONE query per cluster (no LLM involved)
    results_by_cluster: dict[str, list[dict]] = {}
    all_web_sources: list[dict] = []
    for c in clusters:
        hits = _web_search(c["search_query"], num_results=RESULTS_PER_CLUSTER)
        # Dedup by URL within cluster
        seen: set[str] = set()
        unique = []
        for h in hits:
            if h["url"] and h["url"] not in seen:
                seen.add(h["url"])
                unique.append(h)
        results_by_cluster[c["id"]] = unique
        all_web_sources.extend(unique)

    # 4. Single batched synthesis call  (1 LLM call, covers ALL clusters)
    synth = _synthesise_timeline(clusters, results_by_cluster)

    # 5. Map "idx" references back to real source dicts, build final timeline
    timeline: list[dict] = []
    for i, ev in enumerate(synth.get("timeline", [])):
        cid = ev.get("cluster_id", "")
        idx = ev.get("idx")
        cluster = next((c for c in clusters if c["id"] == cid), None)
        hits = results_by_cluster.get(cid, [])
        src = hits[idx] if isinstance(idx, int) and 0 <= idx < len(hits) else None

        timeline.append({
            "id":            f"e{i}",
            "cluster_id":    cid,
            "cluster_title": cluster["title"] if cluster else cid,
            "event":         ev.get("event", ""),
            "date":          ev.get("date", "recent"),
            "source_title":  src["title"]       if src else "",
            "source_url":    src["url"]         if src else "",
            "source_type":   src["source_type"] if src else "article",
        })

    # Sort by date string, unknowns/"recent" last
    def _sort_key(e):
        d = e.get("date", "")
        return (0, d) if re.match(r"^\d{4}", d) else (1, d)
    timeline.sort(key=_sort_key)

    # 6. Deduplicate sources list
    seen_src_urls: set[str] = set()
    sources: list[dict] = []
    for s in all_web_sources:
        if s["url"] and s["url"] not in seen_src_urls:
            seen_src_urls.add(s["url"])
            sources.append({"title": s["title"], "url": s["url"], "type": s["source_type"]})

    report: dict = {
        "id":                report_id,
        "title":             "TimelineWeave Report",
        "doc_names":         filenames,
        "generated_at":      now_str,
        "freshness_label":   f"Live web data as of {datetime.now().strftime('%d %b %Y, %H:%M')}",
        "summary":           synth.get("summary", ""),
        "clusters":          clusters,
        "timeline":          timeline,
        "sources":           sources[:25],
    }

    # 7. Persist to DB
    try:
        _save_report(username, report)
    except Exception as e:
        print(f"[TLW] DB save error: {e}")

    return report


# ═════════════════════════════════════════════════════════════════════════════
# PDF export via WeasyPrint
# ═════════════════════════════════════════════════════════════════════════════

_SOURCE_ICONS = {
    "news":     "📰",
    "tweet":    "🐦",
    "review":   "⭐",
    "blog":     "✍️",
    "forum":    "💬",
    "research": "🔬",
    "article":  "🔗",
}

_CLUSTER_COLORS = ["#0d9488", "#f59e0b", "#7c3aed", "#3b82f6", "#ec4899"]


def _render_report_html(report: dict) -> str:
    """Render report dict to a self-contained HTML string for WeasyPrint."""

    doc_names_str = ", ".join(report.get("doc_names", []))
    gen_at = report.get("generated_at", "")
    freshness = report.get("freshness_label", "")
    summary = report.get("summary", "").replace("\n", "<br>")

    clusters = report.get("clusters", [])
    color_by_cluster = {c["id"]: _CLUSTER_COLORS[i % len(_CLUSTER_COLORS)] for i, c in enumerate(clusters)}

    # Legend
    legend_html = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;font-size:12px;color:#374151">'
        f'<span style="width:10px;height:10px;border-radius:50%;background:{color_by_cluster.get(c["id"],"#6b7280")};display:inline-block"></span>'
        f'{c.get("title","")}</span>'
        for c in clusters
    )

    # Timeline entries
    timeline_html = ""
    for ev in report.get("timeline", []):
        color = color_by_cluster.get(ev.get("cluster_id", ""), "#6b7280")
        icon = _SOURCE_ICONS.get(ev.get("source_type", "article"), "🔗")
        src_line = (
            f'<a href="{ev.get("source_url","")}" style="font-size:11px;color:#6366f1;word-break:break-all">{icon} {ev.get("source_title","")[:70]}</a>'
            if ev.get("source_url") else ""
        )
        timeline_html += f"""
        <div style="display:flex;gap:14px;margin-bottom:18px">
            <div style="flex-shrink:0;width:80px;text-align:right;font-size:12px;font-weight:700;color:{color};padding-top:2px">{ev.get('date','')}</div>
            <div style="flex-shrink:0;width:14px;display:flex;flex-direction:column;align-items:center">
                <div style="width:12px;height:12px;border-radius:50%;background:{color};margin-top:4px"></div>
                <div style="flex:1;width:2px;background:#e5e7eb;margin-top:2px"></div>
            </div>
            <div style="flex:1;padding-bottom:4px">
                <div style="font-size:11px;font-weight:600;color:{color};text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px">{ev.get('cluster_title','')}</div>
                <div style="font-size:13px;color:#1f2937;font-weight:600;margin-bottom:4px">{ev.get('event','')}</div>
                {src_line}
            </div>
        </div>"""

    # Sources
    sources_html = ""
    for s in report.get("sources", [])[:15]:
        icon = _SOURCE_ICONS.get(s.get("type", "article"), "🔗")
        sources_html += f'<li style="margin-bottom:4px;font-size:12px"><span style="margin-right:4px">{icon}</span><a href="{s.get("url","")}" style="color:#6366f1">{s.get("title","") or s.get("url","")[:60]}</a></li>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', sans-serif; background: #fff; color: #1f2937; font-size: 14px; line-height: 1.5; }}
  @page {{ margin: 20mm 18mm; size: A4; }}
</style>
</head>
<body>
  <!-- Cover strip -->
  <div style="background:linear-gradient(135deg,#0d9488,#0891b2,#3b82f6);padding:32px 36px;margin-bottom:32px;border-radius:12px">
    <div style="font-size:11px;color:rgba(255,255,255,.7);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px">Nexora AI · TimelineWeave</div>
    <div style="font-size:26px;font-weight:800;color:#fff;margin-bottom:4px">{report.get('title','')}</div>
    <div style="font-size:13px;color:rgba(255,255,255,.8);margin-bottom:18px">Documents: {doc_names_str}</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <span style="background:rgba(255,255,255,.18);border-radius:8px;padding:6px 14px;font-size:12px;color:#fff">📅 {gen_at}</span>
      <span style="background:rgba(255,255,255,.18);border-radius:8px;padding:6px 14px;font-size:12px;color:#fff">🌐 {freshness}</span>
    </div>
  </div>

  <!-- Summary -->
  <div style="margin-bottom:28px;padding:22px 24px;background:#f0fdfa;border-radius:12px;border:1px solid #99f6e4">
    <div style="font-size:12px;font-weight:700;color:#0d9488;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">📋 Summary</div>
    <p style="font-size:14px;line-height:1.7;color:#1f2937">{summary}</p>
  </div>

  <!-- Legend -->
  <div style="margin-bottom:20px">{legend_html}</div>

  <!-- Timeline -->
  <div style="font-size:14px;font-weight:700;color:#1f2937;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #e5e7eb">
    🕒 Event Timeline
  </div>
  {timeline_html or '<p style="color:#6b7280;font-size:13px">No dated events found.</p>'}

  <!-- Sources -->
  <div style="padding:18px;background:#f9fafb;border-radius:12px;border:1px solid #e5e7eb;margin-top:20px;margin-bottom:20px">
    <div style="font-size:12px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px">🔗 Web Sources Consulted</div>
    <ul style="padding-left:16px;columns:2;gap:20px">{sources_html or '<li>No sources.</li>'}</ul>
  </div>

  <div style="text-align:center;font-size:11px;color:#9ca3af;padding-top:12px;border-top:1px solid #e5e7eb">
    Generated by Nexora AI TimelineWeave · {gen_at} · For internal use only
  </div>
</body>
</html>"""


def _export_pdf(report: dict) -> bytes:
    """Render HTML report to PDF bytes using WeasyPrint."""
    try:
        from weasyprint import HTML
        html_str = _render_report_html(report)
        return HTML(string=html_str).write_pdf()
    except ImportError:
        raise RuntimeError(
            "WeasyPrint is not installed. Run: pip install weasyprint --break-system-packages"
        )


# ═════════════════════════════════════════════════════════════════════════════
# JSON parsing helpers  (same defensive strategy as web_augmentor.py)
# ═════════════════════════════════════════════════════════════════════════════

def _clean_llm_json(raw: str) -> str:
    """Strip markdown fences and common LLM JSON artefacts."""
    raw = re.sub(r"```[a-z]*", "", raw, flags=re.IGNORECASE).replace("```", "")
    raw = re.sub(r"(?m)//[^\n]*$", "", raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    return raw.strip()


def _extract_json_block(text: str, opener: str, closer: str):
    """Find the OUTERMOST balanced JSON object or array in *text*."""
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _try_parse(candidate, expected_type):
    if not candidate:
        return None
    try:
        result = json.loads(candidate)
        if isinstance(result, expected_type):
            return result
    except Exception:
        pass
    return None


def _looks_truncated(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if t[-1] not in "]}":
        return True
    unescaped_quotes = len(re.findall(r'(?<!\\)"', t))
    return unescaped_quotes % 2 == 1


def _repair_truncated_array(text: str):
    """Best-effort recovery for a JSON array cut off mid-stream."""
    start = text.find("[")
    if start == -1:
        return None

    depth = 0
    in_str = False
    escape = False
    elem_start = None
    elements = []

    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\" and in_str:
            escape = True
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
            i += 1
            continue
        if in_str:
            i += 1
            continue

        if ch in "[{":
            if depth == 1 and ch == "{" and elem_start is None:
                elem_start = i
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 1 and ch == "}" and elem_start is not None:
                candidate = text[elem_start:i + 1]
                parsed = _try_parse(candidate, dict)
                if parsed is not None:
                    elements.append(parsed)
                elem_start = None
            if depth == 0:
                break
        i += 1

    return elements if elements else None


def _parse_json_array(raw: str, fallback=None) -> list:
    import traceback
    if fallback is None:
        fallback = []

    print(f"[TLW] _parse_json_array raw output ({len(raw)} chars):\n{raw[:800]}{'...' if len(raw) > 800 else ''}")

    cleaned = _clean_llm_json(raw)

    result = _try_parse(cleaned, list)
    if result is not None:
        return result

    block = _extract_json_block(cleaned, "[", "]")
    result = _try_parse(block, list)
    if result is not None:
        return result

    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if m:
        result = _try_parse(m.group(), list)
        if result is not None:
            return result

    if _looks_truncated(cleaned):
        repaired = _repair_truncated_array(cleaned)
        if repaired is not None:
            print(f"[TLW] _parse_json_array: response was TRUNCATED — salvaged {len(repaired)} element(s).")
            return repaired

    try:
        json.loads(block or cleaned or raw)
    except Exception:
        print("[TLW] JSON array parse error — all passes failed. Last exception:")
        traceback.print_exc()

    return fallback


def _parse_json_object(raw: str, fallback=None) -> dict:
    import traceback
    if fallback is None:
        fallback = {}

    print(f"[TLW] _parse_json_object raw output ({len(raw)} chars):\n{raw[:800]}{'...' if len(raw) > 800 else ''}")

    cleaned = _clean_llm_json(raw)

    result = _try_parse(cleaned, dict)
    if result is not None:
        return result

    block = _extract_json_block(cleaned, "{", "}")
    result = _try_parse(block, dict)
    if result is not None:
        return result

    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        result = _try_parse(m.group(), dict)
        if result is not None:
            return result

    try:
        json.loads(block or cleaned or raw)
    except Exception:
        print("[TLW] JSON object parse error — all passes failed. Last exception:")
        traceback.print_exc()

    return fallback


# ═════════════════════════════════════════════════════════════════════════════
# Flask routes
# ═════════════════════════════════════════════════════════════════════════════

@timeline_weave_bp.route("/generate", methods=["POST"])
def generate():
    """
    POST /timeline_weave/generate
    Body: { "files": [...], "session_id": "..." }
    Returns: { "report": <ReportJSON>, "status": "ok" }
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = session.get("username")
    body = request.json or {}
    session_id = body.get("session_id", "tlw-session")
    filenames = body.get("files") or []

    if isinstance(filenames, str):
        filenames = [f.strip() for f in filenames.split(",") if f.strip()]

    if not filenames:
        try:
            conn = sqlite3.connect(DB_NAME)
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT filename FROM uploaded_files WHERE username = ? ORDER BY filename",
                (username,)
            )
            filenames = [r[0] for r in cur.fetchall()]
            conn.close()
        except Exception as e:
            print(f"[TLW] file list fallback error: {e}")

    try:
        report = run_timeline_weave(username, filenames, session_id)
        return jsonify({"status": "ok", "report": report})
    except Exception as e:
        print(f"[TLW] generate error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@timeline_weave_bp.route("/export_pdf", methods=["POST"])
def export_pdf():
    """
    POST /timeline_weave/export_pdf
    Body: { "report": <ReportJSON> }
    Returns: PDF file
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    body = request.json or {}
    report = body.get("report")
    if not report:
        return jsonify({"status": "error", "message": "No report data provided"}), 400

    try:
        pdf_bytes = _export_pdf(report)
        filename = f"nexora_timeline_weave_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        print(f"[TLW] PDF export error: {e}")
        return jsonify({"status": "error", "message": "PDF generation failed"}), 500


@timeline_weave_bp.route("/history", methods=["GET"])
def history():
    """GET /timeline_weave/history — list saved reports for the user."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    return jsonify({"status": "ok", "reports": _get_reports(username)})


@timeline_weave_bp.route("/history/<report_id>", methods=["GET"])
def get_report(report_id: str):
    """GET /timeline_weave/history/<id> — fetch full report JSON."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    report = _get_report_by_id(username, report_id)
    if report is None:
        return jsonify({"status": "error", "message": "Report not found"}), 404
    return jsonify({"status": "ok", "report": report})


@timeline_weave_bp.route("/history/<report_id>", methods=["DELETE"])
def delete_report(report_id: str):
    """DELETE /timeline_weave/history/<id>"""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    ok = _delete_report(username, report_id)
    if ok:
        return jsonify({"status": "ok", "message": "Report deleted"})
    return jsonify({"status": "error", "message": "Report not found"}), 404
