"""
[EN]
Exclusive lock for the release pipeline: packaging and the release gate must never run at the same
time. Both touch the same ``publish/`` and ``dist/`` trees; v0.2.7 hit this when a packaging run
overlapped a gate run that was executing a backend inside ``dist/``, and the resulting archives
carried runtime state files.

A leftover lock from a crashed process is taken over when its PID no longer exists, so a crash
cannot block the pipeline forever.

[ZH]
发布流水线的互斥锁：打包与发布门禁不得同时运行。两者都会读写同一份 ``publish/`` 与 ``dist/`` 树；
v0.2.7 就撞上过"打包与门禁并发"——门禁正在 ``dist/`` 里跑后端，打包把它的残留一起归档了。

崩溃进程留下的锁会在其 PID 已不存在时被接管，因此崩溃不会永久阻塞流水线。
"""
from __future__ import annotations

import json
import os
import socket
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(ROOT, "artifacts", ".pipeline.lock")


class PipelineBusy(RuntimeError):
    """[EN] Another packaging/gate process owns the pipeline.
    [ZH] 另一个打包或门禁进程持有流水线。"""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # [EN] ``os.kill(pid, 0)`` on Windows is not a liveness probe (it can terminate the
        # [EN] process); use OpenProcess + GetExitCodeProcess instead.
        # [ZH] Windows 上 ``os.kill(pid, 0)`` 不是存活探测（它可能直接结束进程）；
        # [ZH] 改用 OpenProcess + GetExitCodeProcess。
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_owner():
    try:
        with open(LOCK_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001 - unreadable lock is treated as stale
        return None


class PipelineLock:
    """[EN] Context manager taking the pipeline lock for ``role``.
    [ZH] 以 ``role`` 身份获取流水线锁的上下文管理器。"""

    def __init__(self, role: str) -> None:
        self.role = role
        self.owned = False

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        payload = json.dumps({
            "pid": os.getpid(),
            "role": self.role,
            "host": socket.gethostname(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, ensure_ascii=False)
        for _ in range(2):
            try:
                fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = _read_owner()
                # [EN] Nested entry: the gate (or build_release) holds the lock and invokes this
                # [EN] script as a child. A child of the owner must be allowed to run; it does not
                # [EN] take over ownership, so it will not delete the parent's lock either.
                # [ZH] 嵌套入口：门禁（或 build_release）持锁并把它作为子进程调用。持有者的子进程
                # [ZH] 必须被放行；它不接管所有权，因此也不会删掉父进程的锁。
                if owner and int(owner.get("pid") or 0) == os.getppid():
                    self.owned = False
                    return
                alive = owner and _pid_alive(int(owner.get("pid") or 0))
                if alive:
                    raise PipelineBusy(
                        "流水线被占用：pid={0} role={1} started_at={2}；打包与门禁不得并发运行。"
                        .format(owner.get("pid"), owner.get("role"), owner.get("started_at")))
                # [EN] Stale lock from a crashed process: take it over.
                # [ZH] 崩溃进程留下的锁：接管。
                try:
                    os.remove(LOCK_PATH)
                except OSError:
                    pass
                continue
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                self.owned = True
                return
        raise PipelineBusy("无法获取流水线锁（连续两次被占用）：" + LOCK_PATH)

    def release(self) -> None:
        if not self.owned:
            return
        owner = _read_owner()
        if owner and int(owner.get("pid") or 0) == os.getpid():
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass
        self.owned = False

    def __enter__(self) -> "PipelineLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False
