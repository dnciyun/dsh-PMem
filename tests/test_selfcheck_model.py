"""作答自检的**可选模型复核**（0.36.0）：确定性那一遍之外，再让模型看一遍证据。

钉三件事：
1. 不传 model 时结果里 ``model.skipped=True``，确定性结论不受影响；
2. 模型给出 ``unsupported`` 时按 ``kind="claim"`` 合并，且 ``should_say_unknown`` 进 note；
3. 模型报错/返回非法 JSON 时只写 note，不把确定性结论带崩。
"""

from __future__ import annotations

import json
import unittest

from pmem.base import clock
from pmem.gate.sdk import PMem
from pmem.memory import selfcheck

from .support import drop_dir, temp_dir


class FakeModel:
    """只认 JSON 的假模型：记录收到的 messages，按剧本返回 content。"""

    name = "fake"
    paid = False

    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[dict] = []

    def complete(self, messages, tools=None, *, hint=None):
        self.messages = messages

        class Usage:
            total = 12

        class Reply:
            def __init__(self, content: str) -> None:
                self.content = content
                self.tool_calls = None
                self.usage = Usage()

        return Reply(self.content)


class ModelCheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("selfcheck-model")
        self.app = PMem.open(
            self.dir / "pmem.db",
            config={"retrieval": {"vector": {"enabled": False}}},
        )
        self.app.db.init()
        clock.set_now("2026-11-05T10:00:00+00:00")
        self.app.store.add(
            "2026-10-26 那天把青鸟七号的代码模块全部对上了",
            valid_from="2026-10-26T09:00:00+00:00",
        )

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self.dir)

    def test_without_model_marked_skipped(self) -> None:
        data = self.app.selfcheck("2026-10-26 那天把青鸟七号对上了。")["data"]
        self.assertTrue(data["clean"], data)
        self.assertTrue(data["model"]["skipped"], data["model"])

    def test_model_claims_merged_as_claim_issue(self) -> None:
        model = FakeModel(
            json.dumps(
                {
                    "unsupported": [
                        {"span": "大家都夸我们做得快", "reason": "证据里没人评价过速度"}
                    ],
                    "should_say_unknown": True,
                },
                ensure_ascii=False,
            )
        )
        data = selfcheck.check_answer(
            self.app.store,
            "2026-10-26 那天把青鸟七号对上了，大家都夸我们做得快。",
            question="青鸟七号那次怎么样？",
            model=model,
        )
        self.assertFalse(data["clean"], data)
        kinds = [issue["kind"] for issue in data["issues"]]
        self.assertIn("claim", kinds, data)
        self.assertIn("不知道", data["note"], data)
        self.assertFalse(data["model"]["skipped"])
        # 复核 prompt 里应该带上了检索到的证据与草稿原文
        joined = "\n".join(str(message.get("content") or "") for message in model.messages)
        self.assertIn("青鸟七号", joined)
        self.assertIn("证据", joined)

    def test_model_garbage_does_not_break_deterministic_result(self) -> None:
        model = FakeModel("我拒绝输出 JSON")
        data = selfcheck.check_answer(
            self.app.store,
            "2026-10-26 那天把青鸟七号对上了。",
            model=model,
        )
        self.assertTrue(data["clean"], data)
        self.assertIn("没给出合法 JSON", data["note"], data)

    def test_model_error_is_caught(self) -> None:
        class Boom:
            name = "boom"
            paid = True

            def complete(self, *args, **kwargs):
                raise RuntimeError("网络断了")

        data = selfcheck.check_answer(
            self.app.store,
            "2026-10-26 那天把青鸟七号对上了。",
            model=Boom(),
        )
        self.assertTrue(data["clean"], data)
        self.assertIn("模型复核失败", data["note"], data)
        self.assertTrue(data["model"]["skipped"], data["model"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
