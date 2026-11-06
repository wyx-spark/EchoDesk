// [EN] EchoDesk WPF frontend module: frontend/Utilities/TrustDialog.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Utilities/TrustDialog.cs。类型与成员说明见下方注释。
using System.Windows;
using System.Windows.Controls;
using EchoDesk.Services;

namespace EchoDesk.Utilities;

/// <summary>批量确认框里的一行：一个待确认的本地插件。</summary>
public sealed class TrustItem
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string Path { get; set; } = "";
    public string Source { get; set; } = "";
    public IReadOnlyList<string> Capabilities { get; set; } = new List<string>();
    public IReadOnlyDictionary<string, object?> Permissions { get; set; } =
        new Dictionary<string, object?>();

    /// <summary>勾选 = 启用（信任并加载）。</summary>
    public bool Enabled { get; set; } = true;

    /// <summary>界面上的复选框（构造时创建，便于"全部启用/全部禁用"批量改）。</summary>
    public CheckBox? Box { get; set; }
}

/// <summary>
/// 本地插件批量信任确认框（任务书修复一）。
///
/// 修复前：<c>CheckTrustPending</c> 每发现一个未确认的插件就弹一次 <c>MessageBox</c>，
/// 而每次答复又会触发 <c>LoadSkillsAsync</c> → 再弹下一个，于是 N 个插件就是 N 次弹窗，
/// 数量多时形成"弹窗轰炸"。
///
/// 现在改为一次性列出全部待确认插件，用户可以逐个勾选或一键全部启用/禁用，
/// 一次点确定即写盘（后端 <c>skills_trust_bulk</c>），此后再也不问。
/// </summary>
public sealed class TrustDialog : Window
{
    private readonly List<TrustItem> _items;

    /// <summary>用户点"确定"后每项的最终启用状态；取消时为空。</summary>
    public IReadOnlyDictionary<string, bool> Decisions { get; private set; } =
        new Dictionary<string, bool>();

    public TrustDialog(IEnumerable<TrustItem> items, Window? owner)
    {
        _items = items.ToList();
        Title = Loc.T("settings.installed.trustTitle");
        Width = 620;
        SizeToContent = SizeToContent.Height;
        MaxHeight = 620;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        if (owner is not null)
            Owner = owner;
        SetResourceReference(BackgroundProperty, "WindowBackground");
        SetResourceReference(FontFamilyProperty, "UiFontFamily");

        var root = new Grid { Margin = new Thickness(16) };
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var intro = new TextBlock
        {
            Text = Loc.T("settings.installed.trustBulkIntro",
                "发现 {0} 个新的本地插件。它们将以你的账户权限运行，只勾选你信任的插件。\n" +
                "你的选择会被记住，之后不再询问。",
                _items.Count),
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 10),
        };
        intro.SetResourceReference(TextBlock.ForegroundProperty, "TextForeground");
        Grid.SetRow(intro, 0);
        root.Children.Add(intro);

        var list = new StackPanel();
        foreach (TrustItem item in _items)
            list.Children.Add(BuildRow(item));
        Grid.SetRow(list, 1);
        root.Children.Add(new ScrollViewer
        {
            Content = list,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            MaxHeight = 400,
        });

        var buttons = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(0, 14, 0, 0),
        };
        buttons.Children.Add(MakeButton(Loc.T("settings.installed.trustEnableAll", "全部启用"),
                                        () => SetAll(true)));
        buttons.Children.Add(MakeButton(Loc.T("settings.installed.trustDisableAll", "全部禁用"),
                                        () => SetAll(false)));
        var ok = MakeButton(Loc.T("settings.button.save", "确定"), () =>
        {
            Decisions = _items.ToDictionary(i => i.Id, i => i.Box?.IsChecked == true);
            DialogResult = true;
        });
        ok.SetResourceReference(BackgroundProperty, "AccentBrush");
        ok.SetResourceReference(ForegroundProperty, "AccentForegroundBrush");
        buttons.Children.Add(ok);
        buttons.Children.Add(MakeButton(Loc.T("settings.button.cancel", "取消"), () =>
        {
            Decisions = new Dictionary<string, bool>();
            DialogResult = false;
        }));
        Grid.SetRow(buttons, 2);
        root.Children.Add(buttons);

        Content = root;
        // 确认框是模态的：必须在窗口显示后再抢焦点，避免 UIA 探针/自动截图时抓到空窗
        Loaded += (_, _) => ok.Focus();
    }

    private UIElement BuildRow(TrustItem item)
    {
        var card = new Border
        {
            Margin = new Thickness(0, 0, 0, 8),
            Padding = new Thickness(10, 8, 10, 8),
            CornerRadius = new CornerRadius(6),
            BorderThickness = new Thickness(1),
        };
        card.SetResourceReference(BackgroundProperty, "SurfaceBrush");
        card.SetResourceReference(BorderBrushProperty, "SeparatorBrush");

        var panel = new StackPanel();
        var box = new CheckBox
        {
            IsChecked = item.Enabled,
            Content = string.IsNullOrEmpty(item.Name) ? item.Id : $"{item.Name} ({item.Id})",
            FontWeight = FontWeights.SemiBold,
        };
        box.SetResourceReference(ForegroundProperty, "TextForeground");
        item.Box = box;
        panel.Children.Add(box);

        panel.Children.Add(Meta(Loc.T("settings.installed.sourceLabel", "来源") + ": " +
                                (item.Source == "dev"
                                    ? Loc.T("settings.installed.sourceDev")
                                    : Loc.T("settings.installed.sourceLocal"))));
        panel.Children.Add(Meta(item.Path));
        if (item.Capabilities.Count > 0)
        {
            panel.Children.Add(Meta(Loc.T("settings.installed.capabilitiesLabel", "能力") + ": " +
                                    string.Join(", ", item.Capabilities)));
        }
        if (item.Permissions.Count > 0)
        {
            var parts = item.Permissions.Select(p => $"{p.Key}={p.Value}");
            panel.Children.Add(Meta(Loc.T("settings.installed.permissionsLabel") +
                                    string.Join("; ", parts)));
        }
        var warn = Meta(Loc.T("settings.installed.trustWarn",
            "⚠️ 插件代码会在本机以你的权限运行，无法限制它能访问什么。"));
        warn.SetResourceReference(TextBlock.ForegroundProperty, "WarningBrush");
        panel.Children.Add(warn);

        card.Child = panel;
        return card;
    }

    private static TextBlock Meta(string text)
    {
        var block = new TextBlock
        {
            Text = text,
            FontSize = 11,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(21, 2, 0, 0),
        };
        block.SetResourceReference(TextBlock.ForegroundProperty, "SubTextBrush");
        return block;
    }

    private static Button MakeButton(string caption, System.Action onClick)
    {
        var button = new Button
        {
            Content = caption,
            Padding = new Thickness(12, 4, 12, 4),
            Margin = new Thickness(6, 0, 0, 0),
            MinWidth = 84,
            Style = Application.Current.TryFindResource("RoundButton") as Style,
        };
        button.Click += (_, _) => onClick();
        return button;
    }

    private void SetAll(bool enabled)
    {
        foreach (TrustItem item in _items)
        {
            if (item.Box is not null)
                item.Box.IsChecked = enabled;
        }
    }
}
