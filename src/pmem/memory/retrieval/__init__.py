"""检索层：lexical（词法通道）+ channels（候选装配）+ search（编排）。

从 ``memory/store.py`` 拆出来的原因只有一个：那 1500 行跟写入/更新/删除混在一个
文件里，改任何一路检索都要在同一个文件里翻。拆完之后 ``MemoryStore`` 仍是唯一入口
（`MemoryStore.search` 与旧私有 `_chan_*` 都还在），这些模块只收它显式传进来的
store/db，**不 import store.py**，所以不会产生循环依赖。

三层各管什么：

- ``lexical``：FTS5 三条词法通道 + 全库 IDF 词权（原来 `_chan_*` / `_term_weights`）；
- ``channels``：融合之后的候选装配——图/时链/实体/向量补候选、原话兜底、
  按预算塞进 k 个槽位、以及召回日志（原来 `_*_append` / `_fetch_scored` 等）；
- ``search``：把上面这些按顺序串起来的那一个 ``run_search``（原来 `MemoryStore.search`
  794 行的函数体），不含 SQL。
"""

from __future__ import annotations

from . import channels, lexical, search
from .search import run_search

__all__ = ["channels", "lexical", "search", "run_search"]
