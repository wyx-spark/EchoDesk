// 模型页的交互逻辑（v0.2.1 跨平台-2 任务一，第 8 个迁移模块）。
//
// 后端动作：
//   model_profiles         → { current, items: [ { alias, name, repo, installed, current,
//                             size_human, context_length, temperature, params, quantization } ] }
//   model_profile_apply    { alias }      切换档位（热加载）
//   capability             → { settings: { temperature, top_p, repeat_penalty, max_tokens,
//                             preset, context_mode, clarify_vague, summary_cache } }
//   capability_settings    { preset, temperature, top_p, repeat_penalty, max_tokens,
//                             context_mode, clarify_vague, summary_cache }
//
// 两处刻意照搬 WPF 版的设计（都不是随手写的）：
//
// 1. **胶囊用 TextBlock 承载文字**：某些框架会把按钮的字符串内容当访问键渲染，
//    下划线被吃掉（"Q4_K_M" 显示成 "Q4KM"）。量化名里下划线是常态，用 TextBlock 包一层
//    就一定原样显示。Avalonia 这边同样处理，behavior 一致、不依赖框架差异。
//
// 2. **滑块边界会被"撑开"**：config.json 是人手写的，max_tokens 常见 2048/4096/8192，
//    而界面范围是 64–4096。若把 8192 硬裁成 4096，用户只是打开设置页、什么都没改就点保存，
//    配置里的 8192 就被悄悄改成 4096 —— 典型的"读一下就写坏"。撑开边界让滑块如实表示现状。
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class ModelPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;
    private bool _syncing;

    /// <summary>默认值：读不到后端设置时用它（与 WPF 版同一组默认）。</summary>
    private const double DefaultTemperature = 0.7;
    private const double DefaultTopP = 0.9;
    private const double DefaultRepeatPenalty = 1.1;
    private const int DefaultMaxTokens = 2048;

    private sealed record Option(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    public ModelPage()
    {
        InitializeComponent();
        BuildOptions();

        ProfileRefreshButton.Click += async (_, _) => await LoadProfilesAsync();
        InferenceSaveButton.Click += async (_, _) => await SaveInferenceAsync();
        InferenceReloadButton.Click += async (_, _) => await LoadInferenceAsync();
        InferenceResetButton.Click += async (_, _) =>
        {
            // 恢复默认：只是把滑块拨回默认值并提示"还要点保存"，
            // 不直接写后端 —— 让用户能先看到结果再决定。
            _syncing = true;
            SetSlider(TempSlider, TempValueText, DefaultTemperature);
            SetSlider(TopPSlider, TopPValueText, DefaultTopP);
            SetSlider(RepeatSlider, RepeatValueText, DefaultRepeatPenalty);
            SetSlider(MaxTokensSlider, MaxTokensValueText, DefaultMaxTokens);
            _syncing = false;
            InferenceStatusText.Text = Loc.T("settings.inference.resetApplied",
                "↩️ 已恢复默认值（点「保存」后写入 config.json）");
        };

        foreach (Slider slider in new[] { TempSlider, TopPSlider, RepeatSlider, MaxTokensSlider })
            slider.PropertyChanged += (_, e) =>
            {
                if (e.Property == Slider.ValueProperty)
                    RefreshSliderLabels();
            };
    }

    public string TabKey => "settings.tab.model";
    public string TabFallback => "模型";
    public string AutomationId => "SettingsTabModel";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }

    /// <summary>
    /// 骨架的保存按钮 → 保存推理参数。
    /// 档位切换是**即时生效**的（胶囊点一下就热加载），不需要"保存"这一步；
    /// 这一页真正需要落盘的只有推理参数。
    /// </summary>
    public async Task ApplyToBackendAsync() => await SaveInferenceAsync();

    private void BuildOptions()
    {
        PresetCombo.ItemsSource = new List<Option>
        {
            new("light", () => Loc.T("settings.inference.presetLight", "轻快")),
            new("balanced", () => Loc.T("settings.inference.presetBalanced", "均衡")),
            new("quality", () => Loc.T("settings.inference.presetQuality", "质量优先")),
        };
        ContextModeCombo.ItemsSource = new List<Option>
        {
            new("summarize", () => Loc.T("settings.inference.contextSummarize", "分段摘要")),
            new("slide", () => Loc.T("settings.inference.contextSlide", "滑动窗口")),
        };
    }

    private void RefreshSliderLabels()
    {
        TempValueText.Text = TempSlider.Value.ToString("0.00");
        TopPValueText.Text = TopPSlider.Value.ToString("0.00");
        RepeatValueText.Text = RepeatSlider.Value.ToString("0.00");
        MaxTokensValueText.Text = ((int)Math.Round(MaxTokensSlider.Value)).ToString();
    }

    /// <summary>填滑块值，必要时撑开边界（见文件头第 2 条）。</summary>
    private void SetSlider(Slider slider, TextBlock label, double value)
    {
        if (double.IsNaN(value) || double.IsInfinity(value))
            return;
        if (value > slider.Maximum)
            slider.Maximum = value;
        else if (value < slider.Minimum)
            slider.Minimum = value;
        slider.Value = value;
        label.Text = slider == MaxTokensSlider
            ? ((int)Math.Round(value)).ToString()
            : value.ToString("0.00");
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
    private async Task<JsonDocument?> SendAsync(object request, TimeSpan? idle = null)
    {
        if (_host is null)
            return null;
        return await _host.State.Backend.SendAsync(request, idleTimeout: idle ?? TimeSpan.FromSeconds(60));
    }

    public async Task RefreshAsync()
    {
        await LoadProfilesAsync();
        await LoadInferenceAsync();
    }

    // -----------------------------------------------------------------
    // ① 档位
    // -----------------------------------------------------------------
    private async Task LoadProfilesAsync()
    {
        ProfileStatusText.Text = Loc.T("settings.model.profileHint", "正在读取模型档位…");
        using JsonDocument? doc = await SendAsync(new { action = "model_profiles" });
        if (doc is null)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileLoadFailed",
                "❌ 读取模型档位失败（后端未响应）");
            return;
        }
        JsonElement data = doc.RootElement;
        ModelProfileChips.Children.Clear();

        int count = 0;
        if (data.TryGetProperty("items", out JsonElement items) && items.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement profile in items.EnumerateArray())
            {
                ModelProfileChips.Children.Add(BuildChip(profile));
                count++;
            }
        }
        ProfileStatusText.Text = count == 0
            ? Loc.T("settings.model.profileEmpty", "config.json 里没有登记模型档位（models 为空）。")
            : "";
    }

    private Control BuildChip(JsonElement profile)
    {
        string alias = GetString(profile, "alias");
        string name = GetString(profile, "name");
        string repo = GetString(profile, "repo");
        bool installed = GetBool(profile, "installed");
        bool current = GetBool(profile, "current");
        int contextLength = GetInt(profile, "context_length");
        string sizeText = GetString(profile, "size_human");

        var lines = new List<string>();
        if (!installed)
            lines.Add(Loc.T("settings.model.profileNotDownloaded", "未下载"));
        if (contextLength > 0)
            lines.Add(Loc.T("settings.model.profileContext", "{0} 上下文", contextLength));

        string caption = name.Length > 0 ? name : alias;
        var content = new StackPanel { Spacing = 1 };
        // 用 TextBlock 而不是直接给按钮字符串内容：避免框架把下划线当访问键吃掉（见文件头第 1 条）。
        content.Children.Add(new TextBlock { Text = caption, FontSize = 11 });
        if (lines.Count > 0)
        {
            content.Children.Add(new TextBlock
            {
                Text = string.Join(" · ", lines), FontSize = 10, Opacity = 0.65,
            });
        }

        string tip = current
            ? Loc.T("settings.model.profileTipCurrent", "当前正在使用这一档")
            : installed
                ? Loc.T("settings.model.profileTipSwitch", "点一下切换到这一档（热加载，无需重启）")
                : Loc.T("settings.model.profileTipDownload", "点一下从仓库下载这一档");

        var chip = new Button
        {
            Content = content,
            Padding = new Thickness(12, 4),
            IsEnabled = !current,
        };
        // Avalonia 的控件**没有** `ToolTip` 属性（WPF 的 Control 有），提示是**附加属性**：
        // 在对象初始化器里写 `ToolTip = …` 会编译报 CS0117，必须用 `ToolTip.SetTip()`。
        // 这是本轮第 5 处"WPF 有、Avalonia 没有"的差异（见记录里的坑列表）。
        ToolTip.SetTip(chip, tip);
        chip.Click += async (_, _) =>
        {
            if (installed)
                await ApplyProfileAsync(alias, caption);
            else
                await DownloadProfileAsync(alias, repo, sizeText, caption);
        };
        return chip;
    }

    private async Task ApplyProfileAsync(string alias, string caption)
    {
        ProfileStatusText.Text = Loc.T("settings.model.profileApplying", "⏳ 正在切换到 {0} …", caption);
        using JsonDocument? doc = await SendAsync(
            new { action = "model_profile_apply", alias }, TimeSpan.FromSeconds(180));
        if (doc is null)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileApplyFailed", "❌ 切换失败（后端未响应）");
            return;
        }
        string content = GetString(doc.RootElement, "content");
        ProfileStatusText.Text = content.Length > 0
            ? content
            : Loc.T("settings.model.profileApplied", "✅ 已切换到 {0}", caption);
        await LoadProfilesAsync();
    }

    private async Task DownloadProfileAsync(string alias, string repo, string sizeText, string caption)
    {
        if (repo.Length == 0)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileNoRepo",
                "❌ 该档位没有登记仓库（config.json 的 models.{0}.repo），请到模型市场里手动下载。", alias);
            return;
        }
        ProfileStatusText.Text = Loc.T("settings.model.profileResolving",
            "🔍 正在仓库 {0} 里查找 {1} …", repo, caption);
        using JsonDocument? doc = await SendAsync(
            new { action = "model_hub_detail", repo, source = "auto" }, TimeSpan.FromSeconds(120));
        if (doc is null)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileFileMissing",
                "⚠️ 仓库 {0} 里没有找到 {1}。请到「模型市场」里挑一个量化版本下载，或改 config.json 里的档位声明。",
                repo, caption);
            return;
        }
        // 详情里取第一个文件发下载请求（与 WPF 版同口径：档位胶囊只负责"下载这一档"）。
        string fileName = "";
        if (doc.RootElement.TryGetProperty("files", out JsonElement files)
            && files.ValueKind == JsonValueKind.Array && files.GetArrayLength() > 0)
        {
            fileName = GetString(files[0], "name");
        }
        if (fileName.Length == 0)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileFileMissing",
                "⚠️ 仓库 {0} 里没有找到 {1}。请到「模型市场」里挑一个量化版本下载，或改 config.json 里的档位声明。",
                repo, caption);
            return;
        }
        await SendAsync(new { action = "model_hub_download", repo, filename = fileName, source = "auto" });
        ProfileStatusText.Text = Loc.T("settings.model.profileDownloadStarted",
            "⬇ 已开始下载 {0}（可在「模型市场」观察进度）", fileName);
    }

    // -----------------------------------------------------------------
    // ② 推理参数
    // -----------------------------------------------------------------
    private async Task LoadInferenceAsync()
    {
        InferenceStatusText.Text = Loc.T("settings.inference.readingNow", "正在读取推理参数…");
        using JsonDocument? doc = await SendAsync(new { action = "capability" });
        if (doc is null || !doc.RootElement.TryGetProperty("settings", out JsonElement settings))
        {
            InferenceStatusText.Text = Loc.T("settings.inference.readUnavailable",
                "⚠️ 未能读取推理参数（后端未响应），可点「重新读取」重试。");
            return;
        }

        _syncing = true;
        try
        {
            SetSlider(TempSlider, TempValueText, GetDouble(settings, "temperature", DefaultTemperature));
            SetSlider(TopPSlider, TopPValueText, GetDouble(settings, "top_p", DefaultTopP));
            SetSlider(RepeatSlider, RepeatValueText, GetDouble(settings, "repeat_penalty", DefaultRepeatPenalty));
            SetSlider(MaxTokensSlider, MaxTokensValueText, GetInt(settings, "max_tokens", DefaultMaxTokens));
            Select(PresetCombo, GetString(settings, "preset"));
            Select(ContextModeCombo, GetString(settings, "context_mode"));
            ClarifyVagueCheck.IsChecked = GetBoolOr(settings, "clarify_vague", true);
            SummaryCacheCheck.IsChecked = GetBoolOr(settings, "summary_cache", true);
        }
        finally
        {
            _syncing = false;
        }
        RefreshSliderLabels();
        InferenceStatusText.Text = Loc.T("settings.inference.loaded",
            "✅ 已读取当前参数（点「保存」后写入 config.json）");
    }

    private string CurrentPreset() => (PresetCombo.SelectedItem as Option)?.Value ?? "balanced";
    private string CurrentContextMode() => (ContextModeCombo.SelectedItem as Option)?.Value ?? "summarize";

    /// <summary>
    /// 保存。**全量发送**面板上的每一个值（而不是只发改动过的）：
    /// 后端按"收到什么写什么"处理，全量发送让界面成为唯一事实来源，
    /// 不会出现"某次改动没发出去、之后每次保存都把它漏掉"的隐性偏差。
    /// </summary>
    private async Task SaveInferenceAsync()
    {
        if (_host is null || _syncing)
            return;
        InferenceStatusText.Text = Loc.T("settings.inference.saving", "⏳ 正在保存…");
        using JsonDocument? doc = await SendAsync(new
        {
            action = "capability_settings",
            preset = CurrentPreset(),
            temperature = TempSlider.Value,
            top_p = TopPSlider.Value,
            repeat_penalty = RepeatSlider.Value,
            max_tokens = (int)Math.Round(MaxTokensSlider.Value),
            context_mode = CurrentContextMode(),
            clarify_vague = ClarifyVagueCheck.IsChecked == true,
            summary_cache = SummaryCacheCheck.IsChecked == true,
        });
        if (doc is null)
        {
            InferenceStatusText.Text = Loc.T("settings.inference.saveFailed", "❌ 保存失败（后端未响应）");
            return;
        }
        InferenceStatusText.Text = Loc.T("settings.inference.saved", "✅ 已保存");
        // 回读：后端会裁剪到合法范围，界面要跟上真实值（quiet 模式，不覆盖上面的提示）。
        await LoadInferenceAsync();
        InferenceStatusText.Text = Loc.T("settings.inference.saved", "✅ 已保存");
    }

    private static string GetString(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? ""
            : "";

    private static bool GetBool(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.True;

    private static bool GetBoolOr(JsonElement item, string name, bool fallback) =>
        item.TryGetProperty(name, out JsonElement el) ? el.ValueKind == JsonValueKind.True : fallback;

    private static int GetInt(JsonElement item, string name, int fallback) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number
            ? el.GetInt32()
            : fallback;

    private static int GetInt(JsonElement item, string name) => GetInt(item, name, 0);

    private static double GetDouble(JsonElement item, string name, double fallback) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number
            ? el.GetDouble()
            : fallback;

    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        ProfileTitle.Text = Loc.T("settings.model.profileTitle", "🎚️ 模型档位（可选默认模型）");
        ProfileHintText.Text = Loc.T("settings.model.profileHint",
            "点胶囊即可切换（热加载，无需重启）；未下载的档位点一下会先下载。");
        ProfileRefreshButton.Content = Loc.T("settings.model.actionRefreshProfiles", "刷新档位");

        InferenceTitle.Text = Loc.T("settings.inference.title", "推理参数");
        TempLabel.Text = Loc.T("settings.inference.temp", "温度");
        TopPLabel.Text = Loc.T("settings.inference.topP", "top_p");
        RepeatLabel.Text = Loc.T("settings.inference.repeat", "重复惩罚");
        MaxTokensLabel.Text = Loc.T("settings.inference.maxTokens", "最大输出 tokens");
        PresetLabel.Text = Loc.T("settings.inference.preset", "预设");
        ContextModeLabel.Text = Loc.T("settings.inference.contextMode", "上下文策略");
        ClarifyVagueCheck.Content = Loc.T("settings.inference.clarifyVague", "模糊问题先追问");
        SummaryCacheCheck.Content = Loc.T("settings.inference.summaryCache", "缓存分段摘要");
        InferenceSaveButton.Content = Loc.T("settings.inference.save", "保存");
        InferenceReloadButton.Content = Loc.T("settings.inference.reload", "🔄 重新读取");
        InferenceResetButton.Content = Loc.T("settings.inference.reset", "恢复默认");

        MarketNotMigratedText.Text = Loc.T("settings.model.marketNotMigrated",
            "⏳ 「模型市场」（搜索、量化筛选、分页、详情、下载进度）还没有迁移到跨平台前端。"
            + "替代做法：把 .gguf 文件放进 app/backend/models/ 后点「刷新档位」；"
            + "在 Windows 上也可以用 WPF 版设置页从市场下载。");

        // 下拉项文案是委托，重建后按值选回。
        string? preset = (PresetCombo.SelectedItem as Option)?.Value;
        string? mode = (ContextModeCombo.SelectedItem as Option)?.Value;
        BuildOptions();
        Select(PresetCombo, preset);
        Select(ContextModeCombo, mode);
        RefreshSliderLabels();
    }
}
