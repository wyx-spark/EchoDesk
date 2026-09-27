using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using EchoDesk.Services;
using EchoDesk.Utilities;

namespace EchoDesk;

// ===========================================================================
// 设置窗口：v0.2.1 新增部分
//   * 语言选择与语言包管理（任务三）
//   * 自动化字段级帮助 / 模板库 / 可视化选取 / 测试向导 / 配置导入导出（任务四）
//   * 「已安装」Tab：插件清单、加载状态、命令、来源、错误详情、卸载、导入导出（任务二 2.6 / 2B.7-2B.8）
//   * 「开发者」Tab：开发者模式、目录、重载、日志、脚手架、清单编辑、钩子与面板（任务 2B.10）
//
// 放在单独的 partial 文件里，避免把既有 1400 行的 SettingsWindow.xaml.cs 继续撑大。
// ===========================================================================

/// <summary>「已安装」Tab 的插件卡片。</summary>
public sealed class SkillCardItem
{
    public string Id { get; init; } = "";
    public string Name { get; init; } = "";
    public string Version { get; init; } = "";
    public string Source { get; init; } = "";
    public string Path { get; init; } = "";
    public bool Loaded { get; init; }
    public bool Enabled { get; init; } = true;
    public bool Dev { get; init; }
    public string Error { get; init; } = "";
    public string SourceTag { get; init; } = "";
    public string StatusText { get; init; } = "";
    public Brush StatusBrush { get; init; } = Brushes.Gray;
    public string Meta { get; init; } = "";
    public string Capabilities { get; init; } = "";
    public string Permissions { get; init; } = "";
    public string Overrode { get; init; } = "";
    public Visibility ErrorVisibility => Error.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility OverrodeVisibility => Overrode.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
}

/// <summary>「开发者」Tab 本地插件清单的一行（可编辑）。</summary>
public sealed class ManifestRow
{
    public string Id { get; set; } = "";
    public string Path { get; set; } = "";
    public string Alias { get; set; } = "";
    public bool Enabled { get; set; } = true;
    public int Order { get; set; }
    public string Note { get; set; } = "";
}

/// <summary>开发者 Tab 里渲染的插件面板 Tab（由技能的声明生成原生控件）。</summary>
public sealed class PluginPanelItem
{
    public string SkillId { get; init; } = "";
    public string PanelId { get; init; } = "";
    public string Title { get; init; } = "";
    public string Description { get; init; } = "";
    public List<PluginPanelField> Fields { get; init; } = new();
    public List<(string Key, string Label)> Actions { get; init; } = new();
}

/// <summary>自动化配置模板（任务四 4.4）：内容全部来自语言包，随界面语言切换。</summary>
public sealed class TemplateItem
{
    public string Id { get; init; } = "";
    public string Label { get; init; } = "";
    public string Instruction { get; init; } = "";
    public string Extensions { get; init; } = "";
}

/// <summary>插件面板里的一个表单字段。</summary>
public sealed class PluginPanelField
{
    public string Key { get; init; } = "";
    public string Type { get; init; } = "text";
    public string Label { get; init; } = "";
    public string Help { get; init; } = "";
    public string Default { get; init; } = "";
}

public partial class SettingsWindow
{
    // ---------- 状态 ----------
    private JsonElement? _skillStatus;                       // skills_list 返回的完整状态
    private readonly Dictionary<string, JsonElement> _fieldHelp = new(StringComparer.Ordinal);
    private readonly List<ManifestRow> _manifestRows = new();
    private readonly List<PluginPanelItem> _panelItems = new();
    private readonly Dictionary<string, TextBox> _panelFields = new(StringComparer.Ordinal);
    private readonly List<string> _templateIds = new();
    private bool _devModeSyncing;
    private int _logLineCount = 200;
    // 修复一：批量确认框的一次性标志（防重入 + 同一窗口只问一次）
    private bool _trustDialogOpen;
    private bool _trustDialogHandled;

    // ======================= 语言（任务三 3.4） =======================

    /// <summary>填充语言下拉框（只列实际有语言包的语言 + 自动）。</summary>
    private void InitLanguageUi()
    {
        LanguageCombo.Items.Clear();
        LanguageCombo.Items.Add(new ComboBoxItem
        {
            Content = Loc.T("settings.general.languageAuto", "自动（跟随系统区域）"),
            Tag = "auto",
        });
        foreach (string locale in Loc.AvailableLocales())
            LanguageCombo.Items.Add(new ComboBoxItem { Content = Loc.DisplayName(locale), Tag = locale });

        string wanted = _uiState.Language.Length > 0 ? _uiState.Language : "auto";
        foreach (object item in LanguageCombo.Items)
        {
            if (item is ComboBoxItem combo && string.Equals((string?)combo.Tag, wanted, StringComparison.OrdinalIgnoreCase))
            {
                LanguageCombo.SelectedItem = combo;
                break;
            }
        }
        LanguageCombo.SelectedItem ??= LanguageCombo.Items.Count > 0 ? LanguageCombo.Items[0] : null;
        UpdateLangPackText();
    }

    private void UpdateLangPackText()
    {
        if (LangPackText is null)
            return;
        string dir = Loc.LangDirectory;
        var available = Loc.AvailableLocales();
        LangPackText.Text =
            $"{Loc.T("settings.general.langCurrent", "当前语言")}: {Loc.DisplayName(Loc.Locale)} ({Loc.Locale})\n" +
            $"{Loc.T("settings.general.langAvailable", "可用语言")}: " +
            (available.Count > 0 ? string.Join(" / ", available.Select(Loc.DisplayName))
                                 : Loc.T("settings.general.langNone", "（未找到语言包）")) + "\n" +
            $"{Loc.T("settings.general.langDir", "语言包目录")}: {dir}";
    }

    private async void LanguageCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || LanguageCombo.SelectedItem is not ComboBoxItem item)
            return;
        string tag = (string?)item.Tag ?? "auto";
        _uiState.Language = tag;
        _uiState.Save();
        string effective = Loc.SetLocale(tag, persist: false);   // 已由上面写盘，避免重复读写
        UpdateLangPackText();
        ApplyLanguage();
        // 后端同步（任务三 3.4）：系统消息、错误提示与技能多语言资源都要跟着切
        await SyncBackendLanguageAsync(effective);
    }

    /// <summary>通知后端切换语言，并把返回的本地化指令表交给主窗口重建指令面板。</summary>
    private async Task SyncBackendLanguageAsync(string locale)
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "set_language", locale });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("commands", out JsonElement commands))
                MainWindow.ApplyCommandsFromBackend(commands);
            SetLoadState(Loc.T("settings.general.languageRestartBackend", "后端语言已同步"));
        }
        catch (Exception ex)
        {
            SetLoadState($"⚠️ {ex.Message}");
        }
    }

    private void ReloadLangButton_Click(object sender, RoutedEventArgs e)
    {
        Loc.Reload();
        UpdateLangPackText();
        ApplyLanguage();
        SetLoadState(Loc.T("settings.general.langReloaded", "✅ 语言包已重新加载"));
    }

    private void OpenLangDirButton_Click(object sender, RoutedEventArgs e) => OpenFolder(Loc.LangDirectory);

    /// <summary>语言切换后重跑一遍：回填 XAML 文案 + 重建那些内容由 Loc.T 生成的列表。</summary>
    internal void ApplyLanguage()
    {
        Loc.Apply(this);
        Title = Loc.T("settings.title");
        SafetyText.Text = DefaultSafetyBody;
        RefreshModeCombo();
        PopulateSortCombos();
        InitThemeControls();     // 主题项的名称也是语言包文本
        InitEmojiControls();     // emoji 策略项同理
        PathsText.Text = DescribePaths();
        UpdateResultsPreview();
        RenderSkillCards();
        RenderManifestRows();
        RenderDevDirs();
        RenderHooks();
        RenderPanels();
        UpdateFieldHelpPanel(null);
    }

    /// <summary>安全协议正文：正式文本来自语言包，这里给中文兜底（键缺失时使用）。</summary>
    private string DefaultSafetyBody => Loc.T("settings.automation.safetyBody",
        "1. 自动化功能仅在本机运行，操作过程使用有头浏览器，你可随时观察；\n2. EchoDesk 不会把任务内容、下载文件或生成结果上传到任何服务器；\n3. 已下载的附件会保留在结果目录中，便于你复核，请在共享前自行确认敏感信息；\n4. 你需要自行确认该自动化操作符合所在组织的规章制度与目标网站的服务条款；\n5. 因违规使用导致的后果由使用者自行承担，Void Entropia Studio 不承担责任。");

    private void RefreshModeCombo()
    {
        if (DefaultModeCombo is null)
            return;
        string? current = (DefaultModeCombo.SelectedItem as ComboBoxItem)?.Tag as string;
        DefaultModeCombo.Items.Clear();
        foreach (string key in Modes)
        {
            DefaultModeCombo.Items.Add(new ComboBoxItem
            {
                Content = Loc.T("mode." + key, key),
                Tag = key,
            });
        }
        SelectMode(current ?? "general");
    }

    private void PopulateSortCombos()
    {
        if (MarketSortCombo is not null && MarketSortCombo.Items.Count >= 3)
        {
            ((ComboBoxItem)MarketSortCombo.Items[0]).Content = Loc.T("settings.market.sortBest", "最佳匹配");
            ((ComboBoxItem)MarketSortCombo.Items[1]).Content = Loc.T("settings.market.sortStars", "Star 最多");
            ((ComboBoxItem)MarketSortCombo.Items[2]).Content = Loc.T("settings.market.sortUpdated", "最近更新");
        }
        if (HubSortCombo is not null && HubSortCombo.Items.Count >= 4)
        {
            ((ComboBoxItem)HubSortCombo.Items[0]).Content = Loc.T("settings.model.sortDownloads", "下载量最多");
            ((ComboBoxItem)HubSortCombo.Items[1]).Content = Loc.T("settings.model.sortUpdated", "最近更新");
            ((ComboBoxItem)HubSortCombo.Items[2]).Content = Loc.T("settings.model.sortLikes", "点赞最多");
            ((ComboBoxItem)HubSortCombo.Items[3]).Content = Loc.T("settings.model.sortName", "名称");
        }
        if (HubSourceCombo is not null && HubSourceCombo.Items.Count >= 3)
        {
            ((ComboBoxItem)HubSourceCombo.Items[0]).Content = Loc.T("settings.model.sourceAuto", "自动（推荐）");
            ((ComboBoxItem)HubSourceCombo.Items[1]).Content = Loc.T("settings.model.sourceMirror", "镜像源 hf-mirror.com");
            ((ComboBoxItem)HubSourceCombo.Items[2]).Content = Loc.T("settings.model.sourceOfficial", "官方源 huggingface.co");
        }
    }

    private void OpenFolder(string? dir)
    {
        if (string.IsNullOrWhiteSpace(dir))
            return;
        try
        {
            Directory.CreateDirectory(dir);
            Process.Start(new ProcessStartInfo { FileName = dir, UseShellExecute = true });
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, Loc.T("settings.title"), MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    // ======================= 自动化：字段帮助与模板（任务四 4.3 / 4.4） =======================

    /// <summary>从后端拉取字段级帮助与模板库（内容随界面语言切换）。</summary>
    private async Task LoadAutomationHelpAsync()
    {
        try
        {
            string? helpJson = await RequestWithRetryAsync(
                new { action = "automation_field_help" },
                Loc.T("settings.automation.labelReadingFieldHelp", "读取填写帮助"), TimeSpan.FromSeconds(60));
            if (helpJson is not null)
            {
                using JsonDocument doc = JsonDocument.Parse(helpJson);
                if (doc.RootElement.TryGetProperty("help", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array)
                {
                    _fieldHelp.Clear();
                    foreach (JsonElement item in arr.EnumerateArray())
                    {
                        string field = GetString(item, "field");
                        if (field.Length > 0)
                            _fieldHelp[field] = item.Clone();
                    }
                }
            }
        }
        catch (Exception ex)
        {
            App.Log("SettingsWindow.LoadAutomationHelp", ex);
        }

        try
        {
            string? tplJson = await RequestWithRetryAsync(
                new { action = "automation_templates" },
                Loc.T("settings.automation.labelReadingTemplates", "读取配置模板"), TimeSpan.FromSeconds(60));
            if (tplJson is not null)
            {
                using JsonDocument doc = JsonDocument.Parse(tplJson);
                TemplateCombo.Items.Clear();
                if (doc.RootElement.TryGetProperty("templates", out JsonElement arr)
                    && arr.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement item in arr.EnumerateArray())
                        TemplateCombo.Items.Add(new TemplateItem
                        {
                            Id = GetString(item, "id"),
                            Label = GetString(item, "label"),
                            Instruction = GetString(item, "instruction"),
                            Extensions = GetString(item, "extensions"),
                        });
                }
                if (TemplateCombo.Items.Count > 0)
                    TemplateCombo.SelectedIndex = 0;
            }
        }
        catch (Exception ex)
        {
            App.Log("SettingsWindow.LoadTemplates", ex);
        }

        UpdateFieldHelpPanel(null);
    }

    /// <summary>把「?」按钮的悬停说明与运行参数标签的提示都填上。</summary>
    private void ApplyFieldHelpTooltips()
    {
        foreach (KeyValuePair<string, JsonElement> pair in _fieldHelp)
        {
            string help = GetString(pair.Value, "help");
            string example = GetString(pair.Value, "example");
            string tip = example.Length > 0
                ? $"{help}\n\n{Loc.T("automation.help.example", "示例")}: {example}"
                : help;
            foreach (Button button in FindTagged<Button>(this, pair.Key))
                button.ToolTip = tip;
        }
        // 运行参数等没有「?」按钮的字段：给标签本身挂 ToolTip
        AttachLabelTip(MaxTasksBox, "max_tasks");
        AttachLabelTip(TimeoutBox, "page_timeout_ms");
        AttachLabelTip(RetriesBox, "retries");
        AttachLabelTip(ResultsRootNameBox, "results_root_name");
        AttachLabelTip(ResultsLocationBox, "results_location");
        AttachLabelTip(ScheduleTimesBox, "schedule_times");
    }

    private void AttachLabelTip(FrameworkElement? target, string field)
    {
        if (target is null || !_fieldHelp.TryGetValue(field, out JsonElement help))
            return;
        target.ToolTip = GetString(help, "help");
    }

    /// <summary>
    /// 把内联帮助面板更新成某个字段的说明；field 为 null 时显示使用提示。
    ///
    /// 面板**始终可见**：折叠的控件既不利于用户发现这个功能，也不会出现在
    /// UI 自动化树里（v0.2.0 排查设置窗口时踩过同样的坑）。没有焦点字段时
    /// 显示一句"把光标放进任一输入框或点 ? "的提示即可。
    /// </summary>
    private void UpdateFieldHelpPanel(string? field)
    {
        if (FieldHelpPanel is null)
            return;
        if (field is null || !_fieldHelp.TryGetValue(field, out JsonElement help))
        {
            FieldHelpTitle.Text = Loc.T("settings.automation.helpPlaceholderTitle", "❓ 字段说明");
            FieldHelpBody.Text = Loc.T("settings.automation.helpPlaceholderBody",
                "把光标放进下面任意一个输入框（或点字段旁的「?」），这里会显示该字段的通俗说明与示例值。完整指南见发布包 docs\\<locale>\\AUTOMATION_GUIDE.md。");
            FieldHelpExample.Text = "";
            return;
        }
        FieldHelpTitle.Text = $"? {GetString(help, "label")}";
        FieldHelpBody.Text = GetString(help, "help");
        string example = GetString(help, "example");
        FieldHelpExample.Text = example.Length > 0
            ? $"{Loc.T("automation.help.example", "示例")}: {example}" : "";
    }

    private void FieldHelp_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button button)
            return;
        string field = (string?)button.Tag ?? "";
        if (!_fieldHelp.TryGetValue(field, out JsonElement help))
        {
            UpdateFieldHelpPanel(field);
            return;
        }
        string text = GetString(help, "help");
        string example = GetString(help, "example");
        MessageBox.Show(this,
            GetString(help, "label") + "\n\n" + text + (example.Length > 0 ? "\n\n" + example : ""),
            Loc.T("settings.automation.onlineHelp"), MessageBoxButton.OK, MessageBoxImage.Information);
    }

    private void AutomationField_GotFocus(object sender, RoutedEventArgs e) => UpdateFieldHelpPanel((string?)(sender as FrameworkElement)?.Tag);

    private void AutomationGuideButton_Click(object sender, RoutedEventArgs e)
    {
        var sb = new System.Text.StringBuilder();
        sb.AppendLine(Loc.T("settings.automation.guideTitle", "自动化配置填写帮助"));
        sb.AppendLine();
        string lastStep = "";
        foreach (JsonElement help in _fieldHelp.Values)
        {
            string step = GetString(help, "step");
            string stepTitle = GetString(help, "stepTitle");
            if (step != lastStep && stepTitle.Length > 0)
            {
                sb.AppendLine("── " + stepTitle + " ──");
                lastStep = step;
            }
            string required = GetBool(help, "required")
                ? Loc.T("automation.help.required", "必填") : Loc.T("automation.help.optional", "可选");
            sb.AppendLine(Loc.T("settings.automation.guideItem", "【{0}】（{1}）",
                GetString(help, "label"), required));
            sb.AppendLine("  " + GetString(help, "help"));
            string example = GetString(help, "example");
            if (example.Length > 0)
                sb.AppendLine($"  {Loc.T("automation.help.example", "示例")}: {example}");
            sb.AppendLine();
        }
        sb.AppendLine(Loc.T("automation.guide.hint",
            "也可以查看完整文档：docs/<locale>/AUTOMATION_GUIDE.md（发布包内 docs 目录）"));
        MessageBox.Show(this, sb.ToString(), Loc.T("settings.automation.onlineHelp"),
            MessageBoxButton.OK, MessageBoxImage.Information);
    }

    private void ApplyTemplateButton_Click(object sender, RoutedEventArgs e)
    {
        if (TemplateCombo.SelectedItem is not TemplateItem template)
            return;
        if (template.Instruction.Length > 0)
            InstructionBox.Text = template.Instruction;
        if (template.Extensions.Length > 0)
            ExtensionsBox.Text = template.Extensions;
        UpdateResultsPreview();
        SetLoadState(Loc.T("automation.template.custom_applied", "✅ 已套用模板「{0}」", template.Label));
    }

    private void SaveTemplateButton_Click(object sender, RoutedEventArgs e)
    {
        string? name = InputDialog.Show(this,
            Loc.T("settings.automation.saveTemplate"), Loc.T("settings.automation.saveTemplatePrompt", "模板名称"), "");
        if (string.IsNullOrWhiteSpace(name))
            return;
        _ = SaveTemplateAsync(name!, new
        {
            label = name,
            instruction = InstructionBox.Text,
            extensions = ExtensionsBox.Text,
        });
    }

    private async Task SaveTemplateAsync(string name, object template)
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "automation_save_template", name, template });
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = GetString(doc.RootElement, "content");
            SetLoadState(content);
            await LoadAutomationHelpAsync();
        }
        catch (Exception ex)
        {
            SetLoadState($"❌ {ex.Message}");
        }
    }

    // ======================= 可视化选取（任务四 4.2） =======================

    private async void PickSelectorButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button button)
            return;
        string target = (string?)button.Tag ?? "task_list";
        string url = TaskUrlBox.Text.Trim();
        if (url.Length == 0)
        {
            MessageBox.Show(this, Loc.T("automation.no_url"), Loc.T("settings.automation.pick"),
                MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        button.IsEnabled = false;
        string original = button.Content?.ToString() ?? "";
        button.Content = Loc.T("settings.automation.pickRunning", "⏳ 等待你在浏览器里点击目标元素（按 Esc 取消）…");
        SetLoadState(button.Content.ToString()!);
        try
        {
            string result = await _backend.SendStreamRequestAsync(
                new { action = "automation_pick", target, url },
                _ => { },
                null,
                cancellationToken: _closing.Token);
            using JsonDocument doc = JsonDocument.Parse(result);
            string selector = GetString(doc.RootElement, "selector");
            if (selector.Length > 0)
            {
                TextBox targetBox = target switch
                {
                    "task_row" => TaskRowBox,
                    "next_page" => NextPageBox,
                    "task_detail_file" => FileSelectorBox,
                    "login_check" => LoginCheckSelectorBox,
                    _ => TaskSelectorBox,
                };
                // 任务三：选取后预览 —— 把"程序将从这些元素提取以下网址：…"摆给用户看，
                // 让他确认选的是对的位置。这是"选取语义与程序逻辑对应"的界面落点：
                // 用户看到的预览就是程序接下来真正会用的地址列表。
                string previewText = BuildPickPreview(doc.RootElement);
                if (previewText.Length > 0 && !ConfirmPickPreview(previewText))
                {
                    SetLoadState(Loc.T("settings.automation.pickDiscarded",
                        "↩️ 已放弃这次选取（未修改配置），请重新点击目标元素。"));
                    return;
                }
                targetBox.Text = selector;
                SetLoadState(Loc.T("settings.automation.pickDone", "✅ 已填入选择器：{0}", selector));
                // 登录检测元素改变后，登录态描述里的"判据"也要跟着变
                if (target == "login_check")
                    await RefreshLoginStateAsync();
            }
            else
            {
                SetLoadState(GetString(doc.RootElement, "content"));
            }
        }
        catch (Exception ex)
        {
            SetLoadState($"❌ {ex.Message}");
        }
        finally
        {
            button.Content = original;
            button.IsEnabled = true;
        }
    }

    /// <summary>把后端回传的选取预览拼成给用户确认的文本（任务三）。</summary>
    private string BuildPickPreview(JsonElement root)
    {
        if (!root.TryGetProperty("preview", out JsonElement preview) || preview.ValueKind != JsonValueKind.Object)
            return "";
        var lines = new List<string>();
        string message = GetString(preview, "message");
        if (message.Length > 0)
            lines.Add(message);
        if (preview.TryGetProperty("samples", out JsonElement samples) && samples.ValueKind == JsonValueKind.Array)
        {
            var names = new List<string>();
            foreach (JsonElement item in samples.EnumerateArray())
            {
                string text = item.ToString();
                if (text.Length > 0)
                    names.Add(text);
            }
            if (names.Count > 0)
                lines.Add(Loc.T("settings.automation.pickPreviewSamples", "\n元素示例：{0}", string.Join(" / ", names)));
        }
        if (preview.TryGetProperty("warnings", out JsonElement warnings) && warnings.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in warnings.EnumerateArray())
            {
                string text = item.ToString();
                if (text.Length > 0)
                    lines.Add("⚠️ " + text);
            }
        }
        return string.Join("\n", lines).Trim();
    }

    /// <summary>
    /// 预览确认框：确认 = 采用这次选取；取消 = 不改配置（要求用户重新选）。
    /// 默认按钮是"取消"——被误选的选择器写进配置后，后续每一步都会失败，
    /// 让"确认"需要一次明确的点击比让"取消"更需要点击更安全。
    /// </summary>
    private bool ConfirmPickPreview(string previewText)
    {
        return MessageBox.Show(this,
            Loc.T("settings.automation.pickPreviewConfirm",
                "程序将从这些元素提取以下网址，请确认位置选对了：\n\n{0}\n\n点「确定」采用这次选取，点「取消」重新选。",
                previewText),
            Loc.T("settings.automation.pickPreviewTitle", "确认选取结果"),
            MessageBoxButton.OKCancel, MessageBoxImage.Question,
            MessageBoxResult.Cancel) == MessageBoxResult.OK;
    }

    // ======================= 测试向导（任务四 4.5） =======================

    private void WizardButton_Click(object sender, RoutedEventArgs e) => _ = RunWizardAsync(1);

    private void WizardStep_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button button && int.TryParse((string?)button.Tag, out int step))
            _ = RunWizardAsync(step);
    }

    private async Task RunWizardAsync(int step)
    {
        try
        {
            SetLoadState(Loc.T("settings.automation.wizardRunning", "⏳ 正在执行，请稍候…"));
            if (step == 4)
            {
                // 第 4 步要跑一次真实任务，走流式（可在主窗口点 ⏹ 停止）
                string log = await _backend.SendStreamRequestAsync(
                    new { action = "automation_wizard", step },
                    delta => SetLoadState(delta.Trim()),
                    null,
                    cancellationToken: _closing.Token);
                ShowWizardResult(step, true, log, Array.Empty<string>());
                return;
            }

            string json = await _backend.SendRequestAsync(new { action = "automation_wizard_step", step },
                idleTimeout: TimeSpan.FromMinutes(5), cancellationToken: _closing.Token);
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("wizard", out JsonElement wizard))
            {
                ShowWizardResult(step, false, GetString(doc.RootElement, "content"), Array.Empty<string>());
                return;
            }
            bool ok = GetBool(wizard, "ok");
            var suggestions = new List<string>();
            if (wizard.TryGetProperty("suggestions", out JsonElement sug) && sug.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement item in sug.EnumerateArray())
                    suggestions.Add(item.ToString());
            }
            ShowWizardResult(step, ok, GetString(wizard, "detail"), suggestions);
        }
        catch (Exception ex)
        {
            ShowWizardResult(step, false, ex.Message, Array.Empty<string>());
        }
    }

    private void ShowWizardResult(int step, bool ok, string detail, IReadOnlyList<string> suggestions)
    {
        string head = ok ? Loc.T("settings.automation.wizardDone", "✅ 这一步通过")
                         : Loc.T("settings.automation.wizardFailed", "❌ 这一步没通过");
        var sb = new System.Text.StringBuilder();
        sb.AppendLine(head);
        sb.AppendLine();
        sb.AppendLine(detail);
        if (suggestions.Count > 0)
        {
            sb.AppendLine();
            sb.AppendLine(Loc.T("settings.automation.wizardSuggestions", "建议："));
            foreach (string item in suggestions)
                sb.AppendLine("· " + item);
        }
        SetLoadState(Loc.T("settings.automation.wizardStepHead", "{0}（{1}）", head,
            Loc.T("settings.automation.wizardStep" + step, "第 " + step + " 步")));
        MessageBox.Show(this, sb.ToString(),
            Loc.T("settings.automation.wizardStep" + step, "第 " + step + " 步"),
            MessageBoxButton.OK, ok ? MessageBoxImage.Information : MessageBoxImage.Warning);
    }

    // ======================= 配置导入导出（任务四 4.7） =======================

    private async void ExportConfigButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.SaveFileDialog
        {
            Title = Loc.T("settings.automation.exportConfig"),
            Filter = Loc.T("settings.automation.configFilterDesc", "EchoDesk 自动化配置")
                + " (*.echodesk-auto)|*.echodesk-auto",
            FileName = "echodesk-automation.echodesk-auto",
        };
        if (dialog.ShowDialog() != true)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(
                new { action = "automation_config_export", path = dialog.FileName });
            using JsonDocument doc = JsonDocument.Parse(json);
            SetLoadState(GetString(doc.RootElement, "content"));
        }
        catch (Exception ex)
        {
            SetLoadState($"❌ {ex.Message}");
        }
    }

    private async void ImportConfigButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = Loc.T("settings.automation.importConfig"),
            Filter = Loc.T("settings.automation.configFilterDesc", "EchoDesk 自动化配置")
                + " (*.echodesk-auto;*.json)|*.echodesk-auto;*.json|"
                + Loc.T("settings.automation.allFilesFilterDesc", "所有文件") + " (*.*)|*.*",
        };
        if (dialog.ShowDialog() != true)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(
                new { action = "automation_config_import", path = dialog.FileName });
            using JsonDocument doc = JsonDocument.Parse(json);
            SetLoadState(GetString(doc.RootElement, "content"));
            if (doc.RootElement.TryGetProperty("automation", out JsonElement el))
                ApplyAutomation(el);
        }
        catch (Exception ex)
        {
            SetLoadState($"❌ {ex.Message}");
        }
    }

    // ======================= 已安装 Tab（任务二 2.6 / 2B.7） =======================

    private void InitSkillsUi()
    {
        LogLinesCombo.Items.Clear();
        foreach (int n in new[] { 100, 200, 500, 1000, 2000 })
            LogLinesCombo.Items.Add(new ComboBoxItem { Content = n.ToString(), Tag = n });
        LogLinesCombo.SelectedIndex = 1;
        RenderSkillCards();
    }

    private async Task LoadSkillsAsync()
    {
        try
        {
            string? json = await RequestWithRetryAsync(new { action = "skills_list" },
                Loc.T("settings.installed.labelReadingSkills", "读取插件列表"), TimeSpan.FromSeconds(60));
            if (json is null)
                return;
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("skills", out JsonElement status))
                return;
            _skillStatus = status.Clone();
            RenderSkillCards();
            RenderDevDirs();
            RenderHooks();
            RenderPanels();
            await RefreshDevLogAsync();
            SyncDevModeCheck();
            // 信任确认框必须延到本次加载结束之后：MessageBox.ShowDialog 会在当前调用栈上
            // 阻塞 UI 线程，放在 LoadInitialAsync 里会让"设置窗口打开后还要很久才可用"，
            // 与 v0.2.0 修复一的目标（打开即见、可交互）相冲突。
            Dispatcher.BeginInvoke(new Action(CheckTrustPending), DispatcherPriority.Background);
        }
        catch (Exception ex)
        {
            App.Log("SettingsWindow.LoadSkills", ex);
            if (SkillStatusText is not null)
                SkillStatusText.Text = $"⚠️ {ex.Message}";
        }
    }

    private void RenderSkillCards()
    {
        if (SkillList is null)
            return;
        var cards = new List<SkillCardItem>();
        if (_skillStatus is JsonElement status
            && status.TryGetProperty("skills", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in arr.EnumerateArray())
            {
                string source = GetString(item, "source");
                bool loaded = GetBool(item, "loaded");
                bool enabled = !item.TryGetProperty("enabled", out JsonElement en) || en.ValueKind != JsonValueKind.False;
                string error = GetString(item, "error");
                string overrode = GetString(item, "overrode");
                string version = GetString(item, "version");
                string sourceTag = source switch
                {
                    "dev" => Loc.T("settings.installed.sourceDev"),
                    "local" => Loc.T("settings.installed.sourceLocal"),
                    _ => Loc.T("settings.installed.sourceMarket"),
                };
                // 修复二：状态颜色也走色板（原来是写死的 Gray/IndianRed/MediumSeaGreen，
                // 在深色背景上几乎糊在一起）。切换主题时 RenderSkillCards 会重建卡片，
                // 因此这里取"当前色板的值"是安全的。
                (string statusText, string brushKey) = !enabled
                    ? (Loc.T("settings.installed.statusDisabled"), "DisabledTextBrush")
                    : error.Length > 0
                        ? (Loc.T("settings.installed.statusFailed"), "ErrorBrush")
                        : loaded
                            ? (Loc.T("settings.installed.statusLoaded"), "SuccessBrush")
                            : (Loc.T("settings.installed.statusNotLoaded"), "SubTextBrush");
                Brush brush = ThemeService.Brush(brushKey);

                var caps = new List<string>();
                string commands = string.Join(", ", GetStringList(item, "commands"));
                if (commands.Length > 0)
                    caps.Add(Loc.T("settings.installed.commandsLabel") + commands);
                string parsers = string.Join(", ", GetStringList(item, "parsers"));
                if (parsers.Length > 0)
                    caps.Add(Loc.T("settings.installed.parsersLabel") + parsers);
                string hooks = string.Join(", ", GetStringList(item, "hooks"));
                if (hooks.Length > 0)
                    caps.Add(Loc.T("settings.installed.hooksLabel") + hooks);
                string panels = string.Join(", ", GetStringList(item, "panels"));
                if (panels.Length > 0)
                    caps.Add(Loc.T("settings.installed.panelsLabel") + panels);

                string permissions = Loc.T("settings.installed.noPermissions");
                if (item.TryGetProperty("permissions", out JsonElement perms) && perms.ValueKind == JsonValueKind.Object)
                {
                    var parts = new List<string>();
                    foreach (JsonProperty property in perms.EnumerateObject())
                        parts.Add($"{property.Name}={property.Value}");
                    if (parts.Count > 0)
                        permissions = string.Join("; ", parts);
                }

                cards.Add(new SkillCardItem
                {
                    Id = GetString(item, "id"),
                    Name = GetString(item, "name"),
                    Version = version,
                    Source = source,
                    Path = GetString(item, "path"),
                    Loaded = loaded,
                    Enabled = enabled,
                    Dev = GetBool(item, "dev"),
                    Error = error,
                    SourceTag = sourceTag,
                    StatusText = statusText,
                    StatusBrush = brush,
                    Meta = (version.Length > 0 ? $"v{version} · " : "") + GetString(item, "path"),
                    Capabilities = string.Join("　", caps),
                    Permissions = Loc.T("settings.installed.permissionsLabel") + permissions,
                    Overrode = overrode.Length > 0
                        ? Loc.T("settings.installed.overrode", "已覆盖：{0}", overrode) : "",
                });
            }
        }
        SkillList.ItemsSource = cards;
        SkillEmptyText.Visibility = cards.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        if (SkillStatusText is not null && _skillStatus is JsonElement st
            && st.TryGetProperty("counts", out JsonElement counts))
        {
            SkillStatusText.Text =
                $"{Loc.T("settings.tab.installed")}: " +
                $"total={GetInt(counts, "total", 0)}, loaded={GetInt(counts, "loaded", 0)}, " +
                $"failed={GetInt(counts, "failed", 0)} · " +
                $"API {GetString(st, "api_version")}";
        }
    }

    /// <summary>
    /// 首次加载未知来源的本地插件时弹**一次**汇总确认框（任务书 2B.9 + 修复一）。
    ///
    /// 修复前是"每发现一个未确认插件就弹一次 MessageBox"，并且每次答复都会触发
    /// 重新加载 → 继续弹下一个，N 个插件就是 N 次弹窗。现在改成一次性列出全部，
    /// 勾选后统一写盘（后端 skills_trust_bulk），此后再也不问。
    /// </summary>
    private void CheckTrustPending()
    {
        if (_trustDialogOpen)
            return;   // 防止重入（加载回包 + 语言切换等都会走到这里）
        if (_skillStatus is not JsonElement status
            || !status.TryGetProperty("skills", out JsonElement arr) || arr.ValueKind != JsonValueKind.Array)
            return;
        if (_trustDialogHandled)
            return;   // 本次窗口会话里已经问过，不再重复弹

        var pending = new List<TrustItem>();
        foreach (JsonElement item in arr.EnumerateArray())
        {
            if (!GetBool(item, "trust_pending"))
                continue;
            var caps = new List<string>();
            foreach (string key in new[] { "commands", "parsers", "hooks", "panels" })
            {
                string joined = string.Join(", ", GetStringList(item, key));
                if (joined.Length > 0)
                    caps.Add($"{key}: {joined}");
            }
            var permissions = new Dictionary<string, object?>();
            if (item.TryGetProperty("permissions", out JsonElement perms)
                && perms.ValueKind == JsonValueKind.Object)
            {
                foreach (JsonProperty property in perms.EnumerateObject())
                    permissions[property.Name] = property.Value.ToString();
            }
            pending.Add(new TrustItem
            {
                Id = GetString(item, "id"),
                Name = GetString(item, "name"),
                Path = GetString(item, "path"),
                Source = GetString(item, "source"),
                Capabilities = caps,
                Permissions = permissions,
                Enabled = true,
            });
        }
        if (pending.Count == 0)
            return;

        _trustDialogHandled = true;
        _trustDialogOpen = true;
        try
        {
            var dialog = new TrustDialog(pending, this);
            dialog.ShowDialog();
            if (dialog.Decisions.Count > 0)
            {
                // 一次性提交全部决定：启用=写入 trusted，禁用=写入 denied（下次不再问）
                var decisions = dialog.Decisions
                    .Select(pair => new { id = pair.Key, trusted = pair.Value })
                    .ToList();
                _ = SendSkillsAsync(new { action = "skills_trust_bulk", decisions });
            }
            else
            {
                // 用户直接关掉对话框：不写任何记录，下次打开设置窗口时会再问一次
                _trustDialogHandled = false;
            }
        }
        finally
        {
            _trustDialogOpen = false;
        }
    }

    private async Task SendSkillsAsync(object request)
    {
        try
        {
            string json = await _backend.SendRequestAsync(request);
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = GetString(doc.RootElement, "content");
            if (content.Length > 0)
                SkillStatusText.Text = content;
            if (doc.RootElement.TryGetProperty("commands", out JsonElement commands))
                MainWindow.ApplyCommandsFromBackend(commands);
            await LoadSkillsAsync();
        }
        catch (InvalidOperationException)
        {
            // 后端未启动/已退出：窗口正在关闭或后端已被停止时，后台请求会撞上这个竞态。
            // 这是预期情况，不是缺陷——只在状态栏说明一句，不写崩溃日志（否则每次关窗
            // 都会留下一条误导性的"内部错误"，v0.2.1 实测中确实出现过）。
            if (SkillStatusText is not null)
                SkillStatusText.Text = Loc.T("settings.installed.backendGone",
                    "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）");
        }
        catch (Exception ex)
        {
            SkillStatusText.Text = $"❌ {ex.Message}";
            App.Log("SettingsWindow.SendSkills", ex);
        }
    }

    private void SkillsRefreshButton_Click(object sender, RoutedEventArgs e) => _ = LoadSkillsAsync();

    private void SkillsReloadButton_Click(object sender, RoutedEventArgs e)
    {
        SetLoadState(Loc.T("settings.installed.reload") + "…");
        _ = SendSkillsAsync(new { action = "skills_reload" });
    }

    private void SkillsInstallFolderButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.OpenFolderDialog { Title = Loc.T("settings.installed.installPrompt") };
        if (dialog.ShowDialog() != true)
            return;
        _ = SendSkillsAsync(new { action = "skills_install_folder", path = dialog.FolderName });
    }

    private void SkillsScaffoldButton_Click(object sender, RoutedEventArgs e)
    {
        ShowTab(Loc.T("settings.tab.developer"));
        ScaffoldIdBox.Focus();
    }

    private void SkillOpenDir_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is SkillCardItem card)
            OpenFolder(card.Path);
    }

    private void SkillDetail_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not SkillCardItem card)
            return;
        var sb = new System.Text.StringBuilder();
        sb.AppendLine($"{card.Name} ({card.Id})");
        sb.AppendLine(card.Path);
        sb.AppendLine();
        sb.AppendLine(card.Error);
        if (card.Error.Length == 0)
            sb.AppendLine(Loc.T("settings.installed.statusLoaded"));
        MessageBox.Show(this, sb.ToString(), Loc.T("settings.installed.detail"),
            MessageBoxButton.OK, MessageBoxImage.Warning);
    }

    private async void SkillExportZip_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not SkillCardItem card)
            return;
        var dialog = new Microsoft.Win32.SaveFileDialog
        {
            Title = Loc.T("settings.installed.exportZip"),
            Filter = "ZIP (*.zip)|*.zip",
            FileName = card.Id + ".zip",
        };
        if (dialog.ShowDialog() != true)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new
            {
                action = "skills_export_zip",
                id = card.Id,
                path = dialog.FileName,
            });
            using JsonDocument doc = JsonDocument.Parse(json);
            SkillStatusText.Text = GetString(doc.RootElement, "content");
        }
        catch (Exception ex)
        {
            SkillStatusText.Text = $"❌ {ex.Message}";
        }
    }

    private async void SkillPublishCheck_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not SkillCardItem card)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "skills_publish_check", id = card.Id });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("publish_check", out JsonElement check))
                return;
            var sb = new System.Text.StringBuilder();
            sb.AppendLine(Loc.T("settings.installed.publishLine", "{0}：{1}",
                Loc.T("settings.installed.publishResult"), GetString(check, "message")));
            sb.AppendLine(Loc.T("settings.installed.publishLine", "{0}：{1}",
                Loc.T("settings.developer.topicLabel", "市场 topic"), GetString(check, "topic")));
            sb.AppendLine(Loc.T("settings.installed.publishLine", "{0}：{1}",
                Loc.T("settings.developer.repoLabel", "建议仓库名"), GetString(check, "repo_name")));
            sb.AppendLine();
            if (check.TryGetProperty("checks", out JsonElement checks) && checks.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement item in checks.EnumerateArray())
                {
                    bool ok = GetBool(item, "ok");
                    sb.AppendLine($"{(ok ? "✓" : "✗")} {GetString(item, "name")}");
                    if (!ok)
                        sb.AppendLine("    " + GetString(item, "hint"));
                }
            }
            MessageBox.Show(this, sb.ToString(), Loc.T("settings.installed.publishResult"),
                MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            SkillStatusText.Text = $"❌ {ex.Message}";
        }
    }

    private void SkillUninstall_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not SkillCardItem card)
            return;
        string question = card.Dev
            ? Loc.T("settings.installed.uninstallConfirmDev",
                "确定卸载开发插件「{0}」吗？\n（只会删除 skills_dev 下的目录，不影响正式安装记录）", card.Name)
            : Loc.T("settings.installed.uninstallConfirm", "确定卸载插件「{0}」吗？", card.Name);
        if (MessageBox.Show(this, question, Loc.T("settings.button.uninstall"),
                MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes)
            return;
        _ = SendSkillsAsync(new { action = "skills_uninstall", id = card.Id, delete_files = true });
    }

    // ======================= 开发者 Tab（任务 2B.10） =======================

    /// <summary>开发者模式开关（修复一：双向可用，状态与生效插件都显示出来）。</summary>
    private void SyncDevModeCheck()
    {
        if (DevModeCheck is null || _skillStatus is not JsonElement status)
            return;
        bool devMode = GetBool(status, "dev_mode");
        bool explicitSet = GetBool(status, "dev_mode_explicit");
        bool detected = GetBool(status, "dev_detected");
        int devCount = GetInt(status, "dev_count", 0);
        _devModeSyncing = true;
        DevModeCheck.IsChecked = devMode;
        _devModeSyncing = false;

        if (DevModeStatusText is not null)
        {
            string how = explicitSet
                ? Loc.T("settings.developer.modeExplicit", "由你的设置决定")
                : Loc.T("settings.developer.modeAuto", "自动：检测到 skills_dev 中有 {0} 个插件", devCount);
            DevModeStatusText.Text =
                (devMode ? Loc.T("settings.developer.modeOn", "开发者模式：已开启")
                         : Loc.T("settings.developer.modeOff", "开发者模式：已关闭"))
                + $" · {how}";
        }
        RenderDevPluginList();
    }

    /// <summary>
    /// 开发者 Tab 里的"当前已启用的开发插件"清单（修复一"状态可见"）。
    /// 关闭开发者模式后这里会变成空，用户即可确认"确实关掉了"。
    /// </summary>
    private void RenderDevPluginList()
    {
        if (DevPluginListText is null)
            return;
        if (_skillStatus is not JsonElement status)
            return;
        var ids = GetStringList(status, "dev_enabled");
        if (ids.Count == 0)
        {
            bool detected = GetBool(status, "dev_detected");
            DevPluginListText.Text = detected
                ? Loc.T("settings.developer.pluginsDisabled",
                        "skills_dev 目录中有插件，但开发者模式已关闭，当前未加载")
                : Loc.T("settings.developer.pluginsEmpty", "当前没有已启用的开发插件");
            return;
        }
        var lines = new List<string>
        {
            Loc.T("settings.developer.pluginsHeader", "已启用的开发插件（{0} 个）：", ids.Count),
        };
        foreach (string id in ids)
            lines.Add($"  • {id}");
        DevPluginListText.Text = string.Join("\n", lines);
    }

    private void RenderDevDirs()
    {
        if (DevDirsText is null || _skillStatus is not JsonElement status
            || !status.TryGetProperty("dirs", out JsonElement dirs))
            return;
        DevDirsText.Text =
            $"{Loc.T("settings.developer.devDir")}: {GetString(dirs, "dev")}\n" +
            $"{Loc.T("settings.developer.installDir")}: {GetString(dirs, "installed")}\n" +
            $"{Loc.T("settings.developer.manifestFile")}: {GetString(dirs, "manifest")}\n" +
            $"{Loc.T("settings.developer.registryFile")}: {GetString(dirs, "registry")}\n" +
            $"{Loc.T("settings.developer.logFile")}: {GetString(dirs, "log")}\n" +
            $"{Loc.T("settings.developer.examplesDir")}: {GetString(dirs, "examples")}";
        DevApiText.Text = Loc.T("settings.developer.apiVersion", "技能 API 版本：{0}",
            GetString(status, "api_version"));
    }

    private void RenderHooks()
    {
        if (HooksText is null || _skillStatus is not JsonElement status
            || !status.TryGetProperty("hooks", out JsonElement hooks) || hooks.ValueKind != JsonValueKind.Object)
            return;
        var lines = new List<string>();
        foreach (JsonProperty property in hooks.EnumerateObject())
        {
            var skills = new List<string>();
            if (property.Value.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement item in property.Value.EnumerateArray())
                    skills.Add(item.ToString());
            }
            if (skills.Count == 0)
                continue;
            string stageName = Loc.T("settings.developer.hookStages." + property.Name, property.Name);
            lines.Add($"{stageName}: {string.Join(", ", skills)}");
        }
        HooksText.Text = lines.Count > 0
            ? string.Join("\n", lines)
            : Loc.T("settings.developer.hooksEmpty");
    }

    /// <summary>
    /// 渲染插件声明的面板（任务书 2.3 UI 面板注册）。
    ///
    /// 面板用自己的原生控件动态生成（文本框 / 复选框 / 按钮），因此技能不需要
    /// 碰任何 UI 代码，也不必引入 WebView2 之类的重依赖（任务书 2B.12 体积优化）。
    /// </summary>
    private void RenderPanels()
    {
        if (PanelList is null)
            return;
        _panelFields.Clear();
        var blocks = new List<UIElement>();
        if (_skillStatus is JsonElement status
            && status.TryGetProperty("panels", out JsonElement panels) && panels.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in panels.EnumerateArray())
            {
                string skillId = GetString(item, "skill_id");
                string panelId = GetString(item, "id");
                var box = new Border
                {
                    CornerRadius = new CornerRadius(8),
                    Padding = new Thickness(12, 10, 12, 10),
                    Margin = new Thickness(0, 0, 0, 10),
                    Background = (Brush)FindResource("InputBackground"),
                    BorderBrush = (Brush)FindResource("InputBorder"),
                    BorderThickness = new Thickness(1),
                };
                var stack = new StackPanel();
                stack.Children.Add(new TextBlock
                {
                    Text = GetString(item, "title"),
                    FontWeight = FontWeights.Bold,
                    Foreground = (Brush)FindResource("AccentBrush"),
                });
                string description = GetString(item, "description");
                if (description.Length > 0)
                    stack.Children.Add(new TextBlock
                    {
                        Text = description,
                        FontSize = 11,
                        TextWrapping = TextWrapping.Wrap,
                        Margin = new Thickness(0, 4, 0, 0),
                        Foreground = (Brush)FindResource("SubTextBrush"),
                    });

                var values = new Dictionary<string, string>(StringComparer.Ordinal);
                if (item.TryGetProperty("fields", out JsonElement fields) && fields.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement field in fields.EnumerateArray())
                    {
                        string key = GetString(field, "key");
                        string type = GetString(field, "type");
                        string label = GetString(field, "label");
                        string help = GetString(field, "help");
                        string defaultValue = GetString(field, "default");
                        string full = $"{skillId}/{panelId}/{key}";
                        if (type == "checkbox")
                        {
                            var check = new CheckBox
                            {
                                Content = label,
                                Margin = new Thickness(0, 8, 0, 0),
                                IsChecked = defaultValue is "1" or "true" or "True",
                                ToolTip = help,
                            };
                            values[key] = check.IsChecked == true ? "1" : "0";
                            check.Checked += (_, _) => values[key] = "1";
                            check.Unchecked += (_, _) => values[key] = "0";
                            stack.Children.Add(check);
                        }
                        else
                        {
                            stack.Children.Add(new TextBlock
                            {
                                Text = label,
                                Margin = new Thickness(0, 8, 0, 2),
                                Foreground = (Brush)FindResource("SubTextBrush"),
                            });
                            var text = new TextBox
                            {
                                Text = defaultValue,
                                Padding = new Thickness(6, 4, 6, 4),
                                ToolTip = help,
                            };
                            values[key] = defaultValue;
                            text.TextChanged += (_, _) => values[key] = text.Text;
                            _panelFields[full] = text;
                            stack.Children.Add(text);
                        }
                    }
                }

                if (item.TryGetProperty("actions", out JsonElement actions) && actions.ValueKind == JsonValueKind.Array)
                {
                    var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 10, 0, 0) };
                    foreach (JsonElement action in actions.EnumerateArray())
                    {
                        string actionKey = GetString(action, "key");
                        var button = new Button
                        {
                            Content = GetString(action, "label"),
                            Padding = new Thickness(12, 4, 12, 4),
                            Margin = new Thickness(0, 0, 8, 0),
                            Style = (Style)FindResource("RoundButton"),
                            Tag = new PanelActionTag(skillId, panelId, actionKey, values),
                        };
                        button.Click += PanelAction_Click;
                        row.Children.Add(button);
                    }
                    stack.Children.Add(row);
                }

                box.Child = stack;
                blocks.Add(box);
            }
        }
        if (blocks.Count == 0)
            blocks.Add(new TextBlock
            {
                Text = Loc.T("settings.developer.panelsEmpty"),
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("SubTextBrush"),
            });
        PanelList.ItemsSource = blocks;
    }

    /// <summary>面板动作按钮携带的信息（技能 id、面板 id、动作 key、以及取值委托）。</summary>
    private sealed record PanelActionTag(string SkillId, string PanelId, string ActionKey,
                                         Dictionary<string, string> Values);

    /// <summary>
    /// 开发者模式开关（修复一）。
    ///
    /// 关键改动：写入配置时后端会同时把 <c>dev_mode_explicit</c> 置为 True，
    /// 这样"关闭"就是真的关闭 —— 否则只要 <c>skills_dev/</c> 里还有插件，
    /// 后端就会按"目录里有插件"自动把模式重新打开（原缺陷）。
    /// </summary>
    private void DevModeCheck_Changed(object sender, RoutedEventArgs e)
    {
        if (!_uiReady || _devModeSyncing)
            return;
        bool enabled = DevModeCheck.IsChecked == true;
        _uiState.DeveloperMode = enabled;
        _uiState.Save();
        SetLoadState(enabled
            ? Loc.T("settings.developer.modeEnabling", "正在开启开发者模式…")
            : Loc.T("settings.developer.modeDisabling", "正在关闭开发者模式并卸载开发插件…"));
        _ = SendSkillsAsync(new { action = "skills_set_dev_mode", enabled });
    }

    private void OpenDevDirButton_Click(object sender, RoutedEventArgs e) => OpenFolder(SkillDir("dev"));

    private void OpenInstallDirButton_Click(object sender, RoutedEventArgs e) => OpenFolder(SkillDir("installed"));

    private void OpenExamplesDirButton_Click(object sender, RoutedEventArgs e) => OpenFolder(SkillDir("examples"));

    private void InstallExamplesButton_Click(object sender, RoutedEventArgs e) =>
        _ = SendSkillsAsync(new { action = "skills_examples" });

    private void ExportDevLogButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.SaveFileDialog
        {
            Title = Loc.T("settings.developer.exportLog"),
            Filter = Loc.T("settings.developer.logFilterDesc", "日志") + " (*.log)|*.log|"
                + Loc.T("settings.developer.textFilterDesc", "文本") + " (*.txt)|*.txt",
            FileName = "skills_dev.log",
        };
        if (dialog.ShowDialog() != true)
            return;
        _ = SendSkillsAsync(new { action = "skills_export_log", path = dialog.FileName });
    }

    private void ClearDevLogButton_Click(object sender, RoutedEventArgs e)
    {
        _ = SendSkillsAsync(new { action = "skills_clear_log" });
        if (DevLogBox is not null)
            DevLogBox.Text = "";
    }

    private void RefreshDevLogButton_Click(object sender, RoutedEventArgs e) => _ = RefreshDevLogAsync();

    private void LogLinesCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || LogLinesCombo.SelectedItem is not ComboBoxItem item)
            return;
        if (item.Tag is int n)
        {
            _logLineCount = n;
            _ = RefreshDevLogAsync();
        }
    }

    private async Task RefreshDevLogAsync()
    {
        if (DevLogBox is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "skills_log", lines = _logLineCount });
            using JsonDocument doc = JsonDocument.Parse(json);
            DevLogBox.Text = GetString(doc.RootElement, "log");
            DevLogBox.ScrollToEnd();
        }
        catch (Exception ex)
        {
            DevLogBox.Text = Loc.T("settings.developer.logLoadFailed", "（读取日志失败：{0}）", ex.Message);
        }
    }

    private void ScaffoldCreateButton_Click(object sender, RoutedEventArgs e)
    {
        string id = ScaffoldIdBox.Text.Trim();
        if (id.Length == 0)
        {
            MessageBox.Show(this, Loc.T("settings.installed.scaffoldId"), Loc.T("settings.tab.developer"),
                MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        _ = ScaffoldAsync(id, ScaffoldNameBox.Text.Trim(), ScaffoldAuthorBox.Text.Trim());
    }

    private async Task ScaffoldAsync(string id, string name, string author)
    {
        try
        {
            string json = await _backend.SendRequestAsync(new
            {
                action = "skills_scaffold",
                id,
                name,
                author,
            });
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = GetString(doc.RootElement, "content");
            SetLoadState(content);
            if (doc.RootElement.TryGetProperty("scaffold", out JsonElement scaffold) && GetBool(scaffold, "ok"))
            {
                string path = GetString(scaffold, "path");
                if (MessageBox.Show(this, Loc.T("settings.installed.scaffoldOk", "✅ 已创建插件骨架：{0}", path)
                        + "\n\n" + Loc.T("settings.installed.scaffoldOpen", "是否现在打开插件目录？"),
                        Loc.T("settings.tab.developer"), MessageBoxButton.YesNo, MessageBoxImage.Question)
                    == MessageBoxResult.Yes)
                {
                    OpenFolder(path);
                }
                ScaffoldIdBox.Text = "";
                ScaffoldNameBox.Text = "";
                ScaffoldAuthorBox.Text = "";
            }
            await LoadSkillsAsync();
        }
        catch (Exception ex)
        {
            SetLoadState($"❌ {ex.Message}");
        }
    }

    // ---- 本地插件清单编辑 ----

    private async Task LoadManifestAsync()
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "skills_manifest_get" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("manifest", out JsonElement manifest))
                return;
            _manifestRows.Clear();
            if (manifest.TryGetProperty("plugins", out JsonElement plugins) && plugins.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement item in plugins.EnumerateArray())
                    _manifestRows.Add(new ManifestRow
                    {
                        Id = GetString(item, "id"),
                        Path = GetString(item, "path"),
                        Alias = GetString(item, "alias"),
                        Enabled = !item.TryGetProperty("enabled", out JsonElement en) || en.ValueKind != JsonValueKind.False,
                        Order = GetInt(item, "order", 0),
                        Note = GetString(item, "note"),
                    });
            }
            RenderManifestRows();
        }
        catch (Exception ex)
        {
            App.Log("SettingsWindow.LoadManifest", ex);
        }
    }

    private void RenderManifestRows()
    {
        if (ManifestList is null)
            return;
        var rows = new List<UIElement>();
        foreach (ManifestRow row in _manifestRows)
        {
            var grid = new Grid { Margin = new Thickness(0, 2, 0, 2) };
            foreach (double width in new[] { 140.0, 1.0, 0.0 })
                grid.ColumnDefinitions.Add(new ColumnDefinition
                {
                    Width = width == 1.0 ? new GridLength(1, GridUnitType.Star)
                        : width == 0.0 ? GridLength.Auto : new GridLength(width),
                });
            var idBox = new TextBox { Text = row.Id, Padding = new Thickness(6, 3, 6, 3), Width = 130 };
            idBox.TextChanged += (_, _) => row.Id = idBox.Text;
            var pathBox = new TextBox { Text = row.Path, Padding = new Thickness(6, 3, 6, 3), Margin = new Thickness(6, 0, 0, 0) };
            pathBox.TextChanged += (_, _) => row.Path = pathBox.Text;
            var aliasBox = new TextBox { Text = row.Alias, Padding = new Thickness(6, 3, 6, 3), Width = 120, Margin = new Thickness(6, 0, 0, 0) };
            aliasBox.TextChanged += (_, _) => row.Alias = aliasBox.Text;
            var enabled = new CheckBox
            {
                IsChecked = row.Enabled,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(8, 0, 0, 0),
                ToolTip = Loc.T("settings.developer.manifestEnabled"),
            };
            enabled.Checked += (_, _) => row.Enabled = true;
            enabled.Unchecked += (_, _) => row.Enabled = false;
            var orderBox = new TextBox { Text = row.Order.ToString(), Padding = new Thickness(6, 3, 6, 3), Width = 46, Margin = new Thickness(8, 0, 0, 0) };
            orderBox.TextChanged += (_, _) =>
            {
                if (int.TryParse(orderBox.Text, out int order))
                    row.Order = order;
            };
            var remove = new Button
            {
                Content = "✕",
                Padding = new Thickness(6, 1, 6, 1),
                Margin = new Thickness(8, 0, 0, 0),
                Style = (Style)FindResource("RoundButton"),
                ToolTip = Loc.T("settings.button.remove"),
            };
            remove.Click += (_, _) =>
            {
                _manifestRows.Remove(row);
                RenderManifestRows();
            };

            var inner = new StackPanel { Orientation = Orientation.Horizontal };
            inner.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.developer.manifestPath"),
                VerticalAlignment = VerticalAlignment.Center,
                Width = 60,
            });
            inner.Children.Add(pathBox);
            inner.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.developer.manifestAlias"),
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(8, 0, 0, 0),
            });
            inner.Children.Add(aliasBox);
            inner.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.developer.manifestOrder"),
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(8, 0, 0, 0),
            });
            inner.Children.Add(orderBox);
            inner.Children.Add(enabled);
            inner.Children.Add(remove);

            Grid.SetColumn(idBox, 0);
            Grid.SetColumn(inner, 1);
            grid.Children.Add(idBox);
            grid.Children.Add(inner);
            rows.Add(grid);
        }
        ManifestList.ItemsSource = rows;
    }

    private void ManifestAddRow_Click(object sender, RoutedEventArgs e)
    {
        _manifestRows.Add(new ManifestRow { Id = "", Path = "", Enabled = true });
        RenderManifestRows();
    }

    private void ManifestOpenRaw_Click(object sender, RoutedEventArgs e)
    {
        string path = SkillDir("manifest");
        if (File.Exists(path))
        {
            try
            {
                Process.Start(new ProcessStartInfo { FileName = path, UseShellExecute = true });
                return;
            }
            catch (Exception ex)
            {
                App.Log("SettingsWindow.ManifestOpenRaw", ex);
            }
        }
        OpenFolder(Path.GetDirectoryName(path));
    }

    private void ManifestSave_Click(object sender, RoutedEventArgs e)
    {
        var plugins = _manifestRows
            .Where(row => row.Id.Trim().Length > 0 && row.Path.Trim().Length > 0)
            .Select(row => new
            {
                id = row.Id.Trim(),
                path = row.Path.Trim(),
                enabled = row.Enabled,
                source = "local",
                order = row.Order,
                alias = row.Alias,
                note = row.Note,
            })
            .ToList();
        var trusted = _manifestRows.Select(row => row.Id.Trim()).Where(x => x.Length > 0).ToList();
        _ = SendSkillsAsync(new { action = "skills_manifest_set", manifest = new { plugins, trusted } });
        SetLoadState(Loc.T("settings.developer.manifestSaved"));
    }

    // ---- 插件面板动作 ----

    private async void PanelAction_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: PanelActionTag tag })
            return;
        var values = new Dictionary<string, object?>();
        foreach (KeyValuePair<string, string> pair in tag.Values)
            values[pair.Key] = pair.Value;
        try
        {
            string json = await _backend.SendRequestAsync(new
            {
                action = "skills_panel_action",
                skill_id = tag.SkillId,
                panel_id = tag.PanelId,
                panel_action = tag.ActionKey,
                values,
            });
            using JsonDocument doc = JsonDocument.Parse(json);
            DevLogBox.Text = GetString(doc.RootElement, "content");
        }
        catch (Exception ex)
        {
            DevLogBox.Text = $"❌ {ex.Message}";
        }
    }

    /// <summary>安全地取状态里的某个目录（状态还没加载时返回空串，不抛异常）。</summary>
    private string SkillDir(string name)
    {
        if (_skillStatus is JsonElement status
            && status.TryGetProperty("dirs", out JsonElement dirs)
            && dirs.ValueKind == JsonValueKind.Object)
        {
            return GetString(dirs, name);
        }
        return "";
    }

    /// <summary>在控件树里找出所有 Tag 等于给定值的 T 控件（给「?」按钮批量挂说明用）。</summary>
    private static IEnumerable<T> FindTagged<T>(DependencyObject root, string tag) where T : FrameworkElement
    {
        var found = new List<T>();
        Walk(root, found, tag);
        return found;
    }

    private static void Walk<T>(DependencyObject node, List<T> found, string tag) where T : FrameworkElement
    {
        if (node is T typed && string.Equals((string?)typed.Tag, tag, StringComparison.Ordinal))
            found.Add(typed);
        // 只有 Visual / Visual3D 才有可视化子节点。逻辑树里还会出现 ColumnDefinition、
        // RowDefinition 这类非 Visual 的 DependencyObject，对它调
        // VisualTreeHelper.GetChildrenCount 会抛 InvalidOperationException
        // （v0.2.1 开发中真的踩到过：设置窗口加载完就写了一条前端崩溃日志）。
        if (node is Visual or System.Windows.Media.Media3D.Visual3D)
        {
            int count = VisualTreeHelper.GetChildrenCount(node);
            for (int i = 0; i < count; i++)
                Walk(VisualTreeHelper.GetChild(node, i), found, tag);
        }
        foreach (object? child in LogicalTreeHelper.GetChildren(node))
        {
            if (child is DependencyObject dep)
                Walk(dep, found, tag);
        }
    }
}
