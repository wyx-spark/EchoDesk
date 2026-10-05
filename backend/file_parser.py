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
  .pdf   pypdf（BSD-3-Clause）   .txt/.md/.csv 等 直接读取（自动识别编码）

v0.2.1 变更（任务书 1.3 合规与体积）：
  PDF 解析从 PyMuPDF 换成 pypdf。PyMuPDF 是 **AGPL-3.0**（或 Artifex 商业许可），
  对外开源分发会产生著佐权义务，与 Apache-2.0 项目许可不兼容；pypdf 是 BSD-3-Clause、
  纯 Python、无原生扩展。已安装体积从 52.9MB 降到约 1.4MB（见 records/v0.2.1-研发记录.md）。
  代价：pypdf 不解析扫描件（本来也需要 OCR，不在支持范围内），复杂排版的
  阅读顺序还原能力弱于 MuPDF；对"取出正文文字交给模型"这一用途足够。

新增（v0.2.1 任务二 2.3）：技能可声明额外的文件扩展名解析器。未识别的扩展名会先询问
  已加载技能，再回落到"不支持的文件类型"。
"""
import os
from typing import Callable, Dict, Optional

# pywin32 只在 Windows 上存在。**必须容错导入**：顶层 `import win32com.client` 在
# Linux/macOS 上会直接 ImportError，把整个后端打死在启动阶段（跨平台化最先踩到的坑）。
# 导入失败时 win32=None，读 Office 文档的路径降级为"内置解析器 + 明确说明"。
try:
    import win32com.client as win32
    _WIN32_IMPORT_ERROR = ""
except Exception as _e:  # noqa: BLE001 - 缺依赖属正常降级，不是错误
    win32 = None
    _WIN32_IMPORT_ERROR = str(_e)

# 平台判断收口在 platform_support；这里不在模块顶层 import 它是因为
# file_parser 会被 main.py 在"抑制 stdout"的上下文里导入，延迟到函数内更安全。
def _platform_is_windows() -> bool:
    try:
        import platform_support

        return platform_support.is_windows()
    except Exception:  # noqa: BLE001
        return os.name == "nt"


def _wps_available() -> bool:
    """WPS COM 是否可用。优先问 platform_support（它认得 ECHODESK_FORCE_PLATFORM，
    测试才能在没有 Linux/macOS 机器的前提下验证降级路径）；模块不可用时退回本模块判断。"""
    try:
        import platform_support

        return platform_support.wps_com_available()
    except Exception:  # noqa: BLE001
        return win32 is not None


def _wps_unavailable_reason() -> str:
    """不可用的原因：分清"平台没有这个能力"与"本机缺 pywin32"——两者的处理办法完全不同。"""
    if not _platform_is_windows():
        return "当前平台不支持 WPS COM 接口（该能力仅 Windows 提供）"
    return f"本机缺少 pywin32（{_WIN32_IMPORT_ERROR}），无法使用 WPS COM 读取"

# 技能注册的扩展名解析器：{".ofd": (技能id, 处理函数(filepath) -> str)}
_SKILL_PARSERS: Dict[str, "tuple[str, Callable[[str], str]]"] = {}


def register_skill_parser(ext: str, handler: Callable[[str], str], skill_id: str = "") -> bool:
    """注册技能提供的扩展名解析器（任务二 2.3 文件解析扩展）。

    ``ext`` 可带或不带前导点；内置解析器已覆盖的扩展名不会被技能覆盖，
    以保证主程序行为可预测（返回 False 表示未注册）。
    """
    key = str(ext or "").strip().lower()
    if not key:
        return False
    if not key.startswith("."):
        key = "." + key
    if key in _WPS_PROGIDS or key in _TEXT_EXTS or key in _BUILTIN_EXTS:
        return False
    _SKILL_PARSERS[key] = (skill_id, handler)
    return True


def unregister_skill_parser(ext: str) -> bool:
    """注销技能解析器（卸载技能时调用）。"""
    key = str(ext or "").strip().lower()
    if key and not key.startswith("."):
        key = "." + key
    return _SKILL_PARSERS.pop(key, None) is not None


def unregister_skill_parsers(skill_id: str) -> int:
    """注销某个技能注册的全部解析器，返回移除数量。"""
    doomed = [k for k, (sid, _) in _SKILL_PARSERS.items() if sid == skill_id]
    for key in doomed:
        _SKILL_PARSERS.pop(key, None)
    return len(doomed)


def skill_parser_extensions() -> Dict[str, str]:
    """返回 {扩展名: 技能id}，供设置界面展示"已接管的格式"。"""
    return {ext: sid for ext, (sid, _handler) in _SKILL_PARSERS.items()}

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

# 内置解析器直接支持的扩展名（技能不得覆盖这些，保证主程序行为可预测）
_BUILTIN_EXTS = {".docx", ".xlsx", ".xlsm", ".xls", ".pptx", ".pdf"}

# ---------------------------------------------------------------------------
# 解析失败标记（多语言说明，v0.2.1）
# ---------------------------------------------------------------------------
# 解析器用这几个中文前缀作为**机器可读的失败标记**（与界面语言无关，因此不会
# 因为切换语言而失效）；展示给用户前请调用 localize_error() 转成当前语言。
_ERROR_MARKERS = ("WPS读取失败", "解析失败", "文件内容为空", "不支持的文件类型")


def is_parse_error(text: str) -> bool:
    """判断解析器返回的是不是失败提示（机器标记，见上）。"""
    return bool(text) and text.startswith(_ERROR_MARKERS)


def localize_error(text: str) -> str:
    """把解析失败标记翻译成当前界面语言；无法识别时原样返回。"""
    from i18n import t as _t

    raw = str(text or "")
    if raw.startswith("WPS读取失败: "):
        return _t("file.wps_failed", error=raw[len("WPS读取失败: "):])
    if raw.startswith("WPS读取失败，且 "):
        # 旧版二进制格式、缺少 WPS 的提示：整段保留细节，只翻译前缀
        return _t("file.parse_failed", error=raw)
    if raw.startswith("解析失败: "):
        return _t("file.parse_failed", error=raw[len("解析失败: "):])
    if raw.startswith("不支持的文件类型: "):
        return _t("file.unsupported", ext=raw[len("不支持的文件类型: "):])
    if raw.startswith("文件内容为空"):
        return _t("file.empty")
    return raw

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
            if _platform_is_windows():
                return (
                    f"WPS读取失败，且 {ext} 为旧版二进制格式，内置解析器不支持。\n"
                    f"请安装 WPS/Office 后重试，或先另存为 {ext}x。\nWPS: {wps_result}"
                )
            # 非 Windows：旧版二进制格式（.doc/.xls）只有 WPS COM 能读 —— 这是
            # 任务五里"必须给替代路径"的典型场景，所以文案要给三条可执行的做法。
            return (
                f"{ext} 是旧版二进制格式，需要 Windows 上的 WPS COM 接口才能读取；"
                f"当前平台不提供该能力。\n"
                f"替代做法：① 在 Windows 机器上另存为 {ext}x 后来读；"
                f"② 用 MCP/插件接入外部转换服务；"
                f"③ 安装提供该扩展名的技能（技能可注册自己的解析器）。\n"
                f"WPS: {wps_result}\n解析: {fallback}"
            )
        return f"WPS读取失败，内置解析也失败。\nWPS: {wps_result}\n解析: {fallback}"
    return _parse_builtin(filepath, ext)


def _truncate(text: str, max_chars: int) -> str:
    """按字符数截断；max_chars <= 0 表示不限制（长文档交给分段摘要处理）。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[内容过长，已截断]"


def _read_office_with_wps(filepath: str, max_chars: int) -> str:
    """通过 WPS COM 读取文档文本（WPS 未安装/平台不支持/打开失败时返回错误字符串）。

    返回串始终以 ``WPS读取失败`` 开头 —— 调用方靠这个前缀决定是否回退内置解析器，
    所以降级路径**不改协议**，只把"为什么没有"写清楚。
    """
    if not _wps_available():
        # 不是"出错"，而是任务五定义的能力边界：文案必须写清原因，让调用方给出替代路径
        return f"WPS读取失败: {_wps_unavailable_reason()}。"
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
        elif ext in _SKILL_PARSERS:
            # 任务二 2.3：未知扩展名先询问已加载技能提供的解析器
            skill_id, handler = _SKILL_PARSERS[ext]
            try:
                text = handler(filepath)
            except Exception as e:
                return f"解析失败: 技能「{skill_id}」的 {ext} 解析器出错（{type(e).__name__}: {e}）"
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
    """PDF：用 pypdf 提取文本层（BSD-3-Clause，替代 AGPL 的 PyMuPDF）。

    扫描件没有文本层，提取结果为空 —— 这种情况需要 OCR，不在支持范围内，
    调用方会得到"文件内容为空"，由上层提示用户改用 WPS/OCR 另存后重试。
    """
    from pypdf import PdfReader

    reader = PdfReader(filepath)
    if getattr(reader, "is_encrypted", False):
        # 空口令加密（只设了权限密码）可以先解密；真正的用户口令无法处理
        try:
            reader.decrypt("")
        except Exception as e:
            return f"解析失败: PDF 已加密，无法读取（{e}）"
    parts = []
    for index, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:  # 单页损坏不应让整份文档失败
            text = f"[第 {index} 页提取失败: {e}]"
        if text:
            parts.append(f"【第 {index} 页】\n{text}")
    return "\n\n".join(parts)
