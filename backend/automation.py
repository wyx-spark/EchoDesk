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
* **复用用户已经打开的浏览器（任务十七）**：默认先尝试用 CDP 连接用户**自己以调试模式
  打开**的那个浏览器（调试端口只监听 127.0.0.1），连上后**只操作他当前活动的那一个
  标签页**，不新建、不关闭、不切换他的其他标签页；连不上时按
  ``automation.browser_reuse`` 的取值决定"报错"还是"回退到程序新开实例"（回退时进度
  里会明确告知代价是**需要重新登录**）。页面内跳转一律用 ``element.click()`` 而不是
  ``page.goto()``：点击沿用当前会话与 Referer，跳出来的页面才不需要重新登录。
  详细的安全边界见下方 ``CDP_LOOPBACK_HOST`` 那一段注释。

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
import socket
import subprocess
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
from i18n import log_user
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

# ===========================================================================
# 浏览器选择（任务十六，自测问题修复-1）
# ===========================================================================
#
# 任务书要求："所有需要浏览器的功能（主要是自动化）应至少支持 Chrome，
# 进而扩展到更多浏览器"，并给出 `automation.browser` 配置项与 `auto` 语义。
#
# **实际能支持到哪一步（任务书要求 DSH 给出清单）**：
#   * 能：Chromium 系（Edge、Chrome，以及通过 `executable_path` 指定的
#     Brave / Opera / Vivaldi 等 Chromium 品牌）与 Playwright 自带的 Chromium；
#     Firefox 由 Playwright 的 `firefox` 引擎驱动。
#   * 不能：国内一些基于 **IE 内核**（Trident）或深度定制外壳的浏览器
#     （老版 360 安全浏览器兼容模式、QQ 浏览器 IE 模式、部分政务专用浏览器）
#     —— Playwright 没有 Trident 驱动，这些浏览器**无法被自动化**。
#     替代做法：用它们把页面另存/导出为文件后走"本地文档"路径，
#     或改用 Edge/Chrome 的 IE 模式打开同一站点。
#
# `channel` 为空串表示用 Playwright 自带的浏览器（无需用户另装）。
BROWSER_CHOICES: Dict[str, Dict[str, str]] = {
    "edge": {"engine": "chromium", "channel": "msedge"},
    "chrome": {"engine": "chromium", "channel": "chrome"},
    "chromium": {"engine": "chromium", "channel": ""},
    "firefox": {"engine": "firefox", "channel": ""},
}

#: ``auto`` 的候选顺序。**edge 排第一是刻意的**：v0.2.2 之前默认通道就是 msedge，
#: 保持它在最前，老用户升级后行为不变，chrome/chromium 只是多出来的兜底
#: （"只增不减"）。Chromium 系排在 Firefox 前面，因为 CDP 复用（任务十七）
#: 只对 Chromium 系有效。
AUTO_ORDER = ("edge", "chrome", "chromium", "firefox")

#: 面向用户的浏览器名（提示里写 "Google Chrome" 而不是内部代号 "chrome"）。
BROWSER_LABELS: Dict[str, str] = {
    "edge": "Microsoft Edge",
    "chrome": "Google Chrome",
    "chromium": "Chromium (Playwright)",
    "firefox": "Mozilla Firefox",
}

# ===========================================================================
# 复用**已经打开**的浏览器（任务十七，自测问题修复-1）
# ===========================================================================
#
# 用户的原话是三个诉求：① 可视化选取与自动化要在**已打开的标签页**上做；
# ② 任务列表要在已打开的标签页里找，否则又要重新登录；③ 点击链接要**模拟手动点击**，
# 这样跳出来的页面才不需要重新登录。
#
# 为什么"新开一个实例"满足不了这三条：``launch_persistent_context`` 无论怎么配，
# 得到的都是**另一个浏览器**——即使它把登录态从数据目录里带过来了，用户当前那个窗口
# 里的登录、筛选、翻页、SPA 路由状态它一概看不见。要真正接管用户手上这一份，
# 只有 CDP（Chrome DevTools Protocol）一条路：浏览器以调试模式启动后，外部程序可以
# 连上它并**接管它已有的标签页**（Playwright 的 ``connect_over_cdp``）。
#
# **CDP 只对 Chromium 系有效**（Chrome / Edge / Chromium / Brave / Vivaldi…）：
# Firefox 走的是自己的 marionette 协议、根本没有 CDP 端点，所以配置成 firefox 时
# 我们不假装能连（见 :func:`cdp_supported`），而是直接走兜底的新实例路径并如实告知。
# 后续若要支持 Firefox，扩展点在 :meth:`AutomationEngine._connect_reuse_session` ——
# 那里是"怎么拿到一个可用的 context"的唯一入口，换协议只改那一处。
#
# **安全边界（任务书第 6 条要求写进注释）**：
#   * 调试端口只监听**本机回环地址** 127.0.0.1（:data:`CDP_LOOPBACK_HOST`），程序代启动
#     浏览器时还额外显式传 ``--remote-debugging-address=127.0.0.1``。CDP 端点等价于
#     "那台浏览器的完全控制权"（能读页面、能读 Cookie、能执行脚本），暴露到局域网/公网
#     等于把用户的登录会话交出去，所以这一条不能只靠"默认行为"；
#   * **只操作用户指定的那一个标签页**：连上之后立刻用 :func:`choose_target_index` 确定
#     "用户当前活动的那一页"，此后所有操作都只落在这一页上。用户把它关掉时我们**报错**，
#     绝不"换一页继续干"——那正是任务书要求避免的"误操作其他标签页"；
#   * **不读取、不导出用户的 Cookie**：复用路径下连 ``storage_state()`` 都不调用
#     （那是给程序自己的持久化目录做备份用的，用户浏览器的凭据与我们无关）；
#   * **不关闭用户的浏览器**：复用会话的 ``_BrowserSession.close()`` 只断开连接
#     （``owns_browser=False``），既不 ``context.close()`` 也不 ``browser.close()``；
#   * 日志里只记"端口号 / 是否连上 / 目标标签页地址"，不记任何凭据。
CDP_LOOPBACK_HOST = "127.0.0.1"

#: 默认调试端口。9222 是 Chrome/Edge 的惯用值，也是文档里对用户可见的那个数字。
CDP_DEFAULT_PORT = 9222

#: 连接调试端口的超时（毫秒）。端口没人监听时是**立刻**失败的（ECONNREFUSED），
#: 这个超时只是给"端口开着但不响应"的异常情况兜底，所以不需要很长——
#: 设太大反而会让"没开调试模式"这个最常见的情况白等几十秒。
CDP_CONNECT_TIMEOUT_MS = 5000

#: 程序代启动"调试模式浏览器"后，等调试端口就绪的最长时间（秒）。
#: 浏览器冷启动 + 加载用户数据目录在机械盘上可能要好几秒，25 秒是实测后留的余量。
CDP_READY_TIMEOUT_SECONDS = 25

#: 复用策略（配置项 ``automation.browser_reuse``）的合法取值。
#: ``auto`` = 先 CDP、连不上就回退新实例（默认，兜底开着但会告知代价）；
#: ``required`` = 必须 CDP，连不上报错；``off`` = 从不碰 CDP。
REUSE_MODES = ("auto", "required", "off")

#: 支持 CDP 复用的浏览器取值。**Firefox 不在其中**（协议层不支持，不是没实现）。
CDP_BROWSER_NAMES = ("edge", "chrome", "chromium")

# 两段式可视化选取的状态（任务十七 E）。名字直接对应界面上看到的东西：
#   idle    没有进行中的选取（一切从零开始）
#   waiting **第一步**：页面已经打开/连上，等用户自己登录、导航到目标页面
#   picking **第二步**：用户点了「开始选取」，脚本已注入，高亮+点击选取中
# 一轮结束（选到 / 取消 / 页面被关）后**回到 waiting**，这样用户可以重选或换页面再选。
PICK_STATE_IDLE = "idle"
PICK_STATE_WAITING = "waiting"
PICK_STATE_PICKING = "picking"

#: 一轮选取的结局（``last_result``）。空串 = 还没发生过一轮。
#: ``rejected`` 是"选到了非链接元素"这类被程序拦下的情况：没写配置，请用户重选。
PICK_RESULTS = ("", "picked", "cancelled", "rejected", "page_closed", "error")

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


# ---------- 复用已打开的浏览器：可单测的纯逻辑（任务十七） ----------
#
# 这一组函数刻意写成**模块级纯函数**：它们承载的是任务十七里最容易出错、也最需要
# 被断言的三条规则（端口只能回环、Firefox 不能走 CDP、目标标签页怎么挑）。
# 写成纯函数后 tools/test_browser_reuse.py 可以用假对象直接断言，
# 不必真的启动浏览器——真浏览器在 CI/没有安装的机器上根本起不来，
# 而这三条规则恰恰是"不能靠人工看一眼"的那类东西。


def _cdp_endpoint(port: Any = CDP_DEFAULT_PORT) -> str:
    """构造 CDP 调试端点。

    **这里刻意不接受主机参数**：任何调用方都拼不出 ``http://0.0.0.0:9222`` 这种
    "局域网里谁都能连"的端点。调试端点等于那台浏览器的完全控制权（能读页面、能读
    Cookie、能执行脚本），所以"只连本机"不是调用方的注意事项，而是**函数签名层面
    就做不到别的事**——这是任务书第 6 条"确认不会对外暴露"最可靠的实现方式。

    端口非法（配置被手改坏、超出范围）时回落到默认端口，而不是抛异常：
    一个坏掉的配置不该让整个自动化起不来。
    """
    try:
        value = int(port)
    except Exception:  # noqa: BLE001
        value = CDP_DEFAULT_PORT
    if not 1 <= value <= 65535:
        value = CDP_DEFAULT_PORT
    return f"http://{CDP_LOOPBACK_HOST}:{value}"


def cdp_supported(browser: str) -> bool:
    """配置里的浏览器取值能不能走 CDP 复用。

    ``auto`` 返回 True：候选顺序（edge → chrome → chromium → firefox）里 Chromium 系
    排在前面，能连上就连。

    **``firefox`` 返回 False 不是"还没实现"，而是协议层就不支持**：Firefox 用
    marionette，没有 CDP 端点，重试多少次都连不上。所以上层必须据此**换一条路径**
    （走兜底新实例并如实告知"Firefox 不支持复用已打开的浏览器"），
    而不是拿一个注定失败的连接去试。
    """
    name = str(browser or "auto").strip().lower()
    if name in BROWSER_CHOICES:
        return BROWSER_CHOICES[name]["engine"] == "chromium"
    return True


def _page_url(page: Any) -> str:
    """读一个标签页的地址；页面已被关掉或读不到时返回空串（不抛异常）。

    ``is_closed()`` 要先于 ``url`` 读：页面一关，Playwright 侧读取 ``url``
    会抛 ``TargetClosedError``，而"用户把标签页关了"是复用模式下的**常见情形**
    （不是异常），必须在正常路径上处理掉。
    """
    try:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed) and is_closed():
            return ""
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def choose_target_index(pages: Sequence[Any], probe: Optional[Callable[[Any], Any]] = None) -> int:
    """在候选标签页里挑出"用户当前正在用的那一页"，返回下标；挑不出来返回 -1。

    判定顺序（每一级都只在上一级没有结论时才用）：

    1. ``focused``（``document.hasFocus()``）为真 —— 这是**真正的活动标签页**，
       一个窗口里最多只有一个，所以命中即返回；
    2. 最后一个 ``visible``（``document.visibilityState === 'visible'``）的页面 ——
       会有多个窗口各有一个可见页，取最后创建的那个（``context.pages`` 是创建顺序，
       用户刚打开的那一页在后面）；
    3. 最后一个可用页面 —— 用户把窗口最小化了、或页面还没渲染出来时也会走到这里。

    为什么需要 2、3 两级而不是只认 ``focused``：``hasFocus()`` 在浏览器**失去系统焦点**
    时全为假（用户切到了别的程序），此时"最后一个可见页"仍然是他刚在用的那一页。

    浏览器内部页面（``edge://``、``chrome://``、``about:``）与地址读不到的页一律排除：
    它们不是用户要操作的对象，混进来会让自动化跑在一个空白/设置页上。

    ``probe`` 由调用方注入（生产里是 :meth:`AutomationEngine._describe_page`，
    测试里是假回调），返回 ``{"url", "visible", "focused"}``；probe 抛异常时按
    "该页读不出状态"处理（不影响它作为候选，只是拿不到优先权）——这就是给测试留的桩点。
    """
    items = list(pages or [])
    usable: List[int] = []
    meta: List[Dict[str, bool]] = []
    for index, page in enumerate(items):
        info: Dict[str, Any] = {}
        if probe is not None:
            try:
                probed = probe(page)
                info = dict(probed) if isinstance(probed, dict) else {}
            except Exception:  # noqa: BLE001 - 探测失败按"读不出"处理
                info = {}
        url = str(info.get("url") or "") or _page_url(page)
        if not url or _is_internal_url(url):
            continue
        usable.append(index)
        meta.append({"visible": bool(info.get("visible")), "focused": bool(info.get("focused"))})
    if not usable:
        return -1
    for position, index in enumerate(usable):
        if meta[position]["focused"]:
            return index
    for position in range(len(usable) - 1, -1, -1):
        if meta[position]["visible"]:
            return usable[position]
    return usable[-1]


def _same_site(url_a: str, url_b: str) -> bool:
    """两个地址是否属于同一个站点（协议 + 主机 + 端口一致）。

    用途只有一个：复用模式下如果用户当前那一页跟配置里的「任务列表网址」不是同一个
    站点，就**提醒一句**（而不是擅自导航过去——导航会丢掉他页面上的筛选/表单状态，
    也可能把已登录的页面换成登录页）。两边只要有一边读不出主机名（``file://``、
    相对地址）就返回 False：本地文件场景没有"站点"概念，提示只会变成噪音。
    """
    try:
        a, b = urlparse(url_a or ""), urlparse(url_b or "")
    except Exception:  # noqa: BLE001
        return False
    if not a.netloc or not b.netloc:
        return False
    return (a.scheme.lower(), a.netloc.lower()) == (b.scheme.lower(), b.netloc.lower())


def debug_browser_executable(name: str) -> str:
    """找到某个浏览器的可执行文件路径（用于"用调试模式启动浏览器"）；找不到返回空串。

    **刻意不写"大而全的安装位置探测"**：跨平台的可执行文件位置本来就多（商店版、
    绿色版、非标准路径、各发行版的包名不同），写全了又长又容易误判。这里只覆盖
    最常见的位置 + ``PATH``，找不到就把"该敲什么命令"原样告诉用户，
    让他自己拿命令行启动——这比假装支持、然后报一个底层错误好。

    ``chromium``（Playwright 自带）在这里找不到是正常的：它由 Playwright 管理，
    真要用"调试模式"跑它，命令行参数是一样的（见 :meth:`AutomationEngine.launch_debug_browser`）。
    """
    target = str(name or "").strip().lower()
    if target not in CDP_BROWSER_NAMES:
        return ""
    candidates: List[str] = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if target == "edge":
            candidates += [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
        elif target == "chrome":
            candidates += [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.join(local, r"Google\Chrome\Application\chrome.exe") if local else "",
            ]
    elif sys.platform == "darwin":
        if target == "edge":
            candidates.append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        elif target == "chrome":
            candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        candidates += ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
        if target == "edge":
            candidates = ["microsoft-edge", "microsoft-edge-stable"]
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.path.isfile(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return ""


class _PickMachine:
    """可视化选取的**两段式状态机**（任务书第 4 条）。

    为什么要有它（用户的原话）：「可视化选取要加『开始选取』按钮，点击后才进入选取模式
    ——因为用户可能需要先登录、先导航到目标页面，再开始选。」

    老流程是"一发起就注入脚本、立刻进入选取模式"，问题很具体：用户还没登录、页面还停在
    登录页时，他**根本没有东西可点**；选取脚本的悬停高亮与点击拦截还会干扰他登录。
    所以拆成两段：

        idle ──open──▶ waiting ──begin──▶ picking ──finish/cancel──▶ waiting ──close──▶ idle

    * ``waiting``：页面已打开/连上，程序**什么都不注入**，只显示"请先在浏览器里完成登录、
      导航到目标页面，准备好了点开始选取"，并显示当前目标页的地址供确认；
    * ``picking``：脚本已注入，悬停高亮、点击选取、Esc 取消；
    * 一轮结束（选到 / 取消 / 页面被关）后**回到 waiting**：可重选、可换页面再选；
    * ``close`` 才真正结束这次选取（断开浏览器连接）。

    **状态放在后端而不是前端**是刻意的（任务书要求"可查询、可断言"）：选取脚本真正注入
    到了浏览器里，状态必须与那个事实一致；前端只是"反映状态"，刷新界面、切换设置页、
    甚至重开设置窗口都不会把状态弄丢。前端轮询 ``automation_pick_status`` 拿到的就是
    这里的快照。

    本类是**纯状态**（不持有任何浏览器对象），便于无浏览器环境下单测；
    浏览器句柄放在 :attr:`AutomationEngine._pick_session` 里。
    """

    def __init__(self) -> None:
        self.state = PICK_STATE_IDLE
        self.last_result = ""
        self.target = "task_list"
        self.url = ""
        self.title = ""
        self.selector = ""
        self.message = ""
        self.preview: Dict[str, Any] = {}
        self.rounds = 0            # 已经完成过几轮选取（含取消/失败）
        self.mode = ""             # reuse / debug / fresh，见 _BrowserSession.mode
        self.browser = ""          # 面向用户名的浏览器名
        self.reused = False        # 是否跑在"用户自己开着的浏览器"里
        self.note = ""             # 需要让用户知道的一句话（例如"已复用当前标签页"）
        self.started_at = 0.0
        self.updated_at = ""
        # 待写回配置的选取结果（target, selector）。由 main.py 取走一次后清空 ——
        # 这样"轮询到选了新元素"和"把结果写进配置"这两件事不会重复执行。
        self._pending_apply: Optional[Tuple[str, str]] = None

    # ---- 状态判定 ----

    @property
    def picking(self) -> bool:
        return self.state == PICK_STATE_PICKING

    @property
    def waiting(self) -> bool:
        return self.state == PICK_STATE_WAITING

    @property
    def active(self) -> bool:
        """是否处在"一次选取会话里"（等待准备或选取中）。"""
        return self.state in (PICK_STATE_WAITING, PICK_STATE_PICKING)

    # ---- 迁移 ----

    def open(self, *, target: str = "task_list", url: str = "", title: str = "",
             mode: str = "", browser: str = "", reused: bool = False, note: str = "") -> None:
        """进入第一步「等待准备」。重复调用（还在等待时换页面）是允许的。"""
        self.state = PICK_STATE_WAITING
        self.target = str(target or "task_list")
        self.url = str(url or "")
        self.title = str(title or "")
        self.mode = str(mode or "")
        self.browser = str(browser or "")
        self.reused = bool(reused)
        self.note = str(note or "")
        self.selector = ""
        self.preview = {}
        self.message = ""
        self.last_result = ""
        self._pending_apply = None
        self.started_at = time.time()
        self.updated_at = _now_iso()

    def begin(self) -> None:
        """第一步 → 第二步（用户点了「开始选取」）。

        只有 ``waiting`` 才允许开始：在 ``idle`` 上开始说明"页面还没打开"，
        在 ``picking`` 上开始说明"已经选着呢"。两种都不是能猜的情况，
        所以直接报错并让前端把状态显示出来，而不是"当成新的一轮"把状态弄乱。
        """
        if self.state == PICK_STATE_PICKING:
            raise AutomationError(_t("automation.picker.already_picking"))
        if self.state != PICK_STATE_WAITING:
            raise AutomationError(_t("automation.picker.not_ready"))
        self.state = PICK_STATE_PICKING
        self.last_result = ""
        self.updated_at = _now_iso()

    def finish(self, result: str, *, selector: str = "", message: str = "",
               preview: Optional[Dict[str, Any]] = None) -> None:
        """一轮结束（选取中 → 等待准备），记录结局，供用户重选。

        结束后**回到 waiting 而不是 idle**：用户重选时不必再走一遍"打开页面"，
        而他此刻看到的页面正是他想操作的那一页。
        """
        if self.state != PICK_STATE_PICKING:
            return
        self.state = PICK_STATE_WAITING
        self.last_result = result if result in PICK_RESULTS else "error"
        self.selector = str(selector or "")
        self.message = str(message or "")
        self.preview = dict(preview or {})
        self.rounds += 1
        self.updated_at = _now_iso()
        if self.last_result == "picked" and self.selector:
            self._pending_apply = (self.target, self.selector)

    def cancel(self, reason: str = "cancelled", message: str = "") -> None:
        """取消（Esc / 点「取消选取」）；不在选取中时是空操作。

        Esc 是**页面上**的取消入口：脚本会把 ``cancelled`` 写进页面状态，由轮询收回来
        后走这里，所以两条取消路径最终落到同一个迁移上（状态机只有一个真相）。
        """
        if self.state == PICK_STATE_PICKING:
            self.finish(reason if reason in PICK_RESULTS else "cancelled", message=message)

    def mark_page_gone(self, message: str = "") -> None:
        """目标标签页被用户关掉了。

        这是**明确错误**而不是"换一页继续"：任务书要求"不要误操作其他标签页"，
        而"用户把页面关了"之后最危险的动作就是程序自己找一页替代。
        等价于取消，但结局记为 ``page_closed``，界面上会说清楚原因。
        """
        if self.state == PICK_STATE_PICKING:
            self.finish("page_closed", message=message)
        elif self.state == PICK_STATE_WAITING:
            # 等待准备期间页面被关：没有"进行中的一轮"，但仍要让用户看到原因
            self.last_result = "page_closed"
            self.message = str(message or "")
            self.url = ""
            self.title = ""
            self.updated_at = _now_iso()

    def close(self) -> None:
        """结束这次选取，回到 idle（浏览器连接由调用方断开）。"""
        self.state = PICK_STATE_IDLE
        self.url = ""
        self.title = ""
        self.note = ""
        self.mode = ""
        self.browser = ""
        self.reused = False
        self.preview = {}
        self.updated_at = _now_iso()

    def take_pending_apply(self) -> Optional[Tuple[str, str]]:
        """取走"待写回配置的选取结果"，只成功返回一次。"""
        pending = self._pending_apply
        self._pending_apply = None
        return pending

    def snapshot(self) -> Dict[str, Any]:
        """当前状态的**纯数据**快照（不含面向用户的文案，便于单测断言）。"""
        return {
            "state": self.state,
            "stage": 2 if self.picking else (1 if self.waiting else 0),
            "active": self.active,
            "picking": self.picking,
            "last_result": self.last_result,
            "target": self.target,
            "url": self.url,
            "title": self.title,
            "selector": self.selector,
            "message": self.message,
            "preview": dict(self.preview),
            "rounds": self.rounds,
            "mode": self.mode,
            "browser": self.browser,
            "reused": self.reused,
            "note": self.note,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


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

    **任务十七（自测问题修复-1）新增三档来源（``mode``）与两处关键差别**：

    * ``fresh``  —— 兜底的老办法（``launch_persistent_context`` 开全新实例）。
      代价是用户必须重新登录一次，界面上会明确说出来。
    * ``reuse``  —— 连上用户**已经打开**的浏览器（``connect_over_cdp``），目标页是
      用户当前活动的那一个标签页。这是用户真正想要的那一档。
    * ``debug``  —— 连上"程序代启动的调试模式浏览器"。它和 ``reuse`` 在技术上完全一样
      （都是 CDP 端点），差别只在**浏览器进程由谁启动**，因此分别记名以便提示文案不同。

    ``owns_browser`` 是安全边界的一部分：**复用模式下一律为 False**，
    意味着 ``close()`` 只断开连接、绝不 ``context.close()``/``browser.close()``。
    这不是洁癖——CDP 连上的上下文就是用户的浏览器本体，关掉它等于替用户关掉
    他所有窗口和标签页；而按 Chromium 的语义，``context.close()`` 在 CDP 场景下
    会连带关闭它管理的浏览器。

    ``target_page`` 是"本次操作的那一个标签页"（任务书要求"不要误操作其他标签页"）：
    一旦确定就记在这里，后续所有动作只认它；它为 None 表示本次是新实例模式、
    需要调用方自己开一个新标签页（``AutomationEngine._work_page``）。
    """

    def __init__(self, playwright: Any, context: Any, browser: Any = None, note: str = "",
                 profile_dir: str = "", first_run: bool = False, *, mode: str = "fresh",
                 reused: bool = False, endpoint: str = "", target_page: Any = None,
                 owns_browser: bool = True) -> None:
        self.playwright = playwright
        self.context = context
        self.browser = browser
        self.note = note
        self.profile_dir = profile_dir
        self.first_run = first_run    # 本次是否是"首次使用这个数据目录"（需要引导登录）
        self.mode = mode              # fresh | reuse | debug
        self.reused = bool(reused)    # 是否跑在"用户自己开着的浏览器"里
        self.endpoint = endpoint      # 调试端点（只用于日志/提示，永远是 127.0.0.1）
        self.target_page = target_page
        self.target_url = ""          # 目标标签页确定时的地址（供界面显示与确认）
        self.target_title = ""
        self.owns_browser = bool(owns_browser)

    @property
    def pages(self) -> List[Any]:
        return list(getattr(self.context, "pages", []) or [])

    def new_page(self) -> Any:
        return self.context.new_page()

    def close(self) -> None:
        """关闭上下文（持久化上下文会连带关掉浏览器），再停 Playwright。

        顺序不能反：先 ``context.close()`` 才能保证 Cookie / localStorage 落盘，
        否则下次启动拿不到这次刚登录的会话。

        **复用模式（``owns_browser=False``）下只断开连接**：既不关上下文也不关浏览器，
        用户那个浏览器得原样留在那儿——他的其他标签页、他的登录、他正在填的表单
        都不该因为我们的自动化跑完而消失。
        """
        if self.owns_browser:
            for closer in (
                lambda: self.context.close() if self.context is not None else None,
                lambda: self.browser.close() if self.browser is not None else None,
            ):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 关闭失败不能影响主流程
                    pass
        try:
            if self.playwright is not None:
                self.playwright.stop()
        except Exception:  # noqa: BLE001
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
        # 可视化选取的两段式状态机与它自己的浏览器会话（任务十七）：
        # **独立于自动化运行的会话**——用户可能一边看着运行日志一边选元素，
        # 两者共用一个 session 字段会互相踢掉对方的浏览器。
        self._pick_machine = _PickMachine()
        self._pick_session: Optional["_BrowserSession"] = None
        self._pick_lock = threading.Lock()

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
            log_user(self.logger, "sys.log_result_write_failed", level="warning",
                     error=_redact(exc, 200))
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
                log_user(self.logger, "sys.log_login_state_clear_failed", level="warning",
                         error=_redact(exc, 200))
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

    def _browser_candidates(self) -> List[str]:
        """给出这次要**依次尝试**的浏览器列表（任务十六）。

        优先级：用户显式指定的那个 → 旧字段 ``browser_channel`` 指向的那个
        （尊重既有配置，升级不该把人已经调好的 Edge 换掉）→ :data:`AUTO_ORDER`。
        """
        want = str(getattr(self.cfg, "browser", "auto") or "auto").strip().lower()
        if want in BROWSER_CHOICES:
            return [want]
        order: List[str] = []
        legacy = str(getattr(self.cfg, "browser_channel", "") or "").strip().lower()
        if legacy:
            for name, spec in BROWSER_CHOICES.items():
                if spec["channel"] and spec["channel"].lower() == legacy:
                    order.append(name)
        for name in AUTO_ORDER:
            if name not in order:
                order.append(name)
        return order

    def _launch_browser(self, playwright: Any, name: str,
                        kwargs: Dict[str, Any]) -> Tuple[Any, str]:
        """按候选名启动一次持久化上下文，返回 ``(context, 错误文本)``。

        **不预检"系统里装没装这个浏览器"**：跨平台的可执行文件探测既啰嗦又容易
        误判（商店版、绿色版、非标准安装路径都会漏）。直接试着启动、按真实结果
        决策更可靠，失败原因还能原样带给用户。
        """
        spec = BROWSER_CHOICES[name]
        engine = getattr(playwright, spec["engine"], None)
        if engine is None:
            return None, _t("automation.browser.engine_missing", engine=spec["engine"])
        try:
            if spec["channel"]:
                return engine.launch_persistent_context(channel=spec["channel"], **kwargs), ""
            return engine.launch_persistent_context(**kwargs), ""
        except Exception as exc:  # noqa: BLE001 —— 没装/损坏/被策略拦截都会在这里
            return None, _redact(exc, 160)

    def _open_session(self) -> "_BrowserSession":
        """启动有头浏览器并打开**持久化上下文**（登录态由浏览器自己保存与复用）。

        **任务十六（自测问题修复-1）**：不再是"只认一个通道、失败就退默认 Chromium"，
        而是按 :meth:`_browser_candidates` 给出的候选**依次尝试**（默认
        ``edge → chrome → chromium → firefox``），第一个能起来的就用它。
        全部失败时把**每个浏览器各自的失败原因**拼进错误里一起给用户，
        这样"我没装 Chrome"和"浏览器被安全软件拦住"能区分开，
        而不是甩一句底层的 playwright 报错。

        ``note`` 里带可读的说明（实际用了哪个浏览器、跳过了哪几个），
        由调用方转成进度事件。
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
        kwargs: Dict[str, Any] = {
            "user_data_dir": profile,
            "headless": False,          # 固定有头：用户要能看见并手动登录
            "viewport": {"width": 1440, "height": 900},
            "accept_downloads": True,
        }
        candidates = self._browser_candidates()
        note = ""
        context = None
        used = ""
        failures: List[str] = []
        for name in candidates:
            context, error = self._launch_browser(playwright, name, kwargs)
            if context is not None:
                used = name
                break
            label = BROWSER_LABELS.get(name, name)
            failures.append(_t("automation.browser.candidate_failed",
                               browser=label, error=error))
            # 复用已有的用户可见文案键（同义同参）
            log_user(self.logger, "automation.browser.candidate_failed", level="warning",
                     browser=name, error=error)
        if context is None:
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            raise AutomationError(
                _t("automation.browser.launch_failed", detail="；".join(failures))
            )
        if len(candidates) > 1:
            note = _t("automation.browser.using",
                      browser=BROWSER_LABELS.get(used, used))

        # 首次使用该目录：把旧机制留下的 browser_state.json 里的 Cookie 导入一次，
        # 老用户升级后不必重新登录（只导入，不删除源文件）。
        migrated = self._migrate_legacy_state(context) if first_run else 0
        if migrated:
            note = (note + _t("automation.login.migrated", count=migrated)).strip()
        return _BrowserSession(playwright, context, getattr(context, "browser", None),
                               note, profile_dir=profile, first_run=first_run)

    # =====================================================================
    # 复用已打开的浏览器（任务十七，自测问题修复-1）
    # =====================================================================

    @property
    def reuse_mode(self) -> str:
        """复用策略：``auto`` / ``required`` / ``off``（非法值按 ``auto`` 处理）。

        ``auto`` 之所以是默认（用户的决定）：不让功能整体不可用，但代价必须说清楚——
        回退到新实例时进度里会明说"需要重新登录"（见 :meth:`_open_work_session`）。
        """
        raw = str(getattr(self.cfg, "browser_reuse", "auto") or "auto").strip().lower()
        return raw if raw in REUSE_MODES else "auto"

    @property
    def cdp_port(self) -> int:
        """调试端口（非法值回落默认端口，配置被手改坏也不该让功能起不来）。"""
        try:
            value = int(getattr(self.cfg, "cdp_port", CDP_DEFAULT_PORT) or CDP_DEFAULT_PORT)
        except Exception:  # noqa: BLE001
            value = CDP_DEFAULT_PORT
        return value if 1 <= value <= 65535 else CDP_DEFAULT_PORT

    @property
    def cdp_endpoint(self) -> str:
        """调试端点。**永远是 127.0.0.1**（见 :func:`_cdp_endpoint`）。"""
        return _cdp_endpoint(self.cdp_port)

    @property
    def browser_choice(self) -> str:
        """用户选的浏览器取值（``auto`` / ``edge`` / ``chrome`` / ``chromium`` / ``firefox``）。"""
        return str(getattr(self.cfg, "browser", "auto") or "auto").strip().lower()

    @property
    def browser_label(self) -> str:
        """面向用户的浏览器名（``auto`` 要展开成"依次尝试…"，否则界面上只看到一个 auto）。"""
        want = self.browser_choice
        if want == "auto":
            return _t("automation.picker.browser_auto")
        return BROWSER_LABELS.get(want, want)

    @property
    def debug_profile_dir(self) -> str:
        """程序代启动"调试模式浏览器"时用的数据目录。

        **为什么不复用 ``profile_dir``**：浏览器不允许两个实例共用同一个用户数据目录，
        而 ``profile_dir`` 是给兜底新实例（``launch_persistent_context``）用的；
        两者一旦同时开，必有一个起不来，报错还是那种难以理解的
        "ProcessSingleton / user data directory is already in use"。所以调试实例
        单独一个目录（同级加 ``_debug``）。

        放在同级是刻意的：用户改了「登录态目录」之后，调试实例的数据目录跟着一起搬，
        不会出现"登录态设到 D 盘、调试浏览器还在 C 盘"这种莫名其妙的错位。
        """
        base = self.profile_dir
        return base + "_debug"

    def _describe_page(self, page: Any) -> Dict[str, Any]:
        """读一个标签页的"自我介绍"（地址 / 标题 / 可见 / 是否聚焦）。

        只读这四项——**不读页面内容、不读 Cookie**：挑"用户正在看哪一页"只需要这四项，
        而多读一眼用户的页面内容就多一分越界的风险（任务书第 6 条）。

        ``evaluate`` 失败（页面正被关掉、正在导航、跨域帧）时返回"读不出来"，
        由 :func:`choose_target_index` 决定怎么用，**不抛异常**：
        "用户恰好在这一瞬间关了页面"是正常情形，不该变成一次崩溃。
        """
        info: Dict[str, Any] = {"url": _page_url(page), "title": "", "visible": False,
                                "focused": False}
        if page is None:
            return info
        try:
            probed = page.evaluate(
                "() => ({ url: location.href, title: document.title,"
                " visible: document.visibilityState === 'visible',"
                " focused: document.hasFocus() })"
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("读取标签页信息失败：%s", _redact(exc, 120))
            return info
        if isinstance(probed, dict):
            for key in ("url", "title", "visible", "focused"):
                if probed.get(key) is not None:
                    info[key] = probed[key]
        return info

    def _connect_reuse_session(self) -> Optional["_BrowserSession"]:
        """尝试连上**已经以调试模式打开**的浏览器；连不上返回 ``None``（不抛异常）。

        两种情形在这里合流：用户自己用 ``--remote-debugging-port`` 启动的浏览器、
        以及「用调试模式启动浏览器」由程序代启动的那个 —— 对 Playwright 来说都是
        "某个 127.0.0.1:9222 上的 CDP 端点"，没有任何区别。

        **连不上不是错误**（用户没开调试模式是常态），所以这里只记一条 debug 日志，
        把"该不该失败"留给调用方按 ``automation.browser_reuse`` 决定。

        后续要支持 Firefox 之类的非 CDP 浏览器，扩展点就在这里：
        它是"怎么拿到一个可用的 context"的唯一入口，换协议只改这一处。
        """
        if _sync_playwright is None:
            return None
        playwright = None
        try:
            playwright = _sync_playwright().start()
            browser = playwright.chromium.connect_over_cdp(
                self.cdp_endpoint, timeout=CDP_CONNECT_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 —— 端口没人监听是最常见的情况
            log_user(self.logger, "sys.log_cdp_connect_failed",
                     port=self.cdp_port, error=_redact(exc, 200))
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
            return None
        # 连上的浏览器里通常已经有 context（用户开着的那些窗口）；一个都没有时
        # （刚启动、还没开窗口）才自己新建一个 —— 新建 context 只影响我们这个调试实例，
        # 不会动用户已有的标签页。
        context = None
        contexts = list(getattr(browser, "contexts", []) or [])
        if contexts:
            context = contexts[0]
        else:
            try:
                context = browser.new_context()
            except Exception as exc:  # noqa: BLE001
                log_user(self.logger, "sys.log_debug_no_context", level="warning",
                         error=_redact(exc, 150))
                try:
                    playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                return None
        log_user(self.logger, "sys.log_cdp_connected", port=self.cdp_port)
        # owns_browser=False：**绝不关闭用户的浏览器**（见 _BrowserSession.close）
        return _BrowserSession(playwright, context, None, mode="reuse", reused=True,
                               endpoint=self.cdp_endpoint, owns_browser=False)

    def _select_target_page(self, session: "_BrowserSession") -> Optional[Any]:
        """在会话里挑出本次操作的目标标签页，并把它的地址/标题记进会话。

        挑完就**固定下来**（写进 ``session.target_page``）：任务书要求"限制在用户指定的
        页面上，不要误操作其他标签页"，而"每次都重新挑一次"正是误操作其他页的根源——
        用户切一下标签页，程序的下一个动作就会落到新页上。
        """
        pages = session.pages
        index = choose_target_index(pages, self._describe_page)
        if index < 0:
            return None
        page = pages[index]
        info = self._describe_page(page)
        session.target_page = page
        session.target_url = str(info.get("url") or _page_url(page) or "")
        session.target_title = str(info.get("title") or "")
        return page

    def _work_page(self, session: "_BrowserSession") -> Any:
        """取本次操作的目标标签页；复用模式下就是用户的当前标签页。

        复用模式（``target_page`` 已定）**不新建标签页**：用户看到的、登录过的那一页
        才是要操作的对象。只有兜底新实例（``target_page`` 为 None）才新开一页。
        """
        page = session.target_page
        if page is not None:
            return page
        page = session.new_page()
        session.target_page = page     # 记下来：本轮后续动作都只用这一页
        session.target_url = _page_url(page)
        return page

    def _enforce_target_page(self, session: "_BrowserSession") -> Any:
        """确认目标标签页还活着；被用户关掉时抛错（**不换别的页**）。

        这是任务书"不要误操作其他标签页"的执行点：页面没了就明确报错，
        让用户自己决定下一步（重新开始选取 / 重新打开页面），
        而不是由程序在你的一堆标签页里挑一个"看起来差不多"的继续干。
        """
        page = session.target_page
        if page is None:
            return self._work_page(session)
        # 只认 is_closed()：**不能拿"地址为空"当被关掉**——“刚新开的空白页”地址本来就是空的，
        # 那样会把兜底新实例的第一页误判成"用户关了页面"，功能直接不可用。
        closed = False
        try:
            closed = bool(page.is_closed())
        except Exception:  # noqa: BLE001 - 某些实现没有 is_closed，按"还活着"处理
            closed = False
        if closed:
            raise AutomationError(_t("automation.reuse.target_closed"))
        return page

    def _refresh_target_info(self, session: "_BrowserSession") -> None:
        """把目标页当前的地址/标题同步到会话上（供界面显示"我现在在哪一页"）。

        为什么每次都要重读而不是记一次就完：两段式流程里**用户会自己导航**
        （这正是任务书第 4 条的设计），"准备阶段"显示的地址必须是他此刻真正打开的那一页，
        否则他会以为程序还在看旧页面、不敢动手。
        """
        page = session.target_page
        if page is None:
            return
        info = self._describe_page(page)
        session.target_url = str(info.get("url") or _page_url(page) or "")
        session.target_title = str(info.get("title") or "")

    def _prepare_task_list_page(self, page: Any, session: "_BrowserSession",
                                url: str = "") -> str:
        """让目标页停在任务列表上；返回一句要告诉用户的话（可为空）。

        规则（任务书：点击链接模拟手动点击、任务列表在已打开的标签页里找）：

        * **复用模式**：用户当前这一页就是他日常用的那一页，**绝不导航**——
          导航会丢掉他的筛选/表单/SPA 路由状态，还可能把已登录的页面换成登录页
          （这正是用户抱怨"又要重新登录"的来源）。只有当他那一页还是空白页
          （``about:blank``，读不出地址）时才 ``goto`` 配置里的任务列表网址，
          这也是任务书允许的例外："首次打开时仍可 goto"。
        * **兜底新实例**：老行为，``goto`` 任务列表网址（``url`` 优先于配置）。

        返回的说明用来让用户在界面上确认"程序正在哪一页上干活"。
        """
        target_url = (url or "").strip() or self.task_list_url
        current = _page_url(page)
        if session.reused and current and not _is_internal_url(current):
            note = _t("automation.reuse.using_open_tab", url=_clip(current, 150))
            if target_url and not _same_site(current, target_url):
                note += _t("automation.reuse.tab_mismatch", want=_clip(target_url, 120))
            self._refresh_target_info(session)
            return note
        if target_url:
            self._goto(page, target_url)
        self._refresh_target_info(session)
        return ""

    def _open_work_session(self) -> "_BrowserSession":
        """打开一次工作会话：**优先复用用户已打开的浏览器**（任务十七的核心入口）。

        决策顺序**刻意是这样**：

        1. 浏览器选的是 ``firefox`` → 不碰 CDP。Firefox 没有 CDP 端点（协议层不支持），
           拿一个注定失败的连接去试只会白等 5 秒再报一个看不懂的错；直接走兜底并在
           note 里说明"Firefox 不支持复用已打开的浏览器"（任务书 F 条）；
        2. ``browser_reuse=off`` → 用户明确要求别碰他的浏览器，直接兜底；
        3. 连 ``127.0.0.1:<端口>`` 上的调试浏览器（用户自己开的或程序代启动的都一样）；
        4. 连不上：``required`` 就报错并给出引导（"请用调试模式启动浏览器"），
           ``auto`` 就回退到全新实例 —— 但**必须告诉用户代价是重新登录**。

        为什么默认要回退（``auto`` 的语义，也是用户拍板的那条）：不让功能整体不可用。
        一个新用户第一次点"运行"时，浏览器多半没开调试模式；此时直接报错会让他以为
        功能坏了，而回退到老办法至少能把活干完，代价只是登录一次——前提是我们
        **说清楚**。所以回退路径的 note 不是可选的装饰，是这条设计的一部分。
        """
        want = self.browser_choice
        label = self.browser_label
        mode = self.reuse_mode
        note = ""
        if mode != "off" and not cdp_supported(want):
            # Firefox：不试连接，直接兜底（任务书 F 条明确要求的边界）
            note = _t("automation.reuse.unsupported_browser", browser=label)
        elif mode != "off":
            session = self._connect_reuse_session()
            if session is not None:
                page = self._select_target_page(session)
                if page is not None:
                    session.note = _t("automation.reuse.connected",
                                      browser=label, port=self.cdp_port,
                                      url=_clip(session.target_url, 150))
                    self._audit("browser_reuse", result="ok", mode="reuse",
                                port=self.cdp_port, url=session.target_url)
                    return session
                # 连上了却没有一个可用的普通页面（全是内部页面）：不能对着
                # edge://settings 这类页面干活，关掉连接走兜底。
                log_user(self.logger, "sys.log_debug_no_tab", level="warning")
                note = _t("automation.reuse.no_usable_tab")
                session.close()
            elif mode == "required":
                self._audit("browser_reuse", result="failed", mode="required",
                            port=self.cdp_port, error="not_connected")
                raise AutomationError(
                    _t("automation.reuse.required_failed", port=self.cdp_port,
                       browser=label)
                )
            else:
                note = _t("automation.reuse.not_connected", port=self.cdp_port)
        elif mode == "off":
            note = _t("automation.reuse.disabled")

        # ---- 兜底：老路径（全新实例）----
        session = self._open_session()
        note = (note + _t("automation.reuse.fallback_cost")).strip()
        session.note = (session.note + note).strip()
        self._audit("browser_reuse", result="fallback", mode=mode,
                    port=self.cdp_port, browser=want)
        return session

    # ---------- "用调试模式启动浏览器"入口（任务十七 A.2） ----------

    def debug_browser_command(self) -> Dict[str, Any]:
        """返回"怎么以调试模式启动浏览器"的命令行（不执行）。

        ``--remote-debugging-address=127.0.0.1`` 是**显式**写上的：Chromium 默认就绑
        回环，但把安全边界交给"默认行为"是不牢靠的（默认值会变、不同分支的实现也不同），
        所以宁可多一个参数，让它在我们看得见的这一行里。
        """
        want = self.browser_choice
        if want not in CDP_BROWSER_NAMES:
            want = "chrome" if want == "auto" else want
        exe = debug_browser_executable(want)
        args = [
            f"--remote-debugging-port={self.cdp_port}",
            f"--remote-debugging-address={CDP_LOOPBACK_HOST}",
            f"--user-data-dir={self.debug_profile_dir}",
        ]
        return {
            "browser": want,
            "label": BROWSER_LABELS.get(want, want),
            "executable": exe,
            "args": args,
            "command": " ".join([exe or BROWSER_LABELS.get(want, want)] + [
                (f'"{a}"' if " " in a else a) for a in args]),
            "endpoint": self.cdp_endpoint,
            "profile_dir": self.debug_profile_dir,
        }

    def launch_debug_browser(self) -> Dict[str, Any]:
        """代启动一个带调试端口的浏览器，等端口就绪后返回。

        **这个实例不由程序关闭**（用户的设计决定）：用户可以自己在里面登录、导航，
        程序只是"连上去用一下"。所以这里既不记 pid 也不在退出时杀进程——
        他的登录态与标签页要留到他下次自己关掉为止。

        ``--user-data-dir`` 指向自动化专用的 ``*_debug`` 目录而不是用户的日常 profile，
        有两个原因，都很实际：
          1. **浏览器不允许两个实例共用同一个用户数据目录**，用日常 profile 的话
             用户只要开着浏览器，我们这个就起不来；
          2. Chromium 136 以后**拒绝在默认用户数据目录上开调试端口**（安全策略），
             硬来会得到一个端口永远不监听、看起来"启动成功了"的假象。
        """
        info = self.debug_browser_command()
        exe = info["executable"]
        if not exe:
            return {**info, "ok": False, "message": _t("automation.reuse.debug_no_exe",
                                                       browser=info["label"])}
        try:
            os.makedirs(self.debug_profile_dir, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return {**info, "ok": False,
                    "message": _t("automation.browser.profile_dir_failed",
                                  path=self.debug_profile_dir, error=_redact(exc, 150))}
        try:
            creation = 0
            if os.name == "nt":
                creation = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
            elif hasattr(os, "setsid"):
                creation = 0                        # POSIX 用下面 preexec 的 setsid
            kwargs: Dict[str, Any] = {"close_fds": True}
            if os.name == "nt":
                kwargs["creationflags"] = creation
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([exe] + info["args"], **kwargs)  # noqa: S603 - 参数是固定拼的
        except Exception as exc:  # noqa: BLE001
            return {**info, "ok": False, "message": _t("automation.reuse.debug_launch_failed",
                                                       error=_redact(exc, 200))}
        ready = self._wait_cdp_ready(CDP_READY_TIMEOUT_SECONDS)
        self._audit("debug_browser_launch", result="ok" if ready else "timeout",
                    port=self.cdp_port, browser=info["browser"])
        return {
            **info,
            "ok": ready,
            "message": (_t("automation.reuse.debug_ready", port=self.cdp_port)
                        if ready else
                        _t("automation.reuse.debug_not_ready", port=self.cdp_port)),
        }

    def _wait_cdp_ready(self, timeout_seconds: int = CDP_READY_TIMEOUT_SECONDS) -> bool:
        """轮询等待调试端口就绪（**只做 TCP 连通性探测**）。

        为什么不顺便发一个 HTTP 请求确认"这真是 CDP"：那需要构造带正确 Host 头的请求
        （Chromium 会用 Host 校验挡掉 DNS rebinding），为一个"是否就绪"的判断引入这些
        细节不划算——端口能连上，紧接着的 ``connect_over_cdp`` 会给出权威结论。
        """
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((CDP_LOOPBACK_HOST, self.cdp_port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.4)
        return False

    def browser_reuse_payload(self) -> Dict[str, Any]:
        """给前端的"复用已打开的浏览器"能力状态（**不含任何凭据**）。

        只报告配置、能力与"当前端口上有没有东西"，不报告用户的任何页面内容：
        前端需要它来显示"这次会不会复用成功"以及给出引导，仅此而已。
        """
        want = self.browser_choice
        mode = self.reuse_mode
        supported = cdp_supported(want)
        debug = self.debug_browser_command()
        return {
            "browser": want,
            "browser_label": self.browser_label,
            "browser_reuse": mode,
            "cdp_port": self.cdp_port,
            "endpoint": self.cdp_endpoint,
            "supported": supported,
            "available": self._cdp_port_open(),
            "debug_executable": debug.get("executable", ""),
            "debug_command": debug.get("command", ""),
            "debug_profile_dir": self.debug_profile_dir,
            "profile_dir": self.profile_dir,
            # 面向用户的说明（后端本地化，前端直接显示）
            "note": ("" if supported else
                     _t("automation.reuse.unsupported_browser", browser=self.browser_label)),
        }

    def _cdp_port_open(self) -> bool:
        """本机调试端口上是否已经有东西在监听（不做连接、不发请求，只探一下）。"""
        try:
            with socket.create_connection((CDP_LOOPBACK_HOST, self.cdp_port), timeout=0.4):
                return True
        except OSError:
            return False

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
            log_user(self.logger, "sys.log_login_migrate_failed", level="warning",
                     error=_redact(exc, 200))
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
            log_user(self.logger, "sys.log_login_state_save_failed", level="warning",
                     error=_redact(exc, 200))

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
            log_user(self.logger, "sys.log_page_load_timeout", level="warning",
                     url=_redact(url, 200))
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
        log_user(self.logger, "sys.log_selector_query_failed", level="warning",
                 error=_redact(self._selector_error, 200))

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
        reason: str = "fallback",
    ) -> Iterator[Dict[str, Any]]:
        """点击元素触发跳转，返回跳转后的页面对象（失败返回 None）。

        任务书「任务二 特殊情况回退」：href 是 ``javascript:void(0)`` / ``#`` / 空时，
        不能用 href 导航，必须回退到点击。点击后有两种可能，都要接住：

        1. **新标签页**（带 ``target="_blank"`` 或 JS ``window.open``）；
        2. **当前页跳转**（``location.href = ...``）。

        ``reason`` 只影响**给用户看的那句话**（任务十七）：

        * ``fallback``（默认）：这次点击是"没有可用网址"的退路，提示里要点明原因；
        * ``manual``：这次点击是**刻意的**——任务书要求"点击链接需要模拟手动点击，
          这样跳转出的页面才无需登录"。此时不该说"没有可直接打开的网址"
          （那个链接明明有），否则用户会以为程序判断错了。

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
        if reason == "manual":
            yield self._event("delta", _t("automation.link.click_manual",
                                          raw=_clip(entry.href_raw, 60) or _clip(entry.href, 60)))
        else:
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

    # ---------- 模拟手动点击进入页面（任务十七 C） ----------

    @staticmethod
    def _handle_alive(handle: Any) -> bool:
        """句柄指向的元素是否还挂在当前文档上（**快速失败**，不等待超时）。

        为什么必须问这一句：``element.click()`` 在句柄失效时不是立刻报错，
        而是**重试到超时**（默认 15 秒以上，``_click`` 里还有三级降级，最坏能拖到 30 秒）。
        每个任务白等半分钟、还只换来一个失败，体验比直接退回导航差得多。
        ``el.isConnected`` 是同步求值的，句柄一失效就立刻抛"未附着到文档"，
        毫秒级返回——用它做前置判断，代价可以忽略。
        """
        if handle is None:
            return False
        try:
            return bool(handle.evaluate("el => !!(el && el.isConnected)"))
        except Exception:  # noqa: BLE001 - 句柄失效/上下文销毁都是"不可用"
            return False

    def _relocate_entry(self, page: Any, entry: _LinkEntry) -> Optional[Any]:
        """在（可能已经重新载入的）任务列表页上，把这条任务**重新定位**成可点击句柄。

        为什么需要它：``ElementHandle`` 引用的是"某个文档里的那个元素"。每处理完一个任务
        我们会回到任务列表，页面一旦重新载入，之前收集的那批句柄就全部失效。
        而任务十七要求"进详情页要模拟手动点击"，点击就必须有一个**活着的**句柄——
        所以回到列表之后要按"地址相同 / 文本相同"把目标元素重新找回来。

        匹配顺序刻意是**地址优先、文本兜底**：地址是站点给的唯一标识，最可靠；
        文本可能因为页面上的角标（"（新）"、未读数）而变化，只能当兜底。
        找不到就返回 None，由调用方退回按地址导航，不硬撑。
        """
        selector = str(getattr(self.cfg, "task_list_selector", "") or "").strip()
        if not selector:
            return None
        row_selector = str(getattr(self.cfg, "task_row_selector", "") or "")
        try:
            candidates = self._collect_links(page, selector, row_selector)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("重新定位任务元素失败：%s", _redact(exc, 150))
            return None
        fallback: Optional[Any] = None
        for candidate in candidates:
            if candidate.handle is None:
                continue
            if entry.href and candidate.href and candidate.href == entry.href:
                return candidate.handle
            if fallback is None and entry.text and candidate.text == entry.text:
                fallback = candidate.handle
        return fallback

    def _open_task_detail(
        self,
        context: Any,
        page: Any,
        entry: _LinkEntry,
        pages_before: Sequence[Any],
    ) -> Iterator[Dict[str, Any]]:
        """进入任务详情页：**优先模拟手动点击**，点不动才退回按地址导航。

        用户的原话是这条需求的由来：「点击链接需要模拟手动点击，这样跳转出的页面才无需登录。」
        原因是内网系统常见的一类实现：服务端在**跳转时**校验 Referer 或一次性 token，
        或者前端有路由守卫；直接 ``page.goto(href)`` 会被当成"从外部直接访问"而弹回登录页，
        而 ``element.click()`` 走的是页面自己的跳转逻辑，会话、Referer、SPA 状态全部沿用。

        也就因此不能简单地"一律点击"：句柄失效（页面重新载入过）时点击是注定失败的，
        与其让用户白等，不如按地址导航进去——那种情况下"能进去"比"用点击进去"重要。
        """
        handle = entry.handle
        if not self._handle_alive(handle):
            handle = self._relocate_entry(page, entry)
        if handle is not None:
            clicked = _LinkEntry(text=entry.text, href=entry.href, handle=handle,
                                 frame=entry.frame, href_raw=entry.href_raw,
                                 href_kind=entry.href_kind, click_only=entry.click_only)
            opened = yield from self._click_into(context, page, clicked, pages_before,
                                                 reason="manual")
            if opened is not None:
                return opened
        if entry.href:
            yield self._event("delta", _t("automation.link.goto_fallback",
                                          url=_clip(entry.href, 150)))
            self._goto(page, entry.href)
            return page
        return None

    def _back_to_task_list(self, page: Any, task_selector: str) -> bool:
        """处理完一个任务后回到任务列表；返回是否"看起来回到了"。

        优先用 ``go_back()``（浏览器自己的后退）：它是**同一个会话内的历史导航**，
        保留 Referer 与 SPA 的前端状态，是"再点一次列表"之外最接近用户手动操作的路径。
        后退之后用任务选择器验一下是否真的回到了列表——很多站点点任务会新开标签页，
        此时当前页的"后退"会退到列表**之前**的那一页，必须识别出来并改用地址导航。

        验不出来（没配选择器）就直接按地址导航，保持老行为。
        """
        if task_selector.strip():
            try:
                page.go_back(wait_until="domcontentloaded", timeout=self.page_timeout_ms)
                self._wait_loaded(page)
                if self._collect_links(page, task_selector,
                                       str(getattr(self.cfg, "task_row_selector", "") or "")):
                    return True
            except Exception as exc:  # noqa: BLE001 - 没有历史/超时/被站点拦截都要兜住
                self.logger.debug("后退回列表失败：%s", _redact(exc, 150))
        if not self.task_list_url:
            return False
        self._goto(page, self.task_list_url)
        self._wait_loaded(page)
        return True

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
            log_user(self.logger, "sys.log_automation_selector_test_failed", level="warning",
                     error=_redact(exc, 200))
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
  // 任务十七：**每次都完整重装**，不再走"已安装就直接返回"的老路子。
  // 原因很具体：用户点「取消选取」后我们会把浮层摘掉（见 PICKER_STOP_JS），
  // 而老代码在这种情况下只重置了状态、不重建节点，于是"取消后再点开始选取"
  // 会出现"脚本已装好但看不见高亮框"的半死状态。
  // 重装会重复绑定一组监听器，但它们是幂等的：旧监听器只写自己闭包里那个已被替换的
  // state 对象、只改已经摘除的节点，不会再影响页面。
  document.querySelectorAll('[data-echodesk]').forEach(n => { try { n.remove(); } catch (e) {} });
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

    #: 轮询页面上的拾取结果（任务十七把这段老代码里的内联表达式提出来共用）：
    #: 两段式流程的"选取中"状态就是靠它把页面里的事实收回到后端状态机上的。
    PICKER_POLL_JS = (
        "() => { const s = window.__echodeskPick; if (!s) return null;"
        " return s.picked ? {picked: s.picked} : (s.cancelled ? {cancelled: true} : null); }"
    )

    #: 停止/清理拾取脚本（任务十七新增）：把浮层摘掉、把状态标记为取消。
    #:
    #: 为什么要把它与 PICKER_JS 分开：取消是**独立的一步**（用户可能按 Esc、也可能在
    #: 程序里点「取消选取」），而 PICKER_JS 的职责只是"装上"。分开之后，
    #: "取消 → 再点开始选取"这条路径才干净：先清理、再完整重装。
    #: ``__echodeskPickerInstalled`` 置回 false 是刻意的 —— 重装时 PICKER_JS 会重建节点，
    #: 而旧监听器只操作已摘除的节点与已被替换的 state，不会再干扰页面。
    PICKER_STOP_JS = (
        "() => { const s = window.__echodeskPick;"
        " if (s) { s.cancelled = true; s.picked = null; }"
        " document.querySelectorAll('[data-echodesk]').forEach(n => {"
        " try { n.remove(); } catch (e) {} });"
        " window.__echodeskPickerInstalled = false;"
        " return 'stopped'; }"
    )

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

        session: Optional[_BrowserSession] = None
        page = None
        try:
            # 任务十七：与自动化走同一条"复用优先"的会话入口（老 WPF 界面用的就是这条
            # 一次性流程）。复用时页面就是用户当前那一页，**不再新开标签页、不再导航**。
            session = self._open_work_session()
            if session.note:
                yield self._event("delta", session.note)
            page = self._work_page(session)
            page.set_default_timeout(self.page_timeout_ms)
            if not url and not session.reused:
                # 兜底新实例又没有网址：连"打开哪里"都不知道，只能明确报错。
                # （复用模式不需要网址——用户自己已经把页面开到想选的位置了。）
                yield self._event("error", _t("automation.pick.no_url"))
                return
            note = self._prepare_task_list_page(page, session, url)
            if note:
                yield self._event("delta", note)
            elif not session.reused:
                yield self._event("delta", _t("automation.pick.opening", url=url))
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
                if not first.href or first.handle is not None:
                    # 任务二：伪链接的第一个任务也能进——回退到点击；
                    # 任务十七：**有可点句柄时一律点击**（模拟手动点击，跳出来的
                    # 详情页沿用当前会话，不需要重新登录）。
                    detail = yield from self._click_into(page.context, page, first,
                                                         session.pages, reason="manual")
                    if detail is None:
                        if first.href:
                            # 点不动（句柄已失效等）才退回按地址导航
                            self._goto(page, first.href)
                            self._wait_loaded(page)
                        else:
                            yield self._event("error", _t("automation.pick.first_task_no_link",
                                                          title=_clip(first.text, 40)))
                            return
                    else:
                        page = detail
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
                    state = page.evaluate(self.PICKER_POLL_JS)
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
            log_user(self.logger, "sys.log_pick_failed", level="warning",
                     error=_redact(exc, 200))
            yield self._event("error", message)
        finally:
            try:
                if session is not None and session.context is not None:
                    # 复用模式下**不导出**登录态：那是用户自己浏览器的凭据，
                    # 我们没有理由把它复制到程序的数据目录里（任务书第 6 条）。
                    if not session.reused:
                        self._save_storage_state(session.context)   # 导出备份（主机制是数据目录）
            except Exception:  # noqa: BLE001
                pass
            # 无论走哪条路径结束，都先把页面上的拾取脚本收干净：留着它会让用户的页面
            # 一直有"点哪里都被拦下"的副作用（任务十七：取消/超时都要清理）。
            try:
                if page is not None and not _is_internal_url(_page_url(page)):
                    page.evaluate(self.PICKER_STOP_JS)
            except Exception:  # noqa: BLE001
                pass
            if session is not None:
                session.close()

    # =====================================================================
    # 可视化选取：两段式（任务十七 E）
    # =====================================================================
    #
    # 与上面的 ``pick_selector``（老的一次性生成器）并存，不是替代关系：
    #   * ``pick_selector`` 把"打开页面 → 注入脚本 → 等点击 → 出结果"挤在一次调用里，
    #     用户在浏览器里准备页面的那段时间**程序只是干等**，也无法告诉他"准备好再点"；
    #   * 两段式把它拆成"准备好"与"开始选"两步，中间那段时间由用户掌控：
    #     登录、翻页、点进某个任务，都发生在**注入脚本之前**，不会被打扰。
    # 老接口保持原签名与返回格式不变（``main.py`` 的 ``_stream_automation_pick``
    # 依赖 ``event["selector"]``），老界面照旧可用。

    def pick_state(self) -> Dict[str, Any]:
        """当前可视化选取状态（可查询；前端只反映它）。"""
        with self._pick_lock:
            return self._pick_payload()

    def take_pending_apply(self) -> Optional[Tuple[str, str]]:
        """取走"待写回配置的选取结果"，只成功返回一次（取不到返回 ``None``）。

        为什么要单独一个"取走"动作：前端是**轮询**状态的（每几百毫秒问一次），
        而"把选择器写进配置"只能发生一次——否则配置文件会被反复重写。
        把标记放在状态机里、由这里取走，就天然只有第一个问到的请求会拿到它。
        """
        with self._pick_lock:
            return self._pick_machine.take_pending_apply()

    def _pick_payload(self) -> Dict[str, Any]:
        """把状态机的快照翻译成前端要显示的东西（**文案在后端本地化**）。

        文案为什么由后端给而不是前端硬编码：选取状态是后端的事实（脚本注入到了浏览器里），
        对应的提示（"准备阶段做什么 / 选取阶段做什么"）必须和这个事实一一对应；
        放在后端还能一次性拿到中英双语（任务书要求所有面向用户文案走语言包），
        前端不必为"我到底该显示哪句话"再判断一遍。
        """
        snap = self._pick_machine.snapshot()
        state = snap["state"]
        last = snap["last_result"]
        if state == PICK_STATE_PICKING:
            hint = _t("automation.picker.hint_picking", target=_target_label(snap["target"]))
            state_label = _t("automation.picker.state_picking")
        elif state == PICK_STATE_WAITING:
            hint = _t("automation.picker.hint_waiting")
            state_label = _t("automation.picker.state_waiting")
        else:
            hint = _t("automation.picker.hint_idle")
            state_label = _t("automation.picker.state_idle")
        message = snap["message"]
        if not message and snap["note"]:
            message = snap["note"]
        if last == "page_closed" and not message:
            message = _t("automation.reuse.target_closed")
        return {
            **snap,
            "state_label": state_label,
            "hint": hint,
            "last_result_label": (_t(f"automation.picker.result.{last}") if last else ""),
            "ready_prompt": _t("automation.picker.ready_prompt"),
            "buttons": {
                "open": _t("automation.picker.start"),
                "begin": _t("automation.picker.begin"),
                "cancel": _t("automation.picker.cancel"),
                "close": _t("automation.picker.close"),
                "debug": _t("automation.picker.debug_browser"),
                "reload": _t("automation.picker.reload_tab"),
            },
            "labels": {
                "section": _t("automation.picker.section"),
                "target": _t("automation.picker.target_label"),
                "current": _t("automation.picker.current_label"),
                "browser": _t("automation.picker.browser_label"),
                "state": _t("automation.picker.state_label"),
                "result": _t("automation.picker.result_label"),
                "reuse": _t("automation.picker.reuse_label"),
                "cdp_port": _t("automation.picker.port_label"),
                "hint": _t("automation.picker.hint_label"),
            },
            "can_begin": state == PICK_STATE_WAITING,
            "can_cancel": state == PICK_STATE_PICKING,
            "can_close": snap["active"],
        }

    def _pick_live_page(self) -> Any:
        """取选取会话的目标页；页面没了就报错复位（**绝不换页**）。"""
        session = self._pick_session
        if session is None:
            raise AutomationError(_t("automation.picker.not_ready"))
        try:
            return self._enforce_target_page(session)
        except AutomationError:
            self._pick_machine.mark_page_gone(_t("automation.reuse.target_closed"))
            raise

    def pick_open(self, target: str = "task_list", url: str = "") -> Dict[str, Any]:
        """第一步：打开/连上目标页面，进入「等待准备」。

        打开之后程序**什么也不注入**，只把"当前在哪一页"告诉用户，让他自己完成登录与导航。
        这正是任务书第 4 条要的两段式：把"准备页面"和"选取元素"分开。
        """
        with self._pick_lock:
            machine = self._pick_machine
            if machine.picking:
                # 正在选取时不允许"重开"：那会让页面上已经装好的脚本与状态对不上
                raise AutomationError(_t("automation.picker.already_picking"))
            url = (url or "").strip() or self.task_list_url
            session = self._pick_session
            note = ""
            if session is None:
                session = self._open_work_session()
                self._pick_session = session
                page = self._work_page(session)
                note = session.note
            else:
                # 已经在会话里：重新按"用户当前活动的那一页"取一次目标页 ——
                # 用户在准备阶段自己开了新标签页（例如从任务列表点进了某个页面）
                # 是常有的事，这一步让他不必先关掉再重开。
                page = self._select_target_page(session)
                if page is None:
                    raise AutomationError(_t("automation.reuse.no_usable_tab"))
            prepared = self._prepare_task_list_page(page, session, url)
            if prepared:
                note = (note + prepared).strip()
            machine.open(target=str(target or "task_list"),
                         url=session.target_url or _page_url(page),
                         title=session.target_title,
                         mode=session.mode,
                         browser=self.browser_label,
                         reused=session.reused,
                         note=note)
            self._audit("pick_open", url=session.target_url, result="ok", target=target,
                        mode=session.mode, reused=bool(session.reused))
            return self._pick_payload()

    def pick_begin(self) -> Dict[str, Any]:
        """第二步：用户点了「开始选取」——**这时才注入脚本**。

        注入放在这一步而不是 ``pick_open``，就是为了让用户先登录、先导航：
        脚本一注入，页面上的点击就会被拦下来用于选取，这正是"准备阶段"不能被打扰的原因。
        """
        with self._pick_lock:
            machine = self._pick_machine
            machine.begin()          # 非法状态在这里直接抛错（含文案）
            try:
                page = self._pick_live_page()
            except AutomationError:
                raise
            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001 - 置前失败不影响选取本身
                pass
            try:
                page.evaluate(self.PICKER_JS, _t("automation.pick.banner"))
            except Exception as exc:  # noqa: BLE001
                # 注入失败：状态不能停在"选取中"（用户会以为在等他点，其实什么也点不到）
                machine.finish("error", message=_t("automation.picker.inject_failed",
                                                   error=_redact(exc, 200)))
                raise AutomationError(_t("automation.picker.inject_failed",
                                         error=_redact(exc, 200))) from exc
            self._audit("pick_begin", url=machine.url, result="ok", target=machine.target)
            return self._pick_payload()

    def pick_status(self) -> Dict[str, Any]:
        """查询状态；**选取中时顺带收一次结果**（前端轮询用）。

        为什么让"查询"承担收结果的职责：程序没有后台线程去盯页面，而结果本身是
        **页面里的事实**。把收取挂在轮询上，既有明确的心跳（前端每隔几百毫秒问一次），
        又不需要引入线程与它带来的一堆竞态；用户关掉程序时也不会留下后台线程。
        """
        with self._pick_lock:
            machine = self._pick_machine
            session = self._pick_session
            if machine.active and session is not None:
                try:
                    # 页面被用户关掉时必须**立刻**反映出来（而不是等他点了「开始选取」
                    # 才发现），并且绝不换一页继续 —— 任务书"不要误操作其他标签页"。
                    self._enforce_target_page(session)
                    # 用户会在准备阶段自己导航，所以每次查询都顺手刷新"当前目标页"，
                    # 让界面显示的地址始终是他此刻真正打开的那一页。
                    self._refresh_target_info(session)
                    if machine.state == PICK_STATE_WAITING:
                        if session.target_url:
                            machine.url = session.target_url
                        if session.target_title:
                            machine.title = session.target_title
                except AutomationError as exc:
                    if machine.state == PICK_STATE_WAITING:
                        machine.mark_page_gone(str(exc))
                except Exception as exc:  # noqa: BLE001 - 读不到就保持上一次的值
                    self.logger.debug("刷新目标页信息失败：%s", _redact(exc, 120))
            if machine.picking:
                self._collect_pick_result()
            return self._pick_payload()

    def _collect_pick_result(self) -> None:
        """从目标页面上收一次选取结果，并驱动状态迁移。"""
        machine = self._pick_machine
        session = self._pick_session
        page = session.target_page if session is not None else None
        closed = False
        try:
            closed = bool(page.is_closed()) if page is not None else True
        except Exception:  # noqa: BLE001
            closed = False
        if closed:
            machine.mark_page_gone(_t("automation.reuse.target_closed"))
            return
        try:
            state = page.evaluate(self.PICKER_POLL_JS)
        except Exception as exc:  # noqa: BLE001 —— 页面被关/跳转走了
            log_user(self.logger, "sys.log_pick_result_read_failed",
                     error=_redact(exc, 150))
            machine.mark_page_gone(_t("automation.reuse.target_closed"))
            return
        if not isinstance(state, dict):
            return
        if state.get("picked"):
            picked = dict(state["picked"] or {})
            selector = str(picked.get("selector") or "").strip()
            preview = self._preview_pick(page, selector, machine.target, picked)
            if preview.get("blocking"):
                # 位置选错了（点到容器/按钮）：不改配置，请用户重选（回到等待准备）
                self._audit("pick_rejected", url=machine.url, result="not_a_link",
                            error=preview["blocking"], target=machine.target)
                machine.finish("rejected", message=preview["blocking"], preview=preview)
                return
            count = int(picked.get("count") or 0)
            message = (_t("automation.pick.generated", selector=selector, count=count)
                       + (_t("automation.pick.element_text",
                             text=_clip(picked.get("text"), 120)) if picked.get("text") else "")
                       + "\n" + str(preview.get("message") or ""))
            self._audit("pick_done", url=machine.url, result="ok", target=machine.target,
                        matches=int(preview.get("matches") or 0),
                        links=int(preview.get("links") or 0),
                        click_only=int(preview.get("click_only") or 0))
            machine.finish("picked", selector=selector, message=message, preview=preview)
            return
        if state.get("cancelled"):
            machine.finish("cancelled", message=_t("automation.picker.cancelled"))

    def pick_cancel(self) -> Dict[str, Any]:
        """取消选取（页面按 Esc / 程序里点「取消选取」），回到「等待准备」。

        两条取消路径最终都落到同一个迁移上：页面上按 Esc 由轮询收回来，
        程序里点按钮则在这里直接处理。**状态机只有一个真相**，不会因为入口不同而分叉。
        """
        with self._pick_lock:
            machine = self._pick_machine
            if machine.picking:
                session = self._pick_session
                page = session.target_page if session is not None else None
                try:
                    if page is not None and not page.is_closed():
                        page.evaluate(self.PICKER_STOP_JS)
                except Exception:  # noqa: BLE001 - 页面已经没了就没什么可清理的
                    pass
                machine.finish("cancelled", message=_t("automation.picker.cancelled"))
                self._audit("pick_cancel", url=machine.url, result="cancelled",
                            target=machine.target)
            return self._pick_payload()

    def pick_close(self) -> Dict[str, Any]:
        """结束这次选取：清脚本、断开浏览器连接，回到 idle。

        **只断开连接**：复用模式下这个浏览器是用户自己的，程序没有权力关它
        （见 ``_BrowserSession.close`` 的说明）。
        """
        with self._pick_lock:
            machine = self._pick_machine
            session = self._pick_session
            self._pick_session = None
            if session is not None:
                page = session.target_page
                try:
                    if page is not None and not page.is_closed():
                        page.evaluate(self.PICKER_STOP_JS)
                except Exception:  # noqa: BLE001
                    pass
                session.close()
            machine.close()
            self._audit("pick_close", result="ok")
            return self._pick_payload()

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
            log_user(self.logger, "sys.log_element_test_failed", level="warning",
                     error=_redact(exc, 200))
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

            # ---- 启动/接管浏览器 ----
            #
            # 任务十七：这里不再写死 `_open_session()`（那永远是新实例），而是走
            # `_open_work_session()` —— 优先**连上用户已经打开的那个浏览器**，
            # 连不上才按策略回退到新实例，并在 note 里把代价（要重新登录）说清楚。
            yield self._event("delta", _t("automation.run.launching_browser"))
            session = self._open_work_session()
            if session.note:
                yield self._event("delta", session.note)
            page = self._work_page(session)
            page.set_default_timeout(self.page_timeout_ms)
            try:
                page.set_default_navigation_timeout(self.page_timeout_ms)
            except Exception:  # noqa: BLE001
                pass

            # ---- 打开任务列表 ----
            yield self._event("delta", _t("automation.run.opening_task_list"))
            # 复用模式下这一步**不会导航**（用户当前那一页就是他要处理的那一页），
            # 只回一句"程序正在哪一页上干活"；只有兜底新实例才 goto 配置里的网址。
            prepared = self._prepare_task_list_page(page, session)
            if prepared:
                yield self._event("delta", prepared)
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
                        # 任务十七：回列表优先用浏览器自己的"后退"（同会话内的历史导航，
                        # 保留 Referer 与 SPA 状态），验不出列表时 _back_to_task_list 内部
                        # 会自动改回按地址导航 —— 老行为是唯一的兜底，不会因此变差。
                        self._back_to_task_list(page, task_selector)
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
            log_user(self.logger, "sys.log_automation_run_exception", level="exception")
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
            # 任务十七 C：**一律优先模拟手动点击**（伪链接本来就必须点击）。
            # 点不动（句柄已随页面重载失效）时 _open_task_detail 内部会退回按地址导航。
            opened = yield from self._open_task_detail(context, page, entry, pages_before)
            if opened is None:
                return _TaskOutcome("failed", "", [], _t(
                    "automation.task.no_navigable_link", kind=entry.href_kind,
                    raw=_clip(entry.href_raw, 60)))
            if opened is not page:
                detail_page = opened
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
