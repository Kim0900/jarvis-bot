"""Task #164 reversible production-primary patch."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from contextvars import ContextVar

from daily_history_primary_adapter import (
    ACTION_FAIL_CLOSED,
    ACTION_FALLBACK_LEGACY,
    classify_layout_result,
    independent_header_disagreements,
    layout_to_daily_history,
    persist_layout_primary,
)

_CURRENT_IMAGE = ContextVar("task164_current_image", default=None)
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


def install(bot):
    if getattr(bot, "_task164_primary_patch_installed", False):
        return

    original_ocr = bot.google_vision_ocr
    original_process = bot.process_and_save_call_document
    original_mark = bot.mark_call_image_ingestion

    async def wrapped_ocr(image_bytes):
        _CURRENT_IMAGE.set(bytes(image_bytes))
        return await original_ocr(image_bytes)

    async def wrapped_mark(source_id, status, fmt=None, inserted_count=0, last_error=None):
        if status == "COMPLETED" and _consume_primary_completed(source_id):
            return {"ok": True, "task164_primary_already_completed": True}
        return await original_mark(
            source_id,
            status,
            fmt=fmt,
            inserted_count=inserted_count,
            last_error=last_error,
        )

    bot.google_vision_ocr = wrapped_ocr
    bot.mark_call_image_ingestion = wrapped_mark
    bot._task164_original_process = original_process
    bot._task164_primary_patch_installed = True
