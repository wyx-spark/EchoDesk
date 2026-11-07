#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Remove build-time and cross-platform redundancies from a release tree.

[EN]
Release trees are assembled from Python wheels and PyInstaller analysis output. Both carry
material that is useful to developers but never loaded at run time:

* test suites, demo packages and ``__pycache__`` directories;
* Windows import libraries (``.lib``), static archives (``.a``), debug symbols (``.pdb``);
* C extensions that belong to a different operating system (for example ``.so`` files in
  a Windows tree, or ``.pyd`` files in a Linux tree);
* Playwright's bundled developer tooling (trace viewer, recorder, dashboard, HTML report
  assets and TypeScript declarations), which the automation feature never opens.

[ZH]
发布树由 Python 轮子与 PyInstaller 分析结果组装而成，两者都携带对开发者有用、但运行期
永不加载的材料：

* 测试套件、演示包与 ``__pycache__`` 目录；
* Windows 导入库（``.lib``）、静态归档（``.a``）、调试符号（``.pdb``）；
* 属于其它操作系统的 C 扩展（例如 Windows 树中的 ``.so``，或 Linux 树中的 ``.pyd``）；
* Playwright 随包的开发者工具（跟踪查看器、录制器、仪表盘、HTML 报告资源与 TypeScript
  声明），自动化功能从不打开这些内容。

[EN] The pruning policy is explicit and platform-aware; it never removes an extension that
the target platform can load.
[ZH] 清理策略是显式且感知平台的；绝不删除目标平台能够加载的扩展。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

import reproducible as repro

#: [EN] Directory names that contain only tests or bytecode caches.
#: [ZH] 仅包含测试或字节码缓存的目录名。
TEST_DIR_NAMES = {"test", "tests", "testing", "demos", "__pycache__"}

#: [EN] Developer-only subtrees inside third-party packages.
#: [ZH] 第三方包内仅开发者使用的子树。
DEV_ONLY_SUBTREES = (
    "playwright/driver/package/types",
    "playwright/driver/package/lib/vite",
)

#: [EN] Extensions that are never needed at run time on any platform.
#: [ZH] 任何平台运行期都不需要的扩展名。
COMMON_JUNK_SUFFIXES = (".pyc", ".pyo", ".pdb", ".lib", ".exp", ".a")

#: [EN] Extensions that belong to a different platform than the target.
#: [ZH] 与目标平台不符的扩展名。
FOREIGN_SUFFIXES = {
    "win-x64": (".so", ".dylib"),
    "linux-x64": (".pyd", ".dylib"),
    "osx-arm64": (".pyd",),
}


def _dir_size(tree: str) -> tuple[int, int]:
    total = files = 0
    for base, _dirs, names in os.walk(tree):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(base, name))
                files += 1
            except OSError:
                pass
    return total, files


def prune_tree(tree: str, rid: str) -> dict[str, object]:
    """[EN] Prune one release tree in place and return a machine-readable summary.

    [ZH] 原地清理一个发布树，并返回机器可读的汇总。
    """
    if rid not in FOREIGN_SUFFIXES:
        raise SystemExit(
            f"[EN] Unknown RID: {rid}\n[ZH] 未知 RID：{rid}"
        )
    before_bytes, before_files = _dir_size(tree)
    removed: list[dict[str, object]] = []
    foreign = FOREIGN_SUFFIXES[rid]

    # [EN] Remove known developer-only subtrees first, then walk the remaining tree. The
    # [EN] match is suffix-based because the subtrees sit under different package roots
    # [EN] (for example ``_internal/playwright/...`` in a PyInstaller tree).
    # [ZH] 先移除已知的仅开发者子树，再遍历剩余目录树。匹配采用后缀形式，因为这些子树位于
    # [ZH] 不同的包根下（例如 PyInstaller 树中的 ``_internal/playwright/...``）。
    for rel in DEV_ONLY_SUBTREES:
        suffix = "/" + rel.replace("\\", "/")
        for base, dirs, _files in os.walk(tree, topdown=True):
            for name in list(dirs):
                candidate = os.path.join(base, name)
                candidate_rel = "/" + os.path.relpath(candidate, tree).replace(os.sep, "/")
                if not candidate_rel.endswith(suffix):
                    continue
                size, count = _dir_size(candidate)
                shutil.rmtree(candidate, ignore_errors=True)
                dirs.remove(name)
                removed.append({"kind": "developer-tooling",
                                "path": candidate_rel.lstrip("/"),
                                "bytes": size, "files": count})

    for base, dirs, files in os.walk(tree, topdown=False):
        for name in list(dirs):
            if name not in TEST_DIR_NAMES:
                continue
            target = os.path.join(base, name)
            size, count = _dir_size(target)
            shutil.rmtree(target, ignore_errors=True)
            removed.append({"kind": "tests-or-cache",
                            "path": os.path.relpath(target, tree).replace(os.sep, "/"),
                            "bytes": size, "files": count})
        for name in files:
            full = os.path.join(base, name)
            lowered = name.lower()
            if lowered == "base_library.zip":
                # [EN] PyInstaller stamps this archive with the wall clock; rewrite only the
                # [EN] zip metadata so the payload and entry order stay untouched.
                # [ZH] PyInstaller 会为该归档写入墙上时钟；仅重写 zip 元数据，保持载荷与
                # [ZH] 条目顺序不变。
                count = repro.rewrite_zip_timestamps(full)
                removed.append({"kind": "normalized-zip-metadata",
                                "path": os.path.relpath(full, tree).replace(os.sep, "/"),
                                "bytes": 0, "files": count})
            if lowered.endswith(COMMON_JUNK_SUFFIXES):
                kind = "debug-or-build"
            elif lowered.endswith(foreign):
                kind = "foreign-extension"
            else:
                continue
            try:
                size = os.path.getsize(full)
                os.remove(full)
                removed.append({"kind": kind,
                                "path": os.path.relpath(full, tree).replace(os.sep, "/"),
                                "bytes": size, "files": 1})
            except OSError:
                pass

    after_bytes, after_files = _dir_size(tree)
    return {
        "tree": tree,
        "rid": rid,
        "before_bytes": before_bytes,
        "before_files": before_files,
        "after_bytes": after_bytes,
        "after_files": after_files,
        "removed_bytes": before_bytes - after_bytes,
        "removed_files": before_files - after_files,
        "removed": removed,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Prune a release tree")
    parser.add_argument("--tree", required=True)
    parser.add_argument("--rid", required=True, choices=sorted(FOREIGN_SUFFIXES))
    parser.add_argument("--json", default="", help="[EN] optional summary path / [ZH] 可选汇总路径")
    args = parser.parse_args(argv)
    summary = prune_tree(args.tree, args.rid)
    print(f"[prune] [EN] {args.rid}: {summary['before_bytes']} -> {summary['after_bytes']} bytes, "
          f"removed {summary['removed_files']} files / [ZH] {args.rid}："
          f"{summary['before_bytes']} -> {summary['after_bytes']} 字节，移除 {summary['removed_files']} 个文件",
          flush=True)
    if args.json:
        import json

        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
