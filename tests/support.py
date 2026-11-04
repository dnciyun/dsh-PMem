"""测试辅助：临时库、SDK 工厂、MCP 子进程客户端。

注意：临时目录一律放在仓库内 ``.tmp/tests``，不用系统 %TEMP% ——
沙箱对系统临时目录的写/改权限不稳，会把测试变成环境问题。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pmem.gate.sdk import PMem

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = Path(__file__).resolve().parents[1]


def temp_dir(prefix: str = "t") -> Path:
    base = ROOT / ".tmp" / "tests"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{prefix}-{os.getpid()}-{time.time_ns()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def drop_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class TempDB:
    """with TempDB() as t: app = t.app(...)"""

    def __enter__(self) -> TempDB:
        self.dir = temp_dir("pmem")
        return self

    def __exit__(self, *exc: object) -> None:
        drop_dir(self.dir)

    @property
    def path(self) -> Path:
        return self.dir / "pmem.db"

    def app(self, **kwargs: object) -> PMem:
        return PMem.open(self.path, **kwargs)


def make_app(tmpdir: Path, **kwargs: object) -> PMem:
    return PMem.open(tmpdir / "pmem.db", **kwargs)


class InProcessMcp:
    """进程内 MCP 客户端：直接喂 JSON-RPC 给 McpServer，不走管道。

    为什么不用子进程：沙箱/CI 环境常常禁止 CreatePipe（WinError 5），
    那会让协议测试变成环境测试。子进程冒烟测试单独保留一条，见 test_gate.TestMcpStdio。
    """

    def __init__(self, db_path: Path) -> None:
        import io

        from pmem.gate.mcp_server import McpServer
        from pmem.gate.sdk import PMem

        self.app = PMem.open(db_path)
        self._buf = io.StringIO()
        self._server = McpServer(self.app, out=self._buf)
        self.next_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        self._buf.seek(0)
        self._buf.truncate(0)
        self._server.handle(
            {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        )
        text = self._buf.getvalue().strip()
        if not text:
            return {}
        return json.loads(text.splitlines()[-1])

    def notify(self, method: str, params: dict | None = None) -> None:
        self._server.handle({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def raw_tool(self, name: str, args: dict) -> dict:
        return self.call("tools/call", {"name": name, "arguments": args})

    def tool(self, name: str, args: dict) -> dict:
        resp = self.raw_tool(name, args)
        result = resp.get("result") or {}
        content = result.get("content") or []
        if not content:
            return result
        return json.loads(content[0]["text"])

    def close(self) -> None:
        self.app.close()


class McpClient:
    """极简 MCP stdio 客户端：发一行 JSON-RPC，读一行结果。"""

    def __init__(self, db_path: Path) -> None:
        import os

        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONHOME", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pmem.gate.mcp_server", "--db", str(db_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(db_path.parent),
        )
        self.next_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"mcp server died: {err}")
        return json.loads(line)

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}, ensure_ascii=False) + "\n"
        )
        self.proc.stdin.flush()

    def raw_tool(self, name: str, args: dict) -> dict:
        return self.call("tools/call", {"name": name, "arguments": args})

    def tool(self, name: str, args: dict) -> dict:
        resp = self.raw_tool(name, args)
        result = resp.get("result") or {}
        content = result.get("content") or []
        if not content:
            return result
        return json.loads(content[0]["text"])

    def close(self) -> None:
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()
            self.proc.wait(timeout=5)
