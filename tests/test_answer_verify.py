"""答案保真减法（P0-A / 0.40.0，默认关）：claim 级 unsupported + risk + 最小修法。

守六件事：

1. 日期：有支撑（含不同写法、按解析后的日期比）不报；无支撑报 ``date_*``；
2. 专名：上下文里有的不报；没有的报 ``name_*``；
3. 纯评价句（没有有型槽）不进分母、不罚；
4. 短答案分母保护：``risk = unsupported / max(min_claims, typed_claims)``；
5. 关时（默认）短路：模块只返回 ``{"enabled": false}``，0 SQL；``PMem.render/search``
   与「没有 answer.verify 键」的旧视图逐字节一致（SQL 序列 + 返回体）；
6. ``mode=strip`` 保守：只删「整句有型断言全无支撑」的句子，带引用编号的句子不动。
"""

from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import answer_verify, journal

from .support import InProcessMcp, drop_dir, temp_dir

BASE_OVERRIDES: dict = {
    "retrieval": {
        "vector": {"enabled": False, "channel": False},
        "graph": {"append": False},
    }
}
LEGACY_KEYS = frozenset({"answer.verify"})



# grader-dev 的 H1 离线预演 20 条 strip 回归样例（外部证据，只做回归，不进产品语料）。
# 字段：id / claim_type / strip_span（判卷点名的无依据说法）/ sentence（应被删的整句）/ answer（真实答案原文）。
H1_STRIP_SAMPLES = [
    {
        "id": "P14-CASE-added-gift/d45",
        "claim_type": "date",
        "strip_span": "4月27日",
        "sentence": "4月27日卡片夹进了玄关抽屉那本画册里，说周末再拿出来",
        "answer": "按记录：那张手写小卡是打算等周宁**周末回家再给**。限制是**先别在她面前提**，用户怕惊喜提前漏了。\n\n4月27日卡片夹进了玄关抽屉那本画册里，说周末再拿出来。但**后来到底送没送出去，记录里没有**，我不敢替你说送了。"
    },
    {
        "id": "P14-CASE-batch-suv/d45",
        "claim_type": "date",
        "strip_span": "4月12日",
        "sentence": "4月12日你也说过那车不是你的、你哪有车，这跟“借李楠的车开”对得上",
        "answer": "你没车。那辆白色SUV是李楠的，他有时候借你开两天。平时你主要是坐地铁，偶尔打车；早上地铁挤，到站还得再走一段。4月12日你也说过那车不是你的、你哪有车，这跟“借李楠的车开”对得上。"
    },
    {
        "id": "P16-CASE-batch-birthday/d15",
        "claim_type": "date",
        "strip_span": "6月8号",
        "sentence": "6月8号是你当时张口记岔的，被姐姐当场纠正过",
        "answer": "6月18号。6月8号是你当时张口记岔的，被姐姐当场纠正过。"
    },
    {
        "id": "P16-CASE-batch-sister_boundary/d15",
        "claim_type": "relative_date",
        "strip_span": "这周日",
        "sentence": "具体哪天我记不准了——当时说的是“这周日”，现在过了几天，我不确定是不是还没到",
        "answer": "你姐那个体育老师面试的事吧？上回你说要去给她掐25分钟，坐对面当模拟考官，她讲快了你按铃。具体哪天我记不准了——当时说的是“这周日”，现在过了几天，我不确定是不是还没到。\n\n替她累归累，上回你还提过房间乱、要洗一桶衣服，不晓得后来洗了没。"
    },
    {
        "id": "P17-CASE-005/d03",
        "claim_type": "relative_date",
        "strip_span": "明天",
        "sentence": "明天还得早起陪你爸去社区医院，今晚能睡就早点睡，剩下的搁明天",
        "answer": "嗯，不说就不说，堵着就堵着吧，不用非得今天捋顺。明天还得早起陪你爸去社区医院，今晚能睡就早点睡，剩下的搁明天。"
    },
    {
        "id": "P17-CASE-008/d03",
        "claim_type": "date",
        "strip_span": "4月8号",
        "sentence": "不过这个新版是4月8号就定下来的，按记录看是在写出来之前就改好的，不是交完之后才返工",
        "answer": "具体哪天交的，我这边记录里对不上——我只记得4月12号我还在认逾期，13号问你那段写好了没，14号还停在扣子发烫那儿、说要接着往下编。所以“交完”是哪天，我不敢给准数。\n\n你说的改动，我倒是记得：原先写的是阿桔把纽扣交给鲁三，后来改成她没给，把黄铜纽扣藏进风衣内袋，扣子突然发烫，指向渡口西码头。不过这个新版是4月8号就定下来的，按记录看是在写出来之前就改好的，不是交完之后才返工。如果你说“交完后改”另有一次，那我就不记得了。"
    },
    {
        "id": "P17-CASE-008/d15",
        "claim_type": "date",
        "strip_span": "4月14号",
        "sentence": "到4月14号，已经在说从发烫那儿接着往下编了",
        "answer": "具体哪天交的，我记忆里没有确凿的日子——只记得4月12号晚上还欠着，说“今晚补上或明天写好念给你听”；到4月14号，已经在说从发烫那儿接着往下编了。\n\n改动是这条：阿桔没把纽扣给鲁三，而是把黄铜纽扣藏进风衣内袋，扣子突然发烫，指向渡口西码头。"
    },
    {
        "id": "P17-CASE-008/d45",
        "claim_type": "date",
        "strip_span": "4月8号",
        "sentence": "改动记得：4月8号那版是阿桔没把纽扣给鲁三，她把黄铜纽扣藏进风衣内袋，扣子突然发烫，指向渡口西码头",
        "answer": "哪天交的我说不准。记录只到4月12号我认了逾期、说今晚补或第二天写好念给你听；13号的记录里还有“夜雾那段写好了没”，14号说停在发烫那儿、今天接着往下编。具体哪天交的没有记录。\n\n改动记得：4月8号那版是阿桔没把纽扣给鲁三，她把黄铜纽扣藏进风衣内袋，扣子突然发烫，指向渡口西码头。"
    },
    {
        "id": "P17-CASE-012/d15",
        "claim_type": "date",
        "strip_span": "5 月 15 号",
        "sentence": "我记得的是 5 月 15 号那次：当时说定收工提醒改成提前半小时说一声，并且只说一次，剩下的你自己安排",
        "answer": "我记得的是 5 月 15 号那次：当时说定收工提醒改成提前半小时说一声，并且只说一次，剩下的你自己安排。如果你说的是另一个“改口”，比如某个称呼或说法，那我这儿没查到具体是哪天，不敢随便接。"
    },
    {
        "id": "P17-CASE-012/d45",
        "claim_type": "date",
        "strip_span": "8月10号",
        "sentence": "8月10号晚上说的——写便笺的时候叫你誊写员，闲聊还是小满",
        "answer": "8月10号晚上说的——写便笺的时候叫你誊写员，闲聊还是小满。你说“笔友”听着太正经了。"
    },
    {
        "id": "P17-CASE-016/d45",
        "claim_type": "date",
        "strip_span": "7 月 14 号",
        "sentence": "另外我还记着一条 7 月 14 号的记录：阿桔那条线，她把纽扣掏出来，回去找老褚，往桌上一放",
        "answer": "梦里那颗纽扣一直没拿出来，你醒了还惦记着，说大概是白天卷宗看多了，什么都往案子上靠。\n\n另外我还记着一条 7 月 14 号的记录：阿桔那条线，她把纽扣掏出来，回去找老褚，往桌上一放。这条跟那个梦是不是一回事，我不确定。"
    },
    {
        "id": "P17-CASE-017/d15",
        "claim_type": "date",
        "strip_span": "6月20日",
        "sentence": "- **6月20日**：你提到在新地方还没听惯楼下的说话声",
        "answer": "办成了。捋一下：\n\n- **5月24日**：你计划七月四号搬，去红谷滩翠苑路看一室一厅，离法院步行十五分钟。\n- **6月8日**：已经搬好了，就是翠苑路那套一室一厅。通勤短了，房租贵些。\n- **6月20日**：你提到在新地方还没听惯楼下的说话声。当时我估摸“搬过来快两周了”，你没有明确确认，但跟6月8日搬完是对得上的。\n\n所以实际比原定的七月四号早，六月初就搬完了。"
    },
    {
        "id": "P17-CASE-017/d45",
        "claim_type": "date",
        "strip_span": "7月18号",
        "sentence": "- **7月18号**你提到想买打印机放“出租屋”，但这只能算推测，不能说明你已经住进去了",
        "answer": "按我记得的捋：\n\n- **5月24号**你提过计划：打算**7月4号搬**，去红谷滩翠苑路看一室一厅，离法院步行十五分钟，图通勤。\n- 之后没有明确的“搬完了”记录。\n- **7月18号**你提到想买打印机放“出租屋”，但这只能算推测，不能说明你已经住进去了。\n\n所以：计划日期是7月4号；至于有没有真的搬成、哪天搬的，我这里没有确切记录。麻烦你确认一下，我再记准。"
    },
    {
        "id": "P17-CASE-032/d45",
        "claim_type": "date",
        "strip_span": "2026-05-30",
        "sentence": "称呼这块我能确认：私聊时我叫你“书宁”，只有你加班或者喊累的时候才叫“书宁姐”，平时不累的时候别叫，这是2026-05-30说定的",
        "answer": "公告署名原来是什么、后来改成什么，这条我记忆里确实没有，不敢瞎说。\n\n称呼这块我能确认：私聊时我叫你“书宁”，只有你加班或者喊累的时候才叫“书宁姐”，平时不累的时候别叫，这是2026-05-30说定的。另外2026-08-10也定过：写便笺时你叫我“誊写员”，闲聊就叫我“小满”。\n\n公告署名要是你还想找，我可以再搜一遍记忆。"
    },
    {
        "id": "P20-CASE-004/d00",
        "claim_type": "date",
        "strip_span": "19号",
        "sentence": "咖啡先接着戒到19号，之后一天最多一杯",
        "answer": "靠不靠谱，主要看缓冲攒到什么程度——你自己也说了，这步得慢慢来。7月底这个点听着远，其实掰着指头就数到了，把攒钱的进度倒推着算一算就清楚。\n\n别的倒不用担心：第三幕刚过去，手感正顺；老迟那几段前史你还欠着没讲。真正得盯的是作息，这几天你凌晨还在想情节，全职以后没人管着，更容易昼夜颠倒。咖啡先接着戒到19号，之后一天最多一杯。\n\n要不要辞，等缓冲的数字出来再说，比现在干想有用。"
    },
    {
        "id": "P20-CASE-006/d00",
        "claim_type": "date",
        "strip_span": "19号",
        "sentence": "咖啡还戒着呢，到19号，别破例",
        "answer": "无糖热茶呗，你自己不就这规矩：半夜写稿只喝这个，加一点糖都不行，说甜了脑子飘。茶叶多放点，苦一点正好。\n\n咖啡还戒着呢，到19号，别破例。"
    },
    {
        "id": "P20-CASE-011/d45",
        "claim_type": "date",
        "strip_span": "7月18号",
        "sentence": "到7月18号那版里，纽扣碰上车票还会发热",
        "answer": "三次都讲完了，第三次是5月7号那晚讲清的。\n\n纽扣现在算交接钥匙——上一班的人交到下一班手上的那种，不是压箱底的念想。所以早先“念想”那版说法就不作数了，得改。\n\n后来7月初又定过这道具要大改，前头牵连的地方也跟着动。到7月18号那版里，纽扣碰上车票还会发热。具体改到什么份上我记不全，你手头稿子为准。"
    },
    {
        "id": "P20-CASE-028/d45",
        "claim_type": "date",
        "strip_span": "7月3号",
        "sentence": "你提过7月3号路过中央大街，但没直接说住哪个城市",
        "answer": "你提过7月3号路过中央大街，但没直接说住哪个城市；我猜是哈尔滨，不确定，你纠正我。"
    },
    {
        "id": "P20-CASE-042/d15",
        "claim_type": "date",
        "strip_span": "8月15号",
        "sentence": "8月15号那条提到林潇跟你说过什么，但内容断了，我不敢替它往下补",
        "answer": "记得两种说法：三张和四张。8月5号你说，先前跟我讲的是收到三张，可翻记录又看到自己写的是四张；实物没找到，所以当时你也不确定哪个对。\n\n之后有没有定下来，我这边不清楚。8月15号那条提到林潇跟你说过什么，但内容断了，我不敢替它往下补。要不要我查一下？"
    },
    {
        "id": "P01-NEW-006/d03",
        "claim_type": "number",
        "strip_span": "三次",
        "sentence": "蓝色邮票烫得阿眠换了三次手，才把它夹进值班簿",
        "answer": "我记着的就到这儿：那枚钥匙挂在门后的铜钩上，阿岚还没找到锁；她每晚都把朝海那面窗擦干净。蓝色邮票烫得阿眠换了三次手，才把它夹进值班簿；老周说，这邮票只在潮汐镇半夜开窗时才发烫。\n\n再往后，比如钩子上有没有盐霜、半夜有没有人来找她、钥匙到底开哪把锁，这些我都没记着，不敢往下编。"
    }
]


def _config(mode: str = "flag", **overrides: object) -> Config:
    raw = copy.deepcopy(BASE_OVERRIDES)
    verify = {"enabled": True, "mode": mode, "min_claims": 2, "risk_threshold": 0.34, "max_claims": 40}
    verify.update(overrides)
    raw["answer"] = {"verify": verify}
    return Config(raw)


class LegacyCfg:
    """0.39.0 的 cfg 视图：``answer.verify`` 键不存在。"""

    def __init__(self, real: object) -> None:
        self._real = real

    def get(self, key: str, default: object = None) -> object:
        if key in LEGACY_KEYS:
            return default
        return self._real.get(key, default)  # type: ignore[attr-defined]


def _run_counted(store: object, query: str, cfg: object | None = None) -> tuple[dict, list[str]]:
    real_cfg = store.cfg
    if cfg is not None:
        store.cfg = cfg
    statements: list[str] = []
    conn = store.db.conn
    conn.set_trace_callback(statements.append)
    try:
        result = store.search(query, k=8, record=False)
    finally:
        conn.set_trace_callback(None)
        store.cfg = real_cfg
    return result, statements


def _payload(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "latency_ms"}


class VerifyBase(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, config: Config | None = None, *, seed: bool = True) -> PMem:
        path = temp_dir("answer-verify")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=config)
        self._apps.append(app)
        if seed:
            app.store.add(
                "2026年11月5日和李静去看了房子",
                session_id="s1",
                created_at="2026-11-05T10:00:00+00:00",
            )
            app.store.add("用户住在杭州", session_id="s1")
            app.store.add("2026年10月1日去爬了山", session_id="s1",
                          created_at="2026-10-01T10:00:00+00:00")
        return app

    def _items(self, app: PMem, query: str = "李静 房子 杭州 咖啡") -> list[dict]:
        return app.store.search(query, k=8, record=False)["items"]


class DisabledTest(VerifyBase):
    def test_module_returns_enabled_false_and_zero_sql(self) -> None:
        app = self._open()  # 默认配置 answer.verify.enabled=false
        statements: list[str] = []
        conn = app.db.conn
        conn.set_trace_callback(statements.append)
        try:
            result = answer_verify.verify_answer(app.store, "2026年12月24日去看了房子")
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(result, {"enabled": False})
        self.assertEqual(statements, [], "关时不应该发任何 SQL")

    def test_sdk_short_circuits(self) -> None:
        app = self._open()
        payload = app.verify_answer("随便一句答案", items=[])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], {"enabled": False})

    def test_render_and_search_byte_identical_to_legacy_view(self) -> None:
        app = self._open()
        app.store.search("李静 房子", k=8, record=False)  # 预热
        real_result, real_sql = _run_counted(app.store, "李静 房子")
        legacy_result, legacy_sql = _run_counted(app.store, "李静 房子", LegacyCfg(app.store.cfg))
        self.assertEqual(real_sql, legacy_sql)
        self.assertEqual(
            json.dumps(_payload(real_result), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(legacy_result), ensure_ascii=False, sort_keys=True),
        )
        items = self._items(app)
        real_render = app.render(items=items)
        real_cfg = app.config
        app.config = LegacyCfg(real_cfg)  # type: ignore[assignment]
        try:
            legacy_render = app.render(items=items)
        finally:
            app.config = real_cfg  # type: ignore[assignment]
        self.assertEqual(
            real_render.get("data"),
            legacy_render.get("data"),
            "关时 PMem.render 输出必须与旧视图逐字节一致",
        )

    def test_mcp_tool_listed_and_disabled(self) -> None:
        path = temp_dir("answer-verify-mcp")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db")
        app.close()
        mcp = InProcessMcp(path / "pmem.db")
        try:
            names = {tool["name"] for tool in mcp.call("tools/list")["result"]["tools"]}
            self.assertIn("memory_verify_answer", names)
            data = mcp.tool("memory_verify_answer", {"answer": "2026年12月24日去了"})
            self.assertEqual(data["data"], {"enabled": False})
        finally:
            mcp.close()

    def test_cli_verify_short_circuits(self) -> None:
        from pmem.gate import cli

        path = temp_dir("answer-verify-cli")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db")
        app.close()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--db", str(path / "pmem.db"), "--json", "verify", "测试答案"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["data"], {"enabled": False})


class EnabledTest(VerifyBase):
    def setUp(self) -> None:
        super().setUp()
        self.app = self._open(_config())
        self.items = self._items(self.app)

    def _verify(self, answer: str, *, items: list[dict] | None = "default", cfg: Config | None = None):
        real_cfg = self.app.store.cfg
        if cfg is not None:
            self.app.store.cfg = cfg
        try:
            return answer_verify.verify_answer(
                self.app.store, answer, items=self.items if items == "default" else items
            )
        finally:
            self.app.store.cfg = real_cfg

    def test_supported_date_different_literal_form(self) -> None:
        result = self._verify("11月5日你们去看了房子。")
        dates = [c for c in result["claims"] if c["kind"] == "date"]
        self.assertTrue(dates)
        self.assertTrue(all(c["supported"] for c in dates), "不同写法应按解析后的日期判支撑")

    def test_supported_name_and_relation(self) -> None:
        result = self._verify("李静在杭州当老师，用户住在杭州。")
        self.assertTrue(result["claims"])
        self.assertTrue(all(c.get("supported") for c in result["claims"]), result["claims"])

    def test_fabricated_date_is_flagged(self) -> None:
        result = self._verify("你们 2026年12月24日又去看了房子。")
        bad = [c for c in result["claims"] if not c["supported"]]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["kind"], "date")
        self.assertEqual(bad[0]["reason"], "date_fabricated")
        self.assertTrue(result["flagged"])
        self.assertGreaterEqual(result["risk"], 0.34)
        self.assertIn("删掉", bad[0]["fix"])

    def test_fabricated_name_is_reported_but_not_scored(self) -> None:
        # 专名抽取误伤 8.8%（H1），默认 kinds 只含 date/relative_date → 只报告不计风险。
        result = self._verify("赵铁柱也去了，还说要请你喝咖啡。")
        bad = [c for c in result["claims"] if not c["supported"]]
        self.assertTrue(bad)
        self.assertEqual(bad[0]["kind"], "name")
        self.assertEqual(bad[0]["reason"], "name_fabricated")
        self.assertEqual(result["unsupported"], 0, "非计分类型不算 risk")
        self.assertGreaterEqual(result["reported_unsupported"], 1)
        self.assertFalse(result["flagged"])

    def test_number_without_support_is_reported_but_not_scored(self) -> None:
        result = self._verify("你们花了 8888 元吃火锅。")
        bad = [c for c in result["claims"] if not c["supported"]]
        self.assertEqual([c["kind"] for c in bad], ["number"])
        self.assertEqual(result["unsupported"], 0)
        self.assertFalse(result["flagged"])

    def test_date_in_db_but_not_in_context_is_classified(self) -> None:
        # 10月1日那条记忆在库里，但这次注入的 items 里没有它 → 不是编造，是「没注入」。
        result = self._verify("你们 10月1日去爬了山。")
        bad = [c for c in result["claims"] if not c["supported"]]
        self.assertTrue(bad)
        self.assertEqual(bad[0]["reason"], "date_not_in_context")

    def test_pure_evaluation_not_penalized(self) -> None:
        result = self._verify("那次真的很开心，感觉特别值得。")
        self.assertEqual(result["typed_claims"], 0)
        self.assertEqual(result["unsupported"], 0)
        self.assertEqual(result["risk"], 0.0)
        self.assertFalse(result["flagged"])

    def test_short_answer_denominator_guard(self) -> None:
        result = self._verify("2026年12月24日。")
        self.assertEqual(result["typed_claims"], 1)
        self.assertEqual(result["unsupported"], 1)
        self.assertEqual(result["risk"], 0.5, "短答案分母是 max(min_claims, typed)")

    def test_no_items_skips_support_judgement(self) -> None:
        result = self._verify("2026年12月24日去看了房子。", items=None)
        self.assertEqual(result["unsupported"], 0)
        self.assertEqual(result["risk"], 0.0)
        self.assertIn("跳过支撑判定", result["note"])
        self.assertTrue(all(c["supported"] is None for c in result["claims"]))

    def test_empty_items_marks_everything_unsupported(self) -> None:
        result = self._verify("2026年12月24日去看了房子。", items=[])
        self.assertEqual(result["unsupported"], 1)
        self.assertEqual(result["risk"], 0.5)

    def test_hint_mode_gives_minimal_fix_without_rewriting(self) -> None:
        result = self._verify("2026年12月24日去看了房子。", cfg=_config("hint"))
        self.assertEqual(result["mode"], "hint")
        self.assertTrue(result["hints"])
        self.assertIn("删掉", result["hint"])
        self.assertNotIn("stripped", result, "hint 模式不改答案")

    def test_strip_mode_is_conservative_and_keeps_citations(self) -> None:
        answer = "你们 2028年1月1日去滑雪。\n你们 2026年12月24日又去了[2]。\n那天很开心。"
        result = self._verify(answer, cfg=_config("strip"))
        stripped = result["stripped"]
        self.assertTrue(stripped, "无引用的无支撑句应被删掉")
        self.assertNotIn("2028年1月1日", stripped)
        self.assertIn("[2]", stripped, "带引用编号的句子不能被删（会破坏编号）")
        self.assertIn("那天很开心", stripped, "纯评价句不该被删")
        self.assertEqual(len(result["removed"]), 1)

    def test_invalid_mode_falls_back_to_flag(self) -> None:
        result = self._verify("2026年12月24日去看了房子。", cfg=_config("nonsense"))
        self.assertEqual(result["mode"], "flag")
        self.assertNotIn("stripped", result)

    def test_sdk_returns_report_when_enabled(self) -> None:
        payload = self.app.verify_answer(
            "2026年12月24日去看了房子。", items=self.items, query="哪天去的？"
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["enabled"])
        self.assertIn("保真检查", payload["data"]["report"])


class _FakeStore:
    """只带 cfg 的壳：verify_answer 需要 store.cfg；没有 db 时跳过 DB 分类。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg


class H1StripRegressionTest(unittest.TestCase):
    """grader-dev 的 20 条 H1 strip 样例回归：日期类必须能删掉被判分点名的那句。

    只做回归断言，不依赖外部 benchmark 路径（answer/sentence 已内联）。
    两条裸「19号」由 verifier 的保守裸日号分支覆盖；「三次」属于数字类——
    按 H1 结论（13 条误伤里 10 条来自数字）数字类**只 flag 不 strip**。
    """

    def test_date_samples_strip_the_flagged_sentence(self) -> None:
        store = _FakeStore(_config("strip"))
        date_samples = [s for s in H1_STRIP_SAMPLES if s["claim_type"] in ("date", "relative_date")]
        self.assertEqual(len(date_samples), 19)
        for sample in date_samples:
            with self.subTest(id=sample["id"]):
                result = answer_verify.verify_answer(store, sample["answer"], items=[])
                stripped = result.get("stripped") or ""
                self.assertNotIn(sample["sentence"], stripped, "被判分点名的那句没删掉")
                self.assertTrue(result.get("removed"), "strip 没记录删了哪句")
                self.assertTrue(
                    any(c["kind"] in ("date", "relative_date") and c["supported"] is False for c in result["claims"]),
                    "没识别出日期类无支撑断言",
                )

    def test_number_sample_is_flagged_not_stripped(self) -> None:
        sample = next(s for s in H1_STRIP_SAMPLES if s["claim_type"] == "number")
        store = _FakeStore(_config("strip"))
        result = answer_verify.verify_answer(store, sample["answer"], items=[])
        # 「三次」是中文数字，当前阿拉伯数字抽取器不认；但即使认出来，数字类也不触发 strip。
        self.assertIsNone(result.get("stripped"), "数字类不该触发 strip")




class KindsScopeTest(unittest.TestCase):
    """``answer.verify.kinds``：flag 报告所有类型，risk/strip 只算 kinds 里的。"""

    def test_default_kinds_only_dates(self) -> None:
        store = _FakeStore(_config())
        result = answer_verify.verify_answer(store, "赵铁柱来了，2026年12月24日。", items=[])
        self.assertEqual(result["kinds"], ["date", "relative_date"])
        self.assertEqual(len(result["claims"]), 2)  # name + date 都报告
        self.assertEqual(result["unsupported"], 1, "只有 date 计分")
        self.assertEqual(result["reported_unsupported"], 2)
        self.assertEqual(result["risk"], 0.5)  # 1 / max(2, 1)
        self.assertTrue(result["flagged"])

    def test_custom_kinds_can_score_names(self) -> None:
        store = _FakeStore(_config(kinds=["name"]))
        result = answer_verify.verify_answer(store, "赵铁柱来了。", items=[])
        self.assertEqual(result["kinds"], ["name"])
        self.assertEqual(result["unsupported"], 1)
        self.assertEqual(result["risk"], 0.5)
        self.assertTrue(result["flagged"])

    def test_empty_kinds_scores_nothing(self) -> None:
        store = _FakeStore(_config(kinds=[]))
        result = answer_verify.verify_answer(store, "赵铁柱 2026年12月24日来了。", items=[])
        self.assertEqual(result["unsupported"], 0)
        self.assertEqual(result["risk"], 0.0)
        self.assertFalse(result["flagged"])
        self.assertTrue(result["claims"], "不计分也要报告 claim")


class MechanismTest(unittest.TestCase):
    """机制层的几条反直觉约束（时间词不是专名 / 右边界回退 / 数字不 strip）。"""

    def setUp(self) -> None:
        self.store = _FakeStore(_config())

    def test_time_words_are_not_name_claims(self) -> None:
        result = answer_verify.verify_answer(self.store, "米米周三要值日。", items=[])
        self.assertFalse(
            any(c["kind"] == "name" and "周" in c["text"] for c in result["claims"]),
            "「周三」是时间词，不该被当成专名断言",
        )
        self.assertIsNone(result.get("stripped"))

    def test_name_right_boundary_falls_back_to_known_prefix(self) -> None:
        items = [{"id": "m1", "text": "叶楠是结构工程师", "day": "2026-05-01"}]
        result = answer_verify.verify_answer(self.store, "叶楠生日最后确认是八月十八号。", items=items)
        names = [c for c in result["claims"] if c["kind"] == "name"]
        self.assertTrue(names)
        self.assertTrue(names[0]["supported"], names[0])
        self.assertEqual(names[0]["text"], "叶楠", "右边界被多吃一个字时应回退到已知前缀")

    def test_number_only_sentence_is_never_stripped(self) -> None:
        result = answer_verify.verify_answer(
            _FakeStore(_config("strip")), "你们花了 8888 元吃火锅。", items=[]
        )
        self.assertIsNone(result.get("stripped"), "数字类只 flag，不 strip")
        self.assertTrue(
            any(c["kind"] == "number" and not c["supported"] for c in result["claims"])
        )

    def test_supported_sentence_blocks_strip(self) -> None:
        items = [{"id": "m1", "text": "4月5日去看了房子", "day": "2026-04-05"}]
        answer = "4月5日你们去看了房子。4月27日又去了。"
        result = answer_verify.verify_answer(_FakeStore(_config("strip")), answer, items=items)
        stripped = result.get("stripped") or ""
        self.assertIn("4月5日", stripped, "有支撑的句子不能被删")
        self.assertNotIn("4月27日", stripped, "无支撑日期的句子应被删")

    def test_mid_word_name_candidate_is_skipped(self) -> None:
        # 「关于备用钥匙」会被姓氏表切成「于备用」——左边界是功能字，跳过（宁少报）。
        result = answer_verify.verify_answer(self.store, "材料里关于备用钥匙的交接有两条说法。", items=[])
        self.assertFalse(
            any(c["kind"] == "name" and "备用" in c["text"] for c in result["claims"]),
            "词中间切出来的假名字不该被当成专名断言",
        )


class StripDbGuardTest(VerifyBase):
    """strip 的产品安全阀：库里其实有的日期，不因「这次没注入」被删。"""

    def test_strip_keeps_date_that_exists_in_db_but_not_in_context(self) -> None:
        app = self._open(_config("strip"))
        app.store.add(
            "2026年7月12日是我的生日",
            session_id="s1",
            created_at="2026-07-12T10:00:00+00:00",
        )
        items = app.store.search("咖啡 杭州 房子", k=8, record=False)["items"]
        result = answer_verify.verify_answer(app.store, "7月12号，公历。", items=items)
        bad = [c for c in result["claims"] if not c["supported"]]
        self.assertTrue(bad)
        self.assertEqual(bad[0]["reason"], "date_not_in_context", "应分类为「库里有但没注入」")
        self.assertTrue(all(c.get("db_checked") for c in result["claims"]))
        self.assertIsNone(result.get("stripped"), "库里有的日期不能被 strip 误删")


class NumberAssertionTest(unittest.TestCase):
    """数字断言（task-34）：带单位/量词才算有型；纯序数、裸数字不罚；默认不计分。"""

    def setUp(self) -> None:
        self.store = _FakeStore(_config())

    def _numbers(self, answer: str, items: list[dict] | None = None):
        result = answer_verify.verify_answer(self.store, answer, items=[] if items is None else items)
        return [c for c in result["claims"] if c["kind"] == "number"], result

    def test_arabic_number_with_unit_detected(self) -> None:
        nums, result = self._numbers("你们花了 8888 元吃火锅。")
        self.assertEqual(len(nums), 1)
        self.assertEqual(nums[0]["text"], "8888 元")
        self.assertEqual(nums[0]["value"], 8888.0)
        self.assertFalse(nums[0]["supported"])
        self.assertEqual(result["unsupported"], 0, "number 默认不在 kinds → 只报告不计风险")
        self.assertFalse(result["flagged"])

    def test_chinese_number_with_unit_detected(self) -> None:
        nums, _result = self._numbers("那周跑了三次医院。")
        self.assertTrue(nums, "「三次」是带量词的中文数字断言")
        self.assertEqual(nums[0]["text"], "三次")
        self.assertEqual(nums[0]["value"], 3.0)

    def test_bare_number_and_ordinal_not_typed(self) -> None:
        nums, _result = self._numbers("第3次去，年龄 24。")
        self.assertEqual(nums, [], "序数（第3次）和裸数字（24）都不是有型数字断言")

    def test_number_supported_by_value_across_notations(self) -> None:
        items = [{"id": "m1", "text": "去医院挂了三次号", "day": "2026-05-01"}]
        nums, _result = self._numbers("那周跑了 3 次医院。", items=items)
        self.assertTrue(nums)
        self.assertTrue(nums[0]["supported"], "「3 次」应能对上材料里的「三次」")


class NameExtractionRegressionTest(unittest.TestCase):
    """专名抽取 FP 压缩（task-34）：词片段不报，真名召回报。"""

    def setUp(self) -> None:
        self.store = _FakeStore(_config())

    def _names(self, answer: str):
        result = answer_verify.verify_answer(self.store, answer, items=[])
        return [c for c in result["claims"] if c["kind"] == "name"], result

    def test_word_fragments_are_not_names(self) -> None:
        for answer in (
            "别塞进行李箱，晕车药提前吃。",
            "米米周三要值日，得晚点接。",
            "是一张旧地图，上面画着一只蓝色小船。",
            "你翻出那张报价单，上面写安装费另算。",
        ):
            names, _result = self._names(answer)
            self.assertEqual(names, [], f"普通词被切成了人名：{answer}")

    def test_real_names_are_recalled(self) -> None:
        for answer, expected in (
            ("你上周和李婷去看了房子。", "李婷"),
            ("提到林潇跟你说过什么。", "林潇"),
            ("刘颖改过庭室的述职材料。", "刘颖"),
        ):
            names, _result = self._names(answer)
            self.assertIn(expected, [c["text"] for c in names], answer)
            self.assertTrue(all(not c["supported"] for c in names))

    def test_name_report_does_not_enter_risk_by_default(self) -> None:
        _names_list, result = self._names("李婷来了。")
        self.assertEqual(result["unsupported"], 0)
        self.assertFalse(result["flagged"])
        self.assertGreaterEqual(result["reported_unsupported"], 1)


class DateDerivationTest(VerifyBase):
    """task-36：中文数字日期 / 相对日期推导后不再误判 fabricated。"""

    def _verify(self, answer: str, items: list[dict], mode: str = "flag") -> dict:
        return answer_verify.verify_answer(_FakeStore(_config(mode)), answer, items=items)

    def test_cn_number_date_supported_by_item_text(self) -> None:
        items = [{"id": "m1", "text": "孩子生日是十二月二号，想请小朋友来家里画画", "day": "2026-06-01"}]
        result = self._verify("日期是 12月2号。", items)
        dates = [c for c in result["claims"] if c["kind"] == "date"]
        self.assertTrue(dates)
        self.assertTrue(dates[0]["supported"], "原文「十二月二号」应能支撑「12月2号」")

    def test_relative_weekday_derived_from_item_record_day(self) -> None:
        clock.set_now("2026-07-01T10:00:00+08:00")
        try:
            items = [{"id": "m1", "text": "奶油要周五下午送来，收货后先放冰箱", "day": "2026-06-30"}]
            result = self._verify("周五（7月3日）下午送到。", items)
        finally:
            clock.reset_now()
        dates = [c for c in result["claims"] if c["kind"] == "date"]
        relatives = [c for c in result["claims"] if c["kind"] == "relative_date"]
        self.assertTrue(any(c["text"] == "7月3日" and c["supported"] for c in dates), dates)
        self.assertTrue(any(c["text"] == "周五" and c["supported"] for c in relatives), relatives)

    def test_unresolved_relative_date_is_not_fabricated(self) -> None:
        app = self._open(_config("strip"))  # 有 DB：相对日期换算不可靠时只标 not_in_context
        result = answer_verify.verify_answer(
            app.store, "明天还得早起陪你爸去社区医院。", items=[], query=""
        )
        bad = [c for c in result["claims"] if c.get("supported") is False]
        self.assertTrue(bad)
        self.assertTrue(all(c["reason"].endswith("not_in_context") for c in bad), bad)
        self.assertIsNone(result.get("stripped"), "相对日期不能被 strip 当编造删掉")

    def test_yearless_date_matches_same_month_day_across_year(self) -> None:
        # 跨年 rollover：6/1 说的「12月2号」按最近一次推成 2025-12-02，
        # 但库/上下文里是 2026-12-02 的同月同日 → 不能判编造。
        clock.set_now("2026-06-01T10:00:00+08:00")
        try:
            items = [{"id": "m1", "text": "住户搬到了海棠溪那边", "day": "2026-12-02"}]
            result = self._verify("12月2号。", items)
        finally:
            clock.reset_now()
        dates = [c for c in result["claims"] if c["kind"] == "date"]
        self.assertTrue(dates)
        self.assertTrue(dates[0]["supported"], dates)

    def test_truly_fabricated_absolute_date_still_detected(self) -> None:
        app = self._open(_config())
        result = answer_verify.verify_answer(app.store, "你 9月9日去看了房子。", items=[])
        bad = [c for c in result["claims"] if c.get("supported") is False]
        self.assertTrue(bad)
        self.assertEqual(bad[0]["reason"], "date_fabricated")


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
