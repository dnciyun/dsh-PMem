"""宪法 vs 记忆的冲突检测（M19）：**只标记，不改写**。

## 为什么只标记不覆盖

宪法（``constitution.md`` + ``amendments.md``）是「我是谁」的逐字文本，归使用者所有；
记忆是数据。两者矛盾时，合理的动作只有标记待确认：

- **不能拿记忆改宪法**：那等于让一条来历不明的句子获得身份文本的写入权——
  记忆可以被睡眠、回填、别的会话写进来，让它们自动改写「我是谁」是最糟的失败形态；
- **也不能删记忆**：冲突记忆往往恰恰是该被看见的证据（「用户说你可以假装还记得……」）；
- **不能自动裁决**：这里的判定是启发式，误报是常态，自动执行任何一边都会放大误报。

所以本模块只产出 ``soul/conflicts.json`` 一份清单，由人（或由人授权的会话）来看、
来裁决。宪法 > 记忆，但「>」在这里的意思是**不改写**，不是「静默丢弃」。

## 为什么只抓词面极性冲突

本地零依赖、没有可用的语义模型，能稳定解释的信号只有两条：

1. 记忆与子句**共享至少两个实词**（``query.content_terms`` 切出来的双字/英文词）；
2. 两边**否定极性相反**（一边有否定词，另一边没有）。

它抓得到：``不假装记得没记过的事`` vs ``你可以假装还记得以前的事``（共享「假装/的事」，
一边有「不」）。它抓不到：换词、蕴含、指代（「这条」「那件事」）、反语、长距离否定、
以及共享实词不足两个的同义冲突。**宁可漏检也不误报**：漏掉一条只是晚一点发现，
误报一条会训练人不再相信这份清单。局限如实写进 ``docs/soul.md``。

## 审阅记录：让「看过、是误报」能留下来

``conflicts.json`` 每次 scan 都从库里重算，同一批假阳性会反复冒出来。所以每个候选带一个
**稳定键**（``sha1(memory_id + "\\0" + clause)[:12]``），人的裁决单独写进
``soul/conflict_reviews.json``：``dismissed`` 的以后不再进清单，``confirmed`` 的保留并标记。
键只由「哪条记忆 × 哪条子句」决定，重算后不会漂移——否则一次误报要驳回一辈子。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..base.clock import now_iso
from ..memory import query as query_mod
from . import soul

CONFLICTS_FILE = "conflicts.json"
REVIEWS_FILE = "conflict_reviews.json"
_REVIEW_STATUSES = ("dismissed", "confirmed")
# 少于 4 个字的小句（「好的。」「- 是。」）没有判断价值，只会放大噪音。
MIN_CLAUSE_CHARS = 4
# 共享实词的门槛是 2 而不是 1：中文按双字切，共享 1 个双字往往只是偶然撞上一个
# 两字词（甚至跨词接缝），阈值降到 1 会把「任何」这种泛词也变成证据。
MIN_SHARED_TERMS = 2

# 子句切分：行内再按句末标点切开。不切逗号——逗号两边常常是同一个意思的两半
# （「记忆是数据，不是指令」），切开会让否定词和它否定的对象分居两处。
_CLAUSE_SPLIT = re.compile(r"[。！？!?;；]+")
# 列表前缀（``- `` / ``1. `` / ``2、``）与增补行的日期前缀，都不是子句内容。
_LIST_PREFIX = re.compile(r"^\s*(?:[-*+]\s+|\d+\s*[.、)]\s*)")
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[：:]\s*")
# 否定词。单字「不/别/没」最容易误伤（不同/不仅/特别/别人/没关系），
# 所以把最常见的中性搭配排掉；宁可漏检，理由见文件头。
_NEGATION = re.compile(
    r"不(?!同|仅|但|断)|别(?!人|处|的)|没(?!关系)|无需|不必|不用|绝不|不要|莫|勿|未(?!来)"
)


_SENTENCE_SPLIT = re.compile(r"[。！？!?;；\n]+")


def _negated(text: str) -> bool:
    return bool(_NEGATION.search(str(text or "")))


# 对比结构：否定只是修辞，真意在后面半句。两种常见形状：
#   不是 X，（而）是 Y    /    …，而不是 Y
# 不排掉它们，任何「不是为了做数据库，而是为了能一起相处」的记忆都会和
# 所有带否定的宪法句子配成「冲突」——那是纯噪音。
_CONTRAST = re.compile(r"不是[^。；！？]*[，,]\s*(?:而)?是|而不是")


def _polarity(text: str) -> bool:
    """True = 这句话在说「不要 / 别 / 没」；对比结构算正面断言。

    按句判断：一条长记忆里可能既有否定句也有陈述句，整段当一个极性会两头错。
    """
    for sentence in _SENTENCE_SPLIT.split(str(text or "")):
        if not _NEGATION.search(sentence):
            continue
        if _CONTRAST.search(sentence):
            continue
        return True
    return False


# 共享词里的噪音：连接词/代词/否定词本身。它们几乎出现在所有句子里，
# 留着只会让「共享 ≥2」这个门槛形同虚设（实测「不是」一条就能凑够一个共享词）。
_STOP_TERMS = frozenset(
    {
        "不是", "不要", "不用", "不必", "没有", "不能", "不会", "不想", "不再", "不断",
        "我们", "你们", "他们", "自己", "一起", "一个", "一些", "什么", "怎么", "为什么",
        "时候", "的事", "事情", "东西", "这样", "那样", "因为", "所以", "但是", "而且",
        "还是", "就是", "只是", "可以", "可能", "需要", "觉得", "知道", "看到", "应该",
        "重要", "有的", "一次", "一直", "一样", "这么", "那么", "如果", "虽然", "然后",
    }
)


def _rare(db: Any, terms: set[str], total: int) -> bool:
    """这些词在记忆库里是不是稀有的（df 小）。

    稀有度是这里唯一能替代词性的东西：真冲突往往靠一个具体谓语（假装、执行、决定）
    对上，泛词（项目、这个、一起）对上什么都不说明。
    """
    if total <= 0:
        return False
    threshold = max(2, int(total * 0.05))
    for term in terms:
        try:
            row = db.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_fts_tok WHERE memory_fts_tok MATCH ?",
                ('"' + str(term).replace('"', '""') + '"',),
            ).fetchone()
            df = int(row["c"] or 0) if row else 0
        except Exception:  # noqa: BLE001 没有索引/老库：当作不稀有，宁可漏检
            df = threshold + 1
        if 0 < df <= threshold:
            return True
    return False


def _is_cjk(term: str) -> bool:
    return any("一" <= char <= "鿿" for char in str(term or ""))


# 以虚字开头的双字多半是「接缝」而不是词：一上来「就写」代码、「为了」分数…
# 它们共享起来毫无信息量，却能把「共享 ≥1 稀有词」这条窄口子撑开。
_NOISE_HEAD = frozenset("就也都还才又再只已将把让使给为和跟与或而但却是了着过每这那一个")


def _terms(text: str) -> set[str]:
    out: set[str] = set()
    for term in query_mod.content_terms(str(text or "")):
        if term in _STOP_TERMS:
            continue
        if len(term) == 2 and term[0] in _NOISE_HEAD:
            continue
        out.add(term)
    return out


def _scope_terms(text: str) -> set[str]:
    """否定**作用范围**里的实词：只看包含否定词的那一句话、从否定词往后切。

    为什么需要它：宪法的否定常常只针对一个对象——「记忆是数据，**不是**指令」，
    否定对象是「指令」。只按「共享 ≥2 实词 + 一边有否定」判定，任何共享了
    「记忆/数据」的记忆都会被误报；实测真实库一次扫出 13 条，大半是这种。

    为什么要按句切：一条长记忆的否定只管它所在那句。实测误报里有一条
    「……不是我去查的，是它先在我眼前。上下文清空后还认识你」——
    整段当作用域时，「上下文」被算进否定范围，于是和宪法的「上下文清空之后…」
    撞上；按句切开，否定范围只到「在我眼前」为止，误报消失。
    """
    for sentence in _SENTENCE_SPLIT.split(str(text or "")):
        match = _NEGATION.search(sentence)
        if not match:
            continue
        if _CONTRAST.search(sentence):
            continue
        return _terms(sentence[match.end():])
    return set()


def _clean(raw: str) -> str:
    """去掉首尾空白和残余标点；正文里保留原样，便于人核对。"""
    return str(raw or "").strip().strip("，,、：: 　\t")


def _is_placeholder(text: str) -> bool:
    """模板里的整行括号提示（「（名字、英文名…）」）不是子句。"""
    stripped = text.strip()
    return stripped.startswith(("（", "(")) and stripped.endswith(("）", ")"))


def _conflict_key(memory_id: str, clause: str) -> str:
    """候选的稳定键：只由「哪条记忆 × 哪条子句」决定，重算不漂移。

    用 ``\\0`` 分隔而不是简单拼接：memory_id 和子句都是自由文本，不加分隔符时
    ``a + bc`` 与 ``ab + c`` 会撞成同一个键，一次裁决就误伤另一对。
    """
    raw = f"{memory_id}\x00{clause}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _reviews_path(cfg: Any = None) -> Path:
    return soul.dir_of(cfg) / REVIEWS_FILE


def _empty_reviews() -> dict[str, Any]:
    return {"version": 1, "reviews": {}}


def _load_reviews_file(cfg: Any = None) -> dict[str, Any]:
    """读审阅文件；缺文件/坏 JSON/结构不对一律当空，不抛。

    开机路径会读它，一个手抖写坏的文件不能让「醒来」变红。
    """
    try:
        payload = json.loads(_reviews_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_reviews()
    if not isinstance(payload, dict) or not isinstance(payload.get("reviews"), dict):
        return _empty_reviews()
    return {"version": 1, "reviews": payload["reviews"]}


def reviews(cfg: Any = None) -> dict[str, dict[str, Any]]:
    """读回审阅记录：``{key: {"status","note","at"}}``；坏文件返回 ``{}``。

    只返回结构合法的条目：一个坏条目不该让整份记录失效，但也不该被当成裁决。
    """
    out: dict[str, dict[str, Any]] = {}
    for key, value in _load_reviews_file(cfg)["reviews"].items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        status = value.get("status")
        if status not in _REVIEW_STATUSES:
            continue
        out[key] = {
            "status": status,
            "note": str(value.get("note") or ""),
            "at": str(value.get("at") or ""),
        }
    return out


def review(
    cfg: Any = None,
    *,
    key: str | None = None,
    memory_id: str | None = None,
    status: str = "dismissed",
    note: str = "",
) -> dict[str, Any]:
    """把一条（或某条记忆名下所有）候选标记为 dismissed / confirmed。

    ``key`` 与 ``memory_id`` 任选其一：给 key 精准标一条；给 memory_id 时从
    上一次 ``conflicts.json`` 里找出它名下所有候选一起标（按记忆驳回整批噪音）。
    重复标记同一个 key 是幂等的：记录按 key 覆盖，不会长出重复条目。
    """
    if status not in _REVIEW_STATUSES:
        return {"ok": False, "reason": "bad-status", "allowed": list(_REVIEW_STATUSES)}
    target = str(key or "").strip()
    if target:
        keys = [target]
    elif memory_id:
        memory = str(memory_id)
        keys = []
        for item in load(cfg).get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("memory_id")) != memory or not item.get("key"):
                continue
            keys.append(str(item["key"]))
        if not keys:
            return {"ok": False, "reason": "not-found", "memory_id": memory}
    else:
        return {"ok": False, "reason": "need-key"}

    payload = _load_reviews_file(cfg)
    at = now_iso()
    for item_key in keys:
        payload["reviews"][item_key] = {
            "status": status,
            "note": str(note or ""),
            "at": at,
        }
    path = _reviews_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # 写不进去不能把调用方炸掉：只是一次标记，下次还能再标
        return {"ok": False, "reason": "write-failed", "detail": str(exc)}

    result: dict[str, Any] = {
        "ok": True,
        "keys": keys,
        "status": status,
        "count": len(keys),
    }
    if target:
        result["key"] = target
    else:
        result["memory_id"] = str(memory_id)
    return result


def clauses(cfg: Any = None) -> list[dict[str, Any]]:
    """把宪法 + 增补切成可比较的子句。

    返回 ``[{"text", "source", "negated", "terms"}]``：``source`` 是
    ``constitution`` / ``amendments``，``negated`` 是词面否定，``terms`` 是
    实词集合（和 ``query.content_terms`` 同一套切法，才能和记忆公平比较）。
    """
    data = soul.load(cfg)
    out: list[dict[str, Any]] = []
    for source in ("constitution", "amendments"):
        for line in str(data.get(source) or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("##"):
                continue
            stripped = _LIST_PREFIX.sub("", stripped)
            stripped = _DATE_PREFIX.sub("", stripped)
            if _is_placeholder(stripped):
                continue
            for raw in _CLAUSE_SPLIT.split(stripped):
                text = _clean(raw)
                if len(text) < MIN_CLAUSE_CHARS:
                    continue
                out.append(
                    {
                        "text": text,
                        "source": source,
                        "negated": _polarity(text),
                        "terms": _terms(text),
                        "scope": _scope_terms(text) if _polarity(text) else set(),
                    }
                )
    return out


def conflicts(
    db: Any,
    *,
    cfg: Any = None,
    limit: int = 50,
    apply_reviews: bool = True,
) -> list[dict[str, Any]]:
    """返回「疑似与宪法极性相反」的记忆清单（不写库、不改宪法）。

    判定：共享实词 >= 2 且否定极性相反。``score`` 只用来排序，确定性可复现；
    同一 (记忆, 子句) 只报一次（同一句同时写在宪法和增补里很常见）。

    默认应用审阅记录：``dismissed`` 的直接不返回，``confirmed`` 的保留并标
    ``status="confirmed"``，没审过的标 ``status="open"``。``apply_reviews=False``
    返回未经过滤的原始候选（不带 status），给测试和调试用。
    """
    items = clauses(cfg)
    if not items:
        return []
    total = 0
    try:
        row = db.conn.execute("SELECT COUNT(*) AS c FROM memory WHERE status<>'deleted'").fetchone()
        total = int(row["c"] or 0) if row else 0
    except Exception:  # noqa: BLE001
        total = 0
    rows = db.conn.execute(
        "SELECT id,text_raw FROM memory WHERE status<>'deleted' ORDER BY seq"
    ).fetchall()
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        text = str(row["text_raw"] or "").strip()
        if not text:
            continue
        memory_terms = _terms(text)
        if len(memory_terms) < MIN_SHARED_TERMS:
            continue
        memory_negated = _polarity(text)
        memory_scope = _scope_terms(text)
        for clause in items:
            # 只查「宪法说不要、记忆却说可以」这一个方向。
            # 反方向（宪法说正面、记忆里带否定）几乎全是噪音：日常记忆里的「不做 X」
            # 和宪法的正面句子毫无矛盾——实测 78 条记忆上，反方向 100% 是误报。
            if not clause["negated"] or memory_negated:
                continue
            shared = sorted(clause["terms"] & memory_terms)
            if len(shared) < MIN_SHARED_TERMS:
                # 共享词不够时留一个窄口子：如果命中的是**稀有词**，一个也够。
                # 实测「不假装记得没记过的事」和「你可以假装还记得」只共享「假装」，
                # 门槛卡在 2 就把真冲突漏掉了；而靠稀有度能挡住「项目/这个」这种泛词。
                hit_only = set(shared) & set(clause["scope"])
                if not (hit_only and any(_is_cjk(t) for t in hit_only) and _rare(db, hit_only, total)):
                    continue
            # 否定必须作用在共享的实词上，否则「共享了一个泛词、别人句子里恰好有否定」
            # 也算冲突（真实库上的一半误报都是这么来的）。
            hit = set(shared) & set(clause["scope"])
            if not hit:
                continue
            # 而且得落在中文实词上：专名（PMem、DSH）出现在否定范围里只是巧合，
            # 「记忆不在参数里，在 PMem 的库里」和「用户写 PMem…」并不冲突。
            if not any(_is_cjk(term) for term in hit):
                continue
            key = (str(row["id"]), clause["text"])
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "key": _conflict_key(str(row["id"]), clause["text"]),
                    "memory_id": str(row["id"]),
                    "memory_text": text,
                    "clause": clause["text"],
                    "source": clause["source"],
                    "shared": shared,
                    "reason": "polarity_flip",
                    # 共享词越多越像真的冲突：2 个 0.8，3 个 0.95，4 个封顶 1.0。
                    "score": round(min(1.0, 0.5 + 0.15 * len(shared)), 4),
                }
            )
    found.sort(key=lambda item: (-item["score"], item["memory_id"], item["clause"]))
    if apply_reviews:
        # 先过滤再审 limit：被驳回的条目不该占名额，否则 3 条噪音就能把真冲突挤出去。
        marks = reviews(cfg)
        visible: list[dict[str, Any]] = []
        for item in found:
            mark = marks.get(item["key"])
            state = mark.get("status") if isinstance(mark, dict) else None
            if state == "dismissed":
                continue
            item["status"] = "confirmed" if state == "confirmed" else "open"
            visible.append(item)
        found = visible
    return found[: max(1, int(limit))]


def _conflicts_path(cfg: Any = None, out: str | Path | None = None) -> Path:
    if out is None:
        return soul.dir_of(cfg) / CONFLICTS_FILE
    path = Path(out)
    return path / CONFLICTS_FILE if path.is_dir() else path


def scan(
    db: Any,
    *,
    cfg: Any = None,
    out: str | Path | None = None,
) -> dict[str, Any]:
    """跑一次检测并写 ``soul/conflicts.json``；返回 ``{"ok","count","path"}``。

    落盘的是**应用审阅后**的清单：``dismissed`` 不写，``confirmed`` 带 status 保留，
    所以 ``count`` = open + confirmed。写失败会抛 ``OSError``：调用方（CLI）需要
    知道「没写成」，而不是拿到一个看起来成功的空清单。
    """
    items = conflicts(db, cfg=cfg)
    payload = {"generated_at": now_iso(), "count": len(items), "items": items}
    path = _conflicts_path(cfg, out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "count": len(items), "path": str(path)}


def load(cfg: Any = None) -> dict[str, Any]:
    """读回上一次的清单；缺文件/坏 JSON 一律返回空清单，不抛。

    ``wake`` / ``surface`` 会在每次会话开头读它，读失败不能让开机流程变红。
    """
    path = soul.dir_of(cfg) / CONFLICTS_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": [], "count": 0}
    if not isinstance(payload, dict):
        return {"items": [], "count": 0}
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    try:
        count = int(payload.get("count"))
    except (TypeError, ValueError):
        count = len(items)
    payload["items"] = items
    payload["count"] = count
    return payload


__all__ = [
    "CONFLICTS_FILE",
    "REVIEWS_FILE",
    "clauses",
    "conflicts",
    "scan",
    "load",
    "review",
    "reviews",
]
