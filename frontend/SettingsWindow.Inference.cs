// [EN] EchoDesk WPF frontend module: frontend/SettingsWindow.Inference.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/SettingsWindow.Inference.cs。类型与成员说明见下方注释。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using EchoDesk.Services;

namespace EchoDesk;

/// <summary>
/// 设置窗口 ·「推理参数」（模型页里的一组采样参数控件）。
///
/// 需求背景：温度 / top_p / 重复惩罚 / 最大生成长度、生成预设、上下文策略与两个行为开关
/// 一直只能改 <c>config.json</c> —— 用户想让回答别那么发散，得先找到文件、改对键名、再重启。
/// 后端其实**早就有**读写通道（<c>capability</c> 读、<c>capability_settings</c> 写），
/// 只是前端一处都没调用，属于"接口齐全但界面入口缺失"。这个文件就是把入口补上。
///
/// 为什么控件用 C# 程序化生成、而不是写进 XAML（与「模型档位」同构，见 SettingsWindow.Profiles.cs）：
/// 4 条滑块 + 2 个下拉 + 2 个开关 + 3 个按钮 + 各自的说明，写进 XAML 要两百来行，
/// 而其中绝大部分是可复制的重复结构（四条滑块行的差别只有范围/步长/默认值/文案键）。
/// XAML 那边只留一个宿主 <c>InferencePanel</c>（一个 StackPanel），布局与文案全在这里拼。
///
/// 与后端交互的两个动作（实现都在 backend/main.py）：
/// <list type="bullet">
/// <item><c>{"action":"capability"}</c> → 响应里的 <c>settings</c> 对象就是当前值（main.py:682-701）；</item>
/// <item><c>{"action":"capability_settings", ...}</c> → 写回白名单字段，后端做范围校验并写盘（main.py:1754-1845）。</item>
/// </list>
/// </summary>
public partial class SettingsWindow
{
    // ---------- 采样参数的范围与默认值 ----------

    // 默认值必须与后端 config.py 的默认值以及 model_profile.PRESETS["balanced"] 对齐：
    // 两边对不上时，「恢复默认」填回去的值就不是后端认为的默认值，
    // 用户点一下"恢复默认"反而把参数改成了别的档。
    private const double DefaultTemperature = 0.7;
    private const double DefaultTopP = 0.9;
    private const double DefaultRepeatPenalty = 1.1;
    private const int DefaultMaxTokens = 1024;

    /// <summary>
    /// 一条滑块行（标签 + 滑块 + 数值 + 恢复默认 + 一行说明）。
    ///
    /// 之所以把这一串引用收进对象、再整体存进列表：切换界面语言时要**整批重排**这些文本，
    /// 而文本是创建时从语言包取的（不走 XAML 的 Loc.Key 附加属性，见 <see cref="RelocalizeInference"/>）。
    /// </summary>
    private sealed class InferenceSliderRow
    {
        public Slider Control { get; init; } = null!;
        public TextBlock Label { get; init; } = null!;
        public TextBlock Value { get; init; } = null!;
        public TextBlock Tip { get; init; } = null!;
        public Button Reset { get; init; } = null!;
        public string LabelKey { get; init; } = "";
        public string LabelFallback { get; init; } = "";
        public string TipKey { get; init; } = "";
        public string TipFallback { get; init; } = "";
        /// <summary>数值显示格式：采样参数两位小数、token 数取整。</summary>
        public string Format { get; init; } = "0.00";
        public double Default { get; init; }
    }

    // ---------- 控件引用（生成后由本文件的各处回调使用） ----------

    private readonly List<InferenceSliderRow> _inferenceSliders = new();
    private TextBlock? _inferenceTitle;
    private TextBlock? _inferencePresetLabel;
    private TextBlock? _inferenceContextLabel;
    private ComboBox? _inferencePresetCombo;
    private ComboBox? _inferenceContextCombo;
    private CheckBox? _inferenceClarifyCheck;
    private CheckBox? _inferenceSummaryCacheCheck;
    private TextBlock? _inferenceClarifyTip;
    private TextBlock? _inferenceSummaryCacheTip;
    private Button? _inferenceReloadButton;
    private Button? _inferenceSaveButton;
    private TextBlock? _inferenceStatus;

    /// <summary>
    /// 生成预设清单：Tag 是后端 <c>model_profile.PRESETS</c> 的键（语言无关），
    /// 显示文本走语言包 —— 后端的 <c>presets</c> 清单也带本地化标签，
    /// 但那要等一次请求回来才有；这三档是**固定的**（后端 PRESETS 就是这三个键），
    /// 写死在界面这边反而让下拉框在窗口画出来时就是完整的。
    /// </summary>
    private static readonly (string Tag, string Key, string Fallback)[] InferencePresetOptions =
    {
        ("light", "settings.inference.presetLight", "轻量"),
        ("balanced", "settings.inference.presetBalanced", "均衡"),
        ("quality", "settings.inference.presetQuality", "高质量"),
    };

    /// <summary>上下文策略清单：<c>summarize</c> = 分段摘要、<c>slide</c> = 滑动窗口丢弃（后端白名单就这两项）。</summary>
    private static readonly (string Tag, string Key, string Fallback)[] InferenceContextOptions =
    {
        ("summarize", "settings.inference.contextSummarize", "分段摘要"),
        ("slide", "settings.inference.contextSlide", "滑动窗口丢弃"),
    };

    /// <summary>控件是否已经生成（语言切换回调与异步刷新都要先确认它在）。</summary>
    private bool _inferenceBuilt;

    /// <summary>
    /// 程序化改值期间为真。
    ///
    /// 回填（读后端）与语言重排都会改两个下拉框的选中项，而"选中项变化"在正常路径下
    /// 是要**立刻发请求**的（预设 / 上下文策略改了就生效）。不按住这个标志，
    /// "读一次设置"会顺手把预设与上下文策略写回后端 —— 读操作变成写操作。
    /// </summary>
    private bool _inferenceSyncing;

    // ---------- 初始化 ----------

    /// <summary>
    /// 构造期生成整组控件（由 SettingsWindow 构造函数调用，位置紧挨 InitHubMarketUi()）。
    ///
    /// 必须赶在 <c>_uiReady = true</c> 之前调用：生成过程中会设置下拉框的默认选中项，
    /// 那一刻 _uiReady 还是 false，事件处理器的"未就绪就直接返回"分支会拦住它，
    /// 于是窗口还没画出来就不会先发一轮请求。
    /// </summary>
    private void InitInferenceUi()
    {
        if (InferencePanel is null)
            return;   // XAML 里没有宿主面板时不硬塞（旧布局文件）

        var border = new Border
        {
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            // 内边距与各处间距都刻意收紧：模型页的竖向空间要留给「模型市场」的结果列表
            // （那一行是 * 行，被挤到 0 高度整页就废了，见 SettingsWindow.Profiles.cs 的说明）。
            Padding = new Thickness(10, 6, 10, 6),
        };
        // 用 SetResourceReference（ThemeService.Bind）而不是 FindResource 取值：
        // 后者是**取值快照**，切主题后这个面板会留在旧配色里（MarkdownRenderer/TrustDialog 同样的理由）。
        ThemeService.Bind(border, Border.BackgroundProperty, "InputBackground");
        ThemeService.Bind(border, Border.BorderBrushProperty, "InputBorder");

        var root = new StackPanel();
        border.Child = root;
        InferencePanel.Children.Add(border);

        // ---- 第 1 行：标题 + 生成预设 + 上下文策略 ----
        // 用 WrapPanel 而不是 Grid：窄窗口下自动换行，不需要为每种语言分别调列宽。
        var head = new WrapPanel { Orientation = Orientation.Horizontal };
        _inferenceTitle = Identify(new TextBlock
        {
            Text = Loc.T("settings.inference.title", "🎛️ 推理参数（采样）"),
            FontSize = 12,
            FontWeight = FontWeights.Bold,
            VerticalAlignment = VerticalAlignment.Center,
        }, "InferenceTitle");
        ThemeService.Bind(_inferenceTitle, TextBlock.ForegroundProperty, "AccentBrush");
        head.Children.Add(_inferenceTitle);

        _inferencePresetLabel = MakeInferenceCaption("InferencePresetLabel",
            Loc.T("settings.inference.preset", "生成预设"));
        head.Children.Add(_inferencePresetLabel);
        _inferencePresetCombo = MakeInferenceCombo("InferencePresetCombo", 96);
        _inferencePresetCombo.SelectionChanged += InferencePresetCombo_SelectionChanged;
        head.Children.Add(_inferencePresetCombo);

        _inferenceContextLabel = MakeInferenceCaption("InferenceContextLabel",
            Loc.T("settings.inference.contextMode", "上下文策略"));
        head.Children.Add(_inferenceContextLabel);
        _inferenceContextCombo = MakeInferenceCombo("InferenceContextCombo", 150);
        _inferenceContextCombo.SelectionChanged += InferenceContextCombo_SelectionChanged;
        head.Children.Add(_inferenceContextCombo);
        // 下拉项的文本来自语言包，与 RelocalizeInference 用的是同一份清单（避免两处各写一遍而漂移）。
        // 此刻 _uiReady 还是 false，选中默认项引发的 SelectionChanged 会被处理器挡回去，不会先发请求；
        // 真正的当前值随后由 LoadInferenceSettingsAsync 读回并回填。
        RebuildInferenceCombo(_inferencePresetCombo, InferencePresetOptions);
        RebuildInferenceCombo(_inferenceContextCombo, InferenceContextOptions);
        SelectInferenceComboItem(_inferencePresetCombo, "balanced");
        root.Children.Add(head);

        // ---- 第 2 行：四条采样参数滑块（两列两行，省竖向空间） ----
        // 模型页的竖向空间本来就紧张（模型市场的结果列表是 * 行，被挤到 0 高度就废了，
        // 见 SettingsWindow.Profiles.cs 的说明），所以这里刻意排成两列。
        var grid = new Grid { Margin = new Thickness(0, 5, 0, 0) };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        AddInferenceSliderCell(grid, 0, 0, MakeInferenceSliderRow(
            "InferenceTemperature",
            "settings.inference.temp", "温度",
            "settings.inference.tempTip", "越高回答越发散，越低越保守",
            min: 0.0, max: 1.5, step: 0.05, initial: DefaultTemperature, format: "0.00",
            fallbackValue: DefaultTemperature));
        AddInferenceSliderCell(grid, 0, 1, MakeInferenceSliderRow(
            "InferenceTopP",
            "settings.inference.topP", "核采样 top_p",
            "settings.inference.topPTip", "候选词范围，调低更集中",
            min: 0.1, max: 1.0, step: 0.05, initial: DefaultTopP, format: "0.00",
            fallbackValue: DefaultTopP));
        AddInferenceSliderCell(grid, 1, 0, MakeInferenceSliderRow(
            "InferenceRepeatPenalty",
            "settings.inference.repeat", "重复惩罚",
            "settings.inference.repeatTip", "调高少复读，过高变生硬",
            min: 1.0, max: 1.5, step: 0.05, initial: DefaultRepeatPenalty, format: "0.00",
            fallbackValue: DefaultRepeatPenalty));
        AddInferenceSliderCell(grid, 1, 1, MakeInferenceSliderRow(
            "InferenceMaxTokens",
            "settings.inference.maxTokens", "最大生成",
            "settings.inference.maxTokensTip", "单次回复的 token 上限",
            min: 64, max: 4096, step: 64, initial: DefaultMaxTokens, format: "0",
            fallbackValue: DefaultMaxTokens));
        root.Children.Add(grid);

        // ---- 第 3 行：两个行为开关 + 读写按钮 ----
        var foot = new WrapPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 5, 0, 0) };

        _inferenceClarifyCheck = Identify(new CheckBox
        {
            Content = Loc.T("settings.inference.clarifyVague", "模糊提问先给方向/追问"),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            IsChecked = true,
        }, "InferenceClarifyCheck");
        foot.Children.Add(_inferenceClarifyCheck);
        _inferenceClarifyTip = MakeInferenceTip("InferenceClarifyTip",
            Loc.T("settings.inference.clarifyVagueTip", "问题太模糊时先问清楚再答"),
            new Thickness(6, 0, 14, 0));
        foot.Children.Add(_inferenceClarifyTip);

        _inferenceSummaryCacheCheck = Identify(new CheckBox
        {
            Content = Loc.T("settings.inference.summaryCache", "缓存分段摘要"),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            IsChecked = true,
        }, "InferenceSummaryCacheCheck");
        foot.Children.Add(_inferenceSummaryCacheCheck);
        _inferenceSummaryCacheTip = MakeInferenceTip("InferenceSummaryCacheTip",
            Loc.T("settings.inference.summaryCacheTip", "长文档摘要复用，省时间"),
            new Thickness(6, 0, 14, 0));
        foot.Children.Add(_inferenceSummaryCacheTip);

        _inferenceReloadButton = MakeInferenceButton("InferenceReloadButton",
            Loc.T("settings.inference.reload", "↻ 重新读取"), 86);
        _inferenceReloadButton.Click += InferenceReloadButton_Click;
        foot.Children.Add(_inferenceReloadButton);

        _inferenceSaveButton = MakeInferenceButton("InferenceSaveButton",
            Loc.T("settings.inference.save", "💾 保存"), 74);
        // 明确写出"保存"覆盖哪些东西：上面四条滑块改了就只是改界面，
        // 而两个开关（复选框）没有"改了就发"的即时通道，也在这一次保存里一起写回 ——
        // 不写清楚的话，用户勾了开关就以为已经生效了。
        _inferenceSaveButton.ToolTip = Loc.T("settings.inference.saveTip",
            "把四条采样参数、生成预设、上下文策略与两个开关一次性写入 config.json");
        _inferenceSaveButton.Click += InferenceSaveButton_Click;
        foot.Children.Add(_inferenceSaveButton);
        root.Children.Add(foot);

        // ---- 第 4 行：状态文本（独自一行、允许折行：错误信息往往比较长） ----
        _inferenceStatus = Identify(new TextBlock
        {
            FontSize = 11,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 4, 0, 0),
        }, "InferenceStatusText");
        ThemeService.Bind(_inferenceStatus, TextBlock.ForegroundProperty, "SubTextBrush");
        root.Children.Add(_inferenceStatus);

        _inferenceBuilt = true;
        // 语言切换后重排本组文本：这些控件是代码生成的，Loc.Apply() 的附加属性回填**管不到它们**
        // （它只认 XAML 里声明的 Loc.Key/TipKey），不重排的话本组控件会一直停在打开时的语言。
        Loc.LanguageChanged += (_, _) =>
        {
            if (Dispatcher.CheckAccess())
                RelocalizeInference();
            else
                Dispatcher.InvokeAsync(RelocalizeInference);
        };
        SetInferenceStatus(Loc.T("settings.inference.readHint",
            "这些参数保存后写入 config.json，重启程序依然生效。"));
    }

    // ---------- 小控件工厂 ----------

    // Identify<T>() 与 GetBoolOr() 在 SettingsWindow.xaml.cs 的辅助区（两个功能文件共用，
    // 各写一份会在同一个 partial 类里撞名）。

    private TextBlock MakeInferenceCaption(string id, string text)
    {
        var block = Identify(new TextBlock
        {
            Text = text,
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(14, 0, 0, 0),
        }, id);
        ThemeService.Bind(block, TextBlock.ForegroundProperty, "SubTextBrush");
        return block;
    }

    private TextBlock MakeInferenceTip(string id, string text, Thickness margin)
    {
        var block = Identify(new TextBlock
        {
            Text = text,
            FontSize = 10,
            Margin = margin,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
            // 悬停能看到全文：窄窗口或英文长句会被裁掉，裁掉的部分不该就此丢失
            ToolTip = text,
        }, id);
        ThemeService.Bind(block, TextBlock.ForegroundProperty, "SubTextBrush");
        return block;
    }

    private ComboBox MakeInferenceCombo(string id, double width) =>
        Identify(new ComboBox
        {
            Width = width,
            FontSize = 11,
            Margin = new Thickness(6, 0, 0, 0),
            VerticalAlignment = VerticalAlignment.Center,
        }, id);

    private Button MakeInferenceButton(string id, string text, double minWidth)
    {
        var button = Identify(new Button
        {
            Content = text,
            FontSize = 11,
            Padding = new Thickness(8, 2, 8, 2),
            MinWidth = minWidth,
            Margin = new Thickness(0, 0, 8, 0),
            VerticalAlignment = VerticalAlignment.Center,
        }, id);
        if (TryFindResource("RoundButton") is Style style)
            button.Style = style;
        return button;
    }

    private static void AddInferenceSliderCell(Grid grid, int row, int column, UIElement cell)
    {
        Grid.SetRow(cell, row);
        Grid.SetColumn(cell, column);
        grid.Children.Add(cell);
    }

    /// <summary>
    /// 造一条滑块行：标签 + 滑块 + 数值 + 恢复默认（第一行），说明文字（第二行）。
    ///
    /// 说明单独占一行而不是塞在标签旁边：中文句子在窄格子里会被裁掉大半，
    /// 而"越高越发散"这类提示正是这个面板存在的意义（用户看不懂参数名才需要它）。
    /// </summary>
    private StackPanel MakeInferenceSliderRow(
        string id, string labelKey, string labelFallback, string tipKey, string tipFallback,
        double min, double max, double step, double initial, string format, double fallbackValue)
    {
        var label = new TextBlock
        {
            Text = Loc.T(labelKey, labelFallback),
            FontSize = 11,
            Width = 64,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
        };
        ThemeService.Bind(label, TextBlock.ForegroundProperty, "SubTextBrush");

        var slider = Identify(new Slider
        {
            Minimum = min,
            Maximum = max,
            SmallChange = step,
            LargeChange = step * 4,
            TickFrequency = step,
            IsSnapToTickEnabled = true,
            Width = 116,
            Margin = new Thickness(6, 0, 6, 0),
            VerticalAlignment = VerticalAlignment.Center,
            // 初始值在这里就给好：下面的 ValueChanged 是**设完值之后**才挂的，
            // 免得处理器在绑定对象还没就位时被调用（v0.2.0 那次"设置窗口卡死"就是
            // XAML 解析期 ValueChanged 先于控件就绪触发导致的）。
            Value = initial,
        }, id + "Slider");

        var value = Identify(new TextBlock
        {
            Text = initial.ToString(format),
            FontSize = 11,
            Width = 34,
            TextAlignment = TextAlignment.Right,
            VerticalAlignment = VerticalAlignment.Center,
        }, id + "Value");
        ThemeService.Bind(value, TextBlock.ForegroundProperty, "SubTextBrush");

        var reset = MakeInferenceButton(id + "Reset", Loc.T("settings.inference.reset", "恢复默认"), 0);
        reset.FontSize = 10;
        reset.Padding = new Thickness(6, 1, 6, 1);
        reset.Margin = new Thickness(0);
        reset.ToolTip = Loc.T("settings.inference.resetTip",
            "把这个参数填回默认值（改完记得点「保存」）");

        var tip = MakeInferenceTip(id + "Tip", Loc.T(tipKey, tipFallback),
            new Thickness(64, 0, 0, 0));

        var row = new InferenceSliderRow
        {
            Control = slider,
            Label = label,
            Value = value,
            Tip = tip,
            Reset = reset,
            LabelKey = labelKey,
            LabelFallback = labelFallback,
            TipKey = tipKey,
            TipFallback = tipFallback,
            Format = format,
            Default = fallbackValue,
        };
        // 两个处理器（滑块与「恢复默认」）都只认 Tag 上的绑定对象，所以 Tag 必须先设好再挂处理器
        slider.Tag = row;
        reset.Tag = row;
        slider.ValueChanged += InferenceSlider_ValueChanged;
        reset.Click += InferenceResetButton_Click;

        var line = new StackPanel { Orientation = Orientation.Horizontal };
        line.Children.Add(label);
        line.Children.Add(slider);
        line.Children.Add(value);
        line.Children.Add(reset);

        var cell = new StackPanel { Orientation = Orientation.Vertical, Margin = new Thickness(0, 2, 10, 2) };
        cell.Children.Add(line);
        cell.Children.Add(tip);
        _inferenceSliders.Add(row);
        return cell;
    }

    // ---------- 事件 ----------

    private void InferenceSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        // 只刷新数字显示，不发请求：滑块每动一格都写盘既吵又没必要，
        // 真正写回由「保存」按钮统一完成（"改完再确认"比"边拖边写"更符合预期）。
        if (sender is Slider slider && slider.Tag is InferenceSliderRow row)
            row.Value.Text = slider.Value.ToString(row.Format);
    }

    private void InferenceResetButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: InferenceSliderRow row })
            return;
        row.Control.Value = row.Default;
        row.Value.Text = row.Control.Value.ToString(row.Format);
        SetInferenceStatus(Loc.T("settings.inference.resetApplied", "已恢复默认值（点「保存」后生效）"));
    }

    private async void InferencePresetCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || _inferenceSyncing)
            return;
        if ((_inferencePresetCombo?.SelectedItem as ComboBoxItem)?.Tag is not string preset)
            return;
        string label = _inferencePresetCombo?.SelectedItem is ComboBoxItem item
            ? item.Content?.ToString() ?? preset : preset;
        // 预设不是"另一个参数"，它在后端会**一次性改写温度/top_p/重复惩罚/最大生成长度**
        // （model_profile.PRESETS），所以发完必须回读一次：不回读的话下面四条滑块还是旧值，
        // 界面会自相矛盾（下拉框写着「高质量」，滑块还停在上一档的数字上）。
        await SendInferencePatchAsync(
            new { action = "capability_settings", preset },
            Loc.T("settings.inference.presetApplied", "✅ 已切换到「{0}」预设", label),
            reread: true);
    }

    private async void InferenceContextCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_uiReady || _inferenceSyncing)
            return;
        if ((_inferenceContextCombo?.SelectedItem as ComboBoxItem)?.Tag is not string mode)
            return;
        string label = _inferenceContextCombo?.SelectedItem is ComboBoxItem item
            ? item.Content?.ToString() ?? mode : mode;
        await SendInferencePatchAsync(
            new { action = "capability_settings", context_mode = mode },
            Loc.T("settings.inference.contextApplied", "✅ 上下文策略已改为「{0}」", label),
            reread: false);
    }

    private async void InferenceReloadButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await LoadInferenceSettingsAsync();
        }
        catch (Exception ex)
        {
            // 事件处理器是 async void：异常无人接住会顺着消息泵冒到进程级，
            // 这里兜住并记日志（与其它 async void 处理器同样的处理）。
            SetInferenceStatus(Loc.T("settings.inference.readFailed", "❌ 读取推理参数失败：{0}", ex.Message));
            App.Log("SettingsWindow.InferenceReload", ex);
        }
    }

    private async void InferenceSaveButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            if (_inferenceSliders.Count < 4)
                return;
            // 一次性把面板上的**全部**值发过去（而不是只发改动过的字段）：
            // 后端按"收到什么写什么"处理，全量发送让界面成为唯一事实来源，
            // 不会出现"某次改动没发出去、之后每次保存都把它漏掉"的隐性偏差。
            //
            // 也带上 preset：后端处理顺序是 preset → context_mode → 三个数值 → max_tokens
            // （main.py:1763-1804），所以数值字段会覆盖预设带来的值 ——
            // 用户"选了预设又手改了两项"时，手改的值优先，这正是期望语义。
            var patch = new
            {
                action = "capability_settings",
                preset = CurrentInferencePreset(),
                temperature = _inferenceSliders[0].Control.Value,
                top_p = _inferenceSliders[1].Control.Value,
                repeat_penalty = _inferenceSliders[2].Control.Value,
                max_tokens = (int)Math.Round(_inferenceSliders[3].Control.Value),
                context_mode = CurrentInferenceContextMode(),
                clarify_vague = _inferenceClarifyCheck?.IsChecked == true,
                summary_cache = _inferenceSummaryCacheCheck?.IsChecked == true,
            };
            await SendInferencePatchAsync(patch, Loc.T("settings.inference.saved", "✅ 已保存"),
                reread: true);
        }
        catch (Exception ex)
        {
            SetInferenceStatus(Loc.T("settings.inference.saveFailed", "❌ 保存失败：{0}", ex.Message));
            App.Log("SettingsWindow.InferenceSave", ex);
        }
    }

    // ---------- 读写 ----------

    /// <summary>
    /// 读回当前推理参数（窗口初始化 + 「重新读取」按钮）。
    ///
    /// <paramref name="quiet"/> 为真时不动状态文本：预设/保存后的回读只是为了让界面跟上
    /// 后端**裁剪过的真实值**，调用方随后会写自己的提示，这里再插一句会把提示顶掉。
    /// </summary>
    private async Task LoadInferenceSettingsAsync(bool quiet = false)
    {
        if (!_inferenceBuilt)
            return;
        if (!quiet)
            SetInferenceStatus(Loc.T("settings.inference.readingNow", "正在读取推理参数…"));

        string? json = await RequestWithRetryAsync(
            new { action = "capability" },
            Loc.T("settings.inference.reading", "读取推理参数"),
            TimeSpan.FromSeconds(60));
        if (json is null)
        {
            // 超时/后端忙的具体原因已经由 RequestWithRetryAsync 写到窗口底部的状态行，
            // 这里只在本组控件旁给一句"可以点重新读取"，不重复同一句报错。
            if (!quiet)
                SetInferenceStatus(Loc.T("settings.inference.readUnavailable",
                    "⚠️ 未能读取推理参数（后端未响应），可点「重新读取」重试。"));
            return;
        }
        try
        {
            using JsonDocument doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("settings", out JsonElement settings))
            {
                if (!quiet)
                    SetInferenceStatus(Loc.T("settings.inference.readFailed",
                        "❌ 读取推理参数失败：{0}", Loc.T("settings.inference.noData", "后端未返回参数")));
                return;
            }
            ApplyInferenceSettings(settings);
            if (!quiet)
                SetInferenceStatus(Loc.T("settings.inference.loaded",
                    "✅ 已读取当前参数（点「保存」后写入 config.json）"));
        }
        catch (JsonException ex)
        {
            SetInferenceStatus(Loc.T("settings.inference.readFailed", "❌ 读取推理参数失败：{0}", ex.Message));
        }
    }

    /// <summary>把后端返回的 <c>settings</c> 对象回填到控件上。</summary>
    private void ApplyInferenceSettings(JsonElement settings)
    {
        if (_inferenceSliders.Count < 4)
            return;
        bool previous = _inferenceSyncing;
        _inferenceSyncing = true;
        try
        {
            SetInferenceSliderValue(_inferenceSliders[0], GetDouble(settings, "temperature", DefaultTemperature));
            SetInferenceSliderValue(_inferenceSliders[1], GetDouble(settings, "top_p", DefaultTopP));
            SetInferenceSliderValue(_inferenceSliders[2], GetDouble(settings, "repeat_penalty", DefaultRepeatPenalty));
            SetInferenceSliderValue(_inferenceSliders[3], GetInt(settings, "max_tokens", DefaultMaxTokens));
            SelectInferenceComboItem(_inferencePresetCombo, GetString(settings, "preset"));
            SelectInferenceComboItem(_inferenceContextCombo, GetString(settings, "context_mode"));
            if (_inferenceClarifyCheck is not null)
                _inferenceClarifyCheck.IsChecked = GetBoolOr(settings, "clarify_vague", true);
            if (_inferenceSummaryCacheCheck is not null)
                _inferenceSummaryCacheCheck.IsChecked = GetBoolOr(settings, "summary_cache", true);
        }
        finally
        {
            _inferenceSyncing = previous;
        }
    }

    /// <summary>
    /// 填一个滑块值，必要时先把该滑块的边界撑开。
    ///
    /// 为什么需要撑开：config.json 是人手写的，max_tokens 常见的值是 2048 / 4096 / 8192，
    /// 而界面范围是 64–4096。若把 8192 硬裁到 4096，用户只是打开设置窗口、什么都没改
    /// 就点了一下「保存」，配置里的 8192 就被悄悄改成了 4096 —— "读一下就写坏"。
    /// 撑开边界后滑块能如实表示配置现状（超出常规范围说明是用户自己特意写的）。
    /// </summary>
    private void SetInferenceSliderValue(InferenceSliderRow row, double value)
    {
        if (double.IsNaN(value) || double.IsInfinity(value))
            return;
        if (value > row.Control.Maximum)
            row.Control.Maximum = value;
        else if (value < row.Control.Minimum)
            row.Control.Minimum = value;
        row.Control.Value = value;
        row.Value.Text = row.Control.Value.ToString(row.Format);
    }

    /// <summary>写回一小块设置（预设 / 上下文策略 / 整组参数）。</summary>
    /// <param name="patch">请求体（后端只认白名单字段）。</param>
    /// <param name="okText">成功提示（后端没给文案时用它）。</param>
    /// <param name="reread">成功后是否回读一次（预设会连带改数值、后端也会裁剪越界值）。</param>
    private async Task SendInferencePatchAsync(object patch, string okText, bool reread)
    {
        if (_inferenceStatus is null)
            return;
        SetInferenceStatus(Loc.T("settings.inference.saving", "正在保存…"));
        try
        {
            string json = await _backend.SendRequestAsync(patch);
            using JsonDocument doc = JsonDocument.Parse(json);
            string content = GetString(doc.RootElement, "content");
            if (reread)
                await LoadInferenceSettingsAsync(quiet: true);
            // 后端的 content 会列出**实际改动了哪些字段**（"已保存能力设置：temperature, max_tokens"），
            // 这句话比界面上那句固定提示更有信息量，所以放进悬停提示，正文用界面语言里那句。
            SetInferenceStatus(okText, content);
        }
        catch (Exception ex)
        {
            SetInferenceStatus(Loc.T("settings.inference.saveFailed", "❌ 保存失败：{0}", ex.Message));
        }
    }

    private string CurrentInferencePreset() =>
        (_inferencePresetCombo?.SelectedItem as ComboBoxItem)?.Tag as string ?? "";

    private string CurrentInferenceContextMode() =>
        (_inferenceContextCombo?.SelectedItem as ComboBoxItem)?.Tag as string ?? "";

    // ---------- 文本与状态 ----------

    private void SetInferenceStatus(string text, string? detail = null)
    {
        if (_inferenceStatus is null)
            return;
        _inferenceStatus.Text = text;
        _inferenceStatus.ToolTip = string.IsNullOrWhiteSpace(detail) ? null : detail;
    }

    private static void SelectInferenceComboItem(ComboBox? combo, string tag)
    {
        if (combo is null || tag.Length == 0)
            return;
        for (int i = 0; i < combo.Items.Count; i++)
        {
            if (combo.Items[i] is ComboBoxItem item && Equals(item.Tag, tag))
            {
                combo.SelectedIndex = i;
                return;
            }
        }
    }

    /// <summary>
    /// 按新语言重排本组控件（供 <see cref="Loc.LanguageChanged"/> 调用）。
    ///
    /// 只改文本、不重新读后端：语言切换是纯界面行为，为它多发一轮请求既慢又可能
    /// 把旧语言的提示文案拉回来（与模型市场的 RelocalizeHubMarket 同样的取舍）。
    /// 状态行里的动态文本不在这里重排 —— 它是"上一次操作的结果"，下一次操作自然会刷新。
    /// </summary>
    private void RelocalizeInference()
    {
        if (!_inferenceBuilt)
            return;
        bool previous = _inferenceSyncing;
        _inferenceSyncing = true;   // 重建下拉项会动选中项，别让它顺手发请求
        try
        {
            if (_inferenceTitle is not null)
                _inferenceTitle.Text = Loc.T("settings.inference.title", "🎛️ 推理参数（采样）");
            if (_inferencePresetLabel is not null)
                _inferencePresetLabel.Text = Loc.T("settings.inference.preset", "生成预设");
            if (_inferenceContextLabel is not null)
                _inferenceContextLabel.Text = Loc.T("settings.inference.contextMode", "上下文策略");
            RebuildInferenceCombo(_inferencePresetCombo, InferencePresetOptions);
            RebuildInferenceCombo(_inferenceContextCombo, InferenceContextOptions);
            foreach (InferenceSliderRow row in _inferenceSliders)
            {
                row.Label.Text = Loc.T(row.LabelKey, row.LabelFallback);
                row.Tip.Text = Loc.T(row.TipKey, row.TipFallback);
                row.Tip.ToolTip = row.Tip.Text;
                row.Reset.Content = Loc.T("settings.inference.reset", "恢复默认");
                row.Reset.ToolTip = Loc.T("settings.inference.resetTip",
                    "把这个参数填回默认值（改完记得点「保存」）");
            }
            if (_inferenceClarifyCheck is not null)
                _inferenceClarifyCheck.Content = Loc.T("settings.inference.clarifyVague", "模糊提问先给方向/追问");
            if (_inferenceClarifyTip is not null)
                _inferenceClarifyTip.Text = Loc.T("settings.inference.clarifyVagueTip",
                    "问题太模糊时先问清楚再答");
            if (_inferenceSummaryCacheCheck is not null)
                _inferenceSummaryCacheCheck.Content = Loc.T("settings.inference.summaryCache", "缓存分段摘要");
            if (_inferenceSummaryCacheTip is not null)
                _inferenceSummaryCacheTip.Text = Loc.T("settings.inference.summaryCacheTip",
                    "长文档摘要复用，省时间");
            if (_inferenceReloadButton is not null)
                _inferenceReloadButton.Content = Loc.T("settings.inference.reload", "↻ 重新读取");
            if (_inferenceSaveButton is not null)
            {
                _inferenceSaveButton.Content = Loc.T("settings.inference.save", "💾 保存");
                _inferenceSaveButton.ToolTip = Loc.T("settings.inference.saveTip",
                    "把四条采样参数、生成预设、上下文策略与两个开关一次性写入 config.json");
            }
        }
        finally
        {
            _inferenceSyncing = previous;
        }
    }

    /// <summary>按新语言重建下拉项（保持当前选中项：Tag 是语言无关的键）。</summary>
    private static void RebuildInferenceCombo(ComboBox? combo, (string Tag, string Key, string Fallback)[] options)
    {
        if (combo is null)
            return;
        string? selected = (combo.SelectedItem as ComboBoxItem)?.Tag as string;
        combo.Items.Clear();
        foreach ((string tag, string key, string fallback) in options)
            combo.Items.Add(new ComboBoxItem { Content = Loc.T(key, fallback), Tag = tag });
        if (selected is not null)
        {
            for (int i = 0; i < combo.Items.Count; i++)
            {
                if (combo.Items[i] is ComboBoxItem item && Equals(item.Tag, selected))
                {
                    combo.SelectedIndex = i;
                    return;
                }
            }
        }
        if (combo.SelectedIndex < 0 && combo.Items.Count > 0)
            combo.SelectedIndex = 0;   // 没选中项时给个默认值，避免下拉框显示空白
    }

    // ---------- JSON 读取辅助（补 SettingsWindow.xaml.cs 里缺的那个小数读取） ----------

    /// <summary>
    /// 读一个小数。
    ///
    /// 不能直接 <c>GetDouble()</c>：字段缺失、被写成字符串或写成 null 都会抛异常，
    /// 而这些值来自用户手写的 config.json（与 GetInt 不能直接 GetInt32 是同一个理由）。
    /// </summary>
    private static double GetDouble(JsonElement el, string key, double fallback)
    {
        if (!el.TryGetProperty(key, out JsonElement v))
            return fallback;
        if (v.ValueKind == JsonValueKind.Number && v.TryGetDouble(out double number)
            && !double.IsNaN(number) && !double.IsInfinity(number))
            return number;
        if (v.ValueKind == JsonValueKind.String
            && double.TryParse(v.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out double parsed)
            && !double.IsNaN(parsed) && !double.IsInfinity(parsed))
            return parsed;
        return fallback;
    }
}
