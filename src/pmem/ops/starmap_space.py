"""星图的深空布局（M9.7b）。

M9 的旧布局是「脑形」：把节点塞进一个海马体轮廓里。好看，但形状是**外面给**的，
记忆一多就挤成一张关系图。这一版换掉：

- **半径 = 时间**。早的在中心，新的在外，盘面自己从中心长出来；
- **角度 = 分组**。每个分组占一条螺旋臂，同一组的记忆沿臂排开（`我们` 在正上方）；
- **高度 = 盘厚**。盘不是一张纸，分组之间错落，整体有一个很轻的翘曲；
- **局部松弛**。相似 / 共同提及的记忆互相靠一点，但不许破坏臂的骨架。

坐标约定：盘面在 XZ 平面，Y 是盘厚（上为正）。世界坐标归一化到半径 1，
再投影到 1000×1000 的 2D 框给 SVG / PNG 用（俯角 ``PITCH``）。

确定性：所有抖动都来自 sha256，同一条记忆永远不会跳位置。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

# ---- 几何常量 -------------------------------------------------------------

R_IN = 0.055          # 最内圈半径：留出中心那团光核
R_OUT = 1.0           # 最外圈半径
TWIST = 1.15          # 每跑到外圈一次，臂扭过的弧度
THICK = 0.055         # 半盘厚
PITCH = 0.56          # 2D 投影俯角（弧度，约 32°）
BOX = 1000.0          # SVG / PNG 的画布边长
SPAN = 430.0          # 世界半径 1 → 画布半径

# 2D 投影时把盘厚放大，否则 SVG 里完全看不出立体。
_FLATTEN = 4.2

_GRADE_PULL = {"S": 0.60, "A": 0.75, "B": 0.90, "C": 1.0, "D": 1.08, None: 1.0}
_GRADE_SIZE = {"S": 5.4, "A": 4.5, "B": 3.5, "C": 2.7, "D": 2.0, None: 3.0}
_GRADE_SPIKE = {"S": 1.0, "A": 0.68, "B": 0.16, "C": 0.06, "D": 0.0, None: 0.10}


def _h(*parts: Any) -> int:
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


def _hf(*parts: Any) -> float:
    """确定性的 [0,1)。"""
    return _h(*parts) / float(0xFFFFFFFFFFFF)


def _jitter(*parts: Any, amp: float) -> float:
    return (_hf(*parts) - 0.5) * 2.0 * amp


def _ts(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _norm(values: Iterable[float | None]) -> tuple[float, float]:
    real = [v for v in values if v is not None]
    if not real:
        return 0.0, 1.0
    lo, hi = min(real), max(real)
    if hi - lo < 1e-6:
        return lo, lo + 1.0
    return lo, hi


# ---- 分组扇区 -------------------------------------------------------------


def _sector_map(paths: list[str]) -> dict[str, float]:
    """把分组排到圆周上。``我们/*`` 从正上方（-π/2）开始，其余顺时针铺开。

    顺序只用路径本身决定，所以加一个分组不会让所有星星重排——它只会占掉
    新的一段弧。
    """
    def key(path: str) -> tuple[int, str]:
        return (0 if path == "我们" or path.startswith("我们/") else 1, path)

    ordered = sorted(set(paths), key=key)
    total = len(ordered)
    out: dict[str, float] = {}
    for index, path in enumerate(ordered):
        base = -math.pi / 2.0 + (index / total) * math.tau if total else 0.0
        out[path] = base + _jitter(path, "sector", amp=0.06)
    return out


# ---- 主布局 ---------------------------------------------------------------


def layout(graph: dict[str, Any], prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """给 ``collect`` 的图加上 3D 坐标、尺寸、星尘和时间轴。

    ``prev`` 是上一次生成的星图（M9.7c 增量模式）。给了它，**已经在盘上的
    记忆一律不动**：位置、高度、时间点全部沿用上一次的值，新记忆自己找位置。
    页面每隔一两秒就可能重画一次，谁也不希望星星跳来跳去。
    """
    nodes: list[dict[str, Any]] = graph.get("nodes") or []
    links: list[dict[str, Any]] = graph.get("links") or []
    meta = graph.setdefault("meta", {})

    memories = [n for n in nodes if n.get("type") == "memory"]
    times = {n["id"]: _ts(n.get("created_at")) for n in memories}
    lo, hi = _norm(times.values())

    # 半径不完全按"绝对时间距离"：真实写作是突发的（一晚上写 48 条），
    # 直接映射会把 2/3 的记忆挤进最内圈，盘面空掉一大半。改用**时间秩序**
    # （第几个记忆就落在第几环），再掺三成真实时间。时间依然单调——内圈更早——
    # 但密度是均匀的，盘面才立得起来。
    ordered = sorted(
        memories,
        key=lambda n: (times.get(n["id"]) is None, times.get(n["id"]) or 0.0, n.get("seq") or 0),
    )
    rank = {node["id"]: index for index, node in enumerate(ordered)}
    total = max(1, len(ordered) - 1)

    paths = sorted({p for n in memories for p in (n.get("groups") or [])})
    sectors = _sector_map(paths)
    ungrouped = [n for n in memories if not (n.get("groups") or [])]

    # --- 记忆节点：半径 = 时间，角度 = 分组 --------------------------------
    for node in memories:
        stamp = times.get(node["id"])
        if stamp is None:
            # 没有时间就按 seq 在盘上均匀铺开，避免全叠在中心。
            seq = float(node.get("seq") or 0)
            t = min(1.0, max(0.0, (seq % 997) / 997.0))
        else:
            t = (stamp - lo) / (hi - lo)
        t = min(1.0, max(0.0, t))

        position = 0.70 * (rank.get(node["id"], 0) / total) + 0.30 * t
        radius = R_IN + (position ** 0.92) * (R_OUT - R_IN)
        radius *= _GRADE_PULL.get(node.get("grade"), 1.0)
        # 休眠 / 归档的往外飘一点，像已经冷却的星。
        if node.get("status") in ("dormant", "archived"):
            radius *= 1.06
        radius = min(R_OUT * 1.04, max(R_IN * 0.85, radius))

        groups = node.get("groups") or []
        if groups:
            # 一条记忆可以属于多个分组：取第一个有扇区的，其余只当轻微拉扯。
            anchor = next((g for g in groups if g in sectors), None)
            angle = sectors.get(anchor, _hf(node["id"], "ang") * math.tau)
        else:
            angle = _hf(node["id"], "ang") * math.tau
        angle += TWIST * (radius / R_OUT)
        angle += _jitter(node["id"], "spread", amp=0.075)

        node["_u"] = radius * math.cos(angle)
        node["_v"] = radius * math.sin(angle)
        node["_h"] = (
            THICK * math.sin(angle * 2.0 + _hf(node["id"], "warp") * 0.7) * (0.35 + 0.65 * radius)
            + _jitter(node["id"], "thick", amp=THICK * 0.55)
        )
        node["_t"] = round(t, 5)
        node["_sector"] = anchor if groups else ""

    # --- 增量模式：已经在盘上的，位置就此钉死 ------------------------------
    locked: dict[str, dict[str, Any]] = {}
    if prev:
        for old in prev.get("nodes") or []:
            if not all(isinstance(old.get(k), (int, float)) for k in ("x3", "y3", "z3")):
                continue
            locked[str(old.get("id"))] = old
    for node in memories:
        old = locked.get(node["id"])
        if old is None:
            continue
        node["_u"] = float(old["x3"])
        node["_v"] = float(old["z3"])
        node["_h"] = float(old["y3"])
        if isinstance(old.get("t"), (int, float)):
            node["_t"] = float(old["t"])
        node["_locked"] = True

    # --- 反重叠：太挤的地方推开，但不动骨架 --------------------------------
    _relax([n for n in memories], min_d=0.021, rounds=5)

    # --- 相似 / 共同提及：拉近一点，让关系可见 ----------------------------
    by_id = {n["id"]: n for n in memories}
    for _ in range(3):
        for link in links:
            if link.get("type") not in ("similar", "co_mention", "group_chain"):
                continue
            a, b = by_id.get(link.get("source")), by_id.get(link.get("target"))
            if a is None or b is None:
                continue
            # 增量模式下被钉住的一端不让位，全由另一端让开。
            wa = 0.0 if a.get("_locked") else 1.0
            wb = 0.0 if b.get("_locked") else 1.0
            if wa + wb == 0.0:
                continue
            k = 0.11 * float(link.get("weight") or 0.3) / (wa + wb)
            du = (b["_u"] - a["_u"]) * k
            dv = (b["_v"] - a["_v"]) * k
            a["_u"] += du * wa
            a["_v"] += dv * wa
            b["_u"] -= du * wb
            b["_v"] -= dv * wb
        for node in memories:
            _clamp(node, R_OUT * 1.05)
    _relax(memories, min_d=0.019, rounds=2)

    # --- 分组节点：站在自己那条臂的外端 ------------------------------------
    group_nodes = [n for n in nodes if n.get("type") == "group"]
    members_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in memories:
        for path in node.get("groups") or []:
            members_by_group[path].append(node)
    for node in group_nodes:
        path = node.get("path") or node.get("label") or ""
        members = members_by_group.get(path) or []
        if members:
            u = sum(m["_u"] for m in members) / len(members)
            v = sum(m["_v"] for m in members) / len(members)
            length = math.hypot(u, v) or 1.0
            # 稍微推到外面一点，标签不会压在记忆上。
            node["_u"] = u / length * min(R_OUT * 1.02, max(0.18, length + 0.05))
            node["_v"] = v / length * min(R_OUT * 1.02, max(0.18, length + 0.05))
            node["_h"] = THICK * 1.9
        else:
            sector = sectors.get(path, _hf(path, "ang") * math.tau)
            node["_u"] = R_OUT * 0.92 * math.cos(sector + TWIST)
            node["_v"] = R_OUT * 0.92 * math.sin(sector + TWIST)
            node["_h"] = THICK * 1.9
        node["_t"] = 1.0
        node["_sector"] = path

    # --- 实体：悬在提及它的记忆上方 ---------------------------------------
    mentions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        if link.get("type") not in ("mention", "co_mention"):
            continue
        source, target = link.get("source") or "", link.get("target") or ""
        if source.startswith("ent:") and target.startswith("mem:"):
            entity_id, memory_id = source, target[4:]
        elif target.startswith("ent:") and source.startswith("mem:"):
            entity_id, memory_id = target, source[4:]
        else:
            continue
        memory = by_id.get(memory_id)
        if memory is not None:
            mentions[entity_id].append(memory)
    for node in nodes:
        if node.get("type") != "entity":
            continue
        sources = mentions.get(node["id"]) or []
        if sources:
            u = sum(m["_u"] for m in sources) / len(sources)
            v = sum(m["_v"] for m in sources) / len(sources)
        else:
            u = _jitter(node["id"], "eu", amp=0.75)
            v = _jitter(node["id"], "ev", amp=0.75)
        node["_u"] = u
        node["_v"] = v
        node["_h"] = THICK * 3.4 + _jitter(node["id"], "eh", amp=THICK)
        node["_t"] = 1.0
        node["_sector"] = ""

    # --- 特质：贴着证据记忆 -----------------------------------------------
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        if link.get("type") != "evidence":
            continue
        source, target = link.get("source") or "", link.get("target") or ""
        if source.startswith("trait:") and target.startswith("mem:"):
            memory = by_id.get(target[4:])
            if memory is not None:
                evidence[source].append(memory)
    for node in nodes:
        if node.get("type") != "trait":
            continue
        sources = evidence.get(node["id"]) or []
        if sources:
            node["_u"] = sum(m["_u"] for m in sources) / len(sources)
            node["_v"] = sum(m["_v"] for m in sources) / len(sources)
        else:
            node["_u"] = _jitter(node["id"], "tu", amp=0.5)
            node["_v"] = _jitter(node["id"], "tv", amp=0.5)
        node["_h"] = -THICK * 2.6
        node["_t"] = 1.0
        node["_sector"] = ""

    # 分组 / 实体 / 特质也沿用上一次的位置：它们本来就是跟着成员走的，
    # 成员不动，它们也没有理由动；锁住只是为了让两次输出逐字节一致。
    for node in nodes:
        if node.get("type") in ("memory", "core"):
            continue
        old = locked.get(str(node.get("id")))
        if old is None:
            continue
        node["_u"] = float(old["x3"])
        node["_v"] = float(old["z3"])
        if isinstance(old.get("t"), (int, float)):
            node["_t"] = float(old["t"])

    # --- 中心：不再是「我」，只是一团很淡的光核 ----------------------------
    for node in nodes:
        if node.get("type") == "core":
            node["_u"] = 0.0
            node["_v"] = 0.0
            node["_h"] = 0.0
            node["_t"] = 0.0
            node["label"] = ""
            node["sub"] = ""
            node["hidden"] = True

    # --- 尺寸 / 星芒 / 世界坐标 -------------------------------------------
    for node in nodes:
        kind = node.get("type")
        if kind == "memory":
            grade = node.get("grade")
            size = _GRADE_SIZE.get(grade, 3.0)
            size *= 0.86 + 0.34 * float(node.get("importance") or 0.5)
            node["size"] = round(size, 3)
            node["spike"] = round(_GRADE_SPIKE.get(grade, 0.10), 3)
            node["glow"] = round(size * (2.6 if grade in ("S", "A") else 2.0), 3)
        elif kind == "entity":
            base = 3.4 + min(2.6, math.sqrt(max(1, node.get("mention_count") or 1)) * 0.95)
            node["size"] = round(base, 3)
            node["spike"] = 0.22
            node["glow"] = round(base * 2.1, 3)
        elif kind == "group":
            node["size"] = 3.0
            node["spike"] = 0.0
            node["glow"] = 5.2
        elif kind == "trait":
            node["size"] = 3.2
            node["spike"] = 0.3
            node["glow"] = 6.0
        else:  # core
            node["size"] = 3.2
            node["spike"] = 0.0
            node["glow"] = 5.5
        node["r"] = node["size"]
        node["x3"] = round(node["_u"], 6)
        node["z3"] = round(node["_v"], 6)
        node["y3"] = round(node["_h"], 6)
        # 2D 投影：给 SVG / PNG 的老渲染器用，它们只认 x / y / r。
        node["x"] = round(BOX / 2 + node["_u"] * SPAN, 2)
        node["y"] = round(
            BOX / 2
            + (node["_v"] * math.cos(PITCH) - node["_h"] * _FLATTEN * math.sin(PITCH)) * SPAN,
            2,
        )

    for node in nodes:
        node.pop("_u", None)
        node.pop("_v", None)
        node.pop("_h", None)
        node.pop("_locked", None)
        node["t"] = node.pop("_t", 1.0)
        node["arm"] = node.pop("_sector", "")

    # --- 星尘：事件 / 召回 / 指标 / 反思 / 睡眠 ----------------------------
    dust = graph.get("dust") or []
    prev_dust: dict[str, dict[str, Any]] = {}
    if prev:
        for item in prev.get("dust") or []:
            if isinstance(item.get("x3"), (int, float)):
                prev_dust[str(item.get("id"))] = item
    _place_dust(dust, nodes, graph, prev_dust)

    # --- 时间轴 ------------------------------------------------------------
    history = graph.get("history") or []
    days = [row["day"] for row in history if row.get("day")]
    meta["timeline"] = {
        "start": min(days) if days else None,
        "end": max(days) if days else None,
        "days": len(days),
        "dust": len(dust),
    }

    graph["space"] = {
        "r_in": R_IN,
        "r_out": R_OUT,
        "twist": TWIST,
        "thick": THICK,
        "pitch": PITCH,
        "sectors": len(sectors),
    }
    kept = sum(1 for node in memories if node.get("id") in locked)
    if prev:
        meta["incremental"] = {
            "locked": kept,
            "fresh": len(memories) - kept,
            "reused_dust": sum(1 for item in dust if str(item.get("id")) in prev_dust),
        }
    else:
        meta["incremental"] = {"locked": 0, "fresh": len(memories), "reused_dust": 0}

    graph["shape"] = {
        "kind": "organic",
        "layout": "spiral-disk",
        "arms": len(sectors),
        "twist": TWIST,
        "incremental": bool(prev),
    }
    return graph


def _clamp(node: dict[str, Any], limit: float) -> None:
    u, v = node["_u"], node["_v"]
    length = math.hypot(u, v)
    if length > limit and length > 0:
        node["_u"] = u / length * limit
        node["_v"] = v / length * limit


def _relax(items: list[dict[str, Any]], *, min_d: float, rounds: int = 4) -> None:
    """网格哈希反重叠。O(n) 一轮，3000 个点也不卡。

    带 ``_locked`` 的点只当障碍，不被推动——增量模式靠这一条保证旧星不动。
    """
    if len(items) < 2:
        return
    cell = min_d
    for _ in range(rounds):
        grid: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            grid[(int(item["_u"] // cell), int(item["_v"] // cell))].append(item)
        moved = 0
        for item in items:
            if item.get("_locked"):
                continue
            cx, cy = int(item["_u"] // cell), int(item["_v"] // cell)
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for other in grid.get((gx, gy), ()):
                        if other is item:
                            continue
                        du = item["_u"] - other["_u"]
                        dv = item["_v"] - other["_v"]
                        d2 = du * du + dv * dv
                        if d2 >= min_d * min_d:
                            continue
                        if d2 < 1e-12:
                            angle = _hf(item["id"], other["id"]) * math.tau
                            du, dv, d = math.cos(angle), math.sin(angle), 1e-6
                        else:
                            d = math.sqrt(d2)
                        push = (min_d - d) * (1.0 if other.get("_locked") else 0.5)
                        item["_u"] += du / d * push
                        item["_v"] += dv / d * push
                        moved += 1
        if moved <= len(items) * 0.04:
            break


def _place_dust(dust: list[dict[str, Any]], nodes: list[dict[str, Any]],
                graph: dict[str, Any],
                locked: dict[str, dict[str, Any]] | None = None) -> None:
    """把每一条日志放到盘上：贴着它触碰的记忆，或者按时间落在外圈。

    增量模式下，上一次已经在盘上的星尘原样保留——一万多个粒子全部重算
    没有任何收益，只会让画面抖一下。
    """
    if not dust:
        return
    anchors = {}
    for node in nodes:
        if node.get("type") in ("memory", "entity"):
            anchors[node["id"]] = (node["x3"], node["y3"], node["z3"])

    times = [_ts(item.get("t")) for item in dust]
    lo, hi = _norm(times)

    for index, item in enumerate(dust):
        old = (locked or {}).get(str(item.get("id")))
        if old is not None:
            item["x3"] = old["x3"]
            item["y3"] = old["y3"]
            item["z3"] = old["z3"]
            continue
        anchor = None
        target = item.get("m")
        if target:
            key = target if target.startswith(("mem:", "ent:")) else f"mem:{target}"
            anchor = anchors.get(key)
        if anchor is None:
            stamp = times[index]
            t = (stamp - lo) / (hi - lo) if stamp is not None else 0.5
            radius = R_IN + (min(1.0, max(0.0, t)) ** 0.72) * (R_OUT - R_IN)
            radius *= 0.55 + 0.5 * _hf(item.get("id") or index, "dr")
            angle = _hf(item.get("id") or index, "da") * math.tau
            item["x3"] = round(radius * math.cos(angle), 6)
            item["z3"] = round(radius * math.sin(angle), 6)
            item["y3"] = round(_jitter(item.get("id") or index, "dh", amp=THICK * 2.4), 6)
            continue
        spread = 0.014 + 0.020 * _hf(item.get("id") or index, "ds")
        item["x3"] = round(anchor[0] + _jitter(item.get("id") or index, "dx", amp=spread), 6)
        item["y3"] = round(anchor[1] + _jitter(item.get("id") or index, "dy", amp=spread * 1.6), 6)
        item["z3"] = round(anchor[2] + _jitter(item.get("id") or index, "dz", amp=spread), 6)


__all__ = ["layout", "R_IN", "R_OUT", "TWIST", "THICK", "PITCH"]
