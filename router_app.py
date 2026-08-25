"""Runtime AI provider router for the OCR backend.

This module keeps the existing ocr_backend.py code intact, but replaces its
Claude-only claude_vision function before Gunicorn exposes the Flask app.
"""

import base64
import io
import logging
import os
from typing import Callable

import requests

import ocr_backend

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_VISION_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
GEMINI_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-1.5-flash")


def _prepare_image(raw: bytes) -> tuple[bytes, str]:
    """Normalize uploaded images to JPEG, matching the existing backend behavior."""
    try:
        img = ocr_backend.Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        max_h = 3000
        if img.height > max_h:
            ratio = max_h / img.height
            img = img.resize((int(img.width * ratio), max_h), ocr_backend.Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning("router image normalization failed, using original: %s", exc)
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return raw, "image/png"
        if raw[:4] in (b"RIFF", b"WEBP"):
            return raw, "image/webp"
        return raw, "image/jpeg"


def _extract_anthropic_text(data: dict) -> str:
    blocks = data.get("content") or []
    return "".join(block.get("text", "") for block in blocks if block.get("type") == "text").strip()


def _is_provider_unavailable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in (
        "credit balance is too low",
        "insufficient_credit",
        "insufficient credit",
        "rate limit",
        "rate_limit",
        "overloaded",
        "timeout",
        "timed out",
        "429",
        "529",
        "502",
        "503",
        "504",
    ))


def _call_anthropic(image_bytes: bytes, media_type: str, prompt: str, max_tokens: int) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError("Anthropic API key is not configured")

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json=payload,
        timeout=60,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Anthropic HTTP {res.status_code}: {res.text[:500]}")
    text = _extract_anthropic_text(res.json())
    if not text:
        raise RuntimeError("Anthropic returned an empty text response")
    return text


def _call_openai(image_bytes: bytes, media_type: str, prompt: str, max_tokens: int) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI API key is not configured")

    data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode()}"
    payload = {
        "model": OPENAI_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    res = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"OpenAI HTTP {res.status_code}: {res.text[:500]}")
    data = res.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not text:
        raise RuntimeError("OpenAI returned an empty text response")
    return text.strip()


def _call_gemini(image_bytes: bytes, media_type: str, prompt: str, max_tokens: int) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Gemini API key is not configured")

    payload = {
        "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": media_type, "data": base64.b64encode(image_bytes).decode()}},
            ],
        }],
    }
    res = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
        json=payload,
        timeout=60,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {res.status_code}: {res.text[:500]}")
    data = res.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty text response")
    return text


def routed_vision(image_bytes: bytes, prompt: str, max_tokens: int = 500) -> str:
    prepared, media_type = _prepare_image(image_bytes)
    providers: list[tuple[str, Callable[[bytes, str, str, int], str]]] = [
        ("anthropic", _call_anthropic),
        ("openai", _call_openai),
        ("gemini", _call_gemini),
    ]
    last_error: Exception | None = None

    for idx, (name, fn) in enumerate(providers):
        try:
            text = fn(prepared, media_type, prompt, max_tokens)
            if idx > 0:
                logger.warning("AI OCR fallback succeeded via %s after %s", name, last_error)
            return text
        except Exception as exc:
            last_error = exc
            if name == "anthropic" and _is_provider_unavailable(exc):
                logger.warning("Anthropic OCR unavailable, trying fallback: %s", exc)
                continue
            if name != "anthropic":
                logger.warning("AI OCR provider %s failed: %s", name, exc)
                continue
            raise

    raise RuntimeError(f"All OCR AI providers failed: {last_error}")


ocr_backend.claude_vision = routed_vision
app = ocr_backend.app
