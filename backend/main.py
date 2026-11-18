"""
[EN]
EchoDesk backend entry point. It communicates with the frontend over stdin/stdout
using a line-delimited JSON protocol.

Protocol:
  {"action": "chat", "message": "..."}         -> one response line {"type": "assistant|system", "content": ...}
  {"action": "chat_stream", "message": "..."}  -> multiple streamed lines: {"type":"delta",...} ... {"type":"done",...}
  {"action": "stop"}                           -> interrupts the current streaming generation
  {"action": "file", "path": "..."}            -> one response line
  {"action": "file_stream", "path": "..."}     -> streamed: summary_progress lines + done (task 4)
  {"action": "inference_tune_stream"}            -> live hardware benchmark: tune progress + done
  {"action": "model_scan"}                     -> rescans the model directory
  {"action": "model_load", "name": "..."}      -> hot-loads the named model without restarting the process

**Reasoning-content channel (v0.2.1 model reasoning separation, tasks 3/4)**:

For reasoning models, the thinking process and the final answer travel on two separate
channels; the answer pane carries the final answer only:

* ``{"type":"delta","channel":"reasoning","content":...}`` -- a reasoning-content increment;
* ``{"type":"delta","channel":"answer","content":...}`` -- a final-answer increment;
* ``{"type":"delta","channel":"answer","reset":true}`` -- **invalidates the answer pane**: the text
  previously emitted as an "answer" is in fact reasoning content (the model's opening tag
  lives in the prompt and only the closing tag is observable, e.g. DeepSeek-R1); the
  frontend must move the accumulated answer text into the reasoning pane;
* the ``done`` line additionally carries ``reasoning`` (the full reasoning text) and
  ``reasoning_unclosed: true`` (the tag never closed, which usually means the token budget
  was exhausted by reasoning and the answer has not started yet).

**Streaming heartbeat and runtime metrics (v0.2.1 known-issues optimization, tasks 5/7)**:

* ``{"type":"progress","channel":"heartbeat","stats":{...},"waiting_first_token":bool}``
  -- **heartbeat**: the model is still computing but has produced no new token yet
  (the prefill phase can last tens of seconds). The frontend uses it to display
  "processing input / waiting for the first token" instead of looking frozen.
  Heartbeats are produced by the engine's reader thread (see
  ``llm_engine._iter_with_heartbeat``); the interval is taken from
  ``config.stream_heartbeat_seconds`` (default 1.5 seconds).
* the ``done`` line additionally carries ``stats``: runtime metrics measured by the backend
  (first-token latency, output token count, tokens/s, total elapsed time, whether the values
  are estimates), for display in the status area. Field definitions: ``llm_engine.StreamStats``.

Delta lines without a ``channel`` field keep the legacy semantics (progress/summary and
other non-conversational text); the frontend handles them unchanged.

**No-model-ready state (v0.2.1 general optimization, task 1)**:
If no local model is present at startup, the backend **neither blocks nor exits**; it keeps
running in a "no model ready" state: it initializes a Backend whose ``llm`` is left empty and
serves the main loop as usual. Behaviour boundaries are defined by :meth:`Backend.model_state`:
reasoning requests return guidance text (without crashing), while the model-independent
features -- file parsing, settings read/write, session-history inspection -- **remain fully
available**. After the user places a ``.gguf`` file into the model directory manually, the
backend **hot-loads it in place** (same engine object, no process restart).

Reader scope: the only actions this module serves are the file-upload, model-load and
text-generation paths plus read-only state queries. Web search, the model library, the
plugin marketplace, update checking, the local HTTP API and external provider routing are
**physically removed**, so no code path here can reach the network.

Startup sequence: ``need_download`` (**non-blocking**; reports only which model is missing,
with ``blocking: false``) -> ``ready`` (with ``model_ready: false`` and ``model_missing``
details). The former "exit when the user declines" behaviour is removed: exit is triggered
only by the user (the frontend sends ``exit``).

[ZH]
EchoDesk 后端主程序：与前端通过 stdin/stdout 以 JSON 行协议通信。

协议：
  {"action": "chat", "message": "..."}         -> 一行响应 {"type": "assistant|system", "content": ...}
  {"action": "chat_stream", "message": "..."}  -> 多行流式响应：{"type":"delta",...}... {"type":"done",...}
  {"action": "stop"}                           -> 中断当前流式生成
  {"action": "file", "path": "..."}            -> 一行响应
  {"action": "file_stream", "path": "..."}     -> 流式：summary_progress 进度行 + done（任务四）
  {"action": "inference_tune_stream"}            -> 实时硬件基准测试：调优进度 + done
  {"action": "model_scan"}                     -> 重新扫描模型文件夹
  {"action": "model_load", "name": "..."}      -> 热加载指定模型（不重启进程）

**思考内容通道（v0.2.1 模型思考内容分离 任务三/四）**：

推理模型的思考过程与最终回答走两条通道，回答区只承载最终答案：

* ``{"type":"delta","channel":"reasoning","content":...}`` —— 思考内容增量；
* ``{"type":"delta","channel":"answer","content":...}`` —— 最终回答增量；
* ``{"type":"delta","channel":"answer","reset":true}`` —— **作废回答区**：此前发出的
  "回答"实为思考内容（模型的起始标签写在提示词内，只有结束标签可见，如
  DeepSeek-R1），前端应将已累积的回答文本移入思考区；
* ``done`` 行额外携带 ``reasoning``（思考全文）与 ``reasoning_unclosed: true``
  （标签未闭合，通常意味着 token 预算已被思考耗尽、答案尚未开始）。

**流式心跳与运行指标（v0.2.1 优化已知问题-2 任务五/七）**：

* ``{"type":"progress","channel":"heartbeat","stats":{...},"waiting_first_token":bool}``
  —— **心跳**：模型仍在计算但暂时没有新 token（prefill 阶段可能持续数十秒）。
  前端据此显示"正在处理输入 / 等待首 token"，避免界面呈现为停滞状态。
  心跳由引擎的读者线程产生（见 ``llm_engine._iter_with_heartbeat``），
  间隔取自 ``config.stream_heartbeat_seconds``（默认 1.5 秒）。
* ``done`` 行额外携带 ``stats``：后端实测的运行指标（首 token 延迟、输出 token 数、
  token/s、总耗时、是否为估算值），供状态区显示。字段定义见 ``llm_engine.StreamStats``。

没有 ``channel`` 字段的 delta 行沿用既有语义（进度/摘要等非对话文本），前端按原样处理。

**无模型就绪（v0.2.1 综合优化 任务一）**：
启动时若本地缺少模型，后端**不再阻塞等待、也不再退出**，而是以"无模型就绪"状态继续运行：
初始化一个 ``llm`` 留空的 Backend，主循环照常提供服务。行为边界见 :meth:`Backend.model_state`：
推理类请求返回引导文案（不崩溃），而文件解析 / 技能市场 / 设置读写 / 模型市场搜索与下载 /
会话历史查看等不依赖模型的功能**照常可用**。用户在设置中下载模型，或把 ``.gguf`` 手动放入
模型文件夹后，后端**原地热加载**（同一个引擎对象，无需重启进程）。

启动顺序：``need_download``（**非阻塞**，仅告知缺少哪个模型，带 ``blocking: false``）→
``ready``（带 ``model_ready: false`` 与 ``model_missing`` 详情）。旧行为"用户拒绝即退出"
已移除：退出仅由用户主动触发（前端发 ``exit``）。
"""
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


class Backend:
    """
    [EN]
    Backend service: responsible for model loading, session restoration, warm-up, and
    request dispatch.

    [ZH]
    后端服务：负责模型加载、会话恢复、预热与请求分发。
    """

    def __init__(self, config: Config, model_path: str = "") -> None:
        self.config = config
        # [EN] Engine: a real engine when a model is supplied, otherwise an "empty but
        # [EN] loadable in place" engine (task 1). The **same object is deliberately kept**
        # [EN] (see the notes in llm_engine.LazyLLMEngine): after a hot load the references
        # [EN] held by command_handler / summarizer need no rewiring, so the silent failure
        # [EN] mode "loaded successfully but still using the old object" cannot occur.
        # [ZH] 引擎：提供模型即为真引擎；未提供则是"留空、可原地加载"的引擎（相关设计）。
        # [ZH] 刻意**保持同一个对象**（见 llm_engine.LazyLLMEngine 的说明）：热加载后
        # [ZH] command_handler / summarizer 中的引用无需重新接线，也就不会出现
        # [ZH] "加载成功但仍在使用旧对象"这类静默错误。
        # [EN] v0.2.4 task 3: an explicit override may make the configured
        # [EN] parameters unloadable (a 999999-token window, or GPU layers on a
        # [EN] machine without an accelerator). The backend must start anyway and
        # [EN] enter the no-model state, so the user can open Settings and change
        # [EN] the value back; a raised exception here used to terminate the
        # [EN] process before the first `ready` handshake.
        # [ZH] v0.2.4 任务-3：显式覆盖可能让已配置的参数无法加载（999999 token 窗口，
        # [ZH] 或没有加速器却要求 GPU 层数）。后端必须照常启动并进入无模型状态，使用户
        # [ZH] 能打开设置改回取值；此前这里抛异常会在首次 `ready` 握手前终止进程。
        self._startup_inference_error = ""
        if model_path:
            try:
                self.llm = self._build_engine(config, model_path)
            except Exception as exc:  # noqa: BLE001 - startup must survive a bad override
                self._startup_inference_error = f"{type(exc).__name__}: {exc}"
                log.exception("模型加载失败（已进入无模型状态，可在设置页改回推荐值）")
                self.llm = self._build_engine(config, "")
        else:
            self.llm = self._build_engine(config, "")
        self.conv = ConversationManager(config.max_history, config.default_mode)
        # [EN] The two switches controlling reasoning content written to history (reasoning
        # [EN] separation in an earlier release): whether to persist it and how many
        # [EN] characters to keep per entry. They live here rather than inside
        # [EN] ConversationManager because these values come from config.json and the
        # [EN] conversation manager itself does not depend on the config module.
        # [ZH] 思考内容写入历史的两个开关（此前版本 模型思考内容分离）：是否持久化、
        # [ZH] 每条保留多少字符。放在这里而不是 ConversationManager 内部，是因为
        # [ZH] 这两个值来自 config.json，而会话管理器本身不依赖配置模块。
        _reasoning_cfg = getattr(config, "reasoning", None)
        self.conv.persist_reasoning = bool(getattr(_reasoning_cfg, "persist", True))
        self.conv.reasoning_max_chars = int(getattr(_reasoning_cfg, "history_max_chars", 8000) or 0)
        self.summarizer = DocumentSummarizer(
            self.llm, config.summarize, engine_factory=self._make_summary_engine,
        )
        # [EN] Serial background queue for document parsing and chunked
        # [EN] summarization (v0.2.6 task 2.1). A single worker keeps model
        # [EN] inference serialized and lets the request dispatcher answer
        # [EN] immediately.
        # [ZH] 文档解析与分段摘要的串行后台队列（v0.2.6 任务 2.1）。单工作线程
        # [ZH] 保证模型推理串行，并使请求分发层可以立即应答。
        self._conv_lock = threading.RLock()
        self._document_jobs = DocumentJobManager(self._run_document_job)
        # [EN] Fix 4: chunked-summary cache (direction 8) and the wiring of the "manual
        # [EN] context compaction" callback.
        # [ZH] 兼容性修正：分段摘要缓存（方向八）与"手动压缩上下文"回调接线。
        self._summary_cache = None
        self._capability_cache = None
        self._capability_key = None
        self.stream_active = False
        self.stop_event = threading.Event()
        # [EN] Reader: one benchmark at a time. The native runtime is not re-entrant, so
        # [EN] a second tuning run while the first is loading models would corrupt it.
        # [ZH] Reader：同一时刻只允许一次基准测试。原生运行时不可重入，第二次调优若在
        # [ZH] 第一次加载模型期间启动会破坏共享状态。
        self._tuner_running = False
        # [EN] Reader: the inference-configuration file is the single source of truth for
        # [EN] every llama.cpp construction parameter (threads, batch, mlock, KV cache,
        # [EN] FlashAttention). It is read once here and re-read after every optimisation
        # [EN] run, so a tuned file takes effect on the next model load.
        # [ZH] Reader：推理配置文件是全部 llama.cpp 构建参数的唯一来源（线程、批大小、
        # [ZH] 内存锁定、KV Cache、FlashAttention）。此处读取一次，每次优化运行后重新
        # [ZH] 读取，因此调优后的文件在下次加载模型时生效。
        self.inference_config = InferenceConfig.load()
        # [EN] Compaction event log (task 9): created lazily, see compact_log().
        # [ZH] 压缩事件日志（相关设计）：惰性创建，见 compact_log()。
        self._compact_log = None
        # [EN] Total injected multi-file length: this value used to be set only in tests and
        # [EN] was never assigned on the production path. As a result
        # [EN] config.files.total_chars (default 60000) had no effect and injection was
        # [EN] unbounded -- a pre-existing defect found in the capability extension-1 audit.
        # [EN] It is assigned here.
        # [ZH] 多文件注入总长：此前该值只在测试中被设置过，生产路径从未赋值 ——
        # [ZH] 结果 config.files.total_chars（默认 60000）**实际上没有生效**，
        # [ZH] 注入是"不限长"的（后续版本 审计发现的既有缺陷）。此处补上。
        self.conv.files_total_chars = int(getattr(config.files, "total_chars", 0) or 0)

    def clear_summary_cache(self) -> int:
        """
    [EN]
    Clear the chunked-summary cache and return the number of entries removed (used by
    /clearcache and the action interface).

    [ZH]
    清空分段摘要缓存并返回清除的条目数（供 /clearcache 与动作接口使用）。
    """
        cache = self.summary_cache()
        return cache.clear() if cache is not None else 0

    # [EN] ---------- No-model-ready state and hot loading (general optimization in an
    # [EN] earlier release, design sections II and V, tasks 1/2/5) ----------
    # [ZH] ---------- 无模型就绪 / 热加载（此前版本 综合优化 相关设计、二、五） ----------

    @property
    def model_ready(self) -> bool:
        """
        [EN]
        Whether a usable model is currently available (a precondition for reasoning features).

        [ZH]
        当前是否有可用模型（推理类功能的前提）。
        """
        return bool(getattr(self.llm, "ready", True))

    def models_dir(self) -> str:
        """
        [EN]
        Actual path of the model directory (the guidance text must show the real path, not a
        placeholder).

        Reader: the path comes from ``config.model_hub.models_dir`` and is resolved
        against the writable state directory. The former model-library manager
        (which also reconciled a download registry) is removed, so this method is
        now the single place that answers "where do local .gguf files live".

        [ZH]
        模型文件夹的实际路径（引导文案中必须给出真实路径，不能使用占位符）。

        Reader：路径来自 ``config.model_hub.models_dir``，并相对可写状态目录解析。
        原先的模型库管理器（还负责核对下载登记表）已被删除，因此"本地 .gguf 放在哪里"
        现在由本方法统一回答。
        """
        try:
            value = str(getattr(getattr(self.config, "model_hub", None),
                               "models_dir", "models") or "models")
            if os.path.isabs(value):
                return value
            return os.path.abspath(os.path.join(state_dir(), value))
        except Exception:
            return os.path.abspath("models")

    def model_state(self) -> Dict[str, Any]:
        """
        [EN]
        Machine-readable model state, so the frontend can distinguish "backend ready" from
        "reasoning available" (task 1).

        When ``ready=false`` the frontend should keep the UI interactive, show the status bar
        as "no model", and use ``guide`` to direct the user to the model marketplace or to
        placing a model file manually.

        [ZH]
        机器可读的模型状态，供前端区分"后端已就绪"与"推理可用"（任务一）。

        ``ready=false`` 时前端应保持界面可交互、把状态栏显示为"无模型"，
        并按 ``guide`` 引导用户去模型市场或手动放置模型文件。
        """
        ready = self.model_ready
        return {
            "ready": ready,
            "model": os.path.basename(str(getattr(self.llm, "model_path", "") or "")),
            "models_dir": self.models_dir(),
            "n_ctx": int(getattr(self.llm, "n_ctx", 0) or 0),
            # [EN] Native and usable context are reported side by side so the
            # [EN] interface can state both facts instead of only the effective
            # [EN] value: a 32K-capable model that is limited to 4K by physical
            # [EN] memory is a different situation from an 8K model that is
            # [EN] already running at its full window.
            # [ZH] 原生上下文与可用上下文并列上报，使界面能够同时陈述两个事实，而不是
            # [ZH] 只给出生效值：一个原生支持 32K、但受物理内存限制而只能用 4K 的模型，
            # [ZH] 与一个已经跑满窗口的 8K 模型，是两种不同的状况。
            "native_context": int(getattr(self.llm, "n_ctx_train", 0) or 0),
            "usable_context": int(getattr(self.llm, "max_n_ctx", 0)
                                  or getattr(self.llm, "n_ctx", 0) or 0),
            "inference_profile": str(getattr(self.config, "inference_profile", "balanced")),
            "inference_override": bool(getattr(self.config, "inference_override", False)),
            "warnings": list(self._inference_warnings()),
            "startup_error": str(getattr(self, "_startup_inference_error", "") or ""),
            "context_limited_by": self._context_limited_by(),
            "context_limit_text": self._context_limit_text(),
            "guide": "" if ready else self.guide_text(),
        }

    def _inference_warnings(self) -> List[str]:
        """
        [EN]
        Return the localized warnings attached to the active inference plan.
        [ZH]
        返回当前推理规划携带的已本地化警告。
        """
        plan = getattr(self.llm, "inference_plan", None) or {}
        if not isinstance(plan, dict):
            return []
        return [str(item) for item in (plan.get("warnings") or []) if str(item)]

    def _context_limited_by(self) -> str:
        """
        [EN]
        Return the constraint that determined the planned context ceiling, or an
        empty string when no model has been planned yet.

        [ZH]
        返回决定规划上下文上限的约束；尚未规划任何模型时返回空串。
        """
        plan = getattr(self.llm, "inference_plan", None) or {}
        if not isinstance(plan, dict):
            return ""
        return str(plan.get("context_limited_by") or "")

    def _context_limit_text(self) -> str:
        """
        [EN]
        Produce the localized sentence that explains a reduced context window.

        The four cases are deliberately distinct: a native-window limit is a
        property of the model, a hardware limit is a property of this machine, a
        configuration limit was requested by the user, and a preset limit was
        chosen by the active inference preset. Collapsing them into one message
        would tell the user to change the wrong setting.

        [ZH]
        生成解释上下文窗口被收窄的本地化语句。

        四种情形刻意区分：原生窗口限制属于模型属性，硬件限制属于本机属性，配置限制
        由用户请求，预设限制由当前推理预设选择。把它们合并成一条消息会引导用户修改
        错误的设置。
        """
        if not self.model_ready:
            return ""
        reason = self._context_limited_by()
        plan = getattr(self.llm, "inference_plan", None) or {}
        usable = int(
            (plan.get("n_ctx_max") if isinstance(plan, dict) else 0)
            or getattr(self.llm, "max_n_ctx", 0)
            or getattr(self.llm, "n_ctx", 0)
            or 0
        )
        native = int(
            (plan.get("native_context") if isinstance(plan, dict) else 0)
            or getattr(self.llm, "n_ctx_train", 0)
            or 0
        )
        if reason == "native":
            return _t("sys.context_limit_native", ctx=native or usable)
        if reason == "hardware":
            return _t("sys.context_limit_hardware", ctx=usable)
        if reason == "config":
            return _t("sys.context_limit_config", ctx=usable)
        if reason == "preset":
            return _t("sys.context_limit_preset", ctx=usable)
        if reason == "override":
            return _t("sys.context_limit_override", ctx=usable)
        return ""

    def inference_summary(self) -> Dict[str, Any]:
        """
        [EN]
        Machine-readable snapshot of the active inference plan for the settings
        page and the status area. Every value is a number or an identifier; the
        only localized field is ``context_limit_text``, which the interface
        displays verbatim.

        [ZH]
        供设置页与状态区使用的推理规划机器可读快照。除 ``context_limit_text`` 之外
        的字段均为数值或标识符；该本地化字段由界面原样展示。
        """
        try:
            plan = getattr(self.llm, "inference_plan", None) or {}
            if not isinstance(plan, dict):
                plan = {}
            usable = int(
                getattr(self.llm, "max_n_ctx", 0) or getattr(self.llm, "n_ctx", 0) or 0
            )
            return {
                "profile": str(getattr(self.config, "inference_profile", "balanced")),
                "n_ctx": int(getattr(self.llm, "n_ctx", 0) or 0),
                "n_ctx_max": usable,
                "n_ctx_native": int(
                    plan.get("native_context")
                    or getattr(self.llm, "n_ctx_train", 0)
                    or 0
                ),
                "n_batch": int(
                    plan.get("n_batch") or getattr(self.llm, "n_batch", 0) or 0
                ),
                "n_threads": int(
                    plan.get("n_threads") or getattr(self.llm, "n_threads", 0) or 0
                ),
                "n_gpu_layers": int(
                    plan.get("n_gpu_layers") or getattr(self.llm, "n_gpu_layers", 0) or 0
                ),
                "kv_cache_type": str(
                    plan.get("kv_cache_type")
                    or getattr(self.llm, "kv_cache_type", "f16")
                ),
                "context_limited_by": self._context_limited_by(),
                "context_limit_text": self._context_limit_text(),
                "ready": bool(self.model_ready),
                "override": bool(plan.get("allow_override")
                                 or getattr(self.config, "inference_override", False)),
                "n_ctx_requested": int(plan.get("n_ctx_requested", 0) or 0),
                "n_gpu_layers_requested": int(
                    plan.get("n_gpu_layers_requested", 0) or 0),
                "n_batch_requested": int(plan.get("n_batch_requested", 0) or 0),
                "recommended_n_ctx": int(plan.get("recommended_n_ctx", 0) or 0),
                "recommended_n_gpu_layers": int(
                    plan.get("recommended_n_gpu_layers", 0) or 0),
                "recommended_n_batch": int(plan.get("recommended_n_batch", 0) or 0),
                "hardware_budget": int(plan.get("hardware_budget", 0) or 0),
                "warnings": self._inference_warnings(),
            }
        except Exception:
            log.warning("生成推理摘要失败", exc_info=True)
            return {}

    def hardware_diagnostics(self) -> Dict[str, Any]:
        """
        [EN]
        Machine-readable hardware and recommendation snapshot for the settings
        page's diagnostics panel (v0.2.4 task 3). It reports aggregate
        capabilities only, never a host identifier or an absolute path.

        [ZH]
        供设置页诊断面板使用的硬件与推荐值快照（v0.2.4 任务-3）。只上报聚合能力，
        绝不上报主机标识或绝对路径。
        """
        try:
            profile = hardware_profiler.probe()
            rec = self._recommended_plan()
            plan = getattr(self.llm, "inference_plan", None) or {}
            if not isinstance(plan, dict):
                plan = {}
            return {
                "cpu": {
                    "physical_cores": int(profile.cpu.physical_cores),
                    "logical_cores": int(profile.cpu.logical_cores),
                    "avx2": bool(profile.cpu.avx2),
                    "avx512": bool(profile.cpu.avx512),
                },
                "gpu": {
                    "available": bool(profile.gpu.available),
                    "backend": profile.gpu.detected_backend,
                    "label": profile.gpu.label,
                    "name": profile.gpu.name,
                    "total_vram_bytes": int(profile.gpu.total_vram_bytes),
                    "free_vram_bytes": int(profile.gpu.free_vram_bytes),
                },
                "ram": {
                    "total_bytes": int(profile.ram_total_bytes),
                    "available_bytes": int(profile.ram_available_bytes),
                },
                "model": {
                    "name": os.path.basename(str(getattr(self.llm, "model_path", "") or "")),
                    "layers": int(rec.model_layers),
                    "native_context": int(rec.native_context),
                },
                "recommended": {
                    "profile": str(getattr(self.config, "inference_profile", "balanced")),
                    "n_ctx": int(rec.n_ctx_max),
                    "n_gpu_layers": int(rec.n_gpu_layers),
                    "n_batch": int(rec.n_batch),
                    "hardware_budget": int(rec.hardware_budget),
                },
                "requested": {
                    "n_ctx": int(getattr(self.config, "context_length", 0) or 0),
                    "n_gpu_layers": int(getattr(self.config, "n_gpu_layers", 0) or 0),
                    "n_batch": int(getattr(self.config, "n_batch", 0) or 0),
                    "override": bool(getattr(self.config, "inference_override", False)),
                },
                "warnings": [str(item) for item in (plan.get("warnings") or [])],
            }
        except Exception:
            log.warning("生成硬件诊断信息失败", exc_info=True)
            return {}

    def _current_model_path(self) -> str:
        """
        [EN]
        Return the model file actually in use, or the configured file when no
        model is loaded but the file exists. The test-load and override paths
        must target a real file; an empty string means "nothing to load".
        [ZH]
        返回当前实际使用的模型文件；未加载模型但配置文件存在时返回配置的文件。
        测试加载与覆盖路径必须指向真实文件；空字符串表示"没有可加载的模型"。
        """
        path = str(getattr(self.llm, "model_path", "") or "").strip()
        if path:
            return path
        configured = os.path.basename(str(getattr(self.config, "model_path", "") or ""))
        if not configured:
            return ""
        candidate = os.path.join(self.models_dir(), configured)
        return candidate if os.path.isfile(candidate) else ""

    def _recommended_plan(self):
        """
        [EN]
        Recompute the safe recommendation with the override disabled. The
        settings page displays these values as defaults and the save path uses
        them to decide whether the user actually changed anything.
        [ZH]
        在关闭覆盖的情况下重新计算安全推荐值。设置页把它们显示为默认值，保存路径
        据此判断用户是否真的改动了参数。
        """
        cfg = self.config
        path = self._current_model_path()
        return hardware_profiler.plan_inference(
            profile=hardware_profiler.probe(),
            model_path=path,
            train_context=resolve_context_window(path, 0) if path else 0,
            requested_context=0,
            max_context=int(cfg.max_context or hardware_profiler.DEFAULT_MAX_CONTEXT),
            requested_gpu_layers=0,
            n_threads_override=int(self.inference_config.resolve_n_threads()),
            inference_profile=str(getattr(cfg, "inference_profile", "")),
            requested_n_batch=0,
            allow_override=False,
        )

    def _inference_failure(self, plan, exc: Exception) -> Dict[str, Any]:
        """
        [EN]
        Build the structured failure payload shared by save and test-load.
        The raw exception text is preserved; suggestions are localized and
        actionable. The live engine is never replaced here.
        [ZH]
        构造保存与测试加载共用的结构化失败负载。保留原始异常文本；建议文案已本地化
        且可操作。此处绝不替换正在运行的引擎。
        """
        error = str(exc)
        lowered = error.lower()
        if "llama_context" in lowered or "failed to create context" in lowered:
            requested = int(getattr(plan, "n_ctx_requested", 0) or 0)
            budget = int(getattr(plan, "hardware_budget", 0) or 0)
            error = _t("inference.error_context_alloc_failed",
                       requested=requested, budget=budget, error=error)
        suggestions: List[str] = []
        lowered = error.lower()
        if "gpu" in lowered or "cuda" in lowered or "offload" in lowered:
            suggestions.append(_t("inference.suggest_lower_gpu_layers"))
        if "context" in lowered or "memory" in lowered or "oom" in lowered:
            suggestions.append(_t("inference.suggest_lower_context"))
        suggestions.append(_t("inference.suggest_use_recommended"))
        summary = self.inference_summary()
        summary.update({
            "ok": False,
            "error": error,
            "error_type": type(exc).__name__,
            "suggestions": suggestions,
            "warnings": list(getattr(plan, "warnings", ()) or ()),
            "plan": plan.to_dict() if hasattr(plan, "to_dict") else {},
        })
        return summary

    def _rebuild_inference(self) -> Dict[str, Any]:
        """
        [EN]
        Re-plan with the current configuration and apply the result. On failure
        the previous engine parameters are restored and the raw error is
        returned; this is the "keep the working engine, report clearly" rule
        agreed for v0.2.4 task 3.
        [ZH]
        按当前配置重新规划并应用结果。失败时恢复旧引擎参数并返回原始错误；这是
        v0.2.4 任务-3 约定的"保留可用引擎、明确报错"规则。
        """
        cfg = self.config
        path = self._current_model_path()
        plan = hardware_profiler.plan_inference(
            profile=hardware_profiler.probe(),
            model_path=path,
            train_context=resolve_context_window(path, 0) if path else 0,
            requested_context=int(cfg.context_length or 0),
            max_context=int(cfg.max_context or hardware_profiler.DEFAULT_MAX_CONTEXT),
            requested_gpu_layers=int(self.inference_config.n_gpu_layers),
            n_threads_override=int(self.inference_config.resolve_n_threads()),
            inference_profile=str(getattr(cfg, "inference_profile", "")),
            requested_n_batch=int(self.inference_config.n_batch),
            # [EN] v0.2.6 item 17: the configured context window is an explicit
            # [EN] user decision and is adopted verbatim. The previous code let
            # [EN] the planner shrink it unless `inference_override` happened to
            # [EN] be set, which made the numeric setting look advisory.
            # [ZH] v0.2.6 第 17 项：配置中的上下文窗口属于用户的显式决定，原样采纳。
            # [ZH] 旧代码只有在 `inference_override` 恰好为真时才不收缩，使数字设置
            # [ZH] 看起来只是建议值。
            allow_override=True,
        )
        snapshot = {
            "n_threads": getattr(self.llm, "n_threads", None),
            "n_batch": getattr(self.llm, "n_batch", None),
            "n_gpu_layers": getattr(self.llm, "n_gpu_layers", None),
            "n_ctx": getattr(self.llm, "n_ctx", None),
            "max_n_ctx": getattr(self.llm, "max_n_ctx", None),
            "kv_cache_type": getattr(self.llm, "kv_cache_type", None),
            "n_ctx_train": getattr(self.llm, "n_ctx_train", None),
            "backend_label": getattr(self.llm, "backend_label", None),
            "inference_plan": dict(getattr(self.llm, "inference_plan", {}) or {}),
        }
        try:
            blocking = _override_blocking_error(plan) or _preflight_context_error(plan)
            if blocking:
                raise RuntimeError(blocking)
            self.llm.apply_plan(plan)
            if path and self.model_ready:
                # [EN] Rebuild the underlying object so the new construction-time
                # [EN] parameters actually take effect.
                # [ZH] 重建底层对象，使新的构建期参数真正生效。
                self.llm.reload(n_ctx=plan.n_ctx_initial)
            self.invalidate_capability()
            log_user(
                log, "sys.log_inference_profile",
                profile=str(getattr(cfg, "inference_profile", "")),
                native=plan.native_context or _t("sys.log_unknown"),
                ctx=plan.n_ctx_initial,
                max_ctx=plan.n_ctx_max,
                batch=plan.n_batch,
                threads=plan.n_threads,
            )
            for warning in plan.warnings:
                log_user(log, "sys.log_inference_warning", warning=warning)
        except Exception as exc:  # noqa: BLE001 - keep the working engine alive
            for key, value in snapshot.items():
                if value is not None:
                    try:
                        setattr(self.llm, key, value)
                    except Exception:  # noqa: BLE001
                        pass
            log.warning("应用推理设置失败（已保留原引擎）: %s", exc, exc_info=True)
            gc.collect()
            return self._inference_failure(plan, exc)
        summary = self.inference_summary()
        summary["ok"] = True
        summary["error"] = ""
        summary["warnings"] = list(plan.warnings)
        summary["plan"] = plan.to_dict()
        return summary

    def apply_inference_profile(self, name: str) -> Dict[str, Any]:
        """
        [EN]
        Persist the selected inference preset and rebuild the live engine with
        the parameters it implies.

        The rebuild is mandatory rather than optional: ``n_ctx`` and ``n_batch``
        are construction-time parameters of llama.cpp, so a preset change that
        only rewrote configuration would report success while the running model
        kept the previous values. When no model is loaded, the plan is still
        recomputed so that the first load uses the selected preset.

        Returns the updated inference summary, augmented with ``ok`` and
        ``error``.

        [ZH]
        持久化所选的推理预设，并以该预设隐含的参数重建当前引擎。

        重建是必需的而非可选的：``n_ctx`` 与 ``n_batch`` 都是 llama.cpp 的构建期
        参数，只改写配置会报告成功，而正在运行的模型仍沿用旧值。未加载模型时同样
        重新计算规划，使首次加载即采用所选预设。

        返回更新后的推理摘要，并附带 ``ok`` 与 ``error`` 字段。
        """
        from hardware_profiler import get_inference_preset

        preset = get_inference_preset(name)
        cfg = self.config
        cfg.inference_profile = preset.name
        if not bool(getattr(cfg, "inference_override", False)):
            # [EN] Reader: the batch size is a hardware parameter, so it is written to
            # [EN] ``inference_config.json`` rather than to the application config.
            # [ZH] Reader：批大小属于硬件参数，因此写入 ``inference_config.json``，
            # [ZH] 而不是应用配置。
            self.inference_config.n_batch = int(preset.n_batch)
            self.inference_config.save()
        error = ""
        try:
            cfg.save()
        except Exception as exc:  # noqa: BLE001 - a read-only medium must not abort
            log.warning("保存推理预设失败（不影响本次运行）", exc_info=True)
            error = str(exc)
        summary = self._rebuild_inference()
        if error:
            summary["ok"] = False
            summary["error"] = error
            summary["error_type"] = type(error).__name__
        return summary

    def guide_text(self) -> str:
        """
        [EN]
        Guidance text for the no-model case: current state, three viable paths, and the real
        model directory path (task 2).

        [ZH]
        无模型时的引导文案：说明现状 + 三条可行路径 + 模型文件夹真实路径（任务二）。
        """
        return _t("sys.no_model_guide", dir=self.models_dir())

    def load_model(self, name_or_path: str = "", *,
                   profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        [EN]
        Hot-load a model (task 5): used after a download completes, when a new model is
        discovered by a scan, and when the user switches models manually.

        Returns ``{ok, name, error, state}``. On failure the backend **stays in the no-model
        state** and reports the cause faithfully (corrupt file / unsupported format /
        insufficient memory), so the user can retry directly without restarting the process.

        ``profile`` (optional): the entry from ``config.models`` used to apply that entry's
        own ``context_length`` and ``temperature`` (the settings-page "model profile" switch
        takes this path, with the same semantics as the ``/model <alias>`` command). When
        omitted, the window is opened according to the global configuration.

        [ZH]
        热加载模型（任务五）：下载完成、扫描到新模型、用户手动切换都走这里。

        返回 ``{ok, name, error, state}``。失败时**保持无模型状态**并把原因如实带回
        （文件损坏 / 格式不支持 / 内存不足），用户可以直接重试，无需重启进程。

        ``profile``（可选）：``config.models`` 中该档位的档案，用于套用该档自己的
        ``context_length`` 与 ``temperature``（设置页的"模型档位"切换走这条路，
        与命令行 ``/model <别名>`` 的语义一致）。不提供时按全局配置开窗。
        """
        want = str(name_or_path or "").strip()
        if not want:
            want = os.path.basename(str(getattr(self.config, "model_path", "") or ""))
        if not want:
            return {"ok": False, "name": "", "error": _t("sys.no_model_name_given"),
                    "state": self.model_state()}
        path = want if os.path.isabs(want) else os.path.join(self.models_dir(), os.path.basename(want))
        if not os.path.isfile(path):
            return {"ok": False, "name": os.path.basename(want),
                    "error": _t("sys.model_file_missing", name=os.path.basename(want),
                                dir=self.models_dir()),
                    "state": self.model_state()}
        requested = None
        if profile and profile.get("context_length"):
            try:
                requested = int(profile["context_length"])
            except (TypeError, ValueError):
                requested = None
        try:
            train_ctx = resolve_context_window(path, 0)
            plan = hardware_profiler.plan_inference(
                profile=hardware_profiler.probe(),
                model_path=path,
                train_context=train_ctx,
                requested_context=int(requested or self.config.context_length or 0),
                max_context=int(self.config.max_context or hardware_profiler.DEFAULT_MAX_CONTEXT),
                requested_gpu_layers=int(self.inference_config.n_gpu_layers),
                n_threads_override=int(self.inference_config.resolve_n_threads()),
                inference_profile=str(getattr(self.config, "inference_profile", "")),
                requested_n_batch=int(self.inference_config.n_batch),
                # [EN] v0.2.6 item 17: adopt the explicit window verbatim.
                # [ZH] v0.2.6 第 17 项：原样采纳显式窗口。
                allow_override=True,
            )
            blocking = _override_blocking_error(plan) or _preflight_context_error(plan)
            if blocking:
                raise RuntimeError(blocking)
            self.llm.apply_plan(plan)
            self.llm.load(path, plan.n_ctx_initial)
        except Exception as e:  # noqa: BLE001
            # [EN] Covers every failure mode here: a corrupt file, an unsupported format,
            # [EN] or insufficient memory.
            # [ZH] 损坏、格式不支持、内存不足都在这里。
            log.exception("热加载模型失败: %s", os.path.basename(path))
            return {"ok": False, "name": os.path.basename(path),
                    "error": _t("sys.model_load_failed", name=os.path.basename(path), error=e),
                    "state": self.model_state()}
        name = os.path.basename(path)
        self.config.model_path = name
        self._startup_inference_error = ""
        # [EN] Override the temperature only when the profile specifies one; a profile value
        # [EN] also takes precedence over a manual change, since the profile is the
        # [EN] recommended value for that entry.
        # [ZH] 仅当档位自带温度时才覆盖；用户手动修改过的温度同样以档案为准，
        # [ZH] 因为档案就是该档位的推荐值。
        if profile and profile.get("temperature") is not None:
            try:
                self.config.temperature = float(profile["temperature"])
            except (TypeError, ValueError):
                pass
        try:
            self.config.save()
        except Exception:
            log.warning("配置保存失败（不影响本次运行）", exc_info=True)
        self.touch_model_usage()
        # [EN] After a model switch, the previous KV cache and history summary are no longer
        # [EN] applicable.
        # [ZH] 切换模型后旧 KV 缓存与历史摘要均不再适用。
        self.llm.clear_cache()
        try:
            self.warmup()
        except Exception:
            log.warning("热加载后的预热失败（不影响使用）", exc_info=True)
        # [EN] Task 14: switching models is a user-initiated action, so this line appears in
        # [EN] the status bar immediately and must follow the UI language.
        # [ZH] 相关设计：切换模型是用户主动操作，该行会立即出现在状态栏，必须与界面语言一致。
        log.info("%s", _t("sys.log_model_hot_loaded", name=name,
                          ctx=int(getattr(self.llm, "n_ctx", 0) or 0)))
        return {"ok": True, "name": name, "error": "", "state": self.model_state()}

    def scan_models(self, auto_load: bool = True) -> Dict[str, Any]:
        """
        [EN]
        Rescan the model directory (task 2: a manually placed ``.gguf`` is then recognized).

        Reader: the scan is a plain directory listing. The former registry-based
        implementation is removed together with the model library, so nothing here
        can reach the network or a download index. With exactly one model it is
        loaded automatically and reported; with several models only the list is
        returned and the user chooses.

        [ZH]
        重新扫描模型文件夹（任务二：手动放置 ``.gguf`` 后能被识别）。

        Reader：扫描就是普通的目录列举。原先基于登记表的实现已随模型库一并删除，
        因此此处不可能触达网络或下载索引。只有一个模型时自动加载并提示；多个模型时
        只返回清单，由用户选择。
        """
        directory = self.models_dir()
        entries: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith(".gguf"):
                continue
            full = os.path.join(directory, name)
            if not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({
                "name": name,
                "size_human": _format_bytes(size),
                "quantization": _profile_quant_text(name),
            })
        current = os.path.basename(str(getattr(self.llm, "model_path", "") or ""))
        loaded = ""
        error = ""
        if auto_load and not self.model_ready and len(entries) == 1:
            result = self.load_model(str(entries[0].get("name") or ""))
            loaded = result.get("name", "") if result.get("ok") else ""
            error = str(result.get("error") or "")
        return {
            "count": len(entries),
            "models": entries,
            "models_dir": directory,
            "current": current,
            "auto_loaded": loaded,
            "error": error,
            "state": self.model_state(),
        }

    # [EN] ---------- Fix 4: model capability profile and summary cache ----------
    # [ZH] ---------- 兼容性修正：模型能力画像 / 摘要缓存 ----------

    def capability(self):
        """
        [EN]
        Capability profile of the current model (fix 4, directions 6/7).

        The cache is keyed by "model path + context window + thread count + GPU layer count":
        switching or reloading the model, or changing the window, changes the key and
        invalidates the cache automatically. This is less error-prone than writing an
        explicit invalidate call at every reload site (a single missed site would display the
        previous model's parameter count).

        [ZH]
        当前模型的能力画像（修复四 方向六/七）。

        缓存按「模型路径 + 上下文窗口 + 线程数 + GPU 层数」做键：模型切换、
        重载、修改窗口后键会变化，缓存自动失效。这比在每个重载点手写
        invalidate 更不容易遗漏（遗漏一处就会显示上一个模型的参数量）。
        """
        from model_profile import build_profile

        key = (
            str(getattr(self.llm, "model_path", "")),
            int(getattr(self.llm, "n_ctx", 0) or 0),
            int(getattr(self.llm, "n_threads", 0) or 0),
            int(getattr(self.llm, "n_gpu_layers", 0) or 0),
        )
        cached = getattr(self, "_capability_cache", None)
        if cached is not None and getattr(self, "_capability_key", None) == key:
            return cached
        profile = build_profile(self.llm, self.config)
        self._capability_cache = profile
        self._capability_key = key
        return profile

    def invalidate_capability(self) -> None:
        """
        [EN]
        Invalidate the capability-profile cache immediately (normally unnecessary, since the
        cache key changes by itself).

        [ZH]
        让能力画像缓存立即失效（一般不需要手动调用，缓存键会自动变化）。
        """
        self._capability_cache = None
        self._capability_key = None

    def model_capability(self) -> Dict[str, Any]:
        """
        [EN]
        Machine-readable capability profile (for /status and the frontend status area).

        [ZH]
        机器可读的能力画像（供 /status 与前端状态区使用）。
        """
        try:
            return self.capability().public()
        except Exception:
            log.debug("能力画像生成失败", exc_info=True)
            return {}

    def summary_cache(self):
        """
        [EN]
        Chunked-summary cache (created lazily; returns None when config.summary_cache is
        False).

        [ZH]
        分段摘要缓存（惰性创建；config.summary_cache=False 时返回 None）。
        """
        if not bool(getattr(self.config, "summary_cache", True)):
            return None
        if self._summary_cache is None:
            from model_profile import SummaryCache

            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "summary_cache.json")
            self._summary_cache = SummaryCache(path)
        return self._summary_cache





    def _make_summary_engine(self, model_path: str):
        """
        [EN]
        Create an engine for the dedicated summary model named by summarize.model, opening the
        window according to that model's training context.

        [ZH]
        为 summarize.model 指定的专用摘要模型创建引擎（按该模型训练窗口开窗）。
        """
        return self._build_engine(self.config, model_path)

    @staticmethod
    def _build_engine(config: Config, model_path: str):
        """
        [EN]
        Create the inference engine from the hardware profile and the selected model.

        The context window is exactly the configured user value (v0.2.6 item 17):
        the planner supplies the accelerator decision and the context ceiling only.
        Reader: **thread count, batch size, mlock, mmap, FlashAttention, KV-cache
        width and GPU layers are no longer passed from here** -- the engine reads
        them from ``inference_config.json``, so this method hard-codes no tuning
        value at all.

        When ``model_path`` is empty, :class:`LazyLLMEngine` (an empty engine) is returned:
        that is the carrier of the "no model ready" state -- the backend starts as usual and
        :meth:`Backend.load_model` **loads in place** once a model is available.

        [ZH]
        依据硬件档案与所选模型创建推理引擎。

        上下文窗口严格等于配置中的用户数值（v0.2.6 第 17 项）：规划器只提供加速器决策与
        上下文上限。Reader：**线程数、批大小、mlock、mmap、FlashAttention、KV Cache 宽度
        与 GPU 层数都不再从本方法传入** —— 引擎统一从 ``inference_config.json`` 读取，
        因此本方法不硬编码任何调优值。

        ``model_path`` 为空时返回 :class:`LazyLLMEngine`（空引擎）：这是"无模型就绪"
        的载体，后端照常启动，等模型到位后由 :meth:`Backend.load_model` **原地加载**。
        """
        profile = hardware_profiler.probe()
        # [EN] Reader: the configuration file is the single source of truth for every
        # [EN] hardware-facing parameter, including the ones the planner used to derive.
        # [ZH] Reader：配置文件是所有硬件相关参数的唯一来源，包括过去由规划器推导的那些。
        inference = InferenceConfig.load()
        path = str(model_path or "").strip()
        if not path:
            plan = hardware_profiler.plan_inference(
                profile=profile,
                requested_context=0,
                max_context=_context_ceiling(config, inference),
                requested_gpu_layers=0,
                n_threads_override=int(inference.resolve_n_threads()),
                inference_profile=str(getattr(config, "inference_profile", "")),
                initial_context_preference=int(inference.n_ctx_default),
                requested_n_batch=int(inference.n_batch),
                # [EN] Reader: the planner is not making a decision here -- it only
                # [EN] reports the machine's ceiling. The initial window is the small
                # [EN] value from ``inference_config.json`` and grows with the document.
                # [ZH] Reader：此处规划器不做决策，只报告机器上限。初始窗口取
                # [ZH] ``inference_config.json`` 中的小值，随后随文档增长。
                allow_override=False,
            )
            log.info("%s", _t("sys.log_model_none_state"))
            engine = LazyLLMEngine(
                plan.n_ctx_initial,
                reasoning=getattr(config, "reasoning", None),
                profile=profile, max_n_ctx=plan.n_ctx_max,
                backend_label=plan.backend, inference=inference,
            )
            engine.inference_plan = plan.to_dict()
            return engine
        train_ctx = resolve_context_window(path, 0)
        plan = hardware_profiler.plan_inference(
            profile=profile,
            model_path=path,
            train_context=train_ctx,
            requested_context=0,
            max_context=_context_ceiling(config, inference),
            requested_gpu_layers=0,
            n_threads_override=int(inference.resolve_n_threads()),
            inference_profile=str(getattr(config, "inference_profile", "")),
            initial_context_preference=int(inference.n_ctx_default),
            requested_n_batch=int(inference.n_batch),
            # [EN] Reader: same rule as the no-model branch -- the ceiling comes from the
            # [EN] configuration file, the initial window is small and document-driven
            # [EN] growth happens through ``set_document_tokens``.
            # [ZH] Reader：与无模型分支同一规则 —— 上限来自配置文件，初始窗口取小值，
            # [ZH] 之后由 ``set_document_tokens`` 按文档驱动扩展。
            allow_override=False,
        )
        blocking = _override_blocking_error(plan) or _preflight_context_error(plan)
        if blocking:
            raise RuntimeError(blocking)
        features = ",".join(
            name for name, enabled in (("AVX2", profile.cpu.avx2),
                                       ("AVX-512", profile.cpu.avx512)) if enabled
        ) or "—"
        log_user(
            log, "sys.log_hardware_profile",
            physical=profile.cpu.physical_cores,
            logical=profile.cpu.logical_cores,
            features=features,
            ram=_format_bytes(profile.ram_total_bytes),
            available=_format_bytes(profile.ram_available_bytes),
            gpu=profile.gpu.label,
            backend=plan.backend,
        )
        log_user(
            log, "sys.log_model_loading",
            # [EN] The signature of i18n.t() is t(key, **kwargs): it does **not accept a
            # [EN] second positional argument**. This was once written as
            # [EN] `train_ctx or _t("sys.log_unknown", …)`, while resolve_context_window()
            # [EN] returns 0 when the GGUF metadata is unreadable -> the right-hand side of
            # [EN] `or` executes -> `TypeError: t() takes 1 positional argument but 2 were
            # [EN] given`. A missing key must be left to the fallback chain.
            # [ZH] i18n.t() 的签名是 t(key, **kwargs)：**不接受第二个位置参数**。
            # [ZH] 此处曾写成 `train_ctx or _t("sys.log_unknown", "未知")`，而
            # [ZH] resolve_context_window() 在 GGUF 元数据读取不到时返回 0 → 执行 or 右侧
            # [ZH] → `TypeError: t() takes 1 positional argument but 2 were given`。
            # [ZH] 缺键的回落应交给回退链处理。
            path=path, ctx=plan.n_ctx_initial, trained=train_ctx or _t("sys.log_unknown"),
            threads=plan.n_threads, batch=plan.n_batch, gpu=plan.n_gpu_layers,
        )
        engine = LLMEngine(
            path, plan.n_ctx_initial,
            reasoning=getattr(config, "reasoning", None),
            profile=profile, max_n_ctx=plan.n_ctx_max,
            backend_label=plan.backend, inference=inference,
        )
        engine.inference_plan = plan.to_dict()
        return engine

    def restore_session(self) -> int:
        """
        [EN]
        Restore the previous session and return the number of restored turns; if the model
        used last time differs from the current one, log a reminder.

        [ZH]
        恢复上次会话，返回恢复的对话轮数；若上次使用的模型与当前不同则记录提醒。
        """
        if not self.config.session_file:
            return 0
        restored, prev_model = self.conv.load_session(self.config.session_file)
        if prev_model and prev_model != self.config.model_path:
            log.warning("上次会话使用模型 %s，当前为 %s（对话仍恢复，可 /model 切换）", prev_model, self.config.model_path)
        return len(self.conv.history) // 2 if restored else 0

    def save_session(self) -> None:
        if self.config.session_file:
            try:
                self.conv.save_session(self.config.session_file, self.config.model_path)
            except Exception:
                log.warning("会话保存失败", exc_info=True)

    # [EN] ---------- Hardware auto-tuning (Reader benchmark module) ----------
    # [ZH] ---------- 硬件自动调优（Reader 基准测试模块） ----------

    def _current_model_file(self) -> str:
        """
        [EN]
        Absolute path of the model currently in use, or an empty string. Used by the
        tuner, which must benchmark the very file the reader will load.

        [ZH]
        当前使用中的模型绝对路径，没有则为空串。调优模块需要基准测试阅读器真正会加载的
        那个文件。
        """
        path = str(getattr(self.llm, "model_path", "") or "").strip()
        if path and os.path.isfile(path):
            return os.path.abspath(path)
        return ""

    def _tune_progress_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Convert one tuner progress event into a protocol ``delta`` line with the
        ``tune`` channel, so the interface can render the benchmark live without
        knowing the tuner's internal stages.

        [ZH]
        把一条调优进度事件转换成带 ``tune`` 通道的协议 ``delta`` 行，使界面无需了解
        调优器内部阶段即可实时渲染基准测试。
        """
        stage = str(event.get("stage") or "")
        if stage == "start":
            text = _t("sys.tune_started", model=event.get("model", ""))
        elif stage == "baseline":
            trial = event.get("trial") or {}
            text = _t("sys.tune_baseline", threads=trial.get("n_threads", 0),
                      batch=trial.get("n_batch", 0), tps=trial.get("tps", 0),
                      ttft=trial.get("ttft_ms", 0))
        elif stage == "trial":
            trial = event.get("trial") or {}
            text = _t("sys.tune_trial", threads=trial.get("n_threads", 0),
                      batch=trial.get("n_batch", 0), tps=trial.get("tps", 0),
                      verdict=("adopt" if event.get("improved") else "keep"))
        elif stage == "persisted":
            text = _t("sys.tune_persisted", path=event.get("path", ""))
        elif stage == "done":
            report = event.get("report") or {}
            text = _t("sys.tune_done", reason=report.get("reason", ""),
                      tps=(report.get("best") or {}).get("tps", 0))
        else:
            text = ""
        # [EN] Private keys (``_report`` holds the live Python object) must not reach the
        # [EN] protocol: they are not JSON serialisable and exist only for in-process
        # [EN] callers.
        # [ZH] 私有键（``_report`` 持有活的 Python 对象）绝不能进入协议：它们不可 JSON
        # [ZH] 序列化，只服务于进程内调用方。
        public = {key: value for key, value in event.items() if not key.startswith("_")}
        return {"type": "delta", "channel": "tune", "stage": stage,
                "content": text, "event": public}

    def _tune_events(self, model_path: str,
                     should_stop=None) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Yield the progress events of one benchmark round.

        The benchmark runs **in this process** and, for every trial, takes the engine's
        process-wide inference lock (see ``llm_engine._INFERENCE_LOCK``). That is what
        makes it safe to keep the measurement off the protocol loop: the lock serialises
        the trial against any conversation turn, so a chat request during tuning waits for
        one trial instead of corrupting the shared llama.cpp state, while status queries
        (``ping``, ``context``, ``inference_config``) are answered immediately because
        they never touch the native runtime.

        A child process was evaluated and rejected: on this Windows console layout the
        grandchild's redirected stdout handle is not reliably usable (its ``flush()``
        raises ``OSError: [Errno 22]``) and a file-based event channel hung the spawn
        itself, so the measurement would have been silently lost. The in-process design
        keeps a single, verified code path.

        [ZH]
        产出一次基准测试的进度事件。

        基准测试**在本进程内**运行，并且每次试探都会获取引擎的进程级推理锁
        （见 ``llm_engine._INFERENCE_LOCK``）。这正是把测量移出协议循环后仍然安全的原因：
        该锁把试探与任何对话轮次串行化，因此调优期间的对话请求会等待一次试探，而不是
        破坏共享的 llama.cpp 状态；而状态查询（``ping``、``context``、
        ``inference_config``）因为根本不触碰原生运行时，可以立即应答。

        已评估并否决了子进程方案：在当前 Windows 控制台布局下，孙进程被重定向的 stdout
        句柄并不可靠（其 ``flush()`` 抛 ``OSError: [Errno 22]``），而改用文件事件通道后
        连进程创建本身都挂起，测量会被静默丢失。进程内方案只保留一条经过验证的路径。
        """
        yield from inference_tuner.iter_tune(
            model_path,
            config=self.inference_config,
            should_stop=should_stop,
        )

    def _handle_inference_tune(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Start the hardware benchmark on a background worker and answer at once. The worker
        only drains the tuner's events, so neither the protocol loop nor the reading
        session is blocked; a conversation turn started meanwhile waits for one trial
        because both sides use the same inference lock.

        [ZH]
        在后台工作线程上启动硬件基准测试并立即应答。工作线程只负责排空调优事件，因此
        协议循环与阅读会话都不会被阻塞；期间发起的对话轮次会等待一次试探，因为两侧使用
        同一把推理锁。
        """
        model_path = self._current_model_file()
        if not model_path:
            return {"type": "system", "content": _t("sys.tune_no_model"),
                    "tune": {"ok": False, "reason": "model_missing"}}
        if getattr(self, "_tuner_running", False):
            return {"type": "system", "content": _t("sys.tune_running"),
                    "tune": {"ok": False, "reason": "already_running"}}

        def _run() -> None:
            self._tuner_running = True
            try:
                for event in self._tune_events(model_path,
                                               should_stop=lambda: self.stop_event.is_set()):
                    log.info("[tune] %s", event.get("stage"))
            except Exception:  # noqa: BLE001 - the worker must never kill the process
                log.exception("硬件调优失败")
            finally:
                self.inference_config = InferenceConfig.load()
                self._tuner_running = False

        threading.Thread(target=_run, name="inference-tune", daemon=True).start()
        return {"type": "system", "content": _t("sys.tune_scheduled"),
                "tune": {"ok": True, "scheduled": True,
                         "model": os.path.basename(model_path)}}

    def _stream_inference_tune(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Streaming variant of the benchmark: every trial is forwarded as soon as it is
        measured, so the operator watches the tuning happen instead of waiting for one
        opaque result. Runs on the streaming worker thread, so the protocol loop stays
        free as well.

        [ZH]
        基准测试的流式版本：每次试探测完即转发，操作者能看到调优过程，而不是等待一个
        不透明的最终结果。运行在流式工作线程上，因此协议循环同样空闲。
        """
        model_path = self._current_model_file()
        if not model_path:
            yield {"type": "error", "content": _t("sys.tune_no_model"),
                   "tune": {"ok": False, "reason": "model_missing"}}
            return
        self._tuner_running = True
        report: Dict[str, Any] = {}
        try:
            for event in self._tune_events(
                    model_path, should_stop=lambda: self.stop_event.is_set()):
                if event.get("stage") == "done":
                    payload = event.get("report")
                    if isinstance(payload, dict):
                        report = payload
                yield self._tune_progress_event(event)
        except Exception as exc:  # noqa: BLE001 - report, never crash the worker
            log.exception("流式硬件调优失败")
            yield {"type": "error", "content": _t("sys.internal_error", error=exc)}
            return
        finally:
            self._tuner_running = False
            # [EN] Re-read the file so the next model load uses the tuned values.
            # [ZH] 重新读取文件，以便下次加载模型即采用调优后的数值。
            self.inference_config = InferenceConfig.load()
        yield {"type": "done", "content": _t("sys.tune_finished"),
               "tune": report, "inference_config": self.inference_config.describe()}

    def touch_model_usage(self) -> None:
        """
        [EN]
        Retained no-op. The model library that stored the "last used" timestamp is
        physically removed; keeping the method (instead of deleting its call sites)
        keeps the loading path stable and costs nothing.

        [ZH]
        保留的空实现。记录"最后使用时间"的模型库已被物理删除；保留本方法（而不是
        逐个删除调用点）可让加载路径保持稳定，且不产生任何成本。
        """
        return None

    def warmup(self) -> None:
        """
        [EN]
        Run one minimal inference with the current system prompt to warm up the KV cache and
        reduce the first response latency.

        With no model this is skipped: the point of warm-up is "a faster first response", and
        without a model there is nothing to warm up. It must not hold up startup (task 1: the
        backend must become ready even when the model is missing).

        [ZH]
        用当前系统提示词做一次极小推理，预热 KV 缓存，缩短首次回复延迟。

        无模型时直接跳过：预热的意义是"首次回复更快"，没有模型则无从谈起，
        也不能让它拖住启动流程（任务一：缺失模型时后端必须照常就绪）。
        """
        if not self.config.warmup:
            return
        # [EN] Task 14 (self-test fix-1): these lines are **visible to the user** -- the
        # [EN] backend process's stderr is read line by line into the status bar
        # [EN] (`StderrReceived` -> `SetStatus` in `MainWindow.axaml.cs`), so messages such as
        # [EN] "preheating the model..." must also be in English when the UI language is
        # [EN] English.
        # [EN]
        # [EN] Always write `log.info("%s", _t(...))` rather than `log.info(_t(...))`: the
        # [EN] latter treats the translated text itself as a % format string, so a single `%`
        # [EN] in the translation raises an exception or silently consumes later arguments.
        # [EN]
        # [EN] Known timing constraint: the language is delivered by the frontend through the
        # [EN] `set_language` request, so startup logs emitted **before** that request can only
        # [EN] use the default language. This constraint cannot be avoided.
        # [ZH] 相关设计（自测问题修复-1）：以下几行**用户可见** —— 后端进程的 stderr
        # [ZH] 被前端逐行读到状态栏（`MainWindow.axaml.cs` 中 `StderrReceived` → `SetStatus`），
        # [ZH] 因此"模型预热中…"这类消息在英文界面下也必须使用英文。
        # [ZH]
        # [ZH] 一律写成 `log.info("%s", _t(...))` 而不是 `log.info(_t(...))`：后者会把译文
        # [ZH] 本身当作 % 格式串，译文只要出现一个 `%` 就会抛异常或吞掉后续参数。
        # [ZH]
        # [ZH] 已知时序约束：语言由前端通过 `set_language` 请求下发，**早于该请求的启动日志**
        # [ZH] 只能落到默认语言 —— 这条约束无法规避。
        if not self.model_ready:
            log.info("%s", _t("sys.log_model_skip_preheat"))
            return
        log.info("%s", _t("sys.log_model_preheating"))
        self.llm.chat_complete(self.conv.build_messages("ping"), max_tokens=1)
        log.info("%s", _t("sys.log_model_preheated"))

    # [EN] ---------- Multimodal and speech (v0.2.6 Part 2) ----------
    # [ZH] ---------- 多模态与语音（v0.2.6 Part 2） ----------

    # [EN] ---------- Request dispatch ----------
    # [ZH] ---------- 请求分发 ----------

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Handle a request that returns a single response line.

        [ZH]
        处理单行响应的请求。
        """
        action = request.get("action")
        # [EN] Lightweight liveness probe (v0.2.6 task 2.1). It performs no model
        # [EN] work and no conversation access, so it must answer immediately even
        # [EN] while a background document job is running.
        # [ZH] 轻量存活探测（v0.2.6 任务 2.1）。不调用模型、不访问会话，因此即使
        # [ZH] 后台文档作业正在运行，也必须立即应答。
        if action == "ping":
            return {"type": "pong", "ok": True, "busy": bool(self.stream_active),
                    "ts": time.time()}
        if action == "file_async":
            return self._handle_file_async(request)
        if action == "file_job_status":
            return self._handle_file_job_status(request)
        if action == "file_job_cancel":
            job_id = str(request.get("job_id") or "")
            cancelled = self._document_jobs.cancel(job_id)
            return {"type": "system", "content": "",
                    "job_id": job_id, "cancelled": cancelled,
                    "job": (self._document_jobs.get(job_id).snapshot()
                            if self._document_jobs.get(job_id) else {})}
        if action == "chat":
            return self._handle_chat(request.get("message", ""))
        if action == "file":
            # [EN] Multi-file (task 8): files is an array of paths; single-file requests still
            # [EN] use path (backward compatible).
            # [ZH] 多文件（相关设计）：files 是路径数组；单文件仍走 path（向后兼容）。
            paths = request.get("files")
            if isinstance(paths, list) and paths:
                result = self.load_files_into_context([str(p) for p in paths])
                return {"type": "system", "content": result["text"],
                        "files": [
                            {"name": r.get("name", ""), "ok": bool(r.get("ok")),
                             "chars": int(r.get("chars", 0) or 0),
                             "message": str(r.get("message", ""))}
                            for r in result["files"]
                        ],
                        "names": result["names"], "failed": result["failed"]}
            return self._handle_file(request.get("path", ""))
        # [EN] Context snapshot: the reading pipeline's single most important status
        # [EN] query (window usage, remaining budget), so it is kept in the slim
        # [EN] dispatch alongside the file and generation paths.
        # [ZH] 上下文快照：阅读链路最重要的状态查询（窗口占用与剩余预算），因此与文件、
        # [ZH] 生成两条路径一起保留在精简后的分发中。
        if action == "context":
            return self._handle_context()
        # [EN] ---------- No-model-ready state and hot loading (general optimization in an
        # [EN] earlier release, design sections II and V, tasks 1/2/5) ----------
        # [ZH] ---------- 无模型就绪 / 热加载（此前版本 综合优化 相关设计、二、五） ----------
        if action == "model_state":
            return {"type": "system", "content": "", "model_state": self.model_state()}
        if action == "model_scan":
            return self._handle_model_scan(request)
        if action == "model_load":
            return self._handle_model_load(request)
        # [EN] ---------- Model profiles (settings-page entry for multiple default models) ----------
        # [ZH] ---------- 模型档位（多档默认模型的设置页入口） ----------
        if action == "model_profiles":
            return {"type": "system", "content": "", "model_profiles": self.profiles_state()}
        if action == "model_profile_apply":
            return self._handle_model_profile_apply(request)
        if action == "model_profile_save":
            return self._handle_model_profile_save(request)
        if action == "model_profile_delete":
            return self._handle_model_profile_delete(request)
        # [EN] ---------- Multilingual support (task 3, 3.4) ----------
        # [ZH] ---------- 多语言（相关设计 3.4） ----------
        if action == "set_language":
            return self._handle_set_language(request)
        if action == "language":
            return self._handle_language()
        # [EN] ---------- Command palette data source (task 2, 2.6) ----------
        # [ZH] ---------- 指令面板数据源（相关设计 2.6） ----------
        if action == "commands":
            return {"type": "system", "content": "", "commands": []}
        # [EN] ---------- Cross-platform: platform and capability boundaries ----------
        # [ZH] ---------- 跨平台（相关设计 / 相关设计）：平台与能力边界 ----------
        # [EN] The frontend receives the same report from ready.platform at startup; this
        # [EN] action lets the "capability list" panel and troubleshooting re-query it at any
        # [EN] time (for example after the user changes machine, plugs in an external GPU, or
        # [EN] installs WPS).
        # [ZH] 前端启动时从 ready.platform 获取同一份报告；该动作供"能力清单"面板
        # [ZH] 与排障时随时再次查询（例如用户更换机器、插入外置显卡、安装 WPS 之后）。
        if action == "platform":
            report = platform_support.report()
            return {"type": "system", "content": "", "platform": report}
        # [EN] ---------- Fix 4: model capability and context management ----------
        # [ZH] ---------- 兼容性修正：模型能力与上下文管理 ----------
        if action == "capability":
            return {"type": "system", "content": "", "capability": self.model_capability(),
                    "presets": _preset_list(),
                    "inference_presets": hardware_profiler.inference_preset_menu(),
                    "task_templates": _task_template_list(),
                    "recommendation": _recommendation(),
                    "summary_cache": (self.summary_cache().stats() if self.summary_cache() else {}),
                    "context": self._context_snapshot(),
                    "inference": self.inference_summary(),
                    "hardware": self.hardware_diagnostics(),
                    "settings": {
                        "preset": self.config.preset,
                        "inference_profile": str(
                            getattr(self.config, "inference_profile", "balanced")),
                        "context_mode": self.config.context_mode,
                        "n_ctx": int(getattr(self.config, "context_length", 0) or 0),
                        "n_gpu_layers": int(getattr(self.config, "n_gpu_layers", 0) or 0),
                        "n_batch": int(getattr(self.config, "n_batch", 0) or 0),
                        "inference_override": bool(
                            getattr(self.config, "inference_override", False)),
                        "temperature": float(getattr(self.config, "temperature", 0.7) or 0.7),
                        "top_p": float(getattr(self.config, "top_p", 0.9)),
                        "repeat_penalty": float(getattr(self.config, "repeat_penalty", 1.1)),
                        "max_tokens": int(getattr(self.config, "max_tokens", 1024)),
                        "clarify_vague": bool(getattr(self.config, "clarify_vague", True)),
                        "summary_cache": bool(getattr(self.config, "summary_cache", True)),
                        "summarize": {
                            "chunk_chars": self.config.summarize.chunk_chars,
                            "overlap_chars": self.config.summarize.overlap_chars,
                            "max_summary_tokens": self.config.summarize.max_summary_tokens,
                            "merge_max_tokens": self.config.summarize.merge_max_tokens,
                            "trigger_ratio": self.config.summarize.trigger_ratio,
                            "min_chars": self.config.summarize.min_chars,
                            "keep_pointer": self.config.summarize.keep_pointer,
                            "enabled": self.config.summarize.enabled,
                        },
                    }}
        if action == "context_compress":
            result = self.compact_context(force=bool(request.get("force", True)))
            return {"type": "system", "content": "", "compress": result,
                    "context": self._context_snapshot()}
        if action == "capability_settings":
            return self._handle_capability_settings(request)
        if action == "summary_cache_clear":
            cache = self.summary_cache()
            cleared = cache.clear() if cache else 0
            return {"type": "system", "content": "", "cleared": cleared,
                    "summary_cache": (cache.stats() if cache else {})}
        # [EN] Reader: inference configuration and hardware auto-tuning.
        # [EN] ``inference_config`` reports the active file, ``inference_tune``
        # [EN] runs the benchmark and persists the winning combination.
        # [ZH] Reader：推理配置与硬件自动调优。``inference_config`` 报告当前生效文件，
        # [ZH] ``inference_tune`` 运行基准测试并把胜出的参数组合落盘。
        if action == "inference_config":
            return {"type": "system", "content": "", "inference_config": self.inference_config.describe()}
        if action == "inference_tune":
            return self._handle_inference_tune(request)
        return {"type": "system", "content": _t("sys.unknown_action")}

    def _handle_model_scan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Rescan the model directory: a manually placed ``.gguf`` is then recognized and
        hot-loaded when needed.

        [ZH]
        重新扫描模型文件夹：手工放入 ``.gguf`` 后能被识别并按需热加载。
        """
        try:
            result = self.scan_models(auto_load=bool(request.get("auto_load", True)))
        except Exception as e:  # noqa: BLE001
            log.exception("扫描模型文件夹失败")
            return {"type": "system", "content": _t("sys.model_scan_failed", error=e),
                    "model_state": self.model_state()}
        if result.get("auto_loaded"):
            content = _t("sys.model_scan_auto_loaded", name=result["auto_loaded"])
        elif result.get("count") == 0:
            content = _t("sys.model_scan_empty", dir=result.get("models_dir", self.models_dir()))
        elif not self.model_ready:
            content = _t("sys.model_scan_found", count=result["count"],
                         dir=result.get("models_dir", self.models_dir()))
        else:
            content = _t("sys.model_scan_ok", count=result["count"])
        if result.get("error"):
            content += " ⚠️ " + str(result["error"])
        return {"type": "system", "content": content, "model_scan": result,
                "model_state": result.get("state", self.model_state())}

    def _handle_model_load(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Hot-load the specified model (without restarting the process). On failure the
        no-model state is retained and the reason is reported.

        [ZH]
        热加载指定模型（不重启进程）。失败时保持无模型状态并给出原因。
        """
        result = self.load_model(str(request.get("name") or ""))
        if result.get("ok"):
            content = _t("sys.model_hot_loaded", name=result.get("name", ""))
        else:
            content = _t("sys.model_hot_load_failed", error=result.get("error", ""))
        return {"type": "system", "content": content, "model_load": result,
                "model_state": result.get("state", self.model_state())}

    # [EN] ---------- Model profiles (multiple default models: display and switch in the
    # [EN] settings page) ----------
    # [ZH] ---------- 模型档位（多档默认模型：设置页里显示与切换） ----------

    def profiles_state(self) -> Dict[str, Any]:
        """
        [EN]
        List the model profiles registered in ``config.models`` and mark "currently in use"
        and "already downloaded".

        Why this is needed (user requirement: "both profiles should be selectable defaults and
        switchable in settings"): `config.models` was previously usable only through the
        ``/model <alias>`` command and was invisible in the settings UI, so users could not
        tell which profiles existed or which one was present locally. This turns that registry
        into structured data rendered by the settings page as a profile list (with size,
        quantization, parameter count, and a switch button).

        It deliberately **does not access the network**: it reads local files only, and a
        profile that has not been downloaded is reported as `installed=false` (downloading is
        done through the model marketplace, or through the "download" button on the profile
        row, which uses the marketplace streaming download).

        [ZH]
        列出 ``config.models`` 中登记的模型档位，并标出"当前使用""是否已下载"。

        为什么需要它（用户需求："两档都作为可选默认显示、可在设置里切换"）：
        `config.models` 原本只能通过命令行 ``/model <别名>`` 使用，设置界面中不可见，
        用户无法知道有哪些档位、哪一档已在本地。这里把这份注册表转为结构化数据，
        由设置页渲染成"档位"列表（含体积/量化/参数量与切换按钮）。

        刻意**不联网**：只读取本地文件，未下载的档位如实标为 `installed=false`
        （下载请走模型市场，或点击档位行上的「下载」—— 它使用市场那套流式下载）。
        """
        current = os.path.basename(str(getattr(self.config, "model_path", "") or ""))
        models_dir = self.models_dir()
        items: List[Dict[str, Any]] = []
        for alias, profile in (self.config.models or {}).items():
            raw = profile if isinstance(profile, dict) else {}
            name = os.path.basename(str(raw.get("path") or alias))
            full = os.path.join(models_dir, name)
            exists = os.path.isfile(full)
            size = os.path.getsize(full) if exists else 0
            items.append({
                "alias": alias,
                "name": name,
                # [EN] path and note are returned as well (found during capability extension-1
                # [EN] frontend integration): the profile edit form must **prefill** the
                # [EN] values the user originally entered; otherwise "open, edit, save" would
                # [EN] treat the hidden fields as empty values.
                # [ZH] path 与 note 也回传（后续版本 前端联调发现）：档位编辑表单需**回填**
                # [ZH] 用户当初填写的值，否则"打开编辑再保存"会把未显示的字段按空值处理。
                "path": str(raw.get("path") or ""),
                "note": str(raw.get("note") or ""),
                "repo": str(raw.get("repo") or ""),
                "installed": exists,
                "size": size,
                "size_human": _format_bytes(size) if exists else "",
                "context_length": int(raw.get("context_length") or 0),
                "temperature": raw.get("temperature"),
                # [EN] Parameter count and quantization are inferred from the file name: the
                # [EN] settings page can show the differences between profiles **without
                # [EN] loading the model**.
                # [ZH] 参数量/量化均按文件名推断：**不加载模型**也能在设置页显示档位差异。
                "params": _profile_params_text(name),
                "quantization": _profile_quant_text(name),
                # [EN] `current` **must also require that the file really exists** (v0.2.2
                # [EN] batch 4).
                # [EN]
                # [EN] The original predicate was only `name == current`, so when "config
                # [EN] points to a model whose file is not on disk" (a fresh install with no
                # [EN] model downloaded yet), the same profile was **both** `current=true` and
                # [EN] `installed=false`. The UI computes `IsEnabled = !current`, disables it,
                # [EN] renders the self-contradictory label "not downloaded - currently in
                # [EN] use", and **blocks the download entry point**: the user sees a profile
                # [EN] that claims to be in use but cannot click it to download.
                # [EN]
                # [EN] The two states are mutually exclusive by definition: if the file is not
                # [EN] present, nothing can be "in use". `installed` is the fact, and `current`
                # [EN] should be built on top of it.
                # [ZH] `current` **必须同时要求文件确实存在**（v0.2.2 第 4 批）。
                # [ZH]
                # [ZH] 原判据只有 `name == current`，因此当"config 指向某个模型、但该文件
                # [ZH] 不在磁盘上"时（全新安装、尚未下载任何模型的机器即如此），同一档位会
                # [ZH] **同时**为 `current=true` 与 `installed=false` —— 界面按
                # [ZH] `IsEnabled = !current` 将其禁用，渲染出"未下载 · ▶ 正在使用"这种
                # [ZH] 自相矛盾的标签，并且**阻断了下载入口**：
                # [ZH] 用户看到一个声称自己"正在使用"的档位，却无法点击它去下载。
                # [ZH]
                # [ZH] 两个状态本互斥：文件都不在，就没有任何东西"正在使用"。
                # [ZH] `installed` 是事实，`current` 应建立在它之上。
                "current": bool(name) and name == current and exists,
            })
        return {"current": current, "models_dir": models_dir, "items": items}

    def apply_profile(self, alias: str) -> Dict[str, Any]:
        """
        [EN]
        Switch to a profile from ``config.models`` (hot load plus that profile's window and
        temperature).

        Same semantics as the ``/model <alias>`` command: an unknown alias, a file missing
        locally, or a load failure **reports the reason faithfully**, and the UI prompts
        accordingly (for a profile that is not downloaded, the UI directs the user to
        download it).

        [ZH]
        切换到 ``config.models`` 中的某一档（热加载 + 套用该档的窗口与温度）。

        与命令行 ``/model <别名>`` 走同一份语义：找不到别名、文件不在本地、
        加载失败都**如实返回原因**，界面据此提示（未下载的档位由界面引导去下载）。
        """
        alias = str(alias or "").strip()
        profile = (self.config.models or {}).get(alias)
        if not isinstance(profile, dict):
            return {"ok": False, "alias": alias, "installed": False,
                    "error": _t("sys.profile_unknown", alias=alias),
                    "state": self.model_state()}
        name = os.path.basename(str(profile.get("path") or alias))
        full = name if os.path.isabs(name) else os.path.join(self.models_dir(), name)
        if not os.path.isfile(full):
            return {"ok": False, "alias": alias, "name": name, "installed": False,
                    "repo": str(profile.get("repo") or self.config.model_repo or ""),
                    "error": _t("sys.profile_not_installed", name=name, dir=self.models_dir()),
                    "state": self.model_state()}
        result = self.load_model(full, profile=profile)
        result["alias"] = alias
        return result

    def _handle_model_profile_apply(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        The "switch" button on the settings page: change profile and return the new model
        state.

        [ZH]
        设置页的「切换」按钮：切换档位并回传新的模型状态。
        """
        alias = str(request.get("alias") or "")
        result = self.apply_profile(alias)
        if result.get("ok"):
            content = _t("sys.profile_applied", alias=alias, name=result.get("name", ""))
        else:
            content = _t("sys.profile_apply_failed", error=result.get("error", ""))
        return {"type": "system", "content": content, "model_profile": result,
                "model_profiles": self.profiles_state(),
                "model_state": result.get("state", self.model_state())}

    # [EN] ---------- Custom model profiles (capability extension-1, task 5 C) ----------
    # [ZH] ---------- 自定义模型档位（后续版本 相关设计 C） ----------

    #: [EN] Fields and types permitted in a profile (whitelist). **Storing arbitrary keys is
    #: deliberately avoided**: `config.models` is read by /model, the settings page, and the
    #: load path, so a misspelled key would silently do nothing, whereas a whitelist can
    #: reject it at write time.
    #: [ZH] 档位中允许写入的字段与类型（白名单）。**刻意不做"任意键都存"**：
    #: [ZH] `config.models` 会被 /model、设置页与加载路径共同读取，写入一个拼错的键
    #: [ZH] 只会静默失效，而白名单能在写入时即报错。
    PROFILE_FIELDS = ("path", "repo", "context_length", "temperature", "note", "quantization")

    def _save_profile_field(self, alias: str, profile: Dict[str, Any]) -> None:
        models = dict(self.config.models or {})
        models[alias] = profile
        self.config.models = models
        self.config.save()

    def save_profile(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Add or modify a model profile (the alias is the key), written to the ``models``
        section of ``config.json``.

        The task requires "users can define custom model profiles (not only two presets),
        setting the alias, context length, temperature, and note". Previously there were only
        "read" and "switch" operations and no write entry point, so adding a profile required
        editing ``config.json`` by hand. This adds the write path while keeping the existing
        field semantics unchanged (**extension, not rewrite**: the ``models`` structure remains
        ``{alias: {path, repo, context_length, temperature, ...}}``; old configurations are
        still readable, with only two optional keys, ``note`` and ``quantization``, added).

        An existing alias means modification (not appending); ``rename_from`` supports renaming.

        [ZH]
        新增/修改一个模型档位（别名即键），写入 ``config.json`` 的 ``models`` 段。

        设计规范要求"用户可自定义模型档位（不只是两档预设），设定别名、上下文长度、
        温度、备注" —— 原先只有"读"与"切换"，没有任何写入口，添加档位必须手工修改
        ``config.json``。此处补上写入口，并保持既有字段语义不变（**扩展而不是重写**：
        ``models`` 的结构仍为 ``{别名: {path, repo, context_length, temperature, ...}}``，
        老配置照常读取，只是新增了 ``note`` 与 ``quantization`` 两个可选键）。

        别名相同即修改（而非追加），``rename_from`` 支持改名。
        """
        alias = str(request.get("alias") or "").strip()
        if not alias:
            return {"ok": False, "error": _t("sys.profile_alias_required"),
                    "state": self.profiles_state()}
        if len(alias) > 32 or any(ch in alias for ch in "\\/:\n\t"):
            return {"ok": False, "error": _t("sys.profile_alias_invalid", alias=alias),
                    "state": self.profiles_state()}
        models = dict(self.config.models or {})
        rename_from = str(request.get("rename_from") or "").strip()
        base = dict(models.get(rename_from) or {}) if rename_from else dict(models.get(alias) or {})
        if rename_from and rename_from != alias:
            models.pop(rename_from, None)
        for field in self.PROFILE_FIELDS:
            if field not in request:
                continue
            value = request.get(field)
            if field == "context_length":
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    return {"ok": False, "error": _t("sys.profile_bad_context", value=value),
                            "state": self.profiles_state()}
                # [EN] **Out-of-range values must be rejected, not silently clamped**: clamping
                # [EN] in the UI slider is considerate, clamping in the API is misleading (the
                # [EN] user passes 100000 and gets 32768 while believing the setting applied).
                # [EN] During the first round of testing, temperature=9 was silently changed to
                # [EN] 1.5 together with a "saved" message.
                # [ZH] **越界必须报错而不是静默夹紧**：界面滑块夹紧是体贴，接口夹紧则是误导
                # [ZH] （用户传入 100000 却得到 32768，还以为设置成功）。首轮验证中
                # [ZH] 传入 temperature=9 被静默改为 1.5 且提示"已保存"。
                if number < 0 or number > int(self.config.max_context):
                    return {"ok": False,
                            "error": _t("sys.profile_context_out_of_range",
                                        value=number, low=0, high=self.config.max_context),
                            "state": self.profiles_state()}
                value = number
            elif field == "temperature":
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return {"ok": False, "error": _t("sys.profile_bad_temperature", value=value),
                            "state": self.profiles_state()}
                if number < 0.0 or number > 1.5:
                    return {"ok": False,
                            "error": _t("sys.profile_temperature_out_of_range",
                                        value=number, low=0.0, high=1.5),
                            "state": self.profiles_state()}
                value = number
            else:
                value = str(value or "").strip()
            if value in ("", None):
                base.pop(field, None)
            else:
                base[field] = value
        if not base.get("path"):
            base["path"] = alias if alias.lower().endswith(".gguf") else f"{alias}.gguf"
        models[alias] = base
        self.config.models = models
        self.config.save()
        return {"ok": True, "alias": alias, "profile": base, "state": self.profiles_state(),
                "content": _t("sys.profile_saved", alias=alias)}

    def delete_profile(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Delete a profile registration (**without deleting the model file on disk**).

        Why only the registration is removed: deleting a file is an irreversible, heavyweight
        operation, whereas a profile is only a record of "I commonly use this one". Actual file
        deletion has its own entry point in the model library (with a confirmation dialog).

        [ZH]
        删除一个档位登记（**不删除磁盘上的模型文件**）。

        为什么只删登记：删除文件是不可逆的重操作，而档位只是"我常用这一档"的记录。
        真正删除文件在模型库中另有入口（带确认弹窗）。
        """
        alias = str(request.get("alias") or "").strip()
        models = dict(self.config.models or {})
        if alias not in models:
            return {"ok": False, "error": _t("sys.profile_unknown", alias=alias),
                    "state": self.profiles_state()}
        name = os.path.basename(str((models.get(alias) or {}).get("path") or alias))
        current = os.path.basename(str(getattr(self.config, "model_path", "") or ""))
        if name and name == current:
            return {"ok": False, "error": _t("sys.profile_in_use", alias=alias),
                    "state": self.profiles_state()}
        models.pop(alias, None)
        self.config.models = models
        self.config.save()
        return {"ok": True, "alias": alias, "state": self.profiles_state(),
                "content": _t("sys.profile_deleted", alias=alias)}

    @staticmethod
    def _profile_response_content(result: Dict[str, Any]) -> str:
        """
        [EN]
        Normalize profile write results into one line of UI text (failures always carry a ❌
        prefix).

        Why this is a separate helper: some texts in the `error` field already include ❌ (e.g.
        `profile_in_use`) while others do not (e.g. `profile_unknown`, a neutral message reused
        elsewhere). The UI needs consistent presentation, so the prefix is added in the **output
        layer** rather than by modifying those shared language keys.

        [ZH]
        把档位写操作的结果统一成一行界面文本（失败一定带 ❌ 前缀）。

        为什么单独抽出：`error` 字段中的文案有的自带 ❌（如 `profile_in_use`）、
        有的没有（如 `profile_unknown` 是给别处复用的中性提示）。界面需要一致的呈现，
        因此由**输出层**补齐前缀，而不是修改那些被复用的语言键。
        """
        content = str(result.get("content") or "")
        if content:
            return content
        error = str(result.get("error") or "")
        if not error:
            return ""
        return error if error.startswith("❌") else f"❌ {error}"

    def _handle_model_profile_save(self, request: Dict[str, Any]) -> Dict[str, Any]:
        result = self.save_profile(request)
        return {"type": "system", "content": self._profile_response_content(result),
                "model_profile_save": result, "model_profiles": result.get("state", {})}

    def _handle_model_profile_delete(self, request: Dict[str, Any]) -> Dict[str, Any]:
        result = self.delete_profile(request)
        return {"type": "system", "content": self._profile_response_content(result),
                "model_profile_delete": result, "model_profiles": result.get("state", {})}

    # [EN] ---------- Skill marketplace (task 5) ----------
    # [ZH] ---------- 技能市场（相关设计） ----------









    def _handle_context(self) -> Dict[str, Any]:
        """
        [EN]
        Return the current context usage (used/limit) for real-time display in the frontend
        toolbar.

        [ZH]
        返回当前上下文占用（used/limit，供前端工具栏实时显示）。
        """
        response: Dict[str, Any] = {"type": "context"}
        self._attach_context(response)
        return response

    def _output_token_budget(self) -> int:
        """
        [EN]
        Compute the output-token budget for one generation.

        The budget is the larger of the configured value and 8% of the hardware
        context ceiling, clamped to ``[64, 8192]``. The percentage keeps a strong
        model from being cut off on a large window without allowing the output to
        consume the whole allocation; the fixed upper bound protects smaller
        machines from an excessive reservation.

        [ZH]
        计算单次生成的输出 token 预算。

        取配置值与硬件上下文上限 8% 中的较大者，并收敛到 ``[64, 8192]``。按比例
        取值使强模型在大窗口下不会被截断，同时避免输出占满整个分配；固定上限则
        防止较小机器做出过大的预留。
        """
        configured = int(getattr(self.config, "max_tokens", 0) or 0)
        ceiling = int(getattr(self.llm, "max_n_ctx", 0)
                      or getattr(self.llm, "n_ctx", 0) or 0)
        dynamic = int(ceiling * 0.08) if ceiling > 0 else 0
        dynamic = max(256, min(8192, dynamic)) if dynamic > 0 else 1024
        return max(64, min(8192, max(configured, dynamic)))

    def _attach_context(self, response: Dict[str, Any]) -> None:
        """
        [EN]
        Attach the current context usage to the response so the frontend refreshes in real
        time (it naturally decreases after /clear or /undo).

        Optimization 5: the KV cache estimate and context window are provided additionally for
        the lightweight status area below the input box (the frontend no longer has to parse
        the Chinese text in /status).

        [ZH]
        把当前上下文占用附加到响应上，前端据此实时刷新（/clear、/undo 后自然减少）。

        优化五：额外给出 KV Cache 估算与上下文窗口，供输入框下方的轻量状态区显示
        （前端不必再去解析 /status 中的中文文本）。

        Reader：基准测试运行期间**跳过**这里。`_estimate_tokens` 会调用当前模型的
        原生分词器，而基准测试正持有推理锁并操作另一个原生模型实例；此时读取会让
        协议应答一直等到整轮调优结束（实测：异步调优动作 30 秒内拿不到应答）。跳过
        上下文块的代价只是状态区短暂缺少占用数字，换来的是一条永远不会被测量阻塞的
        应答通道。
        """
        # [EN] Never touch the native runtime while a benchmark owns it.
        # [ZH] 基准测试持有时绝不触碰原生运行时。
        if getattr(self, "_tuner_running", False):
            return
        try:
            messages = [{"role": "system", "content": self.conv.get_system_prompt()}] + self.conv.history
            response["used"] = self._estimate_tokens(messages)
            # [EN] Task 10 (self-test fix-1): report the two measurements **side by side**:
            # [EN]   ``used``              = system prompt + history (the persistent baseline;
            # [EN]                           reflects /clear and /compact immediately);
            # [EN]   ``used_last_request`` = **everything actually sent to the model in the last
            # [EN]                           turn**, including the file body, retrieval
            # [EN]                           fragments, and search results injected that turn.
            # [EN] The user-reported "usage is far below 1/3" occurred because only the former
            # [EN] existed; it misses the largest component. Not overwriting ``used`` is
            # [EN] deliberate: existing displays and regression assertions rely on it, and
            # [EN] reporting both is more informative.
            # [ZH] 相关设计（自测问题修复-1）：把两个口径**并列**给出 ——
            # [ZH]   ``used``                = 系统提示 + 历史（持久占用的底数；
            # [ZH]                             /clear 与 /compact 后立即反映变化）；
            # [ZH]   ``used_last_request``   = **上一轮真正发给模型的全部内容**，含本轮注入的
            # [ZH]                             文件正文、检索片段与搜索结果。
            # [ZH] 用户报告的"占用远未到 1/3"就是因为原先只有前者；只看它等于漏掉了最大的一块。
            # [ZH] 不覆盖 ``used`` 是刻意的：既有显示与回归断言都基于它，并列给出信息更全。
            response["used_last_request"] = int(getattr(self, "_last_request_tokens", 0) or 0)
            # [EN] v0.2.6 item 17: report the window actually allocated, which is
            # [EN] exactly the user's choice. The engine no longer grows on demand,
            # [EN] so the progress bar must be scaled against this fixed value.
            # [ZH] v0.2.6 第 17 项：上报实际分配的窗口，它严格等于用户选择值。引擎已
            # [ZH] 不再按需扩展，因此进度条必须以这个固定值为分母。
            effective_ctx = int(getattr(self.llm, "max_n_ctx", 0) or self.llm.n_ctx or 0)
            response["limit"] = int(effective_ctx * 0.8)
            response["n_ctx"] = effective_ctx
            # [EN] v0.2.6 item 17: expose the user's explicit setting separately
            # [EN] from the effective window so the front end can preselect the
            # [EN] correct entry in the 4096/8192/16384/32768 selector.
            # [ZH] v0.2.6 第 17 项：把用户显式设置与生效窗口分开上报，使前端能在
            # [ZH] 4096/8192/16384/32768 选择器中预选正确项。
            response["context_length"] = int(getattr(self.config, "context_length", 0) or 0)
            # [EN] Native and planned context are reported alongside the effective
            # [EN] ceiling so that the interface can distinguish "the model cannot
            # [EN] exceed this window" from "this machine cannot afford a larger
            # [EN] window". The explanatory sentence is localized by the backend and
            # [EN] displayed verbatim.
            # [ZH] 原生上下文与规划上下文同生效上限一并上报，使界面能够区分"模型本身
            # [ZH] 无法超出该窗口"与"本机无法承担更大的窗口"。解释语句由后端本地化后
            # [ZH] 由界面原样展示。
            response["n_ctx_native"] = int(getattr(self.llm, "n_ctx_train", 0) or 0)
            response["inference_profile"] = str(
                getattr(self.config, "inference_profile", "balanced"))
            response["n_batch"] = int(getattr(self.llm, "n_batch", 0) or 0)
            response["context_limited_by"] = self._context_limited_by()
            response["context_limit_text"] = self._context_limit_text()
            try:
                response["kv_cache_text"] = self.llm.kv_cache_text()
            except Exception:
                response["kv_cache_text"] = ""
            # [EN] Physical-memory pressure for the status strip. ``probe()`` caches its result,
            # [EN] so this does not run GPU detection on every context refresh.
            # [ZH] 状态区所需的物理内存压力。``probe()`` 会缓存结果，因此不会在每次上下文刷新时
            # [ZH] 重复执行 GPU 探测。
            try:
                hw = hardware_profiler.probe()
                response["memory"] = {
                    "total_bytes": int(hw.ram_total_bytes),
                    "available_bytes": int(hw.ram_available_bytes),
                }
            except Exception:
                response["memory"] = {}
            try:
                response["model"] = os.path.basename(self.config.model_path or "")
            except Exception:
                response["model"] = ""
            # [EN] Task 1: machine-readable "is inference available". The frontend uses it to
            # [EN] show "no model" and turn the inference entry point into guidance, instead of
            # [EN] reporting an error only after the user submits. Without it the frontend can
            # [EN] only guess.
            # [ZH] 相关设计：机器可读的"推理是否可用"。前端据此显示"无模型"并把推理入口
            # [ZH] 转为引导，而不是等用户提交后才报错。缺少该字段时前端只能推测。
            response["model_ready"] = self.model_ready
            try:
                response["search"] = False
            except Exception:
                response["search"] = False
            # [EN] Development task 3 in an earlier release: the mode is also exposed as a
            # [EN] machine-readable field. The frontend previously matched the Chinese prefix
            # [EN] of the "mode switched" message, which stopped working in the English UI.
            # [ZH] 此前版本 相关设计：模式也作为机器可读字段。前端此前依靠匹配
            # [ZH] "✅ 已切换模式为: " 这个中文前缀来判断，切换到英文界面后即失效。
            try:
                response["mode"] = self.conv.get_mode()
            except Exception:
                response["mode"] = ""
            try:
                response["temperature"] = float(getattr(self.config, "temperature", 0.7) or 0.7)
            except Exception:
                response["temperature"] = None
            # [EN] Fix 4 (direction 7: capability transparency): include how fast the model can
            # [EN] run, how large the window is, and whether a capability boundary has been
            # [EN] reached, so the frontend status area can display it in real time without the
            # [EN] user reading the Chinese text of /status.
            # [ZH] 兼容性修正（方向七：能力透明化）：把"模型能跑多快、窗口开了多大、
            # [ZH] 是否触及能力边界"一并带上，前端状态区据此实时显示，
            # [ZH] 用户不必去翻阅 /status 的中文文本。
            profile = self.model_capability()
            if profile:
                response["capability"] = {
                    "parameters_text": profile.get("parameters_text", ""),
                    "quantization": profile.get("quantization", ""),
                    "train_context": profile.get("train_context", 0),
                    "usable_context": profile.get("usable_context", 0),
                    "kv_cache_text": profile.get("kv_cache_text", ""),
                    "last_tps": profile.get("last_tps", 0.0),
                    "last_tokens": profile.get("last_tokens", 0),
                    "warnings": profile.get("warnings", []),
                }
            response["preset"] = str(getattr(self.config, "preset", "") or "")
            response["context_mode"] = str(getattr(self.config, "context_mode", "summarize") or "summarize")
        except Exception:
            pass

    def _context_snapshot(self) -> Dict[str, Any]:
        """
        [EN]
        Take a standalone context snapshot (without the type field) for reuse by the
        capability endpoints.

        [ZH]
        单独取一份上下文快照（不含 type 字段），供能力接口复用。
        """
        snapshot: Dict[str, Any] = {"type": "context"}
        self._attach_context(snapshot)
        snapshot.pop("type", None)
        return snapshot


    def _parse_override_request(self, request: Dict[str, Any]) -> tuple:
        """
        [EN]
        Validate the optional ``n_ctx`` / ``n_gpu_layers`` / ``n_batch`` fields
        submitted by the settings page. Only a sanity bound is enforced here;
        the values themselves are the user's decision and are adopted verbatim
        by the planner.

        [ZH]
        校验设置页提交的可选 ``n_ctx`` / ``n_gpu_layers`` / ``n_batch`` 字段。
        此处只执行合理性边界；取值本身是用户的决定，由规划器原样采纳。
        """
        from config import MAX_CONTEXT_OVERRIDE, MIN_CONTEXT

        values: Dict[str, int] = {}
        if "n_ctx" in request and request.get("n_ctx") is not None:
            try:
                value = int(request["n_ctx"])
            except (TypeError, ValueError):
                return {}, _t("inference.invalid_value", field="n_ctx",
                              error=request.get("n_ctx"))
            if value < MIN_CONTEXT or value > MAX_CONTEXT_OVERRIDE:
                return {}, _t("inference.value_out_of_range", field="n_ctx",
                              low=MIN_CONTEXT, high=MAX_CONTEXT_OVERRIDE)
            values["n_ctx"] = value
        if "n_gpu_layers" in request and request.get("n_gpu_layers") is not None:
            try:
                value = int(request["n_gpu_layers"])
            except (TypeError, ValueError):
                return {}, _t("inference.invalid_value", field="n_gpu_layers",
                              error=request.get("n_gpu_layers"))
            if value < -1 or value > 10000:
                return {}, _t("inference.value_out_of_range", field="n_gpu_layers",
                              low=-1, high=10000)
            values["n_gpu_layers"] = value
        if "n_batch" in request and request.get("n_batch") is not None:
            try:
                value = int(request["n_batch"])
            except (TypeError, ValueError):
                return {}, _t("inference.invalid_value", field="n_batch",
                              error=request.get("n_batch"))
            if value < 1 or value > 65536:
                return {}, _t("inference.value_out_of_range", field="n_batch",
                              low=1, high=65536)
            values["n_batch"] = value
        return values, ""

    def _test_load_inference(self, values: Dict[str, int]) -> Dict[str, Any]:
        """
        [EN]
        Run a throwaway load plus a tiny completion in a **child process**.
        The child owns the model memory, so a 60-second timeout can terminate it
        with the process and release every byte; the live engine is never
        duplicated in this process.

        [ZH]
        在**子进程**中执行一次用完即弃的加载与极短推理。模型内存归子进程所有，
        因此 60 秒超时可以连同进程一起终止并释放全部内存；本进程内绝不会出现
        两份模型同时常驻。
        """
        import subprocess

        path = self._current_model_path()
        if not path:
            return {"ok": False, "error": _t("inference.test_no_model"),
                    "error_type": "NoModel", "suggestions": [], "warnings": []}
        plan = hardware_profiler.plan_inference(
            profile=hardware_profiler.probe(),
            model_path=path,
            train_context=resolve_context_window(path, 0),
            requested_context=int(values.get("n_ctx", 0)),
            max_context=int(self.config.max_context or hardware_profiler.DEFAULT_MAX_CONTEXT),
            requested_gpu_layers=int(values.get("n_gpu_layers", 0)),
            n_threads_override=int(self.inference_config.resolve_n_threads()),
            inference_profile=str(getattr(self.config, "inference_profile", "")),
            requested_n_batch=int(values.get("n_batch", 0)),
            allow_override=True,
        )
        blocking = _override_blocking_error(plan) or _preflight_context_error(plan)
        if blocking:
            return self._inference_failure(plan, RuntimeError(blocking))

        payload = {
            "path": path,
            "n_ctx": int(values.get("n_ctx", 0)),
            "n_gpu_layers": int(values.get("n_gpu_layers", 0)),
            "n_batch": int(values.get("n_batch", 0)),
            "profile": str(getattr(self.config, "inference_profile", "balanced")),
            "threads": int(self.inference_config.resolve_n_threads()),
        }
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--inference-test-load", json.dumps(payload)]
        else:
            command = [sys.executable, os.path.abspath(__file__),
                       "--inference-test-load", json.dumps(payload)]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs: Dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 60,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started = time.time()
        try:
            completed = subprocess.run(command, **kwargs)
        except subprocess.TimeoutExpired:
            timeout_error = TimeoutError(_t("inference.test_timeout", seconds=60))
            return self._inference_failure(plan, timeout_error)
        result: Dict[str, Any] = {}
        for line in reversed((completed.stdout or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                result = candidate
                break
        if completed.returncode != 0 or not result.get("ok"):
            detail = str(result.get("error") or "").strip()
            if not detail:
                tail = (completed.stderr or "").strip().splitlines()[-3:]
                detail = "\n".join(tail) or f"exit code {completed.returncode}"
            return self._inference_failure(plan, RuntimeError(detail))
        return {
            "ok": True,
            "elapsed_seconds": round(time.time() - started, 2),
            "plan": result.get("plan") or plan.to_dict(),
            "answer": str(result.get("answer") or ""),
            "warnings": list(plan.warnings),
        }

    def _handle_capability_settings(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Adjust capability-related settings at runtime (fix 4: presets / sliding window /
        chunked-summary parameters).

        Only whitelisted fields are accepted, and each is range-checked: these parameters
        directly affect inference results, and accepting arbitrary frontend values would easily
        produce a configuration that "looks fine but cannot run".

        v0.2.4 task 3 adds the optional ``n_ctx`` / ``n_gpu_layers`` / ``n_batch``
        override fields and ``test_load``. An override is persisted and applied
        verbatim; a failed load returns the raw error while the previous engine
        parameters are restored, so a working model keeps working.

        [ZH]
        运行期调整能力相关设置（修复四：预设 / 滑动窗口 / 分段摘要参数）。

        只接受白名单字段并逐个做范围校验：这些参数会直接影响推理结果，
        放任前端传什么就写什么，很容易产生"看起来可用但无法运行"的配置。

        v0.2.4 任务-3 增加可选覆盖字段 ``n_ctx`` / ``n_gpu_layers`` / ``n_batch``
        与 ``test_load``。覆盖值会被持久化并原样应用；加载失败时返回原始错误并恢复
        旧引擎参数，使原本可用的模型继续可用。
        """
        changed: List[str] = []
        cfg = self.config
        # [EN] v0.2.6 item 17: remember the context value so a failed reload can
        # [EN] restore it. The interface must never keep showing a window that the
        # [EN] engine refused to allocate.
        # [ZH] v0.2.6 第 17 项：记住原上下文值，使重载失败时可以恢复。界面绝不能
        # [ZH] 继续显示一个引擎拒绝分配的窗口。
        previous_context = int(getattr(cfg, "context_length", 0) or 0)
        values, value_error = self._parse_override_request(request)
        if value_error:
            return {"type": "system", "content": value_error, "ok": False,
                    "error": value_error, "error_type": "ValidationError"}
        if bool(request.get("test_load")):
            result = self._test_load_inference(values)
            return {"type": "system", "content": "", "test_load": result,
                    "inference": self.inference_summary(),
                    "hardware": self.hardware_diagnostics()}

        preset = str(request.get("preset") or "").strip().lower()
        if preset:
            from model_profile import apply_preset

            values_preset = apply_preset(preset)
            if values_preset is None:
                return {"type": "system", "content": _t("sys.preset_not_found", name=preset)}
            cfg.preset = preset
            cfg.temperature = float(values_preset["temperature"])
            cfg.top_p = float(values_preset["top_p"])
            cfg.repeat_penalty = float(values_preset["repeat_penalty"])
            cfg.max_tokens = int(values_preset["max_tokens"])
            changed.append("preset")

        inference_changed = False
        profile_name = str(request.get("inference_profile") or "").strip().lower()
        if profile_name:
            from hardware_profiler import get_inference_preset, normalize_inference_preset

            normalized = normalize_inference_preset(profile_name)
            preset_values = get_inference_preset(normalized)
            if normalized != str(getattr(cfg, "inference_profile", "")):
                cfg.inference_profile = normalized
                changed.append("inference_profile")
                inference_changed = True
            if "n_batch" not in values and not bool(getattr(cfg, "inference_override", False)):
                if int(self.inference_config.n_batch) != int(preset_values.n_batch):
                    self.inference_config.n_batch = int(preset_values.n_batch)
                    self.inference_config.save()
                    inference_changed = True

        if "n_ctx" in values:
            cfg.context_length = int(values["n_ctx"])
            changed.append("n_ctx")
            inference_changed = True
        if "n_gpu_layers" in values:
            self.inference_config.n_gpu_layers = int(values["n_gpu_layers"])
            changed.append("n_gpu_layers")
            inference_changed = True
        if "n_batch" in values:
            self.inference_config.n_batch = int(values["n_batch"])
            changed.append("n_batch")
            inference_changed = True
        if "n_gpu_layers" in values or "n_batch" in values:
            # [EN] Persist hardware overrides in the inference configuration file.
            # [ZH] 硬件覆盖值持久化到推理配置文件。
            self.inference_config.save()
        if values or "allow_override" in request:
            recommendation = self._recommended_plan()
            differs = (
                int(cfg.context_length or 0) != int(recommendation.n_ctx_max)
                or int(self.inference_config.n_gpu_layers) != int(recommendation.n_gpu_layers)
                or int(self.inference_config.n_batch) != int(recommendation.n_batch)
            )
            override = bool(request.get("allow_override", False)) or differs
            if override != bool(getattr(cfg, "inference_override", False)):
                cfg.inference_override = override
                changed.append("inference_override")
                inference_changed = True

        mode = str(request.get("context_mode") or "").strip().lower()
        if mode:
            if mode not in ("summarize", "slide"):
                return {"type": "system", "content": _t("sys.context_mode_invalid", mode=mode)}
            cfg.context_mode = mode
            changed.append("context_mode")

        # [EN] Sampling parameter ranges. The temperature upper bound was widened from 1.0 to
        # [EN] 1.5 (capability extension-1, task 5): the settings slider goes up to 0-1.5, and a
        # [EN] backend that capped at 1.0 produced "drag to the end, save, and it reverts",
        # [EN] which users perceive as a deceptive UI. 1.5 remains a controllable range
        # [EN] (llama.cpp supports up to 2.0, but small models above 1.5 mostly produce
        # [EN] incoherent output, so the limit was set here after weighing the tradeoff).
        # [ZH] 采样参数范围。温度上限从 1.0 放宽到 1.5（后续版本 相关设计）：
        # [ZH] 设置界面的滑块做到 0–1.5，后端只认到 1.0 会导致"拖到头保存后又变回去"，
        # [ZH] 用户会认为界面在误导。1.5 仍是可控范围（llama.cpp 支持到 2.0，
        # [ZH] 但小模型超过 1.5 基本只输出无意义内容，权衡后定在此处）。
        for key, low, high in (("temperature", 0.0, 1.5), ("top_p", 0.0, 1.0),
                               ("repeat_penalty", 0.5, 2.0)):
            if key in request and request.get(key) is not None:
                try:
                    value = float(request[key])
                except (TypeError, ValueError):
                    continue
                setattr(cfg, key, max(low, min(high, value)))
                changed.append(key)

        if request.get("max_tokens") is not None:
            try:
                cfg.max_tokens = max(64, min(8192, int(request["max_tokens"])))
                changed.append("max_tokens")
            except (TypeError, ValueError):
                pass

        for flag in ("clarify_vague", "summary_cache"):
            if flag in request:
                setattr(cfg, flag, bool(request[flag]))
                if flag == "summary_cache":
                    # [EN] Recreated on demand after the switch changes.
                    # [ZH] 开关切换后按需重新创建。
                    self._summary_cache = None
                changed.append(flag)

        summarize = request.get("summarize")
        if isinstance(summarize, dict):
            for key, low, high in (("chunk_chars", 200, 20000),
                                   ("overlap_chars", 0, 4000),
                                   ("max_summary_tokens", 32, 2048),
                                   ("merge_max_tokens", 64, 8192)):
                if key in summarize:
                    try:
                        setattr(cfg.summarize, key, max(low, min(high, int(summarize[key]))))
                        changed.append(f"summarize.{key}")
                    except (TypeError, ValueError):
                        pass
            if "trigger_ratio" in summarize:
                try:
                    cfg.summarize.trigger_ratio = max(0.05, min(0.95, float(summarize["trigger_ratio"])))
                    changed.append("summarize.trigger_ratio")
                except (TypeError, ValueError):
                    pass
            for key in ("enabled", "keep_pointer"):
                if key in summarize:
                    setattr(cfg.summarize, key, bool(summarize[key]))
                    changed.append(f"summarize.{key}")

        if not changed:
            return {"type": "system", "content": _t("sys.nothing_changed")}
        try:
            cfg.save()
        except Exception as exc:  # noqa: BLE001 - report instead of silently dropping
            log.warning("保存能力设置失败", exc_info=True)
            return {"type": "system",
                    "content": _t("inference.save_failed", error=exc),
                    "ok": False, "error": str(exc),
                    "error_type": type(exc).__name__, "suggestions": [],
                    "warnings": [], "changed": changed}
        if inference_changed:
            result = self._rebuild_inference()
            if not result.get("ok"):
                # [EN] v0.2.6 item 17: restore the previous context value so the
                # [EN] persisted configuration and the running engine agree. The
                # [EN] error payload still explains why the requested size failed.
                # [ZH] v0.2.6 第 17 项：恢复原上下文值，使持久化配置与运行中的引擎
                # [ZH] 保持一致。错误负载仍会说明请求值失败的原因。
                if "n_ctx" in values and previous_context > 0:
                    cfg.context_length = previous_context
                    changed = [item for item in changed if item != "n_ctx"]
                    try:
                        cfg.save()
                    except Exception:  # noqa: BLE001 - [EN] the returned error is primary
                                       # [ZH] 返回的错误才是主要信息
                        log.warning("恢复上下文设置后保存失败", exc_info=True)
                return {
                    "type": "system",
                    "content": _t("sys.inference_override_failed",
                                  error=result.get("error") or ""),
                    "ok": False,
                    "error": result.get("error") or "",
                    "error_type": result.get("error_type") or "Error",
                    "suggestions": result.get("suggestions") or [],
                    "warnings": result.get("warnings") or [],
                    "changed": changed,
                    "inference": result,
                    "capability": self.model_capability(),
                    "hardware": self.hardware_diagnostics(),
                    "context": self._context_snapshot(),
                }
        return {"type": "system",
                "content": _t("sys.capability_settings_saved", fields=", ".join(changed)),
                "changed": changed, "capability": self.model_capability(),
                "inference": self.inference_summary(),
                "hardware": self.hardware_diagnostics(),
                "context": self._context_snapshot()}

    def stream(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Streaming chat: yield delta lines one by one and a done line at the end (interruptible
        by stop).

        [ZH]
        流式对话：逐个产出 delta 行，最后产出 done 行（支持 stop 中断）。
        """
        action = request.get("action")
        if action == "inference_tune_stream":
            # [EN] Reader: live hardware benchmark. Every trial is streamed, so the
            # [EN] operator sees the tuning happen instead of a frozen interface.
            # [ZH] Reader：实时硬件基准测试。每次试探都流式上报，操作者看到的是调优过程，
            # [ZH] 而不是一个卡住的界面。
            yield from self._stream_inference_tune(request)
            return
        if action == "file_stream":
            # [EN] Multi-file (task 8): the files array takes precedence; stage=true means
            # [EN] "parse only, do not summarize".
            # [ZH] 多文件（相关设计）：files 数组优先；stage=true 表示"只解析不摘要"。
            paths = request.get("files")
            if isinstance(paths, list) and paths:
                yield from self._stream_files([str(p) for p in paths],
                                              stage=bool(request.get("stage")))
                return
            yield from self._stream_file(str(request.get("path") or ""))
            return

        user_msg = request.get("message", "")

        # [EN] Reader: no external provider can serve the turn -- the local GGUF engine
        # [EN] is the only inference path by construction.
        # [ZH] Reader：本地 GGUF 引擎是构造上唯一的推理路径，本层不存在外部提供方。
        # [EN] Task 1: provide guidance when no model is available instead of raising an
        # [EN] exception or letting the frontend see "inference failed".
        # [ZH] 相关设计：无模型时提供引导，而不是抛出异常或让前端看到"推理失败"。
        if not self.model_ready:
            guide = self.guide_text()
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", guide)
            yield {"type": "assistant", "content": guide, "model_ready": False, "guide": True}
            done: Dict[str, Any] = {"type": "done", "content": guide,
                                    "model_ready": False, "guide": True}
            self._attach_context(done)
            yield done
            return

        # [EN] Vague-question guidance (system-prompt rework in an earlier release): uses
        # [EN] the same check as _handle_chat and **must also intercept on the streaming
        # [EN] path** -- normal frontend conversations use chat_stream, and this branch
        # [EN] previously existed only in _handle_chat (non-streaming actions and slash
        # [EN] commands), so "offer directions for vague questions" never took effect in the
        # [EN] GUI (a pre-existing defect found by this round's integration tests).
        # [ZH] 模糊提问引导（此前版本 系统提示词重构）：与 _handle_chat 使用同一判断，
        # [ZH] **必须在流式路径也拦截一次** —— 前端正常对话走 chat_stream，而该分支
        # [ZH] 此前只写在 _handle_chat（非流式 action 与斜杠指令）中，因此"模糊提问给出方向"
        # [ZH] 在图形界面中从未生效（本轮集成测试发现的既有缺陷）。
        vague_guide = self._vague_guide(user_msg)
        if vague_guide:
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", vague_guide)
            self._compact_history()
            yield {"type": "delta", "channel": _CH_ANSWER, "content": vague_guide}
            vague_done: Dict[str, Any] = {"type": "done", "content": vague_guide,
                                          "clarify": True}
            self._attach_context(vague_done)
            yield vague_done
            return


        # [EN] Reader: the web-search layer is physically removed, so the context is
        # [EN] built from the conversation and the uploaded document only.
        # [ZH] Reader：联网搜索层已物理删除，上下文只由对话与已上传文档构成。
        search_context = ""
        # [EN] Task 1 (self-test fix-1): **the gate "the question mentions a file" is removed**.
        # [EN] The old form `bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)`
        # [EN] caused "the model does not see the file content after a file is uploaded": for
        # [EN] questions such as "summarize this", "analyze this", or "who is the counterparty"
        # [EN] no FILE_INTENT keyword matches, so not a single character of the file body
        # [EN] entered the context; the model saw only the file name in the system prompt and
        # [EN] truthfully answered that no content was visible. The retrieval path had already
        # [EN] reached the same conclusion (see _retrieval_context_for: that gate prevented
        # [EN] unnamed questions such as "what is the penalty rate" from retrieving), but the
        # [EN] full-text injection path had not followed. The old
        # [EN] `bool(self.conv.file_content)` is also wrong for large files: a body above
        # [EN] files.disk_threshold_chars is spilled to disk and entry.content is cleared, so
        # [EN] the gate was permanently false. The rule is now "inject whenever a file exists",
        # [EN] and the length is still bounded by the files.total_chars budget.
        # [ZH] 相关设计（自测问题修复-1）：**不再以"问题中是否提到文件"作为注入开关**。
        # [ZH] 旧写法 `bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)`
        # [ZH] 直接造成"上传文件后模型回答看不到文件内容"：用户提问「帮我总结一下」
        # [ZH] 「分析一下」「甲方是谁」时不包含任何 FILE_INTENT 关键词，文件正文便一个字
        # [ZH] 都未进入上下文，模型只能看到系统提示中的文件名，于是如实回答"没有看到内容"。
        # [ZH] 检索路径早已得出同一结论（见 _retrieval_context_for 的说明：该开关会使
        # [ZH] "违约金比例是多少"这类不点名的提问检索不到），只是全文注入这条路径未同步。
        # [ZH] 旧写法中的 `bool(self.conv.file_content)` 对大文件同样不正确：正文超过
        # [ZH] files.disk_threshold_chars 会落盘、entry.content 被清空，开关随之恒为假。
        # [ZH] 现在改为"有文件即注入"，长度仍由 files.total_chars 预算约束。
        include_file = self.conv.has_file()
        messages = self.conv.build_messages(user_msg, search_context,
                                            include_file=include_file)
        # [EN] Task 10 (self-test fix-1): record how large **the messages actually sent this
        # [EN] turn** were.
        # [ZH] 相关设计（自测问题修复-1）：记录**本轮真正发出的消息**的大小。
        self._note_request_size(messages)
        # [EN] v0.2.6 task 13: route the turn to the active external provider instead of
        # [EN] the local engine. History, files and retrieval were already assembled by
        # [EN] ``build_messages``, so switching providers does not change chat semantics.
        # [ZH] v0.2.6 任务 13：把本轮路由到生效的外部提供方，而不是本地引擎。历史、
        # [ZH] 文件与检索已由 ``build_messages`` 组装完成，因此切换提供方不改变对话语义。
        # [EN] Task 2 (known-issues optimization-2): when search is enabled but no usable
        # [EN] result was obtained, **tell the user explicitly** instead of continuing silently
        # [EN] -- otherwise the user assumes "search did not work" or "the model ignored the
        # [EN] results", when in fact this layer obtained nothing. The notice is emitted as the
        # [EN] first paragraph prefix of the answer area.
        # [ZH] 相关设计（优化已知问题-2）：搜索已开启但未取得可用结果时，**明确告知用户**，
        # [ZH] 而不是静默继续 —— 否则用户会认为"搜索未生效"或"模型忽略了搜索结果"，
        # [ZH] 实际是该层未取得任何内容。该提示作为回答区的首段前缀发出。
        parts: list = []
        # [EN] Reasoning content and the answer travel on two channels (task 3): the channel
        # [EN] names are agreed with the frontend in the module header protocol description.
        # [ZH] 思考内容与回答走两条通道（相关设计）：通道名与前端约定见模块头协议说明。
        splitter = self.llm.make_splitter() if hasattr(self.llm, "make_splitter") else None
        # [EN] Runtime metrics (known-issues optimization-2, task 7): measured by the engine and
        # [EN] reported with heartbeats and on done.
        # [ZH] 运行指标（优化已知问题-2 相关设计）：由引擎验证，随心跳与 done 上报。
        stats = StreamStats()
        heartbeat = float(getattr(self.config, "stream_heartbeat_seconds", 1.5) or 1.5)
        try:
            for channel, piece in self.llm.chat_stream_split(
                    messages,
                    temperature=float(getattr(self.config, "temperature", 0.7) or 0.7),
                    max_tokens=self._output_token_budget(),
                    stop_event=self.stop_event,
                    top_p=float(getattr(self.config, "top_p", 0.9) or 0.9),
                    repeat_penalty=float(getattr(self.config, "repeat_penalty", 1.1) or 1.1),
                    splitter=splitter,
                    stats=stats,
                    heartbeat_seconds=heartbeat):
                # [EN] The stop check is placed **after** yielding a piece (known-issues
                # [EN] optimization-2, task 5). Placing it at the top of the loop would discard
                # [EN] the piece the model has already generated and which only awaits
                # [EN] delivery: when the stop flag is set there are usually one or two pieces
                # [EN] already queued in the engine (its reader thread is faster than the
                # [EN] consumer), and dropping them produces the inconsistency "reasoning
                # [EN] content that the UI had already displayed before stop is missing from
                # [EN] done" -- an existing regression test asserts exactly this. Placed after
                # [EN] yielding: content already received is delivered as usual, then the loop
                # [EN] stops immediately without waiting for the model to finish.
                # [ZH] 停止检查放在**产出之后**（优化已知问题-2 相关设计）。
                # [ZH] 放在循环开头会丢弃"模型已经生成、只差交付"的那一块内容：
                # [ZH] 停止信号置位时往往已有一两块排在引擎队列中（引擎的读者线程
                # [ZH] 比消费者快），丢弃它们会出现"停止前界面上已显示过的思考内容
                # [ZH] 在 done 中却不存在"这种不一致 —— 既有回归用例正好断言了这一点。
                # [ZH] 放在产出之后：已取得的内容照常交付，随后立即停止，不再等待模型跑完。
                if channel == _CH_HEARTBEAT:
                    # [EN] Task 5: during prefill (before the first token) there may be no
                    # [EN] content for tens of seconds. The heartbeat lets the UI distinguish
                    # [EN] "still working" from "stuck", and refreshes the live metrics at the
                    # [EN] same time.
                    # [ZH] 相关设计：prefill 阶段（首 token 之前）可能数十秒没有任何内容。
                    # [ZH] 心跳使界面能够区分"仍在工作"与"已停滞"，并同时刷新实时指标。
                    yield {"type": "progress", "channel": _CH_HEARTBEAT,
                           "stats": stats.to_dict(),
                           "waiting_first_token": not stats.prefill_done}
                elif channel == _CH_REASONING:
                    yield {"type": "delta", "channel": _CH_REASONING, "content": piece}
                elif channel == _CH_RESET:
                    # [EN] Orphan closing tag: everything sent to the answer area so far is in
                    # [EN] fact reasoning. After receiving reset, the frontend moves the text
                    # [EN] already present in the answer area into the reasoning area; the
                    # [EN] backend does not need to resend it.
                    # [ZH] 孤立结束标签：此前发送到回答区的内容实际上都是思考内容。前端收到
                    # [ZH] reset 后把回答区已有文本"搬"到思考区，后端无需重发。
                    yield {"type": "delta", "channel": _CH_ANSWER,
                           "reset": True, "content": ""}
                else:
                    parts.append(piece)
                    yield {"type": "delta", "channel": _CH_ANSWER, "content": piece}
                if self.stop_event.is_set():
                    break
            # [EN] The splitter's own accumulated answer text is the authoritative result: when
            # [EN] an orphan closing tag appears it has already reclassified "content
            # [EN] mistakenly emitted to the answer area" as reasoning, so a second accumulator
            # [EN] must not be kept here (otherwise done.content would carry the reasoning
            # [EN] content too -- exactly what test C2 caught).
            # [ZH] 以分离器自身的回答累积串作为权威结果：出现孤立结束标签时它已把
            # [ZH] "此前误发到回答区的内容"改判给思考区，此处不能再另存一份累积串
            # [ZH] （否则 done.content 会把思考内容一并带上，测试 C2 正是这样发现的）。
            full = splitter.answer_text if splitter is not None else "".join(parts)
            thinking = splitter.reasoning_text.strip() if splitter is not None else ""
            # [EN] P1 invariant: an empty model answer is a reportable outcome, not a silent
            # [EN] success. Without this notice the interface renders an empty assistant bubble
            # [EN] and the user cannot tell whether generation is still running or failed.
            # [ZH] P1 不变量：模型回答为空属于必须如实报告的结局，不能当作静默成功。缺少这段
            # [ZH] 提示时界面会渲染一个空的助手气泡，用户无法判断是仍在生成还是已经失败。
            if not full.strip() and not thinking.strip() and not self.stop_event.is_set():
                full = _t("sys.empty_output")
                yield {"type": "delta", "channel": _CH_ANSWER, "content": full}
            # [EN] Persist even on interruption: task 5 requires that "an aborted reasoning
            # [EN] process leaves no partial content in the answer area". The answer area stores
            # [EN] only the answer, and the reasoning is stored in a separate field, so the
            # [EN] frontend can clean up accordingly.
            # [ZH] 中断时也要落库：设计规范相关设计要求"思考过程中止不残留半截内容到回答区"，
            # [ZH] 回答区只存回答、思考单独存字段，前端据此清理即可。
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", full, reasoning=thinking)
            self._compact_history()
            done: Dict[str, Any] = {"type": "done", "content": full,
                                    "stopped": self.stop_event.is_set()}
            # [EN] Runtime metrics (task 7): the output token count is measured precisely with
            # [EN] the **model's real tokenizer** (the engine only performs a coarse fallback
            # [EN] estimate in _finish_stats and sets tokens_estimated accordingly). The text
            # [EN] obtained here is the complete text (including reasoning), consistent with
            # [EN] "how much the model actually generated".
            # [ZH] 运行指标（相关设计）：输出 token 数使用**模型真实分词器**精确统计（引擎在
            # [ZH] _finish_stats 中只做兜底粗估，并已置位 tokens_estimated）。
            # [ZH] 此处取得的是完整文本（含思考），与"模型实际生成了多少"口径一致。
            try:
                counted_text = (thinking + full) if thinking else full
                tokens, estimated = self.llm.count_output_tokens(counted_text)
                stats.tokens = tokens
                stats.tokens_estimated = estimated
                if stats.elapsed_ms > 0 and tokens:
                    stats.tps = tokens / (stats.elapsed_ms / 1000.0)
            except Exception:
                log.debug("输出 token 计数失败（保留粗估）", exc_info=True)
            stats.stopped = self.stop_event.is_set()
            done["stats"] = stats.to_dict()
            if thinking:
                done["reasoning"] = thinking
            if splitter is not None and splitter.unclosed and not self.stop_event.is_set():
                # [EN] The tag did not close, and this is not a user-initiated stop (the
                # [EN] frontend already shows "generation stopped" when the user stops, so
                # [EN] additionally reporting "length limit exhausted" would be misleading):
                # [EN] this usually means the token budget was consumed by reasoning.
                # [ZH] 标签未闭合，且不是用户主动停止（用户停止时前端已有"已停止生成"提示，
                # [ZH] 再报"长度上限用完"会造成误导）：通常意味着 token 预算被思考耗尽。
                done["reasoning_unclosed"] = True
            # [EN] Warn when throughput drops noticeably after the context is extended (a
            # [EN] larger window means a larger KV cache and slower inference).
            # [ZH] 扩展上下文后速度明显下降时给出提示（窗口越大 KV Cache 越大、越慢）。
            if self.config.speed_hint and not self.stop_event.is_set():
                hint = self.llm.speed_hint(self.config.speed_min_tps)
                if hint:
                    done["content"] = f"{full}\n\n{hint}" if full else hint
                    _write({"type": "delta", "channel": _CH_ANSWER, "content": f"\n\n{hint}"})
            # [EN] Attach the current context usage so that the front end can refresh.
            # [ZH] 附带当前上下文占用，供前端刷新。
            self._attach_context(done)
            yield done
        except Exception as e:
            log.exception("流式推理失败")
            yield {"type": "error", "content": _t("sys.inference_failed", error=e)}

    # [EN] ---------- Open platform: local HTTP API and provider routing (v0.2.6 Part 4) ----------
    # [ZH] ---------- 开放平台：本地 HTTP API 与多 Provider 路由（v0.2.6 任务 4） ----------
















    def _vague_guide(self, user_msg: str) -> str:
        """
        [EN]
        The **guidance text** to return when the question is too vague; return an empty string
        when it should not be intercepted.

        The original mechanism (fix 4, direction 7) was "ask a follow-up question requesting
        material"; the v0.2.1 system-prompt rework upgraded it to **guidance**: list 2-3
        possible directions, each with a one-line approach, and let the user reply with a
        number.

        It triggers only when there is neither file context nor history: with context present,
        "what should I do about this" usually refers to the preceding content, and guidance
        would be redundant.

        **One case is not intercepted**: ``/task guide`` (the user explicitly wants the model
        to list directions). The predicate is "the task layer is the guide template".

        It is a method because **the two call sites must agree**: an earlier round of
        integration testing found that this check existed only in `_handle_chat`, while normal
        frontend conversations use `chat_stream` from `stream()` -- in other words, "offer
        directions / ask follow-up for vague questions" never took effect in the GUI (a
        historical defect, see records/v0.2.1-系统提示词重构记录.md). The fix is not to copy
        the code to a second site, but to keep only this one copy.

        [ZH]
        提问过于模糊时应返回的**引导话术**；不应拦截时返回空串。

        修复四（方向七）的原本机制是"先追问要材料"，v0.2.1 系统提示词重构将其升级为
        **引导**：列出 2~3 种可能的方向、每种配一句做法，让用户回复一个编号。

        仅在"没有文件上下文、也没有历史"时触发：已有上下文时"这个怎么办"
        往往指代上文内容，引导反而多余。

        **一种情况下不拦截**（能力扩展-1 任务一之后，guide 不再是模式而是任务模板）：
        ``/task guide``（用户明确要模型自行列出方向）。判据是"任务层就是 guide 模板"。

        抽成方法是因为**两个调用点必须一致**：上一轮集成测试发现该判断原先只写在
        `_handle_chat` 中，而前端正常对话走的是 `stream()` 的 `chat_stream` ——
        也就是说"模糊提问给方向/追问"在图形界面中从未生效（历史遗留缺陷，
        见 records/v0.2.1-系统提示词重构记录.md）。修法不是把代码复制到第二个位置，
        而是只保留这一份。
        """
        if not bool(getattr(self.config, "clarify_vague", True)):
            return ""
        if getattr(self.conv, "task_template", "") == "guide":
            return ""
        if self.conv.history or self.conv.file_content:
            return ""
        from prompts import clarification_prompt, is_vague_question

        if not is_vague_question(user_msg):
            return ""
        return clarification_prompt(user_msg)

    def _handle_chat(self, user_msg: str) -> Dict[str, Any]:
        # [EN] Reader: the slash-command registry is physically removed. A leading "/"
        # [EN] is treated as ordinary text, so the model sees exactly what the user
        # [EN] typed instead of a silently swallowed command.
        # [ZH] Reader：斜杠指令表已物理删除。以 "/" 开头的内容按普通文本处理，模型看到的
        # [ZH] 就是用户原样输入，而不会被静默吞掉。
        # [EN] Task 1: with no model, skip inference and return guidance directly (and record
        # [EN] it in history so the user sees the context after switching back).
        # [ZH] 相关设计：无模型时不走推理，直接给出引导（并记入历史，用户切回后能看到上下文）。
        if not self.model_ready:
            guide = self.guide_text()
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", guide)
            self._compact_history()
            return {"type": "assistant", "content": guide, "model_ready": False, "guide": True}

        # [EN] Intent short-circuit: when the user asks "what did I just say", quote the most
        # [EN] recent input directly to prevent small models from confusing the perspective.
        # [ZH] 意图短路：用户询问"我刚才说了什么"时直接引用最近输入，避免小模型混淆视角。
        last_user = self.conv.last_user_message()
        if last_user and REPEAT_INTENT.search(user_msg):
            answer = _t("sys.repeat_intent", text=last_user)
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", answer)
            self._compact_history()
            return {"type": "assistant", "content": answer}

        # [EN] Fix 4 (direction 7) plus the system-prompt rework of an earlier release: when
        # [EN] the question is too vague, give **guidance** first (list 2-3 possible
        # [EN] directions for the user to choose), rather than answering blindly or requesting
        # [EN] material. The predicate lives in _vague_guide, shared by the streaming path
        # [EN] (normal frontend conversations) and the non-streaming path.
        # [ZH] 兼容性修正（方向七）+ 此前版本 系统提示词重构：提问过于模糊时先给出**引导**
        # [ZH] （列出 2~3 种可能方向供用户选择），而不是强行作答或反向索取材料。
        # [ZH] 判断本体在 _vague_guide 中 —— 流式路径（前端正常对话）与非流式路径共用同一份。
        guide = self._vague_guide(user_msg)
        if guide:
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", guide)
            self._compact_history()
            return {"type": "assistant", "content": guide, "clarify": True}

        # [EN] Reader: the web-search layer is physically removed, so the context is
        # [EN] built from the conversation and the uploaded document only.
        # [ZH] Reader：联网搜索层已物理删除，上下文只由对话与已上传文档构成。
        search_context = ""
        # [EN] Task 1 (self-test fix-1): **the gate "the question mentions a file" is removed**.
        # [EN] The old form `bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)`
        # [EN] caused "the model does not see the file content after a file is uploaded": for
        # [EN] questions such as "summarize this", "analyze this", or "who is the counterparty"
        # [EN] no FILE_INTENT keyword matches, so not a single character of the file body
        # [EN] entered the context; the model saw only the file name in the system prompt and
        # [EN] truthfully answered that no content was visible. The retrieval path had already
        # [EN] reached the same conclusion (see _retrieval_context_for: that gate prevented
        # [EN] unnamed questions such as "what is the penalty rate" from retrieving), but the
        # [EN] full-text injection path had not followed. The old
        # [EN] `bool(self.conv.file_content)` is also wrong for large files: a body above
        # [EN] files.disk_threshold_chars is spilled to disk and entry.content is cleared, so
        # [EN] the gate was permanently false. The rule is now "inject whenever a file exists",
        # [EN] and the length is still bounded by the files.total_chars budget.
        # [ZH] 相关设计（自测问题修复-1）：**不再以"问题中是否提到文件"作为注入开关**。
        # [ZH] 旧写法 `bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)`
        # [ZH] 直接造成"上传文件后模型回答看不到文件内容"：用户提问「帮我总结一下」
        # [ZH] 「分析一下」「甲方是谁」时不包含任何 FILE_INTENT 关键词，文件正文便一个字
        # [ZH] 都未进入上下文，模型只能看到系统提示中的文件名，于是如实回答"没有看到内容"。
        # [ZH] 检索路径早已得出同一结论（见 _retrieval_context_for 的说明：该开关会使
        # [ZH] "违约金比例是多少"这类不点名的提问检索不到），只是全文注入这条路径未同步。
        # [ZH] 旧写法中的 `bool(self.conv.file_content)` 对大文件同样不正确：正文超过
        # [ZH] files.disk_threshold_chars 会落盘、entry.content 被清空，开关随之恒为假。
        # [ZH] 现在改为"有文件即注入"，长度仍由 files.total_chars 预算约束。
        include_file = self.conv.has_file()
        messages = self.conv.build_messages(user_msg, search_context,
                                            include_file=include_file)
        # [EN] Task 10 (self-test fix-1): this path must also record "how much was actually sent
        # [EN] this turn". It is the **non-streaming** answer path; omitting it leaves the status
        # [EN] area showing the result of the previous streaming request -- exactly the class of
        # [EN] "display does not match reality" that task 10 eliminates.
        # [ZH] 相关设计（自测问题修复-1）：这条路径同样需要记录"本轮实际发出多少"。
        # [ZH] 它是**非流式**回答路径，缺了它则状态区数字会一直停在上一次流式请求的结果上
        # [ZH] —— 这正是相关设计要消除的那类"显示与真实不符"。
        self._note_request_size(messages)
        try:
            thinking, response = self.llm.chat_complete_split(
                messages,
                temperature=float(getattr(self.config, 'temperature', 0.7) or 0.7),
                max_tokens=self._output_token_budget(),
                top_p=float(getattr(self.config, "top_p", 0.9) or 0.9),
                repeat_penalty=float(getattr(self.config, "repeat_penalty", 1.1) or 1.1),
            )
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", response, reasoning=thinking)
            self._compact_history()
            result: Dict[str, Any] = {"type": "assistant", "content": response}
            if thinking:
                # [EN] The same content is delivered on two channels (task 4): the UI displays
                # [EN] the reasoning collapsed and shows only the answer.
                # [ZH] 同一份内容分别提供给两条通道（相关设计）：界面折叠展示思考、只显示答案。
                result["reasoning"] = thinking
            self._attach_context(result)
            return result
        except Exception as e:
            log.exception("推理失败")
            return {"type": "system", "content": _t("sys.inference_failed", error=e)}

    # =====================================================================
    # [EN] Task 17: reuse an already-open browser and two-phase visual selection (self-test
    # [EN] fix-1)
    # [ZH] 相关设计：复用已打开的浏览器 + 两段式可视化选取（自测问题修复-1）
    # =====================================================================

    #: [EN] Persistence field for the "login check element" in two-phase selection.
    #:
    #: [EN] It **must be a class attribute and accessed via ``self.``**: a bare name inside
    #: [EN] a method body follows LEGB and cannot see the class scope. The mapping table of
    #: [EN] the existing ``_apply_picked_selector`` has no login_check entry, so it would
    #: [EN] **silently discard** this selector even though the configuration does have
    #: [EN] ``login_check_selector``; the mapping is added here.
    #: [ZH] 两段式选取中"登录检测元素"的落盘字段。
    #:
    #: [ZH] **必须是类属性、且通过 ``self.`` 访问**：方法体中的裸名字走 LEGB，找不到类作用域。
    #: [ZH] 既有 ``_apply_picked_selector`` 的映射表没有 login_check，会**静默丢弃**该选择器，
    #: [ZH] 而配置中确实有 ``login_check_selector`` 这一项，因此在此补充映射。
    _PICK_EXTRA_FIELDS: Dict[str, str] = {"login_check": "login_check_selector"}

    # [EN] This group consists of **additional** handlers; no existing function body was
    # [EN] changed.
    # [ZH] 这一组是**新增**的处理器，没有改动任何既有函数的函数体。


    def _handle_file(self, filepath: str) -> Dict[str, Any]:
        """
        [EN]
        Load one file into context and always answer with a structured result.

        [ZH]
        将单个文件载入上下文，并始终返回结构化结果。
        """
        if not filepath:
            return {"type": "system", "content": _t("sys.file_need_path")}
        try:
            return {"type": "system", "content": self.load_file_into_context(filepath)}
        except Exception as exc:  # noqa: BLE001
            log.exception("加载文件失败: %s", filepath)
            return {"type": "error", "ok": False,
                    "content": _t("sys.file_load_failed", error=exc)}

    # [EN] ---------- Multiple files (design specification) ----------
    # [ZH] ---------- 多文件（相关设计） ----------

    def _files_cfg(self):
        """
        [EN]
        Return the file-handling configuration section.

        [ZH]
        多文件配置（老配置文件没有这一段时回落到默认值）。
        """
        return getattr(self.config, "files", None) or FilesConfig()

    def _disk_store_dir(self) -> str:
        """
        [EN]
        Return the directory used for large file storage.

        [ZH]
        大文件正文的落盘目录（``results`` 同级的 ``file_cache``）。

        放在程序目录下而不是临时目录：用户可能要按上下文里的指针去回查原文，
        临时目录会被系统清理，指针就失效了。
        """
        base = os.path.join(user_data_root(), "file_cache")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            log.warning("无法创建文件缓存目录: %s", base, exc_info=True)
        return base

    def _store_large_file(self, name: str, content: str) -> str:
        """
        [EN]
        Store large file content on disk and return a pointer.

        [ZH]
        把超大文件正文落盘，返回落盘路径（失败时返回空串，调用方按内存处理）。
        """
        try:
            safe = re.sub(r"[^\w.\-]+", "_", name or "file")[:80] or "file"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(self._disk_store_dir(), f"{stamp}-{safe}.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            log_user(log, "sys.log_large_file_stored", path=path, chars=len(content))
            return path
        except Exception:
            log.warning("大文件落盘失败，改为在内存中保留", exc_info=True)
            return ""

    # [EN] ---------- Asynchronous document jobs (v0.2.6 tasks 2.1 / 2.2) ----------
    # [ZH] ---------- 文档后台作业（v0.2.6 任务 2.1 / 2.2） ----------

    def _handle_file_async(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Accept an upload and enqueue background parsing plus summarization.

        The response is produced before any parsing work starts: the caller
        receives the job identifier immediately and may continue chatting. The
        final summary is injected into the main conversation only when the job
        completes, and only as a single system message.

        [ZH]
        接收上传并将解析与摘要排入后台队列。

        应答在任何解析工作开始前产生：调用方立即取得作业标识并可继续对话。
        最终摘要仅在作业完成时注入主对话，且只作为一条系统消息。
        """
        raw = request.get("files")
        if not isinstance(raw, list) or not raw:
            raw = [request.get("path")] if request.get("path") else []
        paths = [str(item) for item in raw if str(item or "").strip()]
        if not paths:
            return {"type": "system", "content": _t("sys.file_need_path")}
        try:
            job = self._document_jobs.submit(paths)
        except Exception as exc:  # noqa: BLE001
            log.exception("提交后台文件作业失败")
            return {"type": "error", "ok": False,
                    "content": _t("sys.file_load_failed", error=exc)}
        return {"type": "system", "ok": True, "accepted": True, "job_id": job.id,
                "status": job.status, "count": len(paths),
                "content": _t("sys.file_async_accepted", count=len(paths),
                              job_id=job.id)}

    def _handle_file_job_status(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Return the current state of one background document job, together with
        the events appended after the caller-supplied cursor.

        [ZH]
        返回单个后台文档作业的当前状态，以及调用方游标之后追加的事件。
        """
        job_id = str(request.get("job_id") or "")
        job = self._document_jobs.get(job_id)
        if job is None:
            return {"type": "error", "ok": False,
                    "content": _t("sys.file_job_not_found", job_id=job_id)}
        cursor = int(request.get("cursor") or 0)
        snapshot = job.snapshot(cursor)
        return {"type": "file_job", "ok": True, "content": "",
                "job_id": job.id, "job": snapshot,
                "events": snapshot["events"], "cursor": snapshot["cursor"]}

    def _run_document_job(self, job: Any) -> None:
        """
        [EN]
        Background runner for one document job.

        Invariants:
        * The main conversation is never touched while parsing or summarizing.
          Chunk prompts are recorded in a private temporary conversation object
          (task 2.2), and the final summary is committed as one system message.
        * A cancellation request is observed between files and inside the
          summarizer, so a long document stops promptly.
        * A parse failure for one file does not abort the remaining files.

        [ZH]
        单个文档作业的后台执行函数。

        不变量：
        * 解析与摘要期间绝不触碰主对话。分段提示词记录在私有临时会话对象中
          （任务 2.2），最终摘要以一条系统消息提交。
        * 取消请求在文件之间与摘要器内部均被检查，长文档可及时停止。
        * 单份文件解析失败不会中止其余文件。
        """
        cfg = self._files_cfg()
        limit = int(getattr(cfg, "max_count", 10) or 10)
        accepted = list(job.paths[:limit])
        ignored = list(job.paths[limit:])
        total = len(accepted)
        outcomes: List[Dict[str, Any]] = []
        if ignored:
            job.emit({"type": "file_status", "phase": "ignored",
                      "content": _t("sys.files_too_many", limit=limit)})
        if not accepted:
            job.status = STATUS_CANCELLED
            job.error = "no_accepted_paths"
            return

        for index, path in enumerate(accepted, 1):
            if job.cancel_event.is_set():
                job.status = STATUS_CANCELLED
                return
            name = os.path.basename(path) or path
            job.emit({"type": "file_status", "phase": "start", "index": index,
                      "total": total, "name": name,
                      "content": _t("sys.file_status_start", index=index,
                                    total=total, name=name)})
            try:
                content = parse_file_with_wps(path, self.config.file_max_chars)
            except Exception as exc:  # noqa: BLE001 - one file must not abort the job
                log.exception("后台解析文件失败: %s", path)
                outcome = {"ok": False, "name": name, "chars": 0,
                           "message": _t("sys.file_load_failed", error=exc)}
                outcomes.append(outcome)
                job.emit({"type": "file_status", "phase": "failed", "index": index,
                          "total": total, "name": name, "ok": False,
                          "content": outcome["message"]})
                continue
            if is_parse_error(content):
                outcome = {"ok": False, "name": name, "chars": 0,
                           "message": f"❌ {localize_error(content)}"}
                outcomes.append(outcome)
                job.emit({"type": "file_status", "phase": "failed", "index": index,
                          "total": total, "name": name, "ok": False,
                          "content": outcome["message"]})
                continue

            chars = len(content)
            try:
                tokens = self.llm.count_tokens(content)
            except Exception:
                tokens = chars // 2
            # [EN] Reader: the window is sized from this document. A file beyond the
            # [EN] 128K hard ceiling is rejected explicitly instead of being silently
            # [EN] truncated, so the user can split it and retry.
            # [ZH] Reader：窗口按本文档分配。超过 128K 硬上限的文件会被明确拒绝，而不是
            # [ZH] 静默截断，使用户可以拆分后重试。
            exceeds = getattr(self.llm, "document_exceeds_capacity", None)
            if callable(exceeds) and exceeds(tokens):
                too_long = _t("sys.file_too_long", name=name, tokens=tokens,
                              limit=MAX_DYNAMIC_CONTEXT)
                outcomes.append({"ok": False, "name": name, "chars": chars,
                                 "tokens": tokens, "message": too_long})
                job.emit({"type": "file_status", "phase": "failed", "index": index,
                          "total": total, "name": name, "ok": False,
                          "content": too_long})
                continue
            try:
                self.llm.set_document_tokens(tokens)
            except Exception:  # noqa: BLE001 - allocation must not abort the job
                log.debug("文档上下文分配失败（沿用当前窗口）", exc_info=True)
            stored = ""
            threshold = int(getattr(cfg, "disk_threshold_chars", 0) or 0)
            if threshold > 0 and chars > threshold:
                stored = self._store_large_file(name, content)
            effective_ctx = int(getattr(self.llm, "max_n_ctx", 0)
                                or getattr(self.llm, "n_ctx", 0) or 0)
            # [EN] Budget routing (v0.2.6 item 17): under 70% of the user's window
            # [EN] the body is injected in full and summary is never attempted.
            # [ZH] 预算路由（v0.2.6 第 17 项）：低于用户窗口 70% 时直接全量注入，
            # [ZH] 绝不尝试摘要。
            budget_tokens = int(effective_ctx * _LONG_TEXT_FULL_INJECT_RATIO)
            needs_summary_no_model = (effective_ctx > 0 and tokens >= budget_tokens
                                      and not self.model_ready)
            summary_text = ""
            summary_meta: Dict[str, Any] = {}
            result = None
            if (self.model_ready and effective_ctx > 0
                    and tokens >= budget_tokens):
                # [EN] Tell the user why summarization is being used before the
                # [EN] long-running model calls begin (v0.2.6 item 17).
                # [ZH] 在长时间模型调用开始前告知用户为何启用摘要（v0.2.6 第 17 项）。
                job.emit({"type": "file_status", "phase": "summarizing",
                          "index": index, "total": total, "name": name,
                          "content": _t("sys.file_summary_mode", name=name,
                                        tokens=tokens, n_ctx=effective_ctx)})
                # [EN] Task 2.2: a private conversation object owns every
                # [EN] intermediate prompt. It is discarded when the job ends.
                # [ZH] 任务 2.2：私有会话对象承载全部中间提示词，作业结束时丢弃。
                temporary_conversation = ConversationManager(1, "chat")
                result = self.summarizer.summarize(
                    content, source_name=name, force=True,
                    on_progress=job.emit,
                    should_stop=job.cancel_event.is_set,
                    conversation=temporary_conversation,
                )
                if result.aborted or job.cancel_event.is_set():
                    job.status = STATUS_CANCELLED
                    return
                if result.summarized:
                    summary_text = result.text
                    summary_meta = {
                        "chunks": result.chunk_count,
                        "origin_chars": result.origin_chars,
                        "pointer": result.pointer,
                        "status": result.status,
                        "async": True,
                    }
            if job.cancel_event.is_set():
                job.status = STATUS_CANCELLED
                return

            # [EN] v0.2.6 item 17: a genuine summarization failure (not a
            # [EN] concurrency lock) means the file cannot be represented within
            # [EN] the current window. Report exactly that and do not register the
            # [EN] file, so the interface never shows a fake success. The user can
            # [EN] raise the context window or upload in batches and retry.
            # [ZH] v0.2.6 第 17 项：真正的摘要失败（非并发锁）意味着该文件无法在当前
            # [ZH] 窗口内表达。如实报告该结论且不登记文件，界面绝不显示假成功。用户可
            # [ZH] 调大上下文或分批次上传后重试。
            if (result is not None and result.status == "failed"
                    and not _summary_failure_is_transient(result)):
                final_text = _t("sys.file_exceeds_context",
                                name=name, tokens=tokens, n_ctx=effective_ctx)
                outcomes.append({"ok": False, "name": name, "chars": chars,
                                 "tokens": tokens, "message": final_text})
                job.emit({"type": "file_status", "phase": "failed", "index": index,
                          "total": total, "name": name, "ok": False,
                          "content": final_text})
                continue

            if summary_text:
                final_text = (_t("sys.file_summary_mode", name=name, tokens=tokens,
                                 n_ctx=effective_ctx) + "\n" + summary_text)
            elif needs_summary_no_model:
                # [EN] The file needs a summary but no model is loaded: keep the
                # [EN] parsed body and tell the user how to proceed instead of
                # [EN] claiming it was fully injected.
                # [ZH] 文件需要摘要但当前没有模型：保留已解析正文并说明如何继续，
                # [ZH] 而不是声称已全量注入。
                final_text = _t("sys.file_loaded_no_model", name=name, chars=chars,
                                tokens=tokens,
                                guide=_t("sys.no_model_guide", dir=self.models_dir()))
            elif result is not None and result.status == "failed":
                final_text = _t("sys.file_async_failed", name=name,
                                error=result.error or "")
            else:
                final_text = _t("sys.file_async_ready", name=name, chars=chars,
                                tokens=tokens)

            # [EN] Commit point: the main conversation is modified only here,
            # [EN] under a lock, and only with the final result.
            # [ZH] 提交点：主对话仅在此处、持锁修改，且只写入最终结果。
            with self._conv_lock:
                entry = self.conv.add_file(path, content)
                if stored:
                    entry.stored_at = stored
                    entry.content = ""
                if summary_text:
                    self.conv.set_entry_summary(entry, summary_text, summary_meta)
                if summary_text:
                    self.conv.add_message("system", final_text)
                self.save_session()
            outcome = {"ok": True, "name": name, "chars": chars, "tokens": tokens,
                       "summary_chars": len(summary_text), "stored_at": stored,
                       "message": final_text}
            outcomes.append(outcome)
            job.emit({"type": "file_ready", "index": index, "total": total,
                      "name": name, "ok": True, "chars": chars,
                      "summary_chars": len(summary_text),
                      "summarized": bool(summary_text), "content": final_text})
        job.result = {"files": outcomes, "ignored": ignored}

    # [EN] ---------- Prompt template library (v0.2.6 Part 3, task 3.4) ----------
    # [ZH] ---------- 提示词模板库（v0.2.6 Part 3 任务 3.4） ----------






    def load_file_into_context(self, filepath: str, progress_sink=None) -> str:
        """
        [EN]
        Load a single file into the conversation context.

        [ZH]
        解析单个文件并入上下文（多文件时是其中一份）。

        返回给用户看的反馈文本（成功或失败都会说明原因）。
        ``progress_sink`` 透传给分段摘要（相关设计：流式入口据此实时报进度）。
        实际由 :meth:`_load_one_file` 完成，本方法只保留"返回一行文本"的旧语义。
        """
        result = self._load_one_file(filepath, progress_sink=progress_sink)
        return str(result.get("message", ""))

    def _load_one_file(self, filepath: str, progress_sink=None,
                       allow_summary: bool = True) -> Dict[str, Any]:
        """
        [EN]
        Load and parse one file for context injection.

        [ZH]
        解析一份文件、登记到会话并按需分段摘要。

        返回 ``{"ok": bool, "name": str, "message": str, "chars": int, ...}`` ——
        结构化结果让多文件场景能逐份报告状态（设计规范要求"每个文件独立显示状态
        （成功、失败、进度）""单个失败不影响其他"）。

        ``allow_summary=False`` 时只解析不摘要（多文件分批汇总时用：先把所有文件
        解析登记完，再决定哪一份需要摘要，避免"第一份就在摘要、后面几份还没进去"）。
        """
        name = os.path.basename(filepath) or filepath
        content = parse_file_with_wps(filepath, self.config.file_max_chars)
        if is_parse_error(content):
            # [EN] This used to concatenate `f"❌ {content}"` directly, sending these
            # [EN] **Chinese** parse-failure strings to the UI verbatim: in the English UI the
            # [EN] user saw a whole Chinese sentence (the 11-line error strings from
            # [EN] `file_parser.py`). `localize_error()` was written for exactly this path;
            # [EN] before, it was called only from the two command_handler call sites, and
            # [EN] the upload path was missed (see the knock-on defect in section 6.2 of the
            # [EN] hard-coded string audit list).
            # [ZH] 这里曾直接拼 `f"❌ {content}"`，把这批**中文**解析失败串原样送到界面：
            # [ZH] 英文界面下用户看到整句中文（`file_parser.py` 的 11 行错误串）。
            # [ZH] `localize_error()` 就是为这条路径写的，此前只被 command_handler 的两处调用，
            # [ZH] 上传路径漏了（见硬编码清查清单 §6.2 的连带缺陷）。
            return {"ok": False, "name": name, "chars": 0,
                    "message": f"❌ {localize_error(content)}"}
        files_cfg = self._files_cfg()
        entry = self.conv.add_file(filepath, content)
        try:
            tokens = self.llm.count_tokens(content)
        except Exception:
            tokens = len(content) // 2
        # [EN] Oversized file: the body is spilled to disk and the context carries only a
        # [EN] pointer plus the summary, so that one file cannot blow up the conversation and
        # [EN] the window.
        # [ZH] 超大文件：正文落盘，上下文只带指针 + 摘要，避免一份文件撑爆会话与窗口
        threshold = int(getattr(files_cfg, "disk_threshold_chars", 0) or 0)
        if threshold > 0 and len(content) > threshold:
            stored = self._store_large_file(name, content)
            if stored:
                entry.stored_at = stored
                entry.content = ""
                log_user(log, "sys.log_file_stored", name=name, threshold=threshold, path=stored)
        # [EN] Budget routing (v0.2.6 item 17). The threshold is 70% of the
        # [EN] user-selected window; below it the parsed body is injected in full
        # [EN] and summarization is never attempted, so no fact is lost. The
        # [EN] previous implementation consulted the summarizer's own min_chars /
        # [EN] trigger_ratio pair, which could summarize a file that already fitted.
        # [ZH] 预算路由（v0.2.6 第 17 项）。阈值为用户所选窗口的 70%；低于该值时
        # [ZH] 解析后的正文直接全量注入，绝不尝试摘要，因此不会丢事实。旧实现使用
        # [ZH] 摘要器自己的 min_chars / trigger_ratio，可能对本可容纳的文件也做摘要。
        effective_ctx = int(getattr(self.llm, "max_n_ctx", 0) or self.llm.n_ctx or 0)
        budget_tokens = int(effective_ctx * _LONG_TEXT_FULL_INJECT_RATIO)
        if allow_summary and effective_ctx > 0 and tokens >= budget_tokens:
            log.info(
                "文件 %s 约 %d tokens，达到窗口 %d 的 %.0f%%：启用分段摘要",
                name, tokens, effective_ctx, _LONG_TEXT_FULL_INJECT_RATIO * 100,
            )
            # [EN] Design specification: file parsing **does not depend on the model** and
            # [EN] must still complete; only the "chunked summary" step needs the model. So
            # [EN] the parsed full text is kept here and only the summary step is replaced by
            # [EN] guidance (instead of failing the whole load).
            # [ZH] 相关设计：文件解析**不依赖模型**，必须照常完成；只有"分段摘要"这一步需要模型。
            # [ZH] 所以这里保留已解析的全文，只把摘要这一步换成引导（而不是整个加载失败）。
            if not self.model_ready:
                return {"ok": True, "name": name, "chars": len(content), "tokens": tokens,
                        "needs_summary": True,
                        "message": _t("sys.file_loaded_no_model", name=name,
                                      chars=len(content), tokens=tokens,
                                      guide=_t("sys.no_model_guide", dir=self.models_dir()))}
            head = _t("sys.file_summary_mode", name=name, tokens=tokens,
                      n_ctx=effective_ctx) + "\n"
            outcome: Dict[str, Any] = {}
            summary_message = self.summarize_entry(
                entry, force=True, source_name=name,
                trigger=_t("sys.summarize_trigger_auto"),
                progress_sink=progress_sink, outcome=outcome)
            # [EN] A genuine failure means the file cannot be represented in the
            # [EN] current window. Remove the just-registered entry and report the
            # [EN] actionable limit instead of a success message. A concurrency-lock
            # [EN] failure keeps the retry path because the file may still fit.
            # [ZH] 真正的失败意味着文件无法在当前窗口内表达：撤销刚登记的文件并给出
            # [ZH] 可操作的超限提示，而不是成功文案。并发锁失败保留重试路径，因为文件
            # [ZH] 仍可能装得下。
            if (outcome.get("status") == "failed"
                    and not outcome.get("aborted")
                    and not _summary_failure_is_transient(outcome.get("error", ""))):
                try:
                    self.conv.remove_file_by_name(name)
                except Exception:  # noqa: BLE001 - [EN] cleanup must not mask the report
                                   # [ZH] 清理不得掩盖报告
                    log.debug("撤销超限文件登记失败：%s", name, exc_info=True)
                return {"ok": False, "name": name, "chars": len(content),
                        "tokens": tokens, "needs_summary": True,
                        "message": _t("sys.file_exceeds_context",
                                      name=name, tokens=tokens, n_ctx=effective_ctx)}
            return {"ok": True, "name": name, "chars": len(content), "tokens": tokens,
                    "needs_summary": True,
                    "message": head + summary_message}
        return {"ok": True, "name": name, "chars": len(content), "tokens": tokens,
                "needs_summary": False,
                "message": _t("sys.file_loaded", name=name, chars=len(content), tokens=tokens)}

    def load_files_into_context(self, paths: List[str], progress_sink=None) -> Dict[str, Any]:
        """
        [EN]
        Load multiple files into the conversation context.

        [ZH]
        按顺序加载多份文件（相关设计），返回结构化汇总。

        **单个失败不影响其他**：逐份独立 try/except，失败的那份只进 ``failed``
        列表并在汇总里点名。设计规范明确要求这一点，所以这里刻意不用"一次失败就
        整体退出"的短路写法。

        数量上限由 ``config.files.max_count`` 控制，超出的文件**不处理**并在汇总里
        如实说明（而不是静默丢掉）。
        """
        cfg = self._files_cfg()
        limit = int(getattr(cfg, "max_count", 10) or 10)
        wanted = [p for p in (paths or []) if str(p).strip()]
        accepted = wanted[:limit]
        ignored = wanted[limit:]
        results: List[Dict[str, Any]] = []
        for path in accepted:
            if self.stop_event.is_set():
                results.append({"ok": False, "name": os.path.basename(path),
                                "message": _t("sys.file_load_failed", error="stopped")})
                break
            try:
                results.append(self._load_one_file(path, progress_sink=progress_sink))
            except Exception as exc:  # noqa: BLE001
                # [EN] A single failure must not affect the other files.
                # [ZH] 单份失败不能影响其他。
                log.exception("加载文件失败: %s", path)
                results.append({"ok": False, "name": os.path.basename(path),
                                "message": _t("sys.file_load_failed", error=exc)})
        ok = [r for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok")]
        parts = [str(r.get("message", "")) for r in results]
        if ignored:
            parts.append(_t("sys.files_too_many", limit=limit))
        if len(accepted) > 1:
            parts.append(_t("sys.files_loaded_summary", count=len(ok),
                            total=len(accepted), failed=len(failed),
                            names=_t("sys.list_separator").join(self.conv.file_names())))
        return {
            "ok": len(ok), "failed": len(failed), "files": results,
            "ignored": ignored, "text": "\n".join(p for p in parts if p),
            "names": self.conv.file_names(),
        }

    def _stream_files(self, paths: List[str], stage: bool = False) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Stream file-loading progress and results.

        [ZH]
        流式加载多份文件（相关设计）：逐份解析 + 逐份进度 + 逐份结果行。

        与单文件 ``file_stream`` 的区别：这里在**文件之间**也有输出
        （``file_status`` 行：第 i/n 份、成功/失败、字符数），因此用户能看到
        "第 3/8 份正在处理"，而不是一个笼统的进度条。
        ``stage=True`` 时只解析不摘要（前端分两阶段调用：先全部登记，再摘要）。
        """
        wanted = [str(p) for p in (paths or []) if str(p).strip()]
        if not wanted:
            yield {"type": "error", "content": _t("sys.file_need_path")}
            return
        cfg = self._files_cfg()
        limit = int(getattr(cfg, "max_count", 10) or 10)
        accepted = wanted[:limit]
        ignored = wanted[limit:]
        total = len(accepted)
        self.stop_event.clear()
        progress: queue.Queue = queue.Queue()
        done_flag = threading.Event()
        collected: List[Dict[str, Any]] = []

        def _worker() -> None:
            try:
                for index, path in enumerate(accepted, 1):
                    if self.stop_event.is_set():
                        break
                    name = os.path.basename(path)
                    progress.put({"__file_start__": True, "index": index, "total": total,
                                  "name": name})
                    try:
                        result = self._load_one_file(
                            path,
                            progress_sink=lambda event, i=index, t=total, n=name: progress.put(
                                {**event, "__file__": {"index": i, "total": t, "name": n}}),
                            allow_summary=not stage)
                    except Exception as exc:  # noqa: BLE001
                        # [EN] A single failure does not affect the other files.
                        # [ZH] 单份失败不影响其他。
                        log.exception("加载文件失败: %s", path)
                        result = {"ok": False, "name": name,
                                  "message": _t("sys.file_load_failed", error=exc)}
                    result["index"] = index
                    result["total"] = total
                    result["path"] = path
                    collected.append(result)
                    progress.put({"__file_done__": True, "index": index, "total": total,
                                  "name": name, "result": result})
            finally:
                done_flag.set()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        yield {"type": "delta",
               "content": _t("sys.files_stream_start", count=total)}
        while not done_flag.wait(0.3):
            yield from self._drain_file_progress(progress, collected, total)
            if self.stop_event.is_set():
                yield {"type": "delta", "content": _t("sys.summary_stopping")}
        yield from self._drain_file_progress(progress, collected, total)
        if ignored:
            yield {"type": "system", "content": _t("sys.files_too_many", limit=limit)}
        ok = [r for r in collected if r.get("ok")]
        failed = [r for r in collected if not r.get("ok")]
        summary = _t("sys.files_loaded_summary", count=len(ok), total=len(accepted),
                     failed=len(failed),
                     names=_t("sys.list_separator").join(r.get("name", "") for r in ok))
        done: Dict[str, Any] = {"type": "done", "content": summary,
                                "files": [{"name": r.get("name", ""), "ok": bool(r.get("ok")),
                                           "chars": int(r.get("chars", 0) or 0),
                                           "message": str(r.get("message", ""))}
                                          for r in collected],
                                "names": self.conv.file_names()}
        self._attach_context(done)
        yield done

    @staticmethod
    def _drain_file_progress(progress: "queue.Queue", collected: List[Dict[str, Any]],
                             total: int) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Drain queued file-progress events.

        [ZH]
        把进度队列里的内容转成协议行（摘要进度 / 逐份状态）。
        """
        while True:
            try:
                event = progress.get_nowait()
            except queue.Empty:
                return
            if event.get("__file_start__"):
                yield {"type": "file_status", "phase": "start",
                       "index": event["index"], "total": event["total"],
                       "name": event["name"],
                       "content": _t("sys.file_status_start", index=event["index"],
                                     total=event["total"], name=event["name"])}
                continue
            if event.get("__file_done__"):
                result = event.get("result") or {}
                yield {"type": "file_status",
                       "phase": "ok" if result.get("ok") else "failed",
                       "index": event["index"], "total": event["total"],
                       "name": event["name"], "ok": bool(result.get("ok")),
                       "chars": int(result.get("chars", 0) or 0),
                       "content": str(result.get("message", ""))}
                continue
            meta = event.get("__file__") or {}
            payload = {k: v for k, v in event.items() if not k.startswith("__")}
            payload["type"] = "summary_progress"
            if meta:
                payload["file_index"] = meta.get("index")
                payload["file_total"] = meta.get("total")
                payload["file_name"] = meta.get("name")
            yield payload

    # [EN] Design specification / design specification (self-test fix-1): this spot used to
    # [EN] hold an older, **duplicate definition** of ``load_file_into_context`` (the
    # [EN] single-file era implementation: it went through ``conv.set_file``, spilled nothing
    # [EN] to disk, and had no structured multi-file return). In Python the later definition
    # [EN] of the same name wins, so the implementation above -- the new one with disk spill
    # [EN] and multi-file bookkeeping -- **became dead code**, and the single-file upload
    # [EN] path kept running the old logic:
    # [EN]   * bodies above ``files.disk_threshold_chars`` were not spilled to disk and the
    # [EN]     whole text stayed in the conversation;
    # [EN]   * behaviour diverged from ``load_files_into_context`` (the multi-file entry
    # [EN]     point): the same action took two different paths, with different disk-spill
    # [EN]     and summary results.
    # [EN] The old version has been removed; ``_load_one_file`` now handles everything
    # [EN] (parse -> register -> spill to disk -> summarize).
    # [ZH] 相关设计/相关设计（自测问题修复-1）：此处原有一个**重复定义**的旧版
    # [ZH] ``load_file_into_context``（单文件时代实现：走 ``conv.set_file``、没有落盘、
    # [ZH] 也没有多文件的结构化返回）。Python 同名方法后定义者胜，于是上面那份
    # [ZH] **带落盘与多文件登记的新实现反倒成了死代码**，单文件上传路径一直走着旧逻辑：
    # [ZH]   * 超过 ``files.disk_threshold_chars`` 的正文不会落盘，整篇留在会话里；
    # [ZH]   * 与 ``load_files_into_context``（多文件入口）行为不一致——同一个动作
    # [ZH]     走单文件和多文件两条路，落盘/摘要结果不同。
    # [ZH] 现已删除旧版，统一由 ``_load_one_file`` 承担（解析 → 登记 → 落盘 → 摘要）。
    def summarize_current_file(self, force: bool = True, source_name: str = "",
                               trigger: str = "", progress_sink=None) -> str:
        """
        [EN]
        Summarize the active file.

        [ZH]
        对当前已加载文件做分段摘要并替换注入内容（供自动触发与 /summarize 调用）。

        兼容性修正（方向八：缓存与复用）：摘要结果按"文件指纹 + 摘要参数"缓存，
        同一文件二次上传/二次摘要直接命中缓存，不再重复跑 map-reduce
        （长文档一次摘要是几十次模型调用，重复计算代价很高）。

        ``trigger``（"自动"/"手动"）是**界面文本**：默认值留空，进来后按当前语言
        解析（不能在形参默认值里写 ``_t(...)``——那是导入时求值，会固化语言）。

        ``progress_sink``（相关设计）：传入一个可调用对象时，摘要过程中的结构化进度
        事件会交给它（流式入口据此实时推给前端）；不传则退回"打到 stderr"的旧行为，
        这样非流式调用（``/summarize``、自动触发）不会往协议里插异步行。
        """
        trigger = trigger or _t("sys.summarize_trigger_manual")
        entry = self.conv._active
        if entry is None:
            return _t("cmd.resp.summarize_no_file")
        return self.summarize_entry(entry, force=force, source_name=source_name,
                                    trigger=trigger, progress_sink=progress_sink)

    def summarize_entry(self, entry, force: bool = True, source_name: str = "",
                        trigger: str = "", progress_sink=None,
                        outcome: Optional[Dict[str, Any]] = None) -> str:
        """
        [EN]
        Summarize a specific file entry.

        ``outcome`` is an optional caller-owned dictionary. When supplied it is
        filled with the machine-readable terminal state (``status``, ``error``,
        ``summarized``) so the upload path can distinguish a genuine window
        overflow from a concurrency-lock retry without parsing localized text
        (v0.2.6 item 17).

        [ZH]
        对**指定文件条目**做分段摘要（多文件下必须能指名道姓，相关设计）。

        ``outcome`` 为可选的、由调用方持有的字典。提供时写入机器可读的终态
        （``status`` / ``error`` / ``summarized``），使上传路径无需解析本地化文案
        即可区分“窗口溢出”与“并发锁重试”（v0.2.6 第 17 项）。

        与 ``summarize_current_file`` 的差别只有"作用于哪一份"：
        后者取当前活动文件，前者由调用方指定条目。
        """
        trigger = trigger or _t("sys.summarize_trigger_manual")
        if entry is None or not (entry.content or entry.stored_at):
            return _t("cmd.resp.summarize_no_file")
        # [EN] Design specification: without a model there is nothing to summarize, so give
        # [EN] guidance instead of raising.
        # [ZH] 相关设计：无模型时摘要无从谈起，给引导而不是抛异常
        if not self.model_ready:
            return _t("sys.summarize_need_model",
                      guide=_t("sys.no_model_guide", dir=self.models_dir()))
        if not entry.content:
            # [EN] The body is already on disk (oversized file): read it back to summarize
            # [EN] it instead of reporting "no content".
            # [ZH] 正文已落盘（超大文件）：从磁盘读回来做摘要，而不是报"没有内容"
            try:
                with open(entry.stored_at, "r", encoding="utf-8", errors="replace") as handle:
                    entry.content = handle.read()
            except Exception:
                log.warning("读取落盘正文失败: %s", entry.stored_at, exc_info=True)
                return _t("sys.file_load_failed", error=entry.stored_at)
        name = source_name or entry.name or os.path.basename(entry.path or "")

        # [EN] Cache hit: load the previous summary straight back into the context (skipping
        # [EN] every model call).
        # [ZH] 命中缓存：直接把上次的摘要装回上下文（跳过全部模型调用）
        cache = self.summary_cache()
        cache_key_path = entry.path or name
        params = {
            "chunk_chars": self.config.summarize.chunk_chars,
            "overlap_chars": self.config.summarize.overlap_chars,
            "max_summary_tokens": self.config.summarize.max_summary_tokens,
            "merge_max_tokens": self.config.summarize.merge_max_tokens,
        }
        if cache is not None and os.path.isfile(cache_key_path):
            hit = cache.get(cache_key_path, params)
            if hit and hit.get("text"):
                self.conv.set_entry_summary(entry, str(hit["text"]), {
                    "chunks": hit.get("chunk_count") or 0,
                    "origin_chars": hit.get("origin_chars") or 0,
                    "pointer": "",
                    "cached": True,
                    "hits": hit.get("hits") or 0,
                })
                self.llm.clear_cache()
                log_user(log, "sys.log_summary_cache_hit", name=name, hits=hit.get("hits"))
                if outcome is not None:
                    outcome.update({"status": "ok", "error": "",
                                    "summarized": True, "aborted": False})
                return (
                    _t("sys.summary_cache_hit", trigger=trigger, name=name) + "\n"
                    + _t("sys.summary_cache_hit_detail", chars=hit.get('origin_chars') or 0,
                         chunks=hit.get('chunk_count') or 0) + "\n"
                    + _t("sys.summary_cache_hit_note")
                )

        def _progress(event: Dict[str, Any]) -> None:
            """
            [EN]
            Report progress for a long-running operation.

            [ZH]
            摘要进度（相关设计）：有 sink 就交给流式入口实时推给前端，
            没有就退回打到 stderr 的旧行为（不往协议里插异步行）。
            """
            if progress_sink is not None:
                try:
                    progress_sink(event)
                except Exception:
                    log.debug("进度转发失败（已忽略）", exc_info=True)
                return
            label = self._summary_progress_text(event)
            if label:
                text = emoji_mod.prefer(f"📝 {label}")
                # [EN] v0.2.2 batch 4: when a host progress channel exists, use structured
                # [EN] events (the front end renders them as live messages); otherwise fall
                # [EN] back to stderr (behaviour for a directly run backend or an older
                # [EN] front end is unchanged down to the character).
                # [ZH] v0.2.2 第 4 批：有宿主进度通道就用结构化事件（前端渲染成活消息），
                # [ZH] 没有才退回 stderr（直接跑后端 / 老前端的行为一字不变）。
                if _host_progress_sink is not None:
                    _host_progress_sink(text)
                    return
                print(text, file=sys.stderr, flush=True)

        result = self.summarizer.summarize(
            entry.content, source_name=name, force=force, on_progress=_progress,
            should_stop=lambda: self.stop_event.is_set(),
        )
        if outcome is not None:
            outcome.update({
                "status": str(getattr(result, "status", "") or ""),
                "error": str(getattr(result, "error", "") or ""),
                "summarized": bool(getattr(result, "summarized", False)),
                "aborted": bool(getattr(result, "aborted", False)),
            })
        # [EN] Design specification: report "slow" and "broken" separately. Neither an abort
        # [EN] nor a failure injects half a summary, and the retry path is preserved.
        # [ZH] 相关设计：把"慢"与"坏"分开报。中止/失败都不注入半份摘要，并保留重试路径。
        if result.aborted:
            return _t("sys.summary_aborted", name=name, done=len(result.segments),
                      total=result.chunk_count, elapsed=f"{result.elapsed:.0f}")
        if not result.summarized:
            if result.status == "failed":
                log.error("分段摘要失败: %s（%s）", name, result.error)
                return _t("sys.summary_failed", name=name, chunks=result.chunk_count,
                          failed=result.failed_chunks, timeout=result.timeout_chunks,
                          elapsed=f"{result.elapsed:.0f}")
            return _t("sys.summary_too_short", chars=result.origin_chars)
        self.conv.set_entry_summary(entry, result.text,
            {"chunks": result.chunk_count, "origin_chars": result.origin_chars,
             "pointer": result.pointer, "status": result.status},
        )
        # [EN] Write the cache: the next request for the same file (with unchanged content
        # [EN] and parameters) reuses it directly.
        # [ZH] 写入缓存：下次同一文件（内容与参数都没变）直接复用
        if cache is not None and os.path.isfile(cache_key_path):
            cache.put(cache_key_path, params, result.text,
                      chunk_count=result.chunk_count, origin_chars=result.origin_chars)
            cache.save()
        # [EN] The injected content structure changed, so the old KV cache is invalidated.
        # [ZH] 注入内容结构变化，旧 KV 缓存失效。
        self.llm.clear_cache()
        summary_tokens = self.llm.count_tokens(result.text)
        pointer = result.pointer
        shown = pointer[:160] + ("…" if len(pointer) > 160 else "")
        text = (
            _t("sys.summary_done", trigger=trigger, name=name) + "\n"
            + _t("sys.summary_done_stats", chars=result.origin_chars,
                 chunks=result.chunk_count, tokens=summary_tokens) + "\n"
            + _t("sys.summary_done_note") + "\n"
            + _t("sys.summary_done_pointer", pointer=shown)
        )
        # [EN] When some chunks fall back, say so honestly instead of pretending everything
        # [EN] is fine (design specification: a real fault must produce an error message).
        # [ZH] 有段落走兜底时如实说明，不假装一切正常（相关设计：真故障要给错误信息）
        if result.status == "partial":
            text += "\n" + _t("sys.summary_partial_notice",
                              ok=result.ok_chunks + result.partial_chunks,
                              total=result.chunk_count,
                              failed=result.failed_chunks, timeout=result.timeout_chunks,
                              skipped=result.skipped_chunks, slow=result.slow_chunks)
        elif result.slow_chunks:
            text += "\n" + _t("sys.summary_slow_notice", slow=result.slow_chunks,
                              total=result.chunk_count, elapsed=f"{result.elapsed:.0f}")
        return text

    def _summary_progress_text(self, event: Dict[str, Any]) -> str:
        """
        [EN]
        Format summary progress text for the frontend.

        [ZH]
        把摘要进度事件转成一行人类可读文案（相关设计：结构化数据 → 界面文本）。

        文案统一在这里按语言包解析：``summarizer.py`` 只产结构化字段，不含界面文案，
        这样摘要算法不必关心当前界面语言，也不会把语言固化在导入时。
        """
        phase = str(event.get("phase") or "")
        status = str(event.get("status") or "")
        index = int(event.get("index") or 0)
        total = int(event.get("total") or 0)
        elapsed = float(event.get("elapsed") or 0.0)
        chunk_elapsed = float(event.get("chunk_elapsed") or 0.0)
        if phase == "prepare":
            return _t("summary.progress.prepare", chunks=total, chars=int(event.get("chars") or 0))
        if phase == "map":
            if status == "start":
                return _t("summary.progress.chunk_start", index=index, total=total,
                          chars=int(event.get("chars") or 0))
            if status == "running":
                base = _t("summary.progress.chunk_running", index=index, total=total,
                          produced=int(event.get("tokens") or 0),
                          seconds=f"{chunk_elapsed:.0f}")
                if event.get("slow"):
                    base += " " + _t("summary.progress.chunk_slow",
                                     seconds=f"{chunk_elapsed:.0f}")
                return base
            if status == "ok":
                return _t("summary.progress.chunk_ok", index=index, total=total,
                          chars=int(event.get("summary_chars") or 0),
                          seconds=f"{chunk_elapsed:.0f}")
            if status == "retry":
                return _t("summary.progress.chunk_retry", index=index, total=total)
            if status == "timeout":
                return _t("summary.progress.chunk_timeout", index=index, total=total)
            if status == "failed":
                return _t("summary.progress.chunk_failed", index=index, total=total)
        if phase == "reduce":
            return _t("summary.progress.reduce", index=index, total=total,
                      seconds=f"{elapsed:.0f}")
        if phase == "done":
            return _t("summary.progress.done", chunks=total, chars=int(event.get("summary_chars") or 0),
                      seconds=f"{elapsed:.0f}")
        if phase == "aborted":
            return _t("summary.progress.aborted", index=index, total=total)
        if phase == "failed":
            return _t("summary.progress.failed", total=total)
        return ""

    def _stream_file(self, filepath: str) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Stream a file operation with progress updates.

        [ZH]
        流式加载文件（相关设计）：解析 → 必要时分段摘要 → 边跑边报进度。

        为什么要有流式入口：非流式的 ``file`` 动作在整个摘要期间**没有一行输出**，
        小块模型 + 纯 CPU 下一段要跑几十秒，用户在界面上看到的就是"卡住了"。
        这里把摘要放到子线程，主线程轮询进度队列并逐行推给前端（与模型下载同一套做法），
        同时 ``stop`` 动作能通过 ``self.stop_event`` 真正打断它。
        """
        if not filepath:
            yield {"type": "error", "content": _t("sys.file_need_path")}
            return
        self.stop_event.clear()
        progress: queue.Queue = queue.Queue()
        outcome: Dict[str, Any] = {}
        done_flag = threading.Event()

        def _sink(event: Dict[str, Any]) -> None:
            progress.put(event)

        def _worker() -> None:
            try:
                outcome["text"] = self.load_file_into_context(filepath, progress_sink=_sink)
            except Exception as exc:  # noqa: BLE001
                log.exception("流式加载文件失败")
                outcome["text"] = _t("sys.file_load_failed", error=exc)
            finally:
                done_flag.set()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        yield {"type": "delta", "content": _t("sys.file_stream_start",
                                              name=os.path.basename(filepath))}
        while not done_flag.wait(0.3):
            drained = False
            while True:
                try:
                    event = progress.get_nowait()
                except queue.Empty:
                    break
                drained = True
                payload = dict(event)
                payload["type"] = "summary_progress"
                payload["content"] = self._summary_progress_text(event)
                yield payload
            if self.stop_event.is_set():
                yield {"type": "delta", "content": _t("sys.summary_stopping")}
                drained = True
            _ = drained
        # [EN] Wrap-up: flush the events left in the queue (so the last progress line is not
        # [EN] dropped).
        # [ZH] 收尾：把队列里剩下的事件推完（避免最后一条进度被丢掉）
        while True:
            try:
                event = progress.get_nowait()
            except queue.Empty:
                break
            payload = dict(event)
            payload["type"] = "summary_progress"
            payload["content"] = self._summary_progress_text(event)
            yield payload
        thread.join(timeout=5)
        text = str(outcome.get("text") or "")
        done: Dict[str, Any] = {"type": "done", "content": text,
                                "aborted": bool(self.stop_event.is_set()),
                                "model_ready": self.model_ready}
        self._attach_context(done)
        yield done



    def _estimate_tokens(self, messages) -> int:
        """
        [EN]
        Estimate token count for a text string.

        [ZH]
        用模型分词器估算消息 token 数；失败时按字符数粗略估算。
        """
        try:
            return sum(len(self.llm.llm.tokenize(m["content"].encode("utf-8"))) for m in messages)
        except Exception:
            return sum(len(m["content"]) for m in messages) // 2

    def _note_request_size(self, messages) -> None:
        """
        [EN]
        Record the size of the current model request.

        [ZH]
        记下本轮**实际发给模型**的消息有多大（相关设计，自测问题修复-1）。

        为什么需要它：状态区的上下文占用原来只算 ``system + history``
        （见 :meth:`_attach_context`），**完全没算这一轮注入的文件正文、检索片段与
        搜索结果** —— 而这三块恰恰是大头（文件块上限 60000 字符，比多数会话的历史还长）。
        于是用户看到的现象就是"占用远未到 1/3"，而它同时又"一会儿多一会儿少"：
        少的那些轮次只是恰好没注入文件（修复相关设计之前，文件注入与否取决于
        问题里有没有"文件/合同/总结"这类词），多的那些轮次才是真实占用。

        **不去伪造一个"平均值"**：占用本来就会随注入内容变化，这是事实而不是缺陷。
        正确做法是把"上一轮真实发出去了多少"如实报出来，让用户看到的就是真实的那一个数。
        """
        try:
            self._last_request_tokens = int(self._estimate_tokens(messages))
        except Exception:
            self._last_request_tokens = 0

    @staticmethod
    def _fmt_mb(n: int) -> str:
        """
        [EN]
        Format a byte count in megabytes.

        [ZH]
        把字节数格式化为可读的 MB 字符串，用于下载进度展示。
        """
        return f"{n / 1048576:.1f} MB"

    def _compaction_cfg(self):
        """
        [EN]
        Return the conversation-compaction configuration.

        [ZH]
        取压缩配置（老配置文件没有这一段时回落到默认值）。
        """
        return getattr(self.config, "compact", None) or CompactionConfig()

    def compact_log(self):
        """
        [EN]
        Compact the conversation log when it exceeds its budget.

        [ZH]
        压缩事件日志（惰性创建；``log_file`` 为空时返回 None）。
        """
        cfg = self._compaction_cfg()
        path = str(getattr(cfg, "log_file", "") or "")
        if not path:
            return None
        if getattr(self, "_compact_log", None) is None:
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
            self._compact_log = CompactLog(
                path,
                record_content=bool(getattr(cfg, "log_content", True)),
                max_events=int(getattr(cfg, "log_max_events", 5000) or 5000),
            )
        return self._compact_log

    @staticmethod
    def _turn_start(history: List[dict], turns: int) -> int:
        """
        [EN]
        Return the index of the current turn's first message.

        [ZH]
        返回"最近 ``turns`` 轮"的起始下标（grace zone 的左边界）。

        轮的定义：一次 user 消息开始，到它的 assistant 回复为止。
        从末尾往前数 ``turns`` 个 user 消息，它们之前的位置就是要压缩的边界。
        数不够就返回 0（全部保留，不压缩）。
        """
        if turns <= 0:
            return len(history)
        seen = 0
        for index in range(len(history) - 1, -1, -1):
            if history[index].get("role") == "user":
                seen += 1
                if seen >= turns:
                    return index
        return 0

    @staticmethod
    def _truncate_tool_outputs(messages: List[dict], limit: int) -> tuple:
        """
        [EN]
        Truncate oversized tool outputs in the conversation.

        [ZH]
        把"大块内容"在进压缩器之前单独截断（设计规范相关设计·工具输出单独处理）。

        ``limit`` 为 0 时不截断。返回 ``(处理后的消息, 被截断条数, 省下的字符数)``。

        为什么要单独处理：文件解析结果、搜索结果、自动化结果往往是**一条消息
        几万字符**。不过滤的话压缩器的窗口会被一条消息吃光（旧实现靠
        ``content[:300]`` 粗暴截断 —— 那等于把这些内容整条丢掉，
        连"这里曾经有一大段文件内容"都看不出来）。这里保留**头尾+长度说明**：
        头部是文档标题与摘要、尾部常常是结论与落款，两端比中段更有信息量。
        """
        if limit <= 0:
            return messages, 0, 0
        result = []
        truncated = 0
        saved = 0
        for msg in messages:
            content = str(msg.get("content", ""))
            if len(content) <= limit:
                result.append(msg)
                continue
            head = content[: int(limit * 0.6)]
            tail = content[-int(limit * 0.3):]
            body = (f"{head}\n…（此处省略 {len(content) - len(head) - len(tail)} 字符，"
                    f"原文共 {len(content)} 字符）…\n{tail}")
            result.append({**msg, "content": body})
            truncated += 1
            saved += len(content) - len(body)
        return result, truncated, saved

    def _compact_history(self, force: bool = False, focus: str = "",
                         reason: str = "") -> Dict[str, Any]:
        """
        [EN]
        Compact older conversation history into a summary.

        [ZH]
        上下文长度管理：把"装不下"的历史搬出上下文。

        三个模式（``config.compact`` 与 ``context_mode`` 共同决定）：

        * ``summarize``（默认）：更早的历史交给模型压成要点摘要，信息保留最好；
        * ``slide``：直接丢弃最早的历史（滑动窗口），不做额外推理，最快。

        相关设计（优化已知问题-2）改掉的三件事：

        1. **触发按 token 比例**（``compact.trigger_ratio``，默认 0.70）而不是
           硬编码 80%，阈值可配；
        2. **grace zone**：最近 ``compact.grace_turns`` **轮**逐字保留
           （与 ``keep_ratio`` 的 token 预算取并集，谁保留得多听谁的），
           避免"刚发生的一轮被压掉、更早的留着"；
        3. **摘要提示词可自定义** + ``/compact focus on …`` 指定重点，
           并把整次压缩作为事件写入 ``compact_log.jsonl``（可追溯/可回放）。

        ``force=True`` 时忽略阈值直接压缩（``/compact`` 手动触发用）。
        返回一份统计给调用方（指令回执 / 前端状态显示）。
        """
        conv = self.conv
        cfg = self._compaction_cfg()
        ratio = float(getattr(cfg, "trigger_ratio", 0.70) or 0.70)
        # [EN] Compaction is evaluated against the hardware ceiling so that the
        # [EN] small initial allocation does not trigger premature summarisation.
        # [ZH] 压缩判断以硬件上限为基准，避免较小的初始分配导致过早摘要。
        effective_ctx = int(getattr(self.llm, "max_n_ctx", 0) or self.llm.n_ctx or 0)
        limit = int(effective_ctx * ratio)
        trigger = "manual" if force else "auto"
        result: Dict[str, Any] = {"compressed": False, "limit": limit,
                                 "ratio": ratio, "trigger": trigger}
        if not conv.history:
            result["reason"] = "empty"
            return result
        try:
            current = [{"role": "system", "content": conv.get_system_prompt()}] + conv.history
            total = self._estimate_tokens(current)
        except Exception:
            result["reason"] = "estimate_failed"
            return result
        result["used"] = total
        result["used_before"] = total
        if total <= limit and not force:
            result["reason"] = "under_limit"
            return result

        # [EN] ---------- Preserved region: grace zone (turns) ∪ token budget, union ----------
        # [ZH] ---------- 保留区：grace zone（轮）∪ token 预算，取并集 ----------
        grace_turns = int(getattr(cfg, "grace_turns", 3) or 0)
        keep_ratio = float(getattr(cfg, "keep_ratio", 0.20) or 0.0)
        by_turns = self._turn_start(conv.history, grace_turns)

        keep_tokens = max(1, int(limit * keep_ratio))
        keep_count = 0
        acc = 0
        for msg in reversed(conv.history):
            acc += self._estimate_tokens([msg])
            keep_count += 1
            if acc >= keep_tokens:
                break
        # [EN] Union: whichever keeps more wins (the grace zone keeps "turns", keep_ratio
        # [EN] keeps "volume").
        # [ZH] 并集：谁保留得多听谁的（grace zone 保"轮"，keep_ratio 保"量"）
        keep_start = min(by_turns, len(conv.history) - keep_count)
        if keep_start <= 0 and force and grace_turns > 1:
            # [EN] Manual compaction (/compact or the status area button) is the user
            # [EN] explicitly asking to "make room right now": if the grace zone happens to
            # [EN] cover the whole history, shrink it to 1 turn and retry -- otherwise "the
            # [EN] user pressed compact and nothing happened" is the worse experience.
            # [EN] Automatic triggering does **not** make this concession: automatic
            # [EN] compaction starts from "it is nearly full", and "the last 3 turns alone
            # [EN] fill it" means the user should decide (by lowering grace_turns); the
            # [EN] program must not silently crush what was just said.
            # [ZH] 手动压缩（/compact 或状态区按钮）被用户明确要求"现在就腾地方"：
            # [ZH] 此时若 grace zone 恰好覆盖全部历史，就把它收缩到 1 轮再做一次 ——
            # [ZH] 否则"用户点了压缩却什么也没发生"是更糟的体验。自动触发**不做**这个
            # [ZH] 让步：自动压缩的背景是"快满了"，而"最近 3 轮就占满"说明该由用户
            # [ZH] 自己选（提示他调小 grace_turns），不该由程序擅自压掉刚说过的话。
            by_turns = self._turn_start(conv.history, 1)
            keep_start = min(by_turns, len(conv.history) - keep_count)
            if keep_start > 0:
                log_user(log, "sys.log_compaction_manual", grace=grace_turns)
                result["grace_shrunk"] = True
        result["grace"] = grace_turns
        result["kept_by_turns"] = len(conv.history) - by_turns
        if keep_start <= 0:
            result["reason"] = "nothing_to_drop"
            log.info("上下文 %d/%d tokens 超限，但保留区覆盖了全部历史（grace=%d 轮），"
                     "本次不压缩", total, limit, grace_turns)
            return result
        dropped = conv.history[:keep_start]
        conv.history = conv.history[keep_start:]
        result.update(compressed=True, dropped=len(dropped),
                      kept=len(conv.history))

        mode = str(getattr(self.config, "context_mode", "summarize") or "summarize").lower()
        event = CompactEvent(
            kind=EVENT_SKIP, trigger=trigger, mode=mode, limit=limit, ratio=ratio,
            used_before=total, dropped=len(dropped), kept=len(conv.history),
            grace=grace_turns,
            reason=reason or ("manual" if force else "over_limit"),
        )
        if mode == "slide":
            # [EN] Sliding window: no summary is produced; the oldest history is simply moved
            # [EN] out of the context.
            # [ZH] 滑动窗口：不做摘要，只是把最早的历史移出上下文
            log.info("上下文 %d/%d tokens：滑动窗口丢弃最早 %d 条（未生成摘要，阈值 %.0f%%）",
                     total, limit, len(dropped), ratio * 100)
            result["mode"] = "slide"
            result["summary_chars"] = 0
        else:
            # [EN] ---------- Truncate large blocks before feeding the compressor ----------
            # [ZH] ---------- 大块内容单独截断后再喂压缩器 ----------
            prepared, truncated, saved = self._truncate_tool_outputs(
                dropped, int(getattr(cfg, "tool_output_chars", 2000) or 0))
            event.truncated_outputs = truncated
            event.saved_chars = saved
            if truncated:
                log.info("压缩前单独截断 %d 条大块内容，少喂 %d 字符给压缩器",
                         truncated, saved)

            message_chars = int(getattr(cfg, "message_chars", 300) or 300)
            input_chars = int(getattr(cfg, "input_chars", 4000) or 4000)
            text = "\n".join(
                f"{'用户' if m['role'] == 'user' else '助手'}: "
                f"{str(m.get('content', ''))[:message_chars]}"
                for m in prepared
            )[:input_chars]

            prompt = str(getattr(cfg, "prompt", "") or "").strip()
            focus_text = str(focus or "").strip()
            if focus_text:
                # [EN] User-specified focus: append it at the end of the prompt instead of
                # [EN] replacing it, so the general requirements are not lost.
                # [ZH] 用户指定重点：追加在提示词末尾而不是替换它，避免丢掉通用要求
                prompt = f"{prompt}\n本次特别要求保留的重点：{focus_text}"
            event.prompt = prompt
            event.focus = focus_text

            summary = ""
            try:
                summary = self.llm.chat_complete(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.3,
                    max_tokens=int(getattr(cfg, "summary_max_tokens", 200) or 200),
                ).strip()
            except Exception as exc:  # noqa: BLE001
                # [EN] A compaction failure must not crash this round of conversation.
                # [ZH] 压缩失败不能让本轮对话崩。
                log.warning("历史压缩失败，已直接裁剪", exc_info=True)
                event.error = str(exc)[:200]
            if summary:
                conv.summary = f"{conv.summary}\n{summary}".strip()
            result["mode"] = "summarize"
            result["summary_chars"] = len(summary)
            result["focus"] = focus_text
            event.kind = EVENT_SUMMARY
            event.summary_chars = len(summary)
            event.summary = summary
            log.info(
                "上下文 %d/%d tokens 超限（阈值 %.0f%%）：grace %d 轮 + 预算保留共 %d 条，"
                "压缩 %d 条为摘要（%d 字）%s",
                total, limit, ratio * 100, grace_turns, len(conv.history),
                len(dropped), len(summary),
                f"，重点={focus_text}" if focus_text else "",
            )
        try:
            result["used_after"] = self._estimate_tokens(
                [{"role": "system", "content": conv.get_system_prompt()}] + conv.history)
            event.used_after = result["used_after"]
        except Exception:
            result["used_after"] = None
        # [EN] Persist the event (a failure only logs a warning; the compaction result is
        # [EN] unaffected).
        # [ZH] 事件落盘（失败只告警，不影响压缩结果）
        journal = self.compact_log()
        if journal is not None:
            journal.append(event)
        result["event"] = event.to_dict()
        return result

    def compact_context(self, force: bool = True, focus: str = "") -> Dict[str, Any]:
        """
        [EN]
        Compact the model context when it exceeds its limit.

        [ZH]
        对外暴露的手动压缩入口（``/compact`` 与前端按钮共用）。

        ``focus`` 是用户指定的重点（``/compact focus on 合同金额``），
        会追加到摘要提示词里（相关设计"支持用户自定义重点"）。

        压缩后清掉 KV 缓存：历史变了，缓存不再对应，
        不清的话下一次生成会带着错误的注意力前缀。
        """
        result = self._compact_history(force=force, focus=focus,
                                       reason="focus" if focus else "")
        if result.get("compressed"):
            try:
                self.llm.clear_cache()
            except Exception:
                log.debug("压缩后清 KV 缓存失败", exc_info=True)
        return result

    def compaction_diagnostics(self) -> Dict[str, Any]:
        """
        [EN]
        Return diagnostics about context compaction.

        [ZH]
        压缩链路的可观测快照（相关设计"压缩事件可追溯"的查询入口）。

        返回当前生效的阈值/保留策略、日志汇总、以及回放出的当前摘要 ——
        用户与测试都能据此回答"什么时候压的、压了什么、现在摘要是什么"。
        """
        cfg = self._compaction_cfg()
        journal = self.compact_log()
        info: Dict[str, Any] = {
            "trigger_ratio": float(getattr(cfg, "trigger_ratio", 0.70) or 0.70),
            "grace_turns": int(getattr(cfg, "grace_turns", 3) or 0),
            "keep_ratio": float(getattr(cfg, "keep_ratio", 0.20) or 0.20),
            "mode": str(getattr(self.config, "context_mode", "summarize") or "summarize"),
            "log_file": str(getattr(cfg, "log_file", "") or ""),
            "summary_chars": len(self.conv.summary or ""),
        }
        if journal is not None:
            info["journal"] = journal.stats()
            events = journal.events()
            info["replay_steps"] = len(replay(events))
            info["last_event"] = (events[-1].to_dict() if events else {})
        return info




    @staticmethod
    def _merge_action(response: Dict[str, Any], action: Dict[str, Any]) -> None:
        """
        [EN]
        Merge a command action into the response map.

        [ZH]
        把动作字段并入响应（不覆盖 type）。
        """
        for key, value in action.items():
            if key != "type":
                response[key] = value


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


# ---------------------------------------------------------------------------
# [EN] The old behaviour has been removed (general optimization in an earlier release,
# [EN] design specification): two functions used to live here:
# [EN]   * `_await_download_decision()` -- when the model was missing, it **blocked** reading
# [EN]     stdin for the user's answer; on `exit` or a closed stdin it returned None and the
# [EN]     caller ended the process;
# [EN]   * `_download_with_progress()` -- a one-off downloader dedicated to that function
# [EN]     (the model marketplace already has a more complete download implementation,
# [EN]     `_stream_model_hub_download`: resumable transfer, retries, speed/remaining time,
# [EN]     cancellation, SHA256 verification).
# [EN] Together they formed the corner "either download immediately or the whole
# [EN] application is unusable", which this round removes.
# [EN] A missing model now goes through the "no-model-ready" state: the backend keeps
# [EN] running and guides the user to download from the model marketplace or place a .gguf
# [EN] manually; once it is there it is hot-loaded in place. Exit is only triggered by the
# [EN] user (the front end sends `exit`, handled in the main loop).
# [EN] The corresponding language keys `sys.await_download_confirm` / `sys.model_declined` /
# [EN] `sys.model_missing_no_repo` were removed as well, so that no text "describing
# [EN] deprecated behaviour" is left behind.
# [ZH] 旧行为已移除（此前版本 综合优化 相关设计）：这里曾有两个函数
# [ZH]   * `_await_download_decision()` —— 缺模型时**阻塞**读 stdin 等用户答复，
# [ZH]     收到 `exit` 或 stdin 关闭就返回 None，调用方随之结束进程；
# [ZH]   * `_download_with_progress()` —— 上面那个函数专用的一次性下载器
# [ZH]     （模型市场已经有更完整的下载实现 `_stream_model_hub_download`：
# [ZH]     断点续传、重试、速度/剩余时间、取消、SHA256 校验）。
# [ZH] 它们合起来构成"要么立即下载，要么整个应用不可用"的墙角，正是本轮要拆掉的东西。
# [ZH] 现在缺模型走"无模型就绪"：后端照常运行，引导用户在模型市场下载或手动放置 .gguf，
# [ZH] 到位后原地热加载。退出只由用户主动触发（前端发 `exit`，主循环里处理）。
# [ZH] 对应的语言键 `sys.await_download_confirm` / `sys.model_declined` /
# [ZH] `sys.model_missing_no_repo` 也一并删除，避免留下"描述已废弃行为"的文案。
# ---------------------------------------------------------------------------


def _run_inference_test_load_cli(payload_json: str) -> None:
    """
    [EN]
    Child-process entry point for the settings page's "Test load" button.
    It loads the requested parameters, runs one tiny completion and prints a
    single JSON line on stdout. Running in a child process is what makes the
    60-second timeout enforceable: the parent can terminate the process and
    reclaim the model memory immediately.

    [ZH]
    设置页「测试加载」按钮的子进程入口。按请求参数加载模型、执行一次极短推理，并在
    stdout 打印一行 JSON。放在子进程中运行，才使 60 秒超时可强制实施：父进程可以
    终止该进程并立即回收模型内存。
    """
    engine = None
    try:
        payload = json.loads(payload_json)
        path = str(payload.get("path") or "")
        from hardware_profiler import plan_inference, probe

        plan = plan_inference(
            profile=probe(),
            model_path=path,
            train_context=resolve_context_window(path, 0),
            requested_context=int(payload.get("n_ctx") or 0),
            max_context=int(Config.load().max_context or 32768),
            requested_gpu_layers=int(payload.get("n_gpu_layers") or 0),
            n_threads_override=int(payload.get("threads") or 0),
            inference_profile=str(payload.get("profile") or ""),
            requested_n_batch=int(payload.get("n_batch") or 0),
            allow_override=True,
        )
        blocking = _override_blocking_error(plan) or _preflight_context_error(plan)
        if blocking:
            raise RuntimeError(blocking)
        from llm_engine import LLMEngine

        with LLMEngine(
            path,
            plan.n_ctx_initial,
            n_threads=plan.n_threads,
            n_batch=plan.n_batch,
            n_gpu_layers=plan.n_gpu_layers,
            max_n_ctx=plan.n_ctx_max,
            backend_label=plan.backend,
        ) as engine:
            engine.apply_plan(plan)
            answer = engine.chat_complete(
                [{"role": "user", "content": "ping"}],
                temperature=0.0,
                max_tokens=8,
            )
        print(json.dumps({
            "ok": True,
            "plan": plan.to_dict(),
            "answer": str(answer or "")[:120],
            "warnings": list(plan.warnings),
        }, ensure_ascii=False), flush=True)
    except Exception as exc:  # noqa: BLE001 - the parent renders the raw error
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }, ensure_ascii=False), flush=True)
        raise SystemExit(3)
    finally:
        if engine is not None:
            engine.close()
        del engine
        gc.collect()


def main() -> None:
    # [EN] Hidden child-process mode used by the settings page's "Test load"
    # [EN] button. It must run before Config.load / Backend construction so the
    # [EN] ordinary startup path is untouched.
    # [ZH] 设置页「测试加载」按钮使用的隐藏子进程模式。必须在 Config.load 与 Backend
    # [ZH] 构造之前处理，使普通启动路径完全不受影响。
    if "--inference-test-load" in sys.argv:
        index = sys.argv.index("--inference-test-load")
        if index + 1 < len(sys.argv):
            _run_inference_test_load_cli(sys.argv[index + 1])
        return
    # [EN] Compatibility fix: switch the output streams to UTF-8 first and decide whether
    # [EN] emoji can be emitted safely.
    # [EN] This must run before any output: when the console code page is not UTF-8, emoji
    # [EN] become "??", and downgrading afterwards would already be too late (log and
    # [EN] progress messages are printed while the program is running).
    # [ZH] 兼容性修正：先把输出流切到 UTF-8 并判断能否安全输出 emoji。
    # [ZH] 必须在任何输出之前执行——控制台代码页不是 UTF-8 时 emoji 会变成 "??"，
    # [ZH] 那时再降级就已经晚了（日志/进度消息是边跑边打的）。
    emoji_mod.configure_streams()
    if "--no-emoji" in sys.argv:
        emoji_mod.set_enabled(False)

    config = Config.load()

    # [EN] Design specification: a missing model **no longer prompts and then exits**; the
    # [EN] backend keeps running in the "no-model-ready" state. Only one "is the model there"
    # [EN] check is made here; when it is missing, the Backend is still constructed (with the
    # [EN] engine left empty), and a **non-blocking** need_download is sent to the front end
    # [EN] before ready.
    # [ZH] 相关设计：模型缺失**不再征询后退出**，而是以"无模型就绪"状态继续跑。
    # [ZH] 这里只做一次"模型在不在"的判断；缺模型时先把 Backend 建起来（引擎留空），
    # [ZH] 稍后在 ready 之前发一条**非阻塞**的 need_download 告知前端。
    # [EN] Reader: the model directory is resolved exactly like Backend.models_dir()
    # [EN] (config.model_hub.models_dir, relative to the writable state directory).
    # [ZH] Reader：模型目录的解析方式与 Backend.models_dir() 完全一致
    # [ZH] （config.model_hub.models_dir，相对可写状态目录解析）。
    _models_setting = str(
        getattr(getattr(config, "model_hub", None), "models_dir", "models") or "models")
    _models_dir = (_models_setting if os.path.isabs(_models_setting)
                   else os.path.join(state_dir(), _models_setting))
    model_path = check_model(config.model_path, _models_dir)
    if not model_path:
        log.warning("%s", _t("sys.log_missing_model", model=config.model_path))

    try:
        with _suppress_stdout():
            backend = Backend(config, model_path or "")
            restored = backend.restore_session()
            backend.warmup()
            backend.touch_model_usage()   # [EN] Feature 4: record the model's last use time.
                                          # [ZH] 新增四：记录模型最后使用时间
    except Exception as e:
        log_user(log, "sys.log_backend_init_failed", level="exception")
        # [EN] Write one error response to stdout so that the front end's first request can
        # [EN] read it.
        # [ZH] 向 stdout 输出一条错误响应，前端首条请求即可读到
        print(json.dumps({"type": "system", "content": _t("sys.backend_init_failed", error=e)},
                         ensure_ascii=False), flush=True)
        return

    # [EN] Reader: the skill runtime is physically removed, so there is nothing to load
    # [EN] at start-up. The stage is kept as an explicit no-op comment instead of being
    # [EN] silently deleted, so a future re-introduction is an obvious edit.
    # [ZH] Reader：技能运行期已物理删除，启动阶段无任何可加载内容。该阶段保留为显式空
    # [ZH] 注释而不是被静默删除，使将来若重新引入也是一处显而易见的改动。

    # [EN] Design specification: tell the front end when the model is missing
    # [EN] (**non-blocking**) so that it shows guidance instead of a forced choice such as
    # [EN] "download now or refuse and exit". Emitted before ready, so the front end gets
    # [EN] all the information at once when it renders the ready state.
    # [ZH] 相关设计：缺模型时告知前端（**非阻塞**）——前端据此显示引导，而不是弹"是否下载、
    # [ZH] 拒绝就退出"的强制选择。放在 ready 之前，前端渲染就绪状态时一次就能拿到全部信息。
    model_missing: Dict[str, Any] = {}
    if not backend.model_ready:
        # [EN] Reader edition: **no network request during startup**. There is no model
        # [EN] repository, no mirror and no download path in this build, so the notice
        # [EN] only names the missing local file and the real models directory; the user
        # [EN] places a ``.gguf`` file there manually.
        # [ZH] Reader 版：**启动阶段不发任何网络请求**。本构建没有模型仓库、没有镜像、
        # [ZH] 也没有下载路径，因此这条告知只说明缺失的本地文件与真实的模型目录；用户
        # [ZH] 把 ``.gguf`` 文件手动放进去即可。
        model_missing = {
            "model": config.model_path,
            "target": config.model_path,
            "size": 0,
            "models_dir": backend.models_dir(),
            "error": "",
        }
        _write({
            "type": "need_download",
            # [EN] blocking=False: **new semantics**. The old behaviour was "the backend
            # [EN] stops and waits for an answer, and refusing exits"; it is now only a
            # [EN] notification, the backend is ready and keeps serving (see the module
            # [EN] header).
            # [ZH] blocking=False：**新语义**。旧行为是"后端停下等答复，拒绝即退出"；
            # [ZH] 现在它只是一条告知，后端已就绪并继续服务（见模块头部的说明）。
            "blocking": False,
            "model": config.model_path,
            "size": 0,
            "current": config.model_path,
            "target": config.model_path,
            "models_dir": backend.models_dir(),
            "content": _t(
                "sys.model_missing_ask", model=config.model_path, repo="",
                target=config.model_path,
                size=_t("sys.model_missing_unknown_size"),
                dir=backend.models_dir(),
            ),
        })

    log_user(log, "sys.log_ready", app=APP_NAME, version=APP_VERSION, studio=STUDIO)
    # [EN] Reader: no local HTTP server is started. The only inbound channel is the
    # [EN] stdin/stdout JSON protocol handled by the loop below.
    # [ZH] Reader：不启动任何本地 HTTP 服务。唯一入站通道就是下方循环处理的 stdin/stdout
    # [ZH] JSON 行协议。
    # [EN] Ready declaration: emitted only after warm-up and all other preparation have
    # [EN] finished; the front end uses it to unlock input. It also carries the restored
    # [EN] history for the front end to display.
    # [ZH] 就绪声明：预热等准备工作全部完成后才输出，前端据此放行输入；附带恢复的历史消息供前端展示
    _write({"type": "ready", "version": APP_VERSION, "studio": STUDIO, "restored": restored,
            "history": backend.conv.history,
            "locale": _i18n().locale, "i18n": _i18n().info(),
            "commands": [],
            "skills": {},
            # [EN] Reader: the inference configuration in force at launch, so the status
            # [EN] area can show the tuned parameters without a second round trip.
            # [ZH] Reader：启动时生效的推理配置，使状态区无需二次往返即可显示调优参数。
            "inference_config": backend.inference_config.describe(),
            # [EN] Design specification: clearly distinguish "the backend is ready" from
            # [EN] "inference is available".
            # [ZH] 相关设计：明确区分"后端已就绪"与"推理可用"
            "model_ready": backend.model_ready,
            "model_state": backend.model_state(),
            "model_missing": model_missing,
            # [EN] Compatibility fix / compatibility fix: tell the front end whether emoji
            # [EN] are available and where the data directory is (both are needed for
            # [EN] compliance and for support troubleshooting).
            # [ZH] 兼容性修正 / 兼容性修正：告知前端 emoji 是否可用、数据目录在哪（合规与支持排查都要用）
            "emoji": emoji_mod.enabled(),
            # [EN] Design specification: platform and capability boundaries -- the front end
            # [EN] uses this to show the degraded-mode banner and to check that both sides
            # [EN] judge the platform the same way.
            # [ZH] 相关设计：平台与能力边界 —— 前端据此显示降级横幅，并校验前后端平台判断一致
            "platform": platform_support.report(),
            "paths": {"program": program_dir(), "data": user_data_root()}})

    def _stream_worker(request: Dict[str, Any]) -> None:
        saw_terminal = False
        try:
            for response in backend.stream(request):
                if response.get("type") in ("done", "error"):
                    saw_terminal = True
                _write(response)
            # [EN] A generator that ends without a terminal line would leave the interface
            # [EN] waiting for a completion that never arrives; report it explicitly.
            # [ZH] 生成器未产出终止行就结束，会使界面一直等待永远不会到来的完成信号；此处
            # [ZH] 必须显式报告。
            if not saw_terminal:
                _write({"type": "error", "ok": False,
                        "content": _t("sys.empty_output")})
        except Exception as exc:  # noqa: BLE001
            # [EN] A failure inside a streaming generator must be reported as a structured
            # [EN] terminal event; otherwise the interface waits forever for a "done" line.
            # [ZH] 流式生成器内部的失败必须转成结构化终止事件；否则界面会一直等待
            # [ZH] "done" 行而永久挂起。
            log.exception("流式请求失败")
            _write({"type": "error", "ok": False,
                    "content": _t("sys.internal_error", error=exc)})
        finally:
            backend.stream_active = False
            backend.stop_event.clear()
            try:
                backend.save_session()
            except Exception:  # noqa: BLE001
                log.warning("流式请求结束后保存会话失败", exc_info=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # [EN] Tolerate a UTF-8 BOM that the front end may include when writing to stdin.
        # [ZH] 兼容前端写入 stdin 时可能带上的 UTF-8 BOM
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff").strip()
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log.warning("收到非法 JSON: %r", line)
            continue

        action = request.get("action")

        # [EN] While a stream is active only ``stop`` and lightweight read-only probes
        # [EN] are accepted; any new conversation or file request is told that the
        # [EN] backend is busy. Reader: the local API is removed, so the stream flag is
        # [EN] the only source of "busy".
        # [ZH] 流式生成期间只接受 ``stop`` 与轻量只读探测；新的对话/文件请求提示忙碌。
        # [ZH] Reader：本地 API 已删除，因此"忙碌"只由流式标志决定。
        if backend.stream_active and action != "stop":
            # [EN] Liveness probes and background-job polling are lightweight and
            # [EN] must remain available while a long task runs (v0.2.6 task 2.1).
            # [ZH] 存活探测与后台作业轮询属于轻量请求，长任务运行期间必须保持
            # [ZH] 可用（v0.2.6 任务 2.1）。
            is_lightweight = action in ("ping", "file_job_status", "file_job_cancel",
                                        # [EN] Read-only state queries.
                                        # [ZH] 只读状态查询。
                                        "inference_config", "model_state")
            if not is_lightweight:
                # [EN] Busy flag: the
                # [EN] single stdout stream is shared, and two threads writing at once
                # [EN] interleave into broken JSON, so concurrent requests are refused while
                # [EN] long tasks run. The structured flag lets the UI say "the backend is
                # [EN] busy" instead of "the model center is broken".
                # [ZH] busy 标记（此前版本 模型市场优化）：单条 stdout 是共享流，
                # [ZH] 两个线程同时写会交错成坏 JSON，所以长任务期间一律拒绝并发请求。
                # [ZH] 带上结构化标记，界面才能说清"是后端忙"而不是"模型中心坏了"。
                _write({"type": "system", "content": _t("sys.busy"), "busy": True})
                continue

        if action in ("chat_stream", "file_stream", "inference_tune_stream"):
            # [EN] Long tasks run on streaming threads: progress is emitted live and
            # [EN] stop can interrupt them. ``inference_tune_stream`` streams each
            # [EN] benchmark trial, so the operator sees the tuning progress immediately.
            # [ZH] 长任务都走流式线程：进度实时输出，stop 可中断。
            # [ZH] ``inference_tune_stream`` 逐个试验流式上报基准测试，操作者可即时看到调优进度。
            backend.stop_event.clear()
            backend.stream_active = True
            threading.Thread(target=_stream_worker, args=(request,), daemon=True).start()
        elif action == "stop":
            if backend.stream_active:
                backend.stop_event.set()
                _write({"type": "system", "content": _t("sys.stop_sent")})
            else:
                _write({"type": "system", "content": _t("sys.stop_none")})
        elif action in ("exit", "quit"):
            # [EN] Design specification: exit is only triggered by the user. Under the old
            # [EN] behaviour "refusing to download the model" also reached this point; now
            # [EN] the backend keeps running without a model, so only the front end's "exit"
            # [EN] button uses this path.
            # [ZH] 相关设计：退出只由用户主动触发。旧行为里"拒绝下载模型"也会走到这里，
            # [ZH] 现在缺模型时后端照常运行，这条路只剩前端"退出"按钮会用到。
            log_user(log, "sys.log_exit_requested")
            backend.save_session()
            _write({"type": "system", "content": _t("sys.exit_ack")})
            return
        else:
            # [EN] v0.2.2 batch 4: open the "host progress" channel while a non-streaming
            # [EN] request is handled, so that the chunked-summary progress of actions such
            # [EN] as `/file` also reaches the front end as structured events (previously it
            # [EN] could only write to stderr, which the front end treated as one log line).
            # [EN] The channel is closed again immediately afterwards.
            # [ZH] v0.2.2 第 4 批：非流式请求处理期间打开"宿主进度"通道，
            # [ZH] 让 `/file` 这类动作的分段摘要进度也能以结构化事件送到前端
            # [ZH] （此前只能写 stderr，前端当一行日志看待）。处理完立即关掉。
            global _host_progress_sink
            _host_progress_sink = _emit_host_progress
            try:
                response = backend.handle(request)
            except Exception as exc:  # noqa: BLE001
                # [EN] P0 invariant: no request handler may terminate the process. A
                # [EN] single unparsed extension or a missing file used to raise through
                # [EN] this loop and kill the backend; now the failure is logged and
                # [EN] returned as a structured error the interface can display.
                # [ZH] P0 不变量：任何请求处理函数都不得终止进程。此前单个未解析的扩展名
                # [ZH] 或缺失文件会一路抛出本循环并杀死后端；现在记录日志并返回界面可
                # [ZH] 显示的结构化错误。
                log.exception("处理请求失败: action=%r", action)
                response = {"type": "error", "ok": False,
                            "content": _t("sys.internal_error", error=exc),
                            "error": str(exc), "action": action}
            finally:
                _host_progress_sink = None
            # [EN] Attach the context usage; it decreases in real time after /clear or /undo.
            # [ZH] 附带上下文占用，/clear、/undo 后实时减少
            backend._attach_context(response)
            _write(response)
            backend.save_session()
            if response.get("action") == "exit":
                log.info("%s", _t("sys.exit_ack"))
                return


if __name__ == "__main__":
    # [EN] Process-level crash and exception hooks are installed only when the program is
    # [EN] really running as the backend process (design specification).
    # [ZH] 进程级崩溃/异常钩子只在真正作为后端进程运行时安装（相关设计）。
    install_crash_handlers()
    main()
