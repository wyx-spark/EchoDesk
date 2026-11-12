"""
[EN]
Unified inference-provider layer (v0.2.6 Part 4, task 2.3).

Motivation
    Before this module the backend could only generate with the local GGUF model
    through :class:`llm_engine.LLMEngine`; every caller was hard-wired to that one
    implementation. This module introduces one small interface,
    :class:`InferenceProvider`, with three implementations:

    * :class:`LlamaCppProvider` -- the existing local engine (the default, and the
      only implementation that works with the network switched off);
    * :class:`OllamaProvider` -- an Ollama daemon, normally on ``127.0.0.1:11434``;
    * :class:`OpenAICompatibleProvider` -- any endpoint that speaks the OpenAI
      ``/v1/chat/completions`` protocol, including LM Studio, vLLM, and hosted
      services.

Zero new dependencies
    The official OpenAI SDK would add ``httpx`` plus a dependency tree, and the
    Ollama SDK would add another package, for two HTTP calls that are trivially
    expressible with :mod:`urllib`. The repository already pins its release
    contents (``requirements.lock`` and the SBOM gate), so this module deliberately
    uses only the standard library. That decision belongs to the same lightweight
    audit as :mod:`mcp_client` and is recorded in ``records/``.

The informed-consent red line
    A provider whose endpoint is not on the loopback interface transmits the
    conversation to a third party. EchoDesk's central promise is local privacy, so
    this is **not** a preference that can be defaulted on: it requires an explicit
    grant, stored as ``api_consent_granted``. Two independent layers enforce it:

    1. :func:`apply_settings` raises :class:`ApiConsentRequired` (a
       :class:`PermissionError` subclass, as the design specification demands) when
       an enabled provider points outside the loopback interface without that
       grant, so a request cannot be saved by mistake;
    2. :class:`ProvidersConfig` (see :mod:`config`) **disables** any such provider
       found in a hand-edited ``config.json`` at load time instead of raising,
       because a configuration file that cannot be loaded would make the program
       impossible to start. The warning is reported on stderr, never silently.

    Consequently "the grant can be absent" is safe in both directions: saving is
    refused, and loading degrades to the local provider.

Threading
    Providers are stateless with respect to generation except for the wrapped
    engine; serialisation is the caller's responsibility (see
    :mod:`local_api`, which owns a lock because HTTP requests arrive on their own
    threads).

[ZH]
统一推理提供方层（v0.2.6 任务 2.3）。

动机
    本模块之前，后端只能通过 :class:`llm_engine.LLMEngine` 用本地 GGUF 模型生成，
    每个调用点都硬绑定到这一实现。本模块引入一个小接口 :class:`InferenceProvider`
    与三个实现：

    * :class:`LlamaCppProvider` —— 现有本地引擎（默认实现，也是断网时唯一可用的实现）；
    * :class:`OllamaProvider` —— Ollama 守护进程，通常位于 ``127.0.0.1:11434``；
    * :class:`OpenAICompatibleProvider` —— 任何遵循 OpenAI
      ``/v1/chat/completions`` 协议的端点，包括 LM Studio、vLLM 与托管服务。

零新增依赖
    官方 OpenAI SDK 会引入 ``httpx`` 及其依赖树，Ollama SDK 又是另一个包，
    而这两个调用用 :mod:`urllib` 就能清晰表达。仓库已用
    ``requirements.lock`` 与 SBOM 门禁锁定发布内容，因此本模块**刻意只用标准库**。
    该项取舍与 :mod:`mcp_client` 同属轻量化审查，一并记录在 ``records/``。

知情同意红线
    端点不在环回接口上的提供方会把对话发送给第三方。EchoDesk 的核心承诺是本地隐私，
    因此这**不是**可以默认开启的偏好，而必须显式授权，并保存为
    ``api_consent_granted``。两层独立机制共同保证：

    1. 当已启用的提供方指向环回接口之外且缺少授权时，:func:`apply_settings`
       抛出 :class:`ApiConsentRequired`（:class:`PermissionError` 的子类，符合设计
       规范要求），使误操作无法保存；
    2. 配置模型 :class:`ProvidersConfig`（见 :mod:`config`）在加载手改的
       ``config.json`` 时**直接禁用**这类提供方而不抛异常 —— 否则文件无法加载，
       程序将彻底无法启动。告警写入 stderr，绝不静默。

    因此"授权缺失"在两个方向上都是安全的：保存被拒绝，加载退化为本地提供方。

线程
    除被包装的引擎外，提供方相对生成本身无状态；串行化由调用方负责
    （见 :mod:`local_api`，它持有锁，因为 HTTP 请求来自各自线程）。
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional

from i18n import t as _t

#: [EN] Provider kind identifiers. These strings are persisted in ``config.json`` and
#: [EN] travel over the front-end protocol, so they are treated as a stable schema.
#: [ZH] 提供方类型标识。这些字符串会写入 ``config.json`` 并经前端协议传输，视为稳定模式。
KIND_LOCAL = "llamacpp"
KIND_OLLAMA = "ollama"
KIND_OPENAI = "openai_compatible"

#: [EN] Every kind accepted by configuration validation.
#: [ZH] 配置校验接受的全部类型。
SUPPORTED_KINDS = (KIND_LOCAL, KIND_OLLAMA, KIND_OPENAI)

#: [EN] Host names that denote the loopback interface. ``0.0.0.0`` is deliberately
#: [EN] absent: it is a bind address, not a destination, and treating it as local
#: [EN] would let a typo silently bypass the consent gate.
#: [ZH] 表示环回接口的主机名。刻意**不包含** ``0.0.0.0``：它是绑定地址而非目标地址，
#: [ZH] 把它当作本地会让一个笔误悄悄绕过同意门禁。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"})

#: [EN] Default endpoints. Ollama's documented default port is 11434; the OpenAI
#: [EN] compatible endpoint has no sensible default and must be supplied by the user.
#: [ZH] 默认端点。Ollama 文档默认端口为 11434；OpenAI 兼容端点没有合理默认值，
#: [ZH] 必须由用户填写。
DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"

#: [EN] Upper bound for a single HTTP response body read into memory (16 MiB). A
#: [EN] misconfigured endpoint must not be able to exhaust the process.
#: [ZH] 单次 HTTP 响应体读入内存的上限（16 MiB）。配置错误的端点不得耗尽进程内存。
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ProviderError(RuntimeError):
    """
    [EN]
    A provider call failed: unreachable endpoint, HTTP error, malformed response, or
    an unsupported configuration.

    [ZH]
    提供方调用失败：端点不可达、HTTP 错误、响应格式错误或配置不受支持。
    """


class ApiConsentRequired(PermissionError):
    """
    [EN]
    An external endpoint was configured without ``api_consent_granted``.

    It derives from :class:`PermissionError` because the design specification demands
    exactly that exception for this case: "the backend must raise ``PermissionError``
    and refuse the save". Callers may therefore catch either the concrete class or
    the built-in type.

    [ZH]
    在未取得 ``api_consent_granted`` 的情况下配置了外部端点。

    它继承 :class:`PermissionError`，因为设计规范对该情形正是如此要求：
    "后端必须抛出 ``PermissionError`` 拒绝保存"。因此调用方既可以捕获具体类型，
    也可以捕获内置类型。
    """


# ===========================================================================
# [EN] Endpoint classification
# [ZH] 端点分类
# ===========================================================================

def endpoint_host(url: str) -> str:
    """
    [EN]
    Return the lower-cased host name of an endpoint, or an empty string when the
    value cannot be parsed.

    A malformed endpoint is reported as "not loopback" so that it fails closed: an
    unparsable address must never be treated as local.

    [ZH]
    返回端点的主机名（小写），无法解析时返回空字符串。

    格式错误的端点一律按"非环回"处理，即**失败时关闭**：无法解析的地址绝不能被
    当作本地地址。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        # [EN] ``urllib.parse`` treats "127.0.0.1:11434" as a scheme plus path; add
        # [EN] the most likely scheme so a user entering a bare host still works.
        # [ZH] ``urllib.parse`` 会把 "127.0.0.1:11434" 解析成 scheme 加 path；
        # [ZH] 补上最可能的协议，使用户只填主机名时也能工作。
        text = "http://" + text
    try:
        return (urllib.parse.urlsplit(text).hostname or "").strip().lower()
    except ValueError:
        return ""


def is_loopback_endpoint(url: str) -> bool:
    """
    [EN]
    Whether an endpoint stays on the local machine.

    Scheme is checked as well: only ``http`` and ``https`` are valid generation
    transports. A non-HTTP scheme (``file:``, ``ftp:``) is rejected, which also
    blocks local-file access through a crafted endpoint.

    [ZH]
    端点是否只在本机。

    同时校验协议：只有 ``http`` 与 ``https`` 是有效的生成传输。非 HTTP 协议
    （``file:``、``ftp:``）被拒绝，这也封堵了通过构造端点读取本地文件的路径。
    """
    text = str(url or "").strip()
    if not text:
        return True
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").strip().lower()
    if host in LOOPBACK_HOSTS:
        return True
    # [EN] Accept any 127.0.0.0/8 address, not only 127.0.0.1: the whole range is
    # [EN] loopback by definition, and some tools bind 127.0.0.1 variations.
    # [ZH] 接受整个 127.0.0.0/8 网段而不只是 127.0.0.1：该网段按定义均为环回，
    # [ZH] 且部分工具会绑定 127.0.0.1 的其他变体。
    try:
        return socket.inet_aton(host).startswith(b"\x7f") or host == "::1"
    except OSError:
        return False


def _normalize_base(url: str, *, default: str = "") -> str:
    """
    [EN]
    Normalise an endpoint: apply the default when empty, add ``http://`` when no
    scheme is present, and strip trailing slashes so path joining is unambiguous.

    [ZH]
    规范化端点：为空时套用默认值，缺少协议时补 ``http://``，并去掉尾部斜杠，
    使路径拼接无歧义。
    """
    text = str(url or "").strip()
    if not text:
        text = default
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text.rstrip("/")


# ===========================================================================
# [EN] HTTP plumbing (standard library only)
# [ZH] HTTP 基础实现（仅标准库）
# ===========================================================================

def _headers(api_key: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    [EN]
    Build request headers. The key is only sent when non-empty, so a local endpoint
    that does not use authentication is not handed a meaningless ``Authorization``
    header (some strict servers reject an empty bearer token).

    [ZH]
    构造请求头。密钥为空时不发送，因此不使用鉴权的本地端点不会收到无意义的
    ``Authorization`` 头（部分严格的服务器会拒绝空的 bearer token）。
    """
    out = {"Content-Type": "application/json", "Accept": "application/json"}
    key = str(api_key or "").strip()
    if key:
        out["Authorization"] = f"Bearer {key}"
    if extra:
        out.update(extra)
    return out


def _read_json(response: Any) -> Dict[str, Any]:
    """
    [EN]
    Read and decode one JSON object from a response, with a size bound.

    [ZH]
    从响应中读取并解码一个 JSON 对象，并施加长度上限。
    """
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError(_t("provider.error_too_large"))
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProviderError(_t("provider.error_bad_json", error=exc)) from exc
    if not isinstance(data, dict):
        raise ProviderError(_t("provider.error_not_object"))
    return data


def _post_json(url: str, payload: Dict[str, Any], *, api_key: str,
               timeout: float) -> Dict[str, Any]:
    """
    [EN]
    Perform one JSON POST and decode the reply.

    [ZH]
    执行一次 JSON POST 并解码响应。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            return _read_json(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - [EN] the body is best-effort diagnostics
                            # [ZH] 响应体仅用于尽力诊断
            detail = ""
        raise ProviderError(_t("provider.error_http", code=exc.code,
                               detail=detail.strip()[:400] or exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(_t("provider.error_unreachable", url=url,
                               error=exc.reason)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise ProviderError(_t("provider.error_timeout", timeout=timeout)) from exc


def _get_json(url: str, *, api_key: str, timeout: float) -> Dict[str, Any]:
    """
    [EN]
    Perform one JSON GET and decode the reply.

    [ZH]
    执行一次 JSON GET 并解码响应。
    """
    request = urllib.request.Request(url, headers=_headers(api_key), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            return _read_json(response)
    except urllib.error.HTTPError as exc:
        raise ProviderError(_t("provider.error_http", code=exc.code,
                               detail=exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(_t("provider.error_unreachable", url=url,
                               error=exc.reason)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise ProviderError(_t("provider.error_timeout", timeout=timeout)) from exc


def _iter_lines(response: Any) -> Iterator[bytes]:
    """
    [EN]
    Iterate a streaming HTTP response line by line.

    ``urllib`` exposes the socket as a line-buffered file object, so iterating the
    response itself yields lines as they arrive. Wrapping the loop here keeps the
    protocol parsers below readable.

    [ZH]
    逐行遍历流式 HTTP 响应。

    ``urllib`` 把套接字暴露为行缓冲文件对象，因此直接遍历响应即可在到达时取得行。
    在此处封装循环，使下面的协议解析器保持可读。
    """
    for line in response:
        yield line if isinstance(line, bytes) else str(line).encode("utf-8")


def _normalize_messages(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    [EN]
    Coerce messages into ``[{"role": ..., "content": ...}]``.

    Only text content is supported: a caller that passes a multimodal content array
    gets its text parts concatenated, because no external provider in this version is
    declared to accept images. Silently forwarding an unknown shape would produce a
    confusing server-side error instead.

    [ZH]
    将消息规整为 ``[{"role": ..., "content": ...}]``。

    仅支持文本内容：传入多模态内容数组的调用方会得到其文本部分的拼接结果，因为本版
    没有声明任何外部提供方接受图像。静默转发未知结构只会在服务端得到令人困惑的错误。
    """
    out: List[Dict[str, str]] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip() or "user"
        content = item.get("content")
        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in (None, "text"):
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            content = "".join(parts)
        out.append({"role": role, "content": str(content or "")})
    return out


# ===========================================================================
# [EN] Provider interface
# [ZH] 提供方接口
# ===========================================================================

class InferenceProvider(ABC):
    """
    [EN]
    One generation backend.

    The interface is intentionally tiny: generation in both modes plus two
    diagnostics methods. Chat history, retrieval and tool orchestration stay in the
    caller (:class:`main.Backend`), so switching providers never changes
    conversation semantics.

    [ZH]
    单个生成后端。

    接口刻意保持极小：两种模式的生成加两个诊断方法。会话历史、检索与工具编排留在
    调用方（:class:`main.Backend`），因此切换提供方不会改变对话语义。
    """

    #: [EN] Kind identifier (see :data:`SUPPORTED_KINDS`).
    #: [ZH] 类型标识（见 :data:`SUPPORTED_KINDS`）。
    kind: str = KIND_LOCAL
    #: [EN] Human-readable name shown in settings and diagnostics.
    #: [ZH] 设置与诊断中显示的可读名称。
    label: str = ""
    #: [EN] Model identifier used by the provider.
    #: [ZH] 提供方使用的模型标识。
    model: str = ""
    #: [EN] Endpoint in use (empty for the local engine).
    #: [ZH] 使用的端点（本地引擎为空）。
    api_base: str = ""

    @property
    def is_external(self) -> bool:
        """
        [EN]
        Whether generation leaves the local machine. The local engine always
        returns False; the HTTP providers derive it from their endpoint.

        [ZH]
        生成过程是否离开本机。本地引擎恒为 False；HTTP 提供方由端点判定。
        """
        return self.kind != KIND_LOCAL and not is_loopback_endpoint(self.api_base)

    @abstractmethod
    def chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
             max_tokens: int = 1024, top_p: Optional[float] = None,
             repeat_penalty: Optional[float] = None) -> str:
        """
        [EN]
        Non-streaming generation; returns the complete answer text.

        [ZH]
        非流式生成，返回完整回答文本。
        """

    @abstractmethod
    def stream_chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
                    max_tokens: int = 1024, top_p: Optional[float] = None,
                    repeat_penalty: Optional[float] = None,
                    stop_event: Optional[threading.Event] = None) -> Iterator[str]:
        """
        [EN]
        Streaming generation; yields answer increments.

        ``stop_event`` is honoured only by the local engine, whose generation loop
        can be interrupted. HTTP providers cannot cancel an in-flight request with
        ``urllib``; the consumer simply stops reading, and the iterator is closed.
        That limitation is documented rather than hidden.

        [ZH]
        流式生成，产出回答增量。

        ``stop_event`` 仅由本地引擎处理，其生成循环可被中断。HTTP 提供方无法用
        ``urllib`` 取消在途请求；消费者停止读取并关闭迭代器即可。该限制如实记录，
        不做隐藏。
        """

    @abstractmethod
    def list_models(self) -> List[str]:
        """
        [EN]
        Return the model identifiers the provider reports.

        [ZH]
        返回提供方报告的模型标识。
        """

    def health(self) -> Dict[str, Any]:
        """
        [EN]
        Connectivity report for settings and diagnostics.

        [ZH]
        供设置与诊断使用的连通性报告。
        """
        started = time.time()
        try:
            models = self.list_models()
        except ProviderError as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": int((time.time() - started) * 1000)}
        return {"ok": True, "models": models[:50], "model_count": len(models),
                "elapsed_ms": int((time.time() - started) * 1000)}

    def describe(self) -> Dict[str, Any]:
        """
        [EN]
        Structured description (no secrets). ``api_key`` is never included: this
        dictionary is written into protocol responses and logs.

        [ZH]
        结构化描述（不含密钥）。绝不包含 ``api_key``：该字典会写入协议响应与日志。
        """
        return {
            "kind": self.kind,
            "label": self.label,
            "model": self.model,
            "api_base": self.api_base,
            "is_external": self.is_external,
        }


class LlamaCppProvider(InferenceProvider):
    """
    [EN]
    The local GGUF engine, adapted to the provider interface.

    The wrapped engine is injected rather than owned: :class:`main.Backend` keeps a
    single engine object for the whole process lifetime (see
    ``llm_engine.LazyLLMEngine``), and the provider must observe hot model reloads
    through that same reference.

    [ZH]
    本地 GGUF 引擎，适配为提供方接口。

    被包装的引擎由外部注入而非由本对象持有：:class:`main.Backend` 在整个进程生命周期内
    保持同一个引擎对象（见 ``llm_engine.LazyLLMEngine``），提供方必须通过该引用观察到
    模型热加载。
    """

    kind = KIND_LOCAL
    label = "llama.cpp (local)"

    def __init__(self, engine: Any, model_name: str = "") -> None:
        self.engine = engine
        self.model = str(model_name or "")
        self.api_base = ""

    def chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
             max_tokens: int = 1024, top_p: Optional[float] = None,
             repeat_penalty: Optional[float] = None) -> str:
        if self.engine is None:
            raise ProviderError(_t("provider.error_no_engine"))
        return self.engine.chat_complete(
            _normalize_messages(messages), temperature=temperature,
            max_tokens=max_tokens, top_p=top_p, repeat_penalty=repeat_penalty)

    def stream_chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
                    max_tokens: int = 1024, top_p: Optional[float] = None,
                    repeat_penalty: Optional[float] = None,
                    stop_event: Optional[threading.Event] = None) -> Iterator[str]:
        if self.engine is None:
            raise ProviderError(_t("provider.error_no_engine"))
        yield from self.engine.chat_stream(
            _normalize_messages(messages), temperature=temperature,
            max_tokens=max_tokens, stop_event=stop_event, top_p=top_p,
            repeat_penalty=repeat_penalty)

    def list_models(self) -> List[str]:
        if self.engine is None:
            return []
        return [self.model] if self.model else []

    def health(self) -> Dict[str, Any]:
        ready = self.engine is not None and bool(getattr(self.engine, "model_path", ""))
        return {"ok": ready, "models": self.list_models(),
                "error": "" if ready else _t("provider.error_no_model"),
                "elapsed_ms": 0}


class OpenAICompatibleProvider(InferenceProvider):
    """
    [EN]
    Any endpoint implementing the OpenAI ``/v1/chat/completions`` protocol.

    The base URL may be given with or without the ``/v1`` suffix; both forms are
    accepted because every compatible product documents it differently (LM Studio
    shows ``http://localhost:1234/v1``, while several providers show the bare
    host). Paths are joined from the normalised base.

    [ZH]
    任何实现 OpenAI ``/v1/chat/completions`` 协议的端点。

    基础地址可以带或不带 ``/v1`` 后缀；两种形式都接受，因为各兼容产品的文档并不一致
    （LM Studio 显示 ``http://localhost:1234/v1``，而不少提供方只给主机名）。
    路径由规范化后的基础地址拼接。
    """

    kind = KIND_OPENAI
    label = "OpenAI-compatible"

    def __init__(self, api_base: str, model: str, api_key: str = "",
                 timeout: float = 120.0) -> None:
        base = _normalize_base(api_base)
        if not base:
            raise ProviderError(_t("provider.error_no_base"))
        # [EN] Accept both ".../v1" and a bare base; remember which form was used so
        # [EN] the UI can display exactly what the user entered.
        # [ZH] 同时接受 ".../v1" 与裸基础地址；记录实际使用形式，
        # [ZH] 使界面能原样显示用户输入。
        self.api_base = base
        self._root = base if base.endswith("/v1") else base + "/v1"
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "")
        self.timeout = max(1.0, float(timeout or 120.0))

    def _payload(self, messages: List[Dict[str, Any]], stream: bool, temperature: float,
                 max_tokens: int, top_p: Optional[float],
                 repeat_penalty: Optional[float]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": _normalize_messages(messages),
            "stream": bool(stream),
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        if top_p is not None:
            payload["top_p"] = float(top_p)
        # [EN] ``repeat_penalty`` is not part of the OpenAI schema. It is forwarded
        # [EN] because llama.cpp-based servers (including EchoDesk's own local API)
        # [EN] accept it as an extension, and silently dropping a user setting is
        # [EN] worse than letting an incompatible server ignore an unknown field.
        # [ZH] ``repeat_penalty`` 不属于 OpenAI 模式。此处仍转发，因为基于 llama.cpp
        # [ZH] 的服务器（包括 EchoDesk 自己的本地 API）把它作为扩展接受；静默丢弃用户
        # [ZH] 设置，比让不兼容的服务器忽略未知字段更糟。
        if repeat_penalty is not None:
            payload["repeat_penalty"] = float(repeat_penalty)
        return payload

    def chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
             max_tokens: int = 1024, top_p: Optional[float] = None,
             repeat_penalty: Optional[float] = None) -> str:
        data = _post_json(f"{self._root}/chat/completions",
                          self._payload(messages, False, temperature, max_tokens,
                                        top_p, repeat_penalty),
                          api_key=self.api_key, timeout=self.timeout)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(_t("provider.error_bad_response", error=exc)) from exc
        return str((message or {}).get("content") or "")

    def stream_chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
                    max_tokens: int = 1024, top_p: Optional[float] = None,
                    repeat_penalty: Optional[float] = None,
                    stop_event: Optional[threading.Event] = None) -> Iterator[str]:
        body = json.dumps(
            self._payload(messages, True, temperature, max_tokens, top_p, repeat_penalty),
            ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._root}/chat/completions", data=body,
            headers=_headers(self.api_key, {"Accept": "text/event-stream"}), method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise ProviderError(_t("provider.error_http", code=exc.code,
                                   detail=exc.reason)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(_t("provider.error_unreachable", url=self.api_base,
                                   error=exc.reason)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderError(_t("provider.error_timeout", timeout=self.timeout)) from exc
        with response:
            for raw in _iter_lines(response):
                if stop_event is not None and stop_event.is_set():
                    return
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    return
                try:
                    payload = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                try:
                    delta = payload["choices"][0].get("delta") or {}
                except (KeyError, IndexError, TypeError):
                    continue
                piece = delta.get("content")
                if piece:
                    yield str(piece)

    def list_models(self) -> List[str]:
        data = _get_json(f"{self._root}/models", api_key=self.api_key, timeout=self.timeout)
        items = data.get("data")
        if not isinstance(items, list):
            items = data.get("models") if isinstance(data.get("models"), list) else []
        names: List[str] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name")
                if name:
                    names.append(str(name))
            elif isinstance(item, str):
                names.append(item)
        return names


class OllamaProvider(InferenceProvider):
    """
    [EN]
    Ollama's native API (``/api/chat``, newline-delimited JSON).

    Ollama also exposes an OpenAI-compatible surface, but its native protocol is used
    because it reports the model list and context metadata more accurately and has
    one fewer translation layer.

    [ZH]
    Ollama 原生 API（``/api/chat``，逐行 JSON）。

    Ollama 也提供 OpenAI 兼容接口，但此处使用其原生协议：模型清单与上下文元数据更准确，
    且少一层转译。
    """

    kind = KIND_OLLAMA
    label = "Ollama"

    def __init__(self, api_base: str = "", model: str = "", api_key: str = "",
                 timeout: float = 120.0) -> None:
        self.api_base = _normalize_base(api_base, default=DEFAULT_OLLAMA_BASE)
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "")
        self.timeout = max(1.0, float(timeout or 120.0))

    def _payload(self, messages: List[Dict[str, Any]], stream: bool, temperature: float,
                 max_tokens: int, top_p: Optional[float],
                 repeat_penalty: Optional[float]) -> Dict[str, Any]:
        options: Dict[str, Any] = {"temperature": float(temperature),
                                   "num_predict": int(max_tokens)}
        if top_p is not None:
            options["top_p"] = float(top_p)
        if repeat_penalty is not None:
            options["repeat_penalty"] = float(repeat_penalty)
        return {"model": self.model, "messages": _normalize_messages(messages),
                "stream": bool(stream), "options": options}

    def chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
             max_tokens: int = 1024, top_p: Optional[float] = None,
             repeat_penalty: Optional[float] = None) -> str:
        data = _post_json(f"{self.api_base}/api/chat",
                          self._payload(messages, False, temperature, max_tokens,
                                        top_p, repeat_penalty),
                          api_key=self.api_key, timeout=self.timeout)
        message = data.get("message")
        if not isinstance(message, dict):
            raise ProviderError(_t("provider.error_bad_response", error="message"))
        return str(message.get("content") or "")

    def stream_chat(self, messages: List[Dict[str, Any]], *, temperature: float = 0.7,
                    max_tokens: int = 1024, top_p: Optional[float] = None,
                    repeat_penalty: Optional[float] = None,
                    stop_event: Optional[threading.Event] = None) -> Iterator[str]:
        body = json.dumps(
            self._payload(messages, True, temperature, max_tokens, top_p, repeat_penalty),
            ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/api/chat", data=body,
            headers=_headers(self.api_key), method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise ProviderError(_t("provider.error_http", code=exc.code,
                                   detail=exc.reason)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(_t("provider.error_unreachable", url=self.api_base,
                                   error=exc.reason)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderError(_t("provider.error_timeout", timeout=self.timeout)) from exc
        with response:
            for raw in _iter_lines(response):
                if stop_event is not None and stop_event.is_set():
                    return
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = payload.get("message")
                if isinstance(message, dict):
                    piece = message.get("content")
                    if piece:
                        yield str(piece)
                if payload.get("done"):
                    return

    def list_models(self) -> List[str]:
        data = _get_json(f"{self.api_base}/api/tags", api_key=self.api_key,
                         timeout=self.timeout)
        items = data.get("models")
        names: List[str] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
        return names


# ===========================================================================
# [EN] Construction and configuration
# [ZH] 构造与配置
# ===========================================================================

def provider_config_model(config: Any) -> Any:
    """
    [EN]
    Return the active provider declaration from a configuration object.

    The lookup is defensive because this module is also used against plain
    dictionaries and partially built objects in tests and in the settings page.

    [ZH]
    从配置对象返回当前生效的提供方声明。

    采用防御式查找，因为测试与设置页也会用普通字典和部分构造的对象调用本模块。
    """
    providers = getattr(config, "providers", None)
    if providers is None:
        return None
    active = str(getattr(providers, "active", KIND_LOCAL) or KIND_LOCAL)
    if active == KIND_LOCAL:
        return None
    for item in (getattr(providers, "providers", []) or []):
        if str(getattr(item, "kind", "")) == active:
            return item
    return None


def build_provider(config: Any, engine: Any = None, model_name: str = "") -> InferenceProvider:
    """
    [EN]
    Build the active provider from configuration.

    Consent is re-checked here as well, even though the configuration model already
    disables unapproved external providers. The reason is defence in depth: an
    in-memory configuration may have been mutated after validation (the settings
    handler does exactly that before saving), and generation must never silently
    leave the machine because of a state that skipped validation.

    [ZH]
    按配置构造当前生效的提供方。

    此处再次校验同意状态，尽管配置模型已经禁用未获批的外部提供方。理由是纵深防御：
    内存中的配置可能在通过校验之后被修改（设置处理器在保存前正是如此），生成过程绝不能
    因为某个跳过了校验的状态而静默离开本机。
    """
    providers = getattr(config, "providers", None)
    active = str(getattr(providers, "active", KIND_LOCAL) or KIND_LOCAL)
    if active == KIND_LOCAL:
        return LlamaCppProvider(engine, model_name)
    if active not in SUPPORTED_KINDS:
        raise ProviderError(_t("provider.error_unknown_kind", kind=active))
    model_cfg = provider_config_model(config)
    if model_cfg is None:
        raise ProviderError(_t("provider.error_missing_config", kind=active))
    if not bool(getattr(model_cfg, "enabled", False)):
        raise ProviderError(_t("provider.error_disabled", kind=active))
    api_base = str(getattr(model_cfg, "api_base", "") or "")
    granted = bool(getattr(providers, "api_consent_granted", False)
                   or getattr(model_cfg, "api_consent_granted", False))
    if not is_loopback_endpoint(api_base) and not granted:
        raise ApiConsentRequired(_t("provider.error_no_consent", endpoint=api_base))
    timeout = float(getattr(providers, "timeout_seconds", 120.0) or 120.0)
    api_key = str(getattr(model_cfg, "api_key", "") or "")
    model = str(getattr(model_cfg, "model", "") or "")
    if active == KIND_OLLAMA:
        return OllamaProvider(api_base=api_base, model=model, api_key=api_key,
                              timeout=timeout)
    return OpenAICompatibleProvider(api_base=api_base, model=model, api_key=api_key,
                                    timeout=timeout)


@dataclass
class ProviderSettings:
    """
    [EN]
    Normalised result of a settings request, ready to be written into the
    configuration model.

    [ZH]
    设置请求的规范化结果，可直接写入配置模型。
    """

    active: str
    timeout_seconds: float
    api_consent_granted: bool
    api_consent_at: str
    providers: List[Dict[str, Any]]


def _now_iso() -> str:
    """
    [EN]
    UTC timestamp used for the consent record (audit trail).

    [ZH]
    用于同意记录的 UTC 时间戳（审计留痕）。
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_settings(payload: Dict[str, Any]) -> ProviderSettings:
    """
    [EN]
    Validate a settings request and return the normalised values.

    Rules:
      * ``active`` must be a supported kind; an unknown value is an error rather than
        a silent fallback, because silently switching back to local would hide a
        broken front-end request;
      * a provider of kind ``llamacpp`` is implicit and never stored in the list;
      * an external endpoint requires the grant; the check happens for every enabled
        provider, not only the active one, because enabling a second external
        provider is the same privacy decision;
      * the grant timestamp is refreshed whenever a new grant is recorded.

    [ZH]
    校验设置请求并返回规范化取值。

    规则：
      * ``active`` 必须是受支持的类型；未知值视为错误而不是静默回落 —— 静默切回本地
        会掩盖前端请求已经损坏；
      * ``llamacpp`` 类型是隐式的，从不存入列表；
      * 外部端点必须取得授权；该检查覆盖**每个已启用**的提供方，而不仅是当前生效的那个，
        因为再启用一个外部提供方是同一个隐私决定；
      * 记录新授权时刷新授权时间戳。
    """
    active = str((payload or {}).get("active") or KIND_LOCAL).strip()
    if active not in SUPPORTED_KINDS:
        raise ProviderError(_t("provider.error_unknown_kind", kind=active))
    try:
        timeout = float((payload or {}).get("timeout_seconds", 120.0) or 120.0)
    except (TypeError, ValueError):
        timeout = 120.0
    timeout = max(5.0, min(600.0, timeout))
    global_grant = bool((payload or {}).get("api_consent_granted", False))
    raw_items = (payload or {}).get("providers")
    items: List[Dict[str, Any]] = []
    seen: set = set()
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip()
            if kind not in (KIND_OLLAMA, KIND_OPENAI):
                raise ProviderError(_t("provider.error_unknown_kind", kind=kind or "?"))
            if kind in seen:
                raise ProviderError(_t("provider.error_duplicate_kind", kind=kind))
            seen.add(kind)
            api_base = str(raw.get("api_base") or "").strip()
            if kind == KIND_OLLAMA and not api_base:
                api_base = DEFAULT_OLLAMA_BASE
            if kind == KIND_OPENAI and not api_base:
                raise ProviderError(_t("provider.error_no_base"))
            if not is_loopback_endpoint(api_base) and not global_grant:
                raise ApiConsentRequired(
                    _t("provider.error_no_consent", endpoint=api_base))
            items.append({
                "kind": kind,
                "api_base": api_base,
                "api_key": str(raw.get("api_key") or ""),
                "model": str(raw.get("model") or "").strip(),
                "enabled": bool(raw.get("enabled", True)),
                "api_consent_granted": bool(
                    global_grant or raw.get("api_consent_granted", False)),
            })
    if active != KIND_LOCAL and active not in seen:
        raise ProviderError(_t("provider.error_active_missing", kind=active))
    consent_at = _now_iso() if global_grant else ""
    return ProviderSettings(active=active, timeout_seconds=timeout,
                            api_consent_granted=global_grant,
                            api_consent_at=consent_at, providers=items)


def apply_settings(config: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    [EN]
    Validate, persist and report a provider-settings request.

    Raises :class:`ApiConsentRequired` (a ``PermissionError``) when an external
    endpoint is enabled without the grant, which is the design specification's
    acceptance test. Nothing is written in that case: the configuration object is
    modified only after validation succeeds.

    [ZH]
    校验、持久化并报告一次提供方设置请求。

    在启用外部端点而未取得授权时抛出 :class:`ApiConsentRequired`（``PermissionError``），
    这正是设计规范的验收条件。此时不写入任何内容：只有校验通过之后才修改配置对象。
    """
    normalized = normalize_settings(payload)
    providers_cfg = getattr(config, "providers", None)
    if providers_cfg is None:
        raise ProviderError(_t("provider.error_no_config"))
    providers_cfg.active = normalized.active
    providers_cfg.timeout_seconds = normalized.timeout_seconds
    providers_cfg.api_consent_granted = normalized.api_consent_granted
    providers_cfg.api_consent_at = normalized.api_consent_at or getattr(
        providers_cfg, "api_consent_at", "")
    providers_cfg.providers = list(normalized.providers)
    config.save()
    return describe_config(config)


def describe_config(config: Any) -> Dict[str, Any]:
    """
    [EN]
    Provider state for the settings page and diagnostics.

    Secrets are masked. ``api_key`` is never returned; only whether one is stored,
    which is what the interface needs to show "configured" without leaking it back
    into a JSON line.

    [ZH]
    供设置页与诊断使用的提供方状态。

    密钥被打码。绝不回传 ``api_key``，只回传"是否已保存"，这正是界面显示"已配置"
    所需的信息，同时避免它再次出现在 JSON 行中。
    """
    providers_cfg = getattr(config, "providers", None)
    active = str(getattr(providers_cfg, "active", KIND_LOCAL) or KIND_LOCAL)
    items: List[Dict[str, Any]] = [
        {"kind": KIND_LOCAL, "label": "llama.cpp (local)", "api_base": "",
         "is_external": False, "enabled": True,
         "model": str(getattr(config, "model_path", "") or "")}]
    for item in (getattr(providers_cfg, "providers", []) or []):
        api_base = str(getattr(item, "api_base", "") or "")
        items.append({
            "kind": str(getattr(item, "kind", "")),
            "label": OllamaProvider.label if getattr(item, "kind", "") == KIND_OLLAMA
                     else OpenAICompatibleProvider.label,
            "api_base": api_base,
            "model": str(getattr(item, "model", "") or ""),
            "enabled": bool(getattr(item, "enabled", False)),
            "api_key_set": bool(str(getattr(item, "api_key", "") or "")),
            "api_consent_granted": bool(getattr(item, "api_consent_granted", False)),
            "is_external": not is_loopback_endpoint(api_base),
        })
    return {
        "active": active,
        "timeout_seconds": float(getattr(providers_cfg, "timeout_seconds", 120.0) or 120.0),
        "api_consent_granted": bool(getattr(providers_cfg, "api_consent_granted", False)),
        "api_consent_at": str(getattr(providers_cfg, "api_consent_at", "") or ""),
        "providers": items,
        "supported_kinds": list(SUPPORTED_KINDS),
        "active_provider": (build_provider_summary(config)),
    }


def build_provider_summary(config: Any) -> Dict[str, Any]:
    """
    [EN]
    Describe the active provider without connecting to it (safe for a status line).

    [ZH]
    描述当前生效的提供方但不连接（可安全用于状态行）。
    """
    providers_cfg = getattr(config, "providers", None)
    active = str(getattr(providers_cfg, "active", KIND_LOCAL) or KIND_LOCAL)
    if active == KIND_LOCAL:
        return {"kind": KIND_LOCAL, "label": "llama.cpp (local)",
                "model": str(getattr(config, "model_path", "") or ""),
                "api_base": "", "is_external": False, "ready": True, "error": ""}
    item = provider_config_model(config)
    if item is None:
        return {"kind": active, "label": "", "model": "", "api_base": "",
                "is_external": True, "ready": False,
                "error": _t("provider.error_missing_config", kind=active)}
    api_base = str(getattr(item, "api_base", "") or "")
    granted = bool(getattr(providers_cfg, "api_consent_granted", False)
                   or getattr(item, "api_consent_granted", False))
    external = not is_loopback_endpoint(api_base)
    ready = bool(getattr(item, "enabled", False)) and (not external or granted)
    error = ""
    if not bool(getattr(item, "enabled", False)):
        error = _t("provider.error_disabled", kind=active)
    elif external and not granted:
        error = _t("provider.error_no_consent", endpoint=api_base)
    return {"kind": active,
            "label": OllamaProvider.label if active == KIND_OLLAMA
                     else OpenAICompatibleProvider.label,
            "model": str(getattr(item, "model", "") or ""),
            "api_base": api_base, "is_external": external, "ready": ready,
            "error": error}


def test_connection(config: Any, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    [EN]
    Test one provider configuration without activating it.

    The request payload may carry an unsaved draft (``kind``, ``api_base``,
    ``api_key``, ``model``), which is what the settings page needs: the user should
    be able to verify an endpoint before committing it. Consent is enforced for the
    draft too, so "test" cannot be used to send a request to a third party without
    the grant.

    [ZH]
    测试一份提供方配置但不激活它。

    请求载荷可以携带尚未保存的草稿（``kind``、``api_base``、``api_key``、``model``），
    这正是设置页需要的能力：用户应当能在提交之前验证端点。草稿同样强制同意，
    因此"测试"无法被用来在未授权的情况下向第三方发送请求。
    """
    draft = payload or {}
    kind = str(draft.get("kind") or KIND_LOCAL).strip()
    if kind == KIND_LOCAL:
        return {"ok": True, "kind": kind, "models": [], "message": _t("provider.test_local")}
    if kind not in SUPPORTED_KINDS:
        return {"ok": False, "kind": kind,
                "error": _t("provider.error_unknown_kind", kind=kind)}
    api_base = str(draft.get("api_base") or "").strip()
    if kind == KIND_OLLAMA and not api_base:
        api_base = DEFAULT_OLLAMA_BASE
    if not is_loopback_endpoint(api_base):
        providers_cfg = getattr(config, "providers", None)
        granted = bool(getattr(providers_cfg, "api_consent_granted", False)
                       or draft.get("api_consent_granted", False))
        if not granted:
            raise ApiConsentRequired(_t("provider.error_no_consent", endpoint=api_base))
    timeout = float(draft.get("timeout_seconds") or
                    getattr(getattr(config, "providers", None), "timeout_seconds", 30.0) or 30.0)
    try:
        if kind == KIND_OLLAMA:
            provider: InferenceProvider = OllamaProvider(
                api_base=api_base, model=str(draft.get("model") or ""),
                api_key=str(draft.get("api_key") or ""), timeout=min(timeout, 30.0))
        else:
            provider = OpenAICompatibleProvider(
                api_base=api_base, model=str(draft.get("model") or ""),
                api_key=str(draft.get("api_key") or ""), timeout=min(timeout, 30.0))
        report = provider.health()
    except ProviderError as exc:
        return {"ok": False, "kind": kind, "api_base": api_base, "error": str(exc)}
    report.update({"kind": kind, "api_base": api_base})
    return report
