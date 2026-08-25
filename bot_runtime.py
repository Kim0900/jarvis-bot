"""Runtime entrypoint for jarvis-bot with AI provider fallback.

Render currently starts bot_v5.py directly. Running this module instead keeps
bot_v5.py intact, but wraps Anthropic SDK calls so provider-level failures can
fall back to Gemini instead of being retried against Anthropic forever.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx

logger = logging.getLogger(__name__)

_REAL_ANTHROPIC = anthropic.Anthropic
GEMINI_MODEL = os.getenv("GEMINI_MODEL", os.getenv("GEMINI_TEXT_MODEL", "gemini-1.5-flash"))


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


class _FallbackMessage:
    def __init__(self, text: str):
        self.content = [_TextBlock(text=text)]
        self.stop_reason = "end_turn"


def _is_provider_unavailable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
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
        )
    )


def _anthropic_part_to_gemini_part(part: Any) -> dict[str, Any] | None:
    if isinstance(part, str):
        return {"text": part}
    if not isinstance(part, dict):
        return None

    kind = part.get("type")
    if kind == "text":
        return {"text": part.get("text", "")}
    if kind == "image":
        source = part.get("source") or {}
        if source.get("type") != "base64":
            return None
        return {
            "inline_data": {
                "mime_type": source.get("media_type") or "image/jpeg",
                "data": source.get("data") or "",
            }
        }
    return None


def _anthropic_messages_to_gemini(messages: list[dict[str, Any]], system: Any = None) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []

    system_text = ""
    if isinstance(system, str):
        system_text = system.strip()
    elif isinstance(system, list):
        system_text = "\n".join(
            p.get("text", "") for p in system if isinstance(p, dict) and p.get("type") == "text"
        ).strip()

    for msg in messages or []:
        role = "model" if msg.get("role") == "assistant" else "user"
        raw_content = msg.get("content", "")
        raw_parts = raw_content if isinstance(raw_content, list) else [raw_content]
        parts = []
        for raw_part in raw_parts:
            converted = _anthropic_part_to_gemini_part(raw_part)
            if converted:
                parts.append(converted)
        if parts:
            contents.append({"role": role, "parts": parts})

    if system_text:
        if contents and contents[0]["role"] == "user":
            contents[0]["parts"].insert(0, {"text": f"[system]\n{system_text}\n"})
        else:
            contents.insert(0, {"role": "user", "parts": [{"text": f"[system]\n{system_text}"}]})

    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    return contents


def _gemini_generate(*, messages: list[dict[str, Any]], system: Any = None, max_tokens: int = 1000, temperature: float = 0) -> _FallbackMessage:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    payload = {
        "contents": _anthropic_messages_to_gemini(messages, system),
        "generationConfig": {
            "temperature": temperature or 0,
            "maxOutputTokens": max_tokens or 1000,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    with httpx.Client(timeout=120.0) as client:
        res = client.post(url, json=payload)
    if res.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {res.status_code}: {res.text[:500]}")

    data = res.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty text response")
    return _FallbackMessage(text)


class _MessagesProxy:
    def __init__(self, real_messages: Any):
        self._real_messages = real_messages

    def create(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._real_messages.create(*args, **kwargs)
        except Exception as exc:
            if not _is_provider_unavailable(exc):
                raise
            logger.warning("Anthropic unavailable; falling back to Gemini: %s", exc)
            return _gemini_generate(
                messages=kwargs.get("messages") or [],
                system=kwargs.get("system"),
                max_tokens=kwargs.get("max_tokens") or 1000,
                temperature=kwargs.get("temperature") or 0,
            )


class _AnthropicFallbackClient:
    def __init__(self, *args: Any, **kwargs: Any):
        self._real_client = _REAL_ANTHROPIC(*args, **kwargs)
        self.messages = _MessagesProxy(self._real_client.messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_client, name)


anthropic.Anthropic = _AnthropicFallbackClient

import bot_v5  # noqa: E402


if __name__ == "__main__":
    bot_v5.main()
