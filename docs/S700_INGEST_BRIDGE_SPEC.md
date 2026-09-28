# S700 JSONL → s700_trips 적재 (task#150) — 호출 규약 / 검증 절차

> 작성: CASPER 2026-09-28. 대상: YOUNGSIL bridge(Drive→Render push), CASSANDRA(검증), TAEO.
> **raw_calls 에는 INSERT/UPDATE 하지 않는다.** 결과는 `s700_trips` staging 에만 기록된다.

## 1. 적재 endpoint
`POST https://<jarvis-bot>/mcp/ingest_s700_jsonl`  (헤더 `X-MCP-Key`, `Content-Type: application/json`)

```json
{
  "jsonl_text": "<s700_YYYYMMDD.jsonl 파일 본문 전체>",
  "source_file_id": "<Drive fileId>",
  "source_file_name": "s700_20260902.jsonl",
  "dry_run": true
}
```
- `jsonl_text` 필수(문자열, 최대 8MB). `source_file_id` 또는 `source_file_name` 중 하나 필수(provenance).
- **처음엔 반드시 `dry_run:true`** 로 예상 결과(건수/매칭 미리보기)를 확인한 뒤 `false` 로 재호출.
- 같은 파일/같은 Trip 재호출은 안전하다(row 증가 0, 최초 provenance 유지). 파일이 여러 폴더에 중복돼 있어도 동일.

### 응답 핵심 필드
| 필드 | 의미 |
|---|---|
| `stats.frames / accepted_frames` | frame 수 / 수용조건 통과 종료프레임 수 |
| `stats.rejected{사유:건수}` | Fail-Closed 거절 사유별 집계(조용한 유실 없음) |
| `stats.duplicate_in_input / conflict_trip_start` | 입력 내 중복 / 같은 trip_start 인데 값이 다른 충돌(둘 다 미저장) |
| `inserted` | 신규 저장 건수 |
| `duplicates_existing` | 이미 있는 Trip(trip_key 동일) — 저장 안 함 |
| `conflicts_existing[]` | DB에 같은 trip_start 가 다른 값으로 이미 있음 — **사람 확인 필요**, 저장 안 함 |
| `insert_failed[]` | 개별 INSERT 실패(다른 행은 계속 처리) |
| `match{상태:건수}` / `match_preview[]`(dry_run) | 매칭 결과 요약 |

## 2. 재매칭 endpoint
`POST /mcp/s700_match` `{ "dry_run": false, "limit": 500 }`
콜카드(raw_calls)가 S700 보다 늦게 들어온 경우 PENDING/UNMATCHED/PROVISIONAL/AMBIGUOUS 를 재평가한다.
`MATCHED` 는 재평가하지 않는다(flapping 방지). 단 raw_calls 삭제로 연결이 끊긴 MATCHED 는 자가치유로 재평가.

## 3. 수용조건(Fail-Closed) — 하나라도 어긋나면 저장하지 않고 사유만 집계
type=frame · trip_closed=true · checksum_valid=true · decode_warning=null · meter_fare==fare_copy(정수, >0) ·
trip_start/end 12자리(yyMMddHHmmss, KST) 유효 · end≥start · 12시간 이하 · distance_raw 정수≥0 ·
**ascii(98자) 재해독 결과가 JSON 필드와 전부 일치 + checksum 재계산 PASS** · decoder_version=`s700-decoder-*` · trip_start 미래(>10분) 아님.

## 4. 식별/멱등
`trip_key = SHA-256("s700|trip_start|trip_end|fare|distance_raw")`(UNIQUE) + `trip_start` UNIQUE.
같은 trip_start 에 값이 다른 Trip 이 오면(이중 Trip 위험) 어느 쪽도 자동 채택하지 않고 `conflicts_existing` 으로 보고.

## 5. 매칭 상태 (raw_calls 는 읽기 전용)
| 상태 | 의미 |
|---|---|
| MATCHED | 다중증거 충족 + 다른 Trip 이 점유하지 않은 raw_calls 1건과 1:1 (`matched_raw_call_id` FK) |
| PROVISIONAL | 요금 일치하나 구조필드(운행시간_분/주행거리_km) 부재 → **자동확정 금지**, FK 연결 없이 detail 에만 후보 기록 |
| AMBIGUOUS | 복수후보 / 이미 점유된 콜카드 |
| UNMATCHED | 후보 없음 → 누락/배회 후보로 staging 에만 유지(raw_calls 자동생성 없음) |
- 카카오T: 요금 exact + 시작≤180s + 종료≤180s(존재 시) — **카카오 한정 1차 규칙**, 전역 규칙 아님.
- 우버 등: 요금 exact + 운행시간(≤1분) + 거리(≤3%) 구조필드 필수. 비고 텍스트는 canonical 로 보지 않음.

## 6. 검증 요청 시나리오 (TAEO J)
1. 9/1 S700 실파일 → 확정 Trip 7건(GPX 시간매칭은 후속 enrichment 계층)
2. 9/2 콜카드 9건 ↔ S700: 9/9 MATCHED, 자정횡단(23:58:39→00:17:36, 13,500원) = id1454
3. 9/6 우버: 콜카드 구조필드 NULL → PROVISIONAL (구조필드 보강 후 `/mcp/s700_match` → MATCHED)
4. 같은 JSONL 2회 replay → `inserted:0`
5. 손상/미완료 frame → `stats.rejected` 사유 집계
6. 복수후보 → AMBIGUOUS
자동 테스트: `python tests/test_s700.py` (38), `python tests/test_s700_db.py` (25).
