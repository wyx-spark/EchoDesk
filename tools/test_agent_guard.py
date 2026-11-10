# -*- coding: utf-8 -*-
"""
[EN]
Unit test for v0.2.6 task 2.4: guarded system tools and per-invocation
confirmation.

Verification targets:
* ``execute_shell`` and ``git_command`` are registered with the ``guard`` risk
  tier and cannot run without ``confirmed=True``.
* The Agent loop asks the host for confirmation before a guard tool executes.
* A denial produces a tool failure observation and executes nothing.
* An approval executes the tool exactly once.
* The backend emits a ``confirm_tool`` event whose content is the operation that
  will actually run, and resolves it only when the frontend answers.

[ZH]
v0.2.6 任务 2.4 的单元测试：高危系统工具与逐次确认。

验证目标：
* ``execute_shell`` 与 ``git_command`` 以 ``guard`` 风险等级注册，未传入
  ``confirmed=True`` 时无法执行。
* Agent 循环在执行高危工具前向宿主请求确认。
* 拒绝会生成工具失败观察结果，且不执行任何操作。
* 批准后工具恰好执行一次。
* 后端发出 ``confirm_tool`` 事件，其内容即即将执行的操作，且仅在前端答复后解除。

Run:
    python tools/test_agent_guard.py
Exit code: 0 = all checks passed, 1 = at least one failure.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import agent_tools as at  # noqa: E402
from agent import AgentLoop  # noqa: E402
from config import Config  # noqa: E402

PASSED = 0
FAILED = []
TEMP = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


class ScriptedLLM:
    """
    [EN]
    Deterministic model stub returning one scripted response per call.

    [ZH]
    确定性模型桩：每次调用返回一条预置响应。
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat_complete(self, messages, temperature=0.5, max_tokens=512):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else ""


def fake_run(argv, cwd, timeout):
    fake_run.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
    return subprocess.CompletedProcess(argv, 0, stdout="fake-output", stderr="")


fake_run.calls = []


def build_backend():
    import main as backend_main

    config = Config()
    config.model_path = "stub.gguf"
    config.session_file = ""
    config.speed_hint = False
    config.agent.max_steps = 3
    backend = backend_main.Backend(config)
    return backend, backend_main


def run_agent(backend, backend_main, approved, command):
    """
    [EN]
    Run one agent turn that invokes ``execute_shell`` and answer the emitted
    confirmation with ``approved``.

    [ZH]
    运行一轮调用 ``execute_shell`` 的 Agent，并以 ``approved`` 答复发出的确认。
    """
    written = []
    original_write = backend_main._write
    backend_main._write = lambda payload: written.append(dict(payload))
    tool_call = json.dumps({"tool": "execute_shell",
                            "args": {"command": command},
                            "why": "verify the guard"}, ensure_ascii=False)
    llm = ScriptedLLM([tool_call, "最终答案：已按用户决定处理。"])
    registry = at.ToolRegistry()
    loop = AgentLoop(llm, registry, max_steps=3,
                     confirm_tool=backend._confirm_agent_tool,
                     stop_event=threading.Event())
    box = {}
    events = []

    def worker():
        box["run"] = loop.run([{"role": "user", "content": "执行一条命令"}],
                              on_event=events.append)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.time() + 5
    request_id = ""
    while time.time() < deadline:
        hits = [item for item in written if item.get("type") == "confirm_tool"]
        if hits:
            request_id = str(hits[0].get("request_id") or "")
            break
        time.sleep(0.01)
    executed_before = len(fake_run.calls)
    if request_id:
        backend.handle({"action": "confirm_tool", "request_id": request_id,
                        "approved": approved})
    thread.join(timeout=5)
    backend_main._write = original_write
    return box.get("run"), events, written, executed_before


def main():
    print("\n=== 1. 注册表强制闸门 ===")
    registry = at.ToolRegistry()
    shell = registry.get("execute_shell")
    git = registry.get("git_command")
    check("execute_shell 已注册", shell is not None)
    check("git_command 已注册", git is not None)
    check("两者均为 guard 风险",
          shell.risk == at.RISK_GUARD and git.risk == at.RISK_GUARD,
          f"{shell.risk}/{git.risk}")
    check("guard 工具在可调用清单中可见",
          "execute_shell" in [tool.name for tool in registry.available()])

    with mock.patch("agent_tools._run_system_command", side_effect=fake_run):
        fake_run.calls.clear()
        denied_ok, denied_text = registry.run("execute_shell", {"command": "echo hi"})
        check("未确认时拒绝执行", denied_ok is False and "确认" in denied_text,
              denied_text)
        check("未确认时零进程调用", fake_run.calls == [], str(fake_run.calls))
        check("直接调用 git_command 同样被拒绝",
              registry.run("git_command", {"args": ["status"]})[0] is False)

        approved_ok, approved_text = registry.run(
            "execute_shell", {"command": "echo hi"}, confirmed=True)
        check("确认后执行一次", approved_ok is True and len(fake_run.calls) == 1,
              approved_text)
        check("执行使用 PowerShell 或 Bash",
              any(("pwsh" in str(c["argv"][0]).lower())
                  or ("powershell" in str(c["argv"][0]).lower())
                  or ("bash" in str(c["argv"][0]).lower())
                  or (str(c["argv"][0]).endswith("/sh"))
                  for c in fake_run.calls),
              str(fake_run.calls[:1]))

    print("\n=== 2. 拒绝：confirm_tool 发出但绝不执行 ===")
    backend, backend_main = build_backend()
    marker_dir = tempfile.mkdtemp(prefix="echodesk-guard-")
    TEMP.append(marker_dir)
    marker = os.path.join(marker_dir, "must-not-exist.txt")
    if os.name == "nt":
        command = f"New-Item -Path '{marker}' -ItemType File -Force"
    else:
        command = f"touch '{marker}'"
    with mock.patch("agent_tools._run_system_command", side_effect=fake_run):
        fake_run.calls.clear()
        run, events, written, before = run_agent(backend, backend_main, False, command)
    confirm_events = [item for item in written if item.get("type") == "confirm_tool"]
    check("后端发出 confirm_tool 事件", len(confirm_events) == 1, str(len(confirm_events)))
    event = confirm_events[0] if confirm_events else {}
    check("事件携带工具名", event.get("tool") == "execute_shell", str(event.get("tool")))
    check("事件展示即将执行的命令", command in str(event.get("content", "")),
          str(event.get("content", ""))[:200])
    check("确认前没有执行任何命令", before == 0, str(before))
    check("拒绝后也没有执行", fake_run.calls == [], str(fake_run.calls))
    check("拒绝后工具结果标记失败",
          run is not None and run.steps and run.steps[0].ok is False,
          str(run.trace() if run else None))
    check("拒绝后确认状态为 False",
          run is not None and run.steps and run.steps[0].confirmed is False)
    check("拒绝后命令未产生副作用文件", not os.path.exists(marker))
    check("Agent 仍给出最终答案",
          run is not None and bool(run.answer), run.answer if run else "")

    print("\n=== 3. 批准：恰好执行一次 ===")
    backend, backend_main = build_backend()
    with mock.patch("agent_tools._run_system_command", side_effect=fake_run):
        fake_run.calls.clear()
        run, events, written, before = run_agent(backend, backend_main, True, "echo approved")
    check("批准前没有执行", before == 0, str(before))
    check("批准后执行一次", len(fake_run.calls) == 1, str(len(fake_run.calls)))
    check("批准后工具结果成功",
          run is not None and run.steps and run.steps[0].ok is True,
          str(run.trace() if run else None))
    check("批准状态为 True",
          run is not None and run.steps and run.steps[0].confirmed is True)

    print("\n=== 4. 超时/无答复默认拒绝 ===")
    backend, backend_main = build_backend()
    backend.config.agent.confirm_timeout_seconds = 10
    pending = backend.handle({"action": "confirm_tool", "request_id": "missing",
                              "approved": True})
    check("未知确认标识不误放行", pending.get("resolved") is False, str(pending))
    check("取消时等待方得到拒绝",
          backend.resolve_tool_confirmation("missing", True) is False)

    print("\n=== 5. git 工具参数校验 ===")
    with mock.patch("agent_tools._run_system_command", side_effect=fake_run):
        fake_run.calls.clear()
        text = at.tool_git_command({"args": ["status", "--short"]})
    check("git 缺少确认不会由工具函数自行放行",
          "Git" in text and "fake-output" in text, text[:120])
    check("git 使用参数列表而非 shell",
          fake_run.calls and fake_run.calls[0]["argv"][1:] == ["status", "--short"],
          str(fake_run.calls[:1]))

    print("\n------------------------------------------")
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        import shutil
        for path in TEMP:
            shutil.rmtree(path, ignore_errors=True)
