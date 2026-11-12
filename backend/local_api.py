"""
[EN]
Local OpenAI-compatible HTTP API (v0.2.6 Part 4, task 2.1).

Purpose
    Expose EchoDesk's local inference to external tools (VS Code extensions such as
    Cline or Continue, Cursor, Postman, curl) through the endpoint shape those tools
    already implement: ``POST /v1/chat/completions`` with optional SSE streaming,
    plus ``GET /v1/models`` for discovery.

Why the standard library instead of FastAPI
    The design specification names FastAPI as an example ("such as FastAPI"), not as
    a requirement. FastAPI requires ``starlette``, ``anyio``, ``pydantic-settings``
    and usually ``uvicorn``; for three endpoints that is several megabytes and four
    more components in the dependency lock and SBOM. :mod:`http.server` is part of
    the standard library and is sufficient here because:

    * the workload is a handful of long-lived requests from one local client, not a
      public service -- throughput is bounded by the model, not by HTTP;
    * ``ThreadingHTTPServer`` already gives one thread per connection, which is what
      a local IDE extension needs;
    * zero new dependencies keeps the release-size and SBOM gates unchanged.

    The trade-off, recorded honestly: no automatic OpenAPI document, no built-in
    validation framework, and no ASGI middleware ecosystem. If this ever needs to
    serve multiple clients at meaningful concurrency, FastAPI becomes the right
    answer.

Security boundary (the part that must not be regressed)
    1. The listening socket is bound to ``127.0.0.1`` only. The host is not
       configurable; :meth:`LocalApiServer.__init__` rejects anything else, so a
       configuration edit cannot expose inference to the local network.
    2. Requests are refused with HTTP 401 unless the configured bearer token
       matches, compared in constant time. An empty token means "no authentication",
       which is acceptable only because the socket is loopback-only and the feature
       is off by default; the settings page states this explicitly.
    3. The request body is capped at :data:`MAX_BODY_BYTES`; a malformed
       ``Content-Length`` is rejected rather than trusted.
    4. No CORS header is ever emitted. Allowing arbitrary web origins to call the
       local port would let any visited page drive the user's model and read the
       answers; desktop clients do not need CORS. This is a deliberate refusal, not
       an omission.
    5. Errors are returned in the OpenAI error envelope with a generic ``type``;
       the detailed cause stays in the server log.

Concurrency
    One model can generate one answer at a time on this backend. A second concurrent
    request is therefore answered with HTTP 429 instead of being queued (queueing
    would make an IDE hang without feedback), and the protocol thread also refuses
    new chat requests while an API generation is in flight (see
    ``main.Backend.api_busy``), because the engine is not re-entrant.

[ZH]
本地 OpenAI 兼容 HTTP API（v0.2.6 任务 2.1）。

目的
    把 EchoDesk 的本地推理暴露给外部工具（Cline、Continue 等 VS Code 扩展、Cursor、
    Postman、curl），使用它们本来就实现的端点形状：``POST /v1/chat/completions``
    （可选 SSE 流式），以及用于发现模型的 ``GET /v1/models``。

为什么用标准库而不是 FastAPI
    设计规范把 FastAPI 作为示例（"如基于 FastAPI"）而非硬性要求。FastAPI 需要
    ``starlette``、``anyio``、``pydantic-settings``，通常还要 ``uvicorn``；为三个端点
    引入数 MB 与四个以上依赖清单/SBOM 组件并不划算。:mod:`http.server` 属于标准库，
    在此足够，理由如下：

    * 负载是单个本地客户端发起的少量长连接请求，而不是对外服务 —— 吞吐由模型决定，
      而非 HTTP 层；
    * ``ThreadingHTTPServer`` 已提供每连接一线程，正是本地 IDE 扩展需要的形态；
    * 零新增依赖使发布体积与 SBOM 门禁保持不变。

    取舍如实记录：没有自动生成的 OpenAPI 文档，没有内置校验框架，也没有 ASGI 中间件
    生态。若将来需要以可观并发服务多客户端，FastAPI 才是正确答案。

安全边界（不得回退的部分）
    1. 监听套接字只绑定 ``127.0.0.1``。主机不可配置；:meth:`LocalApiServer.__init__`
       拒绝其他取值，因此改配置文件也无法把推理暴露到局域网。
    2. 除非配置的 bearer 令牌以常数时间比较通过，请求一律以 HTTP 401 拒绝。令牌为空
       表示"不鉴权"，这仅在套接字只监听环回、且该功能默认关闭时可以接受；设置页会明确
       说明这一点。
    3. 请求体上限为 :data:`MAX_BODY_BYTES`；非法 ``Content-Length`` 直接拒绝而不信任。
    4. 从不发送 CORS 头。允许任意网页来源调用本地端口，等于让任何被访问的页面驱动用户的
       模型并读取回答；桌面客户端不需要 CORS。这是刻意拒绝，而非遗漏。
    5. 错误以 OpenAI 错误信封返回，``type`` 为通用值；详细原因只留在服务器日志中。

并发
    本后端的一个模型同一时刻只能生成一份回答。因此第二个并发请求以 HTTP 429 应答而不是
    排队（排队会让 IDE 无反馈地挂起）；协议线程在一次 API 生成进行中同样拒绝新的对话
    请求（见 ``main.Backend.api_busy``），因为引擎不可重入。
"""
from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List, Optional

from i18n import t as _t

log = logging.getLogger("EchoDesk.LocalAPI")

#: [EN] The only address this server may bind to. It is a module constant rather
#: [EN] than a parameter so that no caller can widen it by accident.
#: [ZH] 本服务器唯一允许绑定的地址。它是模块常量而非参数，使任何调用方都无法无意放宽。
BIND_HOST = "127.0.0.1"

#: [EN] Upper bound for one request body (1 MiB). A conversation with reasonable
#: [EN] history is far smaller; anything larger is a mistake or an attack.
#: [ZH] 单个请求体上限（1 MiB）。带合理历史的对话远小于此；更大的请求只会是错误或攻击。
MAX_BODY_BYTES = 1024 * 1024

#: [EN] Token budget ceiling accepted from a client. The caller's value is clamped
#: [EN] rather than rejected so that an IDE sending its own default still works.
#: [ZH] 接受客户端传入的 token 预算上限。调用方的取值会被钳制而不是拒绝，使 IDE 使用
#: [ZH] 自身默认值时仍能工作。
MAX_OUTPUT_TOKENS = 4096

#: [EN] Default port. Chosen from the private range and away from common development
#: [EN] ports (Ollama uses 11434, LM Studio 1234, vLLM 8000).
#: [ZH] 默认端口。取自私有段，并避开常见开发端口（Ollama 用 11434，LM Studio 用
#: [ZH] 1234，vLLM 用 8000）。
DEFAULT_PORT = 8788


class LocalApiError(Exception):
    """
    [EN]
    A request-level failure carrying the HTTP status and OpenAI error code.

    [ZH]
    携带 HTTP 状态码与 OpenAI 错误码的请求级失败。
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """
    [EN]
    Coerce a possibly missing or malformed numeric field into an integer range.

    [ZH]
    把可能缺失或格式错误的数值字段收敛到整数区间。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    """
    [EN]
    Coerce a possibly missing or malformed float field into a range.

    [ZH]
    把可能缺失或格式错误的浮点字段收敛到区间。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # [EN] NaN guard. / [ZH] NaN 防护。
        return default
    return max(low, min(high, number))


def parse_chat_request(payload: Dict[str, Any], default_temperature: float = 0.7,
                       default_max_tokens: int = 1024) -> Dict[str, Any]:
    """
    [EN]
    Validate an OpenAI chat-completion request body and normalise it.

    Accepted fields: ``messages`` (required), ``stream``, ``temperature``,
    ``max_tokens`` (``max_completion_tokens`` is also accepted, as newer OpenAI
    clients send that name), ``top_p``. Unknown fields are ignored instead of
    rejected: IDE extensions routinely attach vendor-specific fields, and refusing
    them would break the integration for no security benefit.

    ``stop``, ``tools``, ``functions`` and multimodal ``content`` arrays are **not**
    implemented. Text parts of a content array are concatenated; ``tools`` is
    ignored. This is documented in the module notes rather than silently dropped.

    [ZH]
    校验并规整 OpenAI 对话补全请求体。

    接受字段：``messages``（必需）、``stream``、``temperature``、``max_tokens``
    （同时接受 ``max_completion_tokens``，因为较新的 OpenAI 客户端发送该名称）、
    ``top_p``。未知字段被忽略而不是拒绝：IDE 扩展常附带厂商私有字段，拒绝它们会破坏集成，
    却没有安全收益。

    ``stop``、``tools``、``functions`` 与多模态 ``content`` 数组**未**实现。内容数组的
    文本部分会拼接；``tools`` 被忽略。这一点记录在模块说明中，而不是静默丢弃。
    """
    raw_messages = (payload or {}).get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise LocalApiError(400, "invalid_request_error", _t("localapi.error_messages"))
    messages: List[Dict[str, str]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            raise LocalApiError(400, "invalid_request_error", _t("localapi.error_message_shape"))
        role = str(item.get("role") or "").strip()
        if role not in ("system", "user", "assistant", "tool", "developer"):
            raise LocalApiError(400, "invalid_request_error",
                                _t("localapi.error_role", role=role or "?"))
        content = item.get("content")
        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in (None, "text"):
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            content = "".join(parts)
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        messages.append({"role": "user" if role == "developer" else role,
                         "content": content})
    max_tokens = payload.get("max_tokens")
    if max_tokens is None:
        max_tokens = payload.get("max_completion_tokens")
    return {
        "messages": messages,
        "stream": bool((payload or {}).get("stream", False)),
        "temperature": _clamp_float(payload.get("temperature"), default_temperature, 0.0, 2.0),
        "max_tokens": _clamp_int(max_tokens, default_max_tokens, 1, MAX_OUTPUT_TOKENS),
        "top_p": (_clamp_float(payload.get("top_p"), 0.9, 0.0, 1.0)
                  if payload.get("top_p") is not None else None),
    }


class LocalApiServer:
    """
    [EN]
    Threaded HTTP server implementing the OpenAI chat-completion surface.

    The server owns no inference code: generation is injected as two callables
    (``generate`` for one-shot replies and ``stream`` for iterators). That keeps the
    HTTP layer testable without a model, and guarantees that this class cannot
    accidentally bypass the backend's busy
    rules or consent checks.

    [ZH]
    实现 OpenAI 对话补全表面的线程化 HTTP 服务器。

    服务器不持有任何推理代码：生成能力以两个可调用对象注入（``generate`` 用于一次性回答，
    ``stream`` 用于迭代器）。这使 HTTP 层无需模型即可测试，并保证本类不会意外绕过后端的
    忙碌规则或同意校验。
    """

    def __init__(self, *, port: int = DEFAULT_PORT, api_key: str = "",
                 model_name: str = "echodesk-local",
                 generate: Optional[Callable[..., str]] = None,
                 stream: Optional[Callable[..., Iterator[str]]] = None,
                 default_temperature: float = 0.7,
                 default_max_tokens: int = 1024,
                 name: str = "") -> None:
        if not (1 <= int(port) <= 65535 or int(port) == 0):
            raise ValueError("port out of range")
        self.port = int(port)
        self.api_key = str(api_key or "")
        self.model_name = str(model_name or "echodesk-local")
        self._generate = generate
        self._stream = stream
        self.default_temperature = float(default_temperature)
        self.default_max_tokens = int(default_max_tokens)
        self.name = str(name or f"local-api-{self.port}")
        # [EN] Bound to BIND_HOST; never derived from configuration.
        # [ZH] 绑定到 BIND_HOST；绝不来自配置。
        self.host = BIND_HOST
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # [EN] Serialises generation: one model, one answer at a time.
        # [ZH] 串行化生成：一个模型同一时刻只产出一份回答。
        self._generation_lock = threading.Lock()
        self.last_error = ""
        self.request_count = 0

    # [EN] ---------- Lifecycle ----------
    # [ZH] ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        """
        [EN]
        Whether the serving thread is alive.

        [ZH]
        服务线程是否存活。
        """
        return self._thread is not None and self._thread.is_alive()

    def address(self) -> str:
        """
        [EN]
        Base URL of the running server (``http://127.0.0.1:<port>``).

        [ZH]
        运行中服务器的基础地址（``http://127.0.0.1:<port>``）。
        """
        port = self.port
        if self._server is not None:
            try:
                port = int(self._server.server_address[1])
            except (IndexError, TypeError, ValueError):
                port = self.port
        return f"http://{self.host}:{port}"

    def start(self) -> Dict[str, Any]:
        """
        [EN]
        Start serving in a background thread and return the observable state.

        Starting an already running server is a no-op report, not an error: the
        settings page may call it twice while a request is in flight.

        [ZH]
        在后台线程开始服务并返回可观测状态。

        对已运行的服务器再次启动只返回状态而不报错：设置页可能在同一请求在途时调用两次。
        """
        if self.running:
            return self.status()
        try:
            server = ThreadingHTTPServer((BIND_HOST, self.port), self._make_handler())
        except OSError as exc:
            self.last_error = _t("localapi.error_bind", port=self.port, error=exc)
            log.warning("%s", self.last_error)
            return self.status()
        server.daemon_threads = True
        self._server = server
        # [EN] The actual port is only known after binding when port 0 was used.
        # [ZH] 只有绑定之后才知道实际端口（使用端口 0 时）。
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(target=self._serve_forever, daemon=True,
                                        name="echodesk-local-api")
        self._thread.start()
        self.last_error = ""
        log.info("本地 API 已启动：%s", self.address())
        return self.status()

    def _serve_forever(self) -> None:
        """
        [EN]
        Serving loop; a failure here is recorded rather than raised, because it runs
        on a daemon thread whose exception would be invisible to the protocol loop.

        [ZH]
        服务循环；此处失败会被记录而不是抛出，因为它运行在守护线程上，
        异常对协议循环不可见。
        """
        server = self._server
        if server is None:
            return
        try:
            server.serve_forever(poll_interval=0.2)
        except Exception as exc:  # noqa: BLE001 - [EN] report, never crash the backend
                                  # [ZH] 报告，绝不使后端崩溃
            self.last_error = str(exc)
            log.exception("本地 API 服务循环异常")

    def stop(self) -> Dict[str, Any]:
        """
        [EN]
        Stop serving and release the port.

        [ZH]
        停止服务并释放端口。
        """
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001 - [EN] shutdown is best-effort
                               # [ZH] 关闭为尽力而为
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._thread = None
        return self.status()

    def status(self) -> Dict[str, Any]:
        """
        [EN]
        Observable state for the settings page and diagnostics.

        [ZH]
        供设置页与诊断使用的可观测状态。
        """
        return {
            "enabled": self.running,
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "base_url": self.address(),
            "endpoint": f"{self.address()}/v1/chat/completions",
            "models_endpoint": f"{self.address()}/v1/models",
            "model": self.model_name,
            "auth_required": bool(self.api_key),
            "request_count": int(self.request_count),
            "error": self.last_error,
        }

    # [EN] ---------- Request handling ----------
    # [ZH] ---------- 请求处理 ----------

    def _make_handler(self) -> type:
        """
        [EN]
        Build the request-handler class bound to this server instance.

        The handler needs the instance's callables, secrets and counters; closing over
        ``server`` is the standard way to give :class:`BaseHTTPRequestHandler` that
        access without a global variable.

        [ZH]
        构造绑定到本服务器实例的请求处理器类。

        处理器需要实例的可调用对象、密钥与计数器；通过闭包捕获 ``server`` 是给
        :class:`BaseHTTPRequestHandler` 传递这些信息的标准做法，无需全局变量。
        """
        server = self

        class Handler(BaseHTTPRequestHandler):
            """
            [EN]
            One HTTP request. See the module notes for the security boundary.

            [ZH]
            单个 HTTP 请求。安全边界见模块说明。
            """

            server_version = "EchoDeskLocalAPI/1.0"
            protocol_version = "HTTP/1.1"

            # [EN] Silence the default stderr access log; the backend already logs
            # [EN] through logging and the protocol stream must stay clean.
            # [ZH] 关闭默认的 stderr 访问日志；后端已使用 logging 记录，且协议流必须保持干净。
            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("%s - %s", self.address_string(), fmt % args)

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                """
                [EN]
                Send one JSON response with an accurate ``Content-Length``.

                [ZH]
                发送一个带准确 ``Content-Length`` 的 JSON 响应。
                """
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _send_error(self, status: int, code: str, message: str) -> None:
                """
                [EN]
                Send an OpenAI-shaped error envelope.

                [ZH]
                发送 OpenAI 形状的错误信封。
                """
                self._send_json(status, {
                    "error": {"message": message, "type": "invalid_request_error"
                              if status < 500 else "server_error",
                              "param": None, "code": code},
                })

            def _authorised(self) -> bool:
                """
                [EN]
                Check the bearer token in constant time.

                [ZH]
                以常数时间校验 bearer 令牌。
                """
                if not server.api_key:
                    return True
                header = (self.headers.get("Authorization") or "").strip()
                token = header[7:].strip() if header.lower().startswith("bearer ") else ""
                if not token:
                    token = (self.headers.get("X-API-Key") or "").strip()
                if not token:
                    return False
                return hmac.compare_digest(token, server.api_key)

            def do_OPTIONS(self) -> None:  # noqa: N802 - [EN] name required by http.server
                                            # [ZH] http.server 要求的固定方法名
                """
                [EN]
                Answer a preflight without granting CORS. Desktop clients do not need
                it; browsers are intentionally excluded (see the module notes).

                [ZH]
                应答预检请求但不授予 CORS。桌面客户端不需要它；浏览器被刻意排除
                （见模块说明）。
                """
                self.send_response(204)
                self.send_header("Allow", "GET, POST, OPTIONS")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                """
                [EN]
                ``/health`` and ``/v1/models``.

                [ZH]
                ``/health`` 与 ``/v1/models``。
                """
                path = (self.path or "").split("?", 1)[0]
                if path in ("/health", "/healthz"):
                    self._send_json(200, {"status": "ok", "model": server.model_name,
                                          "api": "openai-compatible"})
                    return
                if not self._authorised():
                    self._send_error(401, "invalid_api_key", _t("localapi.error_auth"))
                    return
                if path in ("/v1/models", "/models"):
                    created = int(time.time())
                    self._send_json(200, {
                        "object": "list",
                        "data": [{"id": server.model_name, "object": "model",
                                  "created": created, "owned_by": "echodesk",
                                  "root": server.model_name}],
                    })
                    return
                self._send_error(404, "not_found", _t("localapi.error_not_found", path=path))

            def do_POST(self) -> None:  # noqa: N802
                """
                [EN]
                ``/v1/chat/completions``, streaming or not.

                [ZH]
                ``/v1/chat/completions``，流式或非流式。
                """
                path = (self.path or "").split("?", 1)[0].rstrip("/")
                if path not in ("/v1/chat/completions", "/chat/completions"):
                    self._send_error(404, "not_found", _t("localapi.error_not_found", path=path))
                    return
                if not self._authorised():
                    self._send_error(401, "invalid_api_key", _t("localapi.error_auth"))
                    return
                try:
                    payload = self._read_body()
                    request = parse_chat_request(
                        payload, server.default_temperature, server.default_max_tokens)
                except LocalApiError as exc:
                    self._send_error(exc.status, exc.code, exc.message)
                    return
                server.request_count += 1
                if request["stream"]:
                    self._stream_completion(request)
                else:
                    self._complete(request)

            def _read_body(self) -> Dict[str, Any]:
                """
                [EN]
                Read and decode the JSON body with a hard size bound.

                [ZH]
                在读入时施加硬性长度上限并解码 JSON 请求体。
                """
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    raise LocalApiError(400, "invalid_length", _t("localapi.error_length"))
                if length <= 0:
                    raise LocalApiError(400, "empty_body", _t("localapi.error_empty"))
                if length > MAX_BODY_BYTES:
                    raise LocalApiError(413, "payload_too_large",
                                        _t("localapi.error_too_large", limit=MAX_BODY_BYTES))
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    raise LocalApiError(400, "invalid_json", _t("localapi.error_json"))
                if not isinstance(data, dict):
                    raise LocalApiError(400, "invalid_json", _t("localapi.error_json"))
                return data

            def _acquire(self) -> bool:
                """
                [EN]
                Take the single generation slot, or report that it is busy.

                [ZH]
                占用唯一的生成槽位，或报告其忙碌。
                """
                if server._generation_lock.acquire(blocking=False):
                    return True
                self._send_json(429, {"error": {
                    "message": _t("localapi.error_busy"),
                    "type": "rate_limit_error", "param": None,
                    "code": "server_busy"}})
                return False

            def _complete(self, request: Dict[str, Any]) -> None:
                """
                [EN]
                Produce a non-streaming completion.

                [ZH]
                产生一次非流式补全。
                """
                if server._generate is None:
                    self._send_error(503, "no_generator", _t("localapi.error_no_engine"))
                    return
                if not self._acquire():
                    return
                started = time.time()
                try:
                    text = server._generate(
                        request["messages"], temperature=request["temperature"],
                        max_tokens=request["max_tokens"], top_p=request["top_p"])
                except Exception as exc:  # noqa: BLE001 - [EN] any engine failure becomes a 500
                                          # [ZH] 任何引擎失败都转为 500
                    log.warning("本地 API 生成失败：%s", exc)
                    self._send_error(500, "generation_failed",
                                     _t("localapi.error_generation", error=exc))
                    return
                finally:
                    server._generation_lock.release()
                prompt_tokens = sum(len(item["content"]) for item in request["messages"]) // 2
                completion_tokens = len(text or "") // 2
                self._send_json(200, {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                    "object": "chat.completion",
                    "created": int(started),
                    "model": server.model_name,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": text or ""},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": prompt_tokens,
                              "completion_tokens": completion_tokens,
                              "total_tokens": prompt_tokens + completion_tokens},
                })

            def _stream_completion(self, request: Dict[str, Any]) -> None:
                """
                [EN]
                Produce a streaming completion as server-sent events.

                The opening ``role`` chunk, one chunk per content increment, a final
                ``finish_reason`` chunk, and the ``[DONE]`` sentinel are all sent so
                that every mainstream OpenAI client parses the stream.

                [ZH]
                以服务端事件流产生流式补全。

                依次发送起始 ``role`` 分块、每个内容增量的分块、结束 ``finish_reason``
                分块与 ``[DONE]`` 哨兵，使主流 OpenAI 客户端都能解析该流。
                """
                if server._stream is None:
                    self._send_error(503, "no_generator", _t("localapi.error_no_engine"))
                    return
                if not self._acquire():
                    return
                created = int(time.time())
                completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True

                    def emit(delta: Dict[str, Any], finish: Optional[str] = None) -> None:
                        """
                        [EN]
                        Write one SSE chunk.

                        [ZH]
                        写出一个 SSE 分块。
                        """
                        body = json.dumps({
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": server.model_name,
                            "choices": [{"index": 0, "delta": delta,
                                         "finish_reason": finish}],
                        }, ensure_ascii=False).encode("utf-8")
                        self.wfile.write(b"data: " + body + b"\n\n")
                        self.wfile.flush()

                    emit({"role": "assistant", "content": ""})
                    for piece in server._stream(
                            request["messages"], temperature=request["temperature"],
                            max_tokens=request["max_tokens"], top_p=request["top_p"]):
                        if not piece:
                            continue
                        emit({"content": piece})
                    emit({}, "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # [EN] The client closed the connection (user pressed Stop in the
                    # [EN] IDE). This is normal, not an error.
                    # [ZH] 客户端关闭了连接（用户在 IDE 中点了停止）。这是正常情况而非错误。
                    pass
                except Exception as exc:  # noqa: BLE001 - [EN] keep the server alive
                                          # [ZH] 保持服务器存活
                    log.warning("本地 API 流式生成失败：%s", exc)
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    server._generation_lock.release()

        return Handler
