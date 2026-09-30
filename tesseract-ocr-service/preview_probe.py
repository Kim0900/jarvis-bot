"""One-image, read-only Task #164 probe, registered only on Render PR previews.

The public upload endpoint accepts only the exact Golden JPEG by SHA-256. It never
stores the image in the repository or database, and the OCR key stays on Render.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from flask import Response, redirect, request

GOLDEN_SHA256 = "c64bee34f1af960e0970e28fbb36fe7b84281f264c3f63922a0265954de59694"
MAX_IMAGE_BYTES = 1_500_000
PROBE_SCRIPT = Path(__file__).with_name("daily_history_overlay_poc.py")
_lock = threading.Lock()
_state = {"started": False, "token": None, "status": "READY", "result": None}
_access_token = secrets.token_urlsafe(32)
print(f"[TASK164_PREVIEW_ACCESS] {_access_token}", flush=True)


def _page(title: str, body: str, *, refresh: bool = False) -> Response:
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    response = Response(
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        + meta + '<title>' + html.escape(title) + '</title>'
        '<body style="font:16px/1.6 sans-serif;max-width:840px;margin:2rem auto;'
        'padding:0 1rem"><h1>' + html.escape(title) + '</h1>' + body + '</body></html>',
        mimetype="text/html",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _run_once(image_bytes: bytes) -> None:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name
        process = subprocess.run(
            [sys.executable, str(PROBE_SCRIPT), "--image", tmp_path,
             "--expected-count", "10", "--expected-sum", "70400",
             "--expected-direct-count", "1", "--expected-date", "2026-09-28",
             "--shadow-legacy", "--latency-probe-extra-cards", "6"],
            cwd=str(PROBE_SCRIPT.parent),
            env=os.environ.copy(),
            capture_output=True, text=True, timeout=240,
        )
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError:
            stderr_tail = (process.stderr or "")[-1800:]
            # Never echo secrets. Current subprocess does not print the API key, but
            # redact the variable name defensively and keep only a short traceback tail.
            stderr_tail = stderr_tail.replace("OCR_SPACE_API_KEY", "[REDACTED_ENV]")
            result = {
                "ok": False,
                "error_code": "PROBE_OUTPUT_INVALID",
                "exit_code": process.returncode,
                "stderr_tail": stderr_tail,
            }
        result["probe_exit_code"] = process.returncode
        safe_result = {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "error_code": result.get("error_code"),
            "date": result.get("date"),
            "header": result.get("header"),
            "detected_card_count": result.get("detected_card_count"),
            "observed_fare_sum": result.get("observed_fare_sum"),
            "observed_direct_count": result.get("observed_direct_count"),
            "actual_total_ocr_calls": result.get("actual_total_ocr_calls"),
            "ocrspace_duration_ms": result.get("ocrspace_duration_ms"),
            "ocr_call_wall_ms": result.get("ocr_call_wall_ms"),
            "total_wall_duration_ms": result.get("total_wall_duration_ms"),
            "diagnostic_latency_probe_extra_calls": result.get("diagnostic_latency_probe_extra_calls"),
            "legacy_shadow": result.get("legacy_shadow"),
            "probe_exit_code": result.get("probe_exit_code"),
            "stderr_tail": result.get("stderr_tail"),
        }
        print("[TASK164_PREVIEW_RESULT] " + json.dumps(
            safe_result, ensure_ascii=False, separators=(",", ":")
        ), flush=True)
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error_code": "PROBE_TIMEOUT_240S"}
    except Exception as exc:
        result = {"ok": False, "error_code": "PROBE_EXCEPTION",
                  "exception_type": type(exc).__name__}
    finally:
        if tmp_path:
            os.unlink(tmp_path)
    with _lock:
        _state["result"] = result
        _state["status"] = "DONE"


def register_preview_probe(app) -> None:
    if os.getenv("IS_PULL_REQUEST", "").lower() != "true":
        return

    @app.route("/task164-probe", methods=["GET", "POST"])
    def task164_probe():
        supplied = request.args.get("access") if request.method == "GET" else request.form.get("access")
        if not secrets.compare_digest(supplied or "", _access_token):
            return _page("찾을 수 없음", "<p>검증 링크가 올바르지 않습니다.</p>"), 404
        if request.method == "GET":
            return _page(
                "Task #164 읽기 전용 검증",
                "<p>9/28 Golden 원본 JPEG만 받습니다. OCR 키는 서버 내부에 유지되고 "
                "운영 DB·Drive에는 기록하지 않습니다.</p>"
                '<form method="post" enctype="multipart/form-data">'
                '<input type="hidden" name="access" value="' + _access_token + '">'
                '<input type="file" name="image" accept="image/jpeg" required>'
                '<button type="submit">검증 실행</button></form>',
            )
        file = request.files.get("image")
        if file is None:
            return _page("입력 오류", "<p>JPEG 파일이 없습니다.</p>"), 400
        image_bytes = file.read(MAX_IMAGE_BYTES + 1)
        if (len(image_bytes) > MAX_IMAGE_BYTES or
                hashlib.sha256(image_bytes).hexdigest() != GOLDEN_SHA256):
            return _page("입력 오류", "<p>Golden 원본과 일치하지 않습니다.</p>"), 400
        with _lock:
            if _state["started"]:
                return _page("이미 실행됨", "<p>검증은 한 번만 실행됩니다.</p>"), 409
            _state["started"] = True
            _state["status"] = "RUNNING"
            _state["token"] = secrets.token_urlsafe(32)
            token = _state["token"]
        threading.Thread(target=_run_once, args=(image_bytes,), daemon=True).start()
        return redirect("/task164-probe/result/" + token, code=303)

    @app.route("/task164-probe/result/<token>", methods=["GET"])
    def task164_probe_result(token: str):
        with _lock:
            if not secrets.compare_digest(token, _state["token"] or ""):
                return _page("찾을 수 없음", "<p>결과 링크가 올바르지 않습니다.</p>"), 404
            status = _state["status"]
            result = _state["result"]
        if status != "DONE":
            return _page("검증 진행 중", "<p>OCR 응답을 기다리는 중입니다.</p>",
                         refresh=True)
        return _page(
            "검증 결과",
            "<p>결과에 주소가 포함될 수 있습니다. 링크를 공개하지 마십시오.</p>"
            '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">'
            + html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            + "</pre>",
        )
