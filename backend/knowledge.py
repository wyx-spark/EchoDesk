"""
[EN]
Local folder knowledge base (v0.2.6 Part 3, tasks 3.1 and 3.2).

Implementation constraints, all deliberate:

* **Standard library only.** The index is a SQLite database queried through the
  FTS5 extension that ships with CPython. No vector database (ChromaDB, FAISS)
  and no embedding model are introduced: either would add tens to hundreds of
  megabytes to the release tree, which contradicts the established installation
  size target, and neither is required for the document scale this tool handles.
* **Chinese segmentation is written here.** FTS5's ``unicode61`` tokenizer does
  not segment Chinese text, so a character-bigram tokenizer is applied to every
  chunk before it is written, and the same tokenizer is applied to the query.
  This keeps recall acceptable for Chinese material without a third-party
  dependency such as ``jieba``.
* **Incremental by (size, modification time).** A folder is re-scanned on every
  re-index; files whose size and modification time are unchanged keep their
  existing chunks, so a second index is proportional to the amount of new
  material rather than to the library size.
* **Behind the background worker.** Scanning, splitting, tokenizing and writing
  are performed through ``document_jobs``' single worker thread; this module
  itself contains no thread management and performs no work on the protocol
  thread except :meth:`KnowledgeBase.search`.
* **Every write path is transactional per document.** A file whose parse fails
  is recorded with its error and does not abort the remaining files.

[ZH]
本地文件夹知识库（v0.2.6 任务 3.1 与 3.2）。

实现约束（均为刻意选择）：

* **仅使用标准库。** 索引是 SQLite 数据库，经 CPython 随附的 FTS5 扩展检索。
  不引入向量库（ChromaDB、FAISS），也不引入嵌入模型：二者都会给发布树增加几十
  到几百 MB，与本项目既定的安装体积目标冲突；对本工具处理的文档规模也非必需。
* **中文分词在本模块内实现。** FTS5 的 ``unicode61`` 分词器不切分中文，因此每个
  切片写入前先经汉字二元组分词，查询走同一分词器。这样在中文材料上仍有可用的
  召回率，且不依赖 ``jieba`` 等第三方包。
* **按（大小、修改时间）增量。** 每次重建索引都会重新扫描文件夹；大小与修改时间
  未变的文件保留既有切片，因此二次建库的代价与新素材数量成正比，而非与库大小
  成正比。
* **在后台工作线程内执行。** 扫描、切片、分词与写入统一经 ``document_jobs`` 的
  单工作线程执行；本模块自身不管理线程，除 :meth:`KnowledgeBase.search` 外不在
  协议线程上执行任何工作。
* **写入以单份文件为事务边界。** 解析失败的文件记录错误后继续处理其余文件，不会
  中断整个作业。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: [EN] Preferred chunk length in characters (roughly 300 tokens) and the
#: [EN] overlap between adjacent chunks. The overlap prevents an answer that
#: [EN] straddles a chunk boundary from being missed.
#: [ZH] 首选切片长度（字符，约 300 token）与相邻切片的重叠长度。重叠可避免
#: [ZH] 答案横跨切片边界时被漏掉。
DEFAULT_CHUNK_CHARS = 600
DEFAULT_OVERLAP_CHARS = 100

#: [EN] Default number of passages injected per question and the default total
#: [EN] character budget. Small local models lose accuracy when too much material
#: [EN] is injected at once.
#: [ZH] 每次提问默认注入的片段数与默认总字符预算。一次性注入过多材料会降低
#: [ZH] 本地小模型的准确率。
DEFAULT_TOP_K = 4
DEFAULT_MAX_CONTEXT_CHARS = 2400

#: [EN] Extensions read directly as text. Office and PDF formats are delegated to
#: [EN] ``file_parser`` so that one parsing implementation serves both paths.
#: [ZH] 直接按文本读取的扩展名。Office 与 PDF 交由 ``file_parser`` 解析，
#: [ZH] 使两条路径共用同一份解析实现。
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log",
    ".ini", ".cfg", ".conf", ".yml", ".yaml", ".toml", ".xml", ".html", ".htm",
    ".rst", ".tex", ".py", ".js", ".ts", ".java", ".cs", ".c", ".h", ".cpp",
    ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".sql",
}

#: [EN] Extensions delegated to ``file_parser``. ``.doc``/``.xls`` are only
#: [EN] readable on Windows through WPS; on other platforms they are recorded as
#: [EN] failures and skipped.
#: [ZH] 交由 ``file_parser`` 解析的扩展名。``.doc``/``.xls`` 仅在 Windows 上经
#: [ZH] WPS 可读；其余平台会记录为失败并跳过。
DOCUMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".pptx", ".xlsx", ".xls", ".xlsm"}

#: [EN] Directories never scanned: version-control metadata, virtual
#: [EN] environments, build output and caches contain no user knowledge and can
#: [EN] contain tens of thousands of files.
#: [ZH] 永不扫描的目录：版本控制元数据、虚拟环境、构建产物与缓存不含用户知识，
#: [ZH] 且可能包含数万个文件。
SKIP_DIRECTORIES = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv",
    "env", ".idea", ".vs", ".vscode", "bin", "obj", "dist", "build",
    ".cache", ".pytest_cache", ".mypy_cache", "site-packages",
}

#: [EN] File-size ceiling for indexing. A single uncommonly large file would
#: [EN] dominate the index and stall the worker; such files are skipped and
#: [EN] counted in the diagnostics.
#: [ZH] 建索引的文件大小上限。单个异常巨大的文件会主导索引并阻塞工作线程；
#: [ZH] 这类文件被跳过并计入诊断统计。
DEFAULT_MAX_FILE_BYTES = 12 * 1024 * 1024

_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WHITESPACE = re.compile(r"[ \t\u3000]+")


class KnowledgeCancelled(Exception):
    """
    [EN]
    Raised inside :meth:`KnowledgeBase.index_library` when the owning job has been
    cancelled. The job runner converts it into the cancelled terminal state.

    [ZH]
    所属作业被取消时在 :meth:`KnowledgeBase.index_library` 内抛出。作业执行器将其
    转换为「已取消」终止状态。
    """


def tokenize(text: str) -> List[str]:
    """
    [EN]
    Split text into index tokens.

    ASCII words are lower-cased and kept whole. A run of Chinese characters is
    expanded into character bigrams, because a single Chinese character is too
    ambiguous to be a useful token and a whole run is unlikely to repeat
    verbatim in a question. A one-character run is kept as a unigram so that it
    can still be matched.

    [ZH]
    将文本切分为索引词元。

    ASCII 词转为小写并整体保留；连续汉字的每一段展开为汉字二元组 —— 单个汉字作为
    词元区分度不足，而整段汉字在提问中几乎不会逐字重复。长度为 1 的汉字段保留为
    一元组，以保证仍可命中。
    """
    lowered = (text or "").lower()
    tokens: List[str] = [match.group(0) for match in _ASCII_WORD.finditer(lowered)]
    for match in _CJK_RUN.finditer(lowered):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
            continue
        for index in range(len(run) - 1):
            tokens.append(run[index:index + 2])
    return tokens


def query_expression(text: str) -> str:
    """
    [EN]
    Build the FTS5 ``MATCH`` expression for a question.

    Tokens are quoted individually and joined with ``OR``: a question shares only
    part of its vocabulary with the source material, so an ``AND`` requirement
    would suppress nearly every hit. Duplicate tokens are removed while
    preserving order.

    [ZH]
    为提问构造 FTS5 ``MATCH`` 表达式。

    各词元分别加引号并以 ``OR`` 连接：提问与原文只共享部分词汇，若用 ``AND``
    几乎会压掉全部命中。重复词元按首次出现顺序去重。
    """
    seen: set = set()
    parts: List[str] = []
    for token in tokenize(text):
        if token in seen:
            continue
        seen.add(token)
        parts.append('"' + token.replace('"', '""') + '"')
    return " OR ".join(parts)


def chunk_text(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS,
               overlap_chars: int = DEFAULT_OVERLAP_CHARS) -> List[str]:
    """
    [EN]
    Split one document into overlapping chunks.

    Paragraph breaks are normalized first. The window then advances by
    ``chunk_chars - overlap_chars``; each window end is pulled back to the last
    natural break (newline, full stop, space) inside its final 40 %, so chunks
    rarely end mid-sentence. A window with no natural break is cut hard, which
    keeps a single very long paragraph bounded.

    The returned list never contains empty strings.

    [ZH]
    将一份文档切分为带重叠的片段。

    先归一化段落换行；窗口按 ``chunk_chars - overlap_chars`` 前进；每个窗口的结尾
    回退到其后 40% 范围内最后一个自然断点（换行、句号、空格），使片段尽量不终止于
    句子中间。没有自然断点的窗口按长度硬切，保证单个超长段落也有界。

    返回列表不含空串。
    """
    if not text:
        return []
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [segment.strip() for segment in re.split(r"\n{2,}", normalized)]
    paragraphs = [_WHITESPACE.sub(" ", segment) for segment in paragraphs if segment]
    flat = "\n".join(paragraphs)
    if len(flat) <= chunk_chars:
        return [flat] if flat else []

    step = max(1, chunk_chars - max(0, overlap_chars))
    chunks: List[str] = []
    start = 0
    length = len(flat)
    while start < length:
        end = min(length, start + chunk_chars)
        if end < length:
            floor = start + int(chunk_chars * 0.6)
            cut = flat.rfind("\n", floor, end)
            if cut <= start:
                cut = flat.rfind("。", floor, end)
            if cut <= start:
                cut = flat.rfind(". ", floor, end)
            if cut <= start:
                cut = flat.rfind(" ", floor, end)
            if cut > start:
                end = cut + 1
        piece = flat[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


def default_db_path() -> str:
    """
    [EN]
    Default location of the index database: ``<state dir>/knowledge/index.sqlite3``.

    ``state_dir()`` is imported lazily so that this module stays importable in
    tests that patch the environment before importing ``config``.

    [ZH]
    索引数据库的默认位置：``<状态目录>/knowledge/index.sqlite3``。

    ``state_dir()`` 惰性导入，使本模块在测试于导入 ``config`` 之前改写环境变量时
    仍可导入。
    """
    import config  # noqa: PLC0415 - [EN] lazy import avoids a module import cycle
                        # [ZH] 惰性导入，避免模块级循环依赖

    return os.path.join(config.state_dir(), "knowledge", "index.sqlite3")


def read_document(path: str, max_chars: int = 0) -> Tuple[str, str]:
    """
    [EN]
    Read one indexable document and return ``(text, error)``.

    ``error`` is non-empty when the file could not be read; ``text`` is empty in
    that case. Text extensions are read directly with UTF-8 first and two
    fallback encodings; document extensions are delegated to
    ``file_parser.parse_file_with_wps`` so that PDF and Office files are parsed by
    the same implementation as the upload path.

    [ZH]
    读取一份可索引文档并返回 ``(正文, 错误)``。

    读取失败时 ``error`` 非空，此时 ``text`` 为空。文本扩展名先按 UTF-8 读取，
    失败再尝试两种回退编码；文档扩展名交由 ``file_parser.parse_file_with_wps``，
    使 PDF 与 Office 文件走与上传路径相同的解析实现。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXTENSIONS:
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                with open(path, "r", encoding=encoding, errors="strict") as handle:
                    body = handle.read()
                return (body[:max_chars] if max_chars and len(body) > max_chars else body), ""
            except UnicodeDecodeError:
                continue
            except OSError as exc:
                return "", f"{type(exc).__name__}: {exc}"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                body = handle.read()
            return (body[:max_chars] if max_chars and len(body) > max_chars else body), ""
        except OSError as exc:
            return "", f"{type(exc).__name__}: {exc}"
    if ext in DOCUMENT_EXTENSIONS:
        try:
            from file_parser import is_parse_error, parse_file_with_wps  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - [EN] parser unavailable -> recorded failure
                                  # [ZH] 解析器不可用时按失败记录
            return "", f"file_parser unavailable: {exc}"
        try:
            body = parse_file_with_wps(path, max_chars)
        except Exception as exc:  # noqa: BLE001 - [EN] one file must not abort the job
                                  # [ZH] 单份文件不得中止作业
            return "", f"{type(exc).__name__}: {exc}"
        if is_parse_error(body):
            return "", body
        return body, ""
    return "", "unsupported extension"


class KnowledgeBase:
    """
    [EN]
    SQLite-backed folder knowledge base.

    A single database holds every mounted library, so a conversation can bind
    any subset of libraries (task 3.2: per-conversation isolation) while the
    index write path stays shared and serialized by the document worker.

    Threading contract: an internal re-entrant lock serializes every database
    access, because the writer runs on the document worker thread while
    :meth:`search` runs on the protocol thread. Connections are opened per
    operation rather than shared, which keeps the lock the only shared mutable
    state.

    [ZH]
    基于 SQLite 的文件夹知识库。

    单一数据库承载全部已挂载库，因此对话可绑定其中任意子集（任务 3.2：按会话
    隔离），而写入路径仍共享并由文档工作线程串行执行。

    线程约定：内部可重入锁串行化全部数据库访问 —— 写入在文档工作线程上执行，
    :meth:`search` 在协议线程上执行。连接按操作创建而不共享，使锁成为唯一的共享
    可变状态。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        """
        [EN]
        Open (or create) the index database and ensure the schema exists.

        [ZH]
        打开（或创建）索引数据库并确保表结构存在。
        """
        self.db_path = str(db_path or default_db_path())
        self._dir = os.path.dirname(self.db_path) or "."
        self._lock = threading.RLock()
        self._schema_ready = False
        self.ensure_schema()

    # [EN] ---------- Schema ----------
    # [ZH] ---------- 表结构 ----------

    def _connect(self) -> sqlite3.Connection:
        """
        [EN]
        Open one connection with the pragmas required for a writer and a reader
        to coexist (WAL journaling).

        [ZH]
        打开一个连接，并设置写入方与读取方可共存所需的参数（WAL 日志）。
        """
        os.makedirs(self._dir, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def ensure_schema(self) -> None:
        """
        [EN]
        Create the four tables when missing. The FTS5 table stores the token
        stream, not the raw text: raw text is read back through ``kb_chunk`` so
        that the index and the retrieval result can never diverge on
        normalization.

        [ZH]
        缺少表时创建四张表。FTS5 表存放分词后的词元流而非原文：原文经 ``kb_chunk``
        回读，使索引与检索结果不会在归一化上产生分歧。
        """
        with self._lock:
            connection = self._connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS kb_library (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        root TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'idle',
                        last_error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS kb_document (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        library_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        name TEXT NOT NULL,
                        size INTEGER NOT NULL DEFAULT 0,
                        mtime REAL NOT NULL DEFAULT 0,
                        chunks INTEGER NOT NULL DEFAULT 0,
                        indexed_at REAL NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT '',
                        UNIQUE(library_id, path)
                    );
                    CREATE TABLE IF NOT EXISTS kb_chunk (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        document_id INTEGER NOT NULL,
                        library_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        text TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc
                        ON kb_chunk(document_id);
                    CREATE INDEX IF NOT EXISTS idx_kb_doc_lib
                        ON kb_document(library_id);
                    CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
                        body,
                        chunk_id UNINDEXED,
                        tokenize='unicode61'
                    );
                    """
                )
                connection.commit()
                self._schema_ready = True
            finally:
                connection.close()

    # [EN] ---------- Library registry ----------
    # [ZH] ---------- 库注册表 ----------

    @staticmethod
    def library_id_for(root: str) -> str:
        """
        [EN]
        Stable identifier derived from the absolute folder path, so that
        re-mounting the same folder reuses the existing index instead of
        creating a duplicate library.

        [ZH]
        由绝对路径派生的稳定标识；重新挂载同一文件夹时复用既有索引，而不是新建
        重复的库。
        """
        key = os.path.abspath(root).casefold().encode("utf-8", "replace")
        return "kb-" + hashlib.sha1(key).hexdigest()[:12]

    def libraries(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Every mounted library with its document and chunk counts.

        [ZH]
        全部已挂载库及其文档数与切片数。
        """
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT l.id, l.name, l.root, l.created_at, l.updated_at,
                           l.status, l.last_error,
                           (SELECT COUNT(*) FROM kb_document d
                             WHERE d.library_id = l.id) AS documents,
                           (SELECT COUNT(*) FROM kb_chunk c
                             WHERE c.library_id = l.id) AS chunks
                      FROM kb_library l
                  ORDER BY l.created_at ASC
                    """
                ).fetchall()
            finally:
                connection.close()
        return [dict(row) for row in rows]

    def get_library(self, library_id: str) -> Optional[Dict[str, Any]]:
        """
        [EN]
        One library by identifier, or ``None`` when it is not mounted.

        [ZH]
        按标识取单个库；未挂载时返回 ``None``。
        """
        for item in self.libraries():
            if item["id"] == str(library_id or ""):
                return item
        return None

    def mount(self, root: str, name: str = "") -> Dict[str, Any]:
        """
        [EN]
        Register a folder as a library.

        The folder is validated here (it must exist and be a directory), but it
        is **not** scanned: scanning belongs to the background job so that the
        protocol thread never blocks on a large tree. A folder already mounted
        is returned unchanged.

        [ZH]
        将文件夹注册为库。

        此处只校验文件夹（必须存在且为目录），**不扫描**：扫描属于后台作业，
        协议线程不得因大目录树而阻塞。已挂载的文件夹原样返回。
        """
        path = os.path.abspath(os.path.expanduser(str(root or "").strip()))
        if not path or not os.path.isdir(path):
            raise ValueError("knowledge directory does not exist")
        library_id = self.library_id_for(path)
        existing = self.get_library(library_id)
        if existing is not None:
            return existing
        now = time.time()
        label = (name or os.path.basename(path) or path).strip()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT INTO kb_library(id, name, root, created_at, updated_at,"
                    " status, last_error) VALUES(?,?,?,?,?,?,?)",
                    (library_id, label, path, now, now, "idle", ""),
                )
                connection.commit()
            finally:
                connection.close()
        return {"id": library_id, "name": label, "root": path,
                "created_at": now, "updated_at": now, "status": "idle",
                "last_error": "", "documents": 0, "chunks": 0}

    def set_status(self, library_id: str, status: str, error: str = "") -> None:
        """
        [EN]
        Record the current indexing state of one library for the user interface.

        [ZH]
        记录单个库的当前建索引状态，供界面显示。
        """
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE kb_library SET status=?, last_error=?, updated_at=?"
                    " WHERE id=?",
                    (str(status), str(error), time.time(), str(library_id)),
                )
                connection.commit()
            finally:
                connection.close()

    def remove_library(self, library_id: str) -> bool:
        """
        [EN]
        Unmount a library and purge every document, chunk and full-text row that
        belongs to it. Returns whether the library existed.

        [ZH]
        卸载库并清除其全部文档、切片与全文索引行。返回该库是否存在。
        """
        with self._lock:
            connection = self._connect()
            try:
                if connection.execute("SELECT 1 FROM kb_library WHERE id=?",
                                      (str(library_id),)).fetchone() is None:
                    return False
                self._purge_documents(connection, str(library_id))
                connection.execute("DELETE FROM kb_library WHERE id=?", (str(library_id),))
                connection.commit()
            finally:
                connection.close()
        return True

    # [EN] ---------- Scanning ----------
    # [ZH] ---------- 扫描 ----------

    def iter_files(self, root: str, max_files: int = 20000,
                   max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> List[Dict[str, Any]]:
        """
        [EN]
        List the indexable files under ``root``.

        Symbolic links to directories are not followed: a link loop would make
        the scan unbounded. Hidden files, skipped directories and unsupported
        extensions are excluded. The result is sorted by path so that indexing
        order, progress reporting and error lists are deterministic.

        [ZH]
        列出 ``root`` 下可索引的文件。

        不跟随指向目录的符号链接：链接环会使扫描无界。隐藏文件、跳过目录与不支持的
        扩展名均被排除。结果按路径排序，使建索引顺序、进度上报与错误清单可复现。
        """
        found: List[Dict[str, Any]] = []
        visited: set = set()
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            try:
                real = os.path.realpath(current)
            except OSError:
                continue
            if real in visited:
                dirnames[:] = []
                continue
            visited.add(real)
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in SKIP_DIRECTORIES and not name.startswith(".")
            )
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in TEXT_EXTENSIONS and ext not in DOCUMENT_EXTENSIONS:
                    continue
                path = os.path.join(current, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path) or stat.st_size > max_file_bytes:
                    continue
                found.append({"path": path, "name": name, "size": int(stat.st_size),
                              "mtime": float(stat.st_mtime)})
                if len(found) >= max_files:
                    return found
        return found

    # [EN] ---------- Indexing ----------
    # [ZH] ---------- 建索引 ----------

    def index_library(self, library_id: str, progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                      cancel_event: Optional[threading.Event] = None,
                      chunk_chars: int = DEFAULT_CHUNK_CHARS,
                      overlap_chars: int = DEFAULT_OVERLAP_CHARS,
                      max_files: int = 20000,
                      max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                      max_chars_per_file: int = 200000) -> Dict[str, Any]:
        """
        [EN]
        Incrementally index one library and return the counters.

        A file is re-parsed only when its size or modification time changed; a
        file that disappeared from disk has its rows removed. Progress is
        reported once per file through ``progress`` (the job's ``emit``), which
        keeps the interface responsive without an unsolicited stdout channel.

        Raises :class:`KnowledgeCancelled` when ``cancel_event`` is set, so the
        worker can stop promptly between files.

        [ZH]
        增量建立单个库的索引并返回计数。

        仅在大小或修改时间变化时重新解析文件；磁盘上已消失的文件会被删除索引行。
        进度按文件通过 ``progress``（作业的 ``emit``）上报，使界面无需额外的主动
        stdout 通道即可保持响应。

        ``cancel_event`` 被置位时抛出 :class:`KnowledgeCancelled`，使工作线程可在
        文件之间及时停止。
        """
        library = self.get_library(library_id)
        if library is None:
            raise ValueError("knowledge library not mounted")
        root = str(library["root"])
        started = time.time()
        stats: Dict[str, Any] = {"library_id": library_id, "total": 0, "added": 0,
                                 "updated": 0, "removed": 0, "skipped": 0,
                                 "failed": 0, "chunks": 0, "elapsed": 0.0}
        files = self.iter_files(root, max_files=max_files,
                                max_file_bytes=max_file_bytes)
        stats["total"] = len(files)
        existing = self._existing_documents(library_id)
        self.set_status(library_id, "indexing")

        seen = set()
        for index, entry in enumerate(files, 1):
            if cancel_event is not None and cancel_event.is_set():
                self.set_status(library_id, "cancelled")
                raise KnowledgeCancelled("knowledge indexing cancelled")
            path = entry["path"]
            seen.add(path)
            previous = existing.get(path)
            if (previous is not None
                    and int(previous["size"]) == entry["size"]
                    and abs(float(previous["mtime"]) - entry["mtime"]) < 0.001
                    and not previous["error"]):
                stats["skipped"] += 1
            else:
                body, error = read_document(path, max_chars=max_chars_per_file)
                if error or not body.strip():
                    message = error or "empty document"
                    self._store_document(library_id, entry, [], error=message)
                    stats["failed"] += 1
                else:
                    pieces = chunk_text(body, chunk_chars, overlap_chars)
                    self._store_document(library_id, entry, pieces, error="")
                    stats["chunks"] += len(pieces)
                    if previous is None:
                        stats["added"] += 1
                    else:
                        stats["updated"] += 1
            if progress is not None:
                progress({"type": "knowledge_progress", "phase": "indexing",
                          "library_id": library_id, "index": index,
                          "total": len(files), "name": entry["name"],
                          "path": path, "stats": dict(stats)})
        missing = [path for path in existing if path not in seen]
        for path in missing:
            self._delete_document(library_id, path)
            stats["removed"] += 1
        stats["elapsed"] = round(time.time() - started, 3)
        self.set_status(library_id, "ready")
        if progress is not None:
            progress({"type": "knowledge_progress", "phase": "done",
                      "library_id": library_id, "index": len(files),
                      "total": len(files), "stats": dict(stats)})
        return stats

    def _existing_documents(self, library_id: str) -> Dict[str, Dict[str, Any]]:
        """
        [EN]
        Current document rows of one library, keyed by absolute path.

        [ZH]
        单个库当前的文档行，以绝对路径为键。
        """
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT path, size, mtime, error FROM kb_document WHERE library_id=?",
                    (str(library_id),),
                ).fetchall()
            finally:
                connection.close()
        return {row["path"]: dict(row) for row in rows}

    def _store_document(self, library_id: str, entry: Dict[str, Any],
                        chunks: Sequence[str], error: str = "") -> None:
        """
        [EN]
        Replace one document and its chunks atomically.

        Chunks are written only when parsing succeeded; a failed file keeps a
        row with its error and zero chunks, so the next index run retries it
        (the retry condition is "row absent or row has an error").

        [ZH]
        原子替换一份文档及其切片。

        仅在解析成功时写入切片；失败的文件保留一行并记录错误、切片数为零，使下次
        建索引会重试（重试条件为「无对应行」或「该行带错误」）。
        """
        path = str(entry["path"])
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT id FROM kb_document WHERE library_id=? AND path=?",
                    (str(library_id), path),
                ).fetchone()
                if row is not None:
                    self._purge_document_rows(connection, int(row["id"]))
                    connection.execute("DELETE FROM kb_document WHERE id=?",
                                       (int(row["id"]),))
                cursor = connection.execute(
                    "INSERT INTO kb_document(library_id, path, name, size, mtime,"
                    " chunks, indexed_at, error) VALUES(?,?,?,?,?,?,?,?)",
                    (str(library_id), path, str(entry["name"]), int(entry["size"]),
                     float(entry["mtime"]), len(chunks), time.time(), str(error)),
                )
                document_id = int(cursor.lastrowid)
                for ordinal, text in enumerate(chunks):
                    chunk = connection.execute(
                        "INSERT INTO kb_chunk(document_id, library_id, ordinal, text)"
                        " VALUES(?,?,?,?)",
                        (document_id, str(library_id), ordinal, text),
                    )
                    connection.execute(
                        "INSERT INTO kb_fts(body, chunk_id) VALUES(?,?)",
                        (" ".join(tokenize(text)), int(chunk.lastrowid)),
                    )
                connection.commit()
            finally:
                connection.close()

    def _delete_document(self, library_id: str, path: str) -> None:
        """
        [EN]
        Remove one document row together with its chunks and full-text rows.

        [ZH]
        删除单份文档行及其切片与全文索引行。
        """
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT id FROM kb_document WHERE library_id=? AND path=?",
                    (str(library_id), path),
                ).fetchone()
                if row is not None:
                    self._purge_document_rows(connection, int(row["id"]))
                    connection.execute("DELETE FROM kb_document WHERE id=?",
                                       (int(row["id"]),))
                    connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _purge_document_rows(connection: sqlite3.Connection, document_id: int) -> None:
        """
        [EN]
        Delete the full-text rows and chunk rows of one document. The full-text
        rows must be removed first: the join back to ``kb_chunk`` is how they are
        located.

        [ZH]
        删除单份文档的全文索引行与切片行。必须先删全文行：它们经回连
        ``kb_chunk`` 定位。
        """
        connection.execute(
            "DELETE FROM kb_fts WHERE chunk_id IN"
            " (SELECT id FROM kb_chunk WHERE document_id=?)",
            (int(document_id),),
        )
        connection.execute("DELETE FROM kb_chunk WHERE document_id=?", (int(document_id),))

    def _purge_documents(self, connection: sqlite3.Connection, library_id: str) -> None:
        """
        [EN]
        Delete every document of one library; used when a library is unmounted.

        [ZH]
        删除单个库的全部文档；库被卸载时使用。
        """
        connection.execute(
            "DELETE FROM kb_fts WHERE chunk_id IN"
            " (SELECT id FROM kb_chunk WHERE library_id=?)",
            (str(library_id),),
        )
        connection.execute("DELETE FROM kb_chunk WHERE library_id=?", (str(library_id),))
        connection.execute("DELETE FROM kb_document WHERE library_id=?", (str(library_id),))

    # [EN] ---------- Retrieval ----------
    # [ZH] ---------- 检索 ----------

    def search(self, query: str, library_ids: Optional[Iterable[str]] = None,
               top_k: int = DEFAULT_TOP_K, min_score: float = 0.0,
               snippet_chars: int = 600) -> List[Dict[str, Any]]:
        """
        [EN]
        Retrieve the highest-ranked chunks for a question.

        ``library_ids`` restricts the search to the libraries bound to the
        current conversation (task 3.2); ``None`` searches every mounted
        library. The BM25 score is returned as a positive number where larger is
        better, so ``min_score`` reads naturally in configuration.

        An empty expression (a question made only of punctuation) returns no
        hits rather than raising a syntax error from FTS5.

        [ZH]
        为提问取回排名最高的切片。

        ``library_ids`` 将检索限制在当前对话所绑定的库（任务 3.2）；``None`` 表示
        检索全部已挂载库。BM25 得分以“越大越好”的正数返回，使配置中的
        ``min_score`` 语义直观。

        空表达式（提问仅含标点）返回空结果，而不是让 FTS5 抛出语法错误。
        """
        expression = query_expression(query)
        if not expression:
            return []
        limit = max(1, min(int(top_k or DEFAULT_TOP_K), 20))
        selected: Optional[List[str]] = None
        if library_ids is not None:
            selected = [str(item) for item in library_ids if str(item or "").strip()]
            if not selected:
                return []
        sql = (
            "SELECT c.id AS chunk_id, c.text AS text, c.ordinal AS ordinal,"
            " c.library_id AS library_id, d.path AS path, d.name AS name,"
            " bm25(kb_fts) AS rank"
            " FROM kb_fts"
            " JOIN kb_chunk c ON c.id = kb_fts.chunk_id"
            " JOIN kb_document d ON d.id = c.document_id"
            " WHERE kb_fts MATCH ?"
        )
        parameters: List[Any] = [expression]
        if selected is not None:
            sql += " AND c.library_id IN (" + ",".join("?" for _ in selected) + ")"
            parameters.extend(selected)
        sql += " ORDER BY rank LIMIT ?"
        parameters.append(limit * 3)
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(sql, parameters).fetchall()
            except sqlite3.OperationalError:
                # [EN] A malformed expression (for example a sole operator) must
                # [EN] degrade to "no result", never to a failed request.
                # [ZH] 表达式异常（例如只剩运算符）必须退化为「无结果」，绝不导致
                # [ZH] 请求失败。
                return []
            finally:
                connection.close()
        hits: List[Dict[str, Any]] = []
        for row in rows:
            score = -float(row["rank"])
            if score < float(min_score or 0.0):
                continue
            text = str(row["text"])
            hits.append({
                "chunk_id": int(row["chunk_id"]),
                "library_id": str(row["library_id"]),
                "path": str(row["path"]),
                "name": str(row["name"]),
                "ordinal": int(row["ordinal"]),
                "score": round(score, 6),
                "text": text[:snippet_chars],
            })
            if len(hits) >= limit:
                break
        return hits

    def as_context(self, query: str, library_ids: Optional[Iterable[str]] = None,
                   top_k: int = DEFAULT_TOP_K,
                   max_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
                   min_score: float = 0.0) -> str:
        """
        [EN]
        Render the retrieved passages as an injectable context block.

        The block is Chinese prompt content, consistent with the rest of the
        prompt text, and is deliberately outside the language pack. An empty
        string means no usable hit; the caller must not inject an empty block.

        [ZH]
        将检索到的片段渲染为可注入的上下文块。

        该块与其余提示词一样属于中文提示词内容，刻意不进入语言包。返回空串表示
        没有可用命中；调用方不得注入空块。
        """
        hits = self.search(query, library_ids=library_ids, top_k=top_k,
                           min_score=min_score)
        if not hits:
            return ""
        lines = ["【本地知识库检索结果】（来自用户挂载的本地资料；回答时请用 [编号] 标注依据，"
                 "没有依据的内容不要写成引用）"]
        used = 0
        picked = 0
        for hit in hits:
            body = hit["text"].strip()
            if not body:
                continue
            head = (f"[{picked + 1}] 来源：{hit['name']} · {hit['path']}"
                    f"（第 {hit['ordinal'] + 1} 片段）")
            block = f"{head}\n{body}"
            if picked and used + len(block) > int(max_chars or DEFAULT_MAX_CONTEXT_CHARS):
                break
            lines.append(block)
            used += len(block)
            picked += 1
        return "\n\n".join(lines) if picked else ""

    def stats(self) -> Dict[str, Any]:
        """
        [EN]
        Aggregate index counters for diagnostics and status display.

        [ZH]
        供诊断与状态显示使用的索引汇总计数。
        """
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM kb_library) AS libraries,"
                    " (SELECT COUNT(*) FROM kb_document) AS documents,"
                    " (SELECT COUNT(*) FROM kb_chunk) AS chunks"
                ).fetchone()
            finally:
                connection.close()
        return {"libraries": int(row["libraries"]),
                "documents": int(row["documents"]),
                "chunks": int(row["chunks"]),
                "db_path": self.db_path}
