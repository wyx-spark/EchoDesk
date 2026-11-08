// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/AgentPage.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/AgentPage.axaml.cs。类型与成员说明见下方注释。
// [EN] Interaction logic of the Agent page (v0.2.1 cross-platform-2 development task 1,
// [EN] the sixth migrated module).
// [ZH] 智能体页的交互逻辑（v0.2.1 跨平台-2 研发任务 1，第 6 个迁移模块）。
//
// [EN] Data shapes of the two backend actions (identical to the WPF version):
// [ZH] 两个后端动作的数据形状（与 WPF 版一致）：
//   agent_state → { agent: { mode, max_steps, tools: [ { name, risk, description,
//                            args_hint, enabled } ] } }
//   mcp_state   → { mcp: { enabled, auto_connect, timeout_seconds,
//                          servers: [ { name, command, args[], enabled, connected,
//                                       error, tools[] } ] } }
// [EN]   Write: agent_configure { tool, allowed } or { steps }
// [ZH]   写：agent_configure { tool, allowed } 或 { steps }
//
// [EN] **Tool authorization defaults come from the backend**: a tool with `risk == "safe"`
// [EN] is enabled by default when there is no explicit setting (the WPF version uses
// [EN] the same rule). The front end does not invent defaults on its own -- otherwise an
// [EN] inconsistency such as "the UI shows it on while the backend has it off" would show
// [EN] up in security-relevant features, which is too costly.
// [ZH] **工具授权的默认值来自后端**：`risk == "safe"` 的工具在没有明确设置时默认开启
// [ZH] （WPF 版同一口径）。前端不自己发明默认值——否则"界面显示开着、后端其实关着"
// [ZH] 这类不一致会出现在安全相关的功能上，代价太大。
//
// [EN] **MCP is read-only display in this round**: full editing (adding and removing
// [EN] servers, changing command and arguments, env passthrough, timeout, connection test)
// [EN] needs a whole form and belongs to the next round. What is shown here is the status
// [EN] and "why it was not migrated", not a UI that does nothing when clicked -- consistent
// [EN] with how the Installed page and the Developer page were handled.
// [ZH] **MCP 本轮只做只读展示**：完整编辑（增删服务器、改命令与参数、env 透传、
// [ZH] 超时、连接测试）需要一整块表单，属于下一轮。这里显示状态与"为什么没迁移"，
// [ZH] 而不是摆一个点了没反应的界面 —— 与已安装页、开发者页的处置口径一致。
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class AgentPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;
    private bool _syncing;

    public AgentPage()
    {
        InitializeComponent();
        AgentReloadButton.Click += async (_, _) => await RefreshAsync();
        AgentStepsApplyButton.Click += async (_, _) => await ApplyStepsAsync();
        AgentStepsBox.KeyDown += async (_, e) =>
        {
            if (e.Key == Key.Enter)
            {
                e.Handled = true;
                await ApplyStepsAsync();
            }
        };

        // [EN] ---- MCP (v0.2.2, batch 2) ----
        // [ZH] ---- MCP（v0.2.2 第 2 批）----
        McpReloadButton.Click += async (_, _) => await LoadMcpStateAsync();
        McpSaveButton.Click += async (_, _) => await SendMcpApplyAsync(connect: false);
        // [EN] "Connect and fetch tools" = save + auto_connect=true: equivalent to
        // [EN] /mcp connect. It can also be used once without ticking "connect on startup"
        // [EN] -- that is exactly what this button is for (probing the path).
        // [ZH] 「连接并取工具」= 保存 + auto_connect=true：与 /mcp connect 等价。
        // [ZH] 界面上不勾「启动时连接」也能用一次 —— 这正是这个按钮的用处（试通路）。
        McpConnectButton.Click += async (_, _) => await SendMcpApplyAsync(connect: true);
        McpAddButton.Click += (_, _) => ToggleMcpAddForm(true);
        McpAddExampleButton.Click += (_, _) =>
        {
            ToggleMcpAddForm(true);
            McpFormNameBox.Text = "echo";
            McpFormCommandBox.Text = "python";
            McpFormArgsBox.Text = ExampleMcpServerPath();
            McpStatusText.Text = Loc.T("settings.mcp.exampleFilled",
                "已填入示例服务器（命令 python，参数是随包示例脚本的路径）：点「💾 保存」或「🔌 连接并取工具」验证通路。");
        };
        McpFormAddConfirmButton.Click += (_, _) => AddMcpServerFromForm();
        McpFormCancelButton.Click += (_, _) => ToggleMcpAddForm(false);
        // [EN] The master switch and auto-connect only mark the state as "pending save";
        // [EN] no request is sent immediately (see the edit model of McpServerRow).
        // [ZH] 总开关与自动连接只标记"待保存"，不立刻发请求（见 McpServerRow 的编辑模型）。
        McpEnabledCheck.IsCheckedChanged += (_, _) =>
        {
            if (_mcpSyncing)
                return;
            RenderMcpServerRows();   // [EN] The global switch changed; each row's
                                     // [EN] "disabled" state must follow.
                                     // [ZH] 全局开关变了，每行的"未启用"状态要跟着变
            SetMcpStagedHint();
        };
        McpAutoConnectCheck.IsCheckedChanged += (_, _) =>
        {
            if (!_mcpSyncing)
                SetMcpStagedHint();
        };
    }

    /// <summary>
    /// [EN] Inline form submit for adding a server (the name and the command must both be
    /// [EN] non-empty; duplicates are rejected).
    /// [ZH] 新增 server 的内联表单提交（名称与命令都不能为空；重名要挡下来）。
    /// </summary>
    private void AddMcpServerFromForm()
    {
        string name = (McpFormNameBox.Text ?? "").Trim();
        string command = (McpFormCommandBox.Text ?? "").Trim();
        if (name.Length == 0 || command.Length == 0)
        {
            McpStatusText.Text = Loc.T("settings.mcp.formInvalid", "⚠️ 名称与命令都不能为空");
            return;
        }
        foreach (McpServerRow existing in _mcpServers)
        {
            if (string.Equals(existing.Name, name, StringComparison.OrdinalIgnoreCase))
            {
                McpStatusText.Text = Loc.T("settings.mcp.formDuplicate",
                    "⚠️ 已经有一个叫「{0}」的 server 了", name);
                return;
            }
        }
        _mcpServers.Add(new McpServerRow
        {
            Name = name,
            Command = command,
            Args = SplitMcpArgs(McpFormArgsBox.Text),
            Enabled = true,
            Known = false,
        });
        _mcpServersDirty = true;
        ToggleMcpAddForm(false);
        RenderMcpServerRows();
        SetMcpStagedHint();
    }

    public string TabKey => "settings.tab.agent";
    public string TabFallback => "智能体";
    public string AutomationId => "SettingsTabAgent";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }
    public Task ApplyToBackendAsync() => Task.CompletedTask;

    // -----------------------------------------------------------------
    /// <summary>
    /// [EN] Sends one request. <paramref name="timeoutSeconds"/> defaults to 60 --
    /// [EN] **mcp_apply must launch an external process**, so that path explicitly gets
    /// [EN] 120 seconds (the same value as the WPF side).
    /// [ZH] 发一条请求。<paramref name="timeoutSeconds"/> 默认为 60 ——
    /// [ZH] **mcp_apply 要拉起外部进程**，那条路径显式给 120 秒（与 WPF 侧同一取值）。
    /// </summary>
    private async Task<JsonDocument?> SendAsync(object request, int timeoutSeconds = 60)
    {
        if (_host is null)
            return null;
        return await _host.State.ActiveBackend.SendAsync(
            request, idleTimeout: TimeSpan.FromSeconds(timeoutSeconds));
    }

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        await LoadAgentStateAsync();
        await LoadMcpStateAsync();
    }

    // -----------------------------------------------------------------
    // [EN] Tool authorization
    // [ZH] 工具授权
    // -----------------------------------------------------------------
    private async Task LoadAgentStateAsync()
    {
        AgentStateText.Text = Loc.T("settings.agent.readingNow", "正在读取…");
        using JsonDocument? doc = await SendAsync(new { action = "agent_state" });
        if (doc is null || !doc.RootElement.TryGetProperty("agent", out JsonElement agent))
        {
            AgentStateText.Text = Loc.T("settings.agent.readUnavailable",
                "⚠️ 后端不可用，无法读取智能体状态");
            return;
        }

        int maxSteps = GetInt(agent, "max_steps", 4);
        _syncing = true;
        AgentStepsBox.Text = maxSteps.ToString();
        _syncing = false;

        RenderToolRows(agent, maxSteps);
        AgentStatusText.Text = Loc.T("settings.agent.loaded", "✅ 已读取智能体状态");
    }

    /// <summary>
    /// [EN] Rebuilds the tool list and the step count from the <c>agent</c> object returned
    /// [EN] by the backend.
    ///
    /// [EN] It is a separate method because the <c>mcp_apply</c> response **also** carries
    /// [EN] an <c>agent</c> object: the MCP tools were just registered, so the tool
    /// [EN] permission list above must be updated too, otherwise the user has to click
    /// [EN] "reload" once more to see the new tools (the WPF side does the same).
    /// [ZH] 用后端回传的 <c>agent</c> 对象重排工具清单与步数。
    ///
    /// [ZH] 单独立一个方法是因为 <c>mcp_apply</c> 的响应里**也**带一份 <c>agent</c>：
    /// [ZH] MCP 工具刚注册进来，上面的工具权限清单必须跟着更新，否则用户要自己再点一次
    /// [ZH] 「重新读取」才看得到新工具（WPF 侧同一处理）。
    /// </summary>
    private void RenderAgentState(JsonElement agent)
    {
        int maxSteps = GetInt(agent, "max_steps", 4);
        _syncing = true;
        AgentStepsBox.Text = maxSteps.ToString();
        _syncing = false;
        RenderToolRows(agent, maxSteps);
    }

    private void RenderToolRows(JsonElement agent, int maxSteps)
    {
        var rows = new List<Control>();
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
                // [EN] Defaults: safe tools are on by default, everything else is off (the
                // [EN] same rule as the backend).
                // [ZH] 默认值：safe 工具默认开启，其余默认关闭（与后端同一口径）。
                bool isEnabled = GetBoolOr(tool, "enabled", risk == "safe");
                total++;
                if (isEnabled)
                    enabled++;
                rows.Add(BuildToolRow(name, risk, GetString(tool, "description"),
                    GetString(tool, "args_hint"), isEnabled));
            }
        }

        AgentToolList.Children.Clear();
        if (total == 0)
        {
            AgentToolList.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.agent.noTools", "（后端没有返回任何工具）"),
                FontSize = 11,
                Opacity = 0.7,
            });
        }
        else
        {
            foreach (Control row in rows)
                AgentToolList.Children.Add(row);
        }

        // [EN] The language pack (the same one as in WPF) needs 4 arguments: mode, maximum
        // [EN] number of calls, enabled count, total count. This used to pass only 3 (mode
        // [EN] was missing), so string.Format threw FormatException and Loc.T returned the
        // [EN] raw template; the status line then showed the unformatted placeholder text
        // [EN] (hard-coded audit §5.3 C-1). mode comes back from the backend's agent_state
        // [EN] (the same source as `_agentMode` on the WPF side).
        // [ZH] 语言包（与 WPF 同一份）要 4 个参数：模式 · 最多调用次数 · 已启用/总数。
        // [ZH] 这里曾只传 3 个（缺 mode）→ string.Format 抛 FormatException → Loc.T 返回模板原文，
        // [ZH] 状态行直接显示「当前模式：{0} · 最多调用 {1} 次工具 · 已启用 {2}/{3} 个工具」
        // [ZH]（硬编码清查 §5.3 C-1）。mode 由后端的 agent_state 回传（与 WPF `_agentMode` 同源）。
        string agentMode = GetString(agent, "mode");
        AgentStateText.Text = Loc.T("settings.agent.state",
            "当前模式：{0} · 最多调用 {1} 次工具 · 已启用 {2}/{3} 个工具",
            agentMode.Length > 0 ? agentMode : "-", maxSteps, enabled, total);
    }

    /// <summary>
    /// [EN] One tool authorization row: the name on the left plus a risk label and a
    /// [EN] description, a switch on the right.
    /// [ZH] 一行工具授权：左侧名称 + 风险标签 + 说明，右侧开关。
    /// </summary>
    private Control BuildToolRow(string name, string risk, string description, string argsHint, bool isEnabled)
    {
        bool safe = risk == "safe";
        (string riskText, string brushKey) = risk switch
        {
            "safe" => (Loc.T("settings.agent.riskSafe", "安全"), "MutedForegroundBrush"),
            "sensitive" => (Loc.T("settings.agent.riskSensitive", "敏感"), "AccentBrush"),
            "guard" => (Loc.T("settings.agent.riskGuard", "需确认"), "AccentBrush"),
            _ => (Loc.T("settings.agent.riskUnknown", "未知"), "MutedForegroundBrush"),
        };

        var info = new StackPanel { Spacing = 2 };
        var head = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        head.Children.Add(new TextBlock { Text = name, FontWeight = FontWeight.SemiBold, FontSize = 12 });
        head.Children.Add(new TextBlock
        {
            Text = riskText,
            FontSize = 10,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = TryBrush(brushKey),
        });
        if (safe)
        {
            head.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.agent.safeAlwaysOn", "只读，始终允许"),
                FontSize = 10,
                Opacity = 0.6,
                VerticalAlignment = VerticalAlignment.Center,
            });
        }
        info.Children.Add(head);

        if (description.Length > 0)
            info.Children.Add(new TextBlock
            {
                Text = description, FontSize = 11, Opacity = 0.8, TextWrapping = TextWrapping.Wrap,
            });
        if (argsHint.Length > 0)
            info.Children.Add(new TextBlock
            {
                // [EN] The pack value is a single-argument template: the argument must be
                // [EN] passed in, otherwise the UI shows the raw template followed by the
                // [EN] value.
                // [ZH] 包值是「参数：{0}」——参数要传进去，否则界面上会出现「参数：{0}：{…}」
                Text = Loc.T("settings.agent.argsHint", "参数：{0}", argsHint),
                FontSize = 10, Opacity = 0.6, TextWrapping = TextWrapping.Wrap,
            });
        if (!safe)
            info.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.agent.sensitiveWarn",
                    "⚠️ 该工具会改动本机内容或访问外部网络，只在需要时开启。"),
                FontSize = 10, Opacity = 0.75, TextWrapping = TextWrapping.Wrap,
            });

        var check = new CheckBox
        {
            // [EN] The "enabled" label: the WPF check box carries this text, so it is
            // [EN] aligned here (it used to be a bare check box and the user did not know
            // [EN] what it toggled -- the "text only" item C3 of the difference list).
            // [ZH] 「启用」标签：WPF 侧的勾选框带这一行字，这里对齐（此前只有一个裸勾选框，
            // [ZH] 用户不知道勾的是什么 —— 差异清单 C3 的「仅文案」一条）。
            Content = Loc.T("settings.agent.enable", "启用"),
            FontSize = 11,
            IsChecked = isEnabled,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(10, 0, 0, 0),
        };
        ToolTip.SetTip(check, safe
            ? Loc.T("settings.agent.safeAlwaysOn", "安全工具默认可用（不需要授权，也无法关闭）")
            : Loc.T("settings.agent.enableTip",
                "勾选后模型可以自行调用这个工具；取消勾选则一律拒绝"));
        AutomationProperties.SetAutomationId(check, "AgentToolCheck_" + name);
        check.IsCheckedChanged += async (_, _) =>
        {
            if (_syncing || _host is null)
                return;
            bool allowed = check.IsChecked == true;
            AgentStatusText.Text = Loc.T("settings.agent.saving", "⏳ 正在保存…");
            using JsonDocument? doc = await SendAsync(new { action = "agent_configure", tool = name, allowed });
            AgentStatusText.Text = doc is not null
                ? (allowed
                    ? Loc.T("settings.agent.allowed", "✅ 已授权：{0}", name)
                    : Loc.T("settings.agent.denied", "⛔ 已关闭：{0}", name))
                // [EN] The pack value is a single-argument failure template: the "backend
                // [EN] did not respond" case reuses the existing main.error.noResponse, and
                // [EN] no code fallback is appended any more (a fallback never takes effect
                // [EN] while the key exists; the UI would only show the raw placeholder).
                // [ZH] 包值是「❌ 设置失败：{0}」——"后端未响应"这一档用现成的 main.error.noResponse，
                // [ZH] 不再拼代码兜底（兜底在键存在时根本不会生效，界面上只会露出 {0}）
                : Loc.T("settings.agent.saveFailed", "❌ 设置失败：{0}",
                    Loc.T("main.error.noResponse", "无响应"));
            await LoadAgentStateAsync();
        };

        var grid = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        Grid.SetColumn(info, 0);
        Grid.SetColumn(check, 1);
        grid.Children.Add(info);
        grid.Children.Add(check);

        var card = new Border { Child = grid };
        card.Classes.Add("card");
        return card;
    }

    private async Task ApplyStepsAsync()
    {
        if (_host is null || _syncing)
            return;
        if (!int.TryParse(AgentStepsBox.Text?.Trim(), out int steps))
        {
            // [EN] The pack value is a range template: the lower and upper bounds match
            // [EN] the WPF side (1-8).
            // [ZH] 包值是「⚠️ 步数需要在 {0}–{1} 之间」：上下限与 WPF 侧同一口径（1–8）
            AgentStatusText.Text = Loc.T("settings.agent.stepsRange",
                "⚠️ 步数需要在 {0}–{1} 之间", 1, 8);
            return;
        }
        AgentStatusText.Text = Loc.T("settings.agent.saving", "⏳ 正在保存…");
        using JsonDocument? doc = await SendAsync(new { action = "agent_configure", steps });
        AgentStatusText.Text = doc is not null
            ? Loc.T("settings.agent.stepsDone", "✅ 最大步数已设为 {0}", steps)
            : Loc.T("settings.agent.saveFailed", "❌ 设置失败：{0}",
                Loc.T("main.error.noResponse", "无响应"));
        await LoadAgentStateAsync();
    }

    // =================================================================
    // [EN] MCP (v0.2.2, batch 2: expanded from "read-only display" to editable, aligned
    // [EN] with SettingsWindow.Mcp.cs on the WPF side)
    // [ZH] MCP（v0.2.2 第 2 批：从"只读展示"补成可编辑，对齐 WPF 的 SettingsWindow.Mcp.cs）
    // =================================================================
    //
    // [EN] An MCP (Model Context Protocol) server is an external process configured by the
    // [EN] user; once it is connected, the tools it provides are registered in the Agent's
    // [EN] tool table (`mcp__<server>__<tool>`), so the model can call them as needed.
    // [EN] Difference from built-in tools: built-in tools ship with the program while MCP
    // [EN] tools come from third-party processes -- hence the two explicit safety
    // [EN] statements here: **MCP tools are always treated as sensitive tools** (they must
    // [EN] be checked separately in the tool permission area above), and **environment
    // [EN] variables are not passed through by default** (so secrets are not handed over
    // [EN] casually).
    // [ZH] MCP（Model Context Protocol）Server = 用户自己配置的外部进程，接上之后它提供的工具
    // [ZH] 会注册进 Agent 的工具表（`mcp__<server>__<tool>`），模型就能按需调用。
    // [ZH] 与内置工具的区别：内置工具随程序走，MCP 工具来自第三方进程 —— 所以这里有两道明确的
    // [ZH] 安全表达：**MCP 工具一律按敏感工具处理**（要在上面的工具权限区另外勾选）与
    // [ZH] **默认不透传环境变量**（密钥不会被顺手交给它）。
    //
    // [EN] Backend protocol (unchanged, not a single word):
    // [ZH] 后端协议（一个字都没改）：
    //   mcp_state → { mcp: { enabled, auto_connect, timeout_seconds, servers[], error } }
    // [EN]   mcp_apply → saves **and takes effect immediately** (rebuilds the bridge and
    // [EN] the tool table); the response returns both mcp and agent.
    // [ZH]   mcp_apply → 保存**并立刻生效**（重建桥接器与工具表），响应里同时回传 mcp 与 agent

    /// <summary>
    /// [EN] One MCP server as shown in the UI (local edit state plus the read-only state
    /// [EN] returned by the backend).
    /// [ZH] 界面上的一个 MCP Server（本地编辑状态 + 后端回传的只读状态）。
    /// </summary>
    private sealed class McpServerRow
    {
        public string Name = "";
        public string Command = "";
        public List<string> Args = new();
        /// <summary>
        /// [EN] env from the configuration (the backend does **not** return this field, so
        /// [EN] even a known server can only have an empty table here).
        /// [ZH] 配置里的 env（后端**不回传**这个字段，所以已知 server 也只能是空表）。
        /// </summary>
        public Dictionary<string, string> Env = new();
        public bool Enabled = true;
        public List<string> PassthroughEnv = new();

        // [EN] The following comes from the read-only state of mcp_state.
        // [ZH] 以下来自 mcp_state 的只读状态
        public bool Connected;
        public List<string> Tools = new();
        public string Error = "";
        /// <summary>
        /// [EN] Whether this is a server known to the backend (false for one just added in
        /// [EN] the UI; used for the status text only).
        /// [ZH] 是否是后端已知的 server（界面上新加的为 false，只用于状态文案）。
        /// </summary>
        public bool Known;

        public string CommandText => Args.Count > 0 ? Command + " " + string.Join(" ", Args) : Command;
    }

    private readonly List<McpServerRow> _mcpServers = new();

    /// <summary>
    /// [EN] The list was modified (added, removed or enable check) → the whole servers
    /// [EN] array must be sent back on save. See BuildMcpPatch.
    /// [ZH] 列表被改过（增/删/启用勾选）→ 保存时要把 servers 整份发回去。见 BuildMcpPatch。
    /// </summary>
    private bool _mcpServersDirty;

    /// <summary>
    /// [EN] True while the state is being applied back: a programmatic change of the check
    /// [EN] box must not count as "modified by the user".
    /// [ZH] 回填期间为真：勾选框的程序化改值不该被当成"用户改过"。
    /// </summary>
    private bool _mcpSyncing;

    /// <summary>
    /// [EN] Global error reported by the backend (for example why one server failed to
    /// [EN] connect).
    /// [ZH] 后端报的全局错误（例如某个 server 连接失败的原因）。
    /// </summary>
    private string _mcpError = "";

    private async Task LoadMcpStateAsync(bool quiet = false)
    {
        if (!quiet)
            McpStatusText.Text = Loc.T("settings.mcp.readingNow", "正在读取 MCP 状态…");
        using JsonDocument? doc = await SendAsync(new { action = "mcp_state" });
        if (doc is null || !doc.RootElement.TryGetProperty("mcp", out JsonElement mcp))
        {
            McpSummaryText.Text = Loc.T("settings.mcp.readUnavailable",
                "⚠️ 未能读取 MCP 状态（后端未响应），可点「↻ 重新读取」重试。");
            return;
        }
        RenderMcpState(mcp);
        if (!quiet)
            McpStatusText.Text = Loc.T("settings.mcp.loaded", "✅ 已读取 MCP 状态");
    }

    /// <summary>
    /// [EN] Renders the mcp_state response onto the controls (the whole state is decided
    /// [EN] by the backend; the UI only displays it).
    /// [ZH] 把 mcp_state 的响应渲染到控件上（整段状态都由后端决定，界面只是显示）。
    /// </summary>
    private void RenderMcpState(JsonElement mcp)
    {
        _mcpSyncing = true;
        try
        {
            bool enabled = GetBool(mcp, "enabled");
            bool autoConnect = GetBool(mcp, "auto_connect");
            double timeout = mcp.TryGetProperty("timeout_seconds", out JsonElement t)
                             && t.ValueKind == JsonValueKind.Number ? t.GetDouble() : 20.0;
            McpEnabledCheck.IsChecked = enabled;
            McpAutoConnectCheck.IsChecked = autoConnect;
            McpTimeoutBox.Text = timeout.ToString("0.##");
            McpSummaryText.Text = string.Join("　·　", new[]
            {
                Loc.T("settings.mcp.enabled", "启用 MCP") + "：" + (enabled ? "✔" : "✘"),
                Loc.T("settings.mcp.autoConnect", "启动时连接") + "：" + (autoConnect ? "✔" : "✘"),
                Loc.T("settings.mcp.timeoutLabel", "连接超时（秒）") + $"：{timeout:0.##}",
            });

            _mcpServers.Clear();
            if (mcp.TryGetProperty("servers", out JsonElement servers) && servers.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement server in servers.EnumerateArray())
                {
                    var row = new McpServerRow
                    {
                        Name = GetString(server, "name"),
                        Command = GetString(server, "command"),
                        Enabled = GetBoolOr(server, "enabled", true),
                        Connected = GetBool(server, "connected"),
                        Error = GetString(server, "error"),
                        Known = true,
                    };
                    row.Args = ReadStringList(server, "args");
                    row.PassthroughEnv = ReadStringList(server, "passthrough_env");
                    row.Tools = ReadStringList(server, "tools");
                    // [EN] env is not in mcp_state at the moment (the backend deliberately
                    // [EN] does not return it); if it can be read, it is kept and sent back
                    // [EN] as is, and if not it stays an empty table -- the day the backend
                    // [EN] exposes it, nothing here needs to change.
                    // [ZH] env 目前不在 mcp_state 里（后端有意不回传）；能读到就保管起来原样回传，
                    // [ZH] 读不到就是空表 —— 后端哪天放出来，这里不用改。
                    if (server.TryGetProperty("env", out JsonElement env) && env.ValueKind == JsonValueKind.Object)
                    {
                        foreach (JsonProperty property in env.EnumerateObject())
                        {
                            if (property.Value.ValueKind == JsonValueKind.String)
                                row.Env[property.Name] = property.Value.GetString() ?? "";
                        }
                    }
                    _mcpServers.Add(row);
                }
            }
            _mcpError = GetString(mcp, "error");
            // [EN] The state read back from the backend is the truth: after a rebuild,
            // [EN] "unsaved changes" are discarded naturally, so dirty must be cleared here
            // [EN] (otherwise the next save would overwrite with the old list).
            // [ZH] 后端回读的状态就是真相：重排之后"未保存的改动"自然被丢弃，
            // [ZH] 所以这里必须把 dirty 清掉（否则下次保存会拿旧列表覆盖）。
            _mcpServersDirty = false;
            RenderMcpServerRows();
        }
        finally
        {
            _mcpSyncing = false;
        }
    }

    private static List<string> ReadStringList(JsonElement item, string name)
    {
        var list = new List<string>();
        if (item.TryGetProperty(name, out JsonElement array) && array.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement element in array.EnumerateArray())
            {
                if (element.ValueKind == JsonValueKind.String)
                    list.Add(element.GetString() ?? "");
            }
        }
        return list;
    }

    /// <summary>
    /// [EN] Rebuilds the server rows (used both for local add/remove and for a full
    /// [EN] apply-back).
    /// [ZH] 重排 server 行（本地增删与整段回填都走它）。
    /// </summary>
    private void RenderMcpServerRows()
    {
        bool previous = _mcpSyncing;
        _mcpSyncing = true;
        try
        {
            McpServerList.Children.Clear();
            if (_mcpServers.Count == 0)
            {
                McpServerList.Children.Add(new TextBlock
                {
                    Text = Loc.T("settings.mcp.empty", "还没有配置 MCP Server。"),
                    FontSize = 11, Opacity = 0.7,
                });
                return;
            }
            bool globalEnabled = McpEnabledCheck.IsChecked == true;
            foreach (McpServerRow row in _mcpServers)
                McpServerList.Children.Add(BuildMcpServerRow(row, globalEnabled));
        }
        finally
        {
            _mcpSyncing = previous;
        }
    }

    private Control BuildMcpServerRow(McpServerRow row, bool globalEnabled)
    {
        var info = new StackPanel { Spacing = 2 };
        var head = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        head.Children.Add(new TextBlock { Text = row.Name, FontWeight = FontWeight.SemiBold, FontSize = 12 });

        // [EN] Connection status: globally off / this server is disabled / connected /
        // [EN] connection failed / pending save. Make each case clear -- this is the first
        // [EN] place a user looks when checking "why the tools do not appear".
        // [ZH] 连接状态：全局关 / 该 server 未启用 / 已连接 / 连接失败 / 待保存，几种说清楚 ——
        // [ZH] 用户排查"为什么工具没出现"时，第一个要看的就是这里。
        string stateText;
        string brushKey;
        if (!globalEnabled || !row.Enabled)
        {
            stateText = Loc.T("settings.mcp.statusDisabled", "⏸ 未启用");
            brushKey = "MutedForegroundBrush";
        }
        else if (row.Connected)
        {
            stateText = Loc.T("settings.mcp.statusConnected", "✅ 已连接");
            brushKey = "AccentBrush";
        }
        else if (row.Error.Length > 0)
        {
            stateText = Loc.T("settings.mcp.statusFailed", "⚠️ 连接失败：{0}", row.Error);
            brushKey = "AccentBrush";
        }
        else if (row.Known)
        {
            stateText = Loc.T("settings.mcp.statusPending", "⏳ 未连接");
            brushKey = "MutedForegroundBrush";
        }
        else
        {
            stateText = Loc.T("settings.mcp.statusNew", "🆕 待保存");
            brushKey = "AccentBrush";
        }
        head.Children.Add(new TextBlock
        {
            Text = stateText, FontSize = 10, VerticalAlignment = VerticalAlignment.Center,
            Foreground = TryBrush(brushKey), TextWrapping = TextWrapping.Wrap,
        });
        info.Children.Add(head);

        info.Children.Add(new TextBlock
        {
            Text = Loc.T("settings.mcp.commandLine", "命令：{0}", row.CommandText),
            FontSize = 10, Opacity = 0.65, TextWrapping = TextWrapping.Wrap,
        });
        info.Children.Add(new TextBlock
        {
            Text = row.Tools.Count > 0
                ? Loc.T("settings.mcp.toolsFound", "工具 {0} 个：{1}",
                    row.Tools.Count, Shorten(string.Join(", ", row.Tools), 160))
                : Loc.T("settings.mcp.toolsNone", "未发现工具"),
            FontSize = 10, Opacity = 0.65, TextWrapping = TextWrapping.Wrap,
        });
        if (row.PassthroughEnv.Count > 0)
        {
            info.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.mcp.passthrough", "透传环境变量：{0}", string.Join(", ", row.PassthroughEnv)),
                FontSize = 10, TextWrapping = TextWrapping.Wrap, Foreground = TryBrush("AccentBrush"),
            });
        }

        var actions = new StackPanel { Orientation = Orientation.Vertical, Spacing = 4 };
        var check = new CheckBox
        {
            Content = Loc.T("settings.mcp.enableCheck", "启用"),
            FontSize = 11,
            IsChecked = row.Enabled,
            VerticalAlignment = VerticalAlignment.Top,
        };
        AutomationProperties.SetAutomationId(check, "McpServerCheck_" + row.Name);
        check.IsCheckedChanged += (_, _) =>
        {
            if (_mcpSyncing)
                return;
            row.Enabled = check.IsChecked == true;
            _mcpServersDirty = true;
            SetMcpStagedHint();
        };
        actions.Children.Add(check);

        var delete = new Button
        {
            Content = Loc.T("settings.mcp.delete", "🗑 移除"),
            Padding = new Thickness(10, 3),
            FontSize = 11,
        };
        AutomationProperties.SetAutomationId(delete, "McpServerDelete_" + row.Name);
        ToolTip.SetTip(delete, Loc.T("settings.mcp.deleteTip",
            "从列表里移除这个 server（点「💾 保存」后生效；不会删除磁盘上的任何文件）"));
        delete.Click += (_, _) =>
        {
            _mcpServers.Remove(row);
            _mcpServersDirty = true;
            RenderMcpServerRows();
            McpStatusText.Text = Loc.T("settings.mcp.removedHint",
                "已从列表里移除 {0}：点「💾 保存」后才会真的从配置里删掉（不会删除磁盘上的任何文件）。",
                row.Name);
        };
        actions.Children.Add(delete);

        var grid = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        Grid.SetColumn(info, 0);
        Grid.SetColumn(actions, 1);
        grid.Children.Add(info);
        grid.Children.Add(actions);

        var border = new Border { Child = grid };
        border.Classes.Add("card");
        AutomationProperties.SetAutomationId(border, "McpServer_" + row.Name);
        return border;
    }

    /// <summary>
    /// [EN] Tool names can be very long (mcp__server__tool), so flatten them onto one line
    /// [EN] and truncate for display.
    /// [ZH] 工具名可能很长（mcp__server__tool），压成一行并截断显示。
    /// </summary>
    private static string Shorten(string text, int max)
    {
        string flat = text.Replace('\r', ' ').Replace('\n', ' ').Trim();
        return flat.Length <= max ? flat : flat[..max] + "…";
    }

    /// <summary>
    /// [EN] Splits the argument string on whitespace (an empty string → an empty array).
    /// Quote parsing is deliberately not implemented, to avoid "I assumed it understands".
    /// [ZH] 参数串按空白切分（空串 → 空数组）。刻意不做引号解析，避免"我以为它懂"。
    /// </summary>
    private static List<string> SplitMcpArgs(string? text)
    {
        var list = new List<string>();
        foreach (string piece in (text ?? "").Split(
                     new[] { ' ', '\t', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries))
        {
            list.Add(piece);
        }
        return list;
    }

    /// <summary>
    /// [EN] Path of the example MCP server shipped with the package.
    ///
    /// [EN] Why several candidate directories: in the portable layout the program sits in
    /// [EN] `app\` and the examples are in the outer directory; during development the exe
    /// [EN] is in `frontend-avalonia\...\bin\Debug\net10.0\` and the examples are in
    /// [EN] `examples\mcp\` at the repository root. When none is found, the "most likely"
    /// [EN] path is returned so the user can edit it -- more useful than an empty string
    /// [EN] (the user can see at a glance what to fill in).
    /// [ZH] 随包示例 MCP server 的路径。
    ///
    /// [ZH] 为什么要几个候选目录：便携版布局下程序在 `app\`、示例在外层目录；
    /// [ZH] 开发时 exe 在 `frontend-avalonia\...\bin\Debug\net10.0\`，示例在仓库根的
    /// [ZH] `examples\mcp\`。都没找到时返回"最可能的那个路径"让用户自己改 ——
    /// [ZH] 比填一个空串有用（用户一眼能看出该填什么）。
    /// </summary>
    private static string ExampleMcpServerPath()
    {
        var roots = new List<string> { UserPaths.ProgramDirectory, UserPaths.DataRoot };
        string? dir = AppContext.BaseDirectory;
        for (int i = 0; i < 4 && dir is not null; i++)
        {
            roots.Add(dir);
            dir = System.IO.Path.GetDirectoryName(dir.TrimEnd(
                System.IO.Path.DirectorySeparatorChar));
        }
        foreach (string root in roots)
        {
            try
            {
                string candidate = System.IO.Path.Combine(root, "examples", "mcp", "echo_server.py");
                if (System.IO.File.Exists(candidate))
                    return candidate;
            }
            catch (ArgumentException)
            {
                // [EN] Skip this candidate when the path contains invalid characters.
                // [ZH] 路径里有非法字符时跳过这个候选
            }
        }
        return System.IO.Path.Combine(UserPaths.ProgramDirectory, "examples", "mcp", "echo_server.py");
    }

    private void SetMcpStagedHint() =>
        McpStatusText.Text = Loc.T("settings.mcp.stagedHint",
            "改动在点「💾 保存」或「🔌 连接并取工具」后生效（会立刻重新注册工具，不需要重启）。");

    private void ToggleMcpAddForm(bool show)
    {
        McpFormPanel.IsVisible = show;
        if (show)
            McpFormNameBox.Focus();
    }

    /// <summary>
    /// [EN] Saves and applies (when <paramref name="connect"/> is true, a connection is
    /// [EN] also required immediately).
    ///
    /// [EN] The response returns `mcp` and `agent` **together**: the MCP tools were just
    /// [EN] registered, so the tool permission list above is rebuilt as well -- the user
    /// [EN] sees the newly appeared `mcp__…` tools right away (otherwise they would have
    /// [EN] to click "reload" once more).
    /// [ZH] 保存并应用（<paramref name="connect"/> 为真时同时要求立刻连接）。
    ///
    /// [ZH] 响应用 `mcp` + `agent` **一起回传**：MCP 工具刚重新注册过，
    /// [ZH] 所以顺手把上面的工具权限清单也重排一遍 —— 用户能立刻看到新出现的
    /// [ZH] `mcp__…` 工具（否则要自己再点一次「重新读取」）。
    /// </summary>
    private async Task SendMcpApplyAsync(bool connect)
    {
        string timeoutText = (McpTimeoutBox.Text ?? "").Trim();
        if (timeoutText.Length > 0
            && (!double.TryParse(timeoutText, System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture, out double seconds)
                || seconds < 1.0 || seconds > 600.0))
        {
            McpStatusText.Text = Loc.T("settings.mcp.timeoutRange", "⚠️ 超时需要在 {0}–{1} 秒之间", 1, 600);
            return;
        }
        McpStatusText.Text = Loc.T("settings.mcp.saving", "正在保存 MCP 配置…");
        // [EN] Launching an external process may take a while, so the timeout is generous
        // [EN] (the same value as the WPF side).
        // [ZH] 拉起外部进程可能要等一会儿，超时给足（WPF 侧同一取值）
        using JsonDocument? doc = await SendAsync(BuildMcpPatch(connect), 120);
        if (doc is null)
        {
            McpStatusText.Text = Loc.T("settings.mcp.saveFailed",
                "❌ 保存 MCP 配置失败：{0}", Loc.T("settings.mcp.noData", "后端未响应"));
            return;
        }
        string content = GetString(doc.RootElement, "content");
        int registered = 0;
        if (doc.RootElement.TryGetProperty("mcp_apply", out JsonElement apply))
            registered = GetInt(apply, "registered", 0);
        bool hasState = doc.RootElement.TryGetProperty("mcp", out JsonElement mcp);
        if (hasState)
            RenderMcpState(mcp);
        if (doc.RootElement.TryGetProperty("agent", out JsonElement agent))
            RenderAgentState(agent);   // [EN] The MCP tools were just registered, so the
                                       // [EN] tool permission list must follow.
                                       // [ZH] MCP 工具刚注册进来，工具权限清单要跟着更新
        if (content.Length > 0)
        {
            // [EN] The backend only returns content on failure (the reason, prefixed with
            // [EN] the error mark); show it as it is and do not invent a sentence. The UI
            // [EN] is not rebuilt at this point either -- what the user just filled in must
            // [EN] stay so that one correction allows a retry.
            // [ZH] 后端只在失败时给 content（❌ 开头的原因），如实显示，不自己编一句；
            // [ZH] 此时也不重排界面 —— 用户刚填的东西要留着，改一处就能重试。
            McpStatusText.Text = content;
            return;
        }
        McpStatusText.Text = connect
            ? Loc.T("settings.mcp.connected", "✅ 已连接：注册了 {0} 个工具", registered)
            : Loc.T("settings.mcp.saved", "✅ 已保存（工具已重新注册，无需重启）");
        if (!hasState)
            await LoadMcpStateAsync(quiet: true);   // [EN] Read the state once more when the
                                                    // [EN] response did not carry it.
                                                    // [ZH] 响应里没带状态时补读一次
    }

    /// <summary>
    /// [EN] Builds the request body of mcp_apply.
    ///
    /// [EN] **servers is sent only when the list was modified**: once the backend receives
    /// [EN] servers it replaces the whole set (and rebuilds the processes and the tool
    /// [EN] table), while mcp_state **does not return** a per-server env for safety reasons
    /// [EN] -- an unnecessary resend would wipe the env written by hand in config.json. Not
    /// [EN] sending it when the list is unchanged is the strongest protection this release
    /// [EN] can offer (the thorough fix belongs to the backend: put env into mcp_state, or
    /// [EN] let mcp_apply merge by name).
    /// [ZH] 组装 mcp_apply 的请求体。
    ///
    /// [ZH] **servers 只在列表被改过时才发**：后端收到 servers 就整份替换（并重建进程与工具表），
    /// [ZH] 而 mcp_state 出于安全**不回传** per-server 的 env —— 无谓地重发一次就会把
    /// [ZH] config.json 里手写的 env 抹掉。没改列表就不发，这是本版能做的最大保全
    /// [ZH] （彻底修法在后端：把 env 放进 mcp_state，或让 mcp_apply 按 name 合并）。
    /// </summary>
    private Dictionary<string, object?> BuildMcpPatch(bool connect)
    {
        var patch = new Dictionary<string, object?>
        {
            ["action"] = "mcp_apply",
            ["enabled"] = McpEnabledCheck.IsChecked == true,
            ["auto_connect"] = connect || McpAutoConnectCheck.IsChecked == true,
        };
        string timeoutText = (McpTimeoutBox.Text ?? "").Trim();
        if (timeoutText.Length > 0)
            patch["timeout_seconds"] = timeoutText;   // [EN] A string is fine: the backend
                                                      // [EN] converts it to float and clamps
                                                      // [EN] it to 1-600.
                                                      // [ZH] 字符串即可：后端自己转 float 并夹到 1–600
        if (_mcpServersDirty)
        {
            var servers = new List<Dictionary<string, object?>>();
            foreach (McpServerRow row in _mcpServers)
            {
                servers.Add(new Dictionary<string, object?>
                {
                    ["name"] = row.Name,
                    ["command"] = row.Command,
                    ["args"] = row.Args,
                    ["env"] = row.Env,
                    ["enabled"] = row.Enabled,
                    ["passthrough_env"] = row.PassthroughEnv,
                });
            }
            patch["servers"] = servers;
        }
        return patch;
    }

    /// <summary>
    /// [EN] Resolves a brush by semantic key. **It must be resolved on a control instance**,
    /// [EN] not with Application.Current.TryFindResource: application resources live in
    /// [EN] ThemeDictionaries (one set for Light and one for Dark), and a lookup on
    /// [EN] Application does not carry the "current theme variant", so it never finds them
    /// [EN] and returns null. The consequence is that code-generated tool cards and MCP
    /// [EN] cards have no border and no background at all (observed: the tool rows on the
    /// [EN] Agent page look like plain text without cards). TryFindResource on a control
    /// [EN] instance uses ActualThemeVariant.
    /// [ZH] 按语义键取画刷。**必须在控件实例上解析**，不能用 Application.Current.TryFindResource：
    /// [ZH] 应用的资源放在 ThemeDictionaries 里（Light/Dark 各一份），
    /// [ZH] 在 Application 上查不会带上"当前主题变体"，于是永远取不到 —— 返回 null。
    /// [ZH] 后果是代码生成的工具卡片与 MCP 卡片全都画不出边框和底色（实测：Agent 页工具行
    /// [ZH] 看起来像没有卡片的纯文本）。控件实例上的 TryFindResource 会用 ActualThemeVariant。
    /// </summary>
    private Brush? TryBrush(string key)
    {
        return TryGetResource(key, ActualThemeVariant, out object? value) && value is Brush brush ? brush : null;
    }

    private static string GetString(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? ""
            : "";

    private static bool GetBool(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.True;

    private static bool GetBoolOr(JsonElement item, string name, bool fallback) =>
        item.TryGetProperty(name, out JsonElement el)
            ? el.ValueKind == JsonValueKind.True
            : fallback;

    private static int GetInt(JsonElement item, string name, int fallback) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number
            ? el.GetInt32()
            : fallback;

    // -----------------------------------------------------------------
    // [EN] Probe interface (used by `EchoDesk --probe pages`: the editable MCP section)
    // [ZH] 探针接口（`EchoDesk --probe pages` 用：MCP 可编辑那一块）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Observable state of the MCP section (switch apply-back, server row count, form
    /// [EN] and button texts).
    /// [ZH] MCP 小节的可观测状态（开关回填 / 服务器行数 / 表单与按钮文案）。
    /// </summary>
    public string McpSnapshot()
    {
        int rows = 0;
        foreach (Control child in McpServerList.Children)
        {
            if (child is Border)
                rows++;
        }
        return string.Join("; ", new[]
        {
            $"mcpEnabled={(McpEnabledCheck.IsChecked == true ? 1 : 0)}",
            $"mcpAutoConnect={(McpAutoConnectCheck.IsChecked == true ? 1 : 0)}",
            $"mcpTimeout={((McpTimeoutBox.Text ?? "").Length > 0 ? 1 : 0)}",
            $"mcpServerRows={rows}",
            $"mcpEnabledLabel={(string.IsNullOrEmpty(McpEnabledCheck.Content as string) ? 0 : 1)}",
            $"mcpSecurityText={(string.IsNullOrEmpty(McpSecurity.Text) ? 0 : 1)}",
            $"mcpSaveButton={(string.IsNullOrEmpty(McpSaveButton.Content as string) ? 0 : 1)}",
            $"mcpConnectButton={(string.IsNullOrEmpty(McpConnectButton.Content as string) ? 0 : 1)}",
            $"mcpAddButton={(string.IsNullOrEmpty(McpAddButton.Content as string) ? 0 : 1)}",
            $"mcpStatus={(string.IsNullOrEmpty(McpStatusText.Text) ? 0 : 1)}",
            $"agentToolRows={AgentToolList.Children.Count}",
        });
    }

    /// <summary>
    /// [EN] Probe: expands the add-server form and fills in a fake server (**changes the UI
    /// [EN] only, sends no request**).
    /// [ZH] 探针：展开新增服务器表单并填一个假的 server（**只改界面，不发请求**）。
    /// </summary>
    public void ProbeOpenAddForm()
    {
        ToggleMcpAddForm(true);
        McpFormNameBox.Text = "probe-server";
        McpFormCommandBox.Text = "python";
        McpFormArgsBox.Text = "--version";
    }

    /// <summary>
    /// [EN] Probe: submits the two boxes above (verifies "local list + unsaved hint"; does
    /// [EN] not write the backend configuration).
    /// [ZH] 探针：提交上面那两个框（验证"本地列表 + 未保存提示"，不写后端配置）。
    /// </summary>
    public void ProbeSubmitAddForm() => AddMcpServerFromForm();

    /// <summary>
    /// [EN] Probe: reads the MCP state once (a real backend action).
    /// [ZH] 探针：读一次 MCP 状态（真实后端动作）。
    /// </summary>
    public async Task ProbeReloadMcpAsync() => await LoadMcpStateAsync();

    /// <summary>
    /// [EN] Probe: removes the row just added **without sending a request**, so no test
    /// [EN] data is left in the UI. (A real save must go through McpSaveButton -- the probe
    /// [EN] deliberately does not click it, otherwise it would modify the user's
    /// [EN] config.json.)
    /// [ZH] 探针：**不发请求**地撤掉刚加的那一行，避免把测试数据留在界面里。
    /// [ZH] （真要保存必须走 McpSaveButton —— 探针刻意不点它，否则会改到用户的 config.json。）
    /// </summary>
    public void ProbeDropServer(string name)
    {
        _mcpServers.RemoveAll(row => string.Equals(row.Name, name, StringComparison.Ordinal));
        _mcpServersDirty = false;
        RenderMcpServerRows();
    }

    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        AgentTitle.Text = Loc.T("settings.agent.title", "🛡 智能体（Agent）");
        AgentToolsTitle.Text = Loc.T("settings.agent.toolsTitle", "工具权限");
        AgentToolsSubtitle.Text = Loc.T("settings.agent.safeAlwaysOn",
            "安全工具默认可用（不需要授权，也无法关闭）");
        AgentIntroText.Text = Loc.T("settings.agent.intro",
            "智能体可以调用下列工具。开启越多能力越强，风险也越大；只读工具默认允许。");
        AgentStepsLabel.Text = Loc.T("settings.agent.stepsLabel", "最大工具调用数");
        AgentStepsApplyButton.Content = Loc.T("settings.agent.stepsApply", "应用");
        AgentStepsHint.Text = Loc.T("settings.agent.stepsHint",
            "一次任务里最多允许「思考 → 调用工具」的轮数。太小会做不完，太大会让单次请求变慢。");
        McpTitle.Text = Loc.T("settings.mcp.title", "🔌 MCP Server（外部工具）");
        McpReloadButton.Content = Loc.T("settings.mcp.reload", "↻ 重新读取");
        McpSaveButton.Content = Loc.T("settings.mcp.save", "💾 保存");
        McpConnectButton.Content = Loc.T("settings.mcp.connect", "🔌 连接并取工具");
        ToolTip.SetTip(McpSaveButton, Loc.T("settings.mcp.saveTip",
            "把这一节的配置写进 config.json，并立刻重新注册工具（不需要重启）"));
        ToolTip.SetTip(McpConnectButton, Loc.T("settings.mcp.connectTip",
            "保存当前配置并立刻拉起外部进程、取回工具（等价指令：/mcp connect）"));
        McpIntro.Text = Loc.T("settings.mcp.intro",
            "MCP Server 是你自己配置的外部进程：接上之后它提供的工具会出现在上面的工具清单里，Agent 就能按需调用。");
        McpSecurity.Text = Loc.T("settings.mcp.security",
            "⚠️ 安全：MCP 工具一律按敏感工具处理 —— 要在上面的工具权限里另外勾选，Agent 才会调用；"
            + "连接外部进程时默认不透传环境变量（HF_TOKEN 之类的密钥不会被顺手交给它）。只接你自己信任的 Server。");
        McpEnabledCheck.Content = Loc.T("settings.mcp.enabled", "启用 MCP");
        McpAutoConnectCheck.Content = Loc.T("settings.mcp.autoConnect", "启动时连接");
        ToolTip.SetTip(McpAutoConnectCheck, Loc.T("settings.mcp.autoConnectTip",
            "勾选后保存/启动时会真的拉起这些外部进程；不勾选则只在用到时按需连接。"));
        McpTimeoutLabel.Text = Loc.T("settings.mcp.timeoutLabel", "连接超时（秒）");
        McpTimeoutHint.Text = Loc.T("settings.mcp.timeoutHint", "1–600");
        McpListTitle.Text = Loc.T("settings.mcp.listTitle", "MCP Server 列表");
        McpAddButton.Content = Loc.T("settings.mcp.add", "＋ 添加服务器");
        McpAddExampleButton.Content = Loc.T("settings.mcp.addExample", "＋ 加示例服务器");
        ToolTip.SetTip(McpAddExampleButton, Loc.T("settings.mcp.addExampleTip",
            "填入随包示例 examples\\mcp\\echo_server.py（命令 python），用来验证通路"));
        McpFormTitle.Text = Loc.T("settings.mcp.formTitle", "新增 MCP Server");
        McpFormNameLabel.Text = Loc.T("settings.mcp.formName", "名称");
        McpFormCommandLabel.Text = Loc.T("settings.mcp.formCommand", "命令");
        McpFormArgsLabel.Text = Loc.T("settings.mcp.formArgs", "参数（按空格切分）");
        McpFormAddConfirmButton.Content = Loc.T("settings.mcp.formAdd", "添加");
        McpFormCancelButton.Content = Loc.T("settings.mcp.formCancel", "取消");
        McpStagedHint.Text = Loc.T("settings.mcp.stagedHint",
            "改动在点「💾 保存」或「🔌 连接并取工具」后生效（会立刻重新注册工具，不需要重启）。");
        AgentReloadButton.Content = Loc.T("settings.agent.reload", "🔄 重新读取");
        // [EN] Re-layout the loaded data in the new language (the texts of the tool rows
        // [EN] and server rows are composed at runtime).
        // [ZH] 已加载的数据按新语言重排（工具行与服务器行的文案是运行时拼的）。
        if (_host is not null)
            _ = RefreshAsync();
    }
}
