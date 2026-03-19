"""
자비스(JARVIS) 텔레그램 봇 v4 최종판
- 순수익 목표: 100,000원/일
- 일일 보험료 자동 기록: 7,945원
- 지출 관리: 충전/타이어/오일/세차/기타
- 휴무일 자동 처리
- Render Web Service 호환
"""

import os, re, json, base64, logging, threading
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

load_dotenv()
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "0"))
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

NET_GOAL        = 100000
CARD_FEE_RATE   = 0.033
INSURANCE_DAILY = 7945
AVG_NET_FARE    = round(9504 * (1 - CARD_FEE_RATE))
DOW_KOR         = ["월","화","수","목","금","토","일"]

EXPENSE_KEYWORDS = {
    "충전":   ("⚡ 전기충전",  ["충전","전기"]),
    "타이어": ("🔧 타이어교체",["타이어"]),
    "오일":   ("🔧 오일교환",  ["오일","엔진오일"]),
    "세차":   ("🚿 세차",      ["세차"]),
    "보험":   ("📋 보험료",    ["보험"]),
}

# ── Health Check ─────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis OK")
    def log_message(self, *args): pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ── Supabase ─────────────────────────────────────
def sb_h():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

def sb_insert(t, d):
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=sb_h(), json=d, timeout=10)
        return r.status_code in [200,201]
    except Exception as e:
        logger.error(f"insert: {e}"); return False

def sb_select(t, p=""):
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{p}", headers=sb_h(), timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.error(f"select: {e}"); return []

def sb_upsert(t, d, c):
    try:
        h = sb_h(); h["Prefer"] = "resolution=merge-duplicates"
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}?on_conflict={c}", headers=h, json=d, timeout=10)
        return r.status_code in [200,201]
    except Exception as e:
        logger.error(f"upsert: {e}"); return False

def sb_delete_last(t, td):
    try:
        rows = sb_select(t, f"날짜=eq.{td}&자동여부=eq.false&order=id.desc&limit=1&select=id")
        if not rows: return False
        r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{t}?id=eq.{rows[0]['id']}", headers=sb_h(), timeout=10)
        return r.status_code in [200,204]
    except Exception as e:
        logger.error(f"delete: {e}"); return False

# ── Utils ─────────────────────────────────────────
def get_dow(d): return DOW_KOR[d.weekday()]
def fmt(a):
    try: return f"{int(a):,}원"
    except: return "0원"
def calc_net(rev): return round(rev * (1 - CARD_FEE_RATE))

def today_summary(td):
    data = sb_select("raw_calls", f"날짜=eq.{td}&select=요금,콜유형")
    total = sum(c.get("요금",0) or 0 for c in data)
    return {"건수":len(data),"매출":total,"순매출":calc_net(total),
            "배회":sum(1 for c in data if c.get("콜유형")=="배회")}

def today_expenses(td):
    data = sb_select("expenses", f"날짜=eq.{td}&select=카테고리,금액,메모,자동여부&order=id.asc")
    return {"items":data,"total":sum(c.get("금액",0) or 0 for c in data)}

def insurance_exists(td):
    return len(sb_select("expenses", f"날짜=eq.{td}&카테고리=eq.📋 보험료&select=id")) > 0

def insert_insurance(td):
    if not insurance_exists(td):
        sb_insert("expenses", {"날짜":str(td),"카테고리":"📋 보험료",
                               "금액":INSURANCE_DAILY,"메모":"자동기록 (연290만÷365일)","자동여부":True})

def get_strategy(hour, location=""):
    rows = sb_select("strategy_lookup","select=행동지침,시간대,위치구분,우선순위")
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
        lok=위치=="어디서나" or not location or 위치 in location
        if tok and lok and pri in ["긴급","높음"]: matched.append(s)
    if not matched: return ""
    matched.sort(key=lambda x:{"긴급":0,"높음":1}.get(x.get("우선순위"),2))
    return matched[0].get("행동지침","")

def auth_check(update):
    ALLOWED_IDS = [ALLOWED_CHAT_ID, 8329666973]
    return update.effective_chat.id in ALLOWED_IDS

def build_report(td):
    dow=get_dow(td); s=today_summary(td); exp=today_expenses(td)
    순수익=s["순매출"]-exp["total"]
    달성률=round(순수익/NET_GOAL*100,1)
    잔여=max(0,NET_GOAL-순수익)
    잔여콜=round(잔여/AVG_NET_FARE) if 잔여>0 else 0
    카수=s["매출"]-s["순매출"]
    msg=(f"📊 *{td.strftime('%m/%d')} ({dow}) 마감 리포트*\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"총매출:      {fmt(s['매출'])}\n"
         f"카드수수료:  △{fmt(카수)}\n"
         f"순매출:      {fmt(s['순매출'])}\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"📋 *오늘 지출*")
    if exp["items"]:
        for item in exp["items"]:
            msg+=f"\n  {item['카테고리']}: {fmt(item['금액'])}{'  (자동)' if item.get('자동여부') else ''}"
        msg+=f"\n  지출합계: {fmt(exp['total'])}"
    else:
        msg+="\n  지출 없음"
    msg+=(f"\n━━━━━━━━━━━━━━━━━━━━\n"
          f"✅ *실질순수익: {fmt(순수익)}*\n"
          f"목표({fmt(NET_GOAL)}) 달성률: *{달성률}%*")
    if 순수익>=NET_GOAL: msg+="\n🎉 *오늘 목표 달성!*"
    else: msg+=f"\n잔여순수익: {fmt(잔여)} (약 {잔여콜}콜)"
    return msg

def parse_expense(text):
    am=re.search(r'(\d[\d,]+)',text.replace(" ",""))
    if not am: return None,None
    amount=int(am.group(1).replace(",",""))
    for key,(cat,kws) in EXPENSE_KEYWORDS.items():
        for kw in kws:
            if kw in text: return cat,amount
    if text.startswith("지출"):
        parts=text.split(); memo=parts[1] if len(parts)>2 else "기타"
        return f"📦 {memo}",amount
    return None,None

# ── 보험료 자동 스케줄러 (별도 스레드) ───────────
def insurance_scheduler():
    """매일 00:01에 보험료 자동 기록"""
    import time
    while True:
        now = datetime.now()
        # 다음 00:01까지 대기
        next_run = now.replace(hour=0, minute=1, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait_secs = (next_run - now).total_seconds()
        time.sleep(wait_secs)
        insert_insurance(date.today())
        logger.info(f"보험료 자동기록: {date.today()} {fmt(INSURANCE_DAILY)}")
        time.sleep(60)  # 중복 실행 방지

# ── C모드 ─────────────────────────────────────────
async def process_call_card(image_bytes, update):
    await update.message.reply_text("📥 콜카드 분석 중...")
    b64=base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt='이 콜카드 이미지에서 정보를 추출해서 JSON만 반환해줘.\n{"배차시각":"HH:MM","출발지":"OO구 OO동","도착지":"OO구 OO동","요금":숫자,"카드사":"카드사명","콜유형":"카카오T 또는 배회"}'
    try:
        resp=claude_client.messages.create(model="claude-opus-4-6",max_tokens=300,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
                {"type":"text","text":prompt}]}])
        m=re.search(r'\{.*\}',resp.content[0].text.strip(),re.DOTALL)
        if not m:
            await update.message.reply_text("❌ 콜카드 인식 실패. 다시 올려주세요."); return
        data=json.loads(m.group())
    except Exception as e:
        await update.message.reply_text("❌ 분석 오류."); logger.error(f"OCR: {e}"); return

    now=datetime.now(); call_date=now.date()
    배차시각=data.get("배차시각","")
    if 배차시각:
        try:
            h=int(배차시각.split(":")[0])
            if h<4 and now.hour>=4: call_date=now.date()
            elif h<4: call_date=now.date()-timedelta(days=1)
        except: pass

    요일=get_dow(call_date)
    if not sb_insert("raw_calls",{"날짜":str(call_date),"요일":요일,"배차시각":배차시각,
        "출발지":data.get("출발지"),"도착지":data.get("도착지"),"요금":data.get("요금"),
        "콜유형":data.get("콜유형","카카오T"),"비고":data.get("카드사","")}):
        await update.message.reply_text("❌ DB 저장 실패."); return

    s=today_summary(call_date); exp=today_expenses(call_date)
    순수익=s["순매출"]-exp["total"]
    달성률=round(순수익/NET_GOAL*100,1)
    잔여=max(0,NET_GOAL-순수익)
    잔여콜=round(잔여/AVG_NET_FARE) if 잔여>0 else 0
    try: h2=int(배차시각.split(":")[0]) if 배차시각 else now.hour
    except: h2=now.hour
    strategy=get_strategy(h2,data.get("출발지",""))
    g_tag="\n     🔥 경산 루프! 복귀콜 대기 권장." if "경산" in str(data.get("도착지","")) or "경산" in str(data.get("출발지","")) else ""
    유형=data.get("콜유형","카카오T")

    msg=(f"📥 *저장완료* — {call_date.strftime('%m/%d')} ({요일})\n\n"
         f"{'🚖' if 유형=='카카오T' else '🚶'} {배차시각 or '시각미확인'} | {data.get('출발지','미확인')} → {data.get('도착지','미확인')}\n"
         f"💰 {fmt(data.get('요금',0))} | {유형}{g_tag}\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"📊 *오늘 누계*\n"
         f"  건수: {s['건수']}건 | 매출: {fmt(s['매출'])}\n"
         f"  순수익: {fmt(순수익)}\n"
         f"  목표({fmt(NET_GOAL)}) 달성률: *{달성률}%*\n"
         f"  잔여: {fmt(잔여)} (약 {잔여콜}콜)")
    if 순수익>=NET_GOAL: msg+="\n  🎉 *오늘 목표 달성!*"
    if strategy: msg+=f"\n━━━━━━━━━━━━━━━━━━━━\n🎯 *지금 행동*\n{strategy}"
    await update.message.reply_text(msg,parse_mode="Markdown")

# ── 지출 ──────────────────────────────────────────
async def handle_expense(text, update):
    today=date.today(); cat,amount=parse_expense(text)
    if not cat or not amount:
        await update.message.reply_text("💬 금액을 함께 입력해주세요.\n예: `충전 15000`",parse_mode="Markdown"); return
    if not sb_insert("expenses",{"날짜":str(today),"카테고리":cat,"금액":amount,"메모":text,"자동여부":False}):
        await update.message.reply_text("❌ 지출 저장 실패."); return
    exp=today_expenses(today); s=today_summary(today); 순수익=s["순매출"]-exp["total"]
    msg=(f"💸 *지출 기록완료*\n{cat}: {fmt(amount)}\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"오늘 지출합계: {fmt(exp['total'])}\n"
         f"현재 순수익: {fmt(순수익)}\n"
         f"목표 달성률: {round(순수익/NET_GOAL*100,1)}%")
    await update.message.reply_text(msg,parse_mode="Markdown")

async def handle_expense_check(update):
    today=date.today(); exp=today_expenses(today)
    if not exp["items"]:
        await update.message.reply_text("📋 오늘 지출 내역이 없습니다."); return
    msg=f"📋 *오늘 지출 내역* — {today.strftime('%m/%d')}\n━━━━━━━━━━━━━━━━━━━━"
    for item in exp["items"]:
        msg+=f"\n{item['카테고리']}: {fmt(item['금액'])}{'  (자동)' if item.get('자동여부') else ''}"
    msg+=f"\n━━━━━━━━━━━━━━━━━━━━\n합계: {fmt(exp['total'])}"
    await update.message.reply_text(msg,parse_mode="Markdown")

async def handle_expense_cancel(update):
    today=date.today()
    if sb_delete_last("expenses",today):
        exp=today_expenses(today)
        await update.message.reply_text(f"✅ 마지막 지출 삭제완료.\n현재 지출합계: {fmt(exp['total'])}")
    else:
        await update.message.reply_text("❌ 삭제할 지출이 없습니다.")

# ── 휴무 ──────────────────────────────────────────
async def handle_rest_day(update):
    today=date.today(); dow=get_dow(today)
    s=today_summary(today)
    if s["건수"]>0:
        await update.message.reply_text(f"⚠️ 오늘 콜 {s['건수']}건 있어 휴무 불가."); return
    insert_insurance(today)
    sb_upsert("daily_summary",{"날짜":str(today),"요일":dow,"총건수":0,"총매출":0,
                               "배회건수":0,"정상여부":"휴무","휴무여부":True},"날짜")
    msg=(f"📅 *{today.strftime('%m/%d')} ({dow}) — 휴무*\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"📋 지출\n  보험료: {fmt(INSURANCE_DAILY)} (자동기록)\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"운행 없음. 내일도 화이팅! 💪")
    await update.message.reply_text(msg,parse_mode="Markdown")

# ── A모드 ─────────────────────────────────────────
async def handle_realtime(text, update):
    hm=re.search(r'(\d{1,2})시',text); hour=int(hm.group(1)) if hm else datetime.now().hour
    loc=next((kw for kw in ["수성구","성내","중구","동구","달서구","북구","서구","경산"] if kw in text),"")
    strategy=get_strategy(hour,loc)
    today=date.today(); dow=get_dow(today); s=today_summary(today); exp=today_expenses(today)
    순수익=s["순매출"]-exp["total"]; gm=re.search(r'공차\s*(\d+)분',text)
    prompt=(f"대구 전기차 택시 전략 AI 자비스야.\n"
            f"현재: {hour}시 | 위치: {loc or '미확인'} | {f'공차 {gm.group(1)}분 경과' if gm else ''}\n"
            f"오늘({dow}): {s['건수']}건 순수익 {fmt(순수익)} / 목표 {fmt(NET_GOAL)}\n"
            f"전략DB: {strategy or '일반운행'}\n"
            f"지금 즉시 할 행동 3줄. 대표님이라고 불러줘. 이모지 활용.")
    try:
        resp=claude_client.messages.create(model="claude-sonnet-4-6",max_tokens=250,
            messages=[{"role":"user","content":prompt}])
        advice=resp.content[0].text.strip()
    except: advice=strategy or "현재 전략 데이터를 확인해주세요."
    await update.message.reply_text(f"⚡ *{hour}시 전략*\n━━━━━━━━━━━━━━━━━━━━\n{advice}",parse_mode="Markdown")

# ── B모드 ─────────────────────────────────────────
async def handle_report(update):
    today=date.today(); dow=get_dow(today)
    insert_insurance(today)
    msg=build_report(today)
    s=today_summary(today); exp=today_expenses(today); 순수익=s["순매출"]-exp["total"]
    tomorrow_dow=get_dow(today+timedelta(days=1))
    try:
        prompt=(f"대구 전기차 택시 전략 AI 자비스야.\n"
                f"오늘({dow}): {s['건수']}건 / 순수익 {fmt(순수익)} / 달성률 {round(순수익/NET_GOAL*100,1)}%\n"
                f"내일은 {tomorrow_dow}요일.\n"
                f"✅ 잘한 점:\n📌 개선점:\n🎯 내일 핵심전략:\n각각 한 줄. 대표님이라고 불러줘.")
        resp=claude_client.messages.create(model="claude-sonnet-4-6",max_tokens=200,
            messages=[{"role":"user","content":prompt}])
        msg+=f"\n━━━━━━━━━━━━━━━━━━━━\n{resp.content[0].text.strip()}"
    except: pass
    await update.message.reply_text(msg,parse_mode="Markdown")
    sb_upsert("daily_summary",{"날짜":str(today),"요일":dow,"총건수":s["건수"],
        "총매출":s["매출"],"배회건수":s["배회"],"정상여부":"정상","휴무여부":False},"날짜")

async def handle_today_quick(update):
    today=date.today(); dow=get_dow(today); s=today_summary(today); exp=today_expenses(today)
    순수익=s["순매출"]-exp["total"]; 달성률=round(순수익/NET_GOAL*100,1)
    잔여=max(0,NET_GOAL-순수익); 잔여콜=round(잔여/AVG_NET_FARE) if 잔여>0 else 0
    msg=(f"📊 *오늘 현황* — {today.strftime('%m/%d')} ({dow})\n"
         f"건수: {s['건수']}건 | 매출: {fmt(s['매출'])}\n"
         f"지출: {fmt(exp['total'])} | 순수익: *{fmt(순수익)}*\n"
         f"목표 달성률: *{달성률}%*\n"
         f"잔여: {fmt(잔여)} (약 {잔여콜}콜)")
    await update.message.reply_text(msg,parse_mode="Markdown")

async def handle_weekly(update):
    today=date.today(); ws=today-timedelta(days=today.weekday())
    calls=sb_select("raw_calls",f"날짜=gte.{ws}&날짜=lte.{today}&select=날짜,요금")
    exps=sb_select("expenses",f"날짜=gte.{ws}&날짜=lte.{today}&select=금액")
    if not calls:
        await update.message.reply_text("📊 이번 주 데이터가 없습니다."); return
    tr=sum(c.get("요금",0) or 0 for c in calls); te=sum(e.get("금액",0) or 0 for e in exps)
    np=calc_net(tr)-te; cnt=len(calls); days=len(set(c["날짜"] for c in calls))
    msg=(f"📈 *이번 주 성과*\n━━━━━━━━━━━━━━━━━━━━\n"
         f"기간: {ws.strftime('%m/%d')} ~ {today.strftime('%m/%d')}\n"
         f"운행일: {days}일 | 총건수: {cnt}건\n"
         f"총매출: {fmt(tr)} | 지출: △{fmt(te)}\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"✅ 주간 순수익: *{fmt(np)}*\n"
         f"일평균 순수익: {fmt(np//days if days else 0)}")
    await update.message.reply_text(msg,parse_mode="Markdown")

async def handle_monthly(update):
    today=date.today(); ms=today.replace(day=1)
    calls=sb_select("raw_calls",f"날짜=gte.{ms}&날짜=lte.{today}&select=날짜,요금")
    exps=sb_select("expenses",f"날짜=gte.{ms}&날짜=lte.{today}&select=금액,카테고리")
    tr=sum(c.get("요금",0) or 0 for c in calls); te=sum(e.get("금액",0) or 0 for e in exps)
    np=calc_net(tr)-te; cnt=len(calls); days=len(set(c["날짜"] for c in calls))
    cat_totals={}
    for e in exps:
        cat=e.get("카테고리","기타"); cat_totals[cat]=cat_totals.get(cat,0)+(e.get("금액",0) or 0)
    msg=(f"📅 *{today.strftime('%m')}월 성과*\n━━━━━━━━━━━━━━━━━━━━\n"
         f"운행일: {days}일 | 총건수: {cnt}건\n"
         f"총매출: {fmt(tr)}\n━━━━━━━━━━━━━━━━━━━━\n📋 *지출 내역*")
    for cat,amt in sorted(cat_totals.items(),key=lambda x:-x[1]):
        msg+=f"\n  {cat}: {fmt(amt)}"
    msg+=(f"\n  지출합계: △{fmt(te)}\n━━━━━━━━━━━━━━━━━━━━\n"
          f"✅ *월 순수익: {fmt(np)}*\n"
          f"일평균 순수익: {fmt(np//days if days else 0)}")
    await update.message.reply_text(msg,parse_mode="Markdown")

# ── 핸들러 ────────────────────────────────────────
async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    msg=("🤖 *자비스(JARVIS) 가동*\n━━━━━━━━━━━━━━━━━━━━\n"
         "📥 *데이터 입력*\n"
         "  콜카드 이미지 → 자동저장+브리핑\n"
         "  `충전 15000` → 전기충전 기록\n"
         "  `타이어 80000` → 타이어교체\n"
         "  `오일 35000` → 오일교환\n"
         "  `세차 5000` → 세차\n"
         "  `지출 내용 금액` → 기타 지출\n"
         "  `지출취소` → 마지막 지출 삭제\n"
         "  `휴무` → 휴무 처리\n"
         "━━━━━━━━━━━━━━━━━━━━\n"
         "📊 *조회*\n"
         "  `오늘` → 현황 빠른 조회\n"
         "  `오늘 마감` → 전체 리포트\n"
         "  `지출 확인` → 오늘 지출 목록\n"
         "  `이번 주` → 주간 성과\n"
         "  `이번 달` → 월간 성과\n"
         "━━━━━━━━━━━━━━━━━━━━\n"
         "⚡ *실시간 전략*\n"
         "  `전략` 또는 `실시간` → 지금 전략\n"
         "  `지금 21시 수성구` → 맞춤 전략\n"
         "  `공차 20분째` → 재진입 거점 안내")
    await update.message.reply_text(msg,parse_mode="Markdown")

async def cmd_id(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"채팅 ID: `{update.effective_chat.id}`",parse_mode="Markdown")

async def handle_photo(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    photo=update.message.photo[-1]; file=await context.bot.get_file(photo.file_id)
    await process_call_card(bytes(await file.download_as_bytearray()),update)

async def handle_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    text=update.message.text.strip()

    if text=="휴무": await handle_rest_day(update); return
    if "지출취소" in text: await handle_expense_cancel(update); return
    if text=="지출 확인": await handle_expense_check(update); return

    exp_kws=["충전","전기","타이어","오일","엔진오일","세차","보험","지출"]
    if any(kw in text for kw in exp_kws) and bool(re.search(r'\d{3,}',text)):
        await handle_expense(text,update); return

    if any(t in text for t in ["마감","오늘 정리","결산"]): await handle_report(update); return
    if text in ["오늘","현황"]: await handle_today_quick(update); return
    if any(t in text for t in ["이번 달","이번달","월간"]): await handle_monthly(update); return
    if any(t in text for t in ["이번 주","이번주","주간"]): await handle_weekly(update); return

    if text in ["전략","실시간"]:
        h=datetime.now().hour; s=get_strategy(h)
        await update.message.reply_text(f"⚡ *{h}시 전략*\n{s or '이 시간대는 자유 운행입니다.'}",parse_mode="Markdown"); return

    if re.search(r'\d{1,2}시',text) or any(t in text for t in ["지금","공차","어디","뭐해야"]):
        await handle_realtime(text,update); return

    today=date.today(); dow=get_dow(today); s=today_summary(today); exp=today_expenses(today)
    순수익=s["순매출"]-exp["total"]
    prompt=(f"대구 전기차 택시 전략 AI 자비스야.\n"
            f"오늘 {dow}요일 | {s['건수']}건 | 순수익 {fmt(순수익)} / 목표 {fmt(NET_GOAL)}\n"
            f"대표님 메시지: {text}\n간결하고 실용적으로 답해줘. 대표님이라고 불러줘.")
    try:
        resp=claude_client.messages.create(model="claude-sonnet-4-6",max_tokens=300,
            messages=[{"role":"user","content":prompt}])
        reply=resp.content[0].text.strip()
    except: reply="잠시 후 다시 시도해 주세요."
    await update.message.reply_text(reply)

# ── 메인 ─────────────────────────────────────────
def main():
    logger.info("자비스 봇 v4 시작...")
    threading.Thread(target=run_health_server,daemon=True).start()
    threading.Thread(target=insurance_scheduler,daemon=True).start()
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("id",cmd_id))
    app.add_handler(MessageHandler(filters.PHOTO,handle_photo))
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,handle_text))
    logger.info("봇 폴링 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
