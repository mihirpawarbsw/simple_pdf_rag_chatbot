"""
pulse_grid.py — Nexora AI  |  PulseGrid
================================================================
Turns your document's key topics into two live, data-driven views —
no synthesis LLM call needed for either:

  🎯 StatCards        — animated gauge / big-number cards with a
                         cross-source sparkline, built by regex-scanning
                         real Tavily search snippets for numbers.
  🗺️ CredibilityGrid  — topic × source-tier heatmap. Rows = doc topics,
                         columns = source tiers (news/research/blog/
                         forum/social). Cell = raw result count.
                         Instantly shows which topics are backed by
                         credible sources vs. only forum chatter.

Token-efficiency design (same family as visual_pulse.py / timeline_weave.py):
  1. Extract 3-5 TOPICS from doc chunks        (1 LLM call, tiny schema:
     title + doc_context + a single "current stats" search query)
  2. Tavily search — ONE query per topic        (no LLM involved — plain
     web search, NOT image search, "include_images": false)
  3. StatCards       — regex-extract numbers/%/currency out of the
                        snippet text returned by Tavily. Pure regex,
                        zero LLM calls.
  4. CredibilityGrid — classify each result's domain into a source
                        tier and tally counts per topic. Pure counting,
                        zero LLM calls.
  5. Build structured JSON report object        (single source of truth)
  6a. Return JSON for in-app viewer              (GET /pulse_grid/view)
  6b. Render PDF server-side via WeasyPrint      (POST /pulse_grid/export_pdf)

Total LLM calls per report: 1 (fixed), regardless of topic count.
Everything else is Tavily + regex + counting.

Register in app.py:
    from pulse_grid import pulse_grid_bp
    app.register_blueprint(pulse_grid_bp)

Routes:
    POST /pulse_grid/generate
         Body: { "files": [...], "session_id": "..." }
         Returns: { "report": <ReportJSON>, "status": "ok" }

    POST /pulse_grid/export_pdf
         Body: { "report": <ReportJSON> }
         Returns: PDF file download

    GET  /pulse_grid/history
         Returns: [ list of saved reports for user ]

    GET  /pulse_grid/history/<report_id>
         Returns: full ReportJSON

    DELETE /pulse_grid/history/<report_id>
         Deletes a saved report

ReportJSON schema:
    {
        "id":              "pg_<uuid>",
        "title":           "PulseGrid Report",
        "doc_names":       ["file.pdf", ...],
        "generated_at":    "2026-07-03 12:00:00",
        "freshness_label": "Live web data as of …",
        "topics": [
            { "id": "t0", "title": "RAG Adoption", "doc_context": "…",
              "stat_query": "RAG adoption statistics 2026" },
            …
        ],
        "stat_cards": [
            {
                "id": "sc0", "topic_id": "t0", "topic_title": "RAG Adoption",
                "display_value": "62%", "raw_number": 62.0, "unit": "%",
                "trend": "up" | "down" | null,
                "context": "…snippet the number was pulled from…",
                "sparkline": [0, 22, 41, 63, 100],
                "source_title": "…", "source_url": "…", "source_type": "news"
            }, …
        ],
        "credibility_grid": {
            "tiers": ["news", "research", "blog", "forum", "social", "other"],
            "rows": [
                { "topic_id": "t0", "topic_title": "RAG Adoption",
                  "counts": {"news": 3, "research": 2, "blog": 1,
                             "forum": 0, "social": 1, "other": 0},
                  "total": 7 }, …
            ]
        },
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

pulse_grid_bp = Blueprint("pulse_grid_bp", __name__, url_prefix="/pulse_grid")

# ── DB (reuse app's DB_NAME via env or default) ───────────────────────────────
DB_NAME = os.getenv("DB_NAME", "chat_history.db")

# ── Web-search provider config ────────────────────────────────────────────────
# Tavily only. Plain web search (NOT include_images) — this is the whole point:
# real snippets in, regex/counting out, no second LLM call.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ── LLM config ────────────────────────────────────────────────────────────────
# Topic extraction is the ONLY LLM call in the whole pipeline. Small schema
# (title + doc_context + ONE query) keeps max_tokens low even at 5 topics.
_TOPIC_CFG = {
    "model_name":  "llama-3.3-70b-versatile",
    "temperature": 0.0,
    "max_tokens":  700,
}

MAX_TOPICS            = 5
RESULTS_PER_TOPIC      = 10   # Tavily results fetched per topic (regular search, not images)
MAX_CARDS_PER_TOPIC    = 2    # stat cards kept per topic after regex extraction
MAX_SPARKLINE_POINTS   = 8

CURRENT_YEAR = datetime.now().year


# ═════════════════════════════════════════════════════════════════════════════
# DB helpers
# ═════════════════════════════════════════════════════════════════════════════

def _init_pg_table() -> None:
    """Create pulse_grid_reports table if it doesn't exist."""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pulse_grid_reports (
            id          TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _save_report(username: str, report: dict) -> None:
    _init_pg_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO pulse_grid_reports (id, username, report_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (report["id"], username, json.dumps(report), report["generated_at"])
    )
    conn.commit()
    conn.close()


def _get_reports(username: str) -> list[dict]:
    _init_pg_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, report_json, created_at FROM pulse_grid_reports "
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
                "id":            r.get("id", row[0]),
                "title":         r.get("title", "PulseGrid Report"),
                "doc_names":     r.get("doc_names", []),
                "generated_at":  r.get("generated_at", row[2]),
                "card_count":    len(r.get("stat_cards", [])),
                "topic_count":   len(r.get("topics", [])),
            })
        except Exception:
            pass
    return reports


def _get_report_by_id(username: str, report_id: str) -> dict | None:
    _init_pg_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT report_json FROM pulse_grid_reports WHERE id = ? AND username = ?",
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
    _init_pg_table()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM pulse_grid_reports WHERE id = ? AND username = ?",
        (report_id, username)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# ═════════════════════════════════════════════════════════════════════════════
# Chroma helpers  (same pattern as visual_pulse.py / timeline_weave.py)
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
        print(f"[PG] Chroma error: {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Step 1 — Topic extraction  (the ONLY LLM call in the pipeline)
# ═════════════════════════════════════════════════════════════════════════════

def _extract_topics(chunks: list[dict]) -> list[dict]:
    """
    Ask the LLM to pull 3-5 KEY TOPICS from the document chunks.
    Minimal schema per topic to keep both the prompt and the response small.
    Returns list of {"title": str, "doc_context": str, "stat_query": str}
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        grouped[c["source"]].append(c["text"])

    source_blobs = []
    for src, texts in list(grouped.items())[:6]:
        blob = trim_to_budget(" ".join(texts[:5]), 500)
        source_blobs.append(f'[{src}]: {blob}')

    context = trim_to_budget("\n\n".join(source_blobs), 3000)

    prompt = f"""You are a research analyst preparing a live stats dashboard.

Read the document excerpts below and extract 3-5 KEY TOPICS central to the
documents, each of which has measurable real-world statistics worth pulling
fresh from the web.

For each topic provide ONLY:
- title        : 3-6 word topic label
- doc_context  : 1 short sentence (max 15 words) on what the doc says about it
- stat_query   : ONE targeted web-search query that would surface CURRENT
                 statistics/figures/percentages about this topic
                 (e.g. "{{topic}} adoption statistics {CURRENT_YEAR}",
                 favour concrete, numeric, current-year phrasing)

Output ONLY a valid JSON array. No markdown. No preamble. Be terse.

Documents:
\"\"\"
{context}
\"\"\"

JSON:"""

    raw = call_llm_with_fallback(prompt, _TOPIC_CFG)
    topics = _parse_json_array(raw, fallback=[])

    out = []
    for i, t in enumerate(topics[:MAX_TOPICS]):
        title = t.get("title", f"Topic {i+1}")
        out.append({
            "id":          f"t{i}",
            "title":       title,
            "doc_context": t.get("doc_context", ""),
            "stat_query":  t.get("stat_query", f"{title} statistics {CURRENT_YEAR}"),
        })
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — Web search  (Tavily only, plain search — NOT images, ONE query/topic)
# ═════════════════════════════════════════════════════════════════════════════

def _web_search(query: str, num_results: int = RESULTS_PER_TOPIC) -> list[dict]:
    """
    Search the web using Tavily (plain, include_images=false). Returns list of:
    { "title": str, "url": str, "content": str, "score": float }
    """
    if not TAVILY_API_KEY:
        print(
            "[PG] _web_search: TAVILY_API_KEY is empty/unset — skipping search. "
            "Set the TAVILY_API_KEY environment variable (and restart the app "
            "process so it picks up the new env var) to enable PulseGrid."
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
                "include_images":      False,
            },
            timeout=10,
        )

        if resp.status_code != 200:
            print(f"[PG] Tavily HTTP {resp.status_code} for query={query!r}: {resp.text[:500]}")
            return []

        data = resp.json()

        if "error" in data:
            print(f"[PG] Tavily API error for query={query!r}: {data['error']}")
            return []

        raw_results = data.get("results", [])
        if not raw_results:
            print(f"[PG] Tavily returned 0 results for query={query!r}.")
            return []

        out = []
        for item in raw_results:
            url = item.get("url", "")
            if not url:
                continue
            out.append({
                "title":   item.get("title", "") or _domain_of(url),
                "url":     url,
                "content": item.get("content", "") or "",
                "score":   item.get("score", 0.0),
            })

        print(f"[PG] Tavily returned {len(out)} result(s) for query={query!r}")
        return out[:num_results]

    except requests.exceptions.Timeout:
        print(f"[PG] Tavily timeout for query={query!r}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[PG] Tavily request error for query={query!r}: {e}")
        return []
    except Exception as e:
        print(f"[PG] Tavily unexpected error for query={query!r}: {e}")
        return []


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "") or url[:40]
    except Exception:
        return url[:40]


# ── Source-tier classification (feeds BOTH StatCards source_type + CredibilityGrid columns)
_TIER_NEWS = [
    "bbc.com", "cnn.com", "reuters.com", "techcrunch.com", "theverge.com",
    "wired.com", "zdnet.com", "venturebeat.com", "arstechnica.com",
    "bloomberg.com", "ft.com", "wsj.com", "economist.com", "forbes.com",
    "businessinsider.com", "cnbc.com", "nytimes.com", "theguardian.com",
    "apnews.com", "npr.org", "axios.com", "engadget.com",
]
_TIER_RESEARCH = [
    "arxiv.org", "semanticscholar.org", "researchgate.net", "springer.com",
    "acm.org", "ieee.org", "nature.com", "sciencedirect.com", "ncbi.nlm.nih.gov",
    "jstor.org", "pnas.org", "mdpi.com",
]
_TIER_BLOG = [
    "medium.com", "substack.com", "dev.to", "hashnode.dev", "blogspot.com",
    "wordpress.com", "ghost.io",
]
_TIER_FORUM = [
    "reddit.com", "news.ycombinator.com", "quora.com", "stackexchange.com",
    "stackoverflow.com", "discourse.org",
]
_TIER_SOCIAL = [
    "twitter.com", "x.com", "facebook.com", "instagram.com", "tiktok.com",
    "linkedin.com", "threads.net",
]

CREDIBILITY_TIERS = ["news", "research", "blog", "forum", "social", "other"]


def _classify_tier(url: str) -> str:
    url_l = url.lower()
    if any(d in url_l for d in _TIER_RESEARCH):
        return "research"
    if any(d in url_l for d in _TIER_NEWS):
        return "news"
    if any(d in url_l for d in _TIER_SOCIAL):
        return "social"
    if any(d in url_l for d in _TIER_FORUM):
        return "forum"
    if any(d in url_l for d in _TIER_BLOG):
        return "blog"
    return "other"


# ═════════════════════════════════════════════════════════════════════════════
# Step 3 — StatCards  (pure regex, zero LLM calls)
# ═════════════════════════════════════════════════════════════════════════════

_UNIT_MULTIPLIER = {
    "trillion": 1e12, "billion": 1e9, "bn": 1e9,
    "million": 1e6, "mn": 1e6, "thousand": 1e3, "k": 1e3,
}

_UP_WORDS = ["increase", "grew", "grow", "rose", "rising", "surge", "jump",
             "soar", "growth", "accelerat", "climb", "boost", "expand"]
_DOWN_WORDS = ["decrease", "fell", "fall", "dropped", "drop", "decline",
               "shrink", "plunge", "falling", "slump", "reduc", "contract"]

_NUMBER_RE = re.compile(
    r'(?P<currency>[$€£])?\s?'
    r'(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)'
    r'\s?(?P<unit>%|percent|percentage points?|trillion|billion|bn|million|mn|thousand|k)?',
    re.IGNORECASE,
)


def _looks_like_year(numstr: str) -> bool:
    return bool(re.fullmatch(r'(19|20)\d{2}', numstr))


def _extract_stats_from_text(text: str) -> list[dict]:
    """
    Regex-scan a snippet for numeric statistics. Returns list of:
    { "display_value", "raw_number", "unit", "trend", "context", "signal" }
    "signal" is a rough priority score used only to pick the best card(s).
    """
    out = []
    for m in _NUMBER_RE.finditer(text):
        numstr = m.group("num")
        unit_raw = (m.group("unit") or "").lower()
        currency = m.group("currency")

        is_percent = unit_raw in ("%", "percent") or "percentage point" in unit_raw
        has_multiplier = unit_raw in _UNIT_MULTIPLIER

        # Skip low-signal bare numbers (likely years, page numbers, list indices)
        if not currency and not is_percent and not has_multiplier:
            if _looks_like_year(numstr):
                continue
            if "," not in numstr and "." not in numstr and len(numstr) <= 2:
                continue

        try:
            raw = float(numstr.replace(",", ""))
        except ValueError:
            continue

        multiplier = _UNIT_MULTIPLIER.get(unit_raw, 1)
        normalized = raw * multiplier

        if is_percent:
            display = f"{numstr}%"
            unit_label = "%"
            signal = 3
        elif currency:
            unit_word = f" {unit_raw}" if has_multiplier else ""
            display = f"{currency}{numstr}{unit_word}"
            unit_label = unit_raw or "currency"
            signal = 3
        elif has_multiplier:
            display = f"{numstr} {unit_raw}"
            unit_label = unit_raw
            signal = 2
        else:
            display = numstr
            unit_label = ""
            signal = 1

        start = max(0, m.start() - 70)
        end = min(len(text), m.end() + 70)
        window = text[start:end]
        window_l = window.lower()

        trend = None
        if any(w in window_l for w in _UP_WORDS):
            trend = "up"
        elif any(w in window_l for w in _DOWN_WORDS):
            trend = "down"

        out.append({
            "display_value": display,
            "raw_number":    raw,
            "normalized":    normalized,
            "unit":          unit_label,
            "trend":         trend,
            "context":       window.strip(),
            "signal":        signal,
        })
    return out


def _sparkline_from_points(values: list[float]) -> list[int]:
    """Min-max normalize a list of raw numbers to a 0-100 int scale for a sparkline."""
    if not values:
        return []
    if len(values) == 1:
        return [50]
    lo, hi = min(values), max(values)
    if hi == lo:
        return [50] * len(values)
    return [round((v - lo) / (hi - lo) * 100) for v in values]


def _build_stat_cards(topics: list[dict], results_by_topic: dict[str, list[dict]]) -> list[dict]:
    """
    For each topic, regex-scan its Tavily result snippets, keep the strongest
    MAX_CARDS_PER_TOPIC distinct stats as cards, and use every distinct number
    found for that topic (across all its sources) as a cross-source sparkline.
    Zero LLM calls — pure regex + sorting.
    """
    cards: list[dict] = []
    card_idx = 0

    for topic in topics:
        hits = results_by_topic.get(topic["id"], [])
        all_candidates = []
        for r in hits:
            for stat in _extract_stats_from_text(r["content"]):
                stat["_source"] = r
                all_candidates.append(stat)

        if not all_candidates:
            continue

        # Dedup near-identical numbers (same raw_number, same unit) — keep first (top-ranked source)
        seen_keys: set[tuple] = set()
        deduped = []
        for c in all_candidates:
            key = (round(c["raw_number"], 2), c["unit"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(c)

        # Sparkline uses every distinct normalized value found for this topic (cross-source)
        spark_values = [c["normalized"] for c in deduped[:MAX_SPARKLINE_POINTS]]
        sparkline = _sparkline_from_points(spark_values)

        # Rank by signal (percent/currency > multiplier-word > bare number), then source score
        ranked = sorted(
            deduped,
            key=lambda c: (c["signal"], c["_source"].get("score", 0.0)),
            reverse=True,
        )

        for c in ranked[:MAX_CARDS_PER_TOPIC]:
            src = c["_source"]
            url = src["url"]
            cards.append({
                "id":            f"sc{card_idx}",
                "topic_id":      topic["id"],
                "topic_title":   topic["title"],
                "display_value": c["display_value"],
                "raw_number":    c["raw_number"],
                "unit":          c["unit"],
                "trend":         c["trend"],
                "context":       c["context"],
                "sparkline":     sparkline,
                "source_title":  src["title"] or _domain_of(url),
                "source_url":    url,
                "source_type":   _classify_tier(url),
            })
            card_idx += 1

    return cards


# ═════════════════════════════════════════════════════════════════════════════
# Step 4 — CredibilityGrid  (pure counting, zero LLM calls)
# ═════════════════════════════════════════════════════════════════════════════

def _build_credibility_grid(topics: list[dict], results_by_topic: dict[str, list[dict]]) -> dict:
    rows = []
    for topic in topics:
        hits = results_by_topic.get(topic["id"], [])
        counts = {tier: 0 for tier in CREDIBILITY_TIERS}
        for r in hits:
            tier = _classify_tier(r["url"])
            counts[tier] = counts.get(tier, 0) + 1
        rows.append({
            "topic_id":    topic["id"],
            "topic_title": topic["title"],
            "counts":      counts,
            "total":       sum(counts.values()),
        })
    return {"tiers": CREDIBILITY_TIERS, "rows": rows}


# ═════════════════════════════════════════════════════════════════════════════
# Step 5 — Orchestrator
# ═════════════════════════════════════════════════════════════════════════════

def run_pulse_grid(
    username:   str,
    filenames:  list[str],
    session_id: str,
) -> dict:
    """
    Full pipeline. Returns a ReportJSON dict.
    Designed to be called from the Flask route.
    """
    report_id = f"pg_{uuid.uuid4().hex[:12]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Fetch doc chunks
    chunks = _fetch_chunks(username, filenames)
    if not chunks:
        return {
            "id": report_id, "title": "PulseGrid Report",
            "doc_names": filenames, "generated_at": now_str,
            "freshness_label": f"Generated {now_str}",
            "topics": [], "stat_cards": [],
            "credibility_grid": {"tiers": CREDIBILITY_TIERS, "rows": []},
            "sources": [],
        }

    # 2. Extract topics  (1 LLM call — the ONLY one)
    topics: list[dict] = _extract_topics(chunks)
    if not topics:
        topics = [{
            "id": "t0", "title": "General Overview",
            "doc_context": "Content from uploaded documents.",
            "stat_query": f"{filenames[0] if filenames else 'topic'} statistics {CURRENT_YEAR}",
        }]

    # 3. Web search — ONE query per topic (no LLM involved)
    results_by_topic: dict[str, list[dict]] = {}
    all_sources: list[dict] = []
    for t in topics:
        hits = _web_search(t["stat_query"], num_results=RESULTS_PER_TOPIC)
        seen: set[str] = set()
        unique = []
        for h in hits:
            if h["url"] and h["url"] not in seen:
                seen.add(h["url"])
                unique.append(h)
        results_by_topic[t["id"]] = unique
        all_sources.extend(unique)

    # 4. StatCards — regex extraction (no LLM)
    stat_cards = _build_stat_cards(topics, results_by_topic)

    # 5. CredibilityGrid — counting (no LLM)
    credibility_grid = _build_credibility_grid(topics, results_by_topic)

    # 6. Deduplicate sources list
    seen_src_urls: set[str] = set()
    sources: list[dict] = []
    for s in all_sources:
        if s["url"] and s["url"] not in seen_src_urls:
            seen_src_urls.add(s["url"])
            sources.append({
                "title": s["title"] or _domain_of(s["url"]),
                "url":   s["url"],
                "type":  _classify_tier(s["url"]),
            })

    report: dict = {
        "id":                report_id,
        "title":             "PulseGrid Report",
        "doc_names":         filenames,
        "generated_at":      now_str,
        "freshness_label":   f"Live web data as of {datetime.now().strftime('%d %b %Y, %H:%M')}",
        "topics":            topics,
        "stat_cards":        stat_cards,
        "credibility_grid":  credibility_grid,
        "sources":           sources[:25],
    }

    # 7. Persist to DB
    try:
        _save_report(username, report)
    except Exception as e:
        print(f"[PG] DB save error: {e}")

    return report


# ═════════════════════════════════════════════════════════════════════════════
# PDF export via WeasyPrint
# ═════════════════════════════════════════════════════════════════════════════

_TIER_ICONS = {
    "news":     "📰",
    "research": "🔬",
    "blog":     "✍️",
    "forum":    "💬",
    "social":   "🐦",
    "other":    "🔗",
}

_TOPIC_COLORS = ["#0d9488", "#f59e0b", "#7c3aed", "#3b82f6", "#ec4899"]


def _sparkline_svg(points: list[int], color: str, width: int = 90, height: int = 26) -> str:
    if not points or len(points) < 2:
        return ""
    step = width / (len(points) - 1)
    coords = " ".join(f"{i*step:.1f},{height - (p/100*height):.1f}" for i, p in enumerate(points))
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _heat_color(count: int, max_count: int) -> str:
    if count == 0 or max_count == 0:
        return "#f3f4f6"
    ratio = min(1.0, count / max_count)
    # interpolate between light teal and deep teal
    r = round(209 + (13 - 209) * ratio)
    g = round(250 + (148 - 250) * ratio)
    b = round(229 + (136 - 229) * ratio)
    return f"rgb({r},{g},{b})"


def _render_report_html(report: dict) -> str:
    """Render report dict to a self-contained HTML string for WeasyPrint."""

    doc_names_str = ", ".join(report.get("doc_names", []))
    gen_at = report.get("generated_at", "")
    freshness = report.get("freshness_label", "")

    topics = report.get("topics", [])
    color_by_topic = {t["id"]: _TOPIC_COLORS[i % len(_TOPIC_COLORS)] for i, t in enumerate(topics)}

    # ── Stat cards grid ──
    cards_html = ""
    for c in report.get("stat_cards", []):
        color = color_by_topic.get(c.get("topic_id", ""), "#6b7280")
        arrow = "▲" if c.get("trend") == "up" else ("▼" if c.get("trend") == "down" else "")
        arrow_color = "#16a34a" if c.get("trend") == "up" else ("#dc2626" if c.get("trend") == "down" else "#9ca3af")
        icon = _TIER_ICONS.get(c.get("source_type", "other"), "🔗")
        spark = _sparkline_svg(c.get("sparkline", []), color)
        cards_html += f"""
        <div style="break-inside:avoid;margin-bottom:14px;padding:16px 18px;border:1px solid #e5e7eb;border-radius:14px;border-left:4px solid {color};background:#fff">
            <div style="font-size:10.5px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">{c.get('topic_title','')}</div>
            <div style="display:flex;align-items:baseline;gap:8px">
                <span style="font-size:26px;font-weight:800;color:#111827">{c.get('display_value','')}</span>
                <span style="font-size:13px;font-weight:700;color:{arrow_color}">{arrow}</span>
            </div>
            <div style="margin:8px 0">{spark}</div>
            <div style="font-size:11px;color:#6b7280;line-height:1.4;margin-bottom:6px">{(c.get('context','') or '')[:110]}</div>
            <a href="{c.get('source_url','')}" style="font-size:10px;color:#6366f1;word-break:break-all">{icon} {(c.get('source_title','') or '')[:45]}</a>
        </div>"""

    # ── Credibility heatmap table ──
    grid = report.get("credibility_grid", {"tiers": CREDIBILITY_TIERS, "rows": []})
    tiers = grid.get("tiers", CREDIBILITY_TIERS)
    rows = grid.get("rows", [])
    max_count = max([r["counts"].get(tier, 0) for r in rows for tier in tiers], default=0)

    header_cells = "".join(
        f'<th style="padding:8px 10px;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#374151;text-align:center">{_TIER_ICONS.get(t,"")} {t}</th>'
        for t in tiers
    )
    body_rows = ""
    for r in rows:
        color = color_by_topic.get(r.get("topic_id", ""), "#6b7280")
        cells = "".join(
            f'<td style="padding:10px;text-align:center;background:{_heat_color(r["counts"].get(t,0), max_count)};'
            f'font-size:13px;font-weight:700;color:#1f2937;border:1px solid #fff">{r["counts"].get(t,0)}</td>'
            for t in tiers
        )
        body_rows += (
            f'<tr><td style="padding:10px;font-size:12px;font-weight:700;color:{color};border-left:3px solid {color};'
            f'background:#fafafa">{r.get("topic_title","")}</td>{cells}'
            f'<td style="padding:10px;text-align:center;font-size:12px;font-weight:700;color:#111827;background:#f3f4f6">{r.get("total",0)}</td></tr>'
        )

    grid_html = f"""
    <table style="width:100%;border-collapse:collapse;margin-top:6px">
        <thead><tr>
            <th style="padding:8px 10px;font-size:10.5px;text-align:left;color:#374151">TOPIC</th>
            {header_cells}
            <th style="padding:8px 10px;font-size:10.5px;color:#374151">TOTAL</th>
        </tr></thead>
        <tbody>{body_rows}</tbody>
    </table>""" if rows else '<p style="color:#6b7280;font-size:13px">No source data collected.</p>'

    # Sources
    sources_html = ""
    for s in report.get("sources", [])[:15]:
        icon = _TIER_ICONS.get(s.get("type", "other"), "🔗")
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
  .pg-cards {{ columns: 2; column-gap: 14px; }}
</style>
</head>
<body>
  <!-- Cover strip -->
  <div style="background:linear-gradient(135deg,#0d9488,#0891b2,#3b82f6);padding:32px 36px;margin-bottom:32px;border-radius:12px">
    <div style="font-size:11px;color:rgba(255,255,255,.7);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px">Nexora AI · PulseGrid</div>
    <div style="font-size:26px;font-weight:800;color:#fff;margin-bottom:4px">{report.get('title','')}</div>
    <div style="font-size:13px;color:rgba(255,255,255,.8);margin-bottom:18px">Documents: {doc_names_str}</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <span style="background:rgba(255,255,255,.18);border-radius:8px;padding:6px 14px;font-size:12px;color:#fff">📅 {gen_at}</span>
      <span style="background:rgba(255,255,255,.18);border-radius:8px;padding:6px 14px;font-size:12px;color:#fff">🌐 {freshness}</span>
    </div>
  </div>

  <!-- Stat cards -->
  <div style="font-size:14px;font-weight:700;color:#1f2937;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #e5e7eb">
    🎯 Stat Cards
  </div>
  <div class="pg-cards">{cards_html or '<p style="color:#6b7280;font-size:13px">No statistics found in web results.</p>'}</div>

  <!-- Credibility grid -->
  <div style="font-size:14px;font-weight:700;color:#1f2937;margin:28px 0 16px;padding-bottom:8px;border-bottom:2px solid #e5e7eb">
    🗺️ Credibility Grid
  </div>
  {grid_html}

  <!-- Sources -->
  <div style="padding:18px;background:#f9fafb;border-radius:12px;border:1px solid #e5e7eb;margin-top:24px;margin-bottom:20px">
    <div style="font-size:12px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px">🔗 Web Sources Consulted</div>
    <ul style="padding-left:16px;columns:2;gap:20px">{sources_html or '<li>No sources.</li>'}</ul>
  </div>

  <div style="text-align:center;font-size:11px;color:#9ca3af;padding-top:12px;border-top:1px solid #e5e7eb">
    Generated by Nexora AI PulseGrid · {gen_at} · For internal use only
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
# JSON parsing helpers  (same defensive strategy as visual_pulse.py — only
# needed for the single topic-extraction LLM call)
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

    print(f"[PG] _parse_json_array raw output ({len(raw)} chars):\n{raw[:800]}{'...' if len(raw) > 800 else ''}")

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
            print(f"[PG] _parse_json_array: response was TRUNCATED — salvaged {len(repaired)} element(s).")
            return repaired

    try:
        json.loads(block or cleaned or raw)
    except Exception:
        print("[PG] JSON array parse error — all passes failed. Last exception:")
        traceback.print_exc()

    return fallback


# ═════════════════════════════════════════════════════════════════════════════
# Flask routes
# ═════════════════════════════════════════════════════════════════════════════

@pulse_grid_bp.route("/generate", methods=["POST"])
def generate():
    """
    POST /pulse_grid/generate
    Body: { "files": [...], "session_id": "..." }
    Returns: { "report": <ReportJSON>, "status": "ok" }
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = session.get("username")
    body = request.json or {}
    session_id = body.get("session_id", "pg-session")
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
            print(f"[PG] file list fallback error: {e}")

    try:
        report = run_pulse_grid(username, filenames, session_id)
        return jsonify({"status": "ok", "report": report})
    except Exception as e:
        print(f"[PG] generate error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@pulse_grid_bp.route("/export_pdf", methods=["POST"])
def export_pdf():
    """
    POST /pulse_grid/export_pdf
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
        filename = f"nexora_pulse_grid_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        print(f"[PG] PDF export error: {e}")
        return jsonify({"status": "error", "message": "PDF generation failed"}), 500


@pulse_grid_bp.route("/history", methods=["GET"])
def history():
    """GET /pulse_grid/history — list saved reports for the user."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    return jsonify({"status": "ok", "reports": _get_reports(username)})


@pulse_grid_bp.route("/history/<report_id>", methods=["GET"])
def get_report(report_id: str):
    """GET /pulse_grid/history/<id> — fetch full report JSON."""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    report = _get_report_by_id(username, report_id)
    if report is None:
        return jsonify({"status": "error", "message": "Report not found"}), 404
    return jsonify({"status": "ok", "report": report})


@pulse_grid_bp.route("/history/<report_id>", methods=["DELETE"])
def delete_report(report_id: str):
    """DELETE /pulse_grid/history/<id>"""
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    username = session.get("username")
    ok = _delete_report(username, report_id)
    if ok:
        return jsonify({"status": "ok", "message": "Report deleted"})
    return jsonify({"status": "error", "message": "Report not found"}), 404
