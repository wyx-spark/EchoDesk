# 跨平台支持与能力边界

> 本文说明 EchoDesk 在 Windows / Linux / macOS 三平台上**分别能做什么、不能做什么、
> 为什么、以及不能做时该怎么办**。降级行为的原则是：绝不假装成功，也绝不留下一句
> "不支持"了事 —— 每条降级都必须给出可执行的替代路径。

## 一、当前状态

| | Windows | Linux | macOS |
| --- | --- | --- | --- |
| 正式交付物 | Inno Setup 安装包 `EchoDesk-v0.2.1-Setup.exe` | 暂无安装包（`.tar.gz` 由打包脚本产出） | 暂无安装包（`.tar.gz`/`.dmg` 需签名与公证） |
| 前端 | WPF（`frontend/`，功能完整） | Avalonia 原型（`frontend-avalonia/`，已完成主窗口） | Avalonia 原型（同左） |
| 后端 | Python（与前端通过 stdin/stdout 的 JSON 行协议通信） | 同一份后端代码，无需改动 | 同一份后端代码，无需改动 |

**关键设计**：前后端之间只有一条 JSON 行协议，跨平台的工作量集中在
"前端界面框架"与"平台专属能力"两处；对话、检索、Agent、MCP、插件、模型管理
这些业务逻辑是纯 Python，天然三平台通用。

## 二、能力矩阵

| 能力 | Windows | Linux | macOS | 说明 |
| --- | --- | --- | --- | --- |
| 对话与流式输出 | ✅ | ✅ | ✅ | 含思考内容折叠、运行指标、上下文占用 |
| 文件解析（docx/xlsx/pptx/pdf/txt/csv/md…） | ✅ | ✅ | ✅ | 纯 Python 解析库（python-docx / openpyxl / python-pptx / pypdf / xlrd） |
| WPS/Office **加密文档**阅读 | ✅ | ❌ | ❌ | 依赖 WPS 的 COM 接口，仅 Windows 有 |
| 旧版二进制格式（`.doc`/`.xls`） | ✅ | ❌ | ❌ | 同上；`.docx`/`.xlsx` 不受影响 |
| 本地检索（RAG，BM25 + 引用溯源） | ✅ | ✅ | ✅ | 纯标准库实现，不依赖向量库 |
| Agent 平台、MCP 客户端、插件 / 技能 | ✅ | ✅ | ✅ | MCP 为自研 stdio JSON-RPC 客户端 |
| 模型管理与本地推理 | ✅ | ✅ | ✅ | 推理引擎 llama.cpp 三平台均有构建 |
| 企业内网自动化（Playwright + 本机 Edge） | ✅ | ❌ | ❌ | 依赖本机 Edge 的有头模式，仅 Windows |

## 三、降级边界（只有两条）

跨平台化**没有**把功能"降级成能用但不对"的状态：真正做不到的只有两条，
其余全部原样可用。

### 1. WPS COM 读取加密文档

* **不可用的原因**：加密文档需要调用 WPS 的 COM 接口（`Kwps.Application` 等），
  COM 是 Windows 专有机制。
* **用户看到什么**：明确文案 —— "当前平台不支持 WPS COM 接口（该能力仅 Windows 提供）"，
  并带上替代做法；**不会**被伪装成笼统的"解析失败"，也**不会**把
  "请安装 WPS/Office 后重试"这条 Windows 专属建议显示给 Linux/macOS 用户。
* **替代路径**：
  1. 在 Windows 机器上另存为未加密文件或 `.docx` 后拿到当前平台读；
  2. 用 MCP 服务或插件接入外部转换工具；
  3. 安装提供该扩展名解析器的技能（技能可以注册自己的解析器）。
* **空口令加密文档**（只设了权限密码）在 PDF 上仍可由通用解析器读出，
  与平台无关。

### 2. 企业内网自动化（Playwright 驱动本机 Edge）

* **不可用的原因**：该功能按设计驱动**本机已登录的 Edge**（有头模式）做内网作业，
  非 Windows 平台没有这个组合。
* **用户看到什么**：该页面不提供；配置校验 `validate()` 返回
  "仅 Windows 提供该能力（不是依赖缺失问题）" —— 这条文案很重要：
  它把"平台没有"与"装了就能用"区分开，避免用户去装一个装了也没用的包。
* **替代路径**：用 MCP 服务或技能调用外部脚本完成同一流程；或在一台 Windows
  机器上运行自动化后导出结果，再由当前平台读取结果文件。

## 四、平台探测与上报

后端在 `ready` 握手里带一个 `platform` 块，也可以随时用 `platform` 动作再取一次：

```json
{
  "kind": "windows",                 // windows / macos / linux / unknown
  "name": "Windows",
  "os": "Windows 10 (10.0.26300)",
  "architecture": "x86_64",
  "rid": "win-x64",                  // 发布切片名
  "python": {"version": "3.13.5", "frozen": false},
  "forced": false,                   // true 仅出现在测试注入平台时
  "capabilities": {
    "wps_com": {"available": true, "reason": "...", "alternative": "", "windows_only": true}
  },
  "degraded": [],                    // 本平台不可用的 Windows 专属能力
  "user_data_root": "C:\\...\\EchoDesk-v0.2.1"
}
```

前端（Avalonia 版）自己也会探测一次这些事实，并**比对**自己算出的 `kind` 与后端上报的
`kind`；不一致时会在界面上提示"前后端平台判断不一致"（平台判断漂移是真实存在的风险，
例如两边对"未知平台"的处理不同）。

## 五、目录与环境变量

用户数据根目录的判定顺序（三方一致：Avalonia 前端、WPF 前端、Python 后端）：

1. 环境变量 `ECHODESK_DATA`；
2. 程序目录名为 `app` → 取它上一级（发布物布局 `EchoDesk-v0.2.1\app\`）；
3. Windows：程序目录本身（保持既有发布行为不变）；
4. macOS：`~/Library/Application Support/EchoDesk`；
5. Linux：`$XDG_DATA_HOME/EchoDesk` 或 `~/.local/share/EchoDesk`。

界面语言包放在 `<数据根>/lang/ui/`；**后端**语言包在 `backend/lang/`（或打包后的
`app/backend/_internal/lang/`），两者不能放同一目录，否则同名文件互相覆盖。

| 环境变量 | 用途 |
| --- | --- |
| `ECHODESK_DATA` | 指定用户数据目录（便携版、企业受管盘、测试） |
| `ECHODESK_BACKEND` | 指定后端可执行文件路径（开发 / 自定义布局） |
| `ECHODESK_LANG_DIR` | 指定界面语言包目录（翻译者现场试文案） |
| `ECHODESK_FORCE_PLATFORM` | **仅测试用**：把平台判断强制成 `windows`/`macos`/`linux`/`unknown`，用来在没有非 Windows 机器时验证降级路径 |

## 六、打包与分发

* **Windows**：`tools\build_release.ps1 -MakeInstaller` 产出 Inno Setup 安装包。
* **Linux / macOS**：**PyInstaller 不能交叉编译** —— 要得到目标平台的后端可执行文件，
  就必须在目标平台上运行打包。因此仓库提供 `tools/pack_crossplatform.py`：
  在哪个平台跑就产哪个平台的包（发布 Avalonia 前端 → 打包后端 → 清理调试符号 →
  产出 `.tar.gz` → 写 `SHA256SUMS.txt` 与 `manifest.json`）。
* **CI 矩阵**：`.github/workflows/ci.yml` 的 `cross-platform` 任务在
  Windows / Ubuntu / macOS 三个 runner 上各构建、各发布、各跑一次无窗口自检。
* **macOS 签名与公证**：需要维护者的 Developer ID 证书与 Apple 账号，
  脚本**不擅自执行**，而是把命令与缺失步骤写进产物里的 `SIGNING.txt`。
  在拿到证书前，macOS 用户需要右键打开或 `xattr -dr com.apple.quarantine` 解除隔离。
* **AppImage**：需要 `appimagetool` 与 AppDir 结构，当前以 `.tar.gz` 为准（未完成项见
  `records/v0.2.1-跨平台记录.md`）。

## 七、验证证据（如实说明）

开发机只有 Windows（无 WSL、无 Docker、无 macOS），所以证据分四层，各层能证明什么
写在下面，**不把构建级证据说成运行级证据**：

| 层次 | 做法 | 能证明什么 |
| --- | --- | --- |
| 真实运行 | Windows 上跑 `EchoDesk.exe --selftest`：平台探测 → 按平台规则拉起后端 → `ready` 握手 → `platform`/`capability` 往返 | Windows 上整条链路真的通（11/11） |
| 运行级（注入平台） | `python tools\test_platform.py`：用 `ECHODESK_FORCE_PLATFORM` 把平台强制成 linux/macos，断言降级文案、能力清单、旧格式解析提示、自动化校验原因（69/69） | 非 Windows 的**降级逻辑**是对的；报告里 `forced=true` 明确标注这不是真实平台 |
| 构建级 | 从 Windows 交叉发布 `linux-x64` / `osx-arm64` 自包含产物 | 前端工程与代码在目标 RID 下**能构建出产物**，但不等于能在那些系统上跑起来 |
| CI | 三平台矩阵：构建 + 发布 + 平台测试 + 无窗口自检 | 只有在这里才拿到真实的 Linux/macOS 运行证据 |

**当前缺口（诚实记录）**：仓库内没有来自真实 Linux/macOS 的运行证据；
CI 尚未跑过一次（仓库还没推送过），因此矩阵是"已写好、待运行"。
非 Windows 上的真实交互（窗口、字体、拖拽、输入法）也还没被人工验证过。

## 八、前端实现差异（WPF → Avalonia）

迁移不是"换个皮肤"，以下几处是**实现方式必须改**的：

| 方面 | WPF | Avalonia | 影响 |
| --- | --- | --- | --- |
| 富文本 | `FlowDocument` + `Inline` | Markdown 解析成控件树（段落/表格/代码块都是控件） | 表格用 `Grid` 画、代码块带独立复制按钮；选择是"按段落"而不是整篇 |
| 链接 | 行内 Run 可挂 ToolTip 显示 URL | 行内 Run 不是控件，挂不了 ToolTip | 原型把 URL 附在链接文本后（信息不丢，但样式不同） |
| 字号缩放 | 改 `FlowDocument` 一处字号 | 消息区按新字号重渲染 | 行为一致，实现从"改属性"变成"重建" |
| 主题 | 自己维护两套色板 + `DynamicResource` | `FluentTheme` + `ThemeVariant` | 色板键与对比度约束仍沿用 `tools/check_theme.py` 的口径 |
| 拖放 | `DataObject` / `DataFormats.Files` | Avalonia 12 的 `DataTransfer` API | 代码需改写（旧 API 已移除） |
| 剪贴板 | `Clipboard.SetText` | `IClipboard.SetTextAsync`（扩展方法） | 变为异步调用 |

## 九、未完成项

见 `records/v0.2.1-跨平台记录.md` 的"未完成项"一节，摘要如下：

* 前端只迁移了主窗口；设置窗口与各功能面板仍是 WPF 专属（任务二是逐模块迁移，本轮按任务书
  要求先完成主窗口并留下可复用的服务层）。
* Linux/macOS 的 PyInstaller 产物、归档与签名尚未在真实平台上产出（需 CI 或维护者机器）。
* macOS 公证、Linux AppImage 未自动化。
* 非 Windows 的图标、字体回退、输入法、高 DPI 等真实交互未验证。
