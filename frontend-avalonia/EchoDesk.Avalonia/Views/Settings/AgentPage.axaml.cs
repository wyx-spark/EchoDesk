// 智能体页的交互逻辑（v0.2.1 跨平台-2 任务一，第 6 个迁移模块）。
//
// 两个后端动作的数据形状（与 WPF 版一致）：
//   agent_state → { agent: { mode, max_steps, tools: [ { name, risk, description,
//                            args_hint, enabled } ] } }
//   mcp_state   → { mcp: { enabled, auto_connect, timeout_seconds,
//                          servers: [ { name, command, args[], enabled, connected,
//                                       error, tools[] } ] } }
//   写：agent_configure { tool, allowed } 或 { steps }
//
// **工具授权的默认值来自后端**：`risk == "safe"` 的工具在没有明确设置时默认开启
// （WPF 版同一口径）。前端不自己发明默认值——否则"界面显示开着、后端其实关着"
// 这类不一致会出现在安全相关的功能上，代价太大。
//
// **MCP 本轮只做只读展示**：完整编辑（增删服务器、改命令与参数、env 透传、
// 超时、连接测试）需要一整块表单，属于下一轮。这里显示状态与"为什么没迁移"，
// 而不是摆一个点了没反应的界面 —— 与已安装页、开发者页的处置口径一致。
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
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
    }

    public string TabKey => "settings.tab.agent";
    public string TabFallback => "智能体";
    public string AutomationId => "SettingsTabAgent";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }
    public Task ApplyToBackendAsync() => Task.CompletedTask;

    // -----------------------------------------------------------------
    private async Task<JsonDocument?> SendAsync(object request)
    {
        if (_host is null)
            return null;
        return await _host.State.Backend.SendAsync(request, idleTimeout: TimeSpan.FromSeconds(60));
    }

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        await LoadAgentStateAsync();
        await LoadMcpStateAsync();
    }

    // -----------------------------------------------------------------
    // 工具授权
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
                // 默认值：safe 工具默认开启，其余默认关闭（与后端同一口径）。
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

        AgentStateText.Text = Loc.T("settings.agent.state",
            "已授权 {0} / {1} 个工具　·　最大步数 {2}", enabled, total, maxSteps);
        AgentStatusText.Text = Loc.T("settings.agent.loaded", "✅ 已读取智能体状态");
    }

    /// <summary>一行工具授权：左侧名称 + 风险标签 + 说明，右侧开关。</summary>
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
                Text = $"{Loc.T("settings.agent.argsHint", "参数")}：{argsHint}",
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
            IsChecked = isEnabled,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(10, 0, 0, 0),
        };
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
                : Loc.T("settings.agent.saveFailed", "❌ 保存失败（后端未响应）");
            await LoadAgentStateAsync();
        };

        var grid = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        Grid.SetColumn(info, 0);
        Grid.SetColumn(check, 1);
        grid.Children.Add(info);
        grid.Children.Add(check);

        return new Border
        {
            Background = TryBrush("CardBackgroundBrush"),
            BorderBrush = TryBrush("CardBorderBrush"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(12, 8),
            Child = grid,
        };
    }

    private async Task ApplyStepsAsync()
    {
        if (_host is null || _syncing)
            return;
        if (!int.TryParse(AgentStepsBox.Text?.Trim(), out int steps))
        {
            AgentStatusText.Text = Loc.T("settings.agent.stepsRange",
                "⚠️ 步数必须是一个整数（建议 1–12）");
            return;
        }
        AgentStatusText.Text = Loc.T("settings.agent.saving", "⏳ 正在保存…");
        using JsonDocument? doc = await SendAsync(new { action = "agent_configure", steps });
        AgentStatusText.Text = doc is not null
            ? Loc.T("settings.agent.stepsDone", "✅ 最大步数已设为 {0}", steps)
            : Loc.T("settings.agent.saveFailed", "❌ 保存失败（后端未响应）");
        await LoadAgentStateAsync();
    }

    // -----------------------------------------------------------------
    // MCP（只读）
    // -----------------------------------------------------------------
    private async Task LoadMcpStateAsync()
    {
        using JsonDocument? doc = await SendAsync(new { action = "mcp_state" });
        if (doc is null || !doc.RootElement.TryGetProperty("mcp", out JsonElement mcp))
        {
            McpSummaryText.Text = Loc.T("settings.mcp.readUnavailable",
                "⚠️ 后端不可用，无法读取 MCP 状态");
            return;
        }

        bool enabled = GetBool(mcp, "enabled");
        bool autoConnect = GetBool(mcp, "auto_connect");
        double timeout = mcp.TryGetProperty("timeout_seconds", out JsonElement t)
                         && t.ValueKind == JsonValueKind.Number ? t.GetDouble() : 20.0;
        McpSummaryText.Text = string.Join("　·　", new[]
        {
            Loc.T("settings.mcp.enabled", "MCP") + "：" + (enabled ? "✔" : "✘"),
            Loc.T("settings.mcp.autoConnect", "自动连接") + "：" + (autoConnect ? "✔" : "✘"),
            Loc.T("settings.mcp.timeoutLabel", "超时") + $"：{timeout:0.##}s",
        });

        var rows = new List<Control>();
        if (mcp.TryGetProperty("servers", out JsonElement servers) && servers.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement server in servers.EnumerateArray())
            {
                string name = GetString(server, "name");
                string command = GetString(server, "command");
                bool serverEnabled = GetBoolOr(server, "enabled", true);
                bool connected = GetBool(server, "connected");
                string error = GetString(server, "error");
                int toolCount = 0;
                if (server.TryGetProperty("tools", out JsonElement tools) && tools.ValueKind == JsonValueKind.Array)
                    toolCount = tools.GetArrayLength();

                (string statusText, string brushKey) = error.Length > 0
                    ? (Loc.T("settings.mcp.statusFailed", "连接失败"), "AccentBrush")
                    : !serverEnabled
                        ? (Loc.T("settings.mcp.statusDisabled", "已禁用"), "MutedForegroundBrush")
                        : connected
                            ? (Loc.T("settings.mcp.statusConnected", "已连接"), "AccentBrush")
                            : (Loc.T("settings.mcp.statusPending", "未连接"), "MutedForegroundBrush");

                var info = new StackPanel { Spacing = 2 };
                var head = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
                head.Children.Add(new TextBlock { Text = name, FontWeight = FontWeight.SemiBold, FontSize = 12 });
                head.Children.Add(new TextBlock
                {
                    Text = statusText, FontSize = 10, VerticalAlignment = VerticalAlignment.Center,
                    Foreground = TryBrush(brushKey),
                });
                head.Children.Add(new TextBlock
                {
                    Text = toolCount > 0
                        ? Loc.T("settings.mcp.toolsFound", "{0} 个工具", toolCount)
                        : Loc.T("settings.mcp.toolsNone", "没有工具"),
                    FontSize = 10, Opacity = 0.6, VerticalAlignment = VerticalAlignment.Center,
                });
                info.Children.Add(head);
                info.Children.Add(new TextBlock
                {
                    Text = Loc.T("settings.mcp.commandLine", "命令行") + "：" + command,
                    FontSize = 10, Opacity = 0.65, TextWrapping = TextWrapping.Wrap,
                });
                if (error.Length > 0)
                    info.Children.Add(new TextBlock
                    {
                        Text = Loc.T("settings.mcp.mcpError", "错误") + "：" + error,
                        FontSize = 10, TextWrapping = TextWrapping.Wrap, Foreground = TryBrush("AccentBrush"),
                    });

                rows.Add(new Border
                {
                    Background = TryBrush("CardBackgroundBrush"),
                    BorderBrush = TryBrush("CardBorderBrush"),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(6),
                    Padding = new Thickness(12, 8),
                    Child = info,
                });
            }
        }

        McpServerList.Children.Clear();
        if (rows.Count == 0)
        {
            McpServerList.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.mcp.empty", "还没有配置任何 MCP 服务器。"),
                FontSize = 11, Opacity = 0.7,
            });
        }
        else
        {
            foreach (Control row in rows)
                McpServerList.Children.Add(row);
        }
    }

    private static Brush? TryBrush(string key)
    {
        if (Application.Current is null)
            return null;
        if (Application.Current.TryFindResource(key, out object? value) && value is Brush brush)
            return brush;
        return null;
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
    public void ApplyTexts()
    {
        AgentToolsTitle.Text = Loc.T("settings.agent.toolsTitle", "工具授权");
        AgentIntroText.Text = Loc.T("settings.agent.intro",
            "智能体可以调用下列工具。开启越多能力越强，风险也越大；只读工具默认允许。");
        AgentStepsLabel.Text = Loc.T("settings.agent.stepsLabel", "最大步数");
        AgentStepsApplyButton.Content = Loc.T("settings.agent.stepsApply", "应用");
        AgentStepsHint.Text = Loc.T("settings.agent.stepsHint",
            "一次任务里最多允许「思考 → 调用工具」的轮数。太小会做不完，太大会让单次请求变慢。");
        McpTitle.Text = Loc.T("settings.mcp.title", "MCP 服务器");
        McpNotMigratedText.Text = Loc.T("settings.mcp.notMigrated",
            "⏳ MCP 的增删改（命令、参数、环境变量透传、超时、连接测试）还没有迁移到跨平台前端，"
            + "这里只显示当前状态。替代做法：编辑 app/backend/config.json 的 mcp 段，"
            + "或在 Windows 上用 WPF 版设置页。");
        AgentReloadButton.Content = Loc.T("settings.agent.reload", "🔄 重新读取");
        // 已加载的数据按新语言重排（工具行与服务器行的文案是运行时拼的）。
        if (_host is not null)
            _ = RefreshAsync();
    }
}
