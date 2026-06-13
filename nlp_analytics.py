"""
nlp_analytics.py — Nexora AI  |  NLP Analytics Suite
======================================================
Provides five NLP analysis endpoints backed by Chroma chunks + LLM,
following the same fetch/invoke pattern as knowledge_graph.py.

Register in app.py:
    from nlp_analytics import nlp_analytics_bp
    app.register_blueprint(nlp_analytics_bp)

Single master route:
    POST /nlp_analytics
    body: { "session_id": "...", "files": [...], "analyses": ["all"] }

Returns:
    {
        "sentiment":   { "overall": "Positive", "score": 0.72, "breakdown": [...] },
        "wordcloud":   [ {"word": "...", "weight": 42}, ... ],
        "topics":      [ {"id": 0, "label": "...", "keywords": [...], "weight": 0.3}, ... ],
        "keyphrases":  [ {"phrase": "...", "score": 0.9, "source": "..."}, ... ],
        "entities":    [ {"text": "...", "type": "ORG", "count": 5}, ... ],
        "readability": { "score": 68.2, "grade": "Standard", "avg_sentence_len": 18 },
        "stats":       { "chunks": 42, "sources": 3, "total_words": 8400 }
    }
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from math import log

from token_utils import trim_to_budget
from api_router  import call_llm_with_fallback

import chromadb
from flask import Blueprint, jsonify, request, session

from rag_logic import CHROMA_PATH, CHROMA_COLLECTION, get_llm

nlp_analytics_bp = Blueprint("nlp_analytics_bp", __name__)

# ── LLM config ────────────────────────────────────────────────────────────────
_LLM_SETTINGS = {"model_name": "llama-3.3-70b-versatile", "temperature": 0.0}

# ── Common English stop-words (lightweight, no NLTK dep) ─────────────────────
_STOPWORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "by","from","up","about","into","through","during","is","are","was","were",
    "be","been","being","have","has","had","do","does","did","will","would",
    "could","should","may","might","shall","can","need","dare","ought","used",
    "i","me","my","myself","we","our","ours","ourselves","you","your","yours",
    "he","him","his","she","her","hers","it","its","they","them","their",
    "what","which","who","whom","this","that","these","those","then","than",
    "so","yet","both","either","neither","not","no","nor","as","if","while",
    "although","because","since","unless","until","when","where","how","all",
    "each","every","more","most","other","some","such","only","own","same",
    "also","just","over","s","t","also","very","also","also","also",
}


# ─── Chroma fetch (identical pattern to knowledge_graph.py) ──────────────────

def _fetch_chunks(username: str, filenames: list[str] | None) -> list[dict]:
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

        results   = col.get(where=where, limit=120, include=["documents", "metadatas"])
        docs      = results.get("documents", []) or []
        metadatas = results.get("metadatas", []) or []

        return [
            {"text": d, "source": (m or {}).get("source", "Unknown")}
            for d, m in zip(docs, metadatas)
            if d and len(d.strip()) > 40
        ]
    except Exception as e:
        print(f"[NLP] Chroma fetch error: {e}")
        return []


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
        print(f"[NLP] DB error: {e}")
        return []


def _combined_text(chunks: list[dict]) -> str:
    return " ".join(c["text"] for c in chunks)


# ─── 1. Sentiment Analysis ────────────────────────────────────────────────────
def _analyse_sentiment(chunks: list[dict], session_id: str) -> dict:
    from collections import defaultdict
    by_source: dict[str, list[str]] = defaultdict(list)
    for ch in chunks:
        by_source[ch["source"]].append(ch["text"])

    breakdown, scores = [], []

    for source, texts in list(by_source.items())[:8]:
        combined = trim_to_budget(" ".join(texts[:4]), 1_000)  # 1k tok/source
        prompt = f"""Sentiment of this excerpt from "{source}".
Return ONLY JSON: {{"label":"Positive"|"Negative"|"Neutral"|"Mixed","score":0.0-1.0,"reason":"one sentence"}}

Text: \\"\\"\\"{combined}\\"\\"\\"

JSON:"""
        try:
            raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 80})
            raw = re.sub(r"^```[a-z]*\\n?|```$", "", raw, flags=re.MULTILINE).strip()
            obj = json.loads(re.search(r"\\{.*\\}", raw, re.DOTALL).group())
            breakdown.append({"source": source, "label": obj.get("label","Neutral"),
                              "score": float(obj.get("score", 0.5)), "reason": obj.get("reason","")})
            scores.append(float(obj.get("score", 0.5)))
        except Exception as e:
            print(f"[NLP/Sentiment] {source}: {e}")
            breakdown.append({"source": source, "label": "Neutral", "score": 0.5, "reason": ""})
            scores.append(0.5)

    avg = sum(scores) / len(scores) if scores else 0.5
    overall = "Positive" if avg >= 0.65 else "Negative" if avg <= 0.35 else (
        "Mixed" if any(b["label"] == "Mixed" for b in breakdown) else "Neutral"
    )
    return {"overall": overall, "score": round(avg, 3), "breakdown": breakdown}


# ─── 2. Word Cloud ────────────────────────────────────────────────────────────

def _build_wordcloud(chunks: list[dict]) -> list[dict]:
    """TF-IDF-lite word weights — no external deps."""
    docs_words: list[list[str]] = []
    for ch in chunks:
        words = re.findall(r"[a-zA-Z]{3,}", ch["text"].lower())
        docs_words.append([w for w in words if w not in _STOPWORDS])

    N = len(docs_words)
    if N == 0:
        return []

    # TF across all docs
    tf: Counter = Counter(w for doc in docs_words for w in doc)

    # DF per word
    df: Counter = Counter()
    for doc in docs_words:
        for w in set(doc):
            df[w] += 1

    # TF-IDF score
    scored = {
        w: tf[w] * log((N + 1) / (df[w] + 1))
        for w in tf if tf[w] >= 2
    }

    top = sorted(scored.items(), key=lambda x: x[1], reverse=True)[:120]
    max_s = top[0][1] if top else 1
    return [{"word": w, "weight": round((s / max_s) * 100)} for w, s in top]


# ─── 3. Topic Modelling ───────────────────────────────────────────────────────
def _model_topics(chunks: list[dict], session_id: str) -> list[dict]:
    combined = trim_to_budget(" ".join(c["text"] for c in chunks[:12]), 2_000)
    prompt = f"""Identify 5 latent topics.
For each: label (2-4 words), keywords (6 terms), weight (0.0-1.0, sum≈1.0).
Output ONLY a valid JSON array. No markdown.

Text: \\"\\"\\"{combined}\\"\\"\\"

JSON:"""
    try:
        raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 500})
        raw = re.sub(r"^```[a-z]*\\n?|```$", "", raw, flags=re.MULTILINE).strip()
        arr = json.loads(re.search(r"\\[.*\\]", raw, re.DOTALL).group())
        return [{"id": i, "label": t.get("label", f"Topic {i}"),
                 "keywords": t.get("keywords",[]), "weight": round(float(t.get("weight", 0.2)), 3)}
                for i, t in enumerate(arr) if isinstance(t, dict)]
    except Exception as e:
        print(f"[NLP/Topics] {e}")
        return []


# ─── 4. Keyphrase Extraction ──────────────────────────────────────────────────
def _extract_keyphrases(chunks: list[dict], session_id: str) -> list[dict]:
    from collections import defaultdict
    by_source: dict[str, list[str]] = defaultdict(list)
    for ch in chunks:
        by_source[ch["source"]].append(ch["text"])

    all_phrases: list[dict] = []
    for source, texts in list(by_source.items())[:6]:
        combined = trim_to_budget(" ".join(texts[:4]), 900)
        prompt = f"""Top 8 keyphrases from "{source}".
Return ONLY JSON array: [{{"phrase":"...","score":0.0-1.0}}]. No markdown.

Text: \\"\\"\\"{combined}\\"\\"\\"

JSON:"""
        try:
            raw = call_llm_with_fallback(prompt, {**_LLM_SETTINGS, "max_tokens": 300})
            raw = re.sub(r"^```[a-z]*\\n?|```$", "", raw, flags=re.MULTILINE).strip()
            arr = json.loads(re.search(r"\\[.*\\]", raw, re.DOTALL).group())
            for p in arr:
                if isinstance(p, dict) and p.get("phrase"):
                    all_phrases.append({"phrase": p["phrase"], "score": float(p.get("score", 0.5)), "source": source})
        except Exception as e:
            print(f"[NLP/Keyphrases] {source}: {e}")

    return sorted(all_phrases, key=lambda x: x["score"], reverse=True)[:40]


# ─── 5. Named Entity Recognition ─────────────────────────────────────────────

def _extract_entities(chunks: list[dict], session_id: str) -> list[dict]:
    """NER via LLM — returns aggregated entity counts."""
    combined = " ".join(c["text"] for c in chunks[:15])[:4000]
    prompt = f"""You are a Named Entity Recognition assistant.

Extract all named entities from the text below.
Group by type: PERSON, ORG, LOCATION, DATE, PRODUCT, CONCEPT, OTHER.

Return ONLY a valid JSON array of objects with keys:
  "text"  : entity string
  "type"  : entity type (one of the above)
  "count" : approximate mention count (integer)

No markdown.

Text:
\"\"\"{combined}\"\"\"

JSON:"""
    try:
        llm  = get_llm(session_id, _LLM_SETTINGS)
        raw  = llm.invoke(prompt).content.strip()
        raw  = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
        arr  = json.loads(re.search(r"\[.*\]", raw, re.DOTALL).group())
        valid_types = {"PERSON","ORG","LOCATION","DATE","PRODUCT","CONCEPT","OTHER"}
        return [
            {
                "text":  e.get("text", ""),
                "type":  e.get("type", "OTHER") if e.get("type") in valid_types else "OTHER",
                "count": int(e.get("count", 1)),
            }
            for e in arr
            if isinstance(e, dict) and e.get("text")
        ]
    except Exception as e:
        print(f"[NLP/NER] {e}")
        return []


# ─── 6. Readability ──────────────────────────────────────────────────────────

def _compute_readability(chunks: list[dict]) -> dict:
    """Flesch Reading Ease approximation — no deps."""
    text = _combined_text(chunks)
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.strip()) > 10]
    words     = re.findall(r"[a-zA-Z]+", text)
    syllables = sum(
        max(1, len(re.findall(r"[aeiouAEIOU]", w)) )
        for w in words
    )

    n_sent = max(1, len(sentences))
    n_word = max(1, len(words))
    n_syll = max(1, syllables)

    # Flesch Reading Ease
    score = 206.835 - 1.015 * (n_word / n_sent) - 84.6 * (n_syll / n_word)
    score = max(0.0, min(100.0, score))

    if score >= 70:   grade = "Easy"
    elif score >= 50: grade = "Standard"
    elif score >= 30: grade = "Difficult"
    else:             grade = "Very Difficult"

    return {
        "score":            round(score, 1),
        "grade":            grade,
        "avg_sentence_len": round(n_word / n_sent, 1),
        "total_words":      n_word,
        "total_sentences":  n_sent,
    }


# ─── Route ───────────────────────────────────────────────────────────────────

@nlp_analytics_bp.route("/nlp_analytics", methods=["POST"])
def nlp_analytics():
    """
    POST /nlp_analytics
    Body: { "session_id": "...", "files": [...], "analyses": ["all"] }
    """
    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username   = session.get("username")
    body       = request.json or {}
    session_id = body.get("session_id", "nlp-session")
    filenames  = body.get("files") or []
    analyses   = body.get("analyses", ["all"])

    if isinstance(filenames, str):
        filenames = [f.strip() for f in filenames.split(",") if f.strip()]

    if not filenames:
        filenames = _all_filenames(username)

    chunks = _fetch_chunks(username, filenames or None)
    if not chunks:
        return jsonify({
            "sentiment":   {},
            "wordcloud":   [],
            "topics":      [],
            "keyphrases":  [],
            "entities":    [],
            "readability": {},
            "stats":       {"chunks": 0, "sources": 0, "total_words": 0},
        })

    run_all = "all" in analyses
    result: dict = {}

    if run_all or "sentiment"   in analyses:
        result["sentiment"]   = _analyse_sentiment(chunks, session_id)

    if run_all or "wordcloud"   in analyses:
        result["wordcloud"]   = _build_wordcloud(chunks)

    if run_all or "topics"      in analyses:
        result["topics"]      = _model_topics(chunks, session_id)

    if run_all or "keyphrases"  in analyses:
        result["keyphrases"]  = _extract_keyphrases(chunks, session_id)

    if run_all or "entities"    in analyses:
        result["entities"]    = _extract_entities(chunks, session_id)

    if run_all or "readability" in analyses:
        result["readability"] = _compute_readability(chunks)

    total_words = len(re.findall(r"[a-zA-Z]+", _combined_text(chunks)))
    result["stats"] = {
        "chunks":      len(chunks),
        "sources":     len(set(c["source"] for c in chunks)),
        "total_words": total_words,
    }

    return jsonify(result)