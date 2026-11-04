"""M9.8：把插件装进 DSH profile 的安装器。

这里守的是**可预演、可回滚、不碰别人的东西**：

- 装之前不知道会改什么 → ``status`` / ``--dry-run`` 必须说清楚；
- 装完了要能原样撤掉，而且**只撤自己加的**（其它插件的条目、qq-bridge 区块、
  记忆库都不许动）；
- 仓库在哪就写哪的路径（这一版就是为了干掉写死的 ``F:/Ai/AWB``）；
- 反复装不许叠出第二份注册块。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pmem.gate import dsh_install

from .support import drop_dir, temp_dir

PROFILE_PATCH = """\
- id: ui-chat
  config:
    transcriptView: standard
# === qq-bridge MCP BEGIN ===
# 由 scripts/setup-dsh.mjs 维护
- insert:
    - id: mcp-snowluma
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: snowluma
# === qq-bridge MCP END ===

- id: dsh-social-bridge-plugin
  config:
    port: 11230
"""


class InstallerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m98")
        self.home = Path(self._dir) / "home"
        self.repo = Path(self._dir) / "repo"
        (self.repo / "deploy" / "dsh-pmem").mkdir(parents=True)
        (self.repo / "deploy" / "dsh-pmem" / "index.js").write_text("export {}\n", encoding="utf-8")
        (self.repo / "src").mkdir(parents=True)
        self.profile = self.home / ".dsh" / "profiles" / "desktop"
        self.profile.mkdir(parents=True)
        self.pkg_path = self.profile / "package.json"
        self.patch_path = self.profile / "cordis.patch.yml"
        self.pkg_path.write_text(
            json.dumps(
                {
                    "name": "dsh-profile-desktop",
                    "dependencies": {"some-other": "1.0.0"},
                    "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "some-other"]}},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.patch_path.write_text(PROFILE_PATCH, encoding="utf-8")
        self.db = self.repo / "data" / "pmem.db"

    def tearDown(self) -> None:
        drop_dir(self._dir)

    def _install(self, **kwargs):
        return dsh_install.install(
            profile="desktop", repo=self.repo, db=self.db, home=self.home, **kwargs
        )

    def _pkg(self) -> dict:
        return json.loads(self.pkg_path.read_text(encoding="utf-8"))

    # ---- 安装 -------------------------------------------------------------

    def test_install_registers_bundle_and_mcp(self) -> None:
        report = self._install()
        self.assertTrue(report.ok, report.render())
        pkg = self._pkg()
        self.assertIn("dsh-pmem", pkg["dependencies"])
        self.assertTrue(pkg["dependencies"]["dsh-pmem"].startswith("link:"))
        self.assertIn("dsh-pmem", pkg["dsh"]["profile"]["bundles"])
        # 别人的东西不许动
        self.assertEqual(pkg["dependencies"]["some-other"], "1.0.0")
        self.assertIn("@deepseek-ai/dsh-base", pkg["dsh"]["profile"]["bundles"])

        text = self.patch_path.read_text(encoding="utf-8")
        self.assertIn(dsh_install.BEGIN, text)
        self.assertIn(dsh_install.END, text)
        self.assertIn("mcp-pmem", text)
        self.assertIn(str(self.repo / "src"), text)
        self.assertIn(str(self.db), text)
        # qq-bridge 区块必须原样留着
        self.assertIn("# === qq-bridge MCP BEGIN ===", text)
        self.assertIn("mcp-snowluma", text)

    def test_install_creates_the_database(self) -> None:
        self.assertFalse(self.db.exists())
        self._install()
        self.assertTrue(self.db.exists(), "装完应该有一个空库，而不是等第一次写入")

    def test_install_is_idempotent(self) -> None:
        self._install()
        before_pkg = self.pkg_path.read_text(encoding="utf-8")
        before_patch = self.patch_path.read_text(encoding="utf-8")
        report = self._install()
        self.assertFalse(report.changed, f"第二次安装不该再改文件：{report.render()}")
        self.assertTrue(all(s.action in ("keep", "warn") for s in report.steps), report.render())
        self.assertEqual(self.pkg_path.read_text(encoding="utf-8"), before_pkg)
        self.assertEqual(self.patch_path.read_text(encoding="utf-8"), before_patch)
        self.assertEqual(self.patch_path.read_text(encoding="utf-8").count(dsh_install.BEGIN), 1)

    def test_dry_run_writes_nothing(self) -> None:
        before_pkg = self.pkg_path.read_text(encoding="utf-8")
        before_patch = self.patch_path.read_text(encoding="utf-8")
        report = self._install(dry_run=True)
        self.assertTrue(report.changed, "预演也要报告会改什么")
        self.assertEqual(self.pkg_path.read_text(encoding="utf-8"), before_pkg)
        self.assertEqual(self.patch_path.read_text(encoding="utf-8"), before_patch)
        self.assertFalse(self.db.exists())

    def test_reinstall_moves_the_paths(self) -> None:
        self._install()
        other = Path(self._dir) / "moved"
        (other / "deploy" / "dsh-pmem").mkdir(parents=True)
        (other / "deploy" / "dsh-pmem" / "index.js").write_text("export {}\n", encoding="utf-8")
        (other / "src").mkdir(parents=True)
        dsh_install.install(
            profile="desktop", repo=other, db=other / "data" / "pmem.db",
            home=self.home, make_db=False,
        )
        text = self.patch_path.read_text(encoding="utf-8")
        self.assertEqual(text.count(dsh_install.BEGIN), 1, "重复安装不许叠出第二块")
        self.assertIn(str(other / "src"), text)
        self.assertNotIn(str(self.repo / "src"), text)

    def test_missing_repo_is_reported_not_written(self) -> None:
        before = self.patch_path.read_text(encoding="utf-8")
        report = dsh_install.install(
            profile="desktop", repo=Path(self._dir) / "nope", db=self.db,
            home=self.home,
        )
        self.assertFalse(report.ok)
        self.assertIn("missing", [step.action for step in report.steps])
        self.assertEqual(self.patch_path.read_text(encoding="utf-8"), before)

    # ---- 卸载 -------------------------------------------------------------

    def test_uninstall_restores_the_original_files(self) -> None:
        pkg_before = self._pkg()
        self._install()
        report = dsh_install.uninstall(profile="desktop", home=self.home)
        self.assertTrue(report.changed, report.render())
        self.assertEqual(self._pkg(), pkg_before, "卸载后 package.json 应当回到原样")
        text = self.patch_path.read_text(encoding="utf-8")
        self.assertNotIn(dsh_install.BEGIN, text)
        self.assertNotIn("mcp-pmem", text)
        self.assertIn("mcp-snowluma", text, "别人的 MCP 条目被误删了")
        self.assertIn("dsh-social-bridge-plugin", text)
        self.assertTrue(self.db.exists(), "卸载不许删记忆库")

    def test_uninstall_is_idempotent(self) -> None:
        self._install()
        dsh_install.uninstall(profile="desktop", home=self.home)
        second = dsh_install.uninstall(profile="desktop", home=self.home)
        self.assertFalse(second.changed)
        self.assertTrue(all(step.action in ("keep", "warn") for step in second.steps))

    # ---- 体检 -------------------------------------------------------------

    def test_status_reports_each_piece(self) -> None:
        fresh = dsh_install.status(profile="desktop", repo=self.repo, db=self.db, home=self.home)
        actions = {step.key: step.action for step in fresh.steps}
        self.assertEqual(actions["repo"], "keep")
        self.assertEqual(actions["dep"], "missing")
        self.assertEqual(actions["bundle"], "missing")
        self.assertEqual(actions["patch"], "missing")
        self.assertEqual(actions["db"], "missing")

        self._install()
        after = dsh_install.status(profile="desktop", repo=self.repo, db=self.db, home=self.home)
        actions = {step.key: step.action for step in after.steps}
        self.assertEqual(actions["dep"], "keep")
        self.assertEqual(actions["bundle"], "keep")
        self.assertEqual(actions["patch"], "keep")
        self.assertEqual(actions["db"], "keep")
        self.assertTrue(after.ok)

    def test_status_notices_a_stale_path(self) -> None:
        self._install()
        text = self.patch_path.read_text(encoding="utf-8")
        self.patch_path.write_text(
            text.replace(str(self.repo / "src"), "F:/Ai/AWB/src"), encoding="utf-8"
        )
        report = dsh_install.status(profile="desktop", repo=self.repo, db=self.db, home=self.home)
        step = next(s for s in report.steps if s.key == "patch")
        self.assertEqual(step.action, "update", "路径对不上时体检要报出来")
        self.assertTrue(any("重跑" in note for note in report.notes))

    def test_node_modules_gap_is_a_warning_not_a_failure(self) -> None:
        report = self._install()
        step = next(s for s in report.steps if s.key == "link")
        self.assertEqual(step.action, "warn")
        self.assertTrue(report.ok, "还没 pnpm install 不算安装失败")
        self.assertTrue(any("pnpm install" in note for note in report.notes))


class RepoRootCase(unittest.TestCase):
    def test_repo_root_is_this_checkout(self) -> None:
        root = dsh_install.repo_root()
        self.assertTrue((root / "src" / "pmem" / "gate" / "dsh_install.py").exists(), root)
        self.assertTrue((root / "deploy" / "dsh-pmem").is_dir(), root)

    def test_mcp_block_uses_absolute_paths(self) -> None:
        repo = Path("C:/somewhere/repo")
        block = dsh_install.mcp_block(repo=repo, db=repo / "data" / "pmem.db")
        self.assertIn("pmem.gate.mcp_server", block)
        self.assertIn(str(repo / "data" / "pmem.db"), block)
        self.assertIn(str(repo / "src"), block)
        self.assertNotIn("F:/Ai/AWB", block.replace("\\", "/"))
        self.assertIn("failOnStartupError: false", block)
        self.assertEqual(block.count(dsh_install.BEGIN), 1)


if __name__ == "__main__":
    unittest.main()
