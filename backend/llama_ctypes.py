from __future__ import annotations

import sys
import os
import ctypes
import functools
import logging
import pathlib

log = logging.getLogger("echodesk.llama_ctypes")


class NativeSymbolMissing(RuntimeError):
    """
    [EN]
    A symbol was declared for the native library but is not exported by the DLL that
    was actually loaded. Raised at *call* time, never at import time: the backend must
    still start (and still work for every API the loaded build does provide), and the
    drift itself is tracked by ``tools/probe_native_symbols.py`` rather than hidden.

    [ZH]
    声明了该原生符号，但实际加载的 DLL 并未导出它。**调用时**才抛出，导入期绝不抛：
    后端必须仍能启动（并且在该构建确实提供的能力上正常工作），而声明漂移由
    ``tools/probe_native_symbols.py`` 负责跟踪，而不是被悄悄吞掉。
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            "native symbol %r is not exported by the loaded llama.cpp build" % name)
        self.symbol = name


def _missing_symbol(name: str):
    """[EN] Placeholder that fails loudly when called. / [ZH] 被调用即明确报错的占位器。"""

    def _raise(*_args, **_kwargs):
        raise NativeSymbolMissing(name)

    _raise.__name__ = name
    return _raise

from typing import (
    Any,
    Callable,
    List,
    Union,
    Optional,
    TYPE_CHECKING,
    TypeVar,
    Generic,
)
from typing import TypeAlias


#: [EN] RID -> directory name under ``backend/native``. The packaging pipeline builds one
#: [EN] directory per target and keeps only that target's libraries.
#: [ZH] RID 到 ``backend/native`` 下目录名的映射。打包流水线按目标平台构建，只保留该目标
#: [ZH] 的动态库。
NATIVE_RID_DIRS = {
    "win-x64": "win-x64",
    "win-arm64": "win-arm64",
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64",
    "osx-arm64": "osx-arm64",
    "osx-x64": "osx-x64",
}


def current_native_rid() -> str:
    """
    [EN]
    RID of the running interpreter, in the same vocabulary as the packaging scripts.
    Unknown platforms return an empty string so callers can fall back to the flat
    ``native`` directory used by older development trees.

    [ZH]
    当前解释器的 RID，命名与打包脚本一致。未知平台返回空串，使调用方可以回退到旧开发树
    使用的扁平 ``native`` 目录。
    """
    import platform  # noqa: PLC0415 - only needed when locating the native directory

    machine = (platform.machine() or "").lower()
    if sys.platform == "win32":
        return "win-x64" if machine in ("amd64", "x86_64") else "win-arm64"
    if sys.platform == "darwin":
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-x64"
    if sys.platform.startswith("linux"):
        return "linux-x64" if machine in ("x86_64", "amd64") else "linux-arm64"
    return ""


def native_library_dir() -> pathlib.Path:
    """
    [EN]
    Directory holding the native llama.cpp libraries for this process.

    Search order:
      1. ``ECHODESK_LLAMA_LIB`` (explicit override, used by tests and packaging);
      2. ``backend/native/<rid>`` (the release layout);
      3. ``backend/native`` flat (legacy development layout).

    A missing directory is returned as-is: the loader raises a precise error when it
    cannot find the library, and the backend can still start without a model.

    [ZH]
    本进程使用的 llama.cpp 原生库目录。

    查找顺序：
      1. ``ECHODESK_LLAMA_LIB``（显式覆盖，测试与打包使用）；
      2. ``backend/native/<rid>``（发布布局）；
      3. ``backend/native`` 扁平目录（旧开发布局）。

    目录不存在时原样返回：加载器找不到库时会给出精确错误，而后端仍可在无模型状态下启动。
    """
    override = os.environ.get("ECHODESK_LLAMA_LIB")
    if override:
        return pathlib.Path(override)
    base = pathlib.Path(os.path.abspath(os.path.dirname(__file__))) / "native"
    rid = current_native_rid()
    if rid:
        candidate = base / NATIVE_RID_DIRS.get(rid, rid)
        if candidate.is_dir():
            return candidate
    return base


# Load the library
def load_shared_library(lib_base_name: str, base_path: pathlib.Path):
    """Platform independent shared library loader"""
    # Searching for the library in the current directory under the name "libllama" (default name
    # for llamacpp) and "llama" (default name for this repo)
    lib_paths: List[pathlib.Path] = []
    # Determine the file extension based on the platform
    if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
        lib_paths += [
            base_path / f"lib{lib_base_name}.so",
        ]
    elif sys.platform == "darwin":
        lib_paths += [
            base_path / f"lib{lib_base_name}.so",
            base_path / f"lib{lib_base_name}.dylib",
        ]
    elif sys.platform == "win32":
        lib_paths += [
            base_path / f"{lib_base_name}.dll",
            base_path / f"lib{lib_base_name}.dll",
        ]
    else:
        raise RuntimeError("Unsupported platform")

    cdll_args = dict()  # type: ignore

    # [EN] POSIX: preload the ggml dependencies in dependency order with RTLD_GLOBAL so
    # [EN] that libllama can resolve them even when the wheel did not record an rpath.
    # [EN] Best effort: a missing optional backend (Metal/BLAS/mtmd) is not an error.
    # [ZH] POSIX：按依赖顺序以 RTLD_GLOBAL 预加载 ggml 依赖，使 libllama 在轮子未记录
    # [ZH] rpath 时也能解析它们。尽力而为：缺少可选后端（Metal/BLAS/mtmd）不算错误。
    if sys.platform.startswith(("linux", "darwin")):
        extension = ".so" if sys.platform.startswith("linux") else ".dylib"
        for base in ("libggml-base", "libggml", "libggml-cpu",
                     "libggml-metal", "libggml-blas", "libmtmd"):
            for candidate in (base + extension, base + ".0" + extension):
                dependency = base_path / candidate
                if not dependency.exists():
                    continue
                try:
                    ctypes.CDLL(str(dependency), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass
                break

    # Add the library directory to the DLL search path on Windows (if needed)
    if sys.platform == "win32":
        os.add_dll_directory(str(base_path))
        os.environ["PATH"] = str(base_path) + os.pathsep + os.environ["PATH"]

    if sys.platform == "win32" and sys.version_info >= (3, 8):
        os.add_dll_directory(str(base_path))
        if "CUDA_PATH" in os.environ:
            os.add_dll_directory(os.path.join(os.environ["CUDA_PATH"], "bin"))
            os.add_dll_directory(os.path.join(os.environ["CUDA_PATH"], "lib"))
        if "HIP_PATH" in os.environ:
            os.add_dll_directory(os.path.join(os.environ["HIP_PATH"], "bin"))
            os.add_dll_directory(os.path.join(os.environ["HIP_PATH"], "lib"))
        cdll_args["winmode"] = ctypes.RTLD_GLOBAL

    # Try to load the shared library, handling potential errors
    for lib_path in lib_paths:
        if lib_path.exists():
            try:
                return ctypes.CDLL(str(lib_path), **cdll_args)  # type: ignore
            except Exception as e:
                raise RuntimeError(f"Failed to load shared library '{lib_path}': {e}")

    raise FileNotFoundError(
        f"Shared library with base name '{lib_base_name}' not found"
    )


# ctypes sane type hint helpers
#
# - Generic Pointer and Array types
# - PointerOrRef type with a type hinted byref function
#
# NOTE: Only use these for static type checking not for runtime checks
# no good will come of that

if TYPE_CHECKING:
    CtypesCData = TypeVar("CtypesCData", bound=ctypes._CData)  # type: ignore

    CtypesArray: TypeAlias = ctypes.Array[CtypesCData]  # type: ignore

    CtypesPointer: TypeAlias = ctypes._Pointer[CtypesCData]  # type: ignore

    CtypesVoidPointer: TypeAlias = ctypes.c_void_p

    class CtypesRef(Generic[CtypesCData]):
        pass

    CtypesPointerOrRef: TypeAlias = Union[
        CtypesPointer[CtypesCData], CtypesRef[CtypesCData]
    ]

    CtypesFuncPointer: TypeAlias = ctypes._FuncPointer  # type: ignore

F = TypeVar("F", bound=Callable[..., Any])


def ctypes_function_for_shared_library(lib: ctypes.CDLL):
    """Decorator for defining ctypes functions with type hints"""

    def ctypes_function(
        name: str, argtypes: List[Any], restype: Any, enabled: bool = True
    ):
        def decorator(f: F) -> F:
            if enabled:
                try:
                    func = getattr(lib, name)
                except AttributeError:
                    # [EN] The declaration table is written against one llama.cpp
                    # [EN] revision; the DLL in ``backend/native`` may be another. A
                    # [EN] missing symbol must not break the import of the whole ABI
                    # [EN] module, so a call-time stub is installed instead. Anything
                    # [EN] the runtime actually needs will fail loudly on first use,
                    # [EN] naming the symbol.
                    # [ZH] 声明表按某个 llama.cpp 修订编写，而 ``backend/native`` 里的
                    # [ZH] DLL 可能是另一个修订。缺失符号不得让整个 ABI 模块导入失败，
                    # [ZH] 因此安装一个调用时才报错的占位器；运行时真正需要的能力会在
                    # [ZH] 首次调用时带着符号名炸出来。
                    log.debug("native symbol not exported, stubbed: %s", name)
                    stub = _missing_symbol(name)
                    functools.wraps(f)(stub)
                    return stub  # type: ignore[return-value]
                func.argtypes = argtypes
                func.restype = restype
                functools.wraps(f)(func)
                return func
            else:
                return f

        return decorator

    return ctypes_function


def _byref(obj: CtypesCData, offset: Optional[int] = None) -> CtypesRef[CtypesCData]:
    """Type-annotated version of ctypes.byref"""
    ...


byref = _byref if TYPE_CHECKING else ctypes.byref
