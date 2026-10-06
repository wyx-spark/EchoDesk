// 自动化页的交互逻辑（v0.2.1 跨平台-2 任务一，第 7 个迁移模块）。
//
// 后端动作：
//   automation_config      → { automation: { …20 个字段… } }
//   automation_config_set  { automation: patch } → { content, automation }
//   automation_run / stop / automation_status / automation_clear_login
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
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class AutomationPage : UserControl, ISettingsPage
{
    /// <summary>安全协议版本（与 WPF 版一致；后端据此判断用户接受的是哪一版协议）。</summary>
    private const string SafetyVersion = "1.0";

    private ISettingsHost? _host;
    private bool _loading;
    private bool _available = true;

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
    }

    public string TabKey => "settings.tab.automation";
    public string TabFallback => "自动化";
    public string AutomationId => "SettingsTabAutomation";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }

    // -----------------------------------------------------------------
    private async Task<JsonDocument?> SendAsync(object request)
    {
        if (_host is null)
            return null;
        return await _host.State.Backend.SendAsync(request, idleTimeout: TimeSpan.FromSeconds(90));
    }

    private void SetInputsEnabled(bool enabled)
    {
        foreach (Control control in _inputs)
            control.IsEnabled = enabled;
    }

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
        }
        finally
        {
            _loading = false;
        }

        UpdateResultsPreview();
        AutomationStatusText.Text = Loc.T("settings.automation.stateIdle", "空闲");
    }

    /// <summary>保存位置留空时显示"程序根目录"，并预览完整目录（与 WPF 版同一口径）。</summary>
    private void UpdateResultsPreview()
    {
        if (_loading)
            return;
        string root = ResultsRootNameBox.Text?.Trim() ?? "";
        if (root.Length == 0)
            root = Loc.T("settings.automation.defaultResultsRoot", "EchoDesk 结果");
        string task = TaskNameBox.Text?.Trim() ?? "";
        if (task.Length == 0)
            task = Loc.T("settings.automation.defaultTaskName", "未命名任务");
        string location = ResultsLocationBox.Text?.Trim() ?? "";
        if (location.Length == 0)
            location = Loc.T("settings.automation.programRoot", "程序根目录");
        ResultsPreviewText.Text = Loc.T("settings.automation.resultsPreview",
            "结果将保存到：{0}\\{1}\\{2}\\（附件与摘要）", location, root, task);
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
        NotMigratedText.Text = Loc.T("settings.automation.notMigrated", "");
        RunLogLabel.Text = Loc.T("settings.automation.runLog", "运行日志");
        UpdateResultsPreview();
    }
}
