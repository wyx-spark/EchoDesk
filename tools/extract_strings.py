# -*- coding: utf-8 -*-
"""待翻译文本提取器（任务书 任务三 3.5）。

它做两件事：

1. **盘点**：扫描源码里还没收编进语言包的面向用户文本（默认判据：字符串字面量
   含中日韩字符，且不在 ``Loc.T(...)`` / ``t(...)`` / ``api.lang(...)`` 调用里，
   也不是注释、日志格式串或正则），输出成报告。这是判断"多语言覆盖率"的依据，
   也是给下一版排期的清单。
2. **生成模板**：``--scaffold <locale>`` 为新语言生成语言包骨架（结构与源语言
   一致，值为空、注释里带源文案），供社区翻译。

用法
----
    python tools/extract_strings.py                      # 输出待收编清单
    python tools/extract_strings.py --json report.json    # 同时写出 JSON 报告
    python tools/extract_strings.py --scaffold ja         # 生成日语语言包模板
    python tools/extract_strings.py --path frontend       # 只扫描某个目录

退出码始终为 0（它是盘点工具，不是门禁；门禁是 tools/check_lang.py）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_LOCALE = "zh-Hans"

#: 含中日韩字符的字符串字面量（Python 的三引号/单双引号 + C# 的 @"..." / "..."）
_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

#: 已经收编的写法：这些调用里的字符串不算"待收编"
_WRAPPED = re.compile(
    r"(?:\bLoc\.T\(|\bLoc\.Has\(|\bt\(|_t\(|api\.lang\(|Loc\.Key=|Loc\.TipKey=|Loc\.HelpKey=|"
    r"tr\(|message=|content=)"
)

#: 明确排除的模式：日志、正则、路径、异常包装、非用户可见
_SKIP_LINE = re.compile(
    r"(log\.|logging\.|Logger|App\.Log|Debug\.|Trace\.|Console\.|"
    r"new Regex|Regex\.|re\.compile|"
    r"//|#|/\*|\*|"
    r"\.exe|\.dll|\.gguf|\.json|\.log|\.md|\.ps1|\.bat|"
    r"throw new|catch \(|Name = |Tag = |AutomationProperties)"
)

#: 逐文件扫描的目标。默认只扫随发布物发给用户的代码（后端 + 前端）；
#: tools/ 与 examples/ 是研发脚本与示例插件，不属于界面文案，用 --path 显式指定才扫。
_TARGETS = ("backend", "frontend")
_SKIP_DIRS = {"bin", "obj", "__pycache__", ".venv", "node_modules", ".git", "lang"}


def _iter_files(base):
    for current, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith((".py", ".cs", ".xaml", ".ps1")):
                yield os.path.join(current, name)


def _strip_comments(text, suffix):
    """把注释与 Python 文档字符串替换成等长空白，避免把说明文字当成待翻译文本。

    只做"等长替换"（保留换行位置），因此行号仍然准确。
    """
    chars = list(text)
    n = len(chars)

    def blank(start, end):
        for i in range(start, min(end, n)):
            if chars[i] != "\n":
                chars[i] = " "

    if suffix in (".py",):
        i = 0
        while i < n:
            ch = chars[i]
            if ch == "#":
                end = text.find("\n", i)
                blank(i, n if end < 0 else end)
                i = n if end < 0 else end
                continue
            if text.startswith('"""', i) or text.startswith("'''", i):
                quote = text[i:i + 3]
                end = text.find(quote, i + 3)
                end = n if end < 0 else end + 3
                blank(i, end)
                i = end
                continue
            i += 1
    else:
        i = 0
        while i < n:
            if text.startswith("//", i):
                end = text.find("\n", i)
                blank(i, n if end < 0 else end)
                i = n if end < 0 else end
                continue
            if text.startswith("/*", i):
                end = text.find("*/", i + 2)
                end = n if end < 0 else end + 2
                blank(i, end)
                i = end
                continue
            i += 1
    return "".join(chars)


def _literals(text, suffix):
    """抽出字符串字面量及其行号（够用的启发式：不解析语法树）。"""
    out = []
    if suffix in (".py", ".ps1"):
        pattern = re.compile(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'|"([^"\n]*)"|\'([^\'\n]*)\'', re.S)
    else:
        pattern = re.compile(r'@"((?:[^"]|"")*)"|"([^"\n]*)"', re.S)
    for match in pattern.finditer(text):
        value = next((g for g in match.groups() if g is not None), "")
        if not value:
            continue
        line = text.count("\n", 0, match.start()) + 1
        # 上下文窗口向后多取一些：XAML 里 Loc.Key 写在 Text 属性的后面一行
        out.append((line, value, text[max(0, match.start() - 80):match.end() + 220]))
    return out


def collect(paths):
    """返回 [(相对路径, 行号, 文本)]，已按文件与行号排序。"""
    found = []
    for base_name in paths:
        base = os.path.join(ROOT, base_name) if not os.path.isabs(base_name) else base_name
        if not os.path.exists(base):
            continue
        for path in _iter_files(base):
            try:
                with io.open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except OSError:
                continue
            suffix = os.path.splitext(path)[1]
            text = _strip_comments(raw, suffix)
            rel = os.path.relpath(path, ROOT)
            for line, value, context in _literals(text, suffix):
                if not _CJK.search(value):
                    continue
                one_line = value.strip().replace("\n", " ")
                if len(one_line) < 2:
                    continue
                if _WRAPPED.search(context):
                    continue
                if _SKIP_LINE.search(context.split("\n")[0]):
                    continue
                found.append((rel, line, one_line[:160]))
    return sorted(set(found))


def report(items, quiet):
    by_file = {}
    for rel, line, text in items:
        by_file.setdefault(rel, []).append((line, text))
    if not quiet:
        for rel in sorted(by_file):
            print(f"\n{rel}  （{len(by_file[rel])} 条）")
            for line, text in by_file[rel][:40]:
                print(f"  {line:>5}  {text}")
            if len(by_file[rel]) > 40:
                print(f"         …（还有 {len(by_file[rel]) - 40} 条）")
    print()
    print(f"共 {len(items)} 条尚未收编进语言包的文本，分布在 {len(by_file)} 个文件。")
    print("提示：把用户可见的文本搬到语言包并用 Loc.T / t(\"键名\") 取用；")
    print("      日志、异常包装、内部标识符可以保留原文（它们不需要翻译）。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="EchoDesk 待翻译文本提取器")
    parser.add_argument("--path", action="append", help="只扫描指定目录（可重复）")
    parser.add_argument("--json", metavar="FILE", help="把报告写成 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出统计")
    parser.add_argument("--scaffold", metavar="LOCALE", help="为新语言生成语言包模板")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.scaffold:
        # 复用 check_lang 的脚手架逻辑，保证两处生成的模板完全一致
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import check_lang

        packs = {}
        for directory in (check_lang.BACKEND_LANG, check_lang.FRONTEND_LANG):
            packs.update(check_lang.load_dir(directory))
        return check_lang.scaffold(args.scaffold, packs)

    items = collect(args.path or list(_TARGETS))
    if args.json:
        with io.open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump([{"file": a, "line": b, "text": c} for a, b, c in items],
                      f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"已写入报告：{args.json}")
    return report(items, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
