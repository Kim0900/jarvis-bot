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
        # Task #206: PostgREST paging stand-in. max_rows emulates the project's db-max-rows cap
        # (a response is silently shortened -> HTTP 206 + Content-Range); range_quirk breaks the
        # header contract: None | "missing" | "garbled" | "total_changes" | "no_page_progress" | "total_understated" | "overlap" | "forged_range" | "star_nonempty" | "row_no_id" | "row_nondict" | "id_str" | "id_float" | "id_none" | "id_bool" | "id_numeric_str" | "range_on_empty"
        self.max_rows: int | None = None
        self.range_quirk: str | None = None
        self._range_calls = 0
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
                        if "count=exact" in self.headers.get("Prefer", ""):
                            self._send_counted(rows, params)
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
                            new_row = dict(item)
                            if table == "raw_calls" and new_row.get("id") is None:
                                # like the real serial primary key
                                new_row["id"] = 1 + max(
                                    [r["id"] for r in rows if isinstance(r.get("id"), int)] or [0])
                            rows.append(new_row)
                            out.append(dict(new_row))
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

            def _send_counted(self, rows, params):
                qp = dict(params)
                everything = outer._select(rows, [(k, v) for k, v in params if k not in ("limit", "offset")])
                total = len(everything)
                offset = int(qp.get("offset") or 0)
                limit = int(qp.get("limit") or total)
                if outer.max_rows is not None:
                    limit = min(limit, outer.max_rows)
                page = everything[offset: offset + limit]
                outer._range_calls += 1
                quirk = outer.range_quirk
                if quirk == "no_page_progress" and offset > 0:
                    page = []
                if quirk == "overlap" and offset > 0:
                    page = everything[offset - 1: offset - 1 + limit]  # row read twice, one missed
                shown_total = total + (1 if quirk == "total_changes" and outer._range_calls > 1 else 0)
                if quirk == "total_understated":
                    shown_total = max(total - 1, 0)
                if page:
                    crange = f"{offset}-{offset + len(page) - 1}/{shown_total}"
                else:
                    crange = f"*/{shown_total}"
                if quirk == "forged_range" and page:
                    crange = f"{offset + 1}-{offset + len(page)}/{shown_total}"  # right count, wrong offsets
                if quirk == "star_nonempty" and page:
                    crange = f"*/{shown_total}"
                if quirk == "row_no_id" and page:
                    page = [{k: v for k, v in page[0].items() if k != "id"}] + page[1:]
                if quirk in ("id_str", "id_float", "id_none", "id_bool", "id_numeric_str") and page and offset == 0:
                    bad = {"id_str": "abc", "id_float": 7.5, "id_none": None, "id_bool": True, "id_numeric_str": "12"}[quirk]
                    page = [dict(page[0], id=bad)] + page[1:]
                if quirk == "row_nondict" and page:
                    page = page[:-1] + ["not-a-row"]
                if quirk == "range_on_empty":
                    page = []
                    crange = f"0-0/{shown_total}"
                if quirk == "garbled":
                    crange = "items 0 of many"
                raw = json.dumps(page).encode()
                status = 206 if offset + len(page) < total else 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if quirk != "missing":
                    self.send_header("Content-Range", crange)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

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
            out.sort(key=lambda r: (0, r.get(col)) if isinstance(r.get(col), (int, float)) else (1, str(r.get(col))),
                     reverse=(direction == "desc"))
        if qp.get("limit"):
            out = out[: int(qp["limit"])]
        sel = qp.get("select")
        if sel and sel != "*":
            cols = [c.strip() for c in sel.split(",")]
            out = [{c: r.get(c) for c in cols} for r in out]
        return [dict(r) for r in out]
