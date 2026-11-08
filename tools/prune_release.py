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

[EN] v0.2.5 adds three size rules that were found by auditing the real trees:

* prefix-layout data inside ``site-packages`` (``lib64/`` on Linux, ``lib/`` on macOS)
  installed by wheels that carry ``.data/data`` entries. ``pip install --target`` copies
  them next to the package, but nothing in the application ever looks there; llama.cpp
  loads its shared objects from ``llama_cpp/lib`` instead;
* duplicate shared-library aliases inside one directory. The llama.cpp wheels ship the
  unversioned name (what ``ctypes`` opens) and the ``SONAME`` name (what the loader
  resolves) plus a full-version copy that no ``DT_NEEDED`` / ``LC_LOAD_DYLIB`` entry
  references. Only byte-identical duplicates are removed;
* language packs other than English and Chinese. Both frontends and the backend ship
  exactly ``en`` and ``zh-Hans``; other locales are dead weight in the release archive.

[ZH] v0.2.5 新增三条依据实测目录树制定的体积规则：

* ``site-packages`` 中属于「前缀布局」的数据目录（Linux ``lib64/``、macOS ``lib/``）。
  它们的来源是轮子的 ``.data/data`` 条目，``pip install --target`` 会把它们铺在包旁边，
  但程序里没有任何代码路径引用；llama.cpp 实际从 ``llama_cpp/lib`` 加载共享库；
* 同一目录内重复的共享库别名。llama.cpp 轮子同时提供无版本名（``ctypes`` 打开的）、
  ``SONAME`` 名（加载器解析依赖时用的）与一个完整版本副本，而没有任何
  ``DT_NEEDED`` / ``LC_LOAD_DYLIB`` 引用完整版本。只删除内容逐字节相同的重复项；
* 英文与中文之外的语言包。两个前端与后端都只随包 ``en`` 与 ``zh-Hans``；其它语言在
  发布归档里是纯体积。

[EN] The pruning policy is explicit and platform-aware; it never removes an extension that
the target platform can load.
[ZH] 清理策略是显式且感知平台的；绝不删除目标平台能够加载的扩展。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
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
    # ``.exe`` on a POSIX tree is a pip console-script shim generated on the Windows build
    # machine; the target platform can neither execute it nor import it.
    "win-x64": (".so", ".dylib"),
    "linux-x64": (".pyd", ".dylib", ".exe"),
    "osx-arm64": (".pyd", ".exe"),
}

#: [EN] Prefix-layout library directories created by wheels with ``.data/data`` entries.
#: [ZH] 由带 ``.data/data`` 条目的轮子生成的「前缀布局」库目录。
PREFIX_DATA_DIRS = {
    "linux-x64": ("lib64",),
    "osx-arm64": ("lib",),
}

#: [EN] Languages actually shipped by both frontends and the backend.
#: [ZH] 两个前端与后端实际随包的语言。
SHIPPED_LOCALES = {"en", "zh-hans", "zh-hant"}

#: [EN] Shared-library alias patterns whose full-version copy is never referenced.
#: [ZH] 完整版本副本永不被引用的共享库别名模式。
_FULL_VERSION_SO = re.compile(r"^(?P<base>lib[^/]+?\.so)\.(?P<major>\d+)\.\d+\.\d+$")
_FULL_VERSION_DYLIB = re.compile(r"^(?P<base>lib[^/]+?)\.(?P<major>\d+)\.\d+\.\d+\.dylib$")


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


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_prefix_libs(path: str) -> bool:
    """[EN] Is this directory prefix-layout library data rather than an importable package?

    [ZH] 这个目录是「前缀布局」的库数据，而不是可导入的包吗？

    The test is deliberately conservative: any Python source makes it a package we must
    keep, and at least one shared object is required before removing anything.
    """
    has_library = False
    for base, _dirs, names in os.walk(path):
        for name in names:
            if name.endswith((".py", ".pyi", ".pyx")):
                return False
            if name.endswith((".so", ".dylib")):
                has_library = True
    return has_library


def _prune_prefix_data(tree: str, rid: str, removed: list[dict[str, object]]) -> None:
    """[EN] Remove wheel ``.data/data`` libraries that ``pip --target`` misplaced.

    [ZH] 删除被 ``pip --target`` 铺错位置的轮子 ``.data/data`` 库。

    ``llama_cpp_python`` wheels carry a ``.data/data/lib64`` (Linux) or ``.data/data/lib``
    (macOS) payload, which ``pip install --target`` materialises **inside**
    ``site-packages``. Nothing on the target platform resolves that location: the package
    itself ships the same libraries under ``llama_cpp/lib/``, and that is the path
    ``llama_cpp.py`` passes to ``ctypes``.
    """
    names = PREFIX_DATA_DIRS.get(rid, ())
    if not names:
        return
    for base, dirs, _files in os.walk(tree, topdown=False):
        if os.path.basename(base) != "site-packages":
            continue
        for name in list(dirs):
            if name not in names:
                continue
            target = os.path.join(base, name)
            if not os.path.isdir(target) or not _looks_like_prefix_libs(target):
                continue
            size, count = _dir_size(target)
            shutil.rmtree(target, ignore_errors=True)
            removed.append({
                "kind": "prefix-data-layout",
                "path": os.path.relpath(target, tree).replace(os.sep, "/"),
                "bytes": size,
                "files": count,
            })


def _prune_duplicate_libs(tree: str, removed: list[dict[str, object]]) -> None:
    """[EN] Remove full-version shared-library aliases that nothing references.

    [ZH] 删除没有任何引用的「完整版本」共享库别名。

    ``libllama.so.0`` is both the unversioned name's ``SONAME`` and the name every
    ``DT_NEEDED`` entry uses; ``libllama.so.0.5.0`` is a third, byte-identical copy. On
    macOS the equivalent is ``libllama.0.dylib`` (install name) versus
    ``libllama.0.0.1.dylib``. Only a byte-identical sibling is ever removed.
    """
    for base, _dirs, files in os.walk(tree):
        for name in files:
            match = _FULL_VERSION_SO.match(name) or _FULL_VERSION_DYLIB.match(name)
            if not match:
                continue
            if name.endswith(".dylib"):
                canonical = f"{match.group('base')}.{match.group('major')}.dylib"
            else:
                canonical = f"{match.group('base')}.{match.group('major')}"
            full = os.path.join(base, name)
            keep = os.path.join(base, canonical)
            if not (os.path.isfile(full) and os.path.isfile(keep)):
                continue
            try:
                if os.path.getsize(full) != os.path.getsize(keep):
                    continue
                if _sha256(full) != _sha256(keep):
                    continue
                size = os.path.getsize(full)
                os.remove(full)
            except OSError:
                continue
            removed.append({
                "kind": "duplicate-library-alias",
                "path": os.path.relpath(full, tree).replace(os.sep, "/"),
                "bytes": size,
                "files": 1,
            })


_LOCALE_FILE = re.compile(
    r"^(?P<locale>[A-Za-z]{2}(?:[-_][A-Za-z]{2,8})?)(?:\.[A-Za-z0-9_-]+)?\.json$"
)


def _prune_unshipped_locales(tree: str, removed: list[dict[str, object]]) -> None:
    """[EN] Keep only English and Chinese packs inside ``lang`` directories.

    [ZH] 在 ``lang`` 目录内只保留英文与中文语言包。

    The rule is scoped to directories literally named ``lang`` so that unrelated JSON
    (configuration, caches, skill metadata) is never touched, and it only recognises
    two-letter locale prefixes (``ja.json``, ``pt-BR.json``) — so ``app.json`` is not
    mistaken for a language.
    """
    for base, _dirs, files in os.walk(tree):
        if os.path.basename(base).lower() != "lang":
            continue
        for name in files:
            match = _LOCALE_FILE.match(name)
            if not match:
                continue
            if match.group("locale").lower() in SHIPPED_LOCALES:
                continue
            full = os.path.join(base, name)
            try:
                size = os.path.getsize(full)
                os.remove(full)
            except OSError:
                continue
            removed.append({
                "kind": "unshipped-locale",
                "path": os.path.relpath(full, tree).replace(os.sep, "/"),
                "bytes": size,
                "files": 1,
            })


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

    # [EN] v0.2.5 size rules: misplaced wheel data, dead library aliases, unshipped locales.
    # [ZH] v0.2.5 体积规则：铺错位置的轮子数据、无引用的库别名、未随包的语言。
    _prune_prefix_data(tree, rid, removed)
    _prune_duplicate_libs(tree, removed)
    _prune_unshipped_locales(tree, removed)

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
