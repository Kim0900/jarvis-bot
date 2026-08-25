"""Render entrypoint for Jarvis v5.

The full bot implementation lives in bot_v5_legacy.py. This thin wrapper keeps
Render's existing `python bot_v5.py` start command working while installing the
AI provider fallback before the legacy bot imports and uses Anthropic.
"""

from __future__ import annotations

import logging

import sitecustomize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

try:
    sitecustomize._install_ai_fallback()
except Exception as exc:
    logger.warning("MAGI AI fallback wrapper install failed: %s", exc)

import bot_v5_legacy


if __name__ == "__main__":
    bot_v5_legacy.main()
