// [EN] EchoDesk WPF frontend module: frontend/SettingsWindow.Agent.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/SettingsWindow.Agent.cs。类型与成员说明见下方注释。
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using EchoDesk.Services;

namespace EchoDesk;

/// <summary>
/// 设置窗口 ·「智能体」页（能力扩展-1 任务二：Agent 工具权限）。
///
/// 为什么单独开一个 Tab、而不是塞进「模型」页：模型页的竖向空间已经被模型市场占满
/// （结果列表是 <c>*</c> 行），再插一块"每个工具一行"的清单（本版 7 个工具）会把结果列表
/// 挤到几乎看不见 —— 见 SettingsWindow.Profiles.cs 里记着的同类教训。
/// 工具清单本身也要跟着后端增减，放在自己有 ScrollViewer 的页里才不会牵动别的页。
///
/// 与后端交互的两个动作（实现见 backend/main.py 的 configure_agent / agent_diagnostics）：
/// <list type="bullet">
/// <item><c>{"action":"agent_state"}</c> → <c>agent</c> 对象：mode / max_steps /
/// tools[{name,description,risk,enabled,args_hint}] / enabled_tools[]；</item>
/// <item><c>{"action":"agent_configure","tool":"web_search","allowed":true}</c> 或
/// <c>{"action":"agent_configure","steps":5}</c> → <c>agent_config</c> 对象。</item>
/// </list>
/// 写入会落盘到 config.json（后端负责），所以这里点一次就长期生效，
/// 不需要每次对话都确认 —— 这正是"预授权"这一档的意义。
///
/// 控件用 C# 程序化生成（与 SettingsWindow.Inference.cs 同一套写法）：
/// 工具清单是**后端数据**，条数不固定，XAML 里写不出来。
/// </summary>
public partial class SettingsWindow
{
    /// <summary>一行的控件引用（重新读取时整批重建，语言切换时按需重排文本）。</summary>
    private sealed class AgentToolRow
    {
        public Border Root { get; init; } = null!;
        public TextBlock Name { get; init; } = null!;
        public TextBlock Risk { get; init; } = null!;
        public TextBlock Description { get; init; } = null!;
        public TextBlock ArgsHint { get; init; } = null!;
        public TextBlock Warn { get; init; } = null!;
        public CheckBox Enable { get; init; } = null!;
        public string ToolName { get; init; } = "";
        public string RiskLevel { get; init; } = "safe";
    }

    // ---------- 控件引用 ----------

    private TextBlock? _agentTitle;
    private TextBlock? _agentIntro;
    private Button? _agentReloadButton;
    private TextBlock? _agentStateText;
    private TextBlock? _agentStepsLabel;
    private TextBox? _agentStepsBox;
    private Button? _agentStepsButton;
    private TextBlock? _agentStepsHint;
    private TextBlock? _agentToolsTitle;
    private TextBlock? _agentSafeHint;
    private StackPanel? _agentToolList;
    private TextBlock? _agentStatus;

    private readonly List<AgentToolRow> _agentRows = new();

    /// <summary>控件是否已经生成（语言切换回调与异步刷新都要先确认它在）。</summary>
    private bool _agentBuilt;

    /// <summary>
    /// 程序化改勾选状态期间为真：勾选框的变化在正常路径下要**立刻发请求**，
    /// 回填时不按住的话，"读一次状态"会把后端配置原样再写一遍（读操作变成写操作）。
    /// </summary>
    private bool _agentSyncing;

    /// <summary>最近一次读到的工具总数/已启用数（语言重排时要重拼状态行）。</summary>
    private int _agentToolTotal;
    private int _agentToolEnabled;
    private string _agentMode = "";

    // ---------- 初始化 ----------

    /// <summary>
    /// 构造期生成整页控件（由 SettingsWindow 构造函数调用，紧挨 InitInferenceUi()）。
    /// 与它同样的理由：必须赶在 <c>_uiReady = true</c> 之前，窗口还没画出来时不发请求。
    /// </summary>
    private void InitAgentUi()
    {
        if (AgentPanel is null)
            return;   // XAML 里没有宿主面板时不硬塞（旧布局文件）

        var card = new Border
        {
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(12, 10, 12, 10),
        };
        // 用资源引用而不是取值快照：否则切主题后这一页会留在旧配色里
        ThemeService.Bind(card, Border.BackgroundProperty, "InputBackground");
        ThemeService.Bind(card, Border.BorderBrushProperty, "InputBorder");

        var root = new StackPanel();
        card.Child = root;
        AgentPanel.Children.Add(card);

        // ---- 标题行：标题 + 说明 + 重新读取 ----
        var head = new WrapPanel { Orientation = Orientation.Horizontal };
        _agentTitle = IdentifyText(new TextBlock
        {
            Text = Loc.T("settings.agent.title", "🤖 智能体（Agent）"),
            FontSize = 14,
            FontWeight = FontWeights.Bold,
            VerticalAlignment = VerticalAlignment.Center,
        }, "AgentTitle", "AccentBrush");
        head.Children.Add(_agentTitle);

        _agentReloadButton = MakeAgentButton("AgentReloadButton",
            Loc.T("settings.agent.reload", "↻ 重新读取"));
        _agentReloadButton.Margin = new Thickness(12, 0, 0, 0);
        _agentReloadButton.Click += AgentReloadButton_Click;
        head.Children.Add(_agentReloadButton);
        root.Children.Add(head);

        _agentIntro = MakeAgentText("AgentIntro", Loc.T("settings.agent.intro",
            "Agent 模式下模型自己决定调用哪个工具、按什么顺序，直到给出结论；每一步（调了什么、参数是什么、返回什么、耗时多久）都显示在对话里，按 Esc 可随时停止。"),
            11, "SubTextBrush");
        _agentIntro.TextWrapping = TextWrapping.Wrap;
        _agentIntro.Margin = new Thickness(0, 6, 0, 0);
        root.Children.Add(_agentIntro);

        // ---- 状态行（模式 / 已启用工具数） ----
        _agentStateText = MakeAgentText("AgentStateText", "", 11, "SubTextBrush");
        _agentStateText.TextWrapping = TextWrapping.Wrap;
        _agentStateText.Margin = new Thickness(0, 4, 0, 0);
        root.Children.Add(_agentStateText);

        // ---- 步数上限 ----
        var stepsRow = new WrapPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 10, 0, 0) };
        _agentStepsLabel = MakeAgentText("AgentStepsLabel",
            Loc.T("settings.agent.stepsLabel", "最大工具调用步数"), 11, null);
        _agentStepsLabel.VerticalAlignment = VerticalAlignment.Center;
        stepsRow.Children.Add(_agentStepsLabel);

        _agentStepsBox = Identify(new TextBox
        {
            Width = 46,
            Margin = new Thickness(6, 0, 0, 0),
            Padding = new Thickness(4, 2, 4, 2),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Text = "4",
            ToolTip = Loc.T("settings.agent.stepsHint", "1–8：越大越能多步完成任务，也越慢"),
        }, "AgentStepsBox");
        _agentStepsBox.KeyDown += AgentStepsBox_KeyDown;
        stepsRow.Children.Add(_agentStepsBox);

        _agentStepsButton = MakeAgentButton("AgentStepsApplyButton",
            Loc.T("settings.agent.stepsApply", "应用"));
        _agentStepsButton.Margin = new Thickness(8, 0, 0, 0);
        _agentStepsButton.Click += AgentStepsButton_Click;
        stepsRow.Children.Add(_agentStepsButton);

        _agentStepsHint = MakeAgentText("AgentStepsHint",
            Loc.T("settings.agent.stepsHint", "1–8：越大越能多步完成任务，也越慢"), 10, "SubTextBrush");
        _agentStepsHint.Margin = new Thickness(8, 0, 0, 0);
        _agentStepsHint.VerticalAlignment = VerticalAlignment.Center;
        stepsRow.Children.Add(_agentStepsHint);
        root.Children.Add(stepsRow);

        // ---- 工具清单 ----
        _agentToolsTitle = MakeAgentText("AgentToolsTitle",
            Loc.T("settings.agent.toolsTitle", "工具权限"), 12, "AccentBrush");
        _agentToolsTitle.FontWeight = FontWeights.Bold;
        _agentToolsTitle.Margin = new Thickness(0, 14, 0, 0);
        root.Children.Add(_agentToolsTitle);

        // "安全工具默认可用"这句放在清单上方而不是每行重复：它是所有安全工具的共同前提，
        // 重复 5 遍会把 7 行清单撑成一大堵墙。
        _agentSafeHint = MakeAgentText("AgentSafeHint",
            Loc.T("settings.agent.safeAlwaysOn", "安全工具默认可用（不需要授权，也无法关闭）"),
            10, "SubTextBrush");
        _agentSafeHint.TextWrapping = TextWrapping.Wrap;
        _agentSafeHint.Margin = new Thickness(0, 2, 0, 0);
        root.Children.Add(_agentSafeHint);

        _agentToolList = Identify(new StackPanel { Margin = new Thickness(0, 4, 0, 0) }, "AgentToolList");
        root.Children.Add(_agentToolList);

        // ---- 状态行（读写反馈） ----
        _agentStatus = MakeAgentText("AgentStatusText", "", 11, "SubTextBrush");
        _agentStatus.TextWrapping = TextWrapping.Wrap;
        _agentStatus.Margin = new Thickness(0, 8, 0, 0);
        root.Children.Add(_agentStatus);

        // MCP 小节（能力扩展-1 任务五 B）挂在同一页的末尾：它提供的是"给 Agent 用的外部工具"，
        // 与上面的工具权限本就是同一件事的两半（MCP 工具一律按敏感工具处理，要在这份清单里勾选）。
        InitMcpUi(root);

        _agentBuilt = true;
        // 语言切换后重排本页文本：控件是代码生成的，Loc.Apply() 的附加属性回填管不到它们
        Loc.LanguageChanged += (_, _) =>
        {
            if (Dispatcher.CheckAccess())
            {
                RelocalizeAgent();
                RelocalizeMcp();
            }
            else
            {
                Dispatcher.InvokeAsync(() =>
                {
                    RelocalizeAgent();
                    RelocalizeMcp();
                });
            }
        };
        SetAgentStatus(Loc.T("settings.agent.readyHint",
            "改动会写入 config.json，长期生效（不需要每次对话都确认）。"));
    }

    // ---------- 小控件工厂（与推理参数面板同一套约定） ----------

    // Identify<T>() 与 GetBoolOr() 在 SettingsWindow.xaml.cs 的辅助区（两个功能文件共用，
    // 各写一份会在同一个 partial 类里撞名）。

    private static TextBlock IdentifyText(TextBlock block, string id, string brushKey)
    {
        Identify(block, id);
        if (brushKey.Length > 0)
            ThemeService.Bind(block, TextBlock.ForegroundProperty, brushKey);
        return block;
    }

    private TextBlock MakeAgentText(string id, string text, double fontSize, string? brushKey)
    {
        var block = Identify(new TextBlock { Text = text, FontSize = fontSize }, id);
        if (brushKey is not null)
            ThemeService.Bind(block, TextBlock.ForegroundProperty, brushKey);
        return block;
    }

    private Button MakeAgentButton(string id, string text)
    {
        var button = Identify(new Button
        {
            Content = text,
            FontSize = 11,
            Padding = new Thickness(8, 2, 8, 2),
            MinWidth = 0,
            VerticalAlignment = VerticalAlignment.Center,
        }, id);
        if (TryFindResource("RoundButton") is Style style)
            button.Style = style;
        return button;
    }

    // ---------- 事件 ----------

    private async void AgentReloadButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await LoadAgentStateAsync();
        }
        catch (Exception ex)
        {
            // async void 处理器：异常无人接住会冒到进程级，这里兜住并记日志
            SetAgentStatus(Loc.T("settings.agent.readFailed", "❌ 读取 Agent 状态失败：{0}", ex.Message));
            App.Log("SettingsWindow.AgentReload", ex);
        }
    }

    private async void AgentStepsButton_Click(object sender, RoutedEventArgs e) => await ApplyAgentStepsAsync();

    /// <summary>步数框回车即应用（数字框的常规预期，不必先点按钮）。</summary>
    private async void AgentStepsBox_KeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key != System.Windows.Input.Key.Enter)
            return;
        e.Handled = true;
        await ApplyAgentStepsAsync();
    }

    /// <summary>
    /// 勾选框：**立即**写回。用 Checked/Unchecked 而不是 Click ——
    /// WPF 的 ToggleButtonAutomationPeer.Toggle()（无障碍工具、UI 自动化与
    /// tools\ui_probe.ps1 都走这条）只调 OnToggle()，不会触发 Click，
    /// 挂 Click 会变成"鼠标点有效、自动化点没反应"（模型市场的分面筛选踩过这个坑）。
    /// </summary>
    private async void AgentToolCheck_Changed(object sender, RoutedEventArgs e)
    {
        if (!_uiReady || _agentSyncing)
            return;
        if (sender is not CheckBox { Tag: AgentToolRow row } box)
            return;
        bool allowed = box.IsChecked == true;
        await SendAgentPatchAsync(
            new { action = "agent_configure", tool = row.ToolName, allowed },
            allowed
                ? Loc.T("settings.agent.allowed", "✅ 已授权 {0}", row.ToolName)
                : Loc.T("settings.agent.denied", "🔒 已取消授权 {0}", row.ToolName));
    }

    private async Task ApplyAgentStepsAsync()
    {
        if (_agentStepsBox is null)
            return;
        if (!int.TryParse(_agentStepsBox.Text.Trim(), out int steps) || steps < 1 || steps > 8)
        {
            SetAgentStatus(Loc.T("settings.agent.stepsRange", "⚠️ 步数需要在 {0}–{1} 之间", 1, 8));
            return;
        }
        await SendAgentPatchAsync(
            new { action = "agent_configure", steps },
            Loc.T("settings.agent.stepsDone", "✅ 步数上限已设为 {0}", steps));
    }

    // ---------- 读写 ----------

    /// <summary>
    /// 读回 Agent 状态（进设置页与「重新读取」都走这里）。
    ///
    /// <paramref name="quiet"/> 为真时不动状态文本：改动后的回读只是为了拿到**后端真正
    /// 接受了什么**（例如步数被夹到 1–8），调用方随后会写自己的提示。
    /// </summary>
    private async Task LoadAgentStateAsync(bool quiet = false)
    {
        if (!_agentBuilt)
            return;
        if (!quiet)
            SetAgentStatus(Loc.T("settings.agent.readingNow", "正在读取 Agent 状态…"));

        string? json = await RequestWithRetryAsync(
            new { action = "agent_state" },
            Loc.T("settings.agent.reading", "读取 Agent 状态"),
            TimeSpan.FromSeconds(60));
        if (json is null)
        {
            if (!quiet)
                SetAgentStatus(Loc.T("settings.agent.readUnavailable",
                    "⚠️ 未能读取 Agent 状态（后端未响应），可点「重新读取」重试。"));
            return;
        }
        try
        {
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("agent", out JsonElement agent))
            {
                if (!quiet)
                    SetAgentStatus(Loc.T("settings.agent.readFailed", "❌ 读取 Agent 状态失败：{0}",
                        Loc.T("settings.agent.noData", "后端未返回 agent 字段（接口不可用？）")));
                return;
            }
            RenderAgentState(agent);
            if (!quiet)
                SetAgentStatus(Loc.T("settings.agent.loaded", "✅ 已读取当前工具与权限"));
        }
        catch (JsonException ex)
        {
            SetAgentStatus(Loc.T("settings.agent.readFailed", "❌ 读取 Agent 状态失败：{0}", ex.Message));
        }
    }

    /// <summary>把 agent_state 的响应渲染成一个工具一行。</summary>
    private void RenderAgentState(JsonElement agent)
    {
        _agentMode = GetString(agent, "mode");
        int maxSteps = GetInt(agent, "max_steps", 4);
        if (_agentStepsBox is not null)
            _agentStepsBox.Text = maxSteps.ToString();

        _agentRows.Clear();
        _agentToolList?.Children.Clear();
        int total = 0, enabled = 0;
        if (agent.TryGetProperty("tools", out JsonElement tools) && tools.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement tool in tools.EnumerateArray())
            {
                string name = GetString(tool, "name");
                if (name.Length == 0)
                    continue;
                string risk = GetString(tool, "risk");
                if (risk.Length == 0)
                    risk = "safe";
                bool isEnabled = GetBoolOr(tool, "enabled", risk == "safe");
                total++;
                if (isEnabled)
                    enabled++;
                AddAgentToolRow(name, risk, GetString(tool, "description"),
                    GetString(tool, "args_hint"), isEnabled);
            }
        }
        _agentToolTotal = total;
        _agentToolEnabled = enabled;
        RefreshAgentStateLine();

        if (total == 0 && _agentToolList is not null)
        {
            TextBlock empty = MakeAgentText("AgentToolEmpty",
                Loc.T("settings.agent.noTools", "（后端没有返回任何工具）"), 11, "SubTextBrush");
            _agentToolList.Children.Add(empty);
        }
    }

    /// <summary>造一个工具行（左：名称/风险/说明/参数提示；右：启用勾选框）。</summary>
    private void AddAgentToolRow(string name, string risk, string description, string argsHint, bool isEnabled)
    {
        if (_agentToolList is null)
            return;

        var info = new StackPanel();

        var head = new StackPanel { Orientation = Orientation.Horizontal };
        var nameBlock = MakeAgentText("", name, 12, null);
        nameBlock.FontWeight = FontWeights.SemiBold;
        head.Children.Add(nameBlock);

        bool sensitive = risk.Equals("sensitive", StringComparison.OrdinalIgnoreCase);
        var riskBlock = MakeAgentText("", AgentRiskLabel(risk), 10,
            sensitive ? "WarningBrush" : "SuccessBrush");
        riskBlock.Margin = new Thickness(6, 0, 0, 0);
        riskBlock.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(riskBlock);
        info.Children.Add(head);

        var desc = MakeAgentText("", description, 11, "SubTextBrush");
        desc.TextWrapping = TextWrapping.Wrap;
        desc.Margin = new Thickness(0, 2, 0, 0);
        info.Children.Add(desc);

        var hint = MakeAgentText("", argsHint.Length > 0
            ? Loc.T("settings.agent.argsHint", "参数：{0}", argsHint) : "", 10, "SubTextBrush");
        hint.TextWrapping = TextWrapping.Wrap;
        hint.Visibility = argsHint.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
        hint.Tag = argsHint;   // 原文留着：语言切换后要按新语言重拼"参数：…"前缀
        info.Children.Add(hint);

        // 敏感工具单独给一句"会把数据带出去/读到本机内容"：
        // 工具说明是后端给的（可能只有一句话），而"授权意味着什么"必须让用户在勾之前看懂。
        var warn = MakeAgentText("", sensitive ? AgentSensitiveWarning(name) : "", 10, "WarningBrush");
        warn.TextWrapping = TextWrapping.Wrap;
        warn.Visibility = sensitive ? Visibility.Visible : Visibility.Collapsed;
        info.Children.Add(warn);

        var check = Identify(new CheckBox
        {
            Content = Loc.T("settings.agent.enable", "启用"),
            FontSize = 11,
            IsChecked = isEnabled,
            // 安全工具恒可用（后端 is_allowed 对 safe 一律放行）：勾选框只作**状态展示**，
            // 置灰避免用户以为"取消勾选就能关掉"，结果下次读取又变回勾上。
            IsEnabled = sensitive,
            VerticalAlignment = VerticalAlignment.Top,
            Margin = new Thickness(10, 0, 0, 0),
            ToolTip = sensitive
                ? Loc.T("settings.agent.enableTip",
                    "勾选后 Agent 可以在对话里自行调用这个工具（写入 config.json，长期生效）")
                : Loc.T("settings.agent.safeAlwaysOn", "安全工具默认可用（不需要授权，也无法关闭）"),
        }, "AgentToolCheck_" + name);
        check.Checked += AgentToolCheck_Changed;
        check.Unchecked += AgentToolCheck_Changed;

        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        Grid.SetColumn(info, 0);
        Grid.SetColumn(check, 1);
        grid.Children.Add(info);
        grid.Children.Add(check);

        var line = new Border
        {
            Padding = new Thickness(8, 6, 8, 6),
            Margin = new Thickness(0, 3, 0, 0),
            CornerRadius = new CornerRadius(4),
            BorderThickness = new Thickness(1),
            Child = grid,
        };
        ThemeService.Bind(line, Border.BackgroundProperty, "WindowBackground");
        ThemeService.Bind(line, Border.BorderBrushProperty, "InputBorder");
        Identify(line, "AgentTool_" + name);

        var row = new AgentToolRow
        {
            Root = line,
            Name = nameBlock,
            Risk = riskBlock,
            Description = desc,
            ArgsHint = hint,
            Warn = warn,
            Enable = check,
            ToolName = name,
            RiskLevel = risk,
        };
        check.Tag = row;   // 勾选处理器靠 Tag 找回这一行（工具名与风险等级）
        _agentRows.Add(row);
        _agentToolList.Children.Add(line);
    }

    /// <summary>写回一条 Agent 配置，然后**回读**一次（后端会夹取范围，界面要显示真正生效的值）。</summary>
    private async Task SendAgentPatchAsync(object patch, string okText)
    {
        if (_agentStatus is null)
            return;
        SetAgentStatus(Loc.T("settings.agent.saving", "正在保存…"));
        try
        {
            string json = await _backend.SendRequestAsync(patch);
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("agent_config", out JsonElement cfg))
            {
                bool ok = GetBoolOr(cfg, "ok", false);
                await LoadAgentStateAsync(quiet: true);
                SetAgentStatus(ok
                    ? okText
                    : Loc.T("settings.agent.saveFailed", "❌ 设置失败：{0}",
                        GetString(cfg, "error").Length > 0
                            ? GetString(cfg, "error")
                            : Loc.T("settings.agent.rejected", "后端拒绝了这次改动")));
                return;
            }
            SetAgentStatus(Loc.T("settings.agent.saveFailed", "❌ 设置失败：{0}",
                Loc.T("settings.agent.noData", "后端未返回 agent_config 字段（接口不可用？）")));
        }
        catch (Exception ex)
        {
            SetAgentStatus(Loc.T("settings.agent.saveFailed", "❌ 设置失败：{0}", ex.Message));
        }
    }

    // ---------- 文案与状态 ----------

    /// <summary>风险等级标签：safe/sensitive/guard 三档（guard = 本版不提供的保护类工具）。</summary>
    private static string AgentRiskLabel(string risk) => risk.ToLowerInvariant() switch
    {
        "sensitive" => Loc.T("settings.agent.riskSensitive", "敏感"),
        "guard" => Loc.T("settings.agent.riskGuard", "受保护"),
        "safe" => Loc.T("settings.agent.riskSafe", "安全"),
        _ => Loc.T("settings.agent.riskUnknown", "未知"),
    };

    /// <summary>
    /// 敏感工具的"授权意味着什么"。按工具名给具体提示（联网 / 读本机文件是两种完全不同的
    /// 风险），认不出来的敏感工具退回一句通用说明 —— 后端将来加工具时不会显示成空白。
    /// </summary>
    private static string AgentSensitiveWarning(string toolName) => toolName.ToLowerInvariant() switch
    {
        "web_search" => Loc.T("settings.agent.warnWebSearch", "⚠️ 会把关键词发到互联网"),
        "file_read" => Loc.T("settings.agent.warnFileRead", "⚠️ 会把本机文件内容读进上下文"),
        _ => Loc.T("settings.agent.sensitiveWarn", "⚠️ 敏感工具：授权后模型会自行调用，可能把数据带到外部或读取本机内容"),
    };

    /// <summary>重拼状态行（模式 / 步数 / 已启用工具数），语言切换后也靠它保持同语言。</summary>
    private void RefreshAgentStateLine()
    {
        if (_agentStateText is null)
            return;
        _agentStateText.Text = Loc.T("settings.agent.state",
            "当前模式：{0} · 最多调用 {1} 次工具 · 已启用 {2}/{3} 个工具",
            _agentMode.Length > 0 ? _agentMode : "-",
            _agentStepsBox?.Text ?? "-", _agentToolEnabled, _agentToolTotal);
    }

    private void SetAgentStatus(string text, string? detail = null)
    {
        if (_agentStatus is null)
            return;
        _agentStatus.Text = text;
        _agentStatus.ToolTip = string.IsNullOrWhiteSpace(detail) ? null : detail;
    }

    /// <summary>
    /// 按新语言重排本页（供 <see cref="Loc.LanguageChanged"/> 调用）。
    ///
    /// 工具行里的**名称与说明是后端数据**（后端不本地化工具描述），只重排界面自己的文案；
    /// 行本身按新语言重建（"启用"/"参数："/风险标签都是界面文案）。
    /// </summary>
    private void RelocalizeAgent()
    {
        if (!_agentBuilt)
            return;
        bool previous = _agentSyncing;
        _agentSyncing = true;   // 重建行会动勾选状态，别让它顺手发请求
        try
        {
            if (_agentTitle is not null)
                _agentTitle.Text = Loc.T("settings.agent.title", "🤖 智能体（Agent）");
            if (_agentIntro is not null)
                _agentIntro.Text = Loc.T("settings.agent.intro",
                    "Agent 模式下模型自己决定调用哪个工具、按什么顺序，直到给出结论；每一步（调了什么、参数是什么、返回什么、耗时多久）都显示在对话里，按 Esc 可随时停止。");
            if (_agentReloadButton is not null)
                _agentReloadButton.Content = Loc.T("settings.agent.reload", "↻ 重新读取");
            if (_agentStepsLabel is not null)
                _agentStepsLabel.Text = Loc.T("settings.agent.stepsLabel", "最大工具调用步数");
            if (_agentStepsButton is not null)
                _agentStepsButton.Content = Loc.T("settings.agent.stepsApply", "应用");
            if (_agentStepsHint is not null)
                _agentStepsHint.Text = Loc.T("settings.agent.stepsHint",
                    "1–8：越大越能多步完成任务，也越慢");
            if (_agentStepsBox is not null)
                _agentStepsBox.ToolTip = _agentStepsHint?.Text;
            if (_agentToolsTitle is not null)
                _agentToolsTitle.Text = Loc.T("settings.agent.toolsTitle", "工具权限");
            if (_agentSafeHint is not null)
                _agentSafeHint.Text = Loc.T("settings.agent.safeAlwaysOn",
                    "安全工具默认可用（不需要授权，也无法关闭）");
            foreach (AgentToolRow row in _agentRows)
            {
                bool sensitive = row.RiskLevel.Equals("sensitive", StringComparison.OrdinalIgnoreCase);
                row.Risk.Text = AgentRiskLabel(row.RiskLevel);
                row.Warn.Text = sensitive ? AgentSensitiveWarning(row.ToolName) : "";
                row.Enable.Content = Loc.T("settings.agent.enable", "启用");
                row.Enable.ToolTip = sensitive
                    ? Loc.T("settings.agent.enableTip",
                        "勾选后 Agent 可以在对话里自行调用这个工具（写入 config.json，长期生效）")
                    : Loc.T("settings.agent.safeAlwaysOn", "安全工具默认可用（不需要授权，也无法关闭）");
                // 参数提示前缀是界面文案、值来自后端
                string hint = row.ArgsHint.Tag as string ?? "";
                if (hint.Length > 0)
                    row.ArgsHint.Text = Loc.T("settings.agent.argsHint", "参数：{0}", hint);
            }
            RefreshAgentStateLine();
        }
        finally
        {
            _agentSyncing = previous;
        }
    }
}
