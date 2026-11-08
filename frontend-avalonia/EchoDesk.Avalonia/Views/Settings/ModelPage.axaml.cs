// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ModelPage.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ModelPage.axaml.cs。类型与成员说明见下方注释。
// [EN] Interaction logic of the model page (v0.2.1 cross-platform-2 development task 1,
// [EN] the eighth migrated module).
// [ZH] 模型页的交互逻辑（v0.2.1 跨平台-2 研发任务 1，第 8 个迁移模块）。
//
// [EN] Backend actions:
// [ZH] 后端动作：
//   model_profiles         → { current, items: [ { alias, name, repo, installed, current,
//                             size_human, context_length, temperature, params, quantization } ] }
// [EN]   model_profile_apply    { alias }      switches the profile (hot reload)
// [ZH]   model_profile_apply    { alias }      切换档位（热加载）
//   capability             → { settings: { temperature, top_p, repeat_penalty, max_tokens,
//                             preset, context_mode, clarify_vague, summary_cache } }
//   capability_settings    { preset, temperature, top_p, repeat_penalty, max_tokens,
//                             context_mode, clarify_vague, summary_cache }
//
// [EN] Two design decisions deliberately copied from the WPF build (neither is casual):
// [ZH] 两处刻意照搬 WPF 版的设计（都不是随手写的）：
//
// [EN] 1. **The pill uses a TextBlock to carry the text**: some frameworks render the
// [EN]    string content of a button as an access key, so the underscore is eaten
// [EN]    ("Q4_K_M" is displayed as "Q4KM"). Underscores are normal in quantization names;
// [EN]    wrapping the text in a TextBlock guarantees verbatim display. The Avalonia build
// [EN]    does the same, so behavior is consistent and independent of framework
// [EN]    differences.
// [ZH] 1. **胶囊用 TextBlock 承载文字**：某些框架会把按钮的字符串内容当访问键渲染，
// [ZH]    下划线被吃掉（"Q4_K_M" 显示成 "Q4KM"）。量化名里下划线是常态，用 TextBlock 包一层
// [ZH]    就一定原样显示。Avalonia 这边同样处理，behavior 一致、不依赖框架差异。
//
// [EN] 2. **The slider bounds are "expanded"**: config.json is hand-written and max_tokens
// [EN]    is commonly 2048/4096/8192, while the UI range is 64-4096. If 8192 were hard
// [EN]    clipped to 4096, a user who merely opens the settings page and clicks save
// [EN]    without changing anything would have the 8192 in the configuration quietly
// [EN]    rewritten as 4096 — the classic "a read corrupts the write". Expanding the bounds
// [EN]    lets the slider reflect the actual state faithfully.
// [ZH] 2. **滑块边界会被"撑开"**：config.json 是人手写的，max_tokens 常见 2048/4096/8192，
// [ZH]    而界面范围是 64–4096。若把 8192 硬裁成 4096，用户只是打开设置页、什么都没改就点保存，
// [ZH]    配置里的 8192 就被悄悄改成 4096 —— 典型的"读一下就写坏"。撑开边界让滑块如实表示现状。
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
    // [EN] Recommendation snapshot from the backend, used to fill the editable
    // [EN] override inputs and to decide locally whether a value is over budget.
    // [ZH] 来自后端的推荐值快照，用于填入可编辑覆盖输入框并在本地判断是否超预算。
    private int _recommendedNctx;
    private int _recommendedGpuLayers;
    private int _recommendedBatch;
    private bool _recommendedGpuAvailable;
    private int _hardwareBudget;
    private readonly List<string> _backendWarnings = new();

    /// <summary>
    /// [EN] Default values: used when the backend settings cannot be read (the same set of
    /// [EN] defaults as the WPF build).
    /// [ZH] 默认值：读不到后端设置时用它（与 WPF 版同一组默认）。
    /// </summary>
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
        InferenceTestLoadButton.Click += async (_, _) => await TestLoadInferenceAsync();
        NctxBox.TextChanged += (_, _) => UpdateOverrideWarnings();
        GpuLayersBox.TextChanged += (_, _) => UpdateOverrideWarnings();
        InferenceResetButton.Click += async (_, _) =>
        {
            // [EN] Restore defaults: this only moves the sliders back to the default values
            // [EN] and hints that "save is still required"; it does not write to the backend
            // [EN] directly, so the user can see the result before deciding.
            // [ZH] 恢复默认：只是把滑块拨回默认值并提示"还要点保存"，
            // [ZH] 不直接写后端 —— 让用户能先看到结果再决定。
            _syncing = true;
            SetSlider(TempSlider, TempValueText, DefaultTemperature);
            SetSlider(TopPSlider, TopPValueText, DefaultTopP);
            SetSlider(RepeatSlider, RepeatValueText, DefaultRepeatPenalty);
            SetSlider(MaxTokensSlider, MaxTokensValueText, DefaultMaxTokens);
            NctxBox.Text = _recommendedNctx.ToString();
            GpuLayersBox.Text = _recommendedGpuLayers.ToString();
            _syncing = false;
            UpdateOverrideWarnings();
            InferenceStatusText.Text = Loc.T("settings.inference.resetApplied",
                "↩️ 已恢复默认值（点「保存」后写入 config.json）");
        };

        foreach (Slider slider in new[] { TempSlider, TopPSlider, RepeatSlider, MaxTokensSlider })
            slider.PropertyChanged += (_, e) =>
            {
                if (e.Property == Slider.ValueProperty)
                    RefreshSliderLabels();
            };

        // [EN] Local model library (v0.2.2, batch 2): rescan / refresh
        // [ZH] 本地模型库（v0.2.2 第 2 批）：重扫 / 刷新
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
    /// [EN] The shell's save button -> saves the inference parameters.
    /// [EN] Profile switching **takes effect immediately** (clicking a pill hot-reloads), so
    /// [EN] it needs no "save" step; the only thing on this page that really has to be
    /// [EN] persisted is the inference parameters.
    /// [ZH] 骨架的保存按钮 → 保存推理参数。
    /// [ZH] 档位切换是**即时生效**的（胶囊点一下就热加载），不需要"保存"这一步；
    /// [ZH] 这一页真正需要落盘的只有推理参数。
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
        // [EN] Inference presets are construction-time profiles (initial context,
        // [EN] usable share of the native/hardware ceiling, batch size and thread
        // [EN] preference) and are deliberately separate from the sampling preset
        // [EN] above.
        // [ZH] 推理预设属于构建期档位（初始上下文、原生/硬件上限的可用比例、批大小与
        // [ZH] 线程偏好），与上面的采样预设刻意分开。
        InferenceProfileCombo.ItemsSource = new List<Option>
        {
            new("fast", () => Loc.T("settings.inference.profileFast", "极速")),
            new("balanced", () => Loc.T("settings.inference.profileBalanced", "均衡")),
            new("quality", () => Loc.T("settings.inference.profileQuality", "质量")),
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

    /// <summary>
    /// [EN] Fills in a slider value and expands the bounds when necessary (see item 2 in the
    /// [EN] file header).
    /// [ZH] 填滑块值，必要时撑开边界（见文件头第 2 条）。
    /// </summary>
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
        // [EN] ActiveBackend: at run time this is App.State.Backend; in probe mode it is the
        // [EN] probe's own connection (see the notes on AppState.ProbeBackendOverride).
        // [ZH] ActiveBackend：运行时就是 App.State.Backend；探针模式下是探针自己那条连接
        // [ZH] （见 AppState.ProbeBackendOverride 的说明）。
        return await _host.State.ActiveBackend.SendAsync(request, idleTimeout: idle ?? TimeSpan.FromSeconds(60));
    }

    public async Task RefreshAsync()
    {
        await LoadProfilesAsync();
        await LoadInferenceAsync();
        await LoadRegistryAsync();
        // [EN] The model market only refreshes "the page it already has" and **does not
        // [EN] search automatically** (consistent with the WPF build: opening the settings
        // [EN] page must not fire a remote request out of nowhere; that is what the user
        // [EN] does by clicking "search").
        // [ZH] 模型市场只刷新"已有的那一页"，**不自动搜索**（与 WPF 版一致：
        // [ZH] 打开设置页不该凭空打一次远端请求，那是用户点「搜索」才做的事）
        await Market.RefreshAsync();
    }

    // [EN] ---------- (3) Local model library (v0.2.2 batch 2, WPF HubRegistry) ----------
    // [ZH] ---------- ③ 本地模型库（v0.2.2 第 2 批补全，对齐 WPF 的 HubRegistry 那一块） ----------
    //
    // [EN] Division of work with the "model market" (on the WPF side these have always been
    // [EN] two different things; copied item by item here):
    // [EN]   market = what the remote side "can download"; library = what the local disk
    // [EN]   "has".
    // [ZH] 与「模型市场」的分工（WPF 侧一直是两块不同的东西，此处逐项照抄）：
    // [ZH]   市场 = 远端"能下载什么"；模型库 = 本地磁盘"有什么"。
    //
    // [EN] Three backend actions:
    // [ZH] 三个后端动作：
    //   model_hub_registry   → { registry: { count, total_size_human,
    //                            models: [ { name, size_human, quantization, repo,
    //                                        downloaded_at, last_used, current, exists } ] } }
    // [EN]   model_scan           -> rescans the model folder (manual .gguf, no restart)
    // [ZH]   model_scan           → 重新扫描模型文件夹（手工放进 .gguf 后不必重启）
    // [EN]   model_load {name}    -> hot-loads that model (it becomes current on success)
    // [ZH]   model_load {name}    → 热加载该模型（成功后就是当前模型）
    // [EN]   model_hub_uninstall {name} -> deletes the model file
    // [ZH]   model_hub_uninstall {name} → 删除模型文件

    /// <summary>
    /// [EN] One row in the local model library.
    /// [ZH] 本地模型库里的一行。
    /// </summary>
    private sealed record RegistryRow(string Name, string SizeText, string Meta,
                                      bool InUse, bool Exists);

    /// <summary>
    /// [EN] Second-confirmation state for deletion: the first click turns the button into
    /// [EN] "confirm delete?" and only a second click executes.
    /// [ZH] 删除的二次确认状态：第一次点变「确认删除？」，再点一次才执行。
    /// </summary>
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

    /// <summary>
    /// [EN] One row: name + in-use badge / metadata, with "load" and "delete" on the right.
    /// [ZH] 一行：名称 + 使用中徽标 / 元信息，右侧「加载」「删除」。
    /// </summary>
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

        // [EN] "Load": a model that is in use needs no further click, and the button is also
        // [EN] disabled when the file is missing (a leftover registration) — the same rule as
        // [EN] WPF's CanLoad.
        // [ZH] 「加载」：使用中的模型不需要再点；文件不存在（登记残留）时也点不了 ——
        // [ZH] 与 WPF 的 CanLoad 同一口径。
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

        // [EN] "Delete": a model in use may not be deleted (the backend blocks it again),
        // [EN] and a second confirmation is required.
        // [ZH] 「删除」：使用中的模型不允许删（后端也会再拦一次），且需要二次确认。
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

    /// <summary>
    /// [EN] "Rescan model folder": after placing a .gguf by hand it can be loaded without a
    /// [EN] restart.
    /// [ZH] 「重新扫描模型文件夹」：手工放进 .gguf 后不必重启即可加载。
    /// </summary>
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
    /// [EN] Hot-loads one local model. The timeout is set to 10 minutes because reading
    /// [EN] several GB of weights into memory takes time; the default 60 seconds would make
    /// [EN] "the model actually loaded successfully while the UI reports a failure" happen
    /// [EN] (the same value as on the WPF side).
    /// [ZH] 热加载某个本地模型。超时给到 10 分钟：把几 GB 的权重读进内存本来就要等，
    /// [ZH] 用默认的 60 秒会让"模型其实加载成功了、界面却报失败"（WPF 侧同一取值）。
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
    /// [EN] Deletes a model file. Two safeguards, aligned item by item with WPF:
    /// [EN]   1. **A model in use cannot be deleted** (switch away first); the backend blocks
    /// [EN]      it again;
    /// [EN]   2. **A second confirmation**, whose text states that "the file is deleted and
    /// [EN]      the action cannot be undone".
    /// [EN] The Avalonia build has no dialog system, so the confirmation happens inline (the
    /// [EN] same approach as profile deletion).
    /// [ZH] 删除模型文件。两道保护，与 WPF 逐条对齐：
    /// [ZH]   ① **使用中的模型不给删**（先切走再删），后端也会再拦一次；
    /// [ZH]   ② **二次确认**，且确认文案里写明"删的是文件、不可撤销"。
    /// [ZH] Avalonia 侧没有对话框体系，用就地二次确认（与档位删除同一套做法）。
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
            // [EN] First click: only turn this row's button into "confirm delete?" and state
            // [EN] the consequences; no deletion is performed.
            // [ZH] 第一次点：只把这一行的按钮换成「确认删除？」并说明后果，不执行任何删除。
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

    // [EN] ---------- (1) Model profiles ----------
    // [ZH] ---------- ① 档位 ----------
    private async Task LoadProfilesAsync()
    {
        // [EN] The resource value contains {0} and the argument must be passed (otherwise
        // [EN] Loc.T returns an unformatted template and {0} shows up in the UI).
        // [ZH] 包值带 {0}，必须传参（否则 Loc.T 返回未格式化模板、界面上露出 {0}）
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
        // [EN] A TextBlock is used instead of putting the string content directly on the
        // [EN] button: this keeps the framework from treating the underscore as an access key
        // [EN] (see item 1 in the file header).
        // [ZH] 用 TextBlock 而不是直接给按钮字符串内容：避免框架把下划线当访问键吃掉（见文件头第 1 条）。
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
            // [EN] **v0.2.2 batch 4 (the lead planner's third ruling): the criterion changed
            // [EN] from `!current` to `!(current && installed)`.**
            //
            // [EN] **Avalonia reproduced this defect from WPF verbatim** (both sides used
            // [EN] `IsEnabled = !current`), so the ruling requires "fix both sides together".
            // [EN] Trigger condition: config points at a model whose file is not on disk (a
            // [EN] fresh installation looks exactly like this) -> the backend once returned
            // [EN] `current=true` together with `installed=false` -> the pill showed "not
            // [EN] downloaded" but was disabled -> **the user could not click it to download**.
            //
            // [EN] The backend has been corrected in step (`current` now requires the file to
            // [EN] really exist); this is one more safety net: as long as the model is not
            // [EN] installed locally, the pill must be clickable, and clicking it takes the
            // [EN] download branch.
            // [ZH] **v0.2.2 第 4 批（总策划裁定三）：判据从 `!current` 改成 `!(current && installed)`。**
            //
            // [ZH] 这条缺陷 **Avalonia 原样复刻了 WPF**（两边都是 `IsEnabled = !current`），
            // [ZH] 所以裁定要求"两边一起修"。触发条件：config 指着某个模型、而模型文件不在磁盘上
            // [ZH] （全新安装就是这样）→ 后端曾同时给出 `current=true` 与 `installed=false` →
            // [ZH] 胶囊显示"未下载"却被禁用 → **用户点不动它去下载**。
            //
            // [ZH] 后端已同步修正（`current` 现在要求文件真的存在），这里再兜一层：
            // [ZH] 只要没装到本地，胶囊就必须可点，点了走下载分支。
            IsEnabled = !(current && installed),
        };
        // [EN] Avalonia controls **do not have** a `ToolTip` property (WPF's Control does);
        // [EN] the tip is an **attached property**: writing `ToolTip = ...` inside an object
        // [EN] initializer fails to compile with CS0117, and `ToolTip.SetTip()` must be used.
        // [EN] This is the fifth "WPF has it, Avalonia does not" difference in this round
        // [EN] (see the pitfall list in the notes).
        // [ZH] Avalonia 的控件**没有** `ToolTip` 属性（WPF 的 Control 有），提示是**附加属性**：
        // [ZH] 在对象初始化器里写 `ToolTip = …` 会编译报 CS0117，必须用 `ToolTip.SetTip()`。
        // [ZH] 这是本轮第 5 处"WPF 有、Avalonia 没有"的差异（见记录里的坑列表）。
        ToolTip.SetTip(chip, tip);
        chip.Click += async (_, _) =>
        {
            if (installed)
                await ApplyProfileAsync(alias, caption);
            else
                await DownloadProfileAsync(alias, repo, sizeText, caption);
        };

        // [EN] The "edit" button: reads this profile into the editor (completed in v0.2.2,
        // [EN] aligned with the WPF profile editor).
        // [ZH] 「编辑」按钮：把这一档读进编辑器（v0.2.2 补全，对齐 WPF 的档位编辑器）
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

    // [EN] ---------- Profile editor (new / edit / delete, WPF Profiles.cs) ----------
    // [ZH] ---------- 档位编辑器（新建 / 编辑 / 删除；对齐 WPF 的 SettingsWindow.Profiles.cs） ----------

    /// <summary>
    /// [EN] Alias of the profile currently being edited (empty = creating a new one). On a
    /// [EN] rename it is sent to the backend as rename_from.
    /// [ZH] 当前正在编辑的档位别名（空 = 新建）。改名时作为 rename_from 发给后端。
    /// </summary>
    private string _profileEditingAlias = "";
    private bool _profileDeletePending;

    /// <summary>
    /// [EN] The alias of the profile currently in use (the top-level <c>current</c> of the
    /// [EN] backend <c>model_profiles</c> response).
    ///
    /// [EN] Why it is stored: the resource value of `settings.model.profileHint` contains
    /// [EN] {0} ("currently in use: {0}..."), and `ApplyTexts()` (on a language switch) needs
    /// [EN] it too — the language switch happens after the data, so without this field the
    /// [EN] text could only be hardcoded or would expose {0} (hardcoded-string audit §5.3
    /// [EN] C-1).
    /// [ZH] 当前正在使用的档位名（后端 <c>model_profiles</c> 的顶层 <c>current</c>）。
    ///
    /// [ZH] 为什么要存下来：`settings.model.profileHint` 的包值带 {0}（「当前使用：{0}…」），
    /// [ZH] 而 `ApplyTexts()`（语言切换时）也要用它——语言切换发生在数据之后，
    /// [ZH] 没有这个字段就只能写死或露出 {0}（硬编码清查 §5.3 C-1）。
    /// </summary>
    private string _currentProfile = "";

    /// <summary>
    /// [EN] The "unknown" text used by the profile status line: the language pack is
    /// [EN] preferred and Chinese is the fallback when it is unavailable (the same approach
    /// [EN] as WPF).
    /// [ZH] 档位状态行用的「未知」：优先用语言包，取不到时回落中文（与 WPF 同一写法）。
    /// </summary>
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
        // [EN] Temperature is the only field on this page whose legal range (0-1.5) is known,
        // [EN] so it is guarded once on the client.
        // [EN] Everything else (context cap, alias length, path validity) is **never clamped
        // [EN] here** — the original value is sent to the backend and the backend's ❌ text is
        // [EN] displayed (clamping in the API is lying; the comment of the same name in the
        // [EN] backend main.py records this lesson, and the front end does not duplicate a
        // [EN] second set of rules).
        // [ZH] 温度是界面上唯一"明知合法范围"的字段（0–1.5），先在客户端挡一道；
        // [ZH] 其余（上下文上限、别名长度、路径合法性）**一律不在这里夹紧** ——
        // [ZH] 把原值发给后端、显示后端返回的 ❌ 文案（接口夹紧=骗人，后端 main.py 的
        // [ZH] 同名注释写过这条教训，前端不重复第二套规则）。
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
        // [EN] Only fields **that have content** are sent: the backend treats an empty string
        // [EN] as "delete this entry", while an empty input box in the UI more likely means
        // [EN] "I did not touch it" — omitting a key means the existing value is not
        // [EN] overwritten.
        // [ZH] 只发**填了内容**的字段：后端把空字符串当成"删掉这一项"，
        // [ZH] 而界面上空输入框更可能是"我没动它" —— 少发一个键 = 不覆盖已有值。
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
    /// [EN] Reads a numeric field from the response as text (used to fill an input box).
    /// [EN] GetString cannot be used directly: `context_length` / `temperature` are
    /// [EN] **numbers** in JSON and GetString only accepts strings, so it returns an empty
    /// [EN] string — the symptom is "these two boxes are empty when editing a profile" and
    /// [EN] the existing values are wiped as soon as the user saves (worse than not showing
    /// [EN] them at all).
    /// [ZH] 把响应里的一个数值字段读成文本（用于填输入框）。
    /// [ZH] 不能直接用 GetString：`context_length` / `temperature` 在 JSON 里是**数字**，
    /// [ZH] GetString 只认字符串，会返回空串 —— 表现是"编辑某档时这两个框是空的"，
    /// [ZH] 用户一保存就把已有值清掉了（比不显示更糟）。
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
        // [EN] The backend puts both success and failure under model_profile_save: content on
        // [EN] success, error on failure.
        // [ZH] 后端把成功/失败都放在 model_profile_save 里：成功给 content，失败给 error
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
    /// [EN] Deletes a profile registration (**the model file on disk is not deleted**).
    /// [EN] Inline second confirmation (the same approach as the skill market): the first
    /// [EN] click turns the button into "confirm delete?" and only a second click executes.
    /// [EN] The confirmation text must state "the registration is deleted, the file is not" —
    /// [EN] that is the point users most easily misunderstand.
    /// [ZH] 删除档位登记（**不删磁盘上的模型文件**）。
    /// [ZH] 就地二次确认（与技能市场同一套做法）：第一次点变「确认删除？」，再点一次才执行。
    /// [ZH] 确认文案里必须写明"只删登记、不删文件" —— 那是用户最容易误解的一点。
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
        // [EN] The first file in the details is used to send the download request (the same
        // [EN] rule as the WPF build: the profile pill is only responsible for "downloading
        // [EN] this profile").
        // [ZH] 详情里取第一个文件发下载请求（与 WPF 版同口径：档位胶囊只负责"下载这一档"）。
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

    // [EN] ---------- (2) Inference parameters ----------
    // [ZH] ---------- ② 推理参数 ----------
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
            Select(InferenceProfileCombo, GetString(settings, "inference_profile"));
            Select(ContextModeCombo, GetString(settings, "context_mode"));
            ClarifyVagueCheck.IsChecked = GetBoolOr(settings, "clarify_vague", true);
            SummaryCacheCheck.IsChecked = GetBoolOr(settings, "summary_cache", true);
            if (doc.RootElement.TryGetProperty("inference", out JsonElement inference))
                ApplyInferenceInfo(inference);
            if (doc.RootElement.TryGetProperty("hardware", out JsonElement hardware))
            {
                ApplyHardwareInfo(hardware);
                ApplyHardwareInputs(hardware);
            }
        }
        finally
        {
            _syncing = false;
        }
        RefreshSliderLabels();
        UpdateOverrideWarnings();
        InferenceStatusText.Text = Loc.T("settings.inference.loaded",
            "✅ 已读取当前参数（点「保存」后写入 config.json）");
    }

    /// <summary>
    /// [EN] Render the hardware diagnostics panel and remember the recommendation.
    /// [ZH] 渲染硬件诊断面板并记住推荐值。
    /// </summary>
    private void ApplyHardwareInfo(JsonElement hardware)
    {
        int physical = 0, logical = 0;
        if (hardware.TryGetProperty("cpu", out JsonElement cpu))
        {
            physical = GetInt(cpu, "physical_cores");
            logical = GetInt(cpu, "logical_cores");
        }
        HardwareCpuText.Text = Loc.T("settings.inference.hardwareCpu",
            "🧠 CPU：{0} 物理核 / {1} 逻辑核", physical, logical);

        bool gpuAvailable = false;
        string gpuName = "";
        long totalVram = 0;
        if (hardware.TryGetProperty("gpu", out JsonElement gpu))
        {
            gpuAvailable = GetBool(gpu, "available");
            gpuName = GetString(gpu, "name");
            totalVram = GetLong(gpu, "total_vram_bytes");
            string label = GetString(gpu, "label");
            HardwareGpuText.Text = gpuAvailable
                ? Loc.T("settings.inference.hardwareGpu",
                    "🎮 GPU：{0}（{1}，显存 {2}）", gpuName, label, FormatBytes(totalVram))
                : Loc.T("settings.inference.hardwareGpuCpu",
                    "🎮 GPU：未探测到可用加速器，使用 CPU 推理（{0}）",
                    gpuName.Length > 0 ? gpuName : label);
        }

        long ramTotal = 0;
        if (hardware.TryGetProperty("ram", out JsonElement ram))
            ramTotal = GetLong(ram, "total_bytes");
        HardwareRamText.Text = Loc.T("settings.inference.hardwareRam",
            "💾 系统内存：{0}", FormatBytes(ramTotal));

        int native = 0;
        if (hardware.TryGetProperty("model", out JsonElement model))
            native = GetInt(model, "native_context");
        HardwareNativeText.Text = Loc.T("settings.inference.hardwareNative",
            "📄 模型原生窗口：{0} tokens", native);

        int budget = 0, recNctx = 0, recGpu = 0, recBatch = 0;
        string recProfile = "";
        if (hardware.TryGetProperty("recommended", out JsonElement recommended))
        {
            recNctx = GetInt(recommended, "n_ctx");
            recGpu = GetInt(recommended, "n_gpu_layers");
            recBatch = GetInt(recommended, "n_batch");
            budget = GetInt(recommended, "hardware_budget");
            recProfile = GetString(recommended, "profile");
        }
        _recommendedNctx = recNctx;
        _recommendedGpuLayers = recGpu;
        _recommendedBatch = recBatch;
        _recommendedGpuAvailable = gpuAvailable;
        _hardwareBudget = budget;
        HardwareBudgetText.Text = Loc.T("settings.inference.hardwareBudget",
            "📐 硬件预算上限：{0} tokens", budget);
        HardwareRecommendedText.Text = Loc.T("settings.inference.hardwareRecommended",
            "⭐ 推荐：n_ctx {0} · n_gpu_layers {1} · n_batch {2} · 档位 {3}",
            recNctx, recGpu, recBatch, recProfile);

        if (hardware.TryGetProperty("warnings", out JsonElement warnings)
            && warnings.ValueKind == JsonValueKind.Array)
        {
            _backendWarnings.Clear();
            foreach (JsonElement warning in warnings.EnumerateArray())
            {
                if (warning.ValueKind == JsonValueKind.String)
                    _backendWarnings.Add(warning.GetString() ?? "");
            }
        }
    }

    /// <summary>
    /// [EN] Fill the editable inputs: the recommendation when no override is
    /// [EN] active, otherwise the user's persisted request.
    /// [ZH] 填充可编辑输入框：未启用覆盖时填推荐值，否则填用户已保存的请求值。
    /// </summary>
    private void ApplyHardwareInputs(JsonElement hardware)
    {
        bool overrideActive = false;
        int requestedNctx = _recommendedNctx;
        int requestedGpu = _recommendedGpuLayers;
        if (hardware.TryGetProperty("requested", out JsonElement requested))
        {
            overrideActive = GetBool(requested, "override");
            if (overrideActive)
            {
                requestedNctx = GetInt(requested, "n_ctx");
                requestedGpu = GetInt(requested, "n_gpu_layers");
            }
        }
        _syncing = true;
        try
        {
            NctxBox.Text = requestedNctx.ToString();
            GpuLayersBox.Text = requestedGpu.ToString();
        }
        finally
        {
            _syncing = false;
        }
    }

    private string CurrentPreset() => (PresetCombo.SelectedItem as Option)?.Value ?? "balanced";
    private string CurrentInferenceProfile() =>
        (InferenceProfileCombo.SelectedItem as Option)?.Value ?? "balanced";
    private string CurrentContextMode() => (ContextModeCombo.SelectedItem as Option)?.Value ?? "summarize";

    /// <summary>
    /// [EN] Rendered context description, retained so that a language switch can
    /// re-render it without another backend round trip.
    /// [ZH] 已渲染的上下文描述，保留它是为了让语言切换时无需再次请求后端即可重新渲染。
    /// </summary>
    private string _contextWindowText = "";

    /// <summary>
    /// [EN] Render the native-versus-usable context description from the backend
    /// snapshot. The explanatory sentence is produced by the backend because only
    /// the backend knows which constraint (native window, hardware memory,
    /// configuration or preset) determined the final window; the interface must
    /// not re-derive it from numbers and risk naming the wrong cause.
    /// [ZH] 依据后端快照渲染「原生上下文 / 实际可用」描述。解释语句由后端生成，因为只有
    /// [ZH] 后端知道最终窗口由哪种约束（原生窗口、硬件内存、配置或预设）决定；界面不得
    /// [ZH] 从数值自行推导，以免把原因说错。
    /// </summary>
    private void ApplyInferenceInfo(JsonElement inference)
    {
        bool ready = GetBool(inference, "ready");
        int native = GetInt(inference, "n_ctx_native");
        int usable = GetInt(inference, "n_ctx_max");
        if (usable <= 0)
            usable = GetInt(inference, "n_ctx");
        int batch = GetInt(inference, "n_batch");
        string limit = GetString(inference, "context_limit_text");
        string text;
        if (!ready)
        {
            text = Loc.T("settings.inference.contextNoModel",
                "尚未加载模型：原生上下文与实际可用上下文将在模型就绪后显示。");
        }
        else
        {
            text = native > 0
                ? Loc.T("settings.inference.contextNativeUsable",
                    "原生上下文 {0} tokens · 实际可用 {1} tokens · 批大小 {2}",
                    native, usable, batch)
                : Loc.T("settings.inference.contextUsableOnly",
                    "实际可用 {0} tokens · 批大小 {1}", usable, batch);
            if (limit.Length > 0)
                text += " · " + limit;
        }
        _contextWindowText = text;
        ContextWindowText.Text = text;
        _backendWarnings.Clear();
        if (inference.ValueKind == JsonValueKind.Object
            && inference.TryGetProperty("warnings", out JsonElement warningList)
            && warningList.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement warning in warningList.EnumerateArray())
                if (warning.ValueKind == JsonValueKind.String)
                    _backendWarnings.Add(warning.GetString() ?? "");
        }
        UpdateOverrideWarnings();
    }

    /// <summary>
    /// [EN] Save. **Sends every value** on the panel in full (rather than only the changed
    /// [EN] ones): the backend writes whatever it receives, so sending everything in full
    /// [EN] makes the interface the single source of truth, and the hidden drift where "a
    /// [EN] change was not sent once and is then missed by every later save" cannot occur.
    /// [ZH] 保存。**全量发送**面板上的每一个值（而不是只发改动过的）：
    /// [ZH] 后端按"收到什么写什么"处理，全量发送让界面成为唯一事实来源，
    /// [ZH] 不会出现"某次改动没发出去、之后每次保存都把它漏掉"的隐性偏差。
    /// </summary>
    /// <summary>
    /// [EN] Read the editable override inputs, returning false when either box is
    /// [EN] not a valid integer.
    /// [ZH] 读取可编辑的覆盖输入；任一输入框不是合法整数时返回 false。
    /// </summary>
    private (int Nctx, int GpuLayers, bool Ok) ReadOverrideInputs()
    {
        bool ctxOk = int.TryParse((NctxBox.Text ?? "").Trim(), out int nctx);
        bool gpuOk = int.TryParse((GpuLayersBox.Text ?? "").Trim(), out int gpu);
        return (nctx, gpu, ctxOk && gpuOk && nctx >= 512 && gpu >= -1);
    }

    /// <summary>
    /// [EN] Show the over-budget warnings beside the inputs without blocking the
    /// [EN] save path. The tooltip carries the concrete numbers; the inline line
    /// [EN] keeps the same text visible for keyboard and screen-reader users.
    /// [ZH] 在输入框旁显示超预算警告，但不阻断保存。悬停提示给出具体数值；行内文本
    /// [ZH] 保留同样内容，方便键盘与读屏用户。
    /// </summary>
    private void UpdateOverrideWarnings()
    {
        if (_syncing)
            return;
        var (nctx, gpu, ok) = ReadOverrideInputs();
        var warnings = new List<string>();
        if (!ok)
        {
            warnings.Add(Loc.T("settings.inference.overrideInvalid",
                "⚠️ 请输入整数：n_ctx ≥ 512，n_gpu_layers ≥ -1"));
        }
        else
        {
            if (_hardwareBudget > 0 && nctx > _hardwareBudget)
            {
                warnings.Add(Loc.T("settings.inference.warnContextOverBudget",
                    "⚠️ n_ctx {0} 超出硬件预算 {1} tokens；保存后会按你的选择尝试加载，失败时会明确报错。",
                    nctx, _hardwareBudget));
            }
            if (gpu != 0 && !_recommendedGpuAvailable)
            {
                warnings.Add(Loc.T("settings.inference.warnGpuUnavailable",
                    "⚠️ 本机未探测到可用 GPU 加速器；n_gpu_layers={0} 会在加载时失败。", gpu));
            }
            else if (gpu > 0 && _recommendedGpuLayers >= 0 && gpu > _recommendedGpuLayers)
            {
                warnings.Add(Loc.T("settings.inference.warnGpuLayersOver",
                    "⚠️ n_gpu_layers {0} 高于安全推荐值 {1}；可能显存不足。",
                    gpu, _recommendedGpuLayers));
            }
        }
        foreach (string warning in _backendWarnings)
        {
            if (!warnings.Contains(warning))
                warnings.Add(warning);
        }
        string ctxWarning = warnings.Count > 0 ? string.Join("\n", warnings) : "";
        string gpuWarning = warnings.Count > 0 ? string.Join("\n", warnings) : "";
        NctxWarningIcon.IsVisible = !ok || (warnings.Count > 0);
        GpuLayersWarningIcon.IsVisible = !ok || (warnings.Count > 0);
        IBrush? warningBrush = TryBrush("WarningBrush");
        NctxWarningIcon.Foreground = warningBrush ?? Brushes.Goldenrod;
        GpuLayersWarningIcon.Foreground = warningBrush ?? Brushes.Goldenrod;
        ToolTip.SetTip(NctxWarningIcon, ctxWarning);
        ToolTip.SetTip(GpuLayersWarningIcon, gpuWarning);
        OverrideWarningsText.Text = string.Join("\n", warnings);
        OverrideWarningsText.IsVisible = warnings.Count > 0;
        OverrideWarningsText.Foreground = warningBrush ?? Brushes.Goldenrod;
    }

    /// <summary>
    /// [EN] Error dialog required by v0.2.4 task 3: a failed override must be
    /// [EN] shown, never swallowed. The raw backend error and its suggestions are
    /// [EN] displayed verbatim on separate lines.
    /// [ZH] v0.2.4 任务-3 要求的错误对话框：覆盖失败必须展示，绝不吞掉。后端原始
    /// [ZH] 错误与其建议分行原样显示。
    /// </summary>
    private async Task ShowErrorDialogAsync(string title, string message)
    {
        Window? owner = TopLevel.GetTopLevel(this) as Window;
        var dialog = new Window
        {
            Title = title,
            Width = 560,
            Height = 300,
            CanResize = false,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
        };
        var text = new TextBlock
        {
            Text = message,
            TextWrapping = TextWrapping.Wrap,
            FontSize = 12,
            Margin = new Thickness(0, 0, 0, 10),
        };
        var ok = new Button
        {
            Content = Loc.T("settings.inference.dialogOk", "确定"),
            HorizontalAlignment = HorizontalAlignment.Right,
            Padding = new Thickness(18, 4),
        };
        ok.Click += (_, _) => dialog.Close();
        dialog.Content = new StackPanel
        {
            Margin = new Thickness(18, 16),
            Spacing = 8,
            Children =
            {
                new TextBlock { Text = title, FontSize = 15, FontWeight = FontWeight.Bold },
                text,
                ok,
            },
        };
        if (owner is not null)
            await dialog.ShowDialog(owner);
        else
            dialog.Show();
    }

    /// <summary>
    /// [EN] "Test load": ask the backend to try the current inputs in a child
    /// [EN] process with a 60-second timeout. A failure is rendered in a dialog.
    /// [ZH]「测试加载」：让后端在子进程中用当前输入尝试加载，超时 60 秒。失败时
    /// [ZH] 以对话框展示。
    /// </summary>
    private async Task TestLoadInferenceAsync()
    {
        var (nctx, gpu, ok) = ReadOverrideInputs();
        if (!ok)
        {
            await ShowErrorDialogAsync(
                Loc.T("settings.inference.testLoadFailedTitle", "测试加载失败"),
                Loc.T("settings.inference.overrideInvalid",
                    "⚠️ 请输入整数：n_ctx ≥ 512，n_gpu_layers ≥ -1"));
            return;
        }
        InferenceTestLoadButton.IsEnabled = false;
        InferenceStatusText.Text = Loc.T("settings.inference.testingLoad",
            "⏳ 正在用当前参数测试加载（最多 60 秒）…");
        try
        {
            using JsonDocument? doc = await SendAsync(new
            {
                action = "capability_settings",
                test_load = true,
                n_ctx = nctx,
                n_gpu_layers = gpu,
                inference_profile = CurrentInferenceProfile(),
            }, TimeSpan.FromSeconds(75));
            if (doc is null)
            {
                InferenceStatusText.Text = Loc.T("settings.inference.testLoadFailed",
                    "❌ 测试加载失败：{0}", Loc.T("main.error.noResponse", "无响应"));
                return;
            }
            JsonElement result = doc.RootElement.TryGetProperty("test_load", out JsonElement test)
                ? test : default;
            bool passed = result.ValueKind == JsonValueKind.Object && GetBool(result, "ok");
            if (passed)
            {
                InferenceStatusText.Text = Loc.T("settings.inference.testLoadOk",
                    "✅ 测试加载成功（{0} 秒）", GetDouble(result, "elapsed_seconds", 0));
                _backendWarnings.Clear();
                UpdateOverrideWarnings();
                return;
            }
            string error = result.ValueKind == JsonValueKind.Object
                ? GetString(result, "error") : "";
            var suggestions = new List<string>();
            if (result.ValueKind == JsonValueKind.Object
                && result.TryGetProperty("suggestions", out JsonElement list)
                && list.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement item in list.EnumerateArray())
                    if (item.ValueKind == JsonValueKind.String)
                        suggestions.Add(item.GetString() ?? "");
            }
            string message = error.Length > 0
                ? error
                : Loc.T("settings.inference.testLoadFailed",
                    "❌ 测试加载失败：{0}", Loc.T("settings.inference.unknownError", "未知原因"));
            if (suggestions.Count > 0)
                message += "\n\n" + string.Join("\n", suggestions);
            InferenceStatusText.Text = Loc.T("settings.inference.testLoadFailed",
                "❌ 测试加载失败：{0}", error);
            await ShowErrorDialogAsync(
                Loc.T("settings.inference.testLoadFailedTitle", "测试加载失败"), message);
        }
        finally
        {
            InferenceTestLoadButton.IsEnabled = true;
        }
    }

    private async Task SaveInferenceAsync()
    {
        if (_host is null || _syncing)
            return;
        var (nctx, gpu, inputsOk) = ReadOverrideInputs();
        if (!inputsOk)
        {
            string invalid = Loc.T("settings.inference.overrideInvalid",
                "⚠️ 请输入整数：n_ctx ≥ 512，n_gpu_layers ≥ -1");
            InferenceStatusText.Text = invalid;
            await ShowErrorDialogAsync(
                Loc.T("settings.inference.saveFailedTitle", "保存失败"), invalid);
            return;
        }
        bool allowOverride = nctx != _recommendedNctx || gpu != _recommendedGpuLayers;
        InferenceStatusText.Text = Loc.T("settings.inference.saving", "⏳ 正在保存…");
        // [EN] A preset or override change rebuilds the underlying model, which
        // [EN] may take minutes for a multi-gigabyte file. The idle timeout must
        // [EN] therefore exceed the model load time, otherwise a successful
        // [EN] rebuild would be reported as a failure.
        // [ZH] 预设或覆盖变更会重建底层模型，对于数 GB 的文件可能耗时数分钟。因此空闲
        // [ZH] 超时必须大于模型加载时间，否则一次成功的重建会被报告为失败。
        using JsonDocument? doc = await SendAsync(new
        {
            action = "capability_settings",
            preset = CurrentPreset(),
            inference_profile = CurrentInferenceProfile(),
            temperature = TempSlider.Value,
            top_p = TopPSlider.Value,
            repeat_penalty = RepeatSlider.Value,
            max_tokens = (int)Math.Round(MaxTokensSlider.Value),
            context_mode = CurrentContextMode(),
            clarify_vague = ClarifyVagueCheck.IsChecked == true,
            summary_cache = SummaryCacheCheck.IsChecked == true,
            n_ctx = nctx,
            n_gpu_layers = gpu,
            allow_override = allowOverride,
        }, TimeSpan.FromMinutes(10));
        if (doc is null)
        {
            string noResponse = Loc.T("settings.inference.saveFailed", "❌ 保存失败：{0}",
                Loc.T("main.error.noResponse", "无响应"));
            InferenceStatusText.Text = noResponse;
            await ShowErrorDialogAsync(
                Loc.T("settings.inference.saveFailedTitle", "保存失败"), noResponse);
            return;
        }

        bool responseOk = true;
        if (doc.RootElement.TryGetProperty("ok", out JsonElement okElement)
            && okElement.ValueKind == JsonValueKind.False)
            responseOk = false;
        JsonElement inference = default;
        if (doc.RootElement.TryGetProperty("inference", out JsonElement inferenceElement))
        {
            inference = inferenceElement;
            if (inferenceElement.ValueKind == JsonValueKind.Object
                && inferenceElement.TryGetProperty("ok", out JsonElement inferenceOk)
                && inferenceOk.ValueKind == JsonValueKind.False)
                responseOk = false;
        }
        if (!responseOk)
        {
            string error = GetString(inference, "error");
            if (error.Length == 0)
                error = GetString(doc.RootElement, "error");
            if (error.Length == 0)
                error = GetString(doc.RootElement, "content");
            var suggestions = new List<string>();
            foreach (JsonElement source in new[] { doc.RootElement, inference })
            {
                if (source.ValueKind == JsonValueKind.Object
                    && source.TryGetProperty("suggestions", out JsonElement list)
                    && list.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement item in list.EnumerateArray())
                        if (item.ValueKind == JsonValueKind.String)
                            suggestions.Add(item.GetString() ?? "");
                }
            }
            string message = error.Length > 0 ? error
                : Loc.T("settings.inference.unknownError", "未知原因");
            if (suggestions.Count > 0)
                message += "\n\n" + string.Join("\n", suggestions);
            InferenceStatusText.Text = Loc.T("settings.inference.saveFailed",
                "❌ 保存失败：{0}", error);
            await ShowErrorDialogAsync(
                Loc.T("settings.inference.saveFailedTitle", "保存失败"), message);
            if (doc.RootElement.TryGetProperty("hardware", out JsonElement hardware))
            {
                ApplyHardwareInfo(hardware);
                ApplyHardwareInputs(hardware);
            }
            if (inference.ValueKind == JsonValueKind.Object)
                ApplyInferenceInfo(inference);
            return;
        }
        if (doc.RootElement.TryGetProperty("hardware", out JsonElement savedHardware))
        {
            ApplyHardwareInfo(savedHardware);
            ApplyHardwareInputs(savedHardware);
        }
        if (inference.ValueKind == JsonValueKind.Object)
            ApplyInferenceInfo(inference);
        InferenceStatusText.Text = Loc.T("settings.inference.saved", "✅ 已保存");
        // [EN] Read back: the backend clamps to the legal range, so the UI must follow the
        // [EN] real values (quiet mode, so the message above is not overwritten).
        // [ZH] 回读：后端会裁剪到合法范围，界面要跟上真实值（quiet 模式，不覆盖上面的提示）。
        await LoadInferenceAsync();
        InferenceStatusText.Text = Loc.T("settings.inference.saved", "✅ 已保存");
    }

    private static string GetString(JsonElement item, string name) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out JsonElement el)
        && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? ""
            : "";

    private static bool GetBool(JsonElement item, string name) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out JsonElement el)
        && el.ValueKind == JsonValueKind.True;

    private static bool GetBoolOr(JsonElement item, string name, bool fallback) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out JsonElement el)
            ? el.ValueKind == JsonValueKind.True
            : fallback;

    private static int GetInt(JsonElement item, string name, int fallback) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out JsonElement el)
        && el.ValueKind == JsonValueKind.Number
            ? el.GetInt32()
            : fallback;

    private static int GetInt(JsonElement item, string name) => GetInt(item, name, 0);

    private static long GetLong(JsonElement item, string name) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out JsonElement el)
        && el.ValueKind == JsonValueKind.Number
            ? el.GetInt64()
            : 0;

    private static double GetDouble(JsonElement item, string name, double fallback) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out JsonElement el)
        && el.ValueKind == JsonValueKind.Number
            ? el.GetDouble()
            : fallback;

    private static string FormatBytes(long bytes)
    {
        if (bytes <= 0)
            return "0 MB";
        const double mib = 1024.0 * 1024.0;
        const double gib = 1024.0 * 1024.0 * 1024.0;
        return bytes >= gib
            ? $"{bytes / gib:0.0} GB"
            : $"{bytes / mib:0} MB";
    }

    /// <summary>
    /// [EN] Looks up a theme brush; returns null when the theme has no such key (controls
    /// [EN] then use the default foreground color and do not crash).
    /// [ZH] 取主题画刷；主题里没有这个键时返回 null（控件用默认前景色，不崩）。
    /// </summary>
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
        InferenceProfileLabel.Text = Loc.T("settings.inference.profileLabel", "推理档位");
        ContextModeLabel.Text = Loc.T("settings.inference.contextMode", "上下文策略");
        NctxLabel.Text = Loc.T("settings.inference.nctxLabel", "上下文 n_ctx");
        GpuLayersLabel.Text = Loc.T("settings.inference.gpuLayersLabel", "GPU 层数");
        OverrideHintText.Text = Loc.T("settings.inference.overrideHint",
            "默认填推荐值；修改后按你的选择加载，超限只警告、不阻止保存。");
        HardwareDiagnosticsTitle.Text = Loc.T("settings.inference.hardwareTitle",
            "🔍 硬件诊断（探测结果与推荐值）");
        ClarifyVagueCheck.Content = Loc.T("settings.inference.clarifyVague", "模糊问题先追问");
        SummaryCacheCheck.Content = Loc.T("settings.inference.summaryCache", "缓存分段摘要");
        InferenceSaveButton.Content = Loc.T("settings.inference.save", "保存");
        InferenceReloadButton.Content = Loc.T("settings.inference.reload", "🔄 重新读取");
        InferenceResetButton.Content = Loc.T("settings.inference.reset", "恢复默认");
        InferenceTestLoadButton.Content = Loc.T("settings.inference.testLoad", "🧪 测试加载");

        // [EN] Local model library (v0.2.2, batch 2). The title is rewritten as data arrives
        // [EN] ("(N items · size)"); the only guarantee here is that "it is not empty even
        // [EN] before any data has been read". The text of both buttons is static.
        // [ZH] 本地模型库（v0.2.2 第 2 批）。标题会随数据重写（"（N 个 · 大小）"），
        // [ZH] 这里只保证"还没读到数据时它也不是空的"；两个按钮的文案是静态的。
        HubRegistryTitle.Text = Loc.T("settings.model.localRegistryTitle", "本地模型库");
        HubRescanButton.Content = Loc.T("settings.model.actionRescan", "重新扫描模型文件夹");
        HubRegistryRefreshButton.Content = Loc.T("settings.model.actionRefreshRegistry", "刷新本地模型库");

        MarketNotMigratedText.Text = "";

        // [EN] The drop-down item text is a delegate, so after rebuilding the item is
        // [EN] selected again by value.
        // [ZH] 下拉项文案是委托，重建后按值选回。
        string? preset = (PresetCombo.SelectedItem as Option)?.Value;
        string? profile = (InferenceProfileCombo.SelectedItem as Option)?.Value;
        string? mode = (ContextModeCombo.SelectedItem as Option)?.Value;
        BuildOptions();
        Select(PresetCombo, preset);
        Select(InferenceProfileCombo, profile);
        Select(ContextModeCombo, mode);
        ContextWindowText.Text = _contextWindowText.Length > 0
            ? _contextWindowText
            : Loc.T("settings.inference.contextNoModel",
                "尚未加载模型：原生上下文与实际可用上下文将在模型就绪后显示。");
        RefreshSliderLabels();
        Market.ApplyTexts();
    }

    // [EN] ---------- (3) Model market (completed in this round, v0.2.2) ----------
    // [ZH] ---------- ③ 模型市场（本轮补全，v0.2.2） ----------

    // [EN] ---------- Probe interface (`EchoDesk --probe market`) ----------
    // [ZH] ---------- 探针接口（`EchoDesk --probe market` 用：无人值守验整条市场交互） ----------

    /// <summary>
    /// [EN] Observable state snapshot of the market section (see
    /// [EN] ModelMarketSection.Snapshot).
    /// [ZH] 市场区块的可观测状态快照（见 ModelMarketSection.Snapshot）。
    /// </summary>
    public ModelMarketSection.MarketState MarketSnapshot() => Market.Snapshot();

    public Task ProbeMarketSearchAsync() => Market.ProbeSearchAsync();
    public Task ProbeMarketFilterAsync(string keyword) => Market.ProbeFilterAsync(keyword);
    public Task ProbeMarketToggleFacetAsync(int index) => Market.ProbeToggleFacetAsync(index);
    public Task ProbeMarketGoToPageAsync(int page) => Market.ProbeGoToPageAsync(page);
    public Task ProbeMarketSetPageSizeAsync(int size) => Market.ProbeSetPageSizeAsync(size);
    public Task ProbeMarketResetAsync() => Market.ProbeResetFiltersAsync();
    public Task ProbeMarketOpenPinnedDetailAsync() => Market.ProbeOpenPinnedDetailAsync();

    /// <summary>
    /// [EN] Probe: fills in demonstration data for the model market (for README screenshots,
    /// [EN] optionally in English).
    /// [ZH] 探针：填入模型市场演示数据（README 截图用，可选英文）。
    /// </summary>
    public void ProbeShowDemoMarket(bool english = false) => Market.ProbeShowDemo(english);

    // [EN] ---------- Probe interface (`EchoDesk --probe pages`: model library) ----------
    // [ZH] ---------- 探针接口（`EchoDesk --probe pages` 用：本地模型库那一块） ----------

    /// <summary>
    /// [EN] Observable state of the local model library (row count / empty state / whether
    /// [EN] both buttons have text).
    /// [ZH] 本地模型库的可观测状态（行数 / 空状态 / 两个按钮有没有文案）。
    /// </summary>
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

    /// <summary>
    /// [EN] Probe: rescans the model folder and re-reads the model library (through the real
    /// [EN] backend actions).
    /// [ZH] 探针：重扫模型文件夹 + 重读模型库（走真实后端动作）。
    /// </summary>
    public async Task ProbeRescanAsync() => await RescanModelsAsync();

    /// <summary>
    /// [EN] Probe: re-reads the model library only (no disk rescan).
    /// [ZH] 探针：只重读模型库（不重扫磁盘）。
    /// </summary>
    public async Task ProbeReloadRegistryAsync() => await LoadRegistryAsync();

    // [EN] ---------- Probe interface (`EchoDesk --probe pages`: presets, context) ----------
    // [ZH] ---------- 探针接口（`EchoDesk --probe pages` 用：推理预设与上下文描述） ----------

    /// <summary>
    /// [EN] Observable state of the inference preset controls: number of options,
    /// selected identifier and whether the context description was rendered.
    /// [ZH] 推理预设控件的可观测状态：选项数量、当前选中标识以及上下文描述是否已渲染。
    /// </summary>
    public string InferenceSnapshot()
    {
        int options = InferenceProfileCombo.ItemsSource is IEnumerable<Option> items
            ? System.Linq.Enumerable.Count(items)
            : 0;
        string selected = CurrentInferenceProfile();
        bool contextText = !string.IsNullOrWhiteSpace(ContextWindowText.Text);
        bool profileLabel = !string.IsNullOrWhiteSpace(InferenceProfileLabel.Text);
        int visibleWarnings = (NctxWarningIcon.IsVisible ? 1 : 0)
                              + (GpuLayersWarningIcon.IsVisible ? 1 : 0);
        return string.Join("; ", new[]
        {
            $"profileOptions={options}",
            $"profileSelected={selected}",
            $"contextText={(contextText ? 1 : 0)}",
            $"profileLabel={(profileLabel ? 1 : 0)}",
            $"saveButton={(string.IsNullOrEmpty(InferenceSaveButton.Content as string) ? 0 : 1)}",
            $"nctxField={(string.IsNullOrEmpty(NctxBox.Text) ? 0 : 1)}",
            $"gpuLayersField={(string.IsNullOrEmpty(GpuLayersBox.Text) ? 0 : 1)}",
            $"testLoadButton={(string.IsNullOrEmpty(InferenceTestLoadButton.Content as string) ? 0 : 1)}",
            $"diagnosticsPanel={(HardwareDiagnosticsExpander is null ? 0 : 1)}",
            $"warnIcons={visibleWarnings}",
            $"recommendedNctx={_recommendedNctx}",
            $"recommendedGpuLayers={_recommendedGpuLayers}",
        });
    }

    /// <summary>
    /// [EN] Probe entry point: put deliberately over-budget values into the
    /// [EN] editable inputs and report the resulting warning state. The probe
    /// [EN] never clicks "Test load", so it cannot block for 60 seconds.
    /// [ZH] 探针入口：向可编辑输入框填入故意超预算的值并上报警告状态。探针不会点击
    /// [ZH]「测试加载」，因此不会阻塞 60 秒。
    /// </summary>
    public string ProbeOverrideSnapshot()
    {
        _syncing = true;
        NctxBox.Text = "999999";
        GpuLayersBox.Text = "99";
        _syncing = false;
        UpdateOverrideWarnings();
        return InferenceSnapshot();
    }

    /// <summary>
    /// [EN] Probe entry point: re-read the inference settings from the backend.
    /// [ZH] 探针入口：从后端重新读取推理设置。
    /// </summary>
    public async Task ProbeReloadInferenceAsync() => await LoadInferenceAsync();

    /// <summary>
    /// [EN] The market section (many controls, so it is a separate UserControl; this page
    /// [EN] decides "who sends the requests").
    /// [ZH] 市场区块（控件多，单独一个 UserControl；由这里负责"谁来发请求"）。
    /// </summary>
    private ModelMarketSection? _market;

    private ModelMarketSection Market => _market ??= CreateMarket();

    private ModelMarketSection CreateMarket()
    {
        var section = new ModelMarketSection();
        // [EN] Request sending is delegated to this page (which already holds the backend
        // [EN] connection); the market itself touches nothing global beyond AppState.
        // [ZH] 把"怎么发请求"交给这一页（页面已经持有后端连接），市场本身不碰 AppState 之外的全局
        section.Attach(new MarketHost(this));
        MarketHostGrid.Children.Add(section);
        Grid.SetRow(section, 0);
        return section;
    }

    /// <summary>
    /// [EN] The bridge between the market section and the page: it exposes only the three
    /// [EN] things the section really needs.
    /// [ZH] 市场区块与页面之间的桥：只暴露它真正需要的三件事。
    /// </summary>
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



