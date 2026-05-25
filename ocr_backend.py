
from flask import Flask, request, jsonify
import base64
import os
import requests
import json
import io
from PIL import Image
import logging
from datetime import datetime, timedelta, date, timezone

app = Flask(__name__)

# Supabase 및 Claude API 키는 환경 변수에서 로드
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')

KST = timezone(timedelta(hours=9))

DOW_KOR = ["월", "화", "수", "목", "금", "토", "일"]
FISH_TAG_EMOJI = {"유동인구": "🚶", "오피스": "🏢", "주거": "🏠", "상업": "🛍️", "환승": "🚏", "병원": "🏥", "학교": "🏫", "공항": "✈️", "역": "🚉", "터미널": "🚌", "호텔": "🏨", "관광": "🏞️", "번화가": "✨", "주말": "🗓️", "평일": "🗓️", "심야": "🌙", "새벽": "🌅", "출근": "🚗", "퇴근": "🚕", "혼잡": " congested", "한산": " calm"}


def get_fish_slot(hour: int) -> str | None:
    if 19 <= hour < 21:
        return "19~21"
    elif 21 <= hour < 24:
        return "21~24"
    elif 0 <= hour < 2:
        return "00~02"
    return None

def get_fish_report(hour: int) -> dict:
    slot = get_fish_slot(hour)
    current_time_window = slot if slot else "미확인"
    today = datetime.now(KST).date()
    day_of_week_kor = DOW_KOR[today.weekday()]

    # demand_map에서 현재 시간대 및 요일 데이터 조회
    demand_data = sb_select("demand_map", {
        "time_window": f"eq.{slot}",
        "day_of_week": f"eq.{day_of_week_kor}"
    })

    current_eta_advantage = 0
    official_var_score = 0
    top_time_window = "미확인"
    top_location = "미확인"
    analysis_summary = "미확인"

    if demand_data:
        # 가장 높은 콜 수 또는 평균 요금을 가진 지역 선택 (여기서는 첫 번째)
        data = demand_data[0]
        top_location = data.get('zone', '미확인')
        analysis_summary = data.get('note', '미확인')
        current_eta_advantage = data.get("eta_advantage", 0) # demand_map에서 직접 조회

    # daily_summary에서 official_var_score 조회
    score_data = sb_select("daily_summary", {"date": f"eq.{today.isoformat()}"})
    if score_data and score_data[0].get('official_var_score'):
        # official_var_score는 JSONB 타입이므로, 내부에서 var_6_eta_score를 추출
        official_var_score = score_data[0]["official_var_score"].get('var_6_eta_score')
        if official_var_score != "미확인":
            try:
                official_var_score = int(official_var_score)
            except ValueError:
                official_var_score = 0
    else:
        official_var_score = "미확인"

    return {
        "current_time_window": current_time_window,
        "current_eta_advantage": current_eta_advantage,
        "official_var_score": official_var_score,
        "top_time_window": top_time_window, # 현재는 slot과 동일하게 설정
        "top_location": top_location,
        "analysis_summary": analysis_summary
    }

HEADERS_SB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

def sb_h(method: str, path: str, **kwargs) -> dict | list | None:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    with requests.Session() as client:
        r = client.request(method, url, headers=HEADERS_SB, **kwargs)
        if r.status_code in (200, 201):
            return r.json()
        logger.error(f"Supabase {method} {path} → {r.status_code}: {r.text}")
        return None

def sb_select(table: str, params: dict = None) -> list:
    result = sb_h("GET", table, params=params or {})
    return result if isinstance(result, list) else []

def sb_get_setting(setting_name: str) -> str | None:
    # user_settings 테이블에서 특정 설정값 조회
    # 현재는 user_id를 하드코딩 (인증 기능 부재)
    settings = sb_select("user_settings", {"setting_name": f"eq.{setting_name}"})
    if settings:
        return settings[0].get('setting_value')
    return None

def sb_set_setting(setting_name: str, setting_value: str) -> dict | None:
    # user_settings 테이블에 설정값 저장/업데이트
    payload = {"setting_name": setting_name, "setting_value": setting_value}
    return sb_upsert("user_settings", payload, "setting_name")

def sb_get_goals() -> dict:
    daily = sb_get_setting("daily_goal")
    monthly = sb_get_setting("monthly_goal")
    return {"daily": int(daily) if daily else 150000, "monthly": int(monthly) if monthly else 4500000}

def sb_set_goals(daily: int, monthly: int):
    sb_set_setting("daily_goal", str(daily))
    sb_set_setting("monthly_goal", str(monthly))

def sb_get_server_url() -> str | None:
    return sb_get_setting("server_url")

def sb_set_server_url(url: str):
    sb_set_setting("server_url", url)

def sb_get_deductions() -> dict:
    ins = sb_get_setting("ded_ins")
    loan = sb_get_setting("ded_loan")
    chg = sb_get_setting("ded_chg")
    dep = sb_get_setting("ded_dep")
    etc = sb_get_setting("ded_etc")
    return {
        "ins": int(ins) if ins else 7945,
        "loan": int(loan) if loan else 0,
        "chg": int(chg) if chg else 0,
        "dep": int(dep) if dep else 0,
        "etc": int(etc) if etc else 0,
    }

def sb_set_deductions(ins: int, loan: int, chg: int, dep: int, etc: int):
    sb_set_setting("ded_ins", str(ins))
    sb_set_setting("ded_loan", str(loan))
    sb_set_setting("ded_chg", str(chg))
    sb_set_setting("ded_dep", str(dep))
    sb_set_setting("ded_etc", str(etc))

def sb_get_claude_key() -> str | None:
    return sb_get_setting("claude_key")

def sb_set_claude_key(key: str):
    sb_set_setting("claude_key", key)

def sb_upsert(table: str, data: dict, on_conflict: str) -> dict | None:
    return sb_h(
        "POST", table,
        json=data,
        headers={**HEADERS_SB, "Prefer": f"resolution=merge-duplicates,return=representation"},
        params={"on_conflict": on_conflict}
    )

def claude_vision(image_bytes: bytes, prompt: str, max_tokens: int = 500) -> str:
    """Claude API 비동기 호출. 모든 이미지 포맷을 JPEG로 정규화 후 전송."""

    def _prepare_image(raw: bytes) -> tuple[bytes, str]:
        """이미지를 JPEG로 변환 + 최대 높이 3000px 리사이즈"""
        try:
            img = Image.open(io.BytesIO(raw))
            # RGB 변환
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            # 최대 높이 3000px (세로 긴 이미지 처리)
            MAX_H = 3000
            if img.height > MAX_H:
                ratio = MAX_H / img.height
                img = img.resize((int(img.width * ratio), MAX_H), Image.LANCZOS)
                logger.info(f"이미지 높이 축소: {img.height}→{MAX_H}px")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue(), "image/jpeg"
        except Exception as e:
            logger.warning(f"이미지 변환 실패({e}) → 원본 사용")
            # 포맷 감지
            if raw[:4] == b"RIFF" or raw[:4] == b"WEBP":
                return raw, "image/webp"
            elif raw[:8] == b"\x89PNG\r\n\x1a\n":
                return raw, "image/png"
            return raw, "image/jpeg"

    img_data, media_type = _prepare_image(image_bytes)
    b64 = base64.b64encode(img_data).decode()

    headers = {
        'Content-Type': 'application/json',
        'x-api-key': CLAUDE_API_KEY,
        'anthropic-version': '2023-06-01',
    }
    payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': max_tokens,
        'messages': [
            {
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}},
                    {'type': 'text', 'text': prompt},
                ],
            }
        ],
    }

    with requests.Session() as session:
        claude_response = session.post('https://api.anthropic.com/v1/messages', headers=headers, json=payload)
    claude_response.raise_for_status()
    claude_data = claude_response.json()
    return claude_data['content'][0]['text'].strip()

def ocr_daily_history(image_bytes: bytes) -> dict | None:
    """
    카카오T '일별 운행 이력' 화면 OCR.
    반환: {날짜: "YYYY-MM-DD", 콜목록: [{배차시각, 하차시각, 출발지, 도착지, 요금, 결제방식}, ...]}
    """
    prompt = (
        "이 카카오T '일별 운행 이력' 화면에서 정보를 추출해서 JSON만 반환해줘.\n"
        '{"날짜":"YYYY-MM-DD","콜목록":['
        '{"배차시각":"HH:MM","하차시각":"HH:MM","출발지":"대구 OO구 OO동",'
        '"도착지":"대구 OO구 OO동","요금":숫자,"결제방식":"자동 또는 직접"}]}\n'
        "⚠️ 날짜: 상단 'YYYY년 M월 D일' → YYYY-MM-DD 변환\n"
        "⚠️ 배차시각: 각 콜의 앞 시각 (예: 02:58 - 03:11 에서 02:58)\n"
        "⚠️ 하차시각: 각 콜의 뒤 시각 (예: 02:58 - 03:11 에서 03:11)\n"
        "⚠️ 결제방식: '직접결제' 텍스트 있으면 '직접', 없으면 '자동'\n"
        "⚠️ 요금: 파란색 숫자. 직접결제는 표시된 요금 그대로 추출\n"
        "⚠️ 출발지/도착지: '대구 OO구 OO동' 형식. 구/동만 추출\n"
        "JSON만 반환. 설명·마크다운 금지."
    )
    try:
        raw = claude_vision(image_bytes, prompt, max_tokens=2000)
        raw = raw.strip()
        import re as _re
        raw = _re.sub(r"```json\s*", "", raw)
        raw = _re.sub(r"```\s*", "", raw)
        raw = raw.strip()
        import json as _json
        return _json.loads(raw)
    except Exception as e:
        print(f"일별운행이력 OCR 오류: {e}")
        return None

def sb_select_receipts(date_str: str, start_time: str, end_time: str, amount: int) -> list:
    # payment_receipts 테이블에서 해당 날짜, 시간, 금액에 일치하는 결제 내역 조회
    # Supabase는 시간 범위 쿼리를 지원하므로, start_time과 end_time을 활용
    params = {
        "날짜": f"eq.{date_str}",
        "시작시간": f"lte.{end_time}", # 콜의 하차시각이 결제 시작시간보다 늦거나 같고
        "종료시간": f"gte.{start_time}", # 콜의 배차시각이 결제 종료시간보다 빠르거나 같고
        "총매출": f"eq.{amount}"
    }
    return sb_select("payment_receipts", params)

def delete_duplicate_call(date_str: str, 배차시각: str, 요금: int) -> int:
    """
    raw_calls 테이블에서 중복 콜 삭제.
    동일 날짜, 배차시각, 요금의 콜이 있으면 삭제하고 삭제된 건수 반환.
    """
    # Supabase는 'delete' 메소드에서 'json' body를 지원하지 않으므로,
    # 'params'를 사용하여 필터링 조건을 전달합니다.
    params = {
        "날짜": f"eq.{date_str}",
        "배차시각": f"eq.{배차시각}",
        "요금": f"eq.{요금}"
    }
    # sb_h 함수는 이미 requests.Session()을 사용하므로, 동기적으로 호출 가능
    r = sb_h("DELETE", "raw_calls", params=params)
    if isinstance(r, list):
        return len(r)
    return 0

def cross_check_receipts(date_str: str) -> dict:
    """
    콜카드와 결제내역을 교차 대조하여 배회 영업을 식별하고, 결과를 반환합니다.
    매칭: 콜카드 하차시각 ↔ 결제시각 ±20분 (자정넘김 처리)
    미매칭 결제내역 자동분류:
      - 콜카드 운행 공백 시간대 → 배회영업 후보
      - 콜카드 운행 중 시간대   → 누락 콜카드 후보
    """
    from datetime import date as date_cls, timedelta

    calls = sb_select("raw_calls", {"날짜": f"eq.{date_str}"})
    try:
        y, mo, d = date_str.split("-")
        next_date_str = str(date_cls(int(y),int(mo),int(d)) + timedelta(days=1))
    except Exception:
        next_date_str = date_str

    receipts_today = sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})
    receipts_next  = sb_select("payment_receipts", {"날짜": f"eq.{next_date_str}"})
    receipts = receipts_today + receipts_next

    if not calls and not receipts:
        return {"status": "error", "message": f"⚠️ {date_str} 데이터 없음"}

    def to_min_smart(배차시각, 대상시각, 대상날짜):
        try:
            bh, bm = 배차시각.split(":")
            base = int(bh)*60+int(bm)
            th, tm = 대상시각.split(":")
            target = int(th)*60+int(tm)
            if 대상날짜 == next_date_str:
                target += 1440
            elif target < base - 60:
                target += 1440
            return target
        except: return None

    def to_min_abs(time_str, date_str_local):
        """날짜 기반 분 변환. mins<300 휴리스틱 제거 — 새벽 운행 오류 방지."""
        try:
            h, m = time_str.split(":")
            mins = int(h)*60+int(m)
            if date_str_local == next_date_str:
                mins += 1440  # 익일 날짜면 +1440만 적용
            return mins
        except: return None

    # STEP 1: 하차시각 ↔ 결제시각 ±20분 매칭
    matched_call_ids    = set()
    matched_receipt_ids = set()
    fee_mismatches      = []  # 금액 불일치 목록
    direct_updated      = []  # 직접결제 요금 자동업데이트 목록

    for i, call in enumerate(calls):
        배차 = call.get('배차시각') or ""
        하차 = call.get('하차시각')
        if not 하차:
            try:
                bh, bm = 배차.split(":")
                est = int(bh)*60+int(bm)+20
                하차 = f"{est//60%24:02d}:{est%60:02d}"
            except: continue
        c_min = to_min_smart(배차, 하차, date_str)
        best_j, best_diff = None, 99999
        for j, rcpt in enumerate(receipts):
            if j in matched_receipt_ids: continue
            rcpt_date = rcpt.get("날짜", date_str)
            r_min = to_min_smart(배차, rcpt.get('시각', '') or "", rcpt_date)
            if r_min and c_min:
                diff = abs(c_min - r_min)
                if diff <= 20 and diff < best_diff:
                    best_diff = diff
                    best_j = j
        if best_j is not None:
            matched_call_ids.add(i)
            matched_receipt_ids.add(best_j)
            call_fee = call.get('요금') or 0
            rcpt_fee = receipts[best_j].get('요금') or 0

            # 직접결제(요금=0) 콜카드 → 결제내역 요금으로 자동 업데이트
            if call_fee == 0 and rcpt_fee > 0:
                call_id = call.get('id')
                if call_id:
                    sb_h("PATCH", f"raw_calls?id=eq.{call_id}",
                               json={"요금": rcpt_fee, "비고": "직접결제(요금확인완료)"})
                    direct_updated.append({
                        "배차시각": 배차,
                        "요금": rcpt_fee,
                    })

            # 금액 불일치 (둘 다 0이 아니고 차이 ≥500원)
            elif call_fee > 0 and rcpt_fee > 0:
                fee_diff = abs(call_fee - rcpt_fee)
                FEE_DIFF_THRESHOLD = 500 # bot_v5.py에서 가져옴
                if fee_diff >= FEE_DIFF_THRESHOLD:
                    fee_mismatches.append({
                        "call_id": call.get('id'),
                        "배차시각": 배차,
                        "call_fee": call_fee,
                        "rcpt_fee": rcpt_fee,
                        "diff": fee_diff,
                    })

    unmatched_calls    = [c for i,c in enumerate(calls)    if i not in matched_call_ids]
    unmatched_receipts = [r for j,r in enumerate(receipts) if j not in matched_receipt_ids]

    # STEP 2: 콜카드 점유 시간대 계산 → 미매칭 결제내역 분류
    occupied = []
    for call in calls:
        배차 = call.get('배차시각') or ""
        하차 = call.get('하차시각') or ""
        if not 하차:
            try:
                bh, bm = 배차.split(":")
                est = int(bh)*60+int(bm)+20
                하차 = f"{est//60%24:02d}:{est%60:02d}"
            except: pass
        s = to_min_abs(배차, date_str)
        e = to_min_abs(하차, date_str)
        if s and e:
            occupied.append((s-5, e+5))

    baehoe_rcpt  = []
    missing_rcpt = []
    for r in unmatched_receipts:
        r_date = r.get('날짜', date_str)
        r_min  = to_min_abs(r.get('시각',"") or "", r_date)
        if r_min is None:
            missing_rcpt.append(r)
            continue
        if any(s <= r_min <= e for s,e in occupied):
            missing_rcpt.append(r)   # 운행 중 시간 → 누락 콜카드
        else:
            baehoe_rcpt.append(r)    # 공백 시간 → 배회영업

    # 결과 출력
    lines_out = [f"📊 교차대조 결과 — {date_str}"]
    lines_out.append(f"콜카드 {len(calls)}건 / 결제내역 {len(receipts)}건 / 매칭 {len(matched_call_ids)}건")
    lines_out.append("")

    # 직접결제 요금 자동업데이트 표시
    if direct_updated:
        lines_out.append(f"💳 직접결제 요금 자동확인 {len(direct_updated)}건:")
        for d in direct_updated:
            lines_out.append(f"  ✅ {d['배차시각']} → {d['요금']} 업데이트")
        lines_out.append("")

    if unmatched_calls:
        lines_out.append(f"🟠 콜카드에만 있음 {len(unmatched_calls)}건:")
        for c in unmatched_calls:
            배차시각 = c.get('배차시각', '-')
            출발지 = c.get('출발지', '')
            도착지 = c.get('도착지', '')
            요금 = c.get('요금') or 0
            lines_out.append(f"  {배차시각} {출발지}→{도착지} {요금}")
        lines_out.append("")

    if baehoe_rcpt:
        lines_out.append(f"🚶 배회영업 후보 (공백시간) {len(baehoe_rcpt)}건:")
        for r in baehoe_rcpt:
            날짜 = r.get('날짜', '')
            날짜표시 = f"({날짜})" if 날짜 != date_str else ""
            시각 = r.get('시각', '-')
            요금 = r.get('요금') or 0
            lines_out.append(f"  {시각}{날짜표시} {요금}")
        lines_out.append("")

    if missing_rcpt:
        lines_out.append(f"🔴 누락 콜카드 후보 (운행중 시간) {len(missing_rcpt)}건:")
        for r in missing_rcpt:
            날짜 = r.get('날짜', '')
            날짜표시 = f"({날짜})" if 날짜 != date_str else ""
            시각 = r.get('시각', '-')
            요금 = r.get('요금') or 0
            lines_out.append(f"  {시각}{날짜표시} {요금}")
        lines_out.append("")

    # 금액 불일치 표시
    if fee_mismatches:
        lines_out.append(f"💰 금액 불일치 {len(fee_mismatches)}건 (차이 ≥500원):")
        for fm in fee_mismatches:
            lines_out.append(
                f"  {fm_배차시각} 콜카드:{fm_call_fee} vs "
                f"결제:{fm_rcpt_fee} (차이 {fm_diff:,}원)"
            )
        lines_out.append("  → \'대조 금액확인 YYYY-MM-DD\' 로 버튼 선택")
        lines_out.append("")

    if not unmatched_calls and not unmatched_receipts:
        if fee_mismatches:
            lines_out.append("⚠️ 매칭 완료 — 금액 불일치 확인 필요")
        else:
            lines_out.append("✅ 완전 매칭 — 누락 없음")

    if unmatched_calls:
        lines_out.append(f"💡 \'배회분류 확정 {date_str}\' → 콜카드 미매칭 배회 처리")
    if baehoe_rcpt:
        lines_out.append(f"💡 \'대조 확정 {date_str}\' → 배회후보 {len(baehoe_rcpt)}건 raw_calls 자동 추가")

    return {"status": "success", "message": "\n".join(lines_out), "baehoe_rcpt": baehoe_rcpt, "unmatched_calls": unmatched_calls}

def process_daily_history(image_bytes: bytes):
    """
    일별 운행이력 이미지 처리:
    OCR → 날짜 보정 → raw_calls 저장 → 결과 안내
    """
    from datetime import date, timedelta

    data = ocr_daily_history(image_bytes)
    if not data or not data.get('콜목록'):
        return {"status": "error", "message": "일별 운행이력 인식 실패"}

    # 화면 날짜 파싱
    screen_date_str = data.get('날짜', '')
    try:
        screen_date = date.fromisoformat(screen_date_str)
    except Exception:
        screen_date = datetime.now(KST).date()
        print(f"날짜 파싱 실패: {screen_date_str} → 오늘 사용")

    DOW_MAP = ["월","화","수","목","금","토","일"]
    saved = 0
    updated = 0
    dates_used = set()
    result_lines = []

    for call in data.get("콜목록", []):
        배차 = call.get('배차시각', '')
        하차 = call.get('하차시각', '')
        출발 = call.get('출발지', '')
        도착 = call.get('도착지', '')
        요금 = call.get("요금", 0) or 0
        결제방식 = call.get('결제방식', '자동')

        # 날짜 보정: 자정 넘겨도 운행 시작일 귀속
        save_date = screen_date

        save_date_str = str(save_date)
        dow = DOW_MAP[save_date.weekday()]
        dates_used.add(save_date_str)

        # 중복 삭제 후 재저장
        deleted = delete_duplicate_call(save_date_str, 배차, 요금)
        if deleted:
            updated += deleted

        # 비고에 기간 명시 (자정 넘긴 운행)
        비고_추가 = []
        try:
            배차_dt = datetime.strptime(f"{screen_date_str} {배차}", "%Y-%m-%d %H:%M")
            하차_dt = datetime.strptime(f"{screen_date_str} {하차}", "%Y-%m-%d %H:%M")
            if 하차_dt < 배차_dt: # 자정 넘긴 경우
                하차_dt += timedelta(days=1)
                비고_추가.append(f"({screen_date.strftime('%m/%d')}~{(screen_date + timedelta(days=1)).strftime('%m/%d')})")
        except ValueError:
            pass # 시간 파싱 오류 무시

        비고 = "직접결제(요금미확인)" if 결제방식 == "직접" else None
        if 비고_추가:
            비고 = (비고 + " " + " ".join(비고_추가)) if 비고 else " ".join(비고_추가)

        # '배회' 로직 구현 (bot_v5.py의 cross_check_receipts 로직 참조)
        # 이 부분은 OCR된 개별 콜에 대한 처리이므로, 전체적인 교차대조 로직과는 다름.
        # 일단 OCR된 콜은 기본적으로 '카카오T'로 간주하고, 추후 전체 교차대조에서 '배회'를 식별.
        콜유형 = "카카오T"

        payload = {
            "날짜":     save_date_str,
            "요일":     dow,
            "배차시각": 배차,
            "하차시각": 하차,
            "출발지":   출발,
            "도착지":   도착,
            "요금":     요금,
            "콜유형":   콜유형,
            "비고":     비고,
        }
        result = sb_upsert("raw_calls", payload, "날짜,배차시각,요금") # on_conflict 추가
        if result:
            saved += 1
            직접표시 = " [직접결제]" if 결제방식 == "직접" else ""
            콜유형표시 = f" [{콜유형}]" if 콜유형 != "카카오T" else ""
            result_lines.append(
                f"  {배차}~{하차} {출발}→{도착} {요금:,}원{직접표시}{콜유형표시}"
            )

    # 결과 메시지
    dates_sorted = sorted(list(dates_used))
    msg = [
        f"✅ 일별 운행이력 저장 완료",
        f"총 {saved}건 저장 (중복 {updated}건 업데이트)"
    ]
    for d in dates_sorted:
        msg.append(f"- {d} ({DOW_MAP[date.fromisoformat(d).weekday()]})")
    msg.extend(result_lines)

    return {"status": "success", "message": "\n".join(msg)}


def calc_official_var_score(날짜: str):
    """카카오 AI 배차 공식 6변수 평가 및 daily_summary 저장"""
    today_date = date.today()
    mo = 날짜[:7]

    # var_2: 오늘 운행완료수
    calls_today = sb_select("raw_calls", {"날짜": f"eq.{날짜}"})
    daily_completed = len(calls_today)

    # var_2: 이번달 일평균
    calls_month = sb_select("raw_calls", {
        "and": f"(날짜.gte.{mo}-01,날짜.lte.{mo}-31)"
    })
    days_so_far = (today_date - date(int(mo[:4]), int(mo[5:7]), 1)).days + 1
    monthly_avg = len(calls_month) / max(days_so_far, 1)

    # var_3·4: sekuti에서 조회 (현재 0으로 고정 — 마스터 등급)
    avoid_count = 0
    one_star_count = 0

    # var_5: 수락률 (현재 100% 유지 중)
    acceptance_rate = 100

    # AI 진입 추정 (운행완료수 기반)
    AREA_AVG_LOW, AREA_AVG_HIGH = 18, 25
    if monthly_avg >= AREA_AVG_HIGH:
        ai_estimate = "85%+"
    elif monthly_avg >= AREA_AVG_LOW:
        ai_estimate = f"{int(60 + (monthly_avg-AREA_AVG_LOW)/(AREA_AVG_HIGH-AREA_AVG_LOW)*25)}%"
    else:
        ai_estimate = f"{int(40 + monthly_avg/AREA_AVG_LOW*20)}%"

    score = {
        "date": 날짜,
        "vars": {
            "var_1_acceptance_prob": "양호" if len(calls_month) >= 30 else "데이터 축적 중",
            "var_2_daily_completed": daily_completed,
            "var_2_monthly_avg": round(monthly_avg, 1),
            "var_2_area_avg_est": f"{AREA_AVG_LOW}~{AREA_AVG_HIGH}",
            "var_3_avoid_count_monthly": avoid_count,
            "var_4_one_star_monthly": one_star_count,
            "var_5_acceptance_rate": acceptance_rate,
            "var_6_eta_score": "위치 의존"
        },
        "weak_var": "var_2_daily_completed",
        "ai_inclusion_estimate": ai_estimate,
        "improvement_needed": "운행 시간 확대 + 매일 운행" if monthly_avg < AREA_AVG_LOW else "유지"
    }

    # daily_summary에 upsert
    sb_h("POST", f"daily_summary",
        json={"날짜": 날짜, "official_var_score": score},
        params={"on_conflict": "날짜"}
    )
    return score


@app.route('/ocr', methods=['POST'])
def ocr_process():
    if not CLAUDE_API_KEY:
        return jsonify({'error': 'Claude API 키가 설정되지 않았습니다.'}), 500

    data = request.get_json()
    if not data or 'image' not in data or 'media_type' not in data:
        return jsonify({'error': '이미지 데이터가 필요합니다.'}), 400

    image_base64 = data['image']
    media_type = data['media_type']

    try:
        image_bytes = base64.b64decode(image_base64)
        prompt = '''이 택시 매출집계 영수증에서 정보를 추출해서 JSON만 반환해줘.
{"날짜":"YYYY-MM-DD","시작시간":"HH:MM","종료시간":"HH:MM","카카오T":숫자,"카드":숫자,"현금":숫자,"총매출":숫자}
날짜:집계일/정산일/영업일(YYYY-MM-DD). 시작시간/종료시간:HH:MM 없으면 null.
카카오T:앱결제합계. 카드:카드결제합계. 현금:현금합계. 총매출:전체합계. 숫자만(원제외).
JSON만 반환. 설명금지.'''
        
        raw_text = claude_vision(image_bytes, prompt)
        ocr_result = json.loads(raw_text)
        
        return jsonify(ocr_result), 200

    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Claude API 요청 오류: {e}'}), 500
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Claude 응답 JSON 파싱 오류: {e}'}), 500
    except Exception as e:
        return jsonify({'error': f'서버 오류: {e}'}), 500


@app.route('/cross_check_receipts', methods=['POST'])
def cross_check_receipts_route():
    date_str = request.json.get('date')
    if not date_str:
        return jsonify({"status": "error", "message": "날짜(date)가 필요합니다."}), 400
    
    result = cross_check_receipts(date_str)
    return jsonify(result)

@app.route('/process_daily_history', methods=['POST'])
def process_daily_history_route():
    if not CLAUDE_API_KEY:
        return jsonify({'error': 'Claude API 키가 설정되지 않았습니다.'}), 500

    data = request.get_json()
    if not data or 'image' not in data or 'media_type' not in data:
        return jsonify({'error': '이미지 데이터가 필요합니다.'}), 400

    image_base64 = data['image']
    media_type = data['media_type']

    try:
        image_bytes = base64.b64decode(image_base64)
        result = process_daily_history(image_bytes)
        return jsonify(result), 200

    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Claude API 요청 오류: {e}'}), 500
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Claude 응답 JSON 파싱 오류: {e}'}), 500
    except Exception as e:
        return jsonify({'error': f'서버 오류: {e}'}), 500

@app.route('/fish_finder', methods=['GET'])
def fish_finder():
    hour_str = request.args.get('hour')
    try:
        hour = int(hour_str) if hour_str else datetime.now(KST).hour
        report = get_fish_report(hour)
        if report:
            return jsonify({'report': report}), 200
        else:
            return jsonify({'message': '해당 시간대의 어군 브리핑이 없습니다.'}), 404
    except ValueError:
        return jsonify({'error': '유효하지 않은 시간 형식입니다.'}), 400
    except Exception as e:
        return jsonify({'error': f'어군탐지기 오류: {e}'}), 500

@app.route('/calculate_score', methods=['POST'])
def calculate_score():
    data = request.get_json()
    if not data or 'date' not in data:
        return jsonify({'error': '날짜 데이터가 필요합니다.'}), 400
    
    target_date = data['date']
    try:
        score = calc_official_var_score(target_date)
        return jsonify(score), 200
    except Exception as e:
        return jsonify({'error': f'스코어 계산 오류: {e}'}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
