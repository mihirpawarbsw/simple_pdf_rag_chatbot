"""
web_augmentor.py — Nexora AI  |  Web-Grounded Research Augmentor
================================================================
Combines your uploaded documents (via ChromaDB RAG) with live web
intelligence to produce a "Doc vs. World" executive report.

Pipeline:
  1. Extract key topics from user's docs  (via Chroma + LLM)
  2. For each topic → fire a web search   (Tavily)
  3. LLM synthesises Doc claim vs Web evidence per topic
  4. Build structured JSON report object  (single source of truth)
  5a. Return JSON for in-app viewer       (GET /web_augmentor/view)
  5b. Render PDF server-side via WeasyPrint (POST /web_augmentor/export_pdf)

Register in app.py:
    from web_augmentor import web_augmentor_bp
    app.register_blueprint(web_augmentor_bp)

Routes:
    POST /web_augmentor/generate
         Body: { "files": [...], "session_id": "..." }
         Returns: { "report": <ReportJSON>, "status": "ok" }

    POST /web_augmentor/export_pdf
         Body: { "report": <ReportJSON> }
         Returns: PDF file download

    GET  /web_augmentor/history
         Returns: [ list of saved reports for user ]

    DELETE /web_augmentor/history/<report_id>
         Deletes a saved report

ReportJSON schema:
    {
        "id":           "war_<uuid>",
        "title":        "Web-Grounded Intelligence Report",
        "doc_names":    ["file.pdf", ...],
        "generated_at": "2024-01-01 12:00:00",
        "freshness_label": "Live as of …",
        "executive_summary": "…",
        "overall_verdict":   "validated|mixed|outdated",
        "topics": [
            {
                "id":        "t0",
                "title":     "RAG Architecture",
                "doc_claim": "The document states …",
                "web_evidence": [
                    { "title": "…", "snippet": "…", "url": "…", "source_type": "article|news|tweet|review" }
                ],
                "synthesis":   "…",
                "verdict":     "confirmed|partially_outdated|contradicted|new_development",
                "trend_score": 82          ← 0-100 web trendiness
            },
            …
        ],
        "opportunities": ["…", …],
        "risks":         ["…", …],
        "sources":       [ { "title":"…", "url":"…", "type":"…" }, … ]
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

web_augmentor_bp = Blueprint("web_augmentor_bp", __name__, url_prefix="/web_augmentor")

# ── DB (reuse app's DB_NAME via env or default) ───────────────────────────────
DB_NAME = os.getenv("DB_NAME", "chat_history.db")

# ── Web-search provider config ────────────────────────────────────────────────
# Tavily only.
TAVILY_API_KEY  = os.getenv("TAVILY_API_KEY",  "")

# ── LLM config ────────────────────────────────────────────────────────────────
_LLM_CFG = {
    "model_name":  "llama-3.3-70b-versatile",
    "temperature": 0.2,
    "max_tokens":  1200,
}

_TOPIC_EXTRACT_CFG = {
    "model_name":  "llama-3.3-70b-versatile",
    "temperature": 0.0,
    # NOTE: 5-8 topics * (title + doc_claim + 3 search_queries) in JSON
    # routinely exceeds 600 tokens and gets cut off mid-array, which is
    # the root cause of "Expecting value" JSON errors. Raised to 1800.
    "max_tokens":  1800,
}


# ═════════════════════════════════════════════════════════════════════════════
# DB helpers
# ═════════════════════════════════════════════════════════════════════════════

def _init_war_table() -> None:
    """Create web_augmentor_reports table if it doesn't exist."""
    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS web_augmentor_reports (
            id          TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _save_report(username: str, report: dict) -> None:
    _init_war_table()
    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO web_augmentor_reports (id, username, report_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (report["id"], username, json.dumps(report), report["generated_at"])
    )
    conn.commit()
    conn.close()


def _get_reports(username: str) -> list[dict]:
    _init_war_table()
    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, report_json, created_at FROM web_augmentor_reports "
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
                "title":        r.get("title", "Web-Grounded Report"),
                "doc_names":    r.get("doc_names", []),
                "generated_at": r.get("generated_at", row[2]),
                "overall_verdict": r.get("overall_verdict", "mixed"),
                "topic_count":  len(r.get("topics", [])),
            })
        except Exception:
            pass
    return reports


def _get_report_by_id(username: str, report_id: str) -> dict | None:
    _init_war_table()
    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()
    cur.execute(
        "SELECT report_json FROM web_augmentor_reports WHERE id = ? AND username = ?",
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
    _init_war_table()
    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()
    cur.execute(
        "DELETE FROM web_augmentor_reports WHERE id = ? AND username = ?",
        (report_id, username)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# ═════════════════════════════════════════════════════════════════════════════
# Chroma helpers  (same pattern as cluster_universe.py)
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_chunks(username: str, filenames: list[str]) -> list[dict]:
    """Return list of {"text": str, "source": str} from ChromaDB."""
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col    = client.get_collection(CHROMA_COLLECTION)

        where: dict = (
            {"$and": [{"username": username}, {"source": {"$in": filenames}}]}
            if filenames else {"username": username}
        )

        results   = col.get(where=where, limit=150, include=["documents", "metadatas"])
        docs      = results.get("documents", []) or []
        metadatas = results.get("metadatas",  []) or []

        return [
            {"text": doc, "source": meta.get("source", "Unknown") if meta else "Unknown"}
            for doc, meta in zip(docs, metadatas)
            if doc and len(doc.strip()) > 40
        ]
    except Exception as e:
        print(f"[WAR] Chroma error: {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Step 1 — Topic extraction
# ═════════════════════════════════════════════════════════════════════════════

def _extract_topics(chunks: list[dict]) -> list[dict]:
    """
    Ask the LLM to pull 5-8 key topics from the document chunks,
    each with a short doc_claim summarising what the doc says about it.
    Returns list of {"title": str, "doc_claim": str, "search_queries": [str, …]}
    """
    # Group by source, grab representative snippets
    grouped: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        grouped[c["source"]].append(c["text"])

    source_blobs = []
    for src, texts in list(grouped.items())[:6]:
        blob = trim_to_budget(" ".join(texts[:6]), 600)
        source_blobs.append(f'[{src}]: {blob}')

    context = trim_to_budget("\n\n".join(source_blobs), 3800)

    prompt = f"""You are an expert research analyst.

Read the document excerpts below and extract 5-8 KEY TOPICS that are central to the documents.

For each topic provide:
- title        : 3-6 word topic label
- doc_claim    : 1-2 sentence summary of what the documents say about this topic
- search_queries: array of 3 highly targeted web search queries to find the LATEST news,
                  articles, tweets, reviews, and trends about this topic (be specific,
                  include year 2024 or 2025 in at least one query)

Output ONLY a valid JSON array. No markdown. No preamble.

Documents:
\"\"\"
{context}
\"\"\"

JSON:"""

    raw = call_llm_with_fallback(prompt, _TOPIC_EXTRACT_CFG)
    return _parse_json_array(raw, fallback=[])


# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — Web search  (Tavily only)
# ═════════════════════════════════════════════════════════════════════════════

def _web_search(query: str, num_results: int = 6) -> list[dict]:
    """
    Search the web for a query using Tavily. Returns list of:
    { "title": str, "snippet": str, "url": str, "source_type": str }
    """
    if not TAVILY_API_KEY:
        print(
            "[WAR] _web_search: TAVILY_API_KEY is empty/unset — skipping search. "
            "Set the TAVILY_API_KEY environment variable (and restart the app "
            "process so it picks up the new env var) to enable web search."
        )
        return []

    try:
        import requests
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key":        TAVILY_API_KEY,
                "query":          query,
                "search_depth":   "advanced",
                "max_results":    num_results,
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=10,
        )

        if resp.status_code != 200:
            print(
                f"[WAR] Tavily HTTP {resp.status_code} for query={query!r}: "
                f"{resp.text[:500]}"
            )
            return []

        data = resp.json()

        if "error" in data:
            print(f"[WAR] Tavily API error for query={query!r}: {data['error']}")
            return []

        raw_results = data.get("results", [])
        if not raw_results:
            print(f"[WAR] Tavily returned 0 results for query={query!r}. Raw response keys: {list(data.keys())}")
            return []

        results = []
        for item in raw_results:
            results.append({
                "title":       item.get("title", ""),
                "snippet":     item.get("content", "")[:280],
                "url":         item.get("url", ""),
                "source_type": _classify_source(item.get("url", "")),
            })

        print(f"[WAR] Tavily returned {len(results)} result(s) for query={query!r}")
        return results[:num_results]

    except requests.exceptions.Timeout:
        print(f"[WAR] Tavily timeout for query={query!r}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[WAR] Tavily request error for query={query!r}: {e}")
        return []
    except Exception as e:
        print(f"[WAR] Tavily unexpected error for query={query!r}: {e}")
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
# Step 3 — Per-topic synthesis
# ═════════════════════════════════════════════════════════════════════════════

def _synthesise_topic(topic: dict, web_results: list[dict]) -> dict:
    """
    Given a topic dict and its web results, ask the LLM to:
    - Write a synthesis paragraph
    - Issue a verdict
    - Score trendiness
    """
    if not web_results:
        return {
            **topic,
            "web_evidence": [],
            "synthesis":   "No web data could be retrieved for this topic.",
            "verdict":     "no_data",
            "trend_score": 0,
        }

    web_block = "\n".join(
        f"[{i+1}] ({r['source_type'].upper()}) {r['title']}\n    {r['snippet'][:220]}\n    URL: {r['url']}"
        for i, r in enumerate(web_results[:6])
    )

    prompt = f"""You are a senior intelligence analyst producing a "Doc vs. World" briefing.

TOPIC: {topic.get("title", "")}

WHAT THE DOCUMENT SAYS:
{topic.get("doc_claim", "")}

LATEST WEB INTELLIGENCE (articles, news, tweets, reviews, research):
{web_block}

Your task:
1. Write a concise synthesis (3-5 sentences) comparing what the document claims vs what the
   web evidence shows. Be specific. Mention source types (news/tweet/review etc.) where relevant.
2. Issue one of these verdicts:
   - "confirmed"           → web evidence strongly supports the document
   - "partially_outdated"  → document is mostly right but some parts are superseded
   - "contradicted"        → web evidence directly contradicts the document
   - "new_development"     → web reveals significant new info the document didn't capture
3. Give a trend_score 0-100 (how trendy/discussed is this topic on the web right now).
4. List 1-2 key opportunities and 1-2 key risks this web evidence reveals.

Output ONLY valid JSON. No markdown.

{{
  "synthesis":     "…",
  "verdict":       "confirmed|partially_outdated|contradicted|new_development",
  "trend_score":   82,
  "opportunities": ["…"],
  "risks":         ["…"]
}}"""

    raw = call_llm_with_fallback(prompt, _LLM_CFG)
    parsed = _parse_json_object(raw, fallback={})

    return {
        **topic,
        "web_evidence": web_results[:6],
        "synthesis":    parsed.get("synthesis", ""),
        "verdict":      parsed.get("verdict",   "partially_outdated"),
        "trend_score":  int(parsed.get("trend_score", 50)),
        "_opportunities": parsed.get("opportunities", []),
        "_risks":         parsed.get("risks",         []),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Step 4 — Overall executive summary + opportunity/risk synthesis
# ═════════════════════════════════════════════════════════════════════════════

def _build_executive_summary(topics: list[dict], doc_names: list[str]) -> dict:
    """Generate executive summary + overall verdict from all synthesised topics."""
    verdicts = [t.get("verdict", "") for t in topics]
    confirmed   = verdicts.count("confirmed")
    outdated    = verdicts.count("partially_outdated")
    contradicted = verdicts.count("contradicted")
    new_dev     = verdicts.count("new_development")

    if contradicted >= 2 or (contradicted + outdated) >= len(topics) // 2 + 1:
        overall_verdict = "outdated"
    elif confirmed >= len(topics) // 2 + 1:
        overall_verdict = "validated"
    else:
        overall_verdict = "mixed"

    # Collect opportunities and risks from all topics
    all_opps  = []
    all_risks = []
    for t in topics:
        all_opps.extend(t.pop("_opportunities", []))
        all_risks.extend(t.pop("_risks",         []))

    topic_summaries = "\n".join(
        f"- {t.get('title','')}: {t.get('verdict','')} (trend {t.get('trend_score',0)}/100) — {t.get('synthesis','')[:150]}"
        for t in topics
    )

    prompt = f"""You are a senior executive writing a brief for the C-suite.

Documents analysed: {', '.join(doc_names)}
Number of topics assessed: {len(topics)}
Verdict breakdown: {confirmed} confirmed, {outdated} partially outdated, {contradicted} contradicted, {new_dev} new developments found.

Topic summaries:
{topic_summaries}

Write a 4-6 sentence executive summary that:
- Opens with a verdict on the overall health/accuracy of these documents relative to today's landscape
- Highlights the 2-3 most significant findings
- Ends with a forward-looking sentence for decision-makers

Output ONLY the summary paragraph text. No JSON. No bullet points."""

    summary = call_llm_with_fallback(prompt, {**_LLM_CFG, "max_tokens": 400})

    # Deduplicate and cap lists
    seen_opps  = list(dict.fromkeys(all_opps))[:6]
    seen_risks = list(dict.fromkeys(all_risks))[:6]

    return {
        "executive_summary": summary.strip(),
        "overall_verdict":   overall_verdict,
        "opportunities":     seen_opps,
        "risks":             seen_risks,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Step 5 — Orchestrator
# ═════════════════════════════════════════════════════════════════════════════

def run_web_augmentor(
    username:   str,
    filenames:  list[str],
    session_id: str,
) -> dict:
    """
    Full pipeline. Returns a ReportJSON dict.
    Designed to be called from the Flask route.
    """
    report_id = f"war_{uuid.uuid4().hex[:12]}"
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Fetch doc chunks
    chunks = _fetch_chunks(username, filenames)
    if not chunks:
        return {
            "id": report_id, "title": "Web-Grounded Intelligence Report",
            "doc_names": filenames, "generated_at": now_str,
            "freshness_label": f"Generated {now_str}",
            "executive_summary": "No document content found. Please upload and index files first.",
            "overall_verdict": "mixed",
            "topics": [], "opportunities": [], "risks": [], "sources": [],
        }

    # 2. Extract topics
    raw_topics: list[dict] = _extract_topics(chunks)
    if not raw_topics:
        # Fallback: use cluster-style keyword extraction
        raw_topics = [{"title": "General Overview", "doc_claim": "Content from uploaded documents.", "search_queries": [f"{f} overview 2025" for f in filenames[:2]]}]

    all_web_sources: list[dict] = []
    synthesised_topics: list[dict] = []

    # 3. Web search + synthesis per topic
    for topic in raw_topics[:7]:   # cap at 7 topics to control latency
        queries: list[str] = topic.get("search_queries", [topic.get("title", "") + " 2025"])
        topic_results: list[dict] = []

        for q in queries[:3]:
            hits = _web_search(q, num_results=5)
            topic_results.extend(hits)

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_results: list[dict] = []
        for r in topic_results:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                unique_results.append(r)
        topic_results = unique_results[:8]

        all_web_sources.extend(topic_results)

        syn = _synthesise_topic(topic, topic_results)
        synthesised_topics.append(syn)

    # 4. Executive summary + overall verdict
    exec_data = _build_executive_summary(synthesised_topics, filenames)

    # 5. Deduplicate sources list
    seen_src_urls: set[str] = set()
    sources: list[dict] = []
    for s in all_web_sources:
        if s["url"] and s["url"] not in seen_src_urls:
            seen_src_urls.add(s["url"])
            sources.append({"title": s["title"], "url": s["url"], "type": s["source_type"]})

    report: dict = {
        "id":                report_id,
        "title":             "Web-Grounded Intelligence Report",
        "doc_names":         filenames,
        "generated_at":      now_str,
        "freshness_label":   f"Live web data as of {datetime.now().strftime('%d %b %Y, %H:%M')}",
        "executive_summary": exec_data["executive_summary"],
        "overall_verdict":   exec_data["overall_verdict"],
        "topics":            synthesised_topics,
        "opportunities":     exec_data["opportunities"],
        "risks":             exec_data["risks"],
        "sources":           sources[:25],
    }

    # 6. Persist to DB
    try:
        _save_report(username, report)
    except Exception as e:
        print(f"[WAR] DB save error: {e}")

    return report


# ═════════════════════════════════════════════════════════════════════════════
# PDF export via WeasyPrint
# ═════════════════════════════════════════════════════════════════════════════

_VERDICT_LABELS = {
    "confirmed":           ("✅ Confirmed",          "#22c55e"),
    "partially_outdated":  ("⚠️ Partially Outdated", "#f59e0b"),
    "contradicted":        ("❌ Contradicted",        "#ef4444"),
    "new_development":     ("🆕 New Development",    "#3b82f6"),
    "no_data":             ("— No Data",             "#6b7280"),
}

_OVERALL_LABELS = {
    "validated": ("Validated",        "#22c55e"),
    "mixed":     ("Mixed Results",    "#f59e0b"),
    "outdated":  ("Needs Review",     "#ef4444"),
}

_SOURCE_ICONS = {
    "news":     "📰",
    "tweet":    "🐦",
    "review":   "⭐",
    "blog":     "✍️",
    "forum":    "💬",
    "research": "🔬",
    "article":  "🔗",
}


def _render_report_html(report: dict) -> str:
    """Render report dict to a self-contained HTML string for WeasyPrint."""

    doc_names_str = ", ".join(report.get("doc_names", []))
    gen_at        = report.get("generated_at", "")
    freshness     = report.get("freshness_label", "")

    ov_label, ov_color = _OVERALL_LABELS.get(
        report.get("overall_verdict", "mixed"), ("Mixed Results", "#f59e0b")
    )

    exec_summary = report.get("executive_summary", "").replace("\n", "<br>")

    # Topics HTML
    topics_html = ""
    for i, t in enumerate(report.get("topics", [])):
        vl, vc = _VERDICT_LABELS.get(t.get("verdict", ""), ("—", "#6b7280"))
        ts      = t.get("trend_score", 0)

        # Web evidence cards
        evidence_html = ""
        for ev in t.get("web_evidence", [])[:4]:
            icon = _SOURCE_ICONS.get(ev.get("source_type", "article"), "🔗")
            evidence_html += f"""
            <div style="border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px;margin-bottom:8px;background:#fafafa">
                <div style="font-size:11px;color:#6b7280;margin-bottom:4px">{icon} {ev.get('source_type','').upper()}</div>
                <div style="font-size:13px;font-weight:600;color:#1f2937;margin-bottom:4px">{ev.get('title','')}</div>
                <div style="font-size:12px;color:#4b5563;margin-bottom:6px">{ev.get('snippet','')[:200]}</div>
                <a href="{ev.get('url','')}" style="font-size:11px;color:#6366f1;word-break:break-all">{ev.get('url','')[:80]}</a>
            </div>"""

        topics_html += f"""
        <div style="margin-bottom:28px;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
            <div style="padding:16px 20px;background:linear-gradient(90deg,#f5f3ff,#eff6ff);border-bottom:1px solid #e5e7eb;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
                <div>
                    <span style="font-size:11px;color:#6b7280;font-weight:600">TOPIC {i+1}</span>
                    <div style="font-size:16px;font-weight:700;color:#1f2937">{t.get('title','')}</div>
                </div>
                <div style="display:flex;align-items:center;gap:10px">
                    <span style="background:{vc}22;color:{vc};border:1px solid {vc}44;border-radius:20px;padding:4px 12px;font-size:12px;font-weight:600">{vl}</span>
                    <span style="background:#f3f4f6;border-radius:20px;padding:4px 12px;font-size:12px;color:#374151">🔥 Trend {ts}/100</span>
                </div>
            </div>
            <div style="padding:18px 20px;display:grid;grid-template-columns:1fr 1fr;gap:20px">
                <div>
                    <div style="font-size:11px;font-weight:700;color:#7c3aed;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">📄 Document Says</div>
                    <p style="font-size:13px;color:#374151;line-height:1.6;margin:0">{t.get('doc_claim','')}</p>
                    <div style="margin-top:16px;padding:12px;background:#f5f3ff;border-radius:8px;border-left:3px solid #7c3aed">
                        <div style="font-size:11px;font-weight:700;color:#7c3aed;margin-bottom:6px">🧠 SYNTHESIS</div>
                        <p style="font-size:12px;color:#374151;line-height:1.6;margin:0">{t.get('synthesis','')}</p>
                    </div>
                </div>
                <div>
                    <div style="font-size:11px;font-weight:700;color:#0369a1;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">🌐 Web Intelligence</div>
                    {evidence_html or '<p style="color:#6b7280;font-size:13px">No web results retrieved.</p>'}
                </div>
            </div>
        </div>"""

    # Opportunities & Risks
    opps_html  = "".join(f'<li style="margin-bottom:6px;color:#374151">{o}</li>' for o in report.get("opportunities", []))
    risks_html = "".join(f'<li style="margin-bottom:6px;color:#374151">{r}</li>' for r in report.get("risks", []))

    # Sources
    sources_html = ""
    for i, s in enumerate(report.get("sources", [])[:15]):
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
  <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed,#a855f7);padding:32px 36px;margin-bottom:32px;border-radius:12px">
    <div style="font-size:11px;color:rgba(255,255,255,.7);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px">Nexora AI · Web-Grounded Intelligence</div>
    <div style="font-size:26px;font-weight:800;color:#fff;margin-bottom:4px">{report.get('title','')}</div>
    <div style="font-size:13px;color:rgba(255,255,255,.8);margin-bottom:18px">Documents: {doc_names_str}</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <span style="background:rgba(255,255,255,.18);backdrop-filter:blur(8px);border-radius:8px;padding:6px 14px;font-size:12px;color:#fff">📅 {gen_at}</span>
      <span style="background:rgba(255,255,255,.18);backdrop-filter:blur(8px);border-radius:8px;padding:6px 14px;font-size:12px;color:#fff">🌐 {freshness}</span>
      <span style="background:{_OVERALL_LABELS.get(report.get('overall_verdict','mixed'),('','#f59e0b'))[1]};border-radius:8px;padding:6px 14px;font-size:12px;font-weight:700;color:#fff">Overall: {ov_label}</span>
    </div>
  </div>

  <!-- Executive Summary -->
  <div style="margin-bottom:28px;padding:22px 24px;background:#f5f3ff;border-radius:12px;border:1px solid #ddd6fe">
    <div style="font-size:12px;font-weight:700;color:#7c3aed;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">📋 Executive Summary</div>
    <p style="font-size:14px;line-height:1.7;color:#1f2937">{exec_summary}</p>
  </div>

  <!-- Topics -->
  <div style="font-size:14px;font-weight:700;color:#1f2937;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #e5e7eb">
    📌 Topic-by-Topic Intelligence Breakdown
  </div>
  {topics_html}

  <!-- Opportunities & Risks -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px">
    <div style="padding:18px;background:#f0fdf4;border-radius:12px;border:1px solid #bbf7d0">
      <div style="font-size:12px;font-weight:700;color:#16a34a;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px">🚀 Opportunities Revealed</div>
      <ul style="padding-left:16px">{opps_html or '<li style="color:#6b7280">None identified.</li>'}</ul>
    </div>
    <div style="padding:18px;background:#fef2f2;border-radius:12px;border:1px solid #fecaca">
      <div style="font-size:12px;font-weight:700;color:#dc2626;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px">⚠️ Risks Flagged</div>
      <ul style="padding-left:16px">{risks_html or '<li style="color:#6b7280">None identified.</li>'}</ul>
    </div>
  </div>

  <!-- Sources -->
  <div style="padding:18px;background:#f9fafb;border-radius:12px;border:1px solid #e5e7eb;margin-bottom:20px">
    <div style="font-size:12px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px">🔗 Web Sources Consulted</div>
    <ul style="padding-left:16px;columns:2;gap:20px">{sources_html or '<li>No sources.</li>'}</ul>
  </div>

  <div style="text-align:center;font-size:11px;color:#9ca3af;padding-top:12px;border-top:1px solid #e5e7eb">
    Generated by Nexora AI Web Augmentor · {gen_at} · For internal use only
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
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _clean_llm_json(raw: str) -> str:
    """Strip markdown fences and common LLM JSON artefacts."""
    # Remove all ``` fences (with optional language tag)
    raw = re.sub(r"```[a-z]*", "", raw, flags=re.IGNORECASE).replace("```", "")
    # Remove JS/Python-style single-line comments  (// ...)
    raw = re.sub(r"(?m)//[^\n]*$", "", raw)
    # Remove trailing commas before ] or }  (e.g. [..., ] or {..., })
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # Normalise smart/curly quotes to straight quotes
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    return raw.strip()


def _extract_json_block(text: str, opener: str, closer: str):
    """
    Find the OUTERMOST balanced JSON object or array in *text*.
    Returns the matched substring, or None if not found.
    More reliable than a greedy regex for nested structures.
    """
    start = text.find(opener)
    if start == -1:
        return None
    depth  = 0
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
    return None  # unbalanced


def _try_parse(candidate, expected_type):
    """Attempt json.loads; return parsed value only if it matches expected_type."""
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
    """
    Heuristic: does this look like the LLM response got cut off by max_tokens
    rather than being malformed for some other reason?
    True when the trimmed text doesn't end with a closing bracket/brace,
    or ends mid-string (odd number of unescaped quotes).
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if t[-1] not in "]}":
        return True
    # crude odd-quote check (ignoring escaped quotes)
    unescaped_quotes = len(re.findall(r'(?<!\\)"', t))
    return unescaped_quotes % 2 == 1


def _repair_truncated_array(text: str):
    """
    Best-effort recovery for a JSON array that was cut off mid-stream
    (typically because max_tokens was too low). Walks the top-level
    elements of the array one at a time and keeps every element that
    parses cleanly on its own, discarding only the dangling partial
    element at the very end. Returns a list (possibly empty) or None
    if no opening '[' was found at all.
    """
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

    # Reached end of string while still inside an element (depth >= 1) —
    # that dangling partial element is simply dropped, which is correct:
    # we keep every complete topic the model finished before cutting off.
    return elements if elements else None


def _parse_json_array(raw: str, fallback=None) -> list:
    import traceback
    if fallback is None:
        fallback = []

    print(f"[WAR] _parse_json_array raw output ({len(raw)} chars):\n{raw[:800]}{'...' if len(raw) > 800 else ''}")

    cleaned = _clean_llm_json(raw)

    # Pass 1: whole cleaned string
    result = _try_parse(cleaned, list)
    if result is not None:
        return result

    # Pass 2: outermost [...] block via balanced-bracket scan
    block = _extract_json_block(cleaned, "[", "]")
    result = _try_parse(block, list)
    if result is not None:
        return result

    # Pass 3: greedy regex fallback (catches simple non-nested cases)
    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if m:
        result = _try_parse(m.group(), list)
        if result is not None:
            return result

    # Pass 4: response looks truncated (hit max_tokens mid-array) — salvage
    # every complete element instead of throwing the whole list away.
    if _looks_truncated(cleaned):
        repaired = _repair_truncated_array(cleaned)
        if repaired is not None:
            print(
                f"[WAR] _parse_json_array: response was TRUNCATED "
                f"(likely max_tokens cutoff) — salvaged {len(repaired)} complete "
                f"element(s) out of a partial array. Consider raising max_tokens."
            )
            return repaired

    # All passes failed — log with full traceback
    truncated_flag = _looks_truncated(cleaned)
    try:
        json.loads(block or cleaned or raw)
    except Exception:
        print(
            f"[WAR] JSON array parse error — all passes failed "
            f"(looks_truncated={truncated_flag}). Last exception:"
        )
        traceback.print_exc()

    return fallback


def _parse_json_object(raw: str, fallback=None) -> dict:
    import traceback
    if fallback is None:
        fallback = {}

    print(f"[WAR] _parse_json_object raw output ({len(raw)} chars):\n{raw[:800]}{'...' if len(raw) > 800 else ''}")

    cleaned = _clean_llm_json(raw)

    # Pass 1: whole cleaned string
    result = _try_parse(cleaned, dict)
    if result is not None:
        return result

    # Pass 2: outermost {...} block via balanced-bracket scan
    block = _extract_json_block(cleaned, "{", "}")
    result = _try_parse(block, dict)
    if result is not None:
        return result

    # Pass 3: greedy regex fallback
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        result = _try_parse(m.group(), dict)
        if result is not None:
            return result

    # All passes failed — log with full traceback
    try:
        json.loads(block or cleaned or raw)
    except Exception:
        print("[WAR] JSON object parse error — all passes failed. Last exception:")
        traceback.print_exc()

    return fallback



# ═════════════════════════════════════════════════════════════════════════════
# Flask routes
# ═════════════════════════════════════════════════════════════════════════════

@web_augmentor_bp.route("/generate", methods=["POST"])
def generate():
    """
    POST /web_augmentor/generate
    Body: { "files": [...], "session_id": "..." }
    Returns: { "report": <ReportJSON>, "status": "ok" }
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username   = session.get("username")
    body       = request.json or {}
    session_id = body.get("session_id", "war-session")
    filenames  = body.get("files") or []

    if isinstance(filenames, str):
        filenames = [f.strip() for f in filenames.split(",") if f.strip()]

    # Default to all user files if none specified
    if not filenames:
        try:
            conn = sqlite3.connect(DB_NAME)
            cur  = conn.cursor()
            cur.execute(
                "SELECT DISTINCT filename FROM uploaded_files WHERE username = ? ORDER BY filename",
                (username,)
            )
            filenames = [r[0] for r in cur.fetchall()]
            conn.close()
        except Exception as e:
            print(f"[WAR] file list fallback error: {e}")

    try:
        report = run_web_augmentor(username, filenames, session_id)
        return jsonify({"status": "ok", "report": report})
    except Exception as e:
        print(f"[WAR] generate error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@web_augmentor_bp.route("/export_pdf", methods=["POST"])
def export_pdf():
    """
    POST /web_augmentor/export_pdf
    Body: { "report": <ReportJSON> }
    Returns: PDF file
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    body   = request.json or {}
    report = body.get("report")
    if not report:
        return jsonify({"status": "error", "message": "No report data provided"}), 400

    try:
        pdf_bytes = _export_pdf(report)
        filename  = f"nexora_web_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        print(f"[WAR] PDF export error: {e}")
        return jsonify({"status": "error", "message": "PDF generation failed"}), 500


@web_augmentor_bp.route("/history", methods=["GET"])
def history():
    """GET /web_augmentor/history — list saved reports for the user."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    return jsonify({"status": "ok", "reports": _get_reports(username)})


@web_augmentor_bp.route("/history/<report_id>", methods=["GET"])
def get_report(report_id: str):
    """GET /web_augmentor/history/<id> — fetch full report JSON."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    report   = _get_report_by_id(username, report_id)
    if report is None:
        return jsonify({"status": "error", "message": "Report not found"}), 404
    return jsonify({"status": "ok", "report": report})


@web_augmentor_bp.route("/history/<report_id>", methods=["DELETE"])
def delete_report(report_id: str):
    """DELETE /web_augmentor/history/<id>"""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    ok       = _delete_report(username, report_id)
    if ok:
        return jsonify({"status": "ok", "message": "Report deleted"})
    return jsonify({"status": "error", "message": "Report not found"}), 404
