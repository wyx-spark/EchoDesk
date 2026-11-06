// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/MainWindow.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/MainWindow.axaml.cs。类型与成员说明见下方注释。
// 主窗口（任务一原型 / 任务二第一个迁移模块，本轮补齐「各功能面板」）。
//
// 迁移策略：**事件处理逻辑照搬 WPF 版**（发送、流式双通道、停止、文件上传、状态刷新、
// 指令面板、常用语、通知条、状态区、Agent 步骤行），换掉的只有界面 API。
//
// 与 WPF 版对齐的**行为**（不是"长得像"，而是同样的判据与文案来源）：
//   · 指令表来自后端 {"action":"commands"}，后端没起来时用本地回退表；
//   · 候选排序规则逐条一致（精确匹配 > 名字长度 > 指令表顺序）；
//   · 状态区每一项的取值字段与 WPF 版相同（status / context / capability /
//     automation_status / market_status / update_check）；
//   · 通知条 6 秒后自动隐藏，显示时压掉操作提示；
//   · Agent 的 agent_step / tool_call / tool_result 各渲染成**独立一行**，
//     且随字号一起缩放；
//   · 危险指令要二次确认 —— Avalonia 没有 MessageBox，改成"同一个输入框里
//     再按一次 Enter"，与设置页"再点一次确认"的做法一致。
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Animation;
using Avalonia.Animation.Easings;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Input.Platform;
using Avalonia.Interactivity;
using Avalonia.Layout;
using Avalonia.Markup.Xaml;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Avalonia.VisualTree;
using EchoDesk.Avalonia.Services;
using EchoDesk.Avalonia.Utilities;

namespace EchoDesk.Avalonia.Views;

public partial class MainWindow : Window
{
    /// <summary>默认字号（Ctrl+0 恢复到这个值，与 WPF 的 14 一致）。</summary>
    private const double DefaultFontSize = 14;

    private readonly AppState _state = App.State;
    private readonly SnippetStore _snippets = new();

    /// <summary>已定稿的消息（用于换字号/换语言时整屏重渲染）。</summary>
    private readonly List<Message> _transcript = new();

    private CancellationTokenSource? _streamCts;
    private bool _busy;

    // [EN] Streaming state. The answer and reasoning buffers are appended from the backend
    //      reader thread, therefore every access is serialised through the gate below while the
    //      rendering pass itself executes on the UI thread only.
    // [ZH] 流式状态。回答与思考缓冲区由后端读取线程追加，因此所有访问均经下方闸门串行化，
    //      而渲染过程本身只在界面线程执行。
    private readonly object _streamGate = new();
    private readonly StringBuilder _liveAnswer = new();
    private readonly StringBuilder _liveReasoning = new();
    private MessageView? _live;
    private DispatcherTimer? _renderTimer;
    private volatile bool _renderDirty;
    private string _liveRendered = "";
    private int _renderCount;
    private Control? _liveCaret;
    private readonly Stopwatch _caretClock = Stopwatch.StartNew();

    /// <summary>
    /// [EN] Coalescing interval of the streaming render timer: at most one Markdown parse per
    ///      frame budget, which keeps tokens that arrive at a higher rate from each triggering
    ///      their own layout pass.
    /// [ZH] 流式渲染定时器的合并间隔：每个帧预算最多执行一次 Markdown 解析，使以更高频率到达的
    ///      Token 不会各自触发一次布局过程。
    /// </summary>
    private static readonly TimeSpan RenderInterval = TimeSpan.FromMilliseconds(33);

    /// <summary>
    /// [EN] Total duration of one breathing cycle of the streaming caret, in milliseconds.
    /// [ZH] 流式光标单个呼吸周期的总时长，单位为毫秒。
    /// </summary>
    private const double CaretBreathMilliseconds = 1200;


    // 指令面板 / 常用语面板
    private TextBox? _paletteSource;
    private bool _commandLike;
    private bool _commandRowOpen;
    private string _pendingDestructive = "";

    // 通知条 / 崩溃提示
    private DispatcherTimer? _toastTimer;

    // 退出倒计时（/exit，v0.2.2 第 3 批）：与 WPF 同样是"致谢 + 10 秒倒计时"。
    private DispatcherTimer? _exitTimer;
    private int _exitCountdown;

    // 运行指标的定时刷新（v0.2.2 第 3 批）：生成中每 500ms 问一次后端，不用前端估算。
    // WPF 版同一间隔、同一失败态处理（见 frontend/MainWindow.xaml.cs 的 StartMetricsTimer）。
    private DispatcherTimer? _metricsTimer;
    private bool _metricsStoppedState;

    /// <summary>首次运行引导是否已展示（进程内一次性，避免同一轮多次触发）。</summary>
    private bool _firstRunShown;

    /// <summary>待确认下载的模型名（第二次确认才真的下载，见 HandleConfirmModel）。</summary>
    private string _pendingModelDownload = "";

    /// <summary>本次生成的指标是否处于失败态（v0.2.2 第 4 批，对齐 WPF 的 _metricsState == "failed"）。</summary>
    private bool _metricsFailed;

    /// <summary>上一次已知的"模型就绪"状态（用来识别 false → true 的那一次跳变）。</summary>
    private bool? _modelWasReady;

    // [EN] Manual double-click bookkeeping for the custom caption. Platform click counters may be
    //      reset by the native move loop started by the first press, therefore the interval and
    //      distance are tracked explicitly so that maximise/restore stays deterministic.
    // [ZH] 自定义标题栏的手动双击判定状态。首次按下会启动平台移动循环，系统的点击计数可能因此复位，
    //      故此处显式记录时间间隔与位移，确保最大化/还原行为稳定可重复。
    private DateTime _lastCaptionPressAt = DateTime.MinValue;
    private Point _lastCaptionPressPoint;
    private DateTime _lastCaptionToggleAt = DateTime.MinValue;

    // 状态区
    private List<string> _capabilityWarnings = new();
    private string _updateUrl = "";
    private bool _updateAvailable;

    // Agent 步骤区（本轮迁移的功能面板之一）
    private Border? _agentRegion;
    private StackPanel? _agentRows;

    // 运行指标：由后端上报（done.stats / progress.stats），前端不估算
    private string _stripMetrics = "";

    // 本轮流式的运行指标（done.stats 里带 stopped 的那份）。
    // 它原先只是 StreamChatAsync 里的一个局部变量，改成字段是为了让事件分发
    // （DispatchStreamEvent）能独立成一个可被探针直接调用的方法 —— 见那里的说明。
    private string _liveMetrics = "";

    /// <summary>
    /// Agent 步骤区当前的行数（**探针用**：无人值守时无法"看"界面，
    /// 只能靠可读的数字断言"步骤行确实渲染了"）。没有步骤区时返回 0。
    /// </summary>
    public int AgentStepRowCount
        => _agentRegion is null ? 0 : EnumerateAgentRows(_agentRegion).Count();

    /// <summary>
    /// 探针/截图模式：为 true 时 Opened 不拉后端。
    /// 截图必须可复现，不能依赖本机是否装了模型、网络是否可达；真实运行路径不受影响。
    /// </summary>
    public bool ProbeSuppressBackendStart { get; set; }

    private sealed record Message(string Role, string Content, string Reasoning, string Metrics);

    /// <summary>一条消息的可更新视图（流式期间就地更新，避免整屏重排）。</summary>
    private sealed class MessageView
    {
        public required Border Root { get; init; }
        public required ContentControl ContentHost { get; init; }
        public required SelectableTextBlock ThinkingText { get; init; }
        public required Expander ThinkingBox { get; init; }
        public required TextBlock MetricsText { get; init; }
        public string Content { get; set; } = "";
        public string Reasoning { get; set; } = "";
        public string Metrics { get; set; } = "";
    }

    public MainWindow()
    {
        // 必须调生成的 InitializeComponent()，**不能**只调 AvaloniaXamlLoader.Load(this)。
        // 两个都在时以 InitializeComponent 为准：它除了 Load 还会把 x:Name 具名控件
        // 赋给字段（SettingsWindow 与 7 个设置页一直这么写，只有主窗口漏了）。
        // 只调 Load 的后果不是"少几个字段"，而是构造到下面第 N 行就 NullReferenceException，
        // **整个前端起不来**（实测：dotnet build 与 dotnet publish 的产物都如此）。
        InitializeComponent();
        _state.Initialize();

        Width = _state.Settings.WindowWidth;
        Height = _state.Settings.WindowHeight;
        // 位置与最大化：在设置宽高**之后**恢复（否则被构造时的默认位置覆盖）
        RestoreWindowPlacement();

        CommandPalette.PlacementTarget = CommandInputBox;
        SnippetPalette.PlacementTarget = SnippetButton;

        Loc.LanguageChanged += (_, _) => ApplyTexts();
        _state.AppearanceChanged += (_, _) =>
        {
            ApplyFontSize();
            RenderTranscript();
        };

        WireEvents();
        BuildLanguageCombo();
        ApplyTexts();
        ApplyFontSize();
        ApplyStatusStripState();
        ShowCapabilityBanner();

        Closing += OnClosing;
        // [EN] The caption glyph and accessible names must track state changes that do not pass
        //      through our own controls (for example Aero Snap or a native maximise gesture).
        // [ZH] 标题栏字形与无障碍名称必须跟随不经过自有控件的状态变化（例如系统贴靠或原生最大化手势）。
        PropertyChanged += OnWindowPropertyChanged;
        UpdateWindowChromeState();
        Opened += async (_, _) =>
        {
            // 先把窗口尺寸收进当前屏幕的可用区域，再开后端：
            // 记忆里的尺寸可能是旧版本留下的（或被拖到过更小的屏幕），
            // 超出屏幕时窗口底部的输入区/状态区会被裁掉，用户会以为"窗口太大、点不到"。
            ClampSizeToScreen();
            if (!ProbeSuppressBackendStart)
                await StartBackendAsync();
        };
    }

    // -----------------------------------------------------------------
    // [EN] Custom window chrome / [ZH] 自定义窗口外框
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Starts a platform move loop when the custom caption is pressed with the primary
    ///      button. A second click within the double-click interval toggles the maximised state.
    ///      Because the move loop is executed by the operating system, cursor tracking, screen
    ///      snapping and per-monitor DPI transitions are inherited instead of being reimplemented.
    /// [ZH] 自定义标题栏被主键按下时启动平台移动循环；在双击间隔内的第二次点击切换最大化状态。
    ///      由于移动循环由操作系统执行，光标跟随、屏幕贴靠与逐屏 DPI 切换均为系统原生行为，
    ///      而非在本层重新实现。
    /// </summary>
    private void OnTitleBarPointerPressed(object? sender, PointerPressedEventArgs e)
    {
        if (!e.GetCurrentPoint(this).Properties.IsLeftButtonPressed)
            return;
        // [EN] Caption controls own their input; the drag surface must not steal it.
        // [ZH] 窗口控制按钮自行处理输入，拖拽载面不得抢占。
        if (e.Source is Visual visual && visual.FindAncestorOfType<Button>(includeSelf: true) is not null)
            return;

        // [EN] Double-click detection is performed explicitly (interval + displacement) because
        //      the platform click counter is not reliable once the native move loop has started.
        // [ZH] 双击判定在此显式完成（时间间隔 + 位移），因为平台移动循环启动后系统点击计数不再可靠。
        DateTime now = DateTime.UtcNow;
        Point point = e.GetPosition(this);
        bool doubleByInterval = (now - _lastCaptionPressAt).TotalMilliseconds <= 400
            && Math.Abs(point.X - _lastCaptionPressPoint.X) <= 4
            && Math.Abs(point.Y - _lastCaptionPressPoint.Y) <= 4;
        _lastCaptionPressAt = now;
        _lastCaptionPressPoint = point;

        if (doubleByInterval || e.ClickCount == 2)
        {
            // [EN] A single gesture must not toggle twice, even when both detection paths fire.
            // [ZH] 同一次手势不得触发两次切换，即使两条判定路径同时命中。
            if ((now - _lastCaptionToggleAt).TotalMilliseconds > 200)
            {
                _lastCaptionToggleAt = now;
                _lastCaptionPressAt = DateTime.MinValue;
                ToggleMaximize();
            }
            e.Handled = true;
            return;
        }

        BeginMoveDrag(e);
        e.Handled = true;
    }

    /// <summary>
    /// [EN] Toggles the window between the maximised and restored states.
    /// [ZH] 在最大化与还原状态之间切换窗口。
    /// </summary>
    private void ToggleMaximize()
    {
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
        UpdateWindowChromeState();
    }

    /// <summary>
    /// [EN] Applies the current window state to the custom caption: the restore glyph replaces
    ///      the maximise glyph when maximised, and both the tooltip and the accessible name are
    ///      rebuilt in the active interface language.
    /// [ZH] 将当前窗口状态应用到自定义标题栏：最大化时以还原字形替换最大化字形，并按当前界面语言
    ///      重建悬停提示与无障碍名称。
    /// </summary>
    private void UpdateWindowChromeState()
    {
        bool maximized = WindowState == WindowState.Maximized;
        MaximizeGlyph.IsVisible = !maximized;
        RestoreGlyph.IsVisible = maximized;
        TitleBarTitle.Text = Title;

        string minimize = Loc.T("main.window.minimize", "最小化");
        string maximize = maximized
            ? Loc.T("main.window.restore", "还原")
            : Loc.T("main.window.maximize", "最大化");
        string close = Loc.T("main.window.close", "关闭");

        ToolTip.SetTip(TitleBarMinimizeButton, minimize);
        ToolTip.SetTip(TitleBarMaximizeButton, maximize);
        ToolTip.SetTip(TitleBarCloseButton, close);
        AutomationProperties.SetName(TitleBarMinimizeButton, minimize);
        AutomationProperties.SetName(TitleBarMaximizeButton, maximize);
        AutomationProperties.SetName(TitleBarCloseButton, close);
    }

    /// <summary>
    /// [EN] Routes window-state property changes to the caption updater.
    /// [ZH] 将窗口状态属性变更路由至标题栏更新逻辑。
    /// </summary>
    private void OnWindowPropertyChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property == WindowStateProperty)
            UpdateWindowChromeState();
    }

    /// <summary>把窗口尺寸限制在当前屏幕的可用区域内（留 24px 余量，保证标题栏与底部都能抓到）。</summary>
    private void ClampSizeToScreen()
    {
        global::Avalonia.Platform.Screen? screen =
            Screens.ScreenFromWindow(this) ?? Screens.Primary;
        if (screen is null)
            return;
        double scale = screen.Scaling > 0 ? screen.Scaling : 1.0;
        // 注意：Avalonia 的 Width/Height 是**客户区**，窗口边框与标题栏还要再加约 16x39。
        // 只按可用区域减 24 的话，客户区虽然"装得下"，整窗反而会高出屏幕一截。
        double maxWidth = screen.WorkingArea.Width / scale - 48;
        double maxHeight = screen.WorkingArea.Height / scale - 56;
        if (maxWidth > 0 && Width > maxWidth)
            Width = Math.Max(MinWidth, maxWidth);
        if (maxHeight > 0 && Height > maxHeight)
            Height = Math.Max(MinHeight, maxHeight);
    }

    // -----------------------------------------------------------------
    // 初始化与文案
    // -----------------------------------------------------------------

    private void WireEvents()
    {
        // [EN] Custom caption controls: minimise, maximise-or-restore and close. The pointer
        //      driven move loop is attached in XAML (OnTitleBarPointerPressed).
        // [ZH] 自定义标题栏控件：最小化、最大化/还原与关闭。指针驱动的移动循环在 XAML 中挂接
        //      （OnTitleBarPointerPressed）。
        TitleBarMinimizeButton.Click += (_, _) => WindowState = WindowState.Minimized;
        TitleBarMaximizeButton.Click += (_, _) => ToggleMaximize();
        TitleBarCloseButton.Click += (_, _) => Close();

        SendButton.Click += async (_, _) => await SendAsync();
        StopButton.Click += async (_, _) => await StopAsync();
        UploadButton.Click += async (_, _) => await PickFilesAsync();
        SettingsButton.Click += async (_, _) => await OpenSettingsAsync("settings.tab.general");
        ModelChip.Click += async (_, _) => await OpenSettingsAsync("settings.tab.model");
        ModeChip.Click += async (_, _) => await OpenSettingsAsync("settings.tab.general");
        FontDownButton.Click += (_, _) => _state.SetFontSize(_state.Settings.FontSize - 1);
        FontUpButton.Click += (_, _) => _state.SetFontSize(_state.Settings.FontSize + 1);
        ThemeButton.Click += (_, _) => CycleTheme();
        LangCombo.SelectionChanged += (_, _) =>
        {
            if (LangCombo.SelectedItem is LangItem item)
                _state.SetLanguage(item.Tag);
        };

        // 输入区
        InputBox.KeyDown += OnInputKeyDown;
        InputBox.TextChanged += (_, _) => UpdateCommandUi(InputBox.Text ?? "");
        CommandInputBox.KeyDown += OnCommandInputKeyDown;
        CommandInputBox.TextChanged += (_, _) => UpdateCommandPalette(CommandInputBox);
        CommandButton.Click += (_, _) => ToggleCommandRow();
        SnippetButton.Click += (_, _) => ToggleSnippetPalette();

        // 指令面板 / 常用语面板的鼠标选择
        CommandList.PointerReleased += (_, _) =>
        {
            if (CommandList.SelectedItem is CommandItem item)
                ExecuteCommandItem(item);
        };
        SnippetList.PointerReleased += (_, _) =>
        {
            if (SnippetList.SelectedItem is Snippet snippet)
                InsertSnippet(snippet);
        };

        // 状态区
        StripSearchButton.Click += async (_, _) => await ToggleSearchAsync();
        StripAutomationButton.Click += async (_, _) => await OpenSettingsAsync("settings.tab.automation");
        StripMarketButton.Click += async (_, _) => await OpenSettingsAsync("settings.tab.market");
        StripContextButton.Click += async (_, _) => await RefreshContextAsync();
        StripKvButton.Click += async (_, _) => await OpenSettingsAsync("settings.tab.model");
        StripCapabilityButton.Click += async (_, _) => await OpenSettingsAsync("settings.tab.model");
        StripCompactButton.Click += async (_, _) => await CompactContextAsync();
        StripWarnButton.Click += (_, _) => ShowWarnings();
        StripUpdateButton.Click += (_, _) => OpenUpdatePage();
        StripToggleButton.Click += (_, _) =>
        {
            _state.Settings.StatusStripCollapsed = !_state.Settings.StatusStripCollapsed;
            _state.Settings.Save();
            ApplyStatusStripState();
        };

        AddHandler(DragDrop.DragOverEvent, OnDragOver);
        AddHandler(DragDrop.DropEvent, OnDrop);

        // ---- v0.2.2 第 3 批：窗口级快捷键与滚轮 ----
        // 用 **Tunnel** 路由：这是 Avalonia 里 WPF `OnPreviewKeyDown` 的对应物 ——
        // 必须比输入框先看到按键，否则 Ctrl+L / Ctrl+M 会被输入框当普通字符吃掉，
        // Esc 也永远轮不到"关面板 / 停生成"。
        AddHandler(KeyDownEvent, OnWindowKeyDown, RoutingStrategies.Tunnel);
        // 消息区 Ctrl+滚轮调字号（与 WPF 的 MessageScroll_PreviewMouseWheel 同一行为）
        MessageScroll.PointerWheelChanged += OnMessageWheel;
        // 输入框获得焦点时关掉全部面板（WPF 的 InputBox_GotFocus 同一行为）：
        // 面板浮在输入框上方，用户点了输入框还留着面板会挡住刚输入的内容。
        InputBox.GotFocus += (_, _) => CloseAllPalettes();

        // 上下文胶囊的点击处理器**只能注册一次**。它原先挂在 ApplyTexts() 里 ——
        // 而 ApplyTexts 每次切语言都会跑一遍，于是每切一次语言就多一个处理器
        // （点一下发 N 次 context 请求）。
        ContextChip.Click += async (_, _) => await RefreshContextAsync();
    }

    /// <summary>
    /// 窗口级快捷键（对齐 WPF 的 <c>OnPreviewKeyDown</c>）：
    /// Ctrl+Enter 发送 · Esc 停止/关面板 · Ctrl+L 清空 · Ctrl+O 上传 · Ctrl+T 深浅色 ·
    /// Ctrl+, 设置 · Ctrl+M 设置(技能市场) · Ctrl+Shift+C 复制全部对话 ·
    /// Ctrl+加/减/0 调字号。
    /// </summary>
    private void OnWindowKeyDown(object? sender, KeyEventArgs e)
    {
        bool ctrl = e.KeyModifiers == KeyModifiers.Control;
        bool ctrlShift = e.KeyModifiers == (KeyModifiers.Control | KeyModifiers.Shift);

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
                    _ = SendCommandAsync("/clear");
                    return;
                case Key.O:
                    e.Handled = true;
                    _ = PickFilesAsync();
                    return;
                case Key.T:
                    e.Handled = true;
                    CycleTheme();
                    return;
                case Key.OemComma:
                    e.Handled = true;
                    _ = OpenSettingsAsync("settings.tab.general");
                    return;
                case Key.M:
                    e.Handled = true;
                    _ = OpenSettingsAsync("settings.tab.market");
                    return;
                case Key.OemPlus:
                case Key.Add:
                    e.Handled = true;
                    _state.SetFontSize(_state.Settings.FontSize + 1);
                    SetStatus(Loc.T("main.status.fontSize", "字号 {0}", _state.Settings.FontSize));
                    return;
                case Key.OemMinus:
                case Key.Subtract:
                    e.Handled = true;
                    _state.SetFontSize(_state.Settings.FontSize - 1);
                    SetStatus(Loc.T("main.status.fontSize", "字号 {0}", _state.Settings.FontSize));
                    return;
                case Key.D0:
                case Key.NumPad0:
                    e.Handled = true;
                    _state.SetFontSize(DefaultFontSize);
                    SetStatus(Loc.T("main.status.fontSize", "字号 {0}", _state.Settings.FontSize));
                    return;
            }
        }
        if (e.Key == Key.Escape)
        {
            // 优先级与 WPF 一致：先关面板，再停生成。**不再无条件 StopAsync**——
            // 原先 Avalonia 侧只要焦点在输入框就停生成，用户按 Esc 取消补全会误停。
            if (CommandPalette.IsOpen || SnippetPalette.IsOpen)
            {
                CloseAllPalettes();
                e.Handled = true;
                return;
            }
            if (_busy)
            {
                e.Handled = true;
                _ = StopAsync();
            }
        }
    }

    /// <summary>Ctrl+滚轮调字号（WPF 的 MessageScroll_PreviewMouseWheel 同一行为）。</summary>
    private void OnMessageWheel(object? sender, PointerWheelEventArgs e)
    {
        if (e.KeyModifiers != KeyModifiers.Control)
            return;
        e.Handled = true;
        _state.SetFontSize(_state.Settings.FontSize + (e.Delta.Y > 0 ? 1 : -1));
        SetStatus(Loc.T("main.status.fontSize", "字号 {0}", _state.Settings.FontSize));
    }

    /// <summary>Ctrl+T：主题三态循环（与标题栏那个 🌓 按钮同一实现）。</summary>
    private void CycleTheme()
    {
        string next = _state.Settings.Theme switch
        {
            "auto" => "light",
            "light" => "dark",
            _ => "auto",
        };
        _state.SetTheme(next);
        ApplyTexts();
    }

    private void OnInputKeyDown(object? sender, KeyEventArgs e)
    {
        // 指令面板打开时优先吃掉方向键/Enter/Esc/Tab（与 WPF 版同一优先级）
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        // Alt+↑（任务十二）：把上一条**用户**消息复制回输入框。
        // 判据放在最前，且要求"确实是 Alt 而不是 Ctrl/Shift"——否则会和未来
        // 可能加进来的其它方向键快捷键互相偷键。
        if (e.Key == Key.Up && e.KeyModifiers == KeyModifiers.Alt)
        {
            e.Handled = true;
            RecallLastUserMessage();
            return;
        }
        if (e.Key == Key.Enter && e.KeyModifiers.HasFlag(KeyModifiers.Control))
        {
            e.Handled = true;
            _ = SendAsync();
        }
        else if (e.Key == Key.Escape)
        {
            e.Handled = true;
            _ = StopAsync();
        }
    }

    /// <summary>
    /// 任务十二：把上一条用户消息复制回输入框（Alt+↑）。
    ///
    /// 三个刻意的取舍（记录 §8.4 要求"实现前先确认与既有快捷键不冲突"）：
    ///   * **只找 role == "user"**：复制助手回复没有意义，而"再问一次".
    ///     是用户真正会反复做的事；
    ///   * **不覆盖已有输入**是错的还是对的？这里选择**覆盖**并在状态栏提示，
    ///     与"上一条"的语义一致；用户按 Ctrl+Z 或再打一遍即可恢复，
    ///     而"什么都不做"会让人以为快捷键没生效；
    ///   * 输入框里已有的内容**不做确认弹窗**：这毕竟是可撤销的编辑动作，
    ///     弹窗会让连续追问变得很烦。用状态栏一句话说明"已复制上一条"。
    /// </summary>
    private void RecallLastUserMessage()
    {
        for (int i = _transcript.Count - 1; i >= 0; i--)
        {
            if (!string.Equals(_transcript[i].Role, "user", StringComparison.Ordinal))
                continue;
            string text = _transcript[i].Content;
            if (text.Length == 0)
                continue;
            InputBox.Text = text;
            InputBox.CaretIndex = text.Length;
            InputBox.Focus();
            SetStatus(Loc.T("main.recall.lastUser", "↩️ 已复制上一条提问（Alt+↑）"));
            return;
        }
        SetStatus(Loc.T("main.recall.none", "还没有可以复制的提问"));
    }

    private void OnCommandInputKeyDown(object? sender, KeyEventArgs e)
    {
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            string text = (CommandInputBox.Text ?? "").Trim();
            CommandInputBox.Text = "";
            CloseAllPalettes();
            if (text.Length > 0)
                _ = SendCommandAsync(text);
        }
        else if (e.Key == Key.Escape)
        {
            e.Handled = true;
            CloseAllPalettes();
            CommandInputRow.IsVisible = false;
            _commandRowOpen = false;
            UpdateCommandButtonText();
        }
    }

    private sealed record LangItem(string Tag, string Label)
    {
        public override string ToString() => Label;
    }

    private void BuildLanguageCombo()
    {
        var items = new List<LangItem> { new("auto", Loc.T("settings.general.languageAuto", "自动（跟随系统区域）")) };
        foreach (string locale in Loc.AvailableLocales())
            items.Add(new LangItem(locale, Loc.DisplayName(locale)));
        LangCombo.ItemsSource = items;
        string current = _state.Settings.Language;
        LangCombo.SelectedItem = items.FirstOrDefault(x => string.Equals(x.Tag, current, StringComparison.OrdinalIgnoreCase))
                                 ?? items[0];
    }

    /// <summary>把界面文案刷成当前语言（Avalonia 版用直接赋值，不用 WPF 的附加属性遍历）。</summary>
    private void ApplyTexts()
    {
        Title = $"EchoDesk {_state.Backend.Version ?? "v0.2.1"}";
        VersionText.Text = _state.Backend.Version ?? "v0.2.1";
        BrandText.Text = Loc.T("main.brand", "📡 EchoDesk");
        PoweredText.Text = Loc.T("main.powered", "由熵寂工作室出品");
        PlatformChip.Text = $"🖥 {_state.Platform.DisplayName}";
        // 模型胶囊的文案口径必须与 WPF 完全一致（同一份语言包、同一批键）：
        //   未就绪 → main.model.noModel（"🧠 模型: 无模型"）
        //   已就绪 → main.model.chip（"🧠 模型: {0}"）
        // 此前这里用的是 main.model.chipUnknown，它在语言包里只有 "🧠"，
        // 于是 Avalonia 的胶囊只剩一个脑子图标、WPF 却是整句话 —— 截图上一眼就能看出来。
        ModelText.Text = _state.Backend.ModelReady
            ? Loc.T("main.model.chip", "🧠 模型: {0}", string.IsNullOrEmpty(_state.Backend.LoadedModel) ? "local" : _state.Backend.LoadedModel)
            : Loc.T("main.model.noModel", "🧠 模型: 无模型");
        // 状态栏的"未知/未连接"初值也必须走语言包（v0.2.2 UI 英文扫描）：
        // XAML 里原先写死中文，后端没连上时英文界面会整条露出中文；
        // 这里给全套初值，随后的状态事件会把它们覆盖成实时值。
        ContextChipText.Text = Loc.T("main.context.buttonIdle", "上下文：---/---");
        ModeText.Text = Loc.T("main.mode.chip", "🛠️ 模式: {0}", "general");
        SearchText.Text = Loc.T("main.strip.search", "🔍 搜索：{0}", Loc.T("main.strip.off", "关"));
        AutoText.Text = Loc.T("main.strip.automation", "⚙️ 自动化：{0}", Loc.T("main.strip.off", "关"));
        MarketText.Text = Loc.T("main.strip.market", "🌐 市场：{0}",
            Loc.T("main.strip.marketUnconnected", "未连接"));
        StripContextText.Text = Loc.T("main.strip.contextStrip", "📊 Context {0}%", 0);
        StripCapabilityText.Text = Loc.T("main.strip.capability", "🧠 {0}", "---");
        StripCompactText.Text = Loc.T("main.strip.compact", "🗜 压缩上下文");
        // 视觉对齐：WPF 版的发送/上传是**纯图标圆形按钮**（➤ / 📎），不是带文字的方块按钮。
        // 所以这里不再往 Content 里写文案；含义靠 ToolTip 与无障碍 HelpText 传达，
        // 与 WPF 完全一致（main.button.sendTip / uploadTip）。
        StopButton.Content = "⏹";
        HintText.Text = Loc.T("main.hint", "💡 拖拽文件可上传 · Ctrl+Enter 发送 · Esc 停止 · A-/A+ 或 Ctrl+滚轮 调字号");
        // 指令输入框的提示文字走 TextBox 自带的水印：见 MainWindow.axaml 里 CommandInputRow 的注释
        // （手工叠 TextBlock 的办法既对不准、也不会自己隐藏）。
        CommandInputBox.PlaceholderText = Loc.T("main.command.placeholder", "在此处输入以“/”开头的指令 · 了解用法输入 /help");
        SnippetHint.Text = Loc.T("main.snippet.hint", "/snippet add 名称 文本 可新增 · 空列表时先添加");
        PaletteHint.Text = Loc.T("main.paletteHint",
            "↑↓ 选择 · Tab 补全 · Enter 执行（候选不唯一时先补全命令名再确认）· Esc 关闭");
        CommandBarHint.Text = Loc.T("main.command.hint", "💡 检测到指令格式：点击左下角「打开命令输入框」按钮");
        ToolTip.SetTip(CommandButton, Loc.T("main.command.tip", "打开/关闭命令输入框"));
        ToolTip.SetTip(SnippetButton, Loc.T("main.snippet.tip", "打开常用语选择框"));
        ToolTip.SetTip(SettingsButton, Loc.T("main.button.settingsTip", "打开设置"));
        ToolTip.SetTip(UploadButton, Loc.T("main.button.uploadTip", "上传文件加入上下文"));
        ToolTip.SetTip(SendButton, Loc.T("main.button.sendTip", "发送（Ctrl+Enter）"));
        ToolTip.SetTip(ModelChip, Loc.T("main.model.clickTip", "点击打开「设置 → 模型」"));
        ToolTip.SetTip(ModeChip, Loc.T("main.mode.clickTip", "点击打开「设置 → 常规」"));
        ToolTip.SetTip(StripSearchButton, Loc.T("main.strip.searchTip", "点击切换联网搜索"));
        ToolTip.SetTip(StripAutomationButton, Loc.T("main.strip.automationTip", "点击打开「设置 → 自动化」"));
        ToolTip.SetTip(StripMarketButton, Loc.T("main.strip.marketTip", "点击打开「设置 → 技能市场」"));
        ToolTip.SetTip(StripContextButton, Loc.T("main.strip.contextTip", "点击刷新上下文占用"));
        ToolTip.SetTip(StripKvButton, Loc.T("main.strip.kvTip", "点击打开「设置 → 模型」"));
        ToolTip.SetTip(StripCapabilityButton, Loc.T("main.strip.capabilityTip", "点击打开「设置 → 模型」"));
        ToolTip.SetTip(StripCompactButton, Loc.T("main.strip.compactTip", "立刻压缩上下文，腾出空间"));
        // 包值是「有 {0} 条能力提示，点击查看详情」——必须把条数传进去，否则 Loc.T 会返回
        // 未格式化模板、悬停提示直接露出 {0}（硬编码清查 §5.3 C-1）。无告警时该按钮隐藏
        //（见 :1102 的 IsVisible），所以「0 条」这一档不会真的被看到。
        ToolTip.SetTip(StripWarnButton, Loc.T("main.strip.warnTip",
            "有 {0} 条能力提示，点击查看详情", _capabilityWarnings.Count));
        // 标题栏四个按钮的悬停说明：此前只写在 MainWindow.axaml 的 ToolTip.Tip 里、
        // 代码里**没有任何覆盖点** → 英文界面悬停必现中文（硬编码清查 §5.1）
        ToolTip.SetTip(FontDownButton, Loc.T("main.button.fontDownTip", "缩小字号"));
        ToolTip.SetTip(FontUpButton, Loc.T("main.button.fontUpTip", "放大字号"));
        ToolTip.SetTip(ThemeButton, Loc.T("main.button.themeTip", "切换深色/浅色主题"));
        ToolTip.SetTip(LangCombo, Loc.T("main.button.langTip", "界面语言"));
        ThemeButton.Content = _state.Settings.Theme switch
        {
            "light" => "☀",
            "dark" => "🌙",
            _ => "🌓",
        };
        // 状态区里与语言相关的固定文案（数值部分由后端数据驱动，不在这里覆盖）
        if (!_updateAvailable)
            StripUpdateText.Text = Loc.T("main.update.idle", "🎉 新版本");
        StripWarnText.Text = _capabilityWarnings.Count > 0
            ? Loc.T("main.strip.warn", "⚠️ 能力提示 {0}", _capabilityWarnings.Count)
            : Loc.T("main.strip.warnIdle", "⚠️ 能力提示");
        UpdateCommandButtonText();
        UpdateSnippetButtonText();
        UpdateCommandUi(InputBox.Text ?? "");
        ApplyStatusStripState();
        if (string.IsNullOrEmpty(ContextChipText.Text))
            ContextChipText.Text = Loc.T("main.context.buttonIdle", "上下文：---/---");
        ShowCapabilityBanner();
        ApplyHelpText();
        // [EN] Caption text is language-dependent, therefore it is refreshed with the rest of
        //      the interface strings. / [ZH] 标题栏文案依赖界面语言，因此随其余界面文案一并刷新。
        UpdateWindowChromeState();
    }

    /// <summary>
    /// 屏幕阅读器说明（WPF 用 <c>svc:Loc.HelpKey</c> 附加属性，Avalonia 侧直接
    /// <see cref="AutomationProperties.SetHelpText"/>）。
    ///
    /// 为什么用 HelpText 而**不是** Name：Name 会覆盖控件真实可读的文本，
    /// 屏幕阅读器就再也读不到"已就绪 / 模型名"这些动态值了
    /// （v0.2.0 排查时踩过，WPF 侧注释也写了这一条）。HelpText 是**补充说明**，两者共存。
    /// </summary>
    private void ApplyHelpText()
    {
        AutomationProperties.SetHelpText(StatusText, Loc.T("main.status.help",
            "运行状态：正在启动后端 / 已就绪 / 处理中 / 后端已退出"));
        AutomationProperties.SetHelpText(ModelChip, Loc.T("main.model.help", "当前模型与切换入口"));
        AutomationProperties.SetHelpText(ModeChip, Loc.T("main.mode.help", "当前对话模式与切换入口"));
        AutomationProperties.SetHelpText(StatusStrip, Loc.T("main.strip.help", "轻量状态区：各项可点击"));
        AutomationProperties.SetHelpText(StripSearchButton, Loc.T("main.strip.searchHelp", "联网搜索开关"));
        AutomationProperties.SetHelpText(StripAutomationButton, Loc.T("main.strip.autoHelp", "自动化状态"));
        AutomationProperties.SetHelpText(StripMarketButton, Loc.T("main.strip.marketHelp", "技能市场连接状态"));
        AutomationProperties.SetHelpText(StripContextButton, Loc.T("main.strip.contextHelp", "上下文占用比例"));
        AutomationProperties.SetHelpText(StripKvButton, Loc.T("main.strip.kvHelp", "KV 缓存占用"));
        AutomationProperties.SetHelpText(StripCapabilityButton, Loc.T("main.strip.capabilityHelp", "模型能力信息"));
        AutomationProperties.SetHelpText(StripCompactButton, Loc.T("main.strip.compactHelp", "立刻压缩上下文"));
    }

    private void ApplyFontSize()
    {
        InputBox.FontSize = _state.Settings.FontSize + 1;
        CommandInputBox.FontSize = _state.Settings.FontSize;
        // 消息区字号：逐条重建（Avalonia 没有 FlowDocument 那种"整篇一个字号"的容器）
        RenderTranscript();
        // Agent 步骤区与思考区同样跟随（Ctrl+滚轮调字号时才不会落下它们）
        ScaleAgentTrace();
    }

    /// <summary>
    /// 平台能力横幅（任务五的降级文案）：**只在能力缺失时显示**，
    /// 并且必须给出"为什么没有 + 替代路径"，而不是一句"不支持"。
    /// </summary>
    private void ShowCapabilityBanner()
    {
        PlatformInfo platform = _state.Platform;
        var missing = platform.Capabilities().Where(c => !c.Available).ToList();
        if (missing.Count == 0)
        {
            PlatformBanner.IsVisible = false;
            return;
        }
        PlatformBanner.IsVisible = true;
        PlatformBannerText.Text = Loc.T("main.platform.degraded", "⚠️ 当前平台（{0}）有 {1} 项能力不可用", platform.DisplayName, missing.Count);
        PlatformBannerDetail.Text = string.Join("；", missing.Select(m => $"{m.Capability}：{m.Note}"));
    }

    private async Task ShowCapabilityAsync()
    {
        var lines = new List<string>
        {
            Loc.T("main.capability.title", "本平台能力清单（{0}）", _state.Platform.Summary()),
            "",
        };
        foreach ((string capability, bool available, string note) in _state.Platform.Capabilities())
            lines.Add($"{(available ? "✅" : "❌")} {capability} —— {note}");

        // 后端的能力画像（/capability）也要能看到：前后端各有一套能力判断，用户需要对照
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "capability" }, idleTimeout: TimeSpan.FromSeconds(90));
        if (doc is not null)
        {
            using (doc)
            {
                if (doc.RootElement.TryGetProperty("capability", out JsonElement cap))
                {
                    lines.Add("");
                    lines.Add(Loc.T("main.capability.backend", "后端能力画像："));
                    foreach (JsonProperty property in cap.EnumerateObject())
                    {
                        string value = property.Value.ValueKind switch
                        {
                            JsonValueKind.True => "✅",
                            JsonValueKind.False => "❌",
                            JsonValueKind.String => property.Value.GetString() ?? "",
                            JsonValueKind.Number => property.Value.ToString(),
                            JsonValueKind.Array => $"{property.Value.GetArrayLength()} 项",
                            _ => property.Value.ValueKind.ToString(),
                        };
                        lines.Add($"  · {property.Name}: {value}");
                    }
                }
            }
        }
        AddMessage("system", string.Join("\n", lines));
    }

    // -----------------------------------------------------------------
    // 后端启动
    // -----------------------------------------------------------------

    private async Task StartBackendAsync()
    {
        SetStatus(Loc.T("main.connecting", "⏳ 正在连接后端，请稍候..."));
        StartResult started = await _state.Backend.StartAsync();
        if (!started.Ok)
        {
            // 后端缺失/启动失败**不是崩溃**：把命令与原因摆出来，界面继续可用（任务五）
            AddMessage("system", Loc.T("main.error.backendUnavailable", "❌ 后端不可用：{0}", started.Error));
            SetStatus(Loc.T("main.status.backendMissing", "后端未启动"));
            PlatformBanner.IsVisible = true;
            PlatformBannerText.Text = Loc.T("main.platform.backendMissing", "⚠️ 后端未启动");
            PlatformBannerDetail.Text = started.Error;
            return;
        }

        _state.Backend.StderrReceived += line => Dispatcher.UIThread.Post(() => SetStatus(Trim(line, 120)));
        // v0.2.2 第 4 批（总策划定向放开的那一处协议扩展）：宿主的**结构化**进度事件
        // 渲染成"活消息"——同一条系统气泡就地更新，而不是像 stderr 那样只在状态栏闪一下。
        // 这修的正是差异清单 §F5 第 3 条："后端 stderr 那一行仍只写状态栏"。
        _state.Backend.HostProgressReceived += text => Dispatcher.UIThread.Post(() =>
        {
            if (_progressView is null)
            {
                _transcript.Add(new Message("system", Emoji.Prefer(text), "", ""));
                _progressView = AddMessageView("system", "", "", "");
                _progressView.ContentHost.Content = BuildPlainText("system", text);
            }
            else
            {
                UpdateProgressMessage(text);
            }
            SetStatus(Trim(text, 120));
        });
        _state.Backend.ProcessExited += code => Dispatcher.UIThread.Post(() =>
        {
            AddMessage("system", Loc.T("main.error.backendCrashed", "⚠️ 后端进程意外退出（退出码 {0}）。", code));
            SetStatus(Loc.T("main.status.backendExited", "后端已退出"));
        });
        _state.Backend.ModelReadyChanged += ready => Dispatcher.UIThread.Post(() =>
        {
            ApplyTexts();
            SetStatus(ready
                ? Loc.T("main.status.ready", "就绪")
                : Loc.T("main.status.noModel", "后端已就绪，但没有加载模型"));
            // 模型热加载就绪通知（v0.2.2 第 3 批）：用户在设置页点「加载」后回到主窗口，
            // 如果只有状态行悄悄从"无模型"变成"就绪"，很容易错过。
            // 系统消息 + 通知条两处同时说一句（与 WPF 的处置一致）。
            if (ready && _modelWasReady != true)
            {
                string text = Loc.T("main.model.hotLoaded", "✅ 模型已就绪，现在可以正常对话了。");
                AddMessage("system", text);
                Toast(text);
            }
            _modelWasReady = ready;
        });

        JsonDocument? ready = await _state.Backend.WaitForReadyAsync(TimeSpan.FromMinutes(10));
        if (ready is null)
        {
            AddMessage("system", Loc.T("main.error.notReady", "❌ 后端没有就绪：{0}", _state.Backend.LastError ?? ""));
            SetStatus(Loc.T("main.status.notReady", "未就绪"));
            return;
        }
        using (ready)
        {
            ApplyTexts();
            RestoreHistory();
            // 首次运行引导与启动快捷方式检测（v0.2.2 第 3 批）：
            // 放在"后端就绪之后"——引导里提到的模型市场是后端能力，先说明再让用户去点。
            ShowFirstRunGuideIfNeeded();
            CheckStartupShortcut();

            // 平台一致性：前端按本机判断，后端也上报自己的判断，不一致就是 bug（记录里叫"平台判断漂移"）
            string backendKind = _state.Backend.BackendPlatformKind;
            if (backendKind.Length > 0 && backendKind != _state.Platform.KindName)
            {
                AddMessage("system", Loc.T("main.platform.mismatch",
                    "⚠️ 前后端平台判断不一致：前端 {0} / 后端 {1}。降级行为可能不符合预期。",
                    _state.Platform.KindName, backendKind));
            }
            if (!_state.Backend.ModelReady && !string.IsNullOrWhiteSpace(_state.Backend.ModelGuide))
            {
                // 视觉对齐：WPF 把这段"没有模型怎么办"的说明作为**一条系统消息**放进对话区
                // （MainWindow.xaml.cs 的 AddMessage("system", guide)），而不是窗口顶部的横幅。
                // 横幅会把标题行整体往下推，截图里与 WPF 的版面完全对不上 —— 改成同一条路径。
                AddMessage("system", _state.Backend.ModelGuide);
                GuideBanner.IsVisible = false;
            }
        }
        SetStatus(_state.Backend.ModelReady
            ? Loc.T("main.status.ready", "就绪")
            : Loc.T("main.status.noModel", "后端已就绪，但没有加载模型"));

        // 就绪后把状态区能拉的全拉一遍：与 WPF 版同一批动作
        await LoadCommandsAsync();
        await RefreshStatusAsync();
        await RefreshContextAsync();
        await RefreshAutomationStatusAsync();
        await RefreshMarketStatusAsync();
        await CheckUpdateAsync();
    }

    private void RestoreHistory()
    {
        foreach ((string role, string content, string reasoning) in _state.Backend.RestoredHistory)
        {
            if (string.IsNullOrEmpty(content) && string.IsNullOrEmpty(reasoning))
                continue;
            AddMessage(role, content, reasoning);
        }
        // 「已自动恢复上次会话（N 轮）」+ 欢迎语（v0.2.2 第 3 批）：
        // 用户重启程序后最想知道的就是"我上次的对话还在不在"，一句明确的说明比
        // 让他自己数气泡靠谱（WPF 版同一处理，`main.welcomeRestored` / `main.welcome`）。
        int restored = _state.Backend.RestoredCount;
        AddMessage("system", restored > 0
            ? Loc.T("main.welcomeRestored", "📂 已自动恢复上次会话（{0} 轮）", restored)
            : Loc.T("main.welcome", "✨ 欢迎使用 EchoDesk {0} — 由{1}出品",
                _state.Backend.Version ?? "v0.2.2",
                Loc.T("main.studioName", "熵寂工作室")));
    }

    /// <summary>
    /// 首次运行引导（对齐 WPF 的 `ShowFirstRunGuideIfNeeded`）。
    /// 只展示一次，看过就写 `FirstRunDone` —— 每次都弹等于没有引导。
    /// </summary>
    private void ShowFirstRunGuideIfNeeded()
    {
        if (_state.Settings.FirstRunDone || _firstRunShown)
            return;
        _firstRunShown = true;
        _state.Settings.FirstRunDone = true;
        _state.Settings.Save();

        AddMessage("system", string.Join(Environment.NewLine, new[]
        {
            Loc.T("main.firstRun.title", "👋 第一次使用 EchoDesk，三步开始："),
            Loc.T("main.firstRun.step1", "1️⃣ 上传文件：点「📎 上传文件」或直接把文件拖进窗口"),
            Loc.T("main.firstRun.step2", "2️⃣ 提问：把要提取/审查/汇总的内容直接写出来"),
            Loc.T("main.firstRun.step3", "3️⃣ 没有模型时：到「设置 → 模型」的模型市场里下载一个"),
            Loc.T("main.firstRun.keys",
                "⌨️ 快捷键：Ctrl+Enter 发送 · Esc 停止 · Ctrl+L 清空 · Ctrl+O 上传 · Ctrl+Shift+C 复制全部 · Ctrl+滚轮 调字号"),
        }));
    }

    /// <summary>
    /// 启动快捷方式检测（对齐 WPF 的 `CheckStartupShortcut`）。
    ///
    /// **总策划本轮裁定：自动创建**（原来是"只检测与说明，不自动创建"）。
    /// WPF 的判据是"已存在且有效 → 什么都不做；存在但失效 → 提示重建；
    /// 不存在且用户没拒绝过 → 建一个"。跨平台版按同一判据，只是产物换成各平台的启动器
    /// （`.cmd` / `.command` / `.desktop`，见 Services/ShortcutService.cs 的说明）。
    ///
    /// 用户拒绝过一次（`ShortcutPromptDeclined`）就不再建 —— 这条与 WPF 一致，
    /// 也是"自动创建"不至于变成"每次启动都往桌面上写文件"的唯一保障。
    /// </summary>
    private void CheckStartupShortcut()
    {
        try
        {
            if (ShortcutService.IsValid())
                return;

            if (ShortcutService.Exists())
            {
                // 存在但失效（程序目录被改名/搬家）：直接重建，并说明原因
                string path = ShortcutService.Create();
                AddMessage("system", Loc.T("main.shortcut.recreated",
                    "🔧 检测到启动快捷方式已失效（通常是因为程序文件夹被移动或改名），已重新创建：\n{0}",
                    path));
                return;
            }

            if (_state.Settings.ShortcutPromptDeclined)
                return;

            string created = ShortcutService.Create();
            AddMessage("system", Loc.T("main.shortcut.created",
                "✅ 已创建启动快捷方式：{0}", created));
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.shortcut.failed", "⚠️ 快捷方式处理失败: {0}", ex.Message));
        }
    }

    /// <summary>
    /// 窗口位置记忆 + 可见性校验（v0.2.2 第 3 批）。
    ///
    /// **可见性校验不能省**：用户把窗口拖到第二块屏、之后拔掉那块屏，
    /// 下次启动按记忆恢复就会把窗口放到**屏幕外** —— 界面看起来像"没打开"。
    /// 所以恢复前先确认位置落在某个屏幕的可用区域内，否则退回系统默认位置。
    /// </summary>
    private void RestoreWindowPlacement()
    {
        if (!double.IsNaN(_state.Settings.WindowLeft) && !double.IsNaN(_state.Settings.WindowTop)
            && IsOnAnyScreen(_state.Settings.WindowLeft, _state.Settings.WindowTop))        {
            WindowStartupLocation = WindowStartupLocation.Manual;
            Position = new PixelPoint(
                (int)Math.Round(_state.Settings.WindowLeft),
                (int)Math.Round(_state.Settings.WindowTop));
        }
        if (_state.Settings.WindowMaximized)
            WindowState = WindowState.Maximized;
    }

    /// <summary>窗口左上角是否落在某块屏幕的可用区域内（留 40px 容错，标题栏要能被抓住）。</summary>
    private bool IsOnAnyScreen(double left, double top)
    {
        foreach (global::Avalonia.Platform.Screen screen in Screens.All)
        {
            PixelRect area = screen.WorkingArea;
            if (left + 40 >= area.X && left <= area.X + area.Width - 40
                && top + 40 >= area.Y && top <= area.Y + area.Height - 40)
            {
                return true;
            }
        }
        return false;
    }

    /// <summary>退出前记住窗口位置/尺寸/最大化状态（与 WPF 同口径；位置用"还原态"的值）。</summary>
    private void SaveWindowPlacement()
    {
        try
        {
            // 最大化时 Position 是最大化后的值，直接记会把"还原后"的位置弄丢，
            // 所以最大化时只更新 WindowMaximized 标志，位置沿用上一次记住的。
            _state.Settings.WindowMaximized = WindowState == WindowState.Maximized;
            if (WindowState == WindowState.Normal)
            {
                _state.Settings.WindowWidth = Width;
                _state.Settings.WindowHeight = Height;
                _state.Settings.WindowLeft = Position.X;
                _state.Settings.WindowTop = Position.Y;
            }
            _state.Settings.Save();
        }
        catch (Exception)
        {
            // 位置记不下来不该阻断退出流程
        }
    }

    /// <summary>取后端的本地化指令表（含技能指令）；失败时保留本地回退表。</summary>
    private async Task LoadCommandsAsync()
    {
        if (CommandCatalog.LoadedFromBackend)
            return;
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "commands" });
        if (doc is null)
            return;
        using (doc)
        {
            if (doc.RootElement.TryGetProperty("commands", out JsonElement commands))
                CommandCatalog.ApplyFromBackend(commands);
        }
    }

    /// <summary>
    /// 刷新状态区（搜索 / KV / 能力 / 模式 / 模型名）。
    ///
    /// **这里此前发的是 `action = "status"`，而后端根本没有这个动作**
    /// （`backend/main.py` 的动作分支表里只有 `context`）。于是响应落到"未知动作"分支，
    /// `ApplyStatusFields` 一个字段都取不到 —— 表现是状态区那几项**启动后永远停在
    /// XAML 的占位值**，而且不报错（这正是"控件在、功能不在"的静默失效）。
    /// 后端 `_attach_context()` 本就把这六类字段挂在 **context** 响应上，
    /// 所以修法不是改后端协议，而是**取对响应**：发 `context`，并把状态字段与上下文
    /// 字段都从同一份响应里取（WPF 侧的 `RefreshStatusBarAsync` 就是这么做的）。
    /// </summary>
    private async Task RefreshStatusAsync()
    {
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "context" });
        if (doc is null)
            return;
        using (doc)
        {
            ApplyStatusFields(doc.RootElement);
            ApplyContextFields(doc.RootElement);
        }
    }

    private async Task RefreshContextAsync()
    {
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "context" });
        if (doc is null)
            return;
        using (doc)
            ApplyContextFields(doc.RootElement);
    }

    private async Task RefreshAutomationStatusAsync()
    {
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "automation_status" });
        if (doc is null)
            return;
        using (doc)
        {
            if (!doc.RootElement.TryGetProperty("automation", out JsonElement st))
                return;
            bool running = st.TryGetProperty("running", out JsonElement r) && r.ValueKind == JsonValueKind.True;
            bool enabled = st.TryGetProperty("enabled", out JsonElement en) && en.ValueKind == JsonValueKind.True;
            SetAutomationStatus(running, enabled);
        }
    }

    private async Task RefreshMarketStatusAsync()
    {
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "market_status" });
        if (doc is null)
            return;
        using (doc)
        {
            if (!doc.RootElement.TryGetProperty("market_status", out JsonElement st))
                return;
            bool enabled = st.TryGetProperty("enabled", out JsonElement en) && en.ValueKind == JsonValueKind.True;
            SetMarketStatus(enabled
                ? Loc.T("main.strip.marketConnected", "🌐 市场：已启用")
                : Loc.T("main.strip.market", "🌐 市场：{0}", Loc.T("settings.market.state.disabled", "未启用")));
        }
    }

    /// <summary>
    /// 检查新版本（任务五 · 方案 A：只检查与提示，不自动下载替换）。
    /// 失败静默 —— 检查更新不该影响正常使用。
    /// </summary>
    private async Task CheckUpdateAsync()
    {
        try
        {
            JsonDocument? doc = await _state.Backend.SendAsync(new { action = "update_check" },
                idleTimeout: TimeSpan.FromSeconds(60));
            if (doc is null)
                return;
            using (doc)
            {
                if (!doc.RootElement.TryGetProperty("update", out JsonElement update))
                    return;
                string status = update.TryGetProperty("status", out JsonElement st) ? st.GetString() ?? "" : "";
                if (!string.Equals(status, "available", StringComparison.Ordinal))
                    return;
                _updateUrl = update.TryGetProperty("url", out JsonElement url) ? url.GetString() ?? "" : "";
                string latest = update.TryGetProperty("latest", out JsonElement l) ? l.GetString() ?? "" : "";
                if (_updateUrl.Length == 0)
                    return;
                _updateAvailable = true;
                StripUpdateText.Text = Loc.T("main.update.notice", "🎉 发现新版本 {0}（当前 {1}）",
                    latest, _state.Backend.Version ?? "v0.2.1");
                ToolTip.SetTip(StripUpdateButton, Loc.T("main.update.tip", "点击打开下载页面") + "\n" + _updateUrl);
                StripUpdateButton.IsVisible = true;
            }
        }
        catch (Exception)
        {
            // 忽略：检查更新是尽力而为
        }
    }

    private void OpenUpdatePage()
    {
        if (_updateUrl.Length == 0)
            return;
        try
        {
            // UseShellExecute 在三大平台上都会交给系统默认浏览器
            Process.Start(new ProcessStartInfo { FileName = _updateUrl, UseShellExecute = true });
        }
        catch (Exception ex)
        {
            Toast(Loc.T("main.update.checkFailed", "打开下载页面失败：{0}", ex.Message), error: true);
        }
    }

    // -----------------------------------------------------------------
    // 状态区字段映射（字段名与 WPF 版完全一致）
    // -----------------------------------------------------------------

    private void ApplyStatusFields(JsonElement root)
    {
        if (root.TryGetProperty("model_ready", out JsonElement readyEl)
            && readyEl.ValueKind == JsonValueKind.False)
        {
            ModelText.Text = Loc.T("main.model.noModel", "🧠 模型: 无模型");
        }
        else if (root.TryGetProperty("model", out JsonElement modelEl) && modelEl.ValueKind == JsonValueKind.String)
        {
            string model = modelEl.GetString() ?? "";
            if (model.Length > 0)
                ModelText.Text = Loc.T("main.model.chip", "🧠 模型: {0}", Shorten(model));
        }
        if (root.TryGetProperty("mode", out JsonElement modeEl) && modeEl.ValueKind == JsonValueKind.String)
        {
            string mode = modeEl.GetString() ?? "";
            if (mode.Length > 0)
                ModeText.Text = Loc.T("main.mode.chip", "🛠️ 模式: {0}", mode);
        }
        if (root.TryGetProperty("search", out JsonElement searchEl)
            && searchEl.ValueKind is JsonValueKind.True or JsonValueKind.False)
        {
            SetSearchStatus(searchEl.GetBoolean());
        }
        if (root.TryGetProperty("kv_cache_text", out JsonElement kvEl) && kvEl.ValueKind == JsonValueKind.String)
            SetKvCacheStatus(kvEl.GetString() ?? "");
        if (root.TryGetProperty("capability", out JsonElement capEl) && capEl.ValueKind == JsonValueKind.Object)
            SetCapabilityStatus(capEl);
        if (root.TryGetProperty("context", out JsonElement ctxEl) && ctxEl.ValueKind == JsonValueKind.Object)
            ApplyContextFields(ctxEl);
    }

    /// <summary>上下文占用：主标题与状态区两处一起更新（都来自后端，前端不估算）。</summary>
    private void ApplyContextFields(JsonElement context)
    {
        int used = GetIntField(context, "used");
        int limit = GetIntField(context, "limit");
        if (limit <= 0)
            return;
        // 任务十（自测问题修复-1）：显示值改用**上一轮真正发出去的量**，理由见
        // backend/main.py 的 _attach_context 与 records §9.4（旧显示只算 system+history，
        // 漏掉文件正文/检索片段，用户看到的值只有真实值的四分之一）。
        // 两者取较大值：底数与上一轮都可能是当前占用的大头，取大不会漏报；
        // 后端未提供该字段时（旧版）自然退化为 used。
        int lastRequest = GetIntField(context, "used_last_request");
        int shown = Math.Max(used, lastRequest);
        // 数字缩写（v0.2.2 第 3 批）：1.2k / 3.4m 这类写法在窄胶囊里比
        // "12000/131072" 可读得多，也与 WPF 一致（差异清单 B7 的「仅文案」一条）。
        ContextChipText.Text = Loc.T("main.context.button", "上下文：{0}/{1}",
            AbbreviateNumber(shown), AbbreviateNumber(limit));
        int percent = (int)Math.Round(shown * 100.0 / limit);
        StripContextText.Text = Loc.T("main.strip.context", "📊 上下文 {0}%", percent);
        ToolTip.SetTip(StripContextButton, Loc.T("main.strip.contextHelpBoth",
            "上下文已用 {0} / {1} tokens（{2}%）。其中常驻底数 {3}，上一轮实际发出 {4}。点击刷新。",
            shown, limit, percent, used, lastRequest));
        // 胶囊本身也要有 ToolTip（此前只有状态区那个按钮有）：鼠标停在标题栏的
        // 「上下文：…」上时，用户看到的第一个反馈就应该是完整口径。
        ToolTip.SetTip(ContextChip, Loc.T("main.context.chipTip",
            "上下文已用 {0} / {1} tokens（{2}%）。常驻底数 {3}，上一轮实际发出 {4}。点击刷新。",
            shown, limit, percent, used, lastRequest));
    }

    /// <summary>
    /// 数字缩写：1200 → 1.2k、3400000 → 3.4m（与 WPF 版同一口径）。
    /// 小于 1000 的原样返回 —— "0.9k" 这种反而更难读。
    /// </summary>
    private static string AbbreviateNumber(int value)
    {
        if (value < 0)
            return value.ToString();
        if (value < 1000)
            return value.ToString();
        if (value < 1000000)
            return (value / 1000.0).ToString("0.#") + "k";
        return (value / 1000000.0).ToString("0.#") + "m";
    }

    private void SetSearchStatus(bool on)
    {
        string state = on ? Loc.T("main.strip.on", "开") : Loc.T("main.strip.off", "关");
        SearchText.Text = Loc.T("main.strip.search", "🔍 搜索：{0}", state);
    }

    private void SetKvCacheStatus(string text)
    {
        if (string.IsNullOrWhiteSpace(text) || text == "---")
            return;
        StripKvText.Text = Loc.T("main.strip.kv", "💾 KV: {0}", text);
    }

    private void SetAutomationStatus(bool running, bool enabled)
    {
        string state = running
            ? Loc.T("main.strip.autoRunning", "运行中")
            : enabled ? Loc.T("main.strip.autoReady", "已启用") : Loc.T("main.strip.off", "关");
        AutoText.Text = Loc.T("main.strip.automation", "⚙️ 自动化：{0}", state);
    }

    private void SetMarketStatus(string text) => MarketText.Text = text;

    /// <summary>
    /// 状态区：模型能力（参数量 · 量化 · 可用窗口 · 实测速度）。
    /// 四项都缺时保持占位文本 —— 空白会让人以为界面坏了。
    /// </summary>
    private void SetCapabilityStatus(JsonElement capability)
    {
        string parameters = GetStringField(capability, "parameters_text");
        string quantization = GetStringField(capability, "quantization");
        int usable = GetIntField(capability, "usable_context");
        double tps = GetDoubleField(capability, "last_tps");

        var parts = new List<string>();
        if (parameters.Length > 0)
            parts.Add(parameters);
        if (quantization.Length > 0)
            parts.Add(quantization);
        if (usable > 0)
            parts.Add($"{usable / 1024}k");
        if (tps > 0)
            parts.Add(Loc.T("main.strip.capabilityTps", "{0:0.#} 字/秒", tps));
        if (parts.Count > 0)
            StripCapabilityText.Text = Loc.T("main.strip.capability", "🧠 {0}", string.Join(" · ", parts));

        _capabilityWarnings = GetStringListField(capability, "warnings");
        StripWarnButton.IsVisible = _capabilityWarnings.Count > 0;
        StripWarnText.Text = Loc.T("main.strip.warn", "⚠️ 能力提示 {0}", _capabilityWarnings.Count);
        if (_capabilityWarnings.Count > 0)
            ToolTip.SetTip(StripWarnButton, string.Join("\n", _capabilityWarnings.Select(w => "• " + w)));
    }

    /// <summary>把全部能力提示讲清楚（状态区只放得下一个数字）。</summary>
    private void ShowWarnings()
    {
        if (_capabilityWarnings.Count == 0)
            return;
        var lines = new List<string> { Loc.T("main.strip.warnTitle", "模型能力提示"), "" };
        for (int i = 0; i < _capabilityWarnings.Count; i++)
            lines.Add($"{i + 1}. {_capabilityWarnings[i]}");
        AddMessage("system", string.Join("\n", lines));
    }

    private static string GetStringField(JsonElement node, string name)
        => node.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? "" : "";

    private static int GetIntField(JsonElement node, string name)
        => node.TryGetProperty(name, out JsonElement el)
           && el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out int value) ? value : 0;

    private static double GetDoubleField(JsonElement node, string name)
        => node.TryGetProperty(name, out JsonElement el)
           && el.ValueKind == JsonValueKind.Number && el.TryGetDouble(out double value) ? value : 0;

    private static List<string> GetStringListField(JsonElement node, string name)
    {
        var list = new List<string>();
        if (node.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in el.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String)
                    list.Add(item.GetString() ?? "");
            }
        }
        return list.Where(x => x.Length > 0).ToList();
    }

    /// <summary>状态栏模型名过长时截断显示。</summary>
    private static string Shorten(string name)
    {
        const int limit = 28;
        return name.Length <= limit ? name : name[..limit] + "…";
    }

    // -----------------------------------------------------------------
    // 状态区的折叠状态（存 ui_state.json，下次启动沿用）
    // -----------------------------------------------------------------

    private void ApplyStatusStripState()
    {
        bool collapsed = _state.Settings.StatusStripCollapsed;
        StatusStripItems.IsVisible = !collapsed;
        StripToggleText.Text = collapsed
            ? Loc.T("main.strip.toggleExpand", "▸")
            : Loc.T("main.strip.toggleCollapse", "▾");
        string tip = collapsed
            ? Loc.T("main.strip.expandTip", "展开状态区（搜索 / 自动化 / 市场 / 上下文 / KV Cache / 能力）")
            : Loc.T("main.strip.collapseTip", "折叠状态区");
        ToolTip.SetTip(StripToggleButton, tip);
        AutomationProperties.SetName(StripToggleButton, tip);
    }

    // -----------------------------------------------------------------
    // 指令面板
    // -----------------------------------------------------------------

    /// <summary>聊天输入框出现 "/"+英文字母 时：只显示底部提示与按钮文字，不改动加号图标。</summary>
    private void UpdateCommandUi(string text)
    {
        _commandLike = text.Length >= 1 && text.StartsWith('/') && (text.Length == 1 || IsAsciiLetter(text[1]));
        CommandBarHint.IsVisible = _commandLike;
        // 输入框里出现 "/" 也弹指令面板（面板目标跟着切到聊天输入框）
        if (text.StartsWith('/'))
            UpdateCommandPalette(InputBox);
        UpdateCommandButtonText();
    }

    private static bool IsAsciiLetter(char c)
        => (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');

    private void UpdateCommandButtonText()
    {
        CommandButtonText.Text = _commandLike
            ? Loc.T("main.command.move", "转移到命令输入框")
            : _commandRowOpen
                ? Loc.T("main.command.close", "关闭命令输入框")
                : Loc.T("main.command.open", "打开命令输入框");
        CommandIconText.Text = _commandRowOpen ? "×" : "+";
    }

    private void ToggleCommandRow()
    {
        // 聊天输入框里像指令 → 按钮变成"转移到命令输入框"：把内容搬过去再展开
        if (_commandLike)
        {
            CommandInputBox.Text = InputBox.Text;
            InputBox.Text = "";
            CommandInputRow.IsVisible = true;
            _commandRowOpen = true;
            CommandInputBox.Focus();
            CommandInputBox.CaretIndex = (CommandInputBox.Text ?? "").Length;
            UpdateCommandButtonText();
            UpdateCommandPalette(CommandInputBox);
            return;
        }
        _commandRowOpen = !_commandRowOpen;
        CommandInputRow.IsVisible = _commandRowOpen;
        if (_commandRowOpen)
            CommandInputBox.Focus();
        else
            CloseAllPalettes();
        UpdateCommandButtonText();
    }

    private void UpdateCommandPalette(TextBox source)
    {
        string text = source.Text ?? "";
        List<CommandItem> filtered = CommandCatalog.Rank(text);
        if (filtered.Count > 0)
        {
            _paletteSource = source;
            CommandPalette.PlacementTarget = source;
            CommandList.ItemsSource = filtered;
            CommandList.SelectedIndex = 0;
            CommandPalette.IsOpen = true;
            return;
        }
        CloseCommandPalette();
    }

    /// <summary>指令面板打开时接管按键。返回 true 表示已处理。</summary>
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
                    return false;   // 无选中项 → 回落为正常发送
                string typed = (_paletteSource?.Text ?? "").Trim();
                bool exact = string.Equals(item.Command, typed, StringComparison.OrdinalIgnoreCase);
                // 候选不唯一且首项并非精确匹配时**先补全不执行**：匹配项多时（如 /s）
                // 应让用户看清完整指令与参数格式后再决定，而不是盲目执行第一项。
                if (!exact && CommandList.Items.Count > 1)
                {
                    ExecuteCommandItem(item, completeOnly: true);
                    Toast(Loc.T("main.command.completed",
                        "已补全为 {0}，确认无误后再按 Enter 执行", item.Command));
                    return true;
                }
                ExecuteCommandItem(item);
                return true;
        }
        return false;
    }

    private void MoveCommandSelection(int delta)
    {
        if (CommandList.Items.Count == 0)
            return;
        int index = (CommandList.SelectedIndex + delta + CommandList.Items.Count) % CommandList.Items.Count;
        CommandList.SelectedIndex = index;
        if (CommandList.SelectedItem is { } selected)
            CommandList.ScrollIntoView(selected);
    }

    private void CloseCommandPalette() => CommandPalette.IsOpen = false;

    private void CloseAllPalettes()
    {
        CommandPalette.IsOpen = false;
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
    }

    private void ExecuteCommandItem(CommandItem item, bool completeOnly = false)
    {
        CloseCommandPalette();
        TextBox target = _paletteSource ?? CommandInputBox;

        // 需要参数或 Tab 补全 → 只填"指令+空格"，让用户继续输入参数
        if (item.NeedsArgs || completeOnly)
        {
            target.Text = item.Command + " ";
            target.CaretIndex = target.Text.Length;
            target.Focus();
            return;
        }
        // 无需参数 → 直接执行
        if (ReferenceEquals(target, InputBox))
        {
            InputBox.Text = item.Command;
            _ = SendAsync();       // 危险指令的确认环节在 SendAsync 内统一处理
            return;
        }
        RunCommandFromCommandBox(item.Command);
    }

    /// <summary>从指令输入框执行一条指令（含危险指令的二次确认）。</summary>
    private void RunCommandFromCommandBox(string command)
    {
        if (!ConfirmDestructive(command))
            return;
        CommandInputBox.Text = "";
        _ = SendCommandAsync(command);
    }

    /// <summary>
    /// 危险指令的二次确认。
    ///
    /// Avalonia 没有内置 MessageBox，而"再点一次确认"这个项目里已有的做法
    /// （见设置页的卸载确认）在这里不适用 —— 指令是靠 Enter 触发的，没有按钮可点。
    /// 因此改成**同一个输入框里再按一次 Enter**：第一次把指令放回输入框并提示，
    /// 第二次才真的执行。判据是"输入框里现在就是这条危险指令"，用户改一个字就重置。
    /// </summary>
    private bool ConfirmDestructive(string command)
    {
        if (!_state.Settings.ConfirmDestructiveCommands || !IsDestructive(command))
        {
            _pendingDestructive = "";
            return true;
        }
        if (string.Equals(_pendingDestructive, command, StringComparison.OrdinalIgnoreCase)
            && string.Equals((CommandInputBox.Text ?? "").Trim(), command, StringComparison.OrdinalIgnoreCase))
        {
            _pendingDestructive = "";
            return true;
        }
        _pendingDestructive = command;
        CommandInputRow.IsVisible = true;
        _commandRowOpen = true;
        CommandInputBox.Text = command;
        CommandInputBox.CaretIndex = command.Length;
        CommandInputBox.Focus();
        UpdateCommandButtonText();
        Toast(Loc.T("main.command.confirmDestructive",
            "⚠️ {0} 会改变现有状态：再按一次 Enter 确认执行", command));
        return false;
    }

    private static bool IsDestructive(string command)
    {
        CommandItem? item = CommandCatalog.Items
            .FirstOrDefault(c => string.Equals(c.Command, command, StringComparison.OrdinalIgnoreCase));
        if (item is not null)
            return item.Destructive;
        // 回退表没命中（例如技能指令）时按已知的危险指令名单判断
        return command is "/clear" or "/clearcache" or "/exit" or "/quit";
    }

    /// <summary>
    /// 把一条指令发给后端（走非流式 chat 动作，与 WPF 版的 SendCommandAsync 一致）。
    ///
    /// **但有几条指令必须由前端本地处理**（与 WPF 侧同一套做法）：后端对它们只回一句
    /// 说明文本、不做任何事，转发过去等于"看起来执行了、其实什么都没发生"：
    ///   * `/darkmode`  → 真正切换主题（后端不改前端主题，它管不着）；
    ///   * `/snippet …` → 常用语的增删列表（存在前端 `snippets.json` 里，
    ///                    后端的 `_snippet` 只回一句提示，SnippetStore 的 Add/Remove
    ///                    此前**没有任何调用者**）；
    ///   * `/help`、`/stop` → 后端不可用时的兜底。
    /// 后端在线时 `/help` 仍然走后端（指令表由后端权威给出），只有离线才本地兜底。
    /// </summary>
    private async Task SendCommandAsync(string command)
    {
        if (_busy)
        {
            Toast(Loc.T("main.error.busy", "⏳ 上一条回复仍在生成，请先停止或等待完成"), error: true);
            return;
        }

        string head = command.Trim();
        string verb = head.Split(' ', 2)[0].ToLowerInvariant();
        if (!_state.Backend.IsRunning
            && (verb == "/help" || verb == "/stop" || verb == "/darkmode" || verb == "/snippet"))
        {
            // 离线兜底：这几个不需要后端也能正确执行
            AddMessage("user", command);
            HandleLocalCommand(verb, head);
            return;
        }
        if (verb == "/darkmode" || verb == "/snippet")
        {
            AddMessage("user", command);
            HandleLocalCommand(verb, head);
            return;
        }

        AddMessage("user", command);
        SetBusy(true);
        try
        {
            JsonDocument? doc = await _state.Backend.SendAsync(new { action = "chat", message = command },
                idleTimeout: BackendService.HeavyIdleTimeout);
            if (doc is null)
            {
                Toast(Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", _state.Backend.LastError ?? ""), error: true);
                return;
            }
            using (doc)
            {
                string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                string action = GetStringField(doc.RootElement, "action");
                // `/exit`：背板只回一句致谢，真正的"倒计时 + 关闭"必须前端做
                // （v0.2.2 第 3 批，与 WPF 的 HandleChatResponse 同一处理）。
                if (action == "exit")
                {
                    BeginExitCountdown(content.Length > 0
                        ? content
                        : Loc.T("main.thanks", "👋 感谢使用熵寂工作室出品的 EchoDesk，欢迎您下次使用！"));
                    return;
                }
                if (content.Length > 0)
                    AddMessage("assistant", content);
                ApplyStatusFields(doc.RootElement);
                // `/model` 需要下载时后端回 confirm_model，由前端征得同意后再发下载
                if (action == "confirm_model")
                    HandleConfirmModel(GetStringField(doc.RootElement, "model"));
                if (doc.RootElement.TryGetProperty("context", out JsonElement ctx) && ctx.ValueKind == JsonValueKind.Object)
                    ApplyContextFields(ctx);
            }
        }
        catch (Exception ex)
        {
            Toast(Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message), error: true);
        }
        finally
        {
            SetBusy(false);
            await RefreshContextAsync();
        }
    }

    // -----------------------------------------------------------------
    // 本地指令（不经过后端；与 WPF 版的本地兜底同一套语义）
    // -----------------------------------------------------------------

    /// <summary>
    /// `/darkmode` 与 `/snippet` 的前端本地实现。
    ///
    /// 为什么不能让后端做：
    ///   * 主题是**前端**的状态（`AppState.SetTheme`），后端改不了也不该改；
    ///   * 常用语存在前端自己的 `snippets.json`（`SnippetStore`），后端只回一句提示。
    /// 这两条在 WPF 侧都是本地处理的（`HandleDarkMode` / `HandleSnippetCommand`），
    /// Avalonia 迁移时漏了，于是用户敲下去只看到一句"这个是前端管的"之类的回执。
    /// </summary>
    private void HandleLocalCommand(string verb, string raw)
    {
        switch (verb)
        {
            case "/darkmode":
            {
                // 与主题按钮同一套三态循环：auto → light → dark → auto
                string next = _state.Settings.Theme switch
                {
                    "auto" => "light",
                    "light" => "dark",
                    _ => "auto",
                };
                // 主题名走**已存在的**三个键（settings.general.themeSystem/Light/Dark）。
                // 不要动态拼 "settings.general.theme." + next —— 那会拼出一个不存在的键，
                // 语言包缺失时 Loc.T 会静默回落成兜底中文（反向审计脚本抓到过这一类）。
                string label = next switch
                {
                    "light" => Loc.T("settings.general.themeLight", "浅色"),
                    "dark" => Loc.T("settings.general.themeDark", "深色"),
                    _ => Loc.T("settings.general.themeSystem", "跟随系统"),
                };
                _state.SetTheme(next);
                ApplyTexts();
                // 消息区整屏重渲染：换主题后旧气泡的配色也要跟着变（与换字号/换语言同一路径）
                RenderTranscript();
                AddMessage("assistant", Loc.T("main.local.darkmode", "🎨 主题已切换为：{0}", label));
                break;
            }
            case "/snippet":
            {
                string[] parts = raw.Split(' ', 3, StringSplitOptions.RemoveEmptyEntries);
                string action = parts.Length > 1 ? parts[1].ToLowerInvariant() : "list";
                if (action == "add")
                {
                    // 用法：/snippet add 名称 文本
                    if (parts.Length < 4 && parts.Length < 3)
                    {
                        AddMessage("assistant", Loc.T("main.snippet.usageAdd",
                            "用法：/snippet add 名称 文本…"));
                        break;
                    }
                    string name = parts[2];
                    string text = parts.Length > 3 ? parts[3] : "";
                    if (text.Length == 0)
                    {
                        AddMessage("assistant", Loc.T("main.snippet.usageAdd",
                            "用法：/snippet add 名称 文本…"));
                        break;
                    }
                    bool added = _snippets.Add(name, text);
                    AddMessage("assistant", added
                        ? Loc.T("main.snippet.added", "✅ 已新增常用语「{0}」", name)
                        : Loc.T("main.snippet.exists", "❌ 常用语「{0}」已存在（可先 remove 再添加）", name));
                    break;
                }
                if (action == "remove")
                {
                    if (parts.Length < 3)
                    {
                        AddMessage("assistant", Loc.T("main.snippet.usageRemove",
                            "用法：/snippet remove 名称"));
                        break;
                    }
                    string name = parts[2];
                    bool removed = _snippets.Remove(name);
                    AddMessage("assistant", removed
                        ? Loc.T("main.snippet.removed", "🗑️ 已删除常用语「{0}」", name)
                        : Loc.T("main.snippet.notFound", "❌ 未找到常用语「{0}」", name));
                    break;
                }
                // list（或缺省）
                _snippets.Load();
                AddMessage("assistant", _snippets.Items.Count == 0
                    ? Loc.T("main.snippet.empty", "（还没有常用语，输入 /snippet add 名称 文本 添加）")
                    : Loc.T("main.snippet.listHeader", "📋 常用语：\n") + string.Join("\n",
                        _snippets.Items.Select(s => Loc.T("main.snippet.listItem", "🔹 {0}：{1}", s.Name, s.Text))));
                break;
            }
            case "/help":
                AddMessage("assistant", Loc.T("main.local.helpOffline",
                    "后端未运行，暂时看不到完整指令表。后端启动后输入 /help 可列出全部指令。"));
                break;
            case "/stop":
                _ = StopAsync();
                break;
            default:
                AddMessage("assistant", Loc.T("main.local.commandOffline",
                    "后端未运行，这条指令暂时无法执行。"));
                break;
        }
    }

    // -----------------------------------------------------------------
    // 常用语面板
    // -----------------------------------------------------------------

    private void ToggleSnippetPalette()
    {
        if (SnippetPalette.IsOpen)
        {
            SnippetPalette.IsOpen = false;
            UpdateSnippetButtonText();
            return;
        }
        _snippets.Load();
        SnippetList.ItemsSource = _snippets.Items.ToList();
        if (_snippets.Items.Count > 0)
            SnippetList.SelectedIndex = 0;
        SnippetPalette.PlacementTarget = SnippetButton;
        SnippetPalette.IsOpen = true;
        UpdateSnippetButtonText();
        if (_snippets.Items.Count == 0)
            Toast(Loc.T("main.snippet.empty", "还没有常用语：用 /snippet add 名称 文本 添加"));
    }

    private void UpdateSnippetButtonText()
        => SnippetButtonText.Text = SnippetPalette.IsOpen
            ? Loc.T("main.snippet.close", "收起常用语选择框")
            : Loc.T("main.snippet.open", "打开常用语选择框");

    /// <summary>点选一条常用语 → **插入到光标处**（v0.2.2 第 4 批 3c）。</summary>
    ///
    /// 此前是"覆盖整个输入框"：用户先写了一半、再想插一句常用语，前面的内容就没了。
    /// WPF 侧的做法是插入到光标位置，这里对齐 —— 光标停在哪儿就插到哪儿，
    /// 并且插完把光标放到插入内容的后面（用户可以接着写）。
    private void InsertSnippet(Snippet snippet)
    {
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
        string current = InputBox.Text ?? "";
        int caret = Math.Clamp(InputBox.CaretIndex, 0, current.Length);
        string inserted = snippet.Text;
        // 前后各补一个空格，避免和已有文字粘成一个词（已经是行首/行尾时不补）
        bool needSpaceBefore = caret > 0 && !char.IsWhiteSpace(current[caret - 1]);
        bool needSpaceAfter = caret < current.Length && !char.IsWhiteSpace(current[caret]);
        string piece = (needSpaceBefore ? " " : "") + inserted + (needSpaceAfter ? " " : "");
        InputBox.Text = current[..caret] + piece + current[caret..];
        InputBox.CaretIndex = caret + piece.Length;
        InputBox.Focus();
        Toast(Loc.T("main.snippet.inserted", "已插入常用语「{0}」，可直接编辑后发送", snippet.Name));
    }

    // -----------------------------------------------------------------
    // 通知条
    // -----------------------------------------------------------------

    /// <summary>底部通知条：短暂显示后自动隐藏（6 秒，与 WPF 版一致）。</summary>
    private void Toast(string message, bool error = false)
    {
        ToastText.Text = Emoji.Prefer(message);
        if (Application.Current is not null
            && Application.Current.TryFindResource(error ? "ErrorBrush" : "AccentBrush", out object? brush)
            && brush is IBrush ib)
        {
            ToastText.Foreground = ib;
        }
        ToastBorder.IsVisible = true;
        HintText.IsVisible = false;
        _toastTimer ??= new DispatcherTimer { Interval = TimeSpan.FromSeconds(6) };
        _toastTimer.Stop();
        _toastTimer.Tick -= OnToastTick;
        _toastTimer.Tick += OnToastTick;
        _toastTimer.Start();
    }

    private void OnToastTick(object? sender, EventArgs e)
    {
        _toastTimer?.Stop();
        ToastBorder.IsVisible = false;
        HintText.IsVisible = true;
    }

    // -----------------------------------------------------------------
    // 搜索开关 / 上下文压缩
    // -----------------------------------------------------------------

    private async Task ToggleSearchAsync()
    {
        if (!_state.Backend.IsRunning)
            return;
        try
        {
            JsonDocument? doc = await _state.Backend.SendAsync(new { action = "chat", message = "/search toggle" },
                idleTimeout: TimeSpan.FromSeconds(60));
            if (doc is null)
            {
                Toast(Loc.T("main.strip.searchToggleFailed", "切换搜索失败：{0}", _state.Backend.LastError ?? ""), error: true);
                return;
            }
            using (doc)
            {
                string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                ApplyStatusFields(doc.RootElement);
                Toast(content.Length > 0 ? content : Loc.T("main.strip.searchToggled", "已切换联网搜索"));
            }
        }
        catch (Exception ex)
        {
            Toast(Loc.T("main.strip.searchToggleFailed", "切换搜索失败：{0}", ex.Message), error: true);
        }
    }

    /// <summary>
    /// 手动压缩上下文：走 context_compress 而不是让用户等自动触发 ——
    /// 准备上传大文件、或上下文已经 70% 时，用户需要一个"立刻腾地方"的按钮。
    /// </summary>
    private async Task CompactContextAsync()
    {
        if (!_state.Backend.IsRunning)
            return;
        StripCompactButton.IsEnabled = false;
        StripCompactText.Text = Loc.T("main.strip.compacting", "🗜 压缩中…");
        try
        {
            JsonDocument? doc = await _state.Backend.SendAsync(new { action = "context_compress", force = true },
                idleTimeout: BackendService.HeavyIdleTimeout);
            if (doc is null)
            {
                Toast(Loc.T("main.strip.compactFailed", "❌ 压缩上下文失败：{0}", _state.Backend.LastError ?? ""), error: true);
                return;
            }
            using (doc)
            {
                if (doc.RootElement.TryGetProperty("context", out JsonElement context))
                    ApplyContextFields(context);
                string message = Loc.T("main.strip.compactNone", "上下文无需压缩");
                if (doc.RootElement.TryGetProperty("compress", out JsonElement compress)
                    && compress.ValueKind == JsonValueKind.Object
                    && compress.TryGetProperty("compressed", out JsonElement c)
                    && c.ValueKind == JsonValueKind.True)
                {
                    int dropped = GetIntField(compress, "dropped");
                    int before = GetIntField(compress, "used_before");
                    int after = GetIntField(compress, "used_after");
                    message = after > 0
                        ? Loc.T("main.strip.compactDone", "已压缩：移出 {0} 条（{1} → {2} tokens）", dropped, before, after)
                        : Loc.T("main.strip.compactDoneShort", "已压缩：移出 {0} 条较早消息", dropped);
                }
                AddMessage("system", Loc.T("main.strip.compactResult", "🗜 {0}", message));
                Toast(message);
            }
        }
        catch (Exception ex)
        {
            Toast(Loc.T("main.strip.compactFailed", "❌ 压缩上下文失败：{0}", ex.Message), error: true);
        }
        finally
        {
            StripCompactText.Text = Loc.T("main.strip.compact", "🗜 压缩上下文");
            StripCompactButton.IsEnabled = true;
        }
    }

    // -----------------------------------------------------------------
    // 设置窗口
    // -----------------------------------------------------------------

    /// <summary>打开设置窗口并选中某个页签（状态区各项都是"点一下跳到对应设置"）。</summary>
    private async Task OpenSettingsAsync(string tabKey)
    {
        try
        {
            var window = new SettingsWindow(_state);
            window.SelectTab(tabKey);
            await window.ShowDialog(this);
            // 回来后把可能被改动的业务设置反映到状态区
            ApplyTexts();
            await RefreshStatusAsync();
        }
        catch (Exception ex)
        {
            Toast(Loc.T("main.error.settingsFailed", "⚠️ 无法打开设置窗口：{0}", ex.Message), error: true);
        }
    }

    // -----------------------------------------------------------------
    // 发送 / 停止
    // -----------------------------------------------------------------

    private async Task SendAsync()
    {
        string text = (InputBox.Text ?? "").Trim();
        if (text.Length == 0 || _busy || !_state.Backend.IsRunning)
            return;

        // 危险指令在发送前统一确认（与从指令面板执行时同一判据）
        if (text.StartsWith('/') && IsDestructive(text) && !ConfirmDestructiveSend(text))
            return;

        InputBox.Text = "";
        AddMessage("user", text);

        SetBusy(true);
        try
        {
            if (!_state.Backend.ModelReady)
            {
                // 无模型：后端会返回引导文案（非流式），界面不把它当失败
                JsonDocument? doc = await _state.Backend.SendAsync(new { action = "chat", message = text });
                if (doc is null)
                {
                    AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", _state.Backend.LastError ?? ""));
                    return;
                }
                using (doc)
                {
                    string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                    AddMessage("assistant", content);
                    ApplyStatusFields(doc.RootElement);
                }
                return;
            }

            await StreamChatAsync(text);
        }
        finally
        {
            SetBusy(false);
        }
    }

    /// <summary>
    /// 聊天输入框里直接发送危险指令时的确认。
    /// 与指令输入框同一套两段式确认，只是位置不同 —— 这里把指令搬进指令输入框，
    /// 让"再按一次 Enter"发生在同一个地方（避免两个输入框各有一套确认状态）。
    /// </summary>
    private bool ConfirmDestructiveSend(string command)
    {
        if (!_state.Settings.ConfirmDestructiveCommands)
            return true;
        _pendingDestructive = command;
        CommandInputRow.IsVisible = true;
        _commandRowOpen = true;
        InputBox.Text = "";
        CommandInputBox.Text = command;
        CommandInputBox.CaretIndex = command.Length;
        CommandInputBox.Focus();
        UpdateCommandButtonText();
        Toast(Loc.T("main.command.confirmDestructive",
            "⚠️ {0} 会改变现有状态：再按一次 Enter 确认执行", command));
        return false;
    }

    /// <summary>
    /// [EN] Streams one conversation turn: dual-channel deltas, structured events and
    ///      cancellation support. Every delta reaches the buffers through the stream gate and is
    ///      rendered by the coalescing timer, never directly from the reader thread.
    /// [ZH] 流式执行一轮对话：双通道增量、结构化事件与可中止能力。每个增量均通过流式闸门写入
    ///      缓冲区，并由合并式定时器渲染，绝不直接在后端读取线程上渲染。
    /// </summary>
    private async Task StreamChatAsync(string text)
    {
        ResetStreamBuffers();
        _live = AddMessageView("assistant", "", "");
        StartRenderTimer();
        _streamCts = new CancellationTokenSource();

        string finalText = "";
        _liveMetrics = "";
        try
        {
            finalText = await _state.Backend.SendStreamAsync(
                new { action = "chat_stream", message = text },
                onDelta: delta => AppendAnswerText(delta),
                // [EN] Context accounting is handled exclusively by the done event through
                //      DispatchStreamEvent and ApplyContextFields, because only that path carries
                //      the value of the previous request. Reporting it here would overwrite that
                //      authoritative figure.
                // [ZH] 上下文计量仅由 done 事件经 DispatchStreamEvent 与 ApplyContextFields 处理，
                //      因为只有该路径携带上一轮请求的数值。在此处重复上报会覆盖该权威数值。
                onContext: (_, _) => { },
                idleTimeout: null,   // 流式不设空闲超时（后端可能在等待很长的生成）
                cancellationToken: _streamCts.Token,
                onEvent: DispatchStreamEvent,
                onDeltaChannel: (channel, content, reset) => Dispatcher.UIThread.Post(() =>
                {
                    if (channel == "reasoning")
                    {
                        AppendReasoningText(content);
                        if (_live is not null)
                        {
                            _live.ThinkingText.Text = Emoji.Prefer(SnapshotReasoning());
                            _live.ThinkingBox.IsVisible = true;
                        }
                    }
                    else
                    {
                        if (reset)
                        {
                            // [EN] The answer channel is declared void: the accumulated content in
                        //      fact belongs to the reasoning channel and is moved there.
                        // [ZH] 回答通道被声明作废：此前累积的内容实际属于思考通道，故迁移至该通道。
                            lock (_streamGate)
                            {
                                _liveReasoning.Insert(0, _liveAnswer.ToString());
                                _liveAnswer.Clear();
                            }
                            if (_live is not null)
                            {
                                _live.ThinkingText.Text = Emoji.Prefer(SnapshotReasoning());
                                _live.ThinkingBox.IsVisible = true;
                            }
                        }
                        AppendAnswerText(content);
                    }
                }));
        }
        catch (OperationCanceledException)
        {
            finalText = SnapshotAnswer();
            _liveMetrics = Loc.T("main.metrics.stopped", "⏹ 已中止");
            if (_live is not null)
                _live.Metrics = _liveMetrics;
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
            finalText = SnapshotAnswer();
            // [EN] Generation failed: the metrics row must state the failure explicitly,
            //      otherwise the previous figures remain on screen and the interface appears to be
            //      still computing.
            // [ZH] 生成失败：指标行必须明确说明失败，否则界面上会残留上一轮的数值，
            //      使界面看起来仍在计算。
            _metricsFailed = true;
            Dispatcher.UIThread.Post(() => ShowMetrics(_liveMetrics));
        }
        finally
        {
            StopRenderTimer();
            string answer = finalText.Length > 0 ? finalText : SnapshotAnswer();
            string reasoning = SnapshotReasoning();
            RemoveLiveMessage();
            AddMessage("assistant", answer, reasoning, _liveMetrics);
            _streamCts?.Dispose();
            _streamCts = null;
            _agentRegion = null;
            _agentRows = null;
            await RefreshContextAsync();
        }
    }

    /// <summary>
    /// 流式事件分发（progress / agent_step / tool_call / tool_result / done）。
    ///
    /// **为什么它是一个独立方法而不是内联在 StreamChatAsync 的 lambda 里**：
    /// 无人值守的界面探针（Probe.cs）无法"点一下界面"来触发 Agent 步骤行，
    /// 但它可以把后端真实发出的那些事件行逐条喂进这个方法，再读
    /// <see cref="AgentStepRowCount"/> 断言"步骤行确实渲染出来了"。
    /// 内联在 lambda 里时这条路径**没有任何办法在无人值守下验证**，
    /// 而 v0.2.2 记录 §10 第 3 条（Agent 步骤行渲染）正是卡在这里。
    /// </summary>
    public void DispatchStreamEvent(string type, JsonElement root)
    {
        switch (type)
        {
            case "progress":
            {
                // 心跳：说明"还在算"，避免界面看起来像卡死
                if (root.TryGetProperty("stats", out JsonElement beat) && beat.ValueKind == JsonValueKind.Object)
                {
                    bool waiting = root.TryGetProperty("waiting_first_token", out JsonElement w)
                                   && w.ValueKind == JsonValueKind.True;
                    Dispatcher.UIThread.Post(() => ShowMetrics(FormatMetrics(beat, waiting)));
                }
                string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                Dispatcher.UIThread.Post(() => SetStatus(Trim(Emoji.Prefer(content), 120)));
                break;
            }
            case "agent_step":
                HandleAgentStep(root);
                break;
            case "tool_call":
                HandleToolCall(root);
                break;
            case "tool_result":
                HandleToolResult(root);
                break;
            case "done":
            {
                if (root.TryGetProperty("reasoning", out JsonElement r))
                {
                    string reasoning = r.GetString() ?? "";
                    if (reasoning.Length > 0 && SnapshotReasoning().Length == 0)
                    {
                        AppendReasoningText(reasoning);
                        Dispatcher.UIThread.Post(() =>
                        {
                            if (_live is not null)
                            {
                                _live.ThinkingText.Text = Emoji.Prefer(SnapshotReasoning());
                                _live.ThinkingBox.IsVisible = true;
                            }
                        });
                    }
                }
                if (root.TryGetProperty("stats", out JsonElement stats) && stats.ValueKind == JsonValueKind.Object)
                {
                    bool stopped = root.TryGetProperty("stopped", out JsonElement st)
                                   && st.ValueKind == JsonValueKind.True;
                    _liveMetrics = FormatMetrics(stats, stopped);
                    string text = _liveMetrics;
                    Dispatcher.UIThread.Post(() => ShowMetrics(text));
                }
                if (root.TryGetProperty("reasoning_unclosed", out JsonElement unclosed)
                    && unclosed.ValueKind == JsonValueKind.True)
                {
                    _liveMetrics = (_liveMetrics + "  "
                        + Loc.T("main.metrics.reasoningUnclosed", "⚠️ 思考未闭合（token 预算可能被思考用完）")).Trim();
                    // 只拼进指标一行是不够的：思考未闭合意味着**思考还没写完就被截断**，
                    // 用户需要立刻看到那段内容才能判断是不是预算不够（WPF 会展开折叠区）。
                    Dispatcher.UIThread.Post(() =>
                    {
                        if (_live is not null && _live.ThinkingText.Text?.Length > 0)
                        {
                            _live.ThinkingBox.IsVisible = true;
                            _live.ThinkingBox.IsExpanded = true;
                        }
                    });
                }
                HandleAgentSummary(root);
                break;
            }
        }
    }

    private static string FormatMetrics(JsonElement stats, bool stopped)
    {
        var parts = new List<string>();
        if (stats.TryGetProperty("first_token_ms", out JsonElement ft) && ft.ValueKind == JsonValueKind.Number && ft.GetInt32() > 0)
            parts.Add(Loc.T("main.metrics.ttft", "⚡ 首 token {0}", FormatMs(ft.GetInt32())));
        if (stats.TryGetProperty("tps", out JsonElement tps) && tps.ValueKind == JsonValueKind.Number)
            parts.Add(Loc.T("main.metrics.tps", "🚀 {0:0.#} token/s", tps.GetDouble()));
        if (stats.TryGetProperty("tokens", out JsonElement tokens) && tokens.ValueKind == JsonValueKind.Number)
        {
            bool estimated = stats.TryGetProperty("tokens_estimated", out JsonElement est) && est.ValueKind == JsonValueKind.True;
            parts.Add(estimated
                ? Loc.T("main.metrics.outputTokensEstimated", "≈{0} tokens", tokens.GetInt32())
                : Loc.T("main.metrics.outputTokens", "{0} tokens", tokens.GetInt32()));
        }
        if (stats.TryGetProperty("elapsed_ms", out JsonElement el) && el.ValueKind == JsonValueKind.Number)
            parts.Add(Loc.T("main.metrics.elapsed", "⏱️ {0}", FormatMs(el.GetInt32())));
        if (stopped)
            parts.Add(Loc.T("main.metrics.stopped", "⏹ 已中止"));
        return string.Join(" · ", parts);
    }

    private static string FormatMs(int ms) => ms >= 1000 ? $"{ms / 1000.0:0.#}s" : $"{ms}ms";

    /// <summary>
    /// 状态区的运行指标（后端实测上报，前端不做估算）。
    ///
    /// **失败态文案**（v0.2.2 第 4 批，差异清单 §F5 第 2 条）：WPF 的指标区分
    /// running / waiting / done / stopped / **failed**，失败时显示 `main.metrics.failed`。
    /// 跨平台版此前只有"有值就显示、没值就保持旧值"，生成失败时界面看起来像"还在算" ——
    /// 这条差异本轮补掉。
    /// </summary>
    private void ShowMetrics(string text)
    {
        _stripMetrics = text ?? "";
        if (_metricsFailed)
        {
            string failed = Loc.T("main.metrics.failed", "⚠️ 失败");
            _stripMetrics = _stripMetrics.Length > 0 ? $"{_stripMetrics}  ·  {failed}" : failed;
        }
        MetricsText.Text = Emoji.Prefer(Trim(_stripMetrics, 120));
        MetricsText.IsVisible = _stripMetrics.Length > 0;
        ToolTip.SetTip(MetricsText, Loc.T("main.metrics.tip", "运行指标：全部由后端实测上报"));
    }

    private async Task StopAsync()
    {
        if (_streamCts is null)
            return;
        await _state.Backend.SendFireAndForgetAsync(new { action = "stop" });
        try
        {
            _streamCts.Cancel();
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private void SetBusy(bool busy)
    {
        _busy = busy;
        SendButton.IsEnabled = !busy;
        StopButton.IsVisible = busy;
        // 指标定时刷新与"忙"状态同生共死（v0.2.2 第 3 批）：
        // 空闲时每 500ms 问一次后端纯属浪费，而生成中不刷新就看不出速度变化。
        if (busy)
        {
            _metricsFailed = false;   // 新一轮生成：清掉上一次的失败标记
            StartMetricsTimer();
        }
        else
        {
            StopMetricsTimer();
        }
    }

    // -----------------------------------------------------------------
    // Agent 步骤区（agent_step / tool_call / tool_result 各一行）
    // -----------------------------------------------------------------

    private void HandleAgentStep(JsonElement root)
    {
        string phase = GetStringField(root, "phase");
        int stepIndex = GetIntField(root, "index");
        int maxSteps = GetIntField(root, "max_steps");
        if (phase == "finalize")
        {
            // 步数用完后的"收敛"补一轮：用户要知道为什么突然出结论了
            AddAgentRow(Loc.T("main.agent.finalize", "🧩 正在汇总结论"), "MutedForegroundBrush");
        }
        else if (phase == "thinking")
        {
            AddAgentRow(maxSteps > 0
                    ? Loc.T("main.agent.step", "📍 第 {0}/{1} 步", stepIndex, maxSteps)
                    : Loc.T("main.agent.stepNoMax", "📍 第 {0} 步", stepIndex),
                "MutedForegroundBrush");
        }
        else if (phase == "thought")
        {
            // 任务十三（自测问题修复-1）：后端把这一步的思考内容分离出来后，随
            // phase="thought" 事件发过来。WPF 版把这一行渲染出来，Avalonia 迁移时漏了
            // —— 于是跨平台版上"Agent 的思考过程"仍然看不见（本轮逐项比对时发现）。
            // 有思考就渲染成一行（截断到 200 字符，与 tool_result 的观察结果同一口径），
            // 没有就什么都不打，避免每步都多出一行空的。
            string reasoning = GetStringField(root, "reasoning");
            if (reasoning.Trim().Length > 0)
                AddAgentRow(Loc.T("main.agent.thought", "💭 {0}", OneLineText(reasoning, 200)),
                    "MutedForegroundBrush");
        }
        // phase == "answer"：模型已决定直接作答，答案马上出现在回答区，
        // 这里不再多打一行（步骤区只讲"用了什么工具"）
    }

    private void HandleToolCall(JsonElement root)
    {
        string tool = GetStringField(root, "tool");
        string why = GetStringField(root, "why");
        string args = root.TryGetProperty("args", out JsonElement argsEl)
            ? OneLineText(argsEl.GetRawText(), 120) : "";
        var line = new StringBuilder(Loc.T("main.agent.call", "🔧 调用 {0}", tool));
        if (args.Length > 0)
            line.Append(' ').Append(Loc.T("main.agent.args", "参数 {0}", args));
        if (why.Length > 0)
            line.Append(' ').Append(Loc.T("main.agent.why", "· 理由：{0}", why));
        AddAgentRow(line.ToString(), "AccentBrush");
    }

    private void HandleToolResult(JsonElement root)
    {
        string tool = GetStringField(root, "tool");
        string observation = GetStringField(root, "observation");
        int elapsedMs = GetIntField(root, "elapsed_ms");
        bool ok = root.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True;
        if (!ok && IsAgentDeniedObservation(observation))
        {
            // 权限不足单独一档、且加重字重：这不是"工具坏了"，而是"用户没授权"
            AddAgentRow(Loc.T("main.agent.denied",
                    "🔒 {0} 未启用（用 /agent allow {1} 授权后可用）", tool, tool),
                "WarningBannerBrush", strong: true);
        }
        else if (!ok)
        {
            AddAgentRow(Loc.T("main.agent.failed", "❌ {0} 失败：{1}", tool, OneLineText(observation, 200)),
                "MutedForegroundBrush");
        }
        else
        {
            AddAgentRow(Loc.T("main.agent.result", "↩ {0} 返回：{1}", tool, OneLineText(observation, 200))
                        + " " + Loc.T("main.agent.elapsed", "（{0} ms）", elapsedMs),
                "MutedForegroundBrush");
        }
    }

    /// <summary>Agent 轮的汇总（步数 / 工具 / 总耗时）。判据是"真的跑过 Agent"。</summary>
    private void HandleAgentSummary(JsonElement root)
    {
        if (!root.TryGetProperty("agent", out JsonElement agent) || agent.ValueKind != JsonValueKind.Object)
            return;
        int steps = GetIntField(agent, "steps");
        if (steps <= 0 && _agentRegion is null)
            return;   // 普通对话不该被塞进一行"共 0 步"
        if (agent.TryGetProperty("hit_max_steps", out JsonElement hit) && hit.ValueKind == JsonValueKind.True)
            AddAgentRow(Loc.T("main.agent.hitMax", "⚠️ 已达步数上限，改为直接给结论"), "WarningBannerBrush");
        bool stopped = root.TryGetProperty("stopped", out JsonElement st) && st.ValueKind == JsonValueKind.True;
        string summary = stopped
            ? Loc.T("main.agent.summaryStopped", "⏹ 已中止：{0} 步 · {1} ms", steps, GetIntField(agent, "elapsed_ms"))
            : Loc.T("main.agent.summary", "✅ 完成：{0} 步 · {1} ms", steps, GetIntField(agent, "elapsed_ms"));
        List<string> tools = GetStringListField(agent, "tools");
        if (tools.Count > 0)
            summary += " · " + Loc.T("main.agent.toolsUsed", "工具：{0}", string.Join(", ", tools));
        AddAgentRow(summary, "MutedForegroundBrush");
    }

    private void EnsureAgentRegion()
    {
        if (_agentRows is not null)
            return;
        (_agentRegion, _agentRows) = CreateAgentTraceRegion();
        MessagePanel.Children.Add(_agentRegion);
    }

    private void AddAgentRow(string text, string brushKey, bool strong = false)
    {
        if (string.IsNullOrEmpty(text))
            return;
        Dispatcher.UIThread.Post(() =>
        {
            EnsureAgentRegion();
            if (_agentRows is null)
                return;
            AppendAgentTraceRow(_agentRows, text, brushKey, strong);
            ScrollToBottom();
        });
    }

    private (Border Root, StackPanel Rows) CreateAgentTraceRegion()
    {
        var title = new TextBlock
        {
            Text = Loc.T("main.agent.title", "🛠️ Agent 步骤"),
            FontSize = Math.Max(9, _state.Settings.FontSize - 2),
            Opacity = 0.7,
        };
        var rows = new StackPanel { Spacing = 1 };
        var body = new StackPanel { Spacing = 2 };
        body.Children.Add(title);
        body.Children.Add(rows);

        var border = new Border
        {
            Margin = new Thickness(0, 0, 40, 4),
            Padding = new Thickness(10, 6, 10, 6),
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            Background = ResolveBrush("CardBackgroundBrush", Brushes.Transparent),
            BorderBrush = ResolveBrush("CardBorderBrush", Brushes.Gray),
            Child = body,
        };
        ToolTip.SetTip(border, Loc.T("main.agent.tip",
            "模型自己决定的每一步：调用哪个工具、参数是什么、返回了什么、耗时多久。按 Esc 可随时停止。"));
        // 界面探针用：按这个 AutomationId 找 Agent 步骤区
        AutomationProperties.SetAutomationId(border, "AgentTrace");
        return (border, rows);
    }

    private void AppendAgentTraceRow(StackPanel rows, string text, string brushKey, bool strong)
    {
        var row = new TextBlock
        {
            // 与消息文本一致地做 emoji 降级（本机没有 emoji 字体时换成纯文本标记）
            Text = Emoji.Prefer(text),
            FontSize = Math.Max(9, _state.Settings.FontSize - 2),
            TextWrapping = TextWrapping.Wrap,
            FontWeight = strong ? FontWeight.SemiBold : FontWeight.Normal,
            Foreground = ResolveBrush(brushKey, ResolveBrush("MutedForegroundBrush", Brushes.Gray)),
        };
        rows.Children.Add(row);
    }

    /// <summary>Agent 步骤区的字号跟随全局字号（Ctrl+滚轮调字号时才不会落下它）。</summary>
    private void ScaleAgentTrace()
    {
        if (_agentRegion is null)
            return;
        double size = Math.Max(9, _state.Settings.FontSize - 2);
        foreach (TextBlock row in EnumerateAgentRows(_agentRegion))
            row.FontSize = size;
    }

    private static IEnumerable<TextBlock> EnumerateAgentRows(Border region)
    {
        if (region.Child is not StackPanel body)
            yield break;
        foreach (object child in body.Children)
        {
            if (child is TextBlock label)
                yield return label;
            else if (child is StackPanel rows)
                foreach (object row in rows.Children)
                    if (row is TextBlock text)
                        yield return text;
        }
    }

    /// <summary>
    /// 工具返回「没权限」而不是「执行失败」的判据。
    ///
    /// 后端的措辞是固定的两句中文（agent_tools.ToolRegistry.run：敏感工具未授权时
    /// 「…当前未启用…」，本版不提供的保护类工具「…当前不可用」），而它就是模型看到的
    /// 原始观察结果、不随界面语言变 —— 所以按原文匹配是稳的。
    /// </summary>
    private static bool IsAgentDeniedObservation(string observation)
        => observation.Contains(AgentDeniedMark, StringComparison.Ordinal)
           // 兼容旧串：v0.2.2 之前后端那两句是写死中文的（旧版后端仍可能这么发）
           || observation.Contains("未启用", StringComparison.Ordinal)
           || observation.Contains("当前不可用", StringComparison.Ordinal);

    /// <summary>
    /// 后端"被拒绝"观察串的**语言无关标记**（v0.2.2 硬编码清查第 6 批）。
    ///
    /// 原先只能按中文原文匹配（见上面注释），但后端那两句本地化后**不再固定是中文**，
    /// 按原文匹配在英文界面下必然失效。现在后端在观察串前加这个 ASCII 标记，
    /// 前端据此判定，显示前剥掉（见 <see cref="OneLineText"/>）。
    /// </summary>
    private const string AgentDeniedMark = "[!DENIED] ";

    /// <summary>把多行文本压成一行并截断：步骤行的参数与工具返回常是多行/很长的 JSON。</summary>
    private static string OneLineText(string text, int max)
    {
        if (string.IsNullOrEmpty(text))
            return "";
        string flat = text.Replace('\r', ' ').Replace('\n', ' ').Trim();
        // 剥掉"被拒绝"标记：它只给判定用，不该显示给用户
        if (flat.StartsWith(AgentDeniedMark, StringComparison.Ordinal))
            flat = flat[AgentDeniedMark.Length..].Trim();
        while (flat.Contains("  ", StringComparison.Ordinal))
            flat = flat.Replace("  ", " ", StringComparison.Ordinal);
        return flat.Length <= max ? flat : flat[..max] + "…";
    }

    // -----------------------------------------------------------------
    // 文件上传
    // -----------------------------------------------------------------

    private async Task PickFilesAsync()
    {
        // 待确认的超限上传：再点一次「上传文件」即继续（Avalonia 没有 MessageBox，
        // 与危险指令、模型下载用的是同一套两段式确认）。
        if (_pendingUpload.Count > 0)
        {
            List<string> confirmed = _pendingUpload;
            _pendingUpload = new List<string>();
            await SendFilesAsync(confirmed, confirmed: true);
            return;
        }

        var files = await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = Loc.T("main.upload.chooseTitle", "选择要上传的文件"),
            AllowMultiple = true,
            // 扩展名过滤器（v0.2.2 第 4 批 3c）：WPF 侧一直有，跨平台版此前是"所有文件"。
            // 与 WPF 同一份清单（doc/docx/xls/xlsx/ppt/pptx/txt/pdf）。
            FileTypeFilter = new[]
            {
                new FilePickerFileType(Loc.T("main.upload.filter", "支持的文件"))
                {
                    Patterns = new[] { "*.doc", "*.docx", "*.xls", "*.xlsx", "*.ppt", "*.pptx", "*.txt", "*.pdf" },
                },
                new FilePickerFileType(Loc.T("main.upload.filterAll", "所有文件"))
                {
                    Patterns = new[] { "*" },
                },
            },
        });
        var paths = files.Select(f => f.TryGetLocalPath()).Where(p => !string.IsNullOrEmpty(p)).Cast<string>().ToList();
        if (paths.Count > 0)
            await SendFilesAsync(paths);
    }

    private void OnDragOver(object? sender, DragEventArgs e)
    {
        // Avalonia 12 的拖放换成了 DataTransfer API（e.Data / DataFormats.Files 已移除）
        e.DragEffects = e.DataTransfer.Contains(DataFormat.File) ? DragDropEffects.Copy : DragDropEffects.None;
        e.Handled = true;
    }

    private async void OnDrop(object? sender, DragEventArgs e)
    {
        try
        {
            var paths = e.DataTransfer.TryGetFiles()?
                .Select(f => f.TryGetLocalPath())
                .Where(p => !string.IsNullOrEmpty(p))
                .Cast<string>()
                .ToList() ?? new List<string>();
            if (paths.Count > 0)
                await SendFilesAsync(paths);
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
        }
    }

    /// <summary>
    /// 上传文件（`file_stream`）：单文件与多文件是同一个动作的两种参数形态。
    ///
    /// v0.2.2 第 4 批 3c 补齐四处（此前只写状态栏一行）：
    ///   * **文件数与总量超限询问**（10 个 / 60MB）：超限先问一次，确认后只处理前 10 个；
    ///   * **逐份上传状态**（`file_status` 的 start / ok / failed）就地更新同一条消息；
    ///   * **长文档分段摘要进度**（`summary_progress`）：替换当前文件那一行，不追加刷屏；
    ///   * **上传期间可中止**：把 `_streamCts` 真正挂上（此前它是 null，点「停止」无效），
    ///     Esc / ⏹ 都能真的停下后端。
    ///
    /// 数量与总量的**规则以后端为准**（`sys.files_too_many` / `sys.files_partial`），
    /// 前端这一套只用于"先问一句"，不自己另算一套结论。
    /// </summary>
    private async Task SendFilesAsync(List<string> paths, bool confirmed = false)
    {
        if (_busy)
        {
            AddMessage("system", Loc.T("main.error.busy", "⏳ 上一条回复仍在生成，请先停止或等待完成"));
            return;
        }
        var list = paths.Where(p => !string.IsNullOrWhiteSpace(p)).ToList();
        if (list.Count == 0)
            return;

        // ---- 超限询问（只问一次；再点一次「上传文件」= 继续）----
        if (!confirmed && list.Count > 1)
        {
            long totalBytes = 0;
            foreach (string path in list)
            {
                try { totalBytes += new FileInfo(path).Length; }
                catch (Exception) { /* 取不到大小就不计入总量 */ }
            }
            bool tooMany = list.Count > MaxUploadCount;
            bool tooLarge = totalBytes > MaxUploadBytes;
            if (tooMany || tooLarge)
            {
                _pendingUpload = list.Take(MaxUploadCount).ToList();
                string reason = tooMany
                    ? Loc.T("main.upload.confirmCount", "一次选择了 {0} 个文件（上限 {1} 个）",
                        list.Count, MaxUploadCount)
                    : Loc.T("main.upload.confirmSize", "所选文件共 {0}（上限 {1}）",
                        FormatBytes(totalBytes), FormatBytes(MaxUploadBytes));
                AddMessage("system", Loc.T("main.upload.confirmContinue",
                    "{0}。\n\n继续会只处理前 {1} 个文件，其余忽略。是否继续？", reason, MaxUploadCount)
                    + Environment.NewLine
                    + Loc.T("main.upload.confirmHint",
                        "再点一次「📎 上传文件」（或按 Ctrl+O）即继续；点别的地方则取消。"));
                Toast(Loc.T("main.upload.confirmTitle", "文件较多"), error: true);
                return;
            }
        }

        AddMessage("user", "📎 " + string.Join("\n", list.Select(Path.GetFileName)));

        // 逐份状态与分段摘要都写进**同一条**系统消息（8 个文件也不会把对话刷满）
        AddMessage("system", list.Count > 1
            ? Loc.T("main.upload.uploadingMany", "📥 正在上传 {0} 个文件…", list.Count)
            : Loc.T("main.upload.uploading", "📤 正在上传文件: {0} ...", Path.GetFileName(list[0])));
        var lines = new List<string>();

        SetBusy(true);
        // 上传期间「发送」按钮的**含义**变成停止（与 WPF 一致），但外观仍是 WPF 的 ➤ 图标：
        // 文字提示放在 ToolTip 里，不在按钮上换字（WPF 版按钮上从来没有文字）。
        ToolTip.SetTip(SendButton, Loc.T("main.button.stopSummaryTip", "中止分段摘要 (Esc)"));
        _streamCts = new CancellationTokenSource();
        // 探针据此断言"上传期间真的可以中止"（此前 _streamCts 是 null，点停停止无效）
        ProbeUploadCancellable = true;
        ProbeUploadStatusLines = 0;
        int okCount = 0, failedCount = 0;
        try
        {
            object request = list.Count == 1
                ? new { action = "file_stream", path = list[0] }
                : new { action = "file_stream", files = list };
            string content = await _state.Backend.SendStreamAsync(
                request,
                onDelta: null,
                // 同上：上下文显示交给 done 行的 ApplyContextFields（那里才有 used_last_request）
                onContext: (_, _) => { },
                idleTimeout: BackendService.HeavyIdleTimeout,
                cancellationToken: _streamCts.Token,
                onEvent: (type, root) => DispatchUploadEvent(type, root, lines,
                    ref okCount, ref failedCount),
                onDeltaChannel: (_, _, _) => { });
            if (content.Length > 0)
                AddMessage("assistant", content);
            else if (list.Count > 1)
                AddMessage("system", Loc.T("main.upload.doneMany",
                    "✅ 已加载 {0} 个文件（失败 {1} 个）", okCount, failedCount));
        }
        catch (OperationCanceledException)
        {
            AddMessage("system", Loc.T("main.upload.cancelled", "⏹ 已中止本次上传"));
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
        }
        finally
        {
            _streamCts?.Dispose();
            _streamCts = null;
            SetBusy(false);
            SendButton.Content = "➤";
            ToolTip.SetTip(SendButton, Loc.T("main.button.sendTip", "发送 (Ctrl+Enter)"));
            await RefreshContextAsync();
        }
    }

    /// <summary>超限上传的待确认列表（再点一次「上传文件」即继续）。</summary>
    private List<string> _pendingUpload = new();

    /// <summary>单次上传的文件数上限（与 WPF 一致）。</summary>
    private const int MaxUploadCount = 10;

    /// <summary>单次上传的总量上限（60 MB，与 WPF 一致）。</summary>
    private const long MaxUploadBytes = 60L * 1024 * 1024;

    private static string FormatBytes(long bytes)
        => bytes >= 1048576 ? $"{bytes / 1048576.0:0.#} MB" : $"{bytes / 1024.0:0.#} KB";

    /// <summary>
    /// 把上传过程中的结构化事件渲染成"逐份状态 + 摘要进度"。
    ///
    /// 两条事件的分工（与 WPF 一致）：
    ///   * `file_status`：一份文件的开始 / 成功 / 失败，**追加**一行；
    ///   * `summary_progress`：长文档分段摘要的进度，**替换最后一行**（不追加，否则刷屏）。
    /// </summary>
    private void DispatchUploadEvent(string type, JsonElement root, List<string> lines,
                                     ref int okCount, ref int failedCount)
    {
        string index = "", total = "";
        if (root.TryGetProperty("index", out JsonElement ix) && ix.ValueKind == JsonValueKind.Number)
            index = ix.GetInt32().ToString();
        if (root.TryGetProperty("total", out JsonElement tl) && tl.ValueKind == JsonValueKind.Number)
            total = tl.GetInt32().ToString();

        if (type == "file_status")
        {
            string name = GetStringField(root, "name");
            string phase = GetStringField(root, "phase");
            if (phase == "start")
            {
                lines.Add(Loc.T("main.upload.fileStart", "⏳ [{0}/{1}] {2} …", index, total, name));
            }
            else if (GetBoolField(root, "ok"))
            {
                okCount++;
                int chars = GetIntField(root, "chars");
                lines.Add(Loc.T("main.upload.fileOk", "✅ [{0}/{1}] {2}（{3} 字符）",
                    index, total, name, chars));
            }
            else
            {
                failedCount++;
                lines.Add(Loc.T("main.upload.fileFailed", "❌ [{0}/{1}] {2} 解析失败",
                    index, total, name));
            }
            RenderUploadStatus(lines);
            return;
        }

        if (type != "summary_progress")
            return;
        string text = GetStringField(root, "content");
        if (text.Length == 0)
            return;
        string prefix = index.Length > 0 && total.Length > 0 ? $"[{index}/{total}] " : "";
        if (lines.Count > 0 && lines[^1].StartsWith('⏳'))
            lines[^1] = prefix + text;
        else
            lines.Add(prefix + text);
        RenderUploadStatus(lines);
    }

    /// <summary>把多行上传状态写进最后一条系统消息（就地更新，不新增气泡）。</summary>
    private void RenderUploadStatus(List<string> lines)
    {
        ProbeUploadStatusLines = lines.Count;
        string text = string.Join(Environment.NewLine, lines);
        Dispatcher.UIThread.Post(() =>
        {
            if (_transcript.Count > 0)
                _transcript[^1] = _transcript[^1] with { Content = Emoji.Prefer(text) };
            // 最后一条视图就是刚加的那条系统消息（AddMessage 之后没有追加别的）
            if (_progressView is not null)
                _progressView.ContentHost.Content = BuildPlainText("system", text);
            else if (MessagePanel.Children.Count > 0
                     && MessagePanel.Children[^1] is Control last)
            {
                ReplaceLastPlainText(last, text);
            }
            ScrollToBottom();
        });
    }

    /// <summary>把一条系统消息视图的正文换成新的纯文本（就地更新用）。</summary>
    private void ReplaceLastPlainText(Control root, string text)
    {
        foreach (Control control in EnumerateDescendants(root))
        {
            if (control is SelectableTextBlock block)
            {
                block.Text = Emoji.Prefer(text);
                return;
            }
        }
    }

    private static IEnumerable<Control> EnumerateDescendants(Control root)
    {
        foreach (Control child in root.GetVisualDescendants().OfType<Control>())
            yield return child;
        if (root is ContentControl { Content: Control content })
            yield return content;
    }

    private static bool GetBoolField(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.True;

    // -----------------------------------------------------------------
    // 探针接口（`EchoDesk --probe upload` 用：上传路径与常用语插入）
    // -----------------------------------------------------------------

    /// <summary>上传期间是否挂上了可取消的 CTS（点「停止」/Esc 能不能真的停下后端）。</summary>
    public bool ProbeUploadCancellable { get; private set; }

    /// <summary>最近一次上传里逐份状态行的条数（断言"逐份状态真的渲染了"）。</summary>
    public int ProbeUploadStatusLines { get; private set; }

    /// <summary>最近一条系统消息的正文（断言上传结果写在了界面上）。</summary>
    public string ProbeLastSystemText()
    {
        for (int i = _transcript.Count - 1; i >= 0; i--)
        {
            if (_transcript[i].Role == "system")
                return _transcript[i].Content;
        }
        return "";
    }

    /// <summary>探针：走一次真实上传（不经过文件选择器）。</summary>
    public async Task ProbeUploadAsync(List<string> paths, bool confirmed = true)
    {
        await SendFilesAsync(paths, confirmed);
    }

    /// <summary>探针：直接设置输入框内容与光标，然后插一条常用语（验"插入到光标处"）。</summary>
    public string ProbeInsertSnippetAtCaret(string current, int caret, string snippetText)
    {
        InputBox.Text = current;
        InputBox.CaretIndex = caret;
        InsertSnippet(new Snippet { Name = "probe", Text = snippetText });
        return InputBox.Text ?? "";
    }

    /// <summary>
    /// 探针：填入一组**演示数据**，供 README 截图使用（`--probe shots`）。
    /// 只走真实的消息渲染路径（AddMessage / MarkdownRenderer / 状态条），不截图临时拼的控件，
    /// 因此截图里的排版、字号、状态区与用户实际看到的完全一致。
    /// </summary>
    public void ProbePrepareShowcase(bool english = false)
    {
        MessagePanel.Children.Clear();
        _transcript.Clear();
        _agentRegion = null;
        _agentRows = null;
        _live = null;

        if (english)
        {
            AddMessage("user", "Summarize the liability clauses in this contract, especially the penalty cap and termination terms.");
            AddMessage("assistant",
                "Parsed **Equipment Purchase Agreement (2026 revision).docx** (18 pages / 21,480 characters) "
                + "and used Contract mode to locate the liability clauses:\n\n"
                + "### Key liability terms\n"
                + "| Clause | Term | Risk |\n"
                + "| --- | --- | --- |\n"
                + "| 8.1 · Late delivery | 0.05% of contract value per day, capped at 10% | Medium |\n"
                + "| 8.3 · Quality defect | Replace within 15 days; 10% of defective value after that | High |\n"
                + "| 9.2 · Unilateral termination | Buyer may terminate, but must reimburse reasonable costs | Low |\n\n"
                + "### Findings\n"
                + "- **Penalty cap is low**: 10% is below the 15%–20% range common for equipment purchases.\n"
                + "- **Quality remedy is incomplete**: clause 8.3 covers replacement only, not line-down losses or third-party inspection fees.\n"
                + "- **Termination favours the buyer**, but “reasonable costs” is undefined; add a calculation basis.\n\n"
                + "### Suggested rewrite (clause 8.1)\n"
                + "> For each day of delay, Seller shall pay 0.05% of the contract value as liquidated damages. "
                + "If cumulative damages reach 20% of the contract value, Buyer may terminate and claim actual losses.\n\n"
                + "Source pointer: Chapter 8 “Liability” · characters 12,840–15,206 ▍",
                "",
                "Output 428 tokens · 18.4 tok/s · first token 0.8s · context 6,144/8,192");

            InputBox.Text = "Raise the clause 8.1 penalty cap to 20% and draft a negotiation note.";
            InputBox.CaretIndex = InputBox.Text.Length;

            VersionText.Text = "v0.2.2";
            StatusText.Text = "⏳ Generating · 428 tokens written";
            ModelText.Text = "🧠 Model: Qwen2.5-7B-Instruct-Q4_K_M";
            ModeText.Text = "MODE CONTRACT";
            ContextChipText.Text = "Context: 6,144/8,192";
            SearchText.Text = "🔎 Search: off";
            AutoText.Text = "🤖 Automation: on";
            MarketText.Text = "🧩 Skills: 12";
            StripContextText.Text = "📚 Context: 6,144/8,192";
            StripKvText.Text = "💾 KV: ~0.34 GB";
            StripCapabilityText.Text = "🖥 Capabilities: WPS ready";
            StripCompactText.Text = "🗜 Compaction: on";
            MetricsText.Text = "⚡ 18.4 tok/s · first token 0.8s";
            HintText.Text = "Enter to send · Shift+Enter for a new line · Ctrl+O to upload";
        }
        else
        {
            AddMessage("user", "帮我总结这份合同的违约责任，重点看违约金上限和解除条件。");
            AddMessage("assistant",
                "已解析 **《设备采购合同（2026 修订版）.docx》**（18 页 / 21,480 字），"
                + "并用合同模式定位到违约责任相关条款：\n\n"
                + "### 违约责任要点\n"
                + "| 条款 | 约定 | 风险 |\n"
                + "| --- | --- | --- |\n"
                + "| 第 8.1 条 · 逾期交付 | 每逾期 1 日按合同总价 0.05% 计违约金，累计上限 10% | 中 |\n"
                + "| 第 8.3 条 · 质量不符 | 乙方 15 日内更换；逾期按不合格品金额 10% 赔偿 | 高 |\n"
                + "| 第 9.2 条 · 单方解除 | 甲方可解除，但须赔偿乙方已发生的合理成本 | 低 |\n\n"
                + "### 关键结论\n"
                + "- **违约金上限偏低**：10% 的上限低于同类设备采购常见的 15%–20%。\n"
                + "- **质量责任缺少兜底**：第 8.3 条只约定更换，未覆盖停线损失与第三方检测费用。\n"
                + "- **解除权对甲方有利**，但“合理成本”未定义，建议补充计价口径。\n\n"
                + "### 建议改写（第 8.1 条）\n"
                + "> 每逾期一日，乙方按合同总价的 0.05% 向甲方支付违约金；累计违约金达到合同总价 20% 时，"
                + "甲方有权解除合同并要求赔偿实际损失。\n\n"
                + "原文指针：第 8 章「违约责任」· 字符区间 12,840–15,206 ▍",
                "",
                "输出 428 tokens · 18.4 tok/s · 首字 0.8s · 上下文 6,144/8,192");

            InputBox.Text = "把第 8.1 条的违约金上限改成 20%，再给我一版谈判口径。";
            InputBox.CaretIndex = InputBox.Text.Length;

            VersionText.Text = "v0.2.2";
            StatusText.Text = "⏳ 正在生成 · 已输出 428 tokens";
            ModelText.Text = "🧠 模型: Qwen2.5-7B-Instruct-Q4_K_M";
            ModeText.Text = "MODE 合同审阅";
            ContextChipText.Text = "上下文：6,144/8,192";
            SearchText.Text = "🔎 搜索：关闭";
            AutoText.Text = "🤖 自动化：已启用";
            MarketText.Text = "🧩 技能市场：12 个";
            StripContextText.Text = "📚 上下文：6,144/8,192";
            StripKvText.Text = "💾 KV：约 0.34 GB";
            StripCapabilityText.Text = "🖥 本机能力：WPS 可用";
            StripCompactText.Text = "🗜 压缩：开启";
            MetricsText.Text = "⚡ 18.4 tok/s · 首字 0.8s";
            HintText.Text = "Enter 发送 · Shift+Enter 换行 · Ctrl+O 上传文档";
        }

        MetricsText.IsVisible = true;
        StopButton.IsVisible = true;
    }

    // -----------------------------------------------------------------
    // 消息渲染
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Starts the coalescing render timer. A single dispatcher timer drives both the
    ///      throttled Markdown refresh and the breathing phase of the streaming caret, therefore
    ///      no second timer competes for the UI thread budget.
    /// [ZH] 启动合并式渲染定时器。由单个调度定时器同时驱动节流后的 Markdown 刷新与流式光标的
    ///      呼吸相位，因此不会有第二个定时器争夺界面线程的时间预算。
    /// </summary>
    private void StartRenderTimer()
    {
        if (_renderTimer is null)
        {
            _renderTimer = new DispatcherTimer { Interval = RenderInterval };
            _renderTimer.Tick += OnRenderTick;
        }
        _caretClock.Restart();
        _renderTimer.Start();
    }

    /// <summary>
    /// [EN] Stops the render timer and performs a final flush so that the last token received
    ///      before completion is always rendered, even when it arrived after the final tick.
    /// [ZH] 停止渲染定时器并执行最终刷新，确保完成之前收到的最后一个 Token 一定被渲染，
    ///      即使它晚于最后一次定时器触发才到达。
    /// </summary>
    private void StopRenderTimer()
    {
        _renderTimer?.Stop();
        if (_renderDirty)
            RenderLive();
        // [EN] The caret belongs to the streaming state only; completion removes it immediately.
        // [ZH] 光标仅属于流式状态；生成完成时立即移除。
        _liveCaret = null;
    }

    private void OnRenderTick(object? sender, EventArgs e)
    {
        // [EN] Opacity is a render-only property, therefore the breathing update never
        //      invalidates measure or arranges a new text layout.
        // [ZH] 不透明度仅影响渲染，因此呼吸更新不会使测量失效，也不会重新安排文本布局。
        UpdateCaretOpacity();
        if (_renderDirty)
            RenderLive();
    }

    private void MarkDirty() => _renderDirty = true;

    /// <summary>
    /// [EN] Appends an answer fragment under the stream gate. The backend reader thread may be
    ///      the caller, so no UI element is touched here; only the shared buffer is extended and
    ///      the render pass is marked as pending.
    /// [ZH] 在流式闸门下追加回答片段。调用方可能是后端读取线程，因此此处不触碰任何界面元素；
    ///      仅扩展共享缓冲区并将渲染过程标记为待执行。
    /// </summary>
    private void AppendAnswerText(string text)
    {
        lock (_streamGate)
            _liveAnswer.Append(text);
        MarkDirty();
    }

    /// <summary>
    /// [EN] Appends a reasoning fragment under the stream gate.
    /// [ZH] 在流式闸门下追加思考片段。
    /// </summary>
    private void AppendReasoningText(string text)
    {
        lock (_streamGate)
            _liveReasoning.Append(text);
    }

    /// <summary>
    /// [EN] Returns a consistent snapshot of a streaming buffer. A snapshot is required because
    ///      the buffer may grow while the UI thread is parsing the previous content.
    /// [ZH] 返回流式缓冲区的一致快照。必须使用快照，因为界面线程解析上一份内容期间缓冲区可能仍在增长。
    /// </summary>
    private string SnapshotAnswer()
    {
        lock (_streamGate)
            return _liveAnswer.ToString();
    }

    private string SnapshotReasoning()
    {
        lock (_streamGate)
            return _liveReasoning.ToString();
    }

    /// <summary>
    /// [EN] Clears both streaming buffers under the gate.
    /// [ZH] 在闸门下清空两个流式缓冲区。
    /// </summary>
    private void ResetStreamBuffers()
    {
        lock (_streamGate)
        {
            _liveAnswer.Clear();
            _liveReasoning.Clear();
        }
        _liveRendered = "";
    }

    /// <summary>
    /// [EN] Creates the caret element for the current font size. The element is two pixels wide
    ///      and one text line high, and it is inserted into the inline flow rather than overlaid,
    ///      so it cannot cover surrounding content.
    /// [ZH] 按当前字号创建光标元素。该元素宽两像素、高度为一行文本，并插入行内流中而非叠加层，
    ///      因此不会遮挡周围内容。
    /// </summary>
    private Control CreateTypingCaret()
    {
        double height = Math.Max(12, _state.Settings.FontSize * 1.15);
        var caret = new Border
        {
            Width = 2,
            Height = height,
            Margin = new Thickness(3, 0, 0, 0),
            CornerRadius = new CornerRadius(1),
            Background = ResolveBrush("AccentBrush", Brushes.Teal),
            VerticalAlignment = VerticalAlignment.Center,
            Opacity = CaretOpacity(),
        };
        AutomationProperties.SetAutomationId(caret, "TypingCaret");
        return caret;
    }

    /// <summary>
    /// [EN] Current breathing opacity of the caret, computed from the elapsed stream time on a
    ///      continuous 1 -> 0 -> 1 cosine cycle. The phase is continuous across re-renders, so
    ///      a recreated caret resumes at the same level instead of restarting.
    /// [ZH] 光标的当前呼吸不透明度，根据已流逝的流式时间在连续的 1 → 0 → 1 余弦周期上计算。
    ///      相位在多次重渲染之间保持连续，因此重建后的光标从同一水平继续，而不会重新开始。
    /// </summary>
    private double CaretOpacity()
    {
        double phase = (_caretClock.ElapsedMilliseconds % CaretBreathMilliseconds) / CaretBreathMilliseconds;
        return 0.5 + 0.5 * Math.Cos(phase * 2 * Math.PI);
    }

    private void UpdateCaretOpacity()
    {
        if (_liveCaret is not null)
            _liveCaret.Opacity = CaretOpacity();
    }

    /// <summary>
    /// [EN] Renders the buffered answer into the live message. The pass runs at most once per
    ///      render interval and skips work when neither the text nor the caret state changed,
    ///      which prevents repeated Markdown parsing from entering a layout feedback loop.
    /// [ZH] 将缓冲的回答渲染进流式消息。该过程每个渲染间隔至多执行一次，并在文本与光标状态均未
    ///      变化时跳过，从而防止重复的 Markdown 解析进入布局反馈循环。
    /// </summary>
    private void RenderLive()
    {
        _renderDirty = false;
        if (_live is null)
            return;
        string answer = SnapshotAnswer();
        if (_liveCaret is not null && string.Equals(answer, _liveRendered, StringComparison.Ordinal))
            return;
        _liveRendered = answer;
        _renderCount++;
        // [EN] A fresh caret instance is created per pass; its opacity is taken from the shared
        //      phase so that the breathing remains continuous.
        // [ZH] 每次渲染创建新的光标实例；其不透明度取自共享相位，因此呼吸保持连续。
        _liveCaret = CreateTypingCaret();
        _live.ContentHost.Content = MarkdownRenderer.Build(
            Emoji.Prefer(answer), _state.Settings.FontSize, _liveCaret);
        ScrollToBottom();
    }


    private MessageView AddMessageView(string role, string content, string reasoning, string metrics = "")
    {
        (Border root, ContentControl host, SelectableTextBlock thinking, Expander thinkingBox, TextBlock metricsText) =
            BuildMessageControls(role, content, reasoning, metrics);
        MessagePanel.Children.Add(root);
        ScrollToBottom();
        return new MessageView
        {
            Root = root,
            ContentHost = host,
            ThinkingText = thinking,
            ThinkingBox = thinkingBox,
            MetricsText = metricsText,
            Content = content,
            Reasoning = reasoning,
            Metrics = metrics,
        };
    }

    private void RemoveLiveMessage()
    {
        if (_live is not null)
        {
            MessagePanel.Children.Remove(_live.Root);
            _live = null;
        }
    }

    private void AddMessage(string role, string content, string reasoning = "", string metrics = "")
    {
        // [EN] Emoji degradation is applied to the message body at the single entry point, before
        //      rendering, so that emoji inside code blocks and tables are degraded as well.
        // [ZH] Emoji 降级在唯一的入口处、渲染之前施加于消息正文，因此代码块与表格内部的 emoji
        //      同样会被降级。
        content = Emoji.Prefer(content);
        reasoning = Emoji.Prefer(reasoning);
        _transcript.Add(new Message(role, content, reasoning, metrics));
        MessageView view = AddMessageView(role, content, reasoning, metrics);
        // [EN] Only assistant messages are rendered as Markdown. User and system messages are
        //      rendered as plain text in the role body colour; the role itself is conveyed by the
        //      header label rather than by an inline icon.
        // [ZH] 仅助手消息按 Markdown 呈现。用户与系统消息以角色正文色呈现为纯文本；角色信息由
        //      标题行标识传达，而不再使用行内图标。
        if (role == "assistant")
            view.ContentHost.Content = MarkdownRenderer.Build(content, _state.Settings.FontSize);
        else
            view.ContentHost.Content = BuildPlainText(role, content);
    }

    /// <summary>整屏重渲染（换字号/换语言时用）：从 _transcript 重建，保证不丢内容。</summary>
    private void RenderTranscript()
    {
        MessagePanel.Children.Clear();
        // Agent 步骤区不在 _transcript 里（它是"过程信息"，与消息正文分开存），
        // 整屏重建会把它从可视树上摘掉。**必须同时把引用清空**：否则流式过程中若刚好
        // 切了字号，后续的步骤行会往一个已经脱离可视树的容器里追加 —— 界面看起来就像
        // "思考到一半 Agent 步骤突然不动了"。
        _agentRegion = null;
        _agentRows = null;
        foreach (Message message in _transcript)
        {
            MessageView view = AddMessageView(message.Role, message.Content, message.Reasoning, message.Metrics);
            view.ContentHost.Content = message.Role == "assistant"
                ? MarkdownRenderer.Build(message.Content, _state.Settings.FontSize)
                : BuildPlainText(message.Role, message.Content);
        }
    }

    // -----------------------------------------------------------------
    // [EN] Probe interface: message layout and streaming throttle
    // [ZH] 探针接口：消息布局与流式节流
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Reports the visual parameters of a freshly constructed user message and assistant
    ///      message. The values allow an unattended probe to assert the flattened layout: the
    ///      user block carries a light background, the assistant body is transparent, the spacing
    ///      below each message is increased, and the action bar starts hidden and excluded from
    ///      hit testing.
    /// [ZH] 报告新构建的用户消息与助手消息的视觉参数。借助这些数值，无人值守探针可以断言平铺式
    ///      布局：用户块带有浅色背景、助手正文透明、每条消息下方间距增大，且操作条初始为隐藏状态
    ///      并排除于命中测试之外。
    /// </summary>
    public string ProbeMessageStyleSnapshot()
    {
        var user = BuildMessageControls("user", "probe", "", "");
        var assistant = BuildMessageControls("assistant", "probe", "", "");
        return Describe(user.Item1) + "\n" + Describe(assistant.Item1);

        static string Describe(Border bubble)
        {
            var stack = bubble.Child as StackPanel;
            StackPanel? bar = null;
            TextBlock? roleLabel = null;
            if (stack is not null)
            {
                foreach (object child in stack.Children)
                {
                    if (child is StackPanel candidate
                        && string.Equals(AutomationProperties.GetAutomationId(candidate), "MessageActions",
                            StringComparison.Ordinal))
                        bar = candidate;
                    if (child is Grid header)
                        roleLabel = header.Children.OfType<TextBlock>().FirstOrDefault();
                }
            }
            // [EN] The reveal state is exercised through the same helper that the pointer
            //      handlers invoke, then restored, so the idle values remain those of a message
            //      that has not been hovered.
            // [ZH] 浮现状态通过指针处理器调用的同一辅助方法演练，随后复原，因此静息值仍为
            //      尚未悬停的消息所应有的取值。
            string hover = "n/a";
            if (bar is not null)
            {
                SetActionBarVisible(bar, true);
                hover = $"{bar.Opacity:0.##}/{bar.IsHitTestVisible}";
                SetActionBarVisible(bar, false);
            }
            return $"bg={bubble.Background}｜padLeft={bubble.Padding.Left:0.#}"
                   + $"｜marginBottom={bubble.Margin.Bottom:0.#}"
                   + $"｜barOpacity={bar?.Opacity:0.##}｜barHit={bar?.IsHitTestVisible}"
                   + $"｜barHover={hover}"
                   + $"｜barTransitions={bar?.Transitions?.Count ?? 0}"
                   + $"｜roleFont={(roleLabel?.FontSize ?? 0):0.#}";
        }
    }

    /// <summary>
    /// [EN] Exercises the streaming pipeline without a backend connection. A high-frequency token
    ///      burst is pushed into the shared buffer while the coalescing timer runs, then a final
    ///      fragment is appended after the last tick and the forced flush is verified to contain
    ///      it. The report also states whether the caret exists during streaming and whether it
    ///      has disappeared once the turn is completed.
    /// [ZH] 在不连接后端的前提下演练流式管线。在合并式定时器运行期间向共享缓冲区写入高频 Token
    ///      突发，随后在最后一次定时器触发之后追加最后一个片段，并验证强制刷新结果包含该片段。
    ///      报告同时说明光标在流式期间是否存在、以及本轮结束后是否已经消失。
    /// </summary>
    public async Task<string> ProbeStreamingAsync(int tokenCount)
    {
        ResetStreamBuffers();
        _live = AddMessageView("assistant", "", "");
        StartRenderTimer();
        _renderCount = 0;
        for (int i = 0; i < tokenCount; i++)
        {
            AppendAnswerText($"tok{i} ");
            if (i % 200 == 199)
                await Task.Delay(40);
        }
        bool caretDuring = _liveCaret is not null && FindByAutomationId(_live.Root, "TypingCaret") is not null;
        // [EN] The final fragment deliberately arrives after the last timer tick.
        // [ZH] 最后一个片段刻意在最后一次定时器触发之后到达。
        AppendAnswerText("final-token");
        StopRenderTimer();
        string rendered = FlattenContentText(_live.ContentHost);
        bool tailPresent = rendered.Contains("final-token", StringComparison.Ordinal);
        RemoveLiveMessage();
        AddMessage("assistant", rendered);
        bool caretAfter = FindByAutomationId(MessagePanel, "TypingCaret") is not null;
        return $"tokens={tokenCount}｜renders={_renderCount}｜interval={RenderInterval.TotalMilliseconds:0}"
               + $"｜tailPresent={tailPresent}｜caretDuring={caretDuring}｜caretAfter={caretAfter}";
    }

    /// <summary>
    /// [EN] Flattens the rendered text of a control tree, including inline runs, so that a probe
    ///      can verify that the rendered content equals the buffered content.
    /// [ZH] 将控件树的已渲染文本（含行内片段）拼接为单一字符串，使探针能够验证渲染内容与缓冲区
    ///      内容一致。
    /// </summary>
    private static string FlattenContentText(Control? root)
    {
        var builder = new StringBuilder();
        Collect(root);
        return builder.ToString();

        void Collect(Control? node)
        {
            switch (node)
            {
                case TextBlock block:
                    if (!string.IsNullOrEmpty(block.Text))
                    {
                        builder.Append(block.Text);
                    }
                    else if (block.Inlines is not null)
                    {
                        foreach (Inline inline in block.Inlines)
                            CollectInline(inline);
                    }
                    break;
                case Panel panel:
                    foreach (Control child in panel.Children)
                        Collect(child);
                    break;
                case Decorator { Child: Control decorated }:
                    Collect(decorated);
                    break;
                case ContentControl { Content: Control content }:
                    Collect(content);
                    break;
            }
        }

        void CollectInline(Inline inline)
        {
            switch (inline)
            {
                case Run run:
                    builder.Append(run.Text);
                    break;
                case Span { Inlines: not null } span:
                    foreach (Inline child in span.Inlines)
                        CollectInline(child);
                    break;
            }
        }
    }

    /// <summary>
    /// [EN] Finds a control by automation identifier without relying on the visual tree, so that
    ///      the lookup also works before the window has been laid out.
    /// [ZH] 依据自动化标识查找控件，且不依赖可视化树，因此该查找在窗口完成布局之前同样有效。
    /// </summary>
    private static Control? FindByAutomationId(Control root, string automationId)
    {
        if (string.Equals(AutomationProperties.GetAutomationId(root), automationId, StringComparison.Ordinal))
            return root;
        foreach (Control child in EnumerateChildControls(root))
        {
            Control? found = FindByAutomationId(child, automationId);
            if (found is not null)
                return found;
        }
        return null;
    }

    private static IEnumerable<Control> EnumerateChildControls(Control node)
    {
        switch (node)
        {
            case Panel panel:
                foreach (Control child in panel.Children)
                    yield return child;
                break;
            case Decorator { Child: Control decorated }:
                yield return decorated;
                break;
            case ContentControl { Content: Control content }:
                yield return content;
                break;
            case TextBlock { Inlines: not null } block:
                foreach (Inline inline in block.Inlines)
                {
                    if (inline is InlineUIContainer { Child: Control inlineChild })
                        yield return inlineChild;
                }
                break;
        }
    }

    /// <summary>
    /// [EN] Role body colour: the counterpart of UserBrush, AssistantBrush and SystemBrush.
    /// [ZH] 角色正文色：与 UserBrush、AssistantBrush 及 SystemBrush 相对应。
    /// </summary>
    private static string RoleBrushKey(string role) => role switch
    {
        "user" => "UserBrush",
        "assistant" => "AssistantBrush",
        _ => "SystemBrush",
    };

    /// <summary>
    /// [EN] Renders plain text for user and system messages. The role is conveyed by the header
    ///      label rather than by an inline icon, which keeps the flattened layout free of
    ///      decorative prefixes.
    /// [ZH] 呈现用户与系统消息的纯文本。角色信息由标题行的标识传达，而不再使用行内图标，
    ///      从而使平铺式布局不带有装饰性前缀。
    /// </summary>
    private Control BuildPlainText(string role, string content)
    {
        var selectable = new SelectableTextBlock
        {
            Text = Emoji.Prefer(content),
            TextWrapping = TextWrapping.Wrap,
            FontSize = _state.Settings.FontSize,
            // WPF 用 ThemeService 把整段文字的 Foreground 绑到角色色上
            Foreground = ResolveBrush(RoleBrushKey(role), Brushes.Black),
        };
        return selectable;
    }

    /// <summary>
    /// [EN] Builds the flattened message container. Assistant output is rendered directly on the
    ///      window surface, while a user message is presented as a light block indented from the
    ///      right edge. The action bar remains in the layout flow beneath the body and is revealed
    ///      on pointer entry, therefore it neither covers the text nor intercepts input while it
    ///      is hidden.
    /// [ZH] 构建平铺式消息容器。助手输出直接呈现于窗口表面，用户消息则以浅色块的形式自右缘缩进
    ///      展示。操作条始终位于正文下方的布局流中，并在指针进入时浮现，因此既不会覆盖文本，
    ///      也不会在隐藏期间拦截输入。
    /// </summary>
    private (Border, ContentControl, SelectableTextBlock, Expander, TextBlock) BuildMessageControls(
        string role, string content, string reasoning, string metrics)
    {
        // [EN] Probe anchors are preserved unchanged: AnswerBody, ReasoningRegion and RunMetrics.
        // [ZH] 界面探针锚点保持不变：AnswerBody、ReasoningRegion 与 RunMetrics。
        var contentHost = new ContentControl();
        AutomationProperties.SetAutomationId(contentHost, "AnswerBody");
        bool isUser = string.Equals(role, "user", StringComparison.Ordinal);

        var root = new StackPanel { Spacing = 6 };
        root.Children.Add(BuildMessageHeader(role));

        var thinkingText = new SelectableTextBlock
        {
            Text = reasoning,
            TextWrapping = TextWrapping.Wrap,
            FontSize = _state.Settings.FontSize - 1,
            Opacity = 0.8,
        };
        // [EN] Character count of the reasoning region: it allows the length to be judged while
        //      the region is collapsed.
        // [ZH] 思考区字数标签：在折叠状态下仍可判断该段内容的篇幅。
        var thinkingHeader = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        var thinkingTitle = new TextBlock
        {
            Text = Loc.T("main.reasoning.title", "🧠 思考过程"),
            VerticalAlignment = VerticalAlignment.Center,
        };
        var thinkingCount = new TextBlock
        {
            Text = reasoning.Length > 0 ? Loc.T("main.reasoning.count", "· {0} 字", reasoning.Length) : "",
            FontSize = _state.Settings.FontSize - 3,
            Opacity = 0.6,
            VerticalAlignment = VerticalAlignment.Center,
        };
        thinkingHeader.Children.Add(thinkingTitle);
        thinkingHeader.Children.Add(thinkingCount);

        var thinkingBox = new Expander
        {
            Header = thinkingHeader,
            Content = new ScrollViewer
            {
                MaxHeight = 220,
                Content = thinkingText,
            },
            // [EN] The region expands automatically while streaming and collapses once the turn
            //      is complete.
            // [ZH] 该区域在流式期间自动展开，并在本轮完成后自动收起。
            IsExpanded = role == "assistant" && reasoning.Length > 0,
            IsVisible = reasoning.Length > 0,
        };
        ToolTip.SetTip(thinkingBox, Loc.T("main.reasoning.tip",
            "模型的推理过程（思考内容）。点击展开或收起；思考内容不会作为上下文发给模型，也不参与复制回答。"));
        root.Children.Add(thinkingBox);

        root.Children.Add(contentHost);

        var metricsText = new TextBlock
        {
            Text = metrics,
            FontSize = _state.Settings.FontSize - 3,
            Opacity = 0.6,
            TextWrapping = TextWrapping.Wrap,
            IsVisible = metrics.Length > 0,
        };
        AutomationProperties.SetAutomationId(metricsText, "RunMetrics");
        AutomationProperties.SetAutomationId(thinkingBox, "ReasoningRegion");
        root.Children.Add(metricsText);

        // [EN] Action bar with the four established copy operations: copy, Markdown, code and
        //      select all. It is aligned to the bottom-right corner of the message.
        // [ZH] 操作条包含四项既有复制操作：复制、Markdown、代码与全选。其对齐方式为消息
        //      右下角。
        var actionBar = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(0, 2, 0, 0),
            Opacity = 0,
            IsHitTestVisible = false,
            Transitions = new Transitions
            {
                new DoubleTransition
                {
                    Property = OpacityProperty,
                    Duration = TimeSpan.FromMilliseconds(160),
                    Easing = new CubicEaseOut(),
                },
            },
        };
        AutomationProperties.SetAutomationId(actionBar, "MessageActions");
        AddCopyButton(actionBar, Loc.T("main.message.copy", "📋 复制"),
            Loc.T("main.message.copyPlainTip", "复制为纯文本"),
            async () => await CopyTextAsync(StripCodeFenceForPlain(content)));
        // [EN] Only assistant messages expose the Markdown and code operations, because user and
        //      system messages have no Markdown source to copy. The operation count and position
        //      therefore depend on the role.
        // [ZH] 仅助手消息提供 Markdown 与代码两项操作，因为用户与系统消息没有可供复制的
        //      Markdown 源文本。因此操作项的数量与位置随角色而定。
        if (role == "assistant")
        {
            AddCopyButton(actionBar, Loc.T("main.message.copyMarkdown", "⧉ Markdown"),
                Loc.T("main.message.copyMarkdownTip", "复制原始 Markdown 源码"),
                async () => await CopyTextAsync(content),
                Loc.T("main.message.copiedMarkdown", "已复制 Markdown 源码"));
            AddCopyButton(actionBar, Loc.T("main.message.copyCode", "</> 代码"),
                Loc.T("main.message.copyCodeTip", "复制全部代码块"),
                async () => await CopyTextAsync(ExtractCodeBlocks(content)),
                Loc.T("main.message.copiedCode", "已复制代码块"));
        }
        AddCopyButton(actionBar, Loc.T("main.message.selectAll", "全选"),
            Loc.T("main.message.selectAllTip", "选中本条消息全部文本"),
            () => { SelectAllIn(contentHost); return Task.CompletedTask; });
        root.Children.Add(actionBar);

        // [EN] The container is transparent for assistant and system messages, so the body is
        //      painted directly on the window surface. The transparent background is retained
        //      because it makes the whole block a pointer hit-test target.
        // [ZH] 助手与系统消息的容器为透明，正文因此直接绘制于窗口表面。保留透明背景是因为
        //      它使整个区块成为指针命中测试的目标。
        var bubble = new Border
        {
            Background = isUser
                ? ResolveBrush("SurfaceAltBrush", Brushes.WhiteSmoke)
                : Brushes.Transparent,
            BorderThickness = new Thickness(0),
            CornerRadius = new CornerRadius(10),
            Padding = isUser ? new Thickness(14, 10, 14, 10) : new Thickness(0),
            Margin = new Thickness(isUser ? 64 : 0, 4, 0, 18),
            HorizontalAlignment = isUser ? HorizontalAlignment.Right : HorizontalAlignment.Stretch,
            Child = root,
        };
        if (isUser)
            bubble.MaxWidth = 720;
        AutomationProperties.SetAutomationId(bubble, "MessageRoot");

        // [EN] Hover reveal: the bar fades in when the pointer enters the message block and fades
        //      out when it leaves. The handlers are attached to the container itself, therefore
        //      moving the pointer onto the revealed buttons does not trigger a leave event.
        // [ZH] 悬停浮现：指针进入消息区块时操作条淡入，离开时淡出。处理器挂接在容器本身上，
        //      因此指针移动到已浮现的按钮上不会触发离开事件。
        bubble.PointerEntered += (_, _) => SetActionBarVisible(actionBar, true);
        bubble.PointerExited += (_, _) => SetActionBarVisible(actionBar, false);

        return (bubble, contentHost, thinkingText, thinkingBox, metricsText);
    }

    /// <summary>
    /// [EN] Builds the role header: a small accent-coloured role label on the left and the
    ///      timestamp on the right. The single compact row separates the roles without competing
    ///      with the body typography.
    /// [ZH] 构建角色标题行：左侧为小号强调色角色标识，右侧为时间戳。该行紧凑且唯一，
    ///      在不与正文排版争夺注意力的前提下区分角色。
    /// </summary>
    private Grid BuildMessageHeader(string role)
    {
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto") };
        var label = new TextBlock
        {
            Text = RoleLabel(role),
            FontSize = Math.Max(10, _state.Settings.FontSize - 3),
            FontWeight = FontWeight.SemiBold,
            Foreground = ResolveBrush("AccentBrush", Brushes.Teal),
            VerticalAlignment = VerticalAlignment.Center,
        };
        AutomationProperties.SetAutomationId(label, "RoleLabel");
        var stamp = new TextBlock
        {
            Text = DateTime.Now.ToString("HH:mm:ss"),
            FontSize = 10,
            Foreground = ResolveBrush("SubTextBrush", Brushes.Gray),
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(label, 0);
        Grid.SetColumn(stamp, 2);
        header.Children.Add(label);
        header.Children.Add(stamp);
        return header;
    }

    /// <summary>
    /// [EN] Shows or hides the action bar without altering the layout. While hidden the bar is
    ///      excluded from hit testing, therefore the pointer passes through to the message body
    ///      and text selection remains unaffected.
    /// [ZH] 在不改变布局的前提下显示或隐藏操作条。隐藏期间该条不参与命中测试，
    ///      因此指针可穿透至消息正文，文本选择不受影响。
    /// </summary>
    private static void SetActionBarVisible(StackPanel bar, bool visible)
    {
        bar.Opacity = visible ? 1 : 0;
        bar.IsHitTestVisible = visible;
    }

    /// <summary>
    /// [EN] Returns the localised role label displayed in the message header.
    /// [ZH] 返回消息标题行中显示的角色标识文案。
    /// </summary>
    private static string RoleLabel(string role) => role switch
    {
        "user" => Loc.T("main.message.user", "我"),
        "assistant" => Loc.T("main.message.assistant", "助手"),
        _ => Loc.T("main.message.system", "系统"),
    };

    private void ScrollToBottom() => MessageScroll.ScrollToEnd();

    // -----------------------------------------------------------------
    // 复制操作条（v0.2.2 第 3 批）
    // -----------------------------------------------------------------

    /// <summary>往操作条上加一个复制类按钮（统一字号、内边距与失败提示）。</summary>
    private void AddCopyButton(StackPanel bar, string label, string tip,
                               Func<Task> action, string? successToast = null)
    {
        // 尺寸照抄 WPF 的 MakeActionButton：10 号字、内边距 6,1、左边距 4、胶囊圆角 17。
        var button = new Button
        {
            Content = label,
            FontSize = 10,
            Padding = new Thickness(6, 1, 6, 1),
            Margin = new Thickness(4, 0, 0, 0),
            CornerRadius = new CornerRadius(17),
        };
        ToolTip.SetTip(button, tip);
        button.Click += async (_, _) =>
        {
            await action();
            if (successToast is not null)
                Toast(successToast);
        };
        bar.Children.Add(button);
    }

    /// <summary>写剪贴板。空内容给一句提示，剪贴板被占用时静默（与 WPF 同样处理）。</summary>
    private async Task CopyTextAsync(string text)
    {
        if (text.Length == 0)
        {
            Toast(Loc.T("main.message.nothingToCopy", "⚠️ 没有可复制的内容"), error: true);
            return;
        }
        if (TopLevel.GetTopLevel(this)?.Clipboard is { } clipboard)
        {
            try
            {
                await clipboard.SetTextAsync(text);
            }
            catch (Exception)
            {
                // 剪贴板被别的进程占用：不打断用户，也不假装成功
            }
        }
    }

    /// <summary>
    /// 纯文本口径：把 Markdown 代码围栏（```）去掉，只留内容。
    /// 「复制」按钮给的是**可读文本**，「Markdown」按钮才是带语法的原文 —— 两者必须有区别，
    /// 否则多出来的按钮没有意义。
    /// </summary>
    private static string StripCodeFenceForPlain(string content)
    {
        var builder = new StringBuilder();
        foreach (string line in content.Replace("\r\n", "\n").Split('\n'))
        {
            if (line.TrimStart().StartsWith("```", StringComparison.Ordinal))
                continue;
            builder.AppendLine(line);
        }
        return builder.ToString().Trim();
    }

    /// <summary>把全部代码块拼成一段（「代码」按钮）。没有代码块时返回空串。</summary>
    private static string ExtractCodeBlocks(string content)
    {
        var blocks = new List<string>();
        var current = new StringBuilder();
        bool inside = false;
        foreach (string line in content.Replace("\r\n", "\n").Split('\n'))
        {
            if (line.TrimStart().StartsWith("```", StringComparison.Ordinal))
            {
                if (inside)
                {
                    string block = current.ToString().TrimEnd();
                    if (block.Length > 0)
                        blocks.Add(block);
                    current.Clear();
                }
                inside = !inside;
                continue;
            }
            if (inside)
                current.AppendLine(line);
        }
        if (inside && current.Length > 0)
            blocks.Add(current.ToString().TrimEnd());   // 未闭合的代码块也要收（流式中常见）
        return string.Join(Environment.NewLine + Environment.NewLine, blocks);
    }

    /// <summary>「全选」：把消息正文里所有可选文本控件选中。</summary>
    private static void SelectAllIn(Control root)
    {
        foreach (SelectableTextBlock block in EnumerateSelectable(root))
            block.SelectAll();
    }

    private static IEnumerable<SelectableTextBlock> EnumerateSelectable(Control root)
    {
        var stack = new Stack<Control>();
        stack.Push(root);
        while (stack.Count > 0)
        {
            Control current = stack.Pop();
            if (current is SelectableTextBlock selectable)
                yield return selectable;
            foreach (Control child in current.GetVisualDescendants().OfType<Control>())
                stack.Push(child);
            // ContentControl 的内容不在可视树里时（刚构造还没布局）也要能找到
            if (current is ContentControl { Content: Control content })
                stack.Push(content);
        }
    }

    /// <summary>
    /// 复制全部对话为纯文本（Ctrl+Shift+C，对齐 WPF 的 <c>CopyAllMessages</c>）。
    /// 用 <c>_transcript</c> 而不是遍历可视树：可视树里还有 Agent 步骤区、占位文本等
    /// 不属于"对话"的东西。
    /// </summary>
    private void CopyAllMessages()
    {
        if (_transcript.Count == 0)
        {
            Toast(Loc.T("main.message.nothingToCopy", "⚠️ 没有可复制的内容"), error: true);
            return;
        }
        var builder = new StringBuilder();
        foreach (Message message in _transcript)
        {
            string who = message.Role switch
            {
                "user" => Loc.T("main.message.user", "我"),
                "assistant" => Loc.T("main.message.assistant", "助手"),
                _ => Loc.T("main.message.system", "系统"),
            };
            builder.AppendLine($"[{who}] {message.Content}");
            builder.AppendLine();
        }
        _ = CopyTextAsync(builder.ToString().TrimEnd());
        Toast(Loc.T("main.message.copiedAll", "已复制全部对话"));
    }

    /// <summary>
    /// 取界面语义色画刷。**必须用 TryFindResource，不能 <c>(IBrush?)FindResource(key)</c>**：
    /// 这些画刷放在 App.axaml 的 ThemeDictionaries 里，取不到时 <c>FindResource</c> 返回的是
    /// <c>AvaloniaProperty.UnsetValue</c>（类型 UnsetValueType），它不是 IBrush —— 强转直接抛
    /// InvalidCastException。后果不是"少一个颜色"，而是**第一条消息就把整个前端打断**
    /// （实测：后端不可用时的横幅消息就足以让进程退出）。同一文件里 Toast 早就用了
    /// TryFindResource，这里跟上，取不到就退回兜底色。
    /// </summary>
    private static IBrush ResolveBrush(string key, IBrush fallback)
    {
        if (Application.Current is { } app)
        {
            // [EN] The theme variant is supplied explicitly because the application requests
            //      ThemeVariant.Default while the palette is stored in the Light and Dark theme
            //      dictionaries. The overload without a variant does not resolve those entries.
            // [ZH] 由于应用程序请求 ThemeVariant.Default，而色板存放于 Light 与 Dark 两个主题字典中，
            //      此处显式提供主题变体。不带变体的重载无法解析这些条目。
            if (app.TryFindResource(key, app.ActualThemeVariant, out object? value) && value is IBrush brush)
                return brush;
            if (app.TryFindResource(key, out value) && value is IBrush fallbackBrush)
                return fallbackBrush;
        }
        return fallback;
    }

    private void SetStatus(string text) => StatusText.Text = Emoji.Prefer(text);

    private static string Trim(string text, int limit)
    {
        if (string.IsNullOrEmpty(text))
            return "";
        string flat = text.Replace('\n', ' ').Replace('\r', ' ');
        return flat.Length <= limit ? flat : flat[..limit] + "…";
    }

    private void OnClosing(object? sender, WindowClosingEventArgs e)
    {
        SaveWindowPlacement();
        try
        {
            _streamCts?.Cancel();
        }
        catch (ObjectDisposedException)
        {
        }
    }

    // -----------------------------------------------------------------
    // 模型下载确认 / 退出倒计时 / 后端进度活消息（v0.2.2 第 3 批）
    // -----------------------------------------------------------------

    /// <summary>
    /// `/model` 需要一个还没下载的模型时，后端会回 <c>action = "confirm_model"</c>，
    /// 由前端征得同意后再发下载动作（对齐 WPF 的 <c>ConfirmModelDownloadAsync</c>）。
    ///
    /// 同意的方式：Avalonia 没有 MessageBox，这里用**状态区的一次显式确认**——
    /// 第二次按 Enter 才会真的下载（与危险指令的二次确认同一套做法），
    /// 并同时打印一句"怎么确认"的系统消息，避免用户不知道要按第二次。
    /// </summary>
    private void HandleConfirmModel(string modelName)
    {
        if (string.IsNullOrEmpty(modelName))
            return;
        if (!string.Equals(_pendingModelDownload, modelName, StringComparison.Ordinal))
        {
            _pendingModelDownload = modelName;
            AddMessage("system", Loc.T("main.model.downloadQuestion",
                "确定要下载模型 '{0}' 吗？", modelName) + Environment.NewLine
                + Loc.T("main.dialog.modelDownloadText",
                    "{0}\n\n是否现在下载？下载需要联网且可能耗时较长。",
                    Loc.T("main.model.downloadConfirmHint",
                        "再按一次 Enter（或再点一次「发送」）即开始下载；输入别的内容则取消。")));
            Toast(Loc.T("main.model.waitingConfirm", "⏳ 等待确认下载：再发送一次空消息即可开始"));
            return;
        }
        _pendingModelDownload = "";
        _ = DownloadModelAsync(modelName);
    }

    private async Task DownloadModelAsync(string modelName)
    {
        AddMessage("system", Loc.T("main.model.downloadingNamed",
            "⏳ 正在下载并加载模型 '{0}'，请稍候...", modelName));
        SetStatus(Loc.T("main.status.downloadingModel", "正在下载模型……"));
        try
        {
            // 流式：进度写进一条**活消息**（同一条系统气泡就地更新），而不是刷屏。
            StartProgressMessage();
            string final = await _state.Backend.SendStreamAsync(
                new { action = "model_download", name = modelName },
                onDelta: delta => Dispatcher.UIThread.Post(() => UpdateProgressMessage(delta)),
                onContext: (_, _) => { },
                idleTimeout: TimeSpan.FromMinutes(30));
            EndProgressMessage(final);
            Toast(Loc.T("main.model.downloadDone", "✅ 模型下载完成：{0}", modelName));
            await RefreshStatusAsync();
        }
        catch (Exception ex)
        {
            EndProgressMessage(Loc.T("main.model.operationFailed", "❌ 模型操作失败: {0}", ex.Message));
            Toast(Loc.T("main.model.downloadFailed", "❌ 模型下载失败：{0}", ex.Message), error: true);
        }
        SetStatus(_state.Backend.ModelReady
            ? Loc.T("main.status.ready", "就绪")
            : Loc.T("main.status.noModel", "后端已就绪，但没有加载模型"));
    }

    /// <summary>
    /// `/exit`：致谢 + 10 秒倒计时后自动关闭（对齐 WPF 的 <c>BeginExitCountdown</c>）。
    /// 倒计时期间禁用输入，避免用户以为还能继续提问。
    /// </summary>
    private void BeginExitCountdown(string thanksMessage)
    {
        AddMessage("system", thanksMessage);
        SetBusy(false);
        CloseAllPalettes();
        InputBox.IsEnabled = false;
        SendButton.IsEnabled = false;
        _exitCountdown = 10;
        SetStatus(Loc.T("main.status.exitCountdown", "程序将在 {0} 秒后自动关闭", _exitCountdown));
        _exitTimer?.Stop();
        _exitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _exitTimer.Tick += (_, _) =>
        {
            _exitCountdown--;
            if (_exitCountdown <= 0)
            {
                _exitTimer?.Stop();
                Close();
                return;
            }
            SetStatus(Loc.T("main.status.exitCountdown", "程序将在 {0} 秒后自动关闭", _exitCountdown));
        };
        _exitTimer.Start();
    }

    // ---- 进度"活消息"：同一条系统气泡就地更新，而不是每来一行就加一条 ----

    private MessageView? _progressView;

    private void StartProgressMessage()
    {
        _transcript.Add(new Message("system", Loc.T("main.progress.starting", "⏳ 正在开始…"), "", ""));
        _progressView = AddMessageView("system", "", "", "");
    }

    private void UpdateProgressMessage(string text)
    {
        if (_progressView is null || string.IsNullOrWhiteSpace(text))
            return;
        string trimmed = text.Trim();
        _progressView.ContentHost.Content = BuildPlainText("system", trimmed);
        // 同时更新最后一条 transcript，整屏重渲染（切字号/语言）时不会丢进度
        if (_transcript.Count > 0)
            _transcript[^1] = _transcript[^1] with { Content = trimmed };
        SetStatus(Trim(trimmed, 120));
    }

    private void EndProgressMessage(string final)
    {
        if (_progressView is not null)
        {
            MessageView view = _progressView;
            view.ContentHost.Content = BuildPlainText("system", final);
            if (_transcript.Count > 0)
                _transcript[^1] = _transcript[^1] with { Content = final };
            _progressView = null;
        }
        else if (!string.IsNullOrEmpty(final))
        {
            AddMessage("system", final);
        }
    }

    // -----------------------------------------------------------------
    // 运行指标定时刷新（v0.2.2 第 3 批，对齐 WPF 的 StartMetricsTimer）
    // -----------------------------------------------------------------

    private void StartMetricsTimer()
    {
        _metricsTimer ??= new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
        _metricsTimer.Tick -= MetricsTimerTick;
        _metricsTimer.Tick += MetricsTimerTick;
        _metricsStoppedState = false;
        _metricsTimer.Start();
    }

    private async void MetricsTimerTick(object? sender, EventArgs e)
    {
        if (!_busy || _metricsTimer is null)
        {
            _metricsTimer?.Stop();
            return;
        }
        // 后端在生成过程中会把最新指标挂在 context 响应上；拿不到就保持上一次的值
        // （**不清空**：清空会让指标行一闪一闪的，比"稍旧一点"更难看）。
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "context" });
        if (doc is null)
            return;
        using (doc)
        {
            if (doc.RootElement.TryGetProperty("metrics", out JsonElement metrics)
                && metrics.ValueKind == JsonValueKind.Object)
            {
                ShowMetrics(FormatMetrics(metrics, _metricsStoppedState));
            }
        }
    }

    private void StopMetricsTimer() => _metricsTimer?.Stop();
}







