"""EchoDesk 后端主程序：与前端通过 stdin/stdout 以 JSON 行协议通信。

协议：
  {"action": "chat", "message": "..."}         -> 一行响应 {"type": "assistant|system", "content": ...}
  {"action": "chat_stream", "message": "..."}  -> 多行流式响应：{"type":"delta",...}... {"type":"done",...}
  {"action": "stop"}                           -> 中断当前流式生成
  {"action": "file", "path": "..."}            -> 一行响应
  {"action": "model_download", "name": "..."}  -> 下载模型并热切换，返回一行响应

流式生成在后台线程执行，主循环继续读取 stdin，从而支持 "stop" 中断。
启动时先完成模型加载、会话恢复与预热（warmup），再输出 {"type":"ready",...} 就绪声明；
前端只有读到就绪声明后才会允许用户输入。
"""
import contextlib
import json
import logging
import os
import queue
import sys
import threading
from typing import Any, Dict, Iterator

from version import APP_NAME, APP_VERSION, STUDIO

# 协议走 stdin/stdout，且内容含中文/emoji。某些机器（英文 locale）下 Python
# 默认用 cp1252 编码，写中文会抛 UnicodeEncodeError 直接崩溃（“后端进程已退出”）。
# 统一强制 UTF-8，且用 errors="replace" 兜底，保证任何机器都能正常输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _stream


@contextlib.contextmanager
def _suppress_stdout():
    """临时把 sys.stdout 指向 devnull，吞掉第三方库直接 print 到 stdout 的诊断信息。"""
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
    # 在 import huggingface_hub 相关模块前，就把 HF 端点切换到镜像，
    # 否则 huggingface_hub 默认连 huggingface.co（国内不可达），下载/查询都会超时。
    _cfg = Config.load()
    if _cfg.hf_endpoint and not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = _cfg.hf_endpoint
    from command_handler import CommandHandler
    from conversation import ConversationManager, FILE_INTENT, REPEAT_INTENT
    from download_utils import check_model, download_model, find_best_model
    from file_parser import parse_file_with_wps
    from gguf_meta import resolve_context_window
    from llm_engine import LLMEngine
    from search_engine import SearchEngine
    from summarizer import DocumentSummarizer

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("EchoDesk")

# ---------- 崩溃日志 ----------
# 进程可能因原生代码崩溃（llama.cpp 的 SIGILL/SIGSEGV）等原因无 Python 回溯地直接退出，
# 此时把崩溃原因写入 exe 所在目录的 backend_crash.log，便于在别的机器上排查。
import faulthandler

def _crash_log_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.getcwd()
    return os.path.join(base, "backend_crash.log")


try:
    _crash = open(_crash_log_path(), "a", encoding="utf-8")
    faulthandler.enable(_crash)
    log.info("已启用崩溃日志: %s", _crash_log_path())
except Exception:
    faulthandler.enable()
    _crash = None


def _excepthook(etype, value, tb):
    """未捕获的 Python 异常也写入崩溃日志，避免静默退出。"""
    import traceback
    try:
        with open(_crash_log_path(), "a", encoding="utf-8") as f:
            f.write("\n==== 未捕获异常 ====\n")
            traceback.print_exception(etype, value, tb, file=f)
    except Exception:
        pass
    traceback.print_exception(etype, value, tb)


sys.excepthook = _excepthook

# stdout 写锁：主循环与流式后台线程共用 stdout
_io_lock = threading.Lock()


def _write(response: Dict[str, Any]) -> None:
    with _io_lock:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class Backend:
    """后端服务：负责模型加载、会话恢复、预热与请求分发。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.llm = self._load_model(config)
        self.conv = ConversationManager(config.max_history, config.default_mode)
        self.search = SearchEngine(config.search_results)
        self.summarizer = DocumentSummarizer(
            self.llm, config.summarize, engine_factory=self._make_summary_engine,
        )
        self.commands = CommandHandler(
            self.conv, self.search, self.llm, config, summarizer=self.summarizer,
            file_loader=self.load_file_into_context, summarize_file=self.summarize_current_file,
        )
        self.stream_active = False
        self.stop_event = threading.Event()

    def _make_summary_engine(self, model_path: str):
        """为 summarize.model 指定的专用摘要模型创建引擎（按该模型训练窗口开窗）。"""
        n_ctx = self.config.context_for_model(model_path, resolve_context_window(model_path, 0))
        return LLMEngine(
            model_path, n_ctx,
            n_threads=self.config.n_threads or os.cpu_count() or 4,
            n_batch=self.config.n_batch, n_gpu_layers=self.config.n_gpu_layers,
        )

    @staticmethod
    def _load_model(config: Config) -> LLMEngine:
        """检查本地模型，缺失时自动下载，然后加载（含线程/GPU 自动配置）。

        下载进度打到 stderr（不受 _suppress_stdout 影响），前端连接阶段实时显示。
        上下文窗口按"所选模型"决定：取配置值、模型训练窗口与配置上限三者的最小值。
        """
        model_path = check_model(config.model_path)
        if not model_path:
            log.warning("模型未找到，开始下载 %s ...", config.model_path)

            # 用模糊匹配解析仓库里真实存在的文件名（避免文件名对不上导致 "cannot locate on the Hub"）
            try:
                target, _size = find_best_model(config.model_repo, config.model_path, hf_endpoint=config.hf_endpoint)
            except Exception as e:
                raise RuntimeError(f"无法查询仓库 {config.model_repo}（{e}）。请联网或把模型文件放入 models/ 目录。") from e

            def _progress(done: int, total: int) -> None:
                pct = int(done * 100 / total) if total > 0 else 0
                print(
                    f"📥 正在下载 {target} ... {pct}% "
                    f"({done / 1048576:.1f} MB / {total / 1048576:.1f} MB)",
                    file=sys.stderr, flush=True,
                )

            model_path = download_model(
                target, config.model_repo,
                hf_endpoint=config.hf_endpoint, on_progress=_progress,
            )
            # 下载的是仓库真实文件名，同步配置以免下次仍用不存在的名字
            if target != config.model_path:
                config.model_path = target
                for prof in config.models.values():
                    if prof.get("repo") == config.model_repo:
                        prof["path"] = target
        n_threads = config.n_threads or os.cpu_count() or 4
        # 按模型决定上下文窗口，并在重载/加载时输出窗口大小与 KV Cache 占用
        train_ctx = resolve_context_window(model_path, 0)
        n_ctx = config.context_for_model(model_path, train_ctx)
        log.info(
            "加载模型: %s (ctx=%d, 训练窗口=%s, threads=%d, batch=%d, gpu_layers=%d)",
            model_path, n_ctx, train_ctx or "未知", n_threads, config.n_batch, config.n_gpu_layers,
        )
        return LLMEngine(
            model_path, n_ctx,
            n_threads=n_threads, n_batch=config.n_batch, n_gpu_layers=config.n_gpu_layers,
        )

    def restore_session(self) -> int:
        """恢复上次会话，返回恢复的对话轮数；若上次使用的模型与当前不同则记录提醒。"""
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

    def warmup(self) -> None:
        """用当前系统提示词做一次极小推理，预热 KV 缓存，缩短首次回复延迟。"""
        if not self.config.warmup:
            return
        log.info("模型预热中...")
        self.llm.chat_complete(self.conv.build_messages("ping"), max_tokens=1)
        log.info("模型预热完成")

    # ---------- 请求分发 ----------

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """处理单行响应的请求。"""
        action = request.get("action")
        if action == "chat":
            return self._handle_chat(request.get("message", ""))
        if action == "file":
            return self._handle_file(request.get("path", ""))
        if action == "model_download":
            return self._handle_model_download(request.get("name", ""))
        if action == "context":
            return self._handle_context()
        return {"type": "system", "content": "未知动作"}

    def _handle_context(self) -> Dict[str, Any]:
        """返回当前上下文占用（used/limit，供前端工具栏实时显示）。"""
        response: Dict[str, Any] = {"type": "context"}
        self._attach_context(response)
        return response

    def _attach_context(self, response: Dict[str, Any]) -> None:
        """把当前上下文占用附加到响应上，前端据此实时刷新（/clear、/undo 后自然减少）。"""
        try:
            messages = [{"role": "system", "content": self.conv.get_system_prompt()}] + self.conv.history
            response["used"] = self._estimate_tokens(messages)
            response["limit"] = int(self.llm.n_ctx * 0.8)
        except Exception:
            pass

    def stream(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """流式对话：逐个产出 delta 行，最后产出 done 行（支持 stop 中断）。"""
        if request.get("action") == "model_download":
            yield from self._stream_model_download(request.get("name", ""))
            return

        user_msg = request.get("message", "")
        if user_msg.startswith("/"):
            yield self._handle_chat(user_msg)
            return

        search_context = self._search_if_enabled(user_msg)
        include_file = bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)
        messages = self.conv.build_messages(user_msg, search_context, include_file=include_file)

        parts: list = []
        try:
            for delta in self.llm.chat_stream(messages, temperature=self.commands.temperature, stop_event=self.stop_event):
                if self.stop_event.is_set():
                    break
                parts.append(delta)
                yield {"type": "delta", "content": delta}
            full = "".join(parts)
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", full)
            self._compact_history()
            done: Dict[str, Any] = {"type": "done", "content": full, "stopped": self.stop_event.is_set()}
            # 扩展上下文后速度明显下降时给出提示（窗口越大 KV Cache 越大、越慢）
            if self.config.speed_hint and not self.stop_event.is_set():
                hint = self.llm.speed_hint(self.config.speed_min_tps)
                if hint:
                    done["content"] = f"{full}\n\n{hint}" if full else hint
                    _write({"type": "delta", "content": f"\n\n{hint}"})
            self._attach_context(done)  # 附带当前上下文占用，供前端刷新
            yield done
        except Exception as e:
            log.exception("流式推理失败")
            yield {"type": "error", "content": f"推理失败: {e}"}

    # ---------- 具体动作 ----------

    def _handle_chat(self, user_msg: str) -> Dict[str, Any]:
        if user_msg.startswith("/"):
            content, action = self.commands.handle(user_msg)
            response = {"type": "system", "content": content}
            if action:
                self._merge_action(response, action)
            return response

        # 意图短路：用户问"我刚才说了什么"时直接引用最近输入，避免小模型混淆视角
        last_user = self.conv.last_user_message()
        if last_user and REPEAT_INTENT.search(user_msg):
            answer = f"您刚才说的是：\n{last_user}"
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", answer)
            self._compact_history()
            return {"type": "assistant", "content": answer}

        search_context = self._search_if_enabled(user_msg)
        include_file = bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)
        messages = self.conv.build_messages(user_msg, search_context, include_file=include_file)
        try:
            response = self.llm.chat_complete(messages, temperature=self.commands.temperature)
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", response)
            self._compact_history()
            return {"type": "assistant", "content": response}
        except Exception as e:
            log.exception("推理失败")
            return {"type": "system", "content": f"推理失败: {e}"}

    def _handle_file(self, filepath: str) -> Dict[str, Any]:
        if not filepath:
            return {"type": "system", "content": "请提供文件路径"}
        return {"type": "system", "content": self.load_file_into_context(filepath)}

    def load_file_into_context(self, filepath: str) -> str:
        """解析文件并入上下文；全文超出窗口一定比例时自动分段摘要后注入。

        返回给用户看的反馈文本（成功或失败都会说明原因）。
        """
        content = parse_file_with_wps(filepath, self.config.file_max_chars)
        if content.startswith(("WPS读取失败", "解析失败", "文件内容为空", "不支持的文件类型")):
            return f"❌ {content}"
        self.conv.set_file(filepath, content)
        name = os.path.basename(filepath)
        try:
            tokens = self.llm.count_tokens(content)
        except Exception:
            tokens = len(content) // 2
        if self.summarizer.should_use_summary(content, self.llm.n_ctx):
            log.info(
                "文件 %s 约 %d tokens，超过窗口 %d 的 %.0f%%：转入分段摘要",
                name, tokens, self.llm.n_ctx, self.config.summarize.trigger_ratio * 100,
            )
            head = (
                f"📄 已加载 {name}：约 {tokens} tokens，超过窗口 {self.llm.n_ctx} 的 "
                f"{self.config.summarize.trigger_ratio:.0%}，将先分段摘要再注入（避免直接截断丢信息）。\n"
            )
            return head + self.summarize_current_file(force=True, source_name=name, trigger="自动")
        return f"✅ 已加载文件: {name} ({len(content)} 字符，约 {tokens} tokens)"

    def summarize_current_file(self, force: bool = True, source_name: str = "", trigger: str = "手动") -> str:
        """对当前已加载文件做分段摘要并替换注入内容（供自动触发与 /summarize 调用）。"""
        if not self.conv.file_content:
            return "❌ 当前没有已加载的文件，请先用 /file 上传"
        name = source_name or os.path.basename(self.conv.file_name or "")

        def _progress(index: int, total: int, label: str) -> None:
            # 进度打到 stderr：前端在对话框里实时显示，长文档摘要不会"卡住无反应"
            print(f"📝 {label} ({index}/{total})", file=sys.stderr, flush=True)

        result = self.summarizer.summarize(
            self.conv.file_content, source_name=name, force=force, on_progress=_progress,
        )
        if not result.summarized:
            return f"ℹ️ 文件较短（{result.origin_chars} 字符），无需分段摘要，已按原文注入。"
        self.conv.set_file_summary(
            result.text,
            {"chunks": result.chunk_count, "origin_chars": result.origin_chars, "pointer": result.pointer},
        )
        self.llm.clear_cache()  # 注入内容结构变化，旧 KV 缓存失效
        summary_tokens = self.llm.count_tokens(result.text)
        pointer = result.pointer
        shown = pointer[:160] + ("…" if len(pointer) > 160 else "")
        return (
            f"✅ {trigger}分段摘要完成: {name}\n"
            f"• 原文 {result.origin_chars} 字符 → {result.chunk_count} 段，摘要约 {summary_tokens} tokens\n"
            f"• 已用摘要替代全文注入上下文（提问时自动带上）\n"
            f"• 已保留原文指针：{shown}"
        )

    def _handle_model_download(self, name: str) -> Dict[str, Any]:
        """用户已同意后下载模型并热切换。"""
        if not name:
            return {"type": "system", "content": "缺少模型名称"}
        try:
            path = download_model(name, self.config.model_repo, hf_endpoint=self.config.hf_endpoint)
        except Exception as e:
            return {"type": "system", "content": f"❌ 模型下载失败: {e}"}
        try:
            self.llm.reload(path)
        except Exception as e:
            return {"type": "system", "content": f"❌ 下载完成但加载失败: {e}"}
        self.config.model_path = name
        self.config.save()
        return {"type": "system", "content": f"✅ 模型已下载并加载: {name}"}

    def _stream_model_download(self, name: str) -> Iterator[Dict[str, Any]]:
        """流式下载模型：后台线程下载，逐条 yield 进度，最后 yield done/error。

        进度来自 download_model 的 on_progress 回调（字节 → 百分比）。
        """
        if not name:
            yield {"type": "error", "content": "缺少模型名称"}
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
                yield {"type": "delta", "content": f"📥 正在下载 '{name}'... {pct}% ({self._fmt_mb(a)} / {self._fmt_mb(total)})"}
            elif kind == "path":
                break
            else:  # error
                yield {"type": "error", "content": f"❌ 模型下载失败: {a}"}
                return

        # 下载完成 → 加载并热切换
        try:
            self.llm.reload(a)
        except Exception as e:
            yield {"type": "error", "content": f"❌ 下载完成但加载失败: {e}"}
            return
        self.config.model_path = name
        self.config.save()
        yield {"type": "done", "content": f"✅ 模型已下载并加载: {name}"}

    def _estimate_tokens(self, messages) -> int:
        """用模型分词器估算消息 token 数；失败时按字符数粗略估算。"""
        try:
            return sum(len(self.llm.llm.tokenize(m["content"].encode("utf-8"))) for m in messages)
        except Exception:
            return sum(len(m["content"]) for m in messages) // 2

    @staticmethod
    def _fmt_mb(n: int) -> str:
        """把字节数格式化为可读的 MB 字符串，用于下载进度展示。"""
        return f"{n / 1048576:.1f} MB"

    def _compact_history(self) -> None:
        """上下文长度管理：当上下文超过模型 context_length 的 80% 时，
        保留最近约 20% 的上下文原样，把更早的历史交给模型压缩为摘要。
        """
        conv = self.conv
        if not conv.history:
            return
        limit = int(self.llm.n_ctx * 0.8)
        try:
            current = [{"role": "system", "content": conv.get_system_prompt()}] + conv.history
            total = self._estimate_tokens(current)
        except Exception:
            return
        if total <= limit:
            return

        # 从历史末尾向前累计，保留最新约 20% 上下文原样
        keep_tokens = max(1, int(limit * 0.2))
        keep_count = 0
        acc = 0
        for msg in reversed(conv.history):
            acc += self._estimate_tokens([msg])
            keep_count += 1
            if acc >= keep_tokens:
                break
        if keep_count >= len(conv.history):
            return  # 最新内容已占满，无法压缩（如单轮超大消息）
        dropped = conv.history[: len(conv.history) - keep_count]
        conv.history = conv.history[len(dropped):]

        text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:300]}" for m in dropped
        )[:4000]
        summary = ""
        try:
            summary = self.llm.chat_complete(
                [
                    {"role": "system", "content": "你是对话压缩器。请用不超过150字的要点总结这段对话，保留用户的需求、关键决定和背景信息。"},
                    {"role": "user", "content": text},
                ],
                temperature=0.3,
                max_tokens=180,
            ).strip()
        except Exception:
            log.warning("历史压缩失败，已直接裁剪", exc_info=True)
        if summary:
            conv.summary = f"{conv.summary}\n{summary}".strip()
        log.info(
            "上下文 %d/%d tokens 超限：保留最新 %d 条，压缩 %d 条为摘要（%d 字）",
            total, limit, keep_count, len(dropped), len(summary),
        )

    def _search_if_enabled(self, query: str) -> str:
        if not self.commands.search_enabled:
            return ""
        result = self.search.search(query)
        if result and "未找到" not in result and "搜索失败" not in result:
            return result
        return ""

    @staticmethod
    def _merge_action(response: Dict[str, Any], action: Dict[str, Any]) -> None:
        """把动作字段并入响应（不覆盖 type）。"""
        for key, value in action.items():
            if key != "type":
                response[key] = value


def main() -> None:
    try:
        config = Config.load()
        with _suppress_stdout():
            backend = Backend(config)
            restored = backend.restore_session()
            backend.warmup()
    except Exception as e:
        log.exception("后端初始化失败")
        # 向 stdout 输出一条错误响应，前端首条请求即可读到
        print(json.dumps({"type": "system", "content": f"后端初始化失败: {e}"}, ensure_ascii=False), flush=True)
        return

    log.info("%s %s 就绪 — Powered by %s", APP_NAME, APP_VERSION, STUDIO)
    # 就绪声明：预热等准备工作全部完成后才输出，前端据此放行输入；附带恢复的历史消息供前端展示
    _write({"type": "ready", "version": APP_VERSION, "studio": STUDIO, "restored": restored,
            "history": backend.conv.history})

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
        # 兼容前端写入 stdin 时可能带上的 UTF-8 BOM
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff").strip()
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log.warning("收到非法 JSON: %r", line)
            continue

        action = request.get("action")

        # 流式生成期间：指令类请求（以 / 开头）与 stop 允许；新的对话/文件请求提示忙碌
        if backend.stream_active and action != "stop":
            is_command = isinstance(request.get("message"), str) and request.get("message", "").startswith("/")
            if not is_command:
                _write({"type": "system", "content": "⏳ 上一条回复仍在生成，请先停止（⏹ 按钮或 /stop）或等待完成"})
                continue

        if action == "chat_stream":
            backend.stop_event.clear()
            backend.stream_active = True
            threading.Thread(target=_stream_worker, args=(request,), daemon=True).start()
        elif action == "model_download":
            # 流式下载：进度实时输出，结束后热切换模型
            backend.stop_event.clear()
            backend.stream_active = True
            threading.Thread(target=_stream_worker, args=(request,), daemon=True).start()
        elif action == "stop":
            if backend.stream_active:
                backend.stop_event.set()
                _write({"type": "system", "content": "⏹ 已发送停止请求"})
            else:
                _write({"type": "system", "content": "当前没有正在生成的内容"})
        else:
            response = backend.handle(request)
            backend._attach_context(response)  # 附带上下文占用，/clear、/undo 后实时减少
            _write(response)
            backend.save_session()
            if response.get("action") == "exit":
                log.info("收到退出指令，进程退出")
                return


if __name__ == "__main__":
    main()
