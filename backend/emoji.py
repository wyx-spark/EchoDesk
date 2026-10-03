"""Emoji 降级（任务书修复六）。

背景：Windows 自带字体的 emoji 覆盖率差异极大——实测 ``Segoe UI`` 对项目在用的
20 个 emoji 覆盖率为 **0/20**，只有 ``Segoe UI Emoji`` 是 20/20。因此：

* **界面**（WPF）由字体回退链解决（``Resources/Styles.xaml`` 的 ``UiFontFamily``）；
* **控制台**（本模块）无法依赖字体，只能降级：检测当前输出流是否能安全写出 emoji，
  不能就替换成 ASCII 标记（``[OK]`` / ``[!]``）。
* **语言包文本**同样可能带 emoji，交给前端的 ``Localization`` 在渲染时降级，
  后端只负责把"当前是否支持 emoji"这件事判断清楚并通过 ``ready`` 告知前端。

判断依据（任一不满足即降级）：
  * ``ECHODESK_NO_EMOJI=1`` / ``--no-emoji``：用户显式要求；
  * 输出流编码不是 UTF-8（如被重定向到 ``cp936`` 的管道）；
  * stdout/stderr 是控制台但代码页不是 UTF-8（``chcp`` 未设为 65001）。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

# 常用 emoji → ASCII 标记。未列出的 emoji 统一用 "?" 顶上，
# 保证"绝不出现方块"——宁可信息弱一点，也不要用户看到乱码。
ASCII_MAP: Dict[str, str] = {
    "✅": "[OK]", "❌": "[X]", "⚠️": "[!]", "⚠": "[!]",
    "⏳": "[..]", "⏱️": "[time]", "⏱": "[time]",
    # ⚡：运行指标里的"首 token 延迟"；⏹：中止标记（优化已知问题-2 任务七）
    "⚡": "[fast]", "⏹": "[stop]",
    "✨": "*", "🌙": "[dark]", "☀️": "[light]", "☀": "[light]",
    "📡": "[net]", "🧠": "[model]", "🛠️": "[mode]", "🛠": "[mode]",
    "📂": "[folder]", "📁": "[folder]", "📄": "[file]", "📋": "[copy]",
    "🔍": "[search]", "🔎": "[search]", "📊": "[chart]", "📈": "[chart]",
    "💾": "[save]", "📥": "[in]", "📤": "[out]", "🚀": "[go]",
    "⚙️": "[cfg]", "⚙": "[cfg]", "🔧": "[cfg]", "🔧️": "[cfg]",
    "📝": "[edit]", "👤": "[user]", "🤖": "[bot]", "ℹ️": "[i]", "ℹ": "[i]",
    # 🔁：分段摘要"超时重试"那一行用的（v0.2.1 综合优化 任务四）。
    # 新增 emoji 必须同步补这张表与 frontend\Services\Emoji.cs，否则在无 emoji 字体的
    # 终端上会退化成 "?"（tools\check_emoji.ps1 就是专门拦这件事的门禁）。
    "🔁": "[retry]",
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
    # 模型市场优化（v0.2.1）新增：筛选面板标题与"离线模式"提示。
    # 🎛️ 带变体选择符（U+1F39B U+FE0F），两个写法都要列，
    # 否则带 VS16 的那份在无 emoji 字体下仍会退化成 "?"（check_emoji.ps1 会拦）。
    "🎛️": "[filter]", "🎛": "[filter]",
    "📴": "[offline]",
    # 模型档位（v0.2.1 默认模型切换一轮）：设置页「🎚️ 模型档位」标题。
    # 同样带变体选择符（U+1F39A U+FE0F），两种写法都要列。
    "🎚️": "[tiers]", "🎚": "[tiers]",
}

_enabled: Optional[bool] = None


def _stream_supports_emoji(stream: Any) -> bool:
    """判断一个输出流能否安全写出 emoji（不抛异常、不显示方块）。"""
    if stream is None:
        return False
    encoding = (getattr(stream, "encoding", "") or "").lower()
    if not encoding:
        # 无编码信息（例如被替换过的 StringIO）：按支持处理，避免误降级
        return True
    if "utf" not in encoding:
        return False
    # 是控制台时还要看代码页：cp936/cp437 的控制台会把 emoji 变成 "??"
    try:
        if stream.isatty():
            return _console_code_page_is_utf8()
    except Exception:
        pass
    return True


def _console_code_page_is_utf8() -> bool:
    """Windows 控制台代码页是否为 UTF-8（65001）。非 Windows 一律按支持处理。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP()) == 65001
    except Exception:
        return False


def enabled() -> bool:
    """当前是否允许输出 emoji（结果缓存，重复调用无额外开销）。"""
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
    """显式设置（供启动参数与测试使用）。"""
    global _enabled
    _enabled = bool(value)


def strip(text: str) -> str:
    """把 emoji 替换为 ASCII 标记（无论当前是否允许，用于必须纯文本的场合，如日志文件）。"""
    if not text:
        return text
    out = text
    # 先替换多字符（带变体选择符 U+FE0F 的组合），再替换单字符
    for key in sorted(ASCII_MAP, key=len, reverse=True):
        if key in out:
            out = out.replace(key, ASCII_MAP[key])
    return _drop_remaining(out)


def _drop_remaining(text: str) -> str:
    """清理 ASCII_MAP 未覆盖的 emoji：替换为 '?'，绝不留下方块。"""
    result = []
    for ch in text:
        code = ord(ch)
        if code in (0xFE0F, 0xFE0E, 0x200D):      # 变体选择符与零宽连接符：直接丢弃
            continue
        if (0x1F000 <= code <= 0x1FAFF) or (0x2600 <= code <= 0x27BF) or (0x2B00 <= code <= 0x2BFF):
            result.append("?")
            continue
        result.append(ch)
    return "".join(result)


def prefer(text: str) -> str:
    """终端输出用：不允许 emoji 时降级为 ASCII，否则原样返回。"""
    return text if enabled() else strip(text)


def configure_streams() -> None:
    """尽量让标准输出走 UTF-8（任务书修复六：编码与显示一致）。

    PyInstaller 打包后 stdout 可能是 cp936；``reconfigure`` 是唯一无副作用的改法。
    失败不抛出——输出编码问题不应该让后端无法启动。
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
    # 缓存里的判断要重算（reconfigure 之后结果可能不同）
    global _enabled
    _enabled = None


def info() -> Dict[str, Any]:
    """给 ready 行用的可读状态。"""
    return {
        "emoji": enabled(),
        "stdout_encoding": (getattr(sys.stdout, "encoding", "") or ""),
    }
