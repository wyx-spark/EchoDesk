// [EN] EchoDesk WPF frontend module: frontend/SettingsWindow.Mcp.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/SettingsWindow.Mcp.cs。类型与成员说明见下方注释。
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using EchoDesk.Services;

namespace EchoDesk;

/// <summary>
/// 设置窗口 ·「智能体」页里的 MCP 小节（能力扩展-1 任务五 B）。
///
/// MCP（Model Context Protocol）Server = 用户自己配置的外部进程，接上之后它提供的工具
/// 会注册进 Agent 的工具表（<c>mcp__&lt;server&gt;__&lt;tool&gt;</c>），模型就能按需调用。
/// 与内置工具的区别：内置工具随程序走，MCP 工具来自第三方进程 —— 所以这里有两道明确的
/// 安全表达：<b>MCP 工具一律按敏感工具处理</b>（要在工具权限区另外勾选）与
/// <b>默认不透传环境变量</b>（密钥不会被顺手交给它）。
///
/// 与后端交互（实现见 backend/main.py 的 mcp_diagnostics / _handle_mcp_apply）：
/// <list type="bullet">
/// <item><c>{"action":"mcp_state"}</c> → <c>mcp</c> 对象（enabled / auto_connect /
/// timeout_seconds / servers[] / tools[] / error）；</item>
/// <item><c>{"action":"mcp_apply", ...}</c> → 保存**并立刻生效**（重建桥接器与工具表，
/// 启用 + auto_connect 时才真的拉起外部进程），响应里同时回传 <c>mcp</c> 与 <c>agent</c>。</item>
/// </list>
///
/// 编辑模型：界面上的改动先落在本地（<see cref="McpServerRow"/> 列表），点「💾 保存」或
/// 「🔌 连接并取工具」才整体提交 —— 因为 mcp_apply 本身就是"整份配置替换"的语义，
/// 攒着一起交比每敲一个键就发一次更符合接口形状（也让"改错了不点保存"成为可能）。
/// </summary>
public partial class SettingsWindow
{
    /// <summary>界面上的一个 MCP Server（本地编辑状态 + 后端回传的只读状态）。</summary>
    private sealed class McpServerRow
    {
        public string Name = "";
        public string Command = "";
        public List<string> Args = new();
        /// <summary>配置里的 env（后端**不回传**这个字段，所以已知 server 也只能是空表）。</summary>
        public Dictionary<string, string> Env = new();
        public bool Enabled = true;
        public List<string> PassthroughEnv = new();

        // 以下来自 mcp_state 的只读状态
        public bool Connected;
        public List<string> Tools = new();
        public string Error = "";
        /// <summary>是否是后端已知的 server（界面上新加的为 false，只用于状态文案）。</summary>
        public bool Known;

        public string CommandText => Args.Count > 0
            ? Command + " " + string.Join(" ", Args)
            : Command;
    }

    // ---------- 控件引用 ----------

    private TextBlock? _mcpTitle;
    private TextBlock? _mcpIntro;
    private TextBlock? _mcpSecurity;
    private TextBlock? _mcpStagedHint;
    private Button? _mcpReloadButton;
    private Button? _mcpSaveButton;
    private Button? _mcpConnectButton;
    private CheckBox? _mcpEnabledCheck;
    private CheckBox? _mcpAutoConnectCheck;
    private TextBlock? _mcpTimeoutLabel;
    private TextBox? _mcpTimeoutBox;
    private TextBlock? _mcpTimeoutHint;
    private TextBlock? _mcpListTitle;
    private Button? _mcpAddButton;
    private Button? _mcpAddExampleButton;
    private StackPanel? _mcpFormPanel;
    private TextBlock? _mcpFormTitle;
    private TextBlock? _mcpFormNameLabel;
    private TextBlock? _mcpFormCommandLabel;
    private TextBlock? _mcpFormArgsLabel;
    private TextBox? _mcpFormNameBox;
    private TextBox? _mcpFormCommandBox;
    private TextBox? _mcpFormArgsBox;
    private Button? _mcpFormAddButton;
    private Button? _mcpFormCancelButton;
    private StackPanel? _mcpServerList;
    private TextBlock? _mcpStatus;

    private readonly List<McpServerRow> _mcpServers = new();

    /// <summary>列表被改过（增/删/启用勾选）→ 保存时要把 servers 整份发回去。见 BuildMcpPatch。</summary>
    private bool _mcpServersDirty;

    /// <summary>是否已生成（语言重排与异步刷新都要先确认它在）。</summary>
    private bool _mcpBuilt;

    /// <summary>回填期间为真：勾选框的程序化改值不该被当成"用户改过"。</summary>
    private bool _mcpSyncing;

    /// <summary>后端报的全局错误（例如某个 server 连接失败的原因）。</summary>
    private string _mcpError = "";

    // ---------- 初始化 ----------

    /// <summary>生成 MCP 小节（由 <see cref="InitAgentUi"/> 调用，挂在「智能体」页的末尾）。</summary>
    private void InitMcpUi(StackPanel page)
    {
        var card = new Border
        {
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(12, 10, 12, 10),
            Margin = new Thickness(0, 12, 0, 0),
        };
        ThemeService.Bind(card, Border.BackgroundProperty, "InputBackground");
        ThemeService.Bind(card, Border.BorderBrushProperty, "InputBorder");

        var root = new StackPanel();
        card.Child = root;
        page.Children.Add(card);

        // ---- 标题行：标题 + 三个操作按钮 ----
        var head = new WrapPanel { Orientation = Orientation.Horizontal };
        _mcpTitle = MakeAgentText("McpTitle", Loc.T("settings.mcp.title", "🔌 MCP Server（外部工具）"), 13, "AccentBrush");
        _mcpTitle.FontWeight = FontWeights.Bold;
        _mcpTitle.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(_mcpTitle);

        _mcpReloadButton = MakeAgentButton("McpReloadButton", Loc.T("settings.mcp.reload", "↻ 重新读取"));
        _mcpReloadButton.Margin = new Thickness(12, 0, 0, 0);
        _mcpReloadButton.Click += McpReloadButton_Click;
        head.Children.Add(_mcpReloadButton);

        _mcpSaveButton = MakeAgentButton("McpSaveButton", Loc.T("settings.mcp.save", "💾 保存"));
        _mcpSaveButton.Margin = new Thickness(8, 0, 0, 0);
        _mcpSaveButton.ToolTip = Loc.T("settings.mcp.saveTip",
            "把这一节的配置写进 config.json，并立刻重新注册工具（不需要重启）");
        _mcpSaveButton.Click += McpSaveButton_Click;
        head.Children.Add(_mcpSaveButton);

        _mcpConnectButton = MakeAgentButton("McpConnectButton", Loc.T("settings.mcp.connect", "🔌 连接并取工具"));
        _mcpConnectButton.Margin = new Thickness(8, 0, 0, 0);
        _mcpConnectButton.ToolTip = Loc.T("settings.mcp.connectTip",
            "保存当前配置并立刻拉起外部进程、取回工具（等价指令：/mcp connect）");
        _mcpConnectButton.Click += McpConnectButton_Click;
        head.Children.Add(_mcpConnectButton);
        root.Children.Add(head);

        _mcpIntro = MakeAgentText("McpIntro", Loc.T("settings.mcp.intro",
            "MCP Server 是你自己配置的外部进程：接上之后它提供的工具会出现在上面的工具清单里，Agent 就能按需调用。"), 11, "SubTextBrush");
        _mcpIntro.TextWrapping = TextWrapping.Wrap;
        _mcpIntro.Margin = new Thickness(0, 6, 0, 0);
        root.Children.Add(_mcpIntro);

        // 安全提示必须显眼：这是"把外部进程接进 Agent"这件事的风险边界，
        // 所以用 WarningBrush + 半粗体单独一行，而不是塞进小字提示里。
        _mcpSecurity = MakeAgentText("McpSecurity", Loc.T("settings.mcp.security",
            "⚠️ 安全：MCP 工具一律按敏感工具处理 —— 要在上面的工具权限里另外勾选，Agent 才会调用；连接外部进程时默认不透传环境变量（HF_TOKEN 之类的密钥不会被顺手交给它）。只接你自己信任的 Server。"),
            11, "WarningBrush");
        _mcpSecurity.FontWeight = FontWeights.SemiBold;
        _mcpSecurity.TextWrapping = TextWrapping.Wrap;
        _mcpSecurity.Margin = new Thickness(0, 6, 0, 0);
        root.Children.Add(_mcpSecurity);

        // ---- 总开关 / 启动时连接 / 超时 ----
        var switches = new WrapPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 8, 0, 0) };
        _mcpEnabledCheck = Identify(new CheckBox
        {
            Content = Loc.T("settings.mcp.enabled", "启用 MCP"),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 14, 0),
        }, "McpEnabledCheck");
        _mcpEnabledCheck.Checked += McpToggle_Changed;
        _mcpEnabledCheck.Unchecked += McpToggle_Changed;
        switches.Children.Add(_mcpEnabledCheck);

        _mcpAutoConnectCheck = Identify(new CheckBox
        {
            Content = Loc.T("settings.mcp.autoConnect", "启动时连接"),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 14, 0),
            ToolTip = Loc.T("settings.mcp.autoConnectTip",
                "勾选后保存/启动时会真的拉起这些外部进程；不勾选则只在用到时按需连接。"),
        }, "McpAutoConnectCheck");
        _mcpAutoConnectCheck.Checked += McpToggle_Changed;
        _mcpAutoConnectCheck.Unchecked += McpToggle_Changed;
        switches.Children.Add(_mcpAutoConnectCheck);

        _mcpTimeoutLabel = MakeAgentText("McpTimeoutLabel", Loc.T("settings.mcp.timeoutLabel", "连接超时（秒）"), 11, null);
        _mcpTimeoutLabel.VerticalAlignment = VerticalAlignment.Center;
        switches.Children.Add(_mcpTimeoutLabel);

        _mcpTimeoutBox = Identify(new TextBox
        {
            Width = 52,
            Margin = new Thickness(6, 0, 0, 0),
            Padding = new Thickness(4, 2, 4, 2),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Text = "20",
            ToolTip = Loc.T("settings.mcp.timeoutHint", "1–600"),
        }, "McpTimeoutBox");
        switches.Children.Add(_mcpTimeoutBox);

        _mcpTimeoutHint = MakeAgentText("McpTimeoutHint", Loc.T("settings.mcp.timeoutHint", "1–600"), 10, "SubTextBrush");
        _mcpTimeoutHint.Margin = new Thickness(6, 0, 0, 0);
        _mcpTimeoutHint.VerticalAlignment = VerticalAlignment.Center;
        switches.Children.Add(_mcpTimeoutHint);
        root.Children.Add(switches);

        _mcpStagedHint = MakeAgentText("McpStagedHint", Loc.T("settings.mcp.stagedHint",
            "改动在点「💾 保存」或「🔌 连接并取工具」后生效（会立刻重新注册工具，不需要重启）。"), 10, "SubTextBrush");
        _mcpStagedHint.TextWrapping = TextWrapping.Wrap;
        _mcpStagedHint.Margin = new Thickness(0, 4, 0, 0);
        root.Children.Add(_mcpStagedHint);

        // ---- server 列表 ----
        var listHead = new WrapPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 12, 0, 0) };
        _mcpListTitle = MakeAgentText("McpListTitle", Loc.T("settings.mcp.listTitle", "MCP Server 列表"), 12, "AccentBrush");
        _mcpListTitle.FontWeight = FontWeights.Bold;
        _mcpListTitle.VerticalAlignment = VerticalAlignment.Center;
        listHead.Children.Add(_mcpListTitle);

        _mcpAddButton = MakeAgentButton("McpAddButton", Loc.T("settings.mcp.add", "＋ 添加服务器"));
        _mcpAddButton.Margin = new Thickness(12, 0, 0, 0);
        _mcpAddButton.Click += (_, _) => ToggleMcpAddForm(true);
        listHead.Children.Add(_mcpAddButton);

        _mcpAddExampleButton = MakeAgentButton("McpAddExampleButton", Loc.T("settings.mcp.addExample", "＋ 加示例服务器"));
        _mcpAddExampleButton.Margin = new Thickness(8, 0, 0, 0);
        _mcpAddExampleButton.ToolTip = Loc.T("settings.mcp.addExampleTip",
            "填入随包示例 examples\\mcp\\echo_server.py（命令 python），用来验证通路");
        _mcpAddExampleButton.Click += McpAddExampleButton_Click;
        listHead.Children.Add(_mcpAddExampleButton);
        root.Children.Add(listHead);

        _mcpFormPanel = Identify(new StackPanel { Margin = new Thickness(0, 6, 0, 0), Visibility = Visibility.Collapsed }, "McpFormPanel");
        BuildMcpAddForm(_mcpFormPanel);
        root.Children.Add(_mcpFormPanel);

        _mcpServerList = Identify(new StackPanel { Margin = new Thickness(0, 6, 0, 0) }, "McpServerList");
        root.Children.Add(_mcpServerList);

        _mcpStatus = MakeAgentText("McpStatusText", "", 11, "SubTextBrush");
        _mcpStatus.TextWrapping = TextWrapping.Wrap;
        _mcpStatus.Margin = new Thickness(0, 8, 0, 0);
        root.Children.Add(_mcpStatus);

        _mcpBuilt = true;
        SetMcpStatus(Loc.T("settings.mcp.readyHint",
            "改动点「💾 保存」后立刻生效：工具会重新注册，不需要重启。"));
    }

    /// <summary>新增 server 的内联表单（默认折叠：「＋ 添加服务器」才展开）。</summary>
    private void BuildMcpAddForm(StackPanel form)
    {
        _mcpFormTitle = MakeAgentText("McpFormTitle", Loc.T("settings.mcp.formTitle", "新增 MCP Server"), 11, "AccentBrush");
        _mcpFormTitle.FontWeight = FontWeights.SemiBold;
        form.Children.Add(_mcpFormTitle);

        _mcpFormNameLabel = MakeAgentText("McpFormNameLabel", Loc.T("settings.mcp.formName", "名称"), 10, "SubTextBrush");
        _mcpFormNameLabel.Margin = new Thickness(0, 4, 0, 0);
        form.Children.Add(_mcpFormNameLabel);
        _mcpFormNameBox = Identify(new TextBox
        {
            FontSize = 11,
            Padding = new Thickness(4, 2, 4, 2),
            Margin = new Thickness(0, 2, 0, 0),
            Width = 240,
            HorizontalAlignment = HorizontalAlignment.Left,
        }, "McpFormNameBox");
        form.Children.Add(_mcpFormNameBox);

        _mcpFormCommandLabel = MakeAgentText("McpFormCommandLabel", Loc.T("settings.mcp.formCommand", "命令"), 10, "SubTextBrush");
        _mcpFormCommandLabel.Margin = new Thickness(0, 6, 0, 0);
        form.Children.Add(_mcpFormCommandLabel);
        _mcpFormCommandBox = Identify(new TextBox
        {
            FontSize = 11,
            Padding = new Thickness(4, 2, 4, 2),
            Margin = new Thickness(0, 2, 0, 0),
            Width = 240,
            HorizontalAlignment = HorizontalAlignment.Left,
        }, "McpFormCommandBox");
        form.Children.Add(_mcpFormCommandBox);

        _mcpFormArgsLabel = MakeAgentText("McpFormArgsLabel", Loc.T("settings.mcp.formArgs", "参数（按空格切分）"), 10, "SubTextBrush");
        _mcpFormArgsLabel.Margin = new Thickness(0, 6, 0, 0);
        form.Children.Add(_mcpFormArgsLabel);
        _mcpFormArgsBox = Identify(new TextBox
        {
            FontSize = 11,
            Padding = new Thickness(4, 2, 4, 2),
            Margin = new Thickness(0, 2, 0, 0),
            MinWidth = 360,
            HorizontalAlignment = HorizontalAlignment.Left,
        }, "McpFormArgsBox");
        form.Children.Add(_mcpFormArgsBox);

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 8, 0, 0) };
        _mcpFormAddButton = MakeAgentButton("McpFormAddConfirmButton", Loc.T("settings.mcp.formAdd", "添加"));
        _mcpFormAddButton.Click += McpFormAddButton_Click;
        buttons.Children.Add(_mcpFormAddButton);
        _mcpFormCancelButton = MakeAgentButton("McpFormCancelButton", Loc.T("settings.mcp.formCancel", "取消"));
        _mcpFormCancelButton.Margin = new Thickness(8, 0, 0, 0);
        _mcpFormCancelButton.Click += (_, _) => ToggleMcpAddForm(false);
        buttons.Children.Add(_mcpFormCancelButton);
        form.Children.Add(buttons);
    }

    // ---------- 事件 ----------

    /// <summary>总开关/自动连接的勾选：只标记"待保存"，不立刻发请求（见类注释的编辑模型）。</summary>
    private void McpToggle_Changed(object sender, RoutedEventArgs e)
    {
        if (_mcpSyncing)
            return;
        SetMcpStatus(Loc.T("settings.mcp.stagedHint",
            "改动在点「💾 保存」或「🔌 连接并取工具」后生效（会立刻重新注册工具，不需要重启）。"));
    }

    private async void McpReloadButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await LoadMcpStateAsync();
        }
        catch (Exception ex)
        {
            SetMcpStatus(Loc.T("settings.mcp.readFailed", "❌ 读取 MCP 状态失败：{0}", ex.Message));
            App.Log("SettingsWindow.McpReload", ex);
        }
    }

    private async void McpSaveButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await SendMcpApplyAsync(connect: _mcpAutoConnectCheck?.IsChecked == true);
        }
        catch (Exception ex)
        {
            SetMcpStatus(Loc.T("settings.mcp.saveFailed", "❌ 保存 MCP 配置失败：{0}", ex.Message));
            App.Log("SettingsWindow.McpSave", ex);
        }
    }

    private async void McpConnectButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            // 「连接并取工具」= 保存 + auto_connect=true：与 /mcp connect 等价。
            // 界面上不勾「启动时连接」也能用一次 —— 这正是这个按钮的用处（试通路）。
            await SendMcpApplyAsync(connect: true);
        }
        catch (Exception ex)
        {
            SetMcpStatus(Loc.T("settings.mcp.saveFailed", "❌ 保存 MCP 配置失败：{0}", ex.Message));
            App.Log("SettingsWindow.McpConnect", ex);
        }
    }

    private void McpAddExampleButton_Click(object sender, RoutedEventArgs e)
    {
        ToggleMcpAddForm(true);
        if (_mcpFormNameBox is not null)
            _mcpFormNameBox.Text = "echo";
        if (_mcpFormCommandBox is not null)
            _mcpFormCommandBox.Text = "python";
        if (_mcpFormArgsBox is not null)
            _mcpFormArgsBox.Text = ExampleMcpServerPath();
        SetMcpStatus(Loc.T("settings.mcp.exampleFilled",
            "已填入示例服务器（命令 python，参数是随包示例脚本的路径）：点「💾 保存」或「🔌 连接并取工具」验证通路。"));
    }

    private void McpFormAddButton_Click(object sender, RoutedEventArgs e)
    {
        string name = _mcpFormNameBox?.Text.Trim() ?? "";
        string command = _mcpFormCommandBox?.Text.Trim() ?? "";
        if (name.Length == 0 || command.Length == 0)
        {
            SetMcpStatus(Loc.T("settings.mcp.formInvalid", "⚠️ 名称与命令都不能为空"));
            return;
        }
        if (_mcpServers.Any(s => string.Equals(s.Name, name, StringComparison.OrdinalIgnoreCase)))
        {
            SetMcpStatus(Loc.T("settings.mcp.formDuplicate", "⚠️ 已经有一个叫「{0}」的 server 了", name));
            return;
        }
        _mcpServers.Add(new McpServerRow
        {
            Name = name,
            Command = command,
            // 参数字符串按空格切分（任务书要求的最简形式）：用户要填带空格的参数时，
            // 直接改 config.json 即可 —— 这里刻意不做引号解析，避免"我以为它懂"。
            Args = SplitMcpArgs(_mcpFormArgsBox?.Text ?? ""),
            Enabled = true,
            Known = false,
        });
        _mcpServersDirty = true;
        ToggleMcpAddForm(false);
        RenderMcpServerRows();
        SetMcpStatus(Loc.T("settings.mcp.stagedHint",
            "改动在点「💾 保存」或「🔌 连接并取工具」后生效（会立刻重新注册工具，不需要重启）。"));
    }

    /// <summary>某个 server 的启用勾选（本地生效，保存时随 servers 一起提交）。</summary>
    private void McpServerCheck_Changed(object sender, RoutedEventArgs e)
    {
        if (_mcpSyncing)
            return;
        if (sender is not CheckBox { Tag: McpServerRow row } box)
            return;
        row.Enabled = box.IsChecked == true;
        _mcpServersDirty = true;
        SetMcpStatus(Loc.T("settings.mcp.stagedHint",
            "改动在点「💾 保存」或「🔌 连接并取工具」后生效（会立刻重新注册工具，不需要重启）。"));
    }

    private void McpServerDelete_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as Button)?.Tag is not McpServerRow row)
            return;
        _mcpServers.Remove(row);
        _mcpServersDirty = true;
        RenderMcpServerRows();
        SetMcpStatus(Loc.T("settings.mcp.removedHint",
            "已从列表里移除 {0}：点「💾 保存」后才会真的从配置里删掉（不会删除磁盘上的任何文件）。", row.Name));
    }

    /// <summary>参数串按空白切分（空串 → 空数组）。</summary>
    private static List<string> SplitMcpArgs(string text)
        => text.Split(new[] { ' ', '\t', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries).ToList();

    /// <summary>
    /// 随包示例 MCP server 的路径。
    ///
    /// 为什么要几个候选目录：便携版布局下程序在 <c>app\</c>、示例在外层目录；
    /// 开发时 exe 在 <c>frontend\bin\Release\net10.0-windows\</c>，示例在仓库根的
    /// <c>examples\mcp\</c>。都没找到时返回"最可能的那个路径"让用户自己改 ——
    /// 比填一个空串有用（用户一眼能看出该填什么）。
    /// </summary>
    private static string ExampleMcpServerPath()
    {
        var roots = new List<string>
        {
            ShortcutService.ProgramDirectory,
            ShortcutService.UserDataDirectory,
            AppContext.BaseDirectory,
        };
        string? dir = AppContext.BaseDirectory;
        for (int i = 0; i < 4 && dir is not null; i++)   // 往上找仓库根（开发场景）
        {
            roots.Add(dir);
            dir = Path.GetDirectoryName(dir.TrimEnd(Path.DirectorySeparatorChar));
        }
        foreach (string root in roots)
        {
            try
            {
                string candidate = Path.Combine(root, "examples", "mcp", "echo_server.py");
                if (File.Exists(candidate))
                    return candidate;
            }
            catch (ArgumentException)
            {
                // 路径里有非法字符时跳过这个候选
            }
        }
        return Path.Combine(ShortcutService.ProgramDirectory, "examples", "mcp", "echo_server.py");
    }

    /// <summary>工具名可能很长（mcp__server__tool），压成一行并截断显示。</summary>
    private static string ShortenForDisplay(string text, int max)
    {
        string flat = text.Replace('\r', ' ').Replace('\n', ' ').Trim();
        return flat.Length <= max ? flat : flat[..max] + "…";
    }

    private void ToggleMcpAddForm(bool show)
    {
        if (_mcpFormPanel is null)
            return;
        _mcpFormPanel.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        if (show)
            _mcpFormNameBox?.Focus();
    }

    // ---------- 读写 ----------

    /// <summary>读回 MCP 状态（进设置页与「↻ 重新读取」都走这里）。</summary>
    private async Task LoadMcpStateAsync(bool quiet = false)
    {
        if (!_mcpBuilt)
            return;
        if (!quiet)
            SetMcpStatus(Loc.T("settings.mcp.readingNow", "正在读取 MCP 状态…"));
        string? json = await RequestWithRetryAsync(
            new { action = "mcp_state" },
            Loc.T("settings.mcp.reading", "读取 MCP 状态"),
            TimeSpan.FromSeconds(60));
        if (json is null)
        {
            if (!quiet)
                SetMcpStatus(Loc.T("settings.mcp.readUnavailable",
                    "⚠️ 未能读取 MCP 状态（后端未响应），可点「↻ 重新读取」重试。"));
            return;
        }
        try
        {
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("mcp", out JsonElement mcp))
            {
                if (!quiet)
                    SetMcpStatus(Loc.T("settings.mcp.readFailed", "❌ 读取 MCP 状态失败：{0}",
                        Loc.T("settings.mcp.noData", "后端未返回 mcp 字段（接口不可用？）")));
                return;
            }
            RenderMcpState(mcp);
            if (!quiet)
                SetMcpStatus(Loc.T("settings.mcp.loaded", "✅ 已读取 MCP 状态"));
        }
        catch (JsonException ex)
        {
            SetMcpStatus(Loc.T("settings.mcp.readFailed", "❌ 读取 MCP 状态失败：{0}", ex.Message));
        }
    }

    /// <summary>把 mcp_state 的响应渲染到控件上（整段状态都由后端决定，界面只是显示）。</summary>
    private void RenderMcpState(JsonElement mcp)
    {
        _mcpSyncing = true;
        try
        {
            if (_mcpEnabledCheck is not null)
                _mcpEnabledCheck.IsChecked = GetBoolOr(mcp, "enabled", false);
            if (_mcpAutoConnectCheck is not null)
                _mcpAutoConnectCheck.IsChecked = GetBoolOr(mcp, "auto_connect", false);
            if (_mcpTimeoutBox is not null)
            {
                double timeout = 20.0;
                if (mcp.TryGetProperty("timeout_seconds", out JsonElement tEl)
                    && tEl.ValueKind == JsonValueKind.Number && tEl.TryGetDouble(out double parsed))
                {
                    timeout = parsed;
                }
                _mcpTimeoutBox.Text = timeout.ToString("0.##");
            }

            _mcpServers.Clear();
            if (mcp.TryGetProperty("servers", out JsonElement servers) && servers.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement s in servers.EnumerateArray())
                {
                    var row = new McpServerRow
                    {
                        Name = GetString(s, "name"),
                        Command = GetString(s, "command"),
                        Enabled = GetBoolOr(s, "enabled", true),
                        Connected = GetBool(s, "connected"),
                        Error = GetString(s, "error"),
                        Known = true,
                    };
                    if (s.TryGetProperty("args", out JsonElement args) && args.ValueKind == JsonValueKind.Array)
                        row.Args = args.EnumerateArray()
                            .Where(a => a.ValueKind == JsonValueKind.String)
                            .Select(a => a.GetString() ?? "").ToList();
                    if (s.TryGetProperty("passthrough_env", out JsonElement pass) && pass.ValueKind == JsonValueKind.Array)
                        row.PassthroughEnv = pass.EnumerateArray()
                            .Where(a => a.ValueKind == JsonValueKind.String)
                            .Select(a => a.GetString() ?? "").ToList();
                    if (s.TryGetProperty("tools", out JsonElement tools) && tools.ValueKind == JsonValueKind.Array)
                        row.Tools = tools.EnumerateArray()
                            .Where(a => a.ValueKind == JsonValueKind.String)
                            .Select(a => a.GetString() ?? "").ToList();
                    // env 目前不在 mcp_state 里（后端有意不回传），能读到就保管起来原样回传，
                    // 读不到就是空表 —— 后端哪天放出来，这里不用改。
                    if (s.TryGetProperty("env", out JsonElement env) && env.ValueKind == JsonValueKind.Object)
                        row.Env = env.EnumerateObject()
                            .Where(p => p.Value.ValueKind == JsonValueKind.String)
                            .ToDictionary(p => p.Name, p => p.Value.GetString() ?? "");
                    _mcpServers.Add(row);
                }
            }
            _mcpError = GetString(mcp, "error");
            // 后端回读的状态就是真相：重排之后"未保存的改动"自然被丢弃，
            // 所以这里必须把 dirty 清掉（否则下次保存会拿旧列表覆盖）。
            _mcpServersDirty = false;
            RenderMcpServerRows();
        }
        finally
        {
            _mcpSyncing = false;
        }
    }

    /// <summary>重排 server 行（本地增删与整段回填都走它）。</summary>
    private void RenderMcpServerRows()
    {
        if (_mcpServerList is null)
            return;
        bool previous = _mcpSyncing;
        _mcpSyncing = true;
        try
        {
            _mcpServerList.Children.Clear();
            if (_mcpServers.Count == 0)
            {
                _mcpServerList.Children.Add(MakeAgentText("McpServerEmpty",
                    Loc.T("settings.mcp.empty", "还没有配置 MCP Server。"), 11, "SubTextBrush"));
                return;
            }
            bool globalEnabled = _mcpEnabledCheck?.IsChecked == true;
            foreach (McpServerRow row in _mcpServers)
                _mcpServerList.Children.Add(BuildMcpServerRow(row, globalEnabled));
        }
        finally
        {
            _mcpSyncing = previous;
        }
    }

    private Border BuildMcpServerRow(McpServerRow row, bool globalEnabled)
    {
        var info = new StackPanel();
        var head = new StackPanel { Orientation = Orientation.Horizontal };
        var nameBlock = MakeAgentText("", row.Name, 12, null);
        nameBlock.FontWeight = FontWeights.SemiBold;
        head.Children.Add(nameBlock);

        // 连接状态：全局关 / 该 server 未启用 / 已连接 / 连接失败 / 待保存，几种说清楚 ——
        // 用户排查"为什么工具没出现"时，第一个要看的就是这里。
        string stateText;
        string stateBrush;
        if (!globalEnabled || !row.Enabled)
        {
            stateText = Loc.T("settings.mcp.statusDisabled", "⏸ 未启用");
            stateBrush = "SubTextBrush";
        }
        else if (row.Connected)
        {
            stateText = Loc.T("settings.mcp.statusConnected", "✅ 已连接");
            stateBrush = "SuccessBrush";
        }
        else if (row.Error.Length > 0)
        {
            stateText = Loc.T("settings.mcp.statusFailed", "⚠️ 连接失败：{0}", row.Error);
            stateBrush = "WarningBrush";
        }
        else if (row.Known)
        {
            stateText = Loc.T("settings.mcp.statusPending", "⏳ 未连接");
            stateBrush = "SubTextBrush";
        }
        else
        {
            stateText = Loc.T("settings.mcp.statusNew", "🆕 待保存");
            stateBrush = "InfoBrush";
        }
        var state = MakeAgentText("", stateText, 10, stateBrush);
        state.Margin = new Thickness(8, 0, 0, 0);
        state.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(state);
        info.Children.Add(head);

        var command = MakeAgentText("", Loc.T("settings.mcp.commandLine", "命令：{0}", row.CommandText), 10, "SubTextBrush");
        command.TextWrapping = TextWrapping.Wrap;
        command.Margin = new Thickness(0, 2, 0, 0);
        info.Children.Add(command);

        var tools = MakeAgentText("", row.Tools.Count > 0
            ? Loc.T("settings.mcp.toolsFound", "工具 {0} 个：{1}",
                row.Tools.Count, ShortenForDisplay(string.Join(", ", row.Tools), 160))
            : Loc.T("settings.mcp.toolsNone", "未发现工具"), 10, "SubTextBrush");
        tools.TextWrapping = TextWrapping.Wrap;
        tools.Margin = new Thickness(0, 2, 0, 0);
        info.Children.Add(tools);

        if (row.PassthroughEnv.Count > 0)
        {
            var pass = MakeAgentText("", Loc.T("settings.mcp.passthrough",
                "透传环境变量：{0}", string.Join(", ", row.PassthroughEnv)), 10, "WarningBrush");
            pass.TextWrapping = TextWrapping.Wrap;
            info.Children.Add(pass);
        }

        var actions = new StackPanel { Orientation = Orientation.Vertical, Margin = new Thickness(10, 0, 0, 0) };
        var check = Identify(new CheckBox
        {
            Content = Loc.T("settings.mcp.enableCheck", "启用"),
            FontSize = 11,
            IsChecked = row.Enabled,
            VerticalAlignment = VerticalAlignment.Top,
        }, "McpServerCheck_" + row.Name);
        check.Tag = row;
        check.Checked += McpServerCheck_Changed;
        check.Unchecked += McpServerCheck_Changed;
        actions.Children.Add(check);

        var delete = MakeAgentButton("McpServerDelete_" + row.Name, Loc.T("settings.mcp.delete", "🗑 移除"));
        delete.Margin = new Thickness(0, 4, 0, 0);
        delete.ToolTip = Loc.T("settings.mcp.deleteTip",
            "从列表里移除这个 server（点「💾 保存」后生效；不会删除磁盘上的任何文件）");
        delete.Tag = row;
        delete.Click += McpServerDelete_Click;
        actions.Children.Add(delete);

        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        Grid.SetColumn(info, 0);
        Grid.SetColumn(actions, 1);
        grid.Children.Add(info);
        grid.Children.Add(actions);

        var border = new Border
        {
            Padding = new Thickness(8, 6, 8, 6),
            Margin = new Thickness(0, 3, 0, 0),
            CornerRadius = new CornerRadius(4),
            BorderThickness = new Thickness(1),
            Child = grid,
        };
        ThemeService.Bind(border, Border.BackgroundProperty, "WindowBackground");
        ThemeService.Bind(border, Border.BorderBrushProperty, "InputBorder");
        Identify(border, "McpServer_" + row.Name);
        return border;
    }

    /// <summary>
    /// 保存并应用（<paramref name="connect"/> 为真时同时要求立刻连接）。
    ///
    /// 响应用 `mcp` + `agent` **一起回传**：MCP 工具刚重新注册过，
    /// 所以顺手把上面的工具权限清单也重排一遍 —— 用户能立刻看到新出现的
    /// <c>mcp__…</c> 工具（否则要自己再点一次「重新读取」）。
    /// </summary>
    private async Task SendMcpApplyAsync(bool connect)
    {
        if (!_mcpBuilt || _mcpStatus is null)
            return;
        string timeoutText = _mcpTimeoutBox?.Text.Trim() ?? "";
        if (timeoutText.Length > 0)
        {
            if (!double.TryParse(timeoutText, System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture, out double seconds)
                || seconds < 1.0 || seconds > 600.0)
            {
                SetMcpStatus(Loc.T("settings.mcp.timeoutRange", "⚠️ 超时需要在 {0}–{1} 秒之间", 1, 600));
                return;
            }
        }
        SetMcpStatus(Loc.T("settings.mcp.saving", "正在保存 MCP 配置…"));
        try
        {
            string json = await _backend.SendRequestAsync(BuildMcpPatch(connect),
                idleTimeout: TimeSpan.FromSeconds(120));   // 拉起外部进程可能要等一会儿
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = GetString(doc.RootElement, "content");
            int registered = 0;
            if (doc.RootElement.TryGetProperty("mcp_apply", out JsonElement apply))
                registered = GetInt(apply, "registered", 0);
            bool hasState = doc.RootElement.TryGetProperty("mcp", out JsonElement mcp);
            if (hasState)
                RenderMcpState(mcp);
            if (doc.RootElement.TryGetProperty("agent", out JsonElement agent))
                RenderAgentState(agent);   // MCP 工具刚注册进来，工具权限清单要跟着更新
            if (content.Length > 0)
            {
                // 后端只在失败时给 content（❌ 开头的原因），如实显示，不自己编一句；
                // 此时也不重排界面 —— 用户刚填的东西要留着，改一处就能重试。
                SetMcpStatus(content);
                return;
            }
            SetMcpStatus(connect
                ? Loc.T("settings.mcp.connected", "✅ 已连接：注册了 {0} 个工具", registered)
                : Loc.T("settings.mcp.saved", "✅ 已保存（工具已重新注册，无需重启）"));
            if (!hasState)
                await LoadMcpStateAsync(quiet: true);   // 响应里没带状态时补读一次
        }
        catch (Exception ex)
        {
            SetMcpStatus(Loc.T("settings.mcp.saveFailed", "❌ 保存 MCP 配置失败：{0}", ex.Message));
        }
    }

    /// <summary>
    /// 组装 mcp_apply 的请求体。
    ///
    /// **servers 只在列表被改过时才发**：后端收到 servers 就整份替换（并重建进程与工具表），
    /// 而 mcp_state 出于安全**不回传** per-server 的 env —— 无谓地重发一次就会把
    /// config.json 里手写的 env 抹掉。没改列表就不发，这是本版能做的最大保全
    /// （彻底修法在后端：把 env 放进 mcp_state，或让 mcp_apply 按 name 合并）。
    /// </summary>
    private Dictionary<string, object?> BuildMcpPatch(bool connect)
    {
        var patch = new Dictionary<string, object?>
        {
            ["action"] = "mcp_apply",
            ["enabled"] = _mcpEnabledCheck?.IsChecked == true,
            ["auto_connect"] = connect || _mcpAutoConnectCheck?.IsChecked == true,
        };
        string timeoutText = _mcpTimeoutBox?.Text.Trim() ?? "";
        if (timeoutText.Length > 0)
            patch["timeout_seconds"] = timeoutText;   // 字符串即可：后端自己转 float 并夹到 1–600
        if (_mcpServersDirty)
        {
            patch["servers"] = _mcpServers.Select(row => new Dictionary<string, object?>
            {
                ["name"] = row.Name,
                ["command"] = row.Command,
                ["args"] = row.Args,
                ["env"] = row.Env,
                ["enabled"] = row.Enabled,
                ["passthrough_env"] = row.PassthroughEnv,
            }).ToList();
        }
        return patch;
    }

    private void SetMcpStatus(string text)
    {
        if (_mcpStatus is null)
            return;
        _mcpStatus.Text = text;
    }

    /// <summary>
    /// 按新语言重排 MCP 小节（供 <see cref="Loc.LanguageChanged"/> 调用）。
    /// tool 名、命令、错误文本是后端数据，不重排。
    /// </summary>
    private void RelocalizeMcp()
    {
        if (!_mcpBuilt)
            return;
        if (_mcpTitle is not null)
            _mcpTitle.Text = Loc.T("settings.mcp.title", "🔌 MCP Server（外部工具）");
        if (_mcpIntro is not null)
            _mcpIntro.Text = Loc.T("settings.mcp.intro",
                "MCP Server 是你自己配置的外部进程：接上之后它提供的工具会出现在上面的工具清单里，Agent 就能按需调用。");
        if (_mcpSecurity is not null)
            _mcpSecurity.Text = Loc.T("settings.mcp.security",
                "⚠️ 安全：MCP 工具一律按敏感工具处理 —— 要在上面的工具权限里另外勾选，Agent 才会调用；连接外部进程时默认不透传环境变量（HF_TOKEN 之类的密钥不会被顺手交给它）。只接你自己信任的 Server。");
        if (_mcpStagedHint is not null)
            _mcpStagedHint.Text = Loc.T("settings.mcp.stagedHint",
                "改动在点「💾 保存」或「🔌 连接并取工具」后生效（会立刻重新注册工具，不需要重启）。");
        if (_mcpReloadButton is not null)
            _mcpReloadButton.Content = Loc.T("settings.mcp.reload", "↻ 重新读取");
        if (_mcpSaveButton is not null)
            _mcpSaveButton.Content = Loc.T("settings.mcp.save", "💾 保存");
        if (_mcpConnectButton is not null)
            _mcpConnectButton.Content = Loc.T("settings.mcp.connect", "🔌 连接并取工具");
        if (_mcpEnabledCheck is not null)
            _mcpEnabledCheck.Content = Loc.T("settings.mcp.enabled", "启用 MCP");
        if (_mcpAutoConnectCheck is not null)
            _mcpAutoConnectCheck.Content = Loc.T("settings.mcp.autoConnect", "启动时连接");
        if (_mcpTimeoutLabel is not null)
            _mcpTimeoutLabel.Text = Loc.T("settings.mcp.timeoutLabel", "连接超时（秒）");
        if (_mcpTimeoutHint is not null)
            _mcpTimeoutHint.Text = Loc.T("settings.mcp.timeoutHint", "1–600");
        if (_mcpListTitle is not null)
            _mcpListTitle.Text = Loc.T("settings.mcp.listTitle", "MCP Server 列表");
        if (_mcpAddButton is not null)
            _mcpAddButton.Content = Loc.T("settings.mcp.add", "＋ 添加服务器");
        if (_mcpAddExampleButton is not null)
            _mcpAddExampleButton.Content = Loc.T("settings.mcp.addExample", "＋ 加示例服务器");
        if (_mcpFormTitle is not null)
            _mcpFormTitle.Text = Loc.T("settings.mcp.formTitle", "新增 MCP Server");
        if (_mcpFormNameLabel is not null)
            _mcpFormNameLabel.Text = Loc.T("settings.mcp.formName", "名称");
        if (_mcpFormCommandLabel is not null)
            _mcpFormCommandLabel.Text = Loc.T("settings.mcp.formCommand", "命令");
        if (_mcpFormArgsLabel is not null)
            _mcpFormArgsLabel.Text = Loc.T("settings.mcp.formArgs", "参数（按空格切分）");
        if (_mcpFormAddButton is not null)
            _mcpFormAddButton.Content = Loc.T("settings.mcp.formAdd", "添加");
        if (_mcpFormCancelButton is not null)
            _mcpFormCancelButton.Content = Loc.T("settings.mcp.formCancel", "取消");
        RenderMcpServerRows();
        if (_mcpError.Length > 0)
            SetMcpStatus(Loc.T("settings.mcp.mcpError", "⚠️ MCP：{0}", _mcpError));
    }
}
