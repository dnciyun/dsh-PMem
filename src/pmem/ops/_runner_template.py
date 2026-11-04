# 由 pmem schedule install 生成；改这里会被重装覆盖。
# 为什么是 .py 而不是 .vbs：vbs 隐藏执行会被杀软当病毒特征反复弹窗；
# pythonw.exe 本身没有控制台，任务拉起它不会出现黑框。
import os
import sys

SRC = __SRC__
LOG = __LOG__
ENV_FILE = __ENV_FILE__
ARGV = __ARGV__
REPO = __REPO__

sys.path.insert(0, SRC)
# 计划任务默认工作目录是 System32；eval/backup 会往仓库下写东西，必须切回来
os.chdir(REPO)

if ENV_FILE and os.path.exists(ENV_FILE):
    with open(ENV_FILE, encoding="mbcs", errors="replace") as fh:
        for line in fh:
            text = line.strip()
            if text[:4].lower() != "set ":
                continue
            name, _, value = text[4:].partition("=")
            name = name.strip().strip('"')
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if name:
                os.environ[name] = value

with open(LOG, "a", encoding="utf-8", buffering=1) as fh:
    sys.stdout = fh
    sys.stderr = fh
    from pmem.gate.cli import main

    raise SystemExit(main(ARGV))
