"""
자비스 봇 v5.1 — 2026-03-27
변경사항:
  - [BUG-05 재수정] 콜카드 날짜 강제 오늘날짜 덮어쓰기 완전 제거
    → OCR 추출 날짜 우선 사용, 불명확 시 사용자에게 확인 요청
  - [BUG-10 신규] 전략 출력 시 현재 KST 시각 기준 검증 추가
    → 업로드 시각이 아닌 실제 현재 시각으로 strategy_lookup 조회
  - [신규] 주간 목표 950,000원 / 월요일 기준 주차 시작
  - [신규] 콜카드 날짜 OCR 신뢰도 검증 단계 추가
"""

import os
import re
import json
import logging
import asyncio
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from supabase import create_client, Client
import anthropic
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─────────────────────────────────────────────
# 0. 기본 설정
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

def now_kst() -> datetime:
    """현재 KST datetime 반환 (UTC 오류 방지)"""
    return datetime.now(KST)

def today_kst() -> date:
    """현재 KST date 반환"""
    return now_kst().date()

def current_hour_kst() -> int:
    """현재 KST 시(hour) 반환"""
    return now_kst().hour

def week_start_kst(d: date = None) -> date:
    """월요일 기준 주차 시작일 반환"""
    if d is None:
        d = today_kst()
    return d - timedelta(days=d.weekday())  # weekday(): 월=0, 일=6

# ─────────────────────────────────────────────
# 1. 환경변수
# ─────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ALLOWED_IDS      = {
    int(os.environ["ALLOWED_CHAT_ID"]),
    int(os.environ.get("ALLOWED_CHAT_ID2", "0")),
}
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_KEY"]

# ─────────────────────────────────────────────
# 2. 클라이언트 초기화
# ─────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=30.0)

# ─────────────────────────────────────────────
# 3. 상수
# ─────────────────────────────────────────────
DAILY_INSURANCE   = 7945        # 일일 보험료 (원)
CARD_FEE_RATE     = 0.033       # 카드 수수료율
CHARGE_RATE       = 160         # 충전단가 (원/kWh)
DAILY_TARGET      = 100_000     # 일 순수익 목표 (원)
WEEKLY_TARGET     = 950_000     # 주간 매출 목표 (원) ← 신규
AVG_KWH_PER_DAY   = 35.0       # 일평균 충전량 (kWh) — 실측 반영 필요

OCR_MODEL      = "claude-haiku-4-5-20251001"
SONNET_MODEL   = "claude-sonnet-4-6"

# 콜카드 날짜 유효 범위
DATE_LOOKBACK_DAYS = 60   # 오늘로부터 최대 60일 이전까지 유효

# ─────────────────────────────────────────────
# 4. 인증 미들웨어
# ─────────────────────────────────────────────
def authorized(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_chat.id
        if cid not in ALLOWED_IDS:
            await update.message.reply_text("🚫 인증되지 않은 사용자입니다.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

# ─────────────────────────────────────────────
# 5. 유틸리티
# ─────────────────────────────────────────────
def fmt_money(n) -> str:
    try:
        return f"{int(n):,}원"
    except:
        return str(n)

def parse_money(s: str) -> int:
    """'5,600원' → 5600"""
    return int(re.sub(r"[^\d]", "", s))

def calc_net(gross: int, is_card: bool = True) -> int:
    """순수익 계산 (수수료 + 보험료 제외)"""
    fee = int(gross * CARD_FEE_RATE) if is_card else 0
    return gross - fee - DAILY_INSURANCE

def weekday_str(d: date) -> str:
    days = ["월", "화", "수", "목", "금", "토", "일"]
    return days[d.weekday()]

# ─────────────────────────────────────────────
# 6. 날짜 파싱 & 검증 (BUG-05 완전 수정)
# ─────────────────────────────────────────────
def parse_and_validate_date(raw: str) -> tuple[date | None, str]:
    """
    OCR에서 추출한 날짜 문자열을 파싱·검증.
    반환: (date 객체 or None, 상태 메시지)

    상태:
      "ok"       → 정상
      "future"   → 미래 날짜 (오류)
      "old"      → 60일 초과 과거 (확인 필요)
      "unknown"  → 파싱 실패
    """
    if not raw or raw.strip().upper() in ("", "UNKNOWN", "없음", "N/A"):
        return None, "unknown"

    # 여러 형식 시도
    formats = [
        "%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d",
        "%Y년%m월%d일", "%m/%d", "%m-%d",
    ]
    parsed = None
    for fmt in formats:
        try:
            if fmt in ("%m/%d", "%m-%d"):
                # 연도 없으면 올해로 보정
                tmp = datetime.strptime(raw.strip(), fmt)
                parsed = tmp.replace(year=today_kst().year).date()
            else:
                parsed = datetime.strptime(raw.strip(), fmt).date()
            break
        except ValueError:
            continue

    if parsed is None:
        return None, "unknown"

    today = today_kst()
    if parsed > today:
        return parsed, "future"
    if parsed < today - timedelta(days=DATE_LOOKBACK_DAYS):
        return parsed, "old"
    return parsed, "ok"

# ─────────────────────────────────────────────
# 7. OCR — 콜카드 (BUG-05 완전 수정)
# ─────────────────────────────────────────────
async def ocr_call_card(image_b64: str, mime: str = "image/jpeg") -> dict:
    """
    콜카드 OCR.
    날짜는 절대 오늘 날짜로 강제하지 않음.
    인식 실패 시 None 반환 → 호출부에서 사용자 확인 요청.
    """
    prompt = """카카오T 콜카드 이미지를 분석하여 아래 JSON을 반환하라.

중요 규칙:
1. 날짜(date)는 이미지에 보이는 날짜를 그대로 추출. YYYY/MM/DD 형식으로.
2. 날짜가 보이지 않거나 불확실하면 반드시 "UNKNOWN"을 반환. 절대 오늘 날짜로 대체하지 말 것.
3. 금액은 숫자만 (쉼표 제거, 원 제거).
4. 콜유형: "카카오T" 또는 "배회영업"

반환 형식 (JSON만, 마크다운 없이):
{
  "date": "2026/03/16",
  "dispatch_time": "19:43",
  "origin": "대구 수성구 범어동",
  "destination": "대구 동구 신천4동",
  "amount": 5600,
  "call_type": "카카오T",
  "confidence": "high"
}

confidence: "high"(날짜 명확히 보임) / "low"(날짜 흐리거나 잘림) / "none"(날짜 없음)"""

    resp = claude_client.messages.create(
        model=OCR_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": image_b64
                }},
                {"type": "text", "text": prompt}
            ]
        }]
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    try:
        return json.loads(raw)
    except:
        logger.error(f"OCR JSON 파싱 실패: {raw}")
        return {}

# ─────────────────────────────────────────────
# 8. OCR — 충전 내역
# ─────────────────────────────────────────────
async def ocr_charging(image_b64: str, mime: str = "image/jpeg") -> list[dict]:
    prompt = """충전 영수증 이미지에서 충전 내역을 추출하라.
여러 건이면 배열로 반환.
JSON만 반환 (마크다운 없이):
[{"date":"2026/03/16","kwh":35.2,"amount":5632,"location":"OO충전소"}]
날짜 불명확 시 "UNKNOWN" 사용."""
    resp = claude_client.messages.create(
        model=OCR_MODEL, max_tokens=400,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":mime,"data":image_b64}},
            {"type":"text","text":prompt}
        ]}]
    )
    raw = re.sub(r"```json|```","",resp.content[0].text.strip()).strip()
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else [result]
    except:
        return []

# ─────────────────────────────────────────────
# 9. OCR — 세큐티 등급
# ─────────────────────────────────────────────
async def ocr_sekuti(image_b64: str, mime: str = "image/jpeg") -> dict:
    prompt = """세큐티 등급 화면에서 점수를 추출하라.
JSON만 반환:
{"date":"2026/03/20","total_score":95,"safety_score":100,"air_score":88,"kakao_rating":4.9,"rank_pct":8,"monthly_km":2113}
없는 항목은 null."""
    resp = claude_client.messages.create(
        model=OCR_MODEL, max_tokens=300,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":mime,"data":image_b64}},
            {"type":"text","text":prompt}
        ]}]
    )
    raw = re.sub(r"```json|```","",resp.content[0].text.strip()).strip()
    try:
        return json.loads(raw)
    except:
        return {}

# ─────────────────────────────────────────────
# 10. OCR — 수요지도
# ─────────────────────────────────────────────
async def ocr_demand(image_b64: str, mime: str = "image/jpeg") -> dict:
    prompt = """카카오T 수요지도 화면을 분석하라.
JSON만 반환:
{"date":"2026/03/27","time":"19:30","zones":[{"name":"수성구 범어동","level":"high"},{"name":"중구 성내동","level":"medium"}]}"""
    resp = claude_client.messages.create(
        model=OCR_MODEL, max_tokens=400,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":mime,"data":image_b64}},
            {"type":"text","text":prompt}
        ]}]
    )
    raw = re.sub(r"```json|```","",resp.content[0].text.strip()).strip()
    try:
        return json.loads(raw)
    except:
        return {}

# ─────────────────────────────────────────────
# 10-1. OCR — 결제내역조회
# ─────────────────────────────────────────────
async def ocr_payment(image_b64: str, mime: str = "image/jpeg") -> list[dict]:
    """
    카카오T 결제내역조회 화면 OCR.
    여러 건의 결제목록을 추출.
    카드사 있음 → 카카오T / 없음(현금) → 배회영업
    반환: [{"time":"23:54","amount":5400,"card":"현대카드","call_type":"카카오T"}, ...]
    """
    prompt = """카카오T 결제내역조회 화면에서 모든 거래 내역을 추출하라.
각 거래의 거래일시(시:분), 금액, 카드사를 추출.
카드사가 없으면 null.

JSON 배열만 반환 (마크다운 없이):
[
  {"time":"23:54","amount":5400,"card":"현대카드"},
  {"time":"23:36","amount":12950,"card":"KB카드"},
  {"time":"23:04","amount":6300,"card":null}
]

규칙:
1. 금액은 숫자만 (쉼표/원 제거)
2. 시간은 HH:MM 형식
3. 카드사 없으면 반드시 null
4. 취소 건은 제외
5. JSON 배열만, 설명 없이"""

    resp = claude_client.messages.create(
        model=OCR_MODEL, max_tokens=1000,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":mime,"data":image_b64}},
            {"type":"text","text":prompt}
        ]}]
    )
    raw = re.sub(r"```json|```","",resp.content[0].text.strip()).strip()
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            items = [items]
        # call_type 자동 분류
        for item in items:
            item["call_type"] = "카카오T" if item.get("card") else "배회영업"
        return items
    except Exception as e:
        logger.error(f"결제내역 OCR 파싱 실패: {e} / raw: {raw}")
        return []

# ─────────────────────────────────────────────
# 11. OCR — 이미지 자동 분류
# ─────────────────────────────────────────────
async def classify_image(image_b64: str, mime: str, caption: str = "") -> str:
    """
    이미지 종류 자동 분류.
    반환: "call_card" | "charging" | "sekuti" | "demand" | "unknown"
    """
    cap = (caption or "").strip().lower()

    # 캡션 우선 판단
    if any(k in cap for k in ["충전","charge","kwh"]):
        return "charging"
    if any(k in cap for k in ["세큐티","등급","sekuti"]):
        return "sekuti"
    if any(k in cap for k in ["수요","수요지도","demand"]):
        return "demand"
    if any(k in cap for k in ["결제내역","결제","정산","payment"]):
        return "payment"
    if "call_history" in cap:
        return "call_card"

    # 캡션 없으면 Haiku로 분류
    prompt = """이 이미지가 다음 중 어떤 종류인지 한 단어로만 답하라:
call_card (카카오T 콜카드/운행이력 — 출발지/도착지/요금 1건)
charging (전기차 충전 영수증)
sekuti (세큐티 등급 화면)
demand (수요지도)
payment (카카오T 결제내역조회 — 여러 건의 결제목록)
unknown (그 외)

답변: 위 6가지 중 하나만."""
    resp = claude_client.messages.create(
        model=OCR_MODEL, max_tokens=20,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":mime,"data":image_b64}},
            {"type":"text","text":prompt}
        ]}]
    )
    answer = resp.content[0].text.strip().lower()
    for t in ["call_card","charging","sekuti","demand","payment"]:
        if t in answer:
            return t
    return "call_card"  # 기본값

# ─────────────────────────────────────────────
# 12. DB 저장 함수들
# ─────────────────────────────────────────────
def save_raw_call(d: date, dispatch_time: str, origin: str,
                  destination: str, amount: int,
                  call_type: str = "카카오T", note: str = "") -> bool:
    try:
        row = {
            "날짜": str(d),
            "요일": weekday_str(d),
            "배차시각": dispatch_time,
            "출발지": origin,
            "도착지": destination,
            "요금": amount,
            "콜유형": call_type,
            "비고": note,
        }
        supabase.table("raw_calls").insert(row).execute()
        return True
    except Exception as e:
        logger.error(f"raw_calls 저장 오류: {e}")
        return False

def save_pending_call(d: date, dispatch_time: str, amount: int,
                      call_type: str = "카카오T") -> bool:
    try:
        row = {
            "날짜": str(d),
            "배차시각": dispatch_time,
            "요금": amount,
            "콜유형": call_type,
            "확정": False,
        }
        supabase.table("pending_calls").insert(row).execute()
        return True
    except Exception as e:
        logger.error(f"pending_calls 저장 오류: {e}")
        return False

def save_charging(d: date, kwh: float, amount: int, location: str = "") -> bool:
    try:
        row = {"날짜": str(d), "kwh": kwh, "금액": amount, "장소": location}
        supabase.table("charging_log").insert(row).execute()
        return True
    except Exception as e:
        logger.error(f"charging_log 저장 오류: {e}")
        return False

def save_sekuti(data: dict) -> bool:
    try:
        supabase.table("sekuti_weekly").insert(data).execute()
        return True
    except Exception as e:
        logger.error(f"sekuti_weekly 저장 오류: {e}")
        return False

def save_demand(data: dict) -> bool:
    try:
        supabase.table("demand_map").insert(data).execute()
        return True
    except Exception as e:
        logger.error(f"demand_map 저장 오류: {e}")
        return False

def save_expense(d: date, category: str, amount: int, note: str = "") -> bool:
    try:
        row = {"날짜": str(d), "항목": category, "금액": amount, "비고": note}
        supabase.table("expenses").insert(row).execute()
        return True
    except Exception as e:
        logger.error(f"expenses 저장 오류: {e}")
        return False

# ─────────────────────────────────────────────
# 13. DB 조회 함수들
# ─────────────────────────────────────────────
def get_today_calls(d: date = None) -> list[dict]:
    if d is None:
        d = today_kst()
    r = supabase.table("raw_calls").select("*").eq("날짜", str(d)).execute()
    return r.data or []

def get_today_summary(d: date = None) -> dict:
    if d is None:
        d = today_kst()
    calls = get_today_calls(d)
    total = sum(c.get("요금", 0) for c in calls)
    card_total = sum(c.get("요금", 0) for c in calls if c.get("콜유형") != "현금")
    fee = int(card_total * CARD_FEE_RATE)
    net = total - fee - DAILY_INSURANCE
    return {
        "date": d,
        "count": len(calls),
        "gross": total,
        "fee": fee,
        "net": net,
        "calls": calls,
    }

def get_week_summary(week_start: date = None) -> dict:
    """월요일 기준 주간 집계"""
    if week_start is None:
        week_start = week_start_kst()
    week_end = week_start + timedelta(days=6)

    r = supabase.table("raw_calls").select("날짜,요금,콜유형")\
        .gte("날짜", str(week_start)).lte("날짜", str(week_end)).execute()
    calls = r.data or []

    total = sum(c.get("요금", 0) for c in calls)
    card_total = sum(c.get("요금", 0) for c in calls if c.get("콜유형") != "현금")
    fee = int(card_total * CARD_FEE_RATE)

    # 운행일 수 계산 (휴무 제외)
    operated_days = len(set(c["날짜"] for c in calls))

    return {
        "week_start": week_start,
        "week_end": week_end,
        "total": total,
        "fee": fee,
        "net": total - fee,
        "count": len(calls),
        "operated_days": operated_days,
        "target": WEEKLY_TARGET,
        "achievement": round(total / WEEKLY_TARGET * 100, 1) if WEEKLY_TARGET else 0,
        "remaining": max(0, WEEKLY_TARGET - total),
    }

def get_month_summary(year: int = None, month: int = None) -> dict:
    today = today_kst()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    from calendar import monthrange
    _, last_day = monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    r = supabase.table("raw_calls").select("날짜,요금,콜유형")\
        .gte("날짜", str(start)).lte("날짜", str(end)).execute()
    calls = r.data or []

    total = sum(c.get("요금", 0) for c in calls)
    card_total = sum(c.get("요금", 0) for c in calls if c.get("콜유형") != "현금")
    fee = int(card_total * CARD_FEE_RATE)
    operated_days = len(set(c["날짜"] for c in calls))

    return {
        "year": year, "month": month,
        "total": total, "fee": fee,
        "net": total - fee,
        "count": len(calls),
        "operated_days": operated_days,
        "avg_per_day": round(total / operated_days) if operated_days else 0,
        "avg_per_call": round(total / len(calls)) if calls else 0,
    }

def get_today_expenses(d: date = None) -> list[dict]:
    if d is None:
        d = today_kst()
    r = supabase.table("expenses").select("*").eq("날짜", str(d)).execute()
    return r.data or []

def get_strategy(hour: int, weekday: str = None) -> dict | None:
    """
    strategy_lookup 실제 컬럼 기준 조회
    컬럼: 시간대, 요일, 트리거, 행동지침, 재진입거점, 공차기준분, 우선순위
    """
    try:
        r = supabase.table("strategy_lookup").select("*").execute()
        rows = r.data or []
    except Exception as e:
        logger.error(f"strategy_lookup 조회 오류: {e}")
        return None

    if weekday is None:
        weekday = ["월","화","수","목","금","토","일"][now_kst().weekday()]

    def hour_matches(시간대):
        if not 시간대 or 시간대 == "전체":
            return True
        try:
            t = 시간대.replace("시","").strip()
            parts = t.split("~")
            if len(parts) == 2:
                s, e = int(parts[0]), int(parts[1])
                if s <= e:
                    return s <= hour < e
                else:
                    return hour >= s or hour < e
        except:
            pass
        return False

    def day_matches(요일):
        if not 요일 or 요일 == "전체":
            return True
        return weekday in 요일

    matched = [r for r in rows if hour_matches(r.get("시간대","")) and day_matches(r.get("요일",""))]
    if not matched:
        matched = [r for r in rows if hour_matches(r.get("시간대",""))]
    if not matched:
        return None

    priority = {"높음": 0, "긴급": 0, "보통": 1}
    matched.sort(key=lambda x: priority.get(x.get("우선순위","보통"), 1))
    return matched[0]

def get_latest_sekuti() -> dict | None:
    r = supabase.table("sekuti_weekly").select("*")\
        .order("date", desc=True).limit(1).execute()
    return r.data[0] if r.data else None

def get_stage_status() -> dict:
    """Stage 1→2 전환 지표 현황"""
    today = today_kst()
    two_weeks_ago = today - timedelta(days=14)

    r = supabase.table("raw_calls").select("날짜,요금,도착지")\
        .gte("날짜", str(two_weeks_ago)).execute()
    calls = r.data or []

    total = len(calls)
    if total == 0:
        return {"stage": 1, "avg_fare": 0, "gyeongsan_ratio": 0}

    avg_fare = sum(c.get("요금", 0) for c in calls) / total
    gyeongsan = [c for c in calls if "경산" in (c.get("도착지") or "")]
    long_dist_ratio = len(gyeongsan) / total * 100

    return {
        "stage": 1 if avg_fare < 10000 else 2,
        "avg_fare": round(avg_fare),
        "gyeongsan_ratio": round(long_dist_ratio, 1),
        "total_calls_2w": total,
    }

def get_master_status() -> dict:
    """마스터 조건 판정 (종합95↑ AND 상위5%)"""
    grade = get_latest_sekuti()
    if not grade:
        return {"is_master": False, "reason": "등급 데이터 없음"}

    total_score = grade.get("total_score", 0)
    rank_pct = grade.get("rank_pct", 100)

    conditions = {
        "score_ok": total_score >= 95,
        "rank_ok": rank_pct <= 5,
    }
    is_master = all(conditions.values())

    return {
        "is_master": is_master,
        "total_score": total_score,
        "rank_pct": rank_pct,
        "conditions": conditions,
        "gap_score": max(0, 95 - total_score),
        "gap_rank": max(0, rank_pct - 5),
    }

# ─────────────────────────────────────────────
# 14. 전략 생성 (BUG-10 수정 — 현재 KST 시각 기준)
# ─────────────────────────────────────────────
async def generate_strategy(context_text: str = "") -> str:
    """
    현재 KST 시각 기준으로 strategy_lookup 조회 후 전략 반환.
    DB에 없을 때만 Claude 호출 (BUG-07 유지).
    """
    current_hour = current_hour_kst()
    current_dt = now_kst()

    strategy_row = get_strategy(current_hour)

    if strategy_row:
        trigger   = strategy_row.get("트리거", "")
        action    = strategy_row.get("행동지침", "")
        reentry   = strategy_row.get("재진입거점", "")
        idle_min  = strategy_row.get("공차기준분", "")
        base = f"▶ {trigger}\n{action}"
        if reentry:
            base += f"\n📍 재진입: {reentry}"
        if idle_min:
            base += f"\n⏱ 공차기준: {idle_min}분"
        return (
            f"🕐 현재 시각: {current_dt.strftime('%H:%M')} (KST)\n\n"
            f"{base}\n\n"
            f"{context_text}"
        ).strip()

    # DB 없을 때 Claude 호출
    week_s = get_week_summary()
    today_s = get_today_summary()

    prompt = f"""현재 KST 시각: {current_dt.strftime('%Y-%m-%d %H:%M')}
오늘 누게: {today_s['count']}건 / {fmt_money(today_s['gross'])}
이번 주 누게: {week_s['total']:,}원 (목표 {WEEKLY_TARGET:,}원 / 달성률 {week_s['achievement']}%)
잔여 주간 목표: {fmt_money(week_s['remaining'])}

위 정보를 바탕으로 지금 이 시각({current_dt.strftime('%H')}시)에 맞는
대구 택시 운행 전략을 3줄 이내로 간결하게 안내하라.
{context_text}"""

    resp = claude_client.messages.create(
        model=SONNET_MODEL, max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return (
        f"🕐 현재 시각: {current_dt.strftime('%H:%M')} (KST)\n\n"
        + resp.content[0].text.strip()
    )

# ─────────────────────────────────────────────
# 15. 마감 브리핑 생성
# ─────────────────────────────────────────────
async def generate_closing_briefing(d: date = None) -> str:
    if d is None:
        d = today_kst()

    today_s = get_today_summary(d)
    week_s  = get_week_summary(week_start_kst(d))
    expenses = get_today_expenses(d)
    stage   = get_stage_status()
    master  = get_master_status()

    gross  = today_s["gross"]
    fee    = today_s["fee"]
    charge_cost = int(AVG_KWH_PER_DAY * CHARGE_RATE)
    exp_total = sum(e.get("금액", 0) for e in expenses)
    total_cost = fee + DAILY_INSURANCE + charge_cost + exp_total
    net_real = gross - total_cost

    # 전주 동요일 비교
    last_week_same = d - timedelta(days=7)
    lws = get_today_summary(last_week_same)

    diff = gross - lws["gross"]
    diff_str = f"+{fmt_money(diff)}" if diff >= 0 else f"-{fmt_money(abs(diff))}"

    # Stage 진행률
    stage_lines = (
        f"건당단가: {stage['avg_fare']:,}원 "
        f"({'✅' if stage['avg_fare'] >= 10000 else '🔲'} 10,000원 목표)\n"
        f"장거리비율: {stage['gyeongsan_ratio']}% "
        f"({'✅' if stage['gyeongsan_ratio'] >= 12 else '🔲'} 12% 목표)"
    )

    # 마스터 조건
    m = master
    score_ok_str = "✅" if m["conditions"]["score_ok"] else f"({m['gap_score']}점 부족)"
    rank_ok_str  = "✅" if m["conditions"]["rank_ok"]  else f"({m['gap_rank']}%p 부족)"
    master_lines = (
        f"종합점수: {m['total_score']}점 {score_ok_str}\n"
        f"상위순위: {m['rank_pct']}% {rank_ok_str}\n"
        f"마스터: {'🎖️ 달성!' if m['is_master'] else '미달성'}"
    )

    # 내일 전략
    tomorrow = d + timedelta(days=1)
    tomorrow_strategy = await generate_strategy(
        f"내일({tomorrow.strftime('%m/%d')} {weekday_str(tomorrow)}요일) 운행 전략을 2줄로"
    )

    lines = [
        f"📋 [{d.strftime('%m/%d')} {weekday_str(d)}요일] 마감 브리핑",
        "",
        "① 매출",
        f"  총매출: {fmt_money(gross)} ({today_s['count']}건)",
        f"  카드수수료: -{fmt_money(fee)}",
        f"  순매출: {fmt_money(gross - fee)}",
        "",
        "② 오늘 비용",
        f"  충전비: {fmt_money(charge_cost)}",
        f"  보험료: {fmt_money(DAILY_INSURANCE)}",
    ] + [f"  {e['항목']}: {fmt_money(e['금액'])}" for e in expenses] + [
        f"  합계: {fmt_money(total_cost)}",
        "",
        "③ 실질순수익",
        f"  {fmt_money(net_real)} (달성률 {round(net_real/DAILY_TARGET*100,1)}%)",
        f"  목표까지: {fmt_money(max(0, DAILY_TARGET - net_real))}",
        "",
        "④ 전주 동요일 비교",
        f"  저번 주 {weekday_str(last_week_same)}요일: {fmt_money(lws['gross'])}",
        f"  증감: {diff_str}",
        "",
        "⑤ 주간 현황 (월요일 기준)",
        f"  {week_s['week_start'].strftime('%m/%d')}~{week_s['week_end'].strftime('%m/%d')}",
        f"  누계: {fmt_money(week_s['total'])} / 목표 {fmt_money(WEEKLY_TARGET)}",
        f"  달성률: {week_s['achievement']}% / 잔여: {fmt_money(week_s['remaining'])}",
        "",
        "⑥ Stage 1→2 지표",
        stage_lines,
        "",
        "⑦ 마스터 조건",
        master_lines,
        "",
        "⑧ 내일 전략",
        tomorrow_strategy,
    ]

    return "\n".join(lines)

# ─────────────────────────────────────────────
# 16. 이미지 핸들러 (메인 — BUG-05 완전 수정)
# ─────────────────────────────────────────────
@authorized
async def handle_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    caption = (msg.caption or "").strip()

    # 이미지 다운로드
    if msg.photo:
        file = await msg.photo[-1].get_file()
        mime = "image/jpeg"
    elif msg.document and msg.document.mime_type.startswith("image"):
        file = await msg.document.get_file()
        mime = msg.document.mime_type
    else:
        await msg.reply_text("이미지를 인식할 수 없습니다.")
        return

    await msg.reply_text("📥 이미지 분석 중...")

    try:
        img_bytes = await file.download_as_bytearray()
        img_b64 = base64.b64encode(img_bytes).decode()

        # 분류
        img_type = await classify_image(img_b64, mime, caption)

        if img_type == "charging":
            await _handle_charging_image(msg, img_b64, mime)
        elif img_type == "sekuti":
            await _handle_sekuti_image(msg, img_b64, mime)
        elif img_type == "demand":
            await _handle_demand_image(msg, img_b64, mime)
        elif img_type == "payment":
            await _handle_payment_image(msg, img_b64, mime, ctx)
        else:
            await _handle_call_card_image(msg, img_b64, mime, ctx)
    except Exception as e:
        logger.error(f"이미지 처리 오류: {e}")
        await msg.reply_text("❌ 이미지 처리 중 오류. 잠시 후 다시 시도해 주세요.")

async def _handle_call_card_image(msg, img_b64: str, mime: str, ctx):
    """콜카드 처리 — 날짜 검증 포함"""
    try:
        ocr = await ocr_call_card(img_b64, mime)
    except Exception as e:
        logger.error(f"콜카드 OCR 오류: {e}")
        await msg.reply_text("❌ OCR 오류 발생. 다시 시도해 주세요.")
        return
    if not ocr:
        await msg.reply_text("❌ OCR 실패. 이미지를 다시 전송해 주세요.")
        return

    raw_date = ocr.get("date", "")
    confidence = ocr.get("confidence", "none")
    parsed_date, status = parse_and_validate_date(raw_date)

    amount = ocr.get("amount", 0)
    dispatch_time = ocr.get("dispatch_time", "")
    origin = ocr.get("origin", "")
    destination = ocr.get("destination", "")
    call_type = ocr.get("call_type", "카카오T")

    # ── 날짜 정상 ──────────────────────────────
    if status == "ok":
        ok = save_raw_call(parsed_date, dispatch_time, origin, destination, amount, call_type)
        if ok:
            today_s = get_today_summary(parsed_date)
            await msg.reply_text(
                f"✅ 콜카드 저장완료 — {parsed_date.strftime('%m/%d')} ({weekday_str(parsed_date)})\n\n"
                f"🚖 {dispatch_time} | {origin} → {destination}\n"
                f"💰 {fmt_money(amount)} | {call_type}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📊 {parsed_date.strftime('%m/%d')} 누계\n"
                f"  건수: {today_s['count']}건 | 매출: {fmt_money(today_s['gross'])}\n"
                f"  순수익: {fmt_money(today_s['net'])}\n"
                f"  달성률: {round(today_s['net']/DAILY_TARGET*100,1)}%"
            )
        else:
            await msg.reply_text("❌ DB 저장 실패. 다시 시도해 주세요.")

    # ── 날짜 불명확/실패 → 사용자 확인 요청 ───
    elif status in ("unknown", "future", "old"):
        reason_map = {
            "unknown": "날짜를 인식하지 못했습니다",
            "future":  f"미래 날짜({raw_date})가 감지됐습니다",
            "old":     f"60일 초과 과거({raw_date})입니다",
        }
        reason = reason_map[status]

        # 임시 저장 후 확인 요청
        ctx.user_data["pending_ocr"] = {
            "amount": amount,
            "dispatch_time": dispatch_time,
            "origin": origin,
            "destination": destination,
            "call_type": call_type,
            "raw_date": raw_date,
        }

        today_str = today_kst().strftime("%Y/%m/%d")
        yesterday_str = (today_kst() - timedelta(days=1)).strftime("%Y/%m/%d")

        keyboard = [
            [InlineKeyboardButton(f"오늘 ({today_str})", callback_data=f"date_confirm:{today_str}")],
            [InlineKeyboardButton(f"어제 ({yesterday_str})", callback_data=f"date_confirm:{yesterday_str}")],
            [InlineKeyboardButton("직접 입력 (YYYY/MM/DD)", callback_data="date_manual")],
            [InlineKeyboardButton("❌ 취소", callback_data="date_cancel")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await msg.reply_text(
            f"⚠️ 날짜 확인 필요\n"
            f"사유: {reason}\n\n"
            f"요금: {fmt_money(amount)} | {dispatch_time}\n"
            f"{origin} → {destination}\n\n"
            f"이 콜카드의 날짜를 선택하세요:",
            reply_markup=reply_markup
        )

async def _handle_charging_image(msg, img_b64: str, mime: str):
    try:
        items = await ocr_charging(img_b64, mime)
    except Exception as e:
        logger.error(f"충전 OCR 오류: {e}")
        await msg.reply_text("❌ 충전 OCR 오류. 다시 시도해 주세요.")
        return
    if not items:
        await msg.reply_text("❌ 충전 내역 인식 실패.")
        return
    results = []
    for item in items:
        raw_date = item.get("date", "")
        parsed, status = parse_and_validate_date(raw_date)
        d = parsed if status == "ok" else today_kst()
        kwh = item.get("kwh", 0)
        amount = item.get("amount", 0)
        location = item.get("location", "")
        save_charging(d, kwh, amount, location)
        results.append(f"⚡ {d.strftime('%m/%d')} | {kwh}kWh | {fmt_money(amount)}")
    await msg.reply_text("✅ 충전 기록 완료\n" + "\n".join(results))

async def _handle_sekuti_image(msg, img_b64: str, mime: str):
    data = await ocr_sekuti(img_b64, mime)
    if not data:
        await msg.reply_text("❌ 세큐티 등급 인식 실패.")
        return
    save_sekuti(data)
    score = data.get("total_score", "?")
    rank = data.get("rank_pct", "?")
    await msg.reply_text(
        f"✅ 세큐티 등급 저장\n"
        f"종합: {score}점 / 상위 {rank}%\n"
        f"안전: {data.get('safety_score','?')}점\n"
        f"실내공기: {data.get('air_score','?')}점\n"
        f"카카오T: {data.get('kakao_rating','?')}점"
    )

async def _handle_demand_image(msg, img_b64: str, mime: str):
    data = await ocr_demand(img_b64, mime)
    if not data:
        await msg.reply_text("❌ 수요지도 인식 실패.")
        return
    save_demand(data)
    zones = data.get("zones", [])
    zone_str = "\n".join(f"  {z['name']}: {z['level']}" for z in zones[:5])
    await msg.reply_text(f"✅ 수요지도 저장\n{data.get('time','?')} 기준\n{zone_str}")

async def _handle_payment_image(msg, img_b64: str, mime: str, ctx):
    """
    결제내역조회 OCR → pending_payment에 임시 저장
    이후 '대조' 명령으로 콜카드와 매칭하여 카카오T/배회 분리
    """
    try:
        items = await ocr_payment(img_b64, mime)
    except Exception as e:
        logger.error(f"결제내역 OCR 오류: {e}")
        await msg.reply_text("❌ 결제내역 OCR 오류. 다시 시도해 주세요.")
        return
    if not items:
        await msg.reply_text("❌ 결제내역 인식 실패. 다시 시도해 주세요.")
        return

    # ctx에 임시 저장 (대조 명령에서 사용)
    ctx.user_data["pending_payment"] = items

    kakao = [i for i in items if i["call_type"] == "카카오T"]
    baehoe = [i for i in items if i["call_type"] == "배회영업"]

    lines = [f"✅ 결제내역 인식완료 — {len(items)}건"]
    lines.append(f"  카카오T: {len(kakao)}건 / 배회영업: {len(baehoe)}건")
    lines.append("")
    lines.append("'대조' 명령으로 콜카드와 매칭하세요.")
    await msg.reply_text("\n".join(lines))

# ─────────────────────────────────────────────
# 17. 날짜 확인 콜백 핸들러
# ─────────────────────────────────────────────
@authorized
async def handle_date_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    pending = ctx.user_data.get("pending_ocr")
    if not pending:
        await query.edit_message_text("⏰ 세션 만료. 콜카드를 다시 전송해 주세요.")
        return

    if data == "date_cancel":
        ctx.user_data.pop("pending_ocr", None)
        await query.edit_message_text("❌ 저장 취소됨.")
        return

    if data == "date_manual":
        ctx.user_data["awaiting_date_input"] = True
        await query.edit_message_text(
            "날짜를 입력하세요 (형식: YYYY/MM/DD)\n예) 2026/03/16"
        )
        return

    if data.startswith("date_confirm:"):
        raw = data.replace("date_confirm:", "")
        parsed, status = parse_and_validate_date(raw)
        if status == "ok" and parsed:
            _save_pending_and_reply(query, ctx, parsed)
        else:
            await query.edit_message_text(f"날짜 오류: {raw}")

async def handle_date_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """직접 날짜 입력 처리"""
    if not ctx.user_data.get("awaiting_date_input"):
        return False

    raw = update.message.text.strip()
    parsed, status = parse_and_validate_date(raw)

    if status != "ok" or not parsed:
        await update.message.reply_text(
            f"❌ 날짜 형식 오류: {raw}\nYYYY/MM/DD 형식으로 입력하세요."
        )
        return True

    pending = ctx.user_data.get("pending_ocr")
    if not pending:
        await update.message.reply_text("⏰ 세션 만료.")
        return True

    ok = save_raw_call(
        parsed,
        pending["dispatch_time"],
        pending["origin"],
        pending["destination"],
        pending["amount"],
        pending["call_type"]
    )
    ctx.user_data.pop("pending_ocr", None)
    ctx.user_data.pop("awaiting_date_input", None)

    if ok:
        await update.message.reply_text(
            f"✅ 콜카드 저장완료 — {parsed.strftime('%m/%d')} ({weekday_str(parsed)})\n"
            f"💰 {fmt_money(pending['amount'])} | {pending['call_type']}"
        )
    else:
        await update.message.reply_text("❌ DB 저장 실패.")
    return True

def _save_pending_and_reply(query, ctx, d: date):
    pending = ctx.user_data.pop("pending_ocr", {})
    ok = save_raw_call(
        d,
        pending.get("dispatch_time", ""),
        pending.get("origin", ""),
        pending.get("destination", ""),
        pending.get("amount", 0),
        pending.get("call_type", "카카오T")
    )
    import asyncio
    if ok:
        asyncio.create_task(query.edit_message_text(
            f"✅ 저장완료 — {d.strftime('%m/%d')} ({weekday_str(d)})\n"
            f"💰 {fmt_money(pending.get('amount',0))} | {pending.get('call_type','카카오T')}"
        ))
    else:
        asyncio.create_task(query.edit_message_text("❌ DB 저장 실패."))

# ─────────────────────────────────────────────
# 18. 텍스트 명령어 핸들러
# ─────────────────────────────────────────────
@authorized
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # 날짜 직접 입력 처리
    if await handle_date_text_input(update, ctx):
        return

    # 수동 콜 입력: "콜 7800" 또는 "배회 5600"
    m = re.match(r"^(콜|배회)\s+(\d+)$", text)
    if m:
        call_type = "카카오T" if m.group(1) == "콜" else "배회영업"
        amount = int(m.group(2))
        save_pending_call(today_kst(), now_kst().strftime("%H:%M"), amount, call_type)
        await update.message.reply_text(
            f"📝 임시기록 저장\n{call_type} | {fmt_money(amount)}\n'확정' 명령으로 확정하세요."
        )
        return

    # 확정
    if text == "확정":
        matched   = ctx.user_data.get("matched_result", [])
        unmatched = ctx.user_data.get("unmatched_calls", [])

        # 대조 결과가 있으면 → 콜유형 업데이트 저장
        if matched or unmatched:
            today_d = today_kst()
            updated = 0

            for call, pay in matched:
                try:
                    supabase.table("raw_calls").update({
                        "콜유형": pay["call_type"],
                        "비고": pay.get("card","") or ""
                    }).eq("id", call["id"]).execute()
                    updated += 1
                except Exception as e:
                    logger.error(f"대조 확정 오류: {e}")

            for call in unmatched:
                try:
                    supabase.table("raw_calls").update({
                        "콜유형": "배회영업",
                        "비고": "결제내역 미매칭"
                    }).eq("id", call["id"]).execute()
                    updated += 1
                except Exception as e:
                    logger.error(f"배회 확정 오류: {e}")

            ctx.user_data.pop("matched_result", None)
            ctx.user_data.pop("unmatched_calls", None)
            ctx.user_data.pop("pending_payment", None)
            await update.message.reply_text(
                f"✅ 대조 확정 완료\n"
                f"  카카오T/배회 분류 업데이트: {updated}건"
            )
            return

        # 대조 없이 수동 임시기록 확정
        r = supabase.table("pending_calls").select("*").eq("확정", False).execute()
        pending = r.data or []
        if not pending:
            await update.message.reply_text("확정할 임시기록이 없습니다.")
            return
        for p in pending:
            save_raw_call(
                date.fromisoformat(p["날짜"]),
                p.get("배차시각", ""),
                "", "",
                p.get("요금", 0),
                p.get("콜유형", "카카오T")
            )
            supabase.table("pending_calls").update({"확정": True}).eq("id", p["id"]).execute()
        await update.message.reply_text(f"✅ {len(pending)}건 확정 저장 완료")
        return

    # 대조
    if text == "대조":
        payment_items = ctx.user_data.get("pending_payment")

        # 결제내역이 없으면 기존 임시기록 목록만 표시
        if not payment_items:
            r = supabase.table("pending_calls").select("*").eq("확정", False).execute()
            pending = r.data or []
            if not pending:
                await update.message.reply_text(
                    "임시기록 없음.\n\n"
                    "💡 결제내역 이미지를 먼저 올리면\n"
                    "콜카드와 자동 대조해 카카오T/배회 분리 저장합니다."
                )
                return
            lines = ["📋 임시기록 목록 (결제내역 없음 — 수동확인)"]
            for p in pending:
                lines.append(f"  {p['날짜']} {p.get('배차시각','')} | {fmt_money(p.get('요금',0))} | {p.get('콜유형','')}")
            await update.message.reply_text("\n".join(lines))
            return

        # 결제내역 있음 → 오늘 콜카드와 시각 기준 매칭
        today_d = today_kst()
        calls = get_today_calls(today_d)

        if not calls:
            await update.message.reply_text("오늘 저장된 콜카드 없음. 콜카드를 먼저 업로드하세요.")
            return

        matched = []
        unmatched_calls = []
        unmatched_payments = list(payment_items)

        for call in calls:
            call_time = call.get("배차시각","")
            best = None
            best_diff = 999

            for pay in unmatched_payments:
                pay_time = pay.get("time","")
                # 시각 차이 계산 (분 단위)
                try:
                    ch, cm = int(call_time.split(":")[0]), int(call_time.split(":")[1])
                    ph, pm = int(pay_time.split(":")[0]), int(pay_time.split(":")[1])
                    diff = abs((ch * 60 + cm) - (ph * 60 + pm))
                    if diff > 720: diff = 1440 - diff  # 자정 넘어가는 경우
                    if diff < best_diff:
                        best_diff = diff
                        best = pay
                except:
                    continue

            # 10분 이내 매칭
            if best and best_diff <= 10:
                matched.append((call, best))
                unmatched_payments.remove(best)
            else:
                unmatched_calls.append(call)

        # 결과 표시
        lines = [f"📋 대조 결과 — {today_d.strftime('%m/%d')}"]
        lines.append(f"콜카드 {len(calls)}건 vs 결제내역 {len(payment_items)}건")
        lines.append("")

        if matched:
            lines.append(f"✅ 매칭 {len(matched)}건:")
            for call, pay in matched:
                call_type = pay["call_type"]
                lines.append(
                    f"  {call.get('배차시각','')} {fmt_money(call.get('요금',0))} "
                    f"→ {call_type} "
                    f"({'카드: '+pay['card'] if pay.get('card') else '현금/배회'})"
                )

        if unmatched_calls:
            lines.append(f"\n⚠️ 미매칭 콜카드 {len(unmatched_calls)}건 (배회영업 가능성):")
            for call in unmatched_calls:
                lines.append(f"  {call.get('배차시각','')} {fmt_money(call.get('요금',0))}")

        lines.append("\n'확정' 명령으로 저장하세요.")
        ctx.user_data["matched_result"] = matched
        ctx.user_data["unmatched_calls"] = unmatched_calls
        await update.message.reply_text("\n".join(lines))
        return

    # 오늘
    if text in ("오늘",):
        s = get_today_summary()
        await update.message.reply_text(
            f"📊 오늘 ({today_kst().strftime('%m/%d')} {weekday_str(today_kst())})\n"
            f"건수: {s['count']}건 | 매출: {fmt_money(s['gross'])}\n"
            f"순수익: {fmt_money(s['net'])}\n"
            f"달성률: {round(s['net']/DAILY_TARGET*100,1)}%"
        )
        return

    # 오늘 마감
    if text in ("오늘 마감", "마감"):
        await update.message.reply_text("📋 마감 브리핑 생성 중...")
        briefing = await generate_closing_briefing()
        await update.message.reply_text(briefing)
        return

    # 이번 주
    if text in ("이번 주", "이번주"):
        w = get_week_summary()
        await update.message.reply_text(
            f"📅 이번 주 ({w['week_start'].strftime('%m/%d')}~{w['week_end'].strftime('%m/%d')})\n"
            f"운행일: {w['operated_days']}일 | {w['count']}건\n"
            f"매출: {fmt_money(w['total'])}\n"
            f"목표: {fmt_money(w['target'])} | 달성률: {w['achievement']}%\n"
            f"잔여: {fmt_money(w['remaining'])}"
        )
        return

    # 이번 달
    if text in ("이번 달", "이번달"):
        m = get_month_summary()
        await update.message.reply_text(
            f"📆 {m['month']}월 현황\n"
            f"운행일: {m['operated_days']}일 | {m['count']}건\n"
            f"매출: {fmt_money(m['total'])}\n"
            f"일평균: {fmt_money(m['avg_per_day'])}\n"
            f"건당: {fmt_money(m['avg_per_call'])}"
        )
        return

    # 지출 확인
    if text == "지출 확인":
        expenses = get_today_expenses()
        if not expenses:
            await update.message.reply_text("오늘 지출 내역 없음.")
        else:
            lines = ["💸 오늘 지출"]
            for e in expenses:
                lines.append(f"  {e['항목']}: {fmt_money(e['금액'])}")
            lines.append(f"합계: {fmt_money(sum(e['금액'] for e in expenses))}")
            await update.message.reply_text("\n".join(lines))
        return

    # 지출 입력: "타이어 50000" / "세차 8000" / "오일 35000"
    m = re.match(r"^(타이어|오일|세차|기타)\s+(\d+)(.*)$", text)
    if m:
        category, amount_str, note = m.group(1), m.group(2), m.group(3).strip()
        save_expense(today_kst(), category, int(amount_str), note)
        await update.message.reply_text(f"✅ 지출 저장: {category} {fmt_money(int(amount_str))}")
        return

    # 지출취소
    if text == "지출취소":
        r = supabase.table("expenses").select("id")\
            .eq("날짜", str(today_kst())).order("id", desc=True).limit(1).execute()
        if r.data:
            supabase.table("expenses").delete().eq("id", r.data[0]["id"]).execute()
            await update.message.reply_text("✅ 마지막 지출 삭제 완료")
        else:
            await update.message.reply_text("삭제할 지출 없음.")
        return

    # 휴무
    if text == "휴무":
        # daily_summary에 휴무 기록
        try:
            supabase.table("daily_summary").upsert({
                "날짜": str(today_kst()), "휴무": True
            }).execute()
        except:
            pass
        await update.message.reply_text(f"🏖️ {today_kst().strftime('%m/%d')} 휴무 처리 완료")
        return

    # 전략 / 오늘 전략 / 지금 전략
    if text in ("전략", "오늘 전략", "지금 전략"):
        strategy = await generate_strategy()
        await update.message.reply_text(f"🎯 지금 전략\n{strategy}")
        return

    # "지금 N시 위치"
    m = re.match(r"지금\s+(\d{1,2})시\s*위치", text)
    if m:
        hour = int(m.group(1))
        strategy = get_strategy(hour)
        if strategy:
            await update.message.reply_text(
                f"🕐 {hour}시 전략\n{strategy.get('strategy_text','데이터 없음')}"
            )
        else:
            await update.message.reply_text(f"{hour}시 전략 데이터 없음.")
        return

    # "공차 N분째"
    m = re.match(r"공차\s+(\d+)분", text)
    if m:
        minutes = int(m.group(1))
        if minutes >= 20:
            await update.message.reply_text(
                f"⚠️ 공차 {minutes}분 — 즉시 재진입 거점 이동!\n"
                f"▶ 수성구 황금·범어·고산동 (19~23시)\n"
                f"▶ 중구 성내2동 (23시 이후)"
            )
        else:
            await update.message.reply_text(
                f"공차 {minutes}분. 도착 후 2분30초 대기 — 연속콜 확보 우선."
            )
        return

    # "km N"
    m = re.match(r"^km\s+(\d+)$", text, re.IGNORECASE)
    if m:
        km = int(m.group(1))
        try:
            supabase.table("sekuti_weekly").update({"monthly_km": km})\
                .order("date", desc=True).limit(1).execute()
            await update.message.reply_text(f"✅ 월 주행거리 {km:,}km 업데이트")
        except Exception as e:
            await update.message.reply_text(f"업데이트 실패: {e}")
        return

    # DB 확인
    if text == "DB 확인":
        tables = ["raw_calls","daily_summary","expenses","charging_log",
                  "pending_calls","sekuti_weekly","strategy_lookup","pattern_table"]
        lines = ["🗄️ DB 현황"]
        for t in tables:
            try:
                r = supabase.table(t).select("id", count="exact").execute()
                lines.append(f"  {t}: {r.count}건")
            except:
                lines.append(f"  {t}: 조회불가")
        await update.message.reply_text("\n".join(lines))
        return

    # 오류 확인
    if text == "오류 확인":
        try:
            r = supabase.table("bot_log").select("*")\
                .eq("level", "ERROR").order("created_at", desc=True).limit(10).execute()
            if not r.data:
                await update.message.reply_text("최근 오류 없음 ✅")
            else:
                lines = ["🚨 최근 오류 (최대 10건)"]
                for row in r.data:
                    lines.append(f"  {row.get('created_at','')[:16]} {row.get('message','')}")
                await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"bot_log 조회 불가: {e}")
        return

    # 미인식 입력
    await update.message.reply_text(
        "❓ 명령을 인식하지 못했습니다.\n/help 로 명령어 목록을 확인하세요."
    )

# ─────────────────────────────────────────────
# 19. 기본 명령어
# ─────────────────────────────────────────────
@authorized
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚖 자비스 봇 v5.1 — 운행 중\n"
        f"현재 KST: {now_kst().strftime('%Y-%m-%d %H:%M')}\n"
        "/help 로 명령어 확인"
    )

@authorized
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")

@authorized
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    help_text = """📖 자비스 v5.1 명령어

📥 데이터 입력
  콜카드 이미지 → OCR 자동저장
  충전 이미지   → 충전 기록
  세큐티 이미지 → 등급 업데이트
  수요지도 이미지 → 수요 기록
  콜 7800      → 수동 임시기록
  배회 5600    → 배회 임시기록
  대조         → 임시기록 확인
  확정         → 임시기록 확정저장

💸 지출
  타이어/오일/세차 금액
  지출취소     → 마지막 삭제
  휴무         → 휴무 처리
  km N         → 주행거리 업데이트

📊 조회
  오늘         → 빠른 현황
  오늘 마감    → 전체 브리핑
  이번 주      → 주간 성과 (월요일 기준)
  이번 달      → 월간 성과
  지출 확인    → 오늘 지출
  DB 확인      → DB 현황
  오류 확인    → 에러 로그

⚡ 전략
  전략 / 오늘 전략 / 지금 전략
  지금 N시 위치
  공차 N분째

🎯 목표
  일 순수익: 100,000원
  주간 매출: 950,000원 (월~일)"""
    await update.message.reply_text(help_text)

# ─────────────────────────────────────────────
# 20. 스케줄러 — 보험료 자동 등록 (KST 19:00)
# ─────────────────────────────────────────────
async def auto_insurance():
    """매일 19:00 KST 보험료 자동 등록"""
    today = today_kst()
    # 이미 등록됐으면 스킵
    r = supabase.table("expenses").select("id")\
        .eq("날짜", str(today)).eq("항목", "보험료").execute()
    if r.data:
        return
    save_expense(today, "보험료", DAILY_INSURANCE, "자동등록")
    logger.info(f"보험료 자동등록: {today}")

# ─────────────────────────────────────────────
# 21. 헬스체크 엔드포인트
# ─────────────────────────────────────────────
async def health_check(scope, receive, send):
    if scope["type"] == "http":
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"text/plain"]],
        })
        await send({
            "type": "http.response.body",
            "body": b"OK",
        })

# ─────────────────────────────────────────────
# 22. 메인
# ─────────────────────────────────────────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def _run_health():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


async def post_init(application):
    """이벤트 루프 시작 후 스케줄러 + 헬스체크 초기화 (BUG-11 수정)"""
    threading.Thread(target=_run_health, daemon=True).start()
    scheduler = AsyncIOScheduler(timezone=KST)
    scheduler.add_job(auto_insurance, "cron", hour=19, minute=0)
    scheduler.start()
    logger.info(f"자비스 v5.1 시작 — KST {now_kst().strftime('%Y-%m-%d %H:%M')}")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_date_callback, pattern="^date_"))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.IMAGE, handle_image
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
