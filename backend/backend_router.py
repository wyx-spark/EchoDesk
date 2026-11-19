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
from __future__ import annotations

from backend_support import *  # noqa: F401,F403 - shared backend support layer


class RouterMixin:
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


