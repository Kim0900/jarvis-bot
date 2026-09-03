"""
task93(2026-09-03) 3단계 — MAGI DATA CORE 비LLM OCR 서비스.
마기 재정정 결론: Tesseract + Render 신규 Docker서비스(무료티어),
비용 $0, 카드등록 불필요. Google Cloud Vision(카드등록필요)은 폐기.

이 서비스는 순수 문자인식(TEXT_DETECTION)만 수행 — AI 판단/해석/
분류 없음. 이미지→텍스트 변환까지만 하고, 실제 필드 파싱(정규식)은
jarvis-bot(bot_v5_legacy.py)의 parse_kakao_trip_detail() 등에서
별도로 수행한다(관심사 분리).
"""
import base64
import os
from io import BytesIO

import pytesseract
from flask import Flask, jsonify, request
from PIL import Image

app = Flask(__name__)

MCP_KEY = os.getenv("OCR_MCP_KEY")


def _check_auth():
    if not MCP_KEY:
        return False, "OCR_MCP_KEY 서버환경변수 미설정"
    key = request.headers.get("X-MCP-Key")
    if key != MCP_KEY:
        return False, "인증 실패"
    return True, ""


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "engine": "tesseract", "service": "jarvis-ocr-tesseract"})


@app.route("/ocr", methods=["POST"])
def ocr():
    ok, err = _check_auth()
    if not ok:
        return jsonify({"success": False, "error": err}), 401

    try:
        payload = request.get_json(force=True, silent=True) or {}
        image_b64 = payload.get("image_base64")
        if not image_b64:
            return jsonify({"success": False, "error": "image_base64 필드 필요"}), 400

        lang = payload.get("lang", "kor+eng")
        image_bytes = base64.b64decode(image_b64)
        img = Image.open(BytesIO(image_bytes))

        text = pytesseract.image_to_string(img, lang=lang)
        return jsonify({"success": True, "text": text, "engine": "tesseract", "lang": lang})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
