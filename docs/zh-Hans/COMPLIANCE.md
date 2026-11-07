# EchoDesk 企业环境适配与合规说明

版本：v0.2.2
出品方：熵寂工作室（Void-Entropia-Studio）
适用对象：在受管企业环境中部署 EchoDesk 桌面版（便携版或安装版）的 IT 管理员与合规审查人员

相关文档：[`../PRIVACY.md`](../PRIVACY.zh-Hans.md)（联网行为逐项说明）、[`../EULA.md`](../EULA.zh-Hans.md)（许可协议）、
[`../SECURITY.md`](../../SECURITY.zh-Hans.md)（安全策略）、[`../ENCRYPTED_DOCUMENTS.md`](../ENCRYPTED_DOCUMENTS.zh-Hans.md)（加密文档读取）、
[`../THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.zh-Hans.md)、
[`../SBOM.json`](../SBOM.json)。
English version: [`../en/COMPLIANCE.md`](../en/COMPLIANCE.md)（本文为英文版的中文参考译本，如两者有歧义，**以英文版为准**）。

---

## 一、适用范围与前提

1. 本文件面向"**企业/组织内受管环境**"的适配问题：代理、证书、防火墙、组策略与审计留痕。
   个人用户无需阅读，日常使用请看 [`USER_GUIDE.md`](USER_GUIDE.md) 与 [`FAQ.md`](FAQ.md)。
2. 前提事实（与 [`../PRIVACY.md`](../PRIVACY.zh-Hans.md) 一致）：EchoDesk 是纯本地应用，
   **没有服务器端、没有遥测、不监听端口**；只有五个功能会联网，且都可以关闭。
3. 全局配置文件是后端目录下的 `config.json`；同目录可放 `config.user.json`
   作为**用户覆盖层**（字段优先，程序不会改写它），企业下发的策略建议放在 `config.user.json`。

## 二、代理（Proxy）

### 2.1 程序如何取代理

- 所有出网请求都由**后端**发起，走的是通用 Python HTTP 客户端：
  模型下载与更新检查用标准库 `urllib`，技能市场用 `requests`，模型仓库查询用 `huggingface_hub`，
  联网搜索用第三方检索库。
- `requests` 与 `urllib` 的**默认行为**依次是：
  1. 读取环境变量 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`（含小写形式）；
  2. 若没有这些环境变量，则在**有系统代理存储的平台上**读取系统代理：Windows 上是
     WinINET（"设置 → 网络和 Internet → 代理"），macOS 上是网络服务里的代理
     （"系统设置 → 网络 → 代理"）。**Linux 在这条链路上没有系统代理存储**，
     因此只有环境变量生效——见 2.2。
- 浏览器自动化使用**本机安装的 Microsoft Edge**，其代理与证书跟随系统与 Edge 自身设置。
- **应用内没有代理设置界面（当前未实施）**：EchoDesk 不提供自己的代理配置项，
  代理只能通过系统代理或环境变量生效。
- 联网搜索链路走第三方检索库，其内部 HTTP 实现的代理遵循方式**不受本项目控制，本文不作保证**。

### 2.2 强制走代理

| 方式 | 做法 | 说明 |
| --- | --- | --- |
| A. 系统代理（推荐） | 在 Windows 设置或 macOS 系统设置里配置手动代理，或用组策略（Windows）/配置描述文件（macOS）下发系统代理 | 在 Windows 与 macOS 上同时覆盖 `urllib`、`requests` 与 Edge；Linux 没有这条链路，请用 B |
| B. 环境变量 | Windows 上通过组策略"首选项 → Windows 设置 → 环境变量"下发 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`；macOS/Linux 上写进 shell 配置（`~/.profile`、`~/.zshrc`） | 三平台通用；只需影响 Python 链路时用这种方式，也是 Linux 上唯一可行的方式；环境变量优先于系统代理 |
| C. 启动包装脚本 | 先导出环境变量再启动程序：Windows 用 `.cmd`/`.ps1` 里的 `set HTTPS_PROXY=...` 加 `start "" "app\EchoDesk.exe"`；macOS/Linux 用 `export HTTPS_PROXY=...` 加 `./start.sh` | 便携版快捷方式本身不带参数、不设置环境变量，需要按用户或按机器注入时用包装脚本 |

Windows PowerShell 示例（交互式验证用）：

```powershell
$env:HTTP_PROXY  = "http://proxy.corp.example:8080"
$env:HTTPS_PROXY = "http://proxy.corp.example:8080"
$env:NO_PROXY    = "localhost,127.0.0.1,.corp.example"
& ".\app\EchoDesk.exe"
```

macOS / Linux 示例（同样三个变量，POSIX shell）：

```bash
export HTTP_PROXY="http://proxy.corp.example:8080"
export HTTPS_PROXY="http://proxy.corp.example:8080"
export NO_PROXY="localhost,127.0.0.1,.corp.example"
./start.sh
```

- 代理需要认证时，`http://user:password@proxy.corp.example:8080` 这种写法也可用，
  但**明文凭据会留在环境变量里**，企业环境应优先使用系统代理的凭据机制。
- `NO_PROXY` 用于让本机地址与内网镜像直连（例如 `localhost,127.0.0.1,.corp.example`）。
- 验证是否生效：在代理日志里检索目标域名（见第四节），或在无模型时触发一次模型搜索/更新检查，
  看是否出现证书或连接错误。

### 2.3 彻底禁网（比"指向死代理"更硬的做法）

把代理指向一个不通的地址只是"软"阻断（且依赖各第三方库都遵守代理设置）。
企业要求**确定不出网**时，请用防火墙出站规则实现，并同时关掉对应功能：

1. 防火墙：默认拒绝出站，只放行白名单（见第四节）。用各平台自己的过滤设施——Windows Defender
   防火墙、macOS 的 `pf` 包过滤（或配置描述文件）、Linux 的 `nftables`/`ufw`。
2. 配置：`/search off`、`"marketplace": {"enabled": false}`、`"update": {"enabled": false}`、
   `"automation": {"enabled": false}`。
3. 模型：手工预置 `.gguf` 到模型目录，不从界面触发下载（**模型下载没有"一键禁网"配置项**，
   本机默认不联网；联网只发生在你主动触发的搜索、模型下载与技能市场，且日志仅记录文件名）。

## 三、证书（Certificates）

### 3.1 各条链路使用哪个信任库

| 链路 | HTTP 客户端 | 默认信任库 | 企业根证书如何生效 |
| --- | --- | --- | --- |
| 模型下载、自动更新检查 | 标准库 `urllib` + `ssl` | **Windows 证书存储**（`CA`、`ROOT`） | 把企业根证书装进 Windows 证书存储即生效 |
| 技能市场 | `requests` | **certifi** 自带的 CA 集合 | 需设置 `REQUESTS_CA_BUNDLE` 指向含企业根的 PEM |
| 模型仓库查询 | `huggingface_hub`（内部使用 `requests`/`httpx` 一类的客户端） | certifi 语义的信任库 | 同上；必要时同时设置 `REQUESTS_CA_BUNDLE` 与 `SSL_CERT_FILE` |
| 联网搜索 | 第三方检索库 | 以上游实现为准 | 不作保证；要求严格信任链的企业建议禁用搜索 |
| 浏览器自动化 | 本机 Microsoft Edge | Windows/Edge 证书存储 | 把企业根证书装进 Windows 证书存储即生效 |

> 依据说明（避免猜测）：Python 标准库 `ssl.SSLContext.load_default_certs()` 在 `win32` 平台
> **只加载 Windows 的 `CA` 与 `ROOT` 存储**，只有非 Windows 分支才走 OpenSSL 的默认路径
> （也就是 `SSL_CERT_FILE` / `SSL_CERT_DIR` 生效的那条路径）。
> 因此"在 Windows 上只设 `SSL_CERT_FILE`"**不足以**让标准库链路信任企业根证书。
> `requests` 使用 `certifi` 的捆绑包，除非 `REQUESTS_CA_BUNDLE`（或 `CURL_CA_BUNDLE`）被设置——
> 注意该变量会**整体替换**默认根证书集，而不是追加。

**macOS 与 Linux 上**同样有这两层，但标准库那一层走的是上面说的非 Windows 分支，因此做法是：

- 把企业根证书装进**系统信任库**：macOS 用
  `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain corp-root.crt`；
  Debian/Ubuntu 用 `sudo cp corp-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`；
  RHEL/Fedora 用 `sudo cp corp-root.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust`；
- **同时**下发 `SSL_CERT_FILE`（合并 PEM）。发布物用的是**随包携带的解释器**，它的 OpenSSL 可能看自己
  的默认路径而不是系统信任库，而 `SSL_CERT_FILE` 是任何构建下都能覆盖该路径的设置；
- `requests` 类链路仍然必须设 `REQUESTS_CA_BUNDLE`，与 Windows 一致。
  受管路径示例：Linux 用 `/etc/pki/corp/corp-bundle.pem`，macOS 用
  `/Library/Application Support/Corp/certs/corp-bundle.pem`，
  Windows 用 `C:\ProgramData\Corp\certs\corp-bundle.pem`（见 3.2）。

### 3.2 企业根证书的推荐装法（两手都做）

1. **装进 Windows 证书存储**：把企业根 CA 导入
   "本地计算机 → 受信任的根证书颁发机构"。这样 Edge（自动化）与标准库 `urllib`
   （模型下载、更新检查）都会直接信任。
2. **为 `requests` 类链路准备合并 PEM**：把"企业根证书 + 公共根证书"导出成一个 `.pem`
   放在受管路径（Windows 用 `C:\ProgramData\Corp\certs\corp-bundle.pem`，Linux 用
   `/etc/pki/corp/corp-bundle.pem`，macOS 用 `/Library/Application Support/Corp/certs/corp-bundle.pem`），
   然后把 `REQUESTS_CA_BUNDLE` 指到该路径
   （需要时再加 `SSL_CERT_FILE` 指向同一文件）。
   合并是因为该变量会替换默认根集，只放企业根会导致访问公共站点时证书校验失败。
3. 若企业使用 **TLS 拦截（中间人）代理**，上面两步必须都完成，
   否则会出现"能连上但证书校验失败"的现象。

### 3.3 常见报错与含义

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `CERTIFICATE_VERIFY_FAILED` / `SSLCertVerificationError` | 企业根证书没进信任库，或 `REQUESTS_CA_BUNDLE` 指向的 PEM 不含签发该站点证书的根 | 按 3.2 两步处理 |
| 模型下载失败但浏览器能打开同一地址 | 该链路走标准库 `urllib`（Windows 上是证书存储；其他平台是 OpenSSL 默认路径），浏览器走自己的信任链 | Windows：确认根证书装在"本地计算机"存储而非仅"当前用户"；macOS/Linux：下发指向合并 PEM 的 `SSL_CERT_FILE` |
| 市场搜索报 SSL 错误、其他功能正常 | `requests` 走 certifi | 设置 `REQUESTS_CA_BUNDLE` |
| 代理需要认证、请求返回 407 | 代理凭据未提供 | 用系统代理的凭据机制，或按 2.2 配置 |

## 四、防火墙与域名白名单

### 4.1 需要放行的域名（按功能）

| 功能 | 域名 | 端口 | 说明 |
| --- | --- | --- | --- |
| 模型下载（默认镜像） | `hf-mirror.com` | 443 | 默认数据源；第三方运营，服务器通常位于境内 |
| 模型下载（官方站） | `huggingface.co` | 443 | 在配置里切换到官方源时使用，服务器在境外 |
| 模型下载（CDN 重定向） | 上游站点的 CDN/存储域名（形如 `cdn-lfs.<站点域名>`；**本项目不在代码里固定，请以实际抓包或代理日志为准**） | 443 | 下载链接可能被重定向到上游站点的 CDN/存储域名，具体主机名由上游决定，本项目**不做固定承诺**；严格白名单场景建议改用内网镜像 |
| 联网搜索 | `duckduckgo.com`（含 `html.`、`lite.` 等子域） | 443 | 仅在 `/search on` 后使用；服务器在境外 |
| 技能市场 | `api.github.com`、`raw.githubusercontent.com`、`codeload.github.com` | 443 | 搜索/详情/读取清单/下载源码包 |
| 自动更新检查 | `api.github.com` | 443 | 只读取 Releases 列表；**不下载、不替换文件** |
| 浏览器自动化 | **你自己填写的网址** | 80 / 443 | 程序无内置站点，按业务需求放行 |
| 手动查看更新说明 | `github.com` | 443 | 仅当你点击程序给出的链接、由系统浏览器打开时 |

### 4.2 最小放行策略

| 策略 | 放行 | 配置（写进 `config.json` 或 `config.user.json`） |
| --- | --- | --- |
| **A. 只允许模型下载**（推荐给需要联网拉模型的团队） | `hf-mirror.com`（或你的内网镜像） | `/search off`；`"search_default": false`；`"marketplace": {"enabled": false}`；`"update": {"enabled": false}`；`"automation": {"enabled": false}` |
| **B. 完全离线** | 无 | 同 A，并在防火墙默认拒绝出站；模型用离线 `.gguf` 预置 |
| **C. 允许市场、禁止搜索与自动化** | `api.github.com`、`raw.githubusercontent.com`、`codeload.github.com`、模型下载域名 | `/search off`；`"automation": {"enabled": false}`；`"marketplace": {"enabled": true}` |
| **D. 只允许访问内网镜像与内网站点** | 内网镜像 + 内网业务站点 | 见 4.3；同时关闭搜索、市场与更新检查 |

配置示例（`config.user.json`，作为覆盖层，程序不会改写它）：

```json
{
    "search_default": false,
    "marketplace": { "enabled": false },
    "update": { "enabled": false },
    "automation": { "enabled": false }
}
```

### 4.3 只允许内网镜像的注意事项

- `config.json` 的 `hf_endpoint` 字段会作为环境变量 `HF_ENDPOINT` 生效，
  作用于基于 `huggingface_hub` 的模型下载与仓库查询链路，可指向你的内网镜像
  （例如 `"hf_endpoint": "https://hf.corp.example"`）。
- **限制（据实告知）**：设置窗口「模型中心」的**浏览/搜索**端点固定使用
  `hf-mirror.com` 与 `huggingface.co` 两个地址（`model_hub.source` 只能在
  `auto` / `mirror` / `official` 之间选择），**不读 `hf_endpoint`**。
  纯内网环境下模型中心页会不可用（表现为提示"数据源不可达"），
  但把 `.gguf` 手工放进模型目录后，本地推理与对话不受任何影响。

## 五、组策略与受限环境

### 5.1 程序的行为边界

| 项目 | 现状 |
| --- | --- |
| 注册表写入 | **不写**。唯一的注册表访问是**只读**读取 `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` 下的 `AppsUseLightTheme`（"跟随系统主题"用） |
| 系统服务 | **不安装**任何服务 |
| 监听端口 | **不监听**。前后端用标准输入输出（stdio）按行 JSON 通信，因此不会有入站防火墙提示 |
| 计划任务 | **不创建**。自动化的"定时运行"由程序自身在运行期间调度；关闭程序即不再触发 |
| 开机自启 | 配置项 `automation.start_with_windows` **当前未实施**（改它不会产生任何自启行为）。需要开机启动时，请把外层快捷方式放进"启动"文件夹（`Win+R` → `shell:startup`）；这是 Windows 用户级启动项，EchoDesk 自身不写注册表 |
| 自更新 | **不下载、不替换任何文件**，只检查并提示 |

上表中有两行只在**机制**上是 Windows 专属，其他平台的等价做法是：

- **开机自启**：Windows 把外层快捷方式放进"启动"文件夹（`Win+R` → `shell:startup`）；macOS 用
  "系统设置 → 通用 → 登录项"；Linux 用 XDG autostart 条目
  （`~/.config/autostart/echodesk.desktop`，可基于项目生成到 `deploy/linux/echodesk.desktop`
  的桌面条目，把 `Exec=` 指到你解压出来的 `start.sh`）。
- **注册表 / 配置下发**：macOS 与 Linux 没有注册表，策略以文件形式下发——预置
  `config.user.json`、5.4 表里的各项设置，以及通过各平台配置机制写入的
  `ECHODESK_DATA` / `ECHODESK_LANG_DIR` 环境变量。

### 5.2 目录可写性（重要）

便携版默认是这样的三层结构：

```text
EchoDesk-v0.2.2\
├── install-complete.bat         ← 解压后的收尾脚本（负责创建下面这个快捷方式）
├── Start-EchoDesk-v0.2.2.lnk          ← 启动快捷方式，由上面那个脚本在**你的机器上**创建
│                                  （工作目录 = 用户数据目录）
├── ui_state.json                ← 界面状态
├── snippets.json                ← 常用语
├── results\                 ← 自动化产物（用户数据目录）
└── app\                         ← 程序本体
    ├── EchoDesk.exe
    └── backend\
        ├── EchoDeskBackend.exe
        ├── config.json          ← 主配置（会被程序写回）
        ├── session.json         ← 对话历史
        ├── models\              ← 本地模型
        ├── browser_state.json   ← 自动化登录态
        ├── automation_log.jsonl ← 审计日志
        ├── market_cache.json    ← 市场缓存
        └── skills\              ← 已安装技能
```

- **用户数据目录**（外层）由环境变量 `ECHODESK_DATA` 决定，未设置时按
  "程序目录名为 `app` 则取父目录，否则取程序目录"自动判定。
- **后端相对路径产物默认落在后端工作目录**（便携版即 `app\backend\`），
  **不受 `ECHODESK_DATA` 影响**：会话历史、模型、审计日志、登录态、市场缓存、已安装技能都在那里。
- 因此：**默认部署下 `app\` 目录必须可写**（`config.json` 也会被写回）。
  如果企业要求程序目录只读，请按 5.4 的表把产物路径外移；
  更省事的做法是把整个发布目录复制到用户可写的受管路径（如 `%LOCALAPPDATA%` 或受管盘）再运行。
- **安装版同理**：安装程序把发布目录整体装到**你选择的位置**，默认就是**安装程序自己所在的文件夹** ——
  `DefaultDirName` 取自 `ExtractFileDir({srcexe})`，并且 `AppendDefaultDirName=no`、`UsePreviousAppDir=no`，
  向导以 `PrivilegesRequired=lowest` 运行。快捷方式的工作目录就是这个目录，因此布局与便携版一致。
  （本文档早前的版本写的是"默认 `{autopf}\EchoDesk`／非管理员装到 `%LOCALAPPDATA%\Programs\EchoDesk`"，
  这与 `tools/installer.iss` 的**实际行为不符**，v0.2.2 已改正。）
  正因为默认位置就是用户自己解压安装程序的地方，它对该用户本来就可写 —— 所以默认部署不需要外移任何路径。
  若管理员把安装目录改到 `C:\Program Files\EchoDesk`，标准用户在那里不可写，
  这种部署**必须**按 5.4 把产物路径外移，否则会话、模型与审计日志的写入会失败。

**Linux 与 macOS 的发布包**同样是三层结构，只是用 POSIX 名字，而且完全不创建快捷方式：

```text
EchoDesk-v0.2.2/
├── start.sh                     ← 顶层启动脚本（接管并运行 app/EchoDesk）
├── SIGNING.txt                  ← macOS 签名/公证还缺什么
├── docs/  README*.md  LICENSE*
└── app/
    ├── EchoDesk
    └── backend/                 ← config.json、session.json、models/、审计日志、skills/
```

- 在 macOS/Linux 上，外层目录**并不**自动等于用户数据目录（除非 `ECHODESK_DATA` 指到那里）：
  默认的各平台目录是 macOS 的 `~/Library/Application Support/EchoDesk` 与 Linux 的
  `$XDG_DATA_HOME/EchoDesk`（或 `~/.local/share/EchoDesk`），即
  [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) 第 5.1 节的判定顺序。
- 可写性的口径没变：所有相对路径的后端产物都在 `app/backend/` 下，因此它必须可写，
  否则就要按 5.4 把这些路径外移。把 `.tar.gz` 解压到家目录或受管盘因此不需要额外步骤——
  这一点与装到 `/opt` 的系统级部署不同。

### 5.3 用 `ECHODESK_DATA` 把用户数据放到受管盘

Windows（PowerShell，按用户或按机器下发——组策略的"环境变量"首选项）：

```powershell
[Environment]::SetEnvironmentVariable("ECHODESK_DATA", "D:\CorpData\EchoDesk", "User")
```

macOS / Linux（POSIX shell；程序从自己的环境里读该变量，因此配置文件条目就是受管下发方式）：

```bash
export ECHODESK_DATA="$HOME/CorpData/EchoDesk"   # 写进 ~/.profile 或 ~/.zshrc 即可持久生效
./start.sh
```

- 生效范围：前端（`ui_state.json`、`snippets.json`）与后端由 `user_data_root()` 推导的位置——
  主要是**results的默认根目录**。它**不会**把 `app/backend/` 下的产物搬走（见 5.2）。
- 若 `ECHODESK_DATA` 指向的位置不可写：前端会忽略该变量并回落自动判定；
  后端会直接采用该路径，随后的写入失败会以错误形式暴露（不会静默改写到别处）。
- 建议与 5.4 的表配合使用：`ECHODESK_DATA` 管"用户数据目录"，表里的配置项管"后端产物"。

### 5.4 只读部署需要改的配置项

把下列配置项改成**绝对路径**，即可把运行期写入全部移出 `app/backend/`——Windows 形如
`C:\Corp\EchoDesk\...`，macOS/Linux 形如 `/var/lib/echodesk/...`。这些配置项三平台都接受正斜杠，
`~` 也会展开成家目录，因此同一份 JSON 可以直接下发到三平台：

| 配置项 | 默认落点 | 建议指向 |
| --- | --- | --- |
| `session_file` | `app/backend/session.json` | 受管盘用户目录；置空字符串则**关闭会话持久化** |
| `model_hub.models_dir` | `app/backend/models` | 受管盘模型库 |
| `automation.audit_log_file` | `app/backend/automation_log.jsonl` | 受管盘审计目录（便于集中收集） |
| `automation.login_state_file` | `app/backend/browser_state.json` | 用户本地可写目录（含 Cookie，按敏感数据处理） |
| `automation.processed_file` | `app/backend/automation_processed.json` | 受管盘 |
| `automation.download_dir` | 系统临时目录下的 `echodesk_downloads`（Windows 为 `%TEMP%`，macOS/Linux 为 `/tmp`） | 受管临时目录 |
| `automation.results_location` | 用户数据目录 | 受管盘结果目录 |
| `marketplace.cache_file` | `app/backend/market_cache.json` | 受管盘缓存目录 |
| `marketplace.install_dir` | `app/backend/skills` | 受管盘技能目录 |
| `marketplace.dev_dir` / `marketplace.local_manifest` | `app/backend/skills_dev` / `skills_local.json` | 受管盘 |

> `config.json` 本身仍需可写（程序在切换搜索开关、修改设置时会写回）；
> 若确实必须只读，请把 `config.json` 放到可写位置运行，或接受"设置变更无法保存"的结果。

### 5.5 便携版快捷方式失效后如何重建

**仅限 Windows。** 本节说的是 Windows 便携版的布局：`.lnk` 快捷方式与 `install-complete.bat`
只存在于那里。Linux 与 macOS 的发布包没有需要修复的快捷方式——直接用 `./start.sh` 启动即可。
想要桌面图标就用 `deploy/linux/echodesk.desktop`（可用
`python tools/gen_platform_icons.py` 重新生成）为模板，把 `Exec=` 改成解压目录里的 `start.sh`；
macOS 上把 `app/EchoDesk` 拖进程序坞即可。

1. 直接双击 `app\EchoDesk.exe` 启动程序（这一步永远可用，不依赖快捷方式）。
2. 程序在启动时会检测外层快捷方式：
   - 快捷方式**不存在** → 弹出"是否创建「Start-EchoDesk-v<版本>.lnk」"，点"是"即创建；
   - 快捷方式**已失效**（移动/改名文件夹后目标丢失或工作目录不对）→ 弹出"是否重新创建"，点"是"即重建。
   重建后的快捷方式：目标 = 当前 `EchoDesk.exe`，**工作目录 = 用户数据目录**（这是"results"落在外层的关键）。
3. 如果之前点过"否"（跳过创建），程序不再主动询问：删除用户数据目录下的 `ui_state.json`
   即可重置该标记（该文件同时保存界面状态，删除只影响界面偏好）。
4. 也可以手工新建快捷方式：目标填 `<发布目录>\app\EchoDesk.exe`，
   **"起始位置"填用户数据目录（外层）**，图标可指向 `app\EchoDesk.ico`。

## 六、审计与留痕

### 6.1 结果文件 `result.txt`

- 位置：`<automation.results_location>\<automation.results_root_name>\YYYY-MM-DD\任务名\result.txt`
  （默认即"用户数据目录\results\日期\任务名\"，同名任务自动加 `-1`、`-2` 后缀）。
  路径分隔符随平台：Windows 是 `\`，macOS/Linux 是 `/`，同一个文件即
  `<automation.results_location>/<automation.results_root_name>/YYYY-MM-DD/任务名/result.txt`。
- 内容：**纯文本、不含 Markdown 标记**。文件头列出任务名称、运行日期、开始时间、
  任务列表网址、任务指令与结果目录；正文是每条任务的结果块（审核意见等），
  失败任务会写入失败原因与失败截图文件名。
- 落盘方式：每处理完一条任务就**整份重写**一次，因此中途停止或崩溃不会丢失已完成的结果。
- 同目录还会保存该任务下载到的**原件**与失败截图（`automation.save_results` 为真时）。
- 用途：事后复核"这一批任务处理了哪些链接、得到什么结论、哪些失败了"。

### 6.2 审计日志 `automation_log.jsonl`

- 位置：`automation.audit_log_file` 指定的路径，默认是后端工作目录下的 `automation_log.jsonl`
  （便携版三平台都是 `app/backend/automation_log.jsonl`）。企业建议把它指到受管盘的集中审计目录。
- 格式：JSON Lines，每行一条记录，字段固定为
  `time`（时间）、`event`（事件）、`url`（任务链接，截断到 300 字符）、
  `files`（**仅文件名**）、`result`（结果）、`error`（截断到 200 字符的错误）、
  `safety_version`（安全协议版本）、`safety_accepted_at`（勾选同意的时间），
  外加少量标量统计字段。
- **脱敏**：不写入文件正文、模型输出正文、Cookie、Token 或表单输入值。
- 用途：复核"谁在何时同意了什么版本的自动化安全协议、跑了哪些链接、下载了哪些文件、结果如何"。

### 6.3 其他可用于复核的本机痕迹

| 文件 | 位置 | 内容 | 敏感性 |
| --- | --- | --- | --- |
| `automation_processed.json` | 后端工作目录 | 已处理任务 URL 的哈希集合（去重记录） | 低 |
| `browser_state.json` | 后端工作目录 | 自动化浏览器的 Cookie / localStorage 登录态 | **高，按凭据处理** |
| `market_cache.json` | 后端工作目录 | 技能市场列表快照 | 低 |
| `backend_crash.log` | 后端目录 | Python/原生异常堆栈 | 中（可能含路径） |
| `frontend_crash.log` | 程序目录 | 前端异常堆栈（Windows 上是 WPF，其他平台是 Avalonia） | 中（可能含路径） |
| `session.json` | 后端工作目录 | 对话历史 | **高，含业务内容** |

"后端工作目录"在三平台的发布物里都是 `app/backend/`（源码检出目录里是 `backend/`），
"程序目录"即 `app/`。注意自动化相关产物（`automation_processed.json`、`browser_state.json`）
只会出现在 Windows 上，因为自动化本身仅限 Windows。

### 6.4 留存与清理

- 上述文件（除临时中转目录外）程序**不会自动删除**，留存策略由企业自行制定。
- 需要留存审计证据时，请**先归档** `automation_log.jsonl` 与 `results/`，再清理。
- 清理建议：按留存期限删除 `results/` 下的历史日期目录；
  交接或回收设备时删除 `browser_state.json` 与 `session.json`。

## 七、加密文档读取的合规说明

部分组织会用文档加密系统保护 Office 文档（例如 Cobra DocGuard）。本节是合规口径，
完整的行为说明（前提条件、责任归属、什么落盘什么不落盘）见
[`../ENCRYPTED_DOCUMENTS.zh-Hans.md`](../ENCRYPTED_DOCUMENTS.zh-Hans.md)
（英文版：[`../ENCRYPTED_DOCUMENTS.md`](../ENCRYPTED_DOCUMENTS.md)）。

> **仅限 Windows。** 本节描述的路径要经本机 WPS/Office 的 COM 接口，而 macOS 与 Linux 上不存在该接口。
> 在这两个平台上，受保护文档根本打不开：程序会把该能力报成"仅限 Windows"，而不是解析失败；
> 老式二进制 `.doc`/`.xls` 同理。Windows 具备全部能力；macOS（Apple Silicon）与 Linux（x86_64）可运行全部非 Windows 专属功能，其中加密文档读取退为"仅未加密文档"，浏览器自动化不可用；三平台默认数据目录分别为 `%APPDATA%\EchoDesk`、`~/Library/Application Support/EchoDesk`、`~/.config/EchoDesk`

- **不接触加密驱动、不修改加密策略、不持有密钥。** EchoDesk 不安装、不注册、不补丁、不卸载
  加密客户端，也不读取属于它的密钥与授权材料。
- **它只是调用 WPS。** 受保护文档能否打开，完全由本机已安装的加密客户端、以及组织授予当前
  Windows 账户的访问权限决定。在加密系统看来，发起请求的就是员工手动操作时用的那个
  已授权办公软件，没有区别。
- **加密系统的审计日志会记录 WPS 的文件访问**，与手动打开不作区分。企业现有的审批、复核与
  可追溯流程继续适用 —— 不需要额外监控，也不存在"没被记录"的新路径。
- **不改变企业现有的数据防泄漏边界。** 它既不放宽也不收紧"哪些文件可以打开"：不引入特权通道、
  不引入独立凭据、也没有自己的隐藏接口。
- **未安装企业加密客户端的电脑上**，受保护文档无法解密：EchoDesk 读到的是密文，通用解析器
  无法解析，程序提示解析失败。文件在该机器上仍是密文，无法被打开。
- **这条路径读到的明文只在内存中处理。** 不写磁盘、不写日志（日志只记文件名）、不发往任何
  网络位置；程序退出后随之释放。
- **部署侧的控制手段。** 若策略上不允许这条路径，只要**不在这台机器上安装 WPS** 即可：
  EchoDesk 就没有内存解密这条通道，会回退通用解析器，受保护文档解析失败。这是预期行为，
  也完全可以沿用现有的软件分发规则来落实。

## 八、一页速查清单

| 需求 | 做法 |
| --- | --- |
| 所有流量走企业代理 | Windows/macOS 上配系统代理（方式 A），或下发 `HTTP_PROXY`/`HTTPS_PROXY`（方式 B——也是 Linux 上唯一可行的方式） |
| 信任企业根证书 | Windows：装进 Windows 证书存储 **+** 下发 `REQUESTS_CA_BUNDLE`（合并 PEM）。macOS/Linux：装进系统信任库 **+** 下发 `SSL_CERT_FILE` 与 `REQUESTS_CA_BUNDLE` |
| 只允许模型下载 | 防火墙只放行模型下载域名 + 关搜索/市场/更新/自动化（策略 A） |
| 完全离线 | 防火墙默认拒绝出站 + 离线预置模型 + 关全部联网功能（策略 B） |
| 用户数据放到受管盘 | `ECHODESK_DATA` + 5.4 表的绝对路径配置 |
| 程序目录只读 | 把发布目录复制到可写位置运行；或按 5.4 外移产物（`config.json` 仍需可写） |
| 事后审计 | `automation_log.jsonl`（谁/何时/跑了什么）+ `results/.../result.txt`（做了什么/结果如何） |
| 快捷方式丢失（Windows 便携版） | 直接跑 `app\EchoDesk.exe`，按提示重建（必要时先删 `ui_state.json`） |
| Linux / macOS 发布包 | 解压 `.tar.gz` 后运行 `./start.sh`；用户数据落在 macOS 的 `~/Library/Application Support/EchoDesk`、Linux 的 `$XDG_DATA_HOME/EchoDesk` / `~/.local/share/EchoDesk`，或 `ECHODESK_DATA` 指定的位置 |
| 读取企业加密系统保护的文档 | 装上 WPS 与对应的加密客户端、给账户授予访问权限；读取过程经 WPS，与手动打开完全一致 —— 解密在内存中完成、磁盘不落盘，且不破解任何加密（仅 Windows） |
| 完全禁止这条路径 | 不在该机器上安装 WPS，受保护文档即解析失败（预期行为） |
