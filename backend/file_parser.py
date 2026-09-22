"""文件解析：优先 WPS COM 读取（含加密文档），失败回退内置轻量解析器。

为什么不再依赖 pyxtxt：
  * pyxtxt 的 estrattori 子包在 ``__init__`` 里用 ``os.listdir`` 自动导入同目录下
    所有解析器模块，因此打包容不下它的完整目录就会启动即报
    ``FileNotFoundError``；而完整打包会把 easyocr/torch/torchvision/scipy/cv2/
    skimage/numba 一并拉进来，后端体积从约 140MB 膨胀到近 1GB；
  * 其依赖链还包含 AGPL（PyMuPDF 之外）与 GPL/LGPL 组件（extract-msg/pcodedmp/
    RTFDE/python-bidi/pygame/pynput），对外分发会产生著佐权义务；
  * EchoDesk 只需 Office / TXT / PDF 这几类，用下面这些宽松许可（MIT/BSD）的
    库直接解析即可，行为更可预测。

支持范围：
  .docx  python-docx（MIT）      .doc  仅 WPS COM（含加密文档）
  .xlsx  openpyxl（MIT）         .xls  xlrd（BSD）+ WPS COM
  .pptx  python-pptx（MIT）      .ppt  仅 WPS COM
  .pdf   pymupdf（AGPL/商业）    .txt/.md/.csv 等 直接读取（自动识别编码）
"""
import os
from typing import Dict

import win32com.client as win32

# Office 扩展名 → WPS COM ProgID
_WPS_PROGIDS: Dict[str, str] = {
    ".doc": "Kwps.Application",
    ".docx": "Kwps.Application",
    ".xls": "KExcel.Application",
    ".xlsx": "KExcel.Application",
    ".ppt": "KPPT.Application",
    ".pptx": "KPPT.Application",
}

# 纯文本类扩展名（直接按文本读取，自动尝试多种编码）
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".xml",
    ".html", ".htm", ".ini", ".cfg", ".yaml", ".yml", ".srt", ".vtt",
}

# 需要 WPS COM 才能读取的旧版二进制格式（没有 WPS 时给出明确提示）
_LEGACY_ONLY_WPS = {".doc", ".ppt"}

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5", "latin-1")


def parse_file_with_wps(filepath: str, max_chars: int = 0) -> str:
    """读取文件文本内容。

    - Office 文档：优先 WPS COM（装有 WPS 时可打开加密文档），失败回退内置解析；
    - 其他格式：直接使用内置解析器。
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _WPS_PROGIDS:
        wps_result = _read_office_with_wps(filepath, max_chars)
        if not wps_result.startswith("WPS读取失败"):
            return wps_result
        fallback = _parse_builtin(filepath, ext)
        if not fallback.startswith(("解析失败", "不支持的文件类型")):
            return fallback
        if ext in _LEGACY_ONLY_WPS:
            return (
                f"WPS读取失败，且 {ext} 为旧版二进制格式，内置解析器不支持。\n"
                f"请安装 WPS/Office 后重试，或先另存为 {ext}x。\nWPS: {wps_result}"
            )
        return f"WPS读取失败，内置解析也失败。\nWPS: {wps_result}\n解析: {fallback}"
    return _parse_builtin(filepath, ext)


def _truncate(text: str, max_chars: int) -> str:
    """按字符数截断；max_chars <= 0 表示不限制（长文档交给分段摘要处理）。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[内容过长，已截断]"


def _read_office_with_wps(filepath: str, max_chars: int) -> str:
    """通过 WPS COM 读取文档文本（WPS 未安装或打开失败时返回错误字符串）。"""
    app = doc = None
    try:
        progid = _WPS_PROGIDS[os.path.splitext(filepath)[1].lower()]
        app = win32.gencache.EnsureDispatch(progid)
        app.Visible = False
        doc = app.Documents.Open(filepath)
        if hasattr(doc, "Content"):
            content = doc.Content.Text
        elif hasattr(doc, "ActiveSheet"):
            content = doc.ActiveSheet.UsedRange.Text
        else:
            content = doc.Text
        return _truncate(content, max_chars)
    except Exception as e:
        return f"WPS读取失败: {e}"
    finally:
        if doc:
            try:
                doc.Close(SaveChanges=False)
            except Exception:
                pass
        if app:
            try:
                app.Quit()
            except Exception:
                pass


# ---------- 内置轻量解析器 ----------

def _parse_builtin(filepath: str, ext: str) -> str:
    """按扩展名分发到内置解析器；返回文本或形如「解析失败: ...」的错误串。"""
    try:
        if ext in _TEXT_EXTS:
            text = _read_text(filepath)
        elif ext == ".docx":
            text = _read_docx(filepath)
        elif ext == ".xlsx" or ext == ".xlsm":
            text = _read_xlsx(filepath)
        elif ext == ".xls":
            text = _read_xls(filepath)
        elif ext == ".pptx":
            text = _read_pptx(filepath)
        elif ext == ".pdf":
            text = _read_pdf(filepath)
        else:
            return f"不支持的文件类型: {ext or '(无扩展名)'}"
    except ImportError as e:
        return f"解析失败: 缺少解析依赖（{e}）"
    except Exception as e:
        return f"解析失败: {e}"

    if not text or not text.strip():
        return "文件内容为空"
    return text


def _read_text(filepath: str) -> str:
    """按常见编码依次尝试读取纯文本，避免中文文件乱码。"""
    with open(filepath, "rb") as f:
        raw = f.read()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_docx(filepath: str) -> str:
    """Word：正文段落 + 表格（表格按 行内单元格以 | 连接）。"""
    from docx import Document

    doc = Document(filepath)
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        parts.append("")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_xlsx(filepath: str) -> str:
    """Excel（xlsx/xlsm）：逐工作表输出，行内单元格以 | 连接。"""
    from openpyxl import load_workbook

    wb = load_workbook(filepath, data_only=True, read_only=True)
    parts = []
    try:
        for ws in wb.worksheets:
            parts.append(f"【工作表】{ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    parts.append(" | ".join(cells))
            parts.append("")
    finally:
        wb.close()
    return "\n".join(parts)


def _read_xls(filepath: str) -> str:
    """Excel（旧版 xls）：用 xlrd 读取。"""
    import xlrd

    book = xlrd.open_workbook(filepath)
    parts = []
    for sheet in book.sheets():
        parts.append(f"【工作表】{sheet.name}")
        for r in range(sheet.nrows):
            cells = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
            if any(cells):
                parts.append(" | ".join(cells))
        parts.append("")
    return "\n".join(parts)


def _read_pptx(filepath: str) -> str:
    """PowerPoint：逐页提取文本框与表格文字。"""
    from pptx import Presentation

    prs = Presentation(filepath)
    parts = []
    for index, slide in enumerate(prs.slides, 1):
        parts.append(f"【第 {index} 页】")
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
        parts.append("")
    return "\n".join(parts)


def _read_pdf(filepath: str) -> str:
    """PDF：用 PyMuPDF 提取文本层（扫描件无文本层时返回空）。"""
    try:
        import pymupdf as fitz  # PyMuPDF 1.24+ 推荐写法
    except ImportError:
        import fitz  # 兼容旧版本

    doc = fitz.open(filepath)
    try:
        parts = []
        for index, page in enumerate(doc, 1):
            text = page.get_text().strip()
            if text:
                parts.append(f"【第 {index} 页】\n{text}")
        return "\n\n".join(parts)
    finally:
        doc.close()
