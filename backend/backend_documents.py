# -*- coding: utf-8 -*-
"""
[EN]
Reader backend: document upload, parsing, background jobs and summaries. Split out of the former single-file ``main.py``; the
module contains only the class methods belonging to this responsibility.

[ZH]
Reader 后端：文档上传、解析、后台作业与摘要。自原先的单文件 ``main.py`` 拆出；本模块只包含属于该职责的
类方法。
"""
from __future__ import annotations

from backend_support import *  # noqa: F401,F403 - shared backend support layer


class DocumentMixin:
    _PICK_EXTRA_FIELDS: Dict[str, str] = {"login_check": "login_check_selector"}

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


