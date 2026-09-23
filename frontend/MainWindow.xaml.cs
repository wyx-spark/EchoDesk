using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using EchoDesk.Services;
using EchoDesk.Utilities;

namespace EchoDesk;

public partial class MainWindow : Window
{
    private const string ModePrefix = "✅ 已切换模式为: ";
    private const string ThanksMessage = "👋 感谢使用 Void Entropia Studio 出品的 EchoDesk，欢迎您下次使用！";

    private BackendService? _backend;
    private bool _isProcessing;
    private bool _isDark;
    private bool _stopRequested;
    private bool _stopMode;          // 发送按钮当前处于"停止"模式（流式生成中）
    private bool _commandLike;       // 聊天输入框出现指令格式（"/"+英文字母）
    private bool _commandRowOpen;    // 指令输入行是否展开
    private double _fontSize = 14;
    private TextBox? _paletteSource;
    private DispatcherTimer? _exitTimer;
    private int _exitCountdown;
    private readonly SnippetManager _snippets = new();
    // 本地界面状态（窗口大小/位置/字号/主题、已拒绝的询问），存于程序目录 ui_state.json
    private readonly AppSettings _settings = AppSettings.Load();
    // 下载/预热等实时进度在对话框中的"活"消息（内容块），复用更新避免刷屏
    private TextBox? _liveStatusContent;

    // 流式 Markdown 实时渲染：节流计时器，边生成边把累积文本渲染进 RichTextBox
    private DispatcherTimer? _mdTimer;
    private RichTextBox? _mdTarget;
    private string _mdPending = "";
    // 底部通知条计时器（下载完成/自动化完成/错误提示）
    private DispatcherTimer? _toastTimer;
    // 设置窗口（单实例：再次点击时激活已有窗口）
    private SettingsWindow? _settingsWindow;

    // 聊天输入框中的指令前缀（"/" + 连续英文字母，允许只有一个 "/"）
    private static readonly Regex CommandPrefixRegex = new(@"^/[A-Za-z]*(?: [A-Za-z]+)*", RegexOptions.Compiled);

    // 指令数据（/help 第一行展示 /help 自身；Format 为参数格式，Description 用于完整帮助）
    private static readonly CommandItem[] Commands =
    {
        new("/help", "", "显示此指令介绍：完整指令列表、用法与作用", "显示全部指令及用法", false),
        new("/mode", "<编号|缩写|全称>", "切换对话模式（general/summary/translate/code/contract/creative）；/mode list 查看全部模式及编号；切换时自动套用温度预设", "切换对话模式", true),
        new("/file", "<路径>", "上传本地文件并加入上下文；支持 Office/TXT/PDF，加密 Office 文档在装有 WPS 的机器上自动解密；提问时提及文件即自动注入内容。长文档超出上下文窗口时自动分段摘要后注入，不截断丢信息", "上传文件加入上下文", true),
        new("/summarize", "", "对当前已加载的长文档手动触发全文分段摘要（逐段摘要后合并），摘要进度实时显示在对话框中", "手动触发全文摘要", false),
        new("/search", "on|off", "开启/关闭联网搜索（DuckDuckGo）；开启后提问前会先搜索网络参考信息（需联网）", "开启/关闭联网搜索", true),
        new("/model", "[名称|别名|list]", "查看模型信息（架构/量化/上下文/GPU），或切换本地模型；支持档案别名与前缀匹配；缺失时征询后下载", "查看/切换本地模型", true),
        new("/clearcache", "", "清空推理 KV 缓存（模式/提示词/历史变化时系统会自动清理）", "清空推理 KV 缓存", false),
        new("/prompt", "<文本>|reset", "设置自定义系统提示词，临时改变助手角色；/prompt reset 清除", "设置自定义系统提示词", true),
        new("/temp", "<0.0~1.0>|reset", "调整生成温度（默认随模式预设）；/temp reset 恢复模式预设", "调整生成温度", true),
        new("/darkmode", "on|off", "切换深色/浅色界面主题（即时渲染，无需重启）", "切换深/浅色主题", true),
        new("/stop", "", "停止当前正在生成中的回复（也可点 ⏹ 停止 按钮）", "停止当前生成", false),
        new("/snippet", "add|remove|list …", "管理常用语模板：/snippet add 名称 文本 · remove 名称 · list", "管理常用语模板", true),
        new("/status", "", "查看当前状态：模型/架构、模式、温度、线程、GPU、上下文占用、搜索、对话轮数与文件", "查看当前状态与占用", false),
        new("/clear", "", "清空对话历史与文件上下文，重新开始", "清空对话重新开始", false),
        new("/undo", "", "撤回最近一轮问答（各一条）", "撤回最近一轮问答", false),
        new("/save", "<文件名>", "把对话历史导出为 Markdown 文件（自动补 .md 后缀）", "导出对话为 Markdown", true),
        new("/about", "", "关于 EchoDesk：版本号与出品方信息", "关于 EchoDesk", false),
        new("/exit", "", "退出程序：显示感谢语后 10 秒自动关闭", "退出程序", false),
    };

    public MainWindow()
    {
        InitializeComponent();
        AllowDrop = true; // 允许文件拖拽
        Loaded += MainWindow_Loaded;
    }

    // ---------- 生命周期 ----------

    private async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        RestoreWindowState();
        // 主题与字号来自上次会话（ui_state.json）
        if (!_settings.DarkMode)
            ApplyTheme(false);
        _fontSize = Math.Clamp(_settings.FontSize, 10, 24);
        UpdateAllMessageFontSize();

        _backend = new BackendService();
        // 后端连接完成前禁用全部控件，并提示连接状态
        SetControlsEnabled(false);
        StatusText.Text = "正在连接后端...";
        AddMessage("system", "⏳ 正在连接后端，请稍候...");
        // 实时显示后端起模型下载等 stderr 进度
        _backend.StderrReceived += Backend_StderrReceived;
        // 后端异常退出时在对话框提示，避免用户只看上方状态栏
        _backend.ProcessExited += Backend_ProcessExited;
        try
        {
            // 等待后端完成模型加载 + 会话恢复 + 预热后就绪
            await _backend.StartAsync();
        }
        catch (NeedDownloadException ex)
        {
            // 本地缺少模型：先征询用户，同意才下载
            if (!await ConfirmStartupDownloadAsync(ex))
                return; // 用户拒绝：后端已退出，界面保留提示
        }
        catch (Exception ex)
        {
            StatusText.Text = "后端启动失败";
            AddMessage("system", $"❌ 后端启动失败: {ex.Message}");
            SetControlsEnabled(false); // 保持禁用
            return;
        }

        _backend.StderrReceived -= Backend_StderrReceived;
        SetControlsEnabled(true);
        StatusText.Text = "已就绪";
        string welcome = $"✨ 欢迎使用 EchoDesk {_backend.Version} — Powered by {_backend.Studio}";
        if (_backend.RestoredCount > 0)
            welcome += $"\n📂 已自动恢复上次会话（{_backend.RestoredCount} 轮）";
        AddMessage("system", welcome);
        // 展示恢复的历史消息，让用户能查看之前的对话内容
        foreach (var (role, content) in _backend.RestoredHistory)
        {
            if (role == "user")
                AddMessage("user", content);
            else if (role == "assistant")
                AddMessage("assistant", content);
            else
                AddMessage("system", content);
        }
        MessageScrollViewer.ScrollToBottom();
        InputBox.Focus();
        _ = UpdateContextAsync(); // 显示初始上下文占用
        _ = RefreshStatusBarAsync(); // 状态栏：模型 / 模式 / 搜索状态
        ShowFirstRunGuideIfNeeded();
        CheckStartupShortcut();   // 启动时检测程序目录下的启动快捷方式
    }

    // ---------- 窗口状态记忆（大小/位置/最大化，兼容多显示器与高 DPI） ----------

    /// <summary>恢复上次的窗口大小、位置与最大化状态；位置已不可见时回退居中。</summary>
    private void RestoreWindowState()
    {
        if (_settings.WindowWidth >= 400 && _settings.WindowHeight >= 320)
        {
            Width = _settings.WindowWidth;
            Height = _settings.WindowHeight;
        }
        if (!double.IsNaN(_settings.WindowLeft) && !double.IsNaN(_settings.WindowTop)
            && IsRectVisible(_settings.WindowLeft, _settings.WindowTop, Width, Height))
        {
            WindowStartupLocation = WindowStartupLocation.Manual;
            Left = _settings.WindowLeft;
            Top = _settings.WindowTop;
        }
        if (_settings.WindowMaximized)
            WindowState = WindowState.Maximized;
    }

    /// <summary>窗口矩形是否与虚拟屏幕（含所有显示器）相交，避免还原到已拔掉的显示器上。</summary>
    private static bool IsRectVisible(double left, double top, double width, double height)
    {
        var virtualScreen = new Rect(
            SystemParameters.VirtualScreenLeft,
            SystemParameters.VirtualScreenTop,
            SystemParameters.VirtualScreenWidth,
            SystemParameters.VirtualScreenHeight);
        var window = new Rect(left, top, width, height);
        // 至少露出一部分标题栏才认为可见
        return virtualScreen.IntersectsWith(window)
            && window.Left + window.Width > virtualScreen.Left + 40
            && window.Top + window.Height > virtualScreen.Top + 20;
    }

    /// <summary>关闭时保存窗口状态（最大化时记录还原后的尺寸，下次还原才不会撑满屏）。</summary>
    private void SaveWindowState()
    {
        Rect bounds = WindowState == WindowState.Normal
            ? new Rect(Left, Top, Width, Height)
            : RestoreBounds;
        if (bounds.Width >= 400 && bounds.Height >= 320)
        {
            _settings.WindowWidth = bounds.Width;
            _settings.WindowHeight = bounds.Height;
            _settings.WindowLeft = bounds.Left;
            _settings.WindowTop = bounds.Top;
        }
        _settings.WindowMaximized = WindowState == WindowState.Maximized;
        _settings.FontSize = _fontSize;
        _settings.DarkMode = _isDark;
        _settings.Save();
    }

    // ---------- 状态栏（模型 / 模式 / 搜索 / 自动化 / 市场） ----------

    /// <summary>向后端查询 /status 与自动化/市场状态并刷新状态栏。</summary>
    private async Task RefreshStatusBarAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "chat", message = "/status" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("content", out JsonElement content))
                ApplyStatusText(content.GetString() ?? "");
        }
        catch
        {
            // 后端忙碌/异常时忽略，后续交互会再次刷新
            return;
        }
        await RefreshAutomationStatusAsync();
        await RefreshMarketStatusAsync();
    }

    /// <summary>刷新状态栏的自动化状态段。</summary>
    private async Task RefreshAutomationStatusAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "automation_status" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("automation", out JsonElement st))
                return;
            bool running = st.TryGetProperty("running", out JsonElement r) && r.ValueKind == JsonValueKind.True;
            bool enabled = st.TryGetProperty("enabled", out JsonElement en) && en.ValueKind == JsonValueKind.True;
            AutoText.Text = running ? "自动化: 运行中" : enabled ? "自动化: 已启用" : "自动化: 关";
        }
        catch
        {
            // 忽略
        }
    }

    /// <summary>刷新状态栏的技能市场连接状态段。</summary>
    private async Task RefreshMarketStatusAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "market_status" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("market_status", out JsonElement st))
                return;
            bool enabled = st.TryGetProperty("enabled", out JsonElement en) && en.ValueKind == JsonValueKind.True;
            string state = st.TryGetProperty("state", out JsonElement s) ? s.GetString() ?? "" : "";
            SetMarketStatus(!enabled ? "未启用" : state.Length > 0 ? state : "就绪");
        }
        catch
        {
            // 忽略
        }
    }

    /// <summary>解析 /status 输出，刷新状态栏各段。</summary>
    private void ApplyStatusText(string status)
    {
        foreach (string raw in status.Split('\n'))
        {
            string line = raw.Trim();
            if (line.StartsWith("🧠 模型:", StringComparison.Ordinal))
            {
                ModelText.Text = $"模型: {Shorten(line["🧠 模型:".Length..].Trim())}";
                ModelText.ToolTip = line["🧠 模型:".Length..].Trim();
            }
            else if (line.StartsWith("🛠️", StringComparison.Ordinal) && line.Contains("模式:"))
            {
                ModeText.Text = $"模式: {line[(line.IndexOf("模式:", StringComparison.Ordinal) + 3)..].Trim()}";
            }
            else if (line.Contains("联网搜索:"))
            {
                bool on = line.Contains("开启");
                SearchText.Text = on ? "搜索: 开" : "搜索: 关";
            }
        }
    }

    /// <summary>状态栏模型名过长时截断显示。</summary>
    private static string Shorten(string name)
    {
        string trimmed = name.EndsWith(".gguf", StringComparison.OrdinalIgnoreCase) ? name[..^5] : name;
        return trimmed.Length <= 26 ? trimmed : trimmed[..25] + "…";
    }

    /// <summary>刷新自动化状态段（自动化引擎接入后调用）。</summary>
    private void SetAutomationStatus(bool running, bool enabled = true)
    {
        AutoText.Text = !enabled ? "自动化: 未启用" : running ? "自动化: 运行中" : "自动化: 关";
    }

    /// <summary>刷新技能市场连接状态段（市场接入后调用）。</summary>
    private void SetMarketStatus(string text) => MarketText.Text = $"市场: {text}";

    // ---------- 首次启动引导 ----------

    /// <summary>首次启动时展示欢迎与上手引导（模型下载、快捷方式、常用操作）。</summary>
    private void ShowFirstRunGuideIfNeeded()
    {
        if (_settings.FirstRunDone)
            return;
        _settings.FirstRunDone = true;
        _settings.Save();
        AddMessage("system",
            "🎉 首次使用引导\n" +
            "1️⃣ 模型：首次启动若本地缺少模型，会询问是否下载；也可手动把 .gguf 放到 models/ 目录。\n" +
            "2️⃣ 启动方式：可在程序目录创建「EchoDesk 启动」快捷方式，双击即可运行（已按你的选择处理）。\n" +
            "3️⃣ 长文档：上传合同等长文件时，超出上下文窗口会自动分段摘要后再回答，不会截断丢信息。\n" +
            "4️⃣ 常用指令：输入 /help 查看全部指令；输入 / 会弹出指令列表。\n" +
            "5️⃣ 复制内容：每条消息右下角有「复制 / Markdown / 代码」按钮，也可拖选文本后按 Ctrl+C。\n" +
            "6️⃣ 快捷键：Ctrl+Enter 发送 · Esc 停止 · Ctrl+L 清空 · Ctrl+O 上传 · Ctrl+Shift+C 复制全部 · Ctrl+滚轮 调字号");
        Toast("🎉 欢迎首次使用 EchoDesk，已显示上手引导");
    }

    /// <summary>本地缺少模型时弹确认框；用户同意则下载并等待后端就绪，返回是否继续启动。</summary>
    private async Task<bool> ConfirmStartupDownloadAsync(NeedDownloadException ex)
    {
        string size = ex.Size > 0 ? $"（约 {ex.Size / 1048576.0:0} MB）" : "（大小未知）";
        AddMessage("system", $"📦 本地未找到所需模型：{ex.Model}{size}\n来源仓库：{ex.Repo}");
        StatusText.Text = "等待确认下载";

        var result = MessageBox.Show(
            $"本地未找到所需模型：\n\n{ex.Model}{size}\n来源仓库：{ex.Repo}\n\n" +
            "是否现在下载？下载需要联网，模型较大时可能耗时较长。\n" +
            "选择「否」将退出程序（也可稍后手动把模型文件放入 models/ 目录）。",
            "需要下载模型",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (result != MessageBoxResult.Yes)
        {
            AddMessage("system", "❌ 已取消下载模型，后端已退出。\n可手动把模型文件放入 models/ 目录后重新启动 EchoDesk。");
            StatusText.Text = "未下载模型";
            SetControlsEnabled(false);
            _backend?.Stop();
            return false;
        }

        AddMessage("system", "⏳ 正在下载模型，进度在下方对话框实时显示...");
        StatusText.Text = "正在下载模型……";
        try
        {
            await _backend!.DownloadModelAndWaitReadyAsync(
                ex.Model,
                delta =>
                {
                    UpdateLiveStatus(delta);
                    StatusText.Text = SimplifyStatus(delta);
                },
                message => AddMessage("system", message));
            Toast($"✅ 模型已就绪：{ex.Model}");
            _ = RefreshStatusBarAsync();
            return true;
        }
        catch (Exception e)
        {
            AddMessage("system", $"❌ 模型下载失败: {e.Message}");
            Toast($"❌ 模型下载失败：{e.Message}", error: true);
            StatusText.Text = "模型下载失败";
            SetControlsEnabled(false);
            return false;
        }
    }

    /// <summary>
    /// 启动时检测程序目录下的启动快捷方式：不存在则询问是否创建；
    /// 已失效（移动文件夹/改名后目标丢失）则提示重新创建；用户拒绝后记录状态不再重复询问。
    /// </summary>
    private void CheckStartupShortcut()
    {
        try
        {
            if (ShortcutService.IsValid())
                return;

            if (ShortcutService.Exists())
            {
                var recreate = MessageBox.Show(
                    $"检测到程序目录下的启动快捷方式已失效：\n{ShortcutService.ShortcutPath}\n\n" +
                    "（通常是因为程序文件夹被移动或改名）\n\n是否重新创建？",
                    "快捷方式已失效",
                    MessageBoxButton.YesNo,
                    MessageBoxImage.Warning);
                if (recreate == MessageBoxResult.Yes)
                    ReportShortcutCreated();
                return;
            }

            if (_settings.ShortcutPromptDeclined)
                return;  // 用户之前已拒绝，不再询问

            var create = MessageBox.Show(
                "未在程序目录下检测到启动快捷方式。\n\n" +
                $"是否创建「{ShortcutService.ShortcutFileName}」？\n" +
                "创建后在程序文件夹内双击它即可启动 EchoDesk，无需分辨哪个可执行文件是入口。",
                "创建启动快捷方式",
                MessageBoxButton.YesNo,
                MessageBoxImage.Question);
            if (create == MessageBoxResult.Yes)
            {
                ReportShortcutCreated();
            }
            else
            {
                _settings.ShortcutPromptDeclined = true;
                _settings.Save();
                AddMessage("system", "ℹ️ 已跳过创建启动快捷方式（后续不再询问，可手动创建）。");
            }
        }
        catch (Exception ex)
        {
            AddMessage("system", $"⚠️ 快捷方式处理失败: {ex.Message}");
        }
    }

    private void ReportShortcutCreated()
    {
        try
        {
            string path = ShortcutService.Create();
            AddMessage("system", $"✅ 已创建启动快捷方式：{path}");
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 创建快捷方式失败: {ex.Message}");
        }
    }

    /// <summary>后端 stderr 进度（下载/预热等）：完整内容进对话框，状态栏只显示简化状态。</summary>
    private void Backend_StderrReceived(string line)
    {
        // 只展示下载/加载/预热相关进度，避免无关日志刷屏
        if (line.Contains("正在下载") || line.Contains("下载") || line.Contains("加载模型") || line.Contains("预热"))
        {
            Dispatcher.Invoke(() =>
            {
                UpdateLiveStatus(line.Trim());
                StatusText.Text = SimplifyStatus(line.Trim());
            });
        }
    }

    /// <summary>把完整进度写进对话框：复用同一条"活"消息，避免每条百分比都新增一条。</summary>
    private void UpdateLiveStatus(string text)
    {
        if (_liveStatusContent is null)
        {
            var panel = NewMessagePanel("system", text, out FrameworkElement block);
            _liveStatusContent = (TextBox)block;
            MessagePanel.Children.Add(panel);
        }
        else
        {
            _liveStatusContent.Text = "ℹ️ " + text;
        }
        MessageScrollViewer.ScrollToBottom();
    }

    /// <summary>把详细进度行简化为状态栏短文本。</summary>
    private static string SimplifyStatus(string line)
    {
        if (line.Contains("预热")) return "正在预热模型……";
        if (line.Contains("下载")) return "正在下载模型……";
        if (line.Contains("加载")) return "正在加载模型……";
        return line;
    }

    /// <summary>后端进程异常退出时，在对话框中显著提示（后台线程触发，需调度回 UI）。</summary>
    private void Backend_ProcessExited(int exitCode)
    {
        Dispatcher.Invoke(() =>
        {
            StatusText.Text = "后端已退出";
            AddMessage("system", $"⚠️ 后端进程意外退出（退出码 {exitCode}）。请检查程序所在目录的 backend_crash.log，或重启 EchoDesk。");
        });
    }

    /// <summary>统一启用/禁用输入区域所有控件（连接后端期间禁用）。</summary>
    private void SetControlsEnabled(bool enabled)
    {
        SendButton.IsEnabled = enabled;
        UploadButton.IsEnabled = enabled;
        SnippetButton.IsEnabled = enabled;
        CommandButton.IsEnabled = enabled;
        ContextButton.IsEnabled = enabled;
        InputBox.IsEnabled = enabled;
        CommandInputBox.IsEnabled = enabled;
    }

    protected override void OnClosed(EventArgs e)
    {
        _exitTimer?.Stop();
        _toastTimer?.Stop();
        SaveWindowState();
        _backend?.Dispose();
        base.OnClosed(e);
    }

    // ---------- 快捷键（窗口级） ----------

    /// <summary>
    /// 快捷键：Ctrl+Enter 发送 · Esc 停止生成 · Ctrl+L 清空 · Ctrl+O 上传 ·
    /// Ctrl+Shift+C 复制全部 · Ctrl+加/减/0 调字号 · Ctrl+T 深浅色。
    /// （换行由输入框的 Enter / Shift+Enter 直接处理，无需拦截）
    /// </summary>
    protected override void OnPreviewKeyDown(KeyEventArgs e)
    {
        bool ctrl = Keyboard.Modifiers == ModifierKeys.Control;
        bool ctrlShift = Keyboard.Modifiers == (ModifierKeys.Control | ModifierKeys.Shift);

        if (ctrlShift && e.Key == Key.C)
        {
            e.Handled = true;
            CopyAllMessages();
            return;
        }
        if (ctrl)
        {
            switch (e.Key)
            {
                case Key.L:
                    e.Handled = true;
                    _ = RunLocalCommandAsync("/clear");
                    return;
                case Key.O:
                    e.Handled = true;
                    UploadButton_Click(this, new RoutedEventArgs());
                    return;
                case Key.T:
                    e.Handled = true;
                    HandleDarkMode("/darkmode");
                    return;
                case Key.OemComma:   // Ctrl+, 打开设置
                    e.Handled = true;
                    OpenSettings();
                    return;
                case Key.M:          // Ctrl+M 打开设置并切到技能市场
                    e.Handled = true;
                    OpenSettings(openMarket: true);
                    return;
                case Key.OemPlus:
                case Key.Add:
                    e.Handled = true;
                    AdjustFontSize(1);
                    return;
                case Key.OemMinus:
                case Key.Subtract:
                    e.Handled = true;
                    AdjustFontSize(-1);
                    return;
                case Key.D0:
                case Key.NumPad0:
                    e.Handled = true;
                    AdjustFontSize(14 - _fontSize); // 恢复默认字号
                    return;
            }
        }
        if (e.Key == Key.Escape)
        {
            if (CommandPalette.IsOpen || SnippetPalette.IsOpen)
            {
                CloseAllPalettes();
                e.Handled = true;
                return;
            }
            if (_stopMode)
            {
                e.Handled = true;
                _ = TriggerStopAsync();
            }
            return;
        }
        base.OnPreviewKeyDown(e);
    }

    // ---------- 设置窗口 ----------

    /// <summary>
    /// 打开设置窗口（单实例），主题与字号变化即时同步回主窗口。
    /// 构造与显示一律在主线程同步完成且不做任何后端等待，因此窗口「打开即见」；
    /// 万一构造异常（历史缺陷：FontSizeSlider 回调空引用），也只记录日志并提示，
    /// 不再让整个进程被终止。
    /// </summary>
    private void OpenSettings(bool openMarket = false)
    {
        if (_backend is null)
            return;
        if (_settingsWindow is { IsLoaded: true })
        {
            _settingsWindow.Activate();
            if (openMarket)
                _settingsWindow.ShowMarketTab();
            return;
        }
        try
        {
            var window = new SettingsWindow(
                _backend, _settings, _isDark, _fontSize,
                onThemeChanged: dark => ApplyTheme(dark),
                onFontSizeChanged: size => AdjustFontSize(size - _fontSize));
            _settingsWindow = window;
            window.Closed += (_, _) =>
            {
                _settingsWindow = null;
                _ = RefreshStatusBarAsync();  // 设置可能改了模式/自动化状态
            };
            window.Owner = this;
            window.Show();
        }
        catch (Exception ex)
        {
            _settingsWindow = null;
            App.Log("MainWindow.OpenSettings", ex);
            AddMessage("system", $"❌ 打开设置窗口失败：{ex.Message}");
            Toast($"打开设置窗口失败：{ex.Message}");
            return;
        }
        if (openMarket)
            _settingsWindow.ShowMarketTab();
    }

    private void SettingsButton_Click(object sender, RoutedEventArgs e) => OpenSettings();

    // ---------- 危险指令确认（修复二） ----------

    /// <summary>
    /// 有副作用且不可逆的指令。执行前会二次确认（可在设置「常规」中关闭）。
    /// <c>/exit</c> 不在其中：它本身已有 10 秒可中断的倒计时与感谢提示，等价于内置确认。
    /// </summary>
    private static readonly HashSet<string> DestructiveCommands =
        new(StringComparer.OrdinalIgnoreCase) { "/clear", "/clearcache" };

    /// <summary>判断一串输入是否以危险指令开头。</summary>
    private static string? DestructiveCommandOf(string command)
    {
        string head = command.Trim();
        int space = head.IndexOf(' ');
        if (space > 0)
            head = head[..space];
        return DestructiveCommands.Contains(head) ? head : null;
    }

    /// <summary>危险指令执行前的确认对话框；返回 false 表示用户取消。</summary>
    private bool ConfirmDestructive(string command)
    {
        string? key = DestructiveCommandOf(command);
        if (key is null || !_settings.ConfirmDestructiveCommands)
            return true;
        string detail = key.ToLowerInvariant() switch
        {
            "/clearcache" => "将清空推理 KV 缓存：不会删除任何文件，只是下次回复需要重新计算上下文（略慢）。",
            "/clear" => "将清空当前全部对话历史与已加载的文件上下文，此操作不可撤销。",
            _ => "",
        };
        MessageBoxResult result = MessageBox.Show(
            this,
            $"确认执行 {key} ？\n\n{detail}\n\n（可在「设置 → 常规」中关闭该确认）",
            "确认执行指令", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No);
        return result == MessageBoxResult.Yes;
    }

    /// <summary>执行指令输入框之外的本地指令（含前端自行处理的 /snippet、/darkmode）。</summary>
    private async Task RunLocalCommandAsync(string command)
    {
        if (_isProcessing || _backend is null || command.Length == 0)
            return;
        if (!ConfirmDestructive(command))
            return;
        SetProcessing(true);
        AddMessage("user", command);
        try
        {
            if (command.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(command);
            else if (command.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(command);
            else
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message = command }));
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 请求失败: {ex.Message}");
        }
        finally
        {
            SetProcessing(false);
        }
    }

    // ---------- 发送消息（对话输入栏） ----------

    private async void SendButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        if (_stopMode)
        {
            await TriggerStopAsync();
            return;
        }
        await SendMessageAsync();
    }

    private async void InputBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        // Ctrl+Enter 发送；其余（Enter / Shift+Enter）均插入换行
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.Control)
        {
            e.Handled = true;
            await SendMessageAsync();
        }
    }

    private void InputBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        CloseCommandPalette(); // 指令选择栏仅由指令输入框触发
        UpdateCommandUi(InputBox.Text);
    }

    private void InputBox_GotKeyboardFocus(object sender, KeyboardFocusChangedEventArgs e) => CloseAllPalettes();

    private async Task SendMessageAsync()
    {
        if (_isProcessing || _backend is null)
            return;
        string message = InputBox.Text.Trim();
        if (message.Length == 0)
            return;
        if (!ConfirmDestructive(message))
            return;   // 用户取消：保留输入框内容不动

        InputBox.Text = "";
        SetProcessing(true);
        AddMessage("user", message);

        try
        {
            if (message.Equals("/help", StringComparison.OrdinalIgnoreCase))
                ShowHelpLocally();
            else if (message.Equals("/stop", StringComparison.OrdinalIgnoreCase))
                await TriggerStopAsync();
            else if (message.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(message);
            else if (message.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(message); // 主题为前端职责，本地处理
            else if (message.StartsWith("/"))
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message }));
            else
                await SendStreamAsync(message); // 普通对话走流式输出
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 请求失败: {ex.Message}");
        }
        finally
        {
            SetProcessing(false);
        }
    }

    // ---------- 流式对话输出 ----------

    private async Task SendStreamAsync(string message)
    {
        // 从一开始就使用 RichTextBox，流式过程中即按 Markdown 实时渲染
        var panel = NewMessagePanel("assistant", "", out FrameworkElement contentBlock);
        panel.Children.Remove(contentBlock);
        var rich = CreateRichContent();
        panel.Children.Insert(1, rich);
        MessagePanel.Children.Add(panel);

        var sb = new StringBuilder("🤖 ");
        _mdTarget = rich;
        RenderInto(rich, sb.ToString()); // 初始只显示图标
        _stopRequested = false;
        _stopMode = true;
        SendButton.Content = "⏹";
        SendButton.ToolTip = "停止生成";
        SendButton.IsEnabled = true; // 流式期间按钮变为"停止"，必须可点击

        try
        {
            string full = await _backend!.SendStreamRequestAsync(
                new { action = "chat_stream", message },
                delta =>
                {
                    sb.Append(delta);
                    ScheduleMarkdownRender(sb.ToString()); // 节流实时渲染
                    MessageScrollViewer.ScrollToBottom();
                },
                onContext: (used, limit) => ContextText.Text = FormatContext(used, limit));
            if (_stopRequested && full.Length == 0)
                sb.Append("\n⏹（已停止生成）");
            FinalizeMarkdownRender(sb.ToString()); // 结束：完整渲染一次
            AddMessageActions(panel, sb.ToString(), sb.ToString()); // 生成结束后提供复制操作条
        }
        finally
        {
            StopMarkdownTimer();
            _stopMode = false;
            SendButton.Content = "➤";
            SendButton.ToolTip = "发送 (Ctrl+Enter)";
            SendButton.IsEnabled = true;
        }
    }

    // ---------- 流式 Markdown 实时渲染（节流） ----------

    private void ScheduleMarkdownRender(string text)
    {
        _mdPending = text;
        if (_mdTimer is { IsEnabled: true })
            return; // 已排定，下一次触发读取最新累积文本
        _mdTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(60) };
        _mdTimer.Tick += (_, _) =>
        {
            _mdTimer!.Stop();
            RenderInto(_mdTarget, _mdPending);
        };
        _mdTimer.Start();
    }

    private void FinalizeMarkdownRender(string text)
    {
        StopMarkdownTimer();
        RenderInto(_mdTarget, text);
    }

    private void StopMarkdownTimer()
    {
        if (_mdTimer != null)
        {
            _mdTimer.Stop();
            _mdTimer = null;
        }
    }

    private static void RenderInto(RichTextBox? target, string text)
    {
        if (target is null)
            return;
        target.Document = MarkdownRenderer.Build(text);
    }

    // ---------- 停止生成 ----------

    private async Task TriggerStopAsync()
    {
        if (_backend is null)
            return;
        _stopRequested = true;
        _stopMode = false;
        SendButton.IsEnabled = false;
        StatusText.Text = "正在停止...";
        try
        {
            // fire-and-forget：不读取响应，避免与流式读取竞争 stdout
            await _backend.SendFireAndForgetAsync(new { action = "stop" });
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 停止请求失败: {ex.Message}");
        }
    }

    // ---------- 发送指令（指令输入栏） ----------

    private async void CommandInputBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        if (e.Key == Key.Escape)
        {
            e.Handled = true;
            HideCommandInput();
            return;
        }
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.None)
        {
            e.Handled = true;
            string command = CommandInputBox.Text.Trim();
            if (command.Length > 0)
            {
                if (!ConfirmDestructive(command))
                    return;   // 取消执行时保留输入，便于用户修改
                CommandInputBox.Text = "";
                await SendCommandAsync(command);
            }
        }
    }

    private void CommandInputBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        // 防御：XAML 解析期若该框带初值会先于 CommandPlaceholder 创建而触发本回调
        if (CommandPlaceholder is null)
            return;
        CommandPlaceholder.Visibility = CommandInputBox.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        UpdateCommandPalette(CommandInputBox);
    }

    private async Task SendCommandAsync(string command)
    {
        if (_isProcessing || _backend is null)
            return;
        SetProcessing(true);
        AddMessage("user", command);
        try
        {
            if (command.Equals("/help", StringComparison.OrdinalIgnoreCase))
                ShowHelpLocally();
            else if (command.Equals("/stop", StringComparison.OrdinalIgnoreCase))
                await TriggerStopAsync();
            else if (command.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(command);
            else if (command.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(command);
            else
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message = command }));
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 请求失败: {ex.Message}");
        }
        finally
        {
            SetProcessing(false);
            HideCommandInput(); // 指令执行完毕后收起指令输入行
        }
    }

    // ---------- 指令选择栏 ----------

    /// <summary>
    /// 过滤并**排序**指令候选（修复二）。
    ///
    /// 此前只做前缀过滤、保持声明顺序，导致输入 <c>/clear</c> 时首项是 <c>/clearcache</c>，
    /// 直接按 Enter 会执行错误且有副作用的指令。排序规则：
    /// <list type="number">
    /// <item>与输入**完全一致**的命令排最前；</item>
    /// <item>其余按命令名长度升序（长度越短表示已输入前缀占该命令的比例越高、越具体）；</item>
    /// <item>最后按指令表的原有顺序，保证结果稳定可预期。</item>
    /// </list>
    /// </summary>
    internal static List<CommandItem> RankCommands(string text)
    {
        string query = text.Trim();
        string key = query.Length > 1 ? query[1..] : "";
        return Commands
            .Select((command, index) => (Command: command, Index: index))
            .Where(x => query.Length > 0
                        && query.StartsWith('/')
                        && (key.Length == 0
                            || x.Command.Command.StartsWith("/" + key, StringComparison.OrdinalIgnoreCase)))
            .OrderByDescending(x => string.Equals(x.Command.Command, query, StringComparison.OrdinalIgnoreCase))
            .ThenBy(x => x.Command.Command.Length)
            .ThenBy(x => x.Index)
            .Select(x => x.Command)
            .ToList();
    }

    private void UpdateCommandPalette(TextBox source)
    {
        string text = source.Text;
        if (text.Length > 0 && text.StartsWith('/'))
        {
            List<CommandItem> filtered = RankCommands(text);
            if (filtered.Count > 0)
            {
                _paletteSource = source;
                CommandPalette.PlacementTarget = source;
                CommandList.ItemsSource = filtered;
                CommandList.SelectedIndex = 0;
                CommandPalette.IsOpen = true;
                return;
            }
        }
        CloseCommandPalette();
    }

    private bool HandlePaletteKey(KeyEventArgs e)
    {
        if (!CommandPalette.IsOpen)
            return false;
        switch (e.Key)
        {
            case Key.Down:
                MoveCommandSelection(1);
                return true;
            case Key.Up:
                MoveCommandSelection(-1);
                return true;
            case Key.Tab:
                if (CommandList.SelectedItem is CommandItem tabItem)
                    ExecuteCommandItem(tabItem, completeOnly: true);
                return true;
            case Key.Escape:
                CloseCommandPalette();
                return true;
            case Key.Enter:
                if (CommandList.SelectedItem is not CommandItem item)
                    return false; // 无选中项 → 回落为正常发送
                string typed = (_paletteSource?.Text ?? "").Trim();
                bool exact = string.Equals(item.Command, typed, StringComparison.OrdinalIgnoreCase);
                // 若候选不唯一且首项并非精确匹配，先补全而不执行：
                // 匹配项多时（如 /s）应让用户看清完整指令与参数格式后再决定，
                // 而不是盲目执行列表里的第一项。
                if (!exact && CommandList.Items.Count > 1)
                {
                    ExecuteCommandItem(item, completeOnly: true);
                    Toast($"已补全为 {item.Command}，确认无误后再按 Enter 执行");
                    return true;
                }
                ExecuteCommandItem(item);
                return true;
        }
        return false;
    }

    private void ExecuteCommandItem(CommandItem item, bool completeOnly = false)
    {
        CloseCommandPalette();
        var target = _paletteSource ?? CommandInputBox;

        // 需要参数或 Tab 补全 → 只填入"指令+空格"，让用户继续输入参数
        if (item.NeedsArgs || completeOnly)
        {
            target.Text = item.Command + " ";
            target.CaretIndex = target.Text.Length;
            target.Focus();
            return;
        }
        // 无需参数 → 直接执行
        if (target == InputBox)
        {
            InputBox.Text = item.Command;
            _ = SendMessageAsync();   // 确认环节在 SendMessageAsync 内统一处理
        }
        else
        {
            // 危险指令在此确认：取消时保留输入框内容，便于用户修改
            if (!ConfirmDestructive(item.Command))
                return;
            CommandInputBox.Text = "";
            _ = SendCommandAsync(item.Command);
        }
    }

    private void MoveCommandSelection(int delta)
    {
        if (CommandList.Items.Count == 0)
            return;
        int index = (CommandList.SelectedIndex + delta + CommandList.Items.Count) % CommandList.Items.Count;
        CommandList.SelectedIndex = index;
        CommandList.ScrollIntoView(CommandList.SelectedItem);
    }

    private void CloseCommandPalette() => CommandPalette.IsOpen = false;

    private void CloseAllPalettes()
    {
        CommandPalette.IsOpen = false;
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
    }

    private void CommandList_PreviewMouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (CommandList.SelectedItem is not CommandItem item || e.OriginalSource is not DependencyObject source)
            return;
        // 仅当点击落在某个列表项上时才执行
        if (ItemsControl.ContainerFromElement(CommandList, source) is ListBoxItem container && container.IsSelected)
            ExecuteCommandItem(item);
    }

    /// <summary>聊天输入框出现 "/"+英文字母 时：只显示底部提示与按钮文字，不改动加减号图标。</summary>
    private void UpdateCommandUi(string text)
    {
        _commandLike = text.Length >= 1 && text.StartsWith('/') && (text.Length == 1 || IsAsciiLetter(text[1]));
        CommandBarHint.Visibility = _commandLike ? Visibility.Visible : Visibility.Collapsed;
        UpdateCommandButtonText();
    }

    /// <summary>命令按钮右侧文字：识别到指令→"转移到命令输入框"；否则按开合状态显示"打开/关闭命令输入框"。</summary>
    private void UpdateCommandButtonText()
    {
        CommandButtonText.Text = _commandLike ? "转移到命令输入框" : (_commandRowOpen ? "关闭命令输入框" : "打开命令输入框");
    }

    // ---------- 指令输入行（默认隐藏） ----------

    private void CommandButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        if (_commandRowOpen)
        {
            HideCommandInput();
            return;
        }

        // 把聊天输入框中的指令前缀（"/" 及后面的连续英文字母，允许只有单个 "/"）转移进指令输入框
        // 不加尾随空格（仅 Tab 补全时才补空格，避免用户删多余空格）
        string text = InputBox.Text;
        Match match = CommandPrefixRegex.Match(text);
        if (match.Success && match.Value.Length > 0)
        {
            string token = match.Value.TrimEnd();
            InputBox.Text = text[match.Length..];
            CommandInputBox.Text = token;
            InputBox.CaretIndex = InputBox.Text.Length;
        }
        ShowCommandInput();
    }

    private void ShowCommandInput()
    {
        _commandRowOpen = true;
        CommandInputRow.Visibility = Visibility.Visible;
        CommandIconRotate.Angle = 45; // 加号旋转 45° 变叉号
        UpdateCommandButtonText();
        CommandInputBox.Focus();
        CommandInputBox.CaretIndex = CommandInputBox.Text.Length;
    }

    private void HideCommandInput()
    {
        _commandRowOpen = false;
        CommandInputRow.Visibility = Visibility.Collapsed;
        CommandIconRotate.Angle = 0; // 恢复加号
        UpdateCommandButtonText();
        CloseAllPalettes();
        InputBox.Focus();
    }

    // ---------- 上下文占用（工具栏右侧） ----------

    /// <summary>从响应中读取附带的上文占用并刷新显示（/clear、/undo 后实时减少）。</summary>
    private void UpdateContextFromResponse(JsonElement root)
    {
        if (root.TryGetProperty("used", out JsonElement usedEl) && root.TryGetProperty("limit", out JsonElement limitEl))
            ContextText.Text = FormatContext(usedEl.GetInt32(), limitEl.GetInt32());
    }

    /// <summary>数字格式化：>=1000 显示 k，>=1000000 显示 m。</summary>
    private static string FormatTokens(int n) =>
        n >= 1_000_000 ? $"{n / 1_000_000.0:0.#}m"
        : n >= 1000 ? $"{n / 1000.0:0.#}k"
        : n.ToString();

    private static string FormatContext(int used, int limit) =>
        limit > 0 ? $"上下文：{FormatTokens(used)}/{FormatTokens(limit)}" : "上下文：---/---";

    /// <summary>向后端请求当前上下文占用并更新工具栏显示。</summary>
    private async Task UpdateContextAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "context" });
            using JsonDocument doc = JsonDocument.Parse(json);
            UpdateContextFromResponse(doc.RootElement);
        }
        catch
        {
            // 后端忙碌/异常时忽略，下次交换再更新
        }
    }

    private async void ContextButton_Click(object sender, RoutedEventArgs e) => await UpdateContextAsync();

    private static bool IsAsciiLetter(char c) =>
        (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');

    // ---------- 常用语（常用语模板选择框） ----------

    private void SnippetButton_Click(object sender, RoutedEventArgs e)
    {
        CloseCommandPalette();
        if (SnippetPalette.IsOpen)
        {
            CloseSnippetPalette();
            return;
        }
        SnippetList.ItemsSource = _snippets.Items;
        if (_snippets.Items.Count == 0)
        {
            AddMessage("system", "📋 暂无常用语，可先打开命令输入框输入 /snippet add 名称 文本 新增");
            return;
        }
        // 显示在输入区域上方：命令输入行可见时锚定命令行，否则锚定输入区域容器
        SnippetPalette.PlacementTarget = _commandRowOpen ? (UIElement)CommandInputRow : (UIElement)InputAreaBorder;
        SnippetPalette.IsOpen = true;
        UpdateSnippetButtonText();
    }

    private void CloseSnippetPalette()
    {
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
    }

    /// <summary>常用语按钮右侧文字：按选择框开合状态显示"打开/关闭常用语选择框"。</summary>
    private void UpdateSnippetButtonText()
    {
        SnippetButtonText.Text = SnippetPalette.IsOpen ? "关闭常用语选择框" : "打开常用语选择框";
    }

    private void SnippetList_PreviewMouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (SnippetList.SelectedItem is not Snippet snippet || e.OriginalSource is not DependencyObject source)
            return;
        if (ItemsControl.ContainerFromElement(SnippetList, source) is ListBoxItem container && container.IsSelected)
        {
            CloseSnippetPalette();
            int index = InputBox.CaretIndex;
            InputBox.Text = InputBox.Text.Insert(index, snippet.Text);
            InputBox.CaretIndex = index + snippet.Text.Length;
            InputBox.Focus();
        }
    }

    private void HandleSnippetCommand(string message)
    {
        var parts = message.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length < 2)
        {
            AddMessage("system", "用法: /snippet list | add 名称 文本 | remove 名称");
            return;
        }
        switch (parts[1].ToLowerInvariant())
        {
            case "list":
                if (_snippets.Items.Count == 0)
                {
                    AddMessage("system", "📋 暂无常用语，可用 /snippet add 名称 文本 新增");
                    return;
                }
                var sb = new StringBuilder("📋 常用语：\n");
                foreach (var s in _snippets.Items)
                    sb.AppendLine($"🔹 {s.Name}：{s.Text}");
                AddMessage("system", sb.ToString().TrimEnd());
                break;
            case "add":
                if (parts.Length < 4)
                {
                    AddMessage("system", "用法: /snippet add 名称 文本...");
                    return;
                }
                string name = parts[2];
                string text = string.Join(' ', parts.Skip(3));
                AddMessage("system", _snippets.Add(name, text)
                    ? $"✅ 已新增常用语「{name}」"
                    : $"❌ 常用语「{name}」已存在（可先 remove 再添加）");
                break;
            case "remove":
                if (parts.Length < 3)
                {
                    AddMessage("system", "用法: /snippet remove 名称");
                    return;
                }
                AddMessage("system", _snippets.Remove(parts[2])
                    ? $"🗑️ 已删除常用语「{parts[2]}」"
                    : $"❌ 未找到常用语「{parts[2]}」");
                break;
            default:
                AddMessage("system", "用法: /snippet list | add 名称 文本 | remove 名称");
                break;
        }
    }

    // ---------- 帮助 ----------

    private async Task ShowHelpAsync()
    {
        try
        {
            string json = await _backend!.SendRequestAsync(new { action = "chat", message = "/help" });
            HandleChatResponse(json);
        }
        catch
        {
            ShowHelpLocally(); // 后端不可用时回退本地渲染
        }
    }

    private void ShowHelpLocally()
    {
        var sb = new StringBuilder();
        sb.AppendLine("📖 EchoDesk v0.1.0 完整指令介绍 — Powered by Void Entropia Studio");
        sb.AppendLine();
        foreach (var c in Commands)
        {
            string usage = c.Format.Length > 0 ? $"{c.Command} {c.Format}" : c.Command;
            sb.AppendLine($"🔹 {usage}");
            sb.AppendLine($"   {c.Description}");
            sb.AppendLine();
        }
        AddMessage("system", sb.ToString().TrimEnd());
    }

    private void HandleChatResponse(string responseJson)
    {
        using JsonDocument doc = JsonDocument.Parse(responseJson);
        JsonElement root = doc.RootElement;
        string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "system" : "system";
        string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "无响应" : "无响应";
        string action = root.TryGetProperty("action", out JsonElement a) ? a.GetString() ?? "" : "";

        // /exit：先致谢并倒计时 10 秒，然后自动关闭
        if (action == "exit")
        {
            _backend?.Stop();
            BeginExitCountdown(ThanksMessage);
            return;
        }

        if (type == "assistant")
            AddMessage("assistant", content);
        else
        {
            AddMessage("system", content);
            if (content.StartsWith(ModePrefix, StringComparison.Ordinal))
                ModeText.Text = $"模式: {content[ModePrefix.Length..].Trim()}";
            // 搜索开关变化即时反映到状态栏
            if (content.Contains("搜索已开启", StringComparison.Ordinal))
                SearchText.Text = "搜索: 开";
            else if (content.Contains("搜索已关闭", StringComparison.Ordinal))
                SearchText.Text = "搜索: 关";
        }

        // /model 需要下载：征得用户同意后发送下载动作
        if (action == "confirm_model" && root.TryGetProperty("model", out JsonElement modelEl))
            _ = ConfirmModelDownloadAsync(modelEl.GetString() ?? "");

        // 响应附带的上下文占用：/clear、/undo 后实时减少
        UpdateContextFromResponse(root);
    }

    private async Task ConfirmModelDownloadAsync(string modelName)
    {
        var result = MessageBox.Show(
            $"确定要下载模型 '{modelName}' 吗？\n下载需要联网且可能耗时较长。",
            "确认下载模型",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (result != MessageBoxResult.Yes)
        {
            AddMessage("system", "已取消下载");
            return;
        }

        AddMessage("system", $"⏳ 正在下载并加载模型 '{modelName}'，请稍候...");
        StatusText.Text = "正在下载模型……";
        try
        {
            // 流式下载：完整进度进对话框，状态栏只显示简化状态
            string final = await _backend!.SendStreamRequestAsync(
                new { action = "model_download", name = modelName },
                delta =>
                {
                    UpdateLiveStatus(delta);
                    StatusText.Text = SimplifyStatus(delta);
                });
            AddMessage("system", final);
            Toast($"✅ 模型下载完成：{modelName}");
            _ = RefreshStatusBarAsync();
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 模型操作失败: {ex.Message}");
            Toast($"❌ 模型下载失败：{ex.Message}", error: true);
            StatusText.Text = "就绪";
        }
        StatusText.Text = "就绪";
    }

    // ---------- 退出倒计时 ----------

    private void BeginExitCountdown(string thanksMessage)
    {
        AddMessage("system", thanksMessage);
        StatusText.Text = "正在退出...";
        SetControlsEnabled(false);
        CloseAllPalettes();

        _exitCountdown = 10;
        StatusText.Text = $"程序将在 {_exitCountdown} 秒后自动关闭";
        _exitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _exitTimer.Tick += (_, _) =>
        {
            _exitCountdown--;
            if (_exitCountdown <= 0)
            {
                _exitTimer.Stop();
                Application.Current.Shutdown();
            }
            else
            {
                StatusText.Text = $"程序将在 {_exitCountdown} 秒后自动关闭";
            }
        };
        _exitTimer.Start();
    }

    // ---------- 文件上传 ----------

    private async void UploadButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = "选择要上传的文件",
            Filter = "支持的文件 (*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.txt;*.pdf)|*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.txt;*.pdf|所有文件 (*.*)|*.*",
        };
        if (dialog.ShowDialog() == true)
            await UploadFileAsync(dialog.FileName);
    }

    private async void MainWindow_Drop(object sender, DragEventArgs e)
    {
        if (_backend is null || !e.Data.GetDataPresent(DataFormats.FileDrop))
            return;
        string filePath = ((string[])e.Data.GetData(DataFormats.FileDrop))[0];
        await UploadFileAsync(filePath);
    }

    private async Task UploadFileAsync(string filePath)
    {
        if (_backend is null)
            return;
        AddMessage("system", $"📤 正在上传文件: {System.IO.Path.GetFileName(filePath)} ...");
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "file", path = filePath });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("content", out JsonElement content))
                AddMessage("system", content.GetString() ?? "上传完成");
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 上传失败: {ex.Message}");
        }
    }

    // ---------- 主题切换 ----------

    private void HandleDarkMode(string message)
    {
        string arg = message["/darkmode".Length..].Trim().ToLowerInvariant();
        bool dark = arg switch
        {
            "on" => true,
            "off" => false,
            _ => !_isDark, // 无参数时切换
        };
        ApplyTheme(dark);
        AddMessage("system", dark ? "🌙 已切换深色模式" : "☀️ 已切换浅色模式");
    }

    private void ApplyTheme(bool dark)
    {
        _isDark = dark;
        _settings.DarkMode = dark;
        _settings.Save();  // 主题即时生效并记住选择
        var dictionaries = Application.Current.Resources.MergedDictionaries;
        var theme = dictionaries.FirstOrDefault(
            d => d.Source?.OriginalString?.Contains("Theme.", StringComparison.Ordinal) == true);
        int index = theme is null ? 0 : dictionaries.IndexOf(theme);
        dictionaries[index] = new ResourceDictionary
        {
            Source = new Uri($"Resources/Theme.{(dark ? "Dark" : "Light")}.xaml", UriKind.Relative),
        };
    }

    // ---------- 字号调节（Ctrl+滚轮 / Ctrl+加号减号） ----------

    private void MessageScrollViewer_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (Keyboard.Modifiers == ModifierKeys.Control)
        {
            AdjustFontSize(e.Delta > 0 ? 0.5 : -0.5);
            e.Handled = true;
        }
    }

    /// <summary>调整全局字号并同步到所有消息控件（纯文本 / Markdown / 时间戳以外的内容）。</summary>
    private void AdjustFontSize(double delta)
    {
        _fontSize = Math.Clamp(_fontSize + delta, 10, 24);
        UpdateAllMessageFontSize();
        StatusText.Text = $"字号 {_fontSize:0.#}";
        _settings.FontSize = _fontSize;
        _settings.Save();
    }

    private void UpdateAllMessageFontSize()
    {
        foreach (var child in MessagePanel.Children)
        {
            if (child is not Panel panel)
                continue;
            foreach (var inner in panel.Children)
            {
                if (inner is not FrameworkElement fe || !Equals(fe.Tag, "content"))
                    continue;
                switch (fe)
                {
                    case TextBox box:          // 用户/系统消息（纯文本，可选中复制）
                        box.FontSize = _fontSize;
                        break;
                    case RichTextBox rich:     // 助手消息（Markdown；未显式设字号的 Run 会继承）
                        rich.FontSize = _fontSize;
                        break;
                    case TextBlock text:
                        text.FontSize = _fontSize;
                        break;
                }
            }
        }
    }

    // ---------- UI 辅助 ----------

    private void SetProcessing(bool processing)
    {
        _isProcessing = processing;
        SendButton.IsEnabled = !processing;
        StatusText.Text = processing ? "正在处理..." : "就绪";
    }

    /// <summary>创建可选中/可复制的纯文本内容控件（只读 TextBox：支持拖选与 Ctrl+C）。</summary>
    private TextBox CreatePlainContent(string text, Brush foreground)
    {
        return new TextBox
        {
            Text = text,
            IsReadOnly = true,
            IsReadOnlyCaretVisible = false,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            Padding = new Thickness(0),
            Margin = new Thickness(0),
            TextWrapping = TextWrapping.Wrap,
            // 禁用内部滚动：让滚轮事件冒泡给外层消息列表，滚动体验与整页一致
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            FontSize = _fontSize,
            Tag = "content",
            Foreground = foreground,
            Cursor = Cursors.IBeam,
            ToolTip = "可拖选文本，Ctrl+C 复制",
        };
    }

    /// <summary>创建助手消息用的只读 RichTextBox（Markdown 渲染 + 可选中复制）。</summary>
    private RichTextBox CreateRichContent()
    {
        return new RichTextBox
        {
            IsReadOnly = true,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            Padding = new Thickness(0),
            Margin = new Thickness(0),
            FontSize = _fontSize,
            Tag = "content",
            IsDocumentEnabled = true,
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
    }

    /// <summary>创建消息面板：右上角时间戳 + 内容（Tag="content" 便于统一调字号）+ 复制操作条。</summary>
    private StackPanel NewMessagePanel(string role, string text, out FrameworkElement content)
    {
        (string icon, string brushKey) = role switch
        {
            "user" => ("👤 ", "UserBrush"),
            "assistant" => ("🤖 ", "AssistantBrush"),
            _ => ("ℹ️ ", "SystemBrush"),
        };
        var brush = (Brush)(TryFindResource(brushKey) ?? Brushes.Gray);

        var time = new TextBlock
        {
            Text = DateTime.Now.ToString("HH:mm:ss"),
            FontSize = 10,
            Foreground = (Brush)(TryFindResource("SubTextBrush") ?? Brushes.Gray),
            HorizontalAlignment = HorizontalAlignment.Right,
        };
        content = CreatePlainContent(icon + text, brush);
        var panel = new StackPanel { Margin = new Thickness(5) };
        panel.Children.Add(time);
        panel.Children.Add(content);
        return panel;
    }

    /// <summary>在消息面板底部追加复制操作条（复制 / Markdown / 代码 / 全选）。</summary>
    private void AddMessageActions(StackPanel panel, string plainText, string? markdown = null)
    {
        var bar = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(0, 2, 0, 0),
        };

        string plain = plainText;
        bar.Children.Add(MakeActionButton("📋 复制", "复制为纯文本", () => CopyToClipboard(plain, "已复制为纯文本")));

        if (markdown is not null)
        {
            string md = markdown;
            bar.Children.Add(MakeActionButton("⧉ Markdown", "复制原始 Markdown 源码", () => CopyToClipboard(md, "已复制 Markdown 源码")));

            string code = MarkdownHelper.ExtractCodeBlocks(md);
            if (code.Length > 0)
                bar.Children.Add(MakeActionButton("</> 代码", "复制全部代码块", () => CopyToClipboard(code, "已复制代码块")));
        }

        var target = panel.Children.OfType<FrameworkElement>().FirstOrDefault(e => Equals(e.Tag, "content"));
        bar.Children.Add(MakeActionButton("全选", "选中本条消息全部文本", () => SelectAll(target)));
        panel.Children.Add(bar);
    }

    private static Button MakeActionButton(string caption, string tooltip, Action onClick)
    {
        var button = new Button
        {
            Content = caption,
            FontSize = 10,
            Padding = new Thickness(6, 1, 6, 1),
            Margin = new Thickness(4, 0, 0, 0),
            MinWidth = 0,
            Cursor = Cursors.Hand,
            ToolTip = tooltip,
            Style = Application.Current.TryFindResource("RoundButton") as Style,
        };
        button.Click += (_, _) => onClick();
        return button;
    }

    /// <summary>把文本写入剪贴板（剪贴板被占用时重试，失败给出提示）。</summary>
    private void CopyToClipboard(string text, string successMessage)
    {
        if (string.IsNullOrEmpty(text))
        {
            Toast("⚠️ 没有可复制的内容");
            return;
        }
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                Clipboard.SetText(text);
                Toast($"✅ {successMessage}（{text.Length} 字符）");
                return;
            }
            catch (Exception)
            {
                System.Threading.Thread.Sleep(60);  // 剪贴板被其他程序短暂占用
            }
        }
        Toast("❌ 复制失败：剪贴板被其他程序占用，请重试");
    }

    /// <summary>取出消息控件中的纯文本（TextBox 取 Text，RichTextBox 取文档文本）。</summary>
    private static string ReadContentText(FrameworkElement? element) => element switch
    {
        TextBox box => box.Text,
        RichTextBox rich => new TextRange(rich.Document.ContentStart, rich.Document.ContentEnd).Text.TrimEnd('\r', '\n'),
        _ => "",
    };

    private static void SelectAll(FrameworkElement? element)
    {
        switch (element)
        {
            case TextBox box:
                box.Focus();
                box.SelectAll();
                break;
            case RichTextBox rich:
                rich.Focus();
                rich.SelectAll();
                break;
        }
    }

    /// <summary>把全部对话复制为纯文本（快捷键 Ctrl+Shift+C）。</summary>
    private void CopyAllMessages()
    {
        var sb = new StringBuilder();
        foreach (var child in MessagePanel.Children)
        {
            if (child is not StackPanel panel)
                continue;
            var content = panel.Children.OfType<FrameworkElement>().FirstOrDefault(e => Equals(e.Tag, "content"));
            string text = ReadContentText(content);
            if (text.Length > 0)
                sb.AppendLine(text).AppendLine();
        }
        CopyToClipboard(sb.ToString().TrimEnd(), "已复制全部对话");
    }

    /// <summary>底部通知条：短暂显示后自动隐藏（下载完成/自动化完成/错误提示等）。</summary>
    private void Toast(string message, bool error = false)
    {
        ToastText.Text = message;
        ToastText.Foreground = (Brush)(TryFindResource(error ? "SystemBrush" : "AccentBrush") ?? Brushes.Gray);
        ToastBorder.Visibility = Visibility.Visible;
        HintText.Visibility = Visibility.Collapsed;
        _toastTimer ??= new DispatcherTimer { Interval = TimeSpan.FromSeconds(6) };
        _toastTimer.Stop();
        _toastTimer.Tick += (_, _) =>
        {
            _toastTimer!.Stop();
            ToastBorder.Visibility = Visibility.Collapsed;
            HintText.Visibility = Visibility.Visible;
        };
        _toastTimer.Start();
    }

    private void AddMessage(string role, string content)
    {
        var panel = NewMessagePanel(role, role == "assistant" ? "" : content, out FrameworkElement contentBlock);
        if (role == "assistant")
            ReplaceWithMarkdown(panel, contentBlock, content); // 助手消息渲染为 Markdown
        AddMessageActions(panel, content, role == "assistant" ? content : null);
        MessagePanel.Children.Add(panel);
        MessageScrollViewer.ScrollToBottom();
    }

    /// <summary>把消息面板中的纯文本块替换为渲染好的 Markdown（只读 RichTextBox）。</summary>
    private void ReplaceWithMarkdown(StackPanel panel, FrameworkElement contentBlock, string markdown)
    {
        panel.Children.Remove(contentBlock);
        var richText = CreateRichContent();
        richText.Document = MarkdownRenderer.Build(markdown);
        panel.Children.Insert(1, richText);
    }
}

/// <summary>指令选择栏中的一条指令：Command 指令名，Format 参数格式，Description 完整介绍（/help），Short 精简介绍（指令栏）。</summary>
public sealed record CommandItem(string Command, string Format, string Description, string Short, bool NeedsArgs)
{
    /// <summary>
    /// 指令列表里展示的完整用法（命令名 + 参数格式）。
    /// 修复二：明确显示参数格式，避免把 /clear 与 /clearcache 之类的相近指令看混。
    /// </summary>
    public string Usage => Format.Length > 0 ? $"用法：{Command} {Format}" : $"用法：{Command}（无参数）";
}
