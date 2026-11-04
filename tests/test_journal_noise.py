"""M30 补：流水里的「脚手架台词」识别与清理，以及 QQ/B站唤醒包装的识别。

为什么单独测：这些行**长得像正文**，一旦被当作用户原话消化，就会派生出
「用户【当前时间】…」「用户Approval policy: ask…」这种记忆——比漏记更糟。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem import PMem
from pmem.memory import journal


class TestEnvelope(unittest.TestCase):
    def test_runtime_lines_are_dropped(self) -> None:
        cases = [
            "Approval policy: ask. Operations that require approval may ask…",
            "background job bash-1234 (bash: 跑测试) finished [status: completed, exit code: 0]. Read its output with job_output.",
            "Current DeepSeek Harness implementation checkout: …",
            "Browser time zone for this request: Europe/Rome",
        ]
        for line in cases:
            self.assertEqual(journal._strip_runtime(line), "", line)

    def test_qq_envelope_is_dropped(self) -> None:
        envelope = (
            "【当前时间】2026/11/4 18:29:05（Europe/Rome）"
            "【当前角色】小米枣（完整角色卡请调用 qq_get_prompt 查看）"
            "【你的身份】你在 QQ 上的昵称是「小米枣」"
        )
        self.assertEqual(journal._strip_runtime(envelope), "")

    def test_real_message_survives(self) -> None:
        self.assertEqual(journal._strip_runtime("记住：我喜欢冰美式"), "记住：我喜欢冰美式")
        # 只有一种标记、又像人话的，不能误杀
        self.assertEqual(
            journal._strip_runtime("【当前时间】这三个字好烦"), "【当前时间】这三个字好烦"
        )


class TestPurgeNoise(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = PMem.open(Path(self.tmp.name) / "noise.db")
        self.addCleanup(self.app.close)

    def _raw_line(self, text: str, *, digested: bool = False) -> None:
        """直接塞一行历史流水：写入时的 `_strip_runtime` 已经挡住噪声了，
        这里模拟「老版本写进来的」那些行。"""
        from pmem.base.ids import ulid

        if digested:
            # 外键要求 episode 真存在：先造一篇最简日记
            self.app.db.conn.execute(
                "INSERT OR IGNORE INTO episode(id, subject_id, channel, started_at, day_key, "
                "title, summary, memory_count, derivation, extra_json) "
                "VALUES('ep-x','self','dsh','2026-11-04T23:00:00Z','2026-11-04','测试','',0,"
                "'template','{}')"
            )
        self.app.db.conn.execute(
            "INSERT INTO journal_line(id, subject_id, session_id, day_key, ts, role, text, "
            "episode_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ulid(),
                "self",
                "s1",
                "2026-11-04",
                "2026-11-04T10:00:00Z",
                "user",
                text,
                "ep-x" if digested else None,
                "2026-11-04T10:00:00Z",
            ),
        )
        self.app.db.conn.commit()

    def test_dry_run_does_not_delete(self) -> None:
        self._raw_line("Approval policy: ask. 需要审批")
        self._raw_line("记住，我不吃香菜")
        stats = journal.purge_noise(self.app.db, dry_run=True)
        self.assertEqual(stats["noise"], 1)
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(len(journal.pending(self.app.db)), 2)

    def test_real_run_deletes_only_noise(self) -> None:
        self._raw_line("Approval policy: ask. 需要审批")
        self._raw_line("记住，我不吃香菜")
        stats = journal.purge_noise(self.app.db)
        self.assertEqual(stats["deleted"], 1)
        left = [row["text"] for row in journal.pending(self.app.db)]
        self.assertEqual(left, ["记住，我不吃香菜"])

    def test_digested_lines_are_not_touched(self) -> None:
        self._raw_line("Approval policy: ask. 旧的那行", digested=True)
        self.assertEqual(journal.purge_noise(self.app.db)["deleted"], 0)

    def test_append_refuses_noise_at_write_time(self) -> None:
        """写入时就该挡住：噪声行连流水都进不去。"""
        result = journal.append(self.app.db, "Approval policy: ask. 需要审批", role="user")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "empty")


if __name__ == "__main__":
    unittest.main()
