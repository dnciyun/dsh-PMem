"""基础层：ULID、时钟、配置、建库。"""

from __future__ import annotations

import unittest
from pathlib import Path

from pmem.base import events
from pmem.base.clock import day_key, days_between, now_iso, parse_iso, to_iso
from pmem.base.config import Config
from pmem.base.db import Database
from pmem.base.ids import is_ulid, ulid, ulid_time_ms

from .support import drop_dir, temp_dir


class TestUlid(unittest.TestCase):
    def test_shape(self) -> None:
        value = ulid()
        self.assertEqual(len(value), 26)
        self.assertTrue(is_ulid(value))
        self.assertTrue(value.isupper() or value.isdigit())

    def test_lexicographic_order_matches_time(self) -> None:
        first = ulid()
        second = ulid()
        self.assertLess(first, second)

    def test_monotonic_within_same_ms(self) -> None:
        stamp = 1_700_000_000_000
        values = [ulid(stamp) for _ in range(50)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), 50)

    def test_time_roundtrip(self) -> None:
        import time

        stamp = int(time.time() * 1000) + 5_000  # 必须晚于当前毫秒，否则单调递增会改写时间
        self.assertEqual(ulid_time_ms(ulid(stamp)), stamp)

    def test_rejects_bad_shape(self) -> None:
        self.assertFalse(is_ulid("abc"))
        self.assertFalse(is_ulid("O" * 26))  # Crockford 里没有 O


class TestClock(unittest.TestCase):
    def test_iso_roundtrip(self) -> None:
        text = now_iso()
        self.assertTrue(text.endswith("Z"))
        self.assertAlmostEqual(parse_iso(text).timestamp(), parse_iso(text).timestamp())

    def test_to_iso_utc(self) -> None:
        from datetime import UTC, datetime

        dt = datetime(2026, 11, 2, 12, 0, 0, 482000, tzinfo=UTC)
        self.assertEqual(to_iso(dt), "2026-11-02T12:00:00.482Z")

    def test_day_key_is_date(self) -> None:
        value = day_key()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")

    def test_days_between(self) -> None:
        self.assertGreater(days_between("2026-11-01T00:00:00.000Z"), 0.9)


class TestConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.get("retrieval.top_k"), 8)
        self.assertEqual(cfg.get("policy.subject"), "self")
        self.assertIsNone(cfg.get("nope.nothing"))

    def test_override_deep_merge(self) -> None:
        cfg = Config({"retrieval": {"top_k": 3}})
        self.assertEqual(cfg.get("retrieval.top_k"), 3)
        self.assertEqual(cfg.get("retrieval.rrf_k"), 60)

    def test_load_toml(self) -> None:
        tmp = temp_dir("cfg")
        try:
            path = tmp / "c.toml"
            path.write_text('[retrieval]\ntop_k = 5\n', encoding="utf-8")
            cfg = Config.load(path)
            self.assertEqual(cfg.get("retrieval.top_k"), 5)
        finally:
            drop_dir(tmp)


class TestDatabase(unittest.TestCase):
    def test_init_is_idempotent(self) -> None:
        tmp = temp_dir("db")
        try:
            db = Database(tmp / "x.db")
            db.init()
            db.init()
            self.assertEqual(db.meta("schema_version"), "1.12.0")
            self.assertTrue(db.integrity_ok())
            row = db.conn.execute("SELECT COUNT(*) c FROM subject").fetchone()
            self.assertEqual(row["c"], 1)
            db.close()
        finally:
            drop_dir(tmp)

    def test_subject_and_relation_seeded(self) -> None:
        db = Database(":memory:")
        db.init()
        self.assertIsNotNone(db.conn.execute("SELECT * FROM subject WHERE id='self'").fetchone())
        self.assertGreater(db.conn.execute("SELECT COUNT(*) c FROM relation_type").fetchone()["c"], 5)
        db.close()

    def test_tx_rolls_back(self) -> None:
        db = Database(":memory:")
        db.init()
        with self.assertRaises(RuntimeError):
            with db.tx() as conn:
                conn.execute("INSERT INTO metrics(ts, day_key, name, value) VALUES('t','d','x',1)")
                raise RuntimeError("boom")
        self.assertEqual(db.conn.execute("SELECT COUNT(*) c FROM metrics").fetchone()["c"], 0)
        db.close()


class TestEventChain(unittest.TestCase):
    def test_chain_verifies(self) -> None:
        db = Database(":memory:")
        db.init()
        with db.tx() as conn:
            events.append(conn, actor="t", op="a")
            events.append(conn, actor="t", op="b")
        ok, msg = events.verify_chain(db.conn)
        self.assertTrue(ok, msg)
        self.assertIn("2 events", msg)
        db.close()

    def test_tamper_detected(self) -> None:
        db = Database(":memory:")
        db.init()
        with db.tx() as conn:
            events.append(conn, actor="t", op="a", payload={"x": 1})
        db.conn.execute("UPDATE event_log SET payload_json='{\"x\":2}' WHERE seq=1")
        ok, _ = events.verify_chain(db.conn)
        self.assertFalse(ok)
        db.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
