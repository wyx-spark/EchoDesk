// 技能市场页的交互逻辑（v0.2.1 跨平台-2 任务一，第 4 个迁移模块）。
//
// 后端动作（与 WPF 版一致）：
//   market_search   { keyword, sort, page } → { market: { items: [ { full_name, name,
//                     description, verified, installed, stars, pushed_at, license,
//                     version, html_url } ] } }
//   market_detail   { repo }                → 技能详情（skill.json 的内容）
//   market_install  { repo }                → { content }（后端给的说明文本）
//   market_uninstall{ repo }                → { content }
//
// 与 WPF 版的差异只有"跟着平台走"的部分：
//   * **安装/卸载的确认**改成就地二次确认（按钮变「确认安装？/确认卸载？」）——
//     Avalonia 没有内置 MessageBox。**但安全提示文本照旧显示**（在状态行），
//     因为"第三方技能以你的本地账户权限运行"这句是产品要求必须让用户看到的，
//     不能因为换前端就省掉。
//   * **技能详情**用骨架右侧的字段帮助面板显示，而不是弹对话框 ——
//     既不用再造窗口，又能让详情与列表同屏对照。
using System;
using System.Collections.Generic;
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

public partial class MarketPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;

    /// <summary>排序选项：Tag 是发给后端的 sort 值，文案在 ApplyTexts 里按语言取。</summary>
    private sealed record SortOption(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    private readonly List<JsonElement> _items = new();

    /// <summary>等待二次确认的仓库全名（安装与卸载共用一套就地确认）。</summary>
    private string _pendingRepo = "";
    private string _pendingAction = "";
    private DispatcherTimer? _pendingTimer;

    public MarketPage()
    {
        InitializeComponent();
        BuildOptions();

        MarketSearchBox.KeyDown += async (_, e) =>
        {
            if (e.Key == Key.Enter)
            {
                e.Handled = true;
                await SearchAsync();
            }
        };
        // 输入即搜（300ms 防抖）：与 WPF 侧的 MarketSearchBox_TextChanged 同一行为。
        // 为什么要有：用户的心智是"边打边看结果"，按一次回车才搜会让人以为搜索框坏了；
        // 防抖是为了不每敲一个字就打一次远端。
        MarketSearchBox.TextChanged += (_, _) =>
        {
            if (_suppressSearchEvents)
                return;
            _searchDebounce ??= new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(300) };
            _searchDebounce.Stop();
            _searchDebounce.Tick += async (_, _) =>
            {
                _searchDebounce?.Stop();
                await SearchAsync();
            };
            _searchDebounce.Start();
        };
        MarketSearchButton.Click += async (_, _) => await SearchAsync();
        MarketRefreshButton.Click += async (_, _) =>
        {
            // 刷新 = 清空关键词再搜（与 WPF 一致）；清空会让 TextChanged 也触发一次，
            // 用重入闸挡住重复请求。
            _suppressSearchEvents = true;
            MarketSearchBox.Text = "";
            _suppressSearchEvents = false;
            await SearchAsync();
        };
        MarketSortCombo.SelectionChanged += async (_, _) =>
        {
            if (!_loaded)
                await SearchAsync();
        };
    }

    private bool _loaded;
    private DispatcherTimer? _searchDebounce;
    private bool _suppressSearchEvents;

    public string TabKey => "settings.tab.market";
    public string TabFallback => "技能市场";
    public string AutomationId => "SettingsTabMarket";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }
    public Task ApplyToBackendAsync() => Task.CompletedTask;

    private void BuildOptions()
    {
        MarketSortCombo.ItemsSource = new List<SortOption>
        {
            new("best", () => Loc.T("settings.market.sortBest", "最佳匹配")),
            new("stars", () => Loc.T("settings.market.sortStars", "星标最多")),
            new("updated", () => Loc.T("settings.market.sortUpdated", "最近更新")),
        };
        MarketSortCombo.SelectedIndex = 0;
    }

    // -----------------------------------------------------------------
    // 数据
    // -----------------------------------------------------------------
    private async Task<JsonDocument?> SendAsync(object request)
    {
        if (_host is null)
            return null;
        return await _host.State.Backend.SendAsync(request, idleTimeout: TimeSpan.FromSeconds(90));
    }

    private string CurrentSort() =>
        (MarketSortCombo.SelectedItem as SortOption)?.Value ?? "best";

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        string keyword = MarketSearchBox.Text?.Trim() ?? "";
        MarketStatusText.Text = keyword.Length == 0
            ? Loc.T("settings.market.loadingList", "正在获取技能列表…")
            : Loc.T("settings.market.searching", "正在搜索「{0}」…", keyword);
        MarketSearchButton.IsEnabled = false;
        try
        {
            using JsonDocument? doc = await SendAsync(new
            {
                action = "market_search",
                keyword,
                sort = CurrentSort(),
                page = 1,
            });
            _loaded = true;
            if (doc is null || !doc.RootElement.TryGetProperty("market", out JsonElement market))
            {
                MarketStatusText.Text = Loc.T("settings.market.unavailable",
                    "⚠️ 技能市场不可用（后端未返回数据）");
                return;
            }
            RenderMarket(market);
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = Loc.T("settings.market.requestFailed",
                "❌ 技能市场请求失败：{0}", ex.Message);
        }
        finally
        {
            MarketSearchButton.IsEnabled = true;
        }
    }

    private Task SearchAsync() => RefreshAsync();

    private void RenderMarket(JsonElement market)
    {
        _items.Clear();
        var cards = new List<Control>();
        if (market.TryGetProperty("items", out JsonElement items) && items.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in items.EnumerateArray())
            {
                _items.Add(item.Clone());
                cards.Add(BuildCard(item));
            }
        }
        MarketList.ItemsSource = cards;
        bool empty = cards.Count == 0;
        MarketEmptyText.IsVisible = empty;
        MarketList.IsVisible = !empty;
        // 双击条目打开详情（与 WPF 侧的 MarketList_MouseDoubleClick 同一行为）。
        // 挂在列表上而不是逐项挂：卡片是运行时构建的，逐项挂会重复 N 次。
        MarketList.DoubleTapped -= OnListDoubleTapped;
        MarketList.DoubleTapped += OnListDoubleTapped;
        if (empty)
        {
            MarketEmptyText.Text = Loc.T("settings.market.emptyResult", "没有匹配的技能。") + "\n"
                + Loc.T("settings.market.emptyStatus",
                    "试试更短的关键词，或点「刷新」浏览全部技能。");
        }
        else
        {
            // 包值要两个参数：「共 {0} 个结果，本页 {1} 个」。总数取后端回传的 total_count
            //（与 WPF `SettingsWindow.xaml.cs:1469` 同一字段），取不到时退化为本页条数。
            int total = market.TryGetProperty("total_count", out JsonElement tc)
                        && tc.ValueKind == JsonValueKind.Number ? tc.GetInt32() : cards.Count;
            MarketStatusText.Text = Loc.T("settings.market.resultCount",
                "共 {0} 个结果，本页 {1} 个", total, cards.Count);
        }
    }

    // -----------------------------------------------------------------
    // 卡片
    // -----------------------------------------------------------------
    private Control BuildCard(JsonElement item)
    {
        string fullName = GetString(item, "full_name");
        string name = GetString(item, "name");
        string description = GetString(item, "description");
        bool verified = GetBool(item, "verified");
        bool installed = GetBool(item, "installed");
        int stars = item.TryGetProperty("stars", out JsonElement s) && s.ValueKind == JsonValueKind.Number
            ? s.GetInt32() : 0;
        string pushed = GetString(item, "pushed_at");
        if (pushed.Length >= 10)
            pushed = pushed[..10];
        string license = GetString(item, "license");
        string version = GetString(item, "version");

        var panel = new StackPanel { Spacing = 4 };

        var titleRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        titleRow.Children.Add(new TextBlock
        {
            Text = string.IsNullOrEmpty(name) ? fullName : name,
            FontWeight = FontWeight.SemiBold,
            FontSize = 13,
        });
        titleRow.Children.Add(new TextBlock
        {
            Text = installed
                ? Loc.T("settings.market.badgeInstalled", "● 已安装")
                : verified
                    ? Loc.T("settings.market.badgeVerified", "✔ 已验证")
                    : Loc.T("settings.market.badgeUnverified", "⚠ 未验证"),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = TryBrush(installed || verified ? "AccentBrush" : "MutedForegroundBrush"),
        });
        titleRow.Children.Add(new TextBlock
        {
            Text = $"⭐ {stars}",
            FontSize = 11,
            Opacity = 0.7,
            VerticalAlignment = VerticalAlignment.Center,
        });
        panel.Children.Add(titleRow);

        string meta = string.Join("　", new[]
        {
            version.Length > 0 ? "v" + version : "",
            pushed.Length > 0 ? Loc.T("settings.market.updatedAt", "更新于 {0}", pushed) : "",
            license,
            fullName,
        });
        panel.Children.Add(new TextBlock { Text = meta, FontSize = 11, Opacity = 0.65, TextWrapping = TextWrapping.Wrap });

        if (description.Length > 0)
            panel.Children.Add(new TextBlock
            {
                Text = description.Length > 240 ? description[..240] + "…" : description,
                FontSize = 12,
                TextWrapping = TextWrapping.Wrap,
                Opacity = 0.9,
            });

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 4, 0, 0) };
        var action = new Button
        {
            Content = installed
                ? Loc.T("settings.button.uninstall", "卸载")
                // 未验证的技能**不允许安装**（与 WPF 侧同一规则：ActionText/CanAct）。
                // 第三方技能以本地账户权限运行，未验证就放行等于把这个判断交给用户瞎猜。
                : verified
                    ? Loc.T("settings.button.install", "安装")
                    : Loc.T("settings.market.unverified", "未验证"),
            Padding = new Thickness(12, 3),
            FontSize = 11,
            IsEnabled = installed || verified,
        };
        action.Click += async (_, _) => await OnActionAsync(fullName, !installed, action);
        buttons.Children.Add(action);

        var detail = new Button
        {
            Content = Loc.T("settings.market.detail", "详情"),
            Padding = new Thickness(12, 3),
            FontSize = 11,
        };
        detail.Click += async (_, _) => await ShowDetailAsync(fullName, name);
        buttons.Children.Add(detail);

        var html = GetString(item, "html_url");
        if (html.Length > 0)
        {
            buttons.Children.Add(new TextBlock
            {
                Text = html,
                FontSize = 10,
                Opacity = 0.55,
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
                MaxWidth = 420,
            });
        }
        panel.Children.Add(buttons);

        return new Border
        {
            Background = TryBrush("CardBackgroundBrush"),
            BorderBrush = TryBrush("CardBorderBrush"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(12, 10),
            Child = panel,
            // 双击详情要靠它找回这一条的数据（ItemsControl 没有 SelectedItem）
            Tag = item,
        };
    }

    /// <summary>
    /// 安装 / 卸载。**就地二次确认**：第一次点击先显示安全提示（这是产品要求用户必须看到的
    /// 信息，不能因为换前端就省掉），再点一次才真正执行。
    /// </summary>
    private async Task OnActionAsync(string repo, bool installing, Button button)
    {
        string key = repo + (installing ? "|install" : "|uninstall");
        if (_pendingRepo != key)
        {
            _pendingRepo = key;
            _pendingAction = installing ? "install" : "uninstall";
            button.Content = installing
                ? Loc.T("settings.market.confirmInstallShort", "确认安装？")
                : Loc.T("settings.market.uninstallConfirmShort", "确认卸载？");
            // 包值要 4 个参数（技能 / 仓库 / 版本 / 来源）：只传 repo 会抛 FormatException，
            // Loc.T 返回模板原文 → 整段安全提示变成「技能：{0} 仓库：{1} …」（硬编码清查 §5.3 C-1）。
            // 三个附加值从列表里按 full_name 反查（与 WPF 传 item.Name/Version/HtmlUrl 同一口径）。
            string skillName = "", skillVersion = "", skillSource = "";
            foreach (JsonElement entry in _items)
            {
                if (GetString(entry, "full_name") != repo)
                    continue;
                skillName = GetString(entry, "name");
                skillVersion = GetString(entry, "version");
                skillSource = GetString(entry, "html_url");
                break;
            }
            string skillUnknown = Loc.T("settings.market.state.unknown", "未知");
            MarketStatusText.Text = installing
                ? Loc.T("settings.market.installConfirmText",
                    "即将从 GitHub 安装第三方技能：\n\n技能：{0}\n仓库：{1}\n版本：{2}\n来源：{3}\n\n"
                    + "⚠️ 安全提示：第三方技能以你的本地账户权限运行，EchoDesk 不对其安全性负责。\n"
                    + "安装前会读取并展示其 skill.json 以了解权限与依赖。是否继续？",
                    skillName.Length > 0 ? skillName : repo, repo,
                    skillVersion.Length > 0 ? skillVersion : skillUnknown,
                    skillSource.Length > 0 ? skillSource : repo)
                : Loc.T("settings.market.uninstallConfirm",
                    "确定卸载技能「{0}」吗？（将删除其安装目录并从技能清单移除）再点一次按钮确认。", repo);
            _pendingTimer?.Stop();
            _pendingTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
            _pendingTimer.Tick += (_, _) =>
            {
                _pendingTimer?.Stop();
                _pendingRepo = "";
                button.Content = installing
                    ? Loc.T("settings.button.install", "安装")
                    : Loc.T("settings.button.uninstall", "卸载");
                MarketStatusText.Text = "";
            };
            _pendingTimer.Start();
            return;
        }

        _pendingTimer?.Stop();
        _pendingRepo = "";
        await CallMarketAsync(
            installing ? new { action = "market_install", repo }
                       : (object)new { action = "market_uninstall", repo },
            installing ? Loc.T("settings.button.install", "安装")
                       : Loc.T("settings.button.uninstall", "卸载"));
        button.Content = installing
            ? Loc.T("settings.button.uninstall", "卸载")
            : Loc.T("settings.button.install", "安装");
    }

    private async Task CallMarketAsync(object request, string verb)
    {
        MarketStatusText.Text = Loc.T("settings.market.callRunning",
            "正在{0}…（下载源码并校验 skill.json）", verb);
        try
        {
            using JsonDocument? doc = await SendAsync(request);
            string content = "";
            if (doc is not null && doc.RootElement.TryGetProperty("content", out JsonElement c)
                && c.ValueKind == JsonValueKind.String)
            {
                content = c.GetString() ?? "";
            }
            MarketStatusText.Text = content.Length > 0
                ? content
                : Loc.T("settings.market.callDone", "{0}完成", verb);
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = Loc.T("settings.market.callFailed", "❌ {0}失败：{1}", verb, ex.Message);
            return;
        }
        // 重新搜索以刷新「已安装」徽标（与 WPF 版一致）。
        await RefreshAsync();
    }

    /// <summary>双击列表项 → 打开该技能的详情（与点「详情」同一路径）。</summary>
    private async void OnListDoubleTapped(object? sender, TappedEventArgs e)
    {
        // MarketList 是 ItemsControl（不是 ListBox），没有 SelectedItem；
        // 卡片容器上存了该条目的 JsonElement（见 BuildCard 的 Tag），
        // 从被点的元素沿**逻辑父链**往上找即可。
        Control? current = e.Source as Control;
        while (current is not null)
        {
            if (current.Tag is JsonElement item)
            {
                string fullName = GetString(item, "full_name");
                string name = GetString(item, "name");
                if (fullName.Length > 0)
                    await ShowDetailAsync(fullName, name);
                return;
            }
            current = current.Parent as Control;
        }
    }

    /// <summary>技能详情：复用骨架右侧的字段帮助面板，不另造对话框。</summary>
    private async Task ShowDetailAsync(string repo, string name)
    {
        if (_host is null)
            return;
        _host.ShowFieldHelp(
            Loc.T("settings.market.detailDialogTitle", "技能详情") + "：" + (name.Length > 0 ? name : repo),
            Loc.T("settings.market.loadingList", "正在获取技能列表…"));
        using JsonDocument? doc = await SendAsync(new { action = "market_detail", repo });
        if (doc is null)
        {
            _host.ShowFieldHelp(
                Loc.T("settings.market.detailDialogTitle", "技能详情"),
                // 包值是「❌ 获取详情失败：{0}」：不传参会露出 {0}（硬编码清查 §5.3 C-1）
                Loc.T("settings.market.detailFailed", "❌ 获取详情失败：{0}",
                    Loc.T("main.error.noResponse", "无响应")));
            return;
        }
        JsonElement root = doc.RootElement;
        string body = root.TryGetProperty("content", out JsonElement c) && c.ValueKind == JsonValueKind.String
            ? c.GetString() ?? ""
            : root.ToString();
        _host.ShowFieldHelp(
            // 包值是「技能：{0}」，冒号在语言包里，代码里不再补一个（否则出现「技能：{0}：名字」）
            Loc.T("settings.market.detailSkill", "技能：{0}", name.Length > 0 ? name : repo),
            body.Length > 0 ? body : Loc.T("settings.market.detailNone", "（该技能没有提供详情）"),
            repo);
    }

    private static Brush? TryBrush(string key)
    {
        if (Application.Current is null)
            return null;
        if (Application.Current.TryFindResource(key, out object? value) && value is Brush brush)
            return brush;
        return null;
    }

    private static string GetString(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? ""
            : "";

    private static bool GetBool(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.True;

    // -----------------------------------------------------------------
    // 文案
    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        // 区块标题：与 WPF 的「搜索技能」同一个键、同一个字号口径（15 号加粗强调色）。
        MarketTitle.Text = Loc.T("settings.market.title", "搜索技能");
        MarketSearchButton.Content = Loc.T("settings.market.search", "🔍 搜索");
        MarketRefreshButton.Content = Loc.T("settings.market.refresh", "🔄 刷新");
        MarketSearchTip.Text = Loc.T("settings.market.tip", "输入关键词后回车，也支持「作者/仓库」形式。");
        MarketSearchBox.PlaceholderText = Loc.T("settings.market.searchTip", "搜索技能（留空浏览全部）");
        // 排序下拉项的文案是委托，重建后按值选回。
        string? current = (MarketSortCombo.SelectedItem as SortOption)?.Value;
        var options = new List<SortOption>();
        if (MarketSortCombo.ItemsSource is IEnumerable<SortOption> existing)
            options.AddRange(existing);
        else
            BuildOptions();
        if (options.Count > 0)
        {
            MarketSortCombo.ItemsSource = null;
            MarketSortCombo.ItemsSource = options;
            int index = 0, i = 0;
            foreach (SortOption option in options)
            {
                if (option.Value == current) { index = i; break; }
                i++;
            }
            MarketSortCombo.SelectedIndex = index;
        }
        // 卡片文本是运行时构建的：语言变了要重排一次（不重新请求后端）。
        if (_loaded && _items.Count > 0)
        {
            var cards = new List<Control>();
            foreach (JsonElement item in _items)
                cards.Add(BuildCard(item));
            MarketList.ItemsSource = cards;
        }
    }
}
