"""DSH 会话日志适配：定位、解压（多帧 zstd）、抽取消息。

为什么不用 Python 解 zstd：3.12 没有 ``compression.zstd``，本机也没装 ``zstandard``。
DSH 自带的 Node 有 ``zlib.zstdDecompressSync``，所以解压交给 ``tools/session_dump.mjs``，
Python 只负责解析 —— 依赖方向是"外部工具可选，核心不依赖"。

作用域键必须与 dsh-soul 完全一致（`phase0-recon.md` §5）：
``project:<sha256(小写化、正斜杠、去尾斜杠的 cwd)>``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DUMP_SCRIPT = ROOT / "tools" / "session_dump.mjs"

SESSION_GLOB = "**/session.v4.jsonl.zstd"


# ---------------------------------------------------------------- 作用域

def normalize_path(path: str) -> str:
    """与 dsh-soul 的 normalizePath 等价：反斜杠→正斜杠、去尾斜杠、Windows 路径小写。

    注意「Windows 路径小写」是**按路径形态判断的，不按当前系统**：scope 是跨机器共享的键，
    在 Linux 上处理一个 ``D:\\work\\demo`` 时如果保留大小写，算出来的哈希就和 Windows
    记下的不一样——同一台机器换个系统就成了另一个人。POSIX 路径保持大小写敏感。
    """
    text = str(path or "").replace("\\", "/")
    if len(text) > 1:
        text = text.rstrip("/") or "/"
    if re.match(r"^[A-Za-z]:/", text) or text.startswith("//"):
        return text.lower()
    return text


def project_scope(cwd: str) -> str:
    """``project:<sha256>``，与 dsh-soul 的 projectScope 逐字节一致。"""
    return "project:" + hashlib.sha256(normalize_path(cwd).encode()).hexdigest()


# ---------------------------------------------------------------- Node

def find_node() -> str | None:
    """找 Node：显式配置 > PATH > DSH 自带运行时。"""
    explicit = os.environ.get("PMEM_NODE")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("node")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for pattern in ("dsh-runtimes/*/dependencies/node/bin/node.exe", "dsh-runtimes/*/dependencies/node/bin/node"):
        for candidate in sorted(home.glob(".dsh/" + pattern), reverse=True):
            if candidate.exists():
                return str(candidate)
    return None


# ---------------------------------------------------------------- 解压缓存

def cache_root() -> Path:
    env = os.environ.get("PMEM_CACHE")
    if env:
        return Path(env)
    return ROOT / ".tmp" / "dsh-cache"


def _cache_key(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def dump_session(path: Path, *, node: str | None = None) -> Path:
    """把 .zstd 解到缓存目录，返回明文 JSONL 路径（按 size+mtime 失效）。"""
    node = node or find_node()
    if node is None:
        raise RuntimeError(
            "找不到 Node，无法解压会话日志；请设置 PMEM_NODE，或直接用 .jsonl 文件（测试模式）"
        )
    target = cache_root() / f"{path.parent.name}-{_cache_key(path)}.jsonl"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [node, str(DUMP_SCRIPT), str(path)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"解压失败 {path.name}: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    target.write_bytes(proc.stdout)
    return target


def load_events(path: str | Path, *, node: str | None = None) -> list[dict[str, Any]]:
    """读一个会话的事件列表。``.jsonl`` 直读（测试/已解压），``.zstd`` 走 Node。"""
    p = Path(path)
    if p.suffix == ".zstd":
        p = dump_session(p, node=node)
    events: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


# ---------------------------------------------------------------- 抽取

def session_meta(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if event.get("type") == "session":
            return {
                "id": event.get("id"),
                "cwd": event.get("cwd"),
                "created_at": event.get("createdAt"),
                "origin": event.get("origin", "root"),
                "parent": event.get("parentSession"),
            }
    return {"id": None, "cwd": None, "created_at": None, "origin": "root", "parent": None}


def _text_from_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


_BOT_PREFIXES = (
    "【当前时间】",
    "【当前角色】",
    "你是一个",
    "你负责",
    "请从下面",
    "请针对",
    "请根据以下",
    "请阅读",
    "请为以下",
    "请分析以下",
    "以下是",
)
_BOT_MARKERS = (
    "请从下面的聊天记录",
    "请针对以下候选",
    "可能是黑话",
    "群友语境",
    "输出 JSON",
)


def looks_human(text: str, *, max_chars: int = 600) -> bool:
    """粗筛"人说的话"。

    DSH 的 ``source.kind`` 只有 ``user`` 一种，但插件会把任务模板也当用户消息塞进来
    （黑话学习器的长 prompt 就是典型）。影子模式用它们当查询毫无意义：既不是人的问题，
    又会让 FTS 的 OR 项爆炸（1000 字 ≈ 1000 个双字 token，实测单次 1.8 s）。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > max_chars:
        return False
    if stripped.startswith("<"):
        return False
    if any(stripped.startswith(prefix) for prefix in _BOT_PREFIXES):
        return False
    if any(marker in stripped for marker in _BOT_MARKERS):
        return False
    # 模板化任务通常带大量列表符号
    if stripped.count("\n- ") >= 6 or stripped.count("{{") >= 2:
        return False
    return True


def user_messages(
    events: list[dict[str, Any]],
    *,
    min_chars: int = 6,
    max_chars: int = 600,
    human_only: bool = True,
) -> list[dict[str, Any]]:
    """抽出真正由人发出的消息（跳过斜杠命令、系统注入、插件任务模板）。"""
    out: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "user/message":
            continue
        data = event.get("data") or {}
        source = data.get("source") or {}
        if source.get("kind") not in (None, "user"):
            continue
        text = _text_from_blocks(data.get("content"))
        if len(text) < min_chars:
            continue
        if text.startswith("/"):
            continue
        if human_only and not looks_human(text, max_chars=max_chars):
            continue
        out.append({"seq": event.get("seq"), "time": event.get("time"), "text": text})
    return out


def assistant_texts(events: list[dict[str, Any]], *, min_chars: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "assistant/message":
            continue
        message = (event.get("data") or {}).get("message") or {}
        text = _text_from_blocks(message.get("content"))
        if len(text) >= min_chars:
            out.append({"seq": event.get("seq"), "time": event.get("time"), "text": text})
    return out


def list_sessions(root: str | Path) -> list[Path]:
    return sorted(Path(root).glob(SESSION_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)


def recent_sessions(root: str | Path, *, days: int = 30) -> list[Path]:
    cutoff = time.time() - days * 86400
    return [p for p in list_sessions(root) if p.stat().st_mtime >= cutoff]
