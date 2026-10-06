// 常规页的交互逻辑（v0.2.1 跨平台-2 任务一，第 2 个迁移模块）。
//
// 与 WPF 版的差别只有"跟着平台机制走"的那部分，行为不变：
//   * 主题 / 语言 / 字号**即时生效并立即落盘**（AppState.SetTheme / SetLanguage / SetFontSize）。
//     WPF 版是"改完点保存"；Avalonia 版主窗口本来就有同样的即时按钮（A- / A+ / 🌓 / 语言），
//     设置页跟着即时，两边行为才一致，用户不会遇到"这里改了要保存、那里改了不用"。
//   * emoji 偏好同理（走 AppState.SetEmojiPreference）。
//   * 文案在 ApplyTexts() 里统一重建 —— Avalonia 没有 WPF 的附加属性遍历机制
//     （这是记录里"必须重写"的一处）。
//   * 默认模式是**业务设置**：前端不存，只通过 /mode 指令告诉后端（与 WPF 版同一分工）。
using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using Avalonia.Controls;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class GeneralPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;

    /// <summary>载入控件值时抑制"用户改动"事件 —— 否则一打开设置页就显示"有未保存改动"。</summary>
    private bool _applying;

    /// <summary>一个下拉项：<c>Value</c> 是存进设置的规范值，<c>Text</c> 返回当前语言的文案。</summary>
    private sealed record Option(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    public GeneralPage()
    {
        InitializeComponent();
        BuildOptions();
        HookEvents();
    }

    public string TabKey => "settings.tab.general";
    public string TabFallback => "常规";
    public string AutomationId => "SettingsTabGeneral";

    public void Attach(ISettingsHost host) => _host = host;

    // -----------------------------------------------------------------
    // 下拉项构造
    // -----------------------------------------------------------------
    private void BuildOptions()
    {
        ThemeCombo.ItemsSource = new List<Option>
        {
            new("auto", () => Loc.T("settings.general.themeSystem", "跟随系统")),
            new("light", () => Loc.T("settings.general.themeLight", "浅色")),
            new("dark", () => Loc.T("settings.general.themeDark", "深色")),
        };
        EmojiCombo.ItemsSource = new List<Option>
        {
            new("auto", () => Loc.T("settings.general.emojiAuto", "自动（按本机字体）")),
            new("always", () => Loc.T("settings.general.emojiAlways", "始终显示 emoji")),
            new("never", () => Loc.T("settings.general.emojiNever", "不使用 emoji（纯文本符号）")),
        };
        DefaultModeCombo.ItemsSource = new List<Option>
        {
            new("chat", () => Loc.T("mode.chat", "对话")),
            new("contract", () => Loc.T("mode.contract", "合同")),
            new("agent", () => Loc.T("mode.agent", "智能体")),
        };

        // 语言项：auto + 语言包实际存在的语言（AvailableLocales 来自磁盘与内嵌资源）。
        var languages = new List<Option>
        {
            new("auto", () => Loc.T("settings.general.languageAuto", "自动（跟随系统区域）")),
        };
        foreach (string locale in Loc.AvailableLocales())
            languages.Add(new(locale, () => Loc.DisplayName(locale)));
        LanguageCombo.ItemsSource = languages;
    }

    // -----------------------------------------------------------------
    // 事件
    // -----------------------------------------------------------------
    private void HookEvents()
    {
        ThemeCombo.SelectionChanged += (_, _) =>
        {
            if (_applying || Selected(ThemeCombo) is not { } value || _host is null)
                return;
            _host.State.SetTheme(value);
            _host.ShowStatus(Loc.T("settings.general.themeEffective", "当前生效：{0}",
                Loc.T($"settings.general.theme{(value == "auto" ? "Following" : value == "light" ? "Light" : "Dark")}",
                    value == "light" ? "浅色" : value == "dark" ? "深色" : "跟随系统")));
            _host.MarkDirty();
        };
        EmojiCombo.SelectionChanged += (_, _) =>
        {
            if (_applying || Selected(EmojiCombo) is not { } value || _host is null)
                return;
            _host.State.SetEmojiPreference(value);
            _host.ShowStatus(value == "never"
                ? Loc.T("settings.general.emojiForcedOff", "已按你的选择改用纯文本符号")
                : Loc.T("settings.general.emojiOn", "本机支持 emoji，当前正常显示"));
            _host.MarkDirty();
        };
        LanguageCombo.SelectionChanged += (_, _) =>
        {
            if (_applying || Selected(LanguageCombo) is not { } value || _host is null)
                return;
            _host.State.SetLanguage(value);
            _host.MarkDirty();
        };
        FontSizeSlider.PropertyChanged += (_, e) =>
        {
            if (e.Property != Slider.ValueProperty)
                return;
            FontSizeValueText.Text = $"{FontSizeSlider.Value:0} px";
            if (_applying || _host is null)
                return;
            _host.State.SetFontSize(FontSizeSlider.Value);
            _host.MarkDirty();
        };
        ConfirmDestructiveCheck.IsCheckedChanged += (_, _) =>
        {
            if (_applying || _host is null)
                return;
            _host.MarkDirty();
            _host.ShowStatus(ConfirmDestructiveCheck.IsChecked == true
                ? Loc.T("settings.general.confirmOn", "已开启：执行有副作用的指令前会二次确认")
                : Loc.T("settings.general.confirmOff", "已关闭：有副作用的指令将直接执行"));
        };
        DefaultModeCombo.SelectionChanged += (_, _) =>
        {
            if (_applying || _host is null)
                return;
            _host.MarkDirty();
        };
    }

    private static string? Selected(ComboBox combo) =>
        (combo.SelectedItem as Option)?.Value;

    private static void Select(ComboBox combo, string? value)
    {
        if (combo.ItemsSource is not IEnumerable<Option> options)
            return;
        int index = 0, i = 0;
        foreach (Option option in options)
        {
            if (string.Equals(option.Value, value, StringComparison.OrdinalIgnoreCase))
            {
                index = i;
                break;
            }
            i++;
        }
        combo.SelectedIndex = index;
    }

    // -----------------------------------------------------------------
    // ISettingsPage
    // -----------------------------------------------------------------
    public void LoadFrom(AppSettings settings)
    {
        _applying = true;
        try
        {
            Select(ThemeCombo, settings.Theme);
            Select(EmojiCombo, settings.EmojiPreference);
            Select(LanguageCombo, settings.Language);
            // 默认模式来自后端（见 RefreshAsync），这里先占位在第一项。
            if (DefaultModeCombo.SelectedIndex < 0)
                DefaultModeCombo.SelectedIndex = 0;
            FontSizeSlider.Value = settings.FontSize;
            FontSizeValueText.Text = $"{settings.FontSize:0} px";
            ConfirmDestructiveCheck.IsChecked = settings.ConfirmDestructiveCommands;
        }
        finally
        {
            _applying = false;
        }
    }

    public void ApplyTo(AppSettings settings)
    {
        // 主题 / 语言 / 字号 / emoji 已经在改动那一刻即时落盘（见 HookEvents），
        // 这里只收"改了但没即时落盘"的那一项。
        settings.ConfirmDestructiveCommands = ConfirmDestructiveCheck.IsChecked == true;
    }

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        // 默认模式：后端启动时按 config.json 设定。前端读一次回填，避免显示与实际不符。
        try
        {
            using System.Text.Json.JsonDocument? doc =
                await _host.State.Backend.SendAsync(new { action = "context" });
            if (doc is not null
                && doc.RootElement.TryGetProperty("mode", out var mode)
                && mode.ValueKind == System.Text.Json.JsonValueKind.String)
            {
                _applying = true;
                try
                {
                    Select(DefaultModeCombo, mode.GetString());
                }
                finally
                {
                    _applying = false;
                }
            }
        }
        catch (Exception)
        {
            // 后端没起来时不该让设置窗口打不开：默认模式只是回填，失败就保持默认显示。
        }
    }

    public async Task ApplyToBackendAsync()
    {
        if (_host is null)
            return;
        // 默认模式是**业务设置**：后端持有它，前端只把选择发过去（与 WPF 版同一分工）。
        // 走 /mode 指令而不是直接改 config.json —— 这条路径后端已经实现并验证过，
        // 前端自己写配置文件会绕过后端的校验与迁移逻辑。
        string? mode = Selected(DefaultModeCombo);
        if (string.IsNullOrEmpty(mode))
            return;
        await _host.State.Backend.SendAsync(new { action = "chat", message = $"/mode {mode}" });
    }

    public void ApplyTexts()
    {
        ThemeLabel.Text = Loc.T("settings.general.themeLabel", "界面主题");
        EmojiLabel.Text = Loc.T("settings.general.emojiLabel", "emoji 显示");
        EmojiStatusText.Text = Loc.T("settings.general.emojiHelp",
            "自动：本机有 emoji 字体就显示彩色图标，没有就换成纯文本标记（如 [OK]）。");
        FontSizeLabel.Text = Loc.T("settings.general.fontSize", "界面字号");
        LanguageLabel.Text = Loc.T("settings.general.language", "界面语言");
        LangPackText.Text = Loc.T("settings.general.languageHelp",
            "切换后界面、错误提示与指令帮助立即生效，无需重启。选择结果记录在 ui_state.json。");
        // 默认模式的键在语言包里是一条完整说明（"默认对话模式（下次启动生效…）"），
        // 既当标题也当提示 —— 不另造一个更短的键，避免两处文案将来各说各话。
        DefaultModeLabel.Text = Loc.T("settings.general.defaultMode", "默认对话模式");
        DefaultModeHint.Text = Loc.T("settings.general.defaultMode",
            "默认对话模式（下次启动生效，也可用 /mode 即时切换）");
        ConfirmDestructiveCheck.Content = Loc.T("settings.general.confirmDestructive",
            "执行有副作用的指令（/clear、/clearcache）前二次确认");
        PathsLabel.Text = Loc.T("settings.general.pathUserData", "用户数据目录");
        ThemeStatusText.Text = Loc.T("settings.general.themeNote",
            "主题与 emoji 显示即时生效，无需重启。");
        PathsText.Text = string.Join(Environment.NewLine, new[]
        {
            $"{Loc.T("settings.general.pathProgram", "程序目录")}：{AppContext.BaseDirectory}",
            $"{Loc.T("settings.general.pathUserData", "用户数据目录")}：{UserPaths.DataRoot}",
            $"{Loc.T("settings.general.langDir", "语言包目录")}：{UserPaths.LangDirectory}",
        });

        // 下拉项文案是委托，改了语言要把当前选中项显示刷新一遍。
        RefreshComboTexts(ThemeCombo, EmojiCombo, LanguageCombo, DefaultModeCombo);
    }

    /// <summary>
    /// 重设 ItemsSource 会丢掉选中项，因此这里"重建后按值选回"。
    ///
    /// **重建期间必须打开 _applying 重入闸**：这次刷新只换显示文案、选中值没变，
    /// 但 `Select()` 会把 SelectedIndex 从 -1 设回原值 —— 在控件看来那是一次**真实的选中变化**，
    /// 于是上面三个 SelectionChanged 处理器全部被触发。语言那一个最致命：
    /// SetLanguage → Loc.SetLocale → LanguageChanged → 设置窗口 ApplyTexts →
    /// 所有页面 ApplyTexts → 又回到这里 —— 一条**同步闭环**。
    /// 实测：打开设置窗口时 340 圈后栈溢出（6868 行栈，进程直接死），设置窗口永远打不开。
    /// LoadFrom（:163）与 RefreshAsync（:202）早就用了这个闸，只有"刷新文案"这一处漏了。
    /// </summary>
    private void RefreshComboTexts(params ComboBox[] combos)
    {
        bool previous = _applying;
        _applying = true;
        try
        {
            foreach (ComboBox combo in combos)
            {
                if (combo.ItemsSource is not IEnumerable<Option> options)
                    continue;
                string? current = (combo.SelectedItem as Option)?.Value;
                combo.ItemsSource = null;
                combo.ItemsSource = new List<Option>(options);
                Select(combo, current);
            }
        }
        finally
        {
            _applying = previous;
        }
    }
}
