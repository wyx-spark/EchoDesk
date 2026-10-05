#!/usr/bin/env python3
"""示例 MCP Server（EchoDesk 随包示例，纯标准库）。

它既是**可用的示例**（两个真实工具：文本回显、当前时间），也是 MCP 接入的
**可执行文档** —— ``tools/test_mcp.py`` 会真的拉起这个进程、走一遍
``initialize`` / ``tools/list`` / ``tools/call``，所以它不是"写完没跑过"的样例。

用 EchoDesk 接入（``config.json``）::

    "mcp": {
      "enabled": true,
      "servers": [
        {"name": "echo", "command": "python", "args": ["examples/mcp/echo_server.py"],
         "enabled": true}
      ]
    }

接进来之后：
  * 工具会以 ``mcp__echo__echo_text`` 这样的名字出现在 ``/agent`` 的工具清单里；
  * MCP 工具**默认按敏感工具处理**（外部进程），要用得先
    ``/agent allow mcp__echo__echo_text``；
  * Agent 模式下模型就能调用它了。

协议：JSON-RPC 2.0，一行一个 JSON 对象走 stdin/stdout。支持的方法：
``initialize``、``notifications/initialized``（通知，无响应）、``tools/list``、
``tools/call``。不支持的方法回 ``-32601``，参数错误回 ``-32602``。
"""
import datetime
import json
import sys

SERVER_INFO = {"name": "echodesk-echo-server", "version": "1.0.0"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "echo_text",
        "description": "把输入的文字原样回显（用来验证 MCP 通路是否打通）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文字"}},
            "required": ["text"],
        },
    },
    {
        "name": "now",
        "description": "返回本机当前日期时间（可以指定时区偏移小时数）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tz_offset_hours": {"type": "number", "description": "时区偏移，默认 8（北京时间）"},
            },
        },
    },
]


def call_tool(name, arguments):
    """执行一个工具，返回 MCP 的 content 数组。"""
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "echo_text":
        text = arguments.get("text")
        if not isinstance(text, str):
            raise ValueError("参数 text 必须是字符串")
        return [{"type": "text", "text": text}]
    if name == "now":
        try:
            offset = float(arguments.get("tz_offset_hours", 8))
        except (TypeError, ValueError):
            raise ValueError("参数 tz_offset_hours 必须是数字")
        moment = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=offset)))
        return [{"type": "text",
                 "text": moment.strftime("%Y-%m-%d %H:%M:%S") + f"（UTC{offset:+g}）"}]
    raise KeyError(f"未知工具：{name}")


def handle(request):
    """处理一个请求，返回响应字典；通知（没有 id）返回 None。"""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": SERVER_INFO}
    elif method in ("notifications/initialized", "initialized"):
        return None
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        try:
            content = call_tool(name, params.get("arguments"))
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": str(exc)}}
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": str(exc)}}
        result = {"content": content, "isError": False}
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"不支持的方法：{method}"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    # 必须显式指定 UTF-8：Windows 默认代码页不是 UTF-8，中文会变成乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except json.JSONDecodeError:
            continue                      # 非法行直接忽略（与"跳过日志行"同一策略）
        if not isinstance(request, dict):
            continue
        response = handle(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
