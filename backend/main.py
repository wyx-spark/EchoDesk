"""EchoDesk 后端主程序：与前端通过 stdin/stdout 以 JSON 行协议通信。

协议：
  {"action": "chat", "message": "..."}         -> 一行响应 {"type": "assistant|system", "content": ...}
  {"action": "chat_stream", "message": "..."}  -> 多行流式响应：{"type":"delta",...}... {"type":"done",...}
  {"action": "stop"}                           -> 中断当前流式生成
  {"action": "file", "path": "..."}            -> 一行响应
  {"action": "model_download", "name": "..."}  -> 下载模型并热切换：delta 进度 + done

启动前若本地缺少模型：先输出 {"type":"need_download", model, size, repo} 征询用户，
用户同意（收到 action=model_download）才下载；拒绝或 stdin 关闭则后端直接退出。

流式生成在后台线程执行，主循环继续读取 stdin，从而支持 "stop" 中断。
启动时先完成模型加载、会话恢复与预热（warmup），再输出 {"type":"ready",...} 就绪声明；
前端只有读到就绪声明后才会允许用户输入。
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
    from gguf_meta import resolve_context_window
    from llm_engine import LLMEngine
    from search_engine import SearchEngine
    from summarizer import DocumentSummarizer

# 多语言（任务三）：必须在 Config.load 之后导入，避免 i18n 反向依赖 config 时成环
from i18n import get as _i18n
from i18n import t as _t

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

    def __init__(self, config: Config, model_path: str) -> None:
        self.config = config
        self.llm = self._build_engine(config, model_path)
        self.conv = ConversationManager(config.max_history, config.default_mode)
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
        self.stream_active = False
        self.stop_event = threading.Event()

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

            self._model_hub = ModelHub(self.config.model_hub)
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
    def _build_engine(config: Config, model_path: str) -> LLMEngine:
        """按"所选模型"的上下文窗口创建推理引擎。

        窗口取配置值、模型训练窗口（GGUF 元数据）与 config.max_context 三者的最小值；
        加载时由 LLMEngine 输出窗口大小与 KV Cache 占用日志。
        """
        n_threads = config.n_threads or os.cpu_count() or 4
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

    def touch_model_usage(self) -> None:
        """记录当前模型的"最后使用时间"（新增四：模型库列表要显示该信息）。"""
        try:
            self.model_hub().touch(os.path.basename(self.config.model_path or ""))
        except Exception:
            log.debug("更新模型最后使用时间失败", exc_info=True)

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
        if action == "automation_config":
            return self._handle_automation_config(request)
        if action == "automation_config_set":
            return self._handle_automation_config_set(request)
        if action == "automation_test":
            return self._handle_automation_test(request)
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
        # ---------- 多语言（任务三 3.4） ----------
        if action == "set_language":
            return self._handle_set_language(request)
        if action == "language":
            return self._handle_language()
        # ---------- 指令面板数据源（任务二 2.6） ----------
        if action == "commands":
            return {"type": "system", "content": "", "commands": self.commands.all_commands()}
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
        enabled = bool(request.get("enabled"))
        self.config.marketplace.dev_mode = enabled
        try:
            self.config.save()
        except Exception:
            log.warning("保存开发者模式开关失败", exc_info=True)
        self.skills()._dev_mode_cache = None
        return {"type": "system",
                "content": _t("skill.dev_mode_on" if enabled else "skill.dev_mode_off"),
                "skills": self.skills().status()}

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
        return {"type": "system", "content": "" if ok else "❌ 信任状态写入失败",
                "skills": self.skills().status(), "commands": self.commands.all_commands()}

    def _handle_skills_manifest_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """写入本地插件清单（任务书 2B.3 / 2B.10 插件清单编辑）。"""
        payload = request.get("manifest")
        if not isinstance(payload, dict):
            return {"type": "system", "content": "❌ 清单内容不合法"}
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
            return {"type": "system", "content": "❌ 复制示例插件失败: " + "; ".join(errors)}
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
        """搜索可下载的 GGUF 模型（镜像/官方双源，自动回退）。"""
        try:
            result = self.model_hub().search(
                keyword=str(request.get("keyword") or ""),
                sort=str(request.get("sort") or "downloads"),
                page=int(request.get("page") or 1),
                limit=int(request.get("limit") or self.config.model_hub.search_limit),
                source=request.get("source"),
                min_downloads=int(request.get("min_downloads") or 0),
                quant=str(request.get("quant") or ""),
            )
        except Exception as e:
            return self._model_hub_error(e, "搜索")
        return {"type": "system", "content": result.get("message", ""), "model_hub": result}

    def _handle_model_hub_detail(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """模型详情：所有可用的 GGUF 量化文件与大小。"""
        try:
            detail = self.model_hub().detail(str(request.get("repo") or ""), request.get("source"))
        except Exception as e:
            return self._model_hub_error(e, "读取详情")
        return {"type": "system", "content": "", "detail": detail}

    def _handle_model_hub_registry(self) -> Dict[str, Any]:
        """模型库列表（含当前正在使用的模型标记）。"""
        try:
            data = self.model_hub().registry(current_model=self.config.model_path)
        except Exception as e:
            return self._model_hub_error(e, "读取模型库")
        return {"type": "system", "content": "", "registry": data}

    def _handle_model_hub_uninstall(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """删除模型文件（前端已确认）。"""
        try:
            result = self.model_hub().uninstall(
                str(request.get("name") or ""), current_model=self.config.model_path
            )
        except Exception as e:
            return self._model_hub_error(e, "删除模型")
        return {"type": "system", "content": f"✅ {result.get('message', '')}"}

    def _handle_model_hub_status(self) -> Dict[str, Any]:
        """模型中心状态（供状态栏与设置窗口显示当前数据源）。"""
        try:
            state = self.model_hub().status()
        except Exception as e:
            return self._model_hub_error(e, "读取状态")
        return {"type": "system", "content": "", "model_hub_status": state}

    def _handle_model_hub_config_set(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """保存模型选择栏配置（数据源 / Token / 模型库目录）。"""
        patch = request.get("model_hub") or {}
        if not isinstance(patch, dict):
            return {"type": "system", "content": "❌ model_hub 必须是对象"}
        changed: List[str] = []
        for key in ("enabled", "source", "hf_token", "models_dir", "registry_file", "verify_sha256"):
            if key in patch:
                setattr(self.config.model_hub, key, patch[key])
                changed.append(key)
        try:
            self.config.save()
        except Exception as e:
            return {"type": "system", "content": f"❌ 配置保存失败: {e}"}
        # 数据源等信息变化后重建实例，避免缓存旧设置
        self._model_hub = None
        note = "已保存：" + ("、".join(changed) if changed else "无变化")
        return {"type": "system", "content": f"✅ {note}", "model_hub": self.model_hub().status()}

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
            return self._market_error(e, "搜索")
        return {"type": "system", "content": result.get("message", ""), "market": result}

    def _handle_market_detail(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """技能详情：完整 skill.json、README 摘要、版本、依赖与入口文件。"""
        repo = str(request.get("repo") or "").strip()
        if not repo:
            return {"type": "system", "content": "❌ 缺少仓库名"}
        try:
            detail = self.market().detail(repo)
        except Exception as e:
            return self._market_error(e, "详情获取")
        return {"type": "system", "content": "", "detail": detail}

    def _handle_market_install(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """安装技能（用户已在前端确认）。"""
        repo = str(request.get("repo") or "").strip()
        if not repo:
            return {"type": "system", "content": "❌ 缺少仓库名"}
        try:
            result = self.market().install(repo)
        except Exception as e:
            return self._market_error(e, "安装")
        ok = bool(result.get("ok"))
        icon = "✅" if ok else "❌"
        return {"type": "system", "content": f"{icon} {result.get('message', '')}"}

    def _handle_market_uninstall(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """卸载技能：移除注册表条目并删除技能目录，再回读确认。"""
        skill_id = str(request.get("id") or request.get("repo") or "").strip()
        if not skill_id:
            return {"type": "system", "content": "❌ 缺少技能标识"}
        try:
            result = self.market().uninstall(skill_id)
        except Exception as e:
            return self._market_error(e, "卸载")
        ok = bool(result.get("ok"))
        icon = "✅" if ok else "❌"
        return {"type": "system", "content": f"{icon} {result.get('message', '')}"}

    def _handle_market_installed(self) -> Dict[str, Any]:
        try:
            items = self.market().installed()
        except Exception as e:
            return self._market_error(e, "读取已安装列表")
        return {"type": "system", "content": "", "installed": items}

    def _handle_market_status(self) -> Dict[str, Any]:
        """市场连接状态（供主窗口状态栏显示）。"""
        try:
            state = self.market().status()
        except Exception as e:
            return self._market_error(e, "状态查询")
        return {"type": "system", "content": "", "market_status": state}

    # ---------- 自动化配置与测试（任务四） ----------

    @staticmethod
    def _automation_payload(cfg) -> Dict[str, Any]:
        """把自动化配置转成前端表单需要的扁平结构。"""
        return cfg.model_dump()

    def _handle_automation_config(self, _request: Dict[str, Any]) -> Dict[str, Any]:
        """读取自动化配置（设置窗口打开时调用）。"""
        return {
            "type": "system",
            "content": "自动化配置",
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
            return {"type": "system", "content": "❌ 自动化配置格式不正确"}
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
            return {"type": "system", "content": f"❌ 自动化配置无效: {e}"}
        self.config.save()
        cfg = self.config.automation
        note = ""
        if cfg.enabled and cfg.safety_accepted:
            note = f"（安全协议 v{cfg.safety_version}，同意时间 {cfg.safety_accepted_at}）"
        return {
            "type": "system",
            "content": f"✅ 自动化配置已保存{note}",
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
            return {"type": "system", "content": "❌ 请提供模板名称"}
        values = request.get("template")
        if not isinstance(values, dict):
            return {"type": "system", "content": "❌ 模板内容不合法"}
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
            return {"type": "system", "content": f"❌ 保存模板失败: {e}"}
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
            return {"type": "system", "content": "❌ 第 4 步请使用流式向导（automation_wizard）"}
        try:
            result = self.automation().wizard_step(step)
        except Exception as e:  # noqa: BLE001
            log.exception("测试向导第 %s 步失败", step)
            return {"type": "system", "content": f"❌ 第 {step} 步失败: {e}"}
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
            "note": ("EchoDesk 自动化配置导出文件。已排除登录态、处理记录与临时目录，"
                     "可安全地在同组织的多台电脑之间复用。"),
            "automation": self._exportable_automation(),
        }
        try:
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            return {"type": "system", "content": f"❌ 导出失败: {e}"}
        return {"type": "system", "content": f"✅ 自动化配置已导出到 {dest}", "path": dest}

    def _handle_automation_config_import(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """导入自动化配置（任务书 4.7）。

        导入的语言与当前界面语言不同时给出提示，但**不阻断**导入。
        """
        path = str(request.get("path") or "").strip()
        if not path or not os.path.isfile(path):
            return {"type": "system", "content": f"❌ 找不到文件: {path}"}
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:  # noqa: BLE001
            return {"type": "system", "content": f"❌ 读取失败: {e}"}
        if not isinstance(payload, dict) or payload.get("format") != "echodesk-automation":
            return {"type": "system", "content": "❌ 这不是 EchoDesk 自动化配置文件（缺少 format 标记）"}
        incoming = payload.get("automation")
        if not isinstance(incoming, dict):
            return {"type": "system", "content": "❌ 文件里没有 automation 配置段"}

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
            return {"type": "system", "content": f"❌ 配置无效，未导入: {e}"}
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
            return {"type": "system", "content": "❌ 请先填写任务列表网址与元素位置再测试"}
        try:
            result = self.automation().test_selector(url, selector)
        except Exception as e:
            log.exception("测试选择器失败")
            return {"type": "system", "content": f"❌ 测试选择器失败: {e}"}
        if not result.get("ok"):
            return {"type": "system", "content": f"❌ 测试失败: {result.get('error') or '未匹配到元素'}"}

        lines = [f"✅ 选择器测试通过：匹配到 {result['count']} 个元素（{result.get('kind', 'css')}）"]
        for index, sample in enumerate(result.get("samples", []), 1):
            href = ""
            hrefs = result.get("hrefs") or []
            if index - 1 < len(hrefs) and hrefs[index - 1]:
                href = f" → {hrefs[index - 1]}"
            lines.append(f"  {index}. {sample}{href}")
        if result["count"] > len(result.get("samples", [])):
            lines.append(f"  …（仅预览前 {len(result.get('samples', []))} 个）")
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
        except Exception:
            pass

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
        if action == "automation_pick":
            yield from self._stream_automation_pick(request)
            return
        if action == "automation_wizard":
            yield from self._stream_automation_wizard(request)
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
            yield {"type": "error", "content": _t("sys.inference_failed", error=e)}

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
            answer = _t("sys.repeat_intent", text=last_user)
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
            return {"type": "system", "content": _t("sys.inference_failed", error=e)}

    def _stream_automation(self) -> Iterator[Dict[str, Any]]:
        """跑一次浏览器自动化，把引擎的进度事件原样转发给前端。

        停止：前端发送 action=stop 会置位 stop_event，引擎在各循环处尽早退出。
        """
        engine = self.automation()
        try:
            for event in engine.run(stop_event=self.stop_event):
                # 结束时把结果也写进对话历史，便于后续提问引用
                if event.get("type") == "done":
                    summary = str(event.get("content") or "")
                    if summary:
                        self.conv.add_message("user", "（自动化任务执行结果）")
                        self.conv.add_message("assistant", summary)
                        self._compact_history()
                yield event
        except Exception as e:
            log.exception("自动化执行失败")
            yield {"type": "error", "content": f"❌ 自动化执行失败: {e}"}
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
            yield {"type": "error", "content": f"❌ 可视化选取失败: {e}"}

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
            yield {"type": "error", "content": f"❌ 测试向导第 {step} 步失败: {e}"}
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
            "content": f"📥 准备从 {repo} 下载 {filename}"
                       + (f"（{expected / 1048576:.1f} MB）" if expected else ""),
        }
        self.stop_event.clear()

        # 下载在子线程里进行（urllib 阻塞），主线程轮询进度缓存并把变化推给前端
        latest: Dict[str, str] = {"line": ""}

        def _progress(info: Dict[str, Any]) -> None:
            """字节级进度 → 一行人类可读文本（速度与剩余时间，任务书要求）。"""
            if "retry" in info:
                latest["line"] = (
                    f"↻ 第 {info.get('attempt')}/{info.get('retries')} 次重试：{info.get('error')}"
                )
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
                line += f"  剩余 {eta // 60:02d}:{eta % 60:02d}"
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
                result.update({"ok": False, "message": f"下载失败：{exc}", "path": ""})
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
                    yield {"type": "delta", "content": "⏹ 已请求取消下载，正在收尾（已保留断点）…"}
            if latest["line"] and latest["line"] != reported:
                yield {"type": "delta", "content": latest["line"]}
            thread.join(timeout=10)

            if result.get("ok"):
                path = str(result.get("path") or "")
                self.conv.add_message("user", f"（下载模型 {filename}）")
                self.conv.add_message("assistant", f"模型已下载并加入模型库：{os.path.basename(path)}")
                self._compact_history()
                yield {"type": "done", "content": f"✅ {result.get('message', '下载完成')}", "model": path}
            else:
                icon = "⏹" if result.get("cancelled") else "❌"
                yield {"type": "done", "content": f"{icon} {result.get('message', '下载未完成')}",
                       "cancelled": bool(result.get("cancelled"))}
        except Exception as e:
            log.exception("模型下载失败")
            yield {"type": "error", "content": f"❌ 模型下载失败: {e}"}

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


class ModelMissing(Exception):
    """本地缺少模型：携带仓库中最匹配的文件名与体积，交由前端征询用户后再下载。"""

    def __init__(self, filename: str, size: Optional[int] = None, repo: str = "") -> None:
        self.filename = filename
        self.size = size
        self.repo = repo
        super().__init__(f"本地缺少模型 {filename}")


def _download_with_progress(config: Config, name: str) -> Optional[str]:
    """下载模型并把进度以 delta 行输出（前端在对话框中实时显示）；失败返回 None。

    进度按整数百分比去重，避免刷屏。
    """
    last = {"pct": -1}

    def _progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total > 0 else 0
        if pct == last["pct"]:
            return
        last["pct"] = pct
        _write({
            "type": "delta",
            "content": f"📥 正在下载 {name} ... {pct}% ({done / 1048576:.1f} MB / {total / 1048576:.1f} MB)",
        })

    try:
        path = download_model(
            name, config.model_repo,
            hf_endpoint=config.hf_endpoint, on_progress=_progress,
        )
    except Exception as e:
        log.exception("模型下载失败")
        _write({"type": "error", "content": f"❌ 模型下载失败: {e}"})
        return None

    # 下载的是仓库真实文件名，同步配置以免下次仍用不存在的名字
    if name != config.model_path:
        config.model_path = name
        for prof in config.models.values():
            if prof.get("repo") == config.model_repo:
                prof["path"] = name
        try:
            config.save()
        except Exception:
            log.warning("配置保存失败（不影响本次运行）", exc_info=True)
    _write({"type": "done", "content": f"✅ 模型已下载: {name}，正在加载并预热…"})
    return path


def _await_download_decision(config: Config, fallback_name: str) -> Optional[str]:
    """模型缺失时等待前端答复。

    收到 ``model_download`` → 下载并返回模型路径；收到 ``exit`` 或 stdin 关闭 → 返回 None
    （调用方据此让后端退出，前端显示提示）。
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff").strip()
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log.warning("收到非法 JSON: %r", line)
            continue
        action = request.get("action")
        if action == "model_download":
            name = str(request.get("name") or "").strip() or fallback_name
            log.info("用户已同意下载模型: %s", name)
            return _download_with_progress(config, name)
        if action in ("exit", "quit"):
            return None
        _write({
            "type": "system",
            "content": "⏳ 正在等待是否下载模型的确认（发送 model_download 开始下载，exit 退出）",
        })
    return None


def main() -> None:
    config = Config.load()

    # 本地模型缺失时不再直接下载，而是先征询用户（前端弹确认对话框）
    model_path = check_model(config.model_path)
    if not model_path:
        log.warning("%s", _t("sys.log_missing_model", model=config.model_path))
        try:
            target, size = find_best_model(config.model_repo, config.model_path, hf_endpoint=config.hf_endpoint)
        except Exception as e:
            log.exception("查询模型仓库失败")
            _write({
                "type": "system",
                "content": _t("sys.model_missing_no_repo", model=config.model_path,
                              repo=config.model_repo, error=e),
            })
            return
        _write({
            "type": "need_download",
            "model": target,
            "size": int(size or 0),
            "repo": config.model_repo,
            "current": config.model_path,
            "content": _t(
                "sys.model_missing_ask", model=config.model_path, repo=config.model_repo,
                target=target,
                size=(_t("sys.model_missing_size", mb=f"{size / 1048576:.0f}")
                      if size else _t("sys.model_missing_unknown_size")),
            ),
        })
        model_path = _await_download_decision(config, target)
        if not model_path:
            log.info("%s", _t("sys.model_declined"))
            return

    try:
        with _suppress_stdout():
            backend = Backend(config, model_path)
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

    log.info("%s %s 就绪 — Powered by %s", APP_NAME, APP_VERSION, STUDIO)
    # 就绪声明：预热等准备工作全部完成后才输出，前端据此放行输入；附带恢复的历史消息供前端展示
    _write({"type": "ready", "version": APP_VERSION, "studio": STUDIO, "restored": restored,
            "history": backend.conv.history,
            "locale": _i18n().locale, "i18n": _i18n().info(),
            "commands": backend.commands.all_commands(),
            "skills": backend.skills().status()})

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
                _write({"type": "system", "content": _t("sys.busy")})
                continue

        if action in ("chat_stream", "model_download", "automation_run", "model_hub_download",
                      "automation_pick", "automation_wizard"):
            # 六种长任务都走流式线程：进度实时输出，stop 可中断。
            # automation_* 还会检查 stop_event 尽早退出并关闭浏览器。
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
