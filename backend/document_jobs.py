"""
[EN]
Background document job scheduling for long-document parsing and chunked
summarization.

Design constraints:
* A single worker thread processes jobs strictly in submission order, so two
  documents can never compete for the local model, whose inference is
  serialized by design.
* Submission returns immediately with a job identifier; the caller (the
  protocol dispatcher) therefore answers the upload request without waiting for
  parsing or summarization.
* Every progress entry and terminal state is retained in the job object, so the
  front end can poll for incremental events without an unsolicited stdout
  channel. The global stdout stream remains reserved for request/response
  pairs.
* Cancellation is cooperative: the runner receives the job object and must
  observe ``job.cancel_event`` between expensive operations.

[ZH]
长文档解析与分段摘要的后台作业调度。

设计约束：
* 单个工作线程严格按提交顺序处理作业，两份文档不会同时争用本机模型；模型
  推理本身即按串行设计。
* 提交后立即返回作业标识，调用方（协议分发层）无需等待解析或摘要即可应答
  上传请求。
* 每条进度与终止状态都保存在作业对象内，前端可轮询增量事件，无需额外的
  主动 stdout 通道；全局 stdout 仍只承载请求/响应对。
* 取消为协作式：执行函数持有作业对象，必须在耗时操作之间检查
  ``job.cancel_event``。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("EchoDesk.DocumentJobs")

#: [EN] Terminal states of a document job.
#: [ZH] 文档作业的终止状态。
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_TERMINAL = {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED}


@dataclass
class DocumentJob:
    """
    [EN]
    One background document-processing job.

    ``events`` is an append-only list guarded by ``_lock``. Each entry is a
    protocol-ready dictionary that already carries ``job_id``, so polling
    clients can forward it without further decoration.

    [ZH]
    单个后台文档处理作业。

    ``events`` 是由 ``_lock`` 保护的只增列表。每条记录都是可直接进入协议的
    字典，且已携带 ``job_id``，轮询客户端无需再加工即可转发。
    """

    id: str
    paths: List[str]
    # [EN] Job kind (v0.2.6 Part 3). ``documents`` is the original upload path;
    # [EN] ``knowledge_index`` and ``memory_extract`` share the same queue so
    # [EN] that heavy local work never runs on the protocol thread.
    # [ZH] 作业种类（v0.2.6 Part 3）。``documents`` 是原有的上传路径；
    # [ZH] ``knowledge_index`` 与 ``memory_extract`` 共用同一队列，使繁重的本地
    # [ZH] 工作绝不在协议线程上运行。
    kind: str = "documents"
    # [EN] Kind-specific input. ``documents`` ignores it; the knowledge job reads
    # [EN] the library identifier and the memory job reads a transcript snapshot
    # [EN] captured at conversation end.
    # [ZH] 种类相关的输入。``documents`` 忽略该字段；知识库作业读取库标识，记忆
    # [ZH] 作业读取对话结束时捕获的文本快照。
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    status: str = STATUS_QUEUED
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _events: List[Dict[str, Any]] = field(default_factory=list, repr=False)

    def emit(self, event: Dict[str, Any]) -> None:
        """
        [EN]
        Append one structured event to the job trace.

        [ZH]
        向作业轨迹追加一条结构化事件。
        """
        payload = dict(event)
        payload["job_id"] = self.id
        payload.setdefault("timestamp", time.time())
        with self._lock:
            self._events.append(payload)

    def events_since(self, cursor: int = 0) -> Dict[str, Any]:
        """
        [EN]
        Return the events appended after ``cursor`` together with the next
        cursor value, so a polling client can request only new entries.

        [ZH]
        返回 ``cursor`` 之后追加的事件及下一个游标值，使轮询客户端只获取
        新条目。
        """
        with self._lock:
            total = len(self._events)
            start = max(0, min(int(cursor or 0), total))
            return {"events": [dict(item) for item in self._events[start:]],
                    "cursor": total}

    def snapshot(self, cursor: int = 0) -> Dict[str, Any]:
        """
        [EN]
        Machine-readable job state for diagnostics and polling.

        [ZH]
        供诊断与轮询使用的机器可读作业状态。
        """
        payload = self.events_since(cursor)
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "paths": list(self.paths),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": dict(self.result),
            "events": payload["events"],
            "cursor": payload["cursor"],
        }

    @property
    def terminal(self) -> bool:
        """
        [EN]
        Whether the job has reached a terminal state.

        [ZH]
        作业是否已到达终止状态。
        """
        return self.status in _TERMINAL


class DocumentJobManager:
    """
    [EN]
    Serial background job queue with a single daemon worker.

    A custom daemon thread is used instead of ``ThreadPoolExecutor`` so that an
    in-flight summarization can never keep the backend process alive after the
    user quits: the executor's worker threads are non-daemon and are joined at
    interpreter shutdown.

    [ZH]
    单工作线程的串行后台作业队列。

    这里使用自定义守护线程而非 ``ThreadPoolExecutor``：后者的工作线程不是
    守护线程且会在解释器退出时被等待，用户退出后仍在运行的摘要可能因此阻止
    后端进程结束。
    """

    def __init__(self, runner: Callable[[DocumentJob], None]) -> None:
        self._runner = runner
        self._pending: "queue.Queue[Optional[DocumentJob]]" = queue.Queue()
        self._jobs: Dict[str, DocumentJob] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._loop, name="echodesk-document-jobs", daemon=True,
        )
        self._thread.start()

    # [EN] ---------- Public API ----------
    # [ZH] ---------- 对外接口 ----------

    def submit(self, paths: List[str], kind: str = "documents",
               payload: Optional[Dict[str, Any]] = None) -> DocumentJob:
        """
        [EN]
        Enqueue a job and return it immediately.

        ``kind`` selects the runner branch (document upload, knowledge indexing
        or memory extraction); ``payload`` carries kind-specific input such as a
        library identifier or a transcript snapshot. Both default to the
        original behaviour, so existing callers need no change.

        [ZH]
        将作业入队并立即返回。

        ``kind`` 选择执行分支（文档上传、知识库建索引或记忆提取）；``payload``
        承载与种类相关的输入，例如库标识或对话文本快照。两者默认即原有行为，
        因此既有调用方无需修改。
        """
        with self._lock:
            self._sequence += 1
            job_id = f"doc-{self._sequence}"
            job = DocumentJob(id=job_id, paths=[str(p) for p in (paths or [])],
                              kind=str(kind or "documents"),
                              payload=dict(payload or {}))
            self._jobs[job_id] = job
            self._order.append(job_id)
        self._pending.put(job)
        log.info("后台作业已入队：%s（种类 %s，%d 个路径）",
                 job_id, job.kind, len(job.paths))
        return job

    def get(self, job_id: str) -> Optional[DocumentJob]:
        """
        [EN]
        Look up a job by identifier; returns ``None`` for unknown identifiers.

        [ZH]
        按标识查询作业；未知标识返回 ``None``。
        """
        with self._lock:
            return self._jobs.get(str(job_id or ""))

    def cancel(self, job_id: str) -> bool:
        """
        [EN]
        Request cooperative cancellation. Returns whether the identifier matched
        a job that has not finished yet.

        [ZH]
        请求协作式取消。返回标识是否匹配到尚未结束的作业。
        """
        job = self.get(job_id)
        if job is None or job.terminal:
            return False
        job.cancel_event.set()
        job.emit({"type": "job_cancelled", "status": STATUS_CANCELLED})
        return True

    def status(self) -> Dict[str, Any]:
        """
        [EN]
        Aggregate counters for diagnostics.

        [ZH]
        供诊断使用的汇总计数。
        """
        with self._lock:
            jobs = list(self._jobs.values())
        return {
            "total": len(jobs),
            "active": sum(1 for job in jobs if not job.terminal),
            "queued": sum(1 for job in jobs if job.status == STATUS_QUEUED),
            "done": sum(1 for job in jobs if job.status == STATUS_DONE),
            "failed": sum(1 for job in jobs if job.status == STATUS_FAILED),
            "cancelled": sum(1 for job in jobs if job.status == STATUS_CANCELLED),
        }

    def shutdown(self) -> None:
        """
        [EN]
        Stop accepting work and wake the worker. Running summarization is not
        interrupted abruptly; cooperative cancellation remains the caller's
        responsibility.

        [ZH]
        停止接受新作业并唤醒工作线程。正在运行的摘要不会被强制中断；协作式
        取消仍由调用方负责。
        """
        if self._closed:
            return
        self._closed = True
        self._pending.put(None)

    # [EN] ---------- Worker loop ----------
    # [ZH] ---------- 工作线程 ----------

    def _loop(self) -> None:
        """
        [EN]
        Consume jobs in submission order until shutdown is requested.

        [ZH]
        按提交顺序消费作业，直至收到关闭请求。
        """
        while True:
            job = self._pending.get()
            if job is None:
                return
            if job.cancel_event.is_set():
                job.status = STATUS_CANCELLED
                job.finished_at = time.time()
                job.emit({"type": "job_done", "status": STATUS_CANCELLED})
                continue
            job.status = STATUS_RUNNING
            job.started_at = time.time()
            job.emit({"type": "job_start", "status": STATUS_RUNNING,
                      "paths": list(job.paths)})
            try:
                self._runner(job)
            except Exception as exc:  # noqa: BLE001 - the job must not kill the worker
                job.status = STATUS_FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                log.exception("文档后台作业失败: %s", job.id)
                job.emit({"type": "job_error", "status": STATUS_FAILED,
                          "error": job.error})
            else:
                if job.status not in _TERMINAL:
                    job.status = STATUS_DONE
            finally:
                job.finished_at = time.time()
                if job.status != STATUS_CANCELLED:
                    job.emit({"type": "job_done", "status": job.status,
                              "elapsed": round(job.finished_at - job.started_at, 3)})
                log.info("文档后台作业结束: %s（%s，%.1f 秒）", job.id, job.status,
                         job.finished_at - job.started_at)
