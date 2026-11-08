// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/Emoji.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/Emoji.cs。类型与成员说明见下方注释。
// [EN] Emoji fallback (ported from frontend/Services/Emoji.cs, the design specification
// [EN] fix 6).
// [EN]
// [EN] With cross-platform support, two premises of this logic changed and must be revised
// [EN] together:
// [EN]   1. **The font file is no longer always seguiemj.ttf**: Linux uses Noto Color Emoji
// [EN]      and macOS uses Apple Color Emoji (a .ttc inside the system font directory).
// [EN]      Availability detection can therefore no longer rely on "whether some Windows
// [EN]      file name exists".
// [EN]   2. **The backend tells us whether it emits emoji**: the ready line carries an
// [EN]      `emoji` field (decided by backend/emoji.py); on non-Windows platforms the
// [EN]      backend may already have fallen back to ASCII.
// [EN]
// [EN] The strategy here is therefore: **assume support by default** (Avalonia ships the
// [EN] Inter font fallback, and current versions of all three major platforms have color
// [EN] emoji). The original font-file probe is kept on Windows only (trimmed enterprise
// [EN] builds can indeed lack it), and a setting can force it on or off.
// [EN]
// [EN] The ASCII map must match ASCII_MAP in backend/emoji.py (compared by
// [EN] tools/check_emoji.ps1).
// [ZH] Emoji 降级（移植自 frontend/Services/Emoji.cs，设计规范修复六）。
// [ZH]
// [ZH] 支持跨平台后，这段逻辑的两个前提都变了，必须一并修改：
// [ZH]   1. **字体文件不再固定是 seguiemj.ttf**：Linux 上是 Noto Color Emoji，
// [ZH]      macOS 上是 Apple Color Emoji（且是系统字体目录中的 .ttc）。因此可用性
// [ZH]      检测不能再按“某个 Windows 文件名是否存在”来判断。
// [ZH]   2. **后端会告知我们它是否输出 emoji**：ready 行里有 `emoji` 字段
// [ZH]      （由 backend/emoji.py 判断），非 Windows 平台上后端可能已降级成 ASCII。
// [ZH]
// [ZH] 因此这里的策略是：**默认按支持处理**（Avalonia 自带 Inter 字体回退，且三大
// [ZH] 平台的现代版本都有彩色 emoji），只在 Windows 上保留原来的字体文件检测
// [ZH] （企业精简版系统确实会缺），并可被设置项强制打开或关闭。
// [ZH]
// [ZH] ASCII 映射表必须与 backend/emoji.py 的 ASCII_MAP 保持一致
// [ZH] （由 tools/check_emoji.ps1 比对）。
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace EchoDesk.Avalonia.Services;

public static class Emoji
{
    /// <summary>
    /// [EN] Emoji to ASCII marker. Every emoji not listed here is replaced with "?", because
    /// [EN] weaker information is preferable to a missing-glyph box.
    /// [ZH] emoji → ASCII 标记。未列出的 emoji 统一替换为 "?"，宁可信息弱一点也不显示方块。
    /// </summary>
    private static readonly Dictionary<string, string> AsciiMap = new(StringComparer.Ordinal)
    {
        ["✅"] = "[OK]", ["❌"] = "[X]", ["⚠️"] = "[!]", ["⚠"] = "[!]",
        ["⏳"] = "[..]", ["⏱️"] = "[time]", ["⏱"] = "[time]",
        ["⚡"] = "[fast]",
        ["⏹"] = "[stop]",
        ["✨"] = "*", ["🌙"] = "[dark]", ["☀️"] = "[light]", ["☀"] = "[light]",
        ["📡"] = "[net]", ["🧠"] = "[model]", ["🛠️"] = "[mode]", ["🛠"] = "[mode]",
        ["📂"] = "[folder]", ["📁"] = "[folder]", ["📄"] = "[file]", ["📋"] = "[copy]",
        ["🔍"] = "[search]", ["🔎"] = "[search]", ["📊"] = "[chart]", ["📈"] = "[chart]", ["📐"] = "[ruler]",
        ["🙈"] = "[ignore]",
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
        ["💭"] = "[thought]",
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
    /// [EN] Whether this machine has a color emoji font.
    /// [EN] Windows: still decided by the presence of seguiemj.ttf (trimmed builds and
    /// [EN] Server Core can indeed lack it).
    /// [EN] macOS/Linux: assume support — a system-level color emoji font is standard, and no
    /// [EN] cheap criterion such as the "rendering result" is available (drawing cannot be used
    /// [EN] as a test in a headless environment either).
    /// [ZH] 本机是否具备彩色 emoji 字体。
    /// [ZH] Windows：仍按 seguiemj.ttf 存在性判断（精简版/Server Core 确实会缺）。
    /// [ZH] macOS/Linux：按支持处理 —— 系统级彩色 emoji 字体是标配，且拿不到“渲染结果”
    /// [ZH] 这种廉价判据（无显示器环境下更不能靠绘制来判断）。
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
                found = true;   // [EN] Font directory unavailable: assume support (Avalonia has
                                // [EN] its own fallback chain).
                                // [ZH] 取不到字体目录：按支持处理（Avalonia 自身还有回退链）。
            }
            _available = found;
            return found;
        }
    }

    /// <summary>
    /// [EN] Whether emoji are emitted as status icons (in "follow the system" mode this is
    /// [EN] decided by <see cref="Available"/>).
    /// [ZH] 是否输出 emoji 作为状态图标（"跟随系统"模式下由 <see cref="Available"/> 决定）。
    /// </summary>
    public static bool Enabled { get; private set; } = true;

    /// <summary>
    /// [EN] User setting (auto / always / never).
    /// [ZH] 用户设置（auto / always / never）。
    /// </summary>
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

    /// <summary>
    /// [EN] The emoji flag from the backend ready line: when the backend reports that it is
    /// [EN] not enabled, the front end should degrade as well.
    /// [ZH] 后端 ready 行里的 emoji 标记：后端说它没启用时，前端也应当跟着降级。
    /// </summary>
    public static void ConfigureFromBackend(bool backendEmoji, string? preference)
    {
        Configure(preference);
        if (Enabled && !backendEmoji && string.IsNullOrWhiteSpace(preference))
            Enabled = false;
    }

    /// <summary>
    /// [EN] Serialize back to the settings file ("auto" is preserved by the caller).
    /// [ZH] 序列化回设置文件（"auto" 由调用方保留）。
    /// </summary>
    public static string Serialize(bool enabled) => enabled ? "always" : "never";

    /// <summary>
    /// [EN] Replace emoji with ASCII markers when plain text is required (emoji not allowed);
    /// [EN] otherwise return the text unchanged.
    /// [ZH] 需要纯文本（不允许 emoji）时把 emoji 替换成 ASCII 标记；否则原样返回。
    /// </summary>
    public static string Prefer(string text) => Enabled || string.IsNullOrEmpty(text) ? text : Strip(text);

    /// <summary>
    /// [EN] Unconditionally replace emoji with ASCII markers (for log output, file writes and
    /// [EN] other cases that require plain text).
    /// [ZH] 无条件把 emoji 换成 ASCII 标记（日志、文件写入等必须纯文本的场合）。
    /// </summary>
    public static string Strip(string text)
    {
        if (string.IsNullOrEmpty(text))
            return text;
        string result = text;
        // [EN] Replace the long forms first (sequences with variation selectors) so that "⚠️"
        // [EN] is not consumed by "⚠" first, which would leave an orphaned variation selector.
        // [ZH] 先替换长的（带变体选择符的组合），避免 "⚠️" 被 "⚠" 先吃掉而留下孤立的选择符
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

    /// <summary>
    /// [EN] Also remove emoji that the map does not cover (replaced with "?"), so that no
    /// [EN] missing-glyph box ever appears in the UI.
    /// [ZH] 把映射表没覆盖到的 emoji 也清掉（换成 "?"），确保界面上不会出现方块。
    /// </summary>
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
                i += width;     // [EN] Variation selectors and zero-width joiners: skipped,
                                // [EN] taking no position.
                                // [ZH] 变体选择符与零宽连接符：仅丢弃，不占位
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
