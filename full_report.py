"""
full_report.py — Nexora AI  Full Report Generator
==================================================
Generates comprehensive PDF and DOCX reports from all (or selected)
indexed documents using the RAG pipeline to produce structured sections:
  • Executive Summary
  • Key Findings
  • Detailed Analysis (per-document)
  • Conclusion & Recommendations

Dependencies:
    pip install reportlab python-docx
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import textwrap
from datetime import datetime
from typing import Optional

import chromadb

# ── ReportLab (PDF) ──────────────────────────────────────────────────────────
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── python-docx (DOCX) ───────────────────────────────────────────────────────
from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Inches, Pt, RGBColor

# ── Project imports ───────────────────────────────────────────────────────────
from rag_logic import CHROMA_PATH, CHROMA_COLLECTION, get_llm

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

BRAND_PRIMARY   = colors.HexColor("#6C63FF")   # Nexora violet
BRAND_SECONDARY = colors.HexColor("#A78BFA")
BRAND_DARK      = colors.HexColor("#1E1B2E")
BRAND_LIGHT     = colors.HexColor("#F5F3FF")
ACCENT_RULE     = colors.HexColor("#7C3AED")

SECTIONS = [
    ("executive_summary",       "Executive Summary"),
    ("key_findings",            "Key Findings"),
    ("detailed_analysis",       "Detailed Analysis"),
    ("conclusion",              "Conclusion & Recommendations"),
]

DB_NAME = os.getenv("DB_NAME", "chat_history.db")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Retrieve all document text from ChromaDB
# ─────────────────────────────────────────────────────────────────────────────

def fetch_document_chunks(username: str, selected_docs: list[str]) -> dict[str, str]:
    """
    Pull every stored chunk for the given user (optionally filtered by filename).
    Returns { filename: concatenated_text }.
    """
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    try:
        col = client.get_collection(CHROMA_COLLECTION)
    except Exception:
        return {}

    if selected_docs:
        if len(selected_docs) == 1:
            where: dict = {"$and": [{"username": username}, {"source": selected_docs[0]}]}
        else:
            where = {"$and": [{"username": username}, {"source": {"$in": selected_docs}}]}
    else:
        where = {"username": username}

    results = col.get(where=where, include=["documents", "metadatas"])
    doc_map: dict[str, list[tuple[int, str]]] = {}
    for doc_text, meta in zip(results.get("documents", []), results.get("metadatas", [])):
        source = meta.get("source", "Unknown")
        page   = int(meta.get("page", 0))
        doc_map.setdefault(source, []).append((page, doc_text))

    # Sort chunks by page order and join
    return {
        src: "\n\n".join(t for _, t in sorted(chunks))
        for src, chunks in doc_map.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — LLM section generation
# ─────────────────────────────────────────────────────────────────────────────
def _call_llm(prompt: str, session_id: str, settings: dict) -> str:
    """Uses dedicated GROQ_API_KEY_FULL_REPORT, falls back to router."""
    import os
    from api_router import call_llm_with_fallback
    dedicated = os.getenv("GROQ_API_KEY_FULL_REPORT", "").strip()
    if dedicated:
        try:
            from langchain_groq import ChatGroq
            llm = ChatGroq(
                groq_api_key=dedicated,
                model_name=settings.get("model_name", "llama-3.3-70b-versatile"),
                temperature=settings.get("temperature", 0.0),
                max_tokens=2048,
            )
            return llm.invoke(prompt).content.strip()
        except Exception as e:
            print(f"[Report] Dedicated key failed ({e}), using router.")
    return call_llm_with_fallback(prompt, {**settings, "max_tokens": 2048})


def generate_section(
    section_key: str,
    section_title: str,
    doc_map: dict[str, str],
    session_id: str,
    settings: dict,
) -> str:
    from token_utils import trim_to_budget
    combined_context = ""
    for fname, text in doc_map.items():
        # ← KEY CHANGE: 1 200 tokens per doc instead of 4 000 chars
        snippet = trim_to_budget(text, 1_200)
        combined_context += f"\\n\\n--- Document: {fname} ---\\n{snippet}"

    # Trim the whole context to 6 000 tokens max
    combined_context = trim_to_budget(combined_context, 6_000)

    prompts = {
        "executive_summary": f"""Write a concise Executive Summary (3-5 paragraphs) for these documents.
DOCUMENTS:{combined_context}
Executive Summary only:""",

        "key_findings": f"""List the top 8-12 Key Findings (numbered) from these documents. Be specific.
DOCUMENTS:{combined_context}
Key Findings only:""",

        "detailed_analysis": f"""Provide a Detailed Analysis with sub-sections:
1. Content Overview  2. Main Themes  3. Data & Metrics  4. Risks  5. Strengths
DOCUMENTS:{combined_context}
Detailed Analysis only:""",

        "conclusion": f"""Write:
1. A concise Conclusion (2-3 paragraphs)
2. Recommendations (5-8 actionable, prioritised items)
DOCUMENTS:{combined_context}
Conclusion and Recommendations only:""",
    }

    prompt = prompts.get(section_key, f"Summarise these documents:\\n{combined_context}")
    return _call_llm(prompt, session_id, settings)


def generate_all_sections(
    doc_map: dict[str, str],
    session_id: str,
    settings: dict,
) -> dict[str, str]:
    """Generate content for every report section."""
    result = {}
    for key, title in SECTIONS:
        result[key] = generate_section(key, title, doc_map, session_id, settings)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — PDF generation (ReportLab)
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_styles():
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        name="NexoraTitle",
        fontSize=26, leading=32, spaceAfter=6,
        textColor=BRAND_PRIMARY, fontName="Helvetica-Bold",
        alignment=1,
    ))
    styles.add(ParagraphStyle(
        name="NexoraSubtitle",
        fontSize=13, leading=18, spaceAfter=4,
        textColor=BRAND_SECONDARY, fontName="Helvetica",
        alignment=1,
    ))
    styles.add(ParagraphStyle(
        name="NexoraMeta",
        fontSize=10, leading=14, spaceAfter=2,
        textColor=colors.HexColor("#888888"), fontName="Helvetica",
        alignment=1,
    ))
    styles.add(ParagraphStyle(
        name="NexoraH1",
        fontSize=16, leading=22, spaceBefore=18, spaceAfter=8,
        textColor=BRAND_PRIMARY, fontName="Helvetica-Bold",
    ))
    styles.add(ParagraphStyle(
        name="NexoraH2",
        fontSize=13, leading=18, spaceBefore=12, spaceAfter=6,
        textColor=BRAND_SECONDARY, fontName="Helvetica-Bold",
    ))
    styles.add(ParagraphStyle(
        name="NexoraBody",
        fontSize=10, leading=15, spaceAfter=8,
        textColor=BRAND_DARK, fontName="Helvetica",
    ))
    styles.add(ParagraphStyle(
        name="NexoraCallout",
        fontSize=10, leading=15, spaceAfter=8,
        textColor=colors.HexColor("#4C1D95"),
        backColor=BRAND_LIGHT,
        fontName="Helvetica",
        leftIndent=12, rightIndent=12,
        spaceBefore=6,
        borderPad=8,
    ))
    styles.add(ParagraphStyle(
        name="NexoraBullet",
        fontSize=10, leading=15, spaceAfter=4,
        textColor=BRAND_DARK, fontName="Helvetica",
        leftIndent=20, bulletIndent=8,
    ))
    return styles


def _add_page_number(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#999999"))
    page_num = canvas.getPageNumber()
    canvas.drawCentredString(letter[0] / 2, 0.5 * inch, f"Nexora.AI — Confidential Report  |  Page {page_num}")
    canvas.restoreState()


def _clean_text_for_pdf(text: str) -> str:
    """Strip markdown syntax so ReportLab doesn't choke."""
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.*?)\*", r"<i>\1</i>", text)
    text = re.sub(r"#+\s+", "", text)          # headings
    text = re.sub(r"_{2,}", "", text)           # horizontal rules
    text = text.replace("&", "&amp;").replace("<b>", "<b>").replace("</b>", "</b>")
    return text


def _section_to_pdf_flowables(title: str, body: str, styles) -> list:
    """Convert a section title + body text into ReportLab flowables."""
    flowables = []
    flowables.append(HRFlowable(width="100%", thickness=2, color=ACCENT_RULE, spaceAfter=6))
    flowables.append(Paragraph(title, styles["NexoraH1"]))
    flowables.append(Spacer(1, 6))

    for para in body.split("\n\n"):
        para = para.strip()
        if not para:
            continue

        # Numbered list items
        if re.match(r"^\d+\.", para):
            lines = para.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                cleaned = _clean_text_for_pdf(line)
                flowables.append(Paragraph(f"• {cleaned}", styles["NexoraBullet"]))
            flowables.append(Spacer(1, 4))
            continue

        # Sub-headings (markdown-style)
        if para.startswith("#"):
            cleaned = re.sub(r"#+\s+", "", para)
            flowables.append(Paragraph(cleaned, styles["NexoraH2"]))
            continue

        cleaned = _clean_text_for_pdf(para)
        flowables.append(Paragraph(cleaned, styles["NexoraBody"]))

    flowables.append(Spacer(1, 10))
    return flowables


def build_pdf_report(
    username: str,
    doc_map: dict[str, str],
    sections: dict[str, str],
    selected_docs: list[str],
) -> bytes:
    """Assemble the full PDF and return raw bytes."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.85 * inch,
        leftMargin=0.85 * inch,
        topMargin=1 * inch,
        bottomMargin=0.85 * inch,
    )
    styles = _pdf_styles()
    story  = []

    # ── Cover Page ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 1.2 * inch))
    story.append(Paragraph("NEXORA.AI", styles["NexoraTitle"]))
    story.append(Spacer(1, 0.15 * inch))
    story.append(HRFlowable(width="60%", thickness=2, color=BRAND_SECONDARY, hAlign="CENTER"))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph("Comprehensive Document Intelligence Report", styles["NexoraSubtitle"]))
    story.append(Spacer(1, 0.5 * inch))
    story.append(Paragraph(f"Prepared for: {username}", styles["NexoraMeta"]))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y  %H:%M')}", styles["NexoraMeta"]))
    story.append(Spacer(1, 0.3 * inch))

    # Document list table
    doc_names = list(doc_map.keys())
    table_data = [["#", "Document Name"]]
    for i, name in enumerate(doc_names, 1):
        table_data.append([str(i), name])

    tbl = Table(table_data, colWidths=[0.4 * inch, 5.6 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  BRAND_PRIMARY),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, BRAND_LIGHT]),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
        ("ALIGN",         (0, 0), (0, -1),  "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)
    story.append(PageBreak())

    # ── Report Sections ───────────────────────────────────────────────────────
    for key, title in SECTIONS:
        body = sections.get(key, "Section not generated.")
        story.extend(_section_to_pdf_flowables(title, body, styles))
        story.append(PageBreak())

    # ── Sources Footer ────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=2, color=ACCENT_RULE, spaceAfter=6))
    story.append(Paragraph("Source Documents", styles["NexoraH1"]))
    for i, name in enumerate(doc_names, 1):
        story.append(Paragraph(f"{i}. {name}", styles["NexoraBody"]))

    doc.build(story, onFirstPage=_add_page_number, onLaterPages=_add_page_number)
    return buffer.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — DOCX generation (python-docx)
# ─────────────────────────────────────────────────────────────────────────────

def _set_docx_heading_color(paragraph, hex_color: str):
    """Apply a custom colour to a heading paragraph's runs."""
    rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    for run in paragraph.runs:
        run.font.color.rgb = RGBColor(*rgb)


def _add_horizontal_rule(doc: DocxDocument):
    """Insert a thin coloured paragraph border as a visual divider."""
    p = doc.add_paragraph()
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "7C3AED")
    pBdr.append(bottom)
    pPr.append(pBdr)
    p.paragraph_format.space_after = Pt(4)


def _section_to_docx(doc: DocxDocument, title: str, body: str):
    """Write one section into the DOCX."""
    _add_horizontal_rule(doc)
    h = doc.add_heading(title, level=1)
    _set_docx_heading_color(h, "6C63FF")
    h.paragraph_format.space_before = Pt(14)

    for para_text in body.split("\n\n"):
        para_text = para_text.strip()
        if not para_text:
            continue

        # Sub-headings
        if para_text.startswith("#"):
            clean = re.sub(r"#+\s+", "", para_text)
            sh = doc.add_heading(clean, level=2)
            _set_docx_heading_color(sh, "A78BFA")
            continue

        # Numbered list lines
        if re.match(r"^\d+\.", para_text):
            for line in para_text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                p = doc.add_paragraph(style="List Bullet")
                # Strip leading "N." if present
                clean_line = re.sub(r"^\d+\.\s*", "", line)
                # Handle bold markdown
                parts = re.split(r"\*\*(.*?)\*\*", clean_line)
                for j, part in enumerate(parts):
                    run = p.add_run(part)
                    if j % 2 == 1:
                        run.bold = True
            continue

        # Normal paragraph
        p = doc.add_paragraph()
        parts = re.split(r"\*\*(.*?)\*\*", para_text)
        for j, part in enumerate(parts):
            run = p.add_run(part)
            run.font.size = Pt(10.5)
            if j % 2 == 1:
                run.bold = True

    doc.add_paragraph()  # breathing space


def build_docx_report(
    username: str,
    doc_map: dict[str, str],
    sections: dict[str, str],
    selected_docs: list[str],
) -> bytes:
    """Assemble the full DOCX and return raw bytes."""
    doc = DocxDocument()

    # ── Page setup ────────────────────────────────────────────────────────────
    section = doc.sections[0]
    section.page_width  = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin = section.right_margin = Inches(1)
    section.top_margin  = section.bottom_margin = Inches(1)

    # ── Cover ─────────────────────────────────────────────────────────────────
    cover_title = doc.add_heading("NEXORA.AI", 0)
    cover_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_docx_heading_color(cover_title, "6C63FF")

    sub = doc.add_paragraph("Comprehensive Document Intelligence Report")
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.runs[0].font.size = Pt(14)
    sub.runs[0].font.color.rgb = RGBColor(0xA7, 0x8B, 0xFA)

    doc.add_paragraph()

    meta1 = doc.add_paragraph(f"Prepared for: {username}")
    meta1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta1.runs[0].font.size = Pt(10)
    meta1.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    meta2 = doc.add_paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y  %H:%M')}")
    meta2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta2.runs[0].font.size = Pt(10)
    meta2.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    doc.add_paragraph()
    _add_horizontal_rule(doc)

    # Document index table
    doc_names = list(doc_map.keys())
    tbl = doc.add_table(rows=1, cols=2)
    tbl.style = "Table Grid"
    hdr = tbl.rows[0].cells
    hdr[0].text = "#"
    hdr[1].text = "Document Name"
    for cell in hdr:
        for run in cell.paragraphs[0].runs:
            run.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:fill"), "6C63FF")
        shd.set(qn("w:val"), "clear")
        cell._tc.tcPr.append(shd)

    for i, name in enumerate(doc_names, 1):
        row = tbl.add_row().cells
        row[0].text = str(i)
        row[1].text = name

    doc.add_page_break()

    # ── Sections ─────────────────────────────────────────────────────────────
    for key, title in SECTIONS:
        body = sections.get(key, "Section not generated.")
        _section_to_docx(doc, title, body)
        doc.add_page_break()

    # ── Sources ───────────────────────────────────────────────────────────────
    _add_horizontal_rule(doc)
    sh = doc.add_heading("Source Documents", level=1)
    _set_docx_heading_color(sh, "6C63FF")
    for i, name in enumerate(doc_names, 1):
        doc.add_paragraph(f"{i}. {name}", style="List Number")

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def generate_full_report(
    username: str,
    session_id: str,
    settings: dict,
    selected_docs: list[str],
    fmt: str = "pdf",
) -> tuple[bytes, str]:
    """
    Generate a full report for the given user's documents.

    Parameters
    ----------
    username      : logged-in user
    session_id    : current chat session (for LLM settings)
    settings      : model/temperature/system_prompt dict from get_session_settings()
    selected_docs : list of filenames to include (empty = all)
    fmt           : "pdf" or "docx"

    Returns
    -------
    (file_bytes, filename)
    """
    # 1. Pull document text from ChromaDB
    doc_map = fetch_document_chunks(username, selected_docs)
    if not doc_map:
        raise ValueError("No documents found. Please upload and index files first.")

    # 2. Generate all sections via LLM
    sections = generate_all_sections(doc_map, session_id, settings)

    # 3. Render to chosen format
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "docx":
        file_bytes = build_docx_report(username, doc_map, sections, selected_docs)
        filename   = f"nexora_report_{username}_{timestamp}.docx"
        mime       = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        file_bytes = build_pdf_report(username, doc_map, sections, selected_docs)
        filename   = f"nexora_report_{username}_{timestamp}.pdf"
        mime       = "application/pdf"

    return file_bytes, filename, mime
