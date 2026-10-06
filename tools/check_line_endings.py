#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 `.gitattributes` 校验（并可修复）工作区的换行符。

**为什么需要这个工具，而不是靠 git 自动处理**：

`.gitattributes` 写了 `*.bat text eol=crlf` / `*.ps1 text eol=crlf`，注释里也写明了理由 ——
"否则 cmd.exe 解析 label / 续行会出错（tools\\set_metadata.bat 曾因此失败）"。
但那句话只管**入库**：git 在 `add`/`checkout` 时才做规范化，
**任何直接改写工作区文件的工具都会绕过它**。

这个坑在 v0.2.2 又踩了一次：用 Python 的文本模式读 + `newline=""` 写，
把 `tools/set_metadata.bat` 从 CRLF 变成了 LF，于是发布构建在"元数据写入"这一步失败：

    'get' is not recognized as an internal or external command
    !! 发布构建失败：元数据写入 失败（退出码 1）

而且**排查时容易被误导**：`git cat-file blob HEAD:<路径>` 拿到的是**规范化后的 LF**，
与工作区的 CRLF 一比，会得出"本来就是 LF"的错误结论 —— 我第一次判断就错在这里。
所以判断依据必须是 `.gitattributes` 的规则，不是 git 的 blob。

用法：

    python tools/check_line_endings.py            # 只检查（门禁模式，违规则退出码 1）
    python tools/check_line_endings.py --fix      # 就地修复为规则要求的换行符
    python tools/check_line_endings.py --quiet    # 只打印汇总
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITATTRIBUTES = os.path.join(ROOT, ".gitattributes")

#: 只处理这些属性模式：`<glob> text eol=<crlf|lf>`
RULE = re.compile(r"^(?P<glob>\S+)\s+.*\beol=(?P<eol>crlf|lf)\b")


def read_rules() -> list[tuple[str, str]]:
    """从 `.gitattributes` 读出 (glob, 期望换行符) 列表。"""
    rules: list[tuple[str, str]] = []
    if not os.path.isfile(GITATTRIBUTES):
        return rules
    with io.open(GITATTRIBUTES, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = RULE.match(line)
            if match:
                rules.append((match.group("glob"), match.group("eol")))
    return rules


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
    return [item for item in out.stdout.decode("utf-8", errors="replace").split("\0") if item.strip()]


def expected_eol(path: str, rules: list[tuple[str, str]]) -> str | None:
    """该路径期望的换行符（没有规则则返回 None）。**后写的规则优先**，与 git 的语义一致。"""
    result = None
    for glob, eol in rules:
        if "/" in glob:
            hit = fnmatch.fnmatch(path, glob)
        else:
            hit = fnmatch.fnmatch(os.path.basename(path), glob)
        if hit:
            result = eol
    return result


def actual_eol(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    bare_lf = data.count(b"\n") - crlf
    if crlf and bare_lf:
        return "mixed"
    if crlf:
        return "crlf"
    return "lf"


def convert(data: bytes, target: str) -> bytes:
    """统一成目标换行符（先把 CRLF 归一为 LF，再按目标展开）。"""
    lf = data.replace(b"\r\n", b"\n")
    if target == "crlf":
        return lf.replace(b"\n", b"\r\n")
    return lf


def main() -> int:
    parser = argparse.ArgumentParser(description="按 .gitattributes 校验/修复换行符")
    parser.add_argument("--fix", action="store_true", help="就地修复")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = parser.parse_args()

    rules = read_rules()
    if not rules:
        print("!! .gitattributes 里没有找到任何 `text eol=` 规则，无事可做")
        return 1

    violations: list[tuple[str, str, str]] = []
    checked = 0
    for rel in tracked_files():
        want = expected_eol(rel.replace("\\", "/"), rules)
        if want is None:
            continue
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            data = handle.read()
        checked += 1
        got = actual_eol(data)
        if got != want:
            violations.append((rel.replace("\\", "/"), want, got))

    print("== 换行符门禁（依据 .gitattributes）==")
    print(f"规则 {len(rules)} 条；受检文件 {checked} 个")
    if violations:
        print(f"\n不符合规则的文件 {len(violations)} 个：")
        for rel, want, got in violations[:30]:
            print(f"  - {rel}: 期望 {want.upper()}，实际 {got.upper()}")
        if len(violations) > 30:
            print(f"  …… 另有 {len(violations) - 30} 个")
        if args.fix:
            fixed = 0
            for rel, want, _got in violations:
                path = os.path.join(ROOT, rel)
                with open(path, "rb") as handle:
                    data = handle.read()
                with open(path, "wb") as handle:
                    handle.write(convert(data, want))
                fixed += 1
            print(f"\n已修复 {fixed} 个文件（请重新运行构建/测试确认）")
            return 0
        print("\n修法：python tools/check_line_endings.py --fix")
        return 1
    if not args.quiet:
        print("全部符合。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
