# TASK#195 파서 계층 설계 v2 (캐스퍼, 2026-10-12 KST)

상태: 설계 단계. 카산드라 CONDITIONAL PASS(event 4908) 조건 A~F 반영. 구현·스키마 변경 없음.

## 0. 실측 정정 (v1 전제 수정)
v1은 "약 8,700줄 단일 파일에 파서가 결합"이라 했으나 실측 결과 부분적으로 틀렸다.
- 이미 분리됨(모듈+단위테스트): `daily_history_parser.py`, `daily_history_layout_parser.py`, `uber_trip_parser.py`, `kakao_monthly_history_parser.py`, `s700_ingest.py`. `bot_v5_legacy.parse_daily_history/parse_uber_trip_detail`은 얇은 래퍼.
- 아직 `bot_v5_legacy.py` 내부(L3868~4045): `parse_kakao_trip_detail`, `parse_receipt_summary`, `parse_meter_receipt`, 분류기 `detect_and_parse_call_document`. 이 4개가 실제 이관 대상.
- 따라서 "대규모 분리"가 아니라 "잔여 4개 이관 + 통합 회귀 게이트 + 버전 추적"이 정확한 범위다.
- 테스트 기준선(2026-10-12): unittest 208건 통과. test_s700/test_s700_db는 스크립트형(PASS=43 등)이라 `unittest discover`에서 로더 오류로 보임 → 통합 러너 필요.
- CI는 태스크별 8개 워크플로(paths 필터)로 쪼개져 있어 파서 전체를 한 번에 회귀시키는 게이트가 없다.

## 1. 구조 결정
- LLM 전담 에이전트 아님. 결정론적 `parsers/` 패키지(같은 저장소). 별도 서비스 없음.
- 기존 모듈은 파일 이동 없이 `parsers/__init__.py`가 단일 진입점(`parse_document(text) -> ParseResult`)으로 재노출. 잔여 4개만 신규 모듈로 이관하고 legacy에는 동일 시그니처 래퍼를 남긴다(호출부·엔드포인트 불변).
- 파서 출력 계약: 순수 함수, DB/네트워크 접근 금지, `format`, `parse_errors`, `parser_version`(코드 상수) 포함.

## 2. 조건별 대응
A. 엔드포인트·재시도·중복·스케줄러·순라 불변: 이관은 "함수 본문 무수정 이동 + 래퍼 유지"로 한정. 이전/이후 동일 입력 비교 하네스(Characterization test): 코퍼스 전 케이스를 이관 전 커밋과 이후 커밋에서 각각 실행해 출력 JSON 완전 일치(diff 0)를 PR 게이트로. `/mcp/process_call_image` 계약은 응답 필드 스냅샷 테스트로 고정.
B. 개별 행 검증: 요금·시각·날짜·건수·합계 일치 검증은 기존 Fail-Closed 유지, 신규 파서 경로도 동일 검증 함수 통과 필수. 실패 시 원본 이미지 보존(Drive 원본은 불변)·재처리 가능. LLM 보조는 도입하지 않음(v1의 저신뢰 시 LLM 보조안 철회). 필요해지면 별도 설계·재검증.
C. 코퍼스 보안: 저장소(Git)에 원본 이미지 금지. 코퍼스는 (1) 정답 JSON+OCR 텍스트(비식별: 주소 동 단위 이하 마스킹, 승객·기사 식별자 제거)만 저장소에, (2) 원본 이미지는 승인된 Drive 폴더(대표님 지정, 외부 공유 금지)에 두고 해시로 참조. 음성 사례 필수 포함: OCR 실패, 중복 재업로드, 자정 교차 날짜(#177), ₩→4 오독(#184), compact KOR(#190), LAYOUT_CARD 미해결(#188). **저장 위치·비식별 기준은 대표님 결정 필요.**
D. parser_version 스키마: raw_calls·call_image_ingestions 모두 컬럼 없음(카산드라 확인). 1차는 스키마 변경 없이 `call_image_ingestions`의 기존 메타 필드/로그에 기록 가능한지부터 조사. 컬럼 추가 시: nullable text, 기존 행 NULL 유지(소급 추정 금지), 인덱스 없음, 롤백=컬럼 drop, 재처리는 canonical_identity 게이트로 멱등. 별도 설계 문서와 카산드라 재검증 후에만 마이그레이션.
E. 검증 층 분리: (1) 파서 단위 정답 일치 (2) 이전/이후 diff 0 (3) 실제 적재 E2E(Drive 복사본 투입→raw 불변/격리 확인, #174 방식) (4) 장애 복구(Supabase 타임아웃 시 재시도·무중복, #194 연계). PASS 보고 시 층별로 구분 표기.
F. 소유권·배포 승인: 마기 결정 사항. 캐스퍼 제안은 파서 PR 작성=TAEO 또는 캐스퍼(마기 배정), 판정=카산드라, 배포=대표님 승인.

## 3. 단계 (각 단계 종료 시 카산드라 검증)
0. (착수 가능, 비파괴) 코퍼스 후보 목록 작성: #184 #188 #190 #177 #170 케이스의 정답 JSON 초안. 저장 위치 대표님 결정 전에는 로컬 초안만.
1. 통합 회귀 러너: 기존 unittest+스크립트형 테스트를 한 명령으로 실행, 단일 CI 워크플로. 코드 동작 변경 없음.
2. Characterization 하네스: 잔여 4개 파서 현재 출력을 코퍼스로 고정.
3. 잔여 4개 이관(본문 무수정)+래퍼. diff 0 확인.
4. parser_version 기록(스키마 설계 별도).

## 4. 미확인
- 코퍼스 원본 접근 가능 범위(대표님 Drive 내 위치).
- 현 CI 필수 체크(브랜치 보호) 설정 여부(GitHub 설정 접근 불가).
- 이 세션은 저장소 push 권한이 없음(읽기 전용) → PR 작성은 권한 연결 후.

---

# v2.1 보완 (카산드라 event 4911 보완 7건 대응, 2026-10-12 KST)

## 실측 추가
- 잔여 4개 파서(`parse_kakao_trip_detail`, `parse_receipt_summary`, `parse_meter_receipt`, `detect_and_parse_call_document`)를 import하는 테스트/스크립트가 **0건**(grep 확인). 즉 이관 전 characterization 테스트가 필수이며, 현재 이 4개의 동작은 코드 외에 고정된 근거가 없다.
- 저장소에 이미지 파일 0건(jpg/png 검색). 픽스처는 테스트 안 인라인 텍스트뿐이라 Git 이미지 금지 원칙은 현재 준수 중.
- `call_image_ingestions` 컬럼: source_type, source_id, status, format, inserted_count, last_error, started_at, updated_at, completed_at (JSON/메타 컬럼 없음).

## 보완 1 — 계약 충돌 방지 (분류 우선순위 동결)
`parse_document(text)`는 **신규 추가 함수**이며 `detect_and_parse_call_document`에 위임만 한다(재구현 금지). 반환 dict는 재가공 없이 그대로. 동결할 분류 우선순위:
1. "매출" and "집계" → meter_receipt
2. kakao_monthly(looks_like) → kakao_monthly
3. "일별" and "운행" and "이력" → daily_history
4. uber(looks_like) → uber_trip_detail
5. "배차" and ("기사" or "운행 정보") → kakao_trip_detail
6. 그 외 → `{"format":"unknown", ...}`
기존 이름(parse_daily_history 등)은 래퍼로 유지. 운영 경로가 `parse_document`를 쓰도록 바꾸는 것은 별도 승인 단계(3단계 이후)로 분리.

## 보완 2 — characterization vs golden 분리
- **characterization 세트**: 현재 출력을 그대로 고정(이관 안전망). 알려진 오파싱은 `known_bug` 태그로 명시해 현 오파싱을 "정답"으로 오인하지 않게 하고, 수정은 별도 PR에서만 태그 해제.
- **golden 세트**: 사람이 확정한 정답 JSON. 파서 정확도 판정은 이 세트로.
- diff=0 통과는 "이관이 동작을 바꾸지 않음"만 의미하며 "정확함"을 의미하지 않는다고 보고서에 분리 표기.

## 보완 3 — OCR 원문도 개인정보
OCR 텍스트에는 주소·시각·금액·결제수단이 포함될 수 있어 정답 JSON과 동일 기준으로 비식별(주소 동 단위 이하 마스킹, 식별자 제거) + 접근통제 적용. 비식별 전 원문은 Git·PR·이벤트 payload에 넣지 않는다.

## 보완 4 — 해시와 보관정책
sha256(원본 바이트)는 "동일 파일 식별/무결성 확인용 포인터"일 뿐 진위·보관권한을 증명하지 않는다. 따라서 (a) 승인된 Drive 폴더, (b) 접근 가능자, (c) 보관기간, (d) 해시 알고리즘(sha256 고정), (e) 해시 대장 위치를 **대표님이 확정**한 뒤 코퍼스 구축. 확정 전에는 로컬 초안만.

## 보완 5 — parser_version 저장 (무스키마안 재판정)
점검한 무스키마 후보와 결론:
- `raw_calls.data_source`: canonical identity 게이트가 출처 구분에 쓰므로 변경 금지.
- `raw_calls.비고/메모`: 코드가 `비고`의 "OCR 추출:" 접두어로 요약행을 판정하므로 오염 위험.
- `call_image_ingestions.last_error`: 의미 불일치.
→ **깨끗한 무스키마 슬롯이 없다.** 권고: `call_image_ingestions`에 nullable text `parser_version` 1컬럼 추가(소스 단위, raw_calls와는 source_id로 조인 가능). 기존 행 NULL 유지, 소급 추정 금지, 롤백=컬럼 drop, 읽기 경로=ingestion 조회. **스키마 변경은 별도 설계+카산드라 재검증+대표 승인 후**이며 이 문서는 승인 요청이 아니다.

## 보완 6 — 테스트 수치 재현 방법
기준 커밋 9440796(= main 26d237c + 문서 1개)에서 직접 실행한 값:
- `python -m unittest discover -s tests -p "test_*.py"` → Ran 210, errors=2 (오류 2건은 test_s700/test_s700_db가 스크립트형이라 로더가 실패 처리한 것, 실제 단언 실패 아님) → 정상 208건.
- `python -m unittest tests.test_s700` → RESULT: PASS=43 FAIL=0
- `python -m unittest tests.test_s700_db` → RESULT: PASS=25 FAIL=0
(앞서 "S700 43건"은 test_s700만의 수치였고 test_s700_db는 25건이 추가.) 카산드라 독립 재실행을 요청한다.

## 보완 7 — 상태와 권한 구분
Registry `IN_PROGRESS`는 "설계 보완 작업 중"이지 구현 권한이 아니다. 구현·스키마·배포는 (대표 승인 + 마기 배정 + 카산드라 해당 단계 PASS) 전부 충족 전까지 금지. 현재 허용 범위: 문서 작성, 읽기 전용 조사, 로컬 비식별 초안.
