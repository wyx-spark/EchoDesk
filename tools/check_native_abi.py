#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[EN]
Verify that the platform-specific ABI variants in ``backend/llama_capi.py`` match the
ctypes declarations of the wheels whose native libraries are shipped under
``backend/native/<rid>``.

The three wheels were built against different llama.cpp revisions. This tool executes the
``llama_model_params`` / ``llama_context_params`` class declarations from each wheel in a
namespace populated with this project's helper types, then compares struct size, field
order and every field offset with the variant that ``llama_capi`` selects on that platform.
It also checks the ``llama_sampler_init_penalties`` argument count. A mismatch means the
runtime would read the native struct with the wrong layout — exactly the failure this
check exists to prevent.

Usage:
    python tools/check_native_abi.py

[ZH]
校验 ``backend/llama_capi.py`` 的分平台 ABI 变体与随包原生库所属轮子的 ctypes 声明一致。

三个轮子针对不同的 llama.cpp 修订构建。本工具在注入了本项目辅助类型的命名空间中执行每个
轮子里的 ``llama_model_params`` / ``llama_context_params`` 类声明，再与 ``llama_capi`` 在
该平台选用的变体逐项比较结构体大小、字段顺序与每个字段偏移；同时校验
``llama_sampler_init_penalties`` 的参数个数。不一致意味着运行期会以错误布局读取原生
结构体——这正是本检查要拦截的故障。

用法：
    python tools/check_native_abi.py
"""
from __future__ import annotations

import ctypes
import glob
import io
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
WHEELS = os.path.join(ROOT, "artifacts", "wheels")

# [EN] RID -> (wheel glob, model-params variant, context-params variant, penalty arity).
# [ZH] RID ->（轮子 glob、模型参数变体、上下文参数变体、惩罚采样器参数个数）。
PLATFORMS = {
    "win-x64": ("llama_cpp_python-0.3.19-*-win_amd64.whl",
                "_llama_model_params_legacy", "_llama_context_params_legacy", 4),
    "linux-x64": ("llama_cpp_python-0.3.36-*-manylinux*.whl",
                  "_llama_model_params_linux", "_llama_context_params_linux", 5),
    "osx-arm64": ("llama_cpp_python-0.3.30-*-macosx_11_0_arm64.whl",
                  "_llama_model_params_legacy", "_llama_context_params_macos", 4),
}


def _load_reference(wheel: str, class_name: str, namespace: dict) -> type:
    """[EN] Execute one class declaration from the wheel's binding file.
    [ZH] 从轮子的绑定文件中执行一个类声明。"""
    with zipfile.ZipFile(wheel) as archive:
        candidates = [name for name in archive.namelist()
                      if name.endswith("llama_cpp.py") and "/lib/" not in name]
        if not candidates:
            raise SystemExit(f"[EN] binding file not found in {wheel}\n"
                             f"[ZH] {wheel} 中找不到绑定文件")
        source = archive.read(candidates[0]).decode("utf-8", "replace")
    match = re.search(
        r"^class %s\(ctypes\.Structure\):.*?^    _fields_ = \[.*?^    \]"
        % re.escape(class_name), source, re.S | re.M)
    if not match:
        raise SystemExit(f"[EN] class {class_name} not found in {wheel}\n"
                         f"[ZH] {wheel} 中找不到 {class_name} 类")
    local = dict(namespace)
    local["ctypes"] = ctypes
    exec(compile(match.group(0), wheel, "exec"), local)  # noqa: S102 - trusted local wheel
    return local[class_name]


def _compare(label: str, ours: type, reference: type) -> list[str]:
    problems: list[str] = []
    if ctypes.sizeof(ours) != ctypes.sizeof(reference):
        problems.append(f"{label}: size {ctypes.sizeof(ours)} != {ctypes.sizeof(reference)}")
    ours_fields = list(ours._fields_)
    ref_fields = list(reference._fields_)
    if [name for name, _ in ours_fields] != [name for name, _ in ref_fields]:
        problems.append(f"{label}: field order differs")
    for name, _type in ref_fields:
        if name not in dict(ours._fields_):
            continue
        if getattr(ours, name).offset != getattr(reference, name).offset:
            problems.append(
                f"{label}.{name}: offset {getattr(ours, name).offset} "
                f"!= {getattr(reference, name).offset}")
    return problems


def main() -> int:
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    import llama_capi  # noqa: PLC0415 - needs the backend path

    namespace = dict(vars(llama_capi))
    failures = 0
    print("== native ABI parity ==")
    for rid, (pattern, model_variant, context_variant, penalty_arity) in PLATFORMS.items():
        wheels = sorted(glob.glob(os.path.join(WHEELS, pattern)))
        if not wheels:
            print(f"[--] {rid}: wheel not found ({pattern}); skipped")
            continue
        wheel = wheels[-1]
        print(f"-- {rid}: {os.path.basename(wheel)}")
        problems = []
        for class_name, variant in (("llama_model_params", model_variant),
                                    ("llama_context_params", context_variant)):
            reference = _load_reference(wheel, class_name, namespace)
            ours = getattr(llama_capi, variant)
            problems += _compare(f"{rid}/{class_name}", ours, reference)
        with zipfile.ZipFile(wheel) as archive:
            candidates = [name for name in archive.namelist()
                          if name.endswith("llama_cpp.py") and "/lib/" not in name]
            source = archive.read(candidates[0]).decode("utf-8", "replace")
        signature = re.search(r"def llama_sampler_init_penalties\((.*?)\)",
                              source, re.S)
        actual = 0
        if signature:
            actual = len([part for part in signature.group(1).split(",")
                          if part.strip() and not part.strip().startswith("/")])
        if actual != penalty_arity:
            problems.append(
                f"{rid}/llama_sampler_init_penalties: {actual} args != {penalty_arity}")
        if problems:
            failures += 1
            for problem in problems:
                print(f"[FAIL] {problem}")
        else:
            print(f"[OK]   {rid}: model/context layouts and penalty arity match the wheel")
    print("== result:", "PASS" if failures == 0 else "FAIL", "==")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
