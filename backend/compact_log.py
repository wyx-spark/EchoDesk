"""上下文压缩事件日志（任务九：压缩事件可追溯 / 可回放）。

任务书任务九的优化方向之一是"**压缩事件可追溯**：记录压缩前后 token 数、
摘要内容、触发原因。压缩事件可考虑事件溯源方式，支持回放"。

本模块是一个**追加式 JSONL** 日志（与任务十一的会话日志同一套思路）：

* **追加写**：每条事件一行 JSON，写到一半崩溃最多丢最后一行，前面的都还在；
* **可读**：``ensure_ascii=False``，记事本能直接看；
* **可审计**：记录触发原因、阈值、压缩前后的 token 数、被压缩的消息条数、
  保留条数、摘要全文、以及"摘要里带了什么重点"；
* **可脱敏**：摘要与消息正文可能含合同金额、当事人等敏感信息，故
  ``redact`` 为真时只记**长度与计数**，不记正文（默认记摘要，因为摘要本身
  就是给用户看的压缩产物；但可用 ``compact.log_content=false`` 关掉）；
* **可回放**：:func:`iter_events` 按行读回，:func:`replay` 能重放出
  "当前摘要是怎么一步步变成现在这样的"（每步的输入摘要 + 产出摘要）。

为什么不做成数据库/事件总线：本项目是**本地单机、无服务端**的工具，
一个追加文件 + 纯函数回放已经满足"可追溯、可审计、可回放"三项要求，
并且没有任何新依赖（``tools/check_requirements.py`` 的白名单里没有 DB）。
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

#: 默认文件名（与 ``config.compact.log_file`` 对应）
DEFAULT_LOG_NAME = "compact_log.jsonl"

#: 事件类型
EVENT_TRIGGER = "trigger"      # 触发了一次压缩
EVENT_SUMMARY = "summary"      # 摘要已生成并写回会话
EVENT_SKIP = "skip"            # 本次没压缩（未到阈值 / 无可压缩内容 / 失败）


@dataclass
class CompactEvent:
    """一次压缩事件的完整记录。"""

    kind: str = EVENT_TRIGGER
    time: str = ""
    reason: str = ""            # 触发原因：over_limit / manual / focus / under_limit ...
    trigger: str = "auto"       # auto（超阈值自动）/ manual（/compact 手动）
    mode: str = "summarize"     # summarize / slide
    limit: int = 0              # 阈值（token）
    ratio: float = 0.0          # 触发比例
    used_before: int = 0
    used_after: Optional[int] = None
    dropped: int = 0            # 被压缩的消息条数
    kept: int = 0               # 逐字保留的消息条数
    grace: int = 0              # grace zone 保留的轮数
    summary_chars: int = 0
    summary: str = ""           # 摘要全文（redact 时为空）
    focus: str = ""             # 用户指定的重点（/compact focus on ...）
    prompt: str = ""            # 本次用的摘要提示词（便于复现当时的口径）
    truncated_outputs: int = 0  # 被单独截断的大块内容条数
    saved_chars: int = 0        # 因截断少喂给模型的字符数
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = self.__dict__.copy()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompactEvent":
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)  # type: ignore[arg-type]


class CompactLog:
    """追加式压缩事件日志（JSONL）。

    线程安全：压缩既可能由自动阈值触发，也可能由用户 ``/compact`` 触发，
    两者可能并发（手动压缩时恰好历史超限），因此写盘加锁。
    ``record_content=False`` 时不记摘要正文（只记长度与计数）。
    """

    def __init__(self, path: str, record_content: bool = True, max_events: int = 5000):
        self.path = path
        self.record_content = bool(record_content)
        self.max_events = max(100, int(max_events or 5000))
        self._lock = threading.Lock()

    def append(self, event: CompactEvent) -> bool:
        """追加一条事件；失败只告警（**绝不能因为日志写不进去就让压缩失败**）。"""
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
        """事件过多时只保留最近 ``max_events`` 条（防止日志无限增长）。"""
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
        """读回全部事件（坏行跳过，不因为一行坏了就读不出整份日志）。"""
        return list(iter_events(self.path))

    def stats(self) -> Dict[str, Any]:
        """汇总统计（供 /status 与设置页展示）。"""
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
    """逐行读回事件；坏行跳过并告警（日志是审计用途，不能因为一行坏就全废）。"""
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
    """回放的一步：这次压缩把摘要从 ``before`` 变成了 ``after``。"""

    time: str = ""
    trigger: str = ""
    before: str = ""
    after: str = ""
    used_before: int = 0
    used_after: Optional[int] = None
    dropped: int = 0


def replay(events: List[CompactEvent]) -> List[ReplayStep]:
    """事件溯源回放：把历次摘要拼接过程重放出来。

    会话摘要的实现是"每压缩一次就把新摘要**追加**在旧摘要后面"
    （``conv.summary = old + new``），因此回放就是按时间顺序累计拼接 ——
    这样能回答"现在这段摘要里哪几句是第几次压缩加进来的"，
    这正是"可追溯"的实际用处（用户问"你什么时候把金额那段总结丢掉的"）。
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
    """回放出"当前应当生效的摘要"（供崩溃恢复与一致性校验）。"""
    steps = replay(events)
    return steps[-1].after if steps else ""
