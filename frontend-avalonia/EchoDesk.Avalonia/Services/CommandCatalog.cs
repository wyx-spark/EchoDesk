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
    /// 后端不可用时的本地回退。**刻意只有"读得到"的部分**：完整说明由后端语言包提供，
    /// 这里只保证"后端没起来时指令面板不是空的"。
    /// </summary>
    private static readonly CommandItem[] Fallback =
    {
        new("/help", "", "显示指令介绍", "显示全部指令及用法", false, false),
        new("/mode", "<编号|缩写|全称>", "切换对话模式", "切换对话模式", true, false),
        new("/agent", "[list|allow|deny|steps|on|off]", "查看 Agent 工具与权限", "查看 Agent 工具与权限", true, false),
        new("/mcp", "[list|enable|disable|connect]", "查看 MCP 服务器与工具", "查看 MCP 服务器与工具", true, false),
        new("/task", "[list|<模板名>|off]", "套用任务提示词模板", "套用任务提示词模板", true, false),
        new("/preset", "[list|light|balanced|quality]", "切换生成参数预设", "切换生成参数预设", true, false),
        new("/compact", "", "压缩上下文", "压缩上下文", false, false),
        new("/file", "<路径>", "上传本地文件", "上传文件加入上下文", true, false),
        // /summarize 此前漏在回退表外（WPF 侧 23 条、跨平台版 22 条）：
        // 后端没起来时用户在指令面板里看不到它，而它是"长文档摘要"的唯一手动入口。
        new("/summarize", "", "对当前已加载的长文档手动触发全文分段摘要（逐段摘要后合并），摘要进度实时显示在对话框中", "手动触发全文摘要", false, false),
        new("/search", "on|off", "开启/关闭联网搜索", "开启/关闭联网搜索", true, false),
        new("/model", "[名称|别名|list]", "查看/切换本地模型", "查看/切换本地模型", true, false),
        new("/clearcache", "", "清空推理 KV 缓存", "清空推理 KV 缓存", false, true),
        new("/prompt", "<文本>|reset", "设置自定义系统提示词", "设置自定义系统提示词", true, false),
        new("/temp", "<0.0~1.0>|reset", "调整生成温度", "调整生成温度", true, false),
        new("/darkmode", "on|off", "切换深/浅色主题", "切换深/浅色主题", true, false),
        new("/stop", "", "停止当前生成", "停止当前生成", false, false),
        new("/snippet", "add|remove|list …", "管理常用语模板", "管理常用语模板", true, false),
        new("/status", "", "查看当前状态与能力", "查看当前状态与能力", false, false),
        new("/clear", "", "清空对话历史与文件上下文", "清空对话重新开始", false, true),
        new("/undo", "", "撤回最近一轮问答", "撤回最近一轮问答", false, false),
        new("/save", "<文件名>", "导出对话为 Markdown", "导出对话为 Markdown", true, false),
        new("/about", "", "关于 EchoDesk", "关于 EchoDesk", false, false),
        new("/exit", "", "退出程序", "退出程序", false, true),
    };

    /// <summary>当前生效的指令表。</summary>
    public static List<CommandItem> Items { get; private set; } = Fallback.ToList();

    /// <summary>是否已经用后端数据替换过回退表（界面据此决定要不要再拉一次）。</summary>
    public static bool LoadedFromBackend { get; private set; }

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
        Items = items;
        LoadedFromBackend = true;
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
