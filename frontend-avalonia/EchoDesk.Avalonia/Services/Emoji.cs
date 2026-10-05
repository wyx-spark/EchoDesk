// Emoji 降级（移植自 frontend/Services/Emoji.cs，任务书 修复六）。
//
// 跨平台后这段逻辑的两个前提都变了，必须一起改：
//   1. **字体文件不再固定是 seguiemj.ttf**：Linux 上是 Noto Color Emoji、
//      macOS 上是 Apple Color Emoji（且是 .ttc 在系统字体目录里）。所以可用性
//      检测不能再按"某个 Windows 文件名存不存在"来判断。
//   2. **后端会告诉我们它是否输出 emoji**：ready 行里有 `emoji` 字段
//      （backend/emoji.py 的判断），非 Windows 上后端可能已降级成 ASCII。
//
// 这里的策略因此是：**默认按支持处理**（Avalonia 自带 Inter 字体回退，且三大平台
// 现代版本都有彩色 emoji），只在 Windows 上保留原来的字体文件检测（企业精简版
// 系统确实会缺），并可被设置项强制打开/关闭。
//
// ASCII 映射表与 backend/emoji.py 的 ASCII_MAP 必须一致（tools/check_emoji.ps1 比对）。
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace EchoDesk.Avalonia.Services;

public static class Emoji
{
    /// <summary>emoji → ASCII 标记。未列出的 emoji 统一替换为 "?"，宁可信息弱一点也不显示方块。</summary>
    private static readonly Dictionary<string, string> AsciiMap = new(StringComparer.Ordinal)
    {
        ["✅"] = "[OK]", ["❌"] = "[X]", ["⚠️"] = "[!]", ["⚠"] = "[!]",
        ["⏳"] = "[..]", ["⏱️"] = "[time]", ["⏱"] = "[time]",
        ["⚡"] = "[fast]",
        ["⏹"] = "[stop]",
        ["✨"] = "*", ["🌙"] = "[dark]", ["☀️"] = "[light]", ["☀"] = "[light]",
        ["📡"] = "[net]", ["🧠"] = "[model]", ["🛠️"] = "[mode]", ["🛠"] = "[mode]",
        ["📂"] = "[folder]", ["📁"] = "[folder]", ["📄"] = "[file]", ["📋"] = "[copy]",
        ["🔍"] = "[search]", ["🔎"] = "[search]", ["📊"] = "[chart]", ["📈"] = "[chart]",
        ["💾"] = "[save]", ["📥"] = "[in]", ["📤"] = "[out]", ["🚀"] = "[go]",
        ["⚙️"] = "[cfg]", ["⚙"] = "[cfg]", ["🔧"] = "[cfg]",
        ["📝"] = "[edit]", ["👤"] = "[user]", ["🤖"] = "[bot]", ["ℹ️"] = "[i]", ["ℹ"] = "[i]",
        ["🔁"] = "[retry]",
        ["📍"] = "[step]",
        ["⏸"] = "[pause]",
        ["🔗"] = "[link]", ["🧩"] = "[plugin]", ["🔒"] = "[lock]", ["🔓"] = "[unlock]",
        ["⏹️"] = "[stop]", ["▶️"] = "[run]", ["▶"] = "[run]",
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
        ["🎛️"] = "[filter]",
        ["🎛"] = "[filter]",
        ["📴"] = "[offline]",
        ["🎚️"] = "[tiers]",
        ["🎚"] = "[tiers]",
    };

    private static bool? _available;

    /// <summary>
    /// 本机是否具备彩色 emoji 字体。
    /// Windows：仍按 seguiemj.ttf 存在性判断（精简版/Server Core 确实会缺）。
    /// macOS/Linux：按支持处理 —— 系统级彩色 emoji 字体是标配，且拿不到"渲染结果"
    /// 这种廉价判据（无显示器环境下更不能靠绘制来判断）。
    /// </summary>
    public static bool Available
    {
        get
        {
            if (_available is bool cached)
                return cached;
            if (!PlatformInfo.Current.IsWindows)
            {
                _available = true;
                return true;
            }
            bool found = false;
            try
            {
                string fonts = Environment.GetFolderPath(Environment.SpecialFolder.Fonts);
                found = File.Exists(Path.Combine(fonts, "seguiemj.ttf"));
            }
            catch
            {
                found = true;   // 取不到字体目录：按支持处理（Avalonia 自身还有回退链）
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

    /// <summary>后端 ready 行里的 emoji 标记：后端说它没启用时，前端也应当跟着降级。</summary>
    public static void ConfigureFromBackend(bool backendEmoji, string? preference)
    {
        Configure(preference);
        if (Enabled && !backendEmoji && string.IsNullOrWhiteSpace(preference))
            Enabled = false;
    }

    /// <summary>序列化回设置文件（"auto" 由调用方保留）。</summary>
    public static string Serialize(bool enabled) => enabled ? "always" : "never";

    /// <summary>需要纯文本（不允许 emoji）时把 emoji 替换成 ASCII 标记；否则原样返回。</summary>
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
