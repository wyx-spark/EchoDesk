"""
[EN]
Long-term memory: a local fact store plus a model-driven extractor (v0.2.6
Part 3, task 3.3).

Design decisions:

* **A plain JSON file, not the knowledge index.** Memory entries are short,
  human-readable facts that the user may reasonably inspect and edit; keeping
  them in ``memory.json`` makes that possible without a database reader. The
  knowledge index, by contrast, may hold millions of characters and belongs in
  SQLite.
* **Extraction never touches the live conversation.** The caller passes a
  transcript snapshot and a ``complete(messages)`` callable; the snapshot is
  rendered into a fresh prompt. The conversation object that owns the snapshot
  is a private temporary instance, exactly as the segmented summarizer does
  (v0.2.6 task 2.2), so no prompt can leak into the main history.
* **Privacy boundary.** Everything here is local: extraction sends the
  transcript to the local model only, and the store never writes outside the
  state directory.
* **Failures are contained.** A malformed model reply yields zero new memories
  and an error string, never an exception that could propagate into the
  conversation-end path.

[ZH]
长期记忆：本地事实存储与模型驱动的提取器（v0.2.6 任务 3.3）。

设计决定：

* **使用普通 JSON 文件，而非知识索引。** 记忆条目是简短、可读的事实，用户有权
  检视与修改；存放在 ``memory.json`` 中无需数据库读取器即可做到。知识索引则可能
  包含上百万字符，应当存入 SQLite。
* **提取过程绝不触碰在线对话。** 调用方传入对话文本快照与 ``complete(messages)``
  回调，快照被渲染成一份全新的提示词。持有该快照的会话对象是按分段摘要同样的方式
  创建的私有临时实例（v0.2.6 任务 2.2），因此任何提示词都不会进入主对话历史。
* **隐私边界。** 本模块的一切都在本地：提取只把对话送给本机模型，存储不写出状态
  目录之外。
* **故障受控。** 模型回复格式错误时得到零条新记忆与一个错误串，绝不抛出异常进入
  对话结束路径。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

#: [EN] Categories reported to the interface. They are storage metadata, not UI
#: [EN] text: the interface localizes each category through
#: [EN] ``memory.category.<id>`` in its own language pack.
#: [ZH] 报告给界面的分类。它们是存储元数据而非界面文本：界面经自身语言包的
#: [ZH] ``memory.category.<id>`` 逐项本地化。
CATEGORIES = ("identity", "preference", "project", "contact", "habit", "other")

#: [EN] Maximum transcript length handed to the extractor. A long conversation is
#: [EN] truncated from the front: the most recent turns carry the facts that are
#: [EN] most likely to matter next time.
#: [ZH] 交给提取器的对话文本长度上限。超长对话从头部截断：最近几轮包含下次最可能
#: [ZH] 用到的事实。
MAX_TRANSCRIPT_CHARS = 8000

#: [EN] Default injection budget and item ceiling. The injection block shares the
#: [EN] system-prompt budget with the mode, task and file layers, so it must stay
#: [EN] small.
#: [ZH] 默认注入预算与条目上限。注入块与模式、任务、文件各层共享系统提示词预算，
#: [ZH] 因此必须保持简短。
DEFAULT_MAX_CHARS = 1200
DEFAULT_MAX_ITEMS = 200


def default_memory_path() -> str:
    """
    [EN]
    Default location of the memory file: ``<state dir>/memory.json``.

    ``state_dir()`` is imported lazily so that tests can redirect the state
    directory before the configuration module is imported.

    [ZH]
    记忆文件的默认位置：``<状态目录>/memory.json``。

    ``state_dir()`` 惰性导入，使测试可在导入配置模块之前改写状态目录。
    """
    import config  # noqa: PLC0415 - [EN] lazy import avoids a module import cycle
                        # [ZH] 惰性导入，避免模块级循环依赖

    return os.path.join(config.state_dir(), "memory.json")


def _normalize(text: str) -> str:
    """
    [EN]
    Normalization key used for duplicate detection: whitespace collapsed, common
    punctuation removed, case folded. Two statements that differ only in spacing
    or punctuation describe the same fact.

    [ZH]
    用于重复检测的归一化键：压缩空白、去除常见标点、大小写折叠。仅在空白或标点上
    不同的两条陈述描述的是同一事实。
    """
    lowered = (text or "").strip().lower()
    lowered = re.sub(r"[\s\u3000]+", "", lowered)
    return re.sub(r"[，。！？、；：,.!?;:\"'（）()\[\]【】]", "", lowered)


class MemoryStore:
    """
    [EN]
    Thread-safe fact store persisted as one JSON document.

    Every mutation is flushed immediately: the file is small and a memory that
    was extracted but lost on an unexpected exit is worse than a slightly
    slower write. Writes are serialized by a lock because the extractor runs on
    the document worker thread while the protocol thread may read or delete.

    [ZH]
    以单个 JSON 文档持久化的线程安全事实存储。

    每次修改立即落盘：文件很小，而“已提取却在意外退出时丢失”的记忆比稍慢的写入
    更糟。写入由锁串行化，因为提取在文档工作线程上执行，而协议线程可能同时读取或
    删除。
    """

    def __init__(self, path: Optional[str] = None, max_items: int = DEFAULT_MAX_ITEMS) -> None:
        """
        [EN]
        Bind the store to a file and load its current contents.

        [ZH]
        将存储绑定到文件并载入当前内容。
        """
        self.path = str(path or default_memory_path())
        self.max_items = max(1, int(max_items or DEFAULT_MAX_ITEMS))
        self._lock = threading.RLock()
        self._items: List[Dict[str, Any]] = []
        self._load()

    # [EN] ---------- Persistence ----------
    # [ZH] ---------- 持久化 ----------

    def _load(self) -> None:
        """
        [EN]
        Read the memory file; a missing or unreadable file yields an empty store
        rather than an error, because memory is an enhancement, not a
        prerequisite.

        [ZH]
        读取记忆文件；文件缺失或不可解析时得到空存储而不是报错 —— 记忆是增强项，
        不是前置条件。
        """
        self._items = []
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        raw_items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            return
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            category = str(entry.get("category") or "other")
            self._items.append({
                "id": str(entry.get("id") or self._new_id()),
                "text": text,
                "category": category if category in CATEGORIES else "other",
                "created_at": float(entry.get("created_at") or time.time()),
                "source": str(entry.get("source") or ""),
            })

    def save(self) -> None:
        """
        [EN]
        Write the store atomically: a temporary file is written and then
        replaced, so an interrupted write cannot leave a truncated
        ``memory.json``.

        [ZH]
        原子写入存储：先写临时文件再替换，中断的写入不会留下被截断的
        ``memory.json``。
        """
        with self._lock:
            payload = {"version": 1, "updated_at": time.time(),
                       "items": list(self._items)}
            body = json.dumps(payload, ensure_ascii=False, indent=2)
        directory = os.path.dirname(self.path)
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
            os.replace(temporary, self.path)
        except OSError:
            # [EN] A read-only state directory disables persistence but must not
            # [EN] disable the conversation.
            # [ZH] 只读状态目录会使持久化失效，但不得因此中断对话。
            pass

    @staticmethod
    def _new_id() -> str:
        """
        [EN]
        Short monotonic identifier for one memory entry.

        [ZH]
        单条记忆的短标识，随时间单调。
        """
        return "mem-" + format(int(time.time() * 1000), "x")

    # [EN] ---------- Queries and mutations ----------
    # [ZH] ---------- 查询与修改 ----------

    def items(self) -> List[Dict[str, Any]]:
        """
        [EN]
        Snapshot of every stored fact, oldest first.

        [ZH]
        全部已存事实的快照，按写入时间从早到晚。
        """
        with self._lock:
            return [dict(entry) for entry in self._items]

    def add(self, text: str, category: str = "other", source: str = "") -> Optional[Dict[str, Any]]:
        """
        [EN]
        Add one fact. Returns ``None`` when the fact is empty or duplicates an
        existing entry (duplication is ignored silently: re-running extraction
        over the same conversation must not multiply entries).

        When the item ceiling is reached, the oldest entry is dropped so that the
        store cannot grow without bound.

        [ZH]
        添加一条事实。事实为空或与已有条目重复时返回 ``None``（重复静默忽略：
        对同一段对话重复提取不得使条目翻倍）。

        达到条目上限时丢弃最旧的一条，使存储不会无限增长。
        """
        body = (text or "").strip()
        if not body:
            return None
        normalized = _normalize(body)
        if not normalized:
            return None
        with self._lock:
            if any(_normalize(entry["text"]) == normalized for entry in self._items):
                return None
            entry = {"id": self._new_id(),
                     "text": body[:400],
                     "category": category if category in CATEGORIES else "other",
                     "created_at": time.time(),
                     "source": str(source or "")}
            self._items.append(entry)
            while len(self._items) > self.max_items:
                self._items.pop(0)
        self.save()
        return dict(entry)

    def remove(self, memory_id: str) -> bool:
        """
        [EN]
        Forget one fact by identifier. Returns whether it existed.

        [ZH]
        按标识忘记一条事实。返回其是否存在。
        """
        target = str(memory_id or "")
        with self._lock:
            for index, entry in enumerate(self._items):
                if entry["id"] == target:
                    self._items.pop(index)
                    break
            else:
                return False
        self.save()
        return True

    def clear(self) -> int:
        """
        [EN]
        Forget every fact and return the number of removed entries.

        [ZH]
        忘记全部事实并返回清除条数。
        """
        with self._lock:
            count = len(self._items)
            self._items = []
        self.save()
        return count

    def as_prompt(self, max_chars: int = DEFAULT_MAX_CHARS) -> str:
        """
        [EN]
        Render the memory block injected into the system prompt.

        The ordering is newest first because newer facts are more likely to
        reflect the user's current situation. The text is Chinese prompt
        content and deliberately outside the language pack.

        [ZH]
        渲染注入系统提示词的记忆块。

        按由新到旧排序：较新的事实更可能反映用户当前状况。该文本属于中文提示词
        内容，刻意不进入语言包。
        """
        with self._lock:
            entries = list(reversed(self._items))
        if not entries:
            return ""
        budget = max(120, int(max_chars or DEFAULT_MAX_CHARS))
        lines = ["【长期记忆】以下为用户在此前对话中交代过的事实，供参考；"
                 "与用户本次说法冲突时，以本次说法为准："]
        used = len(lines[0])
        for entry in entries:
            line = f"- {entry['text']}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines) if len(lines) > 1 else ""

    def stats(self) -> Dict[str, Any]:
        """
        [EN]
        Aggregate counters and the file location, for diagnostics.

        [ZH]
        供诊断使用的汇总计数与文件位置。
        """
        with self._lock:
            items = list(self._items)
        categories: Dict[str, int] = {}
        for entry in items:
            categories[entry["category"]] = categories.get(entry["category"], 0) + 1
        return {"count": len(items), "path": self.path, "categories": categories}


#: [EN] The extraction instruction. This is prompt content sent to the model, not
#: [EN] interface text, so it remains Chinese and outside the language pack, in
#: [EN] the same way as the task templates in ``prompts.py``.
#: [ZH] 提取指令。这是发送给模型的提示词内容而非界面文本，因此与本项目
#: [ZH] ``prompts.py`` 中的任务模板一样保持中文且不进入语言包。
_EXTRACTION_INSTRUCTION = (
    "[长期记忆提取任务]\n"
    "一段对话刚刚结束。请从中提取**跨会话仍然有价值**的长期事实，供后续对话参考。\n"
    "规则：\n"
    "1. 只记录用户明确说出的事实，不得推断、不得补全、不得改写含义；\n"
    "2. 每条一句话，不超过 60 字，采用第三人称陈述（例如「用户是一名中学教师」）；\n"
    "3. 忽略一次性的任务细节、临时的计算过程、闲聊，以及助手的说法；\n"
    "4. 类别只能取 identity（身份）、preference（偏好）、project（项目）、"
    "contact（联系方式）、habit（习惯）、other（其他）；\n"
    "5. 没有可提取的内容时输出空数组 []；\n"
    "6. 只输出 JSON 数组，不要解释、不要 Markdown 代码块标记。\n"
    "输出格式示例：\n"
    '[{"text": "用户是一名中学教师", "category": "identity"}]\n\n'
    "[对话记录]\n"
)


def extraction_prompt(transcript: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """
    [EN]
    Build the extraction prompt from a transcript snapshot.

    A transcript longer than ``max_chars`` keeps its tail, because the end of a
    conversation usually contains the facts the user considered important
    enough to state.

    [ZH]
    由对话文本快照构造提取提示词。

    超过 ``max_chars`` 的文本保留尾部：对话末尾通常包含用户认为重要、值得说出
    的事实。
    """
    return _EXTRACTION_INSTRUCTION + _tail(transcript, max_chars)


def extraction_instruction() -> str:
    """
    [EN]
    The extraction instruction on its own, without the transcript. Used when a
    temporary conversation object carries the instruction as its custom prompt
    and the transcript is supplied as the question instead.

    [ZH]
    不带对话记录的提取指令本身。当临时会话对象把该指令作为自定义提示词、而把
    对话记录作为提问传入时使用。
    """
    return _EXTRACTION_INSTRUCTION


def _tail(transcript: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """
    [EN]
    Trim a transcript snapshot to its tail, keeping the most recent statements.

    [ZH]
    截取对话快照的尾部，保留最近的陈述。
    """
    body = (transcript or "").strip()
    if max_chars and len(body) > max_chars:
        body = "……（较早内容已截断）\n" + body[-max_chars:]
    return body


def parse_extraction(raw: str, max_items: int = 20) -> List[Dict[str, str]]:
    """
    [EN]
    Parse the model reply into validated memory candidates.

    Tolerances, each observed in practice with small local models: a fenced code
    block around the JSON, prose before or after the array, a top-level object
    wrapping the array under ``memories``/``items``, and category values outside
    the permitted set (mapped to ``other``).

    [ZH]
    将模型回复解析为经过校验的记忆候选。

    以下容错均来自本地小模型的实际输出：JSON 外层带代码块、数组前后带说明文字、
    数组被包在 ``memories``/``items`` 字段下、类别取值超出允许集合（映射为
    ``other``）。
    """
    text = (raw or "").strip()
    if not text:
        return []
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    payload: Any = None
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start:end + 1])
        except ValueError:
            payload = None
    if payload is None:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                wrapper = json.loads(text[start:end + 1])
            except ValueError:
                wrapper = None
            if isinstance(wrapper, dict):
                for key in ("memories", "items", "facts", "data"):
                    if isinstance(wrapper.get(key), list):
                        payload = wrapper[key]
                        break
    if not isinstance(payload, list):
        return []
    parsed: List[Dict[str, str]] = []
    seen: set = set()
    for entry in payload:
        if isinstance(entry, str):
            body, category = entry, "other"
        elif isinstance(entry, dict):
            body = str(entry.get("text") or entry.get("fact") or "").strip()
            category = str(entry.get("category") or "other").strip().lower()
        else:
            continue
        body = body.strip().strip("「」\"'")
        if not body or len(body) > 200:
            continue
        normalized = _normalize(body)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parsed.append({"text": body,
                       "category": category if category in CATEGORIES else "other"})
        if len(parsed) >= max_items:
            break
    return parsed


def extract_memories(complete: Callable[[List[Dict[str, str]]], str],
                     transcript: str, max_items: int = 20,
                     max_chars: int = MAX_TRANSCRIPT_CHARS,
                     conversation: Any = None) -> Dict[str, Any]:
    """
    [EN]
    Run one extraction pass and return the outcome without raising.

    ``complete`` receives a message list and returns the model's reply; the
    caller owns the model invocation, so this module stays testable with a stub
    and never imports the engine.

    ``conversation`` is an optional **temporary** conversation object. When it
    is supplied, the extraction instruction becomes its custom prompt and the
    transcript is sent as the question, exactly as the segmented summarizer
    routes its chunk prompts through a private conversation: the main
    conversation history must never take part. When it is omitted, the
    instruction and transcript are sent as a single user message.

    The result is ``{"ok": bool, "items": [...], "error": str, "raw": str}``.
    ``ok`` is false when the call itself failed; a call that succeeded but
    produced no parseable fact returns ``ok`` true with an empty list, because
    "nothing worth remembering" is a normal outcome.

    [ZH]
    执行一次提取并返回结果，不抛出异常。

    ``complete`` 接收消息列表并返回模型回复；模型调用由调用方持有，因此本模块既
    可用桩函数测试，也无需导入推理引擎。

    ``conversation`` 是可选的**临时**会话对象。传入时，提取指令成为它的自定义
    提示词、对话记录作为提问发出，与分段摘要把分段提示词交给私有会话处理完全
    一致：主对话历史绝不参与。省略时，指令与对话记录合并为单条用户消息。

    结果为 ``{"ok": bool, "items": [...], "error": str, "raw": str}``。
    调用本身失败时 ``ok`` 为假；调用成功但未解析出事实时 ``ok`` 为真且列表为空 ——
    “没有值得记住的内容”是正常结果。
    """
    try:
        if conversation is not None:
            conversation.set_custom_prompt(_EXTRACTION_INSTRUCTION)
            messages = conversation.build_messages(_tail(transcript, max_chars))
        else:
            messages = [{"role": "user",
                         "content": extraction_prompt(transcript, max_chars=max_chars)}]
        raw = complete(messages)
    except Exception as exc:  # noqa: BLE001 - [EN] extraction must never break the caller
                              # [ZH] 提取绝不得中断调用方
        return {"ok": False, "items": [], "error": f"{type(exc).__name__}: {exc}",
                "raw": ""}
    items = parse_extraction(str(raw or ""), max_items=max_items)
    return {"ok": True, "items": items, "error": "", "raw": str(raw or "")}
