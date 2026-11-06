"""
[EN]
MCP client.

Dependency choice and package-size impact

Conclusion: implement a minimal in-house stdio JSON-RPC client instead of
depending on the official `mcp` package.

* The official ``mcp`` Python package pulls in ``pydantic`` (already present),
  ``anyio``, ``httpx``, ``sse-starlette``, ``starlette``, and a chain of further
  dependencies; the server-side parts are entirely unused by this tool, which only
  needs a client. The release artifact is already compressed into the 350-400 MB
  range, so adding a dozen packages for a feature that only matters when the user
  configures it explicitly is not worthwhile.
* The MCP transport layer itself is JSON-RPC 2.0: one JSON object per line over
  stdin/stdout, with the ``initialize``, ``tools/list``, and ``tools/call``
  methods. This layer requires roughly 200 lines in-house, all readable and
  testable (``tools/test_mcp.py`` launches the bundled example server and exercises
  it end to end).
* Cost, recorded explicitly: this version supports only stdio transport.
  HTTP/SSE transport, the resources and prompts capabilities, sampling, and roots
  are unsupported. If those become necessary, the official package can be adopted
  later; :class:`McpServer` and the registry bridge (``register_mcp_tools``) would
  not change, because only the transport implementation would be replaced. That is
  also why protocol handling and transport handling are kept separate.

Relationship to the skills system

| | Skills | MCP tools |
| :--- | :--- | :--- |
| Role | EchoDesk-owned plugins: register commands, panels, hooks, and file parsers | Tools provided by external processes |
| Installation | Downloaded from the skills marketplace into a local directory | The user writes a command line (`command` + `args`); EchoDesk only launches it |
| Capability surface | Broad (UI, lifecycle, trust mechanism) | Tool invocation only (this version) |
| Consumers | Commands, Agent (pluggable in future) | Agent only, since they are tools by definition |
| Permissions | Trust dialog at install time plus runtime secret scrubbing | The Agent's unified risk grading, with every tool treated as sensitive by default (see below) |

The two mechanisms do not replace each other: skills add functionality to
EchoDesk, while MCP gives the Agent additional external tools. The bridge point is
``register_mcp_tools``: MCP tools are wrapped as ordinary ``Tool`` objects in the
same registry, so permissions, auditing, visibility, and interruptibility all reuse
the Agent's existing machinery with no second rule set.

Data flow and the local-data guarantee

An MCP Server is a user-configured process whose behavior EchoDesk does not
control: its tools may access databases, call APIs, or send content elsewhere.
Therefore:
1. Every MCP tool is treated as ``sensitive`` by default, even if the server
   claims to be read-only, and requires an explicit
   `/agent allow mcp__<server>__<tool>` before it can be invoked;
2. MCP configuration in settings is supplied by the user; EchoDesk never discovers
   servers automatically and never connects to any address automatically;
3. EchoDesk itself makes no network requests; network access depends entirely on
   the user-configured server process.

[ZH]
MCP 客户端。

依赖选择与体积影响

结论：自行实现极小的 stdio JSON-RPC 客户端，不引入 `mcp` 官方包。

* 官方 ``mcp`` Python 包会引入 ``pydantic``（已存在）、``anyio``、``httpx``、
  ``sse-starlette``、``starlette`` 等一长串依赖，其中服务端相关部分对本工具
  （只需要客户端）完全没有用途。发布物已控制在 350–400 MB 区间，为一个"仅当用户
  主动配置时才会使用"的功能再引入十几个包并不划算。
* MCP 的传输层本体是 JSON-RPC 2.0：每行一个 JSON 对象，经 stdin/stdout 传输，
  方法为 ``initialize`` / ``tools/list`` / ``tools/call``。该层自行实现约 200 行，
  全部可读、可测（``tools/test_mcp.py`` 会拉起随包示例服务器并完整执行一遍）。
* 代价（如实记录）：本版仅支持 stdio 传输，不支持 HTTP/SSE 传输、resources/prompts
  两类能力、采样（sampling）与 roots。若后续需要这些能力，可以改用官方包；届时
  :class:`McpServer` 与注册表桥接（``register_mcp_tools``）无需改动，替换的仅是传输
  实现。这也是将"协议"与"传输"分开实现的原因。

与技能系统（skills）的关系

| | 技能（skills） | MCP 工具 |
| :--- | :--- | :--- |
| 定位 | EchoDesk 自身的插件：可注册指令、面板、钩子与文件解析器 | 接入外部进程提供的工具 |
| 安装 | 从技能市场下载到本地目录 | 用户填写命令行（`command` + `args`），EchoDesk 只负责拉起 |
| 能力面 | 全面（含界面、生命周期、信任机制） | 仅工具调用（本版） |
| 使用方 | 指令、Agent（后续可接入） | 仅 Agent（其本身就是工具） |
| 权限 | 安装时的信任对话框 + 运行期密钥清洗 | 复用 Agent 的统一权限分级，且默认全部按敏感工具处理（见下文） |

两者并不互相替代：技能为 EchoDesk 增加功能，MCP 为 Agent 增加外部工具。桥接点为
``register_mcp_tools``：MCP 工具被包装为普通 ``Tool`` 进入同一注册表，因此权限、审计、
可见性与可中断性全部复用 Agent 的既有机制，不存在第二套规则。

数据流向与"数据不出本机"的约定

MCP Server 是用户自行配置的进程，其行为不受 EchoDesk 控制：工具可能连接数据库、
调用 API 或将内容发送到其他位置。因此：

1. 所有 MCP 工具默认按 ``sensitive`` 处理（即使 server 声称只读），必须由用户显式
   执行 `/agent allow mcp__<server>__<tool>` 才可调用；
2. 设置中的 MCP 配置由用户主动填写，EchoDesk 不会自动发现，也不会自动连接任何地址；
3. EchoDesk 自身不发起网络请求，是否联网完全取决于用户配置的 server 进程。
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

#: [EN] Default timeout for a single request, in seconds. External processes may
#: hang, so an upper bound is mandatory.
#: [ZH] 单次请求的默认超时（秒）。外部进程可能挂起，必须设置上限。
DEFAULT_TIMEOUT = 20.0

#: [EN] MCP protocol version. The client declares the version it supports, and the
#: server may report its own.
#: [ZH] MCP 协议版本。客户端声明自身支持的版本，服务端可返回其自身版本。
PROTOCOL_VERSION = "2024-11-05"

MCP_TOOL_PREFIX = "mcp__"


class McpError(RuntimeError):
    """
    [EN]
    An MCP invocation failed: launch failure, timeout, server-reported error, or
    protocol mismatch.
    [ZH]
    MCP 调用失败（启动失败、超时、服务端返回错误或协议不匹配）。
    """


@dataclass
class McpServer:
    """
    [EN]
    Configuration of one MCP Server, sourced from ``config.mcp.servers``.
    [ZH]
    单个 MCP Server 的配置（来自 ``config.mcp.servers``）。
    """

    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    #: [EN] Additional allowlist of environment variable names to inject. Only a
    #: minimal set is passed by default, so secrets from the parent process (such as
    #: HF_TOKEN) are not handed to third-party processes implicitly.
    #: [ZH] 额外允许注入的环境变量白名单。默认只传递最小集合，避免将父进程中的密钥
    #: （如 HF_TOKEN）顺带交给第三方进程。
    passthrough_env: List[str] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        """
        [EN]
        Declaration for the UI.

        ``env`` and ``passthrough_env`` must be returned. ``mcp_apply`` replaces the
        entire servers list, while the UI reads state, modifies it, and sends it
        back in full. If the state omits these two fields, saving an unrelated
        change such as a timeout wipes the hand-written ``env`` entries in
        config.json. This is a real data-loss path found during frontend
        integration. A frontend mitigation exists (skip sending `servers` when the
        list was not modified), but that only avoids the symptom; the root cause is
        here.

        [ZH]
        供界面使用的声明。

        必须回传 ``env`` 与 ``passthrough_env``：``mcp_apply`` 的语义是"整份替换
        servers 列表"，而界面的流程是先读取状态、修改后再整份发回。若状态中不回传这两个
        字段，用户在界面上修改一个超时再保存时，config.json 中手写的 ``env`` 就会被清空。
        这是前端联调时发现的真实数据丢失路径。前端已做一层缓解（列表未被修改时不发送
        servers），但那只是规避症状，根因在此处。
        """
        return {"name": self.name, "command": self.command, "args": list(self.args),
                "env": dict(self.env or {}), "enabled": bool(self.enabled),
                "passthrough_env": list(self.passthrough_env)}


class StdioMcpClient:
    """
    [EN]
    MCP client over stdio: launches a subprocess and speaks JSON-RPC 2.0 with one
    JSON object per line.

    Connection pooling and persistence are deliberately omitted: one subprocess is
    kept per session and shared across repeated tool calls, and ``connect()`` may be
    called again after ``close()``. Starting and stopping a server at runtime is
    therefore equivalent to closing its client and relaunching it on next use, with
    no complex lifecycle management.

    [ZH]
    stdio 传输的 MCP 客户端：拉起子进程，使用每行一个 JSON 对象的 JSON-RPC 2.0。

    刻意不实现连接池与常驻：一次会话内保持一个子进程（多次工具调用共用），
    ``close()`` 之后可再次 ``connect()``。因此运行时启停某个 server 即等同于关闭其
    客户端并在下次使用时重新拉起，无需复杂的生命周期管理。
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
        # [EN] Reader thread plus queue: on Windows, readline() on a pipe blocks
        # indefinitely, and checking a deadline before each loop iteration cannot
        # detect a hung peer (the first implementation hung until the test timeout).
        # The same constraint applies to streaming heartbeats in llm_engine: a
        # blocking read must run on a separate thread and be consumed with a
        # timeout.
        # [ZH] 读线程 + 队列：Windows 上管道的 readline() 会无限阻塞，仅在循环前检查
        # deadline 无法拦截对端挂起（首轮实现导致测试一直挂起到超时）。这与 llm_engine
        # 的流式心跳遵循同一约束：阻塞读必须放到独立线程，由消费者带超时读取。
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None

    # ---------- [EN] Lifecycle ----------
    # ---------- [ZH] 生命周期 ----------

    def _child_env(self) -> Dict[str, str]:
        """
        [EN]
        Build the environment for the child process: a minimal set plus the
        variables the user explicitly allows to pass through.

        Variables are not passed through by default because the parent environment
        commonly contains ``HF_TOKEN``, proxy credentials, and similar secrets;
        handing them unconditionally to a third-party process is a common form of
        privacy incident.

        [ZH]
        为子进程准备环境变量：仅提供最小集合，加上用户显式指定要透传的变量。

        默认不透传的原因：父进程环境中通常包含 ``HF_TOKEN``、代理口令等密钥，将其
        无条件交给第三方进程是隐私事故的典型原因。
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
        """
        [EN]
        Launch the subprocess and complete the ``initialize`` handshake. Returns
        False on failure, with the reason in ``last_error``.
        [ZH]
        拉起子进程并完成 ``initialize`` 握手；失败时返回 False，原因记录在
        ``last_error`` 中。
        """
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
        # [EN] Send the initialized notification required by the protocol. It
        # carries no id and the server does not reply.
        # [ZH] 按协议要求发送 initialized 通知（不带 id，服务端不回复）。
        self._notify("notifications/initialized", {})
        return True

    def _pump(self) -> None:
        """
        [EN]
        Reader thread: enqueue each line produced by the subprocess. Blocking
        occurs on this thread and has no timeout.
        [ZH]
        读线程：将子进程输出的每一行放入队列。阻塞发生在此线程内，且不设置超时。
        """
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
            # [EN] None is the EOF sentinel.
            # [ZH] None 为 EOF 哨兵。
            self._lines.put(None)

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

    # ---------- [EN] JSON-RPC ----------
    # ---------- [ZH] JSON-RPC ----------

    def _send(self, payload: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("MCP 进程未启动")
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise McpError(f"写入 MCP 进程失败：{exc}") from exc

    def _read_response(self, want_id: int, deadline: float) -> Dict[str, Any]:
        """
        [EN]
        Wait on the reader queue for the response matching the request id. The
        timeout is enforced by the queue, not by readline.
        [ZH]
        从读线程队列中等待与请求 id 匹配的响应。超时由队列的 timeout 保证，而不是
        依赖 readline。
        """
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
                # [EN] Some servers log to stdout; skip non-JSON lines.
                # [ZH] 部分 server 会向 stdout 输出日志，跳过非 JSON 行。
                continue
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
            # [EN] A failed notification does not affect the main flow.
            # [ZH] 通知发送失败不影响主流程。
            pass

    # ---------- [EN] Capabilities ----------
    # ---------- [ZH] 能力 ----------

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        [EN]
        ``tools/list``: returns ``[{name, description, inputSchema}]``.
        [ZH]
        ``tools/list``：返回 ``[{name, description, inputSchema}]``。
        """
        if not self.initialized and not self.connect():
            raise McpError(self.last_error or "连接失败")
        result = self._request("tools/list", {})
        tools = result.get("tools")
        return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """
        [EN]
        ``tools/call``: render the returned content as a single text block, since an
        Agent tool result is a string.
        [ZH]
        ``tools/call``：将返回内容拼接为一段文本，因为 Agent 的工具结果为字符串。
        """
        if not self.initialized and not self.connect():
            raise McpError(self.last_error or "连接失败")
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return _render_result(result)


def _render_result(result: Dict[str, Any]) -> str:
    """
    [EN]
    Render an MCP content array as plain text. Text items are rendered as-is;
    other types become a one-line description.
    [ZH]
    将 MCP 的 content 数组渲染为纯文本：text 类型按原样输出，其余类型转为一行说明。
    """
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
# [EN] Bridge to the Agent tool registry
# [ZH] 与 Agent 工具注册表的桥接
# ===========================================================================

class McpBridge:
    """
    [EN]
    Launch the configured MCP Servers and register their tools in the Agent tool
    table.

    Tools are treated as sensitive by default (``risk="sensitive"``): an MCP Server
    is a user-configured external process, and EchoDesk cannot determine whether it
    is read-only or whether it transmits data externally, so explicit authorization
    is always required. This is a deliberate trade-off: prompt once more rather than
    make security assumptions on the user's behalf.

    [ZH]
    按配置拉起若干 MCP Server，并将其工具注册进 Agent 的工具表。

    默认按敏感工具处理（``risk="sensitive"``）：MCP Server 是用户配置的外部进程，
    EchoDesk 无法判断其是否只读、是否会外发数据，因此一律要求显式授权。这是"宁可多
    询问一次，也不替用户做安全假设"的取舍。
    """

    def __init__(self, servers: Optional[List[McpServer]] = None, timeout: float = DEFAULT_TIMEOUT,
                 auto_connect: bool = False) -> None:
        self.servers: List[McpServer] = list(servers or [])
        self.timeout = timeout
        self._clients: Dict[str, StdioMcpClient] = {}
        # [EN] Fully qualified name -> {server, tool, schema}.
        # [ZH] 全名 → {server, tool, schema}。
        self._tools: Dict[str, Dict[str, Any]] = {}
        self.auto_connect = auto_connect
        self.last_error = ""

    # ---------- [EN] Configuration ----------
    # ---------- [ZH] 配置 ----------

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

    # ---------- [EN] Connection and discovery ----------
    # ---------- [ZH] 连接与发现 ----------

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
        """
        [EN]
        Connect to all enabled servers and fetch their tool lists. Returns
        ``{server: [tool names]}`` together with any errors.
        [ZH]
        连接所有已启用的 server 并拉取工具清单；返回 ``{server: [工具名]}`` 及错误信息。
        """
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
        """
        [EN]
        Invoke a discovered MCP tool (``mcp__<server>__<tool>``).
        [ZH]
        调用一个已发现的 MCP 工具（``mcp__<server>__<tool>``）。
        """
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
        """
        [EN]
        Status for the settings UI: whether each server is enabled, whether it is
        connected, its tool count, and any error.
        [ZH]
        供设置界面使用的状态：每个 server 是否启用、是否已连接、工具数量及错误信息。
        """
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
    """
    [EN]
    Register MCP tools in the Agent tool table and return the number registered.

    ``registry`` is an ``agent_tools.ToolRegistry``. The wrapped tools have these
    properties:
      * Every tool uses ``risk="sensitive"`` (see :class:`McpBridge`);
      * Arguments are described by the MCP-provided ``inputSchema``, so the model
        knows how to populate them;
      * Execution renders the MCP result as text, and exceptions are translated
        into a readable message; a tool never raises.

    [ZH]
    将 MCP 工具注册进 Agent 的工具表，返回注册数量。

    ``registry`` 为 ``agent_tools.ToolRegistry``。包装后的工具具有以下特性：
      * 全部使用 ``risk="sensitive"``（见 :class:`McpBridge` 的说明）；
      * 参数由 MCP 提供的 ``inputSchema`` 描述，使模型知道如何填写；
      * 执行时将 MCP 返回内容渲染为文本，异常转换为可读提示；工具本身绝不抛出异常。
    """
    from agent_tools import RISK_SENSITIVE, Tool

    if not bridge.auto_connect:
        # [EN] No automatic connection: launching an external process has both cost
        # and risk, so it happens only when the user actually needs it.
        # [ZH] 不自动连接：拉起外部进程既有开销也有风险，仅在用户确实需要时才执行。
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
            # [EN] Catch-all: an external process may fail in arbitrary ways.
            # [ZH] 兜底处理：外部进程可能以任意方式失败。
            except Exception as exc:
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
