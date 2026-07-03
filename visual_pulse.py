"""
visual_pulse.py — Nexora AI  |  VisualPulse
================================================================
Turns your document's key topics into a live visual mood-board —
real images pulled fresh from the web for each topic, curated
by AI into a single masonry gallery with captions.

Token-efficiency design (same pattern as timeline_weave.py):
  1. Extract 3-5 CLUSTERS from doc chunks   (1 LLM call, tiny schema:
     title + doc_context + a single image-search query)
  2. Tavily search — ONE query per cluster, include_images=true
     (no LLM involved, images come straight back from Tavily)
  3. ALL clusters' candidate images are batched into a SINGLE final
     LLM call that curates the best 2-3 per cluster + writes captions
     + one overall summary (1 LLM call total, vs. per-topic loops)
  4. Build structured JSON report object  (single source of truth)
  5a. Return JSON for in-app viewer       (GET /visual_pulse/view)
  5b. Render PDF server-side via WeasyPrint (POST /visual_pulse/export_pdf)

Total LLM calls per report: 2 (fixed), regardless of cluster count.

Register in app.py:
    from visual_pulse import visual_pulse_bp
    app.register_blueprint(visual_pulse_bp)

Routes:
    POST /visual_pulse/generate
         Body: { "files": [...], "session_id": "..." }
         Returns: { "report": <ReportJSON>, "status": "ok" }

    POST /visual_pulse/export_pdf
         Body: { "report": <ReportJSON> }
         Returns: PDF file download

    GET  /visual_pulse/history
         Returns: [ list of saved reports for user ]

    GET  /visual_pulse/history/<report_id>
         Returns: full ReportJSON

    DELETE /visual_pulse/history/<report_id>
         Deletes a saved report

ReportJSON schema:
    {
        "id":           "vp_<uuid>",
        "title":        "VisualPulse Report",
        "doc_names":    ["file.pdf", ...],
        "generated_at": "2026-07-03 12:00:00",
        "freshness_label": "Live as of …",
        "summary":      "…",
        "clusters": [
            { "id": "c0", "title": "RAG Architecture", "doc_context": "…" },
            …
        ],
        "gallery": [
            {
                "id":            "g0",
                "cluster_id":    "c0",
                "cluster_title": "RAG Architecture",
                "image_url":     "…",
                "caption":       "…",
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
from urllib.parse import urlparse

import chromadb
from flask import Blueprint, jsonify, request, session, Response

from token_utils import trim_to_budget
from api_router import call_llm_with_fallback
from rag_logic import CHROMA_PATH, CHROMA_COLLECTION

visual_pulse_bp = Blueprint("visual_pulse_bp", __name__, url_prefix="/visual_pulse")

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

# Final curation: ONE call covers every cluster's image picks +
# the executive summary, so this is the only "big" call in the pipeline.
_SYNTH_CFG = {
    "model_name":  "llama-3.3-70b-versatile",
    "temperature": 0.2,
    "max_tokens":  1400,
}

MAX_CLUSTERS = 5
RESULTS_PER_CLUSTER = 6   # candidate images fetched per cluster (pre-curation)
PICKS_PER_CLUSTER = 3     # max images the LLM may keep per cluster


# ═════════════════════════════════════════════════════════════════════════════
# DB helpers
# ═════════════════════════════════════════════════════════════════════════════

def _init_vp_table() -> None:
    """Create visual_pulse_reports table if it doesn't exist."""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visual_pulse_reports (
            id          TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _save_report(username: str, report: dict) -> None:
    _init_vp_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO visual_pulse_reports (id, username, report_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (report["id"], username, json.dumps(report), report["generated_at"])
    )
    conn.commit()
    conn.close()


def _get_reports(username: str) -> list[dict]:
    _init_vp_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, report_json, created_at FROM visual_pulse_reports "
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
                "title":        r.get("title", "VisualPulse Report"),
                "doc_names":    r.get("doc_names", []),
                "generated_at": r.get("generated_at", row[2]),
                "image_count":  len(r.get("gallery", [])),
                "cluster_count": len(r.get("clusters", [])),
            })
        except Exception:
            pass
    return reports


def _get_report_by_id(username: str, report_id: str) -> dict | None:
    _init_vp_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT report_json FROM visual_pulse_reports WHERE id = ? AND username = ?",
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
    _init_vp_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM visual_pulse_reports WHERE id = ? AND username = ?",
        (report_id, username)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# ═════════════════════════════════════════════════════════════════════════════
# Chroma helpers  (same pattern as timeline_weave.py)
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
        print(f"[VP] Chroma error: {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Step 1 — Cluster extraction  (cheap: title + doc_context + ONE query)
# ═════════════════════════════════════════════════════════════════════════════

def _extract_clusters(chunks: list[dict]) -> list[dict]:
    """
    Ask the LLM to pull 3-5 topic CLUSTERS from the document chunks.
    Minimal schema per cluster to keep both the prompt and the
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

    prompt = f"""You are a visual researcher building a mood-board.

Read the document excerpts below and extract 3-5 KEY CLUSTERS (themes/topics)
central to the documents, each of which can be represented visually.

For each cluster provide ONLY:
- title        : 3-6 word cluster label
- doc_context  : 1 short sentence (max 15 words) on what the doc says about it
- search_query : ONE targeted web image-search query that would return clear,
                 concrete, on-topic photos or illustrations for this cluster
                 (favour concrete nouns/scenes over abstract phrasing)

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
            "search_query": c.get("search_query", c.get("title", "") + " photo"),
        })
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — Web image search  (Tavily only, ONE query per cluster)
# ═════════════════════════════════════════════════════════════════════════════

def _web_search_images(query: str, num_results: int = RESULTS_PER_CLUSTER) -> list[dict]:
    """
    Search the web for images using Tavily (include_images=true). Returns list of:
    { "url": str, "description": str, "source_title": str, "source_url": str, "source_type": str }
    """
    if not TAVILY_API_KEY:
        print(
            "[VP] _web_search_images: TAVILY_API_KEY is empty/unset — skipping search. "
            "Set the TAVILY_API_KEY environment variable (and restart the app "
            "process so it picks up the new env var) to enable image search."
        )
        return []

    try:
        import requests
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key":                    TAVILY_API_KEY,
                "query":                      query,
                "search_depth":               "advanced",
                "max_results":                num_results,
                "include_answer":             False,
                "include_raw_content":        False,
                "include_images":             True,
                "include_image_descriptions": True,
            },
            timeout=10,
        )

        if resp.status_code != 200:
            print(f"[VP] Tavily HTTP {resp.status_code} for query={query!r}: {resp.text[:500]}")
            return []

        data = resp.json()

        if "error" in data:
            print(f"[VP] Tavily API error for query={query!r}: {data['error']}")
            return []

        raw_images = data.get("images", [])
        if not raw_images:
            print(f"[VP] Tavily returned 0 images for query={query!r}.")
            return []

        results = []
        for item in raw_images:
            img_url = item.get("url", "")
            if not img_url:
                continue
            domain = _domain_of(img_url)
            results.append({
                "url":           img_url,
                "description":   (item.get("description") or "")[:220],
                "source_title":  domain,
                "source_url":    img_url,
                "source_type":   _classify_source(img_url),
            })

        print(f"[VP] Tavily returned {len(results)} image(s) for query={query!r}")
        return results[:num_results]

    except requests.exceptions.Timeout:
        print(f"[VP] Tavily timeout for query={query!r}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[VP] Tavily request error for query={query!r}: {e}")
        return []
    except Exception as e:
        print(f"[VP] Tavily unexpected error for query={query!r}: {e}")
        return []


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "") or url[:40]
    except Exception:
        return url[:40]


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
# Step 3 — Single batched curation  (ALL clusters, ONE LLM call)
# ═════════════════════════════════════════════════════════════════════════════

def _curate_gallery(clusters: list[dict], images_by_cluster: dict[str, list[dict]]) -> dict:
    """
    Feed every cluster's candidate images (as description + domain) into a
    SINGLE LLM call and get back the curated picks (all clusters) + one
    executive summary. This is the main token-saving move vs. a per-topic loop.

    Images are indexed per-cluster (idx 0,1,2…) so the model references
    "idx" instead of repeating full URLs in its output — python maps idx
    back to the real image dict afterwards.
    """
    has_any_images = any(images_by_cluster.get(c["id"]) for c in clusters)
    if not has_any_images:
        return {"summary": "No web images could be retrieved for any cluster.", "gallery": []}

    blocks = []
    for c in clusters:
        hits = images_by_cluster.get(c["id"], [])
        if not hits:
            blocks.append(f'CLUSTER {c["id"]} — "{c["title"]}" (doc says: {c["doc_context"]})\n  No candidate images.')
            continue
        lines = "\n".join(
            f'  [{i}] ({r["source_type"]}, {r["source_title"]}) {r["description"] or "(no description)"}'
            for i, r in enumerate(hits)
        )
        blocks.append(f'CLUSTER {c["id"]} — "{c["title"]}" (doc says: {c["doc_context"]})\n{lines}')

    web_block = "\n\n".join(blocks)

    prompt = f"""You are curating a visual mood-board that represents document topics with real photos.

{web_block}

Your task, across ALL clusters above, in one pass:
1. For each cluster, pick UP TO {PICKS_PER_CLUSTER} of its numbered [idx] candidates
   that are clearly on-topic, concrete, and likely to be a real usable photo/illustration
   (skip anything that sounds like a logo, icon, ad banner, or unrelated stock filler).
   For each picked image emit: {{ "cluster_id", "idx", "caption" (max 12 words,
   describing what the image shows in relation to the topic) }}.
   It's fine to pick 0 images for a cluster with no good candidates.
2. Write ONE overall "summary": 2-4 sentences on the visual story these topics
   tell together, referencing specific clusters.

Output ONLY valid JSON, no markdown, no preamble:
{{
  "summary": "…",
  "gallery": [
    {{ "cluster_id": "c0", "idx": 0, "caption": "…" }}
  ]
}}"""

    raw = call_llm_with_fallback(prompt, _SYNTH_CFG)
    return _parse_json_object(raw, fallback={"summary": "", "gallery": []})


# ═════════════════════════════════════════════════════════════════════════════
# Step 4 — Orchestrator
# ═════════════════════════════════════════════════════════════════════════════

def run_visual_pulse(
    username:   str,
    filenames:  list[str],
    session_id: str,
) -> dict:
    """
    Full pipeline. Returns a ReportJSON dict.
    Designed to be called from the Flask route.
    """
    report_id = f"vp_{uuid.uuid4().hex[:12]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Fetch doc chunks
    chunks = _fetch_chunks(username, filenames)
    if not chunks:
        return {
            "id": report_id, "title": "VisualPulse Report",
            "doc_names": filenames, "generated_at": now_str,
            "freshness_label": f"Generated {now_str}",
            "summary": "No document content found. Please upload and index files first.",
            "clusters": [], "gallery": [], "sources": [],
        }

    # 2. Extract clusters  (1 LLM call)
    clusters: list[dict] = _extract_clusters(chunks)
    if not clusters:
        clusters = [{
            "id": "c0", "title": "General Overview",
            "doc_context": "Content from uploaded documents.",
            "search_query": f"{filenames[0] if filenames else 'topic'} photo",
        }]

    # 3. Web image search — ONE query per cluster (no LLM involved)
    images_by_cluster: dict[str, list[dict]] = {}
    all_web_sources: list[dict] = []
    for c in clusters:
        hits = _web_search_images(c["search_query"], num_results=RESULTS_PER_CLUSTER)
        # Dedup by URL within cluster
        seen: set[str] = set()
        unique = []
        for h in hits:
            if h["url"] and h["url"] not in seen:
                seen.add(h["url"])
                unique.append(h)
        images_by_cluster[c["id"]] = unique
        all_web_sources.extend(unique)

    # 4. Single batched curation call  (1 LLM call, covers ALL clusters)
    synth = _curate_gallery(clusters, images_by_cluster)

    # 5. Map "idx" references back to real image dicts, build final gallery
    gallery: list[dict] = []
    for i, pick in enumerate(synth.get("gallery", [])):
        cid = pick.get("cluster_id", "")
        idx = pick.get("idx")
        cluster = next((c for c in clusters if c["id"] == cid), None)
        hits = images_by_cluster.get(cid, [])
        img = hits[idx] if isinstance(idx, int) and 0 <= idx < len(hits) else None
        if img is None:
            continue

        gallery.append({
            "id":            f"g{i}",
            "cluster_id":    cid,
            "cluster_title": cluster["title"] if cluster else cid,
            "image_url":     img["url"],
            "caption":       pick.get("caption", ""),
            "source_title":  img["source_title"],
            "source_url":    img["source_url"],
            "source_type":   img["source_type"],
        })

    # 6. Deduplicate sources list
    seen_src_urls: set[str] = set()
    sources: list[dict] = []
    for s in all_web_sources:
        if s["url"] and s["url"] not in seen_src_urls:
            seen_src_urls.add(s["url"])
            sources.append({"title": s["source_title"], "url": s["url"], "type": s["source_type"]})

    report: dict = {
        "id":                report_id,
        "title":             "VisualPulse Report",
        "doc_names":         filenames,
        "generated_at":      now_str,
        "freshness_label":   f"Live web data as of {datetime.now().strftime('%d %b %Y, %H:%M')}",
        "summary":           synth.get("summary", ""),
        "clusters":          clusters,
        "gallery":           gallery,
        "sources":           sources[:25],
    }

    # 7. Persist to DB
    try:
        _save_report(username, report)
    except Exception as e:
        print(f"[VP] DB save error: {e}")

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

    # Masonry gallery (CSS columns), cluster shown as a badge on each image
    gallery_html = ""
    for g in report.get("gallery", []):
        color = color_by_cluster.get(g.get("cluster_id", ""), "#6b7280")
        icon = _SOURCE_ICONS.get(g.get("source_type", "article"), "🔗")
        gallery_html += f"""
        <div style="break-inside:avoid;margin-bottom:14px;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb;position:relative">
            <span style="position:absolute;top:8px;left:8px;background:{color};color:#fff;font-size:10px;font-weight:700;padding:3px 9px;border-radius:20px;text-transform:uppercase;letter-spacing:.04em">{g.get('cluster_title','')}</span>
            <img src="{g.get('image_url','')}" style="width:100%;display:block" />
            <div style="padding:10px 12px">
                <div style="font-size:12.5px;color:#1f2937;font-weight:600;margin-bottom:4px">{g.get('caption','')}</div>
                <a href="{g.get('source_url','')}" style="font-size:10.5px;color:#6366f1;word-break:break-all">{icon} {g.get('source_title','')[:50]}</a>
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
  .vp-masonry {{ columns: 2; column-gap: 14px; }}
</style>
</head>
<body>
  <!-- Cover strip -->
  <div style="background:linear-gradient(135deg,#0d9488,#0891b2,#3b82f6);padding:32px 36px;margin-bottom:32px;border-radius:12px">
    <div style="font-size:11px;color:rgba(255,255,255,.7);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px">Nexora AI · VisualPulse</div>
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

  <!-- Gallery -->
  <div style="font-size:14px;font-weight:700;color:#1f2937;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #e5e7eb">
    🖼️ Visual Gallery
  </div>
  <div class="vp-masonry">{gallery_html or '<p style="color:#6b7280;font-size:13px">No images found.</p>'}</div>

  <!-- Sources -->
  <div style="padding:18px;background:#f9fafb;border-radius:12px;border:1px solid #e5e7eb;margin-top:20px;margin-bottom:20px">
    <div style="font-size:12px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px">🔗 Web Sources Consulted</div>
    <ul style="padding-left:16px;columns:2;gap:20px">{sources_html or '<li>No sources.</li>'}</ul>
  </div>

  <div style="text-align:center;font-size:11px;color:#9ca3af;padding-top:12px;border-top:1px solid #e5e7eb">
    Generated by Nexora AI VisualPulse · {gen_at} · For internal use only
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
# JSON parsing helpers  (same defensive strategy as timeline_weave.py)
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

    print(f"[VP] _parse_json_array raw output ({len(raw)} chars):\n{raw[:800]}{'...' if len(raw) > 800 else ''}")

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
            print(f"[VP] _parse_json_array: response was TRUNCATED — salvaged {len(repaired)} element(s).")
            return repaired

    try:
        json.loads(block or cleaned or raw)
    except Exception:
        print("[VP] JSON array parse error — all passes failed. Last exception:")
        traceback.print_exc()

    return fallback


def _parse_json_object(raw: str, fallback=None) -> dict:
    import traceback
    if fallback is None:
        fallback = {}

    print(f"[VP] _parse_json_object raw output ({len(raw)} chars):\n{raw[:800]}{'...' if len(raw) > 800 else ''}")

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
        print("[VP] JSON object parse error — all passes failed. Last exception:")
        traceback.print_exc()

    return fallback


# ═════════════════════════════════════════════════════════════════════════════
# Flask routes
# ═════════════════════════════════════════════════════════════════════════════

@visual_pulse_bp.route("/generate", methods=["POST"])
def generate():
    """
    POST /visual_pulse/generate
    Body: { "files": [...], "session_id": "..." }
    Returns: { "report": <ReportJSON>, "status": "ok" }
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = session.get("username")
    body = request.json or {}
    session_id = body.get("session_id", "vp-session")
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
            print(f"[VP] file list fallback error: {e}")

    try:
        report = run_visual_pulse(username, filenames, session_id)
        return jsonify({"status": "ok", "report": report})
    except Exception as e:
        print(f"[VP] generate error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@visual_pulse_bp.route("/export_pdf", methods=["POST"])
def export_pdf():
    """
    POST /visual_pulse/export_pdf
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
        filename = f"nexora_visual_pulse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        print(f"[VP] PDF export error: {e}")
        return jsonify({"status": "error", "message": "PDF generation failed"}), 500


@visual_pulse_bp.route("/history", methods=["GET"])
def history():
    """GET /visual_pulse/history — list saved reports for the user."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    return jsonify({"status": "ok", "reports": _get_reports(username)})


@visual_pulse_bp.route("/history/<report_id>", methods=["GET"])
def get_report(report_id: str):
    """GET /visual_pulse/history/<id> — fetch full report JSON."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    report = _get_report_by_id(username, report_id)
    if report is None:
        return jsonify({"status": "error", "message": "Report not found"}), 404
    return jsonify({"status": "ok", "report": report})


@visual_pulse_bp.route("/history/<report_id>", methods=["DELETE"])
def delete_report(report_id: str):
    """DELETE /visual_pulse/history/<id>"""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    ok = _delete_report(username, report_id)
    if ok:
        return jsonify({"status": "ok", "message": "Report deleted"})
    return jsonify({"status": "error", "message": "Report not found"}), 404
