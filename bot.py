"""
자비스(JARVIS) 텔레그램 봇 v5
인계문서 V2 완전 반영판
- 크-V2-08 버그 전체 수정
- V2 DB 스키마 반영 (charging_log, grade_standards, sekuti_weekly)
- 수락률 제거 (세큐티 자동배차 = 측정불가)
- 충전단가 160원/kWh 실측 반영
- 마스터 AND 조건: 종합95점↑ AND 상위5%
- 배회 후 배차 지연 +9.2분 브리핑 반영
"""

import os, re, json, base64, logging, threading
from datetime import datetime, date, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

load_dotenv()
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── KST 시간대 (버그-01 완전 해결) ───────────────
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)
def today_kst(): return now_kst().date()

# ── 환경변수 (구조-04: 하드코딩 제거) ────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_ID  = int(os.environ.get("ALLOWED_CHAT_ID", "0"))
ALLOWED_CHAT_ID2 = int(os.environ.get("ALLOWED_CHAT_ID2", "0"))
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── 상수 (V2 실측 반영) ───────────────────────────
NET_GOAL         = 100000
CARD_FEE_RATE    = 0.033
INSURANCE_DAILY  = 7945
KWH_PRICE        = 160      # 실측 확정 (V2)
DAILY_COST_BASE  = 19333    # 보험+충전+타이어+감가 (V2)
AVG_NET_FARE     = round(9504 * (1 - CARD_FEE_RATE))
DOW_KOR          = ["월","화","수","목","금","토","일"]

# 배회 후 배차 지연 실증치 (V2 체인분석)
HAEWHOE_DELAY_AVG  = 9.2   # 평균 +9.2분
HAEWHOE_DELAY_PEAK = 16.9  # 할증피크 +16.9분

EXPENSE_KEYWORDS = {
    "타이어": ("🔧 타이어교체", ["타이어"]),
    "오일":   ("🔧 오일교환",   ["오일","엔진오일"]),
    "세차":   ("🚿 세차",       ["세차"]),
    "보험":   ("📋 보험료",     ["보험"]),
}


# ════════════════════════════════════════════════
# Health Check 서버
# ════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis v5 OK")
    def log_message(self, *args): pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ════════════════════════════════════════════════
# Supabase REST API
# ════════════════════════════════════════════════
def sb_h():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

def sb_insert(t, d):
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=sb_h(), json=d, timeout=10)
        return r.status_code in [200,201]
    except Exception as e:
        logger.error(f"insert[{t}]: {e}"); return False

def sb_select(t, p=""):
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{p}", headers=sb_h(), timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.error(f"select[{t}]: {e}"); return []

def sb_upsert(t, d, c):
    try:
        h = sb_h(); h["Prefer"] = "resolution=merge-duplicates"
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}?on_conflict={c}", headers=h, json=d, timeout=10)
        return r.status_code in [200,201]
    except Exception as e:
        logger.error(f"upsert[{t}]: {e}"); return False

def sb_delete_last(t, td, extra_filter=""):
    try:
        q = f"날짜=eq.{td}&자동여부=eq.false&order=id.desc&limit=1&select=id"
        if extra_filter: q += f"&{extra_filter}"
        rows = sb_select(t, q)
        if not rows: return False
        r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{t}?id=eq.{rows[0]['id']}",
                         headers=sb_h(), timeout=10)
        return r.status_code in [200,204]
    except Exception as e:
        logger.error(f"delete[{t}]: {e}"); return False

def sb_patch(t, filter_q, data):
    try:
        r = httpx.patch(f"{SUPABASE_URL}/rest/v1/{t}?{filter_q}",
                        headers=sb_h(), json=data, timeout=10)
        return r.status_code in [200,204]
    except Exception as e:
        logger.error(f"patch[{t}]: {e}"); return False


# ════════════════════════════════════════════════
# 유틸리티
# ════════════════════════════════════════════════
def get_dow(d): return DOW_KOR[d.weekday()]
def fmt(a):
    try: return f"{int(a):,}원"
    except: return "0원"
def calc_net(rev): return round(rev * (1 - CARD_FEE_RATE))

def today_summary(td):
    data = sb_select("raw_calls", f"날짜=eq.{td}&select=요금,콜유형")
    total = sum(c.get("요금",0) or 0 for c in data)
    return {"건수":len(data), "매출":total, "순매출":calc_net(total),
            "배회":sum(1 for c in data if c.get("콜유형")=="배회")}

def today_expenses(td):
    data = sb_select("expenses",
        f"날짜=eq.{td}&select=카테고리,금액,메모,자동여부&order=id.asc")
    return {"items":data, "total":sum(c.get("금액",0) or 0 for c in data)}

def today_charging(td):
    """charging_log에서 당일 충전 데이터 조회"""
    data = sb_select("charging_log",
        f"충전일=eq.{td}&select=충전량_kwh,충전금액,충전소")
    total_kwh = sum(c.get("충전량_kwh",0) or 0 for c in data)
    total_amt = sum(c.get("충전금액",0) or 0 for c in data)
    return {"items":data, "total_kwh":total_kwh, "total_amt":total_amt}

def insurance_exists(td):
    return len(sb_select("expenses",
        f"날짜=eq.{td}&카테고리=eq.📋 보험료&select=id")) > 0

def insert_insurance(td):
    if not insurance_exists(td):
        sb_insert("expenses", {"날짜":str(td), "카테고리":"📋 보험료",
                               "금액":INSURANCE_DAILY, "메모":"자동기록",
                               "자동여부":True})

def get_strategy(hour, location=""):
    rows = sb_select("strategy_lookup",
        "select=행동지침,시간대,위치구분,우선순위")
    matched = []
    for s in rows:
        td_str=s.get("시간대",""); 위치=s.get("위치구분",""); pri=s.get("우선순위","보통")
        tok = td_str=="전체"
        if not tok and "~" in td_str:
            try:
                p=td_str.replace("시","").split("~")
                st,en=int(p[0].split(":")[0]),int(p[1].split(":")[0])
                tok=(st<=hour<en) if st<=en else (hour>=st or hour<en)
            except: pass
        lok = 위치=="어디서나" or not location or 위치 in location
        if tok and lok and pri in ["긴급","높음"]: matched.append(s)
    if not matched: return ""
    matched.sort(key=lambda x:{"긴급":0,"높음":1}.get(x.get("우선순위"),2))
    return matched[0].get("행동지침","")

def auth_check(update):
    cid = update.effective_chat.id
    return cid == ALLOWED_CHAT_ID or (ALLOWED_CHAT_ID2 and cid == ALLOWED_CHAT_ID2)

def parse_expense(text):
    am = re.search(r'(\d[\d,]+)', text.replace(" ",""))
    if not am: return None, None
    amount = int(am.group(1).replace(",",""))
    for key,(cat,kws) in EXPENSE_KEYWORDS.items():
        for kw in kws:
            if kw in text: return cat, amount
    if text.startswith("지출"):
        parts=text.split(); memo=parts[1] if len(parts)>2 else "기타"
        return f"📦 {memo}", amount
    return None, None

def parse_manual_call(text):
    is_h = any(kw in text for kw in ["배회","길","길빵"])
    m = re.search(r'(\d{4,6})', text.replace(" ",""))
    if not m: return None
    amount = int(m.group(1))
    dest_m = re.search(r'\d+\s+(.+)', text)
    destination = dest_m.group(1).strip() if dest_m else ""
    return {"요금":amount, "콜유형":"배회" if is_h else "카카오T",
            "목적지힌트":destination}

# 버그-03: 스케줄러 KST 완전 적용
def insurance_scheduler():
    import time
    while True:
        n = now_kst()
        next_run = n.replace(hour=0, minute=1, second=0, microsecond=0)
        if n >= next_run: next_run += timedelta(days=1)
        time.sleep((next_run - n).total_seconds())
        insert_insurance(today_kst())
        logger.info(f"보험료 자동기록: {today_kst()} {fmt(INSURANCE_DAILY)}")
        time.sleep(60)


# ════════════════════════════════════════════════
# 마스터 AND 조건 판정 (V2: 종합95↑ AND 상위5%)
# ════════════════════════════════════════════════
def check_master_v2(종합점수, 상위pct):
    """V2 확정: km 조건 없음. 종합95↑ AND 상위5% 동시 충족"""
    c1 = 종합점수 >= 95
    c2 = 상위pct <= 5.0
    return c1 and c2, c1, c2

def get_grade_from_standards(종합점수, 상위pct):
    """grade_standards 테이블 기반 등급 판정"""
    rows = sb_select("grade_standards",
        "select=등급,점수_하한,점수_상한,순위_기준_pct&order=점수_하한.desc")
    for r in rows:
        min_s = r.get("점수_하한") or 0
        max_s = r.get("점수_상한") or 100
        rank_pct = r.get("순위_기준_pct")
        if min_s <= 종합점수 <= max_s:
            if rank_pct is None or 상위pct <= rank_pct:
                return r.get("등급","일반")
    return "일반"

def get_stage_info():
    """Stage 지표 + 마스터 V2 AND 조건 판정"""
    today = today_kst()
    month_start = today.replace(day=1)

    calls = sb_select("raw_calls",
        f"날짜=gte.{month_start}&날짜=lte.{today}&select=요금,콜유형")
    total_rev = sum(c.get("요금",0) or 0 for c in calls)
    count = len(calls)
    avg_fare = total_rev // count if count else 0

    # sekuti_weekly 최신값
    weekly = sb_select("sekuti_weekly",
        f"기록일=gte.{month_start}&select=월누적km,종합평점,전체순위_pct,마스터조건_점수,마스터조건_km&order=기록일.desc&limit=1")
    w = weekly[0] if weekly else {}
    km        = float(w.get("월누적km") or 0)
    종합점수  = int(w.get("종합평점") or 0)
    상위pct   = float(w.get("전체순위_pct") or 100)

    is_master, c1, c2 = check_master_v2(종합점수, 상위pct)

    # grade_standards에서 등급 조회
    grade = get_grade_from_standards(종합점수, 상위pct)

    gap = []
    if not c1: gap.append(f"종합점수 {95-종합점수}점↑ 필요")
    if not c2: gap.append(f"상위 {상위pct-5:.1f}%p 단축 필요")

    # Stage 판정
    stage = 2 if avg_fare >= 10000 else 1

    # stage_metrics 최신값 (연속콜 등)
    sm = sb_select("stage_metrics",
        "select=건당단가_평균,공차시간_평균,경산루프건수&order=기간종료.desc&limit=1")
    연속콜_rate = 21.0  # 기본값 (V6 확정)

    return {
        "stage":     stage,
        "avg_fare":  avg_fare,
        "km":        km,
        "종합점수":  종합점수,
        "상위pct":   상위pct,
        "grade":     grade,
        "마스터":    is_master,
        "master_gap": gap,
        "c1_점수":   c1,
        "c2_순위":   c2,
    }




# ════════════════════════════════════════════════
# 실시간 수요지도 OCR → demand_map 저장
# ════════════════════════════════════════════════
async def process_demand_map(image_bytes, update):
    """카카오T 실시간 수요지도 스크린샷 → OCR → demand_map 저장"""
    await update.message.reply_text("🗺️ 수요지도 분석 중...")
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = """이 카카오T 실시간 수요지도 이미지를 분석해서 JSON만 반환해줘.
{
  "고수요구역": ["빨간색(진한) 구역의 지역명 목록"],
  "중수요구역": ["주황/연한 구역의 지역명 목록"],
  "현재위치": "택시 아이콘 위치 지역명 또는 null"
}
지도에 표시된 텍스트(역명,구청,동명)만 사용. 없으면 빈 배열. JSON만."""

    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
                {"type":"text","text":prompt}
            ]}]
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            await update.message.reply_text("❌ 수요지도 인식 실패. 다시 올려주세요."); return
        data = json.loads(m.group())
    except Exception as e:
        await update.message.reply_text("❌ 분석 오류."); logger.error(f"수요지도OCR: {e}"); return

    n = now_kst(); today = n.date(); hour = n.hour
    dow = get_dow(today)

    고수요 = data.get("고수요구역") or []
    중수요 = data.get("중수요구역") or []
    현재위치 = data.get("현재위치") or ""

    if not 고수요 and not 중수요:
        await update.message.reply_text("⚠️ 수요 구역을 인식하지 못했습니다. 지도가 잘 보이도록 다시 캡처해 주세요."); return

    sb_insert("demand_map", {
        "기록일":    str(today),
        "요일":      dow,
        "시간":      hour,
        "고수요구역": 고수요,
        "중수요구역": 중수요,
        "현재위치":  현재위치,
    })

    msg = f"🗺️ 수요지도 기록완료\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{today.strftime('%m/%d')} ({dow}) {hour}시\n"
    if 고수요:
        msg += f"\n🔴 고수요 구역\n"
        for g in 고수요: msg += f"  {g}\n"
    if 중수요:
        msg += f"\n🟡 중수요 구역\n"
        for g in 중수요: msg += f"  {g}\n"
    if 현재위치:
        msg += f"\n현재위치: {현재위치}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"

    # 누적 건수 조회
    count = sb_select("demand_map", f"요일=eq.{dow}&시간=eq.{hour}&select=id")
    msg += f"이 시간대 누적: {len(count)}회"
    if len(count) < 10:
        msg += f" (패턴 분석은 10회 이상 수집 후)"

    await update.message.reply_text(msg)

# ════════════════════════════════════════════════
# 세큐티 운행리포트 OCR → sekuti_weekly 저장
# ════════════════════════════════════════════════
async def process_sekuti_report(image_bytes, update):
    """세큐티 앱 운행리포트 스크린샷 → OCR → sekuti_weekly 저장"""
    await update.message.reply_text("📊 세큐티 등급 분석 중...")
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = """이 세큐티 운행리포트 이미지에서 정보를 추출해서 JSON만 반환해줘.
{
  "종합평점": 숫자 또는 null,
  "안전운전점수": 숫자 또는 null,
  "실내공기점수": 숫자 또는 null,
  "kakao_평점": 숫자 또는 null,
  "전체순위_pct": 숫자(상위 N%) 또는 null,
  "월누적km": 숫자 또는 null,
  "등급": "마스터 또는 나이스 또는 화이팅 또는 null"
}
JSON만 반환. 설명 없음."""

    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
                {"type":"text","text":prompt}
            ]}]
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            await update.message.reply_text("❌ 세큐티 화면 인식 실패. 다시 올려주세요."); return
        data = json.loads(m.group())
    except Exception as e:
        await update.message.reply_text("❌ 분석 오류."); logger.error(f"세큐티OCR: {e}"); return

    n = now_kst(); today = n.date()

    # sekuti_weekly 저장
    record = {"기록일": str(today)}
    if data.get("월누적km"):    record["월누적km"]       = data["월누적km"]
    if data.get("종합평점"):    record["종합평점"]       = data["종합평점"]
    if data.get("안전운전점수"): record["안전운전점수"]  = data["안전운전점수"]
    if data.get("실내공기점수"): record["실내공기점수"]  = data["실내공기점수"]
    if data.get("kakao_평점"):  record["kakao_평점"]     = data["kakao_평점"]
    if data.get("전체순위_pct"): record["전체순위_pct"]  = data["전체순위_pct"]
    if data.get("등급"):        record["등급"]           = data["등급"]

    # 월누적km 없으면 최소값 필요
    if "월누적km" not in record: record["월누적km"] = 0

    sb_upsert("sekuti_weekly", record, "기록일")

    # 마스터 AND 조건 판정
    종합 = data.get("종합평점") or 0
    순위 = data.get("전체순위_pct") or 100
    km   = data.get("월누적km") or 0
    is_master, c1, c2 = check_master_v2(종합, 순위)

    gap = []
    if not c1: gap.append(f"종합점수 {95-종합:.0f}점↑ 필요")
    if not c2: gap.append(f"상위 {순위-5:.1f}%p 단축 필요")

    # VOC 경고
    실내공기 = data.get("실내공기점수") or 0
    voc_warn = 실내공기 > 0 and 실내공기 < 92

    msg = f"📊 세큐티 등급 업데이트\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    if data.get("등급"):     msg += f"등급: {data['등급']}\n"
    if 종합:                 msg += f"종합평점: {종합}점\n"
    if data.get("안전운전점수"): msg += f"안전운전: {data['안전운전점수']}점\n"
    if 실내공기:             msg += f"실내공기: {실내공기}점{'  ⚠️' if voc_warn else ''}\n"
    if data.get("kakao_평점"):   msg += f"카카오T: {data['kakao_평점']}점\n"
    if 순위 < 100:           msg += f"전체순위: 상위 {순위}%\n"
    if km:                   msg += f"월누적km: {km:,.1f}km {'✅' if km>=2000 else '⏳'}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"마스터 조건 (종합95↑ AND 상위5%)\n"
    msg += f"  종합점수: {종합}점 {'✅' if c1 else '❌'}\n"
    msg += f"  전체순위: 상위{순위}% {'✅' if c2 else '❌'}\n"
    if is_master:
        msg += f"  → 🎉 마스터 조건 충족!\n"
    elif gap:
        msg += f"  → 잔여: {' / '.join(gap)}\n"

    if voc_warn:
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"⚠️ 실내공기 {실내공기}점 주의\n"
        msg += f"VOC 개선 → 93점 → 종합 97점 → 상위5% 진입"

    await update.message.reply_text(msg)

# ════════════════════════════════════════════════
# 이미지 자동 분류 (Haiku)
# ════════════════════════════════════════════════
async def classify_image(b64):
    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=20,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
                {"type":"text","text":"이 이미지가 무엇인지 한 단어만: 콜카드 / 충전영수증 / 결제내역 / 세큐티등급 / 수요지도 / 기타"}
            ]}]
        )
        t = resp.content[0].text.strip()
        if "충전" in t: return "충전"
        if "결제" in t: return "결제"
        if "세큐티" in t or "등급" in t: return "세큐티"
        if "수요" in t: return "수요지도"
        return "콜카드"
    except:
        return "콜카드"


# ════════════════════════════════════════════════
# 콜카드 OCR (Haiku — 개선-01)
# ════════════════════════════════════════════════
async def process_call_card(image_bytes, update):
    await update.message.reply_text("📥 콜카드 분석 중...")
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = ('이 콜카드 이미지에서 정보를 추출해서 JSON만 반환해줘.\n'
              '{"배차시각":"HH:MM","출발지":"OO구 OO동","도착지":"OO구 OO동",'
              '"요금":숫자,"카드사":"카드사명","콜유형":"카카오T 또는 배회"}')
    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
                {"type":"text","text":prompt}
            ]}]
        )
        m = re.search(r'\{.*\}', resp.content[0].text.strip(), re.DOTALL)
        if not m:
            await update.message.reply_text("❌ 콜카드 인식 실패. 다시 올려주세요."); return
        data = json.loads(m.group())
    except Exception as e:
        await update.message.reply_text("❌ 분석 오류."); logger.error(f"OCR: {e}"); return

    n = now_kst(); call_date = n.date()
    배차시각 = data.get("배차시각","")
    if 배차시각:
        try:
            h = int(배차시각.split(":")[0])
            if h < 4 and n.hour >= 4: call_date = n.date()
            elif h < 4: call_date = n.date() - timedelta(days=1)
        except: pass

    요일 = get_dow(call_date)
    if not sb_insert("raw_calls",{
        "날짜":str(call_date), "요일":요일, "배차시각":배차시각,
        "출발지":data.get("출발지"), "도착지":data.get("도착지"),
        "요금":data.get("요금"), "콜유형":data.get("콜유형","카카오T"),
        "비고":data.get("카드사","")}):
        await update.message.reply_text("❌ DB 저장 실패."); return

    s = today_summary(call_date); exp = today_expenses(call_date)
    순수익 = s["순매출"] - exp["total"]
    달성률 = round(순수익/NET_GOAL*100,1)
    잔여 = max(0, NET_GOAL-순수익)
    잔여콜 = round(잔여/AVG_NET_FARE) if 잔여>0 else 0
    try: h2 = int(배차시각.split(":")[0]) if 배차시각 else n.hour
    except: h2 = n.hour
    strategy = get_strategy(h2, data.get("출발지",""))
    g_tag = "\n     🔥 경산 루프! 복귀콜 대기 권장." if "경산" in str(data.get("도착지","")) else ""
    유형 = data.get("콜유형","카카오T")

    msg = (f"✅ *콜카드 저장완료* — {call_date.strftime('%m/%d')} ({요일})\n\n"
           f"{'🚖' if 유형=='카카오T' else '🚶'} {배차시각 or '시각미확인'} | "
           f"{data.get('출발지','미확인')} → {data.get('도착지','미확인')}\n"
           f"💰 {fmt(data.get('요금',0))} | {유형}{g_tag}\n"
           f"━━━━━━━━━━━━━━━━━━━━\n"
           f"📊 *오늘 누계*\n"
           f"  건수: {s['건수']}건 | 매출: {fmt(s['매출'])}\n"
           f"  순수익: {fmt(순수익)}\n"
           f"  목표({fmt(NET_GOAL)}) 달성률: *{달성률}%*\n"
           f"  잔여: {fmt(잔여)} (약 {잔여콜}콜)")
    if 순수익 >= NET_GOAL: msg += "\n  🎉 *오늘 목표 달성!*"
    if strategy: msg += f"\n━━━━━━━━━━━━━━━━━━━━\n🎯 *지금 행동*\n{strategy}"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# 충전 OCR → charging_log (버그-02: 배열 처리)
# ════════════════════════════════════════════════
async def process_charge_receipt(image_bytes, update):
    await update.message.reply_text("⚡ 충전 내역 분석 중...")
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = """이 충전 영수증에서 모든 충전 내역을 추출해서 JSON 배열만 반환해줘.
[{"충전일자":"YYYY-MM-DD 또는 null","충전소":"이름 또는 null","충전량_kwh":숫자 또는 null,"충전금액":숫자}]
여러 건이면 여러 항목. JSON 배열만."""

    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=500,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
                {"type":"text","text":prompt}
            ]}]
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            await update.message.reply_text("❌ 충전 내역 인식 실패."); return
        charge_list = json.loads(m.group())
        if not isinstance(charge_list, list): charge_list = [charge_list]
    except Exception as e:
        await update.message.reply_text("❌ 분석 오류."); logger.error(f"충전OCR: {e}"); return

    n = now_kst(); saved = []; warned = []
    for data in charge_list:
        amt = data.get("충전금액")
        kwh = data.get("충전량_kwh")
        if not amt or amt > 100000:
            if amt: warned.append(f"금액 이상: {amt:,}원")
            continue

        charge_date = n.date()
        if data.get("충전일자"):
            try:
                from datetime import date as ddate
                charge_date = ddate.fromisoformat(data["충전일자"])
            except: pass

        충전소 = data.get("충전소") or "미확인"
        단가 = round(amt/kwh, 1) if kwh and kwh > 0 else None
        단가_표시 = f"{단가}원/kWh" if 단가 else ""

        if sb_insert("charging_log", {
            "충전일": str(charge_date), "충전소": 충전소,
            "충전량_kwh": kwh, "충전금액": int(amt), "비고": 단가_표시
        }):
            kwh_s = f"{kwh}kWh " if kwh else ""
            saved.append(f"{charge_date.strftime('%m/%d')} {충전소} {kwh_s}{fmt(amt)}")

    if not saved:
        await update.message.reply_text("❌ 저장 가능한 충전 내역이 없습니다."); return

    # 오늘 충전 합계
    ch = today_charging(n.date())
    exp = today_expenses(n.date()); s = today_summary(n.date())
    카수 = s["매출"] - s["순매출"]
    총비용 = exp["total"] + 카수 + ch["total_amt"]
    순수익 = s["순매출"] - exp["total"] - ch["total_amt"]

    msg = f"⚡ *충전 기록 저장완료* ({len(saved)}건)\n"
    for r in saved: msg += f"  {r}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n오늘 충전 합계\n"
    if ch["total_kwh"]: msg += f"  {ch['total_kwh']}kWh / {fmt(ch['total_amt'])}\n"
    if ch["total_kwh"] and ch["total_amt"]:
        실단가 = round(ch["total_amt"]/ch["total_kwh"],1)
        기준차이 = 실단가 - KWH_PRICE
        msg += f"  실측단가: {실단가}원/kWh (기준 {KWH_PRICE}원 {'↑' if 기준차이>0 else '↓'}{abs(기준차이):.1f}원)\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n현재 순수익: {fmt(순수익)}"
    if warned:
        msg += f"\n⚠️ 이상값: {', '.join(warned)}"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# 지출 처리 (충전 제외 — charging_log로 분리됨)
# ════════════════════════════════════════════════
async def handle_expense(text, update):
    today = today_kst(); cat, amount = parse_expense(text)
    if not cat or not amount:
        await update.message.reply_text("💬 금액을 함께 입력해주세요.\n예: 세차 5000"); return
    if not sb_insert("expenses",{"날짜":str(today),"카테고리":cat,"금액":amount,"메모":text,"자동여부":False}):
        await update.message.reply_text("❌ 지출 저장 실패."); return
    exp = today_expenses(today); s = today_summary(today)
    순수익 = s["순매출"] - exp["total"]
    msg = (f"💸 *지출 기록완료*\n{cat}: {fmt(amount)}\n"
           f"━━━━━━━━━━━━━━━━━━━━\n"
           f"오늘 지출합계: {fmt(exp['total'])}\n"
           f"현재 순수익: {fmt(순수익)}")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_expense_check(update):
    today = today_kst(); exp = today_expenses(today); ch = today_charging(today)
    msg = f"📋 *오늘 지출 내역* — {today.strftime('%m/%d')}\n━━━━━━━━━━━━━━━━━━━━"
    if ch["items"]:
        for item in ch["items"]:
            kwh = item.get("충전량_kwh")
            msg += f"\n⚡ 전기충전: {fmt(item.get('충전금액',0))}"
            if kwh: msg += f" ({kwh}kWh)"
    if exp["items"]:
        for item in exp["items"]:
            msg += f"\n{item['카테고리']}: {fmt(item['금액'])}{'  (자동)' if item.get('자동여부') else ''}"
    total = exp["total"] + ch["total_amt"]
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\n합계: {fmt(total)}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_expense_cancel(update):
    today = today_kst()
    if sb_delete_last("expenses", today):
        exp = today_expenses(today)
        await update.message.reply_text(f"✅ 마지막 지출 삭제완료.\n현재 지출합계: {fmt(exp['total'])}")
    else:
        await update.message.reply_text("❌ 삭제할 지출이 없습니다.")


# ════════════════════════════════════════════════
# 수동 콜 + 대조
# ════════════════════════════════════════════════
async def handle_manual_call(text, update):
    n = now_kst(); today = n.date(); 입력시각 = n.strftime("%H:%M")
    parsed = parse_manual_call(text)
    if not parsed:
        await update.message.reply_text("💬 금액을 입력해주세요.\n예: 콜 7800"); return
    if not sb_insert("pending_calls",{
        "날짜":str(today), "입력시각":입력시각, "요금":parsed["요금"],
        "콜유형":parsed["콜유형"], "목적지힌트":parsed["목적지힌트"], "상태":"임시"}):
        await update.message.reply_text("❌ 임시 저장 실패."); return
    pending = sb_select("pending_calls", f"날짜=eq.{today}&상태=eq.임시&select=요금")
    cnt=len(pending); total=sum(c.get("요금",0) or 0 for c in pending)
    유이 = "🚖" if parsed["콜유형"]=="카카오T" else "🚶"
    dest = f" → {parsed['목적지힌트']}" if parsed["목적지힌트"] else ""
    msg = (f"📝 *임시기록* {입력시각}\n{유이} {fmt(parsed['요금'])}{dest}\n"
           f"━━━━━━━━━━━━━━━━━━━━\n"
           f"오늘 임시기록: {cnt}건 / {fmt(total)}")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_compare(update):
    today = today_kst()
    pending = sb_select("pending_calls",
        f"날짜=eq.{today}&상태=eq.임시&select=id,입력시각,요금,콜유형,목적지힌트&order=입력시각.asc")
    actual = sb_select("raw_calls",
        f"날짜=eq.{today}&select=id,배차시각,요금,출발지,도착지&order=배차시각.asc")
    if not pending:
        await update.message.reply_text("📋 오늘 임시기록이 없습니다."); return
    if not actual:
        await update.message.reply_text("📋 콜카드 기록이 없습니다.\n콜카드 이미지를 먼저 올려주세요."); return
    matched=[]; up=list(pending); ua=list(actual)
    for p in list(up):
        for a in list(ua):
            if p["요금"]==a["요금"]:
                matched.append(("✅",p,a,0)); up.remove(p); ua.remove(a); break
    for p in list(up):
        for a in list(ua):
            if abs(p["요금"]-a["요금"])<=1000:
                matched.append(("⚠️",p,a,a["요금"]-p["요금"])); up.remove(p); ua.remove(a); break
    msg = f"📋 *{today.strftime('%m/%d')} 대조 결과*\n━━━━━━━━━━━━━━━━━━━━"
    for i,(st,p,a,diff) in enumerate(matched,1):
        if diff!=0:
            msg += f"\n{i}. {st} {p['입력시각']} {fmt(p['요금'])} → 실제 {fmt(a['요금'])} ({diff:+,}원)"
        else:
            msg += f"\n{i}. {st} {p['입력시각']} {fmt(p['요금'])}"
        if a.get("출발지") and a.get("도착지"):
            msg += f"\n   {a['출발지']} → {a['도착지']}"
    for p in up: msg += f"\n❌ 임시만: {p['입력시각']} {fmt(p['요금'])}"
    for a in ua:
        ds = f" {a.get('출발지','')}→{a.get('도착지','')}" if a.get("출발지") else ""
        msg += f"\n❓ 콜카드만: {a.get('배차시각','')} {fmt(a['요금'])}{ds}"
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\n확정하려면 확정 입력"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_confirm(update):
    today = today_kst(); dow = get_dow(today)
    pending = sb_select("pending_calls", f"날짜=eq.{today}&상태=eq.임시&select=*")
    if not pending:
        await update.message.reply_text("📋 확정할 임시기록이 없습니다."); return
    confirmed = 0
    for p in pending:
        if sb_insert("raw_calls",{
            "날짜":str(today), "요일":dow, "배차시각":p.get("입력시각"),
            "출발지":None, "도착지":p.get("목적지힌트") or None,
            "요금":p.get("요금"), "콜유형":p.get("콜유형","카카오T"),
            "비고":"수동입력"}):
            confirmed += 1
            sb_patch("pending_calls", f"id=eq.{p['id']}", {"상태":"확정"})
    s = today_summary(today)
    await update.message.reply_text(
        f"✅ *{confirmed}건 확정완료*\n오늘 누계: {s['건수']}건 / {fmt(s['매출'])}",
        parse_mode="Markdown")


# ════════════════════════════════════════════════
# km 입력 → sekuti_weekly
# ════════════════════════════════════════════════
async def handle_km(text, update):
    today = today_kst(); dow = get_dow(today)
    m = re.match(r'km\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    if not m:
        await update.message.reply_text("💬 예: km 2113"); return
    km = float(m.group(1))
    sb_upsert("sekuti_weekly", {"기록일":str(today),"월누적km":km}, "기록일")
    마스터_km = km >= 2000
    msg = (f"🚗 *월 km 기록*\n"
           f"현재: {km:,.1f}km\n"
           f"마스터 km(2,000): {'✅ 달성' if 마스터_km else f'⏳ 잔여 {2000-km:,.0f}km'}\n"
           f"※ 마스터 조건은 km 외에 종합95점↑ AND 상위5% 필요")
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# 휴무
# ════════════════════════════════════════════════
async def handle_rest_day(update):
    today = today_kst(); dow = get_dow(today)
    s = today_summary(today)
    if s["건수"]>0:
        await update.message.reply_text(f"⚠️ 오늘 콜 {s['건수']}건 있어 휴무 불가."); return
    insert_insurance(today)
    sb_upsert("daily_summary",{"날짜":str(today),"요일":dow,"총건수":0,"총매출":0,
                               "배회영업건수":0,"정상여부":"휴무","휴무여부":True},"날짜")
    msg = (f"📅 *{today.strftime('%m/%d')} ({dow}) — 휴무*\n"
           f"보험료: {fmt(INSURANCE_DAILY)} (자동기록)\n내일도 화이팅! 💪")
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# A모드 — 실시간 전략
# ════════════════════════════════════════════════
async def handle_realtime(text, update):
    n = now_kst(); hm = re.search(r'(\d{1,2})시',text)
    hour = int(hm.group(1)) if hm else n.hour
    loc = next((kw for kw in ["수성구","성내","중구","동구","달서구","북구","서구","경산"] if kw in text),"")
    strategy = get_strategy(hour, loc)
    today = n.date(); dow = get_dow(today)
    s = today_summary(today); exp = today_expenses(today); ch = today_charging(today)
    순수익 = s["순매출"] - exp["total"] - ch["total_amt"]
    gm = re.search(r'공차\s*(\d+)분', text)
    # strategy_lookup에 데이터 없으면 Claude 호출 없이 직접 안내
    if not strategy:
        await update.message.reply_text(
            f"⚡ {hour}시 전략\n━━━━━━━━━━━━━━━━━━━━\n"
            f"현재 {hour}시는 영업 비추천 시간입니다.\n"
            f"충전·휴식·차량점검 권장. 19시 출근 준비하세요.")
        return
    prompt = (f"대구 전기차 택시 전략 AI 자비스야.\n"
              f"현재: {hour}시 | 위치: {loc or '미확인'} | {f'공차 {gm.group(1)}분 경과' if gm else ''}\n"
              f"오늘({dow}): {s['건수']}건 순수익 {fmt(순수익)} / 목표 {fmt(NET_GOAL)}\n"
              f"전략DB 내용만 사용해서 답해줘: {strategy}\n"
              f"지금 즉시 할 행동 3줄. 대표님이라고 불러줘. 이모지 활용.\n"
              f"마크다운 기호 절대 사용 금지.\n"
              f"반드시 전략DB 내용만 사용. DB 외 정보 절대 추가하지 말 것.")
    try:
        resp = claude_client.messages.create(model="claude-sonnet-4-6", max_tokens=250,
            messages=[{"role":"user","content":prompt}])
        advice = resp.content[0].text.strip()
    except: advice = strategy or "현재 전략 데이터를 확인해주세요."
    msg = f"⚡ {hour}시 전략\n━━━━━━━━━━━━━━━━━━━━\n{advice}"
    await update.message.reply_text(msg)


# ════════════════════════════════════════════════
# B모드 — 마감 브리핑 (V2 완전 반영)
# ════════════════════════════════════════════════
async def handle_report(update):
    today = today_kst(); dow = get_dow(today)
    insert_insurance(today)

    s    = today_summary(today)
    exp  = today_expenses(today)
    ch   = today_charging(today)
    카수  = s["매출"] - s["순매출"]
    순수익 = s["순매출"] - exp["total"] - ch["total_amt"]
    달성률 = round(순수익/NET_GOAL*100,1)
    tomorrow_dow = get_dow(today + timedelta(days=1))

    # 전주 동요일
    prev_data = sb_select("daily_summary",
        f"날짜=eq.{today-timedelta(days=7)}&select=총건수,총매출")
    prev = prev_data[0] if prev_data else None

    # 요일별 평균
    dow_data = sb_select("daily_summary",
        f"요일=eq.{dow}&정상여부=eq.정상&select=총매출,총건수")
    dow_avg_rev = int(sum(d.get("총매출",0) or 0 for d in dow_data)/len(dow_data)) if dow_data else 0

    # 시간대별
    calls = sb_select("raw_calls", f"날짜=eq.{today}&select=배차시각,요금,콜유형")
    hour_stats = {}
    for c in calls:
        if c.get("배차시각"):
            try:
                h = int(str(c["배차시각"]).split(":")[0])
                if h not in hour_stats: hour_stats[h] = []
                if c.get("요금"): hour_stats[h].append(c["요금"])
            except: pass

    # ── 메시지 조립 ──────────────────────────────
    msg = f"📊 *{today.strftime('%m/%d')} ({dow}) 마감 브리핑*\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"총매출: {fmt(s['매출'])}\n"
    msg += f"카드수수료: △{fmt(카수)}\n"
    msg += f"순매출: {fmt(s['순매출'])}\n"

    # 비용 항목 상세
    msg += "━━━━━━━━━━━━━━━━━━━━\n💸 *오늘 비용*\n"
    if ch["items"]:
        for item in ch["items"]:
            kwh = item.get("충전량_kwh")
            충전소 = item.get("충전소","")
            kwh_s = f" {kwh}kWh" if kwh else ""
            msg += f"  ⚡ 충전: {fmt(item.get('충전금액',0))}{kwh_s} / {충전소}\n"
    보험_items = [x for x in exp["items"] if "보험" in x.get("카테고리","")]
    기타_items = [x for x in exp["items"] if "보험" not in x.get("카테고리","")]
    for item in 보험_items:
        msg += f"  {item['카테고리']}: {fmt(item['금액'])} (자동)\n"
    for item in 기타_items:
        note = f" ({item.get('메모','')})" if item.get("메모") else ""
        msg += f"  {item['카테고리']}: {fmt(item['금액'])}{note}\n"
    if 카수 > 0: msg += f"  수수료: {fmt(카수)}\n"
    총비용 = exp["total"] + ch["total_amt"] + 카수
    msg += f"  ─────────────────\n  총비용: {fmt(총비용)} (수수료 포함)\n"

    # 순수익
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"실질순수익: *{fmt(순수익)}*\n"
    msg += f"목표 달성률: *{달성률}%*"
    if 순수익 >= NET_GOAL: msg += " 🎉"
    건당 = s["매출"]//s["건수"] if s["건수"] else 0
    msg += f"\n건수: {s['건수']}건 | 건당: {fmt(건당)}\n"

    # 배회 후 배차 지연 (V2 체인분석 반영)
    배회건수 = s["배회"]
    if 배회건수 > 0:
        msg += "━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"🚶 *배회영업 {배회건수}건*\n"
        is_peak = any(h in hour_stats for h in [21,22,23])
        delay = HAEWHOE_DELAY_PEAK if is_peak else HAEWHOE_DELAY_AVG
        msg += f"  배회 후 배차 지연: +{delay}분 {'⚠️(할증피크)' if is_peak else '(평균)'}\n"
        msg += f"  ※ 카카오T 대비 +{HAEWHOE_DELAY_AVG}분 실증\n"

    # 비교 분석
    msg += "━━━━━━━━━━━━━━━━━━━━\n📈 *비교 분석*\n"
    if prev and prev.get("총매출"):
        diff=s["매출"]-prev["총매출"]; pct=round(diff/prev["총매출"]*100,1)
        msg += f"전주 동요일({(today-timedelta(days=7)).strftime('%m/%d')}): {fmt(prev['총매출'])} {'⬆️' if diff>=0 else '⬇️'}{pct:+.1f}%\n"
    else:
        msg += "전주 동요일: 데이터 없음\n"
    if dow_avg_rev:
        diff2=s["매출"]-dow_avg_rev; pct2=round(diff2/dow_avg_rev*100,1)
        msg += f"{dow}요일 평균({len(dow_data)}회): {fmt(dow_avg_rev)} {'⬆️' if diff2>=0 else '⬇️'}{pct2:+.1f}%\n"

    # 시간대별
    if hour_stats:
        msg += "━━━━━━━━━━━━━━━━━━━━\n⏰ *시간대별*\n"
        for h in sorted(hour_stats.keys()):
            fares = hour_stats[h]
            msg += f"  {h:02d}시: {len(fares)}건 평균 {fmt(sum(fares)//len(fares) if fares else 0)}\n"

    # Stage + 마스터 AND 조건 (V2)
    try:
        si = get_stage_info()
        msg += "━━━━━━━━━━━━━━━━━━━━\n📊 *알고리즘 Stage*\n"
        msg += f"Stage {si['stage']} | 등급: {si['grade']} | 건당: {fmt(si['avg_fare'])}\n"
        if si["종합점수"] or si["상위pct"] < 100:
            msg += f"  종합점수: {si['종합점수']}점 {'✅' if si['c1_점수'] else '❌(95점↑필요)'}\n"
            msg += f"  전체순위: 상위{si['상위pct']:.1f}% {'✅' if si['c2_순위'] else '❌(5%↑필요)'}\n"
            if si["마스터"]:
                msg += "  → 🎉 마스터 조건 충족!\n"
            elif si["master_gap"]:
                msg += f"  → 잔여: {' / '.join(si['master_gap'])}\n"
    except: pass

    # Claude 내일 전략
    try:
        prompt = (f"대구 전기차 택시 전략 AI 자비스야.\n"
                  f"오늘({dow}): {s['건수']}건 / 순수익 {fmt(순수익)} / 달성률 {달성률}%\n"
                  f"내일은 {tomorrow_dow}요일.\n"
                  f"잘한 점 1줄, 개선점 1줄, 내일 핵심전략 1줄만.\n"
                  f"대표님이라고 불러줘. 마크다운 기호 절대 사용 금지.")
        resp = claude_client.messages.create(model="claude-sonnet-4-6", max_tokens=200,
            messages=[{"role":"user","content":prompt}])
        msg += f"━━━━━━━━━━━━━━━━━━━━\n{resp.content[0].text.strip()}"
    except: pass

    await update.message.reply_text(msg, parse_mode="Markdown")
    sb_upsert("daily_summary",{
        "날짜":str(today), "요일":dow, "총건수":s["건수"],
        "총매출":s["매출"], "배회영업건수":s["배회"],
        "목표달성여부":순수익>=NET_GOAL, "정상여부":"정상", "휴무여부":False
    },"날짜")


async def handle_today_quick(update):
    today = today_kst(); dow = get_dow(today)
    s = today_summary(today); exp = today_expenses(today); ch = today_charging(today)
    순수익 = s["순매출"] - exp["total"] - ch["total_amt"]
    달성률 = round(순수익/NET_GOAL*100,1)
    잔여 = max(0,NET_GOAL-순수익)
    잔여콜 = round(잔여/AVG_NET_FARE) if 잔여>0 else 0
    msg = (f"📊 *오늘 현황* — {today.strftime('%m/%d')} ({dow})\n"
           f"건수: {s['건수']}건 | 매출: {fmt(s['매출'])}\n"
           f"지출: {fmt(exp['total']+ch['total_amt'])} | 순수익: *{fmt(순수익)}*\n"
           f"목표 달성률: *{달성률}%*\n잔여: {fmt(잔여)} (약 {잔여콜}콜)")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_weekly(update):
    today = today_kst(); ws = today - timedelta(days=today.weekday())
    calls = sb_select("raw_calls", f"날짜=gte.{ws}&날짜=lte.{today}&select=날짜,요금")
    exps  = sb_select("expenses",  f"날짜=gte.{ws}&날짜=lte.{today}&select=금액")
    chs   = sb_select("charging_log", f"충전일=gte.{ws}&충전일=lte.{today}&select=충전금액")
    if not calls:
        await update.message.reply_text("📊 이번 주 데이터가 없습니다."); return
    tr = sum(c.get("요금",0) or 0 for c in calls)
    te = sum(e.get("금액",0) or 0 for e in exps) + sum(c.get("충전금액",0) or 0 for c in chs)
    np = calc_net(tr)-te; cnt=len(calls); days=len(set(c["날짜"] for c in calls))
    msg = (f"📈 *이번 주 성과*\n━━━━━━━━━━━━━━━━━━━━\n"
           f"기간: {ws.strftime('%m/%d')} ~ {today.strftime('%m/%d')}\n"
           f"운행일: {days}일 | 총건수: {cnt}건\n"
           f"총매출: {fmt(tr)} | 지출: △{fmt(te)}\n"
           f"━━━━━━━━━━━━━━━━━━━━\n주간 순수익: *{fmt(np)}*\n"
           f"일평균 순수익: {fmt(np//days if days else 0)}")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_monthly(update):
    today = today_kst(); ms = today.replace(day=1)
    calls = sb_select("raw_calls", f"날짜=gte.{ms}&날짜=lte.{today}&select=날짜,요금")
    exps  = sb_select("expenses",  f"날짜=gte.{ms}&날짜=lte.{today}&select=금액,카테고리")
    chs   = sb_select("charging_log", f"충전일=gte.{ms}&충전일=lte.{today}&select=충전금액,충전량_kwh")
    tr = sum(c.get("요금",0) or 0 for c in calls)
    te = sum(e.get("금액",0) or 0 for e in exps)
    tc = sum(c.get("충전금액",0) or 0 for c in chs)
    tkwh = sum(c.get("충전량_kwh",0) or 0 for c in chs)
    np = calc_net(tr)-te-tc; cnt=len(calls); days=len(set(c["날짜"] for c in calls))
    cat_totals={}
    for e in exps:
        cat=e.get("카테고리","기타"); cat_totals[cat]=cat_totals.get(cat,0)+(e.get("금액",0) or 0)
    msg = (f"📅 *{today.strftime('%m')}월 성과*\n━━━━━━━━━━━━━━━━━━━━\n"
           f"운행일: {days}일 | 총건수: {cnt}건\n총매출: {fmt(tr)}\n"
           f"━━━━━━━━━━━━━━━━━━━━\n📋 *지출 내역*\n")
    if tc: msg += f"  ⚡ 전기충전: {fmt(tc)} ({tkwh:.1f}kWh)\n"
    for cat,amt in sorted(cat_totals.items(),key=lambda x:-x[1]):
        msg += f"  {cat}: {fmt(amt)}\n"
    msg += (f"  합계: △{fmt(te+tc)}\n━━━━━━━━━━━━━━━━━━━━\n"
            f"월 순수익: *{fmt(np)}*\n일평균: {fmt(np//days if days else 0)}")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_db_check(update):
    today = today_kst()
    tc = sb_select("raw_calls","select=id")
    td = sb_select("raw_calls",f"날짜=eq.{today}&select=id")
    tp = sb_select("pending_calls",f"날짜=eq.{today}&상태=eq.임시&select=id")
    lc = sb_select("raw_calls","select=날짜,배차시각,요금&order=id.desc&limit=1")
    ins = insurance_exists(today)
    msg = (f"📊 *DB 현황*\n━━━━━━━━━━━━━━━━━━━━\n"
           f"전체 콜: {len(tc)}건\n"
           f"오늘 확정: {len(td)}건\n"
           f"오늘 임시: {len(tp)}건\n")
    if lc:
        l=lc[0]; msg+=f"마지막 입력: {l.get('날짜','')} {l.get('배차시각','')} {fmt(l.get('요금',0))}\n"
    msg += f"오늘 보험료: {'✅' if ins else '❌'}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_error_check(update):
    calls = sb_select("raw_calls",
        "select=날짜,배차시각,출발지,도착지,요금,콜유형&order=id.desc&limit=50")
    errors=[]
    for c in calls:
        if not c.get("출발지") and c.get("콜유형")!="배회":
            errors.append(f"⚠️ {c.get('날짜','')} {c.get('배차시각','')} {fmt(c.get('요금',0))} — 출발지 없음")
        if c.get("요금") and c["요금"]<3000:
            errors.append(f"❓ {c.get('날짜','')} {c.get('배차시각','')} {fmt(c.get('요금',0))} — 요금 낮음")
    if not errors:
        await update.message.reply_text("✅ 오류 항목 없습니다."); return
    msg = f"🔍 *오류 확인 필요* ({len(errors)}건)\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += "\n".join(errors[:10])
    if len(errors)>10: msg += f"\n... 외 {len(errors)-10}건"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# 텔레그램 핸들러
# ════════════════════════════════════════════════
async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    msg=("🤖 *자비스(JARVIS) v5 가동*\n━━━━━━━━━━━━━━━━━━━━\n"
         "📥 *데이터 업로드*\n"
         "  콜카드 이미지 → OCR 자동저장\n"
         "  충전 이미지(캡션: 충전) → 충전 기록\n"
         "  콜 7800 → 수동 임시기록\n"
         "  배회 5600 → 배회 임시기록\n"
         "  대조 → 임시 vs 콜카드 대조\n"
         "  확정 → 임시기록 확정저장\n"
         "━━━━━━━━━━━━━━━━━━━━\n"
         "💸 *지출 입력*\n"
         "  타이어/오일/세차 금액\n"
         "  지출 내용 금액 → 기타\n"
         "  지출취소 → 마지막 삭제\n"
         "  휴무 → 휴무 처리\n"
         "━━━━━━━━━━━━━━━━━━━━\n"
         "📊 *조회*\n"
         "  오늘 / 오늘 마감 / 이번 주 / 이번 달\n"
         "  지출 확인 / DB 확인 / 오류 확인\n"
         "━━━━━━━━━━━━━━━━━━━━\n"
         "⚡ *전략·추적*\n"
         "  전략 / 실시간 / 지금 N시 위치\n"
         "  km N → 월 km 기록\n"
         "/명령어 → 전체 목록")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_id(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"채팅 ID: `{update.effective_chat.id}`",
                                    parse_mode="Markdown")

async def cmd_help(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    lines_h=[
        "📋 전체 명령어 (자비스 v5 | V2)",
        "━━━━━━━━━━━━━━━━━━━━",
        "📥 데이터 업로드",
        "  콜카드 이미지 → OCR 자동저장+브리핑",
        "  충전 이미지(캡션: 충전) → charging_log 저장",
        "  콜 7800 → 수동 임시기록",
        "  배회 5600 / 길 5600 → 배회 임시기록",
        "  대조 → 임시기록 vs 콜카드 대조",
        "  확정 → 임시기록 확정저장",
        "━━━━━━━━━━━━━━━━━━━━",
        "💸 지출 입력",
        "  타이어 80000 / 오일 35000 / 세차 5000",
        "  지출 내용 금액 → 기타",
        "  지출취소 → 마지막 삭제",
        "  휴무 → 휴무 처리",
        "  ※ 충전은 이미지 업로드로만 입력",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 조회",
        "  오늘 → 현황 빠른 조회",
        "  오늘 마감 → 전체 브리핑",
        "  이번 주 → 주간 성과",
        "  이번 달 → 월간 성과",
        "  지출 확인 → 오늘 지출 목록",
        "  DB 확인 → DB 현황",
        "  오류 확인 → 에러 데이터",
        "━━━━━━━━━━━━━━━━━━━━",
        "⚡ 전략·추적",
        "  전략 / 실시간 → 지금 전략",
        "  지금 N시 위치 → 맞춤 전략",
        "  공차 N분째 → 재진입 거점",
        "  km N → 월 km 기록",
        "/명령어 → 이 목록 다시 보기",
    ]
    await update.message.reply_text("\n".join(lines_h))


async def handle_document(update:Update, context:ContextTypes.DEFAULT_TYPE):
    """파일로 전송된 이미지 처리 (handle_photo와 동일 로직)"""
    if not auth_check(update): return
    doc = update.message.document
    if not doc or not doc.mime_type or not doc.mime_type.startswith("image/"):
        await update.message.reply_text("이미지 파일만 지원합니다."); return
    file = await context.bot.get_file(doc.file_id)
    image_bytes = bytes(await file.download_as_bytearray())
    caption = (update.message.caption or "").lower()

    if any(kw in caption for kw in ["충전","charge","kwh"]):
        await process_charge_receipt(image_bytes, update); return
    if any(kw in caption for kw in ["세큐티","등급","sekuti"]):
        await process_sekuti_report(image_bytes, update); return
    if any(kw in caption for kw in ["수요","수요지도","demand"]):
        await process_demand_map(image_bytes, update); return

    # 파일명으로 2차 분류
    filename = (doc.file_name or "").lower()
    if "call_history" in filename or "콜" in filename:
        await process_call_card(image_bytes, update)
    else:
        b64 = __import__('base64').standard_b64encode(image_bytes).decode("utf-8")
        img_type = await classify_image(b64)
        if img_type == "충전":
            await process_charge_receipt(image_bytes, update)
        elif img_type == "세큐티":
            await process_sekuti_report(image_bytes, update)
        elif img_type == "수요지도":
            await process_demand_map(image_bytes, update)
        else:
            await process_call_card(image_bytes, update)

async def handle_photo(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())
    caption = (update.message.caption or "").lower()

    # 캡션 기반 1차 분류
    if any(kw in caption for kw in ["충전","charge","kwh"]):
        await process_charge_receipt(image_bytes, update); return
    if any(kw in caption for kw in ["세큐티","등급","sekuti"]):
        await process_sekuti_report(image_bytes, update); return
    if any(kw in caption for kw in ["수요","수요지도","demand"]):
        await process_demand_map(image_bytes, update); return

    # Haiku 자동 분류
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    img_type = await classify_image(b64)
    if img_type == "충전":
        await process_charge_receipt(image_bytes, update)
    elif img_type == "세큐티":
        await process_sekuti_report(image_bytes, update)
    elif img_type == "수요지도":
        await process_demand_map(image_bytes, update)
    else:
        await process_call_card(image_bytes, update)

async def handle_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    text = update.message.text.strip()

    if text == "휴무": await handle_rest_day(update); return
    if "지출취소" in text: await handle_expense_cancel(update); return
    if text == "지출 확인": await handle_expense_check(update); return
    if text in ["DB 확인","db확인","DB확인"]: await handle_db_check(update); return
    if text in ["오류 확인","오류확인"]: await handle_error_check(update); return
    if text in ["대조","콜 대조"]: await handle_compare(update); return
    if text in ["확정","콜 확정"]: await handle_confirm(update); return

    # km 입력
    if re.match(r'km\s*\d', text, re.IGNORECASE):
        await handle_km(text, update); return

    # 지출 입력 (충전 제외 — 충전은 이미지로만)
    exp_kws = ["타이어","오일","엔진오일","세차","보험","지출"]
    if any(kw in text for kw in exp_kws) and bool(re.search(r'\d{3,}',text)):
        await handle_expense(text, update); return

    if any(t in text for t in ["마감","오늘 정리","결산"]):
        await handle_report(update); return
    if text in ["오늘","현황"]: await handle_today_quick(update); return
    if any(t in text for t in ["이번 달","이번달","월간"]): await handle_monthly(update); return
    if any(t in text for t in ["이번 주","이번주","주간"]): await handle_weekly(update); return

    if any(t in text for t in ["전략","실시간","브리핑","오늘 전략","지금 전략"]):
        await handle_realtime(text, update); return

    manual_kws = ["콜 ","배회 ","길 ","길빵 "]
    if any(text.startswith(kw) for kw in manual_kws) and bool(re.search(r'\d{4,6}',text)):
        await handle_manual_call(text, update); return

    if re.search(r'\d{1,2}시',text) or any(t in text for t in ["지금","공차","어디","뭐해야"]):
        await handle_realtime(text, update); return

    today = today_kst(); dow = get_dow(today)
    s = today_summary(today); exp = today_expenses(today); ch = today_charging(today)
    순수익 = s["순매출"] - exp["total"] - ch["total_amt"]
    prompt = (f"대구 전기차 택시 전략 AI 자비스야.\n"
              f"오늘 {dow}요일 | {s['건수']}건 | 순수익 {fmt(순수익)} / 목표 {fmt(NET_GOAL)}\n"
              f"대표님 메시지: {text}\n"
              f"간결하고 실용적으로. 대표님이라고 불러줘.\n"
              f"마크다운 기호 절대 사용 금지. DB에 없는 정보 절대 지어내지 말 것.")
    try:
        resp = claude_client.messages.create(model="claude-sonnet-4-6", max_tokens=300,
            messages=[{"role":"user","content":prompt}])
        reply = resp.content[0].text.strip()
    except: reply = "잠시 후 다시 시도해 주세요."
    await update.message.reply_text(reply)


# ════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════
def main():
    logger.info("자비스 봇 v5 (V2 인계문서 반영) 시작...")
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=insurance_scheduler, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("명령어", cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("봇 v5 폴링 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
