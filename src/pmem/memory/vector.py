"""向量通道（M13 / 0.13.0）：词法答不上来时，用语义相似度做第二手段。

**要解决的问题**：词法层只能撞词。问「昨晚看的那个关于时间循环的动画」，
记忆里写的是「看了石头门」——一个字都不重合，双字、trigram、覆盖率全落空，
``raw_fallback`` 回到原话里捞也是同样的词法检索，于是六条「根本不在词法池里」的
记忆永远捞不上来（90 天语料实测 16 条漏答里占 6 条）。

**做法**：本地 ``bge-small-zh-v1.5``（4 层 / hidden 512 / ONNX int8，24 MB）把
记忆与查询都编码成 512 维单位向量，余弦相似度暴力检索。**不建索引、不调 API**：
几百到几万条记忆的矩阵乘在 CPU 上是毫秒级，比任何近似索引都简单可靠。

四条约束（对齐 PLAN §1）：

- **只在没答案时跑**：``retrieval.vector.trigger_coverage`` 之上不动手。词法有把握时
  加一层语义检索只会多花时间、还可能用「语义上像但事实不对」的条目挤掉对的。
- **只补候选，不重排**：和关联图同一个立场。改排序会同时牵动所有题，收益无法归因。
- **派生可重放**：向量只由 ``memory.text_raw`` 决定，``rebuild()`` 整表重算，
  换模型/换量化版本删表重来即可，不存原文、不改原文。
- **缺件即降级**：没装 onnxruntime、没下载模型 → 所有函数都是空操作，
  词法链路完全不受影响（``ready()`` 里报原因，``pmem vector status`` 能看见）。

模型文件不随仓库分发（119 MB / 24 MB），放在 ``~/.dsh/pmem/models/`` 下，
用 ``scripts/fetch_model.py`` 下载并按 sha256 校验。
"""

from __future__ import annotations

import json
import os
import threading
import unicodedata
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ..base.clock import now_iso

MODEL_ID = "bge-small-zh-v1.5"
# 默认放用户目录：模型文件大、且不该进仓库（用户自己下的，仓库只带代码）。
DEFAULT_DIR = "~/.dsh/pmem/models/bge-small-zh-v1.5"
DEFAULT_ONNX = "onnx/model_int8.onnx"
DEFAULT_DIM = 512
# 记忆与查询都很短；512 只是模型上限，截到 256 省一半注意力成本。
DEFAULT_MAX_TOKENS = 256
# BGE 官方给「短查询检索长段落」推荐的指令前缀；记忆一侧不加。
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

_encoders: dict[tuple[str, float, int], "Encoder"] = {}
_locks = threading.Lock()

# 可被检索的状态（和 store.VISIBLE_STATUS 一致）：superseded/hidden/deleted 的记忆
# 不能进向量池，否则更新题会答成旧说法。
VISIBLE_STATUS = ("active", "contested", "dormant")
_VISIBLE_SQL = "m.status IN (" + ",".join("'" + s + "'" for s in VISIBLE_STATUS) + ")"

_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


# ---------------------------------------------------------------- 配置


def settings(cfg: Any) -> dict[str, Any]:
    """取 ``retrieval.vector`` 那一格；配置缺失时返回空 dict（= 关闭）。"""
    value = cfg.get("retrieval.vector", {}) if cfg is not None else {}
    return value if isinstance(value, dict) else {}


def model_dir(cfg: Any) -> Path:
    raw = settings(cfg).get("model_dir") or DEFAULT_DIR
    return Path(os.path.expanduser(str(raw)))


def onnx_path(cfg: Any) -> Path:
    name = settings(cfg).get("onnx_file") or DEFAULT_ONNX
    path = Path(str(name))
    return path if path.is_absolute() else model_dir(cfg) / path


def model_id(cfg: Any) -> str:
    return str(settings(cfg).get("model") or MODEL_ID)


def dim_of(cfg: Any) -> int:
    return int(settings(cfg).get("dim") or DEFAULT_DIM)


def ready(cfg: Any) -> tuple[bool, str]:
    """能不能用：模型文件在不在、onnxruntime 装没装。返回 ``(ok, 原因)``。"""
    base = model_dir(cfg)
    tok = base / "tokenizer.json"
    onnx = onnx_path(cfg)
    if not tok.is_file():
        return False, f"缺 tokenizer.json（{base}）"
    if not onnx.is_file():
        return False, f"缺 {onnx.name}（{base}）"
    try:  # 只在真要跑的时候才 import：没装 onnxruntime 也不该影响词法链路
        import onnxruntime  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"onnxruntime 不可用：{exc}"
    return True, "ok"


# ---------------------------------------------------------------- 分词
# 纯 Python 实现 HF ``BertTokenizer`` 的三段：normalize → pre_tokenize → wordpiece。
# 为什么不用 ``tokenizers``/``transformers``：两个包各几十 MB，为了一个分词器
# 装进来违背「核心零依赖」；而 BERT 这套规则是固定的，实现一次就够。


def _is_cjk(cp: int) -> bool:
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF
        or 0x2A700 <= cp <= 0x2B73F
        or 0x2B740 <= cp <= 0x2B81F
        or 0x2B820 <= cp <= 0x2CEAF
        or 0xF900 <= cp <= 0xFAFF
        or 0x2F800 <= cp <= 0x2FA1F
    )


def _is_whitespace(ch: str) -> bool:
    cp = ord(ch)
    if cp in (0x20, 0x09, 0x0A, 0x0D):
        return True
    if 0x2000 <= cp <= 0x200A:
        return True
    return cp in (0x1680, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000) or ch.isspace()


def _is_control(ch: str) -> bool:
    cp = ord(ch)
    if cp in (0x09, 0x0A, 0x0D):
        return False
    return unicodedata.category(ch) in ("Cc", "Cf") or cp == 0xFFFD


class Tokenizer:
    """``tokenizer.json``（WordPiece）的最小实现。"""

    def __init__(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        model = data.get("model") or {}
        self.vocab: dict[str, int] = dict(model.get("vocab") or {})
        self.unk = str(model.get("unk_token") or "[UNK]")
        self.prefix = str(model.get("continuing_subword_prefix") or "##")
        self.max_chars = int(model.get("max_input_chars_per_word") or 100)
        norm = data.get("normalizer") or {}
        self.lowercase = bool(norm.get("lowercase"))
        self.strip_accents = bool(norm.get("strip_accents"))
        self.chinese_chars = bool(norm.get("handle_chinese_chars"))
        self.clean_text = bool(norm.get("clean_text", True))
        self.cls_id = int(self.vocab.get("[CLS]", 101))
        self.sep_id = int(self.vocab.get("[SEP]", 102))
        self.pad_id = int(self.vocab.get("[PAD]", 0))
        self.unk_id = int(self.vocab.get(self.unk, 100))

    # -- normalize ------------------------------------------------------
    def normalize(self, text: str) -> str:
        if self.clean_text:
            kept: list[str] = []
            for ch in text:
                if _is_whitespace(ch):
                    kept.append(" ")
                elif not _is_control(ch):
                    kept.append(ch)
            text = "".join(kept)
        if self.chinese_chars:
            out: list[str] = []
            for ch in text:
                if _is_cjk(ord(ch)):
                    out.append(" ")
                    out.append(ch)
                    out.append(" ")
                else:
                    out.append(ch)
            text = "".join(out)
        if self.strip_accents:
            decomposed = unicodedata.normalize("NFD", text)
            text = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
        if self.lowercase:
            text = text.lower()
        return " ".join(text.split())

    def _is_punct(self, ch: str) -> bool:
        return ch in _PUNCT or unicodedata.category(ch).startswith("P")

    def pre_tokenize(self, text: str) -> list[str]:
        """BertPreTokenizer：空白分开后，标点各自成词，其余连续保留。"""
        out: list[str] = []
        for chunk in text.split():
            buf: list[str] = []
            for ch in chunk:
                if self._is_punct(ch):
                    if buf:
                        out.append("".join(buf))
                        buf = []
                    out.append(ch)
                else:
                    buf.append(ch)
            if buf:
                out.append("".join(buf))
        return out

    def wordpiece(self, word: str) -> list[str]:
        if len(word) > self.max_chars:
            return [self.unk]
        pieces: list[str] = []
        start = 0
        n = len(word)
        while start < n:
            end = n
            found: str | None = None
            while start < end:
                piece = word[start:end]
                if start > 0:
                    piece = self.prefix + piece
                if piece in self.vocab:
                    found = piece
                    break
                end -= 1
            if found is None:
                return [self.unk]
            pieces.append(found)
            start = end
        return pieces

    def encode(self, text: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> list[int]:
        ids = [self.cls_id]
        for word in self.pre_tokenize(self.normalize(text or "")):
            for piece in self.wordpiece(word):
                ids.append(int(self.vocab.get(piece, self.unk_id)))
        ids.append(self.sep_id)
        limit = max(2, int(max_tokens))
        if len(ids) > limit:  # 保留头尾特殊符号：截断只砍正文
            ids = ids[: limit - 1] + [self.sep_id]
        return ids


# ---------------------------------------------------------------- 编码器


class Encoder:
    """ONNX 会话 + 分词器：一次装好，进程内复用。

    池化用 CLS（BGE 官方口径）+ L2 归一化，所以余弦相似度就是点积，
    检索时是一整块矩阵乘，不需要逐条算距离。
    """

    def __init__(self, onnx: str | Path, tokenizer: Tokenizer, *, dim: int, threads: int = 1) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(
            str(onnx), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.tok = tokenizer
        self.dim = int(dim)
        self.input_names = {i.name for i in self.sess.get_inputs()}
        self.output_name = self.sess.get_outputs()[0].name

    def embed(
        self,
        texts: Sequence[str],
        *,
        batch: int = 16,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        prefix: bool = False,
    ) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        chunks: list[np.ndarray] = []
        size = max(1, int(batch))
        for start in range(0, len(texts), size):
            part = list(texts[start : start + size])
            seqs = [
                self.tok.encode((QUERY_PREFIX + text) if prefix else text, max_tokens)
                for text in part
            ]
            width = max(len(ids) for ids in seqs)
            ids = np.full((len(seqs), width), self.tok.pad_id, dtype=np.int64)
            mask = np.zeros((len(seqs), width), dtype=np.int64)
            for row, seq in enumerate(seqs):
                ids[row, : len(seq)] = seq
                mask[row, : len(seq)] = 1
            feeds: dict[str, np.ndarray] = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.sess.run([self.output_name], feeds)[0]
            vec = np.asarray(hidden[:, 0, :], dtype=np.float32)  # CLS pooling
            norm = np.linalg.norm(vec, axis=1, keepdims=True)
            norm[norm == 0] = 1.0
            chunks.append(vec / norm)
        return np.concatenate(chunks, axis=0).astype(np.float32)


def encoder(cfg: Any) -> Encoder:
    """按（模型路径, mtime, 线程数）缓存会话：加载一次约 100–200 ms。"""
    onnx = onnx_path(cfg)
    tok_path = model_dir(cfg) / "tokenizer.json"
    threads = int(settings(cfg).get("threads") or 0)
    if threads <= 0:  # 0 = 自动：短查询上更多线程更快，但别把核全占了（daemon 也在跑）
        threads = min(4, os.cpu_count() or 1)
    key = (str(onnx), onnx.stat().st_mtime, threads)
    with _locks:
        hit = _encoders.get(key)
        if hit is not None:
            return hit
        enc = Encoder(onnx, Tokenizer(tok_path), dim=dim_of(cfg), threads=threads)
        if len(_encoders) > 2:
            _encoders.clear()
        _encoders[key] = enc
        return enc


def embed(texts: Sequence[str], cfg: Any, *, prefix: bool = False) -> np.ndarray | None:
    """对外入口：缺件时返回 None（调用方无需 try/except）。"""
    if not texts:
        return np.zeros((0, dim_of(cfg)), dtype=np.float32)
    ok, _reason = ready(cfg)
    if not ok:
        return None
    try:
        return encoder(cfg).embed(
            texts,
            batch=int(settings(cfg).get("batch") or 16),
            max_tokens=int(settings(cfg).get("max_tokens") or DEFAULT_MAX_TOKENS),
            prefix=prefix,
        )
    except Exception:  # noqa: BLE001 模型坏/版本不匹配 → 当作没有这条通道
        return None


# ---------------------------------------------------------------- 存储
# 复用 M1 就建好的 ``memory_vec_plain``（可移植回退表）：换模型只是 model_id 不同，
# 删旧行重算即可，不需要改 schema。

_MAX_CACHE = 4
_matrix_cache: dict[tuple[Any, ...], tuple[np.ndarray, np.ndarray]] = {}


def _pack(vec: np.ndarray) -> bytes:
    """存 fp16：向量已 L2 归一化，fp16 的余弦误差 < 1e-3（实测 0.99997），
    存储直接减半（512 维从 2 KB 到 1 KB），对 308 题的名次没有任何影响。"""
    return np.asarray(vec, dtype=np.float16).tobytes()


def _unpack(blob: bytes, dim: int) -> np.ndarray:
    """兼容 fp32 老行：按 blob 长度判断，不靠版本号。"""
    raw = np.frombuffer(blob, dtype=np.uint8)
    if raw.size == dim * 2:
        arr = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
    elif raw.size == dim * 4:
        arr = np.frombuffer(blob, dtype=np.float32)
    else:
        raise ValueError(f"向量长度不符：{raw.size} 字节 / dim={dim}")
    return arr


def put(conn: Any, seq: int, vec: np.ndarray, cfg: Any) -> None:
    conn.execute(
        "INSERT INTO memory_vec_plain(memory_seq, model_id, dim, vec, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(memory_seq) DO UPDATE SET "
        "model_id=excluded.model_id, dim=excluded.dim, vec=excluded.vec, "
        "updated_at=excluded.updated_at",
        (int(seq), model_id(cfg), int(vec.size), _pack(vec), now_iso()),
    )


def invalidate(db: Any = None) -> None:
    with _locks:
        if db is None:
            _matrix_cache.clear()
        else:
            for key in [k for k in _matrix_cache if k[0] == id(db)]:
                _matrix_cache.pop(key, None)


def ensure(db: Any, cfg: Any, *, limit: int = 4000) -> dict[str, Any]:
    """补齐缺向量的记忆（换模型/新装/新增都能靠它收敛）。"""
    out: dict[str, Any] = {"enabled": bool(settings(cfg).get("enabled")), "model": model_id(cfg)}
    if not out["enabled"]:
        out["skipped"] = "disabled"
        return out
    ok, reason = ready(cfg)
    if not ok:
        out.update(ok=False, reason=reason)
        return out
    conn = db.conn
    model = model_id(cfg)
    stale = conn.execute(
        "SELECT COUNT(*) c FROM memory_vec_plain WHERE model_id<>?", (model,)
    ).fetchone()
    if int(stale["c"] or 0):
        with db.tx():
            conn.execute("DELETE FROM memory_vec_plain WHERE model_id<>?", (model,))
        out["dropped_other_model"] = int(stale["c"] or 0)
    rows = conn.execute(
        "SELECT m.seq, m.text_raw FROM memory m "
        "WHERE m.status<>'deleted' AND NOT EXISTS ("
        "  SELECT 1 FROM memory_vec_plain v WHERE v.memory_seq=m.seq AND v.model_id=?"
        ") ORDER BY m.seq LIMIT ?",
        (model, max(1, int(limit))),
    ).fetchall()
    if not rows:
        out.update(ok=True, built=0, pending=0)
        return out
    texts = [str(r["text_raw"] or "") for r in rows]
    mat = embed(texts, cfg)
    if mat is None:
        out.update(ok=False, reason="embed failed", built=0)
        return out
    dim = dim_of(cfg)
    if mat.shape[1] != dim:
        out.update(ok=False, reason=f"dim mismatch {mat.shape[1]} != {dim}", built=0)
        return out
    with db.tx():
        for row, vec in zip(rows, mat):
            put(conn, int(row["seq"]), vec, cfg)
    invalidate(db)
    out.update(ok=True, built=len(rows))
    return out


def ensure_once(db: Any, cfg: Any, *, limit: int | None = None) -> dict[str, Any]:
    """进程内只补一次：第一次检索（或 daemon 第一拍）把缺的向量补齐。

    为什么不放在 ``PMem.__init__``：打开库（CLI 每条命令、每次测试）都要等模型加载，
    不划算；而检索/后台拍子是「真的要用向量」的时机。没缺的行时这里只多一条
    ``NOT EXISTS`` 点查，几十微秒。
    """
    conf = settings(cfg)
    if not conf.get("enabled"):
        return {"skipped": "disabled"}
    if getattr(db, "_pmem_vec_checked", False):
        return {"skipped": "checked"}
    try:
        db._pmem_vec_checked = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 冻结对象也无所谓，下一次再查一遍而已
        pass
    return ensure(db, cfg, limit=int(limit or conf.get("backfill") or 4000))


def rebuild(db: Any, cfg: Any, *, batch: int = 2000) -> dict[str, Any]:
    """整表重算（换模型/换量化版本后调用）。"""
    ok, reason = ready(cfg)
    if not ok:
        return {"ok": False, "reason": reason}
    with db.tx():
        db.conn.execute("DELETE FROM memory_vec_plain")
    invalidate(db)
    total = 0
    while True:
        part = ensure(db, cfg, limit=batch)
        built = int(part.get("built") or 0)
        total += built
        if built < batch:
            break
    return {"ok": True, "built": total}


def _matrix(db: Any, cfg: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """可见记忆的（向量矩阵, seq 数组）。按「模型 + 行数 + 最大 seq + 更新时间」缓存。"""
    model = model_id(cfg)
    conn = db.conn
    stamp = conn.execute(
        "SELECT COUNT(*) c, COALESCE(MAX(v.memory_seq),0) hi, COALESCE(MAX(v.updated_at),'') ts "
        "FROM memory_vec_plain v JOIN memory m ON m.seq=v.memory_seq "
        "WHERE v.model_id=? AND " + _VISIBLE_SQL,
        (model,),
    ).fetchone()
    count = int(stamp["c"] or 0)
    if count <= 0:
        return None
    key = (id(db), model, count, int(stamp["hi"] or 0), str(stamp["ts"] or ""))
    with _locks:
        hit = _matrix_cache.get(key)
    if hit is not None:
        return hit
    rows = conn.execute(
        "SELECT v.memory_seq AS seq, v.dim AS dim, v.vec AS vec FROM memory_vec_plain v "
        "JOIN memory m ON m.seq=v.memory_seq WHERE v.model_id=? AND " + _VISIBLE_SQL + " "
        "ORDER BY v.memory_seq",
        (model,),
    ).fetchall()
    dim = dim_of(cfg)
    seqs = np.fromiter((int(r["seq"]) for r in rows), dtype=np.int64, count=len(rows))
    mat = np.empty((len(rows), dim), dtype=np.float32)
    keep = 0
    for row in rows:
        try:
            mat[keep] = _unpack(row["vec"], int(row["dim"] or dim))
        except ValueError:
            continue
        keep += 1
    mat = mat[:keep]
    seqs = seqs[:keep]
    with _locks:
        if len(_matrix_cache) > _MAX_CACHE:
            _matrix_cache.clear()
        _matrix_cache[key] = (mat, seqs)
    return mat, seqs


def search(
    db: Any,
    cfg: Any,
    query: str,
    *,
    limit: int = 8,
    min_score: float | None = None,
    prefix: bool | None = None,
) -> dict[int, float]:
    """余弦暴力检索：返回 ``{memory_seq: 相似度}``（降序前的原始分，已 ≥ min_score）。"""
    conf = settings(cfg)
    if not conf.get("enabled"):
        return {}
    pair = _matrix(db, cfg)
    if pair is None:
        return {}
    mat, seqs = pair
    use_prefix = bool(conf.get("query_prefix", False)) if prefix is None else bool(prefix)
    vec = embed([query], cfg, prefix=use_prefix)
    if vec is None or vec.size == 0:
        return {}
    sims = mat @ vec[0]
    floor = float(min_score if min_score is not None else (conf.get("min_score") or 0.45))
    take = min(len(sims), max(1, int(limit) * 3))
    top = np.argpartition(-sims, take - 1)[:take] if take < len(sims) else np.arange(len(sims))
    top = top[np.argsort(-sims[top])]
    out: dict[int, float] = {}
    for idx in top:
        score = float(sims[idx])
        if score < floor:
            break
        out[int(seqs[idx])] = round(score, 4)
        if len(out) >= max(1, int(limit)):
            break
    return out


def embed_new(db: Any, seq: int, text: str, cfg: Any) -> bool:
    """写入一条记忆后顺手补它的向量（失败了无所谓，``ensure`` 会补）。"""
    if not settings(cfg).get("enabled"):
        return False
    vec = embed([text], cfg)
    if vec is None or vec.size == 0:
        return False
    try:
        with db.tx() as conn:
            put(conn, seq, vec[0], cfg)
    except Exception:  # noqa: BLE001 向量是增强能力，写不进不能挡住记忆写入
        return False
    invalidate(db)
    return True


def stats(db: Any, cfg: Any) -> dict[str, Any]:
    model = model_id(cfg)
    row = db.conn.execute(
        "SELECT COUNT(*) c FROM memory_vec_plain WHERE model_id=?", (model,)
    ).fetchone()
    total = db.conn.execute("SELECT COUNT(*) c FROM memory WHERE status<>'deleted'").fetchone()
    ok, reason = ready(cfg)
    return {
        "enabled": bool(settings(cfg).get("enabled")),
        "ready": ok,
        "reason": reason,
        "model": model,
        "dim": dim_of(cfg),
        "dir": str(model_dir(cfg)),
        "onnx": onnx_path(cfg).name,
        "vectors": int(row["c"] or 0),
        "memories": int(total["c"] or 0),
        "min_score": float(settings(cfg).get("min_score") or 0.45),
        "trigger_coverage": float(settings(cfg).get("trigger_coverage") or 0.0),
    }
