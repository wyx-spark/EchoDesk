"""EchoDesk 浏览器自动化引擎（任务四）。

用途
----
把一个"任务列表页 → 任务详情页 → 附件"的重复性网页作业流程自动化：用 Playwright
驱动本机浏览器（默认 Microsoft Edge，有头模式）打开任务列表，逐个进入任务详情页，
按要求下载附件，调用本地模型生成摘要与审核意见，把结果回传前端并落盘留档。

设计取舍
--------
* **有头模式（headless=False）**：任务书要求用户能看到浏览器实际操作过程，也便于在
  出现验证码/二次验证时人工介入，因此不做无人值守的静默运行；通道启动失败时回退到
  Playwright 自带的默认 Chromium，并在进度里说明原因。
* **临时文件策略**：每轮任务使用下载目录下的独立子目录，每个任务再用自己的子目录，
  任务处理完**立即**删除该任务的临时文件（而不是整轮结束再统一清理），避免大量附件
  长期占盘、也避免不同任务的同名文件互相覆盖；单文件超过 200MB 直接跳过。
* **file:// 兜底**：本地/内网页面（file:// 协议）下浏览器可能不触发下载事件，这类
  链接改为直接复制本地文件，保证本地自测与内网离线场景可用。
* **脱敏规则**：审计日志只记事件、任务链接、**文件名**（不含完整路径）、结果状态与
  截断后的错误摘要；文件正文、模型输出正文、Cookie/Token 一律不入日志，形如
  ``token=...``/``password=...``/``secret=...``/``key=...`` 的值统一替换为 ``***``。
* **Shadow DOM 只做简单穿透**：用 ``evaluate`` 遍历 shadowRoot 做 ``querySelectorAll``；
  命中的元素拿不到可点击句柄，因此这类链接只能走"直接请求 URL"的下载兜底路径。

公开接口：``AutomationError`` / ``AutomationEngine``（validate / test_selector / run /
stop / status / cleanup_temp）。``run()`` 是生成器，逐个产出与 EchoDesk 流式协议一致的
``{"type": ..., "content": ...}`` 事件。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import url2pathname

# 后端各模块是"平铺"导入（main.py 里 ``from config import ...``），这里把本文件所在目录
# 补进 sys.path，保证在任意工作目录下被导入时 file_parser 仍可解析。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 文件解析优先复用后端既有实现；pywin32 等依赖缺失时不让整个模块导入失败
try:  # pragma: no cover - 环境相关
    from file_parser import parse_file_with_wps as _parse_file_with_wps
except Exception:  # noqa: BLE001
    _parse_file_with_wps = None  # type: ignore[assignment]

# 新增三：结果文件必须是纯文本，因此模型输出统一经 markdown_to_plain 落盘
try:  # pragma: no cover - 环境相关
    from plaintext import markdown_to_plain as _markdown_to_plain
except Exception:  # noqa: BLE001
    def _markdown_to_plain(text: str) -> str:  # type: ignore[misc]
        """plaintext 模块不可用时的退化实现（仅去围栏，避免整轮结果写不出来）。"""
        return str(text or "")

# 新增三：默认结果位置 = 程序根目录
try:  # pragma: no cover - 环境相关
    from config import program_root as _program_root
except Exception:  # noqa: BLE001
    def _program_root() -> str:  # type: ignore[misc]
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Playwright 同样做容错导入：未安装时由 validate()/run() 给出明确提示，而不是启动即崩
try:  # pragma: no cover - 环境相关
    from playwright.sync_api import TimeoutError as _PWTimeoutError
    from playwright.sync_api import sync_playwright as _sync_playwright
except Exception:  # noqa: BLE001
    _sync_playwright = None  # type: ignore[assignment]

    class _PWTimeoutError(Exception):  # type: ignore[no-redef]
        """Playwright 缺失时的占位超时异常类型。"""


__all__ = ["AutomationError", "AutomationEngine", "MAX_FILE_BYTES"]

log = logging.getLogger("EchoDesk.Automation")

# ---------- 常量 ----------

# 单个附件大小上限：超过则跳过并记录（避免误下载超大文件拖垮磁盘与解析器）
MAX_FILE_BYTES = 200 * 1024 * 1024

# 判定"这是 XPath 而不是 CSS"的前缀（与配置说明保持一致）
_XPATH_PREFIXES = ("//", "(", "xpath=")

# 浏览器内部页面（Edge 每次下载后会自行打开 edge://downloads-hub/ 之类的界面页）。
# 这类页面既不能当作"任务/附件新标签页"来处理，也不要主动关闭——实测关闭它会阻塞数秒。
_INTERNAL_SCHEMES = ("edge", "chrome", "about", "devtools", "view-source", "brave", "opera", "vivaldi")

# file_parser 返回的错误串前缀（这类内容绝不能送进 LLM）
_PARSE_ERROR_PREFIXES = ("解析失败", "不支持的文件类型", "WPS读取失败")

# 验证码/二次验证特征选择器（CSS）。注意：这里不依赖 CSS 的 ``i`` 大小写修饰符
# （部分选择器引擎支持不一致），大小写不敏感的部分交给下面的文本关键词兜底。
_CAPTCHA_SELECTORS = (
    'iframe[src*="captcha"]',
    'iframe[src*="verify"]',
    'img[src*="captcha"]',
    'img[src*="verify"]',
    'input[name*="captcha"]',
    'input[id*="captcha"]',
    'input[name*="verify"]',
    '[class*="captcha"]',
    '[id*="captcha"]',
)
_CAPTCHA_KEYWORDS = ("验证码", "安全验证", "两步验证", "二次验证", "mfa", "captcha", "verification code")

# 审计日志/错误文本的敏感字段（值统一替换为 ***）
_SENSITIVE_RE = re.compile(
    r"(?i)\b(token|access[_-]?token|refresh[_-]?token|password|passwd|pwd|secret|"
    r"api[_-]?key|apikey|key|signature|sig|ticket|auth|authorization|cookie|session[_-]?id)"
    r"(\s*[=:]\s*)"
    r"([^\s&;,\"'\\)]{1,200})"
)

# 送进 LLM 的正文上限（防止超长附件把上下文撑爆；分段摘要属于另一条链路）
_MAX_CHARS_PER_FILE = 8000
_MAX_CHARS_TOTAL = 20000

# 分页保护：最多翻多少页，防止"下一页"永远可点导致死循环
_MAX_PAGES = 50

# 单个任务最多接受的附件数量（防止详情页有几百个链接时跑飞）
_MAX_FILES_PER_TASK = 20

_SYSTEM_PROMPT = (
    "你是 EchoDesk 的文档审核助手。请依据用户给出的任务指令与附件正文："
    "先提炼关键信息（数字、日期、金额、当事方、义务、结论），再给出审核意见。"
    "使用简体中文，只依据文件内容作答，不要编造；材料不足或不确定的地方要明确指出。"
)


class AutomationError(Exception):
    """自动化流程中可直接展示给用户的错误。"""


class _FileTooLarge(Exception):
    """附件超过大小上限（内部信号，不对外暴露）。"""


# ---------- 小工具 ----------


def _now_iso() -> str:
    """当前时间的 ISO 字符串（秒级）。"""
    return datetime.now().isoformat(timespec="seconds")


def _stamp() -> str:
    """文件名用的紧凑时间戳。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _today() -> str:
    """结果目录用的日期（YYYY-MM-DD，任务书 新增三）。"""
    return datetime.now().strftime("%Y-%m-%d")


def _clip(text: Any, limit: int) -> str:
    """截断文本（用于进度信息与展示，不用于日志脱敏）。"""
    s = "" if text is None else str(text)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


def _redact(text: Any, limit: int = 200) -> str:
    """脱敏 + 截断：把 token=/password=/secret=/key= 之类的值替换为 ***。

    审计日志与错误摘要都走这里，确保凭据不落盘；文件正文与模型输出正文
    从不经过日志路径（调用方只传文件名与状态）。
    """
    if text is None:
        return ""
    s = str(text)
    s = _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", s)
    # URL 里的用户信息（https://user:pass@host）同样抹掉
    s = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1***@", s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


def _selector_kind(selector: str) -> str:
    """判定选择器类型：以 ``//``、``(``、``xpath=`` 开头为 XPath，其余为 CSS。"""
    s = (selector or "").strip().lower()
    return "xpath" if s.startswith(_XPATH_PREFIXES) else "css"


def _normalize_selector(selector: str) -> str:
    """把选择器转成 Playwright 能直接吃的形式。

    Playwright 会自动把 ``//`` 开头的字符串当 XPath，但 ``(//a)[1]`` 这种
    带括号的表达式不会，因此显式补 ``xpath=`` 前缀。
    """
    s = (selector or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith("xpath="):
        return s
    if s.startswith("//") or s.startswith("("):
        return "xpath=" + s
    return s


def _safe_filename(name: str, fallback: str = "download") -> str:
    """清理文件名中的非法字符（Windows 文件名限制），保留原后缀。"""
    s = unquote(os.path.basename((name or "").strip()))
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    s = s.strip().strip(".")
    if len(s) > 120:
        stem, ext = os.path.splitext(s)
        s = stem[: 120 - len(ext)] + ext
    return s or fallback


def _unique_path(directory: str, name: str) -> str:
    """在目录内生成不冲突的路径：同名时追加 ``-1``、``-2``。"""
    base = _safe_filename(name)
    stem, ext = os.path.splitext(base)
    candidate = os.path.join(directory, base)
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}-{index}{ext}")
        index += 1
        if index > 1000:  # 理论上到不了，兜底防死循环
            candidate = os.path.join(directory, f"{stem}-{_stamp()}{ext}")
            break
    return candidate


def _is_internal_url(url: str) -> bool:
    """是否为浏览器内部页面（edge://、chrome://、about: 等）。

    这些页面是浏览器自身的界面（例如 Edge 下载后自动打开的下载中心），既不该被当作
    任务/附件的新标签页继续处理，也不该由我们去关闭：实测关闭内部页面会阻塞数秒。
    """
    scheme = (urlparse(url or "").scheme or "").lower()
    return (not scheme) or scheme in _INTERNAL_SCHEMES


def _extension_of(value: str) -> str:
    """取后缀：小写、去前导点、忽略 query/fragment。"""
    if not value:
        return ""
    try:
        path = urlparse(value).path or value
    except Exception:  # noqa: BLE001
        path = value
    ext = os.path.splitext(path.split("?")[0].split("#")[0])[1]
    return ext.lstrip(".").lower()


def _local_path_from_url(url: str) -> str:
    """把 ``file://`` URL 还原为本地路径（Windows 盘符与 UNC 都处理）。"""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return ""
    if parsed.scheme and parsed.scheme.lower() != "file":
        return ""
    path = url2pathname(parsed.path or "")
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    return path


def _is_parse_error(text: str) -> bool:
    """file_parser 的返回值是否为错误串（错误串不能送进 LLM）。"""
    if not text or not str(text).strip():
        return True
    return str(text).lstrip().startswith(_PARSE_ERROR_PREFIXES)


@dataclass
class _LinkEntry:
    """页面上的一个链接（任务链接或附件链接）。"""

    text: str = ""
    href: str = ""
    handle: Any = None      # ElementHandle；None 表示来自 Shadow DOM 穿透，不可点击
    frame: Any = None

    @property
    def key(self) -> str:
        return self.href or self.text


@dataclass
class _TaskOutcome:
    """单个任务的处理结果。"""

    status: str = "failed"          # ok | failed
    summary: str = ""               # 模型输出正文（仅用于回传前端/写结果文件，不入日志）
    files: List[str] = field(default_factory=list)   # 落地到任务目录后的文件名
    error: str = ""
    result_path: str = ""           # 结果文件落盘路径（结果.txt）
    screenshot: str = ""            # 失败截图路径（任务书 新增三：截图留在任务目录内）


# ---------- 主引擎 ----------


class AutomationEngine:
    """浏览器自动化引擎。

    ``cfg`` 是 backend/config.py 里的 ``AutomationConfig`` 实例；所有字段都通过
    ``getattr`` 读取并带默认值，便于配置缺字段时仍可运行、也便于单测传入简单对象。
    """

    def __init__(self, cfg: Any, llm_engine: Any = None, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.llm = llm_engine
        self.logger = logger or log
        self._lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._stop = threading.Event()
        self._active_stop: Optional[Any] = None
        self._running = False
        self._last_run = ""
        self._last_error = ""
        self._processed_count = 0
        self._result_count = 0
        # 本轮结果目录与结果块（新增三：结果.txt 为纯文本，每条任务完成即重写落盘）
        self._run_task_dir = ""
        self._report_header = ""
        self._report_blocks: List[str] = []
        # 最近一次选择器解析失败的原因（用于把"选择器写错"和"页面里没有元素"区分开）
        self._selector_error = ""
        # 技能自动化钩子分发器（v0.2.1 任务二 2.3）：由 main 注入 SkillLoader.fire_hook
        self.hook_dispatcher: Optional[Callable[[str, Dict[str, Any]], Any]] = None
        # 是否处于测试向导的试运行（不写结果文件与处理记录）
        self._dry_run = False

    def _fire(self, stage: str, **context: Any) -> List[Dict[str, Any]]:
        """触发技能挂接的自动化钩子；钩子出错只记日志，绝不中断自动化本身。"""
        if self.hook_dispatcher is None:
            return []
        try:
            return list(self.hook_dispatcher(stage, context) or [])
        except Exception:  # noqa: BLE001
            self.logger.warning("自动化钩子 %s 分发失败", stage, exc_info=True)
            return []

    # ---------- 配置读取（全部容错） ----------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False))

    @property
    def safety_accepted(self) -> bool:
        return bool(getattr(self.cfg, "safety_accepted", False))

    @property
    def safety_version(self) -> str:
        return str(getattr(self.cfg, "safety_version", "1.0") or "")

    @property
    def safety_accepted_at(self) -> str:
        return str(getattr(self.cfg, "safety_accepted_at", "") or "")

    @property
    def task_list_url(self) -> str:
        return str(getattr(self.cfg, "task_list_url", "") or "").strip()

    @property
    def page_timeout_ms(self) -> int:
        try:
            value = int(getattr(self.cfg, "page_timeout_ms", 30000) or 30000)
        except Exception:  # noqa: BLE001
            value = 30000
        return max(3000, value)

    @property
    def file_extensions(self) -> List[str]:
        raw = getattr(self.cfg, "file_extensions", None) or []
        if isinstance(raw, str):
            raw = raw.replace("，", ",").split(",")
        out: List[str] = []
        for item in raw:
            ext = str(item).strip().lstrip(".").lower()
            if ext and ext not in out:
                out.append(ext)
        return out

    @property
    def results_root(self) -> str:
        """结果根目录：``保存位置 / 顶层文件夹名``（新增三）。

        保存位置留空表示程序根目录；顶层文件夹名默认「批量处理结果」。
        """
        raw = str(getattr(self.cfg, "results_location", "") or "").strip()
        base = os.path.abspath(os.path.expanduser(raw)) if raw else _program_root()
        name = _safe_filename(str(getattr(self.cfg, "results_root_name", "") or "批量处理结果"), "批量处理结果")
        return os.path.join(base, name)

    @property
    def task_name(self) -> str:
        """本次运行的任务名称（第三层目录名）。"""
        return _safe_filename(str(getattr(self.cfg, "task_name", "") or "自动化任务"), "自动化任务")[:60]

    @property
    def results_dir(self) -> str:
        """本轮结果目录：``结果根目录/日期/任务名``；尚未开始运行时退化为结果根目录。"""
        if self._run_task_dir:
            return self._run_task_dir
        return os.path.join(self.results_root, _today())

    def _prepare_task_dir(self) -> str:
        """按 ``结果根目录/YYYY-MM-DD/任务名`` 创建本轮结果目录。

        同一日期下同名任务自动追加序号（``-1``、``-2``…）避免覆盖；
        同一天多次运行的结果合并到同一日期目录下。
        """
        day_dir = os.path.join(self.results_root, _today())
        os.makedirs(day_dir, exist_ok=True)
        base = self.task_name
        candidate = os.path.join(day_dir, base)
        suffix = 0
        while os.path.exists(candidate):
            suffix += 1
            candidate = os.path.join(day_dir, f"{base}-{suffix}")
        os.makedirs(candidate, exist_ok=True)
        return candidate

    @property
    def report_path(self) -> str:
        """本轮结果文件路径（固定命名 ``结果.txt``，纯文本无 Markdown）。"""
        return os.path.join(self.results_dir, "结果.txt")

    def _build_report_header(self) -> str:
        """结果文件的文件头：一眼看清这批结果属于哪次运行。"""
        return "\n".join(
            [
                "EchoDesk 批量处理结果",
                "=" * 40,
                f"任务名称：{self.task_name}",
                f"运行日期：{_today()}",
                f"开始时间：{_now_iso()}",
                f"任务列表：{self.task_list_url}",
                f"任务指令：{str(getattr(self.cfg, 'instruction', '') or '')}",
                f"结果目录：{self.results_dir}",
            ]
        )

    def _write_report(self, blocks: Sequence[str], header: str) -> str:
        """把结果写入 ``结果.txt``（纯文本）。

        采用「每次重写整份」的方式：单条任务完成后立即落盘，
        即使中途崩溃或按停止，已处理任务的结果也不会丢。
        """
        if not bool(getattr(self.cfg, "save_results", True)) or self._dry_run:
            return ""
        try:
            os.makedirs(self.results_dir, exist_ok=True)
            body = "\n\n".join(block for block in blocks if block.strip())
            content = header.rstrip() + "\n\n" + body + "\n" if body else header.rstrip() + "\n"
            with open(self.report_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return self.report_path
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("写入结果文件失败：%s", _redact(exc, 200))
            return ""

    def _move_into_task_dir(self, paths: Sequence[str]) -> List[str]:
        """把本轮下载的原始文件移动到任务目录（重名自动加序号），返回落地文件名列表。"""
        moved: List[str] = []
        try:
            os.makedirs(self.results_dir, exist_ok=True)
        except Exception:  # noqa: BLE001
            return moved
        for path in paths:
            try:
                if not os.path.isfile(path):
                    continue
                name = _safe_filename(os.path.basename(path), "file")
                target = _unique_path(self.results_dir, name)
                shutil.move(path, target)
                moved.append(os.path.basename(target))
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("保留下载文件失败：%s", _redact(exc, 150))
        return moved

    @property
    def download_dir(self) -> str:
        raw = str(getattr(self.cfg, "download_dir", "") or "").strip()
        if not raw:
            raw = os.path.join(tempfile.gettempdir(), "echodesk_downloads")
        return os.path.abspath(os.path.expanduser(raw))

    @property
    def processed_file(self) -> str:
        raw = str(getattr(self.cfg, "processed_file", "automation_processed.json") or "").strip()
        return os.path.abspath(os.path.expanduser(raw)) if raw else ""

    @property
    def login_state_file(self) -> str:
        raw = str(getattr(self.cfg, "login_state_file", "browser_state.json") or "").strip()
        return os.path.abspath(os.path.expanduser(raw)) if raw else ""

    @property
    def audit_log_file(self) -> str:
        raw = str(getattr(self.cfg, "audit_log_file", "automation_log.jsonl") or "").strip()
        return os.path.abspath(os.path.expanduser(raw)) if raw else ""

    # ---------- 校验 ----------

    def validate(self) -> List[str]:
        """返回配置校验错误列表（空列表 = 可以运行）。"""
        errors: List[str] = []
        if not self.safety_accepted:
            errors.append("尚未勾选安全协议（automation.safety_accepted=false），自动化不可运行。")
        if not self.enabled:
            errors.append("自动化总开关未开启（automation.enabled=false）。")
        if _sync_playwright is None:
            errors.append("未安装 playwright，请先执行 pip install playwright && playwright install chromium。")

        url = self.task_list_url
        if not url:
            errors.append("任务列表网址为空（automation.task_list_url）。")
        else:
            scheme = (urlparse(url).scheme or "").lower()
            if scheme not in ("http", "https", "file"):
                errors.append(f"任务列表网址协议不受支持：{scheme or '(空)'}（仅支持 http/https/file）。")
        if not str(getattr(self.cfg, "task_list_selector", "") or "").strip():
            errors.append("任务列表元素位置为空（automation.task_list_selector）。")
        if not str(getattr(self.cfg, "task_detail_file_selector", "") or "").strip():
            errors.append("任务详情页文件元素位置为空（automation.task_detail_file_selector）。")
        if not self.file_extensions:
            errors.append("未配置需要预览/下载的文件后缀（automation.file_extensions）。")
        try:
            if int(getattr(self.cfg, "max_tasks", 20) or 0) < 1:
                errors.append("最多处理任务数必须大于 0（automation.max_tasks）。")
        except Exception:  # noqa: BLE001
            errors.append("最多处理任务数不是合法整数（automation.max_tasks）。")
        if self.llm is None or not hasattr(self.llm, "chat_complete"):
            errors.append("未加载本地模型，无法生成摘要与审核意见（请先加载模型）。")
        return errors

    # ---------- 审计与去重 ----------

    def _audit(
        self,
        event: str,
        url: str = "",
        files: Optional[Sequence[str]] = None,
        result: str = "",
        error: str = "",
        **extra: Any,
    ) -> None:
        """追加一行审计日志（JSON Lines，脱敏）。

        只记录：时间 / 事件 / 任务链接 / **仅文件名** / 结果 / 截断错误 / 安全协议版本与
        同意时间，外加少量数值统计。文件正文、模型输出、Cookie、Token 一律不写入。
        """
        path = self.audit_log_file
        if not path:
            return
        record: Dict[str, Any] = {
            "time": _now_iso(),
            "event": event,
            "url": _redact(url, 300),
            "files": [_safe_filename(os.path.basename(str(f))) for f in (files or [])],
            "result": result,
            "error": _redact(error, 200),
            "safety_version": self.safety_version,
            "safety_accepted_at": self.safety_accepted_at,
        }
        for key, value in extra.items():
            # 只允许标量型统计字段，避免调用方顺手塞进正文
            if isinstance(value, (int, float, bool, str)) and value != "":
                record[key] = _redact(value, 120) if isinstance(value, str) else value
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with self._audit_lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 审计失败不能影响主流程
            self.logger.warning("写审计日志失败：%s", _redact(exc, 200))

    def _load_processed(self) -> Set[str]:
        """读取已处理任务标记（URL 哈希集合）。"""
        path = self.processed_file
        if not path or not os.path.isfile(path):
            return set()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("urls", []) if isinstance(data, dict) else data
            return {str(item) for item in (items or [])}
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("读取去重记录失败，按无记录处理：%s", _redact(exc, 200))
            return set()

    def _save_processed(self, urls: Set[str]) -> None:
        """写回去重标记（失败只记日志，不打断流程）。"""
        path = self.processed_file
        if not path:
            return
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"urls": sorted(urls)}, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("保存去重记录失败：%s", _redact(exc, 200))

    @staticmethod
    def _url_key(url: str) -> str:
        """去重键：完整 URL 的哈希（简单起见不做参数归一化）。"""
        return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:32]

    # ---------- 浏览器辅助 ----------

    def _event(self, type_: str, content: str, **extra: Any) -> Dict[str, Any]:
        """构造一个进度事件，并同步回调 on_progress（回调异常不影响主流程）。"""
        event: Dict[str, Any] = {"type": type_, "content": content}
        event.update(extra)
        callback = getattr(self, "_progress_cb", None)
        if callback is not None:
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("on_progress 回调异常：%s", _redact(exc, 120))
        return event

    def _launch(self, playwright: Any) -> Tuple[Any, str]:
        """启动有头浏览器；返回 (browser, 提示文本)。通道失败时回退默认 Chromium。"""
        channel = str(getattr(self.cfg, "browser_channel", "msedge") or "").strip()
        note = ""
        if channel:
            try:
                browser = playwright.chromium.launch(channel=channel, headless=False)
                return browser, ""
            except Exception as exc:  # noqa: BLE001
                note = f"浏览器通道 {channel} 启动失败（{_redact(exc, 120)}），已回退到默认 Chromium。"
                self.logger.warning(note)
        try:
            browser = playwright.chromium.launch(headless=False)
            return browser, note
        except Exception as exc:  # noqa: BLE001
            detail = f"{note}默认 Chromium 也启动失败：{_redact(exc, 200)}"
            raise AutomationError(
                f"无法启动浏览器：{detail}。"
                "请确认已安装 Microsoft Edge（automation.browser_channel=msedge），"
                "或执行 playwright install chromium 安装内置浏览器。"
            ) from exc

    def _new_context(self, browser: Any) -> Any:
        """创建上下文：1440x900、允许下载、尽量恢复上次登录态。"""
        kwargs: Dict[str, Any] = {
            "viewport": {"width": 1440, "height": 900},
            "accept_downloads": True,
        }
        state_file = self.login_state_file
        if state_file and os.path.isfile(state_file):
            kwargs["storage_state"] = state_file
        try:
            return browser.new_context(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 登录态文件损坏时丢弃它重试
            self.logger.warning("恢复登录态失败，改为全新会话：%s", _redact(exc, 200))
            kwargs.pop("storage_state", None)
            return browser.new_context(**kwargs)

    def _save_storage_state(self, context: Any) -> None:
        """持久化 Cookie/localStorage，供下次运行复用登录态。"""
        path = self.login_state_file
        if not path or context is None:
            return
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            context.storage_state(path=path)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("保存登录态失败：%s", _redact(exc, 200))

    def _wait_loaded(self, page: Any) -> None:
        """统一的等待策略：domcontentloaded → （可选）networkidle → 短暂停顿，全部容错。"""
        timeout = self.page_timeout_ms
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        if bool(getattr(self.cfg, "wait_network_idle", True)):
            try:
                page.wait_for_load_state("networkidle", timeout=timeout)
            except Exception:  # noqa: BLE001 - 长连接页面永远等不到空闲，忽略继续
                pass
        try:
            page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            time.sleep(0.3)

    def _goto(self, page: Any, url: str) -> None:
        """导航到指定地址（带超时与异常兜底）。"""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
        except _PWTimeoutError:
            self.logger.warning("页面加载超时（继续处理）：%s", _redact(url, 200))
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(f"打开页面失败：{_redact(exc, 200)}") from exc

    @staticmethod
    def _handle_text(handle: Any) -> str:
        """取元素可见文本（不可见时退回 textContent）。"""
        for getter in ("inner_text", "text_content"):
            try:
                value = getattr(handle, getter)()
                if value:
                    return " ".join(str(value).split())
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _handle_href(self, handle: Any, base_url: str) -> str:
        """取元素链接并补全为绝对地址（相对链接用 urljoin）。"""
        raw = ""
        try:
            raw = handle.get_attribute("href") or ""
        except Exception:  # noqa: BLE001
            raw = ""
        if not raw:
            try:
                raw = handle.evaluate(
                    "el => el.getAttribute('href') || el.href || "
                    "el.getAttribute('data-href') || el.getAttribute('data-url') || ''"
                ) or ""
            except Exception:  # noqa: BLE001
                raw = ""
        raw = str(raw).strip()
        if not raw:
            return ""
        try:
            return urljoin(base_url or "", raw)
        except Exception:  # noqa: BLE001
            return raw

    _SHADOW_JS = """
    (sel) => {
      const out = [];
      const visited = new Set();
      const walk = (root) => {
        let nodes;
        try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
        for (const el of nodes) {
          const sr = el.shadowRoot;
          if (!sr || visited.has(sr)) continue;
          visited.add(sr);
          try { sr.querySelectorAll(sel).forEach((m) => out.push(m)); } catch (e) {}
          walk(sr);
        }
      };
      walk(document);
      return out.map((m) => ({
        text: (m.innerText || m.textContent || '').trim(),
        href: m.getAttribute('href') || m.href || ''
      }));
    }
    """

    def _shadow_query(self, scope: Any, selector: str) -> List[Dict[str, str]]:
        """Shadow DOM 简单穿透：返回 [{text, href}]。

        只支持 CSS 选择器；命中的元素拿不到可点击句柄，因此调用方只能走
        "直接请求 URL"的下载兜底路径。
        """
        if _selector_kind(selector) != "css":
            return []
        try:
            items = scope.evaluate(self._SHADOW_JS, selector)
        except Exception:  # noqa: BLE001
            return []
        results: List[Dict[str, str]] = []
        for item in items or []:
            if isinstance(item, dict):
                results.append({"text": _clip(item.get("text"), 200), "href": str(item.get("href") or "")})
        return results

    def _collect_links(self, page: Any, selector: str, row_selector: str = "") -> List[_LinkEntry]:
        """定位链接元素：主文档 → 同源 iframe → Shadow DOM 简单穿透；按 href 去重。

        ``row_selector`` 非空时先定位行容器，再在行内查找目标链接（分页/行级定位场景）。
        选择器语法错误会被记录到 ``self._selector_error``，便于把"找不到元素"和
        "选择器写错了"区分开（两者对用户的处置方式完全不同）。
        """
        selector = (selector or "").strip()
        if not selector:
            return []
        self._selector_error = ""
        norm = _normalize_selector(selector)
        norm_row = _normalize_selector(row_selector) if (row_selector or "").strip() else ""
        entries: List[_LinkEntry] = []

        # 1) 主文档与同源 iframe：任何一个作用域找到就不再往下找，避免重复
        for frame in list(getattr(page, "frames", []) or []):
            found: List[_LinkEntry] = []
            try:
                rows: List[Any] = frame.locator(norm_row).all() if norm_row else []
                scopes: List[Any] = rows if rows else [frame]
            except Exception as exc:  # noqa: BLE001
                self._record_selector_error("行容器", norm_row, exc)
                continue
            for scope in scopes:
                try:
                    handles = scope.locator(norm).all()
                except Exception as exc:  # noqa: BLE001 - 语法错误在这里暴露
                    self._record_selector_error("元素", norm, exc)
                    continue
                base = ""
                try:
                    base = getattr(frame, "url", "") or page.url or ""
                except Exception:  # noqa: BLE001
                    base = ""
                for handle in handles:
                    found.append(
                        _LinkEntry(
                            text=_clip(self._handle_text(handle), 200),
                            href=self._handle_href(handle, base),
                            handle=handle,
                            frame=frame,
                        )
                    )
            if found:
                entries.extend(found)
                break

        # 2) Shadow DOM 简单穿透（命中元素无句柄，只能按 URL 走请求兜底）
        if not entries:
            for frame in list(getattr(page, "frames", []) or []):
                items = self._shadow_query(frame, selector)
                if items:
                    base = ""
                    try:
                        base = getattr(frame, "url", "") or page.url or ""
                    except Exception:  # noqa: BLE001
                        base = ""
                    for item in items:
                        href = item.get("href") or ""
                        try:
                            href = urljoin(base, href) if href else ""
                        except Exception:  # noqa: BLE001
                            pass
                        entries.append(_LinkEntry(text=_clip(item.get("text"), 200), href=href, handle=None, frame=frame))
                    break

        # 3) 去重（保留首次出现顺序）
        unique: List[_LinkEntry] = []
        seen: Set[str] = set()
        for entry in entries:
            key = entry.key
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        return unique

    # ---------- 验证码/二次验证 ----------

    def _captcha_present(self, page: Any) -> bool:
        """页面是否出现验证码/二次验证特征。"""
        for frame in list(getattr(page, "frames", []) or []):
            for sel in _CAPTCHA_SELECTORS:
                try:
                    if frame.locator(sel).count() > 0:
                        return True
                except Exception:  # noqa: BLE001
                    continue
        try:
            text = page.inner_text("body", timeout=3000) or ""
        except Exception:  # noqa: BLE001
            try:
                text = page.content() or ""
            except Exception:  # noqa: BLE001
                text = ""
        low = text.lower()
        return any(keyword in low for keyword in _CAPTCHA_KEYWORDS)

    def _wait_manual(self, page: Any, stop_fn: Callable[[], bool]) -> Iterator[Dict[str, Any]]:
        """检测到验证码/二次验证时暂停，等用户手动处理；超时返回 False。

        作为生成器使用：``ok = yield from self._wait_manual(page, stop_fn)``。
        """
        if not self._captcha_present(page):
            return True
        limit = 0
        try:
            limit = max(0, int(getattr(self.cfg, "manual_login_wait_seconds", 180) or 0))
        except Exception:  # noqa: BLE001
            limit = 180
        yield self._event(
            "delta",
            f"检测到验证码/二次验证，请在弹出的浏览器窗口中手动完成，程序将等待最多 {limit} 秒…",
        )
        self._audit("manual_required", url=self.task_list_url, result="waiting", error="captcha_or_mfa")
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if stop_fn():
                return False
            try:
                page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                time.sleep(2)
            if not self._captcha_present(page):
                yield self._event("delta", "人工验证已完成，继续执行。")
                return True
        yield self._event("delta", f"等待人工验证超过 {limit} 秒，本任务记为失败并继续下一个。")
        return False

    # ---------- 下载与解析 ----------

    def _save_download(self, download: Any, dest_dir: str) -> str:
        """保存浏览器下载对象到临时目录（含大小限制）。"""
        name = _safe_filename(getattr(download, "suggested_filename", "") or "download")
        path = _unique_path(dest_dir, name)
        download.save_as(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > MAX_FILE_BYTES:
            try:
                os.remove(path)
            except OSError:
                pass
            raise _FileTooLarge(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB（{size} 字节），已跳过")
        return path

    @staticmethod
    def _click(handle: Any) -> None:
        """点击元素，逐级降级（普通点击 → 强制点击 → JS 点击）。"""
        try:
            handle.click(timeout=15000)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            handle.click(timeout=15000, force=True)
            return
        except Exception:  # noqa: BLE001
            pass
        handle.evaluate("el => el.click()")

    def _record_selector_error(self, what: str, selector: str, exc: Exception) -> None:
        """记录选择器解析/查询失败（只保留第一次，供用户提示用）。"""
        if getattr(self, "_selector_error", ""):
            return
        self._selector_error = f"{what}选择器 {selector} 无法解析：{_redact(exc, 200)}"
        self.logger.warning("选择器查询失败：%s", _redact(self._selector_error, 200))

    @staticmethod
    def _new_pages(context: Any, known: Sequence[Any]) -> List[Any]:
        """列出上下文里"新增且属于目标站点"的标签页（忽略浏览器内部页面）。

        只比较 http(s)/file 页面，避免把 Edge 下载中心（edge://downloads-hub/）之类
        的界面页误判成"点击链接打开的新标签页"。
        """
        fresh: List[Any] = []
        for candidate in list(getattr(context, "pages", []) or []):
            if candidate in known:
                continue
            try:
                url = getattr(candidate, "url", "") or ""
            except Exception:  # noqa: BLE001
                url = ""
            if _is_internal_url(url):
                continue
            fresh.append(candidate)
        return fresh

    def _safe_close(self, page: Any) -> None:
        """尽力关闭一个标签页（内部页面不关，异常与超时都吞掉）。"""
        if page is None:
            return
        try:
            if _is_internal_url(getattr(page, "url", "") or ""):
                return
            page.close()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("关闭标签页失败：%s", _redact(exc, 120))

    def _name_from_response(self, response: Any, url: str, hint: str = "") -> str:
        """从响应头/URL 推断文件名。"""
        name = ""
        try:
            disposition = response.headers.get("content-disposition", "") or ""
            match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
            if match:
                name = unquote(match.group(1))
        except Exception:  # noqa: BLE001
            name = ""
        if not name:
            name = os.path.basename(urlparse(url).path or "")
        if not name or not _extension_of(name):
            if hint and _extension_of(hint):
                name = hint
        return _safe_filename(name or hint or "download")

    def _fetch_by_request(self, context: Any, url: str, dest_dir: str, hint: str = "") -> Tuple[Optional[str], str]:
        """兜底下载：用上下文自带请求客户端直接取文件（共享 Cookie）。"""
        if not url:
            return None, "链接为空，无法直接请求"
        try:
            response = context.request.get(url, timeout=self.page_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return None, f"直接请求失败：{_redact(exc, 150)}"
        try:
            if not response.ok:
                return None, f"直接请求失败：HTTP {response.status}"
            body = response.body()
        except Exception as exc:  # noqa: BLE001
            return None, f"读取响应失败：{_redact(exc, 150)}"
        if body is None:
            return None, "响应为空"
        if len(body) > MAX_FILE_BYTES:
            return None, f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB，已跳过"
        name = self._name_from_response(response, url, hint)
        path = _unique_path(dest_dir, name)
        try:
            with open(path, "wb") as fh:
                fh.write(body)
        except Exception as exc:  # noqa: BLE001
            return None, f"写临时文件失败：{_redact(exc, 150)}"
        return path, ""

    def _download_entry(self, context: Any, page: Any, entry: _LinkEntry, dest_dir: str) -> Tuple[Optional[str], str]:
        """下载一个附件链接，返回 (本地路径, 错误说明)。

        顺序：file:// 本地复制 → 点击触发 Playwright 下载 → 新标签页兜底 → 直接请求兜底。
        """
        url = entry.href or ""
        hint = _safe_filename(entry.text or "", fallback="")
        # 1) file:// 协议：浏览器下载事件常不触发，直接复制本地文件（本地自测/内网离线场景）
        if url and (urlparse(url).scheme or "").lower() == "file":
            source = _local_path_from_url(url)
            if not source or not os.path.isfile(source):
                return None, f"本地文件不存在：{_redact(source or url, 150)}"
            dest = _unique_path(dest_dir, _safe_filename(os.path.basename(source), "download"))
            try:
                shutil.copy2(source, dest)
            except Exception as exc:  # noqa: BLE001
                return None, f"复制本地文件失败：{_redact(exc, 150)}"
            try:
                if os.path.getsize(dest) > MAX_FILE_BYTES:
                    os.remove(dest)
                    return None, f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB，已跳过"
            except OSError:
                pass
            return dest, ""

        # 2) 正常路径：点击链接并捕获下载事件
        click_error = ""
        if entry.handle is not None:
            pages_before = list(getattr(context, "pages", []) or [])
            try:
                with page.expect_download(timeout=self.page_timeout_ms) as info:
                    self._click(entry.handle)
                return self._save_download(info.value, dest_dir), ""
            except _FileTooLarge as exc:
                return None, str(exc)
            except Exception as exc:  # noqa: BLE001
                click_error = _redact(exc, 150)
                # 3) 有些站点在新标签页里才触发下载：等价于 context.expect_page 的等待语义
                new_page = None
                for candidate in self._new_pages(context, pages_before):
                    new_page = candidate
                    break
                if new_page is None:
                    try:
                        candidate = context.wait_for_event("page", timeout=3000)
                    except Exception:  # noqa: BLE001
                        candidate = None
                    if candidate is not None and not _is_internal_url(getattr(candidate, "url", "")):
                        new_page = candidate
                    elif candidate is not None:
                        # 浏览器内部页面（如 Edge 下载中心）不是我们要找的新标签页
                        self._safe_close(candidate)
                if new_page is not None:
                    try:
                        target = getattr(new_page, "url", "") or url
                        path, err = self._fetch_by_request(context, target, dest_dir, hint)
                        return path, err
                    finally:
                        self._safe_close(new_page)

        # 4) 最后兜底：直接请求链接本身
        if url and (urlparse(url).scheme or "").lower() in ("http", "https"):
            return self._fetch_by_request(context, url, dest_dir, hint)
        return None, f"下载失败：{click_error or '未能触发下载'}"

    def _parse_downloaded(self, path: str) -> Tuple[str, str]:
        """解析下载的文件，返回 (正文, 错误说明)；错误串绝不返回给 LLM。"""
        global _parse_file_with_wps
        if _parse_file_with_wps is None:
            try:
                from file_parser import parse_file_with_wps as parser  # noqa: WPS433
                _parse_file_with_wps = parser
            except Exception as exc:  # noqa: BLE001
                return "", f"文件解析模块不可用：{_redact(exc, 150)}"
        try:
            text = _parse_file_with_wps(path, 0)
        except Exception as exc:  # noqa: BLE001
            return "", f"解析异常：{_redact(exc, 150)}"
        if _is_parse_error(text):
            return "", f"解析失败：{_redact(text, 150)}"
        return str(text), ""

    # ---------- LLM 与结果落盘 ----------

    def _build_messages(self, docs: Sequence[Tuple[str, str]]) -> List[Dict[str, str]]:
        """拼接任务指令与附件正文（带长度上限，避免撑爆上下文）。"""
        instruction = str(getattr(self.cfg, "instruction", "") or "").strip() or "（未填写任务指令）"
        parts: List[str] = ["任务指令：", instruction, "", "以下为任务附件内容（已按长度上限截断）："]
        used = 0
        for name, text in docs:
            body = text
            if len(body) > _MAX_CHARS_PER_FILE:
                body = body[:_MAX_CHARS_PER_FILE] + "\n…（本文件内容过长，已截断）"
            if used + len(body) > _MAX_CHARS_TOTAL:
                body = body[: max(0, _MAX_CHARS_TOTAL - used)] + "\n…（附件总长度已达上限，后续内容省略）"
            parts.extend(["", f"===== 文件：{name} =====", body])
            used += len(body)
            if used >= _MAX_CHARS_TOTAL:
                break
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]

    def _generate(self, docs: Sequence[Tuple[str, str]]) -> str:
        """调用本地模型生成摘要与审核意见。"""
        if self.llm is None or not hasattr(self.llm, "chat_complete"):
            raise AutomationError("未加载本地模型，无法生成审核意见。")
        messages = self._build_messages(docs)
        temperature = float(getattr(self.cfg, "llm_temperature", 0.3) or 0.3)
        max_tokens = int(getattr(self.cfg, "llm_max_tokens", 512) or 512)
        try:
            try:
                text = self.llm.chat_complete(messages, temperature=temperature, max_tokens=max_tokens)
            except TypeError:
                # 兼容只接受 messages 的实现/测试替身
                text = self.llm.chat_complete(messages)
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(f"模型生成失败：{_redact(exc, 200)}") from exc
        if not text or not str(text).strip():
            raise AutomationError("模型返回为空。")
        return str(text).strip()

    def _save_result(
        self,
        title: str,
        url: str,
        files: Sequence[str],
        summary: str,
        index: int = 0,
        total: int = 0,
    ) -> str:
        """记录单个任务的成功结果并刷新 ``结果.txt``，返回结果文件路径。

        任务书 新增三：结果文件固定命名 ``结果.txt``，且必须是**纯文本**
        （记事本无 Markdown 渲染能力），因此这里用 ``markdown_to_plain`` 剥离标记。
        """
        if not bool(getattr(self.cfg, "save_results", True)) or self._dry_run:
            return ""
        prefix = f"[{index}/{total}] " if index and total else ""
        block = [
            f"{prefix}{title or '任务'}",
            "-" * max(8, min(len(f"{prefix}{title or '任务'}"), 40)),
            f"任务链接：{url}",
            f"处理时间：{_now_iso()}",
            f"附件：{'、'.join(files) if files else '无'}",
            "",
            "审核意见：",
            _markdown_to_plain(summary).strip(),
        ]
        self._report_blocks.append("\n".join(block))
        return self._write_report(self._report_blocks, self._report_header)

    def _save_failure(
        self,
        title: str,
        url: str,
        files: Sequence[str],
        error: str,
        index: int = 0,
        total: int = 0,
        screenshot: str = "",
    ) -> str:
        """把失败任务的信息（含失败截图文件名）写入 ``结果.txt``（任务书 新增三）。"""
        if not bool(getattr(self.cfg, "save_results", True)) or self._dry_run:
            return ""
        prefix = f"[{index}/{total}] " if index and total else ""
        head = f"{prefix}{title or '任务'}（失败）"
        lines = [
            head,
            "-" * max(8, min(len(head), 40)),
            f"任务链接：{url}",
            f"处理时间：{_now_iso()}",
            f"附件：{'、'.join(files) if files else '无'}",
            f"失败原因：{error or '未知错误'}",
        ]
        if screenshot:
            lines.append(f"失败截图：{os.path.basename(screenshot)}")
        self._report_blocks.append("\n".join(lines))
        return self._write_report(self._report_blocks, self._report_header)

    def _save_summary_block(self, header: str) -> str:
        """把本轮汇总写入结果文件末尾。"""
        if not bool(getattr(self.cfg, "save_results", True)) or self._dry_run:
            return ""
        self._report_blocks.append(_markdown_to_plain(header).strip())
        return self._write_report(self._report_blocks, self._report_header)

    def _screenshot(self, page: Any, index: int) -> str:
        """失败截图保存到本轮任务目录内（任务书 新增三），返回路径（失败返回空串）。"""
        try:
            os.makedirs(self.results_dir, exist_ok=True)
            path = _unique_path(self.results_dir, f"失败截图_任务{index}_{_stamp()}.png")
            try:
                page.screenshot(path=path, full_page=True, timeout=10000)
            except Exception:  # noqa: BLE001
                page.screenshot(path=path, timeout=10000)
            return path
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("失败截图未保存：%s", _redact(exc, 150))
            return ""

    # ---------- test_selector ----------

    def test_selector(self, url: str, selector: str, limit: int = 5) -> dict:
        """在真实页面里测试选择器，返回匹配数量、样本文本与链接。

        无论成功失败都会关闭浏览器；返回结构固定为
        ``{"ok", "count", "samples", "hrefs", "error", "kind"}``。
        """
        kind = _selector_kind(selector)
        result: Dict[str, Any] = {"ok": False, "count": 0, "samples": [], "hrefs": [], "error": "", "kind": kind}
        selector = (selector or "").strip()
        if not selector:
            result["error"] = "选择器为空。"
            return result
        if not (url or "").strip():
            result["error"] = "网址为空。"
            return result
        if _sync_playwright is None:
            result["error"] = "未安装 playwright。"
            return result

        browser = playwright = None
        try:
            playwright = _sync_playwright().start()
            browser, _ = self._launch(playwright)
            context = self._new_context(browser)
            page = context.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            self._goto(page, url.strip())
            self._wait_loaded(page)
            # 先做一次选择器语法探测：count() 对语法错误会直接抛错，对"无匹配"返回 0
            try:
                page.locator(_normalize_selector(selector)).count()
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["error"] = f"选择器语法错误：{_redact(exc, 250)}"
                return result
            entries = self._collect_links(page, selector)
            take = max(0, int(limit)) if str(limit).lstrip("-").isdigit() else 5
            result["count"] = len(entries)
            result["samples"] = [_clip(entry.text, 120) for entry in entries[:take]]
            result["hrefs"] = [entry.href or "" for entry in entries[:take]]
            result["ok"] = True
            if not entries:
                result["error"] = self._selector_error or "页面已打开，但没有匹配到元素。"
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["error"] = _redact(exc, 300)
            self.logger.warning("选择器测试失败：%s", _redact(exc, 200))
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        return result

    # ---------- 可视化选取（v0.2.1 任务书 4.2） ----------

    #: 注入页面的拾取脚本。悬停高亮并实时预览选择器，点击即确定，Esc 取消。
    PICKER_JS = r"""
(() => {
  if (window.__echodeskPickerInstalled) {
    window.__echodeskPick = {picked: null, cancelled: false, hover: null};
    return 'reinstalled';
  }
  window.__echodeskPickerInstalled = true;
  const state = window.__echodeskPick = {picked: null, cancelled: false, hover: null};

  const overlay = document.createElement('div');
  overlay.setAttribute('data-echodesk', 'overlay');
  overlay.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;'
    + 'border:2px solid #ff5c5c;background:rgba(255,92,92,0.14);box-sizing:border-box;';
  const tip = document.createElement('div');
  tip.setAttribute('data-echodesk', 'tip');
  tip.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;background:#1f2430;'
    + 'color:#fff;font:12px/1.6 Consolas,monospace;padding:3px 8px;border-radius:4px;'
    + 'max-width:80vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
    + 'box-shadow:0 2px 8px rgba(0,0,0,.35);';
  const banner = document.createElement('div');
  banner.setAttribute('data-echodesk', 'banner');
  banner.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;'
    + 'background:#2b6cb0;color:#fff;font:13px/2 sans-serif;padding:0 12px;text-align:center;'
    + 'pointer-events:none;';
  banner.textContent = 'EchoDesk 可视化选取：把鼠标移到要抓取的元素上并点击；按 Esc 取消';
  const add = () => {
    document.documentElement.appendChild(overlay);
    document.documentElement.appendChild(tip);
    document.documentElement.appendChild(banner);
  };
  const cleanup = () => {
    [overlay, tip, banner].forEach(n => { try { n.remove(); } catch (e) {} });
  };
  add();

  function esc(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, ch => '\\' + ch);
  }
  function unique(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }
  function selectorFor(el) {
    if (!el || el.nodeType !== 1) return '';
    if (el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
    const attrs = el.attributes || [];
    for (let i = 0; i < attrs.length; i++) {
      const a = attrs[i];
      if (a.name.indexOf('data-') === 0 && a.value) {
        const sel = el.tagName.toLowerCase() + '[' + a.name + '="' + a.value.replace(/"/g, '\\"') + '"]';
        if (unique(sel)) return sel;
      }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node.id && unique('#' + esc(node.id))) { parts.unshift('#' + esc(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.prototype.filter.call(parent.children, c => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      if (node.classList && node.classList.length) part += '.' + esc(node.classList[0]);
      parts.unshift(part);
      const candidate = parts.join(' > ');
      if (unique(candidate)) return candidate;
      node = parent;
    }
    return parts.join(' > ');
  }
  function label(el) {
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 50);
    return el.tagName.toLowerCase() + (text ? ' 「' + text + '」' : '');
  }

  let current = null;
  document.addEventListener('mousemove', e => {
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    if (el.hasAttribute && el.hasAttribute('data-echodesk')) return;
    if (el === current) return;
    current = el;
    const r = el.getBoundingClientRect();
    overlay.style.left = r.left + 'px';
    overlay.style.top = r.top + 'px';
    overlay.style.width = r.width + 'px';
    overlay.style.height = r.height + 'px';
    const sel = selectorFor(el);
    let count = 0;
    try { count = document.querySelectorAll(sel).length; } catch (err) { count = -1; }
    state.hover = {selector: sel, count: count, tag: el.tagName.toLowerCase()};
    tip.textContent = sel + '　匹配 ' + count + ' 个　' + label(el);
    tip.style.left = Math.max(4, Math.min(r.left, window.innerWidth - 420)) + 'px';
    tip.style.top = Math.max(30, Math.min(r.bottom + 6, window.innerHeight - 28)) + 'px';
  }, true);

  document.addEventListener('click', e => {
    e.preventDefault();
    e.stopPropagation();
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    const sel = selectorFor(el);
    let count = 0;
    try { count = document.querySelectorAll(sel).length; } catch (err) { count = 0; }
    state.picked = {
      selector: sel,
      count: count,
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300),
      href: (el.getAttribute && (el.getAttribute('href') || '')) || ''
    };
    cleanup();
  }, true);

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { state.cancelled = true; cleanup(); }
  }, true);
  return 'installed';
})();
"""

    def pick_selector(
        self,
        url: str,
        target: str = "task_list",
        *,
        timeout_seconds: int = 300,
        stop_event: Any = None,
    ) -> Iterator[Dict[str, Any]]:
        """可视化选取选择器（任务书 4.2）：在真实页面上点一下元素，生成 CSS 选择器。

        为什么用 Playwright 驱动本机 Edge 而不是内嵌 WebView2（对任务书的偏差，已记入
        docs/v0.2.1-研发记录.md）：

        * WebView2 需要额外引入 SDK 与系统运行时，与"尽量减小打包体积"的全局要求冲突；
        * 任务书要求"登录态与自动化共享（复用 browser_state.json）"——
          WebView2 有自己的用户数据目录，做不到；Playwright 这套直接用同一份
          ``browser_state.json``，登录态天然一致；
        * 自动化本来就用 Playwright 启动 Edge，复用同一引擎不增加任何新依赖，
          且选择器在同一个渲染引擎下生成与验证，结果更可靠。

        ``target`` 取值：``task_list``（任务列表元素）/ ``task_row``（任务行容器）/
        ``next_page``（下一页按钮）/ ``task_detail_file``（详情页附件）。
        后者会先用任务列表选择器点进第一个任务，再开始拾取。
        """
        yield self._event("delta", "正在启动浏览器（有头模式，稍后请在页面上点击目标元素）…")
        if _sync_playwright is None:
            yield self._event("error", "未安装 playwright，无法使用可视化选取。")
            return
        url = (url or "").strip() or self.task_list_url
        if not url:
            yield self._event("error", "请先填写任务列表网址。")
            return

        playwright = browser = context = page = None
        try:
            playwright = _sync_playwright().start()
            browser, note = self._launch(playwright)
            if note:
                yield self._event("delta", note)
            context = self._new_context(browser)   # 复用 browser_state.json 的登录态
            page = context.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            yield self._event("delta", f"正在打开 {url} …")
            self._goto(page, url)
            self._wait_loaded(page)

            if target == "task_detail_file":
                selector = str(getattr(self.cfg, "task_list_selector", "") or "").strip()
                if not selector:
                    yield self._event("error", "详情页附件选取需要先配置「任务列表元素位置」，否则无法进入任务详情。")
                    return
                yield self._event("delta", "正在用任务列表选择器进入第一个任务…")
                entries = self._collect_links(page, selector,
                                             str(getattr(self.cfg, "task_row_selector", "") or ""))
                if not entries:
                    yield self._event("error", "任务列表选择器没有匹配到任何任务，无法进入详情页。"
                                               "请先用「可视化选取」配置任务列表元素位置。")
                    return
                first = entries[0]
                if not first.href:
                    yield self._event("error", f"第一个任务「{_clip(first.text, 40)}」没有可用链接。")
                    return
                self._goto(page, first.href)
                self._wait_loaded(page)
                yield self._event("delta", f"已进入详情页：{_clip(first.text, 40)}")

            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            page.evaluate(self.PICKER_JS)
            yield self._event(
                "delta",
                f"请在弹出的 Edge 窗口里点击目标元素（{_target_label(target)}）。"
                f"页面顶部有蓝色提示条，悬停时会显示选择器预览；按 Esc 取消。",
            )

            deadline = time.time() + max(10, int(timeout_seconds))
            picked: Optional[Dict[str, Any]] = None
            cancelled = False
            while time.time() < deadline:
                if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                    cancelled = True
                    break
                if self._stop.is_set():
                    cancelled = True
                    break
                try:
                    state = page.evaluate(
                        "() => { const s = window.__echodeskPick; if (!s) return null;"
                        " return s.picked ? {picked: s.picked} : (s.cancelled ? {cancelled: true} : null); }"
                    )
                except Exception:  # noqa: BLE001 —— 页面被关掉/跳转时视为取消
                    state = {"cancelled": True}
                if isinstance(state, dict):
                    if state.get("picked"):
                        picked = state["picked"]
                        break
                    if state.get("cancelled"):
                        cancelled = True
                        break
                time.sleep(0.3)

            if picked is None:
                reason = "已取消" if cancelled else f"等待超时（{timeout_seconds} 秒）"
                yield self._event("error", f"可视化选取{reason}，未修改配置。")
                return

            selector = str(picked.get("selector") or "").strip()
            count = int(picked.get("count") or 0)
            yield self._event(
                "done",
                f"✅ 已生成选择器：{selector}\n匹配 {count} 个元素"
                + (f"\n元素文本：{_clip(picked.get('text'), 120)}" if picked.get("text") else ""),
                selector=selector,
                count=count,
                target=target,
                text=str(picked.get("text") or "")[:300],
                href=str(picked.get("href") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            message = f"可视化选取失败：{_redact(exc, 300)}"
            self.logger.warning("可视化选取失败：%s", _redact(exc, 200))
            yield self._event("error", message)
        finally:
            try:
                if context is not None:
                    self._save_storage_state(context)   # 把新登录态写回 browser_state.json
            except Exception:  # noqa: BLE001
                pass
            for closer in (lambda: browser.close() if browser else None,
                           lambda: playwright.stop() if playwright else None):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 分步测试向导（v0.2.1 任务书 4.5） ----------

    def test_element(self, url: str, selector: str, *, click: bool = False) -> Dict[str, Any]:
        """测试任意元素选择器（不限于链接），可选点击一次并观察页面是否跳转。

        返回 ``{"ok", "count", "visible", "samples", "clicked", "new_url", "changed", "error"}``。
        """
        result: Dict[str, Any] = {
            "ok": False, "count": 0, "visible": 0, "samples": [], "clicked": False,
            "new_url": "", "changed": False, "error": "", "kind": _selector_kind(selector),
        }
        selector = (selector or "").strip()
        if not selector:
            result["error"] = "选择器为空。"
            return result
        if not (url or "").strip():
            result["error"] = "网址为空。"
            return result
        if _sync_playwright is None:
            result["error"] = "未安装 playwright。"
            return result

        playwright = browser = None
        try:
            playwright = _sync_playwright().start()
            browser, _ = self._launch(playwright)
            context = self._new_context(browser)
            page = context.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            self._goto(page, url.strip())
            self._wait_loaded(page)
            locator = page.locator(_normalize_selector(selector))
            try:
                count = locator.count()
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"选择器语法错误：{_redact(exc, 250)}"
                return result
            result["count"] = int(count)
            before_url = page.url
            for index in range(min(count, 5)):
                item = locator.nth(index)
                try:
                    text = (item.inner_text(timeout=2000) or "").strip().replace("\n", " ")
                    if text:
                        result["samples"].append(_clip(text, 120))
                    if item.is_visible():
                        result["visible"] += 1
                except Exception:  # noqa: BLE001
                    continue
            if click and count > 0:
                try:
                    locator.first.click(timeout=min(self.page_timeout_ms, 15000))
                    result["clicked"] = True
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:  # noqa: BLE001
                        time.sleep(1.0)
                    result["new_url"] = page.url
                    result["changed"] = page.url != before_url
                except Exception as exc:  # noqa: BLE001
                    result["error"] = f"点击失败：{_redact(exc, 200)}"
            result["ok"] = True
            if count == 0:
                result["error"] = self._selector_error or "页面已打开，但没有匹配到元素。"
        except Exception as exc:  # noqa: BLE001
            result["error"] = _redact(exc, 300)
            self.logger.warning("元素测试失败：%s", _redact(exc, 200))
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        return result

    def wizard_step(self, step: int, **kwargs: Any) -> Dict[str, Any]:
        """测试选择器向导的单个步骤（任务书 4.5）。

        step 1 任务列表元素 / 2 下一页按钮 / 3 详情页附件 / 4 试运行一个任务。
        每一步都给出可读的成功/失败原因与修正建议。
        """
        cfg = self.cfg
        url = str(getattr(cfg, "task_list_url", "") or "").strip()
        out: Dict[str, Any] = {"step": int(step), "ok": False, "title": "", "detail": "",
                               "samples": [], "suggestions": [], "data": {}}

        if int(step) == 1:
            selector = str(getattr(cfg, "task_list_selector", "") or "").strip()
            row = str(getattr(cfg, "task_row_selector", "") or "").strip()
            out["title"] = "第一步：测试任务列表元素位置"
            if not url:
                out["detail"] = "还没有填写「任务列表网址」。"
                out["suggestions"] = ["填上列出全部任务的页面地址，从浏览器地址栏复制即可。"]
                return out
            if not selector:
                out["detail"] = "还没有填写「任务列表元素位置」。"
                out["suggestions"] = ["点右边的「🎯 可视化选取」，然后在页面上点一下任务名称。"]
                return out
            res = self.test_selector(url, selector)
            out["data"] = res
            out["samples"] = res.get("samples") or []
            if res.get("ok") and res.get("count"):
                out["ok"] = True
                out["detail"] = f"匹配到 {res['count']} 个任务链接，例如：{'、'.join(out['samples'][:3]) or '（无文本）'}"
                if row:
                    row_res = self.test_element(url, row)
                    out["data"]["row"] = row_res
                    if row_res.get("count"):
                        out["detail"] += f"\n任务行容器匹配到 {row_res['count']} 行。"
                    else:
                        out["detail"] += "\n⚠️ 任务行容器没有匹配到元素；不确定就把它留空。"
                        out["suggestions"].append("任务行容器是可选项，不确定请留空。")
            elif res.get("ok"):
                out["detail"] = "页面已打开，但这个选择器没有匹配到任何元素。"
                out["suggestions"] = [
                    "确认这一页确实显示了任务列表（有些系统要先登录）。",
                    "用「🎯 可视化选取」重新点一次任务名称。",
                    "若列表在分页/懒加载里，先滚动到任务出现再选取。",
                ]
            else:
                out["detail"] = f"测试失败：{res.get('error') or '未知原因'}"
                out["suggestions"] = ["检查网址能否在浏览器里正常打开。", "检查选择器是否被误改。"]
            return out

        if int(step) == 2:
            selector = str(getattr(cfg, "next_page_selector", "") or "").strip()
            out["title"] = "第二步：测试下一页按钮"
            if not selector:
                out["ok"] = True
                out["detail"] = "没有配置「下一页按钮」——如果任务列表只有一页，这一步可以直接跳过。"
                return out
            if not url:
                out["detail"] = "还没有填写「任务列表网址」。"
                return out
            res = self.test_element(url, selector, click=True)
            out["data"] = res
            if res.get("count") and res.get("clicked"):
                out["ok"] = True
                out["detail"] = (f"找到并点击了「下一页」按钮，页面{'已跳转' if res.get('changed') else '未跳转'}。"
                                 f"\n点击后地址：{res.get('new_url') or '（未变化）'}")
                if not res.get("changed"):
                    out["suggestions"] = [
                        "如果这一页确实是最后一页，未跳转是正常的。",
                        "如果不是最后一页，可能是按钮由 JavaScript 处理：试试换成更精确的选择器。",
                    ]
            elif res.get("count"):
                out["detail"] = f"匹配到 {res['count']} 个元素，但点击没有成功：{res.get('error') or '未知原因'}"
            else:
                out["detail"] = "没有匹配到「下一页」按钮。"
                out["suggestions"] = ["用「🎯 可视化选取」在列表页底部点一下「下一页」。",
                                      "若按钮文字是「>」或「下一页 ›」，注意选的是可点击的那个元素。"]
            return out

        if int(step) == 3:
            selector = str(getattr(cfg, "task_detail_file_selector", "") or "").strip()
            out["title"] = "第三步：测试任务详情页附件位置"
            if not selector:
                out["detail"] = "还没有填写「任务详情页文件元素位置」。"
                out["suggestions"] = ["点「🎯 可视化选取」并选择「详情页附件」，程序会自动进入第一个任务再让你点。"]
                return out
            list_selector = str(getattr(cfg, "task_list_selector", "") or "").strip()
            if not url or not list_selector:
                out["detail"] = "需要先配置好「任务列表网址」与「任务列表元素位置」，否则进不去任务详情页。"
                return out
            playwright = browser = None
            try:
                playwright = _sync_playwright().start()
                browser, _ = self._launch(playwright)
                context = self._new_context(browser)
                page = context.new_page()
                page.set_default_timeout(self.page_timeout_ms)
                self._goto(page, url)
                self._wait_loaded(page)
                entries = self._collect_links(
                    page, list_selector, str(getattr(cfg, "task_row_selector", "") or ""))
                if not entries or not entries[0].href:
                    out["detail"] = "任务列表选择器没有匹配到可用任务，无法进入详情页。"
                    out["suggestions"] = ["先让第一步测试通过。"]
                    return out
                self._goto(page, entries[0].href)
                self._wait_loaded(page)
                locator = page.locator(_normalize_selector(selector))
                count = locator.count()
                samples: List[str] = []
                exts = set(self.file_extensions)
                matched_ext = 0
                for index in range(min(count, 8)):
                    try:
                        text = (locator.nth(index).inner_text(timeout=2000) or "").strip().replace("\n", " ")
                    except Exception:  # noqa: BLE001
                        continue
                    if text:
                        samples.append(_clip(text, 120))
                    ext = _extension_of(locator.nth(index).get_attribute("href") or "")
                    if not ext:
                        ext = _extension_of(text)
                    if ext and ext.lstrip(".").lower() in exts:
                        matched_ext += 1
                out["samples"] = samples
                out["data"] = {"count": int(count), "matched_extensions": matched_ext,
                               "task": _clip(entries[0].text, 60), "task_url": entries[0].href}
                if count:
                    out["ok"] = True
                    out["detail"] = (f"在任务「{_clip(entries[0].text, 40)}」的详情页里匹配到 {count} 个附件元素，"
                                     f"其中 {matched_ext} 个的后缀在「需要预览的文件后缀」内。")
                    if matched_ext == 0:
                        out["suggestions"] = [
                            f"当前允许的后缀：{'、'.join(sorted(exts)) or '（空）'}；"
                            "如果附件是其它格式，请在配置里补上。",
                        ]
                else:
                    out["detail"] = "在任务详情页里没有匹配到附件元素。"
                    out["suggestions"] = ["用「🎯 可视化选取」选择「详情页附件」重新点一次。",
                                          "确认第一个任务确实带附件。"]
            except Exception as exc:  # noqa: BLE001
                out["detail"] = f"测试失败：{_redact(exc, 300)}"
            finally:
                for closer in (lambda: browser.close() if browser else None,
                               lambda: playwright.stop() if playwright else None):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass
            return out

        if int(step) == 4:
            out["title"] = "第四步：试运行一个任务（不写结果文件）"
            errors = self.validate()
            if errors:
                out["detail"] = "配置还没通过校验：\n- " + "\n- ".join(errors)
                out["suggestions"] = ["按上面的提示补齐配置后再试运行。"]
                return out
            collected: List[str] = []
            result_status = "unknown"
            for event in self.run(limit=1, dry_run=True, **kwargs):
                kind = event.get("type")
                if kind == "delta":
                    collected.append(str(event.get("content") or ""))
                elif kind == "done":
                    result_status = "ok"
                    collected.append(str(event.get("content") or ""))
                elif kind == "error":
                    result_status = "error"
                    collected.append(str(event.get("content") or ""))
            out["ok"] = result_status == "ok"
            out["detail"] = _clip("\n".join(line for line in collected if line), 4000)
            out["data"] = {"log": collected, "status": result_status}
            if not out["ok"]:
                out["suggestions"] = ["根据上面的报错检查选择器与登录状态。",
                                      "确认这台电脑上能用手动方式完成同样的下载。"]
            else:
                out["suggestions"] = ["确认无误后，勾选安全协议并打开自动化总开关即可开始批量处理。"]
            return out

        out["detail"] = f"未知的向导步骤：{step}"
        return out

    def _target_label(target: str) -> str:
        """可视化选取时给用户看的目标名称（任务书 4.2 的四种选取目标）。"""
        return {
            "task_list": "任务列表里的任务链接",
            "task_row": "任务所在的那一整行",
            "next_page": "「下一页」按钮",
            "task_detail_file": "任务详情页里的附件名称",
        }.get(target, "目标元素")

    def stop(self) -> None:
        """请求停止（线程安全）：置位内部事件，并同步外部传入的 stop_event。

        内部标志会在下一次 ``run()`` 开始时重置，因此同一个引擎可以反复运行。
        """
        self._stop.set()
        external = self._active_stop
        if external is not None:
            try:
                external.set()
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict:
        """返回当前运行状态快照。"""
        with self._lock:
            return {
                "running": bool(self._running),
                "last_run": self._last_run,
                "processed_count": int(self._processed_count),
                "results": int(self._result_count),
                "last_error": self._last_error,
                # 新增三：结果目录信息，供设置窗口与状态栏展示
                "results_root": self.results_root,
                "results_dir": self.results_dir,
                "report_path": self.report_path if self._run_task_dir else "",
            }

    def cleanup_temp(self) -> int:
        """清理下载临时目录中的残留文件，返回删除的文件数。

        注意：运行期间调用会删掉正在使用的临时文件，因此这里在运行中直接跳过并返回 0，
        仅在空闲时清理（前端应在自动化未运行时调用）。
        """
        root = self.download_dir
        if self._running:
            self.logger.warning("自动化运行中，已跳过临时目录清理。")
            return 0
        if not os.path.isdir(root):
            return 0
        removed = 0
        for base, dirs, files in os.walk(root, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(base, name))
                    removed += 1
                except OSError:
                    continue
            for name in dirs:
                try:
                    os.rmdir(os.path.join(base, name))
                except OSError:
                    continue
        return removed

    # ---------- 主流程 ----------

    def run(self, stop_event: Any = None, on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
            limit: int = 0, dry_run: bool = False) -> Iterator[dict]:
        """执行自动化主流程（生成器），逐个产出进度事件。

        ``limit`` 覆盖配置里的 ``max_tasks``（0 = 用配置值）；
        ``dry_run`` 为真时只试运行（不写结果文件、不写处理记录），供测试向导第四步使用。
        """
        yield from self._run_impl(stop_event, on_progress, limit=limit, dry_run=dry_run)

    def _run_impl(self, stop_event: Any, on_progress: Optional[Callable[[Dict[str, Any]], None]],
                  limit: int = 0, dry_run: bool = False) -> Iterator[Dict[str, Any]]:
        """``run()`` 的实际实现：所有 yield 都在这里，便于 ``finally`` 统一清理。"""
        self._progress_cb = on_progress
        self._active_stop = stop_event
        # 每次运行重置内部停止标志（外部 stop_event 保持调用方给定的状态）
        self._stop.clear()

        def stopped() -> bool:
            if self._stop.is_set():
                return True
            try:
                return bool(stop_event is not None and stop_event.is_set())
            except Exception:  # noqa: BLE001
                return False

        # ---- 安全协议前置检查（必须在启动浏览器之前）----
        if not self.enabled:
            message = "自动化总开关未开启（automation.enabled=false），未启动浏览器。"
            if not self.safety_accepted:
                message = "尚未勾选安全协议，自动化已拒绝执行，未启动浏览器。"
            self._last_error = message
            yield self._event("error", message)
            return
        if not self.safety_accepted:
            message = "安全协议未勾选（automation.safety_accepted=false），自动化已拒绝执行，未启动浏览器。"
            self._last_error = message
            yield self._event("error", message)
            return
        if self._running:
            message = "已有自动化任务在运行中，请等待其结束或先点击停止。"
            self._last_error = message
            yield self._event("error", message)
            return

        errors = self.validate()
        if errors:
            message = "配置校验未通过：\n- " + "\n- ".join(errors)
            self._last_error = message
            yield self._event("error", message)
            return

        # ---- 运行状态与计数器 ----
        with self._lock:
            self._running = True
            self._last_run = _now_iso()
            self._last_error = ""
            self._result_count = 0
        counters = {"ok": 0, "skipped": 0, "failed": 0}
        results: List[str] = []
        processed_urls = self._load_processed() if bool(getattr(self.cfg, "dedupe", True)) else set()
        run_tmp = ""
        browser = context = page = playwright = None
        total_tasks = 0
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            run_tmp = tempfile.mkdtemp(prefix="run_", dir=self.download_dir)
            # ---- 新增三：本轮结果目录 = 结果根目录/日期/任务名（同名自动加序号）----
            # 试运行（dry_run）只走流程，不建目录、不写结果文件、不记处理记录。
            self._report_blocks = []
            self._dry_run = bool(dry_run)
            if bool(getattr(self.cfg, "save_results", True)) and not dry_run:
                self._run_task_dir = self._prepare_task_dir()
                self._report_header = self._build_report_header()
                self._write_report(self._report_blocks, self._report_header)
                yield self._event("delta", f"结果将保存到：{self.results_dir}")
            else:
                self._run_task_dir = ""
                self._report_header = ""
                if dry_run:
                    yield self._event("delta", "试运行模式：只处理一个任务，不写结果文件、不记入处理记录。")
            self._audit("run_start", url=self.task_list_url, result="start",
                        max_tasks=limit or getattr(self.cfg, "max_tasks", 20))
            yield self._event("delta", f"开始自动化：准备打开任务列表 {self.task_list_url}")

            # ---- 启动浏览器 ----
            yield self._event("delta", "正在启动浏览器（有头模式，请勿手动关闭窗口）…")
            playwright = _sync_playwright().start()
            browser, note = self._launch(playwright)
            if note:
                yield self._event("delta", note)
            context = self._new_context(browser)
            page = context.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            try:
                page.set_default_navigation_timeout(self.page_timeout_ms)
            except Exception:  # noqa: BLE001
                pass

            # ---- 打开任务列表 ----
            yield self._event("delta", "正在打开任务列表页…")
            self._goto(page, self.task_list_url)
            manual_ok = yield from self._wait_manual(page, stopped)
            if not manual_ok:
                yield self._event("delta", "任务列表页需要人工验证但未完成，本次运行结束。")
            self._wait_loaded(page)

            # ---- 收集任务链接（含分页）----
            task_selector = str(getattr(self.cfg, "task_list_selector", "") or "")
            row_selector = str(getattr(self.cfg, "task_row_selector", "") or "")
            next_selector = str(getattr(self.cfg, "next_page_selector", "") or "")
            max_tasks = max(1, int(limit or getattr(self.cfg, "max_tasks", 20) or 20))
            pending: List[_LinkEntry] = []
            seen_keys: Set[str] = set()

            for page_index in range(1, _MAX_PAGES + 1):
                if stopped():
                    break
                entries = [e for e in self._collect_links(page, task_selector, row_selector) if e.href]
                new_count = 0
                for entry in entries:
                    if entry.href in seen_keys:
                        continue
                    seen_keys.add(entry.href)
                    pending.append(entry)
                    new_count += 1
                yield self._event(
                    "delta",
                    f"第 {page_index} 页找到 {len(entries)} 个任务链接（新增 {new_count} 个，累计 {len(pending)} 个）。",
                )
                if len(pending) >= max_tasks:
                    break
                if not next_selector.strip():
                    break
                if not self._click_next_page(page, next_selector):
                    yield self._event("delta", "没有可用的下一页，任务收集结束。")
                    break
                self._wait_loaded(page)
                manual_ok = yield from self._wait_manual(page, stopped)
                if not manual_ok:
                    break

            total_tasks = min(len(pending), max_tasks)
            if total_tasks == 0:
                if stopped():
                    # 运行刚开始就被要求停止：正常收尾，不算错误
                    self._save_summary_block("已按请求停止，本轮没有处理任何任务。")
                    yield self._event(
                        "done",
                        "已按请求停止，本轮没有处理任何任务。",
                        processed=0,
                        skipped=0,
                        failed=0,
                        results=[],
                    )
                    return
                message = "未在任务列表页找到任何任务链接（请检查任务列表元素位置）。"
                if self._selector_error:
                    message = f"任务列表元素位置无法使用：{self._selector_error}"
                self._last_error = message
                # 失败信息也写进 结果.txt，避免"结果目录是空的、不知道发生了什么"
                self._save_summary_block(f"本轮未处理任何任务。\n原因：{message}")
                self._audit("run_end", url=self.task_list_url, result="failed", error=message, **counters)
                yield self._event("error", message)
                return
            yield self._event("delta", f"共找到 {len(pending)} 个任务，本轮最多处理 {total_tasks} 个。")

            # ---- 逐个处理任务 ----
            for index, entry in enumerate(pending[:total_tasks], 1):
                if stopped():
                    yield self._event("delta", "已收到停止请求，剩余任务不再处理。")
                    break
                title = entry.text or f"任务{index}"
                url_key = self._url_key(entry.href)
                if bool(getattr(self.cfg, "dedupe", True)) and url_key in processed_urls:
                    counters["skipped"] += 1
                    yield self._event("delta", f"第 {index}/{total_tasks} 个任务：{_clip(title, 40)}——已处理过，跳过。")
                    self._audit("task", url=entry.href, result="skipped", error="已在处理记录中")
                    continue

                task_tmp = ""
                outcome = _TaskOutcome(status="failed", error="未执行")
                # 技能钩子：任务开始前（v0.2.1 任务二 2.3）
                self._fire("before_task", task=title, url=entry.href, index=index,
                           total=total_tasks, run_dir=self._run_task_dir)
                try:
                    task_tmp = tempfile.mkdtemp(prefix=f"t{index}_", dir=run_tmp)
                    attempts = max(0, int(getattr(self.cfg, "retries", 1) or 0)) + 1
                    for attempt in range(1, attempts + 1):
                        if stopped():
                            break
                        if attempt > 1:
                            yield self._event(
                                "delta",
                                f"第 {index}/{total_tasks} 个任务：第 {attempt}/{attempts} 次尝试…",
                            )
                        outcome = yield from self._process_one(
                            context, page, entry, index, total_tasks, task_tmp, stopped
                        )
                        if outcome.status == "ok":
                            break
                except Exception as exc:  # noqa: BLE001 - 单个任务异常不得中断整轮
                    outcome = _TaskOutcome(status="failed", error=f"任务处理异常：{_redact(exc, 200)}")
                finally:
                    if task_tmp:
                        shutil.rmtree(task_tmp, ignore_errors=True)

                if outcome.status == "ok":
                    counters["ok"] += 1
                    processed_urls.add(url_key)
                    with self._lock:
                        self._processed_count += 1
                        self._result_count += 1
                    block = [
                        f"[{index}/{total_tasks}] {title}",
                        f"链接：{entry.href}",
                        f"文件：{'、'.join(outcome.files) if outcome.files else '无'}",
                        "审核意见：",
                        outcome.summary,
                    ]
                    if outcome.result_path:
                        block.append(f"（已写入结果文件：{os.path.basename(outcome.result_path)}）")
                    text = "\n".join(block)
                    results.append(text)
                    yield self._event("delta", f"第 {index}/{total_tasks} 个任务完成。\n" + _clip(text, 2000))
                    self._audit("task", url=entry.href, files=outcome.files, result="ok")
                else:
                    counters["failed"] += 1
                    reason = outcome.error or "未知错误"
                    # 失败信息同样写入 结果.txt，并保留失败截图（任务书 新增三）
                    self._save_failure(
                        title, entry.href, outcome.files, reason,
                        index=index, total=total_tasks, screenshot=outcome.screenshot,
                    )
                    yield self._event(
                        "delta",
                        f"第 {index}/{total_tasks} 个任务失败（已跳过，继续下一个）：{_clip(reason, 300)}",
                    )
                    self._audit("task", url=entry.href, files=outcome.files, result="failed", error=reason)

                # ---- 回到任务列表，准备下一个任务（链接已在收集阶段缓存，无需重新分页定位）----
                # 技能钩子：单个任务处理结束后
                self._fire("after_task", task=title, url=entry.href, index=index,
                           total=total_tasks, status=outcome.status, files=list(outcome.files),
                           summary=outcome.summary, error=outcome.error,
                           result_path=outcome.result_path, run_dir=self._run_task_dir)
                if index < total_tasks and not stopped():
                    try:
                        self._goto(page, self.task_list_url)
                        self._wait_loaded(page)
                    except Exception as exc:  # noqa: BLE001
                        self.logger.debug("返回任务列表失败：%s", _redact(exc, 150))

            if bool(getattr(self.cfg, "dedupe", True)) and not dry_run:
                self._save_processed(processed_urls)

            # ---- 汇总 ----
            header = (
                f"自动化完成：成功 {counters['ok']} 个，跳过 {counters['skipped']} 个，"
                f"失败 {counters['failed']} 个（共发现 {len(pending)} 个任务，处理上限 {total_tasks} 个）。"
            )
            if stopped():
                header = "已按请求停止。" + header
            if bool(getattr(self.cfg, "save_results", True)) and not dry_run:
                header += f"\n结果目录：{self.results_dir}"
                self._save_summary_block(header)
            body = "\n\n".join(results) if results else "（本轮没有生成结果）"
            content = header + "\n\n" + _clip(body, 6000)
            self._audit(
                "run_end", url=self.task_list_url, result="ok" if counters["failed"] == 0 else "failed",
                error="" if counters["failed"] == 0 else f"{counters['failed']} 个任务失败",
                processed=counters["ok"], skipped=counters["skipped"], failed=counters["failed"],
            )
            yield self._event(
                "done",
                content,
                processed=counters["ok"],
                skipped=counters["skipped"],
                failed=counters["failed"],
                results=results,
            )
        except AutomationError as exc:
            message = str(exc)
            self._last_error = message
            self._audit("run_end", url=self.task_list_url, result="failed", error=message, **counters)
            yield self._event("error", message)
        except Exception as exc:  # noqa: BLE001
            message = f"自动化运行异常：{_redact(exc, 300)}"
            self._last_error = message
            self.logger.exception("自动化运行异常")
            self._audit("run_end", url=self.task_list_url, result="failed", error=message, **counters)
            yield self._event("error", message)
        finally:
            # 统一清理：登录态落盘 → 关闭上下文/浏览器 → 停 Playwright → 删本轮临时目录
            try:
                if context is not None:
                    self._save_storage_state(context)
            except Exception:  # noqa: BLE001
                pass
            try:
                if context is not None:
                    context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            if run_tmp:
                shutil.rmtree(run_tmp, ignore_errors=True)
            with self._lock:
                self._running = False
            self._active_stop = None
            self._progress_cb = None

    def _click_next_page(self, page: Any, selector: str) -> bool:
        """点击"下一页"；找不到或不可用返回 False。"""
        try:
            entries = self._collect_links(page, selector)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("定位下一页失败：%s", _redact(exc, 150))
            return False
        for entry in entries:
            if entry.handle is None:
                continue
            try:
                if not entry.handle.is_enabled():
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                self._click(entry.handle)
                return True
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("点击下一页失败：%s", _redact(exc, 150))
                continue
        return False

    def _process_one(
        self,
        context: Any,
        page: Any,
        entry: _LinkEntry,
        index: int,
        total: int,
        task_tmp: str,
        stop_fn: Callable[[], bool],
    ) -> Iterator[Dict[str, Any]]:
        """处理单个任务（生成器，返回值通过 ``yield from`` 拿到）。

        返回值：``_TaskOutcome``。下载的原始文件在成功/失败时都会被移入本轮任务目录
        （新增三：结果目录内保存原始文件与 结果.txt），临时文件目录只用于中转。
        """
        title = entry.text or f"任务{index}"
        files_saved: List[str] = []
        moved_paths: Set[str] = set()   # 已移入结果目录的文件（finally 不再删除）
        names: List[str] = []
        errors: List[str] = []
        pages_before = list(getattr(context, "pages", []) or [])
        try:
            if not entry.href:
                return _TaskOutcome("failed", "", [], "任务链接为空，无法打开详情页。")
            yield self._event("delta", f"第 {index}/{total} 个任务：{_clip(title, 40)}——正在打开详情页…")
            self._goto(page, entry.href)
            if not (yield from self._wait_manual(page, stop_fn)):
                return _TaskOutcome("failed", "", [], f"等待人工验证超时（{getattr(self.cfg, 'manual_login_wait_seconds', 180)} 秒）")
            self._wait_loaded(page)
            if stop_fn():
                return _TaskOutcome("failed", "", [], "已停止")

            # ---- 定位附件链接并按后缀过滤 ----
            file_selector = str(getattr(self.cfg, "task_detail_file_selector", "") or "")
            entries = [e for e in self._collect_links(page, file_selector) if e.href or e.handle is not None]
            allowed = self.file_extensions
            matched: List[_LinkEntry] = []
            skipped_ext = 0
            for candidate in entries:
                ext = _extension_of(candidate.href) or _extension_of(candidate.text)
                if allowed and ext not in allowed:
                    skipped_ext += 1
                    continue
                matched.append(candidate)
                if len(matched) >= _MAX_FILES_PER_TASK:
                    break
            if skipped_ext:
                yield self._event("delta", f"第 {index}/{total} 个任务：已忽略 {skipped_ext} 个后缀不符合要求的文件。")
            if not matched:
                reason = "详情页没有符合后缀要求的文件链接"
                if self._selector_error:
                    reason = f"任务详情页文件元素位置无法使用：{self._selector_error}"
                return _TaskOutcome("failed", "", [], reason)

            # ---- 逐个下载并解析 ----
            documents: List[Tuple[str, str]] = []
            for order, candidate in enumerate(matched, 1):
                if stop_fn():
                    return _TaskOutcome("failed", "", names, "已停止")
                label = _clip(candidate.text or candidate.href, 60) or f"文件{order}"
                yield self._event(
                    "delta",
                    f"第 {index}/{total} 个任务：正在下载 {label}（{order}/{len(matched)}）…",
                )
                path, err = self._download_entry(context, page, candidate, task_tmp)
                if err or not path:
                    errors.append(f"{label}：{err or '下载失败'}")
                    yield self._event("delta", f"第 {index}/{total} 个任务：{label} 跳过（{_clip(err, 120)}）")
                    continue
                files_saved.append(path)
                name = os.path.basename(path)
                names.append(name)
                text, parse_err = self._parse_downloaded(path)
                if parse_err:
                    # 解析错误串绝不交给 LLM；该文件记为失败，其余文件继续
                    errors.append(f"{name}：{parse_err}")
                    yield self._event("delta", f"第 {index}/{total} 个任务：{name} {_clip(parse_err, 120)}")
                    continue
                documents.append((name, text))

            if not documents:
                # 技能钩子：文件下载完成（即使全部失败也通知，便于技能统计）
                self._fire("after_download", task=title, index=index, total=total,
                           files=list(names), errors=list(errors), run_dir=self._run_task_dir)
                return _TaskOutcome("failed", "", names, "；".join(errors) or "没有可用的文件内容")

            # 技能钩子：文件下载后（任务书 2.3 的 after_download 阶段）
            self._fire("after_download", task=title, index=index, total=total,
                       files=list(names), errors=list(errors), run_dir=self._run_task_dir)

            # ---- 生成审核意见 ----
            yield self._event("delta", f"第 {index}/{total} 个任务：正在生成审核意见…")
            summary = self._generate(documents)
            if errors:
                summary += "\n\n【提示】以下文件未能读取，未纳入审核：" + "；".join(errors)
            # 技能钩子：摘要生成后（任务书 2.3 的 after_summary 阶段）
            self._fire("after_summary", task=title, index=index, total=total,
                       files=list(names), summary=summary, run_dir=self._run_task_dir)
            # 新增三：把本轮下载的原始文件移入任务目录（不再随临时目录一起删除）
            kept = self._move_into_task_dir(files_saved)
            moved_paths.update(files_saved)
            for name in kept:
                if name not in names:
                    names.append(name)
            if kept:
                yield self._event("delta", f"第 {index}/{total} 个任务：原始文件已保存到结果目录（{len(kept)} 个）。")
            result_path = self._save_result(title, entry.href, names, summary, index=index, total=total)
            return _TaskOutcome("ok", summary, names, "", result_path)
        except Exception as exc:  # noqa: BLE001
            shot = self._screenshot(page, index)
            # 新增三：失败时也保留下载到的原始文件与失败截图
            kept = self._move_into_task_dir(files_saved)
            moved_paths.update(files_saved)
            for name in kept:
                if name not in names:
                    names.append(name)
            return _TaskOutcome("failed", "", names, _redact(exc, 250), "", shot)
        finally:
            # 只清理"没能移入结果目录"的临时文件（已保留的文件不能删）
            for path in files_saved:
                if path in moved_paths:
                    continue
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    continue
            # 关闭本任务过程中新开出的标签页（避免标签页泄漏）；浏览器内部页面不碰
            for candidate in self._new_pages(context, pages_before):
                self._safe_close(candidate)
