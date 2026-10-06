# 隐私政策（简体中文译本）

> **本文为英文版 [`PRIVACY.md`](PRIVACY.md) 的中文译本。** 如与英文版有歧义，**以英文版为准**。
> 英文版为本仓库的正式版本。

> This is the Chinese translation of the English [`PRIVACY.md`](PRIVACY.md). If the two ever
> disagree, the English version prevails.

# EchoDesk 隐私政策

版本：v0.2.2
生效日期：随本版本发布之日
出品方：熵寂工作室（VoidEntropiaStudio）
适用对象：EchoDesk 桌面版（Python 后端 `EchoDeskBackend.exe` + .NET WPF 前端 `EchoDesk.exe`）

相关文档：[`SECURITY.md`](../SECURITY.zh-Hans.md)（安全策略与漏洞报告）、[`EULA.md`](EULA.md)（许可协议）、
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)（第三方组件清单）、
[`SBOM.json`](SBOM.json)（软件物料清单）、
[`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md) / [`en/COMPLIANCE.md`](en/COMPLIANCE.md)（企业环境适配与合规）。

## 一句话概括

**EchoDesk 是本地 AI 桌面助手：模型推理、文件解析、对话记录全部在本机完成，
不上传你的文档、对话或任何个人数据。** 只有你主动开启的功能才会产生网络请求，
且请求内容在下面逐项列明。

---

## 约定：什么算"本机"，什么算"联网"

- **本机**：你运行 EchoDesk 的这台 Windows 电脑。所有模型权重、对话历史、解析结果、
  批量处理产物都保存在这台电脑的磁盘上。
- **联网**：**后端进程**（`EchoDeskBackend.exe`）主动发起的 HTTP(S) 请求。
  前端（`EchoDesk.exe`，.NET WPF）**自身不发起任何网络请求**，它与后端之间只用标准输入输出
  （stdio，按行 JSON）通信，**不监听任何端口**，因此也没有可被外部访问的本地服务。
- EchoDesk **没有服务器端**：不存在 EchoDesk 的云端 API、账号系统或数据接收端，
  因此"EchoDesk 收集了你的数据"这件事在本架构下没有落点。
- 除第二节列出的**五个功能**外，EchoDesk 不含其他联网代码路径。

## 一、默认不联网的行为

以下操作**完全在本机进行，不产生任何网络请求**：

| 操作 | 说明 |
| --- | --- |
| AI 对话与推理 | 由本机 llama.cpp 加载本地 `.gguf` 模型完成，输入与输出都不离开本机 |
| 文件上传与解析 | Word/Excel/PPT/PDF/TXT 在本机解析，长文档在本机分段摘要 |
| 长文档分段摘要 | 用本机模型逐段摘要，不调用任何云端服务 |
| 对话历史保存 | 保存在后端工作目录下的 `session.json`（便携版为 `app\backend\session.json`），仅本机可读 |
| 常用语、界面状态 | 保存在用户数据目录的 `snippets.json`、`ui_state.json`，仅本机可读 |
| 技能（插件）加载与执行 | 从本机磁盘加载插件代码并在后端进程内执行；**不联网下载、不上报任何信息** |
| 技能市场缓存 | 市场列表缓存在本机 `app\backend\market_cache.json`；**无关键词浏览**在缓存有效期内直接读本机缓存（不联网），缓存过期后自动刷新；**输入关键词搜索**时始终联网 |
| 自动化任务执行 | 用本机已安装的 Microsoft Edge 打开你填写的网址，浏览器操作全部在本机完成 |
| 崩溃与运行日志 | 写在程序目录与本机文件里，不自动上传（见第五节） |

## 二、会联网的功能（共 5 项，均为你主动开启）

### 2.1 模型下载

| 项目 | 内容 |
| --- | --- |
| 触发条件 | 首次启动时程序询问"是否下载模型"，你点了**是**；或在模型中心点击下载；或 `/model` 切换触发征询后你确认 |
| 目标域名 | `hf-mirror.com`（默认镜像站）、`huggingface.co`（官方站，可在配置里切换）；下载链接可能被重定向到该站点的 CDN / 存储域名（形如 `cdn-lfs.<站点域名>`，**具体主机名由上游决定，本项目不作固定承诺**） |
| **发送什么** | ① 你要下载的**模型仓库名与文件名**（例如 `unsloth/Qwen3-0.6B-GGUF`、`Qwen3-0.6B-Q6_K.gguf`）；② 搜索模型时的**搜索关键词与排序、分页参数**；③ 一个说明自己是 EchoDesk 的 `User-Agent`；④ 若你填了 Hugging Face Token（或设置了环境变量 `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN`），请求头里会带 `Authorization: Bearer <你的 Token>`。**注意**：下载链接被重定向到该站点的 CDN 时，标准库 `urllib` 的实现会把该请求头一并带过去；企业若不接受这一点，就**不要填写 Token**（留空时完全不发送该请求头） |
| **不发送什么** | 你的对话内容、你上传/解析过的文档、你的本地文件路径、你的提问历史、机器唯一标识、任何硬件指纹或统计信息 |
| 上传数据 | 无（纯下载；除上面的请求头与文件名外不写入任何内容） |
| 数据去向 | 模型文件**下载到本机**（默认 `app\backend\models\`，可用 `model_hub.models_dir` 改到受管盘）；下载完成后可按大小与 sha256 校验（`model_hub.verify_sha256`，默认开启） |
| 保留多久 | 模型文件一直留在你的磁盘上，**只有你手动删除才会消失**（模型中心可删除，或直接删文件）。第三方站点（镜像站/官方站）的**服务器访问日志由该第三方按其自身政策保留**，EchoDesk 无法查询、导出或删除这些日志 |
| 如何关闭 | ① 询问下载时选"否"，程序不会自行下载；② 把 `.gguf` 文件**手工放进** `models\` 目录，程序只从本地加载，全程不联网；③ 不填 Token、不设 `HF_TOKEN`（Token 只影响私有/受限模型的鉴权，与本机数据无关）；④ 在防火墙/代理层阻断上述域名（见 [`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md)） |

> **注意（当前未实施）**：配置项 `model_hub` 下虽有 `enabled` 字段，但**当前版本未在下载/搜索路径上生效**，
> 请勿依赖它来"一键禁网"；模型下载的可控点只有"是否同意下载"与网络层的域名阻断。

### 2.2 联网搜索

| 项目 | 内容 |
| --- | --- |
| 触发条件 | 你执行 `/search on`（或用输入框下方的状态区开关）之后**再提问**；`/search off` 后提问完全本地 |
| 目标域名 | `duckduckgo.com`（含 `html.duckduckgo.com`、`lite.duckduckgo.com` 等子域，由上游检索库决定其具体端点） |
| **发送什么** | 你这一次提问的**关键词文本**，以及检索条数上限（`search_results`，默认 3 条） |
| **不发送什么** | 你的文档内容、你上传的文件（不会把文件正文塞进搜索关键词）、对话历史、本机路径、账号信息（本功能不需要也不使用任何账号或 API Key） |
| 数据去向 | 关键词发往 DuckDuckGo；返回的标题/摘要/链接**注入本次提问的上下文**并喂给本机模型 |
| 保留多久 | 搜索结果**不写入**本机对话历史；但你这次提问的原文会按会话持久化设置写入本机 `session.json`（可用 `session_file` 置空关闭自动保存）。DuckDuckGo 侧的日志保留由其自身政策决定，EchoDesk 无法删除 |
| 如何关闭 | `/search off`（同时会把 `search_default` 写回 `false`，重启后仍是关闭状态）；企业环境建议在防火墙层一并阻断 `duckduckgo.com` |

> **提示**：开启搜索后，关键词会离开本机。请勿在搜索开启状态下提问敏感内容。

### 2.3 技能市场（含插件安装）

| 项目 | 内容 |
| --- | --- |
| 触发条件 | 你打开设置窗口的「技能市场」页并刷新/搜索；或对某个技能点击「安装/更新」 |
| 目标域名 | `api.github.com`（仓库搜索与详情）、`raw.githubusercontent.com`（读取 `skill.json` / `README.md`，首选）、`codeload.github.com`（下载源码 tarball） |
| **发送什么** | ① 固定检索条件 `topic:echodesk-plugin` 加上你输入的**搜索关键词**、排序与分页参数；② 详情/安装时你要操作的那个**仓库全名**（`owner/name`）；③ 说明自己是 EchoDesk 的 `User-Agent`；④ 若你填了 GitHub Token，请求头里会带 `Authorization: Bearer <你的 Token>`（用于提高 API 限额）。**注意**：该请求头由同一个 HTTP 客户端统一添加，因此上表三个 GitHub 域名都会收到它；它**不会**被发到 GitHub 以外的地址（跨域名重定向时标准客户端会自动去掉该请求头）。企业若不希望凭据出现在多个域名下，就**不要填 Token**——留空时完全不发送该请求头 |
| **不发送什么** | 你的对话内容、文档内容、本机已安装技能清单、本机路径、机器标识 |
| 上传数据 | 无（市场只做只读查询与下载） |
| 数据去向 | 市场列表缓存在本机 `app\backend\market_cache.json`；安装的技能源码解压到本机技能目录（`marketplace.install_dir`，默认 `skills\`） |
| 保留多久 | 缓存保留到你刷新、清空或删除文件为止（`marketplace.cache_ttl_hours` 控制过期时间，默认 6 小时）；已安装技能一直留在磁盘上直到你卸载 |
| 如何关闭 | ① `"marketplace": { "enabled": false }`（写进 `config.json` 或用户覆盖文件 `config.user.json`），市场搜索/安装/状态查询都会返回"已禁用"；② 单纯**不打开**技能市场页；③ 不填 GitHub Token；④ 防火墙层阻断上述域名 |

> **说明（与旧版本描述的重要差异）**：安装技能**只从 GitHub 下载源码 tarball**（`codeload.github.com`，
> 单包上限 50MB 并做 sha256 记录）。技能清单里声明的 npm / PyPI 依赖**只展示、不执行网络安装**，
> `prepare` 脚本**不会自动执行**——因此"装一个技能"不会顺带访问 npm 或 PyPI。

### 2.4 自动更新检查

| 项目 | 内容 |
| --- | --- |
| 触发条件 | 启动时（`update.check_on_startup`，默认开启）或按 `check_interval_hours`（默认 24 小时）到期；也可手动触发 |
| 目标域名 | `api.github.com`（读取 `update.repo` 对应仓库的 Releases 列表）；有新版本时，程序只在对话框里给出链接，由**你自己**点开浏览器访问 `github.com` |
| **发送什么** | 一个 `Accept: application/vnd.github+json` 头与 `User-Agent: EchoDesk/<当前版本号> (+update-check)`——即**当前版本号**会随请求发出 | 
| **不发送什么** | 对话、文档、路径、机器标识、使用统计；**没有**安装 ID、设备号或匿名统计字段 |
| 上传数据 | 无 |
| 数据去向 | 仅读取 Releases 的 tag 与说明文本用于比对版本 |
| 保留多久 | EchoDesk 侧只在配置里记下 `last_check` / `last_seen_version` 两个时间与版本字符串；**不下载、不替换、不静默安装任何文件**（本版本只做"检查并提示"） |
| 如何关闭 | `"update": { "enabled": false }`；或把 `update.repo` 置空（跳过检查）；或关闭 `check_on_startup` 并延长 `check_interval_hours` |

### 2.5 浏览器自动化

| 项目 | 内容 |
| --- | --- |
| 触发条件 | 在设置窗口「自动化」页**勾选安全协议**并开启总开关后运行，或点「立即运行一次」，或到点自动运行（`schedule_enabled`） |
| 目标域名 | **只有你自己填写的那一个任务列表网址**（及该页面上你指定的文件下载链接、翻页链接）；协议限 `http` / `https` / `file`。EchoDesk **不内置任何默认站点** |
| **发送什么** | 浏览器访问你的目标站点时**由该站点正常收到的请求**：URL、Cookie（含你在此浏览器配置里的登录态）、表单填写值、页面上你要求点击/下载的操作 |
| **不发送什么** | EchoDesk **不向任何第三方回传**任务内容、下载到的文件、生成的审核意见或结果；**不存在**"把结果上传到 EchoDesk 服务器"这一步（没有这样的服务器）。另外，任务处理过程中被下载的文件**不会**离开本机 |
| 数据去向 | 全部留在本机：浏览器登录态在 `app\backend\browser_state.json`；下载中转在系统临时目录 `%TEMP%\echodesk_downloads`（可用 `automation.download_dir` 改）；结果与原件在用户数据目录的 `results\` 下 |
| 保留多久 | 下载的中转文件在任务处理完成后清理（异常退出时残留的临时目录在下次启动清理）；`results\` 下的**结果文件与原件保留到你手动删除**——这是审计留痕，程序不会自动清除 |
| 如何关闭 | ① 不勾选安全协议（`automation.safety_accepted=false` 时总开关无法打开）；② `"automation": { "enabled": false }`；③ 关闭 `schedule_enabled` 取消定时任务；④ 删除 `browser_state.json` 退出所有已登录站点。企业环境建议把这套自动化**整体禁用**，因为它会按你的配置访问外网 |

### 2.6 五个功能汇总表

| 功能 | 域名 | 请求里带什么 | 关闭方式 |
| --- | --- | --- | --- |
| 模型下载 | `hf-mirror.com` / `huggingface.co`（+ 可能的 CDN 域名） | 仓库名、文件名、搜索关键词、User-Agent、可选 HF Token | 不同意下载 / 手工放好模型文件 / 阻断域名 |
| 联网搜索 | `duckduckgo.com` | 当次提问关键词、条数上限 | `/search off` / 阻断域名 |
| 技能市场 | `api.github.com`、`raw.githubusercontent.com`、`codeload.github.com` | topic 与搜索关键词、仓库全名、User-Agent、可选 GitHub Token | `marketplace.enabled=false` / 不打开市场 / 阻断域名 |
| 自动更新检查 | `api.github.com` | User-Agent 中的**当前版本号** | `update.enabled=false` / `update.repo` 置空 |
| 浏览器自动化 | **用户自己填写的网址**（无默认站点） | 目标站点正常收到的请求（含你的登录 Cookie） | 不勾选安全协议 / `automation.enabled=false` |

> 除上述五类外，EchoDesk **不含任何遥测、埋点、崩溃上报或使用统计**。

## 三、数据出境与跨境传输说明

**本节请在企业合规审查时重点阅读。**

1. **会离开本机的，只有"请求"，不包括"内容"**：
   - 五个联网功能发出的都是**功能必需的元数据**（模型仓库名/文件名、搜索关键词、topic 与仓库名、版本号），
     **不包含**你的对话正文、你上传或解析的文档内容、批量处理的结果与下载原件。
2. **出境情况**：
   - 模型下载：默认镜像站 `hf-mirror.com` 由第三方运营，其服务器**通常位于境内**（实际位置以运营方公布为准，
     本项目不做保证）；切到官方站 `huggingface.co` 时，请求发往**境外**服务器。
   - 联网搜索：DuckDuckGo 的服务器在**境外**，关键词会出境（这是搜索功能本身的含义）。
   - 技能市场 / 自动更新检查：`api.github.com`、`raw.githubusercontent.com`、`codeload.github.com`
     均为 GitHub 服务，服务器在**境外**。
   - 浏览器自动化：数据流向**你自己填写的目标站点**；是否出境取决于该站点在哪里。
3. **对话与文档不出境**：模型推理、文件解析、分段摘要、自动化结果生成全部在本机完成，
   不存在"把对话或文档发给模型厂商"的链路——因为 EchoDesk 不调用任何云端大模型 API。
4. **没有账号、没有遥测**：EchoDesk 不要求注册，不采集设备指纹，不发送安装 ID；
   上述请求里唯一与"机器"沾边的信息是**当前版本号**（更新检查的 User-Agent）。
5. **合规判断的落点**：如果贵单位的规则是"业务数据不得出境"，那么只要**不使用联网搜索**、
   **把模型下载指向内部镜像或改用离线模型**、**禁用技能市场与自动更新检查**，
   EchoDesk 就是一个纯本地工具；反过来，一旦开启搜索或指向官方站下载，
   相应的元数据就会出境，请按贵单位要求做评估与告知。

## 四、企业/受限环境：彻底禁止联网的操作清单

按下面的清单逐项执行，可把 EchoDesk 收敛为**完全离线**的运行方式
（更详细的做法与域名白名单见 [`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md) / [`en/COMPLIANCE.md`](en/COMPLIANCE.md)）：

1. **模型离线化**：在联网的机器上下载 `.gguf`，拷贝到内网机器的 `models\` 目录，用 `/model` 选择本地文件。
   不要在任何提示里点"下载"。
2. **关搜索**：`/search off`；并确认配置文件里 `"search_default": false`。
3. **关市场**：`"marketplace": { "enabled": false }`。
4. **关更新检查**：`"update": { "enabled": false }`（或 `"repo": ""`）。
5. **关自动化**（如不需要）：`"automation": { "enabled": false }`，且不要勾选安全协议。
   如确实需要自动化但要求不外联，请与网络管理员确认目标站点在你的白名单内。
6. **不填任何 Token**：不填 Hugging Face Token，不设 `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN`，
   不填 GitHub Token。
7. **网络层兜底**：在防火墙/代理层阻断第 2.6 节表中的域名（这是唯一能覆盖"将来新增代码路径"的措施）。
8. **代理留痕**：如果企业本来就要求所有出网流量经代理审计，可保留系统代理设置，
   Python 侧的 `requests` / `urllib` 默认就会走代理，出网记录可在代理日志里核对。

## 五、日志与脱敏

- 崩溃日志：前端 `frontend_crash.log`（程序目录）、后端 `backend_crash.log`（后端目录），
  仅记录异常的堆栈，用于排查启动/运行问题。
- 自动化审计日志 `automation_log.jsonl`：追加式 JSON Lines，记录
  "时间 / 事件 / 任务链接 / **仅文件名** / 结果 / 截断后的错误 / 安全协议版本与同意时间"。
- **脱敏规则**：日志中不写入文件正文内容、模型输出正文、GitHub/Hugging Face Token、Cookie、密码
  或任何表单输入值；超长字段一律截断。
- 日志全部保存在本机，**不自动上传**。企业审计请以 `automation_log.jsonl` 与
  `results\` 下的 `result.txt` 为准（位置与用途参见合规文档的"审计与留痕"一节）。

## 六、临时文件与内存清理

- 自动化下载的中转文件统一放在系统临时目录的专用子目录（默认 `%TEMP%\echodesk_downloads`），
  任务处理完成后清理；程序异常退出时残留的临时目录会在下次启动时清理。
  处理后**需要留存的**原件与 `result.txt` 保存在 `results\` 下，由你自己管理。
- 技能安装的临时目录（`%TEMP%\echodesk-market-*`）在安装结束（含失败）时清理。
- 浏览器登录态（Cookie / localStorage）保存在后端目录的 `browser_state.json`，
  你可以随时删除该文件以退出所有已登录站点；卸载时随程序目录一并删除。
- 推理过程使用的 KV 缓存位于内存，进程退出即释放。

## 七、你的控制权

- 删除后端目录的 `session.json` → 清除对话历史。
- 删除用户数据目录的 `snippets.json` → 清除常用语。
- 删除用户数据目录的 `ui_state.json` → 重置窗口状态与首次引导标记。
- 删除 `models\*.gguf` → 移除本地模型（再次使用会重新询问下载）。
- 删除 `browser_state.json` → 退出自动化浏览器的所有登录态。
- 删除 `market_cache.json` → 清除技能市场缓存。
- 删除 `results\` → 清除自动化产物（**删除前请确认审计留痕是否还需要保留**）。
- 删除整个程序目录 → 等同于完全卸载（便携版不写注册表；安装版通过卸载程序完成）。

## 八、儿童与合规

EchoDesk 面向成年专业用户设计。使用自动化功能访问第三方站点、安装第三方技能时，
你需自行确认该行为符合目标站点的服务条款与你所在组织的规章制度。

## 九、政策变更

本政策如有变更，将在新版本的 `docs/PRIVACY.md` 与发布物内的说明文件中同步更新，
并在 [`CHANGELOG.md`](../CHANGELOG.md) 中注明。

## 十、联系方式

- 安全漏洞与隐私问题：公开反馈渠道为 [Issues](https://github.com/VoidEntropiaStudio/EchoDesk/issues) 与
  [Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions)；如隐私问题必须私下处理，
  请走 [`SECURITY.md`](../SECURITY.zh-Hans.md) 里的私有报告渠道——本仓库 **Security → Report a vulnerability**
  私有公告表单 <https://github.com/VoidEntropiaStudio/EchoDesk/security/advisories/new>——且不要开公开 Issue。
- 一般问题与使用咨询：参见 [`zh-Hans/FAQ.md`](zh-Hans/FAQ.md) / [`en/FAQ.md`](en/FAQ.md)，
  以及发布物内的说明文件。
