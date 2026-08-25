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


def _truthy_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.1f", name, raw, default)
        return default


def _install_ai_fallback() -> None:
    if not _truthy_env("MAGI_AI_FALLBACK_ENABLED", True):
        logger.warning("MAGI AI fallback disabled by MAGI_AI_FALLBACK_ENABLED")
        return

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

    gemini_safe_mode = _truthy_env("MAGI_GEMINI_FREE_TIER_SAFE_MODE", True)
    gemini_min_interval = max(_float_env("MAGI_GEMINI_MIN_INTERVAL_SECONDS", 65.0), 0.0)
    gemini_last_call_at = 0.0
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

    def wait_for_free_tier_window() -> None:
        nonlocal gemini_last_call_at
        if not gemini_safe_mode or gemini_min_interval <= 0:
            return
        elapsed = time.monotonic() - gemini_last_call_at
        delay = gemini_min_interval - elapsed
        if delay > 0:
            logger.warning("Gemini free-tier safe mode; waiting %.1fs before next call", delay)
            time.sleep(delay)
        gemini_last_call_at = time.monotonic()

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
                wait_for_free_tier_window()
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
    if gemini_safe_mode:
        logger.warning(
            "MAGI AI fallback installed: Anthropic provider failures will route to Gemini with free-tier safe mode %.1fs",
            gemini_min_interval,
        )
    else:
        logger.warning("MAGI AI fallback installed: Anthropic provider failures will route to Gemini")


def _install_scheduler_dispatch_patch(legacy: Any) -> None:
    if getattr(legacy, "_magi_scheduler_dispatch_patched", False):
        return

    def fish_scheduler(app: Any) -> None:
        loop = legacy.asyncio.new_event_loop()
        legacy.asyncio.set_event_loop(loop)

        chat_ids = [
            x for x in [
                os.getenv("ALLOWED_CHAT_ID", ""),
                os.getenv("ALLOWED_CHAT_ID2", ""),
            ] if x
        ]

        async def send_all(text: str) -> None:
            for cid in chat_ids:
                try:
                    await app.bot.send_message(chat_id=cid, text=text)
                except Exception as exc:
                    legacy.logger.error(f"어군 브리핑 발송 오류 ({cid}): {exc}")

        sent_hour_keys: set[str] = set()
        sent_start_brief_key: str | None = None
        last_reset_day = -1
        last_recalc_day = -1
        last_dualverify_day = -1
        last_kpi7day = -1
        last_orch_run_ts = 0.0
        last_magi_review_ts = 0.0

        try:
            loop.run_until_complete(legacy.recalc_fish_hour_data())
        except Exception as exc:
            legacy.logger.error(f"fish_hour_data 최초 재계산 실패: {exc}")

        while True:
            try:
                now = legacy.datetime.now(legacy.KST)
                hour_key = f"{now.date()}:{now.hour:02d}"

                if now.hour == 18 and now.minute == 50 and sent_start_brief_key != str(now.date()):
                    try:
                        report = loop.run_until_complete(legacy.get_fish_report_db(hour=19)) or "데이터 없음"
                        loop.run_until_complete(send_all(f"🚀 영업준비 브리핑 (10분 후 출발)\n\n{report}"))
                        sent_start_brief_key = str(now.date())
                        legacy.logger.info("18:50 영업준비 브리핑 발송")
                    except Exception as exc:
                        legacy.logger.error(f"18:50 영업준비 브리핑 오류: {exc}")

                # Dispatch must run before long AI jobs. Haiku/Gemini retries can block past minute 0.
                if now.minute < 3 and hour_key not in sent_hour_keys:
                    in_service = (19 <= now.hour <= 23) or (0 <= now.hour <= 2)
                    if in_service:
                        try:
                            report = loop.run_until_complete(legacy.get_fish_report_db())
                            if report:
                                loop.run_until_complete(send_all(report))
                                legacy.logger.info(f"어군 브리핑 발송: {now.hour}시")
                        except Exception as exc:
                            legacy.logger.error(f"{now.hour}시 정각 브리핑 오류: {exc}")
                        finally:
                            sent_hour_keys.add(hour_key)

                if time.time() - last_orch_run_ts >= 300:
                    last_orch_run_ts = time.time()
                    try:
                        tid = loop.run_until_complete(legacy.run_haiku_orchestration_once())
                        loop.run_until_complete(legacy.mark_scheduler_run("run_haiku_orchestration_once", f"task_id={tid}" if tid else "no_task"))
                    except Exception as exc:
                        legacy.logger.error(f"Haiku오케스트레이션 실행 실패: {exc}")

                if time.time() - last_magi_review_ts >= 300:
                    last_magi_review_ts = time.time()
                    try:
                        tid2 = loop.run_until_complete(legacy.run_magi_auto_review_once())
                        loop.run_until_complete(legacy.mark_scheduler_run("run_magi_auto_review_once", f"task_id={tid2}" if tid2 else "no_task"))
                    except Exception as exc:
                        legacy.logger.error(f"마기(자동)검증 실행 실패: {exc}")

                if now.hour == 4 and now.day != last_kpi7day:
                    try:
                        loop.run_until_complete(legacy.recalc_daily_summary_totals())
                        loop.run_until_complete(legacy.mark_scheduler_run("recalc_daily_summary_totals"))
                    except Exception as exc:
                        legacy.logger.error(f"daily_summary 자동갱신 실패: {exc}")
                        loop.run_until_complete(legacy.mark_scheduler_run("recalc_daily_summary_totals", f"FAIL: {exc}"))
                    try:
                        loop.run_until_complete(legacy.recalc_7day_average())
                        loop.run_until_complete(legacy.mark_scheduler_run("recalc_7day_average"))
                    except Exception as exc:
                        legacy.logger.error(f"7일평균(명령서#035) 재계산 실패: {exc}")
                        loop.run_until_complete(legacy.mark_scheduler_run("recalc_7day_average", f"FAIL: {exc}"))
                    try:
                        loop.run_until_complete(legacy.calc_daily_snapshot())
                        loop.run_until_complete(legacy.mark_scheduler_run("calc_daily_snapshot"))
                    except Exception as exc:
                        legacy.logger.error(f"daily_calc_snapshot(명령서#036) 계산 실패: {exc}")
                        loop.run_until_complete(legacy.mark_scheduler_run("calc_daily_snapshot", f"FAIL: {exc}"))
                    last_kpi7day = now.day

                if now.hour == 8 and now.day != last_dualverify_day:
                    try:
                        loop.run_until_complete(legacy.check_ingestion_gap())
                        loop.run_until_complete(legacy.mark_scheduler_run("check_ingestion_gap"))
                    except Exception as exc:
                        legacy.logger.error(f"인입중단 감지 실패: {exc}")
                    try:
                        asked = loop.run_until_complete(legacy.ask_operated_status_telegram())
                        loop.run_until_complete(legacy.mark_scheduler_run("ask_operated_status_telegram", f"asked={len(asked)}"))
                    except Exception as exc:
                        legacy.logger.error(f"operated_status 질문발송 실패: {exc}")
                    try:
                        dv = loop.run_until_complete(legacy.dual_verify_7day_average())
                        last_dualverify_day = now.day
                        loop.run_until_complete(legacy.mark_scheduler_run("dual_verify_7day_average", "OK" if dv["match"] else "MISMATCH"))
                        if not dv["match"]:
                            msg = (
                                f"⚠️ 7일평균 이중검증 불일치 발견 ({dv['date_range']})\n"
                                f"방식A(raw_calls 직접집계): {dv['method_a']['총매출']:,}원 (일평균 {dv['method_a']['일평균']:,.0f}원)\n"
                                f"방식B(daily_summary): {dv['method_b']['총매출']:,}원 (일평균 {dv['method_b']['일평균']:,.0f}원)\n"
                                f"차이: {dv['diff']:+,}원\n"
                                + "\n".join(dv.get("detail", []))
                            )
                            legacy.logger.warning(f"명령서#028 갭3 검증 불일치: {msg}")
                            loop.run_until_complete(send_all(msg))
                        else:
                            legacy.logger.info(f"명령서#028 갭3 검증 통과 (일치, {dv['method_a']['총매출']:,}원)")
                    except Exception as exc:
                        legacy.logger.error(f"7일평균 이중검증 실행 오류: {exc}")

                if now.hour == 3 and now.day != last_reset_day:
                    sent_start_brief_key = None
                    sent_hour_keys = {key for key in sent_hour_keys if not key.startswith(str(now.date()))}
                    last_reset_day = now.day
                    legacy.logger.info("어군 스케줄러 일간 리셋")

                if now.hour == 3 and now.minute >= 10 and now.day != last_recalc_day:
                    try:
                        loop.run_until_complete(legacy.recalc_fish_hour_data())
                        legacy._FISH_HOUR_CACHE = {}
                        legacy.logger.info("fish_hour_data 일일 재계산 완료")
                    except Exception as exc:
                        legacy.logger.error(f"fish_hour_data 일일 재계산 실패: {exc}")
                    try:
                        loop.run_until_complete(legacy.recalc_fish_hour_data_dow())
                        legacy.logger.info("fish_hour_data_dow 일일 재계산 완료")
                    except Exception as exc:
                        legacy.logger.error(f"fish_hour_data_dow 일일 재계산 실패: {exc}")
                    last_recalc_day = now.day

            except Exception as exc:
                legacy.logger.error(f"fish_scheduler 최외곽 예외 포착(스레드 생존): {exc}")

            time.sleep(30)

    legacy.fish_scheduler = fish_scheduler
    legacy._magi_scheduler_dispatch_patched = True
    logger.warning("MAGI fish scheduler dispatch patch installed: hourly briefing runs before long AI jobs")


try:
    _install_ai_fallback()
except Exception as exc:
    logger.warning("AI fallback installation failed and was skipped: %s", exc)
