// 主窗口（任务一原型 / 任务二第一个迁移模块）。
//
// 迁移策略：**事件处理逻辑照搬 WPF 版**（发送、流式双通道、停止、文件上传、状态刷新），
// 换掉的只有界面 API（WPF 的 RichTextBox/FlowDocument → Avalonia 的 TextBlock/控件树、
// 字体缩放从 FlowDocument 的 FontSize 变成逐控件 FontSize）。
//
// 与原型的验证目标对应，这个窗口要能证明：
//   · 主窗口 + 消息区 + 输入区在 Avalonia 下能跑（真窗口，非截图）
//   · 流式增量渲染不闪、不卡（节流重渲染）
//   · 思考内容折叠区能实时填充（v0.2.1 的双通道协议在跨平台版照样工作）
//   · 文件上传（按钮 + 拖拽）走同一套协议
//   · 字号缩放 / 主题切换 / 语言切换在 Avalonia 下可重写实现
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
using Avalonia.Controls;
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
        AvaloniaXamlLoader.Load(this);
        _state.Initialize();

        Width = _state.Settings.WindowWidth;
        Height = _state.Settings.WindowHeight;

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
        CapabilityButton.Click += async (_, _) => await ShowCapabilityAsync();
        LangCombo.SelectionChanged += (_, _) =>
        {
            if (LangCombo.SelectedItem is LangItem item)
                _state.SetLanguage(item.Tag);
        };
        InputBox.KeyDown += OnInputKeyDown;
        AddHandler(DragDrop.DragOverEvent, OnDragOver);
        AddHandler(DragDrop.DropEvent, OnDrop);
    }

    private void OnInputKeyDown(object? sender, KeyEventArgs e)
    {
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
        PlatformChip.Text = $"🖥 {_state.Platform.DisplayName}";
        ModelChip.Text = _state.Backend.ModelReady
            ? Loc.T("main.model.chip", "🧠 模型: {0}", string.IsNullOrEmpty(_state.Backend.LoadedModel) ? "local" : _state.Backend.LoadedModel)
            : Loc.T("main.model.none", "🧠 无模型");
        UploadButton.Content = Loc.T("main.button.upload", "📎 上传文件");
        SendButton.Content = Loc.T("main.button.send", "发送");
        StopButton.Content = Loc.T("main.button.stop", "⏹ 停止");
        HintText.Text = Loc.T("main.hint", "💡 拖拽文件可上传 · Ctrl+Enter 发送 · Esc 停止");
        InputBox.PlaceholderText = Loc.T("main.input.placeholder", "输入消息，Ctrl+Enter 发送 · 拖拽文件到窗口即可上传");
        ThemeButton.Content = _state.Settings.Theme switch
        {
            "light" => "☀",
            "dark" => "🌙",
            _ => "🌓",
        };
        if (string.IsNullOrEmpty(ContextChip.Text) || ContextChip.Text == "---")
            ContextChip.Text = Loc.T("main.context.buttonIdle", "上下文：---/---");
        ShowCapabilityBanner();
    }

    private void ApplyFontSize()
    {
        InputBox.FontSize = _state.Settings.FontSize + 1;
        // 消息区字号：逐条重建（Avalonia 没有 FlowDocument 那种"整篇一个字号"的容器）
        RenderTranscript();
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
        await RefreshContextAsync();
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

    private async Task RefreshContextAsync()
    {
        JsonDocument? doc = await _state.Backend.SendAsync(new { action = "context" });
        if (doc is null)
            return;
        using (doc)
        {
            if (doc.RootElement.TryGetProperty("used", out JsonElement used)
                && doc.RootElement.TryGetProperty("limit", out JsonElement limit)
                && used.ValueKind == JsonValueKind.Number
                && limit.ValueKind == JsonValueKind.Number)
                ContextChip.Text = Loc.T("main.context.button", "上下文：{0}/{1}", used.GetInt32(), limit.GetInt32());
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
                    ContextChip.Text = Loc.T("main.context.button", "上下文：{0}/{1}", used, limit)),
                idleTimeout: null,   // 流式不设空闲超时（后端可能在等待很长的生成）
                cancellationToken: _streamCts.Token,
                onEvent: (type, root) =>
                {
                    switch (type)
                    {
                        case "progress":
                        {
                            // 心跳：说明"还在算"，避免界面看起来像卡死
                            string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                            Dispatcher.UIThread.Post(() => SetStatus(Trim(Emoji.Prefer(content), 120)));
                            break;
                        }
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
                                metrics = FormatMetrics(stats);
                            if (root.TryGetProperty("reasoning_unclosed", out JsonElement unclosed)
                                && unclosed.ValueKind == JsonValueKind.True)
                                metrics = (metrics + "  " + Loc.T("main.metrics.reasoningUnclosed", "⚠️ 思考未闭合（token 预算可能被思考用完）")).Trim();
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
            await RefreshContextAsync();
        }
    }

    private static string FormatMetrics(JsonElement stats)
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
        if (stats.TryGetProperty("stopped", out JsonElement stopped) && stopped.ValueKind == JsonValueKind.True)
            parts.Add(Loc.T("main.metrics.stopped", "⏹ 已中止"));
        return string.Join(" · ", parts);
    }

    private static string FormatMs(int ms) => ms >= 1000 ? $"{ms / 1000.0:0.#}s" : $"{ms}ms";

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
                    ContextChip.Text = Loc.T("main.context.button", "上下文：{0}/{1}", used, limit)),
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
                    // 剪贴板被占用：静默（原型阶段）
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
            Background = (IBrush?)Application.Current?.FindResource(
                system ? "SystemBubbleBrush" : user ? "UserBubbleBrush" : "AssistantBubbleBrush")
                ?? Brushes.Transparent,
            BorderBrush = (IBrush?)Application.Current?.FindResource("CardBorderBrush") ?? Brushes.Gray,
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
