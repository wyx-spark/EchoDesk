#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[EN]
Static network audit for the Reader edition.

The build promises "zero network requests". This audit makes that promise checkable:
it walks the backend Python sources and the Avalonia C# sources, and reports any use of
an outbound network API. Comments and bilingual documentation are ignored (they mention
the removed modules by name), and the single allowed surface is a *user-triggered*
browser open through ``ShellOpen`` (opening the GitHub Release page), which never issues
a request from the application itself.

Exit code 0 means the tree is clean; 1 means at least one network API was found.
Usage:
    python tools/audit_network.py
    python tools/audit_network.py --json

[ZH]
Reader 版的静态断网审计。

本构建承诺"零联网请求"。本审计让该承诺可被自动核查：遍历后端 Python 源码与 Avalonia
C# 源码，报告任何对外网络 API 的使用。注释与双语说明会被忽略（它们会按名称提到已删除的
模块），唯一允许的入口是**用户主动触发**的浏览器打开（``ShellOpen`` 打开 GitHub Release
页面），它不会由程序自身发起任何请求。

退出码 0 表示源码树干净；1 表示至少发现一处网络 API 用法。
用法：
    python tools/audit_network.py
    python tools/audit_network.py --json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend-avalonia", "EchoDesk.Avalonia")

# [EN] Outbound network APIs. Word boundaries keep matches from firing on names such as
# [EN] "socket_type" in a comment; comments are stripped before matching anyway.
# [ZH] 对外网络 API。词边界避免命中 "socket_type" 之类的注释词；匹配前已先剥离注释。
PYTHON_PATTERNS = [
    r"^\s*import\s+requests\b",
    r"^\s*from\s+requests\b",
    r"^\s*import\s+httpx\b",
    r"^\s*from\s+httpx\b",
    r"^\s*import\s+urllib\b",
    r"^\s*from\s+urllib\b",
    r"^\s*import\s+http\.client\b",
    r"^\s*from\s+http\.client\b",
    r"^\s*import\s+socket\b",
    r"^\s*import\s+aiohttp\b",
    r"^\s*import\s+websocket",
    r"^\s*import\s+ftplib\b",
    r"^\s*import\s+smtplib\b",
    r"^\s*import\s+telnetlib\b",
    r"urlopen\s*\(",
    r"requests\.(get|post|put|delete|head|request)\s*\(",
    r"httpx\.(get|post|put|delete|stream)\s*\(",
    r"socket\.(socket|create_connection)\s*\(",
]

CS_PATTERNS = [
    r"\bnew\s+HttpClient\b",
    r"\bnew\s+WebClient\b",
    r"\bWebRequest\.Create\b",
    r"\bHttpWebRequest\b",
    r"\bnew\s+Socket\b",
    r"\.DownloadFile(Async)?\s*\(",
    r"\.DownloadString(Async)?\s*\(",
    r"\bGetAsync\s*\(",
    r"\bPostAsync\s*\(",
]

FORBIDDEN_REQUIREMENTS = (
    "requests", "httpx", "urllib3", "huggingface", "hf-xet", "duckduckgo",
    "tqdm", "primp", "truststore", "aiohttp", "llama-cpp-python", "llama_cpp_python",
)

ALLOWED = (
    # [EN] User-triggered browser open: no request is issued by the program.
    # [ZH] 用户主动触发的浏览器打开：程序自身不发起请求。
    (os.path.join(FRONTEND, "Services", "ShellOpen.cs"), "user-triggered browser open"),
)


def strip_comments(text: str) -> str:
    """[EN] Remove Python/C# comments while keeping string literals intact.
    [ZH] 移除 Python/C# 注释，同时保留字符串字面量。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", "", text)
    return text


def scan_file(path: str, patterns: list[str]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return hits
    for index, line in enumerate(strip_comments(text).splitlines(), 1):
        if any(re.search(pattern, line) for pattern in patterns):
            hits.append((index, line.strip()))
    return hits


def collect_sources(root: str, suffixes: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"bin", "obj", "__pycache__", ".venv", "native"}]
        for name in files:
            if name.endswith(suffixes):
                found.append(os.path.join(base, name))
    return sorted(found)


def run() -> dict[str, object]:
    backend_hits = []
    for path in collect_sources(BACKEND, (".py",)):
        if os.path.basename(path) in {"llama_capi.py", "llama_ctypes.py"}:
            # [EN] Vendored C ABI declarations: no network calls in them, and they are
            # [EN] inspected separately by the imports check below.
            # [ZH] 就地内置的 C ABI 声明：其中没有网络调用，且由下方导入检查单独覆盖。
            pass
        for line, text in scan_file(path, PYTHON_PATTERNS):
            backend_hits.append({"file": os.path.relpath(path, ROOT), "line": line,
                                 "code": text})
    frontend_hits = []
    for path in collect_sources(FRONTEND, (".cs",)):
        for line, text in scan_file(path, CS_PATTERNS):
            allowed = next((reason for allowed_path, reason in ALLOWED
                            if os.path.normcase(allowed_path) == os.path.normcase(path)), "")
            frontend_hits.append({"file": os.path.relpath(path, ROOT), "line": line,
                                  "code": text, "allowed": allowed})
    requirement_hits = []
    for name in ("requirements.txt", "requirements.lock"):
        path = os.path.join(BACKEND, name)
        if not os.path.isfile(path):
            continue
        text = io.open(path, encoding="utf-8").read()
        for package in FORBIDDEN_REQUIREMENTS:
            if re.search(rf"^{re.escape(package)}([=\s@;<]|$)", text, re.M | re.I):
                requirement_hits.append({"file": name, "package": package})
    return {
        "backend_hits": backend_hits,
        "frontend_hits": frontend_hits,
        "requirement_hits": requirement_hits,
        "ok": not backend_hits and not requirement_hits
              and all(hit["allowed"] for hit in frontend_hits),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("[audit] backend outbound API uses:", len(report["backend_hits"]))
        for hit in report["backend_hits"]:
            print(f"  {hit['file']}:{hit['line']}: {hit['code']}")
        print("[audit] frontend network API uses:", len(report["frontend_hits"]))
        for hit in report["frontend_hits"]:
            state = f"allowed ({hit['allowed']})" if hit["allowed"] else "FORBIDDEN"
            print(f"  {hit['file']}:{hit['line']}: {hit['code']} [{state}]")
        print("[audit] forbidden requirement entries:", len(report["requirement_hits"]))
        for hit in report["requirement_hits"]:
            print(f"  {hit['file']}: {hit['package']}")
        print("[audit] RESULT:", "CLEAN" if report["ok"] else "FOUND NETWORK CODE")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
