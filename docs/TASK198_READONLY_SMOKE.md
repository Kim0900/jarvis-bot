# TASK#198 실서버 읽기 전용 스모크 — 대표님 실행 절차 (PR#75)

상태: **준비만 됨. 캐스퍼는 실행하지 않았다**(작업 환경에서 supabase.co 접속 불가). 이 절차는 **마기가 새 허가와 종료 시각을 준 뒤**, 접속이 열린 대표님 PC에서만 실행한다. 허가 전에는 1번도 실행하지 않는다.

## 범위 (이것만 한다)
- GET 요청 약 10건. 쓰기·PATCH·POST·DELETE·CAS·배포 없음 — 스크립트가 GET 전용 클라이언트만 쓰며 다른 메서드는 요청 전에 예외가 난다(`tests/test_task198_live_probe.py`).
- 쓰기 프로브는 PR에서 제거됨(마기 #5721, 카산드라 #5718). 이력(커밋 23ec8aa)에만 있다.
- 주의: `SUPABASE_KEY`는 봇이 쓰는 키라 키 자체는 쓰기 권한이 있을 수 있다. 읽기 전용은 **스크립트가 GET만 보내는 것**으로 보장된다 → 그래서 아래 3단계(커밋 고정)와 4단계(스크립트 해시)를 건너뛰면 안 된다.
- 첫 5xx·타임아웃·전송 오류에서 즉시 중단(재시도 없음, 종료코드 3). 허가 종료 시각 이후에는 요청을 보내지 않는다.

## 준비물
Windows PowerShell, git, Python 3.11 이상. (Mac/Linux면 말씀해 주세요 — 다른 줄을 드립니다.) 마기에게서 받을 값 3개: **① PR#75 최종 head 40자리**, **② 허가 종료 시각**(예 `2026-10-22T18:00:00+09:00`, 오프셋 필수), **③ 이 문서의 스크립트 해시와 같은지 확인할 값(아래 4단계에 이미 적혀 있음)**. 키 값은 화면에 보이지 않게 입력하며 명령 기록에 남지 않는다.

## 실행 — 한 줄씩 복사해서 붙이고 Enter. 각 줄의 "기대"가 다르면 **멈추고** 출력 전체를 마기에게 전달.

**1. 도구 확인** — 기대: 3.11 이상 / git 버전 출력
```
python --version
```
```
git --version
```

**2. 작업 폴더에 받기** — 기대: 오류 없이 끝남(인증 창이 뜨면 평소 GitHub 계정으로)
```
$d = Join-Path $env:TEMP ("t198_" + (Get-Date -Format yyyyMMdd_HHmmss)); git clone --no-checkout https://github.com/Kim0900/jarvis-bot.git $d
```
```
Set-Location $d
```
```
git fetch origin task198-patrol-alert-stage1
```

**3. 마기가 지정한 커밋으로 고정** — 기대: `OK commit`
```
$sha = Read-Host "PR75 최종 head 40자리를 붙여넣고 Enter"
```
```
git checkout --detach $sha
```
```
if ($sha.Length -eq 40 -and (git rev-parse HEAD) -ieq $sha -and -not (git status --porcelain --untracked-files=no)) { "OK commit" } else { "STOP: 커밋 불일치 또는 변경됨" }
```

**4. 스크립트가 검증된 그대로인지 해시 확인** — 기대: `OK script`
```
if ((Get-FileHash tests\live_smoke_task198_readonly.py -Algorithm SHA256).Hash -ieq "e8f7de1e4626931007e941b445b52b704705a95ce0912969e3cc243015b9f51b") { "OK script" } else { "STOP: 스크립트 해시 불일치" }
```

**5. 격리 환경 만들고 httpx 설치** — 기대: `Successfully installed httpx-0.25.2` 등
```
python -m venv .venv
```
```
.\.venv\Scripts\python.exe -m pip install -q httpx==0.25.2
```

**6. 접속 정보 입력(화면에 표시되지 않음)** — 기대: 입력 후 아무 출력 없음
```
$env:SUPABASE_URL = Read-Host "SUPABASE_URL"
```
```
$env:SUPABASE_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "SUPABASE_KEY" -AsSecureString)).Password
```
```
$env:SUPABASE_INTERNAL_RPC_SECRET = [System.Net.NetworkCredential]::new("", (Read-Host "SUPABASE_INTERNAL_RPC_SECRET" -AsSecureString)).Password
```
```
$na = Read-Host "마기가 준 허가 종료 시각 (예 2026-10-22T18:00:00+09:00)"
```

**7. 실행** — 기대: `PASS` 줄들과 마지막 `ALL PASS`, 종료코드 0
```
.\.venv\Scripts\python.exe -I tests\live_smoke_task198_readonly.py --not-after $na --report "$d\smoke_report.json"
```
```
$LASTEXITCODE
```

**8. 보고서에 키·비밀이 없는지 확인** — 기대: 출력 없음(한 줄이라도 나오면 보고서를 보내지 말고 마기에게 알림)
```
Select-String -Path "$d\smoke_report.json" -SimpleMatch -Pattern @($env:SUPABASE_KEY, $env:SUPABASE_INTERNAL_RPC_SECRET)
```

**9. 보고서 내용을 마기에게 전달** — 화면 전체를 복사해 붙인다
```
Get-Content "$d\smoke_report.json" -Raw
```

**10. 정리(키 지우기)** — 기대: 출력 없음
```
Remove-Item Env:SUPABASE_URL, Env:SUPABASE_KEY, Env:SUPABASE_INTERNAL_RPC_SECRET
```

## 결과 읽는 법
| 종료코드 | 뜻 | 할 일 |
|---|---|---|
| 0 | 모든 점검 통과 | 보고서 전달 |
| 1 | 점검 실패 또는 `NOT RUN`(비밀값 없음) 있음 | 통과 아님. 보고서 전달 |
| 2 | 사용 오류(시각 형식, 이미 있는 보고서 파일, 허가 종료 시각 지남) — **요청을 보내지 않음** | 값을 고쳐 7번만 다시 |
| 3 | 첫 5xx·타임아웃·전송 오류·창 종료로 **중간 중단** | 재시도하지 말고 보고서 전달 |
- 7번은 같은 보고서 이름으로 두 번 못 돈다(덮어쓰기 방지). 다시 돌리려면 파일 이름을 바꾼다.
- 이 결과는 **증거**이며 자체 PASS가 아니다. 판단은 카산드라 검증 뒤 마기.
- 하지 않는 것: 쓰기·CAS·UNKNOWN 시험, 배포, 병합. 별도 승인·업로드 없는 시간대 전까지 금지.

## 안전 장치 (가짜 서버 시험으로 확인: tests/test_task198_live_probe.py)
GET 전용 클라이언트, 5xx·타임아웃 즉시 중단, 창 종료 후 무전송, 보고서는 요청 목록(테이블·파라미터 이름)·응답 코드·Content-Range·행 수·점검 결과만(키·비밀·행 내용 없음), 보고서 파일은 **요청 전에** 확보(이미 있으면 아무것도 보내지 않음), Ctrl-C여도 지금까지의 관측을 기록. 문서의 명령 줄과 해시는 시험이 스크립트와 대조한다.
