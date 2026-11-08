// [EN] EchoDesk WPF frontend module: frontend/Services/BackendService.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/BackendService.cs。类型与成员说明见下方注释。
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace EchoDesk.Services;

/// <summary>
/// [EN] The backend reports that the local model is missing (need_download): the front end
/// [EN] shows a confirmation dialog and downloads only after the user agrees.
/// [ZH] 后端报告本地缺少模型（need_download）：前端据此弹确认框，用户同意后再下载。
/// </summary>
public sealed class NeedDownloadException : Exception
{
    public NeedDownloadException(string model, long size, string repo, string detail)
        : base(detail.Length > 0 ? detail : Loc.T("main.error.modelMissingDetail", "本地缺少模型 {0}", model))
    {
        Model = model;
        Size = size;
        Repo = repo;
    }

    /// <summary>
    /// [EN] Repository file name of the best matching model (returned verbatim once the
    /// [EN] user agrees).
    /// [ZH] 仓库中最匹配的模型文件名（用户同意后原样回传）。
    /// </summary>
    public string Model { get; }

    /// <summary>
    /// [EN] File size in bytes; 0 means unknown.
    /// [ZH] 文件大小（字节，0 表示未知）。
    /// </summary>
    public long Size { get; }

    /// <summary>
    /// [EN] Model repository ID.
    /// [ZH] 模型仓库 ID。
    /// </summary>
    public string Repo { get; }
}

/// <summary>
/// [EN] The backend is busy: the request waited outside the gate longer than the threshold
/// [EN] (usually because the main window is streaming or automation is running).
/// [EN] This is a retryable state; the UI should notify the user instead of waiting forever.
/// [ZH] 后端正忙：请求在闸门外等待超过阈值（通常是主窗口正在流式生成或自动化运行中）。
/// [ZH] 属于「可重试」状态，界面应提示而不是无限等待。
/// </summary>
public sealed class BackendBusyException : Exception
{
    public BackendBusyException(string message) : base(message) { }
}

/// <summary>
/// [EN] The backend returned no response within the threshold (the process is stuck or the
/// [EN] task is unusually long).
/// [ZH] 后端在阈值内没有返回响应（进程卡住或任务异常漫长）。
/// </summary>
public sealed class BackendTimeoutException : Exception
{
    public BackendTimeoutException(string message) : base(message) { }
}

/// <summary>
/// [EN] Manages the EchoBackend.exe child process and talks to it over a JSON-lines protocol
/// [EN] on stdin/stdout.
///
/// [EN] Communication model (v0.2.0 refactor one):
/// <list type="bullet">
/// <item>
/// [EN] A single pump thread reads stdout continuously and posts each line to a queue;
/// [EN] the caller takes lines from the queue, so timeouts are safe: an expired wait never
/// [EN] leaves a dangling read behind (which would otherwise attribute the next response to
/// [EN] the wrong request).
/// </item>
/// <item>
/// [EN] "Write request + read response" is still serialized by the request gate (stdout is a
/// [EN] single shared stream).
/// </item>
/// <item>
/// [EN] Both the gate wait and the response wait have thresholds: a timeout throws a
/// [EN] recognizable exception, and the UI shows a hint instead of waiting forever.
/// </item>
/// </list>
///
/// [ZH] 管理 EchoBackend.exe 子进程，通过 stdin/stdout 以 JSON 行协议通信。
///
/// [ZH] 通信模型（v0.2.0 修复一重构）：
/// [ZH] 1. stdout 由唯一的「泵」线程持续读取并投递到队列，请求方从队列取行，因此可以
/// [ZH] 安全地设置超时——超时不会留下悬挂的读取操作（否则会读错下一条响应的归属）。
/// [ZH] 2. 「写请求 + 读响应」仍由请求闸门串行化（stdout 是单条共享流）。
/// [ZH] 3. 闸门等待与响应等待都有阈值：超时抛出可识别的异常，界面给出提示而非无限
/// [ZH] 等待。
/// </summary>
public sealed class BackendService : IDisposable
{
    private Process? _process;
    private StreamWriter? _writer;
    private StreamReader? _reader;
    private StreamReader? _stdErrReader;
    private CancellationTokenSource? _pumpCts;
    private bool _stopping;

    // [EN] stdout line queue: single writer (the pump), multiple readers (the gate ensures a
    // [EN] single reader at a time).
    // [ZH] stdout 行队列：单写（泵）多读（闸门保证同时只有一个读取方）
    private readonly Channel<string> _lines = Channel.CreateUnbounded<string>(
        new UnboundedChannelOptions { SingleWriter = true, SingleReader = false });

    // [EN] Request gate: stdout is a single shared stream, so concurrent requests from the main
    // [EN] window and the settings window would read each other's responses; all "write request
    // [EN] + read response" calls are therefore serialized. (stop only writes and does not
    // [EN] read, so it bypasses the gate.)
    // [ZH] 请求闸门：stdout 是单条共享流，主窗口与设置窗口并发请求会互相读错响应，
    // [ZH] 因此所有「写请求 + 读响应」的调用串行化。（stop 只写不读，不经过闸门）
    private readonly SemaphoreSlim _requestGate = new(1, 1);

    /// <summary>
    /// [EN] Default threshold for waiting on the gate: once it is exceeded the backend is
    /// [EN] considered busy and the user is asked to retry later.
    /// [ZH] 默认等待闸门释放的阈值：超过即认为后端忙，提示用户稍后重试。
    /// </summary>
    public static readonly TimeSpan DefaultQueueTimeout = TimeSpan.FromSeconds(15);

    /// <summary>
    /// [EN] Default threshold for waiting on a response (idle timeout: the timer restarts every
    /// [EN] time a line arrives).
    /// [ZH] 默认等待响应的阈值（空闲超时：每收到一行即重新计时）。
    /// </summary>
    public static readonly TimeSpan DefaultIdleTimeout = TimeSpan.FromMinutes(10);

    /// <summary>
    /// [EN] Response threshold for heavy tasks (parsing large files, chunked summaries, model
    /// [EN] downloads).
    /// [ZH] 重任务（解析大文件、分段摘要、模型下载）的响应阈值。
    /// </summary>
    public static readonly TimeSpan HeavyIdleTimeout = TimeSpan.FromMinutes(45);

    public BackendService(string? backendPath = null) => _backendPath = backendPath ?? ResolveBackendPath();

    private readonly string _backendPath;

    /// <summary>
    /// [EN] Full path of the backend executable (the release layout is
    /// [EN] app\backend\EchoDeskBackend.exe).
    /// [ZH] 后端可执行文件完整路径（发布布局为 app\backend\EchoDeskBackend.exe）。
    /// </summary>
    public string BackendPath => _backendPath;

    /// <summary>
    /// [EN] Backend working directory: config.json, session.json, models/ and skills_dev/ all
    /// [EN] live under it (that is, app\backend\). Note that **user data is not here**:
    /// [EN] `ui_state.json`, snippets and "results" sit in the outer directory, see
    /// [EN] ShortcutService.UserDataDirectory (fix seven).
    /// [ZH] 后端工作目录：config.json、session.json、models/、skills_dev/ 都在该目录下
    /// [ZH] （即 app\backend\）。注意**用户数据不在**这里 —— `ui_state.json`、常用语与
    /// [ZH] 「results」在外层目录，见 ShortcutService.UserDataDirectory（修复七）。
    /// </summary>
    public string? BackendDirectory => Path.GetDirectoryName(_backendPath);

    /// <summary>
    /// [EN] Locates the backend executable. In the release layout the backend sits in the
    /// [EN] <c>app\backend\</c> subfolder (PyInstaller onedir), while
    /// [EN] <see cref="AppContext.BaseDirectory"/> is the <c>app\</c> folder that holds the
    /// [EN] front end exe, so the relative combination below holds in both layouts (flat and
    /// [EN] the three-level portable one). During development the <c>ECHODESK_BACKEND</c>
    /// [EN] environment variable can point at the executable.
    /// [ZH] 定位后端可执行文件。发布布局下后端位于 <c>app\backend\</c> 子文件夹
    /// [ZH] （PyInstaller onedir），而 <see cref="AppContext.BaseDirectory"/> 就是前端 exe
    /// [ZH] 所在的 <c>app\</c>，因此下面的相对拼接在两种布局（扁平/便携版三层）下都成立。
    /// [ZH] 开发时可用环境变量 <c>ECHODESK_BACKEND</c> 指定路径。
    /// </summary>
    public static string ResolveBackendPath()
    {
        string? overridePath = Environment.GetEnvironmentVariable("ECHODESK_BACKEND");
        if (!string.IsNullOrEmpty(overridePath) && File.Exists(overridePath))
            return overridePath;

        string dir = AppContext.BaseDirectory;
        string[] candidates =
        {
            Path.Combine(dir, "backend", "EchoDeskBackend.exe"),  // [EN] v0.2.0 release layout.
                                                                  // [ZH] v0.2.0 发布布局。
            Path.Combine(dir, "EchoDeskBackend.exe"),             // [EN] Front end directory.
                                                                  // [ZH] 后端与前端同目录。
            Path.Combine(dir, "EchoBackend.exe"),                 // [EN] v0.1.0 layout.
                                                                  // [ZH] 兼容 v0.1.0 布局。
        };
        foreach (string candidate in candidates)
        {
            if (File.Exists(candidate))
                return candidate;
        }
        // [EN] When nothing is found, return the expected path so that error messages can point
        // [EN] at it.
        // [ZH] 都找不到时返回期望路径，便于报错信息指明位置
        return candidates[0];
    }

    /// <summary>
    /// [EN] Version declared by the backend (for example v0.1.0).
    /// [ZH] 后端声明中的版本号（如 v0.1.0）。
    /// </summary>
    public string? Version { get; private set; }

    /// <summary>
    /// [EN] Studio signature declared by the backend.
    /// [ZH] 后端声明中的工作室署名。
    /// </summary>
    public string? Studio { get; private set; }

    /// <summary>
    /// [EN] Number of conversation turns restored automatically at startup.
    /// [ZH] 启动时自动恢复的会话轮数。
    /// </summary>
    public int RestoredCount { get; private set; }

    /// <summary>
    /// [EN] Conversation history restored at startup (role/content/reasoning), for display in
    /// [EN] the UI. v0.2.1 reasoning separation: `reasoning` is the reasoning text of that
    /// [EN] assistant turn (older sessions or plain models do not have this field, in which
    /// [EN] case it is an empty string); the UI rebuilds the collapsed reasoning area from it.
    /// [ZH] 启动时恢复的会话历史（role/content/reasoning），供界面展示。
    /// [ZH] v0.2.1 模型思考内容分离：`reasoning` 是该轮助手回复的思考内容（
    /// [ZH] 老会话或普通模型没有这个字段，此时为空串），界面据此重建折叠的思考区。
    /// </summary>
    public List<(string Role, string Content, string Reasoning)> RestoredHistory { get; } = new();

    /// <summary>
    /// [EN] Whether inference is available (v0.2.1 optimization, development task 1).
    ///
    /// [EN] "The backend is ready" and "inference works" are **two different things**: when the
    /// [EN] model is missing the backend is still ready and serving; only inference features
    /// [EN] return guidance. Defaults to true, and the front end enters no-model mode only when
    /// [EN] the backend explicitly says false.
    /// [ZH] 推理是否可用（v0.2.1 综合优化 任务一）。
    ///
    /// [ZH] "后端已就绪"和"能推理"是**两件事**：模型缺失时后端照样就绪并服务，
    /// [ZH] 只是推理类功能返回引导。默认 true，只有后端明确说 false 才进入无模型模式。
    /// </summary>
    public bool ModelReady { get; private set; } = true;

    /// <summary>
    /// [EN] Real path of the model folder (shown to the user in the guidance text so that a
    /// [EN] .gguf file can be placed manually).
    /// [ZH] 模型文件夹的真实路径（引导文案里要给用户看，便于手动放置 .gguf）。
    /// </summary>
    public string ModelsDir { get; private set; } = "";

    /// <summary>
    /// [EN] Guidance text produced by the backend when no model is available (it contains three
    /// [EN] viable paths and the model folder path).
    /// [ZH] 无模型时后端给出的引导文案（含三条可行路径与模型文件夹路径）。
    /// </summary>
    public string ModelGuide { get; private set; } = "";

    /// <summary>
    /// [EN] File name of the currently loaded model (the one actually loaded by the engine).
    /// [ZH] 当前加载的模型文件名（引擎里实际加载的那个）。
    /// </summary>
    public string LoadedModel { get; private set; } = "";

    /// <summary>
    /// [EN] Raised when model availability changes (no model → loaded, or the reverse).
    /// [EN] The UI switches the status bar and the inference entry point between "guidance" and
    /// [EN] "normal" accordingly, without restarting the front end (development task 6).
    /// [ZH] 模型可用状态发生变化（无模型 → 已加载，或反之）。
    /// [ZH] 界面据此把状态栏与推理入口在"引导"与"正常"之间自动切换，无需重启前端（任务六）。
    /// </summary>
    public event Action<bool>? ModelReadyChanged;

    /// <summary>
    /// [EN] Every line the backend writes to stderr (raised on a background thread for the UI
    /// [EN] to show startup/download progress).
    /// [ZH] 后端 stderr 输出的每一行（后台线程触发，供界面显示启动/下载进度）。
    /// </summary>
    public event Action<string>? StderrReceived;

    /// <summary>
    /// [EN] Raised when the backend process exits unexpectedly (not through Stop), so the UI
    /// [EN] can notify the user in a dialog.
    /// [ZH] 后端进程异常退出（非主动 Stop）时触发，供界面在对话框中提示用户。
    /// </summary>
    public event Action<int>? ProcessExited;

    public bool IsRunning => _process is { HasExited: false };

    /// <summary>
    /// [EN] Starts the backend child process and waits until it reports ready after
    /// [EN] initialization (model loading + warm-up).
    /// [ZH] 启动后端子进程，并等待其完成初始化（模型加载 + 预热）后就绪。
    /// </summary>
    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = _backendPath,
            // [EN] The working directory is the backend folder: configuration, sessions and
            // [EN] models/ are resolved relative to it, so the models are found no matter which
            // [EN] directory the front end is started from.
            // [ZH] 工作目录设为后端所在目录：配置、会话与 models/ 都相对它解析，
            // [ZH] 这样无论从哪个目录启动前端都能找到模型。
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
        // [EN] The backend protocol contains Chinese text and emoji and always writes UTF-8;
        // [EN] read it explicitly as UTF-8 here so that machines with a non-Chinese locale
        // [EN] (cp1252) do not end up with mojibake or decoding exceptions.
        // [ZH] 后端协议含中文/emoji 且始终按 UTF-8 输出；这里显式用 UTF-8 读取，
        // [ZH] 避免在非中文 locale（cp1252）的机器上读成乱码或解码异常。
        var utf8 = new UTF8Encoding(false);
        _reader = new StreamReader(_process.StandardOutput.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);
        _stdErrReader = new StreamReader(_process.StandardError.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);

        // [EN] Sole reader: posts every stdout line to the queue.
        // [ZH] 唯一读取者：把 stdout 的每一行投递到队列
        _pumpCts = new CancellationTokenSource();
        _ = Task.Run(() => PumpAsync(_pumpCts.Token));
        // [EN] Collects stderr in the background so backend problems are easier to diagnose.
        // [ZH] 后台收集 stderr，便于排查后端问题
        _ = Task.Run(ReadErrorsAsync);

        await WaitForReadyAsync(cancellationToken);
    }

    /// <summary>
    /// [EN] stdout pump: reads and posts lines continuously, and closes the queue when the
    /// [EN] process ends or the service stops.
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
                _lines.Writer.TryWrite(line);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or OperationCanceledException)
        {
            // [EN] A read interrupted by the process ending or by an explicit stop is expected.
            // [ZH] 进程结束/主动停止导致的读取中断属正常现象
        }
        finally
        {
            _lines.Writer.TryComplete();
        }
    }

    /// <summary>
    /// [EN] Sends a JSON request and returns the JSON text of the backend response (occasional
    /// [EN] non-JSON diagnostic lines are skipped).
    /// [ZH] 发送一个 JSON 请求，返回后端响应的 JSON 文本（跳过偶发的非 JSON 诊断行）。
    /// </summary>
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
            // [EN] A non-streaming response can also carry model state (model_state /
            // [EN] model_ready): after model_scan or model_load, for example, the UI must
            // [EN] switch to "model available" immediately (development task 6).
            // [ZH] 非流式响应里也可能带模型状态（model_state / model_ready）：
            // [ZH] 例如 model_scan、model_load 之后界面要立刻切到"有模型"（任务六）。
            try
            {
                using JsonDocument doc = JsonDocument.Parse(line);
                ApplyModelState(doc.RootElement);
            }
            catch (JsonException)
            {
                // [EN] When the response is not valid JSON, leave it to the caller to handle
                // [EN] as-is.
                // [ZH] 响应不是合法 JSON 时交给调用方按原样处理
            }
            return line;
        }
        finally
        {
            _requestGate.Release();
        }
    }

    /// <summary>
    /// [EN] Sends a request without reading a response (for actions such as stop that expect no
    /// [EN] response, so there is no race with streaming reads).
    /// [ZH] 只写不读地发送一个请求（用于 stop 等不期待响应的动作，避免与流式读取竞争）。
    /// </summary>
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
    /// [EN] Sends a streaming request: each delta fragment is displayed incrementally through
    /// [EN] the onDelta callback, and the full reply text is returned.
    /// [EN] onContext is called when the done line carries used/limit (to refresh context
    /// [EN] usage).
    /// [EN] onEvent (v0.2.1 optimization, development task 4): every response is handed to the
    /// [EN] caller as (type, root) so that structured progress other than delta (such as
    /// [EN] `summary_progress`) can be handled. **v0.2.1 reasoning separation**:
    /// [EN] `onEvent("done", root)` is also called once before the `done` line, which lets the
    /// [EN] caller read `reasoning` (the full reasoning text), `reasoning_unclosed` and similar
    /// [EN] fields.
    /// [EN] **Note**: `root` is only valid during this callback (the JsonDocument is disposed
    /// [EN] afterwards); copy any value you need on the spot instead of keeping the
    /// [EN] JsonElement.
    /// [EN] The streaming request can be cancelled through cancellationToken (the "Stop" button
    /// [EN] in the UI); by default there is no idle timeout, because automation may wait a long
    /// [EN] time for manual input (a CAPTCHA) without producing a new fragment.
    /// [EN] onDeltaChannel (v0.2.1 reasoning separation): deltas that carry a `channel` field
    /// [EN] here: `("reasoning", text, false)` is reasoning, `("answer", text, false)` is the
    /// [EN] final answer, and `("answer", "", true)` invalidates the answer area (the text sent
    /// [EN] so far actually belongs to the reasoning, so the caller must move the accumulated
    /// [EN] answer text into the reasoning area). Old-style deltas without `channel` (progress,
    /// [EN] summaries and so on) still go through onDelta; the two paths do not interfere.
    /// [ZH] 发送流式请求：每个 delta 片段通过 onDelta 回调增量展示，返回完整回复文本；
    /// [ZH] onContext 在 done 行携带 used/limit 时回调（用于刷新上下文占用）。
    /// [ZH] onEvent（v0.2.1 综合优化 任务四）：每一条应答都以 (type, root) 交给调用方，
    /// [ZH] 用于处理 delta 之外的**结构化进度**（`summary_progress` 等）。
    /// [ZH] **v0.2.1 模型思考内容分离**：还在 `done` 行前调用一次 `onEvent("done", root)`，
    /// [ZH] 调用方据此拿到 `reasoning`（思考全文）、`reasoning_unclosed` 等字段。
    /// [ZH] **注意**：`root` 只在这个回调期间有效（JsonDocument 随后即释放），需要留用请
    /// [ZH] 当场取出值，不要保存 JsonElement。
    /// [ZH] 流式请求可以被 cancellationToken 取消（界面「停止」按钮），默认不设空闲超时
    /// [ZH] ——自动化可能因等待人工输入（验证码）长时间没有新片段。
    /// [ZH] onDeltaChannel（v0.2.1 模型思考内容分离）：带 `channel` 字段的 delta 走这里
    /// [ZH] ——`("reasoning", text, false)` 思考内容、`("answer", text, false)` 最终回答、
    /// [ZH] `("answer", "", true)` 作废回答区（此前发的内容其实属于思考，调用方需把已
    /// [ZH] 累积的回答文本搬到思考区）。没有 `channel` 的旧式 delta（进度/摘要等）仍走
    /// [ZH] onDelta，两条路互不干扰。
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
                            // [EN] Dual channel (reasoning / answer): not appended to full; the
                            // [EN] done line carries the authoritative full text.
                            // [ZH] 双通道（思考 / 回答）：不累加进 full，done 行会给出权威全文
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
                        // [EN] Let the caller take the extra done-line fields before the
                        // [EN] JsonDocument is disposed.
                        // [ZH] 让调用方在 JsonDocument 释放前取走 done 行的附加字段
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

    /// <summary>
    /// [EN] Acquires the request gate; throws <see cref="BackendBusyException"/> when the gate
    /// [EN] is still held after the threshold.
    /// [ZH] 获取请求闸门；超过阈值仍未释放则抛出 <see cref="BackendBusyException"/>。
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
            throw new BackendBusyException(Loc.T("main.error.busy"));
    }

    /// <summary>
    /// [EN] Takes one line from the queue; throws <see cref="BackendTimeoutException"/> when
    /// [EN] the idle threshold is exceeded, and returns null when the queue is closed.
    /// [ZH] 从队列取一行；超过空闲阈值抛出 <see cref="BackendTimeoutException"/>，队列关闭返回
    /// [ZH] null。
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
            // [EN] v0.2.2 batch 4: `host_progress` is a host progress event that the backend
            // [EN] sends on its own while handling a **non-streaming** request (see
            // [EN] `_emit_host_progress` in backend/main.py); it is **not a response**.
            // [EN] Skip it and forward it here; otherwise it would be returned to the caller as
            // [EN] the response, and the symptom would be "the request succeeded but did
            // [EN] nothing".
            // [ZH] v0.2.2 第 4 批：`host_progress` 是后端在处理**非流式**请求期间主动发的
            // [ZH] 宿主进度事件（见 backend/main.py 的 `_emit_host_progress`），**不是响应**。
            // [ZH] 在这里跳过并转交出去 —— 否则它会被当成响应返回给调用方，
            // [ZH] 表现就是"请求成功了但什么都没做"。
            if (TryReadHostProgress(line, out string hostText))
            {
                HostProgressReceived?.Invoke(hostText);
                continue;
            }
            return line;
        }
    }

    /// <summary>
    /// [EN] Host progress event (sent by the backend on its own while it handles a
    /// [EN] non-streaming request; v0.2.2 batch 4).
    /// [ZH] 宿主进度事件（后端在处理非流式请求期间主动发来；v0.2.2 第 4 批）。
    /// </summary>
    public event Action<string>? HostProgressReceived;

    /// <summary>
    /// [EN] Checks whether a line is a host progress event, and if so takes out its body.
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
            // [EN] Local model missing: **no longer** throws to make the UI ask "download or
            // [EN] quit". Development task 1: the backend has already entered "ready without a
            // [EN] model", so just record it here (the guidance text is used together with)
            // [EN] ready) and keep waiting for ready. The backend does not exit because the
            // [EN] model is missing, and the front end should not treat that as a startup
            // [EN] failure.
            // [ZH] 本地缺少模型：**不再**抛异常让界面弹"是否下载、否则退出"。
            // [ZH] 任务一：后端已进入"无模型就绪"，这里只把它记下来（引导文案随 ready 一起用），
            // [ZH] 然后继续等 ready —— 后端不会因为缺模型而退出，前端也不该把这个当成启动失败。
            if (type == "need_download")
            {
                ApplyNeedDownload(root);
                continue;
            }
            // [EN] System messages such as an initialization failure: throw directly so that
            // [EN] the front end can show the concrete reason.
            // [ZH] 初始化失败等系统消息：直接抛出，让前端展示具体原因
            if (type == "system")
            {
                string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                throw new InvalidOperationException(content);
            }
        }
    }

    /// <summary>
    /// [EN] After the user agrees to download the model: sends model_download and reads the
    /// [EN] download progress (onDelta) and messages (onMessage) until the backend has finished
    /// [EN] downloading, loading and warm-up and emits the ready declaration.
    /// [ZH] 用户同意下载模型后：发送 model_download，读取下载进度（onDelta）与提示（onMessage），
    /// [ZH] 直到后端完成下载、加载与预热并发出 ready 声明。
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
            // [EN] Downloading a large model can take a long time without a new progress line
            // [EN] (slow network), so the threshold is generous.
            // [ZH] 下载大模型可能长时间没有新进度行（慢速网络），阈值给足
            string? line = await ReadLineAsync(HeavyIdleTimeout, CancellationToken.None);
            if (line is null)
                throw new InvalidOperationException(Loc.T("main.error.exitedDuringDownload", "后端在下载模型期间退出。"));
            if (!line.TrimStart().StartsWith('{'))
                continue;

            using JsonDocument doc = JsonDocument.Parse(line);
            JsonElement root = doc.RootElement;
            string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "" : "";
            string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            ApplyModelState(root);   // [EN] The done after the download carries model_state.
                                     // [ZH] 下载完成后的 done 带 model_state，界面据此切到"有模型"
            switch (type)
            {
                case "delta":
                    onDelta?.Invoke(content);
                    break;
                case "done":
                    onMessage?.Invoke(content);
                    break;  // [EN] Download complete; keep waiting for ready.
                            // [ZH] 下载完成，继续等 ready
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
    /// [EN] Records the "model missing" notice (development task 1): only the guidance
    /// [EN] information is updated; **the startup flow is not interrupted**. The old behavior
    /// [EN] treated `need_download` as a throwable exception, so the whole application became
    /// [EN] unusable as soon as the user declined; that is exactly the corner that had to be
    /// [EN] removed.
    /// [ZH] 记录"缺模型"这条告知（任务一）：只更新引导信息，**不打断启动流程**。
    /// [ZH] 旧行为把 `need_download` 当成可抛出异常，用户一拒绝就整个应用不可用 —— 那正是
    /// [ZH] 要拆掉的墙角。
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
    /// [EN] Updates the model state from any response (development task 6: after a download
    /// [EN] finishes or a model is hot-loaded the front end switches automatically, with no
    /// [EN] restart). Two kinds of fields are recognized: `model_ready` (boolean) and
    /// [EN] `model_state` (object).
    /// [ZH] 从任意响应里更新模型状态（任务六：下载完成/热加载后前端自动切换，无需重启）。
    /// [ZH] 认两类字段：`model_ready`（布尔）与 `model_state`（对象）。
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
        // [EN] Development task 1: the ready declaration explicitly distinguishes "the backend
        // [EN] is ready" from "inference is available".
        // [ZH] 任务一：就绪声明里明确区分"后端已就绪"与"推理可用"
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
                // [EN] Reasoning text (v0.2.1 reasoning separation): older sessions do not have
                // [EN] this field; when it is absent the value is an empty string.
                // [ZH] 思考内容（v0.2.1 模型思考内容分离）：老会话没有这个字段，取不到就是空串
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
            // [EN] A read interrupted when the process ends is expected.
            // [ZH] 进程结束时读取中断属正常现象
        }
    }

    private void Process_Exited(object? sender, EventArgs e)
    {
        // [EN] An explicit Stop() closes the writer and kills the process first, which may
        // [EN] trigger this event; a flag distinguishes the two cases to avoid a false alarm.
        // [ZH] 主动 Stop() 会先关闭 writer 并 Kill，期间可能触发本事件；用标志位区分，避免误报。
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

    /// <summary>
    /// [EN] Stops the backend process and releases its resources.
    /// [ZH] 停止后端进程并释放资源。
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
