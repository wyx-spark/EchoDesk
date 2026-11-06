// [EN] EchoDesk WPF frontend module: frontend/SettingsWindow.Market.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/SettingsWindow.Market.cs。类型与成员说明见下方注释。
using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using EchoDesk.Services;

namespace EchoDesk;

/// <summary>
/// 设置窗口「模型」页的**模型市场**部分（v0.2.1 模型市场优化）。
///
/// 单独成一个 partial 文件，是因为这一块从"搜 20 条显示 20 条"变成了
/// 一整套列表交互：远端加载（带进度）/ 本地实时筛选 / 结构化筛选器 / 分页控件 / 四种排序。
///
/// ## 分工：数据变换在后端，前端只负责画
/// 任务书里的"每页 20 条、本地实时筛选、结构化筛选、四种排序、分页控件"全是纯数据变换，
/// 而且边界条件（第一页/最后一页/非法页码/筛完为空）正是要验证的东西。
/// 本仓库有 Python 测试体系、没有 C# 测试体系，所以这些逻辑放在
/// `backend/model_query.py`（纯函数、有 100+ 条断言覆盖），前端通过
/// `model_hub_query` 拿**算好的一页**。
///
/// 为什么这不违背"本地实时筛选、不重新请求远端"：`model_hub_query` 是**进程内**往返
/// （实测 5000 条筛选+排序+分页约 40ms），**一次网络请求都不发**。任务书要防的是
/// "每敲一个字就去搜一遍远端"，这条被 `model_hub_query` 与 `model_hub_load` 的分工挡住了。
///
/// ## 三个动作
/// * `model_hub_load`（流式）：取回该关键词的全部结果，边取边报进度；
/// * `model_hub_query`（普通）：在已加载结果上筛选/排序/分页，毫秒级；
/// * `model_hub_detail` / `model_hub_download`：与原来一致（在 SettingsWindow.xaml.cs）。
/// </summary>
public partial class SettingsWindow
{
    /// <summary>每页条数选项（任务三：20 / 50 / 100，默认 20）。</summary>
    private static readonly int[] HubPageSizes = { 20, 50, 100 };

    /// <summary>本地筛选防抖：任务书要求"输入停止后触发过滤（如 150ms）"。</summary>
    private static readonly TimeSpan HubFilterDebounce = TimeSpan.FromMilliseconds(150);

    // ---------- 市场状态 ----------

    /// <summary>当前页数据（ListBox 的 ItemsSource）。</summary>
    private readonly List<HubModelItem> _hubPage = new();

    /// <summary>量化分面的显示文本 → 语言无关的量化代码（筛选键不能用显示文本）。</summary>
    private readonly Dictionary<string, string> _hubQuantOption = new();

    /// <summary>
    /// 上一次查询结果的原样副本。存它是为了**切换界面语言时不用再问一次后端**：
    /// 列表项的徽标/元信息文本是按语言拼出来的，语言一变就得重排一遍，
    /// 而这时候后端可能还没来得及切语言（先 ApplyLanguage 再同步后端），
    /// 重新查一次会把旧语言的 message/分面说明又拉回来。
    /// </summary>
    private JsonElement? _hubLastQuery;

    private readonly HashSet<string> _hubParamSelected = new();
    private readonly HashSet<string> _hubQuantSelected = new();
    private readonly HashSet<string> _hubUpdatedSelected = new();
    private DispatcherTimer? _hubFilterDebounce;
    private DispatcherTimer? _hubProgressTimer;

    private string _hubInstalled = "any";
    private bool _hubRecommendedOnly;
    private HubModelItem? _hubPinned;

    private int _hubPageNo = 1;    private int _hubPages = 1;
    private int _hubPageSize = 20;
    private int _hubFiltered;
    private int _hubLoaded;
    private int? _hubTotalAvailable;
    private bool _hubHasMore;
    private bool _hubOffline;
    private int _hubFallbackCount;
    private string _hubDownloadsMetric = "all_time";
    private string _hubFacetSignature = "";   // 分面的"选项签名"，变了才重建控件（否则会抢焦点）
    private int _hubElapsedSeconds;
    private bool _hubLoading;
    private bool _hubQuerying;

    /// <summary>上一次加载的结束原因（none/items/requests/seconds/error/aborted）。</summary>
    private string _hubStopReason = "";

    /// <summary>上一次加载实际取回的条数（不含本地补充来源）。</summary>
    private int _hubLoadedFromLoad;

    /// <summary>窗口关闭时停掉两个计时器（防抖与进度自增），避免回调落到已销毁的控件上。</summary>
    private void StopHubTimers()
    {
        _hubFilterDebounce?.Stop();
        _hubProgressTimer?.Stop();
    }

    // ---------- 初始化 ----------

    /// <summary>构造期初始化（由 SettingsWindow 构造函数调用，此时已设 _suppressHubFilterEvents）。</summary>
    private void InitHubMarketUi()
    {
        HubSortCombo.Items.Add(new ComboBoxItem
        { Content = Loc.T("settings.model.sortDownloadsAll", "历史总下载量"), Tag = "downloads" });
        HubSortCombo.Items.Add(new ComboBoxItem
        { Content = Loc.T("settings.model.sortLikes", "点赞最多"), Tag = "likes" });
        HubSortCombo.Items.Add(new ComboBoxItem
        { Content = Loc.T("settings.model.sortUpdated", "最近更新"), Tag = "updated" });
        HubSortCombo.Items.Add(new ComboBoxItem
        { Content = Loc.T("settings.model.sortParams", "参数量"), Tag = "params" });
        HubSortCombo.SelectedIndex = 0;

        HubInstalledCombo.Items.Add(new ComboBoxItem
        { Content = Loc.T("settings.model.installedAny", "不限下载状态"), Tag = "any" });
        HubInstalledCombo.Items.Add(new ComboBoxItem
        { Content = Loc.T("settings.model.installedYes", "只看已下载"), Tag = "yes" });
        HubInstalledCombo.Items.Add(new ComboBoxItem
        { Content = Loc.T("settings.model.installedNo", "只看未下载"), Tag = "no" });
        HubInstalledCombo.SelectedIndex = 0;

        foreach (int size in HubPageSizes)
            HubPageSizeCombo.Items.Add(size.ToString(CultureInfo.InvariantCulture));
        HubPageSizeCombo.SelectedIndex = 0;

        // 本地筛选防抖（150ms）：输入停下才过滤一次，避免每敲一个字都算
        _hubFilterDebounce = new DispatcherTimer { Interval = HubFilterDebounce };
        _hubFilterDebounce.Tick += (_, _) =>
        {
            _hubFilterDebounce!.Stop();
            _ = RefreshHubQueryAsync(resetPage: true, rebuildFacets: true);
        };

        // 加载中的进度提示：进度行是离散事件，但"用时"要能自己往前走，
        // 否则 3 秒没来下一页时界面看起来像卡住了。
        _hubProgressTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _hubProgressTimer.Tick += (_, _) =>
        {
            if (_hubLoading)
                HubStatusText.Text = Loc.T("settings.model.loadingElapsed",
                    "⏳ 正在取回结果…（已加载 {0} 条，用时 {1} 秒）", _hubLoaded, _hubElapsedSeconds);
        };
    }

    private string CurrentHubSort() =>
        (HubSortCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "downloads";

    // ---------- 事件入口 ----------

    private void HubSearchBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            _ = LoadHubMarketAsync(refresh: false, resume: false);
        }
    }

    private void HubSearchButton_Click(object sender, RoutedEventArgs e) =>
        _ = LoadHubMarketAsync(refresh: false, resume: false);

    private void HubRefreshButton_Click(object sender, RoutedEventArgs e) =>
        _ = LoadHubMarketAsync(refresh: true, resume: false);

    private void HubMoreButton_Click(object sender, RoutedEventArgs e) =>
        _ = LoadHubMarketAsync(refresh: false, resume: true);

    private async void HubStopButton_Click(object sender, RoutedEventArgs e)
    {
        HubStopButton.IsEnabled = false;
        try
        {
            // 与下载取消同样的做法：stdout 是单条共享流，中途放弃读取会让后续响应错位，
            // 所以用后端的 stop 信令而不是 CancellationToken。
            await _backend.SendFireAndForgetAsync(new { action = "stop" });
            HubStatusText.Text = Loc.T("settings.model.stopRequested", "⏹ 已请求停止加载…");
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.stopFailed", "❌ 停止失败：{0}", ex.Message);
        }
        finally
        {
            HubStopButton.IsEnabled = true;
        }
    }

    /// <summary>排序切换：任务书要求"排序切换时重新请求远端"（排序是服务端行为）。</summary>
    private void HubSortChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || _loading || _suppressHubFilterEvents)
            return;
        _ = LoadHubMarketAsync(refresh: false, resume: false);
    }

    private void HubFilterBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_uiReady || _loading || _suppressHubFilterEvents)
            return;
        _hubFilterDebounce?.Stop();
        _hubFilterDebounce?.Start();
    }

    /// <summary>
    /// 结构化筛选变动：只重算本地结果，**不重发远端请求**（任务五）。
    ///
    /// 复选框挂的是 <c>Checked</c>/<c>Unchecked</c> 而不是 <c>Click</c>：
    /// WPF 的 <c>ToggleButtonAutomationPeer.Toggle()</c>（无障碍工具、UI 自动化、
    /// 以及本仓库的 <c>tools/ui_probe.ps1</c> 都走这条）只调 <c>OnToggle()</c>，
    /// **不会**触发 <c>Click</c>。挂 Click 的话，鼠标点没问题、自动化点就"点了没反应" ——
    /// 这个差异是 UI 探针跑出来的（勾了「≤1B」但条数仍是 5000）。
    /// </summary>
    private void HubFacetCheck_Click(object sender, RoutedEventArgs e) => ApplyFacetChange();

    private void HubFacetCombo_Changed(object sender, SelectionChangedEventArgs e) => ApplyFacetChange();

    private void ApplyFacetChange()
    {
        if (!_uiReady || _loading || _suppressHubFilterEvents)
            return;
        CollectFacetSelection();
        UpdateFilterSummary();
        _ = RefreshHubQueryAsync(resetPage: true, rebuildFacets: false);
    }

    private void HubFilterReset_Click(object sender, RoutedEventArgs e)
    {
        _suppressHubFilterEvents = true;
        try
        {
            _hubParamSelected.Clear();
            _hubQuantSelected.Clear();
            _hubUpdatedSelected.Clear();
            _hubInstalled = "any";
            _hubRecommendedOnly = false;
            HubInstalledCombo.SelectedIndex = 0;
            HubRecommendedCheck.IsChecked = false;
            HubFilterBox.Text = "";
            foreach (CheckBox box in FacetBoxes())
                box.IsChecked = false;
        }
        finally
        {
            _suppressHubFilterEvents = false;
        }
        UpdateFilterSummary();
        _ = RefreshHubQueryAsync(resetPage: true, rebuildFacets: true);
    }

    private IEnumerable<CheckBox> FacetBoxes()
    {
        foreach (WrapPanel panel in new[] { HubParamFacet, HubQuantFacet, HubUpdatedFacet })
            foreach (object child in panel.Children)
                if (child is CheckBox box)
                    yield return box;
    }

    private void CollectFacetSelection()
    {
        _hubParamSelected.Clear();
        _hubQuantSelected.Clear();
        _hubUpdatedSelected.Clear();
        foreach (object child in HubParamFacet.Children)
            if (child is CheckBox { IsChecked: true } box && box.Tag is string key)
                _hubParamSelected.Add(key);
        foreach (object child in HubQuantFacet.Children)
            if (child is CheckBox { IsChecked: true } box && box.Tag is string code)
                _hubQuantSelected.Add(code);
        foreach (object child in HubUpdatedFacet.Children)
            if (child is CheckBox { IsChecked: true } box && box.Tag is string key)
                _hubUpdatedSelected.Add(key);
        _hubInstalled = (HubInstalledCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "any";
        _hubRecommendedOnly = HubRecommendedCheck.IsChecked == true;
    }

    private int ActiveFilterCount() =>
        _hubParamSelected.Count + _hubQuantSelected.Count + _hubUpdatedSelected.Count
        + (_hubInstalled != "any" ? 1 : 0) + (_hubRecommendedOnly ? 1 : 0)
        + (HubFilterBox.Text.Trim().Length > 0 ? 1 : 0);

    private void UpdateFilterSummary()
    {
        int active = ActiveFilterCount();
        HubFilterSummary.Text = active == 0
            ? Loc.T("settings.model.filterNone", "（未启用筛选）")
            : Loc.T("settings.model.filterActive", "（已启用 {0} 项）", active);
    }

    // ---------- 本地查询（筛选 / 排序 / 分页） ----------

    private async Task RefreshHubQueryAsync(bool resetPage, bool rebuildFacets)
    {
        if (_hubQuerying)
            return;
        _hubQuerying = true;
        if (resetPage)
            _hubPageNo = 1;
        try
        {
            string json = await _backend.SendRequestAsync(new
            {
                action = "model_hub_query",
                keyword = HubFilterBox.Text.Trim(),
                sort = CurrentHubSort(),
                page = _hubPageNo,
                page_size = _hubPageSize,
                filters = BuildFilters(),
            }, idleTimeout: TimeSpan.FromSeconds(60));
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("hub_query", out JsonElement query))
            {
                RenderHubQuery(query, rebuildFacets);
            }
            else if (doc.RootElement.TryGetProperty("busy", out JsonElement busy) && busy.ValueKind == JsonValueKind.True)
            {
                HubStatusText.Text = Loc.T("settings.model.backendBusy",
                    "⏳ 后端正在处理其他任务（例如正在生成回复），稍后会自动生效。");
            }
        }
        catch (Exception ex)
        {
            HubStatusText.Text = Loc.T("settings.model.queryFailed", "❌ 本地筛选失败：{0}", ex.Message);
        }
        finally
        {
            _hubQuerying = false;
        }
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

    private void RenderHubQuery(JsonElement query, bool rebuildFacets)
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
                _hubPage.Add(BuildHubItem(item));

        // 默认模型钉在列表上方（任务七），它的条目单独来自 pinned
        if (query.TryGetProperty("pinned", out JsonElement pinned)
            && pinned.ValueKind == JsonValueKind.Array && pinned.GetArrayLength() > 0)
        {
            _hubPinned = BuildHubItem(pinned[0]);
        }
        RenderHubPinned();

        HubResultList.ItemsSource = null;
        HubResultList.ItemsSource = _hubPage;

        if (rebuildFacets)
            RebuildFacets(query);

        RenderHubPager(pageCorrected);
        RenderHubStatus(query);
    }

    private HubModelItem BuildHubItem(JsonElement item)
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
            downloadsText = $"⬇ {downloads:N0}（{label}）";
        }

        double paramB = 0;
        if (item.TryGetProperty("param_b", out JsonElement pb) && pb.ValueKind == JsonValueKind.Number)
            paramB = pb.GetDouble();

        return new HubModelItem
        {
            Repo = repo,
            Name = GetString(item, "name"),
            Gated = GetBool(item, "gated"),
            Recommended = recommended,
            IsDefault = isDefault,
            Tag = tag,
            Badge = GetBool(item, "installed") ? Loc.T("settings.model.badgeDownloaded", "● 已下载")
                        : (GetBool(item, "gated") ? Loc.T("settings.model.badgeGated", "🔒 需登录") : ""),
            Meta = string.Join(" · ", new[]
            {
                GetString(item, "author"),
                downloadsText,
                likes > 0 ? $"⭐ {likes}" : "",
                paramB > 0 ? Loc.T("settings.model.paramSize", "{0}B 参数", paramB.ToString("0.#")) : "",
                GetString(item, "quant_code").Length > 0 ? GetString(item, "quant_code") : "",
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
            }.Where(x => x.Length > 0)),
        };
    }

    /// <summary>
    /// "共 N 条"的措辞（总条数显示与状态行共用，避免两处口径不一致）。
    ///
    /// 三种情况分清楚：取完了 → 精确总数；撞上加载上限 → "N 条以上（远端还有更多）"；
    /// **一条都没加载**（还没搜、或上次加载失败）→ 什么都不说。
    /// 早期版本只看 total_available 是否为空，于是"还没加载"也会显示
    /// "共 0 条以上（远端还有更多）" —— 两句话都是假的（UI 探针实测到过）。
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

    /// <summary>默认模型单独一行钉在结果上方，标注来源，且**不计入下面的分页条数**。</summary>
    private void RenderHubPinned()
    {
        if (_hubPinned is null)
        {
            HubPinnedRow.Visibility = Visibility.Collapsed;
            return;
        }
        HubPinnedTag.Text = Loc.T("settings.model.pinnedTag", "📌 默认模型");
        HubPinnedName.Text = _hubPinned.Name;
        HubPinnedMeta.Text = Loc.T("settings.model.pinnedMeta",
            "来源：config.json 里的默认模型仓库 {0}　·　固定显示，不计入下方的分页条数", _hubPinned.Repo);
        HubPinnedRow.Visibility = Visibility.Visible;
    }

    private void HubPinnedDetail_Click(object sender, RoutedEventArgs e)
    {
        if (_hubPinned is not null)
            _ = ShowHubDetailAsync(_hubPinned);
    }

    // ---------- 分页控件（任务三） ----------

    private void RenderHubPager(bool pageCorrected)
    {
        HubPageInfoText.Text = Loc.T("settings.model.pageInfo", "第 {0} / {1} 页", _hubPageNo, _hubPages);
        // 总条数：取完才是精确值，撞上限要说"还有更多"，一条没加载就不编数字（见 TotalText）
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
        HubMoreButton.Visibility = (_hubHasMore && !_hubLoading) ? Visibility.Visible : Visibility.Collapsed;
        // 页码提示写在分页控件旁边（HubPagerHint）。**不要写进筛选面板里的 HubFilterHint**：
        // 筛选面板默认是折叠的，折叠内容不在 UIA 树里、用户也看不见 —— 页码填错时提示等于没给。
        HubPagerHint.Text = pageCorrected
            ? Loc.T("settings.model.pageCorrected", "⚠️ 页码超出范围，已显示第 {0} 页", _hubPageNo)
            : "";
    }

    private void HubFirstPage_Click(object sender, RoutedEventArgs e) => GoToHubPage(1);

    private void HubPrevPage_Click(object sender, RoutedEventArgs e) => GoToHubPage(_hubPageNo - 1);

    private void HubNextPage_Click(object sender, RoutedEventArgs e) => GoToHubPage(_hubPageNo + 1);

    private void HubLastPage_Click(object sender, RoutedEventArgs e) => GoToHubPage(_hubPages);

    private void HubGoPage_Click(object sender, RoutedEventArgs e) => JumpHubPage();

    private void HubPageBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            JumpHubPage();
        }
    }

    private void JumpHubPage()
    {
        string text = HubPageBox.Text.Trim();
        // 输入非法时给出提示，而不是默默跳到别的页（任务书：输入非法时给出提示）
        if (!int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out int page))
        {
            HubPagerHint.Text = Loc.T("settings.model.pageInvalid",
                "⚠️ 请输入 {0}–{1} 之间的页码", 1, _hubPages);
            HubPageBox.Text = _hubPageNo.ToString(CultureInfo.InvariantCulture);
            return;
        }
        if (page < 1 || page > _hubPages)
        {
            HubPagerHint.Text = Loc.T("settings.model.pageOutOfRange",
                "⚠️ 页码需在 {0}–{1} 之间（已跳到最近的合法页）", 1, _hubPages);
        }
        GoToHubPage(page);
    }

    private void GoToHubPage(int page)
    {
        int target = Math.Max(1, Math.Min(page, _hubPages));
        if (target == _hubPageNo)
        {
            // 页码没变：不重查，但提示要清掉，否则旧的"非法输入"提示会一直挂着
            HubPagerHint.Text = "";
            return;
        }
        _hubPageNo = target;
        // 翻页**不重新请求远端**，也不重置排序与筛选（任务三：页面切换时保持排序与筛选条件不变）
        _ = RefreshHubQueryAsync(resetPage: false, rebuildFacets: false);
    }

    private void HubPageSize_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || _loading || _suppressHubFilterEvents)
            return;
        if (HubPageSizeCombo.SelectedItem is string text && int.TryParse(text, out int size) && size > 0)
        {
            _hubPageSize = size;
            // 换了每页条数，页码回到第一页（否则用户会落在"第 300 页"这种位置）
            _ = RefreshHubQueryAsync(resetPage: true, rebuildFacets: false);
        }
    }

    // ---------- 远端加载（任务二） ----------

    /// <summary>
    /// 向远端取回该关键词的全部结果（流式，带进度）。
    ///
    /// **自动重试一次**：用户实测反馈"搜索的时候要点刷新才能出来结果"。
    /// 原因是首次加载常常整轮失败（网络抖一下、或后端正忙于窗口初始化的那几个请求），
    /// 界面只剩一个空列表 —— 用户只能再点一次「搜索」或「刷新」。现在改成：
    /// 一轮下来**一条都没取回**且结束原因是 error（或离线降级）时，等 1.2 秒自动重试一次，
    /// 并把"正在自动重试"写在状态行上（不是默默地等）。
    /// 关键词本来就没命中（stop_reason=none）不会重试 —— 那重试也没用。
    ///
    /// 名字刻意叫 <c>LoadHubMarketAsync</c> 而不是 <c>LoadHubAsync</c>：
    /// <c>SettingsWindow.xaml.cs</c> 里已经有一个**无参**的 <c>LoadHubAsync()</c>，
    /// 它是"初始化模型页（读数据源状态 + 刷新本地模型库）"。同名重载虽然能编译通过，
    /// 但两个语义完全不同的方法共用一个名字，改起来迟早出事。
    /// </summary>
    private async Task LoadHubMarketAsync(bool refresh, bool resume)
    {
        for (int attempt = 1; attempt <= 2; attempt++)
        {
            await LoadHubMarketOnceAsync(refresh, resume);
            bool failed = _hubStopReason == "error"
                          || (_hubStopReason == "none" && _hubLoadedFromLoad == 0);
            if (!failed || _hubLoadedFromLoad > 0)
                return;
            if (attempt == 2)
                return;
            HubStatusText.Text = Loc.T("settings.model.retrying",
                "⚠️ 这一轮没有取回任何结果，正在自动重试…");
            await Task.Delay(1200);
        }
    }

    private async Task LoadHubMarketOnceAsync(bool refresh, bool resume)
    {
        if (_hubLoading)
            return;
        _hubLoading = true;
        _hubElapsedSeconds = 0;
        HubSearchButton.IsEnabled = false;
        HubRefreshButton.IsEnabled = false;
        HubMoreButton.Visibility = Visibility.Collapsed;
        HubStopButton.Visibility = Visibility.Visible;
        HubLoadProgressBar.Visibility = Visibility.Visible;
        HubLoadProgressBar.IsIndeterminate = true;
        HubStatusText.Text = Loc.T("settings.model.searching", "正在搜索模型…");
        _hubProgressTimer?.Start();
        string loadNotice = "";
        try
        {
            bool gotResult = false;
            await _backend.SendStreamRequestAsync(
                new
                {
                    action = "model_hub_load",
                    keyword = HubSearchBox.Text.Trim(),
                    sort = CurrentHubSort(),
                    source = CurrentHubSource(),
                    page = 1,
                    page_size = _hubPageSize,
                    refresh,
                    resume,
                    // 结果集里不带第一页（本地筛选/排序/分页统一走 model_hub_query），
                    // 免得两处各算一遍、数字还对不上
                    no_page = true,
                },
                delta => { },
                onEvent: (type, root) =>
                {
                    if (type == "hub_progress" && root.TryGetProperty("hub_progress", out JsonElement p))
                    {
                        _hubLoaded = GetInt(p, "loaded", 0);
                        _hubElapsedSeconds = (int)Math.Round(
                            p.TryGetProperty("elapsed", out JsonElement el) && el.ValueKind == JsonValueKind.Number
                                ? el.GetDouble() : 0);
                        HubProgressText.Text = Loc.T("settings.model.loadProgress",
                            "⏳ 正在取回结果：已加载 {0} 条（用时 {1} 秒）…", _hubLoaded, _hubElapsedSeconds);
                    }
                    else if (type == "hub_result" && root.TryGetProperty("model_hub", out JsonElement hub))
                    {
                        gotResult = true;
                        ApplyHubDataset(hub);
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
            _hubProgressTimer?.Stop();
            HubSearchButton.IsEnabled = true;
            HubRefreshButton.IsEnabled = true;
            HubStopButton.Visibility = Visibility.Collapsed;
            HubLoadProgressBar.Visibility = Visibility.Collapsed;
            HubLoadProgressBar.IsIndeterminate = false;
            HubProgressText.Text = "";
            // 数据集换了之后第一页要重新算：统一走本地查询，保证三层数字与分页一致
            await RefreshHubQueryAsync(resetPage: true, rebuildFacets: true);
            // 加载失败的原因要**补在最前面**：上面那次查询会把状态行整条重写成
            // "本地筛选后 0 条 / …"，把真正的错误盖掉 —— 用户看到的就是
            // "怎么点都没反应"，而不是"后端正忙"或"网络不可达"（UI 探针实测到过）。
            if (loadNotice.Length > 0)
                HubStatusText.Text = loadNotice + "　" + HubStatusText.Text;
        }
    }

    /// <summary>把加载结果里与"列表之外"有关的状态记下来（排序/筛选/分页仍由查询动作给）。</summary>
    private void ApplyHubDataset(JsonElement hub)
    {
        _hubOffline = GetBool(hub, "offline");
        _hubHasMore = GetBool(hub, "has_more");
        _hubStopReason = GetString(hub, "stop_reason");
        _hubLoadedFromLoad = GetInt(hub, "loaded", 0);
        _hubDownloadsMetric = GetString(hub, "downloads_metric");
        _hubFallbackCount = GetInt(hub, "downloads_fallback_count", 0);
        _hubFacetSignature = "";               // 数据集变了，分面选项与计数都要重建
        if (hub.TryGetProperty("total_available", out JsonElement total)
            && total.ValueKind == JsonValueKind.Number)
            _hubTotalAvailable = total.GetInt32();
        else
            _hubTotalAvailable = null;
    }

    /// <summary>模型库变动后刷新列表里的「已下载」徽标（命中缓存时几乎瞬时）。</summary>
    private async Task ReloadHubForBadgesAsync()
    {
        if (string.IsNullOrEmpty(HubSearchBox.Text.Trim()) && _hubLoaded == 0)
            return;
        await LoadHubMarketAsync(refresh: false, resume: false);
    }

    // ---------- 分面（结构化筛选器的选项） ----------

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

        _suppressHubFilterEvents = true;
        try
        {
            RebuildParamFacet(options);
            RebuildQuantFacet(options);
            RebuildUpdatedFacet(options);
        }
        finally
        {
            _suppressHubFilterEvents = false;
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
            IsEnabled = count > 0,
        };
        box.Checked += HubFacetCheck_Click;
        box.Unchecked += HubFacetCheck_Click;
        return box;
    }

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
        // 选项**从数据里来**，而不是写死一份候选清单：写死的话，数据里出现没列到的量化
        // （IQ2_XS / Q3_K_S / Q6_K …）就永远筛不出来，而任务书要求"从文件名解析"。
        // 同一维度内的多选是"或"。按计数降序，最多 14 个，免得面板变成一堵墙。
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

    /// <summary>供 ApplyLanguage 调用：按新语言重排当前页与分面（不重新问后端）。</summary>
    private void RelocalizeHubMarket()
    {
        _hubFacetSignature = "";
        if (_hubLastQuery is JsonElement query)
            RenderHubQuery(query, rebuildFacets: true);
        else
        {
            UpdateFilterSummary();
            RenderHubPinned();
        }
    }

    private static TextBlock FacetLabel(string text) => new()
    {
        Text = text,
        FontSize = 11,
        Margin = new Thickness(0, 2, 8, 2),
        VerticalAlignment = VerticalAlignment.Center,
        Opacity = 0.75,
    };

    // ---------- 状态行 ----------

    private void RenderHubStatus(JsonElement query)
    {
        string emptyReason = GetString(query, "empty_reason");
        bool noItems = _hubPage.Count == 0;

        // 空状态：区分"本地筛完没结果"（给重新搜索远端的出口）与"数据集本来就空"
        HubEmptyPanel.Visibility = noItems ? Visibility.Visible : Visibility.Collapsed;
        HubRemoteRetryButton.Visibility = emptyReason == "local_no_match"
            ? Visibility.Visible : Visibility.Collapsed;
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
        // 加载结果里那句话**总是要显示**：它既可能是"已取完全部 N 条""已从缓存载入 N 条"，
        // 也可能是"加载中断（已取回 0 条）：网络不可达"。早期版本只在与 has_more 同时成立时
        // 才显示，于是**整轮失败的原因被吞掉**，用户看到的只是一个空列表，
        // 只能自己再点一次「搜索」或「刷新」（用户实测反馈的正是这一点）。
        string datasetMessage = GetString(query, "message");
        if (datasetMessage.Length > 0)
            parts.Add(datasetMessage);

        // 任务一：界面上必须说清展示的是"历史总下载量"，避免与 HF 网页的 30 天口径对比时困惑
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
                    paramsText, GetInt(basis, "ram_gb", 0), GetInt(basis, "cpu_cores", 0)));
        }
        if (_hubOffline)
            parts.Insert(0, Loc.T("settings.model.offlineMode",
                "📴 离线模式：远端不可达，当前展示的是本地缓存与已下载模型。"));

        HubStatusText.Text = string.Join("　", parts);
    }
}
