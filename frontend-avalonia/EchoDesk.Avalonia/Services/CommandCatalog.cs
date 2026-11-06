// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Services/CommandCatalog.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Services/CommandCatalog.cs。类型与成员说明见下方注释。
// 指令表（指令面板的数据源）—— 与 WPF 版 frontend/MainWindow.xaml.cs 的 CommandItem 同一套语义。
//
// **为什么单独拆出来**：WPF 版把这张表和"谁在用它的界面逻辑"混在 3000 行的主窗口里；
// 本模块要独立迁移、独立验证，所以数据与界面分开：
//   * 本地回退表（后端没起来时也能看见指令）；
//   * 后端 {"action":"commands"} 返回的**本地化**指令元数据（含技能指令）；
//   * 排序规则（与 WPF 版逐条对齐，避免同一输入在两个前端给不同的候选顺序）。
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace EchoDesk.Avalonia.Services;

/// <summary>一条指令。Format 是参数格式（不含命令名），NeedsArgs 由它推导。</summary>
public sealed record CommandItem(string Command, string Format, string Description, string Short,
                                 bool NeedsArgs, bool Destructive);

public static class CommandCatalog
{
    /// <summary>
    /// 后端不可用时的本地回退**只保留结构**（命令名与是否破坏性），文案一律走语言包。
    ///
    /// 为什么不再把中文写在这张表里（v0.2.2 硬编码清查 §5.1）：回退表的 23 条说明是
    /// **整片中文**，后端慢/不可用/降级运行时英文界面会整屏显示中文指令。而同一批文案
    /// 后端语言包（`cmd.items.*`）本来就有 —— 本轮把它**逐字复制**进前端语言包
    /// （`frontend/Resources/Lang/*.main.json` 的 `cmd` 段），这里只按同一套字段组装，
    /// 与 `ApplyFromBackend` 的取值口径逐字段对齐。
    /// </summary>
    private static readonly (string Command, bool Destructive)[] Spec =
    {
        ("/help", false),
        ("/mode", false),
        ("/agent", false),
        ("/mcp", false),
        ("/task", false),
        ("/preset", false),
        ("/compact", false),
        ("/file", false),
        // /summarize 此前漏在回退表外（WPF 侧 23 条、跨平台版 22 条）：
        // 后端没起来时用户在指令面板里看不到它，而它是"长文档摘要"的唯一手动入口。
        ("/summarize", false),
        ("/search", false),
        ("/model", false),
        ("/clearcache", true),
        ("/prompt", false),
        ("/temp", false),
        ("/darkmode", false),
        ("/stop", false),
        ("/snippet", false),
        ("/status", false),
        ("/clear", true),
        ("/undo", false),
        ("/save", false),
        ("/about", false),
        ("/exit", true),
    };

    /// <summary>从语言包组装一条指令（字段与 <see cref="ApplyFromBackend"/> 完全同一口径）。</summary>
    private static CommandItem? BuildFromPack(string command, bool destructive)
    {
        string desc = Loc.T($"cmd.items.{command}.desc");
        string usage = Loc.T($"cmd.items.{command}.usage");
        // 语言包里没有这条指令（极端情况：包被裁剪）→ 不伪造文案，直接不列出
        if (desc.StartsWith("cmd.items.", StringComparison.Ordinal)
            || usage.StartsWith("cmd.items.", StringComparison.Ordinal))
            return null;
        string format = usage.StartsWith(command, StringComparison.OrdinalIgnoreCase)
            ? usage[command.Length..].Trim()
            : usage.Trim();
        // 后端 usage 形如 "/mode <编号|缩写|全称>"，界面只显示参数部分
        string shortText = desc.Split('\n')[0];
        if (shortText.Length > 40)
            shortText = shortText[..40] + "…";
        return new CommandItem(command, format, desc, shortText, format.Length > 0, destructive);
    }

    /// <summary>按当前语言现算回退表（语言切换后自动跟着变，不需要重建缓存）。</summary>
    private static List<CommandItem> BuildFallback()
    {
        var items = new List<CommandItem>();
        foreach ((string command, bool destructive) in Spec)
        {
            CommandItem? item = BuildFromPack(command, destructive);
            if (item is not null)
                items.Add(item);
        }
        return items;
    }

    private static List<CommandItem>? _fromBackend;

    /// <summary>当前生效的指令表（后端数据优先；没有就现算回退表）。</summary>
    public static List<CommandItem> Items => _fromBackend ?? BuildFallback();

    /// <summary>是否已经用后端数据替换过回退表（界面据此决定要不要再拉一次）。</summary>
    public static bool LoadedFromBackend => _fromBackend is not null;

    /// <summary>把后端 {"action":"commands"} 的数组换成当前指令表（与 WPF 版逐字段对齐）。</summary>
    public static void ApplyFromBackend(JsonElement array)
    {
        if (array.ValueKind != JsonValueKind.Array)
            return;
        var items = new List<CommandItem>();
        foreach (JsonElement element in array.EnumerateArray())
        {
            string name = element.TryGetProperty("name", out JsonElement n) ? n.GetString() ?? "" : "";
            if (name.Length == 0)
                continue;
            string usage = element.TryGetProperty("usage", out JsonElement u) ? u.GetString() ?? name : name;
            string desc = element.TryGetProperty("desc", out JsonElement d) ? d.GetString() ?? "" : "";
            string label = element.TryGetProperty("label", out JsonElement l) ? l.GetString() ?? "" : "";
            bool destructive = element.TryGetProperty("destructive", out JsonElement x)
                               && x.ValueKind == JsonValueKind.True;
            string source = element.TryGetProperty("source", out JsonElement s) ? s.GetString() ?? "" : "";
            // 用法里去掉命令名，只留参数格式（后端 usage 形如 "/mode <编号|缩写|全称>"）。
            string format = usage.StartsWith(name, StringComparison.OrdinalIgnoreCase)
                ? usage[name.Length..].Trim()
                : usage.Trim();
            string shortText = desc.Split('\n')[0];
            if (shortText.Length > 40)
                shortText = shortText[..40] + "…";
            if (source == "skill" && label.Length > 0)
                shortText = $"🧩 {label}";
            items.Add(new CommandItem(name, format, desc, shortText, format.Length > 0, destructive));
        }
        if (items.Count == 0)
            return;
        _fromBackend = items;
    }

    /// <summary>
    /// 候选排序（与 WPF 版 RankCommands 同规则）：
    /// 1. 与输入**完全一致**的排最前；2. 其余按命令名长度升序（越短说明前缀占比越高）；
    /// 3. 最后按指令表原有顺序，保证结果稳定可预期。
    /// </summary>
    public static List<CommandItem> Rank(string text)
    {
        string query = (text ?? "").Trim();
        string key = query.Length > 1 ? query[1..] : "";
        if (query.Length == 0 || !query.StartsWith('/'))
            return new List<CommandItem>();
        return Items
            .Select((command, index) => (Command: command, Index: index))
            .Where(x => key.Length == 0
                        || x.Command.Command.StartsWith("/" + key, StringComparison.OrdinalIgnoreCase))
            .OrderByDescending(x => string.Equals(x.Command.Command, query, StringComparison.OrdinalIgnoreCase))
            .ThenBy(x => x.Command.Command.Length)
            .ThenBy(x => x.Index)
            .Select(x => x.Command)
            .ToList();
    }
}
