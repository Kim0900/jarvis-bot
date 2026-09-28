import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import copy
from datetime import datetime, timedelta
import s700_ingest as S

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


def make_ascii(start, end, fare, dist_raw, primary="0A", secondary="08", aux="01", fare_copy=None):
    """Kotlin Decoder v0.4와 동일 오프셋으로 98자 payload 생성 (checksum 포함)."""
    fare_copy = fare if fare_copy is None else fare_copy
    buf = ["0"] * 96
    def put(a, b, s):
        assert len(s) == b - a, (a, b, s)
        buf[a:b] = list(s)
    put(0, 12, start[:12])                      # meter time raw(임의, 검증대상 아님)
    put(18, 20, primary); put(20, 22, secondary)
    put(22, 28, f"{fare:06d}")
    put(44, 56, start); put(56, 68, end)
    put(68, 76, f"{dist_raw:08d}")
    put(82, 84, aux); put(84, 90, f"{fare_copy:06d}")
    body = "".join(buf)
    calc = (-sum(body.encode("ascii"))) & 0xFF
    return body + f"{calc:02X}"


def make_frame(start, end, fare, dist_raw, received_at=None, **kw):
    a = make_ascii(start, end, fare, dist_raw, **{k: v for k, v in kw.items() if k in ("primary", "secondary", "aux", "fare_copy")})
    d = S._redecode_ascii(a)
    rec = {
        "type": "frame", "received_at": received_at or "2026-09-03T00:17:40+09:00",
        "meter_time_raw": None, "ascii": a, "hex": a.encode().hex(), "payload_length": 98,
        "frame_class": "LONG_BUSINESS_FRAME", "checksum_received": a[96:98], "checksum_calculated": a[96:98],
        "checksum_valid": True, "meter_fare": d["meter_fare"], "trip_start": d["trip_start"], "trip_end": d["trip_end"],
        "distance_raw": d["distance_raw"], "fare_copy": d["fare_copy"], "aux_flag": d["aux_flag"],
        "primary_state": d["primary_state"], "secondary_state": d["secondary_state"],
        "decode_warning": None, "trip_closed": True, "decoder_version": "s700-decoder-v0.4",
    }
    rec.update({k: v for k, v in kw.items() if k not in ("primary", "secondary", "aux", "fare_copy")})
    return rec


def jl(*recs):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n"


print("== 1. 파싱/검증 ==")
# TAEO 실측: 9/2 자정횡단 첫 Trip 23:58:39→00:17:36 13,500원 (파일은 s700_20260903)
mid = make_frame("260902235839", "260903001736", 13500, 132500)
rows, st = S.extract_trips(jl(mid), "fid1", "s700_20260903.jsonl")
check("자정횡단 Trip 1건 수용", len(rows) == 1 and st["accepted_frames"] == 1, st)
r0 = rows[0]
check("trip_start KST +09:00 정규화", r0["trip_start"] == "2026-09-02T23:58:39+09:00", r0["trip_start"])
check("trip_end 익일 정상 계산", r0["trip_end"] == "2026-09-03T00:17:36+09:00", r0["trip_end"])
check("distance_km = raw/10000", r0["distance_km"] == 13.25, r0["distance_km"])
check("trip_key 64자 sha256", len(r0["trip_key"]) == 64)
check("trip_key = sha256(s700|start|end|fare|dist)",
      r0["trip_key"] == S.make_trip_key("260902235839", "260903001736", 13500, 132500))
check("match_status 초기 PENDING", r0["match_status"] == "PENDING")

# 재처리 멱등: 같은 파일 2회 replay + 입력 내 중복프레임
rows_a, _ = S.extract_trips(jl(mid), "fid1", "f")
rows_b, st_b = S.extract_trips(jl(mid, mid, mid), "fid1", "f")
check("동일 파일 replay → 동일 trip_key", [r["trip_key"] for r in rows_a] == [r["trip_key"] for r in rows_b])
check("입력 내 중복프레임 1행으로 합침", len(rows_b) == 1 and st_b["duplicate_in_input"] == 2, st_b)

# 시외 1A/08/51 variant (표본 1건, 필드의미 확정 안 하고 그대로 수용)
out = make_frame("260830195933", "260830202324", 16200, 149600, primary="1A", secondary="08", aux="51")
rows_o, _ = S.extract_trips(jl(out), "f", "f")
check("1A/08/51 시외 Trip 수용(14.960km)", len(rows_o) == 1 and rows_o[0]["distance_km"] == 14.96)

def rej(rec, expect):
    rows, st = S.extract_trips(jl(rec), "f", "f")
    ok = len(rows) == 0 and st["rejected"].get(expect) == 1
    check(f"거절: {expect}", ok, st)

prog = copy.deepcopy(mid); prog["trip_closed"] = False
rej(prog, "trip_not_closed")
bad = copy.deepcopy(mid); bad["checksum_valid"] = False
rej(bad, "checksum_invalid")
warn = copy.deepcopy(mid); warn["decode_warning"] = "unknown_state(01/01,aux=00)"
rej(warn, "decode_warning")
fm = make_frame("260902235839", "260903001736", 13500, 132500, fare_copy=13400)
rej(fm, "fare_copy_mismatch")
tamp = copy.deepcopy(mid); tamp["meter_fare"] = 99999; tamp["fare_copy"] = 99999
rej(tamp, "ascii_field_mismatch")
badck = copy.deepcopy(mid); badck["ascii"] = mid["ascii"][:96] + "00"
rej(badck, "ascii_checksum_recheck_failed")
noasc = copy.deepcopy(mid); noasc.pop("ascii")
rej(noasc, "ascii_missing_or_malformed")
rej(make_frame("261399235839", "261399001736", 13500, 1), "trip_time_invalid")
rej(make_frame("260903001736", "260902235839", 13500, 1), "end_before_start")
rej(make_frame("260902000000", "260903000000", 13500, 1), "duration_out_of_range")
rej(make_frame("260909120000", "260909121000", 5000, 1, received_at="2026-09-03T00:00:00+09:00"), "trip_start_in_future")
rej(make_frame("260902235839", "260903001736", 0, 132500), "fare_nonpositive")
old = make_frame("260620121112", "260620121112", 3003, 1); old["checksum_valid"] = False
rej(old, "checksum_invalid")

# 같은 trip_start, 값이 다른 Trip 2개 → 충돌, 둘 다 저장 안 함
c1 = make_frame("260902235839", "260903001736", 13500, 132500)
c2 = make_frame("260902235839", "260903001800", 13800, 133000)
rows_c, st_c = S.extract_trips(jl(c1, c2), "f", "f")
check("동일 trip_start 충돌 → 전부 미저장(Fail-Closed)", len(rows_c) == 0 and st_c["conflict_trip_start"] == 2, st_c)

# 손상 JSON 줄은 조용히 버리지 않고 카운트
rows_j, st_j = S.extract_trips("{broken\n" + jl(mid), "f", "f")
check("깨진 JSON 줄 카운트 + 나머지 정상처리", len(rows_j) == 1 and st_j["invalid_json"] == 1, st_j)

print("== 2. 매칭: 2026-09-02 카카오 9건 (raw_calls 실제 행 그대로) ==")
CALLS_0902 = [  # (id, 배차, 하차, 요금) — 운영 DB 실측(2026-09-28 조회)
    (1448, "00:02", "00:12", 8200), (1447, "18:38", "18:44", 4800), (1445, "18:55", "19:09", 6900),
    (1456, "19:12", "19:24", 6600), (1444, "19:26", "19:51", 10000), (1443, "20:46", "21:09", 12600),
    (1442, "21:15", "21:19", 4500), (1455, "21:48", "22:11", 16900), (1454, "23:58", "00:17", 13500)]
calls = [{"id": i, "날짜": "2026-09-02", "배차시각": b, "하차시각": e, "요금": f, "콜유형": "카카오T",
          "운행시간_분": None, "주행거리_km": None, "영업거리_km": None, "raw_row_type": None,
          "data_source": "drive_ocr_tesseract", "verify_status": "verified"} for i, b, e, f in CALLS_0902]

# S700 시각 = 콜카드 시각 + start(+8~79s) / end(+0~92s) 오차 (TAEO 실측 범위). 23:58건은 실측 그대로.
import itertools
offs = itertools.cycle([(8, 0), (79, 92), (35, 40), (60, 5), (22, 70), (45, 88), (12, 30), (70, 15), (39, 57)])
trips = []
for c in calls:
    b_dt = S._call_datetimes(c)[0]
    e_dt = S._call_datetimes(c)[1]
    so, eo = next(offs)
    if c["id"] == 1454:
        s_dt, e_dt2 = datetime(2026, 9, 2, 23, 58, 39, tzinfo=S.KST), datetime(2026, 9, 3, 0, 17, 36, tzinfo=S.KST)
    else:
        s_dt, e_dt2 = b_dt + timedelta(seconds=so), e_dt + timedelta(seconds=eo)
    trips.append({"trip_start": s_dt.isoformat(), "trip_end": e_dt2.isoformat(), "fare": c["요금"], "distance_km": 5.0})
claimed, results = set(), []
for t in trips:
    st_, mid_, meth, det = S.match_trip(t, calls, claimed)
    results.append((st_, mid_))
    if st_ == "MATCHED":
        claimed.add(mid_)
check("9/9 전부 MATCHED", all(s == "MATCHED" for s, _ in results), results)
check("9건이 서로 다른 raw_calls id에 1:1 대응", len({m for _, m in results}) == 9)
check("자정횡단 23:58 → id1454", results[-1] == ("MATCHED", 1454), results[-1])

t_amb = {"trip_start": "2026-09-02T18:38:20+09:00", "trip_end": "2026-09-02T18:44:10+09:00", "fare": 4800, "distance_km": 2.0}
dup_call = dict(calls[1]); dup_call["id"] = 9999   # 같은 시각·같은 요금 후보가 하나 더 있음
s_, m_, meth, det = S.match_trip(t_amb, calls + [dup_call])
check("복수후보(동시각·동요금) → AMBIGUOUS, 자동확정 안 함", s_ == "AMBIGUOUS" and m_ is None, (s_, det.get("ambiguous_call_ids")))
s_, m_, _, det = S.match_trip(t_amb, calls, claimed_call_ids={1447})
check("후보가 이미 다른 Trip에 점유 → AMBIGUOUS(candidate_already_claimed)", s_ == "AMBIGUOUS" and _ == "candidate_already_claimed", (s_, _))
t_far = dict(t_amb); t_far["trip_start"] = "2026-09-02T18:45:00+09:00"; t_far["trip_end"] = "2026-09-02T18:51:00+09:00"
s_, m_, _, det = S.match_trip(t_far, calls)
check("시간오차 초과(>180s) → UNMATCHED", s_ == "UNMATCHED", (s_, det["evidence"][:1]))
t_none = {"trip_start": "2026-09-02T12:00:00+09:00", "trip_end": "2026-09-02T12:10:00+09:00", "fare": 7777, "distance_km": 3.0}
s_, m_, _, _ = S.match_trip(t_none, calls)
check("콜카드 없는 S700-only → UNMATCHED(누락/배회 후보, raw_calls 생성 없음)", s_ == "UNMATCHED" and m_ is None)
summ = dict(calls[1]); summ.update({"id": 5000, "콜유형": "합계", "data_source": "app_ocr_summary"})
s_, m_, _, det = S.match_trip(t_amb, [summ])
check("요약/합계 행은 후보에서 제외", s_ == "UNMATCHED" and det["candidates_considered"] == 0)
same_fare = dict(calls[1]); same_fare.update({"id": 7001, "배차시각": "21:00", "하차시각": "21:06"})
s_, m_, _, _ = S.match_trip(t_amb, calls + [same_fare])
check("동일요금 반복(4,800원)도 시간창으로 구분 → 18:38건만 MATCHED", s_ == "MATCHED" and m_ == 1447, (s_, m_))

print("== 3. 매칭: 2026-09-06 우버 (TAEO 실측) ==")
uber_call = {"id": 1440, "날짜": "2026-09-06", "배차시각": "02:24", "하차시각": None, "요금": 16400, "콜유형": "우버",
             "운행시간_분": None, "주행거리_km": None, "영업거리_km": None, "raw_row_type": None,
             "data_source": "drive_ocr_tesseract", "verify_status": "unverified"}
uber_trip = {"trip_start": "2026-09-06T02:30:04+09:00", "trip_end": "2026-09-06T02:46:48+09:00", "fare": 16400, "distance_km": 12.23}
s_, m_, meth, det = S.match_trip(uber_trip, [uber_call])
check("우버: 구조필드 NULL(비고에만 값) → PROVISIONAL, MATCHED 금지", s_ == "PROVISIONAL" and m_ is None, (s_, meth))
uc2 = dict(uber_call); uc2.update({"운행시간_분": 17.1, "주행거리_km": 12.37})
s_, m_, meth, det = S.match_trip(uber_trip, [uc2])
check("우버: 구조필드 확보 시 요금+운행시간+거리로 MATCHED(시작 6분차에도)", s_ == "MATCHED" and m_ == 1440, (s_, meth, det.get("chosen")))
uc3 = dict(uber_call); uc3.update({"운행시간_분": 30.0, "주행거리_km": 12.37})
s_, _, _, _ = S.match_trip(uber_trip, [uc3])
check("우버: 운행시간 불일치 → UNMATCHED", s_ == "UNMATCHED", s_)
uc4 = dict(uber_call); uc4.update({"콜유형": "카카오T", "하차시각": "02:31"})
s_, _, _, _ = S.match_trip(uber_trip, [uc4])
check("전역 ±3분 규칙 미적용: 카카오T였다면 6분 차이는 불일치", s_ == "UNMATCHED", s_)

print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
raise SystemExit(1 if FAIL else 0)
