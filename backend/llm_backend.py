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

log = logging.getLogger("echodesk.llm_backend")

#: [EN] Environment variable selecting the runtime policy.
#: [ZH] 选择运行期策略的环境变量。
RUNTIME_ENV = "ECHODESK_LLAMA_RUNTIME"

#: [EN] Accepted values; anything else is treated as ``auto``.
#: [ZH] 合法取值；其它值按 ``auto`` 处理。
MODES = ("auto", "native", "wheel")

_SELECTION_LOCK = threading.Lock()
#: [EN] Result of the most recent decision, readable for diagnostics/status.
#: [ZH] 最近一次决策的结果，供诊断/状态读取。
_LAST_SELECTION: Dict[str, Any] = {}
_WARNED_BAD_MODE = False


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


def _record(runtime: str, mode: str, reason: str) -> None:
    with _SELECTION_LOCK:
        _LAST_SELECTION.update({"runtime": runtime, "mode": mode, "reason": reason})


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
