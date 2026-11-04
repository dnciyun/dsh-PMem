"""event_log 的因果链与导出/导入（M29）。

覆盖四件事：
1. ``idx_event_run`` 索引真的建出来（PRAGMA index_list）；
2. ``export(include_events=True)`` → 空库 import：事件条数一致、哈希链可验、
   payload / prev_hash / hash 逐条一致；而且导入不额外制造本地事件（条数严格相等）；
3. 事件日志是哈希链，**不能**并进已有事件的库：必须明确报错，不假装成功；
4. 补的事件覆盖（entity.merge / reflect.report）真的写出事件。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from pmem.base import events
from pmem.gate.sdk import PMem
from pmem.net import entities
from pmem.reflect import report

from .support import drop_dir, temp_dir

_EVENT_COLUMNS = (
    "seq,id,ts,actor,op,target_tbl,target_id,subject_id,payload_json,idem_key,run_id,prev_hash,hash"
)


def _dump_events(db) -> dict[int, dict]:
    rows = db.conn.execute(f"SELECT {_EVENT_COLUMNS} FROM event_log ORDER BY seq").fetchall()
    return {int(row["seq"]): dict(row) for row in rows}


class EventLogCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("eventlog")
        self.app = PMem.open(Path(self._dir) / "pmem.db")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _other(self, name: str = "other") -> PMem:
        other_dir = temp_dir(f"eventlog-{name}")
        self.addCleanup(drop_dir, other_dir)
        app = PMem.open(other_dir / "pmem.db")
        self.addCleanup(app.close)
        return app

    def _seed(self) -> None:
        """几种真实 op：memory.add / memory.update / memory.feedback / memory.delete。"""
        first = self.app.store.add("导出事件 一：我住在杭州")
        self.app.store.add("导出事件 二：用户喜欢咖啡", provenance="observed")
        third = self.app.store.add("导出事件 三：周三常问财报", provenance="observed")
        self.app.update(first["id"], set_fields={"pinned": True}, reason="验收固定住")
        self.app.feedback(third["id"], "useful", note="有用")
        self.app.delete(third["id"], reason="导出测试：只删除一条")

    # ------------------------------------------------------------------ 索引
    def test_idx_event_run_exists(self) -> None:
        indexes = {
            row["name"]
            for row in self.app.db.conn.execute("PRAGMA index_list(event_log)").fetchall()
        }
        self.assertIn("idx_event_run", indexes)
        # 重复 init（老库/重开）必须幂等
        self.app.db.init()
        indexes_again = {
            row["name"]
            for row in self.app.db.conn.execute("PRAGMA index_list(event_log)").fetchall()
        }
        self.assertIn("idx_event_run", indexes_again)

    def test_legacy_db_without_run_id_gets_migrated(self) -> None:
        """模拟早期/被手改的库：没有 run_id 列时先补列，再幂等建索引。"""
        self.app.store.add("老库迁移：补 run_id 列")
        self.app.close()
        db_path = Path(self._dir) / "pmem.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DROP INDEX IF EXISTS idx_event_run")
            conn.execute("ALTER TABLE event_log DROP COLUMN run_id")
            conn.commit()
            cols = {row[1] for row in conn.execute("PRAGMA table_info(event_log)")}
            self.assertNotIn("run_id", cols)
        finally:
            conn.close()

        reopened = PMem.open(db_path)
        try:
            cols = {row[1] for row in reopened.db.conn.execute("PRAGMA table_info(event_log)")}
            self.assertIn("run_id", cols)
            indexes = {
                row["name"]
                for row in reopened.db.conn.execute("PRAGMA index_list(event_log)").fetchall()
            }
            self.assertIn("idx_event_run", indexes)
            chain_ok, detail = events.verify_chain(reopened.db.conn)
            self.assertTrue(chain_ok, detail)
        finally:
            reopened.close()

    # ------------------------------------------------------- 导出 / 导入往返
    def test_export_import_events_roundtrip(self) -> None:
        self._seed()
        source_events = _dump_events(self.app.db)
        self.assertGreaterEqual(len(source_events), 5)

        text = self.app.export(include_events=True)
        lines = [json.loads(line) for line in text.strip().splitlines()]
        header, rows = lines[0], lines[1:]
        memory_rows = [row for row in rows if row["type"] == "memory"]
        event_rows = [row for row in rows if row["type"] == "event"]
        self.assertEqual(header["counts"]["memory"], len(memory_rows))
        self.assertEqual(header["counts"]["event"], len(event_rows))
        self.assertEqual(len([seq for seq in source_events]), len(event_rows))

        target = self._other()
        result = target.import_bundle(text)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["inserted"], len(memory_rows))
        self.assertEqual(result["data"]["events_input"], len(source_events))
        self.assertEqual(result["data"]["events_inserted"], len(source_events))
        self.assertEqual(result["data"]["ignored"], 0)

        # 1) 条数一致
        target_events = _dump_events(target.db)
        self.assertEqual(len(target_events), len(source_events))
        self.assertEqual(set(target_events), set(source_events))

        # 2) 哈希链可验
        chain_ok, detail = events.verify_chain(target.db.conn)
        self.assertTrue(chain_ok, detail)
        self.assertIn(f"{len(source_events)} events verified", detail)

        # 3) 逐条字段与 payload 一致（抽样 + 全量 hash 比对）
        seqs = sorted(source_events)
        samples = {seqs[0], seqs[len(seqs) // 2], seqs[-1]}
        for seq in seqs:
            src, dst = source_events[seq], target_events[seq]
            self.assertEqual(src["hash"], dst["hash"], f"seq={seq} hash 不一致")
            self.assertEqual(src["prev_hash"], dst["prev_hash"], f"seq={seq} prev_hash 不一致")
            self.assertEqual(src["op"], dst["op"])
            if seq in samples:
                # payload_json 是 canonical JSON：导入端重新序列化后必须逐字节相同
                self.assertEqual(src["payload_json"], dst["payload_json"], f"seq={seq} payload 不一致")
                self.assertEqual(json.loads(src["payload_json"]), json.loads(dst["payload_json"]))

        # 4) 目标库没有因导入多出事件（源库 memory.add 已经记过这些记忆）
        self.assertEqual(
            target.db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"],
            len(source_events),
        )
        # 记忆本身也回来了（软删的那条不在导出里）
        self.assertEqual(
            target.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"],
            len(memory_rows),
        )

    def test_import_events_rejects_nonempty_chain(self) -> None:
        self._seed()
        text = self.app.export(include_events=True)
        target = self._other()
        target.store.add("目标库本来就有事件")
        before = target.db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"]
        result = target.import_bundle(text)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "E_VALIDATION")
        self.assertIn("事件日志", result["error"]["message"])
        self.assertIn("空库", result["error"].get("hint") or "")
        after = target.db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"]
        self.assertEqual(before, after, "失败时不该写入任何事件")

    def test_import_events_dry_run_changes_nothing(self) -> None:
        self._seed()
        text = self.app.export(include_events=True)
        target = self._other()
        result = target.import_bundle(text, dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["data"]["events_inserted"],
            len(_dump_events(self.app.db)),
        )
        self.assertEqual(
            target.db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"], 0
        )
        self.assertEqual(
            target.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"], 0
        )

    def test_default_export_has_no_event_rows(self) -> None:
        """老格式不变：默认导出仍只有记忆行，老导入端也能读。"""
        self._seed()
        text = self.app.export()
        lines = [json.loads(line) for line in text.strip().splitlines()]
        self.assertEqual(lines[0]["counts"]["event"], 0)
        self.assertFalse(any(row["type"] == "event" for row in lines[1:]))
        target = self._other()
        result = target.import_bundle(text)
        self.assertTrue(result["ok"], result)
        # 记忆导入自己的事件照常写，链仍然连续
        self.assertGreater(
            target.db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"], 0
        )
        chain_ok, detail = events.verify_chain(target.db.conn)
        self.assertTrue(chain_ok, detail)

    def test_run_id_survives_export_import(self) -> None:
        """因果链键 run_id 必须原样过去；idx_event_run 能按它查。"""
        with self.app.db.tx() as conn:
            events.append(
                conn,
                actor="system:sleep",
                op="sleep.run",
                target_tbl="sleep_run",
                target_id="run-fixture",
                subject_id="self",
                payload={"day_key": "2026-11-04", "trigger": "manual"},
                run_id="01RUNFIXTURE0000000000000",
            )
        text = self.app.export(include_events=True)
        target = self._other()
        self.assertTrue(target.import_bundle(text)["ok"])
        row = target.db.conn.execute(
            "SELECT run_id FROM event_log WHERE op='sleep.run' AND target_id='run-fixture'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["run_id"], "01RUNFIXTURE0000000000000")
        # 索引可服务 run_id 过滤（EXPLAIN 至少不报错，且索引名在计划里）
        plan = " ".join(
            str(item[3])
            for item in target.db.conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM event_log WHERE run_id='01RUNFIXTURE0000000000000'"
            )
        )
        self.assertIn("idx_event_run", plan)

    # ------------------------------------------------------- 补的事件覆盖
    def test_entity_merge_writes_event(self) -> None:
        self.app.store.add("小王在字节工作")
        self.app.store.add("小王在美团工作")
        rows = entities.find(self.app.db, subject="self", name="小王")
        self.assertEqual(len(rows), 2)
        keep, drop = rows[0]["id"], rows[1]["id"]
        result = entities.merge(self.app.db, keep, drop)
        self.assertEqual(result["merged"], drop)
        row = self.app.db.conn.execute(
            "SELECT * FROM event_log WHERE op='entity.merge' AND target_id=? ORDER BY seq DESC LIMIT 1",
            (keep,),
        ).fetchone()
        self.assertIsNotNone(row, "entity.merge 没有写事件")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["merged_id"], drop)
        self.assertEqual(row["actor"], "system:net")
        self.assertEqual(row["target_tbl"], "entity")
        chain_ok, detail = events.verify_chain(self.app.db.conn)
        self.assertTrue(chain_ok, detail)

    def test_weekly_reflection_writes_event(self) -> None:
        self.app.store.add("反思测试：我住在杭州")
        self.app.store.add("反思测试：用户喜欢咖啡", provenance="observed")
        payload = report.write(self.app.db, subject="self", key="2026-W44")
        self.assertTrue(payload.get("created"))
        row = self.app.db.conn.execute(
            "SELECT * FROM event_log WHERE op='reflect.report' AND target_id=? LIMIT 1",
            (payload["id"],),
        ).fetchone()
        self.assertIsNotNone(row, "reflect.report 没有写事件")
        event_payload = json.loads(row["payload_json"])
        self.assertEqual(event_payload["period"], "week:2026-W44")
        self.assertIn("quality", event_payload)
        chain_ok, detail = events.verify_chain(self.app.db.conn)
        self.assertTrue(chain_ok, detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
