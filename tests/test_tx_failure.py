"""事务失败后的连接收尸（修 A）：COMMIT/ROLLBACK 失败不能留下僵尸事务。

背景（审计结论见 ``.tmp/review/tx-concurrency.md`` §2.1）：
``_commit_with_retry`` 重试耗尽后裸 COMMIT 抛错时，SQLite 的事务仍然活着；
旧版 ``tx()`` 不回滚也不复位连接，下一次 ``tx()`` 会把它当"嵌套"复用，
后续写入永远不提交——长驻进程（MCP / REST / scheduler）会「写成功但永不落盘」，
还一直占着写锁挡住别的进程。

这里守五件事：
1. COMMIT 重试耗尽后连接被收回干净（``in_transaction=False``），后续写入真落盘；
2. ROLLBACK 也失败时连接被丢弃，下一次访问重建一个干净连接；
3. 事务体抛异常 + ROLLBACK 失败，同样收尸且原异常照常外抛；
4. 非 busy 的 COMMIT 失败（如 I/O）也收尸；
5. 正常提交/回滚与「嵌套复用」语义没被改坏（防修坏）。
"""

from __future__ import annotations

import sqlite3
import unittest

from pmem.base.db import Database

from .support import drop_dir, temp_dir


class _FlakyConn:
    """把真实连接包一层：前 N 次指定语句抛错（默认 database is locked）。

    写法参考 ``tests/test_db_busy.py`` 的 ``_FlakyConn``；这里多一个 message
    参数，用来模拟"非 busy"的 COMMIT 失败。
    """

    def __init__(
        self,
        real: sqlite3.Connection,
        statement: str,
        fails: int,
        message: str = "database is locked",
    ) -> None:
        self._real = real
        self._statement = statement.strip().upper()
        self._fails = fails
        self._message = message

    def execute(self, sql: str, *args: object):
        if self._fails > 0 and sql.strip().upper().startswith(self._statement):
            self._fails -= 1
            raise sqlite3.OperationalError(self._message)
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class _TxBrokenConn:
    """COMMIT 与 ROLLBACK 都失败：模拟「收尸也收不回来」的连接。"""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *args: object):
        head = sql.strip().upper()
        if head.startswith("COMMIT") or head.startswith("ROLLBACK"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class TxFailureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("tx-failure")
        self.path = self.dir / "pmem.db"
        self.db = Database(self.path)
        self.db.BUSY_BASE_DELAY = 0.0
        self.db.init()
        with self.db.tx() as conn:
            conn.execute("CREATE TABLE probe(x INTEGER)")

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    def _count(self) -> list[int]:
        """从**另一条连接**读已提交数据（验证「真落盘」而不是本连接可见）。"""
        other = sqlite3.connect(str(self.path), timeout=5)
        try:
            return [row[0] for row in other.execute("SELECT x FROM probe ORDER BY x")]
        finally:
            other.close()

    def test_commit_exhaustion_recovers_and_later_writes_land(self) -> None:
        """修 A 的核心场景：COMMIT 一直 busy → 重试耗尽 → 不能再留僵尸事务。"""
        real = self.db.conn
        self.db.BUSY_RETRIES = 2
        # 2 次循环重试 + 1 次耗尽后的裸 COMMIT = 3 次失败，之后锁「放开了」
        self.db._conn = _FlakyConn(real, "COMMIT", 3)  # noqa: SLF001 故意的
        with self.assertRaises(sqlite3.OperationalError):
            with self.db.tx() as conn:
                conn.execute("INSERT INTO probe(x) VALUES (1)")

        # 旧版会停在 True，下一次 tx() 被当嵌套、永远不提交
        self.assertFalse(real.in_transaction)
        self.assertFalse(self.db.conn.in_transaction)

        # 后续写入必须真的提交（而不是挂在一个永不提交的旧事务里）
        with self.db.tx() as conn:
            conn.execute("INSERT INTO probe(x) VALUES (2)")
        self.assertEqual(self._count(), [2])  # 1 被回滚、2 落盘
        self.assertFalse(self.db.conn.in_transaction)

    def test_commit_and_rollback_both_fail_discards_connection(self) -> None:
        """收尸也失败：连接被丢弃，下次访问重建干净连接，不进入不确定状态。"""
        real = self.db.conn
        self.db.BUSY_RETRIES = 1
        self.db._conn = _TxBrokenConn(real)  # noqa: SLF001
        with self.assertRaises(sqlite3.OperationalError):
            with self.db.tx() as conn:
                conn.execute("INSERT INTO probe(x) VALUES (1)")

        # 旧连接被关、_conn 置空，db.conn 会按 _open() 重建
        self.assertNotIsInstance(self.db.conn, _TxBrokenConn)
        self.assertIsInstance(self.db.conn, sqlite3.Connection)
        self.assertFalse(self.db.conn.in_transaction)
        with self.assertRaises(sqlite3.ProgrammingError):
            real.execute("SELECT 1")  # 旧连接确实关了

        with self.db.tx() as conn:
            conn.execute("INSERT INTO probe(x) VALUES (2)")
        self.assertEqual(self._count(), [2])

    def test_body_exception_with_failed_rollback_discards_connection(self) -> None:
        """事务体异常 + ROLLBACK 失败：原异常照常抛，连接照样收干净。"""
        real = self.db.conn
        self.db._conn = _FlakyConn(real, "ROLLBACK", 99)  # noqa: SLF001
        with self.assertRaises(ValueError):
            with self.db.tx() as conn:
                conn.execute("INSERT INTO probe(x) VALUES (1)")
                raise ValueError("boom")

        self.assertIsInstance(self.db.conn, sqlite3.Connection)
        self.assertIsNot(self.db.conn, real)
        self.assertFalse(self.db.conn.in_transaction)
        self.assertEqual(self._count(), [])  # 半截写入没有落盘

    def test_non_busy_commit_failure_also_recovers(self) -> None:
        """非 busy 的 COMMIT 失败（例如 I/O）同样不能把活事务留给下一次。"""
        real = self.db.conn
        self.db._conn = _FlakyConn(real, "COMMIT", 1, message="disk I/O error")  # noqa: SLF001
        with self.assertRaises(sqlite3.OperationalError):
            with self.db.tx() as conn:
                conn.execute("INSERT INTO probe(x) VALUES (1)")
        self.assertFalse(self.db.conn.in_transaction)

        with self.db.tx() as conn:
            conn.execute("INSERT INTO probe(x) VALUES (2)")
        self.assertEqual(self._count(), [2])

    def test_normal_commit_and_rollback_unchanged(self) -> None:
        """回归：正常提交/回滚行为不变。"""
        with self.db.tx() as conn:
            conn.execute("INSERT INTO probe(x) VALUES (1)")
        self.assertEqual(self._count(), [1])
        self.assertFalse(self.db.conn.in_transaction)

        with self.assertRaises(RuntimeError):
            with self.db.tx() as conn:
                conn.execute("INSERT INTO probe(x) VALUES (2)")
                raise RuntimeError("boom")
        self.assertEqual(self._count(), [1])  # 2 被回滚
        self.assertFalse(self.db.conn.in_transaction)

    def test_nested_tx_still_reuses_outer_transaction(self) -> None:
        """回归：嵌套 tx 仍是「复用」——内层退出不提交，外层退出才落盘。"""
        with self.db.tx() as conn:
            conn.execute("INSERT INTO probe(x) VALUES (1)")
            with self.db.tx() as inner:
                inner.execute("INSERT INTO probe(x) VALUES (2)")
            self.assertEqual(self._count(), [])  # 内层退出没提交
        self.assertEqual(self._count(), [1, 2])  # 外层退出才提交
        self.assertFalse(self.db.conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
