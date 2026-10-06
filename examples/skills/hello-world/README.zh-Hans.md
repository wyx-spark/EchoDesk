# Hello World —— EchoDesk 示例插件

> 本文是 [README.md](README.md) 的中文参考译本。如两者有歧义，**以英文版为准**。

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

把本目录整个复制到后端工作目录下的 `skills_dev/`，程序启动时就会自动识别。
目录名取自 `config.json` 里 `marketplace.dev_dir` 的默认值 `skills_dev`，
由 `backend/skills_runtime.py` 按后端工作目录解析成绝对路径，因此三平台的实际位置是：

| EchoDesk 是怎么装的 | 后端工作目录 | 插件目录放这里 |
| --- | --- | --- |
| Windows 安装程序 / 便携版 zip | `<发布目录>\app\backend\` | `<发布目录>\app\backend\skills_dev\` |
| macOS / Linux 压缩包 | `<发布目录>/app/backend/app/` | `<发布目录>/app/backend/app/skills_dev/` |
| 源码检出（在 `backend/` 里跑后端） | `backend/` | `backend/skills_dev/` |

```
# Windows (PowerShell)
Copy-Item -Recurse .\hello-world "<release directory>\app\backend\skills_dev\"

# macOS / Linux
cp -R hello-world "<release directory>/app/backend/skills_dev/"
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
├─ README.md                市场卡片会展示本文摘要
└─ README.zh-Hans.md        中文译本（Chinese translation）
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
