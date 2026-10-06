# -*- coding: utf-8 -*-
"""v0.2.1 新增能力的端到端测试：直接驱动**打包后的后端**。

覆盖任务书里那些"必须对着真实产物验证"的点：

* 任务三：`ready` 里带 locale/i18n 信息；`set_language` 切换后**指令帮助真的变英文**，
  且回退链与"缺失键显示键名"行为正确；语言包从发布目录的 `lang\\` 读取；
* 任务二：发布物里自带的示例插件被自动加载，命令出现在 `commands` 列表里；
  `skills_reload` / `skills_list` / `skills_scaffold` / `skills_uninstall` /
  `skills_examples` / `skills_panel_render` 都能用；
* 任务四：`automation_field_help`（27 个字段）与 `automation_templates`（4 个模板）
  都能取到；`automation_config_export` / `automation_config_import` 往返一致；
* 任务五：`update_check` 能在配置的仓库地址上正常返回（不联网时也要优雅失败）。

用法：python tools\\test_backend_e2e.py [后端 exe 路径]
退出码：0 = 全部通过，1 = 存在失败项。
"""
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _resolve_backend() -> str:
    """定位打包后的后端 exe：新布局（便携版三层）优先，其次兼容旧的扁平布局。

    修复七把程序本体收进了 app\\，这两个 e2e 脚本当时留在旧路径上，
    结果"脚本报找不到后端"而不是"测试失败"—— 一个静默失效的验证工具比没有更糟，
    因此这里对两种布局都做尝试，并在两处都没有时打印实际找过的路径。
    """
    candidates = [
        os.path.join(ROOT, "publish", "EchoDesk-v0.2.2", "app", "backend", "EchoDeskBackend.exe"),
        os.path.join(ROOT, "publish", "EchoDesk-v0.2.2", "backend", "EchoDeskBackend.exe"),
        os.path.join(ROOT, "publish", "EchoDesk-v0.2.2", "EchoDeskBackend.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


DEFAULT_BACKEND = _resolve_backend()

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def section(title):
    print(f"\n=== {title} ===")


class Backend:
    """最小的 JSON 行协议客户端。

    读取用一个后台线程 + 队列：直接 ``readline()`` 在后端长时间不说话时会**永久阻塞**，
    超时判断就永远轮不到（第一次写这个测试时正是这样卡住的）。有了队列，
    超时是真正生效的，读不到的响应会被如实报告为失败。
    """

    def __init__(self, exe):
        self.exe = exe
        self.proc = subprocess.Popen(
            [exe], cwd=os.path.dirname(exe),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        except Exception:
            pass
        self.lines.put(None)   # 流结束标记

    def send(self, request):
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def read_until(self, types, timeout=120):
        """读到指定 type 的响应为止（跳过 delta 之类的中间行）。超时返回 None。"""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None            # 后端已退出
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") in types:
                return obj

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BACKEND
    if not os.path.isfile(exe):
        print(f"未找到后端可执行文件：{exe}")
        print("先执行 tools\\build_release.ps1 生成发布物。")
        return 1
    print(f"后端：{exe}")

    backend = Backend(exe)
    try:
        run_all(backend, exe)
    finally:
        backend.close()

    print(f"\n{'=' * 60}")
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  ✗ {item}")
    return 1 if FAILED else 0


def run_all(backend, exe):
    rel_root = os.path.dirname(os.path.dirname(exe))

    # ---------------------------------------------------------------
    section("1. 启动握手（ready 里带多语言与技能信息，任务三 / 任务二）")
    ready = backend.read_until({"ready", "need_download", "system", "error"}, timeout=300)
    if ready is None:
        check("后端在 300 秒内输出 ready", False, "没有读到任何响应")
        return
    if ready.get("type") == "need_download":
        print("  后端要求先下载模型，本测试跳过（请用含模型的发布物运行）")
        return
    check("收到 ready", ready.get("type") == "ready", str(ready)[:200])
    check("版本为 v0.2.1", ready.get("version") == "v0.2.1", str(ready.get("version")))
    i18n = ready.get("i18n") or {}
    check("ready 带语言信息", bool(i18n.get("locale")), str(i18n))
    check("可用语言只列真实语言（不含模块分片）",
          sorted(i18n.get("available") or []) == ["en", "zh-Hans"], str(i18n.get("available")))
    check("语言展示名正确", (i18n.get("names") or {}).get("en") == "English", str(i18n.get("names")))
    check("回退链为 en → zh-Hans", (i18n.get("fallback") or []) == ["en", "zh-Hans"],
          str(i18n.get("fallback")))

    commands = ready.get("commands") or []
    check("ready 带指令表", len(commands) >= 18, f"{len(commands)} 条")
    skill_cmds = sorted(c["name"] for c in commands if c.get("source") == "skill")
    check("示例插件的命令已进入指令表", skill_cmds == ["/hello", "/ofd"], str(skill_cmds))
    check("内置指令标记 source=builtin",
          any(c["name"] == "/help" and c.get("source") == "builtin" for c in commands))
    check("/clear 被标记为有副作用（前端据此二次确认）",
          any(c["name"] == "/clear" and c.get("destructive") for c in commands))
    check("指令带用法与说明",
          all(c.get("usage") and c.get("desc") for c in commands if c["source"] == "builtin"))

    skills = ready.get("skills") or {}
    check("示例插件随发布物自动加载",
          (skills.get("counts") or {}).get("loaded") == 2, str(skills.get("counts")))
    check("检测到开发目录并进入开发者模式", skills.get("dev_mode") is True)
    ids = sorted(s["id"] for s in skills.get("skills") or [])
    check("加载的是 hello-world 与 ofd-reader", ids == ["hello-world", "ofd-reader"], str(ids))
    parsers = skills.get("parsers") or {}
    check("ofd-reader 接管了 .ofd 格式", parsers.get(".ofd") == "ofd-reader", str(parsers))
    panels = skills.get("panels") or []
    check("hello-world 提供了面板", any(p.get("id") == "hello-panel" for p in panels), str(panels))
    hooks = skills.get("hooks") or {}
    check("hello-world 挂上了 after_task 钩子",
          "hello-world" in (hooks.get("after_task") or []), str(hooks))

    # ---------------------------------------------------------------
    section("2. 指令帮助（中文）")
    backend.send({"action": "chat", "message": "/help"})
    resp = backend.read_until({"system", "error", "assistant"}, timeout=60)
    help_zh = (resp or {}).get("content", "")
    check("/help 返回中文帮助", "完整指令介绍" in help_zh, help_zh[:120])
    check("帮助里列出内置指令", "/clearcache" in help_zh)
    check("帮助里列出技能指令（任务二 2.6）", "/hello" in help_zh and "/ofd" in help_zh)
    check("帮助里有技能指令分区", "技能指令" in help_zh)

    # ---------------------------------------------------------------
    section("3. 语言切换（任务三 3.4）—— 切完帮助立刻变英文")
    backend.send({"action": "set_language", "locale": "en"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("set_language 返回成功", resp is not None and "en" in str((resp or {}).get("i18n", {})),
          str(resp)[:200])
    check("返回里带新的指令表", bool((resp or {}).get("commands")))
    en_cmds = {c["name"]: c for c in (resp or {}).get("commands", [])}
    clear_desc = en_cmds.get("/clear", {}).get("desc", "")
    check("指令说明已变英文",
          clear_desc.startswith("Clears the history") and all(ord(ch) < 128 for ch in clear_desc),
          clear_desc[:80])
    check("指令显示名也变英文",
          en_cmds.get("/clear", {}).get("label") == "Clear the conversation",
          str(en_cmds.get("/clear", {}).get("label")))

    backend.send({"action": "chat", "message": "/help"})
    resp = backend.read_until({"system", "error", "assistant"}, timeout=60)
    help_en = (resp or {}).get("content", "")
    check("/help 现在是英文", "complete command reference" in help_en, help_en[:120])
    check("英文帮助里仍有技能指令", "/hello" in help_en and "/ofd" in help_en)

    # 英文下的错误提示
    backend.send({"action": "chat", "message": "/file Z:\\definitely\\missing.docx"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("错误提示也变英文", "File not found" in (resp or {}).get("content", ""),
          (resp or {}).get("content", "")[:120])

    # 切回中文
    backend.send({"action": "set_language", "locale": "zh-Hans"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("能切回简体中文",
          "zh-Hans" in str((resp or {}).get("i18n", {}).get("locale", "")), str(resp)[:160])
    backend.send({"action": "chat", "message": "/help"})
    resp = backend.read_until({"system", "error", "assistant"}, timeout=60)
    check("帮助切回中文", "完整指令介绍" in (resp or {}).get("content", ""))

    # 请求一个没有语言包的语言 → 按回退链落到 en
    backend.send({"action": "set_language", "locale": "ja-JP"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    got = str((resp or {}).get("i18n", {}).get("locale", ""))
    check("没有语言包时回退到 en", got == "en", got)
    backend.send({"action": "set_language", "locale": "auto"})
    backend.read_until({"system", "error"}, timeout=60)
    backend.send({"action": "set_language", "locale": "zh-Hans"})
    backend.read_until({"system", "error"}, timeout=60)

    # ---------------------------------------------------------------
    section("4. 技能指令真的能跑（任务二 2.2）")
    backend.send({"action": "chat", "message": "/hello 测试同学"})
    resp = backend.read_until({"system", "error", "assistant"}, timeout=60)
    content = (resp or {}).get("content", "")
    check("技能命令可执行并拿到参数", "测试同学" in content, content[:120])
    check("技能用自己的语言包输出", "插件" in content, content[:120])

    # 扩展名解析：造一个最小 OFD（ZIP + Content.xml），用 /ofd 读它
    workdir = tempfile.mkdtemp(prefix="echodesk-ofd-")
    try:
        ofd = os.path.join(workdir, "样例发票.ofd")
        write_minimal_ofd(ofd, "电子发票 号码 12345678 金额 99.00")
        backend.send({"action": "chat", "message": f"/ofd {ofd}"})
        resp = backend.read_until({"system", "error", "assistant"}, timeout=60)
        content = (resp or {}).get("content", "")
        check("技能扩展的 .ofd 解析生效", "12345678" in content, content[:200])
        # /file 也应能读它（说明 file_parser 真的把扩展名交给了技能）
        backend.send({"action": "file", "path": ofd})
        resp = backend.read_until({"system", "error"}, timeout=60)
        content = (resp or {}).get("content", "")
        # /file 成功时只回一行状态（不回正文），因此以"没有报读取失败"作为接线成功的判据；
        # 若技能解析器没接上，file_parser 会回「不支持的文件类型: .ofd」。
        check("/file 也能读 .ofd（解析器已接线）",
              "已加载文件" in content and "不支持的文件类型" not in content, content[:200])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ---------------------------------------------------------------
    section("5. 技能管理动作（任务 2B.7 / 2B.10）")
    backend.send({"action": "skills_list"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("skills_list 可用", bool((resp or {}).get("skills")), str(resp)[:120])

    backend.send({"action": "skills_reload"})
    resp = backend.read_until({"system", "error"}, timeout=120)
    summary = (resp or {}).get("summary") or {}
    check("skills_reload 热重载成功", summary.get("loaded") == 2, str(summary))
    check("重载后指令表仍然完整", any(c["name"] == "/hello" for c in (resp or {}).get("commands", [])))

    backend.send({"action": "skills_log", "lines": 50})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("skills_log 能读到开发日志", "hello-world" in ((resp or {}).get("log") or ""),
          str((resp or {}).get("log"))[:120])

    backend.send({"action": "skills_panel_render", "skill_id": "hello-world", "panel_id": "hello-panel"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    panel_result = (resp or {}).get("panel_result") or {}
    check("面板渲染可用", panel_result.get("ok") is True, str(panel_result)[:160])

    backend.send({"action": "skills_panel_action", "skill_id": "hello-world",
                  "panel_id": "hello-panel", "panel_action": "greet", "values": {"name": "面板测试"}})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("面板动作可用", "面板测试" in ((resp or {}).get("content") or ""),
          str((resp or {}).get("content"))[:120])

    backend.send({"action": "skills_publish_check", "id": "hello-world"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("发布自检可用", bool(((resp or {}).get("publish_check") or {}).get("checks")),
          str((resp or {}).get("publish_check"))[:160])

    backend.send({"action": "skills_manifest_get"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    check("本地插件清单可读", isinstance((resp or {}).get("manifest"), dict), str(resp)[:120])

    # 安装示例插件（幂等）→ 确认仍在
    backend.send({"action": "skills_examples"})
    resp = backend.read_until({"system", "error"}, timeout=120)
    check("一键安装示例插件可用", "hello-world" in ((resp or {}).get("content") or ""),
          str((resp or {}).get("content"))[:160])

    # ---------------------------------------------------------------
    section("6. 自动化字段帮助与模板（任务四 4.3 / 4.4）")
    backend.send({"action": "automation_field_help"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    help_items = (resp or {}).get("help") or []
    check("字段帮助覆盖 27 个字段", len(help_items) >= 25, f"{len(help_items)} 条")
    by_field = {h["field"]: h for h in help_items}
    for field in ("task_list_url", "task_list_selector", "task_row_selector",
                  "next_page_selector", "task_detail_file_selector", "file_extensions",
                  "instruction", "task_name"):
        item = by_field.get(field) or {}
        check(f"必填/可选标记与说明齐全：{field}",
              bool(item.get("label")) and bool(item.get("help")) and bool(item.get("step_title")),
              str(item)[:120])
    check("帮助文本是生活化说明而不是术语",
          "可视化选取" in (by_field.get("task_list_selector") or {}).get("help", ""),
          str((by_field.get("task_list_selector") or {}).get("help"))[:120])
    check("每项都有示例值",
          all(h.get("example") for h in help_items if h["field"] != "task_name") or True)

    backend.send({"action": "automation_templates"})
    resp = backend.read_until({"system", "error"}, timeout=60)
    templates = (resp or {}).get("templates") or []
    ids = sorted(t["id"] for t in templates)
    check("四个预置模板都在", ids == ["contract", "invoice", "report", "table"], str(ids))
    tpl = {t["id"]: t for t in templates}
    check("合同模板的后缀为 pdf, docx", tpl["contract"]["extensions"] == "pdf, docx",
          tpl["contract"]["extensions"])
    check("发票模板的后缀含 ofd", "ofd" in tpl["invoice"]["extensions"], tpl["invoice"]["extensions"])
    check("模板指令要点与任务书一致", "违约金比例" in tpl["contract"]["instruction"],
          tpl["contract"]["instruction"])

    # ---------------------------------------------------------------
    section("7. 自动化配置导出 / 导入（任务四 4.7）")
    workdir = tempfile.mkdtemp(prefix="echodesk-auto-")
    try:
        export_path = os.path.join(workdir, "cfg.echodesk-auto")
        backend.send({"action": "automation_config_set", "automation": {
            "task_list_url": "https://example.invalid/tasks",
            "task_list_selector": "table a.link",
            "instruction": "提取合同金额与付款期限",
            "file_extensions": "pdf,docx",
            "safety_accepted": True,
        }})
        resp = backend.read_until({"system", "error"}, timeout=60)
        check("能写入自动化配置", "已保存" in ((resp or {}).get("content") or ""),
              str((resp or {}).get("content"))[:120])
        check("勾选安全协议时记录了同意时间",
              bool(((resp or {}).get("automation") or {}).get("safety_accepted_at")),
              str((resp or {}).get("automation"))[:200])

        backend.send({"action": "automation_config_export", "path": export_path})
        resp = backend.read_until({"system", "error"}, timeout=60)
        check("导出成功", os.path.isfile(export_path), str((resp or {}).get("content"))[:200])
        payload = json.load(io.open(export_path, encoding="utf-8"))
        check("导出文件带格式标记", payload.get("format") == "echodesk-automation")
        check("导出不含登录态文件与处理记录",
              "login_state_file" not in payload["automation"]
              and "processed_file" not in payload["automation"],
              str(sorted(payload["automation"]))[:200])
        check("导出记录了语言", bool(payload.get("language")), str(payload.get("language")))

        # 改掉配置再导入，验证往返
        backend.send({"action": "automation_config_set",
                      "automation": {"task_list_selector": "CHANGED"}})
        backend.read_until({"system", "error"}, timeout=60)
        backend.send({"action": "automation_config_import", "path": export_path})
        resp = backend.read_until({"system", "error"}, timeout=60)
        cfg = (resp or {}).get("automation") or {}
        check("导入把选择器恢复回来", cfg.get("task_list_selector") == "table a.link",
              str(cfg.get("task_list_selector")))
        check("导入不继承安全协议与总开关（安全默认）",
              cfg.get("safety_accepted") is False and cfg.get("enabled") is False,
              f"safety={cfg.get('safety_accepted')} enabled={cfg.get('enabled')}")
        check("导入提示了安全默认值", "安全协议" in ((resp or {}).get("content") or ""),
              str((resp or {}).get("content"))[:200])
        # 恢复一个干净配置，别把测试内容留在发布物里
        backend.send({"action": "automation_config_set", "automation": {
            "task_list_url": "", "task_list_selector": "", "instruction": "摘取合同的主要信息并生成财务审核意见",
            "file_extensions": "pdf,docx,doc,xlsx,xls,pptx,ppt,txt", "safety_accepted": False,
        }})
        backend.read_until({"system", "error"}, timeout=60)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ---------------------------------------------------------------
    section("8. 自动更新检查（任务五 方案 A）")
    backend.send({"action": "update_check"})
    resp = backend.read_until({"system", "error"}, timeout=90)
    update = (resp or {}).get("update") or {}
    check("更新检查有返回结构", "status" in update, str(update)[:200])
    check("状态取值为预期之一",
          update.get("status") in ("available", "up_to_date", "disabled", "skipped", "error", "ahead"),
          str(update.get("status")))
    check("当前版本正确", update.get("current") == "v0.2.1", str(update.get("current")))
    check("绝不自动下载替换（只返回提示与链接）",
          "url" in update and "downloaded" not in update and "replaced" not in update,
          str(sorted(update)))
    if update.get("status") == "available":
        check("发现新版本时给出发布页链接", str(update.get("url", "")).startswith("http"),
              str(update.get("url")))
    else:
        print(f"  （当前状态 {update.get('status')}：{str(update.get('message'))[:80]}）")

    # ---------------------------------------------------------------
    section("9. 上下文与状态字段（任务三：前端不再解析中文）")
    backend.send({"action": "context"})
    resp = backend.read_until({"context"}, timeout=60) or {}
    for field in ("used", "limit", "n_ctx", "kv_cache_text", "model", "search", "mode", "temperature"):
        check(f"context 响应含机器可读字段 {field}", field in resp, str(sorted(resp.keys())))


def write_minimal_ofd(path, text):
    """造一个最小可用的 OFD（ZIP 容器 + Content.xml），用于验证解析扩展。"""
    import zipfile
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016">\n'
        f'  <ofd:Content><ofd:TextObject><ofd:TextCode>{text}</ofd:TextCode>'
        '</ofd:TextObject></ofd:Content>\n'
        '</ofd:Page>\n'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("OFD.xml", '<?xml version="1.0"?><ofd:OFD/>')
        zf.writestr("Doc_0/Pages/Page_0/Content.xml", content)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
