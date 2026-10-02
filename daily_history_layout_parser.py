"""Layout-aware parser primitives for Kakao T daily_history OCR overlays.

Task #164 PoC only.
- No DB writes.
- Converts OCR.space TextOverlay coordinates back to original-image coordinates.
- Builds deterministic trip-card vertical bands from time anchors.
- Plans selective re-OCR only for incomplete cards.
- Enforces a hard external-OCR call budget.
"""
from __future__ import annotations

import re
import statistics
from datetime import date as _date
from typing import Any, Iterable

TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*:\s*(\d{2})\s*[-~–—]\s*"
    r"(\d{1,2})\s*:\s*(\d{2})(?!\d)"
)
FARE_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원")
HANGUL_RE = re.compile(r"[가-힣]")
CONTROL_WORDS = (
    "실시간", "결제취소", "결제 취소", "직접결제", "직접 결제",
    "일별운행", "운행이력", "총", "건", "안내", "공지",
    "요금은", "결제는", "취소", "문의", "고객센터",
)
HEADER_RE = re.compile(r"(?<!\d)(\d{1,2})\s*건\s*[/|·,:–—-]?\s*([\d,]{4,})\s*원")
DATE_RES = (
    re.compile(r"(?<!\d)(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
    re.compile(r"(?<!\d)(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?:\s*[.]?\s*일)?(?!\d)"),
)
LOCATION_RE = re.compile(
    r"[가-힣0-9]{1,20}(?:시|군|구|읍|면|동|리|로|길|역|공항|터미널|병원|아파트)"
)

DEFAULT_MAX_CARDS = 15
DEFAULT_BASE_OCR_CALLS = 2
DEFAULT_MAX_TOTAL_OCR_CALLS = 8


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def map_bbox_to_original(
    bbox: tuple[float, float, float, float],
    crop_box: tuple[float, float, float, float],
    submitted_size: tuple[float, float],
    original_size: tuple[float, float],
) -> dict[str, float]:
    """Map OCR-space bbox from submitted crop pixels to original normalized coordinates.

    bbox=(left, top, width, height) in submitted-image pixels.
    crop_box=(x0,y0,x1,y1) in original-image pixels.
    """
    left, top, width, height = map(float, bbox)
    cx0, cy0, cx1, cy1 = map(float, crop_box)
    sw, sh = map(float, submitted_size)
    ow, oh = map(float, original_size)
    if min(sw, sh, ow, oh) <= 0 or cx1 <= cx0 or cy1 <= cy0:
        raise ValueError("INVALID_COORDINATE_METADATA")

    sx = (cx1 - cx0) / sw
    sy = (cy1 - cy0) / sh
    x0 = cx0 + left * sx
    y0 = cy0 + top * sy
    x1 = x0 + width * sx
    y1 = y0 + height * sy
    return {
        "x0": _clamp01(x0 / ow),
        "y0": _clamp01(y0 / oh),
        "x1": _clamp01(x1 / ow),
        "y1": _clamp01(y1 / oh),
    }


def ocrspace_overlay_to_lines(
    parsed_result: dict[str, Any],
    *,
    chunk_index: int,
    crop_box: tuple[float, float, float, float],
    submitted_size: tuple[float, float],
    original_size: tuple[float, float],
) -> list[dict[str, Any]]:
    """Flatten one OCR.space ParsedResults item into original-normalized line records."""
    overlay = parsed_result.get("TextOverlay") or {}
    if not overlay.get("HasOverlay"):
        return []
    out: list[dict[str, Any]] = []
    for line_index, line in enumerate(overlay.get("Lines") or []):
        words = sorted(line.get("Words") or [], key=lambda w: float(w.get("Left") or 0))
        mapped_words = []
        for word in words:
            text = str(word.get("WordText") or "").strip()
            if not text:
                continue
            mapped = map_bbox_to_original(
                (
                    float(word.get("Left") or 0),
                    float(word.get("Top") or 0),
                    float(word.get("Width") or 0),
                    float(word.get("Height") or 0),
                ),
                crop_box,
                submitted_size,
                original_size,
            )
            mapped_words.append({"text": text, **mapped})
        if not mapped_words:
            continue
        text = " ".join(w["text"] for w in mapped_words)
        out.append({
            "chunk_index": chunk_index,
            "line_index": line_index,
            "text": text,
            "x0": min(w["x0"] for w in mapped_words),
            "y0": min(w["y0"] for w in mapped_words),
            "x1": max(w["x1"] for w in mapped_words),
            "y1": max(w["y1"] for w in mapped_words),
            "words": mapped_words,
        })
    return out


def dedupe_overlap_lines(
    lines: Iterable[dict[str, Any]],
    y_tolerance: float = 0.0025,
    x_tolerance: float = 0.03,
) -> list[dict[str, Any]]:
    """Remove split-overlap duplicates after coordinates are restored to the original image.

    Exact-text duplicates are removed when y is nearly identical. For different split
    chunks, a second spatial gate removes OCR variants occupying the same line geometry;
    the denser text candidate is retained. Geometry dedupe is never applied within the
    same chunk.
    """
    out: list[dict[str, Any]] = []
    for line in sorted(lines, key=lambda x: (x.get("y0", 0), x.get("x0", 0))):
        nt = _norm_text(str(line.get("text") or ""))
        duplicate_index = None
        replace_prior = False
        for idx in range(max(0, len(out) - 16), len(out)):
            prior = out[idx]
            if abs(float(line.get("y0", 0)) - float(prior.get("y0", 0))) > y_tolerance:
                continue
            prior_text = _norm_text(str(prior.get("text") or ""))
            if nt and nt == prior_text:
                duplicate_index = idx
                break

            different_chunk = line.get("chunk_index") != prior.get("chunk_index")
            same_geometry = (
                different_chunk
                and abs(float(line.get("y1", 0)) - float(prior.get("y1", 0))) <= y_tolerance * 1.5
                and abs(float(line.get("x0", 0)) - float(prior.get("x0", 0))) <= x_tolerance
                and abs(float(line.get("x1", 0)) - float(prior.get("x1", 0))) <= x_tolerance
            )
            if same_geometry:
                duplicate_index = idx
                replace_prior = len(nt) > len(prior_text)
                break

        if duplicate_index is None:
            out.append(line)
        elif replace_prior:
            out[duplicate_index] = line
    return out


def _time_anchor(line: dict[str, Any]) -> dict[str, Any] | None:
    m = TIME_RE.search(str(line.get("text") or ""))
    if not m:
        return None
    start_hour, start_minute, end_hour, end_minute = map(int, m.groups())
    if not (0 <= start_hour < 24 and 0 <= end_hour < 24
            and 0 <= start_minute < 60 and 0 <= end_minute < 60):
        return None
    return {
        "start": f"{start_hour:02d}:{start_minute:02d}",
        "end": f"{end_hour:02d}:{end_minute:02d}",
        "y0": float(line["y0"]),
        "y1": float(line["y1"]),
        "line": line,
    }


def _dedupe_anchors(anchors: list[dict[str, Any]], y_tolerance: float = 0.004) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for anchor in sorted(anchors, key=lambda x: x["y0"]):
        same = next((
            p for p in out
            if p["start"] == anchor["start"] and p["end"] == anchor["end"]
            and abs(p["y0"] - anchor["y0"]) <= y_tolerance
        ), None)
        if same is None:
            out.append(anchor)
    return out


def _fare_candidates(text: str) -> list[int]:
    return [int(x.replace(",", "")) for x in FARE_RE.findall(text or "")]


def _looks_like_address(text: str) -> bool:
    compact = _norm_text(text)
    if not compact or not HANGUL_RE.search(compact):
        return False
    if TIME_RE.search(text or "") or FARE_RE.search(text or ""):
        return False
    if any(_norm_text(w) in compact for w in CONTROL_WORDS):
        return False
    # Geography is not limited to Daegu: long-distance destinations must remain valid.
    # Unknown place names fail closed and request re-OCR rather than being guessed.
    return len(compact) >= 4 and bool(LOCATION_RE.search(compact))


def extract_header_date(lines: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Extract one valid service date from OCR overlay text above the first trip card."""
    clean = dedupe_overlap_lines(lines)
    anchors = [_time_anchor(line) for line in clean]
    first_y = min((a["y0"] for a in anchors if a), default=1.0)
    header_text = " ".join(
        str(line.get("text") or "") for line in sorted(
            (line for line in clean if float(line["y1"]) < first_y),
            key=lambda line: (line["y0"], line["x0"]),
        )
    )

    raw_matches = []
    valid_dates = set()
    for pattern in DATE_RES:
        for match in pattern.finditer(header_text):
            y, m, d = map(int, match.groups())
            raw_matches.append((y, m, d))
            try:
                value = _date(y, m, d)
            except ValueError:
                continue
            valid_dates.add(value.isoformat())

    if raw_matches and not valid_dates:
        return {"ok": False, "error_code": "LAYOUT_DATE_INVALID"}
    if len(valid_dates) != 1:
        return {"ok": False, "error_code": "LAYOUT_DATE_MISSING_OR_AMBIGUOUS"}
    return {
        "ok": True,
        "date": next(iter(valid_dates)),
        "source": "OCR_OVERLAY_HEADER",
    }


def extract_header_totals(lines: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Read displayed count and sum only from lines above the first trip anchor."""
    clean = dedupe_overlap_lines(lines)
    anchors = [_time_anchor(line) for line in clean]
    first_y = min((a["y0"] for a in anchors if a), default=1.0)
    header_text = " ".join(
        str(line.get("text") or "") for line in sorted(
            (line for line in clean if float(line["y1"]) < first_y),
            key=lambda line: (line["y0"], line["x0"]),
        )
    )
    candidates = {
        (int(count), int(amount.replace(",", "")))
        for count, amount in HEADER_RE.findall(header_text)
    }
    if len(candidates) != 1:
        return {"ok": False, "error_code": "LAYOUT_HEADER_MISSING_OR_AMBIGUOUS"}
    count, amount = next(iter(candidates))
    if not 1 <= count <= DEFAULT_MAX_CARDS or amount <= 0:
        return {"ok": False, "error_code": "LAYOUT_HEADER_INVALID"}
    return {
        "ok": True,
        "expected_count": count,
        "expected_sum": amount,
        "source": "OCR_OVERLAY_HEADER",
    }


def _card_reocr_reason(card: dict[str, Any]) -> list[str]:
    reasons = []
    if not card.get("start_time") or not card.get("end_time"):
        reasons.append("MISSING_TIME")
    if card.get("time_anchor_count", 0) > 1:
        reasons.append("AMBIGUOUS_TIME")
    if card.get("fare") is None:
        reasons.append("MISSING_FARE")
    address_count = len(card.get("address_lines") or [])
    if address_count < 2:
        reasons.append("ADDRESS_LINES_LT_2")
    elif address_count > 2:
        reasons.append("ADDRESS_LINES_AMBIGUOUS")
    return reasons


def _norm_to_px(box: dict[str, float], original_size: tuple[int, int]) -> dict[str, int]:
    ow, oh = original_size
    return {
        "left": max(0, min(ow, round(box["x0"] * ow))),
        "top": max(0, min(oh, round(box["y0"] * oh))),
        "right": max(0, min(ow, round(box["x1"] * ow))),
        "bottom": max(0, min(oh, round(box["y1"] * oh))),
    }


def _trip_fare_anchors(
    lines: list[dict[str, Any]],
    *,
    expected_count: int,
    expected_sum: int | None,
) -> list[dict[str, Any]]:
    """Return vertically ordered trip-fare anchors, excluding the header total.

    This fallback is intentionally strict. It is eligible only when the number
    of unique fare lines equals the displayed count and, when available, their
    sum equals the displayed header amount. It exists for the observed case
    where OCR misses one time row but still reads every card fare.
    """
    candidates = []
    for line in lines:
        fares = _fare_candidates(str(line.get("text") or ""))
        unique = sorted(set(fares))
        if len(unique) != 1:
            continue
        candidates.append({
            "fare": unique[0],
            "y0": float(line["y0"]),
            "y1": float(line["y1"]),
            "line": line,
        })
    candidates.sort(key=lambda x: (x["y0"], x["line"].get("x0", 0)))

    # Header total is usually the topmost currency line. Remove exactly one
    # independently-known header amount only when the candidate population has
    # more entries than the displayed trip count.
    if expected_sum is not None and len(candidates) > expected_count:
        header_hits = [
            i for i, item in enumerate(candidates)
            if int(item["fare"]) == int(expected_sum)
        ]
        if header_hits:
            candidates.pop(header_hits[0])

    if len(candidates) != int(expected_count):
        return []
    if expected_sum is not None and sum(int(x["fare"]) for x in candidates) != int(expected_sum):
        return []
    return candidates


def _fare_anchored_cards(
    clean: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    fare_anchors: list[dict[str, Any]],
    *,
    original_size: tuple[int, int],
) -> tuple[list[dict[str, Any]], float]:
    """Build card bands from fare rows and attach any time anchor inside each band."""
    fare_centers = [(x["y0"] + x["y1"]) / 2.0 for x in fare_anchors]
    gaps = [
        fare_centers[i + 1] - fare_centers[i]
        for i in range(len(fare_centers) - 1)
        if fare_centers[i + 1] > fare_centers[i]
    ]
    median_gap = statistics.median(gaps) if gaps else 0.08
    if median_gap <= 0:
        raise ValueError("LAYOUT_INVALID_FARE_GEOMETRY")

    cards = []
    for idx, (fare_anchor, fare_y) in enumerate(zip(fare_anchors, fare_centers)):
        if idx == 0:
            y0 = max(0.0, fare_y - median_gap * 0.88)
        else:
            prev_y = fare_centers[idx - 1]
            y0 = min(fare_y, prev_y + min(0.010, (fare_y - prev_y) * 0.16))
        y1 = min(1.0, fare_y + min(0.010, median_gap * 0.16))

        card_lines = [
            line for line in clean
            if y0 <= (float(line["y0"]) + float(line["y1"])) / 2.0 < y1
        ]
        card_lines.sort(key=lambda x: (x["y0"], x["x0"]))
        card_anchors = [
            a for a in anchors
            if y0 <= (a["y0"] + a["y1"]) / 2.0 < y1
        ]
        card_anchors.sort(key=lambda x: x["y0"])
        chosen = card_anchors[0] if len(card_anchors) == 1 else None
        text = "\n".join(str(x.get("text") or "") for x in card_lines)
        addresses = [
            x["text"] for x in card_lines
            if _looks_like_address(str(x.get("text") or ""))
        ]
        direct = "직접결제" in _norm_text(text)
        card = {
            "card_index": idx + 1,
            "start_time": chosen["start"] if chosen else None,
            "end_time": chosen["end"] if chosen else None,
            "time_anchor_count": len(card_anchors),
            "bbox_norm": {"x0": 0.0, "y0": y0, "x1": 1.0, "y1": y1},
            "fare": int(fare_anchor["fare"]),
            "fare_candidates": [int(fare_anchor["fare"])],
            "address_lines": addresses[:4],
            "payment": "직접" if direct else "미확인",
            "payment_evidence": "DIRECT_LABEL" if direct else "NO_DIRECT_LABEL",
            "line_count": len(card_lines),
            "anchor_strategy": "FARE_ANCHOR_FALLBACK",
        }
        card["reocr_reasons"] = _card_reocr_reason(card)
        card["full_crop_px"] = _norm_to_px(card["bbox_norm"], original_size)
        card["fare_crop_px"] = _norm_to_px(
            {"x0": 0.55, "y0": y0, "x1": 1.0, "y1": y1},
            original_size,
        )
        cards.append(card)
    return cards, median_gap


def build_card_layout(
    lines: Iterable[dict[str, Any]],
    *,
    original_size: tuple[int, int],
    expected_count: int | None = None,
    expected_sum: int | None = None,
    max_cards: int = DEFAULT_MAX_CARDS,
    base_ocr_calls: int = DEFAULT_BASE_OCR_CALLS,
    max_total_ocr_calls: int = DEFAULT_MAX_TOTAL_OCR_CALLS,
) -> dict[str, Any]:
    """Create deterministic trip-card bands and a bounded selective re-OCR plan.

    Primary strategy uses time anchors. If exactly the displayed trip count of
    fare anchors is independently available (and their sum matches the header),
    fare-anchor segmentation may recover a missing time line. Missing time is
    never inferred; that card must pass selective re-OCR before final validation.
    """
    clean = dedupe_overlap_lines(lines)
    anchors = _dedupe_anchors([a for a in (_time_anchor(x) for x in clean) if a])

    base = {
        "ok": False,
        "status": "FAIL_CLOSED",
        "error_code": None,
        "detected_card_count": len(anchors),
        "detected_time_anchor_count": len(anchors),
        "expected_count": expected_count,
        "expected_sum": expected_sum,
        "cards": [],
        "reocr_card_indices": [],
        "base_ocr_calls": base_ocr_calls,
        "max_total_ocr_calls": max_total_ocr_calls,
        "planned_total_ocr_calls": base_ocr_calls,
        "anchor_strategy": "TIME_ANCHOR",
    }

    if not anchors:
        return {**base, "error_code": "LAYOUT_NO_TIME_ANCHORS"}
    if len(anchors) > max_cards:
        return {**base, "error_code": "LAYOUT_CARD_COUNT_EXCEEDS_MAX"}

    # A count mismatch no longer fails immediately. If every fare line is read
    # and independently agrees with the header, use fare rows to segment cards
    # and selectively re-OCR only the cards whose time line is absent/ambiguous.
    if expected_count is not None and len(anchors) != int(expected_count):
        fare_anchors = _trip_fare_anchors(
            clean,
            expected_count=int(expected_count),
            expected_sum=expected_sum,
        )
        if not fare_anchors:
            return {**base, "error_code": "LAYOUT_CARD_COUNT_MISMATCH"}
        try:
            cards, median_gap = _fare_anchored_cards(
                clean, anchors, fare_anchors, original_size=original_size
            )
        except ValueError:
            return {**base, "error_code": "LAYOUT_INVALID_FARE_GEOMETRY"}

        reocr = [card["card_index"] for card in cards if card["reocr_reasons"]]
        planned = base_ocr_calls + len(reocr)
        out = {
            **base,
            "ok": True,
            "status": "READY_FOR_SELECTIVE_REOCR" if reocr else "COMPLETE_LAYOUT",
            "detected_card_count": len(cards),
            "cards": cards,
            "reocr_card_indices": reocr,
            "planned_total_ocr_calls": planned,
            "median_anchor_gap_norm": median_gap,
            "anchor_strategy": "FARE_ANCHOR_FALLBACK",
        }
        if planned > max_total_ocr_calls:
            out.update(
                ok=False,
                status="FAIL_CLOSED",
                error_code="LAYOUT_REOCR_BUDGET_EXCEEDED",
            )
        return out

    gaps = [anchors[i + 1]["y0"] - anchors[i]["y0"] for i in range(len(anchors) - 1)]
    median_gap = statistics.median(gaps) if gaps else 0.08
    if median_gap <= 0:
        return {**base, "error_code": "LAYOUT_INVALID_ANCHOR_GEOMETRY"}

    cards: list[dict[str, Any]] = []
    for idx, anchor in enumerate(anchors):
        y0 = max(0.0, anchor["y0"] - min(0.003, median_gap * 0.06))
        y1 = (
            max(y0, anchors[idx + 1]["y0"] - min(0.003, median_gap * 0.06))
            if idx + 1 < len(anchors)
            else min(1.0, anchor["y0"] + median_gap)
        )
        card_lines = [
            line for line in clean
            if y0 <= (float(line["y0"]) + float(line["y1"])) / 2 < y1
        ]
        card_lines.sort(key=lambda x: (x["y0"], x["x0"]))
        text = "\n".join(str(x.get("text") or "") for x in card_lines)
        fares = _fare_candidates(text)
        fare = fares[0] if len(set(fares)) == 1 else None
        address_lines = [
            x["text"] for x in card_lines
            if _looks_like_address(str(x.get("text") or ""))
        ]
        direct = "직접결제" in _norm_text(text)
        card = {
            "card_index": idx + 1,
            "start_time": anchor["start"],
            "end_time": anchor["end"],
            "time_anchor_count": 1,
            "bbox_norm": {"x0": 0.0, "y0": y0, "x1": 1.0, "y1": y1},
            "fare": fare,
            "fare_candidates": fares,
            "address_lines": address_lines[:4],
            "payment": "직접" if direct else "미확인",
            "payment_evidence": "DIRECT_LABEL" if direct else "NO_DIRECT_LABEL",
            "line_count": len(card_lines),
            "anchor_strategy": "TIME_ANCHOR",
        }
        card["reocr_reasons"] = _card_reocr_reason(card)
        card["full_crop_px"] = _norm_to_px(card["bbox_norm"], original_size)
        card["fare_crop_px"] = _norm_to_px(
            {"x0": 0.55, "y0": y0, "x1": 1.0, "y1": y1},
            original_size,
        )
        cards.append(card)

    reocr = [card["card_index"] for card in cards if card["reocr_reasons"]]
    planned = base_ocr_calls + len(reocr)
    if planned > max_total_ocr_calls:
        return {
            **base,
            "cards": cards,
            "reocr_card_indices": reocr,
            "planned_total_ocr_calls": planned,
            "median_anchor_gap_norm": median_gap,
            "error_code": "LAYOUT_REOCR_BUDGET_EXCEEDED",
        }

    return {
        **base,
        "ok": True,
        "status": "COMPLETE_LAYOUT" if not reocr else "READY_FOR_SELECTIVE_REOCR",
        "cards": cards,
        "reocr_card_indices": reocr,
        "planned_total_ocr_calls": planned,
        "median_anchor_gap_norm": median_gap,
    }
