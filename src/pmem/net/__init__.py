"""PMem Net：实体、关系、图查询（M3）。

对外三件事：``entities``（识别与解析）、``relations``（抽边与维护）、
``graph``（1/2 跳查询）。核心不依赖任何第三方库，也不调用模型。
"""

from __future__ import annotations

from . import entities, graph, lexicon, relations

__all__ = ["entities", "graph", "lexicon", "relations"]
