"""
task93(2026-09-03) 3단계 — MAGI DATA CORE 비LLM OCR 서비스.
마기 재정정 결론: Tesseract + Render 신규 Docker서비스(무료티어),
비용 $0, 카드등록 불필요. Google Cloud Vision(카드등록필요)은 폐기.

이 서비스는 순수 문자인식(TEXT_DETECTION)만 수행 — AI 판단/해석/
분류 없음. 이미지→텍스트 변환까지만 하고, 실제 필드 파싱(정규식)은
jarvis-bot(bot_v5_legacy.py)의 parse_kakao_trip_detail() 등에서
별도로 수행한다(관심사 분리).

2026-09-05 실측검증(1차): 기본 PSM(3)이 카카오T "일별운행이력"
스크린샷류 레이아웃에서 특정 텍스트블록을 통째로 건너뛰는 문제를
실제 이미지로 재현확인 → PSM 6("균일 텍스트 블록 가정")으로 교체.
그 시점 추가 안전장치로 긴 이미지(세로/가로>2.5) 자동 2분할을
도입했음(당시는 resize 없이 원본 그대로 처리).

2026-09-11 CASPER_콜카드_파일명비의존_판별_OCR_TIMEOUT 작업지시서
반영(2차): 실제 우버 이미지에서 Gunicorn worker timeout(60s) 재현
확인. 1차 수정(resize 900px+분할유지+timeout25s)을 실제 Render에
배포해 재현측정한 결과 26.66초로 여전히 근접초과 — Render Free
플랜 CPU가 로컬 재현환경보다 대폭 느림을 실측 확인.

이어서 분할 로직 자체의 필요성을 재검증: resize 후에는 분할(2회
호출)이 비분할(1회 호출)보다 항상 25~30% 느리면서 정확도는
동일함을 실측 확인(우버 샘플, 그리고 원 세로8423px 카카오 "일별
운행이력" 11건 전체 리스트로도 비분할+800px에서 전건 정확 추출
확인 — 2026-09-05 분할 도입 당시는 resize 없이 원본 그대로였던
것이 원인으로, PSM6+적정resize 조합에서는 분할이 더 이상 필요
없음). 따라서 분할 로직을 제거하고 MAX_WIDTH를 900→800px로
조정(우버 샘플 750px부터 전필드 정확, 800px로 안전마진 확보).
"""
import base64
import os
import time
from io import BytesIO

import pytesseract
from flask import Flask, jsonify, request
from PIL import Image

app = Flask(__name__)

MCP_KEY = os.getenv("OCR_MCP_KEY")
TESSERACT_CONFIG = "--psm 6"
MAX_WIDTH = 800          # 2026-09-11 2차: 750px부터 전필드 정확 확인, 안전마진 800px
TESSERACT_TIMEOUT_SEC = 25  # Gunicorn worker timeout(60s)보다 충분히 작게


def _check_auth():
    if not MCP_KEY:
        return False, "OCR_MCP_KEY 서버환경변수 미설정"
    key = request.headers.get("X-MCP-Key")
    if key != MCP_KEY:
        return False, "인증 실패"
    return True, ""


def _resize_if_needed(img: Image.Image) -> Image.Image:
    """폭이 MAX_WIDTH를 넘으면 비율유지 축소."""
    if img.width <= MAX_WIDTH:
        return img
    scale = MAX_WIDTH / img.width
    new_size = (MAX_WIDTH, max(1, int(img.height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def smart_ocr(img: Image.Image, lang: str) -> tuple:
    """2026-09-11 2차 실측검증된 OCR 전략. resize(800px상한) + PSM6
    + 단일 image_to_string 호출(분할 제거 — 비분할이 항상 더 빠르고
    정확도 동일함을 실측확인) + Tesseract 자체 timeout."""
    meta = {"orig_size": [img.width, img.height]}
    img = _resize_if_needed(img)
    meta["processed_size"] = [img.width, img.height]
    meta["split"] = False  # 2026-09-11 2차: 분할 로직 제거

    text = pytesseract.image_to_string(
        img, lang=lang, config=TESSERACT_CONFIG, timeout=TESSERACT_TIMEOUT_SEC)
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
        print(f"[OCR_OK] duration_ms={duration_ms} orig={meta['orig_size']} "
              f"processed={meta['processed_size']} text_len={len(text)}", flush=True)
        return jsonify({
            "success": True, "text": text, "engine": "tesseract", "lang": lang,
            "duration_ms": duration_ms,
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
