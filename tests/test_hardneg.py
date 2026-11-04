"""M29：硬负例样本（46 → 318）的生成规则与合并方式。

样本扩容的坑在「造出来的题不像真问题」：所以这里逐条验证三件事——
1. 问句必须是原题（人写的），不能是机器拼的；
2. 诱饵必须来自同一条语料里真实存在的另一条记忆，且不在目标/答案里；
3. 生成是确定性的：跑两遍字节一致，diff 不会乱跳。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEG_FILE = ROOT / "testdata" / "hard-negatives.json"
CORPUS = ROOT / "testdata" / "corpus-90d"


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _norm(text: object) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()【】\[\]…·—-]+", "", str(text or ""))


class TestNegativesFile(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(NEG_FILE.read_text(encoding="utf-8"))
        self.personas = self.payload["personas"]

    def test_count_and_personas(self) -> None:
        synthetic = sum(len(v) for v in self.personas.values())
        original = sum(
            sum(
                1
                for p in json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))["probes"]
                if p.get("category") in ("precision", "no_memory")
            )
            for name in self.personas
        )
        # 目标：干扰题总量从 46 扩到 300+；合成部分单独看也要够一个像样的样本
        self.assertGreaterEqual(synthetic + original, 300)
        self.assertGreaterEqual(synthetic, 250)
        self.assertEqual(len(self.personas), 10)
        for name, probes in self.personas.items():
            self.assertGreaterEqual(len(probes), 10, name)

    def test_queries_are_original_and_distinct_enough(self) -> None:
        seen_ids: set[str] = set()
        for name, probes in self.personas.items():
            original = {
                p["query"]
                for p in json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))["probes"]
            }
            for probe in probes:
                self.assertEqual(probe["origin"], "synthetic")
                self.assertEqual(probe["category"], "precision")
                self.assertIn(probe["query"], original, "问句必须来自原题")
                self.assertNotIn(probe["id"], seen_ids)
                seen_ids.add(probe["id"])
        distinct = len({p["query"] for probes in self.personas.values() for p in probes})
        self.assertGreaterEqual(distinct, 100)

    def test_decoys_exist_in_corpus_but_not_in_target(self) -> None:
        for name, probes in self.personas.items():
            corpus = json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))
            unit_text = {
                u["id"]: _norm(u["text"])
                for day in corpus.get("days") or []
                for u in day.get("units") or []
            }
            all_text = "".join(unit_text.values())
            for probe in probes:
                unit_ids = probe.get("unit_ids") or []
                target_text = "".join(unit_text.get(uid, "") for uid in unit_ids)
                answers = [_norm(a) for a in (probe.get("expect") or {}).get("any") or []]
                for decoy in (probe.get("expect") or {}).get("none") or []:
                    self.assertTrue(_norm(decoy), "诱饵不能为空")
                    self.assertIn(_norm(decoy), all_text, f"诱饵必须是语料里真有的词：{decoy}")
                    self.assertNotIn(_norm(decoy), target_text, f"诱饵不该出现在目标里：{decoy}")
                    for answer in answers:
                        self.assertNotIn(_norm(decoy), answer)
                self.assertIn(probe.get("hardness"), (1, 2, 3))

    def test_generation_is_deterministic(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "gen_hard_negatives", ROOT / "scripts" / "gen_hard_negatives.py"
        )
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        first = {}
        for path in sorted(gen.DATA.glob("p*.json")):
            corpus = json.loads(path.read_text(encoding="utf-8"))
            first[str(corpus.get("id"))] = gen.build_persona(corpus)
        second = {}
        for path in sorted(gen.DATA.glob("p*.json")):
            corpus = json.loads(path.read_text(encoding="utf-8"))
            second[str(corpus.get("id"))] = gen.build_persona(corpus)
        self.assertEqual(first, second)
        # 文件内容就是现在生成器会产出的东西（防止生成器改了却忘了重跑）
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(self.personas, ensure_ascii=False, sort_keys=True),
        )


class TestEvalMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.eval_mod = _load_module(ROOT / "scripts" / "eval_testdata.py", "eval_testdata_m29")

    def test_load_negatives(self) -> None:
        negatives = self.eval_mod.load_negatives()
        self.assertIn("p01", negatives)
        self.assertTrue(all(p.get("origin") == "synthetic" for p in negatives["p01"]))

    def test_load_negatives_missing_file_is_safe(self) -> None:
        self.assertEqual(self.eval_mod.load_negatives(ROOT / "testdata" / "nope.json"), {})

    def test_summarize_negatives_splits_coexist_and_distract(self) -> None:
        rows = [
            {"category": "precision", "noise_bad": False, "rank": 1, "query": "a", "hardness": 1},
            {"category": "precision", "noise_bad": True, "rank": 1, "query": "b", "hardness": 1},
            {"category": "precision", "noise_bad": True, "rank": None, "query": "c", "hardness": 2},
            {"category": "precision", "noise_bad": True, "rank": 2, "query": "c", "hardness": 3},
        ]
        summary = self.eval_mod.summarize_negatives(rows)
        self.assertEqual(summary["probes"], 4)
        self.assertEqual(summary["distinct_queries"], 3)
        self.assertEqual(summary["coexist"], 2)
        self.assertEqual(summary["distract"], 1)
        self.assertEqual(summary["purity"], 25.0)
        self.assertEqual(summary["by_tier"]["1"]["probes"], 2)


if __name__ == "__main__":
    unittest.main()
