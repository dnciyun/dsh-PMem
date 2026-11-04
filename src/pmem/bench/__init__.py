"""长期记忆测试台（M10）：按剧本回放六个月，用真模型当被试点。

和 `pmem eval` 的区别：那个跑合成集，量的是检索算法；这个跑**完整会话**，
量的是「注入 + 工具 + 记忆」合起来在时间流逝后的表现。

三条硬约束（对应测试方案 §2.3 / §3.2 / §3.3）：

1. 时间必须真的往前走 —— 走 `clock.set_now`，不靠改数据；
2. 不许绕过系统自己的流程 —— 每轮都是新会话，只给醒来卡 + 浮现；
3. 每轮的 token 都要记 —— 那是「长期使用的成本」，不能事后估。
"""

from .corpus import Corpus, Day, Exam, Persona, Turn, load_corpus
from .model import Model, OpenAICompatModel, StubModel, Usage, make_model
from .runner import ReplayResult, TurnRecord, replay

__all__ = [
    "Corpus",
    "Day",
    "Exam",
    "Persona",
    "Turn",
    "load_corpus",
    "Model",
    "OpenAICompatModel",
    "StubModel",
    "make_model",
    "Usage",
    "ReplayResult",
    "TurnRecord",
    "replay",
]
