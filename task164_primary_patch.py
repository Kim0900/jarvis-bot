"""Task #164 reversible production-primary patch."""

from __future__ import annotations

import os

def _enabled():
    return os.getenv("DAILY_HISTORY_LAYOUT_PRIMARY_ENABLED", "").lower() == "true"

def install(bot):
    if getattr(bot, "_task164_primary_patch_installed", False):
        return
    bot._task164_primary_patch_installed = True
