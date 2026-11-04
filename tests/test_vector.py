"""1.9.0 / 0.13.0：向量通道（M13，本地 bge-small-zh-v1.5 ONNX 第二手段）。

要守住的东西：

1. **缺件即降级**：没有 onnxruntime / 没有模型文件时，所有函数是空操作，
   词法链路完全不受影响（宁可没有这条通道，也不能把检索带崩）；
2. **只补不重排**：向量条目只能进尾巴，前 ``head`` 名不许动；
3. **派生可重放**：向量只由 ``text_raw`` 决定，``model_id`` 变了整表重算；
4. 分词器和 HF ``BertTokenizer`` 对齐（normalize / pre_tokenize / wordpiece 三段）。

测试默认不需要模型：分词器用合成词表，编码器用假的 ``embed``。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

try:
    import numpy as np
except ImportError:  # 向量通道是可选件：没 numpy 就不跑这组测试，但别让整个测试集收不起来
    np = None

from pmem.base.config import DEFAULTS, Config
from pmem.gate.sdk import PMem
from pmem.memory import vector
from pmem.ops import invariants

if np is None:
    raise unittest.SkipTest("向量通道需要 numpy（可选依赖：pip install numpy onnxruntime）")

VOCAB = {
    "[PAD]": 0,
    "[UNK]": 1,
    "[CLS]": 2,
    "[SEP]": 3,
    "你": 4,
    "好": 5,
    "世": 6,
    "界": 7,
    "，": 8,
    "hello": 9,
    "##world": 10,
    "吗": 11,
    "é": 12,
}


def make_tokenizer(
    root: pathlib.Path, *, lowercase: bool = False, strip_accents: bool = False
) -> pathlib.Path:
    data = {
        "version": "1.0",
        "normalizer": {
            "type": "BertNormalizer",
            "clean_text": True,
            "handle_chinese_chars": True,
            "strip_accents": strip_accents,
            "lowercase": lowercase,
        },
        "pre_tokenizer": {"type": "BertPreTokenizer"},
        "post_processor": {"type": "TemplateProcessing"},
        "model": {
            "type": "WordPiece",
            "unk_token": "[UNK]",
            "continuing_subword_prefix": "##",
            "max_input_chars_per_word": 100,
            "vocab": VOCAB,
        },
    }
    path = root / "tokenizer.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def fake_banker(cfg: Config):
    """假编码器：按关键词给正交方向，同一概念余弦 1.0，不同概念 0.0。

    这样不用 24 MB 的模型也能验证「确保写入 / 余弦排序 / 准入线 / 补候选」四件事。
    """
    dim = vector.dim_of(cfg)
    # 「石头门」和「时间循环」在同一个方向上：词法一个字都不重合，语义是同一件事。
    concepts = {"猫": 0, "狗": 1, "石头门": 2, "时间循环": 2}

    def embed(texts, _cfg, *, prefix: bool = False):
        out = np.zeros((len(texts), dim), dtype=np.float32)
        for row, text in enumerate(texts):
            axis = 7
            for key, slot in concepts.items():
                if key in text:
                    axis = slot
                    break
            out[row, axis] = 1.0
        return out

    return embed


class TestTokenizer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.tok = vector.Tokenizer(make_tokenizer(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normalize_splits_cjk_chars(self) -> None:
        self.assertEqual(self.tok.normalize("你好世界"), "你 好 世 界")
        self.assertEqual(self.tok.normalize("你\n好"), "你 好")

    def test_normalize_drops_control_chars(self) -> None:
        self.assertEqual(self.tok.normalize("你好\x00世界"), "你 好 世 界")

    def test_pre_tokenize_splits_punctuation(self) -> None:
        self.assertEqual(self.tok.pre_tokenize(self.tok.normalize("你好，世界")), ["你", "好", "，", "世", "界"])

    def test_wordpiece_continuation(self) -> None:
        self.assertEqual(self.tok.wordpiece("helloworld"), ["hello", "##world"])

    def test_wordpiece_unknown_char(self) -> None:
        self.assertEqual(self.tok.wordpiece("zzz"), ["[UNK]"])

    def test_encode_wraps_special_tokens(self) -> None:
        self.assertEqual(self.tok.encode("你好世界"), [2, 4, 5, 6, 7, 3])

    def test_encode_truncates_and_keeps_sep(self) -> None:
        ids = self.tok.encode("你好世界", max_tokens=4)
        self.assertEqual(ids[0], 2)
        self.assertEqual(ids[-1], 3)
        self.assertEqual(len(ids), 4)

    def test_lowercase_option(self) -> None:
        tok = vector.Tokenizer(make_tokenizer(self.root, lowercase=True))
        self.assertEqual(tok.normalize("HELLO"), "hello")

    def test_strip_accents_option(self) -> None:
        tok = vector.Tokenizer(make_tokenizer(self.root, strip_accents=True))
        self.assertEqual(tok.normalize("é"), "e")
        self.assertNotEqual(self.tok.normalize("é"), "e")


class TestAvailability(unittest.TestCase):
    def test_ready_false_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config({"retrieval": {"vector": {"model_dir": tmp}}})
            ok, reason = vector.ready(cfg)
            self.assertFalse(ok)
            self.assertIn("tokenizer.json", reason)

    def test_embed_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config({"retrieval": {"vector": {"model_dir": tmp}}})
            self.assertIsNone(vector.embed(["你好"], cfg))
            self.assertEqual(vector.search(None, cfg, "你好"), {})

    def test_embed_once_returns_none_on_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "x.db"
            cfg = Config({"retrieval": {"vector": {"enabled": True, "model_dir": tmp}}})
            app = PMem.open(db_path, config=cfg)
            app.db.init()
            try:
                app.store.add("用户养了一只橘猫", kind="episodic", provenance="user_explicit")
                self.assertEqual(
                    app.db.conn.execute("SELECT COUNT(*) c FROM memory_vec_plain").fetchone()["c"], 0
                )
                data = app.search("橘猫", k=4)["data"]
                self.assertNotIn("added", data.get("vector") or {})
            finally:
                app.close()


class TestVectors(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.cfg = Config(
            {
                "retrieval": {
                    "vector": {
                        "enabled": True,
                        "model_dir": str(root / "model"),
                        "min_score": 0.55,
                        "trigger_coverage": 0.55,
                    }
                }
            }
        )
        self.db_path = root / "pmem.db"
        self.app = PMem.open(self.db_path, config=self.cfg)
        self.app.db.init()
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def _add(self, text: str) -> dict:
        return self.store.add(text, kind="episodic", provenance="user_explicit")

    def test_add_writes_vector_when_enabled(self) -> None:
        with mock.patch.object(vector, "embed", fake_banker(self.cfg)):
            self._add("用户养了一只橘猫叫年糕")
        row = self.app.db.conn.execute("SELECT * FROM memory_vec_plain").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["model_id"], vector.model_id(self.cfg))
        self.assertEqual(row["dim"], vector.dim_of(self.cfg))
        # fp16：512 维 → 1024 字节
        self.assertEqual(len(row["vec"]), vector.dim_of(self.cfg) * 2)

    def test_ensure_fills_missing_and_reports(self) -> None:
        with mock.patch.object(vector, "embed", fake_banker(self.cfg)), mock.patch.object(
            vector, "ready", return_value=(True, "ok")
        ):
            self._add("用户养了一只橘猫叫年糕")
            self._add("用户楼下有只流浪狗")
            self.app.db.conn.execute("DELETE FROM memory_vec_plain")
            result = vector.ensure(self.app.db, self.cfg)
            self.assertTrue(result["ok"])
            self.assertEqual(result["built"], 2)
            data = vector.stats(self.app.db, self.cfg)
            self.assertEqual(data["vectors"], data["memories"])
        # 这台机器上没有真模型：ready 必须如实报缺件
        self.assertFalse(vector.stats(self.app.db, self.cfg)["ready"])

    def test_ensure_drops_other_model_rows(self) -> None:
        with mock.patch.object(vector, "embed", fake_banker(self.cfg)):
            self._add("用户养了一只橘猫叫年糕")
            self.app.db.conn.execute("UPDATE memory_vec_plain SET model_id='old-model'")
            with mock.patch.object(vector, "ready", return_value=(True, "ok")):
                result = vector.ensure(self.app.db, self.cfg)
            self.assertEqual(result.get("dropped_other_model"), 1)
            models = {
                r["model_id"]
                for r in self.app.db.conn.execute("SELECT model_id FROM memory_vec_plain").fetchall()
            }
            self.assertEqual(models, {vector.model_id(self.cfg)})

    def test_search_ranks_by_cosine_and_applies_floor(self) -> None:
        with mock.patch.object(vector, "embed", fake_banker(self.cfg)):
            cat = self._add("用户养了一只橘猫叫年糕")
            self._add("用户楼下有只流浪狗")
            hits = vector.search(self.app.db, self.cfg, "猫", limit=4, min_score=0.0)
            self.assertEqual(len(hits), 2)
            top_seq = next(
                int(r["seq"])
                for r in self.app.db.conn.execute(
                    "SELECT seq FROM memory WHERE id=?", (cat["id"],)
                ).fetchall()
            )
            self.assertAlmostEqual(float(hits[top_seq]), 1.0, places=2)
            # 准入线以上只剩猫
            self.assertEqual(len(vector.search(self.app.db, self.cfg, "猫", min_score=0.55)), 1)

    def test_matrix_cache_refreshes_after_new_vector(self) -> None:
        with mock.patch.object(vector, "embed", fake_banker(self.cfg)):
            self._add("用户养了一只橘猫叫年糕")
            first = vector.search(self.app.db, self.cfg, "猫", min_score=0.0)
            self._add("用户又养了一只猫叫豆豆")
            second = vector.search(self.app.db, self.cfg, "猫", min_score=0.0)
            self.assertEqual(len(second), len(first) + 1)

    def test_embed_new_noop_when_disabled(self) -> None:
        cfg = Config({"retrieval": {"vector": {"enabled": False}}})
        app = PMem.open(pathlib.Path(self.tmp.name) / "off.db", config=cfg)
        app.db.init()
        try:
            app.store.add("用户养了一只橘猫", kind="episodic", provenance="user_explicit")
            self.assertEqual(
                app.db.conn.execute("SELECT COUNT(*) c FROM memory_vec_plain").fetchone()["c"], 0
            )
        finally:
            app.close()


class TestAppendPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PMem.open(pathlib.Path(self.tmp.name) / "p.db", config=Config())
        self.app.db.init()
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    @staticmethod
    def _item(score: float, name: str) -> dict:
        return {"id": name, "text": name, "score": score}

    def test_score_policy_keeps_head_and_sorts_tail(self) -> None:
        payload = [
            self._item(0.90, "a"),
            self._item(0.80, "b"),
            self._item(0.70, "c"),
            self._item(0.60, "d"),
            self._item(0.55, "e"),
            self._item(0.50, "f"),
            self._item(0.45, "g"),
            self._item(0.40, "h"),
        ]
        payload_seq = list(range(8))
        payload_cov = [1.0, 1.0, 1.0, 1.0, 0.2, 0.2, 0.2, 0.0]
        new_item = {**self._item(0.52, "v"), "reach": 0.52}
        out, seqs, covs, spent, added, evicted = self.store._append_items(
            [new_item],
            payload=payload,
            payload_seq=payload_seq,
            payload_cov=payload_cov,
            k=8,
            budget=600,
            spent=0,
            policy="score",
            head=4,
        )
        self.assertEqual(added, 1)
        self.assertEqual(evicted, 1)
        self.assertEqual([it["id"] for it in out[:4]], ["a", "b", "c", "d"])
        self.assertEqual([it["id"] for it in out[4:]], ["e", "v", "f", "g"])
        self.assertEqual(len(covs), len(out))
        self.assertAlmostEqual(covs[5], 0.52)

    def test_zero_cover_policy_only_replaces_blank_tail(self) -> None:
        payload = [self._item(0.9, "a"), self._item(0.5, "b")]
        out, _seqs, _covs, _spent, added, evicted = self.store._append_items(
            [self._item(0.52, "v")],
            payload=payload,
            payload_seq=[1, 2],
            payload_cov=[1.0, 0.0],
            k=2,
            budget=600,
            spent=0,
            policy="zero_cover",
        )
        self.assertEqual(added, 1)
        self.assertEqual(evicted, 1)
        self.assertEqual([it["id"] for it in out], ["a", "v"])

    def test_budget_blocks_append(self) -> None:
        payload = [self._item(0.9, "a")]
        out, _seqs, _covs, _spent, added, evicted = self.store._append_items(
            [self._item(0.52, "v")],
            payload=payload,
            payload_seq=[1],
            payload_cov=[1.0],
            k=4,
            budget=10,
            spent=0,
            policy="score",
        )
        self.assertEqual((added, evicted), (0, 0))
        self.assertEqual([it["id"] for it in out], ["a"])


class TestSearchIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.cfg = Config(
            {
                "retrieval": {
                    "vector": {
                        "enabled": True,
                        "model_dir": str(root / "model"),
                        "min_score": 0.55,
                        "trigger_coverage": 0.55,
                    }
                }
            }
        )
        self.app = PMem.open(root / "pmem.db", config=self.cfg)
        self.app.db.init()
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def test_vector_rescues_memory_lexical_cannot_see(self) -> None:
        with mock.patch.object(vector, "embed", fake_banker(self.cfg)):
            target = self.store.add(
                "用户昨晚看了石头门，第三遍", kind="episodic", provenance="user_explicit"
            )
            self.store.add("用户楼下有只流浪狗叫阿黄", kind="episodic", provenance="user_explicit")
            result = self.app.search("那个时间循环的动画", k=4, record=False)["data"]
        items = result["items"]
        # M30 起向量默认当一路候选取参与 RRF（channels 里是 "vec"），不再只在末尾补两条；
        # 老的补候选路径仍可能同时生效（channels 里是 "vector"），两种都算救回。
        rescued = [it for it in items if it["id"] == target["id"]]
        self.assertEqual(len(rescued), 1)
        channels = rescued[0]["channels"]
        self.assertTrue("vec" in channels or "vector" in channels, channels)

    def test_vector_skipped_when_lexical_confident(self) -> None:
        with mock.patch.object(vector, "embed", fake_banker(self.cfg)):
            self.store.add("用户养了一只橘猫叫年糕", kind="episodic", provenance="user_explicit")
            result = self.app.search("橘猫年糕", k=4, record=False)["data"]
        self.assertEqual((result.get("vector") or {}).get("skipped"), "coverage")

    def test_disabled_channel_leaves_no_trace(self) -> None:
        cfg = Config({"retrieval": {"vector": {"enabled": False}}})
        app = PMem.open(pathlib.Path(self.tmp.name) / "off.db", config=cfg)
        app.db.init()
        try:
            app.store.add("用户养了一只橘猫叫年糕", kind="episodic", provenance="user_explicit")
            data = app.search("猫", k=4, record=False)["data"]
            self.assertNotIn("vector", data)
            self.assertFalse(any(it.get("source") == "vector" for it in data["items"]))
        finally:
            app.close()


class TestInvariantI19(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PMem.open(pathlib.Path(self.tmp.name) / "inv.db", config=Config())
        self.app.db.init()

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def test_bad_vector_length_fails(self) -> None:
        err = self.app.store.add("用户养了一只橘猫", kind="episodic", provenance="user_explicit")
        seq = self.app.db.conn.execute("SELECT seq FROM memory WHERE id=?", (err["id"],)).fetchone()["seq"]
        with self.app.db.tx() as conn:
            conn.execute(
                "INSERT INTO memory_vec_plain(memory_seq, model_id, dim, vec, updated_at) "
                "VALUES(?,?,?,?,?)",
                (int(seq), "test", 512, b"\x00" * 5, "2026-01-01T00:00:00"),
            )
        results = {r["id"]: r for r in invariants.check_all(self.app.db)}
        self.assertFalse(results["I19"]["ok"])

    def test_clean_vector_passes(self) -> None:
        err = self.app.store.add("用户养了一只橘猫", kind="episodic", provenance="user_explicit")
        seq = self.app.db.conn.execute("SELECT seq FROM memory WHERE id=?", (err["id"],)).fetchone()["seq"]
        with self.app.db.tx() as conn:
            conn.execute(
                "INSERT INTO memory_vec_plain(memory_seq, model_id, dim, vec, updated_at) "
                "VALUES(?,?,?,?,?)",
                (int(seq), "test", 512, b"\x00" * 1024, "2026-01-01T00:00:00"),
            )
        results = {r["id"]: r for r in invariants.check_all(self.app.db)}
        self.assertTrue(results["I19"]["ok"])


class TestDefaults(unittest.TestCase):
    def test_defaults_disable_vector_in_tests(self) -> None:
        """测试环境默认关（tests/__init__.py 改的），但仓库默认配置是开的。"""
        self.assertIn("vector", DEFAULTS["retrieval"])
        self.assertTrue(DEFAULTS["retrieval"]["vector"]["model_dir"])
        self.assertFalse(DEFAULTS["retrieval"]["vector"]["enabled"])

    def test_trigger_defaults(self) -> None:
        vec = DEFAULTS["retrieval"]["vector"]
        self.assertEqual(vec["trigger"], "coverage")
        self.assertGreater(vec["trigger_coverage"], 0.0)
        self.assertGreater(vec["min_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
