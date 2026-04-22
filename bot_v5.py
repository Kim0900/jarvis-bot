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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    """Claude API 비동기 호출 (asyncio.to_thread로 블로킹 방지)"""
    b64 = base64.standard_b64encode(image_bytes).decode()

    def _sync_call():
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=30.0)
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

    # 동기 API를 별도 스레드에서 실행 → asyncio 이벤트 루프 블로킹 방지
    return await asyncio.to_thread(_sync_call)

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
        '"요금":숫자,"카드사":"카드사명","콜유형":"카카오T 또는 배회","결제방식":"자동 또는 직접"}\n'
        "⚠️ 결제방식 판별:\n"
        "  - 요금 금액이 숫자로 명확히 표시 → 결제방식=\'자동\', 요금=해당 숫자\n"
        "  - 요금 없음·0원·미표시 → 결제방식=\'직접\', 요금=0\n"
        "⚠️ 도착지 주의:\n"
        "  - 차량번호 패턴(예: 대구 32바 5763, XX가·나·바 NNNN)은 도착지가 아님 → null\n"
        "  - 도착지는 반드시 \'OO구 OO동\' 형식 주소만 허용\n"
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
    매칭: 콜카드 하차시각 ↔ 결제시각 ±20분 (자정넘김 처리)
    미매칭 결제내역 자동분류:
      - 콜카드 운행 공백 시간대 → 배회영업 후보
      - 콜카드 운행 중 시간대   → 누락 콜카드 후보
    """
    from datetime import date as date_cls, timedelta

    calls = await sb_select("raw_calls", {"날짜": f"eq.{date_str}"})
    try:
        y, mo, d = date_str.split("-")
        next_date_str = str(date_cls(int(y),int(mo),int(d)) + timedelta(days=1))
    except Exception:
        next_date_str = date_str

    receipts_today = await sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})
    receipts_next  = await sb_select("payment_receipts", {"날짜": f"eq.{next_date_str}"})
    receipts = receipts_today + receipts_next

    if not calls and not receipts:
        return f"⚠️ {date_str} 데이터 없음"

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
        try:
            h, m = time_str.split(":")
            mins = int(h)*60+int(m)
            if date_str_local == next_date_str:
                mins += 1440
            elif mins < 300:
                mins += 1440
            return mins
        except: return None

    # STEP 1: 하차시각 ↔ 결제시각 ±20분 매칭
    matched_call_ids    = set()
    matched_receipt_ids = set()
    fee_mismatches      = []  # 금액 불일치 목록
    direct_updated      = []  # 직접결제 요금 자동업데이트 목록

    for i, call in enumerate(calls):
        배차 = call.get("배차시각") or ""
        하차 = call.get("하차시각")
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
            r_min = to_min_smart(배차, rcpt.get("시각","") or "", rcpt_date)
            if r_min and c_min:
                diff = abs(c_min - r_min)
                if diff <= 20 and diff < best_diff:
                    best_diff = diff
                    best_j = j
        if best_j is not None:
            matched_call_ids.add(i)
            matched_receipt_ids.add(best_j)
            call_fee = call.get("요금") or 0
            rcpt_fee = receipts[best_j].get("요금") or 0

            # 직접결제(요금=0) 콜카드 → 결제내역 요금으로 자동 업데이트
            if call_fee == 0 and rcpt_fee > 0:
                call_id = call.get("id")
                if call_id:
                    await sb_h("PATCH", f"raw_calls?id=eq.{call_id}",
                               json={"요금": rcpt_fee, "비고": "직접결제(요금확인완료)"})
                    direct_updated.append({
                        "배차시각": 배차,
                        "요금": rcpt_fee,
                    })

            # 금액 불일치 (둘 다 0이 아니고 차이 ≥500원)
            elif call_fee > 0 and rcpt_fee > 0:
                fee_diff = abs(call_fee - rcpt_fee)
                if fee_diff >= FEE_DIFF_THRESHOLD:
                    fee_mismatches.append({
                        "call_id": call.get("id"),
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
        배차 = call.get("배차시각") or ""
        하차 = call.get("하차시각") or ""
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
        r_date = r.get("날짜", date_str)
        r_min  = to_min_abs(r.get("시각","") or "", r_date)
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
            lines_out.append(f"  ✅ {d['배차시각']} → {fmt(d['요금'])} 업데이트")
        lines_out.append("")

    if unmatched_calls:
        lines_out.append(f"🟠 콜카드에만 있음 {len(unmatched_calls)}건:")
        for c in unmatched_calls:
            lines_out.append(f"  {c.get('배차시각','-')} {c.get('출발지','')}→{c.get('도착지','')} {fmt(c.get('요금') or 0)}")
        lines_out.append("")

    if baehoe_rcpt:
        lines_out.append(f"🚶 배회영업 후보 (공백시간) {len(baehoe_rcpt)}건:")
        for r in baehoe_rcpt:
            날짜표시 = f"({r.get('날짜','')})" if r.get("날짜") != date_str else ""
            lines_out.append(f"  {r.get('시각','-')}{날짜표시} {fmt(r.get('요금') or 0)}")
        lines_out.append("")

    if missing_rcpt:
        lines_out.append(f"🔴 누락 콜카드 후보 (운행중 시간) {len(missing_rcpt)}건:")
        for r in missing_rcpt:
            날짜표시 = f"({r.get('날짜','')})" if r.get("날짜") != date_str else ""
            lines_out.append(f"  {r.get('시각','-')}{날짜표시} {fmt(r.get('요금') or 0)}")
        lines_out.append("")

    # 금액 불일치 표시
    if fee_mismatches:
        lines_out.append(f"💰 금액 불일치 {len(fee_mismatches)}건 (차이 ≥500원):")
        for fm in fee_mismatches:
            lines_out.append(
                f"  {fm['배차시각']} 콜카드:{fmt(fm['call_fee'])} vs "
                f"결제:{fmt(fm['rcpt_fee'])} (차이 {fm['diff']:,}원)"
            )
        lines_out.append("  → '대조 금액확인 YYYY-MM-DD' 로 버튼 선택")
        lines_out.append("")

    if not unmatched_calls and not unmatched_receipts:
        if fee_mismatches:
            lines_out.append("⚠️ 매칭 완료 — 금액 불일치 확인 필요")
        else:
            lines_out.append("✅ 완전 매칭 — 누락 없음")

    if unmatched_calls:
        lines_out.append(f"💡 '배회분류 확정 {date_str}' → 콜카드 미매칭 배회 처리")
    if baehoe_rcpt:
        lines_out.append(f"💡 '대조 확정 {date_str}' → 배회후보 {len(baehoe_rcpt)}건 raw_calls 자동 추가")

    return "\n".join(lines_out)


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
    요금 = data.get("요금") or 0
    결제방식 = data.get("결제방식", "자동")

    # 중복 체크 → 자동 삭제 후 재저장
    deleted = await delete_duplicate_call(today, 배차시각, 요금)
    if deleted:
        logger.info(f"중복 콜카드 자동 삭제 후 재저장: {today} {배차시각} {요금}")

    # 직접결제: 요금 0원 → pending 상태로 저장
    is_direct = (결제방식 == "직접") or (요금 == 0)
    비고 = "직접결제(요금미확인)" if is_direct else data.get("카드사")

    payload = {
        "날짜": today,
        "요일": dow,
        "배차시각": 배차시각,
        "하차시각": data.get("하차시각"),
        "출발지": data.get("출발지"),
        "도착지": data.get("도착지"),
        "요금": 요금,
        "콜유형": data.get("콜유형", "카카오T"),
        "비고": 비고,
    }
    result = await sb_insert("raw_calls", payload)
    if result:
        if is_direct:
            await update.message.reply_text(
                f"💳 직접결제 콜카드 저장 (요금 미확인)\n"
                f"{배차시각} {data.get('출발지','?')}→{data.get('도착지','?') or '?'}\n"
                f"⚠️ 결제내역 업로드 후 '대조 {today}' 입력해서 요금 확인하세요."
            )
        else:
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
        # 중복 체크 → 자동 삭제 후 재저장
        deleted = await delete_duplicate_payment(날짜, 시각, 요금)
        if deleted:
            logger.info(f"중복 결제내역 자동 삭제: {날짜} {시각} {요금}")
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

async def handle_rest_day(update: Update, text: str = "휴무"):
    """휴무 처리. '4-7 휴무' 형식으로 날짜 지정 가능."""
    import re
    from datetime import date as date_cls

    target = today_kst()
    date_pat = r"(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})[-/](\d{1,2})"
    m = re.search(date_pat, text)
    if m:
        g = m.groups()
        try:
            if g[0]:
                target = date_cls(int(g[0]), int(g[1]), int(g[2]))
            else:
                target = date_cls(today_kst().year, int(g[3]), int(g[4]))
        except ValueError:
            await update.message.reply_text("❌ 잘못된 날짜입니다.")
            return

    dow_map = ["월","화","수","목","금","토","일"]
    date_str = str(target)
    dow = dow_map[target.weekday()]

    await sb_upsert("daily_summary", {
        "날짜": date_str, "요일": dow,
        "휴무여부": True, "정상여부": "휴무",
    }, on_conflict="날짜")
    await insert_insurance(target)
    await update.message.reply_text(
        f"✅ {date_str} ({dow}) 휴무 처리\n보험료 {INSURANCE_DAILY:,}원 자동 기록"
    )

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




async def confirm_cross_check(date_str: str) -> str:
    """
    '대조 확정 YYYY-MM-DD' 명령어 처리.
    교차대조 미매칭 결제내역(현금) → raw_calls 배회영업으로 자동 추가.
    """
    from datetime import date as date_cls, timedelta

    try:
        y, mo, d = date_str.split("-")
        next_date_str = str(date_cls(int(y),int(mo),int(d)) + timedelta(days=1))
    except Exception:
        return "❌ 날짜 형식 오류 (YYYY-MM-DD)"

    # 콜카드 + 결제내역 조회 (익일 포함)
    calls    = await sb_select("raw_calls", {"날짜": f"eq.{date_str}"})
    receipts = (await sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})) +                (await sb_select("payment_receipts", {"날짜": f"eq.{next_date_str}"}))

    def to_min_smart(배차, 대상시각, 대상날짜):
        try:
            bh, bm = 배차.split(":")
            base = int(bh)*60+int(bm)
            th, tm = 대상시각.split(":")
            target = int(th)*60+int(tm)
            if 대상날짜 == next_date_str:
                target += 1440
            elif target < base - 60:
                target += 1440
            return target
        except: return None

    # 매칭
    matched_r = set()
    for call in calls:
        배차 = call.get("배차시각") or ""
        하차 = call.get("하차시각")
        if not 하차:
            try:
                bh, bm = 배차.split(":")
                est = int(bh)*60+int(bm)+20
                하차 = f"{est//60%24:02d}:{est%60:02d}"
            except: continue
        c_min = to_min_smart(배차, 하차, date_str)
        best_j, best_diff = None, 99999
        for j, r in enumerate(receipts):
            if j in matched_r: continue
            r_min = to_min_smart(배차, r.get("시각","") or "", r.get("날짜", date_str))
            if r_min and c_min:
                diff = abs(c_min - r_min)
                if diff <= 20 and diff < best_diff:
                    best_diff = diff
                    best_j = j
        if best_j is not None:
            matched_r.add(best_j)

    unmatched = [r for j,r in enumerate(receipts) if j not in matched_r]

    # 현금 결제 → 배회영업으로 추가
    baehoe = [r for r in unmatched if (r.get("결제방법") or "") in ("현금","")]

    if not baehoe:
        return f"✅ {date_str} 배회후보 없음 (현금 미매칭 결제내역 없음)"

    DOW_MAP = ["월","화","수","목","금","토","일"]
    added = 0
    for r in baehoe:
        r_date = r.get("날짜") or date_str
        from datetime import date as dc
        try:
            rd = dc.fromisoformat(r_date)
            dow = DOW_MAP[rd.weekday()]
        except: dow = ""

        payload = {
            "날짜":     r_date,
            "요일":     dow,
            "배차시각": r.get("시각"),
            "하차시각": r.get("시각"),  # 결제시각 = 하차시각으로 설정
            "요금":     r.get("요금"),
            "콜유형":   "배회",
            "비고":     "결제내역 교차대조 자동추가",
        }
        result = await sb_insert("raw_calls", payload)
        if result:
            added += 1

    # 요약
    total_calls = len(calls) + added
    kakao = len([c for c in calls if c.get("콜유형","") == "카카오T"])
    lines = [
        f"✅ 대조 확정 완료 — {date_str}",
        f"배회영업 {added}건 raw_calls 추가",
        f"",
        f"📊 확정 후 현황:",
        f"  총 {total_calls}건 (카카오T {kakao}건 + 배회 {added}건)",
        f"  결제내역 {len(receipts)}건 → {'✅ 완전매칭' if total_calls == len(receipts) else f'⚠️ 차이 {abs(total_calls - len(receipts))}건'}",
    ]
    return "\n".join(lines)


async def handle_fee_confirm_request(update, date_str: str):
    """
    '대조 금액확인 YYYY-MM-DD' 명령어.
    해당 날짜 매칭 건 중 금액 불일치(≥500원) 건에 대해
    InlineKeyboard 버튼으로 확인 요청.
    """
    from datetime import date as date_cls, timedelta

    calls    = await sb_select("raw_calls", {"날짜": f"eq.{date_str}"})
    try:
        y,mo,d = date_str.split("-")
        next_d = str(date_cls(int(y),int(mo),int(d)) + timedelta(days=1))
    except: next_d = date_str

    receipts = (await sb_select("payment_receipts", {"날짜": f"eq.{date_str}"})) +                (await sb_select("payment_receipts", {"날짜": f"eq.{next_d}"}))

    def to_min(배차, 대상, 대상날짜):
        try:
            bh,bm = 배차.split(":"); base = int(bh)*60+int(bm)
            th,tm = 대상.split(":"); target = int(th)*60+int(tm)
            if 대상날짜 == next_d: target += 1440
            elif target < base-60: target += 1440
            return target
        except: return None

    matched_r = set()
    mismatches = []

    for call in calls:
        배차 = call.get("배차시각") or ""
        하차 = call.get("하차시각") or ""
        if not 하차:
            try:
                bh,bm = 배차.split(":"); est = int(bh)*60+int(bm)+20
                하차 = f"{est//60%24:02d}:{est%60:02d}"
            except: continue
        c_min = to_min(배차, 하차, date_str)
        call_fee = call.get("요금") or 0
        best_j, best_diff = None, 99999
        for j, r in enumerate(receipts):
            if j in matched_r: continue
            r_min = to_min(배차, r.get("시각","") or "", r.get("날짜", date_str))
            if r_min and c_min:
                diff = abs(c_min - r_min)
                if diff <= 20 and diff < best_diff:
                    best_diff = diff; best_j = j
        if best_j is not None:
            matched_r.add(best_j)
            rcpt_fee = receipts[best_j].get("요금") or 0
            fee_diff = abs(call_fee - rcpt_fee)
            if fee_diff >= FEE_DIFF_THRESHOLD:
                mismatches.append({
                    "call_id": call.get("id"),
                    "배차시각": 배차,
                    "call_fee": call_fee,
                    "rcpt_fee": rcpt_fee,
                    "diff": fee_diff,
                })

    if not mismatches:
        await update.message.reply_text(f"✅ {date_str} 금액 불일치 없음")
        return

    for fm in mismatches:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"콜카드 {fm['call_fee']:,}원",
                callback_data=f"fee:{fm['call_id']}:{fm['call_fee']}"
            ),
            InlineKeyboardButton(
                f"결제내역 {fm['rcpt_fee']:,}원",
                callback_data=f"fee:{fm['call_id']}:{fm['rcpt_fee']}"
            ),
        ]])
        await update.message.reply_text(
            f"⚠️ 금액 불일치 확인\n"
            f"배차: {fm['배차시각']}\n"
            f"콜카드: {fm['call_fee']:,}원 | 결제: {fm['rcpt_fee']:,}원\n"
            f"차이: {fm['diff']:,}원",
            reply_markup=keyboard
        )

async def handle_date_stat(update, text: str):
    """
    날짜+통계 키워드 조합 처리
    예: '3-17 총건수', '3-17 매출', '3-17 순수익', '3-17 지출'
    """
    import re
    from datetime import date as date_cls

    today_d = today_kst()
    dow_map = ["월","화","수","목","금","토","일"]

    # 날짜 추출
    parsed = None
    for pat, mode in [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", "full"),
        (r"(\d{1,2})-(\d{1,2})",           "md"),
        (r"(\d{1,2})/(\d{1,2})",           "md"),
    ]:
        m = re.search(pat, text)
        if m:
            g = m.groups()
            try:
                parsed = date_cls(int(g[0]),int(g[1]),int(g[2])) if mode=="full"                          else date_cls(today_d.year, int(g[0]), int(g[1]))
                break
            except ValueError:
                pass

    if not parsed:
        await update.message.reply_text("❓ 날짜 인식 실패\n예: 3-17 총건수")
        return

    date_key = str(parsed)
    dow = dow_map[parsed.weekday()]
    header = f"📅 {date_key} ({dow})"

    # 통계 키워드 판단
    if any(kw in text for kw in ["총건수", "건수"]):
        calls = await sb_select("raw_calls", {"날짜": f"eq.{date_key}"})
        카카오 = sum(1 for c in calls if (c.get("콜유형") or "") == "카카오T")
        배회   = sum(1 for c in calls if (c.get("콜유형") or "") == "배회")
        총매출 = sum(c.get("요금") or 0 for c in calls)
        건당   = 총매출 // len(calls) if calls else 0
        await update.message.reply_text(
            f"{header} 총건수\n"
            f"총 {len(calls)}콜\n"
            f"  🚕 카카오T {카카오}건\n"
            f"  🚶 배회 {배회}건\n"
            f"건당 평균 {fmt(건당)}"
        )

    elif "매출" in text:
        calls = await sb_select("raw_calls", {"날짜": f"eq.{date_key}"})
        총매출 = sum(c.get("요금") or 0 for c in calls)
        건수   = len(calls)
        건당   = 총매출 // 건수 if 건수 else 0
        await update.message.reply_text(
            f"{header} 매출\n"
            f"총매출 {fmt(총매출)}\n"
            f"콜수 {건수}건 | 건당 {fmt(건당)}"
        )

    elif "순수익" in text:
        calls    = await sb_select("raw_calls",  {"날짜": f"eq.{date_key}"})
        expenses = await sb_select("expenses",   {"날짜": f"eq.{date_key}"})
        총매출 = sum(c.get("요금") or 0 for c in calls)
        총지출 = sum(e.get("금액") or 0 for e in expenses)
        순수익 = calc_net(총매출, 총지출)
        달성률 = min(int(순수익 / NET_GOAL * 100), 999) if NET_GOAL else 0
        달성바 = "█" * min(달성률//10,10) + "░" * max(10-달성률//10,0)
        await update.message.reply_text(
            f"{header} 순수익\n"
            f"매출 {fmt(총매출)} | 지출 {fmt(총지출)}\n"
            f"순수익 {fmt(순수익)}\n"
            f"목표 [{달성바}] {달성률}%"
        )

    elif "지출" in text:
        expenses = await sb_select("expenses", {"날짜": f"eq.{date_key}", "order": "id.asc"})
        총지출 = sum(e.get("금액") or 0 for e in expenses)
        if not expenses:
            await update.message.reply_text(f"{header}\n지출 없음")
            return
        lines_out = [f"{header} 지출 {fmt(총지출)}"]
        for e in expenses:
            auto = " (자동)" if e.get("자동여부") else ""
            lines_out.append(f"  {e.get('카테고리','')} {fmt(e.get('금액') or 0)}{auto}")
        await update.message.reply_text("\n".join(lines_out))

    else:
        # 전체 조회로 위임
        await handle_date_query(update, text)


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




def parse_manual_full(text: str) -> dict | None:
    """
    수동 콜 전체 입력 파싱.
    형식:
      2026 03 01 23 05 수성못>대명1동 8500 카카오
      26 03 01 23 05 수성못>대명1동 8500 배회
      03012305 수성못>대명1동 8500 카카오
      0301 2305 수성못>대명1동 8500 배회
      2026-03-01 23:05 수성못>대명1동 8500 카카오
    """
    import re as _re
    from datetime import date as _date

    orig = text.strip()
    today = _date.today()

    # 콜유형
    콜유형 = "배회" if "배회" in orig else "카카오T"
    clean = _re.sub(r'배회|카카오T?', '', orig).strip()

    # 경로 (출발>도착)
    route_m = _re.search(r'([가-힣\w]+)\s*[>→]\s*([가-힣\w]+)', clean)
    출발지 = 도착지 = None
    if route_m:
        출발지 = route_m.group(1).strip()
        도착지 = route_m.group(2).strip()
        clean = (clean[:route_m.start()] + ' ' + clean[route_m.end():]).strip()

    # 날짜+시각 파싱 (패턴 순서대로 시도)
    날짜 = None
    배차시각 = None

    # A: YYYY-MM-DD HH:MM 또는 YY-MM-DD HH:MM
    m = _re.search(r'(\d{2,4})[.\-](\d{1,2})[.\-](\d{1,2})\s+(\d{1,2}):(\d{2})', clean)
    if m:
        g = m.groups(); y = int(g[0]); y = y+2000 if y<100 else y
        try:
            날짜 = _date(y,int(g[1]),int(g[2])); 배차시각 = f"{int(g[3]):02d}:{g[4]}"
            clean = clean[:m.start()] + ' ' + clean[m.end():]
        except ValueError: pass

    # B: YYYY MM DD HH MM
    if not 날짜:
        m = _re.search(r'(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{2})\b', clean)
        if m:
            g = m.groups()
            try:
                날짜 = _date(int(g[0]),int(g[1]),int(g[2])); 배차시각 = f"{int(g[3]):02d}:{g[4]}"
                clean = clean[:m.start()] + ' ' + clean[m.end():]
            except ValueError: pass

    # C: YY MM DD HH MM
    if not 날짜:
        m = _re.search(r'\b(\d{2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{2})\b', clean)
        if m:
            g = m.groups()
            try:
                날짜 = _date(int(g[0])+2000,int(g[1]),int(g[2])); 배차시각 = f"{int(g[3]):02d}:{g[4]}"
                clean = clean[:m.start()] + ' ' + clean[m.end():]
            except ValueError: pass

    # D: MMDD HHMM
    if not 날짜:
        m = _re.search(r'\b(\d{4})\s+(\d{4})\b', clean)
        if m:
            a,b = m.group(1), m.group(2)
            try:
                날짜 = _date(today.year,int(a[:2]),int(a[2:])); 배차시각 = f"{int(b[:2]):02d}:{b[2:]}"
                clean = clean[:m.start()] + ' ' + clean[m.end():]
            except ValueError: pass

    # E: MMDDHHMM (8자리)
    if not 날짜:
        m = _re.search(r'\b(\d{8})\b', clean)
        if m:
            n = m.group(1)
            try:
                날짜 = _date(today.year,int(n[0:2]),int(n[2:4])); 배차시각 = f"{int(n[4:6]):02d}:{n[6:]}"
                clean = clean[:m.start()] + ' ' + clean[m.end():]
            except ValueError: pass

    if not 날짜 or not 배차시각:
        return None

    # 요금: 남은 clean의 4~6자리 숫자
    fee_m = _re.search(r'(?<!\d)(\d{4,6})(?!\d)', clean)
    if not fee_m:
        return None
    요금 = int(fee_m.group(1))

    요일 = ["월","화","수","목","금","토","일"][날짜.weekday()]
    return {"날짜":str(날짜),"요일":요일,"배차시각":배차시각,
            "출발지":출발지,"도착지":도착지,"요금":요금,"콜유형":콜유형}


async def handle_manual_full_call(update, text: str):
    """수동 전체 입력 콜 저장"""
    data = parse_manual_full(text)
    if not data:
        await update.message.reply_text(
            "❌ 형식 오류\n\n"
            "예시:\n"
            "2026 03 01 23 05 수성못>대명1동 8500 카카오\n"
            "26 03 01 23 05 수성못>대명1동 8500 배회\n"
            "0301 2305 수성못>대명1동 8500 카카오\n"
            "03012305 수성못>대명1동 8500 배회"
        )
        return

    # 중복 체크 → 자동 삭제 후 재저장
    deleted = await delete_duplicate_call(data["날짜"], data["배차시각"], data["요금"])
    if deleted:
        logger.info(f"수동입력 중복 삭제: {data['날짜']} {data['배차시각']}")

    result = await sb_insert("raw_calls", {
        "날짜":     data["날짜"],
        "요일":     data["요일"],
        "배차시각": data["배차시각"],
        "출발지":   data["출발지"],
        "도착지":   data["도착지"],
        "요금":     data["요금"],
        "콜유형":   data["콜유형"],
        "비고":     "수동입력",
    })

    if result:
        await update.message.reply_text(
            f"✅ 수동입력 저장\n"
            f"{data['날짜']}({data['요일']}) {data['배차시각']}\n"
            f"{data.get('출발지','-')}→{data.get('도착지','-')}\n"
            f"{data['요금']:,}원 [{data['콜유형']}]"
        )
    else:
        await update.message.reply_text("❌ DB 저장 실패")


async def handle_call_edit(update, text: str):
    """
    콜카드 수동 수정 명령어.
    형식:
      콜수정 HH:MM 필드=값
      콜수정 YYYY-MM-DD HH:MM 필드=값
    예:
      콜수정 19:18 요금=13100
      콜수정 19:18 배차시각=22:46
      콜수정 2026-03-20 19:18 도착지=수성구 만촌3동
    지원 필드: 배차시각, 하차시각, 출발지, 도착지, 요금, 콜유형, 비고
    """
    import re

    EDITABLE = {"배차시각","하차시각","출발지","도착지","요금","콜유형","비고"}

    parts = text.strip().split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "형식: 콜수정 HH:MM 필드=값\n"
            "날짜지정: 콜수정 YYYY-MM-DD HH:MM 필드=값\n"
            "예) 콜수정 19:18 요금=13100\n"
            "예) 콜수정 2026-03-20 19:18 배차시각=22:46"
        )
        return

    rest = parts[1].strip()

    # 날짜 포함 여부 판단
    date_match = re.match(r'^(\d{4}-\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+)$', rest)
    time_only  = re.match(r'^(\d{1,2}:\d{2})\s+(.+)$', rest)

    if date_match:
        target_date  = date_match.group(1)
        target_time  = date_match.group(2)
        field_str    = date_match.group(3)
    elif time_only:
        target_date  = str(today_kst())
        target_time  = time_only.group(1)
        field_str    = time_only.group(2)
    else:
        await update.message.reply_text("❌ 형식 오류\n예) 콜수정 19:18 요금=13100")
        return

    # 필드=값 파싱
    field_match = re.match(r'^(\S+?)=(.+)$', field_str.strip())
    if not field_match:
        await update.message.reply_text("❌ 필드=값 형식 오류\n예) 요금=13100")
        return

    field = field_match.group(1).strip()
    value = field_match.group(2).strip()

    if field not in EDITABLE:
        await update.message.reply_text(
            f"❌ '{field}' 는 수정 불가\n"
            f"수정 가능: {', '.join(sorted(EDITABLE))}"
        )
        return

    # 요금은 int 변환
    if field == "요금":
        try:
            value = int(value.replace(",","").replace("원",""))
        except ValueError:
            await update.message.reply_text("❌ 요금은 숫자만 입력 (예: 13100)")
            return

    # DB에서 해당 건 찾기
    rows = await sb_select("raw_calls", {
        "날짜": f"eq.{target_date}",
        "배차시각": f"eq.{target_time}",
    })

    if not rows:
        await update.message.reply_text(
            f"⚠️ {target_date} {target_time} 콜 없음\n"
            f"날짜·시각을 확인해주세요."
        )
        return

    if len(rows) > 1:
        lines_out = [f"⚠️ {target_time} 콜이 {len(rows)}건 있습니다. 어느 건?"]
        for r in rows:
            lines_out.append(
                f"  ID:{r['id']} {r.get('출발지','')}→{r.get('도착지','')} {fmt(r.get('요금') or 0)}"
            )
        lines_out.append("ID 지정: 콜수정ID [id] 필드=값")
        await update.message.reply_text("\n".join(lines_out))
        return

    row = rows[0]
    old_val = row.get(field)
    row_id  = row["id"]

    # PATCH
    result = await sb_h("PATCH", f"raw_calls?id=eq.{row_id}", json={field: value})

    if result is not None:
        await update.message.reply_text(
            f"✅ 콜 수정 완료\n"
            f"날짜: {target_date} | 배차: {target_time}\n"
            f"{field}: {old_val} → {value}"
        )
    else:
        await update.message.reply_text("❌ 수정 실패")


async def handle_call_edit_by_id(update, text: str):
    """
    ID 지정 수정: '콜수정ID [id] 필드=값'
    동일 시각 콜이 여러 건일 때 사용
    """
    import re
    EDITABLE = {"배차시각","하차시각","출발지","도착지","요금","콜유형","비고"}

    m = re.match(r'^(\d+)\s+(\S+?)=(.+)$', text.strip())
    if not m:
        await update.message.reply_text("형식: 콜수정ID [id] 필드=값\n예) 콜수정ID 42 요금=13100")
        return

    row_id = int(m.group(1))
    field  = m.group(2).strip()
    value  = m.group(3).strip()

    if field not in EDITABLE:
        await update.message.reply_text(f"❌ '{field}' 수정 불가")
        return

    if field == "요금":
        try:
            value = int(value.replace(",","").replace("원",""))
        except ValueError:
            await update.message.reply_text("❌ 요금은 숫자만")
            return

    rows = await sb_select("raw_calls", {"id": f"eq.{row_id}"})
    if not rows:
        await update.message.reply_text(f"❌ ID {row_id} 없음")
        return

    old_val = rows[0].get(field)
    await sb_h("PATCH", f"raw_calls?id=eq.{row_id}", json={field: value})
    await update.message.reply_text(
        f"✅ ID {row_id} 수정\n{field}: {old_val} → {value}"
    )


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


# ══════════════════════════════════════════════
# 어군탐지기 v2 — 자동 브리핑 시스템
# ══════════════════════════════════════════════

FISH_DATA = {
    "월": {
        "19~21": [["달서구 신당동",      "8",  "9,500",  "S", "신당동 주택가 도로변",   "카카오T 공식 평일 호출 1위"]],
        "21~24": [["달서구 신당동",      "9",  "10,200", "S", "신당동 상가 밀집",       "평일 23시 호출 1위"]],
        "00~02": [["중구 성내1·2동",     "9",  "10,800", "S", "성내2동 앵커",           "유흥가 마감 귀가 폭증"]],
    },
    "화": {
        "19~21": [["수성구 범어·만촌",   "7",  "8,200",  "A", "범어역~만촌역",          "화요일 수성구 준수"]],
        "21~24": [["달서구 신당동",      "7",  "9,500",  "A", "신당동 상가",             "화요일 야간 준수"]],
        "00~02": [["중구 성내1·2동",     "8",  "10,200", "S", "성내2동 앵커",            "화요일 심야 귀가"]],
    },
    "수": {
        "19~21": [["수성구 범어·만촌",   "7",  "8,500",  "A", "범어역~만촌역",          "수요일 수성구 준수"]],
        "21~24": [["중구 동성로/삼덕동", "8",  "9,800",  "A", "삼덕동 먹자골목",        "수요일 야간 선호"]],
        "00~02": [["중구 성내1·2동",     "8",  "10,500", "S", "성내2동 앵커",            "수요일 심야 귀가"]],
    },
    "목": {
        "19~21": [["달서구 신당동",      "6",  "7,800",  "B", "신당동 주택가",           "목요일 콜 저조 — 단축 검토"]],
        "21~24": [["달서구 신당동",      "7",  "9,200",  "A", "신당동 상가",             "목요일 막판 집중"]],
        "00~02": [["중구 성내1·2동",     "7",  "9,800",  "A", "성내2동 앵커",            "목요일 심야 귀가"]],
    },
    "금": {
        "19~21": [["수성구 범어·만촌",   "8",  "7,600",  "A", "범어역~만촌역",          "금요일 수성구 콜 실적 최다"]],
        "21~24": [["중구 동성로/삼덕동", "9",  "10,000", "S", "삼덕동 먹자골목",        "유흥 피크! 술집 01시 마감"]],
        "00~02": [["중구 성내1·2동",     "9",  "11,500", "S", "성내2동 앵커",            "동성로 마감 귀가 폭증"]],
    },
    "토": {
        "19~21": [["수성구 고산2동",     "8",  "9,200",  "S", "수성못 주변",             "주말 17~19시 호출 집중"]],
        "21~24": [["중구 동성로/삼덕동", "9",  "8,800",  "S", "삼덕동~동성로",           "토요일 밤 유흥 최고 피크"]],
        "00~02": [["중구 성내1동",       "9",  "14,400", "S", "성내1동~성내2동",         "토요일 막판 단가 14,433원 최고치"]],
    },
    "일": {
        "19~21": [["수성구 고산2동",     "8",  "9,200",  "S", "수성못 주변",             "주말 호출 1위"]],
        "21~24": [["중구 동성로",        "8",  "11,800", "S", "동성로 입구",             "일요일 밤 단가 11,811원 고효율"]],
        "00~02": [["중구 성내1동",       "10", "12,400", "S", "성내1동",                 "주말 00~01시 호출 1위 구역"]],
    },
}

def get_fish_slot(hour: int) -> str | None:
    """현재 시각 → 어군 슬롯 반환"""
    if 19 <= hour < 21: return "19~21"
    if 21 <= hour <= 23: return "21~24"
    if 0 <= hour < 2:   return "00~02"
    return None

def get_fish_report(custom_hour: int = None) -> str | None:
    """어군 브리핑 텍스트 생성. custom_hour로 특정 시간대 조회 가능."""
    now  = datetime.now(KST)
    hour = custom_hour if custom_hour is not None else now.hour
    day  = DOW_KOR[now.weekday()]
    slot = get_fish_slot(hour)
    if not slot:
        return None
    zones = FISH_DATA.get(day, {}).get(slot, [])
    if not zones:
        return f"🐟 {day}요일 {slot} 어군 데이터 없음\n마기에게 데이터 업데이트 요청하세요."
    lines = [f"🐟 어군브리핑 — {day}요일 {slot}"]
    for idx, z in enumerate(zones, 1):
        grade_icon = {"S": "🔴", "A": "🟠", "B": "🟡", "C": "⚪"}.get(z[3], "⚪")
        lines.append(f"\n#{idx} {z[0]} {grade_icon}{z[3]}등급")
        lines.append(f"  점수 {z[1]}/10 | 예상 {z[2]}원")
        lines.append(f"  📍 {z[4]}")
        lines.append(f"  💡 {z[5]}")
    return "\n".join(lines)

def fish_scheduler(app):
    """18:50 영업준비 브리핑 + 19~02시 매 정각 자동 브리핑"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 발송 대상: 등록된 모든 단말기
    chat_ids = [x for x in [
        os.getenv("ALLOWED_CHAT_ID", ""),
        os.getenv("ALLOWED_CHAT_ID2", ""),
    ] if x]

    async def send_all(text: str):
        for cid in chat_ids:
            try:
                await app.bot.send_message(chat_id=cid, text=text)
            except Exception as e:
                logger.error(f"어군 브리핑 발송 오류 ({cid}): {e}")

    last_sent_hour = -1
    sent_start_brief = False
    last_reset_day = -1

    while True:
        now = datetime.now(KST)

        # ── 매일 03시 플래그 리셋 (운행 종료 후)
        if now.hour == 3 and now.day != last_reset_day:
            sent_start_brief = False
            last_sent_hour   = -1
            last_reset_day   = now.day
            logger.info("어군 스케줄러 일간 리셋")

        # ── 18:50 영업 준비 브리핑
        if now.hour == 18 and now.minute == 50 and not sent_start_brief:
            report = get_fish_report(custom_hour=19) or "데이터 없음"
            msg = f"🚀 영업준비 브리핑 (10분 후 출발)\n\n{report}"
            loop.run_until_complete(send_all(msg))
            sent_start_brief = True
            logger.info("18:50 영업준비 브리핑 발송")

        # ── 19:00 ~ 02:00 매 정각 브리핑
        if now.minute == 0 and now.hour != last_sent_hour:
            in_service = (19 <= now.hour <= 23) or (0 <= now.hour < 2)
            if in_service:
                report = get_fish_report()
                if report:
                    loop.run_until_complete(send_all(report))
                    logger.info(f"어군 브리핑 발송: {now.hour}시")
                last_sent_hour = now.hour

        time.sleep(30)


# ──────────────────────────────────────────────
# 중복 자동 삭제 + 금액 불일치 처리
# ──────────────────────────────────────────────
FEE_DIFF_THRESHOLD = 500  # 이 이상 차이면 확인 요청

async def delete_duplicate_call(날짜: str, 배차시각: str, 요금: int) -> bool:
    """날짜+배차시각+요금 완전 일치 건 삭제. 삭제되면 True."""
    rows = await sb_select("raw_calls", {
        "날짜": f"eq.{날짜}",
        "배차시각": f"eq.{배차시각}",
        "요금": f"eq.{요금}",
    })
    if not rows:
        return False
    for row in rows:
        await sb_h("DELETE", f"raw_calls?id=eq.{row['id']}")
    return True

async def delete_duplicate_payment(날짜: str, 시각: str, 요금: int) -> bool:
    """날짜+시각+요금 완전 일치 결제내역 삭제."""
    rows = await sb_select("payment_receipts", {
        "날짜": f"eq.{날짜}",
        "시각": f"eq.{시각}",
        "요금": f"eq.{요금}",
    })
    if not rows:
        return False
    for row in rows:
        await sb_h("DELETE", f"payment_receipts?id=eq.{row['id']}")
    return True

async def send_fee_confirm(update, call_id: int, 배차시각: str,
                            call_fee: int, rcpt_fee: int, diff: int):
    """금액 불일치 ≥500원 시 InlineKeyboard 확인 요청 발송."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"콜카드 {call_fee:,}원",
                callback_data=f"fee:{call_id}:{call_fee}"
            ),
            InlineKeyboardButton(
                f"결제내역 {rcpt_fee:,}원",
                callback_data=f"fee:{call_id}:{rcpt_fee}"
            ),
        ]
    ])
    await update.message.reply_text(
        f"⚠️ 금액 불일치 확인 요청\n"
        f"배차: {배차시각}\n"
        f"콜카드: {call_fee:,}원\n"
        f"결제내역: {rcpt_fee:,}원\n"
        f"차이: {diff:,}원\n\n"
        f"어느 금액으로 저장할까요?",
        reply_markup=keyboard
    )

async def handle_fee_callback(update, context):
    """InlineKeyboard 버튼 클릭 처리."""
    query = update.callback_query
    await query.answer()
    data = query.data  # "fee:call_id:선택금액"
    try:
        _, call_id_str, fee_str = data.split(":")
        call_id = int(call_id_str)
        selected_fee = int(fee_str)
        # raw_calls 요금 업데이트
        await sb_h("PATCH", f"raw_calls?id=eq.{call_id}",
                   json={"요금": selected_fee})
        await query.edit_message_text(
            f"✅ {selected_fee:,}원으로 저장 완료"
        )
    except Exception as e:
        logger.error(f"fee callback 오류: {e}")
        await query.edit_message_text("❌ 처리 오류")

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


async def cmd_fish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 시간대 어군 브리핑 수동 조회"""
    if not is_allowed(update):
        return
    report = get_fish_report()
    if not report:
        now = datetime.now(KST)
        await update.message.reply_text(
            f"🐟 현재 {now.hour}시는 브리핑 시간대가 아닙니다.\n"
            f"운영시간: 19~21시 / 21~24시 / 00~02시"
        )
        return
    await update.message.reply_text(report)

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
        "[이미지] 콜카드·충전영수증·결제내역 → 자동저장\n\n"
        "[수동입력]\n"
        "콜 금액 / 배회 금액 / 충전 금액\n"
        "타이어·오일·세차 금액 / 지출 항목 금액\n"
        "지출취소 / 휴무 / 4-7 휴무\n\n"
        "[수동전체]\n"
        "2026 03 01 23 05 출발>도착 요금 카카오\n"
        "0301 2305 출발>도착 요금 배회\n\n"
        "[콜수정]\n"
        "콜수정 HH:MM 필드=값\n"
        "콜수정 날짜 HH:MM 필드=값\n"
        "콜수정ID [id] 필드=값\n\n"
        "[조회]\n"
        "오늘·이번 주·이번 달·지출 확인·DB 확인\n"
        "3-7 조회·매출·순수익·총건수·지출\n\n"
        "[교차대조]\n"
        "대조 날짜 (예: 대조 3-7)\n"
        "대조 확정 날짜 / 대조 금액확인 날짜\n\n"
        "[결제삭제]\n"
        "결제삭제 날짜 운행외/0원/전체/HH:MM\n\n"
        "[전략] 전략 / 마기 업데이트 시간대 내용\n\n"
        "[다운로드]\n"
        "주간/월간/전체 다운로드 / 월간 2026-03\n\n"
        "[어군] /fish"
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

    # 교차대조 — YYYY-MM-DD 또는 M-D 형식 지원
    if text.startswith("대조 "):
        import re as _re3
        from datetime import date as _date3
        date_str = text[3:].strip()
        _md = _re3.match(r'^(\d{1,2})-(\d{1,2})$', date_str)
        if _md:
            try:
                _d = _date3(_date3.today().year, int(_md.group(1)), int(_md.group(2)))
                date_str = str(_d)
            except ValueError:
                await update.message.reply_text("❌ 잘못된 날짜입니다.")
                return
        try:
            result = await cross_check(date_str)
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

    # 날짜 + 통계 키워드 조합
    _stat_kws = ["총건수","건수","매출","순수익","지출","조회","상세"]
    _date_pat = r"(\d{1,2})[-/](\d{1,2})|(\d{4})-(\d{1,2})-(\d{1,2})"
    import re as _re
    if _re.search(_date_pat, text) and any(kw in text for kw in _stat_kws):
        if any(kw in text for kw in ["총건수","건수","매출","순수익","지출"]):
            await handle_date_stat(update, text)
        else:
            await handle_date_query(update, text)
        return

    # 특정 날짜 조회 (조회 키워드만 있을 때)
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

    # 수동 전체 입력 (날짜+시각+경로+요금 형식)
    import re as _re2
    _has_route = bool(_re2.search(r'[가-힣\w]+[>→][가-힣\w]+', text))
    _has_date_nums = bool(_re2.search(r'\d{2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\d{2}|\d{4}[\-.]\d{1,2}[\-.]\d{1,2}|\d{8}|\d{4}\s+\d{4}', text))
    if _has_route and _has_date_nums:
        await handle_manual_full_call(update, text)
        return

    # 콜카드 수동 수정
    if text.startswith("콜수정ID "):
        await handle_call_edit_by_id(update, text[6:].strip())
        return

    if text.startswith("콜수정 "):
        await handle_call_edit(update, text)
        return

    # 휴무 (오늘 또는 날짜 지정)
    if "휴무" in text:
        await handle_rest_day(update, text)
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
        .get_updates_read_timeout(5)    # 폴링 빠른 타임아웃 → Conflict 최소화
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

    # 어군탐지기 스케줄러 — app 생성 후 시작
    threading.Thread(target=fish_scheduler, args=(app,), daemon=True).start()
    logger.info("어군탐지기 스케줄러 시작")

    # 핸들러 등록
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fish", cmd_fish))    # 어군 브리핑 수동 조회
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(handle_fee_callback, pattern=r"^fee:"))

    logger.info("자비스 v5 시작")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )

if __name__ == "__main__":
    main()
