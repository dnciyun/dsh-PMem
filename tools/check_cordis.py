"""静态检查 PMem 的 cordis 注册片段（M8）。

不解析 YAML（stdlib 没有），只检查最容易把整条插件弄挂的硬约束：
- 只有一个 pmem 的 insert 条目；
- serverName 不撞保留名；
- 不在 qq-bridge MCP 区块里；
- failOnStartupError: false；
- 有 command/args/cwd。

真正的 DSH 启动验证必须在重启后做，这里不假装。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RESERVED = {"snowluma", "snowluma-host", "web-search-safe"}
ROOT = Path(__file__).resolve().parents[1]


def check(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    checks: list[str] = []

    match = re.search(r"serverName:\s*([^\s]+)", text)
    if match is None:
        errors.append("缺少 serverName")
    else:
        name = match.group(1).strip("'\"")
        if name in RESERVED:
            errors.append(f"serverName {name} 撞保留名")
        checks.append(f"serverName={name}")

    if text.count("- id: mcp-pmem") == 1:
        checks.append("exactly one mcp-pmem entry")
    else:
        errors.append("mcp-pmem 条目数不是 1")

    if "failOnStartupError: false" in text:
        checks.append("failOnStartupError=false")
    else:
        errors.append("缺少 failOnStartupError: false")

    if "command:" in text and "args:" in text and "cwd:" in text:
        checks.append("command/args/cwd present")
    else:
        errors.append("缺少 command/args/cwd")

    if re.search(r"(?m)^# === qq-bridge MCP (BEGIN|END) ===", text):
        errors.append("片段里出现了 qq-bridge 区块标记，不能放进去")
    else:
        checks.append("outside qq-bridge block")

    return {"ok": not errors, "file": str(path), "checks": checks, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=str(ROOT / "deploy" / "cordis.patch.yml"))
    args = parser.parse_args(argv)
    result = check(Path(args.path))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
