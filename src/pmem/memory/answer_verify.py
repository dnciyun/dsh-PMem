"""答案保真减法（P0-A / 0.40.0，默认关）：只指出「有型且上下文无支撑」的断言。

**和 selfcheck 的分工（刻意不合并）**：

- ``memory.selfcheck.check_answer`` = 作答**前**的核对报告：答案里的日期/专名在整个
  记忆库里有没有出处，产出 issue 清单（给写答案的人看）；
- ``memory.answer_verify.verify_answer`` = 作答**后**的保真减法：以**本次注入的
  items 为上下文**，逐 claim 判断有型断言有没有支撑，产出 claim 级清单 + risk +
  「最小修法」（flag/hint/strip 三种模式）。两者取数、判据、产物都不同。

机制（禁止 if-else 模板，抽取器全部复用现成件）：

1. **切分**：答案 → 句子（。！？；换行）→ 短语（，、,），各自保留在原答案里的位置；
2. **有型槽**：日期复用 ``selfcheck._extract_dates/_resolve_date``（另补相对日期走
   ``timeresolve.relative_dates``、裸「N号」走保守分支）；专名复用
   ``selfcheck._name_candidates``（task-34 起带边界/词性/实体模板共现证据打分，
   阈值 ``answer.verify.name_min_score`` 可覆盖）；关系谓词复用 ``net.relations.extract``；
   数字=**带单位/量词**的阿拉伯数字或中文数字（「三次」「8888 元」），纯序数「第3次」
   和裸数字「24」不算（日期/引用编号占过的位置不重复计）；
3. **支撑**：按类型在注入 items 的正文/结构化字段里找同型命中——日期按**解析后的
   日期**比（不按字面），关系按 ``(predicate, object)`` 对，专名按正文/entities 命中
   （带右边界回退：「叶楠生」能退回上下文里的「叶楠」），数字按字面命中；
4. 只有「**计分类型（``answer.verify.kinds``）里的有型断言且无支撑**」计入
   ``unsupported``；其它类型（数字/专名/关系）只报告、不计风险（``reported_unsupported``）；
   纯评价/主观句（没有有型槽）既不进分子也不进分母；
5. ``risk = unsupported / max(min_claims, scored_claims)``（分母防短答案误伤；
   ``scored_claims`` 只数计分类型）。H1 实测依据：日期类 21.5% 召回/0.5% 误伤、
   数字类 23.7%/2.2%、专名 43%/8.8%（「行李箱→李箱」这类切错）——所以默认 kinds
   只放 ``date`` / ``relative_date``。
6. DB 复核只用于**分类**（复用 ``selfcheck._check_dates/_check_names``）：没进上下文但
   库里有 → ``not_in_context``（建议补注入）；库里也没有 → 更像编的（``fabricated``）。
   这一步是「机制」不是「模板」：分类只由这两个确定性检查的结果决定；
7. ``mode=strip`` **只删计分类型（默认日期类）**，且只删「整句有型断言全是无支撑
   计分类型」的句子、带引用编号的句子不动；**有 DB 可核时只删「库里也没有」的日期**
   （``date_fabricated``）——「库里有、只是这次没注入」是检索漏召，删了会误伤正确答案。

``enabled=false``（默认）时直接返回 ``{"enabled": False}``，不读库、不算任何东西。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..base import clock
from . import selfcheck as selfcheck_mod
from . import timeresolve
from .retrieval import timecue
from .text import normalize

# 句子边界：中文句末标点 + 换行；短语边界：逗号/顿号。
_SENTENCE_RE = re.compile(r"[^。！？；\n]+(?:[。！？；\n]+|$)")
_PHRASE_RE = re.compile(r"[^，、,]+")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# 数字断言（task-34）：**必须带单位/量词**才算有型；纯序数（第3次）与裸数字不算。
# 阿拉伯数字 + 单位（含「%」），中文数字 + 单位。日期占过的位置不会重复计。
_NUM_UNIT_AR = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(个|只|条|次|件|位|份|张|台|套|句|段|篇|页|块|岁|天|日|周|星期|月|年|"
    r"小时|分钟|秒|元|角|分|斤|公斤|公里|米|人|层|倍|%|％)"
)
_CN_NUM_TOKEN = r"[零一二两三四五六七八九十百千万半]+"
_NUM_UNIT_CN = re.compile(
    rf"(?<!第)({_CN_NUM_TOKEN})\s*"
    r"(个|只|条|次|件|位|份|张|台|套|句|段|篇|页|块|岁|天|日|周|星期|月|年|"
    r"小时|分钟|秒|元|角|分|斤|公斤|公里|米|人|层|倍)"
)
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_number(token: str) -> float | None:
    """中文数字 → 数值（「三次」=3、「一百二十」=120、「两个半」=2.5）；不懂返回 None。

    只服务数字断言的**有型**判断（带单位才用），不做通用数字解析。
    """
    text = str(token or "").strip()
    if not text:
        return None
    half = 0.0
    if text.endswith("半"):
        half = 0.5
        text = text[:-1]
    if not text:
        return half
    total = 0.0
    section = 0.0
    number = 0.0
    for char in text:
        if char in _CN_DIGITS:
            number = float(_CN_DIGITS[char])
        elif char in _CN_UNITS:
            section += (number or 1.0) * _CN_UNITS[char]
            number = 0.0
        elif char == "万":
            section = (section + number) * 10000.0
            total += section
            section = 0.0
            number = 0.0
        else:
            return None
    return total + section + number + half
# 裸「19号 / 5日」：selfcheck 的日期抽取器要求「有月」或完整日期，这里补一个保守分支
# （只在短语里没有其它日期写法时启用）。「3号线/5号楼」这类不是日期，用后一个字排除。
_BARE_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[号日](?![号线楼层房桌车床位])")
_CITATION_RE = re.compile(r"\[\s*\d+\s*\]|【\s*\d+\s*】|\[\s*[A-Za-z]{1,3}[\-\d]*\s*\]")

# 每种有型槽的最小修法（只给「怎么改这一处」，不重写答案）。
_FIX_TEXT = {
    "date": "删掉「{span}」或补上它对应的那天",
    "relative_date": "把「{span}」删掉或换算成有依据的绝对日期",
    "name": "删掉「{span}」或换成上下文里出现过的名字",
    "number": "删掉「{span}」或给出上下文里的原数",
    "relation": "删掉/改写「{span}」，上下文里找不到这个说法",
}
# 只有这些类型进 risk / strip（config: answer.verify.kinds；H1 实测依据见模块 docstring）。
_DEFAULT_KINDS = frozenset({"date", "relative_date"})


def _cfg_dict(store: Any) -> dict[str, Any]:
    cfg = getattr(store, "cfg", None)
    raw = cfg.get("answer.verify") if cfg is not None else None
    return raw if isinstance(raw, dict) else {}


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _SENTENCE_RE.finditer(text) if m.group(0).strip()]


def _phrase_spans(sentence: str, offset: int) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for match in _PHRASE_RE.finditer(sentence):
        chunk = match.group(0)
        if not chunk.strip():
            continue
        out.append((offset + match.start(), offset + match.end(), chunk))
    return out


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in occupied)


def _date_of_item(item: dict[str, Any]) -> list[str]:
    """条目自带的结构化日期（day / valid_from）。"""
    days: list[str] = []
    for key in ("day", "valid_from"):
        raw = str(item.get(key) or "").strip()
        if len(raw) >= 10 and raw[:4].isdigit():
            days.append(raw[:10])
    return days


class _Context:
    """本次注入 items 的支撑面：正文、结构化日期、专名、数字、关系对。"""

    def __init__(self, items: list[dict[str, Any]], *, today: date) -> None:
        self.texts: list[str] = []
        self.days: set[str] = set()
        self.md_days: set[str] = set()
        self.names: set[str] = set()
        self.numbers: set[str] = set()
        self.number_values: set[float] = set()
        self.relations: set[tuple[str, str]] = set()
        from ..net import relations as net_relations

        for item in items:
            if not isinstance(item, dict):
                continue
            body = str(item.get("text") or item.get("text_raw") or "")
            if not body:
                continue
            self.texts.append(normalize(body))

            def add_day(iso: str) -> None:
                if len(iso) >= 10:
                    self.days.add(iso[:10])
                    self.md_days.add(iso[5:10])

            for day in _date_of_item(item):
                add_day(day)
            for name in item.get("entities") or []:
                if str(name).strip():
                    self.names.add(str(name).strip())
            # 条目正文里的日期：显式写法 + 按这条记忆自己的日期换算出的相对日期。
            for hit in selfcheck_mod._extract_dates(body, today=today, limit=20):
                target = selfcheck_mod._resolve_date(hit, today)
                if target is not None:
                    add_day(target.isoformat())
            base = str(item.get("valid_from") or item.get("day") or "")
            for _phrase, iso in timeresolve.loose_relative_dates(body, base):
                add_day(iso)
            # 关系谓词：复用 net.relations.extract（未知宾语不建边，宁缺勿错）。
            try:
                for triple in net_relations.extract(body, []):
                    dst = triple.get("dst") or {}
                    surface = str(dst.get("name") or "").strip()
                    if surface:
                        self.relations.add((str(triple.get("predicate")), surface))
            except Exception:  # noqa: BLE001 关系抽取是增强，挂了只是少一类支撑
                pass
        self.joined = "\n".join(self.texts)
        self.numbers = set(_NUMBER_RE.findall(self.joined))
        # 数字断言的支撑按「数值」比（「三次」能对上材料里的「3 次」）：上下文里所有
        # 带单位数字 + 裸数字都转成数值集合。
        for match in _NUM_UNIT_AR.finditer(self.joined):
            try:
                self.number_values.add(float(match.group(1)))
            except ValueError:
                pass
        for match in _NUM_UNIT_CN.finditer(self.joined):
            value = _cn_number(match.group(1))
            if value is not None:
                self.number_values.add(float(value))
        for token in _NUMBER_RE.findall(self.joined):
            try:
                self.number_values.add(float(token))
            except ValueError:
                pass

    def supports_date(self, target: date | None, literal: str, *, year_known: bool = True) -> bool:
        if bool(literal) and literal in self.joined:
            return True
        if target is None:
            return False
        if target.isoformat() in self.days:
            return True
        # 没写年份时按「同月日」宽容比：跨年误差（12月2号 vs 材料里的十二月二号按记录日
        # 落在 2026-12-02 而按答题日推成 2025-12-02）不该判编造——宁可漏报不可误报。
        if not year_known and target.strftime("%m-%d") in self.md_days:
            return True
        return False

    def supports_name(self, name: str) -> bool:
        return bool(name) and (name in self.joined or name in self.names)

    def supports_number(self, literal: str, value: float | None = None) -> bool:
        if literal and (literal in self.joined or literal in self.numbers):
            return True
        if value is None:
            return False
        return any(abs(item - float(value)) < 1e-9 for item in self.number_values)

    def supports_relation(self, predicate: str, surface: str) -> bool:
        return (predicate, surface) in self.relations

    def matching_item(self, items: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if needle and needle in normalize(str(item.get("text") or "")):
                return item
        return None


def _evidence(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not item:
        return []
    return [
        {
            "id": str(item.get("id") or ""),
            "day": str(item.get("day") or "")[:10],
            "text": str(item.get("text") or "")[:80],
        }
    ]


def _phrases(text: str) -> list[tuple[int, int, int, str]]:
    """(sentence_index, start, end, phrase_text) 列表。"""
    out: list[tuple[int, int, int, str]] = []
    for index, (start, end) in enumerate(_sentence_spans(text)):
        for p_start, p_end, chunk in _phrase_spans(text[start:end], start):
            out.append((index, p_start, p_end, chunk))
    return out


def _claims_for_phrase(
    phrase: str, *, base: int, today: date, max_claims: int, name_min_score: float | None = None
) -> list[dict[str, Any]]:
    """从一条短语里抽有型槽；返回 claim 骨架（还未判支撑）。"""
    claims: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []

    def add(
        kind: str, start: int, end: int, literal: str, value: Any, year_known: bool = False
    ) -> None:
        if len(claims) >= max_claims or not literal:
            return
        claims.append(
            {
                "kind": kind,
                "span": [base + start, base + end],
                "text": literal,
                "value": value,
                "year_known": bool(year_known),
                "supported": False,
                "reason": "",
                "fix": "",
            }
        )
        occupied.append((start, end))

    # 1) 日期（复用 selfcheck 抽取器 + 解析器；按解析后的日期比，不按字面）。
    for hit in selfcheck_mod._extract_dates(phrase, today=today, limit=max_claims):
        target = selfcheck_mod._resolve_date(hit, today)
        add(
            "date",
            int(hit["start"]),
            int(hit["start"]) + len(str(hit["span"])),
            str(hit["span"]),
            target.isoformat() if target is not None else None,
            hit.get("year") is not None,
        )

    # 1b) 相对日期（明天/这周日/三天前/裸周五/上个月24号）：复用 timeresolve 换算成绝对日期再判支撑。
    for phrase_text, iso in timeresolve.loose_relative_dates(phrase, clock.now_iso()):
        pos = phrase.find(phrase_text)
        if pos < 0 or _overlaps((pos, pos + len(phrase_text)), occupied):
            continue
        add("relative_date", pos, pos + len(phrase_text), phrase_text, iso)

    # 1c) 裸「N号/N日」：只在短语里没有任何其它日期写法时兜底（月/年未知 → value=None）。
    if not any(claim["kind"] in ("date", "relative_date") for claim in claims):
        for match in _BARE_DAY_RE.finditer(phrase):
            if _overlaps(match.span(), occupied):
                continue
            add("date", match.start(), match.end(), match.group(0), None)

    # 2) 专名（复用 selfcheck 的名字候选；已知名字遮蔽留空，避免把上下文里的名字当新名字）。
    names = selfcheck_mod._name_candidates(
        phrase, spans=[], limit=max_claims, min_score=name_min_score
    )
    cursor = 0
    for name in names:
        # 时间词不是专名：「周三」「今天」这类在姓氏表里能切出 2 字候选，但它们是时间槽。
        # 复用 timecue 的现成词表/正则，不再造第二套时间词。
        if (
            timecue._WEEKDAY.fullmatch(name)
            or name in timecue._STRONG_WORDS
            or name in timecue._WEAK_WORDS
        ):
            continue
        pos = phrase.find(name, cursor)
        if pos < 0:
            pos = phrase.find(name)
        if pos < 0 or _overlaps((pos, pos + len(name)), occupied):
            continue
        cursor = pos + len(name)
        add("name", pos, pos + len(name), name, name)

    # 3) 关系谓词（复用 net.relations.extract；只认宾语类型可定的，未知宾语不算断言）。
    from ..net import relations as net_relations

    try:
        triples = net_relations.extract(phrase, [])
    except Exception:  # noqa: BLE001 关系抽取挂了就当这句没有关系断言
        triples = []
    for triple in triples:
        dst = triple.get("dst") or {}
        surface = str(dst.get("name") or "").strip()
        if not surface or len(surface) > 24:
            continue
        start = int(triple.get("offset") or 0)
        add("relation", start, start + len(surface), surface, f"{triple.get('predicate')}|{surface}")

    # 4) 数字断言（task-34）：**带单位/量词**才是有型断言；纯序数（第3次）不算。
    for pattern, chinese in ((_NUM_UNIT_AR, False), (_NUM_UNIT_CN, True)):
        for match in pattern.finditer(phrase):
            span = match.span()
            if _overlaps(span, occupied):
                continue
            prefix = phrase[max(0, span[0] - 2) : span[0]]
            if prefix.strip().endswith("第"):
                continue  # 序数：第3次/第2名
            if _CITATION_RE.search(phrase[max(0, span[0] - 2) : span[1] + 2]):
                continue
            value = _cn_number(match.group(1)) if chinese else float(match.group(1))
            add("number", span[0], span[1], match.group(0), value)
    return claims


def _db_classification(
    store: Any, text: str, *, today: date, max_claims: int, name_min_score: float | None = None
) -> tuple[set[str], set[str]]:
    """DB 复核（只用于分类）：返回 (库里也没有的日期字面, 库里也没有的专名)。

    复用 selfcheck 的两条确定性检查：它们「有 issue」= 库里没有出处。
    """
    bare_dates: set[str] = set()
    bare_names: set[str] = set()
    conn = getattr(getattr(store, "db", None), "conn", None)
    subject = str(getattr(store, "subject", "self"))
    if conn is None:
        return bare_dates, bare_names
    try:
        issues, _checked, _cut = selfcheck_mod._check_dates(
            conn, subject, text, today=today, limit=max_claims, evidence_limit=1
        )
        bare_dates = {str(issue.get("span") or "") for issue in issues}
    except Exception:  # noqa: BLE001 老库/坏表：分类退化为「上下文没支撑」即可
        pass
    try:
        issues, _checked, _cut = selfcheck_mod._check_names(
            conn, subject, text, max_names=max_claims, min_score=name_min_score
        )
        bare_names = {str(issue.get("span") or "") for issue in issues}
    except Exception:  # noqa: BLE001 同上
        pass
    return bare_dates, bare_names


def _judge_claim(
    claim: dict[str, Any],
    context: _Context,
    *,
    items: list[dict[str, Any]],
    bare_dates: set[str],
    bare_names: set[str],
) -> None:
    kind = claim["kind"]
    literal = str(claim["text"])
    if kind in ("date", "relative_date"):
        target = date.fromisoformat(claim["value"]) if claim.get("value") else None
        supported = context.supports_date(
            target, literal, year_known=bool(claim.get("year_known"))
        )
        claim["supported"] = supported
        if supported:
            claim["reason"] = "date_in_context"
            claim["evidence"] = _evidence(context.matching_item(items, literal))
        elif literal in bare_dates:
            claim["reason"] = "date_fabricated"
            claim["fix"] = _FIX_TEXT["date"].format(span=literal)
        else:
            claim["reason"] = "date_not_in_context"
            claim["fix"] = _FIX_TEXT["date"].format(span=literal)
    elif kind == "name":
        supported = context.supports_name(literal)
        # 右边界回退（机制，不特判具体词）：「叶楠生日」被候选成「叶楠生」时，
        # 若上下文里有更短的前缀名字，就按能对上的最长前缀判支撑并同步 span。
        if not supported and len(literal) > 2:
            for cut in range(len(literal) - 1, 1, -1):
                if context.supports_name(literal[:cut]):
                    literal = literal[:cut]
                    claim["text"] = literal
                    claim["span"][1] = int(claim["span"][0]) + cut
                    supported = True
                    break
        claim["supported"] = supported
        if supported:
            claim["reason"] = "name_in_context"
            claim["evidence"] = _evidence(context.matching_item(items, literal))
        elif literal in bare_names:
            claim["reason"] = "name_fabricated"
            claim["fix"] = _FIX_TEXT["name"].format(span=literal)
        else:
            claim["reason"] = "name_not_in_context"
            claim["fix"] = _FIX_TEXT["name"].format(span=literal)
    elif kind == "number":
        supported = context.supports_number(
            literal, float(claim["value"]) if claim.get("value") is not None else None
        )
        claim["supported"] = supported
        if supported:
            claim["reason"] = "number_in_context"
            claim["evidence"] = _evidence(context.matching_item(items, literal))
        else:
            claim["reason"] = "number_not_in_context"
            claim["fix"] = _FIX_TEXT["number"].format(span=literal)
    else:  # relation
        predicate, _, surface = str(claim.get("value") or "|").partition("|")
        supported = context.supports_relation(predicate, surface)
        claim["supported"] = supported
        if supported:
            claim["reason"] = "relation_in_context"
            claim["evidence"] = _evidence(context.matching_item(items, surface))
        else:
            claim["reason"] = "relation_not_in_context"
            claim["fix"] = _FIX_TEXT["relation"].format(span=literal)


def _strip_unsupported(
    text: str, claims: list[dict[str, Any]], *, scored_kinds: set[str]
) -> tuple[str | None, list[dict[str, Any]]]:
    """保守版 strip：**只删 ``answer.verify.kinds`` 里的类型**（默认 date/relative_date），
    且只删「整句有型断言全是无支撑的计分类型」的句子。

    为什么只删日期类：H1 离线预演（grader-dev，2026-11-07）20 条 strip 样例里，
    日期类 19/93 召回、误伤 3/592（0.5%）；数字类误伤 13 条里 10 条来自数字、专名
    抽取器有 8.8% 误伤（「行李箱→李箱」）——所以数字/专名只 flag/hint，不进 strip。
    有 DB 可核时只删「库里也没有」的日期（``date_fabricated``）：库里有、只是这次没
    注入，是检索漏召，删了会误伤正确答案。
    """
    sentences = _sentence_spans(text)
    removed: list[dict[str, Any]] = []
    drop: set[int] = set()

    def _strippable(claim: dict[str, Any]) -> bool:
        if claim.get("kind") not in scored_kinds or claim.get("supported") is not False:
            return False
        if claim.get("db_checked"):
            return str(claim.get("reason") or "") == "date_fabricated"
        return True

    for index, (s_start, s_end) in enumerate(sentences):
        sentence_text = text[s_start:s_end]
        if _CITATION_RE.search(sentence_text):
            continue  # 删了会破坏引用编号 → 保守跳过
        typed = [c for c in claims if s_start <= int(c["span"][0]) < s_end]
        if not typed:
            continue
        bad_scored = any(_strippable(c) for c in typed)
        any_supported = any(c.get("supported") is True for c in typed)
        # 同句的非计分类型无支撑断言不阻止删除（它本来也只是报告项），
        # 但句子里有任何已支撑的断言就不动。
        if bad_scored and not any_supported:
            drop.add(index)
            removed.append({"span": [s_start, s_end], "text": sentence_text.strip()})
    if not drop:
        return None, removed
    kept = "".join(
        text[s_start:s_end] for index, (s_start, s_end) in enumerate(sentences) if index not in drop
    )
    stripped = kept.strip()
    return (stripped or None), removed


def verify_answer(
    store: Any,
    answer: str,
    items: list[dict[str, Any]] | dict[str, Any] | None = None,
    *,
    query: str = "",
) -> dict[str, Any]:
    """答案保真检查。``enabled=false``（默认）时返回 ``{"enabled": False}``，零副作用。

    ``items``：这次注入的证据条目（``memory_search`` 的 items，或 ``{"items": [...]}``）。
    给 ``None`` 表示「没有上下文可核」——此时只列有型断言、不做支撑判定（不误伤）；
    给空列表表示「上下文确实为空」，有型断言都会被判无支撑。
    """
    cfg = _cfg_dict(store)
    if not bool(cfg.get("enabled", False)):
        return {"enabled": False}

    text = str(answer or "")
    max_claims = selfcheck_mod._cfg_int(cfg, "answer.verify.max_claims", 40)
    min_claims = selfcheck_mod._cfg_int(cfg, "answer.verify.min_claims", 2)
    threshold = float(cfg.get("risk_threshold", 0.34) or 0.0)
    # 计分类型（P0-A 0.40.0 补）：flag 报告所有类型，但**risk 与 strip 只算 kinds**。
    # 默认 date/relative_date（H1 实测：日期 21.5%/0.5%，数字 23.7%/2.2%，专名 43%/8.8%）。
    raw_kinds = cfg.get("kinds")
    if isinstance(raw_kinds, (list, tuple)):
        scored_kinds = {str(k).strip() for k in raw_kinds if str(k).strip()}
    else:
        scored_kinds = set(_DEFAULT_KINDS)
    mode = str(cfg.get("mode", "flag") or "flag").strip().lower()
    if mode not in ("flag", "hint", "strip"):
        mode = "flag"
    # 专名候选证据分阈值（task-34，可选；Lead 还没加配置键时用 selfcheck 默认值）。
    raw_min_score = cfg.get("name_min_score")
    name_min_score: float | None
    try:
        name_min_score = float(raw_min_score) if raw_min_score is not None else None
    except (TypeError, ValueError):
        name_min_score = None
    today = date.fromisoformat(clock.day_key())
    note_parts: list[str] = []
    if len(text) > 4000:
        text = text[:4000]
        note_parts.append("答案超过 4000 字，已截断")
    if not text.strip():
        return {
            "enabled": True,
            "mode": mode,
            "kinds": sorted(scored_kinds),
            "risk": 0.0,
            "threshold": threshold,
            "flagged": False,
            "typed_claims": 0,
            "scored_claims": 0,
            "unsupported": 0,
            "reported_unsupported": 0,
            "claims": [],
            "note": "answer 为空",
        }

    if isinstance(items, dict):
        items = items.get("items")
    if items is not None:
        items = [item for item in items if isinstance(item, dict)]
        if len(items) > 60:  # 上下文再多也不会全看，截断保持确定性
            items = items[:60]
    context = _Context(items, today=today) if items is not None else None

    claims: list[dict[str, Any]] = []
    for _sentence_index, start, end, phrase in _phrases(text):
        for claim in _claims_for_phrase(
            phrase, base=start, today=today, max_claims=max_claims, name_min_score=name_min_score
        ):
            if len(claims) >= max_claims:
                note_parts.append(f"有型断言超过 {max_claims} 条，只核了前 {max_claims} 条")
                break
            claims.append(claim)
        if len(claims) >= max_claims:
            break

    if context is None:
        for claim in claims:
            claim["supported"] = None
            claim["reason"] = "no_context"
        note_parts.append("没有注入 items，跳过支撑判定（不判罚）")
    else:
        bare_dates: set[str] = set()
        bare_names: set[str] = set()
        has_db = getattr(getattr(store, "db", None), "conn", None) is not None
        if any(c["kind"] in ("date", "relative_date") for c in claims) or any(
            c["kind"] == "name" for c in claims
        ):
            # DB 复核只用来分类（没进上下文 vs 库里也没有），不改变「无支撑」的判定。
            bare_dates, bare_names = _db_classification(
                store, text, today=today, max_claims=max_claims, name_min_score=name_min_score
            )
        for claim in claims:
            _judge_claim(claim, context, items=items or [], bare_dates=bare_dates, bare_names=bare_names)
            claim["db_checked"] = bool(has_db)
        # 相对日期分类：**永不判 fabricated**。相对说法依赖各行的逻辑时间，换算基准一错
        # 就会把 required 当编造删掉（task-36：宁可漏报不可误报）。库/上下文都推不出时
        # 只标 relative_date_not_in_context，由 strip 的 fabricated-only 守卫保护。
        if has_db:
            for claim in claims:
                if claim["kind"] == "relative_date" and claim.get("supported") is False:
                    claim["reason"] = "relative_date_not_in_context"

    typed = len(claims)
    scored_claims = sum(1 for c in claims if c["kind"] in scored_kinds)
    reported_unsupported = sum(1 for c in claims if c.get("supported") is False)
    unsupported = sum(
        1 for c in claims if c.get("supported") is False and c["kind"] in scored_kinds
    )
    risk = round(unsupported / max(min_claims, scored_claims), 4) if scored_claims else 0.0
    result: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "query": str(query or ""),
        "kinds": sorted(scored_kinds),
        "typed_claims": typed,
        "scored_claims": scored_claims,
        "unsupported": unsupported,
        "reported_unsupported": reported_unsupported,
        "risk": risk,
        "threshold": threshold,
        "flagged": bool(unsupported) and risk >= threshold,
        "claims": claims,
    }
    if reported_unsupported > unsupported:
        note_parts.append(
            f"另有 {reported_unsupported - unsupported} 条非计分类型（{','.join(sorted({c['kind'] for c in claims if c.get('supported') is False and c['kind'] not in scored_kinds}))}）"
            "无支撑，只报告不计风险"
        )
    if mode == "hint":
        hints = [c["fix"] for c in claims if c.get("supported") is False and c.get("fix")]
        result["hints"] = hints
        result["hint"] = "；".join(dict.fromkeys(hints)) if hints else ""
    if mode == "strip":
        stripped, removed = _strip_unsupported(text, claims, scored_kinds=scored_kinds)
        result["removed"] = removed
        result["stripped"] = stripped
        if stripped is None:
            note_parts.append("保守模式下没有可整句删除的段落（或删了会破坏引用编号）")
    result["note"] = "；".join(note_parts)
    return result


def format_report(result: dict[str, Any]) -> str:
    """把 verify 结果渲染成中文多行（CLI/MCP/控制台共用）。"""
    if not result.get("enabled"):
        return "答案保真检查未启用（answer.verify.enabled=false）。"
    mode = str(result.get("mode") or "flag")
    kinds = [str(k) for k in (result.get("kinds") or [])]
    typed = int(result.get("typed_claims") or 0)
    scored = int(result.get("scored_claims") or 0)
    unsupported = int(result.get("unsupported") or 0)
    reported = int(result.get("reported_unsupported") or unsupported)
    risk = float(result.get("risk") or 0.0)
    threshold = float(result.get("threshold") or 0.0)
    lines = [
        f"保真检查：{typed} 条有型断言（计分 {scored} 条，类型 {'/'.join(kinds) or '无'}），"
        f"{unsupported} 条无支撑；risk {risk:.2f}（阈值 {threshold:.2f}，"
        f"{'触发' if result.get('flagged') else '未触发'}）。"
    ]
    if reported > unsupported:
        lines.append(f"另有 {reported - unsupported} 条非计分类型的无支撑断言，只报告不计风险。")
    for claim in result.get("claims") or []:
        if claim.get("supported") is not False:
            continue
        span = claim.get("text") or ""
        reason = str(claim.get("reason") or "")
        mark = "" if claim.get("kind") in kinds else "（不计分）"
        lines.append(f"- [{claim.get('kind')}]{mark} {span}：{reason}")
        if claim.get("fix"):
            lines.append(f"    最小修法：{claim['fix']}")
    if mode == "hint" and result.get("hint"):
        lines.append(f"宿主提示：{result['hint']}")
    if mode == "strip":
        if result.get("stripped"):
            lines.append("保守删减后答案：")
            lines.append(str(result["stripped"]))
        else:
            lines.append("保守删减：无（未改答案）")
    if result.get("note"):
        lines.append(str(result["note"]))
    return "\n".join(lines)
