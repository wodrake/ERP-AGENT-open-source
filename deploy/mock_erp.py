"""轻量 ERP HTTP 服务。

数据来自 evals/business 的冻结用例，请求转发到 evals.business.fixture.ERP，
这样本地和 compose 不必连接真实 Java ERP。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from evals.business.dataset import cases, extended_cases
from evals.business.fixture import ERP


def build_case() -> dict:
    """合并冻结用例里的零件、供应商和库存，按 id 去重。"""
    merged = {"parts": [], "suppliers": [], "inventory": [], "orders": []}
    seen: set[tuple[str, int]] = set()
    for case in list(cases()) + list(extended_cases()):
        fixtures = case.get("fixtures") or {}
        for key in ("parts", "suppliers", "inventory"):
            for row in fixtures.get(key) or []:
                identity = (key, int(row.get("id", -1)))
                if identity in seen:
                    continue
                seen.add(identity)
                merged[key].append(row)
    return {"fixtures": merged}


def _coerce_params(raw: dict) -> dict:
    params = {}
    for key, value in raw.items():
        if isinstance(value, list):
            value = value[0] if value else None
        if value is None:
            continue
        if key in {"current", "size", "status", "supplierId", "partId"} and str(value).lstrip("-").isdigit():
            value = int(value)
        params[key] = value
    return params


class MockERPServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8081):
        self.erp = ERP(build_case())
        self._lock = threading.Lock()
        handler = self._handler_class()
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.mock = self  # type: ignore[attr-defined]

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        shown = "127.0.0.1" if host in {"0.0.0.0", ""} else host
        return f"http://{shown}:{port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        with self._lock:
            fn = getattr(self.erp, method.lower(), None)
            if fn is None:
                return {"code": 501, "message": f"mock-erp does not implement {method}", "data": None}
            if method.upper() in {"POST", "PUT"}:
                return asyncio.run(fn(path, json_data=body, params=params))
            if method.upper() == "PATCH":
                return asyncio.run(fn(path, params=params or body))
            if method.upper() == "DELETE":
                return asyncio.run(fn(path))
            return asyncio.run(fn(path, params=params))

    def _handler_class(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self._dispatch("GET")

            def do_POST(self):  # noqa: N802
                self._dispatch("POST")

            def do_PUT(self):  # noqa: N802
                self._dispatch("PUT")

            def do_PATCH(self):  # noqa: N802
                self._dispatch("PATCH")

            def do_DELETE(self):  # noqa: N802
                self._dispatch("DELETE")

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send({"status": "ok", "service": "mock-erp"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = None
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError:
                        self._send({"code": 400, "message": "invalid JSON", "data": None}, 400)
                        return
                params = _coerce_params(parse_qs(parsed.query))
                try:
                    payload = server.call(method, parsed.path, params=params, body=body)
                except Exception as exc:  # fixture 缺口要变成结构化错误，而不是断开连接
                    payload = {"code": 500, "message": f"mock-erp error: {exc}", "data": None}
                self._send(payload)

            def _send(self, payload: dict, status: int = 200) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args) -> None:
                return

        return Handler


def main() -> None:
    host = os.getenv("MOCK_ERP_HOST", "0.0.0.0")
    port = int(os.getenv("MOCK_ERP_PORT", "8081"))
    server = MockERPServer(host, port)
    print(f"mock-erp listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
