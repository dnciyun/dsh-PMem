"""实体识别、解析、别名与合并（M3 / 03 §8.1）。

分工：

- **识别**：T0 规则（词表 + 结构模板 + 后缀），产出带 span 的 mention；
- **解析**：同名同类型合并到一个 ``entity``；上下文冲突时**拆成不同身份**
  （``name_norm`` 加 ``#<上下文指纹>`` 后缀），避免"所有小王都是同一个人"；
- **别名**：``X（也叫 Y）`` 这类显式说法进 ``entity_alias``，查询走别名表；
- **合并**：人工确认后把两个实体并成一个，migration 可回滚（``status='merged'``）。

为什么不直接上向量聚类：同名不同人的判定需要**可解释的证据**（同现的组织/地点），
而 T0 上下文集合正好是这种证据；向量只会在"像"上打转，给不出为什么。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Iterable

from ..base import events
from ..base.clock import now_iso
from ..base.ids import ulid
from ..memory.text import normalize
from . import lexicon

_STRONG_VERBS = (
    "说|问|告诉|让|觉得|认为|喜欢|不喜欢|不太喜欢|讨厌|住在|搬去|搬到|定居"
)
# 2 字名与 3 字名分开匹配：合并成 {2,3} 时正则会先吃 3 个字，
# 把"王然住在"抽成"王然住"、"陈默不喜欢"抽成"陈默不"（实测踩过）。
_PERSON3 = re.compile(rf"(?P<name>[\u4e00-\u9fff]{{3}})(?=(?:{_STRONG_VERBS}))")
_PERSON2 = re.compile(rf"(?P<name>[\u4e00-\u9fff]{{2}})(?=(?:{_STRONG_VERBS}))")
# "X 在 Y 工作"必须从句首开始，否则自由聊天里"频去别人评论区"全会被当人名
_PERSON_WORKS = re.compile(
    r"(?:^|(?<=[，。；！？,.;!?\n]))"
    r"(?P<name>[\u4e00-\u9fff]{2,3})"
    r"(?=(?:在|于)[^，。；！？,.;!?\n]{1,16}(?:工作|上班|任职))"
)
_NAME_HEAD_BLACKLIST = set("说问让给用做去来想要能会信看听回发写把被从对向")
_VERB_CHARS = set("住在不也正已都还就才更最很太说问让觉得认喜讨搬定用做写研我你他她它")
_PERSON_TITLE = re.compile(
    r"(?:^|(?<=[，。；！？,.;!?\n]))(?P<name>[\u4e00-\u9fff]{1,2}(?:总|哥|姐|老师|先生|女士))"
)
_PERSON_AT = re.compile(r"@(?P<name>[A-Za-z0-9_\-]{2,20})")
_ORG_SUFFIX = re.compile(
    r"(?P<name>[\u4e00-\u9fff]{2,8}(?:公司|集团|科技|工作室|银行|大学|学院|医院|实验室))"
)
_PLACE_SUFFIX = re.compile(r"(?P<name>[\u4e00-\u9fff]{2,4}(?:省|市|县|区|镇|村))")
_FUNCTION_CHARS = set("在的了是在去来就要能会想我你他她它这那")

# ---------------------------------------------------------------- 实体桥用的补充规则
# 为什么需要补充：记忆正文是第三人称转述（「用户家访发现王子轩爸妈……」/「用户班的
# 王子轩总不交作业」），只靠「人名 + 强谓词」模板抽不到这些名字，实体表里就只剩
# 词表命中的地点/爱好。检索侧要把「问句里提到的那个已知实体」和散落在别条记忆里
# 的事实连起来，必须先让这些名字有 mention。
#
# 两个来源，都守「别从任意双字造名字」这条线：
#   1. 关系称呼：封闭集合，单用户库里指代稳定（妈妈/奶奶/病人/达人……）；
#   2. 人名候选：中文姓氏或小/老/阿/大 前缀开头，且后面紧跟亲属词/代词/主语副词，
#      接住「王子轩爸妈」「王子轩又没交作业」这种句式。
_ROLE_TERMS: tuple[str, ...] = (
    "妈妈", "爸爸", "奶奶", "爷爷", "姥姥", "姥爷", "外婆", "外公",
    "儿子", "女儿", "媳妇", "老婆", "老公", "孩子", "室友", "闺蜜", "朋友",
    "同事", "组长", "导师", "老板", "房东", "病人", "客户", "甲方", "编辑",
    "师傅", "医生", "护士长", "达人",
)
_NICK_PREFIX = ("小", "老", "阿", "大")
_SURNAMES = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
_NICKNAME = re.compile(r"(?P<name>老[一二三四五六七八九十]{1,2})")
_PERSON_KIN = re.compile(
    r"(?P<name>[\u4e00-\u9fff]{2,3})(?=(?:爸妈|爸爸|妈妈|父母|奶奶|爷爷|姥姥|姥爷|外婆|外公|儿子|女儿|媳妇|老婆|老公|他|她|家))"
)
_PERSON_ACT = re.compile(
    r"(?P<name>[\u4e00-\u9fff]{2,3})(?=(?:总|又|也|还|就|才|不|没|把|被|跟|和|在|来|去|回|说|问|让|笑|哭|骂|答|听|看))"
)
_BRIDGE_STOP = frozenset(
    (
        "老是 总是 还是 就是 但是 于是 不是 也好 也都 还有 就会 才说 把用 被他 跟他 和他 "
        "在说 来看 去看 回说 说说 问问 让说 笑笑 哭说 骂说 答说 听说 看说 的话 一直 开始 "
        "然后 其实 已经 自己 现在 今天 昨天 明天 上午 下午 晚上 中午 时候 一个 什么 怎么 "
        "因为 所以 可以 知道 觉得 记得 一样 有点 有些 真的 居然 反而 干脆 直接 当然 可能 "
        "应该 需要 起来 出来 回来 下来 上来 过来 进去 出去 回去 用户 我们 你们 他们 人家 "
        "大家 别人 家里 公司 学校 单位 东西 事情 地方 朋友 同事 同学 老师 老板 领导"
    ).split()
)
_BRIDGE_BAD_TAIL = set(
    "的了着过吗呢吧啊哦呀嘛是在有和与跟把被让给对为于从向到说问想要会能个之其们多都也才就"
)
_BRIDGE_BAD_HEAD = set(
    "的了着过吗呢吧啊哦呀嘛是在有和与跟把被让给对为于从向到说问想要会能个之其们多都也才就但可又还就都没不很户"
)


def _bridge_person_ok(name: str) -> bool:
    """实体桥的人名候选过滤：宁缺毋滥，只收姓氏/昵称前缀开头、不像虚词的字串。"""
    if not 2 <= len(name) <= 3 or any(c.isdigit() for c in name):
        return False
    if name in _BRIDGE_STOP or name in _ROLE_TERMS:
        return False
    if name[0] in _BRIDGE_BAD_HEAD and name[0] not in _NICK_PREFIX:
        return False
    if name[-1] in _BRIDGE_BAD_TAIL or name[-1] in _VERB_CHARS:
        return False
    if name[0] in _NICK_PREFIX:
        return True
    return name[0] in _SURNAMES

_ALIAS_CONNECTOR = r"(?:也叫|又名|昵称是|别名是|别名|简称|全称|英文叫|英文名|中文叫|中文名)"
_ALIAS_PAREN = re.compile(
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9_]{2,12})\s*[（(]\s*(?:" + _ALIAS_CONNECTOR + r")?\s*"
    r"(?P<alias>[\u4e00-\u9fffA-Za-z0-9_]{1,12})\s*[)）]"
)
_ALIAS_VERB = re.compile(
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9_]{2,12})\s*" + _ALIAS_CONNECTOR + r"\s*"
    r"(?P<alias>[\u4e00-\u9fffA-Za-z0-9_]{1,12})"
)

# 上下文槽：只有**单值**谓词才配当身份证据。
# "喜欢 A"和"喜欢 B"可以同时成立，拿它拆身份会把一个人拆成两个。
_CONTEXT_RULES = (
    ("works_at", re.compile(r"(?:在|于)([^，。；！？,.;!?\n]{1,16}?)(?:工作|上班|任职)"), "org"),
    ("studies_at", re.compile(r"(?:在|于)([^，。；！？,.;!?\n]{1,16}?)(?:上学|读书|读研|读博|毕业)"), "org"),
    ("lives_in", re.compile(r"(?:住在|搬到|定居在?|安居在?)([^，。；！？,.;!?\n]{1,16})"), "place"),
)

_PRIORITY = {
    "lexicon": 0,
    "person": 1,
    "person_kin": 1,
    "person_act": 1,
    "role": 1,
    "nickname": 1,
    "org": 2,
    "place": 2,
    "alias": 3,
}


def norm_name(name: str) -> str:
    """实体名归一：NFKC、去空白、去包裹标点、英文小写。"""
    t = normalize(name).strip(" \t，,。.；;：:、!！?？()（）[]【】<>《》\"'“”‘’")
    return t.lower()


def _hash8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------- 识别
def _dict_candidates(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for term in lexicon.all_terms():
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            start = idx + len(term)
            if term.isascii():
                before = text[idx - 1] if idx > 0 else ""
                after = text[idx + len(term)] if idx + len(term) < len(text) else ""
                if (before and (before.isalnum() or before == "_")) or (
                    after and (after.isalnum() or after == "_")
                ):
                    continue  # "AI" 不能从 "said" 里命中
            out.append(
                {
                    "surface": term,
                    "start": idx,
                    "end": idx + len(term),
                    "type": lexicon.type_of(term) or "concept",
                    "confidence": 0.9,
                    "extractor": "lexicon",
                }
            )
    return out


def _pattern_candidates(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pattern, extractor in (
        (_PERSON3, "person_template"),
        (_PERSON2, "person_template"),
        (_PERSON_WORKS, "person_works"),
    ):
        for match in pattern.finditer(text):
            name = match.group("name")
            if _person_ok(name):
                out.append(
                    {
                        "surface": name,
                        "start": match.start("name"),
                        "end": match.end("name"),
                        "type": "person",
                        "confidence": 0.65 if extractor == "person_works" else 0.55,
                        "extractor": extractor,
                    }
                )
    for match in _PERSON_TITLE.finditer(text):
        name = match.group("name")
        if _person_ok(name):
            out.append(
                {
                    "surface": name,
                    "start": match.start("name"),
                    "end": match.end("name"),
                    "type": "person",
                    "confidence": 0.7,
                    "extractor": "person_title",
                }
            )
    for match in _PERSON_AT.finditer(text):
        out.append(
            {
                "surface": match.group("name"),
                "start": match.start(),
                "end": match.end(),
                "type": "person",
                "confidence": 0.65,
                "extractor": "at_mention",
            }
        )
    for match in _ORG_SUFFIX.finditer(text):
        name = match.group("name")
        if _person_ok_prefix(name):
            out.append(
                {
                    "surface": name,
                    "start": match.start(),
                    "end": match.end(),
                    "type": "org",
                    "confidence": 0.6,
                    "extractor": "org_suffix",
                }
            )
    for match in _PLACE_SUFFIX.finditer(text):
        name = match.group("name")
        if _person_ok_prefix(name):
            out.append(
                {
                    "surface": name,
                    "start": match.start(),
                    "end": match.end(),
                    "type": "place",
                    "confidence": 0.55,
                    "extractor": "place_suffix",
                }
            )
    for pattern, extractor, confidence in (
        (_PERSON_KIN, "person_kin", 0.5),
        (_PERSON_ACT, "person_act", 0.45),
        (_NICKNAME, "nickname", 0.65),
    ):
        for match in pattern.finditer(text):
            name = match.group("name")
            if _bridge_person_ok(name):
                out.append(
                    {
                        "surface": name,
                        "start": match.start("name"),
                        "end": match.end("name"),
                        "type": "person",
                        "confidence": confidence,
                        "extractor": extractor,
                    }
                )
    return out


def _role_candidates(text: str) -> list[dict[str, Any]]:
    """关系称呼当 person mention：封闭集合，单用户库里指代稳定（妈妈/病人/达人）。"""
    out: list[dict[str, Any]] = []
    for term in _ROLE_TERMS:
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            start = idx + len(term)
            out.append(
                {
                    "surface": term,
                    "start": idx,
                    "end": idx + len(term),
                    "type": "person",
                    "confidence": 0.72,
                    "extractor": "role",
                }
            )
    return out


def _person_ok(name: str) -> bool:
    if len(name) < 2:
        return False
    if name[0] in "我你他她它这那哪每各某":
        return False
    if name in lexicon.PERSON_STOPWORDS:
        return False
    if name in lexicon.TYPE_BY_TERM:
        return False
    if any(c.isdigit() for c in name):
        return False
    if name[0] in _NAME_HEAD_BLACKLIST or name[-1] in "我你他她它":
        return False  # "信我" 这类从动词/代词里切出来的不是人名
    if len(name) == 3 and name[2] in _VERB_CHARS:
        return False  # "王然住" / "陈默不" 这类假名字
    return True


def _person_ok_prefix(name: str) -> bool:
    """组织/地点后缀规则的前缀过滤：去掉后缀后必须是干净的 2–6 字词。"""
    body = re.sub(
        r"(工作室|实验室|公司|集团|科技|银行|大学|学院|医院|省|市|县|区|镇|村)$", "", name
    )
    if len(body) < 2 or len(body) > 6:
        return False
    if body in lexicon.PERSON_STOPWORDS:
        return False
    if any(word in body for word in ("评论", "别人", "视频", "会话", "消息", "私信", "回复")):
        return False
    return not any(c in _FUNCTION_CHARS for c in body)


def _context_slots(text: str) -> set[str]:
    """从一句话里取身份上下文槽，形如 ``works_at:字节``。

    只收词表能定型的功能性宾语：未知宾语不参与消歧，否则"在考虑换工作"
    也会被当成一条工作经历。
    """
    from ..memory.text import _norm_obj

    out: set[str] = set()
    for predicate, pattern, expected in _CONTEXT_RULES:
        for match in pattern.finditer(text):
            value = _norm_obj(match.group(1))
            if value and lexicon.type_of(value) == expected:
                out.add(f"{predicate}:{norm_name(value)}")
    return out


def _slot_map(row: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in _stored_context(row):
        slot, _, value = item.partition(":")
        if slot and value:
            mapping[slot] = value
    return mapping


def extract_mentions(text: str) -> list[dict[str, Any]]:
    """抽取不重叠的 mention，按出现位置排序。

    冲突处理：**长词优先 + 位置优先**。词典命中（``lexicon``）永远压过模板，
    因为词表是人工确认过的类型，而模板只给出"像人名"的证据。
    """
    candidates = _dict_candidates(text) + _pattern_candidates(text) + _role_candidates(text)
    candidates.sort(key=lambda c: (c["start"], -(c["end"] - c["start"]), _PRIORITY.get(c["extractor"], 9)))
    accepted: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for cand in candidates:
        if any(cand["start"] < end and start < cand["end"] for start, end in occupied):
            continue
        accepted.append(cand)
        occupied.append((cand["start"], cand["end"]))
    accepted.sort(key=lambda c: c["start"])
    return accepted


def extract_aliases(text: str) -> list[tuple[str, str]]:
    """抽取显式别名：``张伟（小伟）`` / ``张伟也叫小伟`` / ``小米，英文名 Mira``。"""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in (_ALIAS_PAREN, _ALIAS_VERB):
        for match in pattern.finditer(text):
            name = match.group("name").strip()
            alias = re.sub(
                r"^(?:英文叫|英文名|中文叫|中文名|简称|全称)\s*", "", match.group("alias").strip()
            )
            if not name or not alias:
                continue
            # "用户在做可塑性记忆(PMemories)" 是句子，不是名字
            if any(c in name for c in "在的了是我你他她它做说叫"):
                continue
            key = (norm_name(name), norm_name(alias))
            if not key[0] or not key[1] or key in seen or key[0] == key[1]:
                continue
            seen.add(key)
            pairs.append((name, alias))
    return pairs


# ---------------------------------------------------------------- 存储
def _extra(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["extra_json"])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _stored_context(row: Any) -> set[str]:
    return set(_extra(row).get("disambig") or [])


def _candidates(db: Any, subject: str, base: str) -> list[Any]:
    like = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "#%"
    rows = db.conn.execute(
        "SELECT e.*, 0 AS via_alias FROM entity e "
        "WHERE e.subject_id=? AND e.status='active' "
        "AND (e.name_norm=? OR e.name_norm LIKE ? ESCAPE '\\') "
        "UNION ALL "
        "SELECT e.*, 1 AS via_alias FROM entity e "
        "JOIN entity_alias a ON a.entity_id=e.id "
        "WHERE e.subject_id=? AND e.status='active' AND a.alias_norm=?",
        (subject, base, like, subject, base),
    ).fetchall()
    return list(rows)


def _create(
    db: Any,
    *,
    subject: str,
    name: str,
    base: str,
    type_hint: str | None,
    context: set[str],
    confidence: float,
    provenance: str,
    disambiguated: bool,
) -> Any:
    name_norm = base
    if disambiguated and context:
        name_norm = f"{base}#{_hash8('|'.join(sorted(context)))}"
    entity_id = ulid()
    ts = now_iso()
    extra = {"disambig": sorted(context)} if context else {}
    db.conn.execute(
        "INSERT INTO entity(id, subject_id, type, name, name_norm, status, confidence, provenance, "
        "first_seen, last_seen, mention_count, extra_json) VALUES(?,?,?,?,?, 'active',?,?,?,?,0,?)",
        (
            entity_id,
            subject,
            type_hint or "concept",
            name.strip(),
            name_norm,
            max(0.0, min(1.0, confidence)),
            provenance,
            ts,
            ts,
            json.dumps(extra, ensure_ascii=False, sort_keys=True),
        ),
    )
    return db.conn.execute("SELECT * FROM entity WHERE id=?", (entity_id,)).fetchone()


def ensure_self(db: Any, subject: str = "self") -> Any:
    """取得"我"这个实体；没有就建。关系抽取的 src 绝大多数是它。"""
    rows = _candidates(db, subject, "我")
    for row in rows:
        if row["name_norm"] == "我":
            return row
    return _create(
        db,
        subject=subject,
        name="我",
        base="我",
        type_hint="person",
        context=set(),
        confidence=1.0,
        provenance="user_explicit",
        disambiguated=False,
    )


def resolve(
    db: Any,
    *,
    subject: str,
    name: str,
    type_hint: str | None = None,
    context: Iterable[str] = (),
    create: bool = True,
    confidence: float = 0.6,
    provenance: str = "inferred",
) -> dict[str, Any]:
    """把名字解析到一个实体。

    返回 ``{entity, ambiguous, candidates, created}``：
    ``ambiguous=True`` 时 ``entity`` 是得分最高者，调用方**不应**直接写边，
    应要求调用者消歧（04 §5.6 的 ``E_CONFLICT``）。
    """
    base = norm_name(name)
    if not base:
        raise ValueError("实体名为空")
    rows = _candidates(db, subject, base)
    ctx = {c for c in context if c}

    if type_hint:
        typed = [r for r in rows if r["type"] == type_hint]
        if typed:
            rows = typed

    if not rows:
        if not create:
            return {"entity": None, "ambiguous": False, "candidates": [], "created": False}
        row = _create(
            db,
            subject=subject,
            name=name,
            base=base,
            type_hint=type_hint,
            context=ctx,
            confidence=confidence,
            provenance=provenance,
            disambiguated=False,
        )
        return {"entity": row, "ambiguous": False, "candidates": [], "created": True}

    # 有上下文：同槽同值是同一身份；同槽不同值才是另一个人
    if ctx:
        ctx_pairs: dict[str, str] = {}
        for item in ctx:
            slot, _, value = item.partition(":")
            if slot and value:
                ctx_pairs[slot] = value
        mapped = [(r, _slot_map(r)) for r in rows]
        same = [r for r, slots in mapped if any(slots.get(slot) == value for slot, value in ctx_pairs.items())]
        conflict = [r for r, slots in mapped if any(slot in slots and slots[slot] != value for slot, value in ctx_pairs.items())]
        if len(same) == 1:
            row = same[0]
            _adopt_context(db, row, ctx)
            return {"entity": row, "ambiguous": False, "candidates": rows, "created": False}
        if len(same) > 1:
            return {
                "entity": same[0],
                "ambiguous": True,
                "candidates": same,
                "created": False,
            }
        if conflict and all(slots for _, slots in mapped):
            row = _create(
                db,
                subject=subject,
                name=name,
                base=base,
                type_hint=type_hint or rows[0]["type"],
                context=ctx,
                confidence=confidence,
                provenance=provenance,
                disambiguated=True,
            )
            return {"entity": row, "ambiguous": False, "candidates": rows, "created": True}
        # 旧实体没有冲突槽：认领上下文（第一份证据），不拆
        row = rows[0]
        _adopt_context(db, row, ctx)
        return {"entity": row, "ambiguous": False, "candidates": rows, "created": False}

    if len(rows) == 1:
        return {"entity": rows[0], "ambiguous": False, "candidates": rows, "created": False}
    with_ctx = [r for r in rows if _stored_context(r)]
    if with_ctx:
        # 多个同名身份、这次又没有上下文 → 不能猜
        return {"entity": None, "ambiguous": True, "candidates": rows, "created": False}
    return {"entity": rows[0], "ambiguous": False, "candidates": rows, "created": False}


def _adopt_context(db: Any, row: Any, ctx: set[str]) -> None:
    stored = _stored_context(row)
    merged = stored | ctx
    if not ctx or merged == stored:
        return
    extra = _extra(row)
    extra["disambig"] = sorted(merged)
    db.conn.execute(
        "UPDATE entity SET extra_json=? WHERE id=?",
        (json.dumps(extra, ensure_ascii=False, sort_keys=True), row["id"]),
    )


def record_mention(
    db: Any,
    *,
    entity_id: str,
    memory_seq: int,
    span_start: int,
    span_end: int,
    surface: str,
    confidence: float,
    extractor: str,
) -> bool:
    """记一次提及；同一 (实体, 记忆, 起点) 只记一次。"""
    exists = db.conn.execute(
        "SELECT 1 FROM entity_mention WHERE entity_id=? AND memory_seq=? AND span_start=?",
        (entity_id, memory_seq, span_start),
    ).fetchone()
    if exists:
        return False
    db.conn.execute(
        "INSERT INTO entity_mention(entity_id, memory_seq, span_start, span_end, surface, confidence, extractor) "
        "VALUES(?,?,?,?,?,?,?)",
        (entity_id, memory_seq, span_start, span_end, surface, confidence, extractor),
    )
    ts = now_iso()
    db.conn.execute(
        "UPDATE entity SET mention_count=mention_count+1, last_seen=?, first_seen=MIN(first_seen, ?) WHERE id=?",
        (ts, ts, entity_id),
    )
    return True


def add_alias(db: Any, entity_id: str, alias: str, *, source: str = "template", confidence: float = 0.8) -> bool:
    alias_norm = norm_name(alias)
    if not alias_norm:
        return False
    exists = db.conn.execute(
        "SELECT 1 FROM entity_alias WHERE entity_id=? AND alias_norm=?", (entity_id, alias_norm)
    ).fetchone()
    if exists:
        return False
    db.conn.execute(
        "INSERT INTO entity_alias(id, entity_id, alias, alias_norm, lang, source, confidence, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (ulid(), entity_id, alias, alias_norm, None, source, confidence, now_iso()),
    )
    return True


def find(db: Any, *, subject: str, name: str) -> list[Any]:
    base = norm_name(name)
    return _candidates(db, subject, base) if base else []


def merge(db: Any, keep_id: str, drop_id: str, *, actor: str = "system:net") -> dict[str, Any]:
    """把 ``drop`` 并入 ``keep``：别名、提及、关系全部改挂，旧实体留墓碑。"""
    if keep_id == drop_id:
        raise ValueError("不能合并到自身")
    keep = db.conn.execute("SELECT * FROM entity WHERE id=? AND status='active'", (keep_id,)).fetchone()
    drop = db.conn.execute("SELECT * FROM entity WHERE id=? AND status='active'", (drop_id,)).fetchone()
    if keep is None or drop is None:
        raise KeyError("实体不存在或已合并")

    moved_alias = 0
    for row in db.conn.execute("SELECT * FROM entity_alias WHERE entity_id=?", (drop_id,)).fetchall():
        if add_alias(db, keep_id, row["alias"], source=row["source"], confidence=row["confidence"]):
            moved_alias += 1
    # 被合并方自己的名字/归一化名也作为别名保留
    add_alias(db, keep_id, drop["name"], source="merge", confidence=0.9)
    add_alias(db, keep_id, drop["name_norm"], source="merge", confidence=0.9)

    moved_mention = 0
    for row in db.conn.execute("SELECT * FROM entity_mention WHERE entity_id=?", (drop_id,)).fetchall():
        dup = db.conn.execute(
            "SELECT 1 FROM entity_mention WHERE entity_id=? AND memory_seq=? AND span_start=?",
            (keep_id, row["memory_seq"], row["span_start"]),
        ).fetchone()
        if dup:
            db.conn.execute("DELETE FROM entity_mention WHERE seq=?", (row["seq"],))
            continue
        db.conn.execute("UPDATE entity_mention SET entity_id=? WHERE seq=?", (keep_id, row["seq"]))
        moved_mention += 1

    # 分组/等级也改挂到保留实体；两边都有等级时留更强的那个
    for member in db.conn.execute(
        "SELECT * FROM grp_member WHERE target_tbl='entity' AND target_id=?", (drop_id,)
    ).fetchall():
        dup = db.conn.execute(
            "SELECT 1 FROM grp_member WHERE grp_id=? AND target_tbl='entity' AND target_id=?",
            (member["grp_id"], keep_id),
        ).fetchone()
        if dup:
            db.conn.execute("DELETE FROM grp_member WHERE id=?", (member["id"],))
        else:
            db.conn.execute("UPDATE grp_member SET target_id=? WHERE id=?", (keep_id, member["id"]))
    keep_grade = db.conn.execute(
        "SELECT grade FROM target_grade WHERE target_tbl='entity' AND target_id=?", (keep_id,)
    ).fetchone()
    drop_grade = db.conn.execute(
        "SELECT * FROM target_grade WHERE target_tbl='entity' AND target_id=?", (drop_id,)
    ).fetchone()
    if drop_grade is not None:
        order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
        if keep_grade is None or order.get(drop_grade["grade"], 9) < order.get(keep_grade["grade"], 9):
            db.conn.execute(
                "INSERT INTO target_grade(target_tbl, target_id, grade, rationale, evidence_json, "
                "assigned_by, created_at, updated_at) VALUES('entity',?,?,?,?,?,?,?) "
                "ON CONFLICT(target_tbl, target_id) DO UPDATE SET grade=excluded.grade, "
                "rationale=excluded.rationale, evidence_json=excluded.evidence_json, "
                "assigned_by=excluded.assigned_by, updated_at=excluded.updated_at",
                (
                    keep_id,
                    drop_grade["grade"],
                    drop_grade["rationale"],
                    drop_grade["evidence_json"],
                    drop_grade["assigned_by"],
                    drop_grade["created_at"],
                    now_iso(),
                ),
            )
        db.conn.execute(
            "DELETE FROM target_grade WHERE target_tbl='entity' AND target_id=?", (drop_id,)
        )

    moved_rel = 0
    for row in db.conn.execute(
        "SELECT * FROM relation WHERE src_id=? OR dst_id=?", (drop_id, drop_id)
    ).fetchall():
        src_id = keep_id if row["src_id"] == drop_id else row["src_id"]
        dst_id = keep_id if row["dst_id"] == drop_id else row["dst_id"]
        if src_id == dst_id:
            db.conn.execute("DELETE FROM relation WHERE id=?", (row["id"],))
            continue
        dup = db.conn.execute(
            "SELECT * FROM relation WHERE src_id=? AND dst_id=? AND predicate=? "
            "AND status IN ('active','contested')",
            (src_id, dst_id, row["predicate"]),
        ).fetchone()
        if dup and dup["id"] != row["id"]:
            evidence = _merge_evidence(dup["evidence_json"], row["evidence_json"])
            db.conn.execute(
                "UPDATE relation SET weight=?, confidence=?, evidence_json=?, updated_at=? WHERE id=?",
                (
                    max(dup["weight"], row["weight"]),
                    max(dup["confidence"], row["confidence"]),
                    evidence,
                    now_iso(),
                    dup["id"],
                ),
            )
            db.conn.execute("DELETE FROM relation WHERE id=?", (row["id"],))
        else:
            db.conn.execute("UPDATE relation SET src_id=?, dst_id=? WHERE id=?", (src_id, dst_id, row["id"]))
        moved_rel += 1

    db.conn.execute(
        "UPDATE entity SET status='merged', canonical_id=? WHERE id=?", (keep_id, drop_id)
    )
    db.conn.execute(
        "UPDATE entity SET mention_count=(SELECT COUNT(*) FROM entity_mention WHERE entity_id=?), "
        "last_seen=? WHERE id=?",
        (keep_id, now_iso(), keep_id),
    )
    # 合并会改挂别名/提及/关系/分组/等级，是事实层的结构性变更：留一条事件，
    # 否则「这个实体去哪了」在 event_log 里查不到（只有 status='merged'+canonical_id 可猜）。
    with db.tx() as conn:
        events.append(
            conn,
            actor=actor,
            op="entity.merge",
            target_tbl="entity",
            target_id=keep_id,
            subject_id=keep["subject_id"],
            payload={
                "merged_id": drop_id,
                "merged_name": drop["name"],
                "aliases": moved_alias,
                "mentions": moved_mention,
                "relations": moved_rel,
            },
        )
    return {
        "keep": keep_id,
        "merged": drop_id,
        "aliases": moved_alias,
        "mentions": moved_mention,
        "relations": moved_rel,
        "actor": actor,
    }


def _merge_evidence(left: str, right: str) -> str:
    def _ids(raw: str) -> list[str]:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(data, list):
            return [str(item.get("memory_id")) for item in data if isinstance(item, dict) and item.get("memory_id")]
        return []

    seen: list[str] = []
    for item in _ids(left) + _ids(right):
        if item not in seen:
            seen.append(item)
    return json.dumps([{"memory_id": mid} for mid in seen], ensure_ascii=False)


def ingest(
    db: Any,
    *,
    subject: str,
    memory_seq: int,
    text: str,
    confidence: float = 0.6,
) -> list[dict[str, Any]]:
    """从一条记忆里抽实体并落库，返回可供关系抽取使用的 mention 列表。

    两遍：先抽全部 mention 得到"上下文"（同句出现的组织/地点），再逐个解析。
    这样 ``小王在字节工作`` 里的"小王"才会按"字节"这个上下文归身份。
    """
    text = normalize(text)
    raw = extract_mentions(text)
    context = _context_slots(text)
    resolved: list[dict[str, Any]] = []
    for mention in raw:
        result = resolve(
            db,
            subject=subject,
            name=mention["surface"],
            type_hint=mention["type"],
            context=context,
            confidence=mention["confidence"],
            provenance="inferred",
        )
        row = result["entity"]
        if row is None:
            resolved.append({**mention, "entity_id": None, "ambiguous": True})
            continue
        record_mention(
            db,
            entity_id=row["id"],
            memory_seq=memory_seq,
            span_start=mention["start"],
            span_end=mention["end"],
            surface=mention["surface"],
            confidence=mention["confidence"],
            extractor=mention["extractor"],
        )
        resolved.append(
            {
                **mention,
                "entity_id": row["id"],
                "entity_name": row["name"],
                "entity_type": row["type"],
                "ambiguous": result["ambiguous"],
            }
        )

    for name, alias in extract_aliases(text):
        found = find(db, subject=subject, name=name)
        if not found:
            # 别名句本身可能没有谓词，名字不会被 mention 规则抽到；
            # 显式别名是强证据，直接把主体建出来（2–3 个汉字按人名处理）。
            cjk = all("\u4e00" <= ch <= "\u9fff" for ch in name)
            hint = "person" if cjk and 2 <= len(name) <= 3 else None
            target = resolve(
                db, subject=subject, name=name, type_hint=hint, confidence=0.6
            )["entity"]
        elif len(found) == 1:
            target = found[0]
        else:
            continue
        if target is None:
            continue
        add_alias(db, target["id"], alias, source="template")
        # 别名先于实体出现时，"小舟"可能已经作为独立人物被建过；
        # 显式别名是强证据，同类型就直接并进来，避免同一个人两个节点。
        existing = [r for r in find(db, subject=subject, name=alias) if r["id"] != target["id"]]
        if len(existing) == 1 and existing[0]["type"] == target["type"]:
            merge(db, target["id"], existing[0]["id"], actor="system:alias")
    return resolved


def list_entities(
    db: Any,
    *,
    subject: str = "self",
    type_: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM entity WHERE subject_id=? AND status='active'"
    params: list[Any] = [subject]
    if type_:
        sql += " AND type=?"
        params.append(type_)
    if query:
        sql += " AND (name_norm LIKE ? OR name LIKE ?)"
        like = f"%{query.lower()}%"
        params.extend([like, f"%{query}%"])
    sql += " ORDER BY mention_count DESC, last_seen DESC LIMIT ?"
    params.append(limit)
    out = []
    for row in db.conn.execute(sql, params).fetchall():
        out.append(
            {
                "id": row["id"],
                "type": row["type"],
                "name": row["name"],
                "name_norm": row["name_norm"],
                "confidence": row["confidence"],
                "mention_count": row["mention_count"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "disambig": sorted(_stored_context(row)),
            }
        )
    return out


# ---------------------------------------------------------------- 检索侧的实体桥（M22）
# 检索要回答的是「两条记忆之间没有共同稀有词」的问题：问句里只有一个人名/称呼，
# 答案在另一条记忆里（「不交作业的那个孩子」→ 另一条写「离婚」的记忆）。
# 这里只做纯查询，不写库：
#   1. 从问句认出**库里真实存在的实体**（词表/实体名/别名/关系称呼），不做自由造名；
#   2. 经 ``entity_mention`` 反查记忆 seq（已知实体那一跳）；
#   3. 再从词法种子记忆做**一跳共现实体**扩展（种子里有谁 → 还有哪条记忆提过谁）。
# 名字对不上就整路不触发（返回空），把噪声关在门外。
_ROLE_SELF = re.compile(
    r"我(?:的)?(?P<role>妈|爸|奶奶|爷爷|姥姥|外公|外婆|儿子|女儿|媳妇|老婆|老公|孩子|室友|闺蜜|组长|导师|老板|房东|病人|达人)"
)
_ROLE_CANON = {
    "妈": "妈妈",
    "爸": "爸爸",
}
_VISIBLE = ("active", "contested", "dormant")


def _name_in_query(name_norm: str, q_norm: str) -> bool:
    """实体名是否出现在问句里；顺带过滤实体表里抽歪的短词。"""
    if not 2 <= len(name_norm) <= 8:
        return False
    if name_norm in _ROLE_TERMS:
        return name_norm in q_norm
    if name_norm in _BRIDGE_STOP:
        return False
    if name_norm[0] in _BRIDGE_BAD_HEAD:
        return False
    return name_norm in q_norm


def query_entities(
    conn: Any,
    *,
    subject: str = "self",
    query: str,
    min_mentions: int = 2,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """从问句里认出**已知实体**：实体名/别名直配 + 「我妈/我奶奶」这类关系称呼。

    返回 ``[{id, name, type, mention_count, surface, weight}]``，按证据强度排序。
    """
    q = norm_name(query)
    if not q:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    rows = conn.execute(
        "SELECT id,name,type,name_norm,mention_count FROM entity "
        "WHERE subject_id=? AND status='active' AND mention_count>=? "
        "ORDER BY mention_count DESC LIMIT 400",
        (subject, max(1, int(min_mentions))),
    ).fetchall()
    for row in rows:
        name = str(row["name_norm"] or "")
        if not _name_in_query(name, q) or row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "mention_count": int(row["mention_count"] or 0),
                "surface": name,
                "weight": 1.0,
            }
        )
    # 别名（「X（也叫 Y）」）也算已知实体
    try:
        alias_rows = conn.execute(
            "SELECT e.id,e.name,e.type,e.name_norm,e.mention_count,a.alias_norm "
            "FROM entity_alias a JOIN entity e ON e.id=a.entity_id "
            "WHERE e.subject_id=? AND e.status='active' LIMIT 800",
            (subject,),
        ).fetchall()
    except Exception:  # noqa: BLE001 老库没有别名表也不影响主路
        alias_rows = []
    for row in alias_rows:
        alias = str(row["alias_norm"] or "")
        if row["id"] in seen or not _name_in_query(alias, q):
            continue
        seen.add(row["id"])
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "mention_count": int(row["mention_count"] or 0),
                "surface": alias,
                "weight": 0.9,
            }
        )
    # 关系称呼：问句说「我妈/我奶奶」，库里存的是「妈妈/奶奶」
    for match in _ROLE_SELF.finditer(q):
        role = match.group("role")
        canon = _ROLE_CANON.get(role, role)
        for row in rows:
            if row["id"] in seen or str(row["name_norm"] or "") != canon:
                continue
            seen.add(row["id"])
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["type"],
                    "mention_count": int(row["mention_count"] or 0),
                    "surface": canon,
                    "weight": 0.95,
                }
            )
            break
    out.sort(key=lambda item: (-float(item["weight"]), -int(item["mention_count"])))
    return out[: max(1, int(limit))]


def _visible_seqs(conn: Any, seqs: Iterable[int], *, subject: str = "self") -> list[int]:
    """过滤到当前可见状态的 seq（superseded 的旧记忆不参与桥）。"""
    want = [int(s) for s in seqs]
    if not want:
        return []
    marks = ",".join("?" for _ in want)
    rows = conn.execute(
        f"SELECT seq FROM memory WHERE seq IN ({marks}) AND subject_id=? "
        f"AND status IN ({','.join('?' for _ in _VISIBLE)})",
        (*want, subject, *_VISIBLE),
    ).fetchall()
    visible = {int(r["seq"]) for r in rows}
    return [s for s in want if s in visible]


def _mention_seqs(
    conn: Any,
    entity_ids: Iterable[str],
    *,
    subject: str = "self",
    limit: int = 32,
    exclude: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """实体 → 提及它的记忆 seq，按「命中实体数 / 提及可信度 / 新近」排序。"""
    ids = [str(e) for e in entity_ids]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    params: list[Any] = [*ids, subject, *_VISIBLE]
    sql = (
        f"SELECT em.memory_seq AS seq, COUNT(DISTINCT em.entity_id) AS hits, "
        f"SUM(em.confidence) AS conf, MAX(e.mention_count) AS pop "
        f"FROM entity_mention em JOIN entity e ON e.id=em.entity_id "
        f"JOIN memory m ON m.seq=em.memory_seq "
        f"WHERE em.entity_id IN ({marks}) AND m.subject_id=? "
        f"AND m.status IN ({','.join('?' for _ in _VISIBLE)}) "
    )
    excluded = [int(s) for s in exclude]
    if excluded:
        sql += f" AND em.memory_seq NOT IN ({','.join('?' for _ in excluded)}) "
        params.extend(excluded)
    sql += " GROUP BY em.memory_seq ORDER BY hits DESC, conf DESC, seq DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except Exception:  # noqa: BLE001 老库没有 mention 表 → 这一路自然关掉
        return []


def memory_hits(
    conn: Any,
    *,
    subject: str = "self",
    query: str,
    limit: int = 16,
    seed_seqs: Iterable[int] = (),
    hop: bool = True,
    hop_entities: int = 6,
    hop_min_mentions: int = 2,
    hop_limit: int = 12,
    min_mentions: int = 2,
) -> dict[str, Any]:
    """实体桥纯查询：问句已知实体 → mention 反查；再从词法种子做一跳共现扩展。

    返回 ``{query_entities, query_seqs, hop_entities, hop_seqs, channel, scores}``：
    ``channel`` 是交给 RRF 的有序 seq 列表；``scores`` 是 seq→桥强度（补候选排序用）。
    """
    info: dict[str, Any] = {
        "query_entities": [],
        "query_seqs": [],
        "hop_entities": [],
        "hop_seqs": [],
        "channel": [],
        "scores": {},
    }
    matched = query_entities(
        conn, subject=subject, query=query, min_mentions=min_mentions, limit=8
    )
    info["query_entities"] = [str(m["name"]) for m in matched]
    scores: dict[int, float] = {}
    query_seqs: list[int] = []
    if matched:
        for row in _mention_seqs(
            conn, [m["id"] for m in matched], subject=subject, limit=max(1, int(limit)) * 2
        )[: max(1, int(limit))]:
            seq = int(row["seq"])
            query_seqs.append(seq)
            scores[seq] = round(1.2 + 0.1 * float(row["hits"] or 0), 4)
    hop_seqs: list[int] = []
    if hop and seed_seqs:
        seeds = _visible_seqs(conn, list(seed_seqs)[:8], subject=subject)
        if seeds:
            smarks = ",".join("?" for _ in seeds)
            try:
                spread = conn.execute(
                    f"SELECT e.id,e.name,e.type,e.mention_count,COUNT(*) AS c "
                    f"FROM entity_mention em JOIN entity e ON e.id=em.entity_id "
                    f"WHERE em.memory_seq IN ({smarks}) AND e.status='active' "
                    f"AND e.mention_count>=? "
                    f"AND e.type IN ('person','org','place','tool','concept') "
                    f"GROUP BY e.id ORDER BY c DESC LIMIT 64",
                    (*seeds, max(1, int(hop_min_mentions))),
                ).fetchall()
            except Exception:  # noqa: BLE001
                spread = []
            ranked: list[tuple[float, Any]] = []
            for row in spread:
                name = str(row["name"] or "")
                if row["type"] == "person" and name not in _ROLE_TERMS:
                    if not _bridge_person_ok(name):
                        continue  # 模板抽歪的名字（「没敢多」「户嘴上」）不进桥
                df = max(1, int(row["mention_count"] or 1))
                weight = float(row["c"] or 1) / math.log(2.0 + df)  # 稀有实体的桥更有信息量
                if row["type"] == "person":
                    weight *= 1.3
                ranked.append((weight, row))
            ranked.sort(key=lambda pair: -pair[0])
            chosen = [row for _w, row in ranked[: max(0, int(hop_entities))]]
            info["hop_entities"] = [str(row["name"]) for row in chosen]
            if chosen:
                ids = [str(row["id"]) for row in chosen]
                weight_by_id = {
                    str(row["id"]): weight
                    for weight, row in ranked[: max(0, int(hop_entities))]
                }
                imarks = ",".join("?" for _ in ids)
                params: list[Any] = [*ids, subject, *_VISIBLE, *seeds]
                try:
                    pair_rows = conn.execute(
                        f"SELECT em.memory_seq AS seq, em.entity_id AS eid "
                        f"FROM entity_mention em JOIN memory m ON m.seq=em.memory_seq "
                        f"WHERE em.entity_id IN ({imarks}) AND m.subject_id=? "
                        f"AND m.status IN ({','.join('?' for _ in _VISIBLE)}) "
                        f"AND em.memory_seq NOT IN ({smarks})",
                        params,
                    ).fetchall()
                except Exception:  # noqa: BLE001
                    pair_rows = []
                agg: dict[int, tuple[set[str], float]] = {}
                for row in pair_rows:
                    seq = int(row["seq"])
                    eid = str(row["eid"])
                    shared, total = agg.setdefault(seq, (set(), 0.0))
                    if eid in shared:
                        continue
                    shared.add(eid)
                    agg[seq] = (shared, total + float(weight_by_id.get(eid) or 0.0))
                ranked_seqs = sorted(agg.items(), key=lambda item: -item[1][1])
                for seq, (_shared, total) in ranked_seqs:
                    if seq in query_seqs:
                        continue
                    hop_seqs.append(seq)
                    scores[seq] = round(min(2.0, 0.45 + total), 4)
                    if len(hop_seqs) >= max(1, int(hop_limit)):
                        break
    channel: list[int] = []
    for seq in [*query_seqs, *hop_seqs]:
        if seq not in channel:
            channel.append(seq)
    channel = channel[: max(1, int(limit))]
    info["query_seqs"] = query_seqs
    info["hop_seqs"] = hop_seqs
    info["channel"] = channel
    info["scores"] = {seq: scores[seq] for seq in channel if seq in scores}
    return info
