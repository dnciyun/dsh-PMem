"""两阶段检索（task-49）：重排器缺件降级 + 成对编码 + 开关关闭等价。"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory.retrieval import rerank

from .support import drop_dir, temp_dir

OFF_A = Config({"retrieval": {"rerank": {"enabled": False}}})
OFF_B = Config({"retrieval": {"rerank": {"enabled": False}, "time_router": {"enabled": False}}})
ON = Config(
    {
        "retrieval": {
            "rerank": {"enabled": True, "pool": 40, "max_len": 320, "threads": 1, "blend": 1.0}
        }
    }
)


def _mini_tokenizer(path: Path) -> None:
    pieces = ["<unk>", "<s>", "</s>", "<pad>", "▁我", "▁住", "▁南", "▁岗", "▁在", "▁北", "▁京"]
    data = {
        "model": {
            "type": "Unigram",
            "unk_id": 0,
            "vocab": [[piece, -1.0] for piece in pieces],
        },
        "added_tokens": [
            {"content": "<unk>", "id": 0, "special": True},
            {"content": "<s>", "id": 1, "special": True},
            {"content": "</s>", "id": 2, "special": True},
            {"content": "<pad>", "id": 3, "special": True},
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class RerankUnitTest(unittest.TestCase):
    def test_disabled_ready_returns_false_without_touching_model(self) -> None:
        ok, why = rerank.ready(Config())
        self.assertFalse(ok)
        self.assertIn("enabled=false", why)

    def test_missing_model_is_silent_degrade(self) -> None:
        tmp = temp_dir("rerank-missing")
        try:
            with mock.patch.object(rerank, "model_dir", lambda: Path(tmp)):
                ok, why = rerank.ready(ON)
                self.assertFalse(ok)
                self.assertTrue(why)
                self.assertEqual(rerank.score("q", ["d"], ON), [])
        finally:
            drop_dir(tmp)

    def test_pair_encoding_has_cls_sep_and_attention(self) -> None:
        path = Path(temp_dir("rerank-tokenizer")) / "tokenizer.json"
        try:
            _mini_tokenizer(path)
            tok = rerank.UnigramTokenizer(path)
            ids, mask, types = tok.pair("我住南岗", "在北京", max_len=64)
            self.assertEqual(ids[0], tok.cls_id)
            self.assertEqual(ids[-1], tok.sep_id)
            self.assertEqual(ids.count(tok.sep_id), 3)  # XLM-R 句对：q SEP SEP d SEP
            self.assertEqual(len(ids), len(mask))
            self.assertEqual(len(ids), len(types))
            self.assertTrue(all(value == 1 for value in mask))
            self.assertTrue(all(value == 0 for value in types))
            short_ids, _mask, _types = tok.pair("我住南岗", "在北京", max_len=8)
            self.assertLessEqual(len(short_ids), 8)
            self.assertEqual(short_ids[-1], tok.sep_id)
        finally:
            drop_dir(path.parent)

    def test_rerank_is_noop_when_disabled(self) -> None:
        items = [{"id": "a", "text": "用户住在南岗"}, {"id": "b", "text": "用户喜欢咖啡"}]
        out = rerank.rerank(items, "南岗", Config())
        self.assertEqual(out, items)
        self.assertTrue(all("rerank_score" not in item for item in out))

    def test_logits_shapes(self) -> None:
        self.assertEqual(rerank._logits_to_scores(np.array([[2.5]], dtype=np.float32)), [2.5])
        self.assertEqual(
            rerank._logits_to_scores(np.array([[1.0, 3.0]], dtype=np.float32)), [2.0]
        )


class RerankSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        clock.set_now("2026-11-08T12:00:00+08:00")
        self._dirs = []
        self._apps = []
        seed_dir = temp_dir("rerank-seed")
        self._dirs.append(seed_dir)
        seed = PMem.open(seed_dir / "seed.db", config=Config())
        seed.store.add("用户住在南岗，平时坐地铁上班", session_id="s1")
        seed.store.add("用户周末喜欢去江边散步", session_id="s1")
        self._seed_path = seed_dir / "seed.db"
        seed.close()

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open_copy(self, config: Config) -> PMem:
        path = temp_dir("rerank-run")
        self._dirs.append(path)
        db = path / "pmem.db"
        shutil.copyfile(self._seed_path, db)
        app = PMem.open(db, config=config)
        self._apps.append(app)
        return app

    def test_disabled_is_byte_identical(self) -> None:
        app_a = self._open_copy(OFF_A)
        app_b = self._open_copy(OFF_B)
        app_a.store.search("南岗 地铁", k=8, record=False)
        app_b.store.search("南岗 地铁", k=8, record=False)
        conn_a, conn_b = app_a.store.db.conn, app_b.store.db.conn
        stmts_a: list[str] = []
        stmts_b: list[str] = []
        conn_a.set_trace_callback(stmts_a.append)
        conn_b.set_trace_callback(stmts_b.append)
        found_a = app_a.store.search("南岗 地铁", k=8, record=False)
        found_b = app_b.store.search("南岗 地铁", k=8, record=False)
        conn_a.set_trace_callback(None)
        conn_b.set_trace_callback(None)
        for payload in (found_a, found_b):
            payload.pop("latency_ms", None)
            if isinstance(payload.get("meta", {}).get("retrieval"), dict):
                payload["meta"]["retrieval"].pop("latency_ms", None)
        self.assertEqual(
            json.dumps(found_a, ensure_ascii=False, sort_keys=True),
            json.dumps(found_b, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(stmts_a, stmts_b)
        self.assertNotIn("rerank_score", json.dumps(found_a))

    def test_enabled_scores_are_applied_when_ready(self) -> None:
        app = self._open_copy(ON)
        with mock.patch.object(rerank, "ready", lambda cfg: (True, "ok")), mock.patch.object(
            rerank, "score", lambda query, docs, cfg: [-1.0, 5.0][: len(docs)]
        ), mock.patch.object(
            rerank, "settings", lambda cfg: {"enabled": True, "pool": 40, "max_len": 320, "blend": 1.0}
        ):
            found = app.store.search("南岗 地铁", k=8, record=False)
        self.assertTrue(
            any("rerank_score" in item for item in found["items"]),
            found["items"],
        )
        reranked = [item for item in found["items"] if "rerank_score" in item]
        self.assertTrue(all(0.0 <= float(item["rerank_score"]) <= 1.0 for item in reranked))


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
