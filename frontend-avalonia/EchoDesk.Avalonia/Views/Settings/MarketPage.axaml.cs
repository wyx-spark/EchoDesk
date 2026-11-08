// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/MarketPage.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/MarketPage.axaml.cs。类型与成员说明见下方注释。
// [EN] Interaction logic of the skill market page (v0.2.1 cross-platform batch 2,
// [EN] development task 1; the fourth migrated module).
// [ZH] 技能市场页的交互逻辑（v0.2.1 跨平台-2 任务一，第 4 个迁移模块）。
// [EN]
// [EN] Backend actions (consistent with the WPF version):
// [ZH] 后端动作（与 WPF 版一致）：
// [EN]   market_search   { keyword, sort, page } → { market: { items: [ { full_name, name,
// [EN]                     description, verified, installed, stars, pushed_at, license,
// [EN]                     version, html_url } ] } }
// [ZH]   market_search   { keyword, sort, page } → { market: { items: [ { full_name, name,
// [ZH]                     description, verified, installed, stars, pushed_at, license,
// [ZH]                     version, html_url } ] } }
// [EN]   market_detail   { repo }                → skill details (the content of skill.json)
// [ZH]   market_detail   { repo }                → 技能详情（skill.json 的内容）
// [EN]   market_install  { repo }                → { content } (backend explanation text)
// [ZH]   market_install  { repo }                → { content }（后端给的说明文本）
// [EN]   market_uninstall{ repo }                → { content }
// [ZH]   market_uninstall{ repo }                → { content }
// [EN]
// [EN] The only differences from the WPF version are the parts that "follow the platform":
// [ZH] 与 WPF 版的差异只有“跟着平台走”的部分：
// [EN]   * **The install/uninstall confirmation** becomes an in-place second confirmation
// [EN]     (the button turns into "Confirm install? / Confirm uninstall?") — Avalonia has
// [EN]     no built-in MessageBox. **The safety notice text is still shown** (in the
// [EN]     status line), because the sentence "third-party skills run with your local
// [EN]     account privileges" is something the product requires the user to see and must
// [EN]     not be dropped just because the front end changed.
// [ZH]   * **安装/卸载的确认**改成就地二次确认（按钮变「确认安装？/确认卸载？」）——
// [ZH]     Avalonia 没有内置 MessageBox。**但安全提示文本照旧显示**（在状态行），
// [ZH]     因为“第三方技能以你的本地账户权限运行”这句是产品要求必须让用户看到的，
// [ZH]     不能因为换前端就省掉。
// [EN]   * **Skill details** are shown in the field help panel on the right of the shell
// [EN]     instead of a dialog — no extra window to build, and the details and the list
// [EN]     can be compared on the same screen.
// [ZH]   * **技能详情**用骨架右侧的字段帮助面板显示，而不是弹对话框 ——
// [ZH]     既不用再造窗口，又能让详情与列表同屏对照。
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

    /// <summary>
    /// [EN] Sort option: Tag is the sort value sent to the backend; the text is chosen by
    /// [EN] language in ApplyTexts.
    /// [ZH] 排序选项：Tag 是发给后端的 sort 值，文案在 ApplyTexts 里按语言取。
    /// </summary>
    private sealed record SortOption(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    private readonly List<JsonElement> _items = new();

    /// <summary>
    /// [EN] Full name of the repository awaiting the second confirmation (install and
    /// [EN] uninstall share one in-place confirmation flow).
    /// [ZH] 等待二次确认的仓库全名（安装与卸载共用一套就地确认）。
    /// </summary>
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
        // [EN] Search as you type (300 ms debounce): the same behavior as
        // [EN] MarketSearchBox_TextChanged on the WPF side. Why it is needed: users
        // [EN] expect to "type and see results"; searching only on Enter makes them think
        // [EN] the box is broken. The debounce avoids hitting the remote side on every
        // [EN] keystroke.
        // [ZH] 输入即搜（300ms 防抖）：与 WPF 侧的 MarketSearchBox_TextChanged 同一行为。
        // [ZH] 为什么要有：用户的心智是“边打边看结果”，按一次回车才搜会让人以为搜索框坏了；
        // [ZH] 防抖是为了不每敲一个字就打一次远端。
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
            // [EN] Refresh = clear the keyword and search again (consistent with WPF);
            // [EN] clearing also raises TextChanged once, so a reentrancy gate blocks the
            // [EN] duplicate request.
            // [ZH] 刷新 = 清空关键词再搜（与 WPF 一致）；清空会让 TextChanged 也触发一次，
            // [ZH] 用重入闸挡住重复请求。
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
    // [EN] Data
    // [ZH] 数据
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
        // [EN] Double-clicking an entry opens the details (the same behavior as
        // [EN] MarketList_MouseDoubleClick on the WPF side). The handler is attached to
        // [EN] the list rather than to each item: cards are built at runtime, and
        // [EN] attaching per item would repeat the handler N times.
        // [ZH] 双击条目打开详情（与 WPF 侧的 MarketList_MouseDoubleClick 同一行为）。
        // [ZH] 挂在列表上而不是逐项挂：卡片是运行时构建的，逐项挂会重复 N 次。
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
            // [EN] The message-pack value takes two arguments ("{0} results in total,
            // [EN] {1} on this page"). The total comes from the total_count returned by
            // [EN] the backend (the same field as `SettingsWindow.xaml.cs:1469` in WPF);
            // [EN] when unavailable it falls back to the number of items on this page.
            // [ZH] 包值要两个参数：「共 {0} 个结果，本页 {1} 个」。总数取后端回传的
            // [ZH] total_count（与 WPF `SettingsWindow.xaml.cs:1469` 同一字段），
            // [ZH] 取不到时退化为本页条数。
            int total = market.TryGetProperty("total_count", out JsonElement tc)
                        && tc.ValueKind == JsonValueKind.Number ? tc.GetInt32() : cards.Count;
            MarketStatusText.Text = Loc.T("settings.market.resultCount",
                "共 {0} 个结果，本页 {1} 个", total, cards.Count);
        }
    }

    // -----------------------------------------------------------------
    // [EN] Cards
    // [ZH] 卡片
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
                // [EN] Unverified skills **cannot be installed** (the same rule as
                // [EN] ActionText/CanAct on the WPF side): third-party skills run with
                // [EN] local account privileges, so letting an unverified one through
                // [EN] would leave that judgment to the user's guesswork.
                // [ZH] 未验证的技能**不允许安装**（与 WPF 侧同一规则：ActionText/CanAct）。
                // [ZH] 第三方技能以本地账户权限运行，未验证就放行等于把这个判断交给
                // [ZH] 用户瞎猜。
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
            // [EN] Double-click details rely on it to find the data of this entry again
            // [EN] (ItemsControl has no SelectedItem).
            // [ZH] 双击详情要靠它找回这一条的数据（ItemsControl 没有 SelectedItem）
            Tag = item,
        };
    }

    /// <summary>
    /// [EN] Install / uninstall. **In-place second confirmation**: the first click only
    /// [EN] shows the safety notice (information the product requires the user to see and
    /// [EN] that must not be dropped just because the front end changed); a second click
    /// [EN] actually performs the action.
    /// [ZH] 安装 / 卸载。**就地二次确认**：第一次点击先显示安全提示（这是产品要求用户
    /// [ZH] 必须看到的信息，不能因为换前端就省掉），再点一次才真正执行。
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
            // [EN] The message-pack value takes 4 arguments (skill / repository / version
            // [EN] / source): passing only repo throws FormatException, and Loc.T then
            // [EN] returns the raw template, turning the whole safety notice into
            // [EN] "skill: {0} repository: {1} ..." (hardcoded-text audit §5.3 C-1).
            // [EN] The three additional values are looked up in the list by full_name (the
            // [EN] same convention as item.Name/Version/HtmlUrl in WPF).
            // [ZH] 包值要 4 个参数（技能 / 仓库 / 版本 / 来源）：只传 repo 会抛
            // [ZH] FormatException，Loc.T 返回模板原文 → 整段安全提示变成
            // [ZH] 「技能：{0} 仓库：{1} …」（硬编码清查 §5.3 C-1）。
            // [ZH] 三个附加值从列表里按 full_name 反查（与 WPF 传
            // [ZH] item.Name/Version/HtmlUrl 同一口径）。
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
        // [EN] Search again to refresh the "Installed" badge (consistent with the WPF
        // [EN] version).
        // [ZH] 重新搜索以刷新「已安装」徽标（与 WPF 版一致）。
        await RefreshAsync();
    }

    /// <summary>
    /// [EN] Double-clicks a list entry → opens the details of that skill (the same path
    /// [EN] as clicking "Details").
    /// [ZH] 双击列表项 → 打开该技能的详情（与点「详情」同一路径）。
    /// </summary>
    private async void OnListDoubleTapped(object? sender, TappedEventArgs e)
    {
        // [EN] MarketList is an ItemsControl (not a ListBox) and has no SelectedItem; the
        // [EN] card container stores the JsonElement of that entry (see Tag in BuildCard);
        // [EN] walking up the **logical parent chain** from the clicked element is enough.
        // [ZH] MarketList 是 ItemsControl（不是 ListBox），没有 SelectedItem；
        // [ZH] 卡片容器上存了该条目的 JsonElement（见 BuildCard 的 Tag），
        // [ZH] 从被点的元素沿**逻辑父链**往上找即可。
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

    /// <summary>
    /// [EN] Skill details: reuse the field help panel on the right of the shell instead of
    /// [EN] creating another dialog.
    /// [ZH] 技能详情：复用骨架右侧的字段帮助面板，不另造对话框。
    /// </summary>
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
                // [EN] The message-pack value is the "failed to get details" template:
                // [EN] without arguments the UI would expose {0} (hardcoded-text audit
                // [EN] §5.3 C-1).
                // [ZH] 包值是「❌ 获取详情失败：{0}」：不传参会露出 {0}
                // [ZH] （硬编码清查 §5.3 C-1）
                Loc.T("settings.market.detailFailed", "❌ 获取详情失败：{0}",
                    Loc.T("main.error.noResponse", "无响应")));
            return;
        }
        JsonElement root = doc.RootElement;
        string body = root.TryGetProperty("content", out JsonElement c) && c.ValueKind == JsonValueKind.String
            ? c.GetString() ?? ""
            : root.ToString();
        _host.ShowFieldHelp(
            // [EN] The message-pack value is "skill: {0}"; the colon is in the language
            // [EN] pack and is not appended again in code (otherwise it would read
            // [EN] "skill: {0}: name").
            // [ZH] 包值是「技能：{0}」，冒号在语言包里，代码里不再补一个
            // [ZH] （否则出现「技能：{0}：名字」）
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
    // [EN] Texts
    // [ZH] 文案
    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        // [EN] Section title: the same key and the same font-size convention as "Search
        // [EN] skills" in WPF (15 pt, bold, accent color).
        // [ZH] 区块标题：与 WPF 的「搜索技能」同一个键、同一个字号口径（15 号加粗强调色）。
        MarketTitle.Text = Loc.T("settings.market.title", "搜索技能");
        MarketSearchButton.Content = Loc.T("settings.market.search", "🔍 搜索");
        MarketRefreshButton.Content = Loc.T("settings.market.refresh", "🔄 刷新");
        MarketSearchTip.Text = Loc.T("settings.market.tip", "输入关键词后回车，也支持「作者/仓库」形式。");
        MarketSearchBox.PlaceholderText = Loc.T("settings.market.searchTip", "搜索技能（留空浏览全部）");
        // [EN] The texts of the sort dropdown items are delegates; after rebuilding they
        // [EN] are selected back by value.
        // [ZH] 排序下拉项的文案是委托，重建后按值选回。
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
        // [EN] Card texts are built at runtime: when the language changes they must be
        // [EN] rebuilt once (without requesting the backend again).
        // [ZH] 卡片文本是运行时构建的：语言变了要重排一次（不重新请求后端）。
        if (_loaded && _items.Count > 0)
        {
            var cards = new List<Control>();
            foreach (JsonElement item in _items)
                cards.Add(BuildCard(item));
            MarketList.ItemsSource = cards;
        }
    }
}
