using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using EchoDesk.Services;
using EchoDesk.Utilities;

namespace EchoDesk;

public partial class MainWindow : Window
{
    private const string ModePrefix = "✅ 已切换模式为: ";
    private const string ThanksMessage = "👋 感谢使用 Void Entropia Studio 出品的 EchoDesk，欢迎您下次使用！";

    private BackendService? _backend;
    private bool _isProcessing;
    private bool _isDark;
    private bool _stopRequested;
    private bool _stopMode;          // 发送按钮当前处于"停止"模式（流式生成中）
    private bool _commandLike;       // 聊天输入框出现指令格式（"/"+英文字母）
    private bool _commandRowOpen;    // 指令输入行是否展开
    private double _fontSize = 14;
    private TextBox? _paletteSource;
    private DispatcherTimer? _exitTimer;
    private int _exitCountdown;
    private readonly SnippetManager _snippets = new();
    // 下载/预热等实时进度在对话框中的"活"消息（内容块），复用更新避免刷屏
    private TextBlock? _liveStatusContent;

    // 流式 Markdown 实时渲染：节流计时器，边生成边把累积文本渲染进 RichTextBox
    private DispatcherTimer? _mdTimer;
    private RichTextBox? _mdTarget;
    private string _mdPending = "";

    // 聊天输入框中的指令前缀（"/" + 连续英文字母，允许只有一个 "/"）
    private static readonly Regex CommandPrefixRegex = new(@"^/[A-Za-z]*(?: [A-Za-z]+)*", RegexOptions.Compiled);

    // 指令数据（/help 第一行展示 /help 自身；Format 为参数格式，Description 用于完整帮助）
    private static readonly CommandItem[] Commands =
    {
        new("/help", "", "显示此指令介绍：完整指令列表、用法与作用", "显示全部指令及用法", false),
        new("/mode", "<编号|缩写|全称>", "切换对话模式（general/summary/translate/code/contract/creative）；/mode list 查看全部模式及编号；切换时自动套用温度预设", "切换对话模式", true),
        new("/file", "<路径>", "上传本地文件并加入上下文；支持 Office/TXT/PDF，加密 Office 文档在装有 WPS 的机器上自动解密；提问时提及文件即自动注入内容", "上传文件加入上下文", true),
        new("/search", "on|off", "开启/关闭联网搜索（DuckDuckGo）；开启后提问前会先搜索网络参考信息（需联网）", "开启/关闭联网搜索", true),
        new("/model", "[名称|别名|list]", "查看模型信息（架构/量化/上下文/GPU），或切换本地模型；支持档案别名与前缀匹配；缺失时征询后下载", "查看/切换本地模型", true),
        new("/clearcache", "", "清空推理 KV 缓存（模式/提示词/历史变化时系统会自动清理）", "清空推理 KV 缓存", false),
        new("/prompt", "<文本>|reset", "设置自定义系统提示词，临时改变助手角色；/prompt reset 清除", "设置自定义系统提示词", true),
        new("/temp", "<0.0~1.0>|reset", "调整生成温度（默认随模式预设）；/temp reset 恢复模式预设", "调整生成温度", true),
        new("/darkmode", "on|off", "切换深色/浅色界面主题（即时渲染，无需重启）", "切换深/浅色主题", true),
        new("/stop", "", "停止当前正在生成中的回复（也可点 ⏹ 停止 按钮）", "停止当前生成", false),
        new("/snippet", "add|remove|list …", "管理常用语模板：/snippet add 名称 文本 · remove 名称 · list", "管理常用语模板", true),
        new("/status", "", "查看当前状态：模型/架构、模式、温度、线程、GPU、上下文占用、搜索、对话轮数与文件", "查看当前状态与占用", false),
        new("/clear", "", "清空对话历史与文件上下文，重新开始", "清空对话重新开始", false),
        new("/undo", "", "撤回最近一轮问答（各一条）", "撤回最近一轮问答", false),
        new("/save", "<文件名>", "把对话历史导出为 Markdown 文件（自动补 .md 后缀）", "导出对话为 Markdown", true),
        new("/about", "", "关于 EchoDesk：版本号与出品方信息", "关于 EchoDesk", false),
        new("/exit", "", "退出程序：显示感谢语后 10 秒自动关闭", "退出程序", false),
    };

    public MainWindow()
    {
        InitializeComponent();
        AllowDrop = true; // 允许文件拖拽
        Loaded += MainWindow_Loaded;
    }

    // ---------- 生命周期 ----------

    private async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        _backend = new BackendService();
        // 后端连接完成前禁用全部控件，并提示连接状态
        SetControlsEnabled(false);
        StatusText.Text = "正在连接后端...";
        AddMessage("system", "⏳ 正在连接后端，请稍候...");
        // 实时显示后端起模型下载等 stderr 进度
        _backend.StderrReceived += Backend_StderrReceived;
        // 后端异常退出时在对话框提示，避免用户只看上方状态栏
        _backend.ProcessExited += Backend_ProcessExited;
        try
        {
            // 等待后端完成模型加载 + 会话恢复 + 预热后就绪
            await _backend.StartAsync();
            _backend.StderrReceived -= Backend_StderrReceived;
            SetControlsEnabled(true);
            StatusText.Text = "已就绪";
            string welcome = $"✨ 欢迎使用 EchoDesk {_backend.Version} — Powered by {_backend.Studio}";
            if (_backend.RestoredCount > 0)
                welcome += $"\n📂 已自动恢复上次会话（{_backend.RestoredCount} 轮）";
            AddMessage("system", welcome);
            // 展示恢复的历史消息，让用户能查看之前的对话内容
            foreach (var (role, content) in _backend.RestoredHistory)
            {
                if (role == "user")
                    AddMessage("user", content);
                else if (role == "assistant")
                    AddMessage("assistant", content);
                else
                    AddMessage("system", content);
            }
            MessageScrollViewer.ScrollToBottom();
            InputBox.Focus();
            _ = UpdateContextAsync(); // 显示初始上下文占用
        }
        catch (Exception ex)
        {
            StatusText.Text = "后端启动失败";
            AddMessage("system", $"❌ 后端启动失败: {ex.Message}");
            SetControlsEnabled(false); // 保持禁用
        }
    }

    /// <summary>后端 stderr 进度（下载/预热等）：完整内容进对话框，状态栏只显示简化状态。</summary>
    private void Backend_StderrReceived(string line)
    {
        // 只展示下载/加载/预热相关进度，避免无关日志刷屏
        if (line.Contains("正在下载") || line.Contains("下载") || line.Contains("加载模型") || line.Contains("预热"))
        {
            Dispatcher.Invoke(() =>
            {
                UpdateLiveStatus(line.Trim());
                StatusText.Text = SimplifyStatus(line.Trim());
            });
        }
    }

    /// <summary>把完整进度写进对话框：复用同一条"活"消息，避免每条百分比都新增一条。</summary>
    private void UpdateLiveStatus(string text)
    {
        if (_liveStatusContent is null)
        {
            var panel = NewMessagePanel("system", text, out _liveStatusContent);
            MessagePanel.Children.Add(panel);
        }
        else
        {
            _liveStatusContent.Text = "ℹ️ " + text;
        }
        MessageScrollViewer.ScrollToBottom();
    }

    /// <summary>把详细进度行简化为状态栏短文本。</summary>
    private static string SimplifyStatus(string line)
    {
        if (line.Contains("预热")) return "正在预热模型……";
        if (line.Contains("下载")) return "正在下载模型……";
        if (line.Contains("加载")) return "正在加载模型……";
        return line;
    }

    /// <summary>后端进程异常退出时，在对话框中显著提示（后台线程触发，需调度回 UI）。</summary>
    private void Backend_ProcessExited(int exitCode)
    {
        Dispatcher.Invoke(() =>
        {
            StatusText.Text = "后端已退出";
            AddMessage("system", $"⚠️ 后端进程意外退出（退出码 {exitCode}）。请检查程序所在目录的 backend_crash.log，或重启 EchoDesk。");
        });
    }

    /// <summary>统一启用/禁用输入区域所有控件（连接后端期间禁用）。</summary>
    private void SetControlsEnabled(bool enabled)
    {
        SendButton.IsEnabled = enabled;
        UploadButton.IsEnabled = enabled;
        SnippetButton.IsEnabled = enabled;
        CommandButton.IsEnabled = enabled;
        ContextButton.IsEnabled = enabled;
        InputBox.IsEnabled = enabled;
        CommandInputBox.IsEnabled = enabled;
    }

    protected override void OnClosed(EventArgs e)
    {
        _exitTimer?.Stop();
        _backend?.Dispose();
        base.OnClosed(e);
    }

    // ---------- 发送消息（对话输入栏） ----------

    private async void SendButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        if (_stopMode)
        {
            await TriggerStopAsync();
            return;
        }
        await SendMessageAsync();
    }

    private async void InputBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        // Ctrl+Enter 发送；其余（Enter / Shift+Enter）均插入换行
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.Control)
        {
            e.Handled = true;
            await SendMessageAsync();
        }
    }

    private void InputBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        CloseCommandPalette(); // 指令选择栏仅由指令输入框触发
        UpdateCommandUi(InputBox.Text);
    }

    private void InputBox_GotKeyboardFocus(object sender, KeyboardFocusChangedEventArgs e) => CloseAllPalettes();

    private async Task SendMessageAsync()
    {
        if (_isProcessing || _backend is null)
            return;
        string message = InputBox.Text.Trim();
        if (message.Length == 0)
            return;

        InputBox.Text = "";
        SetProcessing(true);
        AddMessage("user", message);

        try
        {
            if (message.Equals("/help", StringComparison.OrdinalIgnoreCase))
                ShowHelpLocally();
            else if (message.Equals("/stop", StringComparison.OrdinalIgnoreCase))
                await TriggerStopAsync();
            else if (message.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(message);
            else if (message.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(message); // 主题为前端职责，本地处理
            else if (message.StartsWith("/"))
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message }));
            else
                await SendStreamAsync(message); // 普通对话走流式输出
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 请求失败: {ex.Message}");
        }
        finally
        {
            SetProcessing(false);
        }
    }

    // ---------- 流式对话输出 ----------

    private async Task SendStreamAsync(string message)
    {
        // 从一开始就使用 RichTextBox，流式过程中即按 Markdown 实时渲染
        var panel = NewMessagePanel("assistant", "", out TextBlock contentBlock);
        panel.Children.Remove(contentBlock);
        var rich = new RichTextBox
        {
            IsReadOnly = true,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            FontSize = _fontSize,
            Tag = "content",
            IsDocumentEnabled = true,
        };
        panel.Children.Insert(1, rich);
        MessagePanel.Children.Add(panel);

        var sb = new StringBuilder("🤖 ");
        _mdTarget = rich;
        RenderInto(rich, sb.ToString()); // 初始只显示图标
        _stopRequested = false;
        _stopMode = true;
        SendButton.Content = "⏹";
        SendButton.ToolTip = "停止生成";
        SendButton.IsEnabled = true; // 流式期间按钮变为"停止"，必须可点击

        try
        {
            string full = await _backend!.SendStreamRequestAsync(
                new { action = "chat_stream", message },
                delta =>
                {
                    sb.Append(delta);
                    ScheduleMarkdownRender(sb.ToString()); // 节流实时渲染
                    MessageScrollViewer.ScrollToBottom();
                },
                onContext: (used, limit) => ContextText.Text = FormatContext(used, limit));
            if (_stopRequested && full.Length == 0)
                sb.Append("\n⏹（已停止生成）");
            FinalizeMarkdownRender(sb.ToString()); // 结束：完整渲染一次
        }
        finally
        {
            StopMarkdownTimer();
            _stopMode = false;
            SendButton.Content = "➤";
            SendButton.ToolTip = "发送 (Ctrl+Enter)";
            SendButton.IsEnabled = true;
        }
    }

    // ---------- 流式 Markdown 实时渲染（节流） ----------

    private void ScheduleMarkdownRender(string text)
    {
        _mdPending = text;
        if (_mdTimer is { IsEnabled: true })
            return; // 已排定，下一次触发读取最新累积文本
        _mdTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(60) };
        _mdTimer.Tick += (_, _) =>
        {
            _mdTimer!.Stop();
            RenderInto(_mdTarget, _mdPending);
        };
        _mdTimer.Start();
    }

    private void FinalizeMarkdownRender(string text)
    {
        StopMarkdownTimer();
        RenderInto(_mdTarget, text);
    }

    private void StopMarkdownTimer()
    {
        if (_mdTimer != null)
        {
            _mdTimer.Stop();
            _mdTimer = null;
        }
    }

    private static void RenderInto(RichTextBox? target, string text)
    {
        if (target is null)
            return;
        target.Document = MarkdownRenderer.Build(text);
    }

    // ---------- 停止生成 ----------

    private async Task TriggerStopAsync()
    {
        if (_backend is null)
            return;
        _stopRequested = true;
        _stopMode = false;
        SendButton.IsEnabled = false;
        StatusText.Text = "正在停止...";
        try
        {
            // fire-and-forget：不读取响应，避免与流式读取竞争 stdout
            await _backend.SendFireAndForgetAsync(new { action = "stop" });
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 停止请求失败: {ex.Message}");
        }
    }

    // ---------- 发送指令（指令输入栏） ----------

    private async void CommandInputBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (HandlePaletteKey(e))
        {
            e.Handled = true;
            return;
        }
        if (e.Key == Key.Escape)
        {
            e.Handled = true;
            HideCommandInput();
            return;
        }
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.None)
        {
            e.Handled = true;
            string command = CommandInputBox.Text.Trim();
            if (command.Length > 0)
            {
                CommandInputBox.Text = "";
                await SendCommandAsync(command);
            }
        }
    }

    private void CommandInputBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        CommandPlaceholder.Visibility = CommandInputBox.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        UpdateCommandPalette(CommandInputBox);
    }

    private async Task SendCommandAsync(string command)
    {
        if (_isProcessing || _backend is null)
            return;
        SetProcessing(true);
        AddMessage("user", command);
        try
        {
            if (command.Equals("/help", StringComparison.OrdinalIgnoreCase))
                ShowHelpLocally();
            else if (command.Equals("/stop", StringComparison.OrdinalIgnoreCase))
                await TriggerStopAsync();
            else if (command.StartsWith("/snippet", StringComparison.OrdinalIgnoreCase))
                HandleSnippetCommand(command);
            else if (command.StartsWith("/darkmode", StringComparison.OrdinalIgnoreCase))
                HandleDarkMode(command);
            else
                HandleChatResponse(await _backend.SendRequestAsync(new { action = "chat", message = command }));
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 请求失败: {ex.Message}");
        }
        finally
        {
            SetProcessing(false);
            HideCommandInput(); // 指令执行完毕后收起指令输入行
        }
    }

    // ---------- 指令选择栏 ----------

    private void UpdateCommandPalette(TextBox source)
    {
        string text = source.Text;
        if (text.Length >= 1 && text.StartsWith('/'))
        {
            // 只输入 "/" 时显示全部指令；继续输入则按前缀过滤
            string query = text.Length > 1 ? text[1..] : "";
            var filtered = Commands
                .Where(c => query.Length == 0 || c.Command.StartsWith("/" + query, StringComparison.OrdinalIgnoreCase))
                .ToList();
            if (filtered.Count > 0)
            {
                _paletteSource = source;
                CommandPalette.PlacementTarget = source;
                CommandList.ItemsSource = filtered;
                CommandList.SelectedIndex = 0;
                CommandPalette.IsOpen = true;
                return;
            }
        }
        CloseCommandPalette();
    }

    private bool HandlePaletteKey(KeyEventArgs e)
    {
        if (!CommandPalette.IsOpen)
            return false;
        switch (e.Key)
        {
            case Key.Down:
                MoveCommandSelection(1);
                return true;
            case Key.Up:
                MoveCommandSelection(-1);
                return true;
            case Key.Tab:
                if (CommandList.SelectedItem is CommandItem tabItem)
                    ExecuteCommandItem(tabItem, completeOnly: true);
                return true;
            case Key.Escape:
                CloseCommandPalette();
                return true;
            case Key.Enter:
                if (CommandList.SelectedItem is CommandItem item)
                {
                    ExecuteCommandItem(item);
                    return true;
                }
                return false; // 无选中项 → 回落为正常发送
        }
        return false;
    }

    private void ExecuteCommandItem(CommandItem item, bool completeOnly = false)
    {
        CloseCommandPalette();
        var target = _paletteSource ?? CommandInputBox;

        // 需要参数或 Tab 补全 → 只填入"指令+空格"，让用户继续输入参数
        if (item.NeedsArgs || completeOnly)
        {
            target.Text = item.Command + " ";
            target.CaretIndex = target.Text.Length;
            target.Focus();
            return;
        }
        // 无需参数 → 直接执行
        if (target == InputBox)
        {
            InputBox.Text = item.Command;
            _ = SendMessageAsync();
        }
        else
        {
            CommandInputBox.Text = "";
            _ = SendCommandAsync(item.Command);
        }
    }

    private void MoveCommandSelection(int delta)
    {
        if (CommandList.Items.Count == 0)
            return;
        int index = (CommandList.SelectedIndex + delta + CommandList.Items.Count) % CommandList.Items.Count;
        CommandList.SelectedIndex = index;
        CommandList.ScrollIntoView(CommandList.SelectedItem);
    }

    private void CloseCommandPalette() => CommandPalette.IsOpen = false;

    private void CloseAllPalettes()
    {
        CommandPalette.IsOpen = false;
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
    }

    private void CommandList_PreviewMouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (CommandList.SelectedItem is not CommandItem item || e.OriginalSource is not DependencyObject source)
            return;
        // 仅当点击落在某个列表项上时才执行
        if (ItemsControl.ContainerFromElement(CommandList, source) is ListBoxItem container && container.IsSelected)
            ExecuteCommandItem(item);
    }

    /// <summary>聊天输入框出现 "/"+英文字母 时：只显示底部提示与按钮文字，不改动加减号图标。</summary>
    private void UpdateCommandUi(string text)
    {
        _commandLike = text.Length >= 1 && text.StartsWith('/') && (text.Length == 1 || IsAsciiLetter(text[1]));
        CommandBarHint.Visibility = _commandLike ? Visibility.Visible : Visibility.Collapsed;
        UpdateCommandButtonText();
    }

    /// <summary>命令按钮右侧文字：识别到指令→"转移到命令输入框"；否则按开合状态显示"打开/关闭命令输入框"。</summary>
    private void UpdateCommandButtonText()
    {
        CommandButtonText.Text = _commandLike ? "转移到命令输入框" : (_commandRowOpen ? "关闭命令输入框" : "打开命令输入框");
    }

    // ---------- 指令输入行（默认隐藏） ----------

    private void CommandButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        if (_commandRowOpen)
        {
            HideCommandInput();
            return;
        }

        // 把聊天输入框中的指令前缀（"/" 及后面的连续英文字母，允许只有单个 "/"）转移进指令输入框
        // 不加尾随空格（仅 Tab 补全时才补空格，避免用户删多余空格）
        string text = InputBox.Text;
        Match match = CommandPrefixRegex.Match(text);
        if (match.Success && match.Value.Length > 0)
        {
            string token = match.Value.TrimEnd();
            InputBox.Text = text[match.Length..];
            CommandInputBox.Text = token;
            InputBox.CaretIndex = InputBox.Text.Length;
        }
        ShowCommandInput();
    }

    private void ShowCommandInput()
    {
        _commandRowOpen = true;
        CommandInputRow.Visibility = Visibility.Visible;
        CommandIconRotate.Angle = 45; // 加号旋转 45° 变叉号
        UpdateCommandButtonText();
        CommandInputBox.Focus();
        CommandInputBox.CaretIndex = CommandInputBox.Text.Length;
    }

    private void HideCommandInput()
    {
        _commandRowOpen = false;
        CommandInputRow.Visibility = Visibility.Collapsed;
        CommandIconRotate.Angle = 0; // 恢复加号
        UpdateCommandButtonText();
        CloseAllPalettes();
        InputBox.Focus();
    }

    // ---------- 上下文占用（工具栏右侧） ----------

    /// <summary>从响应中读取附带的上文占用并刷新显示（/clear、/undo 后实时减少）。</summary>
    private void UpdateContextFromResponse(JsonElement root)
    {
        if (root.TryGetProperty("used", out JsonElement usedEl) && root.TryGetProperty("limit", out JsonElement limitEl))
            ContextText.Text = FormatContext(usedEl.GetInt32(), limitEl.GetInt32());
    }

    /// <summary>数字格式化：>=1000 显示 k，>=1000000 显示 m。</summary>
    private static string FormatTokens(int n) =>
        n >= 1_000_000 ? $"{n / 1_000_000.0:0.#}m"
        : n >= 1000 ? $"{n / 1000.0:0.#}k"
        : n.ToString();

    private static string FormatContext(int used, int limit) =>
        limit > 0 ? $"上下文：{FormatTokens(used)}/{FormatTokens(limit)}" : "上下文：---/---";

    /// <summary>向后端请求当前上下文占用并更新工具栏显示。</summary>
    private async Task UpdateContextAsync()
    {
        if (_backend is null)
            return;
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "context" });
            using JsonDocument doc = JsonDocument.Parse(json);
            UpdateContextFromResponse(doc.RootElement);
        }
        catch
        {
            // 后端忙碌/异常时忽略，下次交换再更新
        }
    }

    private async void ContextButton_Click(object sender, RoutedEventArgs e) => await UpdateContextAsync();

    private static bool IsAsciiLetter(char c) =>
        (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');

    // ---------- 常用语（常用语模板选择框） ----------

    private void SnippetButton_Click(object sender, RoutedEventArgs e)
    {
        CloseCommandPalette();
        if (SnippetPalette.IsOpen)
        {
            CloseSnippetPalette();
            return;
        }
        SnippetList.ItemsSource = _snippets.Items;
        if (_snippets.Items.Count == 0)
        {
            AddMessage("system", "📋 暂无常用语，可先打开命令输入框输入 /snippet add 名称 文本 新增");
            return;
        }
        // 显示在输入区域上方：命令输入行可见时锚定命令行，否则锚定输入区域容器
        SnippetPalette.PlacementTarget = _commandRowOpen ? (UIElement)CommandInputRow : (UIElement)InputAreaBorder;
        SnippetPalette.IsOpen = true;
        UpdateSnippetButtonText();
    }

    private void CloseSnippetPalette()
    {
        SnippetPalette.IsOpen = false;
        UpdateSnippetButtonText();
    }

    /// <summary>常用语按钮右侧文字：按选择框开合状态显示"打开/关闭常用语选择框"。</summary>
    private void UpdateSnippetButtonText()
    {
        SnippetButtonText.Text = SnippetPalette.IsOpen ? "关闭常用语选择框" : "打开常用语选择框";
    }

    private void SnippetList_PreviewMouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (SnippetList.SelectedItem is not Snippet snippet || e.OriginalSource is not DependencyObject source)
            return;
        if (ItemsControl.ContainerFromElement(SnippetList, source) is ListBoxItem container && container.IsSelected)
        {
            CloseSnippetPalette();
            int index = InputBox.CaretIndex;
            InputBox.Text = InputBox.Text.Insert(index, snippet.Text);
            InputBox.CaretIndex = index + snippet.Text.Length;
            InputBox.Focus();
        }
    }

    private void HandleSnippetCommand(string message)
    {
        var parts = message.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length < 2)
        {
            AddMessage("system", "用法: /snippet list | add 名称 文本 | remove 名称");
            return;
        }
        switch (parts[1].ToLowerInvariant())
        {
            case "list":
                if (_snippets.Items.Count == 0)
                {
                    AddMessage("system", "📋 暂无常用语，可用 /snippet add 名称 文本 新增");
                    return;
                }
                var sb = new StringBuilder("📋 常用语：\n");
                foreach (var s in _snippets.Items)
                    sb.AppendLine($"🔹 {s.Name}：{s.Text}");
                AddMessage("system", sb.ToString().TrimEnd());
                break;
            case "add":
                if (parts.Length < 4)
                {
                    AddMessage("system", "用法: /snippet add 名称 文本...");
                    return;
                }
                string name = parts[2];
                string text = string.Join(' ', parts.Skip(3));
                AddMessage("system", _snippets.Add(name, text)
                    ? $"✅ 已新增常用语「{name}」"
                    : $"❌ 常用语「{name}」已存在（可先 remove 再添加）");
                break;
            case "remove":
                if (parts.Length < 3)
                {
                    AddMessage("system", "用法: /snippet remove 名称");
                    return;
                }
                AddMessage("system", _snippets.Remove(parts[2])
                    ? $"🗑️ 已删除常用语「{parts[2]}」"
                    : $"❌ 未找到常用语「{parts[2]}」");
                break;
            default:
                AddMessage("system", "用法: /snippet list | add 名称 文本 | remove 名称");
                break;
        }
    }

    // ---------- 帮助 ----------

    private async Task ShowHelpAsync()
    {
        try
        {
            string json = await _backend!.SendRequestAsync(new { action = "chat", message = "/help" });
            HandleChatResponse(json);
        }
        catch
        {
            ShowHelpLocally(); // 后端不可用时回退本地渲染
        }
    }

    private void ShowHelpLocally()
    {
        var sb = new StringBuilder();
        sb.AppendLine("📖 EchoDesk v0.1.0 完整指令介绍 — Powered by Void Entropia Studio");
        sb.AppendLine();
        foreach (var c in Commands)
        {
            string usage = c.Format.Length > 0 ? $"{c.Command} {c.Format}" : c.Command;
            sb.AppendLine($"🔹 {usage}");
            sb.AppendLine($"   {c.Description}");
            sb.AppendLine();
        }
        AddMessage("system", sb.ToString().TrimEnd());
    }

    private void HandleChatResponse(string responseJson)
    {
        using JsonDocument doc = JsonDocument.Parse(responseJson);
        JsonElement root = doc.RootElement;
        string type = root.TryGetProperty("type", out JsonElement t) ? t.GetString() ?? "system" : "system";
        string content = root.TryGetProperty("content", out JsonElement c) ? c.GetString() ?? "无响应" : "无响应";
        string action = root.TryGetProperty("action", out JsonElement a) ? a.GetString() ?? "" : "";

        // /exit：先致谢并倒计时 10 秒，然后自动关闭
        if (action == "exit")
        {
            _backend?.Stop();
            BeginExitCountdown(ThanksMessage);
            return;
        }

        if (type == "assistant")
            AddMessage("assistant", content);
        else
        {
            AddMessage("system", content);
            if (content.StartsWith(ModePrefix, StringComparison.Ordinal))
                ModeText.Text = $"模式: {content[ModePrefix.Length..].Trim()}";
        }

        // /model 需要下载：征得用户同意后发送下载动作
        if (action == "confirm_model" && root.TryGetProperty("model", out JsonElement modelEl))
            _ = ConfirmModelDownloadAsync(modelEl.GetString() ?? "");

        // 响应附带的上下文占用：/clear、/undo 后实时减少
        UpdateContextFromResponse(root);
    }

    private async Task ConfirmModelDownloadAsync(string modelName)
    {
        var result = MessageBox.Show(
            $"确定要下载模型 '{modelName}' 吗？\n下载需要联网且可能耗时较长。",
            "确认下载模型",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (result != MessageBoxResult.Yes)
        {
            AddMessage("system", "已取消下载");
            return;
        }

        AddMessage("system", $"⏳ 正在下载并加载模型 '{modelName}'，请稍候...");
        StatusText.Text = "正在下载模型……";
        try
        {
            // 流式下载：完整进度进对话框，状态栏只显示简化状态
            string final = await _backend!.SendStreamRequestAsync(
                new { action = "model_download", name = modelName },
                delta =>
                {
                    UpdateLiveStatus(delta);
                    StatusText.Text = SimplifyStatus(delta);
                });
            AddMessage("system", final);
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 模型操作失败: {ex.Message}");
            StatusText.Text = "就绪";
        }
        StatusText.Text = "就绪";
    }

    // ---------- 退出倒计时 ----------

    private void BeginExitCountdown(string thanksMessage)
    {
        AddMessage("system", thanksMessage);
        StatusText.Text = "正在退出...";
        SetControlsEnabled(false);
        CloseAllPalettes();

        _exitCountdown = 10;
        StatusText.Text = $"程序将在 {_exitCountdown} 秒后自动关闭";
        _exitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _exitTimer.Tick += (_, _) =>
        {
            _exitCountdown--;
            if (_exitCountdown <= 0)
            {
                _exitTimer.Stop();
                Application.Current.Shutdown();
            }
            else
            {
                StatusText.Text = $"程序将在 {_exitCountdown} 秒后自动关闭";
            }
        };
        _exitTimer.Start();
    }

    // ---------- 文件上传 ----------

    private async void UploadButton_Click(object sender, RoutedEventArgs e)
    {
        CloseAllPalettes();
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = "选择要上传的文件",
            Filter = "支持的文件 (*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.txt;*.pdf)|*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.txt;*.pdf|所有文件 (*.*)|*.*",
        };
        if (dialog.ShowDialog() == true)
            await UploadFileAsync(dialog.FileName);
    }

    private async void MainWindow_Drop(object sender, DragEventArgs e)
    {
        if (_backend is null || !e.Data.GetDataPresent(DataFormats.FileDrop))
            return;
        string filePath = ((string[])e.Data.GetData(DataFormats.FileDrop))[0];
        await UploadFileAsync(filePath);
    }

    private async Task UploadFileAsync(string filePath)
    {
        if (_backend is null)
            return;
        AddMessage("system", $"📤 正在上传文件: {System.IO.Path.GetFileName(filePath)} ...");
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "file", path = filePath });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("content", out JsonElement content))
                AddMessage("system", content.GetString() ?? "上传完成");
        }
        catch (Exception ex)
        {
            AddMessage("system", $"❌ 上传失败: {ex.Message}");
        }
    }

    // ---------- 主题切换 ----------

    private void HandleDarkMode(string message)
    {
        string arg = message["/darkmode".Length..].Trim().ToLowerInvariant();
        bool dark = arg switch
        {
            "on" => true,
            "off" => false,
            _ => !_isDark, // 无参数时切换
        };
        ApplyTheme(dark);
        AddMessage("system", dark ? "🌙 已切换深色模式" : "☀️ 已切换浅色模式");
    }

    private void ApplyTheme(bool dark)
    {
        _isDark = dark;
        var dictionaries = Application.Current.Resources.MergedDictionaries;
        var theme = dictionaries.FirstOrDefault(
            d => d.Source?.OriginalString?.Contains("Theme.", StringComparison.Ordinal) == true);
        int index = theme is null ? 0 : dictionaries.IndexOf(theme);
        dictionaries[index] = new ResourceDictionary
        {
            Source = new Uri($"Resources/Theme.{(dark ? "Dark" : "Light")}.xaml", UriKind.Relative),
        };
    }

    // ---------- 字号调节（Ctrl+滚轮） ----------

    private void MessageScrollViewer_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (Keyboard.Modifiers == ModifierKeys.Control)
        {
            _fontSize = Math.Clamp(_fontSize + (e.Delta > 0 ? 0.5 : -0.5), 10, 24);
            UpdateAllMessageFontSize();
            StatusText.Text = $"字号 {_fontSize:0.#}";
            e.Handled = true;
        }
    }

    private void UpdateAllMessageFontSize()
    {
        foreach (var child in MessagePanel.Children)
        {
            if (child is not Panel panel)
                continue;
            foreach (var inner in panel.Children)
            {
                if (inner is not FrameworkElement fe || !Equals(fe.Tag, "content"))
                    continue;
                if (fe is TextBlock text)
                    text.FontSize = _fontSize;
                else if (fe is RichTextBox rich)
                    rich.FontSize = _fontSize; // FlowDocument 未显式设字号的 Run 会继承
            }
        }
    }

    // ---------- UI 辅助 ----------

    private void SetProcessing(bool processing)
    {
        _isProcessing = processing;
        SendButton.IsEnabled = !processing;
        StatusText.Text = processing ? "正在处理..." : "就绪";
    }

    /// <summary>创建消息面板：右上角时间戳 + 内容（Tag="content" 便于统一调字号）。</summary>
    private StackPanel NewMessagePanel(string role, string text, out TextBlock content)
    {
        (string icon, string brushKey) = role switch
        {
            "user" => ("👤 ", "UserBrush"),
            "assistant" => ("🤖 ", "AssistantBrush"),
            _ => ("ℹ️ ", "SystemBrush"),
        };
        var brush = (Brush)(TryFindResource(brushKey) ?? Brushes.Gray);

        var time = new TextBlock
        {
            Text = DateTime.Now.ToString("HH:mm:ss"),
            FontSize = 10,
            Foreground = (Brush)(TryFindResource("SubTextBrush") ?? Brushes.Gray),
            HorizontalAlignment = HorizontalAlignment.Right,
        };
        content = new TextBlock
        {
            Text = icon + text,
            TextWrapping = TextWrapping.Wrap,
            FontSize = _fontSize,
            Tag = "content",
            Foreground = brush,
        };
        var panel = new StackPanel { Margin = new Thickness(5) };
        panel.Children.Add(time);
        panel.Children.Add(content);
        return panel;
    }

    private void AddMessage(string role, string content)
    {
        var panel = NewMessagePanel(role, role == "assistant" ? "" : content, out TextBlock contentBlock);
        if (role == "assistant")
            ReplaceWithMarkdown(panel, contentBlock, content); // 助手消息渲染为 Markdown
        MessagePanel.Children.Add(panel);
        MessageScrollViewer.ScrollToBottom();
    }

    /// <summary>把消息面板中的流式文本块替换为渲染好的 Markdown（只读 RichTextBox）。</summary>
    private void ReplaceWithMarkdown(StackPanel panel, TextBlock contentBlock, string markdown)
    {
        panel.Children.Remove(contentBlock);
        var richText = new RichTextBox
        {
            IsReadOnly = true,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            FontSize = _fontSize,
            Tag = "content",
            IsDocumentEnabled = true,
        };
        richText.Document = MarkdownRenderer.Build(markdown);
        panel.Children.Insert(1, richText);
    }
}

/// <summary>指令选择栏中的一条指令：Command 指令名，Format 参数格式，Description 完整介绍（/help），Short 精简介绍（指令栏）。</summary>
public sealed record CommandItem(string Command, string Format, string Description, string Short, bool NeedsArgs);
