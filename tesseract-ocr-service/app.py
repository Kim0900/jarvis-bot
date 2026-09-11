"""
task93(2026-09-03) 3단계 — MAGI DATA CORE 비LLM OCR 서비스.
마기 재정정 결론: Tesseract + Render 신규 Docker서비스(무료티어),
비용 $0, 카드등록 불필요. Google Cloud Vision(카드등록필요)은 폐기.

이 서비스는 순수 문자인식(TEXT_DETECTION)만 수행 — AI 판단/해석/
분류 없음. 이미지→텍스트 변환까지만 하고, 실제 필드 파싱(정규식)은
jarvis-bot(bot_v5_legacy.py)의 parse_kakao_trip_detail() 등에서
별도로 수행한다(관심사 분리).

2026-09-05 실측검증 기반 개선(대표님 "완벽 구현" 지시):
①기본 PSM(3)이 카카오T "일별운행이력" 스크린샷류 레이아웃에서 특정
텍스트블록을 통째로 건너뛰는 문제를 실제 이미지로 재현확인(예:
"23:34-23:48/동인동/방촌동" 항목 완전누락) → PSM 6("균일 텍스트
블록 가정")으로 교체, 동일 이미지 재현시 전건 정확 추출 확인.
②매우 긴 세로스크롤 이미지(세로/가로 2.5배 초과)는 안전장치로
자동 2분할(0~55%/45~100%, 10%겹침) 후 각각 OCR, 텍스트 연결.

2026-09-11 CASPER_콜카드_파일명비의존_판별_OCR_TIMEOUT 작업지시서 반영:
실제 우버 이미지(1080x2929/1080x3619, 세로/가로>2.5라 분할대상)에서
Gunicorn worker timeout(60s) 재현 확인. 자체 재현측정(빠른 CPU
환경) 결과 원본 그대로도 처리시간은 5초 내외였으나, Render Free
플랜은 CPU가 크게 제한적이라 동일 작업이 훨씬 오래 걸릴 수 있음.
- 폭 900px 초과시 비율유지 축소
- pytesseract 자체 timeout 추가
- 구조화 로그 추가

2026-09-11 YOUNGSIL 운영실측 최소복구 1차:
Render Free(0.15 CPU) 실제 운영에서 900px 분할 OCR이 25초 timeout을
초과하는 것을 확인. 정상 입력 정확도 경로를 보존하기 위해 전역 해상도를
낮추지 않고, 각 OCR 조각마다 900px 14초 우선 → timeout 조각만 600px
12초 fallback 1회로 제한했다.

2026-09-11 YOUNGSIL 운영실측 최소복구 2차:
실제 실패 샘플(1080x2929)에서 전체 900px OCR은 우버 날짜줄의 AM/PM을
간헐적으로 손실했지만 원본 상단 소영역 OCR은 AM/PM을 안정적으로 복원했다.
파서에 시간대를 추정하는 규칙을 넣지 않고, 긴 이미지에서 전체 OCR 결과에
날짜+AM/PM 패턴이 없을 때만 원본 해상도 상단 헤더를 최대 5초 보조 OCR해
텍스트 앞에 붙인다. 정상적으로 날짜+AM/PM이 읽힌 요청은 추가 OCR을 하지 않는다.
"""
import base64
import os
import re
import time
from io import BytesIO

import pytesseract
from flask import Flask, jsonify, request
from PIL import Image

app = Flask(__name__)

MCP_KEY = os.getenv("OCR_MCP_KEY")
TESSERACT_CONFIG = "--psm 6"
MAX_WIDTH = 900
PRIMARY_TIMEOUT_SEC = 14
FALLBACK_WIDTH = 600
FALLBACK_TIMEOUT_SEC = 12
HEADER_TIMEOUT_SEC = 5
DATE_WITH_MERIDIEM_RE = re.compile(
    r'\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.\S*\s*(?:AM|PM)\s*\d{1,2}:\d{2}',
    re.IGNORECASE,
)


def _check_auth():
    if not MCP_KEY:
        return False, "OCR_MCP_KEY 서버환경변수 미설정"
    key = request.headers.get("X-MCP-Key")
    if key != MCP_KEY:
        return False, "인증 실패"
    return True, ""


def _resize_to_width(img: Image.Image, max_width: int) -> Image.Image:
    """폭이 max_width를 넘으면 비율을 유지한 채 축소한다."""
    if img.width <= max_width:
        return img
    scale = max_width / img.width
    new_size = (max_width, max(1, int(img.height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def _resize_if_needed(img: Image.Image) -> Image.Image:
    """기본 OCR 경로는 기존 검증값인 900px 상한을 유지한다."""
    return _resize_to_width(img, MAX_WIDTH)


def _ocr_chunk_with_fallback(img: Image.Image, lang: str, chunk_name: str, meta: dict) -> str:
    """기존 900px OCR을 우선하고, timeout 난 조각에만 600px 1회 fallback."""
    try:
        return pytesseract.image_to_string(
            img, lang=lang, config=TESSERACT_CONFIG, timeout=PRIMARY_TIMEOUT_SEC)
    except RuntimeError as primary_error:
        fallback_img = _resize_to_width(img, FALLBACK_WIDTH)
        meta["fallback_used"] = True
        meta.setdefault("fallback_chunks", []).append(chunk_name)
        print(
            f"[OCR_FALLBACK] chunk={chunk_name} primary_timeout={PRIMARY_TIMEOUT_SEC}s "
            f"primary_size={[img.width, img.height]} "
            f"fallback_size={[fallback_img.width, fallback_img.height]} "
            f"error={primary_error}",
            flush=True,
        )
        return pytesseract.image_to_string(
            fallback_img,
            lang=lang,
            config=TESSERACT_CONFIG,
            timeout=FALLBACK_TIMEOUT_SEC,
        )


def _maybe_prepend_original_header(orig_img: Image.Image, text: str, lang: str, meta: dict) -> str:
    """긴 화면에서 전체 OCR이 날짜+AM/PM을 놓친 경우에만 원본 상단을 보조 OCR한다.

    시간대를 추정하지 않는다. 보조 OCR 자체가 실패/timeout하면 기존 OCR 텍스트를
    그대로 반환해 기존 경로를 깨뜨리지 않는다.
    """
    if not meta.get("split") or DATE_WITH_MERIDIEM_RE.search(text):
        return text

    # 실제 1080x2929 우버 샘플 기준 상단 앱바+날짜/금액 영역만 포함.
    # 비율로 제한해 다른 긴 이미지에서도 전체를 다시 OCR하지 않도록 한다.
    header_top = max(0, int(orig_img.height * 0.04))
    header_bottom = min(orig_img.height, max(header_top + 1, int(orig_img.height * 0.24)))
    header = orig_img.crop((0, header_top, orig_img.width, header_bottom))
    try:
        header_text = pytesseract.image_to_string(
            header, lang=lang, config=TESSERACT_CONFIG, timeout=HEADER_TIMEOUT_SEC)
        if DATE_WITH_MERIDIEM_RE.search(header_text):
            meta["header_ocr_used"] = True
            print(
                f"[OCR_HEADER_RECOVERY] header_size={[header.width, header.height]} "
                f"text_len={len(header_text)}",
                flush=True,
            )
            return header_text + "\n" + text
        meta["header_ocr_attempted"] = True
    except RuntimeError as e:
        meta["header_ocr_timeout"] = True
        print(f"[OCR_HEADER_TIMEOUT] error={e}", flush=True)
    except Exception as e:
        meta["header_ocr_error"] = True
        print(f"[OCR_HEADER_ERROR] error={e}", flush=True)
    return text


def smart_ocr(img: Image.Image, lang: str) -> tuple:
    """PSM 6 + 900px 기본 resize + 긴 이미지 2분할 + timeout fallback.

    전체 OCR 이후 날짜+AM/PM이 사라진 긴 화면에 한해서만 원본 상단 소영역을
    추가 OCR한다. 반환: (텍스트, 처리단계별 메타정보 dict)
    """
    orig_img = img
    meta = {
        "orig_size": [img.width, img.height],
        "fallback_used": False,
        "header_ocr_used": False,
    }
    img = _resize_if_needed(img)
    meta["processed_size"] = [img.width, img.height]

    w, h = img.width, img.height
    ratio = h / max(w, 1)
    meta["split"] = ratio > 2.5

    if meta["split"]:
        top = img.crop((0, 0, w, int(h * 0.55)))
        bottom = img.crop((0, int(h * 0.45), w, h))
        text_top = _ocr_chunk_with_fallback(top, lang, "top", meta)
        text_bottom = _ocr_chunk_with_fallback(bottom, lang, "bottom", meta)
        text = text_top + "\n" + text_bottom
    else:
        text = _ocr_chunk_with_fallback(img, lang, "full", meta)

    text = _maybe_prepend_original_header(orig_img, text, lang, meta)
    return text, meta


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "engine": "tesseract", "service": "jarvis-ocr-tesseract"})


@app.route("/ocr", methods=["POST"])
def ocr():
    ok, err = _check_auth()
    if not ok:
        return jsonify({"success": False, "error": err, "error_code": "AUTH_FAILED"}), 401

    t_start = time.time()
    try:
        payload = request.get_json(force=True, silent=True) or {}
        image_b64 = payload.get("image_base64")
        if not image_b64:
            return jsonify({"success": False, "error": "image_base64 필드 필요",
                             "error_code": "BAD_REQUEST", "error_stage": "input"}), 400

        lang = payload.get("lang", "kor+eng")
        image_bytes = base64.b64decode(image_b64)
        img = Image.open(BytesIO(image_bytes))

        text, meta = smart_ocr(img, lang)
        duration_ms = int((time.time() - t_start) * 1000)
        print(
            f"[OCR_OK] duration_ms={duration_ms} orig={meta['orig_size']} "
            f"processed={meta['processed_size']} split={meta['split']} "
            f"fallback_used={meta['fallback_used']} "
            f"fallback_chunks={meta.get('fallback_chunks', [])} "
            f"header_ocr_used={meta['header_ocr_used']} "
            f"text_len={len(text)}",
            flush=True,
        )
        return jsonify({
            "success": True,
            "text": text,
            "engine": "tesseract",
            "lang": lang,
            "duration_ms": duration_ms,
            "fallback_used": meta["fallback_used"],
            "header_ocr_used": meta["header_ocr_used"],
        })
    except RuntimeError as e:
        duration_ms = int((time.time() - t_start) * 1000)
        print(f"[OCR_TIMEOUT] duration_ms={duration_ms} error={e}", flush=True)
        return jsonify({"success": False, "error": "OCR 처리시간 초과",
                         "error_code": "OCR_TIMEOUT", "error_stage": "OCR",
                         "duration_ms": duration_ms}), 400
    except Exception as e:
        duration_ms = int((time.time() - t_start) * 1000)
        print(f"[OCR_ERROR] duration_ms={duration_ms} error={e}", flush=True)
        return jsonify({"success": False, "error": str(e),
                         "error_code": "OCR_ERROR", "error_stage": "OCR",
                         "duration_ms": duration_ms}), 400


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
