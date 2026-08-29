"""文件解析：优先 WPS COM 读取（含加密文档），失败回退 pyxtxt 通用解析。"""
import os
from typing import Dict

import win32com.client as win32
from pyxtxt import xtxt

# Office 扩展名 → WPS COM ProgID
_WPS_PROGIDS: Dict[str, str] = {
    ".doc": "Kwps.Application",
    ".docx": "Kwps.Application",
    ".xls": "KExcel.Application",
    ".xlsx": "KExcel.Application",
    ".ppt": "KPPT.Application",
    ".pptx": "KPPT.Application",
}


def parse_file_with_wps(filepath: str, max_chars: int = 10000) -> str:
    """读取文件文本内容。

    - Office 文档：优先 WPS COM（装有 WPS 时可打开加密文档），失败自动回退 pyxtxt；
    - 其他格式：直接使用 pyxtxt。
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _WPS_PROGIDS:
        wps_result = _read_office_with_wps(filepath, max_chars)
        if not wps_result.startswith("WPS读取失败"):
            return wps_result
        fallback = _parse_other(filepath, max_chars)
        if not fallback.startswith("解析失败"):
            return fallback
        return f"WPS读取失败，通用解析也失败。\nWPS: {wps_result}\n解析: {fallback}"
    return _parse_other(filepath, max_chars)


def _truncate(text: str, max_chars: int) -> str:
    return text[:max_chars] + "\n...[内容过长，已截断]" if len(text) > max_chars else text


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


def _parse_other(filepath: str, max_chars: int) -> str:
    try:
        text = xtxt(filepath)
        return _truncate(text, max_chars) if text.strip() else "文件内容为空"
    except Exception as e:
        return f"解析失败: {e}"
