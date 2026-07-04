"""
api_router.py — Nexora AI  |  Smart API Key Router
====================================================
Centralised Groq / Gemini key rotation + rate-limit handling.

Usage (replace every direct ChatGroq / get_llm call):
    from api_router import get_routed_llm, call_llm_with_fallback

Key pool is read from environment variables at startup:
    GROQ_API_KEY           — primary key  (general use)
    GROQ_API_KEY_FULL_REPORT — secondary key  (reports)
    GROQ_UPLOAD_API        — tertiary key   (upload / ingest helpers)
    INSIGHTS_GEMINI_API_KEY — Gemini flash  (fallback when Groq 429s)
    GEMINI_API_KEY          — Gemini flash  (second Gemini fallback)

The router:
  1. Tries each Groq key in round-robin until one succeeds.
  2. If ALL Groq keys are rate-limited → falls back to Gemini Flash.
  3. Surfaces the original exception only when every provider fails.

Drop-in replacement for rag_logic.get_llm():
    llm = get_routed_llm(session_id, settings)
    response = llm.invoke(prompt)

Or use the higher-level helper that handles retries internally:
    text = call_llm_with_fallback(prompt, settings)
"""

from __future__ import annotations

import os
import time
import random
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Key pools ───────────────────────────────────────────────────────────────

def _groq_keys() -> list[str]:
    """Return all configured Groq API keys (non-empty, deduplicated)."""
    candidates = [
        os.getenv("GROQ_API_KEY", ""),
        os.getenv("GROQ_API_KEY_FULL_REPORT", ""),
        os.getenv("GROQ_UPLOAD_API", ""),
    ]
    seen, keys = set(), []
    for k in candidates:
        k = k.strip()
        if k and k not in seen and "your_" not in k:
            seen.add(k)
            keys.append(k)
    return keys


def _gemini_keys() -> list[str]:
    candidates = [
        os.getenv("INSIGHTS_GEMINI_API_KEY", ""),
        os.getenv("GEMINI_API_KEY", ""),
    ]
    seen, keys = set(), []
    for k in candidates:
        k = k.strip()
        if k and k not in seen and "your_" not in k:
            seen.add(k)
            keys.append(k)
    return keys


# ─── Round-robin state (in-process, resets on restart) ───────────────────────

_groq_index = 0


def _next_groq_key() -> Optional[str]:
    global _groq_index
    keys = _groq_keys()
    if not keys:
        return None
    key = keys[_groq_index % len(keys)]
    _groq_index = (_groq_index + 1) % len(keys)
    return key


# ─── LLM factories ───────────────────────────────────────────────────────────

def _make_groq_llm(api_key: str, settings: dict):
    from langchain_groq import ChatGroq
    return ChatGroq(
        groq_api_key=api_key,
        model_name=settings.get("model_name", "llama-3.3-70b-versatile"),
        temperature=settings.get("temperature", 0.0),
        max_tokens=settings.get("max_tokens", 1024),  # cap output tokens
    )


def _make_gemini_llm(api_key: str, settings: dict):
    """
    Gemini Flash via LangChain Google GenAI.
    Install: pip install langchain-google-genai
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=api_key,
            temperature=settings.get("temperature", 0.0),
            max_output_tokens=settings.get("max_tokens", 1024),
        )
    except ImportError:
        raise RuntimeError(
            "langchain-google-genai not installed. "
            "Run: pip install langchain-google-genai"
        )


# ─── Public API ──────────────────────────────────────────────────────────────

def get_routed_llm(session_id: str, settings: dict):
    """
    Drop-in replacement for rag_logic.get_llm().
    Returns the first available LLM instance (Groq → Gemini fallback).
    Does NOT invoke; call .invoke() / .stream() on the result.
    """
    key = _next_groq_key()
    if key:
        return _make_groq_llm(key, settings)

    # No Groq keys configured — try Gemini
    for gkey in _gemini_keys():
        return _make_gemini_llm(gkey, settings)

    raise RuntimeError(
        "No API keys configured. Set GROQ_API_KEY or GEMINI_API_KEY in .env"
    )


def call_llm_with_fallback(
    prompt: str,
    settings: dict,
    *,
    max_retries: int = 3,
    retry_delay: float = 0.5,
) -> str:
    """
    Invoke the LLM with automatic key rotation on 429 / RateLimitError.

    Returns the text content string.
    Raises RuntimeError only when every provider has been exhausted.

    Example:
        text = call_llm_with_fallback(my_prompt, {"model_name": "llama-3.3-70b-versatile"})
    """
    groq_keys  = _groq_keys()
    gemini_keys = _gemini_keys()
    all_errors: list[str] = []

    # ── Try every Groq key ────────────────────────────────────────────────────
    for attempt, key in enumerate(groq_keys):
        try:
            llm      = _make_groq_llm(key, settings)
            response = llm.invoke(prompt)
            return response.content.strip()
        except Exception as e:
            err = str(e)
            all_errors.append(f"Groq[{attempt}]: {err[:120]}")
            if _is_rate_limit(err):
                if _is_daily_quota(err):
                    # Org-level daily token quota — ALL Groq keys under this
                    # account share the same pool, so rotating keys (or
                    # sleeping and retrying) cannot possibly help. Skip
                    # straight to Gemini instead of burning request time.
                    logger.warning(
                        "Groq daily token quota exhausted (org-wide) — "
                        "skipping remaining Groq keys, falling back to Gemini"
                    )
                    break
                logger.warning(
                    "Groq 429 on key #%d — waiting %.1fs before next key",
                    attempt, retry_delay
                )
                time.sleep(retry_delay + random.uniform(0, 1))
                continue
            # Non-rate-limit error — re-raise immediately
            raise

    # ── All Groq keys exhausted — try Gemini ─────────────────────────────────
    logger.warning("All Groq keys rate-limited. Falling back to Gemini Flash.")
    for attempt, key in enumerate(gemini_keys):
        try:
            llm      = _make_gemini_llm(key, settings)
            response = llm.invoke(prompt)
            return response.content.strip()
        except Exception as e:
            err = str(e)
            all_errors.append(f"Gemini[{attempt}]: {err[:120]}")
            if _is_rate_limit(err):
                time.sleep(retry_delay * 2)
                continue
            raise

    raise RuntimeError(
        f"All providers rate-limited or failed.\nErrors: {'; '.join(all_errors)}"
    )


def call_llm_streaming(prompt: str, settings: dict):
    """
    Generator that yields text tokens.  Uses the primary Groq key; on 429
    falls back to a second key (streaming cannot hot-swap mid-stream).

    Usage:
        for token in call_llm_streaming(prompt, settings):
            yield token
    """
    groq_keys = _groq_keys()

    for attempt, key in enumerate(groq_keys):
        try:
            llm = _make_groq_llm(key, settings)
            for chunk in llm.stream(prompt):
                yield chunk.content
            return  # success — done
        except Exception as e:
            err = str(e)
            if _is_rate_limit(err) and attempt < len(groq_keys) - 1:
                logger.warning("Groq stream 429 on key #%d — retrying with next key", attempt)
                time.sleep(2)
                continue
            raise  # propagate for non-429 or last key

    # Last resort: Gemini (non-streaming invoke, then yield full text)
    for key in _gemini_keys():
        try:
            llm      = _make_gemini_llm(key, settings)
            response = llm.invoke(prompt)
            yield response.content
            return
        except Exception:
            pass

    raise RuntimeError("All providers failed during streaming.")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _is_rate_limit(error_str: str) -> bool:
    lower = error_str.lower()
    return any(kw in lower for kw in (
        "rate_limit", "rate limit", "429", "too many requests",
        "ratelimiterror", "quota", "resource_exhausted",
    ))


def _is_daily_quota(error_str: str) -> bool:
    """
    True for org-wide DAILY token-quota errors (TPD), as opposed to a
    transient per-minute/per-second rate limit (RPM/TPM). Daily quota
    errors are shared across every key in the same Groq org, so retrying
    with a different key from the pool is pointless until the quota resets
    (Groq reports the reset time in the error, often 5-10+ minutes away —
    far longer than any web request should block for).
    """
    lower = error_str.lower()
    return "tokens per day" in lower or "tpd" in lower
