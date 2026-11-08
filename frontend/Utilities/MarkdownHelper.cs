// [EN] EchoDesk WPF frontend module: frontend/Utilities/MarkdownHelper.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Utilities/MarkdownHelper.cs。类型与成员说明见下方注释。
using System.Text.RegularExpressions;

namespace EchoDesk.Utilities
{
    /// <summary>
    /// [EN] Markdown rendering helper (optional): converts Markdown text returned by the model
    /// [EN] into plain text suitable for direct display in a WPF TextBlock (markup symbols are
    /// [EN] removed and readable content is kept).
    /// [ZH] Markdown 渲染辅助（可选）：将模型返回的 Markdown 文本转换为
    /// [ZH] 适合在 WPF TextBlock 中直接显示的纯文本（移除标记符号，保留可读内容）。
    /// </summary>
    public static class MarkdownHelper
    {
        /// <summary>
        /// [EN] Converts Markdown source text into plain text.
        /// [ZH] 将 Markdown 源文本转换为纯文本。
        /// </summary>
        public static string ToPlainText(string markdown)
        {
            if (string.IsNullOrEmpty(markdown)) return markdown ?? "";

            var text = markdown;

            // [EN] Code block ```lang\n...``` -> the code content is kept.
            // [ZH] 代码块 ```lang\n...``` -> 保留代码内容
            text = Regex.Replace(text, @"```[a-zA-Z0-9_+-]*[ \t]*\r?\n?(.*?)```", "$1", RegexOptions.Singleline);
            // [EN] Inline code `code`.
            // [ZH] 行内代码 `code`
            text = Regex.Replace(text, @"`([^`\n]+)`", "$1");
            // [EN] Image ![alt](url) -> image placeholder that keeps the alt text.
            // [ZH] 图片 ![alt](url) -> [图片: alt]
            text = Regex.Replace(text, @"!\[([^\]]*)\]\([^)]*\)", "[图片: $1]");
            // [EN] Link [text](url) -> text.
            // [ZH] 链接 [text](url) -> text
            text = Regex.Replace(text, @"\[([^\]]+)\]\([^)]*\)", "$1");
            // [EN] Heading # ...
            // [ZH] 标题 # ...
            text = Regex.Replace(text, @"(?m)^\s{0,3}#{1,6}\s+", "");
            // [EN] Bold **text** / __text__.
            // [ZH] 粗体 **text** / __text__
            text = Regex.Replace(text, @"\*\*([^*]+)\*\*", "$1");
            text = Regex.Replace(text, @"__([^_]+)__", "$1");
            // [EN] Italic *text* / _text_.
            // [ZH] 斜体 *text* / _text_
            text = Regex.Replace(text, @"\*([^*\n]+)\*", "$1");
            text = Regex.Replace(text, @"(?<!\w)_([^_\n]+)_(?!\w)", "$1");
            // [EN] Strikethrough ~~text~~.
            // [ZH] 删除线 ~~text~~
            text = Regex.Replace(text, @"~~([^~]+)~~", "$1");
            // [EN] Unordered list.
            // [ZH] 无序列表
            text = Regex.Replace(text, @"(?m)^\s{0,3}[-*+]\s+", "• ");
            // [EN] Ordered list (numbering semantics are kept; only the Markdown period
            // [EN] formatting is dropped).
            // [ZH] 有序列表（保留编号语义，去掉 Markdown 点号格式差异）
            text = Regex.Replace(text, @"(?m)^\s{0,3}\d+\.\s+", "  ");
            // [EN] Quote >
            // [ZH] 引用 >
            text = Regex.Replace(text, @"(?m)^\s{0,3}>\s?", "");
            // [EN] Horizontal rule ---
            // [ZH] 分隔线 ---
            text = Regex.Replace(text, @"(?m)^\s*([-*_])\s*(\1\s*){2,}$", "────────────");

            return text.Trim();
        }

        /// <summary>
        /// [EN] Extracts the content of all code blocks in the Markdown (``` fences),
        /// [EN] separated by blank lines; returns an empty string when there is no fenced
        /// [EN] code block. Used by the “copy code” button of a message.
        /// [ZH] 提取 Markdown 中的全部代码块内容（``` 围栏），用空行分隔；
        /// [ZH] 没有围栏代码块时返回空串。供消息的「复制代码」按钮使用。
        /// </summary>
        public static string ExtractCodeBlocks(string markdown)
        {
            if (string.IsNullOrEmpty(markdown)) return "";

            var blocks = new System.Collections.Generic.List<string>();
            foreach (Match m in Regex.Matches(markdown, @"```[a-zA-Z0-9_+#.-]*[ \t]*\r?\n(.*?)```", RegexOptions.Singleline))
            {
                string code = m.Groups[1].Value.TrimEnd('\r', '\n');
                if (code.Length > 0)
                    blocks.Add(code);
            }
            return string.Join("\n\n", blocks);
        }

        /// <summary>
        /// [EN] Number of fenced code blocks in the Markdown.
        /// [ZH] Markdown 中围栏代码块的数量。
        /// </summary>
        public static int CountCodeBlocks(string markdown)
        {
            if (string.IsNullOrEmpty(markdown)) return 0;
            return Regex.Matches(markdown, @"```[a-zA-Z0-9_+#.-]*[ \t]*\r?\n(.*?)```", RegexOptions.Singleline).Count;
        }
    }
}
