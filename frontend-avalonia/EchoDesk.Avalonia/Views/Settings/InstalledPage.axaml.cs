// 已安装页的交互逻辑（v0.2.1 跨平台-2 任务一，第 3 个迁移模块）。
//
// 数据来自后端 `skills_list`，响应形状（与 WPF 版一致）：
//     { "skills": { "skills": [ { id, name, version, source, loaded, enabled, error,
//                                 overrode, commands[], parsers[], hooks[], panels[] } ] } }
//
// 与 WPF 版的差异只有两处，都是"跟着平台走"而不是行为改变：
//   * **卸载没有模态确认框**：Avalonia 没有内置 MessageBox，而要弹一个自定义对话框就得
//     再写一个窗口。这里改成"按钮就地变成『确认卸载？』、3 秒内再点一次才执行"——
//     同样是"要让用户确认一次"，但不引入一个新窗口与它的生命周期管理。
//     （WPF 版的 MessageBox 还会阻塞 UI 线程，记录里提到过它必须延后调用。）
//   * "从文件夹安装 / 新建本地插件"两个入口需要文件夹选择器与输入对话框，
//     本轮未迁移 —— 按钮不显示，而不是显示一个点了没反应的按钮。
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class InstalledPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;

    /// <summary>最近一次 skills_list 的原始响应，渲染卡片时反复用到。</summary>
    private JsonElement? _status;

    /// <summary>等待二次确认卸载的插件 id（见文件头注释）。</summary>
    private string _pendingUninstallId = "";
    private DispatcherTimer? _pendingTimer;

    public InstalledPage()
    {
        InitializeComponent();
        SkillsRefreshButton.Click += async (_, _) => await RefreshAsync();
        SkillsReloadButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_reload" });
            await RefreshAsync();
        };
        SkillsExamplesButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_examples" });
            await RefreshAsync();
        };
    }

    public string TabKey => "settings.tab.installed";
    public string TabFallback => "已安装";
    public string AutomationId => "SettingsTabInstalled";

    public void Attach(ISettingsHost host) => _host = host;

    // 已安装页全是"从后端读"的内容，没有需要收进 ui_state.json 的界面设置。
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }
    public Task ApplyToBackendAsync() => Task.CompletedTask;

    // -----------------------------------------------------------------
    // 数据
    // -----------------------------------------------------------------
    private async Task<JsonDocument?> SendAsync(object request)
    {
        if (_host is null)
            return null;
        return await _host.State.Backend.SendAsync(request, idleTimeout: TimeSpan.FromSeconds(60));
    }

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        SkillStatusText.Text = Loc.T("settings.installed.labelReadingSkills", "读取插件列表");
        using JsonDocument? doc = await SendAsync(new { action = "skills_list" });
        if (doc is null)
        {
            SkillStatusText.Text = Loc.T("settings.installed.backendGone",
                "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）");
            return;
        }
        if (!doc.RootElement.TryGetProperty("skills", out JsonElement status))
            return;
        _status = status.Clone();

        var cards = new List<Control>();
        if (status.TryGetProperty("skills", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in arr.EnumerateArray())
                cards.Add(BuildCard(item));
        }

        SkillList.ItemsSource = cards;
        bool empty = cards.Count == 0;
        SkillEmptyText.IsVisible = empty;
        SkillList.IsVisible = !empty;
        SkillStatusText.Text = empty
            ? ""
            : Loc.T("settings.installed.loadedCount", "已加载 {0} 个插件", cards.Count);
    }

    // -----------------------------------------------------------------
    // 卡片
    // -----------------------------------------------------------------
    private Control BuildCard(JsonElement item)
    {
        string id = GetString(item, "id");
        string name = GetString(item, "name");
        string source = GetString(item, "source");
        string version = GetString(item, "version");
        bool loaded = GetBool(item, "loaded");
        bool enabled = !item.TryGetProperty("enabled", out JsonElement en) || en.ValueKind != JsonValueKind.False;
        string error = GetString(item, "error");
        string overrode = GetString(item, "overrode");

        string sourceTag = source switch
        {
            "dev" => Loc.T("settings.installed.sourceDev", "🔧 开发"),
            "local" => Loc.T("settings.installed.sourceLocal", "📁 本地"),
            _ => Loc.T("settings.installed.sourceMarket", "🏪 市场"),
        };
        (string statusText, string statusBrush) = !enabled
            ? (Loc.T("settings.installed.statusDisabled", "⏸ 已禁用"), "MutedForegroundBrush")
            : error.Length > 0
                ? (Loc.T("settings.installed.statusFailed", "❌ 加载失败"), "AccentBrush")
                : loaded
                    ? (Loc.T("settings.installed.statusLoaded", "✅ 已加载"), "AccentBrush")
                    : (Loc.T("settings.installed.statusNotLoaded", "⏸ 未加载"), "MutedForegroundBrush");

        var panel = new StackPanel { Spacing = 4 };

        var titleRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        titleRow.Children.Add(new TextBlock
        {
            Text = string.IsNullOrEmpty(name) ? id : name,
            FontWeight = FontWeight.SemiBold,
            FontSize = 13,
        });
        titleRow.Children.Add(new TextBlock { Text = sourceTag, FontSize = 11, Opacity = 0.75, VerticalAlignment = VerticalAlignment.Center });
        if (!string.IsNullOrEmpty(version))
            titleRow.Children.Add(new TextBlock { Text = "v" + version, FontSize = 11, Opacity = 0.6, VerticalAlignment = VerticalAlignment.Center });
        titleRow.Children.Add(new TextBlock
        {
            Text = statusText,
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = TryBrush(statusBrush),
        });
        panel.Children.Add(titleRow);

        // 能力行（指令 / 接管格式 / 自动化钩子 / 面板）—— 与 WPF 版同口径，只显示非空的
        var caps = new List<string>();
        AppendList(caps, item, "commands", "settings.installed.commandsLabel", "指令：");
        AppendList(caps, item, "parsers", "settings.installed.parsersLabel", "接管格式：");
        AppendList(caps, item, "hooks", "settings.installed.hooksLabel", "自动化钩子：");
        AppendList(caps, item, "panels", "settings.installed.panelsLabel", "面板：");
        if (caps.Count > 0)
            panel.Children.Add(new TextBlock { Text = string.Join("　", caps), FontSize = 11, Opacity = 0.75, TextWrapping = TextWrapping.Wrap });

        if (overrode.Length > 0)
            panel.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.installed.overrode", "已覆盖：{0}", overrode),
                FontSize = 11,
                Opacity = 0.75,
                TextWrapping = TextWrapping.Wrap,
            });

        if (error.Length > 0)
            panel.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.installed.detail", "错误详情") + "：" + error,
                FontSize = 11,
                TextWrapping = TextWrapping.Wrap,
                Foreground = TryBrush("AccentBrush"),
            });

        var uninstall = new Button
        {
            Content = Loc.T("settings.installed.uninstall", "卸载"),
            Padding = new Thickness(10, 3),
            FontSize = 11,
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 4, 0, 0),
        };
        uninstall.Click += async (_, _) => await OnUninstallAsync(id, uninstall);
        panel.Children.Add(uninstall);

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

    private static void AppendList(List<string> into, JsonElement item, string field, string key, string fallback)
    {
        if (!item.TryGetProperty(field, out JsonElement arr) || arr.ValueKind != JsonValueKind.Array)
            return;
        var values = new List<string>();
        foreach (JsonElement entry in arr.EnumerateArray())
        {
            if (entry.ValueKind == JsonValueKind.String)
                values.Add(entry.GetString() ?? "");
        }
        if (values.Count > 0)
            into.Add(Loc.T(key, fallback) + string.Join(", ", values));
    }

    /// <summary>
    /// 卸载的二次确认：第一次点击把按钮变成"确认卸载？"，3 秒内再点一次才真正执行。
    /// 为什么不用模态对话框：Avalonia 没有内置 MessageBox，为一个确认框再造一个窗口
    /// 不划算，而完全不做确认又会让误点直接删插件。
    /// </summary>
    private async Task OnUninstallAsync(string id, Button button)
    {
        if (_pendingUninstallId != id)
        {
            _pendingUninstallId = id;
            button.Content = Loc.T("settings.installed.uninstallConfirmShort", "确认卸载？");
            _pendingTimer?.Stop();
            _pendingTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            _pendingTimer.Tick += (_, _) =>
            {
                _pendingTimer?.Stop();
                _pendingUninstallId = "";
                button.Content = Loc.T("settings.installed.uninstall", "卸载");
            };
            _pendingTimer.Start();
            return;
        }

        _pendingTimer?.Stop();
        _pendingUninstallId = "";
        await SendAsync(new { action = "skills_uninstall", id, delete_files = true });
        await RefreshAsync();
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
        SkillsRefreshButton.Content = Loc.T("settings.installed.refresh", "🔄 刷新");
        SkillsReloadButton.Content = Loc.T("settings.installed.reload", "🔄 重载本地插件");
        SkillsExamplesButton.Content = Loc.T("settings.developer.installExamples", "📦 安装示例插件");
        InstalledTipText.Text = Loc.T("settings.installed.tip",
            "这里列出市场安装、本地安装与开发目录中的全部插件及其加载状态。");
        SkillEmptyText.Text = Loc.T("settings.installed.empty",
            "还没有加载任何插件。可以在「技能市场」安装，或点「➕ 新建本地插件」开始写一个。");
        // 卡片内容是运行时构建的：语言变了要重建一次（Avalonia 没有 WPF 的附加属性遍历）。
        if (_status is { } status)
            _ = RefreshAsync();
    }
}
