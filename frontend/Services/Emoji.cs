using System;
using System.Collections.Generic;
using System.Text;

namespace EchoDesk.Services;

/// <summary>
/// Emoji 降级（任务书修复六）。
///
/// 主路径是"让 emoji 正常显示"：WPF 的字体回退链（<c>UiFontFamily</c>）已经把
/// <c>Segoe UI Emoji</c> 排进家族列表，所以 Windows 10/11 默认环境下 emoji 会正常渲染。
///
/// 本类负责**降级策略**：Windows 10 早期版本、精简版系统（N 版/Server Core）、
/// 被组策略裁掉字体的企业环境里可能连 <c>seguiemj.ttf</c> 都没有，此时 emoji 又变成方块。
/// 于是提供：
/// <list type="bullet">
/// <item><b>自动检测</b>：<see cref="Available"/> 检查 emoji 字体是否真的存在；</item>
/// <item><b>手动开关</b>：设置窗口里的"使用 emoji"选项，写入 <c>ui_state.json</c>；</item>
/// <item><b>ASCII 替换</b>：关掉或被检测出不支持时，<c>✅</c> → <c>[OK]</c>、
/// <c>⚠️</c> → <c>[!]</c>，绝不留下方块。</item>
/// </list>
///
/// 替换点只有两处（保持"一处收口"）：<see cref="Loc.T(string)"/> 与消息渲染。
/// </summary>
public static class Emoji
{
    /// <summary>emoji → ASCII 标记。未列出的 emoji 统一替换为 "?"，宁可信息弱一点也不显示方块。</summary>
    private static readonly Dictionary<string, string> AsciiMap = new(StringComparer.Ordinal)
    {
        ["✅"] = "[OK]", ["❌"] = "[X]", ["⚠️"] = "[!]", ["⚠"] = "[!]",
        ["⏳"] = "[..]", ["⏱️"] = "[time]", ["⏱"] = "[time]",
        ["✨"] = "*", ["🌙"] = "[dark]", ["☀️"] = "[light]", ["☀"] = "[light]",
        ["📡"] = "[net]", ["🧠"] = "[model]", ["🛠️"] = "[mode]", ["🛠"] = "[mode]",
        ["📂"] = "[folder]", ["📁"] = "[folder]", ["📄"] = "[file]", ["📋"] = "[copy]",
        ["🔍"] = "[search]", ["🔎"] = "[search]", ["📊"] = "[chart]", ["📈"] = "[chart]",
        ["💾"] = "[save]", ["📥"] = "[in]", ["📤"] = "[out]", ["🚀"] = "[go]",
        ["⚙️"] = "[cfg]", ["⚙"] = "[cfg]", ["🔧"] = "[cfg]",
        ["📝"] = "[edit]", ["👤"] = "[user]", ["🤖"] = "[bot]", ["ℹ️"] = "[i]", ["ℹ"] = "[i]",
        // 🔁：分段摘要"超时重试"那一行用的（v0.2.1 综合优化 任务四）。
        // 与 backend\emoji.py 的 ASCII_MAP 必须保持一致（tools\check_emoji.ps1 会比对）。
        ["🔁"] = "[retry]",
        ["🔗"] = "[link]", ["🧩"] = "[plugin]", ["🔒"] = "[lock]", ["🔓"] = "[unlock]",
        ["⏹️"] = "[stop]", ["⏹"] = "[stop]", ["▶️"] = "[run]", ["▶"] = "[run]",
        ["↻"] = "[reload]", ["⧉"] = "[md]", ["✓"] = "[v]", ["✗"] = "[x]",
        ["🎉"] = "[done]", ["🆕"] = "[new]", ["🗑️"] = "[del]", ["🗑"] = "[del]",
        ["➕"] = "[+]", ["➖"] = "[-]", ["👉"] = "->", ["🙋"] = "[?]",
        ["📦"] = "[pkg]",
        ["🗜"] = "[zip]",
        ["✔"] = "[v]",
        ["✕"] = "[x]",
        ["❓"] = "[?]",
        ["➤"] = ">",
        ["⬇"] = "[down]",
        ["⭐"] = "[*]",
        ["🌐"] = "[web]",
        ["🌡"] = "[temp]",
        ["🎨"] = "[art]",
        ["🎮"] = "[game]",
        ["🎯"] = "[target]",
        ["🏗"] = "[build]",
        ["🏪"] = "[store]",
        ["🏷"] = "[tag]",
        ["👋"] = "[hi]",
        ["💡"] = "[idea]",
        ["💬"] = "[chat]",
        ["📌"] = "[pin]",
        ["📎"] = "[attach]",
        ["📏"] = "[ruler]",
        ["📖"] = "[book]",
        ["📚"] = "[books]",
        ["🔄"] = "[sync]",
        ["🔌"] = "[plug]",
        ["🔹"] = "-",
        ["🖥"] = "[desktop]",
        ["🗂"] = "[files]",
        ["🧪"] = "[test]",
        ["🧵"] = "[thread]",
        ["🪟"] = "[window]",
        ["📉"] = "[trend-down]",
    };

    /// <summary>emoji 字体文件（Windows 自带）；缺失即认为环境不支持彩色 emoji。</summary>
    private static readonly string[] EmojiFontFiles = { "seguiemj.ttf" };

    private static bool? _available;

    /// <summary>
    /// 本机是否具备 emoji 字体。检测文件存在性而不是"渲染结果"——
    /// 后者需要一次真实绘制，代价高且在无界面环境不可用。
    /// </summary>
    public static bool Available
    {
        get
        {
            if (_available is bool cached)
                return cached;
            bool found = false;
            try
            {
                string fonts = Environment.GetFolderPath(Environment.SpecialFolder.Fonts);
                foreach (string file in EmojiFontFiles)
                {
                    if (System.IO.File.Exists(System.IO.Path.Combine(fonts, file)))
                    {
                        found = true;
                        break;
                    }
                }
            }
            catch
            {
                // 取不到字体目录：按支持处理（WPF 自身还有回退链）
                found = true;
            }
            _available = found;
            return found;
        }
    }

    /// <summary>是否输出 emoji 作为状态图标（"跟随系统"模式下由 <see cref="Available"/> 决定）。</summary>
    public static bool Enabled { get; private set; } = true;

    /// <summary>用户设置（auto / always / never）。</summary>
    public static void Configure(string? preference)
    {
        string value = (preference ?? "auto").Trim().ToLowerInvariant();
        Enabled = value switch
        {
            "always" or "on" or "true" => true,
            "never" or "off" or "false" => false,
            _ => Available,
        };
    }

    /// <summary>是否处于"用户显式选择"状态（设置界面里显示成"始终/从不"）。</summary>
    public static string Serialize(bool enabled) => enabled ? "always" : "never";

    /// <summary>
    /// 需要纯文本（不允许 emoji）时把 emoji 替换成 ASCII 标记；否则原样返回。
    /// 注意：这里只处理 emoji，不动中文与普通标点。
    /// </summary>
    public static string Prefer(string text) => Enabled || string.IsNullOrEmpty(text) ? text : Strip(text);

    /// <summary>无条件把 emoji 换成 ASCII 标记（日志、文件写入等必须纯文本的场合）。</summary>
    public static string Strip(string text)
    {
        if (string.IsNullOrEmpty(text))
            return text;
        string result = text;
        // 先替换长的（带变体选择符的组合），避免 "⚠️" 被 "⚠" 先吃掉而留下孤立的选择符
        foreach (KeyValuePair<string, string> pair in AsciiMap)
        {
            if (pair.Key.Length < 2)
                continue;
            if (result.Contains(pair.Key, StringComparison.Ordinal))
                result = result.Replace(pair.Key, pair.Value, StringComparison.Ordinal);
        }
        foreach (KeyValuePair<string, string> pair in AsciiMap)
        {
            if (pair.Key.Length != 1)
                continue;
            if (result.Contains(pair.Key, StringComparison.Ordinal))
                result = result.Replace(pair.Key, pair.Value, StringComparison.Ordinal);
        }
        return DropRemaining(result);
    }

    /// <summary>把映射表没覆盖到的 emoji 也清掉（换成 "?"），确保界面上不会出现方块。</summary>
    private static string DropRemaining(string text)
    {
        var builder = new StringBuilder(text.Length);
        int i = 0;
        while (i < text.Length)
        {
            int code = char.ConvertToUtf32(text, i);
            bool pair = char.IsSurrogatePair(text, i);
            int width = pair ? 2 : 1;
            if (code is 0xFE0F or 0xFE0E or 0x200D)
            {
                i += width;     // 变体选择符与零宽连接符：仅丢弃，不占位
                continue;
            }
            bool isEmoji = (code >= 0x1F000 && code <= 0x1FAFF)
                           || (code >= 0x2600 && code <= 0x27BF)
                           || (code >= 0x2B00 && code <= 0x2BFF);
            if (isEmoji)
                builder.Append('?');
            else
                builder.Append(text, i, width);
            i += width;
        }
        return builder.ToString();
    }
}
