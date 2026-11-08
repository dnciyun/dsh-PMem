"""``pmem`` 命令行：人工运维入口（体检、导入导出、修数据）。

默认人类可读；``--json`` 输出与 SDK 相同的统一包，方便脚本复用。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime as _datetime
from pathlib import Path
from typing import Any

import pmem
from ..base.config import (
    Config,
    default_config_path,
    parse_config_value,
    read_config_file,
    set_config_value,
    write_config_file,
)
from ..base.db import Database
from ..base.errors import PmemError
from ..memory.store import DEFAULT_SCOPE
from .sdk import PMem


def _emit(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        err = result.get("error", {})
        print(f"✗ {err.get('code')}: {err.get('message')}", file=sys.stderr)
        if err.get("hint"):
            print(f"  提示：{err['hint']}", file=sys.stderr)
        return 1
    data = result.get("data", {})
    _print_human(data)
    return 0


def _print_sleep(data: dict[str, Any]) -> None:
    """睡眠 run / catchup 的人读输出。"""
    if "runs" in data:  # catchup
        print(f"— 补跑 {data['days']} 天：执行 {data['ran']} 轮，跳过 {data['skipped']} 天，"
              f"失败 {data['failed']} 轮")
        for item in data["runs"]:
            print(f"  {item['day_key']}  {item['status']}  {item['elapsed_s']}s  {item['run_id']}")
        return
    print(f"— 睡眠 {data['day_key']} · {data['status']} · {data['elapsed_s']}s · run {data['run_id']}")
    for stage in data.get("stages", []):
        mark = "跳过" if stage.get("skipped") else ("✓" if stage.get("ok") else "✗")
        stats = stage.get("stats") or {}
        brief = ", ".join(f"{k}={v}" for k, v in list(stats.items())[:6])
        print(f"  {mark} {stage['name']}  {stage.get('ms', 0)} ms  {brief}")
    if data.get("error"):
        print(f"  ! {data['error']}")
    if data.get("report_path"):
        print(f"  报告：{data['report_path']}")


def _print_big_day(item: dict[str, Any]) -> None:
    """大睡一场的逐天进度（一行一天）。"""
    day = item.get("day")
    if item.get("error"):
        print(f"  {day}  ✗ {item['error']}", flush=True)
        return
    diary = item.get("diary") or {}
    sleep = item.get("sleep") or {}
    if diary.get("skipped"):
        mode = f"跳过（{diary['skipped']}）"
    else:
        mode = diary.get("mode") or "?"
        if diary.get("episode_id"):
            mode += f" 日记 {str(diary['episode_id'])[:8]}"
        if diary.get("facts"):
            mode += f" 事实 {diary['facts']} 条"
    stage = sleep.get("status") or sleep.get("skipped") or "?"
    print(f"  {day}  {mode}  睡眠 {stage}", flush=True)


def _print_evolve(data: dict[str, Any]) -> None:
    """进化层的人读输出：清单/统计用结构化 JSON，匹配结果逐条列。"""
    if data.get("rendered"):
        print(data["rendered"])
        return
    if "items" in data and data["items"]:
        for item in data["items"]:
            summary = item.get("summary") or (
                f"{item.get('situation', '')} → {item.get('action', '')}"
                f"（{item.get('outcome', '')}，sim {item.get('similarity', '')}）"
            )
            print(f"  · {summary}")
        return
    if "items" in data:
        print("— 没有匹配项")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _print_trait(data: dict[str, Any]) -> None:
    if "versions" in data:
        from ..trait.history import render_versions

        print(render_versions(data["versions"]))
        if data.get("timeline"):
            print("时间线：")
            for event in data["timeline"][:10]:
                print(f"  {event['ts']}  {event['op']}")
        return
    if "items" in data and data["items"] and "key" in data["items"][0]:
        for item in data["items"]:
            label = (item.get("value") or {}).get("label") or item["key"]
            print(
                f"  {item['status']:<11} {item['type']:<9} {label}  "
                f"strength {item['strength']:.2f}  证据 {item['evidence_count']} 条/{item['evidence_days']} 天"
            )
        return
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _print_human(data: dict[str, Any]) -> None:
    if "id" in data and "text" in data:  # get
        print(f"— {data['id']}  [{data['status']}]  {data['kind']}")
        print(f"  {data['text']}")
        print(f"  重要度 {data['importance']:.3f} · 可信度 {data['confidence']:.3f} · "
              f"来源 {data['provenance']} · 证据 {data['evidence_count']} 条")
        print(f"  时间 {data['created_at']}（{data['day']}）  取值区间 {data['valid_from'] or '—'} → {data['valid_to'] or '—'}")
        if data.get("groups") or data.get("grade"):
            print(f"  分组 {' / '.join(data.get('groups') or []) or '—'}  ｜ 等级 {data.get('grade') or '—'}")
        return
    if "id" in data and "status" in data:  # add
        dup = f"（已存在，合并证据：{data['duplicate_of']}）" if data.get("duplicate_of") else ""
        replay = " [幂等重放]" if data.get("idempotent_replay") else ""
        print(f"✓ {data['id']} [{data['status']}] 重要度 {data['importance']:.3f} "
              f"可信度 {data['confidence']:.3f}{dup}{replay}")
        if data.get("groups") or data.get("grade"):
            print(f"  分组 {' / '.join(data.get('groups') or []) or '—'}  ｜ 等级 {data.get('grade') or '—'}")
        return
    if "awb_hit_rate" in data:  # shadow run
        print(f"— 影子查询 {data['queries']} 条（错误 {data['errors']}）")
        print(f"  AWB 命中率 {data['awb_hit_rate']:.0%} · soul(FTS) {data['soul_fts_hit_rate']:.0%} · "
              f"soul(LIKE) {data['soul_like_hit_rate']:.0%}")
        print(f"  重合率：对比 FTS {data['overlap_fts_mean']:.0%} · 对比 LIKE {data['overlap_like_mean']:.0%}")
        print(f"  tokens 均值 {data['tokens_mean']} / p95 {data['tokens_p95']} · "
              f"延迟 p50 {data['latency_p50_ms']} ms / p95 {data['latency_p95_ms']} ms")
        print(f"  明细 → {data.get('out_path') or data.get('report_path')}")
        return
    if "messages" in data and "added" in data:  # backfill
        print(f"— 回填：扫描 {data['files_scanned']} 个文件 / 有效会话 {data['sessions']} 个")
        print(f"  用户消息 {data['messages']} 条 → 新增 {data['added']} 条，"
              f"幂等重放 {data['replay']} 条，重复合并 {data['duplicates']} 条")
        print(f"  作用域 {data.get('scope')}")
        return
    if "deleted" in data:  # delete
        print(f"✓ 删除 {data['deleted']} 条（hard={data.get('hard')}）")
        return
    if "gates" in data and "relation" in data:  # net eval
        print(f"— 关系评测 seed={data['seed']} ｜ 记忆 {data['memories']} 条")
        for gate in data["gates"]:
            mark = "✓" if gate["pass"] else "✗"
            print(f"  {mark} {gate['name']}: {gate['actual']}（目标 {gate['target']}）")
        rel = data["relation"]
        dis = data["disambiguation"]
        hop = data["two_hop"]
        print(f"  关系：P {rel['precision']} / R {rel['recall']}（预测 {rel['predicted']} / 真值 {rel['gold']}）")
        print(f"  消歧 {dis['correct']}/{dis['total']} · 别名 {data['alias']['accuracy']}")
        print(f"  2 跳：{hop['edges']} 边，p95 {hop['p95_ms']} ms（{hop['queries']} 次查询）")
        return
    if "gates" in data:  # eval
        print(f"— 评测 seed={data['seed']} ｜ 记忆 {data['memories']} 条")
        for gate in data["gates"]:
            mark = "✓" if gate["pass"] else "✗"
            print(f"  {mark} {gate['name']}: {gate['actual']}（目标 {gate['target']}）")
        recall = data["recall"]
        print(f"  召回：词法 {recall['lexical_recall_at_k']} · 仅语义 {recall['paraphrase_recall_at_k']} · "
              f"延迟 p50 {recall['latency_p50_ms']} ms")
        return
    if "moved" in data and "by_status" in data:  # lifecycle tick
        print(f"— 生命周期：移动 {data['moved']} 条  {data['by_status'] or ''}")
        if data.get("by_dormant_grade"):
            print(f"  按等级休眠：{data['by_dormant_grade']}")
        if data.get("note"):
            print(f"  说明：{data['note']}")
        return
    if "groups_by_depth" in data:  # group stats
        print(f"— 分组 {data['groups']} 个  成员关系 {data['memberships']} 条")
        print(f"  分级：{data['grades'] or '无'}")
        print(f"  已分组记忆 {data['grouped_memories']}/{data['total_memories']}"
              f"（未分组 {data['ungrouped_memories']}）")
        return
    if "items" in data and data["items"] and "members" in data["items"][0]:  # group tree
        for item in data["items"]:
            tail = f"{item['members']} 项"
            if item.get("children"):
                tail += f" / {item['children']} 子组"
            print("  " * (item["depth"] - 1) + f"{item['name']}/  （{tail}，{item['kind']}）")
        return
    if "items" in data and data["items"] and "tbl" in data["items"][0]:  # group targets
        print(f"— {data.get('path') or '全部分组'}：{len(data['items'])} 项")
        for item in data["items"]:
            label = item.get("name") or (item.get("text") or "")[:60]
            print(f"  [{item.get('grade') or '-'}] {item['path']}  {item['tbl']}:{item['id'][:10]}  {label}")
        return
    if "entities_by_type" in data:  # net stats
        types = " · ".join(f"{k} {v}" for k, v in sorted(data["entities_by_type"].items()))
        print(f"— 实体 {data['entities']} 个（{types or '无'}）  提及 {data['mentions']} 次")
        print(f"  关系 {data['relations']} 条  状态 {data['relations_by_status']}")
        preds = " · ".join(f"{k} {v}" for k, v in list(data["relations_by_predicate"].items())[:8])
        if preds:
            print(f"  谓词：{preds}")
        return
    if "items" in data and data["items"] and "mention_count" in data["items"][0]:  # entities
        print(f"— 实体 {len(data['items'])} 个")
        for item in data["items"]:
            extra = f"（{', '.join(item['disambig'])}）" if item.get("disambig") else ""
            print(f"  {item['name']}{extra}  [{item['type']}]  提及 {item['mention_count']} 次  conf {item['confidence']:.2f}")
        return
    if "depth" in data and "items" in data:  # graph query（含空结果）
        src = (data.get("src") or {}).get("name") or (data.get("request") or {}).get("src") or "?"
        if not data["items"]:
            print(f"— {src} 出发：没有命中路径（扫描 {data.get('scanned', 0)} 条）")
            return
        print(f"— {src} 出发：{len(data['items'])} 条路径（扫描 {data.get('scanned', 0)} 条）")
        for item in data["items"]:
            print(f"  [{item['weight']:.3f}] {item['hops']}跳  " + " → ".join(item["path"]))
            print(f"      {'/'.join(item['predicates'])}  证据 {len(item['evidence'])} 条")
        return
    if "relations_created" in data and "scanned" in data:  # net extract
        print(f"— 补抽记忆 {data['scanned']} 条 · 提及 {data['mentions']} 次")
        print(f"  新增关系 {data['relations_created']} · 强化 {data['relations_reinforced']}")
        return
    if "pmi" in data and "transitive" in data:  # net derive
        pmi = data["pmi"]
        tr = data["transitive"]
        print(f"— 共现 PMI：候选对 {pmi['pairs']}，新增 {pmi['created']}（低于门槛 {pmi['skipped_low']}）")
        print(f"  传递闭包：新增 {tr['created']}，已存在跳过 {tr['skipped_existing']}")
        return
    if "dormant" in data and "cutoff" in data:  # net dormant
        print(f"— 转入休眠 {data['dormant']} 条（截止 {data['cutoff']}）")
        return
    if "keep" in data and "merged" in data:  # net merge
        print(f"✓ 已合并 {data['merged']} → {data['keep']}")
        print(f"  别名 {data['aliases']} · 提及 {data['mentions']} · 关系 {data['relations']}")
        return
    if "items" in data and data["items"] and "left" in data["items"][0]:  # conflicts
        for item in data["items"]:
            print(f"  [{item['kind']}] {item['slot']}  {item['left'][:10]}… ↔ {item['right'][:10]}…")
            print(f"      id={item['id']}  severity={item['severity']}")
        return
    if "items" in data:  # search / history
        items = data["items"]
        print(f"— 命中 {len(items)} 条（候选 {data.get('total_candidates', 0)}，"
              f"{data.get('latency_ms', 0)} ms，{data.get('budget_used', 0)} tokens）")
        for item in items:
            if "op" in item:  # history
                print(f"  {item['ts']}  {item['op']:<18} {item['actor']}")
                continue
            print(f"  [{item.get('score', 0):.3f}] {item.get('day', '')} · {item.get('kind', '')} · {item['text'][:100]}")
            if item.get("grade") or item.get("groups"):
                print(f"      {item.get('grade') or '-'}  {' / '.join(item.get('groups') or [])}")
        return
    if "memory_total" in data:
        print(f"— 记忆 {data['memory_total']} 条  库 {data['db_size_bytes'] / 1024:.1f} KB  "
              f"schema {data['schema_version']}")
        print(f"  状态：{data['by_status']}")
        print(f"  类型：{data['by_kind']}")
        print(f"  事件：{data['event_count']} 条   完整性：{'ok' if data['integrity_ok'] else 'FAIL'}")
        return
    if "features" in data:
        print(f"— {data['name']['cn']} {data['server']} (schema {data['schema_version']})")
        print(f"  能力：{data['features']}")
        print(f"  降级：{data['degraded'] or '无'}")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _cmd_config(args: argparse.Namespace) -> int:
    """``config get/set/list/path``：读写 ``~/.dsh/pmem/config.toml``。

    为什么要有这条命令：插件设置页里的 PMem 级开关（例如夜间鲸眠）需要一个
    持久化出口；写回用点号键 TOML，CLI/MCP 下次启动就生效。
    """
    path = Path(args.config) if args.config else default_config_path()
    action = str(args.action)
    if action == "path":
        print(str(path))
        return 0
    data = read_config_file(path)
    if action == "get":
        value = Config.load(path).get(args.key, None)
        if args.json:
            print(json.dumps(
                {"ok": True, "data": {"key": args.key, "value": value, "path": str(path)}},
                ensure_ascii=False, indent=2,
            ))
        elif isinstance(value, str):
            print(value)
        else:
            print(json.dumps(value, ensure_ascii=False))
        return 0
    if action == "set":
        value = parse_config_value(args.value)
        set_config_value(data, args.key, value)
        write_config_file(path, data)
        effective = Config.load(path).get(args.key, None)
        if args.json:
            print(json.dumps(
                {
                    "ok": True,
                    "data": {"key": args.key, "value": effective, "path": str(path)},
                },
                ensure_ascii=False, indent=2,
            ))
        else:
            print(f"已写入 {args.key} = {json.dumps(effective, ensure_ascii=False)}  ({path})")
        return 0
    # list：只列配置文件里**显式写过**的键（未写 = 用默认值，不在这里刷屏）
    lines: list[str] = []

    def walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                walk(value, dotted)
            else:
                lines.append(f"{dotted} = {json.dumps(value, ensure_ascii=False)}")

    walk(data, "")
    if args.json:
        print(json.dumps({"ok": True, "data": {"path": str(path), "items": lines}},
                         ensure_ascii=False, indent=2))
    elif lines:
        print("\n".join(lines))
    else:
        print(f"（配置文件是空的：{path}）")
    return 0


# ---------------------------------------------------------------------------
# 断言层（claim，0.41.0，默认关）对外接口。
#
# CLI 的 ``pmem claim ...`` 与 MCP 的 ``claim_*`` 工具**共用这一份实现**（MCP 侧只做
# 参数转接，不复制业务逻辑）。内核在 ``pmem.memory.claim`` / ``pmem.memory.claim_extract``；
# 这里只负责：目标日归一、limit/k 夹取、统一 ``{ok, data}`` 信封。
# 关闸（``claim.enabled=false``）时 prep/apply/rule 由内核直接返回 enabled=false，
# 不读材料、不写任何表；list/search/delete 仍可显式调用（用户主动要看/删）。
# ---------------------------------------------------------------------------
_CLAIM_LIMIT_DEFAULT = 100
_CLAIM_LIMIT_MAX = 500
_CLAIM_K_DEFAULT = 8
_CLAIM_K_MAX = 50
_CLAIM_SOURCE_MAX = 200000


def _claim_bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _claim_day(day_key: str | None) -> str:
    """抽取目标日：显式给就用，否则默认「昨天」（和睡眠/鲸眠同一口径）。"""
    text = str(day_key or "").strip()
    if text:
        return text[:10]
    from datetime import timedelta

    from ..base import clock

    return clock.day_key(clock.now() - timedelta(days=1))


def claim_enabled(app: PMem) -> bool:
    """claim 主开关（默认关）。"""
    try:
        return bool(app.config.get("claim.enabled", False))
    except Exception:  # noqa: BLE001 坏 config 一律当关
        return False


def claim_list_payload(
    app: PMem,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: Any = _CLAIM_LIMIT_DEFAULT,
    include_superseded: bool = False,
) -> dict[str, Any]:
    from ..memory import claim as claim_mod

    rows = claim_mod.list_claims(
        app.db,
        subject_id=getattr(app, "subject", None),
        kind=(str(kind).strip() or None) if kind else None,
        status=(str(status).strip() or None) if status else None,
        include_superseded=bool(include_superseded),
        limit=_claim_bounded(limit, _CLAIM_LIMIT_DEFAULT, 1, _CLAIM_LIMIT_MAX),
    )
    return {
        "ok": True,
        "data": {"items": rows, "total": len(rows), "enabled": claim_enabled(app)},
    }


def claim_search_payload(
    app: PMem,
    *,
    query: str,
    kind: str | None = None,
    k: Any = _CLAIM_K_DEFAULT,
) -> dict[str, Any]:
    from ..memory import claim as claim_mod

    text = str(query or "").strip()
    if not text:
        return {
            "ok": False,
            "error": {"code": "E_VALIDATION", "message": "query 不能为空", "field": "query"},
        }
    rows = claim_mod.search(
        app.db,
        text,
        subject_id=getattr(app, "subject", None),
        kind=(str(kind).strip() or None) if kind else None,
        k=_claim_bounded(k, _CLAIM_K_DEFAULT, 1, _CLAIM_K_MAX),
    )
    return {
        "ok": True,
        "data": {"query": text, "items": rows, "total": len(rows), "enabled": claim_enabled(app)},
    }


def claim_delete_payload(
    app: PMem, *, claim_id: str, reason: str = "user"
) -> dict[str, Any]:
    from ..memory import claim as claim_mod

    target = str(claim_id or "").strip()
    if not target:
        return {
            "ok": False,
            "error": {"code": "E_VALIDATION", "message": "claim_id 不能为空", "field": "claim_id"},
        }
    try:
        data = claim_mod.soft_delete(app.db, target, reason=str(reason or "user"))
    except PmemError as exc:
        return {"ok": False, "error": exc.to_dict()}
    return {"ok": True, "data": data}


def claim_prep_payload(
    app: PMem, *, day_key: str | None = None, max_source_chars: Any = None
) -> dict[str, Any]:
    from ..memory import claim_extract

    data = claim_extract.prep(
        app.db,
        subject=getattr(app, "subject", "self") or "self",
        day_key=_claim_day(day_key),
        config=app.config,
        max_source_chars=(
            None
            if max_source_chars is None
            else _claim_bounded(max_source_chars, 0, 0, _CLAIM_SOURCE_MAX)
        ),
    )
    return {"ok": True, "data": data}


def claim_apply_payload(
    app: PMem, *, day_key: str | None = None, payload: Any = None
) -> dict[str, Any]:
    from ..memory import claim_extract

    if payload is None:
        return {
            "ok": False,
            "error": {
                "code": "E_VALIDATION",
                "message": "payload 不能为空（{\"claims\": [...]} 或 claims 数组）",
                "field": "payload",
            },
        }
    try:
        data = claim_extract.apply(
            app.db,
            subject=getattr(app, "subject", "self") or "self",
            day_key=_claim_day(day_key),
            payload=payload,
            config=app.config,
        )
    except PmemError as exc:
        return {"ok": False, "error": exc.to_dict()}
    return {"ok": True, "data": data}


def claim_rule_payload(app: PMem, *, day_key: str | None = None) -> dict[str, Any]:
    from ..memory import claim_extract

    data = claim_extract.rule_extract(
        app.db,
        subject=getattr(app, "subject", "self") or "self",
        day_key=_claim_day(day_key),
        config=app.config,
    )
    return {"ok": True, "data": data}


def _print_claim(data: dict[str, Any], action: str) -> None:
    """``pmem claim ...`` 的人读输出（--json 时走 _emit，不走这里）。"""
    if action in ("list", "search"):
        items = data.get("items") or []
        head = f"搜索「{data.get('query')}」" if action == "search" else "列表"
        state = "开" if data.get("enabled") else "关"
        print(f"— 断言{head}：{len(items)} 条（claim.enabled={state}）")
        for item in items:
            polarity = "¬" if int(item.get("polarity") or 0) == 0 else ""
            subject = item.get("subject_id") or "self"
            confidence = float(item.get("confidence") or 0)
            print(
                f"  · [{str(item.get('id') or '')[:8]}] {item.get('kind')} · {subject} "
                f"{polarity}{item.get('predicate')} {item.get('object')}"
                f"  ({item.get('status')}, {confidence:.2f})"
            )
            quote = str(item.get("evidence_quote") or "").strip()
            if quote:
                print(f"      原话：{quote[:80]}")
        if not items and not data.get("enabled"):
            print("  （claim.enabled=false：断言层没有数据；先开主开关再做抽取）")
        return
    if action == "delete":
        print(f"✓ 已软删断言 {data.get('claim_id')}（只改 status，事件链留墓碑）")
        return
    if action == "prep":
        if data.get("enabled") is False:
            print(f"— {data.get('note') or 'claim.enabled=false，跳过抽取'}")
            return
        material = data.get("material") or []
        print(
            f"— 任务书已就绪（{data.get('day_key')} · 原话 {len(material)} 行 / "
            f"{data.get('source_chars', 0)} 字）"
        )
        print(data.get("task") or "")
        return
    if action in ("apply", "rule"):
        if data.get("enabled") is False:
            print(f"— {data.get('note') or 'claim.enabled=false，不入库'}")
            return
        print(
            f"✓ 断言入库：扫描 {data.get('scanned', 0)} / 保留 {data.get('kept', 0)} / "
            f"新增 {data.get('inserted', 0)} / 合并 {data.get('merged', 0)} / "
            f"更新 {data.get('updated', 0)} / 版本 {data.get('versioned', 0)} / "
            f"丢弃 {data.get('dropped', 0)}"
        )
        reasons = data.get("reasons") or {}
        if reasons:
            print(f"  丢弃原因：{reasons}")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


# 常驻三栏笔记（1.12.0，默认关）：show / prep / apply。产品内不调模型，
# prep 只出任务书给宿主，apply 校验后写回（和 claim extract 的 prep/apply 同构）。
def notes_show_payload(app: PMem) -> dict[str, Any]:
    from ..memory import notes as notes_mod

    subject = getattr(app, "subject", "self") or "self"
    return {
        "ok": True,
        "data": {
            "enabled": notes_mod.enabled(app.config),
            "subject": subject,
            "items": notes_mod.get(app.db, subject=subject),
            "render": notes_mod.render(app.db, app.config, subject=subject),
            "person_cards": notes_mod.render_person_cards(app.db, app.config, subject=subject),
            "line_seq": notes_mod.latest_line_seq(app.db, subject=subject),
        },
    }


def notes_prep_payload(app: PMem, *, max_lines: Any = None) -> dict[str, Any]:
    from ..memory import notes as notes_mod

    data = notes_mod.prep(
        app.db,
        subject=getattr(app, "subject", "self") or "self",
        config=app.config,
        max_lines=None if max_lines is None else int(max_lines),
    )
    return {"ok": True, "data": data}


def notes_apply_payload(app: PMem, *, payload: Any = None) -> dict[str, Any]:
    from ..memory import notes as notes_mod

    if payload is None:
        return {
            "ok": False,
            "error": {
                "code": "E_VALIDATION",
                "message": "payload 不能为空（{\"about_user\",\"about_self\",\"open\"} 对象）",
                "field": "payload",
            },
        }
    try:
        data = notes_mod.apply(
            app.db,
            subject=getattr(app, "subject", "self") or "self",
            payload=payload,
            config=app.config,
        )
    except PmemError as exc:
        return {"ok": False, "error": exc.to_dict()}
    return {"ok": True, "data": data}


def _print_notes(data: dict[str, Any], action: str) -> None:
    """``pmem notes ...`` 的人读输出（--json 时走 _emit，不经过这里）。"""
    if action == "show":
        state = "开" if data.get("enabled") else "关"
        print(f"— 常驻三栏笔记（notes.enabled={state}，已整理到 line_seq={data.get('line_seq')}）")
        rendered = str(data.get("render") or "")
        print(rendered if rendered else "  （三栏都为空：先 prep → 宿主产出 JSON → apply）")
        cards = str(data.get("person_cards") or "")
        if cards:
            print(cards)
        return
    if action == "prep":
        if data.get("enabled") is False:
            print(f"— {data.get('note') or 'notes.enabled=false，跳过整理'}")
            return
        if data.get("needed") is False:
            print(f"— {data.get('note') or '没有新的流水，本次不用整理'}")
            return
        material = data.get("material") or []
        print(f"— 任务书已就绪（原话 {len(material)} 行，line_seq "
              f"{data.get('min_line_seq')}→{data.get('max_line_seq')}）")
        print(data.get("task") or "")
        return
    if action == "apply":
        if data.get("enabled") is False:
            print(f"— {data.get('note') or 'notes.enabled=false，不写库'}")
            return
        print(f"✓ 三栏已写回：更新 {len(data.get('updated') or {})} 栏，"
              f"line_seq={data.get('line_seq')}")
        skipped = data.get("skipped") or {}
        if skipped:
            print(f"  未更新的栏：{skipped}")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，✓/✗ 会直接炸；只改编码，不改行为
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(prog="pmem", description=f"{pmem.__name_cn__} {pmem.__name_en__}")
    parser.add_argument("--db", default=None, help="库文件路径（默认 data/pmem.db）")
    parser.add_argument("--config", default=None, help="TOML 配置路径")
    parser.add_argument("--subject", default="self")
    parser.add_argument("--scope-key", default=DEFAULT_SCOPE)
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument(
        "--now",
        default=None,
        help="把系统时间固定在这个时刻（ISO-8601；测试/回放用，等价于 PMEM_NOW）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库（幂等）")
    sub.add_parser("stats", help="体检统计")
    sub.add_parser("capabilities", help="能力与降级状态")
    sub.add_parser("doctor", help="完整性检查")

    # 配置文件（0.36.0）：设置页的 PMem 级开关从这里落盘；默认 ~/.dsh/pmem/config.toml。
    p_cfg = sub.add_parser("config", help="配置文件：get / set / list / path（默认 ~/.dsh/pmem/config.toml）")
    cfg_sub = p_cfg.add_subparsers(dest="action", required=True)
    p_cfg_get = cfg_sub.add_parser("get", help="读一个键（点号路径；返回生效值，未写=默认值）")
    p_cfg_get.add_argument("key")
    p_cfg_set = cfg_sub.add_parser("set", help="写一个键（true/false/数字/字符串；点号路径）")
    p_cfg_set.add_argument("key")
    p_cfg_set.add_argument("value")
    cfg_sub.add_parser("list", help="列出配置文件里显式写过的键")
    cfg_sub.add_parser("path", help="打印配置文件路径")

    p_vac = sub.add_parser("vacuum", help="VACUUM：压缩数据库并回收已删空间，输出前后体积")
    p_vac.add_argument(
        "--analyze",
        action="store_true",
        help="VACUUM 后同时跑 PRAGMA optimize（更新查询计划统计）；默认只 VACUUM",
    )

    p_rs = sub.add_parser("restore", help="从备份文件恢复整个库（会覆盖当前库）")
    p_rs.add_argument("backup", help="备份文件路径（pmem backup run / backup list 产出的 .db）")
    p_rs.add_argument("--force", action="store_true", help="目标库已有数据时也覆盖")

    p_add = sub.add_parser("add", help="写一条记忆")
    p_add.add_argument("text")
    p_add.add_argument("--kind", default="semantic")
    p_add.add_argument("--provenance", default="user_explicit")
    p_add.add_argument("--session", default=None)
    p_add.add_argument("--source-kind", default="cli")
    p_add.add_argument("--sensitivity", default="personal")
    p_add.add_argument("--pinned", action="store_true")
    p_add.add_argument("--valid-from", default=None)
    p_add.add_argument("--valid-to", default=None)
    p_add.add_argument("--idem", default=None)
    p_add.add_argument("--group", action="append", default=None, help="分组路径，可多次传")
    p_add.add_argument("--grade", default=None, choices=["S", "A", "B", "C", "D"])
    p_add.add_argument("--grade-reason", default=None)

    p_search = sub.add_parser("search", help="召回")
    p_search.add_argument("query")
    p_search.add_argument("--k", type=int, default=None, help="返回几条；默认读 [retrieval].top_k")
    p_search.add_argument("--budget", type=int, default=None, help="token 预算；默认读 [retrieval].token_budget")
    p_search.add_argument("--mode", default="auto", choices=["auto", "keyword", "vector"])
    p_search.add_argument("--kind", action="append", default=None)
    p_search.add_argument("--group", default=None, help="只看某个分组（含子组）")
    p_search.add_argument("--grade", action="append", default=None, choices=["S", "A", "B", "C", "D"])
    p_search.add_argument("--pinned-only", action="store_true")

    # 作答后自检（0.36.0）：草稿回答里的日期/专名有没有记忆出处（确定性，0 模型调用）。
    p_self = sub.add_parser("selfcheck", help="作答后自检：草稿回答里的日期/专名有没有记忆出处")
    p_self.add_argument("answer", nargs="?", default=None, help="草稿回答；不传则从 stdin 读")
    p_self.add_argument("--question", default=None, help="原问题（可选，只记录）")

    # 答案保真减法（P0-A / 0.40.0，默认关）：以本次注入的 items 为上下文，逐 claim 指出
    # 「有型且无支撑」的断言，给 risk + 最小修法（flag/hint/strip，默认 flag）。
    p_verify = sub.add_parser("verify", help="答案保真检查：答案里有没有上下文没支撑的有型断言")
    p_verify.add_argument("answer", nargs="?", default=None, help="待检答案；不传则从 stdin 读")
    p_verify.add_argument("--query", default="", help="原问题（可选，只记录）")
    p_verify.add_argument("--items", default=None, help="这次注入的 items JSON 文件（对象数组）")

    # 作答计划（0.37.0）：回答之前问一句「这题该怎么答」，拿回结构化计划 + 一段纪律文本。
    p_plan = sub.add_parser("plan", help="作答计划：纪律字段化（只答被问的 / 不补没依据的细节）")
    p_plan.add_argument("question", nargs="?", default=None, help="用户问话；不传则从 stdin 读")
    p_plan.add_argument("--when", default=None, help="逻辑时间（ISO-8601），换算「今天」的基准")
    p_plan.add_argument("--from-search", action="store_true",
                        help="先拿这句问话检索一次，把结果当证据卡喂进计划（不额外花钱）")

    # 统一渲染（P0-2）：召回（或给定 items）→ 产品唯一的「证据块」文本，宿主/CLI/MCP 共用。
    p_render = sub.add_parser("render", help="统一渲染：召回结果 → 证据块文本")
    p_render.add_argument("query", nargs="?", default=None, help="问话；不传则用 --items 的 JSON 文件")
    p_render.add_argument("--items", default=None, help="items JSON 文件（对象数组）")
    p_render.add_argument("--k", type=int, default=None, help="先检索时返回几条；默认读 [retrieval].top_k")
    p_render.add_argument("--title", default="相关记忆")
    p_render.add_argument("--budget", type=int, default=None)

    # 断言层（claim，0.41.0，默认关）：列表 / 搜索 / 删除 / 抽取（prep/apply/rule）。
    # 业务实现与 MCP 的 claim_* 工具共用（见本文件 claim_*_payload）。
    p_claim = sub.add_parser("claim", help="断言层：list / search / delete / extract（默认关）")
    claim_sub = p_claim.add_subparsers(dest="claim_action", required=True)
    p_cl = claim_sub.add_parser("list", help="列出断言（默认不含已改口/已删除）")
    p_cl.add_argument("--kind", default=None)
    p_cl.add_argument("--status", default=None)
    p_cl.add_argument("--limit", type=int, default=_CLAIM_LIMIT_DEFAULT)
    p_cl.add_argument("--include-superseded", action="store_true")
    p_cs = claim_sub.add_parser("search", help="确定性检索断言（不做 FTS）")
    p_cs.add_argument("query")
    p_cs.add_argument("--kind", default=None)
    p_cs.add_argument("--k", type=int, default=_CLAIM_K_DEFAULT)
    p_cd = claim_sub.add_parser("delete", help="软删一条断言（只改 status，留墓碑）")
    p_cd.add_argument("claim_id")
    p_cd.add_argument("--reason", default="user")
    p_ce = claim_sub.add_parser(
        "extract", help="抽取断言：--prep 出任务书 / --apply 入库 / --rule 规则降级"
    )
    p_ce.add_argument("--day", default=None, help="哪一天（YYYY-MM-DD，默认昨天）")
    p_ce.add_argument("--prep", action="store_true", help="输出宿主任务书 + 当天原话材料（默认）")
    p_ce.add_argument(
        "--apply", dest="apply_file", default=None, metavar="JSON",
        help="宿主产出的 {claims:[...]} JSON 文件；校验后入库",
    )
    p_ce.add_argument("--rule", action="store_true", help="无宿主时的确定性规则降级抽取")
    p_ce.add_argument(
        "--max-source-chars", type=int, default=None, help="只取最近 N 字符原话（0=全读）"
    )

    # 常驻三栏笔记（1.12.0，默认关）：show 看现状 / prep 出任务书 / apply 回填。
    # 产品内不调模型：prep 只把材料与禁止事项给宿主，apply 只做校验与落库。
    p_notes = sub.add_parser("notes", help="常驻三栏笔记（默认关）：show / prep / apply")
    notes_sub = p_notes.add_subparsers(dest="notes_action", required=True)
    notes_sub.add_parser("show", help="看当前三栏与渲染结果")
    p_np = notes_sub.add_parser("prep", help="输出整理任务书（最近新流水 + 现有三栏 + 禁止事项）")
    p_np.add_argument("--max-lines", type=int, default=None, help="最多取多少行新流水")
    p_na = notes_sub.add_parser("apply", help="把宿主产出的 JSON 校验后写回三栏")
    p_na.add_argument(
        "--file", default=None, metavar="JSON",
        help="宿主产出 JSON 文件；省略则从 stdin 读",
    )

    p_trace = sub.add_parser("trace", help="按字查原话（一字不差）：会话 / 行号 / 角色")
    p_trace.add_argument("query")
    p_trace.add_argument("--limit", type=int, default=20)
    p_trace.add_argument(
        "--include-retracted", action="store_true", help="连撤回过的原话一起看（带标记）"
    )

    p_retr = sub.add_parser("retract", help="撤回一句原话或一条记忆（墓碑，不是删除）")
    p_retr.add_argument("target", help="记忆 id，或 trace 查到的原话 id")
    p_retr.add_argument("--reason", default="", help="为什么撤回（写进事件日志，方便认账）")
    p_retr.add_argument(
        "--kind", default="auto", choices=["auto", "line", "memory"], help="auto=自己认"
    )

    p_get = sub.add_parser("get", help="取一条")
    p_get.add_argument("id")

    p_upd = sub.add_parser("update", help="改元数据 / 纠错")
    p_upd.add_argument("id")
    p_upd.add_argument(
        "--kind",
        default=None,
        choices=["episodic", "semantic", "person", "relation", "experience", "goal", "meta"],
        help="改 kind（控制台批量改元数据用；store.update 已支持这个字段）",
    )
    p_upd.add_argument("--status", default=None)
    p_upd.add_argument("--sensitivity", default=None)
    p_upd.add_argument("--importance", type=float, default=None)
    p_upd.add_argument("--pin", action="store_true")
    p_upd.add_argument("--unpin", action="store_true")
    p_upd.add_argument("--supersede", default=None, help="用新文本替代（旧行保留为 superseded）")
    p_upd.add_argument("--reason", default=None)

    p_del = sub.add_parser("delete", help="删除")
    p_del.add_argument("id")
    p_del.add_argument("--hard", action="store_true")
    p_del.add_argument(
        "--vacuum",
        action="store_true",
        help="hard 删除后 VACUUM 回收空间（维护窗口内拒绝执行）",
    )

    p_hist = sub.add_parser("history", help="变更时间线")
    p_hist.add_argument("id")
    p_hist.add_argument("--limit", type=int, default=50)

    p_exp = sub.add_parser("export", help="导出 JSONL")
    p_exp.add_argument("--out", default=None)
    p_exp.add_argument("--no-redact", action="store_true")
    p_exp.add_argument(
        "--with-events",
        action="store_true",
        help="连同 event_log 一起导出（事件是哈希链，导入端要求空库）",
    )
    p_imp = sub.add_parser("import", help="导入 JSONL（M8）")
    p_imp.add_argument("path", nargs="?", default="-", help="文件路径；- 表示 stdin")
    p_imp.add_argument("--on-conflict", choices=["skip", "merge", "fail"], default="skip")
    p_imp.add_argument("--dry-run", action="store_true")
    p_imp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_vec = sub.add_parser("vector", help="向量通道（M13）：本地 bge 语义检索")
    vec_sub = p_vec.add_subparsers(dest="vector_action", required=True)
    vec_sub.add_parser("status", help="通道状态：模型 / 依赖 / 向量数")
    vec_sub.add_parser("build", help="补齐缺向量的记忆（幂等）")
    vec_sub.add_parser("rebuild", help="整表重算（换模型 / 量化版本后用）")
    p_vs = vec_sub.add_parser("search", help="只看向量排名（调试用）")
    p_vs.add_argument("query")
    p_vs.add_argument("--k", type=int, default=8)
    p_vs.add_argument("--min-score", type=float, default=0.0)

    sub.add_parser("mcp", help="以 MCP stdio server 运行（供宿主拉起）")

    p_eval = sub.add_parser("eval", help="跑合成集评测（M2/M3 Gate）")
    p_eval.add_argument("--seed", type=int, default=7)
    p_eval.add_argument("--out", default=None, help="输出目录，默认 .tmp/eval")
    p_eval.add_argument("--k", type=int, default=5)
    p_eval.add_argument("--net", action="store_true", help="跑 M3 关系网络评测")
    p_eval.add_argument("--edges", type=int, default=100000, help="2 跳基准的边数")
    p_eval.add_argument("--record", action="store_true", help="把评测指标写入当前库，供 monitor 看趋势")

    p_fb = sub.add_parser("feedback", help="给一条记忆打反馈")
    p_fb.add_argument("id")
    p_fb.add_argument("signal", choices=["useful", "irrelevant", "wrong", "confirm", "deny", "pin", "unpin"])
    p_fb.add_argument("--note", default=None)

    p_cons = sub.add_parser("consolidate", help="去重合并（等效夜间巩固的 s2）")
    p_cons.add_argument("--near", type=float, default=None)
    p_cons.add_argument("--dry-run", action="store_true")

    p_conf = sub.add_parser("conflicts", help="冲突列表与裁决")
    conf_sub = p_conf.add_subparsers(dest="action", required=True)
    p_conf_list = conf_sub.add_parser("list", help="列出冲突")
    p_conf_list.add_argument("--status", default="open")
    p_conf_list.add_argument("--limit", type=int, default=20)
    conf_sub.add_parser("detect", help="扫描并记录新冲突")
    p_conf_res = conf_sub.add_parser("resolve", help="裁决一条冲突")
    p_conf_res.add_argument("id")
    p_conf_res.add_argument("decision", choices=["prefer_left", "prefer_right", "keep_both", "dismissed"])
    p_conf_res.add_argument("--note", default=None)

    p_net = sub.add_parser("net", help="关系网络（M3）：实体/关系/2 跳查询")
    net_sub = p_net.add_subparsers(dest="net_action", required=True)
    net_sub.add_parser("stats", help="实体与关系统计")
    p_ne = net_sub.add_parser("entities", help="列出实体")
    p_ne.add_argument("--type", default=None)
    p_ne.add_argument("--query", default=None)
    p_ne.add_argument("--limit", type=int, default=None, help="默认走 SDK 的 50")
    p_nq = net_sub.add_parser("query", help="1/2 跳图查询")
    p_nq.add_argument("src", nargs="?", default=None)
    p_nq.add_argument("--dst", default=None)
    p_nq.add_argument("--predicate", default=None)
    p_nq.add_argument("--depth", type=int, default=2, choices=[1, 2])
    p_nq.add_argument("--limit", type=int, default=30)
    p_nq.add_argument("--dormant", action="store_true", help="包含休眠关系")
    p_nq.add_argument("--as-of", default=None)
    p_na = net_sub.add_parser("add", help="人工新增一条关系")
    p_na.add_argument("src")
    p_na.add_argument("predicate")
    p_na.add_argument("dst")
    p_na.add_argument("--weight", type=float, default=None)
    p_na.add_argument("--evidence", action="append", default=None)
    p_nx = net_sub.add_parser("extract", help="对已有记忆补抽实体/关系（幂等）")
    p_nx.add_argument("--limit", type=int, default=None)
    p_nx.add_argument("--id", action="append", default=None)
    p_nx.add_argument("--reset", action="store_true", help="先清空派生网络再重算")
    net_sub.add_parser("derive", help="共现 PMI + 传递闭包")
    p_nd = net_sub.add_parser("dormant", help="关系休眠（不删除）")
    p_nd.add_argument("--days", type=int, default=None)
    p_nd.add_argument("--weight", type=float, default=None)
    p_nm = net_sub.add_parser("merge", help="合并两个实体")
    p_nm.add_argument("keep")
    p_nm.add_argument("drop")

    p_soul = sub.add_parser("soul", help="自我层（M17）：宪法 / 叙事 / 未闭环承诺")
    soul_sub = p_soul.add_subparsers(dest="soul_action", required=True)
    p_ss = soul_sub.add_parser("show", help="打印会注入到新会话的自我层文本（--all 看全部增补）")
    p_ss.add_argument("--all", action="store_true", help="不按预算裁：所有增补 + 完整叙事")
    p_ss.add_argument("--budget", type=int, default=None, help="按指定 token 预算渲染，默认配置值")
    soul_sub.add_parser("status", help="版本 / 字数 / 未闭环承诺")
    soul_sub.add_parser("init", help="首次建模板（已有文件不覆盖）")
    p_sd = soul_sub.add_parser("diff", help="当前文件 vs 上一次提交前")
    p_sd.add_argument("--kind", choices=("constitution", "narrative"), default="constitution")
    soul_sub.add_parser("loops", help="只看未闭环承诺")
    p_sa = soul_sub.add_parser("amend", help="宪法增补（相处里长出来的那一句，只追加）")
    p_sa.add_argument("text")
    p_sa.add_argument("--note", default="")
    p_sa.add_argument("--pin", action="store_true", help="钉住：放进常驻席（有期限，到期/超编退席）")
    p_sa.add_argument("--root", action="store_true", help="标成根：永驻，不参与退席（隐含 --pin）")

    p_sn = soul_sub.add_parser("note", help="改叙事里的一段 / 加勾未闭环承诺")
    p_sn.add_argument("--field", default="停在哪")
    p_sn.add_argument("--text", default="")
    p_sn.add_argument("--loop", action="append", default=[])
    p_sn.add_argument("--done", action="append", default=[])

    p_sc = soul_sub.add_parser("commit", help="提交一版宪法或叙事")
    p_sc.add_argument("kind", choices=("constitution", "narrative"))
    p_sc.add_argument("--file", default="")
    p_sc.add_argument("--text", default="")
    p_sc.add_argument("--note", default="")
    p_sc.add_argument("--by", default="mira")

    p_sconf = soul_sub.add_parser("conflicts", help="宪法 vs 记忆：疑似极性冲突（只标记，不改写）")
    p_sconf.add_argument("--scan", action="store_true", help="重新扫一遍并写 soul/conflicts.json")
    p_sconf.add_argument("--limit", type=int, default=20)
    p_sconf.add_argument("--dismiss", default=None, help="驳回一条候选（给 key 或 memory_id），以后不再进清单")
    p_sconf.add_argument("--confirm", default=None, help="确认一条候选（给 key 或 memory_id），保留并标为 confirmed")
    p_sconf.add_argument("--reviews", action="store_true", help="只看审阅记录")

    p_grp = sub.add_parser("group", help="分组与分级（AI 自建目录）")
    grp_sub = p_grp.add_subparsers(dest="group_action", required=True)
    p_gt = grp_sub.add_parser("tree", help="看目录树")
    p_gt.add_argument("--path", default=None)
    p_gg = grp_sub.add_parser("targets", help="看某个分组里有什么")
    p_gg.add_argument("path")
    p_gg.add_argument("--tbl", default=None, choices=["memory", "entity", "trait", "experience"])
    p_gg.add_argument("--limit", type=int, default=100)
    p_ga = grp_sub.add_parser("assign", help="把目标放进分组 / 打等级")
    p_ga.add_argument("target_tbl", choices=["memory", "entity", "trait", "experience"])
    p_ga.add_argument("target_id")
    p_ga.add_argument("--path", default=None)
    p_ga.add_argument("--grade", default=None, choices=["S", "A", "B", "C", "D"])
    p_ga.add_argument("--rationale", default=None)
    p_gr = grp_sub.add_parser("rename", help="重命名分组（子组路径一起改）")
    p_gr.add_argument("ref")
    p_gr.add_argument("name")
    p_gm = grp_sub.add_parser("merge", help="合并两个分组")
    p_gm.add_argument("source")
    p_gm.add_argument("target")
    grp_sub.add_parser("gc", help="归档空分组")
    grp_sub.add_parser("stats", help="分组/分级统计")

    p_sf = sub.add_parser("surface", help="低成本浮现：会话身份卡 / 线索·情境·节律")
    p_sf.add_argument("--session", required=True)
    p_sf.add_argument("--cue", default=None)
    p_sf.add_argument("--context", default=None, help="JSON：cwd/files/command 等情境线索")
    p_sf.add_argument("--mode", default="session_start", choices=["session_start", "turn"])
    p_sf.add_argument("--budget", type=int, default=None)
    p_sf.add_argument("--used", default=None, help="回执：这条浮现后被用到了")
    p_sf.add_argument("--receipts", action="store_true", help="浮现命中率")

    p_wk = sub.add_parser("wake", help="醒来卡：我是谁 / 我们 / 现在 / 下次（只读）")
    p_wk.add_argument("--budget", type=int, default=None)

    p_dt = sub.add_parser("doubt", help="「我到底在哪些问题上没把握」：读 recall.weak 流水")
    p_dt.add_argument("--limit", type=int, default=20)
    p_dt.add_argument("--days", type=int, default=30)

    p_ctx = sub.add_parser("context", help="上下文水位：到线自动交接（估算 + 宿主实测）")
    p_ctx.add_argument("--session", default=None)
    p_ctx.add_argument("--tokens", type=int, default=None, help="这一轮 messages 估算出来的 token 数")
    p_ctx.add_argument("--actual", type=int, default=None, help="宿主 tokenMeter 量出来的真实 token")
    p_ctx.add_argument("--window", type=int, default=None, help="覆盖窗口大小（默认读配置）")
    p_ctx.add_argument("--record", action="store_true", help="写状态；跨档才说话")
    p_ctx.add_argument("--no-auto", dest="no_auto", action="store_true", help="关掉自动交接（设置页开关的旁路）")
    p_ctx.add_argument("--model", default=None, help="当前模型名；用来按 [context.windows] 匹配窗口大小")

    p_lc = sub.add_parser("lifecycle", help="生命周期（按等级淡忘）")
    lc_sub = p_lc.add_subparsers(dest="lifecycle_action", required=True)
    p_lt = lc_sub.add_parser("tick", help="跑一次流转")
    p_lt.add_argument("--dry-run", action="store_true")

    p_sl = sub.add_parser("sleep", help="夜间睡眠（M4）：8 阶段 replay")
    sl_sub = p_sl.add_subparsers(dest="sleep_action", required=True)
    p_sll = sl_sub.add_parser("log", help="最近几次夜间睡眠（每阶段跑了什么）")
    p_sll.add_argument("--limit", type=int, default=14)
    p_slr = sl_sub.add_parser("run", help="跑/续跑一轮睡眠")
    p_slr.add_argument("--day", default=None, help="处理哪一天，默认昨天")
    p_slr.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="有未完成 run 时续跑（默认开，与 SDK/MCP 一致）",
    )
    p_slr.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="忽略未完成的 run，从 s1 重新跑",
    )
    p_slr.add_argument("--dry-run", action="store_true", help="计算后整体回滚，只写报告")
    p_slr.add_argument("--catchup", type=int, default=0, help="补跑最近 N 天")
    p_slr.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_slr.add_argument(
        "--stop-at",
        default=None,
        choices=[
            "s1_collect",
            "s2_dedup",
            "s3_relations",
            "s4_conflicts",
            "s5_patterns",
            "s6_traits",
            "s7_reflect",
            "s8_snapshot",
        ],
        help="演练：在该阶段成功后停下（partial）",
    )
    p_sls = sl_sub.add_parser("status", help="最近一次睡眠状态")
    p_sls.add_argument("--day", default=None)
    p_sls.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_srp = sl_sub.add_parser("report", help="打印最近的睡眠报告（Markdown）")
    p_srp.add_argument("--day", default=None)
    p_srp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_slw = sl_sub.add_parser(
        "whale", help="鲸眠（0.35.0）：开一个「鲸眠」LLM 会话把当天原文消化成日记"
    )
    p_slw.add_argument("--day", default=None, help="处理哪一天，默认昨天")
    p_slw.add_argument("--model", default=None, help="覆盖模型名（默认读 [sleep.whale].model）")
    p_slw.add_argument("--dry-run", action="store_true", help="只跑会话、不落库、不跑固定 8 阶段")
    p_slw.add_argument("--no-sleep", action="store_true", help="只写日记，不接着跑固定 8 阶段")
    p_slw.add_argument("--show", action="store_true", help="跑完把鲸眠会话转录打印出来")
    p_slw.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_slb = sl_sub.add_parser(
        "big", help="大睡一场（0.35.1）：把已有的记忆按天从头睡一遍（默认全部）"
    )
    p_slb.add_argument("--from", dest="start", default=None, help="起始日期 YYYY-MM-DD（含）")
    p_slb.add_argument("--to", dest="end", default=None, help="结束日期 YYYY-MM-DD（含）")
    p_slb.add_argument("--limit", type=int, default=0, help="最多睡几天（默认 0 = 全部）")
    p_slb.add_argument("--dry-run", action="store_true", help="整体演练：不落库，只算")
    p_slb.add_argument("--template", action="store_true", help="强制模板写日记，不调模型")
    p_slb.add_argument("--skip-digested", action="store_true", help="只睡还没消化过的日子")
    p_slb.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_slp = sl_sub.add_parser("plan", help="大睡计划：列出要睡哪些天（只读，不执行）")
    p_slp.add_argument("--from", dest="start", default=None, help="起始日期 YYYY-MM-DD（含）")
    p_slp.add_argument("--to", dest="end", default=None, help="结束日期 YYYY-MM-DD（含）")
    p_slp.add_argument("--limit", type=int, default=0, help="最多几天（默认 0 = 全部）")
    p_slp.add_argument("--skip-digested", action="store_true", help="只列还没消化过的天")
    p_slp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_ev = sub.add_parser("evolve", help="进化引擎（M5）：复核 / 经验复用 / 冲突升级 / 调参")
    ev_sub = p_ev.add_subparsers(dest="evolve_action", required=True)
    p_ev_run = ev_sub.add_parser("run", help="跑一层或全部进化")
    p_ev_run.add_argument(
        "--layer",
        action="append",
        choices=["patterns", "conflicts", "experience", "tuning"],
        default=None,
        help="只跑指定层，可多次传",
    )
    p_ev_run.add_argument("--dry-run", action="store_true")
    p_ev_run.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_ev_rev = ev_sub.add_parser("review", help="看未复核的候选")
    p_ev_rev.add_argument("--tbl", default="pattern", choices=["pattern", "experience", "conflict"])
    p_ev_rev.add_argument("--limit", type=int, default=30)
    p_ev_rev.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_ev_lab = ev_sub.add_parser("label", help="给候选打复核标签")
    p_ev_lab.add_argument("target_tbl", choices=["pattern", "experience", "conflict", "trait"])
    p_ev_lab.add_argument("target_id")
    p_ev_lab.add_argument("verdict", choices=["correct", "wrong", "unsure"])
    p_ev_lab.add_argument("--note", default=None)
    p_ev_lab.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_ev_exp = ev_sub.add_parser("experience", help="经验卡：匹配 / 复用 / 反馈")
    exp_sub = p_ev_exp.add_subparsers(dest="experience_action", required=True)
    p_exm = exp_sub.add_parser("match", help="按情境找可复用经验")
    p_exm.add_argument("situation")
    p_exm.add_argument("--limit", type=int, default=3)
    p_exm.add_argument("--threshold", type=float, default=None)
    p_exm.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_exr = exp_sub.add_parser("reuse", help="记录一次复用")
    p_exr.add_argument("id")
    p_exr.add_argument("--note", default=None)
    p_exr.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_exf = exp_sub.add_parser("feedback", help="记录复用效果")
    p_exf.add_argument("id")
    p_exf.add_argument("signal", choices=["useful", "irrelevant", "wrong"])
    p_exf.add_argument("--note", default=None)
    p_exf.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_ev_tun = ev_sub.add_parser("tuning", help="参数自调")
    tun_sub = p_ev_tun.add_subparsers(dest="tuning_action", required=True)
    p_tun_run = tun_sub.add_parser("run", help="评估候选并只在提升时生效")
    p_tun_run.add_argument("--dry-run", action="store_true")
    p_tun_run.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_tun_rb = tun_sub.add_parser("rollback", help="撤销已生效参数")
    p_tun_rb.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_tun_st = tun_sub.add_parser("status", help="看已生效参数与版本")
    p_tun_st.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_ev_stats = ev_sub.add_parser("stats", help="进化层总览")
    p_ev_stats.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_prof = sub.add_parser("profile", help="记忆画像（M6）：稳定特质 + 目标")
    p_prof.add_argument("--budget", type=int, default=300)
    p_prof.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_rf = sub.add_parser("reflect", help="反思（M7）：质量审计 / 周报 / 历史")
    p_rf.add_argument("--period", default=None, help="day:YYYY-MM-DD 或 week:YYYY-Www，省略=今天")
    p_rf.add_argument("--persist", action="store_true", help="写入 reflection（默认只看不存）")
    p_rf.add_argument("--only", default=None, help="只看某个缺陷码")
    p_rf.add_argument("--limit", type=int, default=20)
    p_rf.add_argument("--history", action="store_true", help="列出历史反思周期")
    p_rf.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_srv = sub.add_parser("serve", help="REST 门面（M8）：POST /v1/memory/<tool>")

    p_daemon = sub.add_parser("daemon", help="长期运行（M9）：调度睡眠/周报/备份/监控/月报")
    p_daemon.add_argument("--once", action="store_true", help="只检查一次（给系统计划任务用）")
    p_daemon.add_argument("--interval", type=int, default=None, help="轮询间隔秒数，默认 [runtime] tick_seconds")
    p_daemon.add_argument("--requeue-dead", action="store_true", help="先把 dead 任务放回队列再跑")

    p_backup = sub.add_parser("backup", help="备份与恢复演练（M9）")
    backup_sub = p_backup.add_subparsers(dest="backup_action", required=True)
    p_bk_run = backup_sub.add_parser("run", help="在线备份一份并校验")
    p_bk_run.add_argument(
        "--kind",
        default="manual",
        choices=["daily", "manual", "pre-migrate"],
        help="备份类别（决定保留池）：默认 manual",
    )
    p_bk_run.add_argument("--keep", type=int, default=None, help="保留多少份 manual 备份")
    p_bk_list = backup_sub.add_parser("list", help="列出备份/快照")
    p_bk_drill = backup_sub.add_parser("drill", help="只读恢复演练：验哈希、事件链、外键、恢复耗时")
    p_bk_drill.add_argument("--source", default=None, help="指定备份文件；默认最近一份")
    p_bk_counts = backup_sub.add_parser("counts", help="按 kind 统计快照数量（看 daily 保留现状）")
    p_bk_counts.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_reidx = sub.add_parser("reindex", help="重建派生索引")
    reidx_sub = p_reidx.add_subparsers(dest="reindex_action", required=True)
    p_reidx_fts = reidx_sub.add_parser(
        "fts", help="重建 FTS 索引：memory_fts_tok / memory_fts_tri / journal_fts / episode_fts"
    )
    p_reidx_fts.add_argument("--dry-run", action="store_true", help="只报当前行数，不重建")

    p_mnt = sub.add_parser("maintenance", help="维护窗口闸：窗口内 daemon 只自愈、不执行任务")
    mnt_sub = p_mnt.add_subparsers(dest="maintenance_action", required=True)
    p_mnt_on = mnt_sub.add_parser("on", help="标记维护中（daemon 暂停执行任务）")
    p_mnt_on.add_argument(
        "--minutes", type=int, default=None, help="窗口分钟数，默认 [runtime] maintenance_minutes（30）"
    )
    p_mnt_on.add_argument("--reason", default="manual maintenance", help="为什么维护（写进标记文件）")
    p_mnt_off = mnt_sub.add_parser("off", help="取消维护标记（幂等）")
    p_mnt_st = mnt_sub.add_parser("status", help="看维护窗口状态")
    for _p in (p_mnt_on, p_mnt_off, p_mnt_st):
        _p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_mon = sub.add_parser("monitor", help="健康检查（M9）")
    p_mon.add_argument("--record", action="store_true", help="把指标写入 metrics")

    p_rep = sub.add_parser("report", help="月报（M9）")
    rep_sub = p_rep.add_subparsers(dest="report_action", required=True)
    p_rep_m = rep_sub.add_parser("monthly", help="生成月报 Markdown")
    p_rep_m.add_argument("--month", default=None, help="YYYY-MM，默认上个月")
    p_rep_m.add_argument("--out", default=None, help="输出目录或 .md 路径")

    p_star = sub.add_parser("star", help="记忆海马体星图（M9）")
    p_star.add_argument("--out", default="data/starmap", help="输出基名，默认 data/starmap")
    p_star.add_argument("--no-png", action="store_true", help="不生成 PNG（只出 HTML/SVG/JSON）")
    p_star.add_argument(
        "--incremental",
        action="store_true",
        help="增量：旧星不动，只写 JSON（页面实时刷新用）",
    )
    p_star.add_argument("--reveal-secrets", action="store_true", help="不隐去 secret 记忆正文")
    p_star.add_argument("--open", action="store_true", help="生成后用浏览器打开 HTML")
    p_srv.add_argument("--host", default=None)
    p_srv.add_argument("--port", type=int, default=None)
    p_srv.add_argument("--token", default=None)

    p_dsh = sub.add_parser("dsh", help="把插件装进 DSH profile（M9.8）")
    dsh_sub = p_dsh.add_subparsers(dest="dsh_cmd", required=True)
    for _name, _help in (
        ("install", "登记 bundle + 按本机路径写 MCP 注册块"),
        ("uninstall", "只摘注册，一条记忆都不动"),
        ("status", "逐条体检安装状态"),
    ):
        _p = dsh_sub.add_parser(_name, help=_help)
        _p.add_argument("--profile", default="desktop", help="profile 名，默认 desktop")
        _p.add_argument("--repo", default=None, help="仓库根，默认按本文件位置推断")
        _p.add_argument("--db", default=None, help="记忆库路径，默认 <repo>/data/pmem.db")
        if _name != "status":
            _p.add_argument("--dry-run", action="store_true", help="只预演，不落盘")
        if _name == "install":
            _p.add_argument("--python", default="python", help="MCP server 用的 python")
            _p.add_argument("--no-init", action="store_true", help="库不存在时不要自动建")

    p_sc = sub.add_parser("schedule", help="Windows 计划任务：daemon 常驻 + 每周 eval（M9.9）")
    sc_sub = p_sc.add_subparsers(dest="schedule_cmd", required=True)
    for _name, _help in (
        ("install", "登记 pmem-daemon（每 N 分钟）与 pmem-eval（每周日）；--autostart 才装登录自启"),
        ("uninstall", "摘掉登记；包装器与日志留着"),
        ("status", "看任务在不在、日志最后写了什么"),
    ):
        _p = sc_sub.add_parser(_name, help=_help)
        _p.add_argument("--repo", default=None, help="仓库根，默认按本文件位置推断")
        _p.add_argument("--db", default=None, help="记忆库路径")
        if _name == "install":
            _p.add_argument("--python", default=None, help="用哪个解释器跑，默认当前解释器")
            _p.add_argument("--every", type=int, default=10, help="daemon 间隔分钟，默认 10")
            _p.add_argument("--no-eval", action="store_true", help="不登记每周 eval")
            _p.add_argument("--autostart", action="store_true", help="装登录自启（启动文件夹 .lnk，默认不装）")
        if _name != "status":
            _p.add_argument("--dry-run", action="store_true", help="只预演，不落盘")

    p_rc = sub.add_parser("receipt", help="浮现回执（M9.9）：这一轮真用到了哪几条浮现")
    p_rc.add_argument("--session", default=None, help="会话 id；只看 --stats 时可省略")
    p_rc.add_argument("--text", default=None, help="这一轮的回答正文")
    p_rc.add_argument("--exclude", default="", help="用户这句话，用来排除「复述题面」")
    p_rc.add_argument("--tokens", type=int, default=None, help="本轮注入的 tokens（采样）")
    p_rc.add_argument("--stats", action="store_true", help="只看 token_per_turn 分布")
    p_rc.add_argument("--dry-run", action="store_true", help="只判定，不落回执")

    p_bn = sub.add_parser("bench", help="长期记忆测试台（M10）：按剧本回放，真模型当被试点")
    bn_sub = p_bn.add_subparsers(dest="bench_cmd", required=True)
    p_br = bn_sub.add_parser("run", help="回放一个剧本")
    p_br.add_argument("--corpus", required=True, help="剧本 JSON 路径")
    p_br.add_argument("--backend", default="stub", choices=["stub", "openai"])
    p_br.add_argument("--model", default=None, help="模型名，默认读 PMEM_BENCH_MODEL")
    p_br.add_argument("--base-url", default=None, help="OpenAI 兼容端点")
    p_br.add_argument("--api-key", default=None, help="凭证，默认读 PMEM_BENCH_API_KEY")
    p_br.add_argument("--days", type=int, default=None, help="只回放前 N 天（冒烟用）")
    p_br.add_argument("--sleep-each-day", action="store_true", help="每天结束跑一次系统睡眠")
    p_br.add_argument("--no-exams", action="store_true", help="只回放，不跑考核")
    p_br.add_argument("--autowrite", action="store_true", help="规则兜底写入（名字/生日/城市/工作/家人）")
    p_br.add_argument("--judge", default=None, choices=["stub", "model"], help="判卷方式，默认 stub")
    p_br.add_argument("--budget", type=int, default=300, help="醒来卡预算")
    p_br.add_argument("--out", default=None, help="报告路径")

    p_tr = sub.add_parser("trait", help="特质（M6）：列表 / 版本 / 时间线 / 指标")
    tr_sub = p_tr.add_subparsers(dest="trait_action", required=True)
    p_tl = tr_sub.add_parser("list", help="列出特质")
    p_tl.add_argument("--status", default=None, choices=["hypothesis", "established", "deprecated"])
    p_tl.add_argument("--scope", default=None, choices=["self", "person", "topic"])
    p_tl.add_argument("--limit", type=int, default=None, help="默认走 SDK 的 50")
    p_tl.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_ts = tr_sub.add_parser("show", help="看某个特质的版本链与时间线")
    p_ts.add_argument("ref", help="trait id 或 key")
    p_ts.add_argument("--limit", type=int, default=50)
    p_ts.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_th = tr_sub.add_parser("history", help="只看事件时间线")
    p_th.add_argument("ref")
    p_th.add_argument("--limit", type=int, default=50)
    p_th.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_tm = tr_sub.add_parser("metrics", help="trace_rate / churn / 分布")
    p_tm.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_bf = sub.add_parser("backfill", help="从外部数据源回填记忆")
    bf_sub = p_bf.add_subparsers(dest="source", required=True)
    p_bf_dsh = bf_sub.add_parser("dsh", help="从 DSH 会话日志回填（只读）")
    p_bf_dsh.add_argument("--root", default=None, help="会话根目录，默认 ~/.dsh/sessions")
    p_bf_dsh.add_argument("--days", type=int, default=3650)
    p_bf_dsh.add_argument("--limit", type=int, default=None)
    p_bf_dsh.add_argument("--scope", default=None, help="作用域键，默认按当前 cwd 计算")
    p_bf_dsh.add_argument("--node", default=None, help="node 可执行文件路径")
    p_bf_dsh.add_argument("--dry-run", action="store_true")
    p_bf_dsh.add_argument("--reset", action="store_true", help="先清掉上一次回填的记忆再重跑")
    p_bf_use = bf_sub.add_parser("usage", help="从日志重算「被用过」：召回/回执/反馈（M9.8b）")
    p_bf_use.add_argument("--dry-run", action="store_true", help="只报告会改多少，不落盘")
    p_bf_tok = bf_sub.add_parser("tokens", help="按当前分词规则重算 text_seg 与 FTS 索引")
    p_bf_tok.add_argument("--dry-run", action="store_true", help="只报告会改多少，不落盘")

    p_jn = sub.add_parser("journal", help="日记（M10）：当天原话 → 一篇日记 → 事实进星图")
    jn_sub = p_jn.add_subparsers(dest="journal_action", required=True)
    p_jt = jn_sub.add_parser("turn", help="一轮的原话，一次性写进流水（插件用，一次进程）")
    p_jt.add_argument("--session", default=None)
    p_jt.add_argument("--lines", required=True, help='JSON 数组：[{"role":"user","text":"..."}]')
    p_ja = jn_sub.add_parser("append", help="记一行流水（原文原样）")
    p_ja.add_argument("--text", required=True, help="这一句原话")
    p_ja.add_argument("--role", default="user", choices=["user", "assistant"])
    p_ja.add_argument("--session", default=None)
    p_ja.add_argument("--ts", default=None, help="ISO 时间，默认现在")
    p_jd = jn_sub.add_parser("digest", help="消化成日记（默认昨天；--model 才真调模型）")
    p_jd.add_argument("--day", default=None, help="YYYY-MM-DD，默认最近一天还有流水的")
    p_jd.add_argument("--model", default=None, help="模型名（不给则用模板兜底，零成本）")
    p_jd.add_argument("--dry-run", action="store_true")
    p_jd.add_argument("--all", action="store_true", help="把所有没消化的日子都走一遍")
    p_jd.add_argument("--prune", type=int, default=None, help="顺带清理超过 N 天的原文")
    p_jd.add_argument("--sleep-check", action="store_true", help="只在空闲/跨天时消化（daemon 用）")
    p_jd.add_argument("--idle", type=int, default=None, help="空闲多少分钟算该睡，默认读配置")
    p_jn.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_pn = jn_sub.add_parser("purge-noise", help="清掉流水里的脚手架台词（只删还没消化的）")
    p_pn.add_argument("--dry-run", action="store_true", help="只报会删多少，不动库")
    p_ri = jn_sub.add_parser("reindex", help="重建原话兜底索引（FTS5 报 malformed 时用）")
    p_ri.add_argument("--dry-run", action="store_true", help="只报行数，不动库")
    p_js = jn_sub.add_parser("status", help="有几篇日记、积压多少流水、该不该睡")
    p_js.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_jr = jn_sub.add_parser("recent", help="最近几天的日记")
    p_jr.add_argument("--limit", type=int, default=7)
    p_jr.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_jrp = jn_sub.add_parser("repair", help="补齐日记声称派生但没写成的事实（幂等重放，可反复跑）")
    p_jrp.add_argument("--dry-run", action="store_true", help="只报告缺多少，不写库")
    p_jrp.add_argument("--limit", type=int, default=50, help="最多检查多少篇日记（默认 50）")
    p_jrp.add_argument("--episode", default=None, help="只修这一篇（episode id）")
    p_jrp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_sh = sub.add_parser("shadow", help="影子模式（只记录不注入）")
    sh_sub = p_sh.add_subparsers(dest="action", required=True)
    for action, helptext in (("run", "跑一轮影子召回"), ("report", "把 shadow 日志汇总成报告")):
        action_parser = sh_sub.add_parser(action, help=helptext)
        action_parser.add_argument("--root", default=None)
        action_parser.add_argument("--days", type=int, default=30)
        action_parser.add_argument("--limit", type=int, default=None)
        action_parser.add_argument("--scope", default=None)
        action_parser.add_argument("--out", default=None)
        action_parser.add_argument("--file", default=None, help="report 时读哪个 shadow 日志")
        action_parser.add_argument("--soul", default=None, help="dsh-soul 库路径")
        action_parser.add_argument("--node", default=None)

    args = parser.parse_args(argv)

    if getattr(args, "now", None):
        from ..base import clock as clock_mod

        clock_mod.set_now(args.now)

    # 默认配置文件（0.36.0）：插件只传 --db，过去不传 --config 就等于永远用内置默认值。
    # 现在没显式给就用 `~/.dsh/pmem/config.toml`；文件不存在 = 行为完全不变。
    if args.config is None:
        args.config = str(default_config_path())

    if args.cmd == "config":
        return _cmd_config(args)

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        return mcp_main(
            [a for a in ["--db", args.db or "", "--config", args.config or ""] if a]
        )

    app = PMem.open(args.db, config_path=args.config, subject=args.subject, scope_key=args.scope_key)
    try:
        if args.cmd == "init":
            app.db.init(subject=args.subject)
            return _emit({"ok": True, "data": app.store.stats()}, args.json)
        if args.cmd == "stats":
            return _emit(app.stats(), args.json)
        if args.cmd == "capabilities":
            return _emit(app.capabilities(), args.json)
        if args.cmd == "doctor":
            return _doctor(app, args.json)
        if args.cmd == "vacuum":
            if not hasattr(app.db, "vacuum"):
                return _emit(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_UNSUPPORTED",
                            "message": "当前版本没有 Database.vacuum；等它落库后再用",
                        },
                    },
                    args.json,
                )
            before = _db_pages(app.db)
            # 显式传 analyze：CLI 的 --analyze 才是开关，不靠 DB 默认值，避免「两个默认」。
            result = app.db.vacuum(analyze=bool(args.analyze))
            after = _db_pages(app.db)
            if result.get("ok") is False:
                return _emit(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_VACUUM",
                            "message": str(result.get("error") or result.get("reason") or "VACUUM 失败"),
                        },
                    },
                    args.json,
                )
            data = {
                "before": {
                    "pages": int(result.get("before_pages", before["pages"])),
                    "bytes": int(result.get("before_bytes", before["bytes"])),
                },
                "after": {
                    "pages": int(result.get("after_pages", after["pages"])),
                    "bytes": int(result.get("after_bytes", after["bytes"])),
                },
                "freed_bytes": int(
                    result.get("saved_bytes", max(0, before["bytes"] - after["bytes"]))
                ),
                "analyze": bool(args.analyze),
                "duration_ms": result.get("duration_ms"),
                "result": result,
            }
            if args.json:
                return _emit({"ok": True, "data": data}, True)
            print(
                f"✓ VACUUM 完成（analyze={'on' if args.analyze else 'off'}）"
                f"  页 {data['before']['pages']} → {data['after']['pages']}"
                f"  库文件 {_fmt_bytes(data['before']['bytes'])} → {_fmt_bytes(data['after']['bytes'])}"
            )
            if data["freed_bytes"]:
                print(f"  含 -wal 共省 {_fmt_bytes(data['freed_bytes'])}")
            return 0
        if args.cmd == "restore":
            try:
                from ..ops.restore import restore_backup
            except ImportError:
                return _emit(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_UNSUPPORTED",
                            "message": "当前版本没有 ops.restore.restore_backup；等它落库后再用",
                        },
                    },
                    args.json,
                )
            db_path = str(app.db.path)
            config = app.config
            app.close()  # 先放掉本进程的文件句柄：Windows 上不关会判「正在使用」
            try:
                result = restore_backup(db_path, args.backup, force=args.force, config=config)
            except Exception as exc:  # noqa: BLE001 恢复失败要给可读原因，不是 traceback
                return _emit(
                    {
                        "ok": False,
                        "error": {"code": "E_RESTORE", "message": f"{type(exc).__name__}: {exc}"},
                    },
                    args.json,
                )
            if result.get("ok") is False:
                return _emit(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_RESTORE",
                            "message": str(result.get("error") or "恢复失败"),
                        },
                        "data": result,
                    },
                    args.json,
                )
            if args.json:
                return _emit({"ok": True, "data": result}, True)
            print(f"✓ 已从备份恢复 → {result.get('db_path', db_path)}")
            print(f"  来源 {result.get('restored_from', args.backup)}")
            if result.get("replaced"):
                print(f"  覆盖了已有库：{result['replaced']}")
            for check in result.get("checks") or []:
                mark = "✓" if check.get("ok") else "✗"
                print(f"  {mark} {check.get('name', '')}: {check.get('detail', '')}")
            print(f"  耗时 {result.get('duration_ms', '-')} ms")
            return 0
        if args.cmd == "eval":
            from ..eval import runner

            out = Path(args.out) if args.out else Path(".tmp/eval")
            if args.net:
                metrics = runner.run_net(seed=args.seed, out_dir=out, edge_count=args.edges)
                _emit({"ok": True, "data": metrics}, args.json)
                return 0 if metrics["ok"] else 1
            metrics = runner.run(seed=args.seed, out_dir=out, k=args.k)
            if args.record:
                from ..ops.metrics import record_eval

                metrics["recorded"] = record_eval(app.db, metrics, subject=args.subject)
            _emit({"ok": True, "data": metrics}, args.json)
            return 0 if metrics["ok"] else 1
        if args.cmd == "feedback":
            from ..memory import feedback as feedback_mod

            result = feedback_mod.apply(app.db, args.id, args.signal, note=args.note)
            return _emit({"ok": True, "data": result}, args.json)
        if args.cmd == "consolidate":
            from ..memory import dup as dup_mod

            near = args.near or app.config.get("dedup.near", 0.92)
            stats = dup_mod.consolidate(app.db, near=near, dry_run=args.dry_run)
            return _emit({"ok": True, "data": stats}, args.json)
        if args.cmd == "conflicts":
            from ..memory import conflict as conflict_mod

            if args.action == "list":
                items = conflict_mod.list_conflicts(app.db, status=args.status, limit=args.limit)
                return _emit({"ok": True, "data": {"items": items}}, args.json)
            if args.action == "detect":
                return _emit({"ok": True, "data": conflict_mod.run(app.db)}, args.json)
            result = conflict_mod.resolve(app.db, args.id, args.decision, note=args.note)
            return _emit({"ok": True, "data": result}, args.json)
        if args.cmd == "net":
            if args.net_action == "stats":
                return _emit(app.net_stats(), args.json)
            if args.net_action == "entities":
                # --limit 默认 None：不传就让 SDK 用自己的默认（50），别在 CLI 里再写一个数。
                kwargs: dict[str, Any] = {"type_": args.type, "query": args.query}
                if args.limit is not None:
                    kwargs["limit"] = args.limit
                return _emit(app.entities(**kwargs), args.json)
            if args.net_action == "query":
                return _emit(
                    app.relation(
                        action="query",
                        src=args.src,
                        dst=args.dst,
                        predicate=args.predicate,
                        depth=args.depth,
                        limit=args.limit,
                        include_dormant=args.dormant,
                        as_of=args.as_of,
                    ),
                    args.json,
                )
            if args.net_action == "add":
                return _emit(
                    app.relation(
                        action="add",
                        src=args.src,
                        dst=args.dst,
                        predicate=args.predicate,
                        weight=args.weight,
                        evidence=args.evidence,
                    ),
                    args.json,
                )
            if args.net_action == "extract":
                return _emit(
                    app.net_backfill(limit=args.limit, memory_ids=args.id, reset=args.reset),
                    args.json,
                )
            if args.net_action == "derive":
                return _emit(app.net_derive(), args.json)
            if args.net_action == "dormant":
                return _emit(app.net_dormant(days=args.days, weight=args.weight), args.json)
            if args.net_action == "merge":
                return _emit(app.net_merge(args.keep, args.drop), args.json)
            parser.error(f"未知 net 子命令 {args.net_action}")
        if args.cmd == "soul":
            from ..identity import soul as soul_mod

            action = args.soul_action
            if action == "show":
                # 默认打的是**真的会注入**的那张卡（按预算裁过）；--all 才是全文。
                # 为什么补这一条：卡上的脚注写着「另有 N 条更早的增补，pmem soul show 可看」，
                # 而 show 以前用的是同一个预算——等于指着自己看不到的东西说「去那儿看」。
                budget = None
                if getattr(args, "all", False):
                    budget = 10_000_000
                elif getattr(args, "budget", None):
                    budget = int(args.budget)
                text = soul_mod.render(app.config, budget=budget)
                if not text:
                    print("（自我层还没建：先 pmem soul init，或直接写 ~/.dsh/pmem/soul/*.md）")
                    return 1
                if args.json:
                    print(json.dumps(
                        {"ok": True, "data": {"text": text, "status": soul_mod.status(app.config)}},
                        ensure_ascii=False,
                        indent=1,
                    ))
                    return 0
                print(text)
                info = soul_mod.status(app.config)
                print(f"— 版本 v{info.get('version')} · {info.get('constitution_tokens')} tok 宪法 / "
                      f"{info.get('narrative_tokens')} tok 叙事 · 未闭环 {len(info.get('open_loops') or [])} 条")
                return 0
            if action == "status":
                data = soul_mod.status(app.config)
                if args.json:
                    print(json.dumps(data, ensure_ascii=False, indent=1))
                else:
                    print(f"— 自我层 {data['dir']}")
                    print(f"  存在：{data['exists']}  版本：v{data.get('version')}  更新：{data.get('updated_at')}")
                    print(f"  宪法 {data['constitution_tokens']} tok / 增补 {data.get('amendments', 0)} 条 / "
                          f"叙事 {data['narrative_tokens']} tok")
                    print(f"  段落：{'、'.join(data.get('sections') or []) or '—'}")
                    for item in data.get("open_loops") or []:
                        print(f"  [ ] {item}")
                return 0
            if action == "init":
                tpl = soul_mod.TEMPLATE
                result = soul_mod.bootstrap(
                    app.config,
                    constitution=tpl["constitution"],
                    narrative=tpl["narrative"],
                )
                print(f"— 自我层目录 {result['dir']}；新建 {result['created'] or '无（已存在，不覆盖）'}")
                return 0
            if action == "diff":
                text = soul_mod.diff(app.config, kind=getattr(args, "kind", "constitution"))
                print(text or "（没有历史版本可对比）")
                return 0
            if action == "loops":
                data = soul_mod.status(app.config)
                loops = data.get("open_loops") or []
                print("\n".join(f"- [ ] {item}" for item in loops) or "（没有未闭环承诺）")
                return 0
            if action == "amend":
                result = soul_mod.amend(
                    args.text,
                    cfg=app.config,
                    note=args.note,
                    pin=bool(getattr(args, "pin", False)) or bool(getattr(args, "root", False)),
                    root=bool(getattr(args, "root", False)),
                )
                if result.get("duplicate"):
                    print("— 这条增补已经存在，没有重复追加")
                else:
                    print(f"— 已增补（v{result['version']}，sha {result['sha']}）")
                return 0
            if action == "note":
                changed = 0
                if args.text:
                    soul_mod.set_section("narrative", args.field, args.text, cfg=app.config)
                    changed += 1
                for item in args.loop or []:
                    soul_mod.add_loop(item, cfg=app.config)
                    changed += 1
                for item in args.done or []:
                    result = soul_mod.check_loop(item, cfg=app.config)
                    changed += 1 if result.get("ok") else 0
                print(f"— 自我层已更新（{changed} 处）")
                return 0
            if action == "commit":
                if args.file:
                    body = Path(args.file).read_text(encoding="utf-8")
                else:
                    body = args.text
                result = soul_mod.commit(
                    args.kind, body, cfg=app.config, note=args.note, author=getattr(args, "by", "mira")
                )
                print(f"— 已提交 {result['kind']} v{result['version']}（sha {result['sha']}）→ {result['dir']}")
                return 0
            if action == "conflicts":
                from ..identity import guard as guard_mod

                if args.dismiss or args.confirm:
                    target = args.dismiss or args.confirm
                    status = "dismissed" if args.dismiss else "confirmed"
                    # 先按 key 试，找不到再按 memory_id 整批标（用户手上的通常是记忆 id）
                    result = guard_mod.review(app.config, key=target, status=status)
                    if not result.get("ok"):
                        result = guard_mod.review(app.config, memory_id=target, status=status)
                    if args.json:
                        return _emit({"ok": bool(result.get("ok")), "data": result}, True)
                    if not result.get("ok"):
                        print(f"✗ 没标上：{result.get('reason')}")
                        return 1
                    print(f"— 已标记 {result['count']} 条为 {result['status']}")
                    return 0
                if args.reviews:
                    data = guard_mod.reviews(app.config)
                    if args.json:
                        return _emit({"ok": True, "data": {"reviews": data}}, True)
                    if not data:
                        print("— 还没有审阅记录")
                        return 0
                    for key, info in list(data.items())[-args.limit:]:
                        print(f"{key}  {info.get('status')}  {info.get('note') or ''}")
                    return 0
                scan_info = None
                if args.scan:
                    scan_info = guard_mod.scan(app.db, cfg=app.config)
                data = guard_mod.load(app.config)
                items = (data.get("items") or [])[: max(1, int(args.limit))]
                if args.json:
                    return _emit(
                        {"ok": True, "data": {"scan": scan_info, "count": data.get("count"), "items": items}},
                        True,
                    )
                if not items:
                    print("— 没有发现疑似冲突（只做词面极性检测，漏检是正常的）")
                    return 0
                for item in items:
                    print(f"[{item.get('score')}] {item.get('memory_id')}  {item.get('memory_text')[:40]}")
                    print(f"    宪法：{item.get('clause')}（{item.get('source')}）")
                    print(f"    共享实词：{'、'.join(item.get('shared') or [])}")
                if scan_info:
                    print(f"— 已重扫：{scan_info['count']} 条 → {scan_info['path']}")
                print("— 只标记、不改写：宪法 > 记忆，但「>」指的是不覆盖，不是静默丢弃。")
                return 0
            return 2
        if args.cmd == "group":
            if args.group_action == "tree":
                return _emit(app.group(action="tree", path=args.path), args.json)
            if args.group_action == "targets":
                return _emit(
                    app.group(action="targets", path=args.path, target_tbl=args.tbl, limit=args.limit),
                    args.json,
                )
            if args.group_action == "assign":
                return _emit(
                    app.group(
                        action="assign",
                        target_tbl=args.target_tbl,
                        target_id=args.target_id,
                        path=args.path,
                        grade=args.grade,
                        rationale=args.rationale,
                    ),
                    args.json,
                )
            if args.group_action == "rename":
                return _emit(app.group(action="rename", ref=args.ref, name=args.name), args.json)
            if args.group_action == "merge":
                return _emit(
                    app.group(action="merge", source=args.source, target=args.target), args.json
                )
            if args.group_action == "gc":
                return _emit(app.group(action="gc"), args.json)
            if args.group_action == "stats":
                return _emit(app.group(action="stats"), args.json)
            parser.error(f"未知 group 子命令 {args.group_action}")
        if args.cmd == "surface":
            if args.receipts:
                return _emit(app.surface_receipts(session_id=args.session), args.json)
            if args.used:
                return _emit(
                    app.surface_used(session_id=args.session, memory_id=args.used), args.json
                )
            context = None
            if args.context:
                try:
                    context = json.loads(args.context)
                except json.JSONDecodeError:
                    print("✗ --context 需要 JSON，例如 '{\"cwd\":\"F:/Ai/AWB\"}'", file=sys.stderr)
                    return 2
            result = app.surface(
                session_id=args.session,
                cue=args.cue,
                context=context,
                mode=args.mode,
                budget=args.budget,
            )
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            text = PMem.render_surface(result["data"])
            if text:
                print(text)
            else:
                print(f"— 不浮现（{result['data'].get('note') or '无'}）")
            return 0
        if args.cmd == "wake":
            result = app.wake(budget=args.budget)
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            text = result["data"].get("text") or ""
            print(text if text else "— 还没有可唤醒的记忆")
            return 0
        if args.cmd == "doubt":
            result = app.doubt(limit=args.limit, days=args.days)
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            items = result["data"].get("items") or []
            if not items:
                print("— 最近没有「没把握」的记录")
                return 0
            for item in items:
                reasons = "/".join(item.get("reasons") or []) or "-"
                print(f"{item['ts'][:19]}  [{reasons}]  {item.get('query') or ''}")
            return 0
        if args.cmd == "context":
            from .. import context as context_mod

            if args.tokens is not None or args.actual is not None:
                if args.record:
                    out = context_mod.record(
                        args.session or "cli",
                        int(args.tokens or 0),
                        cfg=app.config,
                        window=args.window,
                        actual=args.actual,
                        auto=False if getattr(args, "no_auto", False) else None,
                        model=getattr(args, "model", None),
                    )
                else:
                    found = context_mod.status(
                        int(args.tokens or 0),
                        cfg=app.config,
                        window=args.window,
                        actual=args.actual,
                        model=getattr(args, "model", None),
                    )
                    out = {
                        "status": found,
                        "session": args.session or "cli",
                        "speak": False,
                        "text": context_mod.render(found, cfg=app.config),
                        "crossed": "",
                    }
                result = {"ok": True, "data": out}
                if args.json:
                    return _emit(result, True)
                found = out["status"]
                print(
                    f"— 上下文约 {found['percent']}%"
                    f"（{found['used']}/{found['window']}，含底座 {found['base']}；"
                    f"messages 估算 {found['tokens']}）"
                )
                if out.get("crossed"):
                    print(f"  跨档：{out['crossed']}")
                if out.get("text"):
                    print(out["text"])
                if out.get("auto", {}).get("do"):
                    print("— 自动交接已触发：插件会写「停在哪」并压缩上下文（设置里可关）")
                return 0
            report = context_mod.report(session_id=args.session, cfg=app.config)
            if args.json:
                return _emit({"ok": True, "data": report}, True)
            sessions = report.get("sessions") or []
            if not sessions:
                print("— 还没有水位记录（插件每轮会写一条）")
                return 0
            for item in sessions:
                print(
                    f"{item.get('updated_at', '')[:19]}  {item.get('session')}  "
                    f"{item.get('percent')}%（{item.get('used')}/{item.get('window')}）"
                    f"  {item.get('level')}"
                )
            return 0
        if args.cmd == "lifecycle":
            return _emit(app.lifecycle(dry_run=args.dry_run), args.json)
        if args.cmd == "sleep":
            if args.sleep_action == "log":
                result = app.sleep_history(limit=getattr(args, "limit", 14) or 14)
                if getattr(args, "json", False):
                    return _emit(result, True)
                runs = result["data"]["runs"]
                if not runs:
                    print("— 还没有睡眠记录：跑一次 `pmem sleep run` 就有了")
                    return 0
                for run in runs:
                    print(f"— {run['day']} · {run['status']} · {run['trigger']} · {run['started_at']}")
                    for stage in run["stages"]:
                        name = stage.get("name") or "?"
                        rest = {k: v for k, v in stage.items() if k != "name"}
                        print(f"    {name}: {str(rest)[:110]}")
                    if run.get("error"):
                        print(f"    错误：{run['error']}")
                return 0
            if args.sleep_action == "plan":
                result = app.sleep_plan(
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                    skip_digested=args.skip_digested,
                )
                if getattr(args, "json", False):
                    return _emit(result, True)
                if not result.get("ok"):
                    return _emit(result, False)
                days = result["data"]["days"]
                print(f"— 大睡计划：{len(days)} 天")
                for day in days:
                    print(f"  {day}")
                return 0
            if args.sleep_action == "whale":
                result = app.sleep_whale(
                    day_key=args.day,
                    model=args.model,
                    dry_run=args.dry_run,
                    run_sleep=not args.no_sleep,
                )
                if getattr(args, "json", False):
                    return _emit(result, True)
                if not result.get("ok"):
                    return _emit(result, False)
                data = result["data"]
                print(
                    f"— 鲸眠 · {data.get('day_key')} · {data.get('verdict')} · "
                    f"回合 {data.get('replies')} · token {data.get('usage_total')}"
                )
                diary = data.get("diary") or {}
                if diary.get("episode_id"):
                    print(
                        f"    日记：{diary.get('title')}（{diary.get('lines')} 行，"
                        f"派生事实 {len(diary.get('facts') or [])} 条，模式 {diary.get('mode')}）"
                    )
                elif diary.get("reason"):
                    print(f"    日记没写成：{diary.get('reason')}")
                if data.get("sleep"):
                    stages = data["sleep"].get("stages") or []
                    print(f"    固定 8 阶段：{len(stages)} 个完成")
                print(f"    转录：{data.get('transcript')}")
                if args.show:
                    from pathlib import Path as _Path

                    text = _Path(str(data.get("transcript"))).read_text(encoding="utf-8")
                    print("---- 鲸眠会话（末尾） ----")
                    print("\n".join(text.splitlines()[-30:]))
                return 0
            if args.sleep_action == "big":
                result = app.sleep_big(
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                    dry_run=args.dry_run,
                    use_whale=not args.template,
                    skip_digested=args.skip_digested,
                    # --json 时不能打进度行，否则 stdout 不是纯 JSON（插件 callPmem 会解析失败）
                    on_day=None if getattr(args, "json", False) else _print_big_day,
                )
                if getattr(args, "json", False):
                    return _emit(result, True)
                if not result.get("ok"):
                    return _emit(result, False)
                data = result["data"]
                print(
                    f"— 大睡一场：{data.get('days')} 天（鲸眠 {data.get('whale_days')} / "
                    f"模板 {data.get('template_days')} / 跳过 {data.get('skipped_days')} / "
                    f"失败 {data.get('failed_days')}），token {data.get('tokens')}，"
                    f"用时 {data.get('elapsed_s')}s，模式 {data.get('mode')}"
                )
                if data.get("note"):
                    print(f"  注意：{data['note']}")
                return 0
            if args.sleep_action == "run":
                result = app.sleep(
                    day_key=args.day,
                    resume=args.resume,
                    dry_run=args.dry_run,
                    catchup=args.catchup,
                    stop_at=args.stop_at,
                )
                if args.json:
                    return _emit(result, True)
                if not result.get("ok"):
                    return _emit(result, False)
                _print_sleep(result["data"])
                return 0 if result["data"].get("status") in ("ok", "dry_run") else 1
            if args.sleep_action == "status":
                return _emit(app.sleep_status(day_key=args.day), args.json)
            status = app.sleep_status(day_key=args.day)
            report = (status.get("data") or {}).get("report_path")
            if not report:
                print("— 还没有睡眠报告")
                return 1
            print(Path(report).read_text(encoding="utf-8"))
            return 0
        if args.cmd == "evolve":
            action = args.evolve_action
            if action == "log":
                result = app.sleep_history(limit=args.limit)
                if args.json:
                    return _emit(result, True)
                runs = result["data"]["runs"]
                if not runs:
                    print("— 还没有睡眠记录：跑一次 `pmem sleep run` 就有了")
                    return 0
                for run in runs:
                    print(f"— {run['day']} · {run['status']} · {run['trigger']} · {run['started_at']}")
                    for stage in run["stages"]:
                        name = stage.get("name") or stage.get("stage") or "?"
                        print(f"    {name}: {str({k: v for k, v in stage.items() if k != 'name'})[:110]}")
                    if run.get("error"):
                        print(f"    错误：{run['error']}")
                return 0

            if action == "run":
                result = app.evolve(action="run", layers=args.layer, dry_run=args.dry_run)
            elif action == "review":
                result = app.evolve(action="review", target_tbl=args.tbl, limit=args.limit)
            elif action == "label":
                result = app.evolve(
                    action="label",
                    target_tbl=args.target_tbl,
                    target_id=args.target_id,
                    verdict=args.verdict,
                    note=args.note,
                )
            elif action == "experience":
                if args.experience_action == "match":
                    result = app.evolve(
                        action="experience_match",
                        situation=args.situation,
                        limit=args.limit,
                        threshold=args.threshold,
                    )
                elif args.experience_action == "reuse":
                    result = app.evolve(
                        action="experience_reuse", target_id=args.id, note=args.note
                    )
                else:
                    result = app.evolve(
                        action="experience_feedback",
                        target_id=args.id,
                        signal=args.signal,
                        note=args.note,
                    )
            elif action == "tuning":
                if args.tuning_action == "run":
                    result = app.evolve(action="tuning", dry_run=args.dry_run)
                elif args.tuning_action == "rollback":
                    result = app.evolve(action="tuning_rollback")
                else:
                    result = app.evolve(action="tuning_status")
            elif action == "stats":
                result = app.evolve(action="stats")
            else:
                parser.error(f"未知 evolve 子命令 {action}")
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            _print_evolve(result["data"])
            return 0
        if args.cmd == "profile":
            result = app.profile(budget=args.budget)
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            print(result["data"]["rendered"])
            return 0
        if args.cmd == "daemon":
            from ..ops import jobs, scheduler

            if args.requeue_dead:
                from datetime import UTC, datetime

                jobs.requeue_dead(app.db, now=datetime.now(UTC))
            if args.once or args.json:
                result = scheduler.tick(
                    app.db, config=app.config, subject=args.subject, runner=None,
                    max_jobs=8,
                )
                if args.json:
                    return _emit({"ok": result["ok"], "data": result}, True)
                _print_tick(result, heartbeat=True)
                return 0 if result["ok"] else 1
            try:
                scheduler.serve(
                    app.db,
                    config=app.config,
                    subject=args.subject,
                    interval=args.interval,
                    on_tick=_print_tick,
                )
            except KeyboardInterrupt:
                return 0
            return 0
        if args.cmd == "backup":
            from ..ops import backup

            action = args.backup_action
            if action == "run":
                result = backup.run(
                    app.db,
                    config=app.config,
                    subject=args.subject,
                    kind=args.kind,
                    tag=args.kind,
                    keep=args.keep,
                )
            elif action == "drill":
                result = backup.drill(app.db, config=app.config, subject=args.subject, source=args.source)
            elif action == "counts":
                result = {"counts": backup.counts(app.db)}
            else:
                result = {"items": backup.list_backups(app.db), "counts": backup.counts(app.db)}
            if args.json:
                return _emit({"ok": True, "data": result}, True)
            if action in ("list", "counts"):
                counts = result["counts"]
                if action == "list":
                    for item in result["items"]:
                        print(
                            f"  {item['created_at'][:19]}  {item['kind']:<9}  "
                            f"{item['memory_count']:>4} 条  {item['size_bytes'] / 1024:.0f} KB  "
                            f"{'✓' if item['verified_at'] else '—'}  {item['path']}"
                        )
                    if not result["items"]:
                        print("— 还没有备份/快照")
                print(f"  合计 {counts['total']} 份：manual {counts.get('manual', 0)} / "
                      f"daily {counts.get('daily', 0)} / monthly {counts.get('monthly', 0)} / "
                      f"pre-migrate {counts.get('pre-migrate', 0)}")
                print(
                    f"  保留策略：manual {int(app.config.get('runtime.backup_keep', 14) or 14)} 份 / "
                    f"daily {int(app.config.get('runtime.daily_keep', 7) or 7)} 份"
                    "（daemon 04:00 离线备份与睡眠每日副本分池各留，不互相挤）"
                )
                return 0
            if action == "run":
                print(
                    f"✓ 备份 {result['snapshot_id']}  {result['memory_count']} 条  "
                    f"{result['size_bytes'] / 1024:.0f} KB  {result['duration_ms']} ms  "
                    f"kind={result.get('kind', '')}"
                )
                print(f"  {result['path']}")
                print(f"  sha256 {result['sha256']}")
                if result.get("pruned"):
                    print(f"  清理旧快照 {len(result['pruned'])} 份")
                return 0
            ok = result["ok"]
            print(("✓ " if ok else "✗ ") + "恢复演练" + ("通过" if ok else "失败"))
            for check in result["checks"]:
                print(f"  {'✓' if check['ok'] else '✗'} {check['name']:<22} {check['detail']}")
            print(f"  恢复耗时 {result['restore_ms']} ms · 来源 {result['source']}")
            return 0 if ok else 1
        if args.cmd == "reindex":
            if args.reindex_action == "fts":
                data = _reindex_fts(app, dry_run=bool(args.dry_run))
                if args.json:
                    return _emit({"ok": True, "data": data}, True)
                verb = "当前" if args.dry_run else "重建后"
                print(f"— FTS 索引（{verb}文档数 / 基表行数）：")
                for name in ("memory_fts_tok", "memory_fts_tri", "journal_fts", "episode_fts"):
                    item = data[name]
                    if item.get("skipped"):
                        print(f"  {name:<16} 跳过：{item['skipped']}")
                        continue
                    if args.dry_run:
                        print(f"  {name:<16} {item.get('docs')} 篇 / 基表 {item.get('rows')} 行")
                    else:
                        print(
                            f"  {name:<16} {item.get('docs_before')} → {item.get('docs')} 篇"
                            f" / 基表 {item.get('rows')} 行"
                        )
                return 0
            return _emit(
                {
                    "ok": False,
                    "error": {
                        "code": "E_UNSUPPORTED",
                        "message": f"未知 reindex 子命令 {args.reindex_action}",
                    },
                },
                args.json,
            )
        if args.cmd == "maintenance":
            from ..ops import maintenance

            action = args.maintenance_action
            if action == "on":
                minutes = (
                    args.minutes
                    if args.minutes is not None
                    else int(app.config.get("runtime.maintenance_minutes", 30) or 30)
                )
                result = maintenance.mark(
                    app.db, config=app.config, reason=args.reason, minutes=minutes
                )
            elif action == "off":
                result = maintenance.clear(app.db, config=app.config)
            else:
                result = maintenance.active(app.db, config=app.config)
            if args.json:
                return _emit({"ok": True, "data": result}, True)
            _print_maintenance(action, result)
            return 0 if result.get("ok", True) else 1
        if args.cmd == "monitor":
            from ..ops import monitor

            result = monitor.collect(app.db, config=app.config, subject=args.subject, record=args.record)
            if args.json:
                return _emit({"ok": result["ok"], "data": result}, True)
            print(monitor.render(result))
            return 0 if result["ok"] else 1
        if args.cmd == "report":
            from ..ops import monthly

            try:
                result = monthly.generate(
                    app.db, config=app.config, subject=args.subject, month=args.month, out=args.out
                )
            except ValueError as exc:
                print(f"✗ {exc}", file=sys.stderr)
                return 1
            if args.json:
                return _emit({"ok": True, "data": result}, True)
            print(result["rendered"])
            print(f"— 已写入 {result['path']}")
            return 0
        if args.cmd == "dsh":
            from . import dsh_install

            action = args.dsh_cmd
            if action == "uninstall":
                report = dsh_install.uninstall(
                    profile=args.profile, dry_run=getattr(args, "dry_run", False)
                )
            elif action == "status":
                report = dsh_install.status(
                    profile=args.profile, repo=args.repo, db=args.db
                )
            else:
                report = dsh_install.install(
                    profile=args.profile,
                    repo=args.repo,
                    db=args.db,
                    python=args.python,
                    dry_run=getattr(args, "dry_run", False),
                    make_db=not args.no_init,
                )
            if args.json:
                return _emit({"ok": report.ok, "data": report.to_dict()}, True)
            print(report.render())
            if any(step.action == "missing" for step in report.steps):
                return 1
            return 0
        if args.cmd == "schedule":
            from . import dsh_install
            from ..ops import tasks as tasks_mod

            repo = args.repo or str(dsh_install.repo_root())
            db_path = args.db or str(Path(repo) / "data" / "pmem.db")
            action = args.schedule_cmd
            if action == "install":
                report = tasks_mod.install(
                    repo=repo,
                    db=db_path,
                    python=args.python,
                    minutes=args.every,
                    weekly_eval=not args.no_eval,
                    autostart=bool(getattr(args, "autostart", False)),
                    dry_run=getattr(args, "dry_run", False),
                )
            elif action == "status":
                report = tasks_mod.status()
            else:
                report = tasks_mod.uninstall(dry_run=getattr(args, "dry_run", False))
            data = report.as_dict()
            if args.json:
                return _emit({"ok": report.ok, "data": data}, True)
            print(f"计划任务：{data['home']}")
            for step in data["steps"]:
                print(f"  {'✓' if step['ok'] else '✗'} {step['name']}: {step['detail']}")
            return 0 if report.ok else 1
        if args.cmd == "bench":
            from ..bench import load_corpus, make_model, replay
            from ..bench.score import ModelJudge, StubJudge

            corpus = load_corpus(args.corpus)
            model = make_model(
                args.backend,
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
            )
            use_model_judge = (args.judge or "stub") == "model"
            judge = ModelJudge(model) if use_model_judge else StubJudge()
            result = replay(
                app,
                corpus,
                model,
                limit_days=args.days,
                sleep_each_day=args.sleep_each_day,
                budget=args.budget,
                exams=not args.no_exams,
                judge=judge,
                autowrite=args.autowrite,
                exam_dir=Path("data/bench") / f"{corpus.persona.id}-exams",
            )
            stamp = _datetime.now().strftime("%Y%m%d-%H%M%S")
            out = args.out or f"data/bench/{corpus.persona.id}-{stamp}.json"
            path = result.write_report(out)
            summary = result.summary()
            if args.json:
                return _emit({"ok": True, "data": summary | {"report": str(path)}}, True)
            print(f"回放 {summary['days']} 天 {summary['turns']} 轮 · 工具 {summary['tool_calls']} 次")
            tokens = summary["tokens"]
            print(
                f"tokens：输入 {tokens['prompt_tokens']} · 输出 {tokens['completion_tokens']}"
                f" · 合计 {tokens['total']}"
                + ("（估算：桩模型）" if tokens.get("estimated") else "")
            )
            per_turn = summary["tokens_per_turn"]
            if per_turn.get("samples"):
                print(f"每轮 tokens：均值 {per_turn['mean']} · p95 {per_turn['p95']}")
            injected = summary["injected_per_turn"]
            if injected.get("samples"):
                print(f"注入 tokens：均值 {injected['mean']} · p95 {injected['p95']}")
            exam = summary.get("exam")
            if exam:
                print(
                    f"考核 {exam['questions']} 题：正确 {exam['classes']['correct']} · 部分 "
                    f"{exam['classes']['partial']} · 编造 {exam['classes']['fabricated']} · "
                    f"不确定 {exam['classes']['uncertain']} · 准确率 {exam['accuracy']}"
                )
            print(f"报告：{path}")
            return 0
        if args.cmd == "journal":
            from ..memory import journal as journal_mod

            action = args.journal_action
            if action == "turn":
                import json as _json

                try:
                    lines = _json.loads(args.lines)
                except ValueError:
                    print("✗ --lines 必须是 JSON 数组", file=sys.stderr)
                    return 2
                if not isinstance(lines, list):
                    print("✗ --lines 必须是 JSON 数组", file=sys.stderr)
                    return 2
                added = []
                for item in lines[:12]:
                    if not isinstance(item, dict):
                        continue
                    res = journal_mod.append(
                        app.db,
                        str(item.get("text") or ""),
                        subject=app.subject,
                        session_id=args.session,
                        role=str(item.get("role") or "user"),
                    )
                    if res.get("ok"):
                        added.append(res["id"])
                if args.json:
                    return _emit({"ok": True, "data": {"added": added}}, True)
                print(f"✓ 流水 +{len(added)}")
                return 0

            if action == "append":
                line = journal_mod.append(
                    app.db,
                    args.text,
                    subject=app.subject,
                    session_id=args.session,
                    role=args.role,
                    ts=args.ts,
                )
                if args.json:
                    return _emit({"ok": True, "data": line}, True)
                if not line.get("ok"):
                    print(f"✗ 没记下来：{line.get('reason')}", file=sys.stderr)
                    return 2
                print(f"✓ 流水 +1（{line['day_key']} · {line['role']}）")
                return 0

            if action == "purge-noise":
                result = journal_mod.purge_noise(
                    app.db, subject=app.subject, dry_run=bool(args.dry_run)
                )
                if args.json:
                    return _emit({"ok": True, "data": result}, True)
                verb = "会删" if result["dry_run"] else "已删"
                print(
                    f"— 扫了 {result['scanned']} 行未消化流水，{verb} {result['noise'] if result['dry_run'] else result['deleted']} 行脚手架台词"
                )
                for line in result.get("sample") or []:
                    print(f"    {line}")
                return 0

            if action == "reindex":
                result = journal_mod.rebuild_index(app.db, dry_run=bool(args.dry_run))
                if args.json:
                    return _emit({"ok": True, "data": result}, True)
                if result["dry_run"]:
                    print(f"— 干跑：{result['lines']} 行原话待重建")
                else:
                    print(f"✓ 原话索引已重建：{result['indexed']}/{result['lines']} 行")
                return 0

            if action == "repair":
                result = journal_mod.repair(
                    app.db,
                    subject=app.subject,
                    episode_id=args.episode,
                    dry_run=bool(args.dry_run),
                    limit=int(args.limit or 50),
                    store=app.store,
                    config=app.config,
                )
                if args.json:
                    return _emit({"ok": True, "data": result}, True)
                verb = "会补" if result["dry_run"] else "已补"
                print(
                    f"— 查了 {result['episodes']} 篇日记，{result['gaps']} 篇有缺口："
                    f"缺 {result['missing']} 条，{verb} {result['added']} 条，失败 {result['failed']} 条"
                )
                if result.get("dropped"):
                    print(f"  （另有 {result['dropped']} 条写过、后来被删，按删除处理，不复活）")
                if result.get("unrepairable"):
                    print(f"  ! {result['unrepairable']} 篇的原文已清理，重建不出事实清单")
                if result.get("remaining"):
                    print(f"  ! {result['remaining']} 篇仍对不上：先看上面的失败原因，再跑一次")
                for err in result.get("errors") or []:
                    print(
                        f"    ✗ #{err.get('index')} {str(err.get('text') or '')[:24]}："
                        f"{err.get('error')}"
                    )
                return 1 if result.get("failed") else 0

            if action == "digest":
                idle = args.idle if args.idle is not None else int(app.config.get("journal.idle_minutes", 45) or 45)
                if args.sleep_check:
                    verdict = journal_mod.due(app.db, subject=app.subject, idle_minutes=idle)
                    if not verdict.get("due"):
                        if args.json:
                            return _emit({"ok": True, "data": {"skipped": verdict}}, True)
                        return 0
                days = journal_mod.pending_days(app.db, subject=app.subject) if args.all else [args.day]
                if not days or days == [None]:
                    days = journal_mod.pending_days(app.db, subject=app.subject)[-1:] or [None]
                writer = None
                if args.model:
                    from ..bench.model import make_model

                    client = make_model(
                        "openai", model=None if args.model in ("openai", "default") else args.model
                    )

                    def writer(rows: list[Any], _client=client) -> Any:  # noqa: F811
                        return journal_mod.model_draft(
                            _client, rows, day_key=rows[0]["day_key"] if rows else ""
                        )

                results = []
                for day in days:
                    results.append(
                        journal_mod.digest(
                            app.db,
                            subject=app.subject,
                            day_key=day,
                            writer=writer,
                            store=app.store,
                            config=app.config,
                            dry_run=args.dry_run,
                        )
                    )
                pruned = 0
                if args.prune and not args.dry_run:
                    pruned = journal_mod.prune(app.db, subject=app.subject, keep_days=args.prune)
                if args.json:
                    return _emit({"ok": True, "data": {"results": results, "pruned": pruned}}, True)
                for item in results:
                    if not item.get("ok"):
                        print(f"— 跳过 {item.get('day_key')}：{item.get('reason')}")
                        continue
                    tag = "（预演）" if args.dry_run else ""
                    print(f"✓ {item['day_key']} 日记{tag}：{item['title']} · {item['lines']} 行 · {len(item['memory_ids'])} 条事实")
                    for line in str(item["summary"]).splitlines():
                        print(f"    {line}")
                    if item.get("problems"):
                        print(f"    ! 校验回落：{', '.join(item['problems'][:3])}")
                if pruned:
                    print(f"✓ 清理原文 {pruned} 行")
                return 0

            if action == "status":
                data = journal_mod.stats(app.db, subject=app.subject)
                idle = int(app.config.get("journal.idle_minutes", 45) or 45)
                data["due"] = journal_mod.due(app.db, subject=app.subject, idle_minutes=idle)
                if args.json:
                    return _emit({"ok": True, "data": data}, True)
                print(
                    f"日记 {data['episodes']} 篇 · 流水 {data['lines']} 行（未消化 {data['pending']}）"
                )
                if data["pending_days"]:
                    print(f"  待消化：{', '.join(data['pending_days'])}")
                verdict = data["due"]
                if verdict.get("due"):
                    print(f"  该睡了：{verdict['reason']}（{verdict.get('day_key')}）")
                else:
                    print(f"  现在不用睡：{verdict.get('reason')}")
                return 0

            if action == "recent":
                rows = journal_mod.recent(app.db, subject=app.subject, items=args.limit)
                if args.json:
                    return _emit({"ok": True, "data": rows}, True)
                if not rows:
                    print("还没有日记。")
                    return 0
                for row in rows:
                    print(f"{row['day_key']}  {row['title']}")
                    for line in str(row["summary"] or "").splitlines():
                        print(f"    {line}")
                return 0

        if args.cmd == "receipt":
            from ..memory import receipt as receipt_mod

            # 注意用 `is None`：插件在中断的轮次里会传空串，那是「有这一轮、但没正文」，
            # 还要记 token 样本，不能掉进 stats 分支。
            if args.stats or args.text is None:
                stats = receipt_mod.token_stats(app.db)
                if args.json:
                    return _emit({"ok": True, "data": stats}, True)
                if not stats.get("samples"):
                    print("还没有 token_per_turn 样本：插件每轮回调一次 receipt 才开始记。")
                    print(f"（门：均值 ≤{stats.get('budget_mean', 600)} · p95 ≤{stats.get('budget_p95', 800)}）")
                    return 0
                print(
                    f"token_per_turn（近 {stats['days']} 天 {stats['samples']} 个样本）："
                    f"均值 {stats['mean']} · p50 {stats['p50']} · p95 {stats['p95']} · 最大 {stats['max']}"
                )
                print(f"  门：均值 ≤{stats['budget_mean']} · p95 ≤{stats['budget_p95']}")
                return 0
            if not args.session:
                print("✗ 判定回执需要 --session", file=sys.stderr)
                return 2
            result = app.receipt(
                session_id=args.session,
                answer=args.text,
                exclude=args.exclude,
                tokens=args.tokens,
                dry_run=args.dry_run,
            )
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            data = result["data"]
            matched = data.get("matched") or []
            print(f"候选 {data.get('candidates', 0)} 条 · 命中 {len(matched)} 条")
            for hit in matched[:10]:
                print(f"  ✓ {hit.get('memory_id')}  「{hit.get('span')}」")
            if data.get("tokens"):
                print(f"  本轮注入 {data['tokens']['tokens']} tokens")
            return 0
        if args.cmd == "star":
            import webbrowser

            from ..ops import starmap

            result = starmap.write(
                app.db,
                out=args.out,
                subject=args.subject,
                redact=not args.reveal_secrets,
                png=not args.no_png,
                incremental=args.incremental,
            )
            if args.json:
                return _emit({"ok": True, "data": result}, True)
            inc = result.get("meta", {}).get("incremental") or {}
            if result.get("incremental"):
                print(f"✓ 星图已增量更新：{inc.get('locked', 0)} 条旧记忆不动 / "
                      f"{inc.get('fresh', 0)} 条新记忆落位")
            else:
                print(f"✓ 星图已生成：{result['meta']['shown']} 条记忆 / "
                      f"{result['meta']['counts']['entities']} 个实体 / "
                      f"{result['meta']['counts']['relations']} 条关系")
            if result.get("html"):
                print(f"  HTML  {result['html']}")
                print(f"  SVG   {result['svg']}")
            print(f"  JSON  {result['json']}")
            if result.get("png", {}).get("ok"):
                print(f"  PNG   {result['png']['path']}")
            if args.open and result.get("html"):
                webbrowser.open(Path(result["html"]).resolve().as_uri())
            return 0
        if args.cmd == "serve":
            from .rest import make_server

            host = args.host or str(app.config.get("gate.rest_host", "127.0.0.1"))
            port = args.port if args.port is not None else int(app.config.get("gate.rest_port", 8765))
            token = args.token if args.token is not None else (
                os.environ.get("PMEM_TOKEN") or str(app.config.get("gate.token", "") or "")
            )
            if host not in ("127.0.0.1", "localhost", "::1") and not token:
                print("✗ 非本机监听必须设置 --token / PMEM_TOKEN / [gate] token", file=sys.stderr)
                return 1
            server = make_server(
                app,
                host=host,
                port=port,
                token=token,
                rate_limit_per_min=int(app.config.get("gate.rate_limit_per_min", 0) or 0),
            )
            actual_host, actual_port = server.server_address[:2]
            print(
                f"— REST 已启动：http://{actual_host}:{actual_port}/v1/memory/<tool>  （Ctrl-C 停止）",
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        if args.cmd == "reflect":
            if args.history:
                from ..reflect import service

                items = service.history(app.db, subject=app.subject, limit=args.limit)
                if args.json:
                    return _emit({"ok": True, "data": {"items": items}}, True)
                if not items:
                    print("— 还没有反思记录")
                for item in items:
                    q = item.get("quality")
                    print(
                        f"  {item['created_at']}  {item['period']:<20} "
                        f"质量 {q if q is not None else '—'}"
                    )
                return 0
            result = app.reflect(
                period=args.period,
                persist=args.persist,
                only=args.only,
                limit=args.limit,
            )
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            print(result["data"]["rendered"])
            return 0
        if args.cmd == "trait":
            if args.trait_action == "list":
                # --limit 默认 None：不传就让 SDK 用自己的默认（50）。
                list_kwargs: dict[str, Any] = {
                    "action": "list",
                    "status": args.status,
                    "scope": args.scope,
                }
                if args.limit is not None:
                    list_kwargs["limit"] = args.limit
                result = app.trait(**list_kwargs)
            elif args.trait_action == "show":
                result = app.trait(action="show", ref=args.ref, limit=args.limit)
            elif args.trait_action == "history":
                result = app.trait(action="history", ref=args.ref, limit=args.limit)
            else:
                result = app.trait(action="metrics")
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            _print_trait(result["data"])
            return 0
        if args.cmd == "backfill" and args.source == "tokens":
            from ..ops import reindex

            stats = reindex.tokens(app.db, subject=app.subject, dry_run=args.dry_run)
            if not args.json:
                tone = "会改" if stats["dry_run"] else "已重算"
                print(f"✓ 分词索引{tone}：{stats['memories']} 条记忆，改动 {stats['changed']} 条")
                for sample in stats["samples"]:
                    print(f"  #{sample['seq']}  {sample['before']} → {sample['after']}")
            return _emit({"ok": True, "data": stats}, args.json)
        if args.cmd == "backfill" and args.source == "usage":
            from ..memory import usage

            stats = usage.replay(app.db, subject=app.subject, dry_run=args.dry_run)
            if not args.json:
                tone = "会改" if stats["dry_run"] else "已重算"
                print(f"✓ 使用语义{tone}：{stats['memories']} 条记忆，改动 {stats['changed']} 条")
                print(f"  真的被用过 {stats['used']} 条 / 被浮现过 {stats['surfaced']} 条")
                src = stats["sources"]
                print(f"  证据：召回日志 {src['recall_log']} 条 / 回执 {src['receipts']} 次 / "
                      f"浮现记录 {src['surface_log']} 条")
            return _emit({"ok": True, "data": stats}, args.json)
        if args.cmd == "backfill":
            from ..adapters import dsh, shadow

            root = Path(args.root) if args.root else Path.home() / ".dsh" / "sessions"
            files = dsh.recent_sessions(root, days=args.days)
            stats = shadow.backfill(
                app,
                files,
                scope_key=args.scope,
                limit=args.limit,
                dry_run=args.dry_run,
                node=args.node,
                reset=args.reset,
            )
            stats["scope"] = args.scope or "按会话 cwd 自动（project:<hash>）"
            return _emit({"ok": True, "data": stats}, args.json)
        if args.cmd == "shadow":
            from ..adapters import dsh, shadow
            from ..adapters.soul import SoulReader

            if args.action == "run":
                root = Path(args.root) if args.root else Path.home() / ".dsh" / "sessions"
                files = dsh.recent_sessions(root, days=args.days)
                out = Path(args.out) if args.out else Path(".tmp/shadow/shadow.jsonl")
                soul = SoulReader(args.soul) if args.soul else SoulReader()
                try:
                    summary = shadow.run_shadow(
                        app,
                        files,
                        scope=args.scope,
                        out_path=out,
                        soul=soul,
                        limit=args.limit,
                        node=args.node,
                    )
                finally:
                    soul.close()
                return _emit({"ok": True, "data": summary}, args.json)
            src = Path(args.file) if args.file else Path(".tmp/shadow/shadow.jsonl")
            records = shadow.load_records(src)
            summary = shadow.summarize(records)
            out = Path(args.out) if args.out else src.with_name("report.md")
            shadow.write_report(summary, records, out)
            summary["report_path"] = str(out)
            return _emit({"ok": True, "data": summary}, args.json)
        if args.cmd == "vector":
            from ..memory import vector as vector_mod

            action = args.vector_action
            if action == "status":
                return _emit({"ok": True, "data": vector_mod.stats(app.db, app.config)}, args.json)
            if action == "build":
                return _emit(
                    {"ok": True, "data": vector_mod.ensure(app.db, app.config)},
                    args.json,
                )
            if action == "rebuild":
                return _emit({"ok": True, "data": vector_mod.rebuild(app.db, app.config)}, args.json)
            scores = vector_mod.search(
                app.db,
                app.config,
                args.query,
                limit=args.k,
                min_score=args.min_score,
            )
            items = []
            for seq, sim in scores.items():
                row = app.db.conn.execute(
                    "SELECT id, text_raw FROM memory WHERE seq=?", (int(seq),)
                ).fetchone()
                if row is not None:
                    items.append({"id": row["id"], "text": row["text_raw"], "score": sim})
            return _emit({"ok": True, "data": {"query": args.query, "items": items}}, args.json)
        if args.cmd == "add":
            return _emit(
                app.add(
                    args.text,
                    kind=args.kind,
                    provenance=args.provenance,
                    session_id=args.session,
                    source_kind=args.source_kind,
                    sensitivity=args.sensitivity,
                    pinned=args.pinned,
                    valid_from=args.valid_from,
                    valid_to=args.valid_to,
                    groups=args.group,
                    grade=args.grade,
                    grade_reason=args.grade_reason,
                    idem_key=args.idem,
                ),
                args.json,
            )
        if args.cmd == "search":
            filters: dict[str, Any] = {}
            if args.kind:
                filters["kind"] = args.kind
            if args.group:
                filters["group"] = args.group
            if args.grade:
                filters["grade"] = args.grade
            if args.pinned_only:
                filters["pinned_only"] = True
            return _emit(
                app.search(args.query, k=args.k, budget=args.budget, mode=args.mode,
                           filters=filters, client="cli"),
                args.json,
            )
        if args.cmd == "selfcheck":
            # 不传位置参数就从 stdin 读（方便管道：Get-Content draft.txt | pmem selfcheck）
            answer = args.answer
            if answer is None:
                answer = sys.stdin.read()
            result = app.selfcheck(answer, question=args.question)
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            data = result.get("data") or {}
            print(data.get("report") or "")
            return 0
        if args.cmd == "verify":
            answer = args.answer
            if answer is None:
                answer = sys.stdin.read()
            items = None
            if args.items:
                items = json.loads(Path(args.items).read_text(encoding="utf-8"))
            result = app.verify_answer(answer, items=items, query=args.query)
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            data = result.get("data") or {}
            print(data.get("report") or "")
            return 0
        if args.cmd == "render":
            items = None
            if getattr(args, "items", None):
                import json as _json

                raw = Path(args.items).read_text(encoding="utf-8")
                loaded = _json.loads(raw)
                if not isinstance(loaded, list):
                    raise SystemExit("--items 需要 JSON 数组")
                items = loaded
            result = app.render(
                items=items, query=args.query, k=args.k, title=args.title, budget=args.budget
            )
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            print((result.get("data") or {}).get("text") or "")
            return 0
        if args.cmd == "claim":
            action = str(args.claim_action)
            if action == "list":
                result = claim_list_payload(
                    app,
                    kind=args.kind,
                    status=args.status,
                    limit=args.limit,
                    include_superseded=args.include_superseded,
                )
            elif action == "search":
                result = claim_search_payload(
                    app, query=args.query, kind=args.kind, k=args.k
                )
            elif action == "delete":
                result = claim_delete_payload(
                    app, claim_id=args.claim_id, reason=args.reason
                )
            else:  # extract
                if args.apply_file and args.rule:
                    parser.error("--apply 与 --rule 只能选一个")
                if args.apply_file:
                    try:
                        payload = json.loads(
                            Path(args.apply_file).read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError) as exc:
                        return _emit(
                            {
                                "ok": False,
                                "error": {
                                    "code": "E_VALIDATION",
                                    "message": f"--apply 读不了 JSON：{exc}",
                                    "field": "apply",
                                },
                            },
                            args.json,
                        )
                    result = claim_apply_payload(
                        app, day_key=args.day, payload=payload
                    )
                elif args.rule:
                    result = claim_rule_payload(app, day_key=args.day)
                else:
                    result = claim_prep_payload(
                        app, day_key=args.day, max_source_chars=args.max_source_chars
                    )
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            _print_claim(result.get("data") or {}, action)
            return 0
        if args.cmd == "notes":
            action = str(args.notes_action)
            if action == "show":
                result = notes_show_payload(app)
            elif action == "prep":
                result = notes_prep_payload(app, max_lines=args.max_lines)
            else:  # apply
                payload: Any = None
                raw = ""
                if getattr(args, "file", None):
                    try:
                        raw = Path(args.file).read_text(encoding="utf-8")
                    except OSError as exc:
                        return _emit(
                            {
                                "ok": False,
                                "error": {
                                    "code": "E_VALIDATION",
                                    "message": f"--file 读不到：{exc}",
                                    "field": "file",
                                },
                            },
                            args.json,
                        )
                else:
                    raw = sys.stdin.read()
                if raw.strip():
                    try:
                        payload = json.loads(raw)
                    except ValueError as exc:
                        return _emit(
                            {
                                "ok": False,
                                "error": {
                                    "code": "E_VALIDATION",
                                    "message": f"payload 不是合法 JSON：{exc}",
                                    "field": "payload",
                                },
                            },
                            args.json,
                        )
                result = notes_apply_payload(app, payload=payload)
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            _print_notes(result.get("data") or {}, action)
            return 0
        if args.cmd == "plan":
            question = args.question
            if question is None:
                question = sys.stdin.read()
            evidences = None
            if args.from_search:
                found = app.search(question, client="cli")
                evidences = ((found.get("data") or {}).get("items") or []) if found.get("ok") else []
            result = app.answer_plan(question, when=args.when, evidences=evidences)
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            data = result.get("data") or {}
            print(data.get("render") or "")
            print()
            print(data.get("summary") or "")
            return 0
        if args.cmd == "trace":
            result = app.trace(
                args.query,
                limit=args.limit,
                include_retracted=bool(getattr(args, "include_retracted", False)),
            )
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            data = result.get("data") or {}
            if data.get("too_short"):
                print("— " + str(data.get("note") or "太短，没查"))
                return 0
            items = data.get("items") or []
            print(f"— 按字查过（{data.get('scope', '')}）：命中 {len(items)} 条")
            for it in items:
                sid = str(it.get("session_id") or "")
                mark = " · 已撤回" if it.get("retracted") else ""
                print(
                    f"  {it.get('role')} · 会话 …{sid[-8:]} · 第 {it.get('line_no')} 行 · {it.get('ts')}{mark}"
                )
                print(f"    {str(it.get('text'))[:160]}")
            if not items:
                print("  " + str(data.get("note") or "库里没有这句话"))
            return 0
        if args.cmd == "retract":
            result = app.retract(args.target, reason=args.reason, kind=args.kind)
            if args.json or not result.get("ok"):
                return _emit(result, args.json)
            data = result.get("data") or {}
            what = "记忆" if data.get("target") == "memory" else "原话"
            if data.get("already"):
                print(f"— 这条{what}已经撤回过（{str(data.get('created_at') or '')[:19]}）")
                return 0
            print(f"✓ 已撤回一条{what}：{args.target}")
            if data.get("excerpt"):
                print(f"  撤回的那句：{data['excerpt']}")
            if data.get("restore_hint"):
                print(f"  只是归档，{data['restore_hint']}；原文与事件链没动")
            else:
                print("  原文没删、事件链没动，只是默认不再返回")
                print("  仍可查：pmem trace \"那句话\" --include-retracted")
            return 0
        if args.cmd == "get":
            return _emit(app.get(args.id), args.json)
        if args.cmd == "update":
            set_fields: dict[str, Any] = {}
            if args.kind:
                set_fields["kind"] = args.kind
            if args.status:
                set_fields["status"] = args.status
            if args.sensitivity:
                set_fields["sensitivity"] = args.sensitivity
            if args.importance is not None:
                set_fields["importance"] = args.importance
            if args.pin:
                set_fields["pinned"] = 1
            if args.unpin:
                set_fields["pinned"] = 0
            supersede = {"text": args.supersede} if args.supersede else None
            return _emit(
                app.update(args.id, set_fields=set_fields or None, supersede_with=supersede, reason=args.reason),
                args.json,
            )
        if args.cmd == "delete":
            if args.vacuum and not args.hard:
                return _emit(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_VALIDATION",
                            "message": "--vacuum 只能配合 --hard 使用",
                            "hint": "软删只是停放，VACUUM 回收不到它占的空间",
                        },
                    },
                    args.json,
                )
            if args.hard and args.vacuum:
                from ..ops import maintenance

                state = maintenance.active(app.db, config=app.config)
                if state.get("active"):
                    return _emit(
                        {
                            "ok": False,
                            "error": {
                                "code": "E_MAINTENANCE",
                                "message": "维护窗口内拒绝执行 VACUUM（hard 删除也没有执行）",
                                "hint": "等维护窗口结束后重试，或先 pmem maintenance off",
                            },
                        },
                        args.json,
                    )
            result = app.delete(args.id, hard=args.hard, reason="cli")
            if result.get("ok") and args.hard and args.vacuum:
                if hasattr(app.db, "vacuum"):
                    result["data"]["vacuum"] = {
                        "ok": True,
                        "before": _db_pages(app.db),
                        "result": app.db.vacuum(),
                        "after": _db_pages(app.db),
                    }
                else:
                    result["data"]["vacuum"] = {
                        "ok": False,
                        "message": "当前版本没有 Database.vacuum；删除已生效，空间未回收",
                    }
            return _emit(result, args.json)
        if args.cmd == "history":
            return _emit(app.history(args.id, limit=args.limit), args.json)
        if args.cmd == "export":
            text = app.export(redact=not args.no_redact, include_events=args.with_events)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
                header = json.loads(text.splitlines()[0]) if text.strip() else {}
                counts = header.get("counts") or {}
                detail = f"记忆 {counts.get('memory', 0)} 条"
                if args.with_events:
                    detail += f"，事件 {counts.get('event', 0)} 条"
                print(f"已导出 → {args.out}（{detail}）")
                return 0
            sys.stdout.write(text)
            return 0
        if args.cmd == "import":
            if args.path == "-":
                data = sys.stdin.read()
            else:
                data = Path(args.path).read_text(encoding="utf-8")
            result = app.import_bundle(
                data, on_conflict=args.on_conflict, dry_run=args.dry_run
            )
            if args.json:
                return _emit(result, True)
            if not result.get("ok"):
                return _emit(result, False)
            stats = result["data"]
            print(
                f"导入：{stats['input']} 行 → 新增 {stats['inserted']} / 合并 {stats['merged']} / "
                f"跳过 {stats['skipped']} / 忽略 {stats['ignored']}"
                + (f"；事件 {stats['events_inserted']} 条" if stats.get("events_input") else "")
                + ("（dry-run，未落库）" if stats["dry_run"] else "")
            )
            return 0
        parser.error(f"未知命令 {args.cmd}")
        return 2
    finally:
        app.close()


def _fts_docs(conn: Any, name: str) -> int | None:
    """FTS 索引里的真实文档数（shadow 表 ``<name>_docsize``）；取不到返回 None。"""
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {name}_docsize").fetchone()
        return int(row[0]) if row else None
    except sqlite3.DatabaseError:
        return None


def _reindex_fts(app: PMem, *, dry_run: bool) -> dict[str, Any]:
    """重建三套 FTS 索引，返回每套的文档数与基表行数（供 CLI/脚本核对 I21）。"""
    from ..memory import journal as journal_mod

    conn = app.db.conn
    out: dict[str, Any] = {"dry_run": dry_run}

    def rows_of(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    for name in ("memory_fts_tok", "memory_fts_tri"):
        item: dict[str, Any] = {
            "rows": rows_of("SELECT COUNT(*) FROM memory"),
            "docs": _fts_docs(conn, name),
            "docs_before": None,
            "rebuilt": False,
        }
        if not dry_run:
            item["docs_before"] = item["docs"]
            with app.db.tx() as tx:
                tx.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
            item["docs"] = _fts_docs(conn, name)
            item["rebuilt"] = True
        out[name] = item

    journal_item: dict[str, Any] = {
        "rows": rows_of("SELECT COUNT(*) FROM journal_line"),
        "docs": _fts_docs(conn, "journal_fts"),
        "docs_before": None,
        "rebuilt": False,
    }
    if not dry_run:
        journal_item["docs_before"] = journal_item["docs"]
        stats = journal_mod.rebuild_index(app.db)
        journal_item["docs"] = _fts_docs(conn, "journal_fts")
        journal_item["rebuilt"] = True
        journal_item["lines"] = stats.get("lines")
    out["journal_fts"] = journal_item

    episode_item: dict[str, Any] = {
        "rows": rows_of("SELECT COUNT(*) FROM episode"),
        "docs": _fts_docs(conn, "episode_fts"),
        "docs_before": None,
        "rebuilt": False,
    }
    if not dry_run:
        rebuild_episodes = getattr(journal_mod, "rebuild_episode_index", None)
        if rebuild_episodes is None:
            episode_item["skipped"] = "journal.rebuild_episode_index 尚未实现"
        else:
            episode_item["docs_before"] = episode_item["docs"]
            rebuild_episodes(app.db)
            episode_item["docs"] = _fts_docs(conn, "episode_fts")
            episode_item["rebuilt"] = True
    out["episode_fts"] = episode_item
    return out


def _db_pages(db: Any) -> dict[str, int]:
    """库的页数与名义字节数（page_count × page_size）——VACUUM 前后对比用。"""
    pages = int(db.conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(db.conn.execute("PRAGMA page_size").fetchone()[0])
    return {"pages": pages, "bytes": pages * page_size}


def _fmt_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GB"


def _local_stamp(value: Any) -> str:
    """把调度器给的 UTC ISO 时间戳转成本地时间（日志是给人看的）。"""
    try:
        from ..base.clock import parse_iso

        return parse_iso(str(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 时间戳坏了也要能打日志
        return str(value)[:19]


def _print_maintenance(action: str, result: dict[str, Any]) -> None:
    """维护窗口的三种人话输出。"""
    if action == "on":
        print(f"✓ 维护窗口已开启：{result.get('reason') or '-'} · "
              f"到 {_local_stamp(result.get('until'))}（{result.get('minutes')} 分钟）")
        print(f"  标记文件 {result.get('path')}")
        print("  daemon 在窗口内只登记/自愈任务、不执行任何任务；"
              "pmem backup / pmem sleep 这类手动命令不走 tick，不受影响。")
        return
    if action == "off":
        print("✓ 维护窗口已取消" if result.get("cleared") else "— 没有维护标记，无需取消")
        return
    if result.get("active"):
        left = int(result.get("remaining_seconds") or 0) // 60
        print(f"● 维护中：{result.get('reason') or '-'} · "
              f"到 {_local_stamp(result.get('until'))}（剩约 {left} 分钟）")
    elif result.get("expired"):
        print(f"— 维护标记已过期（{result.get('reason') or '-'}，"
              f"到 {_local_stamp(result.get('until'))}），视为不活跃")
    else:
        print("— 没有维护窗口，daemon 正常调度")


def _print_tick(result: dict[str, Any], *, heartbeat: bool = False) -> None:
    """打印一次 tick。

    ``heartbeat``（``--once`` 用）：即使什么都没跑也留一行——
    计划任务每 10 分钟写日志，用户才能确认「它真的在跑、只是没活干」。
    没有这一行时日志永远是 0 字节，看起来像任务根本没跑起来。
    """
    if result.get("maintenance"):
        print(
            f"— 维护窗口 {_local_stamp(result.get('at'))} · maintenance=true · "
            f"{result.get('reason') or '-'} · 跳过 {len(result.get('skipped') or [])} 项 · "
            f"到 {_local_stamp(result.get('until'))}",
            flush=True,
        )
        skipped = result.get("skipped") or []
        if skipped:
            print(f"  窗口结束后补跑：{'、'.join(skipped)}", flush=True)
        return
    if not result.get("ran"):
        if heartbeat:
            journal = result.get("journal") or {}
            note = journal.get("skipped") or ("ok" if journal.get("ok") else "err")
            print(
                f"— 心跳 {_local_stamp(result.get('at'))} · ok · 无到期任务 · 日记 {note}"
                f" · 任务 {len(result.get('jobs') or [])}",
                flush=True,
            )
        return
    print(f"— 调度 {result['at'][:19]}：执行 {len(result['ran'])} 项", flush=True)
    for item in result["ran"]:
        mark = "✓" if item["ok"] else "✗"
        print(f"  {mark} {item['kind']:<12} {item.get('elapsed_ms', 0)} ms  "
              f"{item.get('next_run_at', '') or item.get('error', '')}", flush=True)


def _doctor(app: PMem, as_json: bool) -> int:
    from ..base import events
    from ..ops import invariants

    chain_ok, chain_msg = events.verify_chain(app.db.conn)
    inv = invariants.summary(invariants.check_all(app.db))
    detail = app.db.integrity_detail()
    integrity = bool(detail.get("ok"))
    integrity_errors: list[str] = list(detail.get("errors") or [])
    if detail.get("error"):
        integrity_errors.append(str(detail["error"]))
    checks = {
        "integrity": integrity,
        "integrity_errors": integrity_errors,
        "sqlite_version": sqlite3.sqlite_version,
        "event_chain": chain_ok,
        "chain_detail": chain_msg,
        "schema_version": app.db.meta("schema_version"),
        "memory_total": app.store.stats()["memory_total"],
        "invariants": inv,
    }
    result = {"ok": integrity and chain_ok and inv["ok"], "data": checks}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"SQLite：{sqlite3.sqlite_version}")
        print(f"完整性：{'ok' if integrity else 'FAIL'}")
        if not integrity:
            for message in integrity_errors[:5]:
                print(f"  ✗ {message}")
            print(
                "  提示：FTS/原话索引不一致可跑 pmem reindex fts 重建"
                "（memory/journal/episode 三套；只重建原话索引是 pmem journal reindex）"
            )
        print(f"事件链：{'ok' if chain_ok else 'FAIL'}（{chain_msg}）")
        print(f"不变量：{inv['passed']}/{inv['total']} 通过")
        for item in inv["failed"]:
            print(f"  ✗ {item['id']}: {item['detail']}")
        if any(item.get("id") == "I21" for item in inv["failed"]):
            print("  提示：I21 是 FTS 行数漂移，跑 pmem reindex fts 重建三套索引")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
