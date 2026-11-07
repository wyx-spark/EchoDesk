# EchoDesk 开发者文档（v0.2.0）

English version: [`docs/en/DEVELOPER.md`](../en/DEVELOPER.md)（本文为英文版的中文参考译本，如两者有歧义，**以英文版为准**）。
相关文档：[自动化指南](AUTOMATION_GUIDE.md) · [用户手册](USER_GUIDE.md) · [常见问题](FAQ.md) · [技能开发指南](SKILL_GUIDE.md) · [跨平台参考](CROSS_PLATFORM.md)。

本文面向前端/后端/技能开发者，说明架构、通信协议、模块划分、构建流程与扩展方式。

**本文的命令写法**：优先给三平台通用的写法（`python`、`git`、正斜杠），只在确实有差异时展开并标注
平台（虚拟环境激活、环境变量、Windows 专属的发布链）。Windows、macOS、Linux 三个平台都受支持，
但支持程度不同：Windows 10 / 11（x64）为正式交付平台，具备全部能力；macOS（Apple Silicon）与
Linux（x86_64）由 Avalonia 前端提供图形界面，后端代码通用，但两项能力被平台约束降级 ——
加密文档读取仅 Windows 可用（Linux / macOS 退为只读未加密文档），浏览器自动化仅 Windows 驱动本机
Microsoft Edge（另外两个平台不提供该功能）。

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
│  retrieval.py     本地文档检索：BM25 + 分块、字符区间溯源、增量索引（任务六）                 │
│  conversation.py  对话历史、模式、文件上下文、会话持久化                                       │
│  agent.py         Agent 循环：多步工具调用、步数上限、事件流、可中断（能力扩展-1 任务二）     │
│  agent_tools.py   工具层：Tool/ToolRegistry、内置工具集、权限分级 safe/sensitive/guard        │
│  command_handler.py  / 指令解析与分发（含技能注册的外部指令）                                  │
│  file_parser.py   文件解析：WPS COM 优先（可读加密文档）→ 内置轻量解析器                        │
│  skills_runtime.py 技能运行期加载器：发现/优先级/信任/卸载/面板/钩子（任务二）                  │
│  mcp_client.py    MCP stdio 客户端与工具桥接（自研零依赖；任务三）                            │
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

**两套前端、同一套后端。** 上面的图是 WPF 前端（`frontend/`，目标框架 `net10.0-windows`，仅 Windows）。
跨平台前端在 `frontend-avalonia/EchoDesk.Avalonia/`（目标框架 `net10.0`，Windows + macOS + Linux），
用同一套协议驱动同一个后端。两者都用同样的 .NET CLI 从源码检出目录运行——只有 WPF 工程是仅限 Windows 的：

```bash
# 三平台通用：跨平台（Avalonia）前端；它会从 bin/ 逐级向上找到 backend/main.py
dotnet run --project frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj

# 仅 Windows：WPF 前端
dotnet run --project frontend/EchoDesk.csproj
```

后端本身三平台都能跑（`python backend/main.py`）。各平台能力边界为：Windows 具备全部能力；
macOS（Apple Silicon）与 Linux（x86_64）可运行全部非 Windows 专属功能，其中加密文档读取退为
"仅未加密文档"、浏览器自动化不可用。

### 1.1 系统提示词的层次与长度预算（v0.2.1 提示词优化）

系统提示词按下面的顺序拼装，该顺序由 `conversation.py` 的 `MODES` / `_CORE_RULES` /
`_REPLY_LANGUAGE` 与 `get_system_prompt()` 定义：

| 层 | 内容 | 谁写的 | 与其它层的关系 |
| --- | --- | --- | --- |
| 1 | **自定义指令** | 用户 `/prompt <文本>` | 优先级最高：**替换**第 2、3 层 |
| 2 | **模式块** | `conversation.MODES`（3 个模式：chat / contract / agent） | 场景与语气；被第 1 层替换 |
| 3 | **任务层** | `prompts.TASK_TEMPLATES`（`/task`） | **叠加**在第 2 层之后；冲突时以它为准（模板末尾有 `[优先级]` 行） |
| 4 | **共享主干** | `conversation._CORE_RULES` | 视角 / 边界 / 输出纪律，任何组合下都生效 |
| 5 | **语言指令** | `conversation._REPLY_LANGUAGE` | 跟随**界面语言**（`i18n` 当前 locale） |
| 6 | 会话上下文 | `【早期对话摘要】` / `【已上传文件】` | 由 `summary` 与文件状态决定 |

**六要素**：每个模式块自带「角色 / 输出格式 / **详略判断** / 思维链 / 少样本示例 / 语气」六项
（`contract` 与 `agent` 另有一条该场景特有的「边界」）。与场景无关的通用纪律
（不知道就说不知道、不编造出处、先给最可能的答案再问、问题含糊时给 2~3 个方向……）
放在共享主干 `_CORE_RULES` 的七条里——写 3 遍既白占上下文，也会在改动时漏掉几处。

**长度预算与依据**（由 `tools/test_capability.py` 第 9 节断言守住）：
单个模式块 ≤ 480 字符（实测 243~335），模式块 + 共享主干 + 语言指令 ≤ 700 字符
（实测 607~699）。默认上下文 8192 token，中文约 1 字符/token 量级，700 字符约占 8.5%，
余量留给文件摘要与对话历史；0.8B 级模型对冗长提示词尤其敏感，所以宁可把规则共享也不铺陈。

**语言分工**：提示词**正文**保持中文、不进语言包（正文译成英文会削弱针对小模型跑偏
设计的措辞）；**回复语言**由第 5 层跟随界面语言决定——中文界面要求简体中文，
英文界面要求英文，未内置的语言要求"跟随提问语言"。`automation.py` 的审核意见提示词
写死简体中文是**独立的业务决策**（审核意见写入 `results\` 留痕），注释里已注明。

---

## 二、通信协议

### 2.1 基本约定

- 编码：**UTF-8**（后端启动时强制 `sys.stdout/stderr` 为 UTF-8 且 `errors="replace"`，
  避免英文 locale 下 cp1252 编码异常导致进程静默退出——这是 v0.1.0 的一个真实故障）。
- 方向：前端 → 后端（stdin）每行一个请求对象；后端 → 前端（stdout）每行一个响应对象。
- 响应统一带 `type` 字段：`ready` / `system` / `assistant` / `delta` / `done` / `error` / `need_download` / `summary_progress` / `context`。
- 非 JSON 的行（第三方库误打到 stdout 的诊断信息）前端会忽略。

### 2.2 启动握手

后端在**会话恢复 + 预热全部完成**后才输出 `ready`：

```json
{"type":"ready","version":"v0.2.1","studio":"Void Entropia Studio","restored":3,
 "model_ready":true,"model_state":{"ready":true,"model":"Qwen3-0.6B-Q6_K.gguf","models_dir":"...","n_ctx":8192,"guide":""},
 "model_missing":{},"history":[{"role":"user","content":"..."}]}
```

> **协议里的 `studio` 字段一律是英文 `Void Entropia Studio`**（法律主体标识，同 rcedit 元数据与
> SBOM，不随界面语言变化）。中文界面显示的出品方名称来自语言包（`main.studioName` =
> 「熵寂工作室」、`main.studioNameFull` = 「熵寂工作室（Void-Entropia-Studio）」），
> 后端 `/help`、`/status`、`/about` 用的也是语言包键 `app.studio` / `app.studio_full`。
> 也就是说：**中英对应关系由语言包表达，协议与元数据保持英文**。

**`model_ready` 用来区分"后端已就绪"与"推理可用"**（v0.2.1）。v0.2.1 起缺模型**不会**让后端停下：
它以空引擎启动，继续服务所有不依赖推理的请求，推理类请求返回引导。
前端保持输入可用、状态栏显示「无模型」，模型热加载成功后自动切回正常状态。

缺模型时会**在 `ready` 之前**发一条**非阻塞**告知：

```json
{"type":"need_download","blocking":false,"model":"Qwen3-0.6B-Q6_K.gguf","size":495107776,
 "repo":"unsloth/Qwen3-0.6B-GGUF","current":"Qwen3-0.6B-Q6_K.gguf",
 "target":"Qwen3-0.6B-Q6_K.gguf","models_dir":"...","content":"..."}
```

`blocking: false` 是重点：它是**告知**而不是**询问**。旧行为（后端阻塞读 stdin 等答复，
用户拒绝或 stdin 关闭就退出）已删除 —— 现在只有前端发 `action=exit` 才会退出。无模型时的边界：

| 类别 | 请求 | 无模型时行为 |
| --- | --- | --- |
| 推理 | `chat`、`chat_stream` | 返回引导文案，不崩溃 |
| 依赖模型 | 分段摘要、`automation_run` | 先给引导（自动化甚至不会打开浏览器） |
| 不依赖模型 | 文件解析、技能市场、设置读写、模型市场搜索与下载、会话历史 | 正常工作 |
| 状态查询 | `context`、`model_state` | `model_ready: false` + 真实原因 |

### 2.3 请求动作一览

| action | 说明 | 响应形式 |
| --- | --- | --- |
| `chat` | 非流式：普通问答或 `/` 指令 | 一行 `assistant` 或 `system`（附 `used`/`limit`） |
| `chat_stream` | 流式对话 | 多行 `delta` + 一行 `done`（附 `used`/`limit`） |
| `stop` | 中断当前流式生成 / 自动化 | 一行 `system` |
| `file` | 上传并解析文件（过长自动分段摘要） | 一行 `system` |
| `context` | 查询上下文占用 | 一行 `context` + `used`/`limit` |
| `model_download` | 下载模型并热切换 | 多行 `delta` 进度 + `done` |
| `file_stream` | 加载文件，流式输出分段摘要进度 | `delta` + 多行 `summary_progress` + `done` |
| `model_state` | 机器可读的模型状态 | `system` + `model_state` |
| `model_scan` | 重新扫描模型文件夹（登记手工放入的 `.gguf`；只有一个模型时自动加载） | `system` + `model_scan` + `model_state` |
| `model_load` | 按文件名热加载模型（无需重启） | `system` + `model_load` + `model_state` |
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
| `agent_state` | Agent 工具与权限状态（工具清单、是否启用、步数上限） | `system` + `agent` 对象 |
| `agent_configure` | 写 Agent 配置（敏感工具授权、步数上限） | `system` + `agent_config` + `agent` |
| `mcp_state` | MCP 服务器状态与已发现的工具 | `system` + `mcp` 对象 |
| `mcp_apply` | 应用 MCP 配置（启停 server、改命令与超时）并**运行时生效** | `system` + `mcp` |
| `retrieval_state` | 本地检索的可观测状态（索引文件数、块数、上次查询） | `system` + `retrieval` 对象 |
| `capability_settings` | 写回推理参数（温度 / top_p / 重复惩罚 / 最大 token / 预设 / 上下文策略 / 两个开关） | `system` + `settings` |
| `model_profile_save` | 新增或修改一个模型档位（`rename_from` 改名） | `system` + `model_profile_save` + `model_profiles` |
| `model_profile_delete` | 删除档位登记（**不删磁盘文件**；在用档位拒绝删除） | `system` + `model_profile_delete` + `model_profiles` |
| `skills_reload` | 热重载插件：开发来源（`skills_dev/`、本地清单）单独重载，已安装插件保持原样 | `system` + `skills` + `commands` |
| `skills_log` | 读取开发日志 `skills_dev.log` | `system` + `log` |
| `skills_clear_log` / `skills_export_log` | 清空 / 导出开发日志 | `system` + `ok` / 导出路径 |

### 2.4 示例

```jsonc
// 前端 → 后端
{"action":"chat_stream","message":"用一句话说明什么是回声。"}
// 后端 → 前端
{"type":"delta","content":"回声是"}
{"type":"delta","content":"声音在传播"}
{"type":"done","content":"回声是……","used":138,"limit":6553}
```

### 2.5 分段摘要进度（`summary_progress`）

`file_stream`（以及任何最终会摘要长文档的流式请求）会在文本之外输出结构化的进度行：

```json
{"type":"summary_progress","phase":"map","status":"running","index":3,"total":12,
 "chars":3000,"tokens":184,"elapsed":42.1,"chunk_elapsed":11.4,"tps":16.1,"slow":false,
 "stream":true,"ok_chunks":2,"partial_chunks":0,"failed_chunks":0,"timeout_chunks":0,
 "skipped_chunks":0,"slow_chunks":0,"content":"… 第 3/12 段处理中：已生成 184 字符，该段已用 11 秒"}
```

- `phase`：`prepare` / `map` / `reduce` / `done` / `aborted` / `failed`；
- `status`：`start` / `running` / `ok` / `failed` / `timeout` / `skipped` / `retry`；
- `content` 是按语言包渲染好的一行文案（后端解析），其余字段都是结构化的，
  前端要画进度条不必解析文本；
- `tokens` 是**当前段已生成的字符数**，界面据此区分"还在产出"与"没动静"；
- `slow: true` 表示该段超过了 `summarize.slow_chunk_seconds` —— **慢不等于坏**，流程继续；
- `stream: false` 表示引擎没有流式接口，心跳与段内中断只能在段与段之间生效
  （界面会如实说明，而不是假装能中途打断）；
- 中止（`action=stop`）返回 `phase: aborted`，原文完整保留在上下文中，**不注入半份摘要**；
- 每种失败（调用异常、超时、全部段落失败）都有自己的状态；有段落走兜底时，
  最终 `done` 的文案里会说明。

`summarizer.py` 刻意只抛结构化数据、不含任何界面文案，因此摘要算法不依赖界面语言。
每段都优先走 `engine.chat_stream`，这正是"心跳、单段超时、段内中断"能够成立的前提。

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
其依赖链还含 GPL/LGPL 组件，对外分发会产生著佐权义务。因此本项目的发布物不包含这类组件；
确有 OCR 需求的部署可在目标机器上自行安装，并自行承担随之产生的许可证义务。
随包分发的第三方组件及其许可证，逐项列在 `docs/THIRD_PARTY_LICENSES.md`（中文译名对照见
`docs/THIRD_PARTY_LICENSES.zh-Hans.md`），机器可读版本为 `docs/SBOM.json`。

**平台说明。** COM 这条路径**仅限 Windows**：`file_parser.py` 只在平台是 Windows 且能拿到
WPS/Office 自动化对象时才走它。macOS 与 Linux 上直接走内置的纯 Python 解析器，老式二进制
`.doc` / `.xls` 完全读不了，上传这类文件时报的是"该能力仅限 Windows"，**而不是**解析失败。
这条判定写在 `platform_support.py`：先按平台给出能力结论，再决定是否进入解析分支，因此错误信息
描述的是能力边界而不是文件损坏。

### 3.4 自动化（任务四）

`automation.py` 用 Playwright 的 **sync API** 驱动本机 Edge（`channel="msedge"`），
**强制有头模式**（用户可见操作过程，配置里的 `headless` 会被校验器纠正为 False）。
引擎是一个生成器：`run(stop_event, on_progress)` 逐个 yield `delta`/`done`/`error` 事件，
由 `main.py` 原样转发给前端，因此进度实时可见、`stop` 可中断。

**登录态（v0.2.1 自动化优化 任务一）**：用 ``launch_persistent_context`` 打开一个
**持久化用户数据目录**（`automation.login_state_dir`，默认 `browser_profile`，与
`browser_state.json` 同级），Cookie / localStorage 由浏览器自己落盘，后续运行自动复用。
它**返回的是上下文而不是浏览器**（且 `context.browser` 恒为 None），因此"启动 → 建上下文 →
关闭"三件事收在 `_BrowserSession` 里，调用方只碰这一个对象。
`login_state_file`（`browser_state.json`）退化为备份/迁移文件：首次使用新目录时导入一次
Cookie（老用户升级后不必重新登录），每次会话结束再导出一份。
登录检测 `_login_present()` 的判据按可靠性排序：`login_check_selector`（登录后才出现的元素）
→ `login_url_keywords`（URL 关键词）→ 返回 `None` 表示"无法判断，不阻塞"。
`clear_login_state()` 在运行中会拒绝执行（浏览器占着文件）。

**href 提取（任务二）**：选择器只负责**定位元素**，属性读取是单独一步。`_classify_href()`
把原始属性值规范化成 `_HrefInfo(raw, url, kind)`，`kind` ∈
`absolute` / `relative` / `javascript` / `fragment` / `empty` / `other`；
`click_only` 为真的四种（伪链接、页内锚点、空 href、不可导航协议）由 `_click_into()`
**回退到点击元素**，并接住新标签页或当前页跳转。
`_collect_links()` 的去重规则据此修正为**只有拿到 URL 的元素才按 URL 去重**——
以前按 `href` 去重会把一页里几十个 `href="javascript:void(0)"` 的任务压成 1 个。

**可视化选取（任务三）**：`PICKER_JS` 是注入页面的箭头函数（提示条文案由后端按界面语言传入，
脚本里不再硬编码中文），点击后回传 `isLink` / `hasHref` / `linksInside`；`_preview_pick()`
据此产出"程序将从这些元素提取以下网址：…"的预览、在"必须是链接"的位置拦下非链接元素
（返回 `blocking`，调用方不发 `done` 事件、不写配置），并说明伪链接会改为点击。

安全设计：
- `enabled` 只有在 `safety_accepted` 为真时才允许（`config.AutomationConfig` 校验器强制）；
- 协议版本与同意时间记入审计日志；
- 下载文件放系统临时目录，**每个任务处理完立即删除**；
- 审计日志（JSONL）脱敏：只记文件名不记正文、不记 Token/Cookie，敏感键值替换为 `***`；
  登录态相关事件（`login_state_used` / `login_required` / `login_completed` / `login_timeout` /
  `login_state_cleared` / `login_state_migrated`）只记路径、结果与计数，**不记 Cookie 内容**。

**平台说明。** 整个功能**仅限 Windows**：它驱动本机安装的 Microsoft Edge，要有可见窗口与可复用的
登录态，配置界面本身也是 Windows UI 的一部分。macOS 与 Linux 上该能力会报自己不可用——
`Windows-only capability (this is not a missing dependency)`——并给出文档化的替代路径
（MCP 服务器、技能，或在 Windows 机器上跑完自动化再把结果拿过来看）；见
[CROSS_PLATFORM.md](CROSS_PLATFORM.md) 第 3.2 节。

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

## 四、配置参考（backend/config.json）

```jsonc
{
  "model_path": "Qwen3-0.6B-Q6_K.gguf",
  "model_repo": "unsloth/Qwen3-0.6B-GGUF",
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
  "agent": { "max_steps": 4, "allow_sensitive": [], "max_observation_chars": 1200 },
  "retrieval": {                             // 本地检索（任务六）；mode 本版只有 keyword
    "enabled": true, "mode": "keyword", "chunk_chars": 600, "overlap_chars": 100,
    "top_k": 4, "max_context_chars": 2400, "min_score": 0.0
  },
  "mcp": {                                   // MCP stdio 客户端（任务三）；默认关闭、不自动连接
    "enabled": false, "timeout_seconds": 20.0, "auto_connect": false,
    "servers": [ { "name": "", "command": "", "args": [], "env": {},
                   "enabled": true, "passthrough_env": [] } ]
  },
  "automation": { /* 与 backend/config.json 的 automation 段一致：安全协议版本、选择器与超时 */ },
  "marketplace": { "skill_topic": "echodesk-plugin", "cache_ttl_hours": 6, "github_token": "" }
}
```

**配置覆盖机制**：同目录下 `config.user.json` 的字段优先于 `config.json`，
应用只写回 `config.json`，用户手写补丁不会被覆盖。
（`config.user.json` 已在 `.gitignore` 中，适合放 GitHub Token 等个人设置。）

**这个文件在哪——三平台的相对布局相同**：源码检出目录里是 `backend/config.json`，发布包里是
`app/backend/config.json`（反斜杠只是 Windows 的路径分隔符，命令与脚本里统一写 `/`）。
**用户数据目录**——界面状态、对话历史、批处理结果、界面语言包——则在**程序旁边**，即发布目录的外层：
Windows 上就是程序目录本身；macOS 与 Linux 上，当程序目录不叫 `app` 时回退到
`~/Library/Application Support/EchoDesk` 与 `$XDG_DATA_HOME/EchoDesk`（或 `~/.local/share/EchoDesk`）；
三平台都可以用 `ECHODESK_DATA` 覆盖。**它不是 `%APPDATA%\EchoDesk`**：代码里没有任何路径写在那里
（那个写法只出现在两处过时注释里，v0.2.2 已改正），后端通过 `config.user_data_root()` 逐级跳过
`app/`、`backend/` 这些容器目录，得到的答案与前端一致。配置项里写了**相对路径**的后端产物
（会话文件、模型、审计日志、已安装技能）仍落在后端工作目录，即发布包里的 `app/backend/`，
不受 `ECHODESK_DATA` 影响；这些配置项都接受 `~` 与绝对路径，因此企业部署可以把它们指到受管盘。
三个平台的默认数据目录分别是：Windows `%APPDATA%\EchoDesk`，macOS `~/Library/Application Support/EchoDesk`，
Linux `~/.config/EchoDesk`；设置环境变量 `ECHODESK_DATA` 可整体改写该位置，前后端对同一变量的解析结果
由 `tools/check_data_dir_consistency.py` 断言一致。

---

## 五、构建与发布

**后端依赖——三平台命令相同**（只有激活方式不同）：

```bash
# 建立虚拟环境并安装锁定版本的依赖
python -m venv .venv
python -m pip install -r backend/requirements.txt

# macOS / Linux：激活环境（bash、zsh）
. .venv/bin/activate
```

```powershell
# Windows：激活环境（PowerShell；cmd.exe 里用 .venv\Scripts\activate.bat）
.\.venv\Scripts\Activate.ps1
```

**仅 Windows——正式发布链。** `tools/build_release.ps1`、`tools/make_installer.ps1`、
Inno Setup、`rcedit` 与 WPF UI 探针都只存在于 Windows，因为它们要产出 Windows 用户下载的
安装包与便携目录：

```powershell
# 1) 下载构建工具（rcedit，不入库）
cmd /c tools\fetch_tools.bat

# 2) 一键发布（前端多文件 + 后端 onedir + 元数据 + 安装程序 + SHA256SUMS）
& tools\build_release.ps1 -IncludeModels -MakeInstaller

# 从上一版迁移模型/配置时
& tools\build_release.ps1 -IncludeModels -MigrateFrom publish\EchoDesk-v0.1.0

# 只重打包某一端
& tools\build_release.ps1 -SkipBackend                            # 只重建前端
& tools\build_release.ps1 -SkipFrontend                           # 只重建后端
& tools\build_release.ps1 -SkipClean -SkipBackend -SkipFrontend   # 只重跑资源收集/元数据

# 只重打安装程序（已构建过发布目录时）
& tools\make_installer.ps1
```

**三平台通用——图标与便携包（不涉及安装程序）。** 这些都是纯 Python 工具，因此
Windows、macOS、Linux 上命令完全一致；一台 Windows 构建机也能准备好 Linux/macOS 的包
（standalone 后端自带可重定位的预编译解释器，不依赖无法交叉编译的 PyInstaller）：

```bash
# 1) 图标源素材（Pillow；改配色后重跑）
python tools/make_icon.py

# 2) 后端 standalone 产物：全部 RID，或只产一个（可重复传该参数）
python tools/pack_backend.py
python tools/pack_backend.py --platform osx-arm64

# 3) 某个目标 RID 的前端 + 后端整包（dist/EchoDesk-<版本>-<rid>.tar.gz；win-x64 为 .zip）
python tools/pack_crossplatform.py --rid linux-x64
python tools/pack_crossplatform.py --rid osx-arm64 --backend-mode standalone

# 只想看步骤时先演练一遍
python tools/pack_crossplatform.py --plan

# 非 Windows 平台的图标与桌面条目
python tools/gen_platform_icons.py
```

便携包不需要再"安装"别的：macOS/Linux 上就是
`tar -xzf dist/EchoDesk-<版本>-<rid>.tar.gz && cd EchoDesk-<版本>-<rid> && ./start.sh`
（解压时若丢了可执行位，先跑 `chmod +x start.sh app/EchoDesk`）；Windows 上解压 `.zip` 后运行
`启动 EchoDesk.bat` 或 `app/EchoDesk.exe`。用户数据**不**落在发布包里：它落在第四节说的
各平台数据目录，除非 `ECHODESK_DATA` 指到别处。

**安装程序（Inno Setup 6）**：唯一一份交付物 `EchoDesk-v<版本>-Setup.exe`（**不含模型** ——
模型按需下载，离线场景另发"模型补充包"）由 `tools/installer.iss` 编译，
实际调用者是 `tools/make_installer.ps1`（可行性检查 + 编译 + 完整性断言 + 可选签名 + SHA256）。
默认安装位置是**安装程序自己所在的文件夹**（`[Code] GetDefaultDir` 返回
`ExtractFileDir(ExpandConstant('{srcexe}'))`），向导里仍可改；
快捷方式由 `[Icons]` 段创建，工作目录固定为 `{app}`（外层），这样 `results/` 与用户数据
生成在外层而非 `app/` 里。需要 Inno Setup 6（脚本会自动探测，或用 `-ISCC` 指定）。
Linux 与 macOS 的交付物是 `tools/pack_crossplatform.py` 产出的 `.tar.gz`，完全没有安装程序；
macOS 还需要代码签名与公证，打包脚本不擅自执行，而是把步骤写进产物里的 `SIGNING.txt`。

**构建脚本的三个坑（已在代码中注释）**：
1. `rcedit 不能作用于 PyInstaller 产物`——PyInstaller 把 PKG 归档追加在 PE 映像之后，
   rcedit 重建 PE 会丢掉这段数据，后端启动即报
   `Could not load PyInstaller's embedded PKG archive`。
   因此后端元数据用 spec 的 `version`/`icon` 参数在追加归档前写入，rcedit 只处理前端 exe。
2. **`rcedit` 同样不能作用于最终的 Inno Setup 安装包**——Inno 把安装数据追加在 PE 映像之后
   （实测：最小安装包 PE 末段结束于 893,952 字节、文件总长 2,097,164 字节），
   rcedit 会把这 1.2 MB 追加数据整段丢掉，产出运行即失败的 893,952 字节空壳。
   安装程序的版本资源请用 `[Setup] VersionInfo*` 指令（`signtool` 则是安全的）。
3. PowerShell 在 `ErrorActionPreference=Stop` 下会把原生命令的 stderr 当作终止性错误，
   导致 PyInstaller/dotnet 只打警告就中断构建——统一用 `Invoke-Native` 包装。

**测试**——第一组三平台都能跑，第二组需要 Windows 桌面：

```bash
# 三平台通用
python tools/test_file_parsers.py                # 六种格式解析冒烟测试（退出码 0/1）
python tools/check_standalone_backend.py         # 打包后的 standalone 后端：结构、启动脚本、协议
python tools/check_crossplatform_release.py      # 打包后的发布包：归档格式、可执行位、校验和
python tools/gen_licenses.py                     # 重新生成第三方许可证清单
```

```powershell
# 仅 Windows
powershell -File tools\ui_probe.ps1              # WPF UI 探针：设置窗口快速打开、不产生崩溃日志
& tools\verify_release.ps1 -SkipGui              # 顶层发布门禁，跳过需要交互式桌面的两项
```

**可复现构建**：

所有打包脚本从 `HEAD` 的提交时间派生单一 `SOURCE_DATE_EPOCH`；当 `git status --porcelain` 报告
未提交改动时拒绝打包。全部时间格式化均使用 UTC。两个 .NET 工程启用确定性编译与显式 `PathMap`；
运行期依赖按平台精确锁定于 `backend/requirements.lock`，PyInstaller 构建环境锁定于
`backend/requirements-build.lock`。zip 与 tar.gz 写入器固定条目时间戳、属主、权限、顺序与 gzip
头。Inno Setup 使用 `notimestamp` 与 `TimeStampRounding=1`，最终 Setup 可执行文件的 PE 时间戳
在原地规范化，不触碰追加的安装数据。

在干净提交上验证该保证：

```powershell
powershell tools\verify_reproducible_build.ps1
```

脚本在两次完整构建之间清除编译器缓存与构建输出，随后比对安装程序、三个平台归档、其清单以及
发布树中每个文件的 SHA-256；任何差异都会使验证失败。

**版本控制与推送（统一 SSH）**：

从 v0.2.1 跨平台-2 起，本仓库的 Git 远端操作**统一使用 SSH**，不再使用 HTTPS
（HTTPS 推送到 GitHub 在维护者机器上反复被中断：`RPC failed; HTTP 408` /
`Recv failure: Connection was reset`；SSH 不受同一网络干扰，也不需要每次输凭据）。
三处口径要一致。Git 操作本身三平台完全相同：

```bash
# remote 必须是 SSH（scp 形态 user@host:path）
git remote -v
git remote set-url origin git@github.com:wyx-spark/EchoDesk.git
```

推送包装脚本与 SSH 门禁是 PowerShell 脚本，因此仅限 Windows：

```powershell
# 一键推送（分支 + 标签；预检覆盖工作区、标签不变式、安装包哈希与 SSH 连通性）
& tools\push_release.ps1 -DryRun     # 只检查
& tools\push_release.ps1             # 检查后推送

# 统一 SSH 门禁（已包含在 verify_release.ps1 里；-TestConnection 才真连 GitHub）
& tools\check_git_ssh.ps1 -TestConnection
```

macOS 与 Linux 上等价的做法是手动执行同样的检查——`git status`、`git tag vX.Y.Z`、
`git push origin master --tags`——并把那两个脚本预检的内容（remote 是 SSH、标签与发布版本一致、
安装包哈希）写进拉取请求说明里。`push_release.ps1` 会**断言** remote 是 SSH 形式：发现 HTTPS 直接判预检失败
并打印 `git remote set-url` 的改法，而不是"照样推、失败之后再说"。门禁则把这三处
（remote、脚本默认值、文档示例）一起钉住，防止下次改动漂回 HTTPS。

**密钥准备与故障排查**（推送失败时脚本末尾也会打印同一份清单）。只有家目录的位置不同，
命令本身一致：

```bash
# macOS / Linux
ls -l ~/.ssh                                  # 要有 id_ed25519 与 id_ed25519.pub
chmod 600 ~/.ssh/id_ed25519                   # 私钥对同组/其他用户可读时 ssh 会拒绝使用
ssh-add ~/.ssh/id_ed25519                     # 交给 ssh-agent
ssh -T git@github.com                         # 期望 Hi <用户名>! You've successfully authenticated
```

```powershell
# Windows（PowerShell）；%USERPROFILE%\.ssh 与 ~/.ssh 是同一个目录
Get-ChildItem $env:USERPROFILE\.ssh           # 要有 id_ed25519 与 id_ed25519.pub
ssh-add $env:USERPROFILE\.ssh\id_ed25519      # 交给 ssh-agent
ssh -T git@github.com                         # 期望 Hi <用户名>! You've successfully authenticated
```

公钥（`id_ed25519.pub` 的**全文**，不是私钥）要加到 GitHub 的
Settings → SSH and GPG keys。**网络挡 22 端口时**改走 443，在 `~/.ssh/config` 里加
（Windows 上是 `%USERPROFILE%\.ssh\config`，同一个文件）：

```
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```

**网页链接仍是 HTTPS**：Issues / Discussions / Releases / Advisories 这类链接保持
`https://` 是正确的（SSH 地址在浏览器里打不开）。门禁只检查"git 操作示例"，
不检查网页链接；`records/` 下的历史记录也不改写（那里记的是当时的事实）。

---

## 六、扩展点

### 6.1 既有扩展点

- **新增指令**：`command_handler.py` 的 `_handlers` 注册 + `HELP_TEXT`，
  前端 `MainWindow.xaml.cs` 的 `Commands` 数组同步（指令面板与 /help 才看得到）。
- **新增协议动作**：`main.py` 的 `handle()`（一次性）或 `stream()`（流式）里分发；
  前端在 `BackendService` 加对应方法。长任务请走 `stream()`，复用 stop 中断与进度上报。
- **新增文件格式**：`file_parser.py` 加 `_read_xxx` 并在 `_parse_builtin` 分发，
  同时把扩展名加进 `_TEXT_EXTS` 或新分支，并更新前端文件对话框的 Filter。

### 6.2 工具接口（Agent 的一个工具就是一个 `Tool`）

工具注册与调用约束定义在 `backend/agent_tools.py`，关键结构如下：

```python
Tool(name="calc", description="计算算术表达式", args_hint='{"expr": "表达式"}',
     risk=RISK_SAFE, run=tool_calc, example='{"tool": "calc", "args": {...}, "why": "..."}')
```

- **注册与调用**：`ToolRegistry.register(tool)`；`default_tools()` 是内置工具集；
  `ToolRegistry.run(name, args)` 返回 `(是否成功, 观察结果文本)`，每次调用记进 `registry.calls`
  （可观测性），`public_list()` 给界面/文档用。
- **约定：工具不抛异常，失败以文本返回**。`ToolRegistry.run` 会兜住异常，并按
  `looks_like_failure()` 的统一口径（`_FAILURE_PREFIXES` / `_FAILURE_MARKERS`）把
  "算不出来""没搜到""MCP 对端报错"识别成失败——否则界面上这些都显示成"成功"，
  用户看到的是"工具用过了但什么也没有"。
- **权限分级**：`safe`（纯计算与"只看用户已经放进来的内容"，默认可用）、
  `sensitive`（会把数据带出去或读本机任意内容，必须在 `config.agent.allow_sensitive`
  里预授权）、`guard`（有写入副作用的工具，**本版一个都没有**，保留这一档只为统一词表）。
  判定入口只有 `ToolRegistry.is_allowed()`；未授权**不是静默失败**，而是一条
  "未启用 + 怎么开"的观察结果，模型据此换办法或直接作答。
- **需要宿主能力的工具**（会话、搜索、摘要器）走 `ToolContext` 注入，签名是 `(args, ctx)`，
  由 `_CTX_TOOLS` 一张表列全——刻意**不检查签名**（打包/装饰器环境下容易失效），
  这张表在注册表构造时就能一眼看全"哪些工具会碰宿主状态"。
- **提示词里的「工具协议 + 可用工具」**由 `ToolRegistry.protocol_text()` 在**运行时追加**：
  可用工具随权限变化，而模式块有 700 字符预算，运行期追加既不吃预算，
  也不会出现"提示词里列了没启用的工具"。
- **循环**在 `agent.AgentLoop.run()`：事件是 `agent_step` / `tool_call` / `tool_result`，
  `stop_event` 置位即停（与流式对话**共用同一条中断路径**）；到 `max_steps` 后
  追加一条"不要再调工具、直接用已有信息给结论"的指令再收敛一次。
  工具的 JSON 调用格式由 `parse_tool_call()` 解析（纯函数、可单测），
  对代码块包裹、前后夹解释文字等跑偏写法都做容错。

### 6.3 MCP 集成（stdio-only 的取舍）

- `mcp_client.StdioMcpClient`：自研约 200 行的 JSON-RPC 2.0 客户端，一行一个 JSON 走
  stdin/stdout，只实现 `initialize` / `tools/list` / `tools/call`（外加
  `notifications/initialized`）。调用带超时（`mcp.timeout_seconds`，默认 20 秒），
  外部进程卡死不会拖住主程序。
- `mcp_client.McpBridge`：把配置里的 server 声明（`McpServer`：`command` / `args` /
  `env` / `enabled` / `passthrough_env`）变成客户端；`discover()` 拉取工具清单，
  `call()` 调用工具，`status()` 给界面与 `/mcp` 用。
- `register_mcp_tools(registry, bridge)`：把发现的工具包装成普通 `Tool` 注册进
  **同一个** `ToolRegistry`，名字是 `mcp__<server>__<tool>`，`risk` 一律 `sensitive`，
  参数说明直接用 MCP 自己的 `inputSchema`。于是权限、审计、可见性、可中断全部复用
  Agent 那一套，不存在第二套规则。
- **为什么不用官方 `mcp` 包**：它会带进 `anyio` / `httpx` / `sse-starlette` / `starlette`
  等一串依赖，其中服务端相关的东西对一个"只需要客户端"的功能完全无用，
  而发布物正卡在 350–400 MB 区间。
- **代价（如实记录）**：本版**只支持 stdio 传输**，不支持 HTTP/SSE，也不支持
  resources / prompts / sampling 与 roots。将来换成官方包时，`McpServer` 与
  `register_mcp_tools` 都不用改 —— 换的只是传输实现，这也是把"协议"与"传输"分开写的原因。
- **拉起进程是显式动作**：`auto_connect` 默认 `false`，
  只有 `refresh_agent_tools(discover_mcp=True)`（由 `/mcp connect`、设置页写入触发）
  才会真的启动外部进程；被停用的 server 会被 `close()` 掉，工具立刻从清单里消失。

### 6.4 检索扩展点

检索相关能力定义在 `backend/retrieval.py`，关键结构如下：

- `Retriever.add_file(name, text)` 只索引那一份（**增量**，已有文件一个字节都不动，
  同名文件按最新内容替换）；`remove_file(name)` 精确摘除并重建 BM25 统计；
  `search(query, top_k, files)` 返回 `List[ScoredChunk]`（`files` 用于限定范围）；
  `as_context(query)` 渲染成带编号与 `文件名（字符 a-b）` 的注入文本，没有命中就返回空串
  （调用方据此**不注入空块**）。
- **扩展点只有 `Retriever.search()` 一个方法**：`RetrievalConfig.mode` 已预留
  `hybrid` / `vector`，只要返回值仍是 `List[ScoredChunk]`，注入、溯源、权限与 `/status`
  都不用改。现在选这两个取值会退化成 keyword（不报错，避免用户改了配置就起不来）。
- 分词与分块都在本模块内：中文 2-gram + 英文词 + **数值词元**（数值必须单独抓，
  否则"违约金比例是多少"这类带数字的关键事实检索不到）；分块不切句、带重叠，
  区间是**原文的绝对偏移**——溯源的依据，用户要能拿着"字符 210-480"回原文核对。
- 与分段摘要的分工由 `main.Backend._retrieval_context_for()` 决定：短文档不检索、
  长文档"摘要给全局 + 检索给细节"。**不要把它改成"所有文件都检索一遍"**：
  短文档全文注入本来更准，硬检索只会丢信息。

### 6.5 插件依赖与版本校验

技能运行时定义在 `backend/skills_runtime.py`，关键结构如下：

- `parse_manifest(manifest)["dependencies"]`：归一成 `{插件 id: 版本要求}`。
  `skill.json` 的 `dependencies` 支持**列表**（`["other"]`、`["other>=1.2"]`）与
  **字典**（`{"other": ">=1.2"}`）两种写法，由 `_parse_dependencies()` 统一。
- `version_satisfies(version, spec)`：支持 `>=` / `>` / `<=` / `<` / `==`；
  `parse_requirement()` 认可选的 `v` 前缀（照抄 GitHub tag 的 `v1.2.0` 很常见，
  不认它会让校验被静默绕过）；要求为空或无法解析时**一律放行**（宁可宽松也不误伤），
  比较前把版本段补齐到同样长度（`1.0` 与 `1.0.0` 视为同一个版本）。
- `SkillLoader._check_dependencies(records)`：在"发现之后、逐个加载之前"统一判定
  ——依赖可能互相交叉，必须先拿到全部清单。缺依赖、依赖被禁用、版本不满足都会
  **拒绝加载并写清原因**；被依赖插件没写版本时**只警告放行**（开发期插件常忘写版本）。
- `echodesk.min_version` 用同一个 `version_satisfies` 校验主程序版本（`APP_VERSION`）。
- `SkillLoader._verify_declared_commands()` 核对"声明的命令"与"实际注册的命令"，
  不一致（含"什么都没声明却注册了命令"）写进警告与 `skills_dev.log`。
- 插件加载、卸载与注册都兜 `BaseException` 而不只是 `Exception`：插件里一句
  `sys.exit()` / `raise SystemExit` 会穿透 `Exception`，以前会把后端进程一起带走；
  指令执行路径上的隔离在 `command_handler.py`。

### 6.6 技能开发规范

- 技能（插件）的开发规范另成专文，核心约束是：插件清单 `skill.json` 必须声明 `id`、`version`、
  `echodesk.api_version`，能力（命令、面板、自动化钩子、文件解析器）必须与实现一一对应，
  权限须显式声明且不得超出清单；本地开发可绕过市场直接从磁盘加载，发布则走 GitHub topic
  `echodesk-plugin` 的发现流程。字段级说明与完整示例的权威位置是
  `docs/zh-Hans/SKILL_GUIDE.md` 与 `docs/en/SKILL_GUIDE.md`（两者章节一一对应，英文本为正式版本）。
