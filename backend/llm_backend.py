# -*- coding: utf-8 -*-
"""
[EN]
The single place that decides which llama.cpp runtime serves a model.

Two runtimes coexist during T3:

* ``native`` -- :class:`llama_native.NativeLlama`, the in-tree ctypes runtime over
  ``backend/native/<rid>`` (no CUDA dependency);
* ``wheel`` -- ``llama_cpp.Llama`` from the installed ``llama-cpp-python`` wheel,
  which still owns vision (``chat_handler``/mmproj) and is the field rollback lever.

``ECHODESK_LLAMA_RUNTIME`` selects the policy: ``auto`` (default), ``native`` or
``wheel``. ``auto`` sends text models to the native runtime and anything carrying a
``chat_handler`` to the wheel, because vision is not implemented natively until T3-2.
A native load failure in ``auto`` is never silently retried on the wheel: that would
paint a native defect as "it works" and we would never receive the report.

[ZH]
唯一决定"某个模型由哪套 llama.cpp 运行期服务"的地方。

T3 期间两套运行期共存：

* ``native`` —— :class:`llama_native.NativeLlama`，基于 ``backend/native/<rid>`` 的
  树内 ctypes 运行期（不依赖 CUDA）；
* ``wheel`` —— 已安装的 ``llama-cpp-python`` 轮子里的 ``llama_cpp.Llama``，视觉
  （``chat_handler``/mmproj）仍由它承担，它同时也是现场回滚杆。

``ECHODESK_LLAMA_RUNTIME`` 选择策略：``auto``（默认）/ ``native`` / ``wheel``。
``auto`` 把纯文本模型交给原生运行期，把带 ``chat_handler`` 的交给轮子 —— 视觉在
T3-2 之前没有原生实现。``auto`` 下原生加载失败**绝不**静默重试轮子：那会把原生缺陷
伪装成"能跑"，我们就永远收不到该有的报告。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict

from vision_errors import CODE_RUNTIME_UNAVAILABLE, VisionError

log = logging.getLogger("echodesk.llm_backend")

#: [EN] Environment variable selecting the runtime policy.
#: [ZH] 选择运行期策略的环境变量。
RUNTIME_ENV = "ECHODESK_LLAMA_RUNTIME"

#: [EN] Accepted values; anything else is treated as ``auto``.
#: [ZH] 合法取值；其它值按 ``auto`` 处理。
MODES = ("auto", "native", "wheel")


class NativeVisionUnsupported(RuntimeError):
    """
    [EN]
    Raised when no available runtime can serve a vision request.

    Vision still goes through ``llama-cpp-python`` until ``mtmd`` is wired up, so the
    only runtime that can answer an image is the wheel. When the policy forbids the
    wheel (``native``) or the wheel is not installed at all (the post-T3-3 ``auto``
    case), the request must fail loudly: silently degrading to a text-only answer
    would make the user believe the image was read.

    The class lives here rather than in :mod:`llama_native` because the routing layer
    has to import it without importing the native runtime: ``llama_native`` loads the
    native DLLs at import time, and the vision path must be able to refuse *before*
    any runtime DLL is loaded. :mod:`llama_native` re-exports this class.

    [ZH]
    没有任何可用运行期能承载视觉请求时抛出。

    在 ``mtmd`` 接通之前，视觉仍走 ``llama-cpp-python``，因此唯一能回答图片的运行期是
    轮子。当策略禁止轮子（``native``）或轮子根本没安装（T3-3 之后的 ``auto`` 情形）时，
    请求必须显式失败：静默降级成纯文本回答会让用户误以为图片真的被读了。

    该类放在此处而不是 :mod:`llama_native`，是因为路由层必须能在**不导入原生运行期**的
    前提下导入它：``llama_native`` 在导入时就会加载原生 DLL，而视觉路径必须能在任何
    运行期 DLL 加载**之前**就拒绝。:mod:`llama_native` 重新导出该类。
    """

_SELECTION_LOCK = threading.Lock()
#: [EN] Result of the most recent decision, readable for diagnostics/status.
#: [ZH] 最近一次决策的结果，供诊断/状态读取。
_LAST_SELECTION: Dict[str, Any] = {}
_WARNED_BAD_MODE = False
#: [EN] Cached result of "can llama_cpp be imported"; ``None`` means not probed yet.
#: [ZH] 「llama_cpp 能否导入」的缓存结果；``None`` 表示尚未探测。
_WHEEL_STATE: "bool | None" = None


def runtime_mode() -> str:
    """
    [EN]
    Return the configured runtime policy (``auto`` / ``native`` / ``wheel``).

    An unknown value falls back to ``auto`` with a single warning per process: a
    typo in an installer script or service definition must not brick the app, but it
    must be visible in the log.

    [ZH]
    返回配置的运行期策略（``auto`` / ``native`` / ``wheel``）。

    未知值回落到 ``auto``，每个进程告警一次：安装脚本或服务定义里的笔误不能让应用不可用，
    但必须出现在日志里。
    """
    global _WARNED_BAD_MODE
    raw = str(os.environ.get(RUNTIME_ENV) or "").strip().lower()
    mode = raw or "auto"
    if mode not in MODES:
        if not _WARNED_BAD_MODE:
            _WARNED_BAD_MODE = True
            log.warning("%s=%r 非法，按 auto 处理（有效值：%s）",
                        RUNTIME_ENV, raw, "/".join(MODES))
        mode = "auto"
    return mode


def last_selection() -> Dict[str, Any]:
    """[EN] Copy of the most recent runtime decision. / [ZH] 最近一次运行期决策的副本。"""
    with _SELECTION_LOCK:
        return dict(_LAST_SELECTION)


def will_use_wheel(**kwargs) -> bool:
    """
    [EN]
    Whether a call with these ``kwargs`` will end up on the wheel runtime.

    The engine uses this to decide whether the wheel's DLL preload bootstrap may run.
    That bootstrap registers ``llama_cpp/lib`` so the wheel can load its copies; for
    the native runtime it is actively harmful, because Windows resolves a dependent
    ``ggml.dll`` from the already-loaded module list first, so the native ``llama.dll``
    can end up bound to the wheel's ``ggml.dll`` and report "no backends are loaded".

    [ZH]
    这些 ``kwargs`` 最终是否会落到轮子运行期。

    引擎据此决定能否运行轮子的 DLL 预加载引导。该引导会注册 ``llama_cpp/lib`` 让轮子
    加载自己的副本；对原生运行期它是有害的：Windows 会先从"已加载模块列表"解析依赖的
    ``ggml.dll``，原生 ``llama.dll`` 可能因此绑定到轮子的 ``ggml.dll`` 上，并报
    "no backends are loaded"。
    """
    mode = runtime_mode()
    if mode == "wheel":
        return True
    if mode == "native":
        return False
    return kwargs.get("chat_handler") is not None


def vision_runtime() -> str:
    """
    [EN]
    Return the runtime that must serve a vision request, or raise.

    This is the single decision point for vision routing (task book §12.2: one place
    decides, nobody re-decides). The policy is fixed and asymmetric on purpose:

    * ``wheel``  -> the wheel, or a loud failure if it cannot even be imported;
    * ``native`` -> the native mtmd engine, or a loud failure if its DLLs are absent;
    * ``auto``   -> **the wheel whenever it works**, because on a GPU machine it is
      both faster and field-proven; only when the wheel cannot be imported at all
      does it fall back to the native engine. This is what makes T3-3 safe: once
      ``llama-cpp-python`` is deleted, ``auto`` keeps working by itself instead of
      erroring out.

    "Usable" is decided by actually importing ``llama_cpp``: a shadow/broken package
    must count as unavailable, and the import is needed anyway when the wheel is
    chosen. The native side is probed by file presence only, because importing the
    native runtime loads its DLLs and that must not happen while the wheel serves.

    [ZH]
    返回必须承载视觉请求的运行期，或抛出。

    这是视觉路由的唯一判定点（任务书 §12.2：一处判定，不得在别处重复判定）。策略是刻意
    不对称的：

    * ``wheel`` —— 用轮子；连导入都不行就明确失败；
    * ``native`` —— 用原生 mtmd 引擎；DLL 缺失就明确失败；
    * ``auto`` —— **轮子能用就用轮子**：在 GPU 机器上它更快且久经验证；只有轮子完全
      无法导入时才回落到原生引擎。这条正是 T3-3 的安全垫：一旦删除
      ``llama-cpp-python``，``auto`` 会自己继续工作而不是报错。

    「可用」以真正导入 ``llama_cpp`` 为准：影子包/损坏包必须算作不可用，而且选中轮子时
    本来就需要这次导入。原生侧只做文件存在性探测，因为导入原生运行期会加载它的 DLL，
    而在轮子服务时那绝不能发生。
    """
    mode = runtime_mode()
    if mode == "wheel":
        if not _wheel_importable():
            raise VisionError(
                CODE_RUNTIME_UNAVAILABLE, "media.vision_failed",
                detail="%s=wheel but llama-cpp-python cannot be imported"
                       % RUNTIME_ENV)
        _record("wheel", mode, "forced by %s=wheel" % RUNTIME_ENV)
        return "wheel"
    if mode == "native":
        if not _native_available():
            raise VisionError(
                CODE_RUNTIME_UNAVAILABLE, "media.vision_failed",
                detail="%s=native but the native mtmd runtime is not present"
                       % RUNTIME_ENV)
        _record("native", mode, "forced by %s=native" % RUNTIME_ENV)
        return "native"
    # [EN] ``auto``: the wheel is the default; only a missing wheel falls back.
    # [ZH] ``auto``：默认用轮子；只有轮子缺失才回落。
    if _wheel_importable():
        _record("wheel", mode, "auto: llama-cpp-python is available")
        return "wheel"
    if _native_available():
        reason = ("auto: llama-cpp-python is not importable, falling back to the "
                  "native mtmd engine")
        _record("native", mode, reason, fallback=True)
        return "native"
    _record("none", mode, "auto: neither the wheel nor the native runtime is usable")
    raise VisionError(
        CODE_RUNTIME_UNAVAILABLE, "media.vision_failed",
        detail="no vision runtime is available under %s=auto: llama-cpp-python is "
               "not importable and backend/native/<rid> is missing" % RUNTIME_ENV)


def _wheel_importable() -> bool:
    """
    [EN] Whether ``llama_cpp`` can actually be imported (cached per process).

    A shadow package on ``PYTHONPATH`` that raises on import must count as unusable,
    which ``find_spec`` alone would miss.

    [ZH]
    ``llama_cpp`` 是否真的可以导入（按进程缓存）。

    ``PYTHONPATH`` 上导入即抛错的影子包必须算作不可用，仅靠 ``find_spec`` 会漏掉这种情况。
    """
    global _WHEEL_STATE
    if _WHEEL_STATE is not None:
        return _WHEEL_STATE
    try:
        import llama_cpp  # noqa: F401,PLC0415 - the import IS the probe
        _WHEEL_STATE = True
    except Exception:  # noqa: BLE001 - any import failure means "unusable"
        _WHEEL_STATE = False
    return _WHEEL_STATE


def _native_available() -> bool:
    """
    [EN] Whether the native mtmd runtime exists for this platform.

    Deliberately file-based: importing ``llama_native`` would load the native DLLs,
    which must not happen while the wheel is the selected runtime.

    [ZH]
    本平台是否存在原生 mtmd 运行期。

    刻意只查文件：导入 ``llama_native`` 会加载原生 DLL，而在选中轮子时那绝不能发生。
    """
    try:
        import llama_ctypes  # noqa: PLC0415 - lightweight, no DLL load
        rid = llama_ctypes.current_native_rid()
    except Exception:  # noqa: BLE001
        rid = ""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
    candidates = [os.path.join(root, rid)] if rid else []
    candidates.append(root)
    for directory in candidates:
        if os.path.isfile(os.path.join(directory, "mtmd.dll")):
            return True
        if os.path.isfile(os.path.join(directory, "libmtmd.so")):
            return True
        if os.path.isfile(os.path.join(directory, "libmtmd.dylib")):
            return True
    return False


def record_vision_fallback(reason: str) -> None:
    """
    [EN] Record that ``auto`` had to move from the wheel to the native engine.

    Called only from the selection stage in :mod:`vision`; the reason is surfaced by
    ``VisionService.status()`` so support can see *why* the slower engine is active.

    [ZH]
    记录 ``auto`` 从轮子回落到原生引擎这一事实。

    仅由 :mod:`vision` 的选路阶段调用；原因通过 ``VisionService.status()`` 暴露，使
    支持人员能看到较慢引擎**为何**生效。
    """
    with _SELECTION_LOCK:
        _LAST_SELECTION.update({
            "runtime": "native", "mode": "auto", "reason": str(reason),
            "fallback": True,
        })


def vision_fallback_reason() -> str:
    """[EN] Why ``auto`` fell back to native (empty when it did not). / [ZH] ``auto`` 回落到原生的原因（未回落时为空）。"""
    with _SELECTION_LOCK:
        if not _LAST_SELECTION.get("fallback"):
            return ""
        return str(_LAST_SELECTION.get("reason") or "")


def _record(runtime: str, mode: str, reason: str, **extra: Any) -> None:
    with _SELECTION_LOCK:
        _LAST_SELECTION.pop("fallback", None)
        _LAST_SELECTION.update({"runtime": runtime, "mode": mode, "reason": reason})
        if extra:
            _LAST_SELECTION.update(extra)


def create_llama(**kwargs):
    """
    [EN]
    Construct the llama object for ``kwargs`` according to the runtime policy.

    ``chat_handler`` is the vision signal: ``auto`` keeps such calls on the wheel,
    while ``native`` lets :class:`NativeVisionUnsupported` escape immediately -- a
    text-only answer for an image would be worse than a failure.

    A ``NativeLlama`` construction failure in ``auto`` is re-raised with the rollback
    hint and the original exception chained; no fallback happens.

    [ZH]
    按运行期策略为 ``kwargs`` 构造 llama 对象。

    ``chat_handler`` 是视觉信号：``auto`` 让这类调用留在轮子上，``native`` 则让
    :class:`NativeVisionUnsupported` 立即抛出 —— 对图片给出纯文本回答比失败更糟。

    ``auto`` 下 ``NativeLlama`` 构造失败会带上回滚提示重新抛出，并链上原始异常；不发生
    任何回落。
    """
    mode = runtime_mode()
    chat_handler = kwargs.get("chat_handler")
    vision = chat_handler is not None

    if mode == "wheel":
        runtime, reason = "wheel", "forced by %s=wheel" % RUNTIME_ENV
    elif mode == "native":
        if vision:
            from llama_native import NativeVisionUnsupported  # noqa: PLC0415

            raise NativeVisionUnsupported(
                "vision requires the llama-cpp-python runtime until T3-2: "
                "%s=native forbids a chat_handler on NativeLlama" % RUNTIME_ENV)
        runtime, reason = "native", "forced by %s=native" % RUNTIME_ENV
    elif vision:
        # [EN] Vision has no native implementation yet, so auto keeps it on the wheel.
        # [ZH] 视觉尚无原生实现，因此 auto 让它留在轮子上。
        runtime, reason = "wheel", "auto: chat_handler supplied, vision stays on llama-cpp-python"
    else:
        runtime, reason = "native", "auto: text model on the native runtime"

    _record(runtime, mode, reason)

    if runtime == "wheel":
        # [EN] Imported lazily so native-only deployments never touch the wheel.
        # [ZH] 延迟导入，使只用原生的部署完全不碰轮子。
        from llama_cpp import Llama  # noqa: PLC0415

        return Llama(**kwargs)

    from llama_native import NativeLlama  # noqa: PLC0415

    try:
        return NativeLlama(**kwargs)
    except Exception as exc:  # noqa: BLE001 - re-raised below, never swallowed
        # [EN] Deliberately no wheel fallback: a quiet fallback would hide the native
        # [EN] defect. The message tells the operator the rollback lever exists.
        # [ZH] 刻意不回落轮子：静默回落会掩盖原生缺陷。消息里告知操作者回滚杆存在。
        message = (
            "native llama runtime failed to load the model under "
            "%s=%s: %s: %s. There is no automatic fallback; to keep running on "
            "llama-cpp-python set %s=wheel."
            % (RUNTIME_ENV, mode, type(exc).__name__, exc, RUNTIME_ENV)
        )
        raise RuntimeError(message) from exc
