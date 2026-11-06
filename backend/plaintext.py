"""
[EN]
Markdown-to-plain-text conversion.

Automation result files (``result.txt``) must contain plain text without any
Markdown markup. Notepad and built-in Windows tools cannot render Markdown, so
characters such as ``#``, ``**`` and ``|`` only interfere with reading. This
module removes markup while preserving a readable structure:

* ``# Heading`` becomes the heading text on its own line, underlined with
  ``=`` or ``-`` on the next line.
* ``**bold**``, ``*italic*``, ``~~strike~~`` and inline code retain only text.
* ``- item`` and ``1. item`` preserve the marker and numeric prefix for
  readability in plain text.
* Tables are rendered as column-aligned plain text without vertical bars.
* ``[text](url)`` becomes ``text (url)``; images become ``[Image: alt]``.
* Fenced code blocks lose their fences and are indented by four spaces.
* Block quotes are indented by two spaces.
* Remaining HTML tags are stripped and escape sequences are restored.

[ZH]
Markdown 到纯文本的转换。

自动化生成的 ``result.txt`` 必须是纯文本，不得包含 Markdown 标记。记事本与
Windows 内置工具不具备 Markdown 渲染能力，``#``、``**``、``|`` 等符号只会
干扰阅读。本模块在保留可读结构的前提下移除标记：

* ``# 标题`` 转为独立一行的标题文字，下一行使用 ``=`` 或 ``-`` 下划线。
* ``**粗体**``、``*斜体*``、``~~删除~~`` 与行内代码只保留文字。
* ``- 项`` 与 ``1. 项`` 保留前缀标记与数字编号，便于纯文本阅读。
* 表格转为按列对齐的纯文本，并移除竖线。
* ``[文字](链接)`` 转为 ``文字（链接）``；图片转为 ``[图片：alt]``。
* 围栏代码块移除围栏，并将内容整体缩进四格。
* 引用块缩进两格。
* 其余 HTML 标签被剥离，转义字符被还原。
"""
from __future__ import annotations

import re
from typing import List

__all__ = ["markdown_to_plain"]

_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([^\s`]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_CODE_SPAN_RE = re.compile(r"`([^`]*)`")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")

# [EN] Inline emphasis markers are removed only when paired, so that a single
# [EN] ``*`` or ``_`` in a mathematical expression is preserved.
# [ZH] 行内强调标记仅在成对出现时移除，避免误删数学表达式中的单个 ``*`` 或 ``_``。
_EMPHASIS_PATTERNS = (
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"___(.+?)___", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S), r"\1"),
    (re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", re.S), r"\1"),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
)


def _strip_inline(text: str) -> str:
    """
    [EN]
    Remove all inline Markdown markers from a single line.

    [ZH]
    移除单行中的全部 Markdown 行内标记。
    """
    line = _ESCAPE_RE.sub(r"\1", text)
    line = _IMAGE_RE.sub(lambda m: f"[图片：{m.group(1)}]" if m.group(1).strip() else "[图片]", line)
    line = _LINK_RE.sub(
        lambda m: f"{m.group(1)}（{m.group(2)}）" if m.group(1).strip() else m.group(2), line
    )
    line = _AUTOLINK_RE.sub(r"\1", line)
    line = _CODE_SPAN_RE.sub(r"\1", line)
    for pattern, repl in _EMPHASIS_PATTERNS:
        line = pattern.sub(repl, line)
    line = _HTML_TAG_RE.sub("", line)
    # [EN] Line-break tags such as <br> are removed with other tags; collapse the
    # [EN] whitespace they leave behind.
    # [ZH] 换行标签（如 <br>）已随其他标签一并删除；此处压缩其留下的多余空白。
    return line.replace("\u00a0", " ").rstrip()


def _split_row(line: str) -> List[str]:
    """
    [EN]
    Split a Markdown table row into cell texts.

    [ZH]
    将 Markdown 表格行拆分为单元格文本。
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    cells: List[str] = []
    current: List[str] = []
    escaped = False
    for ch in body:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(ch)
    cells.append("".join(current))
    return [_strip_inline(cell.strip()) for cell in cells]


def _display_width(text: str) -> int:
    """
    [EN]
    Return the display width of text in a monospaced font. CJK full-width
    characters count as two columns.

    [ZH]
    返回文本在等宽字体下的显示宽度。中日韩全角字符按两列计算。
    """
    import unicodedata

    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    """
    [EN]
    Pad text on the right according to its display width so that tables remain
    aligned in a plain-text editor.

    [ZH]
    按显示宽度在右侧补空格，使表格在纯文本编辑器中保持对齐。
    """
    return text + " " * max(0, width - _display_width(text))


def _render_table(rows: List[List[str]]) -> List[str]:
    """
    [EN]
    Render a table as column-aligned plain text without vertical bars.

    [ZH]
    将表格渲染为按列宽对齐的纯文本，不包含竖线。
    """
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    widths = [max(_display_width(row[col]) for row in normalized) for col in range(width)]

    out: List[str] = []
    head = normalized[0]
    out.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(head)).rstrip())
    out.append("  ".join("-" * widths[i] for i in range(width)))
    for row in normalized[1:]:
        out.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
    return out


def markdown_to_plain(text: str) -> str:
    """
    [EN]
    Convert Markdown text to plain text while preserving paragraph and list
    structure and removing all markup symbols.

    [ZH]
    将 Markdown 文本转换为纯文本，保留段落与列表结构并移除全部标记符号。
    """
    if not text:
        return ""
    source = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    out: List[str] = []
    in_fence = False
    fence_indent = "    "
    index = 0

    while index < len(lines):
        line = lines[index]

        fence = _FENCE_RE.match(line)
        if fence:
            in_fence = not in_fence
            if in_fence and not out:
                out.append("")
            index += 1
            continue
        if in_fence:
            out.append((fence_indent + line) if line.strip() else "")
            index += 1
            continue

        # [EN] Table: the current line contains | and the next line is a separator;
        # [EN] process the whole block.
        # [ZH] 表格：当前行包含 | 且下一行为分隔行；整块处理。
        if "|" in line and index + 1 < len(lines) and _TABLE_SEP_RE.match(lines[index + 1]):
            rows: List[List[str]] = [_split_row(line)]
            cursor = index + 2
            while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
                rows.append(_split_row(lines[cursor]))
                cursor += 1
            out.append("")
            out.extend(_render_table(rows))
            out.append("")
            index = cursor
            continue

        if _HR_RE.match(line):
            out.append("-" * 40)
            index += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = _strip_inline(heading.group(2))
            if out and out[-1].strip():
                out.append("")
            out.append(title)
            out.append(("=" if level <= 2 else "-") * max(3, min(len(title), 40)))
            out.append("")
            index += 1
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            inner = _strip_inline(quote.group(1))
            out.append(("  " + inner).rstrip())
            index += 1
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            indent = " " * (len(bullet.group(1)) // 2 * 2)
            out.append((indent + "- " + _strip_inline(bullet.group(2))).rstrip())
            index += 1
            continue

        ordered = _ORDERED_RE.match(line)
        if ordered:
            indent = " " * (len(ordered.group(1)) // 2 * 2)
            out.append(
                (f"{indent}{ordered.group(2)}. " + _strip_inline(ordered.group(3))).rstrip()
            )
            index += 1
            continue

        stripped = _strip_inline(line)
        out.append(stripped.rstrip())
        index += 1

    # [EN] Final cleanup: trim trailing spaces and collapse runs of more than two blank lines.
    # [ZH] 收尾整理：去除行尾空格，并压缩超过两行的连续空行。
    cleaned: List[str] = []
    blank = 0
    for item in out:
        if item.strip():
            blank = 0
            cleaned.append(item.rstrip())
        else:
            blank += 1
            if blank <= 1:
                cleaned.append("")
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned) + "\n"


def contains_markdown(text: str) -> bool:
    """
    [EN]
    Coarsely detect whether text still contains common Markdown markers. The
    check is used by self-tests and regression coverage.

    [ZH]
    粗略检测文本是否仍包含常见 Markdown 标记。该检查用于自检与回归测试。
    """
    if not text:
        return False
    for line in str(text).splitlines():
        if _HEADING_RE.match(line) or _FENCE_RE.match(line):
            return True
        if line.count("|") >= 2 and "|" in line:
            return True
    if re.search(r"\*\*[^*]+\*\*", text) or re.search(r"(?m)^\s*[-*+]\s+\*\*", text):
        return True
    return False
