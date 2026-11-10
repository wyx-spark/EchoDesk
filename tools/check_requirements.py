"""发布前依赖自检：用 AST 取出后端自身代码的顶层导入，与 requirements.txt 对账。

背景（实测发现）：`backend/marketplace.py` 导入 `requests`，但 `backend/requirements.txt`
里没有它。开发机与打包机上都装了 requests（作为其它包的传递依赖），所以问题被掩盖；
在"只装 requirements.txt 的干净环境"里，市场功能会 ImportError。
任务书「任务五」要求用干净 venv 收口，本脚本就是那个收口的自动化部分。

用法：python tools/check_requirements.py [--venv <python.exe>]
      给出 --venv 时，会真的用该解释器逐个 import 顶层依赖，确认可导入。
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
REQUIREMENTS = os.path.join(BACKEND, "requirements.txt")

# 后端自身的代码目录：只扫描随包分发的模块，跳过开发期目录
SKIP_DIRS = {
    "__pycache__", "_internal", ".venv", "venv", ".idea", "models",
    "skills", "skills_dev", "lang", ".cache", "build", "dist",
}

# 标准库与随包模块（backend 内部模块名，不是第三方依赖）
STDLIB = set(sys.stdlib_module_names)


def own_modules() -> set[str]:
    out = set()
    for name in os.listdir(BACKEND):
        path = os.path.join(BACKEND, name)
        if name.endswith(".py"):
            out.add(name[:-3])
        elif os.path.isdir(path) and name not in SKIP_DIRS:
            if os.path.isfile(os.path.join(path, "__init__.py")):
                out.add(name)
    return out


def imported_top_level() -> dict[str, set[str]]:
    """返回 {顶层模块名: {出现位置}}，只取后端自身代码的绝对导入。"""
    found: dict[str, set[str]] = {}
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, ROOT)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
            except (SyntaxError, UnicodeDecodeError, OSError) as exc:
                print(f"  (跳过无法解析的文件 {rel}: {exc})")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.setdefault(alias.name.split(".")[0], set()).add(
                            f"{rel}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level:      # 相对导入属于包内模块
                        continue
                    if node.module:
                        found.setdefault(node.module.split(".")[0], set()).add(
                            f"{rel}:{node.lineno}")
    return found


def declared() -> set[str]:
    """requirements.txt 里声明的包名（规范化成 import 名）。

    先按 ` @ ` 切掉**直接 URL**（macOS 上 llama-cpp-python 是按 URL 钉死的，
    见 requirements.txt 的说明）；不切的话这一行会被当成一个叫
    "llama-cpp-python @ https://…" 的包名，于是 `llama_cpp` 被判成"未声明"。
    """
    names = set()
    with open(REQUIREMENTS, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            line = re.split(r"\s@\s", line, maxsplit=1)[0].strip()
            name = re.split(r"[<>=!\[;]", line, maxsplit=1)[0].strip()
            if name:
                names.add(name)
    return names


# 发行包名 → import 名 的少数例外（其余按 PEP 503 把 - 换成 _）
DIST_TO_IMPORT = {
    "python-docx": "docx",
    "python-pptx": "pptx",
    "duckduckgo-search": "duckduckgo_search",
    "pywin32": "win32api",          # 顶层无 win32 包，实际是 win32api 等
    "llama-cpp-python": "llama_cpp",
    "pyinstaller": "PyInstaller",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", default="", help="用该解释器实测能否导入")
    args = parser.parse_args()

    imports = imported_top_level()
    decl = declared()
    own = own_modules()

    import_names = {DIST_TO_IMPORT.get(d, d.replace("-", "_")) for d in decl}
    # pywin32 提供多个顶层模块，逐个列出
    import_names.discard("win32api")
    import_names |= {"win32api", "win32com", "win32con", "win32file", "win32gui",
                     "win32clipboard", "pythoncom", "pywintypes"}

    third_party = []
    for mod in sorted(imports):
        if mod in STDLIB or mod in own or mod.startswith("_"):
            continue
        third_party.append(mod)

    missing = [m for m in third_party if m not in import_names]
    print(f"后端自身代码的第三方顶层导入: {len(third_party)} 个")
    for mod in third_party:
        places = sorted(imports[mod])
        mark = "OK " if mod in import_names else "缺失"
        print(f"  [{mark}] {mod:<22} {places[0]}" + (f"  (+{len(places) - 1} 处)" if len(places) > 1 else ""))

    print()
    if missing:
        print(f"!! {len(missing)} 个导入未在 backend/requirements.txt 中声明: {missing}")
        print("   干净环境（只装 requirements.txt）下这些模块会 ImportError。")
    else:
        print("requirements.txt 覆盖了后端全部第三方顶层导入。")

    if args.venv:
        print(f"\n用 {args.venv} 实测导入:")
        failed = []
        for mod in third_party:
            code = f"import {mod}"
            proc = subprocess.run([args.venv, "-c", code], capture_output=True)
            ok = proc.returncode == 0
            print(f"  [{'OK ' if ok else 'FAIL'}] import {mod}")
            if not ok:
                failed.append((mod, proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]))
        if failed:
            print(f"\n!! 实测失败 {len(failed)} 个: {[f[0] for f in failed]}")
            return 1

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
