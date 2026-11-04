"""语文范围（M15 / 1.11.0）：代词、自称、角色与「这是谁的事」。

**要解决的问题**：字面匹配分不清「谁的事」和「什么事」。同样的词，主语不同就是两个事实：

- 问「我是什么工作的」，记忆里「用户的朋友在博远工作」比「用户在云图工作」匹配得还快；
- 问「小明他爸是开什么的」，记忆「小明在念小学二年级」和「小明爸爸开出租车」
  共享的词一样多（都只有「小明」），谁排前面全看运气；
- 问「小雅她是在哪儿上班」，记忆「小雅的男朋友在成都上班」多命中一个「上班」，
  于是把「小雅自己在杭州当老师」压下去。

这些都不是语义相似度问题，是**范围（scope）**问题：代词指向谁、关系词限定谁、
「我」和「我的朋友」不是同一个主语。这层信息在文本里是显式的（她/他/爸/妈/男朋友/朋友），
所以用词法规则做，不用模型：

1. 角色词（爸/妈/哥/姐/朋友/同事…）：问句里出现角色词时，候选里同角色词加一点分——
   「小明**他爸**是开什么的」该落到「小明**爸爸**开出租车」；
2. 他人所属（`用户的妹妹…`）：问句是**问自己**（我/本人/自己/咱）且没有关系词时，
   候选主语是「别人的」就降权——「我是什么工作的」不该答成朋友的；
3. 代词指代本人（`小雅她…`）：问句里「名字+她/他」是「本人」的意思，
   候选里「小雅的男朋友/小雅的老公」是**别人**，降权。

三条都只在词面上判断，能解释、能关、零模型。
"""

from __future__ import annotations

import re
from typing import Any

# 关系/角色词。按「谁的事」分类用，不追求语言完备，覆盖日常对话里的高频角色。
RELATION_WORDS = (
    "爸爸",
    "妈妈",
    "爸",
    "妈",
    "哥哥",
    "姐姐",
    "弟弟",
    "妹妹",
    "哥",
    "姐",
    "弟",
    "妹",
    "儿子",
    "女儿",
    "老婆",
    "老公",
    "媳妇",
    "丈夫",
    "妻子",
    "男朋友",
    "女朋友",
    "男友",
    "女友",
    "男票",
    "女票",
    "朋友",
    "同事",
    "同学",
    "室友",
    "邻居",
    "老板",
    "领导",
    "导师",
    "老师",
    "师傅",
    "医生",
    "护士",
    "客户",
    "亲戚",
    "外婆",
    "外公",
    "爷爷",
    "奶奶",
    "姥姥",
    "姥爷",
    "闺蜜",
    "男朋友",
    # 长尾亲戚/身份：手写表覆盖不到的词由下面的前缀正则兜底
    "姨妈",
    "姑妈",
    "舅妈",
    "婶婶",
    "叔叔",
    "舅舅",
    "嫂子",
    "表哥",
    "表姐",
    "表妹",
    "表弟",
    "堂哥",
    "堂姐",
    "堂妹",
    "堂弟",
    "大伯",
    "小叔",
    "阿姨",
    "婆婆",
    "公公",
    "岳父",
    "岳母",
    "丈母娘",
    "女婿",
    "儿媳",
    "侄子",
    "侄女",
    "外甥",
    "外甥女",
    "干爹",
    "干妈",
    "继母",
    "继父",
    "保姆",
    "司机",
    "店主",
    "房东",
    "租客",
    "老乡",
    "师兄",
    "师姐",
    "师弟",
    "师妹",
    "学长",
    "学姐",
    "校长",
    "上级",
    "下属",
)

# 「小姨 / 二舅 / 大姑 / 表姐 / 三叔」这类带排行或表堂前缀的亲戚：
# 手写表永远追不上，用一条正则兜底（前缀 小大二三四五表堂亲干 + 亲缘字）。
_PREFIX_KIN = re.compile(r"(?:小|大|二|三|四|五|六|七|八|九|表|堂|亲|干)(?:姨|舅|姑|叔|伯|婶|婆|公|哥|姐|妹|弟|嫂|侄|甥)")

# 自称标记：问句在问「我」这边的事。
SELF_MARKERS = ("我", "本人", "自己", "咱", "我们", "俺")

# 「名字 + 她/他」：名字后面跟代词，指的就是本人。
_NAME_PRONOUN = re.compile(r"([\u4e00-\u9fff]{2,3})(?:她|他)(?!们)")

# 候选里「别人的事」：用户的<关系词>
_OTHER_OWNER = re.compile(r"用户(?:的)?(?:朋友|同事|同学|室友|邻居|老板|领导|导师|老师|妹妹|姐姐|弟弟|哥哥|儿子|女儿|老婆|老公|媳妇|丈夫|妻子|男朋友|女朋友|男友|女友|闺蜜|外婆|外公|爷爷|奶奶|亲戚)")


def settings(cfg: Any) -> dict[str, Any]:
    value = cfg.get("retrieval.scope", {}) if cfg is not None else {}
    return value if isinstance(value, dict) else {}


# 角色词归一：爸爸/爸、妈妈/妈 是同一个角色，问句写「他爸」、记忆写「爸爸」也要能对上。
_CANON = {
    "爸爸": "爸",
    "妈妈": "妈",
    "哥哥": "哥",
    "姐姐": "姐",
    "弟弟": "弟",
    "妹妹": "妹",
    "男朋友": "男伴",
    "女友": "女伴",
    "女朋友": "女伴",
    "男友": "男伴",
    "男票": "男伴",
    "女票": "女伴",
    "媳妇": "妻",
    "妻子": "妻",
    "丈夫": "夫",
    "导师": "老师",
    "师傅": "师傅",
    "医生": "医生",
}


def canonical(word: str) -> str:
    return _CANON.get(word, word)


def relation_words(text: str) -> set[str]:
    """文本里出现的角色词（归一到同一个人物角色）。"""
    body = str(text or "")
    found: set[str] = set()
    for word in sorted(RELATION_WORDS, key=len, reverse=True):
        if word in body:
            found.add(canonical(word))
            body = body.replace(word, " ")
    for match in _PREFIX_KIN.finditer(body):
        # 带前缀的亲戚保留原词形（小姨 ≠ 姨，表姐 ≠ 姐），只做「同词」匹配
        found.add(match.group(0))
    return found


def relation_surface(text: str) -> list[str]:
    """文本里出现的角色词**原样**（给小姨/二舅这种长尾词做召回种子用）。"""
    body = str(text or "")
    out: list[str] = []
    for word in sorted(RELATION_WORDS, key=len, reverse=True):
        if len(word) >= 2 and word in body:
            out.append(word)
            body = body.replace(word, " ")
    for match in _PREFIX_KIN.finditer(body):
        out.append(match.group(0))
    return out


def recall_terms(query: str) -> list[str]:
    """问句里可以当召回种子的词：名字（「X她/他」的 X）+ 长尾角色词。"""
    terms: list[str] = []
    for name in named_pronoun_subjects(query):
        if len(name) >= 2 and name not in terms:
            terms.append(name)
    for word in relation_surface(query):
        if len(word) >= 2 and word not in terms:
            terms.append(word)
    return terms


def is_self_query(query: str) -> bool:
    return any(marker in str(query or "") for marker in SELF_MARKERS)


def named_pronoun_subjects(query: str) -> list[str]:
    """「小雅她…」「小明他…」里的名字：指的是本人，不是他的关系人。"""
    return [match.group(1) for match in _NAME_PRONOUN.finditer(str(query or ""))]


def other_owner(text: str) -> bool:
    """候选是不是在说「用户的朋友/用户的妹妹」这种别人的事。"""
    return bool(_OTHER_OWNER.search(str(text or "")))


_RELATION_TAIL = re.compile(
    r"(?:的)(男朋友|女朋友|男友|女友|老公|老婆|媳妇|丈夫|妻子|爸爸|妈妈|爸|妈|哥|姐|弟|妹|孩子|儿子|女儿)"
)


def owner_conflict(text: str, query: str) -> bool:
    """候选里的「X的<关系人>」和问句里的「X她/他」冲突（问本人，却答人家属）。

    问句自己问了那个关系（「小雅她男朋友在哪儿上班」）就不算冲突——那正是要答的。
    """
    body = str(text or "")
    asked = relation_words(query)
    for name in named_pronoun_subjects(query):
        for match in re.finditer(re.escape(name), body):
            tail = body[match.end() : match.end() + 8]
            hit = _RELATION_TAIL.search(tail)
            if hit and canonical(hit.group(1)) not in asked:
                return True
    return False


def name_focus(query: str, text: str, *, bonus: float = 0.04) -> float:
    """问句里「小雅她…」时，候选含「小雅」的加分：问的就是这个人本人。"""
    names = named_pronoun_subjects(query)
    if not names:
        return 0.0
    body = str(text or "")
    return bonus if any(name in body for name in names) else 0.0


def role_bonus(query: str, text: str, *, bonus: float = 0.035) -> float:
    """问句和候选共享角色词时的加分；一个都不共享但有角色词时不罚（可能藏在别处）。"""
    asked = relation_words(query)
    if not asked:
        return 0.0
    return bonus if asked & relation_words(text) else 0.0
