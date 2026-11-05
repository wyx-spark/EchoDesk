# -*- coding: utf-8 -*-
"""任务十七「复用已打开的浏览器 + 两段式可视化选取」的**无浏览器**测试。

为什么必须有一套"不启动浏览器"的测试
------------------------------------
任务十七里最容易出错、也最不能靠人工看一眼确认的，是这几条**规则**：

* 调试端口只能连本机回环地址（连错了等于把用户的登录会话交出去）；
* Firefox 根本不能走 CDP，程序**不该去试**（试了只是白等然后报一个看不懂的错）；
* 连不上已打开的浏览器时，要么按设置报错、要么回退新实例**并明说需要重新登录**；
* 只能操作"用户当前活动的那一个标签页"，页面被关掉就报错，**绝不换一页继续**；
* 复用模式下**绝不关闭用户的浏览器**；
* 两段式状态机的迁移（等待准备 → 选取中 → 完成/取消 → 回到等待准备，且能重选）。

这些规则用真浏览器反而不好测：真浏览器在没装 Playwright 浏览器的机器上起不来，
真点一次也无法断言"它没有去碰别的标签页"。所以这里用**桩对象**：假的 page / context /
Playwright，把"浏览器"替换成可断言的记录器。真实链路另由
``tools/test_automation_e2e.py``、``tools/test_automation_login.py`` 覆盖。

运行：python tools/test_browser_reuse.py（不需要浏览器、不联网）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

#: 所有会产生文件的路径都指到这里（见 make_engine 的说明），跑完删除。
SCRATCH = tempfile.mkdtemp(prefix="echodesk_reuse_test_")

import automation  # noqa: E402
from automation import (  # noqa: E402
    AutomationEngine, AutomationError, _PickMachine, _cdp_endpoint, _drive, _same_site,
    cdp_supported, choose_target_index, debug_browser_executable,
    PICK_STATE_IDLE, PICK_STATE_PICKING, PICK_STATE_WAITING,
)
from config import AutomationConfig  # noqa: E402

#: 模块级 Playwright 入口的**真实实现**（可能是 None：本机没装 playwright）。
#: 桩测试会把它换成假对象，实机段开始前必须还原——否则实机测试拿着"假浏览器"跑，
#: 报出来的错看起来全像产品缺陷（本轮实测踩到过：实机段复用了桩里那页
#: https://oa.example.com/tasks，还报 FakePage 没有 click 方法）。
REAL_SYNC_PLAYWRIGHT = automation._sync_playwright

CHECKS: list = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print(f"{'✅' if condition else '❌'} {name}" + ("" if condition else f"  ← {detail!r}"))


# ----------------------------------------------------------------- 桩对象


class FakeHandle:
    """假的元素句柄：``click()`` 会把所属页面的地址改成 ``goto_url``（模拟跳转）。

    ``alive`` 控制 ``evaluate("el => el.isConnected")`` 的返回值 —— 这是"句柄还活着吗"
    的判据，也是"点不动就退回按地址导航"那条分支的开关。
    """

    def __init__(self, page, goto_url="", alive=True):
        self.page = page
        self.goto_url = goto_url
        self.alive = alive
        self.clicks = 0
        self.expect_download_calls = 0

    def click(self, timeout=None, force=False):
        self.clicks += 1
        if self.goto_url:
            self.page.url = self.goto_url

    def evaluate(self, expression, *args):
        if not self.alive:
            raise RuntimeError("Element is not attached to the DOM")
        return True


class FakePage:
    """假的标签页：记录每一次"可能影响用户"的动作，供断言使用。"""

    def __init__(self, url="", visible=True, focused=False, title="", closed=False):
        self.url = url
        self.visible = visible
        self.focused = focused
        self.title = title
        self._closed = closed
        self.evaluated: list = []
        self.goto_calls: list = []
        self.front_calls = 0
        self.close_calls = 0
        self.go_back_calls = 0
        self.clicked_handles: list = []
        # 页面里装了什么（PICKER_JS 注入 / 停止 / 结果）——用表达式里的关键字区分
        self.picker_installed = False
        self.picker_stopped = False
        self.picker_result = None

    # ---- Playwright Page 的最小接口 ----
    def is_closed(self):
        return self._closed

    def set_default_timeout(self, value):
        self.default_timeout = value

    def set_default_navigation_timeout(self, value):
        self.default_nav_timeout = value

    def bring_to_front(self):
        self.front_calls += 1

    def close(self):
        self.close_calls += 1
        self._closed = True

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, ms):
        return None

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    def go_back(self, **kwargs):
        self.go_back_calls += 1

    def evaluate(self, expression, *args):
        self.evaluated.append(expression)
        if "__echodeskPickerInstalled" in expression:
            self.picker_installed = True
            return "installed"
        if "pickerInstalled = false" in expression:      # PICKER_STOP_JS
            self.picker_stopped = True
            self.picker_installed = False
            return "stopped"
        if "__echodeskPick" in expression or "__echodeskPick" in str(args):
            return self.picker_result
        if "visibilityState" in expression:
            return {"url": self.url, "title": self.title,
                    "visible": self.visible, "focused": self.focused}
        return None


class FakeContext:
    def __init__(self, pages=None, ignores=None):
        self.pages = list(pages or [])
        self.new_page_calls = 0
        self.closed = False
        self._ignores = ignores

    def new_page(self):
        self.new_page_calls += 1
        page = FakePage(url="about:blank")
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True

    def add_cookies(self, cookies):
        return None

    def storage_state(self, path=None):
        return {}


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False

    def close(self):
        self.closed = True

    def new_context(self):
        context = FakeContext()
        self.contexts.append(context)
        return context


class FakeChromium:
    def __init__(self, browser=None, fail=False):
        self.browser = browser
        self.fail = fail
        self.connect_calls: list = []

    def connect_over_cdp(self, endpoint, timeout=None):
        self.connect_calls.append(endpoint)
        if self.fail or self.browser is None:
            raise RuntimeError("connect ECONNREFUSED 127.0.0.1")
        return self.browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class FakeLauncher:
    """假的 ``_sync_playwright``。

    注意它必须**可调用**：生产代码里写的是 ``_sync_playwright().start()``
    —— ``sync_playwright()`` 返回的是"启动器"，它的 ``.start()`` 才返回 Playwright 对象。
    桩若只实现 ``.start()``，那次调用会抛 TypeError，而它恰好落在
    ``_connect_reuse_session`` 的 except 里，于是表现为"连不上"，
    看起来像被测代码有毛病，其实是桩不像。这个坑在本轮实测踩过一次。
    """

    def __init__(self, pw):
        self._pw = pw
        self.starts = 0

    def __call__(self):
        return self

    def start(self):
        self.starts += 1
        return self._pw


def make_engine(**cfg_kwargs) -> AutomationEngine:
    """建一个引擎。

    **所有落地路径都指到临时目录**：``audit_log_file`` 的默认值是
    ``automation_log.jsonl``（相对当前工作目录），不指走的话跑一次测试就会往**仓库根目录**
    的审计日志里追加几行——测试污染仓库文件是不能接受的（本轮实测踩到过）。
    """
    cfg_kwargs.setdefault("audit_log_file", os.path.join(SCRATCH, "audit.jsonl"))
    cfg_kwargs.setdefault("login_state_dir", os.path.join(SCRATCH, "profile"))
    cfg_kwargs.setdefault("processed_file", "")
    cfg_kwargs.setdefault("download_dir", os.path.join(SCRATCH, "dl"))
    cfg = AutomationConfig(**cfg_kwargs)
    return AutomationEngine(cfg)


def make_session(pages=None, reused=True, mode="reuse"):
    """构造一个"已连上用户浏览器"的会话桩（不涉及任何真浏览器）。"""
    context = FakeContext(pages or [])
    session = automation._BrowserSession(
        FakePlaywright(FakeChromium()), context, None, mode=mode, reused=reused,
        endpoint=_cdp_endpoint(9222), owns_browser=False,
    )
    return session, context


# ----------------------------------------------------------------- 1. 端口与回环


def test_loopback():
    print("\n=== 1. 调试端口只连本机回环地址（安全边界）===")
    check("默认端点指向 127.0.0.1", _cdp_endpoint(9222) == "http://127.0.0.1:9222",
          _cdp_endpoint(9222))
    for bad in ("0.0.0.0", "::", "192.0.2.10"):
        made = _cdp_endpoint(bad)
        check(f"端口传入 {bad!r} 时仍只会连 127.0.0.1", made.startswith("http://127.0.0.1:"), made)
        check(f"端口传入 {bad!r} 时端点里不含外部地址", bad not in made, made)
    for bad in (0, -1, 70000, "abc", None):
        check(f"非法端口 {bad!r} 回落到默认 {automation.CDP_DEFAULT_PORT}",
              _cdp_endpoint(bad) == f"http://127.0.0.1:{automation.CDP_DEFAULT_PORT}",
              _cdp_endpoint(bad))

    engine = make_engine(cdp_port=9333)
    check("引擎读到的端口来自配置", engine.cdp_port == 9333, engine.cdp_port)
    check("引擎端点只绑回环", engine.cdp_endpoint == "http://127.0.0.1:9333", engine.cdp_endpoint)
    check("便携/异常配置下端点仍只绑回环",
          make_engine(cdp_port=0).cdp_endpoint == "http://127.0.0.1:9222")

    cmd = make_engine(browser="chrome", cdp_port=9333).debug_browser_command()
    check("代启动命令行显式带上 --remote-debugging-address=127.0.0.1",
          "--remote-debugging-address=127.0.0.1" in cmd["args"], cmd["args"])
    check("代启动命令行带上调试端口", "--remote-debugging-port=9333" in cmd["args"], cmd["args"])
    check("代启动命令行用自动化专用数据目录（_debug 后缀）",
          any(a.startswith("--user-data-dir=") and a.endswith("_debug") for a in cmd["args"]),
          cmd["args"])
    check("代启动数据目录与兜底新实例的数据目录不同",
          cmd["profile_dir"] != make_engine(browser="chrome").profile_dir,
          (cmd["profile_dir"], make_engine(browser="chrome").profile_dir))


# ----------------------------------------------------------------- 2. CDP 决策


def test_cdp_decisions():
    print("\n=== 2. 哪些浏览器该尝试 CDP（Firefox 必须为否）===")
    check("edge 支持 CDP", cdp_supported("edge") is True)
    check("chrome 支持 CDP", cdp_supported("chrome") is True)
    check("chromium 支持 CDP", cdp_supported("chromium") is True)
    check("auto 支持 CDP（候选里 Chromium 系在前）", cdp_supported("auto") is True)
    check("firefox **不**支持 CDP（协议层不支持，不是没实现）", cdp_supported("firefox") is False)
    check("未知取值按 auto 处理（配置被改坏时不至于瘫掉）", cdp_supported("") is True)


def test_firefox_never_tries_cdp():
    """Firefox：不试连接、直接兜底，并且把原因说出来。"""
    engine = make_engine(browser="firefox")
    chromium = FakeChromium(browser=FakeBrowser([FakeContext([FakePage("https://oa/x")])]))
    pw = FakePlaywright(chromium)
    automation._sync_playwright = FakeLauncher(pw)          # 打桩：真 Playwright 不动

    def fake_open_session():
        session, _ctx = make_session([FakePage("about:blank")], reused=False, mode="fresh")
        session.note = ""
        return session

    engine._open_session = fake_open_session                  # type: ignore[assignment]
    session = engine._open_work_session()
    check("Firefox 下没有调用 connect_over_cdp",
          chromium.connect_calls == [], chromium.connect_calls)
    check("Firefox 下走了兜底新实例（模式 = fresh）", session.mode == "fresh", session.mode)
    check("Firefox 的提示里明确说了不支持复用",
          "不支持复用" in session.note, session.note)
    check("Firefox 的提示里也说明了代价（要重新登录）",
          "重新登录" in session.note, session.note)


def test_cdp_connect_failure_falls_back():
    """连不上调试端口：默认（auto）回退新实例，**不抛异常**，且明说需要重新登录。"""
    engine = make_engine()
    chromium = FakeChromium(fail=True)
    automation._sync_playwright = FakeLauncher(FakePlaywright(chromium))

    fallback_calls = {"n": 0}

    def fake_open_session():
        fallback_calls["n"] += 1
        session, _ctx = make_session([FakePage("about:blank")], reused=False, mode="fresh")
        session.note = ""
        return session

    engine._open_session = fake_open_session                  # type: ignore[assignment]
    session = None
    error = ""
    try:
        session = engine._open_work_session()
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    check("连不上 CDP 时不抛异常", error == "", error)
    check("连不上 CDP 时确实尝试过连接",
          chromium.connect_calls == ["http://127.0.0.1:9222"], chromium.connect_calls)
    check("连不上 CDP 时回退到新实例（调用了一次 _open_session）", fallback_calls["n"] == 1,
          fallback_calls)
    check("兜底会话不是复用会话", session is not None and session.reused is False)
    check("兜底提示里告知了代价：需要重新登录",
          session is not None and "重新登录" in session.note, getattr(session, "note", ""))


def test_reuse_mode_required_and_off():
    """``required`` 连不上就报错；``off`` 干脆不试。"""
    engine = make_engine(browser_reuse="required")
    chromium = FakeChromium(fail=True)
    automation._sync_playwright = FakeLauncher(FakePlaywright(chromium))
    engine._open_session = lambda: (_ for _ in ()).throw(      # type: ignore[assignment]
        AssertionError("required 模式不该回退到新实例"))
    raised = ""
    try:
        engine._open_work_session()
    except AutomationError as exc:
        raised = str(exc)
    check("required 模式连不上时抛 AutomationError", raised != "", raised)
    check("required 模式的报错里带了端口号与引导",
          "9222" in raised and "调试" in raised, raised)

    engine_off = make_engine(browser_reuse="off")
    chromium_off = FakeChromium(fail=True)
    automation._sync_playwright = FakeLauncher(FakePlaywright(chromium_off))
    fallback_calls = {"n": 0}

    def fake_open_session():
        fallback_calls["n"] += 1
        session, _ctx = make_session([FakePage("about:blank")], reused=False, mode="fresh")
        session.note = ""
        return session

    engine_off._open_session = fake_open_session               # type: ignore[assignment]
    session_off = engine_off._open_work_session()
    check("off 模式完全不尝试 CDP", chromium_off.connect_calls == [], chromium_off.connect_calls)
    check("off 模式直接走新实例", fallback_calls["n"] == 1 and session_off.mode == "fresh")
    check("off 模式提示里说明了「按设置不复用」",
          "不复用" in session_off.note or "不连接" in session_off.note, session_off.note)


def test_successful_reuse():
    """连得上：拿到用户的上下文，并且**不关闭**用户的浏览器。"""
    active = FakePage("https://oa.example.com/tasks", focused=True)
    other = FakePage("https://news.example.com/", visible=True)
    context = FakeContext([other, active])
    browser = FakeBrowser([context])
    chromium = FakeChromium(browser=browser)
    pw = FakePlaywright(chromium)
    automation._sync_playwright = FakeLauncher(pw)

    engine = make_engine(browser="chrome")
    session = engine._connect_reuse_session()
    check("连上后返回会话", session is not None)
    check("端点只用了回环地址",
          chromium.connect_calls == ["http://127.0.0.1:9222"], chromium.connect_calls)
    check("复用会话标记为 reuse", session.mode == "reuse" and session.reused is True)
    check("复用会话声明「不拥有浏览器」（close 时不会关它）", session.owns_browser is False)

    page = engine._select_target_page(session)
    check("选中的是用户正在看的那一页（有焦点的那页）", page is active, repr(page))
    check("会话里记住了目标页", session.target_page is active)
    check("目标页的地址被记录（供界面确认）",
          session.target_url == "https://oa.example.com/tasks", session.target_url)

    session.close()
    check("关闭复用会话时**没有**关闭浏览器", browser.closed is False)
    check("关闭复用会话时**没有**关闭用户的上下文", context.closed is False)
    check("关闭复用会话时断开了 Playwright（不留连接）", pw.stopped >= 1, pw.stopped)


# ----------------------------------------------------------------- 3. 目标标签页


def test_choose_target_index():
    print("\n=== 3. 目标标签页的选择规则 ===")

    def probe(page):
        return {"url": page.url, "visible": page.visible, "focused": page.focused}

    focused = [FakePage("https://a/1", visible=True), FakePage("https://a/2", visible=True, focused=True),
               FakePage("https://a/3", visible=True)]
    check("有焦点页时选它（真正的活动标签页）",
          choose_target_index(focused, probe) == 1, choose_target_index(focused, probe))

    no_focus = [FakePage("https://a/1", visible=False), FakePage("https://a/2", visible=True),
                FakePage("https://a/3", visible=False)]
    check("没有焦点页时选最后一个可见页",
          choose_target_index(no_focus, probe) == 1, choose_target_index(no_focus, probe))

    none_visible = [FakePage("https://a/1", visible=False), FakePage("https://a/2", visible=False)]
    check("窗口最小化（都不可见）时退化为最后一个可用页",
          choose_target_index(none_visible, probe) == 1, choose_target_index(none_visible, probe))

    internal = [FakePage("edge://settings", visible=True, focused=True),
                FakePage("about:blank", visible=True), FakePage("chrome://version", visible=True)]
    check("浏览器内部页不参与选择（不会对着设置页干活）",
          choose_target_index(internal, probe) == -1, choose_target_index(internal, probe))

    mixed = [FakePage("edge://settings", visible=True, focused=True),
             FakePage("https://a/1", visible=True)]
    check("内部页即使有焦点也不选它",
          choose_target_index(mixed, probe) == 1, choose_target_index(mixed, probe))

    check("没有任何标签页时返回 -1", choose_target_index([], probe) == -1)

    # probe 抛异常的页面不该让整个选择崩掉
    def bad_probe(page):
        if page.url.endswith("/boom"):
            raise RuntimeError("页面正在导航")
        return {"url": page.url, "visible": True, "focused": False}

    with_bad = [FakePage("https://a/boom"), FakePage("https://a/ok")]
    check("探测某页失败时按「读不出状态」处理，不影响其它页参与选择",
          choose_target_index(with_bad, bad_probe) == 1, choose_target_index(with_bad, bad_probe))


def test_selection_does_not_touch_other_tabs():
    """选择目标页的过程**不得**新建/关闭/切换任何标签页。"""
    target = FakePage("https://oa/tasks", focused=True)
    others = [FakePage("https://news/1", visible=True), FakePage("https://mail/1", visible=True)]
    context = FakeContext([others[0], target, others[1]])
    session = automation._BrowserSession(
        FakePlaywright(FakeChromium()), context, None, mode="reuse", reused=True,
        owns_browser=False,
    )
    engine = make_engine()
    page = engine._select_target_page(session)
    check("选中了活动页", page is target)
    check("没有新建标签页", context.new_page_calls == 0, context.new_page_calls)
    for index, other in enumerate(others + [target]):
        check(f"标签页 #{index} 没有被关闭", other.close_calls == 0, other.close_calls)
        check(f"标签页 #{index} 没有被切到前台", other.front_calls == 0, other.front_calls)
        check(f"标签页 #{index} 没有被导航", other.goto_calls == [], other.goto_calls)


def test_target_page_closed_is_an_error():
    """目标页被关掉：报错，**不换一页继续**。"""
    target = FakePage("https://oa/tasks", closed=True)
    spare = FakePage("https://other/")
    context = FakeContext([target, spare])
    session = automation._BrowserSession(
        FakePlaywright(FakeChromium()), context, None, mode="reuse", reused=True,
        owns_browser=False, target_page=target,
    )
    engine = make_engine()
    raised = ""
    try:
        engine._enforce_target_page(session)
    except AutomationError as exc:
        raised = str(exc)
    check("目标页被关掉时抛 AutomationError", raised != "", raised)
    check("报错说明是「标签页被关闭」", "关闭" in raised, raised)
    check("没有偷偷改用另一个标签页", session.target_page is target)
    check("备用标签页没有被导航", spare.goto_calls == [], spare.goto_calls)


def test_reuse_does_not_navigate():
    """复用模式下不导航、不新建标签页；只有空白页才 goto 配置里的任务列表网址。"""
    engine = make_engine(task_list_url="https://oa.example.com/tasks")
    page = FakePage("https://oa.example.com/tasks?page=3")
    session, context = make_session([page])
    session.target_page = page
    note = engine._prepare_task_list_page(page, session)
    check("复用模式下不导航（保留用户的筛选/翻页状态）", page.goto_calls == [], page.goto_calls)
    check("复用模式下回了「正在哪一页操作」的说明", "标签页" in note, note)
    check("同站点时不给「站点不一致」的警告", "不是同一个站点" not in note, note)

    other = FakePage("https://news.example.com/")
    session2, _ctx2 = make_session([other])
    note2 = engine._prepare_task_list_page(other, session2)
    check("跨站点时只警告、不替用户跳转", other.goto_calls == [], other.goto_calls)
    check("跨站点警告里带上了配置的网址", "不是同一个站点" in note2, note2)

    blank = FakePage("")
    session3, _ctx3 = make_session([blank])
    engine._prepare_task_list_page(blank, session3)
    check("用户的页面还是空白页时，才按配置打开任务列表网址",
          blank.goto_calls == ["https://oa.example.com/tasks"], blank.goto_calls)

    fresh_page = FakePage("")
    session4, _ctx4 = make_session([fresh_page], reused=False, mode="fresh")
    engine._prepare_task_list_page(fresh_page, session4)
    check("兜底新实例仍按老行为打开配置网址",
          fresh_page.goto_calls == ["https://oa.example.com/tasks"], fresh_page.goto_calls)
    check("_same_site 只认协议+主机+端口一致",
          _same_site("https://a.com/x", "https://a.com/y") is True
          and _same_site("https://a.com", "http://a.com") is False
          and _same_site("file:///c:/x", "file:///c:/y") is False)


# ----------------------------------------------------------------- 4. 点击优先


def test_click_preferred_over_goto():
    """进详情页：有活句柄就**模拟手动点击**，不直接 goto。"""
    engine = make_engine(task_list_url="https://oa/tasks", task_list_selector="a.task")
    list_page = FakePage("https://oa/tasks")
    handle = FakeHandle(list_page, goto_url="https://oa/detail/1")
    entry = automation._LinkEntry(text="任务1", href="https://oa/detail/1", handle=handle,
                                  href_raw="https://oa/detail/1", href_kind="absolute")
    context = FakeContext([list_page])
    opened = _drive(engine._open_task_detail(context, list_page, entry, [list_page]))
    check("点了链接（模拟手动点击）", handle.clicks >= 1, handle.clicks)
    check("点击后页面确实跳走了", list_page.url == "https://oa/detail/1", list_page.url)
    check("走点击路径时没有被 goto 直接打开",
          list_page.goto_calls == [], list_page.goto_calls)
    check("返回的详情页就是跳转后的那一页", opened is list_page, repr(opened))


def test_dead_handle_falls_back_to_goto():
    """句柄失效（页面重载过）：退回按地址导航，而不是白等点击超时。"""
    engine = make_engine(task_list_url="https://oa/tasks", task_list_selector="a.task")
    list_page = FakePage("https://oa/tasks")
    dead = FakeHandle(list_page, goto_url="", alive=False)
    entry = automation._LinkEntry(text="任务2", href="https://oa/detail/2", handle=dead,
                                  href_raw="https://oa/detail/2", href_kind="absolute")
    context = FakeContext([list_page])
    opened = _drive(engine._open_task_detail(context, list_page, entry, [list_page]))
    check("失效句柄没有被点击（避免 15 秒级的等待）", dead.clicks == 0, dead.clicks)
    check("失效句柄时退回按地址导航",
          list_page.goto_calls == ["https://oa/detail/2"], list_page.goto_calls)
    check("退回导航后返回当前页", opened is list_page)


def test_relocate_uses_selector():
    """回到列表页后按地址把任务重新定位成可点击句柄。"""
    engine = make_engine(task_list_selector="a.task")
    page = FakePage("https://oa/tasks")
    handle = FakeHandle(page, goto_url="https://oa/detail/9")

    class FakeLocator:
        def __init__(self, handles):
            self._handles = handles

        def all(self):
            return self._handles

    class FakeFrame(FakePage):
        def locator(self, selector):
            return FakeLocator([handle])

    frame = FakeFrame("https://oa/tasks")
    page.frames = [frame]
    engine._read_href = lambda h, base: automation._classify_href("https://oa/detail/9", base)
    engine._handle_text = lambda h: "任务9"
    entry = automation._LinkEntry(text="任务9", href="https://oa/detail/9")
    found = engine._relocate_entry(page, entry)
    check("重新定位找到了可点击句柄", found is handle, repr(found))
    check("没配任务列表选择器时不硬撑（返回 None 让调用方退回导航）",
          make_engine()._relocate_entry(page, entry) is None)


# ----------------------------------------------------------------- 5. 两段式状态机


def test_pick_machine_transitions():
    print("\n=== 4. 两段式选取状态机（等待准备 → 选取中 → 完成/取消 → 回到等待准备）===")
    machine = _PickMachine()
    check("初始状态是 idle", machine.state == PICK_STATE_IDLE, machine.state)
    check("初始不是进行中的会话", machine.active is False and machine.picking is False)
    check("初始 stage 为 0", machine.snapshot()["stage"] == 0, machine.snapshot()["stage"])

    raised = ""
    try:
        machine.begin()
    except AutomationError as exc:
        raised = str(exc)
    check("未打开页面就「开始选取」会被拒绝", raised != "", raised)
    check("被拒绝后状态仍是 idle（没有半死不活）", machine.state == PICK_STATE_IDLE, machine.state)

    machine.open(target="task_list", url="https://oa/tasks", title="任务列表",
                 mode="reuse", browser="Microsoft Edge", reused=True, note="已复用")
    check("open() 进入 waiting（第一步：等待用户准备）",
          machine.state == PICK_STATE_WAITING, machine.state)
    check("waiting 时 stage=1 且是「进行中」的会话",
          machine.snapshot()["stage"] == 1 and machine.active is True)
    check("记住了目标页地址（界面要显示给用户确认）", machine.url == "https://oa/tasks")
    check("记住了复用信息（reused=True）", machine.reused is True and machine.mode == "reuse")
    check("等待准备时还没有注入脚本（selector 为空）", machine.selector == "")

    machine.begin()
    check("begin() 进入 picking（第二步：开始选取）",
          machine.state == PICK_STATE_PICKING, machine.state)
    check("picking 时 stage=2", machine.snapshot()["stage"] == 2)
    raised = ""
    try:
        machine.begin()
    except AutomationError as exc:
        raised = str(exc)
    check("选取中再次 begin 会被拒绝（不会把状态弄乱）", raised != "", raised)
    check("被拒绝后仍在 picking", machine.state == PICK_STATE_PICKING, machine.state)

    machine.cancel()
    check("取消后回到 waiting（可以重选）", machine.state == PICK_STATE_WAITING, machine.state)
    check("取消记录为 last_result=cancelled", machine.last_result == "cancelled",
          machine.last_result)
    check("取消后本轮计数 +1", machine.rounds == 1, machine.rounds)
    check("取消不会产生「待写入配置」的结果", machine.take_pending_apply() is None)
    machine.cancel()
    check("不在选取中时再取消是空操作（不会再加一轮）", machine.rounds == 1, machine.rounds)

    machine.begin()
    machine.finish("picked", selector="a.task", message="已生成", preview={"matches": 3})
    check("选取完成后回到 waiting（任务书要求：可重选）",
          machine.state == PICK_STATE_WAITING, machine.state)
    check("记录选择器", machine.selector == "a.task", machine.selector)
    check("记录预览", machine.preview.get("matches") == 3, machine.preview)
    check("选取结果进入「待写回配置」队列",
          machine.take_pending_apply() == ("task_list", "a.task"))
    check("待写标记只能取走一次（轮询不会重复写配置）",
          machine.take_pending_apply() is None)

    machine.begin()
    machine.finish("rejected", message="你选中的不是链接")
    check("选错位置时 last_result=rejected", machine.last_result == "rejected", machine.last_result)
    check("选错位置不写配置", machine.take_pending_apply() is None)
    check("选错后仍在 waiting（请用户重选）", machine.state == PICK_STATE_WAITING)

    machine.begin()
    machine.mark_page_gone()
    check("目标页被关掉时记 page_closed", machine.last_result == "page_closed",
          machine.last_result)
    check("目标页被关掉后回到 waiting（但不换页，由用户决定下一步）",
          machine.state == PICK_STATE_WAITING)

    machine.begin()
    machine.close()
    check("close() 回到 idle", machine.state == PICK_STATE_IDLE, machine.state)
    check("close() 清掉目标页地址", machine.url == "", machine.url)
    check("close() 后不再是进行中的会话", machine.active is False)


def test_picker_payload_and_language():
    print("\n=== 5. 后端接口与语言包（面向用户的文案必须双语齐全）===")
    engine = make_engine()
    payload = engine.pick_state()
    check("初始状态可查询", payload["state"] == "idle", payload["state"])
    check("状态带了本地化标签", payload["state_label"] != "", payload["state_label"])
    check("带了「等待准备」阶段要显示的提示语", payload["ready_prompt"] != "",
          payload["ready_prompt"])
    check("提示语与任务书要求一致（先登录、导航，再点开始选取）",
          "登录" in payload["ready_prompt"] and "开始选取" in payload["ready_prompt"],
          payload["ready_prompt"])
    check("按钮文案由后端语言包提供", all(
        payload["buttons"][k] for k in ("open", "begin", "cancel", "close", "debug")),
        payload["buttons"])
    check("初始状态 can_begin=False（还没打开页面）", payload["can_begin"] is False)
    check("初始状态 can_cancel=False", payload["can_cancel"] is False)

    raised = ""
    try:
        engine.pick_begin()
    except AutomationError as exc:
        raised = str(exc)
    check("引擎层：未打开就 begin 抛 AutomationError", raised != "", raised)
    check("引擎层：begin 失败后状态仍是 idle", engine.pick_state()["state"] == "idle")
    closed = engine.pick_close()
    check("引擎层：没有会话时 close 是安全的（不抛异常）", closed["state"] == "idle",
          closed["state"])

    # 语言包：任务十七新增的 automation 段键必须中英双语齐全、占位符一致
    required = [
        "picker.ready_prompt", "picker.state_idle", "picker.state_waiting",
        "picker.state_picking", "picker.begin", "picker.cancel", "picker.close",
        "picker.start", "picker.debug_browser", "picker.reload_tab", "picker.section",
        "picker.hint_idle", "picker.hint_waiting", "picker.hint_picking",
        "picker.result.picked", "picker.result.cancelled", "picker.result.rejected",
        "picker.result.page_closed", "picker.result.error", "picker.already_picking",
        "picker.not_ready", "picker.inject_failed", "picker.browser_auto",
        "reuse.connected", "reuse.using_open_tab", "reuse.tab_mismatch",
        "reuse.not_connected", "reuse.disabled", "reuse.fallback_cost",
        "reuse.unsupported_browser", "reuse.required_failed", "reuse.no_usable_tab",
        "reuse.target_closed", "reuse.debug_ready", "reuse.debug_not_ready",
        "reuse.debug_no_exe", "reuse.debug_launch_failed",
        "link.click_manual", "link.goto_fallback",
    ]
    packs = {}
    for locale in ("zh-Hans", "en"):
        with open(os.path.join(ROOT, "backend", "lang", f"{locale}.json"), encoding="utf-8") as fh:
            packs[locale] = json.load(fh)["automation"]
    missing = [k for k in required if not packs["zh-Hans"].get(k) or not packs["en"].get(k)]
    check(f"任务十七的 {len(required)} 个语言键中英双语齐全", not missing, missing)
    check("英文不是照抄中文（抽查两条）",
          packs["en"]["picker.state_waiting"] != packs["zh-Hans"]["picker.state_waiting"]
          and packs["en"]["reuse.fallback_cost"] != packs["zh-Hans"]["reuse.fallback_cost"])

    import re

    def holders(text):
        return sorted(set(re.findall(r"\{(\w+)\}", str(text))))

    mismatch = [k for k in required if holders(packs["zh-Hans"].get(k)) != holders(packs["en"].get(k))]
    check("中英语言键的占位符一致", not mismatch, mismatch)

    # 界面状态文案要能跟着语言切换（后端 _t 跟随 set_language）
    from i18n import set_locale  # noqa: E402

    set_locale("en")
    english = engine.pick_state()["state_label"]
    set_locale("zh-Hans")
    chinese = engine.pick_state()["state_label"]
    check("状态标签随界面语言变化（英文界面不会出现中文状态名）",
          english != chinese and english.isascii(), (english, chinese))


# ----------------------------------------------------------------- 6. 调试启动


def test_debug_browser_helpers():
    print("\n=== 6. 「用调试模式启动浏览器」的边界 ===")
    check("找不到可执行文件时返回空串而不是猜一个路径",
          isinstance(debug_browser_executable("firefox"), str))
    check("Firefox 不参与调试启动（没有 CDP）", debug_browser_executable("firefox") == "")
    engine = make_engine(browser="firefox")
    info = engine.debug_browser_command()
    check("命令里仍然显式绑回环（不给「默认行为」留口子）",
          "--remote-debugging-address=127.0.0.1" in info["args"], info["args"])

    # 代启动：可执行文件找不到时必须**明确失败**，而不是假装启动成功
    engine2 = make_engine(browser="edge")
    engine2.debug_browser_command = lambda: {                       # type: ignore[assignment]
        "browser": "edge", "label": "Microsoft Edge", "executable": "",
        "args": [], "command": "msedge", "endpoint": _cdp_endpoint(9222),
        "profile_dir": engine2.debug_profile_dir,
    }
    result = engine2.launch_debug_browser()
    check("找不到浏览器时 launch_debug_browser 返回 ok=False", result["ok"] is False, result["ok"])
    check("失败时给出可读的说明（不是底层异常栈）",
          "没有找到" in result["message"], result["message"])

    # 端口探测要用**我们自己控制的端口**来断言，不能假设本机 9222 空闲：
    # 用户此刻可能正开着调试模式的浏览器，那时返回 True 才是正确行为。
    import socket as _socket
    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy_port = listener.getsockname()[1]
    check("端口探测：有监听时返回 True",
          make_engine(cdp_port=busy_port)._cdp_port_open() is True)
    listener.close()
    probe = _socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    check("端口探测：没人监听时返回 False",
          make_engine(cdp_port=free_port)._cdp_port_open() is False)


# ----------------------------------------------------------------- 7/8. 实机（可选）


def start_live_site():
    """起一个本机回环 HTTP 站点（两个实机用例共用），返回 ``(httpd, base)``。

    用 http 而不是 file://：真实内网系统都是 http(s)，而"点击链接 / 保留会话"
    这些行为在 file:// 下与真实站点差别很大。
    """
    import http.server
    import socketserver
    import threading

    list_page = ("<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
                 "<title>任务列表</title></head><body><h1>待处理任务</h1><ul>"
                 "<li><a class=\"task\" href=\"/detail?id=1\">合同A</a></li>"
                 "<li><a class=\"task\" href=\"/detail?id=2\">合同B</a></li>"
                 "</ul></body></html>")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
            body = list_page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, base


def test_two_stage_live(base):
    """真的开一个浏览器，把两段式跑一遍（默认跳过，见 main()）。

    为什么这一段必须存在而不是"只在报告里说一句验证过了"：任务书第 4 条的核心是
    **准备阶段不许注入脚本**——这件事只有真页面能证明（桩里的"页面"是我们自己写的）。
    默认跳过是因为它需要本机有 Edge/Chrome；没有浏览器的环境里，上面那批桩测试
    仍然覆盖了全部规则。

    这里浏览器走**兜底新实例**（``browser_reuse=off``），不要求用户先开调试端口，
    验证的是两段式本身而不是复用链路（复用链路见下一个用例）。
    """
    print("\n=== 7. 两段式选取的实机验证（真实浏览器）===")
    engine = make_engine(
        enabled=True, safety_accepted=True, task_list_url=base + "/",
        task_list_selector="a.task", browser="edge", browser_reuse="off",
        page_timeout_ms=15000,
    )
    try:
        payload = engine.pick_open("task_list")
        check("实机：open 之后进入 waiting", payload["state"] == "waiting", payload["state"])
        check("实机：显示了当前目标页地址", str(payload["url"]).startswith(base), payload["url"])
        page = engine._pick_session.target_page          # 测试读取内部句柄
        check("实机【关键】等待准备阶段没有注入选取脚本",
              page.evaluate("() => !window.__echodeskPick") is True)

        # 模拟"用户先登录 / 先翻页"：自己导航到别处
        page.goto(base + "/?page=2", wait_until="domcontentloaded")
        payload = engine.pick_status()
        check("实机：准备阶段用户自己导航不会被打断", payload["state"] == "waiting")
        check("实机：显示的目标页跟着用户导航更新", "page=2" in payload["url"], payload["url"])

        payload = engine.pick_begin()
        check("实机：begin 之后进入 picking", payload["state"] == "picking", payload["state"])
        check("实机【关键】这时脚本才注入到页面里",
              page.evaluate("() => !!window.__echodeskPick") is True)

        page.keyboard.press("Escape")
        payload = engine.pick_status()
        check("实机：按 Esc 后回到 waiting（可重选）", payload["state"] == "waiting")
        check("实机：Esc 被记为 cancelled", payload["last_result"] == "cancelled")
        check("实机：取消后高亮浮层已清掉",
              page.evaluate("() => document.querySelectorAll('[data-echodesk]').length") == 0)

        engine.pick_begin()
        page.click("a.task", timeout=8000)
        payload = engine.pick_status()
        check("实机：重选后拿到选取结果", payload["state"] == "waiting"
              and payload["last_result"] == "picked", payload["last_result"])
        check("实机：生成了选择器", bool(payload["selector"]), payload["selector"])
        check("实机：选取结果进入待写回队列",
              engine.take_pending_apply() == ("task_list", payload["selector"]))
        check("实机：待写标记只出现一次", engine.take_pending_apply() is None)

        final = engine.pick_close()
        check("实机：close 之后回到 idle 且没有残留会话",
              final["state"] == "idle" and engine._pick_session is None, final["state"])
    except Exception:
        check("实机测试未抛异常（本机是否装有 Edge？）", False, traceback.format_exc(limit=3))
    finally:
        try:
            engine.pick_close()
        except Exception:  # noqa: BLE001
            pass


def test_reuse_live(site_base):
    """实机验证**真的复用**一个已经打开的调试浏览器（默认跳过）。

    这是任务十七最核心的一条：用户要的不是"另一个浏览器"，而是**他手上那一个**。
    下面每一步都用真浏览器真 CDP 走一遍，包括最要紧的安全边界：
    复用结束后用户的浏览器必须还活着、他的标签页一个都没少也没多。

    跑完会留下一个以调试模式开着的浏览器窗口 —— 这与产品行为一致
    （``launch_debug_browser`` 的实例**不由程序关闭**，用户可以继续在里面登录）。
    """
    from playwright.sync_api import sync_playwright

    print("\n=== 8. 复用已打开的浏览器（实机）===")
    engine = make_engine(
        enabled=True, safety_accepted=True, browser="chrome", browser_reuse="required",
        cdp_port=9222, task_list_url=site_base + "/", task_list_selector="a.task",
        page_timeout_ms=15000,
    )
    try:
        if not engine._cdp_port_open():
            print("   （端口 9222 上没人监听：调用 launch_debug_browser() 代启动）")
            result = engine.launch_debug_browser()
            check("实机：代启动调试浏览器返回 ok=True", result.get("ok") is True,
                  result.get("message"))
            check("实机：代启动后调试端口真的在监听", engine._cdp_port_open() is True)
            check("实机：代启动命令显式绑回环",
                  "--remote-debugging-address=127.0.0.1" in result.get("args", []),
                  result.get("args"))

        # 借 CDP 在"用户的浏览器"里开一页（模拟用户日常打开的那一页），随后只断开连接
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(engine.cdp_endpoint, timeout=8000)
        context = browser.contexts[0]
        page = context.new_page()
        page.goto(site_base + "/", wait_until="domcontentloaded")
        page.bring_to_front()
        tabs_before = [p.url for p in context.pages]
        pw.stop()

        payload = engine.pick_open("task_list")
        check("实机：复用成功并进入 waiting（第一步）", payload["state"] == "waiting",
              payload["state"])
        check("实机：reused=True、mode=reuse",
              payload["reused"] is True and payload["mode"] == "reuse",
              (payload["reused"], payload["mode"]))
        check("实机：复用的就是用户打开的那一页",
              str(payload["url"]).startswith(site_base), payload["url"])
        session = engine._pick_session
        check("实机：没有新建标签页", len(context.pages) == len(tabs_before),
              (len(tabs_before), len(context.pages)))

        target = session.target_page
        check("实机：准备阶段没有注入脚本",
              target.evaluate("() => !window.__echodeskPick") is True)
        engine.pick_begin()
        check("实机：点了开始选取才注入脚本",
              target.evaluate("() => !!window.__echodeskPick") is True)
        target.click("a.task", timeout=8000)
        payload = engine.pick_status()
        check("实机：在用户的页面里选到了元素",
              payload["state"] == "waiting" and payload["last_result"] == "picked",
              (payload["state"], payload["last_result"]))
        check("实机：生成了选择器", bool(payload["selector"]), payload["selector"])
        engine.pick_close()

        # ---- 安全边界：用户的浏览器与标签页必须原样还在 ----
        pw2 = sync_playwright().start()
        browser2 = pw2.chromium.connect_over_cdp(engine.cdp_endpoint, timeout=8000)
        context2 = browser2.contexts[0]
        tabs_after = [p.url for p in context2.pages]
        pw2.stop()
        check("实机【安全边界】复用结束后浏览器仍然活着（能再连上）", True)
        check("实机【安全边界】没有关闭用户的标签页",
              all(u in tabs_after for u in tabs_before), (tabs_before, tabs_after))
        check("实机【安全边界】没有多出我们新建的标签页",
              len(tabs_after) == len(tabs_before), (tabs_before, tabs_after))
    except Exception:
        check("实机复用测试未抛异常（本机是否装有 Chrome 且 9222 空闲？）", False,
              traceback.format_exc(limit=3))
    finally:
        try:
            engine.pick_close()
        except Exception:  # noqa: BLE001
            pass


def main():
    print("任务十七：复用已打开的浏览器 + 两段式可视化选取（无浏览器测试）")
    # 全程用桩替换 Playwright；每个用例自己按需要再覆盖一次
    automation._sync_playwright = None
    try:
        test_loopback()
        test_cdp_decisions()
        test_firefox_never_tries_cdp()
        test_cdp_connect_failure_falls_back()
        test_reuse_mode_required_and_off()
        test_successful_reuse()
        test_choose_target_index()
        test_selection_does_not_touch_other_tabs()
        test_target_page_closed_is_an_error()
        test_reuse_does_not_navigate()
        test_click_preferred_over_goto()
        test_dead_handle_falls_back_to_goto()
        test_relocate_uses_selector()
        test_pick_machine_transitions()
        test_picker_payload_and_language()
        test_debug_browser_helpers()
        if os.environ.get("ECHODESK_LIVE_BROWSER") == "1":
            # 实机段开始前还原真实的 Playwright 入口（见 REAL_SYNC_PLAYWRIGHT 的说明）
            automation._sync_playwright = REAL_SYNC_PLAYWRIGHT
            httpd, base = start_live_site()
            try:
                test_two_stage_live(base)
                test_reuse_live(base)
            finally:
                httpd.shutdown()
                httpd.server_close()
        else:
            print("\n=== 7/8. 实机验证（两段式 + 真复用）===")
            print("   已跳过（默认不开浏览器）。要连同真实浏览器一起跑："
                  "set ECHODESK_LIVE_BROWSER=1 && python tools\\test_browser_reuse.py")
    except Exception:
        check("测试整体未抛异常", False, traceback.format_exc(limit=4))
    finally:
        # 还原模块级入口：测试进程结束时不该留下"被替换过的 Playwright"
        automation._sync_playwright = REAL_SYNC_PLAYWRIGHT
        import shutil
        shutil.rmtree(SCRATCH, ignore_errors=True)

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    for name in failed:
        print(f"  ❌ {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
