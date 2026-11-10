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
  {"action": "model_download", "name": "..."}  -> downloads a model and hot-swaps it: delta progress + done
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
features -- file parsing, skill marketplace, settings read/write, model marketplace search and
download, session-history inspection -- **remain fully available**. After the user downloads a
model in settings, or places a ``.gguf`` file into the model directory manually, the backend
**hot-loads it in place** (same engine object, no process restart).

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
  {"action": "model_download", "name": "..."}  -> 下载模型并热切换：delta 进度 + done
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

from reasoning import ANSWER as _CH_ANSWER
from reasoning import REASONING as _CH_REASONING
from reasoning import RESET as _CH_RESET
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
    from config import AutomationConfig, Config
    # [EN] Switch the Hugging Face endpoint to the mirror before importing
    # [EN] huggingface_hub modules; otherwise huggingface_hub connects to huggingface.co by
    # [EN] default (unreachable in some regions) and downloads/queries time out.
    # [ZH] 在导入 huggingface_hub 相关模块前把 HF 端点切换到镜像，
    # [ZH] 否则 huggingface_hub 默认连接 huggingface.co（部分地区不可达），下载/查询都会超时。
    _cfg = Config.load()
    if _cfg.hf_endpoint and not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = _cfg.hf_endpoint
    from command_handler import CommandHandler
    from compact_log import EVENT_SKIP, EVENT_SUMMARY, CompactEvent, CompactLog, replay
    from config import CompactionConfig
    # [EN] Task 1 (self-test fix-1): FILE_INTENT is no longer imported. It used to gate
    # [EN] whether the file body was injected into the context; that gate has been removed
    # [EN] (see the include_file explanation in _stream_chat).
    # [ZH] 相关设计（自测问题修复-1）：不再导入 FILE_INTENT —— 它曾作为"是否把文件正文
    # [ZH] 注入上下文"的开关，现已取消（详见 _stream_chat 中 include_file 的说明）。
    from conversation import ConversationManager, REPEAT_INTENT
    from document_jobs import STATUS_CANCELLED, DocumentJobManager
    from download_utils import check_model, download_model, find_best_model
    from file_parser import is_parse_error, localize_error, parse_file_with_wps
    from gguf_meta import format_bytes as _format_bytes
    from gguf_meta import resolve_context_window
    import hardware_profiler
    from llm_engine import LazyLLMEngine, LLMEngine, NoModelError, StreamStats
    # [EN] v0.2.6 Part 2: multimodal path policy, vision adapter and speech
    # [EN] services. All three are dependency-free with respect to the locked
    # [EN] requirements (see the module notes of media/vision/speech).
    # [ZH] v0.2.6 Part 2：多模态路径策略、视觉适配层与语音服务。三者相对已锁定的
    # [ZH] 依赖清单均不新增依赖（见 media/vision/speech 的模块说明）。
    import media as media_mod
    import speech as speech_mod
    import vision as vision_mod
    # [EN] Heartbeat channel marker (task 5): the engine emits it during prefill, see
    # [EN] llm_engine._iter_with_heartbeat.
    # [ZH] 心跳通道标记（相关设计）：引擎在 prefill 阶段产出，见 llm_engine._iter_with_heartbeat。
    from llm_engine import HEARTBEAT as _CH_HEARTBEAT
    from search_engine import FAIL_KIND_KEYS, SearchEngine
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
from config import program_dir, program_root as user_data_root

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


def _excepthook(etype, value, tb):
    """
    [EN]
    Also write uncaught Python exceptions to the crash log so that silent termination does
    not occur.

    [ZH]
    未捕获的 Python 异常也写入崩溃日志，避免进程静默退出。
    """
    import traceback
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

    [ZH]
    按文件名推断量化文本（Q6_K / Q4_K_M）；无法获取时返回空串。
    """
    try:
        from model_hub import guess_quantization
        return guess_quantization(name)
    except Exception:  # noqa: BLE001
        return ""


def _write(response: Dict[str, Any]) -> None:
    with _io_lock:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


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
        # [EN] v0.2.6 Part 2: media services. Vision and transcription are created
        # [EN] lazily (their modules load native libraries); the recorder is a plain
        # [EN] state machine and costs nothing until started.
        # [ZH] v0.2.6 Part 2：媒体服务。视觉与转写惰性创建（其模块会加载原生库）；
        # [ZH] 录音器只是状态机，启动前没有开销。
        self._vision_engine: Any = None
        self._transcriber: Any = None
        self._media_recorder = speech_mod.AudioRecorder(
            max_seconds=int(getattr(getattr(config, "media", None),
                                    "max_record_seconds", 60) or 60))
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
        self.search = SearchEngine(config.search_results,
                                   searxng_url=getattr(config, "searxng_url", ""))
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
        # [EN] Pending per-invocation confirmations for guard tools (v0.2.6
        # [EN] task 2.4). The agent worker registers a waiter, the dispatcher
        # [EN] resolves it when the frontend answers.
        # [ZH] 高危工具的待确认请求（v0.2.6 任务 2.4）。Agent 工作线程登记等待者，
        # [ZH] 前端答复后由分发器解除。
        self._pending_tool_confirmations: Dict[str, Dict[str, Any]] = {}
        self._tool_confirm_lock = threading.Lock()
        self.commands = CommandHandler(
            self.conv, self.search, self.llm, config, summarizer=self.summarizer,
            file_loader=self.load_file_into_context, summarize_file=self.summarize_current_file,
            files_loader=self.load_files_into_context,
        )
        # [EN] Browser automation engine (Playwright): created lazily, not loaded unless used.
        # [ZH] 浏览器自动化引擎（Playwright）：惰性创建，未使用时不加载。
        self._automation = None
        # [EN] Skill marketplace: also created lazily.
        # [ZH] 技能市场：同样惰性创建。
        self._market = None
        # [EN] Model selector (feature 4): created lazily.
        # [ZH] 模型选择栏（新增四）：惰性创建。
        self._model_hub = None
        # [EN] Skill runtime loader (task 2): also created lazily; the directory is scanned
        # [EN] only on first use.
        # [ZH] 技能运行期加载器（相关设计）：同样惰性创建，首次使用时才扫描目录。
        self._skills = None
        # [EN] Fix 4: chunked-summary cache (direction 8) and the wiring of the "manual
        # [EN] context compaction" callback.
        # [ZH] 兼容性修正：分段摘要缓存（方向八）与"手动压缩上下文"回调接线。
        self._summary_cache = None
        self._capability_cache = None
        self._capability_key = None
        self.commands.compact_provider = self.compact_context
        self.commands.profile_provider = self.model_capability
        self.commands.cache_clear_provider = self.clear_summary_cache
        self.commands.agent_configurator = self.configure_agent
        self.commands.mcp_control = self.mcp_control
        self.stream_active = False
        self.stop_event = threading.Event()
        # [EN] Observable state of the search pipeline (known-issues optimization-2, task 2):
        # [EN] the result object of the last search and the explanation of "why it was not
        # [EN] used". The task requires per-stage investigation, so this state is retained for
        # [EN] inspection.
        # [ZH] 搜索链路可观测状态（优化已知问题-2 相关设计）：上一次搜索的结果对象与
        # [ZH] "为什么未使用"的说明。设计规范要求逐环节排查，因此状态需保留以便查询。
        self._last_search = None
        self._last_search_note = ""
        # [EN] Compaction event log (task 9): created lazily, see compact_log().
        # [ZH] 压缩事件日志（相关设计）：惰性创建，见 compact_log()。
        self._compact_log = None
        # [EN] Agent tool registry (capability extension-1, task 2): created lazily; the trace
        # [EN] of the last run is retained so /agent and agent_diagnostics() can show "what
        # [EN] each step did".
        # [ZH] Agent 工具注册表（后续版本 相关设计）：惰性创建；上一次运行的轨迹留档，
        # [ZH] 供 /agent 与 agent_diagnostics() 显示"每一步做了什么"。
        self._agent_registry = None
        self._last_agent_run = None
        self._agent_run = None
        # [EN] Summary of the last retrieval (used by /status and retrieval diagnostics to
        # [EN] show "what was actually retrieved this time").
        # [ZH] 上一次检索的摘要（供 /status 与检索诊断显示"本次实际检索了什么"）。
        self._last_retrieval = None
        # [EN] Local document retrieval (capability extension-1, task 6): created lazily; the
        # [EN] index is updated **incrementally** as files are added or removed.
        # [ZH] 本地文档检索（后续版本 相关设计）：惰性创建，索引随文件增删**增量**更新。
        self._retriever = None
        # [EN] MCP client (capability extension-1, task 3): created lazily; rebuilt and the
        # [EN] old process closed when the config signature changes.
        # [ZH] MCP 客户端（后续版本 相关设计）：惰性创建；配置签名变化时重建并关闭旧进程。
        self._mcp_bridge = None
        self._mcp_signature = ""
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

        [ZH]
        模型文件夹的实际路径（引导文案中必须给出真实路径，不能使用占位符）。
        """
        try:
            return self.model_hub().models_dir
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
            n_threads_override=int(cfg.n_threads or 0),
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
            requested_gpu_layers=int(cfg.n_gpu_layers or 0),
            n_threads_override=int(cfg.n_threads or 0),
            inference_profile=str(getattr(cfg, "inference_profile", "")),
            requested_n_batch=int(cfg.n_batch or 0),
            allow_override=bool(getattr(cfg, "inference_override", False)),
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
            cfg.n_batch = int(preset.n_batch)
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
                requested_gpu_layers=int(self.config.n_gpu_layers or 0),
                n_threads_override=int(self.config.n_threads or 0),
                inference_profile=str(getattr(self.config, "inference_profile", "")),
                requested_n_batch=int(self.config.n_batch or 0),
                allow_override=bool(getattr(self.config, "inference_override", False)),
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

        The scan results come from ``model_hub.registry()`` (which also registers files
        placed into the directory by hand). With exactly one model it is loaded automatically
        and reported; with several models only the list is returned and the user chooses.

        [ZH]
        重新扫描模型文件夹（任务二：手动放置 ``.gguf`` 后能被识别）。

        扫描结果来自 ``model_hub.registry()``（它会把手工放入目录的文件一并登记）。
        只有一个模型时自动加载并提示；多个模型时只返回清单，由用户选择。
        """
        hub = self.model_hub()
        info = hub.registry(current_model=str(getattr(self.llm, "model_path", "") or ""))
        models = [m for m in info.get("models", []) if m.get("exists")]
        loaded = ""
        error = ""
        if auto_load and not self.model_ready and len(models) == 1:
            result = self.load_model(str(models[0].get("name") or ""))
            loaded = result.get("name", "") if result.get("ok") else ""
            error = str(result.get("error") or "")
        return {
            "count": len(models),
            "models": [{"name": m.get("name"), "size_human": m.get("size_human"),
                        "quantization": m.get("quantization")} for m in models],
            "models_dir": info.get("models_dir", self.models_dir()),
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

    def skills(self):
        """
        [EN]
        Create the skill loader on demand and wire it into the command table, file parsing,
        and automation hooks (task 2, 2.2).

        [ZH]
        按需创建技能加载器，并把它接入指令表 / 文件解析 / 自动化钩子（任务二 2.2）。
        """
        if self._skills is None:
            from skills_runtime import SkillLoader

            self._skills = SkillLoader(self.config.marketplace, self.commands, logger=log)
            try:
                from file_parser import skill_parser_extensions

                self.commands.skill_parsers_provider = skill_parser_extensions
            except Exception:
                self.commands.skill_parsers_provider = None
        else:
            self._skills.cfg = self.config.marketplace
            self._skills.commands = self.commands
        return self._skills

    def load_skills(self) -> dict:
        """
        [EN]
        Load all plugins at startup or on reload; every failure is isolated and does not
        affect backend startup.

        [ZH]
        启动时/重载时加载全部插件；任何失败都被隔离，不影响后端启动。
        """
        try:
            return self.skills().load_all()
        except Exception as e:  # noqa: BLE001
            log.exception("技能加载失败（已忽略，后端继续启动）")
            return {"total": 0, "loaded": 0, "failed": 0, "skipped": 0, "error": str(e)}

    def model_hub(self):
        """
        [EN]
        Create the model hub on demand (feature 4).

        [ZH]
        按需创建模型中心（新增四）。
        """
        if self._model_hub is None:
            from model_hub import ModelHub

            self._model_hub = ModelHub(self.config.model_hub, app_config=self.config)
        else:
            self._model_hub.cfg = self.config.model_hub
        return self._model_hub

    def market(self):
        """
        [EN]
        Create the skill marketplace on demand (avoids loading network dependencies when the
        marketplace is not used).

        [ZH]
        按需创建技能市场（避免未使用市场时也加载网络相关依赖）。
        """
        if self._market is None:
            from marketplace import Marketplace

            self._market = Marketplace(self.config.marketplace)
        else:
            self._market.cfg = self.config.marketplace
        return self._market

    def automation(self):
        """
        [EN]
        Create the automation engine on demand (avoids loading Playwright when automation is
        not used).

        [ZH]
        按需创建自动化引擎（避免未使用自动化时也加载 Playwright）。
        """
        if self._automation is None:
            from automation import AutomationEngine

            self._automation = AutomationEngine(self.config.automation, llm_engine=self.llm)
            # [EN] Skill automation hooks (task 2, 2.3): attach the plugin hooks such as
            # [EN] after_task / after_download here.
            # [ZH] 技能自动化钩子（相关设计 2.3）：把插件的 after_task / after_download 等挂载上来。
            try:
                self._automation.hook_dispatcher = self.skills().fire_hook
            except Exception:
                log.warning("注入技能自动化钩子失败（自动化仍可正常使用）", exc_info=True)
            # [EN] A previous forced termination may have left download temp files behind;
            # [EN] clean them up once here (consistent with PRIVACY.md: "temp directories left
            # [EN] by an abnormal exit are cleaned on the next startup").
            # [ZH] 上次进程被强制终止时可能残留下载临时文件，此处清理一次
            # [ZH] （与 PRIVACY.md 中"程序异常退出时残留的临时目录会在下次启动时清理"一致）。
            try:
                removed = self._automation.cleanup_temp()
                if removed:
                    log_user(log, "sys.log_automation_cleaned", count=removed)
            except Exception:
                log.warning("启动清理自动化临时目录失败", exc_info=True)
        else:
            self._automation.cfg = self.config.automation
        return self._automation

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

        The initial window is the planned small allocation, while the hardware ceiling is
        retained by the engine for on-demand growth (see
        ``llm_engine.LLMEngine.maybe_grow_context``). The thread count, accelerator choice
        and GPU layer count all come from ``hardware_profiler.plan_inference``; no tuning
        value is hard-coded in this method.

        When ``model_path`` is empty, :class:`LazyLLMEngine` (an empty engine) is returned:
        that is the carrier of the "no model ready" state -- the backend starts as usual and
        :meth:`Backend.load_model` **loads in place** once a model is available.

        [ZH]
        依据硬件档案与所选模型创建推理引擎。

        初始窗口取规划后的较小分配值，硬件上限则保留在引擎内供按需扩展（见
        ``llm_engine.LLMEngine.maybe_grow_context``）。线程数、加速器选择与 GPU 层数
        全部来自 ``hardware_profiler.plan_inference``；本方法不再硬编码任何调优值。

        ``model_path`` 为空时返回 :class:`LazyLLMEngine`（空引擎）：这是"无模型就绪"
        的载体，后端照常启动，等模型到位后由 :meth:`Backend.load_model` **原地加载**。
        """
        profile = hardware_profiler.probe()
        path = str(model_path or "").strip()
        if not path:
            plan = hardware_profiler.plan_inference(
                profile=profile,
                requested_context=int(config.context_length or 0),
                max_context=int(config.max_context or hardware_profiler.DEFAULT_MAX_CONTEXT),
                requested_gpu_layers=0,
                n_threads_override=int(config.n_threads or 0),
                inference_profile=str(getattr(config, "inference_profile", "")),
                requested_n_batch=int(config.n_batch or 0),
                allow_override=bool(getattr(config, "inference_override", False)),
            )
            log.info("%s", _t("sys.log_model_none_state"))
            engine = LazyLLMEngine(
                plan.n_ctx_initial, n_threads=plan.n_threads,
                n_batch=plan.n_batch, n_gpu_layers=plan.n_gpu_layers,
                reasoning=getattr(config, "reasoning", None),
                profile=profile, max_n_ctx=plan.n_ctx_max,
                backend_label=plan.backend,
            )
            engine.inference_plan = plan.to_dict()
            return engine
        train_ctx = resolve_context_window(path, 0)
        plan = hardware_profiler.plan_inference(
            profile=profile,
            model_path=path,
            train_context=train_ctx,
            requested_context=int(config.context_length or 0),
            max_context=int(config.max_context or hardware_profiler.DEFAULT_MAX_CONTEXT),
            requested_gpu_layers=int(config.n_gpu_layers or 0),
            n_threads_override=int(config.n_threads or 0),
            inference_profile=str(getattr(config, "inference_profile", "")),
            requested_n_batch=int(config.n_batch or 0),
            allow_override=bool(getattr(config, "inference_override", False)),
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
            n_threads=plan.n_threads, n_batch=plan.n_batch,
            n_gpu_layers=plan.n_gpu_layers,
            reasoning=getattr(config, "reasoning", None),
            profile=profile, max_n_ctx=plan.n_ctx_max,
            backend_label=plan.backend,
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

    def touch_model_usage(self) -> None:
        """
        [EN]
        Record the "last used" time of the current model (feature 4: the model library list
        displays it).

        [ZH]
        记录当前模型的"最后使用时间"（新增四：模型库列表需要显示该信息）。
        """
        try:
            self.model_hub().touch(os.path.basename(self.config.model_path or ""))
        except Exception:
            log.debug("更新模型最后使用时间失败", exc_info=True)

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

    def _media_cfg(self):
        """
        [EN]
        Return the media configuration section, tolerating configs created before
        this section existed (tests and third-party constructions).

        [ZH]
        返回媒体配置节；兼容该节存在之前构造的配置对象（测试与第三方构造）。
        """
        value = getattr(self.config, "media", None)
        if value is None:
            from config import MediaConfig
            value = MediaConfig()
            self.config.media = value
        return value

    def _vision_model_paths(self) -> tuple:
        """
        [EN]
        Resolve the vision model and projector paths from configuration.

        An empty ``vision_model`` means "use the active text model". Relative
        configured values resolve against the model directory, so a user can write
        a bare file name in ``config.json``.

        [ZH]
        由配置解析视觉模型与投影路径。

        ``vision_model`` 为空表示"使用当前文本模型"。配置中的相对值按模型目录解析，
        因此用户可以在 ``config.json`` 中只写文件名。
        """
        cfg = self._media_cfg()
        raw_model = str(getattr(cfg, "vision_model", "") or "").strip()
        if raw_model:
            model = raw_model if os.path.isabs(raw_model) else os.path.join(self.models_dir(), raw_model)
        else:
            model = self._current_model_path()
        raw_mmproj = str(getattr(cfg, "vision_mmproj", "") or "").strip()
        if raw_mmproj and not os.path.isabs(raw_mmproj):
            raw_mmproj = os.path.join(self.models_dir(), raw_mmproj)
        mmproj = vision_mod.find_mmproj(model, raw_mmproj)
        return model, mmproj

    def _vision_service(self):
        """
        [EN]
        Return the lazily created vision service, synchronized with the current
        model selection and inference tuning.

        [ZH]
        返回惰性创建的视觉服务，并与当前模型选择及推理调优同步。
        """
        cfg = self._media_cfg()
        model, mmproj = self._vision_model_paths()
        n_ctx = int(getattr(cfg, "vision_n_ctx", vision_mod.DEFAULT_VISION_CONTEXT)
                    or vision_mod.DEFAULT_VISION_CONTEXT)
        n_threads = int(getattr(self.llm, "n_threads", 0) or 0)
        n_gpu_layers = int(getattr(self.llm, "n_gpu_layers", 0) or 0)
        if self._vision_engine is None:
            self._vision_engine = vision_mod.VisionService(
                model, mmproj, n_ctx=n_ctx, n_threads=n_threads,
                n_gpu_layers=n_gpu_layers)
        else:
            self._vision_engine.configure(model, mmproj, n_ctx=n_ctx)
        return self._vision_engine

    def _stt_service(self):
        """
        [EN]
        Build a transcription service from configuration. The object is cheap; the
        underlying executable is started only by ``transcribe_audio``.

        [ZH]
        由配置构造转写服务。该对象代价很低；底层可执行文件只在
        ``transcribe_audio`` 中启动。
        """
        cfg = self._media_cfg()
        model = str(getattr(cfg, "stt_model", "") or "").strip()
        if model and not os.path.isabs(model):
            model = os.path.join(self.models_dir(), model)
        return speech_mod.WhisperTranscriber(
            executable=str(getattr(cfg, "stt_executable", "") or ""),
            model=model,
            models_dir=self.models_dir(),
            language=str(getattr(cfg, "stt_language", "auto") or "auto"),
        )

    def _media_failure(self, code: str, message: str, **extra: Any) -> Dict[str, Any]:
        """
        [EN]
        Uniform failure payload for media actions. ``error_code`` is stable and
        machine-readable; ``content`` is already localized.

        [ZH]
        媒体动作的统一失败负载。``error_code`` 稳定且机器可读；``content`` 已本地化。
        """
        payload: Dict[str, Any] = {"type": "system", "ok": False,
                                   "content": str(message), "error_code": str(code)}
        payload.update(extra)
        return payload

    def media_state(self) -> Dict[str, Any]:
        """
        [EN]
        Report media capabilities, staging directories and current settings.

        The interface uses this single response to learn where to write an image
        or a recording, which voices exist, and which capability is unavailable.
        Directories are created on demand, so an empty data directory is not an
        error.

        [ZH]
        报告媒体能力、暂存目录与当前设置。

        界面通过这一个响应获知图片/录音的写入位置、可用音色以及哪项能力不可用。
        目录按需创建，因此空数据目录不视为错误。
        """
        cfg = self._media_cfg()
        dirs = media_mod.ensure_media_dirs()
        vision_status = self._vision_service().status()
        stt_status = self._stt_service().status()
        tts = speech_mod.list_voices()
        return {
            "type": "media_state",
            "ok": True,
            "content": "",
            "dirs": {**media_mod.describe_dirs(), **dirs},
            "vision": {**vision_status,
                       "enabled": bool(getattr(cfg, "vision_enabled", True))},
            "stt": {**stt_status,
                    "enabled": bool(getattr(cfg, "stt_enabled", True))},
            "tts": {
                **tts,
                "enabled": bool(getattr(cfg, "tts_enabled", True)),
                "auto_read": bool(getattr(cfg, "tts_auto_read", False)),
                "voice": str(getattr(cfg, "tts_voice", "") or ""),
                "rate": int(getattr(cfg, "tts_rate", 0) or 0),
            },
            "ptt": {
                "enabled": bool(getattr(cfg, "ptt_enabled", True)),
                "shortcut": str(getattr(cfg, "ptt_shortcut", "RightCtrl") or "RightCtrl"),
            },
            "recording": {
                "active": bool(self._media_recorder.active),
                "max_seconds": int(getattr(cfg, "max_record_seconds", 60) or 60),
            },
            "retention_days": int(getattr(cfg, "temp_retention_days", 7) or 0),
        }

    def image_chat(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Answer a prompt about one staged local image.

        The image itself never crosses the protocol: the request carries an
        absolute path inside ``temp/images`` and this method validates it before
        the vision backend reads it. On success both the user turn and the answer
        are appended to the conversation history so that a follow-up question can
        refer to the previous answer; the image is described to the text model
        only through that history text (image bytes are not retained).

        [ZH]
        针对一张已暂存的本地图片回答提示词。

        图片本身从不跨协议传递：请求携带 ``temp/images`` 内的绝对路径，本方法在
        视觉后端读取之前完成校验。成功时用户轮与回答都追加进对话历史，使后续提问
        可以引用上一轮回答；图片仅通过该历史文本被文本模型感知（不保留图片字节）。
        """
        cfg = self._media_cfg()
        if not bool(getattr(cfg, "vision_enabled", True)):
            return self._media_failure("vision_disabled", _t("media.vision_disabled"))
        if not self.model_ready:
            return self._media_failure("no_model", self.guide_text(), guide=True)
        try:
            image_path = media_mod.validate_image_path(str(request.get("path") or ""))
        except media_mod.MediaError as exc:
            return self._media_failure(exc.code, exc.localize(_t))
        prompt = str(request.get("prompt") or "").strip() or _t("media.image_default_prompt")
        service = self._vision_service()
        reasons = service.status()
        if not reasons.get("available"):
            return self._media_failure(
                "vision_unavailable", str(reasons.get("reason_text") or ""),
                vision=reasons)
        try:
            max_tokens = int(request.get("max_tokens")
                             or getattr(cfg, "vision_max_tokens", 512) or 512)
            temperature = float(request.get("temperature")
                                if request.get("temperature") is not None
                                else getattr(cfg, "vision_temperature", 0.3))
            answer = service.chat(image_path, prompt, max_tokens=max_tokens,
                                  temperature=temperature)
        except vision_mod.VisionError as exc:
            return self._media_failure(exc.code, exc.localize(_t),
                                       detail=str(exc.kwargs.get("detail") or ""))
        if not answer:
            return self._media_failure("vision_empty", _t("media.vision_empty"))
        name = os.path.basename(image_path)
        # [EN] Record the turn with an explicit marker, so a later text model call
        # [EN] knows the content came from an image rather than from the user's typing.
        # [ZH] 以显式标记记录该轮，使后续文本模型调用知道内容来自图片而非用户键入。
        try:
            self.conv.add_message("user", _t("media.image_user_message", name=name,
                                             prompt=prompt))
            self.conv.add_message("assistant", answer)
            self._compact_history()
        except Exception:  # noqa: BLE001 - [EN] history recording must not lose the answer
                           # [ZH] 记录历史失败不得丢失回答
            log.warning("记录图片对话历史失败", exc_info=True)
        return {"type": "assistant", "ok": True, "content": answer,
                "image": name, "image_path": image_path,
                "vision": {k: reasons.get(k) for k in
                           ("model", "mmproj", "handler", "loaded")}}

    def transcribe_audio(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Transcribe a staged audio file with the local Whisper executable and return
        the recognized text.

        [ZH]
        使用本地 Whisper 可执行文件转写已暂存的音频，并返回识别文本。
        """
        cfg = self._media_cfg()
        if not bool(getattr(cfg, "stt_enabled", True)):
            return self._media_failure("stt_disabled", _t("media.stt_disabled"))
        try:
            audio_path = media_mod.validate_audio_path(str(request.get("path") or ""))
        except media_mod.MediaError as exc:
            return self._media_failure(exc.code, exc.localize(_t))
        transcriber = self._stt_service()
        status = transcriber.status()
        if not status.get("available"):
            return self._media_failure(
                "stt_unavailable", str(status.get("reason_text") or ""), stt=status)
        try:
            result = transcriber.transcribe(
                audio_path, language=str(request.get("language") or "") or None)
        except speech_mod.SpeechError as exc:
            return self._media_failure(exc.code, exc.localize(_t),
                                       detail=str(exc.kwargs.get("detail") or ""))
        text = str(result.get("text") or "").strip()
        if not text:
            return self._media_failure("stt_empty", _t("media.stt_empty"))
        return {"type": "transcription", "ok": True, "content": text, "text": text,
                "path": audio_path, "elapsed_ms": int(result.get("elapsed_ms") or 0),
                "stt": {k: status.get(k) for k in ("executable", "model", "language")}}

    def tts_generate(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Synthesize speech for the supplied text and return the absolute audio path.

        Playback belongs to the interface layer, which uses the platform's own
        player; the backend only produces a file below ``temp/audio/tts``.

        [ZH]
        为给定文本合成语音并返回音频绝对路径。

        播放由界面层使用平台自带播放器完成；后端只负责在 ``temp/audio/tts``
        下产出文件。
        """
        cfg = self._media_cfg()
        if not bool(getattr(cfg, "tts_enabled", True)):
            return self._media_failure("tts_disabled", _t("media.tts_disabled"))
        text = str(request.get("text") or "").strip()
        if not text:
            return self._media_failure("tts_empty", _t("media.tts_empty_text"))
        voice = str(request.get("voice") or getattr(cfg, "tts_voice", "") or "")
        try:
            rate = int(request.get("rate")
                       if request.get("rate") is not None
                       else getattr(cfg, "tts_rate", 0))
        except (TypeError, ValueError):
            rate = int(getattr(cfg, "tts_rate", 0) or 0)
        target = speech_mod.tts_output_path()
        try:
            speech_mod.ensure_media_dirs()
            path = speech_mod.synthesize_speech(text, target, voice=voice, rate=rate)
        except speech_mod.SpeechError as exc:
            return self._media_failure(exc.code, exc.localize(_t),
                                       detail=str(exc.kwargs.get("detail") or ""))
        return {"type": "audio", "ok": True, "content": "", "path": path,
                "format": os.path.splitext(path)[1].lstrip(".").lower(),
                "voice": voice, "rate": rate}

    def audio_record_start(self) -> Dict[str, Any]:
        """
        [EN]
        Start a push-to-talk recording. The interface sends this on key press and
        receives the target path immediately.

        [ZH]
        开始按键录音。界面在按下按键时发送本动作，并立即收到目标路径。
        """
        try:
            info = self._media_recorder.start()
        except speech_mod.SpeechError as exc:
            return self._media_failure(exc.code, exc.localize(_t),
                                       detail=str(exc.kwargs.get("detail") or ""))
        return {"type": "recording", "ok": True, "content": "",
                "active": True, "path": info.get("path", ""),
                "max_seconds": info.get("max_seconds", 0)}

    def audio_record_stop(self) -> Dict[str, Any]:
        """
        [EN]
        Stop the active recording and return the finished WAV path. The interface
        then calls ``transcribe_audio`` with that path.

        [ZH]
        停止当前录音并返回成品 WAV 路径。随后界面以该路径调用 ``transcribe_audio``。
        """
        try:
            info = self._media_recorder.stop()
        except speech_mod.SpeechError as exc:
            return self._media_failure(exc.code, exc.localize(_t),
                                       detail=str(exc.kwargs.get("detail") or ""))
        return {"type": "recording", "ok": True, "content": "", "active": False,
                "path": info.get("path", ""),
                "duration_seconds": info.get("duration_seconds", 0),
                "size_bytes": info.get("size_bytes", 0)}

    def audio_record_cancel(self) -> Dict[str, Any]:
        """
        [EN]
        Abort the active recording and discard the captured data.

        [ZH]
        中止当前录音并丢弃已采集数据。
        """
        self._media_recorder.cancel()
        return {"type": "recording", "ok": True, "content": "",
                "active": False, "cancelled": True}

    def media_config_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Update the interface-facing subset of the media configuration.

        Only switches and speech-generation choices are accepted here; model and
        executable paths stay in ``config.json`` because changing them requires a
        model load and a path policy decision that the settings page cannot make.
        Unknown fields are ignored, and the persisted file is written through
        :meth:`Config.save` so validation always runs.

        [ZH]
        更新媒体配置中面向界面的子集。

        此处只接受开关与语音合成选项；模型与可执行文件路径仍留在
        ``config.json``，因为更改它们需要加载模型并作出设置页无法完成的路径策略
        决定。未知字段被忽略，持久化经 :meth:`Config.save` 完成，保证校验执行。
        """
        cfg = self._media_cfg()
        changed: List[str] = []
        switches = {
            "vision_enabled": bool,
            "stt_enabled": bool,
            "tts_enabled": bool,
            "tts_auto_read": bool,
            "ptt_enabled": bool,
        }
        for key, caster in switches.items():
            if key in request:
                value = bool(request.get(key))
                if getattr(cfg, key) != value:
                    setattr(cfg, key, value)
                    changed.append(key)
        if "tts_voice" in request:
            voice = str(request.get("tts_voice") or "").strip()
            if str(getattr(cfg, "tts_voice", "")) != voice:
                cfg.tts_voice = voice
                changed.append("tts_voice")
        if "tts_rate" in request:
            try:
                rate = max(-10, min(10, int(request.get("tts_rate") or 0)))
            except (TypeError, ValueError):
                rate = 0
            if int(getattr(cfg, "tts_rate", 0)) != rate:
                cfg.tts_rate = rate
                changed.append("tts_rate")
        if "ptt_shortcut" in request:
            shortcut = str(request.get("ptt_shortcut") or "").strip()
            if shortcut and shortcut != getattr(cfg, "ptt_shortcut", ""):
                cfg.ptt_shortcut = shortcut
                changed.append("ptt_shortcut")
        if changed:
            try:
                self.config.save()
            except Exception as exc:  # noqa: BLE001 - [EN] persist failure must be visible
                                     # [ZH] 持久化失败必须可见
                return self._media_failure("save_failed", _t("media.config_save_failed"),
                                           detail=str(exc), changed=changed)
        state = self.media_state()
        state.update({"changed": changed})
        return state

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
        # [EN] v0.2.6 Part 2: media protocol. Every request except image_chat and
        # [EN] transcribe_audio is cheap and safe to answer while a long task runs.
        # [ZH] v0.2.6 Part 2：媒体协议。除 image_chat 与 transcribe_audio 外，
        # [ZH] 其余请求代价很低，长任务运行期间也可安全应答。
        if action == "media_state":
            return self.media_state()
        if action == "media_config_set":
            return self.media_config_set(request)
        if action == "image_chat":
            return self.image_chat(request)
        if action == "transcribe_audio":
            return self.transcribe_audio(request)
        if action == "tts_generate":
            return self.tts_generate(request)
        if action == "audio_record_start":
            return self.audio_record_start()
        if action == "audio_record_stop":
            return self.audio_record_stop()
        if action == "audio_record_cancel":
            return self.audio_record_cancel()
        if action == "confirm_tool":
            return self._handle_tool_confirmation(request)
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
        if action == "model_download":
            return self._handle_model_download(request.get("name", ""))
        if action == "context":
            return self._handle_context()
        if action == "automation_config":
            return self._handle_automation_config(request)
        if action == "automation_config_set":
            return self._handle_automation_config_set(request)
        if action == "automation_test":
            return self._handle_automation_test(request)
        if action == "automation_login_state":
            return self._handle_automation_login_state(request)
        if action == "automation_clear_login":
            return self._handle_automation_clear_login(request)
        if action == "automation_wizard_step":
            return self._handle_automation_wizard_step(request)
        if action == "automation_config_export":
            return self._handle_automation_config_export(request)
        if action == "automation_config_import":
            return self._handle_automation_config_import(request)
        if action == "automation_templates":
            return {"type": "system", "content": "", "templates": self._automation_templates()}
        if action == "automation_save_template":
            return self._handle_automation_save_template(request)
        if action == "automation_field_help":
            return {"type": "system", "content": "", "help": self._automation_field_help()}
        if action == "automation_status":
            status = self.automation().status()
            # [EN] "Enabled" is decided by the configuration (enabled is always False while
            # [EN] the safety agreement is unchecked).
            # [ZH] 是否"已启用"由配置决定（安全协议未勾选时 enabled 一定为 False）。
            status["enabled"] = bool(
                self.config.automation.enabled and self.config.automation.safety_accepted
            )
            return {"type": "system", "content": "", "automation": status}
        # [EN] ---------- Task 17: reuse an already-open browser and two-phase visual
        # [EN] selection ----------
        # [ZH] ---------- 相关设计：复用已打开的浏览器 + 两段式可视化选取 ----------
        # [EN] These are **additional** branches; the behaviour of every existing branch above
        # [EN] is unchanged: the old streaming `automation_pick` (used by the WPF UI) is
        # [EN] retained as-is and keeps working. The "synchronous request + frontend polling"
        # [EN] design was chosen over streaming because the slow part of the two-phase flow is
        # [EN] the period in which **the user prepares the page**; holding a streaming
        # [EN] connection idle during that period makes it more likely to be killed by a
        # [EN] timeout.
        # [ZH] 这些分支均为**新增**，未改动上述任何既有分支的行为：
        # [ZH] 原有的流式 `automation_pick`（WPF 界面在用）原样保留，可继续工作。
        # [ZH] 之所以采用"同步请求 + 前端轮询"而不是流式：两段式流程中真正耗时的是
        # [ZH] **用户自行准备页面**的那段时间，程序在此期间不执行操作，长期挂起一个流式连接
        # [ZH] 反而更容易因超时被中断。
        if action == "automation_browser_reuse":
            return self._handle_automation_browser_reuse(request)
        if action == "automation_launch_debug_browser":
            return self._handle_automation_launch_debug_browser(request)
        if action == "automation_pick_step":
            return self._handle_automation_pick_step(request)
        if action in ("automation_pick_open", "automation_pick_begin", "automation_pick_status",
                      "automation_pick_cancel", "automation_pick_close"):
            # [EN] The five entry points share one handler (the state machine lives in the
            # [EN] backend; each action merely advances it).
            # [ZH] 五个入口共用一个处理器（状态机在后端，动作只是推进状态机）。
            return self._handle_automation_pick_step(
                {**request, "step": action.rsplit("_", 1)[-1]}
            )
        if action == "market_search":
            return self._handle_market_search(request)
        if action == "market_detail":
            return self._handle_market_detail(request)
        if action == "market_install":
            return self._handle_market_install(request)
        if action == "market_uninstall":
            return self._handle_market_uninstall(request)
        if action == "market_installed":
            return self._handle_market_installed()
        if action == "market_status":
            return self._handle_market_status()
        # [EN] ---------- Model selector (feature 4) ----------
        # [ZH] ---------- 模型选择栏（新增四） ----------
        if action == "model_hub_search":
            return self._handle_model_hub_search(request)
        if action == "model_hub_query":
            return self._handle_model_hub_query(request)
        if action == "model_hub_detail":
            return self._handle_model_hub_detail(request)
        if action == "model_hub_registry":
            return self._handle_model_hub_registry()
        if action == "model_hub_uninstall":
            return self._handle_model_hub_uninstall(request)
        if action == "model_hub_status":
            return self._handle_model_hub_status()
        if action == "model_hub_config_set":
            return self._handle_model_hub_config_set(request)
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
            return {"type": "system", "content": "", "commands": self.commands.all_commands()}
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
                        "temperature": float(self.commands.temperature),
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
        # [EN] ---------- Agent and MCP (capability extension-1, tasks 2 / 3) ----------
        # [ZH] ---------- Agent 与 MCP（后续版本 相关设计 / 三） ----------
        if action == "agent_state":
            return {"type": "system", "content": "", "agent": self.agent_diagnostics()}
        if action == "agent_configure":
            return self._handle_agent_config(request)
        if action == "mcp_state":
            return {"type": "system", "content": "", "mcp": self.mcp_diagnostics()}
        if action == "mcp_apply":
            return self._handle_mcp_apply(request)
        if action == "retrieval_state":
            return {"type": "system", "content": "", "retrieval": self.retrieval_diagnostics()}
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
        # [EN] ---------- Skills and local plugins (task 2 / task 2 supplement) ----------
        # [ZH] ---------- 技能与本地插件（相关设计 / 相关设计·补） ----------
        if action == "skills_list":
            return {"type": "system", "content": "", "skills": self.skills().status()}
        if action == "skills_reload":
            return self._handle_skills_reload(request)
        if action == "skills_set_dev_mode":
            return self._handle_skills_dev_mode(request)
        if action == "skills_log":
            return {"type": "system", "content": "", "log": self.skills().read_log(
                int(request.get("lines") or 200))}
        if action == "skills_clear_log":
            return {"type": "system", "content": "", "ok": self.skills().clear_log()}
        if action == "skills_export_log":
            return self._handle_skills_export_log(request)
        if action == "skills_scaffold":
            return self._handle_skills_scaffold(request)
        if action == "skills_install_folder":
            return self._handle_skills_install_folder(request)
        if action == "skills_uninstall":
            return self._handle_skills_uninstall(request)
        if action == "skills_export_zip":
            return self._handle_skills_export_zip(request)
        if action == "skills_publish_check":
            return self._handle_skills_publish_check(request)
        if action == "skills_trust":
            return self._handle_skills_trust(request)
        if action == "skills_trust_bulk":
            return self._handle_skills_trust_bulk(request)
        if action == "skills_pending_review":
            # [EN] Fix 1: data source for the bulk confirmation dialog (lists only the
            # [EN] plugins that need asking and have not been answered yet).
            # [ZH] 兼容性修正：批量确认框的数据源（只列出"需要询问且尚未答复"的插件）。
            return {"type": "system", "content": "",
                    "pending": self.skills().pending_review(),
                    "skills": self.skills().status()}
        if action == "skills_manifest_get":
            return {"type": "system", "content": "", "manifest": self.skills().local_manifest()}
        if action == "skills_manifest_set":
            return self._handle_skills_manifest_set(request)
        if action == "skills_panel_render":
            return {"type": "system", "content": "", "panel_result": self.skills().panel_render(
                str(request.get("skill_id") or ""), str(request.get("panel_id") or ""))}
        if action == "skills_panel_action":
            return self._handle_skills_panel_action(request)
        if action == "skills_examples":
            return self._handle_skills_examples(request)
        # [EN] ---------- Auto-update (task 5, option A: check and notify only) ----------
        # [ZH] ---------- 自动更新（相关设计 方案 A：只检查与提示） ----------
        if action == "update_check":
            return self._handle_update_check(request)
        # [EN] v0.2.4 patch: the user must explicitly ask for a download; the backend
        # [EN] never downloads on its own. Applying is a separate, equally explicit step.
        # [ZH] v0.2.4 补丁：下载必须由用户显式触发，后端绝不自行下载。应用更新同样是
        # [ZH] 一个独立的、同样需要显式触发的步骤。
        if action == "update_download":
            return self._handle_update_download(request)
        if action == "update_apply":
            return self._handle_update_apply(request)
        if action == "update_ignore":
            return self._handle_update_ignore(request)
        return {"type": "system", "content": _t("sys.unknown_action")}

    # [EN] ---------- Multilingual support (task 3, 3.4) ----------
    # [ZH] ---------- 多语言（相关设计 3.4） ----------

    def _handle_set_language(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Switch the backend language: affects system messages, error messages, and command
        help.

        The selection is written to ui_state.json by the frontend (task 3.4 persistence
        requirement); the backend applies it only for the current run, so switching language
        does not rewrite config.json.

        [ZH]
        切换后端语言：影响系统消息、错误提示与指令帮助。

        选择结果由前端写入 ui_state.json（设计规范 3.4 持久化要求），
        后端只在本次运行内生效，避免为了切换语言而改写 config.json。
        """
        locale = str(request.get("locale") or "auto")
        effective = _i18n().set_locale(locale)
        log_user(log, "sys.log_language_switched", effective=effective, locale=locale)
        info = _i18n().info()
        # [EN] After a language change, command help and skill panel titles must follow.
        # [ZH] 语言变化后指令帮助与技能面板标题都需要随之变化。
        return {"type": "system", "content": _t("sys.language_switched", locale=effective),
                "i18n": info, "commands": self.commands.all_commands()}

    def _handle_language(self) -> Dict[str, Any]:
        return {"type": "system", "content": "", "i18n": _i18n().info()}

    # [EN] ---------- Skills and local plugins (task 2 / task 2 supplement) ----------
    # [ZH] ---------- 技能与本地插件（相关设计 / 相关设计·补） ----------

    def _handle_skills_reload(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Hot-reload plugins (task 2B.5): changes in skills_dev/ become visible without a
        restart.

        The ``scope`` parameter (capability extension-1, task 4): ``"dev"`` reloads only
        plugins in the development directory and the local manifest (the "reload local
        plugins" button on the developer page takes this path); the default ``"all"`` reloads
        everything. The two entry points map to ``SkillLoader.reload_dev()`` and
        ``reload_all()``. A single action with a parameter is exposed instead of two actions
        so that no new protocol action is added (existing frontend buttons only need one extra
        field).

        [ZH]
        热重载插件（设计规范 2B.5）：无需重启即可看到 skills_dev/ 中的改动。

        ``scope`` 参数（能力扩展-1 任务四）：``"dev"`` 只重载开发目录与本地清单中的插件
        （开发者页的「重载本地插件」按钮即走这条路径），缺省 ``"all"`` 重载全部。
        两个入口对应 ``SkillLoader.reload_dev()`` 与 ``reload_all()`` —— 后端只暴露一个
        动作、用参数区分，是为了不新增协议动作（前端已存在的按钮只需多带一个字段）。
        """
        scope = str(request.get("scope") or "all").strip().lower()
        try:
            loader = self.skills()
            summary = loader.reload_dev() if scope == "dev" else loader.reload_all()
        except Exception as e:  # noqa: BLE001
            log.exception("重载插件失败")
            return {"type": "system", "content": _t("err.skill_failed", error=e)}
        text = _t("skill.reload_summary", loaded=summary.get("loaded", 0),
                  total=summary.get("total", 0), failed=summary.get("failed", 0))
        if scope == "dev":
            text += "\n" + _t("skill.reload_dev_scope")
        return {"type": "system", "content": text, "skills": self.skills().status(),
                "commands": self.commands.all_commands(), "summary": summary}

    def _handle_skills_dev_mode(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Developer-mode toggle (fix 1: must work in both directions).

        Disabling must perform four steps; missing any one makes the user believe it is still
        enabled:
        1. Record that "the user set this explicitly"; otherwise the dev_mode property turns
           True again automatically while ``skills_dev/`` still contains plugins (this was the
           root cause of "once enabled, it cannot be disabled").
        2. Unload the loaded development plugins and remove their commands from the command
           table.
        3. Stop hot reloading (once ``dev_mode`` is False, the dev directory no longer
           participates in discovery).
        4. Clear the development log.

        [ZH]
        开发者模式开关（修复一：必须双向可用）。

        关闭时需完成四件事，缺一件用户就会认为"未关闭"：
        1. 记录"用户显式设置过"，否则 ``skills_dev/`` 中仍有插件时 dev_mode 属性会自动
           重新变为 True（这是原先"一旦开启就关不掉"的根因）；
        2. 卸载已加载的开发插件，并把它们的指令从指令表移除；
        3. 停止热重载（``dev_mode`` 为 False 后 dev 目录不再参与发现）；
        4. 清理开发日志。
        """
        enabled = bool(request.get("enabled"))
        self.config.marketplace.dev_mode = enabled
        self.config.marketplace.dev_mode_explicit = True
        try:
            self.config.save()
        except Exception:
            log.warning("保存开发者模式开关失败", exc_info=True)
        loader = self.skills()
        loader._dev_mode_cache = None
        unloaded: List[str] = []
        cleared = False
        if enabled:
            # [EN] On enable, rescan so that plugins in skills_dev/ take effect immediately.
            # [ZH] 开启时重新发现，使 skills_dev/ 中的插件立即生效。
            loader.reload_all()
            content = _t("skill.dev_mode_on")
        else:
            unloaded = loader.unload_dev()
            cleared = loader.clear_log()
            content = _t("skill.dev_mode_off")
            if unloaded:
                content += "\n" + _t("skill.dev_mode_unloaded", count=len(unloaded),
                                     ids=", ".join(unloaded))
        commands = self.commands.all_commands()
        return {"type": "system", "content": content,
                "skills": loader.status(), "commands": commands,
                "dev_mode": enabled, "unloaded": unloaded,
                "log_cleared": cleared}

    def _handle_skills_trust_bulk(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Record trust decisions for local plugins in bulk (fix 1: one aggregated confirmation
        instead of one dialog per plugin).

        Request body: ``{"decisions": [{"id": "...", "trusted": true|false}, ...]}``. The
        shorthand ``{"ids": [...], "trusted": true}`` ("enable all / disable all") is also
        accepted.

        [ZH]
        批量记录本地插件的信任选择（修复一：一次汇总确认，不再逐个弹窗）。

        请求体：``{"decisions": [{"id": "...", "trusted": true|false}, ...]}``
        也接受 ``{"ids": [...], "trusted": true}`` 的简写形式（"全部启用/全部禁用"）。
        """
        raw = request.get("decisions")
        decisions: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                skill_id = str(item.get("id") or "").strip()
                if not skill_id:
                    continue
                decisions.append({"id": skill_id, "trusted": bool(item.get("trusted", True))})
        elif isinstance(request.get("ids"), list):
            trusted = bool(request.get("trusted", True))
            decisions = [{"id": str(x), "trusted": trusted}
                         for x in request["ids"] if str(x).strip()]
        if not decisions:
            return {"type": "system", "content": _t("skill.trust_nothing"),
                    "skills": self.skills().status()}

        loader = self.skills()
        for decision in decisions:
            loader.trust_plugin(decision["id"], decision["trusted"])
        # [EN] Denied plugins must be unloaded immediately; newly trusted plugins must be
        # [EN] loaded immediately.
        # [ZH] 被拒绝的插件需立即卸载；新信任的插件需立即加载。
        for decision in decisions:
            if not decision["trusted"]:
                try:
                    loader.unload(decision["id"])
                except Exception:  # noqa: BLE001
                    log.warning("卸载被拒绝的插件 %s 失败", decision["id"], exc_info=True)
        loader.reload_all()
        approved = sum(1 for d in decisions if d["trusted"])
        return {"type": "system",
                "content": _t("skill.trust_saved", approved=approved,
                              denied=len(decisions) - approved),
                "skills": loader.status(), "commands": self.commands.all_commands()}

    def _handle_skills_export_log(self, request: Dict[str, Any]) -> Dict[str, Any]:
        dest = str(request.get("path") or "").strip()
        if not dest:
            dest = os.path.join(os.path.expanduser("~"), "Desktop", "skills_dev.log")
        try:
            path = self.skills().export_log(dest)
        except Exception as e:  # noqa: BLE001
            return {"type": "system", "content": _t("err.skill_failed", error=e)}
        return {"type": "system", "content": _t("skill.log_exported", path=path)}

    def _handle_skills_scaffold(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Create a local plugin skeleton (task 2B.6, new-plugin wizard).

        [ZH]
        新建本地插件骨架（设计规范 2B.6 新建插件向导）。
        """
        result = self.skills().scaffold(
            str(request.get("id") or ""), str(request.get("name") or ""),
            str(request.get("author") or ""), str(request.get("target") or ""),
        )
        if not result.get("ok"):
            return {"type": "system", "content": f"❌ {result.get('message', '')}"}
        summary = self.skills().reload_all()
        return {"type": "system", "content": f"✅ {result.get('message', '')}",
                "scaffold": result, "skills": self.skills().status(),
                "commands": self.commands.all_commands(), "summary": summary}

    def _handle_skills_install_folder(self, request: Dict[str, Any]) -> Dict[str, Any]:
        result = self.skills().install_from_folder(
            str(request.get("path") or ""), dev=bool(request.get("dev")))
        if not result.get("ok"):
            return {"type": "system", "content": f"❌ {result.get('message', '')}"}
        self.skills().reload_all()
        return {"type": "system", "content": f"✅ {result.get('message', '')}",
                "install": result, "skills": self.skills().status(),
                "commands": self.commands.all_commands()}

    def _handle_skills_uninstall(self, request: Dict[str, Any]) -> Dict[str, Any]:
        result = self.skills().uninstall(str(request.get("id") or ""),
                                        delete_files=bool(request.get("delete_files", True)))
        if not result.get("ok"):
            return {"type": "system", "content": f"❌ {result.get('message', '')}"}
        return {"type": "system", "content": f"✅ {result.get('message', '')}",
                "skills": self.skills().status(), "commands": self.commands.all_commands()}

    def _handle_skills_export_zip(self, request: Dict[str, Any]) -> Dict[str, Any]:
        result = self.skills().export_zip(str(request.get("id") or ""), str(request.get("path") or ""))
        if not result.get("ok"):
            return {"type": "system", "content": f"❌ {result.get('message', '')}"}
        return {"type": "system", "content": f"✅ {result.get('message', '')}", "export": result}

    def _handle_skills_publish_check(self, request: Dict[str, Any]) -> Dict[str, Any]:
        result = self.skills().publish_checklist(str(request.get("id") or ""))
        return {"type": "system", "content": "", "publish_check": result}

    def _handle_skills_trust(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Record or revoke trust in a local plugin (task 2B.9, first-load confirmation dialog).

        [ZH]
        记录/撤销对本地插件的信任（设计规范 2B.9 首次加载确认框）。
        """
        skill_id = str(request.get("id") or "")
        trusted = bool(request.get("trusted", True))
        ok = self.skills().trust_plugin(skill_id, trusted)
        if not trusted:
            self.skills().unload(skill_id)
        return {"type": "system", "content": "" if ok else _t("sys.trust_write_failed"),
                "skills": self.skills().status(), "commands": self.commands.all_commands()}

    def _handle_skills_manifest_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Write the local plugin manifest (task 2B.3 / 2B.10, plugin manifest editing).

        [ZH]
        写入本地插件清单（设计规范 2B.3 / 2B.10 插件清单编辑）。
        """
        payload = request.get("manifest")
        if not isinstance(payload, dict):
            return {"type": "system", "content": _t("sys.skill_manifest_invalid")}
        ok = self.skills().save_local_manifest(payload)
        if not ok:
            return {"type": "system", "content": _t("skill.manifest_save_failed",
                                                    path=self.skills().local_manifest_file)}
        self.skills().reload_all()
        return {"type": "system", "content": _t("skill.manifest_saved"),
                "manifest": self.skills().local_manifest(), "skills": self.skills().status(),
                "commands": self.commands.all_commands()}

    def _handle_skills_panel_action(self, request: Dict[str, Any]) -> Dict[str, Any]:
        result = self.skills().panel_action(
            str(request.get("skill_id") or ""), str(request.get("panel_id") or ""),
            str(request.get("panel_action") or ""),
            request.get("values") if isinstance(request.get("values"), dict) else {},
        )
        return {"type": "system", "content": result.get("text") or result.get("message") or "",
                "panel_result": result}

    def _handle_skills_examples(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Copy the bundled example plugins into skills_dev/ (task 2.5, example skills).

        [ZH]
        把随包附带的示例插件复制到 skills_dev/（设计规范 2.5 示例技能）。
        """
        loader = self.skills()
        src_root = loader.examples_dir()
        if not os.path.isdir(src_root):
            return {"type": "system", "content": _t("skill.examples_missing", path=src_root)}
        names = [d for d in sorted(os.listdir(src_root))
                 if os.path.isdir(os.path.join(src_root, d)) and not d.startswith(".")]
        only = str(request.get("id") or "").strip()
        if only:
            names = [n for n in names if n == only]
        copied: List[str] = []
        errors: List[str] = []
        os.makedirs(loader.dev_dir, exist_ok=True)
        for name in names:
            dest = os.path.join(loader.dev_dir, name)
            try:
                if os.path.exists(dest):
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(os.path.join(src_root, name), dest,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                copied.append(name)
            except OSError as e:
                errors.append(f"{name}: {e}")
        if copied:
            loader.reload_all()
        if not copied and errors:
            return {"type": "system", "content": _t("sys.skill_examples_copy_failed",
                                                    error="; ".join(errors))}
        text = _t("skill.examples_copied", names=", ".join(copied), dir=loader.dev_dir)
        if errors:
            text += " ⚠️ " + "; ".join(errors)
        return {"type": "system", "content": text, "copied": copied,
                "skills": loader.status(), "commands": self.commands.all_commands()}

    # [EN] ---------- Auto-update (task 5, option A) ----------
    # [ZH] ---------- 自动更新（相关设计 方案 A） ----------

    def _handle_update_check(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Check GitHub Releases for a newer version; notify only, do not download or replace.

        [ZH]
        检查 GitHub Releases 是否有新版本；只提示，不下载、不替换。
        """
        try:
            from update_check import check_for_update

            result = check_for_update(self.config.update, force=bool(request.get("force")))
        except Exception as e:  # noqa: BLE001
            log.exception("检查更新失败")
            return {"type": "system", "content": _t("update.check_failed", error=e),
                    "update": {"ok": False, "error": str(e)}}
        # [EN] Record the time of this check so GitHub is not queried on every startup
        # [EN] (task 5.2, enterprise-network friendliness).
        # [ZH] 记录本次检查时间，避免每次启动都请求 GitHub（设计规范 5.2 企业环境友好）。
        if result.get("checked_at"):
            self.config.update.last_check = result["checked_at"]
            self.config.update.last_seen_version = result.get("latest") or ""
            try:
                self.config.save()
            except Exception:
                log.debug("保存更新检查时间失败", exc_info=True)
        return {"type": "system", "content": result.get("message", ""), "update": result}

    def _download_update_payload(self, request: Dict[str, Any],
                                 progress=None) -> Dict[str, Any]:
        """
        [EN] Shared download path for the synchronous and streaming actions. It validates
        the tag/asset pair, re-resolves the URL, optionally forwards byte progress, and
        records the tag. Callers turn the result into their own response shape.
        [ZH] 同步与流式动作共用的下载路径：校验标签/附件组合、重新解析 URL、可选转发字节进度，
        并记录标签。调用方把结果包装成各自的响应形态。
        """
        tag = str(request.get("tag") or "").strip()
        asset = str(request.get("asset") or "").strip()
        if not tag or not asset:
            raise ValueError(_t("update.download_missing"))
        from update_check import download_update_asset

        dest = os.path.join(user_data_root(), "updates")
        result = download_update_asset(self.config.update, tag, asset, dest, progress=progress)
        self.config.update.last_seen_version = tag
        try:
            self.config.save()
        except Exception:
            log.debug("保存更新版本失败", exc_info=True)
        return result

    def _handle_update_download(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Download one asset of an already-detected release. The caller supplies only
        the tag and the asset name; ``update_check.download_update_asset`` re-resolves
        the URL from the GitHub API and verifies size and SHA-256 before the file is
        renamed into place. The file lands in ``<user_data_root>/updates/``, never in
        the program directory, so an interrupted download cannot corrupt the running
        installation.

        [ZH]
        下载已检测到的发布中的某个附件。调用方只提供标签与附件名称；
        ``update_check.download_update_asset`` 会重新从 GitHub API 解析 URL，并在文件
        改名落位前校验大小与 SHA-256。文件落在 ``<用户数据根>/updates/``，绝不进入
        程序目录，因此中断的下载不会破坏正在运行的安装。
        """
        try:
            result = self._download_update_payload(request)
        except Exception as e:  # noqa: BLE001 - report, never crash the backend
            log.exception("下载更新失败")
            return {"type": "system", "content": _t("update.download_failed", error=e),
                    "ok": False, "update_download": {"ok": False, "error": str(e)}}
        return {"type": "system", "content": _t("update.downloaded", name=result.get("name", "")),
                "ok": True, "update_download": result}

    def _stream_update_download(self, request: Dict[str, Any]):
        """
        [EN]
        Streaming update download: emit one ``update_progress`` line per throttled
        chunk (downloaded bytes, total bytes, percent) and a final ``done`` line with
        the verified path. Progress is written through the same locked stdout writer
        as every other protocol line, so it can never interleave with another response.
        [ZH]
        流式更新下载：每经过节流的一个块输出一条 ``update_progress`` 行（已下载字节、
        总字节、百分比），最后输出带校验路径的 ``done`` 行。进度通过与其它协议行相同的
        加锁 stdout 写入器发出，因此绝不会与另一个响应交错。
        """
        def on_progress(downloaded: int, total: int) -> None:
            total = max(0, int(total or 0))
            downloaded = max(0, int(downloaded or 0))
            percent = int(round(downloaded * 100.0 / total)) if total > 0 else 0
            _write({
                "type": "update_progress",
                "update_progress": {
                    "downloaded": downloaded,
                    "total": total,
                    "percent": max(0, min(100, percent)),
                },
            })

        try:
            result = self._download_update_payload(request, progress=on_progress)
        except Exception as e:  # noqa: BLE001 - report as a stream error, never crash
            log.exception("下载更新失败")
            yield {"type": "error", "content": _t("update.download_failed", error=e),
                   "update_download": {"ok": False, "error": str(e)}}
            return
        yield {"type": "done",
               "content": _t("update.downloaded", name=result.get("name", "")),
               "update_download": result}

    def _handle_update_ignore(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN] Persist the release version the user chose to ignore. The next startup
        check returns ``status=ignored`` for the same version instead of prompting; a
        newer release still prompts normally.
        [ZH] 持久化用户选择忽略的发布版本。下次启动检查对相同版本返回 ``status=ignored``
        而不再提示；更新的版本仍会正常提示。
        """
        from update_check import parse_version

        version = str(request.get("version") or "").strip()
        if not version or parse_version(version) == (0, 0, 0):
            return {"type": "system", "content": _t("update.ignore_failed"),
                    "ok": False, "ignored_version": ""}
        self.config.update.ignored_version = version
        try:
            self.config.save()
        except Exception as e:  # noqa: BLE001
            log.exception("保存忽略版本失败")
            return {"type": "system", "content": _t("update.ignore_failed") + f"：{e}",
                    "ok": False, "ignored_version": ""}
        return {"type": "system", "content": _t("update.ignore_saved", version=version),
                "ok": True, "ignored_version": version}

    def _handle_update_apply(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Apply a previously downloaded update. Before anything is launched, the small
        user-data files are copied into an isolated backup under
        ``<user_data_root>/updates/``; models are intentionally left in place because
        the installer excludes them. The installer is then started detached with the
        current install root as ``/DIR`` so an upgrade overwrites program files only.

        [ZH]
        应用此前下载的更新。启动任何东西之前，先把小型用户数据文件复制到
        ``<用户数据根>/updates/`` 下的隔离备份中；模型刻意保留在原位，因为安装程序
        已排除它们。随后以当前安装根目录作为 ``/DIR`` 分离启动安装程序，使升级只覆盖
        程序文件。
        """
        raw_path = str(request.get("path") or "").strip()
        tag = str(request.get("tag") or "").strip()
        updates_dir = os.path.abspath(os.path.join(user_data_root(), "updates"))
        path = os.path.abspath(raw_path) if raw_path else ""
        if (not path or not os.path.isfile(path)
                or os.path.normcase(os.path.commonpath([path, updates_dir]))
                != os.path.normcase(updates_dir)):
            return {"type": "system", "content": _t("update.apply_missing"),
                    "ok": False, "update_apply": {"ok": False, "error": "installer not found in updates dir"}}
        if os.path.splitext(path)[1].lower() not in (".exe", ".msi"):
            return {"type": "system", "content": _t("update.apply_unsupported"),
                    "ok": False, "update_apply": {"ok": False, "error": "unsupported installer type"}}
        try:
            from update_check import backup_user_data

            program = os.path.abspath(program_dir())
            install_root = os.path.dirname(program) if os.path.basename(program).lower() == "app" else program
            backup = backup_user_data(program, user_data_root(), tag or "update")
        except Exception as e:  # noqa: BLE001
            log.exception("更新前备份用户数据失败")
            return {"type": "system", "content": _t("update.backup_failed", error=e),
                    "ok": False, "update_apply": {"ok": False, "error": str(e)}}
        try:
            if path.lower().endswith(".msi"):
                args = ["msiexec", "/i", path, "/qn"]
            else:
                # [EN] /SILENT keeps a progress window (the user asked to restart and
                # [EN] should see that something is happening); /DIR pins the install
                # [EN] root to the current one so a portable layout stays portable.
                # [ZH] /SILENT 保留进度窗口（用户已选择重启，应当看到事情正在发生）；
                # [ZH] /DIR 固定为当前安装根，使便携布局保持便携。
                args = [path, "/SILENT", f'/DIR="{install_root}"']
            creationflags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
            subprocess.Popen(  # noqa: S603 - fixed, validated installer path
                args, cwd=install_root, close_fds=True,
                creationflags=creationflags, start_new_session=(os.name != "nt"))
        except Exception as e:  # noqa: BLE001
            log.exception("启动更新安装程序失败")
            return {"type": "system", "content": _t("update.apply_failed", error=e),
                    "ok": False, "update_apply": {"ok": False, "error": str(e)}}
        return {
            "type": "system",
            "content": _t("update.apply_started", path=path),
            "ok": True,
            "update_apply": {
                "ok": True,
                "path": path,
                "install_root": install_root,
                "backup_dir": backup.get("dir", ""),
                "backed_up": backup.get("copied", []),
                "models_left_in_place": backup.get("models", 0),
            },
        }

    # [EN] ---------- Model selector (feature 4) ----------
    # [ZH] ---------- 模型选择栏（新增四） ----------

    def _model_hub_error(self, e: Exception, verb: str) -> Dict[str, Any]:
        log.exception("模型中心%s失败", verb)
        return {"type": "system", "content": _t("err.hub_failed", verb=verb, error=e)}

    def _handle_model_hub_search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Search downloadable GGUF models (mirror and official sources, with automatic
        fallback).

        After the v0.2.1 model-marketplace optimization, **one call fetches all results for the
        keyword** (bounded by a safety limit); filtering, sorting, and pagination are then done
        locally, returning page ``page``. This is the synchronous version; the UI uses the
        streaming version ``model_hub_load`` with progress feedback.

        [ZH]
        搜索可下载的 GGUF 模型（镜像/官方双源，自动回退）。

        v0.2.1 模型市场优化后：**一次调用即取回该关键词的全部结果**
        （受安全上限约束），再在本地做筛选/排序/分页，返回第 ``page`` 页。
        这是同步版本；界面走的是带进度反馈的流式版本 ``model_hub_load``。
        """
        try:
            result = self.model_hub().search(
                keyword=str(request.get("keyword") or ""),
                sort=str(request.get("sort") or "downloads"),
                page=int(request.get("page") or 1),
                limit=int(request.get("limit") or 0),
                page_size=int(request.get("page_size") or 0),
                source=request.get("source"),
                min_downloads=int(request.get("min_downloads") or 0),
                quant=str(request.get("quant") or ""),
                filters=request.get("filters") if isinstance(request.get("filters"), dict) else None,
                refresh=bool(request.get("refresh")),
                resume=bool(request.get("resume")),
            )
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_search"))
        return {"type": "system", "content": result.get("message", ""), "model_hub": result}

    def _handle_model_hub_query(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Apply local filtering/sorting/paging to the **already loaded result set** (no new
        remote request).

        This is the delivery point for tasks 3/4/5/6: the paging control, live local
        filtering, structured filters, and the four sort orders all send only this action, so
        changing a filter does not trigger another network round trip.

        [ZH]
        在**已加载的结果集**上做本地筛选/排序/翻页（不重新请求远端）。

        设计规范任务三/四/五/六的落点：分页控件、本地实时筛选、结构化筛选器、四种排序
        都只发这一个动作，因此不会因为"筛选条件改了一下"就重新发起网络请求。
        """
        try:
            result = self.model_hub().query_session(request)
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_status"))
        return {"type": "system", "content": "", "hub_query": result}

    def _stream_model_hub_load(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Stream the model-marketplace results: report progress while fetching, then hand the
        complete dataset to the UI.

        This must stream because task 2 requires "progress feedback during loading (e.g.
        loading 500 / 4356) to avoid a long blank wait", while fetching 5000 entries requires
        5 page requests and takes tens of seconds. Progress lines use a dedicated type
        (``hub_progress``); like the existing ``summary_progress`` they are consumed by the
        frontend onEvent callback and do not disturb the existing delta/done semantics.

        [ZH]
        流式加载模型市场结果：边取边报进度，最后把完整数据集交给界面。

        必须是流式的：任务二要求"加载过程需有进度反馈（如正在加载 500 / 4356），
        避免用户长时间等待"，而一次取回 5000 条需要 5 页请求、耗时数十秒。
        进度行使用独立 type（``hub_progress``），与既有的 ``summary_progress`` 一样，
        由前端 onEvent 回调消费，不干扰 delta/done 的既有语义。
        """
        hub = self.model_hub()
        state: Dict[str, Any] = {}

        def on_progress(event: Dict[str, Any]) -> None:
            state["last"] = event
            _write({"type": "hub_progress", "hub_progress": event,
                    "content": _t("hub.load.progress", loaded=event.get("loaded", 0),
                                  pages=event.get("pages", 0),
                                  elapsed=event.get("elapsed", 0))})

        try:
            dataset = hub.load_all(
                keyword=str(request.get("keyword") or ""),
                sort=str(request.get("sort") or "downloads"),
                source=request.get("source"),
                resume=bool(request.get("resume")),
                refresh=bool(request.get("refresh")),
                on_progress=on_progress,
                should_stop=lambda: self.stop_event.is_set(),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("模型市场加载失败")
            dataset = hub.offline_dataset(error=e, keyword=str(request.get("keyword") or ""),
                                          sort=str(request.get("sort") or "downloads"),
                                          source=request.get("source"))
        if not bool(request.get("no_page")):
            dataset["query"] = hub.query_dataset(
                dataset,
                page=request.get("page") or 1,
                page_size=int(request.get("page_size") or 0) or None,
                filters=request.get("filters") if isinstance(request.get("filters"), dict) else None,
            )
        message = str(dataset.get("message") or "")
        # [EN] The result set uses its own type: the frontend's SendStreamRequestAsync only
        # [EN] returns content on done, so structured data must occupy its own message (the
        # [EN] same approach as file_stream).
        # [ZH] 结果集走独立 type：前端 SendStreamRequestAsync 的 done 只返回 content，
        # [ZH] 结构化数据必须单独占用一条消息（与 file_stream 的做法一致）。
        yield {"type": "hub_result", "model_hub": dataset}
        yield {"type": "done", "content": message, "model_hub": dataset}

    def _handle_model_hub_detail(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Model details: all available GGUF quantization files and their sizes.

        [ZH]
        模型详情：所有可用的 GGUF 量化文件与大小。
        """
        try:
            detail = self.model_hub().detail(str(request.get("repo") or ""), request.get("source"))
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_detail"))
        return {"type": "system", "content": "", "detail": detail}

    def _handle_model_hub_registry(self) -> Dict[str, Any]:
        """
        [EN]
        Model library listing (including the marker for the model currently in use).

        "Currently in use" is taken from the model **actually loaded in the engine**, not from
        the name written in the configuration: in the no-model-ready state the configuration
        still holds a default file name although that file does not exist and was never
        loaded, so marking by configuration would display a false "in use" state (task 1:
        status must reflect reality).

        [ZH]
        模型库列表（含当前正在使用的模型标记）。

        "当前使用中"取自**引擎中实际加载的**模型，而不是配置中写的名称：
        无模型就绪状态下配置里仍有默认文件名，但该文件并不存在、也未被加载，
        按配置标记会显示出"正在使用中"的假象（任务一：状态必须反映真实情况）。
        """
        current = str(getattr(self.llm, "model_path", "") or "")
        try:
            data = self.model_hub().registry(current_model=current)
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_registry"))
        data["model_ready"] = self.model_ready
        return {"type": "system", "content": "", "registry": data}

    def _handle_model_hub_uninstall(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Delete a model file (already confirmed by the frontend).

        [ZH]
        删除模型文件（前端已确认）。
        """
        try:
            result = self.model_hub().uninstall(
                str(request.get("name") or ""), current_model=self.config.model_path
            )
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_uninstall_model"))
        return {"type": "system", "content": f"✅ {result.get('message', '')}"}

    def _handle_model_hub_status(self) -> Dict[str, Any]:
        """
        [EN]
        Model hub status (for the status bar and settings window to show the current data
        source).

        [ZH]
        模型中心状态（供状态栏与设置窗口显示当前数据源）。
        """
        try:
            state = self.model_hub().status()
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_status"))
        return {"type": "system", "content": "", "model_hub_status": state}

    def _handle_model_hub_config_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Save the model selector configuration (data source / token / model library directory).

        [ZH]
        保存模型选择栏配置（数据源 / Token / 模型库目录）。
        """
        patch = request.get("model_hub") or {}
        if not isinstance(patch, dict):
            return {"type": "system", "content": _t("sys.model_hub_must_be_object")}
        changed: List[str] = []
        for key in ("enabled", "source", "hf_token", "models_dir", "registry_file", "verify_sha256"):
            if key in patch:
                setattr(self.config.model_hub, key, patch[key])
                changed.append(key)
        try:
            self.config.save()
        except Exception as e:
            return {"type": "system", "content": _t("sys.config_save_failed", error=e)}
        # [EN] Rebuild the instance after data-source or similar changes so stale settings
        # [EN] are not cached.
        # [ZH] 数据源等信息变化后重建实例，避免缓存旧设置。
        self._model_hub = None
        if changed:
            note = _t("sys.model_hub_saved", fields=_t("sys.list_sep").join(changed))
        else:
            note = _t("sys.model_hub_no_change")
        return {"type": "system", "content": note, "model_hub": self.model_hub().status()}

    # [EN] ---------- No-model-ready state, rescan, and hot loading (general optimization
    # [EN] in an earlier release, design section V, tasks 2/5) ----------
    # [ZH] ---------- 无模型就绪 / 重新扫描 / 热加载（此前版本 综合优化 相关设计、五） ----------

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

    def _market_error(self, e: Exception, verb: str) -> Dict[str, Any]:
        log.exception("技能市场%s失败", verb)
        return {"type": "system", "content": _t("err.market_failed", verb=verb, error=e)}

    def _handle_market_search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Search skills: keyword + sort order + page number (a cached snapshot is returned
        directly on a cache hit).

        [ZH]
        搜索技能：关键词 + 排序 + 页码（缓存命中时直接返回快照）。
        """
        try:
            keyword = str(request.get("keyword") or "").strip()
            sort = str(request.get("sort") or "best").strip().lower()
            if sort not in ("best", "stars", "updated"):
                sort = "best"
            try:
                page = max(1, int(request.get("page") or 1))
            except (TypeError, ValueError):
                page = 1
            result = self.market().search(keyword, sort, page)
        except Exception as e:
            return self._market_error(e, _t("sys.verb_search"))
        return {"type": "system", "content": result.get("message", ""), "market": result}

    def _handle_market_detail(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Skill details: full skill.json, README excerpt, version, dependencies, and entry files.

        [ZH]
        技能详情：完整 skill.json、README 摘要、版本、依赖与入口文件。
        """
        repo = str(request.get("repo") or "").strip()
        if not repo:
            return {"type": "system", "content": _t("sys.market_missing_repo")}
        try:
            detail = self.market().detail(repo)
        except Exception as e:
            return self._market_error(e, _t("sys.verb_get_detail"))
        return {"type": "system", "content": "", "detail": detail}

    def _handle_market_install(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Install a skill (the user has confirmed in the frontend) and **hot-reload
        immediately after installation**.

        Capability extension-1, task 4: the audit found that "install and it works" was
        missing its last step -- the old implementation only returned a message, and the user
        had to click "reload local plugins" or restart, otherwise the new command did not
        appear in the command palette (this is the concrete manifestation of the task
        statement's "runtime loader wiring is not complete"). Success now calls
        ``skills().reload_all()`` directly and returns the load result to the UI.

        [ZH]
        安装技能（用户已在前端确认），**安装完成后立即热重载**。

        能力扩展-1 任务四：审计发现"安装后即可使用"缺少最后一步 —— 旧实现只返回一句文案，
        用户必须自行点击「重载本地插件」或重启，否则指令面板中看不到新指令
        （这正是设计规范所述"运行期加载器接线尚未完成"的实际表现）。
        现在安装成功后直接调用 ``skills().reload_all()``，并把加载结果一并返回界面。
        """
        repo = str(request.get("repo") or "").strip()
        if not repo:
            return {"type": "system", "content": _t("sys.market_missing_repo")}
        try:
            result = self.market().install(repo)
        except Exception as e:
            return self._market_error(e, _t("sys.verb_install"))
        ok = bool(result.get("ok"))
        icon = "✅" if ok else "❌"
        response: Dict[str, Any] = {"type": "system",
                                    "content": f"{icon} {result.get('message', '')}",
                                    "market_install": result}
        if ok:
            response["content"] += "\n" + self._reload_skills_note()
            response["model_skills"] = self.skills().status()
        return response

    def _reload_skills_note(self) -> str:
        """
        [EN]
        Hot-reload plugins and describe the result clearly (reused after install/uninstall).

        [ZH]
        热重载插件并把结果说明清楚（供安装/卸载后复用）。
        """
        try:
            summary = self.skills().reload_all()
        # [EN] A reload failure must not mask a successful install.
        # [ZH] 重载失败不得掩盖安装成功。
        except Exception as e:
            log.exception("安装后热重载插件失败")
            return _t("sys.skills_reload_failed", error=e)
        return _t("sys.skills_reloaded", loaded=summary.get("loaded", 0),
                  total=summary.get("total", 0), failed=summary.get("failed", 0))

    def _handle_market_uninstall(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Uninstall a skill: remove the registry entry and delete the skill directory, then
        read back to confirm (and hot-reload immediately as well).

        [ZH]
        卸载技能：移除注册表条目并删除技能目录，再回读确认（同样立即热重载）。
        """
        skill_id = str(request.get("id") or request.get("repo") or "").strip()
        if not skill_id:
            return {"type": "system", "content": _t("sys.market_missing_skill_id")}
        try:
            result = self.market().uninstall(skill_id)
        except Exception as e:
            return self._market_error(e, _t("sys.verb_uninstall"))
        ok = bool(result.get("ok"))
        icon = "✅" if ok else "❌"
        response: Dict[str, Any] = {"type": "system",
                                    "content": f"{icon} {result.get('message', '')}",
                                    "market_uninstall": result}
        if ok:
            # [EN] A reload is mandatory after uninstall: otherwise the deleted plugin's
            # [EN] commands remain in the command table and fail only when invoked.
            # [ZH] 卸载后必须重载：否则被删插件的指令仍留在指令表中，点击后才报错。
            response["content"] += "\n" + self._reload_skills_note()
            response["model_skills"] = self.skills().status()
        return response

    def _handle_market_installed(self) -> Dict[str, Any]:
        try:
            items = self.market().installed()
        except Exception as e:
            return self._market_error(e, _t("sys.verb_read_installed"))
        return {"type": "system", "content": "", "installed": items}

    def _handle_market_status(self) -> Dict[str, Any]:
        """
        [EN]
        Marketplace connection status (for the main window status bar).

        [ZH]
        市场连接状态（供主窗口状态栏显示）。
        """
        try:
            state = self.market().status()
        except Exception as e:
            return self._market_error(e, _t("sys.verb_query_status"))
        return {"type": "system", "content": "", "market_status": state}

    # [EN] ---------- Automation configuration and testing (task 4) ----------
    # [ZH] ---------- 自动化配置与测试（相关设计） ----------

    @staticmethod
    def _automation_payload(cfg) -> Dict[str, Any]:
        """
        [EN]
        Convert the automation configuration into the flat structure required by the frontend
        form.

        [ZH]
        把自动化配置转换为前端表单所需的扁平结构。
        """
        return cfg.model_dump()

    def _automation_login_payload(self) -> Dict[str, Any]:
        """
        [EN]
        Current login-state information (v0.2.1 automation optimization, task 1): returns only
        paths/sizes/timestamps, never cookie contents.

        [ZH]
        登录态现状（v0.2.1 自动化优化 任务一）：只返回路径/大小/时间，不含 Cookie 内容。
        """
        try:
            return self.automation().login_state_status()
        except Exception as exc:  # noqa: BLE001
            return {"exists": False, "profile_dir": "", "size_bytes": 0, "modified": "",
                    "legacy_state_file": "", "legacy_exists": False,
                    "login_check_selector": "", "url_keywords": [], "runnable": True,
                    "error": str(exc)[:200]}

    def _handle_automation_login_state(self, _request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Read the automation login-state information (shared by the settings window and the
        "clear login state" button).

        [ZH]
        读取自动化登录态信息（设置窗口与"清除登录态"按钮共用）。
        """
        return {
            "type": "system",
            "content": "",
            "login_state": self._automation_login_payload(),
        }

    def _handle_automation_clear_login(self, _request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Clear the automation login state (delete the persisted data directory); the next run
        guides the user through login again.

        [ZH]
        清除自动化登录态（删除持久化数据目录），下次运行重新引导登录。
        """
        if self.automation().running:
            return {"type": "system", "content": _t("sys.automation_clear_login_running"),
                    "login_state": self._automation_login_payload()}
        result = self.automation().clear_login_state()
        if result.get("ok"):
            content = _t("sys.automation_clear_login_done",
                         files=int(result.get("removed_files") or 0),
                         path=str(result.get("profile_dir") or ""))
            if result.get("legacy_removed"):
                content += "\n" + _t("sys.automation_clear_login_legacy")
        else:
            content = _t("sys.automation_clear_login_failed",
                         error=str(result.get("error") or _t("automation.value.unknown_reason")))
        return {"type": "system", "content": content,
                "login_state": self._automation_login_payload(),
                "cleared": bool(result.get("ok"))}

    def _handle_automation_config(self, _request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Read the automation configuration (called when the settings window opens).

        [ZH]
        读取自动化配置（设置窗口打开时调用）。
        """
        return {
            "type": "system",
            "content": _t("sys.automation_config_title"),
            "automation": self._automation_payload(self.config.automation),
        }

    def _handle_automation_config_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Update the automation configuration and write it back to the config file.

        Safety agreement: when accepted (safety_accepted=True), the agreement version and
        consent time are recorded; when not accepted, the AutomationConfig validator forces
        enabled to False.

        [ZH]
        更新自动化配置并写回配置文件。

        安全协议：勾选（safety_accepted=True）时记录协议版本与同意时间；
        未勾选时 AutomationConfig 校验器会强制把 enabled 置为 False。
        """
        import datetime

        patch = request.get("automation") or {}
        if not isinstance(patch, dict):
            return {"type": "system", "content": _t("sys.automation_bad_format")}
        data = self.config.automation.model_dump()
        data.update({k: v for k, v in patch.items() if k in data})
        # [EN] When the safety agreement is accepted, record version and time; when it is
        # [EN] unchecked, clear the consent time and disable the switch.
        # [ZH] 勾选安全协议时记录版本与时间；取消勾选则清空同意时间并关闭开关。
        if patch.get("safety_accepted") and not self.config.automation.safety_accepted:
            data["safety_accepted_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            data["safety_version"] = data.get("safety_version") or "1.0"
        elif patch.get("safety_accepted") is False:
            data["safety_accepted_at"] = ""
            data["enabled"] = False
        try:
            self.config.automation = AutomationConfig(**data)
        except Exception as e:
            return {"type": "system", "content": _t("sys.automation_config_invalid", error=e)}
        self.config.save()
        cfg = self.config.automation
        note = ""
        if cfg.enabled and cfg.safety_accepted:
            note = _t("sys.automation_safety_note", safety_version=cfg.safety_version,
                      accepted_at=cfg.safety_accepted_at)
        return {
            "type": "system",
            "content": _t("sys.automation_config_saved", note=note),
            "automation": self._automation_payload(cfg),
        }

    # [EN] ---------- Automation configuration wizard, templates, import and export
    # [EN] (task 4 in an earlier release) ----------
    # [ZH] ---------- 自动化配置向导、模板与导入导出（此前版本 相关设计） ----------

    #: [EN] Fields that must appear in "field help" (order equals UI order).
    #: [ZH] 需要出现在「填写帮助」中的字段（顺序即界面顺序）。
    AUTOMATION_FIELDS = (
        ("task_list_url", "step1", True),
        ("task_list_selector", "step1", True),
        ("task_row_selector", "step1", False),
        ("next_page_selector", "step1", False),
        ("task_detail_file_selector", "step2", True),
        ("file_extensions", "step2", True),
        ("instruction", "step3", True),
        ("task_name", "step4", False),
        ("results_root_name", "step4", False),
        ("results_location", "step4", False),
        # [EN] Login state (automation optimization in an earlier release, task 1): grouped
        # [EN] under step 5 "run parameters" because these are all "how to run" settings and
        # [EN] most users do not need to change them (the defaults suffice).
        # [ZH] 登录态（此前版本 自动化优化 相关设计）：归入第 5 步「运行参数」，
        # [ZH] 因为它们都是"如何运行"的设置，且大多数用户不需要修改（默认值即可）。
        ("login_state_dir", "step5", False),
        ("login_check_selector", "step5", False),
        ("login_url_keywords", "step5", False),
        ("max_tasks", "step5", False),
        ("page_timeout_ms", "step5", False),
        ("retries", "step5", False),
        ("dedupe", "step5", False),
        ("manual_login_wait_seconds", "step5", False),
        ("llm_temperature", "step5", False),
        ("llm_max_tokens", "step5", False),
        ("save_results", "step5", False),
        ("wait_network_idle", "step5", False),
        ("audit_log_file", "step5", False),
        ("download_dir", "step5", False),
        # [EN] Task 16 (self-test fix-1): browser selection is grouped under "run
        # [EN] parameters". It is placed before browser_channel because the UI and the
        # [EN] documentation treat the new field as authoritative; the old field exists only to
        # [EN] preserve compatibility with older configurations.
        # [ZH] 相关设计（自测问题修复-1）：浏览器选择归入「运行参数」。
        # [ZH] 放在 browser_channel 之前：界面与文档均以新字段为准，
        # [ZH] 旧字段仅用于不破坏老配置。
        ("browser", "step5", False),
        ("browser_channel", "step5", False),
        ("schedule_enabled", "step5", False),
        ("schedule_times", "step5", False),
        ("start_with_windows", "step5", False),
        ("safety_accepted", "step5", True),
        ("enabled", "step5", False),
    )

    #: [EN] Preset templates (task 4.4); the content comes from the language pack and can be
    #: multilingual.
    #: [ZH] 预置模板（设计规范 4.4）；内容取自语言包，可多语言。
    AUTOMATION_TEMPLATES = ("contract", "invoice", "report", "table")

    @staticmethod
    def _automation_field_help() -> List[Dict[str, Any]]:
        """
        [EN]
        Field-level help (task 4.3): label, plain-language description, example, whether it is
        required, and the step it belongs to.

        [ZH]
        字段级帮助（设计规范 4.3）：标签、通俗说明、示例、是否必填、所在步骤。
        """
        items: List[Dict[str, Any]] = []
        for name, step, required in Backend.AUTOMATION_FIELDS:
            key = f"automation.help.{name}"
            items.append({
                "field": name,
                "step": step,
                "step_title": _t(f"automation.help.{step}_title"),
                "required": bool(required),
                "label": _t(f"{key}.label"),
                "help": _t(f"{key}.help"),
                "example": _t(f"{key}.example"),
            })
        return items

    @staticmethod
    def _automation_templates() -> List[Dict[str, Any]]:
        """
        [EN]
        Preset templates (task 4.4) plus user-defined templates (stored next to
        config.user.json).

        [ZH]
        预置模板（设计规范 4.4）+ 用户自定义模板（存放在 config.user.json 旁）。
        """
        items: List[Dict[str, Any]] = []
        for name in Backend.AUTOMATION_TEMPLATES:
            items.append({
                "id": name,
                "label": _t(f"automation.template.{name}.label"),
                "instruction": _t(f"automation.template.{name}.instruction"),
                "extensions": _t(f"automation.template.{name}.extensions"),
                "builtin": True,
            })
        for name, values in Backend._load_custom_templates().items():
            items.append({
                "id": name,
                "label": values.get("label") or name,
                "instruction": values.get("instruction") or "",
                "extensions": values.get("extensions") or "",
                "builtin": False,
            })
        return items

    @staticmethod
    def _custom_template_path() -> str:
        return os.path.join(os.getcwd(), "automation_templates.json")

    @staticmethod
    def _load_custom_templates() -> Dict[str, Dict[str, str]]:
        try:
            with open(Backend._custom_template_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for name, values in (data.get("templates") or {}).items():
            if isinstance(values, dict):
                out[str(name)] = {k: str(v) for k, v in values.items()}
        return out

    def _handle_automation_save_template(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Save the current configuration as a custom template (task 4.4, last sentence).

        [ZH]
        把当前配置保存为自定义模板（设计规范 4.4 末句）。
        """
        name = str(request.get("name") or "").strip()
        if not name:
            return {"type": "system", "content": _t("sys.automation_template_need_name")}
        values = request.get("template")
        if not isinstance(values, dict):
            return {"type": "system", "content": _t("sys.automation_template_bad_content")}
        data = {"schema": 1, "templates": self._load_custom_templates()}
        data["templates"][name] = {
            "label": str(values.get("label") or name),
            "instruction": str(values.get("instruction") or ""),
            "extensions": str(values.get("extensions") or ""),
        }
        try:
            with open(self._custom_template_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            return {"type": "system", "content": _t("sys.automation_template_save_failed", error=e)}
        return {"type": "system", "content": _t("automation.template.custom_saved", name=name),
                "templates": self._automation_templates()}

    def _handle_automation_wizard_step(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        One step of the selector-testing wizard (task 4.5): steps 1 to 4.

        Step 4 (dry run) takes several minutes and uses the streaming channel
        ``automation_wizard``; this handler covers the single synchronous checks of steps 1 to
        3.

        [ZH]
        测试选择器向导的单步（设计规范 4.5）：第 1~4 步。

        第 4 步（试运行）需要数分钟，走流式通道 ``automation_wizard``；
        这里处理 1~3 步这类"单次同步"检查。
        """
        try:
            step = int(request.get("step") or 1)
        except (TypeError, ValueError):
            step = 1
        if step == 4:
            return {"type": "system", "content": _t("sys.automation_wizard_step4_stream")}
        try:
            result = self.automation().wizard_step(step)
        except Exception as e:  # noqa: BLE001
            log.exception("测试向导第 %s 步失败", step)
            return {"type": "system", "content": _t("sys.automation_wizard_step_failed",
                                                    step=step, error=e)}
        return {"type": "system", "content": result.get("detail", ""), "wizard": result}

    def _exportable_automation(self) -> Dict[str, Any]:
        """
        [EN]
        Export the automation configuration (task 4.7): excludes sensitive information such as
        login state and tokens.

        [ZH]
        导出自动化配置（设计规范 4.7）：排除登录态、Token 等敏感信息。
        """
        data = self.config.automation.model_dump()
        # [EN] The login-state file contains session cookies and is never exported; the path
        # [EN] itself is excluded too, to prevent overwriting on another machine.
        # [ZH] 登录态文件内容是会话 Cookie，绝不导出；路径本身也不导出，避免跨机覆盖。
        data.pop("login_state_file", None)
        data.pop("processed_file", None)
        data.pop("download_dir", None)
        return data

    def _handle_automation_config_export(self, request: Dict[str, Any]) -> Dict[str, Any]:
        dest = str(request.get("path") or "").strip()
        if not dest:
            dest = os.path.join(os.path.expanduser("~"), "Desktop", "echodesk-automation.echodesk-auto")
        if not dest.lower().endswith(".echodesk-auto"):
            dest += ".echodesk-auto"
        payload = {
            "format": "echodesk-automation",
            "schema": 1,
            "app_version": APP_VERSION,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "language": _i18n().locale,
            "note": _t("sys.automation_export_note"),
            "automation": self._exportable_automation(),
        }
        try:
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            return {"type": "system", "content": _t("sys.automation_export_failed", error=e)}
        return {"type": "system", "content": _t("sys.automation_export_done", path=dest), "path": dest}

    def _handle_automation_config_import(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Import the automation configuration (task 4.7).

        When the imported language differs from the current UI language, a notice is shown,
        but the import is **not blocked**.

        [ZH]
        导入自动化配置（设计规范 4.7）。

        导入的语言与当前界面语言不同时给出提示，但**不阻断**导入。
        """
        path = str(request.get("path") or "").strip()
        if not path or not os.path.isfile(path):
            return {"type": "system", "content": _t("sys.automation_import_no_file", path=path)}
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:  # noqa: BLE001
            return {"type": "system", "content": _t("sys.automation_import_read_failed", error=e)}
        if not isinstance(payload, dict) or payload.get("format") != "echodesk-automation":
            return {"type": "system", "content": _t("sys.automation_import_bad_format")}
        incoming = payload.get("automation")
        if not isinstance(incoming, dict):
            return {"type": "system", "content": _t("sys.automation_import_no_section")}

        # [EN] The safety agreement and the enable switch are not inherited from the file: they
        # [EN] must be accepted by the local user in person (secure by default).
        # [ZH] 安全协议与开关不从文件中继承：必须由本机用户亲自勾选（安全默认）。
        incoming.pop("safety_accepted", None)
        incoming.pop("safety_accepted_at", None)
        incoming.pop("enabled", None)

        data = self.config.automation.model_dump()
        applied = {k: v for k, v in incoming.items() if k in data}
        data.update(applied)
        data["safety_accepted"] = False
        data["enabled"] = False
        try:
            self.config.automation = AutomationConfig(**data)
        except Exception as e:  # noqa: BLE001
            return {"type": "system", "content": _t("sys.automation_import_invalid", error=e)}
        self.config.save()

        source_lang = str(payload.get("language") or "")
        note = ""
        if source_lang and source_lang != _i18n().locale:
            note = (_t("automation.import_lang_notice", source=source_lang, current=_i18n().locale))
        return {
            "type": "system",
            "content": (_t("automation.import_ok", count=len(applied))
                        + ("\n" + note if note else "")
                        + "\n" + _t("automation.import_safety_reset")),
            "automation": self._automation_payload(self.config.automation),
            "imported_fields": sorted(applied),
        }

    def _handle_automation_test(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Test a selector: open a browser at the given URL and preview the selector's matches.

        [ZH]
        测试选择器：打开浏览器访问指定网址，预览选择器匹配结果。
        """
        url = str(request.get("url") or self.config.automation.task_list_url or "").strip()
        selector = str(request.get("selector") or self.config.automation.task_list_selector or "").strip()
        if not url or not selector:
            return {"type": "system", "content": _t("sys.automation_test_need_fields")}
        try:
            result = self.automation().test_selector(url, selector)
        except Exception as e:
            log.exception("测试选择器失败")
            return {"type": "system", "content": _t("sys.automation_test_failed", error=e)}
        if not result.get("ok"):
            return {"type": "system", "content": _t(
                "sys.automation_test_error",
                error=result.get("error") or _t("sys.automation_test_unmatched"))}

        lines = [_t("sys.automation_selector_ok", count=result["count"],
                    kind=result.get("kind", "css"))]
        for index, sample in enumerate(result.get("samples", []), 1):
            href = ""
            hrefs = result.get("hrefs") or []
            if index - 1 < len(hrefs) and hrefs[index - 1]:
                href = f" → {hrefs[index - 1]}"
            lines.append(f"  {index}. {sample}{href}")
        if result["count"] > len(result.get("samples", [])):
            lines.append(_t("sys.automation_preview_only",
                            count=len(result.get("samples", []))))
        return {"type": "system", "content": "\n".join(lines)}

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
        """
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
            # [EN] Report the hardware ceiling rather than the currently allocated
            # [EN] window: the engine grows on demand up to that ceiling, so the
            # [EN] front-end progress bar must be scaled against it; otherwise a
            # [EN] modest history would display as over 100% before compaction runs.
            # [ZH] 上报硬件上限而非当前已分配的窗口：引擎按需扩展至该上限，因此
            # [ZH] 前端进度条必须以其为分母；否则一段不长的历史在压缩触发之前就会
            # [ZH] 显示为超过 100%。
            effective_ctx = int(getattr(self.llm, "max_n_ctx", 0) or self.llm.n_ctx or 0)
            response["limit"] = int(effective_ctx * 0.8)
            response["n_ctx"] = effective_ctx
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
                response["search"] = bool(self.commands.search_enabled)
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
                response["temperature"] = float(self.commands.temperature)
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

    def mcp_control(self, action: str = "", name: str = "") -> Dict[str, Any]:
        """
        [EN]
        Unified entry point for ``/mcp``: read state, enable/disable a server, or force a
        connection.

        Enabling/disabling is written to ``config.json`` (a single user press persists) and
        **rebuilds the tool table immediately**, satisfying the task's "enable/disable at
        runtime without restart".

        [ZH]
        ``/mcp`` 的统一入口：读取状态、启停某个 server、或强制连接。

        启停会写入 ``config.json``（用户按一次即长期生效）并**立即重建工具表**，
        满足设计规范"运行时启停，无需重启"。
        """
        cfg = getattr(self.config, "mcp", None)
        if cfg is None:
            return {}
        if not action:
            return self.mcp_diagnostics()
        if action in ("enable", "disable"):
            target = None
            for server in (cfg.servers or []):
                if str(getattr(server, "name", "")).strip() == str(name).strip():
                    target = server
                    break
            if target is None:
                return {"ok": False, "state": self.mcp_diagnostics()}
            target.enabled = (action == "enable")
            self.config.save()
            # [EN] A disabled server must actually have its process terminated; an enabled one
            # [EN] connects immediately only if auto_connect says so.
            # [ZH] 被关闭的 server 必须真正终止进程；被开启的按 auto_connect 决定是否立即连接。
            if self._mcp_bridge is not None:
                self._mcp_bridge.close()
            self._mcp_bridge = None
            self._agent_registry = None
            if action == "enable" and bool(getattr(cfg, "auto_connect", False)):
                self.refresh_agent_tools(discover_mcp=True)
            else:
                self.refresh_agent_tools(discover_mcp=False)
            return {"ok": True, "state": self.mcp_diagnostics()}
        if action == "connect":
            summary = self.refresh_agent_tools(discover_mcp=True)
            return {"ok": bool(summary.get("registered")),
                    "registered": summary.get("registered", 0),
                    "state": self.mcp_diagnostics()}
        return {"ok": False, "state": self.mcp_diagnostics()}

    def _handle_agent_config(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Agent configuration writes from the settings page or commands (tool authorization, step
        limit).

        It uses the same ``configure_agent`` as the ``/agent`` command: the UI and the command
        **must not have two separate permission implementations**, otherwise inconsistencies
        such as "the UI shows enabled but it is not actually enabled" are inevitable.

        [ZH]
        设置页/指令的 Agent 配置写入（工具授权、步数上限）。

        与 ``/agent`` 指令共用同一个 ``configure_agent`` —— 界面与指令**不能各有一套
        权限逻辑**，否则必然出现"界面显示已启用、实际未启用"这类不一致。
        """
        if "steps" in request:
            result = self.configure_agent("__steps__", request.get("steps"))
        elif request.get("tool"):
            result = self.configure_agent(str(request.get("tool")),
                                          bool(request.get("allowed", True)))
        else:
            result = self.configure_agent()
        return {"type": "system", "content": "", "agent_config": result,
                "agent": self.agent_diagnostics()}

    def _handle_mcp_apply(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Apply the MCP configuration (enable/disable servers, change commands and timeouts) with
        **immediate effect at runtime**.

        The task requires "users can enable/disable an MCP server in settings without a
        restart": ``refresh_agent_tools`` rebuilds the tool registry and (when enabled) starts
        the newly configured server processes and attaches their tools to the Agent; a disabled
        server is ``close()``-ed and its tools disappear from the list immediately.

        [ZH]
        应用 MCP 配置（启用/禁用 server、修改命令与超时），并**在运行时立即生效**。

        设计规范要求"用户可在设置中启用/禁用某个 MCP Server，无需重启"：
        ``refresh_agent_tools`` 会重建工具注册表，并在启用时拉起新配置的 server
        进程、把工具挂入 Agent；被停用的 server 会被 ``close()``，
        其工具立即从清单中消失。
        """
        cfg = getattr(self.config, "mcp", None)
        if cfg is None:
            return {"type": "system", "content": _t("sys.mcp_unavailable")}
        if "enabled" in request:
            cfg.enabled = bool(request.get("enabled"))
        if "auto_connect" in request:
            cfg.auto_connect = bool(request.get("auto_connect"))
        if request.get("timeout_seconds") is not None:
            try:
                cfg.timeout_seconds = max(1.0, min(600.0, float(request["timeout_seconds"])))
            except (TypeError, ValueError):
                return {"type": "system", "content": _t("sys.mcp_bad_timeout")}
        servers = request.get("servers")
        if isinstance(servers, list):
            from config import McpServerConfig
            from mcp_client import McpBridge

            # [EN] Merge with the existing configuration by name: entries sent back by the UI
            # [EN] may omit env / passthrough_env (older frontends do not return them). Without
            # [EN] merging, "change a timeout and save" would erase environment variables the
            # [EN] user wrote by hand in config.json -- silent data loss that must be prevented
            # [EN] at the source.
            # [ZH] 按 name 与既有配置合并：界面回传的条目可能省略 env / passthrough_env
            # [ZH] （老版本前端不回传它们）。不合并的话，"改个超时再保存"会抹掉用户手写在
            # [ZH] config.json 中的环境变量 —— 属于静默数据丢失，必须从根源上阻止。
            existing = {str(getattr(item, "name", "")): item for item in (cfg.servers or [])}
            cleaned = []
            for item in servers:
                if not isinstance(item, dict):
                    continue
                parsed = McpBridge.server_from_dict(item)
                if parsed is None:
                    return {"type": "system",
                            "content": _t("sys.mcp_bad_server",
                                          item=str(item.get("name") or item.get("command") or ""))}
                old = existing.get(parsed.name)
                if old is not None:
                    if "env" not in item:
                        parsed.env = dict(getattr(old, "env", {}) or {})
                    if "passthrough_env" not in item:
                        parsed.passthrough_env = list(getattr(old, "passthrough_env", []) or [])
                # [EN] Store the **model object** rather than a dict back into the config:
                # [EN] pydantic warns at serialization time about a declared type receiving a
                # [EN] dict, and the emitted JSON may disagree with the declaration.
                # [ZH] 使用**模型对象**而不是 dict 存回配置：pydantic 对"声明了类型却塞入
                # [ZH] dict"会给出序列化警告，写出的 JSON 也可能与声明不一致。
                cleaned.append(McpServerConfig(
                    name=parsed.name, command=parsed.command, args=list(parsed.args),
                    env=dict(parsed.env), enabled=bool(parsed.enabled),
                    passthrough_env=list(parsed.passthrough_env),
                ))
            cfg.servers = cleaned
        self.config.save()
        # [EN] Configuration changed -> rebuild the bridge and the tool table; a process is
        # [EN] actually started only when "enabled + connect required".
        # [ZH] 配置已变化 → 重建桥接器与工具表；仅当"已启用 + 要求连接"时才真正拉起进程。
        if self._mcp_bridge is not None:
            self._mcp_bridge.close()
        self._mcp_bridge = None
        self._agent_registry = None
        should_connect = bool(cfg.enabled) and bool(cfg.auto_connect) and bool(cfg.servers)
        summary = self.refresh_agent_tools(discover_mcp=should_connect)
        return {"type": "system", "content": "",
                "mcp_apply": {"ok": True, "registered": summary.get("registered", 0),
                              "connected": should_connect},
                "mcp": self.mcp_diagnostics(), "agent": self.agent_diagnostics()}

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
            n_threads_override=int(self.config.n_threads or 0),
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
            "threads": int(self.config.n_threads or 0),
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
            self.commands.temperature = cfg.temperature
            self.commands.temp_manual = False
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
                if int(cfg.n_batch or 0) != int(preset_values.n_batch):
                    cfg.n_batch = int(preset_values.n_batch)
                    inference_changed = True

        if "n_ctx" in values:
            cfg.context_length = int(values["n_ctx"])
            changed.append("n_ctx")
            inference_changed = True
        if "n_gpu_layers" in values:
            cfg.n_gpu_layers = int(values["n_gpu_layers"])
            changed.append("n_gpu_layers")
            inference_changed = True
        if "n_batch" in values:
            cfg.n_batch = int(values["n_batch"])
            changed.append("n_batch")
            inference_changed = True
        if values or "allow_override" in request:
            recommendation = self._recommended_plan()
            differs = (
                int(cfg.context_length or 0) != int(recommendation.n_ctx_max)
                or int(cfg.n_gpu_layers or 0) != int(recommendation.n_gpu_layers)
                or int(cfg.n_batch or 0) != int(recommendation.n_batch)
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
                if key == "temperature":
                    self.commands.temperature = cfg.temperature
                    self.commands.temp_manual = True
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
        if action == "model_download":
            yield from self._stream_model_download(request.get("name", ""))
            return
        if action == "update_download":
            yield from self._stream_update_download(request)
            return
        if action == "automation_run":
            yield from self._stream_automation()
            return
        if action == "model_hub_download":
            yield from self._stream_model_hub_download(request)
            return
        if action == "model_hub_load":
            yield from self._stream_model_hub_load(request)
            return
        if action == "automation_pick":
            yield from self._stream_automation_pick(request)
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
        if action == "automation_wizard":
            yield from self._stream_automation_wizard(request)
            return

        user_msg = request.get("message", "")
        if user_msg.startswith("/"):
            yield self._handle_chat(user_msg)
            return

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

        # [EN] Agent mode (capability extension-1, task 2): the loop in which the model calls
        # [EN] tools autonomously, carried on a dedicated channel. It is dispatched here rather
        # [EN] than in the chat path because it produces a **multi-step event stream**
        # [EN] (agent_step / tool_call / tool_result + a final done), unlike a single
        # [EN] generation.
        # [ZH] Agent 模式（后续版本 相关设计）：模型自主调用工具的循环，走独立通道。
        # [ZH] 放在这里而不是聊天路径中，是因为它产出的是**多步事件流**
        # [ZH] （agent_step / tool_call / tool_result + 最终 done），与单次生成不同。
        if self.conv.get_mode() == "agent":
            yield from self._stream_agent(user_msg)
            return

        search_context = self._search_if_enabled(user_msg)
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
        messages = self.conv.build_messages(user_msg, search_context, include_file=include_file,
                                            retrieval_context=self._retrieval_context_for(user_msg))
        # [EN] Task 10 (self-test fix-1): record how large **the messages actually sent this
        # [EN] turn** were.
        # [ZH] 相关设计（自测问题修复-1）：记录**本轮真正发出的消息**的大小。
        self._note_request_size(messages)
        # [EN] Task 2 (known-issues optimization-2): when search is enabled but no usable
        # [EN] result was obtained, **tell the user explicitly** instead of continuing silently
        # [EN] -- otherwise the user assumes "search did not work" or "the model ignored the
        # [EN] results", when in fact this layer obtained nothing. The notice is emitted as the
        # [EN] first paragraph prefix of the answer area.
        # [ZH] 相关设计（优化已知问题-2）：搜索已开启但未取得可用结果时，**明确告知用户**，
        # [ZH] 而不是静默继续 —— 否则用户会认为"搜索未生效"或"模型忽略了搜索结果"，
        # [ZH] 实际是该层未取得任何内容。该提示作为回答区的首段前缀发出。
        search_note = self._search_note()
        if search_note:
            yield {"type": "delta", "channel": _CH_ANSWER, "content": search_note + "\n\n"}

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
                    temperature=self.commands.temperature,
                    max_tokens=int(getattr(self.config, "max_tokens", 1024) or 1024),
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

    # [EN] ---------- Agent (capability extension-1, task 2) ----------
    # [ZH] ---------- Agent（后续版本 相关设计） ----------

    def agent_registry(self) -> Any:
        """
        [EN]
        The current tool registry (including permission state).

        Created lazily and **rebuilt after configuration changes**: ``_agent_registry`` is a
        cache, and ``refresh_agent_tools()`` clears it (that path is taken by MCP
        enable/disable and permission changes).

        [ZH]
        当前的工具注册表（含权限状态）。

        惰性创建，且**配置变更后重建**：``_agent_registry`` 是缓存，
        ``refresh_agent_tools()`` 会清除它（MCP 启停、权限变更都走该路径）。
        """
        from agent_tools import ToolContext, ToolRegistry

        if self._agent_registry is not None:
            return self._agent_registry
        cfg = getattr(self.config, "agent", None)
        allowed = list(getattr(cfg, "allow_sensitive", []) or [])
        registry = ToolRegistry(
            allowed_sensitive=allowed,
            ctx=ToolContext(
                conv=self.conv,
                search=self.search,
                summarizer=self.summarizer,
                count_tokens=self._estimate_tokens,
                files_dir=os.path.dirname(os.path.abspath(self.config.model_path or ".")),
            ),
        )
        self._agent_registry = registry
        return registry

    def refresh_agent_tools(self, discover_mcp: bool = False) -> Dict[str, Any]:
        """
        [EN]
        Rebuild the tool registry (called after MCP enable/disable or configuration changes).

        ``discover_mcp=True`` actually starts the configured MCP server processes and fetches
        their tool lists. This is an action that is **both costly and risky** (external
        processes, possibly network access), so it must be requested explicitly and is never
        performed implicitly on every question.

        [ZH]
        重建工具注册表（MCP 启停、配置变更后调用）。

        ``discover_mcp=True`` 会真正拉起配置中的 MCP Server 进程并拉取工具清单 ——
        这是**有代价也有风险**的动作（外部进程、可能联网），因此必须显式请求，
        不会在每次提问时隐式执行。
        """
        self._agent_registry = None
        registry = self.agent_registry()
        registered = 0
        if discover_mcp:
            bridge = self.mcp_bridge()
            bridge.auto_connect = True
            try:
                from mcp_client import register_mcp_tools

                registered = register_mcp_tools(registry, bridge)
            # [EN] MCP unavailability must not affect the Agent itself.
            # [ZH] MCP 不可用不得影响 Agent 本身。
            except Exception as exc:
                log.warning("注册 MCP 工具失败：%s", exc)
        return {"registered": registered,
                "tools": registry.names(),
                "enabled": [tool.name for tool in registry.available()]}

    def mcp_bridge(self) -> Any:
        """
        [EN]
        Build or reuse the MCP bridge for ``config.mcp`` (lazily).

        [ZH]
        按 ``config.mcp`` 构造/复用 MCP 桥接器（惰性）。
        """
        from mcp_client import McpBridge, McpServer

        cfg = getattr(self.config, "mcp", None)
        if self._mcp_bridge is None or self._mcp_signature != self._mcp_config_signature():
            if self._mcp_bridge is not None:
                # [EN] The configuration changed: close the old process first.
                # [ZH] 配置已变化：先关闭旧进程。
                self._mcp_bridge.close()
            servers: List[Any] = []
            for raw in (getattr(cfg, "servers", []) or []):
                item = raw if isinstance(raw, dict) else raw.model_dump()
                server = McpBridge.server_from_dict(item)
                if server is not None:
                    servers.append(server)
            self._mcp_bridge = McpBridge(
                servers=servers,
                timeout=float(getattr(cfg, "timeout_seconds", 20.0) or 20.0),
                auto_connect=bool(getattr(cfg, "auto_connect", False)),
            )
            self._mcp_signature = self._mcp_config_signature()
        return self._mcp_bridge

    def _mcp_config_signature(self) -> str:
        cfg = getattr(self.config, "mcp", None)
        try:
            return json.dumps({
                "enabled": bool(getattr(cfg, "enabled", False)),
                "servers": [
                    (item if isinstance(item, dict) else item.model_dump())
                    for item in (getattr(cfg, "servers", []) or [])
                ],
                "timeout": float(getattr(cfg, "timeout_seconds", 20.0) or 20.0),
            }, ensure_ascii=False, sort_keys=True)
        except Exception:
            return ""

    def mcp_diagnostics(self) -> Dict[str, Any]:
        """
        [EN]
        Observable MCP state (for /mcp, the settings page, and tests).

        [ZH]
        MCP 的可观测状态（供 /mcp、设置页与测试使用）。
        """
        cfg = getattr(self.config, "mcp", None)
        bridge = self.mcp_bridge()
        return {
            "enabled": bool(getattr(cfg, "enabled", False)),
            "auto_connect": bool(getattr(cfg, "auto_connect", False)),
            "timeout_seconds": float(getattr(cfg, "timeout_seconds", 20.0) or 20.0),
            "servers": bridge.status(),
            "tools": bridge.tool_names(),
            "error": bridge.last_error,
        }

    def agent_diagnostics(self) -> Dict[str, Any]:
        """
        [EN]
        Observable Agent state (for /agent, tests, and troubleshooting).

        The task requires "users can see every step of the Agent", so the backend must first
        retain "every step" as structured data before the frontend can display it correctly;
        otherwise troubleshooting depends on reading logs.

        [ZH]
        Agent 的可观测状态（供 /agent、测试与排查使用）。

        设计规范要求"用户能看到 Agent 的每一步"，后端必须先把"每一步"留存为结构化数据，
        前端才有可能正确显示；否则排查时只能依靠翻阅日志。
        """
        registry = self._agent_registry or self.agent_registry()
        cfg = getattr(self.config, "agent", None)
        return {
            "mode": self.conv.get_mode(),
            "max_steps": int(getattr(cfg, "max_steps", 4) or 4),
            "tools": registry.public_list(),
            "enabled_tools": [tool.name for tool in registry.available()],
            "last_run": getattr(self, "_last_agent_run", None),
        }

    def _iter_agent_events(self, runner: Any) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Turn the Agent loop (synchronous and blocking) into a generator that can yield in a
        streaming fashion.

        Why a thread plus a queue: every step of the loop calls the model (several seconds) and
        executes tools, so running it synchronously inside the generator would leave the UI
        silent for a long time. This follows the existing approach of the streaming path
        (``llm_engine._iter_with_heartbeat``): a reader thread plus a queue, with heartbeats
        emitted while waiting. The stop flag is passed to the loop (checked at every step) and
        also returns immediately here.

        [ZH]
        把 Agent 循环（同步、会阻塞）转为可流式产出的生成器。

        为什么使用线程 + 队列：循环中每一步都要调用模型（数秒）并执行工具，
        若在生成器内同步运行，界面上会出现长时间无任何输出 —— 与流式路径的
        既有做法（``llm_engine._iter_with_heartbeat``）保持一致：读者线程 + 队列，
        等待期间发送心跳。停止信号既传给循环（每步检查），也在此处立即返回。
        """
        import queue as _queue
        import threading

        events: "_queue.Queue[Any]" = _queue.Queue()
        box: Dict[str, Any] = {}

        def worker() -> None:
            try:
                box["run"] = runner(lambda event: events.put(event))
            # [EN] A failure of the loop itself must still report its cause.
            # [ZH] 循环本身失败也必须给出原因。
            except Exception as exc:
                box["error"] = str(exc)
            finally:
                events.put(None)

        thread = threading.Thread(target=worker, daemon=True, name="echodesk-agent")
        thread.start()
        heartbeat = float(getattr(self.config, "stream_heartbeat_seconds", 1.5) or 1.5)
        while True:
            if self.stop_event.is_set():
                break
            try:
                event = events.get(timeout=max(0.2, heartbeat))
            except _queue.Empty:
                yield {"type": "progress", "channel": _CH_HEARTBEAT, "agent": True}
                continue
            if event is None:
                break
            yield event
        thread.join(timeout=5)
        self._agent_run = box.get("run")

    def _handle_tool_confirmation(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Resolve a pending guard-tool confirmation from the frontend.

        [ZH]
        解除前端发来的高危工具待确认请求。
        """
        request_id = str(request.get("request_id") or "")
        approved = bool(request.get("approved"))
        resolved = self.resolve_tool_confirmation(request_id, approved)
        return {"type": "system", "content": "", "request_id": request_id,
                "resolved": resolved, "approved": approved}

    def resolve_tool_confirmation(self, request_id: str, approved: bool) -> bool:
        """
        [EN]
        Record the user's decision and wake the waiting agent worker.

        Returns whether the identifier matched a pending request.

        [ZH]
        记录用户决定并唤醒等待中的 Agent 工作线程。

        返回标识是否匹配到待确认请求。
        """
        with self._tool_confirm_lock:
            pending = self._pending_tool_confirmations.get(str(request_id or ""))
            if pending is None:
                return False
            pending["approved"] = bool(approved)
            pending["event"].set()
            return True

    def _confirm_agent_tool(self, request: Dict[str, Any]) -> bool:
        """
        [EN]
        Emit a ``confirm_tool`` event and block until the user answers or the
        configured timeout expires.

        The event is written under the shared stdout lock, so it cannot
        interleave with stream lines. The default answer is denial: a missing
        frontend, a timeout, or a stop request never executes the command.

        [ZH]
        发出 ``confirm_tool`` 事件，并阻塞到用户答复或配置的超时到期。

        事件在共享 stdout 锁内写出，不会与流式行交错。默认答复为拒绝：前端
        缺失、超时或用户中止都不会执行命令。
        """
        request_id = str(request.get("request_id") or uuid.uuid4().hex[:16])
        timeout = float(getattr(getattr(self.config, "agent", None),
                                "confirm_timeout_seconds", 120) or 120)
        pending: Dict[str, Any] = {"event": threading.Event(), "approved": False}
        with self._tool_confirm_lock:
            self._pending_tool_confirmations[request_id] = pending
        tool = str(request.get("tool") or "")
        command = str(request.get("command") or "")
        payload = dict(request)
        payload["request_id"] = request_id
        payload["type"] = "confirm_tool"
        payload["content"] = _t("agent.confirm_tool", tool=tool, command=command)
        try:
            _write(payload)
        except Exception:
            log.warning("发送工具确认事件失败", exc_info=True)
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            if pending["event"].wait(0.2):
                break
            if self.stop_event.is_set():
                break
        with self._tool_confirm_lock:
            self._pending_tool_confirmations.pop(request_id, None)
        approved = bool(pending["approved"] and not self.stop_event.is_set())
        log.info("高危工具确认 %s：%s", tool, "允许" if approved else "拒绝或超时")
        return approved

    def _stream_agent(self, user_msg: str) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Streaming handling of Agent mode: multi-step events plus a final done.

        [ZH]
        Agent 模式的流式处理：多步事件 + 最终 done。
        """
        from agent import AgentLoop

        cfg = getattr(self.config, "agent", None)
        registry = self.agent_registry()
        self._last_agent_run = None
        messages = self._agent_messages(user_msg, registry)
        loop = AgentLoop(
            self.llm, registry,
            max_steps=int(getattr(cfg, "max_steps", 4) or 4),
            stop_event=self.stop_event,
            max_tokens=int(getattr(self.config, "max_tokens", 1024) or 1024),
            max_observation_chars=int(getattr(cfg, "max_observation_chars", 1200) or 1200),
            confirm_tool=self._confirm_agent_tool,
        )
        temperature = float(getattr(self.commands, "temperature", 0.5) or 0.5)

        for event in self._iter_agent_events(
                lambda emit: loop.run(messages, temperature=temperature, on_event=emit)):
            if event.get("type") in ("agent_step", "tool_call", "tool_result"):
                yield event
            else:
                # [EN] Other events such as heartbeats are passed through unchanged (the
                # [EN] frontend only inspects type; extra fields are harmless).
                # [ZH] 心跳等其它事件原样透传（前端只识别 type，多余字段无影响）。
                yield event

        run = self._agent_run
        answer = str(getattr(run, "answer", "") or "") if run is not None else ""
        if run is None:
            answer = _t("sys.inference_failed", error="agent loop did not return")
        # [EN] Send the final answer once as streaming deltas: the frontend's existing render
        # [EN] path can display the conclusion without changes, while the done line remains the
        # [EN] authoritative content (consistent with the chat_stream contract).
        # [ZH] 把最终答案按流式 delta 发送一遍：前端既有的渲染路径无需改动即可显示结论，
        # [ZH] done 行仍是权威内容（与 chat_stream 的约定一致）。
        if answer:
            yield {"type": "delta", "channel": _CH_ANSWER, "content": answer}

        self.conv.add_message("user", user_msg)
        self.conv.add_message("assistant", answer)
        self._compact_history()

        if run is not None:
            self._last_agent_run = {
                "steps": run.trace(),
                "stopped": run.stopped,
                "hit_max_steps": run.hit_max_steps,
                "elapsed_ms": run.elapsed_ms,
                "tools": [step.tool for step in run.steps if step.tool],
            }

        done: Dict[str, Any] = {
            "type": "done",
            "content": answer,
            "stopped": bool(getattr(run, "stopped", False)),
            "agent": {
                "steps": len(getattr(run, "steps", []) or []),
                "tools": [step.tool for step in getattr(run, "steps", []) if step.tool],
                "hit_max_steps": bool(getattr(run, "hit_max_steps", False)),
                "elapsed_ms": int(getattr(run, "elapsed_ms", 0) or 0),
            },
        }
        self._attach_context(done)
        yield done

    def _agent_messages(self, user_msg: str, registry: Any) -> List[dict]:
        """
        [EN]
        Messages for an Agent turn: system prompt + **tool protocol appended at runtime** +
        history + question.

        The tool protocol and the available tool list are **dynamic** (they depend on
        permission switches), so they are appended here rather than written into
        ``MODES["agent"]``: the mode block must respect the 700-character audit limit, and the
        prompt must not list "tools that are currently disabled".

        [ZH]
        Agent 轮的 messages：系统提示词 + **运行期追加的工具协议** + 历史 + 问题。

        工具协议与可用工具清单是**动态的**（受权限开关影响），因此在此处追加，
        而不是写进 ``MODES["agent"]``：模式块必须遵守 700 字符的审计上限，
        也不能在提示词中列出"当前未启用的工具"。
        """
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
        messages = self.conv.build_messages(user_msg, "", include_file=include_file)
        self._note_request_size(messages)
        messages[0] = {
            "role": "system",
            "content": str(messages[0].get("content", "")) + "\n\n" + registry.protocol_text(),
        }
        return messages

    # [EN] ---------- Concrete actions ----------
    # [ZH] ---------- 具体动作 ----------

    # [EN] ---------- Local retrieval (capability extension-1, task 6) ----------
    # [ZH] ---------- 本地检索（后续版本 相关设计） ----------

    def retriever(self) -> Any:
        """
        [EN]
        The current retriever (created lazily, configured from ``config.retrieval``).

        [ZH]
        当前检索器（惰性创建，配置取自 ``config.retrieval``）。
        """
        if self._retriever is None:
            from retrieval import Retriever, config_view

            self._retriever = Retriever(config_view(getattr(self.config, "retrieval", None)))
        return self._retriever

    def index_files(self) -> Dict[str, Any]:
        """
        [EN]
        Synchronize **the files currently in the context** into the index (incrementally: only
        new files are added, only removed files are deleted).

        Why synchronize before every injection instead of "index once at upload": files can
        enter through /file, drag and drop, and session restoration, and same-name replacement
        is possible; aligning once "at the moment of use" is the least error-prone option. The
        alignment itself is an in-memory operation (measured under 20 ms for 47 KB), so it is
        not a performance concern.

        [ZH]
        把**当前上下文中的文件**同步进索引（增量：只补充新文件、只删除已移除的文件）。

        为什么在每次注入前同步而不是"上传时索引一次"：文件可经 /file、拖拽、
        会话恢复三条路径进入，且存在同名替换；统一在"使用时对齐一次"最不容易遗漏。
        对齐本身是纯内存操作（47 KB 实测 < 20 ms），不构成性能问题。
        """
        retriever = self.retriever()
        try:
            entries = list(self.conv.files)
        except Exception:
            return retriever.stats()
        present = {str(getattr(entry, "name", "") or "") for entry in entries}
        for name in list(retriever.files):
            if name not in present:
                retriever.remove_file(name)
        for entry in entries:
            name = str(getattr(entry, "name", "") or "")
            if not name:
                continue
            body = str(getattr(entry, "content", "") or "")
            if not body and str(getattr(entry, "stored_at", "") or ""):
                # [EN] The body was spilled to disk: read it back for indexing (retrieval
                # [EN] needs the original text, not the summary).
                # [ZH] 正文落盘的文件：从磁盘读回来索引（检索要用原文，不能用摘要）。
                try:
                    with open(str(entry.stored_at), "r", encoding="utf-8", errors="replace") as handle:
                        body = handle.read()
                except OSError:
                    body = ""
            if not body:
                body = str(getattr(entry, "summary", "") or "")
            if not body:
                continue
            existing = getattr(retriever, "_chunks_by_file", {}).get(name) or []
            # [EN] Already indexed with the same length -> do not split it again (incremental)
            # [ZH] 已经索引过且长度一致 → 不重复切分（增量）
            if existing and sum(len(chunk.text) for chunk in existing) >= min(len(body), 1):
                continue
            retriever.add_file(name, body)
        return retriever.stats()

    def retrieval_diagnostics(self) -> Dict[str, Any]:
        """
        [EN]
        Observable retrieval state (for /status, tests, and troubleshooting).

        [ZH]
        检索的可观测状态（供 /status、测试与排查使用）。
        """
        stats = dict(self.retriever().stats())
        stats["last_query"] = getattr(self, "_last_retrieval", None)
        return stats

    def _retrieval_context_for(self, user_msg: str) -> str:
        """
        [EN]
        Decide whether retrieval fragments should be injected, based on the division of labour
        between "summary" and "retrieval".

        Rules (consistent with the module description in ``backend/retrieval.py``):
          * no files, or retrieval disabled -> inject nothing;
          * the file is a **short document** (can be injected in full) -> do not retrieve; the
            full text is more accurate;
          * the file is a **long document** (already spilled to disk or already summarized) ->
            retrieve the top-k fragments and inject them.

        **A "the question mentions a file" requirement is deliberately no longer applied.**
        Retrieval is cheap and self-validating: a query with no hits (``as_context`` returns an
        empty string) injects nothing anyway. Retaining that gate meant questions that do not
        name the file, such as "what is the penalty rate", failed to retrieve -- while
        multi-document Q&A specifically requires that users need not name the file (this was
        the observed symptom in the first round of testing).
        (Task 1 supplement: the full-text injection path also carried the same gate, which was
        the root cause of "the model does not see the file content after upload"; that gate has
        now been removed as well, see ``include_file`` in ``_stream_chat``.)

        [ZH]
        按"摘要 vs 检索"的分工决定是否注入检索片段。

        规则（与 ``backend/retrieval.py`` 模块说明一致）：
          * 没有文件、或检索被关闭 → 不注入；
          * 文件是**短文档**（可全文注入）→ 不检索，全文更准确；
          * 文件是**长文档**（已落盘或已生成摘要）→ 检索 top-k 片段注入。

        **刻意不再要求"问题中提及文件"**。检索本身成本低且可自证：没有命中的查询
        （``as_context`` 返回空串）本来就不会注入任何内容。若沿用该开关，
        "违约金比例是多少"这类**不点名文件**的提问反而检索不到 —— 而多文档问答
        恰恰要求用户不必点名（首轮实测即为此症状）。
        （任务一补充：全文注入路径原先也挂有同一开关，这正是"上传文件后模型
        回答看不到文件内容"的根因；现已一并取消，见 ``_stream_chat`` 的
        ``include_file``。）
        """
        cfg = getattr(self.config, "retrieval", None)
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return ""
        if not self.conv.files:
            return ""
        long_docs = [entry for entry in self.conv.files
                     if str(getattr(entry, "stored_at", "") or "") or str(getattr(entry, "summary", "") or "")]
        if not long_docs:
            return ""
        try:
            self.index_files()
            context = self.retriever().as_context(user_msg)
        except Exception as exc:  # [EN] A retrieval failure must not affect the answer.
                                  # [ZH] 检索失败不能连累作答。
            log.warning("检索失败，退回原有注入方式：%s", exc)
            return ""
        if not context:
            return ""
        self._last_retrieval = {
            "query": user_msg,
            "files": [str(getattr(entry, "name", "")) for entry in long_docs],
            "injected_chars": len(context),
        }
        return context

    def configure_agent(self, tool: str = "", allowed: Any = True) -> Dict[str, Any]:
        """
        [EN]
        Write entry point for ``/agent``: read state, toggle a tool's permission, or change the
        step limit.

        Call shapes (decided by ``CommandHandler._agent``):
          * ``configure_agent()``                    -> read state only
          * ``configure_agent("web_search", True)``  -> authorize
          * ``configure_agent("__steps__", 5)``      -> change the step limit

        Permissions and the step limit are written to ``config.json`` (``config.save()``): one
        user press persists them. Otherwise every restart would require re-authorization,
        which would in practice be criticized and lead to the whole feature being disabled.

        [ZH]
        ``/agent`` 的写入口：读取状态、开关某个工具的权限、修改步数上限。

        调用形式（由 ``CommandHandler._agent`` 决定）：
          * ``configure_agent()``                    → 只读状态
          * ``configure_agent("web_search", True)``  → 授权
          * ``configure_agent("__steps__", 5)``      → 修改步数上限

        权限与步数均写入 ``config.json``（``config.save()``）：用户按一次即长期生效，
        否则每次重启都要重新授权，实际使用中必然引发不满并导致该功能被整体关闭。
        """
        registry = self.agent_registry()
        cfg = getattr(self.config, "agent", None)
        if tool == "__steps__":
            try:
                steps = max(1, min(8, int(allowed)))
            except (TypeError, ValueError):
                return {"ok": False, "max_steps": getattr(cfg, "max_steps", 4)}
            if cfg is not None:
                cfg.max_steps = steps
                self.config.save()
            return {"ok": True, "max_steps": steps}
        if tool:
            if not registry.allow(tool, bool(allowed)):
                return {"ok": False, "tools": registry.names()}
            if cfg is not None:
                cfg.allow_sensitive = sorted(registry.allowed_sensitive)
                self.config.save()
            return {"ok": True, "enabled": registry.is_allowed(registry.get(tool))}
        return self.agent_diagnostics()

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

        **Two cases are not intercepted** (after capability extension-1, task 1, guide is no
        longer a mode but a task template): ``/task guide`` (the user explicitly wants the model
        to list directions) and agent mode (the model is already meant to decide what to do).
        The predicate changed from "mode name equals guide" to "the task layer is the guide
        template".

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

        **两种情况下不拦截**（能力扩展-1 任务一之后，guide 不再是模式而是任务模板）：
        ``/task guide``（用户明确要模型自行列出方向）与 agent 模式（模型本来就要自行
        决定做什么）。判据从"模式名等于 guide"改为"任务层就是 guide 模板"。

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
        if self.conv.get_mode() == "agent":
            return ""
        if self.conv.history or self.conv.file_content:
            return ""
        from prompts import clarification_prompt, is_vague_question

        if not is_vague_question(user_msg):
            return ""
        return clarification_prompt(user_msg)

    def _handle_chat(self, user_msg: str) -> Dict[str, Any]:
        if user_msg.startswith("/"):
            content, action = self.commands.handle(user_msg)
            response = {"type": "system", "content": content}
            if action:
                self._merge_action(response, action)
            return response

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

        search_context = self._search_if_enabled(user_msg)
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
        messages = self.conv.build_messages(user_msg, search_context, include_file=include_file,
                                            retrieval_context=self._retrieval_context_for(user_msg))
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
                temperature=self.commands.temperature,
                max_tokens=int(getattr(self.config, "max_tokens", 1024) or 1024),
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

    def _stream_automation(self) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Run browser automation once and forward the engine's progress events to the frontend
        unchanged.

        Stopping: the frontend sends action=stop, which sets stop_event; the engine exits its
        loops as early as possible.

        [ZH]
        运行一次浏览器自动化，把引擎的进度事件原样转发给前端。

        停止：前端发送 action=stop 会置位 stop_event，引擎在各循环处尽早退出。
        """
        # [EN] Functional boundary of task 1: the "generate summary / review opinion" step of
        # [EN] automation depends on the model. With no model, provide guidance first so the
        # [EN] whole task does not fail halfway through (which wastes time and leaves partial
        # [EN] results).
        # [ZH] 相关设计的功能边界：自动化中"生成摘要/审核意见"这一步依赖模型。
        # [ZH] 无模型时先给出引导，避免整轮任务执行到一半才失败（既浪费时间又留下半份结果）。
        if not self.model_ready:
            yield {"type": "error",
                   "content": _t("sys.automation_need_model",
                                 guide=_t("sys.no_model_guide", dir=self.models_dir())),
                   "model_ready": False}
            return
        engine = self.automation()
        try:
            for event in engine.run(stop_event=self.stop_event):
                # [EN] On completion, also write the result into the conversation history so
                # [EN] later questions can reference it.
                # [ZH] 结束时把结果也写入对话历史，便于后续提问引用。
                if event.get("type") == "done":
                    summary = str(event.get("content") or "")
                    if summary:
                        self.conv.add_message("user", _t("sys.automation_result_marker"))
                        self.conv.add_message("assistant", summary)
                        self._compact_history()
                yield event
        except Exception as e:
            log.exception("自动化执行失败")
            yield {"type": "error", "content": _t("sys.automation_run_failed", error=e)}
        finally:
            # [EN] Fallback cleanup of the download temp directory (the engine cleans files per
            # [EN] task; this additionally removes the empty directories of this run).
            # [ZH] 兜底清理下载临时目录（引擎按任务清理文件，此处连同本轮的空目录一并清除）。
            try:
                removed = engine.cleanup_temp()
                if removed:
                    log_user(log, "sys.log_automation_cleanup", count=removed)
            except Exception:
                log.warning("自动化临时目录清理失败", exc_info=True)

    def _stream_automation_pick(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Visual selector picking (task 4.2): click an element on the real page to generate a
        selector.

        This is streamed because the user may need to log in and interact with the page, which
        can take minutes; the frontend needs live progress and must be able to stop at any time
        (by closing the browser window).

        [ZH]
        可视化选取选择器（设计规范 4.2）：在真实页面上点击元素即生成选择器。

        之所以走流式通道：用户可能需要在页面上登录、点击操作，耗时以分钟计，
        前端需要实时进度，并且能够随时停止（关闭浏览器窗口）。
        """
        target = str(request.get("target") or "task_list")
        url = str(request.get("url") or self.config.automation.task_list_url or "").strip()
        try:
            timeout_seconds = int(request.get("timeout") or 300)
        except (TypeError, ValueError):
            timeout_seconds = 300
        engine = self.automation()
        try:
            for event in engine.pick_selector(url, target, timeout_seconds=timeout_seconds,
                                             stop_event=self.stop_event):
                if event.get("type") == "done" and event.get("selector"):
                    self._apply_picked_selector(target, str(event["selector"]))
                yield event
        except Exception as e:  # noqa: BLE001
            log.exception("可视化选取失败")
            yield {"type": "error", "content": _t("sys.automation_pick_failed", error=e)}

    def _apply_picked_selector(self, target: str, selector: str) -> None:
        """
        [EN]
        Write the picking result into the configuration (the frontend only displays it; the
        backend persists it centrally to avoid double writes).

        [ZH]
        把选取结果写入配置（前端只负责展示，落盘由后端统一执行，避免双写）。
        """
        field = {
            "task_list": "task_list_selector",
            "task_row": "task_row_selector",
            "next_page": "next_page_selector",
            "task_detail_file": "task_detail_file_selector",
        }.get(target)
        if not field:
            return
        data = self.config.automation.model_dump()
        data[field] = selector
        try:
            self.config.automation = AutomationConfig(**data)
            self.config.save()
            log_user(log, "sys.log_visual_pick_written", field=field, selector=selector)
        except Exception:
            log.warning("保存可视化选取结果失败", exc_info=True)

    # =====================================================================
    # [EN] Task 17: reuse an already-open browser and two-phase visual selection (self-test
    # [EN] fix-1)
    # [ZH] 相关设计：复用已打开的浏览器 + 两段式可视化选取（自测问题修复-1）
    # =====================================================================

    #: [EN] Persistence field for the "login check element" in two-phase selection.
    #:
    #: [EN] It **must be a class attribute and accessed via ``self.``**: a bare name inside
    #: [EN] a method body follows LEGB and cannot see the class scope (automation.py's
    #: [EN] ``_target_label`` hit exactly this problem). The mapping table of the existing
    #: [EN] ``_apply_picked_selector`` has no login_check entry, so it would **silently
    #: [EN] discard** this selector even though the configuration does have
    #: [EN] ``login_check_selector``; the mapping is added here.
    #: [ZH] 两段式选取中"登录检测元素"的落盘字段。
    #:
    #: [ZH] **必须是类属性、且通过 ``self.`` 访问**：方法体中的裸名字走 LEGB，找不到类作用域
    #: [ZH] （automation.py 的 ``_target_label`` 曾因此出现问题）。既有
    #: [ZH] ``_apply_picked_selector`` 的映射表没有 login_check，会**静默丢弃**该选择器，
    #: [ZH] 而配置中确实有 ``login_check_selector`` 这一项，因此在此补充映射。
    _PICK_EXTRA_FIELDS: Dict[str, str] = {"login_check": "login_check_selector"}

    # [EN] This group consists of **additional** handlers; no existing function body was
    # [EN] changed: the old streaming ``automation_pick`` / ``_stream_automation_pick`` /
    # [EN] ``_apply_picked_selector`` are kept as-is, and the WPF UI and the existing tests
    # [EN] keep working as before.
    # [ZH] 这一组是**新增**的处理器，没有改动任何既有函数的函数体：
    # [ZH] 老的流式 ``automation_pick`` / ``_stream_automation_pick`` / ``_apply_picked_selector``
    # [ZH] 原样保留，WPF 界面与既有测试继续照旧工作。

    def _automation_browser_payload(self) -> Dict[str, Any]:
        """
        [EN]
        Build the browser-status payload returned to the frontend.

        [ZH]
        把"复用已打开的浏览器"的当前能力状态给前端（不含任何凭据）。
        """
        engine = self.automation()
        return engine.browser_reuse_payload()

    def _handle_automation_browser_reuse(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Handle reuse of an existing automation browser session.

        [ZH]
        查询/设置"复用已打开的浏览器"（相关设计 A：三档连接方式）。

        ``set`` 非空时按 patch 写配置——**只认白名单里的三个字段**，
        免得前端顺手把别的配置项也改了。之所以不并入 ``automation_config_set``：
        那是设置窗口「保存」按钮走的整体保存路径，而这里的三个字段是"用一次就想要
        立刻生效"的开关（连不上时改一下端口再试），单独一条更直接也更好排查。
        """
        patch = request.get("set")
        if isinstance(patch, dict) and patch:
            allowed = ("browser", "browser_reuse", "cdp_port")
            data = self.config.automation.model_dump()
            for key in allowed:
                if key in patch:
                    data[key] = patch[key]
            try:
                self.config.automation = AutomationConfig(**data)
                self.config.save()
            except Exception as e:  # noqa: BLE001
                log.warning("保存浏览器复用设置失败", exc_info=True)
                engine = self.automation()
                return {"type": "system",
                        "content": _t("sys.automation_config_invalid", error=e),
                        "browser_reuse": engine.browser_reuse_payload()}
        # [EN] **The engine must be fetched after the configuration has been written**: the
        # [EN] `AutomationEngine.cfg` attribute points at a configuration object, and the
        # [EN] statement above, `self.config.automation = AutomationConfig(...)`, replaces
        # [EN] that object, while `Backend.automation()` binds the new object to the engine
        # [EN] only when it is called. Fetching the engine first would return the **old**
        # [EN] values to the front end here (this was hit during verification: the UI was set
        # [EN] to "must reuse" but still displayed "auto").
        # [ZH] **必须在写完配置之后再取引擎**：`AutomationEngine.cfg` 指向的是一个配置对象，
        # [ZH] 上面那句 `self.config.automation = AutomationConfig(...)` 换掉了那个对象，
        # [ZH] 而 `Backend.automation()` 只在被调用时才会把新对象绑给引擎。
        # [ZH] 先取引擎的话，这里回给前端的会是**改动前**的旧值（验证踩到过：
        # [ZH] 界面点了"必须复用"却仍显示"自动"）。
        engine = self.automation()
        return {"type": "system", "content": "",
                "browser_reuse": engine.browser_reuse_payload()}

    def _handle_automation_launch_debug_browser(self, _request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Launch a debuggable browser for automation setup.

        [ZH]
        用调试模式启动一个浏览器（相关设计 A.2 的入口）。

        **这个浏览器不由程序关闭**：用户可以自己在里面登录、导航，程序只连上去用一下。
        """
        try:
            result = self.automation().launch_debug_browser()
        except Exception as e:  # noqa: BLE001
            log.exception("启动调试模式浏览器失败")
            return {"type": "system",
                    "content": _t("automation.reuse.debug_launch_failed", error=e),
                    "browser_debug": {"ok": False}}
        return {"type": "system", "content": str(result.get("message") or ""),
                "browser_debug": result}

    def _handle_automation_pick_step(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        [EN]
        Handle one step of the visual selector-picking flow.

        [ZH]
        两段式可视化选取的统一入口（open / begin / status / cancel / close）。

        每一步都返回**完整的当前状态**（``pick``），前端只负责照着显示：
        状态机在后端，刷新界面、切换设置页都不会把它弄丢；也因此"选取中"这个
        事实（脚本确实注入到了浏览器里）与界面显示永远一致。

        选取成功后由这里把选择器写进配置（复用既有的 ``_apply_picked_selector``，
        与老流程同一条落盘路径），并且**只写一次**——状态机里的待写标记取走即清空，
        前端每隔几百毫秒轮询也不会把配置文件反复重写。
        """
        engine = self.automation()
        step = str(request.get("step") or "status").strip().lower()
        # [EN] Local import of AutomationError: main.py loads `automation` on demand at the
        # [EN] top level (see automation()), and this follows the same style so that
        # [EN] Playwright is not started early just for one exception type.
        # [ZH] 局部导入 AutomationError：main.py 顶层是"按需加载 automation"的（见 automation()），
        # [ZH] 这里跟随同样的风格，避免为了一个异常类型把 Playwright 提前拉起来。
        from automation import AutomationError

        if step == "open" and engine.running:
            # [EN] Visual selection may not start while automation is running: that run is
            # [EN] **driving the same tab** (in reuse mode, the very page the user is on), so
            # [EN] injecting the picker script here would interfere with its clicks and
            # [EN] navigation, and both sides would fail in hard-to-understand ways. Better
            # [EN] to state this clearly up front.
            # [ZH] 自动化跑着的时候不允许开始选取：那条运行正在**驱动同一个标签页**
            # [ZH] （复用模式下就是用户当前这一页），此时注入选取脚本会与它的点击/导航
            # [ZH] 互相干扰，两边都会给出难以理解的失败。宁可先说清楚。
            return {"type": "system", "content": _t("automation.picker.busy_running"),
                    "pick": engine.pick_state()}

        try:
            if step == "open":
                payload = engine.pick_open(str(request.get("target") or "task_list"),
                                           str(request.get("url") or ""))
            elif step == "begin":
                payload = engine.pick_begin()
            elif step == "cancel":
                payload = engine.pick_cancel()
            elif step == "close":
                payload = engine.pick_close()
            else:
                payload = engine.pick_status()
        except AutomationError as e:
            # [EN] An illegal action rejected by the state machine (starting a pick before a
            # [EN] page is open, or starting another pick while one is in progress): this is
            # [EN] not a fault; state it clearly and return the current state.
            # [ZH] 状态机拒绝的非法动作（还没打开就开始选、正在选又要开始选）：
            # [ZH] 这不是故障，把话说清楚并回传当前状态即可
            return {"type": "system", "content": str(e), "pick": engine.pick_state()}
        except Exception as e:  # noqa: BLE001
            log.exception("可视化选取（两段式）失败")
            return {"type": "system", "content": _t("sys.automation_pick_failed", error=e),
                    "pick": engine.pick_state()}
        pending = engine.take_pending_apply()
        if pending:
            target, selector = pending
            if target in self._PICK_EXTRA_FIELDS:
                # [EN] The "login check element" (login_check) is not in the mapping table of
                # [EN] the existing _apply_picked_selector, and that old path would
                # [EN] **silently discard** persisting this selector (the configuration does
                # [EN] have automation.login_check_selector). _apply_picked_selector is an
                # [EN] existing function whose body is not changed here, so a mapping is added
                # [EN] here and the same persistence logic is reused.
                # [ZH] "登录检测元素"（login_check）不在既有 _apply_picked_selector 的映射表里，
                # [ZH] 那条老路径会**静默丢弃**这个选择器的落盘（配置里明明有
                # [ZH] automation.login_check_selector 这一项）。_apply_picked_selector 属于
                # [ZH] 既有函数、本次不改它的函数体，所以在这里补一条映射后走同一套落盘逻辑。
                self._save_automation_field(self._PICK_EXTRA_FIELDS[target], selector)
            else:
                self._apply_picked_selector(target, selector)
        content = str(payload.get("message") or payload.get("hint") or "")
        return {"type": "system", "content": content, "pick": payload}

    def _save_automation_field(self, field: str, value: str) -> None:
        """
        [EN]
        Persist a single automation configuration field.

        [ZH]
        把单个自动化配置字段写盘（与 ``_apply_picked_selector`` 同一口径）。
        """
        data = self.config.automation.model_dump()
        if field not in data:
            return
        data[field] = value
        try:
            self.config.automation = AutomationConfig(**data)
            self.config.save()
            log_user(log, "sys.log_visual_pick_written", field=field, selector=value)
        except Exception:
            log.warning("保存可视化选取结果失败", exc_info=True)

    def _stream_automation_wizard(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Stream the automation configuration wizard responses.

        [ZH]
        测试选择器向导（设计规范 4.5）：第 4 步需要跑一次真实任务，因此走流式。

        第 1~3 步由 ``automation_wizard_step`` 同步处理；这里只在 step=4 时使用。
        """
        try:
            step = int(request.get("step") or 4)
        except (TypeError, ValueError):
            step = 4
        engine = self.automation()
        try:
            yield {"type": "delta", "content": _t("automation.wizard_step_start", step=step)}
            if step == 4:
                for event in engine.run(stop_event=self.stop_event, limit=1, dry_run=True):
                    yield event
            else:
                result = engine.wizard_step(step)
                yield {"type": "done", "content": result.get("detail", ""), "wizard": result}
        except Exception as e:  # noqa: BLE001
            log.exception("测试向导执行失败")
            yield {"type": "error", "content": _t("sys.automation_wizard_run_failed",
                                                  step=step, error=e)}
        finally:
            try:
                engine.cleanup_temp()
            except Exception:
                pass

    def _stream_model_hub_download(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Stream a model-hub download with progress events.

        [ZH]
        下载模型库中选定的 GGUF 文件（新增四）。

        进度以 delta 行输出（百分比 + 速度 + 剩余时间），前端实时显示；
        前端发送 action=stop 可取消，取消时保留 ``.part`` 断点，下次继续。
        """
        repo = str(request.get("repo") or "").strip()
        filename = str(request.get("filename") or "").strip()
        try:
            expected = int(request.get("size") or 0)
        except (TypeError, ValueError):
            expected = 0
        source = request.get("source")
        hub = self.model_hub()

        # [EN] Final manual confirmation before the download starts (size and expected disk
        # [EN] usage).
        # [ZH] 下载前的最后一次人工确认信息（大小与预计磁盘占用）
        yield {
            "type": "delta",
            "content": _t("sys.hub_download_prepare", repo=repo, filename=filename,
                          size=(_t("sys.hub_download_size", mb=f"{expected / 1048576:.1f}")
                                if expected else "")),
        }
        self.stop_event.clear()

        # [EN] The download runs in a worker thread (urllib blocks); the main thread polls
        # [EN] the progress cache and pushes changes to the front end.
        # [ZH] 下载在子线程里进行（urllib 阻塞），主线程轮询进度缓存并把变化推给前端
        latest: Dict[str, str] = {"line": ""}

        def _progress(info: Dict[str, Any]) -> None:
            """
            [EN]
            Report progress for a long-running operation.

            [ZH]
            字节级进度 → 一行人类可读文本（速度与剩余时间，设计规范要求）。
            """
            if "retry" in info:
                latest["line"] = _t("sys.hub_download_retry", attempt=info.get("attempt"),
                                    retries=info.get("retries"), error=info.get("error"))
                return
            done = int(info.get("done") or 0)
            total = int(info.get("total") or 0)
            speed = float(info.get("speed") or 0)
            eta = int(info.get("eta") or 0)
            pct = float(info.get("percent") or 0)
            line = f"📥 {pct:5.1f}%  {done / 1048576:.1f}/{total / 1048576:.1f} MB"
            if speed > 0:
                line += f"  {speed / 1048576:.2f} MB/s"
            if eta > 0:
                line += _t("sys.hub_download_eta", time=f"{eta // 60:02d}:{eta % 60:02d}")
            latest["line"] = line

        result: Dict[str, Any] = {}
        done_flag = threading.Event()

        def _worker() -> None:
            try:
                result.update(hub.download(
                    repo, filename, expected_size=expected, source=source,
                    on_progress=_progress,
                    should_stop=lambda: self.stop_event.is_set(),
                    retries=int(getattr(self.config.model_hub, "download_retries", 3) or 3),
                ))
            except Exception as exc:  # noqa: BLE001
                result.update({"ok": False,
                               "message": _t("sys.hub_download_failed", error=exc), "path": ""})
            finally:
                done_flag.set()

        try:
            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            reported = ""
            while not done_flag.wait(0.4):
                if latest["line"] and latest["line"] != reported:
                    reported = latest["line"]
                    yield {"type": "delta", "content": reported}
                if self.stop_event.is_set():
                    yield {"type": "delta", "content": _t("sys.hub_download_cancelling")}
            if latest["line"] and latest["line"] != reported:
                yield {"type": "delta", "content": latest["line"]}
            thread.join(timeout=10)

            if result.get("ok"):
                path = str(result.get("path") or "")
                self.conv.add_message("user", _t("sys.model_download_user_marker", filename=filename))
                self.conv.add_message("assistant", _t("sys.model_downloaded_to_library",
                                                     name=os.path.basename(path)))
                self._compact_history()
                # [EN] Design specification: after the download completes the model is
                # [EN] **hot-loaded in place**, so the user does not have to restart the
                # [EN] process or the front end. On failure (corrupt file, unsupported
                # [EN] format, insufficient memory) the backend stays in the no-model state
                # [EN] and reports the reason plus a retry path.
                # [ZH] 相关设计：下载完成后**就地热加载**，用户不必重启进程/前端。
                # [ZH] 失败（损坏、格式不支持、内存不足）时保持无模型状态并给出原因与重试路径。
                yield {"type": "delta", "content": _t("sys.model_hot_loading",
                                                      name=os.path.basename(path))}
                load = self.load_model(os.path.basename(path))
                if load.get("ok"):
                    content = (f"✅ {result.get('message') or _t('sys.download_done')} · "
                               + _t("sys.model_hot_loaded", name=load.get("name", "")))
                else:
                    content = (f"⚠️ {result.get('message') or _t('sys.download_done')} · "
                               + _t("sys.model_hot_load_failed", error=load.get("error", "")))
                yield {"type": "done", "content": content,
                       "model": path if load.get("ok") else "",
                       "model_load": load, "model_state": self.model_state()}
            else:
                icon = "⏹" if result.get("cancelled") else "❌"
                yield {"type": "done",
                       "content": f"{icon} {result.get('message') or _t('sys.download_incomplete')}",
                       "cancelled": bool(result.get("cancelled"))}
        except Exception as e:
            log.exception("模型下载失败")
            yield {"type": "error", "content": _t("sys.download_failed", error=e)}

    def _handle_file(self, filepath: str) -> Dict[str, Any]:
        if not filepath:
            return {"type": "system", "content": _t("sys.file_need_path")}
        return {"type": "system", "content": self.load_file_into_context(filepath)}

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
        job = self._document_jobs.submit(paths)
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
            stored = ""
            threshold = int(getattr(cfg, "disk_threshold_chars", 0) or 0)
            if threshold > 0 and chars > threshold:
                stored = self._store_large_file(name, content)
            effective_ctx = int(getattr(self.llm, "max_n_ctx", 0)
                                or getattr(self.llm, "n_ctx", 0) or 0)
            summary_text = ""
            summary_meta: Dict[str, Any] = {}
            result = None
            if self.model_ready and self.summarizer.should_use_summary(content, effective_ctx):
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

            if summary_text:
                final_text = (_t("sys.file_async_summary_head", name=name) + "\n"
                              + summary_text)
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
        # [EN] The summarisation trigger is evaluated against the hardware
        # [EN] ceiling, not the small initial window: a file that fits within the
        # [EN] ceiling expands the window on demand and must not be summarised
        # [EN] prematurely.
        # [ZH] 摘要触发判断以硬件上限为基准，而非较小的初始窗口：能够容纳在上限
        # [ZH] 之内的文件会按需扩展窗口，不得被提前摘要。
        effective_ctx = int(getattr(self.llm, "max_n_ctx", 0) or self.llm.n_ctx or 0)
        if allow_summary and self.summarizer.should_use_summary(content, effective_ctx):
            log.info(
                "文件 %s 约 %d tokens，超过窗口 %d 的 %.0f%%：转入分段摘要",
                name, tokens, effective_ctx, self.config.summarize.trigger_ratio * 100,
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
            head = _t("sys.file_auto_summary_head", name=name, tokens=tokens,
                      n_ctx=effective_ctx,
                      ratio=f"{self.config.summarize.trigger_ratio:.0%}") + "\n"
            return {"ok": True, "name": name, "chars": len(content), "tokens": tokens,
                    "needs_summary": True,
                    "message": head + self.summarize_entry(
                        entry, force=True, source_name=name,
                        trigger=_t("sys.summarize_trigger_auto"),
                        progress_sink=progress_sink)}
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
                        trigger: str = "", progress_sink=None) -> str:
        """
        [EN]
        Summarize a specific file entry.

        [ZH]
        对**指定文件条目**做分段摘要（多文件下必须能指名道姓，相关设计）。

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

    def _handle_model_download(self, name: str) -> Dict[str, Any]:
        """
        [EN]
        Handle a model download request.

        [ZH]
        用户已同意后下载模型并热切换。
        """
        if not name:
            return {"type": "system", "content": _t("sys.model_need_name")}
        try:
            path = download_model(name, self.config.model_repo, hf_endpoint=self.config.hf_endpoint)
        except Exception as e:
            return {"type": "system", "content": _t("sys.download_failed", error=e)}
        try:
            self.llm.reload(path)
        except Exception as e:
            return {"type": "system", "content": _t("sys.model_load_after_download_failed", error=e)}
        self.config.model_path = name
        self.config.save()
        return {"type": "system", "content": _t("sys.model_downloaded_and_loaded", name=name)}

    def _stream_model_download(self, name: str) -> Iterator[Dict[str, Any]]:
        """
        [EN]
        Stream model-download progress and completion.

        [ZH]
        流式下载模型：后台线程下载，逐条 yield 进度，最后 yield done/error。

        进度来自 download_model 的 on_progress 回调（字节 → 百分比）。
        """
        if not name:
            yield {"type": "error", "content": _t("sys.model_need_name")}
            return
        q: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                path = download_model(
                    name,
                    self.config.model_repo,
                    hf_endpoint=self.config.hf_endpoint,
                    on_progress=lambda done, total: q.put(("progress", done, total)),
                )
                q.put(("path", path, 0, 0))
            except Exception as e:
                q.put(("error", None, 0, 0, str(e)))

        threading.Thread(target=worker, daemon=True).start()
        while True:
            kind, a, b, *_ = q.get()
            if kind == "progress":
                total = b
                pct = int(a * 100 / total) if total > 0 else 0
                yield {"type": "delta", "content": _t(
                    "sys.model_downloading_progress", name=name, pct=pct,
                    done=self._fmt_mb(a), total=self._fmt_mb(total))}
            elif kind == "path":
                break
            else:  # error
                yield {"type": "error", "content": _t("sys.download_failed", error=a)}
                return

        # [EN] Download complete -> load and hot-switch.
        # [ZH] 下载完成 → 加载并热切换
        try:
            self.llm.reload(a)
        except Exception as e:
            yield {"type": "error", "content": _t("sys.model_load_after_download_failed", error=e)}
            return
        self.config.model_path = name
        self.config.save()
        yield {"type": "done", "content": _t("sys.model_downloaded_and_loaded", name=name)}

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

    def _search_if_enabled(self, query: str) -> str:
        """
        [EN]
        Run a web search when search is enabled.

        [ZH]
        按开关执行搜索并返回**可注入上下文**的文本（不可用时为空串）。

        优化已知问题-2 相关设计：失败与"没搜到"都**如实记录**，并且把不可用的原因
        记进 ``self._last_search_note``，由 :meth:`_search_note` 转成给用户的一句话
        —— 旧实现把失败静默成空串，用户完全看不出"搜索其实没生效"，只看得出
        "回答里没有搜索结果"。
        """
        self._last_search_note = ""
        if not self.commands.search_enabled:
            return ""
        outcome = self.search.search(query)
        if outcome.usable:
            self._last_search = outcome
            return outcome.as_context()
        self._last_search = outcome
        # [EN] Unavailable: distinguish "failed" from "no results"; the two need different
        # [EN] wording for the user.
        # [ZH] 不可用：区分"失败"与"没有结果"，两者要给用户不同的说法
        if not outcome.ok:
            # [EN] Item 4 of the design specification (self-test fix-1): **translate the
            # [EN] underlying exception into a category the user can understand**. The old
            # [EN] code put outcome.error straight into the message, so the user saw
            # [EN] "web search failed (https://www.bing.com/search ConnectError: error
            # [EN] sending request for url (...))" -- wording meant for developers that
            # [EN] neither said what to do nor distinguished "the network is down" from
            # [EN] "the search source changed". Categories now produce user-facing wording;
            # [EN] the original error still goes to the log and to search_diagnostics, so it
            # [EN] is available when troubleshooting.
            # [ZH] 相关设计第 4 条（自测问题修复-1）：把底层异常**翻译成用户能理解的一类**。
            # [ZH] 旧写法直接把 outcome.error 塞进提示，用户看到的是
            # [ZH] 「联网搜索失败（https://www.bing.com/search ConnectError: error sending
            # [ZH]   request for url (…)）」—— 那是给开发者看的话，既没告诉他该怎么办，
            # [ZH] 也没区分"网络不通"和"搜索源改版"。现在按类别给面向用户的说法；
            # [ZH] 原始错误仍进日志与 search_diagnostics，排查时拿得到。
            kind = getattr(outcome, "error_kind", "") or "unknown"
            key = FAIL_KIND_KEYS.get(kind, "sys.search_failed")
            if key == "sys.search_failed":
                self._last_search_note = _t(key, error=outcome.error[:120])
            else:
                self._last_search_note = _t(key)
            log.warning("搜索失败（类别=%s 源=%s）：%s",
                        kind, getattr(outcome, "source", ""), outcome.error)
        else:
            self._last_search_note = _t("sys.search_empty")
        log_user(log, "sys.log_search_no_result",
                 ok=outcome.ok, count=outcome.count, reason=self._last_search_note)
        return ""

    def _search_note(self) -> str:
        """
        [EN]
        Build the note describing search results.

        [ZH]
        取出上一次搜索的"为什么没用上"说明（无则空串）。
        """
        return str(getattr(self, "_last_search_note", "") or "")

    def search_diagnostics(self) -> Dict[str, Any]:
        """
        [EN]
        Return diagnostics about the search subsystem.

        [ZH]
        搜索链路的可观测快照（供状态区与测试断言四个环节）。

        设计规范要求"逐环节排查"，所以这里把每个环节的结果都暴露出来：
        是否开启、是否执行、返回条数、注入字符数、失败原因。
        """
        outcome = getattr(self, "_last_search", None)
        return {
            "enabled": bool(self.commands.search_enabled),
            "executed": outcome is not None,
            "ok": bool(outcome.ok) if outcome is not None else False,
            "results": int(outcome.count) if outcome is not None else 0,
            "elapsed_ms": int(outcome.elapsed_ms) if outcome is not None else 0,
            "error": str(outcome.error) if outcome is not None else "",
            # [EN] Design specification: also expose the "failure category" and the search
            # [EN] source that actually took effect. On networks in mainland China, "the
            # [EN] primary source always fails and the built-in Bing source does the work"
            # [EN] must be visible in diagnostics; otherwise troubleshooting keeps chasing
            # [EN] duckduckgo.
            # [ZH] 相关设计：把"失败类别"与"实际生效的搜索源"也暴露出来，
            # [ZH] 国内网络下"主源永远失败、真正干活的是内建 Bing 源"这件事
            # [ZH] 必须能从诊断里看出来，否则排查会一直盯着 duckduckgo 打转。
            "error_kind": str(getattr(outcome, "error_kind", "")) if outcome is not None else "",
            "source": str(getattr(outcome, "source", "")) if outcome is not None else "",
            "note": self._search_note(),
        }

    @staticmethod
    def _merge_action(response: Dict[str, Any], action: Dict[str, Any]) -> None:
        """
        [EN]
        Merge an automation action into the pending action map.

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
    model_path = check_model(config.model_path)
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

    # [EN] Design specification in an earlier release: load installed skills and local
    # [EN] plugins (discovery / import / registration commands). Placed after warm-up so a
    # [EN] skill-loading failure cannot slow down or block application startup, and the
    # [EN] exceptions are already isolated.
    # [ZH] 此前版本 相关设计：加载已安装技能与本地插件（发现/导入/注册命令）。
    # [ZH] 放在预热之后：技能加载失败不会拖慢或阻塞主程序启动，且异常已被隔离。
    try:
        skill_summary = backend.load_skills()
        if skill_summary.get("total"):
            log_user(log, "sys.log_skills_loaded",
                     total=skill_summary.get("total"), failed=skill_summary.get("failed"),
                     skipped=skill_summary.get("skipped"))
    except Exception:
        log.exception("技能加载阶段异常（已忽略）")
        skill_summary = {}

    # [EN] Design specification: tell the front end when the model is missing
    # [EN] (**non-blocking**) so that it shows guidance instead of a forced choice such as
    # [EN] "download now or refuse and exit". Emitted before ready, so the front end gets
    # [EN] all the information at once when it renders the ready state.
    # [ZH] 相关设计：缺模型时告知前端（**非阻塞**）——前端据此显示引导，而不是弹"是否下载、
    # [ZH] 拒绝就退出"的强制选择。放在 ready 之前，前端渲染就绪状态时一次就能拿到全部信息。
    model_missing: Dict[str, Any] = {}
    if not backend.model_ready:
        target, size, repo_error = "", 0, ""
        try:
            target, size = find_best_model(config.model_repo, config.model_path,
                                           hf_endpoint=config.hf_endpoint)
        except Exception as e:  # noqa: BLE001
            # [EN] A failed repository lookup (offline / unreachable mirror) does not affect
            # [EN] no-model startup: the guidance still offers both the model marketplace and
            # [EN] manual placement, only without an "expected size" to display.
            # [ZH] 查仓库失败（离线/镜像不可达）不影响无模型启动：引导里仍给出模型市场与
            # [ZH] 手动放置两条路径，只是没有"预计体积"可显示。
            log.warning("查询模型仓库失败（不影响无模型启动）: %s", e)
            repo_error = str(e)
        model_missing = {
            "model": config.model_path,
            "repo": config.model_repo,
            "target": target or config.model_path,
            "size": int(size or 0),
            "models_dir": backend.models_dir(),
            "error": repo_error,
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
            "size": int(size or 0),
            "repo": config.model_repo,
            "current": config.model_path,
            "target": target or config.model_path,
            "models_dir": backend.models_dir(),
            "content": _t(
                "sys.model_missing_ask", model=config.model_path, repo=config.model_repo,
                target=target or config.model_path,
                size=(_t("sys.model_missing_size", mb=f"{size / 1048576:.0f}")
                      if size else _t("sys.model_missing_unknown_size")),
                dir=backend.models_dir(),
            ),
        })

    log_user(log, "sys.log_ready", app=APP_NAME, version=APP_VERSION, studio=STUDIO)
    # [EN] Ready declaration: emitted only after warm-up and all other preparation have
    # [EN] finished; the front end uses it to unlock input. It also carries the restored
    # [EN] history for the front end to display.
    # [ZH] 就绪声明：预热等准备工作全部完成后才输出，前端据此放行输入；附带恢复的历史消息供前端展示
    _write({"type": "ready", "version": APP_VERSION, "studio": STUDIO, "restored": restored,
            "history": backend.conv.history,
            "locale": _i18n().locale, "i18n": _i18n().info(),
            "commands": backend.commands.all_commands(),
            "skills": backend.skills().status(),
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
        try:
            for response in backend.stream(request):
                _write(response)
        finally:
            backend.stream_active = False
            backend.stop_event.clear()
            backend.save_session()

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

        # [EN] While a stream is active: command requests (starting with /) and stop are
        # [EN] allowed; new conversation or file requests are told that the backend is busy.
        # [ZH] 流式生成期间：指令类请求（以 / 开头）与 stop 允许；新的对话/文件请求提示忙碌
        if backend.stream_active and action != "stop":
            is_command = isinstance(request.get("message"), str) and request.get("message", "").startswith("/")
            # [EN] Liveness probes and background-job polling are lightweight and
            # [EN] must remain available while a long task runs (v0.2.6 task 2.1).
            # [ZH] 存活探测与后台作业轮询属于轻量请求，长任务运行期间必须保持
            # [ZH] 可用（v0.2.6 任务 2.1）。
            is_lightweight = action in ("ping", "file_job_status", "file_job_cancel",
                                        "confirm_tool", "media_state",
                                        "audio_record_start", "audio_record_stop",
                                        "audio_record_cancel")
            if not is_command and not is_lightweight:
                # [EN] Busy flag (model marketplace optimization in an earlier release): the
                # [EN] single stdout stream is shared, and two threads writing at once
                # [EN] interleave into broken JSON, so concurrent requests are refused while
                # [EN] long tasks run. The structured flag lets the UI say "the backend is
                # [EN] busy" instead of "the model center is broken".
                # [ZH] busy 标记（此前版本 模型市场优化）：单条 stdout 是共享流，
                # [ZH] 两个线程同时写会交错成坏 JSON，所以长任务期间一律拒绝并发请求。
                # [ZH] 带上结构化标记，界面才能说清"是后端忙"而不是"模型中心坏了"。
                _write({"type": "system", "content": _t("sys.busy"), "busy": True})
                continue

        if action in ("chat_stream", "model_download", "update_download", "automation_run",
                      "model_hub_download", "automation_pick", "automation_wizard",
                      "file_stream", "model_hub_load"):
            # [EN] All nine long tasks run on streaming threads: progress is emitted live and
            # [EN] stop can interrupt them. automation_* also checks stop_event to exit early
            # [EN] and close the browser; file_stream (design specification) keeps reporting
            # [EN] progress during chunked summarization, and stop can really interrupt
            # [EN] generation; model_hub_load (model marketplace optimization, design
            # [EN] specification) reports "N entries fetched" page by page.
            # [ZH] 九个长任务都走流式线程：进度实时输出，stop 可中断。
            # [ZH] automation_* 还会检查 stop_event 尽早退出并关闭浏览器；
            # [ZH] file_stream（相关设计）在分段摘要期间持续报进度，stop 能真正打断生成；
            # [ZH] model_hub_load（模型市场优化 相关设计）按页报告"已取回 N 条"。
            if action in ("automation_run", "automation_pick", "automation_wizard") and backend.stream_active:
                _write({"type": "system", "content": _t("sys.automation_running")})
                continue
            backend.stop_event.clear()
            backend.stream_active = True
            threading.Thread(target=_stream_worker, args=(request,), daemon=True).start()
        elif action == "stop":
            if backend.stream_active:
                backend.stop_event.set()
                # [EN] The automation engine has its own stop entry point; notify it as well
                # [EN] so that the browser is closed promptly.
                # [ZH] 自动化引擎另有自己的停止入口，一并通知，确保浏览器及时关闭
                if backend._automation is not None:
                    backend._automation.stop()
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
