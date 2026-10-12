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
