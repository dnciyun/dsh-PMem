"""测试包：把 ``src`` 放进 sys.path，保证不装包也能跑。

跑法（仓库根目录）：``python -m unittest discover -s tests -t . -v``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 测试默认关掉向量通道（M13）：它依赖 ~/.dsh/pmem/models 下的外部模型文件，
# 「装了模型」和「没装模型」的机器上测试行为必须一致；要测它自己的测试显式打开。
from pmem.base.config import DEFAULTS  # noqa: E402

DEFAULTS["retrieval"]["vector"]["enabled"] = False

# 自我层同理：~/.dsh/pmem/soul 在真机上存在，测试必须用临时目录或显式关掉。
DEFAULTS["identity"]["enabled"] = False
