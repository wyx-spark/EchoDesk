// [EN] EchoDesk WPF frontend module: frontend/Services/Emoji.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend/Services/Emoji.cs。类型与成员说明见下方注释。
using System.Text;

namespace EchoDesk.Services;

/// <summary>
/// [EN] Emoji fallback support.
///
/// The primary path relies on WPF font fallback, which includes
/// <c>Segoe UI Emoji</c> and renders emoji correctly on standard Windows 10/11
/// installations.
///
/// This class provides the fallback policy for environments where the emoji font
/// is missing, such as early Windows 10 builds, reduced installations, or
/// enterprise images with fonts removed by policy:
/// <list type="bullet">
/// <item><b>Automatic detection</b>: <see cref="Available"/> checks whether the
/// emoji font file exists.</item>
/// <item><b>Manual switch</b>: the "use emoji" option stored in
/// <c>ui_state.json</c>.</item>
/// <item><b>ASCII replacement</b>: when disabled or unsupported, symbols such as
/// <c>✅</c> and <c>⚠️</c> become <c>[OK]</c> and <c>[!]</c> instead of
/// rendering as boxes.</item>
/// </list>
///
/// Replacement is centralized in <see cref="Loc.T(string)"/> and message
/// rendering.
///
/// [ZH] Emoji 降级支持。
///
/// 主路径依赖 WPF 字体回退链，其中包含 <c>Segoe UI Emoji</c>，在标准 Windows
/// 10/11 环境中可正确渲染 emoji。
///
/// 本类为缺少 emoji 字体的环境提供降级策略，例如早期 Windows 10、精简安装或
/// 被策略移除字体的企业镜像：
/// <list type="bullet">
/// <item><b>自动检测</b>：<see cref="Available"/> 检查 emoji 字体文件是否存在。</item>
/// <item><b>手动开关</b>：设置窗口中写入 <c>ui_state.json</c> 的“使用 emoji”选项。</item>
/// <item><b>ASCII 替换</b>：关闭或不受支持时，<c>✅</c>、<c>⚠️</c> 等符号替换为
/// <c>[OK]</c>、<c>[!]</c>，避免显示为方块。</item>
/// </list>
///
/// 替换集中在 <see cref="Loc.T(string)"/> 与消息渲染处。
/// </summary>
public static class Emoji
{
    /// <summary>
    /// [EN] Emoji-to-ASCII map. Unmapped emoji are replaced with "?" rather than
    /// risking a missing-glyph box.
    /// [ZH] emoji 到 ASCII 标记的映射。未列出的 emoji 统一替换为 "?"，避免出现缺字方块。
    /// </summary>
    private static readonly Dictionary<string, string> AsciiMap = new(StringComparer.Ordinal)
    {
        ["✅"] = "[OK]", ["❌"] = "[X]", ["⚠️"] = "[!]", ["⚠"] = "[!]",
        ["⏳"] = "[..]", ["⏱️"] = "[time]", ["⏱"] = "[time]",
        // [EN] Lightning bolt: first-token latency in runtime metrics.
        // [ZH] 闪电符号：运行指标中的首 token 延迟。
        ["⚡"] = "[fast]",
        // [EN] Stop symbol: aborted streaming and aborted runtime metrics.
        // [ZH] 停止符号：流式中止与运行指标中的“已中止”。
        ["⏹"] = "[stop]",
        ["✨"] = "*", ["🌙"] = "[dark]", ["☀️"] = "[light]", ["☀"] = "[light]",
        ["📡"] = "[net]", ["🧠"] = "[model]", ["🛠️"] = "[mode]", ["🛠"] = "[mode]",
        ["📂"] = "[folder]", ["📁"] = "[folder]", ["📄"] = "[file]", ["📋"] = "[copy]",
        ["🔍"] = "[search]", ["🔎"] = "[search]", ["📊"] = "[chart]", ["📈"] = "[chart]", ["📐"] = "[ruler]",
        ["🙈"] = "[ignore]",
        ["💾"] = "[save]", ["📥"] = "[in]", ["📤"] = "[out]", ["🚀"] = "[go]",
        ["⚙️"] = "[cfg]", ["⚙"] = "[cfg]", ["🔧"] = "[cfg]",
        ["📝"] = "[edit]", ["👤"] = "[user]", ["🤖"] = "[bot]", ["ℹ️"] = "[i]", ["ℹ"] = "[i]",
        // [EN] Repeat symbol: summary timeout retry line. Pin symbol: agent step
        // [EN] line "step N/M". This map must stay consistent with
        // [EN] backend/emoji.py; tools/check_emoji.ps1 compares them.
        // [ZH] 循环符号：摘要超时重试行。定位符号：Agent 步骤行“第 N/M 步”。
        // [ZH] 该映射必须与 backend/emoji.py 保持一致；tools/check_emoji.ps1 会比对。
        ["🔁"] = "[retry]",
        ["📍"] = "[step]",
        // [EN] Pause symbol: MCP server "disabled" status line. It is outside the
        // [EN] check_emoji.ps1 scan range, but is included because an unchecked
        // [EN] symbol can still render as a box.
        // [ZH] 暂停符号：MCP server“已停用”状态行。它不在 check_emoji.ps1 的扫描范围
        // [ZH] 内，但仍主动加入，因为未检查的符号也可能显示为方块。
        ["⏸"] = "[pause]",
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
        // [EN] Model-market filter panel title and offline-mode notice. Strings
        // [EN] with and without the variation selector are distinct and both must
        // [EN] be listed.
        // [ZH] 模型市场筛选面板标题与离线模式提示。带变体选择符（VS16）与不带的是
        // [ZH] 不同字符串，两者都必须列出。
        ["🎛️"] = "[filter]",
        ["🎛"] = "[filter]",
        ["📴"] = "[offline]",
        // [EN] Model-tier settings page title. Both forms are listed because the
        // [EN] variant-selector form would otherwise degrade to "?".
        // [ZH] 模型档位设置页标题。两种写法都必须列出，否则带 VS16 的形式会退化为 "?"。
        ["🎚️"] = "[tiers]",
        ["🎚"] = "[tiers]",
    };

    /// <summary>
    /// [EN] Emoji font files shipped with Windows. A missing file means color
    /// emoji are treated as unsupported.
    /// [ZH] Windows 自带的 emoji 字体文件。文件缺失时按不支持彩色 emoji 处理。
    /// </summary>
    private static readonly string[] EmojiFontFiles = { "seguiemj.ttf" };

    private static bool? _available;

    /// <summary>
    /// [EN] Whether the local system has an emoji font. Detection checks file
    /// existence rather than rendering, which would require a costly draw and is
    /// unavailable in headless environments.
    /// [ZH] 本机是否具备 emoji 字体。检测文件是否存在，而不是检测渲染结果；后者需要
    /// 真实绘制，开销高且在无界面环境中不可用。
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
                // [EN] If the font directory cannot be read, assume support; WPF
                // [EN] still has its own fallback chain.
                // [ZH] 无法读取字体目录时按支持处理；WPF 自身仍有回退链。
                found = true;
            }
            _available = found;
            return found;
        }
    }

    /// <summary>
    /// [EN] Whether emoji are emitted as status icons. In "follow system" mode,
    /// this is determined by <see cref="Available"/>.
    /// [ZH] 是否输出 emoji 作为状态图标。“跟随系统”模式下由 <see cref="Available"/>
    /// 决定。
    /// </summary>
    public static bool Enabled { get; private set; } = true;

    /// <summary>
    /// [EN] Apply the user preference: auto, always, or never.
    /// [ZH] 应用用户设置：auto、always 或 never。
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
    /// [EN] Serialize an explicit user choice for display as "always" or "never".
    /// [ZH] 将用户的显式选择序列化为“始终”或“从不”。
    /// </summary>
    public static string Serialize(bool enabled) => enabled ? "always" : "never";

    /// <summary>
    /// [EN] Replace emoji with ASCII markers when plain text is required;
    /// otherwise return the input unchanged. Chinese text and ordinary
    /// punctuation are not modified.
    /// [ZH] 需要纯文本时将 emoji 替换为 ASCII 标记；否则原样返回。不会修改中文与
    /// 普通标点。
    /// </summary>
    public static string Prefer(string text) => Enabled || string.IsNullOrEmpty(text) ? text : Strip(text);

    /// <summary>
    /// [EN] Unconditionally replace emoji with ASCII markers for logs, file
    /// writes, and other plain-text-only contexts.
    /// [ZH] 无条件将 emoji 替换为 ASCII 标记，适用于日志、文件写入等仅允许纯文本的场合。
    /// </summary>
    public static string Strip(string text)
    {
        if (string.IsNullOrEmpty(text))
            return text;
        string result = text;
        // [EN] Replace longer sequences first so a variant-selector combination
        // [EN] is not partially consumed, leaving an orphan selector.
        // [ZH] 先替换较长的组合，避免变体选择符组合被部分匹配而留下孤立选择符。
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
    /// [EN] Replace emoji not covered by the map with "?" so missing glyphs do
    /// not appear as boxes.
    /// [ZH] 将映射表未覆盖的 emoji 替换为 "?"，确保界面不出现缺字方块。
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
                // [EN] Variation selectors and zero-width joiners are dropped
                // [EN] without emitting a placeholder.
                // [ZH] 变体选择符与零宽连接符直接丢弃，不输出占位符。
                i += width;
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
