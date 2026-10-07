"""Task #181: deterministic parser for Kakao monthly driving-history control totals."""

from __future__ import annotations

import calendar
import re
from datetime import date

_HEADER_RE = re.compile(
    r"(?P<year>20\d{2})\s*년\s*(?P<month>\d{1,2})\s*월\s*(?P<total>\d{1,4})\s*건"
)
_DAY_RE = re.compile(
    r"(?P<day>\d{1,2})\s*일\s*(?:\([^)]*\))?\s*(?P<count>\d{1,4})\s*건"
)


def parse_kakao_monthly_history_text(text: str, *, today: date | None = None) -> dict:
    text = str(text or "")
    today = today or date.today()

    header = _HEADER_RE.search(text)
    if not header:
        return {
            "format": "kakao_monthly_history_control",
            "success": False,
            "error_code": "KAKAO_MONTHLY_HEADER_MISSING",
            "items": [],
        }

    year = int(header.group("year"))
    month = int(header.group("month"))
    total_count = int(header.group("total"))
    if not 1 <= month <= 12:
        return {
            "format": "kakao_monthly_history_control",
            "success": False,
            "error_code": "KAKAO_MONTHLY_INVALID_MONTH",
            "items": [],
        }

    visible = {}
    for m in _DAY_RE.finditer(text):
        day = int(m.group("day"))
        count = int(m.group("count"))
        try:
            date(year, month, day)
        except ValueError:
            continue
        visible[day] = count

    if not visible:
        return {
            "format": "kakao_monthly_history_control",
            "success": False,
            "error_code": "KAKAO_MONTHLY_DAY_ROWS_MISSING",
            "year": year,
            "month": month,
            "monthly_total": total_count,
            "items": [],
        }

    max_day = max(visible)
    if (year, month) < (today.year, today.month):
        control_through = calendar.monthrange(year, month)[1]
    else:
        # Current-month screen: fill internal gaps only through the last day
        # actually visible on screen. Future/unseen trailing days remain OPEN.
        control_through = max_day

    items = []
    for day in range(1, control_through + 1):
        items.append({
            "page_date": date(year, month, day).isoformat(),
            "expected_count": int(visible.get(day, 0)),
        })

    return {
        "format": "kakao_monthly_history_control",
        "success": True,
        "year": year,
        "month": month,
        "monthly_total": total_count,
        "visible_row_count": len(visible),
        "control_through_day": control_through,
        "items": items,
    }


def looks_like_kakao_monthly_history(text: str) -> bool:
    text = str(text or "")
    return (
        "월별" in text
        and "실시간" in text
        and "운행" in text
        and _HEADER_RE.search(text) is not None
    )
