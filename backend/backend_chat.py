# -*- coding: utf-8 -*-
"""
[EN]
Reader backend: conversation streaming, context compaction and turn metrics. Split out of the former single-file ``main.py``; the
module contains only the class methods belonging to this responsibility.

[ZH]
Reader 后端：会话流、上下文压缩与轮次指标。自原先的单文件 ``main.py`` 拆出；本模块只包含属于该职责的
类方法。
"""
from __future__ import annotations

from backend_support import *
# [EN] Explicit re-imports: the reverse language-key audit checks each
# [EN] module for its own i18n imports, and star imports are not visible to it.
# [ZH] 显式重新导入：反向语言键审计按文件检查 i18n 导入，星号导入对它不可见。
from i18n import log_user
from i18n import t as _t  # noqa: F401,F403 - shared backend support layer


class ChatMixin:
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


