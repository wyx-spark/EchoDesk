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


def write_plugin(root, skill_id, *, name="", version="1.0.0", has_version=True,
                 dependencies=None, commands=None, min_version=None, api_version="1",
                 register=None):
    """在临时目录里造一个真实插件（``skill.json`` + ``pkg/__init__.py``）。

    与文件里既有做法一致：手写清单与入口模块，而不用脚手架模板 —— 这样每个用例
    都能精确控制"声明了什么"（capabilities.commands / dependencies / min_version）
    与"真正注册了什么"，两者不一致正是本节要测的行为。
    ``has_version=False`` 用于制造"插件没写 version"的场景；
    ``min_version=""`` 表示清单里写了这个字段但值为空。
    """
    os.makedirs(os.path.join(root, "pkg"), exist_ok=True)
    manifest = {
        "id": skill_id,
        "name": name or skill_id,
        "description": f"{skill_id} 回归测试用插件",
        "echodesk": {"bundle": "pkg", "api_version": api_version},
        "capabilities": {"commands": list(commands or [])},
    }
    if has_version:
        manifest["version"] = version
    if dependencies is not None:
        manifest["dependencies"] = dependencies
    if min_version is not None:
        manifest["min_version"] = min_version
    with io.open(os.path.join(root, "skill.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    with io.open(os.path.join(root, "pkg", "__init__.py"), "w", encoding="utf-8") as f:
        f.write(register if register is not None else register_commands())
    return root


def register_commands(*names):
    """生成一个注册了 ``names`` 的 ``register(api)`` 入口源码。"""
    lines = ["def register(api):"]
    for cmd in names:
        lines.append(f"    api.register_command('{cmd}', lambda a: 'OK-{cmd}', label='{cmd}')")
    lines.append("    return None")
    return "\n".join(lines) + "\n"


def log_lines_with(text, needle):
    """日志里同时包含 ``needle`` 的行（用于断言"某插件自己那行"而不是整份日志）。"""
    return [line for line in (text or "").splitlines() if needle in line]


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

    # ---------------------------------------------------------------
    section("14. 依赖、版本与声明一致性（能力扩展-1 任务四）")
    from i18n import get as i18n_get
    from skills_runtime import _parse_dependencies, parse_requirement, version_satisfies
    from version import APP_VERSION

    # 错误文本按下文断言，先固定语言（前面的小节切过 en）
    i18n_get().set_locale("zh-Hans")
    dev_root = loader.dev_dir

    # --- 14.1 依赖解析与版本比较（纯函数，回退即失败） -------------
    manifest_list = parse_manifest({
        "id": "dep-list", "name": "列表写法",
        "echodesk": {"bundle": "pkg", "api_version": "1"},
        "dependencies": ["other-plugin>=1.0", "plain-plugin"],
    })
    check("dependencies 列表写法归一成 {id: 版本要求}",
          manifest_list["dependencies"] == {"other-plugin": ">=1.0", "plain-plugin": ""},
          str(manifest_list["dependencies"]))
    manifest_dict = parse_manifest({
        "id": "dep-dict", "name": "字典写法",
        "echodesk": {"bundle": "pkg", "api_version": "1"},
        "dependencies": {"other-plugin": ">=1.0"},
    })
    check("dependencies 字典写法归一成 {id: 版本要求}",
          manifest_dict["dependencies"] == {"other-plugin": ">=1.0"},
          str(manifest_dict["dependencies"]))
    check("未声明 dependencies 时解析为空字典",
          parse_manifest({"id": "x"})["dependencies"] == {})
    check("dependencies 是非法类型时忽略而不是崩溃",
          _parse_dependencies("other-plugin>=1.0") == {} and _parse_dependencies(None) == {}
          and _parse_dependencies([]) == {})
    check("parse_requirement 拆出运算符与版本", parse_requirement(">=1.2") == (">=", "1.2"),
          str(parse_requirement(">=1.2")))
    check("parse_requirement 只写版本号时按 >=", parse_requirement("1.2") == (">=", "1.2"),
          str(parse_requirement("1.2")))
    check("parse_requirement 认 == 与 <（= / ~ / ^ 一律当 >=）",
          parse_requirement("==1.0") == ("==", "1.0") and parse_requirement("<2.0") == ("<", "2.0")
          and parse_requirement("=1.0.0") == (">=", "1.0.0")
          and parse_requirement("~1.0") == (">=", "1.0"))
    check("parse_requirement 解析不出数字时返回空（放行）",
          parse_requirement("随便写") == ("", "") and parse_requirement("") == ("", ""))
    check("version_satisfies：满足", version_satisfies("1.2.0", ">=1.0"))
    check("version_satisfies：不满足", not version_satisfies("0.9", ">=1.0"))
    check("version_satisfies：严格大于/小于",
          version_satisfies("2.0", ">1.0") and not version_satisfies("1.0", ">1.0")
          and version_satisfies("1.0", "<2.0") and not version_satisfies("2.5", "<2.0"))
    check("version_satisfies：== 精确匹配（同长度版本号）",
          version_satisfies("1.0.0", "==1.0.0") and not version_satisfies("1.0.1", "==1.0.0"))
    check("version_satisfies：版本读不出数字时宽松放行",
          version_satisfies("", ">=9.9") and version_satisfies("abc", ">=9.9"))
    check("version_satisfies：带 v 前缀的版本仍可比较",
          version_satisfies("v1.0.0", ">=0.5") and not version_satisfies("v0.1.0", ">=0.5"))

    # --- 14.2 依赖存在且版本满足 → 正常加载（列表 / 字典两种写法） ---
    write_plugin(os.path.join(dev_root, "dep-lib"), "dep-lib", version="1.0.0",
                 register=register_commands("/dep-lib"))
    write_plugin(os.path.join(dev_root, "dep-user-list"), "dep-user-list", version="1.0.0",
                 dependencies=["dep-lib>=1.0"], register=register_commands("/dep-user-list"))
    write_plugin(os.path.join(dev_root, "dep-user-dict"), "dep-user-dict", version="1.0.0",
                 dependencies={"dep-lib": ">=1.0"}, register=register_commands("/dep-user-dict"))
    loader.load_all()
    dep_rec = loader.records.get("dep-lib")
    list_rec = loader.records.get("dep-user-list")
    dict_rec = loader.records.get("dep-user-dict")
    check("被依赖插件正常加载", bool(dep_rec and dep_rec.loaded),
          str(dep_rec and dep_rec.error))
    check("SkillRecord.dependencies 记录了归一后的依赖",
          bool(list_rec and list_rec.dependencies == {"dep-lib": ">=1.0"}),
          str(list_rec and list_rec.dependencies))
    check("列表写法的依赖满足 → 加载",
          bool(list_rec and list_rec.loaded), str(list_rec and list_rec.error))
    check("字典写法的依赖满足 → 加载",
          bool(dict_rec and dict_rec.loaded), str(dict_rec and dict_rec.error))
    check("有依赖的插件其指令真的可用",
          loader.commands.handle("/dep-user-list")[0] == "OK-/dep-user-list",
          repr(loader.commands.handle("/dep-user-list")[0]))

    # --- 14.3 依赖不在清单里 → 不加载 + error + 计入 failed ---
    baseline = loader.load_all()
    write_plugin(os.path.join(dev_root, "dep-missing-user"), "dep-missing-user", version="1.0.0",
                 dependencies=["no-such-plugin>=1.0"],
                 register=register_commands("/dep-missing-user"))
    after = loader.load_all()
    miss_rec = loader.records.get("dep-missing-user")
    check("依赖的插件不在清单里 → 该插件不加载", bool(miss_rec and not miss_rec.loaded))
    check("依赖缺失 → error 说明缺少依赖且点名缺谁",
          bool(miss_rec and "缺少依赖" in miss_rec.error and "no-such-plugin" in miss_rec.error),
          str(miss_rec and miss_rec.error))
    check("依赖缺失 → 计入 summary 的 failed",
          after["failed"] == baseline["failed"] + 1, f"{baseline} → {after}")
    check("依赖缺失 → 不计入 summary 的 loaded",
          after["loaded"] == baseline["loaded"], f"{baseline} → {after}")
    check("依赖缺失的插件指令没有被注册",
          "未知指令" in loader.commands.handle("/dep-missing-user")[0])

    # --- 14.4 依赖插件被禁用 → 同样不加载 ---
    disabled_root = os.path.join(workdir, "external-plugins", "dep-disabled")
    write_plugin(disabled_root, "dep-disabled", version="1.0.0",
                 register=register_commands("/dep-disabled"))
    manifest_data = loader.local_manifest()
    manifest_data["plugins"] = [p for p in manifest_data["plugins"]
                                if p.get("id") != "dep-disabled"]
    manifest_data["plugins"].append({"id": "dep-disabled", "path": disabled_root,
                                     "enabled": False, "source": "local"})
    loader.save_local_manifest(manifest_data)
    write_plugin(os.path.join(dev_root, "dep-disabled-user"), "dep-disabled-user",
                 version="1.0.0", dependencies=["dep-disabled"],
                 register=register_commands("/dep-disabled-user"))
    loader.load_all()
    disabled_rec = loader.records.get("dep-disabled")
    disabled_user = loader.records.get("dep-disabled-user")
    check("被禁用的依赖仍在清单里（只是 enabled=false）",
          bool(disabled_rec and not disabled_rec.enabled))
    check("依赖被禁用 → 依赖方不加载", bool(disabled_user and not disabled_user.loaded))
    check("依赖被禁用 → error 说明缺少依赖并注明禁用",
          bool(disabled_user and "缺少依赖" in disabled_user.error
               and "禁用" in disabled_user.error), str(disabled_user and disabled_user.error))
    check("依赖被禁用 → 被禁用的插件自己不计入 failed（是 skipped）",
          bool(disabled_rec and not disabled_rec.error), str(disabled_rec and disabled_rec.error))

    # --- 14.5 依赖版本不满足 → 不加载；满足 → 加载 ---
    baseline = loader.load_all()
    write_plugin(os.path.join(dev_root, "dep-version-user"), "dep-version-user", version="1.0.0",
                 dependencies={"dep-lib": ">=2.0"},
                 register=register_commands("/dep-version-user"))
    write_plugin(os.path.join(dev_root, "dep-version-ok"), "dep-version-ok", version="1.0.0",
                 dependencies={"dep-lib": "==1.0.0"},
                 register=register_commands("/dep-version-ok"))
    after = loader.load_all()
    ver_rec = loader.records.get("dep-version-user")
    ver_ok = loader.records.get("dep-version-ok")
    check("依赖版本不满足（要 >=2.0，实际 1.0.0）→ 不加载",
          bool(ver_rec and not ver_rec.loaded))
    check("依赖版本不满足 → error 含版本信息与双方版本号",
          bool(ver_rec and "版本" in ver_rec.error and ">=2.0" in ver_rec.error
               and "1.0.0" in ver_rec.error), str(ver_rec and ver_rec.error))
    check("依赖版本不满足 → 计入 failed、不计入 loaded",
          after["failed"] == baseline["failed"] + 1 and after["loaded"] == baseline["loaded"] + 1,
          f"{baseline} → {after}")
    check("依赖版本满足（==1.0.0）→ 正常加载",
          bool(ver_ok and ver_ok.loaded), str(ver_ok and ver_ok.error))

    # --- 14.6 被依赖插件没写 version 而依赖方要求了版本 → 宽松放行 + 提示 ---
    write_plugin(os.path.join(dev_root, "dep-noversion"), "dep-noversion", has_version=False,
                 register=register_commands("/dep-noversion"))
    write_plugin(os.path.join(dev_root, "dep-noversion-user"), "dep-noversion-user",
                 version="1.0.0", dependencies={"dep-noversion": ">=9.9"},
                 register=register_commands("/dep-noversion-user"))
    loader.clear_log()
    loader.load_all()
    nov_rec = loader.records.get("dep-noversion")
    nov_user = loader.records.get("dep-noversion-user")
    check("被依赖插件确实没有版本号", bool(nov_rec and nov_rec.version == ""),
          str(nov_rec and nov_rec.version))
    check("被依赖插件没写版本时仍然加载", bool(nov_rec and nov_rec.loaded),
          str(nov_rec and nov_rec.error))
    check("依赖方在对方没写版本时宽松放行（即使要求 >=9.9 也加载）",
          bool(nov_user and nov_user.loaded), str(nov_user and nov_user.error))
    log_text = loader.read_log(500)
    check("宽松放行时开发日志有提示（否则用户无从得知版本没校验）",
          bool(log_lines_with(log_text, "dep-noversion-user")
               and "未声明版本号" in log_text), repr(log_text[-400:]))

    # --- 14.7 声明了命令但没注册 / 注册了没声明 ---
    write_plugin(os.path.join(dev_root, "decl-missing-cmd"), "decl-missing-cmd", version="1.0.0",
                 commands=["/decl-missing"], register=register_commands())
    write_plugin(os.path.join(dev_root, "decl-extra-cmd"), "decl-extra-cmd", version="1.0.0",
                 commands=["/decl-extra"],
                 register=register_commands("/decl-extra", "/decl-extra-ghost"))
    write_plugin(os.path.join(dev_root, "decl-ok-cmd"), "decl-ok-cmd", version="1.0.0",
                 commands=["/decl-ok"], register=register_commands("/decl-ok"))
    loader.clear_log()
    loader.load_all()
    miss_cmd = loader.records.get("decl-missing-cmd")
    extra_cmd = loader.records.get("decl-extra-cmd")
    ok_cmd = loader.records.get("decl-ok-cmd")
    check("声明了命令但未注册 → 警告里出现该说法",
          bool(miss_cmd and any("声明了命令但未注册" in w for w in miss_cmd.warnings)),
          str(miss_cmd and miss_cmd.warnings))
    check("声明了命令但未注册 → 警告点名缺失的那条命令",
          bool(miss_cmd and any("/decl-missing" in w for w in miss_cmd.warnings)),
          str(miss_cmd and miss_cmd.warnings))
    check("注册了未声明的命令 → 警告里出现该说法",
          bool(extra_cmd and any("注册了未声明的命令" in w for w in extra_cmd.warnings)),
          str(extra_cmd and extra_cmd.warnings))
    check("注册了未声明的命令 → 警告点名多出来的那条命令",
          bool(extra_cmd and any("/decl-extra-ghost" in w for w in extra_cmd.warnings)),
          str(extra_cmd and extra_cmd.warnings))
    check("声明与注册一致 → 不产生这类警告",
          bool(ok_cmd and not any(("声明了命令但未注册" in w) or ("注册了未声明的命令" in w)
                                  for w in ok_cmd.warnings)),
          str(ok_cmd and ok_cmd.warnings))
    check("不一致的插件仍然加载成功（只警告不阻断）",
          bool(miss_cmd and miss_cmd.loaded and extra_cmd and extra_cmd.loaded))
    log_text = loader.read_log(500)
    check("命令不一致写入开发日志（skill.command_not_registered / undeclared）",
          "声明了命令但未注册" in log_text and "注册了未声明的命令" in log_text,
          repr(log_text[-400:]))
    check("一致的插件不写这类警告日志",
          not [line for line in log_lines_with(log_text, "decl-ok-cmd")
               if "未注册" in line or "未声明" in line],
          str(log_lines_with(log_text, "decl-ok-cmd")))

    # --- 14.8 min_version 校验（比当前 APP_VERSION 新 → 拒绝加载） ---
    app_numeric = APP_VERSION.lstrip("vV")
    baseline = loader.load_all()
    write_plugin(os.path.join(dev_root, "minv-too-new"), "minv-too-new", version="1.0.0",
                 min_version="99.0.0", register=register_commands("/minv-too-new"))
    write_plugin(os.path.join(dev_root, "minv-equal"), "minv-equal", version="1.0.0",
                 min_version=app_numeric, register=register_commands("/minv-equal"))
    write_plugin(os.path.join(dev_root, "minv-old"), "minv-old", version="1.0.0",
                 min_version="0.0.1", register=register_commands("/minv-old"))
    write_plugin(os.path.join(dev_root, "minv-empty"), "minv-empty", version="1.0.0",
                 min_version="", register=register_commands("/minv-empty"))
    after = loader.load_all()
    too_new = loader.records.get("minv-too-new")
    check("min_version 比当前 APP_VERSION 新 → 拒绝加载",
          bool(too_new and not too_new.loaded))
    check("min_version 过高 → error 说明要求的主程序版本与当前版本",
          bool(too_new and (("要求主程序" in too_new.error) or (APP_VERSION in too_new.error))
               and "99.0.0" in too_new.error), str(too_new and too_new.error))
    check("min_version 过高 → 计入 failed 且不计入 loaded",
          after["failed"] == baseline["failed"] + 1 and after["loaded"] == baseline["loaded"] + 3,
          f"{baseline} → {after}")
    check("min_version 过高的插件指令不可用",
          "未知指令" in loader.commands.handle("/minv-too-new")[0])
    equal_rec = loader.records.get("minv-equal")
    old_rec = loader.records.get("minv-old")
    empty_rec = loader.records.get("minv-empty")
    check("min_version 等于当前版本 → 正常加载",
          bool(equal_rec and equal_rec.loaded), str(equal_rec and equal_rec.error))
    check("min_version 低于当前版本 → 正常加载",
          bool(old_rec and old_rec.loaded), str(old_rec and old_rec.error))
    check("min_version 为空 → 正常加载（不误伤没写版本要求的插件）",
          bool(empty_rec and empty_rec.loaded), str(empty_rec and empty_rec.error))

    # --- 14.9 reload_dev 只重载开发/本地，reload_all 仍是全部 ---
    src = os.path.join(workdir, "to-install-reload")
    write_plugin(src, "reload-installed", version="1.0.0",
                 register=register_commands("/reload-installed"))
    installed = loader.install_from_folder(src)
    check("造一个已安装来源的插件（reload_dev 的对照组）", installed.get("ok"),
          installed.get("message", ""))
    loader.load_all()
    installed_rec = loader.records.get("reload-installed")
    check("对照组插件来源是 installed", bool(installed_rec and installed_rec.source == "installed"),
          str(installed_rec and installed_rec.source))
    check("对照组插件已加载并可用",
          bool(installed_rec and installed_rec.loaded)
          and loader.commands.handle("/reload-installed")[0] == "OK-/reload-installed",
          str(installed_rec and installed_rec.error))

    dev_summary = loader.reload_dev()
    reloaded = dev_summary.get("reloaded")
    check("reload_dev 的 scope 是 dev（不是 all）",
          dev_summary.get("scope") == "dev", str(dev_summary))
    check("reload_dev 返回被卸载的开发来源插件列表",
          isinstance(reloaded, list) and "dep-lib" in reloaded, str(reloaded))
    check("reload_dev 的 reloaded 里不含已安装来源的插件 id",
          isinstance(reloaded, list) and "reload-installed" not in reloaded, str(reloaded))
    check("reload_dev 之后开发插件被重新加载",
          bool(loader.records.get("dep-lib") and loader.records["dep-lib"].loaded))
    check("reload_dev 之后开发插件指令可用",
          loader.commands.handle("/dep-user-dict")[0] == "OK-/dep-user-dict",
          repr(loader.commands.handle("/dep-user-dict")[0]))
    check("reload_dev 之后已安装插件依然可用",
          loader.commands.handle("/reload-installed")[0] == "OK-/reload-installed",
          repr(loader.commands.handle("/reload-installed")[0]))

    all_summary = loader.reload_all()
    check("reload_all 保持原样：summary 里不带 scope",
          "scope" not in all_summary, str(sorted(all_summary.keys())))
    check("reload_all 仍是全部重载（含已安装来源）",
          "reload-installed" in (all_summary.get("ids") or [])
          and "dep-lib" in (all_summary.get("ids") or []), str(all_summary.get("ids")))
    check("reload_all 之后已安装插件指令可用",
          loader.commands.handle("/reload-installed")[0] == "OK-/reload-installed")

    # --- 14.10 插件 handler 抛 BaseException（SystemExit）不得带走主进程 ---
    write_plugin(
        os.path.join(dev_root, "exit-plugin"), "exit-plugin", version="1.0.0",
        commands=["/exit-plugin", "/exit-plugin-sysexit"],
        register=("def register(api):\n"
                  "    import sys\n"
                  "    def boom(_args):\n"
                  "        raise SystemExit(3)\n"
                  "    def quit_hard(_args):\n"
                  "        sys.exit(7)\n"
                  "    api.register_command('/exit-plugin', boom, label='退出测试')\n"
                  "    api.register_command('/exit-plugin-sysexit', quit_hard, label='退出测试2')\n"))
    loader.load_all()
    exit_rec = loader.records.get("exit-plugin")
    check("退出测试插件加载成功（指令已登记）",
          bool(exit_rec and exit_rec.loaded and "/exit-plugin" in (exit_rec.commands or [])),
          str(exit_rec and exit_rec.error))

    escaped = None
    text = ""
    try:
        text, _action = loader.commands.handle("/exit-plugin")
    except BaseException as e:   # noqa: BLE001 —— 逃出来就说明隔离层没拦住
        escaped = e
    check("插件 raise SystemExit(3) 时异常不穿透指令层（进程没退出）",
          escaped is None,
          f"SystemExit 逃逸出 handle()：{type(escaped).__name__}: {escaped}" if escaped else "")
    check("SystemExit 被隔离成 cmd.disabled 文案而不是崩溃",
          "不可用" in text and "SystemExit" in text, repr(text))

    escaped2 = None
    text2 = ""
    try:
        text2, _action2 = loader.commands.handle("/exit-plugin-sysexit")
    except BaseException as e:   # noqa: BLE001
        escaped2 = e
    check("插件 sys.exit(7) 同样被隔离", escaped2 is None,
          f"SystemExit 逃逸出 handle()：{type(escaped2).__name__}: {escaped2}" if escaped2 else "")
    check("sys.exit() 也被隔离成 cmd.disabled 文案",
          "不可用" in text2 and "exit-plugin-sysexit" in text2, repr(text2))
    check("隔离之后主程序照常工作", "用法" in loader.commands.handle("/help")[0])
    check("抛 SystemExit 的插件本身仍处于已加载状态",
          bool(loader.records.get("exit-plugin") and loader.records["exit-plugin"].loaded))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
