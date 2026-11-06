// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ModelPage.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ModelPage.axaml.cs。类型与成员说明见下方注释。
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
using Avalonia.Automation;
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
        ProfileNewButton.Click += (_, _) => OpenProfileEditor(null);
        ProfileSaveButton.Click += async (_, _) => await SaveProfileAsync();
        ProfileDeleteButton.Click += async (_, _) => await DeleteProfileAsync();
        ProfileCancelButton.Click += (_, _) =>
        {
            _profileDeletePending = false;
            _profileEditingAlias = "";
            ProfileEditorPanel.IsVisible = false;
        };
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

        // 本地模型库（v0.2.2 第 2 批）：重扫 / 刷新
        HubRescanButton.Click += async (_, _) => await RescanModelsAsync();
        HubRegistryRefreshButton.Click += async (_, _) => await LoadRegistryAsync();
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
        // ActiveBackend：运行时就是 App.State.Backend；探针模式下是探针自己那条连接
        // （见 AppState.ProbeBackendOverride 的说明）。
        return await _host.State.ActiveBackend.SendAsync(request, idleTimeout: idle ?? TimeSpan.FromSeconds(60));
    }

    public async Task RefreshAsync()
    {
        await LoadProfilesAsync();
        await LoadInferenceAsync();
        await LoadRegistryAsync();
        // 模型市场只刷新"已有的那一页"，**不自动搜索**（与 WPF 版一致：
        // 打开设置页不该凭空打一次远端请求，那是用户点「搜索」才做的事）
        await Market.RefreshAsync();
    }

    // =================================================================
    // ③ 本地模型库（v0.2.2 第 2 批补全，对齐 WPF 的 HubRegistry 那一块）
    // =================================================================
    //
    // 与「模型市场」的分工（WPF 侧一直是两块不同的东西，此处逐项照抄）：
    //   市场 = 远端"能下载什么"；模型库 = 本地磁盘"有什么"。
    //
    // 三个后端动作：
    //   model_hub_registry   → { registry: { count, total_size_human,
    //                            models: [ { name, size_human, quantization, repo,
    //                                        downloaded_at, last_used, current, exists } ] } }
    //   model_scan           → 重新扫描模型文件夹（手工放进 .gguf 后不必重启）
    //   model_load {name}    → 热加载该模型（成功后就是当前模型）
    //   model_hub_uninstall {name} → 删除模型文件

    /// <summary>本地模型库里的一行。</summary>
    private sealed record RegistryRow(string Name, string SizeText, string Meta,
                                      bool InUse, bool Exists);

    /// <summary>删除的二次确认状态：第一次点变「确认删除？」，再点一次才执行。</summary>
    private string _registryDeletePending = "";

    private async Task LoadRegistryAsync(bool keepPendingDelete = false)
    {
        Market.SetStatus(Loc.T("settings.model.registryLoading", "正在读取本地模型库…"));
        using JsonDocument? doc = await SendAsync(new { action = "model_hub_registry" });
        if (doc is null || !doc.RootElement.TryGetProperty("registry", out JsonElement registry))
        {
            Market.SetStatus(Loc.T("settings.model.registryLoadFailed",
                "❌ 读取模型库失败：{0}", Loc.T("settings.model.backendUnavailableShort", "后端未响应")));
            return;
        }

        var rows = new List<RegistryRow>();
        if (registry.TryGetProperty("models", out JsonElement models) && models.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement model in models.EnumerateArray())
            {
                bool current = GetBool(model, "current");
                bool exists = !model.TryGetProperty("exists", out JsonElement ex) || ex.ValueKind == JsonValueKind.True;
                string downloaded = GetString(model, "downloaded_at").Replace('T', ' ');
                string used = GetString(model, "last_used").Replace('T', ' ');
                var parts = new List<string>();
                foreach (string piece in new[]
                         {
                             GetString(model, "quantization"),
                             GetString(model, "repo") is { Length: > 0 } repo
                                 ? repo : Loc.T("settings.model.localFile", "本地文件"),
                             downloaded.Length > 0
                                 ? Loc.T("settings.model.downloadedAt", "下载于 {0}", downloaded) : "",
                             used.Length > 0
                                 ? Loc.T("settings.model.lastUsed", "最后使用 {0}", used)
                                 : Loc.T("settings.model.neverUsed", "尚未使用"),
                         })
                {
                    if (piece.Length > 0)
                        parts.Add(piece);
                }
                rows.Add(new RegistryRow(GetString(model, "name"), GetString(model, "size_human"),
                    string.Join(" · ", parts), current, exists));
            }
        }

        HubRegistryList.Children.Clear();
        if (!keepPendingDelete)
            _registryDeletePending = "";
        if (rows.Count == 0)
        {
            HubRegistryList.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.model.registryEmpty",
                    "（本地模型库还是空的：可在「模型市场」里下载，或把 .gguf 放进 models 目录后点「重新扫描模型文件夹」）"),
                FontSize = 11, Opacity = 0.7, TextWrapping = TextWrapping.Wrap,
            });
        }
        else
        {
            foreach (RegistryRow row in rows)
                HubRegistryList.Children.Add(BuildRegistryRow(row));
        }

        int count = GetInt(registry, "count", rows.Count);
        string total = GetString(registry, "total_size_human");
        HubRegistryTitle.Text = Loc.T("settings.model.registryTitle", "📚 本地模型库（{0} 个 · {1}）",
            count, total);
        Market.SetStatus(Loc.T("settings.model.registryLoaded", "✅ 已读取本地模型库"));
    }

    /// <summary>一行：名称 + 使用中徽标 / 元信息，右侧「加载」「删除」。</summary>
    private Control BuildRegistryRow(RegistryRow row)
    {
        var info = new StackPanel { Spacing = 1 };
        var head = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        head.Children.Add(new TextBlock { Text = row.Name, FontSize = 12, FontWeight = FontWeight.Bold });
        if (row.InUse)
        {
            head.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.model.badgeInUse", "▶ 正在使用"),
                FontSize = 10, VerticalAlignment = VerticalAlignment.Center,
                Foreground = TryBrush("AccentBrush"),
            });
        }
        info.Children.Add(head);
        if (row.Meta.Length > 0)
            info.Children.Add(new TextBlock { Text = row.Meta, FontSize = 10, Opacity = 0.65 });

        // 「加载」：使用中的模型不需要再点；文件不存在（登记残留）时也点不了 ——
        // 与 WPF 的 CanLoad 同一口径。
        var load = new Button
        {
            Content = Loc.T("settings.model.actionLoad", "加载"),
            Padding = new Thickness(10, 3),
            FontSize = 11,
            IsEnabled = row.Exists && !row.InUse,
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTip.SetTip(load, Loc.T("settings.model.loadTip",
            "热加载这个模型（成功后它就是当前模型，不需要重启程序）"));
        load.Click += async (_, _) => await LoadModelAsync(row);

        // 「删除」：使用中的模型不允许删（后端也会再拦一次），且需要二次确认。
        var delete = new Button
        {
            Content = row.Name == _registryDeletePending
                ? Loc.T("settings.model.actionDeleteConfirm", "确认删除？")
                : Loc.T("settings.model.actionDelete", "删除"),
            Padding = new Thickness(10, 3),
            FontSize = 11,
            IsEnabled = !row.InUse,
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTip.SetTip(delete, Loc.T("settings.model.deleteTip",
            "删除模型文件并从模型库移除（不可撤销，之后要重新下载）"));
        delete.Click += async (_, _) => await DeleteModelAsync(row);

        var actions = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 8,
            VerticalAlignment = VerticalAlignment.Center,
        };
        actions.Children.Add(load);
        actions.Children.Add(delete);

        var grid = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        Grid.SetColumn(info, 0);
        Grid.SetColumn(actions, 1);
        grid.Children.Add(info);
        grid.Children.Add(actions);

        var border = new Border
        {
            Padding = new Thickness(8, 4),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(4),
            BorderBrush = TryBrush("CardBorderBrush"),
            Child = grid,
        };
        AutomationProperties.SetAutomationId(border, "HubRegistry_" + row.Name);
        return border;
    }

    /// <summary>「重新扫描模型文件夹」：手工放进 .gguf 后不必重启即可加载。</summary>
    private async Task RescanModelsAsync()
    {
        HubRescanButton.IsEnabled = false;
        Market.SetStatus(Loc.T("settings.model.rescanning", "正在扫描模型文件夹…"));
        try
        {
            using JsonDocument? doc = await SendAsync(new { action = "model_scan" },
                TimeSpan.FromSeconds(180));
            Market.SetStatus(doc is null
                ? Loc.T("settings.model.rescanFailed", "❌ 扫描失败：{0}",
                    Loc.T("settings.model.backendUnavailableShort", "后端未响应"))
                : GetString(doc.RootElement, "content"));
            await LoadRegistryAsync();
        }
        finally
        {
            HubRescanButton.IsEnabled = true;
        }
    }

    /// <summary>
    /// 热加载某个本地模型。超时给到 10 分钟：把几 GB 的权重读进内存本来就要等，
    /// 用默认的 60 秒会让"模型其实加载成功了、界面却报失败"（WPF 侧同一取值）。
    /// </summary>
    private async Task LoadModelAsync(RegistryRow row)
    {
        Market.SetStatus(Loc.T("settings.model.loadingModel", "正在加载模型 {0}…", row.Name));
        using JsonDocument? doc = await SendAsync(
            new { action = "model_load", name = row.Name }, TimeSpan.FromMinutes(10));
        Market.SetStatus(doc is null
            ? Loc.T("settings.model.loadModelFailed", "❌ 加载失败：{0}",
                Loc.T("settings.model.backendUnavailableShort", "后端未响应"))
            : GetString(doc.RootElement, "content"));
        await LoadRegistryAsync();
    }

    /// <summary>
    /// 删除模型文件。两道保护，与 WPF 逐条对齐：
    ///   ① **使用中的模型不给删**（先切走再删），后端也会再拦一次；
    ///   ② **二次确认**，且确认文案里写明"删的是文件、不可撤销"。
    /// Avalonia 侧没有对话框体系，用就地二次确认（与档位删除同一套做法）。
    /// </summary>
    private async Task DeleteModelAsync(RegistryRow row)
    {
        if (row.InUse)
        {
            Market.SetStatus(Loc.T("settings.model.inUseCannotDelete",
                "该模型正在使用中，请先用 /model 切换到其它模型再删除。"));
            return;
        }
        if (!string.Equals(_registryDeletePending, row.Name, StringComparison.Ordinal))
        {
            // 第一次点：只把这一行的按钮换成「确认删除？」并说明后果，不执行任何删除。
            _registryDeletePending = row.Name;
            await LoadRegistryAsync(keepPendingDelete: true);
            Market.SetStatus(Loc.T("settings.model.deleteConfirmText",
                "确定删除模型「{0}」吗？\n\n将删除文件（{1}）并从模型库移除，此操作不可撤销。\n若之后还需要，必须重新下载。",
                row.Name, row.SizeText));
            return;
        }
        _registryDeletePending = "";
        Market.SetStatus(Loc.T("settings.model.deletingModel", "正在删除 {0}…", row.Name));
        using JsonDocument? doc = await SendAsync(
            new { action = "model_hub_uninstall", name = row.Name }, TimeSpan.FromSeconds(60));
        if (doc is null)
        {
            Market.SetStatus(Loc.T("settings.model.deleteFailed", "❌ 删除失败：{0}",
                Loc.T("settings.model.backendUnavailableShort", "后端未响应")));
        }
        else
        {
            string content = GetString(doc.RootElement, "content");
            Market.SetStatus(content.Length > 0
                ? content : Loc.T("settings.model.deleted", "已删除"));
        }
        await LoadRegistryAsync();
    }

    // -----------------------------------------------------------------
    // ① 档位
    // -----------------------------------------------------------------
    private async Task LoadProfilesAsync()
    {
        // 包值带 {0}，必须传参（否则 Loc.T 返回未格式化模板、界面上露出 {0}）
        ProfileStatusText.Text = Loc.T("settings.model.profileHint",
            "当前使用：{0}　·　点胶囊即可切换（热加载，无需重启）；未下载的档位点一下会先下载。",
            _currentProfile.Length > 0 ? _currentProfile : UnknownText());
        using JsonDocument? doc = await SendAsync(new { action = "model_profiles" });
        if (doc is null)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileLoadFailed",
                "❌ 读取模型档位失败：{0}", Loc.T("main.error.noResponse", "无响应"));
            return;
        }
        JsonElement data = doc.RootElement;
        _currentProfile = GetString(data, "current");
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
            // **v0.2.2 第 4 批（总策划裁定三）：判据从 `!current` 改成 `!(current && installed)`。**
            //
            // 这条缺陷 **Avalonia 原样复刻了 WPF**（两边都是 `IsEnabled = !current`），
            // 所以裁定要求"两边一起修"。触发条件：config 指着某个模型、而模型文件不在磁盘上
            // （全新安装就是这样）→ 后端曾同时给出 `current=true` 与 `installed=false` →
            // 胶囊显示"未下载"却被禁用 → **用户点不动它去下载**。
            //
            // 后端已同步修正（`current` 现在要求文件真的存在），这里再兜一层：
            // 只要没装到本地，胶囊就必须可点，点了走下载分支。
            IsEnabled = !(current && installed),
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

        // 「编辑」按钮：把这一档读进编辑器（v0.2.2 补全，对齐 WPF 的档位编辑器）
        var edit = new Button
        {
            Content = Loc.T("settings.model.profileEdit", "编辑"),
            Padding = new Thickness(8, 2),
            FontSize = 10,
            VerticalAlignment = VerticalAlignment.Center,
        };
        edit.Click += (_, _) => OpenProfileEditor(profile);
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 4 };
        row.Children.Add(chip);
        row.Children.Add(edit);
        return row;
    }

    // -----------------------------------------------------------------
    // 档位编辑器（新建 / 编辑 / 删除；对齐 WPF 的 SettingsWindow.Profiles.cs）
    // -----------------------------------------------------------------

    /// <summary>当前正在编辑的档位别名（空 = 新建）。改名时作为 rename_from 发给后端。</summary>
    private string _profileEditingAlias = "";
    private bool _profileDeletePending;

    /// <summary>
    /// 当前正在使用的档位名（后端 <c>model_profiles</c> 的顶层 <c>current</c>）。
    ///
    /// 为什么要存下来：`settings.model.profileHint` 的包值带 {0}（「当前使用：{0}…」），
    /// 而 `ApplyTexts()`（语言切换时）也要用它——语言切换发生在数据之后，
    /// 没有这个字段就只能写死或露出 {0}（硬编码清查 §5.3 C-1）。
    /// </summary>
    private string _currentProfile = "";

    /// <summary>档位状态行用的「未知」：优先用语言包，取不到时回落中文（与 WPF 同一写法）。</summary>
    private static string UnknownText() => Loc.T("capability.unknown", "未知");

    private void OpenProfileEditor(JsonElement? profile)
    {
        ProfileEditorPanel.IsVisible = true;
        _profileDeletePending = false;
        ProfileDeleteButton.Content = Loc.T("settings.profile.delete", "删除");
        ProfileEditorStatus.Text = "";
        if (profile is JsonElement item)
        {
            _profileEditingAlias = GetString(item, "alias");
            ProfileAliasBox.Text = _profileEditingAlias;
            ProfilePathBox.Text = GetString(item, "path");
            ProfileRepoBox.Text = GetString(item, "repo");
            ProfileContextBox.Text = NumberText(item, "context_length");
            ProfileTempBox.Text = NumberText(item, "temperature");
            ProfileEditorTitle.Text = Loc.T("settings.profile.editTitle", "编辑模型档位：{0}",
                _profileEditingAlias);
        }
        else
        {
            _profileEditingAlias = "";
            ProfileAliasBox.Text = "";
            ProfilePathBox.Text = "";
            ProfileRepoBox.Text = "";
            ProfileContextBox.Text = "";
            ProfileTempBox.Text = "";
            ProfileEditorTitle.Text = Loc.T("settings.profile.newTitle", "新建模型档位");
        }
        ProfileEditorPanel.IsVisible = true;
    }

    private async Task SaveProfileAsync()
    {
        string alias = ProfileAliasBox.Text?.Trim() ?? "";
        if (alias.Length == 0)
        {
            ProfileEditorStatus.Text = Loc.T("settings.profile.aliasRequired", "⚠️ 别名不能为空");
            return;
        }
        // 温度是界面上唯一"明知合法范围"的字段（0–1.5），先在客户端挡一道；
        // 其余（上下文上限、别名长度、路径合法性）**一律不在这里夹紧** ——
        // 把原值发给后端、显示后端返回的 ❌ 文案（接口夹紧=骗人，后端 main.py 的
        // 同名注释写过这条教训，前端不重复第二套规则）。
        string tempText = ProfileTempBox.Text?.Trim() ?? "";
        if (tempText.Length > 0)
        {
            if (!double.TryParse(tempText, System.Globalization.NumberStyles.Float,
                                 System.Globalization.CultureInfo.InvariantCulture, out double temp))
            {
                ProfileEditorStatus.Text = Loc.T("settings.profile.tempInvalid",
                    "⚠️ 温度需要是数字（{0}–{1}）", 0, 1.5);
                return;
            }
            if (temp < 0.0 || temp > 1.5)
            {
                ProfileEditorStatus.Text = Loc.T("settings.profile.tempRange",
                    "⚠️ 温度需要在 {0}–{1} 之间", 0, 1.5);
                return;
            }
        }

        var patch = new Dictionary<string, object?> { ["action"] = "model_profile_save", ["alias"] = alias };
        // 只发**填了内容**的字段：后端把空字符串当成"删掉这一项"，
        // 而界面上空输入框更可能是"我没动它" —— 少发一个键 = 不覆盖已有值。
        AddProfileField(patch, "path", ProfilePathBox);
        AddProfileField(patch, "repo", ProfileRepoBox);
        AddProfileField(patch, "context_length", ProfileContextBox);
        AddProfileField(patch, "temperature", ProfileTempBox);
        if (_profileEditingAlias.Length > 0
            && !string.Equals(_profileEditingAlias, alias, StringComparison.Ordinal))
            patch["rename_from"] = _profileEditingAlias;

        ProfileEditorStatus.Text = Loc.T("settings.profile.saving", "正在保存档位…");
        using JsonDocument? doc = await SendAsync(patch, TimeSpan.FromSeconds(60));
        if (doc is null)
        {
            ProfileEditorStatus.Text = Loc.T("settings.profile.saveFailed",
                "❌ 保存失败：{0}", "后端未响应");
            return;
        }
        ApplyProfileWriteResult(doc.RootElement);
        await LoadProfilesAsync();
    }

    private static void AddProfileField(Dictionary<string, object?> patch, string field, TextBox box)
    {
        string text = box.Text?.Trim() ?? "";
        if (text.Length > 0)
            patch[field] = text;
    }

    /// <summary>
    /// 把响应里的一个数值字段读成文本（用于填输入框）。
    /// 不能直接用 GetString：`context_length` / `temperature` 在 JSON 里是**数字**，
    /// GetString 只认字符串，会返回空串 —— 表现是"编辑某档时这两个框是空的"，
    /// 用户一保存就把已有值清掉了（比不显示更糟）。
    /// </summary>
    private static string NumberText(JsonElement item, string name)
    {
        if (item.ValueKind != JsonValueKind.Object || !item.TryGetProperty(name, out JsonElement el))
            return "";
        return el.ValueKind switch
        {
            JsonValueKind.Number => el.GetRawText(),
            JsonValueKind.String => el.GetString() ?? "",
            _ => "",
        };
    }

    private void ApplyProfileWriteResult(JsonElement root)
    {
        string content = GetString(root, "content");
        if (content.Length > 0)
        {
            ProfileEditorStatus.Text = content;
            return;
        }
        // 后端把成功/失败都放在 model_profile_save 里：成功给 content，失败给 error
        if (root.TryGetProperty("model_profile_save", out JsonElement saved))
        {
            bool ok = GetBool(saved, "ok");
            string error = GetString(saved, "error");
            ProfileEditorStatus.Text = ok
                ? Loc.T("settings.profile.saved", "✅ 档位已保存")
                : (error.Length > 0
                    ? error
                    : Loc.T("settings.profile.saveFailed", "❌ 保存失败：{0}",
                        Loc.T("settings.profile.unknownError", "未知原因")));
            return;
        }
        ProfileEditorStatus.Text = Loc.T("settings.profile.saveFailed", "❌ 保存失败：{0}",
            Loc.T("settings.profile.unknownError", "未知原因"));
    }

    /// <summary>
    /// 删除档位登记（**不删磁盘上的模型文件**）。
    /// 就地二次确认（与技能市场同一套做法）：第一次点变「确认删除？」，再点一次才执行。
    /// 确认文案里必须写明"只删登记、不删文件" —— 那是用户最容易误解的一点。
    /// </summary>
    private async Task DeleteProfileAsync()
    {
        if (_profileEditingAlias.Length == 0)
        {
            ProfileEditorStatus.Text = Loc.T("settings.profile.deleteNeedEdit",
                "⚠️ 请先在上面选一个档位（点胶囊旁的「编辑」）再删除。");
            return;
        }
        if (!_profileDeletePending)
        {
            _profileDeletePending = true;
            ProfileDeleteButton.Content = Loc.T("settings.profile.deleteConfirmShort", "确认删除？");
            ProfileEditorStatus.Text = Loc.T("settings.profile.deleteConfirmText",
                "确定删除档位「{0}」吗？\n\n只删除 config.json 里的这条登记，不会删除磁盘上的模型文件；"
                + "之后仍可用「＋ 新建档位」把它登记回来。", _profileEditingAlias);
            return;
        }
        _profileDeletePending = false;
        ProfileDeleteButton.Content = Loc.T("settings.profile.delete", "删除");
        ProfileEditorStatus.Text = Loc.T("settings.profile.deleting", "正在删除档位…");
        using JsonDocument? doc = await SendAsync(
            new { action = "model_profile_delete", alias = _profileEditingAlias }, TimeSpan.FromSeconds(60));
        if (doc is null)
        {
            ProfileEditorStatus.Text = Loc.T("settings.profile.deleteFailed",
                "❌ 删除失败：{0}", "后端未响应");
            return;
        }
        string content = GetString(doc.RootElement, "content");
        ProfileEditorStatus.Text = content.Length > 0
            ? content
            : Loc.T("settings.profile.deleted", "🗑️ 档位已删除（磁盘文件保持不动）");
        _profileEditingAlias = "";
        ProfileEditorPanel.IsVisible = false;
        await LoadProfilesAsync();
    }

    private async Task ApplyProfileAsync(string alias, string caption)
    {
        ProfileStatusText.Text = Loc.T("settings.model.profileApplying", "⏳ 正在切换到 {0} …", caption);
        using JsonDocument? doc = await SendAsync(
            new { action = "model_profile_apply", alias }, TimeSpan.FromSeconds(180));
        if (doc is null)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileApplyFailed",
                "❌ 切换失败：{0}", Loc.T("main.error.noResponse", "无响应"));
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
            InferenceStatusText.Text = Loc.T("settings.inference.saveFailed", "❌ 保存失败：{0}",
                Loc.T("main.error.noResponse", "无响应"));
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

    /// <summary>取主题画刷；主题里没有这个键时返回 null（控件用默认前景色，不崩）。</summary>
    private static Brush? TryBrush(string key)
    {
        if (Application.Current is null)
            return null;
        if (Application.Current.TryFindResource(key, out object? value) && value is Brush brush)
            return brush;
        return null;
    }

    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        ProfileTitle.Text = Loc.T("settings.model.profileTitle", "🎚️ 模型档位（可选默认模型）");
        ProfileHintText.Text = Loc.T("settings.model.profileHint",
            "当前使用：{0}　·　点胶囊即可切换（热加载，无需重启）；未下载的档位点一下会先下载。",
            _currentProfile.Length > 0 ? _currentProfile : UnknownText());
        ProfileRefreshButton.Content = Loc.T("settings.model.actionRefreshProfiles", "刷新档位");
        ProfileNewButton.Content = Loc.T("settings.profile.new", "＋ 新建档位");
        ProfileAliasLabel.Text = Loc.T("settings.profile.alias", "别名");
        ProfilePathLabel.Text = Loc.T("settings.profile.path", "文件名");
        ProfileRepoLabel.Text = Loc.T("settings.profile.repo", "仓库");
        ProfileContextLabel.Text = Loc.T("settings.profile.context", "上下文长度");
        ProfileTempLabel.Text = Loc.T("settings.profile.temperature", "温度");
        ProfileSaveButton.Content = Loc.T("settings.profile.save", "保存档位");
        ProfileDeleteButton.Content = Loc.T("settings.profile.delete", "删除");
        ProfileCancelButton.Content = Loc.T("settings.button.close", "关闭");

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

        // 本地模型库（v0.2.2 第 2 批）。标题会随数据重写（"（N 个 · 大小）"），
        // 这里只保证"还没读到数据时它也不是空的"；两个按钮的文案是静态的。
        HubRegistryTitle.Text = Loc.T("settings.model.localRegistryTitle", "本地模型库");
        HubRescanButton.Content = Loc.T("settings.model.actionRescan", "重新扫描模型文件夹");
        HubRegistryRefreshButton.Content = Loc.T("settings.model.actionRefreshRegistry", "刷新本地模型库");

        MarketNotMigratedText.Text = "";

        // 下拉项文案是委托，重建后按值选回。
        string? preset = (PresetCombo.SelectedItem as Option)?.Value;
        string? mode = (ContextModeCombo.SelectedItem as Option)?.Value;
        BuildOptions();
        Select(PresetCombo, preset);
        Select(ContextModeCombo, mode);
        RefreshSliderLabels();
        Market.ApplyTexts();
    }

    // -----------------------------------------------------------------
    // ③ 模型市场（本轮补全，v0.2.2）
    // -----------------------------------------------------------------

    // -----------------------------------------------------------------
    // 探针接口（`EchoDesk --probe market` 用：无人值守验整条市场交互）
    // -----------------------------------------------------------------

    /// <summary>市场区块的可观测状态快照（见 ModelMarketSection.Snapshot）。</summary>
    public ModelMarketSection.MarketState MarketSnapshot() => Market.Snapshot();

    public Task ProbeMarketSearchAsync() => Market.ProbeSearchAsync();
    public Task ProbeMarketFilterAsync(string keyword) => Market.ProbeFilterAsync(keyword);
    public Task ProbeMarketToggleFacetAsync(int index) => Market.ProbeToggleFacetAsync(index);
    public Task ProbeMarketGoToPageAsync(int page) => Market.ProbeGoToPageAsync(page);
    public Task ProbeMarketSetPageSizeAsync(int size) => Market.ProbeSetPageSizeAsync(size);
    public Task ProbeMarketResetAsync() => Market.ProbeResetFiltersAsync();
    public Task ProbeMarketOpenPinnedDetailAsync() => Market.ProbeOpenPinnedDetailAsync();

    /// <summary>探针：填入模型市场演示数据（README 截图用，可选英文）。</summary>
    public void ProbeShowDemoMarket(bool english = false) => Market.ProbeShowDemo(english);

    // -----------------------------------------------------------------
    // 探针接口（`EchoDesk --probe pages` 用：本地模型库那一块）
    // -----------------------------------------------------------------

    /// <summary>本地模型库的可观测状态（行数 / 空状态 / 两个按钮有没有文案）。</summary>
    public string RegistrySnapshot()
    {
        int rows = 0;
        bool emptyHint = false;
        foreach (Control child in HubRegistryList.Children)
        {
            if (child is TextBlock hint && hint.Text?.StartsWith("（") == true)
            {
                emptyHint = true;
                continue;
            }
            rows++;
        }
        return string.Join("; ", new[]
        {
            $"registryRows={rows}",
            $"registryEmptyHint={(emptyHint ? 1 : 0)}",
            $"registryTitleHasParen={(HubRegistryTitle.Text?.Contains('（') == true ? 1 : 0)}",
            $"rescanButton={(string.IsNullOrEmpty(HubRescanButton.Content as string) ? 0 : 1)}",
            $"refreshRegistryButton={(string.IsNullOrEmpty(HubRegistryRefreshButton.Content as string) ? 0 : 1)}",
            $"statusLine={(string.IsNullOrEmpty(Market.Snapshot().Status) ? 0 : 1)}",
        });
    }

    /// <summary>探针：重扫模型文件夹 + 重读模型库（走真实后端动作）。</summary>
    public async Task ProbeRescanAsync() => await RescanModelsAsync();

    /// <summary>探针：只重读模型库（不重扫磁盘）。</summary>
    public async Task ProbeReloadRegistryAsync() => await LoadRegistryAsync();

    /// <summary>市场区块（控件多，单独一个 UserControl；由这里负责"谁来发请求"）。</summary>
    private ModelMarketSection? _market;

    private ModelMarketSection Market => _market ??= CreateMarket();

    private ModelMarketSection CreateMarket()
    {
        var section = new ModelMarketSection();
        // 把"怎么发请求"交给这一页（页面已经持有后端连接），市场本身不碰 AppState 之外的全局
        section.Attach(new MarketHost(this));
        MarketHostGrid.Children.Add(section);
        Grid.SetRow(section, 0);
        return section;
    }

    /// <summary>市场区块与页面之间的桥：只暴露它真正需要的三件事。</summary>
    private sealed class MarketHost : IModelMarketHost
    {
        private readonly ModelPage _page;
        public MarketHost(ModelPage page) => _page = page;

        public bool BackendAvailable => _page._host?.State.ActiveBackend.IsRunning == true;

        public async Task<JsonDocument?> SendAsync(object request, TimeSpan? idleTimeout = null)
            => await _page.SendAsync(request, idleTimeout);

        public async Task SendStreamAsync(object request, Action<string, JsonElement> onEvent,
                                          TimeSpan? idleTimeout = null)
        {
            if (_page._host is null)
                return;
            await _page._host.State.ActiveBackend.SendStreamAsync(
                request,
                onDelta: _ => { },
                onContext: (_, _) => { },
                idleTimeout: idleTimeout,
                onEvent: onEvent,
                onDeltaChannel: (_, _, _) => { });
        }
    }
}



