"""思考内容与最终回答的分离（v0.2.1 模型思考内容分离 任务三）。

为什么必须在这一层做（任务二已确认推理层给不了）：

* 本项目通过 `llama-cpp-python` 调用 llama.cpp。实测（探针一~四，见
  ``records/v0.2.1-模型思考内容分离记录.md``）该绑定：
    - `create_chat_completion` 的参数表是**固定的 29 个**，既没有
      `reasoning_format`，也没有 `chat_template_kwargs` / `enable_thinking`；
    - `llama_types.py` 里根本没有 `reasoning_content` 字段；
    - 流式 delta 只会出现 ``content``/``role` 两个键，**永远不会**出现
      `reasoning_content`。
  也就是说 llama.cpp 服务端的 `--reasoning-format` 那一套在 Python 绑定里
  不可用，推理层无法把思考内容分流到独立字段。
* 于是模型输出是"标签包裹"形式，思考标签和答案一起躺在 `content` 里。
  本模块就是应用层的兜底：把同一条文本流按标签切成"思考"与"回答"两条通道。

标签形态（实测，不是猜的）：

* Qwen3 系列（`Qwen3-0.6B`）模板在 `enable_thinking` 未定义时**不加任何前缀**，
  由模型自己生成 ``<think>`` … ``</think>`` —— 起始、结束标签都在生成文本里；
* DeepSeek-R1 蒸馏版（`unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF`）的模板在
  `add_generation_prompt` 分支里直接写死了 ``<think>\\n``，**起始标签在提示词里**，
  生成文本只有结束标签 —— 所以"没有起始标签、只有结束标签"必须当作
  "整段都是思考"处理，否则 R1 的思考内容会全部落进回答区（正是本次要修的 bug）；
* 不同模型的标签名不一样（`think` / `thinking` / `reasoning` / `thought` /
  `reflection`，以及 DeepSeek 的特殊 token 形式），所以标签表是**可配置**的，
  默认覆盖上述常见形态。

对外接口：

* :class:`ReasoningSplitter` —— 增量分离器，喂入流式片段，吐出
  ``(通道, 文本)`` 事件，通道取值为 :data:`REASONING` / :data:`ANSWER` /
  :data:`RESET`；负责处理标签跨 chunk、嵌套、未闭合、孤立结束标签等边界。
* :func:`split_text` —— 一次性切分（非流式路径与历史记录回填用）。
* :func:`normalize_tags` —— 把配置里的标签表整理成内部形式。
"""
from typing import Iterable, List, Optional, Sequence, Tuple

#: 通道名：思考内容
REASONING = "reasoning"
#: 通道名：最终回答
ANSWER = "answer"
#: 特殊事件：此前已经在回答通道发出去的内容其实属于思考，请把回答区作废
RESET = "reset"

#: 事件类型：``(通道, 文本)``
Event = Tuple[str, str]

# ---- 标签表的构造 ----
#
# 刻意不写"闭合标签"的字面量：DeepSeek 的思考结束标记与某些工具链里的特殊
# token 同名，直接在源码里写容易在复制/传输中被替换掉。统一用拼接生成，
# 既避免歧义，也让"开标签/闭标签成对"这件事在代码里一眼可见。
_LT = "<"
_GT = ">"
_SLASH = "/"
_BAR = "\uff5c"          # ｜
_MARK = "\u2581"         # ▁（SentencePiece 的词首空格标记）

#: 默认识别的标签名（成对生成 ``<名字>`` / ``</名字>``）
DEFAULT_TAG_NAMES: Tuple[str, ...] = (
    "think", "thinking", "reasoning", "thought", "reflection", "analysis",
)

#: DeepSeek 原生思考 token 形式（部分 GGUF 会把它解码成文本）
_DEEPSEEK_BEGIN = _BAR + "begin" + _MARK + "of" + _MARK + "thinking" + _BAR
_DEEPSEEK_END = _BAR + "end" + _MARK + "of" + _MARK + "thinking" + _BAR
_UNDERSCORE_THINKING = _MARK + "thinking"

#: 除 ASCII 标签外的额外标签对
EXTRA_TAG_PAIRS: Tuple[Tuple[str, str], ...] = (
    (_DEEPSEEK_BEGIN, _DEEPSEEK_END),
    (_UNDERSCORE_THINKING, _DEEPSEEK_END),
)


def _ascii_pair(name: str) -> Tuple[str, str]:
    """由标签名生成 ``<名字>`` / ``</名字>`` 一对。"""
    return (_LT + name + _GT, _LT + _SLASH + name + _GT)


def tag_pair(name: str) -> Tuple[str, str]:
    """公开版 :func:`_ascii_pair`：按标签名取一对标签（测试与自定义配置都用它）。

    这样调用方不必在源码里写闭合标签字面量——那串字符在一些工具链里会被当成
    特殊 token 处理，写死在源码里既不可读也容易被悄悄改掉。
    """
    return _ascii_pair(name)


def default_tags() -> List[Tuple[str, str]]:
    """默认标签表（顺序即匹配优先级，长标签在前避免被短标签截断）。"""
    pairs = [_ascii_pair(name) for name in DEFAULT_TAG_NAMES]
    pairs.extend(EXTRA_TAG_PAIRS)
    # 长标签优先：``<thinking>`` 必须比 ``<think>`` 先匹配，否则会剩下 "ing>"
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def normalize_tags(extra: Optional[Iterable] = None) -> List[Tuple[str, str]]:
    """整理标签表：默认表 + 配置里追加的额外标签对。

    配置形式（``config.json`` 的 ``reasoning.tags``）：:

        "tags": [["<thinking>", "</thinking>"], ["[[", "]]"]]

    只做"追加"而不是"替换"：用户加自定义标签时不该把内置的常见形态弄丢。
    非法项（不是长度 2 的字符串对）直接忽略，避免一个笔误让分离整体失效。
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
    # 去重后重新按长度排序（配置项也可能比内置项长）
    seen = set()
    unique: List[Tuple[str, str]] = []
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(pair)
    return sorted(unique, key=lambda pair: -len(pair[0]))


class ReasoningSplitter:
    """把一条文本流按标签切成"思考 / 回答"两条通道。

    用法（流式）::

        splitter = ReasoningSplitter()
        for piece in model_stream:
            for channel, text in splitter.feed(piece):
                ...
        for channel, text in splitter.close():
            ...

    设计要点：

    * **增量安全**：任何可能是标签前缀的尾巴都会被扣住不发出，所以
      ``<thi`` + ``nk>`` 这种跨 chunk 的标签不会漏判，也不会把半截标签
      当成回答内容显示出去。
    * **孤立结束标签**：先看到结束标签、没看到起始标签时，说明起始标签在
      提示词里（R1 就是这样），此时把**此前发到回答通道的内容整体改判为思考**，
      并通过 :data:`RESET` 事件通知调用方把回答区作废。
    * **未闭合**：整条流结束时仍在标签内，则剩余内容全部归思考，并置
      :attr:`unclosed` —— 这通常意味着模型把 token 预算耗在思考上、
      还没来得及给答案，界面据此给出提示而不是留一个空回答区。
    * **嵌套**：标签内再出现起始标签只加深嵌套，不改变通道；结束标签按
      嵌套深度配对，避免内层标签提前关掉思考区。
    * **多段**：``<think>a</think>答案<think>b</think>`` 的两段思考会合并展示。
    """

    def __init__(self, tags: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        self._tags: List[Tuple[str, str]] = list(tags) if tags is not None else default_tags()
        # 所有标签（开、闭）按长度倒序，用于"最早出现"与"最长前缀"两个判断
        self._opens = sorted({pair[0] for pair in self._tags}, key=len, reverse=True)
        self._closes = sorted({pair[1] for pair in self._tags}, key=len, reverse=True)
        self._open_set = set(self._opens)
        self._close_set = set(self._closes)
        self._all = sorted(self._opens + self._closes, key=len, reverse=True)
        self._max_tag_len = max((len(tag) for tag in self._all), default=0)

        self._buffer = ""            # 扣住的尾巴（可能是半个标签）
        self._reasoning: List[str] = []
        self._answer: List[str] = []
        self._in_reasoning = False
        self._depth = 0
        self._answer_pending = ""    # 上一次 RESET 之后发到回答通道的内容
        self.saw_reasoning = False
        self.unclosed = False
        self.reclassified = False

    # ---------- 结果 ----------

    @property
    def reasoning_text(self) -> str:
        """已归入思考通道的全文。"""
        return "".join(self._reasoning)

    @property
    def answer_text(self) -> str:
        """已归入回答通道的全文。"""
        return "".join(self._answer)

    @property
    def in_reasoning(self) -> bool:
        """当前是否仍在思考标签内（结束时为真说明标签没闭合）。"""
        return self._in_reasoning

    def has_reasoning(self) -> bool:
        """是否真的出现过思考内容（全空白不算，界面据此决定要不要显示折叠区）。"""
        return bool(self.reasoning_text.strip())

    # ---------- 增量喂入 ----------

    def feed(self, text: str) -> List[Event]:
        """喂入一段增量文本，返回本次可发出的事件列表。"""
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
        """结束：把扣住的内容放出来，未闭合的标签按"整段是思考"收尾。"""
        events: List[Event] = []
        if self._buffer:
            if self._in_reasoning:
                self.unclosed = True
            events.extend(self._emit(self._buffer))
            self._buffer = ""
        if self._in_reasoning:
            # 标签一直没闭合：剩下的内容仍然算思考，回答区保持为空
            self.unclosed = True
            self._in_reasoning = False
            self._depth = 0
        return events

    # ---------- 内部：切分 ----------

    def _find_earliest_tag(self, text: str) -> Optional[Tuple[int, str, bool]]:
        """找出 $text$ 中最早出现的标签，返回 ``(位置, 标签, 是否起始标签)``。"""
        best: Optional[Tuple[int, str, bool]] = None
        for tag in self._all:
            index = text.find(tag)
            if index < 0:
                continue
            if best is None or index < best[0] or (index == best[0] and len(tag) > len(best[1])):
                # 同一个字符串既是开标签又是闭标签时按"闭标签"处理：
                # 孤立闭标签有专门的分支兜底，判错也不会把思考漏进回答区。
                is_open = tag in self._open_set and tag not in self._close_set
                best = (index, tag, is_open)
        return best

    def _partial_suffix_len(self, text: str) -> int:
        """$text$ 末尾有多长可能是某个标签的开头（必须扣住不发）。

        只保留"某个标签的真前缀"，所以正常文本最多被延迟一个标签长度；
        中文文本几乎不受影响（不会以 ``<`` 结尾）。
        """
        limit = min(len(text), max(0, self._max_tag_len - 1))
        for size in range(limit, 0, -1):
            suffix = text[-size:]
            if any(tag.startswith(suffix) for tag in self._all):
                return size
        return 0

    def _emit(self, text: str) -> List[Event]:
        """把一段确定不属于标签的文本发到当前通道。"""
        if not text:
            return []
        if self._in_reasoning:
            # 思考区开头紧跟在起始标签之后，模型通常先吐一个换行：去掉它，
            # 否则折叠区一展开就是一行空白（实测 Qwen3 的输出就是 "\n好的，…"）
            chunk = text.lstrip() if not self._reasoning else text
            if not chunk:
                return []
            self._reasoning.append(chunk)
            return [(REASONING, chunk)]
        events: List[Event] = []
        # 回答区开头可能出现结束标签之后的空行，去掉以免答案上方留白
        chunk = text
        if not self._answer:
            chunk = chunk.lstrip()
        if chunk:
            self._answer.append(chunk)
            self._answer_pending += chunk
            events.append((ANSWER, chunk))
        return events

    def _handle_tag(self, is_open: bool) -> List[Event]:
        """处理一个完整标签。"""
        if is_open:
            if not self._in_reasoning:
                self._enter_reasoning()
            else:
                self._depth += 1
            return []
        # 结束标签
        if self._in_reasoning:
            self._depth -= 1
            if self._depth <= 0:
                self._in_reasoning = False
                self._depth = 0
            return []
        # 没有起始标签就来了结束标签：起始标签在提示词里（DeepSeek-R1 这类）
        if self._answer_pending:
            moved = self._answer_pending
            self._reasoning.append(moved)
            self._answer.clear()
            self._answer_pending = ""
            self.reclassified = True
            self.saw_reasoning = True
            # 只发 RESET：这段文本已经在回答通道发出去过了，调用方需要把它
            # **搬**到思考通道（而不是再发一遍，否则会重复）。搬完之后
            # ``reasoning_text`` 里也已经包含了它，供 done 行使用。
            return [(RESET, moved)]
        # 完全没有内容可改判：可能是提示词预先关掉的空思考块
        self.saw_reasoning = True
        return []

    def _enter_reasoning(self) -> None:
        self._in_reasoning = True
        self._depth = 1
        self.saw_reasoning = True
        # 多段思考之间补一个换行，避免上一段末尾与下一段开头粘在一起
        if self._reasoning and not self._reasoning[-1].endswith("\n"):
            self._reasoning.append("\n")


def split_text(text: str, tags: Optional[Sequence[Tuple[str, str]]] = None) -> Tuple[str, str]:
    """一次性切分整段文本，返回 ``(思考内容, 回答内容)``。

    用于非流式路径、历史记录回填与导出。两侧都做 ``strip``：非流式路径上
    "标签外的空行"没有任何展示价值，而流式路径不能这么做（会影响实时渲染）。
    """
    splitter = ReasoningSplitter(tags)
    splitter.feed(text or "")
    splitter.close()
    return splitter.reasoning_text.strip(), splitter.answer_text.strip()


def has_reasoning_tag(text: str, tags: Optional[Sequence[Tuple[str, str]]] = None) -> bool:
    """文本里是否出现任一思考标签（用于快速判断"这像不像带思考的输出"）。"""
    if not text:
        return False
    pairs = list(tags) if tags is not None else default_tags()
    for open_tag, close_tag in pairs:
        if open_tag in text or close_tag in text:
            return True
    return False


def strip_reasoning(text: str, tags: Optional[Sequence[Tuple[str, str]]] = None) -> str:
    """只取回答部分（供不关心思考内容的调用方使用，如摘要、自动化）。"""
    return split_text(text, tags)[1]
