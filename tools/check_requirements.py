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


def _guards_import_error(handlers: list[ast.ExceptHandler]) -> bool:
    """[EN] Judge whether a ``try`` block degrades gracefully when an import is missing.
    [ZH] 判断某个 ``try`` 块在导入缺失时是否会优雅降级。

    [EN] A handler qualifies when it is bare or catches ``ImportError`` / ``ModuleNotFoundError``
    [EN] / ``Exception`` / ``BaseException``. Anything narrower (for example ``ValueError``)
    [ZH] cannot turn a missing optional dependency into a clean fallback, so it is not treated as
    [ZH] a guard.
    [ZH] 无类型或捕获 ``ImportError`` / ``ModuleNotFoundError`` / ``Exception`` / ``BaseException``
    [ZH] 的处理器才算数；更窄的（如 ``ValueError``）无法把缺失的可选依赖变成优雅降级，不算保护。
    """
    for handler in handlers:
        if handler.type is None:
            return True
        names = set()
        candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for candidate in candidates:
            if isinstance(candidate, ast.Name):
                names.add(candidate.id)
            elif isinstance(candidate, ast.Attribute):
                names.add(candidate.attr)
        if names & {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}:
            return True
    return False


def imported_top_level() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """返回 ({顶层模块名: {出现位置}}, {可选导入名: {出现位置}})。

    [EN] Imports are classified as optional when they execute inside a ``try`` block whose
    [EN] handlers tolerate ``ImportError`` (or broader). The guard must be in the same dynamic
    [EN] scope: entering a function/class/lambda resets the state, because a ``try`` around a
    [EN] ``def`` does not guard the ``import`` inside its body. This keeps the gate honest:
    [EN] an unconditionally imported module is still reported as undeclared, while documented
    [EN] optional integrations (OCR through ``pytesseract``/``Pillow``) are listed separately.
    [ZH] 若导入位于一个能容忍 ``ImportError``（或更宽）的 ``try`` 块内，则判定为可选导入。
    [ZH] 保护必须处于同一动态作用域：进入函数/类/lambda 会重置状态，因为包住 ``def`` 的
    [ZH] ``try`` 并不保护其函数体里的 ``import``。这样门禁仍然严格：无保护地导入的模块
    [ZH] 依旧报「未声明」，而文档化的可选集成（经 ``pytesseract``/``Pillow`` 的 OCR）
    [ZH] 单独列出。
    """
    found: dict[str, set[str]] = {}
    optional: dict[str, set[str]] = {}

    def visit(node: ast.AST, rel: str, guarded: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            for child in ast.iter_child_nodes(node):
                visit(child, rel, False)
            return
        if isinstance(node, ast.Try):
            inner = _guards_import_error(node.handlers)
            for child in node.body:
                visit(child, rel, guarded or inner)
            for child in node.handlers + node.orelse + node.finalbody:
                visit(child, rel, guarded)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                target = optional if guarded else found
                target.setdefault(name, set()).add(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            if not node.level and node.module:
                name = node.module.split(".")[0]
                target = optional if guarded else found
                target.setdefault(name, set()).add(f"{rel}:{node.lineno}")
        for child in ast.iter_child_nodes(node):
            visit(child, rel, guarded)

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
            visit(tree, rel, False)
    return found, optional


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

    imports, optional = imported_top_level()
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

    optional_used = [(m, sorted(optional[m])) for m in sorted(optional)
                     if m not in STDLIB and m not in own and not m.startswith("_")]
    if optional_used:
        print("\n可选集成（位于捕获 ImportError 的 try 块内，缺失时按设计降级，不要求声明）:")
        for mod, places in optional_used:
            declared_mark = "已声明" if mod in import_names else "可缺省"
            print(f"  [{declared_mark}] {mod:<20} {places[0]}")

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
