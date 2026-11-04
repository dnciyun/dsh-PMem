"""关系星云布局与渲染：没有边界，只有记忆细胞和纤维。

旧版试过同心圆、脑横截面、海马体轮廓，都太像"示意图"。这一版不再画形状：

- **背景**：深空渐变 + 星云 + 银河带 + 三档星点 + 暗角；
- **布局**：分组/类型只给一个簇心，记忆在簇心附近散开，碰撞清理避免叠字；
- **连线**：相似/共同提及/同组/关系/证据，全部是弯曲的二次贝塞尔；
- **细胞**：半透明胞体 + 光晕 + 短树突；S/A 有十字星芒；
- **确定性**：同一份库两次生成位置一致；没有随机力导向。

输出：交互 HTML（模板在 ``starmap_html``）、静态 SVG、JSON、可选 PNG。
"""

from __future__ import annotations

import html as _html
import math
import random
from pathlib import Path
from typing import Any

GRADE_COLOR = {
    "S": "#ffd27a",
    "A": "#ffe9c7",
    "B": "#8fe3ff",
    "C": "#b7a6ff",
    "D": "#7c8aa5",
    None: "#9fb4cc",
}
# 和 starmap.py 的 JOURNAL_COLOR 保持一致：离线 SVG/PNG 与 Web 星图同色。
JOURNAL_COLOR = "#e9b872"
KIND_COLOR = {
    "episodic": "#7fc8ff",
    "semantic": "#ffe2a8",
    "person": "#ff9fc4",
    "relation": "#8ff0d0",
    "experience": "#ffc27a",
    "goal": "#9ef0a8",
    "meta": "#c9a7ff",
}
TYPE_COLOR = {
    "person": "#ff9fc4",
    "place": "#9ef0a8",
    "org": "#ffd27a",
    "time": "#b7a6ff",
    "event": "#8fe3ff",
    "knowledge": "#c9a7ff",
    "tool": "#8ff0d0",
    "goal": "#9ef0a8",
    "concept": "#ffe9c7",
    "trait": "#c9a7ff",
    "cognition": "#c9a7ff",
}
GROUP_PALETTE = [
    "#ffd27a", "#8fe3ff", "#b7a6ff", "#8ff0d0", "#ff9fc4",
    "#9ef0a8", "#ffc27a", "#c9a7ff", "#7fc8ff", "#ffe9c7",
]
PREDICATE_COLOR = {
    "likes": "#ff9fc4",
    "dislikes": "#ff8a8a",
    "works_at": "#ffd27a",
    "lives_in": "#9ef0a8",
    "uses": "#8ff0d0",
    "part_of": "#8fe3ff",
    "related_to": "#7c8aa5",
    "works_on": "#b7a6ff",
    "knows": "#ffc27a",
    "located_in": "#8ff0d0",
    "studies_at": "#ffe9c7",
    "born_in": "#ff9fc4",
}

# 海马体不是"人脑横截面"：它是一条弯曲的、像海马的带。
# 控制点只定义大概走向；下面用 Catmull-Rom 插值成平滑脊线，再生成带宽。
HIPPO_CTRL: list[list[float]] = [
    [645, 185], [625, 250], [595, 320], [560, 390], [530, 460],
    [510, 530], [505, 600], [525, 660], [565, 700], [615, 705],
    [645, 675], [635, 640], [595, 625], [560, 645],
]
HIPPO_WIDTH_CTRL: list[float] = [70, 80, 95, 105, 105, 95, 80, 62, 45, 35, 30, 28, 30, 34]


def _catmull_rom(points: list[list[float]], widths: list[float], samples: int = 10) -> tuple[list[list[float]], list[float]]:
    out: list[list[float]] = []
    out_w: list[float] = []
    for i in range(len(points) - 1):
        p0 = points[max(0, i - 1)]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[min(len(points) - 1, i + 2)]
        w1, w2 = widths[i], widths[i + 1]
        for k in range(samples):
            t = k / samples
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append([x, y])
            out_w.append(w1 + (w2 - w1) * t)
    out.append([points[-1][0], points[-1][1]])
    out_w.append(widths[-1])
    return out, out_w


def _ribbon_polygon(spine: list[list[float]], widths: list[float]) -> list[list[float]]:
    left: list[list[float]] = []
    right: list[list[float]] = []
    for i, (x, y) in enumerate(spine):
        before = spine[max(0, i - 1)]
        after = spine[min(len(spine) - 1, i + 1)]
        tx, ty = after[0] - before[0], after[1] - before[1]
        length = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / length, tx / length
        w = widths[i]
        left.append([x + nx * w, y + ny * w])
        right.append([x - nx * w, y - ny * w])
    return left + list(reversed(right))


HIPPO_SPINE, HIPPO_WIDTHS = _catmull_rom(HIPPO_CTRL, HIPPO_WIDTH_CTRL)
HIPPO_BODY = _ribbon_polygon(HIPPO_SPINE, HIPPO_WIDTHS)
HIPPO_HEAD = {"cx": 645, "cy": 185, "r": 70}
HIPPO_ANCHORS = {
    "head": (645, 195),
    "neck": (612, 300),
    "chest": (568, 390),
    "heart": (538, 450),
    "belly": (515, 520),
    "lower": (505, 590),
    "tail": (530, 650),
    "curl": (610, 690),
    "back": (600, 400),
    "dorsal": (560, 560),
}
HIPPO_ANCHOR_ORDER = [
    "head", "neck", "chest", "back", "heart", "belly", "dorsal", "lower", "tail", "curl",
]
SHAPE = {
    "kind": "hippocampus",
    "spine": HIPPO_SPINE,
    "widths": HIPPO_WIDTHS,
    "body": HIPPO_BODY,
    "head": HIPPO_HEAD,
    "anchors": {key: list(value) for key, value in HIPPO_ANCHORS.items()},
    "center": [580, 340],
}


def _hash(value: str) -> int:
    import hashlib

    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:12], 16)


def _node_color(node: dict[str, Any]) -> str:
    if node["type"] == "entity":
        return TYPE_COLOR.get(node.get("entity_type") or "", "#9fb4cc")
    if node["type"] == "group":
        return GROUP_PALETTE[_hash(node["id"]) % len(GROUP_PALETTE)]
    if node["type"] == "trait":
        return "#c9a7ff"
    if node["type"] == "core":
        return "#ffe9c7"
    if node.get("type") == "memory" and node.get("source_kind") == "journal":
        return JOURNAL_COLOR
    if node.get("type") == "memory" and node.get("grade"):
        return GRADE_COLOR.get(node["grade"], "#9fb4cc")
    return KIND_COLOR.get(node.get("kind") or "", "#9fb4cc")


def _point_in_poly(x: float, y: float, points: list[list[float]]) -> bool:
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi, yi = points[i]
        xj, yj = points[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _closest_on_spine(x: float, y: float) -> tuple[float, float, float, float]:
    """返回 (离脊线距离, 该处半宽, 最近点 x, y)。"""
    best_d = 1e9
    best_w = 0.0
    best_x = best_y = 0.0
    for i in range(len(HIPPO_SPINE) - 1):
        ax, ay = HIPPO_SPINE[i]
        bx, by = HIPPO_SPINE[i + 1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy or 1.0
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        px, py = ax + t * dx, ay + t * dy
        dist = math.hypot(x - px, y - py)
        if dist < best_d:
            best_d = dist
            best_w = HIPPO_WIDTHS[i] + (HIPPO_WIDTHS[i + 1] - HIPPO_WIDTHS[i]) * t
            best_x, best_y = px, py
    return best_d, best_w, best_x, best_y


def _constrain(x: float, y: float) -> tuple[float, float]:
    """把节点压回海马体带/头部/吻部，防止身体外的"星"。"""
    d, w, px, py = _closest_on_spine(x, y)
    head_d = math.hypot(x - HIPPO_HEAD["cx"], y - HIPPO_HEAD["cy"])
    if d <= w or head_d <= HIPPO_HEAD["r"]:
        return max(20, min(980, x)), max(20, min(980, y))
    if (d - w) <= (head_d - HIPPO_HEAD["r"]):
        factor = (w * 0.92) / d if d else 0
        nx = px + (x - px) * factor
        ny = py + (y - py) * factor
    else:
        factor = (HIPPO_HEAD["r"] * 0.90) / head_d if head_d else 0
        nx = HIPPO_HEAD["cx"] + (x - HIPPO_HEAD["cx"]) * factor
        ny = HIPPO_HEAD["cy"] + (y - HIPPO_HEAD["cy"]) * factor
    return max(20, min(980, nx)), max(20, min(980, ny))


def _group_anchor(path: str, used: set[str]) -> str:
    if path == "我们" or path.startswith("我们/"):
        order = ["heart", "chest", "belly", "head"]
    elif path == "我" or path.startswith("我/"):
        order = ["head", "neck", "chest"]
    else:
        start = _hash(path + "#body") % len(HIPPO_ANCHOR_ORDER)
        order = HIPPO_ANCHOR_ORDER[start:] + HIPPO_ANCHOR_ORDER[:start]
    for name in order:
        if name not in used:
            used.add(name)
            return name
    return HIPPO_ANCHOR_ORDER[_hash(path) % len(HIPPO_ANCHOR_ORDER)]


def _ungrouped_anchor(node: dict[str, Any]) -> str:
    kind = node.get("kind") or "episodic"
    grade = node.get("grade")
    if grade == "S" or kind == "person":
        choices = ["head", "neck", "chest", "heart"]
    elif kind == "semantic":
        choices = ["neck", "chest", "back", "heart", "head"]
    elif kind == "episodic":
        choices = HIPPO_ANCHOR_ORDER
    elif kind == "relation":
        choices = ["chest", "back", "heart", "dorsal"]
    elif kind == "meta":
        choices = ["head", "neck", "heart"]
    else:
        choices = HIPPO_ANCHOR_ORDER
    return choices[_hash(node["id"] + "#body") % len(choices)]


def _radius(grade: str | None, seed: str) -> float:
    base = {"S": 18, "A": 25, "B": 34, "C": 44, "D": 54, None: 38}.get(grade or "", 38)
    return base + (_hash(seed + "#r") % 24)


def _initial_positions(nodes: list[dict[str, Any]]) -> dict[str, list[float]]:
    """确定性初始位置：只给大致簇，不给形状；后面交给力导向。"""
    pos: dict[str, list[float]] = {}
    groups = [node for node in nodes if node["type"] == "group"]
    groups.sort(key=lambda node: (
        0 if node["path"].startswith("我们") else 1 if node["path"].startswith("我") else 2,
        node["path"],
    ))
    group_pos: dict[str, list[float]] = {}
    we = [g for g in groups if g["path"].startswith("我们")]
    me = [g for g in groups if g["path"].startswith("我") and not g["path"].startswith("我们")]
    others = [g for g in groups if g not in we and g not in me]
    for index, group in enumerate(we):
        pos[group["id"]] = [500 + index * 52, 215 + index * 32]
    for index, group in enumerate(me):
        if index == 0:
            pos[group["id"]] = [282, 420]
        elif index == 1:
            pos[group["id"]] = [720, 470]
        else:
            pos[group["id"]] = [340, 610]
    for index, group in enumerate(others):
        angle = -0.6 + index * (math.tau / max(1, len(others)))
        radius = 330 + (_hash(group["path"] + "#r") % 55)
        pos[group["id"]] = [500 + math.cos(angle) * radius, 470 + math.sin(angle) * radius]
    for group in groups:
        group_pos[group["path"]] = list(pos[group["id"]])

    for node in nodes:
        if node["type"] == "memory":
            paths = sorted(node.get("groups") or [])
            center = group_pos.get(paths[0]) if paths else None
            if center is None:
                # 没有分组的记忆也挂到一个簇中心，避免变成散落孤星
                if groups:
                    group = groups[_hash(node["id"] + "#cluster") % len(groups)]
                    center = list(pos[group["id"]])
                    node["_cluster"] = group["id"]
                else:
                    center = [500, 500]
            else:
                node["_cluster"] = groups[[g["path"] for g in groups].index(paths[0])]["id"]
            angle = ((_hash(node["id"] + "#a") % 3600) / 3600) * math.tau
            radius = 58 + (_hash(node["id"] + "#j") % 128)
            pos[node["id"]] = [center[0] + math.cos(angle) * radius, center[1] + math.sin(angle) * radius]
        elif node["type"] == "entity":
            pos[node["id"]] = [500 + ((_hash(node["id"] + "#x") % 500) - 250),
                               500 + ((_hash(node["id"] + "#y") % 500) - 250)]
        elif node["type"] == "trait":
            angle = ((_hash(node["id"]) % 3600) / 3600) * math.tau
            pos[node["id"]] = [500 + math.cos(angle) * 90, 500 + math.sin(angle) * 90]
        elif node["type"] == "core":
            pos[node["id"]] = [500, 500]
    return pos


def layout(graph: dict[str, Any]) -> dict[str, Any]:
    """手排簇 + 碰撞清理：没有边界、没有力导向塌缩，允许杂乱但有簇。

    分组/类型只决定初始簇心；相似/共提及/关系线负责把跨簇的记忆连起来。
    """
    nodes = graph["nodes"]
    pos = _initial_positions(nodes)
    movable = [node for node in nodes if node["type"] not in ("core", "group")]
    for _ in range(110):
        for i in range(len(movable)):
            a = movable[i]
            ax, ay = pos[a["id"]]
            for j in range(i + 1, len(movable)):
                b = movable[j]
                bx, by = pos[b["id"]]
                dx, dy = ax - bx, ay - by
                dist = math.hypot(dx, dy)
                need = max(10.0, (float(a.get("r") or 3) + float(b.get("r") or 3)) * 2.6 + 6)
                if dist >= need:
                    continue
                if dist < 0.01:
                    angle = ((_hash(a["id"] + b["id"]) % 3600) / 3600) * math.tau
                    dx, dy, dist = math.cos(angle), math.sin(angle), 1.0
                push = (need - dist) / 2
                ux, uy = dx / dist, dy / dist
                pos[a["id"]] = [max(35, min(965, ax + ux * push)), max(35, min(965, ay + uy * push))]
                pos[b["id"]] = [max(35, min(965, bx - ux * push)), max(35, min(965, by - uy * push))]
                ax, ay = pos[a["id"]]

    for node in nodes:
        node["x"], node["y"] = pos[node["id"]]
        if node["type"] == "memory":
            base = 2.0 + float(node.get("importance") or 0.5) * 3.0
            if node.get("grade") == "S":
                base += 1.4
            elif node.get("grade") == "A":
                base += 0.7
            if node.get("status") in ("dormant", "archived"):
                base *= 0.72
            node["r"] = round(base, 2)
        elif node["type"] == "entity":
            node["r"] = round(3.8 + min(6.0, math.sqrt(max(1, node.get("mention_count") or 1)) * 1.6), 2)
        elif node["type"] == "group":
            node["r"] = 4.2
        elif node["type"] == "trait":
            node["r"] = 4.2
        elif node["type"] == "core":
            node["r"] = 12
    graph["shape"] = {"kind": "organic"}
    graph["nodes"] = nodes
    return graph


def _curve(source: dict[str, Any], target: dict[str, Any], link: dict[str, Any]) -> tuple[float, float]:
    """两个节点之间的二次贝塞尔控制点：像神经纤维一样弯，而不是直线。"""
    x1, y1 = float(source["x"]), float(source["y"])
    x2, y2 = float(target["x"]), float(target["y"])
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    direction = 1 if _hash(link["source"] + link["target"] + link["type"]) % 2 else -1
    offset = direction * (18 + (_hash(link["source"] + link["target"]) % 42))
    return ((x1 + x2) / 2 - dy / length * offset, (y1 + y2) / 2 + dx / length * offset)


def _link_style(link: dict[str, Any], target: dict[str, Any]) -> tuple[str, float, float]:
    kind = link["type"]
    if kind == "relation":
        return PREDICATE_COLOR.get(link.get("predicate") or "", "#9fb4cc"), 0.8 + float(link.get("weight") or 0.3) * 1.8, 0.45
    if kind == "similar":
        return "#7fd7ff", 0.7 + float(link.get("weight") or 0.3) * 1.6, 0.30
    if kind == "co_mention":
        return "#8ff0d0", 0.7, 0.24
    if kind == "mention":
        return "#8fe3ff", 0.6, 0.16
    if kind == "group_chain":
        return "#b7a6ff", 0.5, 0.14
    if kind == "group":
        return _node_color(target), 0.7, 0.10
    if kind == "evidence":
        return "#c9a7ff", 0.6, 0.16
    return "#9fb4cc", 0.6, 0.14


def render_svg(graph: dict[str, Any]) -> str:
    """静态 SVG：脑轮廓 + 弯曲神经纤维 + 半透明细胞。"""
    nodes = graph["nodes"]
    by_id = {node["id"]: node for node in nodes}
    meta = graph["meta"]
    parts: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000" width="100%" height="100%" '
        'style="background:#05070f" role="img" aria-label="PMem 海马体星图">',
        "<defs>",
        '<radialGradient id="bg" cx="50%" cy="46%" r="72%">'
        '<stop offset="0%" stop-color="#0e1830"/><stop offset="55%" stop-color="#070c18"/>'
        '<stop offset="100%" stop-color="#03050c"/></radialGradient>',
        '<radialGradient id="body" cx="50%" cy="44%" r="64%">'
        '<stop offset="0%" stop-color="#22345a" stop-opacity="0.42"/>'
        '<stop offset="70%" stop-color="#111d36" stop-opacity="0.32"/>'
        '<stop offset="100%" stop-color="#070c18" stop-opacity="0.16"/></radialGradient>',
        '<filter id="glow" x="-90%" y="-90%" width="280%" height="280%">'
        '<feGaussianBlur stdDeviation="7" result="b"/><feMerge><feMergeNode in="b"/>'
        '<feMergeNode in="SourceGraphic"/></feMerge></filter>',
        '<filter id="soft" x="-60%" y="-60%" width="220%" height="220%">'
        '<feGaussianBlur stdDeviation="16"/></filter>',
        "</defs>",
        '<rect width="1000" height="1000" fill="url(#bg)"/>',
    ]
    rng = random.Random(31)
    for _ in range(240):
        x, y = rng.uniform(0, 1000), rng.uniform(0, 1000)
        r = rng.choice([0.4, 0.6, 0.8, 1.0])
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="#cfe3ff" '
            f'opacity="{rng.uniform(0.05, 0.26):.2f}"/>'
        )

    # 连线：二次曲线
    parts.append('<g fill="none" stroke-linecap="round">')
    for link in graph["links"]:
        source, target = by_id.get(link["source"]), by_id.get(link["target"])
        if source is None or target is None:
            continue
        color, width, opacity = _link_style(link, target)
        cx, cy = _curve(source, target, link)
        parts.append(
            f'<path d="M {source["x"]:.2f} {source["y"]:.2f} Q {cx:.2f} {cy:.2f} '
            f'{target["x"]:.2f} {target["y"]:.2f}" stroke="{color}" stroke-width="{width:.2f}" '
            f'opacity="{opacity:.2f}"/>'
        )
    parts.append("</g>")

    for node in nodes:
        kind = node["type"]
        color = _node_color(node)
        x, y, r = float(node["x"]), float(node["y"]), float(node.get("r") or 3)
        if kind == "group":
            parts.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.2" fill="none" stroke="{color}" stroke-opacity="0.65"/>'
            )
            if not (node.get("path") == "我"):
                parts.append(
                    f'<text x="{x:.2f}" y="{y - 10:.2f}" text-anchor="middle" fill="{color}" '
                    f'font-size="12" opacity="0.75">{_html.escape(node["label"])}</text>'
                )
            continue
        if kind == "core":
            parts.append(
                f'<circle cx="500" cy="460" r="64" fill="#ffd27a" opacity="0.08" filter="url(#soft)"/>'
                f'<circle cx="500" cy="460" r="26" fill="#ffd27a" opacity="0.16" filter="url(#glow)"/>'
                f'<circle cx="500" cy="460" r="9" fill="#fff4d6" opacity="0.9"/>'
                f'<text x="500" y="510" text-anchor="middle" fill="#ffe9c7" font-size="19" '
                f'letter-spacing="3">{_html.escape(node["label"])}</text>'
            )
            continue
        if kind == "entity":
            parts.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r * 2.6:.2f}" fill="{color}" opacity="0.10" filter="url(#soft)"/>'
                f'<polygon points="{x:.2f},{y - r:.2f} {x + r * 1.15:.2f},{y:.2f} '
                f'{x:.2f},{y + r:.2f} {x - r * 1.15:.2f},{y:.2f}" fill="{color}" fill-opacity="0.55" '
                f'stroke="{color}" stroke-opacity="0.9"/>'
                f'<text x="{x:.2f}" y="{y - r - 7:.2f}" text-anchor="middle" fill="{color}" '
                f'font-size="12" opacity="0.9">{_html.escape(node["label"])}</text>'
            )
            continue
        if kind == "trait":
            parts.append(
                f'<polygon points="{x:.2f},{y - r:.2f} {x + r * 0.9:.2f},{y - r * 0.4:.2f} '
                f'{x + r * 0.9:.2f},{y + r * 0.4:.2f} {x:.2f},{y + r:.2f} '
                f'{x - r * 0.9:.2f},{y + r * 0.4:.2f} {x - r * 0.9:.2f},{y - r * 0.4:.2f}" '
                f'fill="{color}" fill-opacity="0.5" stroke="{color}" stroke-opacity="0.85"/>'
            )
            continue
        opacity = 0.28 if node.get("status") in ("dormant", "archived") else 0.62
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r * 3.4:.2f}" fill="{color}" opacity="{opacity * 0.16:.2f}" filter="url(#soft)"/>'
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r * 1.7:.2f}" fill="{color}" opacity="{opacity * 0.25:.2f}" filter="url(#glow)"/>'
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" fill="{color}" fill-opacity="{opacity:.2f}" '
            f'stroke="{color}" stroke-opacity="0.9">'
            f'<title>{_html.escape(node.get("text") or node.get("label") or "")}</title></circle>'
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{max(0.8, r * 0.38):.2f}" fill="#ffffff" opacity="{opacity * 0.75:.2f}"/>'
        )
    parts.append(
        f'<text x="28" y="34" fill="#e8f1ff" font-size="20" letter-spacing="2">'
        f'{_html.escape(meta["title"])}</text>'
        f'<text x="28" y="56" fill="#7f98ba" font-size="11">'
        f'{_html.escape(meta["subtitle"])}</text>'
        f'<text x="28" y="976" fill="#4d6485" font-size="10">'
        f'{meta["counts"]["memories"]} 个记忆细胞 · {meta["counts"]["entities"]} 个实体 · '
        f'{len(graph["links"])} 条连接 · {meta["generated_at"][:19]}Z</text>'
        f'<text x="972" y="976" text-anchor="end" fill="#4d6485" font-size="10">'
        f'每条线是相似 / 共同提及 / 关系，不是因果</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _quad(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float], n: int = 14) -> list[tuple[float, float]]:
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((u * u * p1[0] + 2 * u * t * p2[0] + t * t * p3[0],
                    u * u * p1[1] + 2 * u * t * p2[1] + t * t * p3[1]))
    return out


def _cubic(p0: tuple[float, float], c1: tuple[float, float], c2: tuple[float, float],
           p3: tuple[float, float], n: int = 18) -> list[tuple[float, float]]:
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((
            u ** 3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * p3[1],
        ))
    return out


def render_png(graph: dict[str, Any], path: str | Path, *, size: int = 1400) -> dict[str, Any]:
    """可选静态 PNG：脑轮廓 + 半透明细胞 + 弯曲连接。"""
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"Pillow 不可用：{exc}"}

    nodes = graph["nodes"]
    by_id = {node["id"]: node for node in nodes}
    meta = graph["meta"]
    scale = (size * 0.86) / 1000.0
    cx = cy = size / 2
    ox, oy = cx - 500 * scale, cy - 500 * scale

    def p(x: float, y: float) -> tuple[float, float]:
        return ox + x * scale, oy + y * scale

    def pos(node: dict[str, Any]) -> tuple[float, float]:
        return p(float(node["x"]), float(node["y"]))

    def color(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    # 深空底色：不是纯黑，是带一点蓝紫的暗场
    image = Image.new("RGB", (size, size), (4, 6, 14))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        t = y / size
        draw.line([(0, y), (size, y)], fill=(
            int(4 + 7 * (1 - abs(t - 0.45) * 1.8)),
            int(7 + 11 * (1 - abs(t - 0.45) * 1.8)),
            int(16 + 22 * (1 - abs(t - 0.45) * 1.8)),
        ))

    # 星云：几团低饱和的蓝/紫/青，模糊后像远处的气体
    nebula = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    nd = ImageDraw.Draw(nebula)
    clouds = [
        (0.34, 0.38, 0.34, 0.26, (38, 76, 150), 58),
        (0.62, 0.56, 0.40, 0.30, (78, 48, 128), 42),
        (0.52, 0.28, 0.25, 0.38, (20, 92, 122), 34),
        (0.72, 0.24, 0.20, 0.18, (150, 86, 86), 22),
    ]
    for cxr, cyr, rxr, ryr, c, a in clouds:
        nd.ellipse(
            ((cxr - rxr) * size, (cyr - ryr) * size, (cxr + rxr) * size, (cyr + ryr) * size),
            fill=c + (a,),
        )
    nebula = nebula.filter(ImageFilter.GaussianBlur(78))
    image = Image.alpha_composite(image.convert("RGBA"), nebula)

    # 银河带：一条很淡的斜向亮带，增加纵深感
    band = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.line([(0, size * 0.72), (size, size * 0.22)], fill=(100, 132, 205, 16), width=int(size * 0.20))
    band = band.filter(ImageFilter.GaussianBlur(80))
    image = Image.alpha_composite(image, band)

    # 三层星：尘埃、普通、亮星；亮星带十字星芒
    rng = random.Random(41)
    stars = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    star_glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sg = ImageDraw.Draw(star_glow)
    palette = [(190, 214, 255), (230, 240, 255), (255, 240, 216), (170, 204, 255)]
    for _ in range(1150):
        x, y = rng.uniform(0, size), rng.uniform(0, size)
        roll = rng.random()
        if roll < 0.72:
            r, a = rng.uniform(0.35, 0.9), rng.randint(18, 70)
        elif roll < 0.95:
            r, a = rng.uniform(0.9, 1.7), rng.randint(50, 130)
        else:
            r, a = rng.uniform(1.8, 3.0), rng.randint(120, 210)
        c = rng.choice(palette)
        sd.ellipse((x - r, y - r, x + r, y + r), fill=c + (a,))
        if r > 1.7:
            sg.ellipse((x - r * 5, y - r * 5, x + r * 5, y + r * 5), fill=c + (28,))
            sd.line((x - r * 6, y, x + r * 6, y), fill=c + (int(a * 0.22),), width=1)
            sd.line((x, y - r * 6, x, y + r * 6), fill=c + (int(a * 0.22),), width=1)
    star_glow = star_glow.filter(ImageFilter.GaussianBlur(7))
    image = Image.alpha_composite(image, star_glow)
    image = Image.alpha_composite(image, stars)

    # 暗角：让视线回到中心
    vignette = Image.radial_gradient("L").resize((size, size)).point(lambda v: int(v * 0.55))
    image = Image.composite(Image.new("RGB", (size, size), (0, 0, 0)), image.convert("RGB"), vignette).convert("RGBA")

    # 连线（弯曲）
    links_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(links_layer)
    for link in graph["links"]:
        source, target = by_id.get(link["source"]), by_id.get(link["target"])
        if source is None or target is None:
            continue
        c1, c2 = pos(source), pos(target)
        ctrl = _curve(source, target, link)
        ctrl = p(*ctrl)
        c = color(_link_style(link, target)[0])
        alpha = int(255 * min(0.34, _link_style(link, target)[2] * 0.95))
        ld.line(_quad(c1, ctrl, c2), fill=c + (alpha,), width=2 if link["type"] in ("relation", "similar") else 1)
    link_glow = links_layer.filter(ImageFilter.GaussianBlur(5))
    image = Image.alpha_composite(image, link_glow)
    image = Image.alpha_composite(image, links_layer)

    # 光晕 + 半透明细胞
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cells = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    cd = ImageDraw.Draw(cells)
    for node in nodes:
        x, y = pos(node)
        c = color(_node_color(node))
        typ = node["type"]
        if typ == "group":
            r = 4
            cd.ellipse((x - r, y - r, x + r, y + r), outline=c + (180,), width=1)
        elif typ == "core":
            r = 32 * scale
            gd.ellipse((x - r, y - r, x + r, y + r), fill=(255, 210, 122, 22))
            cd.ellipse((x - 14 * scale, y - 14 * scale, x + 14 * scale, y + 14 * scale), fill=(255, 210, 122, 42))
            cd.ellipse((x - 7 * scale, y - 7 * scale, x + 7 * scale, y + 7 * scale), fill=(255, 244, 214, 230))
        elif typ == "entity":
            r = max(5, float(node.get("r") or 4) * 1.7)
            gd.ellipse((x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2), fill=c + (50,))
            cd.polygon([(x, y - r), (x + r * 1.15, y), (x, y + r), (x - r * 1.15, y)], fill=c + (170,), outline=c + (240,))
        elif typ == "trait":
            r = 6
            cd.regular_polygon((x, y, r), n_sides=6, fill=c + (150,), outline=c + (230,))
        else:
            r = max(2.4, float(node.get("r") or 3) * 2.0)
            alpha = 90 if node.get("status") in ("dormant", "archived") else 180
            gd.ellipse((x - r * 4.6, y - r * 4.6, x + r * 4.6, y + r * 4.6), fill=c + (44,))
            cd.ellipse((x - r * 2.4, y - r * 2.4, x + r * 2.4, y + r * 2.4), fill=c + (28,))
            cd.ellipse((x - r, y - r, x + r, y + r), fill=c + (alpha,), outline=c + (245,), width=1)
            cd.ellipse((x - max(1.2, r * 0.34), y - max(1.2, r * 0.34),
                        x + max(1.2, r * 0.34), y + max(1.2, r * 0.34)), fill=(255, 255, 255, 215))
            if node.get("grade") in ("S", "A"):
                cd.line((x - r * 8, y, x + r * 8, y), fill=c + (50,), width=1)
                cd.line((x, y - r * 8, x, y + r * 8), fill=c + (50,), width=1)
    glow = glow.filter(ImageFilter.GaussianBlur(13))
    image = Image.alpha_composite(image, glow)
    image = Image.alpha_composite(image, cells)
    draw = ImageDraw.Draw(image)

    fonts = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf"]

    def font(px: int):
        for candidate in fonts:
            try:
                return ImageFont.truetype(candidate, px)
            except OSError:
                continue
        return ImageFont.load_default()

    f_title, f_sub, f_label, f_small = font(38), font(15), font(15), font(12)
    draw.text((32, 28), meta["title"], font=f_title, fill=(232, 241, 255))
    draw.text((32, 76), meta["subtitle"], font=f_sub, fill=(127, 152, 186))
    draw.text(
        (32, size - 40),
        f'{meta["counts"]["memories"]} 个记忆细胞 · {meta["counts"]["entities"]} 个实体 · '
        f'{len(graph["links"])} 条连接 · 质量分 {meta["quality"] if meta["quality"] is not None else "—"}',
        font=f_small, fill=(100, 126, 164),
    )
    draw.text((size - 32, size - 40), "每条线不是因果，是相似 / 共同提及 / 关系",
              font=f_small, fill=(100, 126, 164), anchor="ra")
    for node in nodes:
        x, y = pos(node)
        if node["type"] == "group":
            if node.get("path") != "我":
                draw.text((x, y - 12), node["label"], font=f_label, fill=_node_color(node), anchor="mm",
                          stroke_width=2, stroke_fill=(4, 6, 14))
        elif node["type"] == "entity":
            draw.text((x, y - 15), node["label"], font=f_small, fill=_node_color(node), anchor="mm",
                      stroke_width=2, stroke_fill=(4, 6, 14))
    draw.text((cx, cy + 128), meta["subject"], font=font(20), fill=(255, 233, 199), anchor="mm",
              stroke_width=2, stroke_fill=(4, 6, 14))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path, "PNG", optimize=True)
    return {"ok": True, "path": str(path), "size_bytes": path.stat().st_size}


__all__ = ["SHAPE", "layout", "render_svg", "render_png", "HIPPO_ANCHORS"]
