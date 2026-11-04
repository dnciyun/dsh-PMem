"""长期记忆测试台：剧本、回放、时钟、记账。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pmem.base import clock
from pmem.bench import load_corpus, replay
from pmem.bench.model import StubModel
from pmem.bench.score import StubJudge, summarize
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

CORPUS = {
    "version": 1,
    "persona": {"id": "t", "name": "测试人物", "profile": "一个用来跑管道的人"},
    "days": [
        {
            "date": "2026-03-01",
            "turns": [
                {
                    "time": "09:00",
                    "user": "我叫阿测，叫我阿测就行。",
                    "write": [
                        {"text": "用户叫阿测。", "groups": ["我/姓名"], "grade": "S"},
                    ],
                },
                {"time": "21:00", "user": "今天有点累。", "write": []},
            ],
        },
        {
            "date": "2026-03-02",
            "turns": [
                {"time": "08:30", "user": "昨天睡得很晚。", "write": []},
            ],
        },
    ],
    "exams": [
        {"after": "2026-03-02", "delay_days": 0, "questions": [{"id": "q1", "q": "我叫什么？", "expect": "阿测"}]}
    ],
}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("bench")
        self.corpus_path = Path(self._dir) / "corpus.json"
        self.corpus_path.write_text(json.dumps(CORPUS, ensure_ascii=False), encoding="utf-8")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)


class TestCorpus(Case):
    def test_loads_persona_days_and_exams(self) -> None:
        corpus = load_corpus(self.corpus_path)
        self.assertEqual(corpus.persona.name, "测试人物")
        self.assertEqual(corpus.turn_count(), 3)
        self.assertEqual(corpus.dates(), ["2026-03-01", "2026-03-02"])
        self.assertEqual(corpus.exams[0].questions[0]["id"], "q1")

    def test_turn_can_be_plain_string(self) -> None:
        data = dict(CORPUS)
        data["days"] = [{"date": "2026-03-01", "turns": ["随便说一句"]}]
        path = Path(self._dir) / "plain.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        corpus = load_corpus(path)
        self.assertEqual(corpus.days[0].turns[0].user, "随便说一句")
        self.assertEqual(corpus.days[0].turns[0].time, "09:00")


class TestReplay(Case):
    def test_replays_turns_on_simulated_dates(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(self.app, corpus, StubModel())
        self.assertEqual(len(result.turns), 3)
        rows = self.app.db.conn.execute(
            "SELECT created_at, text_raw FROM memory ORDER BY seq"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["created_at"].startswith("2026-03-01T09:00"))
        self.assertIn("阿测", rows[0]["text_raw"])

    def test_limit_days_stops_early(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(self.app, corpus, StubModel(), limit_days=1)
        self.assertEqual(len(result.turns), 2)

    def test_clock_is_restored_after_replay(self) -> None:
        corpus = load_corpus(self.corpus_path)
        replay(self.app, corpus, StubModel())
        # 回放结束后不能把整个进程钉在 2026-03-02
        self.assertNotEqual(clock.day_key(), "2026-03-02")

    def test_usage_is_accounted_and_flagged(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(self.app, corpus, StubModel())
        total = result.usage_total
        self.assertGreater(total.total, 0)
        self.assertTrue(total.estimated, "桩模型的 token 必须标成估算")
        self.assertEqual(result.tokens_per_turn()["samples"], 3)

    def test_report_round_trips(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(self.app, corpus, StubModel())
        path = result.write_report(Path(self._dir) / "out" / "report.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["turns"], 3)
        self.assertEqual(len(payload["turns"]), 3)
        self.assertIn("usage", payload["turns"][0])

    def test_tool_calls_land_in_memory(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(self.app, corpus, StubModel())
        self.assertEqual(result.turns[0].tools, ["memory_add"])
        self.assertEqual(result.turns[1].tools, [])


class TestExam(Case):
    def test_exam_does_not_write_into_main_db(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(
            self.app,
            corpus,
            StubModel(),
            exams=True,
            judge=StubJudge(),
            exam_dir=Path(self._dir) / "exams",
        )
        self.assertEqual(len(result.exams), 1)
        self.assertEqual(len(result.exams[0].answers), 1)
        rows = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM surface_log WHERE session_id LIKE 'exam:%'"
        ).fetchone()["c"]
        self.assertEqual(rows, 0, "考试写进了主库，后面的节点会被污染")

    def test_exam_snapshot_exists_and_carries_memory(self) -> None:
        corpus = load_corpus(self.corpus_path)
        out = Path(self._dir) / "exams"
        result = replay(self.app, corpus, StubModel(), exams=True, judge=StubJudge(), exam_dir=out)
        self.assertTrue(out.exists())
        snap = list(out.glob("exam-*.db"))
        self.assertTrue(snap, "没有生成快照")

    def test_scores_are_attached(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(self.app, corpus, StubModel(), exams=True, judge=StubJudge(), exam_dir=Path(self._dir) / "e")
        answer = result.exams[0].answers[0]
        self.assertIn(answer.score.get("class"), ("correct", "partial", "wrong", "fabricated", "uncertain", "unanswered", "unevaluable"))

    def test_exam_tokens_are_separate(self) -> None:
        corpus = load_corpus(self.corpus_path)
        result = replay(self.app, corpus, StubModel(), exams=True, judge=StubJudge(), exam_dir=Path(self._dir) / "e")
        self.assertGreater(result.exam_usage_total().total, 0)
        self.assertGreater(result.summary()["exam_tokens"]["total"], 0)


class TestScore(unittest.TestCase):
    def _answer(self, kind: str, cls: str):
        from types import SimpleNamespace

        return SimpleNamespace(kind=kind, score={"class": cls})

    def test_summary_counts_and_rates(self) -> None:
        answers = [
            self._answer("basic", "correct"),
            self._answer("basic", "partial"),
            self._answer("change", "fabricated"),
            self._answer("unknown", "uncertain"),
        ]
        data = summarize(answers)
        self.assertEqual(data["questions"], 4)
        self.assertEqual(data["classes"]["correct"], 1)
        self.assertEqual(data["classes"]["fabricated"], 1)
        self.assertEqual(data["accuracy"], round((1 + 0.5) / 4, 4))
        self.assertEqual(data["fabrication_rate"], 0.25)
        self.assertEqual(data["by_kind"]["basic"]["correct"], 1)

    def test_stub_judge_treats_uncertain_as_pass(self) -> None:
        judge = StubJudge()
        self.assertEqual(
            judge.judge(question="q", expect="阿测", answer="我不知道，没听你说过。")["class"], "uncertain"
        )
        self.assertEqual(judge.judge(question="q", expect="阿测", answer="阿测")["class"], "correct")


if __name__ == "__main__":
    unittest.main()
