# 跨平台支持与能力边界

> 本文说明 EchoDesk 在 Windows / Linux / macOS 三平台上**分别能做什么、不能做什么、
> 为什么、以及不能做时该怎么办**。降级行为的原则是：绝不假装成功，也绝不留下一句
> "不支持"了事 —— 每条降级都必须给出可执行的替代路径。

## 一、当前状态

> **版本口径**：**v0.2.1 = 跨平台之前**（不含 `frontend-avalonia/`，也没有平台抽象）；
> **v0.2.2 = 跨平台版本**（跨平台前端迁移 + 后端平台化 + 三平台分发与打包）。
> 标签 `v0.2.1` 指向跨平台之前的最后一个提交；它的安装包与发布物由该提交重建并冻结，
> 详见 `records/v0.2.1-发布记录.md` §30。

| | Windows | Linux | macOS |
| --- | --- | --- | --- |
| 正式交付物 | 安装包 `EchoDesk-v0.2.2-Setup.exe`；另有三平台便携包里的 `…-win-x64.zip` | 便携包 `EchoDesk-v0.2.2-linux-x64.tar.gz`（免安装、自带解释器；暂无系统级安装包与 AppImage） | 便携包 `EchoDesk-v0.2.2-osx-arm64.tar.gz`（同上；`.dmg` 与公证需 Apple 证书） |
| 前端 | WPF（`frontend/`，功能完整） | Avalonia（`frontend-avalonia/`：主窗口 + 设置窗口 7 个页签 + 各功能面板均已迁移） | 同左 |
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
  "user_data_root": "C:\\...\\EchoDesk-v0.2.2"
}
```

前端（Avalonia 版）自己也会探测一次这些事实，并**比对**自己算出的 `kind` 与后端上报的
`kind`；不一致时会在界面上提示"前后端平台判断不一致"（平台判断漂移是真实存在的风险，
例如两边对"未知平台"的处理不同）。

## 五、目录与环境变量

用户数据根目录的判定顺序（三方一致：Avalonia 前端、WPF 前端、Python 后端）：

1. 环境变量 `ECHODESK_DATA`；
2. 程序目录名为 `app` → 取它上一级（发布物布局 `EchoDesk-v0.2.2\app\`）；
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

### 6.0 后端有两种形态，各管一段（跨平台-2 轮起）

| 形态 | 用于 | 为什么 |
| :--- | :--- | :--- |
| **PyInstaller onedir** | Windows 正式安装包 | WPF 前端的启动路径只接受一个可执行文件（没有"解释器 + main.py"模式）；而在 Windows 上 standalone 相对它没有收益 |
| **python-build-standalone** | Linux / macOS 与跨平台便携包 | PyInstaller **不能交叉编译**；standalone 是**可重定位的预编译解释器**，于是 Windows 打包机上就能准备三平台后端，目标机解压即用、**无需安装 Python、无需联网** |

后者由 `tools/pack_backend.py` 产出，结构如下：

```
backend-<rid>/
    python/           随包的 CPython（目标机无需安装 Python）
    site-packages/    pip 交叉下载的依赖（**与解释器并排**，靠 PYTHONPATH 注入）
    app/              后端代码
    run-backend.bat   Windows 启动脚本（给人手动启动/调试用）
    run-backend.sh    Linux/macOS 启动脚本（exec，信号与退出码原样透传）
    manifest.json     平台 / Python 版本 / 每个依赖的实际版本 / 各部分体积
```

几个要点都是踩过坑之后的结论：

* **前端不经过启动脚本**，直接调用 `python/python[.exe] app/main.py`。中间多一层 shell
  会让"停止后端"变成"杀掉 shell、Python 还活着"，而前端是按子进程管理后端的。
  启动脚本仍然提供，用途是**给人手动启动与调试**。
* **依赖不并进 `python/lib/site-packages`**，而是与解释器并排：这样"解释器自带的"与
  "我们装的"一眼可分，`manifest.json` 也能逐项记录依赖版本。
* **交叉下载依赖时按目标平台评估环境标记**：pip 自己用的是**打包机**的平台，
  为 Linux 准备依赖时会去要 Linux 版的 `pywin32`，从而整条依赖链失败。
* **不采用 uvbox**：它首次运行要联网下载依赖，与"数据不出本机、断网可用"的核心承诺冲突。
* **`llama-cpp-python` 在 macOS 上要单独取件**（跨平台-2 轮实测，这条值得单独说）：
  上游发布在 PyPI 上的**只有源码包**，所有轮子都托管在作者自己的 GitHub Releases 索引里；
  而其中 **`cpu` 索引的 macOS arm64 轮子是坏的** —— 字节数与官方 digest 完全一致
  （"下完了、也没被篡改"），但包内 `lib/*.dylib` 大面积压缩流损坏，`pip install` 必然失败。
  实测损坏的版本：cpu 索引的 0.3.21 / 0.3.30 / 0.3.35 / 0.3.36，metal 索引的 0.3.31～0.3.36；
  **`metal` 索引（对应 `vX.Y.Z-metal` 这些 Release tag）的 0.3.30 是最后一个完好的**。
  因此 macOS 上按**直接 URL 钉死**这一个轮子（见 `backend/requirements.txt`）——
  用直接 URL 而不是"索引 + 版本号"，是因为两个索引里存在**同名但内容不同**的文件，
  按索引挑是不确定的。对 macOS 而言 metal 构建本来也更正确：它启用 Metal（Apple GPU）后端。
  取件与校验工具：`tools/fetch_wheel.py`（curl 续传 + 逐成员解压校验，
  能直接在打包前把"下完却不能装"的上游坏包挡住）。
* **体积**（口径：磁盘实际占用，硬链接按 inode 去重；三平台均在 Windows 打包机上实测）：
  后端 standalone 形态解压后 **Windows 295.9 MB / Linux 450.9 MB / macOS 352.5 MB**，
  归档 **102.9 / 150.2 / 120.3 MB**。
  差异是真实的、不是统计口径造成的：Linux 的 standalone 解释器本身就更大。
  两个关键决定让这三组数字比第一版小了很多：
  1. **解释器归档用 `install_only_stripped` 而不是 `install_only`** —— 官方对每个平台都发布
     两个变体，剥掉的是原生调试符号（只在用 gdb/lldb 调原生崩溃时才需要；
     Python 自己的 traceback 来自 `.py` 源码）。实测归档 Linux 71.8 → **33.5 MB**、
     Windows 45.2 → **21.0 MB**；打包后 `python/` 从 235.2/125.8 MB 降到 **81.1/33.4 MB**。
     需要原生调试时用 `--keep-symbols` 换回未剥离的变体。
  2. **删掉不可达的 Tcl/Tk 运行时**：`tkinter` 包已删 → `libtcl9*.so`/`libtk9*.so`/`tcl9/`/
     `tcl*.dll` 再无任何 import 能加载（约 7 MB/平台）。这是可达性论证，不是猜。
  **不做标准库裁剪**：量过之后发现瓶颈不是"模块多" —— 未剥离时 Linux `python/` 里
  136.6 MB 是 `libpython3.13.so`、77.2 MB 是 `bin/python3`，都是原生二进制；
  标准库的 `.py` 加起来才几十 MB，其中最该删的（test/idlelib/tkinter/ensurepip/distutils/
  `__pycache__`）早就删了。按"模块多"的印象去裁标准库，收益远小于风险
  （删掉某个冷门模块只会在目标机上以 ImportError 暴露）。
  校验方式：`python tools/check_standalone_backend.py`（结构 / 启动脚本语法 /
  本平台可运行 / 协议往返**与真实动作集**四档；非本机平台额外做
  "原生依赖是不是合法动态库"的静态检查）。
* **弱网或断网构建**：把依赖 wheel 预先放进一个目录，设 `PIP_FIND_LINKS=<该目录>`
  后重跑打包脚本即可 —— pip 会优先从本地取，跳过最不稳定的外部下载环节。
  实测：`llama-cpp-python` 的 wheel 下载地址重定向到 `github.com`，弱网下会让整轮
  依赖安装反复重试直至失败；把它预先下载到本地后，同一轮构建**零重试**通过。
  **不需要改脚本一行**（纯环境变量），这也是"数据不出本机、断网可用"这条产品承诺
  在构建链上的对应做法。

* **Windows**：`tools\build_release.ps1 -MakeInstaller` 产出 Inno Setup 安装包。
* **Linux / macOS**：仓库提供 `tools/pack_crossplatform.py` ——
  发布 Avalonia 前端 → 装入后端（`--backend-mode standalone`）→ 清理调试符号 →
  写顶层启动脚本 `start.sh` → 产出 `.tar.gz` → 写 `SHA256SUMS.txt` 与 `manifest-<rid>.json`。
  两个打包脚本的默认输出目录不同（`pack_backend.py` 用 `publish/`、本脚本用 `dist/`），
  所以本脚本**两处都会找** standalone 后端产物，按各自默认值连着跑就能成功。
* **顶层启动脚本**：发布目录是三层的（根 / `app/` / 程序文件），这样 `UserPaths.DataRoot`
  才能认出"便携布局、用户数据放外层"。代价是用户解压后看到的是一堆文档和一个 `app/` 目录，
  所以脚本会在根目录放一个 `start.sh`，只做一件事：把 `app/EchoDesk` 接管过去
  （`exec`，信号与退出码原样透传）。
* **CI 矩阵**：`.github/workflows/ci.yml` 里有两个跨平台作业，分工不同 ——
  `cross-platform` 在 Windows / Ubuntu / macOS 三个 runner 上各**编译 + 发布 + 跑无窗口自检**，
  证明 Avalonia 的 TFM 是平台中立的；`cross-platform-release` 则真的**打出三个平台的完整发布物**
  （`pack_backend.py` → `check_standalone_backend.py` → `pack_crossplatform.py`），
  因为"交叉准备好的依赖在目标平台上到底能不能起来"，只有在目标平台上真跑一次才算验证过。
* **macOS 签名与公证**：需要维护者的 Developer ID 证书与 Apple 账号，
  脚本**不擅自执行**，而是把命令与缺失步骤写进产物里的 `SIGNING.txt`。
  在拿到证书前，macOS 用户需要右键打开或 `xattr -dr com.apple.quarantine` 解除隔离。
* **AppImage**：需要 `appimagetool` 与 AppDir 结构，当前以 `.tar.gz` 为准（未完成项见
  `records/v0.2.2-跨平台记录.md`）。

## 七、验证证据（如实说明）

开发机只有 Windows（无 WSL、无 Docker、无 macOS），所以证据分五层，各层能证明什么
写在下面，**不把构建级证据说成运行级证据**：

| 层次 | 做法 | 能证明什么 |
| --- | --- | --- |
| 真实运行 | Windows 上跑 `EchoDesk.exe --selftest`：平台探测 → 按平台规则拉起后端 → `ready` 握手 → `platform`/`capability` 往返 | Windows 上整条链路真的通 |
| **打包后后端** | `python tools\check_standalone_backend.py`：结构 / 启动脚本语法 / 解释器真能跑 / 走一遍 JSON 行协议 / **真实动作集**（`commands`、`capability`、`context`） | 三平台产物**结构齐全**；Windows 上连"包内解释器能跑、能应答多个真实动作"都实证了 |
| **非本机平台的静态二进制检查** | 对 `osx-arm64` 校验 `llama_cpp/lib/*.dylib` 的文件头是不是合法 Mach-O | 能在打包机上挡住"上游轮子本身是坏的"这类缺陷（本轮的坏包正是这样被发现的） |
| **完整发布物** | `python tools\check_crossplatform_release.py`：归档格式是否匹配目标平台、`tar.gz` 里关键文件有没有可执行位、目录条目顺序、顶层启动脚本与 `SIGNING.txt` 在不在、`SHA256SUMS.txt` 与清单是否一致 | 用户拿到手的东西**能被正确解开并启动**（这些恰恰是"日志全绿、用户却用不了"的类型） |
| CI | 两个矩阵：`cross-platform`（三平台编译 + 发布 + 无窗口自检）与 `cross-platform-release`（三平台真的打出完整发布物） | 真实的 Linux/macOS 运行证据只能在这里拿到 |

**当前缺口（诚实记录）**：仓库内**没有**来自真实 Linux/macOS 的运行证据 ——
本轮三平台后端与发布物都是在 Windows 打包机上交叉准备并做静态/结构级校验的；
非 Windows 上的真实交互（窗口、字体、拖拽、输入法）也还没被人工验证过。
CI 矩阵是"已写好、待运行"。**不要把上面第 2~4 层当成"在 Linux/macOS 上跑过了"。**

## 八、前端实现差异（WPF → Avalonia）

迁移不是"换个皮肤"，以下几处是**实现方式必须改**的：

| 方面 | WPF | Avalonia | 影响 |
| --- | --- | --- | --- |
| 富文本 | `FlowDocument` + `Inline` | Markdown 解析成控件树（段落/表格/代码块都是控件） | 表格用 `Grid` 画、代码块带独立复制按钮；选择是"按段落"而不是整篇 |
| 链接 | 行内 Run 可挂 ToolTip 显示 URL | 行内 Run 不是控件，挂不了 ToolTip | URL 附在链接文本后（信息不丢，但样式不同） |
| 字号缩放 | 改 `FlowDocument` 一处字号 | 消息区按新字号重渲染 | 行为一致，实现从"改属性"变成"重建" |
| 主题 | 自己维护两套色板 + `DynamicResource` | `FluentTheme` + `ThemeVariant` | 色板键与对比度约束仍沿用 `tools/check_theme.py` 的口径 |
| 拖放 | `DataObject` / `DataFormats.Files` | Avalonia 12 的 `DataTransfer` API | 代码需改写（旧 API 已移除） |
| 剪贴板 | `Clipboard.SetText` | `IClipboard.SetTextAsync`（扩展方法） | 变为异步调用 |
| 危险指令确认 | `MessageBox` 模态框 | 无内置 MessageBox | 改成"同一个输入框里再按一次 Enter"（行为等价、形式不同） |
| 弹层 | `Popup` + `StaysOpen` | `Popup` **没有** `StaysOpen` 属性 | 改为手动开合；`ToolTip` 也必须用 `ToolTip.SetTip()` 附加属性 |

## 九、未完成项

完整清单见 `records/v0.2.2-跨平台-2记录.md` 的"未完成项"与第十二节，摘要如下：

* **没有真实 Linux/macOS 运行证据**：三平台产物是在 Windows 上交叉准备的，只有
  结构级、静态二进制级与 Windows 上的协议级证据（见第七节）。
* **Windows 正式安装包仍走 PyInstaller**：WPF 前端的启动路径只接受一个可执行文件，
  而 standalone 在 Windows 上没有收益 —— 刻意的取舍，不是遗漏。
* macOS 公证（`notarytool`）与 Linux AppImage 未自动化：需要 Apple 证书与 `appimagetool`，
  脚本只把命令与缺失步骤写进 `SIGNING.txt`，**不擅自执行**。
* **构建不可复现**：三处时间戳漂移（`USAGE.txt`、rcedit 写 `.rsrc`、PyInstaller 写 PE 头）
  会让"同一提交重建"得到不同哈希；`SOURCE_DATE_EPOCH` 尚未落地。
* `docs/SBOM.json` 与第三方许可清单**未按 v0.2.2 重新生成**（依赖闭包结构未变）。
* 非 Windows 的图标、字体回退、输入法、高 DPI 等真实交互未验证。
