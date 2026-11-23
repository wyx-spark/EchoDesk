#!/usr/bin/env python3
# [EN] Release-package document check: the shipped docs must describe the package that actually exists.
# [ZH] 发布物文档核对：随包文档必须描述"包里真实存在的东西"。
#
# 为什么需要它（2026-11-23 三连击）：
#   1. README/FAQ/USER_GUIDE 让用户双击 `Start-EchoDesk-v0.2.7.lnk`、跑 `install-complete.bat`，
#      而便携包内两者都不存在（真实入口是 `启动 EchoDesk.bat` / `start.sh`）；
#   2. 目录树声称有 `app\backend\EchoDeskBackend.exe`（便携包没有）；
#   3. README 把 `app\EchoDesk.exe` 标成 "WPF frontend"（包内是 Avalonia）。
#   昨天的对账是"文档 vs 代码"，用户拿到的是"包"——代码对不等于包里对。
#
# 检查项（对每个发布树）：
#   A. 入口存在：win 必须有 `启动 EchoDesk.bat`；linux/osx 必须有 `start.sh`（按内容自动识别）
#   B. 文档不得提及便携包内不存在的入口：`install-complete.bat`、`Start-EchoDesk-*`
#   C. 文档不得声称存在缺席的后端可执行文件：`EchoDeskBackend.exe`（仅当树里确实没有时）
#   D. 前端技术栈一致：若树内有 Avalonia 程序集且无 WPF 核心程序集，
#      文档不得把界面主程序描述成 WPF frontend / WPF 前端
#
# 用法：
#   python tools/check_release_docs.py --tree publish\EchoDesk-v0.2.7
#   python tools/check_release_docs.py --package dist\EchoDesk-v0.2.7-win-x64.zip   (会解到临时目录)
#   退出码：0 通过；1 失败（打印具体文件与命中行）；2 用法错误。

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile

DOC_SUFFIXES = (".md",)


def iter_doc_files(tree: str):
    """产出 (相对路径, 文本)。只读随包文档，不读二进制。"""
    for root, _dirs, files in os.walk(tree):
        for name in files:
            if not name.lower().endswith(DOC_SUFFIXES):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, tree).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    yield rel, fh.read()
            except OSError:
                continue


def iter_archive_docs(package: str):
    """从 zip / tar.gz 里直接读文档，不解整包。"""
    if package.lower().endswith(".zip"):
        with zipfile.ZipFile(package) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(DOC_SUFFIXES):
                    continue
                yield info.filename, zf.read(info).decode("utf-8", "replace")
        return
    with tarfile.open(package, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile() or not member.name.lower().endswith(DOC_SUFFIXES):
                continue
            data = tf.extractfile(member)
            if data is None:
                continue
            yield member.name, data.read().decode("utf-8", "replace")


def archive_members(package: str) -> list[str]:
    if package.lower().endswith(".zip"):
        with zipfile.ZipFile(package) as zf:
            return [i.filename for i in zf.infolist() if not i.is_dir()]
    with tarfile.open(package, "r:*") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def tree_members(tree: str) -> list[str]:
    out = []
    for root, _dirs, files in os.walk(tree):
        for name in files:
            out.append(os.path.relpath(os.path.join(root, name), tree).replace("\\", "/"))
    return out


def detect_platform(members: list[str]) -> str:
    base = {m.split("/")[1] for m in members if "/" in m}
    if "启动 EchoDesk.bat" in base or any(m.lower().endswith(".exe") for m in base):
        return "windows"
    if "start.sh" in base:
        return "posix"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="release-package document check")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--tree", help="已解包的发布树目录")
    src.add_argument("--package", help="zip 或 tar.gz 发布包")
    ap.add_argument("--expect-entry", default="", help="强制指定期望入口文件名（默认按平台判断）")
    args = ap.parse_args()

    tmpdir = None
    try:
        if args.tree:
            tree = args.tree
            members = tree_members(tree)
            docs = list(iter_doc_files(tree))
        else:
            package = args.package
            if not os.path.exists(package):
                print(f"FATAL: package not found: {package}")
                return 2
            members = archive_members(package)
            docs = list(iter_archive_docs(package))

        if not members:
            print("FATAL: empty package/tree")
            return 2

        # 顶层目录名（包内通常有一层 <name>/）
        tops = {m.split("/")[0] for m in members if "/" in m}
        prefix = (sorted(tops)[0] + "/") if len(tops) == 1 else ""
        base_names = {m[len(prefix):] for m in members if m.startswith(prefix)}

        platform = detect_platform(members)
        if args.expect_entry:
            expected = args.expect_entry
        elif platform == "windows":
            expected = "启动 EchoDesk.bat"
        elif platform == "posix":
            expected = "start.sh"
        else:
            expected = ""

        problems: list[str] = []

        # A. 入口存在
        if not expected:
            problems.append("无法识别平台（既没有 启动 EchoDesk.bat 也没有 start.sh）")
        elif expected not in base_names:
            problems.append(f"缺少入口: {expected}（包内根目录实际只有: {', '.join(sorted(list(base_names))[:8])}）")

        # 索引文档标题便于定位
        has_av = any(re.search(r"(^|/)Avalonia\.[^/]*\.dll$", m) for m in members)
        has_wpf = any(re.search(r"(^|/)(PresentationFramework|PresentationCore)\.dll$", m) for m in members)
        has_backend_exe = any(m.endswith("/app/backend/EchoDeskBackend.exe") for m in members)

        for rel, text in docs:
            # B. 便携包内不存在的入口
            for needle in ("install-complete.bat", "Start-EchoDesk-"):
                if needle in text:
                    line = next((l for l in text.splitlines() if needle in l), "")
                    problems.append(f"{rel}: 提到包内不存在的入口 `{needle}` → {line.strip()[:120]}")
            # C. 缺席的后端可执行文件（只看"用户向"文档里对包内路径的直接断言）
            #    排除：开发者文档（描述构建产物）、含 dist\ 或 PyInstaller 的构建说明行。
            dev_doc = "DEVELOPER.md" in rel
            if (not has_backend_exe) and (not dev_doc):
                for raw in text.splitlines():
                    line = raw.strip()
                    if not line:
                        continue
                    if ("app\\backend\\EchoDeskBackend.exe" in line) or ("app/backend/EchoDeskBackend.exe" in line):
                        if ("dist\\" in line) or ("dist/" in line) or ("PyInstaller" in line) or ("pyinstaller" in line):
                            continue
                        problems.append(f"{rel}: 声称包内有 app\\backend\\EchoDeskBackend.exe（实际没有）→ {line[:120]}")
                        break
            # D. 前端技术栈：只抓"目录表里把 app 主程序标成 WPF"这种直接断言
            #    DEVELOPER.md 描述的是构建产物与两套前端的架构，不属于"随包文档断言"，跳过。
            if has_av and not has_wpf and ("DEVELOPER.md" not in rel):
                for raw in text.splitlines():
                    line = raw.strip()
                    if ("app\\EchoDesk.exe" in line or "app/EchoDesk.exe" in line) and ("WPF" in line):
                        problems.append(f"{rel}: 把 app 主程序标成 WPF，但包内是 Avalonia（无 WPF 核心程序集）→ {line[:120]}")
                        break

        if problems:
            print(f"[FAIL] 文档与包内实际不一致：{len(problems)} 处")
            for p in problems:
                print("  - " + p)
            return 1

        print(f"[OK] 文档与包内一致（入口 {expected or 'n/a'}，文档 {len(docs)} 篇，成员 {len(members)} 个，platform={platform}）")
        return 0
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
