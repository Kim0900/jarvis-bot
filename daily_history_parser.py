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

def _merge_overlap_sequences(left: list[int], right: list[int], max_overlap: int = 5) -> list[int]:
    """인접 OCR 조각의 suffix/prefix가 정확히 같은 경우에만 overlap을 제거한다."""
    max_k = min(max_overlap, len(left), len(right))
    for k in range(max_k, 0, -1):
        if left[-k:] == right[:k]:
            return left + right[k:]
    return left + right


def extract_fare_probe_amounts(text: str, displayed_amount: int | None = None) -> list[int]:
    """요금영역 보조 OCR 텍스트에서 'N원' 숫자만 추출한다.

    OCR 서비스가 삽입한 MAGI_OCR_CHUNK marker 단위로 금액을 뽑고,
    인접 chunk의 동일 suffix/prefix만 overlap 중복으로 제거한다.
    화면 상단 합계(displayed_amount)는 전체 병합 후 첫 1회만 제외한다.
    이 함수 결과는 task#164 진단용이며 raw_calls 저장 근거로 사용하지 않는다.
    """
    chunks = [
        part for part in re.split(r"---MAGI_OCR_CHUNK_\d+---", text or "")
        if part and part.strip()
    ]
    if not chunks:
        chunks = [text or ""]

    merged: list[int] = []
    for chunk in chunks:
        current = [int(x.replace(",", "")) for x in _FARE_RE.findall(chunk)]
        if not merged:
            merged = current
        else:
            merged = _merge_overlap_sequences(merged, current)

    if displayed_amount is not None:
        try:
            idx = merged.index(int(displayed_amount))
            merged.pop(idx)
        except ValueError:
            pass
    return merged


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

def repair_daily_history_with_fare_probe(text: str, fare_amounts: list[int]) -> dict[str, Any]:
    """본문 OCR 주소/시간과 요금전용 OCR을 엄격히 결합한다.

    허용 조건:
    - 날짜 존재
    - 화면 표시건수/표시금액 존재
    - unique time-anchor 수 == 표시건수 == fare_amounts 수
    - fare_amounts 합 == 표시금액
    - 모든 time-anchor마다 주소 2개가 정확히 하나의 출발/도착 pair로 수렴
    하나라도 어긋나면 repaired=False로 반환하고 저장에 사용하지 않는다.
    """
    base = parse_daily_history_text(text)
    result = {
        "repaired": False,
        "error_code": None,
        "message": None,
        "parsed": None,
    }

    date_value = base.get("날짜")
    displayed_count = base.get("표시건수")
    displayed_amount = base.get("표시금액")
    anchor_count = int(base.get("time_anchor_count") or 0)

    if not date_value:
        result.update(
            error_code="REPAIR_DATE_MISSING",
            message="날짜가 없어 요금 보정을 중단했습니다.",
        )
        return result
    if displayed_count is None or displayed_amount is None:
        result.update(
            error_code="REPAIR_HEADER_MISSING",
            message="화면 건수/금액 합계를 확인할 수 없어 요금 보정을 중단했습니다.",
        )
        return result
    if anchor_count != int(displayed_count):
        result.update(
            error_code="REPAIR_ANCHOR_COUNT_MISMATCH",
            message=f"시간행 {anchor_count}건과 화면 {displayed_count}건이 달라 보정을 중단했습니다.",
        )
        return result
    if len(fare_amounts or []) != int(displayed_count):
        result.update(
            error_code="REPAIR_FARE_COUNT_MISMATCH",
            message=f"요금 {len(fare_amounts or [])}건과 화면 {displayed_count}건이 달라 보정을 중단했습니다.",
        )
        return result

    normalized_fares = [int(x) for x in fare_amounts]
    if sum(normalized_fares) != int(displayed_amount):
        result.update(
            error_code="REPAIR_FARE_SUM_MISMATCH",
            message=(
                f"요금 합계 {sum(normalized_fares):,}원과 화면 "
                f"{int(displayed_amount):,}원이 달라 보정을 중단했습니다."
            ),
        )
        return result

    chunks = [
        part for part in re.split(r"---MAGI_OCR_CHUNK_\d+---", text or "")
        if part and part.strip()
    ]
    if not chunks:
        chunks = [text or ""]

    order: list[tuple[str, str]] = []
    row_candidates: dict[tuple[str, str], list[dict[str, str]]] = {}

    for chunk in chunks:
        anchors = list(_TIME_RANGE_RE.finditer(chunk))
        for idx, anchor in enumerate(anchors):
            seg_end = anchors[idx + 1].start() if idx + 1 < len(anchors) else len(chunk)
            segment = chunk[anchor.start():seg_end]
            start, end = anchor.groups()
            time_key = (start.zfill(5), end.zfill(5))

            if time_key not in row_candidates:
                order.append(time_key)
                row_candidates[time_key] = []

            addresses = _address_candidates(segment)
            if len(addresses) < 2:
                continue
            row_candidates[time_key].append({
                "출발지": addresses[0],
                "도착지": addresses[1],
                "결제방식": "직접" if "직접결제" in segment else "자동",
            })

    if len(order) != int(displayed_count):
        result.update(
            error_code="REPAIR_ORDER_COUNT_MISMATCH",
            message=f"행 순서 {len(order)}건과 화면 {displayed_count}건이 달라 보정을 중단했습니다.",
        )
        return result

    # 본문 OCR에서 이미 정상 추출된 요금은 보조 OCR 순번과 반드시 일치해야 한다.
    # 이 교차검증으로 "개수와 합계만 우연히 맞는 순서오류"를 차단한다.
    existing_fares = {
        (str(item.get("탑승시각") or ""), str(item.get("하차시각") or "")): int(item.get("요금") or 0)
        for item in (base.get("items") or [])
        if item.get("탑승시각") and item.get("하차시각") and item.get("요금") is not None
    }
    for idx, time_key in enumerate(order):
        if time_key in existing_fares and normalized_fares[idx] != existing_fares[time_key]:
            result.update(
                error_code="REPAIR_EXISTING_FARE_MISMATCH",
                message=(
                    f"{time_key[0]}-{time_key[1]} 기존요금 "
                    f"{existing_fares[time_key]:,}원과 보조요금 "
                    f"{normalized_fares[idx]:,}원이 달라 보정을 중단했습니다."
                ),
            )
            return result

    items = []
    for idx, time_key in enumerate(order):
        candidates = row_candidates.get(time_key) or []
        unique_pairs: dict[tuple[str, str], dict[str, str]] = {}
        for candidate in candidates:
            pair = (candidate["출발지"], candidate["도착지"])
            if pair not in unique_pairs:
                unique_pairs[pair] = dict(candidate)
            elif candidate.get("결제방식") == "직접":
                unique_pairs[pair]["결제방식"] = "직접"

        if len(unique_pairs) != 1:
            result.update(
                error_code="REPAIR_ADDRESS_AMBIGUOUS",
                message=(
                    f"{time_key[0]}-{time_key[1]} 주소 후보가 "
                    f"{len(unique_pairs)}개라 보정을 중단했습니다."
                ),
            )
            return result

        row = next(iter(unique_pairs.values()))
        items.append({
            "탑승시각": time_key[0],
            "하차시각": time_key[1],
            "출발지": row["출발지"],
            "도착지": row["도착지"],
            "요금": normalized_fares[idx],
            "결제방식": row.get("결제방식") or "자동",
        })

    repaired = dict(base)
    repaired["items"] = items
    repaired["parse_errors"] = []
    repaired["repair_used"] = True
    repaired["repair_method"] = "fare_column_probe_ordered"
    validation = validate_daily_history_document(repaired)
    if not validation.get("ok"):
        result.update(
            error_code="REPAIR_POST_VALIDATION_FAILED",
            message=validation.get("message"),
        )
        return result

    result["repaired"] = True
    result["parsed"] = repaired
    result["message"] = "PASS"
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
