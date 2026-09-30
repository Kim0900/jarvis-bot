# Task #164 Golden 실OCR 검증 (Windows PowerShell)

이 작업은 PC에서만 실행한다. 운영 서버와 DB/Drive에는 쓰지 않는다. OCR 키와 원본 이미지는 GitHub/채팅에 올리지 않는다.

1. 대표님의 9/28 Golden 원본을 **다운로드**해 PC의 다운로드 폴더에 `golden.jpg`로 저장한다. 이미지 편집·재압축은 하지 않는다.
2. PC에 Python 3과 Git이 설치돼 있으면 PowerShell에서 아래를 순서대로 실행한다. `Pillow`만 추가 설치한다.

```powershell
git clone --depth 1 --single-branch --branch task164-layout-card-parser-poc https://github.com/Kim0900/jarvis-bot.git
Set-Location jarvis-bot
py -m pip install Pillow
py scripts/daily_history_overlay_poc.py --image "$HOME\Downloads\golden.jpg" --expected-count 10 --expected-sum 70400 --expected-direct-count 1 --prompt-key > result.json
```

3. `OCR_SPACE_API_KEY:` 표시가 나오면, 기존 OCR.space 키를 붙여넣고 Enter를 누른다. 입력 내용은 화면에 보이지 않는다. Render `jarvis-ocr-tesseract` 서비스의 Environment에 같은 이름으로 설정된 키다. 기존 키를 확인할 수 없으면 OCR.space에서 PC 검증용 무료 키를 발급받아 사용할 수 있다. **키를 채팅으로 보내지 않는다.**
4. 실행이 끝나면 아래 명령으로 개인정보가 빠진 요약만 확인한다. `result.json` 원본에는 주소가 포함될 수 있으므로 공개 게시하지 않는다.

```powershell
py -c "import json; d=json.load(open('result.json',encoding='utf-8-sig')); print({k:d.get(k) for k in ('ok','status','error_code','header','detected_card_count','observed_fare_sum','observed_direct_count','actual_total_ocr_calls','ocrspace_duration_ms')})"
```

통과 후보는 `ok=true`, `status=COMPLETE_LAYOUT_VALIDATED`, `header.expected_count=10`, `header.expected_sum=70400`, `observed_fare_sum=70400`, `observed_direct_count=1`, `actual_total_ocr_calls<=8`이다. 이것만으로 최종 승인하지 않는다. 각 카드의 좌표·시각·주소·요금이 원본 화면과 일치하는지, OCR 요청 지연이 운영 제한 안에 드는지도 확인한 뒤 CASSANDRA가 독립 검증한다.

오류가 나면 요약의 `error_code`와 터미널 오류만 공유한다. 키 또는 주소가 포함된 원본 JSON은 그대로 공유하지 않는다.
