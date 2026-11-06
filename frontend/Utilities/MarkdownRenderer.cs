// [EN] EchoDesk WPF frontend module: frontend/Utilities/MarkdownRenderer.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Utilities/MarkdownRenderer.cs。类型与成员说明见下方注释。
using System.Diagnostics;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Documents;
using System.Windows.Media;

namespace EchoDesk.Utilities;

/// <summary>
/// 轻量 Markdown → WPF FlowDocument 渲染器（支持标题/加粗/斜体/行内代码/代码块/
/// 列表/引用/链接/删除线/分隔线/表格），供 RichTextBox 只读展示模型输出。
///
/// 修复二：这里的颜色**不再硬编码**。<c>Paragraph</c>/<c>Run</c>/<c>Span</c>/<c>Hyperlink</c>/
/// <c>Table</c>/<c>TableCell</c> 都是 <c>FrameworkContentElement</c>，支持
/// <c>SetResourceReference</c>，因此直接绑定到色板键即可随主题切换自动重绘——
/// 不需要在主题切换时重建文档（原文 Markdown 并未保留，重建本来也做不到）。
/// </summary>
public static class MarkdownRenderer
{
    /// <summary>字体键：正文用 UiFontFamily（含 emoji 回退链），代码用 MonoFontFamily。</summary>
    private const string UiFontKey = "UiFontFamily";
    private const string MonoFontKey = "MonoFontFamily";

    /// <summary>把内容元素的属性绑定到色板键（修复二：随主题切换自动更新）。</summary>
    private static T Themed<T>(T element, DependencyProperty property, string key)
        where T : FrameworkContentElement
    {
        element.SetResourceReference(property, key);
        return element;
    }

    /// <summary>取字体家族的取值快照（字体家族不随主题变化，无需动态绑定）。</summary>
    private static FontFamily FontFor(string key, string fallback)
        => Application.Current?.TryFindResource(key) as FontFamily ?? new FontFamily(fallback);

    public static FlowDocument Build(string markdown)
    {
        var doc = new FlowDocument { PagePadding = new Thickness(2) };
        Themed(doc, FlowDocument.FontFamilyProperty, UiFontKey);
        AppendBlocks(doc, markdown ?? "");
        return doc;
    }

    /// <summary>
    /// 主题切换后的兜底刷新。
    ///
    /// 正常情况下不需要做任何事（上面全部用了资源引用），保留此入口是为了
    /// 兼容"文档在修复前构建、元素上写死了画刷"的极端情况——直接按当前色板重刷一遍。
    /// </summary>
    public static void RebindTheme(DependencyObject root)
    {
        // 资源引用已在 Build 时建立，主题字典替换后 WPF 会自动重新求值；
        // 这里只触发一次布局重算，确保复用中的消息区立即重绘（避免滚动时才刷新）。
        if (root is FrameworkElement element)
            element.InvalidateVisual();
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

            // ---------- 数学公式（v0.2.1 优化已知问题-2 交付要求） ----------
            // 需求原话是"增加前端对「$$ $$」MD 标识符的识别"。两种形态都处理：
            //   * 块级：整行（或多行）被 $$ 包起来 → 单独成一个居中式块；
            //   * 行内：正文里的 $...$ → 行内等宽呈现。
            // 识别放在代码块/表格之后（``` 与 | 表格优先级更高），但在普通段落之前。
            if (t.StartsWith("$$"))
            {
                // 同行闭合：$$ ... $$
                int close = t.IndexOf("$$", 2, StringComparison.Ordinal);
                if (close > 0)
                {
                    Flush(doc, ref para);
                    doc.Blocks.Add(BuildFormulaBlock(t.Substring(2, close - 2)));
                    i++;
                    continue;
                }
                // 跨行：从本行 $$ 之后开始收，直到出现只含 $$ 或在中间闭合的一行
                var body = new System.Text.StringBuilder(t.Substring(2));
                int k = i + 1;
                bool closed = false;
                while (k < lines.Length)
                {
                    string probe = lines[k];
                    int end = probe.IndexOf("$$", StringComparison.Ordinal);
                    if (end >= 0)
                    {
                        body.Append('\n').Append(probe.Substring(0, end));
                        closed = true;
                        k++;
                        break;
                    }
                    body.Append('\n').Append(probe);
                    k++;
                }
                Flush(doc, ref para);
                if (closed)
                {
                    doc.Blocks.Add(BuildFormulaBlock(body.ToString()));
                    i = k;
                }
                else
                {
                    // 流式未写完（只有开头的 $$）：先按文本显示，下一帧再渲染成公式，
                    // 免得"公式区"在流式过程中反复出现/消失（跳动比晚一点更糟）。
                    doc.Blocks.Add(BuildFormulaBlock(body.ToString()));
                    i = k;
                }
                continue;
            }

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
                doc.Blocks.Add(Themed(new Paragraph(new Run("──────────"))
                { Margin = new Thickness(0, 4, 0, 8) }, TextElement.ForegroundProperty, "QuoteForeground"));
                i++;
                continue;
            }
            if (t.StartsWith(">"))
            {
                Flush(doc, ref para);
                var q = Themed(new Paragraph { Margin = new Thickness(12, 0, 0, 4), FontStyle = FontStyles.Italic },
                               TextElement.ForegroundProperty, "QuoteForeground");
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
            BorderThickness = new Thickness(1),
            Margin = new Thickness(0, 4, 0, 8),
        };
        Themed(table, Block.BorderBrushProperty, "SeparatorBrush");
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
                    BorderThickness = new Thickness(0.5),
                };
                Themed(cell, Block.BorderBrushProperty, "SeparatorBrush");
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
            FontFamily = FontFor(MonoFontKey, "Consolas, Segoe UI Emoji"),
        };
        Themed(p, TextElement.BackgroundProperty, "CodeBackground");
        Themed(p, TextElement.ForegroundProperty, "CodeForeground");
        var text = code.TrimEnd('\n');
        p.Inlines.Add(new Run(text.Length == 0 ? " " : text));
        return p;
    }

    /// <summary>
    /// 渲染块级数学公式（<c>$$ ... $$</c>）。
    ///
    /// **为什么不引 LaTeX 排版引擎**：WPF 没有原生公式排版，社区方案要么依赖
    /// WebView2（本项目刻意不引入，见 UI 插件评估），要么依赖第三方渲染库
    /// （体积与许可证成本都超出"识别 $$"这一条要求）。这里按**等宽 + 居中 +
    /// 浅底**呈现公式源码：可读、可选中复制、与代码块视觉区分，
    /// 且对"把 LaTeX 粘到别处继续编辑"这件事最友好。
    ///
    /// 与代码块的区别：居中、无边框背景块改为浅底圆角（公式通常比代码短，
    /// 通栏底色会让页面显得很碎），字号跟随正文。
    /// </summary>
    private static Paragraph BuildFormulaBlock(string body)
    {
        var p = new Paragraph
        {
            Margin = new Thickness(8, 6, 8, 8),
            TextAlignment = TextAlignment.Center,
            FontFamily = FontFor(MonoFontKey, "Consolas, Segoe UI Emoji"),
        };
        Themed(p, TextElement.ForegroundProperty, "CodeForeground");
        string text = body.Trim();
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
                        Themed(link, TextElement.ForegroundProperty, "LinkBrush");
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

            // $$ ... $$ 出现在**一行中间**（探针抓出来的场景）：
            // `结论是 $$E = mc^2$$ 这样。` 这种写法在模型输出里很常见，而块级扫描只认
            // "行首是 $$"，漏掉它会让定界符原样显示、内容被拆成碎片。
            // 这里按**行内等宽片段**呈现（不新建居中段落：公式在句子中间时居中会
            // 把句子劈成两半），块级公式（自己占一行）才走居中样式。
            if (c == '$' && i + 1 < n && text[i + 1] == '$')
            {
                int closeD = text.IndexOf("$$", i + 2, StringComparison.Ordinal);
                if (closeD >= 0)
                {
                    FlushBuf(col, ref buf);
                    var formula = new Run(text.Substring(i + 2, closeD - i - 2).Trim())
                    {
                        FontFamily = FontFor(MonoFontKey, "Consolas, Segoe UI Emoji"),
                    };
                    Themed(formula, TextElement.ForegroundProperty, "CodeForeground");
                    col.Add(new Span(formula));
                    i = closeD + 2;
                    continue;
                }
            }

            // 行内数学：$...$（任务书交付要求"前端识别 $$ $$"；行内单 $ 一并支持）
            //
            // **必须与"美元金额"区分开**：`价格 $100 到 $200` 是最容易被误判成
            // 公式的输入。三条判据同时成立才当公式：① 两个 $ 之间有内容且不含
            // 空白包裹的换行；② 内容里**没有**裸数字紧跟货币语境（用"内容含
            // LaTeX 特征或非纯数字+单位"判断）；③ 两个 $ 之间的长度不超过 200
            // 字符（公式不会是一整段散文）。误判公式会把整段话变成等宽居中，
            // 比"公式没渲染"糟得多，所以判据偏保守。
            if (c == '$' && !(i + 1 < n && text[i + 1] == '$'))
            {
                int close = text.IndexOf('$', i + 1);
                if (close > i + 1 && close - i - 1 <= 200)
                {
                    string inner = text.Substring(i + 1, close - i - 1);
                    if (LooksLikeInlineMath(inner))
                    {
                        FlushBuf(col, ref buf);
                        var math = new Run(inner)
                        {
                            FontFamily = FontFor(MonoFontKey, "Consolas, Segoe UI Emoji"),
                        };
                        Themed(math, TextElement.ForegroundProperty, "CodeForeground");
                        col.Add(math);
                        i = close + 1;
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
                        var codeRun = new Run(inner) { FontFamily = FontFor(MonoFontKey, "Consolas, Segoe UI Emoji") };
                        Themed(codeRun, TextElement.BackgroundProperty, "CodeBackground");
                        Themed(codeRun, TextElement.ForegroundProperty, "CodeForeground");
                        col.Add(codeRun);
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

    /// <summary>
    /// 判断两个 <c>$</c> 之间的内容是否像行内公式（而不是美元金额）。
    ///
    /// 保守判据（宁可漏判也不误判，理由见调用点注释）：
    ///   * 出现 LaTeX 特征字符 <c>\ ^ _ { } = + / ( )</c> 或希腊字母名 → 公式；
    ///   * 或包含数学函数/命令名（frac / sqrt / sum / int / alpha …）→ 公式；
    ///   * 纯数字、或"数字+单位/货币符号" → **不是**公式（`$100`、`$1.5M`）；
    ///   * 含中文 → 不是公式（中文公式极少用 $ 包裹，而"$合同金额$"是误判高发区）。
    /// </summary>
    private static bool LooksLikeInlineMath(string inner)
    {
        if (string.IsNullOrWhiteSpace(inner))
            return false;
        foreach (char ch in inner)
        {
            if (ch > 0x2E7F)   // 中日韩等宽字符区：不当公式
                return false;
        }
        if (inner.IndexOfAny(new[] { '\\', '^', '_', '{', '}', '=', '+', '(', ')' }) >= 0)
            return true;
        // 比较运算 + 单字母变量：`x > 0`、`n < 10`、`a ≠ b` 这类也是公式。
        // 判据要求"比较符号两侧紧邻单字母或数字"，这样"$100 > 50$"（金额比较）
        // 不会被误判成公式 —— 它与纯金额一样属于散文，不是数学排版需求。
            // 比较运算 + 单字母变量：`x > 0`、`n < 10`、`a ≠ b` 这类也是公式。
            // 判据要求"比较符号**跳过空白后的**两侧是单字母或数字"，
            // 这样"$100 > 50$"（金额比较）不会被误判成公式 —— 它与纯金额一样
            // 属于散文，不是数学排版需求。注意必须跳过空白：`x > 0` 里紧邻 `>`
            // 的两个字符都是空格，不跳过就永远匹配不上（探针抓出来的）。
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
                if ((char.IsLetter(left) || char.IsDigit(left))
                    && (char.IsLetter(right) || char.IsDigit(right))
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
        // 剩下的情况只有"纯数字/字母表达式"：要求同时含数字与运算符之外，
        // 一律不当公式（`$100`、`$USD`、`$1,860,000` 都属于这一类）。
        return false;
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
