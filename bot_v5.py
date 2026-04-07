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

async def sb_delete_receipt(conditions: dict) -> int:
    """payment_receipts 조건부 삭제. 삭제 건수 반환"""
    query = "&".join(f"{k}={v}" for k, v in conditions.items())
    r = await sb_h("DELETE", f"payment_receipts?{query}")
    # Supabase DELETE는 삭제된 행 반환 (Prefer: return=representation)
    if isinstance(r, list):
        return len(r)
    return 0

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

def fmt(n) -> str:
    """None·문자열 안전 처리"""
    if n is None:
        return "0원"
    try:
        return f"{int(n):,}원"
    except (ValueError, TypeError):
        return "0원"

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
        "이 이미지가 아래 중 어느 종류인지 판단해서 해당 단어 하나만 답해줘.\n\n"
        "【충전】\n"
        "  전기차 충전 앱 이용내역. 아래 특징 중 하나라도 있으면 반드시 '충전':\n"
        "  · '충전완료' 또는 '충전량' 텍스트\n"
        "  · kWh 단위\n"
        "  · '충전소' 항목\n"
        "  · '전기차 충전' 탭\n\n"
        "【결제】\n"
        "  카카오T 수익관리/결제내역 화면. 아래 특징이 있으면 '결제':\n"
        "  · '거래일자' 컬럼 (YYYY-MM-DD HH:MM:SS 형식)\n"
        "  · '카드사' 컬럼 (KB카드, 신한카드 등)\n"
        "  · '1승인 정상' 텍스트\n\n"
        "【콜카드】\n"
        "  카카오T 택시 운행기록. '배차', '승차', '하차', 출발지·도착지 주소 있음.\n\n"
        "【세큐티】\n"
        "  세큐티 등급·점수 리포트.\n\n"
        "【기타】\n"
        "  위 4가지에 해당 없음.\n\n"
        "⚠️ 주의: '결제 금액'이라는 텍스트만으로 '결제'로 판단하지 말 것.\n"
        "   충전 앱에도 '결제 금액' 항목이 있음.\n"
        "   kWh·충전량·충전소가 보이면 무조건 '충전'으로 답할 것.\n\n"
        "반드시 콜카드·충전·결제·세큐티·기타 중 하나만 답해. 다른 말 금지."
    )
    result = await claude_vision(image_bytes, prompt, max_tokens=15)
    for keyword in ["콜카드", "충전", "결제", "세큐티"]:
        if keyword in result:
            return keyword
    return "기타"

async def ocr_call_card(image_bytes: bytes) -> dict | None:
    prompt = (
        "이 콜카드 이미지에서 정보를 추출해서 JSON만 반환해줘.\n"
        '{"배차시각":"HH:MM","하차시각":"HH:MM","출발지":"OO구 OO동","도착지":"OO구 OO동",'
        '"요금":숫자,"카드사":"카드사명","콜유형":"카카오T 또는 배회"}\n'
        "배차시각=승차 시각, 하차시각=하차 완료 시각. 없으면 null.\n"
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
        "화면 상단 또는 각 건의 날짜(예: 2026/03/15 또는 2026-03-15)를 반드시 읽어서 YYYY-MM-DD 형식으로 변환해줘.\n"
        "날짜를 확인할 수 없으면 null로 표기. 절대 오늘 날짜로 추정하지 말 것.\n"
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
    콜카드(raw_calls) ↔ 결제내역(payment_receipts) 교차대조.
    매칭 기준: 콜카드 하차시각 ↔ 결제시각 ±5분 + 날짜 자정넘김 처리
    하차시각 없으면 배차시각+20분으로 추정
    """
    from datetime import date as date_cls

    calls = await sb_select("raw_calls", {"날짜": f"eq.{date_str}"})
    # 결제내역은 당일 + 익일(자정 넘김) 동시 조회
    try:
        y, mo, d = date_str.split("-")
        next_date = date_cls(int(y), int(mo), int(d))
        from datetime import timedelta
        next_date_str = str(next_date + timedelta(days=1))
    except Exception:
        next_date_str = date_str

    receipts_today = await sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})
    receipts_next  = await sb_select("payment_receipts", {"날짜": f"eq.{next_date_str}"})
    receipts = receipts_today + receipts_next

    if not calls and not receipts:
        return f"⚠️ {date_str} 데이터 없음 (콜카드·결제내역 모두 미입력)"

    def to_min_smart(배차시각, 대상시각, 대상날짜):
        """배차시각 기준으로 하차/결제시각 분 계산 (자정 넘김 자동 처리)"""
        try:
            bh, bm = 배차시각.split(":")
            base = int(bh)*60 + int(bm)
            th, tm = 대상시각.split(":")
            target = int(th)*60 + int(tm)
            # 결제날짜가 콜날짜+1이면 +1440
            if 대상날짜 == next_date_str:
                target += 1440
            elif target < base - 60:
                # 날짜 같지만 자정 넘긴 경우 (배차보다 1시간 이상 작음)
                target += 1440
            return target
        except Exception:
            return None

    matched_call_ids = set()
    matched_receipt_ids = set()

    # 매칭: 하차시각(없으면 배차+20분 추정) ↔ 결제시각 ±5분
    for i, call in enumerate(calls):
        배차 = call.get("배차시각") or ""
        하차 = call.get("하차시각")

        # 하차시각 없으면 배차+20분 추정
        if not 하차:
            try:
                bh, bm = 배차.split(":")
                est = int(bh)*60+int(bm)+20
                하차 = f"{est//60%24:02d}:{est%60:02d}"
                call_date_for_est = next_date_str if est >= 1440 else date_str
            except Exception:
                continue
        else:
            call_date_for_est = date_str

        call_min = to_min_smart(배차, 하차, call_date_for_est)
        call_fee = call.get("요금") or 0

        best_j, best_diff = None, 99999
        for j, rcpt in enumerate(receipts):
            if j in matched_receipt_ids:
                continue
            rcpt_date = rcpt.get("날짜", date_str)
            rcpt_min = to_min_smart(배차, rcpt.get("시각") or "", rcpt_date)
            if rcpt_min is None or call_min is None:
                continue
            diff = abs(call_min - rcpt_min)
            rcpt_fee = rcpt.get("요금") or 0
            # 요금 일치 + 시각 ±5분 우선
            if diff <= 5 and (call_fee == rcpt_fee or call_fee == 0 or rcpt_fee == 0):
                if diff < best_diff:
                    best_diff = diff
                    best_j = j
        # 요금 미일치라도 시각 ±10분이면 후보
        if best_j is None:
            for j, rcpt in enumerate(receipts):
                if j in matched_receipt_ids:
                    continue
                rcpt_date = rcpt.get("날짜", date_str)
                rcpt_min = to_min_smart(배차, rcpt.get("시각") or "", rcpt_date)
                if rcpt_min is None or call_min is None:
                    continue
                diff = abs(call_min - rcpt_min)
                if diff <= 10 and diff < best_diff:
                    best_diff = diff
                    best_j = j

        if best_j is not None:
            matched_call_ids.add(i)
            matched_receipt_ids.add(best_j)

    unmatched_calls    = [c for i,c in enumerate(calls)    if i not in matched_call_ids]
    unmatched_receipts = [r for j,r in enumerate(receipts) if j not in matched_receipt_ids]

    lines = [f"📊 교차대조 결과 — {date_str}"]
    lines.append(f"콜카드 {len(calls)}건 / 결제내역 {len(receipts)}건 / 매칭 {len(matched_call_ids)}건")
    lines.append("")

    if unmatched_calls:
        lines.append(f"🟠 콜카드에만 있음 (배회영업 후보):")
        for c in unmatched_calls:
            lines.append(f"  {c.get('배차시각','-')} {c.get('출발지','')}→{c.get('도착지','')} {fmt(c.get('요금') or 0)}")
        lines.append("")

    if unmatched_receipts:
        lines.append(f"🔴 결제내역에만 있음 (누락 콜카드 후보):")
        for r in unmatched_receipts:
            날짜표시 = f"({r.get('날짜','')})" if r.get("날짜") != date_str else ""
            lines.append(f"  {r.get('시각','-')}{날짜표시} {fmt(r.get('요금') or 0)} ({r.get('결제방법','')})")
        lines.append("")

    if not unmatched_calls and not unmatched_receipts:
        lines.append("✅ 완전 매칭 — 누락 없음")

    if unmatched_calls:
        lines.append(f"💡 미매칭 {len(unmatched_calls)}건 → '배회분류 확정 {date_str}' 입력")

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
# 중복 체크 헬퍼
# ──────────────────────────────────────────────
async def check_duplicate_call(날짜: str, 배차시각: str, 요금: int) -> bool:
    """raw_calls 중복 체크: 날짜+배차시각+요금 동일하면 True"""
    rows = await sb_select("raw_calls", {
        "날짜": f"eq.{날짜}",
        "배차시각": f"eq.{배차시각}",
        "요금": f"eq.{요금}",
    })
    return len(rows) > 0

async def check_duplicate_payment(날짜: str, 시각: str, 요금: int) -> bool:
    """payment_receipts 중복 체크: 날짜+시각+요금 동일하면 True"""
    rows = await sb_select("payment_receipts", {
        "날짜": f"eq.{날짜}",
        "시각": f"eq.{시각}",
        "요금": f"eq.{요금}",
    })
    return len(rows) > 0

# ──────────────────────────────────────────────
async def process_call_card(update: Update, image_bytes: bytes):
    data = await ocr_call_card(image_bytes)
    if not data:
        await update.message.reply_text("❌ 콜카드 인식 실패. 다시 올려주세요.")
        return

    today = str(today_kst())
    dow = get_dow()
    배차시각 = data.get("배차시각")
    요금 = data.get("요금", 0)

    # 중복 체크
    is_dup = await check_duplicate_call(today, 배차시각, 요금)
    if is_dup:
        await update.message.reply_text(
            f"⚠️ 중복 감지 — 저장 안 됨\n"
            f"{배차시각} {fmt(요금)} 이미 DB에 존재\n"
            f"동일 콜카드를 두 번 올리신 건 아닌지 확인해주세요."
        )
        return

    payload = {
        "날짜": today,
        "요일": dow,
        "배차시각": 배차시각,
        "하차시각": data.get("하차시각"),
        "출발지": data.get("출발지"),
        "도착지": data.get("도착지"),
        "요금": 요금,
        "콜유형": data.get("콜유형", "카카오T"),
        "비고": data.get("카드사"),
    }
    result = await sb_insert("raw_calls", payload)
    if result:
        await update.message.reply_text(
            f"✅ 콜 저장\n"
            f"{배차시각} {data.get('출발지','?')}→{data.get('도착지','?')}\n"
            f"{fmt(요금)} [{data.get('콜유형','카카오T')}]"
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
    skipped = 0
    duplicated = 0
    date_warn = []
    dup_list = []
    for item in items:
        날짜 = item.get("날짜")
        # 날짜 없으면 저장 거부 — 오늘 날짜로 대체하지 않음
        if not 날짜 or 날짜 == "null":
            skipped += 1
            date_warn.append(item.get("시각", "?"))
            continue
        시각 = item.get("시각")
        요금 = item.get("요금", 0)
        # 중복 체크
        is_dup = await check_duplicate_payment(날짜, 시각, 요금)
        if is_dup:
            duplicated += 1
            dup_list.append(f"{시각} {fmt(요금)}")
            continue
        payload = {
            "날짜": 날짜,
            "시각": 시각,
            "요금": 요금,
            "결제방법": item.get("결제방법", "카드"),
        }
        r = await sb_insert("payment_receipts", payload)
        if r:
            saved += 1

    dates = list(set(item.get("날짜") for item in items if item.get("날짜") and item.get("날짜") != "null"))
    msg = f"💳 결제내역 {saved}건 저장"
    if dates:
        msg += f" ({', '.join(dates)})"
    if duplicated > 0:
        msg += f"\n⚠️ 중복 {duplicated}건 저장 안 됨: {', '.join(dup_list)}"
    if skipped > 0:
        msg += f"\n⚠️ 날짜 인식 실패 {skipped}건 저장 안 됨"
        msg += f"\n  시각: {', '.join(date_warn)} → 날짜 보이는 캡처로 재전송"
    if saved > 0 and dates:
        msg += f"\n교차대조: '대조 {dates[0]}' 입력"
    await update.message.reply_text(msg)

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


async def handle_receipt_delete(update, text: str):
    """
    결제내역 삭제 명령어
    결제삭제 YYYY-MM-DD 운행외  → 02:01~18:59 시간대 삭제
    결제삭제 YYYY-MM-DD 0원     → 요금 0원·null 삭제
    결제삭제 YYYY-MM-DD 전체    → 해당 날짜 전체 삭제
    결제삭제 YYYY-MM-DD HH:MM   → 특정 시각 삭제
    """
    parts = text.strip().split(" ", 2)
    if len(parts) < 3:
        await update.message.reply_text(
            "형식:\n"
            "결제삭제 YYYY-MM-DD 운행외\n"
            "결제삭제 YYYY-MM-DD 0원\n"
            "결제삭제 YYYY-MM-DD 전체\n"
            "결제삭제 YYYY-MM-DD HH:MM"
        )
        return

    date_str = parts[1].strip()
    mode = parts[2].strip()

    try:
        rows = await sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})
        if not rows:
            await update.message.reply_text(f"⚠️ {date_str} 결제내역 없음")
            return

        delete_ids = []

        if mode == "운행외":
            for r in rows:
                t = r.get("시각", "") or ""
                try:
                    h, m = t.split(":")
                    mins = int(h) * 60 + int(m)
                    # 운행시간 외: 02:01~18:59
                    if 121 <= mins <= 1139:
                        delete_ids.append(r["id"])
                except Exception:
                    delete_ids.append(r["id"])

        elif mode == "0원":
            for r in rows:
                fee = r.get("요금")
                if fee is None or int(fee) == 0:
                    delete_ids.append(r["id"])

        elif mode == "전체":
            delete_ids = [r["id"] for r in rows]

        elif ":" in mode:
            for r in rows:
                if r.get("시각", "") == mode:
                    delete_ids.append(r["id"])
        else:
            await update.message.reply_text(f"❓ 알 수 없는 모드: {mode}")
            return

        if not delete_ids:
            await update.message.reply_text(f"✅ 삭제 대상 없음 ({mode})")
            return

        deleted = 0
        for rid in delete_ids:
            await sb_h("DELETE", f"payment_receipts?id=eq.{rid}")
            deleted += 1

        await update.message.reply_text(
            f"🗑️ 삭제 완료\n"
            f"{date_str} | 조건: {mode}\n"
            f"{deleted}건 삭제 (전체 {len(rows)}건 중)"
        )

    except Exception as e:
        logger.error(f"결제삭제 오류: {e}")
        await update.message.reply_text(f"❌ 삭제 오류: {str(e)[:200]}")


async def handle_date_query(update, date_str: str):
    """특정 날짜 조회: 운행 내역 + 요약 + 지출"""
    import re
    from datetime import date as date_cls

    text = date_str.replace("조회","").replace("일","").strip()
    today_d = today_kst()
    parsed = None

    for pattern, mode in [
        (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "full"),
        (r"^(\d{1,2})-(\d{1,2})$",           "md"),
        (r"^(\d{1,2})/(\d{1,2})$",           "md"),
    ]:
        m = re.match(pattern, text)
        if m:
            g = m.groups()
            try:
                if mode == "full":
                    parsed = date_cls(int(g[0]), int(g[1]), int(g[2]))
                else:
                    parsed = date_cls(today_d.year, int(g[0]), int(g[1]))
                break
            except ValueError:
                pass

    if not parsed:
        await update.message.reply_text(
            "❓ 날짜 형식 오류\n"
            "예시: 3-2 조회 / 3/2 조회 / 2026-03-02 조회"
        )
        return

    date_key = str(parsed)
    dow_map = ["월","화","수","목","금","토","일"]
    dow = dow_map[parsed.weekday()]

    calls    = await sb_select("raw_calls", {"날짜": f"eq.{date_key}", "order": "배차시각.asc"})
    expenses = await sb_select("expenses",  {"날짜": f"eq.{date_key}", "order": "id.asc"})

    if not calls and not expenses:
        await update.message.reply_text(f"📭 {date_key} ({dow}) 데이터 없음")
        return

    result_lines = [f"📅 {date_key} ({dow}) 조회\n"]

    if calls:
        총매출 = sum(c.get("요금") or 0 for c in calls)
        result_lines.append(f"[운행] {len(calls)}콜 | {fmt(총매출)}")
        for c in calls:
            배차 = c.get("배차시각") or "-"
            출발 = (c.get("출발지") or "")[:8]
            도착 = (c.get("도착지") or "")[:8]
            요금 = fmt(c.get("요금") or 0)
            유형 = c.get("콜유형") or "카카오T"
            icon = "🚕" if 유형 == "카카오T" else "🚶"
            result_lines.append(f"  {icon}{배차} {출발}→{도착} {요금}")
    else:
        총매출 = 0
        result_lines.append("[운행] 없음")

    result_lines.append("")

    총지출 = sum(e.get("금액") or 0 for e in expenses)
    if expenses:
        result_lines.append(f"[지출] {fmt(총지출)}")
        for e in expenses:
            cat = e.get("카테고리") or ""
            amt = fmt(e.get("금액") or 0)
            auto = " (자동)" if e.get("자동여부") else ""
            result_lines.append(f"  {cat} {amt}{auto}")
    else:
        result_lines.append("[지출] 없음")

    result_lines.append("")
    순수익 = calc_net(총매출, 총지출)
    달성률 = min(int(순수익 / NET_GOAL * 100), 999) if NET_GOAL else 0
    달성바 = "█" * min(달성률//10, 10) + "░" * max(10 - 달성률//10, 0)
    result_lines.append("[요약]")
    result_lines.append(f"  매출 {fmt(총매출)} | 지출 {fmt(총지출)}")
    result_lines.append(f"  순수익 {fmt(순수익)}")
    result_lines.append(f"  목표 [{달성바}] {달성률}%")

    msg = "\n".join(result_lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(생략)"
    await update.message.reply_text(msg)


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

async def handle_download_month(update, ym: str):
    """특정 월 다운로드 (예: 2026-03)"""
    try:
        year, month = ym.split("-")
        year, month = int(year), int(month)
    except Exception:
        await update.message.reply_text("❌ 형식 오류: YYYY-MM (예: 2026-03)")
        return

    from datetime import date, timedelta
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)

    start_str = str(start_date)
    end_str   = str(end_date)

    # AND 조건 (날짜 키 중복 버그 방지)
    calls    = await sb_select("raw_calls",    {"and": f"(날짜.gte.{start_str},날짜.lte.{end_str})"})
    expenses = await sb_select("expenses",     {"and": f"(날짜.gte.{start_str},날짜.lte.{end_str})"})
    charging = await sb_select("charging_log", {"and": f"(충전일.gte.{start_str},충전일.lte.{end_str})"})

    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "운행기록"
    headers1 = ["날짜","요일","배차시각","출발지","도착지","요금","콜유형","비고"]
    for col, h in enumerate(headers1, 1):
        cell = ws1.cell(1, col, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="4A90D9")
    for c in calls:
        ws1.append([c.get("날짜"),c.get("요일"),c.get("배차시각"),
                    c.get("출발지"),c.get("도착지"),c.get("요금"),
                    c.get("콜유형"),c.get("비고")])

    ws2 = wb.create_sheet("지출")
    headers2 = ["날짜","카테고리","금액","메모","자동여부"]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(1, col, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="F5A623")
    for e in expenses:
        ws2.append([e.get("날짜"),e.get("카테고리"),e.get("금액"),
                    e.get("메모"),e.get("자동여부")])

    ws3 = wb.create_sheet("충전기록")
    headers3 = ["충전일","충전량(kWh)","충전금액","충전소"]
    for col, h in enumerate(headers3, 1):
        cell = ws3.cell(1, col, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="7ED321")
    for ch in charging:
        ws3.append([ch.get("충전일"),ch.get("충전량_kwh"),
                    ch.get("충전금액"),ch.get("충전소")])

    filepath = f"/tmp/자비스_월간_{ym}.xlsx"
    wb.save(filepath)

    await update.message.reply_document(
        document=open(filepath, "rb"),
        filename=f"자비스_월간_{ym}.xlsx",
        caption=f"📊 {ym} 데이터 ({len(calls)}건)"
    )

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
    try:
        await image_queue.put((update, context))
    except Exception as e:
        logger.error(f"이미지 큐 오류: {e}")
        await update.message.reply_text("❌ 이미지 처리 오류. 다시 시도해주세요.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    doc = update.message.document
    if doc and doc.file_name and doc.file_name.endswith(".xlsx"):
        await handle_excel_import(update, context)
    else:
        await update.message.reply_text("⚠️ xlsx 파일만 이식 가능합니다.")


async def _process_single_command(update, context, text: str) -> str | None:
    """단일 명령어 처리. 줄바꿈 다중 명령어 시 각 줄 처리용."""

    # 결제삭제
    if text.startswith("결제삭제 "):
        parts = text.strip().split(" ", 2)
        if len(parts) < 3:
            return "❌ 형식: 결제삭제 YYYY-MM-DD 운행외|0원|전체|HH:MM"
        date_str = parts[1].strip()
        mode = parts[2].strip()
        try:
            rows = await sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})
            if not rows:
                return f"⚠️ {date_str} 결제내역 없음"
            delete_ids = []
            if mode == "운행외":
                for r in rows:
                    t = r.get("시각", "") or ""
                    try:
                        h, m = t.split(":")
                        mins = int(h)*60+int(m)
                        if 121 <= mins <= 1019:
                            delete_ids.append(r["id"])
                    except Exception:
                        delete_ids.append(r["id"])
            elif mode == "0원":
                for r in rows:
                    fee = r.get("요금")
                    if fee is None or int(fee) == 0:
                        delete_ids.append(r["id"])
            elif mode == "전체":
                delete_ids = [r["id"] for r in rows]
            elif ":" in mode:
                for r in rows:
                    if r.get("시각","") == mode:
                        delete_ids.append(r["id"])
            else:
                return f"❓ 알 수 없는 모드: {mode}"
            if not delete_ids:
                return f"✅ {date_str} 삭제 대상 없음 ({mode})"
            for rid in delete_ids:
                await sb_h("DELETE", f"payment_receipts?id=eq.{rid}")
            return f"🗑️ {date_str} {mode} {len(delete_ids)}건 삭제"
        except Exception as e:
            return f"❌ 삭제 오류: {str(e)[:100]}"

    # 수동 콜
    parsed_call = parse_manual_call(text)
    if parsed_call:
        today = str(today_kst())
        payload = {
            "날짜": today, "요일": get_dow(),
            "배차시각": now_kst().strftime("%H:%M"),
            "요금": parsed_call["요금"],
            "콜유형": parsed_call["콜유형"],
            "도착지": parsed_call.get("도착지힌트"),
        }
        r = await sb_insert("raw_calls", payload)
        return f"✅ {parsed_call['콜유형']} {fmt(parsed_call['요금'])} 입력" if r else "❌ 저장 실패"

    # 지출
    parsed_exp = parse_expense(text)
    if parsed_exp:
        today = str(today_kst())
        payload = {
            "날짜": today,
            "카테고리": parsed_exp["카테고리"],
            "금액": parsed_exp["금액"],
            "메모": parsed_exp.get("메모",""),
            "자동여부": False,
        }
        r = await sb_insert("expenses", payload)
        return f"✅ {parsed_exp['카테고리']} {fmt(parsed_exp['금액'])} 입력" if r else "❌ 저장 실패"

    return f"❓ '{text[:20]}' 인식 불가"

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (update.message.text or "").strip()
    lower = text.lower()

    # ── 줄바꿈 다중 명령어 처리 ──
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) > 1:
        results = []
        for line in lines:
            # 각 줄을 개별 명령어로 처리
            fake_update = update
            line_text = line
            try:
                result = await _process_single_command(fake_update, context, line_text)
                if result:
                    results.append(result)
            except Exception as e:
                results.append(f"❌ '{line_text[:20]}' 오류: {str(e)[:50]}")
        if results:
            await update.message.reply_text("\n".join(results))
        return

    # 결제내역 삭제 명령어
    # "결제삭제 YYYY-MM-DD 운행외" → 운행시간(19~02시) 외 데이터 삭제
    # "결제삭제 YYYY-MM-DD 0원"    → 요금 0원 데이터 삭제
    # "결제삭제 YYYY-MM-DD 전체"   → 해당 날짜 전체 삭제
    if text.startswith("결제삭제 "):
        await handle_receipt_delete(update, text)
        return

    # 교차대조
    if text.startswith("대조 "):
        date_str = text[3:].strip()
        try:
            result = await cross_check(date_str)
            # Telegram 4096자 제한 처리
            if len(result) > 4000:
                result = result[:4000] + "\n...(생략)"
            await update.message.reply_text(result)
        except Exception as e:
            logger.error(f"교차대조 오류: {e}")
            await update.message.reply_text(f"❌ 교차대조 오류: {str(e)[:200]}")
        return

    if text.startswith("배회분류 확정 "):
        date_str = text[8:].strip()
        try:
            result = await confirm_baehoe_classification(date_str)
            await update.message.reply_text(result)
        except Exception as e:
            logger.error(f"배회분류 오류: {e}")
            await update.message.reply_text(f"❌ 배회분류 오류: {str(e)[:200]}")
        return

    # 특정 날짜 조회
    if "조회" in text:
        await handle_date_query(update, text)
        return

    # 조회
    if text == "오늘":
        await handle_today_quick(update)
        return
    if text in ("이번 주", "이번주", "주간"):
        await handle_weekly(update)
        return
    import re as _re2
    _ym = _re2.match(r"^월간\s+(\d{4}-\d{2})\s*$", text.strip())
    if _ym:
        await handle_download_month(update, _ym.group(1))
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

    # 특정 월 다운로드: "월간 2026-03" or "월간2026-03"
    import re as _re
    _ym_match = _re.match(r"^월간\s*(\d{4}-\d{2})$", text.strip())
    if _ym_match:
        await handle_download_month(update, _ym_match.group(1))
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
    await update.message.reply_text("❓ 명령어를 인식하지 못했습니다. /help 로 확인해주세요.")

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
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .get_updates_read_timeout(10)   # 폴링 빠른 타임아웃 → Conflict 최소화
        .build()
    )

    async def post_init(application):
        global image_queue
        image_queue = asyncio.Queue()
        asyncio.create_task(process_image_queue_worker())
        # 기존 webhook 제거 + 이전 인스턴스 세션 정리 — Conflict 방지
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook 삭제 완료 — 폴링 시작")

    app.post_init = post_init

    # 핸들러 등록
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("자비스 v5 시작")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )

if __name__ == "__main__":
    main()
