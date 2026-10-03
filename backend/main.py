"""EchoDesk 后端主程序：与前端通过 stdin/stdout 以 JSON 行协议通信。

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
  "回答"其实是思考内容（模型的起始标签写在提示词里，只有结束标签可见，如
  DeepSeek-R1），前端应把已累积的回答文本搬到思考区；
* ``done`` 行额外带 ``reasoning``（思考全文）、``reasoning_unclosed: true``
  （标签未闭合，通常意味着 token 预算被思考用完、答案还没开始）。

**流式心跳与运行指标（v0.2.1 优化已知问题-2 任务五/七）**：

* ``{"type":"progress","channel":"heartbeat","stats":{...},"waiting_first_token":bool}``
  —— **心跳**：模型还在算但暂时没有新 token（prefill 阶段可能几十秒）。
  前端据此显示"正在处理输入 / 等待首 token"，而不是让界面看起来像卡死。
  心跳由引擎的读者线程产生（见 ``llm_engine._iter_with_heartbeat``），
  间隔取 ``config.stream_heartbeat_seconds``（默认 1.5 秒）。
* ``done`` 行额外带 ``stats``：后端实测的运行指标（首 token 延迟、输出 token 数、
  token/s、总耗时、是否估算），供状态区显示。字段定义见 ``llm_engine.StreamStats``。

没有 ``channel`` 字段的 delta 行沿用旧语义（进度/摘要等非对话文本），前端按原样处理。

**无模型就绪（v0.2.1 综合优化 任务一）**：
启动时若本地缺少模型，后端**不再阻塞等待、也不再退出**，而是以"无模型就绪"状态继续运行：
初始化一个 ``llm`` 留空的 Backend，主循环照常服务。行为边界见 :meth:`Backend.model_state`：
推理类请求返回引导文案（不崩溃），文件解析 / 技能市场 / 设置读写 / 模型市场搜索与下载 /
会话历史查看等不依赖模型的功能**照常可用**。用户在设置里下载模型、或把 ``.gguf`` 手动放进
模型文件夹后，后端**原地热加载**（同一个引擎对象，无需重启进程）。

启动顺序：``need_download``（**非阻塞**，仅告知缺哪个模型，带 ``blocking: false``）→
``ready``（带 ``model_ready: false`` 与 ``model_missing`` 详情）。旧行为"用户拒绝即退出"
已移除：退出只由用户主动触发（前端发 ``exit``）。
"""
import contextlib
import json
import logging
import os
import queue
import shutil
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from reasoning import ANSWER as _CH_ANSWER
from reasoning import REASONING as _CH_REASONING
from reasoning import RESET as _CH_RESET
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
    from config import AutomationConfig, Config
    # 在 import huggingface_hub 相关模块前，就把 HF 端点切换到镜像，
    # 否则 huggingface_hub 默认连 huggingface.co（国内不可达），下载/查询都会超时。
    _cfg = Config.load()
    if _cfg.hf_endpoint and not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = _cfg.hf_endpoint
    from command_handler import CommandHandler
    from conversation import ConversationManager, FILE_INTENT, REPEAT_INTENT
    from download_utils import check_model, download_model, find_best_model
    from file_parser import parse_file_with_wps
    from gguf_meta import format_bytes as _format_bytes
    from gguf_meta import resolve_context_window
    from llm_engine import LazyLLMEngine, LLMEngine, NoModelError, StreamStats
    # 心跳通道标记（任务五）：引擎在 prefill 阶段产出它，见 llm_engine._iter_with_heartbeat
    from llm_engine import HEARTBEAT as _CH_HEARTBEAT
    from search_engine import SearchEngine
    from summarizer import DocumentSummarizer

# 多语言（任务三）：必须在 Config.load 之后导入，避免 i18n 反向依赖 config 时成环
from i18n import get as _i18n
from i18n import t as _t

# Emoji 降级（修复六）与目录布局（修复七）
import emoji as emoji_mod
from config import program_dir, program_root as user_data_root

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


def _preset_list() -> List[Dict[str, Any]]:
    """生成参数预设清单（修复四 方向六）。"""
    from model_profile import preset_menu

    return preset_menu()


def _task_template_list() -> List[Dict[str, Any]]:
    """任务型提示词模板清单（修复四 方向一）。"""
    from prompts import public_list

    return public_list()


def _recommendation() -> Dict[str, Any]:
    """按本机硬件推荐模型规模与生成参数（修复四 方向六）。"""
    from model_profile import _read_machine_memory, recommend_config

    ram, cores = _read_machine_memory()
    return recommend_config(ram, cores)


def _profile_params_text(name: str) -> str:
    """按文件名推断参数量文本（0.6B / 1.7B）；取不到返回空串。

    复用 `model_profile._fmt_params`（与能力画像同一套格式化规则），
    避免在这里再写一份"多少算 B、保留几位小数"的判断。
    """
    try:
        from model_profile import _fmt_params, params_from_name
        return _fmt_params(params_from_name(name))
    except Exception:  # noqa: BLE001 - 只是展示字段，取不到就不显示
        return ""


def _profile_quant_text(name: str) -> str:
    """按文件名推断量化文本（Q6_K / Q4_K_M）；取不到返回空串。"""
    try:
        from model_hub import guess_quantization
        return guess_quantization(name)
    except Exception:  # noqa: BLE001
        return ""


def _write(response: Dict[str, Any]) -> None:
    with _io_lock:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class Backend:
    """后端服务：负责模型加载、会话恢复、预热与请求分发。"""

    def __init__(self, config: Config, model_path: str = "") -> None:
        self.config = config
        # 引擎：给了模型就是真引擎；没给就是一个"空着、可原地加载"的引擎（任务一）。
        # 刻意**保持同一个对象**（见 llm_engine.LazyLLMEngine 的说明）：热加载后
        # command_handler / summarizer 里的引用不用重新接线，也就不会出现
        # "加载成功了但还在用旧对象"这种静默错误。
        self.llm = self._build_engine(config, model_path)
        self.conv = ConversationManager(config.max_history, config.default_mode)
        # 思考内容写入历史的两个开关（v0.2.1 模型思考内容分离）：是否持久化、
        # 每条保留多少字符。放在这里而不是 ConversationManager 内部，是因为
        # 这两个值来自 config.json，而会话管理器本身不依赖配置模块。
        _reasoning_cfg = getattr(config, "reasoning", None)
        self.conv.persist_reasoning = bool(getattr(_reasoning_cfg, "persist", True))
        self.conv.reasoning_max_chars = int(getattr(_reasoning_cfg, "history_max_chars", 8000) or 0)
        self.search = SearchEngine(config.search_results)
        self.summarizer = DocumentSummarizer(
            self.llm, config.summarize, engine_factory=self._make_summary_engine,
        )
        self.commands = CommandHandler(
            self.conv, self.search, self.llm, config, summarizer=self.summarizer,
            file_loader=self.load_file_into_context, summarize_file=self.summarize_current_file,
        )
        # 浏览器自动化引擎（Playwright）：惰性创建，未使用时不加载
        self._automation = None
        # 技能市场：同样惰性创建
        self._market = None
        # 模型选择栏（新增四）：惰性创建
        self._model_hub = None
        # 技能运行期加载器（任务二）：同样惰性创建，第一次用到时才扫描目录
        self._skills = None
        # 修复四：分段摘要缓存（方向八）与"手动压缩上下文"回调接线
        self._summary_cache = None
        self._capability_cache = None
        self._capability_key = None
        self.commands.compact_provider = self.compact_context
        self.commands.profile_provider = self.model_capability
        self.commands.cache_clear_provider = self.clear_summary_cache
        self.stream_active = False
        self.stop_event = threading.Event()
        # 搜索链路可观测状态（优化已知问题-2 任务二）：上一次搜索的结果对象与
        # "为什么没用上"的说明。任务书要求逐环节排查，所以状态要留着可查。
        self._last_search = None
        self._last_search_note = ""

    def clear_summary_cache(self) -> int:
        """清空分段摘要缓存并返回清掉的条目数（供 /clearcache 与动作接口使用）。"""
        cache = self.summary_cache()
        return cache.clear() if cache is not None else 0

    # ---------- 无模型就绪 / 热加载（v0.2.1 综合优化 任务一、二、五） ----------

    @property
    def model_ready(self) -> bool:
        """当前是否有可用模型（推理类功能的前提）。"""
        return bool(getattr(self.llm, "ready", True))

    def models_dir(self) -> str:
        """模型文件夹的实际路径（引导文案里必须给真实路径，不能用占位符）。"""
        try:
            return self.model_hub().models_dir
        except Exception:
            return os.path.abspath("models")

    def model_state(self) -> Dict[str, Any]:
        """机器可读的模型状态，供前端区分"后端已就绪"与"推理可用"（任务一）。

        ``ready=false`` 时前端应保持界面可交互、把状态栏显示为"无模型"，
        并按 ``guide`` 引导用户去模型市场或手动放置模型文件。
        """
        ready = self.model_ready
        return {
            "ready": ready,
            "model": os.path.basename(str(getattr(self.llm, "model_path", "") or "")),
            "models_dir": self.models_dir(),
            "n_ctx": int(getattr(self.llm, "n_ctx", 0) or 0),
            "guide": "" if ready else self.guide_text(),
        }

    def guide_text(self) -> str:
        """无模型时的引导文案：说明现状 + 三条可行路径 + 模型文件夹真实路径（任务二）。"""
        return _t("sys.no_model_guide", dir=self.models_dir())

    def load_model(self, name_or_path: str = "", *,
                   profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """热加载模型（任务五）：下载完成、扫描到新模型、用户手动切换都走这里。

        返回 ``{ok, name, error, state}``。失败时**保持无模型状态**并把原因如实带回
        （文件损坏 / 格式不支持 / 内存不足），用户可以直接重试，不用重启进程。

        ``profile``（可选）：``config.models`` 里那一档的档案，用来套用该档自己的
        ``context_length`` 与 ``temperature``（设置页的"模型档位"切换走这条路，
        与命令行 ``/model <别名>`` 的语义一致）。不给就按全局配置开窗。
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
            n_ctx = self.config.context_for_model(path, train_ctx, requested)
            self.llm.load(path, n_ctx)
        except Exception as e:  # noqa: BLE001 - 损坏/不支持/内存不足都在这里
            log.exception("热加载模型失败: %s", os.path.basename(path))
            return {"ok": False, "name": os.path.basename(path),
                    "error": _t("sys.model_load_failed", name=os.path.basename(path), error=e),
                    "state": self.model_state()}
        name = os.path.basename(path)
        self.config.model_path = name
        # 档位自带温度时才覆盖（用户手动改过温度也以档案为准：档案就是这一档的推荐值）
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
        # 换模型后旧 KV 缓存与历史摘要都不再适用
        self.llm.clear_cache()
        try:
            self.warmup()
        except Exception:
            log.warning("热加载后的预热失败（不影响使用）", exc_info=True)
        log.info("模型热加载完成: %s（ctx=%d）", name, int(getattr(self.llm, "n_ctx", 0) or 0))
        return {"ok": True, "name": name, "error": "", "state": self.model_state()}

    def scan_models(self, auto_load: bool = True) -> Dict[str, Any]:
        """重新扫描模型文件夹（任务二：手动放置 ``.gguf`` 后能被识别）。

        扫描结果来自 ``model_hub.registry()``（它会把手工放进目录的文件一并登记）。
        只有一个模型时自动加载并提示；多个模型时只回报清单，由用户选择。
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

    # ---------- 修复四：模型能力画像 / 摘要缓存 ----------

    def capability(self):
        """当前模型的能力画像（修复四 方向六/七）。

        缓存按「模型路径 + 上下文窗口 + 线程数 + GPU 层数」做键：模型切换、
        重载、改窗口之后键会变，缓存自动失效。这样比在每个重载点手写
        invalidate 更不容易漏（漏一处就会显示上一个模型的参数量）。
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
        """让能力画像缓存立即失效（一般不需要手动调，缓存键会自己变）。"""
        self._capability_cache = None
        self._capability_key = None

    def model_capability(self) -> Dict[str, Any]:
        """机器可读的能力画像（供 /status 与前端状态区使用）。"""
        try:
            return self.capability().public()
        except Exception:
            log.debug("能力画像生成失败", exc_info=True)
            return {}

    def summary_cache(self):
        """分段摘要缓存（惰性创建；config.summary_cache=False 时返回 None）。"""
        if not bool(getattr(self.config, "summary_cache", True)):
            return None
        if self._summary_cache is None:
            from model_profile import SummaryCache

            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "summary_cache.json")
            self._summary_cache = SummaryCache(path)
        return self._summary_cache

    def skills(self):
        """按需创建技能加载器，并把它接到指令表 / 文件解析 / 自动化钩子上（任务二 2.2）。"""
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
        """启动时/重载时加载全部插件；任何失败都被隔离，不影响后端启动。"""
        try:
            return self.skills().load_all()
        except Exception as e:  # noqa: BLE001
            log.exception("技能加载失败（已忽略，后端继续启动）")
            return {"total": 0, "loaded": 0, "failed": 0, "skipped": 0, "error": str(e)}

    def model_hub(self):
        """按需创建模型中心（新增四）。"""
        if self._model_hub is None:
            from model_hub import ModelHub

            self._model_hub = ModelHub(self.config.model_hub, app_config=self.config)
        else:
            self._model_hub.cfg = self.config.model_hub
        return self._model_hub

    def market(self):
        """按需创建技能市场（避免未使用市场时也加载网络相关依赖）。"""
        if self._market is None:
            from marketplace import Marketplace

            self._market = Marketplace(self.config.marketplace)
        else:
            self._market.cfg = self.config.marketplace
        return self._market

    def automation(self):
        """按需创建自动化引擎（避免未使用自动化时也加载 Playwright）。"""
        if self._automation is None:
            from automation import AutomationEngine

            self._automation = AutomationEngine(self.config.automation, llm_engine=self.llm)
            # 技能自动化钩子（任务二 2.3）：把插件的 after_task / after_download 等挂上来
            try:
                self._automation.hook_dispatcher = self.skills().fire_hook
            except Exception:
                log.warning("注入技能自动化钩子失败（自动化仍可正常使用）", exc_info=True)
            # 上次进程被强杀时可能残留下载临时文件，这里清理一次
            # （与 PRIVACY.md 中"程序异常退出时残留的临时目录会在下次启动时清理"一致）。
            try:
                removed = self._automation.cleanup_temp()
                if removed:
                    log.info("已清理上次残留的自动化临时文件: %d 项", removed)
            except Exception:
                log.warning("启动清理自动化临时目录失败", exc_info=True)
        else:
            self._automation.cfg = self.config.automation
        return self._automation

    def _make_summary_engine(self, model_path: str):
        """为 summarize.model 指定的专用摘要模型创建引擎（按该模型训练窗口开窗）。"""
        return self._build_engine(self.config, model_path)

    @staticmethod
    def _build_engine(config: Config, model_path: str):
        """按"所选模型"的上下文窗口创建推理引擎。

        窗口取配置值、模型训练窗口（GGUF 元数据）与 config.max_context 三者的最小值；
        加载时由 LLMEngine 输出窗口大小与 KV Cache 占用日志。

        ``model_path`` 为空时返回 :class:`LazyLLMEngine`（空引擎）：这是"无模型就绪"
        的载体，后端照常启动，等模型到位后由 :meth:`Backend.load_model` **原地加载**
        （任务一 / 任务五）。
        """
        n_threads = config.n_threads or os.cpu_count() or 4
        path = str(model_path or "").strip()
        if not path:
            log.info("未找到可用模型：进入无模型就绪状态（推理类请求将返回引导，其余功能正常）")
            return LazyLLMEngine(
                int(config.context_length or 8192), n_threads=n_threads,
                n_batch=config.n_batch, n_gpu_layers=config.n_gpu_layers,
                reasoning=getattr(config, "reasoning", None),
            )
        train_ctx = resolve_context_window(path, 0)
        n_ctx = config.context_for_model(path, train_ctx)
        log.info(
            "加载模型: %s (ctx=%d, 训练窗口=%s, threads=%d, batch=%d, gpu_layers=%d)",
            path, n_ctx, train_ctx or "未知", n_threads, config.n_batch, config.n_gpu_layers,
        )
        return LLMEngine(
            path, n_ctx,
            n_threads=n_threads, n_batch=config.n_batch, n_gpu_layers=config.n_gpu_layers,
            reasoning=getattr(config, "reasoning", None),
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

    def touch_model_usage(self) -> None:
        """记录当前模型的"最后使用时间"（新增四：模型库列表要显示该信息）。"""
        try:
            self.model_hub().touch(os.path.basename(self.config.model_path or ""))
        except Exception:
            log.debug("更新模型最后使用时间失败", exc_info=True)

    def warmup(self) -> None:
        """用当前系统提示词做一次极小推理，预热 KV 缓存，缩短首次回复延迟。

        无模型时直接跳过：预热的意义是"第一次回复更快"，没有模型就无从谈起，
        不能让它把启动流程拖住（任务一：缺失模型时后端必须照常就绪）。
        """
        if not self.config.warmup:
            return
        if not self.model_ready:
            log.info("无模型，跳过预热")
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
            # 是否"已启用"由配置决定（安全协议未勾选时 enabled 一定为 False）
            status["enabled"] = bool(
                self.config.automation.enabled and self.config.automation.safety_accepted
            )
            return {"type": "system", "content": "", "automation": status}
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
        # ---------- 模型选择栏（新增四） ----------
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
        # ---------- 无模型就绪 / 热加载（v0.2.1 综合优化 任务一、二、五） ----------
        if action == "model_state":
            return {"type": "system", "content": "", "model_state": self.model_state()}
        if action == "model_scan":
            return self._handle_model_scan(request)
        if action == "model_load":
            return self._handle_model_load(request)
        # ---------- 模型档位（多档默认模型的设置页入口） ----------
        if action == "model_profiles":
            return {"type": "system", "content": "", "model_profiles": self.profiles_state()}
        if action == "model_profile_apply":
            return self._handle_model_profile_apply(request)
        # ---------- 多语言（任务三 3.4） ----------
        if action == "set_language":
            return self._handle_set_language(request)
        if action == "language":
            return self._handle_language()
        # ---------- 指令面板数据源（任务二 2.6） ----------
        if action == "commands":
            return {"type": "system", "content": "", "commands": self.commands.all_commands()}
        # ---------- 修复四：模型能力与上下文管理 ----------
        if action == "capability":
            return {"type": "system", "content": "", "capability": self.model_capability(),
                    "presets": _preset_list(),
                    "task_templates": _task_template_list(),
                    "recommendation": _recommendation(),
                    "summary_cache": (self.summary_cache().stats() if self.summary_cache() else {}),
                    "context": self._context_snapshot(),
                    "settings": {
                        "preset": self.config.preset,
                        "context_mode": self.config.context_mode,
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
        # ---------- 技能与本地插件（任务二 / 任务二·补） ----------
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
            # 修复一：批量确认框的数据源（只列"该问且还没答复过"的插件）
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
        # ---------- 自动更新（任务五 方案 A：只检查与提示） ----------
        if action == "update_check":
            return self._handle_update_check(request)
        return {"type": "system", "content": _t("sys.unknown_action")}

    # ---------- 多语言（任务三 3.4） ----------

    def _handle_set_language(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """切换后端语言：影响系统消息、错误提示与指令帮助。

        选择结果由前端写入 ui_state.json（任务书 3.4 持久化要求），
        后端只在本次运行内生效，避免为了切语言去改写 config.json。
        """
        locale = str(request.get("locale") or "auto")
        effective = _i18n().set_locale(locale)
        log.info("界面语言切换为 %s（请求 %s）", effective, locale)
        info = _i18n().info()
        # 语言变化后指令帮助与技能面板标题都要跟着变
        return {"type": "system", "content": _t("sys.language_switched", locale=effective),
                "i18n": info, "commands": self.commands.all_commands()}

    def _handle_language(self) -> Dict[str, Any]:
        return {"type": "system", "content": "", "i18n": _i18n().info()}

    # ---------- 技能与本地插件（任务二 / 任务二·补） ----------

    def _handle_skills_reload(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """热重载插件（任务书 2B.5）：无需重启即可看到 skills_dev/ 里的改动。"""
        try:
            summary = self.skills().reload_all()
        except Exception as e:  # noqa: BLE001
            log.exception("重载插件失败")
            return {"type": "system", "content": _t("err.skill_failed", error=e)}
        text = _t("skill.reload_summary", loaded=summary.get("loaded", 0),
                  total=summary.get("total", 0), failed=summary.get("failed", 0))
        return {"type": "system", "content": text, "skills": self.skills().status(),
                "commands": self.commands.all_commands(), "summary": summary}

    def _handle_skills_dev_mode(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """开发者模式开关（修复一：必须双向可用）。

        关闭时要做四件事，缺一件用户就会觉得"没关掉"：
        1. 记下"用户显式设置过"，否则 dev_mode 属性会因 ``skills_dev/`` 里仍有插件
           而自动重新变成 True（这是原来"一旦开启就关不掉"的根因）；
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
            # 开启时重新发现，让 skills_dev/ 里的插件立刻生效
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
        """批量记录本地插件的信任选择（修复一：一次汇总确认，不再逐个弹窗）。

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
        # 拒绝的插件要立刻卸载；新信任的插件要立刻加载
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
        """新建本地插件骨架（任务书 2B.6 新建插件向导）。"""
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
        """记录/撤销对本地插件的信任（任务书 2B.9 首次加载确认框）。"""
        skill_id = str(request.get("id") or "")
        trusted = bool(request.get("trusted", True))
        ok = self.skills().trust_plugin(skill_id, trusted)
        if not trusted:
            self.skills().unload(skill_id)
        return {"type": "system", "content": "" if ok else _t("sys.trust_write_failed"),
                "skills": self.skills().status(), "commands": self.commands.all_commands()}

    def _handle_skills_manifest_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """写入本地插件清单（任务书 2B.3 / 2B.10 插件清单编辑）。"""
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
        """把随包附带的示例插件复制到 skills_dev/（任务书 2.5 示例技能）。"""
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

    # ---------- 自动更新（任务五 方案 A） ----------

    def _handle_update_check(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """检查 GitHub Releases 是否有新版本；只提示，不下载、不替换。"""
        try:
            from update_check import check_for_update

            result = check_for_update(self.config.update, force=bool(request.get("force")))
        except Exception as e:  # noqa: BLE001
            log.exception("检查更新失败")
            return {"type": "system", "content": _t("update.check_failed", error=e),
                    "update": {"ok": False, "error": str(e)}}
        # 记录本次检查时间，避免每次启动都请求 GitHub（任务书 5.2 企业环境友好）
        if result.get("checked_at"):
            self.config.update.last_check = result["checked_at"]
            self.config.update.last_seen_version = result.get("latest") or ""
            try:
                self.config.save()
            except Exception:
                log.debug("保存更新检查时间失败", exc_info=True)
        return {"type": "system", "content": result.get("message", ""), "update": result}

    # ---------- 模型选择栏（新增四） ----------

    def _model_hub_error(self, e: Exception, verb: str) -> Dict[str, Any]:
        log.exception("模型中心%s失败", verb)
        return {"type": "system", "content": _t("err.hub_failed", verb=verb, error=e)}

    def _handle_model_hub_search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """搜索可下载的 GGUF 模型（镜像/官方双源，自动回退）。

        v0.2.1 模型市场优化后：**一次调用就把该关键词的全部结果取回来**
        （受安全上限约束），再在本地做筛选/排序/分页，返回第 ``page`` 页。
        同步版本；界面走的是带进度反馈的流式版本 ``model_hub_load``。
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
        """在**已加载的结果集**上做本地筛选/排序/翻页（不重新请求远端）。

        任务书任务三/四/五/六的落点：分页控件、本地实时筛选、结构化筛选器、四种排序
        都只发这一个动作，因此不会因为"筛选条件改了一下"就重打网络。
        """
        try:
            result = self.model_hub().query_session(request)
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_status"))
        return {"type": "system", "content": "", "hub_query": result}

    def _stream_model_hub_load(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """流式加载模型市场结果：边取边报进度，最后把完整数据集交给界面。

        为什么必须是流式的：任务二要求"加载过程需有进度反馈（如正在加载 500 / 4356），
        避免用户干等"，而一次取回 5000 条要发 5 页请求、耗时几十秒。
        进度行用独立 type（``hub_progress``），与既有的 ``summary_progress`` 一样，
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
        # 结果集走独立 type：前端 SendStreamRequestAsync 的 done 只回 content，
        # 结构化数据必须自己占一条消息（与 file_stream 的做法一致）。
        yield {"type": "hub_result", "model_hub": dataset}
        yield {"type": "done", "content": message, "model_hub": dataset}

    def _handle_model_hub_detail(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """模型详情：所有可用的 GGUF 量化文件与大小。"""
        try:
            detail = self.model_hub().detail(str(request.get("repo") or ""), request.get("source"))
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_detail"))
        return {"type": "system", "content": "", "detail": detail}

    def _handle_model_hub_registry(self) -> Dict[str, Any]:
        """模型库列表（含当前正在使用的模型标记）。

        "当前使用中"取**引擎里实际加载的**模型，而不是配置里写的名字：
        无模型就绪状态下配置里仍有默认文件名，但那个文件并不存在、也没被加载，
        按配置标会显示出一个"正在使用中"的假象（任务一：状态要反映真实情况）。
        """
        current = str(getattr(self.llm, "model_path", "") or "")
        try:
            data = self.model_hub().registry(current_model=current)
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_registry"))
        data["model_ready"] = self.model_ready
        return {"type": "system", "content": "", "registry": data}

    def _handle_model_hub_uninstall(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """删除模型文件（前端已确认）。"""
        try:
            result = self.model_hub().uninstall(
                str(request.get("name") or ""), current_model=self.config.model_path
            )
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_uninstall_model"))
        return {"type": "system", "content": f"✅ {result.get('message', '')}"}

    def _handle_model_hub_status(self) -> Dict[str, Any]:
        """模型中心状态（供状态栏与设置窗口显示当前数据源）。"""
        try:
            state = self.model_hub().status()
        except Exception as e:
            return self._model_hub_error(e, _t("sys.verb_read_status"))
        return {"type": "system", "content": "", "model_hub_status": state}

    def _handle_model_hub_config_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """保存模型选择栏配置（数据源 / Token / 模型库目录）。"""
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
        # 数据源等信息变化后重建实例，避免缓存旧设置
        self._model_hub = None
        if changed:
            note = _t("sys.model_hub_saved", fields=_t("sys.list_sep").join(changed))
        else:
            note = _t("sys.model_hub_no_change")
        return {"type": "system", "content": note, "model_hub": self.model_hub().status()}

    # ---------- 无模型就绪 / 重新扫描 / 热加载（v0.2.1 综合优化 任务二、五） ----------

    def _handle_model_scan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """重新扫描模型文件夹：手工放进 ``.gguf`` 后能被识别并按需热加载。"""
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
        """热加载指定模型（不重启进程）。失败时保持无模型状态并给出原因。"""
        result = self.load_model(str(request.get("name") or ""))
        if result.get("ok"):
            content = _t("sys.model_hot_loaded", name=result.get("name", ""))
        else:
            content = _t("sys.model_hot_load_failed", error=result.get("error", ""))
        return {"type": "system", "content": content, "model_load": result,
                "model_state": result.get("state", self.model_state())}

    # ---------- 模型档位（多档默认模型：设置页里显示与切换） ----------

    def profiles_state(self) -> Dict[str, Any]:
        """列出 ``config.models`` 里登记的模型档位，并标出"当前使用""是否已下载"。

        为什么需要它（用户需求："两档都作为可选默认显示、可在设置里切换"）：
        `config.models` 原本只能通过命令行 ``/model <别名>`` 使用，设置界面里看不到，
        用户没法知道有哪几档、哪一档已经在本地。这里把这份注册表变成结构化数据，
        由设置页渲染成"档位"列表（含体积/量化/参数量与切换按钮）。

        刻意**不联网**：只读本地文件，未下载的档位就如实标 `installed=false`
        （要下载请走模型市场，或点档位行上的「下载」——它用的是市场那套流式下载）。
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
                "repo": str(raw.get("repo") or ""),
                "installed": exists,
                "size": size,
                "size_human": _format_bytes(size) if exists else "",
                "context_length": int(raw.get("context_length") or 0),
                "temperature": raw.get("temperature"),
                # 参数量/量化都按文件名推断：**不加载模型**也能在设置页显示档位差异
                "params": _profile_params_text(name),
                "quantization": _profile_quant_text(name),
                "current": bool(name) and name == current,
            })
        return {"current": current, "models_dir": models_dir, "items": items}

    def apply_profile(self, alias: str) -> Dict[str, Any]:
        """切换到 ``config.models`` 里的某一档（热加载 + 套用该档的窗口与温度）。

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
        """设置页的「切换」按钮：换档位并回传新的模型状态。"""
        alias = str(request.get("alias") or "")
        result = self.apply_profile(alias)
        if result.get("ok"):
            content = _t("sys.profile_applied", alias=alias, name=result.get("name", ""))
        else:
            content = _t("sys.profile_apply_failed", error=result.get("error", ""))
        return {"type": "system", "content": content, "model_profile": result,
                "model_profiles": self.profiles_state(),
                "model_state": result.get("state", self.model_state())}

    # ---------- 技能市场（任务五） ----------

    def _market_error(self, e: Exception, verb: str) -> Dict[str, Any]:
        log.exception("技能市场%s失败", verb)
        return {"type": "system", "content": _t("err.market_failed", verb=verb, error=e)}

    def _handle_market_search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """搜索技能：关键词 + 排序 + 页码（缓存命中时直接返回快照）。"""
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
        """技能详情：完整 skill.json、README 摘要、版本、依赖与入口文件。"""
        repo = str(request.get("repo") or "").strip()
        if not repo:
            return {"type": "system", "content": _t("sys.market_missing_repo")}
        try:
            detail = self.market().detail(repo)
        except Exception as e:
            return self._market_error(e, _t("sys.verb_get_detail"))
        return {"type": "system", "content": "", "detail": detail}

    def _handle_market_install(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """安装技能（用户已在前端确认）。"""
        repo = str(request.get("repo") or "").strip()
        if not repo:
            return {"type": "system", "content": _t("sys.market_missing_repo")}
        try:
            result = self.market().install(repo)
        except Exception as e:
            return self._market_error(e, _t("sys.verb_install"))
        ok = bool(result.get("ok"))
        icon = "✅" if ok else "❌"
        return {"type": "system", "content": f"{icon} {result.get('message', '')}"}

    def _handle_market_uninstall(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """卸载技能：移除注册表条目并删除技能目录，再回读确认。"""
        skill_id = str(request.get("id") or request.get("repo") or "").strip()
        if not skill_id:
            return {"type": "system", "content": _t("sys.market_missing_skill_id")}
        try:
            result = self.market().uninstall(skill_id)
        except Exception as e:
            return self._market_error(e, _t("sys.verb_uninstall"))
        ok = bool(result.get("ok"))
        icon = "✅" if ok else "❌"
        return {"type": "system", "content": f"{icon} {result.get('message', '')}"}

    def _handle_market_installed(self) -> Dict[str, Any]:
        try:
            items = self.market().installed()
        except Exception as e:
            return self._market_error(e, _t("sys.verb_read_installed"))
        return {"type": "system", "content": "", "installed": items}

    def _handle_market_status(self) -> Dict[str, Any]:
        """市场连接状态（供主窗口状态栏显示）。"""
        try:
            state = self.market().status()
        except Exception as e:
            return self._market_error(e, _t("sys.verb_query_status"))
        return {"type": "system", "content": "", "market_status": state}

    # ---------- 自动化配置与测试（任务四） ----------

    @staticmethod
    def _automation_payload(cfg) -> Dict[str, Any]:
        """把自动化配置转成前端表单需要的扁平结构。"""
        return cfg.model_dump()

    def _automation_login_payload(self) -> Dict[str, Any]:
        """登录态现状（v0.2.1 自动化优化 任务一）：只给路径/大小/时间，不含 Cookie 内容。"""
        try:
            return self.automation().login_state_status()
        except Exception as exc:  # noqa: BLE001
            return {"exists": False, "profile_dir": "", "size_bytes": 0, "modified": "",
                    "legacy_state_file": "", "legacy_exists": False,
                    "login_check_selector": "", "url_keywords": [], "runnable": True,
                    "error": str(exc)[:200]}

    def _handle_automation_login_state(self, _request: Dict[str, Any]) -> Dict[str, Any]:
        """读取自动化登录态信息（设置窗口与"清除登录态"按钮共用）。"""
        return {
            "type": "system",
            "content": "",
            "login_state": self._automation_login_payload(),
        }

    def _handle_automation_clear_login(self, _request: Dict[str, Any]) -> Dict[str, Any]:
        """清除自动化登录态（删除持久化数据目录），下次运行重新引导登录。"""
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
        """读取自动化配置（设置窗口打开时调用）。"""
        return {
            "type": "system",
            "content": _t("sys.automation_config_title"),
            "automation": self._automation_payload(self.config.automation),
        }

    def _handle_automation_config_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """更新自动化配置并写回配置文件。

        安全协议：勾选（safety_accepted=True）时记录协议版本与同意时间；
        未勾选时 AutomationConfig 校验器会强制把 enabled 置为 False。
        """
        import datetime

        patch = request.get("automation") or {}
        if not isinstance(patch, dict):
            return {"type": "system", "content": _t("sys.automation_bad_format")}
        data = self.config.automation.model_dump()
        data.update({k: v for k, v in patch.items() if k in data})
        # 勾选安全协议时记录版本与时间；取消勾选则清空同意时间并关闭开关
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

    # ---------- 自动化配置向导、模板与导入导出（v0.2.1 任务四） ----------

    #: 需要出现在「填写帮助」里的字段（顺序即界面顺序）
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
        # 登录态（v0.2.1 自动化优化 任务一）：归在第 5 步「运行参数」里，
        # 因为它们都是"怎么跑"的设置，而且大多数用户不需要改（默认值即可）。
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
        ("browser_channel", "step5", False),
        ("schedule_enabled", "step5", False),
        ("schedule_times", "step5", False),
        ("start_with_windows", "step5", False),
        ("safety_accepted", "step5", True),
        ("enabled", "step5", False),
    )

    #: 预置模板（任务书 4.4）；内容取自语言包，可多语言
    AUTOMATION_TEMPLATES = ("contract", "invoice", "report", "table")

    @staticmethod
    def _automation_field_help() -> List[Dict[str, Any]]:
        """字段级帮助（任务书 4.3）：标签、通俗说明、示例、是否必填、所在步骤。"""
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
        """预置模板（任务书 4.4）+ 用户自定义模板（存在 config.user.json 旁）。"""
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
        """把当前配置存成自定义模板（任务书 4.4 末句）。"""
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
        """测试选择器向导的单步（任务书 4.5）：第 1~4 步。

        第 4 步（试运行）需要几分钟，走流式通道 ``automation_wizard``；
        这里处理 1~3 步这类"单次同步"的检查。
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
        """导出自动化配置（任务书 4.7）：排除登录态、Token 等敏感信息。"""
        data = self.config.automation.model_dump()
        # 登录态文件内容是会话 Cookie，绝不导出；路径本身也不导出，避免跨机覆盖
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
        """导入自动化配置（任务书 4.7）。

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

        # 安全协议与开关不从文件里继承：必须由本机用户亲自勾选（安全默认）
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
        """测试选择器：打开浏览器访问指定网址，预览选择器匹配结果。"""
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
        """返回当前上下文占用（used/limit，供前端工具栏实时显示）。"""
        response: Dict[str, Any] = {"type": "context"}
        self._attach_context(response)
        return response

    def _attach_context(self, response: Dict[str, Any]) -> None:
        """把当前上下文占用附加到响应上，前端据此实时刷新（/clear、/undo 后自然减少）。

        优化五：额外给出 KV Cache 估算与上下文窗口，供输入框下方的轻量状态区显示
        （前端不必再去解析 /status 里的中文文本）。
        """
        try:
            messages = [{"role": "system", "content": self.conv.get_system_prompt()}] + self.conv.history
            response["used"] = self._estimate_tokens(messages)
            response["limit"] = int(self.llm.n_ctx * 0.8)
            response["n_ctx"] = int(self.llm.n_ctx)
            try:
                response["kv_cache_text"] = self.llm.kv_cache_text()
            except Exception:
                response["kv_cache_text"] = ""
            try:
                response["model"] = os.path.basename(self.config.model_path or "")
            except Exception:
                response["model"] = ""
            # 任务一：机器可读的"推理是否可用"。前端据此显示"无模型"并把推理入口
            # 变成引导，而不是等用户提交后才报错。不带它前端就只能猜。
            response["model_ready"] = self.model_ready
            try:
                response["search"] = bool(self.commands.search_enabled)
            except Exception:
                response["search"] = False
            # v0.2.1 任务三：模式也做成机器可读字段。前端此前靠匹配
            # "✅ 已切换模式为: " 这个中文前缀来判断，切到英文界面后就失效了。
            try:
                response["mode"] = self.conv.get_mode()
            except Exception:
                response["mode"] = ""
            try:
                response["temperature"] = float(self.commands.temperature)
            except Exception:
                response["temperature"] = None
            # 修复四（方向七：能力透明化）：把"模型能跑多快、窗口开了多少、
            # 有没有触到能力边界"一并带上，前端状态区据此实时显示，
            # 用户不必去翻 /status 的中文文本。
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
        """单独取一份上下文快照（不含 type 字段），供能力接口复用。"""
        snapshot: Dict[str, Any] = {"type": "context"}
        self._attach_context(snapshot)
        snapshot.pop("type", None)
        return snapshot

    def _handle_capability_settings(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """运行期调整能力相关设置（修复四：预设 / 滑动窗口 / 分段摘要参数）。

        只接受白名单字段并逐个做范围校验：这些都是会直接影响推理结果的参数，
        放任前端传什么就写什么，很容易做出一个"看起来很行但跑不动"的配置。
        """
        changed: List[str] = []
        cfg = self.config

        preset = str(request.get("preset") or "").strip().lower()
        if preset:
            from model_profile import apply_preset

            values = apply_preset(preset)
            if values is None:
                return {"type": "system", "content": _t("sys.preset_not_found", name=preset)}
            cfg.preset = preset
            cfg.temperature = float(values["temperature"])
            cfg.top_p = float(values["top_p"])
            cfg.repeat_penalty = float(values["repeat_penalty"])
            cfg.max_tokens = int(values["max_tokens"])
            self.commands.temperature = cfg.temperature
            self.commands.temp_manual = False
            changed.append("preset")

        mode = str(request.get("context_mode") or "").strip().lower()
        if mode:
            if mode not in ("summarize", "slide"):
                return {"type": "system", "content": _t("sys.context_mode_invalid", mode=mode)}
            cfg.context_mode = mode
            changed.append("context_mode")

        for key, low, high in (("temperature", 0.0, 1.0), ("top_p", 0.0, 1.0),
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
                    self._summary_cache = None   # 开关切换后重新按需创建
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
        except Exception:
            log.warning("保存能力设置失败", exc_info=True)
        return {"type": "system",
                "content": _t("sys.capability_settings_saved", fields=", ".join(changed)),
                "changed": changed, "capability": self.model_capability(),
                "context": self._context_snapshot()}

    def stream(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """流式对话：逐个产出 delta 行，最后产出 done 行（支持 stop 中断）。"""
        action = request.get("action")
        if action == "model_download":
            yield from self._stream_model_download(request.get("name", ""))
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
            yield from self._stream_file(str(request.get("path") or ""))
            return
        if action == "automation_wizard":
            yield from self._stream_automation_wizard(request)
            return

        user_msg = request.get("message", "")
        if user_msg.startswith("/"):
            yield self._handle_chat(user_msg)
            return

        # 任务一：无模型时给出引导，而不是抛异常/让前端看到"推理失败"
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

        search_context = self._search_if_enabled(user_msg)
        include_file = bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)
        messages = self.conv.build_messages(user_msg, search_context, include_file=include_file)
        # 任务二（优化已知问题-2）：搜索开着却没拿到可用结果时，**明确告知用户**，
        # 而不是静默继续 —— 否则用户会以为"搜索没生效"或"模型忽略了搜索结果"，
        # 实际是这一层就没拿到东西。这条提示作为回答区的首段前缀发出。
        search_note = self._search_note()
        if search_note:
            yield {"type": "delta", "channel": _CH_ANSWER, "content": search_note + "\n\n"}

        parts: list = []
        # 思考内容与回答走两条通道（任务三）：通道名与前端约定见模块头协议说明。
        splitter = self.llm.make_splitter() if hasattr(self.llm, "make_splitter") else None
        # 运行指标（优化已知问题-2 任务七）：由引擎实测，随心跳与 done 上报。
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
                # 停止检查放在**产出之后**（优化已知问题-2 任务五）。
                # 放在循环开头会让"模型已经生成、只差交付"的那一块被丢掉：
                # 停止信号置位时往往已经有一两块排在引擎的队列里（引擎的读者线程
                # 比消费者快），丢掉它们会出现"停止前界面上明明显示过的思考内容
                # 在 done 里却没有"这种不一致 —— 既有回归用例正好断言了这一点。
                # 放在产出之后：已拿到的内容照常交付，之后立刻停止，不再等模型跑完。
                if channel == _CH_HEARTBEAT:
                    # 任务五：prefill 阶段（首 token 之前）可能几十秒没有任何内容。
                    # 心跳让界面能区分"还在工作"与"卡死了"，并顺带刷新实时指标。
                    yield {"type": "progress", "channel": _CH_HEARTBEAT,
                           "stats": stats.to_dict(),
                           "waiting_first_token": not stats.prefill_done}
                elif channel == _CH_REASONING:
                    yield {"type": "delta", "channel": _CH_REASONING, "content": piece}
                elif channel == _CH_RESET:
                    # 孤立结束标签：此前发到回答区的内容其实都是思考。前端收到
                    # reset 后把回答区已有的文本"搬"到思考区，不需要后端重发。
                    yield {"type": "delta", "channel": _CH_ANSWER,
                           "reset": True, "content": ""}
                else:
                    parts.append(piece)
                    yield {"type": "delta", "channel": _CH_ANSWER, "content": piece}
                if self.stop_event.is_set():
                    break
            # 用分离器自己的回答累积串作为权威结果：出现孤立结束标签时它已经把
            # "此前误发到回答区的内容"改判给思考区，这里不能再另存一份累积串
            # （否则 done.content 会把思考内容一起带上，测试 C2 就是这么抓出来的）。
            full = splitter.answer_text if splitter is not None else "".join(parts)
            thinking = splitter.reasoning_text.strip() if splitter is not None else ""
            # 中断时也要落库：任务书任务五要求"思考过程中止不残留半截内容到回答区"，
            # 回答区只存回答、思考单独存字段，前端据此清理即可。
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", full, reasoning=thinking)
            self._compact_history()
            done: Dict[str, Any] = {"type": "done", "content": full,
                                    "stopped": self.stop_event.is_set()}
            # 运行指标（任务七）：输出 token 数用**模型真分词器**精确数（引擎在
            # _finish_stats 里只做了兜底粗估，并已把 tokens_estimated 置真）。
            # 这里拿到的是完整文本（含思考），与"模型实际生成了多少"口径一致。
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
                # 标签没闭合，且不是用户主动停止（用户停止时前端已经有"已停止生成"提示，
                # 再报"长度上限用完"就是误导）：通常意味着 token 预算被思考吃光。
                done["reasoning_unclosed"] = True
            # 扩展上下文后速度明显下降时给出提示（窗口越大 KV Cache 越大、越慢）
            if self.config.speed_hint and not self.stop_event.is_set():
                hint = self.llm.speed_hint(self.config.speed_min_tps)
                if hint:
                    done["content"] = f"{full}\n\n{hint}" if full else hint
                    _write({"type": "delta", "channel": _CH_ANSWER, "content": f"\n\n{hint}"})
            self._attach_context(done)  # 附带当前上下文占用，供前端刷新
            yield done
        except Exception as e:
            log.exception("流式推理失败")
            yield {"type": "error", "content": _t("sys.inference_failed", error=e)}

    # ---------- 具体动作 ----------

    def _handle_chat(self, user_msg: str) -> Dict[str, Any]:
        if user_msg.startswith("/"):
            content, action = self.commands.handle(user_msg)
            response = {"type": "system", "content": content}
            if action:
                self._merge_action(response, action)
            return response

        # 任务一：无模型时不走推理，直接给引导（并记进历史，用户切回来后能看到上下文）
        if not self.model_ready:
            guide = self.guide_text()
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", guide)
            self._compact_history()
            return {"type": "assistant", "content": guide, "model_ready": False, "guide": True}

        # 意图短路：用户问"我刚才说了什么"时直接引用最近输入，避免小模型混淆视角
        last_user = self.conv.last_user_message()
        if last_user and REPEAT_INTENT.search(user_msg):
            answer = _t("sys.repeat_intent", text=last_user)
            self.conv.add_message("user", user_msg)
            self.conv.add_message("assistant", answer)
            self._compact_history()
            return {"type": "assistant", "content": answer}

        # 修复四（方向七）：提问过于模糊时先追问，而不是硬答。
        # 只在"没有文件上下文、也没有历史"时触发：已经有上下文的情况下
        # "这个怎么办"往往指的是上文内容，追问反而多余。
        from prompts import clarification_prompt, is_vague_question

        if (bool(getattr(self.config, "clarify_vague", True))
                and not self.conv.history and not self.conv.file_content
                and is_vague_question(user_msg)):
            answer = clarification_prompt(user_msg)
            return {"type": "assistant", "content": answer, "clarify": True}

        search_context = self._search_if_enabled(user_msg)
        include_file = bool(FILE_INTENT.search(user_msg)) and bool(self.conv.file_content)
        messages = self.conv.build_messages(user_msg, search_context, include_file=include_file)
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
                # 同一份内容分别给两条通道（任务四）：界面折叠展示思考、只显示答案
                result["reasoning"] = thinking
            self._attach_context(result)
            return result
        except Exception as e:
            log.exception("推理失败")
            return {"type": "system", "content": _t("sys.inference_failed", error=e)}

    def _stream_automation(self) -> Iterator[Dict[str, Any]]:
        """跑一次浏览器自动化，把引擎的进度事件原样转发给前端。

        停止：前端发送 action=stop 会置位 stop_event，引擎在各循环处尽早退出。
        """
        # 任务一的功能边界：自动化里"生成摘要/审核意见"那一步依赖模型，
        # 无模型时先给引导，别让整轮任务跑到一半才失败（既浪费时间又留下半份结果）。
        if not self.model_ready:
            yield {"type": "error",
                   "content": _t("sys.automation_need_model",
                                 guide=_t("sys.no_model_guide", dir=self.models_dir())),
                   "model_ready": False}
            return
        engine = self.automation()
        try:
            for event in engine.run(stop_event=self.stop_event):
                # 结束时把结果也写进对话历史，便于后续提问引用
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
            # 兜底清理下载临时目录（引擎按任务清理文件，这里连同本轮的空目录一并清掉）
            try:
                removed = engine.cleanup_temp()
                if removed:
                    log.info("自动化临时文件清理: %d 项", removed)
            except Exception:
                log.warning("自动化临时目录清理失败", exc_info=True)

    def _stream_automation_pick(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """可视化选取选择器（任务书 4.2）：在真实页面上点一下元素即生成选择器。

        之所以走流式通道：用户可能要在页面上登录、点几下，耗时以分钟计，
        前端需要实时进度、并且能随时停止（关掉浏览器窗口）。
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
        """把选取结果写入配置（前端只负责展示，落盘由后端统一做，避免双写）。"""
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
            log.info("可视化选取已写入 %s = %s", field, selector)
        except Exception:
            log.warning("保存可视化选取结果失败", exc_info=True)

    def _stream_automation_wizard(self, request: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """测试选择器向导（任务书 4.5）：第 4 步需要跑一次真实任务，因此走流式。

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
        """下载模型库中选定的 GGUF 文件（新增四）。

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

        # 下载前的最后一次人工确认信息（大小与预计磁盘占用）
        yield {
            "type": "delta",
            "content": _t("sys.hub_download_prepare", repo=repo, filename=filename,
                          size=(_t("sys.hub_download_size", mb=f"{expected / 1048576:.1f}")
                                if expected else "")),
        }
        self.stop_event.clear()

        # 下载在子线程里进行（urllib 阻塞），主线程轮询进度缓存并把变化推给前端
        latest: Dict[str, str] = {"line": ""}

        def _progress(info: Dict[str, Any]) -> None:
            """字节级进度 → 一行人类可读文本（速度与剩余时间，任务书要求）。"""
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
                # 任务五：下载完成后**就地热加载**，用户不必重启进程/前端。
                # 失败（损坏、格式不支持、内存不足）时保持无模型状态并给出原因与重试路径。
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

    def load_file_into_context(self, filepath: str, progress_sink=None) -> str:
        """解析文件并入上下文；全文超出窗口一定比例时自动分段摘要后注入。

        返回给用户看的反馈文本（成功或失败都会说明原因）。
        ``progress_sink`` 透传给分段摘要（任务四：流式入口据此实时报进度）。
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
            # 任务一：文件解析**不依赖模型**，必须照常完成；只有"分段摘要"这一步需要模型。
            # 所以这里保留已解析的全文，只把摘要这一步换成引导（而不是整个加载失败）。
            if not self.model_ready:
                return _t("sys.file_loaded_no_model", name=name, chars=len(content),
                          tokens=tokens,
                          guide=_t("sys.no_model_guide", dir=self.models_dir()))
            head = _t("sys.file_auto_summary_head", name=name, tokens=tokens,
                      n_ctx=self.llm.n_ctx,
                      ratio=f"{self.config.summarize.trigger_ratio:.0%}") + "\n"
            return head + self.summarize_current_file(
                force=True, source_name=name, trigger=_t("sys.summarize_trigger_auto"),
                progress_sink=progress_sink)
        return _t("sys.file_loaded", name=name, chars=len(content), tokens=tokens)

    def summarize_current_file(self, force: bool = True, source_name: str = "",
                               trigger: str = "", progress_sink=None) -> str:
        """对当前已加载文件做分段摘要并替换注入内容（供自动触发与 /summarize 调用）。

        修复四（方向八：缓存与复用）：摘要结果按"文件指纹 + 摘要参数"缓存，
        同一文件二次上传/二次摘要直接命中缓存，不再重复跑 map-reduce
        （长文档一次摘要是几十次模型调用，重复计算代价很高）。

        ``trigger``（"自动"/"手动"）是**界面文本**：默认值留空，进来后按当前语言
        解析（不能在形参默认值里写 ``_t(...)``——那是导入时求值，会固化语言）。

        ``progress_sink``（任务四）：传入一个可调用对象时，摘要过程中的结构化进度
        事件会交给它（流式入口据此实时推给前端）；不传则退回"打到 stderr"的旧行为，
        这样非流式调用（``/summarize``、自动触发）不会往协议里插异步行。
        """
        trigger = trigger or _t("sys.summarize_trigger_manual")
        if not self.conv.file_content:
            return _t("cmd.resp.summarize_no_file")
        # 任务一：无模型时摘要无从谈起，给引导而不是抛异常
        if not self.model_ready:
            return _t("sys.summarize_need_model",
                      guide=_t("sys.no_model_guide", dir=self.models_dir()))
        name = source_name or os.path.basename(self.conv.file_name or "")

        # 命中缓存：直接把上次的摘要装回上下文（跳过全部模型调用）
        cache = self.summary_cache()
        cache_key_path = self.conv.file_name or name
        params = {
            "chunk_chars": self.config.summarize.chunk_chars,
            "overlap_chars": self.config.summarize.overlap_chars,
            "max_summary_tokens": self.config.summarize.max_summary_tokens,
            "merge_max_tokens": self.config.summarize.merge_max_tokens,
        }
        if cache is not None and os.path.isfile(cache_key_path):
            hit = cache.get(cache_key_path, params)
            if hit and hit.get("text"):
                self.conv.set_file_summary(str(hit["text"]), {
                    "chunks": hit.get("chunk_count") or 0,
                    "origin_chars": hit.get("origin_chars") or 0,
                    "pointer": "",
                    "cached": True,
                    "hits": hit.get("hits") or 0,
                })
                self.llm.clear_cache()
                log.info("分段摘要命中缓存：%s（第 %s 次复用）", name, hit.get("hits"))
                return (
                    _t("sys.summary_cache_hit", trigger=trigger, name=name) + "\n"
                    + _t("sys.summary_cache_hit_detail", chars=hit.get('origin_chars') or 0,
                         chunks=hit.get('chunk_count') or 0) + "\n"
                    + _t("sys.summary_cache_hit_note")
                )

        def _progress(event: Dict[str, Any]) -> None:
            """摘要进度（任务四）：有 sink 就交给流式入口实时推给前端，
            没有就退回打到 stderr 的旧行为（不往协议里插异步行）。"""
            if progress_sink is not None:
                try:
                    progress_sink(event)
                except Exception:
                    log.debug("进度转发失败（已忽略）", exc_info=True)
                return
            label = self._summary_progress_text(event)
            if label:
                print(emoji_mod.prefer(f"📝 {label}"), file=sys.stderr, flush=True)

        result = self.summarizer.summarize(
            self.conv.file_content, source_name=name, force=force, on_progress=_progress,
            should_stop=lambda: self.stop_event.is_set(),
        )
        # 任务四：把"慢"与"坏"分开报。中止/失败都不注入半份摘要，并保留重试路径。
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
        self.conv.set_file_summary(
            result.text,
            {"chunks": result.chunk_count, "origin_chars": result.origin_chars,
             "pointer": result.pointer, "status": result.status},
        )
        # 写入缓存：下次同一文件（内容与参数都没变）直接复用
        if cache is not None and os.path.isfile(cache_key_path):
            cache.put(cache_key_path, params, result.text,
                      chunk_count=result.chunk_count, origin_chars=result.origin_chars)
            cache.save()
        self.llm.clear_cache()  # 注入内容结构变化，旧 KV 缓存失效
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
        # 有段落走兜底时如实说明，不假装一切正常（任务四：真故障要给错误信息）
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
        """把摘要进度事件转成一行人类可读文案（任务四：结构化数据 → 界面文本）。

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
        """流式加载文件（任务四）：解析 → 必要时分段摘要 → 边跑边报进度。

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
        # 收尾：把队列里剩下的事件推完（避免最后一条进度被丢掉）
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
        """用户已同意后下载模型并热切换。"""
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
        """流式下载模型：后台线程下载，逐条 yield 进度，最后 yield done/error。

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

        # 下载完成 → 加载并热切换
        try:
            self.llm.reload(a)
        except Exception as e:
            yield {"type": "error", "content": _t("sys.model_load_after_download_failed", error=e)}
            return
        self.config.model_path = name
        self.config.save()
        yield {"type": "done", "content": _t("sys.model_downloaded_and_loaded", name=name)}

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

    def _compact_history(self, force: bool = False) -> Dict[str, Any]:
        """上下文长度管理：把"装不下"的历史搬出上下文。

        两种模式（修复四 基础要求"滑动窗口模式"）：

        * ``summarize``（默认）：更早的历史交给模型压成 150 字摘要，信息保留最好；
        * ``slide``：直接丢弃最早的历史（滑动窗口），不做额外推理，最快，
          但被丢掉的内容不会以摘要形式留下。

        ``force=True`` 时忽略 80% 阈值直接压缩（``/compact`` 手动触发用）。
        返回一份统计给调用方（指令回执 / 前端状态显示）。
        """
        conv = self.conv
        limit = int(self.llm.n_ctx * 0.8)
        result: Dict[str, Any] = {"compressed": False, "limit": limit}
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
            result["reason"] = "nothing_to_drop"
            return result
        dropped = conv.history[: len(conv.history) - keep_count]
        conv.history = conv.history[len(dropped):]
        result.update(compressed=True, dropped=len(dropped), kept=keep_count)

        mode = str(getattr(self.config, "context_mode", "summarize") or "summarize").lower()
        if mode == "slide":
            # 滑动窗口：不做摘要，只是把最早的历史移出上下文
            log.info("上下文 %d/%d tokens：滑动窗口丢弃最早 %d 条（未生成摘要）",
                     total, limit, len(dropped))
            result["mode"] = "slide"
            result["summary_chars"] = 0
        else:
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
            result["mode"] = "summarize"
            result["summary_chars"] = len(summary)
            log.info(
                "上下文 %d/%d tokens 超限：保留最新 %d 条，压缩 %d 条为摘要（%d 字）",
                total, limit, keep_count, len(dropped), len(summary),
            )
        try:
            result["used_after"] = self._estimate_tokens(
                [{"role": "system", "content": conv.get_system_prompt()}] + conv.history)
        except Exception:
            result["used_after"] = None
        return result

    def compact_context(self, force: bool = True) -> Dict[str, Any]:
        """对外暴露的手动压缩入口（``/compact`` 与前端按钮共用）。

        压缩后清掉 KV 缓存：历史变了，缓存不再对应，
        不清的话下一次生成会带着错误的注意力前缀。
        """
        result = self._compact_history(force=force)
        if result.get("compressed"):
            try:
                self.llm.clear_cache()
            except Exception:
                log.debug("压缩后清 KV 缓存失败", exc_info=True)
        return result

    def _search_if_enabled(self, query: str) -> str:
        """按开关执行搜索并返回**可注入上下文**的文本（不可用时为空串）。

        优化已知问题-2 任务二：失败与"没搜到"都**如实记录**，并且把不可用的原因
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
        # 不可用：区分"失败"与"没有结果"，两者要给用户不同的说法
        if not outcome.ok:
            self._last_search_note = _t("sys.search_failed", error=outcome.error[:120])
        else:
            self._last_search_note = _t("sys.search_empty")
        log.info("搜索未产生可用结果：ok=%s 条数=%d 原因=%s",
                 outcome.ok, outcome.count, self._last_search_note)
        return ""

    def _search_note(self) -> str:
        """取出上一次搜索的"为什么没用上"说明（无则空串）。"""
        return str(getattr(self, "_last_search_note", "") or "")

    def search_diagnostics(self) -> Dict[str, Any]:
        """搜索链路的可观测快照（供状态区与测试断言四个环节）。

        任务书要求"逐环节排查"，所以这里把每个环节的结果都暴露出来：
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
            "note": self._search_note(),
        }

    @staticmethod
    def _merge_action(response: Dict[str, Any], action: Dict[str, Any]) -> None:
        """把动作字段并入响应（不覆盖 type）。"""
        for key, value in action.items():
            if key != "type":
                response[key] = value


class ModelMissing(Exception):
    """本地缺少模型：携带仓库中最匹配的文件名与体积，交由前端征询用户后再下载。"""

    def __init__(self, filename: str, size: Optional[int] = None, repo: str = "") -> None:
        self.filename = filename
        self.size = size
        self.repo = repo
        super().__init__(f"本地缺少模型 {filename}")


# ---------------------------------------------------------------------------
# 旧行为已移除（v0.2.1 综合优化 任务一）：这里曾有两个函数
#   * `_await_download_decision()` —— 缺模型时**阻塞**读 stdin 等用户答复，
#     收到 `exit` 或 stdin 关闭就返回 None，调用方随之结束进程；
#   * `_download_with_progress()` —— 上面那个函数专用的一次性下载器
#     （模型市场已经有更完整的下载实现 `_stream_model_hub_download`：
#     断点续传、重试、速度/剩余时间、取消、SHA256 校验）。
# 它们合起来构成"要么立即下载，要么整个应用不可用"的墙角，正是本轮要拆掉的东西。
# 现在缺模型走"无模型就绪"：后端照常运行，引导用户在模型市场下载或手动放置 .gguf，
# 到位后原地热加载。退出只由用户主动触发（前端发 `exit`，主循环里处理）。
# 对应的语言键 `sys.await_download_confirm` / `sys.model_declined` /
# `sys.model_missing_no_repo` 也一并删除，避免留下"描述已废弃行为"的文案。
# ---------------------------------------------------------------------------


def main() -> None:
    # 修复六：先把输出流切到 UTF-8 并判断能否安全输出 emoji。
    # 必须在任何输出之前执行——控制台代码页不是 UTF-8 时 emoji 会变成 "??"，
    # 那时再降级就已经晚了（日志/进度消息是边跑边打的）。
    emoji_mod.configure_streams()
    if "--no-emoji" in sys.argv:
        emoji_mod.set_enabled(False)

    config = Config.load()

    # 任务一：模型缺失**不再征询后退出**，而是以"无模型就绪"状态继续跑。
    # 这里只做一次"模型在不在"的判断；缺模型时先把 Backend 建起来（引擎留空），
    # 稍后在 ready 之前发一条**非阻塞**的 need_download 告知前端。
    model_path = check_model(config.model_path)
    if not model_path:
        log.warning("%s", _t("sys.log_missing_model", model=config.model_path))

    try:
        with _suppress_stdout():
            backend = Backend(config, model_path or "")
            restored = backend.restore_session()
            backend.warmup()
            backend.touch_model_usage()   # 新增四：记录模型最后使用时间
    except Exception as e:
        log.exception("后端初始化失败")
        # 向 stdout 输出一条错误响应，前端首条请求即可读到
        print(json.dumps({"type": "system", "content": _t("sys.backend_init_failed", error=e)},
                         ensure_ascii=False), flush=True)
        return

    # v0.2.1 任务二：加载已安装技能与本地插件（发现/导入/注册命令）。
    # 放在预热之后：技能加载失败不会拖慢或阻塞主程序启动，且异常已被隔离。
    try:
        skill_summary = backend.load_skills()
        if skill_summary.get("total"):
            log.info("技能加载完成：%s", skill_summary)
    except Exception:
        log.exception("技能加载阶段异常（已忽略）")
        skill_summary = {}

    # 任务一：缺模型时告知前端（**非阻塞**）——前端据此显示引导，而不是弹"是否下载、
    # 拒绝就退出"的强制选择。放在 ready 之前，前端渲染就绪状态时一次就能拿到全部信息。
    model_missing: Dict[str, Any] = {}
    if not backend.model_ready:
        target, size, repo_error = "", 0, ""
        try:
            target, size = find_best_model(config.model_repo, config.model_path,
                                           hf_endpoint=config.hf_endpoint)
        except Exception as e:  # noqa: BLE001
            # 查仓库失败（离线/镜像不可达）不影响无模型启动：引导里仍给出模型市场与
            # 手动放置两条路径，只是没有"预计体积"可显示。
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
            # blocking=False：**新语义**。旧行为是"后端停下等答复，拒绝即退出"；
            # 现在它只是一条告知，后端已就绪并继续服务（见模块头部的说明）。
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

    log.info("%s %s 就绪 — Powered by %s", APP_NAME, APP_VERSION, STUDIO)
    # 就绪声明：预热等准备工作全部完成后才输出，前端据此放行输入；附带恢复的历史消息供前端展示
    _write({"type": "ready", "version": APP_VERSION, "studio": STUDIO, "restored": restored,
            "history": backend.conv.history,
            "locale": _i18n().locale, "i18n": _i18n().info(),
            "commands": backend.commands.all_commands(),
            "skills": backend.skills().status(),
            # 任务一：明确区分"后端已就绪"与"推理可用"
            "model_ready": backend.model_ready,
            "model_state": backend.model_state(),
            "model_missing": model_missing,
            # 修复六 / 修复七：告知前端 emoji 是否可用、数据目录在哪（合规与支持排查都要用）
            "emoji": emoji_mod.enabled(),
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
                # busy 标记（v0.2.1 模型市场优化）：单条 stdout 是共享流，
                # 两个线程同时写会交错成坏 JSON，所以长任务期间一律拒绝并发请求。
                # 带上结构化标记，界面才能说清"是后端忙"而不是"模型中心坏了"。
                _write({"type": "system", "content": _t("sys.busy"), "busy": True})
                continue

        if action in ("chat_stream", "model_download", "automation_run", "model_hub_download",
                      "automation_pick", "automation_wizard", "file_stream", "model_hub_load"):
            # 八个长任务都走流式线程：进度实时输出，stop 可中断。
            # automation_* 还会检查 stop_event 尽早退出并关闭浏览器；
            # file_stream（任务四）在分段摘要期间持续报进度，stop 能真正打断生成；
            # model_hub_load（模型市场优化 任务二）按页报告"已取回 N 条"。
            if action in ("automation_run", "automation_pick", "automation_wizard") and backend.stream_active:
                _write({"type": "system", "content": _t("sys.automation_running")})
                continue
            backend.stop_event.clear()
            backend.stream_active = True
            threading.Thread(target=_stream_worker, args=(request,), daemon=True).start()
        elif action == "stop":
            if backend.stream_active:
                backend.stop_event.set()
                # 自动化引擎另有自己的停止入口，一并通知，确保浏览器及时关闭
                if backend._automation is not None:
                    backend._automation.stop()
                _write({"type": "system", "content": _t("sys.stop_sent")})
            else:
                _write({"type": "system", "content": _t("sys.stop_none")})
        elif action in ("exit", "quit"):
            # 任务一：退出只由用户主动触发。旧行为里"拒绝下载模型"也会走到这里，
            # 现在缺模型时后端照常运行，这条路只剩前端"退出"按钮会用到。
            log.info("收到退出请求，保存会话后退出")
            backend.save_session()
            _write({"type": "system", "content": _t("sys.exit_ack")})
            return
        else:
            response = backend.handle(request)
            backend._attach_context(response)  # 附带上下文占用，/clear、/undo 后实时减少
            _write(response)
            backend.save_session()
            if response.get("action") == "exit":
                log.info("%s", _t("sys.exit_ack"))
                return


if __name__ == "__main__":
    main()
