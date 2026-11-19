# -*- coding: utf-8 -*-
"""
[EN]
Shared backend support layer for the Reader edition: protocol I/O, crash handling,
log sanitisation, model-directory helpers and the small pure functions used by the
split backend modules. This file contains no request routing and no class methods;
it is imported by ``main`` and by every backend mixin.

[ZH]
Reader 版后端共享支撑层：协议 I/O、崩溃处理、日志净化、模型目录辅助函数，以及拆分后的
后端模块共用的小型纯函数。本文件不含请求路由，也不含任何类方法；由 ``main`` 与各后端
mixin 导入使用。
"""
__all__ = [
    "_cfg",
    "_CH_ANSWER",
    "_CH_HEARTBEAT",
    "_CH_REASONING",
    "_CH_RESET",
    "_context_ceiling",
    "_crash",
    "_crash_log_path",
    "_emit_host_progress",
    "_excepthook",
    "_format_bytes",
    "_HIGH_FREQUENCY_LOG_RE",
    "_host_progress_sink",
    "_i18n",
    "_io_lock",
    "_is_high_frequency_log_text",
    "_is_operational_log_line",
    "_is_pyinstaller_exit_flush",
    "_LONG_TEXT_FULL_INJECT_RATIO",
    "_OPERATIONAL_LOG_RE",
    "_override_blocking_error",
    "_preflight_context_error",
    "_preset_list",
    "_profile_params_text",
    "_profile_quant_text",
    "_recommendation",
    "_sanitize_protocol_event",
    "_scrub_operational_log_lines",
    "_split_reasoning",
    "_stdout_closed",
    "_summary_failure_is_transient",
    "_suppress_stdout",
    "_t",
    "_task_template_list",
    "_write",
    "Any",
    "APP_NAME",
    "APP_VERSION",
    "Callable",
    "check_model",
    "CompactEvent",
    "CompactionConfig",
    "CompactLog",
    "Config",
    "contextlib",
    "ConversationManager",
    "cuda_bootstrap",
    "Dict",
    "DocumentJobManager",
    "DocumentSummarizer",
    "emoji_mod",
    "EVENT_SKIP",
    "EVENT_SUMMARY",
    "faulthandler",
    "gc",
    "hardware_profiler",
    "inference_tuner",
    "InferenceConfig",
    "install_crash_handlers",
    "is_parse_error",
    "Iterator",
    "json",
    "LazyLLMEngine",
    "List",
    "LLMEngine",
    "localize_error",
    "log",
    "log_user",
    "logging",
    "MAX_DYNAMIC_CONTEXT",
    "ModelMissing",
    "NoModelError",
    "Optional",
    "os",
    "parse_file_with_wps",
    "platform_support",
    "program_dir",
    "queue",
    "re",
    "REPEAT_INTENT",
    "replay",
    "resolve_context_window",
    "shutil",
    "state_dir",
    "STATUS_CANCELLED",
    "StreamStats",
    "STUDIO",
    "subprocess",
    "sys",
    "threading",
    "time",
    "user_data_root",
    "uuid",
]


import contextlib
import gc
import json
import logging
import os
import queue
# [EN] Tasks 1/15 (self-test fix-1): ``_store_large_file`` always used ``re.sub`` to sanitize
# [EN] file names, but this module never imported ``re`` -- the call therefore raised
# [EN] NameError, which its own ``except Exception`` swallowed with only a warning. As a
# [EN] result the large-file spill feature never took effect: files above
# [EN] ``disk_threshold_chars`` stayed in the session in full, the ``file_cache`` directory
# [EN] was never created, and the spill pointer written into the context referenced a
# [EN] non-existent directory.
# [ZH] 相关设计/相关设计（自测问题修复-1）：`_store_large_file` 一直使用 `re.sub` 清洗文件名，
# [ZH] 但本模块从未 import re —— 该调用必然抛出 NameError，又被自身的
# [ZH] `except Exception` 捕获、只留一行 warning，因此"大文件正文落盘"功能
# [ZH] 自始至终没有生效：超过 disk_threshold_chars 的文件仍整篇留在会话中，
# [ZH] file_cache 目录也从未被创建（上下文中给出的落盘指针指向不存在的目录）。
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional

# [EN] v0.2.6 item 23: preload the local llama.cpp / CUDA DLLs before any module
# [EN] below can import ``llama_cpp``. Windows resolves native dependencies through
# [EN] the process DLL search path, not through an ``RTLD_GLOBAL`` symbol table, so
# [EN] ``ggml-cuda.dll`` must be loaded in dependency order (and the ``nvidia/*/bin``
# [EN] directories registered) *before* ``llama.dll`` is touched. This call is placed
# [EN] ahead of every backend import on purpose: it is the only position that is
# [EN] guaranteed to run before the lazy ``from llama_cpp import Llama`` statements.
# [ZH] v0.2.6 第 23 项：在下方任何模块导入 ``llama_cpp`` 之前预加载本地 llama.cpp /
# [ZH] CUDA DLL。Windows 通过进程 DLL 搜索路径解析原生依赖，而不是 ``RTLD_GLOBAL``
# [ZH] 全局符号表，因此必须在触碰 ``llama.dll`` **之前** 按依赖顺序加载
# [ZH] ``ggml-cuda.dll`` 并注册 ``nvidia/*/bin`` 目录。本调用刻意放在所有后端导入
# [ZH] 之前：只有这个位置才能保证早于各模块内惰性的 ``from llama_cpp import Llama``。
import cuda_bootstrap

cuda_bootstrap.ensure()

from reasoning import ANSWER as _CH_ANSWER
from reasoning import REASONING as _CH_REASONING
from reasoning import RESET as _CH_RESET
from reasoning import split_text as _split_reasoning
from version import APP_NAME, APP_VERSION, STUDIO
# [EN] Platform detection and capability boundaries (scope tasks 3 / 5): the decision is
# [EN] made here only, and every other module queries it for "does this platform support
# [EN] this capability", so the checks do not scatter and the frontend stays consistent.
# [ZH] 平台探测与能力边界（设计规范 相关设计 / 相关设计）：只在本模块内判断，
# [ZH] 其余模块一律通过它查询"本平台是否具备该能力"，避免判断分散、前后端不一致。
import platform_support
# [EN] The protocol runs over stdin/stdout and carries Chinese text and emoji. On some
# [EN] machines (English locale) Python defaults to cp1252, and writing Chinese raises
# [EN] UnicodeEncodeError, which terminates the process ("backend exited"). Therefore UTF-8
# [EN] is enforced here, with errors="replace" as a fallback, so output never fails.
# [ZH] 协议走 stdin/stdout，且内容含中文/emoji。某些机器（英文 locale）下 Python
# [ZH] 默认使用 cp1252 编码，写中文会抛 UnicodeEncodeError 导致进程终止（"后端进程已退出"）。
# [ZH] 因此统一强制 UTF-8，并以 errors="replace" 兜底，保证任何机器都能正常输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _stream


@contextlib.contextmanager
def _suppress_stdout():
    """
    [EN]
    Temporarily redirect ``sys.stdout`` to devnull to suppress diagnostic output that
    third-party libraries print directly to stdout.

    [ZH]
    临时把 sys.stdout 指向 devnull，屏蔽第三方库直接 print 到 stdout 的诊断信息。
    """
    saved = sys.stdout
    sink = open(os.devnull, "w", encoding="utf-8", errors="replace")
    sys.stdout = sink
    try:
        yield
    finally:
        sys.stdout = saved
        sink.close()


with _suppress_stdout():
    from config import Config
    # [EN] Reader purity: the Hugging Face endpoint environment variable is no longer
    # [EN] set at start-up. Nothing in this backend reaches the network, so no mirror
    # [EN] has to be configured before importing anything.
    # [ZH] Reader 纯净性：启动阶段不再设置 Hugging Face 端点环境变量。本后端没有任何
    # [ZH] 联网路径，因此无需在导入前配置镜像。
    _cfg = Config.load()
    from compact_log import EVENT_SKIP, EVENT_SUMMARY, CompactEvent, CompactLog, replay
    from config import CompactionConfig
    # [EN] Task 1 (self-test fix-1): FILE_INTENT is no longer imported. It used to gate
    # [EN] whether the file body was injected into the context; that gate has been removed
    # [EN] (see the include_file explanation in _stream_chat).
    # [ZH] 相关设计（自测问题修复-1）：不再导入 FILE_INTENT —— 它曾作为"是否把文件正文
    # [ZH] 注入上下文"的开关，现已取消（详见 _stream_chat 中 include_file 的说明）。
    from conversation import ConversationManager, REPEAT_INTENT
    from document_jobs import STATUS_CANCELLED, DocumentJobManager
    from file_parser import is_parse_error, localize_error, parse_file_with_wps
    from gguf_meta import format_bytes as _format_bytes
    from gguf_meta import resolve_context_window
    import hardware_profiler
    from inference_config import InferenceConfig
    import inference_tuner
    from llm_engine import (LazyLLMEngine, LLMEngine, MAX_DYNAMIC_CONTEXT,
                            NoModelError, StreamStats)
    # [EN] Heartbeat channel marker (task 5): the engine emits it during prefill, see
    # [EN] llm_engine._iter_with_heartbeat.
    # [ZH] 心跳通道标记（相关设计）：引擎在 prefill 阶段产出，见 llm_engine._iter_with_heartbeat。
    from llm_engine import HEARTBEAT as _CH_HEARTBEAT
    from summarizer import DocumentSummarizer

# [EN] Multilingual support (task 3): must be imported after Config.load so that i18n
# [EN] does not create an import cycle by depending on config in reverse.
# [ZH] 多语言（相关设计）：必须在 Config.load 之后导入，避免 i18n 反向依赖 config 时形成循环导入。
from i18n import get as _i18n
from i18n import log_user
from i18n import t as _t

# [EN] Emoji fallback (fix 6) and directory layout (fix 7).
# [ZH] Emoji 降级（兼容性修正）与目录布局（兼容性修正）。
import emoji as emoji_mod
from config import program_dir, program_root as user_data_root, state_dir

# [EN] Reader: the open-platform layer (local HTTP API and external provider routing)
# [EN] is physically removed. No module imported here opens an outbound connection.
# [ZH] Reader：开放平台层（本地 HTTP API 与外部 Provider 路由）已物理删除。此处导入的
# [ZH] 任何模块都不会发起对外连接。

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("EchoDesk")

# [EN] ---------- Crash log ----------
# [ZH] ---------- 崩溃日志 ----------
# [EN] The process can terminate from native code crashes (SIGILL/SIGSEGV in llama.cpp) with
# [EN] no Python traceback. The crash reason is written to backend_crash.log so it can be
# [EN] diagnosed on another machine.
# [EN]
# [EN] These are **process-level** side effects and must never run at import time. This module
# [EN] is imported by nearly every test and tool script under ``tools/``; unconditionally
# [EN] assigning ``sys.excepthook = _excepthook`` made every uncaught exception from any
# [EN] script land in backend_crash.log. In practice the file filled with tracebacks from
# [EN] test_reasoning / test_retrieval / test_search, drowning out real backend crashes and
# [EN] making it look as if the backend crashed again. The hook is now installed only by the
# [EN] program entry point.
# [ZH] 进程可能因原生代码崩溃（llama.cpp 的 SIGILL/SIGSEGV）等原因在没有 Python 回溯的
# [ZH] 情况下直接退出，此时把崩溃原因写入 backend_crash.log，便于在其他机器上排查。
# [ZH]
# [ZH] 这些是**进程级**副作用，绝不能在 import 时执行。本模块会被 `tools/` 下几乎所有
# [ZH] 测试与工具脚本 import；原先无条件执行的 `sys.excepthook = _excepthook` 会让
# [ZH] **任何脚本**的未捕获异常都写进 backend_crash.log —— 验证该文件混入了
# [ZH] test_reasoning / test_retrieval / test_search 的异常回溯，真正的后端崩溃反而被
# [ZH] 这些噪声掩盖。现在改为只由程序入口显式安装。
import faulthandler

_crash = None


def _crash_log_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.getcwd()
    return os.path.join(base, "backend_crash.log")


def _is_pyinstaller_exit_flush(tb) -> bool:
    """
    [EN]
    Recognise PyInstaller's own exit-time stream flush.

    Why this exists (measured on the Reader release tree, 2026-11-15): the PyInstaller
    bootloader runs two one-liners after the entry script returns —
    ``import sys; sys.stdout.flush(); ...`` and the ``stderr`` twin — compiled with the
    filename ``<string>``. When the parent process has already closed the child's pipes
    (the front end stops the backend by cancelling the stdout pump and then killing the
    process tree), that flush raises ``OSError: [Errno 22] Invalid argument``. It is
    **not application code**: nothing in ``backend/`` runs at that point. Recording it in
    ``backend_crash.log`` makes a normal shutdown look like a backend crash and makes the
    release gate non-deterministic (same build, same probe: sometimes one entry,
    sometimes none).

    The condition is deliberately narrow: the frame must be the bootloader's ``<string>``
    module whose code loads ``sys`` and calls ``flush`` on ``stdout``/``stderr``.

    [ZH]
    识别 PyInstaller 自身的"退出时刷新标准流"。

    为什么需要（2026-11-15 在 Reader 发布树上实测）：PyInstaller bootloader 在入口脚本
    返回后会执行两条一行式代码 —— ``import sys; sys.stdout.flush(); ...`` 及其 stderr
    版本，文件名为 ``<string>``。当父进程已经关闭子进程管道时（前端停止后端的方式是
    先取消 stdout 读取泵、再结束进程树），这次 flush 会抛出
    ``OSError: [Errno 22] Invalid argument``。它**不是应用代码**：那一刻 ``backend/`` 里
    没有任何代码在跑。把它记进 ``backend_crash.log`` 会让一次正常关闭看起来像后端崩溃，
    并让发布门禁变得不确定（同一个构建、同一个探针：有时一条、有时没有）。

    判定刻意收得很窄：必须是 bootloader 的 ``<string>`` 模块，且字节码里引用了 ``sys``、
    对 ``stdout``/``stderr`` 调用了 ``flush``。
    """
    if tb is None:
        return False
    code = tb.tb_frame.f_code
    if code.co_filename != "<string>" or code.co_name != "<module>":
        return False
    names = code.co_names
    return ("sys" in names and "flush" in names
            and ("stdout" in names or "stderr" in names))


def _excepthook(etype, value, tb):
    """
    [EN]
    Also write uncaught Python exceptions to the crash log so that silent termination does
    not occur.

    [ZH]
    未捕获的 Python 异常也写入崩溃日志，避免进程静默退出。
    """
    import traceback
    if isinstance(value, OSError) and _is_pyinstaller_exit_flush(tb):
        # [EN] Parent already closed our pipes: expected during shutdown, never a crash.
        # [ZH] 父进程已关闭我们的管道：关闭阶段属预期行为，绝不是崩溃。
        log.debug("退出时刷新标准流失败（父进程已关闭管道），不记入崩溃日志", exc_info=False)
        return
    try:
        with open(_crash_log_path(), "a", encoding="utf-8") as f:
            f.write("\n==== 未捕获异常 ====\n")
            traceback.print_exception(etype, value, tb, file=f)
    except Exception:
        pass
    traceback.print_exception(etype, value, tb)


def install_crash_handlers() -> None:
    """
    [EN]
    Install the process-level crash/exception hooks.

    **Must only be called by the program entry point (``__main__``)**; see the note at the
    top of this section. It is a function rather than module-level code because the packaged
    backend entry point is this file (``EchoBackend.spec``'s ``Analysis([... "main.py"])``),
    so reaching ``__main__`` installs the hooks exactly as before.

    [ZH]
    安装进程级崩溃/异常钩子。

    **只应由程序入口（``__main__``）调用**，理由见本节顶部说明。
    之所以做成函数而不是留在模块级：打包后的后端入口就是本文件
    （``EchoBackend.spec`` 的 ``Analysis([... "main.py"])``），
    走到 ``__main__`` 时会照常安装，行为不变。
    """
    global _crash
    try:
        _crash = open(_crash_log_path(), "a", encoding="utf-8")
        faulthandler.enable(_crash)
        log.info("已启用崩溃日志: %s", _crash_log_path())
    except Exception:
        faulthandler.enable()
        _crash = None
    sys.excepthook = _excepthook

# [EN] stdout write lock: the main loop and streaming background threads share stdout.
# [ZH] stdout 写锁：主循环与流式后台线程共用 stdout。
_io_lock = threading.Lock()


def _context_ceiling(config: Any, inference: Any) -> int:
    """
    [EN]
    Upper bound handed to the hardware planner.

    Two independent limits meet here and the **smaller** one wins: the user's
    configured window (``config.context_length``) and the hard ceiling of the
    inference configuration file (``inference_config.json: n_ctx_max``). Keeping the
    intersection explicit is what makes the bottom layer of the dynamic-context
    contract predictable: no document can ever ask the engine for more than either
    limit.

    [ZH]
    交给硬件规划器的上限。

    此处汇合两个彼此独立的限制，取**较小者**：用户在 ``config.context_length`` 中
    配置的窗口，以及推理配置文件（``inference_config.json: n_ctx_max``）中的硬上限。
    把这个交集写清楚，动态上下文契约的底层才是可预测的：任何文档都不可能向引擎申请
    超过这两个限制中任一个的窗口。
    """
    try:
        configured = int(getattr(config, "context_length", 0) or 0)
    except (TypeError, ValueError):
        configured = 0
    try:
        hard = int(getattr(inference, "n_ctx_max", 0) or 0)
    except (TypeError, ValueError):
        hard = 0
    candidates = [value for value in (configured, hard) if value > 0]
    return min(candidates) if candidates else 0


def check_model(model_name: str, local_dir: str = "./models") -> Optional[str]:
    """
    [EN]
    Resolve a configured model name to an absolute path on disk.

    Reader: this helper replaces the former download-backed implementation. It is
    deliberately limited to local lookups -- it creates the model directory so the
    guidance text can name a real path, then returns the exact match, or the single
    ``*.gguf`` file present when the configured name is absent (tolerating dots and
    hyphens that users rename). An empty string means "no model available".

    [ZH]
    把配置中的模型名解析为磁盘上的绝对路径。

    Reader：本函数取代原先依赖下载模块的实现，**只做本地查找** —— 先创建模型目录，
    使引导文案能给出真实路径；随后返回精确匹配路径，或在配置名缺失时返回目录下唯一的
    ``*.gguf`` 文件（兼容用户改名时点号与连字符的差异）。返回空串表示"没有可用模型"。
    """
    name = str(model_name or "").strip()
    directory = str(local_dir or "./models")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return ""
    if name:
        exact = os.path.join(directory, name)
        if os.path.isfile(exact):
            return os.path.abspath(exact)
    try:
        entries = sorted(
            entry for entry in os.listdir(directory) if entry.lower().endswith(".gguf")
        )
    except OSError:
        return ""
    if name:
        for entry in entries:
            print(f"ℹ️ {name} not found, using {entry}", file=sys.stderr)
            return os.path.abspath(os.path.join(directory, entry))
        return ""
    if len(entries) == 1:
        return os.path.abspath(os.path.join(directory, entries[0]))
    return ""


def _preset_list() -> List[Dict[str, Any]]:
    """
    [EN]
    Build the parameter-preset list (fix 4, direction 6).

    [ZH]
    生成参数预设清单（修复四 方向六）。
    """
    from model_profile import preset_menu

    return preset_menu()


def _task_template_list() -> List[Dict[str, Any]]:
    """
    [EN]
    Task-oriented prompt template list (fix 4, direction 1).

    [ZH]
    任务型提示词模板清单（修复四 方向一）。
    """
    from prompts import public_list

    return public_list()


def _recommendation() -> Dict[str, Any]:
    """
    [EN]
    Recommend a model size and generation parameters for the local hardware (fix 4,
    direction 6).

    [ZH]
    按本机硬件推荐模型规模与生成参数（修复四 方向六）。
    """
    from model_profile import _read_machine_memory, recommend_config

    ram, cores = _read_machine_memory()
    return recommend_config(ram, cores)


#: [EN] Long-text budget routing (v0.2.6 item 17). A document whose token count
#: is below this fraction of the user-selected context window is injected in full;
#: segmented summarization is attempted only above it, so short and medium files
#: keep 100% of their facts. The threshold is fixed at 70% by the task book and
#: must not be derived from the model's advertised window.
#: [ZH] 长文本预算路由（v0.2.6 第 17 项）。token 数低于用户所选上下文窗口该比例的
#: 文档直接全量注入；只有超出该比例才尝试分段摘要，因此短、中等文件的事实 100%
#: 保留。阈值按任务书固定为 70%，不得改用模型标称窗口来推导。
_LONG_TEXT_FULL_INJECT_RATIO = 0.70


def _summary_failure_is_transient(result: Any) -> bool:
    """
    [EN]
    Whether a failed summary may succeed on a retry because the model was busy.
    A concurrency-lock failure is not evidence that the file is too large for the
    window, so the "increase the context / upload in batches" guidance must not
    replace the retry path for it (v0.2.6 item 17).

    ``result`` may be a summary result object or the error string itself.
    [ZH]
    判断一次失败的摘要是否属于“模型忙、重试可成功”的暂时故障。并发锁失败并不能
    证明文件超出窗口，因此不得用“调大上下文 / 分批次上传”的提示取代其重试路径
    （v0.2.6 第 17 项）。

    ``result`` 可以是摘要结果对象，也可以直接是错误字符串。
    """
    text = (result if isinstance(result, str)
            else str(getattr(result, "error", "") or "")).lower()
    return any(marker in text for marker in
               ("lock", "busy", "concurrent", "in use", "another request"))


def _override_blocking_error(plan) -> str:
    """
    [EN]
    Return a human-readable error when an explicit override cannot possibly work
    on this machine, or an empty string when the plan may proceed.

    The check is deliberately performed here instead of relying on llama.cpp:
    a CPU-only build may silently ignore ``n_gpu_layers`` rather than raising, so
    "request 99 GPU layers on an integrated-GPU machine" would otherwise report
    success while nothing was offloaded. The acceptance criterion requires a
    clear, reproducible failure, so the backend owns that decision.

    [ZH]
    当显式覆盖在本机上必然无法工作时返回可读错误；规划可以继续时返回空字符串。

    该检查刻意放在这里，而不是依赖 llama.cpp：纯 CPU 构建可能**静默忽略**
    ``n_gpu_layers`` 而不是抛错，于是"集显机器上请求 99 层 GPU 卸载"会变成
    "报告成功、实际什么都没卸载"。验收标准要求清晰且可复现的失败，因此由后端
    自己做出这个判断。
    """
    if not bool(getattr(plan, "allow_override", False)):
        return ""
    layers = int(getattr(plan, "n_gpu_layers", 0) or 0)
    if layers != 0 and not bool(getattr(plan, "gpu_available", False)):
        return _t("inference.error_no_accelerator", layers=layers)
    return ""


def _preflight_context_error(plan) -> str:
    """
    [EN]
    Reject an obviously impossible context allocation before llama.cpp is
    asked to reserve it. A 999999-token window needs more than 100 GiB of KV
    cache for a small model; attempting it drives the host into swap for a
    minute and can exhaust memory on the build machine. The planner still
    adopts the user's value (the override contract is unchanged); this is the
    load-time feasibility check, and it returns an explicit, actionable error.

    [ZH]
    在要求 llama.cpp 预留之前拒绝明显不可能完成的上下文分配。对一个小模型来说，
    999999 token 窗口需要超过 100 GiB 的 KV Cache；真正尝试会让宿主进入交换区长达
    一分钟，并可能耗尽构建机内存。规划器仍原样采纳用户值（覆盖契约不变）；这里是
    加载阶段的可行性检查，返回明确且可操作的错误。
    """
    kv = int(getattr(plan, "kv_cache_bytes", 0) or 0)
    if kv <= 0:
        return ""
    try:
        profile = hardware_profiler.probe()
        available = int(profile.ram_available_bytes or 0)
    except Exception:  # noqa: BLE001 - the probe must never block loading
        return ""
    if available > 0 and kv > available:
        return _t(
            "inference.error_context_preflight",
            requested=int(getattr(plan, "n_ctx_max", 0) or 0),
            kv=round(kv / (1024 ** 3), 1),
            ram=round(available / (1024 ** 3), 1),
        )
    return ""


def _profile_params_text(name: str) -> str:
    """
    [EN]
    Infer the parameter-count text from the file name (0.6B / 1.7B); return an empty string
    when it cannot be determined.

    Reuses ``model_profile._fmt_params`` (the same formatting rules as the capability
    profile) so that the "how many B" and "how many decimals" logic is not duplicated here.

    [ZH]
    按文件名推断参数量文本（0.6B / 1.7B）；无法获取时返回空串。

    复用 `model_profile._fmt_params`（与能力画像同一套格式化规则），
    避免在这里再写一份"多少算 B、保留几位小数"的判断。
    """
    try:
        from model_profile import _fmt_params, params_from_name
        return _fmt_params(params_from_name(name))
    except Exception:  # noqa: BLE001
        # [EN] Display-only field: when it cannot be read, it is simply not shown.
        # [ZH] 只是展示字段，取不到就不显示。
        return ""


def _profile_quant_text(name: str) -> str:
    """
    [EN]
    Infer the quantization text from the file name (Q6_K / Q4_K_M); return an empty string
    when it cannot be determined.

    Reader: the regular expression lives here instead of in the removed model-library
    module, so a model scan can still label a file without importing anything that
    knows about repositories or downloads.

    [ZH]
    按文件名推断量化文本（Q6_K / Q4_K_M）；无法获取时返回空串。

    Reader：正则表达式随模型库模块一起移除后移到了这里，使模型扫描仍能标注文件，而
    不必导入任何"知道仓库与下载"的模块。
    """
    text = os.path.basename(str(name or ""))
    matches = re.findall(r"(?:IQ\d+_[A-Z0-9]+|Q\d+_K(?:_[A-Z0-9]+)?|Q\d+_[01]|BF16|F16|F32)",
                         text, re.IGNORECASE)
    return str(matches[-1]).upper() if matches else ""


_stdout_closed = False


#: [EN] Pattern of an operational log line that must never be rendered as chat
#: [EN] content. It accepts the formats emitted by ``logging.basicConfig``
#: [EN] (``2026-11-14 18:43:25,123 INFO ...``) as well as bracketed prefixes and
#: [EN] the ``INFO:`` form that some libraries use. The same expression is
#: [EN] mirrored in the Avalonia front end so either side blocks the line.
#: [ZH] 绝不能作为对话内容渲染的运行日志行模式。该表达式接受
#: [ZH] ``logging.basicConfig`` 输出的格式（``2026-11-14 18:43:25,123 INFO ...``）、
#: [ZH] 方括号前缀，以及部分库使用的 ``INFO:`` 形式。Avalonia 前端镜像了同一
#: [ZH] 表达式，因此任一侧都能拦截。
_OPERATIONAL_LOG_RE = re.compile(
    r"^\s*(?:"
    r"(?:\[?\d{4}[-/]\d{2}[-/]\d{2}.*?\]?\s*)?"
    r"(?:INFO|WARNING|ERROR|DEBUG|CRITICAL)\s*:"
    r"|\[\s*\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*\]"
    r"|\[\s*(?:INFO|WARNING|ERROR|DEBUG|CRITICAL)\s*\]"
    r"|\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+"
    r"(?:INFO|WARNING|ERROR|DEBUG|CRITICAL)\b"
    r"|\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r")",
    re.IGNORECASE,
)


def _is_operational_log_line(line: str) -> bool:
    """
    [EN] Return whether one line is an operational log line.
    [ZH] 判断单行是否为运行日志行。
    """
    return bool(str(line or "").strip()) and bool(_OPERATIONAL_LOG_RE.match(str(line)))


#: [EN] v0.2.6 item 21: high-frequency, purely engineering progress (segment merging,
#: [EN] chunked-summary progress, "[x/x] ... done"). Only these lines are still kept out of
#: [EN] the conversation. A meaningful system notice that merely carries a logging prefix
#: [EN] (for example "INFO 未加载模型") must be allowed through.
#: [ZH] v0.2.6 第 21 项：高频、纯工程性质进度（分段合并、分块摘要进度、
#: [ZH] "[x/x] ... done"）。只有这类行仍被挡在对话区之外。仅带日志前缀的有业务含义
#: [ZH] 系统提示（例如 "INFO 未加载模型"）必须放行。
_HIGH_FREQUENCY_LOG_RE = re.compile(
    r"(?:merging\s+segment|合并分段摘要"
    r"|(?:segment|chunk)\s+\d+\s*[/／]\s*\d+"
    r"|第\s*\d+\s*[/／]\s*\d+\s*段"
    r"|\[\s*\d+\s*[/／]\s*\d+\s*\]\s*.*(?:done|complete|完成))",
    re.IGNORECASE,
)


def _is_high_frequency_log_text(text: str) -> bool:
    """
    [EN] Return whether every non-empty line is high-frequency engineering progress.
    [ZH] 判断每一非空行是否都是高频工程进度。
    """
    saw_line = False
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        if not line.strip():
            continue
        saw_line = True
        if not _HIGH_FREQUENCY_LOG_RE.search(line):
            return False
    return saw_line


def _scrub_operational_log_lines(text: str) -> tuple:
    """
    [EN]
    Split text into kept content and diverted log lines.

    Filtering is per line so a message that mixes a real notice with a log line
    keeps the notice instead of being discarded wholesale.

    [ZH]
    把文本拆分为保留内容与被转移的日志行。

    按行过滤，因此真实提示与日志行混排时会保留提示，而不是整条丢弃。
    """
    kept: List[str] = []
    dropped: List[str] = []
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        if _is_operational_log_line(line):
            dropped.append(line.strip())
        else:
            kept.append(line)
    return "\n".join(kept).strip("\n"), dropped


def _sanitize_protocol_event(response: Dict[str, Any]):
    """
    [EN]
    Keep engineering log traffic out of the answer pane and route high-frequency debug
    progress to the status bar.

    v0.2.6 item 21 refined the policy:

    * ``delta`` (answer / reasoning): every operational log line is still diverted or
      dropped, because generated chat content must never contain log text.
    * ``system``: only **high-frequency** engineering progress (segment merging,
      "[x/x] ... done") is diverted. A meaningful notice that happens to carry a logging
      prefix (for example "INFO 未加载模型") is preserved and rendered as a system bubble,
      so the user can read, copy and screenshot it.

    [ZH]
    把工程日志挡在回答区之外，并把高频调试进度改投状态栏。

    v0.2.6 第 21 项细化了策略：

    * ``delta``（回答 / 思考）：仍然转移或丢弃所有运行日志行，因为生成的对话内容
      绝不能混入日志文本。
    * ``system``：只有**高频**工程进度（分段合并、"[x/x] ... done"）被转移。仅带日志
      前缀的有业务含义提示（例如 "INFO 未加载模型"）予以保留，并渲染为系统气泡，
      便于用户阅读、复制与截图。
    """
    if not isinstance(response, dict):
        return response
    event_type = response.get("type")
    if event_type not in ("delta", "system"):
        return response
    content = response.get("content")
    if not isinstance(content, str) or not content:
        return response
    # [EN] v0.2.6 item 21: a system event survives unless the whole body is debug progress.
    # [ZH] v0.2.6 第 21 项：系统事件只在整段正文都是调试进度时才被改投。
    if event_type == "system" and not _is_high_frequency_log_text(content):
        return response
    cleaned, dropped = _scrub_operational_log_lines(content)
    if not dropped:
        return response
    if event_type == "delta" and cleaned.strip():
        updated = dict(response)
        updated["content"] = cleaned
        return updated
    if cleaned.strip():
        updated = dict(response)
        updated["content"] = cleaned
        return updated
    if event_type == "delta" and response.get("channel") != _CH_ANSWER:
        return None
    # [EN] Keep the auxiliary fields (context, model_state, ...) that the caller
    # [EN] attached to the original event; only the type and body change.
    # [ZH] 保留调用方附加在原始事件上的辅助字段（context、model_state 等），
    # [ZH] 只改变类型与正文。
    redirected = dict(response)
    redirected["type"] = "progress"
    redirected["content"] = dropped[0][:500]
    redirected.pop("channel", None)
    redirected.pop("reset", None)
    return redirected


def _write(response: Dict[str, Any]) -> None:
    """
    [EN]
    Write one protocol line to stdout.

    A closed pipe means the front end has exited; the write is then abandoned
    instead of raising ``BrokenPipeError`` and terminating the backend. The
    failure is recorded once in the log, and every later write becomes a no-op.

    [ZH]
    向 stdout 写入一行协议数据。

    管道关闭意味着前端已退出；此时放弃写入，而不是抛出 ``BrokenPipeError``
    终止后端。失败仅记录一次日志，之后的写入均变为空操作。
    """
    global _stdout_closed
    if _stdout_closed:
        return
    # [EN] v0.2.6 item 21: chat deltas must never carry log text; system events are only
    # [EN] scrubbed when the whole body is high-frequency debug progress (see
    # [EN] `_sanitize_protocol_event`), so a meaningful "INFO 未加载模型" notice still
    # [EN] reaches the conversation.
    # [ZH] v0.2.6 第 21 项：对话 delta 绝不能携带日志文本；系统事件仅在整段正文都是高频
    # [ZH] 调试进度时才清洗（见 `_sanitize_protocol_event`），因此有业务含义的
    # [ZH] "INFO 未加载模型" 提示仍能进入对话区。
    response = _sanitize_protocol_event(response)
    if response is None:
        return
    try:
        with _io_lock:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except (BrokenPipeError, ValueError, OSError) as exc:
        _stdout_closed = True
        log.warning("stdout 管道已关闭，停止写入协议行: %s", exc)


# =====================================================================
# [EN] Host progress events (v0.2.2 batch 4)
# [ZH] 宿主进度事件（v0.2.2 第 4 批）
# =====================================================================
#
# [EN] Background: chunked-summary progress used to have two paths -- the `summary_progress`
# [EN] event on streaming requests, and `print(..., file=sys.stderr)` for the **non-streaming**
# [EN] `/file` command. The frontend could only render the latter as "one log line" and could
# [EN] not tell progress apart from a warning.
# [EN]
# [EN] This extension adds the stdout line type `{"type": "host_progress", "content": "..."}`,
# [EN] emitted by the backend **only while a non-streaming request is being handled**. It is
# [EN] the only protocol extension explicitly approved for this release, and it is subject to
# [EN] three self-imposed constraints so the protocol is not polluted:
# [EN]   1. **Only emitted while a request is in flight** (`_host_progress_sink` is not None);
# [EN]      not a single extra byte is written otherwise, so "unexpected JSON while idle"
# [EN]      cannot occur.
# [EN]   2. The frontend always "skips it on sight, never treats it as a response" (both
# [EN]      frontends updated their own ReadUntilJson), so it never displaces a real response
# [EN]      line.
# [EN]   3. Without a sink the behaviour is **entirely unchanged** (still writes to stderr),
# [EN]      so older frontends and direct backend runs are unaffected.
# [ZH] 背景：分段摘要的进度原本有两条路径 —— 流式请求中走 `summary_progress` 事件，
# [ZH] **非流式**的 `/file` 命令则退回 `print(..., file=sys.stderr)`。
# [ZH] 后者前端只能当作"一行日志"显示，无法区分是进度还是警告。
# [ZH]
# [ZH] 本扩展新增一种 stdout 行类型 `{"type": "host_progress", "content": "…"}`，
# [ZH] 仅在**非流式请求处理期间**由后端主动发出。为不污染协议，附加三条约束：
# [ZH]   1. **只在有请求在处理时发送**（`_host_progress_sink` 非 None）；
# [ZH]      请求之外不额外发送任何字节，因此不存在"空闲时收到意外 JSON"的情形。
# [ZH]   2. 前端一律"见到即跳过、不当作响应"（两个前端都修改了各自的 ReadUntilJson），
# [ZH]      因此它不会顶替真正的响应行。
# [ZH]   3. 没有 sink 时行为**完全不变**（照旧写 stderr），保证老前端与直接运行后端的场景不受影响。
_host_progress_sink: Optional[Callable[[str], None]] = None


def _emit_host_progress(text: str) -> None:
    """
    [EN]
    Write one host progress entry as a structured event (called only while a request is
    being handled).

    [ZH]
    把一条宿主进度写成结构化事件（只在请求处理期间被调用）。
    """
    if not text:
        return
    _write({"type": "host_progress", "content": text})


class ModelMissing(Exception):
    """
    [EN]
    Raised when the configured model file is missing.

    [ZH]
    本地缺少模型：携带仓库中最匹配的文件名与体积，交由前端征询用户后再下载。
    """

    def __init__(self, filename: str, size: Optional[int] = None, repo: str = "") -> None:
        self.filename = filename
        self.size = size
        self.repo = repo
        super().__init__(f"本地缺少模型 {filename}")

