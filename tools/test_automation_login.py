# -*- coding: utf-8 -*-
"""自动化优化（v0.2.1 任务书《自动化优化》）实机测试。

覆盖任务书「测试要求（DSH 在实机执行）」的五项，全部**真实跑浏览器**：

1. 登录态复用 —— 首次运行在真实浏览器里登录，关闭浏览器后重新运行，确认无需再次登录；
2. 未登录检测 —— 清除登录态后运行，确认程序提示手动登录而不是直接失败；
3. href 提取 —— 含相对路径 / 绝对路径 / JavaScript 伪链接 / `#` / 空 href 的页面，
   确认五种情况都按任务书的四步流程处理（含伪链接回退到点击）；
4. 非链接元素 —— 可视化选取一个 `<button>` / 容器 `<div>`，确认给出明确提示且不写坏配置；
5. 预览确认 —— 可视化选取任务链接后，确认预览里列出了即将提取的网址。

站点用本机回环 HTTP（127.0.0.1 随机端口）而不是 file://：Cookie 只在 http(s) 源上
才有意义，**登录态复用必须用真实源才能验证**。站点行为模仿真实系统：没有会话 Cookie
访问任务列表会被 302 到 /login。

关于"手动登录"：任务书要求"第一次运行时手动登录"。本机没有第二个人可以操作，
因此那**一次点击登录按钮的动作由脚本代劳**（真的在浏览器里导航到 /do-login），
其余环节（浏览器启动、Cookie 落盘、关闭、重启、复用）全部是真实链路。
这一点在研发记录里如实标注。

运行：python tools/test_automation_login.py
需要本机装有 Edge（msedge 通道）与 playwright。
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import traceback
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from automation import AutomationEngine, _classify_href, _t  # noqa: E402
from config import AutomationConfig  # noqa: E402

CHECKS: list = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print(f"{'✅' if condition else '❌'} {name}" + ("" if condition else f"  ← {detail!r}"))


class FakeLLM:
    def chat_complete(self, messages, temperature=0.3, max_tokens=512):
        return "审核意见：占位。"


# ---------------------------------------------------------------- 测试站点

INDEX = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>任务列表</title></head><body>
<h1>待处理任务</h1>
<ul id="tasks">
  <li><a class="task task-rel" href="detail.html?id=1">合同A-相对路径</a></li>
  <li><a class="task task-abs" href="__ABS__">合同B-绝对路径</a></li>
  <li><a class="task task-js" href="javascript:void(0)" onclick="location.href='detail.html?id=3'">合同C-伪链接</a></li>
  <li><a class="task task-hash" href="#" onclick="location.href='detail.html?id=4'">合同D-页内锚点</a></li>
  <li><a class="task task-empty" onclick="location.href='detail.html?id=5'">合同E-空href</a></li>
</ul>
<button id="not-a-link" type="button">这不是链接</button>
<div id="wrapper" style="display:block;padding:260px 20px 20px 20px">
  <a class="inside" href="detail.html?id=9">容器里的链接</a>
</div>
</body></html>"""

DETAIL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>详情</title></head><body>
<h1>任务详情</h1>
<a class="file" href="files/正文.txt">正文.txt</a>
</body></html>"""

LOGIN_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>登录</title></head><body>
<h1>请登录</h1>
<form method="get" action="/do-login">
  <input id="user" name="user" value="tester">
  <button id="login-submit" type="submit">登录</button>
</form>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    """极小的"需要登录"站点。

    * ``/login``：登录页（URL 含 ``login``，用于 URL 关键词兜底判定）；
    * ``/do-login``：下发 ``echodesk_test_session`` Cookie（真实站点的登录动作）；
    * ``/``：任务列表；**没有 Cookie 就 302 到 /login**；
    * 其余路径按静态文件返回。
    """

    root = ""
    server_version = "EchoDeskTest/1.0"

    def log_message(self, *args):
        pass

    def _send(self, body: str, status: int = 200, cookie: str = ""):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urllib.parse.urlparse(self.path).path
        signed_in = "echodesk_test_session=1" in (self.headers.get("Cookie", "") or "")

        if path == "/login":
            return self._send(LOGIN_PAGE)
        if path == "/do-login":
            return self._send("<html><body>ok</body></html>",
                              cookie="echodesk_test_session=1; Path=/; Max-Age=86400")
        if path in ("/", "/index.html"):
            if not signed_in:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            origin = f"http://127.0.0.1:{self.server.server_address[1]}"
            return self._send(INDEX.replace("__ABS__", f"{origin}/detail.html?id=2"))
        relative = path.lstrip("/") or "index.html"
        # 详情页是动态生成的（要带任务 id 参数），不落盘
        if relative in ("detail.html",):
            return self._send(DETAIL)
        target = os.path.join(self.root, *relative.split("/"))
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as fh:
                return self._send(fh.read())
        return self._send("<html><body>404</body></html>", status=404)


def start_server(root: str):
    handler = type("BoundHandler", (Handler,), {"root": root})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def build_files(root: str):
    files = os.path.join(root, "files")
    os.makedirs(files, exist_ok=True)
    with open(os.path.join(files, "正文.txt"), "w", encoding="utf-8") as fh:
        fh.write("甲方：某某公司\n合同金额：12,345 元\n付款期限：30 天\n")


def make_cfg(tmp: str, base: str, **over) -> AutomationConfig:
    cfg = AutomationConfig(
        enabled=True, safety_accepted=True, safety_version="1.0",
        task_list_url=base + "/",
        task_list_selector="a.task",
        task_detail_file_selector="a.file",
        file_extensions=["txt"],
        instruction="摘取合同主要信息",
        max_tasks=5, dedupe=False, save_results=True,
        results_location=os.path.join(tmp, "results"),
        download_dir=os.path.join(tmp, "dl"),
        page_timeout_ms=15000, retries=0, processed_file="",
        # 全部落在临时目录里：绝不碰仓库根目录的 browser_state.json
        login_state_dir=os.path.join(tmp, "profile"),
        login_state_file=os.path.join(tmp, "browser_state.json"),
        audit_log_file=os.path.join(tmp, "audit.jsonl"),
        manual_login_wait_seconds=8,
        login_check_selector="",
    )
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def texts(events) -> str:
    return "\n".join(str(e.get("content") or "") for e in events)


# ---------------------------------------------------------------- 测试 3a

def test_href_classification(base: str):
    """第 3 项（规则部分）：五类 href 的判定与规范化。"""
    got = {
        "relative": _classify_href("detail.html?id=1", base + "/"),
        "absolute": _classify_href(base + "/detail.html?id=2", base + "/"),
        "javascript": _classify_href("javascript:void(0)", base + "/"),
        "fragment": _classify_href("#", base + "/"),
        "fragment2": _classify_href("#section", base + "/"),
        "empty": _classify_href("", base + "/"),
        "other": _classify_href("mailto:a@b.c", base + "/"),
    }
    check("相对路径被补全为绝对 URL（urljoin 到当前页 origin）",
          got["relative"].url == base + "/detail.html?id=1"
          and got["relative"].kind == "relative", repr(got["relative"]))
    check("绝对路径原样保留", got["absolute"].url == base + "/detail.html?id=2"
          and got["absolute"].kind == "absolute", repr(got["absolute"]))
    check("javascript: 伪链接不可导航（click_only，回退点击）",
          got["javascript"].url == "" and got["javascript"].click_only
          and got["javascript"].kind == "javascript", repr(got["javascript"]))
    check("裸 # 判定为片段链接（click_only）",
          got["fragment"].url == "" and got["fragment"].click_only
          and got["fragment"].kind == "fragment", repr(got["fragment"]))
    check("#section 同样判定为片段链接（对任务书的扩展，已记入记录）",
          got["fragment2"].url == "" and got["fragment2"].click_only, repr(got["fragment2"]))
    check("空 href 判定为空（click_only）",
          got["empty"].url == "" and got["empty"].click_only
          and got["empty"].kind == "empty", repr(got["empty"]))
    check("mailto: 之类不可直接导航的协议归为 other",
          got["other"].url == "" and got["other"].kind == "other", repr(got["other"]))
    check("原始属性值被保留（供日志与用户提示使用）",
          got["javascript"].raw == "javascript:void(0)", repr(got["javascript"].raw))


# ---------------------------------------------------------------- 测试 3b

def test_href_in_browser(tmp: str, base: str):
    """第 3 项（浏览器部分）：真实页面上采集五类链接，并确认伪链接真的回退到点击。"""
    # 专用数据目录：本测试会真的登录一次，不能污染其它用例的登录态
    cfg = make_cfg(tmp, base, login_state_dir=os.path.join(tmp, "profile_href"),
                   audit_log_file=os.path.join(tmp, "audit_href.jsonl"))
    engine = AutomationEngine(cfg, llm_engine=FakeLLM())
    session = None
    try:
        session = engine._open_session()
        page = session.new_page()
        engine._goto(page, base + "/do-login")
        engine._goto(page, base + "/")
        engine._wait_loaded(page)
        entries = engine._collect_links(page, "a.task")
        wanted = {
            "task-rel": "合同A-相对路径",
            "task-abs": "合同B-绝对路径",
            "task-js": "合同C-伪链接",
            "task-hash": "合同D-页内锚点",
            "task-empty": "合同E-空href",
        }
        found = {}
        for cls, text in wanted.items():
            found[cls] = next((e for e in entries if e.text == text), None)
        check("页面上 5 个任务链接全部被采集到（伪链接不再被按 href 去重压掉）",
              len(entries) == 5, f"采集到 {len(entries)} 个：{[e.text for e in entries]}")
        check("相对路径任务：href 被补成绝对 URL",
              found["task-rel"] is not None
              and found["task-rel"].href == base + "/detail.html?id=1",
              repr(getattr(found["task-rel"], "href", None)))
        check("绝对路径任务：href 原样保留",
              found["task-abs"] is not None
              and found["task-abs"].href == base + "/detail.html?id=2",
              repr(getattr(found["task-abs"], "href", None)))
        check("javascript: 任务：click_only 且无 URL",
              found["task-js"] is not None and found["task-js"].click_only
              and found["task-js"].href == ""
              and found["task-js"].href_kind == "javascript",
              repr(getattr(found["task-js"], "href_kind", None)))
        check("裸 # 任务：click_only 且无 URL",
              found["task-hash"] is not None and found["task-hash"].click_only
              and found["task-hash"].href == ""
              and found["task-hash"].href_kind == "fragment",
              repr(getattr(found["task-hash"], "href_kind", None)))
        check("空 href 任务：click_only 且无 URL",
              found["task-empty"] is not None and found["task-empty"].click_only
              and found["task-empty"].href == ""
              and found["task-empty"].href_kind == "empty",
              repr(getattr(found["task-empty"], "href_kind", None)))

        target = found["task-js"]
        pages_before = session.pages
        gen = engine._click_into(session.context, page, target, pages_before)
        opened = None
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            opened = stop.value
        check("javascript: 伪链接点击后成功跳转到详情页（回退生效）",
              opened is not None and "detail.html?id=3" in (opened.url or ""),
              repr(getattr(opened, "url", None)))
    except Exception:
        check("浏览器内的 href 测试未抛异常", False, traceback.format_exc(limit=3))
    finally:
        if session is not None:
            session.close()


# ---------------------------------------------------------------- 测试 1

def test_login_reuse(tmp: str, base: str):
    """第 1 项：首次运行登录一次，重开程序后无需再次登录。"""
    # 专用数据目录 + 专用 storage_state：第一次运行必须是"从未登录过"的状态，
    # 否则测不出"首次运行引导登录"。
    profile = os.path.join(tmp, "profile_login")
    cfg = make_cfg(tmp, base, login_state_dir=profile, login_check_selector="#tasks",
                   login_state_file=os.path.join(tmp, "state_login.json"),
                   audit_log_file=os.path.join(tmp, "audit_login.jsonl"),
                   results_location=os.path.join(tmp, "results_login"))
    engine = AutomationEngine(cfg, llm_engine=FakeLLM())

    state = {"present_calls": 0, "signed_in": False}
    original = engine._login_present

    def hook(page):
        """第 1 次判定：如实返回"未登录"（触发程序提示与等待）。

        第 2 次判定（等待循环里）：先在真实浏览器里完成登录，再如实判定。
        登录动作用"导航到 /do-login"代替人工点按钮——见模块 docstring 的说明。
        """
        state["present_calls"] += 1
        if state["present_calls"] >= 2 and not state["signed_in"]:
            try:
                page.goto(base + "/do-login", wait_until="domcontentloaded", timeout=10000)
                page.goto(base + "/", wait_until="domcontentloaded", timeout=10000)
                state["signed_in"] = True
            except Exception:  # noqa: BLE001
                pass
        return original(page)

    engine._login_present = hook            # type: ignore[assignment]
    events = list(engine.run())
    body = texts(events)
    check("首次运行检测到未登录并提示手动登录",
          "手动登录" in body, body[:900])
    check("登录成功后程序自动继续（无需重启）",
          _t("automation.login.done") in body, body[-600:])
    check("首次运行后登录态数据目录已建立且非空（Cookie 真的落盘）",
          os.path.isdir(profile) and any(
              os.path.getsize(os.path.join(r, f)) > 0
              for r, _d, fs in os.walk(profile) for f in fs),
          profile)

    # ---- 第二次运行：全新引擎、全新浏览器进程，应当直接复用登录态 ----
    engine2 = AutomationEngine(cfg, llm_engine=FakeLLM())
    seen = {"calls": 0}
    original2 = engine2._login_present

    def hook2(page):
        seen["calls"] += 1
        return original2(page)

    engine2._login_present = hook2           # type: ignore[assignment]
    events2 = list(engine2.run())
    body2 = texts(events2)
    check("第二次运行完全没有要求再次手动登录",
          "手动登录" not in body2 and "未登录" not in body2, body2[:700])
    check("第二次运行直接判定为已登录（没有走「登录成功」分支）",
          _t("automation.login.done") not in body2 and seen["calls"] >= 1, body2[:700])
    check("第二次运行顺利处理了任务（未因登录失败中止）",
          any(e.get("type") == "done" for e in events2), body2[-500:])
    check("第二次运行至少成功处理 1 个任务",
          "成功 0 个" not in body2.split("\n")[0], body2[-300:])

    audit = ""
    if os.path.isfile(cfg.audit_log_file):
        with open(cfg.audit_log_file, "r", encoding="utf-8") as fh:
            audit = fh.read()
    check("审计日志记录了登录态使用事件（login_state_used）",
          '"event": "login_state_used"' in audit, audit[-300:])
    check("审计日志不含任何 Cookie 值（脱敏）",
          "echodesk_test_session" not in audit, audit[-300:])


# ---------------------------------------------------------------- 测试 2

def test_login_required_detection(tmp: str, base: str):
    """第 2 项：清除登录态后运行，确认提示手动登录而不是直接失败。"""
    profile = os.path.join(tmp, "profile2")
    cfg = make_cfg(tmp, base, login_state_dir=profile, login_check_selector="#tasks",
                   login_state_file=os.path.join(tmp, "state_clear.json"),
                   audit_log_file=os.path.join(tmp, "audit_clear.jsonl"),
                   results_location=os.path.join(tmp, "results_clear"),
                   manual_login_wait_seconds=6)

    setup = AutomationEngine(cfg, llm_engine=FakeLLM())
    session = setup._open_session()
    try:
        page = session.new_page()
        page.goto(base + "/do-login", wait_until="domcontentloaded")
        page.goto(base + "/", wait_until="domcontentloaded")
    finally:
        session.close()
    check("清除前登录态数据目录存在", os.path.isdir(profile), profile)

    engine = AutomationEngine(cfg, llm_engine=FakeLLM())
    cleared = engine.clear_login_state()
    check("清除登录态返回成功", cleared.get("ok") is True, repr(cleared))
    check("清除后数据目录已被删除", not os.path.isdir(profile), profile)
    check("清除登录态记录了删除的文件数", int(cleared.get("removed_files") or 0) > 0,
          repr(cleared.get("removed_files")))

    events = list(engine.run())
    body = texts(events)
    check("清除登录态后运行：程序提示需要手动登录（而不是直接失败）",
          "手动登录" in body, body[:900])
    check("等待登录超时后给出明确说明",
          "等待登录超过" in body, body[-800:])
    check("未登录时以「本轮不再继续」收尾（避免每个任务各失败一遍）",
          "未检测到登录状态" in body and any(e.get("type") == "error" for e in events),
          body[-700:])
    check("未登录时任务计数为 0（没有盲目往下跑）",
          "共找到" not in body, body[-500:])

    audit = ""
    if os.path.isfile(cfg.audit_log_file):
        with open(cfg.audit_log_file, "r", encoding="utf-8") as fh:
            audit = fh.read()
    check("审计日志记录了清除登录态事件（login_state_cleared）",
          '"event": "login_state_cleared"' in audit, audit[-300:])
    check("审计日志记录了未登录等待事件（login_required / login_timeout）",
          '"event": "login_required"' in audit and '"event": "login_timeout"' in audit,
          audit[-300:])


# ---------------------------------------------------------------- 测试 4 / 5

class _AutoPickPage:
    """包装真实页面：拾取脚本一装好就用**真实鼠标点击**点一下目标元素。

    这样 ``pick_selector`` 的整条链路（真实浏览器 → 注入拾取脚本 → 真实点击 →
    后端预览与拦截 → 事件）都能自动跑完；生产代码里不留任何测试钩子。
    """

    def __init__(self, page, click_selector: str):
        self._page = page
        self._click_selector = click_selector

    def evaluate(self, expression, *args):
        result = self._page.evaluate(expression, *args)
        if isinstance(expression, str) and "__echodeskPickerInstalled" in expression:
            try:
                self._page.click(self._click_selector, timeout=8000)
            except Exception:  # noqa: BLE001
                pass
        return result

    def __getattr__(self, name):
        return getattr(self._page, name)


def pick_with_click(tmp: str, base: str, target: str, click_selector: str, tag: str):
    """真实跑一次 ``pick_selector``，返回 (事件列表, 带预览的那个事件)。

    选取被拦下时（选到非链接元素）程序发的是 ``error`` 事件而不是 ``done``，
    但两者都带 ``preview``；这里统一取"带预览的那个事件"，调用方按 ``type`` 区分。
    """
    cfg = make_cfg(tmp, base, login_state_dir=os.path.join(tmp, "pk_" + tag),
                   audit_log_file=os.path.join(tmp, f"audit_{tag}.jsonl"))
    engine = AutomationEngine(cfg, llm_engine=FakeLLM())
    session = engine._open_session()
    page = session.new_page()
    page.set_default_timeout(engine.page_timeout_ms)
    engine._goto(page, base + "/do-login")
    engine._goto(page, base + "/")
    engine._wait_loaded(page)
    auto = _AutoPickPage(page, click_selector)
    session.new_page = lambda: auto        # 实例属性覆盖方法：仅在测试里这么做

    original_open = engine._open_session
    engine._open_session = lambda: session  # type: ignore[assignment]
    events = []
    try:
        events = list(engine.pick_selector(base + "/", target, timeout_seconds=20))
    except Exception:  # noqa: BLE001
        check(f"{target}/{tag} 选取未抛异常", False, traceback.format_exc(limit=3))
    finally:
        engine._open_session = original_open  # type: ignore[assignment]
        session.close()
    carriers = [e for e in events if isinstance(e.get("preview"), dict) and e.get("preview")]
    return events, (carriers[-1] if carriers else {})


def test_non_link(tmp: str, base: str):
    """第 4 项：可视化选取非链接元素时给出明确提示，且不写坏配置。"""
    events, result = pick_with_click(tmp, base, "task_list", "#not-a-link", "btn")
    body = texts(events)
    preview = result.get("preview") or {}
    check("选中按钮时给出「不是链接 / 无法提取网址」的明确提示",
          "不是链接" in body and "无法提取网址" in body, body[-500:])
    check("非链接元素不返回选择器（配置不会被写坏）",
          result.get("type") == "error" and not result.get("selector"),
          repr((result.get("type"), result.get("selector"))))
    check("提示里说明了实际选中的标签名（button）",
          "button" in str(preview.get("blocking") or preview.get("message") or ""),
          repr(preview.get("blocking")))
    check("提示语明确要求「请重新选择」",
          "请重新选择" in str(preview.get("blocking") or ""), repr(preview.get("blocking")))
    check("被拒绝的选取在审计日志里留痕（pick_rejected）",
          os.path.isfile(os.path.join(tmp, "audit_btn.jsonl"))
          and "pick_rejected" in open(os.path.join(tmp, "audit_btn.jsonl"),
                                      encoding="utf-8").read(),
          "audit_btn.jsonl")

    events2, result2 = pick_with_click(tmp, base, "task_list", "#wrapper", "wrap")
    body2 = texts(events2)
    preview2 = result2.get("preview") or {}
    check("选中「容器」时提示指出它本身不是链接、里面有 1 个链接",
          "容器" in body2 and "1 个链接" in body2, body2[-500:])
    check("容器同样不写入配置（不返回选择器）",
          not result2.get("selector"), repr(result2.get("selector")))

    events3, result3 = pick_with_click(tmp, base, "task_row", "#wrapper", "row")
    body3 = texts(events3)
    check("「任务行容器」位置选中容器是合法的（不做链接要求）",
          bool(result3.get("selector")), body3[-400:])


def test_preview(tmp: str, base: str):
    """第 5 项：可视化选取后确认预览显示了提取到的网址。"""
    events, result = pick_with_click(tmp, base, "task_list", "a.task-rel", "rel")
    preview = result.get("preview") or {}
    message = str(preview.get("message") or "")
    body = texts(events)
    check("选取任务链接后返回了选择器", bool(result.get("selector")), repr(result.get("selector")))
    check("预览文案包含「程序将从这些元素提取以下网址」",
          "程序将从这些元素提取以下网址" in message, repr(message))
    check("预览列出了提取到的具体网址（含补全后的绝对地址）",
          any("detail.html?id=1" in str(u) for u in (preview.get("hrefs") or [])),
          repr(preview.get("hrefs")))
    check("预览统计了匹配元素数与链接数",
          int(preview.get("matches") or 0) >= 1 and int(preview.get("links") or 0) >= 1,
          f"matches={preview.get('matches')} links={preview.get('links')}")
    check("预览带上了元素示例文本", bool(preview.get("samples")), repr(preview.get("samples")))
    check("选取成功的进度事件本身也带预览文字",
          "提取以下网址" in body, body[-400:])
    check("预览确认所需的数据随 done 事件一并回传（type=done 且 selector/preview 都在）",
          result.get("type") == "done" and bool(result.get("selector")) and bool(preview),
          repr((result.get("type"), list(result.keys()))))

    # 伪链接：预览应说明"改为点击触发"，且没有可用网址
    events2, result2 = pick_with_click(tmp, base, "task_list", "a.task-js", "js")
    preview2 = result2.get("preview") or {}
    message2 = str(preview2.get("message") or "")
    check("选中 JavaScript 伪链接时，预览说明「改为点击元素触发」",
          "点击元素触发" in message2, repr(message2))
    check("伪链接不在预览里列出网址（因为它没有网址）",
          not [u for u in (preview2.get("hrefs") or []) if u], repr(preview2.get("hrefs")))
    check("预览标记了伪链接的种类（JavaScript 伪链接）",
          "JavaScript" in message2, repr(message2))


def main():
    tmp = tempfile.mkdtemp(prefix="echodesk_auto_login_")
    server = None
    try:
        build_files(tmp)
        server, base = start_server(tmp)
        print(f"测试站点：{base}\n")

        print("=== 1. href 提取：五类值的判定与规范化 ===")
        test_href_classification(base)
        print("\n=== 2. href 提取：真实页面采集 + 伪链接点击回退 ===")
        test_href_in_browser(tmp, base)
        print("\n=== 3. 登录态复用：两次运行（首次登录 → 关闭 → 复用） ===")
        test_login_reuse(tmp, base)
        print("\n=== 4. 未登录检测 + 清除登录态 ===")
        test_login_required_detection(tmp, base)
        print("\n=== 5. 非链接元素识别 ===")
        test_non_link(tmp, base)
        print("\n=== 6. 选取后预览确认 ===")
        test_preview(tmp, base)
    except Exception:
        check("测试整体未抛异常", False, traceback.format_exc(limit=4))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    for name in failed:
        print(f"  ❌ {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
