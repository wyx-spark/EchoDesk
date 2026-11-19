# -*- coding: utf-8 -*-
"""
[EN]
Reader backend: model lifecycle. Split out of the former single-file ``main.py``; the
module contains only the class methods belonging to this responsibility.

[ZH]
Reader 后端：模型生命周期。自原先的单文件 ``main.py`` 拆出；本模块只包含属于该职责的
类方法。
"""
from __future__ import annotations

from backend_support import *  # noqa: F401,F403 - shared backend support layer


class ModelMixin:
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


