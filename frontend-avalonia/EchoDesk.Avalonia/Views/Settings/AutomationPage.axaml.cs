// 自动化页的交互逻辑（v0.2.1 跨平台-2 任务一，第 7 个迁移模块）。
//
// 后端动作：
//   automation_config      → { automation: { …20 个字段… } }
//   automation_config_set  { automation: patch } → { content, automation }
//   automation_run / stop / automation_status / automation_clear_login
//
// **任务十七（自测问题修复-1）新增的四个动作**（都在本页下半部分的"可视化选取"块里）：
//   automation_browser_reuse          → { browser_reuse: { …能力与配置… } }（可带 set 写回）
//   automation_launch_debug_browser   → { browser_debug: { ok, command, message } }
//   automation_pick_open/begin/status/cancel/close
//                                     → { pick: { state, … } }（两段式状态机，后端持有）
//
// 两段式的分工（任务书第 4 条）：**状态机在后端**，前端只做两件事——
//   ① 按 `pick.state` 决定哪些按钮可用、显示后端回传的文案；
//   ② 选取中用 DispatcherTimer 轮询 `automation_pick_status`（后端借这次轮询去页面上
//      收结果）。这样"脚本到底注入了没有"这个事实永远与界面一致，切换设置页也不会丢状态。
//
// **平台降级（任务书明确要求）**：自动化依赖本机 Edge 的受控启动，
// 是 EchoDesk 在非 Windows 上的两条降级边界之一。本页在那些平台上：
//   ① 顶部横幅说明原因**与替代做法**（不把平台限制说成"没装 Playwright"这类依赖缺失）；
//   ② 全部输入禁用 —— 让人填一整套配置却永远跑不起来，比直接说明不可用更糟；
//   ③ 后端还会再拒绝一次（平台检查在 automation.validate 里），前端只是不让人白填。
//
// 保存走 `ApplyToBackendAsync()`：这正是"设置窗口骨架的保存按钮 → 各页自己同步业务设置"
// 这条契约的用途（界面设置归 ui_state.json，业务设置归后端 config.json）。
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class AutomationPage : UserControl, ISettingsPage
{
    /// <summary>安全协议版本（与 WPF 版一致；后端据此判断用户接受的是哪一版协议）。</summary>
    private const string SafetyVersion = "1.0";

    /// <summary>选取目标取值（与后端 _PICK_PURPOSE 的键一致；顺序即界面顺序）。</summary>
    private static readonly string[] PickTargets =
        { "task_list", "task_row", "next_page", "task_detail_file", "login_check" };

    /// <summary>浏览器取值（与后端 automation.BROWSER_CHOICES / AUTO_ORDER 一致）。</summary>
    private static readonly string[] BrowserChoices = { "auto", "edge", "chrome", "chromium", "firefox" };

    /// <summary>复用策略取值（与后端 automation.REUSE_MODES 一致）。</summary>
    private static readonly string[] ReuseModes = { "auto", "required", "off" };

    private ISettingsHost? _host;
    private bool _loading;
    private bool _available = true;

    /// <summary>"选取中"的轮询定时器：后端没有后台线程，结果靠这里的心跳收回来。</summary>
    private readonly DispatcherTimer _pickTimer = new() { Interval = TimeSpan.FromMilliseconds(700) };
    private bool _pickPolling;

    /// <summary>后端最近一次回传的选取状态原文，用于"复制命令"之类只读展示。</summary>
    private string _debugCommand = "";

    /// <summary>
    /// 正在用代码填写下拉框（FillCombo）时为真。
    ///
    /// 为什么必须有这个标志：给 ComboBox 设 SelectedIndex 会触发 SelectionChanged，
    /// 而那个事件是会**写回后端配置**的。界面刚打开时 ApplyTexts 先用默认值填一遍、
    /// RefreshAsync 再用配置里的真实值填一遍——若不挡住，第一遍那次"默认 auto"
    /// 会先把用户配置好的浏览器覆盖成 auto，等他看到界面时配置已经被自己改掉了。
    /// </summary>
    private bool _fillingCombos;

    /// <summary>所有输入控件：非 Windows 上统一禁用（见文件头）。</summary>
    private List<Control> _inputs = new();

    public AutomationPage()
    {
        InitializeComponent();
        _inputs = new List<Control>
        {
            SafetyCheck, AutomationEnabledCheck,
            TaskUrlBox, TaskSelectorBox, TaskRowBox, NextPageBox, FileSelectorBox,
            ExtensionsBox, InstructionBox, MaxTasksBox, TimeoutBox, RetriesBox,
            DedupeCheck, SaveResultsCheck,
            TaskNameBox, ResultsRootNameBox, ResultsLocationBox,
            ScheduleCheck, ScheduleTimesBox,
            LoginStateDirBox, LoginCheckSelectorBox, LoginKeywordsBox,
            ClearLoginStateButton, SaveConfigButton, RunOnceButton,
            // 任务十七新增的控件也纳入平台降级（非 Windows 上自动化整体不可用）
            BrowserChoiceBox, ReuseModeBox, CdpPortBox, PickTargetBox,
            PickOpenButton, PickBeginButton, PickCancelButton, PickCloseButton,
            LaunchDebugBrowserButton, PickReloadTabButton,
            // v0.2.2 第 2 批新增的控件同样属于"自动化能力"：非 Windows 上一起禁用，
            // 免得让人填完模板、跑完向导才发现整页跑不起来。
            TemplateCombo, ApplyTemplateButton, SaveTemplateButton,
            TestSelectorButton, OpenResultsButton, BrowseResultsButton,
            WizardButton, WizardStep1Button, WizardStep2Button, WizardStep3Button,
            WizardStep4Button, ExportConfigButton, ImportConfigButton,
        };

        SaveConfigButton.Click += async (_, _) => await SaveAsync();
        ReloadConfigButton.Click += async (_, _) => await RefreshAsync();
        RunOnceButton.Click += async (_, _) => await RunOnceAsync();
        StopAutomationButton.Click += async (_, _) => await StopAsync();
        ClearLoginStateButton.Click += async (_, _) => await ClearLoginAsync();
        // 结果目录预览：任一相关字段变化就更新（不请求后端，纯本地拼路径）。
        ResultsLocationBox.TextChanged += (_, _) => UpdateResultsPreview();
        ResultsRootNameBox.TextChanged += (_, _) => UpdateResultsPreview();
        TaskNameBox.TextChanged += (_, _) => UpdateResultsPreview();

        // ---- v0.2.2 第 2 批：安全协议联动 / 结果目录 / 登录态 / 模板 / 字段帮助 /
        //      测试选择器 / 测试向导 / 配置导入导出 ----
        // 安全协议 ↔ 总开关联动：没接受协议就禁用总开关并强制取消（对齐 WPF 的 SyncSafetyUi）
        SafetyCheck.IsCheckedChanged += (_, _) => SyncSafetyUi();
        OpenResultsButton.Click += async (_, _) => await OpenResultsAsync();
        BrowseResultsButton.Click += async (_, _) => await BrowseResultsAsync();
        TestSelectorButton.Click += async (_, _) => await TestSelectorAsync();
        TemplateCombo.SelectionChanged += (_, _) => { /* 只在点「套用模板」时才写入 */ };
        ApplyTemplateButton.Click += (_, _) => ApplySelectedTemplate();
        SaveTemplateButton.Click += (_, _) => ToggleSaveTemplateForm(true);
        SaveTemplateConfirmButton.Click += async (_, _) => await SaveTemplateAsync();
        SaveTemplateCancelButton.Click += (_, _) => ToggleSaveTemplateForm(false);
        AutomationGuideButton.Click += (_, _) => ShowGuide();
        WizardButton.Click += async (_, _) => await RunWizardAsync(1);
        WizardStep1Button.Click += async (_, _) => await RunWizardAsync(1);
        WizardStep2Button.Click += async (_, _) => await RunWizardAsync(2);
        WizardStep3Button.Click += async (_, _) => await RunWizardAsync(3);
        WizardStep4Button.Click += async (_, _) => await RunWizardAsync(4);
        WizardResultCloseButton.Click += (_, _) => WizardResultPanel.IsVisible = false;
        ExportConfigButton.Click += async (_, _) => await ExportConfigAsync();
        ImportConfigButton.Click += async (_, _) => await ImportConfigAsync();
        // 逐字段「?」：点开显示说明（Avalonia 侧统一走骨架右侧的字段帮助面板）
        foreach (Button help in new[]
                 {
                     HelpTaskListUrlButton, HelpTaskListSelector, HelpTaskRowSelector,
                     HelpNextPageSelector, HelpTaskDetailFileSelector, HelpFileExtensions,
                     HelpInstruction, HelpLoginStateDir, HelpLoginCheckSelector, HelpLoginUrlKeywords,
                 })
        {
            help.Content = "?";
            ToolTip.SetTip(help, Loc.T("settings.automation.fieldHelpTip", "查看填写说明"));
            help.Click += (_, _) => ShowFieldHelp((help.Tag as string) ?? "");
        }
        // 输入框获得焦点时把字段说明切到该字段（WPF 的 AutomationField_GotFocus 同一行为）
        foreach (TextBox box in new[]
                 {
                     TaskUrlBox, TaskSelectorBox, TaskRowBox, NextPageBox, FileSelectorBox,
                     ExtensionsBox, InstructionBox, LoginStateDirBox, LoginCheckSelectorBox,
                     LoginKeywordsBox,
                 })
        {
            box.GotFocus += (_, _) => ShowFieldHelp((box.Tag as string) ?? "");
        }

        // ---- 任务十七：可视化选取与浏览器复用 ----
        PickOpenButton.Click += async (_, _) => await PickStepAsync("open");
        PickBeginButton.Click += async (_, _) => await PickStepAsync("begin");
        PickCancelButton.Click += async (_, _) => await PickStepAsync("cancel");
        PickCloseButton.Click += async (_, _) => await PickStepAsync("close");
        PickReloadTabButton.Click += async (_, _) => await PickStepAsync("open");   // 重新取"当前标签页"
        LaunchDebugBrowserButton.Click += async (_, _) => await LaunchDebugBrowserAsync();
        // 选了别的浏览器/复用方式就立刻写回后端：这两个开关决定"这次会不会复用成功"，
        // 等用户去点整个页面的「保存」再生效太绕（页脚保存按钮仍会再同步一次，幂等）。
        BrowserChoiceBox.SelectionChanged += async (_, _) => await SaveReuseSettingsAsync();
        ReuseModeBox.SelectionChanged += async (_, _) => await SaveReuseSettingsAsync();
        CdpPortBox.LostFocus += async (_, _) => await SaveReuseSettingsAsync();
        _pickTimer.Tick += async (_, _) => await PollPickAsync();
    }

    public string TabKey => "settings.tab.automation";
    public string TabFallback => "自动化";
    public string AutomationId => "SettingsTabAutomation";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }

    // -----------------------------------------------------------------
    private async Task<JsonDocument?> SendAsync(object request, int timeoutSeconds = 90)
    {
        if (_host is null)
            return null;
        return await _host.State.Backend.SendAsync(request, idleTimeout: TimeSpan.FromSeconds(timeoutSeconds));
    }

    private void SetInputsEnabled(bool enabled)
    {
        foreach (Control control in _inputs)
            control.IsEnabled = enabled;
    }

    /// <summary>给 ComboBox 填"(取值, 显示名)"并选中某个取值（显示名由语言包提供）。</summary>
    private void FillCombo(ComboBox box, string[] values, Func<string, string> label, string? current)
    {
        _fillingCombos = true;
        try
        {
            box.Items.Clear();
            int selected = 0;
            for (int i = 0; i < values.Length; i++)
            {
                box.Items.Add(new ComboBoxItem { Content = label(values[i]), Tag = values[i] });
                if (string.Equals(values[i], current, StringComparison.OrdinalIgnoreCase))
                    selected = i;
            }
            box.SelectedIndex = box.ItemCount > 0 ? selected : -1;
        }
        finally
        {
            _fillingCombos = false;
        }
    }

    private static string ComboValue(ComboBox box) =>
        (box.SelectedItem as ComboBoxItem)?.Tag as string ?? "";

    private void SelectCombo(ComboBox box, string? value)
    {
        _fillingCombos = true;
        try
        {
            for (int i = 0; i < box.ItemCount; i++)
            {
                if (box.Items[i] is ComboBoxItem item
                    && string.Equals(item.Tag as string, value, StringComparison.OrdinalIgnoreCase))
                {
                    box.SelectedIndex = i;
                    return;
                }
            }
        }
        finally
        {
            _fillingCombos = false;
        }
    }

    private string BrowserLabel(string name) => name switch
    {
        "edge" => "Microsoft Edge",
        "chrome" => "Google Chrome",
        "chromium" => "Chromium (Playwright)",
        "firefox" => "Mozilla Firefox",
        _ => Loc.T("settings.automation.browserAuto", "自动（依次尝试 Edge → Chrome → Chromium）"),
    };

    private string ReuseModeLabelText(string mode) => mode switch
    {
        "required" => Loc.T("settings.automation.reuseRequired",
            "必须复用：连不上已打开的浏览器就报错，不新开实例"),
        "off" => Loc.T("settings.automation.reuseOff",
            "不复用：永远用程序自己新开的浏览器实例（要重新登录）"),
        _ => Loc.T("settings.automation.reuseAuto",
            "自动（默认）：先复用已打开的浏览器，连不上才新开实例并提示需要重新登录"),
    };

    private string PickTargetLabelText(string target) => target switch
    {
        "task_row" => Loc.T("settings.automation.pickTargetRow", "任务行容器"),
        "next_page" => Loc.T("settings.automation.pickTargetNextPage", "「下一页」按钮"),
        "task_detail_file" => Loc.T("settings.automation.pickTargetFile", "详情页附件"),
        "login_check" => Loc.T("settings.automation.pickTargetLogin", "登录后才出现的元素"),
        _ => Loc.T("settings.automation.pickTargetList", "任务列表元素"),
    };

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;

        // 平台能力：非 Windows 上自动化不可用（降级边界之一）。
        _available = PlatformInfo.Current.IsWindows;
        PlatformBanner.IsVisible = !_available;
        if (!_available)
        {
            PlatformBannerTitle.Text = Loc.T("settings.automation.platformBannerTitle",
                "⚠️ 本平台（{0}）不支持自动化", PlatformInfo.Current.DisplayName);
            PlatformBannerBody.Text = Loc.T("settings.automation.platformBannerBody",
                "企业内网自动化需要驱动本机的 Microsoft Edge，目前只在 Windows 上提供。"
                + "这是平台限制，不是缺少依赖 —— 装任何包都不会让它在 Linux/macOS 上可用。"
                + "替代做法：在 Windows 机器上运行自动化，把结果目录复制过来使用；"
                + "其余功能（对话、文档处理、检索、智能体）在本平台一切正常。");
        }
        SetInputsEnabled(_available);
        // 停止按钮只在运行时可用，单独处理（这里先按平台能力给值）。
        StopAutomationButton.IsEnabled = _available;

        using JsonDocument? doc = await SendAsync(new { action = "automation_config" });
        if (doc is null)
        {
            AutomationStatusText.Text = Loc.T("settings.automation.stateFailed",
                "⚠️ 后端不可用，无法读取自动化配置");
            return;
        }
        if (!doc.RootElement.TryGetProperty("automation", out JsonElement automation))
            return;

        ApplyAutomation(automation);
        UpdateResultsPreview();
        AutomationStatusText.Text = Loc.T("settings.automation.stateIdle", "空闲");
        // 顺便把"复用能力"、"选取状态"、"登录态"与字段帮助读一次：
        // 后端持有状态机与帮助文本，界面重新打开时要能恢复显示。
        await RefreshBrowserReuseAsync();
        await PickStepAsync("status");
        await RefreshLoginStateAsync();
        await LoadAutomationHelpAsync();
    }

    /// <summary>把后端回传的 <c>automation</c> 对象渲染到控件上（配置读取与导入共用）。</summary>
    private void ApplyAutomation(JsonElement automation)
    {
        _loading = true;
        try
        {
            SafetyCheck.IsChecked = GetBool(automation, "safety_accepted");
            AutomationEnabledCheck.IsChecked = GetBool(automation, "enabled");
            TaskUrlBox.Text = GetString(automation, "task_list_url");
            TaskSelectorBox.Text = GetString(automation, "task_list_selector");
            TaskRowBox.Text = GetString(automation, "task_row_selector");
            NextPageBox.Text = GetString(automation, "next_page_selector");
            FileSelectorBox.Text = GetString(automation, "task_detail_file_selector");
            ExtensionsBox.Text = GetString(automation, "file_extensions");
            InstructionBox.Text = GetString(automation, "instruction");
            MaxTasksBox.Text = GetInt(automation, "max_tasks", 20).ToString();
            TimeoutBox.Text = GetInt(automation, "page_timeout_ms", 30000).ToString();
            RetriesBox.Text = GetInt(automation, "retries", 1).ToString();
            DedupeCheck.IsChecked = GetBoolOr(automation, "dedupe", true);
            SaveResultsCheck.IsChecked = GetBoolOr(automation, "save_results", true);
            TaskNameBox.Text = GetString(automation, "task_name");
            ResultsRootNameBox.Text = GetString(automation, "results_root_name");
            ResultsLocationBox.Text = GetString(automation, "results_location");
            ScheduleCheck.IsChecked = GetBool(automation, "schedule_enabled");
            ScheduleTimesBox.Text = GetString(automation, "schedule_times");
            LoginStateDirBox.Text = GetString(automation, "login_state_dir");
            LoginCheckSelectorBox.Text = GetString(automation, "login_check_selector");
            LoginKeywordsBox.Text = GetString(automation, "login_url_keywords");
            SelectCombo(BrowserChoiceBox, GetString(automation, "browser") is { Length: > 0 } b ? b : "auto");
            SelectCombo(ReuseModeBox, GetString(automation, "browser_reuse") is { Length: > 0 } r ? r : "auto");
            CdpPortBox.Text = GetInt(automation, "cdp_port", 9222).ToString();
        }
        finally
        {
            _loading = false;
        }
        SyncSafetyUi();
    }

    /// <summary>安全协议 ↔ 总开关联动（对齐 WPF 的 SyncSafetyUi）。</summary>
    private void SyncSafetyUi()
    {
        bool accepted = SafetyCheck.IsChecked == true;
        // 没接受协议时把总开关强制取消 + 禁用：不能让人"看着是开着的，其实跑不起来"。
        // 顺序有意如此：先取消勾选再禁用，否则控件会停在"禁用但仍显示勾选"的状态。
        if (!accepted && AutomationEnabledCheck.IsChecked == true)
            AutomationEnabledCheck.IsChecked = false;
        AutomationEnabledCheck.IsEnabled = _available && accepted;

        if (!accepted && !string.IsNullOrEmpty(AutomationStatusText.Text))
        {
            // 只在已经有状态时补一句原因，避免页面刚打开就刷满提示
            AutomationStatusText.Text = Loc.T("settings.automation.safetyDisabled",
                "未接受安全协议：自动化已关闭且不可启用");
        }
    }
    /// <summary>
    /// 结果目录预览（对齐 WPF 的 UpdateResultsPreview）。
    ///
    /// **占位符必须是 4 个**：语言包里的 `settings.automation.resultsPreview` 是
    /// "目录结构：{0}\{1}\{2}\{3}\（…）"（程序根 / 结果目录名 / 日期 / 任务名）。
    /// 这里此前只传了 3 个，`Loc.T` 的 `string.Format` 抛 `FormatException` 后
    /// 走 catch 返回**原模板**——于是界面上显示的是一串 `{0}\{1}\{2}\{3}` 字面量。
    /// 本轮（第 2 批）按 WPF 的参数个数与顺序改对，并补上日期那一层。
    /// </summary>
    private void UpdateResultsPreview()
    {
        if (_loading)
            return;
        string root = ResultsRootNameBox.Text?.Trim() ?? "";
        if (root.Length == 0)
            root = Loc.T("settings.automation.defaultResultsRoot", "results");
        string task = TaskNameBox.Text?.Trim() ?? "";
        if (task.Length == 0)
            task = Loc.T("settings.automation.defaultTaskName", "auto-task");
        string location = ResultsLocationBox.Text?.Trim() ?? "";
        if (location.Length == 0)
            location = Loc.T("settings.automation.programRoot", "程序根目录");
        ResultsPreviewText.Text = Loc.T("settings.automation.resultsPreview",
            "目录结构：{0}\\{1}\\{2}\\{3}\\（同名任务自动加 -1、-2；内含「result.txt」与下载的原始文件）",
            location, root, $"{DateTime.Now:yyyy-MM-dd}", task);
    }

    /// <summary>收集当前控件值 → patch（字段名与 WPF 版逐一对齐）。</summary>
    private Dictionary<string, object?> BuildPatch() => new()
    {
        ["safety_accepted"] = SafetyCheck.IsChecked == true,
        ["safety_version"] = SafetyVersion,
        ["enabled"] = AutomationEnabledCheck.IsChecked == true,
        ["task_list_url"] = (TaskUrlBox.Text ?? "").Trim(),
        ["task_list_selector"] = (TaskSelectorBox.Text ?? "").Trim(),
        ["task_row_selector"] = (TaskRowBox.Text ?? "").Trim(),
        ["next_page_selector"] = (NextPageBox.Text ?? "").Trim(),
        ["task_detail_file_selector"] = (FileSelectorBox.Text ?? "").Trim(),
        ["file_extensions"] = (ExtensionsBox.Text ?? "").Trim(),
        ["instruction"] = (InstructionBox.Text ?? "").Trim(),
        ["max_tasks"] = ParseInt(MaxTasksBox.Text, 20),
        ["page_timeout_ms"] = ParseInt(TimeoutBox.Text, 30000),
        ["retries"] = ParseInt(RetriesBox.Text, 1),
        ["dedupe"] = DedupeCheck.IsChecked == true,
        ["save_results"] = SaveResultsCheck.IsChecked == true,
        ["task_name"] = (TaskNameBox.Text ?? "").Trim(),
        ["results_root_name"] = (ResultsRootNameBox.Text ?? "").Trim(),
        ["results_location"] = (ResultsLocationBox.Text ?? "").Trim(),
        ["schedule_enabled"] = ScheduleCheck.IsChecked == true,
        ["schedule_times"] = (ScheduleTimesBox.Text ?? "").Trim(),
        ["login_state_dir"] = (LoginStateDirBox.Text ?? "").Trim(),
        ["login_check_selector"] = (LoginCheckSelectorBox.Text ?? "").Trim(),
        ["login_url_keywords"] = (LoginKeywordsBox.Text ?? "").Trim(),
        // 任务十七：浏览器与复用设置也在同一次保存里同步（与上面几个字段同一口径）
        ["browser"] = ComboValue(BrowserChoiceBox) is { Length: > 0 } value ? value : "auto",
        ["browser_reuse"] = ComboValue(ReuseModeBox) is { Length: > 0 } reuse ? reuse : "auto",
        ["cdp_port"] = ParseInt(CdpPortBox.Text, 9222),
    };

    private static int ParseInt(string? text, int fallback) =>
        int.TryParse((text ?? "").Trim(), out int value) ? value : fallback;

    private async Task SaveAsync()
    {
        if (_host is null || !_available)
            return;
        if (AutomationEnabledCheck.IsChecked == true && SafetyCheck.IsChecked != true)
        {
            // 后端也会拒绝，但在这里先说清楚，省一次往返。
            AutomationStatusText.Text = Loc.T("settings.automation.enabledNeedsSafety",
                "⚠️ 启用自动化前需要先勾选并同意安全协议");
            return;
        }
        AutomationStatusText.Text = Loc.T("settings.automation.stateSaving", "⏳ 正在保存…");
        using JsonDocument? doc = await SendAsync(new
        {
            action = "automation_config_set",
            automation = BuildPatch(),
        });
        if (doc is null)
        {
            AutomationStatusText.Text = Loc.T("settings.automation.saveFailed", "❌ 保存失败（后端未响应）");
            return;
        }
        string content = GetString(doc.RootElement, "content");
        AutomationStatusText.Text = content.Length > 0
            ? content
            : Loc.T("settings.automation.saved", "已保存");
        AppendLog(AutomationStatusText.Text);
    }

    /// <summary>骨架的保存按钮会调这里（业务设置的同步入口）。</summary>
    public async Task ApplyToBackendAsync() => await SaveAsync();

    private async Task RunOnceAsync()
    {
        if (_host is null || !_available)
            return;
        if (SafetyCheck.IsChecked != true)
        {
            AutomationStatusText.Text = Loc.T("settings.automation.safetyRequiredText",
                "⚠️ 请先阅读并同意安全协议");
            return;
        }
        using JsonDocument? doc = await SendAsync(new { action = "automation_run" });
        AutomationStatusText.Text = doc is not null
            ? Loc.T("settings.automation.runStarted", "▶ 已开始运行（可在下方日志观察进度）")
            : Loc.T("settings.automation.runFailed", "❌ 启动失败");
    }

    private async Task StopAsync()
    {
        if (_host is null)
            return;
        await SendAsync(new { action = "stop" });
        AutomationStatusText.Text = Loc.T("settings.automation.stopRequested", "⏹ 已请求停止");
    }

    private async Task ClearLoginAsync()
    {
        if (_host is null)
            return;
        await SendAsync(new { action = "automation_clear_login" });
        AutomationStatusText.Text = Loc.T("settings.automation.loginCleared", "🗑 登录态已清除");
    }

    private void AppendLog(string line)
    {
        if (string.IsNullOrWhiteSpace(line))
            return;
        string existing = AutomationLogBox.Text ?? "";
        AutomationLogBox.Text = existing.Length == 0 ? line : existing + Environment.NewLine + line;
    }

    // =================================================================
    // v0.2.2 第 2 批：结果目录 / 登录态 / 模板 / 字段帮助 / 测试选择器 / 测试向导 / 导入导出
    // =================================================================
    //
    // 全部对齐 WPF（`SettingsWindow.xaml.cs` 与 `SettingsWindow.Skills.cs` 的对应方法），
    // 用的后端动作一个字都没改：
    //   automation_status         → 结果根目录（由后端按自己的规则解析"程序根目录"）
    //   automation_login_state    → 登录态只读状态（目录/占用/最后更新，**不含 Cookie 内容**）
    //   automation_field_help     → 逐字段的 label/help/example/step
    //   automation_templates      → 预置模板（内容来自后端语言包，随语言切换）
    //   automation_save_template  → 存为自定义模板
    //   automation_test           → 打开浏览器预览选择器匹配到的元素
    //   automation_wizard / _step → 四步测试向导（第 4 步走流式，可在主窗口停止）
    //   automation_config_export/import → 配置导出/导入（不含登录态与处理记录）

    /// <summary>逐字段的填写说明（`automation_field_help` 回传，键是字段名）。</summary>
    private readonly Dictionary<string, JsonElement> _fieldHelp = new(StringComparer.Ordinal);

    /// <summary>在文件管理器中打开结果根目录（不存在时先创建）。</summary>
    private async Task OpenResultsAsync()
    {
        // 让后端按自己的规则解析"程序根目录"，避免前端猜路径（与 WPF 同一做法）
        using JsonDocument? doc = await SendAsync(new { action = "automation_status" });
        string root = doc is not null
                      && doc.RootElement.TryGetProperty("automation", out JsonElement status)
            ? GetString(status, "results_root")
            : "";
        if (root.Length == 0)
        {
            ResultsStatusText.Text = Loc.T("settings.automation.resultsDirMissing",
                "⚠️ 未取到结果目录，请先保存设置");
            return;
        }
        ResultsStatusText.Text = ShellOpen.TryOpenDirectory(root)
            ? Loc.T("settings.automation.opened", "已打开：{0}", root)
            : Loc.T("settings.automation.resultsDirOpenFailed", "❌ 打开结果目录失败：{0}", root);
    }

    /// <summary>
    /// 「浏览…」选择保存位置。Avalonia 走系统文件夹选择器（`StorageProvider`）——
    /// 与 WPF 的 `OpenFolderDialog` 同一件事，只是 API 不同。
    /// </summary>
    private async Task BrowseResultsAsync()
    {
        TopLevel? top = TopLevel.GetTopLevel(this);
        if (top is null)
            return;
        var folders = await top.StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = Loc.T("settings.automation.resultsLocationPrompt", "选择结果保存位置"),
            AllowMultiple = false,
        });
        if (folders.Count == 0)
            return;
        string? path = folders[0].TryGetLocalPath();
        if (string.IsNullOrEmpty(path))
            return;
        ResultsLocationBox.Text = path;
        UpdateResultsPreview();
    }

    /// <summary>
    /// 读取登录态（只读）。
    ///
    /// 只显示"数据目录在哪、占多大、最后改动时间"，**不显示任何 Cookie 内容** ——
    /// 界面上不该出现凭据，也不该给用户"程序在读我的密码"的错觉（WPF 同一口径）。
    /// </summary>
    private async Task RefreshLoginStateAsync()
    {
        using JsonDocument? doc = await SendAsync(new { action = "automation_login_state" });
        if (doc is null || !doc.RootElement.TryGetProperty("login_state", out JsonElement state))
            return;
        bool exists = GetBool(state, "exists");
        string dir = GetString(state, "profile_dir");
        long size = state.TryGetProperty("size_bytes", out JsonElement sizeEl)
                    && sizeEl.TryGetInt64(out long parsed) ? parsed : 0;
        string modified = GetString(state, "modified");
        string detail = exists
            ? Loc.T("settings.automation.loginStateSaved", "登录态：已保存（{0}，约 {1} MB{2}）",
                dir, (size / 1048576.0).ToString("0.0"),
                modified.Length > 0
                    ? Loc.T("settings.automation.loginStateModified", "，最后更新 {0}", modified) : "")
            : Loc.T("settings.automation.loginStateEmpty",
                "登录态：尚未保存（{0} 不存在）。第一次运行自动化时会在浏览器里引导你手动登录。", dir);
        if (GetBool(state, "legacy_exists"))
            detail += Loc.T("settings.automation.loginStateLegacy", "　旧备份文件 browser_state.json 仍在。");
        LoginStateText.Text = detail;
    }

    /// <summary>拉取字段级帮助与模板库（内容随界面语言切换）。</summary>
    private async Task LoadAutomationHelpAsync()
    {
        using (JsonDocument? help = await SendAsync(new { action = "automation_field_help" }))
        {
            if (help is not null
                && help.RootElement.TryGetProperty("help", out JsonElement array)
                && array.ValueKind == JsonValueKind.Array)
            {
                _fieldHelp.Clear();
                foreach (JsonElement item in array.EnumerateArray())
                {
                    string field = GetString(item, "field");
                    if (field.Length > 0)
                        _fieldHelp[field] = item.Clone();
                }
            }
        }

        using (JsonDocument? templates = await SendAsync(new { action = "automation_templates" }))
        {
            if (templates is not null && templates.RootElement.TryGetProperty("templates", out JsonElement list))
            {
                string? keep = (TemplateCombo.SelectedItem as TemplateItem)?.Id;
                var items = new List<TemplateItem>();
                if (list.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement item in list.EnumerateArray())
                    {
                        items.Add(new TemplateItem(
                            GetString(item, "id"), GetString(item, "label"),
                            GetString(item, "instruction"), GetString(item, "extensions")));
                    }
                }
                TemplateCombo.ItemsSource = items;
                if (items.Count > 0)
                    SelectTemplate(keep);
            }
        }
    }

    private void SelectTemplate(string? id)
    {
        if (TemplateCombo.ItemsSource is not List<TemplateItem> items)
            return;
        int index = 0;
        for (int i = 0; i < items.Count; i++)
        {
            if (string.Equals(items[i].Id, id, StringComparison.OrdinalIgnoreCase))
            {
                index = i;
                break;
            }
        }
        TemplateCombo.SelectedIndex = index;
    }

    /// <summary>套用模板：只填"指令"与"文件后缀"两个字段，其余按实际网站自己调。</summary>
    private void ApplySelectedTemplate()
    {
        if (TemplateCombo.SelectedItem is not TemplateItem template)
        {
            TemplateStatusText.Text = Loc.T("settings.automation.templateNotSelected",
                "⚠️ 请先在上面的下拉框里选一个模板");
            return;
        }
        if (template.Instruction.Length > 0)
            InstructionBox.Text = template.Instruction;
        if (template.Extensions.Length > 0)
            ExtensionsBox.Text = template.Extensions;
        UpdateResultsPreview();
        TemplateStatusText.Text = Loc.T("automation.template.custom_applied",
            "✅ 已套用模板「{name}」", template.Label);
    }

    private void ToggleSaveTemplateForm(bool show)
    {
        SaveTemplatePanel.IsVisible = show;
        if (show)
        {
            SaveTemplateNameBox.Text = "";
            SaveTemplateNameBox.Focus();
        }
    }

    private async Task SaveTemplateAsync()
    {
        string name = (SaveTemplateNameBox.Text ?? "").Trim();
        if (name.Length == 0)
        {
            TemplateStatusText.Text = Loc.T("settings.automation.saveTemplatePrompt", "模板名称");
            return;
        }
        ToggleSaveTemplateForm(false);
        using JsonDocument? doc = await SendAsync(new
        {
            action = "automation_save_template",
            name,
            template = new
            {
                label = name,
                instruction = InstructionBox.Text,
                extensions = ExtensionsBox.Text,
            },
        });
        string content = doc is null ? "" : GetString(doc.RootElement, "content");
        TemplateStatusText.Text = content.Length > 0
            ? content
            : Loc.T("automation.template.custom_saved", "✅ 已保存为自定义模板「{name}」", name);
        await LoadAutomationHelpAsync();
    }

    /// <summary>
    /// 「?」：把该字段的说明摆到右侧字段帮助面板；再点一次同样的字段时收起
    /// （WPF 是弹 MessageBox，Avalonia 侧统一走骨架的面板 —— 见 ISettingsHost.ShowFieldHelp）。
    /// </summary>
    private void ShowFieldHelp(string field)
    {
        if (_host is null)
            return;
        if (field.Length == 0 || !_fieldHelp.TryGetValue(field, out JsonElement help))
        {
            _host.ShowFieldHelp(
                Loc.T("settings.automation.helpPlaceholderTitle", "❓ 字段说明"),
                Loc.T("settings.automation.helpPlaceholderBody",
                    "把光标放进下面任意一个输入框（或点字段旁的「?」），这里会显示该字段的通俗说明与示例值。"
                    + "完整指南见发布包 docs\\<locale>\\AUTOMATION_GUIDE.md。"));
            return;
        }
        string example = GetString(help, "example");
        _host.ShowFieldHelp(
            "? " + GetString(help, "label"),
            GetString(help, "help"),
            example.Length > 0 ? Loc.T("automation.help.example", "示例") + ": " + example : "");
    }

    /// <summary>
    /// 「📖 填写帮助」：把全部字段说明按"步骤"分组拼成一段文本展示。
    ///
    /// 与 WPF 的差别只有承载方式：WPF 用 `MessageBox`，这里用页面内的一块可滚动文本
    /// （行为等价：内容一致、可复制；少一个必须逐次关闭的模态窗口）。
    /// </summary>
    private void ShowGuide()
    {
        var builder = new System.Text.StringBuilder();
        builder.AppendLine(Loc.T("automation.guide.title", "自动化配置填写帮助"));
        builder.AppendLine();
        string lastStep = "";
        foreach (JsonElement help in _fieldHelp.Values)
        {
            string step = GetString(help, "step");
            string stepTitle = GetString(help, "stepTitle");
            if (step != lastStep && stepTitle.Length > 0)
            {
                builder.AppendLine("── " + stepTitle + " ──");
                lastStep = step;
            }
            string required = GetBool(help, "required")
                ? Loc.T("automation.help.required", "必填")
                : Loc.T("automation.help.optional", "可选");
            builder.AppendLine(Loc.T("settings.automation.guideItem", "【{0}】（{1}）",
                GetString(help, "label"), required));
            builder.AppendLine("  " + GetString(help, "help"));
            string example = GetString(help, "example");
            if (example.Length > 0)
                builder.AppendLine($"  {Loc.T("automation.help.example", "示例")}: {example}");
            builder.AppendLine();
        }
        builder.AppendLine(Loc.T("automation.guide.hint",
            "也可以查看完整文档：docs/<locale>/AUTOMATION_GUIDE.md（发布包内 docs 目录）"));

        WizardResultPanel.IsVisible = true;
        WizardResultTitle.Text = Loc.T("automation.guide.title", "自动化配置填写帮助");
        WizardResultText.Text = builder.ToString();
        AutomationGuideButton.Focus();
    }

    /// <summary>「测试选择器」：打开浏览器访问该网址，预览选择器匹配到的元素。</summary>
    private async Task TestSelectorAsync()
    {
        string url = (TaskUrlBox.Text ?? "").Trim();
        string selector = (TaskSelectorBox.Text ?? "").Trim();
        if (url.Length == 0 || selector.Length == 0)
        {
            AppendLog(Loc.T("settings.automation.needUrlAndSelector", "❌ 请先填写任务列表网址与元素位置"));
            return;
        }
        TestSelectorButton.IsEnabled = false;
        AppendLog(Loc.T("settings.automation.testingSelector", "🔍 正在测试选择器：{0}", selector));
        AutomationStatusText.Text = Loc.T("settings.automation.stateTesting",
            "状态：测试选择器中（浏览器已打开）");
        try
        {
            using JsonDocument? doc = await SendAsync(new { action = "automation_test", url, selector },
                timeoutSeconds: 180);
            AppendLog(doc is null
                ? Loc.T("settings.automation.testFailed", "❌ 测试失败：{0}",
                    Loc.T("settings.automation.pickBackendDown", "⚠️ 后端没有响应"))
                : GetString(doc.RootElement, "content"));
        }
        finally
        {
            TestSelectorButton.IsEnabled = _available;
            AutomationStatusText.Text = Loc.T("settings.automation.stateIdle", "空闲");
        }
    }

    /// <summary>
    /// 四步测试向导。第 4 步要在**真实页面上跑一个任务**，所以走流式（可在主窗口点 ⏹ 停止）；
    /// 前三步是同步的 `automation_wizard_step`。
    /// </summary>
    private async Task RunWizardAsync(int step)
    {
        if (_host is null || !_available)
            return;
        WizardResultPanel.IsVisible = true;
        WizardResultTitle.Text = Loc.T("settings.automation.wizardStep" + step, "第 " + step + " 步");
        WizardResultText.Text = Loc.T("settings.automation.wizardRunning", "⏳ 正在执行，请稍候…");
        AutomationStatusText.Text = Loc.T("settings.automation.wizardRunning", "⏳ 正在执行，请稍候…");

        if (step == 4)
        {
            // 流式：每次 delta 更新一次结果文本，让用户看到"真的在跑"
            var log = new System.Text.StringBuilder();
            await _host.State.ActiveBackend.SendStreamAsync(
                new { action = "automation_wizard", step },
                onDelta: _ => { },
                onContext: (_, _) => { },
                idleTimeout: TimeSpan.FromMinutes(5),
                onEvent: (name, payload) =>
                {
                    // 与 WPF 同一口径：把后端推来的可读文本追加到结果区
                    if (payload.ValueKind == JsonValueKind.Object
                        && payload.TryGetProperty("content", out JsonElement content)
                        && content.ValueKind == JsonValueKind.String)
                    {
                        log.Append(content.GetString());
                        string text = log.ToString().Trim();
                        Dispatcher.UIThread.Post(() => WizardResultText.Text = text);
                    }
                },
                onDeltaChannel: (_, _, _) => { });
            if (log.Length == 0)
                WizardResultText.Text = Loc.T("settings.automation.wizardDone", "✅ 这一步通过");
        }
        else
        {
            using JsonDocument? doc = await SendAsync(
                new { action = "automation_wizard_step", step }, timeoutSeconds: 300);
            if (doc is null || !doc.RootElement.TryGetProperty("wizard", out JsonElement wizard))
            {
                WizardResultText.Text = doc is null
                    ? Loc.T("settings.automation.pickBackendDown", "⚠️ 后端没有响应")
                    : GetString(doc.RootElement, "content");
                AutomationStatusText.Text = Loc.T("settings.automation.wizardFailed", "❌ 这一步没通过");
                return;
            }
            bool ok = GetBool(wizard, "ok");
            var builder = new System.Text.StringBuilder();
            builder.AppendLine(ok
                ? Loc.T("settings.automation.wizardDone", "✅ 这一步通过")
                : Loc.T("settings.automation.wizardFailed", "❌ 这一步没通过"));
            builder.AppendLine();
            builder.AppendLine(GetString(wizard, "detail"));
            if (wizard.TryGetProperty("suggestions", out JsonElement suggestions)
                && suggestions.ValueKind == JsonValueKind.Array)
            {
                var lines = new List<string>();
                foreach (JsonElement item in suggestions.EnumerateArray())
                {
                    if (item.ValueKind == JsonValueKind.String)
                        lines.Add("· " + item.GetString());
                }
                if (lines.Count > 0)
                {
                    builder.AppendLine();
                    builder.AppendLine(Loc.T("settings.automation.wizardSuggestions", "建议："));
                    foreach (string line in lines)
                        builder.AppendLine(line);
                }
            }
            WizardResultText.Text = builder.ToString().Trim();
            AutomationStatusText.Text = Loc.T("settings.automation.wizardStepHead", "{0}（{1}）",
                ok ? Loc.T("settings.automation.wizardDone", "✅ 这一步通过")
                   : Loc.T("settings.automation.wizardFailed", "❌ 这一步没通过"),
                Loc.T("settings.automation.wizardStep" + step, "第 " + step + " 步"));
        }
    }

    /// <summary>导出自动化配置（`*.echodesk-auto`；不含登录态、处理记录与临时目录）。</summary>
    private async Task ExportConfigAsync()
    {
        TopLevel? top = TopLevel.GetTopLevel(this);
        if (top is null)
            return;
        var file = await top.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
        {
            Title = Loc.T("settings.automation.exportConfig", "导出配置（.echodesk-auto）"),
            SuggestedFileName = "echodesk-automation.echodesk-auto",
            DefaultExtension = "echodesk-auto",
            FileTypeChoices = new[]
            {
                new FilePickerFileType(Loc.T("settings.automation.configFilterDesc", "EchoDesk 自动化配置"))
                {
                    Patterns = new[] { "*.echodesk-auto" },
                },
            },
        });
        string? path = file?.TryGetLocalPath();
        if (string.IsNullOrEmpty(path))
            return;
        using JsonDocument? doc = await SendAsync(new { action = "automation_config_export", path });
        ResultsStatusText.Text = doc is null
            ? Loc.T("settings.automation.saveFailed", "❌ 保存失败：{0}",
                Loc.T("settings.automation.pickBackendDown", "⚠️ 后端没有响应"))
            : GetString(doc.RootElement, "content");
    }

    /// <summary>导入自动化配置并**立刻回填界面**（与 WPF 的 ImportConfigButton_Click 一致）。</summary>
    private async Task ImportConfigAsync()
    {
        TopLevel? top = TopLevel.GetTopLevel(this);
        if (top is null)
            return;
        var files = await top.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = Loc.T("settings.automation.importConfig", "导入配置（.echodesk-auto）"),
            AllowMultiple = false,
            FileTypeFilter = new[]
            {
                new FilePickerFileType(Loc.T("settings.automation.configFilterDesc", "EchoDesk 自动化配置"))
                {
                    Patterns = new[] { "*.echodesk-auto", "*.json" },
                },
                new FilePickerFileType(Loc.T("settings.automation.allFilesFilterDesc", "所有文件"))
                {
                    Patterns = new[] { "*" },
                },
            },
        });
        if (files.Count == 0)
            return;
        string? path = files[0].TryGetLocalPath();
        if (string.IsNullOrEmpty(path))
            return;
        using JsonDocument? doc = await SendAsync(new { action = "automation_config_import", path });
        if (doc is null)
        {
            ResultsStatusText.Text = Loc.T("settings.automation.saveFailed", "❌ 保存失败：{0}",
                Loc.T("settings.automation.pickBackendDown", "⚠️ 后端没有响应"));
            return;
        }
        ResultsStatusText.Text = GetString(doc.RootElement, "content");
        if (doc.RootElement.TryGetProperty("automation", out JsonElement automation))
        {
            ApplyAutomation(automation);
            UpdateResultsPreview();
        }
    }

    /// <summary>自动化模板（内容全部来自后端语言包，随界面语言切换）。</summary>
    private sealed record TemplateItem(string Id, string Label, string Instruction, string Extensions)
    {
        public override string ToString() => Label;
    }

    // -----------------------------------------------------------------
    // 探针接口（`EchoDesk --probe pages` 用：模板 / 字段帮助 / 安全联动 / 结果预览）
    // -----------------------------------------------------------------

    /// <summary>本页第 2 批新增部分的可观测状态。</summary>
    public string AutomationSnapshot()
    {
        string preview = ResultsPreviewText.Text ?? "";
        return string.Join("; ", new[]
        {
            $"templateCount={TemplateCombo.ItemCount}",
            $"fieldHelpCount={_fieldHelp.Count}",
            // 预览里出现 {0} 说明 string.Format 抛过 FormatException（本轮修的就是它）
            $"previewPlaceholders={(preview.Contains("{0}") ? 1 : 0)}",
            $"previewHasDate={(preview.Contains(DateTime.Now.ToString("yyyy-MM-dd")) ? 1 : 0)}",
            // 安全协议联动：总开关的可用性必须与协议勾选一致
            $"safetyAccepted={(SafetyCheck.IsChecked == true ? 1 : 0)}",
            $"enabledEditable={(AutomationEnabledCheck.IsEnabled ? 1 : 0)}",
            $"openResultsButton={(string.IsNullOrEmpty(OpenResultsButton.Content as string) ? 0 : 1)}",
            $"browseResultsButton={(string.IsNullOrEmpty(BrowseResultsButton.Content as string) ? 0 : 1)}",
            $"testSelectorButton={(string.IsNullOrEmpty(TestSelectorButton.Content as string) ? 0 : 1)}",
            $"wizardAndIoButtons={(string.IsNullOrEmpty(WizardButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(WizardStep4Button.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(ExportConfigButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(ImportConfigButton.Content as string) ? 0 : 1)}",
            $"loginStateText={(string.IsNullOrEmpty(LoginStateText.Text) ? 0 : 1)}",
        });
    }

    /// <summary>探针：勾选/取消安全协议（验联动，不需要后端）。</summary>
    public void ProbeSetSafety(bool accepted)
    {
        SafetyCheck.IsChecked = accepted;
        if (accepted)
            AutomationEnabledCheck.IsChecked = true;   // 验"接受协议后总开关可以打开"
        SyncSafetyUi();
    }

    /// <summary>探针：按字段名显示一次字段帮助（验 `automation_field_help` 真的被读进来了）。</summary>
    public void ProbeShowFieldHelp(string field) => ShowFieldHelp(field);

    /// <summary>探针：当前已知的字段帮助键（逗号分隔），供断言"后端确实回了这些字段"。</summary>
    public string FieldHelpKeys()
    {
        var keys = new List<string>(_fieldHelp.Keys);
        return string.Join(",", keys.ToArray());
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

    // =================================================================
    // 任务十七：复用已打开的浏览器 + 两段式可视化选取
    // =================================================================
    //
    // 这一块的**文案几乎都来自后端**（automation.picker.* / automation.reuse.*），
    // 前端只把 JSON 里的字符串直接摆到界面上。原因有两个：
    //   ① 状态机在后端，"此刻该显示哪句话"和后端状态是同一件事，分开写一定会漂移；
    //   ② 任务书要求所有面向用户的文案都走语言包、中英双语齐全 —— 后端语言包一份就够，
    //      前端不必再维护一遍同样的句子（本工程的语言包目录不在本次改动范围内）。

    /// <summary>读一次"复用能力"（浏览器、端口、是否支持、调试启动命令）。</summary>
    private async Task RefreshBrowserReuseAsync()
    {
        using JsonDocument? doc = await SendAsync(new { action = "automation_browser_reuse" });
        if (doc is null)
            return;
        if (!doc.RootElement.TryGetProperty("browser_reuse", out JsonElement reuse))
            return;
        ApplyBrowserReuseState(reuse);
    }

    private void ApplyBrowserReuseState(JsonElement reuse)
    {
        string label = reuse.TryGetProperty("browser_label", out JsonElement lb)
            ? lb.GetString() ?? "" : "";
        string mode = reuse.TryGetProperty("browser_reuse", out JsonElement md)
            ? md.GetString() ?? "" : "auto";
        bool supported = reuse.TryGetProperty("supported", out JsonElement sp) && sp.ValueKind == JsonValueKind.True;
        bool available = reuse.TryGetProperty("available", out JsonElement av) && av.ValueKind == JsonValueKind.True;
        int port = reuse.TryGetProperty("cdp_port", out JsonElement pt) && pt.ValueKind == JsonValueKind.Number
            ? pt.GetInt32() : 9222;
        _debugCommand = reuse.TryGetProperty("debug_command", out JsonElement dc)
            ? dc.GetString() ?? "" : "";
        string note = reuse.TryGetProperty("note", out JsonElement nt) ? nt.GetString() ?? "" : "";

        // 状态行说清楚三件事：这次会不会复用、靠什么复用、现在能不能复用
        string state;
        if (!supported)
            state = note.Length > 0 ? note
                : Loc.T("settings.automation.reuseUnsupported",
                    "当前浏览器不支持复用已打开的浏览器");
        else if (mode == "off")
            state = Loc.T("settings.automation.reuseOffState",
                "已按设置关闭复用：程序会自己新开浏览器实例（需要重新登录）。");
        else
            state = available
                ? Loc.T("settings.automation.reusePortOpen",
                    "✅ 检测到调试端口 {0} 有浏览器在监听：点「打开 / 连接浏览器」即可直接复用它。", port)
                : Loc.T("settings.automation.reusePortClosed",
                    "当前调试端口 {0} 上没有检测到浏览器。点「用调试模式启动浏览器」可以由程序代启动一个；"
                    + "你自己用命令行启动的浏览器（带 --remote-debugging-port={0}）同样能被连上。", port);
        ReuseStateText.Text = Loc.T("settings.automation.reuseStateLine",
            "浏览器：{0} · 复用方式：{1} · 端口 {2}\n{3}", label, ReuseModeLabelText(mode), port, state);

        // 调试启动按钮只在"支持 CDP 且没连上"时才需要；Firefox 上它没有意义（必须禁用而不是
        // 让人点了报错）—— 这正是任务书 F 条要求说清楚的边界。
        LaunchDebugBrowserButton.IsEnabled = _available && supported;
        LaunchDebugBrowserButton.Content = Loc.T("settings.automation.launchDebug", "🚀 用调试模式启动浏览器");
        PickDebugHintText.Text = supported
            ? (_debugCommand.Length > 0
                ? Loc.T("settings.automation.debugCommandHint", "调试启动命令：{0}", _debugCommand)
                : Loc.T("settings.automation.debugNoExeHint",
                    "没有找到浏览器的可执行文件：请手动启动浏览器并加上调试参数，或先在上面换一个浏览器。"))
            : Loc.T("settings.automation.firefoxReuseHint",
                "Firefox 没有 CDP 调试接口，无法复用已打开的浏览器；想复用请把浏览器改成 Chrome、Edge 或 Chromium。");
    }

    /// <summary>浏览器/复用方式/端口改动后立刻写回后端（不必等页脚的"保存"）。</summary>
    private async Task SaveReuseSettingsAsync()
    {
        // `_fillingCombos` 这个条件不能少：它挡住"程序自己填下拉框"触发的事件，
        // 否则界面刚打开时 ApplyTexts 的默认值会先把用户配置好的浏览器覆盖成 auto。
        if (_host is null || !_available || _loading || _fillingCombos)
            return;
        using JsonDocument? doc = await SendAsync(new
        {
            action = "automation_browser_reuse",
            set = new Dictionary<string, object?>
            {
                ["browser"] = ComboValue(BrowserChoiceBox) is { Length: > 0 } b ? b : "auto",
                ["browser_reuse"] = ComboValue(ReuseModeBox) is { Length: > 0 } r ? r : "auto",
                ["cdp_port"] = ParseInt(CdpPortBox.Text, 9222),
            },
        });
        if (doc is not null && doc.RootElement.TryGetProperty("browser_reuse", out JsonElement reuse))
            ApplyBrowserReuseState(reuse);
    }

    private async Task LaunchDebugBrowserAsync()
    {
        if (_host is null || !_available)
            return;
        ReuseStateText.Text = Loc.T("settings.automation.debugLaunching", "⏳ 正在用调试模式启动浏览器…");
        // 冷启动 + 加载数据目录要几秒，这里给足超时（后端自己最多等 25 秒）
        using JsonDocument? doc = await SendAsync(new { action = "automation_launch_debug_browser" },
            timeoutSeconds: 120);
        if (doc is null)
        {
            ReuseStateText.Text = Loc.T("settings.automation.saveFailed", "❌ 保存失败（后端未响应）");
            return;
        }
        string content = GetString(doc.RootElement, "content");
        if (content.Length > 0)
        {
            ReuseStateText.Text = content;
            AppendLog(content);
        }
        if (doc.RootElement.TryGetProperty("browser_debug", out JsonElement debug))
        {
            // 启动完再读一次能力状态：端口就绪后"可以复用"的提示会立刻变绿
            await RefreshBrowserReuseAsync();
        }
    }

    /// <summary>推一下两段式状态机（open / begin / status / cancel / close）。</summary>
    private async Task PickStepAsync(string step)
    {
        if (_host is null || !_available)
            return;
        object request = step == "open"
            ? new { action = "automation_pick_open", step, target = ComboValue(PickTargetBox) is { Length: > 0 } t ? t : "task_list" }
            : new { action = "automation_pick_step", step };
        // 打开/连接浏览器可能要几秒（冷启动更久），超时给宽一点；轮询则必须短，
        // 否则界面会"卡"在一次没必要的等待上。
        using JsonDocument? doc = await SendAsync(request, timeoutSeconds: step == "open" ? 120 : 30);
        if (doc is null)
        {
            PickMessageText.Text = Loc.T("settings.automation.pickBackendDown", "⚠️ 后端没有响应");
            StopPickTimer();
            return;
        }
        string content = GetString(doc.RootElement, "content");
        if (content.Length > 0)
            AppendLog(content);
        if (doc.RootElement.TryGetProperty("pick", out JsonElement pick))
            ApplyPickState(pick);
        else
            PickMessageText.Text = content;
    }

    /// <summary>轮询：后端借这次询问去页面上收一次结果（前端只是心跳）。</summary>
    private async Task PollPickAsync()
    {
        if (_pickPolling)
            return;
        _pickPolling = true;
        try
        {
            await PickStepAsync("status");
        }
        finally
        {
            _pickPolling = false;
        }
    }

    private void StartPickTimer()
    {
        if (!_pickTimer.IsEnabled)
            _pickTimer.Start();
    }

    private void StopPickTimer()
    {
        if (_pickTimer.IsEnabled)
            _pickTimer.Stop();
    }

    /// <summary>把后端回传的状态渲染到界面：按钮可用性 + 提示 + 当前目标页。</summary>
    private void ApplyPickState(JsonElement pick)
    {
        string state = GetString(pick, "state");
        bool picking = state == "picking";
        picking = picking || (pick.TryGetProperty("picking", out JsonElement pk) && pk.ValueKind == JsonValueKind.True);
        bool active = pick.TryGetProperty("active", out JsonElement ac) && ac.ValueKind == JsonValueKind.True;

        // 按钮文案与标签：优先用后端语言包回传的（中英双语天然一致），拿不到时用本地兜底
        if (pick.TryGetProperty("buttons", out JsonElement buttons))
        {
            PickOpenButton.Content = ComboText(buttons, "open", "打开 / 连接浏览器");
            PickBeginButton.Content = ComboText(buttons, "begin", "开始选取");
            PickCancelButton.Content = ComboText(buttons, "cancel", "取消选取");
            PickCloseButton.Content = ComboText(buttons, "close", "结束选取");
            PickReloadTabButton.Content = ComboText(buttons, "reload", "重新读取当前标签页");
            if (buttons.TryGetProperty("debug", out JsonElement dbg) && dbg.ValueKind == JsonValueKind.String)
                LaunchDebugBrowserButton.Content = dbg.GetString();
        }

        PickStateText.Text = Loc.T("settings.automation.pickStateLine",
            "状态：{0}{1}",
            GetString(pick, "state_label"),
            GetString(pick, "last_result_label") is { Length: > 0 } r
                ? Loc.T("settings.automation.pickLastResult", "（上次结果：{0}）", r)
                : "");
        PickReadyPromptText.Text = GetString(pick, "ready_prompt");
        PickHintText.Text = GetString(pick, "hint");
        string url = GetString(pick, "url");
        string title = GetString(pick, "title");
        string browser = GetString(pick, "browser");
        PickTargetUrlText.Text = url.Length > 0
            ? Loc.T("settings.automation.pickCurrentPage",
                "当前目标页：{0}{1}　浏览器：{2}", url,
                title.Length > 0 ? Loc.T("settings.automation.pickPageTitle", "（{0}）", title) : "",
                browser)
            : Loc.T("settings.automation.pickNoPage", "当前还没有目标页。");
        string message = GetString(pick, "message");
        PickMessageText.Text = message.Length > 0 ? message : PickMessageText.Text;

        // 两段式的按钮可用性就是状态机的直接映射：等待准备时只能"开始选取"，
        // 选取中只能"取消选取"——不给用户任何"点错一下就把状态搞乱"的机会。
        PickOpenButton.IsEnabled = _available && !picking;
        PickBeginButton.IsEnabled = _available && !picking && active;
        PickCancelButton.IsEnabled = _available && picking;
        PickCloseButton.IsEnabled = _available && active;
        PickReloadTabButton.IsEnabled = _available && !picking;
        PickTargetBox.IsEnabled = _available && !active;

        if (picking)
            StartPickTimer();
        else
            StopPickTimer();
    }

    private static string ComboText(JsonElement obj, string name, string fallback) =>
        obj.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? (el.GetString() is { Length: > 0 } text ? text : fallback)
            : fallback;

    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        SafetyTitle.Text = Loc.T("settings.automation.sectionSafety", "安全协议");
        SafetyBodyText.Text = Loc.T("settings.automation.safetyBody", "");
        SafetyCheck.Content = Loc.T("settings.automation.toggleSafety", "我已阅读并同意上述安全协议");
        AutomationEnabledCheck.Content = Loc.T("settings.automation.toggleEnabled", "启用自动化");
        BasicSectionTitle.Text = Loc.T("settings.automation.sectionBasic", "任务与选择器");
        TaskListUrlLabel.Text = Loc.T("settings.automation.taskListUrl", "任务列表网址");
        TaskListSelectorLabel.Text = Loc.T("settings.automation.taskListSelector", "任务链接选择器");
        TaskRowSelectorLabel.Text = Loc.T("settings.automation.taskRowSelector", "任务行选择器");
        NextPageSelectorLabel.Text = Loc.T("settings.automation.nextPageSelector", "「下一页」选择器");
        TaskDetailFileSelectorLabel.Text = Loc.T("settings.automation.taskDetailFileSelector", "详情页附件选择器");
        DetailSectionTitle.Text = Loc.T("settings.automation.sectionDetail", "详情页与文件");
        FileExtensionsLabel.Text = Loc.T("settings.automation.fileExtensions", "要下载的文件类型");
        InstructionLabel.Text = Loc.T("settings.automation.instruction", "附加指令");
        ExtensionsHintText.Text = Loc.T("settings.automation.extensionsHint", "常用：合同 pdf, docx · 表格 xlsx, xls");
        RunSectionTitle.Text = Loc.T("settings.automation.sectionRun", "运行参数");
        MaxTasksLabel.Text = Loc.T("settings.automation.maxTasks", "单次最多处理任务数");
        PageTimeoutLabel.Text = Loc.T("settings.automation.pageTimeout", "页面超时（毫秒）");
        RetriesLabel.Text = Loc.T("settings.automation.retries", "失败重试次数");
        DedupeCheck.Content = Loc.T("settings.automation.dedupe", "任务去重（跳过处理过的）");
        SaveResultsCheck.Content = Loc.T("settings.automation.saveResults", "把结果保存到本地");
        ResultSectionTitle.Text = Loc.T("settings.automation.sectionResult", "结果与审计");
        TaskNameLabel.Text = Loc.T("settings.automation.taskName", "任务名称");
        ResultsRootNameLabel.Text = Loc.T("settings.automation.resultsRootName", "结果目录名");
        ResultsLocationLabel.Text = Loc.T("settings.automation.resultsLocation", "保存位置（留空＝程序根目录）");
        ScheduleCheck.Content = Loc.T("settings.automation.scheduleEnabled", "启用定时");
        ScheduleTimesBox.PlaceholderText = Loc.T("settings.automation.scheduleTimes", "定时时间（逗号分隔）");
        LoginSectionTitle.Text = Loc.T("settings.automation.loginSection", "登录态");
        LoginHintText.Text = Loc.T("settings.automation.loginHint", "");
        LoginStateDirLabel.Text = Loc.T("settings.automation.loginStateDir", "登录态目录");
        LoginCheckSelectorLabel.Text = Loc.T("settings.automation.loginCheckSelector", "登录检测元素");
        LoginUrlKeywordsLabel.Text = Loc.T("settings.automation.loginUrlKeywords", "登录后 URL 关键词");
        ClearLoginStateButton.Content = Loc.T("settings.automation.clearLogin", "🗑 清除登录态");
        SaveConfigButton.Content = Loc.T("settings.automation.saveConfig", "保存自动化配置");
        ReloadConfigButton.Content = Loc.T("settings.automation.reloadConfig", "🔄 重新读取");
        RunOnceButton.Content = Loc.T("settings.automation.runOnce", "▶ 立即运行一次");
        StopAutomationButton.Content = Loc.T("settings.automation.stopRun", "⏹ 停止");
        RunLogLabel.Text = Loc.T("settings.automation.runLog", "运行日志");

        // ---- v0.2.2 第 2 批：模板 / 字段帮助 / 结果目录 / 测试向导 / 导入导出 ----
        TemplatesTitle.Text = Loc.T("settings.automation.templates", "配置模板");
        ApplyTemplateButton.Content = Loc.T("settings.automation.applyTemplate", "套用模板");
        SaveTemplateButton.Content = Loc.T("settings.automation.saveTemplate", "存为自定义模板");
        AutomationGuideButton.Content = Loc.T("settings.automation.onlineHelp", "📖 填写帮助");
        ToolTip.SetTip(AutomationGuideButton, Loc.T("settings.automation.onlineHelpTip",
            "在本窗口内查看每个字段的通俗说明、完整示例与排查表"));
        ToolTip.SetTip(TemplateCombo, Loc.T("settings.automation.templateTip",
            "选一个模板一键填入常用配置，再按实际网站微调。"));
        SaveTemplateConfirmButton.Content = Loc.T("settings.button.save", "保存");
        SaveTemplateCancelButton.Content = Loc.T("settings.button.close", "关闭");
        SaveTemplateNameBox.PlaceholderText = Loc.T("settings.automation.saveTemplatePrompt", "模板名称");
        OpenResultsButton.Content = Loc.T("settings.automation.openResults", "📂 打开结果目录");
        ToolTip.SetTip(OpenResultsButton, Loc.T("settings.automation.openResultsTip",
            "在系统文件管理器里打开结果根目录"));
        BrowseResultsButton.Content = Loc.T("settings.automation.browseResults", "浏览…");
        TestSelectorButton.Content = Loc.T("settings.automation.testSelector", "测试选择器");
        ToolTip.SetTip(TestSelectorButton, Loc.T("settings.automation.testSelectorTip",
            "打开浏览器访问该网址，预览选择器匹配到的元素"));
        WizardButton.Content = Loc.T("settings.automation.wizard", "🧪 测试向导");
        ToolTip.SetTip(WizardButton, Loc.T("settings.automation.wizardTip",
            "分四步验证配置：任务列表 → 下一页 → 详情页附件 → 试运行一个任务"));
        WizardStep1Button.Content = Loc.T("settings.automation.wizardStep1", "第 1 步");
        WizardStep2Button.Content = Loc.T("settings.automation.wizardStep2", "第 2 步");
        WizardStep3Button.Content = Loc.T("settings.automation.wizardStep3", "第 3 步");
        WizardStep4Button.Content = Loc.T("settings.automation.wizardStep4", "第 4 步");
        ExportConfigButton.Content = Loc.T("settings.automation.exportConfig", "导出配置");
        ImportConfigButton.Content = Loc.T("settings.automation.importConfig", "导入配置");
        ToolTip.SetTip(ExportConfigButton, Loc.T("settings.automation.exportTip",
            "导出时不包含登录态、处理记录与临时目录，可安全地跨机复用。"));
        ToolTip.SetTip(ImportConfigButton, Loc.T("settings.automation.importTip",
            "导入不会继承安全协议与总开关，需要在本机重新勾选。"));
        ToolTip.SetTip(SaveTemplateButton, Loc.T("settings.automation.saveTemplateTip",
            "把当前的「附加指令」与「文件后缀」存成一个可复用的模板"));
        WizardResultCloseButton.Content = Loc.T("settings.button.close", "关闭");
        ResultsStatusText.Text = "";
        TemplateStatusText.Text = "";

        // ---- 任务十七：可视化选取与浏览器复用 ----
        // 下拉框的**显示名**是前端本地化的（它们是"字段值"的枚举，不是状态文案）；
        // 具体状态与提示语由后端回传，见 ApplyPickState。
        PickSectionTitle.Text = Loc.T("settings.automation.pickSection", "可视化选取（在已打开的浏览器里选）");
        BrowserChoiceLabel.Text = Loc.T("settings.automation.browserChoice", "浏览器");
        ReuseModeLabel.Text = Loc.T("settings.automation.reuseMode", "复用已打开的浏览器");
        CdpPortLabel.Text = Loc.T("settings.automation.cdpPort", "调试端口");
        PickTargetLabel.Text = Loc.T("settings.automation.pickTarget", "选取目标");
        string? keepBrowser = ComboValue(BrowserChoiceBox);
        string? keepReuse = ComboValue(ReuseModeBox);
        string? keepTarget = ComboValue(PickTargetBox);
        FillCombo(BrowserChoiceBox, BrowserChoices, BrowserLabel, keepBrowser);
        FillCombo(ReuseModeBox, ReuseModes, ReuseModeLabelText, keepReuse);
        FillCombo(PickTargetBox, PickTargets, PickTargetLabelText,
            keepTarget is { Length: > 0 } ? keepTarget : "task_list");
        PickHintText.Text = Loc.T("settings.automation.pickHintIdle",
            "点「打开 / 连接浏览器」开始：程序会连接你已经打开的那个浏览器，只在你当前的那一个标签页上操作。");
        PickReadyPromptText.Text = Loc.T("settings.automation.pickReadyPrompt",
            "请在浏览器中完成登录、并导航到你要选取元素的页面。准备好了点「开始选取」。");
        if (PickStateText.Text is null || PickStateText.Text!.Length == 0)
            PickStateText.Text = Loc.T("settings.automation.pickStateLine", "状态：{0}{1}",
                Loc.T("settings.automation.pickStateIdle", "未开始"), "");
        UpdateResultsPreview();
    }
}

