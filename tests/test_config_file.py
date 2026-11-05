"""配置文件（0.36.0）：``pmem config get/set/list/path`` 与默认路径。

为什么钉这几条：
1. 插件设置页的开关靠这条命令落盘，契约（JSON 形状）变了插件就静默失效；
2. 默认路径必须跟着 ``DSH_HOME`` 走——测试/部署会把它指到临时目录，写死
   ``~/.dsh`` 会把用户真实配置改掉；
3. ``config get`` 返回的是**生效值**（文件 > 默认值），插件用 false 当默认，
   所以「文件里没有这个键」也必须能读出 False，不能报错。
"""

from __future__ import annotations

import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from pmem.base.config import Config, default_config_path
from pmem.gate import cli

from .support import drop_dir, temp_dir


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class ConfigFileCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("config-file")
        self.db = str(self.dir / "pmem.db")
        self.config = str(self.dir / "config.toml")

    def tearDown(self) -> None:
        drop_dir(self.dir)

    def _json(self, *args: str) -> dict:
        code, out = run_cli(["--db", self.db, "--config", self.config, "--json", *args])
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def test_missing_file_reads_defaults(self) -> None:
        data = self._json("config", "get", "sleep.whale.enabled")
        self.assertTrue(data["ok"], data)
        self.assertIs(data["data"]["value"], False, "夜间真模型开关默认必须是关")

    def test_set_then_get_round_trip(self) -> None:
        data = self._json("config", "set", "sleep.whale.enabled", "true")
        self.assertIs(data["data"]["value"], True)
        data = self._json("config", "get", "sleep.whale.enabled")
        self.assertIs(data["data"]["value"], True)
        # 文件是可被 tomllib 读回来的点号键 TOML，且只写这一行
        text = Path(self.config).read_text(encoding="utf-8")
        self.assertIn("sleep.whale.enabled = true", text)
        self.assertTrue(Config.load(self.config).get("sleep.whale.enabled"))

    def test_set_keeps_other_keys_and_types(self) -> None:
        self._json("config", "set", "retrieval.time_router.cap_k", "12")
        self._json("config", "set", "sleep.whale.enabled", "true")
        self._json("config", "set", "journal.model", "deepseek-chat")
        cfg = Config.load(self.config)
        self.assertEqual(cfg.get("retrieval.time_router.cap_k"), 12)
        self.assertEqual(cfg.get("journal.model"), "deepseek-chat")
        self.assertTrue(cfg.get("sleep.whale.enabled"))
        data = self._json("config", "list")
        joined = "\n".join(data["data"]["items"])
        self.assertIn("retrieval.time_router.cap_k = 12", joined)
        self.assertIn("journal.model = \"deepseek-chat\"", joined)

    def test_set_false_writes_boolean_not_string(self) -> None:
        self._json("config", "set", "sleep.whale.enabled", "false")
        text = Path(self.config).read_text(encoding="utf-8")
        self.assertIn("sleep.whale.enabled = false", text)
        self.assertIs(Config.load(self.config).get("sleep.whale.enabled"), False)


class DefaultPathCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("config-home")

    def tearDown(self) -> None:
        drop_dir(self.dir)

    def test_default_path_follows_dsh_home(self) -> None:
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.dir)}):
            self.assertEqual(default_config_path(), self.dir / "pmem" / "config.toml")

    def test_cli_uses_dsh_home_by_default(self) -> None:
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.dir)}):
            code, out = run_cli(["--json", "config", "path"])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), str(self.dir / "pmem" / "config.toml"))

    def test_cli_writes_inside_dsh_home(self) -> None:
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.dir)}):
            code, out = run_cli(
                ["--db", str(self.dir / "pmem.db"), "--json", "config", "set",
                 "sleep.whale.enabled", "true"]
            )
        self.assertEqual(code, 0, out)
        path = self.dir / "pmem" / "config.toml"
        self.assertTrue(path.exists())
        self.assertTrue(Config.load(path).get("sleep.whale.enabled"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
