# TASK#198 실서버 읽기 전용 스모크 — 대표님 실행 절차 (PR#75, CASSANDRA #5916 정정판)

상태: **준비만 됨. 캐스퍼는 실행하지 않았다**(작업 환경에서 supabase.co 접속 불가). 이전 승인(#5911, 2026-10-22 19:45~21:00 KST, 커밋 7705163)은 **종료되었고 이 정정판 커밋에는 적용되지 않는다.** 아래 세 값은 마기가 **새 승인 이벤트**에 적어 준 것만 쓴다: **① 승인 커밋 40자리 ② 창 시작 ③ 창 종료**(오프셋 포함 예 `2026-10-24T19:45:00+09:00`, 길이 6시간 이하). 승인 전에는 1번도 실행하지 않는다.

## 범위 (이것만 한다)
- GET 요청 약 10건. 쓰기·PATCH·POST·DELETE·CAS·배포 없음 — 스크립트의 클라이언트는 GET만 있고 다른 메서드는 요청 전에 예외가 난다.
- 첫 5xx·타임아웃·전송 오류에서 즉시 중단(재시도 없음, 종료코드 3).
- 참고: `SUPABASE_KEY`는 봇이 쓰는 키라 키 자체는 쓰기 권한이 있을 수 있다. 읽기 전용은 **스크립트가 GET만 보내는 것**으로 보장되므로 3·4·6단계(승인 커밋·해시·사전점검)를 건너뛰면 안 된다.

## 스크립트가 스스로 강제하는 것 (SEC-01~04, 가짜 서버·임시 git 저장소 시험으로 확인)
- **창 시작·종료 이중 게이트**: 시작 전이면 요청 0건, 종료 0.5초 전부터 요청 0건. 요청마다 제한 시간 = min(15초, 창 종료까지 남은 시간)이고, 창 종료 시각에는 진행 중인 요청도 **강제 중단**(프로세스가 기다리지 않고 끝남)해서 종료 시각 이후에 진행되는 GET이 없다.
- **승인 커밋 고정**: `--approved-commit`(승인 이벤트에서 복사한 40자리 소문자 16진)가 `git rev-parse HEAD`와 같고, 추적 파일이 깨끗하며, 스크립트 파일이 `git show HEAD:…`와 바이트 단위로 같아야 한다. 아니면 **키를 읽기 전에, 요청 전에** 종료코드 2.
- **비밀 비출력**: 출력·보고서는 모두 키/비밀값을 [REDACTED]로 바꾸는 필터를 거치고, 서버의 Content-Range는 `a-b/n` 또는 `*/n` 형식일 때만 저장(아니면 `INVALID_FORMAT`이며 점검 실패), 완성된 보고서에 비밀값이 있으면 **쓰지 않고** 종료코드 4.

## 준비물
Windows PowerShell, git, Python 3.11 이상. (Mac/Linux면 말씀해 주세요.) **각 줄의 "기대"와 다르면 멈춘다.** `STOP`이 나오면 창이 스스로 닫힌다(환경변수도 함께 사라짐).

## 실행 — 한 줄씩 복사해서 붙이고 Enter

**1. 도구 확인** — 기대: 3.11 이상 / git 버전
```
python --version
```
```
git --version
```

**2. 작업 폴더에 받기** — 기대: 오류 없음
```
$d = Join-Path $env:TEMP ("t198_" + (Get-Date -Format yyyyMMdd_HHmmss)); git clone --no-checkout https://github.com/Kim0900/jarvis-bot.git $d
```
```
Set-Location $d
```
```
git fetch origin task198-patrol-alert-stage1
```

**3. 승인 커밋으로 고정** — 기대: `OK commit` (아니면 `STOP` 후 창이 닫힘)
```
$sha = Read-Host "승인 이벤트의 커밋 40자리를 붙여넣고 Enter"
```
```
if ($sha -cmatch '^[0-9a-f]{40}$') { git checkout --detach $sha 2>$null | Out-Null; if (((git rev-parse HEAD) -ceq $sha) -and -not (git status --porcelain --untracked-files=no)) { "OK commit" } else { "STOP: 커밋 불일치 또는 변경됨"; exit 1 } } else { "STOP: 40자리 소문자 16진이 아님"; exit 1 }
```

**4. 스크립트 해시** — 기대: `OK script`
```
if ((Get-FileHash tests\live_smoke_task198_readonly.py -Algorithm SHA256).Hash -ieq "effe2cd0ebf1d7372c9520a4208f1b1f627bf081c96cd7dac5ec6de41e483bc0") { "OK script" } else { "STOP: 스크립트 해시 불일치"; exit 1 }
```

**5. 격리 환경** — 기대: 오류 없음
```
python -m venv .venv
```
```
.\.venv\Scripts\python.exe -m pip install -q httpx==0.25.2
```

**6. 승인 창 입력과 사전 점검(키 없이, 요청 없이)** — 기대: `commit pin OK; window OPEN` 과 `CHECK OK` (창이 아직 열리지 않았으면 `NOT YET OPEN` — 시작 시각 뒤에 6번부터 다시)
```
$nb = Read-Host "승인 창 시작 (예 2026-10-24T19:45:00+09:00)"
```
```
$na = Read-Host "승인 창 종료 (예 2026-10-24T21:00:00+09:00)"
```
```
.\.venv\Scripts\python.exe -I tests\live_smoke_task198_readonly.py --check-only --approved-commit $sha --not-before $nb --not-after $na --report "$d\smoke_report.json"
```

**7. 접속 정보 입력(화면에 표시되지 않음)** — 기대: 출력 없음. 6번이 `CHECK OK`일 때만.
```
$env:SUPABASE_URL = Read-Host "SUPABASE_URL"
```
```
$env:SUPABASE_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "SUPABASE_KEY" -AsSecureString)).Password
```
```
$env:SUPABASE_INTERNAL_RPC_SECRET = [System.Net.NetworkCredential]::new("", (Read-Host "SUPABASE_INTERNAL_RPC_SECRET" -AsSecureString)).Password
```

**8. 실행 + 비밀 검사 + 표시 + 환경변수 정리(한 줄, 도중에 오류가 나도 정리됨)** — 기대: `PASS` 줄들, `ALL PASS`, 보고서 JSON, 마지막에 `exit=0`. 보고서에 비밀값이 있으면 **내용을 표시하지 않고 파일을 지운 뒤** `STOP`만 출력한다(비밀 검사는 참/거짓만 쓰며 일치한 줄을 화면에 내지 않는다).
```
try { .\.venv\Scripts\python.exe -I tests\live_smoke_task198_readonly.py --approved-commit $sha --not-before $nb --not-after $na --report "$d\smoke_report.json"; $rc = $LASTEXITCODE; if (Select-String -Path "$d\smoke_report.json" -SimpleMatch -Pattern @(@($env:SUPABASE_KEY, $env:SUPABASE_INTERNAL_RPC_SECRET) | Where-Object { $_ }) -Quiet -ErrorAction SilentlyContinue) { Remove-Item "$d\smoke_report.json" -Force; "STOP: 보고서에 비밀값 의심 - 파일을 지웠다. 아무것도 보내지 말고 마기에게 '비밀 유출 의심'만 알린다(키 교체 판단은 마기)" } else { Get-Content "$d\smoke_report.json" -Raw }; "exit=$rc" } finally { Remove-Item Env:SUPABASE_URL, Env:SUPABASE_KEY, Env:SUPABASE_INTERNAL_RPC_SECRET -ErrorAction SilentlyContinue }
```

**9. 정리 확인** — 기대: `0` (환경변수 3개 모두 없음)
```
(Get-ChildItem Env: | Where-Object { $_.Name -like 'SUPABASE_*' } | Measure-Object).Count
```

## 결과 읽는 법
| 종료코드 | 뜻 | 할 일 |
|---|---|---|
| 0 | 모든 점검 통과 | 8번 화면 전체를 마기에게 |
| 1 | 점검 실패 또는 `NOT RUN`(비밀값 없음) | 통과 아님. 화면 전체 전달 |
| 2 | 승인 커밋·창 시작/종료·보고서 경로 오류 — **요청을 보내지 않음** | 값 확인 후 6번부터 |
| 3 | 첫 5xx·타임아웃·전송 오류·창 종료로 중간 중단 | 재시도 금지, 화면 전체 전달 |
| 4 | 보고서에 비밀값이 있어 **보고서를 쓰지 않음** | 아무것도 보내지 말고 마기에게 알림 |
- 같은 보고서 이름으로 두 번 못 돈다(덮어쓰기 방지). 다시 돌리려면 `smoke_report.json`을 다른 이름으로 바꾸거나 새 폴더에서 1번부터.
- 이 결과는 **증거**이며 자체 PASS가 아니다. 판단은 카산드라 검증 뒤 마기.
- 하지 않는 것: 쓰기·CAS·UNKNOWN 시험, 배포, 병합.
- 참고: 파일 삭제(`Remove-Item`)는 SSD에서 흔적 제거를 보장하지 않는다. 비밀 유출이 의심되면 키 교체는 마기가 판단한다.
