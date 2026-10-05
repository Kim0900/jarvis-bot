"""Task #27 deterministic daily operation report v1.

Inputs:
- canonical raw_calls selected through Task #164 canonical RPC
- optional S700 rows for roaming *candidate* telemetry only

No raw rows are written or modified.
"""

from __future__ import annotations

from collections import defaultdict


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _minutes(hhmm):
    if not hhmm:
        return None
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _hour_bucket(hhmm):
    m = _minutes(hhmm)
    if m is None:
        return None
    return f"{m // 60:02d}:00"


def build_daily_operation_report_v1(date_value, canonical_rows, s700_rows=None):
    rows = [r for r in (canonical_rows or []) if str(r.get("날짜") or "") == str(date_value)]
    s700_rows = list(s700_rows or [])

    total_count = len(rows)
    total_revenue = sum(_int(r.get("요금")) for r in rows)
    avg_fare = int(total_revenue / total_count) if total_count else 0

    platform = defaultdict(lambda: {"count": 0, "revenue": 0})
    provenance = defaultdict(lambda: {"count": 0, "revenue": 0})
    hourly = defaultdict(lambda: {"count": 0, "revenue": 0})

    weak_candidate_rows = 0
    for row in rows:
        p = str(row.get("canonical_platform") or "UNKNOWN")
        platform[p]["count"] += 1
        platform[p]["revenue"] += _int(row.get("요금"))

        src = str(row.get("data_source") or "(NULL)")
        provenance[src]["count"] += 1
        provenance[src]["revenue"] += _int(row.get("요금"))

        if _int(row.get("canonical_weak_candidate_count")) > 0:
            weak_candidate_rows += 1

        hb = _hour_bucket(row.get("배차시각"))
        if hb:
            hourly[hb]["count"] += 1
            hourly[hb]["revenue"] += _int(row.get("요금"))

    hourly_rows = []
    for bucket in sorted(hourly):
        item = dict(hourly[bucket])
        item["hour"] = bucket
        item["avg_fare"] = int(item["revenue"] / item["count"]) if item["count"] else 0
        hourly_rows.append(item)

    positive_hours = [x for x in hourly_rows if x["revenue"] > 0]
    highest_hour = None
    lowest_hour = None
    if positive_hours:
        highest_hour = max(positive_hours, key=lambda x: (x["revenue"], -int(x["hour"][:2])))
        lowest_hour = min(positive_hours, key=lambda x: (x["revenue"], int(x["hour"][:2])))

    timed = []
    missing_gap_fields = 0
    for row in rows:
        start = _minutes(row.get("배차시각"))
        end = _minutes(row.get("하차시각"))
        if start is None or end is None:
            missing_gap_fields += 1
            continue
        timed.append((start, end, row))

    timed.sort(key=lambda x: (x[0], x[1], _int(x[2].get("id"))))
    gaps = []
    invalid_gap_pairs = 0
    for idx in range(1, len(timed)):
        prev_end = timed[idx - 1][1]
        next_start = timed[idx][0]
        gap = next_start - prev_end
        if gap < 0:
            invalid_gap_pairs += 1
            continue
        gaps.append(gap)

    gap_summary = {
        "definition": "previous_dropoff_to_next_dispatch",
        "analyzable_trip_count": len(timed),
        "analyzable_gap_count": len(gaps),
        "missing_time_trip_count": missing_gap_fields,
        "invalid_gap_pair_count": invalid_gap_pairs,
        "min_gap_min": min(gaps) if gaps else None,
        "max_gap_min": max(gaps) if gaps else None,
        "avg_gap_min": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "continuous_call_gap_count": sum(1 for g in gaps if g <= 10),
        "continuous_call_threshold_min": 10,
        "partial": bool(missing_gap_fields or invalid_gap_pairs),
    }

    unmatched_s700 = [
        r for r in s700_rows
        if str(r.get("match_status") or "").upper() == "UNMATCHED"
    ]
    roaming_candidate = {
        "candidate_only": True,
        "count": len(unmatched_s700),
        "revenue": sum(_int(r.get("fare")) for r in unmatched_s700),
        "warning": (
            "S700 UNMATCHED는 배회 확정이 아님. 콜카드 누락/매처 미성숙으로 "
            "과대 산정될 수 있어 후보로만 표시."
        ),
    }

    return {
        "version": "daily_operation_report_v1",
        "date_basis": "calendar_day",
        "date": str(date_value),
        "total_count": total_count,
        "total_revenue": total_revenue,
        "avg_fare": avg_fare,
        "platform": dict(platform),
        "provenance": dict(provenance),
        "weak_identity_candidate_rows": weak_candidate_rows,
        "hourly": hourly_rows,
        "highest_revenue_hour": highest_hour,
        "lowest_revenue_hour": lowest_hour,
        "gap": gap_summary,
        "roaming_candidate": roaming_candidate,
    }

def build_briefing_section_a_v1(report):
    """Map the canonical daily report to the stable briefing Section A contract.

    This function is intentionally pure so Section A can be regression-tested
    without importing Telegram/bot runtime code. Missing or wrong report
    versions fail closed; callers must not silently fall back to raw_calls.
    """
    if not isinstance(report, dict):
        raise ValueError("daily_operation_report_v1 report required")
    if report.get("version") != "daily_operation_report_v1":
        raise ValueError("unexpected daily operation report version")
    if report.get("date_basis") != "calendar_day":
        raise ValueError("Section A requires calendar_day canonical report")

    platform = report.get("platform") or {}

    def _platform_count(name):
        value = platform.get(name) or {}
        return _int(value.get("count"))

    roaming = report.get("roaming_candidate") or {}
    return {
        "source": "daily_operation_report_v1",
        "date_basis": "calendar_day",
        "calls": _int(report.get("total_count")),
        "revenue": _int(report.get("total_revenue")),
        "avg_fare": _int(report.get("avg_fare")),
        "platform": {
            "kakao": _platform_count("KAKAO"),
            "uber": _platform_count("UBER"),
            "roam_confirmed": _platform_count("ROAM"),
        },
        "weak_identity_candidate_rows": _int(report.get("weak_identity_candidate_rows")),
        "roaming_candidate": {
            "candidate_only": bool(roaming.get("candidate_only", True)),
            "count": _int(roaming.get("count")),
            "revenue": _int(roaming.get("revenue")),
        },
    }

def build_month_activity_v1(date_value, canonical_rows):
    """Deterministic calendar-month activity stats from canonical trip rows.

    Rows after date_value are ignored. Empty calendar days remain in the
    denominator for calendar_avg_calls, while workday_avg_calls uses only dates
    with at least one canonical trip.
    """
    from datetime import date as _date

    target = _date.fromisoformat(str(date_value))
    month_prefix = target.strftime("%Y-%m")
    rows = [
        r for r in (canonical_rows or [])
        if str(r.get("날짜") or "").startswith(month_prefix)
        and str(r.get("날짜") or "") <= str(date_value)
    ]
    operating_days = sorted({str(r.get("날짜")) for r in rows if r.get("날짜")})
    count = len(rows)
    return {
        "date_basis": "calendar_day",
        "month": month_prefix,
        "through_date": str(date_value),
        "cumulative_calls": count,
        "calendar_days_elapsed": target.day,
        "calendar_avg_calls": round(count / max(target.day, 1), 2),
        "operating_days": len(operating_days),
        "workday_avg_calls": round(count / len(operating_days), 2) if operating_days else 0.0,
    }



def resolve_briefing_date_v1(arg=None, today_value=None):
    """Resolve optional briefing date tokens to YYYY-MM-DD.

    Supported: None/today, '오늘', '어제', or explicit YYYY-MM-DD.
    Raises ValueError for unsupported tokens to keep production E2E explicit.
    """
    from datetime import date as _date, timedelta as _timedelta

    today = _date.fromisoformat(str(today_value)) if today_value is not None else _date.today()
    token = "" if arg is None else str(arg).strip()
    if not token or token in ("오늘", "today"):
        return today.isoformat()
    if token in ("어제", "yesterday"):
        return (today - _timedelta(days=1)).isoformat()
    try:
        return _date.fromisoformat(token).isoformat()
    except Exception as exc:
        raise ValueError("briefing date must be 오늘/어제/YYYY-MM-DD") from exc
