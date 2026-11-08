"""交叉编码器重排（task-49，默认关）：检索第二阶段，bge-reranker-base ONNX。

为什么要有第二阶段：现在的相关性是「词面覆盖率 + 向量 + 重要性」的**无交互**近似；
两个候选都提到同一个词，但一个在讲需求、一个在讲反例，无交互分不开。交叉编码器把
(query, doc) 拼成一对送进 BERT，让 attention 直接看两者关系，再打分。

工程约束：
- **默认关**：`retrieval.rerank.enabled=false` 时 search 不会 import onnxruntime、
  不会发额外 SQL，行为与 0.40.0 逐字节一致；
- **缺件静默降级**：onnxruntime / tokenizer / onnx 任一缺失，``ready()`` 返回 False，
  search 继续走原路径，不抛异常；
- 模型不随仓库走：``scripts/fetch_model.py --model rerank`` 落到
  ``~/.dsh/pmem/models/bge-reranker-base``（sha256 + .part 原子下载）。

分词器口径：Xenova/bge-reranker-base 的 ``tokenizer.json`` 是 Unigram (XLM-R)，
不是 BERT WordPiece；这里按 ``tokenizer.json`` 的 vocab + scores 做一个最小 Unigram
Viterbi（不引入 transformers/tokenizers 新依赖）。句对格式按 XLM-R：
``<s> q </s></s> d </s>``，attention_mask=1，token_type_ids=0（模型输入只吃前两列）。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "bge-reranker-base"
DEFAULT_POOL = 40
DEFAULT_MAX_LEN = 320
DEFAULT_THREADS = 1
DEFAULT_BLEND = 1.0
# 按 fetch_model.py 下载的 onnx 文件名优先取量化档（279 MB），再退 int8/fp32。
ONNX_CANDIDATES = (
    "onnx/model_quantized.onnx",
    "onnx/model_int8.onnx",
    "onnx/model.onnx",
)

_SESSION_CACHE: dict[tuple[str, float, int], Any] = {}
_TOKENIZER_CACHE: dict[tuple[str, float], Any] = {}


def _cfg(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    try:
        return cfg.get(key, default)
    except Exception:  # noqa: BLE001 坏 config 当作缺省
        return default


def settings(cfg: Any) -> dict[str, Any]:
    raw = _cfg(cfg, "retrieval.rerank")
    data = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(data.get("enabled", False)),
        "pool": max(1, int(data.get("pool", DEFAULT_POOL) or DEFAULT_POOL)),
        "max_len": max(8, int(data.get("max_len", DEFAULT_MAX_LEN) or DEFAULT_MAX_LEN)),
        "threads": max(1, int(data.get("threads", DEFAULT_THREADS) or DEFAULT_THREADS)),
        "blend": max(0.0, min(1.0, float(data.get("blend", DEFAULT_BLEND) or 0.0))),
    }


def model_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".dsh" / "pmem" / "models" / DEFAULT_MODEL


def onnx_path() -> Path | None:
    root = model_dir()
    for rel in ONNX_CANDIDATES:
        candidate = root / rel.replace("/", os.sep)
        if candidate.is_file():
            return candidate
    return None


def tokenizer_path() -> Path:
    return model_dir() / "tokenizer.json"


def ready(cfg: Any) -> tuple[bool, str]:
    """模型 / tokenizer / onnxruntime 三样齐不齐；任何一样缺失都返回 False（不抛）。"""
    if not settings(cfg)["enabled"]:
        return False, "retrieval.rerank.enabled=false"
    if tokenizer_path().is_file() is False:
        return False, f"缺 tokenizer.json（{model_dir()}）"
    if onnx_path() is None:
        return False, f"缺 reranker onnx（{model_dir()}）"
    try:
        import onnxruntime  # noqa: F401 只在真要用的时候 import
    except Exception as exc:  # noqa: BLE001
        return False, f"onnxruntime 不可用：{exc}"
    return True, "ok"


class UnigramTokenizer:
    """tokenizer.json（Unigram）的最小 Viterbi：不引入 tokenizers/transformers。

    只服务重排器的**相对分数**：分词错一点不会让排序翻盘，但绝不能引入新依赖。
    预分词按 XLM-R 的 Metaspace：空格 → ``▁``，并在开头补一个 ``▁``。
    """

    def __init__(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        model = data.get("model") or {}
        vocab = model.get("vocab") or []
        self.vocab: dict[str, int] = {}
        self.scores: dict[str, float] = {}
        for item in vocab:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            piece, score = str(item[0]), float(item[1])
            self.vocab[piece] = len(self.vocab)
            self.scores[piece] = score
        special = {}
        for token in data.get("added_tokens") or []:
            content = str(token.get("content") or "")
            if content:
                special[content] = int(token.get("id") or 0)
        self.cls_id = int(special.get("<s>", self.vocab.get("<s>", 0)))
        self.sep_id = int(special.get("</s>", self.vocab.get("</s>", 2)))
        self.pad_id = int(special.get("<pad>", self.vocab.get("<pad>", 1)))
        self.unk_id = int(special.get("<unk>", self.vocab.get("<unk>", 3)))
        self.max_piece = 24

    def _normalize(self, text: str) -> str:
        text = unicodedata.normalize("NFKC", str(text or ""))
        text = " ".join(text.split())
        # Metaspace：空格替换为 ▁，并在开头补 ▁（XLM-R 的口径）。
        return "▁" + text.replace(" ", "▁")

    def _tokenize(self, text: str) -> list[int]:
        normalized = self._normalize(text)
        length = len(normalized)
        if not length:
            return []
        neg = float("-inf")
        best = [neg] * (length + 1)
        back: list[tuple[int, str] | None] = [None] * (length + 1)
        best[0] = 0.0
        for start in range(length):
            if best[start] == neg:
                continue
            limit = min(length, start + self.max_piece)
            for end in range(start + 1, limit + 1):
                piece = normalized[start:end]
                score = self.scores.get(piece)
                if score is None:
                    continue
                candidate = best[start] + score
                if candidate > best[end]:
                    best[end] = candidate
                    back[end] = (start, piece)
        ids: list[int] = []
        cursor = length
        while cursor > 0 and back[cursor] is not None:
            start, piece = back[cursor]  # type: ignore[misc]
            ids.append(self.vocab.get(piece, self.unk_id))
            cursor = start
        ids.reverse()
        if cursor > 0:  # 有字符没被任何 piece 覆盖：剩下的逐字按 unk 兜底
            ids = [self.unk_id] * cursor + ids
        return ids

    def encode(self, text: str) -> list[int]:
        return self._tokenize(text)

    def pair(
        self, query: str, doc: str, *, max_len: int = DEFAULT_MAX_LEN
    ) -> tuple[list[int], list[int], list[int]]:
        """返回 (input_ids, attention_mask, token_type_ids)；XLM-R 双 SEP。"""
        q_ids = self._tokenize(query)
        d_ids = self._tokenize(doc)
        budget = max(8, int(max_len))
        ids = [self.cls_id, *q_ids, self.sep_id, self.sep_id, *d_ids, self.sep_id]
        if len(ids) > budget:
            # 先保 query，再截 doc；最后保留结尾 SEP。
            keep_q = min(len(q_ids), max(1, budget // 2))
            room = max(1, budget - keep_q - 4)
            ids = [self.cls_id, *q_ids[:keep_q], self.sep_id, self.sep_id,
                   *d_ids[:room], self.sep_id]
            ids = ids[: budget - 1] + [self.sep_id]
        mask = [1] * len(ids)
        types = [0] * len(ids)
        return ids, mask, types


def tokenizer() -> UnigramTokenizer:
    path = tokenizer_path()
    key = (str(path), path.stat().st_mtime if path.exists() else 0.0)
    cached = _TOKENIZER_CACHE.get(key)
    if cached is None:
        cached = UnigramTokenizer(path)
        _TOKENIZER_CACHE.clear()
        _TOKENIZER_CACHE[key] = cached
    return cached


def _session(cfg: Any) -> Any:
    onnx = onnx_path()
    if onnx is None:
        raise RuntimeError("missing reranker onnx")
    threads = settings(cfg)["threads"]
    key = (str(onnx), onnx.stat().st_mtime, threads)
    cached = _SESSION_CACHE.get(key)
    if cached is not None:
        return cached
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(onnx), sess_options=options, providers=["CPUExecutionProvider"]
    )
    _SESSION_CACHE.clear()
    _SESSION_CACHE[key] = session
    return session


def _logits_to_scores(logits: Any) -> list[float]:
    """兼容 (N,1) 取 logit、(N,2) 取 logit[1]-logit[0]，其余展平。"""
    import numpy as np

    array = np.asarray(logits, dtype=np.float32)
    if array.ndim == 1:
        return [float(value) for value in array]
    if array.ndim == 2 and array.shape[1] == 2:
        return [float(row[1] - row[0]) for row in array]
    return [float(value) for value in array.reshape(-1)]


def score(query: str, docs: list[str], cfg: Any) -> list[float]:
    """逐对打分；batch 一次前向。docs 为空返回 []。"""
    if not docs:
        return []
    if not tokenizer_path().is_file() or onnx_path() is None:
        return []  # 缺件静默：检索侧 ready() 已经是 false，这里再兜一层
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        return []

    settings_value = settings(cfg)
    tok = tokenizer()
    session = _session(cfg)
    encoded = [tok.pair(query, doc, max_len=settings_value["max_len"]) for doc in docs]
    max_len = max(len(ids) for ids, _mask, _types in encoded)
    batch = len(encoded)
    input_ids = np.full((batch, max_len), tok.pad_id, dtype=np.int64)
    attention_mask = np.zeros((batch, max_len), dtype=np.int64)
    token_type_ids = np.zeros((batch, max_len), dtype=np.int64)
    for index, (ids, mask, types) in enumerate(encoded):
        input_ids[index, : len(ids)] = ids
        attention_mask[index, : len(mask)] = mask
        token_type_ids[index, : len(types)] = types
    names = {item.name for item in session.get_inputs()}
    feeds: dict[str, Any] = {"input_ids": input_ids, "attention_mask": attention_mask}
    if "token_type_ids" in names:
        feeds["token_type_ids"] = token_type_ids
    if "position_ids" in names:
        feeds["position_ids"] = np.tile(np.arange(max_len, dtype=np.int64), (batch, 1))
    outputs = session.run(None, feeds)
    return _logits_to_scores(outputs[0])


def rerank(items: list[dict[str, Any]], query: str, cfg: Any) -> list[dict[str, Any]]:
    """返回按 rerank score 降序的新列表；打开时给条目加 rerank_score，不改结构。"""
    if not items:
        return []
    if not settings(cfg)["enabled"]:
        return [dict(item) for item in items]
    docs = [str(item.get("text") or "") for item in items]
    scores = score(query, docs, cfg)
    if len(scores) != len(items):  # 缺件/异常：原样返回，不丢条目
        return [dict(item) for item in items]
    result = []
    for item, value in zip(items, scores):
        copy = dict(item)
        copy["rerank_score"] = float(value)
        result.append(copy)
    result.sort(key=lambda item: -float(item.get("rerank_score") or 0.0))
    return result


def status(cfg: Any) -> dict[str, Any]:
    ok, why = ready(cfg)
    return {
        "ready": ok,
        "why": why,
        "model_dir": str(model_dir()),
        "onnx": onnx_path().name if onnx_path() else None,
        "enabled": settings(cfg)["enabled"],
    }
