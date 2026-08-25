"""Autoload AI provider fallback for the Render bot process.

Python imports this file automatically at interpreter startup when it is present
on sys.path. Render currently runs `python bot_v5.py`, so this hook installs the
Anthropic -> Gemini fallback without requiring a Render start-command change.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _install_ai_fallback() -> None:
    try:
        import anthropic
        import httpx
    except Exception as exc:
        logger.warning("AI fallback not installed; dependency import failed: %s", exc)
        return

    real_anthropic = getattr(anthropic, "Anthropic", None)
    if real_anthropic is None:
        logger.warning("AI fallback not installed; anthropic.Anthropic is missing")
        return
    if getattr(real_anthropic, "_magi_fallback_wrapped", False):
        return

    gemini_model = os.getenv("GEMINI_MODEL") or os.getenv("GEMINI_TEXT_MODEL") or "gemini-3.6-flash"
    if gemini_model in ("gemini-1.5-flash", "gemini-2.0-flash"):
        gemini_model = "gemini-3.6-flash"

    tool_id_to_name: dict[str, str] = {}

    @dataclass
    class TextBlock:
        text: str
        type: str = "text"

    @dataclass
    class ToolUseBlock:
        id: str
        name: str
        input: dict[str, Any]
        thought_signature: str | None = None
        type: str = "tool_use"

    class FallbackMessage:
        def __init__(self, blocks: list[Any], stop_reason: str = "end_turn"):
            self.content = blocks
            self.stop_reason = stop_reason

    def is_provider_unavailable(exc: Exception) -> bool:
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

    def anthropic_part_to_gemini_part(part: Any) -> dict[str, Any] | None:
        if isinstance(part, str):
            return {"text": part}
        if not isinstance(part, dict):
            block_type = getattr(part, "type", None)
            if block_type == "text":
                return {"text": getattr(part, "text", "")}
            if block_type == "tool_use":
                converted = {"functionCall": {"name": getattr(part, "name", ""), "args": getattr(part, "input", {}) or {}}}
                thought_signature = getattr(part, "thought_signature", None)
                if thought_signature:
                    converted["thoughtSignature"] = thought_signature
                return converted
            return None

        kind = part.get("type")
        if kind == "text":
            return {"text": part.get("text", "")}
        if kind == "tool_result":
            tool_name = tool_id_to_name.get(part.get("tool_use_id", "")) or "tool_result"
            return {
                "functionResponse": {
                    "name": tool_name,
                    "response": {"content": part.get("content", "")},
                }
            }
        if kind == "image":
            source = part.get("source") or {}
            if source.get("type") != "base64":
                return None
            return {
                "inlineData": {
                    "mimeType": source.get("media_type") or "image/jpeg",
                    "data": source.get("data") or "",
                }
            }
        return None

    def anthropic_messages_to_gemini(messages: list[dict[str, Any]], system: Any = None) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []

        system_text = ""
        if isinstance(system, str):
            system_text = system.strip()
        elif isinstance(system, list):
            system_text = "\n".join(
                p.get("text", "") for p in system if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        if system_text:
            system_text = (
                f"{system_text}\n\n"
                "[Gemini fallback SQL rule]\n"
                "When calling query_supabase, send exactly one SELECT statement, with no trailing semicolon and no second statement. "
                "Use real schema names from errors/results. For bot_briefings, the type column is briefing_type, not brief_type."
            )

        for msg in messages or []:
            role = "model" if msg.get("role") == "assistant" else "user"
            raw_content = msg.get("content", "")
            raw_parts = raw_content if isinstance(raw_content, list) else [raw_content]
            parts: list[dict[str, Any]] = []
            for raw_part in raw_parts:
                converted = anthropic_part_to_gemini_part(raw_part)
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

    def anthropic_tools_to_gemini(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        declarations = []
        for tool in tools or []:
            name = tool.get("name")
            if not name:
                continue
            declaration = {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            }
            declarations.append(declaration)
        if not declarations:
            return None
        return [{"functionDeclarations": declarations}]

    def normalize_tool_args(name: str, args: Any) -> dict[str, Any]:
        if not isinstance(args, dict):
            return {}
        if name != "query_supabase":
            return args

        normalized = dict(args)
        sql = normalized.get("sql")
        if isinstance(sql, str):
            sql = sql.strip().replace("brief_type", "briefing_type")
            if ";" in sql:
                sql = sql.split(";", 1)[0].strip()
            normalized["sql"] = sql
        return normalized

    def gemini_parts_to_blocks(parts: list[dict[str, Any]]) -> list[Any]:
        blocks: list[Any] = []
        for idx, part in enumerate(parts or []):
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if text:
                blocks.append(TextBlock(text=text))
            function_call = part.get("functionCall") or part.get("function_call")
            if function_call:
                name = function_call.get("name", "")
                args = normalize_tool_args(name, function_call.get("args") or {})
                thought_signature = part.get("thoughtSignature") or part.get("thought_signature")
                tool_id = f"gemini_tool_{len(tool_id_to_name) + idx + 1}"
                tool_id_to_name[tool_id] = name
                blocks.append(ToolUseBlock(id=tool_id, name=name, input=args, thought_signature=thought_signature))
        return blocks

    def retry_delay_seconds(response_text: str, attempt: int) -> float:
        match = re.search(r"retry in ([0-9.]+)s", response_text, re.IGNORECASE)
        if match:
            return min(max(float(match.group(1)) + 1.0, 1.0), 90.0)
        return min(2.0 ** attempt, 30.0)

    def gemini_generate(
        *,
        messages: list[dict[str, Any]],
        system: Any = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1000,
        temperature: float = 0,
    ) -> FallbackMessage:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        payload: dict[str, Any] = {
            "contents": anthropic_messages_to_gemini(messages, system),
            "generationConfig": {
                "temperature": temperature or 0,
                "maxOutputTokens": max_tokens or 1000,
            },
        }
        gemini_tools = anthropic_tools_to_gemini(tools)
        if gemini_tools:
            payload["tools"] = gemini_tools

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent"
        with httpx.Client(timeout=120.0) as client:
            res = None
            for attempt in range(4):
                res = client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
                if res.status_code != 429 or attempt == 3:
                    break
                delay = retry_delay_seconds(res.text, attempt)
                logger.warning("Gemini rate limited; retrying in %.1fs", delay)
                time.sleep(delay)
        if res is None or res.status_code >= 400:
            status_code = getattr(res, "status_code", "unknown")
            text = getattr(res, "text", "")
            raise RuntimeError(f"Gemini HTTP {status_code}: {text[:500]}")

        data = res.json()
        candidate = (data.get("candidates") or [{}])[0]
        parts = candidate.get("content", {}).get("parts", [])
        blocks = gemini_parts_to_blocks(parts)
        if not blocks:
            finish_reason = candidate.get("finishReason") or candidate.get("finish_reason") or "unknown"
            raise RuntimeError(f"Gemini returned no text/tool blocks; finish_reason={finish_reason}")
        stop_reason = "tool_use" if any(getattr(block, "type", None) == "tool_use" for block in blocks) else "end_turn"
        return FallbackMessage(blocks, stop_reason=stop_reason)

    class MessagesProxy:
        def __init__(self, real_messages: Any):
            self._real_messages = real_messages
            self._fallback_active = False

        def _gemini_create(self, kwargs: dict[str, Any]) -> Any:
            return gemini_generate(
                messages=kwargs.get("messages") or [],
                system=kwargs.get("system"),
                tools=kwargs.get("tools") or [],
                max_tokens=kwargs.get("max_tokens") or 1000,
                temperature=kwargs.get("temperature") or 0,
            )

        def create(self, *args: Any, **kwargs: Any) -> Any:
            if self._fallback_active:
                return self._gemini_create(kwargs)
            try:
                return self._real_messages.create(*args, **kwargs)
            except Exception as exc:
                if not is_provider_unavailable(exc):
                    raise
                self._fallback_active = True
                logger.warning("Anthropic unavailable; falling back to Gemini: %s", exc)
                return self._gemini_create(kwargs)

    class AnthropicFallbackClient:
        _magi_fallback_wrapped = True

        def __init__(self, *args: Any, **kwargs: Any):
            self._real_client = real_anthropic(*args, **kwargs)
            self.messages = MessagesProxy(self._real_client.messages)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real_client, name)

    anthropic.Anthropic = AnthropicFallbackClient
    logger.warning("MAGI AI fallback installed: Anthropic provider failures will route to Gemini")


try:
    _install_ai_fallback()
except Exception as exc:
    logger.warning("AI fallback installation failed and was skipped: %s", exc)
