# -*- coding: utf-8 -*-
"""
[EN]
EchoDesk Reader backend entry point. It initialises the process, owns the request
state object and terminates in the stdin/stdout JSON protocol loop. Routing lives
in :mod:`backend_router`, model lifecycle in :mod:`backend_model`, conversation in
:mod:`backend_chat`, documents in :mod:`backend_documents`, profiles and context in
:mod:`backend_profiles`, and shared helpers in :mod:`backend_support`.

[ZH]
EchoDesk Reader 后端入口：负责进程初始化、持有请求状态对象，并收束于 stdin/stdout
JSON 协议循环。路由见 :mod:`backend_router`，模型生命周期见 :mod:`backend_model`，
会话见 :mod:`backend_chat`，文档见 :mod:`backend_documents`，档案与上下文见
:mod:`backend_profiles`，共享工具见 :mod:`backend_support`。
"""
from __future__ import annotations

from backend_support import *
# [EN] Explicit re-imports: the reverse language-key audit checks each
# [EN] module for its own i18n imports, and star imports are not visible to it.
# [ZH] 显式重新导入：反向语言键审计按文件检查 i18n 导入，星号导入对它不可见。
from i18n import log_user
from i18n import t as _t  # noqa: F401,F403 - shared backend support layer
from backend_model import ModelMixin
from backend_profiles import ProfileMixin
from backend_chat import ChatMixin
from backend_documents import DocumentMixin
from backend_router import RouterMixin


class Backend(ModelMixin, ProfileMixin, ChatMixin, DocumentMixin, RouterMixin):
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

