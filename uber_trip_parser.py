"""Deterministic Uber trip-detail parser v2.

Supports both:
- legacy Uber detail screens containing 요금/순수익/정산 labels
- compact detail screens with one large top currency amount, trip metrics,
  two KR addresses, and optional 직접 결제 label

The compact variant deliberately does NOT search arbitrary currency values across
the whole OCR text. It accepts only a unique plausible currency amount in the
header section before the metrics block.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


_CURRENCY_RE = re.compile(r"(?:₩|￦|¥|\\|[Ww])\s*([\d,]{4,})")
_DATE_TIME_RE = re.compile(
    r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.[^\dAP]*?(AM|PM)\s*(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"(\d{1,3})분\s*(\d{1,2})초")
_DISTANCE_RE = re.compile(r"([\d.]+)\s*km", re.IGNORECASE)
_ADDRESS_LINE_RE = re.compile(
    r"^\s*(?P<addr>.+?)\s+(?:KR|KOR)\s*$",
    re.IGNORECASE,
)


def _address_candidates(text: str) -> list[str]:
    """Return address-like lines ending in KR/KOR, Korean or English.

    Foreign-rider Uber screens can localize addresses to English (for example
    "Daegu Suseong District KOR"). The country suffix is the stable anchor;
    no Korean administrative-token requirement is imposed.
    """
    out = []
    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        m = _ADDRESS_LINE_RE.match(line)
        if not m:
            continue
        addr = m.group("addr").strip(" ·•")
        if len(addr) < 5:
            continue
        if re.search(r"(?:₩|￦|\b(?:AM|PM)\b|\bkm\b|\d+분|\d+초)", addr, re.IGNORECASE):
            continue
        if not re.search(r"[가-힣A-Za-z]", addr):
            continue
        out.append(addr)
    return out


def _amounts(text: str) -> list[int]:
    out = []
    for raw in _CURRENCY_RE.findall(text or ""):
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue
        if 1000 <= value <= 1000000:
            out.append(value)
    return out


def extract_focused_uber_fare_amounts(text: str) -> list[int]:
    """Extract fare candidates from a spatially isolated Uber header crop.

    The crop is expected to contain the trip header/date and large fare only.
    Date/time, duration, distance, and arbitrary body numbers are rejected.
    A caller must treat more than one unique amount as ambiguous.
    """
    out = []
    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.search(
            r"(?:\b(?:AM|PM)\b|\bkm\b|\d{1,2}:\d{2}|\d+\s*분|\d+\s*초|"
            r"\b20\d{2}\b|운행|일반\s*콜|XL)",
            line,
            re.IGNORECASE,
        ):
            continue
        nums = re.findall(
            r"(?<!\d)(\d{1,3}(?:\s*[,\.]\s*\d{3})+|\d{4,6})(?!\d)",
            line,
        )
        if len(nums) != 1:
            continue
        raw = re.sub(r"[,\.\s]", "", nums[0])
        try:
            value = int(raw)
        except ValueError:
            continue
        if 1000 <= value <= 1000000:
            out.append(value)
    return sorted(set(out))


def reconcile_compact_fare_with_probe(parsed: dict, probe_text: str | None) -> dict:
    """Cross-check compact Uber fare against focused header OCR.

    No probe evidence means compatibility fallback to the existing whole-image
    parse. More than one focused candidate fails closed. Exactly one focused
    candidate is spatially stronger fare evidence and may replace a conflicting
    whole-image amount without changing any structural trip field.
    """
    out = dict(parsed or {})
    if out.get("ui_variant") != "compact_detail_v2" or probe_text is None:
        return {"ok": True, "parsed": out, "probe_status": "NOT_APPLICABLE"}

    amounts = extract_focused_uber_fare_amounts(probe_text)
    if not amounts:
        return {"ok": True, "parsed": out, "probe_status": "NO_EVIDENCE"}
    if len(amounts) != 1:
        return {
            "ok": False,
            "error_code": "UBER_FOCUSED_FARE_AMBIGUOUS",
            "message": "집중 요금 OCR 후보가 유일하지 않음",
            "probe_amounts": amounts,
        }

    focused = amounts[0]
    whole = out.get("요금")
    if whole is not None and int(whole) != focused:
        out["요금_whole_ocr_참고"] = int(whole)
        out["요금"] = focused
        out["요금근거"] = "COMPACT_HEADER_FOCUSED_REOCR"
        return {
            "ok": True,
            "parsed": out,
            "probe_status": "CORRECTED",
            "focused_amount": focused,
            "whole_amount": int(whole),
        }

    out["요금"] = focused
    if whole is None:
        out["요금근거"] = "COMPACT_HEADER_FOCUSED_REOCR"
    return {
        "ok": True,
        "parsed": out,
        "probe_status": "AGREED" if whole is not None else "RECOVERED",
        "focused_amount": focused,
        "whole_amount": int(whole) if whole is not None else None,
    }


def _header_bare_amounts(text: str) -> list[int]:
    """Fallback for compact Uber screens when OCR drops the currency glyph.

    Only inspect the text AFTER the parsed trip datetime and BEFORE the metrics
    block. Accept a standalone numeric line, optionally using comma or dot as
    a thousands separator. This deliberately excludes date/time, duration,
    distance, and later point/reward numbers.
    """
    header = _header_section(text)
    dm = _DATE_TIME_RE.search(header)
    if not dm:
        return []

    tail = header[dm.end():]
    out = []
    for raw_line in tail.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = re.fullmatch(
            r"[^\dA-Za-z가-힣]{0,3}"
            r"(\d{1,3}(?:\s*[,\.]\s*\d{3})+|\d{4,6})"
            r"[^\dA-Za-z가-힣]{0,3}",
            line,
        )
        if not m:
            continue
        raw = re.sub(r"[,\.\s]", "", m.group(1))
        try:
            value = int(raw)
        except ValueError:
            continue
        if 1000 <= value <= 1000000:
            out.append(value)
    return out


def looks_like_uber_trip_detail(text: str) -> bool:
    text = str(text or "")
    if "운행 세부사항" not in text:
        return False

    # Both legacy and compact UIs need independent trip anchors. "순수익" is
    # never sufficient by itself because generic/malformed pages can contain
    # the same label pair.
    has_datetime = bool(_DATE_TIME_RE.search(text))
    has_duration = bool(_DURATION_RE.search(text))
    has_distance = bool(_DISTANCE_RE.search(text))
    has_two_addresses = len(_address_candidates(text)) >= 2
    has_currency = bool(_amounts(text))
    independent_anchor_count = sum([
        has_datetime,
        has_duration,
        has_distance,
        has_two_addresses,
        has_currency,
    ])

    if "순수익" in text:
        # Legacy keeps its semantic label, but still needs at least three
        # independent trip anchors before it can be classified as Uber detail.
        return independent_anchor_count >= 3

    # Compact/new UI has no "순수익", so require a stronger 4/5 signature.
    return independent_anchor_count >= 4


def _parse_datetime(text: str, result: dict) -> None:
    m = _DATE_TIME_RE.search(text)
    if not m:
        result["parse_errors"].append("날짜시각 파싱실패")
        return

    y, mo, d, ampm, h, mi = m.groups()
    hour = int(h)
    if ampm.upper() == "PM" and hour != 12:
        hour += 12
    if ampm.upper() == "AM" and hour == 12:
        hour = 0
    result["날짜"] = f"{y}-{int(mo):02d}-{int(d):02d}"
    result["배차시각"] = f"{hour:02d}:{mi}"


def _parse_duration_distance(text: str, result: dict) -> None:
    m = _DURATION_RE.search(text)
    if m:
        minutes, seconds = map(int, m.groups())
        result["운행시간_분"] = round(minutes + seconds / 60, 1)
        result["운행시간_초"] = minutes * 60 + seconds
    else:
        result["parse_errors"].append("운행시간 파싱실패")

    m = _DISTANCE_RE.search(text)
    if m:
        result["거리_km"] = float(m.group(1))
    else:
        result["parse_errors"].append("거리 파싱실패")


def _parse_addresses(text: str, result: dict) -> None:
    matches = _address_candidates(text)
    if len(matches) >= 2:
        result["출발지"] = matches[0]
        result["도착지"] = matches[1]
        result["주소표기"] = (
            "ENGLISH"
            if re.search(r"[A-Za-z]", matches[0] + " " + matches[1])
            and not re.search(r"[가-힣]", matches[0] + " " + matches[1])
            else "KOREAN_OR_MIXED"
        )
    else:
        result["parse_errors"].append(f"출발/도착 파싱실패(찾은건수:{len(matches)})")


def _parse_payment(text: str, result: dict) -> None:
    if re.search(r"직접\s*결제", text):
        result["결제방식"] = "직접"
        result["결제수단"] = "직접결제"
        result["결제증거"] = "DIRECT_LABEL"
    else:
        result["결제방식"] = "미확인"
        result["결제증거"] = "NO_DIRECT_LABEL"


def _header_section(text: str) -> str:
    text = str(text or "")
    # On the compact screen the large revenue amount is before the metrics block.
    # OCR may slightly alter spaces, so stop at the first reliable metric label.
    cuts = []
    for marker in ("\n시간", "\n거리", "시간\n", "거리\n"):
        i = text.find(marker)
        if i >= 0:
            cuts.append(i)
    end = min(cuts) if cuts else min(len(text), 600)
    return text[:end]


def _parse_fare(text: str, result: dict) -> None:
    # Legacy UI: keep the existing label-proximate rule.
    m = re.search(r"요금[\s\S]{0,20}?(?:₩|￦|\\|[Ww])\s*([\d,]{4,})", text)
    if m:
        result["요금"] = int(m.group(1).replace(",", ""))
        result["요금근거"] = "LEGACY_FARE_LABEL"
        return

    # Compact/new UI: only the header section may supply the primary fare.
    header_amounts = sorted(set(_amounts(_header_section(text))))
    if len(header_amounts) == 1:
        result["요금"] = header_amounts[0]
        result["요금근거"] = "COMPACT_HEADER_UNIQUE_CURRENCY"
        return
    if len(header_amounts) > 1:
        result["parse_errors"].append(
            "상단요금 다중후보:" + ",".join(map(str, header_amounts))
        )
        return

    # Some foreign-rider / XL OCR results preserve the amount digits but drop
    # the currency glyph. Use only a unique standalone numeric line between the
    # datetime anchor and the metrics block.
    bare_amounts = sorted(set(_header_bare_amounts(text)))
    if len(bare_amounts) == 1:
        result["요금"] = bare_amounts[0]
        result["요금근거"] = "COMPACT_HEADER_UNIQUE_BARE_AMOUNT"
        return
    if len(bare_amounts) > 1:
        result["parse_errors"].append(
            "상단요금 숫자후보 다중:" + ",".join(map(str, bare_amounts))
        )
    else:
        result["parse_errors"].append("요금 파싱실패")


def _cross_validate_legacy_settlement(text: str, result: dict) -> None:
    m = re.search(r"정산[\s\S]{0,40}?-\s*(?:₩|￦|\\|[Ww])\s*([\d,]{4,})", text)
    if not m or "요금" not in result:
        return
    settle_amt = int(m.group(1).replace(",", ""))
    if abs(int(result["요금"]) - settle_amt) > 100:
        result["parse_errors"].append(
            f"요금({result['요금']})≠정산액({settle_amt}) 불일치 — OCR오류 의심, 확인필요"
        )
        result["요금_정산액_참고"] = settle_amt


def parse_uber_trip_detail_text(text: str) -> dict:
    text = str(text or "")
    compact = "순수익" not in text
    result = {
        "format": "uber_trip_detail",
        "parse_errors": [],
        "콜유형": "우버",
        "ui_variant": "compact_detail_v2" if compact else "legacy_detail",
    }

    _parse_datetime(text, result)
    _parse_duration_distance(text, result)
    _parse_addresses(text, result)
    _parse_payment(text, result)
    _parse_fare(text, result)
    _cross_validate_legacy_settlement(text, result)

    # Convenience only; persistence may choose not to store this derived field.
    if result.get("배차시각") and result.get("운행시간_초") is not None:
        try:
            hh, mm = map(int, result["배차시각"].split(":"))
            base = datetime(2000, 1, 1, hh, mm)
            end = base + timedelta(seconds=int(result["운행시간_초"]))
            result["하차시각_추정"] = end.strftime("%H:%M")
        except Exception:
            pass

    return result


def validate_uber_trip_detail(parsed: dict) -> dict:
    """Strict pre-save gate for both compact and legacy Uber detail UIs."""
    variant = parsed.get("ui_variant")

    if variant == "compact_detail_v2":
        if any(str(e).startswith("상단요금 다중후보") for e in parsed.get("parse_errors", [])):
            return {
                "ok": False,
                "error_code": "UBER_COMPACT_FARE_AMBIGUOUS",
                "message": "상단 요금이 유일하지 않음",
            }

    required = {
        "날짜": parsed.get("날짜"),
        "배차시각": parsed.get("배차시각"),
        "출발지": parsed.get("출발지"),
        "도착지": parsed.get("도착지"),
        "요금": parsed.get("요금"),
        "운행시간_분": parsed.get("운행시간_분"),
        "거리_km": parsed.get("거리_km"),
    }
    missing = [k for k, v in required.items() if v in (None, "")]
    if missing:
        if variant == "legacy_detail":
            return {
                "ok": False,
                "error_code": "UBER_LEGACY_REQUIRED_FIELD_MISSING",
                "message": "legacy 필수필드 누락:" + ",".join(missing),
            }
        return {
            "ok": False,
            "error_code": "UBER_COMPACT_REQUIRED_FIELD_MISSING",
            "message": "필수필드 누락:" + ",".join(missing),
        }

    return {"ok": True}
