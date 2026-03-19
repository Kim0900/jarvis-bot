"""
자비스(JARVIS) 텔레그램 봇 v2
supabase 라이브러리 제거 → httpx 직접 REST API 통신
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, date, timedelta

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
import anthropic

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── 환경변수 ──────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY")
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── 요일별 목표 ───────────────────────────────────
GOAL_MAP = {
    "일": 180000, "금": 150000, "수": 130000,
    "토": 120000, "화": 110000, "월": 100000, "목": 102233
}
DOW_KOR = ["월", "화", "수", "목", "금", "토", "일"]


# ════════════════════════════════════════════════
# Supabase REST API 직접 호출
# ════════════════════════════════════════════════

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


def sb_insert(table: str, data: dict) -> bool:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        r = httpx.post(url, headers=sb_headers(), json=data, timeout=10)
        return r.status_code in [200, 201]
    except Exception as e:
        logger.error(f"DB 저장 오류: {e}")
        return False


def sb_select(table: str, filters: str = "") -> list:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
        r = httpx.get(url, headers=sb_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception as e:
        logger.error(f"DB 조회 오류: {e}")
        return []


def sb_upsert(table: str, data: dict, on_conflict: str) -> bool:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
        headers = sb_headers()
        headers["Prefer"] = "resolution=merge-duplicates"
        r = httpx.post(url, headers=headers, json=data, timeout=10)
        return r.status_code in [200, 201]
    except Exception as e:
        logger.error(f"DB upsert 오류: {e}")
        return False


# ════════════════════════════════════════════════
# 유틸리티
# ════════════════════════════════════════════════

def get_dow(d: date) -> str:
    return DOW_KOR[d.weekday()]


def format_money(amount) -> str:
    try:
        return f"{int(amount):,}원"
    except:
        return "0원"


def get_today_summary(target_date: date) -> dict:
    data = sb_select("raw_calls", f"날짜=eq.{target_date}&select=요금,콜유형")
    total = sum(c.get("요금", 0) or 0 for c in data)
    count = len(data)
    haewhoe = sum(1 for c in data if c.get("콜유형") == "배회")
    return {"건수": count, "매출": total, "배회": haewhoe}


def get_strategy(hour: int, location: str = "") -> str:
    strategies = sb_select("strategy_lookup", "select=트리거,행동지침,시간대,위치구분,우선순위")
    matched = []
    for s in strategies:
        시간대 = s.get("시간대", "")
        위치 = s.get("위치구분", "")
        우선순위 = s.get("우선순위", "보통")

        time_ok = False
        if 시간대 == "전체":
            time_ok = True
        elif "~" in 시간대:
            try:
                parts = 시간대.replace("시", "").split("~")
                start = int(parts[0].split(":")[0])
                end = int(parts[1].split(":")[0])
                if start <= end:
                    time_ok = start <= hour < end
                else:
                    time_ok = hour >= start or hour < end
            except:
                pass

        loc_ok = 위치 == "어디서나" or not location or 위치 in location

        if time_ok and loc_ok and 우선순위 in ["긴급", "높음"]:
            matched.append(s)

    if not matched:
        return ""
    order = {"긴급": 0, "높음": 1, "보통": 2}
    matched.sort(key=lambda x: order.get(x.get("우선순위", "보통"), 2))
    return matched[0].get("행동지침", "")


def auth_check(update: Update) -> bool:
    return update.effective_chat.id == ALLOWED_CHAT_ID


# ════════════════════════════════════════════════
# C모드 — 콜카드 OCR + 저장 + 브리핑
# ════════════════════════════════════════════════

async def process_call_card(image_bytes: bytes, update: Update):
    await update.message.reply_text("📥 콜카드 분석 중...")

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    ocr_prompt = """이 콜카드 이미지에서 정보를 추출해서 JSON만 반환해줘. 설명 없이 JSON만.

{
  "배차시각": "HH:MM",
  "출발지": "OO구 OO동",
  "도착지": "OO구 OO동",
  "요금": 숫자만,
  "카드사": "카드사명",
  "콜유형": "카카오T 또는 배회"
}"""

    try:
        resp = claude_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64
                    }},
                    {"type": "text", "text": ocr_prompt}
                ]
            }]
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            await update.message.reply_text("❌ 콜카드 인식 실패. 다시 올려주세요.")
            return
        data = json.loads(m.group())
    except Exception as e:
        await update.message.reply_text(f"❌ 분석 오류: {str(e)[:80]}")
        return

    now = datetime.now()
    call_date = now.date()
    배차시각 = data.get("배차시각", "")
    if 배차시각:
        try:
            h = int(배차시각.split(":")[0])
            if h < 4 and now.hour >= 4:
                call_date = now.date()
            elif h < 4:
                call_date = now.date() - timedelta(days=1)
        except:
            pass

    요일 = get_dow(call_date)

    saved = sb_insert("raw_calls", {
        "날짜": str(call_date),
        "요일": 요일,
        "배차시각": 배차시각,
        "출발지": data.get("출발지"),
        "도착지": data.get("도착지"),
        "요금": data.get("요금"),
        "콜유형": data.get("콜유형", "카카오T"),
        "비고": data.get("카드사", "")
    })

    if not saved:
        await update.message.reply_text("❌ DB 저장 실패. 다시 시도해 주세요.")
        return

    summary = get_today_summary(call_date)
    goal = GOAL_MAP.get(요일, 130000)
    달성률 = round(summary["매출"] / goal * 100, 1) if goal else 0
    잔여 = max(0, goal - summary["매출"])
    잔여콜 = round(잔여 / 9500) if 잔여 > 0 else 0

    try:
        h = int(배차시각.split(":")[0]) if 배차시각 else now.hour
    except:
        h = now.hour
    strategy = get_strategy(h, data.get("출발지", ""))

    is_gyeongsan = "경산" in str(data.get("도착지", "")) or "경산" in str(data.get("출발지", ""))
    g_tag = "\n     🔥 경산 루프! 복귀콜 대기 권장." if is_gyeongsan else ""

    요금표시 = format_money(data.get("요금", 0))
    유형 = data.get("콜유형", "카카오T")
    이모지 = "🚖" if 유형 == "카카오T" else "🚶"

    msg = f"""📥 *저장완료* — {call_date.strftime('%m/%d')} ({요일})

{이모지} {배차시각 or '시각미확인'} | {data.get('출발지','미확인')} → {data.get('도착지','미확인')}
💰 {요금표시} | {유형}{g_tag}
━━━━━━━━━━━━━━━━━━━━
📊 *오늘 누계*
  건수: {summary['건수']}건 | 매출: {format_money(summary['매출'])}
  목표({format_money(goal)}) 달성률: *{달성률}%*
  잔여: {format_money(잔여)} (약 {잔여콜}콜)"""

    if 달성률 >= 100:
        msg += "\n  🎉 *오늘 목표 달성!*"

    if strategy:
        msg += f"\n━━━━━━━━━━━━━━━━━━━━\n🎯 *지금 행동*\n{strategy}"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# A모드 — 실시간 전략
# ════════════════════════════════════════════════

async def handle_realtime(text: str, update: Update):
    hour_m = re.search(r'(\d{1,2})시', text)
    hour = int(hour_m.group(1)) if hour_m else datetime.now().hour

    location = ""
    for kw in ["수성구", "성내", "중구", "동구", "달서구", "북구", "서구", "경산"]:
        if kw in text:
            location = kw
            break

    strategy = get_strategy(hour, location)

    today = date.today()
    dow = get_dow(today)
    summary = get_today_summary(today)
    goal = GOAL_MAP.get(dow, 130000)

    gong_m = re.search(r'공차\s*(\d+)분', text)
    gong_str = f"공차 {gong_m.group(1)}분 경과" if gong_m else ""

    prompt = f"""대구 전기차 택시 전략 AI 자비스야.
현재: {hour}시 | 위치: {location or '미확인'} | {gong_str}
오늘({dow}): {summary['건수']}건 {format_money(summary['매출'])} / 목표 {format_money(goal)}
전략DB: {strategy or '일반운행'}

지금 즉시 할 행동을 3줄로 알려줘.
1줄: 핵심 행동
2줄: 데이터 근거
3줄: 다음 체크포인트
대표님이라고 불러줘. 이모지 활용."""

    try:
        resp = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}]
        )
        advice = resp.content[0].text.strip()
    except:
        advice = strategy or "현재 전략 데이터를 확인해주세요."

    msg = f"⚡ *{hour}시 전략*\n━━━━━━━━━━━━━━━━━━━━\n{advice}"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# B모드 — 마감 리포트
# ════════════════════════════════════════════════

async def handle_report(update: Update):
    today = date.today()
    dow = get_dow(today)
    summary = get_today_summary(today)
    goal = GOAL_MAP.get(dow, 130000)
    달성률 = round(summary["매출"] / goal * 100, 1) if goal else 0

    # 내일 요일
    tomorrow_dow = get_dow(today + timedelta(days=1))
    tomorrow_goal = GOAL_MAP.get(tomorrow_dow, 130000)

    prompt = f"""대구 전기차 택시 전략 AI 자비스야.

오늘 {today.strftime('%m/%d')} ({dow}) 결과:
건수: {summary['건수']}건 | 매출: {format_money(summary['매출'])} | 달성률: {달성률}%
목표: {format_money(goal)}

마감 리포트 작성:
📊 {today.strftime('%m/%d')} ({dow}) 마감
━━━━━━━━━━━━━━━━━━━━
[건수/매출/달성률 한 줄]
━━━━━━━━━━━━━━━━━━━━
✅ 잘한 점: (한 줄)
📌 개선점: (한 줄)
━━━━━━━━━━━━━━━━━━━━
내일({tomorrow_dow}) 전략: (핵심 두 줄)
내일 목표: {format_money(tomorrow_goal)}

대표님이라고 불러줘."""

    try:
        resp = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}]
        )
        report = resp.content[0].text.strip()
    except:
        report = f"📊 {today.strftime('%m/%d')} ({dow}) 마감\n건수: {summary['건수']}건 | 매출: {format_money(summary['매출'])}\n달성률: {달성률}%"

    await update.message.reply_text(report, parse_mode="Markdown")

    sb_upsert("daily_summary", {
        "날짜": str(today),
        "요일": dow,
        "총건수": summary["건수"],
        "총매출": summary["매출"],
        "배회건수": summary["배회"],
        "정상여부": "정상"
    }, "날짜")


# ════════════════════════════════════════════════
# 주간 리포트
# ════════════════════════════════════════════════

async def handle_weekly(update: Update):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    data = sb_select("raw_calls", f"날짜=gte.{week_start}&날짜=lte.{today}&select=날짜,요금")

    if not data:
        await update.message.reply_text("📊 이번 주 데이터가 없습니다.")
        return

    total = sum(c.get("요금", 0) or 0 for c in data)
    count = len(data)
    days = len(set(c["날짜"] for c in data))

    msg = f"""📈 *이번 주 성과*
━━━━━━━━━━━━━━━━━━━━
기간: {week_start.strftime('%m/%d')} ~ {today.strftime('%m/%d')}
운행일: {days}일 | 총건수: {count}건
총매출: {format_money(total)}
일평균: {format_money(total // days if days else 0)}
건당단가: {format_money(total // count if count else 0)}"""

    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════
# 텔레그램 핸들러
# ════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    msg = """🤖 *자비스(JARVIS) 시작*
━━━━━━━━━━━━━━━━━━━━
📥 콜카드 이미지 → 자동 저장+브리핑
⚡ "지금 21시 수성구" → 전략 조언
📊 "오늘 마감" → 마감 리포트
📈 "이번 주" → 주간 리포트
━━━━━━━━━━━━━━━━━━━━
/today — 오늘 현황
/strategy — 지금 전략"""
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"채팅 ID: `{update.effective_chat.id}`", parse_mode="Markdown")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    today = date.today()
    dow = get_dow(today)
    s = get_today_summary(today)
    goal = GOAL_MAP.get(dow, 130000)
    달성률 = round(s["매출"] / goal * 100, 1) if goal else 0
    msg = f"""📊 *오늘 현황* — {today.strftime('%m/%d')} ({dow})
건수: {s['건수']}건 | 매출: {format_money(s['매출'])}
목표: {format_money(goal)} | 달성률: *{달성률}%*
잔여: {format_money(max(0, goal - s['매출']))}"""
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    h = datetime.now().hour
    s = get_strategy(h)
    msg = f"⚡ *{h}시 전략*\n{s or '이 시간대는 자유 운행입니다.'}"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    await process_call_card(bytes(image_bytes), update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    text = update.message.text.strip()

    if any(t in text for t in ["이번 주", "이번주", "주간"]):
        await handle_weekly(update)
        return

    if any(t in text for t in ["마감", "리포트", "오늘 정리", "결산"]):
        await handle_report(update)
        return

    if re.search(r'\d{1,2}시', text) or any(t in text for t in ["지금", "공차", "어디", "뭐해야"]):
        await handle_realtime(text, update)
        return

    today = date.today()
    dow = get_dow(today)
    s = get_today_summary(today)
    goal = GOAL_MAP.get(dow, 130000)

    prompt = f"""대구 전기차 택시 전략 AI 자비스야.
오늘 {dow}요일 | {s['건수']}건 {format_money(s['매출'])} / 목표 {format_money(goal)}
대표님 메시지: {text}
간결하고 실용적으로 답해줘."""

    try:
        resp = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        reply = resp.content[0].text.strip()
    except:
        reply = "잠시 후 다시 시도해 주세요."

    await update.message.reply_text(reply)


# ════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════

def main():
    logger.info("자비스 봇 시작...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("봇 폴링 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
