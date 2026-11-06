// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Utilities/MarkdownRenderer.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Utilities/MarkdownRenderer.cs。类型与成员说明见下方注释。
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
using Avalonia.Automation;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
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
                // **误包进代码块的表格还原**（v0.2.2 第 4 批 3b）：模型经常把整张表格
                // 塞进 ``` 里（"表格用代码块包起来更整齐"是常见误解），照原样渲染就变成
                // 一坨对齐不了的等宽文本。判据与 WPF 一致：内容里出现表格分隔行 → 按
                // Markdown 重新解析；否则才是真正的代码块。
                string bodyText = body.ToString().TrimEnd('\n');
                if (LooksLikeMarkdown(bodyText))
                    panel.Children.Add(Build(bodyText, fontSize));
                else
                    panel.Children.Add(BuildCodeBlock(bodyText, lang, fontSize));
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
        ParseInlines(tb.Inlines!, text, fontSize, bold: false, italic: false);
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
        ParseInlines(tb.Inlines!, text, size, bold: true, italic: false);
        return tb;
    }

    private static Control BuildQuote(string text, double fontSize)
    {
        var inner = NewTextBlock(fontSize);
        inner.Opacity = 0.85;
        ParseInlines(inner.Inlines!, text, fontSize, bold: false, italic: true);
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
            ParseInlines(body.Inlines!, text, fontSize, bold: false, italic: false);
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
        var parsed = rows.Select(SplitCells).ToList();
        if (parsed.Count == 0)
            return new TextBlock { Text = "" };

        // 列对齐（v0.2.2 第 4 批 3b）：从分隔行（|:---:|---:|）读出来，与 WPF 同一判据。
        // 此前全部左对齐 —— 数字列（金额、数量）因此看起来是歪的。
        int separatorIndex = -1;
        for (int r = 0; r < rows.Count; r++)
        {
            if (IsTableSeparator(rows[r]))
            {
                separatorIndex = r;
                break;
            }
        }
        List<string> separatorCells = separatorIndex >= 0 ? parsed[separatorIndex] : new List<string>();

        int columns = parsed.Max(r => r.Count);
        var aligns = new TextAlignment[columns];
        for (int c = 0; c < columns; c++)
        {
            string spec = c < separatorCells.Count ? separatorCells[c].Trim() : "";
            aligns[c] = spec.StartsWith(':') && spec.EndsWith(':') ? TextAlignment.Center
                : spec.EndsWith(':') ? TextAlignment.Right
                : TextAlignment.Left;
        }

        var grid = new Grid();
        for (int c = 0; c < columns; c++)
            grid.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));
        for (int r = 0; r < parsed.Count; r++)
            grid.RowDefinitions.Add(new RowDefinition(GridLength.Auto));

        IBrush border = Brush("MarkdownRuleBrush", Color.FromArgb(60, 128, 128, 128));
        int rowIndex = 0;
        for (int r = 0; r < parsed.Count; r++)
        {
            if (r == separatorIndex)
                continue;   // 分隔行本身不渲染成一行
            // 表头加粗的判据与 WPF 一致：分隔行之前的行都是表头（分隔行在第 0 行时也当表头）
            bool header = r < separatorIndex || separatorIndex == 0;
            for (int c = 0; c < columns; c++)
            {
                string cell = c < parsed[r].Count ? parsed[r][c] : "";
                var tb = NewTextBlock(fontSize);
                tb.Margin = new Thickness(6, 4, 6, 4);
                tb.TextAlignment = aligns[c];
                if (header)
                    tb.FontWeight = FontWeight.SemiBold;
                ParseInlines(tb.Inlines!, cell, fontSize, bold: header, italic: false);
                var cellBorder = new Border
                {
                    BorderThickness = new Thickness(0, 0, 1, header ? 1 : 0),
                    BorderBrush = border,
                    Child = tb,
                };
                Grid.SetRow(cellBorder, rowIndex);
                Grid.SetColumn(cellBorder, c);
                grid.Children.Add(cellBorder);
            }
            rowIndex++;
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

    /// <summary>
    /// 判断一段（被 ``` 包起来的）内容其实是不是 Markdown：出现形如 <c>|:---:|---:|</c>
    /// 的表格分隔行就算。与 WPF 的 <c>LooksLikeMarkdown</c> 同一判据。
    /// </summary>
    private static bool LooksLikeMarkdown(string code)
    {
        foreach (string raw in code.Replace("\r\n", "\n").Split('\n'))
        {
            string t = raw.Trim();
            if (!t.StartsWith('|'))
                continue;
            List<string> cells = SplitCells(t);
            if (cells.Count >= 2 && cells.All(c => IsSeparatorCell(c)))
                return true;
        }
        return false;
    }

    /// <summary>分隔行里的单个单元格：<c>---</c> / <c>:---</c> / <c>---:</c> / <c>:---:</c>。</summary>
    private static bool IsSeparatorCell(string cell)
    {
        string s = cell.Trim();
        if (s.Length == 0)
            return false;
        if (s.StartsWith(':'))
            s = s[1..];
        if (s.EndsWith(':'))
            s = s[..^1];
        return s.Length > 0 && s.All(c => c == '-');
    }

    // -----------------------------------------------------------------
    // 行内
    // -----------------------------------------------------------------

    /// <summary>
    /// 解析行内标记并逐段塞进 Inlines。
    ///
    /// **递归解析内层**（v0.2.2 第 4 批 3b）：此前是"扁平"的 —— `**粗中带 `码`**`
    /// 里那段行内代码不会被识别，整段当成粗体文字原样显示反引号。
    /// 现在与 WPF 的 <c>ParseInlines</c> 一样：配对包裹符里的内容**再走一遍本函数**。
    /// 行内代码是唯一的例外（它内部不解析 Markdown，与 CommonMark 一致）。
    /// </summary>
    private static void ParseInlines(InlineCollection col, string text, double fontSize, bool bold, bool italic)
    {
        int i = 0;
        int n = text.Length;
        var buffer = new StringBuilder();

        void Flush()
        {
            if (buffer.Length == 0)
                return;
            var run = new Run(buffer.ToString());
            run.FontWeight = bold ? FontWeight.SemiBold : FontWeight.Normal;
            run.FontStyle = italic ? FontStyle.Italic : FontStyle.Normal;
            col.Add(run);
            buffer.Clear();
        }

        while (i < n)
        {
            char c = text[i];

            // 链接 [文本](url)：标签内部支持嵌套；**可点击并打开浏览器**（3b）。
            if (c == '[')
            {
                int close = text.IndexOf("](", i, StringComparison.Ordinal);
                if (close >= 0)
                {
                    int end = text.IndexOf(')', close + 2);
                    if (end >= 0)
                    {
                        Flush();
                        string label = text[(i + 1)..close];
                        string url = text[(close + 2)..end];
                        col.Add(BuildLink(label, url, fontSize));
                        i = end + 1;
                        continue;
                    }
                }
            }

            // 行内 $$...$$（出现在一行中间，例如「结论是 $$E = mc^2$$ 这样」）。
            // 块级扫描只认行首的 $$，不处理这一支就会把定界符原样显示、内容被拆碎。
            if (c == '$' && i + 1 < n && text[i + 1] == '$')
            {
                int closeD = text.IndexOf("$$", i + 2, StringComparison.Ordinal);
                if (closeD >= 0)
                {
                    Flush();
                    col.Add(Mono(text[(i + 2)..closeD].Trim(), italic));
                    i = closeD + 2;
                    continue;
                }
            }

            // 行内数学 $...$。**必须与美元金额区分开**：`价格 $100 到 $200` 是最容易
            // 被误判成公式的输入，误判会把整段话变成等宽居中，比"公式没渲染"糟得多。
            // 判据见 LooksLikeInlineMath（与 WPF 逐条一致，含"跳过空白后的比较运算"那一条）。
            if (c == '$' && !(i + 1 < n && text[i + 1] == '$'))
            {
                int close = text.IndexOf('$', i + 1);
                if (close > i + 1 && close - i - 1 <= 200)
                {
                    string inner = text[(i + 1)..close];
                    if (LooksLikeInlineMath(inner))
                    {
                        Flush();
                        col.Add(Mono(inner, italic));
                        i = close + 1;
                        continue;
                    }
                }
            }

            // 配对包裹符：` `、** **、__ __、~~ ~~、* *、_ _
            // 顺序有意如此：先两字符再单字符，否则 `**` 会被当成两个 `*`。
            if (TryMark(text, i, out string mark, out int markLen))
            {
                int closeIdx = text.IndexOf(mark, i + markLen, StringComparison.Ordinal);
                if (closeIdx > i + markLen)
                {
                    string inner = text[(i + markLen)..closeIdx];
                    Flush();
                    if (mark == "`")
                    {
                        // 行内代码：直接成块，**不**解析内部 Markdown（与 CommonMark 一致）
                        var code = new Run(inner)
                        {
                            FontFamily = MonoFont,
                            Background = Brush("MarkdownCodeBrush", Color.FromArgb(28, 128, 128, 128)),
                        };
                        col.Add(code);
                    }
                    else
                    {
                        // 递归解析内层 —— 这就是"粗中嵌码/链"能显示出来的原因
                        var span = new Span();
                        span.FontWeight = mark is "**" or "__" ? FontWeight.SemiBold
                            : bold ? FontWeight.SemiBold : FontWeight.Normal;
                        span.FontStyle = italic ? FontStyle.Italic : FontStyle.Normal;
                        if (mark == "~~")
                        {
                            var strike = new Span { TextDecorations = TextDecorations.Strikethrough };
                            ParseInlines(strike.Inlines!, inner, fontSize, bold, italic);
                            col.Add(strike);
                        }
                        else
                        {
                            bool innerBold = bold || mark is "**" or "__";
                            bool innerItalic = italic || mark is "*" or "_";
                            ParseInlines(span.Inlines!, inner, fontSize, innerBold, innerItalic);
                            col.Add(span);
                        }
                    }
                    i = closeIdx + markLen;
                    continue;
                }
            }

            buffer.Append(c);
            i++;
        }
        Flush();
    }

    /// <summary>可点击的链接。</summary>
    ///
    /// **为什么用 InlineUIContainer 包一个 Button**：Avalonia 里 `Run` 不是 `Control`，
    /// 既挂不了 `PointerPressed` 也挂不了 ToolTip（WPF 侧用的是 `Hyperlink.RequestNavigate`）。
    /// `InlineUIContainer` 是 Avalonia 提供的"在文本流里嵌一个控件"的机制 ——
    /// 用它包一个"看起来像链接"的扁按钮（无边框、无背景、下划线 + 主题色），
    /// 排版上与一段文字无异，但**真的可以点**，点开交给系统浏览器。
    private static Inline BuildLink(string label, string url, double fontSize)
    {
        var button = new Button
        {
            Content = Emoji.Prefer(label.Length > 0 ? label : url),
            FontSize = fontSize,
            Foreground = Brush("MarkdownLinkBrush", Color.FromArgb(255, 66, 133, 244)),
            Background = Brushes.Transparent,
            BorderThickness = new Thickness(0),
            Padding = new Thickness(0),
            Margin = new Thickness(0),
            Cursor = new Cursor(StandardCursorType.Hand),
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTip.SetTip(button, url);
        button.Click += (_, _) => OpenUrl(url);
        AutomationProperties.SetAutomationId(button, "MarkdownLink");
        return new InlineUIContainer(button);
    }

    /// <summary>用系统默认程序打开链接（UseShellExecute：交给各平台的 shell 处理）。</summary>
    private static void OpenUrl(string url)
    {
        if (string.IsNullOrWhiteSpace(url))
            return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = url,
                UseShellExecute = true,
            });
        }
        catch (Exception)
        {
            // 没有默认浏览器 / URL 非法：静默（不打断阅读）
        }
    }

    /// <summary>等宽 + 代码前景色的行内片段（行内公式用）。</summary>
    private static Run Mono(string text, bool italic)
    {
        return new Run(text)
        {
            FontFamily = MonoFont,
            FontStyle = italic ? FontStyle.Italic : FontStyle.Normal,
            Foreground = Brush("MarkdownCodeForeground", Color.FromArgb(200, 60, 60, 60)),
        };
    }

    /// <summary>识别当前位置的配对包裹符；返回 false 表示这里没有标记。</summary>
    private static bool TryMark(string text, int i, out string mark, out int length)
    {
        mark = "";
        length = 0;
        if (i >= text.Length)
            return false;
        if (i + 1 < text.Length)
        {
            string two = text.Substring(i, 2);
            if (two is "**" or "__" or "~~")
            {
                mark = two;
                length = 2;
                return true;
            }
        }
        if (text[i] is '`' or '*' or '_')
        {
            mark = text[i].ToString();
            length = 1;
            return true;
        }
        return false;
    }

    /// <summary>
    /// 判断两个 <c>$</c> 之间的内容是否像行内公式（而不是美元金额）。
    ///
    /// 保守判据（宁可漏判也不误判，理由见调用点注释）：
    ///   * 含中日韩等宽字符 → **不是**公式（`$合同金额$` 是误判高发区）；
    ///   * 出现 LaTeX 特征字符 <c>\ ^ _ { } = + ( )</c> → 是公式；
    ///   * 比较运算两侧（**跳过空白后**）是"字母对字母/数字" → 是公式
    ///     （`x > 0` 是公式，`$100 > 50$` 不是）；
    ///   * 含常见数学函数/命令名 → 是公式；
    ///   * 其余（纯数字、数字+单位/货币符号）→ 不是公式。
    /// </summary>
    private static bool LooksLikeInlineMath(string inner)
    {
        if (string.IsNullOrWhiteSpace(inner))
            return false;
        foreach (char ch in inner)
        {
            if (ch > 0x2E7F)
                return false;
        }
        if (inner.IndexOfAny(new[] { '\\', '^', '_', '{', '}', '=', '+', '(', ')' }) >= 0)
            return true;

        foreach (char op in new[] { '>', '<' })
        {
            int pos = inner.IndexOf(op);
            if (pos < 0)
                continue;
            int li = pos - 1;
            while (li >= 0 && inner[li] == ' ')
                li--;
            int ri = pos + 1;
            while (ri < inner.Length && inner[ri] == ' ')
                ri++;
            if (li < 0 || ri >= inner.Length)
                continue;
            char left = inner[li];
            char right = inner[ri];
            if ((char.IsLetterOrDigit(left) && char.IsLetterOrDigit(right))
                && (char.IsLetter(left) || char.IsLetter(right)))
                return true;
        }

        string lower = inner.ToLowerInvariant();
        foreach (string token in new[]
                 {
                     "frac", "sqrt", "sum", "int", "prod", "lim", "log", "sin", "cos", "tan",
                     "alpha", "beta", "gamma", "delta", "theta", "lambda", "sigma", "omega", "pi",
                     "times", "cdot", "leq", "geq", "neq", "approx", "infty", "partial",
                 })
        {
            if (lower.Contains(token))
                return true;
        }
        return false;
    }

    // -----------------------------------------------------------------
    // 小工具
    // -----------------------------------------------------------------

    // [EN] Body line height is 1.6x the paragraph size, the typographic baseline of the shell.
    // [ZH] 正文行高为段落字号的 1.6 倍，即外壳的排版基线。
    private static TextBlock NewTextBlock(double fontSize) => new()
    {
        TextWrapping = TextWrapping.Wrap,
        FontSize = fontSize,
        LineHeight = fontSize * 1.6,
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



