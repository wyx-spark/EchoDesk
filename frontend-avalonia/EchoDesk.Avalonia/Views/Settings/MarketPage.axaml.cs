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
        MarketSearchButton.Click += async (_, _) => await SearchAsync();
        MarketRefreshButton.Click += async (_, _) =>
        {
            MarketSearchBox.Text = "";
            await SearchAsync();
        };
        MarketSortCombo.SelectionChanged += async (_, _) =>
        {
            if (!_loaded)
                await SearchAsync();
        };
    }

    private bool _loaded;

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
        if (empty)
        {
            MarketEmptyText.Text = Loc.T("settings.market.emptyResult", "没有匹配的技能。") + "\n"
                + Loc.T("settings.market.emptyStatus",
                    "试试更短的关键词，或点「刷新」浏览全部技能。");
        }
        else
        {
            MarketStatusText.Text = Loc.T("settings.market.resultCount", "共 {0} 个技能", cards.Count);
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
                : Loc.T("settings.button.install", "安装"),
            Padding = new Thickness(12, 3),
            FontSize = 11,
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
            MarketStatusText.Text = installing
                ? Loc.T("settings.market.installConfirmText",
                    "即将从 GitHub 安装第三方技能（{0}）。⚠️ 第三方技能以你的本地账户权限运行，"
                    + "EchoDesk 不对其安全性负责。再点一次按钮确认安装。", repo)
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
                Loc.T("settings.market.detailFailed", "❌ 读取技能详情失败"));
            return;
        }
        JsonElement root = doc.RootElement;
        string body = root.TryGetProperty("content", out JsonElement c) && c.ValueKind == JsonValueKind.String
            ? c.GetString() ?? ""
            : root.ToString();
        _host.ShowFieldHelp(
            Loc.T("settings.market.detailSkill", "技能") + "：" + (name.Length > 0 ? name : repo),
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
