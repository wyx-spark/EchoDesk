# -*- coding: utf-8 -*-
"""Hello World —— EchoDesk 官方示例插件。

它同时是三类开发者需要的范例：

1. **本地插件范例**：把本目录放到 ``backend/skills_dev/`` 即可被识别与热重载；
2. **市场插件范例**：本目录就是一个合法的技能仓库（根目录有含 ``echodesk.bundle``
   的 ``skill.json``），推到 GitHub 并打上 topic ``echodesk-plugin`` 就能被市场搜到；
3. **API 使用范例**：演示命令注册、UI 面板、自动化钩子、配置读写与多语言。

本插件只依赖标准库，不访问网络、不写技能目录以外的任何文件。

关于 ``api``：它由加载器调用 ``register(api)`` 时注入，是 ``skills_runtime.SkillAPI``
的实例。可用能力见 ``docs/zh-Hans/SKILL_GUIDE.md``「技能 API」一节。
"""
import os
import time


def register(api):
    """加载器调用的注册入口（约定函数名，也可用 setup / register_skill）。"""

    # ------------------------------------------------------------------
    # 1) 注册一个 /hello 指令
    # ------------------------------------------------------------------
    def handle(args):
        """args 是命令名之后的参数列表：/hello 世界 → ["世界"]。"""
        who = " ".join(args).strip() if args else api.lang("hello.default_name", "世界")
        return api.lang(
            "hello.reply",
            "你好，{name}！这是来自插件「{skill}」的问候。\n"
            "当前界面语言：{locale}；EchoDesk 版本：{version}。",
            name=who, skill=api.name, locale=api.locale(), version=api.app_version(),
        )

    api.register_command(
        "/hello", handle,
        label="@cmd.hello.label",
        usage="/hello [名字]",
        description="@cmd.hello.description",
    )

    # ------------------------------------------------------------------
    # 2) 注册一个设置窗口里的面板（前端用原生控件渲染）
    # ------------------------------------------------------------------
    def on_action(panel_id, action, values):
        if action != "greet":
            return ""
        who = str(values.get("name") or "").strip() or api.lang("hello.default_name", "世界")
        # 面板里填过的名字记到插件自己的配置里，下次打开还在
        api.set_config({"last_name": who})
        return api.lang("panel.result", "面板收到名字：{name}", name=who)

    def render():
        cfg = api.get_config()
        last = cfg.get("last_name") or api.lang("panel.none_yet", "（还没有）")
        return api.lang(
            "panel.render",
            "上次打招呼的名字：{last}\n数据目录：{dir}",
            last=last, dir=api.data_dir(),
        )

    api.register_panel(
        "hello-panel",
        "@panel.title",
        description="@panel.description",
        fields=[
            {"key": "name", "type": "text", "label": "@panel.name.label",
             "default": "世界", "help": "@panel.name.help"},
            {"key": "remember", "type": "checkbox", "label": "@panel.remember.label",
             "default": True, "help": "@panel.remember.help"},
        ],
        actions=[{"key": "greet", "label": "@panel.greet.label", "primary": True}],
        on_action=on_action,
        render=render,
    )

    # ------------------------------------------------------------------
    # 3) 挂接自动化钩子：每个任务结束后往插件自己的数据目录写一行记录
    # ------------------------------------------------------------------
    def on_task_done(context):
        line = "{time}\t{task}\t{status}\n".format(
            time=time.strftime("%Y-%m-%d %H:%M:%S"),
            task=context.get("task") or "",
            status=context.get("status") or "",
        )
        path = os.path.join(api.data_dir(), "task_log.tsv")
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            api.warn("无法写入任务日志：" + path)

    api.register_automation_hook("after_task", on_task_done)
