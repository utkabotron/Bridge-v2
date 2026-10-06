"""Media analysis: image (vision), audio (speech-to-text + translation), document (pypdf).

All functions return the analysis text or raise on failure. Model calls go through
src/llm.py, which shapes the request for the model family and records the cost.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

from . import llm
from .config import (
    AUDIO_ANALYSIS_TIMEOUT,
    DIRECT_MODEL,
    DOCUMENT_ANALYSIS_TIMEOUT,
    IMAGE_ANALYSIS_TIMEOUT,
    TRANSCRIBE_MODEL,
)

logger = logging.getLogger(__name__)


# ── Image analysis (vision) ──────────────────────────────

async def analyze_image(image_bytes: bytes, mime: str, target_lang: str) -> str:
    """Translate text in image or describe if no text."""
    b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:{mime};base64,{b64}"

    system_prompt = (
        f"You are a professional translator. Translate ALL text visible in this image into {target_lang}.\n\n"
        f"Rules:\n"
        f"1. Chat screenshots: for EACH message use this exact format (name and translation on SEPARATE lines):\n"
        f"**Sender name**\n"
        f"translated text\n\n"
        f"This separates RTL and LTR text to avoid broken layout. Translate EVERY message, do not skip any.\n"
        f"2. Other images with text (articles, signs, menus, documents): translate all text, "
        f"preserving the original structure (headings, paragraphs, labels, lists).\n"
        f"3. Output ONLY the translated text — no comments, notes, or metadata.\n"
        f"4. Preserve names, phone numbers, URLs, emojis, and numbers exactly as written.\n"
        f"5. Maintain the natural, conversational tone and register; adapt formality to match {target_lang}.\n"
        f"6. Translate the FULL content — no omissions. Double-check that every part is included.\n"
        f"7. Preserve idiomatic nuance; the result must read naturally in {target_lang}.\n"
        f"8. If the image has no text, provide a brief description in {target_lang} (1-2 sentences)."
    )

    user_text = (
        f"This is an image. Translate all text to {target_lang}. "
        f"If it's a chat screenshot, put each sender name on its own line (bold) "
        f"and the translation on the next line. Never mix name and translation on the same line."
    )

    result = await llm.chat(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        model=DIRECT_MODEL, purpose="image", max_tokens=2000, timeout=IMAGE_ANALYSIS_TIMEOUT,
    )
    return result.text


# ── Audio transcription (speech-to-text + translation) ───

async def transcribe_audio(audio_bytes: bytes, filename: str, target_lang: str) -> str:
    """Transcribe audio, then translate if needed."""
    transcript = await llm.transcribe(
        audio_bytes, filename, model=TRANSCRIBE_MODEL, timeout=AUDIO_ANALYSIS_TIMEOUT,
    )
    if not transcript:
        return "(empty audio)"

    result = await llm.chat(
        [
            {
                "role": "system",
                "content": (
                    f"Translate the following audio transcription to {target_lang}. "
                    f"Keep the original text first, then provide the translation."
                ),
            },
            {"role": "user", "content": transcript},
        ],
        model=DIRECT_MODEL, purpose="voice_translate", max_tokens=1000,
        timeout=AUDIO_ANALYSIS_TIMEOUT,
    )
    return result.text


# ── Document analysis (PDF/text) ─────────────────────────

async def analyze_document(
    doc_bytes: bytes, filename: str, mime: str, target_lang: str,
) -> str:
    """Extract text from document and summarize+translate."""
    text = _extract_document_text(doc_bytes, filename, mime)
    if not text:
        return "(could not extract text from document)"

    # Truncate to avoid token limits
    max_chars = 15000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(truncated)"

    result = await llm.chat(
        [
            {
                "role": "system",
                "content": (
                    f"Translate the following document content to {target_lang}, "
                    f"preserving the original structure and order. "
                    f"After the translation, add a brief summary (2-3 sentences) in {target_lang}. "
                    f"Do NOT include the original language text — only {target_lang}."
                ),
            },
            {"role": "user", "content": text},
        ],
        model=DIRECT_MODEL, purpose="document", max_tokens=3000, timeout=DOCUMENT_ANALYSIS_TIMEOUT,
    )
    return result.text


def _extract_document_text(doc_bytes: bytes, filename: str, mime: str) -> Optional[str]:
    """Extract text from PDF or text-based documents."""
    if mime == "application/pdf" or filename.lower().endswith(".pdf"):
        return _extract_pdf_text(doc_bytes)

    # Text-based files
    text_mimes = {
        "text/plain", "text/csv", "text/html", "text/xml",
        "application/json", "application/xml",
    }
    if mime in text_mimes or mime.startswith("text/"):
        try:
            return doc_bytes.decode("utf-8", errors="replace")
        except Exception:
            return None

    return None


def _extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from PDF using pypdf."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text = []
        for page in reader.pages[:50]:  # limit pages
            text = page.extract_text()
            if text:
                pages_text.append(text)
        return "\n\n".join(pages_text) if pages_text else None
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return None
