// 后端子进程管理与 JSON 行协议（移植自 frontend/Services/BackendService.cs，任务书 任务二）。
//
// 这是"复用而不是重写"的核心证据：协议层（stdin 写一行 JSON / stdout 读一行 JSON、
// 单请求闸门、空闲超时、流式 delta 双通道）在 WPF 版已经被验证过 4 轮，跨平台版
// **逐段照搬**，只改三处与平台有关的地方：
//
//   1. **后端定位**（ResolveLaunch）：不再是写死的 EchoDeskBackend.exe。三个平台分别
//      找 `EchoDeskBackend[.exe]`；找不到打包产物时回退到"解释器 + backend/main.py"
//      —— 这一条让原型在没有 PyInstaller 产物的 Linux/macOS 上也能真跑起来。
//   2. **子进程环境变量**：Python 在非 Windows 的 C/POSIX locale 下 stdout 编码是
//      ASCII，直接吃中文/emoji 会 UnicodeEncodeError 把后端打死。所以显式注入
//      PYTHONUTF8=1 / PYTHONIOENCODING=utf-8（Windows 上无害）。
//   3. **找不到后端时的行为**：不抛异常崩前台，而是返回带 `Command` 与 `Error` 的
//      启动结果 —— 让界面显示"后端缺失 + 怎么办"，这正是任务五要求的降级文案。
//
// 其余设计照旧：stdout 单写泵 → Channel<string> → 单请求闸门串行化，因为 stdout 是
// 一条共享流，并发请求会互相读错响应。
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

/// <summary>请求队列超时（上一条回复还在生成时）。</summary>
public sealed class BackendBusyException : Exception
{
    public BackendBusyException(string message) : base(message) { }
}

/// <summary>空闲超时（后端在阈值内没有任何输出）。</summary>
public sealed class BackendTimeoutException : Exception
{
    public BackendTimeoutException(string message) : base(message) { }
}

/// <summary>后端启动方式（打包产物 or 解释器脚本）。</summary>
public sealed class BackendLaunch
{
    public required string Executable { get; init; }
    public required string[] Args { get; init; }
    public required string WorkDirectory { get; init; }

    /// <summary>给人看的启动命令（自检打印、错误信息里带上，便于用户自查）。</summary>
    public string CommandLine => Args.Length == 0
        ? Executable
        : Executable + " " + string.Join(' ', Args.Select(a => a.Contains(' ') ? '"' + a + '"' : a));

    /// <summary>是否走"解释器 + main.py"路径（未打包的开发/源码运行）。</summary>
    public bool IsScriptMode { get; init; }

    /// <summary>
    /// 额外的子进程环境变量（v0.2.1 跨平台-2 任务二）。
    /// 目前只有"包内 standalone Python"用它注入 PYTHONPATH —— 依赖装在
    /// `backend/site-packages/`，与解释器**并排**而不是合并进解释器的 site-packages，
    /// 所以必须显式告诉解释器去哪找它们。
    /// </summary>
    public IReadOnlyDictionary<string, string> ExtraEnvironment { get; init; }
        = new Dictionary<string, string>();
}

/// <summary>启动结果。失败不抛异常 —— 跨平台后"后端缺失"是可预期的正常状态，要能显示给用户。</summary>
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

    // stdout 行队列：单写（泵）多读（闸门保证同时只有一个读取方）
    private readonly Channel<string> _lines = Channel.CreateUnbounded<string>(
        new UnboundedChannelOptions { SingleWriter = true, SingleReader = false });

    // 请求闸门：stdout 是单条共享流，并发请求会互相读错响应，所有"写请求 + 读响应"串行化
    private readonly SemaphoreSlim _requestGate = new(1, 1);

    /// <summary>默认等待闸门释放的阈值：超过即认为后端忙。</summary>
    public static readonly TimeSpan DefaultQueueTimeout = TimeSpan.FromSeconds(15);

    /// <summary>默认等待响应的阈值（空闲超时：每收到一行即重新计时）。</summary>
    public static readonly TimeSpan DefaultIdleTimeout = TimeSpan.FromMinutes(10);

    /// <summary>重任务（解析大文件、模型下载、Agent 长任务）的响应阈值。</summary>
    public static readonly TimeSpan HeavyIdleTimeout = TimeSpan.FromMinutes(45);

    private readonly BackendLaunch? _launch;

    public BackendService(string? backendPath = null)
    {
        _launch = backendPath is null ? ResolveLaunch() : BuildLaunchFromPath(backendPath);
    }

    /// <summary>后端可执行文件/解释器的路径（未找到时为 null）。</summary>
    public string? BackendPath => _launch?.Executable;

    /// <summary>后端工作目录：config.json、session.json、models/、skills_dev/ 都在该目录下。</summary>
    public string? BackendDirectory => _launch?.WorkDirectory;

    /// <summary>启动失败原因（界面横幅显示它）。</summary>
    public string? LaunchError { get; private set; }

    /// <summary>最近一次请求的失败原因（超时/中断），供界面展示。</summary>
    public string? LastError { get; private set; }

    /// <summary>
    /// 每一行原始 stdout（自检与排障用）。**不参与协议处理**，只是旁路观察点。
    /// </summary>
    public event Action<string>? LineReceived;

    public string? Version { get; private set; }
    public string? Studio { get; private set; }
    public int RestoredCount { get; private set; }

    /// <summary>启动时恢复的会话历史（role/content/reasoning）。</summary>
    public List<(string Role, string Content, string Reasoning)> RestoredHistory { get; } = new();

    /// <summary>后端是否已加载模型（false = 界面可用但没有推理能力）。</summary>
    public bool ModelReady { get; private set; } = true;

    public string ModelsDir { get; private set; } = "";
    public string ModelGuide { get; private set; } = "";
    public string LoadedModel { get; private set; } = "";

    /// <summary>后端上报的平台信息（ready.platform）—— 用来校验前后端平台判断一致。</summary>
    public string BackendPlatformKind { get; private set; } = "";

    /// <summary>后端是否输出 emoji（ready.emoji）。</summary>
    public bool BackendEmoji { get; private set; } = true;

    /// <summary>后端可用动作清单（ready.commands）。</summary>
    public List<string> Commands { get; } = new();

    public event Action<bool>? ModelReadyChanged;
    public event Action<string>? StderrReceived;
    public event Action<int>? ProcessExited;

    public bool IsRunning => _process is { HasExited: false };

    // -----------------------------------------------------------------
    // 后端定位
    // -----------------------------------------------------------------

    /// <summary>
    /// 按平台规则定位后端。顺序（前者存在就用前者）：
    ///   1. 环境变量 ECHODESK_BACKEND（自检/开发指定绝对路径）
    ///   2. &lt;程序目录&gt;/backend/EchoDeskBackend[.exe]（v0.2.x 发布布局）
    ///   3. &lt;程序目录&gt;/EchoDeskBackend[.exe]（扁平布局）
    ///   4. &lt;程序目录&gt;/EchoBackend[.exe]（兼容 v0.1.0 布局）
    ///   5. **解释器 + backend/main.py**：从程序目录向上最多 6 层找 backend/main.py
    ///      （开发布局：bin/Release/net10.0/… → 仓库根）。这一步是原型能在
    ///      Linux/macOS 上真跑起来的关键 —— 那些平台没有 PyInstaller 产物。
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

        // 包内 standalone Python（v0.2.1 跨平台-2 任务二）：后端随包携带一个可重定位的
        // CPython，因此**不需要**在目标平台上编译出可执行文件。必须排在"向上找
        // main.py"之前 —— 发布树里也有 app/ 目录，顺序反了会在开发布局下选错目标。
        BackendLaunch? bundled = TryBundledStandalone(dir);
        if (bundled is not null)
            return bundled;

        // 源码运行：找 main.py
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
                    // 有脚本但没解释器：把这条错误说清楚（比"找不到后端"有用得多）
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
    /// 包内 standalone Python 的后端布局（v0.2.1 跨平台-2 任务二，由 tools/pack_backend.py 产出）：
    ///   <c>backend/python/**</c>        随包携带的 CPython（目标机**无需**安装 Python）
    ///   <c>backend/site-packages/**</c> pip 交叉下载的依赖（与解释器并排，不合并进去）
    ///   <c>backend/app/**</c>          后端代码（backend/ 的内容）
    ///
    /// 与 PyInstaller 布局（<c>backend/EchoDeskBackend[.exe]</c>）互不干扰：两者都在时
    /// 优先用可执行文件 —— 那是 Windows 正式交付形态，经过完整验证。
    ///
    /// **为什么前端直接调解释器，而不是调 run-backend.bat / run-backend.sh**：
    /// 中间多一层 shell 会让"停止后端"变成"杀掉 shell，Python 还活着"，
    /// 而前端是按子进程管理后端的（killed 判定、退出码、stdin/stdout 管道）。
    /// 启动脚本仍然提供，用途是给人手动启动/调试。
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
            // 依赖在前、后端代码在后：这样 app/ 里的模块不会意外遮蔽第三方包
            // （后端目录是扁平的一堆 .py，出现过同名风险的地方要小心）。
            ["PYTHONPATH"] = sitePackages + Path.PathSeparator + appDir,
            // 发布目录可能只读；.pyc 又属于"运行期残留"（卸载清单与门禁都要处理它）。
            ["PYTHONDONTWRITEBYTECODE"] = "1",
        };
        // **工作目录必须是 backend/，不是 backend/app/**（v0.2.2 全面双语与三平台友好 修正）。
        //
        // 为什么（实测发现的跨平台不一致）：后端的相对路径都是相对**工作目录**解析的 ——
        // `models_dir` 默认 "models"（backend/model_hub.py）、`skills_dev`（backend/skills_runtime.py）。
        // Windows 的 WPF 前端把工作目录设为后端 exe 所在目录（`…\app\backend\`），
        // 所以模型目录是 `…\app\backend\models\`；而这里原来传的是 `backend/app`，
        // 于是同一个应用在 Linux/macOS 上把模型目录变成 `…/app/backend/app/models/` ——
        // 用户按文档放的 .gguf 会"放对了地方却不被识别"。
        // 统一成 backend/ 之后，三平台的模型目录与插件目录都落在 `app/backend/` 下。
        //
        // 注意 `WorkDirectory` 的变化**不影响用户数据根目录**：那个由 `backend/config.py` 的
        // `user_data_root()` 按 config.py 的位置推导（跳过 app/backend 这些容器目录），
        // 与进程的工作目录无关。
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
        // 显式给了路径：exe 直接跑；.py 用解释器跑
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

    /// <summary>给出"找不到后端"的完整解释（界面横幅直接用这段话）。</summary>
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

    // -----------------------------------------------------------------
    // 启动 / 读取
    // -----------------------------------------------------------------

    /// <summary>启动后端子进程。**不等待 ready**（调用方决定要不要等；界面可以先画出来再等）。</summary>
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

        // 包内 standalone Python 的额外环境（PYTHONPATH 等），见 TryBundledStandalone。
        foreach (var pair in _launch.ExtraEnvironment)
            startInfo.Environment[pair.Key] = pair.Value;

        // Python 侧编码：POSIX/C locale 下 Python 默认用 ASCII 编码 stdout，
        // 中文与 emoji 会直接抛 UnicodeEncodeError 把后端打死（Windows 上无害）。
        startInfo.Environment["PYTHONUTF8"] = "1";
        startInfo.Environment["PYTHONIOENCODING"] = "utf-8";
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";
        // 后端要知道自己是被前端拉起来的（将来做"独占模型"时用）
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
            // 复用 WPF 侧的同一个键（"无法启动后端进程。"）：两侧同一批错误文案，别再各写一份
            LaunchError = Loc.T("main.error.startProcessFailed", "无法启动后端进程。");
            return Task.FromResult(new StartResult { Ok = false, Command = _launch.CommandLine, Error = LaunchError });
        }

        _process.EnableRaisingEvents = true;
        _process.Exited += Process_Exited;
        _writer = _process.StandardInput;
        // 协议含中文/emoji 且始终 UTF-8：显式按 UTF-8 读取，避免 cp1252/GBK 机器上乱码
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

    /// <summary>stdout 泵：持续读取并投递，进程结束或停止时关闭队列。</summary>
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
            // 进程结束/主动停止导致的读取中断属正常现象
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
            // stderr 读取失败不影响主流程
        }
    }

    /// <summary>等待 ready 握手；超时或未就绪退出返回 null（不抛，自检与界面都好处理）。</summary>
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
                // 复用 WPF 侧的键（同一句文案）
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
                return doc;   // 调用方负责 Dispose
            }
            if (type == "system")
            {
                string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                LastError = content;
                doc.Dispose();
                continue;   // 系统提示（如"模型加载失败"）不阻断：后端会继续进入"无模型就绪"
            }
            doc.Dispose();
        }
    }

    /// <summary>发送一个请求并返回响应（解析成 JsonDocument，调用方 Dispose）。超时/中断返回 null。</summary>
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

    /// <summary>发送请求，返回响应原文（自检以外的调用方一般用 <see cref="SendAsync"/>）。</summary>
    public async Task<string?> SendForLineAsync(
        object request,
        TimeSpan? queueTimeout = null,
        TimeSpan? idleTimeout = null,
        CancellationToken cancellationToken = default)
    {
        if (_writer is null || _reader is null)
        {
            // 复用 WPF 侧的键
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

    /// <summary>只写不读地发送（stop 等不期待响应的动作，避免与流式读取抢 stdout）。</summary>
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
    /// 发送流式请求：每个 delta 片段通过 onDelta 回调增量展示，返回完整回复文本。
    /// `onDeltaChannel`（v0.2.1 双通道）：带 `channel` 字段的 delta 走这里
    /// —— ("reasoning", text, false) 思考内容、("answer", text, false) 回答、
    /// ("answer", "", true) 作废回答区（已累积的回答其实属于思考）。
    /// `onEvent`：done 行之前把每一条结构化事件交出去（进度、metrics 等）。
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

    /// <summary>获取请求闸门；超过阈值仍未释放则视为后端忙。</summary>
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

    /// <summary>从队列取一行；超过空闲阈值抛 <see cref="BackendTimeoutException"/>，队列关闭返回 null。</summary>
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
            // v0.2.2 第 4 批：`host_progress` 是后端在处理**非流式**请求期间主动发的
            // 进度事件（宿主进度，见 backend/main.py 的 `_emit_host_progress`）。
            // 它**不是**响应，必须在这里跳过并转交出去 —— 否则会被当成响应返回，
            // 调用方拿到一个只有 content 的进度行，看起来就是"请求成功了但什么都没做"。
            if (TryReadHostProgress(line, out string hostText))
            {
                HostProgressReceived?.Invoke(hostText);
                continue;
            }
            return line;
        }
    }

    /// <summary>宿主进度事件（后端在处理非流式请求期间主动发来）。</summary>
    public event Action<string>? HostProgressReceived;

    /// <summary>判断一行是不是宿主进度事件；是则取出它的正文。</summary>
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

    // -----------------------------------------------------------------
    // 状态应用
    // -----------------------------------------------------------------

    /// <summary>把 ready 行里的信息记下来（版本、恢复历史、模型状态、平台、命令清单）。</summary>
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

    /// <summary>把响应里的模型状态同步到属性上（model_scan / model_load 后界面要立刻变）。</summary>
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
            return;   // 主动停止不算异常退出
        _lines.Writer.TryComplete();
        int code = -1;
        try
        {
            code = _process?.ExitCode ?? -1;
        }
        catch (InvalidOperationException)
        {
            // 进程对象已释放：保持 -1
        }
        ProcessExited?.Invoke(code);
    }

    /// <summary>停止后端进程（先礼后兵：kill 整棵进程树，避免 Linux 上留下孤儿 Python）。</summary>
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
            // 已经退出/无权限：忽略
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

