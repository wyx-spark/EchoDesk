// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Utilities/MarkdownRenderer.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Utilities/MarkdownRenderer.cs。类型与成员说明见下方注释。
// [EN] Renderer that turns Markdown into Avalonia controls (counterpart of
// [EN] Utilities/MarkdownRenderer.cs in the WPF version).
// [ZH] Markdown → Avalonia 控件的渲染器（对应 WPF 版的 Utilities/MarkdownRenderer.cs）。
//
// [EN] **This is the one thing the prototype most needs to prove**: rich text in the WPF
// [EN] version is "Markdown → FlowDocument + Inline", while Avalonia has no FlowDocument,
// [EN] only TextBlock.Inlines (limited to inline layout within a single paragraph).
// [ZH] **这是本次原型最需要证明的一件事**：WPF 版的富文本是“Markdown → FlowDocument + Inline”，
// [ZH] 而 Avalonia 没有 FlowDocument，只有 TextBlock.Inlines（限于单段的行内排版）。
// [EN] The cross-platform version therefore uses a different structure:
// [EN]   * **Block level** (paragraph, heading, list, quote, table, code block, formula
// [EN]     block) = a control tree (StackPanel + Border + Grid);
// [EN]   * **Inline level** (bold, italic, strikethrough, inline code, link) = Run/Span
// [EN]     inside TextBlock.Inlines.
// [ZH] 于是跨平台版换了一种结构：
// [ZH]   * **块级**（段落/标题/列表/引用/表格/代码块/公式块）= 控件树（StackPanel + Border + Grid）；
// [ZH]   * **行内**（粗体/斜体/删除线/行内代码/链接）= TextBlock.Inlines 里的 Run/Span。
// [EN] The prototype confirmed that this structure works (the verification requirement of
// [EN] development task 1), but its **capability differences from the WPF version are
// [EN] real** and have been recorded one by one in records (for example, FlowDocument's
// [EN] automatic pagination and whole-document selection are replaced by "each paragraph
// [EN] is selectable").
// [ZH] 这套结构在原型里被验证可行（研发任务 1 的验证要求），但它与 WPF 版的**能力差异是真的**，
// [ZH] 已逐条记进 records（例如 FlowDocument 的自动分页/整段选择被换成“每段可选中”）。
//
// [EN] Markdig is deliberately not referenced: the WPF version is a hand-written parser
// [EN] with no third-party dependencies (26 KB), and the cross-platform version keeps the
// [EN] same tradeoff; the parsing rules are implemented to be "sufficient and testable",
// [EN] covering code blocks, tables, headings, lists, quotes, formulas, and inline markup.
// [ZH] 刻意不引 Markdig：WPF 版是零第三方依赖的手写解析（26 KB），跨平台版沿用同一取舍 ——
// [ZH] 解析规则按“够用且可测”实现，覆盖代码块/表格/标题/列表/引用/公式/行内标记。
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
    /// <summary>
    /// [EN] Monospace font chain: each platform contributes one built-in monospace font, with
    /// [EN] monospace as the final fallback.
    /// [ZH] 等宽字体链：三个平台各取一个自带等宽字体，最后留 monospace 兜底。
    /// </summary>
    public static readonly FontFamily MonoFont =
        new("Consolas, Menlo, DejaVu Sans Mono, Liberation Mono, Courier New, monospace");

    /// <summary>
    /// [EN] Renders Markdown into a control tree that can be hosted by a message container.
    /// [EN] When a trailing caret control is supplied, it is appended to the end of the
    /// [EN] final flow text block so that the streaming position follows the last glyph.
    /// [ZH] 将 Markdown 渲染为可由消息容器承载的控件树。若提供尾随光标控件，则将其追加到最后一个
    /// [ZH] 流式文本块的末尾，使流式位置紧随最后一个字形。
    /// </summary>
    public static Control Build(string markdown, double fontSize, Control? trailingCaret = null)
    {
        var panel = new StackPanel { Spacing = 6 };
        if (string.IsNullOrEmpty(markdown))
        {
            AttachTrailingCaret(panel, trailingCaret, fontSize);
            return panel;
        }

        string[] lines = markdown.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
        int i = 0;
        while (i < lines.Length)
        {
            string line = lines[i];
            string trimmed = line.TrimStart();

            // [EN] Code block ```lang ... ```
            // [ZH] 代码块 ```lang ... ```
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
                i++;   // [EN] Skip the closing ```.
                       // [ZH] 跳过收尾的 ```。
                // [EN] **Restoring a table wrapped in a code block by mistake** (v0.2.2
                // [EN] batch 4, 3b): the model often wraps an entire table in ``` ("wrapping
                // [EN] a table in a code block looks tidier" is a common misconception),
                // [EN] and rendering it verbatim yields monospaced text that cannot be
                // [EN] aligned. The criterion matches WPF: if the content contains a table
                // [EN] separator row, re-parse it as Markdown; otherwise it really is a code
                // [EN] block.
                // [ZH] **误包进代码块的表格还原**（v0.2.2 第 4 批 3b）：模型经常把整张表格
                // [ZH] 塞进 ``` 里（“表格用代码块包起来更整齐”是常见误解），照原样渲染就变成
                // [ZH] 一坨对齐不了的等宽文本。判据与 WPF 一致：内容里出现表格分隔行 → 按
                // [ZH] Markdown 重新解析；否则才是真正的代码块。
                string bodyText = body.ToString().TrimEnd('\n');
                if (LooksLikeMarkdown(bodyText))
                    panel.Children.Add(Build(bodyText, fontSize));
                else
                    panel.Children.Add(BuildCodeBlock(bodyText, lang, fontSize));
                continue;
            }

            // [EN] Formula block $$ ... $$
            // [ZH] 公式块 $$ ... $$
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

            // [EN] Table: the current line is | a | b | and the next line is |---|
            // [ZH] 表格：当前行是 | a | b | 且下一行是 |---|
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

            // [EN] Heading # .. ######
            // [ZH] 标题 # .. ######
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

            // [EN] Horizontal rule
            // [ZH] 分隔线
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

            // [EN] List item (ordered / unordered / task list); consecutive list items merge
            // [EN] into one list block.
            // [ZH] 列表项（有序/无序/任务清单）；连续的列表项合并成一个列表块
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

            // [EN] Quote
            // [ZH] 引用
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

            // [EN] Plain paragraph: consecutive text lines form one paragraph (soft line
            // [EN] breaks are handled within the same paragraph).
            // [ZH] 普通段落：连续的文本行合成一段（软换行按同一段处理）
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
        AttachTrailingCaret(panel, trailingCaret, fontSize);
        return panel;
    }

    // [EN] ---------- Streaming caret attachment ----------
    // [ZH] ---------- 流式光标挂接 ----------

    /// <summary>
    /// [EN] Attaches the supplied caret to the end of the last flow text block. The caret is
    /// [EN] inserted as an inline element of that block, therefore it occupies the exact glyph
    /// [EN] position and participates in the same text layout. A document whose last block is a
    /// [EN] non-flow block (code block, table or formula) receives a dedicated trailing
    /// [EN] paragraph, because appending inlines to a text container that renders a plain text
    /// [EN] string would replace its content.
    /// [ZH] 将给定光标挂接到最后一个流式文本块的末尾。光标作为该块的行内元素插入，因此它占据精确的
    /// [ZH] 字位并参与同一套文本排版。若文档的最后一个块为非流式块（代码块、表格或公式块），
    /// [ZH] 则改用一段专用的尾随段落承载，因为向以纯文本字符串呈现内容的文本容器追加行内元素
    /// [ZH] 会替换其既有内容。
    /// </summary>
    private static void AttachTrailingCaret(StackPanel panel, Control? trailingCaret, double fontSize)
    {
        if (trailingCaret is null)
            return;
        TextBlock? host = FindTrailingFlowTextBlock(panel);
        if (host is null || host.Inlines is null)
        {
            var fallback = NewTextBlock(fontSize);
            fallback.Inlines!.Add(new InlineUIContainer(trailingCaret));
            panel.Children.Add(fallback);
            return;
        }
        host.Inlines.Add(new InlineUIContainer(trailingCaret));
    }

    /// <summary>
    /// [EN] Locates the last text block in document order that renders through its inline
    /// [EN] collection. Containers are traversed in visual order so that the result is the
    /// [EN] block the reader perceives as the final line of the rendered document.
    /// [ZH] 按文档顺序定位最后一个通过行内集合呈现内容的文本块。容器按可视化顺序遍历，
    /// [ZH] 因此结果就是读者所感知的文档末行所在块。
    /// </summary>
    private static TextBlock? FindTrailingFlowTextBlock(Control root)
    {
        TextBlock? last = null;
        Collect(root);
        return last;

        void Collect(Control node)
        {
            switch (node)
            {
                case TextBlock block when string.IsNullOrEmpty(block.Text) && block.Inlines is { Count: > 0 }:
                    last = block;
                    break;
                case Panel panel:
                    foreach (Control child in panel.Children)
                        Collect(child);
                    break;
                case Decorator { Child: Control decorated }:
                    Collect(decorated);
                    break;
            }
        }
    }

    // [EN] ---------- Block level ----------
    // [ZH] ---------- 块级 ----------

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
                    // [EN] Clipboard busy: ignore it (the WPF version shows a prompt; the
                    // [EN] prototype stays silent for now).
                    // [ZH] 剪贴板被占用：忽略（WPF 版会弹提示，原型阶段先静默）
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

    /// <summary>
    /// [EN] Table: Avalonia has no FlowDocument Table, so it is drawn with Grid + Border.
    /// [ZH] 表格：Avalonia 没有 FlowDocument 的 Table，用 Grid + Border 画。
    /// </summary>
    private static Control BuildTable(List<string> rows, double fontSize)
    {
        var parsed = rows.Select(SplitCells).ToList();
        if (parsed.Count == 0)
            return new TextBlock { Text = "" };

        // [EN] Column alignment (v0.2.2 batch 4, 3b): read from the separator row
        // [EN] (|:---:|---:|), using the same criterion as WPF. Previously everything was
        // [EN] left-aligned, so numeric columns (amounts, quantities) looked crooked.
        // [ZH] 列对齐（v0.2.2 第 4 批 3b）：从分隔行（|:---:|---:|）读出来，与 WPF 同一判据。
        // [ZH] 此前全部左对齐 —— 数字列（金额、数量）因此看起来是歪的。
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
                continue;   // [EN] The separator row itself is not rendered as a row.
                            // [ZH] 分隔行本身不渲染成一行
            // [EN] Header bolding uses the same criterion as WPF: every row before the
            // [EN] separator row is a header (and when the separator is row 0, it is still a
            // [EN] header).
            // [ZH] 表头加粗的判据与 WPF 一致：分隔行之前的行都是表头（分隔行在第 0 行时也当表头）
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
    /// [EN] Decides whether content (wrapped in ```) is actually Markdown: a table separator
    /// [EN] row of the form <c>|:---:|---:|</c> is enough. The criterion matches WPF's
    /// [EN] <c>LooksLikeMarkdown</c>.
    /// [ZH] 判断一段（被 ``` 包起来的）内容其实是不是 Markdown：出现形如
    /// [ZH] <c>|:---:|---:|</c> 的表格分隔行就算。与 WPF 的 <c>LooksLikeMarkdown</c> 同一判据。
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

    /// <summary>
    /// [EN] A single cell of the separator row: <c>---</c> / <c>:---</c> / <c>---:</c> /
    /// [EN] <c>:---:</c>.
    /// [ZH] 分隔行里的单个单元格：<c>---</c> / <c>:---</c> / <c>---:</c> / <c>:---:</c>。
    /// </summary>
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

    // [EN] ---------- Inline level ----------
    // [ZH] ---------- 行内 ----------

    /// <summary>
    /// [EN] Parses inline markup and appends each segment to Inlines.
    /// [EN]
    /// [EN] **Recursive parsing of the inner content** (v0.2.2 batch 4, 3b): this used to be
    /// [EN] "flat": the inline code inside `**bold with `code`**` was not recognized, and the
    /// [EN] whole span was rendered as bold text with literal backticks. It now matches WPF's
    /// [EN] <c>ParseInlines</c>: the content between a pair of wrappers **goes through this
    /// [EN] function again**. Inline code is the only exception (its interior is not parsed as
    /// [EN] Markdown, consistent with CommonMark).
    /// [ZH] 解析行内标记并逐段塞进 Inlines。
    /// [ZH]
    /// [ZH] **递归解析内层**（v0.2.2 第 4 批 3b）：此前是“扁平”的 —— `**粗中带 `码`**`
    /// [ZH] 里那段行内代码不会被识别，整段当成粗体文字原样显示反引号。
    /// [ZH] 现在与 WPF 的 <c>ParseInlines</c> 一样：配对包裹符里的内容**再走一遍本函数**。
    /// [ZH] 行内代码是唯一的例外（它内部不解析 Markdown，与 CommonMark 一致）。
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

            // [EN] Link [text](url): the label supports nesting; **clickable and opens the
            // [EN] browser** (3b).
            // [ZH] 链接 [文本](url)：标签内部支持嵌套；**可点击并打开浏览器**（3b）。
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

            // [EN] Inline $$...$$ (appearing mid-line, for example "the conclusion is
            // [EN] $$E = mc^2$$"). The block-level scan only recognizes $$ at the start of a
            // [EN] line; without this branch the delimiters would be shown verbatim and the
            // [EN] content would be split apart.
            // [ZH] 行内 $$...$$（出现在一行中间，例如“结论是 $$E = mc^2$$ 这样”）。
            // [ZH] 块级扫描只认行首的 $$，不处理这一支就会把定界符原样显示、内容被拆碎。
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

            // [EN] Inline math $...$. **It must be distinguished from dollar amounts**:
            // [EN] `price $100 to $200` is the input most easily mistaken for a formula, and
            // [EN] a false positive turns the whole sentence into centered monospace, which is
            // [EN] much worse than "the formula was not rendered". The criterion is in
            // [EN] LooksLikeInlineMath (identical to WPF line by line, including the
            // [EN] "comparison after skipping whitespace" rule).
            // [ZH] 行内数学 $...$。**必须与美元金额区分开**：`价格 $100 到 $200` 是最容易
            // [ZH] 被误判成公式的输入，误判会把整段话变成等宽居中，比“公式没渲染”糟得多。
            // [ZH] 判据见 LooksLikeInlineMath（与 WPF 逐条一致，含“跳过空白后的比较运算”那一条）。
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

            // [EN] Paired wrappers: ` `, ** **, __ __, ~~ ~~, * *, _ _
            // [EN] The order is intentional: two-character wrappers come before
            // [EN] single-character ones, otherwise `**` would be treated as two `*` markers.
            // [ZH] 配对包裹符：` `、** **、__ __、~~ ~~、* *、_ _
            // [ZH] 顺序有意如此：先两字符再单字符，否则 `**` 会被当成两个 `*`。
            if (TryMark(text, i, out string mark, out int markLen))
            {
                int closeIdx = text.IndexOf(mark, i + markLen, StringComparison.Ordinal);
                if (closeIdx > i + markLen)
                {
                    string inner = text[(i + markLen)..closeIdx];
                    Flush();
                    if (mark == "`")
                    {
                        // [EN] Inline code: emitted as a block, **without** parsing inner
                        // [EN] Markdown (consistent with CommonMark).
                        // [ZH] 行内代码：直接成块，**不**解析内部 Markdown（与 CommonMark 一致）
                        var code = new Run(inner)
                        {
                            FontFamily = MonoFont,
                            Background = Brush("MarkdownCodeBrush", Color.FromArgb(28, 128, 128, 128)),
                        };
                        col.Add(code);
                    }
                    else
                    {
                        // [EN] Recursively parse the inner content; this is why nested code or
                        // [EN] links inside bold text can be rendered.
                        // [ZH] 递归解析内层 —— 这就是“粗中嵌码/链”能显示出来的原因
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

    /// <summary>
    /// [EN] Clickable link.
    /// [ZH] 可点击的链接。
    /// </summary>
    ///
    /// [EN] **Why wrap a Button in InlineUIContainer**: in Avalonia, `Run` is not a `Control`,
    /// [EN] so it supports neither `PointerPressed` nor ToolTip (the WPF side uses
    /// [EN] `Hyperlink.RequestNavigate`). `InlineUIContainer` is the mechanism Avalonia
    /// [EN] provides for "embedding a control in a text flow"; wrapping a flat button that
    /// [EN] looks like a link (no border, no background, underline plus theme color) in it
    /// [EN] is typographically indistinguishable from a run of text, but **it really is
    /// [EN] clickable**, and clicking hands the URL to the system browser.
    /// [ZH] **为什么用 InlineUIContainer 包一个 Button**：Avalonia 里 `Run` 不是 `Control`，
    /// [ZH] 既挂不了 `PointerPressed` 也挂不了 ToolTip（WPF 侧用的是 `Hyperlink.RequestNavigate`）。
    /// [ZH] `InlineUIContainer` 是 Avalonia 提供的“在文本流里嵌一个控件”的机制 ——
    /// [ZH] 用它包一个“看起来像链接”的扁按钮（无边框、无背景、下划线 + 主题色），
    /// [ZH] 排版上与一段文字无异，但**真的可以点**，点开交给系统浏览器。
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

    /// <summary>
    /// [EN] Opens a link with the system default program (UseShellExecute: hand it to the
    /// [EN] platform shell).
    /// [ZH] 用系统默认程序打开链接（UseShellExecute：交给各平台的 shell 处理）。
    /// </summary>
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
            // [EN] No default browser / invalid URL: stay silent (do not interrupt reading).
            // [ZH] 没有默认浏览器 / URL 非法：静默（不打断阅读）
        }
    }

    /// <summary>
    /// [EN] Inline monospace fragment with the code foreground color (used by inline formulas).
    /// [ZH] 等宽 + 代码前景色的行内片段（行内公式用）。
    /// </summary>
    private static Run Mono(string text, bool italic)
    {
        return new Run(text)
        {
            FontFamily = MonoFont,
            FontStyle = italic ? FontStyle.Italic : FontStyle.Normal,
            Foreground = Brush("MarkdownCodeForeground", Color.FromArgb(200, 60, 60, 60)),
        };
    }

    /// <summary>
    /// [EN] Recognizes a paired wrapper at the current position; returns false when no marker
    /// [EN] is present here.
    /// [ZH] 识别当前位置的配对包裹符；返回 false 表示这里没有标记。
    /// </summary>
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
    /// [EN] Decides whether the content between two <c>$</c> characters looks like inline math
    /// [EN] rather than a dollar amount.
    /// [EN]
    /// [EN] Conservative criteria (prefer missing a match over a false positive; the reason is
    /// [EN] in the call-site comment):
    /// [EN]   * contains CJK wide characters → **not** a formula (a Chinese amount between
    /// [EN]     dollar signs is a frequent false positive);
    /// [EN]   * contains LaTeX-specific characters <c>\ ^ _ { } = + ( )</c> → is a formula;
    /// [EN]   * both sides of a comparison operator (**after skipping whitespace**) are
    /// [EN]     "letter vs letter/digit" → is a formula (`x > 0` is a formula, `$100 > 50$`
    /// [EN]     is not);
    /// [EN]   * contains a common math function or command name → is a formula;
    /// [EN]   * anything else (pure digits, digits plus units or currency symbols) → not a
    /// [EN]     formula.
    /// [ZH] 判断两个 <c>$</c> 之间的内容是否像行内公式（而不是美元金额）。
    /// [ZH]
    /// [ZH] 保守判据（宁可漏判也不误判，理由见调用点注释）：
    /// [ZH]   * 含中日韩等宽字符 → **不是**公式（`$合同金额$` 是误判高发区）；
    /// [ZH]   * 出现 LaTeX 特征字符 <c>\ ^ _ { } = + ( )</c> → 是公式；
    /// [ZH]   * 比较运算两侧（**跳过空白后**）是“字母对字母/数字” → 是公式
    /// [ZH]     （`x > 0` 是公式，`$100 > 50$` 不是）；
    /// [ZH]   * 含常见数学函数/命令名 → 是公式；
    /// [ZH]   * 其余（纯数字、数字+单位/货币符号）→ 不是公式。
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

    // [EN] ---------- Utilities ----------
    // [ZH] ---------- 小工具 ----------

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

    /// <summary>
    /// [EN] Gets a theme brush: prefers resources defined per theme in App.axaml, falling back
    /// [EN] to a fallback color (so it never crashes).
    /// [ZH] 取主题画刷：优先 App.axaml 里按主题定义的资源，取不到用兜底色（保证不崩）。
    /// </summary>
    private static IBrush Brush(string key, Color fallback)
    {
        try
        {
            if (Application.Current is { } app)
            {
                // [EN] The theme variant is passed explicitly: the application requests
                // [EN] ThemeVariant.Default while the palette lives in the Light and Dark
                // [EN] dictionaries.
                // [ZH] 显式传入主题变体：应用程序请求 ThemeVariant.Default，而色板存放于 Light 与
                // [ZH] Dark 两个主题字典中。
                if (app.TryFindResource(key, app.ActualThemeVariant, out object? value) && value is IBrush brush)
                    return brush;
                if (app.TryFindResource(key, out value) && value is IBrush fallbackBrush)
                    return fallbackBrush;
            }
        }
        catch (Exception)
        {
            // [EN] Resource system not initialized (windowless self-check mode): use the
            // [EN] fallback color directly.
            // [ZH] 资源系统未初始化（自检的无窗口模式）：直接用兜底色
        }
        return new SolidColorBrush(fallback);
    }
}



