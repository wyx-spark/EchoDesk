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

# 多语言（任务三）：必须在 config 之后导入，避免 i18n 反向依赖 config 时成环
from i18n import t as _t

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

# 自动化审核意见的系统提示（v0.2.1 提示词优化：补齐「输出格式 / 思维」，
# 与 conversation.MODES 用同一套六要素写法）。
#
# 【语言策略是有意的，不是与对话层矛盾】这里写死"使用简体中文"，而交互对话的
# 回复语言**跟随界面语言**（见 conversation._REPLY_LANGUAGE）。两者场景不同：
# 审核意见会写入 results\ 下的 result.txt 作为留痕材料，属于业务产出而不是界面
# 文案，语言固定为中文是产品决策——该结论已由 records/v0.2.1-修复记录.md 的
# 「产品层面的观察」一节记录在案，这里把它写明，避免下次被当成不一致改掉。
_SYSTEM_PROMPT = (
    "【角色】你是文档审核助手，依据用户给出的任务指令与附件正文给出审核意见。\n"
    "【输出格式】先提炼关键信息（数字、日期、金额、当事方、义务、结论），"
    "再用「1. 2. 3.」列出审核意见，每条一句话。\n"
    "【思维】先逐份通读附件并记下关键要素，全部读完后再统一给意见，不要边读边下结论。\n"
    "【边界】只依据附件正文作答，不要编造；材料不足或不确定的地方必须明确指出；"
    "没有可用的附件内容时，直接说明无法审核，不要凭任务指令推测。\n"
    "【语气】书面、中立，使用简体中文（审核意见会写入结果文件长期留痕）。"
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


def _drive(gen: Iterator[Any]) -> Any:
    """把一个"靠 ``return`` 传值、靠 ``yield`` 报进度"的生成器跑完，取出返回值。

    有些跳转逻辑写成生成器是为了上报进度事件（``_click_into``），但它的调用方
    ``wizard_step`` 是**同步**方法、没有进度通道。与其在同步方法里写
    ``while True: next(...)`` + ``StopIteration`` 样板，不如收成这个小工具。
    """
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _target_label(target: str) -> str:
    """可视化选取时给用户看的目标名称（任务书 4.2 的四种选取目标）。

    **必须是模块级函数**（2026-09 修复）：它原先定义在 ``AutomationEngine`` 的类体里，
    而调用点（``_stream_pick``）写的是裸名字 ``_target_label(target)`` ——
    Python 的方法体不会把类作用域纳入 LEGB 查找，裸名字只会找到模块全局，
    于是每次可视化选取都会 ``NameError``，被外层 ``except`` 兜住后用户看到的
    是"可视化选取失败：name '_target_label' is not defined"，
    本该显示的"请在弹出的 Edge 窗口里点击目标元素"永远出不来。
    它是纯函数、不依赖 ``self``，放在模块级最自然，也顺便杜绝此类作用域问题。
    """
    return {
        "task_list": _t("automation.target.task_list"),
        "task_row": _t("automation.target.task_row"),
        "next_page": _t("automation.target.next_page"),
        "task_detail_file": _t("automation.target.task_detail_file"),
        "login_check": _t("automation.target.login_check"),
    }.get(target, _t("automation.target.default"))


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
class _HrefInfo:
    """一次 href 提取的完整结果（v0.2.1 自动化优化 任务二）。

    ``kind`` 取值（判定顺序即优先级）：

    * ``absolute``   —— 本身就是绝对地址，直接可用；
    * ``relative``   —— 相对路径，已用当前页面地址补全为绝对 URL；
    * ``javascript`` —— ``javascript:`` 之类的伪链接，**不能**用来导航；
    * ``fragment``   —— 空片段（``#``）或页内锚点，不是任务/附件地址；
    * ``empty``      —— href 属性为空（含"元素根本不是链接"的情况）；
    * ``other``      —— 有值但协议不可直接导航（如 ``mailto:``、``tel:``）。

    ``click_only`` 为真表示：**不要用 url 导航，改为点击元素触发跳转**。
    任务书「任务二 特殊情况回退」要求程序自动完成这个回退，而不是直接失败。
    """

    raw: str = ""
    url: str = ""
    kind: str = "empty"

    @property
    def click_only(self) -> bool:
        return self.kind in ("javascript", "fragment", "empty", "other")


#: 伪链接前缀：这些值在 ``<a href>`` 里表示"由 JavaScript 处理点击"
_PSEUDO_HREF_PREFIXES = ("javascript:", "vbscript:", "about:blank")

#: 能直接交给浏览器导航的协议白名单；不在名单里的一律走点击回退
_NAVIGABLE_SCHEMES = ("http", "https", "file", "ftp")


def _classify_href(raw: Any, base_url: str) -> _HrefInfo:
    """规范化 href 属性值：绝对 URL 直接用，相对路径拼 origin，伪链接标记为只能点击。

    背景（任务书「任务二」）：XPath / CSS 选择器只负责**定位元素**，不负责读取属性。
    正确的流程是「定位 → 读取属性 → 规范化 → 特殊情况回退」，本函数负责后两步。
    """
    text = str(raw or "").strip()
    if not text:
        return _HrefInfo("", "", "empty")
    low = text.lower()
    if low.startswith(_PSEUDO_HREF_PREFIXES):
        return _HrefInfo(text, "", "javascript")
    if text.startswith("#"):
        # 任务书只列了裸 ``#``；这里把 ``#xxx`` 一并归入片段链接：页内锚点同样不是
        # "任务详情页 / 附件地址"，导航过去只会停在列表页自身（这属于对任务书的扩展，
        # 已记入 records/v0.2.1-自动化优化记录.md）。
        return _HrefInfo(text, "", "fragment")
    scheme = ""
    try:
        scheme = (urlparse(text).scheme or "").lower()
    except Exception:  # noqa: BLE001
        scheme = ""
    if scheme and scheme not in _NAVIGABLE_SCHEMES:
        return _HrefInfo(text, "", "other")
    try:
        absolute = urljoin(base_url or "", text)
    except Exception:  # noqa: BLE001
        absolute = text
    if not absolute:
        return _HrefInfo(text, "", "empty")
    return _HrefInfo(text, absolute, "absolute" if scheme else "relative")


@dataclass
class _LinkEntry:
    """页面上的一个链接（任务链接或附件链接）。"""

    text: str = ""
    href: str = ""
    handle: Any = None      # ElementHandle；None 表示来自 Shadow DOM 穿透，不可点击
    frame: Any = None
    href_raw: str = ""            # href 属性的原始值（未补全），用于日志与用户提示
    href_kind: str = "absolute"   # 见 _HrefInfo.kind
    click_only: bool = False      # True = 不能用 href 导航，必须点击元素

    @property
    def key(self) -> str:
        """去重键：有 URL 用 URL；没有 URL（伪链接）时只有文本可用。

        注意：**伪链接不去重**（见 ``_collect_links``）——一页里 10 个任务的
        ``href="javascript:void(0)"`` 长得完全一样，按 href 去重会只剩 1 个任务。
        """
        return self.href or self.text

    @property
    def address(self) -> str:
        """给用户看的地址：伪链接显示原始值，便于解释"为什么改成点击"。"""
        return self.href or self.href_raw


@dataclass
class _TaskOutcome:
    """单个任务的处理结果。"""

    status: str = "failed"          # ok | failed
    summary: str = ""               # 模型输出正文（仅用于回传前端/写结果文件，不入日志）
    files: List[str] = field(default_factory=list)   # 落地到任务目录后的文件名
    error: str = ""
    result_path: str = ""           # 结果文件落盘路径（result.txt）
    screenshot: str = ""            # 失败截图路径（任务书 新增三：截图留在任务目录内）


# ---------- 主引擎 ----------


class _BrowserSession:
    """一次浏览器会话：持久化上下文 + （可能为 None 的）浏览器实例 + Playwright 句柄。

    v0.2.1 自动化优化 任务一把登录态机制换成了 ``launch_persistent_context``，
    它**返回的是上下文而不是浏览器**（且 Python 侧 ``context.browser`` 恒为 None），
    于是"启动 → 新建上下文 → 关闭"这三个动作必须一起管。以前每个调用点都自己写
    ``browser.close()``，换机制时要改 6 处，容易漏；收成一个对象后只改这里。
    """

    def __init__(self, playwright: Any, context: Any, browser: Any = None, note: str = "",
                 profile_dir: str = "", first_run: bool = False) -> None:
        self.playwright = playwright
        self.context = context
        self.browser = browser
        self.note = note
        self.profile_dir = profile_dir
        self.first_run = first_run    # 本次是否是"首次使用这个数据目录"（需要引导登录）

    @property
    def pages(self) -> List[Any]:
        return list(getattr(self.context, "pages", []) or [])

    def new_page(self) -> Any:
        return self.context.new_page()

    def close(self) -> None:
        """关闭上下文（持久化上下文会连带关掉浏览器），再停 Playwright。

        顺序不能反：先 ``context.close()`` 才能保证 Cookie / localStorage 落盘，
        否则下次启动拿不到这次刚登录的会话。
        """
        for closer in (
            lambda: self.context.close() if self.context is not None else None,
            lambda: self.browser.close() if self.browser is not None else None,
            lambda: self.playwright.stop() if self.playwright is not None else None,
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - 关闭失败不能影响主流程
                pass


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
        # 本轮结果目录与结果块（新增三：result.txt 为纯文本，每条任务完成即重写落盘）
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
    def running(self) -> bool:
        """是否正在运行（清除登录态等破坏性操作要先看它）。"""
        with self._lock:
            return bool(self._running)

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

        保存位置留空表示程序根目录；顶层文件夹名默认「results」。
        """
        raw = str(getattr(self.cfg, "results_location", "") or "").strip()
        base = os.path.abspath(os.path.expanduser(raw)) if raw else _program_root()
        name = _safe_filename(str(getattr(self.cfg, "results_root_name", "") or "results"), "results")
        return os.path.join(base, name)

    @property
    def task_name(self) -> str:
        """本次运行的任务名称（第三层目录名）。"""
        return _safe_filename(str(getattr(self.cfg, "task_name", "") or "auto-task"), "auto-task")[:60]

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
        """本轮结果文件路径（固定命名 ``result.txt``，纯文本无 Markdown）。"""
        return os.path.join(self.results_dir, "result.txt")

    def _build_report_header(self) -> str:
        """结果文件的文件头：一眼看清这批结果属于哪次运行。"""
        return "\n".join(
            [
                _t("automation.report.title"),
                "=" * 40,
                _t("automation.report.task_name", name=self.task_name),
                _t("automation.report.date", date=_today()),
                _t("automation.report.started_at", time=_now_iso()),
                _t("automation.report.task_list_url", url=self.task_list_url),
                _t("automation.report.instruction",
                   instruction=str(getattr(self.cfg, "instruction", "") or "")),
                _t("automation.report.results_dir", path=self.results_dir),
            ]
        )

    def _write_report(self, blocks: Sequence[str], header: str) -> str:
        """把结果写入 ``result.txt``（纯文本）。

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
        # 平台边界排在最前（任务五）：非 Windows 上"没装 Playwright"不是真正的原因，
        # 先说平台限制，用户才不会去装一个装了也没用的包。
        try:
            import platform_support

            if not platform_support.automation_available():
                errors.append(_t("automation.validate.platform_unsupported"))
        except Exception:  # noqa: BLE001 - 平台模块不可用时退回原有校验
            pass
        if not self.safety_accepted:
            errors.append(_t("automation.validate.safety_not_accepted"))
        if not self.enabled:
            errors.append(_t("automation.validate.disabled"))
        if _sync_playwright is None:
            errors.append(_t("automation.validate.no_playwright"))

        url = self.task_list_url
        if not url:
            errors.append(_t("automation.validate.no_url"))
        else:
            scheme = (urlparse(url).scheme or "").lower()
            if scheme not in ("http", "https", "file"):
                errors.append(_t("automation.validate.bad_scheme",
                                 scheme=scheme or _t("automation.value.empty_paren")))
        if not str(getattr(self.cfg, "task_list_selector", "") or "").strip():
            errors.append(_t("automation.validate.no_list_selector"))
        if not str(getattr(self.cfg, "task_detail_file_selector", "") or "").strip():
            errors.append(_t("automation.validate.no_detail_selector"))
        if not self.file_extensions:
            errors.append(_t("automation.validate.no_extensions"))
        try:
            if int(getattr(self.cfg, "max_tasks", 20) or 0) < 1:
                errors.append(_t("automation.validate.max_tasks_zero"))
        except Exception:  # noqa: BLE001
            errors.append(_t("automation.validate.max_tasks_invalid"))
        if self.llm is None or not hasattr(self.llm, "chat_complete"):
            errors.append(_t("automation.validate.no_model"))
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

    @property
    def profile_dir(self) -> str:
        """自动化专用的**持久化用户数据目录**（v0.2.1 自动化优化 任务一）。

        位置：``automation.login_state_dir``；留空时取 ``browser_state.json`` 同级的
        ``browser_profile``。任务书要求"独立数据目录，不与用户日常浏览器共用"——
        浏览器不允许两个实例共用同一个 user data dir，共用会直接启动失败。
        """
        raw = str(getattr(self.cfg, "login_state_dir", "") or "").strip()
        if raw:
            return os.path.abspath(os.path.expanduser(raw))
        state = self.login_state_file
        base = os.path.dirname(state) if state else ""
        return os.path.join(base or os.getcwd(), "browser_profile")

    @property
    def login_url_keywords(self) -> List[str]:
        """判定"这是登录页"的 URL 关键词（小写）。"""
        raw = getattr(self.cfg, "login_url_keywords", None) or []
        if isinstance(raw, str):
            raw = raw.replace("，", ",").split(",")
        out: List[str] = []
        for item in raw:
            word = str(item).strip().lower()
            if word and word not in out:
                out.append(word)
        return out

    def profile_ready(self) -> bool:
        """数据目录是否已经存在（= 至少启动过一次，可能已有登录态）。"""
        try:
            return os.path.isdir(self.profile_dir)
        except Exception:  # noqa: BLE001
            return False

    def login_state_status(self) -> Dict[str, Any]:
        """登录态现状（给设置界面显示，不含任何 Cookie 内容）。"""
        profile = self.profile_dir
        exists = os.path.isdir(profile)
        size = 0
        modified = ""
        if exists:
            try:
                for current, _dirs, files in os.walk(profile):
                    for name in files:
                        try:
                            size += os.path.getsize(os.path.join(current, name))
                        except OSError:
                            continue
                modified = datetime.fromtimestamp(os.path.getmtime(profile)).strftime("%Y-%m-%d %H:%M")
            except Exception:  # noqa: BLE001
                pass
        state_file = self.login_state_file
        return {
            "profile_dir": profile,
            "exists": exists,
            "size_bytes": size,
            "modified": modified,
            "legacy_state_file": state_file,
            "legacy_exists": bool(state_file) and os.path.isfile(state_file),
            "login_check_selector": str(getattr(self.cfg, "login_check_selector", "") or ""),
            "url_keywords": self.login_url_keywords,
            "runnable": not self.running,
        }

    def clear_login_state(self) -> Dict[str, Any]:
        """清除自动化登录态：删除持久化数据目录（下次运行需重新登录）。

        任务书「任务一 清除登录态」与「任务四 审计日志记录清除事件」。
        返回 ``{"ok", "profile_dir", "removed_files", "legacy_removed", "error"}``。
        删除失败（例如浏览器还开着、文件被占用）会如实返回错误而不是假装成功。
        """
        result: Dict[str, Any] = {
            "ok": False, "profile_dir": self.profile_dir, "removed_files": 0,
            "legacy_removed": False, "error": "",
        }
        if self.running:
            result["error"] = "running"
            return result
        profile = self.profile_dir
        if os.path.isdir(profile):
            count = 0
            try:
                for _current, _dirs, files in os.walk(profile):
                    count += len(files)
            except Exception:  # noqa: BLE001
                count = 0
            try:
                shutil.rmtree(profile)
            except Exception as exc:  # noqa: BLE001
                result["error"] = _redact(exc, 200)
                self.logger.warning("清除自动化登录态失败：%s", _redact(exc, 200))
                return result
            result["removed_files"] = count
        # 旧机制的 storage_state 备份文件里也有 Cookie，一并清掉才算"清除登录态"
        state_file = self.login_state_file
        if state_file and os.path.isfile(state_file):
            try:
                os.remove(state_file)
                result["legacy_removed"] = True
            except Exception as exc:  # noqa: BLE001
                result["error"] = _redact(exc, 200)
        result["ok"] = not os.path.isdir(profile)
        self._audit("login_state_cleared", result="ok" if result["ok"] else "failed",
                    error=result["error"], files_removed=int(result["removed_files"]),
                    legacy_removed=bool(result["legacy_removed"]))
        return result

    def _open_session(self) -> "_BrowserSession":
        """启动有头浏览器并打开**持久化上下文**（登录态由浏览器自己保存与复用）。

        通道（``browser_channel``，默认 msedge）失败时回退默认 Chromium；两条路都失败
        才抛 AutomationError。``note`` 里带可读的回退说明，由调用方转成进度事件。
        """
        if _sync_playwright is None:
            raise AutomationError(_t("automation.validate.no_playwright"))
        profile = self.profile_dir
        first_run = not os.path.isdir(profile)
        try:
            os.makedirs(profile, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(
                _t("automation.browser.profile_dir_failed", path=profile, error=_redact(exc, 150))
            ) from exc

        playwright = _sync_playwright().start()
        channel = str(getattr(self.cfg, "browser_channel", "msedge") or "").strip()
        kwargs: Dict[str, Any] = {
            "user_data_dir": profile,
            "headless": False,          # 固定有头：用户要能看见并手动登录
            "viewport": {"width": 1440, "height": 900},
            "accept_downloads": True,
        }
        note = ""
        context = None
        if channel:
            try:
                context = playwright.chromium.launch_persistent_context(channel=channel, **kwargs)
            except Exception as exc:  # noqa: BLE001
                note = _t("automation.browser.channel_fallback", channel=channel,
                          error=_redact(exc, 120))
                self.logger.warning(note)
        if context is None:
            try:
                context = playwright.chromium.launch_persistent_context(**kwargs)
            except Exception as exc:  # noqa: BLE001
                try:
                    playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                detail = _t("automation.browser.chromium_also_failed",
                            note=note, error=_redact(exc, 200))
                raise AutomationError(
                    _t("automation.browser.launch_failed", detail=detail)
                ) from exc

        # 首次使用该目录：把旧机制留下的 browser_state.json 里的 Cookie 导入一次，
        # 老用户升级后不必重新登录（只导入，不删除源文件）。
        migrated = self._migrate_legacy_state(context) if first_run else 0
        if migrated:
            note = (note + _t("automation.login.migrated", count=migrated)).strip()
        return _BrowserSession(playwright, context, getattr(context, "browser", None),
                               note, profile_dir=profile, first_run=first_run)

    def _migrate_legacy_state(self, context: Any) -> int:
        """把 ``browser_state.json`` 的 Cookie 导入新数据目录，返回导入条数。

        一次性迁移，失败只记日志：这是兼容路径，不能因为老文件损坏就挡住启动。
        """
        path = self.login_state_file
        if not path or not os.path.isfile(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            cookies = data.get("cookies") if isinstance(data, dict) else None
            if not cookies:
                return 0
            context.add_cookies(cookies)
            self._audit("login_state_migrated", result="ok", cookies=len(cookies))
            return len(cookies)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("迁移旧登录态失败（忽略）：%s", _redact(exc, 200))
            return 0

    def _save_storage_state(self, context: Any) -> None:
        """导出 storage_state 到 ``browser_state.json``（备份/迁移用，不是主机制）。

        主机制是持久化数据目录，浏览器自己落盘；这里额外导出一份纯 JSON，方便用户
        备份或换机搬走。**只写本机文件，没有任何上传行为。**
        """
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
            raise AutomationError(_t("automation.browser.goto_failed",
                                     error=_redact(exc, 200))) from exc

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

    @staticmethod
    def _read_href(handle: Any, base_url: str) -> _HrefInfo:
        """任务二第 2~3 步：对**已定位到的元素**读取地址并规范化为绝对 URL。

        读取顺序与网页实际写法对齐（前一个取不到才试下一个）：

        1. ``href`` 属性 —— 标准写法；
        2. JS 求值 ``el.getAttribute('href') || el.href || data-href || data-url``
           —— 覆盖"用 ``<div data-href>`` / ``<li data-url>`` 承载地址"的站点；
        3. 都没有 → ``kind="empty"``，调用方据此走点击回退。

        选择器只负责第 1 步（定位），属性读取必须单独做——这正是任务书要纠正的
        "概念混淆"。
        """
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
        return _classify_href(raw, base_url)

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
        href: m.getAttribute('href') || m.href || m.getAttribute('data-href') || m.getAttribute('data-url') || ''
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
        """定位链接元素：主文档 → 同源 iframe → Shadow DOM 简单穿透。

        ``row_selector`` 非空时先定位行容器，再在行内查找目标链接（分页/行级定位场景）。
        选择器语法错误会被记录到 ``self._selector_error``，便于把"找不到元素"和
        "选择器写错了"区分开（两者对用户的处置方式完全不同）。

        去重规则（任务二修正）：**只有拿到 URL 的元素才按 URL 去重**。伪链接
        （``javascript:void(0)`` / ``#`` / 空 href）在一页里可能出现几十次且值完全相同，
        按 href 去重会把整页任务压成 1 个。
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
                self._record_selector_error(_t("automation.selector.what_row"), norm_row, exc)
                continue
            for scope in scopes:
                try:
                    handles = scope.locator(norm).all()
                except Exception as exc:  # noqa: BLE001 - 语法错误在这里暴露
                    self._record_selector_error(_t("automation.selector.what_element"), norm, exc)
                    continue
                base = ""
                try:
                    base = getattr(frame, "url", "") or page.url or ""
                except Exception:  # noqa: BLE001
                    base = ""
                for handle in handles:
                    info = self._read_href(handle, base)
                    found.append(
                        _LinkEntry(
                            text=_clip(self._handle_text(handle), 200),
                            href=info.url,
                            handle=handle,
                            frame=frame,
                            href_raw=info.raw,
                            href_kind=info.kind,
                            click_only=info.click_only,
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
                        info = _classify_href(item.get("href") or "", base)
                        entries.append(_LinkEntry(
                            text=_clip(item.get("text"), 200), href=info.url, handle=None,
                            frame=frame, href_raw=info.raw, href_kind=info.kind,
                            click_only=info.click_only,
                        ))
                    break

        # 3) 去重（保留首次出现顺序；无 URL 的伪链接一律保留，见 docstring）
        unique: List[_LinkEntry] = []
        seen: Set[str] = set()
        for entry in entries:
            if not entry.href:
                unique.append(entry)
                continue
            if entry.href in seen:
                continue
            seen.add(entry.href)
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
            _t("automation.captcha.waiting", limit=limit),
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
                yield self._event("delta", _t("automation.captcha.done"))
                return True
        yield self._event("delta", _t("automation.captcha.timeout", limit=limit))
        return False

    # ---------- 登录态（v0.2.1 自动化优化 任务一 / 任务四） ----------

    def _login_present(self, page: Any) -> Optional[bool]:
        """判断当前页面是否处于**已登录**状态。

        返回 ``True`` 已登录 / ``False`` 未登录 / ``None`` 无法判断（不阻塞流程）。
        判定优先级（最可靠的排在最前）：

        1. 配置了「登录后才出现的元素」（``automation.login_check_selector``）时，
           该元素存在 = 已登录，不存在 = 未登录；这是唯一能真正判定"登录了没有"的方法；
        2. 没配元素时退化用 URL 关键词（默认含 login / signin / sso / cas 等）：
           URL 命中关键词 = 未登录（被跳到登录页了）；
        3. 两者都没有信息时返回 ``None``——**不猜测、不阻塞**，只记一条审计。

        任务书要求"提供一种方式判断当前是否已登录……若检测到未登录，暂停自动化并提示
        用户手动登录"，第 1 条就是那个"方式"，第 2 条是开箱即用的兜底。
        """
        selector = str(getattr(self.cfg, "login_check_selector", "") or "").strip()
        if selector:
            norm = _normalize_selector(selector)
            for frame in list(getattr(page, "frames", []) or []):
                try:
                    if frame.locator(norm).count() > 0:
                        return True
                except Exception:  # noqa: BLE001 - 选择器写错/跨域帧都当作"没找到"
                    continue
            return False
        url = ""
        try:
            url = str(getattr(page, "url", "") or "")
        except Exception:  # noqa: BLE001
            url = ""
        low = url.lower()
        if low and any(word in low for word in self.login_url_keywords):
            return False
        return None

    def _wait_login(
        self, page: Any, stop_fn: Callable[[], bool], *, first_run: bool = False,
    ) -> Iterator[Dict[str, Any]]:
        """检测到未登录时暂停，等用户手动登录；超时返回 False。

        与 ``_wait_manual``（验证码）分开：判据不同（登录态检测 vs 验证码特征），
        提示语也不同。登录完成后浏览器会自动把 Cookie 写进持久化数据目录，
        程序不需要额外动作——这正是方案 A 的核心。

        作为生成器使用：``ok = yield from self._wait_login(page, stop_fn)``。
        """
        state = self._login_present(page)
        if state is True:
            self._audit("login_state_used", url=self.task_list_url, result="ok",
                        profile=self.profile_dir)
            return True
        if state is None:
            # 无法判断（没配检测元素、URL 也不像登录页）：不打扰用户，只留痕
            self._audit("login_state_used", url=self.task_list_url, result="unknown",
                        profile=self.profile_dir)
            return True

        try:
            limit = max(0, int(getattr(self.cfg, "manual_login_wait_seconds", 180) or 0))
        except Exception:  # noqa: BLE001
            limit = 180
        yield self._event(
            "delta",
            _t("automation.login.first_run" if first_run else "automation.login.required",
               limit=limit),
        )
        self._audit("login_required", url=self.task_list_url, result="waiting",
                    first_run=bool(first_run), profile=self.profile_dir)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if stop_fn():
                return False
            try:
                page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                time.sleep(2)
            if self._login_present(page) is not False:
                yield self._event("delta", _t("automation.login.done"))
                # 登录成功：立刻把会话落盘，避免用户随后强退导致这次登录白做
                try:
                    self._save_storage_state(page.context)
                except Exception:  # noqa: BLE001
                    pass
                self._audit("login_completed", url=self.task_list_url, result="ok",
                            profile=self.profile_dir)
                return True
        yield self._event("delta", _t("automation.login.timeout", limit=limit))
        self._audit("login_timeout", url=self.task_list_url, result="failed",
                    profile=self.profile_dir)
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
            raise _FileTooLarge(_t("automation.file.too_large_with_size",
                                   mb=MAX_FILE_BYTES // (1024 * 1024), size=size))
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
        self._selector_error = _t("automation.selector.unparsable", what=what, selector=selector,
                                  error=_redact(exc, 200))
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

    # ---------- 链接跳转：href 不可用时回退到点击（任务二第 4 步） ----------

    def _click_into(
        self,
        context: Any,
        page: Any,
        entry: _LinkEntry,
        pages_before: Sequence[Any],
    ) -> Iterator[Dict[str, Any]]:
        """点击元素触发跳转，返回跳转后的页面对象（失败返回 None）。

        任务书「任务二 特殊情况回退」：href 是 ``javascript:void(0)`` / ``#`` / 空时，
        不能用 href 导航，必须回退到点击。点击后有两种可能，都要接住：

        1. **新标签页**（带 ``target="_blank"`` 或 JS ``window.open``）；
        2. **当前页跳转**（``location.href = ...``）。

        作为生成器使用：``target = yield from self._click_into(...)``。
        """
        if entry.handle is None:
            yield self._event("delta", _t("automation.link.no_clickable_handle",
                                          raw=_clip(entry.href_raw, 60)))
            return None
        before_url = ""
        try:
            before_url = str(getattr(page, "url", "") or "")
        except Exception:  # noqa: BLE001
            before_url = ""
        yield self._event("delta", _t("automation.link.click_fallback",
                                      raw=_clip(entry.href_raw, 60) or entry.href_kind))
        try:
            self._click(entry.handle)
        except Exception as exc:  # noqa: BLE001
            yield self._event("delta", _t("automation.link.click_failed",
                                          error=_redact(exc, 150)))
            return None

        # 1) 新标签页
        for candidate in self._new_pages(context, pages_before):
            yield self._event("delta", _t("automation.link.opened_new_tab"))
            return candidate

        # 2) 当前页跳转：给页面一点时间走完导航，再比对地址
        try:
            page.wait_for_load_state("domcontentloaded",
                                     timeout=min(self.page_timeout_ms, 8000))
        except Exception:  # noqa: BLE001
            pass
        self._wait_loaded(page)
        try:
            if str(getattr(page, "url", "") or "") != before_url:
                yield self._event("delta", _t("automation.link.navigated_in_place",
                                              url=_clip(page.url, 150)))
                return page
        except Exception:  # noqa: BLE001
            pass
        yield self._event("delta", _t("automation.link.click_no_navigation"))
        return None

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
            return None, _t("automation.fetch.empty_link")
        try:
            response = context.request.get(url, timeout=self.page_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return None, _t("automation.fetch.request_failed", error=_redact(exc, 150))
        try:
            if not response.ok:
                return None, _t("automation.fetch.http_error", status=response.status)
            body = response.body()
        except Exception as exc:  # noqa: BLE001
            return None, _t("automation.fetch.read_failed", error=_redact(exc, 150))
        if body is None:
            return None, _t("automation.fetch.empty_body")
        if len(body) > MAX_FILE_BYTES:
            return None, _t("automation.file.too_large", mb=MAX_FILE_BYTES // (1024 * 1024))
        name = self._name_from_response(response, url, hint)
        path = _unique_path(dest_dir, name)
        try:
            with open(path, "wb") as fh:
                fh.write(body)
        except Exception as exc:  # noqa: BLE001
            return None, _t("automation.fetch.temp_write_failed", error=_redact(exc, 150))
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
                return None, _t("automation.file.local_missing", source=_redact(source or url, 150))
            dest = _unique_path(dest_dir, _safe_filename(os.path.basename(source), "download"))
            try:
                shutil.copy2(source, dest)
            except Exception as exc:  # noqa: BLE001
                return None, _t("automation.file.local_copy_failed", error=_redact(exc, 150))
            try:
                if os.path.getsize(dest) > MAX_FILE_BYTES:
                    os.remove(dest)
                    return None, _t("automation.file.too_large", mb=MAX_FILE_BYTES // (1024 * 1024))
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
        return None, _t("automation.file.download_failed",
                        error=click_error or _t("automation.file.no_download_triggered"))

    def _parse_downloaded(self, path: str) -> Tuple[str, str]:
        """解析下载的文件，返回 (正文, 错误说明)；错误串绝不返回给 LLM。"""
        global _parse_file_with_wps
        if _parse_file_with_wps is None:
            try:
                from file_parser import parse_file_with_wps as parser  # noqa: WPS433
                _parse_file_with_wps = parser
            except Exception as exc:  # noqa: BLE001
                return "", _t("automation.parse.module_unavailable", error=_redact(exc, 150))
        try:
            text = _parse_file_with_wps(path, 0)
        except Exception as exc:  # noqa: BLE001
            return "", _t("automation.parse.exception", error=_redact(exc, 150))
        if _is_parse_error(text):
            return "", _t("automation.parse.failed", error=_redact(text, 150))
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
            raise AutomationError(_t("automation.llm.not_loaded"))
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
            raise AutomationError(_t("automation.llm.generate_failed",
                                     error=_redact(exc, 200))) from exc
        if not text or not str(text).strip():
            raise AutomationError(_t("automation.llm.empty_reply"))
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
        """记录单个任务的成功结果并刷新 ``result.txt``，返回结果文件路径。

        任务书 新增三：结果文件固定命名 ``result.txt``，且必须是**纯文本**
        （记事本无 Markdown 渲染能力），因此这里用 ``markdown_to_plain`` 剥离标记。
        """
        if not bool(getattr(self.cfg, "save_results", True)) or self._dry_run:
            return ""
        prefix = f"[{index}/{total}] " if index and total else ""
        untitled = _t("automation.result.untitled_task")
        head = f"{prefix}{title or untitled}"
        block = [
            head,
            "-" * max(8, min(len(head), 40)),
            _t("automation.result.task_url", url=url),
            _t("automation.result.processed_at", time=_now_iso()),
            _t("automation.result.attachments",
               files=_t("automation.list_sep").join(files) if files else _t("automation.list_none")),
            "",
            _t("automation.result.review_opinion"),
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
        """把失败任务的信息（含失败截图文件名）写入 ``result.txt``（任务书 新增三）。"""
        if not bool(getattr(self.cfg, "save_results", True)) or self._dry_run:
            return ""
        prefix = f"[{index}/{total}] " if index and total else ""
        untitled = _t("automation.result.untitled_task")
        failed_suffix = _t("automation.result.failed_suffix")
        head = f"{prefix}{title or untitled}{failed_suffix}"
        lines = [
            head,
            "-" * max(8, min(len(head), 40)),
            _t("automation.result.task_url", url=url),
            _t("automation.result.processed_at", time=_now_iso()),
            _t("automation.result.attachments",
               files=_t("automation.list_sep").join(files) if files else _t("automation.list_none")),
            _t("automation.result.failure_reason",
               error=error or _t("automation.result.unknown_error")),
        ]
        if screenshot:
            lines.append(_t("automation.result.failure_screenshot",
                            name=os.path.basename(screenshot)))
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
            # 发布任务书「全局要求一」：程序生成的文件名一律英文
            # （原为 `失败截图_任务{index}_{stamp}.png`）。
            path = _unique_path(self.results_dir, f"failure-screenshot-task{index}-{_stamp()}.png")
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
        ``{"ok", "count", "samples", "hrefs", "error", "kind"}``，
        并额外给出 ``href_kinds``（每个样本的链接类型，供"为什么读不到网址"的判断）。
        """
        kind = _selector_kind(selector)
        result: Dict[str, Any] = {"ok": False, "count": 0, "samples": [], "hrefs": [],
                                  "href_kinds": [], "error": "", "kind": kind}
        selector = (selector or "").strip()
        if not selector:
            result["error"] = _t("automation.test.empty_selector")
            return result
        if not (url or "").strip():
            result["error"] = _t("automation.test.empty_url")
            return result
        if _sync_playwright is None:
            result["error"] = _t("automation.test.no_playwright")
            return result

        session: Optional[_BrowserSession] = None
        try:
            session = self._open_session()
            page = session.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            self._goto(page, url.strip())
            self._wait_loaded(page)
            # 先做一次选择器语法探测：count() 对语法错误会直接抛错，对"无匹配"返回 0
            try:
                page.locator(_normalize_selector(selector)).count()
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["error"] = _t("automation.test.syntax_error", error=_redact(exc, 250))
                return result
            entries = self._collect_links(page, selector)
            take = max(0, int(limit)) if str(limit).lstrip("-").isdigit() else 5
            result["count"] = len(entries)
            result["samples"] = [_clip(entry.text, 120) for entry in entries[:take]]
            result["hrefs"] = [entry.href or "" for entry in entries[:take]]
            result["href_kinds"] = [entry.href_kind for entry in entries[:take]]
            result["ok"] = True
            if not entries:
                result["error"] = self._selector_error or _t("automation.test.no_match")
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["error"] = _redact(exc, 300)
            self.logger.warning("选择器测试失败：%s", _redact(exc, 200))
        finally:
            if session is not None:
                session.close()
        return result

    # ---------- 可视化选取（v0.2.1 任务书 4.2） ----------

    #: 注入页面的拾取脚本。悬停高亮并实时预览选择器，点击即确定，Esc 取消。
    #:
    #: 参数 ``bannerText`` 是页面顶部提示条的文案（由后端按界面语言传入，脚本里不再
    #: 硬编码中文——任务三要求提示语与程序逻辑一致，硬编码会让英文界面出现中文提示）。
    #: 返回值里除了选择器，还带上"这是不是链接元素"的判定依据：
    #: ``isLink`` / ``hasHref`` / ``linksInside``，后端据此给出"你选中的不是链接"的提示。
    PICKER_JS = r"""
(bannerText) => {
  const TEXT = bannerText || 'EchoDesk picker: hover and click the target element; press Esc to cancel';
  const existing = document.querySelector('[data-echodesk="banner"]');
  if (window.__echodeskPickerInstalled) {
    window.__echodeskPick = {picked: null, cancelled: false, hover: null};
    if (existing) existing.textContent = TEXT;
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
  banner.textContent = TEXT;
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
    // 任务三：把"这个元素是不是链接"的判定依据一并回传，后端据此给提示。
    const tag = el.tagName.toLowerCase();
    const hrefAttr = (el.getAttribute && (el.getAttribute('href')
      || el.getAttribute('data-href') || el.getAttribute('data-url'))) || '';
    let linksInside = 0;
    try { linksInside = el.querySelectorAll ? el.querySelectorAll('a').length : 0; } catch (err) { linksInside = 0; }
    state.picked = {
      selector: sel,
      count: count,
      tag: tag,
      isLink: tag === 'a',
      hasHref: !!hrefAttr,
      linksInside: linksInside,
      text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300),
      href: hrefAttr
    };
    cleanup();
  }, true);

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { state.cancelled = true; cleanup(); }
  }, true);
  return 'installed';
}
"""

    #: 四个可视化选取目标的**用途**语义（任务三：选取语义与程序逻辑对应）。
    #: ``requires_link`` 为真表示这个位置必须能读到网址（href），
    #: 为假表示它是容器或按钮，程序在后续步骤里会对它做别的处理。
    _PICK_PURPOSE: Dict[str, Tuple[str, bool]] = {
        "task_list": ("jump", True),          # 读 href 作为跳转目标
        "task_row": ("row", False),           # 行容器：在行内再找链接
        "next_page": ("click", False),        # 按钮：只点击
        "task_detail_file": ("download", True),   # 读 href 下载
        "login_check": ("presence", False),   # 只判断"在不在"，不读网址
    }

    def _preview_pick(
        self, page: Any, selector: str, target: str, picked: Dict[str, Any],
    ) -> Dict[str, Any]:
        """任务三：把"选取结果"翻译成"程序接下来会怎么做"，供用户确认。

        做三件事：

        1. **选取后预览** —— 用选择器回到页面里真实查一遍，列出"程序将从这些元素提取
           以下网址：…"，让用户确认选对了位置（而不是只回一个选择器字符串）；
        2. **非链接元素识别** —— 用户可能点到按钮、``<div>``、``<li>`` 这类没有 href 的
           元素；对"必须能读网址"的位置直接拦下并提示重新选择（否则配置会被悄悄写坏）；
        3. **空 href / JavaScript 伪链接提示** —— 这类元素提取不到网址，程序会改为点击
           触发；这里明确告诉用户，避免他以为程序"没读到链接就是坏了"。

        返回值同时进 ``done`` 事件，前端据此弹确认框。
        """
        purpose, requires_link = self._PICK_PURPOSE.get(target, ("jump", True))
        preview: Dict[str, Any] = {
            "target": target, "purpose": purpose, "requires_link": requires_link,
            "matches": 0, "links": 0, "click_only": 0, "hrefs": [], "samples": [],
            "warnings": [], "blocking": "", "kind_labels": [],
        }
        try:
            entries = self._collect_links(page, selector)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("选取预览失败：%s", _redact(exc, 150))
            entries = []

        preview["matches"] = len(entries)
        with_href = [e for e in entries if e.href]
        without = [e for e in entries if not e.href]
        preview["links"] = len(with_href)
        preview["click_only"] = len(without)
        preview["hrefs"] = [e.href for e in with_href[:5]]
        preview["samples"] = [
            (_clip(e.text, 60) or e.address or _t("automation.value.no_text"))
            for e in entries[:5]
        ]
        kinds: List[str] = []
        for entry in without:
            label = _t(f"automation.link.kind.{entry.href_kind}")
            if label not in kinds:
                kinds.append(label)
        preview["kind_labels"] = kinds

        # ---- 非链接元素识别（只对"必须能读网址"的位置拦） ----
        is_link = bool(picked.get("isLink"))
        has_href_attr = bool(picked.get("hasHref"))
        links_inside = 0
        try:
            links_inside = int(picked.get("linksInside") or 0)
        except Exception:  # noqa: BLE001
            links_inside = 0
        tag = str(picked.get("tag") or "").lower() or "?"
        if requires_link and not is_link and not has_href_attr and preview["links"] == 0:
            if links_inside > 0:
                preview["blocking"] = _t("automation.link.not_a_link_container",
                                         tag=tag, count=links_inside)
            else:
                preview["blocking"] = _t("automation.link.not_a_link", tag=tag)
            preview["warnings"].append(preview["blocking"])
            return preview

        # ---- 预览文案 ----
        lines: List[str] = []
        if purpose == "click":
            lines.append(_t("automation.link.preview_click_target"))
        elif purpose == "row":
            lines.append(_t("automation.link.preview_row_target"))
        elif purpose == "presence":
            lines.append(_t("automation.link.preview_presence_target",
                            count=preview["matches"]))
        else:
            lines.append(_t("automation.link.preview_title"))
            if with_href:
                for index, entry in enumerate(with_href[:5], 1):
                    lines.append(_t("automation.link.preview_item", index=index,
                                    url=_clip(entry.href, 120)))
                if len(with_href) > 5:
                    lines.append(_t("automation.link.preview_more", count=len(with_href)))
            else:
                lines.append(_t("automation.link.preview_none"))
        if without and purpose in ("jump", "download"):
            notice = _t("automation.link.click_only_notice",
                        kinds=_t("automation.list_sep").join(kinds)
                        or _t("automation.value.empty_paren"))
            lines.append("\n" + notice)
            preview["warnings"].append(notice)
        # 只匹配到 1 个元素时提醒一句：可视化选取生成的是"精确到这一个"的选择器，
        # 而任务列表/附件位置通常需要匹配多条。这里不拦截（单个任务站点是合法用法），
        # 但必须让用户在确认框里看得见——否则他会以为程序"只处理了第一条"是 bug。
        if purpose in ("jump", "download") and preview["matches"] == 1:
            single = _t("automation.link.preview_single_match")
            lines.append("\n" + single)
            preview["warnings"].append(single)
        preview["message"] = "".join(lines)
        return preview

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
        records/v0.2.1-研发记录.md）：

        * WebView2 需要额外引入 SDK 与系统运行时，与"尽量减小打包体积"的全局要求冲突；
        * 任务书要求"登录态与自动化共享"——WebView2 有自己的用户数据目录，做不到；
          Playwright 这套与自动化共用同一个持久化数据目录，登录态天然一致
          （v0.2.1 自动化优化后不再依赖 ``browser_state.json``，见 ``_open_session``）；
        * 自动化本来就用 Playwright 启动 Edge，复用同一引擎不增加任何新依赖，
          且选择器在同一个渲染引擎下生成与验证，结果更可靠。

        ``target`` 取值：``task_list``（任务列表元素）/ ``task_row``（任务行容器）/
        ``next_page``（下一页按钮）/ ``task_detail_file``（详情页附件）。
        后者会先用任务列表选择器点进第一个任务，再开始拾取。
        """
        yield self._event("delta", _t("automation.pick.starting"))
        if _sync_playwright is None:
            yield self._event("error", _t("automation.pick.no_playwright"))
            return
        url = (url or "").strip() or self.task_list_url
        if not url:
            yield self._event("error", _t("automation.pick.no_url"))
            return

        session: Optional[_BrowserSession] = None
        page = None
        try:
            session = self._open_session()      # 与自动化共用持久化登录态
            if session.note:
                yield self._event("delta", session.note)
            page = session.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            yield self._event("delta", _t("automation.pick.opening", url=url))
            self._goto(page, url)
            self._wait_loaded(page)
            # 任务四：可视化选取同样要能识别"没登录"——用户在这里登录一次，
            # 登录态就进了持久化目录，后续自动化与再次选取都直接复用。
            if not (yield from self._wait_login(page, lambda: self._stop.is_set(),
                                                first_run=session.first_run)):
                yield self._event("error", _t("automation.pick.login_required"))
                return

            if target == "task_detail_file":
                selector = str(getattr(self.cfg, "task_list_selector", "") or "").strip()
                if not selector:
                    yield self._event("error", _t("automation.pick.detail_needs_list_selector"))
                    return
                yield self._event("delta", _t("automation.pick.entering_first_task"))
                entries = self._collect_links(page, selector,
                                             str(getattr(self.cfg, "task_row_selector", "") or ""))
                if not entries:
                    yield self._event("error", _t("automation.pick.no_task_matched"))
                    return
                first = entries[0]
                if not first.href:
                    # 任务二：伪链接的第一个任务也能进——回退到点击
                    if first.click_only and first.handle is not None:
                        detail = yield from self._click_into(page.context, page, first,
                                                             session.pages)
                        if detail is None:
                            yield self._event("error", _t("automation.pick.first_task_no_link",
                                                          title=_clip(first.text, 40)))
                            return
                        page = detail
                    else:
                        yield self._event("error", _t("automation.pick.first_task_no_link",
                                                      title=_clip(first.text, 40)))
                        return
                else:
                    self._goto(page, first.href)
                    self._wait_loaded(page)
                yield self._event("delta", _t("automation.pick.entered_detail",
                                              title=_clip(first.text, 40)))

            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            page.evaluate(self.PICKER_JS, _t("automation.pick.banner"))
            yield self._event(
                "delta",
                _t("automation.pick.prompt", target=_target_label(target)),
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
                reason = (_t("automation.pick.cancelled") if cancelled
                          else _t("automation.pick.timeout_reason", seconds=timeout_seconds))
                yield self._event("error", _t("automation.pick.aborted", reason=reason))
                return

            selector = str(picked.get("selector") or "").strip()
            count = int(picked.get("count") or 0)
            # ---- 任务三：选取后预览 + 非链接元素识别 + 伪链接提示 ----
            preview = self._preview_pick(page, selector, target, picked)
            if preview.get("blocking"):
                # 位置选错了（点到容器/按钮）：不改配置，直接请用户重选
                self._audit("pick_rejected", url=url, result="not_a_link",
                            error=preview["blocking"], target=target)
                yield self._event("error", preview["blocking"], preview=preview,
                                  selector="", target=target)
                return
            message = (_t("automation.pick.generated", selector=selector, count=count)
                       + (_t("automation.pick.element_text",
                              text=_clip(picked.get("text"), 120)) if picked.get("text") else "")
                       + "\n" + str(preview.get("message") or ""))
            self._audit("pick_done", url=url, result="ok", target=target,
                        matches=int(preview.get("matches") or 0),
                        links=int(preview.get("links") or 0),
                        click_only=int(preview.get("click_only") or 0))
            yield self._event(
                "done",
                message,
                selector=selector,
                count=count,
                target=target,
                text=str(picked.get("text") or "")[:300],
                href=str(picked.get("href") or ""),
                preview=preview,
            )
        except Exception as exc:  # noqa: BLE001
            message = _t("automation.pick.failed", error=_redact(exc, 300))
            self.logger.warning("可视化选取失败：%s", _redact(exc, 200))
            yield self._event("error", message)
        finally:
            try:
                if session is not None and session.context is not None:
                    self._save_storage_state(session.context)   # 导出备份（主机制是数据目录）
            except Exception:  # noqa: BLE001
                pass
            if session is not None:
                session.close()

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
            result["error"] = _t("automation.test.empty_selector")
            return result
        if not (url or "").strip():
            result["error"] = _t("automation.test.empty_url")
            return result
        if _sync_playwright is None:
            result["error"] = _t("automation.test.no_playwright")
            return result

        session: Optional[_BrowserSession] = None
        try:
            session = self._open_session()
            page = session.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            self._goto(page, url.strip())
            self._wait_loaded(page)
            locator = page.locator(_normalize_selector(selector))
            try:
                count = locator.count()
            except Exception as exc:  # noqa: BLE001
                result["error"] = _t("automation.test.syntax_error", error=_redact(exc, 250))
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
                    result["error"] = _t("automation.test.click_failed", error=_redact(exc, 200))
            result["ok"] = True
            if count == 0:
                result["error"] = self._selector_error or _t("automation.test.no_match")
        except Exception as exc:  # noqa: BLE001
            result["error"] = _redact(exc, 300)
            self.logger.warning("元素测试失败：%s", _redact(exc, 200))
        finally:
            if session is not None:
                session.close()
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
            out["title"] = _t("automation.wizard.step1_title")
            if not url:
                out["detail"] = _t("automation.wizard.no_url")
                out["suggestions"] = [_t("automation.wizard.step1.no_url_tip")]
                return out
            if not selector:
                out["detail"] = _t("automation.wizard.no_list_selector")
                out["suggestions"] = [_t("automation.wizard.step1.no_selector_tip")]
                return out
            res = self.test_selector(url, selector)
            out["data"] = res
            out["samples"] = res.get("samples") or []
            if res.get("ok") and res.get("count"):
                out["ok"] = True
                samples = _t("automation.list_sep").join(out["samples"][:3]) or _t("automation.value.no_text")
                out["detail"] = _t("automation.wizard.step1.matched",
                                   count=res["count"], samples=samples)
                if row:
                    row_res = self.test_element(url, row)
                    out["data"]["row"] = row_res
                    if row_res.get("count"):
                        out["detail"] += _t("automation.wizard.step1.row_matched",
                                            count=row_res["count"])
                    else:
                        out["detail"] += _t("automation.wizard.step1.row_no_match")
                        out["suggestions"].append(_t("automation.wizard.step1.row_optional_tip"))
            elif res.get("ok"):
                out["detail"] = _t("automation.wizard.step1.no_element")
                out["suggestions"] = [
                    _t("automation.wizard.step1.suggest1"),
                    _t("automation.wizard.step1.suggest2"),
                    _t("automation.wizard.step1.suggest3"),
                ]
            else:
                out["detail"] = _t("automation.wizard.test_failed",
                                   error=res.get("error") or _t("automation.value.unknown_reason"))
                out["suggestions"] = [_t("automation.wizard.step1.fail_tip1"),
                                      _t("automation.wizard.step1.fail_tip2")]
            return out

        if int(step) == 2:
            selector = str(getattr(cfg, "next_page_selector", "") or "").strip()
            out["title"] = _t("automation.wizard.step2_title")
            if not selector:
                out["ok"] = True
                out["detail"] = _t("automation.wizard.step2.not_configured")
                return out
            if not url:
                out["detail"] = _t("automation.wizard.no_url")
                return out
            res = self.test_element(url, selector, click=True)
            out["data"] = res
            if res.get("count") and res.get("clicked"):
                out["ok"] = True
                out["detail"] = (
                    _t("automation.wizard.step2.clicked",
                       state=(_t("automation.wizard.step2.navigated") if res.get("changed")
                              else _t("automation.wizard.step2.not_navigated")))
                    + _t("automation.wizard.step2.new_url",
                         url=res.get("new_url") or _t("automation.value.unchanged"))
                )
                if not res.get("changed"):
                    out["suggestions"] = [
                        _t("automation.wizard.step2.last_page_tip"),
                        _t("automation.wizard.step2.js_tip"),
                    ]
            elif res.get("count"):
                out["detail"] = _t("automation.wizard.step2.click_failed",
                                   count=res["count"],
                                   error=res.get("error") or _t("automation.value.unknown_reason"))
            else:
                out["detail"] = _t("automation.wizard.step2.no_match")
                out["suggestions"] = [_t("automation.wizard.step2.no_match_tip1"),
                                      _t("automation.wizard.step2.no_match_tip2")]
            return out

        if int(step) == 3:
            selector = str(getattr(cfg, "task_detail_file_selector", "") or "").strip()
            out["title"] = _t("automation.wizard.step3_title")
            if not selector:
                out["detail"] = _t("automation.wizard.no_detail_selector")
                out["suggestions"] = [_t("automation.wizard.step3.no_selector_tip")]
                return out
            list_selector = str(getattr(cfg, "task_list_selector", "") or "").strip()
            if not url or not list_selector:
                out["detail"] = _t("automation.wizard.step3.needs_prereq")
                return out
            session: Optional[_BrowserSession] = None
            try:
                session = self._open_session()
                page = session.new_page()
                page.set_default_timeout(self.page_timeout_ms)
                self._goto(page, url)
                self._wait_loaded(page)
                entries = self._collect_links(
                    page, list_selector, str(getattr(cfg, "task_row_selector", "") or ""))
                if not entries:
                    out["detail"] = _t("automation.wizard.step3.no_usable_task")
                    out["suggestions"] = [_t("automation.wizard.step3.let_step1_pass")]
                    return out
                if entries[0].href:
                    self._goto(page, entries[0].href)
                    self._wait_loaded(page)
                elif entries[0].click_only and entries[0].handle is not None:
                    # 任务二回退：第一个任务是伪链接时走点击（进度事件在这里无人接收，
                    # 因为 wizard_step 是同步 API；点击失败的结果由下面的提示语体现）
                    detail = _drive(self._click_into(page.context, page, entries[0],
                                                     session.pages))
                    if detail is None:
                        out["detail"] = _t("automation.wizard.step3.no_usable_task")
                        out["suggestions"] = [_t("automation.wizard.step3.let_step1_pass")]
                        return out
                    page = detail
                else:
                    out["detail"] = _t("automation.wizard.step3.no_usable_task")
                    out["suggestions"] = [_t("automation.wizard.step3.let_step1_pass")]
                    return out
                locator = page.locator(_normalize_selector(selector))
                count = locator.count()
                samples: List[str] = []
                exts = set(self.file_extensions)
                matched_ext = 0
                link_entries = self._collect_links(page, selector)
                for index in range(min(count, 8)):
                    try:
                        text = (locator.nth(index).inner_text(timeout=2000) or "").strip().replace("\n", " ")
                    except Exception:  # noqa: BLE001
                        continue
                    if text:
                        samples.append(_clip(text, 120))
                    # 任务二：统一用 _classify_href 判定后缀，避免 javascript:# 之类的
                    # 伪链接被 urljoin 拼成"看起来像地址"的东西后误判后缀
                    info = _classify_href(
                        (locator.nth(index).get_attribute("href") or ""), page.url)
                    ext = _extension_of(info.url) or _extension_of(text)
                    if ext and ext.lstrip(".").lower() in exts:
                        matched_ext += 1
                click_only = sum(1 for e in link_entries if e.click_only)
                out["samples"] = samples
                out["data"] = {"count": int(count), "matched_extensions": matched_ext,
                               "task": _clip(entries[0].text, 60),
                               "task_url": entries[0].href or entries[0].href_raw,
                               "click_only": click_only}
                if count:
                    out["ok"] = True
                    out["detail"] = _t("automation.wizard.step3.matched",
                                       title=_clip(entries[0].text, 40), count=count,
                                       matched=matched_ext)
                    if click_only:
                        # 任务三：伪链接要明确告诉用户"后续会改成点击"
                        out["detail"] += "\n" + _t(
                            "automation.link.click_only_notice",
                            kinds=_t("automation.link.kind.javascript"))
                    if matched_ext == 0:
                        out["suggestions"] = [
                            _t("automation.wizard.step3.extensions",
                               extensions=(_t("automation.list_sep").join(sorted(exts))
                                           or _t("automation.value.empty_paren"))),
                        ]
                else:
                    out["detail"] = _t("automation.wizard.step3.no_match")
                    out["suggestions"] = [_t("automation.wizard.step3.no_match_tip1"),
                                          _t("automation.wizard.step3.no_match_tip2")]
            except Exception as exc:  # noqa: BLE001
                out["detail"] = _t("automation.wizard.test_failed", error=_redact(exc, 300))
            finally:
                if session is not None:
                    session.close()
            return out

        if int(step) == 4:
            out["title"] = _t("automation.wizard.step4_title")
            errors = self.validate()
            if errors:
                out["detail"] = _t("automation.wizard.validate_failed",
                                   errors="\n- ".join(errors))
                out["suggestions"] = [_t("automation.wizard.step4.fix_then_retry")]
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
                out["suggestions"] = [_t("automation.wizard.step4.fail_tip1"),
                                      _t("automation.wizard.step4.fail_tip2")]
            else:
                out["suggestions"] = [_t("automation.wizard.step4.ok_tip")]
            return out

        out["detail"] = _t("automation.wizard.unknown_step", step=step)
        return out

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
            message = _t("automation.run.disabled")
            if not self.safety_accepted:
                message = _t("automation.run.safety_not_accepted")
            self._last_error = message
            yield self._event("error", message)
            return
        if not self.safety_accepted:
            message = _t("automation.run.safety_required")
            self._last_error = message
            yield self._event("error", message)
            return
        if self._running:
            message = _t("automation.run.already_running")
            self._last_error = message
            yield self._event("error", message)
            return

        errors = self.validate()
        if errors:
            message = _t("automation.run.validate_failed", errors="\n- ".join(errors))
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
        session: Optional[_BrowserSession] = None
        page = None
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
                yield self._event("delta", _t("automation.run.results_saved_to", path=self.results_dir))
            else:
                self._run_task_dir = ""
                self._report_header = ""
                if dry_run:
                    yield self._event("delta", _t("automation.run.dry_run"))
            self._audit("run_start", url=self.task_list_url, result="start",
                        max_tasks=limit or getattr(self.cfg, "max_tasks", 20))
            yield self._event("delta", _t("automation.run.starting", url=self.task_list_url))

            # ---- 启动浏览器（持久化上下文：登录态自动复用，v0.2.1 任务一） ----
            yield self._event("delta", _t("automation.run.launching_browser"))
            session = self._open_session()
            if session.note:
                yield self._event("delta", session.note)
            page = session.new_page()
            page.set_default_timeout(self.page_timeout_ms)
            try:
                page.set_default_navigation_timeout(self.page_timeout_ms)
            except Exception:  # noqa: BLE001
                pass

            # ---- 打开任务列表 ----
            yield self._event("delta", _t("automation.run.opening_task_list"))
            self._goto(page, self.task_list_url)
            manual_ok = yield from self._wait_manual(page, stopped)
            if not manual_ok:
                yield self._event("delta", _t("automation.run.manual_not_done"))
            self._wait_loaded(page)
            # ---- 登录态检测（任务四）：未登录则暂停等用户登录，超时中止本轮 ----
            # 这里用 return 而不是"继续跑"：没登录的话后面每一个任务都会失败，
            # 跑完再报 20 条失败对用户毫无帮助（任务书要求"暂停并提示用户手动登录"）。
            if not (yield from self._wait_login(page, stopped, first_run=session.first_run)):
                message = _t("automation.run.login_not_done",
                             seconds=getattr(self.cfg, "manual_login_wait_seconds", 180))
                self._last_error = message
                self._save_summary_block(_t("automation.run.summary_no_task", reason=message))
                self._audit("run_end", url=self.task_list_url, result="failed", error=message,
                            **counters)
                yield self._event("error", message)
                return

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
                # 任务二：伪链接（无 href）也要收进来——它们由点击回退处理，
                # 以前用 `if e.href` 过滤会把整页"javascript:void(0)"的任务全部丢掉。
                entries = [e for e in self._collect_links(page, task_selector, row_selector)
                           if e.href or e.handle is not None]
                new_count = 0
                for entry in entries:
                    if entry.href:
                        if entry.href in seen_keys:
                            continue
                        seen_keys.add(entry.href)
                    pending.append(entry)
                    new_count += 1
                yield self._event(
                    "delta",
                    _t("automation.run.page_found", page=page_index, count=len(entries),
                       new=new_count, total=len(pending)),
                )
                if len(pending) >= max_tasks:
                    break
                if not next_selector.strip():
                    break
                if not self._click_next_page(page, next_selector):
                    yield self._event("delta", _t("automation.run.no_next_page"))
                    break
                self._wait_loaded(page)
                manual_ok = yield from self._wait_manual(page, stopped)
                if not manual_ok:
                    break

            total_tasks = min(len(pending), max_tasks)
            if total_tasks == 0:
                if stopped():
                    # 运行刚开始就被要求停止：正常收尾，不算错误
                    stop_text = _t("automation.run.stopped_no_task")
                    self._save_summary_block(stop_text)
                    yield self._event(
                        "done",
                        stop_text,
                        processed=0,
                        skipped=0,
                        failed=0,
                        results=[],
                    )
                    return
                message = _t("automation.run.no_task_found")
                if self._selector_error:
                    message = _t("automation.run.list_selector_unusable", error=self._selector_error)
                self._last_error = message
                # 失败信息也写进 result.txt，避免"结果目录是空的、不知道发生了什么"
                self._save_summary_block(_t("automation.run.summary_no_task", reason=message))
                self._audit("run_end", url=self.task_list_url, result="failed", error=message, **counters)
                yield self._event("error", message)
                return
            yield self._event("delta", _t("automation.run.tasks_found", count=len(pending),
                                          total=total_tasks))

            # ---- 逐个处理任务 ----
            for index, entry in enumerate(pending[:total_tasks], 1):
                if stopped():
                    yield self._event("delta", _t("automation.run.stop_requested"))
                    break
                title = entry.text or _t("automation.task.fallback_title", index=index)
                url_key = self._url_key(entry.href)
                if bool(getattr(self.cfg, "dedupe", True)) and url_key in processed_urls:
                    counters["skipped"] += 1
                    yield self._event("delta", _t("automation.task.already_processed",
                                                  index=index, total=total_tasks,
                                                  title=_clip(title, 40)))
                    self._audit("task", url=entry.href, result="skipped",
                                error=_t("automation.audit.already_processed"))
                    continue

                task_tmp = ""
                outcome = _TaskOutcome(status="failed", error=_t("automation.task.not_executed"))
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
                                _t("automation.task.retry", index=index, total=total_tasks,
                                   attempt=attempt, attempts=attempts),
                            )
                        outcome = yield from self._process_one(
                            session.context, page, entry, index, total_tasks, task_tmp, stopped
                        )
                        if outcome.status == "ok":
                            break
                except Exception as exc:  # noqa: BLE001 - 单个任务异常不得中断整轮
                    outcome = _TaskOutcome(status="failed",
                                           error=_t("automation.task.exception",
                                                    error=_redact(exc, 200)))
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
                        _t("automation.task.link", url=entry.href),
                        _t("automation.result.attachments",
                           files=(_t("automation.list_sep").join(outcome.files) if outcome.files
                                  else _t("automation.list_none"))),
                        _t("automation.result.review_opinion"),
                        outcome.summary,
                    ]
                    if outcome.result_path:
                        block.append(_t("automation.task.written_to_report",
                                        name=os.path.basename(outcome.result_path)))
                    text = "\n".join(block)
                    results.append(text)
                    yield self._event("delta", _t("automation.task.done", index=index,
                                                  total=total_tasks, text=_clip(text, 2000)))
                    self._audit("task", url=entry.href, files=outcome.files, result="ok")
                else:
                    counters["failed"] += 1
                    reason = outcome.error or _t("automation.result.unknown_error")
                    # 失败信息同样写入 result.txt，并保留失败截图（任务书 新增三）
                    self._save_failure(
                        title, entry.href, outcome.files, reason,
                        index=index, total=total_tasks, screenshot=outcome.screenshot,
                    )
                    yield self._event(
                        "delta",
                        _t("automation.task.failed", index=index, total=total_tasks,
                           reason=_clip(reason, 300)),
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
            header = _t("automation.run.summary_header", ok=counters["ok"],
                        skipped=counters["skipped"], failed=counters["failed"],
                        found=len(pending), limit=total_tasks)
            if stopped():
                header = _t("automation.run.summary_stopped_prefix") + header
            if bool(getattr(self.cfg, "save_results", True)) and not dry_run:
                header += _t("automation.run.summary_results_dir", path=self.results_dir)
                self._save_summary_block(header)
            body = "\n\n".join(results) if results else _t("automation.run.no_results")
            content = header + "\n\n" + _clip(body, 6000)
            self._audit(
                "run_end", url=self.task_list_url, result="ok" if counters["failed"] == 0 else "failed",
                error=("" if counters["failed"] == 0
                       else _t("automation.audit.tasks_failed", count=counters["failed"])),
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
            message = _t("automation.run.unexpected", error=_redact(exc, 300))
            self._last_error = message
            self.logger.exception("自动化运行异常")
            self._audit("run_end", url=self.task_list_url, result="failed", error=message, **counters)
            yield self._event("error", message)
        finally:
            # 统一清理：导出登录态备份 → 关闭持久化上下文（浏览器随之关闭）→ 停 Playwright
            #                          → 删本轮临时目录
            try:
                if session is not None and session.context is not None:
                    self._save_storage_state(session.context)
            except Exception:  # noqa: BLE001
                pass
            if session is not None:
                session.close()
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
        （新增三：结果目录内保存原始文件与 result.txt），临时文件目录只用于中转。
        """
        title = entry.text or _t("automation.task.fallback_title", index=index)
        files_saved: List[str] = []
        moved_paths: Set[str] = set()   # 已移入结果目录的文件（finally 不再删除）
        names: List[str] = []
        errors: List[str] = []
        pages_before = list(getattr(context, "pages", []) or [])
        detail_page = page            # 伪链接走点击时，详情页可能是新标签页
        try:
            yield self._event("delta", _t("automation.task.opening_detail", index=index,
                                          total=total, title=_clip(title, 40)))
            if entry.href:
                # 任务二第 3 步：规范化后的绝对地址直接导航
                self._goto(page, entry.href)
            elif entry.click_only and entry.handle is not None:
                # 任务二第 4 步：伪链接（javascript: / # / 空 href）改为点击触发
                opened = yield from self._click_into(context, page, entry, pages_before)
                if opened is None:
                    return _TaskOutcome("failed", "", [], _t(
                        "automation.task.no_navigable_link", kind=entry.href_kind,
                        raw=_clip(entry.href_raw, 60)))
                detail_page = opened
            else:
                return _TaskOutcome("failed", "", [], _t("automation.task.empty_link"))
            if not (yield from self._wait_manual(detail_page, stop_fn)):
                return _TaskOutcome("failed", "", [], _t(
                    "automation.task.manual_timeout",
                    seconds=getattr(self.cfg, "manual_login_wait_seconds", 180)))
            self._wait_loaded(detail_page)
            if stop_fn():
                return _TaskOutcome("failed", "", [], _t("automation.task.stopped"))

            # ---- 定位附件链接并按后缀过滤 ----
            file_selector = str(getattr(self.cfg, "task_detail_file_selector", "") or "")
            entries = [e for e in self._collect_links(detail_page, file_selector)
                       if e.href or e.handle is not None]
            allowed = self.file_extensions
            matched: List[_LinkEntry] = []
            skipped_ext = 0
            click_only_files = 0
            for candidate in entries:
                ext = _extension_of(candidate.href) or _extension_of(candidate.text)
                if allowed and ext not in allowed:
                    skipped_ext += 1
                    continue
                if candidate.click_only:
                    click_only_files += 1
                matched.append(candidate)
                if len(matched) >= _MAX_FILES_PER_TASK:
                    break
            if skipped_ext:
                yield self._event("delta", _t("automation.task.ignored_extensions",
                                              index=index, total=total, count=skipped_ext))
            if click_only_files:
                # 任务三：明确告诉用户"这些附件读不到网址，改为点击下载"
                yield self._event("delta", _t("automation.task.click_only_files",
                                              index=index, total=total,
                                              count=click_only_files))
            if not matched:
                reason = _t("automation.task.no_matching_files")
                if self._selector_error:
                    reason = _t("automation.task.detail_selector_unusable",
                                error=self._selector_error)
                return _TaskOutcome("failed", "", [], reason)

            # ---- 逐个下载并解析 ----
            documents: List[Tuple[str, str]] = []
            for order, candidate in enumerate(matched, 1):
                if stop_fn():
                    return _TaskOutcome("failed", "", names, _t("automation.task.stopped"))
                label = (_clip(candidate.text or candidate.address, 60)
                         or _t("automation.task.fallback_file", order=order))
                yield self._event(
                    "delta",
                    _t("automation.task.downloading", index=index, total=total, label=label,
                       order=order, count=len(matched)),
                )
                path, err = self._download_entry(context, detail_page, candidate, task_tmp)
                if err or not path:
                    errors.append(_t("automation.task.file_error", label=label,
                                     error=err or _t("automation.file.download_failed_short")))
                    yield self._event("delta", _t("automation.task.file_skipped", index=index,
                                                  total=total, label=label,
                                                  error=_clip(err, 120)))
                    continue
                files_saved.append(path)
                name = os.path.basename(path)
                names.append(name)
                text, parse_err = self._parse_downloaded(path)
                if parse_err:
                    # 解析错误串绝不交给 LLM；该文件记为失败，其余文件继续
                    errors.append(_t("automation.task.file_error", label=name, error=parse_err))
                    yield self._event("delta", _t("automation.task.file_parse_failed",
                                                  index=index, total=total, name=name,
                                                  error=_clip(parse_err, 120)))
                    continue
                documents.append((name, text))

            if not documents:
                # 技能钩子：文件下载完成（即使全部失败也通知，便于技能统计）
                self._fire("after_download", task=title, index=index, total=total,
                           files=list(names), errors=list(errors), run_dir=self._run_task_dir)
                return _TaskOutcome("failed", "", names,
                                    _t("automation.list_sep_semicolon").join(errors)
                                    or _t("automation.task.no_usable_content"))

            # 技能钩子：文件下载后（任务书 2.3 的 after_download 阶段）
            self._fire("after_download", task=title, index=index, total=total,
                       files=list(names), errors=list(errors), run_dir=self._run_task_dir)

            # ---- 生成审核意见 ----
            yield self._event("delta", _t("automation.task.generating", index=index, total=total))
            summary = self._generate(documents)
            if errors:
                summary += _t("automation.task.unreadable_notice",
                              errors=_t("automation.list_sep_semicolon").join(errors))
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
                yield self._event("delta", _t("automation.task.files_kept", index=index,
                                              total=total, count=len(kept)))
            result_path = self._save_result(title, entry.address, names, summary,
                                            index=index, total=total)
            return _TaskOutcome("ok", summary, names, "", result_path)
        except Exception as exc:  # noqa: BLE001
            shot = self._screenshot(detail_page, index)
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
