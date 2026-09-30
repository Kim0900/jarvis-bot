# Task #164 production primary cutover

Status: PRE-MERGE / FEATURE FLAG OFF

## Scope

The layout-aware daily_history parser remains disabled as the production primary path unless:

1. CASSANDRA pre-merge validation passes.
2. At least **2 natural full daily_history samples from different service dates** pass the layout date/count/sum/card gates while production shadow is enabled.
3. No unresolved same-date pre-existing trip rows exist for the target ingestion date.
4. The production feature flag is explicitly enabled.

Flag:

`DAILY_HISTORY_LAYOUT_PRIMARY_ENABLED=false` by default.

## Cutover policy

- Source claim, duplicate-source handling and FAILED retry cleanup remain owned by the existing ingestion ledger.
- A layout result is eligible for save only when status is `COMPLETE_LAYOUT_VALIDATED`.
- Layout date/count/sum/address/fare gate failure is Fail-Closed. It does **not** fall back to legacy.
- Legacy compatibility fallback is allowed only when the layout service is unavailable, times out, or returns 5xx.
- Legacy row-level disagreement is not a quarantine reason by itself.
- Independent date/header count/header sum disagreement is a quarantine reason.
- Same-date rows from unrelated platforms do not block Kakao ingestion. Quarantine is limited to Kakao duplicate candidates with exact fare plus an overlapping start/end clock value.
- The validated batch uses one bulk insert. DB write failure or ingestion-ledger COMPLETED failure rolls back rows for that source_id.
- The pre-cutover 504c4699 path remains the rollback target while the feature flag is OFF.

## 2026-08-06 sample correction

The 2026-08-06 screenshot proved layout parser quality (10 cards / 107,600 won), but it is **not** a clean save-path validation sample.

Existing DB state for that date before layout-primary cutover:

- 9 `trip` rows: 85,900 won
- 2 `unclassified` rows: 19,600 won
- total non-summary rows: 11 / 105,500 won
- separate `daily_total` summary row also exists

The 9 trip timestamps correspond largely to card end-times rather than the screenshot start-times, indicating historical ingestion semantics differ from the new layout contract. The corrected duplicate policy does not treat the whole calendar date as one population. It ignores unrelated Uber/roaming/unclassified rows and only quarantines same-platform Kakao overlap candidates.

## Regression requirements

Pre-merge tests cover:

- transport/5xx-only compatibility fallback
- parser/gate failure Fail-Closed
- independent header disagreement
- source duplicate block
- mixed-platform same-date coexistence and Kakao identity-overlap quarantine
- DB failure rollback
- COMPLETED-mark failure rollback
- positive-only direct-payment semantics

No production primary activation is part of this PR.


## Legacy classifier dependency

The layout primary path is no longer gated exclusively by `detect_and_parse_call_document()`. A conservative independent hint also invokes layout when OCR text contains either the explicit daily-history heading or at least two time-range anchors together with a count and won-amount header. Other document formats remain on the existing path.
