"""
[EN]
Context-compaction event log (task 9: event traceability and replay).

One optimization direction of task 9 is "compaction events must be traceable:
record the token counts before and after compaction, the summary content, and the
trigger reason. Compaction events may use event sourcing to support replay."

This module is an append-only JSONL log, following the same approach as the
session log from task 11:

* Append-only writes: each event is one JSON line, so a crash mid-write loses at
  most the final line; all preceding lines remain intact.
* Human-readable: ``ensure_ascii=False``, so the file can be inspected in a plain
  text editor.
* Auditable: records the trigger reason, threshold, token counts before and after
  compaction, the number of messages compacted, the number retained, the full
  summary text, and which salient points the summary carried.
* Redactable: summaries and message bodies may contain sensitive information such
  as contract amounts and parties. When ``redact`` is true, only lengths and counts
  are recorded, not the body text (by default the summary is recorded, because the
  summary itself is the user-facing compaction artifact; this can be disabled with
  ``compact.log_content=false``).
* Replayable: :func:`iter_events` reads the events back line by line and
  :func:`replay` reconstructs how the current summary evolved step by step (the
  input summary and the produced summary of every step).

Why no database or event bus: this project is a local, single-machine tool with no
server component. An append-only file plus pure-function replay already satisfies
the three requirements of traceability, auditability, and replay, and introduces no
new dependency (the allowlist in ``tools/check_requirements.py`` contains no
database driver).

[ZH]
上下文压缩事件日志（任务九：压缩事件可追溯、可回放）。

任务九的优化方向之一是"压缩事件可追溯：记录压缩前后 token 数、摘要内容、触发
原因。压缩事件可考虑事件溯源方式，支持回放"。

本模块是一个追加式 JSONL 日志（与任务十一的会话日志采用同一思路）：

* 追加写：每条事件一行 JSON，写入中途崩溃最多丢失最后一行，之前的行均保留；
* 可读：使用 ``ensure_ascii=False``，可直接用文本编辑器查看；
* 可审计：记录触发原因、阈值、压缩前后的 token 数、被压缩的消息条数、保留条数、
  摘要全文，以及摘要中包含的重点；
* 可脱敏：摘要与消息正文可能包含合同金额、当事人等敏感信息，因此当 ``redact``
  为真时只记录长度与计数，不记录正文（默认记录摘要，因为摘要本身即面向用户的
  压缩产物；可通过 ``compact.log_content=false`` 关闭）；
* 可回放：:func:`iter_events` 按行读回事件，:func:`replay` 可重放"当前摘要如何
  逐步演变为当前形态"（每步的输入摘要与产出摘要）。

不采用数据库或事件总线的原因：本项目是本地单机、无服务端的工具。一个追加文件
加纯函数回放已满足"可追溯、可审计、可回放"三项要求，且不引入任何新依赖
（``tools/check_requirements.py`` 的白名单中不含数据库驱动）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger("EchoDesk.CompactLog")

#: [EN] Default file name (corresponds to ``config.compact.log_file``).
#: [ZH] 默认文件名（与 ``config.compact.log_file`` 对应）。
DEFAULT_LOG_NAME = "compact_log.jsonl"

#: [EN] Event types.
#: [ZH] 事件类型。
EVENT_TRIGGER = "trigger"      # [EN] A compaction was triggered.
                               # [ZH] 触发了一次压缩。
EVENT_SUMMARY = "summary"      # [EN] Summary generated and written back to the session.
                               # [ZH] 摘要已生成并写回会话。
EVENT_SKIP = "skip"            # [EN] No compaction (threshold not reached, nothing to
                               # [EN] compact, or failure).
                               # [ZH] 本次未压缩（未达阈值、无可压缩内容或失败）。


@dataclass
class CompactEvent:
    """
    [EN]
    Complete record of one compaction event.

    [ZH]
    一次压缩事件的完整记录。
    """

    kind: str = EVENT_TRIGGER
    time: str = ""
    reason: str = ""            # [EN] Trigger reason: over_limit / manual / focus / under_limit ...
                                # [ZH] 触发原因：over_limit / manual / focus / under_limit 等。
    trigger: str = "auto"       # [EN] auto (automatic on threshold) / manual (/compact command).
                                # [ZH] auto（超阈值自动触发）/ manual（/compact 手动触发）。
    mode: str = "summarize"     # [EN] summarize / slide.
                                # [ZH] summarize / slide。
    limit: int = 0              # [EN] Threshold in tokens.
                                # [ZH] 阈值（token）。
    ratio: float = 0.0          # [EN] Trigger ratio.
                                # [ZH] 触发比例。
    used_before: int = 0
    used_after: Optional[int] = None
    dropped: int = 0            # [EN] Number of messages compacted.
                                # [ZH] 被压缩的消息条数。
    kept: int = 0               # [EN] Number of messages retained verbatim.
                                # [ZH] 逐字保留的消息条数。
    grace: int = 0              # [EN] Number of turns retained in the grace zone.
                                # [ZH] grace zone 保留的轮数。
    summary_chars: int = 0
    summary: str = ""           # [EN] Full summary text (empty when redacted).
                                # [ZH] 摘要全文（脱敏时为空）。
    focus: str = ""             # [EN] User-specified focus (/compact focus on ...).
                                # [ZH] 用户指定的重点（/compact focus on ...）。
    prompt: str = ""            # [EN] Summary prompt used for this run (reproduces the
                                # [EN] wording in effect at the time).
                                # [ZH] 本次使用的摘要提示词（便于复现当时的口径）。
    truncated_outputs: int = 0  # [EN] Number of large blocks truncated individually.
                                # [ZH] 被单独截断的大块内容条数。
    saved_chars: int = 0        # [EN] Characters not fed to the model due to truncation.
                                # [ZH] 因截断而少喂给模型的字符数。
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = self.__dict__.copy()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompactEvent":
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)  # type: ignore[arg-type]


class CompactLog:
    """
    [EN]
    Append-only compaction event log in JSONL format.

    Thread safety: compaction may be triggered either automatically by the
    threshold or by the user via ``/compact``, and both may run concurrently (for
    example, a manual compaction while the history is already over the limit);
    therefore file writes are guarded by a lock. When ``record_content=False``, the
    summary body is not recorded (only lengths and counts).

    [ZH]
    追加式压缩事件日志（JSONL）。

    线程安全：压缩既可能由阈值自动触发，也可能由用户通过 ``/compact`` 触发，两者
    可能并发（例如历史已超限时用户执行手动压缩），因此写盘需要加锁。当
    ``record_content=False`` 时不记录摘要正文（只记录长度与计数）。
    """

    def __init__(self, path: str, record_content: bool = True, max_events: int = 5000):
        self.path = path
        self.record_content = bool(record_content)
        self.max_events = max(100, int(max_events or 5000))
        self._lock = threading.Lock()

    def append(self, event: CompactEvent) -> bool:
        """
        [EN]
        Append one event. Failures only produce a warning: a log write failure must
        never cause compaction to fail.

        [ZH]
        追加一条事件；失败时仅记录告警（日志写入失败绝不能让压缩失败）。
        """
        if not self.path:
            return False
        if not event.time:
            event.time = time.strftime("%Y-%m-%dT%H:%M:%S")
        if not self.record_content:
            event.summary = ""
        try:
            with self._lock:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            self._trim_if_needed()
            return True
        except Exception:
            log.warning("压缩事件日志写入失败: %s", self.path, exc_info=True)
            return False

    def _trim_if_needed(self) -> None:
        """
        [EN]
        When the event count is too high, retain only the most recent ``max_events``
        entries to prevent unbounded log growth.

        [ZH]
        事件过多时仅保留最近 ``max_events`` 条，防止日志无限增长。
        """
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
            if len(lines) <= self.max_events:
                return
            keep = lines[-self.max_events:]
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.writelines(keep)
            log.info("压缩事件日志超过 %d 条，已裁剪为最近 %d 条", self.max_events, len(keep))
        except Exception:
            log.debug("压缩事件日志裁剪失败（不影响压缩）", exc_info=True)

    def events(self) -> List[CompactEvent]:
        """
        [EN]
        Read back all events. Corrupt lines are skipped so that one bad line does not
        make the whole log unreadable.

        [ZH]
        读回全部事件（跳过损坏行，避免因单行损坏导致整份日志无法读取）。
        """
        return list(iter_events(self.path))

    def stats(self) -> Dict[str, Any]:
        """
        [EN]
        Aggregated statistics for ``/status`` and the settings page.

        [ZH]
        汇总统计（供 /status 与设置页展示）。
        """
        items = self.events()
        compressed = [e for e in items if e.kind == EVENT_SUMMARY]
        return {
            "file": self.path,
            "events": len(items),
            "compressed": len(compressed),
            "last_time": items[-1].time if items else "",
            "last_reason": items[-1].reason if items else "",
            "saved_chars": sum(e.saved_chars for e in items),
        }


def iter_events(path: str) -> Iterator[CompactEvent]:
    """
    [EN]
    Read events line by line. Corrupt lines are skipped with a warning: the log
    serves auditing purposes, so a single corrupt line must not invalidate the whole
    file.

    [ZH]
    逐行读回事件；损坏行跳过并告警（日志用于审计，不能因单行损坏而整体失效）。
    """
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    yield CompactEvent.from_dict(json.loads(text))
                except Exception:
                    log.warning("压缩事件日志第 %d 行无法解析（已跳过）", number)
    except Exception:
        log.warning("压缩事件日志读取失败: %s", path, exc_info=True)


@dataclass
class ReplayStep:
    """
    [EN]
    One replay step: this compaction changed the summary from ``before`` to
    ``after``.

    [ZH]
    回放的一步：本次压缩将摘要从 ``before`` 变为 ``after``。
    """

    time: str = ""
    trigger: str = ""
    before: str = ""
    after: str = ""
    used_before: int = 0
    used_after: Optional[int] = None
    dropped: int = 0


def replay(events: List[CompactEvent]) -> List[ReplayStep]:
    """
    [EN]
    Event-sourcing replay: reconstruct how the summary was assembled over time.

    The session summary implementation appends each new summary to the previous one
    on every compaction (``conv.summary = old + new``), so replaying simply means
    accumulating the concatenation in chronological order. This answers "which
    compaction introduced each sentence of the current summary", which is the
    practical value of traceability (for example, a user asking when a particular
    amount paragraph stopped being summarized).

    [ZH]
    事件溯源回放：还原历次摘要的拼接过程。

    会话摘要的实现方式是每次压缩都把新摘要追加到旧摘要之后
    （``conv.summary = old + new``），因此回放即按时间顺序累计拼接。这样可以回答
    "当前摘要中的每一句由第几次压缩引入"，这正是可追溯性的实际价值（例如用户询问
    某段金额信息是在何时被摘要忽略的）。
    """
    steps: List[ReplayStep] = []
    accumulated = ""
    for event in events:
        if event.kind != EVENT_SUMMARY or not event.summary:
            continue
        before = accumulated
        accumulated = f"{before}\n{event.summary}".strip() if before else event.summary
        steps.append(ReplayStep(
            time=event.time, trigger=event.trigger, before=before, after=accumulated,
            used_before=event.used_before, used_after=event.used_after,
            dropped=event.dropped,
        ))
    return steps


def final_summary(events: List[CompactEvent]) -> str:
    """
    [EN]
    Replay the summary that should currently be in effect (used for crash recovery
    and consistency verification).

    [ZH]
    回放出"当前应当生效的摘要"（供崩溃恢复与一致性校验使用）。
    """
    steps = replay(events)
    return steps[-1].after if steps else ""
