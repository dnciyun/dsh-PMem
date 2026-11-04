"""M9.7b：星图的深空布局。

这里守的是**几何与语义**，不是像素：

- 盘面要真的铺开（不能全挤在内圈——第一版就是那样）；
- 时间秩序要单调（早的在内、新的在外），但不被写作突发压垮；
- 星尘必须落在盘上，能贴到记忆的就要贴上去；
- 抖动必须是确定的（同一条记忆不会在两次生成之间跳位置）；
- 2D 投影必须留在 0~1000 里（旧的 SVG / PNG 渲染器还靠它）。
"""

from __future__ import annotations

import math
import unittest
from pathlib import Path

from pmem.gate.sdk import PMem
from pmem.ops import starmap, starmap_space

from .support import drop_dir, temp_dir


def _radius(node: dict) -> float:
    return math.hypot(float(node["x3"]), float(node["z3"]))


class SpaceLayoutCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m97b")
        self.path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.path)

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _seed(self, count: int = 40) -> None:
        for index in range(count):
            group = "我们" if index % 3 == 0 else ("工作/PMem" if index % 3 == 1 else "生活")
            self.app.add(
                f"第 {index} 条记忆：关于 {group} 的一件小事，编号 {index}。",
                kind="episodic",
                provenance="observed",
                groups=[group],
            )

    # ---- 几何 -------------------------------------------------------------

    def test_disk_is_filled_not_clustered(self) -> None:
        self._seed(48)
        graph = starmap.build(self.app.db)
        radii = sorted(_radius(n) for n in graph["nodes"] if n["type"] == "memory")
        self.assertGreaterEqual(len(radii), 48)
        quarter = radii[len(radii) // 4]
        median = radii[len(radii) // 2]
        three_quarter = radii[(3 * len(radii)) // 4]
        # 第一版的 p25 是 0.21、中位 0.31：三分之二的记忆挤在内圈。
        self.assertGreater(quarter, 0.22, f"内圈太挤：p25={quarter:.3f}")
        self.assertGreater(median, 0.32, f"中位半径偏小：{median:.3f}")
        self.assertGreater(three_quarter, 0.45, f"外圈太空：p75={three_quarter:.3f}")
        self.assertLessEqual(max(radii), starmap_space.R_OUT * 1.06)

    def test_radius_is_monotonic_in_time(self) -> None:
        # 每条用不同的写入顺序；grade 都是 None，所以只有时间在决定半径。
        self._seed(24)
        graph = starmap.build(self.app.db)
        memories = [n for n in graph["nodes"] if n["type"] == "memory" and not n.get("grade")]
        memories.sort(key=lambda n: n["created_at"])
        radii = [_radius(n) for n in memories]
        first, last = radii[0], radii[-1]
        self.assertLess(first, last, "最早的那条应该比最新的更靠里")
        # 允许局部回退（松弛会推挤），但整体相关要强。
        inversions = sum(1 for a, b in zip(radii, radii[1:]) if b < a - 0.12)
        self.assertLess(inversions, max(2, len(radii) // 6), f"半径顺序太乱：{inversions} 处明显回退")

    def test_groups_become_arms(self) -> None:
        self._seed(36)
        graph = starmap.build(self.app.db)
        arms = {n.get("arm") for n in graph["nodes"] if n["type"] == "memory"}
        self.assertGreaterEqual(len([a for a in arms if a]), 2, f"没有分组扇区：{arms}")
        self.assertEqual(graph["shape"]["layout"], "spiral-disk")
        self.assertEqual(graph["shape"]["kind"], "organic")
        self.assertGreaterEqual(graph["space"]["sectors"], 2)

    def test_projection_stays_inside_the_canvas(self) -> None:
        self._seed(30)
        graph = starmap.build(self.app.db)
        for node in graph["nodes"]:
            self.assertGreaterEqual(node["x"], 0, node["id"])
            self.assertLessEqual(node["x"], 1000, node["id"])
            self.assertGreaterEqual(node["y"], 0, node["id"])
            self.assertLessEqual(node["y"], 1000, node["id"])

    def test_layout_is_deterministic(self) -> None:
        self._seed(20)
        one = starmap.build(self.app.db)
        two = starmap.build(self.app.db)
        first = {n["id"]: (round(n["x3"], 6), round(n["y3"], 6), round(n["z3"], 6)) for n in one["nodes"]}
        second = {n["id"]: (round(n["x3"], 6), round(n["y3"], 6), round(n["z3"], 6)) for n in two["nodes"]}
        self.assertEqual(first, second)

    def test_node_visual_size_is_not_world_radius(self) -> None:
        self._seed(6)
        graph = starmap.build(self.app.db)
        for node in graph["nodes"]:
            # 视觉尺寸是个位数；世界半径 ≤1.1。两者混用会算出 1500 像素的星。
            self.assertLess(node["size"], 20, node["id"])
            self.assertLess(node["glow"], 30, node["id"])
            self.assertGreaterEqual(node["spike"], 0.0)
            self.assertLessEqual(node["spike"], 1.0)

    # ---- 星尘 -------------------------------------------------------------

    def test_dust_comes_from_logs_not_imagination(self) -> None:
        self._seed(12)
        graph = starmap.build(self.app.db)
        dust = graph["dust"]
        self.assertGreater(len(dust), 0, "事件日志没有变成星尘")
        kinds = {item["k"] for item in dust}
        self.assertIn("event", kinds)
        for item in dust:
            for key in ("x3", "y3", "z3"):
                self.assertIn(key, item, item.get("id"))
                self.assertIsInstance(item[key], float)
            self.assertLessEqual(math.hypot(item["x3"], item["z3"]), starmap_space.R_OUT * 1.3)

    def test_dust_anchors_to_memories_when_it_can(self) -> None:
        self._seed(8)
        graph = starmap.build(self.app.db)
        memory_ids = {n["id"][4:] for n in graph["nodes"] if n["type"] == "memory"}
        anchored = [d for d in graph["dust"] if d.get("m") in memory_ids]
        self.assertGreater(len(anchored), 0, "没有任何星尘贴到记忆上")
        by_id = {n["id"]: n for n in graph["nodes"]}
        for item in anchored[:12]:
            memory = by_id.get("mem:" + item["m"])
            self.assertIsNotNone(memory)
            distance = math.dist(
                (item["x3"], item["y3"], item["z3"]),
                (memory["x3"], memory["y3"], memory["z3"]),
            )
            self.assertLess(distance, 0.12, f"星尘离它那条记忆太远：{distance:.3f}")

    # ---- 时间轴 -----------------------------------------------------------

    def test_history_is_cumulative(self) -> None:
        self._seed(15)
        graph = starmap.build(self.app.db)
        history = graph["history"]
        self.assertGreaterEqual(len(history), 1)
        total = sum(row["count"] for row in history)
        self.assertEqual(total, graph["meta"]["shown"])
        self.assertEqual(history[-1]["cumulative"], total)
        running = 0
        for row in history:
            running += row["count"]
            self.assertEqual(row["cumulative"], running)
        self.assertEqual(graph["meta"]["timeline"]["dust"], len(graph["dust"]))

    def test_cursor_thresholds_are_ordered(self) -> None:
        self._seed(10)
        graph = starmap.build(self.app.db)
        values = [n["t"] for n in graph["nodes"] if n["type"] == "memory"]
        self.assertTrue(all(0.0 <= v <= 1.0 for v in values), values)
        self.assertAlmostEqual(min(values), 0.0, places=6)
        self.assertAlmostEqual(max(values), 1.0, places=6)


class IncrementalCase(unittest.TestCase):
    """M9.7c：增量布点。

    页面每隔一两秒就可能重画一次。要求只有一条，但很硬：
    **已经画过的记忆，位置、高度、时间点一个都不许变。** 新记忆自己找位置。
    """

    def setUp(self) -> None:
        self._dir = temp_dir("m97c")
        self.app = PMem.open(Path(self._dir) / "pmem.db")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _add(self, text: str, group: str = "我们") -> None:
        self.app.add(text, kind="episodic", provenance="observed", groups=[group])

    @staticmethod
    def _pos(graph: dict) -> dict[str, tuple]:
        return {
            node["id"]: (node["x3"], node["y3"], node["z3"], node.get("t"))
            for node in graph["nodes"]
            if node["type"] == "memory"
        }

    def test_old_stars_do_not_move_when_a_memory_appears(self) -> None:
        for index in range(24):
            self._add(f"增量测试的第 {index} 条记忆，写在同一组里。")
        first = starmap.build(self.app.db)
        before = self._pos(first)

        self._add("增量测试：这一条是后来才写的。")
        second = starmap.build(self.app.db, prev=first)
        after = self._pos(second)

        self.assertEqual(len(after), len(before) + 1)
        moved = {key: (before[key], after[key]) for key in before if before[key] != after[key]}
        self.assertEqual(moved, {}, f"旧记忆被挪动了：{list(moved)[:3]}")
        self.assertEqual(second["meta"]["incremental"]["locked"], len(before))
        self.assertEqual(second["meta"]["incremental"]["fresh"], 1)
        self.assertTrue(second["shape"]["incremental"])

    def test_new_star_lands_on_the_disk(self) -> None:
        for index in range(12):
            self._add(f"落位测试的第 {index} 条。")
        first = starmap.build(self.app.db)
        self._add("落位测试：新的一颗应当落在盘上，而不是原点。")
        second = starmap.build(self.app.db, prev=first)

        fresh = [
            n for n in second["nodes"]
            if n["type"] == "memory" and n["id"] not in self._pos(first)
        ]
        self.assertEqual(len(fresh), 1)
        node = fresh[0]
        radius = math.hypot(float(node["x3"]), float(node["z3"]))
        self.assertGreater(radius, starmap_space.R_IN)
        self.assertLessEqual(radius, starmap_space.R_OUT * 1.06)
        # 新记忆是最新的，应当落在外面（时间秩序没有被增量模式破坏）。
        self.assertGreaterEqual(node["t"], max(v[3] for v in self._pos(first).values()))

    def test_dust_is_reused_not_recomputed(self) -> None:
        for index in range(10):
            self._add(f"星尘复用的第 {index} 条。")
        first = starmap.build(self.app.db)
        self.assertGreater(len(first["dust"]), 0)
        before = {item["id"]: (item["x3"], item["y3"], item["z3"]) for item in first["dust"]}

        second = starmap.build(self.app.db, prev=first)
        after = {
            item["id"]: (item["x3"], item["y3"], item["z3"])
            for item in second["dust"]
            if item["id"] in before
        }
        self.assertTrue(after)
        self.assertEqual(
            {key: after[key] for key in after if after[key] != before[key]}, {},
            "已在盘上的星尘被重算了，画面会抖",
        )
        self.assertGreater(second["meta"]["incremental"]["reused_dust"], 0)

    def test_without_prev_it_is_a_full_layout(self) -> None:
        for index in range(8):
            self._add(f"全量模式的第 {index} 条。")
        graph = starmap.build(self.app.db)
        self.assertEqual(graph["meta"]["incremental"]["locked"], 0)
        self.assertFalse(graph["shape"]["incremental"])
        again = starmap.build(self.app.db)
        self.assertEqual(self._pos(graph), self._pos(again), "全量布点必须是确定的")

    def test_cli_incremental_only_writes_json(self) -> None:
        for index in range(6):
            self._add(f"命令行增量的第 {index} 条。")
        base = Path(self._dir) / "map"
        full = starmap.write(self.app.db, out=base, png=False)
        self.assertIsNotNone(full["html"])
        existed = Path(full["html"]).stat().st_mtime_ns

        again = starmap.write(self.app.db, out=base, png=False, incremental=True)
        self.assertTrue(again["incremental"])
        self.assertIsNone(again["html"], "增量模式不该重画离线单文件")
        self.assertIsNone(again["svg"])
        self.assertEqual(Path(full["html"]).stat().st_mtime_ns, existed, "离线文件被重写了")

    def test_first_incremental_run_falls_back_to_full(self) -> None:
        self._add("第一次跑增量的时候还没有上一版。")
        base = Path(self._dir) / "fresh"
        result = starmap.write(self.app.db, out=base, png=False, incremental=True)
        self.assertFalse(result["incremental"])
        self.assertIsNotNone(result["html"])
        self.assertTrue(Path(result["json"]).exists())


class OfflineExportCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m97b-html")
        self.app = PMem.open(Path(self._dir) / "pmem.db")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_single_file_export_inlines_the_real_renderer(self) -> None:
        self.app.add("离线导出的测试记忆：星图要能双击打开。", kind="semantic", provenance="observed")
        from pmem.ops import starmap_html

        self.assertIsNotNone(starmap_html.asset_dir(), "找不到 deploy/dsh-pmem/starmap")
        result = starmap.write(self.app.db, out=Path(self._dir) / "map", png=False)
        html = Path(result["html"]).read_text(encoding="utf-8")
        self.assertEqual(html.count("</script>"), 1, "离线单文件只能有一个 script 闭合标签")
        self.assertIn("window.__PMEM_DATA__", html)
        self.assertIn("PMemStar", html, "渲染器没有被内联进去")
        self.assertIn("SIZE_UNIT", html)
        self.assertEqual(html.count("</style>"), 1, "样式只能内联一次")
        self.assertLess(len(html), 900_000, "单文件太大了，检查是不是把重复数据塞进去了")


class SourceColorCase(unittest.TestCase):
    """来源着色（M18）：日记派生的记忆在图上要能被认出来。

    为什么要单独守：等级色是「多重要」，来源是「这话从哪来」。同等级的两条记忆，
    一条是平时写的、一条是从那天日记派生的，扫一眼就该分得开——
    否则日记层做完了，图上等于没做。
    """

    def setUp(self) -> None:
        self._dir = temp_dir("m97b-src")
        self.app = PMem.open(Path(self._dir) / "pmem.db")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _memory_nodes(self) -> list[dict]:
        graph = starmap.collect(self.app.db)
        return [node for node in graph["nodes"] if node["type"] == "memory"]

    def test_journal_memory_carries_its_source(self) -> None:
        self.app.store.add("用户昨晚看了石头门第十二集", kind="episodic", grade="C", source_kind="journal")
        node = self._memory_nodes()[0]
        self.assertEqual(node["source_kind"], "journal")
        self.assertEqual(starmap._node_color(node), starmap.JOURNAL_COLOR)

    def test_source_color_beats_grade_color(self) -> None:
        # 日记派生的也可能有分级；来源优先，否则「从日记来的」被等级色盖掉。
        self.app.store.add("用户做饭把锅烧糊了", kind="episodic", grade="A", source_kind="journal")
        node = self._memory_nodes()[0]
        self.assertEqual(node["grade"], "A")
        self.assertEqual(starmap._node_color(node), starmap.JOURNAL_COLOR)

    def test_normal_memory_keeps_grade_color(self) -> None:
        self.app.store.add("用户养了一只布偶猫", kind="episodic", grade="B", source_kind="dsh_session")
        node = self._memory_nodes()[0]
        self.assertNotEqual(starmap._node_color(node), starmap.JOURNAL_COLOR)
        self.assertEqual(starmap._node_color(node), starmap.GRADE_COLOR["B"])

    def test_journal_color_collides_with_nothing(self) -> None:
        colors = set(starmap.GRADE_COLOR.values()) | set(starmap.KIND_COLOR.values())
        self.assertNotIn(starmap.JOURNAL_COLOR, colors)


if __name__ == "__main__":
    unittest.main()
