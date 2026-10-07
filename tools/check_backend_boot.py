# -*- coding: utf-8 -*-
"""后端启动冒烟检查：**拉起后端 → 等 ready → 发一条最小请求 → 正常退出**。

为什么需要它（v0.2.2 硬编码清查第 6 批的教训，见修复记录第 6 批（二））：
第 6 批（二）把 `platform_support.py` 的 `未知平台（…）` 本地化时，在**模块级**写了 `_t(...)`，
而该模块刻意用函数内延迟导入 i18n —— 于是 `NameError`，**后端连 `ready` 握手都发不出来**。
当时 `selftest`（155/155）与 `check_lang` **全是绿的**：没有任何一项断言"后端到底能不能起来"。
唯一拦住它的是 `tools/ui_probe_avalonia.ps1`（它等 ready 握手）—— 但那是界面探针，
需要 Avalonia 构建产物，跑得也慢。本脚本把这条断言**单独摘出来**，成为一条最小、最快、
只在后端层面就能跑的门禁。

与 `tools/test_backend_e2e.py` 的分工：那个是**完整协议回归**（多语言、指令、流式、工具…，跑几分钟）；
本脚本只回答一个问题 —— **"后端起得来、握得上手、退得干净吗"**，默认 300 秒冷启动上限，
成功时只输出几行证据。

用法：
    python tools/check_backend_boot.py                  # 源码模式（python -u backend/main.py）
    python tools/check_backend_boot.py --exe <后端可执行文件>   # 打包产物模式
    python tools/check_backend_boot.py --timeout 600     # 冷启动给更多时间
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
DEFAULT_TIMEOUT = 300.0          # 冷启动（首次加载模型）给足；CI 上偏慢也不该误报


def app_version() -> str:
    """从 backend/version.py 读版本（不写死——裁定① 的同一条纪律）。"""
    path = os.path.join(BACKEND_DIR, "version.py")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("APP_VERSION"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"读不出 APP_VERSION：{path}")


class Launched:
    """被测后端进程 + 逐行读取线程（直接 readline 会在后端不说话时永久阻塞）。"""

    def __init__(self, argv, cwd):
        self.argv = argv
        self.proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.stderr_lines: "queue.Queue[str | None]" = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self.stderr_lines),
                         daemon=True).start()

    @staticmethod
    def _pump(stream, sink):
        try:
            for line in stream:
                sink.put(line)
        except Exception:                                    # noqa: BLE001
            pass
        sink.put(None)

    def send(self, request: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def read_until(self, types: set[str], timeout: float):
        """读到指定 type 的响应；超时或进程结束返回 None。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue                                     # 非 JSON 行（进度条之类）跳过
            if not types or obj.get("type") in types:
                return obj

    def stderr_tail(self, limit: int = 12) -> str:
        rows = []
        while True:
            try:
                item = self.stderr_lines.get_nowait()
            except queue.Empty:
                break
            if item is None:
                break
            rows.append(item.rstrip())
        return "\n".join(rows[-limit:])


def main() -> int:
    parser = argparse.ArgumentParser(description="后端启动冒烟检查（ready 握手 + 最小请求 + 干净退出）")
    parser.add_argument("--exe", default="", help="被测后端可执行文件；缺省用源码模式跑 main.py")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="冷启动最长等待秒数")
    parser.add_argument("--quiet", action="store_true", help="只输出结论行")
    args = parser.parse_args()

    def say(text: str) -> None:
        if not args.quiet:
            print(text)

    if args.exe:
        exe = os.path.abspath(args.exe)
        if not os.path.exists(exe):
            print(f"[boot] FAIL 被测后端不存在：{exe}")
            return 1
        argv, cwd = [exe], os.path.dirname(exe)
    else:
        argv = [sys.executable, "-u", os.path.join(BACKEND_DIR, "main.py")]
        cwd = BACKEND_DIR

    say(f"[boot] 被测后端：{' '.join(argv)}")
    say(f"[boot] 工作目录：{cwd}（冷启动上限 {args.timeout:.0f} 秒）")

    problems: list[str] = []
    started = time.monotonic()
    launched = Launched(argv, cwd)
    try:
        # 1) ready 握手
        ready = launched.read_until({"ready", "error", "need_download"}, timeout=args.timeout)
        elapsed = time.monotonic() - started
        if ready is None:
            problems.append(f"{args.timeout:.0f} 秒内没有 ready 握手（进程可能启动即崩）")
            say(f"[boot] FAIL 没有 ready 握手（等了 {elapsed:.1f} 秒）")
        else:
            kind = ready.get("type")
            if kind != "ready":
                problems.append(f"首个握手不是 ready 而是 {kind}：{str(ready)[:160]}")
            else:
                version = str(ready.get("version") or "")
                expected = app_version()
                say(f"[boot] OK   ready 握手（用时 {elapsed:.1f} 秒，版本 {version}）")
                if version != expected:
                    problems.append(f"ready 里的版本 {version!r} 与 backend/version.py 的 {expected!r} 不一致")
                if not ready.get("i18n"):
                    problems.append("ready 里没有 i18n 信息")
                say(f"[boot] OK   版本与 version.py 一致（{expected}），带 i18n 信息")

            # 2) 最小请求：`context` 是只读的（返回上下文占用），不需要模型也不改状态。
            #    用真实存在的动作名，而不是随便发一个 —— 否则响应会是"未知动作"，
            #    那就只证明了"stdin/stdout 通"，没证明"请求分发通"。
            #    注意它回的是 `{"type": "context", …}`，所以要按**它自己的类型**等。
            launched.send({"action": "context"})
            reply = launched.read_until({"context", "system", "error"}, timeout=60)
            if reply is None:
                problems.append("context 请求 60 秒内没有响应")
                say("[boot] FAIL context 请求没有响应")
            else:
                content = str(reply.get("content") or "")
                if "未知动作" in content:
                    problems.append(f"context 是未知动作（协议分发没接上）：{content[:80]}")
                    say(f"[boot] FAIL context 被当成未知动作：{content[:60]}")
                elif not (reply.get("used") is not None or reply.get("limit") is not None):
                    problems.append(f"context 响应里没有 used/limit：{str(reply)[:120]}")
                    say(f"[boot] FAIL context 响应缺少字段：{str(reply)[:80]}")
                else:
                    say(f"[boot] OK   context 响应：used={reply.get('used')} "
                        f"limit={reply.get('limit')} n_ctx={reply.get('n_ctx')}")

        # 3) 干净退出：关掉 stdin，后端应在 30 秒内自己收场
        if launched.proc.stdin is not None:
            launched.proc.stdin.close()
        try:
            code = launched.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            launched.proc.kill()
            launched.proc.wait(timeout=10)
            problems.append("关闭 stdin 后 30 秒内没有退出（已强制结束）")
            code = None
        if code is not None:
            say(f"[boot] OK   进程正常退出（退出码 {code}）")
            if code != 0:
                problems.append(f"退出码非 0：{code}")
    finally:
        if launched.proc.poll() is None:
            launched.proc.kill()
            launched.proc.wait(timeout=10)

    if problems:
        print()
        print("[boot] 失败项：")
        for item in problems:
            print(f"   ✗ {item}")
        tail = launched.stderr_tail()
        if tail:
            print("[boot] 后端 stderr 尾部：")
            for line in tail.splitlines():
                print(f"   | {line}")
        print(f"[boot] 结论：失败 {len(problems)} 项")
        return 1
    print(f"[boot] 结论：通过（总耗时 {time.monotonic() - started:.1f} 秒）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
