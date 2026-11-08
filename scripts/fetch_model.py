"""下载向量通道（M13）要用的本地模型：bge-small-zh-v1.5（ONNX）+ 分词器。

    python scripts/fetch_model.py                 # 默认 int8 + fp32 两档都下
    python scripts/fetch_model.py --onnx int8     # 只要 int8（24 MB，默认线上用这个）
    python scripts/fetch_model.py --mirror https://hf-mirror.com

为什么模型不进仓库：119 MB 的权重不该跟着代码走。下载后按 HuggingFace 的 LFS
sha256 校验，放 ``~/.dsh/pmem/models/bge-small-zh-v1.5/``；缺件时向量通道整条
静默降级（``pmem vector status`` 会说明缺什么），词法链路不受影响。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "Xenova/bge-small-zh-v1.5"
ROOT = Path(os.path.expanduser("~")) / ".dsh" / "pmem" / "models" / "bge-small-zh-v1.5"

# 重排器（task-49）：bge-reranker-base 的 ONNX 交叉编码器。
# 文件名以 HF tree 实测为准（2026-11-08）：onnx/model_quantized.onnx 279 MB 默认；
# int8/fp32 留在 choices 里方便对比，不把大文件塞进仓库。
RERANK_REPO = "Xenova/bge-reranker-base"
RERANK_ROOT = Path(os.path.expanduser("~")) / ".dsh" / "pmem" / "models" / "bge-reranker-base"
RERANK_TOKENIZER_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
]
RERANK_ONNX_FILES = {
    "quantized": "onnx/model_quantized.onnx",
    "int8": "onnx/model_int8.onnx",
    "fp32": "onnx/model.onnx",
}
RERANK_BASE = f"https://huggingface.co/{RERANK_REPO}/resolve/main"
TOKENIZER_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
]
ONNX_FILES = {
    "int8": "onnx/model_int8.onnx",  # 24 MB，默认（实测和 fp32 在 308 题上命中相同）
    "fp32": "onnx/model.onnx",       # 95 MB，保底/对比用
}
BASE = f"https://huggingface.co/{REPO}/resolve/main"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def tree(base: str, repo: str = REPO) -> dict[str, dict]:
    url = f"{base}/api/models/{repo}/tree/main?recursive=1"
    with urllib.request.urlopen(url, timeout=60) as resp:
        entries = json.loads(resp.read().decode())
    return {e["path"]: e for e in entries if e.get("type") == "file"}


def fetch(rel: str, meta: dict, base: str, root: Path = ROOT, repo: str = REPO) -> None:
    dest = root / rel.replace("/", os.sep)
    dest.parent.mkdir(parents=True, exist_ok=True)
    want_sha = (meta.get("lfs") or {}).get("oid") or ""
    size = meta.get("size") or 0
    if dest.is_file() and size and dest.stat().st_size == size:
        if not want_sha or sha256_file(dest) == want_sha:
            print(f"skip   {rel} ({size} bytes, sha ok)")
            return
    url = f"{base}/{repo}/resolve/main/{rel}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"get    {rel} ({size} bytes)")
    req = urllib.request.Request(url, headers={"User-Agent": "pmem-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as out:
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            out.write(block)
    got = sha256_file(tmp)
    if want_sha and got != want_sha:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"sha256 mismatch for {rel}: {got} != {want_sha}")
    tmp.replace(dest)
    print(f"ok     {rel} sha256={got}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 PMem 本地 ONNX 模型")
    parser.add_argument("--model", choices=["embed", "rerank"], default="embed", help="下哪个模型")
    parser.add_argument("--mirror", default="https://huggingface.co", help="HF 镜像地址")
    parser.add_argument(
        "--onnx",
        choices=["int8", "fp32", "both", "quantized"],
        default=None,
        help="embed: int8/fp32/both；rerank: quantized/int8/fp32；缺省按模型",
    )
    args = parser.parse_args()
    base = args.mirror.rstrip("/")
    if args.model == "rerank":
        repo, root, tokenizer_files, onnx_files = (
            RERANK_REPO,
            RERANK_ROOT,
            RERANK_TOKENIZER_FILES,
            RERANK_ONNX_FILES,
        )
        choice = args.onnx or "quantized"
        if choice == "both":
            weights = ["int8", "fp32"]
        else:
            weights = [choice]
        if any(key not in onnx_files for key in weights):
            raise SystemExit(f"rerank 不支持 --onnx {choice}（可选 {', '.join(onnx_files)}）")
    else:
        repo, root, tokenizer_files, onnx_files = REPO, ROOT, TOKENIZER_FILES, ONNX_FILES
        choice = args.onnx or "both"
        if choice == "quantized":
            raise SystemExit("embed 不支持 --onnx quantized")
        weights = ["int8"] if choice == "int8" else ["fp32"] if choice == "fp32" else ["int8", "fp32"]
    want = tokenizer_files + [onnx_files[key] for key in weights]
    root.mkdir(parents=True, exist_ok=True)
    entries = tree(base, repo=repo)
    missing = [rel for rel in want if rel not in entries]
    if missing:
        raise SystemExit(f"repo missing files: {missing}")
    for rel in want:
        fetch(rel, entries[rel], base, root=root, repo=repo)
    total = sum((root / rel.replace("/", os.sep)).stat().st_size for rel in want)
    print(f"root={root} files={len(want)} total={total / 1e6:.1f} MB")
    if args.model == "embed":
        print("验证：python -m pmem.gate.cli vector status")
    else:
        print("验证：from pmem.memory.retrieval import rerank; print(rerank.ready(Config()))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
