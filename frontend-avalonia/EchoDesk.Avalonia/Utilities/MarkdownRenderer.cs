// Markdown → Avalonia 控件的渲染器（对应 WPF 版的 Utilities/MarkdownRenderer.cs）。
//
// **这是本次原型最需要证明的一件事**：WPF 版的富文本是"Markdown → FlowDocument + Inline"，
// 而 Avalonia 没有 FlowDocument，只有 TextBlock.Inlines（限于单段的行内排版）。
// 于是跨平台版换了一种结构：
//   * **块级**（段落/标题/列表/引用/表格/代码块/公式块）= 控件树（StackPanel + Border + Grid）；
//   * **行内**（粗体/斜体/删除线/行内代码/链接）= TextBlock.Inlines 里的 Run/Span。
// 这套结构在原型里被验证可行（任务一的验证要求），但它与 WPF 版的**能力差异是真的**，
// 已逐条记进 records（例如 FlowDocument 的自动分页/整段选择被换成"每段可选中"）。
//
// 刻意不引 Markdig：WPF 版是零第三方依赖的手写解析（26 KB），跨平台版沿用同一取舍
// ——解析规则按"够用且可测"实现，覆盖代码块/表格/标题/列表/引用/公式/行内标记。
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Primitives;
using Avalonia.Input.Platform;
using Avalonia.Layout;
using Avalonia.Media;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Utilities;

public static class MarkdownRenderer
{
    /// <summary>等宽字体链：三个平台各取一个自带等宽字体，最后留 monospace 兜底。</summary>
    public static readonly FontFamily MonoFont =
        new("Consolas, Menlo, DejaVu Sans Mono, Liberation Mono, Courier New, monospace");

    /// <summary>把 Markdown 文本渲染成一棵可放进消息气泡的控件树。</summary>
    public static Control Build(string markdown, double fontSize)
    {
        var panel = new StackPanel { Spacing = 6 };
        if (string.IsNullOrEmpty(markdown))
            return panel;

        string[] lines = markdown.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
        int i = 0;
        while (i < lines.Length)
        {
            string line = lines[i];
            string trimmed = line.TrimStart();

            // 代码块 ```lang ... ```
            if (trimmed.StartsWith("```", StringComparison.Ordinal))
            {
                string lang = trimmed[3..].Trim();
                i++;
                var body = new StringBuilder();
                while (i < lines.Length && !lines[i].TrimStart().StartsWith("```", StringComparison.Ordinal))
                {
                    body.AppendLine(lines[i]);
                    i++;
                }
                i++;   // 跳过收尾的 ```
                panel.Children.Add(BuildCodeBlock(body.ToString().TrimEnd('\n'), lang, fontSize));
                continue;
            }

            // 公式块 $$ ... $$
            if (trimmed.StartsWith("$$", StringComparison.Ordinal))
            {
                string first = trimmed.Length > 2 ? trimmed[2..].Trim() : "";
                var body = new StringBuilder();
                if (first.Length > 0)
                    body.AppendLine(first);
                i++;
                while (i < lines.Length && !lines[i].TrimStart().StartsWith("$$", StringComparison.Ordinal))
                {
                    body.AppendLine(lines[i]);
                    i++;
                }
                i++;
                panel.Children.Add(BuildFormulaBlock(body.ToString().TrimEnd('\n'), fontSize));
                continue;
            }

            // 表格：当前行是 | a | b | 且下一行是 |---|
            if (trimmed.StartsWith('|') && i + 1 < lines.Length && IsTableSeparator(lines[i + 1]))
            {
                var rows = new List<string>();
                while (i < lines.Length && lines[i].TrimStart().StartsWith('|'))
                {
                    rows.Add(lines[i].Trim());
                    i++;
                }
                panel.Children.Add(BuildTable(rows, fontSize));
                continue;
            }

            // 标题 # .. ######
            if (trimmed.StartsWith('#'))
            {
                int level = 0;
                while (level < trimmed.Length && trimmed[level] == '#')
                    level++;
                if (level is >= 1 and <= 6 && level < trimmed.Length && trimmed[level] == ' ')
                {
                    panel.Children.Add(BuildHeading(trimmed[(level + 1)..].Trim(), level, fontSize));
                    i++;
                    continue;
                }
            }

            // 分隔线
            string bare = trimmed.TrimEnd();
            if (bare is "---" or "***" or "___" && bare.Length >= 3)
            {
                panel.Children.Add(new Border
                {
                    Height = 1,
                    Margin = new Thickness(0, 6),
                    Background = Brush("MarkdownRuleBrush", Color.FromArgb(60, 128, 128, 128)),
                });
                i++;
                continue;
            }

            // 列表项（有序/无序/任务清单）；连续的列表项合并成一个列表块
            if (IsListItem(trimmed))
            {
                var items = new List<string>();
                while (i < lines.Length && IsListItem(lines[i].TrimStart()))
                {
                    items.Add(lines[i].TrimStart());
                    i++;
                }
                panel.Children.Add(BuildList(items, fontSize));
                continue;
            }

            // 引用
            if (trimmed.StartsWith('>'))
            {
                var quoted = new StringBuilder();
                while (i < lines.Length && lines[i].TrimStart().StartsWith('>'))
                {
                    quoted.AppendLine(lines[i].TrimStart()[1..].TrimStart());
                    i++;
                }
                panel.Children.Add(BuildQuote(quoted.ToString().TrimEnd(), fontSize));
                continue;
            }

            // 普通段落：连续的文本行合成一段（软换行按同一段处理）
            if (trimmed.Length == 0)
            {
                i++;
                continue;
            }
            var para = new List<string>();
            while (i < lines.Length)
            {
                string t = lines[i].TrimStart();
                if (t.Length == 0 || t.StartsWith("```", StringComparison.Ordinal) || t.StartsWith("$$", StringComparison.Ordinal)
                    || t.StartsWith('|') || t.StartsWith('#') || t.StartsWith('>') || IsListItem(t))
                    break;
                para.Add(lines[i]);
                i++;
            }
            panel.Children.Add(BuildParagraph(string.Join("\n", para), fontSize));
        }
        return panel;
    }

    // -----------------------------------------------------------------
    // 块级
    // -----------------------------------------------------------------

    private static Control BuildParagraph(string text, double fontSize)
    {
        var tb = NewTextBlock(fontSize);
        ParseInlines(tb, text, fontSize, bold: false, italic: false);
        return tb;
    }

    private static Control BuildHeading(string text, int level, double fontSize)
    {
        double size = level switch
        {
            1 => fontSize + 8,
            2 => fontSize + 5,
            3 => fontSize + 3,
            4 => fontSize + 1,
            _ => fontSize,
        };
        var tb = NewTextBlock(size);
        tb.FontWeight = FontWeight.SemiBold;
        tb.Margin = new Thickness(0, level <= 2 ? 8 : 4, 0, 2);
        ParseInlines(tb, text, size, bold: true, italic: false);
        return tb;
    }

    private static Control BuildQuote(string text, double fontSize)
    {
        var inner = NewTextBlock(fontSize);
        inner.Opacity = 0.85;
        ParseInlines(inner, text, fontSize, bold: false, italic: true);
        return new Border
        {
            BorderThickness = new Thickness(3, 0, 0, 0),
            BorderBrush = Brush("MarkdownQuoteBrush", Color.FromArgb(120, 128, 128, 128)),
            Padding = new Thickness(10, 4, 4, 4),
            Child = inner,
        };
    }

    private static Control BuildList(List<string> items, double fontSize)
    {
        var panel = new StackPanel { Spacing = 3 };
        foreach (string raw in items)
        {
            string text = raw;
            string marker = "•";
            if (text.StartsWith("- ", StringComparison.Ordinal) || text.StartsWith("* ", StringComparison.Ordinal)
                || text.StartsWith("+ ", StringComparison.Ordinal))
            {
                text = text[2..];
            }
            else if (text.StartsWith("[ ] ", StringComparison.Ordinal))
            {
                marker = "☐";
                text = text[4..];
            }
            else if (text.StartsWith("[x] ", StringComparison.OrdinalIgnoreCase))
            {
                marker = "☑";
                text = text[4..];
            }
            else
            {
                int dot = text.IndexOf(". ", StringComparison.Ordinal);
                if (dot > 0 && dot <= 4 && text[..dot].All(char.IsDigit))
                {
                    marker = text[..(dot + 1)];
                    text = text[(dot + 2)..];
                }
            }

            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*") };
            var bullet = new TextBlock
            {
                Text = marker,
                FontSize = fontSize,
                Margin = new Thickness(0, 0, 8, 0),
                VerticalAlignment = VerticalAlignment.Top,
            };
            var body = NewTextBlock(fontSize);
            ParseInlines(body, text, fontSize, bold: false, italic: false);
            Grid.SetColumn(bullet, 0);
            Grid.SetColumn(body, 1);
            row.Children.Add(bullet);
            row.Children.Add(body);
            panel.Children.Add(row);
        }
        return panel;
    }

    private static Control BuildCodeBlock(string code, string lang, double fontSize)
    {
        var header = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
            Margin = new Thickness(0, 0, 0, 4),
        };
        header.Children.Add(new TextBlock
        {
            Text = lang.Length > 0 ? lang : "code",
            FontSize = fontSize - 2,
            Opacity = 0.6,
            VerticalAlignment = VerticalAlignment.Center,
        });

        var copy = new Button
        {
            Content = Loc.T("main.message.copyCode", "代码"),
            FontSize = fontSize - 3,
            Padding = new Thickness(6, 1, 6, 1),
        };
        copy.Click += async (_, _) =>
        {
            if (TopLevel.GetTopLevel(copy)?.Clipboard is { } clipboard)
            {
                try
                {
                    await clipboard.SetTextAsync(code);
                    copy.Content = Loc.T("main.message.copiedCode", "已复制代码块");
                }
                catch (Exception)
                {
                    // 剪贴板被占用：忽略（WPF 版会弹提示，原型阶段先静默）
                }
            }
        };
        header.Children.Add(copy);

        var body = new SelectableTextBlock
        {
            Text = code,
            FontFamily = MonoFont,
            FontSize = fontSize - 1,
            TextWrapping = TextWrapping.NoWrap,
        };
        var stack = new StackPanel();
        stack.Children.Add(header);
        stack.Children.Add(body);
        return new Border
        {
            Background = Brush("MarkdownCodeBrush", Color.FromArgb(28, 128, 128, 128)),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(10),
            Child = new ScrollViewer
            {
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
                Content = stack,
            },
        };
    }

    private static Control BuildFormulaBlock(string body, double fontSize)
    {
        var tb = new SelectableTextBlock
        {
            Text = body,
            FontFamily = MonoFont,
            FontSize = fontSize + 1,
            TextWrapping = TextWrapping.Wrap,
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        return new Border
        {
            Background = Brush("MarkdownFormulaBrush", Color.FromArgb(24, 100, 140, 200)),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(10, 8, 10, 8),
            Margin = new Thickness(0, 4),
            Child = tb,
        };
    }

    /// <summary>表格：Avalonia 没有 FlowDocument 的 Table，用 Grid + Border 画。</summary>
    private static Control BuildTable(List<string> rows, double fontSize)
    {
        var parsed = rows.Where(r => !IsTableSeparator(r))
                         .Select(SplitCells)
                         .ToList();
        if (parsed.Count == 0)
            return new TextBlock { Text = "" };

        int columns = parsed.Max(r => r.Count);
        var grid = new Grid();
        for (int c = 0; c < columns; c++)
            grid.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));
        for (int r = 0; r < parsed.Count; r++)
            grid.RowDefinitions.Add(new RowDefinition(GridLength.Auto));

        IBrush border = Brush("MarkdownRuleBrush", Color.FromArgb(60, 128, 128, 128));
        for (int r = 0; r < parsed.Count; r++)
        {
            for (int c = 0; c < columns; c++)
            {
                string cell = c < parsed[r].Count ? parsed[r][c] : "";
                var tb = NewTextBlock(fontSize);
                tb.Margin = new Thickness(6, 4, 6, 4);
                if (r == 0)
                    tb.FontWeight = FontWeight.SemiBold;
                ParseInlines(tb, cell, fontSize, bold: r == 0, italic: false);
                var cellBorder = new Border
                {
                    BorderThickness = new Thickness(0, 0, 1, r == 0 ? 1 : 0),
                    BorderBrush = border,
                    Child = tb,
                };
                Grid.SetRow(cellBorder, r);
                Grid.SetColumn(cellBorder, c);
                grid.Children.Add(cellBorder);
            }
        }
        return new Border
        {
            BorderThickness = new Thickness(1, 1, 0, 0),
            BorderBrush = border,
            CornerRadius = new CornerRadius(4),
            Margin = new Thickness(0, 4),
            Child = grid,
        };
    }

    // -----------------------------------------------------------------
    // 行内
    // -----------------------------------------------------------------

    /// <summary>解析行内标记并逐段塞进 TextBlock.Inlines。</summary>
    private static void ParseInlines(TextBlock target, string text, double fontSize, bool bold, bool italic)
    {
        var inlines = new InlineCollection();
        target.Inlines = inlines;
        int i = 0;
        var buffer = new StringBuilder();

        void Flush()
        {
            if (buffer.Length == 0)
                return;
            var run = new Run(buffer.ToString());
            run.FontWeight = bold ? FontWeight.SemiBold : FontWeight.Normal;
            run.FontStyle = italic ? FontStyle.Italic : FontStyle.Normal;
            inlines.Add(run);
            buffer.Clear();
        }

        while (i < text.Length)
        {
            // 行内代码 `code`
            if (text[i] == '`')
            {
                int end = text.IndexOf('`', i + 1);
                if (end > i + 1)
                {
                    Flush();
                    var run = new Run(text[(i + 1)..end])
                    {
                        FontFamily = MonoFont,
                        Background = Brush("MarkdownCodeBrush", Color.FromArgb(28, 128, 128, 128)),
                    };
                    inlines.Add(run);
                    i = end + 1;
                    continue;
                }
            }

            // 粗体 **x** / __x__
            if (i + 1 < text.Length && ((text[i] == '*' && text[i + 1] == '*') || (text[i] == '_' && text[i + 1] == '_')))
            {
                string mark = text.Substring(i, 2);
                int end = text.IndexOf(mark, i + 2, StringComparison.Ordinal);
                if (end > i + 2)
                {
                    Flush();
                    inlines.Add(Styled(text[(i + 2)..end], bold: true, italic: italic, fontSize: fontSize));
                    i = end + 2;
                    continue;
                }
            }

            // 删除线 ~~x~~
            if (i + 1 < text.Length && text[i] == '~' && text[i + 1] == '~')
            {
                int end = text.IndexOf("~~", i + 2, StringComparison.Ordinal);
                if (end > i + 2)
                {
                    Flush();
                    var run = new Run(text[(i + 2)..end]) { TextDecorations = TextDecorations.Strikethrough };
                    inlines.Add(run);
                    i = end + 2;
                    continue;
                }
            }

            // 斜体 *x* / _x_
            if (text[i] is '*' or '_')
            {
                char mark = text[i];
                int end = text.IndexOf(mark, i + 1);
                if (end > i + 1 && end < text.Length)
                {
                    Flush();
                    inlines.Add(Styled(text[(i + 1)..end], bold: bold, italic: true, fontSize: fontSize));
                    i = end + 1;
                    continue;
                }
            }

            // 链接 [text](url)
            if (text[i] == '[')
            {
                int close = text.IndexOf(']', i + 1);
                if (close > i && close + 1 < text.Length && text[close + 1] == '(')
                {
                    int paren = text.IndexOf(')', close + 2);
                    if (paren > close)
                    {
                        Flush();
                        string label = text[(i + 1)..close];
                        string url = text[(close + 2)..paren];
                        // 链接渲染成"下划线 + 主题色"的 Run。**能力差异**：WPF 版给链接挂了
                        // ToolTip 显示 URL，Avalonia 里 Run 不是 Control（ToolTip.SetTip 只接 Control），
                        // 所以原型阶段把 URL 直接附在文本后（不静默丢弃信息）。
                        string shown = label.Contains(url, StringComparison.Ordinal) ? label : $"{label} <{url}>";
                        var run = new Run(shown)
                        {
                            Foreground = Brush("MarkdownLinkBrush", Color.FromArgb(255, 66, 133, 244)),
                            TextDecorations = TextDecorations.Underline,
                        };
                        inlines.Add(run);
                        i = paren + 1;
                        continue;
                    }
                }
            }

            buffer.Append(text[i]);
            i++;
        }
        Flush();
    }

    private static Run Styled(string text, bool bold, bool italic, double fontSize)
    {
        return new Run(text)
        {
            FontWeight = bold ? FontWeight.SemiBold : FontWeight.Normal,
            FontStyle = italic ? FontStyle.Italic : FontStyle.Normal,
            FontSize = fontSize,
        };
    }

    // -----------------------------------------------------------------
    // 小工具
    // -----------------------------------------------------------------

    private static TextBlock NewTextBlock(double fontSize) => new()
    {
        TextWrapping = TextWrapping.Wrap,
        FontSize = fontSize,
        LineHeight = fontSize * 1.5,
        Inlines = new InlineCollection(),
    };

    private static bool IsListItem(string trimmed)
    {
        if (trimmed.StartsWith("- ", StringComparison.Ordinal) || trimmed.StartsWith("* ", StringComparison.Ordinal)
            || trimmed.StartsWith("+ ", StringComparison.Ordinal))
            return true;
        if (trimmed.StartsWith("[ ] ", StringComparison.Ordinal) || trimmed.StartsWith("[x] ", StringComparison.OrdinalIgnoreCase))
            return true;
        int dot = trimmed.IndexOf(". ", StringComparison.Ordinal);
        return dot is > 0 and <= 4 && trimmed[..dot].All(char.IsDigit);
    }

    private static bool IsTableSeparator(string line)
    {
        string t = line.Trim();
        if (!t.StartsWith('|') || t.Length < 3)
            return false;
        foreach (char c in t)
        {
            if (c is not ('|' or '-' or ':' or ' '))
                return false;
        }
        return t.Contains('-');
    }

    private static List<string> SplitCells(string row)
    {
        string t = row.Trim();
        if (t.StartsWith('|'))
            t = t[1..];
        if (t.EndsWith('|'))
            t = t[..^1];
        return t.Split('|').Select(c => c.Trim()).ToList();
    }

    /// <summary>取主题画刷：优先 App.axaml 里按主题定义的资源，取不到用兜底色（保证不崩）。</summary>
    private static IBrush Brush(string key, Color fallback)
    {
        try
        {
            if (Application.Current is { } app && app.TryFindResource(key, out object? value) && value is IBrush brush)
                return brush;
        }
        catch (Exception)
        {
            // 资源系统未初始化（自检的无窗口模式）：直接用兜底色
        }
        return new SolidColorBrush(fallback);
    }
}
