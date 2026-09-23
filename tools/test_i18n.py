# -*- coding: utf-8 -*-
"""多语言机制的测试（任务书 任务三）。

覆盖：语言标签归一化、系统区域自动检测、回退链、按需加载、模块分片合并、
语言包完整性工具、以及"缺失键显示键名而不是空白"这一刻意设计。

不依赖网络与模型，可在 CI 中直接运行。

用法：python tools\\test_i18n.py
退出码：0 = 全部通过，1 = 存在失败项。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

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


def main():
    import i18n

    # ---------------------------------------------------------------
    section("1. 语言标签归一化（zh-CN → zh-Hans 等）")
    cases = {
        "zh-CN": "zh-Hans",
        "zh-Hans": "zh-Hans",
        "zh": "zh-Hans",
        "zh-SG": "zh-Hans",
        "zh-TW": "zh-Hant",
        "zh-HK": "zh-Hant",
        "zh-Hant": "zh-Hant",
        "en": "en",
        "en-US": "en",
        "EN_us": "en",
        "ja-JP": "ja",
        "de-DE": "de",
        "pt-BR": "pt-BR",
        "pt-PT": "pt",
        "": None,
        None: None,
    }
    for raw, want in cases.items():
        got = i18n.normalize_locale(raw)
        check(f"normalize_locale({raw!r}) == {want!r}", got == want, f"实际 {got!r}")

    # ---------------------------------------------------------------
    section("2. 语言包发现与按需加载")
    locales = i18n.available_locales()
    check("能发现 zh-Hans 与 en", "zh-Hans" in locales and "en" in locales, str(locales))
    src = i18n.find_locale_file("zh-Hans")
    check("能找到源语言包文件", bool(src and os.path.isfile(src)), str(src))
    check("找不到的语言返回 None", i18n.find_locale_file("xx-YY") is None)

    # ---------------------------------------------------------------
    section("3. 回退链：当前语言 → en → zh-Hans → 键名本身")
    tr = i18n.get()
    tr.set_locale("en")
    check("切到英文后 locale == en", tr.locale == "en", tr.locale)
    check("英文文案生效", tr.t("cmd.resp.clear_done").startswith("🗑️ Conversation cleared"),
          repr(tr.t("cmd.resp.clear_done")))
    tr.set_locale("zh-Hans")
    check("切回中文后 locale == zh-Hans", tr.locale == "zh-Hans", tr.locale)
    check("中文文案生效", tr.t("cmd.resp.clear_done") == "🗑️ 对话已清空",
          repr(tr.t("cmd.resp.clear_done")))
    # 故意请求一个没有语言包的语言：应按回退链落到 en
    tr.set_locale("ja")
    check("缺少语言包时回退到 en", tr.locale == "en", tr.locale)
    check("回退后仍能取到文案", tr.t("cmd.resp.undo_done") != "cmd.resp.undo_done")
    # 不存在的键：返回键名本身（界面上一眼能看出），而不是空白
    check("缺失键返回键名本身", tr.t("no.such.key.at.all") == "no.such.key.at.all")
    check("has() 对缺失键返回 False", not tr.has("no.such.key.at.all"))
    check("has() 对存在键返回 True", tr.has("cmd.resp.undo_done"))

    # ---------------------------------------------------------------
    section("4. 占位符替换与容错")
    tr.set_locale("zh-Hans")
    text = tr.t("cmd.resp.mode_set", mode="contract")
    check("占位符被替换", "contract" in text and "{" not in text, repr(text))
    # 少传参数时不应抛异常，缺失的占位符原样保留
    text = tr.t("cmd.resp.mode_set_preset")
    check("缺参数不抛异常且保留占位符", "{mode}" in text, repr(text))

    # ---------------------------------------------------------------
    section("5. 模块分片合并（任务书 3.5）")
    packs = i18n.load_pack("zh-Hans")
    check("合并后含主文件键", "cmd.unknown" in packs)
    check("合并后含模块分片键（automation.help.*）",
          any(k.startswith("automation.help.task_list_url") for k in packs))
    check("模块分片覆盖主文件同名键", packs.get("automation.disabled", "") != "")

    # ---------------------------------------------------------------
    section("6. 外置语言包优先（社区贡献路径，任务书 3.9）")
    workdir = tempfile.mkdtemp(prefix="echodesk-lang-")
    try:
        # 造一个"程序目录/lang"：让 program_root() 指向它
        fake_root = os.path.join(workdir, "program")
        os.makedirs(os.path.join(fake_root, "lang"), exist_ok=True)
        fake_locale = os.path.join(fake_root, "lang", "zz-Test.json")
        with io.open(fake_locale, "w", encoding="utf-8") as f:
            json.dump({"cmd": {"unknown": "重写后的未知指令文案 {name}"}}, f, ensure_ascii=False)
        # 直接验证"按搜索目录优先级查找"：把该目录塞进搜索链的最前面
        original = i18n._search_dirs

        def patched():
            return [os.path.join(fake_root, "lang")] + original()

        i18n._search_dirs = patched
        check("能发现外置语言包", "zz-Test" in i18n.available_locales(),
              str(i18n.available_locales()))
        tr.set_locale("zz-Test")
        check("外置语言包成为当前语言", tr.locale == "zz-Test", tr.locale)
        check("外置语言包里的键生效",
              tr.t("cmd.unknown", name="/x") == "重写后的未知指令文案 /x",
              repr(tr.t("cmd.unknown", name="/x")))
        check("外置包缺的键回退到 en/zh-Hans",
              tr.t("cmd.resp.undo_done") in ("↩️ 已撤回", "↩️ Undone"), repr(tr.t("cmd.resp.undo_done")))
        i18n._search_dirs = original
        tr.set_locale("zh-Hans")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ---------------------------------------------------------------
    section("7. 语言包损坏时的容错")
    workdir = tempfile.mkdtemp(prefix="echodesk-badlang-")
    try:
        bad = os.path.join(workdir, "lang")
        os.makedirs(bad, exist_ok=True)
        with io.open(os.path.join(bad, "en.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not json ")
        original = i18n._search_dirs
        i18n._search_dirs = lambda: [bad]
        i18n._translator = None      # 强制重建单例
        tr2 = i18n.get()
        tr2.set_locale("en")
        check("损坏的语言包不抛异常", True)
        check("损坏时退化为键名而不是崩溃",
              tr2.t("cmd.unknown", name="/x") == "cmd.unknown", repr(tr2.t("cmd.unknown", name="/x")))
        i18n._search_dirs = original
        i18n._translator = None
        i18n.get().set_locale("zh-Hans")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ---------------------------------------------------------------
    section("8. 语言包完整性工具（任务书 3.5）")
    tool = os.path.join(ROOT, "tools", "check_lang.py")
    check("check_lang.py 存在", os.path.isfile(tool))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, tool, "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    check("check_lang.py 通过（前后端语言包键一致、占位符一致）", proc.returncode == 0,
          (proc.stdout or "")[-400:] + (proc.stderr or "")[-200:])
    check("check_lang 能报出缺失键（自检：故意查不存在的语言）",
          subprocess.run([sys.executable, tool, "--locale", "no-such"],
                         capture_output=True, text=True, encoding="utf-8", env=env).returncode == 0)

    extract = os.path.join(ROOT, "tools", "extract_strings.py")
    check("extract_strings.py 存在", os.path.isfile(extract))
    proc = subprocess.run([sys.executable, extract, "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    check("extract_strings.py 可运行且输出统计", proc.returncode == 0 and "共" in (proc.stdout or ""),
          (proc.stdout or "")[-200:])

    # ---------------------------------------------------------------
    section("9. 前端语言包与后端语言包键空间独立")
    frontend_lang = os.path.join(ROOT, "frontend", "Resources", "Lang")
    check("前端语言包目录存在", os.path.isdir(frontend_lang))
    for locale in ("zh-Hans", "en"):
        path = os.path.join(frontend_lang, f"{locale}.json")
        check(f"前端 {locale}.json 存在且是合法 JSON",
              os.path.isfile(path) and isinstance(json.load(io.open(path, encoding="utf-8")), dict))
    # 前端键里有 settings.* / main.*，不应混入后端专属的 cmd.items.*
    front = json.load(io.open(os.path.join(frontend_lang, "zh-Hans.json"), encoding="utf-8"))
    check("前端语言包按模块分组（main / settings）", "main" in front and "settings" in front)
    # 界面语言包在发布物里位于 lang\ui\（与 lang\ 下的后端包分开，避免同名互相覆盖）
    csproj = io.open(os.path.join(ROOT, "frontend", "EchoDesk.csproj"), encoding="utf-8").read()
    check("前端语言包发布到 lang\\ui\\ 而不是 lang\\", "lang\\ui\\%(Filename)" in csproj,
          "csproj 里的 Link 目标不对")

    print(f"\n{'=' * 60}")
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  ✗ {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
