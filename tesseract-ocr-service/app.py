"""
task93(2026-09-03) 3단계 — MAGI DATA CORE 비LLM OCR 서비스.
마기 재정정 결론: Tesseract + Render 신규 Docker서비스(무료티어),
비용 $0, 카드등록 불필요. Google Cloud Vision(카드등록필요)은 폐기.

2026-09-16 3차(대표님 지시 "유료플랜은 최후수단, 병목 잡을 방법
우선 검토") — 실제 Render 재현으로 확인된 병목 원인: 공식 스펙상
0.1 vCPU인 Render Free 플랜에서 Tesseract(LSTM 신경망 엔진, CPU
집약적)를 구동하는 것 자체. 이미지 축소/분할제거로는 처리시간이
거의 안 줄어(26.66s→26.46s) 확인. Legacy(OEM0) 엔진 전환도 설치된
언어팩이 LSTM 전용이라 불가.

대안으로 OCR.space 무료 API(카드등록 불필요, 월 25,000건, 자체
서버에서 처리 — Render의 CPU 제약을 완전히 우회) 도입. 안정성을
위해 이중화 구조: OCR.space 우선 시도 → 실패(네트워크오류/키
미설정/API장애/월한도초과 등)시 기존 로컬 Tesseract로 자동
폴백(무료라 유지비용 없음, 최후의 안전망 역할).
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO

import pytesseract
from flask import Flask, jsonify, request
from PIL import Image

app = Flask(__name__)

MCP_KEY = os.getenv("OCR_MCP_KEY")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY")
OCR_SPACE_URL = "https://api.ocr.space/parse/image"
OCR_SPACE_TIMEOUT_SEC = 25  # Gunicorn worker timeout(60s)보다 충분히 작게

TESSERACT_CONFIG = "--psm 6"
MAX_WIDTH = 800
TESSERACT_TIMEOUT_SEC = 25


def _check_auth():
    if not MCP_KEY:
        return False, "OCR_MCP_KEY 서버환경변수 미설정"
    key = request.headers.get("X-MCP-Key")
    if key != MCP_KEY:
        return False, "인증 실패"
    return True, ""


def _resize_if_needed(img: Image.Image) -> Image.Image:
    if img.width <= MAX_WIDTH:
        return img
    scale = MAX_WIDTH / img.width
    new_size = (MAX_WIDTH, max(1, int(img.height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def _ocr_space_recognize(image_bytes: bytes, lang: str, timeout_sec: int = None) -> tuple:
    """OCR.space 무료 API 호출. 2026-09-16 도입 — Render CPU 병목을
    완전히 우회(처리가 OCR.space 자체 서버에서 일어남). 실패시
    (None, 사유)를 반환해 호출측이 로컬 Tesseract로 폴백하게 한다."""
    if not OCR_SPACE_API_KEY:
        return None, "OCR_SPACE_API_KEY 미설정"

    # OCR.space는 language 코드가 하나(3글자)만 필요 — 한글 문서가
    # 주력이므로 kor 고정. 영숫자(날짜/금액/km 등)는 대부분 언어팩이
    # 기본 라틴문자와 함께 인식한다.
    ocr_lang = "kor" if "kor" in lang else "eng"
    b64 = base64.b64encode(image_bytes).decode()
    payload = urllib.parse.urlencode({
        "base64Image": f"data:image/jpeg;base64,{b64}",
        "language": ocr_lang,
        "isOverlayRequired": "false",
        "OCREngine": "2",
        "scale": "true",
    }).encode()

    req = urllib.request.Request(
        OCR_SPACE_URL, data=payload,
        headers={"apikey": OCR_SPACE_API_KEY,
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=(timeout_sec or OCR_SPACE_TIMEOUT_SEC)) as resp:
            result = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"OCR.space 네트워크오류: {e}"
    except Exception as e:
        return None, f"OCR.space 응답파싱오류: {e}"

    if result.get("IsErroredOnProcessing"):
        return None, f"OCR.space 처리오류: {result.get('ErrorMessage')}"
    parsed_list = result.get("ParsedResults") or []
    if not parsed_list:
        return None, "OCR.space 결과없음"
    parsed = parsed_list[0]
    if str(parsed.get("FileParseExitCode")) != "1":
        return None, f"OCR.space 파싱실패: {parsed.get('ErrorMessage')}"

    text = parsed.get("ParsedText") or ""
    meta = {"engine": "ocrspace", "ocrspace_duration_ms": result.get("ProcessingTimeInMilliseconds")}
    return text, meta


TALL_IMAGE_RATIO_THRESHOLD = 4.0
VERY_TALL_IMAGE_RATIO_THRESHOLD = 6.0
SPLIT_OVERLAP_RATIO_OF_CHUNK = 0.20
SPLIT2_OCR_TIMEOUT_SEC = 12
SPLIT4_OCR_TIMEOUT_SEC = 7


def _image_to_jpeg_bytes(img: Image.Image) -> bytes:
    resized = _resize_if_needed(img)
    buf = BytesIO()
    resized.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _split_vertical_chunks(img: Image.Image, parts: int) -> list:
    """세로 이미지를 parts개로 나누되 인접 조각끼리 경계가 겹치게 한다.

    overlap은 각 nominal chunk 높이의 20%다.
    - 2분할: 전체 이미지 기준 10% overlap → 과거 검증값(0~55%,45~100%)과 동일
    - 4분할: 인접 조각마다 전체 높이 약 5% overlap
    """
    if parts < 2:
        return [img]

    h = img.height
    step = h / float(parts)
    overlap = step * SPLIT_OVERLAP_RATIO_OF_CHUNK
    half_overlap = overlap / 2.0
    chunks = []

    for i in range(parts):
        base_start = i * step
        base_end = (i + 1) * step
        start_y = 0 if i == 0 else max(0, int(round(base_start - half_overlap)))
        end_y = h if i == parts - 1 else min(h, int(round(base_end + half_overlap)))
        if end_y <= start_y:
            continue
        chunks.append(img.crop((0, start_y, img.width, end_y)))
    return chunks


def _ocr_split_chunks(img: Image.Image, lang: str, parts: int, timeout_sec: int) -> tuple:
    chunks = _split_vertical_chunks(img, parts)
    texts = []
    metas = []
    processed_sizes = []

    for index, chunk in enumerate(chunks, start=1):
        resized = _resize_if_needed(chunk)
        processed_sizes.append([resized.width, resized.height])
        text, meta = _ocr_space_recognize(
            _image_to_jpeg_bytes(chunk),
            lang,
            timeout_sec=timeout_sec,
        )
        if text is None:
            return None, {
                "failed_chunk": index,
                "failed_reason": meta,
                "processed_sizes": processed_sizes,
            }
        texts.append(f"---MAGI_OCR_CHUNK_{index}---\n{text.strip()}")
        metas.append(meta or {})

    return "\n".join(texts), {
        "processed_sizes": processed_sizes,
        "durations_ms": [m.get("ocrspace_duration_ms") for m in metas],
    }


def smart_ocr(img: Image.Image, lang: str) -> tuple:
    """OCR.space 우선 → 실패시 로컬 Tesseract 폴백.

    task#164(2026-09-29):
    - 세로비 >= 6.0: 4분할
    - 세로비 >= 4.0: 2분할
    - 그 미만: 단일 OCR
    overlap 중복은 호출측 daily_history parser가 time-key로 제거한다.
    """
    meta = {"orig_size": [img.width, img.height]}
    aspect_ratio = (img.height / img.width) if img.width else 0.0

    split_parts = 0
    split_timeout = None
    if aspect_ratio >= VERY_TALL_IMAGE_RATIO_THRESHOLD:
        split_parts = 4
        split_timeout = SPLIT4_OCR_TIMEOUT_SEC
    elif aspect_ratio >= TALL_IMAGE_RATIO_THRESHOLD:
        split_parts = 2
        split_timeout = SPLIT2_OCR_TIMEOUT_SEC

    if split_parts:
        text, split_meta = _ocr_split_chunks(
            img, lang, parts=split_parts, timeout_sec=split_timeout
        )
        if text is not None:
            meta["engine"] = f"ocrspace_split{split_parts}"
            meta["fallback_used"] = False
            meta["split_used"] = True
            meta["split_parts"] = split_parts
            meta["split_overlap_ratio_of_chunk"] = SPLIT_OVERLAP_RATIO_OF_CHUNK
            meta["processed_size"] = split_meta.get("processed_sizes")
            meta["ocrspace_duration_ms"] = split_meta.get("durations_ms")
            return text, meta

        # 조각 하나라도 실패하면 OCR.space를 추가 재호출하지 않고
        # full-image local Tesseract 1회로 폴백해 worker 60초 상한을 지킨다.
        meta["ocrspace_failed_reason"] = split_meta
        meta["split_used"] = True
        meta["split_parts"] = split_parts
        resized = _resize_if_needed(img)
        meta["processed_size"] = [resized.width, resized.height]
        meta["fallback_used"] = True
        meta["engine"] = "tesseract_local_after_split_failure"
        text = pytesseract.image_to_string(
            resized, lang=lang, config=TESSERACT_CONFIG, timeout=TESSERACT_TIMEOUT_SEC)
        return text, meta

    resized = _resize_if_needed(img)
    meta["processed_size"] = [resized.width, resized.height]
    image_bytes = _image_to_jpeg_bytes(img)

    text, ocrspace_meta = _ocr_space_recognize(image_bytes, lang)
    if text is not None:
        meta.update(ocrspace_meta)
        meta["fallback_used"] = False
        meta["split_used"] = False
        meta["split_parts"] = 1
        return text, meta

    meta["ocrspace_failed_reason"] = ocrspace_meta
    meta["fallback_used"] = True
    meta["engine"] = "tesseract_local"
    meta["split_used"] = False
    meta["split_parts"] = 1
    text = pytesseract.image_to_string(
        resized, lang=lang, config=TESSERACT_CONFIG, timeout=TESSERACT_TIMEOUT_SEC)
    return text, meta


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok", "service": "jarvis-ocr-tesseract",
        "ocrspace_configured": bool(OCR_SPACE_API_KEY)
    })


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
        print(f"[OCR_OK] duration_ms={duration_ms} engine={meta.get('engine')} "
              f"fallback={meta.get('fallback_used')} split={meta.get('split_used')} "
              f"parts={meta.get('split_parts')} orig={meta['orig_size']} "
              f"processed={meta['processed_size']} text_len={len(text)}", flush=True)
        return jsonify({
            "success": True, "text": text, "lang": lang,
            "duration_ms": duration_ms, "engine": meta.get("engine"),
            "fallback_used": meta.get("fallback_used"),
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
