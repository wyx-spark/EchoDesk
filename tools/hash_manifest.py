#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write a deterministic SHA-256 manifest of release artefacts and their trees.

[EN]
The reproducible-build verifier used to compute one ``Get-FileHash`` call per file from
PowerShell. On the four release trees that is roughly thirteen thousand process-level
round trips per run, and the verifier does it twice. Hashing itself is fast; the overhead
is in the per-call marshalling. This helper performs the same work with a thread pool and
stdlib ``hashlib`` while keeping the exact output format the verifier compares
(``<hex>  <relative-path>``, forward slashes, one line per entry).

The order is fixed by the command line: named files first, then each tree in the order it
was supplied with its entries sorted by relative path. Two invocations with the same
arguments therefore produce byte-identical manifests.

[ZH]
可复现构建验证器此前从 PowerShell 为每个文件调用一次 ``Get-FileHash``。四棵发布树上，
每次运行大约有一万三千次进程级往返，而验证器要跑两遍。哈希本身很快，耗时在逐次调用
的封送开销上。本工具用线程池与标准库 ``hashlib`` 完成同样的工作，同时保持验证器比较
的精确输出格式（``<十六进制>  <相对路径>``、正斜杠、每条目一行）。

顺序由命令行固定：先按给定顺序输出具名文件，再按给定顺序输出每棵树（树内按相对路径
排序）。相同参数两次调用必然产生逐字节一致的清单。

用法 / Usage:
    python tools/hash_manifest.py --out artifacts/repro-build/run-1.sha256 \
        --file publish/EchoDesk-v0.2.4-Setup.exe \
        --tree publish/EchoDesk-v0.2.4 --tree dist/EchoDesk-v0.2.4-linux-x64
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_files(rel_files: list[str], rel_trees: list[str]) -> list[tuple[str, str]]:
    """[EN] Return ``(display_path, absolute_path)`` in deterministic order.

    [ZH] 以确定顺序返回 ``(展示路径, 绝对路径)``。
    """
    entries: list[tuple[str, str]] = []
    for rel in rel_files:
        display = rel.replace("\\", "/")
        full = os.path.join(ROOT, rel)
        if not os.path.isfile(full):
            raise SystemExit(f"[EN] missing artefact: {display}\n[ZH] 缺少交付物：{display}")
        entries.append((display, full))
    for rel_tree in rel_trees:
        tree_rel = rel_tree.replace("\\", "/").rstrip("/")
        tree = os.path.join(ROOT, rel_tree)
        if not os.path.isdir(tree):
            raise SystemExit(f"[EN] missing release tree: {tree_rel}\n[ZH] 缺少发布目录：{tree_rel}")
        # [EN] Use the lexical path, not realpath: a symlinked workspace root would make
        # [EN] relpath() emit ``..`` segments and change the manifest between machines.
        # [ZH] 使用词法路径而不是 realpath：符号链接的工作区根会让 relpath() 产生 ``..``
        # [ZH] 片段，从而在不同机器上改变清单。
        base = os.path.abspath(tree)
        for dirpath, dirnames, filenames in os.walk(tree):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    # [EN] Hash the link text, not the target: a changed link is a change.
                    # [ZH] 哈希链接文本本身而不是目标：链接变化也是变化。
                    value = hashlib.sha256(os.readlink(full).encode("utf-8")).hexdigest()
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    entries.append((f"{tree_rel}/{rel}", value))
                    continue
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                entries.append((f"{tree_rel}/{rel}", full))
    return entries


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write a deterministic SHA-256 manifest")
    parser.add_argument("--out", required=True, help="[EN] output file / [ZH] 输出文件")
    parser.add_argument("--file", action="append", default=[],
                        help="[EN] artefact path relative to the repo root / [ZH] 相对仓库根的交付物路径")
    parser.add_argument("--tree", action="append", default=[],
                        help="[EN] release tree path relative to the repo root / [ZH] 相对仓库根的发布目录")
    parser.add_argument("--jobs", type=int, default=0,
                        help="[EN] hashing threads (default: CPU count, capped at 16) "
                             "/ [ZH] 哈希线程数（默认 CPU 数，上限 16）")
    args = parser.parse_args(argv)

    if not args.file and not args.tree:
        parser.error("[EN] at least one --file or --tree is required / [ZH] 至少需要一个 --file 或 --tree")

    entries = _collect_files(args.file, args.tree)
    started = time.perf_counter()
    jobs = args.jobs or min(16, (os.cpu_count() or 4))
    # [EN] Pre-computed symlink digests are already hex; only real paths are resolved here.
    # [ZH] 符号链接的摘要已是十六进制串；这里只解析真实路径。
    pending = [(display, path) for display, path in entries if os.path.isabs(path)]
    aliases = {display: path for display, path in entries if not os.path.isabs(path)}
    results: dict[str, str] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for display, digest in zip(
                (display for display, _path in pending),
                pool.map(lambda item: sha256_of(item[1]), pending),
            ):
                results[display] = digest
    results.update(aliases)

    lines = [f"{results[display]}  {display}" for display, _path in entries]
    out_path = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    elapsed = time.perf_counter() - started
    print(f"[hash] {len(lines)} entries in {elapsed:.1f}s -> {os.path.relpath(out_path, ROOT)}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
