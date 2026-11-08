"""常驻三栏笔记（1.12.0，**默认关**）：关于你 / 关于我 / 还没了的事 + 人物卡。

为什么需要它
------------
醒来卡只给「最常用的一小部分」，召回只给「这次相关的细节」。两样都缺一类东西：
**一直都在的长期状态**——这个人是谁、我们在意什么、还有什么没兑现。这些不靠检索碰运气，
而是每轮都应该在场。所以单独一层「笔记」：只在醒来卡里追加一小段，不占检索预算。

为什么默认关
------------
它每轮都挤占上下文，且生成靠一次 LLM 批处理（攒够 ``notes.refresh_lines`` 行原文才整理一次），
写错一次会连续污染很多轮。先 A/B 证明净收益，再谈默认开。关的时候 ``notes.render`` 返回空串，
``wake.build`` 的输出与 0.40.0 **逐字节一致**（有测试钉住）。

职责边界（和 claim / memory 的区别）
------------------------------------
- ``memory``：证据。一条一段叙述，可检索、会被去重/淡忘。
- ``claim``：可核对的最小事实（谁-谓词-什么）。
- ``state_note``：**压缩后的长期状态**。不是证据，不参与检索/淡忘；只回答「现在这人/我们的
  关系/未完的事大致是什么样」。所以它只保留最新一次整理结果（同 kind 取最新一行）。

生成流程（宿主任务书，产品内不调模型）
--------------------------------------
``prep()`` 产出「最近原文 + 现有三栏 + 明确禁止 + JSON 格式」的任务书；宿主模型产出 JSON 后，
``apply()`` 校验并写回。``line_seq`` 记录已整理到哪条 journal_line；配 ``schema_meta`` 里的
watermark，避免同一批流水被反复整理。所有写库动作都走 ``db.tx()``，失败整批回滚。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..base import events
from ..base.clock import now_iso
from ..base.errors import ValidationError

# 三栏的稳定内部名。label 是给人和模型看的，不要反过来用 label 当 key（中文会漂）。
KINDS: tuple[str, ...] = ("about_user", "about_self", "open")
LABELS: dict[str, str] = {
    "about_user": "关于你",
    "about_self": "关于我",
    "open": "还没了的事",
}
# 每栏的字数上限来自 config.notes.*。为什么不做 token 截断：这一层要稳定可预期，
# 字符是确定性的；token 估算留给 wake 的 tokens 字段，不参与这里的分段。
CAP_KEYS: dict[str, str] = {
    "about_user": "notes.user_chars",
    "about_self": "notes.self_chars",
    "open": "notes.open_chars",
}
DEFAULT_CAPS: dict[str, int] = {"about_user": 1200, "about_self": 800, "open": 600}
# 单栏的安全上限：防止宿主把整段原文贴回来，把醒来卡撑爆。渲染层还会按 config 再截一次。
HARD_CAP = 4000
WATERMARK_PREFIX = "notes.line_seq:"


def _get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        value = config.get(key, default)
    except AttributeError:
        return default
    return default if value is None else value


def enabled(config: Any) -> bool:
    """总开关。默认关（缺配置也当关）。"""
    return bool(_get(config, "notes.enabled", False))


def _clip(text: str, cap: int) -> str:
    """按字符截断并留一个省略号：让读的人知道这里不是完整的。"""
    body = " ".join(str(text or "").split())
    cap = max(0, int(cap or 0))
    if cap <= 0 or len(body) <= cap:
        return body
    return body[: max(1, cap - 1)].rstrip() + "…"


def _check_kind(kind: str) -> str:
    kind = str(kind or "").strip()
    if kind not in KINDS:
        raise ValidationError(f"未知的笔记种类：{kind!r}（只支持 {', '.join(KINDS)}）", field="kind")
    return kind


# ---------------------------------------------------------------- 存 / 取


def put(
    db: Any,
    kind: str,
    text: str,
    line_seq: int,
    *,
    subject: str = "self",
) -> dict[str, Any]:
    """写一栏的最新版本（追加一行；同 kind 取 seq 最大的一行）。

    为什么追加而不 UPDATE：笔记是「每次整理得到的状态」，留历史才能回看「哪一步开始写偏」；
    读永远只取最新，所以对上下文没有影响。
    """
    kind = _check_kind(kind)
    body = " ".join(str(text or "").split())[:HARD_CAP]
    if not body:
        raise ValidationError("笔记正文不能为空", field="text")
    stamp = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO state_note(kind, text, line_seq, updated_at) VALUES(?,?,?,?)",
            (kind, body, int(line_seq or 0), stamp),
        )
        row_seq = int(cur.lastrowid or 0)
        events.append(
            conn,
            actor="system:notes",
            op="state_note.put",
            target_tbl="state_note",
            target_id=str(row_seq),
            subject_id=subject,
            payload={"kind": kind, "line_seq": int(line_seq or 0), "chars": len(body)},
        )
    return {"ok": True, "kind": kind, "seq": row_seq, "line_seq": int(line_seq or 0),
            "chars": len(body), "updated_at": stamp}


def get(db: Any, *, subject: str = "self") -> dict[str, dict[str, Any]]:
    """每栏最新一行（没有的栏不出现）。"""
    rows = db.conn.execute(
        "SELECT seq, kind, text, line_seq, updated_at FROM state_note "
        "WHERE seq IN (SELECT MAX(seq) FROM state_note GROUP BY kind) "
        "ORDER BY seq"
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["kind"])] = {
            "seq": int(row["seq"]),
            "text": str(row["text"]),
            "line_seq": int(row["line_seq"]),
            "updated_at": str(row["updated_at"]),
        }
    return out


def _watermark(db: Any, subject: str) -> int:
    row = db.conn.execute(
        "SELECT value FROM schema_meta WHERE key=?", (WATERMARK_PREFIX + subject,)
    ).fetchone()
    try:
        return int(row["value"]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def _set_watermark(conn: Any, subject: str, line_seq: int) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (WATERMARK_PREFIX + subject, str(int(line_seq)), now_iso()),
    )


def latest_line_seq(db: Any, *, subject: str = "self") -> int:
    """已整理到哪条原话（watermark 与各栏行里的 line_seq 取最大）。"""
    state = get(db, subject=subject)
    state_max = max((int(row["line_seq"]) for row in state.values()), default=0)
    return max(_watermark(db, subject), state_max)


# ---------------------------------------------------------------- 渲染


def render(db: Any, config: Any = None, *, subject: str = "self") -> str:
    """渲染三栏（空栏不输出，全空返回 ""）。关时返回 ""，不碰库。"""
    if not enabled(config):
        return ""
    state = get(db, subject=subject)
    lines: list[str] = []
    for kind in KINDS:
        row = state.get(kind)
        if not row:
            continue
        cap = int(_get(config, CAP_KEYS[kind], DEFAULT_CAPS[kind]) or 0)
        body = _clip(row["text"], cap)
        if body:
            lines.append(f"{LABELS[kind]}：{body}")
    return "\n".join(lines)


def render_person_cards(db: Any, config: Any = None, *, subject: str = "self") -> str:
    """渲染 N 张人物卡（``notes.person_cards``，0=不注入）。

    人物卡由 claims 聚合（见 ``claim.person_card``），这里只负责「挑哪几张 + 压成一行」：
    挑提及次数最高的活跃实体；每张 ≤120 字，只放事实与边界，不放 claim id 噪音。
    """
    n = int(_get(config, "notes.person_cards", 0) or 0)
    if n <= 0:
        return ""
    from . import claim as claim_mod

    try:
        rows = db.conn.execute(
            "SELECT pc.entity_id, e.name, e.mention_count FROM person_card pc "
            "JOIN entity e ON e.id=pc.entity_id "
            "WHERE e.subject_id=? AND e.status='active' "
            "ORDER BY e.mention_count DESC, pc.updated_at DESC LIMIT ?",
            (subject, n),
        ).fetchall()
    except Exception:  # noqa: BLE001 老库没有 claim/person_card 表时，这一层就是空的
        return ""
    lines: list[str] = []
    for row in rows:
        card = claim_mod.person_card(db, str(row["entity_id"]))
        if not card:
            continue
        bits: list[str] = []
        for fact in (card.get("facts") or [])[:6]:
            predicate = str(fact.get("predicate") or "").strip()
            obj = str(fact.get("object") or "").strip()
            if not predicate or not obj:
                continue
            if int(fact.get("polarity") or 1) == 0 and not predicate.startswith("不"):
                predicate = "不" + predicate
            bits.append(f"{predicate}{obj}")
        for boundary in (card.get("boundary") or [])[:2]:
            text = str(boundary.get("text") or "").strip()
            if text:
                bits.append(f"边界：{text}")
        if not bits:
            continue
        head = str(row["name"] or "").strip()
        relation = str(card.get("relation") or "").strip()
        if relation:
            head = f"{head}（{relation}）"
        lines.append(f"- {head}：" + _clip("；".join(bits), 120))
    if not lines:
        return ""
    return "人物卡：\n" + "\n".join(lines)


# ---------------------------------------------------------------- 任务书 / 回填


def _normalise_lines(lines: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, row in enumerate(lines or []):
        if isinstance(row, dict):
            text = " ".join(str(row.get("text") or "").split())
            if not text:
                continue
            out.append(
                {
                    "seq": int(row.get("seq") or index + 1),
                    "role": str(row.get("role") or "user"),
                    "text": text,
                    "ts": str(row.get("ts") or ""),
                }
            )
        else:
            text = " ".join(str(row or "").split())
            if text:
                out.append({"seq": index + 1, "role": "user", "text": text, "ts": ""})
    return out


def _load_material(db: Any, subject: str, start: int, limit: int) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT seq, role, text, ts FROM journal_line WHERE subject_id=? AND seq>? "
        "ORDER BY seq LIMIT ?",
        (subject, int(start), int(limit)),
    ).fetchall()
    return [
        {"seq": int(row["seq"]), "role": str(row["role"]), "text": str(row["text"]),
         "ts": str(row["ts"])}
        for row in rows
    ]


def _task_text(existing: dict[str, dict[str, Any]], material: list[dict[str, Any]],
               db_gap_hint: str = "") -> str:
    current = "\n".join(
        f"- {LABELS[kind]}：{existing[kind]['text']}" for kind in KINDS if kind in existing
    ) or "（还没有整理过，三栏都是空的）"
    lines = [
        "你是 PMem 的长期状态整理器。读下面「新原文」，把它增量合并进「三栏笔记」。",
        "",
        "三栏的定位（只写长期状态，不写证据、不写流水账）：",
        "- 关于你：这个人是谁、长期偏好/习惯、边界与雷区、重要他人的关系。",
        "- 关于我：我和这个人的约定、我的定位与说话方式（不要写成产品说明）。",
        "- 还没了的事：答应过但没做的、约好但没兑现的、悬着没结论的话题（写清「谁欠谁/等什么」，不猜结果）。",
        "",
        "现有三栏（这是上次整理的结果，可改写/合并/删除过时内容，但要保留仍然成立的部分）：",
        current,
        "",
        "新原文（按时间顺序；这是唯一素材，不要引入外部知识）：",
        "\n".join(f"[{row['seq']}] {row['role']}: {row['text']}" for row in material),
        "",
        "硬规则（写错会连续污染很多轮，宁可空着也别猜）：",
        "1) **不许把没发生的事写成已发生**：计划/假设/设想/传闻要写成「计划/在考虑/听说」，或干脆不写进「关于你」。",
        "2) **不许编日期、编专名、编地名、编数字**：原文没有的，不写；相对时间算不出绝对时间，就只写相对说法。",
        "3) 没把握就空着：不知道的不写，不要用「可能/大概」硬凑成事实。",
        "4) 「还没了的事」只写原文明确说「答应/约定/下次/还欠/还没」的；对方只是随口一提不算。",
        "5) 不写当天的流水细节、不逐条复述原文；写的是「过一段时间仍然成立的状态」。",
        "6) 三栏总长要短：关于你 ≤{user_cap} 字、关于我 ≤{self_cap} 字、还没了的事 ≤{open_cap} 字。",
        "",
        "输出必须是纯 JSON（不要 markdown 代码块），字段：",
        '{"about_user": "…", "about_self": "…", "open": "…", "line_seq": ' + str(material[-1]["seq"] if material else 0) + "}",
        "- 某栏没有内容就写空串 \"\"（不要写「暂无」）。",
        "- line_seq 填你这次整理到的新原文最大序号（不知道就照上面给的填）。",
    ]
    if db_gap_hint:
        lines.append(db_gap_hint)
    return "\n".join(lines)


def prep(
    db: Any,
    *,
    subject: str = "self",
    config: Any = None,
    lines: Iterable[Any] | None = None,
    max_lines: int | None = None,
) -> dict[str, Any]:
    """产出「宿主可执行」的整理任务书（不调模型）。

    ``lines`` 给了就用它当素材（批处理脚本按块喂历史）；没给就从 journal_line 取
    watermark 之后的新流水（产品内 CLI 用）。攒不够 ``notes.refresh_lines`` 时返回
    ``needed=false``，让调用方别花这次钱。
    """
    if not enabled(config):
        return {
            "enabled": False,
            "subject": subject,
            "material": [],
            "existing": {},
            "task": "",
            "note": "notes.enabled=false，跳过整理（不碰任何表）",
        }
    start = latest_line_seq(db, subject=subject)
    limit = int(max_lines if max_lines is not None else _get(config, "notes.refresh_lines", 40) or 40)
    limit = max(1, min(limit, 4000))
    material = _normalise_lines(lines) if lines is not None else _load_material(db, subject, start, limit)
    existing = get(db, subject=subject)
    min_seq = int(_get(config, "notes.prep.min_lines", 1) or 1) if lines is None else 0
    if not material:
        return {
            "enabled": True,
            "subject": subject,
            "needed": False,
            "material": [],
            "existing": existing,
            "task": "",
            "note": "没有攒够新流水（或已整理到最新），本次不用整理",
        }
    if lines is None and len(material) < max(1, min_seq):
        return {
            "enabled": True,
            "subject": subject,
            "needed": False,
            "material": material,
            "existing": existing,
            "task": "",
            "note": f"新流水只有 {len(material)} 行，未到 notes.prep.min_lines",
        }
    user_cap = int(_get(config, CAP_KEYS["about_user"], DEFAULT_CAPS["about_user"]) or 0)
    self_cap = int(_get(config, CAP_KEYS["about_self"], DEFAULT_CAPS["about_self"]) or 0)
    open_cap = int(_get(config, CAP_KEYS["open"], DEFAULT_CAPS["open"]) or 0)
    task = _task_text(existing, material).replace("{user_cap}", str(user_cap)).replace(
        "{self_cap}", str(self_cap)
    ).replace("{open_cap}", str(open_cap))
    return {
        "enabled": True,
        "subject": subject,
        "needed": True,
        "material": material,
        "existing": existing,
        "min_line_seq": material[0]["seq"],
        "max_line_seq": material[-1]["seq"],
        "task": task,
        "task_text": task,
        "fields": list(KINDS) + ["line_seq"],
    }


def _normalise_payload(payload: Any) -> dict[str, Any]:
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise ValidationError(f"payload 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("payload 必须是 {\"about_user\",\"about_self\",\"open\"} 对象")
    # 兼容宿主多包一层 {"notes": {...}}
    if isinstance(data.get("notes"), dict):
        data = data["notes"]
    out: dict[str, str] = {}
    found = 0
    for kind in KINDS:
        value = data.get(kind)
        if value is None:
            continue
        found += 1
        if not isinstance(value, str):
            raise ValidationError(f"{kind} 必须是字符串", field=kind)
        text = " ".join(value.split())
        if len(text) > HARD_CAP:
            raise ValidationError(f"{kind} 超长（>{HARD_CAP} 字）", field=kind)
        out[kind] = text
    if found == 0:
        # 空对象几乎一定是模型输出坏了（不是「这次没东西可写」）；不拦的话
        # 水位会莫名其妙推进、下次整理跳过一段原文。
        raise ValidationError(
            "payload 里没有任何一栏（至少要给 about_user/about_self/open 之一，"
            "没有内容就给空串）",
            field="payload",
        )
    line_seq = data.get("line_seq")
    if line_seq is not None:
        try:
            out["line_seq"] = int(line_seq)
        except (TypeError, ValueError) as exc:
            raise ValidationError("line_seq 必须是整数", field="line_seq") from exc
    return out


def apply(
    db: Any,
    *,
    subject: str = "self",
    payload: Any = None,
    config: Any = None,
    line_seq: int | None = None,
    actor: str = "system:notes",
) -> dict[str, Any]:
    """校验宿主产出并写回三栏；推进 line_seq。空栏不覆盖旧值（避免一次失败清空状态）。"""
    if not enabled(config):
        return {"enabled": False, "updated": {}, "skipped": {}, "line_seq": None,
                "note": "notes.enabled=false，不写库"}
    data = _normalise_payload(payload)
    existing = get(db, subject=subject)
    updated: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    for kind in KINDS:
        text = data.get(kind, "")
        if not text:
            skipped[kind] = "empty_keep_old" if kind in existing else "empty_no_old"
            continue
        updated[kind] = put(db, kind, text, int(line_seq or data.get("line_seq") or 0), subject=subject)
    advance = line_seq if line_seq is not None else data.get("line_seq")
    try:
        advance = int(advance) if advance is not None else 0
    except (TypeError, ValueError):
        advance = 0
    if advance <= 0:
        # 没有明确水位时，用「已经写回的行」里的最大 line_seq，最差也停在旧水位；
        # 不推进就永远不会清掉流水，所以这里必须给出一个数。
        advance = max([int(row["line_seq"]) for row in updated.values()] or
                      [latest_line_seq(db, subject=subject)])
    with db.tx() as conn:
        _set_watermark(conn, subject, advance)
        events.append(
            conn,
            actor=actor,
            op="state_note.apply",
            target_tbl="state_note",
            target_id=None,
            subject_id=subject,
            payload={"updated": sorted(updated), "skipped": skipped, "line_seq": advance},
        )
    return {
        "enabled": True,
        "updated": updated,
        "skipped": skipped,
        "line_seq": advance,
        "changed": len(updated),
    }
