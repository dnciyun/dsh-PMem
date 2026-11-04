"""答案类型（M28 / 0.26.0）：问句要的是「哪种东西」，记忆里得真有那种东西。

**要解决的问题**：抗干扰那 7 道「相邻事实进前 3」里，「问数字、跑到一段没有数字的
叙述」占了两道——问「成功率数字是多少」，第 1 名是「回滚脚本又重写了一遍」（没有数字）；
问「房子在几层」，第 1 名是「回阿May那里打地铺」（没有楼层）。
词法相似度管不了这件事：两句话说的都是同一件事，只是**没有回答那个槽位**。

做法刻意保守（对齐 PLAN §「可解释、可关」）：

1. 只认问句里**写法明确**的类型：多少/几层/谁/哪+地点名词；
2. 只对能**用字面确认**的类型生效（数字、地点、时间）；人名不做（没有 NER，
   一行里出现人名不代表回答了「谁」，硬做只会误伤）；
3. 默认「命中加分、没命中不扣分」——扣分档做成配置（``miss_factor``），
   因为抽错类型时扣分会把对的挤掉，必须先用 A/B 证明再开。
"""

from __future__ import annotations

import re

# 问句里出现这些，说明在问「哪里」；配合「哪」一起看，避免把「哪个」当地点。
PLACE_NOUNS = (
    "湖",
    "河",
    "江",
    "海",
    "山",
    "街",
    "路",
    "巷",
    "店",
    "楼",
    "城",
    "市",
    "区",
    "县",
    "镇",
    "村",
    "公园",
    "广场",
    "桥",
    "机场",
    "车站",
    "学校",
    "公司",
    "医院",
    "小区",
)

# 人型提问：「哪 + 人名词」也算问人（「哪个室友最爱组局」）。「谁」已经覆盖大部分，
# 这里只补最常见的称呼；没有 NER，认不出的就退回中性，不硬判。
PERSON_NOUNS = (
    "室友",
    "朋友",
    "同事",
    "同学",
    "老师",
    "老板",
    "妈妈",
    "爸爸",
    "女儿",
    "儿子",
    "哥哥",
    "姐姐",
    "弟弟",
    "妹妹",
    "老婆",
    "老公",
    "男朋友",
    "女朋友",
    "家人",
    "亲戚",
    "邻居",
)

# 数字型提问：明确问数量/序号。
NUMBER_MARKERS = (
    "多少",
    "几号",
    "几点",
    "几分",
    "几层",
    "几个",
    "几次",
    "几天",
    "几年",
    "几位",
    "多大",
    "多高",
    "多重",
    "多长",
    "百分之",
)
# 光一个「几」不够（「几点了？」确实问时间，但「怎么几」这种用法到处都是），
# 必须配上单位才算数。
NUMBER_UNITS = ("层", "个", "岁", "斤", "块", "天", "年", "次", "人", "位", "分", "点", "倍", "成")

TIME_MARKERS = ("什么时候", "哪天", "哪一年", "哪年", "哪个季节", "几点")

# 数字型答案的字面证据：阿拉伯数字，或「中文数词 + 量词」。
# 单位只收**能当答案**的那些（楼层/年龄/重量/次数…），不收「一遍」的「遍」——
# 「回滚脚本又重写了一边」这种句子有「一遍」，但它不是数字答案。
_DIGITS = re.compile(r"\d")
_CN_NUM = "一二三四五六七八九十百千万两半"
_CN_NUMBER = re.compile(rf"[{_CN_NUM}]+\s*(?:层|楼|岁|斤|块|天|年|次|人|位|千米|公里|米|公斤|克|元|个|只|条|倍|成|%|％)")
_TIME_WORDS = re.compile(r"\d{1,4}\s*[年月日号]|[上下本这那]?个?月[初中底下]?|今天|昨天|前天|明天|后天|上周|下周|去年|今年|明年")
_PLACE_EVIDENCE = re.compile(
    "|".join(f"[\\u4e00-\\u9fff]{{1,3}}{n}" for n in PLACE_NOUNS)
    + r"|这里|那儿|那边|附近"
)


def kind_of(query: str) -> str | None:
    """问句在问哪种东西：``number`` / ``place`` / ``time`` / ``person``，认不出返回 None。

    优先级：谁（人）> 数字 > 时间 > 地点。原因：「几号」既像数字又像时间，
    但对答案的要求一样（都得给出那一天），归一档不影响判断。
    """
    text = str(query or "")
    if not text:
        return None
    if "谁" in text:
        return "person"
    if any(marker in text for marker in NUMBER_MARKERS):
        return "number"
    if "几" in text and any(unit in text for unit in NUMBER_UNITS):
        return "number"
    if any(marker in text for marker in TIME_MARKERS):
        return "time"
    if any(word in text for word in ("哪", "哪里", "哪儿")):
        if any(noun in text for noun in PERSON_NOUNS):
            return "person"
        if any(noun in text for noun in PLACE_NOUNS):
            return "place"
        return None
    if "什么地方" in text or "地点" in text:
        return "place"
    return None


def has_evidence(text: str, kind: str) -> bool:
    """这一行里有没有「这种类型的东西」的字面证据。"""
    body = str(text or "")
    if not body:
        return False
    if kind == "number":
        return bool(_DIGITS.search(body) or _CN_NUMBER.search(body))
    if kind == "time":
        return bool(_TIME_WORDS.search(body))
    if kind == "place":
        return bool(_PLACE_EVIDENCE.search(body))
    # person：不做字面判断（没有 NER，硬判会误伤），一律算「没证据」→ 调用方应当跳过。
    return False


__all__ = ["kind_of", "has_evidence"]
