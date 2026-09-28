import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
"""task#150 — DB 오케스트레이션 테스트 (가짜 DB: 유니크 제약/컬럼권한/실패주입 모사)."""
import asyncio, copy, json, re
from datetime import datetime, timedelta, timezone
import s700_ingest as S

PASS, FAIL = 0, 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name} {extra}")

# ── 프레임 생성 (Kotlin Decoder v0.4 동일 오프셋) ──
def make_ascii(start, end, fare, dist_raw, primary="0A", secondary="08", aux="01"):
    buf = ["0"] * 96
    def put(a, b, s): buf[a:b] = list(s)
    put(0, 12, start); put(18, 20, primary); put(20, 22, secondary); put(22, 28, f"{fare:06d}")
    put(44, 56, start); put(56, 68, end); put(68, 76, f"{dist_raw:08d}"); put(82, 84, aux); put(84, 90, f"{fare:06d}")
    body = "".join(buf); return body + f"{(-sum(body.encode('ascii'))) & 0xFF:02X}"

def make_frame(start, end, fare, dist_raw, closed=True, received_at="2026-09-28T00:00:00+09:00"):
    a = make_ascii(start, end, fare, dist_raw); d = S._redecode_ascii(a)
    return {"type": "frame", "received_at": received_at, "meter_time_raw": None, "ascii": a, "hex": a.encode().hex(),
            "payload_length": 98, "frame_class": "LONG_BUSINESS_FRAME", "checksum_received": a[96:98],
            "checksum_calculated": a[96:98], "checksum_valid": True, "meter_fare": d["meter_fare"],
            "trip_start": d["trip_start"], "trip_end": d["trip_end"], "distance_raw": d["distance_raw"],
            "fare_copy": d["fare_copy"], "aux_flag": d["aux_flag"], "primary_state": d["primary_state"],
            "secondary_state": d["secondary_state"], "decode_warning": None, "trip_closed": closed,
            "decoder_version": "s700-decoder-v0.4"}

def jl(*recs): return "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n"
def raw12(dt): return dt.strftime("%y%m%d%H%M%S")

# ── 가짜 Supabase 어댑터 ──
class FakeSb:
    ALLOWED_PATCH = {"match_status", "matched_raw_call_id", "match_method", "match_detail"}  # anon 컬럼권한 모사
    def __init__(self, raw_calls=None):
        self.trips, self.raw, self.nid, self.log = [], (raw_calls or []), 1, []
        self.fail_insert_keys, self.fail_get = set(), False
    @staticmethod
    def _d(v): return datetime.fromisoformat(v[:-1] + "+00:00" if str(v).endswith("Z") else v)
    async def get(self, table, p):
        self.log.append(("get", table))
        if self.fail_get: raise RuntimeError("GET failed (simulated)")
        if table == "raw_calls":
            m = re.match(r"\(날짜\.gte\.(.+),날짜\.lte\.(.+)\)", p["and"])
            return [copy.deepcopy(r) for r in self.raw if m.group(1) <= r["날짜"] <= m.group(2)]
        assert table == "s700_trips", table
        rows = list(self.trips)
        if "and" in p:
            m = re.match(r"\(trip_start\.gte\.(.+),trip_start\.lte\.(.+)\)", p["and"])
            lo, hi = self._d(m.group(1)), self._d(m.group(2))
            rows = [r for r in rows if lo <= self._d(r["trip_start"]) <= hi]
        if p.get("matched_raw_call_id") == "not.is.null":
            rows = [r for r in rows if r.get("matched_raw_call_id") is not None]
        if "or" in p:
            sts = set(re.search(r"match_status\.in\.\(([^)]*)\)", p["or"]).group(1).split(","))
            rows = [r for r in rows if r["match_status"] in sts or (r["match_status"] == "MATCHED" and r.get("matched_raw_call_id") is None)]
        rows.sort(key=lambda r: self._d(r["trip_start"]))
        out = []
        for r in rows:  # PostgREST 처럼 timestamptz 는 UTC 로 돌려준다
            c = copy.deepcopy(r); c["trip_start"] = self._d(r["trip_start"]).astimezone(timezone.utc).isoformat()
            c["trip_end"] = self._d(r["trip_end"]).astimezone(timezone.utc).isoformat(); out.append(c)
        return out
    async def insert(self, table, row):
        self.log.append(("insert", table)); assert table == "s700_trips"
        if row["trip_key"] in self.fail_insert_keys: raise RuntimeError("simulated insert failure")
        for r in self.trips:
            if r["trip_key"] == row["trip_key"] or self._d(r["trip_start"]) == self._d(row["trip_start"]):
                raise RuntimeError("409 duplicate key")
        r = copy.deepcopy(row); r["id"] = self.nid; self.nid += 1; r.setdefault("matched_raw_call_id", None)
        self.trips.append(r); return [copy.deepcopy(r)]
    async def patch(self, path, data):
        self.log.append(("patch", path))
        m = re.fullmatch(r"s700_trips\?id=eq\.(\d+)", path)
        assert m, f"허용되지 않은 patch 대상(raw_calls 수정 금지): {path}"
        assert set(data) <= self.ALLOWED_PATCH, f"anon 컬럼권한 위반: {set(data) - self.ALLOWED_PATCH}"
        t = next(r for r in self.trips if r["id"] == int(m.group(1)))
        rid = data.get("matched_raw_call_id")
        if rid is not None and any(o["id"] != t["id"] and o.get("matched_raw_call_id") == rid for o in self.trips):
            raise RuntimeError("unique violation matched_raw_call_id")
        t.update(data)

def run(c): return asyncio.run(c)

# ── fixture: 2026-09-02 카카오 9건 (운영 DB 실측 행 그대로) ──
CALLS = [(1448, "00:02", "00:12", 8200), (1447, "18:38", "18:44", 4800), (1445, "18:55", "19:09", 6900),
         (1456, "19:12", "19:24", 6600), (1444, "19:26", "19:51", 10000), (1443, "20:46", "21:09", 12600),
         (1442, "21:15", "21:19", 4500), (1455, "21:48", "22:11", 16900), (1454, "23:58", "00:17", 13500)]
def raw_rows():
    return [{"id": i, "날짜": "2026-09-02", "배차시각": b, "하차시각": e, "요금": f, "콜유형": "카카오T", "운행시간_분": None,
             "주행거리_km": None, "영업거리_km": None, "raw_row_type": None, "data_source": "drive_ocr_tesseract",
             "verify_status": "verified"} for i, b, e, f in CALLS]
OFFS = [(8, 0), (79, 92), (35, 40), (60, 5), (22, 70), (45, 88), (12, 30), (70, 15), (39, 57)]
def s700_frames():
    out = []
    for c, (so, eo) in zip(raw_rows(), OFFS):
        b, e = S._call_datetimes(c)
        if c["id"] == 1454:
            s, en = datetime(2026, 9, 2, 23, 58, 39, tzinfo=S.KST), datetime(2026, 9, 3, 0, 17, 36, tzinfo=S.KST)
        else:
            s, en = b + timedelta(seconds=so), e + timedelta(seconds=eo)
        out.append(make_frame(raw12(s), raw12(en), c["요금"], 50000 + c["id"], received_at=(en + timedelta(seconds=3)).isoformat()))
    return out
FR = s700_frames()
NOISE = [{"type": "meta", "k": "x"}, make_frame("260902183800", "260902184400", 4800, 1, closed=False)]
FILE = jl(*NOISE, *FR)

print("== A. 최초 적재 + 9/9 매칭 ==")
sb = FakeSb(raw_rows()); raw_before = copy.deepcopy(sb.raw)
r = run(S.ingest_jsonl(sb, FILE, "fid-0902", "s700_20260902.jsonl"))
check("9건 INSERT", r["inserted"] == 9 and len(sb.trips) == 9, r)
check("9건 전부 MATCHED", r["match"] == {"MATCHED": 9}, r["match"])
check("서로 다른 raw_calls id 1:1", len({t["matched_raw_call_id"] for t in sb.trips}) == 9)
check("자정횡단 23:58 → id1454", next(t for t in sb.trips if t["fare"] == 13500)["matched_raw_call_id"] == 1454)
check("raw_calls 미변경(읽기 전용)", sb.raw == raw_before)
check("raw_calls 대상 insert/patch 없음", all(not (op == "insert" and t == "raw_calls") for op, t in [(l[0], l[1]) for l in sb.log if l[0] == "insert"]))
check("진행중 프레임/meta 는 저장 안 됨", r["stats"]["accepted_frames"] == 9)
check("provenance 보존(source_file_*/raw_record)", all(t["source_file_id"] == "fid-0902" and t["raw_record"]["type"] == "frame" for t in sb.trips))

print("== B. 같은 파일 재처리(멱등) ==")
n_before, patches_before = len(sb.trips), sum(1 for l in sb.log if l[0] == "patch")
r2 = run(S.ingest_jsonl(sb, FILE, "fid-0902-copy", "s700_20260902 (복사본).jsonl"))
check("row 증가 0 (중복 파일명/ID 무관)", len(sb.trips) == n_before and r2["inserted"] == 0, r2)
check("duplicates_existing = 9", r2["duplicates_existing"] == 9)
check("재처리 시 patch 0", sum(1 for l in sb.log if l[0] == "patch") == patches_before)
check("최초 provenance 유지(덮어쓰기 없음)", all(t["source_file_id"] == "fid-0902" for t in sb.trips))

print("== C. dry_run ==")
sbc = FakeSb(raw_rows())
rc = run(S.ingest_jsonl(sbc, FILE, "f", "f", dry_run=True))
check("dry_run: 쓰기 0", len(sbc.trips) == 0 and not any(l[0] in ("insert", "patch") for l in sbc.log), sbc.log[:5])
check("dry_run: 예상 9건/9 MATCHED 미리보기", rc["inserted"] == 9 and rc["match"] == {"MATCHED": 9} and len(rc["match_preview"]) == 9, rc)

print("== D. 기존 DB와 trip_start 충돌 ==")
sbd = FakeSb(raw_rows())
first = FR[0]
alt = make_frame(first["trip_start"], first["trip_end"], first["meter_fare"] + 300, first["distance_raw"])
run(S.ingest_jsonl(sbd, jl(alt), "old", "old"))
rd = run(S.ingest_jsonl(sbd, jl(first), "new", "new"))
check("충돌 보고 + 저장 안 함", len(rd["conflicts_existing"]) == 1 and rd["inserted"] == 0 and len(sbd.trips) == 1, rd)

print("== E. 부분 실패 격리 ==")
sbe = FakeSb(raw_rows())
bad_key = S.extract_trips(jl(FR[2]), "x", "x")[0][0]["trip_key"]; sbe.fail_insert_keys.add(bad_key)
re_ = run(S.ingest_jsonl(sbe, FILE, "f", "f"))
check("1건 실패 보고 + 나머지 8건 정상", re_["inserted"] == 8 and len(re_["insert_failed"]) == 1 and re_["match"] == {"MATCHED": 8}, re_)

print("== F. DB 조회 실패는 조용한 성공이 되면 안 됨 ==")
sbf = FakeSb(raw_rows()); sbf.fail_get = True
try: run(S.ingest_jsonl(sbf, FILE, "f", "f")); ok = False
except RuntimeError: ok = True
check("GET 실패 → 예외 전파(INSERT 시도 없음)", ok and not any(l[0] == "insert" for l in sbf.log))

print("== G. 콜카드가 나중에 들어오는 경우: rematch ==")
sbg = FakeSb([])   # raw_calls 아직 없음
rg = run(S.ingest_jsonl(sbg, FILE, "f", "f"))
check("콜카드 없음 → 9건 UNMATCHED(누락/배회 후보, raw_calls 생성 없음)", rg["match"] == {"UNMATCHED": 9} and sbg.raw == [], rg["match"])
sbg.raw = raw_rows()
rr = run(S.rematch(sbg))
check("rematch: 9건 MATCHED 로 전이", rr["evaluated"] == 9 and rr["match"] == {"MATCHED": 9} and rr["changed"] == 9, rr)
rr2 = run(S.rematch(sbg))
check("MATCHED 는 재평가 안 함(flapping 방지)", rr2["evaluated"] == 0, rr2)

print("== H. raw_calls 삭제(FK SET NULL) 후 자가치유 ==")
t0 = sbg.trips[0]; t0["matched_raw_call_id"] = None          # ON DELETE SET NULL 모사
sbg.raw = [c for c in sbg.raw if c["id"] != 1448]            # 해당 raw_call 삭제됨
rh = run(S.rematch(sbg))
check("끊긴 MATCHED 재평가 → UNMATCHED 로 정정", rh["evaluated"] == 1 and t0["match_status"] == "UNMATCHED", (rh, t0["match_status"]))

print("== I. 파일 간 1:1 점유 (같은 콜카드를 두 Trip 이 주장) ==")
call = [{"id": 900, "날짜": "2026-09-02", "배차시각": "18:38", "하차시각": "18:44", "요금": 4800, "콜유형": "카카오T",
         "운행시간_분": None, "주행거리_km": None, "영업거리_km": None, "raw_row_type": None,
         "data_source": "drive_ocr_tesseract", "verify_status": "verified"}]
sbi = FakeSb(call)
fa = make_frame("260902183810", "260902184405", 4800, 100, received_at="2026-09-02T18:45:00+09:00")
fb = make_frame("260902183900", "260902184430", 4800, 200, received_at="2026-09-02T18:46:00+09:00")
run(S.ingest_jsonl(sbi, jl(fa), "a", "a")); rb = run(S.ingest_jsonl(sbi, jl(fb), "b", "b"))
sts = sorted(t["match_status"] for t in sbi.trips)
check("먼저 온 Trip MATCHED, 나중 Trip AMBIGUOUS(이미 점유)", sts == ["AMBIGUOUS", "MATCHED"], sts)
check("DB 유니크 위반 없이 처리", not rb["insert_failed"])

print("== J. 9/6 우버: PROVISIONAL → 구조필드 확보 후 MATCHED ==")
ub = [{"id": 1440, "날짜": "2026-09-06", "배차시각": "02:24", "하차시각": None, "요금": 16400, "콜유형": "우버",
       "운행시간_분": None, "주행거리_km": None, "영업거리_km": None, "raw_row_type": None,
       "data_source": "drive_ocr_tesseract", "verify_status": "unverified"}]
sbj = FakeSb(ub)
fu = make_frame("260906023004", "260906024648", 16400, 122300, received_at="2026-09-06T02:47:00+09:00")
rj = run(S.ingest_jsonl(sbj, jl(fu), "u", "s700_20260906.jsonl"))
check("구조필드 NULL → PROVISIONAL(자동확정 금지, FK 연결 없음)", rj["match"] == {"PROVISIONAL": 1} and sbj.trips[0]["matched_raw_call_id"] is None, rj)
sbj.raw[0]["운행시간_분"], sbj.raw[0]["주행거리_km"] = 17.1, 12.37
rj2 = run(S.rematch(sbj))
check("구조필드 보강 후 rematch → MATCHED(시작 6분차에도)", sbj.trips[0]["match_status"] == "MATCHED" and sbj.trips[0]["matched_raw_call_id"] == 1440, (rj2, sbj.trips[0]["match_status"]))

print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
raise SystemExit(1 if FAIL else 0)
