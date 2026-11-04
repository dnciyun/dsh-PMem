"""M4 补充：续跑与「一次跑完」的等价性、阶段事件幂等键、跨连接单飞。

覆盖审计（.tmp/review/sleep-idempotency.md §2.4/§7.1/§7.2）暴露的四件事：

1. 续跑跳过 s1 时必须重放它的只读查询，否则 s3 对着空工作集空转（续跑 ≠ 一次跑完）；
2. 阶段事件带 ``idem_key=f"{run_id}:{stage}"`` 与 ``run_id``，同一 run 同一阶段只能提交一次；
3. 别的进程已提交的阶段要"采纳"而不是重跑；
4. 抢跑撞键时整个阶段事务回滚、采纳赢家 stats，不产生第二份副作用（s8 不留同 path 两行快照）。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from pmem.base import events
from pmem.base.clock import to_iso
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.sleep import orchestrator

from .support import drop_dir, temp_dir

LIVE = ["我住在杭州", "我目前住在杭州", "我现在住在杭州"]
LIKE = [
    "我喜欢咖啡",
    "我特别喜欢咖啡",
    "我一直喜欢咖啡",
    "我挺喜欢咖啡",
    "我偏爱咖啡",
    "我很喜欢咖啡",
]
EXPERIENCE = {
    "situation": "给陌生项目做技术选型",
    "action": "先跑基线再优化",
    "outcome": "success",
    "lesson": "先建立可复现基线",
}

# 关掉写入时抽取，模拟"批量导入/历史库"——只有睡眠 s3 会补抽实体。
IMPORTED = [
    "小鲸鲸今天在重构睡眠编排器，把 checkpoint 写进 sleep_run",
    "Mira 喜欢在晚上跑一遍睡眠，看看当天坏了什么",
    "用户说重跑睡眠不能重复写事实，这是硬要求",
    "今天试了三次 sleep run，第一次发现 event_log 会涨",
    "小鲸鲸觉得 checkpoint_json 比 stage 表更简单",
    "Mira 担心阶段失败后从头跑会重复副作用",
    "PMem 的 s8 快照用 VACUUM INTO 生成紧凑副本",
    "小鲸鲸说 s2 去重要靠 claim_sig 和近重复两条路",
]

# 等价性只比"该由输入决定"的字段：id / path / sha / size 天然因库不同而不同。
STAGE_KEYS: dict[str, tuple[str, ...]] = {
    "s1_collect": ("memories", "work", "backlog", "by_kind", "feedback", "recalls"),
    "s2_dedup": ("clusters", "candidates", "merged", "skipped", "groups_archived"),
    "s3_relations": (
        "scanned",
        "mentions",
        "relations_created",
        "relations_reinforced",
        "cooccurrence",
        "transitive_created",
        "dormant",
    ),
    "s4_conflicts": ("found", "created", "skipped"),
    "s5_patterns": (
        "patterns_created",
        "patterns_updated",
        "patterns_stale",
        "patterns_by_kind",
        "experience_created",
        "experience_updated",
        "experience_incomplete",
        "experience_invalid",
    ),
    "s6_traits": (
        "candidates",
        "created",
        "versioned",
        "demoted",
        "unchanged",
        "updated_in_place",
        "retired",
        "capacity_retired",
        "active",
        "established",
    ),
    "s7_reflect": (
        "lifecycle_moved",
        "lifecycle_dry_run",
        "reflection_created",
        "conflicts_open",
        "memories",
        "quality",
        "defects",
    ),
    "s8_snapshot": ("memory_count", "verified", "skipped", "kind"),
}


def stamp_for(days_ago: int) -> str:
    return to_iso(datetime.now().astimezone() - timedelta(days=days_ago))


def day_for(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def seed(app: PMem, days: tuple[int, ...] = (3, 2, 1)) -> list[str]:
    """跨 3 天的合成语料：能压出 pattern / trait / experience，s3 也有实体可抽。"""
    ids: list[str] = []
    for index, offset in enumerate(days):
        stamp = stamp_for(offset)
        session = f"s{offset}"
        ids.append(app.store.add(LIVE[index % len(LIVE)], created_at=stamp, session_id=session)["id"])
        ids.append(
            app.store.add(LIKE[index * 2 % len(LIKE)], created_at=stamp, session_id=session)["id"]
        )
        ids.append(
            app.store.add(LIKE[index * 2 + 1 % len(LIKE)], created_at=stamp, session_id=session)["id"]
        )
        ids.append(app.store.add("我在字节工作", created_at=stamp, session_id=session)["id"])
        ids.append(
            app.store.add(
                f"给陌生项目做技术选型时先跑基线再优化，否则无法判断收益（第{index}次）",
                kind="experience",
                extra=EXPERIENCE,
                created_at=stamp,
                session_id=session,
            )["id"]
        )
    return ids


def seed_imported(app: PMem) -> list[str]:
    """同一天、写入时不抽实体（需要 config 关掉 net.auto_extract）。"""
    stamp = stamp_for(1)
    return [
        app.store.add(text, created_at=stamp, session_id="imp")["id"] for text in IMPORTED
    ]


def stage_stats(run: dict) -> dict[str, dict]:
    return {stage["name"]: stage.get("stats") or {} for stage in run["stages"]}


def diff_stage_stats(
    left: dict[str, dict], right: dict[str, dict]
) -> dict[str, dict[str, tuple[object, object]]]:
    diffs: dict[str, dict[str, tuple[object, object]]] = {}
    for name, keys in STAGE_KEYS.items():
        for key in keys:
            a = left.get(name, {}).get(key)
            b = right.get(name, {}).get(key)
            if name == "s7_reflect" and key == "quality" and a is not None and b is not None:
                a, b = round(float(a), 3), round(float(b), 3)
            if a != b:
                diffs.setdefault(name, {})[key] = (a, b)
    return diffs


def table_counts(app: PMem, tables: tuple[str, ...]) -> dict[str, int]:
    return {
        table: app.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        for table in tables
    }


DERIVED = (
    "entity",
    "entity_mention",
    "relation",
    "conflict",
    "pattern",
    "experience",
    "trait",
    "reflection",
    "snapshot",
    "metrics",
)


class SleepResumeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self.app = self._open("sleep-eq")

    def tearDown(self) -> None:
        try:
            self.app.close()
        finally:
            for directory in self._dirs:
                drop_dir(directory)

    def _open(self, prefix: str, *, config: Config | None = None) -> PMem:
        directory = temp_dir(prefix)
        self._dirs.append(directory)
        return PMem.open(Path(directory) / "pmem.db", config=config)


class TestResumeEquivalence(SleepResumeBase):
    def test_resumed_stage_stats_match_full_run(self) -> None:
        """停 s2 后续跑，s1–s8 的关键 stats 与一次跑完逐项一致（不只比行数）。"""
        full_app = self._open("sleep-eq-full")
        try:
            seed(full_app)
            full = full_app.sleep(day_key=day_for(1), resume=False)["data"]
            self.assertEqual(full["status"], "ok")
            full_stats = stage_stats(full)
            full_counts = table_counts(full_app, DERIVED)
        finally:
            full_app.close()

        seed(self.app)
        partial = self.app.sleep(day_key=day_for(1), resume=False, stop_at="s2_dedup")["data"]
        self.assertEqual(partial["status"], "partial")
        resumed = self.app.sleep(day_key=day_for(1), resume=True)["data"]
        self.assertEqual(resumed["status"], "ok")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["run_id"], partial["run_id"])

        row = self.app.db.conn.execute(
            "SELECT status FROM sleep_run WHERE id=?", (resumed["run_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "ok")

        diffs = diff_stage_stats(full_stats, stage_stats(resumed))
        self.assertEqual(diffs, {}, f"续跑与一次跑完阶段 stats 不一致: {diffs}")

        # 这次修复的核心：s3 处理的就是 s1 的工作集，不再空转
        resumed_stats = stage_stats(resumed)
        self.assertGreater(resumed_stats["s1_collect"]["work"], 0)
        self.assertEqual(
            resumed_stats["s3_relations"]["scanned"], resumed_stats["s1_collect"]["work"]
        )
        self.assertEqual(table_counts(self.app, DERIVED), full_counts)

    def test_resume_without_add_time_extraction_matches_full(self) -> None:
        """写入时不抽实体的库（导入/历史库）：续跑也必须补出同样的 entity/mention。"""
        cfg = Config.load(None, overrides={"net": {"auto_extract": False}})
        full_app = self._open("sleep-eq-imp-full", config=cfg)
        resume_app = self._open("sleep-eq-imp-resume", config=cfg)
        try:
            seed_imported(full_app)
            full = full_app.sleep(day_key=day_for(1), resume=False)["data"]
            self.assertEqual(full["status"], "ok")
            full_stats = stage_stats(full)
            full_tables = table_counts(full_app, DERIVED)

            seed_imported(resume_app)
            partial = resume_app.sleep(day_key=day_for(1), resume=False, stop_at="s2_dedup")["data"]
            self.assertEqual(partial["status"], "partial")
            resumed = resume_app.sleep(day_key=day_for(1), resume=True)["data"]
            self.assertEqual(resumed["status"], "ok")
            resumed_stats = stage_stats(resumed)

            # 关掉写入时抽取后，实体只能由 s3 补抽出来；续跑不能空转。
            self.assertGreater(resumed_stats["s3_relations"]["mentions"], 0)
            self.assertEqual(resumed_stats["s3_relations"]["scanned"], len(IMPORTED))
            self.assertEqual(diff_stage_stats(full_stats, resumed_stats), {})
            self.assertEqual(table_counts(resume_app, DERIVED), full_tables)
        finally:
            full_app.close()
            resume_app.close()


class TestStageIdempotencyKeys(SleepResumeBase):
    def test_sleep_events_carry_idem_key_and_run_id(self) -> None:
        seed(self.app)
        run = self.app.sleep(day_key=day_for(1), resume=False)["data"]
        run_id = run["run_id"]

        stage_rows = self.app.db.conn.execute(
            "SELECT idem_key, run_id, payload_json FROM event_log "
            "WHERE op='sleep.stage' AND target_id=?",
            (run_id,),
        ).fetchall()
        self.assertEqual(len(stage_rows), len(orchestrator.STAGES))
        self.assertEqual(
            {row["idem_key"] for row in stage_rows},
            {f"{run_id}:{name}" for name in orchestrator.STAGES},
        )
        self.assertEqual({row["run_id"] for row in stage_rows}, {run_id})

        finish_rows = self.app.db.conn.execute(
            "SELECT idem_key, run_id FROM event_log WHERE op='sleep.finish' AND target_id=?",
            (run_id,),
        ).fetchall()
        self.assertEqual(len(finish_rows), 1)
        self.assertTrue(finish_rows[0]["idem_key"].startswith(f"{run_id}:finish:"))
        self.assertEqual(finish_rows[0]["run_id"], run_id)

        run_rows = self.app.db.conn.execute(
            "SELECT run_id FROM event_log WHERE op='sleep.run' AND target_id=?", (run_id,)
        ).fetchall()
        self.assertEqual(len(run_rows), 1)
        self.assertEqual(run_rows[0]["run_id"], run_id)

    def test_event_idem_key_is_unique(self) -> None:
        """event_log.idem_key 唯一：同键第二条在事务里直接撞 IntegrityError（整笔回滚）。"""
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.db.tx() as conn:
                events.append(conn, actor="test", op="sleep.test", idem_key="dup-key")
                events.append(conn, actor="test", op="sleep.test", idem_key="dup-key")
        count = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE idem_key='dup-key'"
        ).fetchone()["c"]
        self.assertEqual(count, 0)


class TestSingleFlight(SleepResumeBase):
    def test_committed_stages_are_adopted_not_rerun(self) -> None:
        """别的进程已提交 s3–s7：续跑应采纳它们，不重复执行、不产生第二条事件。"""
        seed(self.app)
        partial = self.app.sleep(day_key=day_for(1), resume=False, stop_at="s2_dedup")["data"]
        run_id = partial["run_id"]
        adopted_stages = ("s3_relations", "s4_conflicts", "s5_patterns", "s6_traits", "s7_reflect")
        with self.app.db.tx() as conn:
            for name in adopted_stages:
                events.append(
                    conn,
                    actor="system:sleep",
                    op="sleep.stage",
                    target_tbl="sleep_run",
                    target_id=run_id,
                    subject_id="self",
                    payload={"day_key": day_for(1), "stage": name, "stats": {"by_other": True}},
                    idem_key=f"{run_id}:{name}",
                    run_id=run_id,
                )

        called: list[str] = []
        real = dict(orchestrator.STAGE_FNS)

        def spy(name: str):
            def inner(ctx: object) -> dict:
                called.append(name)
                return real[name](ctx)  # type: ignore[operator]

            return inner

        with mock.patch.dict(
            orchestrator.STAGE_FNS, {name: spy(name) for name in adopted_stages}, clear=False
        ):
            resumed = self.app.sleep(day_key=day_for(1), resume=True)["data"]

        self.assertEqual(resumed["status"], "ok")
        for name in adopted_stages:
            self.assertNotIn(name, called, f"{name} 被重复执行了")
        stats = stage_stats(resumed)
        for name in adopted_stages:
            self.assertEqual(stats[name], {"by_other": True}, name)
        for name in adopted_stages:
            count = self.app.db.conn.execute(
                "SELECT COUNT(*) c FROM event_log WHERE idem_key=?", (f"{run_id}:{name}",)
            ).fetchone()["c"]
            self.assertEqual(count, 1, name)

    def test_collision_rolls_back_and_adopts_winner(self) -> None:
        """抢跑撞键：本进程阶段事务整体回滚，采纳赢家 stats，整轮不判失败。"""
        seed(self.app)
        partial = self.app.sleep(day_key=day_for(1), resume=False, stop_at="s4_conflicts")["data"]
        run_id = partial["run_id"]
        # 模拟"另一进程在我们就绪后才提交 s5"：预写事件，但让本进程的 pre-check 假装没看见。
        with self.app.db.tx() as conn:
            events.append(
                conn,
                actor="system:sleep",
                op="sleep.stage",
                target_tbl="sleep_run",
                target_id=run_id,
                subject_id="self",
                payload={"day_key": day_for(1), "stage": "s5_patterns", "stats": {"winner": True}},
                idem_key=f"{run_id}:s5_patterns",
                run_id=run_id,
            )

        real_committed = orchestrator._committed_stage
        seen = {"s5_patterns": 0}

        def flaky_committed(db: object, run_id_arg: str, name: str) -> dict | None:
            if name == "s5_patterns" and seen["s5_patterns"] == 0:
                seen["s5_patterns"] += 1
                return None  # 第一次 pre-check 还没看见（另一进程刚好在之后提交）
            return real_committed(db, run_id_arg, name)  # type: ignore[arg-type]

        with mock.patch.object(orchestrator, "_committed_stage", flaky_committed):
            resumed = self.app.sleep(day_key=day_for(1), resume=True)["data"]

        self.assertEqual(resumed["status"], "ok")
        self.assertIsNone(resumed["error"])
        stats = stage_stats(resumed)
        self.assertEqual(stats["s5_patterns"], {"winner": True})
        count = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE idem_key=?", (f"{run_id}:s5_patterns",)
        ).fetchone()["c"]
        self.assertEqual(count, 1)
        # 后面的阶段照常跑完
        self.assertEqual(
            [stage["name"] for stage in resumed["stages"] if not stage["skipped"]],
            ["s6_traits", "s7_reflect", "s8_snapshot"],
        )

    def test_loser_snapshot_row_is_dropped(self) -> None:
        """并发 s8 输家多插的快照行要删掉；赢家那行（或同一行）不动。"""
        with self.app.db.tx() as conn:
            for snapshot_id in ("keep-me", "drop-me"):
                conn.execute(
                    "INSERT INTO snapshot(id, created_at, path, size_bytes, sha256, "
                    "schema_version, memory_count, kind) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        "2026-11-01T00:00:00Z",
                        f"backups/{snapshot_id}.db",
                        0,
                        "sha",
                        "1.10.0",
                        0,
                        "daily",
                    ),
                )

        def ids() -> set[str]:
            return {
                row[0]
                for row in self.app.db.conn.execute("SELECT id FROM snapshot").fetchall()
            }

        orchestrator._drop_loser_snapshot(
            self.app.db, keep_stats={"snapshot_id": "keep-me"}, drop_stats={"snapshot_id": "drop-me"}
        )
        self.assertIn("keep-me", ids())
        self.assertNotIn("drop-me", ids())
        # 同一个 id、或有一边没 id：不乱删
        orchestrator._drop_loser_snapshot(
            self.app.db, keep_stats={"snapshot_id": "keep-me"}, drop_stats={"snapshot_id": "keep-me"}
        )
        orchestrator._drop_loser_snapshot(
            self.app.db, keep_stats={}, drop_stats={"snapshot_id": "keep-me"}
        )
        self.assertEqual(ids(), {"keep-me"})

    def test_two_connections_resume_single_flight(self) -> None:
        """两个连接并发续跑同一 partial run：阶段只提交一次、快照只有一行。"""
        seed(self.app)
        partial = self.app.sleep(day_key=day_for(1), resume=False, stop_at="s2_dedup")["data"]
        run_id = partial["run_id"]
        db_path = Path(self.app.db.path)

        # 在 s3 的幂等键预检处会师：不持有写锁，保证两个连接都拿到同一个 run 再开跑。
        gate = threading.Barrier(2, timeout=60)
        gate_lock = threading.Lock()
        gate_calls = {"count": 0}
        real_committed = orchestrator._committed_stage

        def gated_committed(db: object, run_id_arg: str, name: str) -> dict | None:
            if name == "s3_relations":
                with gate_lock:
                    gate_calls["count"] += 1
                    arrivals = gate_calls["count"]
                if arrivals <= 2:
                    gate.wait()
            return real_committed(db, run_id_arg, name)  # type: ignore[arg-type]

        executed: dict[str, int] = {}
        real = dict(orchestrator.STAGE_FNS)

        def counting_s3(ctx: object) -> dict:
            executed["s3_relations"] = executed.get("s3_relations", 0) + 1
            return real["s3_relations"](ctx)  # type: ignore[operator]

        results: dict[str, dict] = {}
        errors: list[BaseException] = []

        def worker(tag: str) -> None:
            try:
                app = PMem.open(db_path)
                try:
                    results[tag] = app.sleep(day_key=day_for(1), resume=True)
                finally:
                    app.close()
            except BaseException as exc:  # noqa: BLE001 收集到主线程断言
                errors.append(exc)
            finally:
                with contextlib.suppress(Exception):
                    gate.abort()

        with mock.patch.object(orchestrator, "_committed_stage", gated_committed), mock.patch.dict(
            orchestrator.STAGE_FNS, {"s3_relations": counting_s3}
        ):
            threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(180)

        self.assertFalse(any(thread.is_alive() for thread in threads), "并发续跑卡住")
        self.assertEqual(errors, [])
        self.assertEqual({item["data"]["status"] for item in results.values()}, {"ok"})
        self.assertEqual({item["data"]["run_id"] for item in results.values()}, {run_id})
        self.assertEqual(executed.get("s3_relations"), 2, "两个连接都应跑到 s3（证明是并发场景）")

        for name in orchestrator.STAGES:
            count = self.app.db.conn.execute(
                "SELECT COUNT(*) c FROM event_log WHERE idem_key=?", (f"{run_id}:{name}",)
            ).fetchone()["c"]
            self.assertEqual(count, 1, f"{name} 阶段被提交了不止一次")
        finish_count = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE op='sleep.finish' AND target_id=?",
            (run_id,),
        ).fetchone()["c"]
        # 第 1 次 = stop_at 的 partial 收尾；第 2 次 = 续跑后的最终收尾。并发不产生第 3 次。
        self.assertEqual(finish_count, 2)

        snapshot_rows = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM snapshot"
        ).fetchone()["c"]
        self.assertEqual(snapshot_rows, 1, "并发 s8 不该留下同 path 两行快照")

        # 派生事实不能因为并发而多出来：与"同种子 + 一次跑完"的库逐表对比
        baseline_app = self._open("sleep-eq-baseline")
        try:
            seed(baseline_app)
            self.assertEqual(
                baseline_app.sleep(day_key=day_for(1), resume=False)["data"]["status"], "ok"
            )
            baseline_counts = table_counts(baseline_app, DERIVED)
        finally:
            baseline_app.close()
        self.assertEqual(table_counts(self.app, DERIVED), baseline_counts)
        run_rows = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM sleep_run WHERE day_key=?", (day_for(1),)
        ).fetchone()["c"]
        self.assertEqual(run_rows, 1)
        # 两个连接都报告 ok，且最终 run 行是 ok
        final = self.app.db.conn.execute(
            "SELECT status, stats_json FROM sleep_run WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual(final["status"], "ok")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
