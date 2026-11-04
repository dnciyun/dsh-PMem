"""M9.8：把 dsh-pmem 装进一个 DSH profile，以及撤回它。

这个模块只做四件事，每一件都必须**幂等、可预演、可回滚**：

1. 在 profile 的 ``package.json`` 里登记这个 bundle（依赖 + ``dsh.profile.bundles``）；
2. 在 profile 的 ``cordis.patch.yml`` 里写入/替换一段 MCP 注册块，
   路径按**本机实际仓库位置**生成（而不是仓库里写死的 ``F:/Ai/AWB``）；
3. 确保记忆库存在（幂等 ``init``）；
4. ``status`` 把上面每一条都验一遍，``uninstall`` 只撤掉自己加的东西。

设计上的两条硬规矩：

- **不碰数据库内容**。``uninstall`` 只摘注册，记忆一条都不删——那是用户的东西。
- **不代跑包管理器**。加完依赖还需要 ``pnpm install`` 才能把 ``link:`` 变成
  ``node_modules`` 里的真文件；这一步联网、耗时、可能改 lock 文件，
  所以只报告、不代劳。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BEGIN = "# === pmem MCP BEGIN ==="
END = "# === pmem MCP END ==="
BUNDLE = "dsh-pmem"
SERVER = "pmem"
BACKUP_SUFFIX = ".pmem-bak"


# ---- 路径 -----------------------------------------------------------------


def repo_root() -> Path:
    """仓库根目录（``src/pmem/gate/dsh_install.py`` 往上四层）。"""
    return Path(__file__).resolve().parents[3]


def dsh_home(home: str | Path | None = None) -> Path:
    """``~/.dsh``。传 ``home`` 是为了让测试完全脱离真实用户目录。"""
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return base / ".dsh"


def profile_dir(profile: str = "desktop", *, home: str | Path | None = None) -> Path:
    return dsh_home(home) / "profiles" / profile


# ---- 生成的 YAML 片段 -----------------------------------------------------


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def mcp_block(*, repo: Path, db: Path, python: str = "python") -> str:
    """profile 级 ``cordis.patch.yml`` 里那一段。

    路径由**安装时**计算，所以仓库搬到别的盘只要重跑一次 ``pmem dsh install``。
    """
    plugin = repo / "deploy" / "dsh-pmem"
    args = [
        "-m",
        "pmem.gate.mcp_server",
        "--db",
        str(db),
    ]
    lines = [
        BEGIN,
        "# 由 `pmem dsh install` 维护；这段区块会被整体替换，请勿手工编辑内部条目。",
        "- insert:",
        "    - id: mcp-pmem",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        f"        serverName: {SERVER}",
        "        transport: stdio",
        f"        command: {_quote(python)}",
        "        args:",
    ]
    lines.extend(f"          - {_quote(a)}" for a in args)
    lines += [
        f"        cwd: {_quote(str(repo))}",
        "        env:",
        f"          PYTHONPATH: {_quote(str(repo / 'src'))}",
        f"          PYTHONIOENCODING: {_quote('utf-8')}",
        "        toolCallTimeoutMs: 120000",
        "        failOnStartupError: false",
        f"      # 插件 bundle 在 {plugin}",
        END,
    ]
    return "\n".join(lines)


def _strip_block(text: str) -> str:
    """去掉已有的 pmem 注册块（连同两侧空行），留下其余内容原样。"""
    while BEGIN in text and END in text:
        start = text.index(BEGIN)
        stop = text.index(END, start) + len(END)
        before = text[:start].rstrip("\n")
        after = text[stop:].lstrip("\n")
        text = before + ("\n\n" if before and after else "\n") + after
    return text


# ---- 报告 -----------------------------------------------------------------


@dataclass
class Step:
    key: str
    title: str
    action: str          # create | update | keep | remove | missing | warn
    detail: str = ""
    path: str = ""


@dataclass
class Report:
    profile: str
    repo: str
    db: str
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(step.action in ("create", "update", "remove") for step in self.steps)

    @property
    def ok(self) -> bool:
        return not any(step.action in ("missing",) for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "profile": self.profile,
            "repo": self.repo,
            "db": self.db,
            "steps": [step.__dict__ for step in self.steps],
            "notes": list(self.notes),
        }

    def render(self) -> str:
        marks = {
            "create": "＋",
            "update": "↻",
            "keep": "＝",
            "remove": "－",
            "missing": "✗",
            "warn": "!",
        }
        out = [f"profile {self.profile}  ·  {self.repo}"]
        for step in self.steps:
            line = f"  {marks.get(step.action, '·')} {step.title}"
            if step.detail:
                line += f" — {step.detail}"
            out.append(line)
        if self.notes:
            out.append("")
            out.extend(f"  {note}" for note in self.notes)
        return "\n".join(out)


# ---- 读现状 ---------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _backup(path: Path) -> None:
    """第一次改动前留一份原件；反复安装不覆盖最早那份。"""
    guard = path.with_name(path.name + BACKUP_SUFFIX)
    if path.exists() and not guard.exists():
        shutil.copy2(path, guard)


# ---- 安装 / 卸载 / 状态 ----------------------------------------------------


def install(
    *,
    profile: str = "desktop",
    repo: str | Path | None = None,
    db: str | Path | None = None,
    python: str = "python",
    home: str | Path | None = None,
    dry_run: bool = False,
    make_db: bool = True,
) -> Report:
    root = Path(repo) if repo is not None else repo_root()
    root = root.resolve()
    db_path = Path(db) if db is not None else root / "data" / "pmem.db"
    db_path = db_path.resolve()
    pdir = profile_dir(profile, home=home)
    report = Report(profile=profile, repo=str(root), db=str(db_path))

    # 1) 仓库本身
    plugin = root / "deploy" / "dsh-pmem"
    index_js = plugin / "index.js"
    if index_js.exists():
        report.steps.append(Step("repo", "插件目录", "keep", str(plugin), str(plugin)))
    else:
        report.steps.append(
            Step("repo", "插件目录", "missing", f"找不到 {index_js}", str(plugin))
        )
        return report

    # 2) profile
    pkg_path = pdir / "package.json"
    pkg = _read_json(pkg_path)
    if pkg is None:
        report.steps.append(
            Step("profile", "DSH profile", "missing", f"读不到 {pkg_path}", str(pkg_path))
        )
        return report
    report.steps.append(Step("profile", "DSH profile", "keep", str(pdir), str(pdir)))

    link = "link:" + str(plugin).replace("\\", "/")
    deps = pkg.setdefault("dependencies", {})
    current = deps.get(BUNDLE)
    pkg_dirty = False
    if current == link:
        report.steps.append(Step("dep", "package.json 依赖", "keep", link, str(pkg_path)))
    else:
        report.steps.append(
            Step("dep", "package.json 依赖", "create" if current is None else "update",
                 f"{current} → {link}" if current else link, str(pkg_path))
        )
        pkg_dirty = True
        if not dry_run:
            deps[BUNDLE] = link

    dsh = pkg.setdefault("dsh", {})
    prof = dsh.setdefault("profile", {})
    bundles = prof.setdefault("bundles", [])
    if BUNDLE in bundles:
        report.steps.append(Step("bundle", "bundles 列表", "keep", BUNDLE, str(pkg_path)))
    else:
        report.steps.append(Step("bundle", "bundles 列表", "create", BUNDLE, str(pkg_path)))
        pkg_dirty = True
        if not dry_run:
            bundles.append(BUNDLE)

    # 只有真的动了 package.json 才回写：证书是"这次安装改了哪些文件"，
    # 不能因为后面写了 YAML 就把 package.json 也重排一遍。
    if pkg_dirty and not dry_run:
        _backup(pkg_path)
        pkg_path.write_text(
            json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # 3) MCP 注册块
    patch_path = pdir / "cordis.patch.yml"
    if not patch_path.exists():
        report.steps.append(
            Step("patch", "MCP 注册", "missing", f"读不到 {patch_path}", str(patch_path))
        )
        return report
    text = _read_text(patch_path)
    wanted = mcp_block(repo=root, db=db_path, python=python)
    if text.count(BEGIN) == 1 and wanted in text:
        report.steps.append(Step("patch", "MCP 注册", "keep", "已指向本机路径", str(patch_path)))
    else:
        report.steps.append(
            Step("patch", "MCP 注册", "create" if BEGIN not in text else "update",
                 "mcp-pmem → 20 个 memory_* 工具", str(patch_path))
        )
        if not dry_run:
            cleaned = _strip_block(text).rstrip("\n")
            body = cleaned + ("\n\n" if cleaned else "") + wanted + "\n"
            _backup(patch_path)
            patch_path.write_text(body, encoding="utf-8")

    # 4) 记忆库
    if db_path.exists():
        report.steps.append(Step("db", "记忆库", "keep", str(db_path), str(db_path)))
    elif dry_run:
        report.steps.append(Step("db", "记忆库", "create", "会被建出来", str(db_path)))
    elif make_db:
        from ..gate.sdk import PMem

        app = PMem.open(db_path)
        app.close()
        report.steps.append(Step("db", "记忆库", "create", str(db_path), str(db_path)))
    else:
        report.steps.append(Step("db", "记忆库", "missing", "不存在（--no-init）", str(db_path)))

    # 5) link 有没有落地
    linked = pdir / "node_modules" / BUNDLE
    if linked.exists():
        report.steps.append(Step("link", "node_modules", "keep", str(linked), str(linked)))
    else:
        report.steps.append(
            Step("link", "node_modules", "warn", "还没 pnpm install", str(linked))
        )
        report.notes.append(
            f"下一步：cd {pdir} && pnpm install  —— 然后重启 DSH"
        )
    return report


def uninstall(
    *,
    profile: str = "desktop",
    home: str | Path | None = None,
    dry_run: bool = False,
) -> Report:
    pdir = profile_dir(profile, home=home)
    report = Report(profile=profile, repo="", db="")

    pkg_path = pdir / "package.json"
    pkg = _read_json(pkg_path)
    if pkg is None:
        report.steps.append(
            Step("profile", "DSH profile", "missing", f"读不到 {pkg_path}", str(pkg_path))
        )
        return report

    deps = pkg.get("dependencies") or {}
    bundles = ((pkg.get("dsh") or {}).get("profile") or {}).get("bundles") or []
    touched = False
    if BUNDLE in deps:
        report.steps.append(Step("dep", "package.json 依赖", "remove", deps[BUNDLE], str(pkg_path)))
        if not dry_run:
            deps.pop(BUNDLE, None)
        touched = True
    else:
        report.steps.append(Step("dep", "package.json 依赖", "keep", "本来就没有", str(pkg_path)))
    if BUNDLE in bundles:
        report.steps.append(Step("bundle", "bundles 列表", "remove", BUNDLE, str(pkg_path)))
        if not dry_run:
            bundles.remove(BUNDLE)
        touched = True
    else:
        report.steps.append(Step("bundle", "bundles 列表", "keep", "本来就没有", str(pkg_path)))
    if touched and not dry_run:
        _backup(pkg_path)
        pkg_path.write_text(
            json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    patch_path = pdir / "cordis.patch.yml"
    text = _read_text(patch_path)
    if BEGIN in text:
        report.steps.append(Step("patch", "MCP 注册", "remove", "整段摘掉", str(patch_path)))
        if not dry_run:
            _backup(patch_path)
            patch_path.write_text(_strip_block(text).rstrip("\n") + "\n", encoding="utf-8")
    else:
        report.steps.append(Step("patch", "MCP 注册", "keep", "本来就没有", str(patch_path)))

    report.notes.append("记忆库没有动：卸载只摘注册，一条记忆都不会删。")
    report.notes.append("想彻底清掉再手工删 data/pmem.db 和 ~/.dsh/pmem/。")
    return report


def status(
    *,
    profile: str = "desktop",
    repo: str | Path | None = None,
    db: str | Path | None = None,
    home: str | Path | None = None,
) -> Report:
    root = Path(repo) if repo is not None else repo_root()
    root = root.resolve()
    db_path = Path(db) if db is not None else root / "data" / "pmem.db"
    db_path = db_path.resolve()
    pdir = profile_dir(profile, home=home)
    report = Report(profile=profile, repo=str(root), db=str(db_path))

    plugin = root / "deploy" / "dsh-pmem"
    report.steps.append(
        Step("repo", "插件目录", "keep" if (plugin / "index.js").exists() else "missing",
             str(plugin), str(plugin))
    )

    pkg_path = pdir / "package.json"
    pkg = _read_json(pkg_path)
    if pkg is None:
        report.steps.append(
            Step("profile", "DSH profile", "missing", f"读不到 {pkg_path}", str(pkg_path))
        )
        return report
    link = "link:" + str(plugin).replace("\\", "/")
    current = (pkg.get("dependencies") or {}).get(BUNDLE)
    report.steps.append(
        Step("dep", "package.json 依赖",
             "keep" if current == link else ("missing" if current is None else "update"),
             current or "未登记", str(pkg_path))
    )
    bundles = ((pkg.get("dsh") or {}).get("profile") or {}).get("bundles") or []
    report.steps.append(
        Step("bundle", "bundles 列表", "keep" if BUNDLE in bundles else "missing",
             BUNDLE if BUNDLE in bundles else "未启用", str(pkg_path))
    )

    patch_path = pdir / "cordis.patch.yml"
    text = _read_text(patch_path)
    if BEGIN not in text:
        report.steps.append(Step("patch", "MCP 注册", "missing", "没有 pmem 区块", str(patch_path)))
    else:
        wanted = mcp_block(repo=root, db=db_path)
        same = wanted in text
        report.steps.append(
            Step("patch", "MCP 注册", "keep" if same else "update",
                 "已指向本机路径" if same else "还指向别的路径（仓库搬家过？）",
                 str(patch_path))
        )
        if not same:
            report.notes.append("重跑 pmem dsh install 就会按当前路径重写这一段。")

    report.steps.append(
        Step("db", "记忆库", "keep" if db_path.exists() else "missing",
             str(db_path) if db_path.exists() else "还没有库", str(db_path))
    )
    linked = pdir / "node_modules" / BUNDLE
    report.steps.append(
        Step("link", "node_modules", "keep" if linked.exists() else "warn",
             str(linked) if linked.exists() else "还没 pnpm install", str(linked))
    )
    if not linked.exists():
        report.notes.append(f"cd {pdir} && pnpm install")
    return report


__all__ = [
    "install",
    "uninstall",
    "status",
    "repo_root",
    "dsh_home",
    "profile_dir",
    "mcp_block",
    "BEGIN",
    "END",
]
