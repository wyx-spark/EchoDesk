// 设置窗口骨架（v0.2.1 跨平台-2 任务一，第 1 个迁移模块）。
//
// 这一层刻意只做四件事，别的一律交给页面：
//   1. 页签容器：按 ISettingsPage 注册页面（未迁移的页签给出**明确的未迁移说明**，
//      不假装功能存在 —— 这是记录里"降级提示要给原因与替代做法"的同一条原则）；
//   2. 窗口状态记忆：尺寸存进 ui_state.json（与主窗口分开存，互不影响）；
//   3. 底部状态行与保存按钮：收集各页改动 → 落盘 → 需要后端的页自己去同步；
//   4. 主题 / 语言即时生效：主题立刻改（AppState.SetTheme），语言立刻重建全部文案
//      （Avalonia 用事件重建，WPF 用附加属性遍历 —— 机制不同，效果一致）。
using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using EchoDesk.Avalonia.Services;
using EchoDesk.Avalonia.Views.Settings;

namespace EchoDesk.Avalonia.Views;

public partial class SettingsWindow : Window, ISettingsHost
{
    private readonly AppState _state;
    private readonly List<ISettingsPage> _pages = new();
    private readonly List<(TabItem Item, string Key, string Fallback)> _tabHeaders = new();
    private bool _dirty;

    /// <summary>
    /// 还没迁移到跨平台前端的页签。**现在是空的**：设置窗口的 7 个页签已全部迁移完成
    /// （v0.2.1 跨平台-2 任务一）。
    ///
    /// 保留这个数组而不是删掉，是因为**主窗口的各功能面板**还没迁移；将来若出现
    /// "先占位、后实现"的页签，在这里登记一行即可，不必把 AddNotMigratedTab 的逻辑找回来。
    /// </summary>
    private static readonly (string Key, string Fallback)[] NotMigratedTabs =
        Array.Empty<(string Key, string Fallback)>();

    public SettingsWindow() : this(new AppState())
    {
    }

    public SettingsWindow(AppState state)
    {
        _state = state;
        InitializeComponent();

        RegisterPage(new GeneralPage());
        RegisterPage(new InstalledPage());
        RegisterPage(new MarketPage());
        RegisterPage(new DeveloperPage());
        RegisterPage(new AgentPage());
        RegisterPage(new AutomationPage());
        RegisterPage(new ModelPage());
        foreach ((string key, string fallback) in NotMigratedTabs)
            AddNotMigratedTab(key, fallback);

        Loc.LanguageChanged += (_, _) => ApplyTexts();
        ApplyTexts();
        LoadFromSettings();

        // 窗口尺寸记忆（任务书要求）。Closing 里存，避免拖动过程中频繁写盘。
        Width = _state.Settings.SettingsWindowWidth;
        Height = _state.Settings.SettingsWindowHeight;
        Closing += (_, _) =>
        {
            _state.Settings.SettingsWindowWidth = Width;
            _state.Settings.SettingsWindowHeight = Height;
            _state.Settings.Save();
        };

        SaveButton.Click += async (_, _) => await SaveAsync();
        CloseButton.Click += (_, _) => Close();
        FieldHelpCloseButton.Click += (_, _) => ClearFieldHelp();
    }

    // -----------------------------------------------------------------
    // ISettingsHost
    // -----------------------------------------------------------------
    public AppState State => _state;

    public void ShowStatus(string text) => LoadStateText.Text = text;

    public void MarkDirty()
    {
        _dirty = true;
        SaveHintText.Text = Loc.T("settings.saveHint.dirty", "有未保存的改动");
    }

    public void ShowFieldHelp(string title, string body, string example = "")
    {
        FieldHelpTitle.Text = title;
        FieldHelpBody.Text = body;
        FieldHelpExample.Text = example;
        FieldHelpExample.IsVisible = !string.IsNullOrWhiteSpace(example);
        FieldHelpPanel.IsVisible = true;
    }

    public void ClearFieldHelp() => FieldHelpPanel.IsVisible = false;

    // -----------------------------------------------------------------
    // 页签注册
    // -----------------------------------------------------------------

    /// <summary>
    /// 选中某个页签（主窗口状态区各项都是"点一下跳到对应设置"，需要直接落到那一页）。
    /// 传的是 <see cref="ISettingsPage.TabKey"/>（如 <c>settings.tab.model</c>），
    /// 找不到就保持默认页 —— 不抛异常，跳转失败不该让设置窗口打不开。
    /// </summary>
    public bool SelectTab(string tabKey)
    {
        foreach ((TabItem item, string key, _) in _tabHeaders)
        {
            if (string.Equals(key, tabKey, StringComparison.OrdinalIgnoreCase))
            {
                Tabs.SelectedItem = item;
                return true;
            }
        }
        return false;
    }

    private void RegisterPage(ISettingsPage page)
    {
        page.Attach(this);
        page.ApplyTexts();
        var item = new TabItem
        {
            Header = Loc.T(page.TabKey, page.TabFallback),
            Content = (Control)page,
            Tag = page,
        };
        AutomationProperties.SetAutomationId(item, page.AutomationId);
        Tabs.Items.Add(item);
        _pages.Add(page);
        _tabHeaders.Add((item, page.TabKey, page.TabFallback));
    }

    /// <summary>
    /// 未迁移页签的占位：说清"这页在哪、现在怎么办"，而不是显示一个空面板。
    /// Windows 上有功能完整的 WPF 版可以直接用；非 Windows 上则明确说明该页暂不可用。
    /// </summary>
    private void AddNotMigratedTab(string key, string fallback)
    {
        var panel = new StackPanel
        {
            Margin = new Thickness(16),
            Spacing = 10,
        };
        var title = new TextBlock { FontWeight = FontWeight.SemiBold, FontSize = 14 };
        var body = new TextBlock { TextWrapping = TextWrapping.Wrap, Opacity = 0.85 };
        var hint = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 12, Opacity = 0.7 };
        panel.Children.Add(title);
        panel.Children.Add(body);
        panel.Children.Add(hint);

        void Fill()
        {
            title.Text = Loc.T("settings.notMigrated.title",
                "⏳ 这一页还没有迁移到跨平台前端（{0}）", Loc.T(key, fallback));
            body.Text = Loc.T("settings.notMigrated.body",
                "跨平台前端（Avalonia）目前完成了主窗口与设置窗口的骨架、常规页；"
                + "其余页签仍只在 WPF 版里可用。这是一个已知的未完成项，不是功能损坏。");
            hint.Text = PlatformInfo.Current.IsWindows
                ? Loc.T("settings.notMigrated.hintWindows",
                    "替代做法：在 Windows 上使用 WPF 版（正式安装包安装的那个），功能是完整的。")
                : Loc.T("settings.notMigrated.hintOther",
                    "替代做法：本页涉及的配置可以直接编辑 app/backend/config.json，"
                    + "或等待后续版本把该页迁移过来。");
        }

        Fill();
        var item = new TabItem { Header = Loc.T(key, fallback), Content = panel };
        Tabs.Items.Add(item);
        _tabHeaders.Add((item, key, fallback));
        Loc.LanguageChanged += (_, _) => Fill();
    }

    // -----------------------------------------------------------------
    // 文案 / 设置
    // -----------------------------------------------------------------
    private void ApplyTexts()
    {
        Title = Loc.T("settings.title", "EchoDesk 设置");
        SaveButton.Content = Loc.T("settings.button.save", "保存");
        CloseButton.Content = Loc.T("settings.button.close", "关闭");
        FieldHelpCloseButton.Content = Loc.T("settings.button.close", "关闭");
        LoadStateText.Text = Loc.T("settings.loadState.loaded", "✅ 配置已载入");
        SaveHintText.Text = _dirty
            ? Loc.T("settings.saveHint.dirty", "有未保存的改动")
            : "";
        foreach ((TabItem item, string key, string fallback) in _tabHeaders)
            item.Header = Loc.T(key, fallback);
        foreach (ISettingsPage page in _pages)
            page.ApplyTexts();
    }

    private void LoadFromSettings()
    {
        foreach (ISettingsPage page in _pages)
        {
            page.LoadFrom(_state.Settings);
            _ = page.RefreshAsync();
        }
    }

    private async Task SaveAsync()
    {
        foreach (ISettingsPage page in _pages)
            page.ApplyTo(_state.Settings);

        bool saved = _state.Settings.Save();
        LoadStateText.Text = saved
            ? Loc.T("settings.loadState.saved", "✅ 设置已保存")
            : Loc.T("settings.loadState.saveFailed", "⚠️ 设置无法写入（目录只读？）");

        // 业务设置（如默认模式）由页面自己同步给后端 —— 前端不持有第二份业务配置。
        foreach (ISettingsPage page in _pages)
        {
            try
            {
                await page.ApplyToBackendAsync();
            }
            catch (Exception ex)
            {
                LoadStateText.Text = Loc.T("settings.loadState.backendSyncFailed",
                    "⚠️ 部分设置未能同步到后端：{0}", ex.Message);
            }
        }

        _dirty = false;
        SaveHintText.Text = "";
    }
}
