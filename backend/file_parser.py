"""
[EN]
File parsing: prefer WPS COM reading (including encrypted documents) and fall back to
the built-in lightweight parsers on failure.

Why the previous dependency on pyxtxt was removed:
  * The ``estrattori`` subpackage of pyxtxt imports every parser module in its
    directory via ``os.listdir`` inside ``__init__``. A distribution that does not
    include its complete directory therefore raises ``FileNotFoundError`` at
    startup, while a complete package pulls in easyocr/torch/torchvision/scipy/cv2/
    skimage/numba and inflates the backend from about 140 MB to nearly 1 GB.
  * Its dependency chain also includes AGPL (in addition to PyMuPDF) and GPL/LGPL
    components (extract-msg/pcodedmp/RTFDE/python-bidi/pygame/pynput), which would
    impose copyleft obligations on external distribution.
  * EchoDesk only needs Office / TXT / PDF formats, which can be parsed directly with
    the permissively licensed (MIT/BSD) libraries listed below, with more predictable
    behavior.

Supported formats:
  .docx  python-docx (MIT)      .doc  WPS COM only (including encrypted documents)
  .xlsx  openpyxl (MIT)         .xls  xlrd (BSD) + WPS COM
  .pptx  python-pptx (MIT)      .ppt  WPS COM only
  .pdf   pypdf (BSD-3-Clause)   .txt/.md/.csv etc. read directly (encoding auto-detected)

v0.2.1 change (specification 1.3, compliance and size):
  PDF parsing moved from PyMuPDF to pypdf. PyMuPDF is AGPL-3.0 (or a commercial
  Artifex license), which imposes copyleft obligations on open-source distribution
  and is incompatible with the project's Apache-2.0 license. pypdf is BSD-3-Clause,
  pure Python, and has no native extension. Installed size decreased from 52.9 MB to
  about 1.4 MB (see records/v0.2.1-研发记录.md). Trade-off: pypdf does not parse
  scanned images (which require OCR and are outside the supported scope), and its
  ability to reconstruct reading order for complex layouts is weaker than MuPDF.
  This is sufficient for the use case of extracting body text to send to the model.

Added in v0.2.1 (task 2.3): skills may declare parsers for additional file
extensions. Unrecognized extensions are first offered to loaded skills and then fall
back to "unsupported file type".

[ZH]
文件解析：优先通过 WPS COM 读取（含加密文档），失败时回退到内置轻量解析器。

不再依赖 pyxtxt 的原因：
  * pyxtxt 的 estrattori 子包在 ``__init__`` 中使用 ``os.listdir`` 自动导入同目录下
    所有解析器模块，因此若打包未包含其完整目录，启动时即抛出
    ``FileNotFoundError``；而完整打包会把 easyocr/torch/torchvision/scipy/cv2/
    skimage/numba 一并引入，使后端体积从约 140MB 膨胀到近 1GB；
  * 其依赖链还包含 AGPL（PyMuPDF 之外）与 GPL/LGPL 组件（extract-msg/pcodedmp/
    RTFDE/python-bidi/pygame/pynput），对外分发会产生著佐权义务；
  * EchoDesk 只需处理 Office / TXT / PDF 这几类格式，使用下列宽松许可（MIT/BSD）
    的库直接解析即可，行为更可预测。

支持范围：
  .docx  python-docx（MIT）      .doc  仅 WPS COM（含加密文档）
  .xlsx  openpyxl（MIT）         .xls  xlrd（BSD）+ WPS COM
  .pptx  python-pptx（MIT）      .ppt  仅 WPS COM
  .pdf   pypdf（BSD-3-Clause）   .txt/.md/.csv 等 直接读取（自动识别编码）

v0.2.1 变更（设计规范 1.3 合规与体积）：
  PDF 解析从 PyMuPDF 更换为 pypdf。PyMuPDF 采用 AGPL-3.0（或 Artifex 商业许可），
  对外开源分发会产生著佐权义务，与 Apache-2.0 项目许可不兼容；pypdf 采用
  BSD-3-Clause、纯 Python 实现、无原生扩展。安装体积从 52.9MB 降至约 1.4MB（见
  records/v0.2.1-研发记录.md）。代价：pypdf 不解析扫描件（本就需要 OCR，不在支持
  范围内），复杂排版的阅读顺序还原能力弱于 MuPDF；对于"提取正文文字交给模型"这一
  用途已足够。

新增（v0.2.1 任务二 2.3）：技能可声明额外的文件扩展名解析器。未识别的扩展名会先
  询问已加载的技能，再回落到"不支持的文件类型"。
"""
import html
import os
import re
import zipfile
from typing import Callable, Dict, Optional

# [EN] pywin32 exists only on Windows. The import must be fault-tolerant: a
# [EN] top-level `import win32com.client` raises ImportError on Linux/macOS and
# [EN] terminates the entire backend during startup (the first regression
# [EN] encountered during cross-platform work). When the import fails, win32 is None
# [EN] and reading Office documents degrades to "built-in parser plus an explicit
# [EN] explanation".
# [ZH] pywin32 仅在 Windows 上存在。必须容错导入：顶层的 `import win32com.client`
# [ZH] 在 Linux/macOS 上会直接抛出 ImportError，导致整个后端在启动阶段终止
# [ZH] （跨平台化过程中最先遇到的回归）。导入失败时 win32 为 None，读取 Office
# [ZH] 文档的路径降级为"内置解析器加明确说明"。
try:
    import win32com.client as win32
    _WIN32_IMPORT_ERROR = ""
except Exception as _e:  # noqa: BLE001 - [EN] A missing dependency is expected degradation, not an error. [ZH] 缺少依赖属于正常降级，并非错误。
    win32 = None
    _WIN32_IMPORT_ERROR = str(_e)

# [EN] Platform detection is centralized in platform_support. It is not imported at
# [EN] module scope here because file_parser is imported by main.py inside a context
# [EN] that suppresses stdout, so deferring the import into the function is safer.
# [ZH] 平台判断集中在 platform_support 中；此处不在模块顶层导入它，是因为
# [ZH] file_parser 会被 main.py 在"抑制 stdout"的上下文中导入，延迟到函数内更安全。
def _platform_is_windows() -> bool:
    try:
        import platform_support

        return platform_support.is_windows()
    except Exception:  # noqa: BLE001
        return os.name == "nt"


def _wps_available() -> bool:
    """
    [EN]
    Report whether WPS COM is available. platform_support is consulted first (it
    honors ECHODESK_FORCE_PLATFORM, so the degraded path can be verified without a
    Linux/macOS machine); if the module is unavailable, this module decides.

    [ZH]
    判断 WPS COM 是否可用。优先询问 platform_support（它识别
    ECHODESK_FORCE_PLATFORM，使测试无需 Linux/macOS 机器即可验证降级路径）；
    模块不可用时回退到本模块自身的判断。
    """
    try:
        import platform_support

        return platform_support.wps_com_available()
    except Exception:  # noqa: BLE001
        return win32 is not None


def _wps_unavailable_reason() -> str:
    """
    [EN]
    Reason for unavailability: distinguish "the platform lacks this capability" from
    "pywin32 is missing on this machine", because the two require entirely different
    remedies.

    [ZH]
    不可用的原因：区分"平台不具备该能力"与"本机缺少 pywin32"，两者所需的处理方式
    完全不同。
    """
    from i18n import t as _t

    if not _platform_is_windows():
        return _t("file.wps_unavailable_reason")
    return _t("file.wps_missing_pywin32", error=_WIN32_IMPORT_ERROR)

# [EN] Extension parsers registered by skills: {".ofd": (skill id, handler(filepath) -> str)}
# [ZH] 技能注册的扩展名解析器：{".ofd": (技能 id, 处理函数(filepath) -> str)}
_SKILL_PARSERS: Dict[str, "tuple[str, Callable[[str], str]]"] = {}


def register_skill_parser(ext: str, handler: Callable[[str], str], skill_id: str = "") -> bool:
    """
    [EN]
    Register an extension parser provided by a skill (task 2.3, file-parsing
    extension).

    ``ext`` may include or omit the leading dot. Extensions already covered by a
    built-in parser are never overridden by a skill, so that main-program behavior
    stays predictable (a False return means nothing was registered).

    [ZH]
    注册技能提供的扩展名解析器（任务二 2.3 文件解析扩展）。

    ``ext`` 可带或不带前导点；内置解析器已覆盖的扩展名不会被技能覆盖，以保证主程序
    行为可预测（返回 False 表示未注册）。
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
    """
    [EN]
    Unregister a skill parser (called when a skill is unloaded).

    [ZH]
    注销技能解析器（卸载技能时调用）。
    """
    key = str(ext or "").strip().lower()
    if key and not key.startswith("."):
        key = "." + key
    return _SKILL_PARSERS.pop(key, None) is not None


def unregister_skill_parsers(skill_id: str) -> int:
    """
    [EN]
    Unregister every parser registered by a given skill and return the number
    removed.

    [ZH]
    注销某个技能注册的全部解析器，返回移除数量。
    """
    doomed = [k for k, (sid, _) in _SKILL_PARSERS.items() if sid == skill_id]
    for key in doomed:
        _SKILL_PARSERS.pop(key, None)
    return len(doomed)


def skill_parser_extensions() -> Dict[str, str]:
    """
    [EN]
    Return {extension: skill id}, used by the settings interface to display the
    formats taken over by skills.

    [ZH]
    返回 {扩展名: 技能 id}，供设置界面展示"已被接管的格式"。
    """
    return {ext: sid for ext, (sid, _handler) in _SKILL_PARSERS.items()}

# [EN] Office extension -> WPS COM ProgID
# [ZH] Office 扩展名 → WPS COM ProgID
_WPS_PROGIDS: Dict[str, str] = {
    ".doc": "Kwps.Application",
    ".docx": "Kwps.Application",
    ".xls": "KExcel.Application",
    ".xlsx": "KExcel.Application",
    ".ppt": "KPPT.Application",
    ".pptx": "KPPT.Application",
}

# [EN] Plain-text extensions (read as text, trying multiple encodings automatically).
# [EN] The list deliberately covers prose formats, source code, configuration files
# [EN] and subtitle transcripts, because users routinely upload README files, code
# [EN] snippets and exported logs. An extension missing from this list is still
# [EN] accepted when the payload is detected as text (see ``_looks_like_text``).
# [ZH] 纯文本类扩展名（直接按文本读取，自动尝试多种编码）。清单刻意覆盖文档、
# [ZH] 源码、配置与字幕等常见类型，因为用户经常上传 README、代码片段与导出的日志。
# [ZH] 未列入本清单的扩展名仍会在载荷被判定为文本时接受（见 ``_looks_like_text``）。
_TEXT_EXTS = {
    # [EN] Prose and structured documents / [ZH] 文档与结构化文本
    ".txt", ".text", ".md", ".markdown", ".mdown", ".mkd", ".rst", ".rest",
    ".adoc", ".asciidoc", ".org", ".tex", ".latex", ".rtf",
    ".csv", ".tsv", ".log", ".json", ".jsonl", ".ndjson", ".json5", ".xml",
    ".html", ".htm", ".xhtml", ".svg", ".css", ".scss", ".sass", ".less",
    ".ini", ".cfg", ".conf", ".properties", ".env", ".toml", ".yaml", ".yml",
    ".srt", ".vtt", ".ass", ".sub",
    # [EN] Source code / [ZH] 源码
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".svelte", ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".go", ".rs",
    ".rb", ".php", ".swift", ".m", ".mm", ".lua", ".pl", ".pm", ".r",
    ".jl", ".dart", ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs",
    ".fs", ".fsx", ".vb", ".asm", ".s", ".sql", ".sh", ".bash", ".zsh",
    ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".dockerfile", ".make",
    ".mk", ".cmake", ".diff", ".patch", ".graphql", ".gql", ".proto", ".thrift",
}

# [EN] Office Open XML variants that share the parser of their canonical format.
# [ZH] 与标准格式共用同一解析器的 Office Open XML 变体。
_OFFICE_ALIASES = {
    ".docm": ".docx", ".dotx": ".docx", ".dotm": ".docx",
    ".xltx": ".xlsx", ".xltm": ".xlsx", ".xlam": ".xlsx",
    ".potx": ".pptx", ".potm": ".pptx", ".ppsx": ".pptx", ".pptm": ".pptx",
}

# [EN] Zip containers whose visible text lives in XML or XHTML members.
# [ZH] 正文位于 XML 或 XHTML 成员中的压缩容器格式。
_ARCHIVE_TEXT_EXTS = {".odt", ".ods", ".odp", ".epub"}

# [EN] Legacy binary formats that require WPS COM (an explicit notice is produced
# [EN] when WPS is not installed)
# [ZH] 需要 WPS COM 才能读取的旧版二进制格式（未安装 WPS 时给出明确提示）
_LEGACY_ONLY_WPS = {".doc", ".ppt"}

# [EN] Extensions supported directly by the built-in parsers. Skills must not
# [EN] override them, so that the main program's behavior stays predictable.
# [ZH] 内置解析器直接支持的扩展名（技能不得覆盖这些，保证主程序行为可预测）。
_BUILTIN_EXTS = {".docx", ".xlsx", ".xlsm", ".xls", ".pptx", ".pdf"} | set(
    _OFFICE_ALIASES) | _ARCHIVE_TEXT_EXTS

# ---------------------------------------------------------------------------
# [EN] Parse-error markers (multilingual explanation; earlier release)
# [ZH] 解析失败标记（多语言说明，此前版本）
# ---------------------------------------------------------------------------
# [EN] v0.2.2 hard-coded-string audit, batch 6: the marker and the message are
# [EN] **separated**.
# [EN]   Previously the Chinese prefix served both as the "machine-readable marker"
# [EN]   and as text shown to the user, so the message itself could only be written
# [EN]   in Chinese; in an English UI, localize_error() could translate the prefix
# [EN]   only, and the detail ("skill \"x\" docx parser failed ...") stayed Chinese.
# [EN]   Now:
# [EN]     * The **marker** is the ASCII constant below; it is used only by
# [EN]       is_parse_error() and **never reaches the UI**.
# [EN]     * The **message** comes from the language pack (file.parse_failed /
# [EN]       file.skill_parser_failed / file.pdf_encrypted ...), so it is already in
# [EN]       the current language when produced; localize_error() only has to strip
# [EN]       the marker.
# [ZH] v0.2.2 硬编码清查第 6 批改造：标记与正文**分开**。
# [ZH]   原先中文前缀既是"机器可读标记"、又要显示给用户，于是正文只能写死中文 ——
# [ZH]   英文界面下 localize_error() 只能翻前缀，细节（"技能「x」的 docx 解析器出错…"）
# [ZH]   原样是中文。
# [ZH]   现在：
# [ZH]     * **标记**是下面的 ASCII 常量，只给 is_parse_error() 判定用，**不进界面**；
# [ZH]     * **正文**由语言包产出（file.parse_failed / file.skill_parser_failed /
# [ZH]       file.pdf_encrypted …），产生时就是当前语言，因此 localize_error() 只需
# [ZH]       把标记剥掉。
_ERROR_MARKS = ("[!WPS]", "[!PARSE]", "[!EMPTY]", "[!EXT]")
#: [EN] Legacy Chinese prefixes are still recognized, for compatibility with strings
#: [EN] produced by older code paths (for example error strings already persisted).
#: [ZH] 旧版中文前缀仍然识别（兼容历史上由旧代码路径产出的字符串，例如已落盘的旧错误串）
_LEGACY_MARKERS = ("WPS读取失败", "解析失败", "文件内容为空", "不支持的文件类型")


def _err(mark: str, key: str, **kwargs) -> str:
    """
    [EN]
    Create a structured parse error with a localized message and category.

    [ZH]
    构造一条解析失败串：**标记 + 当前语言正文**（正文已可直接显示）。
    """
    from i18n import t as _t

    return mark + _t(key, **kwargs)


def _is_wps_failure(text: str) -> bool:
    """
    [EN]
    Return whether a WPS conversion failure indicates the service is unavailable.

    [ZH]
    WPS 读取是不是"没成功"（新标记与旧中文前缀都认）。
    """
    return str(text or "").startswith(("[!WPS]", "WPS读取失败"))


def is_parse_error(text: str) -> bool:
    """
    [EN]
    Return whether an exception is a structured parse error.

    [ZH]
    判断解析器返回的是不是失败提示（机器标记，语言无关，见上）。
    """
    raw = str(text or "")
    return bool(raw) and raw.startswith(_ERROR_MARKS + _LEGACY_MARKERS)


def localize_error(text: str) -> str:
    """
    [EN]
    Return a user-facing localized message for a parse error.

    [ZH]
    把解析失败串转成"当前语言、可直接显示"的文本。

    正文在**产生时**已按当前语言渲染（见 ``_err``），这里剥掉机器标记即可；
    对旧式"中文前缀 + 中文正文"的串，仍按前缀映射到语言包键（兼容分支）。
    """
    from i18n import t as _t

    raw = str(text or "")
    for mark in _ERROR_MARKS:
        if raw.startswith(mark):
            return raw[len(mark):]
    if raw.startswith("WPS读取失败: "):
        return _t("file.wps_failed", error=raw[len("WPS读取失败: "):])
    if raw.startswith("WPS读取失败，且 "):
        # [EN] Legacy binary format with WPS missing: keep the whole message verbatim
        # [EN] and translate only the prefix.
        # [ZH] 旧版二进制格式、缺少 WPS 的提示：整段保留细节，只翻译前缀。
        return _t("file.parse_failed", error=raw)
    if raw.startswith("解析失败: "):
        return _t("file.parse_failed", error=raw[len("解析失败: "):])
    if raw.startswith("不支持的文件类型: "):
        return _t("file.unsupported", ext=raw[len("不支持的文件类型: "):])
    if raw.startswith("文件内容为空"):
        return _t("file.empty")
    return raw

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5", "latin-1")

# [EN] Binary container signatures. A byte sequence that starts with one of these must
# [EN] never be accepted as text by the unknown-extension fallback: the earlier
# [EN] implementation validated a payload by trying gb18030/big5/utf-16, and those
# [EN] encodings can decode almost any byte sequence, so a legacy OLE2 `.doc` (which the
# [EN] platform cannot open without WPS COM) was silently turned into mojibake instead of
# [EN] the structured "unsupported / platform boundary" message.
# [ZH] 二进制容器特征。以这些字节开头的载荷绝不能被"未知扩展名兜底"当成文本：旧实现用
# [ZH] gb18030/big5/utf-16 去试探解码，而这些编码几乎能解出任何字节序列，于是非
# [ZH] Windows 上无法用 WPS COM 打开的旧版 OLE2 `.doc` 会被静默变成乱码，而不是给出
# [ZH] 结构化的「不支持 / 平台能力边界」提示。
_BINARY_MAGIC = (
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # [EN] OLE2: legacy .doc/.xls/.ppt / [ZH] 旧版 Office 复合文档
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",  # [EN] ZIP / OOXML / ODF / EPUB / [ZH] 压缩包与 OOXML
    b"%PDF", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM",
    b"RIFF", b"OggS", b"fLaC", b"ID3", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07", b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe",
)


def _detect_text_encoding(raw: bytes) -> Optional[str]:
    """
    [EN]
    Return the encoding to use when a payload is genuinely text, else ``None``.

    Acceptance is deliberately narrow for **unrecognized extensions**: UTF-16 with a
    BOM, or a payload that decodes as strict UTF-8 (pure ASCII included). Legacy
    single/double-byte encodings (gb18030, big5, latin-1) are *not* accepted here
    because they cannot reject binary data; files with a known text extension still go
    through ``_read_text`` and keep the full encoding list.

    [ZH]
    载荷确为文本时返回应使用的编码，否则返回 ``None``。

    **未识别扩展名**的接受范围刻意收窄：带 BOM 的 UTF-16，或能按严格 UTF-8 解码的
    载荷（纯 ASCII 自然包含在内）。这里不接受 gb18030 / big5 / latin-1 这类旧编码，
    因为它们无法排除二进制数据；已知文本扩展名仍走 ``_read_text``，保留完整编码列表。
    """
    sample = raw[:16384]
    if not sample:
        return None
    if sample.startswith(_BINARY_MAGIC):
        return None
    if b"\x00" in sample:
        # [EN] NUL bytes are legitimate only in BOM-marked UTF-16 text.
        # [ZH] 只有带 BOM 的 UTF-16 文本才允许出现 NUL 字节。
        if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
            return "utf-16"
        return None
    control = sum(1 for byte in sample
                  if byte < 9 or (13 < byte < 32) or byte == 127)
    if control > max(4, len(sample) // 100):
        return None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return None


def _looks_like_text(raw: bytes) -> bool:
    """
    [EN]
    Decide whether a byte payload is plausibly human-readable text.

    A payload is rejected when it carries a known binary signature, contains a NUL byte
    without a UTF-16 BOM, has a high proportion of control characters, or does not
    decode as strict UTF-8. The check is intentionally conservative: it only widens
    acceptance to plainly textual content and never treats an archive or binary document
    as text.

    [ZH]
    判断字节载荷是否可合理地视为人类可读文本。

    带已知二进制特征、无 UTF-16 BOM 却含 NUL 字节、控制字符比例过高，或无法按严格
    UTF-8 解码的载荷会被拒绝。该判定刻意保守：只把明显属于文本的内容纳入接受范围，
    绝不把压缩包或二进制文档当作文本。
    """
    return _detect_text_encoding(raw) is not None


def _read_unknown_text(filepath: str) -> Optional[str]:
    """
    [EN]
    Read an unrecognized extension as text when the payload is textual; return
    ``None`` when it is binary.

    [ZH]
    对未识别的扩展名：载荷为文本时按文本读取，二进制时返回 ``None``。
    """
    with open(filepath, "rb") as handle:
        raw = handle.read()
    encoding = _detect_text_encoding(raw)
    if encoding is None:
        return None
    return raw.decode(encoding, errors="replace")


def parse_file_with_wps(filepath: str, max_chars: int = 0) -> str:
    """
    [EN]
    Parse a document through the WPS conversion service.

    [ZH]
    读取文件文本内容。

    - Office 文档：优先 WPS COM（装有 WPS 时可打开加密文档），失败回退内置解析；
    - 其他格式：直接使用内置解析器。
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _WPS_PROGIDS:
        wps_result = _read_office_with_wps(filepath, max_chars)
        if not _is_wps_failure(wps_result):
            return wps_result
        fallback = _parse_builtin(filepath, ext)
        if not is_parse_error(fallback):
            return fallback
        if ext in _LEGACY_ONLY_WPS:
            if _platform_is_windows():
                # [EN] The message comes from the language pack (the zh value already
                # [EN] reads "WPS read failed, and ..."), and _err prefixes the marker.
                # [ZH] 正文来自语言包（zh 值自带"WPS读取失败，且 …"的措辞），标记由 _err
                # [ZH] 加在前面。
                return _err("[!WPS]", "file.legacy_needs_wps", ext=ext) + f"\nWPS: {wps_result}"
            # [EN] Not Windows: the legacy binary formats (.doc/.xls) can only be read
            # [EN] through WPS COM. This is the typical "always provide an alternative"
            # [EN] case in the design specification, so the message offers three
            # [EN] actionable options.
            # [ZH] 非 Windows：旧版二进制格式（.doc/.xls）只有 WPS COM 能读 —— 这是
            # [ZH] 相关设计里"必须给替代路径"的典型场景，所以文案要给三条可执行的做法。
            return _err("[!WPS]", "file.legacy_needs_windows", ext=ext,
                        wps=wps_result, builtin=fallback)
        return _err("[!WPS]", "file.both_failed", wps=wps_result, builtin=fallback)
    return _parse_builtin(filepath, ext)


def _truncate(text: str, max_chars: int) -> str:
    """
    [EN]
    Truncate text to a maximum length without splitting surrogate pairs.

    [ZH]
    按字符数截断；max_chars <= 0 表示不限制（长文档交给分段摘要处理）。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[内容过长，已截断]"


def _read_office_with_wps(filepath: str, max_chars: int) -> str:
    """
    [EN]
    Read an Office document through the WPS conversion service.

    [ZH]
    通过 WPS COM 读取文档文本（WPS 未安装/平台不支持/打开失败时返回错误字符串）。

    返回串始终以 ``WPS读取失败`` 开头 —— 调用方靠这个前缀决定是否回退内置解析器，
    所以降级路径**不改协议**，只把"为什么没有"写清楚。
    """
    if not _wps_available():
        # [EN] This is not an "error" but a capability boundary defined by the design
        # [EN] specification: the message must state the reason so that the caller can
        # [EN] offer an alternative path.
        # [ZH] 不是"出错"，而是相关设计定义的能力边界：文案必须写清原因，让调用方给出
        # [ZH] 替代路径。
        return _err("[!WPS]", "file.wps_failed", error=_wps_unavailable_reason())
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
        return _err("[!WPS]", "file.wps_failed", error=e)
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


# [EN] ---------- Built-in lightweight parsers ----------
# [ZH] ---------- 内置轻量解析器 ----------

def _canonical_ext(ext: str) -> str:
    """
    [EN]
    Map an Office Open XML variant to the extension of its canonical parser.

    [ZH]
    把 Office Open XML 变体映射到其标准格式对应的解析器扩展名。
    """
    key = str(ext or "").lower()
    return _OFFICE_ALIASES.get(key, key)


def _parse_builtin(filepath: str, ext: str) -> str:
    """
    [EN]
    Parse a document using the built-in parser for its extension.

    Unknown extensions are accepted when the payload is plain text; otherwise a
    structured "unsupported file type" result is returned. No code path may
    propagate an exception to the caller.

    [ZH]
    按扩展名分发到内置解析器；返回文本或形如「解析失败: ...」的错误串。

    未知扩展名在载荷为纯文本时会被接受，否则返回结构化的「不支持的文件类型」结果。
    任何代码路径都不得把异常抛给调用方。
    """
    ext = _canonical_ext(ext)
    try:
        if ext in _TEXT_EXTS:
            if ext == ".rtf":
                text = _read_rtf(filepath)
            else:
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
        elif ext in _ARCHIVE_TEXT_EXTS:
            text = _read_archive_text(filepath, ext)
        elif ext in _SKILL_PARSERS:
            # [EN] Design specification 2.3: for an unknown extension, first ask the
            # [EN] parsers provided by loaded skills.
            # [ZH] 相关设计 2.3：未知扩展名先询问已加载技能提供的解析器
            skill_id, handler = _SKILL_PARSERS[ext]
            try:
                text = handler(filepath)
            except Exception as e:
                return _err("[!PARSE]", "file.skill_parser_failed", skill=skill_id, ext=ext,
                            kind=type(e).__name__, error=e)
        else:
            # [EN] Last resort for extension-less files and unrecognized suffixes: accept
            # [EN] the payload when it is genuinely text, otherwise report the structured
            # [EN] unsupported-type error instead of failing the request.
            # [ZH] 无扩展名与未识别后缀的最后兜底：载荷确为文本时接受，否则返回结构化的
            # [ZH] 「不支持的文件类型」错误，而不是让请求失败。
            text = _read_unknown_text(filepath)
            if text is None:
                from i18n import t as _t

                return _err("[!EXT]", "file.unsupported", ext=ext or _t("file.no_ext"))
    except FileNotFoundError as e:
        return _err("[!PARSE]", "file.not_found", path=filepath, error=e)
    except PermissionError as e:
        return _err("[!PARSE]", "file.permission_denied", path=filepath, error=e)
    except ImportError as e:
        return _err("[!PARSE]", "file.missing_dep", error=e)
    except Exception as e:
        return _err("[!PARSE]", "file.parse_failed", error=e)

    if not text or not text.strip():
        return _err("[!EMPTY]", "file.empty")
    return text


def _read_text(filepath: str) -> str:
    """
    [EN]
    Read a plain-text file with encoding detection.

    [ZH]
    按常见编码依次尝试读取纯文本，避免中文文件乱码。
    """
    with open(filepath, "rb") as f:
        raw = f.read()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_rtf(filepath: str) -> str:
    """
    [EN]
    Extract readable text from an RTF document without an external dependency.

    The implementation strips control words and destinations, decodes the
    ``\\'hh`` hexadecimal escapes and the ``\\uN`` Unicode escapes, and maps
    paragraph and line breaks to newlines. Formatting is intentionally discarded:
    only body text is needed for model context.

    [ZH]
    在不引入外部依赖的前提下从 RTF 文档提取可读文本。

    实现会剥离控制字与目标组，解码 ``\\'hh`` 十六进制转义与 ``\\uN`` Unicode
    转义，并把段落与换行映射为换行符。格式信息被刻意丢弃：模型上下文只需要正文。
    """
    with open(filepath, "rb") as handle:
        raw = handle.read()
    text = raw.decode("latin-1", errors="replace")

    # [EN] Discard destinations whose content is metadata rather than prose.
    # [ZH] 丢弃内容属于元数据而非正文的目标组。
    text = re.sub(
        r"\{\\\*?\\(?:fonttbl|colortbl|stylesheet|info|pict|header|footer|"
        r"footnote|field|object|themedata|datastore|latentstyles)[^{}]*\}",
        " ", text,
    )
    # [EN] Drop remaining braces and the RTF header/footer markers.
    # [ZH] 去掉剩余的组花括号与 RTF 首尾标记。
    text = text.replace("{", " ").replace("}", " ")

    def _decode_hex(match: "re.Match[str]") -> str:
        return bytes([int(match.group(1), 16)]).decode("cp1252", errors="replace")

    text = re.sub(r"\\'([0-9a-fA-F]{2})", _decode_hex, text)

    def _decode_unicode(match: "re.Match[str]") -> str:
        value = int(match.group(1))
        return chr(value) if value >= 0 else "\ufffd"

    text = re.sub(r"\\u(-?\d+)\?", _decode_unicode, text)
    text = text.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "\t")
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("\\", "")
    return html.unescape(text)


def _read_archive_text(filepath: str, ext: str) -> str:
    """
    [EN]
    Extract visible text from a zip container whose body lives in XML/XHTML
    members (ODT/ODS/ODP/EPUB).

    Members are read in archive order and tags are stripped with a conservative
    regular expression; this is sufficient for body-text extraction and avoids a
    third-party office suite dependency.

    [ZH]
    从正文位于 XML/XHTML 成员中的压缩容器（ODT/ODS/ODP/EPUB）提取可见文本。

    成员按压缩包顺序读取，标签以保守的正则剥离；这足以提取正文，且无需引入第三方
    办公套件依赖。
    """
    parts = []
    with zipfile.ZipFile(filepath) as archive:
        names = archive.namelist()
        if ext == ".epub":
            wanted = [n for n in names
                      if n.lower().endswith((".xhtml", ".html", ".htm", ".xml"))]
        else:
            # [EN] ODF stores the document body in content.xml; styles.xml is not prose.
            # [ZH] ODF 的正文位于 content.xml；styles.xml 不属于正文。
            wanted = [n for n in names if n.lower() == "content.xml"]
            if not wanted:
                wanted = [n for n in names
                          if n.lower().endswith((".xhtml", ".html", ".htm", ".xml"))]
        for name in wanted:
            try:
                data = archive.read(name)
            except (KeyError, OSError):
                continue
            chunk = data.decode("utf-8", errors="replace")
            chunk = re.sub(r"<script[^>]*>.*?</script>", " ", chunk,
                           flags=re.IGNORECASE | re.DOTALL)
            chunk = re.sub(r"<style[^>]*>.*?</style>", " ", chunk,
                           flags=re.IGNORECASE | re.DOTALL)
            chunk = re.sub(r"<(?:br|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", chunk,
                           flags=re.IGNORECASE)
            chunk = re.sub(r"<[^>]+>", " ", chunk)
            chunk = html.unescape(chunk)
            chunk = re.sub(r"[ \t]+", " ", chunk)
            chunk = re.sub(r"\n\s*\n+", "\n", chunk).strip()
            if chunk:
                parts.append(chunk)
    return "\n\n".join(parts)


def _read_docx(filepath: str) -> str:
    """
    [EN]
    Extract text from a DOCX document.

    [ZH]
    Word：正文段落 + 表格（表格按 行内单元格以 | 连接）。
    """
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
    """
    [EN]
    Extract text from an XLSX workbook.

    [ZH]
    Excel（xlsx/xlsm）：逐工作表输出，行内单元格以 | 连接。
    """
    from openpyxl import load_workbook

    wb = load_workbook(filepath, data_only=True, read_only=True)
    parts = []
    try:
        for ws in wb.worksheets:
            from i18n import t as _t

            parts.append(_t("file.sheet_header", name=ws.title))
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    parts.append(" | ".join(cells))
            parts.append("")
    finally:
        wb.close()
    return "\n".join(parts)


def _read_xls(filepath: str) -> str:
    """
    [EN]
    Extract text from a legacy XLS workbook.

    [ZH]
    Excel（旧版 xls）：用 xlrd 读取。
    """
    import xlrd

    book = xlrd.open_workbook(filepath)
    parts = []
    for sheet in book.sheets():
        from i18n import t as _t

        parts.append(_t("file.sheet_header", name=sheet.name))
        for r in range(sheet.nrows):
            cells = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
            if any(cells):
                parts.append(" | ".join(cells))
        parts.append("")
    return "\n".join(parts)


def _read_pptx(filepath: str) -> str:
    """
    [EN]
    Extract text from a PPTX presentation.

    [ZH]
    PowerPoint：逐页提取文本框与表格文字。
    """
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
    """
    [EN]
    Extract text from a PDF document.

    [ZH]
    PDF：用 pypdf 提取文本层（BSD-3-Clause，替代 AGPL 的 PyMuPDF）。

    扫描件没有文本层，提取结果为空 —— 这种情况需要 OCR，不在支持范围内，
    调用方会得到"文件内容为空"，由上层提示用户改用 WPS/OCR 另存后重试。
    """
    from pypdf import PdfReader

    reader = PdfReader(filepath)
    if getattr(reader, "is_encrypted", False):
        # [EN] An empty-password encryption (only a permissions password is set) can be
        # [EN] decrypted first; a real user password cannot be handled.
        # [ZH] 空口令加密（只设了权限密码）可以先解密；真正的用户口令无法处理
        try:
            reader.decrypt("")
        except Exception as e:
            return _err("[!PARSE]", "file.pdf_encrypted", error=e)
    parts = []
    for index, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:  # [EN] One corrupted page must not fail the document.
                                # [ZH] 单页损坏不应让整份文档失败。
            from i18n import t as _t

            text = _t("file.pdf_page_failed", page=index, error=e)
        if text:
            from i18n import t as _t

            parts.append(_t("file.page_header", page=index) + f"\n{text}")
    return "\n\n".join(parts)
