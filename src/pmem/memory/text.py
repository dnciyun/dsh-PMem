"""文本处理：归一化、注入消毒、断言签名、分词、3-gram。

三件事必须在这里做完，否则后面全线受污染：

1. **消毒（G13）**：记忆正文最终会被塞进某个模型的上下文，`<|im_start|>`、
   `system:`、CQ 码这类东西必须在入库前剥掉；
2. **归一化**：全角/零宽/空白/千分位统一，检索与去重都靠它；
3. **断言签名**：同源去重、冲突配对、聚类都以它为键（03 §2.2）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff\u2060]")
_WS = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL = re.compile(r"\n{3,}")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_CHAT_MARKERS = re.compile(r"<\|[a-zA-Z0-9_\-]+\|>")
_ROLE_LINE = re.compile(r"(?im)^\s*(system|assistant|user|tool)\s*[:：]")
_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")
_INJECTION_PHRASES = re.compile(
    r"(ignore (all )?(previous|prior) (instructions|prompts)|disregard (the )?(above|previous)|"
    r"忽略(之前|以上|前面)的?(所有)?(指令|提示)|你现在是(一个)?(无限制|不受限))",
    re.IGNORECASE,
)

# 中文持有者 / 谓词模板（03 §2.2）
_HOLDER = r"(我|我自己|自己|用户|主人|他|她)"
# 每条模式自己捕获 obj：这样"在腾讯工作"这种宾语夹在谓词中间的句式才能取到值
_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?:不|没)(?:是)?(?:很|非常|特别)?(?:喜欢|爱|待见)(?P<obj>[^，。；！？,.;!?\n]{1,24})", "dislikes"),
    (r"(?:不喜欢|讨厌|受不了|反感)(?P<obj>[^，。；！？,.;!?\n]{1,24})", "dislikes"),
    (r"(?:很|非常|特别|超|挺)?(?:喜欢|爱|迷上|偏爱)(?P<obj>[^，。；！？,.;!?\n]{1,24})", "likes"),
    (r"住在(?P<obj>[^，。；！？,.;!?\n]{1,24})", "lives_in"),
    (r"搬到(?P<obj>[^，。；！？,.;!?\n]{1,24})", "lives_in"),
    (r"(?:(?<![现正存])在|于)(?P<obj>[^，。；！？,.;!?\n]{1,16}?)(?:工作|上班|任职)", "works_at"),
    (r"(?:使用|在用|用)(?P<obj>[^，。；！？,.;!?\n]{1,24})", "uses"),
    (r"(?:正在)?(?:做|开发|研究)(?P<obj>[^，。；！？,.;!?\n]{1,24})", "works_on"),
)

_FIRST_PERSON = {"我", "我自己", "自己", "用户", "主人", ""}
_ADVERB_TAIL = ("目前", "现在", "最近", "以前", "之前", "已经", "一直", "通常", "平时", "就", "还", "刚好", "很早")


def _holder_of(prefix: str) -> str:
    """谓词之前那截里取持有人：认第一人称，其余取最后 2–3 个字当名字。"""
    text = normalize(prefix).strip("，,。.；;：: 　")
    for adverb in sorted(_ADVERB_TAIL, key=len, reverse=True):
        if text.endswith(adverb):
            text = text[: -len(adverb)].strip()
    if not text:
        return "self"  # 中文常省略主语，省略即"我"
    if text in _FIRST_PERSON:
        return "self"
    tail = text[-3:]
    return _norm_obj(tail) or "self"


def claim_sig(text: str, kind: str = "semantic") -> str:
    """断言签名：``slot:<holder>|<pred>|<obj>|<neg>``。

    用途：同源去重、冲突配对、聚类。命中不了模板就退化成内容哈希
    （``txt:`` 前缀，因此不参与"同断言合并"）。
    """
    norm = normalize(text)
    for pattern, pred in _PATTERNS:
        m = re.search(pattern, norm)
        if not m:
            continue
        obj = _norm_obj(m.group("obj"))
        if not obj:
            continue
        holder = _holder_of(norm[: m.start()])
        # 否定统一表达成 likes + neg=not，否则"喜欢"与"不喜欢"会落进两个不同谓词桶，
        # 冲突检测永远配不上对（实测踩过）。
        if pred == "dislikes":
            pred = "likes"
            neg = "not"
        else:
            neg = "not" if "不" in m.group(0) else "yes"
        return f"slot:{holder}|{pred}|{obj}|{neg}"
    # 定义句：X 是 Y
    m = re.match(r"(?P<subj>[^，。；！？,.;!?\n]{1,20}?)\s*(?:是|就是|叫做|称为)\s*(?P<obj>[^，。；！？,.;!?\n]{1,60})", norm)
    if m:
        return f"def:{_norm_obj(m.group('subj'))}|{_norm_obj(m.group('obj'))}"
    # 兜底
    return "txt:" + sha256_hex(norm)[:32]


def normalize(text: str) -> str:
    """归一化：NFKC（全角→半角）、去零宽、折叠空白、去千分位。"""
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH.sub("", t)
    t = _THOUSANDS.sub("", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = _WS.sub(" ", t)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


def is_mostly_ascii(text: str) -> bool:
    if not text:
        return False
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return ascii_n / len(text) > 0.8


def sanitize(text: str) -> tuple[str, list[str]]:
    """剥除注入载体，返回 (干净文本, 命中的规则名)。"""
    flags: list[str] = []
    out = text
    if _CHAT_MARKERS.search(out):
        flags.append("chat_marker")
        out = _CHAT_MARKERS.sub("", out)
    if _CQ_CODE.search(out):
        flags.append("cq_code")
        out = _CQ_CODE.sub("", out)
    if _ROLE_LINE.search(out):
        flags.append("role_prefix")
        out = _ROLE_LINE.sub("", out)
    if _INJECTION_PHRASES.search(out):
        # 不删正文（可能就是要记的内容），只标记：注入判定交给渲染层与测试
        flags.append("injection_phrase")
    return out.strip(), flags


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_TRAILING = ("这边", "那边", "这里", "那里", "附近", "一带", "这块", "那栋")


def _norm_obj(text: str) -> str:
    """归一化槽位值：去标点、去语气尾缀、去方位尾缀。

    没有这一步，"我现在就住在杭州这边"和"我住在杭州"会变成两个不同的值，
    冲突检测与去重都会失效。
    """
    t = normalize(text).strip("。．.!！?？,，;；:：\"'“”‘’()（） ")
    t = re.sub(r"(的|了|呢|吧|啊|哦|呀)+$", "", t)
    for suffix in _TRAILING:
        if t.endswith(suffix) and len(t) > len(suffix):
            t = t[: -len(suffix)]
    return t


def _cjk_bigrams(text: str) -> list[str]:
    """CJK 双字切分：没有 jieba 时的词法兜底。

    为什么要这一步：FTS5 的 trigram 分词器要求查询 ≥3 字符，
    而中文最常用的正是"杭州""记忆"这类 2 字词。用双字 token 可以
    让 unicode61 通道直接命中，避免 `LIKE '%x%'` 全表扫描（10 万条会慢到不可用）。
    """
    chars = [c for c in text if not c.isspace()]
    out: list[str] = []
    for i in range(len(chars) - 1):
        pair = chars[i] + chars[i + 1]
        if all("\u4e00" <= ch <= "\u9fff" for ch in pair):
            out.append(pair)
    return out


try:  # 每次调用都 try-import 会让没装 jieba 的机器多花 ~0.5 ms/次
    import jieba as _JIEBA  # type: ignore
except Exception:  # noqa: BLE001
    _JIEBA = None


def tokenize(text: str) -> str:
    """给 FTS 的分词串。

    - 英文/数字：原样（unicode61 会切词）
    - 中文：装了 jieba 就切词；没装就用双字切分（按空格分段，避免跨段造词）
    """
    norm = normalize(text)
    # 不要按整篇判断"是不是英文"：一条记忆里常常既有路径又有中文
    # （「DSH 插件与入口索引:1B站养鱼 dsh-bili-whale 0.4.6」），
    # 整篇判成 ASCII 就直接返回原文，于是里面的中文一个双字都进不了索引——
    # 实测这让所有"路径/ID 多"的记忆对中文关键词完全不可见。按段判断才对。
    if _JIEBA is not None and not is_mostly_ascii(norm):
        return " ".join(t for t in _JIEBA.cut(norm) if t.strip())
    tokens: list[str] = []
    for seg in norm.split():
        if is_mostly_ascii(seg):
            tokens.append(seg)
        else:
            tokens.extend(_cjk_bigrams(seg))
    return " ".join(tokens)


# 疑问句里的「没有信息量」的部分。检索是按双字片段撞原文的，
# 所以「我叫什么？」真正该去撞的是「叫」，而不是「我叫」。
_QUERY_NOISE = (
    "请问", "帮我", "告诉我", "你还记得", "记得", "我说过", "我跟你说过", "上次说的",
    "什么", "哪个", "哪里", "哪儿", "哪天", "几号", "几天", "几点", "多少", "怎么",
    "为什么", "是不是", "有没有", "对不对", "好不好", "吗", "呢", "吧", "啊", "呀",
    "我", "你", "您", "他", "她", "它", "的", "了", "是", "在", "有", "和", "与", "跟",
    "这", "那", "那个", "这个", "一下", "回事", "呀",
)
_SELF_ALIAS = (("我", "用户"), ("本人", "用户"), ("自己", "用户"))
# 自称展开**注入**的词：「用户」在记忆里几乎人人是主语，df 极高。
# 它作为查询变体的 token 会把候选池灌满「碰巧以用户开头」的记忆（实测：一句
# 「我早上不挤那趟车」会因为变体多出 50 条以「用户」开头的候选）。
ALIAS_TOKENS = frozenset(target for _old, target in _SELF_ALIAS)


def query_variants(text: str) -> list[str]:
    """给检索用的查询变体（不含原句本身）。

    两种改写，各自解决一类"问法不同"：

    - **去噪**：去掉疑问词与自指，留下名词。``我叫什么`` → ``叫什么``；
    - **自称展开**：记忆里常写「用户叫林野」，用户却问「我叫什么」。
      把自指换成叙述者视角的 ``用户``，两边词表才对得上。

    变体只作为**额外通道**参与融合，原句永远保留 —— 改写错了不能把正确答案挤掉。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    cleaned = raw
    for word in _QUERY_NOISE:
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    out: list[str] = []
    if cleaned and cleaned != raw:
        out.append(cleaned)
    expanded = raw
    for old, new in _SELF_ALIAS:
        expanded = expanded.replace(old, new)
    if expanded != raw:
        out.append(expanded)
    seen: set[str] = set()
    unique: list[str] = []
    for item in out:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def char_ngrams(text: str, n: int = 3) -> set[str]:
    """字符 n-gram，用于无向量时的相似度近似（MMR / 近重复粗筛）。"""
    t = normalize(text)
    if len(t) <= n:
        return {t} if t else set()
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def estimate_tokens(text: str) -> int:
    """粗估 token：中文约 1 字 ≈ 0.8 token，英文约 0.25 token/字符。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.9 + other * 0.28) + 1
