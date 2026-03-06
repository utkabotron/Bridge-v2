"""Media analysis: image (GPT vision), audio (Whisper), document (pypdf).

All functions return the analysis text or raise on failure.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = "https://api.openai.com/v1"


def _headers() -> dict:
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"}


# ── Image analysis (GPT-4.1-mini vision) ────────────────

async def analyze_image(image_bytes: bytes, mime: str, target_lang: str) -> str:
    """Describe image content and translate description to target_lang."""
    b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:{mime};base64,{b64}"

    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are a helpful assistant. Describe the image content concisely. "
                    f"First provide the description in the original language of any text visible, "
                    f"then provide a translation/description in {target_lang}. "
                    f"If no text is visible, just describe what you see in {target_lang}."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "Describe this image."},
                ],
            },
        ],
        "max_tokens": 1000,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{OPENAI_BASE}/chat/completions", json=payload, headers=_headers())
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()


# ── Audio transcription (Whisper + translation) ──────────

async def transcribe_audio(audio_bytes: bytes, filename: str, target_lang: str) -> str:
    """Transcribe audio via Whisper, then translate if needed."""
    # Step 1: Whisper transcription
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{OPENAI_BASE}/audio/transcriptions",
            headers=_headers(),
            data={"model": "whisper-1"},
            files={"file": (filename, audio_bytes)},
        )
        r.raise_for_status()
        transcript = r.json().get("text", "").strip()

    if not transcript:
        return "(empty audio)"

    # Step 2: Translate transcript via LLM
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    f"Translate the following audio transcription to {target_lang}. "
                    f"Keep the original text first, then provide the translation."
                ),
            },
            {"role": "user", "content": transcript},
        ],
        "max_tokens": 1000,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{OPENAI_BASE}/chat/completions", json=payload, headers=_headers())
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()


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

    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    f"Summarize the following document content concisely. "
                    f"Provide the summary in the document's original language, "
                    f"then provide a translation/summary in {target_lang}."
                ),
            },
            {"role": "user", "content": text},
        ],
        "max_tokens": 1500,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{OPENAI_BASE}/chat/completions", json=payload, headers=_headers())
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()


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
