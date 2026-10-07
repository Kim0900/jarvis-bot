"""task#150 (TAEO 발주, 대표 착수승인 2026-09-28) — S700 JSONL → s700_trips staging.

원칙 (TAEO 지시 §A~§J 그대로):
- LLM/AI 호출 없음. 전부 결정론적. 이 모듈은 네트워크·DB에 접근하지 않는 순수 함수 모음이다
  (DB 입출력은 bot_v5_legacy.py 쪽 얇은 어댑터가 담당) — 그래서 로컬에서 실데이터 기준으로 재현검증 가능.
- raw_calls에는 어떤 경우에도 INSERT/UPDATE 하지 않는다. 매칭 결과는 s700_trips 내부
  (matched_raw_call_id/match_status/match_method/match_detail)에만 기록한다.
- Fail-Closed: 수용조건 하나라도 못 맞추면 저장하지 않고 사유별 카운트만 반환한다.
- 매칭은 전역 단일 ±3분 규칙 금지. 콜유형별로 근거(evidence)를 다르게 본다(TAEO §F, §I).
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
DECODER_PREFIX = "s700-decoder-"
LONG_FRAME_LEN = 98
MAX_TRIP_SECONDS = 12 * 3600          # 12시간 초과 Trip은 손상값으로 간주(Golden Day 최장 시외 24분)
FUTURE_TOLERANCE_SEC = 600            # trip_start가 수신시각보다 10분 넘게 미래면 손상
TWELVE_DIGITS = re.compile(r"^\d{12}$")

# ── 매칭 임계값 (근거를 match_detail에 그대로 남긴다) ──────────────────────
KAKAO_START_TOL_SEC = 180             # TAEO 2026-09-02 실측(start 8~79s, end 0~92s) 기반 "1차 Kakao 후보 규칙"
KAKAO_END_TOL_SEC = 180               # ※ Kakao 전용. 전역규칙으로 쓰지 않는다.
UBER_CANDIDATE_START_TOL_SEC = 900    # Uber는 배차→미터시작이 ~6분 벌어짐(2026-09-06 실측). 후보탐색 창일 뿐 판정근거 아님.
UBER_DURATION_TOL_MIN = 1.0
UBER_DISTANCE_TOL_RATIO = 0.03


# ══════════════════════════════════════════════════════════════════════
# 1. 파싱 / 검증 / 정규화
# ══════════════════════════════════════════════════════════════════════
def parse_jsonl(text):
    """JSONL 텍스트 → (레코드 리스트, 통계). 깨진 줄은 버리지 않고 카운트한다(조용한 유실 금지)."""
    records, stats = [], {"lines_total": 0, "invalid_json": 0}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        stats["lines_total"] += 1
        try:
            obj = json.loads(line)
        except Exception:
            stats["invalid_json"] += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            stats["invalid_json"] += 1
    return records, stats


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_meter_dt(s12):
    """'yyMMddHHmmss'(KST 미터기 시계) → aware datetime. 형식/달력 불량이면 None."""
    if not isinstance(s12, str) or not TWELVE_DIGITS.match(s12):
        return None
    try:
        return datetime.strptime(s12, "%y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None


def _parse_received_at(s):
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def _redecode_ascii(ascii_s):
    """ascii(98자)를 Kotlin Decoder(v0.4)와 동일 오프셋으로 다시 해독. JSON 필드와의 교차확인용.
    오프셋: state[18:20]/[20:22], fare[22:28], start[44:56], end[56:68], dist[68:76], aux[82:84],
    fare_copy[84:90], checksum[96:98] = (-sum(ascii[0:96])) & 0xFF."""
    if not isinstance(ascii_s, str) or len(ascii_s) != LONG_FRAME_LEN:
        return None
    try:
        calc = (-sum(ascii_s[:96].encode("ascii"))) & 0xFF
        recv = int(ascii_s[96:98], 16)
    except (UnicodeEncodeError, ValueError):
        return None
    try:
        return {
            "checksum_ok": calc == recv,
            "primary_state": ascii_s[18:20],
            "secondary_state": ascii_s[20:22],
            "meter_fare": int(ascii_s[22:28]),
            "trip_start": ascii_s[44:56],
            "trip_end": ascii_s[56:68],
            "distance_raw": int(ascii_s[68:76]),
            "aux_flag": ascii_s[82:84],
            "fare_copy": int(ascii_s[84:90]),
        }
    except ValueError:
        return None


def validate_frame(rec):
    """TAEO §B 수용조건(Fail-Closed). (ok, reason) 반환. reason은 거절 사유 카운트 키."""
    if rec.get("type") != "frame":
        return False, "not_frame"
    if rec.get("trip_closed") is not True:
        return False, "trip_not_closed"
    if rec.get("checksum_valid") is not True:
        return False, "checksum_invalid"
    if rec.get("decode_warning") is not None:
        return False, "decode_warning"
    fare, fare_copy = rec.get("meter_fare"), rec.get("fare_copy")
    if not (_is_int(fare) and _is_int(fare_copy)):
        return False, "fare_not_integer"
    if fare != fare_copy:
        return False, "fare_copy_mismatch"
    if fare <= 0:
        return False, "fare_nonpositive"
    start, end = _parse_meter_dt(rec.get("trip_start")), _parse_meter_dt(rec.get("trip_end"))
    if start is None or end is None:
        return False, "trip_time_invalid"
    if not (_is_int(rec.get("distance_raw")) and rec["distance_raw"] >= 0):
        return False, "distance_invalid"
    if end < start:
        return False, "end_before_start"
    if (end - start).total_seconds() > MAX_TRIP_SECONDS:
        return False, "duration_out_of_range"
    recv = _parse_received_at(rec.get("received_at"))
    if recv is not None and (start - recv).total_seconds() > FUTURE_TOLERANCE_SEC:
        return False, "trip_start_in_future"
    # 결정론적 재해독: JSON 필드가 원본 ascii와 다르면(디코더 버전 드리프트/변조) 저장하지 않는다.
    redec = _redecode_ascii(rec.get("ascii"))
    if redec is None:
        return False, "ascii_missing_or_malformed"
    if not redec["checksum_ok"]:
        return False, "ascii_checksum_recheck_failed"
    for k in ("primary_state", "secondary_state", "meter_fare", "trip_start", "trip_end",
              "distance_raw", "aux_flag", "fare_copy"):
        if rec.get(k) != redec[k]:
            return False, "ascii_field_mismatch"
    dv = rec.get("decoder_version")
    if not (isinstance(dv, str) and dv.startswith(DECODER_PREFIX)):
        return False, "decoder_version_unknown"
    return True, None


def make_trip_key(trip_start, trip_end, fare, distance_raw):
    """TAEO §D: SHA-256 of s700|trip_start|trip_end|fare|distance_raw (원문 문자열 그대로 사용)."""
    basis = f"s700|{trip_start}|{trip_end}|{fare}|{distance_raw}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def build_row(rec, source_file_id, source_file_name):
    start, end = _parse_meter_dt(rec["trip_start"]), _parse_meter_dt(rec["trip_end"])
    return {
        "trip_key": make_trip_key(rec["trip_start"], rec["trip_end"], rec["meter_fare"], rec["distance_raw"]),
        "source_file_id": source_file_id,
        "source_file_name": source_file_name,
        "trip_start": start.isoformat(),
        "trip_end": end.isoformat(),
        "fare": rec["meter_fare"],
        "distance_raw": rec["distance_raw"],
        "distance_km": round(rec["distance_raw"] / 10000.0, 4),
        "decoder_version": rec["decoder_version"],
        "checksum_valid": True,
        "primary_state": rec["primary_state"],
        "secondary_state": rec["secondary_state"],
        "aux_flag": rec["aux_flag"],
        "raw_record": rec,
        "match_status": "PENDING",
    }


def extract_trips(text, source_file_id, source_file_name):
    """JSONL → (수용 행 리스트, 통계). 같은 trip_key는 1행으로 합치고, 같은 trip_start에 값이 다른
    Trip이 둘 이상이면(충돌) 전부 저장하지 않는다(어느 쪽이 맞는지 기계가 판단 못함 → Fail-Closed)."""
    records, stats = parse_jsonl(text)
    stats.update({"frames": 0, "accepted_frames": 0, "rejected": {}, "duplicate_in_input": 0,
                  "conflict_trip_start": 0})
    by_key, order = {}, []
    for rec in records:
        if rec.get("type") != "frame":
            continue
        stats["frames"] += 1
        ok, reason = validate_frame(rec)
        if not ok:
            # 진행중/미종료 프레임(trip_not_closed)은 정상 흐름이라 별도 집계만 하고 소음으로 보지 않는다.
            stats["rejected"][reason] = stats["rejected"].get(reason, 0) + 1
            continue
        stats["accepted_frames"] += 1
        row = build_row(rec, source_file_id, source_file_name)
        if row["trip_key"] in by_key:
            stats["duplicate_in_input"] += 1
            continue
        by_key[row["trip_key"]] = row
        order.append(row["trip_key"])
    groups = {}
    for k in order:
        groups.setdefault(by_key[k]["raw_record"]["trip_start"], []).append(k)
    rows = []
    for k in order:
        g = groups[by_key[k]["raw_record"]["trip_start"]]
        if len(g) > 1:
            stats["conflict_trip_start"] += 1
            continue
        rows.append(by_key[k])
    stats["trips"] = len(rows)
    return rows, stats


# ══════════════════════════════════════════════════════════════════════
# 2. 매칭 (staging 내부 기록 전용 — raw_calls는 읽기만)
# ══════════════════════════════════════════════════════════════════════
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _call_datetimes(call):
    """raw_calls 행 → (start_dt, end_dt|None). 날짜=플랫폼 캘린더 날짜, 하차가 배차보다 이르면 익일."""
    d = call.get("날짜")
    m = _HHMM.match(str(call.get("배차시각") or ""))
    if not d or not m:
        return None, None
    try:
        base = datetime.strptime(str(d)[:10], "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        return None, None
    start = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0))
    end = None
    m2 = _HHMM.match(str(call.get("하차시각") or ""))
    if m2:
        end = base.replace(hour=int(m2.group(1)), minute=int(m2.group(2)), second=int(m2.group(3) or 0))
        if end < start:
            end += timedelta(days=1)
    return start, end


def is_candidate_row(call):
    """개별 Trip 신원(CALL_CARD)으로 볼 수 있는 raw_calls 행만. 요약/합계/무효화 행은 제외."""
    if call.get("raw_row_type") not in (None, "trip"):
        return False
    if call.get("콜유형") == "합계" or call.get("data_source") == "app_ocr_summary":
        return False
    if call.get("verify_status") == "invalidated_duplicate":
        return False
    if not isinstance(call.get("요금"), int) or not call.get("배차시각"):
        return False
    return True


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def evaluate_candidate(trip, call):
    """단일 후보 평가 → dict(verdict, method, evidence, ...).

    Uber note:
    - Uber 화면 상단 금액은 플랫폼 정산/세금 반영 수입액일 수 있어 S700
      meter_fare와 동일값을 강제하지 않는다.
    - Uber 자동확정은 기존 15분 후보창 + 구조화 운행시간/거리 일치로만 한다.
    - 요금은 audit evidence로만 남긴다.
    """
    t_start = datetime.fromisoformat(trip["trip_start"])
    t_end = datetime.fromisoformat(trip["trip_end"])
    c_start, c_end = _call_datetimes(call)
    call_amount = call.get("요금")
    meter_fare = trip.get("fare")
    ev = {
        "call_id": call.get("id"),
        "call_type": call.get("콜유형"),
        "fare_exact": call_amount == meter_fare,
        "uber_amount": call_amount,
        "meter_fare": meter_fare,
    }
    if (
        isinstance(call_amount, (int, float))
        and isinstance(meter_fare, (int, float))
    ):
        ev["fare_delta"] = call_amount - meter_fare

    if c_start is None:
        return {"verdict": "NO", "evidence": {**ev, "reason": "call_time_unparseable"}}

    signed_start_delta = int((t_start - c_start).total_seconds())
    ev["start_delta_sec"] = abs(signed_start_delta)
    ev["call_to_meter_start_sec"] = signed_start_delta
    if c_end is not None:
        ev["end_delta_sec"] = int(abs((t_end - c_end).total_seconds()))

    ctype = call.get("콜유형")

    if ctype == "카카오T":
        if not ev["fare_exact"]:
            return {"verdict": "NO", "evidence": {**ev, "reason": "fare_mismatch"}}
        # TAEO 검증(2026-09-02 9/9): fare exact + start<=180s + end<=180s(존재 시).
        ok_start = ev["start_delta_sec"] <= KAKAO_START_TOL_SEC
        ok_end = ("end_delta_sec" not in ev) or ev["end_delta_sec"] <= KAKAO_END_TOL_SEC
        ev["rule"] = "kakao_fare_exact+start<=180s+end<=180s"
        if ok_start and ok_end:
            return {"verdict": "MATCH", "method": "kakao_fare_time", "evidence": ev}
        return {"verdict": "NO", "evidence": {**ev, "reason": "kakao_time_out_of_tolerance"}}

    if ctype == "우버":
        # Uber 호출은 픽업 이동 때문에 미터 시작보다 선행할 수 있다.
        # 후보창은 기존 15분을 유지하고, 실제 동일운행 확정은 duration+distance로 한다.
        dur_call = _num(call.get("운행시간_분"))
        dist_call = _num(call.get("주행거리_km"))
        if dist_call is None:
            dist_call = _num(call.get("영업거리_km"))

        ev["rule"] = "uber_start<=15m+duration<=1m+distance<=3pct;fare_advisory_only"

        if ev["start_delta_sec"] > UBER_CANDIDATE_START_TOL_SEC:
            return {
                "verdict": "NO",
                "evidence": {**ev, "reason": "uber_start_outside_candidate_window"},
            }

        if dur_call is None or dist_call is None:
            ev["structured_fields_available"] = False
            return {
                "verdict": "PROVISIONAL",
                "method": "uber_time_only_structured_missing",
                "evidence": ev,
            }

        dur_trip = (t_end - t_start).total_seconds() / 60.0
        ev["duration_diff_min"] = round(abs(dur_trip - dur_call), 2)
        ev["distance_diff_ratio"] = round(
            abs(trip["distance_km"] - dist_call) / max(dist_call, 0.01),
            4,
        )

        if (
            ev["duration_diff_min"] <= UBER_DURATION_TOL_MIN
            and ev["distance_diff_ratio"] <= UBER_DISTANCE_TOL_RATIO
        ):
            return {
                "verdict": "MATCH",
                "method": "uber_time_duration_distance",
                "evidence": ev,
            }

        return {
            "verdict": "NO",
            "evidence": {**ev, "reason": "uber_duration_or_distance_mismatch"},
        }

    # 그 외 콜유형(NULL/배회 등): 기존대로 fare exact + 시간창만으로도 자동확정 금지.
    if not ev["fare_exact"]:
        return {"verdict": "NO", "evidence": {**ev, "reason": "fare_mismatch"}}
    ev["rule"] = "other_type_requires_manual_review"
    if ev["start_delta_sec"] <= UBER_CANDIDATE_START_TOL_SEC:
        return {
            "verdict": "PROVISIONAL",
            "method": "other_type_fare_only",
            "evidence": ev,
        }
    return {
        "verdict": "NO",
        "evidence": {**ev, "reason": "other_type_time_out_of_window"},
    }

def match_trip(trip, calls, claimed_call_ids=frozenset()):
    """S700 Trip 1건 vs raw_calls 후보들 → (match_status, matched_raw_call_id, match_method, match_detail).
    상태: MATCHED / AMBIGUOUS / PROVISIONAL / UNMATCHED. 자동 raw_calls 생성은 없다(TAEO §F, §G)."""
    considered, verdict_rows = [], []
    for call in calls:
        if not is_candidate_row(call):
            continue
        r = evaluate_candidate(trip, call)
        r["call_id"] = call.get("id")
        considered.append(r["evidence"])
        verdict_rows.append(r)
    matches = [r for r in verdict_rows if r["verdict"] == "MATCH"]
    provisionals = [r for r in verdict_rows if r["verdict"] == "PROVISIONAL"]
    detail = {"candidates_considered": len(considered), "claimed_excluded": [],
              "evidence": considered[:8]}

    free = [r for r in matches if r["call_id"] not in claimed_call_ids]
    for r in matches:
        if r["call_id"] in claimed_call_ids:
            detail["claimed_excluded"].append(r["call_id"])

    if len(free) == 1:
        r = free[0]
        return "MATCHED", r["call_id"], r["method"], {**detail, "chosen": r["evidence"]}
    if len(free) > 1:
        detail["ambiguous_call_ids"] = [r["call_id"] for r in free]
        return "AMBIGUOUS", None, "multiple_candidates", detail
    if matches:  # 근거는 충족했지만 이미 다른 S700 Trip이 점유한 행뿐
        return "AMBIGUOUS", None, "candidate_already_claimed", detail
    if len(provisionals) == 1:
        r = provisionals[0]
        return "PROVISIONAL", None, r["method"], {**detail, "provisional_call_id": r["call_id"],
                                                    "note": "근거부족 — 자동확정 안 함, 구조필드 보강 후 재매칭"}
    if len(provisionals) > 1:
        detail["ambiguous_call_ids"] = [r["call_id"] for r in provisionals]
        return "AMBIGUOUS", None, "multiple_provisional", detail
    return "UNMATCHED", None, "no_candidate", detail


# ══════════════════════════════════════════════════════════════════════
# 3. DB 오케스트레이션 (어댑터 주입 — 이 모듈은 여전히 네트워크를 직접 쓰지 않는다)
#    sb 어댑터 계약(실패 시 반드시 예외. None/[] 로 실패를 삼키면 "조용한 성공"이 되므로 금지):
#        await sb.get(table, params_dict)  -> list
#        await sb.insert(table, row_dict)  -> dict | list
#        await sb.patch(path, data_dict)   -> None
# ══════════════════════════════════════════════════════════════════════
RAW_CALL_COLS = ("id,날짜,배차시각,하차시각,요금,콜유형,운행시간_분,주행거리_km,영업거리_km,"
                 "raw_row_type,data_source,verify_status")
REMATCH_STATUSES = ("PENDING", "UNMATCHED", "PROVISIONAL", "AMBIGUOUS")


def _dt(v):
    if isinstance(v, datetime):
        return v
    s = str(v)
    return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)


def _utc(v):
    return _dt(v).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _fetch_raw_calls(sb, trips):
    starts = [_dt(t["trip_start"]) for t in trips]
    lo = (min(starts) - timedelta(days=1)).strftime("%Y-%m-%d")
    hi = (max(starts) + timedelta(days=1)).strftime("%Y-%m-%d")
    return await sb.get("raw_calls", {"select": RAW_CALL_COLS,
                                      "and": f"(날짜.gte.{lo},날짜.lte.{hi})", "limit": "5000"})


async def _claimed_call_ids(sb, exclude_trip_ids=()):
    rows = await sb.get("s700_trips", {"select": "id,matched_raw_call_id",
                                       "matched_raw_call_id": "not.is.null", "limit": "20000"})
    ex = set(exclude_trip_ids)
    return {r["matched_raw_call_id"] for r in rows if r["id"] not in ex}


async def _match_and_write(sb, trips, dry_run):
    """trips 를 시작시각 순으로 매칭하고 s700_trips 에만 기록한다(raw_calls 는 읽기 전용)."""
    if not trips:
        return {}, []
    raw = await _fetch_raw_calls(sb, trips)
    claimed = set(await _claimed_call_ids(sb, exclude_trip_ids=[t.get("id") for t in trips if t.get("id")]))
    summary, preview = {}, []
    for t in sorted(trips, key=lambda x: _dt(x["trip_start"])):
        status, rid, method, detail = match_trip(t, raw, frozenset(claimed))
        if rid is not None:
            claimed.add(rid)
        summary[status] = summary.get(status, 0) + 1
        preview.append({"trip_start": t["trip_start"], "fare": t["fare"], "status": status,
                        "matched_raw_call_id": rid, "method": method})
        if not dry_run and t.get("id") is not None:
            await sb.patch(f"s700_trips?id=eq.{t['id']}", {
                "match_status": status, "matched_raw_call_id": rid,
                "match_method": method, "match_detail": detail})
    return summary, preview


async def ingest_jsonl(sb, text, source_file_id=None, source_file_name=None, dry_run=False):
    rows, stats = extract_trips(text, source_file_id, source_file_name)
    report = {"success": True, "dry_run": bool(dry_run), "source_file_id": source_file_id,
              "source_file_name": source_file_name, "stats": stats, "inserted": 0,
              "duplicates_existing": 0, "conflicts_existing": [], "insert_failed": [], "match": {}}
    if not rows:
        return report

    starts = [_dt(r["trip_start"]) for r in rows]
    lo, hi = _utc(min(starts) - timedelta(days=1)), _utc(max(starts) + timedelta(days=1))
    existing = await sb.get("s700_trips", {"select": "id,trip_key,trip_start",
                                           "and": f"(trip_start.gte.{lo},trip_start.lte.{hi})",
                                           "limit": "20000"})
    by_key = {e["trip_key"] for e in existing}
    by_start = {_dt(e["trip_start"]): e for e in existing}

    todo = []
    for r in rows:
        if r["trip_key"] in by_key:
            report["duplicates_existing"] += 1
            continue
        clash = by_start.get(_dt(r["trip_start"]))
        if clash is not None:
            # 같은 trip_start 인데 값이 다른 Trip: 이중 Trip 위험 -> 저장하지 않고 이상징후로 보고(Fail-Closed)
            report["conflicts_existing"].append({"trip_start": r["trip_start"], "new_trip_key": r["trip_key"],
                                                 "existing_trip_key": clash["trip_key"], "fare": r["fare"]})
            continue
        todo.append(r)

    inserted = []
    for r in todo:
        if dry_run:
            inserted.append(dict(r))
            continue
        try:
            res = await sb.insert("s700_trips", r)
            got = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
            inserted.append({**r, "id": got.get("id")})
        except Exception as e:  # 경합/제약위반 등 — 다른 행 처리는 계속하고 실패는 그대로 보고
            report["insert_failed"].append({"trip_key": r["trip_key"], "trip_start": r["trip_start"],
                                            "error": str(e)[:200]})
    report["inserted"] = len(inserted)

    summary, preview = await _match_and_write(sb, inserted, dry_run)
    report["match"] = summary
    if dry_run:
        report["match_preview"] = preview
    return report


async def rematch(sb, dry_run=False, limit=500):
    """PENDING/UNMATCHED/PROVISIONAL/AMBIGUOUS 를 재평가한다. MATCHED 는 건드리지 않는다(flapping 방지).
    단, MATCHED 인데 raw_calls 삭제로 연결이 끊긴(matched_raw_call_id IS NULL) 행은 자가치유로 재평가한다."""
    statuses = ",".join(REMATCH_STATUSES)
    trips = await sb.get("s700_trips", {
        "select": "id,trip_key,trip_start,trip_end,fare,distance_km,match_status",
        "or": f"(match_status.in.({statuses}),and(match_status.eq.MATCHED,matched_raw_call_id.is.null))",
        "order": "trip_start.asc", "limit": str(int(limit))})
    report = {"success": True, "dry_run": bool(dry_run), "evaluated": len(trips), "match": {}, "changed": 0}
    if not trips:
        return report
    summary, preview = await _match_and_write(sb, trips, dry_run)
    before = {_dt(t["trip_start"]): t["match_status"] for t in trips}
    report["match"] = summary
    report["changed"] = sum(1 for p in preview if before.get(_dt(p["trip_start"])) != p["status"])
    if dry_run:
        report["match_preview"] = preview
    return report
