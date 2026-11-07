"""
[EN]
EchoDesk backend configuration management.

Supports "base configuration + user patch override" (fields in config.user.json take
precedence), modelled on the profile patch mechanism of DeepSeek-Harness: the
application only writes the base configuration back, and manual user patches remain
untouched.

[ZH]
EchoDesk 后端配置管理。

支持"基础配置 + 用户补丁覆盖"（config.user.json 中的字段优先），
仿 DeepSeek-Harness 的 profile patch 机制：应用只写回基础配置，
用户手工补丁保持不变。
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from i18n import t as _t

# [EN] Hard lower bound for the context window (below this, normal conversation is
# [EN] not possible).
# [ZH] 上下文窗口的硬性下限（低于此值无法正常对话）。
MIN_CONTEXT = 512

#: [EN] Sanity ceiling for an explicit user override of the context window. The
#: planner never silently narrows an override, so the configuration layer alone
#: is responsible for rejecting values a 64-bit integer or llama.cpp could not
#: survive. Values below this bound are adopted verbatim and fail at load time
#: with a structured error when the machine cannot serve them.
#: [ZH] 用户显式覆盖上下文窗口时的合理性上限。规划器不会静默收窄覆盖值，因此由
#: 配置层单独负责拒绝 64 位整数或 llama.cpp 无法承受的取值。低于该上限的值会被
#: 原样采纳，并在机器无法承载时于加载阶段以结构化错误失败。
MAX_CONTEXT_OVERRIDE = 4_194_304


def program_dir() -> str:
    """
    [EN]
    Directory containing the program itself (the directory of the executable when
    packaged; ``...\\app`` under the portable layout).

    [ZH]
    程序本体所在目录（打包后即 exe 所在目录；便携版布局下为 ``...\\app``）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


#: [EN] Names of the "container directories" in the release layout: these directories
#: [EN] only hold the program; user data must not be placed inside them.
#: [EN]
#: [EN] Why this list is required (front-end/back-end inconsistency found during the
#: [EN] v0.2.2 bilingual and three-platform compatibility test run): the original
#: [EN] implementation only checked whether the parent directory was named "app",
#: [EN] which computed the wrong root under the standalone packaging layout used on
#: [EN] Linux and macOS:
#: [EN]
#: [EN]   Windows (PyInstaller onedir): <release>\app\backend\EchoDeskBackend.exe
#: [EN]     directory of config.py = <release>\app\backend, parent <release>\app is
#: [EN]     named app -> returns <release>  (correct)
#: [EN]   Linux/macOS (standalone):   <release>/app/backend/app/main.py
#: [EN]     directory of config.py = <release>/app/backend/app, parent is named
#: [EN]     backend (not app) -> returns <release>/app/backend  (incorrect)
#: [EN]
#: [EN] The front end (Avalonia UserPaths) computed <release> instead, so within one
#: [EN] release the front end wrote snippets.json to <release>/ while the back end
#: [EN] wrote session.json to <release>/app/backend/: two different data directories.
#: [EN] The fix is to walk upward past container directories so that both layouts
#: [EN] yield the same answer.
#: [ZH] 发布布局里的"容器目录"名：这些目录只是用来装程序的，用户数据不应落在其中。
#: [ZH]
#: [ZH] 需要该列表的原因（v0.2.2 全面双语与三平台友好验证发现的前后端不一致缺陷）：
#: [ZH] 原实现只判断父目录是否名为 app，在 Linux/macOS 的 standalone 打包布局下会
#: [ZH] 计算错误：
#: [ZH]
#: [ZH]   Windows（PyInstaller onedir）：<发布>\app\backend\EchoDeskBackend.exe
#: [ZH]     config.py 所在目录 = <发布>\app\backend，父目录 <发布>\app 名为 app →
#: [ZH]     返回 <发布>（正确）
#: [ZH]   Linux/macOS（standalone）：<发布>/app/backend/app/main.py
#: [ZH]     config.py 所在目录 = <发布>/app/backend/app，父目录名为 backend（不是
#: [ZH]     app）→ 返回 <发布>/app/backend（错误）
#: [ZH]
#: [ZH] 而前端（Avalonia 的 UserPaths）算出的是 <发布>，于是同一个发布物里前端把
#: [ZH] snippets.json 写到 <发布>/、后端把 session.json 写到 <发布>/app/backend/，
#: [ZH] 形成两个数据目录。修法是逐级向上跳过容器目录，让两种布局得到同一答案。
CONTAINER_DIRS = frozenset({"app", "backend"})


def user_data_root() -> str:
    """
    [EN]
    User data root (fix 7; corrected for the standalone layout in v0.2.2).

    The release uses a portable three-level structure:
    ``EchoDesk-v0.2.2\\app\\backend\\...``, where ``app\\`` holds the program itself
    and only the outer level is visible to the user and suitable for backup
    (shortcuts, ``ui_state.json``, ``snippets.json``, ``results\\``).

    Resolution order (kept consistent with the front-end ``UserPaths.DataRoot`` and
    ``ShortcutService.UserDataDirectory``):
    1. the ``ECHODESK_DATA`` environment variable;
    2. walk upward from the backend directory, skipping directory names listed in
       :data:`CONTAINER_DIRS` (``app`` / ``backend``); the first ancestor not in that
       set is the user data root;
    3. all three real layouts resolve to the same location:
       * installed/portable (Windows): ``<release>\\app\\backend`` -> ``<release>``;
       * standalone (Linux/macOS): ``<release>/app/backend/app`` -> ``<release>``;
       * source checkout: ``<repo>/backend`` -> ``<repo>`` (unchanged from before the
         fix, so developer machine behaviour is unaffected).

    [ZH]
    用户数据根目录（修复七；v0.2.2 修掉 standalone 布局下算错的问题）。

    发布物采用便携版三层结构：``EchoDesk-v0.2.2\\app\\backend\\…``，
    其中 ``app\\`` 是程序本体，外层才是用户能看到、能备份的目录
    （快捷方式、``ui_state.json``、``snippets.json``、``results\\``）。

    判定顺序（与前端 ``UserPaths.DataRoot`` / ``ShortcutService.UserDataDirectory`` 保持一致）：
    1. 环境变量 ``ECHODESK_DATA``；
    2. 从后端目录逐级向上跳过 :data:`CONTAINER_DIRS` 里的目录名（``app`` / ``backend``），
       第一个不在其中的祖先就是用户数据根；
    3. 三种真实布局都落在同一处：
       * 安装/便携（Windows）：``<发布>\\app\\backend`` → ``<发布>``；
       * standalone（Linux/macOS）：``<发布>/app/backend/app`` → ``<发布>``；
       * 源码检出：``<仓库>/backend`` → ``<仓库>``（与修复前一致，不改变开发机行为）。
    """
    override = (os.environ.get("ECHODESK_DATA") or "").strip()
    if override:
        try:
            return os.path.abspath(os.path.expanduser(override))
        except Exception:  # noqa: BLE001 - [EN] fall back to automatic detection when the environment variable is invalid
                           # [ZH] 环境变量异常时退回自动判定
            pass
    if getattr(sys, "frozen", False):
        current = os.path.dirname(os.path.abspath(sys.executable))
    else:
        current = os.path.dirname(os.path.abspath(__file__))
    while os.path.basename(current).lower() in CONTAINER_DIRS:
        parent = os.path.dirname(current)
        if parent == current:   # [EN] Already at the filesystem root; cannot go higher.
                                # [ZH] 已经到文件系统根，不能再往上。
            break
        current = parent
    return current


def program_root() -> str:
    """
    [EN]
    Program root directory (addition 3: default location of results).

    The function name is retained for compatibility with existing callers; the
    implementation delegates to :func:`user_data_root`, so a results directory with
    an empty save location lands in the outer portable directory instead of being
    buried inside ``app\\``.

    [ZH]
    程序根目录（新增三：results 的默认位置）。

    保留函数名以兼容既有调用方；实现改为走 :func:`user_data_root`，
    这样"保存位置留空"的结果目录会落在便携版外层，而不是埋进 ``app\\``。
    """
    return user_data_root()

# [EN] Training context windows of known models (file-name keyword -> tokens).
# [EN] Used to size the window from the selected model without loading it; the value
# [EN] is calibrated at runtime from the GGUF metadata.
# [ZH] 已知模型的训练上下文窗口（文件名关键词 → tokens）。
# [ZH] 用于在无需加载模型时也能按"所选模型"决定窗口大小；运行时还会用 GGUF 元数据校准。
DEFAULT_MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "qwen3.5-0.8b": 32768,
    "qwen3-0.6b": 32768,
    "qwen3": 40960,
    "qwen2.5": 32768,
    "llama-3": 8192,
    "llama3": 8192,
    "gemma": 8192,
    "phi-3": 4096,
}


class FilesConfig(BaseModel):
    """
    [EN]
    Multi-file upload (v0.2.1 known-issue optimization 2, task 8).

    The task specification lists two points that required an explicit decision:
    warning the user and asking whether to continue when there are too many files or
    the total size is too large, and triggering segmented summaries or asking the
    user to select a subset when the total content exceeds the window. The chosen
    values are:

    * ``max_count`` defaults to 10: dragging ten files at once is a common office
      scenario (a batch of contracts, a set of attachments); beyond that it is no
      longer "load once" but "bulk import", which should use a different entry point;
    * ``total_chars`` defaults to 60000: this is the total length limit for
      injection into the context (roughly 30k tokens; actual injection still passes
      through the window budget). On overflow, the most recently uploaded files are
      loaded first and the files that were not injected are reported truthfully,
      because the files just dragged in are usually the ones needed for this task;
    * ``disk_threshold_chars``: a single file larger than this has its body persisted
      to disk, with only a pointer and summary in the context, so that one oversized
      file cannot blow up both the session and the context (the same rationale as
      "handle large blocks separately" in task 9).

    [ZH]
    多文件上传（v0.2.1 优化已知问题-2 任务八）。

    任务书把"文件过多或总大小过大时提示用户并询问是否继续""总内容超出窗口时
    触发分段摘要，或提示用户选择部分文件"都列为需要明确的点。这里的取值：

    * ``max_count`` 默认 10：一次拖十个文件是常见办公场景（一批合同、一套附件），
      再多就不是"一次加载"而是"批量导入"，应该用别的入口；
    * ``total_chars`` 默认 60000：这是注入上下文的总长上限（约 3 万 token 量级，
      实际注入还要过窗口预算），超出时从后上传的开始装并如实提示未注入的文件
      —— 刚拖进来的通常是这次要用的；
    * ``disk_threshold_chars``：单文件超过它就把正文落盘、上下文里只放指针与摘要，
      避免一份超大文件把会话与上下文一起撑爆（与任务九的"大块内容单独处理"同源）。
    """

    max_count: int = Field(default=10, ge=1, le=100)
    total_chars: int = Field(default=60000, ge=0, le=2000000)
    disk_threshold_chars: int = Field(default=20000, ge=0, le=2000000)


class CompactionConfig(BaseModel):
    """
    [EN]
    Context compaction policy (v0.2.1 known-issue optimization 2, task 9).

    Three things were changed against industry practice (Anthropic / ChatGPT /
    Gemini CLI / OpenHands):

    1. Trigger point: changed from a hardcoded 80% to a configurable ratio of the
       token budget (0.70 by default). 0.70 rather than 80% because the estimate is
       inherently approximate (a crude "characters / 2" when no model is loaded) and
       room must remain for the next question and answer; Gemini CLI uses 50%, Claude
       triggers once near the window limit, and 0.70 is a configurable middle value.
    2. Grace zone: the most recent N turns (one question plus one answer counts as
       one turn) are preserved verbatim and only earlier content is compacted. The
       previous implementation kept roughly 20% by token budget, so a single
       over-long message could compact the just-finished conversation and retain
       older content, whereas the most recent turns are precisely the most relevant.
    3. The summary prompt is customizable: ``prompt`` can replace the entire policy
       and ``focus`` can append a focus point
       (``/compact focus on contract amount``).

    Compaction events are appended to ``log_file`` (JSONL), so they can be traced,
    audited, and replayed.

    [ZH]
    上下文压缩策略（v0.2.1 优化已知问题-2 任务九）。

    对照业界做法（Anthropic / ChatGPT / Gemini CLI / OpenHands）改掉三件事：

    1. 触发时机：从"硬编码 80%"改为按 token 比例可配（默认 0.70）。
       选 0.70 而不是 80%：估算本身有误差（无模型时是"字符/2"的粗估），
       而且压缩完还要留出下一轮提问与回答的空间；Gemini CLI 用的是 50%，
       Claude 用的是窗口上限附近的一次性触发，0.70 是可配的中间值。
    2. grace zone：最近 N 轮（一问一答算一轮）逐字保留，只压更早的内容。
       之前的实现只按 token 预算保留"约 20%"，遇到一条超长消息时可能把
       刚发生的对话压掉、留下更早的内容 —— 而"最近几轮"恰恰是最相关的。
    3. 摘要提示词可自定义：``prompt`` 可换整套口径，``focus`` 可追加重点
       （``/compact focus on 合同金额``）。

    压缩事件写入 ``log_file``（JSONL，追加式），可追溯、可审计、可回放。
    """

    # [EN] Trigger ratio: compaction runs when history tokens exceed n_ctx * this
    # [EN] factor (0.05-0.95).
    # [ZH] 触发比例：历史 token 数超过 n_ctx × 该值就压缩（0.05~0.95）
    trigger_ratio: float = Field(default=0.70, ge=0.05, le=0.95)
    # [EN] Number of turns preserved verbatim (grace zone); 1 turn = user + assistant
    # [EN] messages.
    # [ZH] 逐字保留的轮数（grace zone）；1 轮 = user + assistant 两条消息
    grace_turns: int = Field(default=3, ge=0, le=50)
    # [EN] Lower bound for tokens preserved verbatim (combined with grace_turns: the
    # [EN] larger of the two wins).
    # [ZH] 逐字保留的 token 下限（与 grace_turns 取并集：两者谁保留得多听谁的）
    keep_ratio: float = Field(default=0.20, ge=0.0, le=0.8)
    # [EN] Summary prompt (fully replaceable; {focus} is the insertion point for the
    # [EN] user-specified focus).
    # [ZH] 摘要提示词（可整套替换；{focus} 是用户指定重点的插入点）
    prompt: str = (
        "你是对话压缩器。请用不超过 150 字的要点总结这段对话，"
        "必须保留：① 用户的需求与目标；② 已经做出的关键决定；③ 后续还要用到的背景信息"
        "（人名、金额、日期、编号、路径、参数）。不要复述客套话，不要编造。"
    )
    # [EN] Upper bound on summary output tokens.
    # [ZH] 摘要输出的 token 上限
    summary_max_tokens: int = Field(default=200, ge=64, le=1024)
    # [EN] Truncation length per message fed to the compactor (a single over-long
    # [EN] message must not consume the compactor's whole window).
    # [ZH] 单条消息喂给压缩器时的截断长度（避免一条超长消息吃光压缩器的窗口）
    message_chars: int = Field(default=300, ge=50, le=4000)
    # [EN] Total input length limit for the compactor.
    # [ZH] 喂给压缩器的总长上限
    input_chars: int = Field(default=4000, ge=200, le=20000)
    # [EN] Large outputs from tools, files, and search are truncated separately
    # [EN] before entering the history (task requirement); 0 = no truncation (can be
    # [EN] disabled when documents are short and the context is ample).
    # [ZH] 工具/文件/搜索这类大块输出在进历史之前就单独截断（设计规范要求）；
    # [ZH] 0 = 不截断（文档很短、上下文很宽裕时可以关掉）
    tool_output_chars: int = Field(default=2000, ge=0, le=20000)
    # [EN] Compaction event log (JSONL, append-only); an empty string disables
    # [EN] logging.
    # [ZH] 压缩事件日志（JSONL，追加式）；空字符串表示不记录
    log_file: str = "compact_log.jsonl"
    # [EN] Whether the log records summary bodies (false = lengths and counts only,
    # [EN] suitable for classified environments).
    # [ZH] 日志里是否记录摘要正文（false = 只记长度与计数，适合涉密环境）
    log_content: bool = True
    # [EN] Maximum number of events retained in the log (oldest ones are trimmed).
    # [ZH] 日志最多保留多少条事件（超出裁剪最早的）
    log_max_events: int = Field(default=5000, ge=100, le=200000)

    @field_validator("prompt")
    @classmethod
    def _check_prompt(cls, value: str) -> str:
        """
        [EN]
        Fall back to the default when the prompt is empty (a manually corrupted
        configuration file must not produce an empty summary).

        [ZH]
        提示词为空时回落到默认值（配置文件被手改坏也不至于压出空摘要）。
        """
        text = str(value or "").strip()
        return text or CompactionConfig.model_fields["prompt"].default


class SummarizeConfig(BaseModel):
    """
    [EN]
    Long-document segmented summarization (preprocessing) configuration.

    When the full text of a file exceeds a certain proportion of the window, summaries
    are generated segment by segment and merged before injection, rather than
    truncating the document directly.

    v0.2.1 general optimization (task 4) adds progress feedback, heartbeats, timeouts,
    and interruption. With a small model on CPU only, summarizing a long document can
    take over ten minutes; previously only "segment i of N" was printed to stderr, so
    the front-end conversation view showed nothing and the user could not distinguish
    "running" from "hung". The parameters below decide how long silence counts as
    abnormal and what to do when a single segment is too slow.

    [ZH]
    长文档分段摘要（预处理）配置。

    文件全文超出窗口一定比例时，先分段生成摘要再合并注入，而不是直接截断。

    v0.2.1 综合优化（任务四）新增：过程反馈、心跳、超时与中断。
    小模型 + 纯 CPU 下长文档摘要可能跑十几分钟，此前只把"第 i/N 段"打到 stderr，
    前端对话框里什么都看不到，用户无法区分"正在跑"与"已经卡死"。
    下面这组参数决定"多久没动静算异常"以及"单段太慢怎么办"。
    """

    enabled: bool = True
    # [EN] Segmented summarization triggers when the file's token share of the window
    # [EN] exceeds this value.
    # [ZH] 文件 token 占窗口比例超过该值即触发分段摘要
    trigger_ratio: float = Field(default=0.5, ge=0.05, le=0.95)
    # [EN] Segment length and overlap in characters (overlap prevents splitting
    # [EN] through semantic context).
    # [ZH] 单段字符数与段间重叠字符数（重叠避免切断上下文语义）
    chunk_chars: int = Field(default=3000, ge=500, le=20000)
    overlap_chars: int = Field(default=200, ge=0, le=2000)
    # [EN] Length limits for a segment summary and the final merged summary (tokens).
    # [ZH] 单段摘要与最终合并摘要的长度上限（tokens）
    max_summary_tokens: int = Field(default=400, ge=64, le=2048)
    merge_max_tokens: int = Field(default=800, ge=128, le=4096)
    # [EN] Dedicated summarization model path; empty means reuse the current chat
    # [EN] model.
    # [ZH] 专用摘要模型路径；留空表示复用当前对话模型
    model: str = ""
    # [EN] Below this character count no summary is generated; the full text is
    # [EN] injected directly.
    # [ZH] 低于该字符数不触发摘要，直接注入全文
    min_chars: int = Field(default=4000, ge=0)
    # [EN] Keep source pointers in the summary (file name plus paragraph range) so the
    # [EN] original text can be looked up on demand.
    # [ZH] 摘要中保留原文指针（文件名 + 段落区间），便于按需回查原文
    keep_pointer: bool = True

    # ---------- [EN] Task 4: progress feedback ----------
    # ---------- [ZH] 相关设计：过程反馈 ----------
    # [EN] Whether to emit progress events (summary_progress) to the front end.
    # [EN] Disabling this only affects visibility, not the summary result; unattended or
    # [EN] scripted callers can disable it to reduce protocol noise.
    # [ZH] 是否向前端发进度事件（summary_progress）。关掉只影响"看不看得见"，不影响摘要结果；
    # [ZH] 无人值守/脚本化调用时可以关掉以减少协议噪音。
    progress_events: bool = True
    # [EN] Heartbeat interval in seconds: a status is emitted at least this often while
    # [EN] a segment is being processed, preventing long periods without any output.
    # [EN] The 1-second lower bound prevents a value of 0 from flooding the protocol.
    # [ZH] 心跳间隔（秒）：单段处理中每隔这么久至少发一次状态，避免长时间无任何输出。
    # [ZH] 下限 1 秒是防止有人配成 0 把协议刷爆。
    heartbeat_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    # [EN] If a single segment remains incomplete for longer than this, the progress
    # [EN] entry is marked "abnormally slow" and a hint is emitted (hint only, no
    # [EN] interruption: slowness is not failure, and the task specification requires
    # [EN] distinguishing the two).
    # [ZH] 单段超过这么多秒仍未完成，就在进度里标记为"异常缓慢"并给出提示
    # [ZH] （只提示、不打断：慢不等于故障，设计规范要求区分这两种情况）。
    slow_chunk_seconds: int = Field(default=60, ge=10, le=3600)
    # [EN] Per-segment timeout in seconds; 0 = unlimited. The decision is based on
    # [EN] wall-clock time elapsed since the segment started.
    # [ZH] 单段超时（秒）；0 = 不限。判定依据是"该段从开始到现在"的墙钟时间。
    chunk_timeout_seconds: int = Field(default=0, ge=0, le=7200)
    # [EN] Behaviour after a per-segment timeout: skip (skip the segment and continue,
    # [EN] default) / retry (retry once) / abort (abort the entire summary).
    # [ZH] 单段超时后的行为：skip（跳过该段并继续，默认）/ retry（重试一次）/ abort（中止整个摘要）
    chunk_timeout_action: str = "skip"

    # ---------- [EN] Task 4: extractive pre-filtering (此前版本 known-issue optimization 2) ----------
    # ---------- [ZH] 相关设计：抽取式预筛（此前版本 优化已知问题-2） ----------
    # [EN] Before sending to the model, TextRank compresses each segment to roughly
    # [EN] extractive_ratio. It is purely statistical and model-free; a 3000-character
    # [EN] segment takes about 8 ms (see artifacts/probe_extractive.py).
    # [ZH] 送给模型之前，先用 TextRank 把每一段压到 extractive_ratio 左右。
    # [ZH] 纯统计、不用模型，验证 3000 字符的段约 8 ms（见 artifacts/probe_extractive.py）。
    extractive: bool = True
    # [EN] Extraction ratio. Smaller is not better: at 0.18 key facts (contract amount,
    # [EN] dates) were largely lost in testing, whereas 0.35 clearly improved retention
    # [EN] while still achieving a 2.8x compression ratio. See records/ for the measured
    # [EN] results behind this field.
    # [ZH] 抽取比例。不是越小越好：0.18 时验证关键事实（合同金额、日期）大量丢失，
    # [ZH] 0.35 时保留率明显提升而压缩比仍有 2.8 倍。详见 records/ 与本字段的验证记录。
    extractive_ratio: float = Field(default=0.35, ge=0.10, le=0.70)
    # [EN] Segments below this character count skip pre-filtering (the segment is
    # [EN] already short enough that the savings do not justify the risk of losing
    # [EN] information).
    # [ZH] 低于该字符数的段不做预筛（段本身够短，抽省的时抵不上丢信息的风险）
    extractive_min_chars: int = Field(default=600, ge=0, le=20000)

    @field_validator("chunk_timeout_action")
    @classmethod
    def _check_timeout_action(cls, value: str) -> str:
        """
        [EN]
        Validate the timeout behaviour; an invalid value falls back to skip (the
        configuration file may have been corrupted by hand).

        [ZH]
        校验超时行为，非法值回落到默认的 skip（配置文件可能被手改坏）。
        """
        text = str(value or "").strip().lower()
        return text if text in ("skip", "retry", "abort") else "skip"


class AutomationConfig(BaseModel):
    """
    [EN]
    Browser automation configuration (task 4).

    Safety invariant: ``enabled`` may only be true when ``safety_accepted`` is true
    (enforced in the _check_safety validator); the agreement version and acceptance
    time are recorded for audit purposes.

    [ZH]
    浏览器自动化配置（任务四）。

    安全约定：``enabled`` 只有在 ``safety_accepted`` 为真时才允许为真
    （在 _check_safety 校验器里强制），并且记录协议版本与同意时间以便审计。
    """

    enabled: bool = False
    # ---------- [EN] Safety agreement ----------
    # ---------- [ZH] 安全协议 ----------
    safety_accepted: bool = False
    safety_version: str = "1.0"
    safety_accepted_at: str = ""
    # ---------- [EN] Task list page ----------
    # ---------- [ZH] 任务列表页 ----------
    task_list_url: str = ""
    task_list_selector: str = ""      # [EN] CSS or XPath (treated as XPath when it starts with //, (, or xpath=).
                                      # [ZH] CSS 或 XPath（以 //、(、xpath= 开头判为 XPath）。
    task_row_selector: str = ""       # [EN] Optional: row container selector.
                                      # [ZH] 可选：行容器选择器。
    next_page_selector: str = ""      # [EN] Optional: next-page button.
                                      # [ZH] 可选：下一页按钮。
    # ---------- [EN] Task detail page ----------
    # ---------- [ZH] 任务详情页 ----------
    task_detail_file_selector: str = ""
    file_extensions: List[str] = Field(
        default_factory=lambda: ["pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "txt"]
    )
    # ---------- [EN] Task instruction and generation ----------
    # ---------- [ZH] 任务指令与生成 ----------
    instruction: str = "摘取合同的主要信息并生成财务审核意见"
    llm_temperature: float = Field(default=0.3, ge=0.0, le=1.0)
    llm_max_tokens: int = Field(default=512, ge=64, le=4096)
    # ---------- [EN] Runtime parameters ----------
    # ---------- [ZH] 运行参数 ----------
    max_tasks: int = Field(default=20, ge=1, le=500)
    page_timeout_ms: int = Field(default=30000, ge=3000, le=300000)
    wait_network_idle: bool = True
    retries: int = Field(default=1, ge=0, le=5)
    download_dir: str = ""            # [EN] Empty = echodesk_downloads under the system temp directory.
                                      # [ZH] 空 = 系统临时目录下 echodesk_downloads。
    # ---------- [EN] Deduplication and login state ----------
    # ---------- [ZH] 去重与登录态 ----------
    dedupe: bool = True
    processed_file: str = "automation_processed.json"
    # [EN] Login state (此前版本 automation optimization, task 1): the primary mechanism
    # [EN] is now Playwright's persistent user data directory
    # [EN] (``launch_persistent_context``); cookies, localStorage, and sessions are
    # [EN] written by the browser itself into the directory referenced by
    # [EN] ``login_state_dir`` and reused automatically on the next launch.
    # [EN]
    # [EN] ``login_state_file`` therefore degrades to a storage_state file used for
    # [EN] migration and backup: on first use of a new directory, if it exists, its
    # [EN] cookies are imported once (existing users do not need to log in again); it is
    # [EN] still exported once at the end of every session so the user can back it up or
    # [EN] move to another machine. Both files stay local; nothing is uploaded.
    # [ZH] 登录态（此前版本 自动化优化 相关设计）：主机制改为 Playwright 的持久化用户数据目录
    # [ZH] （``launch_persistent_context``），Cookie / localStorage / 会话都由浏览器自己写进
    # [ZH] ``login_state_dir`` 指向的目录，下次启动自动复用。
    # [ZH]
    # [ZH] ``login_state_file`` 因此退化为"迁移与备份用的 storage_state 文件"：首次使用新目录
    # [ZH] 时若它存在，会把里面的 Cookie 导入一次（老用户不用重新登录）；每次会话结束仍会
    # [ZH] 导出一次，便于用户备份或换机迁移。两个文件都只在本机，不含任何上传行为。
    login_state_file: str = "browser_state.json"
    login_state_dir: str = ""          # [EN] Empty = browser_profile directory alongside login_state_file.
                                       # [ZH] 空 = 与 login_state_file 同级的 browser_profile 目录。
    # [EN] Login detection: prefer an element that only appears after login (most
    # [EN] reliable); otherwise fall back to URL keywords.
    # [ZH] 登录态判定：优先用"登录后才出现的元素"（最可靠）；没配就用 URL 关键词兜底。
    login_check_selector: str = ""
    login_url_keywords: List[str] = Field(
        default_factory=lambda: ["login", "signin", "sign-in", "logon", "sso", "passport", "cas", "auth"]
    )
    manual_login_wait_seconds: int = Field(default=180, ge=0, le=3600)
    # ---------- [EN] Browser ----------
    # ---------- [ZH] 浏览器 ----------
    headless: bool = False            # [EN] Headed mode is fixed so the user can see the browser operations.
                                      # [ZH] 固定有头模式：用户可见浏览器操作过程。
    # [EN] Task 16 (self-test fix 1): user-selectable browser.
    # [EN]
    # [EN] Values: ``auto`` / ``edge`` / ``chrome`` / ``chromium`` / ``firefox``.
    # [EN]   * ``auto`` (default): try ``edge -> chrome -> chromium`` in order and use
    # [EN]     the first one that starts. Putting edge first is deliberate: msedge was
    # [EN]     the default before v0.2.2, so existing users see no behaviour change
    # [EN]     after upgrading, while chrome/chromium only add fallbacks, i.e. the
    # [EN]     change is strictly additive;
    # [EN]   * ``chromium``: the browser bundled with Playwright (no separate install
    # [EN]     needed);
    # [EN]   * ``firefox``: Playwright's Firefox. Note that CDP reuse (task 17) does
    # [EN]     not apply to Firefox; selecting it rules out the "take over an already
    # [EN]     running browser" path.
    # [EN]
    # [EN] Candidate definitions and ordering are in ``automation.BROWSER_CHOICES``
    # [EN] and ``automation.AUTO_ORDER``.
    # [ZH] 相关设计（自测问题修复-1）：用户可选的浏览器。
    # [ZH]
    # [ZH] 取值：``auto`` / ``edge`` / ``chrome`` / ``chromium`` / ``firefox``。
    # [ZH]   * ``auto``（默认）：按 ``edge → chrome → chromium`` 依次尝试，
    # [ZH]     任何一个能起来就用它。把 edge 放在第一位是刻意的 —— v0.2.2 之前
    # [ZH]     默认就是 msedge，这样老用户升级后行为不变，而 chrome/chromium
    # [ZH]     只是多了兜底，等于"只增不减"；
    # [ZH]   * ``chromium``：Playwright 自带的浏览器（无需用户另装）；
    # [ZH]   * ``firefox``：Playwright 的 Firefox。注意 CDP 复用（相关设计）
    # [ZH]     不适用于 Firefox，选了它就用不了"接管已打开的浏览器"这条路径。
    # [ZH]
    # [ZH] 具体候选与顺序见 ``automation.BROWSER_CHOICES`` / ``automation.AUTO_ORDER``。
    browser: str = "auto"
    # [EN] Legacy field kept for compatibility: when ``browser="auto"`` and this was
    # [EN] explicitly set by the user, the browser it points to is moved to the front
    # [EN] of the candidate list (respecting the existing user configuration).
    # [ZH] 旧字段，保留兼容：``browser="auto"`` 且这里被用户显式设置过时，
    # [ZH] 它指向的浏览器会被提到候选列表最前面（尊重用户既有配置）。
    browser_channel: str = "msedge"
    # [EN] Task 17 (self-test fix 1): policy for reusing an already running browser.
    # [EN]
    # [EN] Values:
    # [EN]   * ``auto`` (default): first try to attach to the user's running browser via
    # [EN]     CDP; on failure, fall back to "the program starts a new instance". The
    # [EN]     fallback being enabled by default is deliberate: the feature stays usable
    # [EN]     overall, and the fallback is clearly reported in the progress view and the
    # [EN]     interface as "this method requires logging in again";
    # [EN]   * ``required``: must attach to a running browser, otherwise fail with
    # [EN]     guidance (for users who prefer failure over logging in again in another
    # [EN]     instance);
    # [EN]   * ``off``: never attempt CDP and always use the legacy new-instance mode
    # [EN]     (for users who do not want the program to touch the browser they are
    # [EN]     using).
    # [EN]
    # [EN] Why this policy is configurable rather than hardcoded: requirement 5 of the
    # [EN] task specification delegates the degradation path to DSH, but the cost of
    # [EN] degradation (logging in once more) is a trade-off the user should make, so a
    # [EN] switch is provided instead of deciding on the user's behalf.
    # [ZH] 相关设计（自测问题修复-1）：复用已经打开的浏览器的策略。
    # [ZH]
    # [ZH] 取值：
    # [ZH]   * ``auto``（默认）：先尝试通过 CDP 连接用户已打开的浏览器；连不上就回退到
    # [ZH]     "程序新开一个实例"。默认开启回退是刻意的 —— 不让功能整体不可用，
    # [ZH]     但回退时会在进度与界面上明确告知"这种方式需要重新登录"；
    # [ZH]   * ``required``：必须连上已打开的浏览器，连不上就报错并给出引导
    # [ZH]     （用户宁可失败也不愿意在另一个实例里重新登录时选它）；
    # [ZH]   * ``off``：从不尝试 CDP，永远用老的新实例模式
    # [ZH]     （用户不希望程序碰他正在用的浏览器时选它）。
    # [ZH]
    # [ZH] 为什么把这个策略放在配置里而不是写死：设计规范第 5 条要求由 DSH 决定降级路径，
    # [ZH] 但"降级的代价"（重新登录一次）该由用户自己权衡，所以给一个开关而不是替他决定。
    browser_reuse: str = "auto"
    # [EN] CDP debugging port, used both when the program launches a "debug-mode
    # [EN] browser" and when connecting to a browser the user started. Only
    # [EN] 127.0.0.1 is ever contacted (see automation._cdp_endpoint): the debugging
    # [EN] endpoint grants full control of that browser and must never be exposed to the
    # [EN] local network.
    # [ZH] CDP 调试端口。程序代启动"调试模式浏览器"时用它，连接用户自己启动的浏览器时也用它。
    # [ZH] 只会连 127.0.0.1（见 automation._cdp_endpoint）：调试端点等于该浏览器的
    # [ZH] 完全控制权，绝不能暴露到局域网。
    cdp_port: int = 9222
    # ---------- [EN] Results and audit ----------
    # ---------- [ZH] 结果与审计 ----------
    save_results: bool = True
    # [EN] Results directory (addition 3): results root\YYYY-MM-DD\task-name\ (a
    # [EN] numeric suffix -1, -2 is appended on collision).
    # [EN] 此前版本 release specification, global requirement 1: directory names
    # [EN] generated by the program are always English, so the default of
    # [EN] results_root_name changed from the Chinese name to results and the default
    # [EN] of task_name changed to auto-task (the same applies to result file names;
    # [EN] see report_path in automation.py).
    # [ZH] 结果目录（新增三）：结果根目录\YYYY-MM-DD\任务名\（同名自动加 -1、-2）
    # [ZH] 此前版本 发布设计规范「全局要求一」：程序自动生成的目录名一律英文，
    # [ZH] 因此 results_root_name 的默认值由中文名称改为 results，
    # [ZH] task_name 的默认值改为 auto-task（结果文件名同理，
    # [ZH] 见 automation.py 的 report_path）。
    results_root_name: str = "results"       # [EN] Top-level folder name, user-customizable.
                                             # [ZH] 顶层文件夹名，可由用户自定义。
    results_location: str = ""               # [EN] Empty = program root directory; another location may be specified.
                                             # [ZH] 空 = 程序根目录；可指定其他位置。
    task_name: str = "auto-task"             # [EN] Third-level directory: name of this run's task.
                                             # [ZH] 第三层目录名：本次运行的任务名称。
    audit_log_file: str = "automation_log.jsonl"
    # ---------- [EN] Scheduled triggering (4.4) ----------
    # ---------- [ZH] 定时触发（4.4） ----------
    schedule_enabled: bool = False
    schedule_times: List[str] = Field(default_factory=lambda: ["09:00"])
    start_with_windows: bool = False

    @field_validator("file_extensions", "schedule_times", "login_url_keywords", mode="before")
    @classmethod
    def _split_list(cls, value):
        """
        [EN]
        The front-end form submits these items as comma-separated strings; they are
        split into lists here.

        [ZH]
        前端表单里这几项以逗号分隔的字符串提交，这里统一拆成列表。
        """
        if isinstance(value, str):
            items = [item.strip() for item in value.replace("，", ",").split(",")]
            return [item for item in items if item]
        return value

    @field_validator("file_extensions")
    @classmethod
    def _normalize_ext(cls, value: List[str]) -> List[str]:
        """
        [EN]
        Normalize extensions to lowercase without a leading dot, so comparisons are
        case-insensitive.

        [ZH]
        后缀统一为小写、去掉前导点，便于大小写不敏感比较。
        """
        return [str(item).strip().lstrip(".").lower() for item in value if str(item).strip()]

    @field_validator("login_url_keywords")
    @classmethod
    def _normalize_keywords(cls, value: List[str]) -> List[str]:
        """
        [EN]
        Normalize login-page URL keywords to lowercase and remove duplicates (URL
        matching itself is case-insensitive).

        [ZH]
        登录页 URL 关键词统一小写、去重（URL 匹配本身不区分大小写）。
        """
        out: List[str] = []
        for item in value:
            word = str(item).strip().lower()
            if word and word not in out:
                out.append(word)
        return out

    @field_validator("schedule_times")
    @classmethod
    def _normalize_times(cls, value: List[str]) -> List[str]:
        """
        [EN]
        Validate the HH:MM format, zero-pad the hour to two digits, and drop invalid
        entries so the scheduler cannot crash.

        [ZH]
        校验 HH:MM 格式并补零为两位小时，非法项直接剔除（避免定时器崩溃）。
        """
        import re as _re
        result = []
        for item in value:
            text = str(item).strip()
            match = _re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
            if match:
                result.append(f"{int(match.group(1)):02d}:{match.group(2)}")
        return result

    @model_validator(mode="after")
    def _check_safety(self) -> "AutomationConfig":
        """
        [EN]
        The safety agreement is a precondition for automation: the switch is forced
        off when it has not been accepted.

        [ZH]
        安全协议是自动化的前置条件：未勾选时强制关闭开关。
        """
        if self.enabled and not self.safety_accepted:
            print(
                "⚠️ 自动化开关需要先勾选安全协议，已自动关闭（automation.enabled=False）",
                file=sys.stderr, flush=True,
            )
            self.enabled = False
        if self.headless:
            # [EN] Task specification 4.5: only headed mode is supported so the user
            # [EN] can see the browser operations; this forces the correct value.
            # [ZH] 设计规范 4.5：仅使用有头模式，用户可见浏览器操作；这里强制纠正
            print("⚠️ 自动化仅支持有头模式，已忽略 headless=True", file=sys.stderr, flush=True)
            self.headless = False
        return self


class ModelHubConfig(BaseModel):
    """
    [EN]
    Model selection panel configuration (task specification, addition 4).

    The data source can be switched between auto / mirror / official; ``hf_token`` is
    only required for downloading gated models and should be placed in
    config.user.json (already ignored by .gitignore) rather than in the base
    configuration.

    [ZH]
    模型选择栏配置（任务书 新增四）。

    数据源可在「自动 / 镜像 / 官方」之间切换；``hf_token`` 仅在下载 Gated 模型时需要，
    建议放在 config.user.json（已被 .gitignore 忽略）而不是基础配置里。
    """

    enabled: bool = True
    # [EN] auto: prefer the mirror (usually faster in mainland China), falling back to
    # [EN] the official site when unreachable.
    # [ZH] auto：优先镜像站（国内通常更快），不可达时回退官方站。
    source: str = "auto"                    # [EN] auto | mirror | official
                                            # [ZH] auto | mirror | official
    hf_token: str = ""                      # [EN] Required for gated models; when empty the HF_TOKEN environment variable is used.
                                            # [ZH] Gated 模型所需，留空则读 HF_TOKEN 环境变量。
    models_dir: str = "models"              # [EN] Model repository directory (relative to the backend working directory).
                                            # [ZH] 模型库目录（相对后端工作目录）。
    registry_file: str = "models_registry.json"
    # [EN] Once the download is complete, compare sizes; if a sha256 is available and
    # [EN] the file is at most 2 GB, compare hashes as well.
    # [ZH] 下载完整后比对大小；拿得到 sha256 且文件 ≤2GB 时再比对哈希。
    verify_sha256: bool = True
    download_retries: int = Field(default=3, ge=1, le=10)
    search_limit: int = Field(default=20, ge=1, le=100)

    # ---------- [EN] 此前版本 model marketplace optimization ----------
    # ---------- [ZH] 此前版本 模型市场优化 ----------
    # [EN] Safety limits for loading all results automatically (task 2): any one of the
    # [EN] three gates stops the operation and reports "N entries loaded, more
    # [EN] available". This prevents runaway behaviour when a keyword is very broad
    # [EN] (an empty keyword means the entire model repository).
    # [ZH] 自动加载全部结果时的安全上限（相关设计）：三个闸门任一触发即停并告知
    # [ZH] "已加载 N 条，还有更多"。关键词过宽（如空关键词 = 整个模型库）时不至于失控。
    max_items: int = Field(default=5000, ge=20, le=20000)
    max_requests: int = Field(default=8, ge=1, le=50)
    max_seconds: float = Field(default=90.0, ge=5.0, le=600.0)
    # [EN] Local page size (task 3: default 20; the interface offers 20/50/100).
    # [ZH] 本地分页每页条数（相关设计：默认 20，界面可选 20/50/100）
    page_size: int = Field(default=20, ge=5, le=200)
    # [EN] On-disk search result cache (task 2): empty = <user data directory>/cache/
    # [EN] model_hub. 0 means every search fetches again (the cache expires
    # [EN] immediately).
    # [ZH] 搜索结果磁盘缓存（相关设计）：留空 = <用户数据目录>/cache/model_hub。
    # [ZH] 0 表示每次搜索都重新拉取（缓存立即过期）。
    search_cache_dir: str = ""
    search_cache_ttl_hours: int = Field(default=6, ge=0, le=168)


class ReasoningConfig(BaseModel):
    """
    [EN]
    Reasoning content separation configuration (v0.2.1 model reasoning separation).

    Why only these configuration items exist (decision rationale in
    ``records/v0.2.1-模型思考内容分离记录.md``):

    * ``enabled``: disabling it reverts to the legacy behaviour (reasoning tags
      remain in the answer). The switch exists so that "a one-step rollback to the
      previous behaviour" is possible if problems appear; it is not intended to
      encourage disabling;
    * ``persist``: whether reasoning content is written into the conversation
      history. Enabled by default: task 5 of the task specification requires
      "reload a historical session containing reasoning content and confirm the
      separation is correct", which is impossible without persistence;
    * ``history_max_chars``: retention limit per reasoning entry in the history.
      Reasoning is often much longer than the answer; without a limit, session.json
      would be inflated by a single long reasoning block. Content beyond the limit is
      truncated and marked with an ellipsis;
    * ``tags``: appends custom tag pairs (of the form
      ``[["[[think]]", "[[/think]]"]]``). The built-in set already covers common
      forms (see ``reasoning.DEFAULT_TAG_NAMES``); this only appends, so a typo
      cannot discard the built-in tags.

    [ZH]
    思考内容分离配置（v0.2.1 模型思考内容分离）。

    为什么配置项只有这几个（决策依据见
    ``records/v0.2.1-模型思考内容分离记录.md``）：

    * ``enabled`` —— 关掉后退回旧行为（思考标签原样留在回答里）。留这个开关是为了
      "出问题时能一键回到没改之前"，而不是鼓励关掉；
    * ``persist`` —— 思考内容是否写进会话历史。默认写：任务书任务五要求
      "重新加载含思考内容的历史会话，确认区分正确"，不持久化就没法满足；
    * ``history_max_chars`` —— 历史里每条思考内容的保留上限。思考往往比答案长得多，
      不设上限时 session.json 会被单个长思考撑大；超限的部分截断并加省略标记；
    * ``tags`` —— 追加自定义标签对（形如 ``[["[[think]]", "[[/think]]"]]``）。
      内置已覆盖常见形态（见 ``reasoning.DEFAULT_TAG_NAMES``），这里只做追加，
      避免一个笔误把内置标签一起弄丢。
    """

    enabled: bool = True
    persist: bool = True
    history_max_chars: int = Field(default=8000, ge=0, le=200000)
    tags: List[List[str]] = Field(default_factory=list)


class MarketplaceConfig(BaseModel):
    """
    [EN]
    Skill/plug-in marketplace configuration (task 5, item 5.6).

    [ZH]
    技能/插件市场配置（任务五 5.6）。
    """

    enabled: bool = True
    github_token: str = ""                                  # [EN] Optional: raises the GitHub API rate limit.
                                                            # [ZH] 可选：提高 GitHub API 限额。
    cache_ttl_hours: int = Field(default=6, ge=1, le=168)
    skill_topic: str = "echodesk-plugin"                    # [EN] Single unified topic; no other tags are mixed in.
                                                            # [ZH] 统一 topic，不混用其它标签。
    auto_update_check: bool = True
    cache_file: str = "market_cache.json"
    per_page: int = Field(default=20, ge=1, le=100)
    install_dir: str = "skills"
    registry_file: str = "skills_registry.json"
    api_base: str = "https://api.github.com"
    # ---------- [EN] Local plug-in interface (task 2, supplement 2B) ----------
    # ---------- [ZH] 本地插件接口（相关设计·补 2B） ----------
    # [EN] In-development plug-in directory: files placed here are discovered with more
    # [EN] lenient loading and are not written to the formal registry.
    # [ZH] 开发中的插件目录：放进去即被识别，加载更宽松，不写入正式注册表。
    dev_dir: str = "skills_dev"
    # [EN] Explicit local plug-in manifest (arbitrary paths, aliases, enabled state,
    # [EN] load order, and notes may be specified).
    # [ZH] 显式本地插件清单（可指定任意路径、别名、启用状态、加载顺序、备注）。
    local_manifest: str = "skills_local.json"
    # [EN] Developer mode: verbose logging plus hot reload.
    # [ZH] 开发模式：详细日志 + 热重载。
    dev_mode: bool = False
    # [EN] Fix 1: whether the developer-mode switch was explicitly set by the user.
    # [EN] When False, the mode is derived as "enabled if skills_dev/ contains any
    # [EN] plug-in" (works out of the box on first use); when True, dev_mode is
    # [EN] authoritative. Otherwise disabling the switch would be undone automatically
    # [EN] because the directory does contain plug-ins, making developer mode
    # [EN] impossible to turn off.
    # [ZH] 兼容性修正：开发者模式开关是否由用户显式设置过。
    # [ZH] False 时按"skills_dev/ 里有插件就自动启用"处理（首次开箱即用），
    # [ZH] True 时完全以 dev_mode 为准 —— 否则"关掉开关"会因为目录里确实有插件而被
    # [ZH] 自动重新打开，表现为"开发者模式一旦开启就关不掉"。
    dev_mode_explicit: bool = False
    # [EN] Development log (the developer tab displays the last N lines inline).
    # [ZH] 开发日志（开发者 Tab 内嵌展示最后 N 行）。
    dev_log_file: str = "skills_dev.log"
    # [EN] Formal loading accepts only skills with verified=true in the registry
    # [EN] (task 2, item 2.4: load whitelist).
    # [ZH] 正式加载只接受注册表中 verified=true 的技能（相关设计 2.4 加载白名单）。
    require_verified: bool = True


class UpdateConfig(BaseModel):
    """
    [EN]
    Automatic updates (task specification, task 5, option A: check and notify only;
    no automatic download or replacement).

    Options B and C (automatic download/replacement and incremental manifests) are
    not implemented in this version; the rationale is in
    records/v0.2.1-研发记录.md.

    [ZH]
    自动更新（任务书 任务五 方案 A：只检查与提示，不自动下载替换）。

    方案 B/C（自动下载替换、增量清单）本版不实现，理由见 records/v0.2.1-研发记录.md。
    """

    enabled: bool = True
    # [EN] owner/name; the maintainer changes this to the real repository after the
    # [EN] open-source release. Empty means the check is skipped.
    # [ZH] owner/name；开源发布后由维护者改为真实仓库。留空则跳过检查。
    repo: str = "wyx-spark/EchoDesk"
    api_base: str = "https://api.github.com"
    check_on_startup: bool = True
    check_interval_hours: int = Field(default=24, ge=1, le=720)
    include_prerelease: bool = False
    # [EN] Enterprises can disable automatic checks (task specification 5.2, "allow
    # [EN] disabling automatic checks via configuration").
    # [ZH] 企业环境可关掉自动检查（设计规范 5.2「允许通过配置禁用自动检查」）。
    last_check: str = ""
    last_seen_version: str = ""


class McpServerConfig(BaseModel):
    """
    [EN]
    Declaration of one MCP server (v0.2.1 capability extension 1, task 3).

    MCP is configured explicitly by the user: EchoDesk does not auto-discover or
    auto-connect to any address, and does not decide which server to connect to. This
    is also part of the privacy commitment: tools may transmit data externally, so the
    data flow is chosen by the user and the main program only launches the command
    that the user wrote.

    [ZH]
    一个 MCP Server 的声明（v0.2.1 能力扩展-1 任务三）。

    MCP 是用户主动配置的：EchoDesk 不会自动发现、不会自动连接任何地址，
    也不会替用户决定连哪个 server。这一条同时是隐私承诺的一部分 ——
    工具可能把数据带出去，所以数据流向由用户选择，主程序只负责拉起他写的命令。
    """

    name: str = ""
    command: str = ""
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    #: [EN] Parent-process environment variables allowed to pass through to the child
    #: [EN] process (by default none are passed through, so secrets such as HF_TOKEN are
    #: [EN] not handed to third-party processes by accident).
    #: [ZH] 允许透传给子进程的父进程环境变量（默认一个都不透传，
    #: [ZH] 避免把 HF_TOKEN 之类的密钥顺手交给第三方进程）。
    passthrough_env: List[str] = Field(default_factory=list)


class McpConfig(BaseModel):
    """
    [EN]
    MCP client configuration (stdio transport, zero new dependencies; the trade-off
    is documented in ``backend/mcp_client.py``).

    [ZH]
    MCP 客户端配置（stdio 传输，零新依赖；取舍见 ``backend/mcp_client.py``）。
    """

    enabled: bool = False
    #: [EN] Timeout for a single request to one server (seconds). An external process
    #: [EN] may hang, so a limit is mandatory.
    #: [ZH] 单个 server 的一次请求超时（秒）。外部进程可能卡死，必须有上限。
    timeout_seconds: float = Field(default=20.0, ge=1.0, le=600.0)
    #: [EN] Connect and fetch the tool list at startup (default no: launching external
    #: [EN] processes happens only when the user actually needs them).
    #: [ZH] 启动时就连上并拉取工具清单（默认否：拉起外部进程只在用户真要用时发生）。
    auto_connect: bool = False
    servers: List[McpServerConfig] = Field(default_factory=list)


class RetrievalConfig(BaseModel):
    """
    [EN]
    Local document retrieval (v0.2.1 capability extension 1, task 6: RAG depth).

    Approach: keyword BM25 plus paragraph localization, with zero new dependencies
    (rationale in the module notes of ``backend/retrieval.py``: a vector store would
    add tens to hundreds of MB, conflicting with the "350-400 MB after installation"
    target, whereas BM25 is sufficient for this tool's document scale and question
    types). ``mode`` reserves an extension point.

    [ZH]
    本地文档检索（v0.2.1 能力扩展-1 任务六：RAG 深度）。

    方案：关键词 BM25 + 段落定位，零新依赖（理由见 ``backend/retrieval.py``
    的模块说明：向量库会带来几十到几百 MB 的体积，与"安装后 350–400 MB"冲突，
    而本工具的文档规模与问题类型用 BM25 足够）。``mode`` 预留了扩展点。
    """

    enabled: bool = True
    #: [EN] keyword (the only implementation in this version) / hybrid / vector; the
    #: [EN] latter two are reserved for the future and currently degrade to keyword
    #: [EN] (without error, so a configuration edit cannot prevent startup).
    #: [ZH] keyword（本版唯一实现）/ hybrid / vector —— 后两个是给未来留的取值，
    #: [ZH] 现在选它们会退化成 keyword（不会报错，避免用户改配置就起不来）。
    mode: str = "keyword"
    #: [EN] Chunk length and overlap. 600 characters is roughly 300 tokens; four
    #: [EN] chunks occupy about 15% of an 8192 window. The 100-character overlap
    #: [EN] prevents missing an answer that spans a chunk boundary (at the cost of a
    #: [EN] slightly larger context).
    #: [ZH] 分块长度与重叠。600 字符约 300 token，4 块正好占 8192 窗口的 15% 左右；
    #: [ZH] 重叠 100 字符是为了"答案横跨块边界"时不漏（代价是上下文略增）。
    chunk_chars: int = Field(default=600, ge=120, le=4000)
    overlap_chars: int = Field(default=100, ge=0, le=1000)
    #: [EN] Number of segments injected per call and their total length limit. Too many
    #: [EN] segments overwhelm a small model and also crowd the window.
    #: [ZH] 一次注入多少段、总长上限。段数多了小模型会看不过来，也挤占窗口。
    top_k: int = Field(default=4, ge=1, le=12)
    max_context_chars: int = Field(default=2400, ge=200, le=12000)
    #: [EN] BM25 score floor (0 = no threshold). Used to suppress weak noise where only
    #: [EN] a single character matched.
    #: [ZH] BM25 得分下限（0 = 不设阈值）。用于压掉"只有一个字命中"的弱噪声。
    min_score: float = Field(default=0.0, ge=0.0, le=100.0)


class AgentConfig(BaseModel):
    """
    [EN]
    Agent platform configuration (v0.2.1 capability extension 1, task 2).

    Permission model (see the module notes of :mod:`agent_tools`): no tool with side
    effects is provided in this version. Tools are only split into "pure local
    computation / read-only access to uploaded content" (``safe``, enabled by default)
    and "transmits data externally or reads arbitrary local content" (``sensitive``,
    which requires explicit authorization).

    [ZH]
    Agent 平台配置（v0.2.1 能力扩展-1 任务二）。

    权限模型（详见 :mod:`agent_tools` 的模块说明）：有副作用的工具本版一个都不提供，
    只把工具分成"纯本地计算/只读已上传内容"（``safe``，默认可用）与"会把数据带出去或
    读本机任意内容"（``sensitive``，必须显式授权）两类。
    """

    #: [EN] Tool-call step limit. Once reached, the loop switches to "conclude from the
    #: [EN] available information" rather than invoking tools indefinitely. The limit of
    #: [EN] 8 is empirical: small models rarely need more steps, and every extra step is
    #: [EN] a full model call.
    #: [ZH] 工具调用步数上限。到顶之后循环会改成"用已有信息给结论"，不会无限调下去。
    #: [ZH] 上限 8 是经验值：小模型很少需要更多步，而每多一步都是一次完整的模型调用。
    max_steps: int = Field(default=4, ge=1, le=8)
    #: [EN] Names of the sensitive tools that are allowed (web search, reading
    #: [EN] arbitrary local files). The default is empty, meaning all are disabled; the
    #: [EN] user must enable them explicitly in settings or with /agent allow <tool>, so
    #: [EN] the default promise "data does not leave this machine" is preserved.
    #: [ZH] 允许使用的敏感工具名（联网搜索、读任意本地文件）。默认空 = 全部关闭，
    #: [ZH] 用户要在设置里或 /agent allow <工具> 显式打开；这样"数据不出本机"的默认承诺不变。
    allow_sensitive: List[str] = Field(default_factory=list)
    #: [EN] Injection limit for a single tool result (characters). Tool results are fed
    #: [EN] back into the context; without a limit, a single file read can consume half
    #: [EN] the window.
    #: [ZH] 单条工具结果注入上下文的上限（字符）。工具结果会回灌进上下文，不设上限时
    #: [ZH] 一次读文件就能吃掉半个窗口。
    max_observation_chars: int = Field(default=1200, ge=200, le=8000)


class Config(BaseModel):
    """
    [EN]
    Application configuration; fields correspond one-to-one with config.json.

    [ZH]
    应用配置，字段与 config.json 一一对应。
    """

    model_path: str = "Qwen3-0.6B-Q6_K.gguf"
    model_repo: str = "unsloth/Qwen3-0.6B-GGUF"
    hf_endpoint: str = "https://hf-mirror.com"
    # [EN] Default conversation mode. After the mode simplification (capability
    # [EN] extension 1, task 1) only chat/contract/agent remain; legacy values
    # [EN] (general/summary/...) can still be read, because conversation.canonical_mode
    # [EN] migrates them to the new names, so existing config.json files need no manual
    # [EN] edit.
    # [ZH] 默认对话模式。模式简化（后续版本 相关设计）后只剩 chat/contract/agent；
    # [ZH] 旧值（general/summary/…）仍然能读进来 —— conversation.canonical_mode 会把它们
    # [ZH] 迁移成新名字，所以老用户的 config.json 不需要手工改。
    default_mode: str = "chat"
    # [EN] Requested context ceiling (maximum permitted window). The actual
    # [EN] initial allocation is derived by ``hardware_profiler`` from the CPU,
    # [EN] RAM and GPU profile and starts at 2048 tokens; the engine expands it
    # [EN] on demand up to this ceiling or the hardware limit, whichever is
    # [EN] smaller. The previous global constant of 8192 is therefore no longer
    # [EN] a hard limit on the allocated window.
    # [ZH] 请求的上下文上限（允许的最大窗口）。实际初始分配由
    # [ZH] ``hardware_profiler`` 依据 CPU、内存与 GPU 档案推导，起始值为 2048
    # [ZH] token；引擎按需扩展至该上限与硬件上限中的较小者。因此，此前的全局
    # [ZH] 常量 8192 不再是已分配窗口的硬性限制。
    context_length: int = 32768
    # [EN] Configuration ceiling for the context window, used for validation; no value
    # [EN] from any source can exceed it.
    # [ZH] 上下文窗口配置上限：校验用，任何来源的取值都不会超过它
    max_context: int = 32768
    # [EN] Automatically clamp to the selected model's training window (never exceed
    # [EN] the model's training length).
    # [ZH] 自动按所选模型的训练窗口收敛（不超出模型训练长度）
    auto_context: bool = True
    # [EN] Model training window registry (file-name keyword -> tokens); calibrated at
    # [EN] runtime with the GGUF metadata.
    # [ZH] 模型训练窗口注册表（文件名关键词 → tokens）；运行时用 GGUF 元数据校准
    model_context_windows: Dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_MODEL_CONTEXT_WINDOWS))
    # [EN] Long-document segmented summarization.
    # [ZH] 长文档分段摘要
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    # [EN] Context compaction (此前版本 known-issue optimization 2, task 9).
    # [ZH] 上下文压缩（此前版本 优化已知问题-2 相关设计）
    compact: CompactionConfig = Field(default_factory=CompactionConfig)
    # [EN] Multi-file upload (此前版本 known-issue optimization 2, task 8).
    # [ZH] 多文件上传（此前版本 优化已知问题-2 相关设计）
    files: FilesConfig = Field(default_factory=FilesConfig)
    # [EN] Agent platform (此前版本 capability extension 1, task 2).
    # [ZH] Agent 平台（此前版本 后续版本 相关设计）
    agent: AgentConfig = Field(default_factory=AgentConfig)
    # [EN] Local document retrieval (此前版本 capability extension 1, task 6).
    # [ZH] 本地文档检索（此前版本 后续版本 相关设计）
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    # [EN] MCP client (此前版本 capability extension 1, task 3).
    # [ZH] MCP 客户端（此前版本 后续版本 相关设计）
    mcp: McpConfig = Field(default_factory=McpConfig)
    # [EN] Browser automation (task 4).
    # [ZH] 浏览器自动化（相关设计）
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    # [EN] Skill/plug-in marketplace (task 5).
    # [ZH] 技能/插件市场（相关设计）
    marketplace: MarketplaceConfig = Field(default_factory=MarketplaceConfig)
    # [EN] Model selection panel (addition 4).
    # [ZH] 模型选择栏（新增四）
    model_hub: ModelHubConfig = Field(default_factory=ModelHubConfig)
    # [EN] Reasoning content separation (此前版本 model reasoning separation).
    # [ZH] 思考内容分离（此前版本 模型思考内容分离）
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    # [EN] Automatic updates (task 5, option A).
    # [ZH] 自动更新（相关设计 方案 A）
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    # [EN] UI language (task 3, item 3.4): auto = select from the system locale on first
    # [EN] launch; explicit values include "zh-Hans" / "en" / "zh-Hant" / "ja"... The
    # [EN] same field in the front-end ui_state.json takes precedence.
    # [ZH] 界面语言（相关设计 3.4）：auto = 首次启动按系统区域自动选择；
    # [ZH] 也可写 "zh-Hans" / "en" / "zh-Hant" / "ja" …；前端 ui_state.json 同名字段优先。
    language: str = "auto"
    max_history: int = 10
    search_default: bool = False
    # [EN] Temperature ceiling is 1.5 and MUST stay consistent with two other places:
    # [EN]   * the sampling-parameter clamp in `main._handle_capability_settings` is
    # [EN]     `("temperature", 0.0, 1.5)`;
    # [EN]   * the `/temp` command validates the same `0.0 ~ 1.5` range.
    # [EN]
    # [EN] Task 15 (self-test fix 1) observed a real regression: capability extension 1,
    # [EN] task 5 raised the ceiling from 1.0 to 1.5 but missed the model constraint
    # [EN] here (still le=1.0). The consequence was not "the setting is ignored" but a
    # [EN] program that cannot start: the user drags the temperature to 1.5 in settings
    # [EN] -> `capability_settings` accepts it and writes `config.temperature` ->
    # [EN] `save()` persists 1.5 -> on the next launch `Config.load()` fails validation
    # [EN] and the backend crashes during import, leaving only a manual JSON edit as a
    # [EN] recovery path.
    # [EN]
    # [EN] Why this surfaced only now: `save()` previously wrote only to the current
    # [EN] working directory (see the notes on `save()`), so that value went into an
    # [EN] orphan file that nobody read; once `save()` was fixed, the value really took
    # [EN] effect, and the newly added "self-validate before writing" immediately
    # [EN] exposed the latent inconsistency (which is exactly its purpose).
    # [ZH] 温度上限是 1.5，必须与另外两处保持一致：
    # [ZH]   * `main._handle_capability_settings` 里采样参数的夹取范围是
    # [ZH]     `("temperature", 0.0, 1.5)`；
    # [ZH]   * `/temp` 命令的校验同样是 `0.0 ~ 1.5`。
    # [ZH]
    # [ZH] 相关设计（自测问题修复-1）验证发现：后续版本 相关设计把上限从 1.0 放宽到 1.5
    # [ZH] 时，漏改了这里的模型约束（仍是 le=1.0）。后果不是"设置不生效"，而是
    # [ZH] 把程序写成起不来：用户在设置里把温度拖到 1.5 → `capability_settings` 接受并
    # [ZH] 写进 `config.temperature` → `save()` 落盘成 1.5 → 下次启动 `Config.load()`
    # [ZH] 校验失败，后端导入即崩，只能手改 JSON 才救得回来。
    # [ZH]
    # [ZH] 之所以现在才暴露：`save()` 原先只写当前工作目录（见 `save()` 的说明），
    # [ZH] 那份值写进了孤立文件、没人读；而 `save()` 修好之后它就真的生效了 ——
    # [ZH] 新加的"写入前自我校验"把这条潜伏的不一致立刻暴露了出来（这正是加它的目的）。
    temperature: float = Field(default=0.7, ge=0.0, le=1.5)
    max_tokens: int = 1024
    search_results: int = 3
    # ---------- [EN] Fix 4: generation presets and context management ----------
    # ---------- [ZH] 兼容性修正：生成参数预设与上下文管理 ----------
    # [EN] Current preset: light / balanced / quality (switched by /preset; determines
    # [EN] the three values below).
    # [ZH] 当前预设档位：light / balanced / quality（/preset 切换，决定下面三项的取值）
    preset: str = "balanced"
    # [EN] Inference preset: fast / balanced / quality (settings model page). Unlike
    # [EN] ``preset`` above, which only selects sampling parameters, this field governs
    # [EN] construction-time parameters: the initial context window, the usable fraction
    # [EN] of the native/hardware ceiling, the prompt batch size and the thread
    # [EN] preference. The balanced value is the documented default and preserves the
    # [EN] behaviour established by the previous release.
    # [ZH] 推理预设：fast / balanced / quality（设置模型页）。与上面的 ``preset`` 不同，
    # [ZH] 后者只选择采样参数；本字段支配构建期参数：初始上下文窗口、原生/硬件上限的
    # [ZH] 可用比例、提示词批大小与线程偏好。balanced 为文档规定的默认值，并保持上一
    # [ZH] 发布版本已确立的行为。
    inference_profile: str = "balanced"
    # [EN] top_p and repetition penalty: these two mainly control repetition loops and
    # [EN] topic drift in small models.
    # [ZH] top_p 与重复惩罚：小模型"复读"和"跑题"主要由这两项控制
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    repeat_penalty: float = Field(default=1.1, ge=0.5, le=2.0)
    # [EN] Behaviour when the context limit is exceeded:
    # [EN]   summarize: hand early conversation to the model for compaction (default,
    # [EN]              best information retention)
    # [EN]   slide:     discard the oldest history (sliding window; fastest, no extra
    # [EN]              inference)
    # [ZH] 上下文超限时的处理方式：
    # [ZH]   summarize —— 把早期对话交给模型压缩成摘要（默认，信息保留最好）
    # [ZH]   slide     —— 直接丢弃最早的历史（滑动窗口，最快，不做额外推理）
    context_mode: str = "summarize"
    # [EN] Whether to cache long-document segmented summaries (a second upload of the
    # [EN] same file is not recomputed; direction 8).
    # [ZH] 是否缓存长文档分段摘要（同一文件二次上传不重算，方向八）
    summary_cache: bool = True
    # [EN] Ask a clarifying question instead of guessing when the question is too vague
    # [EN] (direction 7); disabling reverts to "always answer".
    # [ZH] 提问过于模糊时先追问而不是硬答（方向七）；关掉即回到"无论如何都作答"
    clarify_vague: bool = True
    # [EN] Character limit for file parsing; 0 means unlimited (long documents go
    # [EN] through segmented summarization, avoiding information loss from direct
    # [EN] truncation).
    # [ZH] 文件解析字符上限；0 表示不限制（长文档交由分段摘要处理，避免直接截断丢信息）
    file_max_chars: int = 200000
    theme: str = "dark"
    warmup: bool = True
    # [EN] Inference performance: 0 means derive the thread count from the local CPU
    # [EN] core count.
    # [ZH] 推理性能：0 表示按本机 CPU 核数自动设置
    n_threads: int = 0
    n_batch: int = 512
    # [EN] Number of GPU acceleration layers: 0 = CPU only (with an NVIDIA GPU this
    # [EN] can be raised, e.g. 99 for all layers).
    # [ZH] GPU 加速层数：0 = 纯 CPU（有 NVIDIA 显卡可调大，如 99=全部）
    n_gpu_layers: int = 0
    # [EN] Explicit inference override (v0.2.4 task 3). When true, the values
    # [EN] in ``context_length``, ``n_gpu_layers`` and ``n_batch`` are treated as
    # [EN] the user's decision: the planner adopts them verbatim and returns
    # [EN] warnings instead of silently narrowing them. The default false keeps
    # [EN] the established behaviour, where the planner recommends and applies a
    # [EN] safe value.
    # [ZH] 显式推理覆盖开关（v0.2.4 任务-3）。为真时，``context_length``、
    # [ZH] ``n_gpu_layers`` 与 ``n_batch`` 被视作用户的决定：规划器原样采纳并返回
    # [ZH] 警告，而不是静默收窄。默认假保留既有行为：由规划器给出并应用安全推荐值。
    inference_override: bool = False
    # [EN] Session persistence file (an empty string disables automatic saving).
    # [ZH] 会话持久化文件（空字符串表示关闭自动保存）
    session_file: str = "session.json"
    # [EN] Model profile registry: alias -> {path, repo, context_length, temperature}.
    # [ZH] 模型档案注册表：别名 -> {path, repo, context_length, temperature}
    models: Dict[str, Dict[str, Any]] = {}
    # [EN] Show a hint at the end of a reply when generation becomes noticeably slower
    # [EN] after the context is extended.
    # [ZH] 扩展上下文后生成明显变慢时，在回复末尾给出提示
    speed_hint: bool = True
    speed_min_tps: float = 3.0
    # ---------- [EN] Streaming heartbeat and runtime metrics (此前版本 known-issue optimization 2, task 5) ----------
    # ---------- [ZH] 流式心跳与运行指标（此前版本 优化已知问题-2 相关设计） ----------
    # [EN] How long without a new token triggers a heartbeat during generation. In the
    # [EN] prefill phase (before the first token), CPU-only inference may produce no
    # [EN] output for tens of seconds, and the interface cannot distinguish "computing"
    # [EN] from "hung". The 0.5-second lower bound prevents a value of 0 from flooding
    # [EN] the protocol; 0 disables heartbeats.
    # [ZH] 生成过程中"多久没有新 token 就发一次心跳"。prefill 阶段（首 token 之前）
    # [ZH] 纯 CPU 上可能几十秒没有任何输出，界面无法区分"在算"与"卡死"。
    # [ZH] 下限 0.5 秒防止有人配成 0 把协议刷爆；0 表示关闭心跳。
    stream_heartbeat_seconds: float = Field(default=1.5, ge=0.0, le=30.0)
    # [EN] Merge throttle interval for front-end deltas (milliseconds). The backend emits
    # [EN] one delta per token; the front end merges UI updates at this interval, because
    # [EN] re-laying out the RichTextBox for every token is the main cause of stutter.
    # [ZH] 前端 delta 的合并节流间隔（毫秒）。后端逐 token 发，前端按这个间隔
    # [ZH] 合并一次 UI 更新 —— 每 token 都重排一次 RichTextBox 才是卡顿的主因。
    stream_throttle_ms: int = Field(default=50, ge=0, le=1000)

    @field_validator("inference_profile", mode="before")
    @classmethod
    def _normalize_inference_profile(cls, value: Any) -> str:
        """
        [EN]
        Normalize the inference preset identifier and reject unknown values by
        falling back to the balanced default. A hand-edited configuration file
        must never prevent startup, and an unknown identifier must not silently
        select an unintended tuning profile.

        [ZH]
        规范化推理预设标识，并通过回落到均衡默认值来拒绝未知取值。手改的配置文件
        绝不能阻止程序启动，未知标识也不得静默选中非预期的调优档位。
        """
        text = str(value or "").strip().lower()
        aliases = {"speed": "fast", "light": "fast", "turbo": "fast",
                   "default": "balanced", "normal": "balanced",
                   "max": "quality", "high": "quality", "best": "quality"}
        text = aliases.get(text, text)
        return text if text in ("fast", "balanced", "quality") else "balanced"

    @model_validator(mode="after")
    def _clamp_context(self) -> "Config":
        """
        [EN]
        Context window validation. In the default (recommended) mode, values from
        any source are clamped to [MIN_CONTEXT, max_context], preserving the
        established automatic behaviour. An explicit override is adopted
        verbatim up to MAX_CONTEXT_OVERRIDE, because the whole point of the
        override is that the user — not the planner — decides; a value the
        machine cannot serve then fails at load time with a structured error.
        A hand-edited value above max_context is treated as an override
        automatically, so editing config.json directly behaves like the
        settings-page input.

        [ZH]
        上下文窗口校验。默认（推荐）模式下，任何来源的取值都钳制到
        [MIN_CONTEXT, max_context]，保持既有的自动行为。显式覆盖值则在
        MAX_CONTEXT_OVERRIDE 以内原样采纳 —— 覆盖的意义正是"由用户而非规划器决定"；
        机器无法承载的值会在加载阶段以结构化错误失败。手改配置中超过 max_context
        的值会自动视为覆盖，使直接编辑 config.json 与设置页输入具有相同语义。
        """
        if self.max_context < MIN_CONTEXT:
            raise ValueError(f"max_context 不得小于 {MIN_CONTEXT}")
        original = self.context_length
        wants_override = bool(self.inference_override) or int(original) > int(self.max_context)
        if wants_override:
            self.inference_override = True
            self.context_length = max(MIN_CONTEXT, min(int(original), MAX_CONTEXT_OVERRIDE))
        else:
            self.context_length = max(MIN_CONTEXT, min(int(original), int(self.max_context)))
        if self.context_length != original:
            limit = MAX_CONTEXT_OVERRIDE if wants_override else self.max_context
            print(
                f"⚠️ context_length={original} 超出允许范围，已钳制为 {self.context_length}"
                f"（上限 {limit}）",
                file=sys.stderr, flush=True,
            )
        return self

    def context_for_model(self, model_path: str, train_context: int = 0, requested: Optional[int] = None) -> int:
        """
        [EN]
        Determine the effective context window from the selected model.

        The result is the minimum of the following three values (with MIN_CONTEXT as
        the floor):
          1. the configured context_length (``requested`` overrides it, e.g. a value
             carried by a model profile);
          2. the model's training window (GGUF metadata train_context, or the
             model_context_windows registry);
          3. the configured ceiling max_context.

        When auto_context is False, item 2 is ignored (the user explicitly asked to
        open the window at the configured value).

        [ZH]
        按"所选模型"决定实际使用的上下文窗口。

        取以下三者的最小值（下限 MIN_CONTEXT）：
          1. 配置请求的 context_length（可用 requested 覆盖，如模型档案自带的值）；
          2. 模型训练窗口（GGUF 元数据 train_context，或 model_context_windows 注册表）；
          3. 配置上限 max_context。

        auto_context=False 时忽略第 2 项（用户明确要求按配置值强制开窗）。
        """
        window = self.lookup_train_context(model_path) or int(train_context or 0)
        limit = int(self.max_context)
        want = int(self.context_length if requested is None else requested)
        if self.auto_context and window > 0:
            limit = min(limit, window)
        return max(MIN_CONTEXT, min(want, limit))

    def lookup_train_context(self, model_path: str) -> int:
        """
        [EN]
        Match a training window from the model_context_windows registry by file-name
        keyword.

        [ZH]
        从 model_context_windows 注册表按文件名关键词匹配训练窗口。
        """
        base = os.path.basename(model_path or "").lower()
        best = 0
        for key, value in (self.model_context_windows or {}).items():
            if key and key.lower() in base:
                best = max(best, int(value))
        return best

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        """
        [EN]
        Locate the configuration file by priority and overlay the user patch
        (config.user.json in the same directory takes precedence).

        Encoding (v0.2.1 model reasoning separation; defect found by the interface
        probe): always read with ``utf-8-sig``. On Windows, Notepad saves UTF-8 with a
        BOM, and plain ``utf-8`` decoding raises
        ``JSONDecodeError: Unexpected UTF-8 BOM``, so the backend crashes during
        startup and the program exits; the user then sees "the backend exited
        unexpectedly" with no indication that a BOM in the configuration file caused
        it. The protocol layer has long tolerated a BOM on stdin (see the read loop in
        main.py), and the configuration side needs the same tolerance.

        Fault tolerance: failure to read the base configuration still raises (the
        application cannot work without it); ``config.user.json`` is an optional
        overlay, so a malformed file only produces a warning and is ignored. A syntax
        error in an optional file edited by hand must never prevent the entire backend
        from starting.

        [ZH]
        按优先级查找配置文件，并叠加用户补丁（config.user.json 同目录优先）。

        编码（v0.2.1 模型思考内容分离，界面探针实测出来的缺陷）：一律用
        ``utf-8-sig`` 读。Windows 上记事本保存 UTF-8 会带 BOM，而普通 ``utf-8``
        解码会直接抛 ``JSONDecodeError: Unexpected UTF-8 BOM`` —— 后端在启动阶段
        就崩掉、程序随之退出，用户看到的是"后端意外退出"，完全看不出是配置文件
        的 BOM 造成的。协议层早就在兼容 stdin 的 BOM（见 main.py 的读取循环），
        配置文件这一侧同样要兼容。

        容错：基础配置读不出来仍然抛异常（没有配置无法工作）；``config.user.json``
        是可选覆盖层，格式坏掉时只告警并忽略它，绝不因此让整个后端起不来 ——
        用户手改一个可选文件写出语法错误，代价不该是程序打不开。
        """
        base_path = cls._find(path)
        if base_path is None:
            raise FileNotFoundError(f"找不到配置文件: {path}")
        with open(base_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)

        # [EN] User patch overlay: config.user.json in the same directory as the base
        # [EN] configuration; its fields take precedence.
        # [ZH] 用户补丁覆盖层：与基础配置同目录的 config.user.json，字段优先
        user_path = os.path.join(os.path.dirname(base_path), "config.user.json")
        if os.path.exists(user_path):
            try:
                with open(user_path, "r", encoding="utf-8-sig") as f:
                    user = json.load(f)
                if isinstance(user, dict):
                    data.update(user)
                else:
                    print("⚠️ config.user.json 顶层不是对象，已忽略该覆盖层",
                          file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 - [EN] a broken overlay must not prevent startup
                                      # [ZH] 覆盖层坏了不能拖垮启动
                print(f"⚠️ config.user.json 解析失败，已忽略该覆盖层：{exc}",
                      file=sys.stderr, flush=True)

        return cls(**data)

    def save(self, path: str = "config.json") -> None:
        """
        [EN]
        Write the current configuration back to the file it was read from (not to the
        current working directory).

        Task 15 (self-test fix 1): this was a genuine read/write asymmetry defect. The
        old implementation called ``open(path, "w")`` with a relative path, writing
        only to the current working directory with no fallback, whereas the read path
        (:meth:`_find`) has four candidate locations (cwd -> executable directory ->
        ``_MEIPASS`` -> directory of this file). Whenever cwd differed from the real
        configuration directory, two failures occurred at once:

        1. Written but not persisted: a new, orphaned ``config.json`` was written, while
           the file that was actually read was never updated. The user observed "I
           changed it in settings and it reverted after restart".
        2. Configuration crossover: that orphan file is the first entry in
           :meth:`_find`, so launching from a different cwd read it in preference,
           appearing as "the configuration changed or became inconsistent by itself".

        Why this went unnoticed: when the packaged front end launches the backend it
        sets the working directory to ``app/backend/`` (``WorkingDirectory`` in
        `BackendService.cs`), where cwd happens to equal the configuration directory,
        so the old code was "accidentally correct". Only source runs, test runs, and
        "the user launches the backend from another directory" exposed the defect;
        every one of these cases was reproduced during the test round (running the
        backend once from the repository root left an orphaned ``config.json`` in the
        root).

        Fix: locate the real file with exactly the same rules as the read path, so
        reads and writes point at the same file. When no candidate exists, the legacy
        behaviour is preserved (write ``path``, relative to cwd). This requirement was
        imposed by the gate: scripts such as `tools/test_model_admin.py` chdir into a
        temporary directory and then `save()` a configuration that does not exist yet;
        if the write were redirected to the program directory, callers would no longer
        find the file at the original path. First runs and tests fall into this
        category, so preserving the legacy behaviour avoids a regression.

        [ZH]
        把当前配置写回它被读出来的那个文件（而不是当前工作目录）。

        任务十五（自测问题修复-1）—— 这是一个读写不对称的真实缺陷。
        旧实现是 ``open(path, "w")``：相对路径，只写当前工作目录，没有任何兜底；
        而读取用的 :meth:`_find` 有四个候选位置
        （cwd → exe 目录 → ``_MEIPASS`` → 本文件所在目录）。只要 cwd 与配置真实
        所在目录不一致，就会同时造成两件坏事：

        1. 写了却存不住：写入的是一份新的、孤立的 ``config.json``，
           真正被读的那份永远不会被更新。用户看到的是"我在设置里改了，
           重启又变回去了"。
        2. 配置串台：那个孤立文件在 :meth:`_find` 里正好排在第一位，
           于是换个 cwd 启动时它反而被优先读取 —— 表现成"配置自己变了/错乱"。

        为什么一直没被发现：打包后前端启动后端时会把工作目录设成
        ``app/backend/``（`BackendService.cs` 的 ``WorkingDirectory``），
        cwd 恰好等于配置所在目录，旧写法"碰巧是对的"。源码运行、跑测试、
        以及"用户自己从别的目录运行后端"这三种情况才会暴露 —— 本轮实测全部命中
        （在仓库根跑一次后端，根目录就多出一份孤立的 ``config.json``）。

        修法：用与读取完全相同的那套规则定位真实文件，读写从此指向同一份。
        一个候选都不存在时保持旧行为（写 ``path``，即相对 cwd）—— 这一点是被门禁
        逼出来的：`tools/test_model_admin.py` 之类会在临时目录里 chdir 之后 `save()`
        一份尚不存在的配置；若此时改写到程序目录，调用方按原路径就找不到文件了。
        首次运行与测试都属于这一类，保持旧行为才不制造回归。
        """
        try:
            target = self._find(path)
        except OSError:
            # [EN] `_find` calls `os.getcwd()` internally, and cwd may already have
            # [EN] been deleted (exactly the state at the end of a TemporaryDirectory
            # [EN] in tests); fall back to the legacy behaviour.
            # [ZH] `_find` 内部会取 `os.getcwd()`，而 cwd 可能已经被删掉
            # [ZH] （测试里的 TemporaryDirectory 结束时正是这种状态）—— 退回旧行为。
            target = None
        if target is None:
            target = path
        data = self.model_dump()
        # [EN] Task 15 (observed in this test round): the data MUST be self-validated
        # [EN] before writing.
        # [EN]
        # [EN] pydantic does not validate assignments by default
        # [EN] (``validate_assignment=False``), so the in-memory object may well hold a
        # [EN] value that violates its own constraints. Observed case: after
        # [EN] `temperature` was set to 1.5 (the constraint being ``le=1.0``), saving
        # [EN] wrote the invalid value into the configuration file, which is read on
        # [EN] every subsequent launch, so the next import raised `ValidationError` and
        # [EN] the program could never start again.
        # [EN]
        # [EN] This failure mode is far worse than "the setting was not saved": the
        # [EN] latter is fixed by clicking once more, while the former requires a manual
        # [EN] JSON edit to recover. It is therefore preferable to let this save fail
        # [EN] (raising with the reason) than to write a configuration the program
        # [EN] itself cannot read.
        # [ZH] 相关设计（本轮验证发现）：写入前必须自我校验。
        # [ZH]
        # [ZH] pydantic 默认不校验赋值（``validate_assignment=False``），所以内存里完全可能
        # [ZH] 存在一个违反自身约束的值。验证：`temperature` 被设成 1.5（约束是
        # [ZH] ``le=1.0``）之后保存，配置文件就被写成了非法值 —— 而它是以后每次启动都
        # [ZH] 要读的文件，于是后端下次导入时直接 `ValidationError`，程序再也起不来。
        # [ZH]
        # [ZH] 这个失败模式比"设置没存上"严重得多：前者用户再点一次就好，后者要用手改
        # [ZH] JSON 才能救回来。所以这里宁可让这次保存失败（如实抛出并带上原因），
        # [ZH] 也不写出一个程序自己都读不了的配置。
        try:
            type(self).model_validate(data)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"拒绝写入非法配置（写入后程序将无法启动）：{exc}") from exc
        # [EN] Idempotency: do not rewrite when the content has no semantic change.
        # [EN] Two practical reasons:
        # [EN]   1. The backend calls `save()` in some flows (writing normalized results
        # [EN]      back after a hot model reload). Rewriting verbatim every time would
        # [EN]      make `git` permanently report "configuration file modified" even
        # [EN]      though no value changed, which is a large amount of noise when
        # [EN]      investigating real changes (and is easily misread as "someone
        # [EN]      changed the configuration").
        # [EN]   2. On read-only media (a portable build on a managed enterprise disk), a
        # [EN]      pointless write is a pointless error.
        # [EN] The comparison uses the parsed object rather than text, so a different key
        # [EN] order does not count as a change (`model_dump()` field order often differs
        # [EN] from hand-written JSON, which was observed in practice).
        # [ZH] 幂等：内容没有语义变化就不重写。两个实际理由：
        # [ZH]   1. 后端在某些流程里会 `save()` 一次（模型热加载后写回规范化结果等）。
        # [ZH]      如果每次都原样重写，`git` 会一直显示"配置文件被改过"而值其实没变 ——
        # [ZH]      排查真实改动时噪声很大（而且会被误判成"谁改了配置"）。
        # [ZH]   2. 只读介质上（企业受管盘里的便携版），一次无谓的写入就等于一次报错。
        # [ZH] 比较用解析后的对象而不是文本：键顺序不同不该算作"变了"
        # [ZH] （`model_dump()` 的字段顺序与手写 JSON 的顺序往往不一致，验证就是这样）。
        if os.path.exists(target):
            try:
                with open(target, encoding="utf-8-sig") as f:
                    if json.load(f) == data:
                        return
            except Exception:  # noqa: BLE001 - [EN] an unreadable file is treated as "needs writing"
                               # [ZH] 读不动就当作"需要写"
                pass
        # [EN] Write LF explicitly (`newline="\n"`). On Windows, text mode writes CRLF
        # [EN] by default, while the repository `.gitattributes` declares this file as
        # [EN] LF, so every save would mark the entire file as modified (git warns
        # [EN] "CRLF will be replaced by LF") and the `tools/check_line_endings.py` gate
        # [EN] would also fail (observed in this test round). The configuration is a file
        # [EN] the program writes itself and humans also read, so its line endings must
        # [EN] be stable.
        # [ZH] 显式写 LF（`newline="\n"`）。Windows 上文本模式默认写 CRLF，而仓库的
        # [ZH] `.gitattributes` 规定这个文件是 LF —— 于是每次保存都会让整个文件变成
        # [ZH] "被改动"（git 会警告 "CRLF will be replaced by LF"），
        # [ZH] `tools/check_line_endings.py` 门禁也会失败（本轮验证发现）。
        # [ZH] 配置是"程序自己写、也要给人看"的文件，行尾必须稳定。
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    @staticmethod
    def _find(path: str) -> str | None:
        """
        [EN]
        Locate the real path of the configuration file by priority.

        [ZH]
        按优先级查找配置文件的真实路径。
        """
        candidates = [os.path.join(os.getcwd(), path)]
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(os.path.dirname(sys.executable), path))
        if getattr(sys, "_MEIPASS", False):
            candidates.append(os.path.join(sys._MEIPASS, path))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), path))
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return None
