# EchoDesk 技能开发指南（v0.2.0）

本指南说明如何编写、提交与发布一个 EchoDesk 技能（插件），以及审核流程与发布规范。
English version: [`docs/en/SKILL_GUIDE.md`](../en/SKILL_GUIDE.md)。
提交技能请使用仓库的「技能提交 / Skill submission」Issue 模板。

---

## 一、技能是什么

技能是一个独立的本地扩展包：一个目录 + 一份 `skill.json` 清单 + 入口代码。
EchoDesk 通过 GitHub topic 发现技能，用户确认后把它安装到
**`app\backend\skills\<技能id>\`**（程序目录下的 `backend\` 里），
并登记到 `app\backend\skills_registry.json`。

**装完即可用**：安装成功后运行期加载器会**自动热重载**——插件声明的指令立刻出现在指令表与
指令面板里，不需要重启程序，也不需要手动点「重载本地插件」。

**技能以本地用户权限运行**。EchoDesk 不对第三方技能的安全性负责，
因此清单必须如实声明依赖与权限，安装前会原样展示给用户。

---

## 二、目录结构

```
my-skill/                     ← 仓库根目录
├─ skill.json                 ← 必需：技能清单
├─ README.md                  ← 建议：市场会展示前 800 字摘要
├─ requirements.txt           ← 可选：依赖（不会自动安装，仅提示用户）
└─ echodesk_skill/            ← 入口包（名称自定，与 skill.json 的 echodesk.bundle 对应）
   ├─ __init__.py             ← 必需（包入口必须是包目录且含 __init__.py）
   └─ main.py
```

---

## 三、skill.json 规范

```jsonc
{
  "// 说明": "EchoDesk 技能清单；echodesk.bundle 是市场验证与安装的必要字段",

  "id": "contract-audit",                  // 技能唯一标识（建议与仓库名一致）
  "name": "合同财务审核",                   // 展示名（中文推荐）
  "version": "1.0.0",                      // 语义化版本
  "description": "读取合同并生成财务审核意见",  // 一句话描述（市场卡片展示）
  "author": "your-github-name",
  "license": "MIT",                        // 许可证（缺失会提示用户）
  "homepage": "https://github.com/you/my-skill",

  "echodesk": {
    "bundle": "echodesk_skill",            // 【必需】入口包目录名或入口文件路径
    "entry": "echodesk_skill/__init__.py", // 可选的显式入口文件
    "min_version": "0.2.0",                // 兼容的最低 EchoDesk 版本
    "api_version": "1"                     // 技能 API 版本
  },

  "dependencies": ["contract-core>=1.2"],  // 依赖的**其他插件**（列表或字典两种写法，运行期校验）
  "requirements": ["python-docx>=1.0"],    // Python 包依赖：安装时仅提示，不自动安装

  "publish": {                             // 可选：其他分发渠道
    "npm": "",                             // 如 "@you/my-skill"
    "pypi": ""                             // 如 "my-skill"
  },

  "capabilities": {                        // 声明的能力（供用户判断权限）
    "commands": ["/audit"],                // 可注册的命令
    "ui_panels": ["audit-panel"],          // 可注册的 UI 面板
    "automation_hooks": ["after_task"],    // 可挂接的自动化钩子
    "file_parsers": [".ofd"]               // 可扩展的文件解析格式
  },

  "prepare": ""                            // 安装后脚本（EchoDesk 不会自动执行）
}
```

**字段硬性要求**

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `echodesk.bundle` | ✅ | 缺失则该技能在市场里标记为「未验证」，安装按钮置灰 |
| `id` 或 `name` | ✅ | 用于生成安装目录名（会被安全化为 `[A-Za-z0-9._-]`） |
| `version` | 建议 | 缺失时市场显示"未知" |
| `license` | 建议 | 缺失时安装提示中会提醒用户 |
| `dependencies` | 可选 | 依赖的**其他插件**（列表或字典写法），运行期校验，见 3.1 |
| `echodesk.min_version` | 建议 | 兼容的最低主程序版本；低于它会被拒绝加载 |

### 3.1 依赖声明与版本兼容（运行期语义）

`dependencies` 声明的是**其他插件**，两种写法等价（加载器会归一成 `{插件 id: 版本要求}`）：

```jsonc
"dependencies": ["contract-core", "audit-utils>=1.2"]              // 列表
"dependencies": { "contract-core": "", "audit-utils": ">=1.2" }    // 字典
```

版本要求支持 `>=` / `>` / `<=` / `<` / `==`，也可以只写版本号（按"至少这个版本"理解），
并允许 `v1.2.0` 这样的前缀。加载器在**发现阶段之后、逐个加载之前**统一判定：

| 情况 | 结果 |
| --- | --- |
| 依赖的插件没装（不在本轮清单里） | ❌ 拒绝加载，说明"缺少依赖 X" |
| 依赖的插件被禁用 | ❌ 拒绝加载，说明"缺少依赖 X（已被禁用）" |
| 依赖的插件版本不满足要求 | ❌ 拒绝加载，说明"需要 X，实际 Y" |
| 主程序版本低于 `echodesk.min_version` | ❌ 拒绝加载，说明需要更高版本的 EchoDesk |
| 依赖在，但对方没写 `version` | ⚠️ 只警告、放行（开发期插件常忘写版本） |

**Python 包依赖**（例如 `python-docx`）请写在 `requirements` 里（或随包放 `requirements.txt`）：
EchoDesk 安装时只提示、**不会自动安装**。

加载成功后加载器还会核对 `capabilities.commands` 与实际注册的命令：**声明了却没注册**、
或**注册了却没声明**都会写进警告与 `app\backend\skills_dev.log`——这类"静默不一致"最难排查。

---

## 四、发布到技能市场

### 4.1 必需步骤

1. **在 GitHub 仓库上打 topic `echodesk-plugin`**
   （仓库页 → 右上齿轮 ⚙ About → Topics）。
   **EchoDesk 只检索这个 topic**，使用其它标签不会被发现。
2. 确保仓库根目录有 `skill.json` 且包含 `echodesk.bundle` 字段。
3. 写一份清楚的 `README.md`：技能做什么、需要什么权限、如何卸载。
4. 推送代码，然后在 EchoDesk 里 `Ctrl+M` 打开技能市场，点「刷新」确认能被搜到。

### 4.2 命名与版本规范

- 仓库名建议 `echodesk-skill-<功能>`（便于检索与识别）。
- 版本遵循语义化版本：`MAJOR.MINOR.PATCH`；
  不兼容变更必须升 `MAJOR` 并同步更新 `echodesk.min_version`。
- 不要在仓库里塞二进制大文件；不要包含模型权重。

### 4.3 安全要求（审核重点）

- **不申请不必要的权限**：`capabilities` 只声明真正使用的能力。
- **不联网**（除非功能必需），联网必须在 README 中显著说明并说明数据去向。
- **不读取用户隐私**：不扫描用户目录、不读取 `session.json`/`snippets.json` 之外的文件。
- **不写注册表、不装服务、不修改系统设置**。
- 不得包含混淆代码、不得在 `prepare` 里做与声明不符的事。
- 不得把 Token/密码写入日志或文件；若需凭据，必须由用户显式配置。

> 安装时 EchoDesk **不会自动执行** `prepare` 脚本。若将来启用该能力，
> 执行前会移除所有名称包含 `KEY`/`SECRET`/`TOKEN`/`PASSWORD` 的环境变量。

---

## 五、审核流程（建议）

技能市场当前直接展示 GitHub topic 下的全部仓库，并区分「已验证 / 未验证」。
计划中的审核流程：

1. **自动检查**（已实现）：`skill.json` 存在且含 `echodesk.bundle` → 标记「已验证」。
2. **人工复核**（待建立）：维护者按 4.3 的安全要求检查，主要关注权限声明是否最小、
   是否有隐蔽的联网/数据收集行为、`license` 是否明确。
3. **标记与撤下**：发现恶意或违规技能时，在验证缓存中标记为不可信（前端按钮置灰），
   并在发布说明中公告。

---

## 六、本地开发与调试

### 6.1 目录与优先级

插件有三个来源，**优先级从高到低**：

| 来源 | 位置 | 用途 |
| --- | --- | --- |
| 开发目录 | `app\backend\skills_dev\<插件id>\` | 日常开发：放进目录即被识别 |
| 本地清单 | `app\backend\skills_local.json` | 显式登记本地插件（含信任 / 拒绝记录） |
| 正式安装 | `app\backend\skills\<插件id>\` + `skills_registry.json` | 从技能市场安装的插件 |

**开发流程**：把插件目录放进 `skills_dev\` → 在设置窗口「开发者」页打开开发者模式 →
改完代码点「🔄 重载本地插件」。同名插件以开发目录里的那份为准，所以不必先卸载
已安装的正式版就能本地调试。

### 6.2 热重载与日志排查

- **自动热重载**：从技能市场**安装 / 卸载成功后**后端会自动热重载，并把加载结果一并回给界面
  ——装完指令立刻可用，不需要重启或手动重载。
- **「重载本地插件」只重载本地来源**：它卸载并重新加载 `skills_dev/` 与本地清单里的插件，
  **已安装的正式插件保持原样**，不会被整体卸下再装一遍（运行期状态也不会被清掉）。
- **日志**：加载、卸载、警告与错误都写进 `app\backend\skills_dev.log`；
  设置窗口「开发者」页可以**查看（可设显示最后 N 行）、🗑 清空、导出日志**。
  排查顺序建议：日志 → 清单是否合法 → 依赖是否装齐、版本是否满足。
- **隔离**：单个插件的导入、注册与指令执行异常都只影响它自己；插件里抛 `BaseException`
  （例如一句 `sys.exit()`）同样会被隔离，不会把后端进程一起带走。

### 6.3 手工校验与注册表字段

```powershell
# 1) 检查清单是否合法（等价于市场的验证逻辑）
python -c "import json;d=json.load(open('skill.json',encoding='utf-8'));print('bundle =',d.get('echodesk',{}).get('bundle'))"

# 2) 检查入口是否可加载
python -c "import importlib.util,pathlib;p=pathlib.Path('echodesk_skill/__init__.py');print('存在' if p.exists() else '缺失')"
```

安装 / 卸载的注册表字段（`app\backend\skills_registry.json`）：

```jsonc
{
  "id": "contract-audit",
  "version": "1.0.0",
  "path": "skills/contract-audit",
  "loader": "python",
  "repo": "you/my-skill",
  "installed_at": "2026-01-01T10:00:00",
  "dependencies": ["python-docx>=1.0"],
  "signature": "<tarball 的 sha256>",
  "license": "MIT"
}
```

---

## 七、常见问题

**Q：市场里搜不到我的技能？**
A：确认 topic 拼写为 `echodesk-plugin`（不要写成 `echodesk-plugin, ai` 之外的形式）、
仓库是公开的、且已有至少一次 push。GitHub 搜索索引有几分钟延迟。

**Q：安装按钮是灰的？**
A：卡片显示「未验证」说明仓库根目录缺 `skill.json` 或其中没有 `echodesk.bundle`。

**Q：需要额外依赖怎么办？**
A：Python 包依赖写在 `requirements` 里（或随包放一份 `requirements.txt`）。
EchoDesk 安装时只提示、不自动安装（安全优先），请在 README 中说明用户需要执行的命令。
依赖**其他插件**则写在 `dependencies` 里，运行期会校验（见 3.1）。

**Q：技能能接入哪些能力？**
A：`capabilities` 可以声明命令、UI 面板、自动化钩子与文件解析扩展。
  **这些扩展点都已在运行期接线**：`backend/skills_runtime.py` 动态导入入口并调用它的
  `register(api)`，把声明的指令真正挂到 `/` 指令表上，所以装完（或放进 `skills_dev/` 后重载）
  就能立刻使用。扩展点清单与接线方式见 `docs/zh-Hans/DEVELOPER.md` 的「扩展点」一节。

**Q：装上了插件，为什么命令没出现？**
A：先看 `app\backend\skills_dev.log`。最常见的是被依赖挡下：依赖的插件没装 / 被禁用、
版本不满足，或主程序版本低于 `echodesk.min_version`——这些都会**拒绝加载并写明原因**。
其次核对 `capabilities.commands` 与实际注册的命令是否一致（不一致会写警告）；
另外开发者模式关闭时，`skills_dev/` 里的插件不会加载。

**Q：插件声明的依赖方没写版本号怎么办？**
A：只警告、放行，不阻断加载（开发期的插件经常忘写版本号）。真正会挡住加载的是
"依赖根本没装 / 被禁用 / 版本不满足"。

**Q：插件报错会把整个程序弄崩吗？**
A：不会。插件的加载、卸载、注册与指令执行都兜 `BaseException`：抛 `SystemExit` 之类
（例如插件里一句 `sys.exit()`）也只中断那一步，不会带走后端进程。
若确实遇到后端退出，请附 `app\backend\backend_crash.log` 与 `app\backend\skills_dev.log`。
