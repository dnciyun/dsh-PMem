"""确定性写入兜底：把用户**明确陈述**的事实写成记忆，不调模型。

为什么需要它：B 方案里"记不记"由模型决定，实测同一份剧本跑三次，写进去的条数是
3 / 6 / 7，生日有时写有时不写。检索修得再好，没写的东西也找不回来。

这里只做**低风险、高价值**的一小撮句式——名字、生日、住在哪、做什么。
它们满足三个条件：句式封闭、抽取可单测、写错了代价小（用户一眼能看出不对）。

刻意不做的事：不从问句里抽（「我生日是几号？」不是事实）、不猜、不补全。
抽到的每一条都带 `rule` 来源，事件日志里能查得到是谁写的。
"""

from __future__ import annotations

import re
from typing import Any

_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM = r"[0-9]{1,2}|[一二三四五六七八九十]{1,3}"

# 名字：只认「我叫X」「我的名字是X」；后面接「什么/啥」的是问句，不抽。
NAME_RE = re.compile(r"我(?:的名字)?(?:叫|是)\s*([\u4e00-\u9fa5A-Za-z]{1,6}?)(?=[，。！？,.!\s]|就行|即可|$)")

# 生日：中英文数字都认（剧本里就是「一月二十三号」）。
BIRTHDAY_RE = re.compile(rf"我(?:的)?生日(?:是|在)?\s*({_NUM})\s*月\s*({_NUM})\s*[日号]")

# 住在哪：「我住在杭州」「我搬到上海」「我在杭州生活」。
CITY_RE = re.compile(r"我(?:住在|搬到|在)\s*([\u4e00-\u9fa5]{2,6}?)(?:市|做|生活|工作|住|，|。|$)")

# 做什么：「我在杭州做前端开发」「我做后端」。
JOB_RE = re.compile(r"我(?:在[\u4e00-\u9fa5]{2,6})?(?:做|干)([\u4e00-\u9fa5]{2,8}?)(?:开发|工作|工程师|，|。|$)")

# 家人：我妈叫王秀兰。只认「妈/爸/姐/哥/弟/妹」这类最明确的。
FAMILY_RE = re.compile(r"我(?:的)?(妈|妈妈|母亲|爸|爸爸|父亲|姐姐|哥哥|弟弟|妹妹)(?:叫|是)\s*([一-龥]{2,4})")

_STOP = {
    "什么", "啥", "谁", "哪儿", "哪里", "多少", "怎样", "怎么样",
    "在", "是", "的", "了", "个", "有点", "一点", "开发",
}
# 只要句子里带着疑问词，就不抽——「我叫什么？希望你平时怎么称呼我？」
# 这种句子结尾没有问号，但整句就是在提问。
_QUESTION = re.compile(r"(什么|啥|几号|哪天|多少|怎么|为什么|吗|呢|谁)")
_CN_NUM = re.compile(r"^[一二三四五六七八九十]{1,3}$")


def _cn_to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_DIGIT.get(text[1:], 0)
    if "十" in text:
        tens, _, ones = text.partition("十")
        value = _CN_DIGIT.get(tens, 0) * 10 + _CN_DIGIT.get(ones, 0)
        return value or None
    return _CN_DIGIT.get(text)


def extract(text: str) -> list[dict[str, Any]]:
    """从一句话里抽事实。返回空列表是常态——抽不到比抽错好。"""
    sentence = str(text or "").strip()
    if not sentence or _QUESTION.search(sentence):
        return []

    found: list[dict[str, Any]] = []

    def push(text_value: str, rule: str, kind: str, group: str, grade: str) -> None:
        if any(item["text"] == text_value for item in found):
            return
        found.append(
            {"text": text_value, "rule": rule, "kind": kind, "groups": [group], "grade": grade}
        )

    match = NAME_RE.search(sentence)
    if match:
        name = match.group(1).strip()
        if name and name not in _STOP and len(name) >= 2:
            push(f"用户叫{name}", "name", "semantic", "我/姓名", "S")

    match = BIRTHDAY_RE.search(sentence)
    if match:
        month, day = _cn_to_int(match.group(1)), _cn_to_int(match.group(2))
        if month and day and 1 <= month <= 12 and 1 <= day <= 31:
            push(f"用户的生日是{month}月{day}日", "birthday", "semantic", "我/生日", "A")

    match = FAMILY_RE.search(sentence)
    if match:
        who, name = match.group(1), match.group(2).strip()
        if name and name not in _STOP and len(name) >= 2:
            push(f"用户的{who}叫{name}", "family", "semantic", "关系/家人", "B")

    match = JOB_RE.search(sentence)
    if match:
        job = match.group(1).strip()
        if job and job not in _STOP and len(job) >= 2:
            push(f"用户做{job}", "job", "semantic", "我/工作", "A")

    match = CITY_RE.search(sentence)
    if match:
        city = match.group(1).strip()
        if city and city not in _STOP and len(city) >= 2 and city != "杭州做":
            push(f"用户在{city}", "city", "semantic", "我/城市", "A")

    return found


def write(
    store: Any,
    text: str,
    *,
    session_id: str | None = None,
    actor_rule: str = "autowrite",
) -> list[dict[str, Any]]:
    """抽到就写，重复的靠 idem 幂等——同一句声明说三遍只落一条。"""
    from .text import claim_sig, sha256_hex

    written: list[dict[str, Any]] = []
    for item in extract(text):
        key = sha256_hex(f"{actor_rule}:{claim_sig(item['text'])}")[:16]
        result = store.add(
            item["text"],
            kind=item["kind"],
            groups=item["groups"],
            grade=item["grade"],
            provenance="user_explicit",
            source_kind="rule",
            session_id=session_id,
            idem_key=f"rule:{key}",
        )
        written.append({**item, "id": (result or {}).get("id"), "duplicate": bool((result or {}).get("idempotent_replay"))})
    return written


__all__ = ["extract", "write"]
