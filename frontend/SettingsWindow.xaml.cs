using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using EchoDesk.Services;

namespace EchoDesk;

/// <summary>技能市场列表项（卡片数据绑定用）。</summary>
public sealed class MarketItem
{
    public string FullName { get; init; } = "";
    public string Name { get; init; } = "";
    public string Description { get; init; } = "";
    public string Badge { get; init; } = "";
    public string Stars { get; init; } = "";
    public string Meta { get; init; } = "";
    public string Version { get; init; } = "";
    public string HtmlUrl { get; init; } = "";
    public bool Verified { get; init; }
    public bool Installed { get; init; }
    public string ActionText => Installed ? "卸载" : (Verified ? "安装" : "未验证");
    public bool CanAct => Installed || Verified;
}

/// <summary>
/// EchoDesk 设置窗口：常规 / 自动化 / 技能市场 三个 Tab。
///
/// 与后端通过既有 JSON 行协议通信（automation_config、automation_config_set、
/// automation_test、automation_run、market_* 等动作）。主题与字号变化通过回调
/// 通知主窗口即时生效。
/// </summary>
public partial class SettingsWindow : Window
{
    /// <summary>安全协议文本（版本号在协议变更时必须递增，并会记入审计日志）。</summary>
    private const string SafetyVersion = "1.0";

    private const string SafetyBody =
        "1. 自动化功能仅在本机运行，操作过程使用有头浏览器，你可随时观察；\n" +
        "2. EchoDesk 不会把任务内容、下载文件或生成结果上传到任何服务器；\n" +
        "3. 所有下载文件保存在系统临时目录，处理完成后立即删除；\n" +
        "4. 你需要自行确认该自动化操作符合所在组织的规章制度与目标网站的服务条款；\n" +
        "5. 因违规使用导致的后果由使用者自行承担，Void Entropia Studio 不承担责任。";

    private static readonly (string Key, string Label)[] Modes =
    {
        ("general", "1. general — 通用助手"),
        ("summary", "2. summary — 摘要专家"),
        ("translate", "3. translate — 翻译专家"),
        ("code", "4. code — 编程专家"),
        ("contract", "5. contract — 合同审查"),
        ("creative", "6. creative — 创意写作"),
    };

    private readonly BackendService _backend;
    private readonly AppSettings _uiState;
    private readonly Action<bool>? _onThemeChanged;
    private readonly Action<double>? _onFontSizeChanged;
    private readonly List<MarketItem> _marketItems = new();
    private readonly DispatcherTimer _searchDebounce;
    // 窗口关闭时取消所有在途请求，避免回调落到已销毁的控件上
    private readonly CancellationTokenSource _closing = new();

    /// <summary>
    /// InitializeComponent() 是否已完成。
    /// 崩溃修复（v0.2.0 修复一）：XAML 解析 <c>Slider Minimum</c> 时会把 Value 从 0 强制为
    /// 最小值并触发 ValueChanged，而那时 FontSizeLabel 等控件尚未创建。此前的处理器直接访问
    /// 控件导致 NullReferenceException，整个前端进程被终止（表现为「设置窗口卡死」）。
    /// 所有由 XAML 挂接的处理器都必须先检查该标志。
    /// </summary>
    private bool _uiReady;

    private bool _loading = true;
    private bool _automationRunning;
    private int _marketPage = 1;

    public SettingsWindow(
        BackendService backend,
        AppSettings uiState,
        bool isDark,
        double fontSize,
        Action<bool>? onThemeChanged,
        Action<double>? onFontSizeChanged)
    {
        InitializeComponent();
        _backend = backend;
        _uiState = uiState;
        _onThemeChanged = onThemeChanged;
        _onFontSizeChanged = onFontSizeChanged;

        SafetyText.Text = SafetyBody;

        foreach (var (key, label) in Modes)
            DefaultModeCombo.Items.Add(new ComboBoxItem { Content = label, Tag = key });

        DarkModeCheck.IsChecked = isDark;
        ConfirmDestructiveCheck.IsChecked = _uiState.ConfirmDestructiveCommands;
        FontSizeSlider.Value = fontSize;
        FontSizeLabel.Text = $"{fontSize:0.#} pt";
        PathsText.Text =
            $"程序目录：{ShortcutService.ProgramDirectory}\n" +
            "后端目录（配置 / models / 会话 / 自动化日志）：程序目录\\backend\n" +
            "技能安装目录：程序目录\\backend\\skills";

        MarketSortCombo.Items.Add(new ComboBoxItem { Content = "最佳匹配", Tag = "best" });
        MarketSortCombo.Items.Add(new ComboBoxItem { Content = "Star 最多", Tag = "stars" });
        MarketSortCombo.Items.Add(new ComboBoxItem { Content = "最近更新", Tag = "updated" });
        MarketSortCombo.SelectedIndex = 0;

        _searchDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(300) };
        _searchDebounce.Tick += (_, _) =>
        {
            _searchDebounce.Stop();
            _marketPage = 1;
            _ = SearchMarketAsync();
        };

        // 先让窗口完成构建与绘制，再异步取数据：窗口打开时间与后端无关（要求 < 500ms）。
        _uiReady = true;
        ContentRendered += SettingsWindow_ContentRendered;
        Closed += (_, _) =>
        {
            _searchDebounce.Stop();
            try
            {
                _closing.Cancel();
            }
            catch (ObjectDisposedException)
            {
            }
        };
    }

    // ---------- 加载 ----------

    /// <summary>窗口首次绘制完成后再加载数据：保证「打开即见、可交互、再填内容」。</summary>
    private async void SettingsWindow_ContentRendered(object? sender, EventArgs e)
    {
        ContentRendered -= SettingsWindow_ContentRendered;  // 只执行一次
        try
        {
            // 让出一次消息循环，确保窗口先真正显示出来
            await Dispatcher.Yield(DispatcherPriority.Background);
            await LoadInitialAsync();
        }
        catch (OperationCanceledException)
        {
            // 窗口已关闭
        }
        catch (Exception ex)
        {
            SetLoadState($"⚠️ 初始化失败：{ex.Message}");
            App.Log("SettingsWindow.LoadInitial", ex);
        }
    }

    private async Task LoadInitialAsync()
    {
        SetLoadState("正在读取配置…");

        string? configJson = await RequestWithRetryAsync(
            new { action = "automation_config" }, "读取自动化配置", TimeSpan.FromSeconds(90));
        if (configJson is not null)
        {
            try
            {
                using JsonDocument doc = JsonDocument.Parse(configJson);
                if (doc.RootElement.TryGetProperty("automation", out JsonElement el))
                    ApplyAutomation(el);
            }
            catch (JsonException ex)
            {
                SetLoadState($"⚠️ 自动化配置解析失败：{ex.Message}");
            }
        }

        string? modeJson = await RequestWithRetryAsync(
            new { action = "chat", message = "/mode" }, "读取当前对话模式", TimeSpan.FromSeconds(60));
        if (modeJson is not null)
        {
            try
            {
                using JsonDocument doc = JsonDocument.Parse(modeJson);
                string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
                // 形如「当前模式: general（/mode list 查看全部）」
                bool matched = false;
                foreach (var (key, _) in Modes)
                {
                    if (content.Contains($": {key}", StringComparison.OrdinalIgnoreCase))
                    {
                        SelectMode(key);
                        matched = true;
                        break;
                    }
                }
                if (!matched)
                    SelectMode("general");
            }
            catch (JsonException)
            {
                SelectMode("general");
            }
        }

        _loading = false;
        MarketStatusText.Text = "技能市场：点「刷新」浏览 topic 为 echodesk-plugin 的技能，或输入关键词搜索。";
        SetLoadState("✅ 配置已载入");
    }

    /// <summary>
    /// 底部加载状态文本：始终保留一行，加载中显示进度、完成后显示结果。
    /// 这样「数据加载期间可交互」在界面上是可见的，而不是空着一片让人以为卡死。
    /// </summary>
    private void SetLoadState(string text)
    {
        if (LoadStateText is null)
            return;
        LoadStateText.Text = text;
    }

    /// <summary>
    /// 带「后端忙」重试的请求：设置窗口可能在主窗口流式生成或自动化运行期间打开，
    /// 此时请求闸门会被长时间占用。这里显示等待状态并重试，而不是卡住或直接报错。
    /// 任何请求都有阈值，不会无限等待。
    /// </summary>
    private async Task<string?> RequestWithRetryAsync(
        object request, string label, TimeSpan total, TimeSpan? idleTimeout = null)
    {
        var clock = Stopwatch.StartNew();
        int waits = 0;
        while (!_closing.IsCancellationRequested && clock.Elapsed < total)
        {
            try
            {
                return await _backend.SendRequestAsync(
                    request, idleTimeout: idleTimeout, cancellationToken: _closing.Token);
            }
            catch (BackendBusyException)
            {
                waits++;
                SetLoadState($"⏳ {label}：后端正在忙（第 {waits} 次等待，最长 {total.TotalSeconds:0} 秒）…");
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(2), _closing.Token);
                }
                catch (OperationCanceledException)
                {
                    return null;
                }
            }
            catch (BackendTimeoutException ex)
            {
                SetLoadState($"⚠️ {label}超时：{ex.Message}");
                return null;
            }
            catch (OperationCanceledException)
            {
                return null;  // 窗口已关闭
            }
        }
        if (!_closing.IsCancellationRequested)
            SetLoadState($"⚠️ {label}未完成（后端长时间忙碌）。窗口可正常使用，稍后点「保存设置」会重试。");
        return null;
    }

    private void ApplyAutomation(JsonElement el)
    {
        SafetyCheck.IsChecked = GetBool(el, "safety_accepted");
        AutomationEnabledCheck.IsChecked = GetBool(el, "enabled");
        TaskUrlBox.Text = GetString(el, "task_list_url");
        TaskSelectorBox.Text = GetString(el, "task_list_selector");
        TaskRowBox.Text = GetString(el, "task_row_selector");
        NextPageBox.Text = GetString(el, "next_page_selector");
        FileSelectorBox.Text = GetString(el, "task_detail_file_selector");
        ExtensionsBox.Text = string.Join(", ", GetStringList(el, "file_extensions"));
        InstructionBox.Text = GetString(el, "instruction");
        MaxTasksBox.Text = GetInt(el, "max_tasks", 20).ToString();
        TimeoutBox.Text = GetInt(el, "page_timeout_ms", 30000).ToString();
        RetriesBox.Text = GetInt(el, "retries", 1).ToString();
        DedupeCheck.IsChecked = GetBool(el, "dedupe");
        SaveResultsCheck.IsChecked = GetBool(el, "save_results");
        ScheduleCheck.IsChecked = GetBool(el, "schedule_enabled");
        ScheduleTimesBox.Text = string.Join(", ", GetStringList(el, "schedule_times"));
        SyncSafetyUi();
        SyncScheduleTimer();
    }

    private void SelectMode(string key)
    {
        for (int i = 0; i < DefaultModeCombo.Items.Count; i++)
        {
            if (DefaultModeCombo.Items[i] is ComboBoxItem item && Equals(item.Tag, key))
            {
                DefaultModeCombo.SelectedIndex = i;
                return;
            }
        }
    }

    // ---------- 常规 ----------

    private void DarkModeCheck_Changed(object sender, RoutedEventArgs e)
    {
        if (!_uiReady || _loading)
            return;
        _onThemeChanged?.Invoke(DarkModeCheck.IsChecked == true);
    }

    private void FontSizeSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        // XAML 解析 Minimum="10" 时 Value 会从 0 被强制为 10 并触发本回调，
        // 而此时 FontSizeLabel 尚未由 InitializeComponent 创建 → 必须判空。
        if (FontSizeLabel is null)
            return;
        FontSizeLabel.Text = $"{e.NewValue:0.#} pt";
        if (!_uiReady || _loading)
            return;
        _onFontSizeChanged?.Invoke(e.NewValue);
    }

    /// <summary>危险指令确认开关：立即写入 ui_state.json 并即时生效（主窗口每次执行前读取）。</summary>
    private void ConfirmDestructiveCheck_Changed(object sender, RoutedEventArgs e)
    {
        if (!_uiReady)
            return;
        _uiState.ConfirmDestructiveCommands = ConfirmDestructiveCheck.IsChecked == true;
        _uiState.Save();
        SaveHintText.Text = _uiState.ConfirmDestructiveCommands
            ? "已开启指令执行确认"
            : "已关闭指令执行确认（有副作用指令将直接执行）";
    }

    // ---------- 自动化：安全协议联动 ----------

    private void SafetyCheck_Changed(object sender, RoutedEventArgs e)
    {
        if (!_uiReady)
            return;
        SyncSafetyUi();
    }

    /// <summary>安全协议未勾选时禁用自动化开关并给出说明（与后端的强制校验一致）。</summary>
    private void SyncSafetyUi()
    {
        bool accepted = SafetyCheck.IsChecked == true;
        AutomationEnabledCheck.IsEnabled = accepted;
        if (!accepted)
        {
            AutomationEnabledCheck.IsChecked = false;
            AutomationEnabledCheck.Content = "启用自动化功能（请先勾选上方安全协议）";
        }
        else
        {
            AutomationEnabledCheck.Content = "启用自动化功能（运行前仍会要求安全协议已勾选）";
        }
    }

    private void SyncScheduleTimer()
    {
        // 定时触发由后端的 schedule 线程负责；前端只负责保存配置并给出说明。
        // 这里保留一个统一入口，便于后续接入「下次执行时间」的展示。
    }

    // ---------- 自动化：保存 ----------

    private async void SaveButton_Click(object sender, RoutedEventArgs e)
    {
        // 常规设置中的默认模式走指令通道（与 /mode 行为一致）
        string? mode = (DefaultModeCombo.SelectedItem as ComboBoxItem)?.Tag as string;
        if (!string.IsNullOrEmpty(mode))
        {
            try
            {
                await _backend.SendRequestAsync(new { action = "chat", message = $"/mode {mode}" });
            }
            catch
            {
                // 模式切换失败不阻塞自动化配置保存
            }
        }

        var patch = new Dictionary<string, object?>
        {
            ["safety_accepted"] = SafetyCheck.IsChecked == true,
            ["safety_version"] = SafetyVersion,
            ["enabled"] = AutomationEnabledCheck.IsChecked == true,
            ["task_list_url"] = TaskUrlBox.Text.Trim(),
            ["task_list_selector"] = TaskSelectorBox.Text.Trim(),
            ["task_row_selector"] = TaskRowBox.Text.Trim(),
            ["next_page_selector"] = NextPageBox.Text.Trim(),
            ["task_detail_file_selector"] = FileSelectorBox.Text.Trim(),
            ["file_extensions"] = ExtensionsBox.Text.Trim(),
            ["instruction"] = InstructionBox.Text.Trim(),
            ["max_tasks"] = ParseInt(MaxTasksBox.Text, 20),
            ["page_timeout_ms"] = ParseInt(TimeoutBox.Text, 30000),
            ["retries"] = ParseInt(RetriesBox.Text, 1),
            ["dedupe"] = DedupeCheck.IsChecked == true,
            ["save_results"] = SaveResultsCheck.IsChecked == true,
            ["schedule_enabled"] = ScheduleCheck.IsChecked == true,
            ["schedule_times"] = ScheduleTimesBox.Text.Trim(),
        };

        try
        {
            string json = await _backend.SendRequestAsync(new { action = "automation_config_set", automation = patch });
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            if (doc.RootElement.TryGetProperty("automation", out JsonElement el))
                ApplyAutomation(el);
            SaveHintText.Text = content.Length > 0 ? content : "已保存";
            AppendLog(SaveHintText.Text);
        }
        catch (Exception ex)
        {
            SaveHintText.Text = $"❌ 保存失败：{ex.Message}";
        }
    }

    private static int ParseInt(string text, int fallback) =>
        int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int value) ? value : fallback;

    // ---------- 自动化：测试选择器 ----------

    private async void TestSelectorButton_Click(object sender, RoutedEventArgs e)
    {
        string url = TaskUrlBox.Text.Trim();
        string selector = TaskSelectorBox.Text.Trim();
        if (url.Length == 0 || selector.Length == 0)
        {
            AppendLog("❌ 请先填写任务列表网址与元素位置");
            return;
        }
        TestSelectorButton.IsEnabled = false;
        AppendLog($"🔍 正在测试选择器：{selector}");
        AutomationStatusText.Text = "状态：测试选择器中（浏览器已打开）";
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "automation_test", url, selector });
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            AppendLog(content);
        }
        catch (Exception ex)
        {
            AppendLog($"❌ 测试失败：{ex.Message}");
        }
        finally
        {
            TestSelectorButton.IsEnabled = true;
            AutomationStatusText.Text = "状态：空闲";
        }
    }

    // ---------- 自动化：运行与停止 ----------

    private async void RunOnceButton_Click(object sender, RoutedEventArgs e)
    {
        if (_automationRunning)
            return;
        if (SafetyCheck.IsChecked != true)
        {
            MessageBox.Show(this, "请先阅读并勾选安全协议，再运行自动化。", "需要同意安全协议",
                MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        _automationRunning = true;
        RunOnceButton.IsEnabled = false;
        StopAutomationButton.IsEnabled = true;
        AutomationStatusText.Text = "状态：运行中（浏览器已打开）";
        AppendLog($"▶ 开始运行自动化 — {DateTime.Now:HH:mm:ss}");
        try
        {
            string final = await _backend.SendStreamRequestAsync(
                new { action = "automation_run" },
                delta => AppendLog(delta));
            AppendLog($"✅ {final}");
            AutomationStatusText.Text = "状态：已完成";
        }
        catch (Exception ex)
        {
            AppendLog($"❌ 自动化失败：{ex.Message}");
            AutomationStatusText.Text = "状态：失败";
        }
        finally
        {
            _automationRunning = false;
            RunOnceButton.IsEnabled = true;
            StopAutomationButton.IsEnabled = false;
        }
    }

    private async void StopAutomationButton_Click(object sender, RoutedEventArgs e)
    {
        StopAutomationButton.IsEnabled = false;
        AppendLog("⏹ 已请求停止自动化，等待当前步骤收尾…");
        try
        {
            await _backend.SendFireAndForgetAsync(new { action = "stop" });
        }
        catch (Exception ex)
        {
            AppendLog($"❌ 停止请求失败：{ex.Message}");
        }
    }

    private void AppendLog(string text)
    {
        if (string.IsNullOrEmpty(text))
            return;
        AutomationLogBox.AppendText(text.EndsWith('\n') ? text : text + Environment.NewLine);
        AutomationLogBox.ScrollToEnd();
    }

    // ---------- 技能市场 ----------

    private void MarketSearchBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_uiReady || _loading)
            return;
        _searchDebounce.Stop();
        _searchDebounce.Start();  // 防抖 300ms
    }

    private void MarketSortCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || _loading)
            return;
        _marketPage = 1;
        _ = SearchMarketAsync();
    }

    private void MarketRefreshButton_Click(object sender, RoutedEventArgs e)
    {
        _marketPage = 1;
        _ = SearchMarketAsync();
    }

    private void MarketInstalledButton_Click(object sender, RoutedEventArgs e) => _ = ShowInstalledAsync();

    private async Task SearchMarketAsync()
    {
        string keyword = MarketSearchBox.Text.Trim();
        string sort = (MarketSortCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "best";
        MarketStatusText.Text = keyword.Length == 0 ? "正在获取技能列表…" : $"正在搜索「{keyword}」…";
        MarketRefreshButton.IsEnabled = false;
        try
        {
            string json = await _backend.SendRequestAsync(new
            {
                action = "market_search",
                keyword,
                sort,
                page = _marketPage,
            });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("market", out JsonElement market))
            {
                MarketStatusText.Text = "⚠️ 技能市场不可用（后端未返回数据）";
                return;
            }
            RenderMarket(market);
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = $"❌ 技能市场请求失败：{ex.Message}";
        }
        finally
        {
            MarketRefreshButton.IsEnabled = true;
        }
    }

    private void RenderMarket(JsonElement market)
    {
        _marketItems.Clear();
        if (market.TryGetProperty("items", out JsonElement items) && items.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in items.EnumerateArray())
            {
                bool verified = item.TryGetProperty("verified", out JsonElement v) && v.ValueKind == JsonValueKind.True;
                bool installed = item.TryGetProperty("installed", out JsonElement ins) && ins.ValueKind == JsonValueKind.True;
                int stars = item.TryGetProperty("stars", out JsonElement s) && s.ValueKind == JsonValueKind.Number ? s.GetInt32() : 0;
                string pushed = item.TryGetProperty("pushed_at", out JsonElement p) ? p.GetString() ?? "" : "";
                if (pushed.Length >= 10)
                    pushed = pushed[..10];
                string license = item.TryGetProperty("license", out JsonElement l) ? l.GetString() ?? "" : "";
                string version = item.TryGetProperty("version", out JsonElement ver) ? ver.GetString() ?? "" : "";

                _marketItems.Add(new MarketItem
                {
                    FullName = item.TryGetProperty("full_name", out JsonElement fn) ? fn.GetString() ?? "" : "",
                    Name = item.TryGetProperty("name", out JsonElement n) ? n.GetString() ?? "" : "",
                    Description = Shorten(item.TryGetProperty("description", out JsonElement d) ? d.GetString() ?? "" : "", 200),
                    Badge = installed ? "● 已安装" : (verified ? "✔ 已验证" : "⚠ 未验证"),
                    Stars = $"⭐ {stars}",
                    Meta = string.Join(" · ", new[]
                    {
                        version.Length > 0 ? $"v{version}" : "",
                        pushed.Length > 0 ? $"更新于 {pushed}" : "",
                        license,
                    }.Where(x => x.Length > 0)),
                    Version = version,
                    HtmlUrl = item.TryGetProperty("html_url", out JsonElement h) ? h.GetString() ?? "" : "",
                    Verified = verified,
                    Installed = installed,
                });
            }
        }

        MarketList.ItemsSource = null;
        MarketList.ItemsSource = _marketItems;
        bool offline = market.TryGetProperty("offline", out JsonElement off) && off.ValueKind == JsonValueKind.True;
        int total = market.TryGetProperty("total_count", out JsonElement tc) && tc.ValueKind == JsonValueKind.Number ? tc.GetInt32() : _marketItems.Count;
        MarketEmptyText.Visibility = _marketItems.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        MarketEmptyText.Text = _marketItems.Count == 0
            ? "没有匹配的技能。\n可换关键词（技能仓库需带 GitHub topic「echodesk-plugin」）。"
            : "";
        MarketStatusText.Text = _marketItems.Count == 0
            ? "结果为空。"
            : $"共 {total} 个结果，本页 {_marketItems.Count} 个" + (offline ? "（离线：展示本地缓存快照）" : "");
    }

    private static string Shorten(string text, int max) =>
        text.Length <= max ? text : text[..max] + "…";

    private async Task ShowInstalledAsync()
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "market_installed" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("installed", out JsonElement list) || list.ValueKind != JsonValueKind.Array)
            {
                MarketStatusText.Text = "⚠️ 无法读取已安装技能列表";
                return;
            }
            var sb = new StringBuilder();
            int count = 0;
            foreach (JsonElement item in list.EnumerateArray())
            {
                string id = item.TryGetProperty("id", out JsonElement i) ? i.GetString() ?? "?" : "?";
                string version = item.TryGetProperty("version", out JsonElement v) ? v.GetString() ?? "" : "";
                sb.AppendLine($"• {id}{(version.Length > 0 ? $" v{version}" : "")}");
                count++;
            }
            MarketStatusText.Text = count == 0 ? "本机尚未安装任何技能。" : $"已安装 {count} 个技能：\n{sb}";
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = $"❌ 读取已安装列表失败：{ex.Message}";
        }
    }

    private void MarketDetail_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not MarketItem item)
            return;
        _ = ShowDetailAsync(item);
    }

    private async Task ShowDetailAsync(MarketItem item)
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "market_detail", repo = item.FullName });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("detail", out JsonElement detail))
            {
                MarketStatusText.Text = "⚠️ 无法获取技能详情";
                return;
            }
            var sb = new StringBuilder();
            sb.AppendLine($"技能：{item.FullName}");
            AppendDetail(sb, "版本", detail, "version");
            AppendDetail(sb, "来源", detail, "html_url");
            AppendDetail(sb, "入口文件", detail, "entry");
            AppendDetail(sb, "包含 prepare 脚本", detail, "has_prepare");
            if (detail.TryGetProperty("dependencies", out JsonElement deps) && deps.ValueKind == JsonValueKind.Array)
            {
                var list = deps.EnumerateArray().Select(x => x.ToString()).ToList();
                sb.AppendLine($"依赖：{(list.Count == 0 ? "无" : string.Join(", ", list))}");
            }
            if (detail.TryGetProperty("readme_excerpt", out JsonElement readme))
            {
                sb.AppendLine();
                sb.AppendLine("README 摘要：");
                sb.AppendLine(Shorten(readme.GetString() ?? "", 800));
            }
            MessageBox.Show(this, sb.ToString(), "技能详情", MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = $"❌ 获取详情失败：{ex.Message}";
        }
    }

    private static void AppendDetail(StringBuilder sb, string label, JsonElement detail, string key)
    {
        if (!detail.TryGetProperty(key, out JsonElement el))
            return;
        string value = el.ValueKind switch
        {
            JsonValueKind.String => el.GetString() ?? "",
            JsonValueKind.True => "是",
            JsonValueKind.False => "否",
            _ => el.ToString(),
        };
        if (value.Length > 0)
            sb.AppendLine($"{label}：{value}");
    }

    private void MarketInstall_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not MarketItem item)
            return;
        _ = ToggleInstallAsync(item);
    }

    private async Task ToggleInstallAsync(MarketItem item)
    {
        if (item.Installed)
        {
            var confirm = MessageBox.Show(this,
                $"确定卸载技能「{item.Name}」吗？\n（将删除其安装目录并从技能清单移除）",
                "卸载技能", MessageBoxButton.YesNo, MessageBoxImage.Question);
            if (confirm != MessageBoxResult.Yes)
                return;
            await CallMarketAsync(new { action = "market_uninstall", repo = item.FullName }, "卸载");
            return;
        }

        var first = MessageBox.Show(this,
            $"即将从 GitHub 安装第三方技能：\n\n" +
            $"技能：{item.Name}\n仓库：{item.FullName}\n版本：{(item.Version.Length > 0 ? item.Version : "未知")}\n" +
            $"来源：{item.HtmlUrl}\n\n" +
            "⚠️ 安全提示：第三方技能以你的本地账户权限运行，EchoDesk 不对其安全性负责。\n" +
            "安装前会读取并展示其 skill.json 以了解权限与依赖。是否继续？",
            "安装第三方技能", MessageBoxButton.YesNo, MessageBoxImage.Warning);
        if (first != MessageBoxResult.Yes)
            return;

        await CallMarketAsync(new { action = "market_install", repo = item.FullName }, "安装");
    }

    private async Task CallMarketAsync(object request, string verb)
    {
        MarketStatusText.Text = $"正在{verb}…（下载源码并校验 skill.json）";
        try
        {
            string json = await _backend.SendRequestAsync(request);
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            MarketStatusText.Text = content.Length > 0 ? content : $"{verb}完成";
            await SearchMarketAsync();  // 刷新「已安装」徽标
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = $"❌ {verb}失败：{ex.Message}";
        }
    }

    private void MarketList_MouseDoubleClick(object sender, System.Windows.Input.MouseButtonEventArgs e)
    {
        if (MarketList.SelectedItem is MarketItem item)
            _ = ShowDetailAsync(item);
    }

    private void CloseButton_Click(object sender, RoutedEventArgs e) => Close();

    /// <summary>供主窗口 Ctrl+M 调用：切到技能市场 Tab 并刷新一次列表。</summary>
    public void ShowMarketTab() => ShowTab("技能市场", refreshMarket: true);

    /// <summary>
    /// 切到指定标题的 Tab（供主窗口状态区点击跳转）。refreshMarket 为真时顺带刷新市场列表。
    /// </summary>
    public void ShowTab(string header, bool refreshMarket = false)
    {
        for (int i = 0; i < Tabs.Items.Count; i++)
        {
            if (Tabs.Items[i] is TabItem tab && Equals(tab.Header, header))
            {
                Tabs.SelectedIndex = i;
                break;
            }
        }
        if (refreshMarket)
        {
            _marketPage = 1;
            _ = SearchMarketAsync();
        }
    }

    // ---------- JSON 读取辅助 ----------

    private static string GetString(JsonElement el, string key) =>
        el.TryGetProperty(key, out JsonElement v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    private static bool GetBool(JsonElement el, string key) =>
        el.TryGetProperty(key, out JsonElement v) && v.ValueKind == JsonValueKind.True;

    private static int GetInt(JsonElement el, string key, int fallback) =>
        el.TryGetProperty(key, out JsonElement v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : fallback;

    private static List<string> GetStringList(JsonElement el, string key)
    {
        var list = new List<string>();
        if (el.TryGetProperty(key, out JsonElement v) && v.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in v.EnumerateArray())
                list.Add(item.ToString());
        }
        return list;
    }
}
