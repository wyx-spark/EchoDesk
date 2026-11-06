// [EN] EchoDesk WPF frontend module: frontend/Services/BackendService.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/BackendService.cs。类型与成员说明见下方注释。
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace EchoDesk.Services;

/// <summary>后端报告本地缺少模型（need_download）：前端据此弹确认框，用户同意后再下载。</summary>
public sealed class NeedDownloadException : Exception
{
    public NeedDownloadException(string model, long size, string repo, string detail)
        : base(detail.Length > 0 ? detail : Loc.T("main.error.modelMissingDetail", "本地缺少模型 {0}", model))
    {
        Model = model;
        Size = size;
        Repo = repo;
    }

    /// <summary>仓库中最匹配的模型文件名（用户同意后原样回传）。</summary>
    public string Model { get; }

    /// <summary>文件大小（字节，0 表示未知）。</summary>
    public long Size { get; }

    /// <summary>模型仓库 ID。</summary>
    public string Repo { get; }
}

/// <summary>
/// 后端正忙：请求在闸门外等待超过阈值（通常是主窗口正在流式生成或自动化运行中）。
/// 属于「可重试」状态，界面应提示而不是无限等待。
/// </summary>
public sealed class BackendBusyException : Exception
{
    public BackendBusyException(string message) : base(message) { }
}

/// <summary>后端在阈值内没有返回响应（进程卡住或任务异常漫长）。</summary>
public sealed class BackendTimeoutException : Exception
{
    public BackendTimeoutException(string message) : base(message) { }
}

/// <summary>
/// 管理 EchoBackend.exe 子进程，通过 stdin/stdout 以 JSON 行协议通信。
///
/// 通信模型（v0.2.0 修复一重构）：
/// <list type="bullet">
/// <item>stdout 由唯一的「泵」线程持续读取并投递到队列，请求方从队列取行，
/// 因此可以安全地设置超时——超时不会留下悬挂的读取操作（否则会读错下一条响应的归属）。</item>
/// <item>「写请求 + 读响应」仍由请求闸门串行化（stdout 是单条共享流）。</item>
/// <item>闸门等待与响应等待都有阈值：超时抛出可识别的异常，界面给出提示而非无限等待。</item>
/// </list>
/// </summary>
public sealed class BackendService : IDisposable
{
    private Process? _process;
    private StreamWriter? _writer;
    private StreamReader? _reader;
    private StreamReader? _stdErrReader;
    private CancellationTokenSource? _pumpCts;
    private bool _stopping;

    // stdout 行队列：单写（泵）多读（闸门保证同时只有一个读取方）
    private readonly Channel<string> _lines = Channel.CreateUnbounded<string>(
        new UnboundedChannelOptions { SingleWriter = true, SingleReader = false });

    // 请求闸门：stdout 是单条共享流，主窗口与设置窗口并发请求会互相读错响应，
    // 因此所有「写请求 + 读响应」的调用串行化。（stop 只写不读，不经过闸门）
    private readonly SemaphoreSlim _requestGate = new(1, 1);

    /// <summary>默认等待闸门释放的阈值：超过即认为后端忙，提示用户稍后重试。</summary>
    public static readonly TimeSpan DefaultQueueTimeout = TimeSpan.FromSeconds(15);

    /// <summary>默认等待响应的阈值（空闲超时：每收到一行即重新计时）。</summary>
    public static readonly TimeSpan DefaultIdleTimeout = TimeSpan.FromMinutes(10);

    /// <summary>重任务（解析大文件、分段摘要、模型下载）的响应阈值。</summary>
    public static readonly TimeSpan HeavyIdleTimeout = TimeSpan.FromMinutes(45);

    public BackendService(string? backendPath = null) => _backendPath = backendPath ?? ResolveBackendPath();

    private readonly string _backendPath;

    /// <summary>后端可执行文件完整路径（发布布局为 app\backend\EchoDeskBackend.exe）。</summary>
    public string BackendPath => _backendPath;

    /// <summary>
    /// 后端工作目录：config.json、session.json、models/、skills_dev/ 都在该目录下
    /// （即 app\backend\）。注意**用户数据不在**这里 —— `ui_state.json`、常用语与
    /// 「results」在外层目录，见 ShortcutService.UserDataDirectory（修复七）。
    /// </summary>
    public string? BackendDirectory => Path.GetDirectoryName(_backendPath);

    /// <summary>
    /// 定位后端可执行文件。发布布局下后端位于 <c>app\backend\</c> 子文件夹（PyInstaller onedir），
    /// 而 <see cref="AppContext.BaseDirectory"/> 就是前端 exe 所在的 <c>app\</c>，
    /// 因此下面的相对拼接在两种布局（扁平/便携版三层）下都成立。
    /// 开发时可用环境变量 <c>ECHODESK_BACKEND</c> 指定路径。
    /// </summary>
    public static string ResolveBackendPath()
    {
        string? overridePath = Environment.GetEnvironmentVariable("ECHODESK_BACKEND");
        if (!string.IsNullOrEmpty(overridePath) && File.Exists(overridePath))
            return overridePath;

        string dir = AppContext.BaseDirectory;
        string[] candidates =
        {
            Path.Combine(dir, "backend", "EchoDeskBackend.exe"),  // v0.2.0 发布布局
            Path.Combine(dir, "EchoDeskBackend.exe"),             // 后端与前端同目录
            Path.Combine(dir, "EchoBackend.exe"),                 // 兼容 v0.1.0 布局
        };
        foreach (string candidate in candidates)
        {
            if (File.Exists(candidate))
                return candidate;
        }
        return candidates[0];  // 都找不到时返回期望路径，便于报错信息指明位置
    }

    /// <summary>后端声明中的版本号（如 v0.1.0）。</summary>
    public string? Version { get; private set; }

    /// <summary>后端声明中的工作室署名。</summary>
    public string? Studio { get; private set; }

    /// <summary>启动时自动恢复的会话轮数。</summary>
    public int RestoredCount { get; private set; }

    /// <summary>
    /// 启动时恢复的会话历史（role/content/reasoning），供界面展示。
    /// v0.2.1 模型思考内容分离：`reasoning` 是该轮助手回复的思考内容（
    /// 老会话或普通模型没有这个字段，此时为空串），界面据此重建折叠的思考区。
    /// </summary>
    public List<(string Role, string Content, string Reasoning)> RestoredHistory { get; } = new();

    /// <summary>
    /// 推理是否可用（v0.2.1 综合优化 任务一）。
    ///
    /// "后端已就绪"和"能推理"是**两件事**：模型缺失时后端照样就绪并服务，
    /// 只是推理类功能返回引导。默认 true，只有后端明确说 false 才进入无模型模式。
    /// </summary>
    public bool ModelReady { get; private set; } = true;

    /// <summary>模型文件夹的真实路径（引导文案里要给用户看，便于手动放置 .gguf）。</summary>
    public string ModelsDir { get; private set; } = "";

    /// <summary>无模型时后端给出的引导文案（含三条可行路径与模型文件夹路径）。</summary>
    public string ModelGuide { get; private set; } = "";

    /// <summary>当前加载的模型文件名（引擎里实际加载的那个）。</summary>
    public string LoadedModel { get; private set; } = "";

    /// <summary>
    /// 模型可用状态发生变化（无模型 → 已加载，或反之）。
    /// 界面据此把状态栏与推理入口在"引导"与"正常"之间自动切换，无需重启前端（任务六）。
    /// </summary>
    public event Action<bool>? ModelReadyChanged;

    /// <summary>后端 stderr 输出的每一行（后台线程触发，供界面显示启动/下载进度）。</summary>
    public event Action<string>? StderrReceived;

    /// <summary>后端进程异常退出（非主动 Stop）时触发，供界面在对话框中提示用户。</summary>
    public event Action<int>? ProcessExited;

    public bool IsRunning => _process is { HasExited: false };

    /// <summary>启动后端子进程，并等待其完成初始化（模型加载 + 预热）后就绪。</summary>
    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = _backendPath,
            // 工作目录设为后端所在目录：配置、会话与 models/ 都相对它解析，
            // 这样无论从哪个目录启动前端都能找到模型。
            WorkingDirectory = BackendDirectory ?? AppContext.BaseDirectory,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        _process = Process.Start(startInfo) ?? throw new InvalidOperationException(Loc.T("main.error.startProcessFailed", "无法启动后端进程。"));
        _process.EnableRaisingEvents = true;
        _process.Exited += Process_Exited;
        _writer = _process.StandardInput;
        // 后端协议含中文/emoji 且始终按 UTF-8 输出；这里显式用 UTF-8 读取，
        // 避免在非中文 locale（cp1252）的机器上读成乱码或解码异常。
        var utf8 = new UTF8Encoding(false);
        _reader = new StreamReader(_process.StandardOutput.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);
        _stdErrReader = new StreamReader(_process.StandardError.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);

        // 唯一读取者：把 stdout 的每一行投递到队列
        _pumpCts = new CancellationTokenSource();
        _ = Task.Run(() => PumpAsync(_pumpCts.Token));
        // 后台收集 stderr，便于排查后端问题
        _ = Task.Run(ReadErrorsAsync);

        await WaitForReadyAsync(cancellationToken);
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

    /// <summary>发送一个 JSON 请求，返回后端响应的 JSON 文本（跳过偶发的非 JSON 诊断行）。</summary>
    public Task<string> SendRequestAsync(
        object request,
        TimeSpan? queueTimeout = null,
        TimeSpan? idleTimeout = null,
        CancellationToken cancellationToken = default)
        => SendRequestCoreAsync(request, queueTimeout, idleTimeout, cancellationToken);

    private async Task<string> SendRequestCoreAsync(
        object request, TimeSpan? queueTimeout, TimeSpan? idleTimeout, CancellationToken cancellationToken)
    {
        EnsureRunning();
        await AcquireGateAsync(queueTimeout, cancellationToken);
        try
        {
            await _writer!.WriteLineAsync(JsonSerializer.Serialize(request));
            await _writer.FlushAsync();
            string line = await ReadUntilJsonAsync(idleTimeout ?? DefaultIdleTimeout, cancellationToken);
            // 非流式响应里也可能带模型状态（model_state / model_ready）：
            // 例如 model_scan、model_load 之后界面要立刻切到"有模型"（任务六）。
            try
            {
                using JsonDocument doc = JsonDocument.Parse(line);
                ApplyModelState(doc.RootElement);
            }
            catch (JsonException)
            {
                // 响应不是合法 JSON 时交给调用方按原样处理
            }
            return line;
        }
        finally
        {
            _requestGate.Release();
        }
    }

    /// <summary>只写不读地发送一个请求（用于 stop 等不期待响应的动作，避免与流式读取竞争）。</summary>
    public async Task SendFireAndForgetAsync(object request)
    {
        if (_writer is null)
            throw new InvalidOperationException(Loc.T("main.error.notStarted", "后端尚未启动。"));
        if (!IsRunning)
            throw new InvalidOperationException(Loc.T("main.error.backendExited"));
        await _writer.WriteLineAsync(JsonSerializer.Serialize(request));
        await _writer.FlushAsync();
    }

    /// <summary>
    /// 发送流式请求：每个 delta 片段通过 onDelta 回调增量展示，返回完整回复文本；
    /// onContext 在 done 行携带 used/limit 时回调（用于刷新上下文占用）。
    /// onEvent（v0.2.1 综合优化 任务四）：每一条应答都以 (type, root) 交给调用方，
    /// 用于处理 delta 之外的**结构化进度**（`summary_progress` 等）。**v0.2.1 模型
    /// 思考内容分离**：还在 `done` 行前调用一次 `onEvent("done", root)`，调用方据此
    /// 拿到 `reasoning`（思考全文）、`reasoning_unclosed` 等字段。
    /// **注意**：`root` 只在这个回调期间有效（JsonDocument 随后即释放），
    /// 需要留用请当场取出值，不要保存 JsonElement。
    /// 流式请求可以被 cancellationToken 取消（界面「停止」按钮），默认不设空闲超时
    /// ——自动化可能因等待人工输入（验证码）长时间没有新片段。
    /// onDeltaChannel（v0.2.1 模型思考内容分离）：带 `channel` 字段的 delta 走这里
    /// ——`("reasoning", text, false)` 思考内容、`("answer", text, false)` 最终回答、
    /// `("answer", "", true)` 作废回答区（此前发的内容其实属于思考，调用方需把已
    /// 累积的回答文本搬到思考区）。没有 `channel` 的旧式 delta（进度/摘要等）仍走
    /// onDelta，两条路互不干扰。
    /// </summary>
    public async Task<string> SendStreamRequestAsync(
        object request,
        Action<string> onDelta,
        Action<int, int>? onContext = null,
        TimeSpan? queueTimeout = null,
        TimeSpan? idleTimeout = null,
        CancellationToken cancellationToken = default,
        Action<string, JsonElement>? onEvent = null,
        Action<string, string, bool>? onDeltaChannel = null)
    {
        EnsureRunning();
        await AcquireGateAsync(queueTimeout, cancellationToken);
        try
        {
            await _writer!.WriteLineAsync(JsonSerializer.Serialize(request));
            await _writer.FlushAsync();

            var full = new StringBuilder();
            while (true)
            {
                string? line = await ReadLineAsync(idleTimeout, cancellationToken);
                if (line is null)
                    throw new InvalidOperationException(Loc.T("main.error.noResponse"));
                if (!line.TrimStart().StartsWith('{'))
                {
                    Debug.WriteLine($"[backend:ignored] {line}");
                    continue;
                }

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
                            // 双通道（思考 / 回答）：不累加进 full，done 行会给出权威全文
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
                        // 让调用方在 JsonDocument 释放前取走 done 行的附加字段
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

    private void EnsureRunning()
    {
        if (_writer is null || _reader is null)
            throw new InvalidOperationException(Loc.T("main.error.notStarted", "后端尚未启动。"));
        if (!IsRunning)
            throw new InvalidOperationException(Loc.T("main.error.backendExited"));
    }

    /// <summary>获取请求闸门；超过阈值仍未释放则抛出 <see cref="BackendBusyException"/>。</summary>
    private async Task AcquireGateAsync(TimeSpan? queueTimeout, CancellationToken cancellationToken)
    {
        TimeSpan wait = queueTimeout ?? DefaultQueueTimeout;
        if (wait <= TimeSpan.Zero)
        {
            await _requestGate.WaitAsync(cancellationToken);
            return;
        }
        if (!await _requestGate.WaitAsync(wait, cancellationToken))
            throw new BackendBusyException(Loc.T("main.error.busy"));
    }

    /// <summary>从队列取一行；超过空闲阈值抛出 <see cref="BackendTimeoutException"/>，队列关闭返回 null。</summary>
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
            throw new BackendTimeoutException(
                Loc.T("main.error.idleTimeout", "后端在 {0:0.#} 分钟内没有响应，已中断等待。可稍后重试，或查看 backend_crash.log。", idleTimeout.Value.TotalMinutes));
        }
        catch (ChannelClosedException)
        {
            return null;
        }
    }

    private async Task<string> ReadUntilJsonAsync(TimeSpan? idleTimeout, CancellationToken cancellationToken)
    {
        while (true)
        {
            string? line = await ReadLineAsync(idleTimeout, cancellationToken);
            if (line is null)
                throw new InvalidOperationException(Loc.T("main.error.noResponse"));
            if (!line.TrimStart().StartsWith('{'))
            {
                Debug.WriteLine($"[backend:ignored] {line}");
                continue;
            }
            // v0.2.2 第 4 批：`host_progress` 是后端在处理**非流式**请求期间主动发的
            // 宿主进度事件（见 backend/main.py 的 `_emit_host_progress`），**不是响应**。
            // 在这里跳过并转交出去 —— 否则它会被当成响应返回给调用方，
            // 表现就是"请求成功了但什么都没做"。
            if (TryReadHostProgress(line, out string hostText))
            {
                HostProgressReceived?.Invoke(hostText);
                continue;
            }
            return line;
        }
    }

    /// <summary>宿主进度事件（后端在处理非流式请求期间主动发来；v0.2.2 第 4 批）。</summary>
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

    private async Task WaitForReadyAsync(CancellationToken cancellationToken)
    {
        while (true)
        {
            string? line = await ReadLineAsync(HeavyIdleTimeout, cancellationToken);
            if (line is null)
                throw new InvalidOperationException(Loc.T("main.error.exitedDuringStartup", "后端未完成启动即退出。"));
            if (!line.TrimStart().StartsWith('{'))
                continue;

            using JsonDocument doc = JsonDocument.Parse(line);
            JsonElement root = doc.RootElement;
            string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "" : "";
            if (type == "ready")
            {
                ApplyReady(root);
                return;
            }
            // 本地缺少模型：**不再**抛异常让界面弹"是否下载、否则退出"。
            // 任务一：后端已进入"无模型就绪"，这里只把它记下来（引导文案随 ready 一起用），
            // 然后继续等 ready —— 后端不会因为缺模型而退出，前端也不该把这个当成启动失败。
            if (type == "need_download")
            {
                ApplyNeedDownload(root);
                continue;
            }
            // 初始化失败等系统消息：直接抛出，让前端展示具体原因
            if (type == "system")
            {
                string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                throw new InvalidOperationException(content);
            }
        }
    }

    /// <summary>
    /// 用户同意下载模型后：发送 model_download，读取下载进度（onDelta）与提示（onMessage），
    /// 直到后端完成下载、加载与预热并发出 ready 声明。
    /// </summary>
    public async Task DownloadModelAndWaitReadyAsync(
        string name, Action<string> onDelta, Action<string>? onMessage = null)
    {
        if (_writer is null || _reader is null)
            throw new InvalidOperationException(Loc.T("main.error.notStarted", "后端尚未启动。"));

        await _writer.WriteLineAsync(JsonSerializer.Serialize(new { action = "model_download", name }));
        await _writer.FlushAsync();

        while (true)
        {
            // 下载大模型可能长时间没有新进度行（慢速网络），阈值给足
            string? line = await ReadLineAsync(HeavyIdleTimeout, CancellationToken.None);
            if (line is null)
                throw new InvalidOperationException(Loc.T("main.error.exitedDuringDownload", "后端在下载模型期间退出。"));
            if (!line.TrimStart().StartsWith('{'))
                continue;

            using JsonDocument doc = JsonDocument.Parse(line);
            JsonElement root = doc.RootElement;
            string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "" : "";
            string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            ApplyModelState(root);   // 下载完成后的 done 带 model_state，界面据此切到"有模型"
            switch (type)
            {
                case "delta":
                    onDelta?.Invoke(content);
                    break;
                case "done":
                    onMessage?.Invoke(content);
                    break;  // 下载完成，继续等 ready
                case "ready":
                    ApplyReady(root);
                    return;
                case "need_download":
                    throw BuildNeedDownload(root);
                case "error":
                    throw new InvalidOperationException(content);
                case "system":
                    onMessage?.Invoke(content);
                    break;
                default:
                    Debug.WriteLine($"[backend:msg] {line}");
                    break;
            }
        }
    }

    private static NeedDownloadException BuildNeedDownload(JsonElement root)
    {
        string model = root.TryGetProperty("model", out JsonElement m) ? m.GetString() ?? "" : "";
        long size = root.TryGetProperty("size", out JsonElement s) && s.ValueKind == JsonValueKind.Number
            ? s.GetInt64() : 0;
        string repo = root.TryGetProperty("repo", out JsonElement r) ? r.GetString() ?? "" : "";
        string detail = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
        return new NeedDownloadException(model, size, repo, detail);
    }

    /// <summary>
    /// 记录"缺模型"这条告知（任务一）：只更新引导信息，**不打断启动流程**。
    /// 旧行为把 `need_download` 当成可抛出异常，用户一拒绝就整个应用不可用 —— 那正是要拆掉的墙角。
    /// </summary>
    private void ApplyNeedDownload(JsonElement root)
    {
        ModelReady = false;
        if (root.TryGetProperty("content", out JsonElement c) && c.GetString() is { Length: > 0 } text)
            ModelGuide = text;
        if (root.TryGetProperty("models_dir", out JsonElement d) && d.GetString() is { Length: > 0 } dir)
            ModelsDir = dir;
    }

    /// <summary>
    /// 从任意响应里更新模型状态（任务六：下载完成/热加载后前端自动切换，无需重启）。
    /// 认两类字段：`model_ready`（布尔）与 `model_state`（对象）。
    /// </summary>
    public void ApplyModelState(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object)
            return;
        string previousModel = LoadedModel;
        bool previousReady = ModelReady;
        if (root.TryGetProperty("model_state", out JsonElement state) && state.ValueKind == JsonValueKind.Object)
        {
            if (state.TryGetProperty("ready", out JsonElement r) && r.ValueKind is JsonValueKind.True or JsonValueKind.False)
                ModelReady = r.ValueKind == JsonValueKind.True;
            if (state.TryGetProperty("models_dir", out JsonElement d) && d.GetString() is { Length: > 0 } dir)
                ModelsDir = dir;
            if (state.TryGetProperty("guide", out JsonElement g) && g.GetString() is { Length: > 0 } guide)
                ModelGuide = guide;
            if (state.TryGetProperty("model", out JsonElement m) && m.GetString() is { } name)
                LoadedModel = name;
        }
        else if (root.TryGetProperty("model_ready", out JsonElement mr)
                 && mr.ValueKind is JsonValueKind.True or JsonValueKind.False)
        {
            ModelReady = mr.ValueKind == JsonValueKind.True;
        }

        if (ModelReady != previousReady || LoadedModel != previousModel)
            ModelReadyChanged?.Invoke(ModelReady);
    }

    private void ApplyReady(JsonElement root)
    {
        Version = root.TryGetProperty("version", out JsonElement v) ? v.GetString() : null;
        Studio = root.TryGetProperty("studio", out JsonElement s) ? s.GetString() : null;
        RestoredCount = root.TryGetProperty("restored", out JsonElement r) ? r.GetInt32() : 0;
        // 任务一：就绪声明里明确区分"后端已就绪"与"推理可用"
        ApplyModelState(root);
        if (root.TryGetProperty("model_missing", out JsonElement missing)
            && missing.ValueKind == JsonValueKind.Object
            && missing.TryGetProperty("models_dir", out JsonElement md)
            && md.GetString() is { Length: > 0 } missingDir && ModelsDir.Length == 0)
        {
            ModelsDir = missingDir;
        }
        if (root.TryGetProperty("history", out JsonElement hist) && hist.ValueKind == JsonValueKind.Array)
        {
            RestoredHistory.Clear();
            foreach (JsonElement m in hist.EnumerateArray())
            {
                string role = m.TryGetProperty("role", out JsonElement rl) ? rl.GetString() ?? "" : "";
                string content = m.TryGetProperty("content", out JsonElement ct) ? ct.GetString() ?? "" : "";
                // 思考内容（v0.2.1 模型思考内容分离）：老会话没有这个字段，取不到就是空串
                string reasoning = m.TryGetProperty("reasoning", out JsonElement rs) ? rs.GetString() ?? "" : "";
                if (role.Length > 0)
                    RestoredHistory.Add((role, content, reasoning));
            }
        }
    }

    private async Task ReadErrorsAsync()
    {
        if (_stdErrReader is not { } error)
            return;
        try
        {
            while (await error.ReadLineAsync() is { } line)
            {
                Debug.WriteLine($"[backend] {line}");
                StderrReceived?.Invoke(line);
            }
        }
        catch (IOException)
        {
            // 进程结束时读取中断属正常现象
        }
    }

    private void Process_Exited(object? sender, EventArgs e)
    {
        // 主动 Stop() 会先关闭 writer 并 Kill，期间可能触发本事件；用标志位区分，避免误报。
        if (_stopping)
            return;
        try
        {
            int code = _process is { HasExited: true } ? _process.ExitCode : -1;
            ProcessExited?.Invoke(code);
        }
        catch (InvalidOperationException)
        {
            ProcessExited?.Invoke(-1);
        }
    }

    /// <summary>停止后端进程并释放资源。</summary>
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
            _writer?.Close();
        }
        catch (IOException)
        {
        }
        if (_process is { HasExited: false })
        {
            try
            {
                _process.Kill();
            }
            catch (InvalidOperationException)
            {
            }
            _process.WaitForExit(2000);
        }
        _process?.Dispose();
        _process = null;
        _writer = null;
        _reader?.Dispose();
        _reader = null;
        _stdErrReader?.Dispose();
        _stdErrReader = null;
        _pumpCts?.Dispose();
        _pumpCts = null;
        _lines.Writer.TryComplete();
    }

    public void Dispose() => Stop();
}
