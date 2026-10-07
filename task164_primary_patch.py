"""Task #164 reversible production-primary patch."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

from daily_history_primary_adapter import (
    ACTION_FAIL_CLOSED,
    ACTION_FALLBACK_LEGACY,
    classify_layout_result,
    independent_header_disagreements,
    layout_to_daily_history,
    persist_layout_primary,
)

_IMAGE_LOCAL = threading.local()
_COMPLETED_IN_PRIMARY = set()
_COMPLETED_LOCK = threading.Lock()


def _enabled():
    return os.getenv("DAILY_HISTORY_LAYOUT_PRIMARY_ENABLED", "").lower() == "true"


def _mark_primary_completed(source_id):
    with _COMPLETED_LOCK:
        _COMPLETED_IN_PRIMARY.add(source_id)


def _consume_primary_completed(source_id):
    with _COMPLETED_LOCK:
        if source_id in _COMPLETED_IN_PRIMARY:
            _COMPLETED_IN_PRIMARY.remove(source_id)
            return True
    return False


async def _capture_layout(image_bytes):
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(image_bytes)
            path = handle.name
        helper = os.path.join(os.path.dirname(__file__), "task164_layout_capture.py")

        def _run():
            return subprocess.run(
                [sys.executable, helper, path],
                capture_output=True,
                text=True,
                timeout=55,
                check=False,
            )

        proc = await asyncio.to_thread(_run)
        if proc.returncode != 0:
            return {
                "transport_unavailable": True,
                "status": None,
                "payload": {},
                "error_code": "LAYOUT_CAPTURE_SUBPROCESS_FAILED",
            }
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        if not lines:
            return {
                "transport_unavailable": True,
                "status": None,
                "payload": {},
                "error_code": "LAYOUT_CAPTURE_EMPTY",
            }
        result = json.loads(lines[-1])
        if result.get("status") is None:
            return {
                "transport_unavailable": True,
                "status": None,
                "payload": result.get("payload") or {},
                "error_code": "LAYOUT_CAPTURE_NO_RESPONSE",
            }
        return {
            "transport_unavailable": False,
            "status": result.get("status"),
            "payload": result.get("payload") or {},
        }
    except subprocess.TimeoutExpired:
        return {
            "transport_unavailable": True,
            "status": None,
            "payload": {},
            "error_code": "LAYOUT_CAPTURE_TIMEOUT",
        }
    except Exception as exc:
        return {
            "transport_unavailable": True,
            "status": None,
            "payload": {},
            "error_code": f"LAYOUT_CAPTURE_{type(exc).__name__}",
        }
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _looks_like_daily_history(text):
    text = str(text or "")
    if "일별" in text and "운행" in text and "이력" in text:
        return True
    anchors = re.findall(
        r"(?<!\d)\d{1,2}:\d{2}\s*[-~–—]\s*\d{1,2}:\d{2}(?!\d)",
        text,
    )
    headerish = bool(
        re.search(r"\d{1,3}\s*건", text)
        and re.search(r"[\d,]{4,}\s*원", text)
    )
    return len(anchors) >= 2 and headerish


def _fail(source_id, error_code, **extra):
    return {
        "success": False,
        "format": "daily_history",
        "saved_count": 0,
        "source_id": source_id,
        "error": error_code,
        "error_code": error_code,
        "error_stage": "layout_primary",
        **extra,
    }


def install(bot):
    if getattr(bot, "_task164_primary_patch_installed", False):
        return

    original_ocr = bot.google_vision_ocr
    original_process = bot.process_and_save_call_document
    original_mark = bot.mark_call_image_ingestion

    async def wrapped_ocr(image_bytes):
        _IMAGE_LOCAL.value = bytes(image_bytes)
        return await original_ocr(image_bytes)

    async def wrapped_mark(source_id, status, fmt=None, inserted_count=0, last_error=None):
        if status in ("COMPLETED", "FAILED") and _consume_primary_completed(source_id):
            return {
                "ok": True,
                "task164_primary_already_completed": True,
                "ignored_status": status,
            }
        return await original_mark(
            source_id,
            status,
            fmt=fmt,
            inserted_count=inserted_count,
            last_error=last_error,
        )

    async def wrapped_process(text, source_id=None):
        if not _enabled():
            return await original_process(text, source_id=source_id)

        try:
            legacy_parsed = bot.detect_and_parse_call_document(text)
        except Exception:
            legacy_parsed = {"format": "unknown", "items": []}

        if (
            legacy_parsed.get("format") != "daily_history"
            and not _looks_like_daily_history(text)
        ):
            return await original_process(text, source_id=source_id)

        image_bytes = getattr(_IMAGE_LOCAL, "value", None)
        if not image_bytes:
            bot.logger.warning(
                "[TASK164_LAYOUT_PRIMARY] original image unavailable; compatibility fallback"
            )
            return await original_process(text, source_id=source_id)

        captured = await _capture_layout(image_bytes)
        action = classify_layout_result(
            captured.get("status"),
            captured.get("payload"),
            transport_error=bool(captured.get("transport_unavailable")),
        )
        if action == ACTION_FALLBACK_LEGACY:
            bot.logger.warning(
                "[TASK164_LAYOUT_PRIMARY] layout unavailable; compatibility fallback reason=%s",
                captured.get("error_code"),
            )
            return await original_process(text, source_id=source_id)

        layout = captured.get("payload") or {}
        if action == ACTION_FAIL_CLOSED:
            return _fail(
                source_id,
                layout.get("error_code") or captured.get("error_code") or "LAYOUT_PRIMARY_FAIL_CLOSED",
            )

        disagreements = independent_header_disagreements(layout, legacy_parsed)
        if disagreements:
            return _fail(
                source_id,
                "LAYOUT_INDEPENDENT_HEADER_DISAGREEMENT",
                disagreements=disagreements,
            )

        parsed = layout_to_daily_history(layout)

        async def select_rows(params):
            return await bot.sb_select("raw_calls", params)

        async def bulk_insert(payloads):
            return await bot.sb_h("POST", "raw_calls", json=payloads)

        async def mark_completed(saved_count):
            expected = int(parsed.get("표시건수") or 0)
            await bot.record_kakao_daily_page_evidence(
                source_id=source_id,
                page_date=parsed.get("날짜"),
                displayed_count=expected,
                covered_count=expected,
                inserted_count=int(saved_count or 0),
                duplicate_skipped_count=max(expected - int(saved_count or 0), 0),
                displayed_amount=parsed.get("표시금액"),
            )
            return await original_mark(
                source_id,
                "COMPLETED",
                fmt="daily_history",
                inserted_count=saved_count,
            )

        persist = await persist_layout_primary(
            parsed,
            source_id,
            select_rows=select_rows,
            bulk_insert=bulk_insert,
            mark_completed=mark_completed,
            rollback_source_rows=bot.delete_partial_source_calls,
            calc_service_date=bot.calc_service_date,
            validate_call_payload=bot.validate_call_payload,
        )
        if not persist.get("ok"):
            return _fail(
                source_id,
                persist.get("error_code") or "LAYOUT_PRIMARY_PERSIST_FAILED",
                quarantine=bool(persist.get("quarantine")),
                overlap_count=persist.get("overlap_count"),
                overlap_candidates=persist.get("overlap_candidates"),
            )

        _mark_primary_completed(source_id)
        legacy_items = legacy_parsed.get("items") or []
        try:
            bot.logger.info(
                "[TASK164_LAYOUT_PRIMARY] " +
                json.dumps({
                    "source_id": source_id,
                    "date": persist.get("date"),
                    "saved_count": persist.get("saved_count"),
                    "covered_count": persist.get("covered_count"),
                    "duplicate_skipped_count": persist.get("duplicate_skipped_count"),
                    "displayed_count": persist.get("displayed_count"),
                    "displayed_amount": persist.get("displayed_amount"),
                    "layout_ocr_calls": layout.get("actual_total_ocr_calls"),
                    "layout_wall_ms": layout.get("total_wall_duration_ms"),
                    "legacy_count": len(legacy_items),
                    "legacy_sum": sum(int(x.get("요금") or 0) for x in legacy_items),
                    "fallback_used": False,
                }, ensure_ascii=False, separators=(",", ":"))
            )
        except Exception:
            pass
        return {
            "success": True,
            "format": "daily_history",
            "saved_count": persist.get("saved_count"),
            "covered_count": persist.get("covered_count"),
            "duplicate_skipped_count": persist.get("duplicate_skipped_count"),
            "source_id": source_id,
            "layout_primary": True,
            "displayed_count": persist.get("displayed_count"),
            "displayed_amount": persist.get("displayed_amount"),
            "layout_ocr_calls": layout.get("actual_total_ocr_calls"),
            "layout_wall_ms": layout.get("total_wall_duration_ms"),
        }

    bot.google_vision_ocr = wrapped_ocr
    bot.process_and_save_call_document = wrapped_process
    bot.mark_call_image_ingestion = wrapped_mark
    bot._task164_primary_patch_installed = True
