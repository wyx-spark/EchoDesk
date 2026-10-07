"""MCP 客户端（v0.2.1 能力扩展-1 任务三）。

**依赖选择与体积影响（任务书明确要求给出的判断）**

结论：**自研极小的 stdio JSON-RPC 客户端，不引入 `mcp` 官方包。**

* 官方的 ``mcp`` Python 包会带进 ``pydantic``（已有）、``anyio``、``httpx``、
  ``sse-starlette``、``starlette`` 等一串依赖，其中服务端相关的东西对本工具
  （只需要**客户端**）完全无用。发布物已经按 350–400 MB 的区间在压，
  为了一个"用户主动配置才用得上"的功能再进十几个包不划算。
* MCP 的**传输层本体是 JSON-RPC 2.0**：一行一个 JSON 对象走 stdin/stdout、
  内容是 ``initialize`` / ``tools/list`` / ``tools/call`` 三个方法。这一层自研
  大约 200 行，全部可读、可测（``tools/test_mcp.py`` 会拉起随包的示例服务器真跑一遍）。
* 代价（如实记录）：本版**只支持 stdio 传输**。HTTP/SSE 传输、resources/prompts
  两类能力、采样（sampling）与 roots 都不支持。要用上这些，将来可以换成官方包，
  届时 :class:`McpServer` 与注册表桥接（``register_mcp_tools``）不用改 —— 换的只是
  传输实现，这也是把"协议"与"传输"分开写的原因。

**与技能系统（skills）的关系**（任务书要求明确）

| | 技能（skills） | MCP 工具 |
| :--- | :--- | :--- |
| 定位 | EchoDesk **自己的**插件：能注册指令、面板、钩子、文件解析器 | 接入**外部进程**提供的工具 |
| 安装 | 从技能市场下载到本地目录 | 用户手写一行命令（`command` + `args`），EchoDesk 只负责拉起它 |
| 能力面 | 全面（有界面、有生命周期、有信任机制） | 只有工具调用（本版） |
| 谁能用 | 指令、Agent（未来可挂） | **只有 Agent**（它们本来就是"工具"） |
| 权限 | 安装时信任对话框 + 运行期密钥清洗 | 走 Agent 的统一权限分级，且**默认全部按敏感工具**（见下） |

两者不互相替代：技能是"给 EchoDesk 加功能"，MCP 是"让 Agent 多几个外部工具"。
桥接点在 ``register_mcp_tools``：MCP 工具被包装成普通 ``Tool`` 进同一个注册表，
因此权限、审计、可见性、可中断全都复用 Agent 那一套，不存在第二套规则。

**数据流向与"数据不出本机"的承诺**（任务书要求写清楚）

MCP Server 是**用户自己配的进程**，它的行为不受 EchoDesk 控制：工具可能连数据库、
调 API、把内容发到别处。所以：

1. 所有 MCP 工具**默认按 `sensitive` 处理**（即使 server 自称只读），
   必须由用户显式 `/agent allow mcp__<server>__<tool>` 才可调用；
2. 设置里的 MCP 配置是用户主动填写的，EchoDesk 不会自动发现、不会自动连接任何地址；
3. EchoDesk 自身不发起网络请求 —— 联网与否取决于用户配的那个 server 进程。
"""
from __future__ import annotations
from i18n import t as _t

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 单次请求的默认超时（秒）。外部进程可能卡死，必须有上限。
DEFAULT_TIMEOUT = 20.0

#: MCP 协议版本（客户端声明自己支持的版本，服务端可以回自己的）。
PROTOCOL_VERSION = "2024-11-05"

MCP_TOOL_PREFIX = "mcp__"


class McpError(RuntimeError):
    """MCP 调用失败（启动失败、超时、服务端返回错误、协议不对）。"""


@dataclass
class McpServer:
    """一个 MCP Server 的配置（来自 ``config.mcp.servers``）。"""

    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    #: 允许注入的环境变量白名单（额外的）；默认只传最小集合，
    #: 避免把父进程的密钥（HF_TOKEN 之类）顺手交给第三方进程。
    passthrough_env: List[str] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        """给界面用的声明。

        **必须回传 ``env`` 与 ``passthrough_env``**：``mcp_apply`` 的语义是"整份替换
        servers 列表"，而界面是先读状态、改一改、再整份发回。若状态里不回传这两个字段，
        用户在界面上改个超时再保存，config.json 里手写的 ``env`` 就被抹成空 ——
        这是前端子代理在联调时发现的真实数据丢失路径（它已做了一层缓解：
        列表没被改过就不发 servers，但那只是绕开，根因在这里）。
        """
        return {"name": self.name, "command": self.command, "args": list(self.args),
                "env": dict(self.env or {}), "enabled": bool(self.enabled),
                "passthrough_env": list(self.passthrough_env)}


class StdioMcpClient:
    """stdio 传输的 MCP 客户端（拉起子进程、走一行一个 JSON 的 JSON-RPC 2.0）。

    刻意**不做连接池/常驻**：一次会话内保持一个子进程（工具的多次调用共用），
    ``close()`` 之后可以再 ``connect()``。这样"运行时启停某个 server"
    就是"关掉它的客户端、下次用到再拉起"，不需要复杂的生命周期管理。
    """

    def __init__(self, server: McpServer, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.server = server
        self.timeout = max(1.0, float(timeout or DEFAULT_TIMEOUT))
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 1
        self.initialized = False
        self.server_info: Dict[str, Any] = {}
        self.last_error = ""
        # 读线程 + 队列：**Windows 上 pipe 的 readline() 会无限阻塞**，
        # 只靠"循环前看一眼 deadline"根本拦不住对端卡死（首轮实测就是把测试挂到超时）。
        # 与 llm_engine 的流式心跳是同一个教训：阻塞读必须放到线程里，由消费者带超时取。
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    def _child_env(self) -> Dict[str, str]:
        """给子进程准备环境变量：只给最小集合 + 用户点名要透传的那些。

        为什么默认不透传：父进程环境里通常有 ``HF_TOKEN``、代理口令之类的东西，
        把它们无条件交给一个第三方进程是隐私事故的常见形状。
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        for key in self.server.passthrough_env:
            if key in os.environ:
                env[key] = os.environ[key]
        env.update({str(k): str(v) for k, v in (self.server.env or {}).items()})
        return {k: v for k, v in env.items() if v != ""}

    def connect(self) -> bool:
        """拉起子进程并完成 ``initialize`` 握手；失败返回 False（原因在 last_error）。"""
        if self._proc is not None and self._proc.poll() is None:
            return self.initialized
        if not self.server.command:
            self.last_error = "没有配置 command"
            return False
        try:
            self._proc = subprocess.Popen(
                [self.server.command] + [str(a) for a in self.server.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=self._child_env(), cwd=os.getcwd(),
            )
        except OSError as exc:
            self.last_error = f"启动失败：{exc}"
            self._proc = None
            return False
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name=f"mcp-{self.server.name}")
        self._reader.start()
        try:
            result = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientInfo": {"name": "EchoDesk", "version": _echodesk_version()},
                "capabilities": {},
            })
        except McpError as exc:
            self.last_error = str(exc)
            self.close()
            return False
        self.server_info = dict((result or {}).get("serverInfo") or {})
        self.initialized = True
        # 按协议要求发一条 initialized 通知（不带 id，服务端不回复）
        self._notify("notifications/initialized", {})
        return True

    def _pump(self) -> None:
        """读线程：把子进程的每一行塞进队列（阻塞发生在**这个线程**里，不带超时）。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._lines.put(None)
            return
        try:
            for line in proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(None)          # None = EOF 哨兵

    def close(self) -> None:
        proc, self._proc = self._proc, None
        self.initialized = False
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._lines.put(None)

    # ---------- JSON-RPC ----------

    def _send(self, payload: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("MCP 进程未启动")
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise McpError(f"写入 MCP 进程失败：{exc}") from exc

    def _read_response(self, want_id: int, deadline: float) -> Dict[str, Any]:
        """从读线程的队列里等匹配 id 的响应（**超时靠队列的 timeout，而不是靠 readline**）。"""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise McpError(f"等待响应超时（{self.timeout:.0f} 秒）")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise McpError(f"等待响应超时（{self.timeout:.0f} 秒）") from None
            if line is None:
                raise McpError("MCP 进程已退出（未响应）")
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue                       # 有些 server 会往 stdout 打日志，跳过
            if not isinstance(message, dict):
                continue
            if message.get("id") != want_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                raise McpError(f"服务端返回错误：{error.get('message') or error}")
            result = message.get("result")
            return result if isinstance(result, dict) else {"value": result}

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return self._read_response(request_id, time.time() + self.timeout)

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except McpError:
            pass                                # 通知失败不影响主流程

    # ---------- 能力 ----------

    def list_tools(self) -> List[Dict[str, Any]]:
        """``tools/list``：返回 ``[{name, description, inputSchema}]``。"""
        if not self.initialized and not self.connect():
            raise McpError(self.last_error or "连接失败")
        result = self._request("tools/list", {})
        tools = result.get("tools")
        return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """``tools/call``：把返回内容拼成一段文本（Agent 的工具结果就是字符串）。"""
        if not self.initialized and not self.connect():
            raise McpError(self.last_error or "连接失败")
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return _render_result(result)


def _render_result(result: Dict[str, Any]) -> str:
    """把 MCP 的 content 数组渲染成纯文本（text 类型优先，其余类型变成一行说明）。"""
    content = result.get("content")
    parts: List[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "resource":
                resource = item.get("resource") or {}
                parts.append(f"[资源 {resource.get('uri', '')}] {resource.get('text', '')}")
            else:
                parts.append(f"[{item.get('type', '未知')} 内容，本版只显示文本]")
    elif isinstance(result, dict) and result.get("value") is not None:
        parts.append(str(result.get("value")))
    text = "\n".join(part for part in parts if part).strip()
    if result.get("isError"):
        return _t("mcp.tool_reported_error", detail=text or _t("mcp.no_detail"))
    return text or _t("mcp.empty_result")


def _echodesk_version() -> str:
    try:
        from version import APP_VERSION

        return str(APP_VERSION)
    except Exception:
        return "0.0.0"


# ===========================================================================
# 与 Agent 工具注册表的桥接
# ===========================================================================

class McpBridge:
    """按配置拉起若干 MCP Server，并把它们的工具注册进 Agent 的工具表。

    **默认按敏感工具处理**（``risk="sensitive"``）：MCP Server 是用户配的外部进程，
    EchoDesk 无从判断它是否只读、会不会外发数据，所以一律要求显式授权。
    这是"宁可多问一句，也不替用户做安全假设"的取舍。
    """

    def __init__(self, servers: Optional[List[McpServer]] = None, timeout: float = DEFAULT_TIMEOUT,
                 auto_connect: bool = False) -> None:
        self.servers: List[McpServer] = list(servers or [])
        self.timeout = timeout
        self._clients: Dict[str, StdioMcpClient] = {}
        self._tools: Dict[str, Dict[str, Any]] = {}      # 全名 → {server, tool, schema}
        self.auto_connect = auto_connect
        self.last_error = ""

    # ---------- 配置 ----------

    @staticmethod
    def server_from_dict(raw: Dict[str, Any]) -> Optional[McpServer]:
        name = str((raw or {}).get("name") or "").strip()
        command = str((raw or {}).get("command") or "").strip()
        if not name or not command:
            return None
        args = raw.get("args")
        env = raw.get("env")
        passthrough = raw.get("passthrough_env")
        return McpServer(
            name=name, command=command,
            args=[str(a) for a in args] if isinstance(args, list) else [],
            env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
            enabled=bool(raw.get("enabled", True)),
            passthrough_env=[str(x) for x in passthrough] if isinstance(passthrough, list) else [],
        )

    # ---------- 连接与发现 ----------

    def client(self, name: str) -> Optional[StdioMcpClient]:
        server = next((item for item in self.servers if item.name == name), None)
        if server is None or not server.enabled:
            return None
        client = self._clients.get(name)
        if client is None:
            client = StdioMcpClient(server, self.timeout)
            self._clients[name] = client
        return client

    def discover(self, force: bool = False) -> Dict[str, Any]:
        """连接所有启用的 server 并拉取工具清单；返回 ``{server: [工具名]}`` 与错误。"""
        if self._tools and not force:
            return {"tools": self.tool_names(), "errors": {}, "cached": True}
        self._tools.clear()
        errors: Dict[str, str] = {}
        for server in self.servers:
            if not server.enabled:
                continue
            client = self.client(server.name)
            assert client is not None
            try:
                for tool in client.list_tools():
                    tool_name = str(tool.get("name") or "").strip()
                    if not tool_name:
                        continue
                    full = f"{MCP_TOOL_PREFIX}{server.name}__{tool_name}"
                    self._tools[full] = {"server": server.name, "tool": tool_name,
                                         "description": str(tool.get("description") or "").strip(),
                                         "schema": tool.get("inputSchema") or {}}
            except McpError as exc:
                errors[server.name] = str(exc)
                self.last_error = str(exc)
        return {"tools": self.tool_names(), "errors": errors, "cached": False}

    def tool_names(self) -> List[str]:
        return list(self._tools)

    def call(self, full_name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """调用一个已发现的 MCP 工具（``mcp__<server>__<tool>``）。"""
        info = self._tools.get(full_name)
        if info is None:
            raise McpError(f"没有发现名为 {full_name} 的工具（先 discover）")
        client = self.client(info["server"])
        if client is None:
            raise McpError(f"server {info['server']} 未启用")
        return client.call_tool(info["tool"], arguments)

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
        self._tools.clear()

    def status(self) -> List[Dict[str, Any]]:
        """给设置界面用的状态：每个 server 是否启用、连上了没、有几个工具、错误。"""
        out: List[Dict[str, Any]] = []
        for server in self.servers:
            tools = [name for name, info in self._tools.items() if info["server"] == server.name]
            client = self._clients.get(server.name)
            out.append({
                **server.public(),
                "connected": bool(client and client.initialized),
                "tools": tools,
                "tool_count": len(tools),
                "error": (client.last_error if client else ""),
                "server_info": (client.server_info if client else {}),
            })
        return out


def register_mcp_tools(registry: Any, bridge: McpBridge) -> int:
    """把 MCP 工具注册进 Agent 的工具表，返回注册数量。

    ``registry`` 是 ``agent_tools.ToolRegistry``。包装出来的工具：
      * 全部 ``risk="sensitive"``（见 :class:`McpBridge` 的说明）；
      * 参数用 MCP 自己给的 ``inputSchema`` 描述（让模型知道怎么填）；
      * 执行时把 MCP 的返回渲染成文本；异常翻译成一句人话（**工具绝不抛**）。
    """
    from agent_tools import RISK_SENSITIVE, Tool

    if not bridge.auto_connect:
        # 不自动连：拉起外部进程是有代价也有风险的动作，只在用户真的要用时做。
        return 0
    try:
        bridge.discover()
    except Exception:
        return 0
    count = 0
    for full_name, info in bridge._tools.items():
        schema = info.get("schema") or {}

        def _run(args: Dict[str, Any], name: str = full_name) -> str:
            try:
                return bridge.call(name, args)
            except McpError as exc:
                return _t("mcp.call_failed", error=exc)
            except Exception as exc:                     # 兜底：外部进程什么都可能干
                return _t("mcp.call_exception", kind=type(exc).__name__, error=exc)

        registry.register(Tool(
            name=full_name,
            description=f"[{info['server']}] "
                        f"{info.get('description') or _t('mcp.tool_default')}",
            args_hint=json.dumps(schema, ensure_ascii=False) if schema else "",
            risk=RISK_SENSITIVE,
            run=_run,
        ))
        count += 1
    return count
