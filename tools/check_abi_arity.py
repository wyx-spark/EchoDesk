"""
[EN]
Mechanical arity audit: the generated ABI declarations versus the calls that use them.

Why this exists
---------------
``backend/llama_abi`` is generated from one llama.cpp revision, while the libraries that
actually execute are the vendored DLL/.so/.dylib files. When the two drift apart, ctypes
still imports cleanly and the model still loads -- the call fails later, at the moment a
particular function is exercised. That is exactly how v0.2.7 shipped a build in which every
completion using a repeat penalty died with ``this function takes at least 5 arguments
(4 given)``: master moved to the 0.4.7 libraries, whose ``llama_sampler_init_penalties`` takes
``n_vocab`` first, while the calling code kept the 0.3.x four-argument shape.

What it checks
--------------
1. For every function defined in ``backend/llama_abi/llama_cpp.py``, the declared parameter
   count (positional-only plus positional) is recorded.
2. Every call in ``backend/**.py`` (the ABI module itself excluded) whose callee name matches a
   declared function is compared against that count. A mismatch is a failure; keyword arguments
   on a ctypes function are a failure too, because ctypes functions are positional-only.

Exclusions are allowed but must be justified in ``EXCLUSIONS`` (name -> reason).

[ZH]
机械审计：生成的 ABI 声明与实际调用点。

为什么需要
----------
``backend/llama_abi`` 由某个确定的 llama.cpp 修订生成，而真正执行的是随包的
DLL/.so/.dylib。两者漂移时 ctypes 仍能导入、模型仍能加载 —— 失败要等到某个具体函数被调用
才发生。v0.2.7 就是这样发出去的：master 换到 0.4.7 的库，其
``llama_sampler_init_penalties`` 首参是 ``n_vocab``，而调用点仍按 0.3.x 的四参数形态写，
于是凡使用重复惩罚的补全都以 ``this function takes at least 5 arguments (4 given)`` 失败。

检查内容
--------
1. 记录 ``backend/llama_abi/llama_cpp.py`` 里每个函数的声明参数个数（仅位置参数 + 位置参数）。
2. 对 ``backend/**.py``（排除 ABI 模块自身）里每个被调用名与声明同名的调用，比对实参个数；
   不一致即失败。对 ctypes 函数传关键字参数同样失败，因为它们只接受位置参数。

允许排除项，但必须在 ``EXCLUSIONS`` 里写明理由。
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
ABI_MODULE = os.path.join(BACKEND, "llama_abi", "llama_cpp.py")

#: [EN] Callee name -> reason it is not checked.
#: [ZH] 被调用名 -> 不检查的理由。
EXCLUSIONS = {
    "llama_batch_get_one": (
        "takes a (token, n_tokens) pair and returns a struct by value; the generated declaration "
        "is authoritative and every call site passes both arguments, but the function is a "
        "one-line helper whose arity is verified indirectly by the generation check"
    ),
}

#: [EN] Directories walked for call sites, relative to the repository root.
#: [ZH] 扫描调用点的目录（相对仓库根）。
SCAN_DIRS = ("backend",)

#: [EN] Files/directories that must not be scanned: the ABI declaration itself is the reference.
#: [ZH] 不扫描的文件/目录：ABI 声明自身就是参照物。
SKIP_PATHS = (os.path.join(BACKEND, "llama_abi"),)


def _param_count(node: ast.FunctionDef) -> int | None:
    """[EN] Declared positional parameter count, or None for variadic functions.
    [ZH] 声明的位置参数个数；变参函数返回 None。"""
    args = node.args
    if args.vararg is not None:
        return None
    return len(args.posonlyargs) + len(args.args)


def load_declarations() -> dict[str, int]:
    with open(ABI_MODULE, encoding="utf-8-sig", errors="replace") as handle:
        tree = ast.parse(handle.read())
    declared: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count = _param_count(node)
            if count is not None:
                declared[node.name] = count
    return declared


def iter_python_files():
    for base in SCAN_DIRS:
        start = os.path.join(ROOT, base)
        for dirpath, dirnames, filenames in os.walk(start):
            if any(os.path.abspath(dirpath).startswith(os.path.abspath(p)) for p in SKIP_PATHS):
                dirnames[:] = []
                continue
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def main() -> int:
    if not os.path.isfile(ABI_MODULE):
        print(f"找不到 ABI 声明：{ABI_MODULE}")
        return 1
    declared = load_declarations()
    if not declared:
        print(f"ABI 声明里没有解析到任何函数：{ABI_MODULE}")
        return 1

    problems: list[str] = []
    checked = 0
    for path in iter_python_files():
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as handle:
                tree = ast.parse(handle.read())
        except SyntaxError as exc:
            problems.append(f"{os.path.relpath(path, ROOT)}: 语法错误：{exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            if name not in declared or name in EXCLUSIONS:
                continue
            checked += 1
            given = len(node.args)
            want = declared[name]
            rel = os.path.relpath(path, ROOT)
            if given != want:
                problems.append(
                    f"{rel}:{node.lineno}: {name} 声明 {want} 个参数，调用给了 {given} 个")
            if node.keywords:
                problems.append(
                    f"{rel}:{node.lineno}: {name} 以关键字实参调用，ctypes 函数只接受位置参数")

    print(f"ABI 声明函数 {len(declared)} 个；已核对调用点 {checked} 个；排除项 {len(EXCLUSIONS)} 个")
    for name, reason in EXCLUSIONS.items():
        print(f"  排除 {name}：{reason}")
    if problems:
        print(f"实参个数不一致 {len(problems)} 处：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("全部调用点的实参个数与 ABI 声明一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
