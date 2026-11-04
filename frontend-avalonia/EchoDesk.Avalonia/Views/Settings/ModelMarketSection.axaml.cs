// 模型市场区块的交互逻辑（v0.2.2：把 WPF 侧的模型市场补全到跨平台版）。
//
// 这一块**以 WPF 版行为为准**（frontend/SettingsWindow.Market.cs，908 行），
// 后端协议一个字都没改。分工与 WPF 完全一致：
//
//   * `model_hub_load`（流式）：按关键词取回结果集，边取边报 `hub_progress`；
//     请求里带 `no_page=true`（结果集里不带第一页 —— 免得两处各算一遍、数字对不上）；
//   * `model_hub_query`（普通）：在**后端内存里那份结果集**上做
//     关键词筛选 → 结构化筛选 → 排序 → 分页，毫秒级进程内往返，**不重新请求远端**；
//   * `model_hub_detail`：仓库里可下载的 GGUF 文件与体积；
//   * `model_hub_download`（流式）：下载单个文件，带进度与取消；
//   * `model_hub_registry` / `model_hub_uninstall` / `model_hub_status` / `model_hub_config_set`。
//
// 三处**与 WPF 不同的实现选择**（都是平台差异，不是行为差异，逐条写清楚）：
//
// 1. **详情用就地展开的文件列表**（HubDetailPanel + HubFileList），不是弹对话框。
//    WPF 版用的是模态对话框；Avalonia 没有内置 MessageBox/对话框体系，
//    而骨架里也没有第三个窗口。就地展开还有个好处：详情与结果列表同屏可对照。
// 2. **Token 输入框用 TextBox + PasswordChar**（WPF 用的是 PasswordBox）。
//    Avalonia 的 TextBox 支持 PasswordChar，语义等价。
// 3. **分面复选框与结果行都是运行时构建**（WPF 也是运行时构建分面，这里只是没有 DataTemplate）。
//
// 一处**刻意的行为保留**：结果区的最小高度 + 外层滚动由 XAML 的 MinHeight 与
// 分页条独立成行保证 —— 任务八（窗口太小看不到搜索结果）在 WPF 侧的结论是
// "结果区设最小高度 + 外层加滚动条"，这里同样处理。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

/// <summary>市场区块需要的那几个能力（由 ModelPage 提供，避免直接依赖整个设置骨架）。</summary>
public interface IModelMarketHost
{
    /// <summary>发一个普通请求（内部会处理超时与异常类型）。</summary>
    Task<JsonDocument?> SendAsync(object request, TimeSpan? idleTimeout = null);

    /// <summary>发一个流式请求（用于 model_hub_load / model_hub_download 的进度）。</summary>
    Task SendStreamAsync(object request, Action<string, JsonElement> onEvent,
                         TimeSpan? idleTimeout = null);

    /// <summary>后端是否可用（不可用时市场给出明确说明，而不是一片空白）。</summary>
    bool BackendAvailable { get; }
}

public partial class ModelMarketSection : UserControl
{
    private IModelMarketHost? _host;

    // ---------- 状态（字段名与 WPF 版一致，便于对照两个实现） ----------
    private readonly List<HubRow> _hubPage = new();
    private readonly HashSet<string> _hubParamSelected = new();
    private readonly HashSet<string> _hubQuantSelected = new();
    private readonly HashSet<string> _hubUpdatedSelected = new();
    private DispatcherTimer? _hubFilterDebounce;
    private DispatcherTimer? _hubProgressTimer;

    private JsonElement? _hubLastQuery;
    private HubRow? _hubPinned;
    private string _hubInstalled = "any";
    private bool _hubRecommendedOnly;
    private int _hubPageNo = 1;
    private int _hubPages = 1;
    private int _hubPageSize = 20;
    private int _hubFiltered;
    private int _hubLoaded;
    private int? _hubTotalAvailable;
    private bool _hubHasMore;
    private bool _hubOffline;
    private int _hubFallbackCount;
    private int _hubElapsedSeconds;
    private bool _hubLoading;
    private bool _hubQuerying;
    private bool _hubSuppressFilterEvents;
    private string _hubFacetSignature = "";
    private string _hubStopReason = "";
    private string _hubDetailRepo = "";

    /// <summary>每页条数（任务三：20 / 50 / 100，默认 20）。</summary>
    private static readonly int[] HubPageSizes = { 20, 50, 100 };

    /// <summary>本地筛选防抖（任务书要求"输入停止后触发过滤，如 150ms"）。</summary>
    private static readonly TimeSpan HubFilterDebounce = TimeSpan.FromMilliseconds(150);

    private sealed record Option(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    /// <summary>结果行（值对象；文案在 BuildHubRow 里按当前语言拼好）。</summary>
    private sealed record HubRow(string Repo, string Name, string Tag, string Badge, string Meta,
                                 bool Recommended, bool IsDefault);

    /// <summary>详情面板里的一行（文件名 + 体积 + 是否已下载）。</summary>
    private sealed record HubFile(string Repo, string FileName, long Size, string SizeText, bool Installed);

    public ModelMarketSection()
    {
        InitializeComponent();
        BuildStaticOptions();
        HookEvents();
    }

    public void Attach(IModelMarketHost host)
    {
        _host = host;
        _ = RefreshStatusAsync();
    }

    // -----------------------------------------------------------------
    // 初始化
    // -----------------------------------------------------------------
    private void BuildStaticOptions()
    {
        HubSortCombo.ItemsSource = new List<Option>
        {
            new("downloads", () => Loc.T("settings.model.sortDownloadsAll", "历史总下载量")),
            new("likes", () => Loc.T("settings.model.sortLikes", "点赞最多")),
            new("updated", () => Loc.T("settings.model.sortUpdated", "最近更新")),
            new("params", () => Loc.T("settings.model.sortParams", "参数量")),
        };
        HubSortCombo.SelectedIndex = 0;

        HubInstalledCombo.ItemsSource = new List<Option>
        {
            new("any", () => Loc.T("settings.model.installedAny", "不限下载状态")),
            new("yes", () => Loc.T("settings.model.installedYes", "只看已下载")),
            new("no", () => Loc.T("settings.model.installedNo", "只看未下载")),
        };
        HubInstalledCombo.SelectedIndex = 0;

        HubSourceCombo.ItemsSource = new List<Option>
        {
            new("auto", () => Loc.T("settings.model.sourceAuto", "自动（优先镜像站）")),
            new("mirror", () => Loc.T("settings.model.sourceMirror", "镜像站")),
            new("official", () => Loc.T("settings.model.sourceOfficial", "官方站")),
        };
        HubSourceCombo.SelectedIndex = 0;

        HubPageSizeCombo.ItemsSource = HubPageSizes
            .Select(size => size.ToString(CultureInfo.InvariantCulture)).ToList();
        HubPageSizeCombo.SelectedIndex = 0;

        HubDetailCloseButton.Click += (_, _) => HubDetailPanel.IsVisible = false;
        HubRemoteRetryButton.Click += async (_, _) => await LoadMarketAsync(refresh: false, resume: false);
        HubFilterResetButton.Click += async (_, _) => await ResetFiltersAsync();
        HubCancelButton.Click += async (_, _) => await CancelAsync();
        HubPinnedDetailButton.Click += async (_, _) =>
        {
            if (_hubPinned is not null)
                await ShowDetailAsync(_hubPinned);
        };
    }

    private void HookEvents()
    {
        HubSearchButton.Click += async (_, _) => await LoadMarketAsync(refresh: false, resume: false);
        HubRefreshButton.Click += async (_, _) => await LoadMarketAsync(refresh: true, resume: false);
        HubMoreButton.Click += async (_, _) => await LoadMarketAsync(refresh: false, resume: true);
        HubStopButton.Click += async (_, _) => await CancelAsync();
        HubSaveTokenButton.Click += async (_, _) => await SaveConfigAsync(includeToken: true);
        HubOpenDirButton.Click += async (_, _) => await OpenModelsDirAsync();

        HubSearchBox.KeyDown += async (_, e) =>
        {
            if (e.Key != Key.Enter)
                return;
            e.Handled = true;
            await LoadMarketAsync(refresh: false, resume: false);
        };

        HubSortCombo.SelectionChanged += async (_, _) => await RefreshQueryAsync(resetPage: true, rebuildFacets: true);
        HubInstalledCombo.SelectionChanged += async (_, _) =>
        {
            if (_hubSuppressFilterEvents)
                return;
            _hubInstalled = (HubInstalledCombo.SelectedItem as Option)?.Value ?? "any";
            UpdateFilterSummary();
            await RefreshQueryAsync(resetPage: true, rebuildFacets: false);
        };
        HubRecommendedCheck.IsCheckedChanged += async (_, _) =>
        {
            if (_hubSuppressFilterEvents)
                return;
            _hubRecommendedOnly = HubRecommendedCheck.IsChecked == true;
            UpdateFilterSummary();
            await RefreshQueryAsync(resetPage: true, rebuildFacets: false);
        };
        HubPageSizeCombo.SelectionChanged += async (_, _) =>
        {
            if (HubPageSizeCombo.SelectedItem is not string text
                || !int.TryParse(text, out int size) || size <= 0)
                return;
            _hubPageSize = size;
            // 任务三要求"切换每页条数后回到第 1 页"：不回到第 1 页的话，
            // 页码可能直接超出新的总页数，看起来像"每页条数没生效"（WPF 侧踩过）。
            await RefreshQueryAsync(resetPage: true, rebuildFacets: false);
        };

        HubFilterBox.TextChanged += (_, _) =>
        {
            if (_hubSuppressFilterEvents)
                return;
            _hubFilterDebounce ??= new DispatcherTimer { Interval = HubFilterDebounce };
            _hubFilterDebounce.Stop();
            _hubFilterDebounce.Tick += async (_, _) =>
            {
                _hubFilterDebounce?.Stop();
                UpdateFilterSummary();
                await RefreshQueryAsync(resetPage: true, rebuildFacets: false);
            };
            _hubFilterDebounce.Start();
        };

        HubPageBox.KeyDown += async (_, e) =>
        {
            if (e.Key != Key.Enter)
                return;
            e.Handled = true;
            await JumpToTypedPageAsync();
        };
        HubGoPageButton.Click += async (_, _) => await JumpToTypedPageAsync();
        HubFirstPageButton.Click += async (_, _) => await GoToPageAsync(1);
        HubPrevPageButton.Click += async (_, _) => await GoToPageAsync(_hubPageNo - 1);
        HubNextPageButton.Click += async (_, _) => await GoToPageAsync(_hubPageNo + 1);
        HubLastPageButton.Click += async (_, _) => await GoToPageAsync(_hubPages);

        HubResultList.DoubleTapped += async (_, _) =>
        {
            if (HubResultList.SelectedItem is Control { DataContext: HubRow row })
                await ShowDetailAsync(row);
        };
    }

    private string CurrentSort() => (HubSortCombo.SelectedItem as Option)?.Value ?? "downloads";

    private string CurrentSource() => (HubSourceCombo.SelectedItem as Option)?.Value ?? "auto";

    private bool HasDataSet => _hubLoaded > 0 || _hubLastQuery is not null;

    // -----------------------------------------------------------------
    // 数据：加载（流式）/ 查询（本地）/ 详情 / 下载
    // -----------------------------------------------------------------

    /// <summary>
    /// 让页面把「本地模型库」那一块的状态也写进**同一条**状态行
    /// （v0.2.2 第 2 批）。
    ///
    /// 为什么不给模型库另起一行：WPF 的模型页只有一条 `HubStatusText`，
    /// 模型市场的搜索/下载与模型库的重扫/加载/删除都往上写。另起一行会让
    /// **同一个 AutomationId 在自动化树里出现两次**（探针取到哪一个不确定），
    /// 而且用户会看到两块各自"以为在说话"的状态 —— 与 WPF 行为不一致。
    /// </summary>
    public void SetStatus(string text) => HubStatusText.Text = text;

    /// <summary>页面刷新时调用：只刷新状态与（已有的）当前页，**不自动搜索**。</summary>
    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        if (HasDataSet)
            await RefreshQueryAsync(resetPage: false, rebuildFacets: false);
        else
            RenderIdleStatus();
    }

    private void RenderIdleStatus()
    {
        HubPageInfoText.Text = "";
        HubTotalText.Text = Loc.T("settings.model.totalNone", "尚未加载结果");
        HubEmptyText.Text = Loc.T("settings.model.hubIdle",
            "输入关键词搜索，或直接点「搜索」浏览热门 GGUF 模型");
        HubEmptyPanel.IsVisible = true;
        HubStatusText.Text = _host is { BackendAvailable: false }
            ? Loc.T("settings.model.backendUnavailable", "⚠️ 后端未启动：模型市场暂不可用。")
            : "";
    }

    private async Task RefreshStatusAsync()
    {
        if (_host is null)
            return;
        try
        {
            using JsonDocument? doc = await _host.SendAsync(new { action = "model_hub_status" });
            if (doc is not null && doc.RootElement.TryGetProperty("model_hub_status", out JsonElement st))
            {
                string note = GetString(st, "source_note");
                if (note.Length > 0 && !HasDataSet)
                    HubStatusText.Text = Loc.T("settings.model.statusSource", "数据源：{0}", note);
            }
        }
        catch (Exception)
        {
            // 状态查询失败不影响市场主流程（搜索时会给出真正的错误）
        }
    }

    private async Task LoadMarketAsync(bool refresh, bool resume)
    {
        if (_host is null || _hubLoading)
            return;
        _hubLoading = true;
        _hubElapsedSeconds = 0;
        HubSearchButton.IsEnabled = false;
        HubRefreshButton.IsEnabled = false;
        HubMoreButton.IsVisible = false;
        HubStopButton.IsVisible = true;
        HubLoadProgressBar.IsVisible = true;
        HubStatusText.Text = Loc.T("settings.model.searching", "正在搜索模型…");
        _hubProgressTimer ??= new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _hubProgressTimer.Tick += (_, _) =>
        {
            _hubElapsedSeconds++;
            if (_hubLoading)
                HubProgressText.Text = Loc.T("settings.model.loadProgress",
                    "⏳ 正在取回结果：已加载 {0} 条（用时 {1} 秒）…", _hubLoaded, _hubElapsedSeconds);
        };
        _hubProgressTimer.Start();

        string loadNotice = "";
        try
        {
            bool gotResult = false;
            await _host.SendStreamAsync(
                new
                {
                    action = "model_hub_load",
                    keyword = HubSearchBox.Text?.Trim() ?? "",
                    sort = CurrentSort(),
                    source = CurrentSource(),
                    page = 1,
                    page_size = _hubPageSize,
                    refresh,
                    resume,
                    // 结果集里不带第一页（本地筛选/排序/分页统一走 model_hub_query）
                    no_page = true,
                },
                onEvent: (type, root) =>
                {
                    if (type == "hub_progress"
                        && root.TryGetProperty("hub_progress", out JsonElement p))
                    {
                        _hubLoaded = GetInt(p, "loaded", 0);
                        _hubElapsedSeconds = (int)Math.Round(
                            p.TryGetProperty("elapsed", out JsonElement el)
                            && el.ValueKind == JsonValueKind.Number ? el.GetDouble() : 0);
                        HubProgressText.Text = Loc.T("settings.model.loadProgress",
                            "⏳ 正在取回结果：已加载 {0} 条（用时 {1} 秒）…", _hubLoaded, _hubElapsedSeconds);
                    }
                    else if (type == "hub_result" && root.TryGetProperty("model_hub", out JsonElement hub))
                    {
                        gotResult = true;
                        ApplyDataset(hub);
                    }
                },
                idleTimeout: TimeSpan.FromMinutes(10));
            if (!gotResult)
                loadNotice = Loc.T("settings.model.hubUnavailable",
                    "⚠️ 模型中心不可用（后端未返回数据）");
        }
        catch (Exception ex)
        {
            loadNotice = Loc.T("settings.model.searchFailed", "❌ 搜索失败：{0}", ex.Message);
        }
        finally
        {
            _hubLoading = false;
            _hubProgressTimer.Stop();
            HubSearchButton.IsEnabled = true;
            HubRefreshButton.IsEnabled = true;
            HubStopButton.IsVisible = false;
            HubLoadProgressBar.IsVisible = false;
            HubProgressText.Text = "";
            // 数据集换了之后第一页要重新算：统一走本地查询，保证三层数字与分页一致
            await RefreshQueryAsync(resetPage: true, rebuildFacets: true);
            // 加载失败的原因补在最前面：上面那次查询会把状态行整条重写，把真正的错误盖掉
            // （WPF 侧的 UI 探针实测到过：用户看到的是"怎么点都没反应"而不是"网络不可达"）
            if (loadNotice.Length > 0)
                HubStatusText.Text = loadNotice + "　" + HubStatusText.Text;
        }
    }

    /// <summary>把加载结果里与"列表之外"有关的状态记下来（排序/筛选/分页仍由查询动作给）。</summary>
    private void ApplyDataset(JsonElement hub)
    {
        _hubOffline = GetBool(hub, "offline");
        _hubHasMore = GetBool(hub, "has_more");
        _hubStopReason = GetString(hub, "stop_reason");
        _hubFallbackCount = GetInt(hub, "downloads_fallback_count", 0);
        _hubFacetSignature = "";               // 数据集变了，分面选项与计数都要重建
        _hubTotalAvailable = hub.TryGetProperty("total_available", out JsonElement total)
                             && total.ValueKind == JsonValueKind.Number
            ? total.GetInt32() : null;
    }

    /// <summary>
    /// 发一次本地查询。返回**是否真的发出去了** —— 调用方（尤其是探针）要能区分
    /// "查询做完了"与"被闸门挡回去了"，否则断言会读到过期状态而误判。
    /// </summary>
    private async Task<bool> RefreshQueryAsync(bool resetPage, bool rebuildFacets)
    {
        if (_host is null || _hubQuerying)
            return false;
        _hubQuerying = true;
        if (resetPage)
            _hubPageNo = 1;
        try
        {
            using JsonDocument? doc = await _host.SendAsync(
                new
                {
                    action = "model_hub_query",
                    keyword = HubFilterBox.Text?.Trim() ?? "",
                    sort = CurrentSort(),
                    page = _hubPageNo,
                    page_size = _hubPageSize,
                    filters = BuildFilters(),
                },
                idleTimeout: TimeSpan.FromSeconds(60));
            if (doc is null)
            {
                HubStatusText.Text = Loc.T("settings.model.queryNoReply",
                    "❌ 本地筛选没有响应（后端可能正忙）");
                return false;
            }
            if (doc.RootElement.TryGetProperty("hub_query", out JsonElement query))
                RenderQuery(query, rebuildFacets);
            else if (GetBool(doc.RootElement, "busy"))
                HubStatusText.Text = Loc.T("settings.model.backendBusy",
                    "⏳ 后端正在处理其他任务（例如正在生成回复），稍后会自动生效。");
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.queryFailed", "❌ 本地筛选失败：{0}", ex.Message);
        }
        finally
        {
            _hubQuerying = false;
        }
        return true;
    }

    /// <summary>
    /// 等到"没有别的查询在跑"再发这一次查询。
    ///
    /// 为什么探针需要它：界面上的每一次改动都会**自己**触发一次查询
    /// （下拉框 SelectionChanged、筛选框 150ms 防抖），探针紧接着也发一次，
    /// 两次撞在一起时后发的那次会被 <c>_hubQuerying</c> 闸门挡回去 ——
    /// 于是探针读到的是**上一次**的状态，断言就会误判成"功能没生效"
    /// （实测踩到：每页条数改成 50 之后总页数仍是 250）。
    /// 真实用户不会遇到这个问题（他只是看到两次查询合成一次），所以这只是在
    /// 探针侧补一个"等闸门放开"的动作，没有改任何产品行为。
    /// </summary>
    private async Task<bool> QueryWhenIdleAsync(bool resetPage, bool rebuildFacets)
    {
        for (int attempt = 0; attempt < 20; attempt++)
        {
            if (!_hubQuerying)
                return await RefreshQueryAsync(resetPage, rebuildFacets);
            await Task.Delay(100);
        }
        return await RefreshQueryAsync(resetPage, rebuildFacets);
    }

    private object BuildFilters() => new
    {
        // 同维度多选是"或"、不同维度之间是"与"（见 backend/model_query.py 的说明）
        @params = _hubParamSelected.ToArray(),
        quant = _hubQuantSelected.ToArray(),
        updated = _hubUpdatedSelected.ToArray(),
        installed = _hubInstalled,
        recommended = _hubRecommendedOnly,
    };

    private async Task ResetFiltersAsync()
    {
        _hubSuppressFilterEvents = true;
        try
        {
            _hubParamSelected.Clear();
            _hubQuantSelected.Clear();
            _hubUpdatedSelected.Clear();
            _hubInstalled = "any";
            _hubRecommendedOnly = false;
            HubFilterBox.Text = "";
            HubInstalledCombo.SelectedIndex = 0;
            HubRecommendedCheck.IsChecked = false;
            foreach (WrapPanel panel in new[] { HubParamFacet, HubQuantFacet, HubUpdatedFacet })
                foreach (Control child in panel.Children)
                    if (child is CheckBox box)
                        box.IsChecked = false;
        }
        finally
        {
            _hubSuppressFilterEvents = false;
        }
        UpdateFilterSummary();
        await RefreshQueryAsync(resetPage: true, rebuildFacets: true);
    }

    private async Task GoToPageAsync(int page)
    {
        _hubPageNo = Math.Clamp(page, 1, Math.Max(1, _hubPages));
        await RefreshQueryAsync(resetPage: false, rebuildFacets: false);
    }

    private async Task JumpToTypedPageAsync()
    {
        string text = HubPageBox.Text?.Trim() ?? "";
        if (!int.TryParse(text, out int wanted))
        {
            // 非法输入不跳页，并把原因写在分页条旁边（**不能**写进折叠的筛选面板里：
            // 折叠内容不在 UIA 树里、用户也看不见 —— WPF 侧踩过）
            // 包值是「⚠️ 请输入 {0}–{1} 之间的页码」：两个参数都要传，
            // 只传一个会抛 FormatException、界面露出 {0}/{1}（硬编码清查 §5.3 C-1）
            HubPagerHint.Text = Loc.T("settings.model.pageInvalid",
                "⚠️ 请输入 {0}–{1} 之间的页码", 1, Math.Max(1, _hubPages));
            return;
        }
        int clamped = Math.Clamp(wanted, 1, Math.Max(1, _hubPages));
        _hubPageNo = clamped;
        await RefreshQueryAsync(resetPage: false, rebuildFacets: false);
        HubPagerHint.Text = clamped != wanted
            ? Loc.T("settings.model.pageCorrected", "⚠️ 页码超出范围，已显示第 {0} 页", clamped)
            : "";
    }

    private async Task CancelAsync()
    {
        if (_host is null)
            return;
        try
        {
            await _host.SendAsync(new { action = "stop" }, TimeSpan.FromSeconds(30));
            HubStatusText.Text = Loc.T("settings.model.stopSent", "⏹ 已发送停止请求");
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.stopFailed", "❌ 停止失败：{0}", ex.Message);
        }
    }

    private async Task SaveConfigAsync(bool includeToken)
    {
        if (_host is null)
            return;
        var patch = new Dictionary<string, object?> { ["source"] = CurrentSource() };
        string token = HubTokenBox.Text?.Trim() ?? "";
        if (includeToken && token.Length > 0)
            patch["hf_token"] = token;
        try
        {
            using JsonDocument? doc = await _host.SendAsync(
                new { action = "model_hub_config_set", model_hub = patch });
            string content = "";
            if (doc is not null && doc.RootElement.TryGetProperty("content", out JsonElement c)
                && c.ValueKind == JsonValueKind.String)
                content = c.GetString() ?? "";
            HubStatusText.Text = content.Length > 0
                ? content : Loc.T("settings.model.saved", "已保存");
            if (doc is not null && doc.RootElement.TryGetProperty("model_hub", out JsonElement st))
                HubStatusText.Text += "　" + Loc.T("settings.model.statusSource", "数据源：{0}",
                    GetString(st, "source_note"));
            // Token 保存完就清空输入框：口令留在界面上没有必要（WPF 侧同样处理）
            if (includeToken && token.Length > 0)
                HubTokenBox.Text = "";
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.saveFailed", "❌ 保存失败：{0}", ex.Message);
        }
    }

    private async Task OpenModelsDirAsync()
    {
        if (_host is null)
            return;
        try
        {
            using JsonDocument? doc = await _host.SendAsync(new { action = "model_hub_status" });
            string dir = doc is not null
                         && doc.RootElement.TryGetProperty("model_hub_status", out JsonElement st)
                ? GetString(st, "models_dir") : "";
            if (dir.Length == 0)
            {
                HubStatusText.Text = Loc.T("settings.model.dirMissing", "⚠️ 未取到模型目录");
                return;
            }
            // 跨平台：交给平台自己打开（不 P/Invoke —— Avalonia 版刻意不引 Windows 专属能力，
            // 见 EchoDesk.Avalonia.csproj 头部说明）。失败不抛，改成一行状态提示。
            if (!TryOpenDirectory(dir))
            {
                HubStatusText.Text = Loc.T("settings.model.dirOpenFailed",
                    "❌ 打开模型目录失败：{0}", dir);
                return;
            }
            HubStatusText.Text = Loc.T("settings.model.dirOpened", "已打开模型目录：{0}", dir);
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.dirOpenFailed", "❌ 打开模型目录失败：{0}", ex.Message);
        }
    }

    // -----------------------------------------------------------------
    // 详情与下载
    // -----------------------------------------------------------------
    private async Task ShowDetailAsync(HubRow row)
    {
        if (_host is null)
            return;
        _hubDetailRepo = row.Repo;
        HubDetailTitle.Text = Loc.T("settings.model.detailTitle", "🧩 {0} 的可用文件", row.Repo);
        HubDetailPanel.IsVisible = true;
        HubFileList.ItemsSource = new List<Control>
        {
            new TextBlock { Text = Loc.T("settings.model.detailLoading", "正在读取文件列表…"), FontSize = 11 },
        };
        try
        {
            using JsonDocument? doc = await _host.SendAsync(
                new { action = "model_hub_detail", repo = row.Repo, source = CurrentSource() });
            if (doc is null || !doc.RootElement.TryGetProperty("detail", out JsonElement detail))
            {
                HubFileList.ItemsSource = new List<Control>
                {
                    new TextBlock { Text = Loc.T("settings.model.detailFailed", "❌ 读取文件列表失败"), FontSize = 11 },
                };
                return;
            }
            var rows = new List<HubFile>();
            if (detail.TryGetProperty("files", out JsonElement files) && files.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement file in files.EnumerateArray())
                {
                    long size = file.TryGetProperty("size", out JsonElement sz)
                                && sz.ValueKind == JsonValueKind.Number ? sz.GetInt64() : 0;
                    rows.Add(new HubFile(
                        row.Repo,
                        GetString(file, "filename"),
                        size,
                        GetString(file, "size_human").Length > 0
                            ? GetString(file, "size_human") : HumanSize(size),
                        GetBool(file, "installed")));
                }
            }
            HubFileList.ItemsSource = rows.Count > 0
                ? rows.Select(BuildFileRow).ToList<Control>()
                : new List<Control>
                {
                    new TextBlock
                    {
                        Text = Loc.T("settings.model.detailEmpty", "该仓库里没有可用的 GGUF 文件"),
                        FontSize = 11,
                    },
                };
        }
        catch (Exception ex)
        {
            HubFileList.ItemsSource = new List<Control>
            {
                new TextBlock
                {
                    Text = Loc.T("settings.model.detailFailedReason", "❌ 读取文件列表失败：{0}", ex.Message),
                    FontSize = 11,
                    TextWrapping = TextWrapping.Wrap,
                },
            };
        }
    }

    private Control BuildFileRow(HubFile file)
    {
        var panel = new StackPanel { Spacing = 2 };
        panel.Children.Add(new TextBlock
        {
            Text = $"{file.FileName}　{file.SizeText}",
            FontSize = 12,
            TextWrapping = TextWrapping.Wrap,
        });
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var action = new Button
        {
            Content = file.Installed
                ? Loc.T("settings.model.actionDownloaded", "已下载")
                : Loc.T("settings.model.actionDownload", "下载"),
            Padding = new Thickness(10, 3),
            FontSize = 11,
            IsEnabled = !file.Installed,
        };
        action.Click += async (_, _) => await DownloadAsync(file);
        row.Children.Add(action);
        panel.Children.Add(row);
        return new Border
        {
            Padding = new Thickness(8, 6),
            Margin = new Thickness(0, 2),
            CornerRadius = new CornerRadius(4),
            BorderThickness = new Thickness(1),
            BorderBrush = TryBrush("CardBorderBrush"),
            Child = panel,
        };
    }

    private async Task DownloadAsync(HubFile file)
    {
        if (_host is null)
            return;
        HubStopButton.IsVisible = true;
        HubLoadProgressBar.IsVisible = true;
        HubProgressText.Text = Loc.T("settings.model.downloadStarting",
            "⏳ 正在下载 {0}…", file.FileName);
        try
        {
            await _host.SendStreamAsync(
                new
                {
                    action = "model_hub_download",
                    repo = file.Repo,
                    filename = file.FileName,
                    size = file.Size,
                    source = CurrentSource(),
                },
                onEvent: (type, root) =>
                {
                    // 后端下载进度用 content 文本 + progress 字段（与 WPF 侧同一消费方式）
                    if (type == "progress")
                    {
                        string content = GetString(root, "content");
                        if (content.Length > 0)
                            HubProgressText.Text = content;
                    }
                    else if (type == "download_done")
                    {
                        HubProgressText.Text = GetString(root, "content");
                    }
                },
                idleTimeout: null);
            HubProgressText.Text = Loc.T("settings.model.downloadDone", "✅ 下载完成：{0}", file.FileName);
            // 下载完刷新一次列表徽标（命中缓存时几乎瞬时）
            await LoadMarketAsync(refresh: false, resume: false);
        }
        catch (Exception ex)
        {
            HubProgressText.Text = Loc.T("settings.model.downloadFailed", "❌ 下载失败：{0}", ex.Message);
        }
        finally
        {
            HubStopButton.IsVisible = false;
            HubLoadProgressBar.IsVisible = false;
        }
    }

    // -----------------------------------------------------------------
    // 渲染：结果行 / 分面 / 分页 / 状态
    // -----------------------------------------------------------------
    private void RenderQuery(JsonElement query, bool rebuildFacets)
    {
        _hubLastQuery = query.Clone();
        _hubPageNo = GetInt(query, "page", 1);
        _hubPages = Math.Max(1, GetInt(query, "pages", 1));
        _hubPageSize = GetInt(query, "page_size", _hubPageSize);
        _hubFiltered = GetInt(query, "filtered", 0);
        _hubLoaded = GetInt(query, "loaded", 0);
        _hubHasMore = GetBool(query, "has_more");
        bool pageCorrected = GetBool(query, "page_corrected");
        _hubTotalAvailable = query.TryGetProperty("total_available", out JsonElement total)
                             && total.ValueKind == JsonValueKind.Number
            ? total.GetInt32() : null;
        _hubOffline = GetBool(query, "offline");

        _hubPage.Clear();
        if (query.TryGetProperty("items", out JsonElement items) && items.ValueKind == JsonValueKind.Array)
            foreach (JsonElement item in items.EnumerateArray())
                _hubPage.Add(BuildHubRow(item));

        if (query.TryGetProperty("pinned", out JsonElement pinned)
            && pinned.ValueKind == JsonValueKind.Array && pinned.GetArrayLength() > 0)
            _hubPinned = BuildHubRow(pinned[0]);
        RenderPinned();

        HubResultList.ItemsSource = _hubPage.Select(BuildResultRow).ToList<Control>();

        if (rebuildFacets)
            RebuildFacets(query);

        RenderPager(pageCorrected);
        RenderStatus(query);
    }

    private HubRow BuildHubRow(JsonElement item)
    {
        string repo = GetString(item, "repo");
        int downloads = GetInt(item, "downloads", 0);
        int likes = GetInt(item, "likes", 0);
        string origin = GetString(item, "origin");
        string metric = GetString(item, "downloads_source");
        bool remote = origin == "remote";
        bool recommended = GetBool(item, "recommended");
        bool isDefault = GetBool(item, "is_default");
        string tag = isDefault
            ? Loc.T("settings.model.tagDefault", "(默认)")
            : (recommended ? Loc.T("settings.model.tagRecommended", "(推荐)") : "");

        // 下载量口径（任务一）：历史总下载量；缺字段的条目注明已回退到 30 天口径，
        // 本地/默认来源没有远端元数据，不显示成"0 次下载"。
        string downloadsText = "";
        if (remote && downloads > 0)
        {
            string label = metric == "default_fallback"
                ? Loc.T("settings.model.downloadsRecent", "最近 30 天")
                : Loc.T("settings.model.downloadsAllTime", "历史总下载量");
            downloadsText = $"⬇ {downloads.ToString("N0", CultureInfo.InvariantCulture)}（{label}）";
        }

        double paramB = 0;
        if (item.TryGetProperty("param_b", out JsonElement pb) && pb.ValueKind == JsonValueKind.Number)
            paramB = pb.GetDouble();

        string meta = string.Join(" · ", new[]
        {
            GetString(item, "author"),
            downloadsText,
            likes > 0 ? $"⭐ {likes}" : "",
            paramB > 0 ? Loc.T("settings.model.paramSize", "{0}B 参数", paramB.ToString("0.#")) : "",
            GetString(item, "quant_code"),
            GetString(item, "updated_at").Length > 0
                ? Loc.T("settings.model.updatedAt", "更新于 {0}", GetString(item, "updated_at"))
                : "",
            GetString(item, "note_key") switch
            {
                "hub.note.default_model" => Loc.T("settings.model.noteDefault",
                    "默认模型（config.json 里配置的那个）"),
                "hub.note.local_model" => Loc.T("settings.model.noteLocal", "本地已下载"),
                _ => "",
            },
        }.Where(x => x.Length > 0));

        return new HubRow(
            repo,
            GetString(item, "name"),
            tag,
            GetBool(item, "installed") ? Loc.T("settings.model.badgeDownloaded", "● 已下载")
                : (GetBool(item, "gated") ? Loc.T("settings.model.badgeGated", "🔒 需登录") : ""),
            meta,
            recommended,
            isDefault);
    }

    private Control BuildResultRow(HubRow row)
    {
        var left = new StackPanel { Spacing = 2 };
        var titleRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 4 };
        if (row.Tag.Length > 0)
            titleRow.Children.Add(new TextBlock
            {
                Text = row.Tag,
                FontSize = 12,
                FontWeight = FontWeight.Bold,
                VerticalAlignment = VerticalAlignment.Center,
                Foreground = TryBrush("AccentBrush"),
            });
        titleRow.Children.Add(new TextBlock
        {
            Text = row.Name,
            FontWeight = FontWeight.Bold,
            Foreground = TryBrush("AccentBrush"),
        });
        if (row.Badge.Length > 0)
            titleRow.Children.Add(new TextBlock
            {
                Text = row.Badge,
                FontSize = 11,
                Margin = new Thickness(8, 0, 0, 0),
                Opacity = 0.75,
            });
        left.Children.Add(titleRow);
        left.Children.Add(new TextBlock
        {
            Text = row.Meta,
            FontSize = 10,
            TextWrapping = TextWrapping.Wrap,
            MaxWidth = 540,
            Opacity = 0.75,
        });

        var detail = new Button
        {
            Content = Loc.T("settings.model.actionDetail", "详情"),
            Padding = new Thickness(10, 3),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
        };
        detail.Click += async (_, _) => await ShowDetailAsync(row);

        var grid = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), Margin = new Thickness(6, 5) };
        Grid.SetColumn(left, 0);
        Grid.SetColumn(detail, 1);
        grid.Children.Add(left);
        grid.Children.Add(detail);
        return grid;
    }

    /// <summary>默认模型单独一行钉在结果上方，标注来源，且**不计入下面的分页条数**。</summary>
    private void RenderPinned()
    {
        if (_hubPinned is null)
        {
            HubPinnedRow.IsVisible = false;
            return;
        }
        HubPinnedTag.Text = Loc.T("settings.model.pinnedTag", "📌 默认模型");
        HubPinnedName.Text = _hubPinned.Name;
        HubPinnedMeta.Text = Loc.T("settings.model.pinnedMeta",
            "来源：config.json 里的默认模型仓库 {0}　·　固定显示，不计入下方的分页条数", _hubPinned.Repo);
        HubPinnedRow.IsVisible = true;
    }

    /// <summary>
    /// "共 N 条"的措辞（总条数显示与状态行共用，避免两处口径不一致）。
    /// 三种情况分清楚：取完了 → 精确总数；撞上加载上限 → "N 条以上（远端还有更多）"；
    /// **一条都没加载** → 什么都不编（早期版本这里会说"共 0 条以上（远端还有更多）"，
    /// 两句话都是假的 —— WPF 侧的 UI 探针实测到过）。
    /// </summary>
    private string TotalText()
    {
        if (_hubTotalAvailable is int total)
            return Loc.T("settings.model.totalCount", "共 {0} 条", total);
        if (_hubHasMore)
            return Loc.T("settings.model.totalMore", "共 {0} 条以上（远端还有更多）", _hubLoaded);
        if (_hubLoaded == 0 && _hubFiltered == 0)
            return Loc.T("settings.model.totalNone", "尚未加载结果");
        return Loc.T("settings.model.totalCount", "共 {0} 条", _hubFiltered);
    }

    private void RenderPager(bool pageCorrected)
    {
        HubPageInfoText.Text = Loc.T("settings.model.pageInfo", "第 {0} / {1} 页", _hubPageNo, _hubPages);
        HubTotalText.Text = TotalText();

        bool first = _hubPageNo <= 1;
        bool last = _hubPageNo >= _hubPages;
        HubFirstPageButton.IsEnabled = !first;
        HubPrevPageButton.IsEnabled = !first;
        HubNextPageButton.IsEnabled = !last;
        HubLastPageButton.IsEnabled = !last;
        HubPageBox.Text = _hubPageNo.ToString(CultureInfo.InvariantCulture);
        HubPageBox.IsEnabled = _hubPages > 1;
        HubGoPageButton.IsEnabled = _hubPages > 1;
        HubMoreButton.IsVisible = _hubHasMore && !_hubLoading && _hubStopReason != "error";
        HubPagerHint.Text = pageCorrected
            ? Loc.T("settings.model.pageCorrected", "⚠️ 页码超出范围，已显示第 {0} 页", _hubPageNo)
            : "";
    }

    private void RenderStatus(JsonElement query)
    {
        string emptyReason = GetString(query, "empty_reason");
        bool noItems = _hubPage.Count == 0;

        HubEmptyPanel.IsVisible = noItems;
        HubRemoteRetryButton.IsVisible = emptyReason == "local_no_match";
        if (noItems)
        {
            HubEmptyText.Text = emptyReason == "local_no_match"
                ? Loc.T("settings.model.emptyLocalMatch", "本地结果中没有匹配，是否重新搜索远端？")
                : (_hubLoaded == 0
                    ? Loc.T("settings.model.emptyDataset",
                        "结果为空。可换关键词（模型需为 GGUF 格式），或点「搜索」加载。")
                    : Loc.T("settings.model.emptyAfterFilter", "当前筛选条件下没有结果。"));
        }

        var parts = new List<string>
        {
            // 三层数字（任务四）：本地筛选后 / 已加载 / 共 N 条
            Loc.T("settings.model.stats", "本地筛选后 {0} 条 / 已加载 {1} 条 / {2}",
                _hubFiltered, _hubLoaded, TotalText()),
        };
        string sourceNote = GetString(query, "source_note");
        if (sourceNote.Length > 0)
            parts.Add(Loc.T("settings.model.statusSource", "数据源：{0}", sourceNote));
        // 加载结果里那句话**总是要显示**：它可能是"已取完全部 N 条"，也可能是
        // "加载中断（已取回 0 条）：网络不可达"。只在 has_more 时才显示会把整轮失败的原因吞掉。
        string datasetMessage = GetString(query, "message");
        if (datasetMessage.Length > 0)
            parts.Add(datasetMessage);

        // 任务一：界面上必须说清展示的是"历史总下载量"（与 HF 网页的 30 天口径不同）
        parts.Add(Loc.T("settings.model.downloadsNote",
            "下载量口径：历史总下载量（与 Hugging Face 网页上的「30 天下载量」不是同一个数）"));
        if (_hubFallbackCount > 0)
            parts.Add(Loc.T("settings.model.downloadsFallback",
                "其中 {0} 条缺少历史总量字段，已回退为「最近 30 天」并逐条标注", _hubFallbackCount));

        if (query.TryGetProperty("recommend_basis", out JsonElement basis)
            && basis.ValueKind == JsonValueKind.Object)
        {
            string paramsText = GetString(basis, "params");
            if (paramsText.Length > 0)
                parts.Add(Loc.T("settings.model.recommendBasis",
                    "本机推荐：{0} 级量化（内存 {1} GB / {2} 核）。",
                    paramsText, GetNumberText(basis, "ram_gb"), GetNumberText(basis, "cpu_cores")));
        }
        if (_hubOffline)
            parts.Insert(0, Loc.T("settings.model.offlineMode",
                "📴 离线模式：远端不可达，当前展示的是本地缓存与已下载模型。"));

        HubStatusText.Text = string.Join("　", parts);
    }

    // -----------------------------------------------------------------
    // 分面
    // -----------------------------------------------------------------

    /// <summary>
    /// 按后端给的分面计数重建筛选控件。**只在选项签名变化时重建**：
    /// 每次查询都重建会让用户刚勾上的复选框被换掉（丢焦点、看起来像没勾上）。
    /// </summary>
    private void RebuildFacets(JsonElement query)
    {
        if (!query.TryGetProperty("options", out JsonElement options)
            || options.ValueKind != JsonValueKind.Object)
            return;
        string signature = options.GetRawText();
        if (signature == _hubFacetSignature)
            return;
        _hubFacetSignature = signature;

        _hubSuppressFilterEvents = true;
        try
        {
            RebuildParamFacet(options);
            RebuildQuantFacet(options);
            RebuildUpdatedFacet(options);
        }
        finally
        {
            _hubSuppressFilterEvents = false;
        }
        UpdateFilterSummary();
    }

    private static int OptionCount(JsonElement options, string group, string key)
    {
        if (!options.TryGetProperty(group, out JsonElement node) || node.ValueKind != JsonValueKind.Object)
            return 0;
        return node.TryGetProperty(key, out JsonElement value) && value.ValueKind == JsonValueKind.Number
            ? value.GetInt32() : 0;
    }

    private CheckBox MakeFacetBox(string label, int count, string tag, bool isChecked)
    {
        var box = new CheckBox
        {
            Content = count > 0 ? $"{label} ({count})" : label,
            Tag = tag,
            IsChecked = isChecked,
            FontSize = 11,
            Margin = new Thickness(0, 2, 12, 2),
            IsEnabled = count > 0 || isChecked,
        };
        box.IsCheckedChanged += async (_, _) =>
        {
            if (_hubSuppressFilterEvents)
                return;
            SyncFacetState();
            UpdateFilterSummary();
            await RefreshQueryAsync(resetPage: true, rebuildFacets: false);
        };
        return box;
    }

    /// <summary>把三个分面面板里勾上的复选框同步进筛选集合（勾选与取消共用一条路径）。</summary>
    private void SyncFacetState()
    {
        _hubParamSelected.Clear();
        _hubQuantSelected.Clear();
        _hubUpdatedSelected.Clear();
        foreach (Control child in HubParamFacet.Children)
            if (child is CheckBox { IsChecked: true } box && box.Tag is string key)
                _hubParamSelected.Add(key);
        foreach (Control child in HubQuantFacet.Children)
            if (child is CheckBox { IsChecked: true } box && box.Tag is string code)
                _hubQuantSelected.Add(code);
        foreach (Control child in HubUpdatedFacet.Children)
            if (child is CheckBox { IsChecked: true } box && box.Tag is string key)
                _hubUpdatedSelected.Add(key);
    }

    private static TextBlock FacetLabel(string text) => new()
    {
        Text = text,
        FontSize = 11,
        Margin = new Thickness(0, 2, 8, 2),
        VerticalAlignment = VerticalAlignment.Center,
        Opacity = 0.75,
    };

    private void RebuildParamFacet(JsonElement options)
    {
        HubParamFacet.Children.Clear();
        HubParamFacet.Children.Add(FacetLabel(Loc.T("settings.model.facetParams", "参数量")));
        foreach (string key in new[] { "p_le1", "p_1_3", "p_3_8", "p_8_20", "p_20_40", "p_gt40", "unknown" })
        {
            string label = key switch
            {
                "p_le1" => Loc.T("settings.model.paramLe1", "≤1B"),
                "p_1_3" => Loc.T("settings.model.param1to3", "1–3B"),
                "p_3_8" => Loc.T("settings.model.param3to8", "3–8B"),
                "p_8_20" => Loc.T("settings.model.param8to20", "8–20B"),
                "p_20_40" => Loc.T("settings.model.param20to40", "20–40B"),
                "p_gt40" => Loc.T("settings.model.paramGt40", ">40B"),
                _ => Loc.T("settings.model.unknown", "未知"),
            };
            HubParamFacet.Children.Add(
                MakeFacetBox(label, OptionCount(options, "params", key), key, _hubParamSelected.Contains(key)));
        }
    }

    private void RebuildQuantFacet(JsonElement options)
    {
        HubQuantFacet.Children.Clear();
        HubQuantFacet.Children.Add(FacetLabel(Loc.T("settings.model.facetQuant", "量化格式")));
        // 选项**从数据里来**，不写死候选清单：写死的话，数据里出现没列到的量化
        // （IQ2_XS / Q3_K_S / Q6_K …）就永远筛不出来。同一维度内多选是"或"。
        // 按计数降序、最多 14 个，免得面板变成一堵墙。
        var codes = new List<(string Code, int Count)>();
        if (options.TryGetProperty("quant", out JsonElement node) && node.ValueKind == JsonValueKind.Object)
        {
            foreach (JsonProperty entry in node.EnumerateObject())
            {
                string code = entry.Name;
                int count = entry.Value.ValueKind == JsonValueKind.Number ? entry.Value.GetInt32() : 0;
                if (code.Equals("UNKNOWN", StringComparison.OrdinalIgnoreCase))
                {
                    if (count > 0 || _hubQuantSelected.Contains("UNKNOWN"))
                        HubQuantFacet.Children.Add(MakeFacetBox(
                            Loc.T("settings.model.quantUnknown", "未标注量化"), count, "UNKNOWN",
                            _hubQuantSelected.Contains("UNKNOWN")));
                    continue;
                }
                if (count > 0 || _hubQuantSelected.Contains(code))
                    codes.Add((code, count));
            }
        }
        codes.Sort((a, b) => b.Count.CompareTo(a.Count));
        foreach ((string code, int count) in codes.Take(14))
            HubQuantFacet.Children.Add(MakeFacetBox(code, count, code, _hubQuantSelected.Contains(code)));
    }

    private void RebuildUpdatedFacet(JsonElement options)
    {
        HubUpdatedFacet.Children.Clear();
        HubUpdatedFacet.Children.Add(FacetLabel(Loc.T("settings.model.facetUpdated", "更新时间")));
        foreach ((string key, string label) in new[]
                 {
                     ("u_7", Loc.T("settings.model.updated7", "近 7 天")),
                     ("u_30", Loc.T("settings.model.updated30", "近 30 天")),
                     ("u_90", Loc.T("settings.model.updated90", "近 90 天")),
                     ("u_365", Loc.T("settings.model.updated365", "近一年")),
                     ("u_old", Loc.T("settings.model.updatedOld", "一年以上")),
                     ("unknown", Loc.T("settings.model.unknown", "未知")),
                 })
        {
            HubUpdatedFacet.Children.Add(
                MakeFacetBox(label, OptionCount(options, "updated", key), key, _hubUpdatedSelected.Contains(key)));
        }
    }

    private void UpdateFilterSummary()
    {
        int active = _hubParamSelected.Count + _hubQuantSelected.Count + _hubUpdatedSelected.Count
                     + (_hubInstalled != "any" ? 1 : 0) + (_hubRecommendedOnly ? 1 : 0)
                     + ((HubFilterBox.Text?.Trim().Length ?? 0) > 0 ? 1 : 0);
        HubFilterSummary.Text = active == 0
            ? Loc.T("settings.model.filterNone", "（未启用筛选）")
            : Loc.T("settings.model.filterActive", "（已启用 {0} 项）", active);
    }

    // -----------------------------------------------------------------
    // 文案
    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        // 区块标题（WPF 的 settings.model.sectionHub）：15 号加粗强调色，和常规页的区块标题同一口径。
        HubSectionTitle.Text = Loc.T("settings.model.sectionHub", "🧠 模型选择栏（Hugging Face GGUF 模型）");
        HubSourceLabel.Text = Loc.T("settings.model.sourceLabel", "数据源");
        HubTokenLabel.Text = "Token";
        HubSaveTokenButton.Content = Loc.T("settings.model.actionSaveToken", "保存 Token");
        HubOpenDirButton.Content = Loc.T("settings.model.actionModelsDir", "模型目录");
        HubSearchButton.Content = Loc.T("settings.model.actionSearch", "🔍 搜索");
        HubRefreshButton.Content = Loc.T("settings.model.actionRefresh", "🔄 刷新");
        HubMoreButton.Content = Loc.T("settings.model.actionMore", "继续取回");
        HubStopButton.Content = Loc.T("settings.model.actionStop", "⏹ 停止");
        HubSearchBox.PlaceholderText = Loc.T("settings.model.placeholder",
            "搜索模型（留空浏览热门 GGUF）");
        HubFilterTitle.Text = Loc.T("settings.model.filterTitle", "🎛️ 本地筛选与排序");
        HubLocalFilterLabel.Text = Loc.T("settings.model.localFilterLabel", "本地筛选");
        HubInstalledLabel.Text = Loc.T("settings.model.facetInstalled", "下载状态");
        HubRecommendedCheck.Content = Loc.T("settings.model.onlyRecommended", "只看推荐");
        HubFilterResetButton.Content = Loc.T("settings.model.actionResetFilter", "重置筛选");
        HubFilterBox.PlaceholderText = Loc.T("settings.model.filterPlaceholder", "在已加载的结果里筛选");
        HubEmptyText.Text = Loc.T("settings.model.hubIdle",
            "输入关键词搜索，或直接点「搜索」浏览热门 GGUF 模型");
        HubRemoteRetryButton.Content = Loc.T("settings.model.researchRemote", "重新搜索远端");
        HubFirstPageButton.Content = Loc.T("settings.model.pageFirst", "首页");
        HubPrevPageButton.Content = Loc.T("settings.model.pagePrev", "上一页");
        HubNextPageButton.Content = Loc.T("settings.model.pageNext", "下一页");
        HubLastPageButton.Content = Loc.T("settings.model.pageLast", "末页");
        HubGoPageButton.Content = Loc.T("settings.model.pageGo", "跳转");
        HubJumpLabel.Text = Loc.T("settings.model.pageJumpLabel", "跳至");
        HubPageSizeLabel.Text = Loc.T("settings.model.pageSizeLabel", "每页");
        HubPinnedDetailButton.Content = Loc.T("settings.model.actionDetail", "详情");
        HubDetailCloseButton.Content = Loc.T("settings.button.close", "关闭");
        HubCancelButton.Content = Loc.T("settings.model.actionStop", "⏹ 停止");
        // 市场这一块此前挂着"未迁移"的说明；本轮补全后那段说明只剩"后端不可用"一种用途，
        // 而它属于 ModelPage 的外壳文本，不在这里设置。
        // 筛选摘要由 UpdateFilterSummary() 按当前勾选状态渲染，这里不赋固定文案
        UpdateFilterSummary();

        // 排序 / 下载状态 / 每页条数：下拉项文本是委托，重建后按值选回
        foreach ((ComboBox combo, string keep) in new[]
                 {
                     (HubSortCombo, CurrentSort()),
                     (HubInstalledCombo, _hubInstalled),
                     (HubSourceCombo, CurrentSource()),
                 })
            RelocalizeCombo(combo, keep);
        RelocalizeCombo(HubPageSizeCombo, _hubPageSize.ToString(CultureInfo.InvariantCulture));

        // 结果行、分面、钉住行的文本都是运行时拼的：语言变了要重排（**不重新问后端**，
        // 与 WPF 的 RelocalizeHubMarket 同一做法）
        _hubFacetSignature = "";
        if (_hubLastQuery is JsonElement query)
            RenderQuery(query, rebuildFacets: true);
        else
        {
            UpdateFilterSummary();
            RenderPinned();
        }
    }

    /// <summary>重建下拉项并按键值选回原选项（ItemsSource 重置会丢选中项）。</summary>
    private static void RelocalizeCombo(ComboBox combo, string keep)
    {
        if (combo.ItemsSource is not System.Collections.IEnumerable source)
            return;
        var items = source.Cast<object>().ToList();
        int index = 0;
        for (int i = 0; i < items.Count; i++)
        {
            string text = items[i] switch
            {
                Option option => option.Value,
                string raw => raw,
                _ => "",
            };
            if (text == keep) { index = i; break; }
        }
        combo.ItemsSource = null;
        combo.ItemsSource = items;
        if (items.Count > 0)
            combo.SelectedIndex = index;
    }

    // -----------------------------------------------------------------
    // 小工具
    // -----------------------------------------------------------------
    private static Brush? TryBrush(string key)
    {
        if (Application.Current is null)
            return null;
        if (Application.Current.TryFindResource(key, out object? value) && value is Brush brush)
            return brush;
        return null;
    }

    private static string GetString(JsonElement item, string name) =>
        item.ValueKind == JsonValueKind.Object
        && item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? ""
            : "";

    private static bool GetBool(JsonElement item, string name) =>
        item.ValueKind == JsonValueKind.Object
        && item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.True;

    /// <summary>
    /// 读一个整数。
    ///
    /// **必须容忍小数**：`JsonElement.GetInt32()` 对 `1.5` 这种浮点值会抛
    /// `FormatException: One of the identified items was in an invalid format`，
    /// 而 JSON.NET（WPF 侧用的）会自动截断 —— 同一个响应换到 System.Text.Json
    /// 上就直接炸。后端的 `recommend_basis.ram_gb` / `returned` 之类字段是浮点，
    /// 实测踩到：**整条状态行渲染失败**（错误显示成"本地筛选失败"），
    /// 而且异常发生在 UI 线程的语言切换回调里，会连带把"切语言"整条路径打断。
    /// 这里统一按 double 读再取整，**假值（非数字）走 fallback**。
    /// </summary>
    private static int GetInt(JsonElement item, string name, int fallback)
    {
        if (item.ValueKind != JsonValueKind.Object || !item.TryGetProperty(name, out JsonElement el))
            return fallback;
        return el.ValueKind switch
        {
            JsonValueKind.Number when el.TryGetDouble(out double value)
                && !double.IsNaN(value) && !double.IsInfinity(value) => (int)Math.Round(value),
            JsonValueKind.String when int.TryParse(el.GetString(), out int parsed) => parsed,
            _ => fallback,
        };
    }

    // -----------------------------------------------------------------
    // 探针接口（`EchoDesk --probe market` 用）
    // -----------------------------------------------------------------

    /// <summary>当前市场的可观测状态（无人值守探针据此断言，不靠"看界面"）。</summary>
    public sealed record MarketState(
        int ItemCount, int FacetOptions, string PageInfo, string Total, bool EmptyVisible,
        bool PinnedVisible, string PagerHint, bool NextEnabled, bool FirstEnabled,
        bool SearchEnabled, bool MoreVisible, string Status, int FileRows);

    /// <summary>只读快照：把"界面看起来是什么样"变成一组可断言的值。</summary>
    public MarketState Snapshot() => new(
        _hubPage.Count,
        CountFacetBoxes(),
        HubPageInfoText.Text ?? "",
        HubTotalText.Text ?? "",
        HubEmptyPanel.IsVisible,
        HubPinnedRow.IsVisible,
        HubPagerHint.Text ?? "",
        HubNextPageButton.IsEnabled,
        HubFirstPageButton.IsEnabled,
        HubSearchButton.IsEnabled,
        HubMoreButton.IsVisible,
        HubStatusText.Text ?? "",
        HubFileList.ItemCount);

    private int CountFacetBoxes()
    {
        int count = 0;
        foreach (WrapPanel panel in new[] { HubParamFacet, HubQuantFacet, HubUpdatedFacet })
            foreach (Control child in panel.Children)
                if (child is CheckBox)
                    count++;
        return count;
    }

    /// <summary>触发一次"搜索"（等价于点 HubSearchButton）。</summary>
    public Task<object?> ProbeSearchAsync() => RunProbeAsync(() => LoadMarketAsync(refresh: false, resume: false));

    /// <summary>输入本地筛选关键词（走与真实输入同一条防抖路径，不等防抖）。</summary>
    public Task<object?> ProbeFilterAsync(string keyword)
        => RunProbeAsync(async () =>
        {
            HubFilterBox.Text = keyword;
            SyncFacetState();
            UpdateFilterSummary();
            await QueryWhenIdleAsync(resetPage: true, rebuildFacets: false);
        });

    /// <summary>勾选分面里的第 <paramref name="index"/> 个复选框（0 基，按参数量→量化→更新时间的顺序）。</summary>
    public Task<object?> ProbeToggleFacetAsync(int index)
        => RunProbeAsync(async () =>
        {
            int seen = 0;
            foreach (WrapPanel panel in new[] { HubParamFacet, HubQuantFacet, HubUpdatedFacet })
            {
                foreach (Control child in panel.Children)
                {
                    if (child is not CheckBox box)
                        continue;
                    if (seen++ != index)
                        continue;
                    if (!box.IsEnabled)
                        return;   // 计数为 0 的选项本来就不可勾（与 WPF 一致）
                    box.IsChecked = true;
                    SyncFacetState();
                    UpdateFilterSummary();
                    await QueryWhenIdleAsync(resetPage: true, rebuildFacets: false);
                    return;
                }
            }
        });

    /// <summary>翻页（与点分页按钮同一条路径）。</summary>
    public Task<object?> ProbeGoToPageAsync(int page)
        => RunProbeAsync(async () =>
        {
            _hubPageNo = Math.Clamp(page, 1, Math.Max(1, _hubPages));
            await QueryWhenIdleAsync(resetPage: false, rebuildFacets: false);
        });

    /// <summary>切换每页条数（20 / 50 / 100）。</summary>
    public Task<object?> ProbeSetPageSizeAsync(int size)
        => RunProbeAsync(async () =>
        {
            int index = Array.IndexOf(HubPageSizes, size);
            if (index < 0)
                return;
            // 直接设选中项会触发 SelectionChanged 处理器（与用户选择同一条路径），
            // 但探针不等"事件里那次查询"，而是自己再查一次，保证拿到的是最终状态。
            HubPageSizeCombo.SelectedIndex = index;
            _hubPageSize = size;
            await QueryWhenIdleAsync(resetPage: true, rebuildFacets: false);
        });

    /// <summary>重置筛选（与点「重置筛选」同一条路径）。</summary>
    public Task<object?> ProbeResetFiltersAsync()
        => RunProbeAsync(async () =>
        {
            _hubSuppressFilterEvents = true;
            try
            {
                _hubParamSelected.Clear();
                _hubQuantSelected.Clear();
                _hubUpdatedSelected.Clear();
                _hubInstalled = "any";
                _hubRecommendedOnly = false;
                HubFilterBox.Text = "";
                HubInstalledCombo.SelectedIndex = 0;
                HubRecommendedCheck.IsChecked = false;
                foreach (WrapPanel panel in new[] { HubParamFacet, HubQuantFacet, HubUpdatedFacet })
                    foreach (Control child in panel.Children)
                        if (child is CheckBox box)
                            box.IsChecked = false;
            }
            finally
            {
                _hubSuppressFilterEvents = false;
            }
            UpdateFilterSummary();
            await QueryWhenIdleAsync(resetPage: true, rebuildFacets: true);
        });

    /// <summary>打开钉住行（默认模型）的详情；没有钉住行时什么都不做。</summary>
    public Task<object?> ProbeOpenPinnedDetailAsync()
        => RunProbeAsync(async () =>
        {
            if (_hubPinned is not null)
                await ShowDetailAsync(_hubPinned);
        });

    /// <summary>
    /// 把动作跑在 UI 线程上，并等它真的做完（探针里不能"发了就算"）。
    /// 用 InvokeAsync 的异步重载：动作内部有 await（等后端响应），
    /// 同步重载会把 UI 线程一起挡住、变成死锁。
    /// </summary>
    private static async Task<object?> RunProbeAsync(Func<Task> action)
    {
        await Dispatcher.UIThread.InvokeAsync(async () => { await action(); });
        return null;
    }

    /// <summary>用系统默认文件管理器打开目录。返回是否成功（失败只提示，不抛）。</summary>
    private static bool TryOpenDirectory(string dir)
    {
        try
        {
            if (!System.IO.Directory.Exists(dir))
                System.IO.Directory.CreateDirectory(dir);
            var info = new System.Diagnostics.ProcessStartInfo
            {
                FileName = dir,
                UseShellExecute = true,   // 交给 shell：Windows 资源管理器 / macOS Finder / Linux xdg-open
            };
            System.Diagnostics.Process.Start(info);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>把数字（可能是浮点）显示成不带多余小数的文本；非数字返回空串。</summary>
    private static string GetNumberText(JsonElement item, string name)
    {
        if (item.ValueKind != JsonValueKind.Object || !item.TryGetProperty(name, out JsonElement el))
            return "";
        if (el.ValueKind == JsonValueKind.Number && el.TryGetDouble(out double value))
        {
            double rounded = Math.Round(value);
            return Math.Abs(value - rounded) < 0.05
                ? rounded.ToString(CultureInfo.InvariantCulture)
                : value.ToString("0.#", CultureInfo.InvariantCulture);
        }
        return el.ValueKind == JsonValueKind.String ? el.GetString() ?? "" : "";
    }

    private static string HumanSize(long bytes)    {
        if (bytes <= 0)
            return "";
        string[] units = { "B", "KB", "MB", "GB", "TB" };
        double value = bytes;
        int unit = 0;
        while (value >= 1024 && unit < units.Length - 1)
        {
            value /= 1024;
            unit++;
        }
        return $"{value:0.#} {units[unit]}";
    }
}
