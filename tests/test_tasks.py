"""M9.9：计划任务——让 30 天日历门真的开始计时（只看登记逻辑，不碰真机）。

0.12.0 起任务用 ``pythonw.exe`` 跑 ``.py`` 启动脚本：不建控制台（不闪黑框），
也不经过脚本宿主（不触发杀软「隐藏执行」弹窗）。
"""

from __future__ import annotations

import sys
import unittest

# 计划任务是 Windows 专属（schtasks / .cmd 包装器）：别的平台上这些断言没有意义，
# 直接跳过，别让 CI 在 Linux 上红一片。
if sys.platform != "win32":
    raise unittest.SkipTest("Windows 计划任务：非 win32 平台跳过")  # noqa: B904

import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from pmem.gate import cli
from pmem.ops import tasks

from .support import drop_dir, temp_dir


class Build(Case := unittest.TestCase):
    def test_wrapper_is_runnable_and_commented(self) -> None:
        body = tasks.build_wrapper(
            python=r"C:\\Py\\python.exe",
            db=r"F:\\Ai\\AWB\\data\\pmem.db",
            repo=r"F:\\Ai\\AWB",
            src=r"F:\\Ai\\AWB\\src",
            action="daemon --once",
            log=Path(r"C:\\Users\\x\\.dsh\\pmem\\tasks\\pmem-daemon.log"),
        )
        self.assertIn('set "PYTHONPATH=F:\\\\Ai\\\\AWB\\\\src"', body)
        self.assertIn('cd /d "F:\\\\Ai\\\\AWB"', body)
        self.assertIn("daemon --once", body)
        self.assertIn(">> ", body)
        self.assertTrue(body.startswith("@echo off"))
        self.assertIn(chr(13) + chr(10), body)

    def test_env_file_is_called_when_present(self) -> None:
        """本机机密（模型 key）不进包装器，放进 env.cmd；重装只会保留这一行调用。"""
        env = Path(r"C:\\\\Users\\\\x\\\\.dsh\\\\pmem\\\\env.cmd")
        body = tasks.build_wrapper(
            python="py",
            db="d",
            repo="r",
            src="s",
            action="daemon --once",
            log=Path("l"),
            env_file=env,
        )
        self.assertIn("if exist", body)
        plain = tasks.build_wrapper(python="py", db="d", repo="r", src="s", action="a", log=Path("l"))
        self.assertNotIn("if exist", plain)
        self.assertEqual(
            tasks.env_path(tasks.tasks_home(r"C:\\Users\\x")), Path(r"C:\Users\x\.dsh\pmem\env.cmd")
        )

    def test_pythonw_prefers_gui_interpreter(self) -> None:
        """pythonw = GUI 子系统：任务拉起它时系统不建控制台。"""
        found = tasks.pythonw()
        self.assertTrue(found.lower().endswith(".exe"))
        self.assertIn(Path(found).name.lower(), ("pythonw.exe", "python.exe"))

    def test_runner_redirects_output_and_reads_env(self) -> None:
        body = tasks.build_runner(
            python=r"C:\\Py\\python.exe",
            db=r"F:\\Ai\\AWB\\data\\pmem.db",
            repo=r"F:\\Ai\\AWB",
            src=r"F:\\Ai\\AWB\\src",
            action="daemon --once",
            log=Path(r"C:\\Users\\x\\.dsh\\pmem\\tasks\\pmem-daemon.log"),
            env_file=Path(r"C:\\Users\\x\\.dsh\\pmem\\env.cmd"),
            pythonw_exe=r"C:\\Py\\pythonw.exe",
        )
        # pythonw 没有控制台，>> 不存在 → 输出必须在进程内重定向
        self.assertIn("sys.stdout = fh", body)
        self.assertIn("sys.path.insert(0, SRC)", body)
        self.assertIn("pmem.gate.cli", body)
        self.assertIn("--db", body)
        # env.cmd 兼容：原来是 cmd 的 call
        self.assertIn("ENV_FILE", body)
        self.assertIn("os.environ[name] = value", body)
        # 计划任务默认工作目录是 System32；eval/backup 会往仓库下写东西，必须切回来
        self.assertIn("os.chdir(REPO)", body)
        self.assertIn("REPO = ", body)
        compile(body, "<runner>", "exec")

    def test_task_command_uses_pythonw_not_cmd(self) -> None:
        launcher = Path("C:/x/pmem-daemon.py")
        self.assertEqual(
            tasks._task_command(r"C:/Py/pythonw.exe", launcher),
            '"C:/Py/pythonw.exe" ' + '"' + str(launcher) + '"',
        )

    def test_paths_live_under_dsh_home(self) -> None:
        home = tasks.tasks_home(r"C:\\Users\\x")
        self.assertEqual(str(home).replace("/", "\\"), r"C:\Users\x\.dsh\pmem\tasks")
        self.assertTrue(str(tasks.wrapper_path(home, tasks.TASK_DAEMON)).endswith("pmem-daemon.cmd"))
        self.assertTrue(str(tasks.runner_path(home, tasks.TASK_DAEMON)).endswith("pmem-daemon.py"))


class Install(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("tasks")
        self.home = Path(self._dir)
        self.appdata = Path(self._dir) / "AppData" / "Roaming"

    def tearDown(self) -> None:
        drop_dir(self._dir)

    def _env(self) -> dict:
        return {"APPDATA": str(self.appdata)}

    def test_dry_run_writes_nothing(self) -> None:
        with mock.patch.dict("os.environ", self._env()):
            report = tasks.install(
                repo="F:/Ai/AWB", db="F:/Ai/AWB/data/pmem.db", home=self.home, dry_run=True
            )
        self.assertTrue(report.ok)
        self.assertEqual(len(report.tasks), 2)
        self.assertFalse(tasks.tasks_home(self.home).exists())
        self.assertTrue(any("[dry-run]" in step.detail for step in report.steps))

    def test_no_eval_skips_weekly_task(self) -> None:
        with mock.patch.dict("os.environ", self._env()), mock.patch.object(
            tasks, "_run", return_value=(0, "ok")
        ):
            report = tasks.install(
                repo="F:/Ai/AWB", db="F:/Ai/AWB/data/pmem.db", home=self.home, weekly_eval=False
            )
        self.assertTrue(report.ok)
        self.assertEqual(report.tasks, [tasks.TASK_DAEMON])

    def test_install_writes_wrapper_and_runner(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv):
            calls.append(argv)
            return 0, "ok"

        with mock.patch.dict("os.environ", self._env()), mock.patch.object(
            tasks, "_run", side_effect=fake_run
        ):
            report = tasks.install(repo="F:/Ai/AWB", db="F:/Ai/AWB/data/pmem.db", home=self.home)

        self.assertTrue(report.ok)
        house = tasks.tasks_home(self.home)
        wrapper = tasks.wrapper_path(house, tasks.TASK_DAEMON)
        runner = tasks.runner_path(house, tasks.TASK_DAEMON)
        self.assertTrue(wrapper.exists(), "给人双击用的 .cmd 还要留")
        self.assertTrue(runner.exists(), "任务真正跑的是 .py")
        self.assertIn("daemon --once", wrapper.read_text(encoding=tasks.cmd_encoding()))
        compile(runner.read_text(encoding="utf-8"), "<runner>", "exec")

        creates = [c for c in calls if "/Create" in c]
        self.assertEqual(len(creates), 2)
        for call in creates:
            target = call[call.index("/TR") + 1]
            self.assertIn("pythonw", target.lower())
            self.assertTrue(target.rstrip('"').endswith(".py"), target)
            self.assertNotIn(".cmd", target)
            self.assertNotIn(".vbs", target)
        self.assertTrue(any("/SC" in c and "WEEKLY" in c for c in calls))
        self.assertFalse((self.appdata / "Microsoft/Windows/Start Menu/Programs/Startup").exists())

    def test_install_cleans_stale_vbs(self) -> None:
        house = tasks.tasks_home(self.home)
        house.mkdir(parents=True, exist_ok=True)
        stale = house / f"{tasks.TASK_DAEMON}.vbs"
        stale.write_text("old", encoding="utf-8")
        with mock.patch.dict("os.environ", self._env()), mock.patch.object(
            tasks, "_run", return_value=(0, "ok")
        ):
            tasks.install(repo="F:/Ai/AWB", db="F:/Ai/AWB/data/pmem.db", home=self.home)
        self.assertFalse(stale.exists(), "0.11 的 vbs 启动器必须清掉，否则又被杀软弹")

    def test_autostart_is_opt_in_shortcut(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv):
            calls.append(argv)
            return 0, "ok"

        with mock.patch.dict("os.environ", self._env()), mock.patch.object(
            tasks, "_run", side_effect=fake_run
        ):
            report = tasks.install(
                repo="F:/Ai/AWB", db="F:/Ai/AWB/data/pmem.db", home=self.home, autostart=True
            )
        self.assertTrue(report.ok)
        self.assertTrue(any("CreateShortcut" in " ".join(c) for c in calls))
        auto = tasks.startup_path(tasks.tasks_home(self.home))
        self.assertIsNotNone(auto)
        self.assertTrue(str(auto).endswith("pmem-startup.lnk"))

    def test_schtasks_failure_is_reported_not_raised(self) -> None:
        with mock.patch.dict("os.environ", self._env()), mock.patch.object(
            tasks, "_run", return_value=(1, "拒绝访问")
        ):
            report = tasks.install(repo="F:/Ai/AWB", db="F:/Ai/AWB/data/pmem.db", home=self.home)
        self.assertFalse(report.ok)
        self.assertEqual(report.tasks, [])
        self.assertIn("拒绝访问", " ".join(step.detail for step in report.steps))


class Status(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("tasks-status")
        self.home = Path(self._dir)
        self.appdata = Path(self._dir) / "AppData" / "Roaming"

    def tearDown(self) -> None:
        drop_dir(self._dir)

    def test_status_reports_missing_tasks_and_logs(self) -> None:
        with mock.patch.dict("os.environ", {"APPDATA": str(self.appdata)}), mock.patch.object(
            tasks, "task_exists", return_value=False
        ):
            report = tasks.status(home=self.home)
        self.assertTrue(report.ok)
        text = " ".join(step.detail for step in report.steps)
        self.assertIn("未登记", text)
        self.assertIn("还没有日志", text)
        self.assertIn("登录自启未装", text)

    def test_status_shows_log_tail(self) -> None:
        house = tasks.tasks_home(self.home)
        house.mkdir(parents=True)
        (house / "pmem-daemon.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"APPDATA": str(self.appdata)}), mock.patch.object(
            tasks, "task_exists", return_value=True
        ):
            report = tasks.status(home=self.home)
        text = " ".join(step.detail for step in report.steps)
        self.assertIn("已登记", text)
        self.assertIn("line3", text)


class Uninstall(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("tasks-un")
        self.home = Path(self._dir)
        self.appdata = Path(self._dir) / "AppData" / "Roaming"

    def tearDown(self) -> None:
        drop_dir(self._dir)

    def test_uninstall_removes_tasks_and_autostart(self) -> None:
        calls: list[list[str]] = []

        with mock.patch.dict("os.environ", {"APPDATA": str(self.appdata)}), mock.patch.object(
            tasks, "_run", side_effect=lambda argv: (calls.append(argv), (0, "ok"))[1]
        ):
            report = tasks.uninstall(home=self.home)
        self.assertTrue(report.ok)
        deletes = [c for c in calls if "/Delete" in c]
        self.assertEqual(len(deletes), 2)
        names = {c[c.index("/TN") + 1] for c in deletes}
        self.assertEqual(names, {tasks.TASK_DAEMON, tasks.TASK_EVAL})

    def test_uninstall_treats_absent_task_as_done(self) -> None:
        with mock.patch.dict("os.environ", {"APPDATA": str(self.appdata)}), mock.patch.object(
            tasks, "_run", return_value=(1, "ERROR: cannot find the task")
        ):
            report = tasks.uninstall(home=self.home)
        self.assertTrue(report.ok)

    def test_uninstall_dry_run_does_not_call_schtasks(self) -> None:
        with mock.patch.dict("os.environ", {"APPDATA": str(self.appdata)}), mock.patch.object(
            tasks, "_run"
        ) as run:
            report = tasks.uninstall(home=self.home, dry_run=True)
        run.assert_not_called()
        self.assertTrue(report.ok)


class Heartbeat(unittest.TestCase):
    """计划任务每 10 分钟跑一次；没活干时也要留一行日志，否则看起来像没跑。"""

    def test_quiet_tick_prints_heartbeat(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            cli._print_tick(
                {
                    "at": "2026-11-03T20:48:23+00:00",
                    "ran": [],
                    "journal": {"ok": True, "skipped": "recent"},
                    "jobs": [{"id": "a"}, {"id": "b"}],
                },
                heartbeat=True,
            )
        out = buf.getvalue()
        self.assertIn("心跳", out)
        self.assertIn("日记 recent", out)
        self.assertIn("任务 2", out)

    def test_no_runner_output_is_silent(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            cli._print_tick({"at": "2026-11-03T20:48:23+00:00", "ran": [], "journal": {}, "jobs": []})
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
