# -*- coding: utf-8 -*-
"""MCP 客户端测试（v0.2.1 能力扩展-1 任务三）。

任务书的验证方式是"配置一个 MCP Server，确认工具可被发现、可被调用、可运行时启停"。
这里**不 mock 协议**：直接拉起随包的示例服务器
（`examples/mcp/echo_server.py`，纯标准库）当真实对端，走真的 stdio JSON-RPC。
所以这组测试同时验证了三件事：客户端能工作、示例服务器是能跑的、协议本身对得上。

| 任务书要求 | 对应断言 |
| --- | --- |
| MCP 客户端能连接并调用外部工具 | 握手、`tools/list`、`tools/call` 真跑通 |
| 运行时启停，无需重启 | 禁用 server → 工具立刻消失；启用 → 重新出现；配置落盘 |
| 工具列表可见 | `mcp_state` 返回每个 server 的工具与连接状态 |
| 权限走统一检查 | MCP 工具一律 `sensitive`（默认不可调用），授权后才可调 |
| 与技能系统区分 | 桥接复用 Agent 的 `ToolRegistry`（同一套权限/审计） |
| 数据流向说清楚 | 默认不透传父进程环境变量（含密钥），只透传白名单里的 |

跑法：``python tools\\test_mcp.py``；退出码 0 = 全部通过。
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import mcp_client as mc       # noqa: E402
import agent_tools as at      # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


SERVER = os.path.join(ROOT, "examples", "mcp", "echo_server.py")


def make_server(name="echo", extra_args=None, enabled=True):
    return mc.McpServer(name=name, command=sys.executable,
                        args=[SERVER] + list(extra_args or []), enabled=enabled)


print("=== 1. 与示例服务器握手、发现工具、调用工具（真 stdio）===")
client = mc.StdioMcpClient(make_server(), timeout=20)
check("示例服务器文件存在", os.path.isfile(SERVER))
connected = client.connect()
check("能拉起进程并完成 initialize", connected, client.last_error)
check("记录了服务端信息", bool(client.server_info.get("name")), str(client.server_info))
tools = client.list_tools()
_names = [tool.get("name") for tool in tools]
check("tools/list 返回了两个工具", _names == ["echo_text", "now"], str(_names))
check("工具带 description", all(tool.get("description") for tool in tools))
check("工具带 inputSchema（模型据此填参数）",
      all(isinstance(tool.get("inputSchema"), dict) for tool in tools))
echoed = client.call_tool("echo_text", {"text": "你好，MCP"})
check("tools/call 回显正确", echoed == "你好，MCP", repr(echoed))
now_text = client.call_tool("now", {"tz_offset_hours": 8})
check("第二个工具也能调用（带参数）", "UTC+8" in now_text and ":" in now_text, now_text)
check("同一进程复用（多次调用不必重连）", client.initialized and client._proc.poll() is None)
client.close()
check("close 之后进程已结束", client._proc is None)

print("=== 2. 错误处理：服务端报错 / 命令不存在 / 超时 ===")
client2 = mc.StdioMcpClient(make_server(), timeout=15)
client2.connect()
try:
    client2.call_tool("不存在的工具", {})
    check("调用未知工具会报错", False, "竟然没报错")
except mc.McpError as exc:
    check("调用未知工具会报错", "未知工具" in str(exc) or "错误" in str(exc), str(exc))
try:
    client2.call_tool("echo_text", {"text": 123})
    check("参数类型错误被服务端拒绝", False, "竟然没报错")
except mc.McpError as exc:
    check("参数类型错误被服务端拒绝", "text" in str(exc) or "错误" in str(exc), str(exc))
client2.close()

_bad = mc.StdioMcpClient(mc.McpServer(name="bad", command="definitely-not-a-real-command-xyz"))
check("命令不存在时 connect 返回 False 并给出原因",
      _bad.connect() is False and bool(_bad.last_error), _bad.last_error)
check("没有 command 时也不抛异常",
      mc.StdioMcpClient(mc.McpServer(name="empty", command="")).connect() is False)

_slow = mc.StdioMcpClient(mc.McpServer(
    name="slow", command=sys.executable,
    # 读 stdin 但**永不回应**：这才是"对端卡住"的形状。用 sleep 的话进程会直接
    # 退出（不读 stdin），测到的是"进程已退出"而不是超时路径。
    args=["-c", "import sys\nfor _ in sys.stdin:\n    pass\n"]), timeout=2)
_started = time.time()
check("对端不响应时按超时返回（不会永久挂住）",
      _slow.connect() is False and (time.time() - _started) < 15, _slow.last_error)
check("超时原因写进了 last_error", "超时" in _slow.last_error, _slow.last_error)
_slow.close()

print("=== 3. 桥接：工具进同一个 Agent 注册表，且默认按敏感处理 ===")
bridge = mc.McpBridge(servers=[make_server()], timeout=20, auto_connect=True)
registry = at.ToolRegistry()
registered = mc.register_mcp_tools(registry, bridge)
check("注册了 2 个 MCP 工具", registered == 2, str(registered))
check("工具名带 server 前缀（避免与内置重名）",
      registry.get("mcp__echo__echo_text") is not None, str(registry.names()))
check("MCP 工具默认按敏感处理（未授权不可调用）",
      registry.get("mcp__echo__echo_text").risk == at.RISK_SENSITIVE)
_ok, _text = registry.run("mcp__echo__echo_text", {"text": "x"})
check("未授权时给出「怎么开」的说明", _ok is False and "未启用" in _text, _text[:80])
check("授权后可以调用",
      registry.allow("mcp__echo__echo_text")
      and registry.run("mcp__echo__echo_text", {"text": "通了"})[1] == "通了")
check("工具描述里带 server 名（多 server 时可分辨）",
      "[echo]" in registry.get("mcp__echo__now").description)
check("MCP 工具失败时被判为失败（不是「用过了但没结果」）",
      registry.run("mcp__echo__echo_text", {"text": 123})[0] is False
      and "失败" in registry.run("mcp__echo__echo_text", {"text": 123})[1],
      str(registry.run("mcp__echo__echo_text", {"text": 123})))
check("public_list 里能看到风险与启用状态",
      any(item["name"].startswith("mcp__") and item["risk"] == "sensitive"
          for item in registry.public_list()))
_auto_off = mc.McpBridge(servers=[make_server()], auto_connect=False)
check("auto_connect=False 时不注册（不擅自拉起外部进程）",
      mc.register_mcp_tools(at.ToolRegistry(), _auto_off) == 0)
bridge.close()

print("=== 4. 环境变量：默认不透传父进程的密钥 ===")
os.environ["ECHODESK_TEST_SECRET_TOKEN"] = "should-not-leak"
_server = make_server()
_client = mc.StdioMcpClient(_server)
_env = _client._child_env()
check("默认不把普通环境变量交给子进程", "ECHODESK_TEST_SECRET_TOKEN" not in _env)
check("最小集合里保留了 PATH（否则命令都起不来）", "PATH" in _env)
_server.passthrough_env = ["ECHODESK_TEST_SECRET_TOKEN"]
check("用户点名透传的才给", "ECHODESK_TEST_SECRET_TOKEN" in mc.StdioMcpClient(_server)._child_env())
del os.environ["ECHODESK_TEST_SECRET_TOKEN"]

print("=== 5. 后端接线：运行时启停（无需重启）===")
import config as cf          # noqa: E402
import i18n                  # noqa: E402
import main as backend_main  # noqa: E402

i18n.set_locale("zh-Hans")
workdir = tempfile.mkdtemp(prefix="echodesk-mcp-")
os.chdir(workdir)
# 任务十五（自测问题修复-1）：`Config.save()` 已改为**写回被读出来的那一份**，
# 而 `Config._find()` 在 cwd 找不到配置文件时会落到**程序目录**的真配置上 ——
# 那就会把 MCP 服务器等测试数据写进仓库里的 `backend/config.json`（实测踩到：
# 仓库里凭空多出一个指向 examples/mcp/echo_server.py 的 echo 服务器）。
# 所以隔离必须靠"**cwd 里真的有一份配置**"实现；旧代码依赖的是"save() 只写 cwd"，
# 而那条行为已经作为缺陷修掉了。这里用 open() 铺，不用 save()（它正是被测对象）。
with open(os.path.join(workdir, "config.json"), "w", encoding="utf-8") as _fh:
    json.dump(cf.Config().model_dump(), _fh, ensure_ascii=False)


class StubEngine(backend_main.LLMEngine):
    def __init__(self):
        self.reasoning_enabled = False
        self.reasoning_tags = []
        self.model_path = "stub.gguf"
        self.n_ctx = 8192
        self.n_ctx_train = 32768
        self.kv_cache_bytes = 0
        self.last_tps = 0.0
        self.last_elapsed = 0.0
        self.last_tokens = 0

    @property
    def ready(self):
        return True

    def speed_hint(self, *a, **k):
        return ""

    def kv_cache_text(self):
        return "0 B"


config = cf.Config()
config.model_path = "stub.gguf"
config.speed_hint = False
backend = backend_main.Backend(config)
backend.llm = StubEngine()

_cfg = {"name": "echo", "command": sys.executable, "args": [SERVER], "enabled": True}
applied = backend.handle({"action": "mcp_apply", "enabled": True, "auto_connect": True,
                          "servers": [_cfg]})
check("mcp_apply 注册了工具", (applied.get("mcp_apply") or {}).get("registered") == 2,
      str(applied.get("mcp_apply")))
_tool_names = [item["name"] for item in (applied.get("agent") or {}).get("tools", [])]
check("Agent 工具表里出现 MCP 工具",
      "mcp__echo__echo_text" in _tool_names and "calc" in _tool_names, str(_tool_names))
check("mcp_state 报告连接成功与工具数",
      any(s["name"] == "echo" and s["connected"] and s["tool_count"] == 2
          for s in (applied.get("mcp") or {}).get("servers", [])),
      str((applied.get("mcp") or {}).get("servers")))
check("配置落盘（重启后仍在）",
      json.load(open(os.path.join(workdir, "config.json"), encoding="utf-8"))["mcp"]["enabled"] is True)

_off = backend.handle({"action": "mcp_apply", "enabled": False, "servers": []})
check("运行时停用后工具立刻消失",
      not [n for n in [(item["name"]) for item in (_off.get("agent") or {}).get("tools", [])]
           if n.startswith("mcp__")],
      str([item["name"] for item in (_off.get("agent") or {}).get("tools", [])]))
check("停用后状态里不再有 server", (_off.get("mcp") or {}).get("servers") == [],
      str((_off.get("mcp") or {}).get("servers")))

_back_on = backend.handle({"action": "mcp_apply", "enabled": True, "auto_connect": True,
                           "servers": [_cfg]})
check("重新启用后工具回来（不需要重启进程）",
      (back_on_registered := (_back_on.get("mcp_apply") or {}).get("registered")) == 2,
      str(back_on_registered))

# 界面保存不能抹掉手写的 env（前端联调发现的真实数据丢失路径）：
# mcp_state 必须回传 env，且 mcp_apply 收到"没带 env"的条目时要与既有配置合并。
_with_env = dict(_cfg, env={"MY_KEY": "1"}, passthrough_env=["PATH"])
backend.handle({"action": "mcp_apply", "enabled": True, "servers": [_with_env]})
_state = backend.mcp_diagnostics()
check("mcp_state 回传 env（界面据此原样回传）",
      (_state["servers"][0].get("env") or {}).get("MY_KEY") == "1",
      str(_state["servers"][0]))
backend.handle({"action": "mcp_apply", "enabled": True, "timeout_seconds": 30,
                "servers": [{"name": "echo", "command": sys.executable, "args": [SERVER],
                             "enabled": True}]})          # 模拟不带 env 的旧客户端
check("没带 env 的保存请求会与既有配置合并（不静默抹掉）",
      (json.load(open(os.path.join(workdir, "config.json"), encoding="utf-8"))
       ["mcp"]["servers"][0].get("env") or {}).get("MY_KEY") == "1",
      str(json.load(open(os.path.join(workdir, "config.json"), encoding="utf-8"))["mcp"]))
check("非法 server（缺 command）被拒绝并提示",
      "❌" in str(backend.handle({"action": "mcp_apply", "servers": [{"name": "坏"}]}).get("content", "")),
      str(backend.handle({"action": "mcp_apply", "servers": [{"name": "坏"}]}).get("content"))[:80])
check("非数字超时被拒绝",
      "❌" in str(backend.handle({"action": "mcp_apply", "timeout_seconds": "soon"}).get("content", "")))
backend.mcp_bridge().close()

print()
print("=" * 60)
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
sys.exit(0 if not FAILED else 1)
