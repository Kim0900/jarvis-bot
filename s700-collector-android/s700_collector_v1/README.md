# S700 Collector v0.1

1차 목표
- GIT_ 장치 자동 탐색
- A002 / C301 NOTIFY 구독
- MTU 247 자동 요청
- 20바이트 조각 포함 모든 Notify 원문 HEX 저장
- STX(0x02)~ETX(0x03) 프레임 재조립
- JSONL 기록
- 연결 끊김 시 자동 재탐색

로그 위치
Android/data/com.example.s700collector/files/S700/s700_YYYYMMDD.jsonl

빌드 방법
1. Android Studio에서 프로젝트 열기
2. Gradle Sync
3. Build > Build APK(s)
4. app/build/outputs/apk/debug/app-debug.apk 설치

<!-- build trigger 1787861342 -->
<!-- retry 1787861549 -->
<!-- diag 1787861725 -->
