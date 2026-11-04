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


def tree(base: str) -> dict[str, dict]:
    url = f"{base}/api/models/{REPO}/tree/main?recursive=1"
    with urllib.request.urlopen(url, timeout=60) as resp:
        entries = json.loads(resp.read().decode())
    return {e["path"]: e for e in entries if e.get("type") == "file"}


def fetch(rel: str, meta: dict, base: str) -> None:
    dest = ROOT / rel.replace("/", os.sep)
    dest.parent.mkdir(parents=True, exist_ok=True)
    want_sha = (meta.get("lfs") or {}).get("oid") or ""
    size = meta.get("size") or 0
    if dest.is_file() and size and dest.stat().st_size == size:
        if not want_sha or sha256_file(dest) == want_sha:
            print(f"skip   {rel} ({size} bytes, sha ok)")
            return
    url = f"{base}/{rel}"
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
    parser = argparse.ArgumentParser(description="下载 bge-small-zh-v1.5 ONNX")
    parser.add_argument("--mirror", default="https://huggingface.co", help="HF 镜像地址")
    parser.add_argument(
        "--onnx", choices=["int8", "fp32", "both"], default="both", help="下哪一档权重"
    )
    args = parser.parse_args()
    base = args.mirror.rstrip("/")
    weights = (
        ["int8"] if args.onnx == "int8" else ["fp32"] if args.onnx == "fp32" else ["int8", "fp32"]
    )
    want = TOKENIZER_FILES + [ONNX_FILES[key] for key in weights]
    ROOT.mkdir(parents=True, exist_ok=True)
    entries = tree(base)
    missing = [rel for rel in want if rel not in entries]
    if missing:
        raise SystemExit(f"repo missing files: {missing}")
    for rel in want:
        fetch(rel, entries[rel], base)
    total = sum((ROOT / rel.replace("/", os.sep)).stat().st_size for rel in want)
    print(f"root={ROOT} files={len(want)} total={total / 1e6:.1f} MB")
    print("验证：python -m pmem.gate.cli vector status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
