# Task #164 / Task #27 Canonical Identity & Precedence v1

Status: DRAFT / CASSANDRA REVIEW REQUIRED  
Production PRIMARY flag: OFF  
Raw-row deletion/overwrite: prohibited

## Goal

Use one deterministic identity/precedence rule for both:

1. Task #164 daily_history ingestion duplicate protection
2. Task #27 daily operation reporting

The raw `raw_calls` rows remain provenance evidence. Canonicalization is selection-only.

## Identity v1

Rows are compared only when calendar date, normalized platform, and fare are equal.

### STRONG_FULL_INTERVAL
Both rows have start and end times, and both start and end match exactly.

### STRONG_PARTIAL_ADDRESS
At least one row has incomplete timing; the same-position available time matches
(start=start or end=end), and normalized origin + destination both match.

### WEAK_TIME_ONLY
Only a cross-boundary/single-time overlap is available without matching addresses.
WEAK candidates are **not auto-collapsed**. Reporting must flag them; ingestion must
quarantine them for review rather than insert another possible duplicate.

This change intentionally replaces the previous broad "any start/end overlap" rule,
which could falsely merge two consecutive same-fare trips sharing a boundary time.

## Precedence v1

Higher evidence rank wins only inside a STRONG duplicate group:

1. verified manual correction
2. completed `drive_ocr_layout_v1`
3. completed `drive_ocr_tesseract`
4. `app_ocr_individual`
5. verified `argos_reconstructed`
6. other verified
7. other source-id backed
8. confirmed
9. other

This does not delete the losing raw row.

## Production regression evidence

### 2026-07-13
Raw after new upload: 28 rows / 250,500 won.
Canonical simulation:
- total: **16 rows / 144,500 won**
- Kakao: 14 / 129,900
- roaming: 2 / 14,600

The 12 new Drive rows are STRONG duplicates of 12 reconstructed rows and the Drive
rows win by provenance rank. Two earlier Kakao reconstructed rows and two roaming
rows remain unique.

### 2026-07-15
Raw after new upload: 27 rows / 240,200 won.
Canonical simulation:
- total: **17 rows / 145,400 won**
- Kakao: 14 / 121,600
- Uber: 1 / 14,700
- roaming: 2 / 9,100

Ten new Drive rows replace ten reconstructed duplicates. The Drive row
00:13-00:19 / 7,800 won has no strong duplicate and remains canonical. Under MAGI
event #3966 calendar-day semantics it belongs to 2026-07-15.

Historical daily_summary is not used as canonical truth here; it is stale relative to
the newly ingested source evidence.

## Full-DB candidate inventory at design time

Strong duplicate pairs observed:
- reconstructed ↔ Drive full interval: 22
- Drive ↔ Drive full interval: 7
- app individual ↔ Drive partial-start+address: 25
- app individual ↔ app individual partial-start+address: 3

Weak time-only candidates observed:
- app individual ↔ Drive: 4
- app individual ↔ app individual: 1

Weak candidates remain unresolved and must be surfaced, not silently collapsed.

## Deliverables in PR #33

- `canonical_identity_v1.py` shared Python policy
- `daily_history_primary_adapter.py` uses the same identity policy
- `migrations/task164_canonical_raw_calls_v1.sql` read-only canonical view definition
- regression tests
- no DB migration applied
- no PRIMARY activation


## Access / RLS contract

Render currently uses the Supabase anon role. The call_image_ingestions ledger is
SELECT-visible to anon only when X-MAGI-RPC-Secret is valid. Because evidence
precedence depends on COMPLETED Drive ingestion, canonical selection must never
silently run without that ledger visibility.

The v1 migration therefore has two fail-closed layers:

1. canonical_raw_calls_v1 includes fn_internal_rpc_secret_ok() in its eligible-row gate,
   so a direct no-secret view query returns zero rows rather than a lower-rank result.
2. get_canonical_raw_calls_v1() explicitly raises 42501 without the secret.

Application consumers must use sb_select_canonical(), which always supplies the
internal RPC secret. Generic sb_select() is not an approved canonical read path.

No-secret behavior is intentionally fail/empty; it must never change the canonical
winner provenance.
