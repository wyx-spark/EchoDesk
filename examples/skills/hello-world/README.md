# Hello World — an EchoDesk example plugin

> A Chinese reference translation is available at [README.zh-Hans.md](README.zh-Hans.md); if the two ever disagree, the English version prevails.

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

Copy this whole directory into `skills_dev/` under the backend working directory and the program
picks it up at start-up. The directory name comes from the default value `skills_dev` of
`marketplace.dev_dir` in `config.json`; `backend/skills_runtime.py` resolves it against the backend
working directory, so the real location on the three platforms is:

| How EchoDesk is installed | Backend working directory | Put the plugin directory here |
| --- | --- | --- |
| Windows installer / portable zip | `<release directory>\app\backend\` | `<release directory>\app\backend\skills_dev\` |
| macOS / Linux archive | `<release directory>/app/backend/app/` | `<release directory>/app/backend/app/skills_dev/` |
| Source checkout (backend run from `backend/`) | `backend/` | `backend/skills_dev/` |

```
# Windows (PowerShell)
Copy-Item -Recurse .\hello-world "<release directory>\app\backend\skills_dev\"

# macOS / Linux
cp -R hello-world "<release directory>/app/backend/skills_dev/"
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
├─ README.md                市场卡片会展示本文摘要
└─ README.zh-Hans.md        中文译本（Chinese translation）
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
