# EchoDesk 技能开发指南（v0.2.0）

本指南说明如何编写、提交与发布一个 EchoDesk 技能（插件），以及审核流程与发布规范。
English version: [`docs/en/SKILL_GUIDE.md`](../en/SKILL_GUIDE.md)。
提交技能请使用仓库的「技能提交 / Skill submission」Issue 模板。

---

## 一、技能是什么

技能是一个独立的本地扩展包：一个目录 + 一份 `skill.json` 清单 + 入口代码。
EchoDesk 通过 GitHub topic 发现技能，用户确认后把它安装到 `backend\skills\<技能id>\`，
并登记到 `backend\skills_registry.json`。

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

  "dependencies": {                        // 依赖声明（安装时仅提示，不自动安装）
    "pip": ["python-docx>=1.0"],
    "npm": []
  },

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

```powershell
# 1) 把技能源码放进后端技能目录（模拟已安装）
#    <程序目录>\backend\skills\<技能id>\
# 2) 手工登记到注册表（或直接用市场安装流程装一次）
#    <程序目录>\backend\skills_registry.json

# 3) 检查清单是否合法（等价于市场的验证逻辑）
python -c "import json;d=json.load(open('skill.json',encoding='utf-8'));print('bundle =',d.get('echodesk',{}).get('bundle'))"

# 4) 检查入口是否可加载
python -c "import importlib.util,pathlib;p=pathlib.Path('echodesk_skill/__init__.py');print('存在' if p.exists() else '缺失')"
```

安装/卸载的注册表字段：

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
A：在 `dependencies.pip` 中声明。EchoDesk 安装时只提示不自动安装（安全优先），
请在 README 中说明用户需要执行的命令。

**Q：技能能接入哪些能力？**
A：`capabilities` 目前可声明命令、UI 面板、自动化钩子与文件解析扩展。
  这些扩展点的接线方式见 `docs/zh-Hans/DEVELOPER.md` 的「扩展点」一节；
  v0.2.0 已完整实现的是**发现/验证/安装/卸载/注册表**，
  运行期加载器（把技能命令真正挂到 `/` 指令表）已定义接口，将在后续版本接线。
