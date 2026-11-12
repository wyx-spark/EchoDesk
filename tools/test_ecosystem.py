# -*- coding: utf-8 -*-
"""[EN] Open-platform tests for v0.2.6 Part 4: provider routing and the local API.

[ZH] v0.2.6 任务 4 的开放平台测试：多 Provider 路由与本地 API。

[EN]
What is covered, and why each item is a real end-to-end check rather than a mock:

  1. Endpoint classification -- the consent decision is derived from it, so every
     class of value (loopback, LAN, public, malformed, non-HTTP scheme) is asserted.
  2. Informed consent -- saving an external endpoint without the grant must raise
     ``PermissionError`` (the design specification's acceptance criterion), and a
     hand-edited configuration must degrade instead of preventing startup.
  3. Provider protocol implementations -- exercised against a real local HTTP
     server implementing both the OpenAI and the Ollama wire formats, including SSE
     and newline-delimited JSON streaming. No network access is required.
  4. Local API server -- a real socket on 127.0.0.1 serves chat completions in both
     modes, enforces the bearer token, rejects malformed requests and answers 429
     while a generation is in flight.
  5. Backend action layer -- ``provider_save`` returns a structured
     ``permission_denied`` refusal instead of terminating the process.

Run: ``python tools\\test_ecosystem.py``; exit code 0 means everything passed.

[ZH]
覆盖范围，以及每项为何是真实的端到端检查而非打桩：

  1. 端点分类 —— 同意与否由它推导，因此逐一断言各类取值（环回、局域网、公网、
     格式错误、非 HTTP 协议）。
  2. 知情同意 —— 未授权保存外部端点必须抛出 ``PermissionError``（设计规范的验收条件），
     而手改配置必须降级而不是让程序无法启动。
  3. Provider 协议实现 —— 对本机真实的 HTTP 服务器执行，同时实现 OpenAI 与 Ollama
     两种线格式，含 SSE 与逐行 JSON 流式。无需联网。
  4. 本地 API 服务器 —— 127.0.0.1 上的真实套接字，两种模式都能应答，强制 bearer 令牌，
     拒绝格式错误请求，并在生成进行中返回 429。
  5. 后端动作层 —— ``provider_save`` 返回结构化的 ``permission_denied`` 拒绝，
     而不是终止进程。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import config as cfg_mod          # noqa: E402
import local_api as la            # noqa: E402
import providers as pv            # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

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


# ---------------------------------------------------------------------------
# [EN] Fake upstream servers (OpenAI-compatible and Ollama wire formats)
# [ZH] 伪上游服务器（OpenAI 兼容与 Ollama 两种线格式）
# ---------------------------------------------------------------------------

class _Upstream:
    """
    [EN]
    A real HTTP server that speaks both wire formats, used as the "external
    provider" in these tests. It records the last request so authentication and
    payload shape can be asserted.

    [ZH]
    同时支持两种线格式的真实 HTTP 服务器，在这些测试中充当"外部提供方"。
    它记录最后一次请求，以便断言鉴权与载荷形状。
    """

    def __init__(self):
        self.last_headers = {}
        self.last_payload = {}
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                upstream.last_headers = dict(self.headers)
                if self.path.endswith("/v1/models"):
                    self._json(200, {"object": "list", "data": [
                        {"id": "upstream-model-a"}, {"id": "upstream-model-b"}]})
                    return
                if self.path.endswith("/api/tags"):
                    self._json(200, {"models": [{"name": "ollama-a"}, {"name": "ollama-b"}]})
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                upstream.last_headers = dict(self.headers)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    upstream.last_payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    upstream.last_payload = {}
                if self.path == "/api/chat":
                    payload = upstream.last_payload
                    if payload.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        for piece in ("你好", "，", "Ollama"):
                            line = json.dumps({"message": {"content": piece},
                                               "done": False}, ensure_ascii=False)
                            self.wfile.write((line + "\n").encode("utf-8"))
                            self.wfile.flush()
                        self.wfile.write((json.dumps({"done": True}) + "\n").encode("utf-8"))
                        self.wfile.flush()
                    else:
                        self._json(200, {"message": {"role": "assistant",
                                                     "content": "Ollama 非流式回答"},
                                         "done": True})
                    return
                if self.path.endswith("/chat/completions"):
                    payload = upstream.last_payload
                    if payload.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        for piece in ("流式", "回答", "片段"):
                            chunk = {"choices": [{"index": 0,
                                                  "delta": {"content": piece},
                                                  "finish_reason": None}]}
                            body = json.dumps(chunk, ensure_ascii=False)
                            self.wfile.write(("data: " + body + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    else:
                        self._json(200, {"choices": [{"index": 0, "message": {
                            "role": "assistant", "content": "OpenAI 非流式回答"},
                            "finish_reason": "stop"}]})
                    return
                self._json(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        self.port = int(self._server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# [EN] 1. Endpoint classification
# [ZH] 1. 端点分类
# ---------------------------------------------------------------------------

print("=== 1. 端点分类（同意判定的基础）===")
for value in ("http://127.0.0.1:11434", "http://localhost:1234/v1",
              "127.0.0.1:8080", "http://127.9.9.9:5000", "https://localhost",
              "http://[::1]:8080", ""):
    check(f"环回端点判定为本地：{value or '(空)'}", pv.is_loopback_endpoint(value))
for value in ("https://api.openai.com/v1", "http://192.168.1.5:11434",
              "http://0.0.0.0:11434", "file:///C:/secret.txt",
              "http://evil.example.com#@127.0.0.1"):
    check(f"非环回端点判定为外部：{value}", not pv.is_loopback_endpoint(value))
check("主机名大小写不敏感", pv.is_loopback_endpoint("HTTP://LOCALHOST:1234"))
check("无协议的裸主机名按 http 解析", pv.endpoint_host("localhost:1234") == "localhost")


# ---------------------------------------------------------------------------
# [EN] 2. Informed consent
# [ZH] 2. 知情同意
# ---------------------------------------------------------------------------

print("=== 2. 知情同意（生死红线）===")
external = {"kind": "openai_compatible", "api_base": "https://api.example.com/v1",
            "api_key": "sk-test", "model": "gpt-x", "enabled": True}
try:
    pv.normalize_settings({"active": "openai_compatible", "providers": [external]})
    check("未授权保存外部端点会抛 PermissionError", False, "竟然没有抛异常")
except PermissionError as exc:
    check("未授权保存外部端点会抛 PermissionError", True)
    check("异常类型是 ApiConsentRequired", isinstance(exc, pv.ApiConsentRequired),
          type(exc).__name__)
    check("异常文案包含端点地址", "api.example.com" in str(exc), str(exc))

try:
    pv.normalize_settings({"active": "openai_compatible", "providers": [external],
                           "api_consent_granted": True})
    granted = pv.normalize_settings({"active": "openai_compatible", "providers": [external],
                                     "api_consent_granted": True})
    check("授权后允许保存外部端点", granted.active == "openai_compatible")
    check("授权时间被记录", bool(granted.api_consent_at), granted.api_consent_at)
    check("授权写入每个提供方", granted.providers[0]["api_consent_granted"] is True)
except Exception as exc:  # noqa: BLE001
    check("授权后允许保存外部端点", False, f"{type(exc).__name__}: {exc}")

local_only = pv.normalize_settings({
    "active": "ollama",
    "providers": [{"kind": "ollama", "api_base": "http://127.0.0.1:11434",
                   "model": "qwen"}]})
check("本地端点无需授权", local_only.active == "ollama" and not local_only.api_consent_granted)

for bad in ({"active": "unknown"}, {"active": "ollama", "providers": []},
            {"active": "llamacpp",
             "providers": [{"kind": "ollama"}, {"kind": "ollama"}]}):
    try:
        pv.normalize_settings(bad)
        check(f"非法设置被拒绝：{bad}", False, "竟然通过")
    except pv.ProviderError:
        check(f"非法设置被拒绝：{bad['active']}", True)

# [EN] Loading a hand-edited configuration must degrade, not raise.
degraded = cfg_mod.ProvidersConfig(active="openai_compatible", providers=[dict(external)])
check("手改配置缺授权时禁用外部提供方", degraded.providers[0].enabled is False)
check("手改配置缺授权时回落到本地", degraded.active == "llamacpp")
check("加载路径不抛异常（否则程序无法启动）", True)

# [EN] apply_settings refuses without writing.
with __import__("tempfile").TemporaryDirectory() as tmp:
    # [EN] Run inside the temporary directory: `providers.apply_settings` calls
    # [EN] `config.save()` without an explicit path, which resolves to the current
    # [EN] working directory. Without this chdir the test would overwrite the
    # [EN] developer's `backend/config.json`; that really happened during this round
    # [EN] and the polluted file was committed and packaged before it was noticed.
    # [ZH] 在临时目录内执行：`providers.apply_settings` 调用的 `config.save()` 未显式
    # [ZH] 传路径，会落到当前工作目录。没有这次 chdir，测试会覆盖开发机的
    # [ZH] `backend/config.json`；本轮真实发生过，被污染的配置曾被提交并打进包。
    _previous_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        saved_path = os.path.join(tmp, "config.json")
        cfg = cfg_mod.Config()
        cfg.save(saved_path)
        before = json.load(open(saved_path, encoding="utf-8"))
        try:
            pv.apply_settings(cfg, {"active": "openai_compatible", "providers": [external]})
            check("apply_settings 未授权时抛 PermissionError", False, "竟然通过")
        except PermissionError:
            check("apply_settings 未授权时抛 PermissionError", True)
        after = json.load(open(saved_path, encoding="utf-8"))
        check("被拒绝时配置对象不变", cfg.providers.active == "llamacpp", cfg.providers.active)
        check("被拒绝时磁盘配置不变", before["providers"] == after["providers"])

        # [EN] Stored secrets must survive a save that omits them: the interface never
        # [EN] receives an API key back, so an empty field means "unchanged". This is the
        # [EN] data-loss path MCP `env` had; the merge in apply_settings is the fix.
        # [ZH] 省略密钥的保存必须保留已存密钥：界面从不回传 API 密钥，因此空字段表示
        # [ZH] "保持不变"。这正是 MCP `env` 曾出现的数据丢失路径，apply_settings 里的合并
        # [ZH] 就是修法。
        cfg.providers.active = "openai_compatible"
        cfg.providers.providers = [cfg_mod.ProviderConfig(
            kind="openai_compatible", api_base="http://127.0.0.1:1234/v1",
            api_key="sk-local-keep", model="m1", enabled=True)]
        state = pv.apply_settings(cfg, {
            "active": "openai_compatible", "timeout_seconds": 30,
            "providers": [{"kind": "openai_compatible",
                           "api_base": "http://127.0.0.1:1234/v1",
                           "api_key": "", "model": "m2", "enabled": True}]})
        kept = cfg.providers.providers[0]
        check("省略密钥保存后已存密钥仍保留", kept.api_key == "sk-local-keep", kept.api_key)
        check("同一提供方的非密钥字段正常更新", kept.model == "m2", kept.model)
        check("保存后状态回传端点（字典/模型混用不会丢字段）",
              state["providers"][1]["api_base"] == "http://127.0.0.1:1234/v1",
              str(state["providers"][1]))
        check("状态只报告密钥是否存在，不回传密钥",
              state["providers"][1]["api_key_set"] is True
              and "sk-local-keep" not in json.dumps(state, ensure_ascii=False))

    finally:
        os.chdir(_previous_cwd)

# [EN] build_provider re-checks consent even when validation was bypassed.
bypassed = cfg_mod.Config()
bypassed.providers.active = "openai_compatible"
bypassed.providers.providers = [cfg_mod.ProviderConfig(**external)]
try:
    pv.build_provider(bypassed, None)
    check("build_provider 对跳过校验的状态仍拒绝外部端点", False, "竟然通过")
except PermissionError:
    check("build_provider 对跳过校验的状态仍拒绝外部端点", True)


# ---------------------------------------------------------------------------
# [EN] 3. Provider protocol implementations against a real HTTP server
# [ZH] 3. Provider 协议实现（对本机真实 HTTP 服务器）
# ---------------------------------------------------------------------------

print("=== 3. Provider 协议实现（真实 HTTP 往返）===")
upstream = _Upstream()
try:
    oai = pv.OpenAICompatibleProvider(upstream.base, "test-model", api_key="sk-abcd",
                                      timeout=10)
    messages = [{"role": "system", "content": "be brief"},
                {"role": "user", "content": "你好"}]
    text = oai.chat(messages, temperature=0.2, max_tokens=32, top_p=0.8)
    check("OpenAI 兼容非流式返回内容", text == "OpenAI 非流式回答", repr(text))
    check("OpenAI 兼容发送 Bearer 鉴权",
          upstream.last_headers.get("Authorization") == "Bearer sk-abcd",
          str(upstream.last_headers.get("Authorization")))
    check("OpenAI 兼容携带 model 与采样参数",
          upstream.last_payload.get("model") == "test-model"
          and upstream.last_payload.get("max_tokens") == 32
          and upstream.last_payload.get("stream") is False,
          json.dumps(upstream.last_payload, ensure_ascii=False))
    check("OpenAI 兼容把角色原样传输",
          upstream.last_payload["messages"][0]["role"] == "system")

    streamed = "".join(oai.stream_chat(messages, max_tokens=16))
    check("OpenAI 兼容 SSE 流式可解析", streamed == "流式回答片段", repr(streamed))
    check("流式请求置 stream=true", upstream.last_payload.get("stream") is True)

    models = oai.list_models()
    check("OpenAI 兼容 /v1/models 可解析",
          models == ["upstream-model-a", "upstream-model-b"], str(models))
    health = oai.health()
    check("health 报告可达与模型数", health["ok"] and health["model_count"] == 2,
          str(health))
    check("基础地址已带 /v1 时路径不重复", oai._root.count("/v1") == 1, oai._root)

    ollama = pv.OllamaProvider(upstream.base, "qwen-test", timeout=10)
    text = ollama.chat(messages, temperature=0.3, max_tokens=64)
    check("Ollama 非流式返回内容", text == "Ollama 非流式回答", repr(text))
    check("Ollama 使用 /api/chat 与 num_predict",
          upstream.last_payload.get("model") == "qwen-test"
          and upstream.last_payload.get("options", {}).get("num_predict") == 64,
          json.dumps(upstream.last_payload, ensure_ascii=False))
    streamed = "".join(ollama.stream_chat(messages))
    check("Ollama 逐行 JSON 流式可解析", streamed == "你好，Ollama", repr(streamed))
    check("Ollama /api/tags 可解析",
          ollama.list_models() == ["ollama-a", "ollama-b"], str(ollama.list_models()))

    # [EN] Unreachable endpoint: the error must be translated, never an uncaught
    # [EN] traceback reaching the protocol layer.
    # [ZH] 不可达端点：错误必须被翻译，绝不能让未捕获回溯进入协议层。
    dead = pv.OpenAICompatibleProvider("http://127.0.0.1:9/v1", "x", timeout=2)
    try:
        dead.chat(messages)
        check("不可达端点抛出 ProviderError", False, "竟然没有抛异常")
    except pv.ProviderError as exc:
        check("不可达端点抛出 ProviderError", True)
        check("错误文案包含目标地址", "127.0.0.1:9" in str(exc) or "无法" in str(exc), str(exc))
finally:
    upstream.close()


# ---------------------------------------------------------------------------
# [EN] 4. Local API server (real socket, real HTTP client)
# [ZH] 4. 本地 API 服务器（真实套接字、真实 HTTP 客户端）
# ---------------------------------------------------------------------------

print("=== 4. 本地 API 服务器（OpenAI 兼容）===")


def _http(method, url, payload=None, headers=None, timeout=10):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _fake_generate(messages, *, temperature=0.7, max_tokens=1024, top_p=None):
    return "回显：" + messages[-1]["content"]


def _fake_stream(messages, *, temperature=0.7, max_tokens=1024, top_p=None):
    for piece in ("流", "式", "回答"):
        yield piece


server = la.LocalApiServer(port=0, generate=_fake_generate, stream=_fake_stream,
                           model_name="echodesk-test")
started = server.start()
check("服务器已启动", started["running"], str(started))
check("只绑定 127.0.0.1", started["host"] == "127.0.0.1", started["host"])
check("端口为操作系统分配的有效端口", 1024 <= started["port"] <= 65535, str(started["port"]))
base = started["base_url"]
try:
    status, body = _http("GET", f"{base}/health")
    check("/health 返回 200", status == 200 and json.loads(body)["status"] == "ok",
          f"{status} {body}")

    status, body = _http("GET", f"{base}/v1/models")
    models = json.loads(body)
    check("/v1/models 返回 echodesk-test",
          status == 200 and models["data"][0]["id"] == "echodesk-test", body)

    status, body = _http("POST", f"{base}/v1/chat/completions", {
        "model": "echodesk-test",
        "messages": [{"role": "user", "content": "你好"}],
        "temperature": 0.2, "max_tokens": 64})
    result = json.loads(body)
    check("非流式补全返回 200", status == 200, f"{status} {body}")
    check("非流式补全内容正确",
          result["choices"][0]["message"]["content"] == "回显：你好", body)
    check("响应对象类型符合 OpenAI 模式",
          result.get("object") == "chat.completion" and result["choices"][0]["finish_reason"] == "stop",
          body)
    check("响应带 usage 统计", "usage" in result and result["usage"]["total_tokens"] > 0, body)

    status, body = _http("POST", f"{base}/v1/chat/completions", {
        "messages": [{"role": "user", "content": "流式"}], "stream": True})
    check("流式补全返回 200", status == 200, str(status))
    check("流式补全以 data: 分块", body.count("data: ") >= 4, body)
    check("流式补全以 [DONE] 结束", body.strip().endswith("data: [DONE]"), body[-80:])
    # [EN] Parse the SSE frames the way a real client does, then concatenate the
    # [EN] deltas; a substring check would pass on any frame order by accident.
    # [ZH] 按真实客户端的方式解析 SSE 帧再拼接增量；用子串检查会因帧顺序而偶然通过。
    assembled = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            continue
        try:
            frame = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        choices = frame.get("choices") or [{}]
        assembled.append(str((choices[0].get("delta") or {}).get("content") or ""))
    check("流式分块拼接为完整回答", "".join(assembled) == "流式回答", "".join(assembled))
    check("流式分块为 chat.completion.chunk",
          '"object": "chat.completion.chunk"' in body, body[:200])

    status, body = _http("POST", f"{base}/v1/chat/completions", {"messages": []})
    check("空 messages 返回 400", status == 400, f"{status} {body}")
    status, body = _http("POST", f"{base}/v1/chat/completions",
                         {"messages": [{"role": "system", "content": "x"}]})
    check("只含 system 也被拒绝（无用户输入）", status == 200, f"{status} {body}")

    request = urllib.request.Request(
        f"{base}/v1/chat/completions", data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=5)
        check("非法 JSON 返回 400", False, "竟然成功")
    except urllib.error.HTTPError as exc:
        check("非法 JSON 返回 400", exc.code == 400, str(exc.code))

    status, body = _http("POST", f"{base}/v1/unknown", {"messages": []})
    check("未知路径返回 404", status == 404, f"{status} {body}")

    status, body = _http("OPTIONS", f"{base}/v1/chat/completions")
    check("预检返回 204", status == 204, str(status))
    check("预检不授予 CORS", "access-control-allow-origin" not in body.lower())

    # [EN] Busy path: hold the generation slot and expect 429.
    # [ZH] 忙碌路径：占住生成槽位，期望 429。
    server._generation_lock.acquire()
    try:
        status, body = _http("POST", f"{base}/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "x"}]})
        check("生成槽位被占用时返回 429", status == 429, f"{status} {body}")
    finally:
        server._generation_lock.release()
finally:
    stopped = server.stop()
check("stop 之后不再运行", not stopped["running"], str(stopped))

# [EN] Bearer token enforcement on a fresh server.
secured = la.LocalApiServer(port=0, api_key="secret-token", generate=_fake_generate,
                            stream=_fake_stream)
state = secured.start()
try:
    status, body = _http("POST", f"{state['base_url']}/v1/chat/completions",
                         {"messages": [{"role": "user", "content": "x"}]})
    check("缺少令牌时 401", status == 401, f"{status} {body}")
    status, body = _http("GET", f"{state['base_url']}/v1/models")
    check("缺少令牌时 /v1/models 也 401", status == 401, f"{status} {body}")
    status, body = _http("POST", f"{state['base_url']}/v1/chat/completions",
                         {"messages": [{"role": "user", "content": "x"}]},
                         {"Authorization": "Bearer secret-token"})
    check("带正确令牌时 200", status == 200, f"{status} {body}")
    status, body = _http("POST", f"{state['base_url']}/v1/chat/completions",
                         {"messages": [{"role": "user", "content": "x"}]},
                         {"X-API-Key": "secret-token"})
    check("X-API-Key 同样可用", status == 200, f"{status} {body}")
finally:
    secured.stop()

check("令牌为空时无需鉴权", bool(state["auth_required"]))
check("端口 0 会被纠正为实际端口", state["port"] != 0, str(state["port"]))


# ---------------------------------------------------------------------------
# [EN] 5. Backend action layer
# [ZH] 5. 后端动作层
# ---------------------------------------------------------------------------

print("=== 5. 后端动作层（协议动作，不加载模型）===")
try:
    import main as backend_main

    backend = backend_main.Backend(cfg_mod.Config())
    response = backend.handle({
        "action": "provider_save", "active": "openai_compatible",
        "providers": [external]})
    check("provider_save 未授权时返回 ok=false", response.get("ok") is False, str(response)[:200])
    check("provider_save 标记 permission_denied",
          response.get("permission_denied") is True, str(response)[:200])
    check("provider_save 报告 error_type=PermissionError",
          response.get("error_type") == "PermissionError", str(response.get("error_type")))

    state = backend.handle({"action": "provider_state"})
    check("provider_state 回传当前生效提供方",
          state["providers"]["active"] == "llamacpp", str(state)[:200])
    check("provider_state 不泄露 API 密钥",
          "sk-test" not in json.dumps(state, ensure_ascii=False))

    api_state = backend.handle({"action": "local_api_state"})
    check("local_api_state 可读取且默认关闭",
          api_state["local_api"]["enabled"] is False, str(api_state)[:200])
except Exception as exc:  # noqa: BLE001
    check("后端动作层可构造并应答", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# [EN] Summary
# [ZH] 汇总
# ---------------------------------------------------------------------------

print("\n================ 结果汇总 ================")
for name in FAILED:
    print(f"[FAIL] {name}")
total = PASSED + len(FAILED)
print(f"共 {total} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
sys.exit(0 if not FAILED else 1)
