"""并发写重试（M21 补）：抢锁失败要等，不能把这一条丢掉。

超高压测试用 16 个进程同时写同一个库，抓到过一次真丢写：
```text
expected 3200 written 3199 lost 1 errors 1
writer 12: OperationalError: database is locked
```
数据没坏、事件链也连续，只是那个进程没抢到锁、超过了 busy_timeout——这种失败
如果被上层 catch 掉，就是**用户的记忆静默少一条**。所以 ``Database.tx()`` 现在
对 SQLITE_BUSY 带抖动的重试；这些测试守的就是「会重试」和「重试耗尽要抛」。
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from pmem.base.db import Database


class _FlakyConn:
    """把真实连接包一层：前 N 次指定语句抛 database is locked。"""

    def __init__(self, real: sqlite3.Connection, statement: str, fails: int) -> None:
        self._real = real
        self._statement = statement.upper()
        self._fails = fails

    def execute(self, sql: str, *args: object):
        if self._fails > 0 and sql.strip().upper().startswith(self._statement):
            self._fails -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class BusyRetryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = None
        self.db = Database(":memory:")
        self.db.BUSY_BASE_DELAY = 0.0
        self.db.init()

    def tearDown(self) -> None:
        self.db.close()

    def test_tx_retries_when_begin_is_locked(self) -> None:
        real = self.db.conn  # 先拿到真实连接（initialize 已完成）
        self.db._conn = _FlakyConn(real, "BEGIN IMMEDIATE", 2)  # noqa: SLF001 故意的
        with self.db.tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS retry_probe(x INTEGER)")
        self.assertEqual(self.db._conn._fails, 0)  # noqa: SLF001 两次都重试掉了

    def test_tx_retries_when_commit_is_locked(self) -> None:
        real = self.db.conn
        self.db._conn = _FlakyConn(real, "COMMIT", 1)  # noqa: SLF001
        with self.db.tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS retry_probe2(x INTEGER)")
        self.assertEqual(self.db._conn._fails, 0)  # noqa: SLF001

    def test_tx_gives_up_after_retries(self) -> None:
        real = self.db.conn
        self.db.BUSY_RETRIES = 3
        self.db._conn = _FlakyConn(real, "BEGIN IMMEDIATE", 99)  # noqa: SLF001
        with self.assertRaises(sqlite3.OperationalError):
            with self.db.tx():
                pass

    def test_non_busy_errors_are_not_swallowed(self) -> None:
        """只有锁冲突才重试；SQL 写错要立刻抛，不能被重试掩盖。"""
        real = self.db.conn
        self.db._conn = _FlakyConn(real, "BEGIN IMMEDIATE", 0)  # noqa: SLF001
        with self.assertRaises(sqlite3.OperationalError):
            with self.db.tx() as conn:
                conn.execute("SELECT * FROM table_that_does_not_exist")

    def test_writes_still_land_after_a_retry(self) -> None:
        real = self.db.conn
        self.db._conn = _FlakyConn(real, "BEGIN IMMEDIATE", 1)  # noqa: SLF001
        with self.db.tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS retry_probe3(x INTEGER)")
            conn.execute("INSERT INTO retry_probe3(x) VALUES (7)")
        rows = self.db.conn.execute("SELECT x FROM retry_probe3").fetchall()
        self.assertEqual([row["x"] for row in rows], [7])


if __name__ == "__main__":
    unittest.main()
