# bot_v5.py — 자비스(JARVIS) 봇 v5 완성판
# 설계서 기준 + 결제내역 OCR + 콜카드↔결제내역 교차대조
# 작성일: 2026-03-30

import os
import json
import time
import asyncio
import logging
import threading
import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import anthropic

load_dotenv()

# ──────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
NET_GOAL      = 150_000      # 일 순수익 목표
CARD_FEE_RATE = 0.033        # 카드수수료 3.3%
INSURANCE_DAILY = 7_945      # 일 보험료
DOW_KOR = ["월", "화", "수", "목", "금", "토", "일"]

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_IDS_RAW    = [
    os.getenv("ALLOWED_CHAT_ID", ""),
    os.getenv("ALLOWED_CHAT_ID2", ""),
]
ALLOWED_IDS = {x for x in ALLOWED_IDS_RAW if x}

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY", "")
PORT              = int(os.getenv("PORT", "10000"))

OCR_MODEL      = "claude-haiku-4-5-20251001"
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

# ──────────────────────────────────────────────
# Health Check 서버
# ──────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Jarvis v5 OK")
    def log_message(self, *args):
        pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"Health server on port {PORT}")
    server.serve_forever()

# ──────────────────────────────────────────────
# Supabase 헬퍼
# ──────────────────────────────────────────────
async def sb_h(method: str, path: str, **kwargs) -> dict | list | None:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.request(method, url, headers=HEADERS_SB, **kwargs)
        if r.status_code in (200, 201):
            return r.json()
        logger.error(f"Supabase {method} {path} → {r.status_code}: {r.text}")
        return None

async def sb_insert(table: str, data: dict) -> dict | None:
    return await sb_h("POST", table, json=data)

async def sb_select(table: str, params: dict = None) -> list:
    result = await sb_h("GET", table, params=params or {})
    return result if isinstance(result, list) else []

async def sb_upsert(table: str, data: dict, on_conflict: str) -> dict | None:
    return await sb_h(
        "POST", table,
        json=data,
        headers={**HEADERS_SB, "Prefer": f"resolution=merge-duplicates,return=representation"},
        params={"on_conflict": on_conflict}
    )

async def sb_delete_last(table: str, filter_params: dict) -> bool:
    rows = await sb_select(table, {**filter_params, "order": "id.desc", "limit": "1"})
    if not rows:
        return False
    row_id = rows[0]["id"]
    r = await sb_h("DELETE", f"{table}?id=eq.{row_id}")
    return True

# ──────────────────────────────────────────────
# 유틸 함수
# ──────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)

def today_kst():
    return now_kst().date()

def get_dow(d=None) -> str:
    d = d or today_kst()
    return DOW_KOR[d.weekday()]

def fmt(n: int) -> str:
    return f"{n:,}원"

def calc_net(매출: int, 지출: int) -> int:
    return int(매출 * (1 - CARD_FEE_RATE)) - 지출

async def today_summary() -> dict:
    today = str(today_kst())
    calls = await sb_select("raw_calls", {"날짜": f"eq.{today}"})
    expenses = await sb_select("expenses", {"날짜": f"eq.{today}"})
    건수 = len(calls)
    매출 = sum(c.get("요금", 0) or 0 for c in calls)
    지출 = sum(e.get("금액", 0) or 0 for e in expenses)
    순수익 = calc_net(매출, 지출)
    달성률 = int(순수익 / NET_GOAL * 100) if NET_GOAL else 0
    return {
        "건수": 건수, "매출": 매출, "지출": 지출,
        "순수익": 순수익, "달성률": 달성률,
    }

async def today_expenses() -> list:
    today = str(today_kst())
    return await sb_select("expenses", {"날짜": f"eq.{today}", "order": "id.asc"})

async def insert_insurance(date):
    date_str = str(date)
    existing = await sb_select(
        "expenses",
        {"날짜": f"eq.{date_str}", "카테고리": "eq.보험료", "자동여부": "eq.true"}
    )
    if existing:
        return
    await sb_insert("expenses", {
        "날짜": date_str,
        "카테고리": "보험료",
        "금액": INSURANCE_DAILY,
        "메모": "자동 보험료",
        "자동여부": True,
    })
    logger.info(f"보험료 자동 기록: {date_str}")

# ──────────────────────────────────────────────
# 이미지 큐 (동시 업로드 과부하 방지)
# ──────────────────────────────────────────────
image_queue: asyncio.Queue = None  # main()에서 초기화

# ──────────────────────────────────────────────
# Claude API — 이미지 분류 + OCR
# ──────────────────────────────────────────────
async def claude_vision(image_bytes: bytes, prompt: str, max_tokens: int = 500) -> str:
    b64 = base64.standard_b64encode(image_bytes).decode()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=OCR_MODEL,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return msg.content[0].text.strip()

async def classify_image(image_bytes: bytes) -> str:
    prompt = (
        "이 이미지가 무엇인지 한 단어로만 답해줘.\n"
        "- 콜카드 (택시 콜카드/운행기록)\n"
        "- 충전 (전기차 충전 내역/영수증)\n"
        "- 결제 (카카오T 결제내역/수익관리 화면)\n"
        "- 세큐티 (세큐티 등급/리포트)\n"
        "- 기타\n"
        "한 단어만 답해. 설명 금지."
    )
    result = await claude_vision(image_bytes, prompt, max_tokens=10)
    for keyword in ["콜카드", "충전", "결제", "세큐티"]:
        if keyword in result:
            return keyword
    return "기타"

async def ocr_call_card(image_bytes: bytes) -> dict | None:
    prompt = (
        "이 콜카드 이미지에서 정보를 추출해서 JSON만 반환해줘.\n"
        '{"배차시각":"HH:MM","출발지":"OO구 OO동","도착지":"OO구 OO동",'
        '"요금":숫자,"카드사":"카드사명","콜유형":"카카오T 또는 배회"}\n'
        "JSON만 반환. 설명 금지."
    )
    raw = await claude_vision(image_bytes, prompt, max_tokens=200)
    try:
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```")
        return json.loads(raw)
    except Exception:
        logger.error(f"콜카드 JSON 파싱 실패: {raw}")
        return None

async def ocr_charge_receipt(image_bytes: bytes) -> list:
    prompt = (
        "이 충전 내역 이미지에서 모든 충전 건을 추출해서 JSON 배열만 반환해줘.\n"
        '[{"충전일자":"YYYY-MM-DD","충전소명":"이름","충전량":숫자(kWh),"결제금액":숫자(원)}, ...]\n'
        "여러 건이 보이면 전부 추출. JSON 배열만 반환. 설명 금지."
    )
    raw = await claude_vision(image_bytes, prompt, max_tokens=500)
    try:
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```")
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        logger.error(f"충전내역 JSON 파싱 실패: {raw}")
        return []

async def ocr_payment_history(image_bytes: bytes) -> list:
    """
    카카오T 결제내역(수익관리 화면) OCR.
    반환: [{"날짜":"YYYY-MM-DD","시각":"HH:MM","요금":숫자,"결제방법":"카드/현금"}, ...]
    """
    prompt = (
        "이 카카오T 수익관리/결제내역 화면에서 모든 운행 건을 추출해서 JSON 배열만 반환해줘.\n"
        '[{"날짜":"YYYY-MM-DD","시각":"HH:MM","요금":숫자,"결제방법":"카드 또는 현금"}, ...]\n'
        "날짜가 없으면 오늘 날짜로 추정하지 말고 null로 표기.\n"
        "JSON 배열만 반환. 설명 금지."
    )
    raw = await claude_vision(image_bytes, prompt, max_tokens=800)
    try:
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```")
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        logger.error(f"결제내역 JSON 파싱 실패: {raw}")
        return []

# ──────────────────────────────────────────────
# 교차대조 로직 (콜카드 ↔ 결제내역)
# ──────────────────────────────────────────────
async def cross_check(date_str: str) -> str:
    """
    지정 날짜의 raw_calls(콜카드 OCR)와 payment_receipts(결제내역 OCR) 교차대조.
    - 시각 ±3분 매칭
    - 미매칭 콜카드 → 배회영업 후보
    - 미매칭 결제내역 → 누락 콜카드 후보
    """
    calls = await sb_select("raw_calls", {"날짜": f"eq.{date_str}", "order": "배차시각.asc"})
    receipts = await sb_select("payment_receipts", {"날짜": f"eq.{date_str}", "order": "시각.asc"})

    if not calls and not receipts:
        return f"⚠️ {date_str} 데이터 없음 (콜카드·결제내역 모두 미입력)"

    matched_call_ids = set()
    matched_receipt_ids = set()

    def to_minutes(t_str: str) -> int | None:
        try:
            h, m = t_str.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None

    # 매칭: 시각 ±3분 + 요금 일치 우선, 요금 없으면 시각만
    for i, call in enumerate(calls):
        call_min = to_minutes(call.get("배차시각", "") or "")
        call_fee = call.get("요금") or 0
        for j, rcpt in enumerate(receipts):
            if j in matched_receipt_ids:
                continue
            rcpt_min = to_minutes(rcpt.get("시각", "") or "")
            rcpt_fee = rcpt.get("요금") or 0
            if call_min is None or rcpt_min is None:
                continue
            time_diff = abs(call_min - rcpt_min)
            if time_diff <= 3:
                # 시각 매칭 → 요금까지 확인
                if call_fee == rcpt_fee or call_fee == 0 or rcpt_fee == 0:
                    matched_call_ids.add(i)
                    matched_receipt_ids.add(j)
                    break
                elif abs(call_fee - rcpt_fee) <= 500:
                    # 요금 500원 오차 허용 (할증 등)
                    matched_call_ids.add(i)
                    matched_receipt_ids.add(j)
                    break

    unmatched_calls = [c for i, c in enumerate(calls) if i not in matched_call_ids]
    unmatched_receipts = [r for j, r in enumerate(receipts) if j not in matched_receipt_ids]

    lines = [f"📊 교차대조 결과 — {date_str}"]
    lines.append(f"콜카드 {len(calls)}건 / 결제내역 {len(receipts)}건 / 매칭 {len(matched_call_ids)}건")
    lines.append("")

    if unmatched_calls:
        lines.append("🟠 콜카드에만 있음 (배회영업 후보):")
        for c in unmatched_calls:
            lines.append(f"  {c.get('배차시각','-')} {c.get('출발지','')}→{c.get('도착지','')} {fmt(c.get('요금',0))}")
        lines.append("")

    if unmatched_receipts:
        lines.append("🔴 결제내역에만 있음 (누락 콜카드 후보):")
        for r in unmatched_receipts:
            lines.append(f"  {r.get('시각','-')} {fmt(r.get('요금',0))} ({r.get('결제방법','')})")
        lines.append("")

    if not unmatched_calls and not unmatched_receipts:
        lines.append("✅ 완전 매칭 — 누락 없음")

    # 배회 자동 분류 제안
    if unmatched_calls:
        lines.append(f"💡 콜카드 미매칭 {len(unmatched_calls)}건 → 배회영업으로 분류할까요?")
        lines.append("  '배회분류 확정 YYYY-MM-DD' 로 확정 가능")

    return "\n".join(lines)

async def confirm_baehoe_classification(date_str: str) -> str:
    """미매칭 콜카드를 배회영업으로 자동 분류 확정"""
    calls = await sb_select("raw_calls", {"날짜": f"eq.{date_str}"})
    receipts = await sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})

    def to_minutes(t_str):
        try:
            h, m = t_str.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None

    matched_call_ids = set()
    for i, call in enumerate(calls):
        call_min = to_minutes(call.get("배차시각", "") or "")
        for j, rcpt in enumerate(receipts):
            rcpt_min = to_minutes(rcpt.get("시각", "") or "")
            if call_min and rcpt_min and abs(call_min - rcpt_min) <= 3:
                matched_call_ids.add(i)
                break

    unmatched = [c for i, c in enumerate(calls) if i not in matched_call_ids]
    if not unmatched:
        return "✅ 미매칭 콜카드 없음"

    count = 0
    for call in unmatched:
        # 콜유형을 배회로 업데이트
        await sb_h(
            "PATCH",
            f"raw_calls?id=eq.{call['id']}",
            json={"콜유형": "배회"}
        )
        count += 1

    return f"✅ {count}건 배회영업으로 분류 완료"

# ──────────────────────────────────────────────
# 이미지 처리 함수
# ──────────────────────────────────────────────
async def process_call_card(update: Update, image_bytes: bytes):
    data = await ocr_call_card(image_bytes)
    if not data:
        await update.message.reply_text("❌ 콜카드 인식 실패. 다시 올려주세요.")
        return

    today = str(today_kst())
    dow = get_dow()
    payload = {
        "날짜": today,
        "요일": dow,
        "배차시각": data.get("배차시각"),
        "출발지": data.get("출발지"),
        "도착지": data.get("도착지"),
        "요금": data.get("요금"),
        "콜유형": data.get("콜유형", "카카오T"),
        "비고": data.get("카드사"),
    }
    result = await sb_insert("raw_calls", payload)
    if result:
        fee = data.get("요금", 0)
        await update.message.reply_text(
            f"✅ 콜 저장\n"
            f"{data.get('배차시각','?')} {data.get('출발지','?')}→{data.get('도착지','?')}\n"
            f"{fmt(fee)} [{data.get('콜유형','카카오T')}]"
        )
    else:
        await update.message.reply_text("❌ DB 저장 실패")

async def process_charge_receipt(update: Update, image_bytes: bytes):
    items = await ocr_charge_receipt(image_bytes)
    if not items:
        await update.message.reply_text("❌ 충전내역 인식 실패. 다시 올려주세요.")
        return

    saved = 0
    for item in items:
        payload = {
            "충전일": item.get("충전일자") or str(today_kst()),
            "충전량_kwh": item.get("충전량"),
            "충전금액": item.get("결제금액"),
            "충전소": item.get("충전소명"),
        }
        r = await sb_insert("charging_log", payload)
        if r:
            saved += 1

    await update.message.reply_text(
        f"⚡ 충전내역 {saved}/{len(items)}건 저장 완료"
    )

async def process_payment_history(update: Update, image_bytes: bytes):
    """결제내역 OCR → payment_receipts 저장"""
    items = await ocr_payment_history(image_bytes)
    if not items:
        await update.message.reply_text("❌ 결제내역 인식 실패. 다시 올려주세요.")
        return

    saved = 0
    today = str(today_kst())
    for item in items:
        payload = {
            "날짜": item.get("날짜") or today,
            "시각": item.get("시각"),
            "요금": item.get("요금"),
            "결제방법": item.get("결제방법", "카드"),
        }
        r = await sb_insert("payment_receipts", payload)
        if r:
            saved += 1

    await update.message.reply_text(
        f"💳 결제내역 {saved}/{len(items)}건 저장\n"
        f"교차대조: '대조 {today}' 입력"
    )

async def process_single_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    image_bytes = bytes(image_bytes)

    image_type = await classify_image(image_bytes)
    logger.info(f"이미지 분류: {image_type}")

    if image_type == "콜카드":
        await process_call_card(update, image_bytes)
    elif image_type == "충전":
        await process_charge_receipt(update, image_bytes)
    elif image_type == "결제":
        await process_payment_history(update, image_bytes)
    elif image_type == "세큐티":
        await update.message.reply_text("📊 세큐티 이미지 저장완료")
    else:
        await update.message.reply_text(
            "❓ 인식할 수 없는 이미지입니다.\n콜카드·충전내역·결제내역을 올려주세요."
        )

async def process_image_queue_worker():
    while True:
        update, context = await image_queue.get()
        try:
            await process_single_image(update, context)
        except Exception as e:
            logger.error(f"이미지 처리 오류: {e}")
            try:
                await update.message.reply_text("❌ 처리 오류. 다시 올려주세요.")
            except Exception:
                pass
        finally:
            image_queue.task_done()
        await asyncio.sleep(1)

# ──────────────────────────────────────────────
# 수동 입력 파서
# ──────────────────────────────────────────────
def parse_manual_call(text: str) -> dict | None:
    """
    '콜 7800' / '배회 5600' / '콜 7800 경산'
    → {"콜유형": ..., "요금": ..., "도착지힌트": ...}
    """
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    keyword = parts[0]
    if keyword not in ("콜", "배회"):
        return None
    try:
        fee = int(parts[1].replace(",", "").replace("원", ""))
    except ValueError:
        return None
    hint = parts[2] if len(parts) >= 3 else None
    return {
        "콜유형": "카카오T" if keyword == "콜" else "배회",
        "요금": fee,
        "도착지힌트": hint,
    }

EXPENSE_KEYWORDS = {
    "충전": ("⚡ 전기충전", "charges"),
    "타이어": ("🔧 타이어교체", None),
    "오일": ("🔧 오일교환", None),
    "세차": ("🚿 세차", None),
}

def parse_expense(text: str) -> dict | None:
    """
    '충전 4595' / '타이어 80000' / '지출 기타 3000'
    → {"카테고리": ..., "금액": ..., "메모": ...}
    """
    parts = text.strip().split()
    if not parts:
        return None

    keyword = parts[0]
    if keyword in EXPENSE_KEYWORDS and len(parts) >= 2:
        label, _ = EXPENSE_KEYWORDS[keyword]
        try:
            fee = int(parts[1].replace(",", "").replace("원", ""))
        except ValueError:
            return None
        return {"카테고리": label, "금액": fee, "메모": keyword}

    if keyword == "지출" and len(parts) >= 3:
        cat = parts[1]
        try:
            fee = int(parts[2].replace(",", "").replace("원", ""))
        except ValueError:
            return None
        return {"카테고리": f"📦 {cat}", "금액": fee, "메모": cat}

    return None

# ──────────────────────────────────────────────
# 핸들러 — 수동 입력
# ──────────────────────────────────────────────
async def handle_manual_call(update: Update, parsed: dict):
    today = str(today_kst())
    dow = get_dow()
    payload = {
        "날짜": today,
        "요일": dow,
        "배차시각": now_kst().strftime("%H:%M"),
        "요금": parsed["요금"],
        "콜유형": parsed["콜유형"],
        "도착지": parsed.get("도착지힌트"),
    }
    r = await sb_insert("raw_calls", payload)
    if r:
        await update.message.reply_text(
            f"✅ {parsed['콜유형']} {fmt(parsed['요금'])} 입력"
        )
    else:
        await update.message.reply_text("❌ 저장 실패")

async def handle_expense(update: Update, parsed: dict):
    today = str(today_kst())
    payload = {
        "날짜": today,
        "카테고리": parsed["카테고리"],
        "금액": parsed["금액"],
        "메모": parsed.get("메모", ""),
        "자동여부": False,
    }
    r = await sb_insert("expenses", payload)
    if r:
        await update.message.reply_text(
            f"✅ 지출 {parsed['카테고리']} {fmt(parsed['금액'])} 입력"
        )
    else:
        await update.message.reply_text("❌ 저장 실패")

async def handle_expense_cancel(update: Update):
    today = str(today_kst())
    ok = await sb_delete_last(
        "expenses",
        {"날짜": f"eq.{today}", "자동여부": "eq.false"}
    )
    if ok:
        await update.message.reply_text("✅ 마지막 수동 지출 삭제")
    else:
        await update.message.reply_text("⚠️ 삭제할 수동 지출 없음")

async def handle_rest_day(update: Update):
    today = str(today_kst())
    dow = get_dow()
    await sb_upsert("daily_summary", {
        "날짜": today, "요일": dow,
        "휴무여부": True, "정상여부": "휴무",
    }, on_conflict="날짜")
    await insert_insurance(today_kst())
    await update.message.reply_text(f"✅ {today} 휴무 처리 + 보험료 자동 기록")

# ──────────────────────────────────────────────
# 핸들러 — 조회
# ──────────────────────────────────────────────
async def handle_today_quick(update: Update):
    s = await today_summary()
    달성바 = "█" * (s["달성률"] // 10) + "░" * (10 - s["달성률"] // 10)
    await update.message.reply_text(
        f"📍 오늘 현황 ({now_kst().strftime('%m/%d %H:%M')})\n"
        f"콜 {s['건수']}건 | 매출 {fmt(s['매출'])}\n"
        f"지출 {fmt(s['지출'])} | 순수익 {fmt(s['순수익'])}\n"
        f"목표 [{달성바}] {s['달성률']}%"
    )

async def handle_weekly(update: Update):
    today = today_kst()
    start = today - timedelta(days=6)
    start_str = str(start)
    end_str = str(today)

    calls = await sb_select(
        "raw_calls",
        {"날짜": f"gte.{start_str}", "날짜": f"lte.{end_str}"}
    )
    expenses = await sb_select(
        "expenses",
        {"날짜": f"gte.{start_str}", "날짜": f"lte.{end_str}"}
    )
    총건수 = len(calls)
    총매출 = sum(c.get("요금", 0) or 0 for c in calls)
    총지출 = sum(e.get("금액", 0) or 0 for e in expenses)
    순수익 = calc_net(총매출, 총지출)
    일평균매출 = 총매출 // 7 if 총매출 else 0

    await update.message.reply_text(
        f"📅 주간 요약 ({start_str} ~ {end_str})\n"
        f"총 {총건수}건 | 매출 {fmt(총매출)}\n"
        f"지출 {fmt(총지출)} | 순수익 {fmt(순수익)}\n"
        f"일평균 매출 {fmt(일평균매출)}"
    )

async def handle_monthly(update: Update):
    today = today_kst()
    start_str = today.replace(day=1).isoformat()
    end_str = str(today)

    calls = await sb_select("raw_calls", {"날짜": f"gte.{start_str}"})
    expenses = await sb_select("expenses", {"날짜": f"gte.{start_str}"})
    총건수 = len(calls)
    총매출 = sum(c.get("요금", 0) or 0 for c in calls)
    총지출 = sum(e.get("금액", 0) or 0 for e in expenses)
    순수익 = calc_net(총매출, 총지출)

    # 카테고리별 지출
    cat_map = {}
    for e in expenses:
        cat = e.get("카테고리", "기타")
        cat_map[cat] = cat_map.get(cat, 0) + (e.get("금액", 0) or 0)
    cat_lines = "\n".join(f"  {k}: {fmt(v)}" for k, v in sorted(cat_map.items()))

    운행일 = len(set(c.get("날짜") for c in calls))
    일평균 = 총매출 // 운행일 if 운행일 else 0

    await update.message.reply_text(
        f"📆 월간 요약 ({start_str} ~)\n"
        f"운행일 {운행일}일 | 총 {총건수}건\n"
        f"총매출 {fmt(총매출)} | 일평균 {fmt(일평균)}\n"
        f"지출 {fmt(총지출)} | 순수익 {fmt(순수익)}\n"
        f"\n지출 카테고리:\n{cat_lines}"
    )

async def handle_expense_check(update: Update):
    expenses = await today_expenses()
    if not expenses:
        await update.message.reply_text("오늘 지출 없음")
        return
    lines = [f"💸 오늘 지출 ({str(today_kst())})"]
    total = 0
    for e in expenses:
        lines.append(f"  {e.get('카테고리','')} {fmt(e.get('금액',0))}")
        total += e.get("금액", 0) or 0
    lines.append(f"합계: {fmt(total)}")
    await update.message.reply_text("\n".join(lines))

async def handle_db_check(update: Update):
    calls = await sb_select("raw_calls", {"order": "id.desc", "limit": "1"})
    total_calls = await sb_select("raw_calls", {})
    total_exp = await sb_select("expenses", {})
    charging = await sb_select("charging_log", {"order": "id.desc", "limit": "1"})

    last_call = calls[0] if calls else {}
    last_charge = charging[0] if charging else {}

    await update.message.reply_text(
        f"🗄️ DB 현황\n"
        f"raw_calls: {len(total_calls)}건\n"
        f"expenses: {len(total_exp)}건\n"
        f"최근 콜: {last_call.get('날짜','-')} {last_call.get('배차시각','-')} {fmt(last_call.get('요금',0))}\n"
        f"최근 충전: {last_charge.get('충전일','-')} {last_charge.get('충전량_kwh','-')}kWh"
    )

# ──────────────────────────────────────────────
# 핸들러 — 전략
# ──────────────────────────────────────────────
async def get_strategy(update: Update):
    hour = now_kst().hour
    # 시간대 매칭
    if 19 <= hour < 21:
        time_key = "19~21"
    elif hour == 21:
        time_key = "21~22"
    elif 22 <= hour < 24:
        time_key = "22~00"
    elif 0 <= hour < 2:
        time_key = "00~02"
    elif 2 <= hour < 3:
        time_key = "02~03"
    else:
        time_key = "전체"

    rows = await sb_select(
        "strategy_lookup",
        {"시간대": f"in.(전체,{time_key})", "order": "우선순위.asc"}
    )
    if not rows:
        await update.message.reply_text("⚠️ 전략 테이블 없음. 마기 업데이트 필요")
        return

    lines = [f"⚡ 현재 전략 ({now_kst().strftime('%H:%M')})"]
    for r in rows:
        priority_icon = {"긴급": "🔴", "높음": "🟠", "보통": "🟡"}.get(r.get("우선순위", ""), "⚪")
        lines.append(f"{priority_icon} [{r.get('시간대','')}] {r.get('행동지침','')}")

    await update.message.reply_text("\n".join(lines))

async def handle_magi_update(update: Update, content: str):
    """'마기 업데이트 [시간대] [내용]' → strategy_lookup INSERT"""
    parts = content.strip().split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("형식: 마기 업데이트 [시간대] [내용]")
        return
    시간대 = parts[0]
    지침 = parts[1]
    await sb_insert("strategy_lookup", {
        "시간대": 시간대,
        "행동지침": 지침,
        "우선순위": "높음",
    })
    await update.message.reply_text(f"✅ 전략 테이블 갱신완료\n[{시간대}] {지침}")

# ──────────────────────────────────────────────
# 핸들러 — 엑셀 다운로드
# ──────────────────────────────────────────────
async def handle_download(update: Update, scope: str):
    today = today_kst()
    if scope == "주간":
        start = today - timedelta(days=6)
        start_str = str(start)
        label = f"{start_str}_{today}"
    elif scope == "월간":
        start_str = today.replace(day=1).isoformat()
        label = f"{today.year}{today.month:02d}"
    else:
        start_str = "2000-01-01"
        label = "전체"

    calls = await sb_select("raw_calls", {"날짜": f"gte.{start_str}"})
    expenses = await sb_select("expenses", {"날짜": f"gte.{start_str}"})
    charging = await sb_select("charging_log", {"충전일": f"gte.{start_str}"})

    wb = openpyxl.Workbook()

    # 시트1 raw_calls
    ws1 = wb.active
    ws1.title = "운행기록"
    headers1 = ["날짜", "요일", "배차시각", "출발지", "도착지", "요금", "콜유형", "비고"]
    for col, h in enumerate(headers1, 1):
        cell = ws1.cell(1, col, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="4A90D9")
    for row, c in enumerate(calls, 2):
        ws1.append([
            c.get("날짜"), c.get("요일"), c.get("배차시각"),
            c.get("출발지"), c.get("도착지"), c.get("요금"),
            c.get("콜유형"), c.get("비고"),
        ])

    # 시트2 expenses
    ws2 = wb.create_sheet("지출")
    headers2 = ["날짜", "카테고리", "금액", "메모", "자동여부"]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(1, col, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="F5A623")
    for row, e in enumerate(expenses, 2):
        ws2.append([
            e.get("날짜"), e.get("카테고리"), e.get("금액"),
            e.get("메모"), e.get("자동여부"),
        ])

    # 시트3 charging_log
    ws3 = wb.create_sheet("충전기록")
    headers3 = ["충전일", "충전량(kWh)", "충전금액", "충전소"]
    for col, h in enumerate(headers3, 1):
        cell = ws3.cell(1, col, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="7ED321")
    for row, ch in enumerate(charging, 2):
        ws3.append([
            ch.get("충전일"), ch.get("충전량_kwh"),
            ch.get("충전금액"), ch.get("충전소"),
        ])

    filepath = f"/tmp/자비스_{scope}_{label}.xlsx"
    wb.save(filepath)

    await update.message.reply_document(
        document=open(filepath, "rb"),
        filename=f"자비스_{scope}_{label}.xlsx",
        caption=f"📊 {scope} 데이터 ({len(calls)}건)"
    )

# ──────────────────────────────────────────────
# 핸들러 — 데이터 이식 (v6 엑셀 → raw_calls)
# ──────────────────────────────────────────────
async def handle_excel_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v6 인수인계 엑셀 업로드 시 raw_calls 자동 이식"""
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()

    import io
    wb = openpyxl.load_workbook(io.BytesIO(bytes(file_bytes)), read_only=True)
    target_sheets = [s for s in wb.sheetnames if "운행" in s or "데이터" in s]

    if not target_sheets:
        await update.message.reply_text("⚠️ 운행 데이터 시트를 찾지 못했습니다.")
        return

    await update.message.reply_text(f"📥 이식 시작: {target_sheets}")
    total_saved = 0
    total_skip = 0

    for sheet_name in target_sheets:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        header = [str(h).strip() if h else "" for h in rows[0]]
        col = {h: i for i, h in enumerate(header)}

        for row in rows[1:]:
            if not any(row):
                continue
            try:
                날짜_raw = row[col.get("날짜", 0)]
                if not 날짜_raw:
                    continue
                날짜 = str(날짜_raw)[:10]
                요금_raw = row[col.get("요금", 5)]
                요금 = int(str(요금_raw).replace(",", "").replace("원", "")) if 요금_raw else 0
                배차시각 = row[col.get("배차시각", 2)]
                배차시각 = str(배차시각) if 배차시각 else None

                # 중복 체크
                existing = await sb_select(
                    "raw_calls",
                    {"날짜": f"eq.{날짜}", "요금": f"eq.{요금}", "배차시각": f"eq.{배차시각}"}
                )
                if existing:
                    total_skip += 1
                    continue

                payload = {
                    "날짜": 날짜,
                    "요일": row[col.get("요일", 1)] or "",
                    "배차시각": 배차시각,
                    "출발지": row[col.get("출발지", 3)],
                    "도착지": row[col.get("도착지", 4)],
                    "요금": 요금,
                    "콜유형": row[col.get("콜유형", 6)] or "카카오T",
                }
                r = await sb_insert("raw_calls", payload)
                if r:
                    total_saved += 1
            except Exception as e:
                logger.error(f"이식 행 오류: {e}")
                continue

    await update.message.reply_text(
        f"✅ 이식 완료\n저장: {total_saved}건 | 중복스킵: {total_skip}건"
    )

# ──────────────────────────────────────────────
# 보험 스케줄러
# ──────────────────────────────────────────────
def insurance_scheduler():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        now = datetime.now(KST)
        next_run = now.replace(hour=0, minute=1, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_sec = (next_run - now).total_seconds()
        logger.info(f"보험 스케줄러 대기: {sleep_sec:.0f}초")
        time.sleep(sleep_sec)
        today = datetime.now(KST).date()  # 반드시 KST
        loop.run_until_complete(insert_insurance(today))
        time.sleep(60)

# ──────────────────────────────────────────────
# 텔레그램 핸들러
# ──────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    chat_id = str(update.effective_chat.id)
    return chat_id in ALLOWED_IDS

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "🤖 자비스 v5 시작\n\n"
        "이미지: 콜카드·충전내역·결제내역·세큐티\n"
        "텍스트: 콜 7800 / 배회 5600 / 오늘 / 이번 주 / 전략\n"
        "다운로드: 주간·월간·전체 다운로드\n"
        "교차대조: 대조 YYYY-MM-DD"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "📋 명령어\n\n"
        "[입력]\n콜 금액 / 배회 금액 / 충전 금액\n"
        "타이어·오일·세차 금액 / 지출 항목 금액\n"
        "지출취소 / 휴무\n\n"
        "[조회]\n오늘 / 이번 주 / 이번 달 / 지출 확인 / DB 확인\n\n"
        "[전략]\n전략 / 마기 업데이트 [시간대] [내용]\n\n"
        "[다운로드]\n주간 다운로드 / 월간 다운로드 / 전체 다운로드\n\n"
        "[교차대조]\n대조 YYYY-MM-DD / 배회분류 확정 YYYY-MM-DD"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await image_queue.put((update, context))

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    doc = update.message.document
    if doc and doc.file_name and doc.file_name.endswith(".xlsx"):
        await handle_excel_import(update, context)
    else:
        await update.message.reply_text("⚠️ xlsx 파일만 이식 가능합니다.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (update.message.text or "").strip()
    lower = text.lower()

    # 교차대조
    if text.startswith("대조 "):
        date_str = text[3:].strip()
        result = await cross_check(date_str)
        await update.message.reply_text(result)
        return

    if text.startswith("배회분류 확정 "):
        date_str = text[8:].strip()
        result = await confirm_baehoe_classification(date_str)
        await update.message.reply_text(result)
        return

    # 조회
    if text == "오늘":
        await handle_today_quick(update)
        return
    if text in ("이번 주", "이번주", "주간"):
        await handle_weekly(update)
        return
    if text in ("이번 달", "이번달", "월간"):
        await handle_monthly(update)
        return
    if text == "지출 확인":
        await handle_expense_check(update)
        return
    if text == "DB 확인":
        await handle_db_check(update)
        return

    # 전략
    if text in ("전략", "실시간"):
        await get_strategy(update)
        return
    if text.startswith("마기 업데이트 "):
        content = text[8:].strip()
        await handle_magi_update(update, content)
        return

    # 다운로드
    if "다운로드" in text:
        if "주간" in text:
            await handle_download(update, "주간")
        elif "월간" in text:
            await handle_download(update, "월간")
        elif "전체" in text:
            await handle_download(update, "전체")
        else:
            await update.message.reply_text("주간·월간·전체 다운로드 중 선택해주세요.")
        return

    # 휴무
    if text == "휴무":
        await handle_rest_day(update)
        return

    # 지출취소
    if text == "지출취소":
        await handle_expense_cancel(update)
        return

    # 수동 콜 입력
    parsed_call = parse_manual_call(text)
    if parsed_call:
        await handle_manual_call(update, parsed_call)
        return

    # 지출 입력
    parsed_exp = parse_expense(text)
    if parsed_exp:
        await handle_expense(update, parsed_exp)
        return

    # 미인식
    await update.message.reply_text("❓ 명령어를 인식하지 못했습니다. /명령어 로 확인해주세요.")

# ──────────────────────────────────────────────
# main()
# ──────────────────────────────────────────────
def main():
    global image_queue

    # Health server
    threading.Thread(target=run_health_server, daemon=True).start()

    # Insurance scheduler
    threading.Thread(target=insurance_scheduler, daemon=True).start()

    # Telegram application
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # 이미지 큐 초기화 및 워커 등록
    loop = asyncio.new_event_loop()

    async def post_init(application):
        global image_queue
        image_queue = asyncio.Queue()
        asyncio.create_task(process_image_queue_worker())

    app.post_init = post_init

    # 핸들러 등록
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("자비스 v5 시작")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
