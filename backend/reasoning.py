"""
[EN]
Separation of reasoning content from the final answer.

The separation is implemented at the application layer because the
``llama-cpp-python`` binding does not expose structured reasoning output:

* ``create_chat_completion`` accepts a fixed parameter set and exposes neither
  ``reasoning_format`` nor ``chat_template_kwargs`` / ``enable_thinking``.
* ``llama_types.py`` defines no ``reasoning_content`` field.
* Streaming deltas contain only ``content`` and ``role``; they never contain
  ``reasoning_content``.

Consequently, the inference layer cannot route reasoning into a separate field,
and model output carries reasoning tags inside the same ``content`` stream. This
module splits that stream into reasoning and answer channels at the application
boundary.

Observed tag forms:

* Qwen3 templates may emit no prefix when ``enable_thinking`` is undefined; the
  model generates both ``<think>`` and ``</think>`` tags in its output.
* DeepSeek-R1 distilled templates may hard-code ``<think>\\n`` in the
  ``add_generation_prompt`` branch. The opening tag is then part of the prompt
  and only the closing tag appears in generated text. A closing tag without an
  opening tag must therefore be treated as "all preceding content is
  reasoning"; otherwise the entire reasoning block would be exposed as answer.
* Tag names differ across models (``think``, ``thinking``, ``reasoning``,
  ``thought``, ``reflection``, and special DeepSeek token forms). The tag table
  is configurable and covers the common forms by default.

Public interfaces:

* :class:`ReasoningSplitter`: incremental splitter that consumes streaming
  fragments and emits ``(channel, text)`` events for :data:`REASONING`,
  :data:`ANSWER`, and :data:`RESET`. It handles tags split across chunks,
  nesting, unclosed tags, and orphaned closing tags.
* :func:`split_text`: one-shot split used by non-streaming paths and history
  replay.
* :func:`normalize_tags`: normalizes the configured tag table.

[ZH]
思考内容与最终回答的分离。

分离逻辑位于应用层，因为 ``llama-cpp-python`` 绑定不提供结构化的思考输出：

* ``create_chat_completion`` 的参数集固定，不提供 ``reasoning_format``、
  ``chat_template_kwargs`` 或 ``enable_thinking``。
* ``llama_types.py`` 未定义 ``reasoning_content`` 字段。
* 流式增量仅包含 ``content`` 与 ``role``，不会包含 ``reasoning_content``。

因此推理层无法将思考内容路由到独立字段，模型输出会在同一个 ``content`` 流中
携带思考标签。本模块在应用边界将该流拆分为思考通道与回答通道。

已观察到的标签形式：

* Qwen3 模板在 ``enable_thinking`` 未定义时可能不添加前缀，由模型在输出中
  生成 ``<think>`` 与 ``</think>`` 两个标签。
* DeepSeek-R1 蒸馏版模板可能在 ``add_generation_prompt`` 分支中硬编码
  ``<think>\\n``。此时起始标签属于提示词，生成文本中只有结束标签。因此，
  只有结束标签而没有起始标签时，必须将此前全部内容视为思考；否则整段思考
  会作为回答暴露。
* 不同模型的标签名不同（``think``、``thinking``、``reasoning``、``thought``、
  ``reflection`` 以及 DeepSeek 特殊 token 形式）。标签表可配置，默认覆盖上述
  常见形式。

公开接口：

* :class:`ReasoningSplitter`：增量拆分器，消费流式片段并输出
  ``(通道, 文本)`` 事件，通道取值为 :data:`REASONING`、:data:`ANSWER` 与
  :data:`RESET`。它处理跨分片标签、嵌套、未闭合标签与孤立结束标签。
* :func:`split_text`：一次性拆分，供非流式路径与历史回填使用。
* :func:`normalize_tags`：规范化配置中的标签表。
"""
from typing import Iterable, List, Optional, Sequence, Tuple

#: [EN] Channel name for reasoning content.
#: [ZH] 思考内容通道名。
REASONING = "reasoning"
#: [EN] Channel name for the final answer.
#: [ZH] 最终回答通道名。
ANSWER = "answer"
#: [EN] Control event indicating that content already emitted on the answer
#: [EN] channel belongs to reasoning and the answer channel must be discarded.
#: [ZH] 控制事件：此前已在回答通道发出的内容属于思考，回答通道必须作废。
RESET = "reset"

#: [EN] Event type: ``(channel, text)``.
#: [ZH] 事件类型：``(通道, 文本)``。
Event = Tuple[str, str]

# ---- [EN] Tag table construction / [ZH] 标签表构造 ----
# [EN] Closing-tag literals are not written directly in source. DeepSeek's
# [EN] end-of-reasoning marker shares its token form with special tokens in some
# [EN] toolchains and can be altered during copying or transport. Constructing
# [EN] tags by concatenation avoids ambiguity and keeps opening/closing pairs
# [EN] explicit.
# [ZH] 不在源码中直接书写闭合标签字面量。DeepSeek 的思考结束标记与部分工具链
# [ZH] 中的特殊 token 形式相同，直接书写可能在复制或传输过程中被替换。通过
# [ZH] 拼接构造标签可避免歧义，并使开标签与闭标签的配对关系显式可见。
_LT = "<"
_GT = ">"
_SLASH = "/"
_BAR = "\uff5c"          # ｜
_MARK = "\u2581"         # ▁（SentencePiece 的词首空格标记）

#: [EN] Default tag names. Each name produces a ``<name>`` / ``</name>`` pair.
#: [ZH] 默认识别的标签名。每个名称生成 ``<名字>`` / ``</名字>`` 一对标签。
DEFAULT_TAG_NAMES: Tuple[str, ...] = (
    "think", "thinking", "reasoning", "thought", "reflection", "analysis",
)

#: [EN] Native DeepSeek reasoning-token forms. Some GGUF models decode them as
#: [EN] ordinary text.
#: [ZH] DeepSeek 原生思考 token 形式。部分 GGUF 模型会将其解码为普通文本。
_DEEPSEEK_BEGIN = _BAR + "begin" + _MARK + "of" + _MARK + "thinking" + _BAR
_DEEPSEEK_END = _BAR + "end" + _MARK + "of" + _MARK + "thinking" + _BAR
_UNDERSCORE_THINKING = _MARK + "thinking"

#: [EN] Additional tag pairs beyond the ASCII tag forms.
#: [ZH] ASCII 标签形式之外的额外标签对。
EXTRA_TAG_PAIRS: Tuple[Tuple[str, str], ...] = (
    (_DEEPSEEK_BEGIN, _DEEPSEEK_END),
    (_UNDERSCORE_THINKING, _DEEPSEEK_END),
)


def _ascii_pair(name: str) -> Tuple[str, str]:
    """
    [EN]
    Generate a ``<name>`` / ``</name>`` tag pair from a tag name.

    [ZH]
    根据标签名生成 ``<名字>`` / ``</名字>`` 一对标签。
    """
    return (_LT + name + _GT, _LT + _SLASH + name + _GT)


def tag_pair(name: str) -> Tuple[str, str]:
    """
    [EN]
    Public form of :func:`_ascii_pair`: return a tag pair for a tag name. Tests
    and custom configurations both use this entry point.

    Callers therefore do not need to write closing-tag literals in source. Some
    toolchains treat those strings as special tokens; hard-coded literals are
    both less readable and easier to alter inadvertently.

    [ZH]
    :func:`_ascii_pair` 的公开形式：按标签名返回一对标签。测试与自定义配置均
    使用该入口。

    调用方因此无需在源码中书写闭合标签字面量。部分工具链会将这些字符串视为
    特殊 token；硬编码字面量既降低可读性，也更容易被意外修改。
    """
    return _ascii_pair(name)


def default_tags() -> List[Tuple[str, str]]:
    """
    [EN]
    Return the default tag table. Order defines match priority; longer tags
    precede shorter ones to prevent truncation.

    [ZH]
    返回默认标签表。顺序即匹配优先级；较长标签排在前面，避免被较短标签截断。
    """
    pairs = [_ascii_pair(name) for name in DEFAULT_TAG_NAMES]
    pairs.extend(EXTRA_TAG_PAIRS)
    # [EN] Longer tags first: ``<thinking>`` must match before ``<think>``,
    # [EN] otherwise the remaining "ing>" would leak into the text.
    # [ZH] 长标签优先：``<thinking>`` 必须先于 ``<think>`` 匹配，否则剩余的
    # [ZH] "ing>" 会泄漏到正文中。
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def normalize_tags(extra: Optional[Iterable] = None) -> List[Tuple[str, str]]:
    """
    [EN]
    Normalize the tag table by combining the default table with additional tag
    pairs from configuration.

    Configuration form (``config.json``, ``reasoning.tags``)::

        "tags": [["<thinking>", "</thinking>"], ["[[", "]]"]]

    Additional pairs are appended, never used to replace the defaults: adding a
    custom tag must not discard the built-in common forms. Invalid entries
    (values that are not two-element string pairs) are ignored so that a typo
    cannot disable the splitter entirely.

    [ZH]
    将默认标签表与配置中的额外标签对合并，生成规范化标签表。

    配置形式（``config.json`` 的 ``reasoning.tags``）：:

        "tags": [["<thinking>", "</thinking>"], ["[[", "]]"]]

    额外标签对仅追加，不替换默认值：添加自定义标签不得丢弃内置常见形式。
    非法项（不是两元素字符串对的值）会被忽略，避免一个笔误使拆分器整体失效。
    """
    pairs = default_tags()
    for item in extra or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        open_tag, close_tag = item
        if not isinstance(open_tag, str) or not isinstance(close_tag, str):
            continue
        if not open_tag or not close_tag:
            continue
        pairs.append((open_tag, close_tag))
    # [EN] Deduplicate, then sort by length again; configured tags may be longer
    # [EN] than the built-in ones.
    # [ZH] 去重后重新按长度排序；配置的标签可能长于内置标签。
    seen = set()
    unique: List[Tuple[str, str]] = []
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(pair)
    return sorted(unique, key=lambda pair: -len(pair[0]))


class ReasoningSplitter:
    """
    [EN]
    Split a text stream into reasoning and answer channels using configured
    tags.

    Streaming usage::

        splitter = ReasoningSplitter()
        for piece in model_stream:
            for channel, text in splitter.feed(piece):
                ...
        for channel, text in splitter.close():
            ...

    Design requirements:

    * Incremental safety: any trailing text that could be a tag prefix is held
      back. A tag split across chunks (``<thi`` + ``nk>``) is not misclassified,
      and a partial tag is never exposed as answer text.
    * Orphaned closing tag: if a closing tag appears without an opening tag,
      the opening tag was part of the prompt. All content previously emitted on
      the answer channel is reclassified as reasoning and a :data:`RESET` event
      instructs the caller to discard the answer region.
    * Unclosed tag: if the stream ends inside a tag, the remaining content is
      classified as reasoning and :attr:`unclosed` is set. This usually means
      the token budget was exhausted during reasoning; the interface can report
      this instead of presenting an empty answer.
    * Nesting: an opening tag inside reasoning increases nesting depth without
      changing the channel. Closing tags are matched by depth so that an inner
      tag cannot close the reasoning region prematurely.
    * Multiple segments: reasoning blocks in
      ``<think>a</think>answer<think>b</think>`` are merged for display.

    [ZH]
    使用配置的标签将文本流拆分为思考通道与回答通道。

    流式用法：:

        splitter = ReasoningSplitter()
        for piece in model_stream:
            for channel, text in splitter.feed(piece):
                ...
        for channel, text in splitter.close():
            ...

    设计要求：

    * 增量安全：任何可能是标签前缀的尾部文本都会被暂扣。跨分片的标签
      （``<thi`` + ``nk>``）不会被误判，半截标签也不会作为回答文本暴露。
    * 孤立结束标签：若结束标签出现而没有起始标签，说明起始标签属于提示词。
      此前已在回答通道发出的全部内容将改判为思考，并通过 :data:`RESET` 事件
      通知调用方作废回答区域。
    * 未闭合标签：若流在标签内部结束，剩余内容归类为思考并设置
      :attr:`unclosed`。这通常表示 token 预算在思考阶段耗尽；界面可据此提示，
      而不是展示空回答。
    * 嵌套：思考区域内再次出现起始标签只增加嵌套深度，不改变通道。结束标签
      按深度配对，避免内层标签提前关闭思考区域。
    * 多段：``<think>a</think>answer<think>b</think>`` 中的多个思考块合并显示。
    """

    def __init__(self, tags: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        self._tags: List[Tuple[str, str]] = list(tags) if tags is not None else default_tags()
        # [EN] All opening and closing tags in descending length order, used for
        # [EN] earliest-match and longest-prefix decisions.
        # [ZH] 全部开标签与闭标签按长度倒序排列，用于最早匹配与最长前缀判断。
        self._opens = sorted({pair[0] for pair in self._tags}, key=len, reverse=True)
        self._closes = sorted({pair[1] for pair in self._tags}, key=len, reverse=True)
        self._open_set = set(self._opens)
        self._close_set = set(self._closes)
        self._all = sorted(self._opens + self._closes, key=len, reverse=True)
        self._max_tag_len = max((len(tag) for tag in self._all), default=0)

        # [EN] Held-back suffix that may contain a partial tag.
        # [ZH] 暂扣的尾部文本，可能包含半个标签。
        self._buffer = ""
        self._reasoning: List[str] = []
        self._answer: List[str] = []
        self._in_reasoning = False
        self._depth = 0
        # [EN] Content emitted on the answer channel since the last RESET.
        # [ZH] 自上次 RESET 以来在回答通道发出的内容。
        self._answer_pending = ""
        self.saw_reasoning = False
        self.unclosed = False
        self.reclassified = False

    # ---------- [EN] Results / [ZH] 结果 ----------

    @property
    def reasoning_text(self) -> str:
        """
        [EN]
        Return the full text accumulated on the reasoning channel.

        [ZH]
        返回已归入思考通道的完整文本。
        """
        return "".join(self._reasoning)

    @property
    def answer_text(self) -> str:
        """
        [EN]
        Return the full text accumulated on the answer channel.

        [ZH]
        返回已归入回答通道的完整文本。
        """
        return "".join(self._answer)

    @property
    def in_reasoning(self) -> bool:
        """
        [EN]
        Return whether the splitter is currently inside a reasoning tag. A true
        value after closing indicates an unclosed tag.

        [ZH]
        返回拆分器当前是否仍在思考标签内。关闭后仍为真表示标签未闭合。
        """
        return self._in_reasoning

    def has_reasoning(self) -> bool:
        """
        [EN]
        Return whether non-whitespace reasoning content was observed. The
        interface uses this to decide whether to show the collapsible region.

        [ZH]
        返回是否观察到非空白的思考内容。界面据此决定是否显示折叠区域。
        """
        return bool(self.reasoning_text.strip())

    # ---------- [EN] Incremental input / [ZH] 增量输入 ----------

    def feed(self, text: str) -> List[Event]:
        """
        [EN]
        Feed an incremental text fragment and return the events that can be
        emitted immediately.

        [ZH]
        输入增量文本片段，并返回当前可立即发出的事件。
        """
        if not text:
            return []
        self._buffer += text
        events: List[Event] = []
        while True:
            hit = self._find_earliest_tag(self._buffer)
            if hit is None:
                keep = self._partial_suffix_len(self._buffer)
                cut = len(self._buffer) - keep
                if cut > 0:
                    events.extend(self._emit(self._buffer[:cut]))
                    self._buffer = self._buffer[cut:]
                return events
            index, tag, is_open = hit
            if index > 0:
                events.extend(self._emit(self._buffer[:index]))
            self._buffer = self._buffer[index + len(tag):]
            events.extend(self._handle_tag(is_open))

    def close(self) -> List[Event]:
        """
        [EN]
        Finalize the stream: release held-back content and treat an unclosed tag
        as reasoning for the remainder of the stream.

        [ZH]
        结束流处理：释放暂扣内容，并将未闭合标签之后的剩余内容按思考处理。
        """
        events: List[Event] = []
        if self._buffer:
            if self._in_reasoning:
                self.unclosed = True
            events.extend(self._emit(self._buffer))
            self._buffer = ""
        if self._in_reasoning:
            # [EN] The tag never closed: the remaining content remains reasoning
            # [EN] and the answer region stays empty.
            # [ZH] 标签始终未闭合：剩余内容仍属于思考，回答区域保持为空。
            self.unclosed = True
            self._in_reasoning = False
            self._depth = 0
        return events

    # ---------- [EN] Internal: splitting / [ZH] 内部：切分 ----------

    def _find_earliest_tag(self, text: str) -> Optional[Tuple[int, str, bool]]:
        """
        [EN]
        Find the earliest tag in ``text`` and return
        ``(position, tag, is_opening_tag)``.

        [ZH]
        查找 ``text`` 中最早出现的标签，返回
        ``(位置, 标签, 是否为起始标签)``。
        """
        best: Optional[Tuple[int, str, bool]] = None
        for tag in self._all:
            index = text.find(tag)
            if index < 0:
                continue
            if best is None or index < best[0] or (index == best[0] and len(tag) > len(best[1])):
                # [EN] If the same string is both an opening and a closing tag,
                # [EN] treat it as closing. The orphaned-closing-tag branch
                # [EN] prevents reasoning from leaking into the answer region.
                # [ZH] 若同一字符串既是开标签又是闭标签，则按闭标签处理。孤立
                # [ZH] 闭标签分支可防止思考内容泄漏到回答区域。
                is_open = tag in self._open_set and tag not in self._close_set
                best = (index, tag, is_open)
        return best

    def _partial_suffix_len(self, text: str) -> int:
        """
        [EN]
        Return the length of the suffix that could be the beginning of a tag and
        must therefore be held back.

        Only genuine tag prefixes are retained, so normal text is delayed by at
        most one tag length. Chinese text is rarely affected because it seldom
        ends with ``<``.

        [ZH]
        返回可能是标签开头的尾部长度；该部分必须暂扣。

        仅保留真正的标签前缀，因此普通文本最多被延迟一个标签长度。中文文本
        很少受影响，因为其结尾通常不是 ``<``。
        """
        limit = min(len(text), max(0, self._max_tag_len - 1))
        for size in range(limit, 0, -1):
            suffix = text[-size:]
            if any(tag.startswith(suffix) for tag in self._all):
                return size
        return 0

    def _emit(self, text: str) -> List[Event]:
        """
        [EN]
        Emit a text segment that is known not to be part of a tag to the current
        channel.

        [ZH]
        将确定不属于标签的文本片段发送到当前通道。
        """
        if not text:
            return []
        if self._in_reasoning:
            # [EN] Reasoning often begins with a newline immediately after the
            # [EN] opening tag; strip it so the expanded region does not start
            # [EN] with a blank line.
            # [ZH] 思考内容常在起始标签后紧跟一个换行；将其去除，避免展开区域
            # [ZH] 以空行开头。
            chunk = text.lstrip() if not self._reasoning else text
            if not chunk:
                return []
            self._reasoning.append(chunk)
            return [(REASONING, chunk)]
        events: List[Event] = []
        # [EN] The answer region may start with a blank line after the closing
        # [EN] tag; strip it to avoid leading whitespace above the answer.
        # [ZH] 回答区域可能在结束标签后以空行开头；去除该空行以避免答案上方留白。
        chunk = text
        if not self._answer:
            chunk = chunk.lstrip()
        if chunk:
            self._answer.append(chunk)
            self._answer_pending += chunk
            events.append((ANSWER, chunk))
        return events

    def _handle_tag(self, is_open: bool) -> List[Event]:
        """
        [EN]
        Process one complete tag.

        [ZH]
        处理一个完整标签。
        """
        if is_open:
            if not self._in_reasoning:
                self._enter_reasoning()
            else:
                self._depth += 1
            return []
        # [EN] Closing tag.
        # [ZH] 结束标签。
        if self._in_reasoning:
            self._depth -= 1
            if self._depth <= 0:
                self._in_reasoning = False
                self._depth = 0
            return []
        # [EN] A closing tag arrived without an opening tag: the opening tag was
        # [EN] part of the prompt.
        # [ZH] 结束标签出现但没有起始标签：起始标签属于提示词。
        if self._answer_pending:
            moved = self._answer_pending
            self._reasoning.append(moved)
            self._answer.clear()
            self._answer_pending = ""
            self.reclassified = True
            self.saw_reasoning = True
            # [EN] Emit only RESET. The text was already sent on the answer
            # [EN] channel and must be moved to the reasoning channel, not sent
            # [EN] again. ``reasoning_text`` already contains it for the final
            # [EN] done event.
            # [ZH] 仅发出 RESET。该文本已在回答通道发送，必须移动到思考通道，
            # [ZH] 而不能重复发送。``reasoning_text`` 已包含该文本，供最终
            # [ZH] done 事件使用。
            return [(RESET, moved)]
        # [EN] No content needs reclassification; this may be an empty reasoning
        # [EN] block closed by the prompt.
        # [ZH] 无需改判任何内容；这可能是提示词预先关闭的空思考块。
        self.saw_reasoning = True
        return []

    def _enter_reasoning(self) -> None:
        self._in_reasoning = True
        self._depth = 1
        self.saw_reasoning = True
        # [EN] Insert a newline between reasoning segments so that adjacent
        # [EN] segments do not run together.
        # [ZH] 在多个思考片段之间插入换行，避免相邻片段粘连。
        if self._reasoning and not self._reasoning[-1].endswith("\n"):
            self._reasoning.append("\n")


def split_text(text: str, tags: Optional[Sequence[Tuple[str, str]]] = None) -> Tuple[str, str]:
    """
    [EN]
    Split a complete text in one pass and return
    ``(reasoning_content, answer_content)``.

    This path is used by non-streaming calls, history replay, and export. Both
    sides are stripped: blank lines outside tags have no display value in a
    non-streaming result, whereas the streaming path must preserve them for
    incremental rendering.

    [ZH]
    一次性拆分完整文本，返回 ``(思考内容, 回答内容)``。

    该路径用于非流式调用、历史回填与导出。两侧均执行 ``strip``：在非流式结果
    中，标签外的空行没有展示价值；流式路径则必须保留这些空行以支持增量渲染。
    """
    splitter = ReasoningSplitter(tags)
    splitter.feed(text or "")
    splitter.close()
    return splitter.reasoning_text.strip(), splitter.answer_text.strip()


def has_reasoning_tag(text: str, tags: Optional[Sequence[Tuple[str, str]]] = None) -> bool:
    """
    [EN]
    Return whether the text contains any reasoning tag. This is a fast check for
    detecting reasoning-bearing output.

    [ZH]
    返回文本中是否包含任一思考标签，用于快速判断输出是否包含思考内容。
    """
    if not text:
        return False
    pairs = list(tags) if tags is not None else default_tags()
    for open_tag, close_tag in pairs:
        if open_tag in text or close_tag in text:
            return True
    return False


def strip_reasoning(text: str, tags: Optional[Sequence[Tuple[str, str]]] = None) -> str:
    """
    [EN]
    Return only the answer portion, for callers that do not consume reasoning
    content (for example, summarization and automation).

    [ZH]
    仅返回回答部分，供不使用思考内容的调用方（例如摘要与自动化）使用。
    """
    return split_text(text, tags)[1]
