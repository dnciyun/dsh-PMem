"""崩溃恢复：写到一半被硬杀，重开之后库还是完整的（从 tools/durability.py 搬进单测）。

为什么必须真杀进程：只有「进程在事务中途被 terminate、来不及跑任何清理」时，
WAL 里才可能留下半截事务、FTS 才可能和 memory 行数对不上。这里每轮：

1. 父进程建好库（schema 已就绪）；
2. 子进程连续 ``memory_add``，成功写到第一条后在 marker 文件落痕；
3. 父进程看到 marker 后小睡随机 50–150 ms，再 ``terminate()``
   （Windows = TerminateProcess，等价 kill -9；POSIX 是 SIGTERM，Python 默认不清理）；
4. 重开库断言：``PRAGMA integrity_check`` = ok、事件哈希链连续、
   ``memory`` 行数 == 两个 FTS 索引行数（没有半写）、``memory.add`` 事件数 == 行数、
   关键字段非空。

默认 3 轮（几秒）；``PMEM_SLOW=1`` 时 8 轮（nightly 用）。判据与阈值不靠「感觉没丢」，
而是 SQLite 自己的完整性检查 + 事件链 + 行数一致——和 ``tools/durability.py`` 同口径。
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
import unittest
from pathlib import Path

from pmem.base import events
from pmem.base.db import Database

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

SLOW = os.environ.get("PMEM_SLOW") == "1"
ROUNDS = 8 if SLOW else 3
MARKER_TIMEOUT_S = 30.0
KILL_SETTLE_S = 10.0

# 子进程写手：独立解释器，直接 `python -c`（纯 stdlib 调用，不依赖 test 包）。
# 参数：argv[1]=库路径，argv[2]=src 路径，argv[3]=marker 路径。
WRITER_CODE = r"""
import sys

sys.path.insert(0, sys.argv[2])

from pmem.base.config import Config
from pmem.gate.sdk import PMem

CONFIG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})

app = PMem.open(sys.argv[1], config=CONFIG, init=False)
index = 0
while True:
    app.store.add(
        f"durability 第 {index} 条：写到一半被 terminate，重开之后库还是完整的。",
        kind="semantic",
        provenance="observed",
        source_kind="durability",
        idem_key=f"durability:{index}",
    )
    if index == 0:
        with open(sys.argv[3], "w", encoding="utf-8") as fh:
            fh.write("wrote")
    index += 1
"""


def _clear_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _check(path: Path) -> dict[str, object]:
    """重开库做一轮只读体检（等价于「进程被杀之后重新打开」）。"""
    db = Database(path)
    try:
        conn = db.conn
        integrity = db.integrity_ok()
        chain_ok, chain_msg = events.verify_chain(conn)
        memory = conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
        fts_tok = conn.execute("SELECT COUNT(*) c FROM memory_fts_tok").fetchone()["c"]
        fts_tri = conn.execute("SELECT COUNT(*) c FROM memory_fts_tri").fetchone()["c"]
        add_events = conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE op='memory.add'"
        ).fetchone()["c"]
        bad_rows = conn.execute(
            "SELECT COUNT(*) c FROM memory "
            "WHERE text_raw IS NULL OR text_raw='' OR id IS NULL "
            "OR created_at IS NULL OR status IS NULL"
        ).fetchone()["c"]
        return {
            "integrity": integrity,
            "chain_ok": chain_ok,
            "chain_msg": chain_msg,
            "memory": memory,
            "fts_tok": fts_tok,
            "fts_tri": fts_tri,
            "add_events": add_events,
            "bad_rows": bad_rows,
        }
    finally:
        db.close()


class TestDurability(unittest.TestCase):
    def setUp(self) -> None:
        from .support import temp_dir

        self.dir = temp_dir("durability")
        self.db_path = self.dir / "pmem.db"
        self._init_db()

    def tearDown(self) -> None:
        from .support import drop_dir

        drop_dir(self.dir)

    def _init_db(self) -> None:
        """建好 schema；子进程用 init=False 打开，只负责写。"""
        from pmem.base.config import Config
        from pmem.gate.sdk import PMem

        app = PMem.open(
            self.db_path,
            config=Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}}),
        )
        app.close()

    def _round(self, index: int, rng: random.Random) -> dict[str, object]:
        _clear_db(self.db_path)
        self._init_db()
        marker = self.dir / f"wrote-{index}"
        env = os.environ.copy()
        paths = [str(ROOT), str(SRC)]
        current = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
        env["PYTHONPATH"] = os.pathsep.join(current + [p for p in paths if p not in current])
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, "-c", WRITER_CODE, str(self.db_path), str(SRC), str(marker)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT),
        )
        try:
            deadline = time.monotonic() + MARKER_TIMEOUT_S
            while not marker.exists():
                if proc.poll() is not None:
                    stderr = (proc.stderr.read() if proc.stderr else b"") or b""
                    self.fail(
                        f"第 {index} 轮写手提前退出（code={proc.returncode}）："
                        f"{stderr.decode('utf-8', 'replace')[-800:]}"
                    )
                if time.monotonic() > deadline:
                    self.fail(f"第 {index} 轮写手 {MARKER_TIMEOUT_S}s 内没写出第一条")
                time.sleep(0.01)
            # 已经至少提交了一条；再让它在写的过程中被杀，制造「事务中途断电」。
            time.sleep(rng.uniform(0.05, 0.15))
            proc.terminate()  # Windows: TerminateProcess；POSIX: SIGTERM（Python 不清理）
            proc.wait(timeout=KILL_SETTLE_S)
        finally:
            if proc.poll() is None:  # pragma: no cover - 保底
                proc.kill()
                proc.wait(timeout=KILL_SETTLE_S)
            if proc.stderr is not None:
                proc.stderr.close()

        snapshot = _check(self.db_path)
        # 硬杀时可能死在「第一条已提交、第二/三条在事务里」的任意点，但已提交的数据
        # 必须完整：memory 行数 == FTS 行数 == memory.add 事件数，且字段非空。
        ok = (
            bool(snapshot["integrity"])
            and bool(snapshot["chain_ok"])
            and int(snapshot["memory"]) >= 1
            and snapshot["memory"] == snapshot["fts_tok"] == snapshot["fts_tri"]
            and snapshot["add_events"] == snapshot["memory"]
            and int(snapshot["bad_rows"]) == 0
        )
        return {"round": index, "ok": ok, "marker_written": marker.exists(), **snapshot}

    def test_terminate_mid_write_leaves_clean_db(self) -> None:
        rng = random.Random(7)
        results = []
        started = time.perf_counter()
        for index in range(ROUNDS):
            results.append(self._round(index, rng))
        elapsed = time.perf_counter() - started

        failures = [item for item in results if not item["ok"]]
        self.assertEqual(
            failures, [], f"{len(failures)}/{ROUNDS} 轮崩溃后库不一致：{failures}"
        )
        # 每轮都要真的写到过东西，否则这个测试等于在测空库。
        for item in results:
            self.assertTrue(item["marker_written"], item)
            self.assertGreaterEqual(int(item["memory"]), 1, item)
            self.assertTrue(item["integrity"], item)
            self.assertTrue(item["chain_ok"], item["chain_msg"])
            self.assertEqual(item["memory"], item["fts_tok"], item)
            self.assertEqual(item["memory"], item["fts_tri"], item)
            self.assertEqual(item["add_events"], item["memory"], item)
        print(
            f"\n[durability] {ROUNDS} 轮 terminate 全部一致，耗时 {elapsed:.1f}s；"
            f"末轮 memory={results[-1]['memory']}，链 {results[-1]['chain_msg']}"
        )


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
