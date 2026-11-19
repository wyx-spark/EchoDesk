# -*- coding: utf-8 -*-
"""
[EN]
Windows DLL preload bootstrap for the local llama.cpp runtime.

Background (v0.2.6 item 23): the official ``llama-cpp-python`` wheel for Windows
ships no ``ggml-cuda.dll`` and resolves native dependencies with the process-wide
``LoadLibrary`` search rules, which on Windows do not consult ``RTLD_GLOBAL``-style
global symbol tables at all. When ``llama.dll`` is imported before ``ggml-cuda.dll``
has its own directory registered, the CUDA backend cannot resolve its sibling
``ggml`` symbols and llama.cpp terminates the process with ``access violation
reading 0x0`` (0xC0000005). Python cannot catch a C++ access violation, so the
only reliable fix is to load the DLLs in dependency order, and to register the
``nvidia\\*\\bin`` directories that carry the CUDA runtime, **before** any module
imports ``llama_cpp``.

``ensure()`` is idempotent and a no-op outside Windows. It is called from the
backend entry point (``main.py``), from the engine's lazy import sites and from
the vision adapter, so every load path is covered even when a tool imports a
module directly.

[ZH]
Windows 下 llama.cpp 运行时的 DLL 预加载引导。

背景（v0.2.6 第 23 项）：官方 ``llama-cpp-python`` 的 Windows 轮子不包含
``ggml-cuda.dll``，且原生依赖的解析走进程级 ``LoadLibrary`` 搜索规则——Windows
根本没有 ``RTLD_GLOBAL`` 那套全局符号表。若 ``llama.dll`` 先于 ``ggml-cuda.dll``
所在目录完成注册就被导入，CUDA 后端无法解析同目录的 ``ggml`` 符号，llama.cpp
会以 ``access violation reading 0x0``（0xC0000005）终止进程。Python 无法捕获
C++ 访问冲突，因此唯一可靠的做法是：**在任何模块导入 ``llama_cpp`` 之前**，
按依赖顺序加载 DLL，并注册承载 CUDA 运行时的 ``nvidia\\*\\bin`` 目录。

``ensure()`` 幂等，非 Windows 平台上为空操作。后端入口（``main.py``）、引擎的
惰性导入点与视觉适配层都会调用它，因此即使某个工具脚本直接导入模块，也不会
绕过引导。
"""
from __future__ import annotations

import ctypes
import glob
import logging
import os
import sys

log = logging.getLogger("EchoDesk.CudaBootstrap")

#: [EN] Guard flag making :func:`ensure` idempotent: the bootstrap must never run
#: [EN] twice, because a second pass would re-register the same directories and
#: [EN] reload the same DLLs for no benefit.
#: [ZH] 使 :func:`ensure` 幂等的守卫标记：引导绝不能执行两次，因为第二遍只会
#: [ZH] 重复注册同一批目录、重复加载同一批 DLL，没有任何收益。
_done = False

#: [EN] Load order of the local DLLs. It mirrors the real dependency chain:
#: [EN] ``ggml-base`` < ``ggml`` < ``ggml-cpu`` < ``ggml-cuda`` < ``llama``.
#: [EN] Loading a dependent before its dependency is exactly the failure this
#: [EN] module exists to prevent.
#: [ZH] 本地 DLL 的加载顺序，对应真实的依赖链：
#: [ZH] ``ggml-base`` < ``ggml`` < ``ggml-cpu`` < ``ggml-cuda`` < ``llama``。
#: [ZH] 在依赖之前加载被依赖项，正是本模块要避免的失败。
_PRELOAD_ORDER = ["ggml-base.dll", "ggml.dll", "ggml-cpu.dll", "ggml-cuda.dll", "llama.dll"]

#: [EN] Files actually loaded by the last successful :func:`ensure` call. It is
#: [EN] diagnostic only: the self-test and the crash record use it to prove that
#: [EN] the expected DLLs were preloaded instead of being resolved lazily.
#: [ZH] 最近一次成功的 :func:`ensure` 实际加载的文件。仅用于诊断：自检与崩溃
#: [ZH] 记录据此证明期望的 DLL 已被预加载，而不是留给惰性解析。
_loaded: list[str] = []


def _site_packages_dirs():
    """
    [EN]
    Yield every distinct ``site-packages`` directory on ``sys.path``.

    A plain ``for path in sys.path`` is not enough: the same directory can appear
    twice (``PYTHONPATH`` plus the interpreter's own path), and duplicate
    ``os.add_dll_directory`` registrations are pointless work.

    [ZH]
    逐个产出 ``sys.path`` 上不重复的 ``site-packages`` 目录。

    仅遍历 ``sys.path`` 是不够的：同一目录可能出现两次（``PYTHONPATH`` 加上
    解释器自带路径），重复调用 ``os.add_dll_directory`` 属于无意义的开销。
    """
    seen = set()
    for p in sys.path:
        if p.endswith("site-packages") and p not in seen:
            seen.add(p)
            yield p


def _frozen_roots():
    """
    [EN]
    Yield the roots added by PyInstaller when the backend runs frozen.

    In a frozen onedir build ``sys.path`` contains ``sys._MEIPASS``
    (``_internal``), not a directory named ``site-packages``. Without these
    roots :func:`ensure` would silently do nothing in the shipped backend, which
    is precisely the packaging hole item 23 has to close.

    [ZH]
    当后端以冻结形式运行时，产出 PyInstaller 附加的根目录。

    在冻结的 onedir 产物里，``sys.path`` 包含 ``sys._MEIPASS``（``_internal``），
    而不是名为 ``site-packages`` 的目录。缺少这些根目录会让 :func:`ensure`
    在随包后端中静默失效，而这正是第 23 项必须堵住的打包漏洞。
    """
    seen = set()
    for p in (getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable or "")):
        if p and os.path.isdir(p) and p not in seen:
            seen.add(p)
            yield p


def _register(directory: str) -> None:
    """
    [EN]
    Register one directory in the process DLL search path.

    ``os.add_dll_directory`` is the documented Windows API since Python 3.8 and
    only affects ``LoadLibraryEx`` calls made with the default search flags, which
    is what ``ctypes.CDLL`` uses. A missing directory and an unsupported platform
    are both ignored: the bootstrap must never be the reason a model fails to
    load.

    [ZH]
    把一个目录注册进进程 DLL 搜索路径。

    ``os.add_dll_directory`` 是 Python 3.8 起的官方 Windows 接口，只影响以默认
    搜索标志执行的 ``LoadLibraryEx`` 调用，而 ``ctypes.CDLL`` 正是如此。目录
    不存在或平台不支持都直接忽略：引导本身绝不能成为模型加载失败的原因。
    """
    try:
        os.add_dll_directory(directory)
    except (OSError, AttributeError):
        pass


def ensure() -> None:
    """
    [EN]
    Preload the local llama.cpp DLLs, in dependency order, if they are present.

    The CUDA runtime directories registered by the ``nvidia-*`` wheels come
    first, because ``ggml-cuda.dll`` links against them; the ``llama_cpp/lib``
    directory comes second, because it holds the ``ggml*`` and ``llama`` DLLs
    themselves. When no ``ggml-cuda.dll`` is found the function returns without
    touching any DLL: a CPU-only wheel must keep working exactly as before.

    [ZH]
    若本地存在 llama.cpp 的 DLL，则按依赖顺序预加载。

    先注册 ``nvidia-*`` 轮子提供的 CUDA 运行时目录，因为 ``ggml-cuda.dll``
    链接这些库；再注册 ``llama_cpp/lib`` 目录，因为 ``ggml*`` 与 ``llama``
    DLL 本体位于其中。找不到 ``ggml-cuda.dll`` 时直接返回、不触碰任何 DLL：
    纯 CPU 轮子必须保持与之前完全一致的行为。
    """
    global _done
    if _done:
        return
    _done = True
    if sys.platform != "win32":
        return

    # [EN] The Reader backend ships its own native runtime under ``backend/native/<rid>``.
    # [EN] When it exists it is the only directory that may be registered: preloading a
    # [EN] different ``llama_cpp/lib`` left over in the build interpreter's site-packages
    # [EN] would put a mismatched ggml.dll into the process and make model loading fail.
    # [ZH] Reader 后端自带 ``backend/native/<rid>`` 运行期。它存在时就是唯一允许注册的
    # [ZH] 目录：若预加载构建解释器 site-packages 里遗留的另一个 ``llama_cpp/lib``，
    # [ZH] 会把版本不匹配的 ggml.dll 带进进程，导致模型加载失败。
    import llama_ctypes  # noqa: PLC0415 - local import keeps this module dependency-free

    rid = llama_ctypes.current_native_rid()
    native_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native", rid) if rid else ""
    lib_dirs: list[str] = []
    roots = list(_site_packages_dirs()) + list(_frozen_roots())
    for root in roots:
        nvidia_root = os.path.join(root, "nvidia")
        if os.path.isdir(nvidia_root):
            for bin_dir in glob.glob(os.path.join(nvidia_root, "*", "bin")):
                _register(bin_dir)
    if native_dir and os.path.isdir(native_dir):
        lib_dirs.append(native_dir)
    else:
        for root in roots:
            candidate = os.path.join(root, "llama_cpp", "lib")
            if os.path.isdir(candidate):
                lib_dirs.append(candidate)

    seen_dirs: set[str] = set()
    for lib_dir in lib_dirs:
        if lib_dir in seen_dirs:
            continue
        seen_dirs.add(lib_dir)
        _register(lib_dir)
        # [EN] A CPU-only build has no ``ggml-cuda.dll``; skip preloading for that
        # [EN] directory instead of aborting the whole pass, so a second root that
        # [EN] does carry the CUDA backend is still handled.
        # [ZH] 纯 CPU 构建没有 ``ggml-cuda.dll``；对该目录跳过预加载而不是终止整轮，
        # [ZH] 使另一个确实带有 CUDA 后端的根目录仍会被处理。
        # [EN] A directory that is the app's native runtime carries exactly the backends
        # [EN] it ships; preload what exists. Legacy wheel directories are skipped when
        # [EN] they are CPU-only, as before.
        # [ZH] 应用自带运行期目录只包含它随包的 backend，存在即预加载。旧轮子目录仍按
        # [ZH] 纯 CPU 情况跳过。
        if os.path.normcase(lib_dir) != os.path.normcase(native_dir) and \
                not os.path.exists(os.path.join(lib_dir, "ggml-cuda.dll")):
            continue
        for name in _PRELOAD_ORDER:
            path = os.path.join(lib_dir, name)
            if not os.path.exists(path):
                continue
            try:
                ctypes.CDLL(path)
            except OSError:
                # [EN] A single unloadable library must not abort the remaining
                # [EN] ones: the llama.cpp loader reports the same problem later
                # [EN] with a much better message than this bootstrap could.
                # [ZH] 单个库无法加载不得中断其余库：llama.cpp 加载器稍后会以远好于
                # [ZH] 本引导的报错信息说明同一问题。
                log.debug("预加载 DLL 失败（忽略）：%s", path, exc_info=True)
                continue
            _loaded.append(path)
    if _loaded:
        log.info("已按依赖顺序预加载 %d 个 llama.cpp DLL", len(_loaded))


def loaded_libraries() -> list[str]:
    """
    [EN]
    Return the DLL paths preloaded by :func:`ensure` in this process.

    The returned list is a copy: callers (the self-test, the crash record) may
    inspect it but must not be able to mutate the bootstrap's own state.

    [ZH]
    返回本进程内由 :func:`ensure` 预加载的 DLL 路径。

    返回的是副本：调用方（自检、崩溃记录）可以检视，但不得改动引导自身的状态。
    """
    return list(_loaded)
