using System.Diagnostics;
using System.Globalization;
using System.IO;
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
    public string ActionText => Installed
        ? Loc.T("settings.button.uninstall", "卸载")
        : (Verified ? Loc.T("settings.button.install", "安装")
                    : Loc.T("settings.market.unverified", "未验证"));
    public bool CanAct => Installed || Verified;
}

/// <summary>模型选择栏：搜索结果项（新增四）。</summary>
public sealed class HubModelItem
{
    public string Repo { get; init; } = "";
    public string Name { get; init; } = "";
    public string Badge { get; init; } = "";
    public string Meta { get; init; } = "";
    public bool Gated { get; init; }
}

/// <summary>模型选择栏：仓库内的 GGUF 文件项（新增四）。</summary>
public sealed class HubFileItem
{
    public string Repo { get; init; } = "";
    public string FileName { get; init; } = "";
    public long Size { get; init; }
    public string SizeText { get; init; } = "";
    public string Quantization { get; init; } = "";
    public bool Installed { get; init; }

    /// <summary>已下载完成的文件显示为「已下载」，按钮置灰，避免重复下载。</summary>
    public string ActionText => Installed
        ? Loc.T("settings.model.actionDownloaded", "已下载")
        : Loc.T("settings.model.actionDownload", "下载");
    public bool CanAct => !Installed;
}

/// <summary>模型选择栏：本地模型库条目（新增四）。</summary>
public sealed class HubRegistryItem
{
    public string Name { get; init; } = "";
    public string SizeText { get; init; } = "";
    public string Meta { get; init; } = "";
    public string CurrentBadge { get; init; } = "";
    public bool InUse { get; init; }

    /// <summary>正在使用的模型不允许删除（后端也会再拦一次）。</summary>
    public bool CanDelete => !InUse;
}

/// <summary>
/// EchoDesk 设置窗口：常规 / 模型 / 自动化 / 技能市场 四个 Tab。
///
/// 与后端通过既有 JSON 行协议通信（automation_config、automation_config_set、
/// automation_test、automation_run、market_*、model_hub_* 等动作）。主题与字号变化通过回调
/// 通知主窗口即时生效。
/// </summary>
public partial class SettingsWindow : Window
{
    /// <summary>安全协议文本（版本号在协议变更时必须递增，并会记入审计日志）。</summary>
    private const string SafetyVersion = "1.0";

    // 任务三：安全协议正文已收编进语言包（键 settings.automation.safetyBody），
    // 由 DefaultSafetyBody 提供中文兜底，界面文本不再硬编码在这里。

    /// <summary>可选的默认对话模式键；显示名由语言包的 mode.&lt;key&gt; 提供。</summary>
    private static readonly string[] Modes =
    {
        "general", "summary", "translate", "code", "contract", "creative",
    };

    private readonly BackendService _backend;
    private readonly AppSettings _uiState;
    private readonly Action<AppTheme>? _onThemeChanged;
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
    // 主题/emoji 下拉框的程序化选中不应触发"用户改了设置"的回调
    private bool _themeSyncing;
    private bool _emojiSyncing;

    // ---------- 模型选择栏状态（新增四） ----------
    private readonly List<HubModelItem> _hubItems = new();
    private readonly List<HubFileItem> _hubFiles = new();
    private readonly List<HubRegistryItem> _hubRegistry = new();
    private readonly DispatcherTimer _hubDebounce;
    private bool _downloading;
    private bool _suppressHubFilterEvents;

    public SettingsWindow(
        BackendService backend,
        AppSettings uiState,
        double fontSize,
        Action<AppTheme>? onThemeChanged,
        Action<double>? onFontSizeChanged)
    {
        InitializeComponent();
        _backend = backend;
        _uiState = uiState;
        _onThemeChanged = onThemeChanged;
        _onFontSizeChanged = onFontSizeChanged;

        // 多语言（任务三）：先回填 XAML 静态文案，再订阅切换事件（切换后即时刷新）
        Loc.Apply(this);
        Title = Loc.T("settings.title");
        Loc.LanguageChanged += (_, _) => ApplyLanguage();

        SafetyText.Text = DefaultSafetyBody;

        foreach (string key in Modes)
            DefaultModeCombo.Items.Add(new ComboBoxItem { Content = Loc.T("mode." + key, key), Tag = key });

        // 主题（修复二）：三态下拉框 + emoji 显示策略（修复六），两者都即时生效
        InitThemeControls();
        InitEmojiControls();
        ConfirmDestructiveCheck.IsChecked = _uiState.ConfirmDestructiveCommands;
        FontSizeSlider.Value = fontSize;
        FontSizeLabel.Text = $"{fontSize:0.#} pt";
        PathsText.Text = DescribePaths();

        MarketSortCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.market.sortBest", "最佳匹配"), Tag = "best" });
        MarketSortCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.market.sortStars", "Star 最多"), Tag = "stars" });
        MarketSortCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.market.sortUpdated", "最近更新"), Tag = "updated" });
        MarketSortCombo.SelectedIndex = 0;
        TemplateCombo.DisplayMemberPath = "Label";   // 模板项是 TemplateItem 对象

        _searchDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(300) };
        _searchDebounce.Tick += (_, _) =>
        {
            _searchDebounce.Stop();
            _marketPage = 1;
            _ = SearchMarketAsync();
        };

        // 模型选择栏：搜索防抖 + 排序/量化筛选项（构造期间静默，避免触发请求）
        _suppressHubFilterEvents = true;
        HubSortCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.model.sortDownloads", "下载量最多"), Tag = "downloads" });
        HubSortCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.model.sortUpdated", "最近更新"), Tag = "updated" });
        HubSortCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.model.sortLikes", "点赞最多"), Tag = "likes" });
        HubSortCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.model.sortName", "名称"), Tag = "name" });
        HubSortCombo.SelectedIndex = 0;
        foreach (string quant in new[] { "", "Q4_K_M", "Q4_0", "Q5_K_M", "Q8_0", "Q2_K", "F16" })
            HubQuantCombo.Items.Add(quant);
        HubQuantCombo.SelectedIndex = 0;
        HubSourceCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.model.sourceAuto", "自动（推荐）"), Tag = "auto" });
        HubSourceCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.model.sourceMirror", "镜像源 hf-mirror.com"), Tag = "mirror" });
        HubSourceCombo.Items.Add(new ComboBoxItem { Content = Loc.T("settings.model.sourceOfficial", "官方源 huggingface.co"), Tag = "official" });
        HubSourceCombo.SelectedIndex = 0;
        _suppressHubFilterEvents = false;

        _hubDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
        _hubDebounce.Tick += (_, _) =>
        {
            _hubDebounce.Stop();
            _ = SearchHubAsync();
        };

        // 先让窗口完成构建与绘制，再异步取数据：窗口打开时间与后端无关（要求 < 500ms）。
        // v0.2.1 新增界面（语言 / 插件 / 开发者）在这里初始化：全部是纯本地操作，
        // 不访问后端，因此不影响"打开即见"的时间指标。
        InitLanguageUi();
        InitSkillsUi();
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
            SetLoadState(Loc.T("settings.loadState.initFailed", "⚠️ 初始化失败：{0}", ex.Message));
            App.Log("SettingsWindow.LoadInitial", ex);
        }
    }

    private async Task LoadInitialAsync()
    {
        SetLoadState(Loc.T("settings.loadState.reading", "正在读取配置…"));

        string? configJson = await RequestWithRetryAsync(
            new { action = "automation_config" },
            Loc.T("settings.automation.labelReadingConfig", "读取自动化配置"), TimeSpan.FromSeconds(90));
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
                SetLoadState(Loc.T("settings.loadState.parseFailed",
                    "⚠️ 自动化配置解析失败：{0}", ex.Message));
            }
        }

        string? modeJson = await RequestWithRetryAsync(
            new { action = "context" },
            Loc.T("settings.general.labelReadingMode", "读取当前对话模式"), TimeSpan.FromSeconds(60));
        if (modeJson is not null)
        {
            try
            {
                using JsonDocument doc = JsonDocument.Parse(modeJson);
                // 任务三：模式改读机器可读字段。此前靠匹配 /mode 的中文输出
                // （"当前模式: general"），界面切成英文后就再也选不中默认模式了。
                string mode = GetString(doc.RootElement, "mode");
                SelectMode(mode.Length > 0 ? mode : "general");
            }
            catch (JsonException)
            {
                SelectMode("general");
            }
        }

        // v0.2.1：字段级帮助与模板库（任务四），以及插件状态（任务二）
        await LoadAutomationHelpAsync();
        ApplyFieldHelpTooltips();
        await LoadSkillsAsync();
        await LoadManifestAsync();

        _loading = false;
        MarketStatusText.Text = Loc.T("settings.market.tip",
            "技能市场：点「刷新」浏览 topic 为 echodesk-plugin 的技能，或输入关键词搜索。");
        SetLoadState(Loc.T("settings.loadState.loaded", "✅ 配置已载入"));
        await LoadHubAsync();
    }

    // ---------- 模型选择栏（新增四） ----------

    /// <summary>初始化模型 Tab：读数据源状态 + 加载本地模型库（不自动发起网络搜索）。</summary>
    private async Task LoadHubAsync()
    {
        string? statusJson = await RequestWithRetryAsync(
            new { action = "model_hub_status" },
            Loc.T("settings.model.labelReadingHubStatus", "读取模型中心状态"), TimeSpan.FromSeconds(60));
        if (statusJson is not null)
        {
            try
            {
                using JsonDocument doc = JsonDocument.Parse(statusJson);
                if (doc.RootElement.TryGetProperty("model_hub_status", out JsonElement st))
                {
                    HubStatusText.Text = Loc.T("settings.model.statusSource",
                        "数据源：{0}", GetString(st, "source_note"));
                    SelectHubSource(GetString(st, "source_setting"));
                    HubRegistryTitle.Text = Loc.T("settings.model.registryTitle", "📚 本地模型库（{0} 个 · {1}）",
                        GetInt(st, "count", 0), GetString(st, "total_size_human"));
                    if (GetBool(st, "token_configured"))
                        HubTokenBox.ToolTip = Loc.T("settings.model.tokenConfigured",
                            "已配置 Token（留空表示不修改）");
                }
            }
            catch (JsonException)
            {
                // 忽略解析失败，模型库仍可单独刷新
            }
        }
        await RefreshHubRegistryAsync();
    }

    private void SelectHubSource(string key)
    {
        for (int i = 0; i < HubSourceCombo.Items.Count; i++)
        {
            if (HubSourceCombo.Items[i] is ComboBoxItem item && Equals(item.Tag, key))
            {
                _suppressHubFilterEvents = true;
                HubSourceCombo.SelectedIndex = i;
                _suppressHubFilterEvents = false;
                return;
            }
        }
    }

    private string CurrentHubSource() =>
        (HubSourceCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "auto";

    private void HubSearchBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_uiReady || _loading)
            return;
        _hubDebounce.Stop();
        _hubDebounce.Start();
    }

    private void HubFilter_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || _loading || _suppressHubFilterEvents)
            return;
        _ = SearchHubAsync();
    }

    private void HubSearchButton_Click(object sender, RoutedEventArgs e) => _ = SearchHubAsync();

    /// <summary>搜索可下载模型：镜像/官方双源，结果里明确显示当前使用的是哪个源。</summary>
    private async Task SearchHubAsync()
    {
        HubSearchButton.IsEnabled = false;
        HubStatusText.Text = Loc.T("settings.model.searching", "正在搜索模型…");
        try
        {
            string sort = (HubSortCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "downloads";
            string quant = (HubQuantCombo.Text ?? "").Trim();
            int minDownloads = int.TryParse(HubMinDownloadsBox.Text.Trim(), out int md) ? md : 0;
            string json = await _backend.SendRequestAsync(new
            {
                action = "model_hub_search",
                keyword = HubSearchBox.Text.Trim(),
                sort,
                quant,
                min_downloads = minDownloads,
                source = CurrentHubSource(),
            }, idleTimeout: TimeSpan.FromSeconds(90));
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("model_hub", out JsonElement hub))
            {
                HubStatusText.Text = Loc.T("settings.model.hubUnavailable",
                    "⚠️ 模型中心不可用（后端未返回数据）");
                return;
            }
            RenderHubResult(hub);
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.searchFailed", "❌ 搜索失败：{0}", ex.Message);
        }
        finally
        {
            HubSearchButton.IsEnabled = true;
        }
    }

    private void RenderHubResult(JsonElement hub)
    {
        _hubItems.Clear();
        if (hub.TryGetProperty("items", out JsonElement items) && items.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in items.EnumerateArray())
            {
                int downloads = GetInt(item, "downloads", 0);
                int likes = GetInt(item, "likes", 0);
                bool gated = GetBool(item, "gated");
                bool installed = GetBool(item, "installed");
                _hubItems.Add(new HubModelItem
                {
                    Repo = GetString(item, "repo"),
                    Name = GetString(item, "name"),
                    Gated = gated,
                    Badge = installed ? Loc.T("settings.model.badgeDownloaded", "● 已下载")
                                      : (gated ? Loc.T("settings.model.badgeGated", "🔒 需登录") : ""),
                    Meta = string.Join(" · ", new[]
                    {
                        GetString(item, "author"),
                        $"⬇ {downloads:N0}",
                        $"⭐ {likes}",
                        GetString(item, "updated_at").Length > 0
                            ? Loc.T("settings.model.updatedAt", "更新于 {0}", GetString(item, "updated_at"))
                            : "",
                    }.Where(x => x.Length > 0)),
                });
            }
        }
        HubResultList.ItemsSource = null;
        HubResultList.ItemsSource = _hubItems;
        HubEmptyText.Visibility = _hubItems.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        string note = GetString(hub, "source_note");
        bool offline = GetBool(hub, "offline");
        HubStatusText.Text = (_hubItems.Count == 0
            ? Loc.T("settings.model.searchEmpty", "结果为空。可换关键词（模型需为 GGUF 格式）。")
            : Loc.T("settings.model.resultCount", "共 {0} 个结果。", _hubItems.Count))
            + (note.Length > 0 ? " " + Loc.T("settings.model.statusSource", "数据源：{0}", note) : "");
        if (offline)
            HubStatusText.Text = Loc.T("settings.model.sourceOffline", "⚠️ 数据源不可用：请检查网络，")
                + HubStatusText.Text;
    }

    private void HubResultList_MouseDoubleClick(object sender, System.Windows.Input.MouseButtonEventArgs e)
    {
        if (HubResultList.SelectedItem is HubModelItem item)
            _ = ShowHubDetailAsync(item);
    }

    private void HubDetail_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is HubModelItem item)
            _ = ShowHubDetailAsync(item);
    }

    /// <summary>展开详情面板：列出该仓库所有 GGUF 量化版本与大小（含预计磁盘占用）。</summary>
    private async Task ShowHubDetailAsync(HubModelItem item)
    {
        HubStatusText.Text = Loc.T("settings.model.readingFiles", "正在读取 {0} 的可用文件…", item.Repo);
        try
        {
            string json = await _backend.SendRequestAsync(
                new { action = "model_hub_detail", repo = item.Repo, source = CurrentHubSource() },
                idleTimeout: TimeSpan.FromSeconds(90));
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("detail", out JsonElement detail))
            {
                HubStatusText.Text = Loc.T("settings.model.detailUnavailable", "⚠️ 无法获取模型详情");
                return;
            }
            _hubFiles.Clear();
            if (detail.TryGetProperty("files", out JsonElement files) && files.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement f in files.EnumerateArray())
                {
                    _hubFiles.Add(new HubFileItem
                    {
                        Repo = item.Repo,
                        FileName = GetString(f, "filename"),
                        Size = f.TryGetProperty("size", out JsonElement sz) && sz.ValueKind == JsonValueKind.Number
                            ? sz.GetInt64() : 0,
                        SizeText = GetString(f, "size_human"),
                        Quantization = GetString(f, "quantization"),
                        Installed = GetBool(f, "installed"),
                    });
                }
            }
            HubFileList.ItemsSource = null;
            HubFileList.ItemsSource = _hubFiles;
            string license = GetString(detail, "license");
            HubDetailTitle.Text =
                Loc.T("settings.model.detailHeader", "{0} — 可用 GGUF 文件 {1} 个", item.Repo, _hubFiles.Count)
                + (license.Length > 0 ? Loc.T("settings.model.detailLicense", "（许可：{0}）", license) : "")
                + (GetBool(detail, "gated") ? "　" + Loc.T("settings.model.detailGated", "🔒 需要 Token") : "");
            HubDetailPanel.Visibility = Visibility.Visible;
            HubStatusText.Text = Loc.T("settings.model.detailHint",
                "选定量化版本后点「下载」，会先显示文件大小与预计磁盘占用。");
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.fetchDetailFailed", "❌ 获取详情失败：{0}", ex.Message);
        }
    }

    private void HubCloseDetail_Click(object sender, RoutedEventArgs e)
    {
        HubDetailPanel.Visibility = Visibility.Collapsed;
        _hubFiles.Clear();
        HubFileList.ItemsSource = null;
    }

    private void HubDownload_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not HubFileItem file)
            return;
        _ = DownloadHubFileAsync(file);
    }

    /// <summary>下载选定量化版本：确认大小后流式下载，实时显示百分比/速度/剩余时间，可取消。</summary>
    private async Task DownloadHubFileAsync(HubFileItem file)
    {
        if (_downloading)
        {
            HubStatusText.Text = Loc.T("settings.model.downloadBusy",
                "⏳ 已有下载在进行中，请等待完成或先取消。");
            return;
        }
        double gb = file.Size / 1073741824.0;
        var confirm = MessageBox.Show(this,
            Loc.T("settings.model.downloadConfirmText",
                "即将下载模型文件：\n\n仓库：{0}\n文件：{1}\n量化：{2}\n大小：{3}\n预计磁盘占用：{3}（下载完成后同时占用这么多空间）\n\n下载完成后会自动登记到本地模型库，可用 /model list 查看并切换。是否继续？",
                file.Repo, file.FileName, file.Quantization, file.SizeText),
            Loc.T("settings.model.downloadConfirmTitle", "确认下载模型"),
            MessageBoxButton.YesNo, MessageBoxImage.Question);
        if (confirm != MessageBoxResult.Yes)
            return;

        _downloading = true;
        HubProgressBar.Visibility = Visibility.Visible;
        HubProgressBar.Value = 0;
        HubCancelButton.Visibility = Visibility.Visible;
        HubProgressText.Text = Loc.T("settings.model.connecting", "正在建立连接…");
        _hubFiles.Clear();
        HubFileList.ItemsSource = null;
        HubDetailPanel.Visibility = Visibility.Collapsed;

        try
        {
            // 不使用 CancellationToken 中断读取：stdout 是单条共享流，中途放弃读取会让
            // 后续响应错位。取消改为向后端发 stop（后端在下载循环里检查并保留断点）。
            string final = await _backend.SendStreamRequestAsync(
                new { action = "model_hub_download", repo = file.Repo, filename = file.FileName, size = file.Size, source = CurrentHubSource() },
                delta =>
                {
                    HubProgressText.Text = delta.Trim();
                    int mark = delta.IndexOf('%');
                    if (mark > 2 && double.TryParse(delta[..mark].Replace("📥", "").Trim(), out double pct))
                        HubProgressBar.Value = Math.Clamp(pct, 0, 100);
                });
            HubStatusText.Text = final;
            HubProgressBar.Value = 100;
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.downloadFailed", "❌ 下载失败：{0}", ex.Message);
        }
        finally
        {
            _downloading = false;
            HubCancelButton.Visibility = Visibility.Collapsed;
            await RefreshHubRegistryAsync();
        }
    }

    private async void HubCancelButton_Click(object sender, RoutedEventArgs e)
    {
        HubCancelButton.IsEnabled = false;
        HubProgressText.Text = Loc.T("settings.model.cancelRequested", "⏹ 已请求取消下载…");
        try
        {
            await _backend.SendFireAndForgetAsync(new { action = "stop" });
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.cancelFailed", "❌ 取消请求失败：{0}", ex.Message);
        }
        finally
        {
            HubCancelButton.IsEnabled = true;
        }
    }

    private void HubRegistryRefreshButton_Click(object sender, RoutedEventArgs e) => _ = RefreshHubRegistryAsync();

    /// <summary>刷新本地模型库：显示已下载模型、占用空间、最后使用时间与当前使用状态。</summary>
    private async Task RefreshHubRegistryAsync()
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "model_hub_registry" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("registry", out JsonElement reg))
                return;
            _hubRegistry.Clear();
            if (reg.TryGetProperty("models", out JsonElement models) && models.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement m in models.EnumerateArray())
                {
                    bool current = GetBool(m, "current");
                    string downloaded = GetString(m, "downloaded_at").Replace('T', ' ');
                    string used = GetString(m, "last_used").Replace('T', ' ');
                    _hubRegistry.Add(new HubRegistryItem
                    {
                        Name = GetString(m, "name"),
                        SizeText = GetString(m, "size_human"),
                        InUse = current,
                        CurrentBadge = current ? Loc.T("settings.model.badgeInUse", "▶ 正在使用") : "",
                        Meta = string.Join(" · ", new[]
                        {
                            GetString(m, "quantization"),
                            GetString(m, "repo").Length > 0
                                ? GetString(m, "repo") : Loc.T("settings.model.localFile", "本地文件"),
                            downloaded.Length > 0
                                ? Loc.T("settings.model.downloadedAt", "下载于 {0}", downloaded) : "",
                            used.Length > 0
                                ? Loc.T("settings.model.lastUsed", "最后使用 {0}", used)
                                : Loc.T("settings.model.neverUsed", "尚未使用"),
                        }.Where(x => x.Length > 0)),
                    });
                }
            }
            HubRegistryList.ItemsSource = null;
            HubRegistryList.ItemsSource = _hubRegistry;
            HubRegistryTitle.Text = Loc.T("settings.model.registryTitle", "📚 本地模型库（{0} 个 · {1}）",
                GetInt(reg, "count", _hubRegistry.Count), GetString(reg, "total_size_human"));
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.registryLoadFailed", "❌ 读取模型库失败：{0}", ex.Message);
        }
    }

    private async void HubDelete_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not HubRegistryItem item)
            return;
        if (item.InUse)
        {
            HubStatusText.Text = Loc.T("settings.model.inUseCannotDelete",
                "该模型正在使用中，请先用 /model 切换到其它模型再删除。");
            return;
        }
        var confirm = MessageBox.Show(this,
            Loc.T("settings.model.deleteConfirmText",
                "确定删除模型「{0}」吗？\n\n将删除文件（{1}）并从模型库移除，此操作不可撤销。\n若之后还需要，必须重新下载。",
                item.Name, item.SizeText),
            Loc.T("settings.model.deleteConfirmTitle", "删除模型"),
            MessageBoxButton.YesNo, MessageBoxImage.Warning);
        if (confirm != MessageBoxResult.Yes)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(
                new { action = "model_hub_uninstall", name = item.Name },
                idleTimeout: TimeSpan.FromSeconds(60));
            using JsonDocument doc = JsonDocument.Parse(json);
            HubStatusText.Text = doc.RootElement.TryGetProperty("content", out JsonElement c)
                ? c.GetString() ?? Loc.T("settings.model.deleted", "已删除")
                : Loc.T("settings.model.deleted", "已删除");
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.deleteFailed", "❌ 删除失败：{0}", ex.Message);
        }
        await RefreshHubRegistryAsync();
        await SearchHubAsync();   // 刷新「已下载」徽标
    }

    private void HubSourceCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || _loading || _suppressHubFilterEvents)
            return;
        _ = SaveHubConfigAsync();
    }

    private async void HubSaveTokenButton_Click(object sender, RoutedEventArgs e)
    {
        HubSaveTokenButton.IsEnabled = false;
        try
        {
            await SaveHubConfigAsync(includeToken: true);
            HubTokenBox.Clear();
        }
        finally
        {
            HubSaveTokenButton.IsEnabled = true;
        }
    }

    /// <summary>保存数据源 / Token 到后端配置（Token 只在填写时才覆盖，避免误清空）。</summary>
    private async Task SaveHubConfigAsync(bool includeToken = false)
    {
        var patch = new Dictionary<string, object?> { ["source"] = CurrentHubSource() };
        string token = HubTokenBox.Password.Trim();
        if (includeToken && token.Length > 0)
            patch["hf_token"] = token;
        try
        {
            string json = await _backend.SendRequestAsync(
                new { action = "model_hub_config_set", model_hub = patch });
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            HubStatusText.Text = content.Length > 0 ? content : Loc.T("settings.model.saved", "已保存");
            if (doc.RootElement.TryGetProperty("model_hub", out JsonElement st))
                HubStatusText.Text += "　" + Loc.T("settings.model.statusSource", "数据源：{0}",
                    GetString(st, "source_note"));
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.saveFailed", "❌ 保存失败：{0}", ex.Message);
        }
    }

    private async void HubOpenDirButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "model_hub_status" });
            using JsonDocument doc = JsonDocument.Parse(json);
            string dir = doc.RootElement.TryGetProperty("model_hub_status", out JsonElement st)
                ? GetString(st, "models_dir") : "";
            if (dir.Length == 0)
            {
                HubStatusText.Text = Loc.T("settings.model.dirMissing", "⚠️ 未取到模型目录");
                return;
            }
            Directory.CreateDirectory(dir);
            Process.Start(new ProcessStartInfo { FileName = dir, UseShellExecute = true });
            HubStatusText.Text = Loc.T("settings.model.dirOpened", "已打开模型目录：{0}", dir);
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.dirOpenFailed", "❌ 打开模型目录失败：{0}", ex.Message);
        }
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
                SetLoadState(Loc.T("settings.loadState.backendBusy",
                    "⏳ {0}：后端正在忙（第 {1} 次等待，最长 {2} 秒）…",
                    label, waits, $"{total.TotalSeconds:0}"));
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
                SetLoadState(Loc.T("settings.loadState.timeout", "⚠️ {0}超时：{1}", label, ex.Message));
                return null;
            }
            catch (OperationCanceledException)
            {
                return null;  // 窗口已关闭
            }
        }
        if (!_closing.IsCancellationRequested)
            SetLoadState(Loc.T("settings.loadState.incomplete",
                "⚠️ {0}未完成（后端长时间忙碌）。窗口可正常使用，稍后点「保存设置」会重试。", label));
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
        // 新增三：批量处理结果目录
        TaskNameBox.Text = GetString(el, "task_name");
        ResultsRootNameBox.Text = GetString(el, "results_root_name");
        ResultsLocationBox.Text = GetString(el, "results_location");
        UpdateResultsPreview();
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

    /// <summary>
    /// 主题下拉框（修复二）：跟随系统 / 浅色 / 深色。
    ///
    /// 每项都带 Tag（AppTheme 名），因此语言切换后重建列表也能保持选中项。
    /// </summary>
    private void InitThemeControls()
    {
        if (ThemeCombo is null)
            return;
        _themeSyncing = true;
        ThemeCombo.Items.Clear();
        foreach (AppTheme theme in new[] { AppTheme.System, AppTheme.Light, AppTheme.Dark })
        {
            ThemeCombo.Items.Add(new ComboBoxItem
            {
                Content = Loc.T(ThemeLabelKey(theme)),
                Tag = ThemeService.Serialize(theme),
            });
        }
        ThemeCombo.SelectedIndex = (int)ThemeService.Selected;
        _themeSyncing = false;
        UpdateThemeStatus();
    }

    private static string ThemeLabelKey(AppTheme theme) => theme switch
    {
        AppTheme.Dark => "settings.general.themeDark",
        AppTheme.Light => "settings.general.themeLight",
        _ => "settings.general.themeSystem",
    };

    /// <summary>把当前主题状态显示出来：选"跟随系统"时明确告知实际生效的是哪一种。</summary>
    private void UpdateThemeStatus()
    {
        if (ThemeStatusText is null)
            return;
        string effective = Loc.T(ThemeLabelKey(ThemeService.Effective));
        ThemeStatusText.Text = ThemeService.Selected == AppTheme.System
            ? Loc.T("settings.general.themeFollowing", "当前跟随系统：{0}", effective)
            : Loc.T("settings.general.themeEffective", "当前生效：{0}", effective);
    }

    private void ThemeCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (ThemeCombo?.SelectedItem is not ComboBoxItem item)
            return;
        if (_themeSyncing || !_uiReady)
            return;
        AppTheme theme = ThemeService.Parse(item.Tag?.ToString());
        _onThemeChanged?.Invoke(theme);   // 主窗口负责应用并持久化
        UpdateThemeStatus();
    }

    /// <summary>emoji 显示策略下拉框（修复六）。</summary>
    private void InitEmojiControls()
    {
        if (EmojiCombo is null)
            return;
        _emojiSyncing = true;
        EmojiCombo.Items.Clear();
        EmojiCombo.Items.Add(new ComboBoxItem
        {
            Content = Loc.T("settings.general.emojiAuto", "自动（按本机字体）"),
            Tag = "auto",
        });
        EmojiCombo.Items.Add(new ComboBoxItem
        {
            Content = Loc.T("settings.general.emojiAlways", "始终显示 emoji"),
            Tag = "always",
        });
        EmojiCombo.Items.Add(new ComboBoxItem
        {
            Content = Loc.T("settings.general.emojiNever", "不使用 emoji（纯文本符号）"),
            Tag = "never",
        });
        EmojiCombo.SelectedIndex = _uiState.EmojiMode switch
        {
            "always" => 1,
            "never" => 2,
            _ => 0,
        };
        _emojiSyncing = false;
        UpdateEmojiStatus();
    }

    private void UpdateEmojiStatus()
    {
        if (EmojiStatusText is null)
            return;
        bool fontPresent = Emoji.Available;
        EmojiStatusText.Text = fontPresent
            ? (Emoji.Enabled
                ? Loc.T("settings.general.emojiOn", "本机支持 emoji，当前正常显示")
                : Loc.T("settings.general.emojiForcedOff", "已按你的选择改用纯文本符号"))
            : Loc.T("settings.general.emojiFontMissing",
                    "本机缺少 emoji 字体，界面自动使用纯文本符号（[OK] / [!]）");
    }

    private void EmojiCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (EmojiCombo?.SelectedItem is not ComboBoxItem item)
            return;
        if (_emojiSyncing || !_uiReady)
            return;
        _uiState.EmojiMode = item.Tag?.ToString() ?? "auto";
        _uiState.Save();
        Emoji.Configure(_uiState.EmojiMode);
        UpdateEmojiStatus();
        // 立刻用新策略重刷界面文案（emoji 可能被换成 ASCII 标记，需要重新取一次语言包）
        Loc.Reapply();
    }

    /// <summary>
    /// 主题变化后重刷本窗口里"代码生成"的内容。
    ///
    /// XAML 里用 DynamicResource 的部分会自动跟随，无需处理；这里只补
    /// 代码构建的控件（插件面板、卡片列表、下拉框项）与状态文本。
    /// </summary>
    public void ApplyThemeResources()
    {
        InitThemeControls();
        UpdateEmojiStatus();
        // 卡片状态色/插件面板等由代码生成的画刷不跟随 DynamicResource，需要重建
        RenderSkillCards();
        RenderPanels();
        // 语言包文本里的 emoji 依赖 Emoji.Enabled，主题切换时一并重取（代价很低）
        Loc.Apply(this);
    }

    /// <summary>界面/数据位置说明（修复七：外层目录与 app/ 的分工要写清楚）。</summary>
    private string DescribePaths()
    {
        string program = ShortcutService.ProgramDirectory;
        string data = ShortcutService.UserDataDirectory;
        var lines = new List<string>
        {
            $"{Loc.T("settings.general.pathProgram", "程序目录")}: {program}",
            $"{Loc.T("settings.general.pathBackend", "后端目录（配置 / models / 会话 / 自动化日志）")}: " +
            $"{Path.Combine(program, "backend")}",
            $"{Loc.T("settings.general.pathSkills", "插件目录")}: " +
            $"{Path.Combine(program, "backend", "skills")}",
            $"{Loc.T("settings.general.pathUserData", "用户数据目录（界面状态 / 常用语 / 批量处理结果）")}: {data}",
        };
        if (!string.Equals(program, data, StringComparison.OrdinalIgnoreCase))
        {
            lines.Add(Loc.T("settings.general.pathPortableNote",
                "提示：本程序按便携版布局运行，程序本体在 app\\ 下，ui_state.json、常用语与「批量处理结果」都在外层目录，便于备份与迁移。"));
        }
        return string.Join("\n", lines);
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
            ? Loc.T("settings.general.confirmOn", "已开启指令执行确认")
            : Loc.T("settings.general.confirmOff", "已关闭指令执行确认（有副作用指令将直接执行）");
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
            AutomationEnabledCheck.Content = Loc.T("settings.automation.enabledNeedsSafety",
                "启用自动化功能（请先勾选上方安全协议）");
        }
        else
        {
            AutomationEnabledCheck.Content = Loc.T("settings.automation.label.enabled",
                "启用自动化功能（运行前仍会要求安全协议已勾选）");
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
            // 新增三：结果目录（顶层文件夹名 / 保存位置 / 任务名称）
            ["task_name"] = TaskNameBox.Text.Trim(),
            ["results_root_name"] = ResultsRootNameBox.Text.Trim(),
            ["results_location"] = ResultsLocationBox.Text.Trim(),
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
            SaveHintText.Text = content.Length > 0 ? content : Loc.T("settings.automation.saved", "已保存");
            AppendLog(SaveHintText.Text);
        }
        catch (Exception ex)
        {
            SaveHintText.Text = Loc.T("settings.automation.saveFailed", "❌ 保存失败：{0}", ex.Message);
        }
    }

    private static int ParseInt(string text, int fallback) =>
        int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int value) ? value : fallback;

    // ---------- 自动化：批量处理结果目录（新增三） ----------

    /// <summary>保存位置留空时显示"程序根目录"，并预览完整目录结构。</summary>
    private void UpdateResultsPreview()
    {
        if (ResultsPreviewText is null)
            return;
        string location = ResultsLocationBox?.Text.Trim() ?? "";
        string rootName = ResultsRootNameBox?.Text.Trim() ?? "";
        if (rootName.Length == 0)
            rootName = Loc.T("settings.automation.defaultResultsRoot", "批量处理结果");
        string task = TaskNameBox?.Text.Trim() ?? "";
        if (task.Length == 0)
            task = Loc.T("settings.automation.defaultTaskName", "自动化任务");
        string baseDir = location.Length == 0
            ? Loc.T("settings.automation.programRoot", "程序根目录") : location;
        ResultsPreviewText.Text = Loc.T("settings.automation.resultsPreview",
            "目录结构：{0}\\{1}\\{2}\\{3}\\（同名任务自动加 -1、-2；内含「结果.txt」与下载的原始文件）",
            baseDir, rootName, $"{DateTime.Now:yyyy-MM-dd}", task);
    }

    private void ResultsOption_TextChanged(object sender, TextChangedEventArgs e) => UpdateResultsPreview();

    /// <summary>在资源管理器中打开结果根目录（不存在时先创建）。</summary>
    private async void OpenResultsButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            // 让后端按自己的规则解析"程序根目录"，避免前端猜路径
            string json = await _backend.SendRequestAsync(new { action = "automation_status" });
            using JsonDocument doc = JsonDocument.Parse(json);
            string root = doc.RootElement.TryGetProperty("automation", out JsonElement st)
                ? GetString(st, "results_root")
                : "";
            if (root.Length == 0)
            {
                SaveHintText.Text = Loc.T("settings.automation.resultsDirMissing",
                    "⚠️ 未取到结果目录，请先保存设置");
                return;
            }
            Directory.CreateDirectory(root);
            Process.Start(new ProcessStartInfo { FileName = root, UseShellExecute = true });
            SaveHintText.Text = Loc.T("settings.automation.opened", "已打开：{0}", root);
        }
        catch (Exception ex)
        {
            SaveHintText.Text = Loc.T("settings.automation.resultsDirOpenFailed",
                "❌ 打开结果目录失败：{0}", ex.Message);
        }
    }

    private void BrowseResultsButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.OpenFolderDialog
        {
            Title = Loc.T("settings.automation.resultsLocationPrompt", "选择批量处理结果的保存位置"),
        };
        if (dialog.ShowDialog(this) == true)
        {
            ResultsLocationBox.Text = dialog.FolderName;
            UpdateResultsPreview();
        }
    }

    // ---------- 自动化：测试选择器 ----------

    private async void TestSelectorButton_Click(object sender, RoutedEventArgs e)
    {
        string url = TaskUrlBox.Text.Trim();
        string selector = TaskSelectorBox.Text.Trim();
        if (url.Length == 0 || selector.Length == 0)
        {
            AppendLog(Loc.T("settings.automation.needUrlAndSelector", "❌ 请先填写任务列表网址与元素位置"));
            return;
        }
        TestSelectorButton.IsEnabled = false;
        AppendLog(Loc.T("settings.automation.testingSelector", "🔍 正在测试选择器：{0}", selector));
        AutomationStatusText.Text = Loc.T("settings.automation.stateTesting",
            "状态：测试选择器中（浏览器已打开）");
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "automation_test", url, selector });
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            AppendLog(content);
        }
        catch (Exception ex)
        {
            AppendLog(Loc.T("settings.automation.testFailed", "❌ 测试失败：{0}", ex.Message));
        }
        finally
        {
            TestSelectorButton.IsEnabled = true;
            AutomationStatusText.Text = Loc.T("settings.automation.stateIdle", "状态：空闲");
        }
    }

    // ---------- 自动化：运行与停止 ----------

    private async void RunOnceButton_Click(object sender, RoutedEventArgs e)
    {
        if (_automationRunning)
            return;
        if (SafetyCheck.IsChecked != true)
        {
            MessageBox.Show(this,
                Loc.T("settings.automation.safetyRequiredText", "请先阅读并勾选安全协议，再运行自动化。"),
                Loc.T("settings.automation.safetyRequiredTitle", "需要同意安全协议"),
                MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        _automationRunning = true;
        RunOnceButton.IsEnabled = false;
        StopAutomationButton.IsEnabled = true;
        AutomationStatusText.Text = Loc.T("settings.automation.stateRunning", "状态：运行中（浏览器已打开）");
        AppendLog(Loc.T("settings.automation.runStarted", "▶ 开始运行自动化 — {0}", $"{DateTime.Now:HH:mm:ss}"));
        try
        {
            string final = await _backend.SendStreamRequestAsync(
                new { action = "automation_run" },
                delta => AppendLog(delta));
            AppendLog($"✅ {final}");
            AutomationStatusText.Text = Loc.T("settings.automation.stateDone", "状态：已完成");
        }
        catch (Exception ex)
        {
            AppendLog(Loc.T("settings.automation.runFailed", "❌ 自动化失败：{0}", ex.Message));
            AutomationStatusText.Text = Loc.T("settings.automation.stateFailed", "状态：失败");
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
        AppendLog(Loc.T("settings.automation.stopRequested", "⏹ 已请求停止自动化，等待当前步骤收尾…"));
        try
        {
            await _backend.SendFireAndForgetAsync(new { action = "stop" });
        }
        catch (Exception ex)
        {
            AppendLog(Loc.T("settings.automation.stopFailed", "❌ 停止请求失败：{0}", ex.Message));
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

    /// <summary>
    /// 「市场 → 已安装」按钮：v0.2.1 起切到独立的「已安装」Tab（展示加载状态、命令、
    /// 来源、错误详情与卸载/导出等操作），而不是只在状态栏里列一串 ID。
    /// </summary>
    private void MarketInstalledButton_Click(object sender, RoutedEventArgs e)
    {
        ShowTab(Loc.T("settings.tab.installed"));
        _ = LoadSkillsAsync();
    }

    private async Task SearchMarketAsync()
    {
        string keyword = MarketSearchBox.Text.Trim();
        string sort = (MarketSortCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "best";
        MarketStatusText.Text = keyword.Length == 0
            ? Loc.T("settings.market.loadingList", "正在获取技能列表…")
            : Loc.T("settings.market.searching", "正在搜索「{0}」…", keyword);
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
                MarketStatusText.Text = Loc.T("settings.market.unavailable",
                    "⚠️ 技能市场不可用（后端未返回数据）");
                return;
            }
            RenderMarket(market);
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = Loc.T("settings.market.requestFailed", "❌ 技能市场请求失败：{0}", ex.Message);
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
                    Badge = installed ? Loc.T("settings.market.badgeInstalled", "● 已安装")
                                      : (verified ? Loc.T("settings.market.badgeVerified", "✔ 已验证")
                                                  : Loc.T("settings.market.badgeUnverified", "⚠ 未验证")),
                    Stars = $"⭐ {stars}",
                    Meta = string.Join(" · ", new[]
                    {
                        version.Length > 0 ? $"v{version}" : "",
                        pushed.Length > 0 ? Loc.T("settings.market.updatedAt", "更新于 {0}", pushed) : "",
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
            ? Loc.T("settings.market.emptyResult",
                "没有匹配的技能。\n可换关键词（技能仓库需带 GitHub topic「echodesk-plugin」）。")
            : "";
        MarketStatusText.Text = _marketItems.Count == 0
            ? Loc.T("settings.market.emptyStatus", "结果为空。")
            : Loc.T("settings.market.resultCount", "共 {0} 个结果，本页 {1} 个", total, _marketItems.Count)
              + (offline ? Loc.T("settings.market.offlineNote", "（离线：展示本地缓存快照）") : "");
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
                MarketStatusText.Text = Loc.T("settings.market.installedListFailed", "⚠️ 无法读取已安装技能列表");
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
            MarketStatusText.Text = count == 0
                ? Loc.T("settings.market.noInstalled", "本机尚未安装任何技能。")
                : Loc.T("settings.market.installedCount", "已安装 {0} 个技能：\n{1}", count, sb);
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = Loc.T("settings.market.installedListLoadFailed",
                "❌ 读取已安装列表失败：{0}", ex.Message);
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
                MarketStatusText.Text = Loc.T("settings.market.detailUnavailable", "⚠️ 无法获取技能详情");
                return;
            }
            var sb = new StringBuilder();
            sb.AppendLine(Loc.T("settings.market.detailSkill", "技能：{0}", item.FullName));
            AppendDetail(sb, Loc.T("settings.market.detailVersion", "版本"), detail, "version");
            AppendDetail(sb, Loc.T("settings.market.detailSource", "来源"), detail, "html_url");
            AppendDetail(sb, Loc.T("settings.market.detailEntry", "入口文件"), detail, "entry");
            AppendDetail(sb, Loc.T("settings.market.detailHasPrepare", "包含 prepare 脚本"), detail, "has_prepare");
            if (detail.TryGetProperty("dependencies", out JsonElement deps) && deps.ValueKind == JsonValueKind.Array)
            {
                var list = deps.EnumerateArray().Select(x => x.ToString()).ToList();
                sb.AppendLine(Loc.T("settings.market.detailDependencies", "依赖：{0}",
                    list.Count == 0 ? Loc.T("settings.market.detailNone", "无") : string.Join(", ", list)));
            }
            if (detail.TryGetProperty("readme_excerpt", out JsonElement readme))
            {
                sb.AppendLine();
                sb.AppendLine(Loc.T("settings.market.detailReadme", "README 摘要："));
                sb.AppendLine(Shorten(readme.GetString() ?? "", 800));
            }
            MessageBox.Show(this, sb.ToString(), Loc.T("settings.market.detailDialogTitle", "技能详情"),
                MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = Loc.T("settings.market.detailFailed", "❌ 获取详情失败：{0}", ex.Message);
        }
    }

    private static void AppendDetail(StringBuilder sb, string label, JsonElement detail, string key)
    {
        if (!detail.TryGetProperty(key, out JsonElement el))
            return;
        string value = el.ValueKind switch
        {
            JsonValueKind.String => el.GetString() ?? "",
            JsonValueKind.True => Loc.T("main.dialog.yes", "是"),
            JsonValueKind.False => Loc.T("main.dialog.no", "否"),
            _ => el.ToString(),
        };
        if (value.Length > 0)
            sb.AppendLine(Loc.T("settings.market.detailLine", "{0}：{1}", label, value));
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
                Loc.T("settings.market.uninstallConfirm",
                    "确定卸载技能「{0}」吗？\n（将删除其安装目录并从技能清单移除）", item.Name),
                Loc.T("settings.market.uninstallTitle", "卸载技能"),
                MessageBoxButton.YesNo, MessageBoxImage.Question);
            if (confirm != MessageBoxResult.Yes)
                return;
            await CallMarketAsync(new { action = "market_uninstall", repo = item.FullName },
                Loc.T("settings.button.uninstall", "卸载"));
            return;
        }

        var first = MessageBox.Show(this,
            Loc.T("settings.market.installConfirmText",
                "即将从 GitHub 安装第三方技能：\n\n技能：{0}\n仓库：{1}\n版本：{2}\n来源：{3}\n\n⚠️ 安全提示：第三方技能以你的本地账户权限运行，EchoDesk 不对其安全性负责。\n安装前会读取并展示其 skill.json 以了解权限与依赖。是否继续？",
                item.Name, item.FullName,
                item.Version.Length > 0 ? item.Version : Loc.T("settings.market.state.unknown", "未知"),
                item.HtmlUrl),
            Loc.T("settings.market.installConfirmTitle", "安装第三方技能"),
            MessageBoxButton.YesNo, MessageBoxImage.Warning);
        if (first != MessageBoxResult.Yes)
            return;

        await CallMarketAsync(new { action = "market_install", repo = item.FullName },
            Loc.T("settings.button.install", "安装"));
    }

    private async Task CallMarketAsync(object request, string verb)
    {
        MarketStatusText.Text = Loc.T("settings.market.callRunning",
            "正在{0}…（下载源码并校验 skill.json）", verb);
        try
        {
            string json = await _backend.SendRequestAsync(request);
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = doc.RootElement.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "" : "";
            MarketStatusText.Text = content.Length > 0 ? content
                : Loc.T("settings.market.callDone", "{0}完成", verb);
            await SearchMarketAsync();  // 刷新「已安装」徽标
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = Loc.T("settings.market.callFailed", "❌ {0}失败：{1}", verb, ex.Message);
        }
    }

    private void MarketList_MouseDoubleClick(object sender, System.Windows.Input.MouseButtonEventArgs e)
    {
        if (MarketList.SelectedItem is MarketItem item)
            _ = ShowDetailAsync(item);
    }

    private void CloseButton_Click(object sender, RoutedEventArgs e) => Close();

    /// <summary>供主窗口 Ctrl+M 调用：切到技能市场 Tab 并刷新一次列表。</summary>
    public void ShowMarketTab() => ShowTab(Loc.T("settings.tab.market"), refreshMarket: true);

    /// <summary>
    /// 切到指定标题的 Tab（供主窗口状态区点击跳转）。refreshMarket 为真时顺带刷新市场列表。
    ///
    /// v0.2.1：调用方传的可能是本地化后的标题，也可能传中文原名（主窗口里的
    /// 「设置 → 模型」之类提示）；两种都认，避免界面语言一变跳转就失效。
    /// </summary>
    public void ShowTab(string header, bool refreshMarket = false)
    {
        for (int i = 0; i < Tabs.Items.Count; i++)
        {
            if (Tabs.Items[i] is not TabItem tab)
                continue;
            string current = tab.Header?.ToString() ?? "";
            if (!Equals(current, header) && !Equals(HeaderKeyOf(i), header))
                continue;
            Tabs.SelectedIndex = i;
            break;
        }
        if (refreshMarket)
        {
            _marketPage = 1;
            _ = SearchMarketAsync();
        }
    }

    /// <summary>第 i 个 Tab 在语言包里的键（用于跨语言查找 Tab）。</summary>
    private static string HeaderKeyOf(int index) => index switch
    {
        0 => "settings.tab.general",
        1 => "settings.tab.model",
        2 => "settings.tab.automation",
        3 => "settings.tab.installed",
        4 => "settings.tab.market",
        5 => "settings.tab.developer",
        _ => "",
    };

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
