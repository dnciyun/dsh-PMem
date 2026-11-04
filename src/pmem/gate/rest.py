"""最小 REST 门面（M8，stdlib only）。

只做协议映射，不做业务逻辑：`POST /v1/memory/<tool>` 的 body 就是 MCP 工具参数，
响应就是统一包。默认只绑 127.0.0.1；对外暴露必须显式改 `[gate] rest_host` 并设 token。

端点：

- `GET  /healthz`            → {ok, name, version, schema}
- `GET  /v1/capabilities`    → 同 memory_capabilities
- `POST /v1/memory/<tool>`   → 同 tools/call
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pmem
from ..base.errors import HTTP_STATUS, PmemError
from . import schemas
from .mcp_server import McpServer
from .sdk import PMem
from .security import RateLimiter, require_token


class _Handler(BaseHTTPRequestHandler):
    server_version = "pmem-rest/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover - 静默
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        expected = getattr(self.server, "token", "")
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        provided = header[7:].strip() if header.lower().startswith("bearer ") else None
        try:
            require_token(expected, provided)
            return True
        except PmemError as exc:
            self._send(exc.http_status, {"ok": False, "error": exc.to_dict()})
            return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send(
                200,
                {
                    "ok": True,
                    "name": pmem.__name_cn__,
                    "version": pmem.__version__,
                    "schema_version": pmem.SCHEMA_VERSION,
                },
            )
            return
        if path == "/v1/capabilities":
            if not self._auth():
                return
            local = _local_app(self.server.app)  # type: ignore[attr-defined]
            try:
                result = local.capabilities()
            finally:
                local.close()
            self._send(200 if result.get("ok") else 500, result)
            return
        self._send(404, {"ok": False, "error": {"code": "E_NOT_FOUND", "message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self._auth():
            return
        parsed = urlparse(self.path)
        prefix = "/v1/memory/"
        if not parsed.path.startswith(prefix):
            self._send(404, {"ok": False, "error": {"code": "E_NOT_FOUND", "message": "not found"}})
            return
        tool = parsed.path[len(prefix) :]
        if not tool.startswith("memory_"):
            tool = f"memory_{tool}"
        if schemas.by_name(tool) is None:
            self._send(
                404,
                {"ok": False, "error": {"code": "E_NOT_FOUND", "message": f"unknown tool: {tool}"}},
            )
            return

        limiter: RateLimiter = self.server.limiter  # type: ignore[attr-defined]
        try:
            limiter.check(self.client_address[0])
        except PmemError as exc:
            self._send(exc.http_status, {"ok": False, "error": exc.to_dict()})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 32 * 1024 * 1024:
            self._send(
                413 if length > 0 else 400,
                {"ok": False, "error": {"code": "E_VALIDATION", "message": "body 为空或过大"}},
            )
            return
        try:
            args = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(args, dict):
                raise ValueError("body 必须是 JSON 对象")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(
                400,
                {"ok": False, "error": {"code": "E_VALIDATION", "message": f"body 解析失败：{exc}"}},
            )
            return

        local = _local_app(self.server.app)  # type: ignore[attr-defined]
        mcp = McpServer(local)
        try:
            result = mcp.dispatch(tool, args)
        except PmemError as exc:
            self._send(exc.http_status, {"ok": False, "error": exc.to_dict()})
            return
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"ok": False, "error": {"code": "E_INTERNAL", "message": str(exc)}})
            return
        finally:
            local.close()
        status = 200 if result.get("ok") else int(
            HTTP_STATUS.get(result.get("error", {}).get("code", "E_INTERNAL"), 400)
        )
        self._send(status, result)


def _local_app(app: Any) -> PMem:
    """HTTP handler 跑在另一个线程；sqlite 连接不能跨线程，按请求开一个。"""
    return PMem.open(
        app.db.path,
        config=app.config,
        subject=app.subject,
        init=False,
        apply_tuned=False,
    )


def make_server(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str = "",
    rate_limit_per_min: int = 0,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.app = app  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    server.limiter = RateLimiter(rate_limit_per_min)  # type: ignore[attr-defined]
    return server
