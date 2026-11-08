// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/BackendService.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/BackendService.cs。类型与成员说明见下方注释。
// [EN] Backend subprocess management and the JSON line protocol. Ported from
// [EN] frontend/Services/BackendService.cs (development task 2 of the design
// [EN] specification).
// [ZH] 后端子进程管理与 JSON 行协议（移植自 frontend/Services/BackendService.cs，
// [ZH] 设计规范研发任务 2）。
//
// [EN] This is the core evidence for "reuse instead of rewrite": the protocol layer
// [EN] (write one JSON line to stdin, read one JSON line from stdout, the single-request
// [EN] gate, the idle timeout, and the dual-channel streaming delta) had already been
// [EN] validated for four rounds in the WPF version, so the cross-platform version
// [EN] copies it **paragraph by paragraph**, changing only three platform-specific
// [EN] points:
// [ZH] 这是“复用而不是重写”的核心证据：协议层（stdin 写一行 JSON、stdout 读一行 JSON、
// [ZH] 单请求闸门、空闲超时、流式 delta 双通道）在 WPF 版已经验证过 4 轮，跨平台版
// [ZH] **逐段照搬**，只改三处与平台有关的地方：
//
// [EN]   1. **Backend resolution** (ResolveLaunch): no longer the hard-coded
// [EN]      EchoDeskBackend.exe. Each of the three platforms looks for
// [EN]      `EchoDeskBackend[.exe]`; when no packaged artifact is found, it falls back
// [EN]      to "interpreter + backend/main.py" -- this is what lets the prototype run
// [EN]      on Linux/macOS without a PyInstaller artifact.
// [ZH]   1. **后端定位**（ResolveLaunch）：不再是写死的 EchoDeskBackend.exe。三个平台分别
// [ZH]      查找 `EchoDeskBackend[.exe]`；找不到打包产物时回退到“解释器 + backend/main.py”
// [ZH]      —— 这一条让原型在没有 PyInstaller 产物的 Linux/macOS 上也能真正跑起来。
// [EN]   2. **Subprocess environment variables**: under a C/POSIX locale on non-Windows
// [EN]      systems, Python encodes stdout as ASCII; feeding it Chinese/emoji directly
// [EN]      raises UnicodeEncodeError and kills the backend. The front end therefore
// [EN]      injects PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 explicitly (harmless on
// [EN]      Windows).
// [ZH]   2. **子进程环境变量**：Python 在非 Windows 的 C/POSIX locale 下 stdout 编码是
// [ZH]      ASCII，直接吃中文/emoji 会 UnicodeEncodeError 把后端打死。所以显式注入
// [ZH]      PYTHONUTF8=1 / PYTHONIOENCODING=utf-8（Windows 上无害）。
// [EN]   3. **Behavior when the backend is missing**: instead of throwing and crashing
// [EN]      the front end, it returns a start result carrying `Command` and `Error`, so
// [EN]      the UI can show "backend missing + what to do", the degraded message
// [EN]      required by development task 5.
// [ZH]   3. **找不到后端时的行为**：不抛异常崩前台，而是返回带 `Command` 与 `Error` 的
// [ZH]      启动结果 —— 让界面显示“后端缺失 + 怎么办”，这正是研发任务 5 要求的降级文案。
//
// [EN] The rest of the design is unchanged: a single stdout pump -> Channel<string> ->
// [EN] a single-request gate serializing requests, because stdout is one shared stream
// [EN] and concurrent requests would read each other's responses.
// [ZH] 其余设计照旧：stdout 单写泵 → Channel<string> → 单请求闸门串行化，因为 stdout 是
// [ZH] 一条共享流，并发请求会互相读错响应。
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Channels;
using System.Threading.Tasks;

namespace EchoDesk.Avalonia.Services;

/// <summary>
/// [EN] Request queue timeout (the previous reply is still being generated).
/// [ZH] 请求队列超时（上一条回复还在生成时）。
/// </summary>
public sealed class BackendBusyException : Exception
{
    public BackendBusyException(string message) : base(message) { }
}

/// <summary>
/// [EN] Idle timeout (the backend produced no output within the threshold).
/// [ZH] 空闲超时（后端在阈值内没有任何输出）。
/// </summary>
public sealed class BackendTimeoutException : Exception
{
    public BackendTimeoutException(string message) : base(message) { }
}

/// <summary>
/// [EN] Backend launch mode (packaged artifact or interpreter script).
/// [ZH] 后端启动方式（打包产物 or 解释器脚本）。
/// </summary>
public sealed class BackendLaunch
{
    public required string Executable { get; init; }
    public required string[] Args { get; init; }
    public required string WorkDirectory { get; init; }

    /// <summary>
    /// [EN] Human-readable launch command (printed during self-check and included in
    /// [EN] error messages so users can diagnose the problem themselves).
    /// [ZH] 给人看的启动命令（自检打印、错误信息里带上，便于用户自查）。
    /// </summary>
    public string CommandLine => Args.Length == 0
        ? Executable
        : Executable + " " + string.Join(' ', Args.Select(a => a.Contains(' ') ? '"' + a + '"' : a));

    /// <summary>
    /// [EN] Whether the "interpreter + main.py" path is used (unpackaged
    /// [EN] development/source run).
    /// [ZH] 是否走“解释器 + main.py”路径（未打包的开发/源码运行）。
    /// </summary>
    public bool IsScriptMode { get; init; }

    /// <summary>
    /// [EN] Extra subprocess environment variables (v0.2.1 cross-platform batch 2,
    /// [EN] development task 2).
    /// [EN] Currently only the bundled standalone Python uses it to inject PYTHONPATH:
    /// [EN] dependencies live in `backend/site-packages/`, **side by side** with the
    /// [EN] interpreter rather than merged into its site-packages, so the interpreter
    /// [EN] must be told explicitly where to find them.
    /// [ZH] 额外的子进程环境变量（v0.2.1 跨平台-2 研发任务 2）。
    /// [ZH] 目前只有“包内 standalone Python”用它注入 PYTHONPATH —— 依赖装在
    /// [ZH] `backend/site-packages/`，与解释器**并排**而不是合并进解释器的 site-packages，
    /// [ZH] 所以必须显式告诉解释器去哪找它们。
    /// </summary>
    public IReadOnlyDictionary<string, string> ExtraEnvironment { get; init; }
        = new Dictionary<string, string>();
}

/// <summary>
/// [EN] Start result. Failure does not throw -- after the cross-platform move,
/// [EN] "backend missing" is an expected normal state that must be shown to the user.
/// [ZH] 启动结果。失败不抛异常 —— 跨平台后“后端缺失”是可预期的正常状态，要能显示给用户。
/// </summary>
public sealed class StartResult
{
    public bool Ok { get; init; }
    public string Command { get; init; } = "";
    public string Error { get; init; } = "";
}

public sealed class BackendService : IDisposable
{
    private Process? _process;
    private StreamWriter? _writer;
    private StreamReader? _reader;
    private StreamReader? _stdErrReader;
    private CancellationTokenSource? _pumpCts;
    private Task? _pumpTask;
    private bool _stopping;

    // [EN] stdout line queue: single writer (the pump), multiple readers (the gate
    // [EN] guarantees only one reader at a time).
    // [ZH] stdout 行队列：单写（泵）多读（闸门保证同时只有一个读取方）。
    private readonly Channel<string> _lines = Channel.CreateUnbounded<string>(
        new UnboundedChannelOptions { SingleWriter = true, SingleReader = false });

    // [EN] Request gate: stdout is one shared stream, so concurrent requests read each
    // [EN] other's responses; every "write request + read response" pair is serialized.
    // [ZH] 请求闸门：stdout 是单条共享流，并发请求会互相读错响应，所有“写请求 + 读响应”
    // [ZH] 串行化。
    private readonly SemaphoreSlim _requestGate = new(1, 1);

    /// <summary>
    /// [EN] Default threshold for waiting for the gate to be released: beyond it the
    /// [EN] backend is considered busy.
    /// [ZH] 默认等待闸门释放的阈值：超过即认为后端忙。
    /// </summary>
    public static readonly TimeSpan DefaultQueueTimeout = TimeSpan.FromSeconds(15);

    /// <summary>
    /// [EN] Default response wait threshold (idle timeout: the timer restarts on every
    /// [EN] received line).
    /// [ZH] 默认等待响应的阈值（空闲超时：每收到一行即重新计时）。
    /// </summary>
    public static readonly TimeSpan DefaultIdleTimeout = TimeSpan.FromMinutes(10);

    /// <summary>
    /// [EN] Response threshold for heavy tasks (parsing large files, model downloads,
    /// [EN] long Agent tasks).
    /// [ZH] 重任务（解析大文件、模型下载、Agent 长任务）的响应阈值。
    /// </summary>
    public static readonly TimeSpan HeavyIdleTimeout = TimeSpan.FromMinutes(45);

    private readonly BackendLaunch? _launch;

    public BackendService(string? backendPath = null)
    {
        _launch = backendPath is null ? ResolveLaunch() : BuildLaunchFromPath(backendPath);
    }

    /// <summary>
    /// [EN] Path of the backend executable/interpreter (null when not found).
    /// [ZH] 后端可执行文件/解释器的路径（未找到时为 null）。
    /// </summary>
    public string? BackendPath => _launch?.Executable;

    /// <summary>
    /// [EN] Backend working directory: config.json, session.json, models/, and
    /// [EN] skills_dev/ all live under it.
    /// [ZH] 后端工作目录：config.json、session.json、models/、skills_dev/ 都在该目录下。
    /// </summary>
    public string? BackendDirectory => _launch?.WorkDirectory;

    /// <summary>
    /// [EN] Launch failure reason (shown in the UI banner).
    /// [ZH] 启动失败原因（界面横幅显示它）。
    /// </summary>
    public string? LaunchError { get; private set; }

    /// <summary>
    /// [EN] Failure reason of the most recent request (timeout/interruption), for
    /// [EN] display in the UI.
    /// [ZH] 最近一次请求的失败原因（超时/中断），供界面展示。
    /// </summary>
    public string? LastError { get; private set; }

    /// <summary>
    /// [EN] Every raw stdout line (for self-check and troubleshooting). **Not part of
    /// [EN] protocol handling**; it is only a side observation point.
    /// [ZH] 每一行原始 stdout（自检与排障用）。**不参与协议处理**，只是旁路观察点。
    /// </summary>
    public event Action<string>? LineReceived;

    public string? Version { get; private set; }
    public string? Studio { get; private set; }
    public int RestoredCount { get; private set; }

    /// <summary>
    /// [EN] Session history restored at startup (role/content/reasoning).
    /// [ZH] 启动时恢复的会话历史（role/content/reasoning）。
    /// </summary>
    public List<(string Role, string Content, string Reasoning)> RestoredHistory { get; } = new();

    /// <summary>
    /// [EN] Whether the backend has loaded a model (false = the UI works but has no
    /// [EN] inference capability).
    /// [ZH] 后端是否已加载模型（false = 界面可用但没有推理能力）。
    /// </summary>
    public bool ModelReady { get; private set; } = true;

    public string ModelsDir { get; private set; } = "";
    public string ModelGuide { get; private set; } = "";
    public string LoadedModel { get; private set; } = "";

    /// <summary>
    /// [EN] Platform information reported by the backend (ready.platform) -- used to
    /// [EN] verify that the front and back ends agree on platform detection.
    /// [ZH] 后端上报的平台信息（ready.platform）—— 用来校验前后端平台判断一致。
    /// </summary>
    public string BackendPlatformKind { get; private set; } = "";

    /// <summary>
    /// [EN] Whether the backend emits emoji (ready.emoji).
    /// [ZH] 后端是否输出 emoji（ready.emoji）。
    /// </summary>
    public bool BackendEmoji { get; private set; } = true;

    /// <summary>
    /// [EN] List of actions available in the backend (ready.commands).
    /// [ZH] 后端可用动作清单（ready.commands）。
    /// </summary>
    public List<string> Commands { get; } = new();

    public event Action<bool>? ModelReadyChanged;
    public event Action<string>? StderrReceived;
    public event Action<int>? ProcessExited;

    public bool IsRunning => _process is { HasExited: false };

    // [EN] ---------- Backend resolution ----------
    // [ZH] ---------- 后端定位 ----------

    /// <summary>
    /// [EN] Resolves the backend according to platform rules. Order (the first existing
    /// [EN] one wins):
    /// [EN]   1. Environment variable ECHODESK_BACKEND (explicit absolute path for
    /// [EN]      self-check/development)
    /// [EN]   2. &lt;program directory&gt;/backend/EchoDeskBackend[.exe] (v0.2.x release
    /// [EN]      layout)
    /// [EN]   3. &lt;program directory&gt;/EchoDeskBackend[.exe] (flat layout)
    /// [EN]   4. &lt;program directory&gt;/EchoBackend[.exe] (v0.1.0 layout compatibility)
    /// [EN]   5. **Interpreter + backend/main.py**: searches up to 6 levels upward from
    /// [EN]      the program directory for backend/main.py (development layout:
    /// [EN]      bin/Release/net10.0/... -> repository root). This step is the key to
    /// [EN]      running the prototype on Linux/macOS, where no PyInstaller artifact
    /// [EN]      exists.
    /// [ZH] 按平台规则定位后端。顺序（前者存在就用前者）：
    /// [ZH]   1. 环境变量 ECHODESK_BACKEND（自检/开发指定绝对路径）
    /// [ZH]   2. &lt;程序目录&gt;/backend/EchoDeskBackend[.exe]（v0.2.x 发布布局）
    /// [ZH]   3. &lt;程序目录&gt;/EchoDeskBackend[.exe]（扁平布局）
    /// [ZH]   4. &lt;程序目录&gt;/EchoBackend[.exe]（兼容 v0.1.0 布局）
    /// [ZH]   5. **解释器 + backend/main.py**：从程序目录向上最多 6 层找 backend/main.py
    /// [ZH]      （开发布局：bin/Release/net10.0/… → 仓库根）。这一步是原型能在
    /// [ZH]      Linux/macOS 上真跑起来的关键 —— 那些平台没有 PyInstaller 产物。
    /// </summary>
    public static BackendLaunch? ResolveLaunch()
    {
        string exeName = PlatformInfo.Current.BackendExecutableName;

        string? overridePath = Environment.GetEnvironmentVariable("ECHODESK_BACKEND");
        if (!string.IsNullOrEmpty(overridePath) && File.Exists(overridePath))
            return BuildLaunchFromPath(overridePath);

        string dir = AppContext.BaseDirectory;
        var exeCandidates = new List<string>
        {
            Path.Combine(dir, "backend", exeName),
            Path.Combine(dir, exeName),
            Path.Combine(dir, "EchoBackend" + (PlatformInfo.Current.IsWindows ? ".exe" : "")),
        };
        foreach (string candidate in exeCandidates)
        {
            if (File.Exists(candidate))
                return new BackendLaunch { Executable = candidate, Args = Array.Empty<string>(), WorkDirectory = Path.GetDirectoryName(candidate)! };
        }

        // [EN] Bundled standalone Python (v0.2.1 cross-platform batch 2, development
        // [EN] task 2): the backend ships with a relocatable CPython, so **no**
        // [EN] executable has to be compiled on the target platform. It must be tried
        // [EN] before "search upward for main.py" -- the release tree also has an app/
        // [EN] directory, and the wrong order picks the wrong target in a development
        // [EN] layout.
        // [ZH] 包内 standalone Python（v0.2.1 跨平台-2 研发任务 2）：后端随包携带一个
        // [ZH] 可重定位的 CPython，因此**不需要**在目标平台上编译出可执行文件。必须排在
        // [ZH] “向上找 main.py”之前 —— 发布树里也有 app/ 目录，顺序反了会在开发布局下
        // [ZH] 选错目标。
        BackendLaunch? bundled = TryBundledStandalone(dir);
        if (bundled is not null)
            return bundled;

        // [EN] Source run: look for main.py.
        // [ZH] 源码运行：找 main.py。
        string? scratch = dir;
        for (int depth = 0; depth < 6 && !string.IsNullOrEmpty(scratch); depth++)
        {
            string[] scriptCandidates =
            {
                Path.Combine(scratch, "backend", PlatformInfo.Current.BackendScriptName),
                Path.Combine(scratch, PlatformInfo.Current.BackendScriptName),
            };
            foreach (string script in scriptCandidates)
            {
                if (!File.Exists(script))
                    continue;
                (string FileName, string[] PrefixArgs)? python = PlatformInfo.Current.FindPython();
                if (python is null)
                {
                    // [EN] Script present but no interpreter: state this error clearly
                    // [EN] (far more useful than "backend not found").
                    // [ZH] 有脚本但没解释器：把这条错误说清楚（比“找不到后端”有用得多）。
                    return null;
                }
                var args = new List<string>(python.Value.PrefixArgs) { "-u", script };
                return new BackendLaunch
                {
                    Executable = python.Value.FileName,
                    Args = args.ToArray(),
                    WorkDirectory = Path.GetDirectoryName(script)!,
                    IsScriptMode = true,
                };
            }
            scratch = Path.GetDirectoryName(scratch.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        }
        return null;
    }

    /// <summary>
    /// [EN] Backend layout for the bundled standalone Python (v0.2.1 cross-platform
    /// [EN] batch 2, development task 2; produced by tools/pack_backend.py):
    /// [EN]   <c>backend/python/**</c>        bundled CPython (the target machine
    /// [EN]                                    **does not need** Python installed)
    /// [EN]   <c>backend/site-packages/**</c> dependencies downloaded by pip (side by
    /// [EN]                                    side with the interpreter, not merged)
    /// [EN]   <c>backend/app/**</c>           backend code (contents of backend/)
    ///
    /// [EN] Independent of the PyInstaller layout
    /// [EN] (<c>backend/EchoDeskBackend[.exe]</c>): when both exist, the executable wins
    /// [EN] -- it is the official Windows delivery form and has been fully validated.
    ///
    /// [EN] **Why the front end calls the interpreter directly instead of
    /// [EN] run-backend.bat / run-backend.sh**: an extra shell layer turns "stop the
    /// [EN] backend" into "kill the shell while Python stays alive", whereas the front
    /// [EN] end manages the backend as a subprocess (killed detection, exit code,
    /// [EN] stdin/stdout pipes). The launch scripts are still provided for manual
    /// [EN] startup/debugging.
    /// [ZH] 包内 standalone Python 的后端布局（v0.2.1 跨平台-2 研发任务 2，由
    /// [ZH] tools/pack_backend.py 产出）：
    /// [ZH]   <c>backend/python/**</c>        随包携带的 CPython（目标机**无需**安装 Python）
    /// [ZH]   <c>backend/site-packages/**</c> pip 交叉下载的依赖（与解释器并排，不合并进去）
    /// [ZH]   <c>backend/app/**</c>          后端代码（backend/ 的内容）
    ///
    /// [ZH] 与 PyInstaller 布局（<c>backend/EchoDeskBackend[.exe]</c>）互不干扰：两者都在时
    /// [ZH] 优先用可执行文件 —— 那是 Windows 正式交付形态，经过完整验证。
    ///
    /// [ZH] **为什么前端直接调解释器，而不是调 run-backend.bat / run-backend.sh**：
    /// [ZH] 中间多一层 shell 会让“停止后端”变成“杀掉 shell，Python 还活着”，
    /// [ZH] 而前端是按子进程管理后端的（killed 判定、退出码、stdin/stdout 管道）。
    /// [ZH] 启动脚本仍然提供，用途是给人手动启动/调试。
    /// </summary>
    private static BackendLaunch? TryBundledStandalone(string dir)
    {
        string backendDir = Path.Combine(dir, "backend");
        string python = PlatformInfo.Current.IsWindows
            ? Path.Combine(backendDir, "python", "python.exe")
            : Path.Combine(backendDir, "python", "bin", "python3");
        string appDir = Path.Combine(backendDir, "app");
        string script = Path.Combine(appDir, PlatformInfo.Current.BackendScriptName);
        if (!File.Exists(python) || !File.Exists(script))
            return null;

        string sitePackages = Path.Combine(backendDir, "site-packages");
        var env = new Dictionary<string, string>
        {
            // [EN] Dependencies first, backend code second: this way modules in app/
            // [EN] cannot accidentally shadow third-party packages (the backend
            // [EN] directory is a flat pile of .py files; watch out where name
            // [EN] collisions have occurred).
            // [ZH] 依赖在前、后端代码在后：这样 app/ 里的模块不会意外遮蔽第三方包
            // [ZH] （后端目录是扁平的一堆 .py，出现过同名风险的地方要小心）。
            ["PYTHONPATH"] = sitePackages + Path.PathSeparator + appDir,
            // [EN] The release directory may be read-only; .pyc files are runtime
            // [EN] residue anyway (both the uninstall list and the gate handle them).
            // [ZH] 发布目录可能只读；.pyc 又属于“运行期残留”（卸载清单与门禁都要处理它）。
            ["PYTHONDONTWRITEBYTECODE"] = "1",
        };
        // [EN] **The working directory must be backend/, not backend/app/** (correction
        // [EN] in v0.2.2, full bilingual support and three-platform friendliness).
        // [EN]
        // [EN] Why (cross-platform inconsistency found in testing): the backend resolves
        // [EN] all relative paths against the **working directory** -- `models_dir`
        // [EN] defaults to "models" (backend/model_hub.py), `skills_dev`
        // [EN] (backend/skills_runtime.py). The Windows WPF front end set the working
        // [EN] directory to the backend exe directory (`…\app\backend\`), so the model
        // [EN] directory was `…\app\backend\models\`; this code used to pass
        // [EN] `backend/app`, which made the same application use
        // [EN] `…/app/backend/app/models/` on Linux/macOS -- a .gguf file placed
        // [EN] according to the documentation "was in the right place but was not
        // [EN] recognized".
        // [EN] After unifying on backend/, the model and plugin directories on all
        // [EN] three platforms live under `app/backend/`.
        // [EN]
        // [EN] Note that the change to `WorkDirectory` **does not affect the user data
        // [EN] root**: `user_data_root()` in `backend/config.py` derives it from the
        // [EN] location of config.py (skipping container directories such as
        // [EN] app/backend), independent of the process working directory.
        // [ZH] **工作目录必须是 backend/，不是 backend/app/**（v0.2.2 全面双语与三平台友好
        // [ZH] 修正）。
        // [ZH]
        // [ZH] 为什么（实测发现的跨平台不一致）：后端的相对路径都是相对**工作目录**解析的 ——
        // [ZH] `models_dir` 默认 "models"（backend/model_hub.py）、`skills_dev`
        // [ZH] （backend/skills_runtime.py）。Windows 的 WPF 前端把工作目录设为后端 exe
        // [ZH] 所在目录（`…\app\backend\`），所以模型目录是 `…\app\backend\models\`；
        // [ZH] 而这里原来传的是 `backend/app`，于是同一个应用在 Linux/macOS 上把模型目录
        // [ZH] 变成 `…/app/backend/app/models/` —— 用户按文档放的 .gguf 会“放对了地方
        // [ZH] 却不被识别”。
        // [ZH] 统一成 backend/ 之后，三平台的模型目录与插件目录都落在 `app/backend/` 下。
        // [ZH]
        // [ZH] 注意 `WorkDirectory` 的变化**不影响用户数据根目录**：那个由
        // [ZH] `backend/config.py` 的 `user_data_root()` 按 config.py 的位置推导
        // [ZH] （跳过 app/backend 这些容器目录），与进程的工作目录无关。
        return new BackendLaunch
        {
            Executable = python,
            Args = new[] { "-u", Path.Combine("app", Path.GetFileName(script)) },
            WorkDirectory = backendDir,
            IsScriptMode = true,
            ExtraEnvironment = env,
        };
    }

    private static BackendLaunch BuildLaunchFromPath(string path)
    {
        // [EN] An explicit path was given: run the exe directly; run a .py through the
        // [EN] interpreter.
        // [ZH] 显式给了路径：exe 直接跑；.py 用解释器跑。
        if (path.EndsWith(".py", StringComparison.OrdinalIgnoreCase))
        {
            (string FileName, string[] PrefixArgs)? python = PlatformInfo.Current.FindPython();
            string exe = python?.FileName ?? "python3";
            var args = new List<string>(python?.PrefixArgs ?? Array.Empty<string>()) { "-u", path };
            return new BackendLaunch
            {
                Executable = exe,
                Args = args.ToArray(),
                WorkDirectory = Path.GetDirectoryName(Path.GetFullPath(path))!,
                IsScriptMode = true,
            };
        }
        return new BackendLaunch
        {
            Executable = path,
            Args = Array.Empty<string>(),
            WorkDirectory = Path.GetDirectoryName(Path.GetFullPath(path))!,
        };
    }

    /// <summary>
    /// [EN] Gives the full explanation for "backend not found" (the UI banner uses
    /// [EN] this text directly).
    /// [ZH] 给出“找不到后端”的完整解释（界面横幅直接用这段话）。
    /// </summary>
    public static string DescribeMissingBackend()
    {
        string exeName = PlatformInfo.Current.BackendExecutableName;
        (string FileName, string[] PrefixArgs)? python = PlatformInfo.Current.FindPython();
        var lines = new List<string>
        {
            Loc.T("main.error.backendMissingHeader",
                "未找到后端：本平台（{0}）期望的后端文件是 backend/{1}。",
                PlatformInfo.Current.DisplayName, exeName),
            Loc.T("main.error.backendMissingSearched", "已查找的位置："),
            Loc.T("main.error.backendMissingEnv", "  · 环境变量 ECHODESK_BACKEND 指定的路径"),
            $"  · {Path.Combine(AppContext.BaseDirectory, "backend", exeName)}",
            $"  · {Path.Combine(AppContext.BaseDirectory, exeName)}",
            Loc.T("main.error.backendMissingSource",
                "  · 从程序目录向上 6 层的 backend/{0}（源码运行方式）",
                PlatformInfo.Current.BackendScriptName),
        };
        lines.Add(python is null
            ? Loc.T("main.error.backendMissingNoPython",
                "本机没有可用的 Python 解释器（已试 python3 / python），因此也无法用源码方式启动后端。")
            : Loc.T("main.error.backendMissingHasPython", "本机有 Python（{0}），但没找到 backend/{1}。",
                python.Value.FileName, PlatformInfo.Current.BackendScriptName));
        lines.Add(Loc.T("main.error.backendMissingHowto",
            "处理办法：安装对应平台的发布包，或把 ECHODESK_BACKEND 指向打包好的后端可执行文件。"));
        return string.Join(Environment.NewLine, lines);
    }

    // [EN] ---------- Startup / reading ----------
    // [ZH] ---------- 启动 / 读取 ----------

    /// <summary>
    /// [EN] Starts the backend subprocess. **Does not wait for ready** (the caller
    /// [EN] decides whether to wait; the UI can draw first and wait later).
    /// [ZH] 启动后端子进程。**不等待 ready**（调用方决定要不要等；界面可以先画出来再等）。
    /// </summary>
    public Task<StartResult> StartAsync(bool verbose = false, CancellationToken cancellationToken = default)
    {
        if (_launch is null)
        {
            string error = DescribeMissingBackend();
            LaunchError = error;
            if (verbose)
                Console.WriteLine("后端启动失败：" + error);
            return Task.FromResult(new StartResult { Ok = false, Command = "", Error = error });
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = _launch.Executable,
            WorkingDirectory = _launch.WorkDirectory,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (string arg in _launch.Args)
            startInfo.ArgumentList.Add(arg);

        // [EN] Extra environment for the bundled standalone Python (PYTHONPATH etc.),
        // [EN] see TryBundledStandalone.
        // [ZH] 包内 standalone Python 的额外环境（PYTHONPATH 等），见 TryBundledStandalone。
        foreach (var pair in _launch.ExtraEnvironment)
            startInfo.Environment[pair.Key] = pair.Value;

        // [EN] Python-side encoding: under a POSIX/C locale, Python encodes stdout as
        // [EN] ASCII by default; Chinese and emoji raise UnicodeEncodeError and kill the
        // [EN] backend (harmless on Windows).
        // [ZH] Python 侧编码：POSIX/C locale 下 Python 默认用 ASCII 编码 stdout，
        // [ZH] 中文与 emoji 会直接抛 UnicodeEncodeError 把后端打死（Windows 上无害）。
        startInfo.Environment["PYTHONUTF8"] = "1";
        startInfo.Environment["PYTHONIOENCODING"] = "utf-8";
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";
        // [EN] The backend needs to know it was launched by the front end (used later
        // [EN] for "exclusive model").
        // [ZH] 后端要知道自己是被前端拉起来的（将来做“独占模型”时用）。
        startInfo.Environment["ECHODESK_FRONTEND"] = "avalonia";

        try
        {
            _process = Process.Start(startInfo);
        }
        catch (Exception ex)
        {
            LaunchError = Loc.T("main.error.startProcessFailedDetail",
                "启动后端进程失败：{0}（命令：{1}）", ex.Message, _launch.CommandLine);
            if (verbose)
                Console.WriteLine(LaunchError);
            return Task.FromResult(new StartResult { Ok = false, Command = _launch.CommandLine, Error = LaunchError });
        }
        if (_process is null)
        {
            // [EN] Reuse the same key as the WPF side ("Failed to start the backend
            // [EN] process."): both sides share one set of error messages, so do not
            // [EN] write another copy.
            // [ZH] 复用 WPF 侧的同一个键（“无法启动后端进程。”）：两侧同一批错误文案，
            // [ZH] 别再各写一份。
            LaunchError = Loc.T("main.error.startProcessFailed", "无法启动后端进程。");
            return Task.FromResult(new StartResult { Ok = false, Command = _launch.CommandLine, Error = LaunchError });
        }

        _process.EnableRaisingEvents = true;
        _process.Exited += Process_Exited;
        _writer = _process.StandardInput;
        // [EN] The protocol carries Chinese/emoji and is always UTF-8: read it explicitly
        // [EN] as UTF-8 to avoid mojibake on cp1252/GBK machines.
        // [ZH] 协议含中文/emoji 且始终 UTF-8：显式按 UTF-8 读取，避免 cp1252/GBK 机器上乱码。
        var utf8 = new UTF8Encoding(false);
        _reader = new StreamReader(_process.StandardOutput.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);
        _stdErrReader = new StreamReader(_process.StandardError.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);

        _pumpCts = new CancellationTokenSource();
        _pumpTask = Task.Run(() => PumpAsync(_pumpCts.Token));
        _ = Task.Run(ReadErrorsAsync);

        if (verbose)
            Console.WriteLine($"后端已启动：{_launch.CommandLine}（工作目录 {_launch.WorkDirectory}）");
        return Task.FromResult(new StartResult { Ok = true, Command = _launch.CommandLine, Error = "" });
    }

    /// <summary>
    /// [EN] stdout pump: continuously reads and delivers lines, and closes the queue
    /// [EN] when the process exits or is stopped.
    /// [ZH] stdout 泵：持续读取并投递，进程结束或停止时关闭队列。
    /// </summary>
    private async Task PumpAsync(CancellationToken cancellationToken)
    {
        try
        {
            while (true)
            {
                string? line = await _reader!.ReadLineAsync(cancellationToken);
                if (line is null)
                    break;
                LineReceived?.Invoke(line);
                _lines.Writer.TryWrite(line);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or OperationCanceledException)
        {
            // [EN] A read interruption caused by process exit or a manual stop is
            // [EN] normal.
            // [ZH] 进程结束/主动停止导致的读取中断属正常现象。
        }
        finally
        {
            _lines.Writer.TryComplete();
        }
    }

    private async Task ReadErrorsAsync()
    {
        try
        {
            while (_stdErrReader is not null)
            {
                string? line = await _stdErrReader.ReadLineAsync();
                if (line is null)
                    break;
                StderrReceived?.Invoke(line);
            }
        }
        catch (Exception)
        {
            // [EN] A stderr read failure does not affect the main flow.
            // [ZH] stderr 读取失败不影响主流程。
        }
    }

    /// <summary>
    /// [EN] Waits for the ready handshake; returns null on timeout or when the process
    /// [EN] exits before ready (does not throw, which is easier for self-check and UI).
    /// [ZH] 等待 ready 握手；超时或未就绪退出返回 null（不抛，自检与界面都好处理）。
    /// </summary>
    public async Task<JsonDocument?> WaitForReadyAsync(TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            TimeSpan remain = deadline - DateTime.UtcNow;
            if (remain <= TimeSpan.Zero)
            {
                LastError = Loc.T("main.error.startupTimeout", "等待后端就绪超时（{0} 秒）。",
                    timeout.TotalSeconds);
                return null;
            }
            string? line;
            try
            {
                line = await ReadLineAsync(remain, cancellationToken);
            }
            catch (BackendTimeoutException)
            {
                LastError = Loc.T("main.error.startupTimeout", "等待后端就绪超时（{0} 秒）。",
                    timeout.TotalSeconds);
                return null;
            }
            if (line is null)
            {
                // [EN] Reuse the WPF side's key (same message).
                // [ZH] 复用 WPF 侧的键（同一句文案）。
                LastError = Loc.T("main.error.exitedDuringStartup", "后端未完成启动即退出。");
                return null;
            }
            if (!line.TrimStart().StartsWith('{'))
                continue;
            JsonDocument doc;
            try
            {
                doc = JsonDocument.Parse(line);
            }
            catch (JsonException)
            {
                continue;
            }
            string type = doc.RootElement.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "" : "";
            if (type == "ready")
            {
                ApplyReady(doc.RootElement);
                return doc;   // [EN] The caller is responsible for Dispose.
                              // [ZH] 调用方负责 Dispose。
            }
            if (type == "system")
            {
                string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                LastError = content;
                doc.Dispose();
                // [EN] System messages (such as "model load failed") do not block: the
                // [EN] backend still proceeds to "ready without a model".
                // [ZH] 系统提示（如“模型加载失败”）不阻断：后端会继续进入“无模型就绪”。
                continue;
            }
            doc.Dispose();
        }
    }

    /// <summary>
    /// [EN] Sends one request and returns the response (parsed as a JsonDocument, which
    /// [EN] the caller disposes). Returns null on timeout/interruption.
    /// [ZH] 发送一个请求并返回响应（解析成 JsonDocument，调用方 Dispose）。超时/中断返回 null。
    /// </summary>
    public async Task<JsonDocument?> SendAsync(
        object request,
        TimeSpan? queueTimeout = null,
        TimeSpan? idleTimeout = null,
        CancellationToken cancellationToken = default)
    {
        string? line = await SendForLineAsync(request, queueTimeout, idleTimeout, cancellationToken);
        if (line is null)
            return null;
        try
        {
            var doc = JsonDocument.Parse(line);
            ApplyModelState(doc.RootElement);
            return doc;
        }
        catch (JsonException ex)
        {
            LastError = Loc.T("main.error.badJsonResponse", "后端返回了非 JSON 响应：{0}", ex.Message);
            return null;
        }
    }

    /// <summary>
    /// [EN] Sends a request and returns the raw response text (callers other than the
    /// [EN] self-check generally use <see cref="SendAsync"/>).
    /// [ZH] 发送请求，返回响应原文（自检以外的调用方一般用 <see cref="SendAsync"/>）。
    /// </summary>
    public async Task<string?> SendForLineAsync(
        object request,
        TimeSpan? queueTimeout = null,
        TimeSpan? idleTimeout = null,
        CancellationToken cancellationToken = default)
    {
        if (_writer is null || _reader is null)
        {
            // [EN] Reuse the WPF side's key.
            // [ZH] 复用 WPF 侧的键。
            LastError = Loc.T("main.error.notStarted", "后端尚未启动。");
            return null;
        }
        try
        {
            await AcquireGateAsync(queueTimeout, cancellationToken);
        }
        catch (BackendBusyException ex)
        {
            LastError = ex.Message;
            return null;
        }
        try
        {
            await _writer.WriteLineAsync(JsonSerializer.Serialize(request));
            await _writer.FlushAsync();
            return await ReadUntilJsonAsync(idleTimeout ?? DefaultIdleTimeout, cancellationToken);
        }
        catch (Exception ex) when (ex is BackendTimeoutException or IOException or InvalidOperationException)
        {
            LastError = ex.Message;
            return null;
        }
        finally
        {
            _requestGate.Release();
        }
    }

    /// <summary>
    /// [EN] Sends without reading (actions such as stop that expect no response, so as
    /// [EN] not to compete with a streaming read for stdout).
    /// [ZH] 只写不读地发送（stop 等不期待响应的动作，避免与流式读取抢 stdout）。
    /// </summary>
    public async Task<bool> SendFireAndForgetAsync(object request)
    {
        if (_writer is null || !IsRunning)
        {
            LastError = Loc.T("main.error.notRunning", "后端未在运行。");
            return false;
        }
        try
        {
            await _writer.WriteLineAsync(JsonSerializer.Serialize(request));
            await _writer.FlushAsync();
            return true;
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException)
        {
            LastError = ex.Message;
            return false;
        }
    }

    /// <summary>
    /// [EN] Sends a streaming request: each delta fragment is shown incrementally via
    /// [EN] the onDelta callback, and the full reply text is returned.
    /// [EN] `onDeltaChannel` (v0.2.1 dual channel): deltas carrying a `channel` field go
    /// [EN] here -- ("reasoning", text, false) reasoning content, ("answer", text, false)
    /// [EN] answer, ("answer", "", true) invalidate the answer area (the accumulated
    /// [EN] answer actually belongs to reasoning).
    /// [EN] `onEvent`: hands over every structured event before the done line (progress,
    /// [EN] metrics, etc.).
    /// [ZH] 发送流式请求：每个 delta 片段通过 onDelta 回调增量展示，返回完整回复文本。
    /// [ZH] `onDeltaChannel`（v0.2.1 双通道）：带 `channel` 字段的 delta 走这里
    /// [ZH] —— ("reasoning", text, false) 思考内容、("answer", text, false) 回答、
    /// [ZH] ("answer", "", true) 作废回答区（已累积的回答其实属于思考）。
    /// [ZH] `onEvent`：done 行之前把每一条结构化事件交出去（进度、metrics 等）。
    /// </summary>
    public async Task<string> SendStreamAsync(
        object request,
        Action<string>? onDelta = null,
        Action<int, int>? onContext = null,
        TimeSpan? queueTimeout = null,
        TimeSpan? idleTimeout = null,
        CancellationToken cancellationToken = default,
        Action<string, JsonElement>? onEvent = null,
        Action<string, string, bool>? onDeltaChannel = null)
    {
        if (_writer is null || _reader is null)
            throw new InvalidOperationException(Loc.T("main.error.notStarted", "后端尚未启动。"));
        await AcquireGateAsync(queueTimeout, cancellationToken);
        try
        {
            await _writer.WriteLineAsync(JsonSerializer.Serialize(request));
            await _writer.FlushAsync();

            var full = new StringBuilder();
            while (true)
            {
                string? line = await ReadLineAsync(idleTimeout, cancellationToken);
                if (line is null)
                    throw new InvalidOperationException(Loc.T("main.error.noResponseExited",
                        "无响应（后端进程可能已退出）。"));
                if (!line.TrimStart().StartsWith('{'))
                    continue;

                using JsonDocument doc = JsonDocument.Parse(line);
                JsonElement root = doc.RootElement;
                string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "" : "";
                string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                ApplyModelState(root);
                switch (type)
                {
                    case "delta":
                        if (root.TryGetProperty("channel", out JsonElement channelEl)
                            && channelEl.ValueKind == JsonValueKind.String)
                        {
                            string channel = channelEl.GetString() ?? "answer";
                            bool reset = root.TryGetProperty("reset", out JsonElement resetEl)
                                         && resetEl.ValueKind == JsonValueKind.True;
                            onDeltaChannel?.Invoke(channel, content, reset);
                        }
                        else
                        {
                            full.Append(content);
                            onDelta?.Invoke(content);
                        }
                        break;
                    case "done":
                        if (onContext is not null
                            && root.TryGetProperty("used", out JsonElement usedEl)
                            && root.TryGetProperty("limit", out JsonElement limitEl)
                            && usedEl.ValueKind == JsonValueKind.Number
                            && limitEl.ValueKind == JsonValueKind.Number)
                            onContext(usedEl.GetInt32(), limitEl.GetInt32());
                        onEvent?.Invoke("done", root);
                        return content.Length > 0 ? content : full.ToString();
                    case "error":
                        throw new InvalidOperationException(content);
                    default:
                        onEvent?.Invoke(type, root);
                        break;
                }
            }
        }
        finally
        {
            _requestGate.Release();
        }
    }

    /// <summary>
    /// [EN] Acquires the request gate; if it is still not released past the threshold,
    /// [EN] the backend is considered busy.
    /// [ZH] 获取请求闸门；超过阈值仍未释放则视为后端忙。
    /// </summary>
    private async Task AcquireGateAsync(TimeSpan? queueTimeout, CancellationToken cancellationToken)
    {
        TimeSpan wait = queueTimeout ?? DefaultQueueTimeout;
        if (wait <= TimeSpan.Zero)
        {
            await _requestGate.WaitAsync(cancellationToken);
            return;
        }
        if (!await _requestGate.WaitAsync(wait, cancellationToken))
            throw new BackendBusyException(Loc.T("main.error.busy", "⏳ 上一条回复仍在生成，请先停止或等待完成"));
    }

    /// <summary>
    /// [EN] Takes one line from the queue; throws <see cref="BackendTimeoutException"/>
    /// [EN] past the idle threshold, and returns null when the queue is closed.
    /// [ZH] 从队列取一行；超过空闲阈值抛 <see cref="BackendTimeoutException"/>，队列关闭返回 null。
    /// </summary>
    private async Task<string?> ReadLineAsync(TimeSpan? idleTimeout, CancellationToken cancellationToken)
    {
        if (idleTimeout is null)
        {
            try
            {
                return await _lines.Reader.ReadAsync(cancellationToken);
            }
            catch (ChannelClosedException)
            {
                return null;
            }
        }

        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        linked.CancelAfter(idleTimeout.Value);
        try
        {
            return await _lines.Reader.ReadAsync(linked.Token);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new BackendTimeoutException(Loc.T("main.error.idleTimeout",
                "后端在 {0:0.#} 分钟内没有响应，已中断等待。可稍后重试，或查看后端日志。",
                idleTimeout.Value.TotalMinutes));
        }
        catch (ChannelClosedException)
        {
            return null;
        }
    }

    private async Task<string> ReadUntilJsonAsync(TimeSpan idleTimeout, CancellationToken cancellationToken)
    {
        while (true)
        {
            string? line = await ReadLineAsync(idleTimeout, cancellationToken);
            if (line is null)
                throw new InvalidOperationException(Loc.T("main.error.noResponse", "无响应"));
            if (!line.TrimStart().StartsWith('{'))
                continue;
            // [EN] v0.2.2 batch 4: `host_progress` is a progress event that the backend
            // [EN] sends on its own while handling a **non-streaming** request (host
            // [EN] progress, see `_emit_host_progress` in backend/main.py). It is
            // [EN] **not** a response, so it must be skipped and forwarded here --
            // [EN] otherwise it is returned as the response and the caller receives a
            // [EN] progress line that only has content, which looks like "the request
            // [EN] succeeded but did nothing".
            // [ZH] v0.2.2 第 4 批：`host_progress` 是后端在处理**非流式**请求期间主动发的
            // [ZH] 进度事件（宿主进度，见 backend/main.py 的 `_emit_host_progress`）。
            // [ZH] 它**不是**响应，必须在这里跳过并转交出去 —— 否则会被当成响应返回，
            // [ZH] 调用方拿到一个只有 content 的进度行，看起来就是“请求成功了但什么都没做”。
            if (TryReadHostProgress(line, out string hostText))
            {
                HostProgressReceived?.Invoke(hostText);
                continue;
            }
            return line;
        }
    }

    /// <summary>
    /// [EN] Host progress event (sent by the backend while handling a non-streaming
    /// [EN] request).
    /// [ZH] 宿主进度事件（后端在处理非流式请求期间主动发来）。
    /// </summary>
    public event Action<string>? HostProgressReceived;

    /// <summary>
    /// [EN] Determines whether a line is a host progress event; if so, extracts its
    /// [EN] body.
    /// [ZH] 判断一行是不是宿主进度事件；是则取出它的正文。
    /// </summary>
    private static bool TryReadHostProgress(string line, out string content)
    {
        content = "";
        try
        {
            using JsonDocument doc = JsonDocument.Parse(line);
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
                return false;
            if (!doc.RootElement.TryGetProperty("type", out JsonElement type)
                || type.ValueKind != JsonValueKind.String
                || type.GetString() != "host_progress")
            {
                return false;
            }
            if (doc.RootElement.TryGetProperty("content", out JsonElement c)
                && c.ValueKind == JsonValueKind.String)
            {
                content = c.GetString() ?? "";
            }
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    // [EN] ---------- Status application ----------
    // [ZH] ---------- 状态应用 ----------

    /// <summary>
    /// [EN] Records the information from the ready line (version, restored history,
    /// [EN] model state, platform, command list).
    /// [ZH] 把 ready 行里的信息记下来（版本、恢复历史、模型状态、平台、命令清单）。
    /// </summary>
    private void ApplyReady(JsonElement root)
    {
        if (root.TryGetProperty("version", out JsonElement v))
            Version = v.GetString();
        if (root.TryGetProperty("studio", out JsonElement s))
            Studio = s.GetString();
        RestoredHistory.Clear();
        if (root.TryGetProperty("history", out JsonElement history) && history.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in history.EnumerateArray())
            {
                string role = item.TryGetProperty("role", out JsonElement r) ? r.GetString() ?? "" : "";
                string content = item.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                string reasoning = item.TryGetProperty("reasoning", out JsonElement rs) ? rs.GetString() ?? "" : "";
                RestoredHistory.Add((role, content, reasoning));
            }
            RestoredCount = RestoredHistory.Count;
        }
        Commands.Clear();
        if (root.TryGetProperty("commands", out JsonElement commands) && commands.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in commands.EnumerateArray())
            {
                string name = item.ValueKind == JsonValueKind.String
                    ? item.GetString() ?? ""
                    : (item.TryGetProperty("name", out JsonElement n) ? n.GetString() ?? "" : "");
                if (!string.IsNullOrEmpty(name))
                    Commands.Add(name);
            }
        }
        if (root.TryGetProperty("emoji", out JsonElement emojiEl)
            && emojiEl.ValueKind is JsonValueKind.True or JsonValueKind.False)
            BackendEmoji = emojiEl.GetBoolean();
        if (root.TryGetProperty("platform", out JsonElement platform)
            && platform.ValueKind == JsonValueKind.Object
            && platform.TryGetProperty("kind", out JsonElement kindEl))
            BackendPlatformKind = kindEl.GetString() ?? "";
        ApplyModelState(root);
    }

    /// <summary>
    /// [EN] Synchronizes the model state from the response onto the properties (the UI
    /// [EN] must update immediately after model_scan / model_load).
    /// [ZH] 把响应里的模型状态同步到属性上（model_scan / model_load 后界面要立刻变）。
    /// </summary>
    public void ApplyModelState(JsonElement root)
    {
        bool? ready = null;
        if (root.TryGetProperty("model_ready", out JsonElement mr)
            && mr.ValueKind is JsonValueKind.True or JsonValueKind.False)
            ready = mr.GetBoolean();
        if (root.TryGetProperty("model_state", out JsonElement state) && state.ValueKind == JsonValueKind.Object)
        {
            if (ready is null
                && state.TryGetProperty("ready", out JsonElement sr)
                && sr.ValueKind is JsonValueKind.True or JsonValueKind.False)
                ready = sr.GetBoolean();
            if (state.TryGetProperty("models_dir", out JsonElement md))
                ModelsDir = md.GetString() ?? ModelsDir;
            if (state.TryGetProperty("guide", out JsonElement g))
                ModelGuide = g.GetString() ?? ModelGuide;
            if (state.TryGetProperty("model", out JsonElement m))
                LoadedModel = m.GetString() ?? LoadedModel;
        }
        if (ready is bool value && value != ModelReady)
        {
            ModelReady = value;
            ModelReadyChanged?.Invoke(value);
        }
    }

    private void Process_Exited(object? sender, EventArgs e)
    {
        if (_stopping)
            return;   // [EN] A manual stop is not an abnormal exit.
                      // [ZH] 主动停止不算异常退出。
        _lines.Writer.TryComplete();
        int code = -1;
        try
        {
            code = _process?.ExitCode ?? -1;
        }
        catch (InvalidOperationException)
        {
            // [EN] The process object has already been disposed: keep -1.
            // [ZH] 进程对象已释放：保持 -1。
        }
        ProcessExited?.Invoke(code);
    }

    /// <summary>
    /// [EN] Stops the backend process (polite first, then forceful: kills the entire
    /// [EN] process tree to avoid leaving orphan Python processes on Linux).
    /// [ZH] 停止后端进程（先礼后兵：kill 整棵进程树，避免 Linux 上留下孤儿 Python）。
    /// </summary>
    public void Stop()
    {
        _stopping = true;
        try
        {
            _pumpCts?.Cancel();
        }
        catch (ObjectDisposedException)
        {
        }
        try
        {
            if (_process is { HasExited: false })
                _process.Kill(entireProcessTree: true);
        }
        catch (Exception)
        {
            // [EN] Already exited / no permission: ignore.
            // [ZH] 已经退出/无权限：忽略。
        }
        try
        {
            _process?.WaitForExit(3000);
        }
        catch (Exception)
        {
        }
        _lines.Writer.TryComplete();
    }

    public void Dispose()
    {
        Stop();
        try
        {
            _writer?.Dispose();
            _reader?.Dispose();
            _stdErrReader?.Dispose();
            _process?.Dispose();
        }
        catch (Exception)
        {
        }
        _pumpCts?.Dispose();
        _requestGate.Dispose();
    }
}

