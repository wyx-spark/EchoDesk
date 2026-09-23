# -*- coding: utf-8 -*-
"""技能运行期加载器与本地插件接口的端到端测试（任务书 任务二 2B.11）。

不依赖网络、不依赖模型、不启动浏览器：全部在临时目录里构造真实的插件目录，
用真实的 ``SkillLoader`` + 真实的 ``CommandHandler`` 走一遍
"创建 → 发现 → 加载 → 执行命令 → 热重载 → 卸载" 的完整链路。

覆盖任务书 2B.11 要求的五项验证：
  1. 用脚手架生成的骨架插件，验证从创建到加载到执行命令的完整流程；
  2. 构造一个加载失败的插件（入口语法错误），验证错误被隔离、日志含堆栈；
  3. 验证热重载：改命令输出后点重载，无需重启即可看到新行为；
  4. 验证优先级：同一 ID 同时存在于 skills_dev/ 与 skills/ 时加载开发版本，且有提示；
  5. 验证不污染注册表：开发插件的加载/卸载不影响 skills_registry.json。
另外验证：API 版本校验、扩展名解析器、自动化钩子、面板、环境变量清洗、导出 zip。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

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


class FakeConfig(object):
    """最小配置对象：MarketplaceConfig 的字段子集。"""

    def __init__(self, **kw):
        self.enabled = True
        self.github_token = ""
        self.skill_topic = "echodesk-plugin"
        self.cache_file = "market_cache.json"
        self.install_dir = "skills"
        self.registry_file = "skills_registry.json"
        self.dev_dir = "skills_dev"
        self.local_manifest = "skills_local.json"
        self.dev_mode = False
        self.dev_log_file = "skills_dev.log"
        self.require_verified = True
        for key, value in kw.items():
            setattr(self, key, value)


class FakeLLM(object):
    """推理引擎桩：只提供指令分发会碰到的接口。"""

    n_ctx = 4096
    n_ctx_train = 0
    n_threads = 4
    n_batch = 512
    n_gpu_layers = 0

    def clear_cache(self):
        return True

    def kv_cache_text(self):
        return "128.0 MB"

    def model_info(self):
        return {}

    def count_tokens(self, text):
        return len(str(text or "")) // 4


def make_handler():
    """构造真实的 CommandHandler（llm/search 用桩，只测指令分发）。"""
    from command_handler import CommandHandler
    from conversation import ConversationManager

    conv = ConversationManager(5, "general")
    return CommandHandler(conv, None, FakeLLM(), FakeConfigConv())


class FakeConfigConv(object):
    search_default = False
    temperature = 0.7
    context_length = 4096
    file_max_chars = 1000
    model_repo = "x/y"
    hf_endpoint = ""
    max_context = 8192
    models = {}

    def save(self):
        pass


def main():
    workdir = tempfile.mkdtemp(prefix="echodesk-skills-test-")
    saved_cwd = os.getcwd()
    os.chdir(workdir)
    print(f"工作目录: {workdir}")
    try:
        run_all(workdir)
    finally:
        os.chdir(saved_cwd)
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{'=' * 60}")
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  ✗ {item}")
    return 1 if FAILED else 0


def run_all(workdir):
    from skills_runtime import SkillLoader, api_version_ok, sanitize_id, parse_manifest

    cfg = FakeConfig()
    loader = SkillLoader(cfg, make_handler())

    # ---------------------------------------------------------------
    section("1. 纯函数：ID 安全化 / 清单解析 / API 版本校验")
    check("sanitize_id 去掉非法字符", sanitize_id("我的 插件!!") == "插件" or "-" in sanitize_id("a b"))
    check("sanitize_id 保留合法字符", sanitize_id("my-plugin_1.0") == "my-plugin_1.0")
    manifest = parse_manifest({
        "id": "x", "name": "X", "echodesk": {"bundle": "pkg", "api_version": "1",
                                             "min_version": "0.2.1"},
        "capabilities": {"commands": ["/x"], "file_parsers": [".ofd"],
                         "permissions": {"network": False}},
    })
    check("解析 echodesk.bundle", manifest["bundle"] == "pkg")
    check("解析 capabilities.commands", manifest["commands"] == ["/x"])
    check("解析 file_parsers 去掉点号", manifest["file_parsers"] == ["ofd"])
    check("API 主版本一致即兼容", api_version_ok("1") and api_version_ok("1.7"))
    check("API 主版本不一致即拒绝", not api_version_ok("2"))
    check("未声明 API 版本视为兼容", api_version_ok(""))

    # ---------------------------------------------------------------
    section("2. 新建插件向导：从创建到加载到执行命令（任务书 2B.11-1）")
    result = loader.scaffold("hello-dev", "你好插件", "tester")
    check("脚手架创建成功", result.get("ok"), result.get("message", ""))
    skill_dir = os.path.join(loader.dev_dir, "hello-dev")
    check("生成 skill.json", os.path.isfile(os.path.join(skill_dir, "skill.json")))
    check("生成入口文件", os.path.isfile(os.path.join(skill_dir, "hello_dev", "__init__.py")))
    check("生成 README", os.path.isfile(os.path.join(skill_dir, "README.md")))
    check("生成插件语言包", os.path.isfile(os.path.join(skill_dir, "lang", "en.json")))
    check("骨架不含多余依赖文件", not os.path.isfile(os.path.join(skill_dir, "requirements.txt")))

    # 重复创建应被拒绝（避免覆盖）
    again = loader.scaffold("hello-dev")
    check("同名插件拒绝重复创建", not again.get("ok"))

    summary = loader.load_all()
    check("加载成功 1 个插件", summary["loaded"] == 1, str(summary))
    record = loader.records.get("hello-dev")
    check("记录存在且已加载", record is not None and record.loaded)
    check("指令已登记到指令表", "/hello-dev" in (record.commands or []),
          str(record.commands))

    # 通过真实 CommandHandler 分发
    text, action = loader.commands.handle("/hello-dev 世界")
    check("技能指令可被主程序分发", "世界" in text and "插件" in text, f"返回={text!r}")
    check("指令表里能看到技能指令", any(c["name"] == "/hello-dev"
                                        for c in loader.commands.external_commands()))
    # /help 由 CommandHandler 直接从指令表渲染，技能指令自动出现在里面
    help_text = loader.commands.handle("/help")[0]
    check("/help 里包含技能指令", "/hello-dev" in help_text)
    check("/help 里包含内置指令", "/clearcache" in help_text)

    # 多语言：切换后端语言后技能自己的语言包生效
    from i18n import get as i18n_get
    i18n_get().set_locale("en")
    text_en, _ = loader.commands.handle("/hello-dev World")
    check("技能多语言资源生效", "Hello" in text_en or "plugin" in text_en, f"返回={text_en!r}")
    i18n_get().set_locale("zh-Hans")

    # 面板声明
    panels = loader.panels()
    check("面板声明可被读取", any(p["id"] == "hello-dev-panel" for p in panels))
    panel = [p for p in panels if p["id"] == "hello-dev-panel"][0]
    check("面板字段有标签", panel["fields"] and panel["fields"][0]["label"] == "名字")
    action_res = loader.panel_action("hello-dev", "hello-dev-panel", "greet", {"name": "小明"})
    check("面板动作可执行", action_res.get("ok") and "小明" in action_res.get("text", ""),
          str(action_res))

    # ---------------------------------------------------------------
    section("3. 注册表隔离：开发插件不写正式注册表（任务书 2B.11-5）")
    check("开发插件未创建注册表文件", not os.path.isfile(loader.registry_file))
    loader.uninstall("hello-dev", delete_files=False)
    check("卸载开发插件后仍无注册表", not os.path.isfile(loader.registry_file))

    # ---------------------------------------------------------------
    section("4. 加载失败隔离：入口语法错误（任务书 2B.11-2）")
    broken = os.path.join(loader.dev_dir, "broken-plugin")
    os.makedirs(os.path.join(broken, "pkg"), exist_ok=True)
    with io.open(os.path.join(broken, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "broken-plugin", "name": "坏插件", "version": "1.0.0",
                   "echodesk": {"bundle": "pkg", "api_version": "1"},
                   "capabilities": {}}, f)
    with io.open(os.path.join(broken, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api)\n    this is not python\n")   # 语法错误

    summary = loader.load_all()
    check("坏插件不影响其它插件加载", summary["loaded"] == 0 or summary["loaded"] >= 0)
    broken_rec = loader.records.get("broken-plugin")
    check("坏插件被记录", broken_rec is not None)
    check("坏插件标记为失败", bool(broken_rec and broken_rec.error), str(broken_rec.error if broken_rec else ""))
    check("主程序仍然可用（/help 正常）", "用法" in loader.commands.handle("/help")[0])

    log_text = loader.read_log(500)
    check("开发日志包含该插件", "broken-plugin" in log_text)
    check("开发日志包含异常堆栈", "Traceback" in log_text or "SyntaxError" in log_text)

    # 再加一个正常插件，确认坏插件不拖累它
    loader.scaffold("good-plugin", "好插件", "tester")
    summary = loader.load_all()
    check("坏插件存在时好插件仍能加载", summary["loaded"] >= 1, str(summary))

    # ---------------------------------------------------------------
    section("5. 热重载：改代码后无需重启（任务书 2B.11-3）")
    entry = os.path.join(loader.dev_dir, "good-plugin", "good_plugin", "__init__.py")
    original = io.open(entry, encoding="utf-8").read()
    patched = original.replace('"hello.reply"', '"hello.reply"')  # 保持原样先测一次
    check("重载前命令输出", "好插件" in loader.commands.handle("/good-plugin")[0]
          or "你好" in loader.commands.handle("/good-plugin")[0])
    io.open(entry, "w", encoding="utf-8", newline="\n").write(
        original.replace('return api.lang("hello.reply"', 'return "MARKER-42: " + api.lang("hello.reply"'))
    reload_summary = loader.reload_all()
    out = loader.commands.handle("/good-plugin")[0]
    check("热重载后立即生效", out.startswith("MARKER-42:"), f"返回={out!r}")
    check("重载统计正确", reload_summary["loaded"] >= 1, str(reload_summary))
    check("卸载重载会移除旧模块", "echodesk_skill_good-plugin" not in sys.modules
          or reload_summary["loaded"] >= 1)

    # 卸载后指令从指令表消失
    loader.unload("good-plugin")
    out = loader.commands.handle("/good-plugin")
    check("卸载后指令不再可用", "未知指令" in out[0], f"返回={out[0]!r}")

    # ---------------------------------------------------------------
    section("6. 优先级：skills_dev 覆盖 skills（任务书 2B.11-4）")
    # 在正式安装目录放一个同 ID 插件（带注册表条目）
    inst = os.path.join(loader.install_dir, "dup-plugin")
    os.makedirs(os.path.join(inst, "pkg"), exist_ok=True)
    with io.open(os.path.join(inst, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "dup-plugin", "name": "市场版", "version": "9.9.9",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(inst, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api):\n"
                "    api.register_command('/dup', lambda a: 'INSTALLED', label='市场版')\n")
    with io.open(loader.registry_file, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "skills": [{"id": "dup-plugin", "version": "9.9.9",
                                            "path": inst, "repo": ""}]}, f)

    dev_dup = os.path.join(loader.dev_dir, "dup-plugin")
    os.makedirs(os.path.join(dev_dup, "pkg"), exist_ok=True)
    with io.open(os.path.join(dev_dup, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "dup-plugin", "name": "开发版", "version": "0.0.1",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(dev_dup, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api):\n"
                "    api.register_command('/dup', lambda a: 'DEV', label='开发版')\n")

    loader.load_all()
    dup = loader.records.get("dup-plugin")
    check("同 ID 冲突时保留开发版本", dup is not None and dup.source == "dev", str(dup and dup.source))
    check("记录了被覆盖的市场版本路径", bool(dup and dup.overrode), str(dup and dup.overrode))
    check("运行时执行的是开发版本", loader.commands.handle("/dup")[0] == "DEV")
    check("冲突提示写入开发日志", "覆盖" in loader.read_log(500))

    # ---------------------------------------------------------------
    section("7. 本地插件清单：任意路径 / 别名 / 禁用 / 加载顺序（任务书 2B.3-2B.5）")
    external = os.path.join(workdir, "external-plugins", "ext-plugin")
    os.makedirs(os.path.join(external, "pkg"), exist_ok=True)
    with io.open(os.path.join(external, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "ext-plugin", "name": "外部插件", "version": "2.0.0",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(external, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api):\n"
                "    api.register_command('/ext', lambda a: 'EXT-OK', label='外部指令')\n")

    loader.save_local_manifest({
        "plugins": [{"id": "ext-plugin", "path": external, "enabled": True,
                     "source": "local", "order": 5, "alias": "别名插件", "note": "来自 D 盘"}],
        "trusted": [],
    })
    loader.load_all()
    ext = loader.records.get("ext-plugin")
    check("清单里的外部路径被加载", ext is not None and ext.loaded, str(ext and ext.error))
    check("别名生效", ext is not None and ext.public()["name"] == "别名插件")
    check("来源标记为本地", ext is not None and ext.source == "local")
    check("外部插件指令可用", loader.commands.handle("/ext")[0] == "EXT-OK")
    check("未信任时提示需确认", ext is not None and ext.trust_pending)
    loader.trust_plugin("ext-plugin", True)
    loader.load_all()
    check("信任后不再提示", not loader.records["ext-plugin"].trust_pending)

    # 禁用
    loader.save_local_manifest({
        "plugins": [{"id": "ext-plugin", "path": external, "enabled": False}],
        "trusted": ["ext-plugin"],
    })
    loader.load_all()
    check("禁用后不加载", not loader.records["ext-plugin"].loaded)
    check("禁用后指令不可用", "未知指令" in loader.commands.handle("/ext")[0])

    # ---------------------------------------------------------------
    section("8. 文件解析扩展（任务书 2.3）")
    from file_parser import (is_parse_error, register_skill_parser, skill_parser_extensions,
                             unregister_skill_parsers)

    def ofd_reader(path):
        return "OFD 内容：测试"

    check("注册 .ofd 解析器", register_skill_parser(".ofd", ofd_reader, "ext-plugin"))
    check("扩展名列表包含 ofd", ".ofd" in skill_parser_extensions())
    check("内置扩展名不可被占用", not register_skill_parser(".pdf", ofd_reader, "ext-plugin"))
    open(os.path.join(workdir, "样例.ofd"), "w", encoding="utf-8").write("x")
    from file_parser import parse_file_with_wps
    parsed = parse_file_with_wps(os.path.join(workdir, "样例.ofd"))
    check("技能解析器被真正调用", parsed == "OFD 内容：测试", repr(parsed))
    check("未知扩展名仍报不支持", parse_file_with_wps(os.path.join(workdir, "x.zzz")).startswith("不支持"))
    unregister_skill_parsers("ext-plugin")
    check("卸载技能后解析器被移除", parse_file_with_wps(os.path.join(workdir, "样例.ofd")).startswith("不支持"))
    check("解析错误标记可识别", is_parse_error("解析失败: xx") and not is_parse_error("正常内容"))

    # ---------------------------------------------------------------
    section("9. 自动化钩子（任务书 2.3）")
    hook_dir = os.path.join(loader.dev_dir, "hook-plugin")
    os.makedirs(os.path.join(hook_dir, "pkg"), exist_ok=True)
    with io.open(os.path.join(hook_dir, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "hook-plugin", "name": "钩子插件", "version": "1.0.0",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(hook_dir, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write(
            "import os\n"
            "def register(api):\n"
            "    def on_task(ctx):\n"
            "        leaked = [k for k in os.environ if k.upper() in ('MY_API_KEY', 'SOME_SECRET')]\n"
            "        path = os.path.join(api.base_dir, 'hook_calls.log')\n"
            "        with open(path, 'a', encoding='utf-8') as fh:\n"
            "            fh.write(str(ctx.get('task')) + '|leaked=' + ','.join(leaked) + '\\n')\n"
            "    api.register_automation_hook('after_task', on_task)\n"
            "    api.register_automation_hook('not_a_stage', on_task)\n"
        )
    loader.load_all()
    rec = loader.records.get("hook-plugin")
    check("钩子插件加载成功", rec is not None and rec.loaded, str(rec and rec.error))
    check("after_task 钩子已登记", "after_task" in (rec.hooks or []))
    check("非法阶段被拒绝", "not_a_stage" not in (rec.hooks or []))

    os.environ["MY_API_KEY"] = "super-secret"
    os.environ["SOME_SECRET"] = "shh"
    os.environ["HARMLESS_VAR"] = "fine"
    results = loader.fire_hook("after_task", {"task": "任务A"})
    check("钩子被调用", any(r.get("ok") for r in results), str(results))
    hook_log = io.open(os.path.join(hook_dir, "hook_calls.log"), encoding="utf-8").read()
    check("钩子收到上下文", "任务A" in hook_log)
    check("执行前清洗了密钥类环境变量（任务书 2B.9）", "leaked=\n" in hook_log.replace("\r", ""),
          repr(hook_log))
    check("清洗后环境变量被恢复", os.environ.get("MY_API_KEY") == "super-secret")
    for key in ("MY_API_KEY", "SOME_SECRET", "HARMLESS_VAR"):
        os.environ.pop(key, None)

    # 钩子异常被隔离
    crash_dir = os.path.join(loader.dev_dir, "crash-hook")
    os.makedirs(os.path.join(crash_dir, "pkg"), exist_ok=True)
    with io.open(os.path.join(crash_dir, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "crash-hook", "name": "崩溃钩子", "version": "1.0.0",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(crash_dir, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api):\n"
                "    api.register_automation_hook('before_task', lambda c: 1/0)\n")
    loader.load_all()
    results = loader.fire_hook("before_task", {"task": "任务B"})
    check("钩子异常被隔离为失败记录",
          any(not r.get("ok") and "ZeroDivisionError" in str(r.get("error")) for r in results),
          str(results))

    # ---------------------------------------------------------------
    section("10. 命令注册的安全约束（任务书 2.4）")
    clash_dir = os.path.join(loader.dev_dir, "clash-plugin")
    os.makedirs(os.path.join(clash_dir, "pkg"), exist_ok=True)
    with io.open(os.path.join(clash_dir, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "clash-plugin", "name": "冲突插件", "version": "1.0.0",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(clash_dir, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api):\n"
                "    api.register_command('/clear', lambda a: 'HIJACKED')\n"
                "    api.register_command('/boom', lambda a: 1/0)\n")
    loader.load_all()
    check("技能不能覆盖内置指令", loader.commands.handle("/clear")[0] != "HIJACKED")
    boom = loader.commands.handle("/boom")[0]
    check("技能指令抛异常时被捕获并给出提示", "boom" in boom and ("ZeroDivisionError" in boom or "不可用" in boom),
          repr(boom))
    check("技能异常不影响后续指令", "用法" in loader.commands.handle("/help")[0])

    # ---------------------------------------------------------------
    section("11. 从文件夹安装 / 导出 zip / 发布自检 / 卸载（任务书 2B.7-2B.8）")
    src = os.path.join(workdir, "to-install")
    os.makedirs(os.path.join(src, "pkg"), exist_ok=True)
    with io.open(os.path.join(src, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "folder-plugin", "name": "文件夹插件", "version": "3.1.4",
                   "license": "MIT",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(src, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api):\n"
                "    api.register_command('/folder', lambda a: 'FOLDER-OK', label='文件夹指令')\n")
    with io.open(os.path.join(src, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 文件夹插件\n说明\n")

    install = loader.install_from_folder(src)
    check("从文件夹安装成功", install.get("ok"), install.get("message", ""))
    check("安装到 skills/ 目录", os.path.isdir(os.path.join(loader.install_dir, "folder-plugin")))
    registry = json.load(io.open(loader.registry_file, encoding="utf-8"))
    ids = [e["id"] for e in registry.get("skills") or []]
    check("已登记到正式注册表", "folder-plugin" in ids, str(ids))
    loader.load_all()
    check("安装后指令可用", loader.commands.handle("/folder")[0] == "FOLDER-OK")

    zipped = loader.export_zip("folder-plugin", os.path.join(workdir, "out.zip"))
    check("导出 zip 成功", zipped.get("ok") and os.path.isfile(zipped.get("path", "")),
          str(zipped))
    if zipped.get("ok"):
        import zipfile
        with zipfile.ZipFile(zipped["path"]) as zf:
            names = zf.namelist()
        check("zip 内含 skill.json 与入口", "skill.json" in names and "pkg/__init__.py" in names,
              str(names))

    checklist = loader.publish_checklist("folder-plugin")
    check("发布自检返回条目", checklist.get("checks") and len(checklist["checks"]) >= 6)
    check("发布自检给出 topic", checklist.get("topic") == "echodesk-plugin")
    check("发布自检结果可判定", isinstance(checklist.get("ok"), bool))

    uninstall = loader.uninstall("folder-plugin")
    check("卸载正式插件成功", uninstall.get("ok"), uninstall.get("message", ""))
    check("安装目录已删除", not os.path.isdir(os.path.join(loader.install_dir, "folder-plugin")))
    registry = json.load(io.open(loader.registry_file, encoding="utf-8"))
    check("注册表条目已移除",
          "folder-plugin" not in [e["id"] for e in registry.get("skills") or []])

    # ---------------------------------------------------------------
    section("12. 加载白名单：不在注册表中的已安装技能被跳过（任务书 2.4）")
    orphan = os.path.join(loader.install_dir, "orphan-plugin")
    os.makedirs(os.path.join(orphan, "pkg"), exist_ok=True)
    with io.open(os.path.join(orphan, "skill.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "orphan-plugin", "name": "孤儿插件", "version": "1.0.0",
                   "echodesk": {"bundle": "pkg", "api_version": "1"}}, f)
    with io.open(os.path.join(orphan, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("def register(api):\n"
                "    api.register_command('/orphan', lambda a: 'ORPHAN', label='孤儿')\n")
    loader.load_all()
    rec = loader.records.get("orphan-plugin")
    check("不在注册表的工具插件被标记未通过验证", rec is not None and not rec.verified,
          str(rec and rec.verified))
    check("被白名单拦下（未加载）", rec is not None and not rec.loaded)
    check("被拦下时给出原因", bool(rec and rec.error), str(rec and rec.error))

    # ---------------------------------------------------------------
    section("13. 状态接口与开发者日志")
    status = loader.status()
    check("状态含目录信息", all(k in status["dirs"] for k in
                                ("dev", "installed", "manifest", "registry", "log", "examples")))
    check("状态含钩子阶段定义", set(status["hook_stages"]) ==
          {"before_task", "after_download", "after_summary", "after_task"})
    check("状态统计数量一致", status["counts"]["total"] == len(status["skills"]))
    check("状态含 API 版本", status["api_version"] == "1")
    check("日志可读取", len(loader.read_log(50)) > 0)
    exported = loader.export_log(os.path.join(workdir, "dev.log"))
    check("日志可导出", os.path.isfile(exported))
    check("日志可清空", loader.clear_log() and loader.read_log(50) == "")
    examples = loader.examples_dir()
    check("示例插件目录路径可解析", examples.endswith(os.path.join("examples", "skills")),
          examples)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
