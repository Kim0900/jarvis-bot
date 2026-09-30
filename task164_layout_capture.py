"""Capture full layout response by reusing the authenticated shadow request."""

import asyncio
import json
import sys

import bot_v5_legacy as bot

_CAPTURE = {}
_OriginalClient = bot.httpx.AsyncClient


class _CaptureClient:
    def __init__(self, *args, **kwargs):
        self._inner = _OriginalClient(*args, **kwargs)

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._inner.__aexit__(exc_type, exc, tb)

    async def post(self, *args, **kwargs):
        response = await self._inner.post(*args, **kwargs)
        _CAPTURE["status"] = response.status_code
        try:
            _CAPTURE["payload"] = response.json()
        except Exception:
            _CAPTURE["payload"] = {"ok": False, "error_code": "NON_JSON_RESPONSE"}
        return response


async def _main(path):
    with open(path, "rb") as handle:
        image_bytes = handle.read()
    bot.httpx.AsyncClient = _CaptureClient
    await bot._run_daily_history_layout_shadow(image_bytes, "task164-primary", {})
    print(json.dumps(_CAPTURE, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1]))
