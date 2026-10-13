"""Task #194 review-only durable Telegram outbox.

Prerequisites before enabling TASK194_OUTBOX_ENABLED=1:
  (1) reviewed migration task194_notification_outbox_review_only.sql applied;
  (2) Render SUPABASE_KEY independently proven to be service_role (not anon);
  (3) CASSANDRA tests concurrency, crash, 403, unknown delivery and rollout/rollback.
Never claim exactly-once: Telegram sendMessage has no atomic transaction with DB ACK.
"""
from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar
from datetime import date, timedelta
from typing import Any

import httpx


def enabled() -> bool:
    return os.getenv("TASK194_OUTBOX_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def configured_recipients() -> list[str]:
    return list(dict.fromkeys(
        x for x in (os.getenv("ALLOWED_CHAT_ID", ""), os.getenv("ALLOWED_CHAT_ID2", "")) if x
    ))


def dedupe_key(job_name: str, service_date: str, recipient: str) -> str:
    return hashlib.sha256(f"{job_name}|{service_date}|{recipient}".encode("utf-8")).hexdigest()


async def _post(legacy: Any, path: str, payload: Any, *, prefer: str | None = None) -> Any:
    headers = dict(legacy.HEADERS_SB)
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{legacy.SUPABASE_URL}/rest/v1/{path}",
            headers=headers, json=payload,
        )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"outbox {path} HTTP {response.status_code}")
    if not response.content or not response.content.strip():
        raise RuntimeError(f"outbox {path} empty successful body")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"outbox {path} invalid JSON") from exc


async def enqueue(legacy: Any, job_name: str, service_date: str | date, body: str) -> int:
    """Enqueue once per job/day/recipient. Retry safely after a partial DB write.

    resolution=ignore-duplicates prevents changing already-SENT rows back to PENDING.
    An empty JSON list is valid only when the unique conflict was ignored.
    """
    if not enabled():
        raise RuntimeError("outbox feature is not enabled")
    recipients = configured_recipients()
    if not recipients:
        raise RuntimeError("outbox has no recipients")
    day = str(service_date)
    if not body or len(body) > 3300:
        raise ValueError("outbox message size invalid")
    for recipient in recipients:
        key = dedupe_key(job_name, day, recipient)
        payload = {
            "dedupe_key": key, "job_name": job_name, "service_date": day,
            "chat_id": recipient,
            "body": body + f"\n[알림 ID: {key[:12]}]",
            "state": "PENDING",
        }
        result = await _post(
            legacy, "task194_notification_outbox?on_conflict=dedupe_key", payload,
            prefer="resolution=ignore-duplicates,return=representation",
        )
        if not isinstance(result, list):
            raise RuntimeError("outbox enqueue returned non-list JSON")
    return len(recipients)


async def dispatch_once(legacy: Any, limit: int = 5) -> dict[str, int]:
    """Claim atomically; retry expired leases up to 3 times, with visible UNKNOWN.

    Crash after Telegram accepted but before DB ACK may lead to a duplicate. This is
    bounded at-least-once attempt semantics, not exactly-once delivery.
    """
    if not enabled():
        return {"disabled": 1}
    rows = await _post(legacy, "rpc/task194_claim_notifications", {"p_limit": max(1, min(limit, 10))})
    if not isinstance(rows, list):
        raise RuntimeError("outbox claim returned non-list JSON")
    from telegram import Bot
    bot = Bot(token=legacy.TELEGRAM_TOKEN)
    result = {"sent": 0, "unknown": 0, "retry": 0}
    for row in rows:
        item_id, token = row["id"], row["lease_token"]
        try:
            # External delivery is deliberately OUTSIDE the DB transaction.
            message = await bot.send_message(chat_id=row["chat_id"], text=row["body"])
        except Exception as exc:
            # A network exception might happen after Telegram accepted the message.
            # Keep it visible as UNKNOWN instead of silently treating it as unsent.
            uncertain = True
            try:
                ack = await _post(legacy, "rpc/task194_release_notification", {
                    "p_id": item_id, "p_lease": token, "p_uncertain": uncertain,
                    "p_error": type(exc).__name__,
                })
                if ack is not True:
                    raise RuntimeError("outbox release lease was not acknowledged")
                result["unknown"] += 1
            except Exception:
                legacy.logger.exception("Task194 outbox UNKNOWN record failed, id=%s", item_id)
                raise
            continue
        # Do not suppress an ACK failure: the expired lease will be retried,
        # and a possible duplicate is explicitly identified by the stable alert ID.
        ack = await _post(legacy, "rpc/task194_ack_notification", {
            "p_id": item_id, "p_lease": token, "p_message_id": message.message_id,
        })
        if ack is not True:
            raise RuntimeError(f"outbox ACK rejected, id={item_id}")
        result["sent"] += 1
    return result


async def count_unknown(legacy: Any) -> int:
    """Read-only watchdog signal; DB errors propagate rather than looking like zero."""
    if not enabled():
        return 0
    rows = await legacy.sb_get_strict(
        "task194_notification_outbox", {"select": "id", "state": "eq.UNKNOWN", "limit": "100"},
    )
    return len(rows)

# Context-local interception avoids changing Telegram behavior for unrelated jobs.
# CASSANDRA F7: the legacy 7-day calculation catches send errors, so the wrapper
# independently propagates an enqueue error after the calculation returns.
_OUTBOX_CONTEXT: ContextVar[dict | None] = ContextVar("task194_outbox_context", default=None)


def install_broadcast_routing(legacy: Any) -> None:
    """Route ONLY the Task194 daily jobs through the outbox, with no global bleed."""
    if not enabled() or getattr(legacy, "_task194_outbox_patched", False):
        return
    original_send = legacy.send_telegram_broadcast

    async def broadcast_proxy(text: str):
        ctx = _OUTBOX_CONTEXT.get()
        if ctx is None:
            return await original_send(text)
        try:
            count = await enqueue(legacy, ctx["job"], ctx["day"], text)
            # Legacy ingestion code expects a send count. This means QUEUED here,
            # never a Telegram delivery acknowledgement.
            return {"sent": count, "failed": 0}
        except Exception as exc:
            ctx["error"] = exc
            raise

    def wrap_job(job: str, original_func, date_func):
        async def wrapped(*args, **kwargs):
            ctx = {"job": job, "day": date_func(), "error": None}
            token = _OUTBOX_CONTEXT.set(ctx)
            try:
                result = await original_func(*args, **kwargs)
                if ctx["error"] is not None:
                    raise RuntimeError("Task194 outbox enqueue failed") from ctx["error"]
                return result
            finally:
                _OUTBOX_CONTEXT.reset(token)
        return wrapped

    # Set the proxy before wrapping; ContextVar is isolated across async tasks.
    legacy.send_telegram_broadcast = broadcast_proxy
    legacy.recalc_7day_average = wrap_job(
        "recalc_7day_average", legacy.recalc_7day_average, legacy.today_kst,
    )
    legacy.check_daily_ingestion_detail = wrap_job(
        "check_daily_ingestion_detail", legacy.check_daily_ingestion_detail,
        lambda: legacy.today_kst() - timedelta(days=1),
    )
    legacy._task194_outbox_patched = True
