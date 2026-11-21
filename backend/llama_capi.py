"""
[EN]
Adapter between :mod:`llama_native` and the vendored llama.cpp ABI declarations.

Why this file exists instead of the hand-maintained declaration table: the native
libraries in ``backend/native/<rid>/`` are a specific llama.cpp revision, and the C
struct layouts (``llama_model_params``, ``llama_context_params``) change between
revisions. A declaration table written for another build does not merely miss a few
symbols -- it lays out structs with the wrong offsets and turns model loading into an
access violation. The vendored modules under ``llama_abi`` are generated from the
exact revision that produced these DLLs (they ship with the same MIT-licensed
distribution and carry the mangled-name candidate lists Windows needs).

[ZH]
:mod:`llama_native` 与内置 llama.cpp ABI 声明之间的适配层。

为什么要这个文件而不是手写声明表：``backend/native/<rid>/`` 里的原生库属于**某个确定的
llama.cpp 修订**，而 C 结构体布局（``llama_model_params``、``llama_context_params``）在
修订之间会变。按另一个构建写的声明表不只是"少几个符号"——它会把结构体按错误偏移摆放，
使加载模型直接变成访问违例。``llama_abi`` 下的模块正是由产出这些 DLL 的同一修订生成
（同一 MIT 授权的分发产物，且自带 Windows 需要的修饰名候选清单）。

The library directory is pinned to ``backend/native/<rid>`` before the vendored module
is imported, because that module resolves and loads the shared library at import time.

在导入内置模块**之前**把库目录钉到 ``backend/native/<rid>``：该模块在导入时就会解析并
加载共享库。
"""
from __future__ import annotations

import os
import sys

_NATIVE_SET = False


def _pin_library_dir() -> None:
    """
    [EN] Point the vendored ABI at our own native directory unless the operator already
    [EN] overrode it. Must run before ``llama_abi.llama_cpp`` is imported.
    [ZH] 若操作者没有显式覆盖，把内置 ABI 指向我们自己的原生库目录。必须在导入
    [ZH] ``llama_abi.llama_cpp`` 之前执行。
    """
    global _NATIVE_SET
    if _NATIVE_SET or os.environ.get("LLAMA_CPP_LIB_PATH"):
        _NATIVE_SET = True
        return
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in _native_candidates(backend_dir):
        if os.path.isdir(candidate):
            os.environ["LLAMA_CPP_LIB_PATH"] = candidate
            break
    _NATIVE_SET = True


def _native_candidates(backend_dir: str):
    """
    [EN] Same search order as the packaging layout: ``native/<rid>`` first, then the
    [EN] flat ``native`` directory used by older development trees.
    [ZH] 与打包布局一致的查找顺序：先 ``native/<rid>``，再回退到旧开发树的扁平 ``native``。
    """
    rid = ""
    try:
        import llama_ctypes  # local import: loader owns the RID vocabulary

        rid = llama_ctypes.current_native_rid()
    except Exception:  # noqa: BLE001 - a missing loader must not break the ABI import
        rid = ""
    root = os.path.join(backend_dir, "native")
    if rid:
        yield os.path.join(root, rid)
    yield root


_pin_library_dir()

from llama_abi.llama_cpp import *  # noqa: E402,F401,F403 - deliberate re-export
from llama_abi import llama_cpp as _module  # noqa: E402

for _name in dir(_module):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_module, _name))
