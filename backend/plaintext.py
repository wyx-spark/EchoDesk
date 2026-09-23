"""Markdown → 纯文本转换（任务书 新增三）。

自动化生成的 ``结果.txt`` 要求是**纯文本，不含任何 Markdown 标记**：
记事本与 Windows 原装工具没有 Markdown 渲染能力，``#``、``**``、``|`` 这些符号
只会干扰阅读。本模块在保留可读结构的前提下剥离标记：

* ``# 标题`` → 标题文字独占一行，下一行用 ``=`` / ``-`` 下划线；
* ``**粗体**`` / ``*斜体*`` / ``~~删除~~`` / ``` `代码` ``` → 只保留文字；
* ``- 项`` / ``1. 项`` → 保留 ``-`` 与数字编号（纯文本下仍然易读）；
* ``| a | b |`` 表格 → 按列宽对齐的纯文本表格（去掉竖线）；
* ``[文字](链接)`` → ``文字（链接）``；``![alt](src)`` → ``[图片：alt]``；
* ````` 代码块 ````` → 去掉围栏，内容整体缩进四格；
* ``> 引用`` → 缩进两格的纯文本；
* 其余 HTML 标签剥离、转义字符还原。
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

# 行内强调标记：成对出现才剥离，避免误伤数学式里的单个 * 或 _
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
    """剥离一行内的所有 Markdown 行内标记。"""
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
    # <br> 之类换行标签转成空格（已随标签一起删掉，这里压掉多余空白）
    return line.replace("\u00a0", " ").rstrip()


def _split_row(line: str) -> List[str]:
    """把 Markdown 表格行拆成单元格文本。"""
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
    """文本在等宽字体下的显示宽度（中日韩全角字符按 2 列计）。"""
    import unicodedata

    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格（中文表格在记事本里才能对齐）。"""
    return text + " " * max(0, width - _display_width(text))


def _render_table(rows: List[List[str]]) -> List[str]:
    """把表格渲染成按列宽对齐的纯文本（无竖线）。"""
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
    """把 Markdown 文本转成纯文本（保留段落与列表结构，去掉所有标记符号）。"""
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

        # 表格：当前行含 | 且下一行是分隔行 → 整块处理
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

    # 收尾整理：去掉行尾空格、压掉 3 行以上的空行
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
    """粗检文本是否仍含常见 Markdown 标记（供自检/回归使用）。"""
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
