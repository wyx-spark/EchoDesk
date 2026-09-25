# EchoDesk 开发者文档（v0.2.0）

本文面向前端/后端/技能开发者，说明架构、通信协议、模块划分、构建流程与扩展方式。

---

## 一、总体架构

```
┌─────────────────────────── app\EchoDesk.exe（C# WPF，net10.0-windows）───────────────────────┐
│  MainWindow            对话主界面：消息列表、输入区、状态栏、通知条、快捷键                     │
│  SettingsWindow        设置窗口：常规 / 模型 / 自动化 / 已安装 / 技能市场 / 开发者              │
│  SettingsWindow.xaml.cs→ 通过 BackendService 与后端通信                                       │
│  Services/BackendService.cs     子进程管理 + JSON 行协议客户端（含请求闸门，串行化 stdout 读取）│
│  Services/ShortcutService.cs    目录布局（程序目录 / 用户数据目录）+ 启动快捷方式检测/创建        │
│  Services/ThemeService.cs       主题三态（跟随系统/浅色/深色）与色板切换（修复二）               │
│  Services/Localization.cs       多语言：语言包加载、回退链、Loc.Key 附加属性回填                 │
│  Services/Emoji.cs              emoji 降级（不支持的环境换成 ASCII 标记，修复六）                │
│  Services/AppSettings.cs        ui_state.json：窗口状态、字号、主题、语言、emoji 策略           │
│  Utilities/MarkdownRenderer.cs  Markdown → FlowDocument 渲染（画刷走色板资源引用）             │
│  Utilities/TrustDialog.cs       本地插件批量信任确认框（修复一）                                │
│  Utilities/SnippetManager.cs    常用语模板（snippets.json）                                   │
└───────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                │  stdin/stdout：每行一个 JSON（UTF-8，无 BOM）
┌───────────────────────────────┴──────────────────────────────────────────────────────────────┐
│                    app\backend\EchoDeskBackend.exe（Python 3.13 + PyInstaller onedir）          │
│  main.py          主循环：读 stdin → 分发；长任务在后台线程产出流式响应（支持 stop 中断）      │
│  config.py        配置模型（pydantic）：窗口上限校验、分段摘要、自动化、技能市场               │
│  model_profile.py 模型能力画像（参数量/量化/窗口/KV/速度）、能力提示、硬件推荐、摘要缓存（修复四）│
│  prompts.py       任务型提示词模板（角色/输出格式/思维链/少样本）与模糊提问检测（修复四）        │
│  emoji.py         emoji 输出能力检测与 ASCII 降级（修复六）                                    │
│  i18n.py          多语言：语言包加载、回退链、占位符容错、外置语言包优先                         │
│  llm_engine.py    llama.cpp 封装：窗口/训练窗口日志、KV Cache 估算、流式生成与速度统计         │
│  gguf_meta.py     轻量解析 GGUF 元数据（不加载模型即可取训练窗口、层数、KV 头数）              │
│  summarizer.py    长文档 map-reduce 分段摘要（分段/重叠/逐段摘要/递归归并/原文指针）           │
│  conversation.py  对话历史、模式、文件上下文、会话持久化                                       │
│  command_handler.py  / 指令解析与分发（含技能注册的外部指令）                                  │
│  file_parser.py   文件解析：WPS COM 优先（可读加密文档）→ 内置轻量解析器                        │
│  skills_runtime.py 技能运行期加载器：发现/优先级/信任/卸载/面板/钩子（任务二）                  │
│  download_utils.py 模型仓库查询与下载（hf-mirror 镜像）                                       │
│  search_engine.py  DuckDuckGo 联网搜索                                                        │
│  automation.py    Playwright 驱动 Edge 的浏览器自动化（有头模式）                              │
│  marketplace.py   GitHub topic 技能市场：搜索/详情/安装/卸载（缓存 + 离线降级）                │
│  update_check.py  自动更新检查（方案 A：只检查与提示，不下载替换）                             │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

**为什么是子进程 + stdin/stdout 而不是本地 HTTP 服务**：
不占端口、不触发防火墙提示、无需处理鉴权；代价是需要自己保证消息边界（按行 JSON）
与并发安全（后端用 `_io_lock` 保护 stdout，前端用请求闸门串行化读取）。

---

## 二、通信协议

### 2.1 基本约定

- 编码：**UTF-8**（后端启动时强制 `sys.stdout/stderr` 为 UTF-8 且 `errors="replace"`，
  避免英文 locale 下 cp1252 编码异常导致进程静默退出——这是 v0.1.0 的一个真实故障）。
- 方向：前端 → 后端（stdin）每行一个请求对象；后端 → 前端（stdout）每行一个响应对象。
- 响应统一带 `type` 字段：`ready` / `system` / `assistant` / `delta` / `done` / `error` / `need_download` / `context`。
- 非 JSON 的行（第三方库误打到 stdout 的诊断信息）前端会忽略。

### 2.2 启动握手

后端在**模型加载 + 会话恢复 + 预热全部完成**后才输出：

```json
{"type":"ready","version":"v0.2.0","studio":"Void Entropia Studio","restored":3,"history":[{"role":"user","content":"..."}]}
```

前端读到 `ready` 才放行输入。启动前若本地缺模型：

```json
{"type":"need_download","model":"Qwen3.5-0.8B-Q4_K_M.gguf","size":532517120,"repo":"unsloth/Qwen3.5-0.8B-GGUF","current":"...","content":"..."}
```

前端弹确认框；同意则发送 `model_download`（后端下载 → 继续初始化 → 最终输出 `ready`），
拒绝或 stdin 关闭则后端直接退出。

### 2.3 请求动作一览

| action | 说明 | 响应形式 |
| --- | --- | --- |
| `chat` | 非流式：普通问答或 `/` 指令 | 一行 `assistant` 或 `system`（附 `used`/`limit`） |
| `chat_stream` | 流式对话 | 多行 `delta` + 一行 `done`（附 `used`/`limit`） |
| `stop` | 中断当前流式生成 / 自动化 | 一行 `system` |
| `file` | 上传并解析文件（过长自动分段摘要） | 一行 `system` |
| `context` | 查询上下文占用 | 一行 `context` + `used`/`limit` |
| `model_download` | 下载模型并热切换 | 多行 `delta` 进度 + `done` |
| `automation_config` | 读取自动化配置 | `system` + `automation` 对象 |
| `automation_config_set` | 保存自动化配置 | `system` + 规范化后的 `automation` |
| `automation_test` | 测试选择器（打开浏览器预览匹配） | 一行 `system`（含匹配数量与样例） |
| `automation_run` | 运行一次自动化 | 多行 `delta` 进度 + `done`（附 `processed`/`skipped`/`failed`/`results`） |
| `automation_status` | 自动化运行状态 | `system` + `automation` |
| `market_search` | 搜索技能 | `system` + `market`（items/分页/offline） |
| `market_detail` | 技能详情 | `system` + `detail` |
| `market_install` | 安装技能 | 一行 `system` |
| `market_uninstall` | 卸载技能 | 一行 `system` |
| `market_installed` | 已安装技能列表 | `system` + `installed` 数组 |
| `market_status` | 市场连接状态 | `system` + `market_status` |

### 2.4 示例

```jsonc
// 前端 → 后端
{"action":"chat_stream","message":"用一句话说明什么是回声。"}
// 后端 → 前端
{"type":"delta","content":"回声是"}
{"type":"delta","content":"声音在传播"}
{"type":"done","content":"回声是……","used":138,"limit":6553}
```

---

## 三、后端模块要点

### 3.1 上下文窗口（任务 1.1）

- `context_length` 默认 **8192**（v0.1.0 为 4096），`max_context` 为硬上限（默认 32768）。
- 实际窗口 = `min(请求值, 模型训练窗口, max_context)`，下限 512；
  `auto_context=false` 时忽略训练窗口一项。
- 模型训练窗口优先取 **GGUF 元数据**（`gguf_meta.py` 只解析头部 KV 区，不加载模型），
  取不到时回退 `config.model_context_windows` 注册表的关键词匹配。
- 加载/重载时输出窗口与 KV Cache 估算日志：
  `上下文窗口: 8192 tokens（模型训练窗口 262144 tokens，KV Cache 约 384.0 MB，线程 16，GPU 层 0）`
- KV Cache 估算公式：`n_ctx × n_layer × n_head_kv × (key_dim + value_dim) × 2 字节`（fp16）。

### 3.2 长文档分段摘要（任务 1.1）

触发：文件 token 数 > 窗口 × `summarize.trigger_ratio`（默认 0.5）且字符数 ≥ `min_chars`。
也可用 `/summarize` 手动强制。

流程（`summarizer.py`，map-reduce）：
1. 按 `chunk_chars` 切分，段间保留 `overlap_chars` 重叠，切点优先落在段落/句号边界；
2. 逐段摘要（`max_summary_tokens`），提示词要求保留数字/日期/金额/当事人/义务/结论；
3. 段摘要合计超 `merge_max_tokens` 时按预算分组再摘要，递归至达标（深度上限 4 + 截断兜底）；
4. 输出带**原文指针**：源文件名 + 每段对应的原文字符区间，便于回查。

注入时用摘要替代全文（`conversation.build_messages` 判断 `file_summary`），
摘要进度打到 stderr，由前端在对话框实时显示（长任务不会"看起来卡住"）。

### 3.3 文件解析（v0.2.0 重构）

优先 WPS COM（能读加密 Office 文档），失败回退内置解析器：

| 扩展名 | 实现 | 许可证 |
| --- | --- | --- |
| .docx | python-docx（段落 + 表格） | MIT |
| .xlsx/.xlsm | openpyxl（逐表逐行） | MIT |
| .pptx | python-pptx（逐页文本框 + 表格） | MIT |
| .pdf | PyMuPDF（文本层） | AGPL/商业 |
| .xls | xlrd | BSD |
| .txt/.md/.csv 等 | 内置多编码探测（utf-8-sig→utf-8→gb18030→utf-16→big5→latin-1） | — |

**为什么不再用 pyxtxt**（v0.2.0 移除）：
其 `estrattori` 子包在 `__init__` 里用 `os.listdir` 自动导入同目录全部解析器，
打包时必须整目录收集（否则启动即 `FileNotFoundError`），而整目录收集会连带引入
easyocr→torch/torchvision/scipy/cv2/skimage/numba，后端体积从 ~178MB 膨胀到 976MB；
其依赖链还含 GPL/LGPL 组件，对外分发会产生著佐权义务。详见 `docs/THIRD_PARTY_LICENSES.md`。

### 3.4 自动化（任务四）

`automation.py` 用 Playwright 的 **sync API** 驱动本机 Edge（`channel="msedge"`），
**强制有头模式**（用户可见操作过程，配置里的 `headless` 会被校验器纠正为 False）。
引擎是一个生成器：`run(stop_event, on_progress)` 逐个 yield `delta`/`done`/`error` 事件，
由 `main.py` 原样转发给前端，因此进度实时可见、`stop` 可中断。

安全设计：
- `enabled` 只有在 `safety_accepted` 为真时才允许（`config.AutomationConfig` 校验器强制）；
- 协议版本与同意时间记入审计日志；
- 下载文件放系统临时目录，**每个任务处理完立即删除**；
- 审计日志（JSONL）脱敏：只记文件名不记正文、不记 Token/Cookie，敏感键值替换为 `***`。

### 3.5 技能市场（任务五）

`marketplace.py` 三层：数据源（GitHub Search API + 本地缓存）→ 服务层（搜索/详情/安装/卸载）
→ 前端市场 Tab。

- **只从 topic `echodesk-plugin` 发现技能**，不接受任意 URL；`install`/`detail` 只接受
  `owner/name` 形式并做正则校验。
- 缓存 `market_cache.json`，TTL `cache_ttl_hours`；网络失败或 API 限流时返回缓存快照并标记 `offline`。
- 仓库验证：根目录存在 `skill.json` 且含 `echodesk.bundle` 字段 → `verified=true`，
  否则卡片显示「未验证」且安装按钮置灰。
- 安装：tarball 下载（限制 50MB，拒绝含 `..`/绝对路径的成员）→ 校验 skill.json →
  复制到 `skills/<id>` → 登记注册表（含 sha256、license、dependencies）→ 清理临时目录 → 回读校验。
- **不自动执行 prepare 脚本**；`_sanitized_env()` 会移除名称含 KEY/SECRET/TOKEN/PASSWORD 的环境变量。

---

## 四、配置参考（backend\config.json）

```jsonc
{
  "model_path": "Qwen3.5-0.8B-Q4_K_M.gguf",
  "model_repo": "unsloth/Qwen3.5-0.8B-GGUF",
  "hf_endpoint": "https://hf-mirror.com",   // 国内镜像；留空用官方
  "context_length": 8192,                    // 请求的上下文窗口
  "max_context": 32768,                      // 硬上限（校验用）
  "auto_context": true,                      // 是否按模型训练窗口收敛
  "model_context_windows": { "qwen3.5-0.8b": 32768 },  // 关键词 → 训练窗口
  "summarize": {
    "enabled": true, "trigger_ratio": 0.5, "chunk_chars": 3000, "overlap_chars": 200,
    "max_summary_tokens": 400, "merge_max_tokens": 800, "model": "", "min_chars": 4000,
    "keep_pointer": true
  },
  "file_max_chars": 200000,                  // 0 = 不限制
  "speed_hint": true, "speed_min_tps": 3.0,
  "automation": { /* 见 backend/config.json，含安全协议与选择器 */ },
  "marketplace": { "skill_topic": "echodesk-plugin", "cache_ttl_hours": 6, "github_token": "" }
}
```

**配置覆盖机制**：同目录下 `config.user.json` 的字段优先于 `config.json`，
应用只写回 `config.json`，用户手写补丁不会被覆盖。
（`config.user.json` 已在 `.gitignore` 中，适合放 GitHub Token 等个人设置。）

---

## 五、构建与发布

```powershell
# 1) 生成图标（改配色后重跑）
python tools\make_icon.py

# 2) 下载构建工具（rcedit，不入库）
cmd /c tools\fetch_tools.bat

# 3) 一键发布（前端多文件 + 后端 onedir + 元数据 + 单入口快捷方式）
& tools\build_release.ps1 -IncludeModels -MigrateFrom publish\EchoDesk-v0.1.0

# 只重打包某一端
& tools\build_release.ps1 -SkipBackend      # 只重建前端
& tools\build_release.ps1 -SkipFrontend     # 只重建后端
& tools\build_release.ps1 -SkipClean -SkipBackend -SkipFrontend   # 只重跑资源收集/元数据
```

**构建脚本的两个坑（已在代码中注释）**：
1. `rcedit 不能作用于 PyInstaller 产物`——PyInstaller 把 PKG 归档追加在 PE 映像之后，
   rcedit 重建 PE 会丢掉这段数据，后端启动即报
   `Could not load PyInstaller's embedded PKG archive`。
   因此后端元数据用 spec 的 `version`/`icon` 参数在追加归档前写入，rcedit 只处理前端 exe。
2. PowerShell 在 `ErrorActionPreference=Stop` 下会把原生命令的 stderr 当作终止性错误，
   导致 PyInstaller/dotnet 只打警告就中断构建——统一用 `Invoke-Native` 包装。

**测试**：
```powershell
python tools\test_file_parsers.py     # 六种格式解析冒烟测试（退出码 0/1）
python tools\gen_licenses.py          # 重新生成第三方许可证清单
```

---

## 六、扩展点

- **新增指令**：`command_handler.py` 的 `_handlers` 注册 + `HELP_TEXT`，
  前端 `MainWindow.xaml.cs` 的 `Commands` 数组同步（指令面板与 /help 才看得到）。
- **新增协议动作**：`main.py` 的 `handle()`（一次性）或 `stream()`（流式）里分发；
  前端在 `BackendService` 加对应方法。长任务请走 `stream()`，复用 stop 中断与进度上报。
- **新增文件格式**：`file_parser.py` 加 `_read_xxx` 并在 `_parse_builtin` 分发，
  同时把扩展名加进 `_TEXT_EXTS` 或新分支，并更新前端文件对话框的 Filter。
- **技能开发规范**：见 `docs/zh-Hans/SKILL_GUIDE.md`（中文）与 `docs/en/SKILL_GUIDE.md`（英文）。
