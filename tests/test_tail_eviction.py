"""尾部保守置换（0.33.0）：槽位已满时，深捞/原话能不能换掉最弱的尾巴。

为什么单独测：0.32.0 的契约是「深捞只填空位」，B 层归因里「覆盖率低 + 槽位满」
是最常见的一类空转——问句已经明说缺什么，却因为 k 个槽位满了直接跳过。
0.33.0 允许置换，但必须守三条纪律，这个文件就是钉住它们：

1. 先占空位，空位没占满不许动既有结果；
2. 头部 `head` 条永不动；受害者只能是覆盖率低于门槛的弱尾巴；条数受 `max_evict` 卡住；
3. 条数 ≤ k、token ≤ budget 仍是硬上限（置换要先退预算再花）。

另有一条接线测试：assistant 原话通道在槽位满时也走这条通路，而不是「预算不够就放不下」。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from pmem.base import clock
from pmem.base.config import Config
from pmem.base.db import Database
from pmem.memory import journal
from pmem.memory.retrieval import channels
from pmem.memory.store import MemoryStore
from pmem.memory.text import estimate_tokens

from .support import drop_dir, temp_dir


def _item(text: str, score: float = 0.5, item_id: str = "") -> dict:
    return {"text": text, "score": score, "id": item_id or f"id-{len(text)}"}


def _cost(text: str) -> int:
    return estimate_tokens(text) + 12


class PlaceItemsCase(unittest.TestCase):
    def test_free_slots_are_filled_without_evicting(self) -> None:
        payload = [_item("一", 0.9), _item("二", 0.8)]
        seq, cov = [1, 2], [0.9, 0.8]
        payload, seq, cov, spent, added, evicted = channels.place_items(
            None,
            [_item("新条目甲"), _item("新条目乙")],
            payload=payload,
            payload_seq=seq,
            payload_cov=cov,
            k=5,
            budget=600,
            spent=10,
            head=3,
            max_evict=2,
        )
        self.assertEqual((added, evicted), (2, 0))
        self.assertEqual(len(payload), 4)
        # 既有的两条没动，新条目接在尾部。
        self.assertEqual(payload[0]["text"], "一")
        self.assertEqual(payload[-1]["text"], "新条目乙")
        self.assertEqual(spent, sum(_cost(item["text"]) for item in payload))

    def test_full_payload_evicts_weakest_tail_only(self) -> None:
        payload = [
            _item("强命中头一", 0.9),
            _item("强命中头二", 0.8),
            _item("中等三", 0.5),
            _item("弱尾巴", 0.2),
            _item("最弱尾巴", 0.1),
        ]
        seq, cov = [1, 2, 3, 4, 5], [0.9, 0.8, 0.7, 0.1, 0.0]
        payload, seq, cov, spent, added, evicted = channels.place_items(
            None,
            [_item("深捞答案", 0.95)],
            payload=payload,
            payload_seq=seq,
            payload_cov=cov,
            k=5,
            budget=600,
            spent=sum(_cost(item["text"]) for item in payload),
            head=3,
            max_evict=2,
            victim_cov_max=0.35,
            cov_value=0.9,
        )
        self.assertEqual((added, evicted), (1, 1))
        self.assertEqual(len(payload), 5, "置换之后条数必须还是 k")
        texts = [item["text"] for item in payload]
        self.assertEqual(texts[:3], ["强命中头一", "强命中头二", "中等三"], texts)
        self.assertNotIn("最弱尾巴", texts)
        self.assertEqual(payload[-1]["text"], "深捞答案")
        self.assertEqual(cov[-1], 0.9, "补进来的条目要用调用方给的覆盖率占位")
        self.assertEqual(seq[-1], 0, "seq 由调用方回填，place_items 先占位")
        self.assertLessEqual(spent, 600)

    def test_victim_threshold_blocks_harmful_swap(self) -> None:
        """所有尾巴都不比 cov_best 明显弱（覆盖率都在门槛之上）→ 宁可不加。"""
        payload = [_item("a", 0.9), _item("b", 0.8), _item("c", 0.7)]
        payload, seq, cov, spent, added, evicted = channels.place_items(
            None,
            [_item("深捞答案", 0.95)],
            payload=payload,
            payload_seq=[1, 2, 3],
            payload_cov=[0.6, 0.55, 0.5],
            k=3,
            budget=600,
            spent=100,
            head=1,
            max_evict=2,
            victim_cov_max=0.45,
        )
        self.assertEqual((added, evicted), (0, 0))
        self.assertEqual([item["text"] for item in payload], ["a", "b", "c"])

    def test_max_evict_caps_replacements(self) -> None:
        payload = [_item("头", 0.9), _item("弱一", 0.2), _item("弱二", 0.2), _item("弱三", 0.2)]
        payload, seq, cov, spent, added, evicted = channels.place_items(
            None,
            [_item("答案甲"), _item("答案乙"), _item("答案丙")],
            payload=payload,
            payload_seq=[1, 2, 3, 4],
            payload_cov=[0.9, 0.1, 0.1, 0.1],
            k=4,
            budget=600,
            spent=100,
            head=2,
            max_evict=1,
            victim_cov_max=0.35,
        )
        self.assertEqual(evicted, 1)
        self.assertEqual(added, 1, "max_evict 用完后不能再挤，剩下的条目应放弃")
        self.assertEqual(len(payload), 4)
        self.assertEqual(payload[0]["text"], "头")

    def test_budget_still_hard_cap(self) -> None:
        payload = [_item("一", 0.9), _item("弱尾巴", 0.1)]
        long_item = {"text": "很长的条目" * 30, "score": 0.9, "id": "long"}
        cost = _cost(long_item["text"])
        release = _cost("弱尾巴")
        spent = sum(_cost(item["text"]) for item in payload)
        budget = spent + cost - release - 1  # 挤掉弱尾巴也还差 1 个 token 才放得下
        payload, seq, cov, spent2, added, evicted = channels.place_items(
            None,
            [long_item],
            payload=payload,
            payload_seq=[1, 2],
            payload_cov=[0.9, 0.1],
            k=2,
            budget=budget,
            spent=spent,
            head=1,
            max_evict=2,
            victim_cov_max=0.35,
        )
        self.assertEqual((added, evicted), (0, 0), f"cost={cost} release={release} budget={budget}")
        self.assertLessEqual(spent2, budget)

    def test_head_is_protected_even_when_it_is_the_weakest(self) -> None:
        """头部覆盖率低也不许换：深捞是补充，不能重排头部（那是另一条产品线）。"""
        payload = [_item("头但弱", 0.1), _item("尾但强", 0.9)]
        payload, seq, cov, spent, added, evicted = channels.place_items(
            None,
            [_item("答案")],
            payload=payload,
            payload_seq=[1, 2],
            payload_cov=[0.0, 0.9],
            k=2,
            budget=600,
            spent=60,
            head=1,
            max_evict=2,
            victim_cov_max=0.35,
        )
        self.assertEqual(payload[0]["text"], "头但弱")
        self.assertEqual((added, evicted), (0, 0))

    def test_tolerates_stale_seq_and_cov_arrays(self) -> None:
        """防御：编排里改过 payload 但没同步数组时（原话兜底/日记头插），不能索引越界。"""
        payload = [_item("一", 0.9), _item("二", 0.8), _item("三", 0.2)]
        payload, seq, cov, spent, added, evicted = channels.place_items(
            None,
            [_item("答案")],
            payload=payload,
            payload_seq=[1],  # 故意短
            payload_cov=[0.9],  # 故意短
            k=3,
            budget=600,
            spent=60,
            head=1,
            max_evict=1,
            victim_cov_max=0.0,
        )
        self.assertEqual(len(payload), 3)
        self.assertEqual(len(seq), 3, "seq 必须补到和 payload 一样长")
        self.assertEqual(len(cov), 3, "覆盖率必须补到和 payload 一样长")
        self.assertEqual((added, evicted), (1, 1))

    def test_defaults_exist(self) -> None:
        cfg = Config()
        # 2 的置换默认关（B 层归因净 −1 题，见 config 注释）；③ 的置换默认开。
        self.assertFalse(cfg.get("retrieval.adaptive.evict"))
        self.assertEqual(cfg.get("retrieval.adaptive.evict_head"), 3)
        self.assertEqual(cfg.get("retrieval.adaptive.max_evict"), 2)
        self.assertEqual(cfg.get("retrieval.adaptive.evict_cover"), 0.0, "只换覆盖率 0 的尾巴")
        self.assertEqual(cfg.get("retrieval.assistant_voice.evict_head"), 3)
        self.assertEqual(cfg.get("retrieval.assistant_voice.max_evict"), 2)


CFG = Config(
    {
        "retrieval": {
            "vector": {"enabled": False},
            "raw_fallback": False,
        },
        "net": {"auto_extract": False},
    }
)
TS = "2026-11-03T10:00:00Z"
ASSISTANT_LINE = "我给自己安了个夜班台账员的设定：只记你说出口的，不猜你心里。"


class AssistantEvictsWhenFullCase(unittest.TestCase):
    """接线：槽位满时 assistant 原话也能挤进来（0.33.0 以前这条会静默放弃）。"""

    def setUp(self) -> None:
        self.dir = temp_dir("assistant-evict")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        clock.set_now(datetime.fromisoformat(TS))
        journal.append(self.db, "今晚杯堆清完了，手都泡皱了", role="user", ts=TS, session_id="s1")
        journal.append(self.db, ASSISTANT_LINE, role="assistant", ts=TS, session_id="s1")
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")
        # 五条都和问句共享「晚上/记东西」这类双字，占满 k=5 个槽位且覆盖率不低
        # （覆盖率够高才不会触发低覆盖加深，测的才是原话通道自己的置换）。
        for index in range(6):
            self.store.add(f"晚上记东西这件事我第{index}次提过了，别又忘", session_id="s2")

    def tearDown(self) -> None:
        self.db.close()
        clock.reset_now()
        drop_dir(self.dir)

    def test_assistant_line_replaces_weak_tail(self) -> None:
        result = self.store.search("你上次说过晚上记东西那个设定叫什么名字", k=5, record=False)
        texts = [str(item.get("text") or "") for item in result["items"]]
        self.assertLessEqual(len(texts), 5, texts)
        self.assertTrue(
            any("夜班台账员" in text for text in texts),
            f"槽位满时 assistant 原话没挤进来：{texts}",
        )
        info = result.get("assistant_voice") or {}
        self.assertGreaterEqual(int(info.get("added") or 0), 1, info)
        self.assertGreaterEqual(int(info.get("evicted") or 0), 1, info)
        # 前面几条词法结果不能被换掉（头部保护）。
        self.assertTrue(any("第0次" in text for text in texts), texts)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
