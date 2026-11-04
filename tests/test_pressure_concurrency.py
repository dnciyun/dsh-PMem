"""并发写：多个独立进程同时写同一个库，一条都不能丢（从 bench/pressure 搬进单测）。

为什么必须是真的多进程：单进程里的 mock 锁（``tests/test_db_busy.py``）只能证明
「会重试」，证明不了「重试之后一条不少」。真丢写只在 4 个独立连接同时抢
``BEGIN IMMEDIATE``、``busy_timeout`` 撞线时才出现——超高压台抓到过一次
（16 进程 3200 条丢 1 条），修法是 ``Database.tx()`` 对 SQLITE_BUSY 带抖动重试。
这里把那条结论钉成回归测试，默认 4 进程 × 100 条（几秒级）；``PMEM_SLOW=1``
放大到 8 进程 × 200 条（给 nightly 用）。

两条用例：

1. ``TestMultiProcessWrite``：N 个进程各写 100/200 条 → 最终条数 = N×100/200，
   事件链连续、``integrity_check`` ok、``memory.idem``/``event_log`` 无重复、
   memory 行数 == 两个 FTS 索引行数（没有半写）。
2. ``TestLockWait``：子进程 A 拿住 ``BEGIN IMMEDIATE`` 睡一会儿，B 再写 →
   B 必须「等锁」而不是丢数据（``busy_timeout`` + 重试生效）。
   ``PMEM_SLOW=1`` 时 A 会睡超过 ``busy_timeout``（5 s），专门走重试路径。

为了把预算花在抢锁上，这里关掉向量与实体抽取（它们都是增强能力，各自在事务里，
不改变 ``add`` 的原子性语义）。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
import unittest
from pathlib import Path

from pmem.base import events
from pmem.base.config import Config
from pmem.base.db import Database
from pmem.gate.sdk import PMem

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

SLOW = os.environ.get("PMEM_SLOW") == "1"

# 默认规模：4 进程 × 100 条；nightly 放大。
WRITERS = 8 if SLOW else 4
PER_WRITER = 200 if SLOW else 100
# 持锁时长：默认 1 s（正常等锁）；slow 时 6.5 s > busy_timeout(5 s)，逼出重试路径。
LOCK_HOLD_S = 6.5 if SLOW else 1.0
JOIN_TIMEOUT_S = 180.0
READY_TIMEOUT_S = 30.0

# 只关增强能力，事务语义保持生产默认。
TEST_OVERRIDES: dict[str, dict[str, object]] = {
    "retrieval": {"vector": {"enabled": False}},
    "net": {"auto_extract": False},
}


def _ensure_child_paths() -> None:
    """spawn 子进程需要能 ``import tests.*`` 和 ``import pmem``。

    父进程的 ``sys.path`` 在 spawn 时会传给子进程，但不同 pytest 启动方式下不一定稳；
    这里把仓库根和 ``src`` 显式塞进 ``PYTHONPATH``（只影响之后启动的子进程，
    父进程的模块早就 import 完了）。
    """
    current = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    wanted = [str(ROOT), str(SRC)]
    merged = current + [part for part in wanted if part not in current]
    os.environ["PYTHONPATH"] = os.pathsep.join(merged)


# ---------------------------------------------------------------- 子进程入口
# 注意：这些函数必须是模块顶层（Windows spawn 靠 pickle 按名字反查），
# 参数只用 str/int 这类可 pickle 的朴素类型。


def _writer_process(
    db_path: str,
    prefix: str,
    count: int,
    report_path: str,
    overrides: dict[str, object],
) -> None:
    """独立连接、独立事务，连续 ``add``；结果写到 report_path 的 JSON。"""
    started = time.perf_counter()
    report: dict[str, object] = {"prefix": prefix, "ok": 0, "err": 0, "first_error": None}
    try:
        app = PMem.open(db_path, config=Config(overrides), init=False)
        try:
            for index in range(count):
                try:
                    app.store.add(
                        f"{prefix} 第 {index} 条：多进程同时写同一个库，一条都不能丢。",
                        kind="semantic",
                        provenance="observed",
                        source_kind="pressure",
                        idem_key=f"pressure:{prefix}:{index}",
                    )
                    report["ok"] = int(report["ok"]) + 1
                except Exception as exc:  # noqa: BLE001 并发下写失败要计数，不能静默
                    report["err"] = int(report["err"]) + 1
                    if report["first_error"] is None:
                        report["first_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            app.close()
    except BaseException as exc:  # noqa: BLE001 子进程崩了也要留证据
        report["fatal"] = f"{type(exc).__name__}: {exc}"
    report["elapsed_s"] = round(time.perf_counter() - started, 2)
    Path(report_path).write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )


def _lock_holder_process(
    db_path: str,
    hold_s: float,
    ready_path: str,
    report_path: str,
) -> None:
    """拿住写锁（``BEGIN IMMEDIATE``）并睡 hold_s，再提交。

    没真写过行，但 ``BEGIN IMMEDIATE`` 已经拿到 RESERVED 写锁——这正是多进程
    抢锁时另一个进程会撞到的那把锁。
    """
    report: dict[str, object] = {"acquired": False, "released": False}
    db = None
    try:
        db = Database(db_path, timeout=10.0)
        db.conn.execute("PRAGMA busy_timeout = 10000")
        db.conn.execute("BEGIN IMMEDIATE")
        report["acquired"] = True
        Path(ready_path).write_text("ready", encoding="utf-8")
        time.sleep(hold_s)
        db.conn.execute("COMMIT")
        report["released"] = True
    except BaseException as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8"
        )


# ---------------------------------------------------------------- 断言辅助


def _db_snapshot(db_path: Path) -> dict[str, object]:
    """重开库做一轮只读体检（新连接，等价于「进程重启后打开」）。"""
    db = Database(db_path)
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
        idem_events = conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE idem_key IS NOT NULL"
        ).fetchone()["c"]
        idem_distinct = conn.execute(
            "SELECT COUNT(DISTINCT idem_key) c FROM event_log WHERE idem_key IS NOT NULL"
        ).fetchone()["c"]
        idem_rows = conn.execute("SELECT COUNT(*) c FROM idem").fetchone()["c"]
        distinct_text = conn.execute(
            "SELECT COUNT(DISTINCT text_sha256) c FROM memory"
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
            "idem_events": idem_events,
            "idem_distinct": idem_distinct,
            "idem_rows": idem_rows,
            "distinct_text": distinct_text,
            "bad_rows": bad_rows,
        }
    finally:
        db.close()


class _Harness(unittest.TestCase):
    """临时库 + spawn 子进程的生命周期收拾。"""

    procs: list[mp.Process]

    def setUp(self) -> None:
        from .support import temp_dir

        _ensure_child_paths()
        self.dir = temp_dir("pressure-conc")
        self.db_path = self.dir / "pmem.db"
        self.procs = []
        # 建好 schema（子进程用 init=False 打开，避免并发建库噪音）。
        app = PMem.open(self.db_path, config=Config(TEST_OVERRIDES))
        app.close()

    def tearDown(self) -> None:
        for proc in self.procs:
            if proc.is_alive():
                proc.terminate()
                try:
                    proc.join(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
        from .support import drop_dir

        drop_dir(self.dir)

    def _spawn(self, target, args: tuple) -> mp.Process:
        ctx = mp.get_context("spawn")
        proc = ctx.Process(target=target, args=args)
        self.procs.append(proc)
        proc.start()
        return proc


class TestMultiProcessWrite(_Harness):
    def test_writers_do_not_lose_rows(self) -> None:
        expected = WRITERS * PER_WRITER
        reports = [self.dir / f"report-{index}.json" for index in range(WRITERS)]
        procs = []
        started = time.perf_counter()
        for index in range(WRITERS):
            procs.append(
                self._spawn(
                    _writer_process,
                    (str(self.db_path), f"w{index:02d}", PER_WRITER, str(reports[index]), TEST_OVERRIDES),
                )
            )

        for index, proc in enumerate(procs):
            proc.join(timeout=JOIN_TIMEOUT_S)
            self.assertFalse(
                proc.is_alive(), f"writer {index} 超时未退出（>{JOIN_TIMEOUT_S}s）"
            )
            self.assertEqual(
                proc.exitcode, 0, f"writer {index} 非零退出：{proc.exitcode}"
            )
        elapsed = time.perf_counter() - started

        payloads = []
        for index, report in enumerate(reports):
            self.assertTrue(report.exists(), f"writer {index} 没写报告，可能启动就崩了")
            payload = json.loads(report.read_text(encoding="utf-8"))
            payloads.append(payload)
            self.assertNotIn("fatal", payload, f"writer {index} 崩了：{payload.get('fatal')}")
            self.assertEqual(payload["err"], 0, f"writer {index} 有写失败：{payload}")
            self.assertEqual(payload["ok"], PER_WRITER, f"writer {index} 少写：{payload}")

        snapshot = _db_snapshot(self.db_path)
        self.assertTrue(snapshot["integrity"], snapshot)
        self.assertTrue(snapshot["chain_ok"], snapshot["chain_msg"])
        self.assertEqual(snapshot["memory"], expected, snapshot)
        self.assertEqual(snapshot["add_events"], expected, snapshot)
        self.assertEqual(snapshot["distinct_text"], expected, "出现重复记忆行")
        self.assertEqual(snapshot["memory"], snapshot["fts_tok"], snapshot)
        self.assertEqual(snapshot["memory"], snapshot["fts_tri"], snapshot)
        self.assertEqual(snapshot["bad_rows"], 0, snapshot)
        # 幂等：每个 idem_key 恰好一行，事件链上也没有重复 idem。
        self.assertEqual(snapshot["idem_rows"], expected, snapshot)
        self.assertEqual(snapshot["idem_events"], expected, snapshot)
        self.assertEqual(snapshot["idem_distinct"], expected, snapshot)
        # 报告里也带上耗时，方便 nightly 看趋势。
        print(
            f"\n[concurrency] {WRITERS} 进程 × {PER_WRITER} 条 = {expected} 条，"
            f"耗时 {elapsed:.1f}s；失败 0；链 {snapshot['chain_msg']}"
        )


class TestLockWait(_Harness):
    def test_writer_waits_for_lock_instead_of_losing_row(self) -> None:
        ready = self.dir / "lock-ready"
        holder_report = self.dir / "holder.json"
        holder = self._spawn(
            _lock_holder_process,
            (str(self.db_path), LOCK_HOLD_S, str(ready), str(holder_report)),
        )

        try:
            deadline = time.monotonic() + READY_TIMEOUT_S
            while not ready.exists():
                if not holder.is_alive():
                    self.fail(f"持锁进程提前退出：exitcode={holder.exitcode}")
                if time.monotonic() > deadline:
                    self.fail(f"持锁进程 {READY_TIMEOUT_S}s 内没拿到锁")
                time.sleep(0.02)

            app = PMem.open(self.db_path, config=Config(TEST_OVERRIDES), init=False)
            started = time.perf_counter()
            try:
                result = app.store.add(
                    "锁等待：另一个进程正持有 BEGIN IMMEDIATE，这一条必须等锁，不能丢。",
                    kind="semantic",
                    provenance="observed",
                    idem_key="lock-wait:1",
                )
            finally:
                app.close()
            waited = time.perf_counter() - started
        finally:
            holder.join(timeout=JOIN_TIMEOUT_S)
            if holder.is_alive():  # pragma: no cover - 理论上不会走到
                holder.terminate()
                holder.join(timeout=10)

        self.assertEqual(holder.exitcode, 0, "持锁进程非零退出")
        holder_payload = json.loads(holder_report.read_text(encoding="utf-8"))
        self.assertTrue(holder_payload["acquired"], holder_payload)
        self.assertTrue(holder_payload["released"], holder_payload)
        # 真等过锁：如果锁没实际挡住，B 会在几毫秒内返回。
        self.assertGreaterEqual(
            waited, LOCK_HOLD_S * 0.5, f"B 只等了 {waited:.2f}s，锁可能没生效"
        )
        self.assertIn(result.get("status"), ("active", "candidate"), result)

        snapshot = _db_snapshot(self.db_path)
        self.assertTrue(snapshot["integrity"], snapshot)
        self.assertTrue(snapshot["chain_ok"], snapshot["chain_msg"])
        self.assertEqual(snapshot["memory"], 1, snapshot)
        self.assertEqual(snapshot["add_events"], 1, snapshot)
        self.assertEqual(snapshot["memory"], snapshot["fts_tok"], snapshot)
        self.assertEqual(snapshot["memory"], snapshot["fts_tri"], snapshot)
        self.assertEqual(snapshot["bad_rows"], 0, snapshot)
        print(
            f"\n[lock-wait] 持锁 {LOCK_HOLD_S:.1f}s，B 实际等待 {waited:.2f}s 后写入成功"
        )


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
