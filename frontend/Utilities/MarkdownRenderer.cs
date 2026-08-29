using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Documents;
using System.Windows.Media;

namespace EchoDesk.Utilities;

/// <summary>
/// 轻量 Markdown → WPF FlowDocument 渲染器（支持标题/加粗/斜体/行内代码/代码块/
/// 列表/引用/链接/删除线/分隔线/表格），供 RichTextBox 只读展示模型输出。
/// </summary>
public static class MarkdownRenderer
{
    private static readonly Brush CodeBg = new SolidColorBrush(Color.FromRgb(0x2b, 0x2b, 0x2b));
    private static readonly Brush CodeFg = new SolidColorBrush(Color.FromRgb(0xe6, 0xe6, 0xe6));
    private static readonly Brush QuoteFg = new SolidColorBrush(Color.FromRgb(0x8a, 0x8a, 0x8a));
    private static readonly Brush BorderBrush = new SolidColorBrush(Color.FromRgb(0x9a, 0x9a, 0x9a));

    public static FlowDocument Build(string markdown)
    {
        var doc = new FlowDocument { PagePadding = new Thickness(2), FontFamily = new FontFamily("Segoe UI") };
        AppendBlocks(doc, markdown ?? "");
        return doc;
    }

    /// <summary>核心解析：把 markdown 文本逐块解析进 doc。供顶层与“误包进代码块的表格”复用。</summary>
    private static void AppendBlocks(FlowDocument doc, string text)
    {
        var lines = text.Replace("\r\n", "\n").Split('\n');
        var para = new Paragraph();
        var code = new System.Text.StringBuilder();
        bool inCode = false;

        int i = 0;
        while (i < lines.Length)
        {
            string line = lines[i];
            if (inCode)
            {
                if (line.TrimStart().StartsWith("```"))
                {
                    FlushCodeBlock(doc, code.ToString());
                    code.Clear();
                    inCode = false;
                }
                else
                {
                    code.AppendLine(line);
                }
                i++;
                continue;
            }

            string t = line.Trim();

            if (t.StartsWith("```"))
            {
                Flush(doc, ref para);
                inCode = true;
                i++;
                continue;
            }
            if (t.Length == 0)
            {
                Flush(doc, ref para);
                i++;
                continue;
            }

            // 表格块：连续若干行以 | 开头，且其中包含分隔行（|---|）
            if (t.StartsWith("|") && CountBars(t) >= 2)
            {
                var rows = new List<string>();
                int j = i;
                while (j < lines.Length && lines[j].Trim().StartsWith("|"))
                {
                    rows.Add(lines[j].Trim());
                    j++;
                }
                var table = TryBuildTable(rows);
                if (table != null)
                {
                    Flush(doc, ref para);
                    doc.Blocks.Add(table);
                    i = j;
                    continue;
                }
                // 不是合法表格（无分隔行，如流式未写完）：按普通文本继续
            }

            if (t.StartsWith("---") || t.StartsWith("***") || t.StartsWith("___"))
            {
                Flush(doc, ref para);
                doc.Blocks.Add(new Paragraph(new Run("──────────"))
                { Foreground = QuoteFg, Margin = new Thickness(0, 4, 0, 8) });
                i++;
                continue;
            }
            if (t.StartsWith(">"))
            {
                Flush(doc, ref para);
                var q = new Paragraph { Margin = new Thickness(12, 0, 0, 4), Foreground = QuoteFg, FontStyle = FontStyles.Italic };
                AddInlines(q, t[1..].Trim());
                doc.Blocks.Add(q);
                i++;
                continue;
            }
            var h = Regex.Match(t, @"^(#{1,6})\s+(.*)$");
            if (h.Success)
            {
                Flush(doc, ref para);
                int level = h.Groups[1].Length;
                var head = new Paragraph { Margin = new Thickness(0, level == 1 ? 8 : 4, 0, 4), FontWeight = FontWeights.Bold };
                head.FontSize = level == 1 ? 20 : level == 2 ? 17 : 15;
                AddInlines(head, h.Groups[2].Value);
                doc.Blocks.Add(head);
                i++;
                continue;
            }
            var ul = Regex.Match(t, @"^[-*+]\s+(.*)$");
            if (ul.Success)
            {
                Flush(doc, ref para);
                var p = new Paragraph { Margin = new Thickness(16, 0, 0, 2) };
                p.Inlines.Add(new Run("• "));
                AddInlines(p, ul.Groups[1].Value);
                doc.Blocks.Add(p);
                i++;
                continue;
            }
            var ol = Regex.Match(t, @"^(\d+)[.、]\s+(.*)$");
            if (ol.Success)
            {
                Flush(doc, ref para);
                var p = new Paragraph { Margin = new Thickness(16, 0, 0, 2) };
                p.Inlines.Add(new Run(ol.Groups[1].Value + ". "));
                AddInlines(p, ol.Groups[2].Value);
                doc.Blocks.Add(p);
                i++;
                continue;
            }

            if (para.Inlines.Count > 0)
                para.Inlines.Add(new LineBreak());
            AddInlines(para, t);
            i++;
        }

        if (inCode)
            FlushCodeBlock(doc, code.ToString());
        Flush(doc, ref para);
    }

    /// <summary>代码块输出：若内容“像 Markdown 表格/标题”，则按 Markdown 解析（修正模型误包进代码块的表格）；否则保持纯代码块。</summary>
    private static void FlushCodeBlock(FlowDocument doc, string code)
    {
        if (LooksLikeMarkdown(code))
            AppendBlocks(doc, code);
        else
            doc.Blocks.Add(BuildCodeBlock(code));
    }

    /// <summary>判断代码块内容是否其实是 Markdown（存在形如 |:---:|---:| 的表格分隔行）。</summary>
    private static bool LooksLikeMarkdown(string code)
    {
        foreach (string raw in code.Replace("\r\n", "\n").Split('\n'))
        {
            string t = raw.Trim();
            if (!t.StartsWith("|"))
                continue;
            var cells = SplitCells(t);
            if (cells.Count >= 2 && cells.All(c => Regex.IsMatch(c, @"^:?-{1,}:?$")))
                return true; // 命中表格对齐行 → 判定为 Markdown
        }
        return false;
    }

    private static int CountBars(string s)
    {
        int n = 0;
        foreach (char c in s)
            if (c == '|') n++;
        return n;
    }

    private static List<string> SplitCells(string row)
    {
        string s = row.Trim();
        if (s.StartsWith("|")) s = s[1..];
        if (s.EndsWith("|")) s = s[..^1];
        return s.Split('|').Select(x => x.Trim()).ToList();
    }

    /// <summary>尝试把若干表格行解析成 FlowDocument.Table；缺少分隔行时返回 null。</summary>
    private static Table? TryBuildTable(List<string> rows)
    {
        int sepIndex = -1;
        for (int r = 0; r < rows.Count; r++)
        {
            var cells = SplitCells(rows[r]);
            if (cells.Count >= 2 && cells.All(c => Regex.IsMatch(c, @"^:?-{1,}:?$")))
            {
                sepIndex = r;
                break;
            }
        }
        if (sepIndex < 0)
            return null;

        int maxCols = rows.Max(r => SplitCells(r).Count);
        var aligns = new TextAlignment[maxCols];
        var sepCells = SplitCells(rows[sepIndex]);
        for (int c = 0; c < maxCols; c++)
        {
            string s = c < sepCells.Count ? sepCells[c] : "";
            aligns[c] = s.StartsWith(":") && s.EndsWith(":") ? TextAlignment.Center
                : s.EndsWith(":") ? TextAlignment.Right
                : s.StartsWith(":") ? TextAlignment.Left
                : TextAlignment.Left;
        }

        var table = new Table
        {
            CellSpacing = 0,
            BorderBrush = BorderBrush,
            BorderThickness = new Thickness(1),
            Margin = new Thickness(0, 4, 0, 8),
        };
        var rg = new TableRowGroup();
        table.RowGroups.Add(rg);

        for (int r = 0; r < rows.Count; r++)
        {
            if (r == sepIndex)
                continue;
            var cells = SplitCells(rows[r]);
            var tr = new TableRow();
            for (int c = 0; c < maxCols; c++)
            {
                string txt = c < cells.Count ? cells[c] : "";
                var p = new Paragraph { Margin = new Thickness(6, 2, 6, 2), TextAlignment = aligns[c] };
                AddInlines(p, txt);
                if (r < sepIndex || sepIndex == 0) // 表头加粗
                    foreach (var run in p.Inlines.OfType<Run>())
                        run.FontWeight = FontWeights.Bold;
                var cell = new TableCell(p)
                {
                    BorderBrush = BorderBrush,
                    BorderThickness = new Thickness(0.5),
                };
                tr.Cells.Add(cell);
            }
            rg.Rows.Add(tr);
        }
        return table;
    }

    private static void Flush(FlowDocument doc, ref Paragraph para)
    {
        if (para.Inlines.Count > 0)
            doc.Blocks.Add(para);
        para = new Paragraph();
    }

    private static Paragraph BuildCodeBlock(string code)
    {
        var p = new Paragraph
        {
            Margin = new Thickness(8, 2, 8, 8),
            Background = CodeBg,
            Foreground = CodeFg,
            FontFamily = new FontFamily("Consolas"),
        };
        var text = code.TrimEnd('\n');
        p.Inlines.Add(new Run(text.Length == 0 ? " " : text));
        return p;
    }

    /// <summary>递归解析行内 Markdown，支持任意嵌套（粗中嵌斜/码/删/链等）。</summary>
    private static void AddInlines(Paragraph p, string text)
    {
        ParseInlines(p.Inlines, text);
    }

    /// <summary>逐字符扫描 text，识别配对标记并递归解析内层，实现嵌套 Markdown 渲染。</summary>
    private static void ParseInlines(InlineCollection col, string text)
    {
        var buf = new System.Text.StringBuilder();
        int i = 0, n = text.Length;
        while (i < n)
        {
            char c = text[i];

            // 链接 [文](url)：优先整段识别，标签内可再嵌套
            if (c == '[')
            {
                int close = text.IndexOf("](", i);
                if (close >= 0)
                {
                    int end = text.IndexOf(')', close + 2);
                    if (end >= 0)
                    {
                        FlushBuf(col, ref buf);
                        string label = text.Substring(i + 1, close - i - 1);
                        string url = text.Substring(close + 2, end - close - 2);
                        var link = new Hyperlink { NavigateUri = new Uri(url, UriKind.RelativeOrAbsolute) };
                        link.RequestNavigate += (_, ea) =>
                        {
                            try { Process.Start(new ProcessStartInfo(ea.Uri.IsAbsoluteUri ? ea.Uri.AbsoluteUri : ea.Uri.OriginalString) { UseShellExecute = true }); }
                            catch { }
                        };
                        ParseInlines(link.Inlines, label); // 标签内部支持嵌套
                        col.Add(link);
                        i = end + 1;
                        continue;
                    }
                }
            }

            // 配对包裹符：** **  __ __  ~~ ~~  * *  _ _  ` `
            if (TryMark(text, i, out string open, out int openLen))
            {
                int closeIdx = text.IndexOf(open, i + openLen);
                if (closeIdx >= 0)
                {
                    FlushBuf(col, ref buf);
                    string inner = text.Substring(i + openLen, closeIdx - (i + openLen));
                    if (open == "`")
                    {
                        // 行内代码：直接成块，不解析内部 Markdown
                        col.Add(new Run(inner) { FontFamily = new FontFamily("Consolas"), Background = CodeBg, Foreground = CodeFg });
                    }
                    else
                    {
                        var span = new Span();
                        ApplyMark(span, open);
                        ParseInlines(span.Inlines, inner); // 递归处理嵌套
                        col.Add(span);
                    }
                    i = closeIdx + openLen;
                    continue;
                }
            }

            buf.Append(c);
            i++;
        }
        FlushBuf(col, ref buf);
    }

    private static void FlushBuf(InlineCollection col, ref System.Text.StringBuilder buf)
    {
        if (buf.Length > 0)
        {
            col.Add(new Run(buf.ToString()));
            buf.Clear();
        }
    }

    /// <summary>判断 text[i] 处是否是一个成对标记开头；是则返回标记串与长度。</summary>
    private static bool TryMark(string text, int i, out string mark, out int len)
    {
        char c = text[i];
        if (c == '*' && i + 1 < text.Length && text[i + 1] == '*') { mark = "**"; len = 2; return true; }
        if (c == '_' && i + 1 < text.Length && text[i + 1] == '_') { mark = "__"; len = 2; return true; }
        if (c == '~' && i + 1 < text.Length && text[i + 1] == '~') { mark = "~~"; len = 2; return true; }
        if (c == '`') { mark = "`"; len = 1; return true; }
        if (c == '*') { mark = "*"; len = 1; return true; }
        if (c == '_') { mark = "_"; len = 1; return true; }
        mark = ""; len = 0; return false;
    }

    private static void ApplyMark(Span span, string mark)
    {
        switch (mark)
        {
            case "**":
            case "__":
                span.FontWeight = FontWeights.Bold;
                break;
            case "*":
            case "_":
                span.FontStyle = FontStyles.Italic;
                break;
            case "~~":
                span.TextDecorations = TextDecorations.Strikethrough;
                break;
        }
    }
}
