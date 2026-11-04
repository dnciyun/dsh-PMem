"""查询理解：把一句自然中文问句压成"真正要搜的东西"。

对抗实测暴露的问题不是"库里有不有"，而是**问句没被读懂**：

    长问句   recall@5 = 0.55
    短关键词 recall@5 = 0.85

同一批 20 题、同一个库，只换了问法。丢的那部分全在「什么 / 怎么 / 注意 / 时候」
这类疑问词和虚词上——它们既不是答案，又会把 OR 检索的候选池稀释掉。

所以这里做两件很小的事：

1. ``content_terms``：去疑问词、去虚词，留下内容词（中文按双字、英文按词）；
2. ``coverage``：候选覆盖了多少"查询里的双字"，用**覆盖率**而不是长度归一的相似度。
   覆盖率对长问句天然公平——它只看"我这句话里的实词，你命中了几成"。
"""

from __future__ import annotations

import re

# 疑问词 + 高频虚词。判断标准只有一条：**它几乎不可能是答案的一部分**。
# 宁可少删（漏删只是噪音），不可多删（删了就是没搜）。
STOP_PHRASES = (
    "为什么", "是什么", "有什么", "什么样", "怎么样", "怎么办", "怎么", "怎样", "如何",
    "什么", "哪些", "哪个", "哪里", "哪儿", "多少", "几个", "是否", "是不是", "有没有",
    "能不能", "会不会", "要不要", "可不可以", "请问", "告诉我", "帮我", "我想知道",
    "记不记得", "还记得", "记不记得住", "知道吗", "说一下", "讲一下", "是什么的",
)
STOP_WORDS = {
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "咱", "自己",
    "的", "了", "着", "过", "是", "在", "有", "和", "与", "或", "就", "都", "也",
    "还", "又", "再", "才", "只", "被", "把", "给", "对", "向", "从", "到", "为",
    "那", "这", "个", "些", "里", "上", "下", "中", "一个", "一下", "一点",
    "吗", "呢", "吧", "啊", "呀", "嘛", "哦", "嗯", "的话", "时候", "地方",
    "东西", "事情", "回事", "情况", "问题", "要求", "需要", "应该", "可以", "就是",
    "注意", "记得", "说", "问", "看", "让", "把", "跟", "比", "因为", "所以",
    "但是", "而且", "然后", "如果", "虽然", "不过", "以及", "其中", "关于",
}

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_.\-/\\:]+")


def content_terms(query: str, *, keep_single_latin: bool = True) -> list[str]:
    """抽出内容词。中文输出双字，英文/数字原样。

    中文没有词典可用（本地零依赖），所以用双字切分：``"微信人格"`` → ``微信/信人/人格``。
    这和 FTS 里的 ``text_seg`` 是同一套切法，所以覆盖率的分子分母能对齐。

    **单字虚词不再逐字删除**（0.12.0）。以前把「我/上/下/中/个/里/这/那……」从整句里
    抠掉再切双字，会把词抠成两半：「早上」→「早」、「上班」→「班」、「公司」→「公」、
    「时间」→「间」，然后接缝处还造出「早不」「班挤」这种不存在的双字。
    后果很具体：问「我早上不挤那趟车之后改成了什么」，命中的记忆里明明有「早上挤地铁」，
    覆盖率却是 **0**——因为它拿到的词是「不挤/趟车/车之/之后/后改/改成」，一个都不在该记忆里。
    虚词的降权交给 IDF（``coverage_idf_sets`` 本来就算候选里的 df），不需要靠删字。
    """
    text = str(query or "").strip()
    if not text:
        return []
    for phrase in sorted(STOP_PHRASES, key=len, reverse=True):
        text = text.replace(phrase, " ")
    # 只整词删多字虚词（的话/时候/东西……）：单字虚词留在句子里让 IDF 去压，
    # 删单字等于在词中间开刀。
    for word in sorted(STOP_WORDS, key=len, reverse=True):
        if len(word) == 1:
            continue
        text = text.replace(word, " ")

    out: list[str] = []
    for chunk in text.split():
        for latin in _LATIN.findall(chunk):
            if len(latin) >= 2 or keep_single_latin:
                out.append(latin.lower())
        for run in _CJK.findall(chunk):
            if len(run) == 1:
                continue
            for i in range(len(run) - 1):
                out.append(run[i : i + 2])
    # 去重保序
    seen: set[str] = set()
    uniq = []
    for term in out:
        if term not in seen:
            seen.add(term)
            uniq.append(term)
    return uniq


def bigrams(text: str) -> set[str]:
    """正文的双字集合（和 content_terms 的中文切法一致）。"""
    body = str(text or "")
    out: set[str] = set()
    for run in _CJK.findall(body):
        if len(run) == 1:
            out.add(run)
            continue
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    for latin in _LATIN.findall(body):
        out.add(latin.lower())
    return out


def coverage(terms: list[str], text: str) -> float:
    """查询内容词被正文覆盖的比例（0..1）。分母是查询自己，和正文长短无关。"""
    if not terms:
        return 0.0
    hay = bigrams(text)
    if not hay:
        return 0.0
    hit = 0.0
    for term in terms:
        if term in hay:
            hit += 1.0
        elif len(term) > 2 and any(term in h or h in term for h in hay if len(h) >= 2):
            # 英文缩写/编号之类，允许包含匹配算半分
            hit += 0.5
    return hit / len(terms)


def coverage_idf(terms: list[str], texts: list[str], *, target: str) -> float:
    """带 IDF 的覆盖率：在**候选集内**越罕见的词越值钱。

    q13 那种题（"B 站养鱼那个插件叫什么版本"）里，"插件/版本" 到处都是、
    "养鱼" 只有那一条——平权覆盖率会把三条打成并列 0.43，加权之后才分得开。
    IDF 用候选集算，不扫全库：候选集就是这次检索的"当下语料"，够用且零成本。
    """
    if not terms or not texts:
        return 0.0
    import math

    hay = bigrams(target)
    total = len(texts)
    weights: list[float] = []
    hits: list[float] = []
    for term in terms:
        df = sum(1 for t in texts if term in bigrams(t))
        weight = math.log(1.0 + total / (1.0 + df))
        weights.append(weight)
        hits.append(weight if term in hay else 0.0)
    denom = sum(weights)
    return (sum(hits) / denom) if denom else 0.0


def coverage_idf_sets(
    terms: list[str],
    hay_sets: list[set[str]],
    target: set[str],
    weights: dict[str, float] | None = None,
) -> float:
    """``coverage_idf`` 的免重算版：候选集的双字集合由调用方切一次就好了。

    为什么单独留这一个：``coverage_idf`` 会在每次比较时重切所有候选文本，
    60 个候选 × 10 个词就是 600 次正则——实测把 9 ms 拖到 150 ms。

    ``weights``（0.12.0）：词权由**全库** df 给出时传进来，换掉「候选集内 df」。
    候选集本身是被词法挑出来的，在里面统计 df 是**反向偏置**：虚词双字恰恰是
    「为什么这条会被选中」的原因，于是它们在候选集里反而不罕见，内容词才罕见，
    信号被压平。全库 df 才能回答「这个词在整个记忆里有多稀罕」。

    给了 ``weights`` 时还有一条更重要的规则：**库里根本不存在的词不进分母**。
    问句天然带接缝双字（「我早/上不/挤那/趟车」），它们谁都匹配不上，
    留在分母里只是把每条记忆的覆盖率一起稀释到 0.05 量级，排序就看不出差别了。
    """
    import math

    if not terms or not hay_sets:
        return 0.0
    total = len(hay_sets)
    num = 0.0
    denom = 0.0
    for term in terms:
        if weights is not None:
            weight = weights.get(term)
            if not weight:  # df=0：这句话里的接缝词，库里没有，不参与分母
                continue
        else:
            df = 0
            for hay in hay_sets:
                if term in hay:
                    df += 1
            weight = math.log(1.0 + total / (1.0 + df))
        denom += weight
        if term in target:
            num += weight
    return (num / denom) if denom else 0.0
