// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/GeneralPage.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/GeneralPage.axaml.cs。类型与成员说明见下方注释。
// [EN] Interaction logic of the General page (v0.2.1 cross-platform-2 development task 1, the
// [EN] second migrated module).
// [ZH] 常规页的交互逻辑（v0.2.1 跨平台-2 研发任务 1，第 2 个迁移模块）。
//
// [EN] The only differences from the WPF version are the parts that follow each platform's
// [EN] mechanisms; behavior is unchanged:
// [EN]   * Theme / language / font size **take effect immediately and are saved at once**
// [EN]     (AppState.SetTheme / SetLanguage / SetFontSize).
// [EN]     The WPF version uses "edit, then click Save"; the Avalonia main window already has the
// [EN]     same immediate controls (A- / A+ / 🌓 / language), and the settings page follows suit
// [EN]     so both behave alike and users never face "this change needs saving, that one does
// [EN]     not".
// [EN]   * Emoji preference works the same way (through AppState.SetEmojiPreference).
// [EN]   * Text is rebuilt centrally in ApplyTexts() -- Avalonia lacks WPF's attached-property
// [EN]     traversal mechanism (one of the places recorded as "must be rewritten").
// [EN]   * The default mode is **business configuration**: the front end does not store it and
// [EN]     only tells the backend through the /mode command (same division of labor as WPF).
// [ZH] 与 WPF 版的差别只有“跟着平台机制走”的那部分，行为不变：
// [ZH]   * 主题 / 语言 / 字号**即时生效并立即落盘**（AppState.SetTheme / SetLanguage / SetFontSize）。
// [ZH]     WPF 版是“改完点保存”；Avalonia 版主窗口本来就有同样的即时按钮（A- / A+ / 🌓 / 语言），
// [ZH]     设置页跟着即时，两边行为才一致，用户不会遇到“这里改了要保存、那里改了不用”。
// [ZH]   * emoji 偏好同理（走 AppState.SetEmojiPreference）。
// [ZH]   * 文案在 ApplyTexts() 里统一重建 —— Avalonia 没有 WPF 的附加属性遍历机制
// [ZH]     （这是记录里“必须重写”的一处）。
// [ZH]   * 默认模式是**业务设置**：前端不存，只通过 /mode 指令告诉后端（与 WPF 版同一分工）。
using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using Avalonia.Controls;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class GeneralPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;

    /// <summary>
    /// [EN] Latest `media_state` snapshot (v0.2.6 Part 2). Null until the settings page
    /// [EN] has asked the backend, so the controls keep their defaults on a failure.
    /// [ZH] 最新的 `media_state` 快照（v0.2.6 Part 2）。在设置页向后端查询之前为 null，
    /// [ZH] 因此后端失败时控件保持默认值。
    /// </summary>
    private MediaState? _mediaSnapshot;

    /// <summary>
    /// [EN] Suppress "user changed" events while loading control values -- otherwise the settings
    /// [EN] page would show "unsaved changes" as soon as it opens.
    /// [ZH] 载入控件值时抑制“用户改动”事件 —— 否则一打开设置页就显示“有未保存改动”。
    /// </summary>
    private bool _applying;

    /// <summary>
    /// [EN] One drop-down item: <c>Value</c> is the canonical value stored in settings, and
    /// [EN] <c>Text</c> returns the text for the current language.
    /// [ZH] 一个下拉项：<c>Value</c> 是存进设置的规范值，<c>Text</c> 返回当前语言的文案。
    /// </summary>
    private sealed record Option(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    public GeneralPage()
    {
        InitializeComponent();
        BuildOptions();
        BuildRateOptions();
        HookEvents();
    }

    public string TabKey => "settings.tab.general";
    public string TabFallback => "常规";
    public string AutomationId => "SettingsTabGeneral";

    public void Attach(ISettingsHost host) => _host = host;

    // -----------------------------------------------------------------
    // [EN] Drop-down item construction
    // [ZH] 下拉项构造
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

        // [EN] Language items: auto plus the languages whose language packs actually exist
        // [EN] (AvailableLocales comes from disk and embedded resources).
        // [ZH] 语言项：auto + 语言包实际存在的语言（AvailableLocales 来自磁盘与内嵌资源）。
        var languages = new List<Option>
        {
            new("auto", () => Loc.T("settings.general.languageAuto", "自动（跟随系统区域）")),
        };
        foreach (string locale in Loc.AvailableLocales())
            languages.Add(new(locale, () => Loc.DisplayName(locale)));
        LanguageCombo.ItemsSource = languages;
    }

    // -----------------------------------------------------------------
    // [EN] Events
    // [ZH] 事件
    // -----------------------------------------------------------------
    private void HookEvents()
    {
        ThemeCombo.SelectionChanged += (_, _) =>
        {
            if (_applying || Selected(ThemeCombo) is not { } value || _host is null)
                return;
            _host.State.SetTheme(value);
            UpdateThemeStatus();
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
        LanguageCombo.SelectionChanged += async (_, _) =>
        {
            if (_applying || Selected(LanguageCombo) is not { } value || _host is null)
                return;
            _host.State.SetLanguage(value);
            _host.MarkDirty();
            // [EN] Language-pack information changes with the language (the "current language"
            // [EN] line); re-layout it.
            // [ZH] 语言包信息随语言变（“当前语言”那一行），重排一遍
            UpdateLangPackText();
            // [EN] Backend synchronization (aligned with WPF's LanguageCombo_SelectionChanged ->
            // [EN] SyncBackendLanguageAsync): system messages, error prompts, and the multilingual
            // [EN] resources of skills are all managed by the backend, so without syncing the user
            // [EN] would see a half-translated state such as "English UI, Chinese replies".
            // [ZH] 后端同步（对齐 WPF 的 LanguageCombo_SelectionChanged → SyncBackendLanguageAsync）：
            // [ZH] 系统消息、错误提示与技能的多语言资源都归后端管，不同步就会出现
            // [ZH] “界面是英文、后端回的话是中文”这种半截状态。
            await SyncBackendLanguageAsync();
        };
        ReloadLangButton.Click += (_, _) =>
        {
            // [EN] Translators do not need to restart after editing the JSON files on disk: Reload
            // [EN] clears the cache and re-reads for the current language, and the resulting
            // [EN] LanguageChanged event makes the settings window and the main window rebuild all
            // [EN] their text.
            // [ZH] 翻译者改完磁盘上的 JSON 后不必重启：Reload 会清缓存并按当前语言重读，
            // [ZH] 随之发出的 LanguageChanged 会让设置窗口与主窗口重建全部文案。
            Loc.Reload();
            UpdateLangPackText();
            _host?.ShowStatus(Loc.T("settings.general.langReloaded", "✅ 语言包已重新加载"));
        };
        OpenLangDirButton.Click += (_, _) =>
        {
            if (!ShellOpen.TryOpenDirectory(UserPaths.LangDirectory))
                _host?.ShowStatus(Loc.T("settings.general.openLangDirFailed",
                    "⚠️ 无法打开语言包目录：{0}", UserPaths.LangDirectory));
        };
        FontSizeSlider.PropertyChanged += (_, e) =>
        {
            if (e.Property != Slider.ValueProperty)
                return;
            FontSizeValueText.Text = $"{FontSizeSlider.Value:0} pt";
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

        // [EN] Media switches (v0.2.6 Part 2). Automatic reading is meaningful only
        // [EN] while speech output is enabled, so the dependent control follows.
        // [ZH] 媒体开关（v0.2.6 Part 2）。自动朗读仅在语音输出启用时才有意义，因此
        // [ZH] 从属控件随之变化。
        TtsEnabledCheck.IsCheckedChanged += (_, _) =>
        {
            if (_applying || _host is null)
                return;
            bool enabled = TtsEnabledCheck.IsChecked == true;
            TtsAutoReadCheck.IsEnabled = enabled;
            TtsVoiceCombo.IsEnabled = enabled;
            TtsRateCombo.IsEnabled = enabled;
            _host.MarkDirty();
        };
        TtsAutoReadCheck.IsCheckedChanged += (_, _) =>
        {
            if (_applying || _host is null)
                return;
            _host.MarkDirty();
        };
        PttEnabledCheck.IsCheckedChanged += (_, _) =>
        {
            if (_applying || _host is null)
                return;
            _host.MarkDirty();
        };
        TtsVoiceCombo.SelectionChanged += (_, _) =>
        {
            if (_applying || _host is null)
                return;
            _host.MarkDirty();
        };
        TtsRateCombo.SelectionChanged += (_, _) =>
        {
            if (_applying || _host is null)
                return;
            _host.MarkDirty();
        };
    }

    /// <summary>
    /// [EN] Speech-rate items. The stored value is the SAPI step (`-10..10`); only a
    /// [EN] practical subset is offered, because a single step barely changes the rate.
    /// [ZH] 语速选项。存储值为 SAPI 档位（`-10..10`）；只提供实用子集，因为单档
    /// [ZH] 之间的差异几乎听不出来。
    /// </summary>
    private void BuildRateOptions()
    {
        var rates = new List<string>();
        for (int step = -10; step <= 10; step += 2)
            rates.Add(step.ToString());
        TtsRateCombo.ItemsSource = rates.ConvertAll(value => new Option(value,
            () => Loc.T("settings.media.rateValue", "语速 {0}", value)));
    }

    /// <summary>
    /// [EN] Voice items from a `media_state` snapshot. The first item is always "engine
    /// default" so that a machine with no voices still has a valid selection.
    /// [ZH] 由 `media_state` 快照构造音色选项。首项始终是"引擎默认"，因此没有音色的
    /// [ZH] 机器仍有合法选择。
    /// </summary>
    private void BuildVoiceOptions(MediaState? state)
    {
        string? current = (TtsVoiceCombo.SelectedItem as Option)?.Value;
        var options = new List<Option>
        {
            new("", () => Loc.T("settings.media.voiceDefault", "默认音色（由系统选择）")),
        };
        if (state is not null)
        {
            foreach (string voice in state.Voices)
                options.Add(new(voice, () => voice));
        }
        TtsVoiceCombo.ItemsSource = options;
        Select(TtsVoiceCombo, current ?? state?.TtsVoice ?? "");
    }

    /// <summary>
    /// [EN] The "effective now: light / dark / follow system" line to the right of the drop-down.
    /// [ZH] 下拉框右侧那行“当前生效：浅色 / 深色 / 跟随系统”。
    /// </summary>
    private void UpdateThemeStatus()
    {
        string themeValue = (ThemeCombo.SelectedItem as Option)?.Value ?? "auto";
        // [EN] Do not apply "effective now: {0}" to the "currently following the system: {0} (...)"
        // [EN] entry in the language pack -- that would produce an unreadable sentence such as
        // [EN] "effective now: currently following the system: 0" (encountered in testing this
        // [EN] round).
        // [ZH] 注意别把“当前生效：{0}” 套在语言包里那条“当前跟随系统：{0}（…）”上 ——
        // [ZH] 那会拼出“当前生效：当前跟随系统：0”这种读不通的句子（本轮实测踩到）。
        string themeName = themeValue switch
        {
            "light" => Loc.T("settings.general.themeLight", "浅色"),
            "dark" => Loc.T("settings.general.themeDark", "深色"),
            _ => Loc.T("settings.general.themeSystem", "跟随系统"),
        };
        ThemeStatusText.Text = Loc.T("settings.general.themeEffective", "当前生效：{0}", themeName);
    }

    private static string? Selected(ComboBox combo) =>
        (combo.SelectedItem as Option)?.Value;

    // -----------------------------------------------------------------
    // [EN] Language-pack information and backend language synchronization (completed in v0.2.2
    // [EN] batch 2, aligned with WPF's UpdateLangPackText / SyncBackendLanguageAsync).
    // [ZH] 语言包信息与后端语言同步（v0.2.2 第 2 批补全，对齐 WPF 的 UpdateLangPackText /
    // [ZH] SyncBackendLanguageAsync）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Re-layout the three information lines of the "language pack" block: current language /
    /// [EN] available languages / language pack directory.
    /// [EN] Line by line, the same criterion as the WPF version (the same set of keys), so the two
    /// [EN] English UIs never mix Chinese and English.
    /// [ZH] 重排「语言包」区块的三行信息：当前语言 / 可用语言 / 语言包目录。
    /// [ZH] 与 WPF 版逐行同口径（同一批键），两边的英文界面不会一处中文一处英文。
    /// </summary>
    private void UpdateLangPackText()
    {
        var available = Loc.AvailableLocales();
        // [EN] Punctuation follows the language as well (v0.2.2 English UI scan): this code used
        // [EN] to concatenate hard-coded full-width ":" and "()" characters, so the English UI
        // [EN] showed `Current language：English（en）` -- a typical half-Chinese, half-English
        // [EN] leftover.
        // [ZH] 标点也随语言走（v0.2.2 UI 英文扫描）：原先这里用硬编码全角“：（）”拼接，
        // [ZH] 英文界面会显示 `Current language：English（en）` —— 半中半英的典型残留。
        LangPackText.Text = string.Join(Environment.NewLine, new[]
        {
            Loc.T("settings.general.langCurrentLine", "{0}: {1} ({2})",
                Loc.T("settings.general.langCurrent", "当前语言"), Loc.DisplayName(Loc.Locale), Loc.Locale),
            Loc.T("settings.general.langAvailableLine", "{0}: {1}",
                Loc.T("settings.general.langAvailable", "可用语言"),
                available.Count > 0
                    ? string.Join(" / ", available.ConvertAll(Loc.DisplayName))
                    : Loc.T("settings.general.langNone", "（未找到语言包）")),
            Loc.T("settings.general.langDirLine", "{0}: {1}",
                Loc.T("settings.general.langDir", "语言包目录"), UserPaths.LangDirectory),
        });
    }

    /// <summary>
    /// [EN] Tell the backend to switch language.
    ///
    /// [EN] **The backend owns business text**: system messages, error prompts, and the
    /// [EN] multilingual resources bundled with skills all live there, so switching only the front
    /// [EN] end's language pack would show the user "English UI, Chinese replies". The response
    /// [EN] carries `commands`, the command table for the new language -- pass it on to
    /// [EN] <see cref="CommandCatalog"/>, otherwise the command panel descriptions stay in the old
    /// [EN] language (the WPF version does the same).
    /// [ZH] 通知后端切换语言。
    ///
    /// [ZH] **后端是业务文案的主人**：系统消息、错误提示与技能自带的多语言资源都在它那边，
    /// [ZH] 前端只切自己的语言包会让用户看到“界面英文、回话中文”。响应里带回的
    /// [ZH] `commands` 是新语言的指令表 —— 顺手交给 <see cref="CommandCatalog"/>，
    /// [ZH] 否则指令面板的说明会停在旧语言（WPF 版同样处理）。
    /// </summary>
    private async Task SyncBackendLanguageAsync()
    {
        if (_host is null)
            return;
        try
        {
            using System.Text.Json.JsonDocument? doc =
                await _host.State.ActiveBackend.SendAsync(
                    new { action = "set_language", locale = Loc.Locale });
            if (doc is null)
            {
                _host.ShowStatus(Loc.T("settings.general.langSyncFailed",
                    "⚠️ 未能同步语言到后端（后端未响应）"));
                return;
            }
            if (doc.RootElement.TryGetProperty("commands", out System.Text.Json.JsonElement commands))
                CommandCatalog.ApplyFromBackend(commands);
            _host.ShowStatus(Loc.T("settings.general.languageRestartBackend", "后端语言已同步"));
        }
        catch (Exception ex)
        {
            _host.ShowStatus($"⚠️ {ex.Message}");
        }
    }

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
            // [EN] The default mode comes from the backend (see RefreshAsync); place a placeholder
            // [EN] in the first item for now.
            // [ZH] 默认模式来自后端（见 RefreshAsync），这里先占位在第一项。
            if (DefaultModeCombo.SelectedIndex < 0)
                DefaultModeCombo.SelectedIndex = 0;
            FontSizeSlider.Value = settings.FontSize;
            FontSizeValueText.Text = $"{settings.FontSize:0} pt";
            ConfirmDestructiveCheck.IsChecked = settings.ConfirmDestructiveCommands;
            // [EN] Media controls start from safe defaults; the backend snapshot fills
            // [EN] them in RefreshAsync.
            // [ZH] 媒体控件从安全默认值开始；后端快照在 RefreshAsync 中回填。
            TtsEnabledCheck.IsChecked = true;
            TtsAutoReadCheck.IsChecked = false;
            PttEnabledCheck.IsChecked = true;
            Select(TtsRateCombo, "0");
            BuildVoiceOptions(null);
        }
        finally
        {
            _applying = false;
        }
        // [EN] Synchronize "effective now: ..." only after loading: at the first ApplyTexts frame
        // [EN] the drop-down item is not selected yet, so the computed value would always be
        // [EN] "follow system" (which does not match the current value shown by WPF).
        // [ZH] 载入之后才同步“当前生效：…”：首帧 ApplyTexts 时下拉项还没选中，
        // [ZH] 那时算出来永远是“跟随系统”（与 WPF 显示的当前值对不上）。
        UpdateThemeStatus();
    }

    public void ApplyTo(AppSettings settings)
    {
        // [EN] Theme / language / font size / emoji are saved immediately when changed (see
        // [EN] HookEvents); this only collects the item that was changed but not saved
        // [EN] immediately.
        // [ZH] 主题 / 语言 / 字号 / emoji 已经在改动那一刻即时落盘（见 HookEvents），
        // [ZH] 这里只收“改了但没即时落盘”的那一项。
        settings.ConfirmDestructiveCommands = ConfirmDestructiveCheck.IsChecked == true;
    }

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        await RefreshMediaAsync();
        // [EN] Default mode: the backend sets it from config.json at startup. The front end reads
        // [EN] it once to fill the control, so the display does not contradict reality.
        // [ZH] 默认模式：后端启动时按 config.json 设定。前端读一次回填，避免显示与实际不符。
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
            // [EN] A backend that is down must not keep the settings window from opening: the
            // [EN] default mode is only a fill-in, so keep the default display on failure.
            // [ZH] 后端没起来时不该让设置窗口打不开：默认模式只是回填，失败就保持默认显示。
        }
    }

    /// <summary>
    /// [EN] Load the media state and fill the speech controls (v0.2.6 Part 2).
    ///
    /// The backend is the single authority for "is a voice engine available" and for
    /// the configured values, so the page never guesses. When the engine is missing
    /// the switches stay visible but disabled, with the reason shown below them.
    ///
    /// [ZH] 载入媒体状态并回填语音控件（v0.2.6 Part 2）。
    ///
    /// "是否存在语音引擎"与已配置取值以后端为唯一权威，设置页不做猜测。引擎缺失时
    /// 开关保持可见但禁用，并在下方说明原因。
    /// </summary>
    private async Task RefreshMediaAsync()
    {
        if (_host is null)
            return;
        try
        {
            var service = new MediaService();
            _mediaSnapshot = await service.LoadStateAsync(_host.State.Backend);
            _applying = true;
            try
            {
                TtsEnabledCheck.IsChecked = _mediaSnapshot.TtsEnabled;
                TtsAutoReadCheck.IsChecked = _mediaSnapshot.TtsAutoRead;
                PttEnabledCheck.IsChecked = _mediaSnapshot.PttEnabled;
                BuildVoiceOptions(_mediaSnapshot);
                Select(TtsRateCombo,
                    (Math.Clamp(_mediaSnapshot.TtsRate, -10, 10) % 2 == 0
                        ? Math.Clamp(_mediaSnapshot.TtsRate, -10, 10)
                        : 0).ToString());
                bool engine = _mediaSnapshot.TtsEngineAvailable && _mediaSnapshot.TtsEnabled;
                TtsEnabledCheck.IsEnabled = _mediaSnapshot.TtsEngineAvailable;
                TtsAutoReadCheck.IsEnabled = engine;
                TtsVoiceCombo.IsEnabled = engine;
                TtsRateCombo.IsEnabled = engine;
            }
            finally
            {
                _applying = false;
            }
        }
        catch (Exception)
        {
            // [EN] A backend that is down must not keep the settings window from opening.
            // [ZH] 后端未启动不应阻止设置窗口打开。
        }
    }

    public async Task ApplyToBackendAsync()
    {
        if (_host is null)
            return;
        // [EN] The default mode is **business configuration**: the backend owns it, and the front
        // [EN] end only sends the choice (same division of labor as WPF).
        // [EN] Use the /mode command instead of editing config.json directly -- the backend has
        // [EN] already implemented and verified this path, whereas the front end writing the
        // [EN] config file itself would bypass the backend's validation and migration logic.
        // [ZH] 默认模式是**业务设置**：后端持有它，前端只把选择发过去（与 WPF 版同一分工）。
        // [ZH] 走 /mode 指令而不是直接改 config.json —— 这条路径后端已经实现并验证过，
        // [ZH] 前端自己写配置文件会绕过后端的校验与迁移逻辑。
        string? mode = Selected(DefaultModeCombo);
        if (string.IsNullOrEmpty(mode))
            return;
        await _host.State.Backend.SendAsync(new { action = "chat", message = $"/mode {mode}" });
        await ApplyMediaConfigAsync();
    }

    /// <summary>
    /// [EN] Persist the speech switches through `media_config_set` (v0.2.6 Part 2).
    ///
    /// Values are sent to the backend rather than written into `config.json` from the
    /// front end: the backend validates them and is the only writer, exactly like the
    /// default-mode path above.
    ///
    /// [ZH] 通过 `media_config_set` 持久化语音开关（v0.2.6 Part 2）。
    ///
    /// 取值发送给后端，而不是由前端写入 `config.json`：后端负责校验且是唯一写入方，
    /// 与上面的默认模式路径一致。
    /// </summary>
    private async Task ApplyMediaConfigAsync()
    {
        if (_host is null)
            return;
        int rate = int.TryParse(Selected(TtsRateCombo), out int parsed) ? parsed : 0;
        try
        {
            using System.Text.Json.JsonDocument? doc = await _host.State.Backend.SendAsync(new
            {
                action = "media_config_set",
                tts_enabled = TtsEnabledCheck.IsChecked == true,
                tts_auto_read = TtsAutoReadCheck.IsChecked == true,
                tts_voice = Selected(TtsVoiceCombo) ?? "",
                tts_rate = rate,
                ptt_enabled = PttEnabledCheck.IsChecked == true,
            }, idleTimeout: TimeSpan.FromSeconds(30));
            if (doc is null)
            {
                _host.ShowStatus(Loc.T("settings.media.saveFailed", "⚠️ 未能保存语音设置（后端未响应）"));
                return;
            }
            if (doc.RootElement.TryGetProperty("ok", out System.Text.Json.JsonElement ok)
                && ok.ValueKind == System.Text.Json.JsonValueKind.False)
            {
                string message = doc.RootElement.TryGetProperty("content",
                    out System.Text.Json.JsonElement content)
                    ? content.GetString() ?? "" : "";
                _host.ShowStatus(string.IsNullOrWhiteSpace(message)
                    ? Loc.T("settings.media.saveFailed", "⚠️ 未能保存语音设置（后端未响应）")
                    : message);
                return;
            }
            _host.ShowStatus(Loc.T("settings.media.saved", "✅ 语音设置已保存"));
        }
        catch (Exception ex)
        {
            _host.ShowStatus($"⚠️ {ex.Message}");
        }
    }

    public void ApplyTexts()
    {        AppearanceTitle.Text = Loc.T("settings.general.sectionAppearance", "外观与默认行为");
        ThemeLabel.Text = Loc.T("settings.general.themeLabel", "界面主题");
        // [EN] WPF puts "effective now: light" to the right of the drop-down and the note "theme
        // [EN] and emoji display take effect immediately" on its own line below.
        // [EN] The two sentences used to be merged into one control, with a different position and
        // [EN] text from WPF -- here they are split back into two controls.
        // [ZH] WPF 把“当前生效：浅色”放在下拉框右侧，把“主题与 emoji 显示即时生效”单独一行放在下面。
        // [ZH] 两句话原来被合并进了同一个控件，位置和文案都与 WPF 不同 —— 这里拆回两个控件。
        UpdateThemeStatus();
        ThemeNote.Text = Loc.T("settings.general.themeNote", "主题与 emoji 显示即时生效，无需重启");
        EmojiLabel.Text = Loc.T("settings.general.emojiLabel", "emoji 显示");
        EmojiStatusText.Text = Emoji.Available
            ? Loc.T("settings.general.emojiOn", "本机支持 emoji，当前正常显示")
            : Loc.T("settings.general.emojiUnavailable", "本机缺少 emoji 字体，已改用纯文本标记");
        FontSizeLabel.Text = Loc.T("settings.general.fontSize", "字体大小");
        LanguageTitle.Text = Loc.T("settings.general.sectionLanguage", "语言 / Language");
        LanguageLabel.Text = Loc.T("settings.general.language", "界面语言");
        LanguageHelpText.Text = Loc.T("settings.general.languageHelp",
            "切换后界面、错误提示与指令帮助立即生效，无需重启。选择结果记录在 ui_state.json。");
        // [EN] The default mode key is one complete description in the language pack ("default
        // [EN] chat mode (takes effect next launch...)"), used as both label and hint -- no
        // [EN] shorter separate key is created, so the two texts cannot diverge later.
        // [ZH] 默认模式的键在语言包里是一条完整说明（“默认对话模式（下次启动生效…）”），
        // [ZH] 既当标题也当提示 —— 不另造一个更短的键，避免两处文案将来各说各话。
        DefaultModeLabel.Text = Loc.T("settings.general.defaultMode",
            "默认对话模式（下次启动生效，也可用 /mode 即时切换）");
        DefaultModeHint.Text = DefaultModeLabel.Text;
        ConfirmDestructiveCheck.Content = Loc.T("settings.general.confirmDestructive",
            "执行有副作用的指令（/clear、/clearcache）前二次确认");
        MediaTitle.Text = Loc.T("settings.media.sectionTitle", "语音与多模态");
        TtsEnabledCheck.Content = Loc.T("settings.media.ttsEnabled", "启用离线语音朗读（TTS）");
        TtsAutoReadCheck.Content = Loc.T("settings.media.ttsAutoRead", "自动朗读助手回答");
        TtsVoiceLabel.Text = Loc.T("settings.media.voice", "音色");
        TtsRateLabel.Text = Loc.T("settings.media.rate", "语速");
        PttEnabledCheck.Content = Loc.T("settings.media.pttEnabled",
            "启用按键说话（长按右 Ctrl，松开自动转写）");
        MediaHint.Text = Loc.T("settings.media.hint",
            "语音与图片功能全部在本机离线运行；缺少本地模型或引擎时会明确提示，绝不调用云端服务。");
        LangPackTitle.Text = Loc.T("settings.general.langPackTitle", "语言包");
        ReloadLangButton.Content = Loc.T("settings.general.reloadLang", "↻ 重新加载语言包");
        OpenLangDirButton.Content = Loc.T("settings.general.openLangDir", "📂 打开语言包目录");
        UpdateLangPackText();
        PathsTitle.Text = Loc.T("settings.general.sectionPaths", "📌 界面与数据位置");
        PathsText.Text = DescribePaths();

        // [EN] Drop-down text is provided by a delegate, so changing the language must refresh the
        // [EN] display of the currently selected item.
        // [ZH] 下拉项文案是委托，改了语言要把当前选中项显示刷新一遍。
        RefreshComboTexts(ThemeCombo, EmojiCombo, LanguageCombo, DefaultModeCombo,
            TtsVoiceCombo, TtsRateCombo);
    }

    /// <summary>
    /// [EN] UI/data location description (aligned with WPF's DescribePaths, line by line).
    ///
    /// [EN] Why list the "backend directory / plugin directory / language pack directory / user
    /// [EN] data directory": when users report questions like "where are the files I uploaded" or
    /// [EN] "where did the plugins go", this is the first-hand answer; if the portable layout
    /// [EN] (program body under app\, user data outside) is not spelled out, users will look
    /// [EN] inside app\ for a results directory that will never appear there.
    /// [ZH] 界面/数据位置说明（对齐 WPF 的 DescribePaths，逐行同口径）。
    ///
    /// [ZH] 为什么要把“后端目录 / 插件目录 / 语言包目录 / 用户数据目录”都列出来：
    /// [ZH] 用户报“我上传的文件在哪”“插件装哪去了”这类问题时，这是第一手答案；
    /// [ZH] 便携版布局（程序本体在 app\、用户数据在外层）如果不写明，用户会去 app\ 里
    /// [ZH] 翻一个永远不会出现的 results 目录。
    /// </summary>
    private static string DescribePaths()
    {
        string program = UserPaths.ProgramDirectory;
        string data = UserPaths.DataRoot;
        var lines = new List<string>
        {
            $"{Loc.T("settings.general.pathProgram", "程序目录")}：{program}",
            $"{Loc.T("settings.general.pathBackend", "后端目录（配置 / models / 会话 / 自动化日志）")}：" +
            $"{System.IO.Path.Combine(program, "backend")}",
            $"{Loc.T("settings.general.pathSkills", "插件目录")}：" +
            $"{System.IO.Path.Combine(program, "backend", "skills")}",
            $"{Loc.T("settings.general.pathUserData", "用户数据目录（界面状态 / 常用语 / results）")}：{data}",
            $"{Loc.T("settings.general.langDir", "语言包目录")}：{UserPaths.LangDirectory}",
        };
        if (!string.Equals(program, data, StringComparison.OrdinalIgnoreCase))
        {
            lines.Add(Loc.T("settings.general.pathPortableNote",
                "提示：本程序按便携版布局运行，程序本体在 app\\ 下，ui_state.json、常用语与「results」都在外层目录，便于备份与迁移。"));
        }
        return string.Join(Environment.NewLine, lines);
    }

    /// <summary>
    /// [EN] Resetting ItemsSource loses the selection, so this method "reselects by value after
    /// [EN] rebuilding".
    ///
    /// [EN] **The _applying reentrancy gate must be opened during rebuilding**: this refresh only
    /// [EN] swaps display text and the selected value does not change, but `Select()` sets
    /// [EN] SelectedIndex from -1 back to the original value, which the control sees as a **real
    /// [EN] selection change**. As a result all three SelectionChanged handlers above fire. The
    /// [EN] language one is the deadliest: SetLanguage -> Loc.SetLocale -> LanguageChanged ->
    /// [EN] settings window ApplyTexts -> all page ApplyTexts -> back here -- a **synchronous
    /// [EN] loop**. Measured: opening the settings window overflowed the stack after 340
    /// [EN] iterations (6868 stack lines, the process died outright), so the settings window could
    /// [EN] never open. LoadFrom (:163) and RefreshAsync (:202) already used this gate; only this
    /// [EN] "refresh text" place had missed it.
    /// [ZH] 重设 ItemsSource 会丢掉选中项，因此这里“重建后按值选回”。
    ///
    /// [ZH] **重建期间必须打开 _applying 重入闸**：这次刷新只换显示文案、选中值没变，
    /// [ZH] 但 `Select()` 会把 SelectedIndex 从 -1 设回原值 —— 在控件看来那是一次**真实的选中变化**，
    /// [ZH] 于是上面三个 SelectionChanged 处理器全部被触发。语言那一个最致命：
    /// [ZH] SetLanguage → Loc.SetLocale → LanguageChanged → 设置窗口 ApplyTexts →
    /// [ZH] 所有页面 ApplyTexts → 又回到这里 —— 一条**同步闭环**。
    /// [ZH] 实测：打开设置窗口时 340 圈后栈溢出（6868 行栈，进程直接死），设置窗口永远打不开。
    /// [ZH] LoadFrom（:163）与 RefreshAsync（:202）早就用了这个闸，只有“刷新文案”这一处漏了。
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

    // -----------------------------------------------------------------
    // [EN] Probe interface (used by `EchoDesk --probe pages`: unattended verification of the
    // [EN] General page's language-pack block and path description).
    // [ZH] 探针接口（`EchoDesk --probe pages` 用：无人值守验常规页的语言包区块与路径说明）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Observable state snapshot (key=value, separated by semicolons). Assertions live in
    /// [EN] Probe.cs.
    /// [ZH] 可观测状态快照（键=值，分号分隔）。断言写在 Probe.cs 里。
    /// </summary>
    public string ProbeSnapshot()
    {
        int available = Loc.AvailableLocales().Count;
        return string.Join("; ", new[]
        {
            $"langCurrent={(LangPackText.Text?.Contains(Loc.Locale) == true ? 1 : 0)}",
            $"langAvailableCount={available}",
            $"reloadLangButton={(string.IsNullOrEmpty(ReloadLangButton.Content as string) ? 0 : 1)}",
            $"openLangDirButton={(string.IsNullOrEmpty(OpenLangDirButton.Content as string) ? 0 : 1)}",
            $"pathsHasBackend={(PathsText.Text?.Contains("backend") == true ? 1 : 0)}",
            $"pathsHasLangDir={(PathsText.Text?.Contains(UserPaths.LangDirectory) == true ? 1 : 0)}",
            $"languageOptions={LanguageCombo.ItemCount}",
        });
    }

    /// <summary>
    /// [EN] Probe: simulate the user switching language (verifies the path that syncs
    /// [EN] `set_language` to the backend).
    /// [ZH] 探针：模拟用户切换语言（验 `set_language` 同步到后端那条路径）。
    /// </summary>
    public async Task ProbeSyncLanguageAsync()
    {
        Loc.Reload();
        UpdateLangPackText();
        await SyncBackendLanguageAsync();
    }
}
