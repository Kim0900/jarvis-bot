"""Task #187: deterministic Telegram messages for data-ingestion visibility."""

from __future__ import annotations

from typing import Any

_KIND_LABELS = {
    "call_image": "운행 이미지",
    "daily_history": "카카오 일별 운행이력",
    "kakao_monthly_history_control": "카카오 월별 운행이력",
    "kakao_trip_detail": "카카오 운행 상세",
    "uber_trip_detail": "Uber 운행 상세",
    "meter_receipt": "미터기 영수증",
    "s700": "S700 미터기 데이터",
    "gpx": "GPX 운행경로",
}

_STAGE_TITLES = {
    "RECEIVED": "📥 데이터 인입 확인",
    "COMPLETED": "✅ 데이터 처리 완료",
    "ERROR": "❌ 데이터 처리 오류",
}


def _safe_text(value: Any, limit: int = 500) -> str:
    s = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _fmt_num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _source_ref(source_id: str | None) -> str | None:
    s = _safe_text(source_id, 120)
    if not s:
        return None
    return s if len(s) <= 12 else "…" + s[-10:]


def _label(kind: str | None, result: dict | None) -> str:
    fmt = None
    if isinstance(result, dict):
        fmt = result.get("format")
    key = str(fmt or kind or "data")
    return _KIND_LABELS.get(key, key)


def format_ingestion_message(
    *,
    stage: str,
    kind: str,
    source_id: str | None = None,
    file_name: str | None = None,
    result: dict | None = None,
    error: str | None = None,
    retryable: bool | None = None,
) -> str:
    """Build a concise, user-facing Telegram notification.

    This function is pure/deterministic and must never include credentials or raw payloads.
    """
    stage = str(stage or "").upper()
    if stage not in _STAGE_TITLES:
        raise ValueError(f"unsupported stage: {stage}")

    result = result or {}
    lines = [_STAGE_TITLES[stage], f"• 종류: {_label(kind, result)}"]

    if file_name:
        lines.append(f"• 파일: {_safe_text(file_name, 180)}")
    ref = _source_ref(source_id)
    if ref:
        lines.append(f"• 참조: {ref}")

    if stage == "RECEIVED":
        lines.append("• 상태: 서버 인입 확인, 처리 시작")
        return "\n".join(lines)

    if stage == "COMPLETED":
        if kind == "s700":
            inserted = int(result.get("inserted") or 0)
            dup = int(result.get("duplicates_existing") or 0)
            conflicts = len(result.get("conflicts_existing") or [])
            failed = len(result.get("insert_failed") or [])
            lines.append(
                f"• 결과: 신규 {inserted}건 / 중복 {dup}건 / 충돌 {conflicts}건 / 실패 {failed}건"
            )
            match = result.get("match") or {}
            if match:
                order = ("MATCHED", "UNMATCHED", "PROVISIONAL", "AMBIGUOUS")
                bits = [f"{k} {int(match.get(k) or 0)}" for k in order if k in match]
                if bits:
                    lines.append("• 매칭: " + " / ".join(bits))
        elif kind == "gpx":
            inserted = int(result.get("inserted") or 0)
            if result.get("duplicate"):
                lines.append("• 결과: 중복 파일 — 신규 저장 0건")
            else:
                lines.append(f"• 결과: 신규 세션 {inserted}건")
            session = result.get("session") or {}
            service_date = session.get("service_date")
            point_count = session.get("point_count")
            if service_date:
                lines.append(f"• 기준일: {_safe_text(service_date, 40)}")
            if point_count is not None:
                lines.append(f"• 트랙포인트: {_fmt_num(point_count)}")
        else:
            saved = int(result.get("saved_count") or result.get("inserted_count") or 0)
            dups = int(result.get("duplicate_skipped_count") or 0)
            if result.get("duplicate") and saved == 0:
                lines.append("• 결과: 이미 처리된 동일 원본 — 신규 저장 0건")
            else:
                lines.append(f"• 결과: 신규 {saved}건" + (f" / 중복 {dups}건" if dups else ""))
            page_date = result.get("page_date")
            if page_date:
                lines.append(f"• 기준일: {_safe_text(page_date, 40)}")
            displayed_count = result.get("displayed_count")
            displayed_amount = result.get("displayed_amount")
            if displayed_count is not None:
                detail = f"화면 {int(displayed_count)}건"
                if displayed_amount is not None:
                    detail += f" / {_fmt_num(displayed_amount)}원"
                lines.append(f"• 검증: {detail}")

        lines.append("• 상태: 처리 완료")
        return "\n".join(lines)

    err = _safe_text(error or result.get("error") or result.get("error_code") or "알 수 없는 오류", 900)
    lines.append(f"• 오류: {err}")
    if retryable is None:
        retryable = result.get("retryable")
    if retryable is not None:
        lines.append("• 재시도: " + ("가능" if bool(retryable) else "불가"))
    lines.append("• 상태: 확인 필요")
    return "\n".join(lines)
