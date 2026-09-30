"""Task #164 reversible production-primary patch."""

from __future__ import annotations

import base64
import json
import os
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


def install(bot):
    if getattr(bot, "_task164_primary_patch_installed", False):
        return
    bot._task164_primary_patch_installed = True
