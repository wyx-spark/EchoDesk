"""
[EN]
Emoji downgrade (task specification fix 6).

Background: emoji coverage of the fonts shipped with Windows varies widely.
Measured coverage of the 20 emoji used by this project is 0/20 for ``Segoe UI``,
while ``Segoe UI Emoji`` covers 20/20. Therefore:

* The user interface (WPF) handles this through the font fallback chain
  (``UiFontFamily`` in ``Resources/Styles.xaml``).
* The console (this module) cannot rely on fonts and must downgrade instead: it
  detects whether the current output stream can safely emit emoji and, if not,
  replaces them with ASCII markers (``[OK]`` / ``[!]``).
* Localization resources may also contain emoji; the front-end ``Localization``
  layer downgrades them at render time. The backend is only responsible for
  determining whether emoji are currently supported and reporting that through
  ``ready`` to the front end.

Downgrade conditions (any one of them triggers the downgrade):
  * ``ECHODESK_NO_EMOJI=1`` / ``--no-emoji``: explicitly requested by the user;
  * the output stream encoding is not UTF-8 (for example, a pipe redirected to
    ``cp936``);
  * stdout/stderr is a console whose code page is not UTF-8 (``chcp`` not set to
    65001).

[ZH]
Emoji 降级（任务书修复六）。

背景：Windows 自带字体的 emoji 覆盖率差异极大——实测 ``Segoe UI`` 对本项目使用的
20 个 emoji 覆盖率为 0/20，只有 ``Segoe UI Emoji`` 达到 20/20。因此：

* 界面（WPF）由字体回退链解决（``Resources/Styles.xaml`` 中的 ``UiFontFamily``）；
* 控制台（本模块）无法依赖字体，只能降级：检测当前输出流能否安全写出 emoji，
  不能则替换为 ASCII 标记（``[OK]`` / ``[!]``）；
* 语言包文本同样可能带 emoji，由前端的 ``Localization`` 在渲染时降级；后端只负责
  判断"当前是否支持 emoji"，并通过 ``ready`` 告知前端。

判断依据（任一不满足即降级）：
  * ``ECHODESK_NO_EMOJI=1`` / ``--no-emoji``：用户显式要求；
  * 输出流编码不是 UTF-8（如被重定向到 ``cp936`` 的管道）；
  * stdout/stderr 是控制台但代码页不是 UTF-8（``chcp`` 未设为 65001）。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

# [EN] Common emoji -> ASCII marker. Emoji not listed here all map to "?" so that
# [EN] no replacement glyph is ever shown: degraded information is preferable to
# [EN] garbled output.
# [ZH] 常用 emoji → ASCII 标记。未列出的 emoji 统一替换为 "?"，确保绝不出现方块：
# [ZH] 宁可信息弱一些，也不要让用户看到乱码。
ASCII_MAP: Dict[str, str] = {
    "✅": "[OK]", "❌": "[X]", "⚠️": "[!]", "⚠": "[!]",
    "⏳": "[..]", "⏱️": "[time]", "⏱": "[time]",
    # [EN] ⚡: first-token latency in runtime metrics; ⏹: abort marker
    # [EN] (known-issue optimization 2, task 7).
    # [ZH] ⚡：运行指标中的"首 token 延迟"；⏹：中止标记（已知问题优化二，相关设计）。
    "⚡": "[fast]", "⏹": "[stop]",
    "✨": "*", "🌙": "[dark]", "☀️": "[light]", "☀": "[light]",
    "📡": "[net]", "🧠": "[model]", "🛠️": "[mode]", "🛠": "[mode]",
    "📂": "[folder]", "📁": "[folder]", "📄": "[file]", "📋": "[copy]",
    "🔍": "[search]", "🔎": "[search]", "📊": "[chart]", "📈": "[chart]", "📐": "[ruler]",
    "💾": "[save]", "📥": "[in]", "📤": "[out]", "🚀": "[go]",
    "⚙️": "[cfg]", "⚙": "[cfg]", "🔧": "[cfg]", "🔧️": "[cfg]",
    "📝": "[edit]", "👤": "[user]", "🤖": "[bot]", "ℹ️": "[i]", "ℹ": "[i]",
    # [EN] 🔁: used by the "timeout retry" line of segmented summarization
    # [EN] (此前版本 general optimization, task 4).
    # [EN] 📍: used by the "step N/M" line of agent steps (此前版本 capability
    # [EN] extension, task 2).
    # [EN] A new emoji must be added both to this table and to
    # [EN] frontend\Services\Emoji.cs; otherwise it degrades to "?" on terminals
    # [EN] without emoji fonts (tools\check_emoji.ps1 enforces this).
    # [ZH] 🔁：分段摘要"超时重试"那一行使用（此前版本 综合优化，相关设计）。
    # [ZH] 📍：Agent 步骤行"第 N/M 步"使用（此前版本 能力扩展，相关设计）。
    # [ZH] 新增 emoji 必须同步补充本表与 frontend\Services\Emoji.cs，否则在无 emoji
    # [ZH] 字体的终端上会退化成 "?"（tools\check_emoji.ps1 是专门拦截此问题的门禁）。
    "🔁": "[retry]",
    "📍": "[step]",
    # [EN] ⏸: used by the MCP server "disabled" status line. It is outside the scan
    # [EN] range of check_emoji.ps1 (U+23F8 falls outside that range), so the gate
    # [EN] does not require it in this table. It is added explicitly because "not
    # [EN] checked by the gate" does not mean "cannot render as a box": on machines
    # [EN] without emoji fonts it equally requires a downgrade.
    # [ZH] ⏸：MCP server"已停用"状态行使用。它不在 check_emoji.ps1 的扫描区间内
    # [ZH] （U+23F8 落在区间之外），因此门禁不会要求它进入本表；主动补充的原因是
    # [ZH] "门禁未检查"不等于"不会显示成方块"——在无 emoji 字体的机器上它同样需要
    # [ZH] 降级。
    "⏸": "[pause]",
    "🔗": "[link]", "🧩": "[plugin]", "🔒": "[lock]", "🔓": "[unlock]",
    "⏹️": "[stop]", "⏹": "[stop]", "▶️": "[run]", "▶": "[run]",
    "↻": "[reload]", "⧉": "[md]", "✓": "[v]", "✗": "[x]",
    "🎉": "[done]", "🆕": "[new]", "🗑️": "[del]", "🗑": "[del]",
    "➕": "[+]", "➖": "[-]", "👉": "->", "🙋": "[?]",
    "📦": "[pkg]",
    "🗜": "[zip]",
    "✔": "[v]",
    "✕": "[x]",
    "❓": "[?]",
    "➤": ">",
    "⬇": "[down]",
    "⭐": "[*]",
    "🌐": "[web]",
    "🌡": "[temp]",
    "🎨": "[art]",
    "🎮": "[game]",
    "🎯": "[target]",
    "🏗": "[build]",
    "🏪": "[store]",
    "🏷": "[tag]",
    "👋": "[hi]",
    "💡": "[idea]",
    "💬": "[chat]",
    # [EN] "Thinking" bubble: used by the reasoning line of every agent step
    # [EN] (main.agent.thought). Inclusion in the downgrade table is mandatory: it
    # [EN] renders as '?' on terminals/fonts without emoji support, and this line
    # [EN] is precisely the content the user needs to read.
    # [ZH] "思考"气泡：Agent 每一步的思考行使用（main.agent.thought）。加入降级表
    # [ZH] 是必须的：终端/字体不支持时会渲染成 '?'，而该行恰恰是用户需要阅读的内容。
    "💭": "[thought]",
    "📌": "[pin]",
    "📎": "[attach]",
    "📏": "[ruler]",
    "📖": "[book]",
    "📚": "[books]",
    "🔄": "[sync]",
    "🔌": "[plug]",
    "🔹": "-",
    "🖥": "[desktop]",
    "🗂": "[files]",
    "🧪": "[test]",
    "🧵": "[thread]",
    "🪟": "[window]",
    "📉": "[trend-down]",
    # [EN] Added by the model-marketplace optimization (此前版本): filter-panel title
    # [EN] and the "offline mode" notice. 🎛️ carries a variation selector
    # [EN] (U+1F39B U+FE0F); both forms must be listed, otherwise the VS16 form still
    # [EN] degrades to "?" on systems without emoji fonts (check_emoji.ps1 enforces
    # [EN] this).
    # [ZH] 模型市场优化（此前版本）新增：筛选面板标题与"离线模式"提示。🎛️ 带变体
    # [ZH] 选择符（U+1F39B U+FE0F），两种写法都必须列出，否则带 VS16 的写法在无
    # [ZH] emoji 字体下仍会退化成 "?"（check_emoji.ps1 会拦截）。
    "🎛️": "[filter]", "🎛": "[filter]",
    "📴": "[offline]",
    # [EN] Model tier feature (此前版本 default-model switch): the
    # [EN] "🎚️ Model tier" title on the settings page. It also carries a variation
    # [EN] selector (U+1F39A U+FE0F); both forms must be listed.
    # [ZH] 模型档位（此前版本 默认模型切换）：设置页"🎚️ 模型档位"标题。同样带变体
    # [ZH] 选择符（U+1F39A U+FE0F），两种写法都必须列出。
    "🎚️": "[tiers]", "🎚": "[tiers]",
}

_enabled: Optional[bool] = None


def _stream_supports_emoji(stream: Any) -> bool:
    """
    [EN]
    Determine whether an output stream can safely emit emoji (without raising an
    exception or rendering replacement glyphs).

    [ZH]
    判断一个输出流能否安全写出 emoji（不抛异常、不显示方块）。
    """
    if stream is None:
        return False
    encoding = (getattr(stream, "encoding", "") or "").lower()
    if not encoding:
        # [EN] No encoding information (for example, a patched StringIO): treat as
        # [EN] supported to avoid an incorrect downgrade.
        # [ZH] 无编码信息（例如被替换过的 StringIO）：按支持处理，避免误降级。
        return True
    if "utf" not in encoding:
        return False
    # [EN] For a console, the code page must also be checked: consoles using
    # [EN] cp936/cp437 convert emoji into "??".
    # [ZH] 是控制台时还需检查代码页：cp936/cp437 控制台会把 emoji 变成 "??"。
    try:
        if stream.isatty():
            return _console_code_page_is_utf8()
    except Exception:
        pass
    return True


def _console_code_page_is_utf8() -> bool:
    """
    [EN]
    Report whether the Windows console code page is UTF-8 (65001). On non-Windows
    platforms, support is always assumed.

    [ZH]
    判断 Windows 控制台代码页是否为 UTF-8（65001）。非 Windows 平台一律按支持
    处理。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP()) == 65001
    except Exception:
        return False


def enabled() -> bool:
    """
    [EN]
    Report whether emoji output is currently permitted. The result is cached, so
    repeated calls add no overhead.

    [ZH]
    当前是否允许输出 emoji（结果已缓存，重复调用无额外开销）。
    """
    global _enabled
    if _enabled is not None:
        return _enabled
    if os.environ.get("ECHODESK_NO_EMOJI", "").strip() in ("1", "true", "yes", "on"):
        _enabled = False
        return _enabled
    if os.environ.get("ECHODESK_EMOJI", "").strip() in ("1", "true", "yes", "on"):
        _enabled = True
        return _enabled
    _enabled = _stream_supports_emoji(getattr(sys, "stdout", None)) and \
        _stream_supports_emoji(getattr(sys, "stderr", None))
    return _enabled


def set_enabled(value: bool) -> None:
    """
    [EN]
    Set the state explicitly (used by startup arguments and tests).

    [ZH]
    显式设置该状态（供启动参数与测试使用）。
    """
    global _enabled
    _enabled = bool(value)


def strip(text: str) -> str:
    """
    [EN]
    Replace emoji with ASCII markers regardless of the current setting. Intended for
    contexts that require plain text, such as log files.

    [ZH]
    将 emoji 替换为 ASCII 标记（无论当前是否允许输出），用于必须使用纯文本的场合，
    例如日志文件。
    """
    if not text:
        return text
    out = text
    # [EN] Replace multi-character sequences first (combinations carrying the U+FE0F
    # [EN] variation selector), then single characters.
    # [ZH] 先替换多字符序列（带 U+FE0F 变体选择符的组合），再替换单字符。
    for key in sorted(ASCII_MAP, key=len, reverse=True):
        if key in out:
            out = out.replace(key, ASCII_MAP[key])
    return _drop_remaining(out)


def _drop_remaining(text: str) -> str:
    """
    [EN]
    Remove emoji not covered by ASCII_MAP by replacing them with '?', so that no
    replacement glyph is ever left behind.

    [ZH]
    清理 ASCII_MAP 未覆盖的 emoji：替换为 '?'，确保不留下任何方块。
    """
    result = []
    for ch in text:
        code = ord(ch)
        if code in (0xFE0F, 0xFE0E, 0x200D):      # [EN] Variation selectors and ZWJ are dropped. / [ZH] 变体选择符与零宽连接符：直接丢弃。
            continue
        if (0x1F000 <= code <= 0x1FAFF) or (0x2600 <= code <= 0x27BF) or (0x2B00 <= code <= 0x2BFF):
            result.append("?")
            continue
        result.append(ch)
    return "".join(result)


def prefer(text: str) -> str:
    """
    [EN]
    For terminal output: downgrade to ASCII when emoji are not permitted, otherwise
    return the text unchanged.

    [ZH]
    终端输出用途：不允许 emoji 时降级为 ASCII，否则原样返回。
    """
    return text if enabled() else strip(text)


def configure_streams() -> None:
    """
    [EN]
    Make standard output use UTF-8 where possible (task specification fix 6: keep
    encoding consistent with display).

    After PyInstaller packaging, stdout may be cp936; ``reconfigure`` is the only
    side-effect-free remedy. Failures are not raised: an output-encoding problem
    must not prevent the backend from starting.

    [ZH]
    尽量让标准输出使用 UTF-8（任务书修复六：编码与显示保持一致）。

    PyInstaller 打包后 stdout 可能是 cp936；``reconfigure`` 是唯一无副作用的修改
    方式。失败时不抛出异常——输出编码问题不应导致后端无法启动。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # [EN] The cached decision must be recomputed; the result may differ after
    # [EN] ``reconfigure``.
    # [ZH] 缓存中的判断需要重新计算（``reconfigure`` 之后结果可能不同）。
    global _enabled
    _enabled = None


def info() -> Dict[str, Any]:
    """
    [EN]
    Human-readable status for the ``ready`` line.

    [ZH]
    供 ``ready`` 行使用的可读状态。
    """
    return {
        "emoji": enabled(),
        "stdout_encoding": (getattr(sys.stdout, "encoding", "") or ""),
    }
