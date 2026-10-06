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
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Input.Platform;
using Avalonia.Layout;
using Avalonia.Markup.Xaml;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using EchoDesk.Avalonia.Services;
using EchoDesk.Avalonia.Utilities;

namespace EchoDesk.Avalonia.Views;

public partial class MainWindow : Window
{
    private readonly AppState _state = App.State;
    private readonly SnippetStore _snippets = new();

    /// <summary>已定稿的消息（用于换字号/换语言时整屏重渲染）。</summary>
    private readonly List<Message> _transcript = new();

    private CancellationTokenSource? _streamCts;
    private bool _busy;

    // 流式中：文本缓冲 + 节流重渲染
    private readonly StringBuilder _liveAnswer = new();
    private readonly StringBuilder _liveReasoning = new();
    private MessageView? _live;
    private DispatcherTimer? _renderTimer;
    private bool _renderDirty;

    // 指令面板 / 常用语面板
    private TextBox? _paletteSource;
    private bool _commandLike;
    private bool _commandRowOpen;
    private string _pendingDestructive = "";

    // 通知条 / 崩溃提示
    private DispatcherTimer? _toastTimer;

    // 状态区
    private List<string> _capabilityWarnings = new();
    private string _updateUrl = "";
    private bool _updateAvailable;

    // Agent 步骤区（本轮迁移的功能面板之一）
    private Border? _agentRegion;
    private StackPanel? _agentRows;

    // 运行指标：由后端上报（done.stats / progress.stats），前端不估算
    private string _stripMetrics = "";

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
        Opened += async (_, _) => await StartBackendAsync();
    }

    // -----------------------------------------------------------------
    // 初始化与文案
    // -----------------------------------------------------------------

    private void WireEvents()
    {
        SendButton.Click += async (_, _) => await SendAsync();
        StopButton.Click += async (_, _) => await StopAsync();
        UploadButton.Click += async (_, _) => await PickFilesAsync();
        SettingsButton.Click += async (_, _) => await OpenSettingsAsync("settings.tab.general");
        ModelChip.Click += async (_, _) => await OpenSettingsAsync("settings.tab.model");
        ModeChip.Click += async (_, _) => await OpenSettingsAsync("settings.tab.general");
        FontDownButton.Click += (_, _) => _state.SetFontSize(_state.Settings.FontSize - 1);
        FontUpButton.Click += (_, _) => _state.SetFontSize(_state.Settings.FontSize + 1);
        ThemeButton.Click += (_, _) =>
        {
            string next = _state.Settings.Theme switch
            {
                "auto" => "light",
                "light" => "dark",
                _ => "auto",
            };
            _state.SetTheme(next);
            ApplyTexts();
        };
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
    }

    private void OnInputKeyDown(object? sender, KeyEventArgs e)
    {
        // 指令面板打开时优先吃掉方向键/Enter/Esc/Tab（与 WPF 版同一优先级）
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
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
        PoweredText.Text = Loc.T("main.powered", "Powered by Void Entropia Studio");
        PlatformChip.Text = $"🖥 {_state.Platform.DisplayName}";
        ModelText.Text = _state.Backend.ModelReady
            ? Loc.T("main.model.chip", "🧠 模型: {0}", string.IsNullOrEmpty(_state.Backend.LoadedModel) ? "local" : _state.Backend.LoadedModel)
            : Loc.T("main.model.noModel", "🧠 模型: 无模型");
        UploadButton.Content = Loc.T("main.button.upload", "📎 上传文件");
        SendButton.Content = Loc.T("main.button.send", "发送");
        StopButton.Content = Loc.T("main.button.stop", "⏹ 停止");
        HintText.Text = Loc.T("main.hint", "💡 拖拽文件可上传 · Ctrl+Enter 发送 · Esc 停止 · A-/A+ 或 Ctrl+滚轮 调字号");
        InputBox.PlaceholderText = Loc.T("main.input.placeholder", "输入消息，Ctrl+Enter 发送 · 拖拽文件到窗口即可上传");
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
        ToolTip.SetTip(StripWarnButton, Loc.T("main.strip.warnTip", "点击查看全部能力提示"));
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
        if (string.IsNullOrEmpty(ContextChip.Text))
            ContextChip.Text = Loc.T("main.context.buttonIdle", "上下文：---/---");
        ShowCapabilityBanner();
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
                GuideBanner.IsVisible = true;
                GuideText.Text = Emoji.Prefer(_state.Backend.ModelGuide);
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

    /// <summary>刷新状态区（搜索 / KV / 能力 / 模式 / 模型名）。</summary>
    private async Task RefreshStatusAsync()
    {
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "status" });
        if (doc is null)
            return;
        using (doc)
            ApplyStatusFields(doc.RootElement);
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
        ContextChip.Text = Loc.T("main.context.button", "上下文：{0}/{1}", used, limit);
        int percent = (int)Math.Round(used * 100.0 / limit);
        StripContextText.Text = Loc.T("main.strip.context", "📊 上下文 {0}%", percent);
        ToolTip.SetTip(StripContextButton, Loc.T("main.strip.contextHelp",
            "上下文已用 {0} / {1} tokens（{2}%）。点击刷新。", used, limit, percent));
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

    /// <summary>把一条指令发给后端（走非流式 chat 动作，与 WPF 版的 SendCommandAsync 一致）。</summary>
    private async Task SendCommandAsync(string command)
    {
        if (_busy)
        {
            Toast(Loc.T("main.error.busy", "⏳ 上一条回复仍在生成，请先停止或等待完成"), error: true);
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
                if (content.Length > 0)
                    AddMessage("assistant", content);
                ApplyStatusFields(doc.RootElement);
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

    /// <summary>点选一条常用语 → 填进输入框（不直接发送，用户可以再补充）。</summary>
    private void InsertSnippet(Snippet snippet)
    {
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
        InputBox.Text = snippet.Text;
        InputBox.CaretIndex = InputBox.Text.Length;
        InputBox.Focus();
        Toast(Loc.T("main.snippet.inserted", "已填入常用语「{0}」，可直接编辑后发送", snippet.Name));
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

    /// <summary>流式对话（chat_stream）：双通道 delta + 结构化事件 + 可中止。</summary>
    private async Task StreamChatAsync(string text)
    {
        _liveAnswer.Clear();
        _liveReasoning.Clear();
        _live = AddMessageView("assistant", "", "");
        StartRenderTimer();
        _streamCts = new CancellationTokenSource();

        string finalText = "";
        string metrics = "";
        try
        {
            finalText = await _state.Backend.SendStreamAsync(
                new { action = "chat_stream", message = text },
                onDelta: delta =>
                {
                    _liveAnswer.Append(delta);
                    MarkDirty();
                },
                onContext: (used, limit) => Dispatcher.UIThread.Post(() =>
                {
                    if (limit > 0)
                    {
                        ContextChip.Text = Loc.T("main.context.button", "上下文：{0}/{1}", used, limit);
                        int percent = (int)Math.Round(used * 100.0 / limit);
                        StripContextText.Text = Loc.T("main.strip.context", "📊 上下文 {0}%", percent);
                    }
                }),
                idleTimeout: null,   // 流式不设空闲超时（后端可能在等待很长的生成）
                cancellationToken: _streamCts.Token,
                onEvent: (type, root) =>
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
                                if (reasoning.Length > 0 && _liveReasoning.Length == 0)
                                {
                                    _liveReasoning.Append(reasoning);
                                    Dispatcher.UIThread.Post(() =>
                                    {
                                        if (_live is not null)
                                        {
                                            _live.ThinkingText.Text = _liveReasoning.ToString();
                                            _live.ThinkingBox.IsVisible = true;
                                        }
                                    });
                                }
                            }
                            if (root.TryGetProperty("stats", out JsonElement stats) && stats.ValueKind == JsonValueKind.Object)
                            {
                                bool stopped = root.TryGetProperty("stopped", out JsonElement st)
                                               && st.ValueKind == JsonValueKind.True;
                                metrics = FormatMetrics(stats, stopped);
                                Dispatcher.UIThread.Post(() => ShowMetrics(metrics));
                            }
                            if (root.TryGetProperty("reasoning_unclosed", out JsonElement unclosed)
                                && unclosed.ValueKind == JsonValueKind.True)
                                metrics = (metrics + "  " + Loc.T("main.metrics.reasoningUnclosed", "⚠️ 思考未闭合（token 预算可能被思考用完）")).Trim();
                            HandleAgentSummary(root);
                            break;
                        }
                    }
                },
                onDeltaChannel: (channel, content, reset) => Dispatcher.UIThread.Post(() =>
                {
                    if (channel == "reasoning")
                    {
                        _liveReasoning.Append(content);
                        if (_live is not null)
                        {
                            _live.ThinkingText.Text = _liveReasoning.ToString();
                            _live.ThinkingBox.IsVisible = true;
                        }
                    }
                    else
                    {
                        if (reset)
                        {
                            // 作废回答区：此前发的内容其实属于思考（把已累积的回答搬进思考区）
                            _liveReasoning.Insert(0, _liveAnswer.ToString());
                            _liveAnswer.Clear();
                            if (_live is not null)
                            {
                                _live.ThinkingText.Text = _liveReasoning.ToString();
                                _live.ThinkingBox.IsVisible = true;
                            }
                        }
                        _liveAnswer.Append(content);
                        MarkDirty();
                    }
                }));
        }
        catch (OperationCanceledException)
        {
            finalText = _liveAnswer.ToString();
            metrics = Loc.T("main.metrics.stopped", "⏹ 已中止");
            if (_live is not null)
                _live.Metrics = metrics;
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
            finalText = _liveAnswer.ToString();
        }
        finally
        {
            StopRenderTimer();
            string answer = finalText.Length > 0 ? finalText : _liveAnswer.ToString();
            string reasoning = _liveReasoning.ToString();
            RemoveLiveMessage();
            AddMessage("assistant", answer, reasoning, metrics);
            _streamCts?.Dispose();
            _streamCts = null;
            _agentRegion = null;
            _agentRows = null;
            await RefreshContextAsync();
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

    /// <summary>状态区的运行指标（后端实测上报，前端不做估算）。</summary>
    private void ShowMetrics(string text)
    {
        _stripMetrics = text ?? "";
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
        => observation.Contains("未启用", StringComparison.Ordinal)
           || observation.Contains("当前不可用", StringComparison.Ordinal);

    /// <summary>把多行文本压成一行并截断：步骤行的参数与工具返回常是多行/很长的 JSON。</summary>
    private static string OneLineText(string text, int max)
    {
        if (string.IsNullOrEmpty(text))
            return "";
        string flat = text.Replace('\r', ' ').Replace('\n', ' ').Trim();
        while (flat.Contains("  ", StringComparison.Ordinal))
            flat = flat.Replace("  ", " ", StringComparison.Ordinal);
        return flat.Length <= max ? flat : flat[..max] + "…";
    }

    // -----------------------------------------------------------------
    // 文件上传
    // -----------------------------------------------------------------

    private async Task PickFilesAsync()
    {
        var files = await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = Loc.T("main.button.upload", "📎 上传文件"),
            AllowMultiple = true,
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

    /// <summary>上传文件（file_stream）：单文件与多文件是同一个动作的两种参数形态。</summary>
    private async Task SendFilesAsync(List<string> paths)
    {
        if (_busy)
        {
            AddMessage("system", Loc.T("main.error.busy", "⏳ 上一条回复仍在生成，请先停止或等待完成"));
            return;
        }
        AddMessage("user", "📎 " + string.Join("\n", paths.Select(Path.GetFileName)));
        SetBusy(true);
        try
        {
            object request = paths.Count == 1
                ? new { action = "file_stream", path = paths[0] }
                : new { action = "file_stream", files = paths };
            string content = await _state.Backend.SendStreamAsync(
                request,
                onDelta: null,
                onContext: (used, limit) => Dispatcher.UIThread.Post(() =>
                {
                    if (limit > 0)
                        ContextChip.Text = Loc.T("main.context.button", "上下文：{0}/{1}", used, limit);
                }),
                idleTimeout: BackendService.HeavyIdleTimeout,
                onEvent: (type, root) =>
                {
                    if (type == "file_status")
                    {
                        string name = root.TryGetProperty("name", out JsonElement n) ? n.GetString() ?? "" : "";
                        string phase = root.TryGetProperty("phase", out JsonElement p) ? p.GetString() ?? "" : "";
                        Dispatcher.UIThread.Post(() => SetStatus($"{name} {phase}".Trim()));
                    }
                });
            AddMessage("assistant", content);
        }
        catch (Exception ex)
        {
            AddMessage("system", Loc.T("main.error.requestFailed", "❌ 请求失败: {0}", ex.Message));
        }
        finally
        {
            SetBusy(false);
            await RefreshContextAsync();
        }
    }

    // -----------------------------------------------------------------
    // 消息渲染
    // -----------------------------------------------------------------

    private void StartRenderTimer()
    {
        _renderTimer ??= new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(120) };
        _renderTimer.Tick -= OnRenderTick;
        _renderTimer.Tick += OnRenderTick;
        _renderTimer.Start();
    }

    private void StopRenderTimer()
    {
        if (_renderTimer is not null)
            _renderTimer.Stop();
        if (_renderDirty)
            RenderLive();
    }

    private void OnRenderTick(object? sender, EventArgs e)
    {
        if (_renderDirty)
            RenderLive();
    }

    private void MarkDirty() => _renderDirty = true;

    /// <summary>把流式缓冲渲染进当前气泡（节流后调用，不在每个 token 上做）。</summary>
    private void RenderLive()
    {
        _renderDirty = false;
        if (_live is null)
            return;
        _live.ContentHost.Content = MarkdownRenderer.Build(_liveAnswer.ToString(), _state.Settings.FontSize);
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
        _transcript.Add(new Message(role, content, reasoning, metrics));
        MessageView view = AddMessageView(role, content, reasoning, metrics);
        if (role == "assistant" || role == "user")
            view.ContentHost.Content = MarkdownRenderer.Build(content, _state.Settings.FontSize);
        else
            view.ContentHost.Content = BuildPlainText(content);
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
            view.ContentHost.Content = message.Role == "system"
                ? BuildPlainText(message.Content)
                : MarkdownRenderer.Build(message.Content, _state.Settings.FontSize);
        }
    }

    private Control BuildPlainText(string content)
    {
        var selectable = new SelectableTextBlock
        {
            Text = Emoji.Prefer(content),
            TextWrapping = TextWrapping.Wrap,
            FontSize = _state.Settings.FontSize,
        };
        return selectable;
    }

    private (Border, ContentControl, SelectableTextBlock, Expander, TextBlock) BuildMessageControls(
        string role, string content, string reasoning, string metrics)
    {
        bool user = role == "user";
        bool system = role == "system";

        var header = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
            Margin = new Thickness(0, 0, 0, 4),
        };
        header.Children.Add(new TextBlock
        {
            Text = role switch
            {
                "user" => Loc.T("main.message.user", "我"),
                "assistant" => Loc.T("main.message.assistant", "助手"),
                _ => Loc.T("main.message.system", "系统"),
            },
            FontWeight = FontWeight.SemiBold,
            FontSize = _state.Settings.FontSize - 2,
            Opacity = 0.7,
            VerticalAlignment = VerticalAlignment.Center,
        });

        var copy = new Button
        {
            Content = Loc.T("main.message.copy", "复制"),
            FontSize = _state.Settings.FontSize - 3,
            Padding = new Thickness(6, 0, 6, 0),
        };
        copy.Click += async (_, _) =>
        {
            if (TopLevel.GetTopLevel(copy)?.Clipboard is { } clipboard && content.Length > 0)
            {
                try
                {
                    await clipboard.SetTextAsync(content);
                }
                catch (Exception)
                {
                    // 剪贴板被占用：静默（与 WPF 版同样处理）
                }
            }
        };
        header.Children.Add(copy);

        var thinkingText = new SelectableTextBlock
        {
            Text = reasoning,
            TextWrapping = TextWrapping.Wrap,
            FontSize = _state.Settings.FontSize - 1,
            Opacity = 0.8,
        };
        var thinkingBox = new Expander
        {
            Header = Loc.T("main.thinking.title", "🧠 思考过程"),
            Content = new ScrollViewer
            {
                MaxHeight = 220,
                Content = thinkingText,
            },
            IsExpanded = false,
            IsVisible = reasoning.Length > 0,
        };

        var contentHost = new ContentControl();
        var metricsText = new TextBlock
        {
            Text = metrics,
            FontSize = _state.Settings.FontSize - 3,
            Opacity = 0.6,
            TextWrapping = TextWrapping.Wrap,
            IsVisible = metrics.Length > 0,
        };

        var body = new StackPanel { Spacing = 4 };
        body.Children.Add(header);
        body.Children.Add(thinkingBox);
        body.Children.Add(contentHost);
        body.Children.Add(metricsText);

        var bubble = new Border
        {
            Background = ResolveBrush(
                system ? "SystemBubbleBrush" : user ? "UserBubbleBrush" : "AssistantBubbleBrush",
                Brushes.Transparent),
            BorderBrush = ResolveBrush("CardBorderBrush", Brushes.Gray),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(12, 8, 12, 8),
            MaxWidth = system ? double.PositiveInfinity : 720,
            HorizontalAlignment = user ? HorizontalAlignment.Right : HorizontalAlignment.Left,
            Child = body,
        };
        if (!system)
            bubble.Margin = user ? new Thickness(60, 0, 0, 0) : new Thickness(0, 0, 60, 0);

        return (bubble, contentHost, thinkingText, thinkingBox, metricsText);
    }

    private void ScrollToBottom() => MessageScroll.ScrollToEnd();

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
        if (Application.Current is { } app && app.TryFindResource(key, out object? value) && value is IBrush brush)
            return brush;
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
        _state.Settings.WindowWidth = Width;
        _state.Settings.WindowHeight = Height;
        _state.Settings.Save();
        try
        {
            _streamCts?.Cancel();
        }
        catch (ObjectDisposedException)
        {
        }
    }
}
