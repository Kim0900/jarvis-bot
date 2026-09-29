"""Deterministic parser/validator for Kakao T daily-history OCR text.

task#164 (2026-09-29)
- Parses multiple visible trip rows by segmenting on HH:MM-HH:MM anchors.
- Uses header count/amount and detected time-anchor count as integrity checks.
- Validation is Fail-Closed: missing date or partial extraction must not write raw_calls.
"""
from __future__ import annotations

import re
from typing import Any

_TIME_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2}:\d{2})\s*[-~–—]\s*(\d{1,2}:\d{2})(?!\d)"
)
_DATE_RE = re.compile(
    r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"
)
_HEADER_RE = re.compile(
    r"실시간\s*운행[\s\S]{0,100}?(\d{1,3})\s*건"
    r"[\s/|·,:-]{0,30}?([\d,]{4,})\s*원"
)
_FARE_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원")
_NUMERIC_TOKEN_RE = re.compile(r"(?<!\d)(?:\d[\d,\.\s]{2,8}\d)(?!\d)")
_ADDRESS_FALLBACK_RE = re.compile(
    r"대구\s+[^\s\r\n]{1,12}\s+[^\s\r\n]{1,24}"
)

def _clean_line(line: str) -> str:
    line = re.sub(r"^[\s•●○·oO0ㆍ\-–—]+", "", line.strip())
    line = re.sub(r"\s+", " ", line)
    return line.strip()

def _safe_numeric_tokens(segment: str) -> list[str]:
    """주소/OCR 원문을 남기지 않고 요금 후보 숫자 형태만 진단한다.

    시간(HH:MM)은 제외하고, 구두점/공백을 제거한 뒤 3~6자리 숫자만
    최대 5개 보존한다. 값 자체는 요금 후보 진단용이며 저장 근거로는
    사용하지 않는다.
    """
    out = []
    for raw in _NUMERIC_TOKEN_RE.findall(segment):
        token = re.sub(r"\D", "", raw)
        if not (3 <= len(token) <= 6):
            continue
        if token not in out:
            out.append(token)
        if len(out) >= 5:
            break
    return out


def _address_candidates(segment: str) -> list[str]:
    out: list[str] = []
    for raw in segment.replace("\r", "\n").split("\n"):
        line = _clean_line(raw)
        if "대구" not in line:
            continue
        pos = line.find("대구")
        value = line[pos:].strip()
        # UI 꼬리표/요금이 붙은 경우 보수적으로 잘라낸다.
        value = re.split(
            r"\s*(?:직접결제|결제\s*취소하기|[\d,]{4,}\s*원)\s*",
            value,
            maxsplit=1,
        )[0].strip()
        if value and value not in out:
            out.append(value)

    if len(out) < 2:
        for m in _ADDRESS_FALLBACK_RE.finditer(segment):
            value = _clean_line(m.group(0))
            if value and value not in out:
                out.append(value)
    return out

def parse_daily_history_text(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format": "daily_history",
        "날짜": None,
        "표시건수": None,
        "표시금액": None,
        "time_anchor_count": 0,
        "parse_errors": [],
        "items": [],
    }
    if not isinstance(text, str) or not text.strip():
        result["parse_errors"].append("OCR 텍스트 비어있음")
        return result

    date_match = _DATE_RE.search(text)
    if date_match:
        y, m, d = map(int, date_match.groups())
        result["날짜"] = f"{y:04d}-{m:02d}-{d:02d}"
    else:
        result["parse_errors"].append("날짜 파싱실패")

    header = _HEADER_RE.search(text)
    if header:
        result["표시건수"] = int(header.group(1))
        result["표시금액"] = int(header.group(2).replace(",", ""))

    # OCR.space 긴이미지 2분할 경로는 조각 marker를 삽입한다.
    # overlap 구간의 동일 운행은 (시작,종료) time key로 합치고,
    # 한 조각이 불완전해도 다른 조각이 완전하면 완전행을 채택한다.
    chunks = [
        part for part in re.split(r"---MAGI_OCR_CHUNK_\d+---", text)
        if part and part.strip()
    ]
    if not chunks:
        chunks = [text]

    order = []
    observed = {}
    complete = {}

    for chunk in chunks:
        anchors = list(_TIME_RANGE_RE.finditer(chunk))
        for idx, anchor in enumerate(anchors):
            seg_end = anchors[idx + 1].start() if idx + 1 < len(anchors) else len(chunk)
            segment = chunk[anchor.start():seg_end]
            start, end = anchor.groups()
            time_key = (start.zfill(5), end.zfill(5))
            if time_key not in observed:
                order.append(time_key)
                observed[time_key] = []
                complete[time_key] = []

            addresses = _address_candidates(segment)
            fares = [int(x.replace(",", "")) for x in _FARE_RE.findall(segment)]
            numeric_tokens = _safe_numeric_tokens(segment)
            observed[time_key].append((len(addresses), len(fares), numeric_tokens))

            if len(addresses) < 2 or not fares:
                continue

            fare = fares[-1]
            origin, dest = addresses[0], addresses[1]
            item = {
                "탑승시각": time_key[0],
                "하차시각": time_key[1],
                "출발지": origin,
                "도착지": dest,
                "요금": fare,
                "결제방식": "직접" if "직접결제" in segment else "자동",
            }
            complete[time_key].append(item)

    result["time_anchor_count"] = len(order)
    if not order:
        result["parse_errors"].append("운행 시간범위 파싱실패")
        return result

    for time_key in order:
        candidates = complete.get(time_key) or []
        if not candidates:
            obs = observed.get(time_key, [])
            best_addr = max((x[0] for x in obs), default=0)
            best_fare = max((x[1] for x in obs), default=0)
            numeric_candidates = []
            for x in obs:
                for token in (x[2] if len(x) > 2 else []):
                    if token not in numeric_candidates:
                        numeric_candidates.append(token)
                    if len(numeric_candidates) >= 5:
                        break
                if len(numeric_candidates) >= 5:
                    break
            candidate_text = ",".join(numeric_candidates) if numeric_candidates else "없음"
            result["parse_errors"].append(
                f"행 파싱불완전({time_key[0]}-{time_key[1]}): "
                f"주소최대{best_addr}개/요금최대{best_fare}개/"
                f"숫자후보={candidate_text}"
            )
            continue

        # overlap 양쪽에서 같은 운행이 잡힌 경우 실제 필드가 같은지 확인.
        # 결제방식은 한쪽 조각에 직접결제 라벨이 잘릴 수 있어 '직접'을 우선 병합한다.
        normalized = {}
        for item in candidates:
            key = (item["출발지"], item["도착지"], item["요금"])
            if key not in normalized:
                normalized[key] = dict(item)
            elif item["결제방식"] == "직접":
                normalized[key]["결제방식"] = "직접"

        if len(normalized) != 1:
            result["parse_errors"].append(
                f"중복조각 OCR 불일치({time_key[0]}-{time_key[1]}): "
                f"후보{len(normalized)}개"
            )
            continue

        result["items"].append(next(iter(normalized.values())))

    if not result["items"]:
        result["parse_errors"].append("콜 목록 파싱실패")
    return result

def validate_daily_history_document(parsed: dict[str, Any]) -> dict[str, Any]:
    items = parsed.get("items") or []
    parsed_count = len(items)
    parsed_amount = sum(int(x.get("요금") or 0) for x in items)
    displayed_count = parsed.get("표시건수")
    displayed_amount = parsed.get("표시금액")
    anchor_count = int(parsed.get("time_anchor_count") or 0)

    base = {
        "ok": False,
        "parsed_count": parsed_count,
        "parsed_amount": parsed_amount,
        "displayed_count": displayed_count,
        "displayed_amount": displayed_amount,
        "time_anchor_count": anchor_count,
    }

    if not parsed.get("날짜"):
        return {
            **base,
            "error_code": "DAILY_HISTORY_DATE_MISSING",
            "message": "일별운행이력 날짜를 인식하지 못해 저장을 중단했습니다.",
        }

    if parsed_count == 0:
        return {
            **base,
            "error_code": "DAILY_HISTORY_NO_ROWS",
            "message": "일별운행이력 운행행을 추출하지 못해 저장을 중단했습니다.",
        }

    expected_count = displayed_count if displayed_count is not None else (anchor_count or None)
    if expected_count is not None and parsed_count != expected_count:
        return {
            **base,
            "expected_count": expected_count,
            "error_code": "DAILY_HISTORY_COUNT_MISMATCH",
            "message": (
                f"일별운행이력 건수 불일치: 화면/감지 {expected_count}건, "
                f"추출 {parsed_count}건. 부분저장을 차단했습니다."
            ),
        }

    if anchor_count and parsed_count != anchor_count:
        return {
            **base,
            "expected_count": anchor_count,
            "error_code": "DAILY_HISTORY_ANCHOR_MISMATCH",
            "message": (
                f"일별운행이력 시간행 {anchor_count}개 중 {parsed_count}개만 "
                "완전 추출되어 부분저장을 차단했습니다."
            ),
        }

    if displayed_amount is not None and parsed_amount != displayed_amount:
        return {
            **base,
            "error_code": "DAILY_HISTORY_AMOUNT_MISMATCH",
            "message": (
                f"일별운행이력 금액 불일치: 화면 {displayed_amount:,}원, "
                f"추출합계 {parsed_amount:,}원. 저장을 차단했습니다."
            ),
        }

    return {**base, "ok": True, "error_code": None, "message": "PASS"}
