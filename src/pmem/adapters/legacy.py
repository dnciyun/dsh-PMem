"""旧记忆插件只读适配（第三方记忆库）：用于 shadow 对比。

**只是基线**，不参与 AWB 的任何写入。已知事实（`docs/phase0-recon.md` + 实测）：

- 表：``span(scope, session, seq, text, …)`` + ``span_fts``（FTS5，默认 unicode61）；
- 中文没有分词 → ``MATCH '记忆'`` 命中 0 条，而 ``LIKE '%记忆%'`` 有 97 条。

所以对比必须分两套口径，否则"重合率 0%"会是个假的结论：

1. ``fts``：旧插件的原生召回（对中文几乎为 0，这是**能力差异**，不是参数差异）；
2. ``like``：把它的存储换成等价的中文可用词法（LIKE），当作"同样的词法基线"再用一次。
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

def _default_db() -> Path:
    """旧记忆库的默认位置。

    这些库是**别人系统**的家，路径因插件而异，所以产品不写死某一家：
    先读环境变量 ``PMEM_LEGACY_DB``；没设就在 ``~/.dsh/*/desktop/evolve.sqlite``
    这类常见位置里找第一个存在的；都找不到就返回一个不存在的路径
    （``available`` 为 False，调用方只会看到「读不到」）。
    """
    override = os.environ.get("PMEM_LEGACY_DB", "").strip()
    if override:
        return Path(override)
    base = Path.home() / ".dsh"
    for pattern in ("*/desktop/evolve.sqlite", "*/evolve.sqlite"):
        hits = sorted(base.glob(pattern))
        if hits:
            return hits[0]
    return base / "legacy" / "memory.sqlite"


DEFAULT_DB = _default_db()


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _terms(query: str) -> list[str]:
    """切成用于 FTS 的词：ASCII 词 + 中文双字。"""
    ascii_words = re.findall(r"[A-Za-z0-9_]{2,}", query)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    bigrams: list[str] = []
    for run in cjk:
        bigrams.extend(run[i : i + 2] for i in range(len(run) - 1))
    return ascii_words + bigrams


class LegacyReader:
    """只读打开旧插件的库；文件不存在或表缺失都不抛，``available`` 为 False。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self._conn: sqlite3.Connection | None = None
        self._has_span = False
        self._open()

    def _open(self) -> None:
        if not self.path.exists():
            return
        try:
            self._conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='span'"
            ).fetchone()
            self._has_span = row is not None
        except sqlite3.DatabaseError:
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None and self._has_span

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> LegacyReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 查询 --------------------------------------------------------------
    def span_count(self, scope: str) -> int:
        if not self.available:
            return 0
        row = self._conn.execute("SELECT COUNT(*) c FROM span WHERE scope=?", (scope,)).fetchone()
        return int(row["c"]) if row else 0

    def search(self, query: str, scope: str, *, k: int = 3, mode: str = "fts") -> list[dict[str, Any]]:
        """``mode='fts'`` 用旧插件原生 FTS；``'like'`` 用等价词法兜底。"""
        if not self.available:
            return []
        if mode == "fts":
            return self._search_fts(query, scope, k)
        return self._search_like(query, scope, k)

    def _search_fts(self, query: str, scope: str, k: int) -> list[dict[str, Any]]:
        terms = _terms(query)
        if not terms:
            return []
        expr = " OR ".join(_quote(t) for t in terms)
        try:
            rows = self._conn.execute(
                "SELECT session, seq, text, bm25(span_fts) AS score FROM span_fts "
                "WHERE span_fts MATCH ? AND scope=? ORDER BY score LIMIT ?",
                (expr, scope, k),
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        return [
            {"session": r["session"], "seq": r["seq"], "text": r["text"], "score": float(r["score"])}
            for r in rows
        ]

    def _search_like(self, query: str, scope: str, k: int) -> list[dict[str, Any]]:
        needle = query.strip()
        if not needle:
            return []
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            "SELECT session, seq, text FROM span WHERE scope=? AND text LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT ?",
            (scope, f"%{escaped}%", k),
        ).fetchall()
        return [
            {"session": r["session"], "seq": r["seq"], "text": r["text"], "score": 0.0}
            for r in rows
        ]

    def scopes(self) -> list[tuple[str, int]]:
        if not self.available:
            return []
        rows = self._conn.execute("SELECT scope, COUNT(*) c FROM span GROUP BY scope ORDER BY c DESC").fetchall()
        return [(r["scope"], int(r["c"])) for r in rows]
