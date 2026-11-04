"""计划任务：让「30 天日历门」真的开始计时。

代码写好了不等于门在走。门要的是**每天都有记录**，而记录只能靠一个每十分钟
醒一次的东西。这里用 Windows 计划任务（`schtasks`）干两件事：

- ``pmem-daemon``：每 N 分钟跑一次 ``pmem daemon --once``（睡眠/周报/备份/监控/月报）；
- ``pmem-eval``：每周跑一次 ``pmem eval --record``，让 recall@5 有第二个点。

**任务用 ``pythonw.exe`` 跑，不经过 cmd / vbs**（0.12.0）。

最初 ``/TR`` 直接指向 ``.cmd``：Windows 会给批处理建一个控制台，桌面每 10 分钟闪
一下黑框。换成无窗 ``.vbs`` 启动器之后黑框没了，但**杀软会按「脚本宿主隐藏执行」
这个经典病毒特征反复弹窗**（火绒每次都问、还顺手把启动文件夹里那个删了）——
用脚本隐藏窗口是错的方向，得让进程本身就没有控制台。

``pythonw.exe`` 是 GUI 子系统的解释器：任务拉起它时系统根本不创建控制台，也不涉及
脚本宿主；命令行里也不需要转义（命令、参数各占一个字段）。输出由被跑的那个
``.py`` 启动脚本自己写进日志（``sys.stdout`` 重定向），顺便把 ``env.cmd`` 里的
本机机密（模型 key 之类）读进环境——原来是 ``cmd`` 的 ``call``，现在换成几行解析。

``.cmd`` 包装器仍然生成一份：它给人双击用（那时出现控制台是正常的）。

开机自启**默认不装**：``schedule install --autostart`` 才往用户「启动」文件夹放一个
``.lnk``（指向同一个 ``pythonw`` + 启动脚本）。``/SC ONLOGON`` 走不通——登录触发
默认是全体用户的，``schtasks`` 直接拒绝访问。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TASK_DAEMON = "pmem-daemon"
TASK_EVAL = "pmem-eval"
STARTUP_FILE = "pmem-startup.lnk"
DEFAULT_MINUTES = 10
DEFAULT_EVAL_AT = "03:30"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    ok: bool
    steps: list[Step] = field(default_factory=list)
    home: str = ""
    tasks: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(Step(name, ok, detail))
        if not ok:
            self.ok = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "home": self.home,
            "tasks": self.tasks,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in self.steps],
        }


def tasks_home(home: str | Path | None = None) -> Path:
    """包装器和日志的落脚处：``<home>/.dsh/pmem/tasks``（``home`` 是用户主目录）。"""
    base = Path(home) if home else Path.home()
    return base / ".dsh" / "pmem" / "tasks"


def cmd_encoding() -> str:
    """``.cmd`` 必须按系统 ANSI 代码页写。

    cmd.exe 不按 UTF-8 读批处理：把中文用户名（比如 C:/Users/example）写成 UTF-8 字节，
    它会当成乱码路径，报「系统找不到指定的路径」。Windows 上用 ``mbcs`` 才是对的。
    """
    return "mbcs" if os.name == "nt" else "utf-8"


def wrapper_path(home: Path, task: str) -> Path:
    return home / f"{task}.cmd"


def log_path(home: Path, task: str) -> Path:
    return home / f"{task}.log"


def startup_dir(home: str | Path | None = None) -> Path | None:
    """用户「启动」文件夹：不占管理员权限就能开机/登录自启的唯一干净位置。

    为什么不用计划任务：``schtasks /SC ONLOGON`` 会**拒绝访问**——登录触发默认是
    全体用户的，要管理员；而「启动」文件夹是每用户的，登录后 Windows 会替你跑一遍。
    """
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA") or str(Path(home or Path.home()) / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_path(home: str | Path | None = None) -> Path | None:
    folder = startup_dir(home)
    return folder / STARTUP_FILE if folder else None


def runner_path(home: Path, task: str) -> Path:
    """任务真正跑的启动脚本：pmem-daemon.py（pythonw 执行，无控制台）。"""
    return home / f"{task}.py"


def pythonw(exe: str | None = None) -> str:
    """找出同一个 Python 安装里的 pythonw.exe（GUI 子系统 = 没有控制台）。"""
    import sys

    base = Path(exe or sys.executable)
    candidate = base.with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else str(base)


RUNNER_TEMPLATE = Path(__file__).with_name("_runner_template.py")


def build_runner(
    *,
    python: str,
    db: str,
    repo: str,
    src: str,
    action: str,
    log: Path,
    env_file: Path | None = None,
    pythonw_exe: str | None = None,
) -> str:
    """生成 pythonw 跑的启动脚本（模板见 _runner_template.py）。"""
    argv = ["--db", db, *action.split()]
    env_path_str = str(env_file) if env_file else ""
    body = RUNNER_TEMPLATE.read_text(encoding="utf-8")
    body = body.replace("__SRC__", repr(src))
    body = body.replace("__LOG__", repr(str(log)))
    body = body.replace("__ENV_FILE__", repr(env_path_str))
    body = body.replace("__REPO__", repr(repo))
    body = body.replace("__ARGV__", repr(argv))
    exe = pythonw_exe or pythonw(python)
    return body + "# pythonw: " + exe + chr(10)


def env_path(home: Path) -> Path:
    """本机机密：``~/.dsh/pmem/env.cmd``（不在 tasks/ 里，重装不碰它）。"""
    return home.parent / "env.cmd"


def build_wrapper(
    *,
    python: str,
    db: str,
    repo: str,
    src: str,
    action: str,
    log: Path,
    env_file: Path | None = None,
) -> str:
    """生成一份可读、可直接运行的 ``.cmd``。

    ``env_file``（``~/.dsh/pmem/env.cmd``）是留给本机机密的：模型凭证这类东西
    不该写进这份会被 ``schedule install`` 重写的包装器里，也不该写进仓库。
    有就 call 一下，没有就跳过——重装不会抹掉它。
    """
    env_line = f'if exist "{env_file}" call "{env_file}"\r\n' if env_file else ""
    return (
        "@echo off\r\n"
        "rem 由 `pmem schedule install` 生成；改这里会被重装覆盖。\r\n"
        "rem 本机机密（模型 key 等）写到 env.cmd，那一个不会被覆盖。\r\n"
        "setlocal\r\n"
        f"{env_line}"
        f'set "PYTHONPATH={src}"\r\n'
        "set PYTHONIOENCODING=utf-8\r\n"
        f'cd /d "{repo}"\r\n'
        f'"{python}" -m pmem.gate.cli --db "{db}" {action} >> "{log}" 2>&1\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 只在真机出错
        return 1, str(exc)


def task_exists(name: str) -> bool:
    code, _ = _run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"])
    return code == 0


def _task_command(pythonw_exe: str, runner: Path) -> str:
    """任务的执行命令：pythonw 直接跑启动脚本（GUI 子系统 = 不建控制台）。"""
    return f'"{pythonw_exe}" "{runner}"'


def create_task(
    name: str,
    runner: Path,
    *,
    pythonw_exe: str,
    minutes: int | None = None,
    weekly: bool = False,
) -> Step:
    target = _task_command(pythonw_exe, runner)
    if weekly:
        argv = ["schtasks", "/Create", "/F", "/TN", name, "/SC", "WEEKLY", "/D", "SUN", "/ST", DEFAULT_EVAL_AT, "/TR", target]
    else:
        argv = [
            "schtasks", "/Create", "/F", "/TN", name,
            "/SC", "MINUTE", "/MO", str(max(1, int(minutes or DEFAULT_MINUTES))),
            "/TR", target,
        ]
    code, out = _run(argv)
    return Step(name, code == 0, out.strip()[:400] or f"exit {code}")


def create_shortcut(path: Path, target: str, arguments: str, workdir: str) -> Step:
    """在「启动」文件夹里放一个 .lnk（不是 vbs：脚本宿主会被杀软反复弹窗）。

    ``.lnk`` 只是普通快捷方式，登录时 Windows 按图执行；目标还是 pythonw，
    所以也不会出现控制台。
    """
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut('{path}'); "
        f"$l.TargetPath = '{target}'; "
        f"$l.Arguments = '{arguments}'; "
        f"$l.WorkingDirectory = '{workdir}'; "
        "$l.Save()"
    )
    code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    return Step("autostart", code == 0, out.strip()[:300] or ("已写" if code == 0 else f"exit {code}"))


def tune_task(name: str) -> Step:
    """把「错过就补跑 / 不并发 / 隐藏」调成我们要的（失败不影响任务本身）。

    ``schtasks /Create`` 不暴露这些设置，只能用 PowerShell 补一次；这条链要过
    cmd → powershell 两层引号，写错了会静默不生效，所以单独成步、失败只降级不报错。
    """
    script = (
        f"$s = Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop; "
        "$s.Settings.StartWhenAvailable = $true; "
        "$s.Settings.MultipleInstances = 'IgnoreNew'; "
        "$s.Settings.Hidden = $true; "
        "Set-ScheduledTask -TaskName '" + name + "' -Settings $s.Settings | Out-Null"
    )
    code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    return Step(f"{name}:settings", code == 0, (out.strip()[:200] or ("已调" if code == 0 else f"exit {code}")))


def _tune_step(name: str) -> tuple[str, bool, str]:
    """tune_task 的 Report 适配：调设置失败只记一句，不让整个安装失败。"""
    try:
        tuned = tune_task(name)
    except Exception:  # noqa: BLE001
        return f"{name}:settings", True, "跳过"
    return tuned.name, True, tuned.detail


def install(
    *,
    repo: str | Path,
    db: str | Path,
    python: str | None = None,
    home: str | Path | None = None,
    minutes: int = DEFAULT_MINUTES,
    weekly_eval: bool = True,
    autostart: bool = False,
    dry_run: bool = False,
) -> Report:
    """登记计划任务。幂等：`/F` 会覆盖同名任务。

    - ``pmem-daemon``：每 N 分钟 ``daemon --once``；
    - ``pmem-eval``：每周日 03:30 ``eval --record``；
    - ``autostart``（默认**不装**，``schedule install --autostart`` 才装）：
      往用户「启动」文件夹放一个指向 pythonw 的 ``.lnk``。

    任务本身用 ``pythonw.exe`` 跑 ``.py`` 启动脚本：不建控制台、不经过脚本宿主，
    所以既不会闪黑框、也不会每次被火绒问一遍。
    """
    import sys

    report = Report(ok=True)
    repo_path = Path(repo).resolve()
    db_path = Path(db).resolve()
    src = repo_path / "src"
    house = tasks_home(home)
    report.home = str(house)

    exe = python or sys.executable
    pwin = pythonw(exe)
    report.add("pythonw", Path(pwin).name.lower() == "pythonw.exe", pwin)
    plans: list[tuple[str, str, bool]] = [(TASK_DAEMON, "daemon --once", False)]
    if weekly_eval:
        plans.append((TASK_EVAL, "eval --record", True))

    for name, action, weekly in plans:
        wrapper = wrapper_path(house, name)
        runner = runner_path(house, name)
        wrapper_body = build_wrapper(
            python=exe,
            db=str(db_path),
            repo=str(repo_path),
            src=str(src),
            action=action,
            log=log_path(house, name),
            env_file=env_path(house),
        )
        runner_body = build_runner(
            python=exe,
            db=str(db_path),
            repo=str(repo_path),
            src=str(src),
            action=action,
            log=log_path(house, name),
            env_file=env_path(house),
            pythonw_exe=pwin,
        )
        if dry_run:
            report.add("files", True, f"[dry-run] 会写 {wrapper.name} + {runner.name}")
        else:
            try:
                house.mkdir(parents=True, exist_ok=True)
                wrapper.write_text(wrapper_body, encoding=cmd_encoding(), newline="")
                runner.write_text(runner_body, encoding="utf-8", newline="")
                stale = house / f"{name}.vbs"
                stale.unlink(missing_ok=True)  # 0.11 的 vbs 启动器：清掉，别再被弹
                report.add("files", True, f"{wrapper.name} + {runner.name}")
            except OSError as exc:  # pragma: no cover - 只在真机出错
                report.add("files", False, f"{house}: {exc}")
                continue
        if dry_run:
            report.add(
                name,
                True,
                f"[dry-run] schtasks /Create /F /TN {name} /TR {_task_command(pwin, runner)!r}",
            )
            report.tasks.append(name)
            continue
        step = create_task(name, runner, pythonw_exe=pwin, minutes=minutes, weekly=weekly)
        report.add(step.name, step.ok, step.detail)
        if step.ok:
            report.tasks.append(name)
            report.add(*_tune_step(name))

    if autostart:
        target = startup_path(house)
        if target is None:
            report.add("autostart", True, "非 Windows，跳过启动文件夹")
        elif dry_run:
            report.add("autostart", True, f"[dry-run] 会写 {target}")
        else:
            step = create_shortcut(
                target,
                pwin,
                f'"{runner_path(house, TASK_DAEMON)}"',
                str(repo_path),
            )
            report.add(step.name, step.ok, step.detail)
    return report


def status(*, home: str | Path | None = None, python: str | None = None) -> Report:
    """看任务在不在、包装器在不在、日志最后改于何时。"""
    import sys

    house = tasks_home(home)
    report = Report(ok=True)
    report.home = str(house)
    exe = python or sys.executable
    for name in (TASK_DAEMON, TASK_EVAL):
        wrapper = wrapper_path(house, name)
        exists = task_exists(name)
        report.tasks.append(name)
        detail = "已登记" if exists else "未登记"
        runner = runner_path(house, name)
        if runner.exists():
            detail += f" · 启动脚本 {runner.name}"
        else:
            detail += " · 启动脚本缺失（重新 install）"
        if not exists:
            detail += f" · 手动跑：\"{exe}\" -m pmem.gate.cli schedule install"
        report.add(name, True, detail)
        log = log_path(house, name)
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            tail = text.strip().splitlines()[-3:]
            report.add(f"{name}:log", True, f"{log.stat().st_size} 字节 · " + " / ".join(tail)[:300])
        else:
            report.add(f"{name}:log", True, "还没有日志（任务还没跑过）")
    auto = startup_path(house)
    if auto is None:
        report.add("autostart", True, "非 Windows：没有启动文件夹概念")
    elif auto.exists():
        report.add("autostart", True, f"登录自启已装：{auto}（不是 vbs，是 .lnk）")
    else:
        report.add("autostart", True, "登录自启未装（需要时 schedule install --autostart）")
    return report


def uninstall(*, home: str | Path | None = None, dry_run: bool = False) -> Report:
    """摘掉登记；包装器和日志留着（日志是这段时间唯一的运行记录）。"""
    report = Report(ok=True)
    house = tasks_home(home)
    report.home = str(house)
    for name in (TASK_DAEMON, TASK_EVAL):
        if dry_run:
            report.add(name, True, f"[dry-run] schtasks /Delete /TN {name} /F")
            continue
        code, out = _run(["schtasks", "/Delete", "/TN", name, "/F"])
        # 本来就没有 == 目标状态，不算失败
        missing = "cannot find" in out.lower() or "找不到" in out
        report.add(name, code == 0 or missing, out.strip()[:300] or f"exit {code}")
    auto = startup_path(house)
    if auto is not None and auto.exists():
        if dry_run:
            report.add("autostart", True, f"[dry-run] 会删 {auto}")
        else:
            try:
                auto.unlink()
                report.add("autostart", True, f"已删 {auto}")
            except OSError as exc:  # pragma: no cover
                report.add("autostart", False, f"{auto}: {exc}")
    return report


__all__ = [
    "cmd_encoding",
    "install",
    "status",
    "uninstall",
    "build_wrapper",
    "build_runner",
    "pythonw",
    "tune_task",
    "tasks_home",
    "wrapper_path",
    "runner_path",
    "TASK_DAEMON",
    "TASK_EVAL",
    "STARTUP_FILE",
    "startup_dir",
    "startup_path",
    "create_shortcut",
]
