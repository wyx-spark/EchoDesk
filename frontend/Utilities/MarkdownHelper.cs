// [EN] EchoDesk WPF frontend module: frontend/Utilities/MarkdownHelper.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Utilities/MarkdownHelper.cs。类型与成员说明见下方注释。
using System;
using System.Text.RegularExpressions;

namespace EchoDesk.Utilities
{
    /// <summary>
    /// Markdown 渲染辅助（可选）：将模型返回的 Markdown 文本转换为
    /// 适合在 WPF TextBlock 中直接显示的纯文本（移除标记符号，保留可读内容）。
    /// </summary>
    public static class MarkdownHelper
    {
        /// <summary>
        /// 将 Markdown 源文本转换为纯文本。
        /// </summary>
        public static string ToPlainText(string markdown)
        {
            if (string.IsNullOrEmpty(markdown)) return markdown ?? "";

            var text = markdown;

            // 代码块 ```lang\n...``` -> 保留代码内容
            text = Regex.Replace(text, @"```[a-zA-Z0-9_+-]*[ \t]*\r?\n?(.*?)```", "$1", RegexOptions.Singleline);
            // 行内代码 `code`
            text = Regex.Replace(text, @"`([^`\n]+)`", "$1");
            // 图片 ![alt](url) -> [图片: alt]
            text = Regex.Replace(text, @"!\[([^\]]*)\]\([^)]*\)", "[图片: $1]");
            // 链接 [text](url) -> text
            text = Regex.Replace(text, @"\[([^\]]+)\]\([^)]*\)", "$1");
            // 标题 # ...
            text = Regex.Replace(text, @"(?m)^\s{0,3}#{1,6}\s+", "");
            // 粗体 **text** / __text__
            text = Regex.Replace(text, @"\*\*([^*]+)\*\*", "$1");
            text = Regex.Replace(text, @"__([^_]+)__", "$1");
            // 斜体 *text* / _text_
            text = Regex.Replace(text, @"\*([^*\n]+)\*", "$1");
            text = Regex.Replace(text, @"(?<!\w)_([^_\n]+)_(?!\w)", "$1");
            // 删除线 ~~text~~
            text = Regex.Replace(text, @"~~([^~]+)~~", "$1");
            // 无序列表
            text = Regex.Replace(text, @"(?m)^\s{0,3}[-*+]\s+", "• ");
            // 有序列表（保留编号语义，去掉 Markdown 点号格式差异）
            text = Regex.Replace(text, @"(?m)^\s{0,3}\d+\.\s+", "  ");
            // 引用 >
            text = Regex.Replace(text, @"(?m)^\s{0,3}>\s?", "");
            // 分隔线 ---
            text = Regex.Replace(text, @"(?m)^\s*([-*_])\s*(\1\s*){2,}$", "────────────");

            return text.Trim();
        }

        /// <summary>
        /// 提取 Markdown 中的全部代码块内容（``` 围栏），用空行分隔；
        /// 没有围栏代码块时返回空串。供消息的「复制代码」按钮使用。
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

        /// <summary>Markdown 中围栏代码块的数量。</summary>
        public static int CountCodeBlocks(string markdown)
        {
            if (string.IsNullOrEmpty(markdown)) return 0;
            return Regex.Matches(markdown, @"```[a-zA-Z0-9_+#.-]*[ \t]*\r?\n(.*?)```", RegexOptions.Singleline).Count;
        }
    }
}
