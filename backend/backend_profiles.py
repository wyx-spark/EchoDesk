# -*- coding: utf-8 -*-
"""
[EN]
Reader backend: model profiles, context reporting and capability settings. Split out of the former single-file ``main.py``; the
module contains only the class methods belonging to this responsibility.

[ZH]
Reader 后端：模型档案、上下文报告与能力设置。自原先的单文件 ``main.py`` 拆出；本模块只包含属于该职责的
类方法。
"""
from __future__ import annotations

from backend_support import *
# [EN] Explicit re-imports: the reverse language-key audit checks each
# [EN] module for its own i18n imports, and star imports are not visible to it.
# [ZH] 显式重新导入：反向语言键审计按文件检查 i18n 导入，星号导入对它不可见。
from i18n import log_user
from i18n import t as _t  # noqa: F401,F403 - shared backend support layer


class ProfileMixin:
    PROFILE_FIELDS = ("path", "repo", "context_length", "temperature", "note", "quantization")

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
            # [EN] Context page inputs: the stored hard ceiling, the active RoPE mode and
            # [EN] whether the model file brings its own scaling (so the page can explain
            # [EN] instead of letting the user stack two extensions).
            # [ZH] 上下文页所需的输入：已存硬上限、当前生效的 RoPE 模式，以及模型文件是否
            # [ZH] 自带缩放参数（页面据此解释，而不是让用户叠加两套扩展）。
            try:
                rope_cfg = self.inference_config.resolve_rope()
            except Exception:  # noqa: BLE001 - diagnostics must never break the snapshot
                rope_cfg = {"type": "none", "scale": 1.0, "freq_scale": 0.0, "orig_ctx": 0}
            response["n_ctx_hard_limit"] = int(
                getattr(self.inference_config, "n_ctx_max", 0) or 0)
            response["rope"] = rope_cfg
            response["model_rope"] = self._model_embedded_rope()
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


    def _model_embedded_rope(self) -> Dict[str, Any]:
        """
        [EN]
        Return the RoPE scaling recorded inside the current model file, or an empty
        mapping when the file carries none.

        Why this matters: a GGUF that already declares ``rope.scaling.*`` is extended by
        llama.cpp on its own. Writing our own factors on top produce a double scaling,
        whose symptom is garbled output on long prompts. The context page therefore
        needs to know before it offers to "extend" such a model.

        [ZH]
        返回当前模型文件内记录的 RoPE 缩放参数；文件未携带时返回空映射。

        为什么需要它：已经声明 ``rope.scaling.*`` 的 GGUF 会被 llama.cpp 自动扩展。
        在此之上再写一份自己的系数会造成双重缩放，症状是长提示词输出乱码。上下文页
        因此必须先知道这一点，才谈得上"扩展"该模型。
        """
        try:
            path = self._current_model_path()
            if not path:
                return {}
            from gguf_meta import architecture, read_metadata

            meta = read_metadata(path)
            if not meta:
                return {}
            prefix = f"{architecture(meta)}.rope.scaling."
            found = {key[len(prefix):]: value for key, value in meta.items()
                     if str(key).startswith(prefix)}
            return found
        except Exception:  # noqa: BLE001 - metadata is advisory, never fatal
            return {}


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
            # [EN] 0 is the documented "give me the recommendation again" sentinel used by the
            # EN] context page's restore button; any other value must stay above MIN_CONTEXT.
            # [ZH] 0 是上下文页「恢复推荐值」使用的哨兵值；其余取值必须不小于 MIN_CONTEXT。
            if value != 0 and (value < MIN_CONTEXT or value > MAX_CONTEXT_OVERRIDE):
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
        # [EN] Hard ceiling of the context allocation (``inference_config.json:
        # EN] n_ctx_max``). It is now editable from the context page so a user who
        # [EN] picks 256K is not silently cut back to the shipped 128K.
        # [ZH] 上下文分配的硬上限（``inference_config.json: n_ctx_max``）。现在可以从
        # [ZH] 上下文页修改，选 256K 的用户不会被随包的 128K 悄悄截断。
        if "n_ctx_max" in request and request.get("n_ctx_max") is not None:
            try:
                value = int(request["n_ctx_max"])
            except (TypeError, ValueError):
                return {}, _t("inference.invalid_value", field="n_ctx_max",
                              error=request.get("n_ctx_max"))
            if value < MIN_CONTEXT or value > MAX_CONTEXT_OVERRIDE:
                return {}, _t("inference.value_out_of_range", field="n_ctx_max",
                              low=MIN_CONTEXT, high=MAX_CONTEXT_OVERRIDE)
            values["n_ctx_max"] = value
        # [EN] RoPE scaling fields. ``scale`` is validated as a float so a reciprocal
        # [EN] can be derived; ``orig_ctx`` of 0 means "read from the model file".
        # [ZH] RoPE 缩放字段。``scale`` 按浮点校验以便推导倒数；``orig_ctx`` 为 0
        # [ZH] 表示"从模型文件读取"。
        if "rope_type" in request and request.get("rope_type") is not None:
            mode = str(request["rope_type"] or "none").strip().lower()
            if mode not in ("none", "linear", "yarn"):
                return {}, _t("inference.rope_type_invalid", mode=mode)
            values["rope_type"] = mode
        if "rope_scale" in request and request.get("rope_scale") is not None:
            try:
                value = float(request["rope_scale"])
            except (TypeError, ValueError):
                return {}, _t("inference.invalid_value", field="rope_scale",
                              error=request.get("rope_scale"))
            if value < 1.0 or value > 64.0:
                return {}, _t("inference.value_out_of_range", field="rope_scale",
                              low=1, high=64)
            values["rope_scale"] = value
        if "rope_orig_ctx" in request and request.get("rope_orig_ctx") is not None:
            try:
                value = int(request["rope_orig_ctx"])
            except (TypeError, ValueError):
                return {}, _t("inference.invalid_value", field="rope_orig_ctx",
                              error=request.get("rope_orig_ctx"))
            if value < 0 or value > MAX_CONTEXT_OVERRIDE:
                return {}, _t("inference.value_out_of_range", field="rope_orig_ctx",
                              low=0, high=MAX_CONTEXT_OVERRIDE)
            values["rope_orig_ctx"] = value
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
            # [EN] The child entry point is main.py (the module that parses the hidden
            # [EN] flag), not this mixin file.
            # [ZH] 子进程入口是 main.py（解析隐藏参数的那个模块），不是本 mixin 文件。
            command = [sys.executable,
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"),
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
        # [EN] Context ceiling and RoPE scaling (v0.2.6-reader context page).
        # [EN] Two rules keep this predictable for non-technical users:
        # [EN]   1. choosing a window larger than the stored ceiling raises the ceiling,
        # [EN]      so the choice is never silently clamped back;
        # [EN]   2. if the model file already carries its own long-context settings, the
        # [EN]      requested scaling is ignored instead of being stacked on top, because
        # [EN]      double scaling garbles long output.
        # [ZH] 上下文上限与 RoPE 缩放（v0.2.6-reader 上下文页）。
        # [ZH] 两条规则让非技术用户也能预期结果：
        # [ZH]   1. 选的窗口大于已存上限时自动抬高上限，用户的选择不会被悄悄截回；
        # [ZH]   2. 若模型文件自带长上下文设置，则忽略本次缩放而不是叠加 —— 双重缩放会让
        # [ZH]      长文本输出变成乱码。
        previous_rope = {
            "type": str(self.inference_config.rope_type),
            "scale": float(self.inference_config.rope_scale),
            "freq_scale": float(self.inference_config.rope_freq_scale),
            "orig_ctx": int(self.inference_config.rope_orig_ctx),
            "n_ctx_max": int(self.inference_config.n_ctx_max),
        }
        rope_notice = ""
        if "n_ctx" in values:
            requested_ctx = int(values["n_ctx"])
            if requested_ctx <= 0:
                # [EN] Restore path: re-plan from the hardware recommendation and drop the
                # [EN] explicit override, which is exactly "back to the recommended window".
                # [ZH] 恢复路径：按硬件推荐重新规划并清除显式覆盖，即"回到推荐窗口"。
                recommendation = self._recommended_plan()
                cfg.context_length = int(recommendation.n_ctx_max)
                cfg.inference_override = False
                self.inference_config.n_ctx_max = int(recommendation.n_ctx_max)
                self.inference_config.rope_type = "none"
                self.inference_config.rope_scale = 1.0
                self.inference_config.rope_freq_scale = 1.0
                self.inference_config.rope_orig_ctx = 0
                for item in ("n_ctx_max", "rope"):
                    if item not in changed:
                        changed.append(item)
                inference_changed = True
                requested_ctx = 0
            if requested_ctx > int(self.inference_config.n_ctx_max):
                self.inference_config.n_ctx_max = requested_ctx
                changed.append("n_ctx_max")
                inference_changed = True
        if "n_ctx_max" in values:
            ceiling = int(values["n_ctx_max"])
            if ceiling < int(cfg.context_length or 0):
                # [EN] A ceiling below the current window would make the planner
                # [EN] contradict the user's saved choice; clamp the ceiling up and say so.
                # [ZH] 上限低于当前窗口会让规划器与用户已保存的选择矛盾；因此把上限抬到
                # [ZH] 窗口大小，并明确提示。
                ceiling = int(cfg.context_length or 0)
                rope_notice = _t("inference.rope_ceiling_raised")
            if ceiling != int(self.inference_config.n_ctx_max):
                self.inference_config.n_ctx_max = max(1, ceiling)
                changed.append("n_ctx_max")
                inference_changed = True
        if any(key in values for key in ("rope_type", "rope_scale", "rope_orig_ctx")):
            mode = str(values.get("rope_type", previous_rope["type"]) or "none").lower()
            scale = float(values.get("rope_scale", previous_rope["scale"]) or 1.0)
            orig = int(values.get("rope_orig_ctx", previous_rope["orig_ctx"]) or 0)
            embedded = self._model_embedded_rope()
            if mode != "none" and embedded:
                mode = "none"
                rope_notice = _t("inference.rope_embedded_ignored")
            self.inference_config.rope_type = mode
            self.inference_config.rope_scale = max(1.0, scale) if mode != "none" else 1.0
            self.inference_config.rope_freq_scale = (
                1.0 / scale if mode != "none" and scale > 1.0 else 1.0)
            self.inference_config.rope_orig_ctx = max(0, orig) if mode != "none" else 0
            changed.append("rope")
            inference_changed = True
        if "n_gpu_layers" in values or "n_batch" in values or inference_changed:
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
                # [EN] Restore the previous RoPE mode and ceiling as well: a rejected
                # [EN] window must not leave the file describing an extension the
                # [EN] running engine never adopted.
                # [ZH] 同时恢复原 RoPE 模式与上限：被拒绝的窗口不应让配置文件描述一个
                # [ZH] 运行中的引擎从未采用的扩展。
                self.inference_config.rope_type = previous_rope["type"]
                self.inference_config.rope_scale = previous_rope["scale"]
                self.inference_config.rope_freq_scale = previous_rope["freq_scale"]
                self.inference_config.rope_orig_ctx = previous_rope["orig_ctx"]
                self.inference_config.n_ctx_max = previous_rope["n_ctx_max"]
                try:
                    self.inference_config.save()
                except Exception:  # noqa: BLE001 - the returned error is primary
                    log.warning("恢复上下文设置后保存推理配置失败", exc_info=True)
                if "n_ctx" in values and previous_context > 0:
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
                "context": self._context_snapshot(),
                "notice": rope_notice}


