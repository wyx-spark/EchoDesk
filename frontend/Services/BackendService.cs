using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace EchoDesk.Services;

/// <summary>后端报告本地缺少模型（need_download）：前端据此弹确认框，用户同意后再下载。</summary>
public sealed class NeedDownloadException : Exception
{
    public NeedDownloadException(string model, long size, string repo, string detail)
        : base(detail.Length > 0 ? detail : $"本地缺少模型 {model}")
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
/// 管理 EchoBackend.exe 子进程，通过 stdin/stdout 以 JSON 行协议通信。
/// 启动时等待后端就绪声明（含预热完成），支持流式响应读取。
/// </summary>
public sealed class BackendService : IDisposable
{
    private Process? _process;
    private StreamWriter? _writer;
    private StreamReader? _reader;
    private StreamReader? _stdErrReader;
    private bool _stopping;
    private readonly string _backendPath;
    // 请求闸门：stdout 是单条共享流，主窗口与设置窗口并发请求会互相读错响应，
    // 因此所有「写请求 + 读响应」的调用串行化。（stop 只写不读，不经过闸门）
    private readonly SemaphoreSlim _requestGate = new(1, 1);

    public BackendService(string? backendPath = null) => _backendPath = backendPath ?? ResolveBackendPath();

    /// <summary>后端可执行文件完整路径（发布布局为 程序目录\backend\EchoDeskBackend.exe）。</summary>
    public string BackendPath => _backendPath;

    /// <summary>后端工作目录：config.json、session.json、models/ 都在该目录下。</summary>
    public string? BackendDirectory => Path.GetDirectoryName(_backendPath);

    /// <summary>
    /// 定位后端可执行文件。发布布局下后端位于 <c>backend\</c> 子文件夹（PyInstaller onedir），
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

    /// <summary>启动时恢复的会话历史（role/content），供界面展示。</summary>
    public List<(string Role, string Content)> RestoredHistory { get; } = new();

    /// <summary>后端 stderr 输出的每一行（后台线程触发，供界面显示启动/下载进度）。</summary>
    public event Action<string>? StderrReceived;

    /// <summary>后端进程异常退出（非主动 Stop）时触发，供界面在对话框中提示用户。</summary>
    public event Action<int>? ProcessExited;

    public bool IsRunning => _process is { HasExited: false };

    /// <summary>启动后端子进程，并等待其完成初始化（模型加载 + 预热）后就绪。</summary>
    public async Task StartAsync()
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
        _process = Process.Start(startInfo) ?? throw new InvalidOperationException("无法启动后端进程。");
        _process.EnableRaisingEvents = true;
        _process.Exited += Process_Exited;
        _writer = _process.StandardInput;
        // 后端协议含中文/emoji 且始终按 UTF-8 输出；这里显式用 UTF-8 读取，
        // 避免在非中文 locale（cp1252）的机器上读成乱码或解码异常。
        var utf8 = new UTF8Encoding(false);
        _reader = new StreamReader(_process.StandardOutput.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);
        _stdErrReader = new StreamReader(_process.StandardError.BaseStream, utf8, detectEncodingFromByteOrderMarks: false);
        // 后台收集 stderr，便于排查后端问题
        _ = Task.Run(ReadErrorsAsync);
        await WaitForReadyAsync();
    }

    /// <summary>发送一个 JSON 请求，返回后端响应的 JSON 文本（跳过偶发的非 JSON 诊断行）。</summary>
    public async Task<string> SendRequestAsync(object request)
    {
        if (_writer is null || _reader is null)
            throw new InvalidOperationException("后端尚未启动。");
        if (!IsRunning)
            throw new InvalidOperationException("后端进程已退出。");

        await _requestGate.WaitAsync();
        try
        {
            await _writer.WriteLineAsync(JsonSerializer.Serialize(request));
            await _writer.FlushAsync();
            return await ReadUntilJsonAsync();
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
            throw new InvalidOperationException("后端尚未启动。");
        if (!IsRunning)
            throw new InvalidOperationException("后端进程已退出。");
        await _writer.WriteLineAsync(JsonSerializer.Serialize(request));
        await _writer.FlushAsync();
    }

    /// <summary>
    /// 发送流式请求：每个 delta 片段通过 onDelta 回调（UI 线程）增量展示，
    /// 返回完整回复文本。
    /// </summary>
    /// <summary>
    /// 发送流式请求：每个 delta 片段通过 onDelta 回调（UI 线程）增量展示，
    /// 返回完整回复文本；onContext 在 done 行携带 used/limit 时回调（用于刷新上下文占用）。
    /// </summary>
    public async Task<string> SendStreamRequestAsync(object request, Action<string> onDelta, Action<int, int>? onContext = null)
    {
        if (_writer is null || _reader is null)
            throw new InvalidOperationException("后端尚未启动。");
        if (!IsRunning)
            throw new InvalidOperationException("后端进程已退出。");

        await _requestGate.WaitAsync();
        try
        {
            await _writer.WriteLineAsync(JsonSerializer.Serialize(request));
            await _writer.FlushAsync();

            var full = new StringBuilder();
            while (true)
            {
                string? line = await _reader.ReadLineAsync();
                if (line is null)
                    throw new InvalidOperationException("后端没有返回响应（进程可能已退出）。");
                if (!line.TrimStart().StartsWith('{'))
                {
                    Debug.WriteLine($"[backend:ignored] {line}");
                    continue;
                }

                using JsonDocument doc = JsonDocument.Parse(line);
                JsonElement root = doc.RootElement;
                string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "" : "";
                string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                switch (type)
                {
                    case "delta":
                        full.Append(content);
                        onDelta?.Invoke(content);
                        break;
                    case "done":
                        if (onContext is not null
                            && root.TryGetProperty("used", out JsonElement usedEl)
                            && root.TryGetProperty("limit", out JsonElement limitEl))
                            onContext(usedEl.GetInt32(), limitEl.GetInt32());
                        return content.Length > 0 ? content : full.ToString();
                    case "error":
                        throw new InvalidOperationException(content);
                    default:
                        Debug.WriteLine($"[backend:msg] {line}");
                        break;
                }
            }
        }
        finally
        {
            _requestGate.Release();
        }
    }

    private async Task<string> ReadUntilJsonAsync()
    {
        while (true)
        {
            string? line = await _reader!.ReadLineAsync();
            if (line is null)
                throw new InvalidOperationException("后端没有返回响应（进程可能已退出）。");
            if (!line.TrimStart().StartsWith('{'))
            {
                Debug.WriteLine($"[backend:ignored] {line}");
                continue;
            }
            return line;
        }
    }

    private async Task WaitForReadyAsync()
    {
        while (true)
        {
            string? line = await _reader!.ReadLineAsync();
            if (line is null)
                throw new InvalidOperationException("后端未完成启动即退出。");
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
            // 本地缺少模型：交给界面弹确认框，用户同意后再下载
            if (type == "need_download")
                throw BuildNeedDownload(root);
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
            throw new InvalidOperationException("后端尚未启动。");

        await _writer.WriteLineAsync(JsonSerializer.Serialize(new { action = "model_download", name }));
        await _writer.FlushAsync();

        while (true)
        {
            string? line = await _reader.ReadLineAsync();
            if (line is null)
                throw new InvalidOperationException("后端在下载模型期间退出。");
            if (!line.TrimStart().StartsWith('{'))
                continue;

            using JsonDocument doc = JsonDocument.Parse(line);
            JsonElement root = doc.RootElement;
            string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "" : "";
            string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
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

    private void ApplyReady(JsonElement root)
    {
        Version = root.TryGetProperty("version", out JsonElement v) ? v.GetString() : null;
        Studio = root.TryGetProperty("studio", out JsonElement s) ? s.GetString() : null;
        RestoredCount = root.TryGetProperty("restored", out JsonElement r) ? r.GetInt32() : 0;
        if (root.TryGetProperty("history", out JsonElement hist) && hist.ValueKind == JsonValueKind.Array)
        {
            RestoredHistory.Clear();
            foreach (JsonElement m in hist.EnumerateArray())
            {
                string role = m.TryGetProperty("role", out JsonElement rl) ? rl.GetString() ?? "" : "";
                string content = m.TryGetProperty("content", out JsonElement ct) ? ct.GetString() ?? "" : "";
                if (role.Length > 0)
                    RestoredHistory.Add((role, content));
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
    }

    public void Dispose() => Stop();
}
