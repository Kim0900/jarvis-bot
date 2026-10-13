"""Minimal in-process PostgREST stand-in for isolated integration tests (Task #194).

Only what the scheduler jobs need: GET with eq/gte/lte/lt/in filters, ``select``,
``order``, ``limit``; POST insert; POST upsert (``on_conflict`` + merge-duplicates);
PATCH.  Failure injection is switchable at runtime so the real ``sb_h`` HTTP stack and
the real job code can be exercised against an "outage".  No credentials, no network
beyond 127.0.0.1.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse


class FakePostgREST:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}
        self.fail_mode: str | None = None  # None | "503" | "reset"
        self.fail_tables: set[str] | None = None  # None = every table
        self.fail_methods: set[str] | None = None  # None = every method
        # GET answers that are HTTP 200 but untrustworthy: None | "empty" | "badjson" | "dict"
        self.get_quirk: str | None = None
        # {table: substring}: a POST to `table` whose body contains the substring answers 503
        self.fail_body_contains: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _failing(self, table: str, method: str) -> bool:
                if outer.fail_mode is None:
                    return False
                if outer.fail_tables is not None and table not in outer.fail_tables:
                    return False
                if outer.fail_methods is not None and method not in outer.fail_methods:
                    return False
                return True

            def _send(self, status: int, body) -> None:
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                url = urlparse(self.path)
                table = url.path.rsplit("/", 1)[-1]
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.requests.append((method, table))
                    if self._failing(table, method):
                        if outer.fail_mode == "reset":
                            self.connection.close()
                            return
                        self._send(503, {"message": "PGRST002 schema cache unavailable"})
                        return
                    needle = outer.fail_body_contains.get(table)
                    if needle and method == "POST" and needle.encode() in body:
                        self._send(503, {"message": "injected per-row write failure"})
                        return
                    params = parse_qsl(url.query, keep_blank_values=True)
                    rows = outer.tables.setdefault(table, [])
                    if method == "GET":
                        quirk = outer.get_quirk
                        if quirk and (outer.fail_tables is None or table in outer.fail_tables):
                            raw = {"empty": b"", "badjson": b"<html>proxy</html>", "dict": b"{}"}[quirk]
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(raw)))
                            self.end_headers()
                            self.wfile.write(raw)
                            return
                        self._send(200, outer._select(rows, params))
                    elif method == "POST":
                        data = json.loads(body or b"{}")
                        items = data if isinstance(data, list) else [data]
                        qp = dict(params)
                        conflict = qp.get("on_conflict")
                        prefer = self.headers.get("Prefer", "")
                        out = []
                        for item in items:
                            if conflict and "merge-duplicates" in prefer:
                                keys = conflict.split(",")
                                hit = next((r for r in rows if all(r.get(k) == item.get(k) for k in keys)), None)
                                if hit is not None:
                                    hit.update(item)
                                    out.append(dict(hit))
                                    continue
                            rows.append(dict(item))
                            out.append(dict(item))
                        if "return=minimal" in prefer:
                            self._send(201, None)
                        else:
                            self._send(201, out)
                    elif method == "PATCH":
                        data = json.loads(body or b"{}")
                        for r in outer._filter(rows, params):
                            r.update(data)
                        self._send(204, None)
                    else:
                        self._send(405, {"message": "unsupported"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> "FakePostgREST":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @staticmethod
    def _match(value, op: str, arg: str) -> bool:
        if op == "in":
            return str(value) in [a.strip('"') for a in arg.strip("()").split(",")]
        if value is None:
            return False
        left, right = str(value), arg
        if op == "eq":
            return left == right
        if op == "gte":
            return left >= right
        if op == "lte":
            return left <= right
        if op == "lt":
            return left < right
        if op == "gt":
            return left > right
        if op == "neq":
            return left != right
        return True

    def _filter(self, rows: list[dict], params: list[tuple[str, str]]) -> list[dict]:
        reserved = {"select", "order", "limit", "offset", "on_conflict"}
        out = list(rows)
        for key, val in params:
            if key in reserved or "." not in val:
                continue
            op, _, arg = val.partition(".")
            out = [r for r in out if self._match(r.get(key), op, arg)]
        return out

    def _select(self, rows: list[dict], params: list[tuple[str, str]]) -> list[dict]:
        out = self._filter(rows, params)
        qp = dict(params)
        order = qp.get("order")
        if order:
            col, _, direction = order.partition(".")
            out.sort(key=lambda r: str(r.get(col)), reverse=(direction == "desc"))
        if qp.get("limit"):
            out = out[: int(qp["limit"])]
        sel = qp.get("select")
        if sel and sel != "*":
            cols = [c.strip() for c in sel.split(",")]
            out = [{c: r.get(c) for c in cols} for r in out]
        return [dict(r) for r in out]
