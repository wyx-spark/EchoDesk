# Hello World —— EchoDesk 示例插件

这是 EchoDesk 的官方示例插件之一，用来演示技能 API 的用法，同时也是
**本地插件**与**市场插件**两种形态的现成范例。

## 它提供什么

| 能力 | 内容 |
| --- | --- |
| 指令 | `/hello [名字]` —— 返回一句问候 |
| UI 面板 | `hello-panel` —— 设置窗口里多出一个 Tab，带一个输入框和一个按钮 |
| 自动化钩子 | `after_task` —— 每个自动化任务结束后，往插件自己的数据目录写一行记录 |
| 配置读写 | 面板里填过的名字保存在本插件目录的 `config.json`（与主配置隔离） |
| 多语言 | `lang/zh-Hans.json` 与 `lang/en.json`，跟随界面语言切换 |

## 三种用法

### 1）作为本地插件（开发调试）

把本目录整个复制到 `backend/skills_dev/` 下，程序启动时就会自动识别：

```powershell
Copy-Item -Recurse .\hello-world "$env:ProgramFiles\EchoDesk\backend\skills_dev\"
```

放进去之后在设置窗口「开发者」页点「🔄 重载本地插件」，再到输入框里敲 `/hello`
即可。开发目录里的插件不写入 `skills_registry.json`，卸载也不影响正式安装记录。

### 2）作为市场插件

本目录本身就是一个合法的技能仓库：

1. 把 `hello-world/` 里的内容作为仓库根目录推到 GitHub（建议仓库名 `echodesk-skill-hello`）；
2. 给仓库打上 topic **`echodesk-plugin`**（仓库页 → ⚙ About → Topics）；
3. 在 EchoDesk 的「技能市场」里刷新即可搜到。

市场只检索 `echodesk-plugin` 这一个 topic，仓库根目录必须有含
`echodesk.bundle` 的 `skill.json`。

### 3）作为 API 参考

`echodesk_hello/__init__.py` 里的 `register(api)` 按六类能力分组注释，
可以直接抄改：

```python
def register(api):
    api.register_command("/hello", handler, label=..., usage=..., description=...)
    api.register_panel("hello-panel", "标题", fields=[...], actions=[...],
                       on_action=..., render=...)
    api.register_automation_hook("after_task", on_task_done)
    api.register_file_parser("ofd", reader)     # 本例未用；见 ofd-reader 示例
    api.get_config() / api.set_config({...})
    api.lang("key", "默认文案", name="世界")
```

## 目录结构

```
hello-world/
├─ skill.json               插件清单：id、入口、能力声明、权限声明
├─ echodesk_hello/
│  └─ __init__.py           入口：register(api)
├─ lang/
│  ├─ zh-Hans.json          插件自己的语言包
│  └─ en.json
└─ README.md                市场卡片会展示本文摘要
```

## 权限声明

`skill.json` 的 `capabilities.permissions` 里声明了本插件用到的东西，供用户在
安装/审查时判断：

```json
{ "network": false, "filesystem": ["skill_dir"] }
```

本示例**不联网**，只读写自己的目录。这是提交到市场时的推荐做法：
权限声明越少，用户越放心。

## 许可证

Apache-2.0，与 EchoDesk 本体一致。

---

# Hello World — an EchoDesk example plugin

This is one of EchoDesk's official example plugins. It shows how the skill API is used, and it is a
ready-made example of both shapes a plugin can take: a **local plugin** and a **marketplace plugin**.

## What it provides

| Capability | Detail |
| --- | --- |
| Command | `/hello [name]` — returns a greeting |
| UI panel | `hello-panel` — the Settings window gains one more tab, with an input box and a button |
| Automation hook | `after_task` — writes one line into the plugin's own data directory after every automation task |
| Configuration read/write | the name typed in the panel is stored in this plugin directory's `config.json` (kept separate from the main configuration) |
| Localisation | `lang/zh-Hans.json` and `lang/en.json`, following the interface language |

## Three ways to use it

### 1) As a local plugin (development and debugging)

Copy this whole directory into `backend/skills_dev/` and the program picks it up at start-up:

```powershell
Copy-Item -Recurse .\hello-world "$env:ProgramFiles\EchoDesk\backend\skills_dev\"
```

Once it is in place, click "🔄 Reload local plugins" on the "Developer" page of the Settings window,
then type `/hello` in the input box. Plugins in the development directory are not written to
`skills_registry.json`, and removing them leaves the official installation records alone.

### 2) As a marketplace plugin

This directory is itself a valid skill repository:

1. Push the contents of `hello-world/` to GitHub as the repository root (the suggested repository
   name is `echodesk-skill-hello`);
2. Give the repository the topic **`echodesk-plugin`** (repository page → ⚙ About → Topics);
3. Refresh the skill marketplace in EchoDesk and it is found.

The marketplace searches only for the one topic `echodesk-plugin`, and the repository root must hold
a `skill.json` containing `echodesk.bundle`.

### 3) As an API reference

`register(api)` in `echodesk_hello/__init__.py` is commented in groups, one per class of capability,
and can be copied and adapted directly:

```python
def register(api):
    api.register_command("/hello", handler, label=..., usage=..., description=...)
    api.register_panel("hello-panel", "标题", fields=[...], actions=[...],
                       on_action=..., render=...)
    api.register_automation_hook("after_task", on_task_done)
    api.register_file_parser("ofd", reader)     # 本例未用；见 ofd-reader 示例
    api.get_config() / api.set_config({...})
    api.lang("key", "默认文案", name="世界")
```

## Directory structure

```
hello-world/
├─ skill.json               插件清单：id、入口、能力声明、权限声明
├─ echodesk_hello/
│  └─ __init__.py           入口：register(api)
├─ lang/
│  ├─ zh-Hans.json          插件自己的语言包
│  └─ en.json
└─ README.md                市场卡片会展示本文摘要
```

## Permission declaration

`capabilities.permissions` in `skill.json` declares what this plugin uses, so that a user can judge
it while installing or reviewing:

```json
{ "network": false, "filesystem": ["skill_dir"] }
```

This example **does not use the network**; it only reads and writes its own directory. That is the
recommended approach when submitting to the marketplace: the fewer permissions declared, the more
comfortable users are.

## Licence

Apache-2.0, the same as EchoDesk itself.
