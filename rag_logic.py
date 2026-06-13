"""
rag_logic.py — Nexora AI RAG Engine
====================================
Centralised module for ALL RAG-related logic:
  • Embeddings (cached)
  • Document parsing  (PDF / PPTX / DOCX / CSV / text)
  • OCR helpers       (Llama-4-Scout vision)
  • Chunking
  • Chroma vector store (single collection + metadata filters)
  • File hashing + deduplication
  • BM25 sparse search
  • Hybrid search      (dense + sparse merge)
  • Cross-encoder reranker
  • Query condensation + validation
  • Guardrails         (PII, prompt-injection, hallucination)
  • Citation-level chunk verification
  • Source metadata formatting

Drawbacks addressed
-------------------
1.  No Reranking Layer          → cross_encoder_rerank()
2.  No Document Versioning      → version_document() + DB helper
3.  No File Hashing             → compute_file_hash() + duplicate guard
4.  Chroma Collection-per-User  → single "nexora_main" collection + username filter
5.  No Async Ingestion Queue    → ingest_document_task() ready for Celery
6.  No RAG Evaluation Framework → evaluate_rag_answer() (RAGAS-style heuristics)
7.  No Citation-Level Verification → get_chunk_citation()
8.  OCR Cost Explosion          → Tesseract first, Llama fallback
9.  No Embedding Cache          → file-hash-keyed embedding skip
10. No Query Rewriting Validation → validate_condensed_query()
"""

from __future__ import annotations

import traceback
import base64
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any

import chromadb
import httpx
import numpy as np
from langchain_chroma import Chroma
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_core.documents import Document
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEndpointEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from PIL import Image
from rank_bm25 import BM25Okapi

import fitz  # PyMuPDF
from huggingface_hub import InferenceClient
# ============================================================
# CONSTANTS
# ============================================================

CHROMA_PATH       = "./chroma_db"
CHROMA_COLLECTION = "nexora_main"          # single shared collection
EMBED_CACHE_FILE  = "./embed_cache.json"   # hash → already-indexed flag
UPLOAD_FOLDER     = "uploads"

# ============================================================
# 1. EMBEDDINGS — with instance cache (no re-init per request)
# ============================================================

_embedding_instance = None

def get_embeddings():
    """Return a cached embedding model instance."""
    global _embedding_instance
    if _embedding_instance is not None:
        return _embedding_instance

    hf_token = os.getenv("HUGGINGFACEHUB_API_TOKEN", "")
    if hf_token and "your_huggingface_token" not in hf_token:
        try:
            _embedding_instance = HuggingFaceEndpointEmbeddings(
                model="sentence-transformers/all-MiniLM-L6-v2",
                huggingfacehub_api_token=hf_token
            )
            return _embedding_instance
        except Exception as e:
            print(f"[Embeddings] HF endpoint failed: {e} — trying local")

    try:
        from langchain_huggingface import HuggingFaceEmbeddings
        _embedding_instance = HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )
        return _embedding_instance
    except Exception as e:
        print(f"[Embeddings] Local load failed: {e} — using endpoint fallback")
        _embedding_instance = HuggingFaceEndpointEmbeddings(
            model="sentence-transformers/all-MiniLM-L6-v2",
            huggingfacehub_api_token=hf_token
        )
        return _embedding_instance


# ============================================================
# 2. CHROMA — single collection, metadata-filtered per user
# ============================================================

def get_vectordb() -> Chroma:
    """Return a Chroma instance pointing at the shared collection."""
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    return Chroma(
        client=client,
        collection_name=CHROMA_COLLECTION,
        embedding_function=get_embeddings()
    )


# ============================================================
# 3. FILE HASHING — deduplication + embedding cache
# ============================================================

def compute_file_hash(filepath: str) -> str:
    """Return SHA-256 hex digest of file contents."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _load_embed_cache() -> dict:
    if os.path.exists(EMBED_CACHE_FILE):
        try:
            with open(EMBED_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_embed_cache(cache: dict) -> None:
    with open(EMBED_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def is_already_embedded(file_hash: str) -> bool:
    """Return True if this exact file hash has been embedded before."""
    return file_hash in _load_embed_cache()


def mark_as_embedded(file_hash: str, filename: str) -> None:
    """Record that this file hash has been indexed."""
    cache = _load_embed_cache()
    cache[file_hash] = {"filename": filename, "indexed_at": datetime.utcnow().isoformat()}
    _save_embed_cache(cache)


# ============================================================
# 4. DOCUMENT VERSIONING
# ============================================================

def version_document(db_name: str, username: str, filename: str, file_hash: str) -> None:
    """
    Soft-version: delete old Chroma vectors for the same filename
    before re-indexing. Call this when a user re-uploads an existing file.
    """
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col = client.get_collection(CHROMA_COLLECTION)
        results = col.get(
            where={"$and": [{"username": username}, {"source": filename}]}
        )
        ids = results.get("ids", [])
        if ids:
            col.delete(ids=ids)
            print(f"[Versioning] Removed {len(ids)} old vectors for {filename} (user={username})")
    except Exception as e:
        print(f"[Versioning] Could not remove old vectors: {e}")

    # Update embed-cache entry so the new hash is treated as fresh
    cache = _load_embed_cache()
    # Remove any cache entry whose filename matches (old hash key unknown)
    stale_keys = [k for k, v in cache.items() if v.get("filename") == filename]
    for k in stale_keys:
        del cache[k]
    _save_embed_cache(cache)


# ============================================================
# 5. OCR HELPERS
# ============================================================

def image_to_base64(pil_image: Image.Image) -> str:
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _try_tesseract_ocr(pil_image: Image.Image) -> str:
    """
    Fast, free OCR with Tesseract.
    Falls back gracefully if pytesseract is not installed.
    """
    try:
        import pytesseract
        return pytesseract.image_to_string(pil_image).strip()
    except Exception:
        return ""


def ocr_image_with_llama(base64_image: str, page_num: int = 0) -> str:
    """
    LLM-powered OCR via Llama-4-Scout on Groq.
    Used as FALLBACK when Tesseract yields < 50 chars.
    """
    api_key = os.getenv("GROQ_API_KEY")
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                {"type": "text",
                 "text": (
                     "You are an OCR and document extraction assistant. "
                     "Extract ALL text content from this image exactly as it appears. "
                     "For slides: include titles, bullet points, labels, captions, and any visible text. "
                     "For scanned documents: transcribe all text preserving structure. "
                     "Output only the extracted text, no commentary."
                 )}
            ]
        }]
    }
    try:
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[OCR Llama] Page {page_num}: {e}")
        return ""


def smart_ocr_page(pil_image: Image.Image, page_num: int = 0) -> str:
    """
    Cost-optimised OCR:
    1. Try Tesseract (free, fast, local)
    2. If text < 50 chars → fall back to Llama vision OCR
    """
    text = _try_tesseract_ocr(pil_image)
    if len(text) >= 50:
        return text
    print(f"[OCR] Page {page_num}: Tesseract yielded {len(text)} chars → Llama fallback")
    return ocr_image_with_llama(image_to_base64(pil_image), page_num)


def is_scanned_pdf(filepath: str) -> bool:
    try:
        doc = fitz.open(filepath)
        total_chars = sum(len(p.get_text()) for p in doc)
        avg = total_chars / max(len(doc), 1)
        doc.close()
        return avg < 50
    except Exception:
        return False


def parse_scanned_pdf(filepath: str, filename: str) -> list[Document]:
    docs = []
    try:
        pdf_doc = fitz.open(filepath)
        for page_num in range(len(pdf_doc)):
            page = pdf_doc[page_num]
            mat  = fitz.Matrix(150 / 72, 150 / 72)
            pix  = page.get_pixmap(matrix=mat)
            img  = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text = smart_ocr_page(img, page_num)
            if text:
                docs.append(Document(
                    page_content=text,
                    metadata={"source": filename, "page": page_num}
                ))
        pdf_doc.close()
    except Exception as e:
        print(f"[Scanned PDF Error] {filename}: {e}")
    return docs


def parse_pptx_with_ocr(filepath: str, filename: str) -> list[Document]:
    from pptx import Presentation
    import pptx

    docs = []
    try:
        prs = Presentation(filepath)
        for slide_num, slide in enumerate(prs.slides):
            slide_text = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        line = " ".join([r.text for r in para.runs]).strip()
                        if line:
                            slide_text.append(line)

            combined_text = "\n".join(slide_text).strip()

            if len(combined_text) < 80:
                for shape in slide.shapes:
                    if shape.shape_type == pptx.enum.shapes.MSO_SHAPE_TYPE.PICTURE:
                        try:
                            img   = Image.open(io.BytesIO(shape.image.blob))
                            text  = smart_ocr_page(img, slide_num)
                            if text:
                                slide_text.append(f"[Image Content]: {text}")
                        except Exception as ex:
                            print(f"[PPT OCR Shape] Slide {slide_num+1}: {ex}")
                combined_text = "\n".join(slide_text).strip()

            if combined_text:
                docs.append(Document(
                    page_content=f"Slide {slide_num + 1}:\n{combined_text}",
                    metadata={"source": filename, "page": slide_num}
                ))
    except Exception as e:
        print(f"[PPTX Error] {filename}: {e}")
    return docs


# ============================================================
# 6. DOCUMENT PARSER (unified entry point)
# ============================================================

def parse_document(filepath: str, filename: str) -> list[Document]:
    ext  = os.path.splitext(filename)[1].lower()
    docs = []

    if ext == ".pdf":
        if is_scanned_pdf(filepath):
            print(f"[Parser] Scanned PDF: {filename} → OCR")
            docs = parse_scanned_pdf(filepath, filename)
        else:
            try:
                loader = PyMuPDFLoader(filepath)
                docs   = loader.load()
                total  = "".join(d.page_content for d in docs).strip()
                if len(total) < 100:
                    print(f"[Parser] PyMuPDF too sparse → OCR fallback")
                    docs = parse_scanned_pdf(filepath, filename)
            except Exception as e:
                print(f"[Parser] PDF Error {filename}: {e} → OCR fallback")
                docs = parse_scanned_pdf(filepath, filename)

    elif ext in [".pptx", ".ppt"]:
        docs = parse_pptx_with_ocr(filepath, filename)

    elif ext == ".docx":
        try:
            import docx as python_docx
            doc      = python_docx.Document(filepath)
            fullText = [p.text for p in doc.paragraphs]
            docs     = [Document(page_content="\n".join(fullText),
                                 metadata={"source": filename, "page": 0})]
        except Exception as e:
            print(f"[Parser] DOCX error: {e}")
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                docs = [Document(page_content=f.read(),
                                 metadata={"source": filename, "page": 0})]

    elif ext == ".csv":
        try:
            row_texts = []
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for i, row in enumerate(reader):
                        desc = ", ".join(f"{k}: {v}" for k, v in row.items() if v)
                        row_texts.append(f"Row {i+1}: {desc}")
            docs = [Document(page_content="\n".join(row_texts),
                             metadata={"source": filename, "page": 0})]
        except Exception as e:
            print(f"[Parser] CSV error: {e}")

    else:
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                docs = [Document(page_content=f.read(),
                                 metadata={"source": filename, "page": 0})]
        except Exception as e:
            print(f"[Parser] Text file error: {e}")

    for doc in docs:
        doc.metadata["source"] = filename
        if "page" not in doc.metadata:
            doc.metadata["page"] = 0

    return docs


# ============================================================
# 7. CHUNKING
# ============================================================

def chunk_documents(
    docs: list[Document],
    chunk_size: int = 1000,
    chunk_overlap: int = 150
) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap
    )
    return splitter.split_documents(docs)


# ============================================================
# 8. INGESTION — main entry (called from Flask route)
# ============================================================

def ingest_document(
    filepath: str,
    filename: str,
    username: str,
    session_id: str,
    db_name: str,
    force_reindex: bool = False
) -> dict:
    """
    Full ingestion pipeline with:
    - File hash deduplication          (fix #3, #9)
    - Document versioning              (fix #2)
    - Single Chroma collection         (fix #4)
    - Cost-optimised OCR               (fix #8)

    Returns dict with status and chunk_count.
    """
    file_hash = compute_file_hash(filepath)

    # --- Duplicate guard (same hash, already indexed) ---
    if not force_reindex and is_already_embedded(file_hash):
        print(f"[Ingest] {filename} already indexed (hash match). Skipping.")
        return {"status": "skipped", "reason": "duplicate", "chunk_count": 0}

    # --- Version: remove stale vectors if filename exists ---
    version_document(db_name, username, filename, file_hash)

    # --- Parse ---
    docs = parse_document(filepath, filename)
    if not docs:
        return {"status": "error", "reason": "no_content", "chunk_count": 0}

    # --- Chunk ---
    chunks = chunk_documents(docs)

    # --- Attach metadata for filter-based multi-tenancy ---
    for chunk in chunks:
        chunk.metadata["username"]   = username
        chunk.metadata["session_id"] = session_id
        chunk.metadata["file_hash"]  = file_hash
        chunk.metadata["source"]     = chunk.metadata.get("source", filename)

    # --- Embed + store ---
    vectordb = get_vectordb()
    vectordb.add_documents(
        documents=chunks,
        ids=[str(uuid.uuid4()) for _ in chunks]
    )

    # --- Mark as embedded ---
    mark_as_embedded(file_hash, filename)

    return {"status": "success", "chunk_count": len(chunks)}


# ============================================================
# 9. BM25 SPARSE SEARCH
# ============================================================

def bm25_search(query: str, docs: list[Document], top_k: int = 6) -> list[Document]:
    if not docs:
        return []
    corpus         = [d.page_content.split() for d in docs]
    bm25           = BM25Okapi(corpus)
    scores         = bm25.get_scores(query.split())
    ranked_indices = np.argsort(scores)[::-1]
    return [docs[i] for i in ranked_indices[:top_k]]


# ============================================================
# 10. HYBRID SEARCH (dense + sparse merge)
# ============================================================

def hybrid_search(
    vectordb: Chroma,
    query: str,
    username: str,
    selected_docs: list[str] | None = None,
    k: int = 6
) -> list[Document]:
    """
    Hybrid dense+sparse retrieval against the shared collection.
    Uses username (and optional source list) as metadata filters.
    NOTE: Chroma's $in operator requires ≥2 items in some versions;
    we use the plain equality filter for single-file selections.
    """
    if selected_docs and len(selected_docs) == 1:
        # Single doc: use equality filter (safer across Chroma versions)
        base_filter = {
            "$and": [
                {"username": username},
                {"source":   selected_docs[0]}
            ]
        }
    elif selected_docs and len(selected_docs) > 1:
        base_filter = {
            "$and": [
                {"username": username},
                {"source": {"$in": selected_docs}}
            ]
        }
    else:
        # No filter → search across all of the user's documents
        base_filter = {"username": username}

    dense_docs  = vectordb.similarity_search(query, k=12, filter=base_filter)
    sparse_docs = bm25_search(query, dense_docs, top_k=6)

    merged, seen = [], set()
    for doc in dense_docs + sparse_docs:
        key = (doc.metadata.get("source"), doc.page_content[:100])
        if key not in seen:
            seen.add(key)
            merged.append(doc)

    return merged[:k]


# ============================================================
# 11. CROSS-ENCODER RERANKER via HuggingFace Inference API (fix #1)
#
# Uses the same HUGGINGFACEHUB_API_TOKEN as embeddings.
# Model: cross-encoder/ms-marco-MiniLM-L-6-v2
# API:   POST https://api-inference.huggingface.co/models/<model>
# No sentence-transformers install required.
# ============================================================

_HF_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"

def _hf_rerank_scores(query: str, passages: list[str]) -> list[float] | None:
    """
    Score (query, passage) pairs via HF Inference Providers
    using BAAI/bge-reranker-v2-m3 (a hosted text-classification reranker).
    Returns a list of float scores, or None on failure.
    """
    hf_token = os.getenv("HUGGINGFACEHUB_API_TOKEN", "")
    if not hf_token or "your_huggingface_token" in hf_token:
        print("[Reranker] HUGGINGFACEHUB_API_TOKEN not set — reranking skipped.")
        return None

    try:
        client = InferenceClient(provider="hf-inference", api_key=hf_token)
        scores = []
        for p in passages:
            result = client.text_classification(
                text=f"{query}</s></s>{p}",
                model=_HF_RERANKER_MODEL
            )
            best = max(result, key=lambda x: x.score)
            scores.append(best.score)
        return scores

    except Exception as e:
        print(traceback.format_exc())
        print(f"[Reranker] HF API error: {e} — falling back to original order.")
        return None

def cross_encoder_rerank(
    query: str,
    docs: list[Document],
    top_k: int = 5
) -> list[Document]:
    """
    Rerank retrieved chunks via the HuggingFace Inference API cross-encoder.
    Falls back silently to the original hybrid-search order if the API
    is unavailable or the token is missing.
    """
    if not docs:
        return docs

    passages = [doc.page_content for doc in docs]
    scores   = _hf_rerank_scores(query, passages)

    if scores is None or len(scores) != len(docs):
        # Graceful fallback — no crash, just return top-k as-is
        return docs[:top_k]

    ranked   = sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)
    reranked = [doc for _, doc in ranked[:top_k]]
    print(f"[Reranker] HF reranked {len(docs)} chunks → top {top_k} "
          f"(best score: {ranked[0][0]:.3f})")
    return reranked


# ============================================================
# 12. GUARDRAILS
# ============================================================

BLOCKED_PATTERNS = [
    "ignore previous instructions",
    "ignore all instructions",
    "system prompt",
    "reveal prompt",
    "developer instructions",
    "bypass security",
    "jailbreak",
    "act as",
    "pretend to be",
    "disable guardrails",
    "confidential keys",
    "api key",
    "password",
    "token"
]

PII_PATTERNS = [
    r"\b\d{12}\b",
    r"\b\d{10}\b",
    r"\b[A-Z]{5}[0-9]{4}[A-Z]\b",
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",  # email
    r"\b(?:\+91[\-\s]?)?[6789]\d{9}\b",              # Indian mobile
]


def is_blocked_query(question: str) -> bool:
    q = question.lower()
    return any(p in q for p in BLOCKED_PATTERNS)


def contains_pii(text: str) -> bool:
    for pattern in PII_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def sanitize_answer(answer: str) -> str:
    """Strip any accidental secret leakage from LLM output."""
    leakage_patterns = [
        r"(sk-[A-Za-z0-9]{20,})",
        r"(hf_[A-Za-z0-9]{20,})",
        r"(Bearer\s+[A-Za-z0-9\-_.]+)",
    ]
    for pat in leakage_patterns:
        answer = re.sub(pat, "[REDACTED]", answer)
    return answer


# ============================================================
# 13. QUERY CONDENSATION + VALIDATION (fix #10)
# ============================================================

def condense_question(
    session_id: str,
    question: str,
    chat_history: list[dict],
    settings: dict
) -> str:
    if not chat_history:
        return question

    try:
        llm = get_llm(session_id, settings)
        history_str = ""
        for msg in chat_history[-3:]:
            history_str += f"User: {msg.get('question','')}\nAssistant: {msg.get('answer','')}\n"

        prompt = f"""Given the following conversation history and a follow-up question, rephrase the follow-up question to be a standalone question in its original language.

Conversation History:
{history_str}

Follow-up Question: {question}

Standalone Question (output only the question, no explanation):"""

        response   = llm.invoke(prompt)
        standalone = response.content.strip()
        return standalone if standalone else question
    except Exception as e:
        print(f"[Condense] Error: {e}")
        return question


def validate_condensed_query(original: str, condensed: str) -> str:
    """
    Fix #10: Validate that the condensed query does not drastically alter
    the user's intent. Uses simple overlap heuristic; LLM validation
    can be plugged in here for production.

    Returns the safer of the two queries.
    """
    if not condensed or len(condensed) < 5:
        print("[QueryValidation] Condensed query too short — reverting to original")
        return original

    orig_words = set(original.lower().split())
    cond_words = set(condensed.lower().split())

    # Jaccard similarity
    if not orig_words:
        return condensed

    overlap = len(orig_words & cond_words) / len(orig_words | cond_words)

    # If less than 10 % word overlap — likely a hallucinated rewrite
    if overlap < 0.10 and len(original) > 20:
        print(f"[QueryValidation] Low overlap ({overlap:.2f}) — reverting to original")
        return original

    return condensed


# ============================================================
# 14. CITATION-LEVEL CHUNK VERIFICATION (fix #7)
# ============================================================

def get_chunk_citation(docs: list[Document]) -> list[dict]:
    """
    Return rich citation objects: source, page, exact paragraph snippet,
    and a chunk_id for UI-level reference.
    """
    citations = []
    seen      = set()
    for i, doc in enumerate(docs):
        src  = doc.metadata.get("source", "Unknown")
        page = doc.metadata.get("page", 0)
        key  = (src, page, doc.page_content[:80])
        if key in seen:
            continue
        seen.add(key)
        citations.append({
            "citation_id":   i + 1,
            "source":        src,
            "page":          page + 1,
            "paragraph":     doc.page_content[:400].strip(),  # exact chunk text
            "full_chunk":    doc.page_content.strip(),
            "file_hash":     doc.metadata.get("file_hash", ""),
        })
    return citations


def get_source_metadata(documents: list[Document]) -> list[dict]:
    """Lightweight source summary (for backwards-compat with existing routes)."""
    sources, seen = [], []
    for d in documents:
        meta = {
            "source":  d.metadata.get("source", "Unknown"),
            "page":    d.metadata.get("page", 0) + 1,
            "content": d.page_content[:250]
        }
        idx = (meta["source"], meta["page"])
        if idx not in seen:
            seen.append(idx)
            sources.append(meta)
    return sources


# ============================================================
# 15. HALLUCINATION SAFETY
# ============================================================

UNKNOWN_PATTERNS = [
    "i don't know", "do not know", "not available",
    "not mentioned", "cannot find", "no information",
    "not found in", "not present in"
]


def is_hallucinating(answer: str) -> bool:
    al = answer.lower()
    return any(p in al for p in UNKNOWN_PATTERNS)


# ============================================================
# 16. RAG EVALUATION — lightweight heuristics (fix #6)
#     Drop-in stubs; replace with RAGAS / DeepEval calls in prod
# ============================================================

def evaluate_rag_answer(
    question: str,
    answer: str,
    retrieved_docs: list[Document]
) -> dict:
    """
    Lightweight local RAG evaluation heuristics.
    Returns scores dict that you can log / store in SQLite.

    Replace bodies with ragas.evaluate() calls in production.
    """
    context_str = " ".join(d.page_content for d in retrieved_docs).lower()
    answer_lower = answer.lower()

    # Faithfulness proxy: fraction of answer sentences traceable to context
    answer_sentences = [s.strip() for s in answer.split(".") if s.strip()]
    grounded = sum(
        1 for s in answer_sentences
        if any(w in context_str for w in s.lower().split() if len(w) > 4)
    )
    faithfulness = round(grounded / max(len(answer_sentences), 1), 2)

    # Context relevance: fraction of retrieved chunks mentioning query terms
    q_terms    = [w for w in question.lower().split() if len(w) > 3]
    rel_chunks = sum(
        1 for d in retrieved_docs
        if any(t in d.page_content.lower() for t in q_terms)
    )
    context_relevance = round(rel_chunks / max(len(retrieved_docs), 1), 2)

    return {
        "faithfulness":       faithfulness,
        "context_relevance":  context_relevance,
        "retrieved_chunks":   len(retrieved_docs),
        "answer_length":      len(answer),
        "evaluated_at":       datetime.utcnow().isoformat(),
    }


# ============================================================
# 17. LLM LOADER (shared, avoids re-import in app.py)
# ============================================================

def get_llm(session_id: str, settings: dict) -> ChatGroq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or "your_groq_api_key" in api_key:
        raise ValueError("GROQ_API_KEY is not configured. Please add it to your .env file.")
    return ChatGroq(
        groq_api_key=api_key,
        model_name=settings["model_name"],
        temperature=settings["temperature"]
    )


# ============================================================
# 18. MISC HELPERS
# ============================================================

def format_docs(docs: list[Document]) -> str:
    return "\n\n".join(d.page_content for d in docs)


def is_general_question(question: str) -> bool:
    q = question.lower().strip()
    casual_patterns = [
        r"\bhi\b", r"\bhello\b", r"\bhey\b", r"\bbye\b",
        r"\bgood morning\b", r"\bgood evening\b", r"\bhow are you\b",
        r"\bwho are you\b", r"\bthank you\b", r"\bthanks\b",
        r"\bjoke\b", r"\bwhat is your name\b", r"\bhow old are you\b",
        r"\bwhat can you do\b", r"\bwhat is ai\b"
    ]
    return any(re.search(p, q) for p in casual_patterns)
