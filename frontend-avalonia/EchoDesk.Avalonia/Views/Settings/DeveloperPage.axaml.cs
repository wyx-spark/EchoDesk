// 开发者页的交互逻辑（v0.2.1 跨平台-2 任务一，第 5 个迁移模块）。
//
// 数据来自后端 `skills_list`，其中与开发相关的字段（与 WPF 版一致）：
//     dev_mode           开发者模式当前是否生效
//     dev_mode_explicit  是"用户显式设置"还是"自动检测"决定的
//     dev_detected       skills_dev 目录里是否有插件
//     dev_count          skills_dev 里的插件数
//     dev_enabled        [已启用的开发插件 id]
//     api_version        技能 API 版本
//     dirs               { dev, installed, manifest, registry, log, examples }
//
// 动作：skills_set_dev_mode / skills_log / skills_clear_log / skills_examples / skills_reload /
//       skills_scaffold
//
// **为什么把"由谁决定"显示出来**（WPF 版修复一的做法，这里照做）：开发者模式既可能由用户
// 显式打开，也可能因为 skills_dev 里有插件而自动生效。只显示"开/关"会让用户困惑
// （"我没开它，为什么是开的？"），所以状态行同时说明依据。
//
// **本轮未迁移**：本地插件清单（skills_manifest_get / skills_manifest_set）的可编辑表格。
// 页面底部给出明确说明与替代做法（直接编辑 JSON 文件），而不是显示一个不能用的空表格。
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Layout;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class DeveloperPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;
    private JsonElement? _status;
    private bool _syncing;

    /// <summary>日志行数选项（与 WPF 版同口径）。</summary>
    private static readonly int[] LogLineOptions = { 100, 200, 500, 1000 };

    public DeveloperPage()
    {
        InitializeComponent();
        LogLinesCombo.ItemsSource = new List<int>(LogLineOptions);
        LogLinesCombo.SelectedIndex = 1;

        DevModeCheck.IsCheckedChanged += async (_, _) =>
        {
            if (_syncing || _host is null)
                return;
            bool enabled = DevModeCheck.IsChecked == true;
            await SendAsync(new { action = "skills_set_dev_mode", enabled });
            await RefreshAsync();
        };
        DevReloadButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_reload" });
            await RefreshAsync();
            DevStatusText.Text = Loc.T("settings.developer.reloaded", "✅ 本地插件已重载");
        };
        InstallExamplesButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_examples" });
            await RefreshAsync();
            DevStatusText.Text = Loc.T("settings.developer.examplesInstalled", "✅ 示例插件已安装到 skills_dev/");
        };
        RefreshDevLogButton.Click += async (_, _) => await RefreshLogAsync();
        ClearDevLogButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_clear_log" });
            await RefreshLogAsync();
            DevStatusText.Text = Loc.T("settings.developer.logCleared", "🗑 日志已清空");
        };
        LogLinesCombo.SelectionChanged += async (_, _) =>
        {
            if (!_syncing)
                await RefreshLogAsync();
        };
        ScaffoldCreateButton.Click += async (_, _) => await ScaffoldAsync();
    }

    public string TabKey => "settings.tab.developer";
    public string TabFallback => "开发者";
    public string AutomationId => "SettingsTabDeveloper";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }
    public Task ApplyToBackendAsync() => Task.CompletedTask;

    // -----------------------------------------------------------------
    private async Task<JsonDocument?> SendAsync(object request)
    {
        if (_host is null)
            return null;
        return await _host.State.Backend.SendAsync(request, idleTimeout: TimeSpan.FromSeconds(60));
    }

    private int CurrentLogLines() =>
        LogLinesCombo.SelectedItem is int n ? n : 200;

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        using JsonDocument? doc = await SendAsync(new { action = "skills_list" });
        if (doc is null)
        {
            DevStatusText.Text = Loc.T("settings.installed.backendGone",
                "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）");
            return;
        }
        if (!doc.RootElement.TryGetProperty("skills", out JsonElement status))
            return;
        _status = status.Clone();

        // 开发者模式开关：**同步时抑制事件**，否则会立刻把"回填"当成"用户改动"再发一次设置。
        bool devMode = GetBool(status, "dev_mode");
        bool explicitSet = GetBool(status, "dev_mode_explicit");
        bool detected = GetBool(status, "dev_detected");
        int devCount = GetInt(status, "dev_count");

        _syncing = true;
        DevModeCheck.IsChecked = devMode;
        _syncing = false;

        string how = explicitSet
            ? Loc.T("settings.developer.modeExplicit", "由你的设置决定")
            : Loc.T("settings.developer.modeAuto", "自动：检测到 skills_dev 中有 {0} 个插件", devCount);
        DevModeStatusText.Text = (devMode
                ? Loc.T("settings.developer.modeOn", "开发者模式：已开启")
                : Loc.T("settings.developer.modeOff", "开发者模式：已关闭"))
            + " · " + how;

        // 已启用的开发插件清单：关闭后应为空，用户据此确认"确实关掉了"。
        var enabled = new List<string>();
        if (status.TryGetProperty("dev_enabled", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement entry in arr.EnumerateArray())
            {
                if (entry.ValueKind == JsonValueKind.String)
                    enabled.Add(entry.GetString() ?? "");
            }
        }
        if (enabled.Count == 0)
        {
            DevPluginListText.Text = detected
                ? Loc.T("settings.developer.pluginsDisabled",
                    "skills_dev 目录中有插件，但开发者模式已关闭，当前未加载")
                : Loc.T("settings.developer.pluginsEmpty", "当前没有已启用的开发插件");
        }
        else
        {
            var lines = new List<string>
            {
                Loc.T("settings.developer.pluginsHeader", "已启用的开发插件（{0} 个）：", enabled.Count),
            };
            foreach (string id in enabled)
                lines.Add("  • " + id);
            DevPluginListText.Text = string.Join(Environment.NewLine, lines);
        }

        DevApiText.Text = Loc.T("settings.developer.apiVersion", "技能 API 版本：{0}",
            GetString(status, "api_version"));

        if (status.TryGetProperty("dirs", out JsonElement dirs))
        {
            DevDirsText.Text = string.Join(Environment.NewLine, new[]
            {
                $"{Loc.T("settings.developer.devDir", "开发插件目录（skills_dev）")}：{GetString(dirs, "dev")}",
                $"{Loc.T("settings.developer.installDir", "正式安装目录（skills）")}：{GetString(dirs, "installed")}",
                $"{Loc.T("settings.developer.manifestFile", "本地插件清单（skills_local.json）")}：{GetString(dirs, "manifest")}",
                $"{Loc.T("settings.developer.registryFile", "插件注册表（skills_registry.json）")}：{GetString(dirs, "registry")}",
                $"{Loc.T("settings.developer.logFile", "开发日志（skills_dev.log）")}：{GetString(dirs, "log")}",
                $"{Loc.T("settings.developer.examplesDir", "示例插件（examples/skills）")}：{GetString(dirs, "examples")}",
            });
        }

        await RefreshLogAsync();
    }

    private async Task RefreshLogAsync()
    {
        if (_host is null)
            return;
        using JsonDocument? doc = await SendAsync(new { action = "skills_log", lines = CurrentLogLines() });
        if (doc is null)
            return;
        string log = "";
        if (doc.RootElement.TryGetProperty("log", out JsonElement l) && l.ValueKind == JsonValueKind.String)
            log = l.GetString() ?? "";
        DevLogBox.Text = log.Length > 0
            ? log
            : Loc.T("settings.developer.logEmpty", "（日志为空）");
    }

    /// <summary>
    /// 新建插件骨架。校验只做"必填 + 基本格式"——真正的规则在后端（它会再校验一次），
    /// 前端校验只为给出更快的反馈。
    /// </summary>
    private async Task ScaffoldAsync()
    {
        if (_host is null)
            return;
        string id = ScaffoldIdBox.Text?.Trim() ?? "";
        string name = ScaffoldNameBox.Text?.Trim() ?? "";
        string author = ScaffoldAuthorBox.Text?.Trim() ?? "";
        if (id.Length == 0)
        {
            DevStatusText.Text = Loc.T("settings.developer.scaffoldIdRequired", "⚠️ 请先填写插件 ID");
            return;
        }
        DevStatusText.Text = Loc.T("settings.developer.scaffoldRunning", "⏳ 正在创建插件骨架…");
        using JsonDocument? doc = await SendAsync(new { action = "skills_scaffold", id, name, author });
        if (doc is null)
        {
            DevStatusText.Text = Loc.T("settings.developer.scaffoldFailed", "❌ 创建失败（后端未响应）");
            return;
        }
        string content = "";
        if (doc.RootElement.TryGetProperty("content", out JsonElement c) && c.ValueKind == JsonValueKind.String)
            content = c.GetString() ?? "";
        DevStatusText.Text = content.Length > 0
            ? content
            : Loc.T("settings.installed.scaffoldOk", "✅ 已创建插件骨架：{0}", id);
        ScaffoldIdBox.Text = "";
        await RefreshAsync();
    }

    private static string GetString(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? ""
            : "";

    private static bool GetBool(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.True;

    private static int GetInt(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number
            ? el.GetInt32()
            : 0;

    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        DevModeSectionLabel.Text = Loc.T("settings.developer.sectionMode", "开发者模式");
        DevModeCheck.Content = Loc.T("settings.developer.mode", "启用开发者模式");
        DevModeShortText.Text = Loc.T("settings.developer.modeShort",
            "放宽的是验证严格度，不是权限边界。");
        DevDirsSectionLabel.Text = Loc.T("settings.developer.sectionDirs", "插件目录");
        InstallExamplesButton.Content = Loc.T("settings.developer.installExamples", "📦 安装示例插件");
        DevReloadSectionLabel.Text = Loc.T("settings.developer.sectionReload", "重载与日志");
        DevReloadButton.Content = Loc.T("settings.developer.reload", "🔄 重载本地插件");
        ClearDevLogButton.Content = Loc.T("settings.developer.clearLog", "🗑 清空日志");
        RefreshDevLogButton.Content = Loc.T("settings.installed.refresh", "🔄 刷新");
        LogLinesLabel.Text = Loc.T("settings.developer.logLines", "显示最后");
        LogLinesUnitText.Text = Loc.T("settings.developer.logLinesUnit", "行");
        ScaffoldSectionLabel.Text = Loc.T("settings.developer.sectionScaffold", "新建本地插件");
        ScaffoldTipText.Text = Loc.T("settings.developer.scaffoldTip",
            "生成符合规范的最小插件骨架，放进 skills_dev/ 即可加载。");
        ScaffoldIdLabel.Text = Loc.T("settings.installed.scaffoldId", "插件 ID");
        ScaffoldNameLabel.Text = Loc.T("settings.installed.scaffoldName", "插件显示名");
        ScaffoldAuthorLabel.Text = Loc.T("settings.installed.scaffoldAuthor", "作者");
        ScaffoldCreateButton.Content = Loc.T("settings.developer.scaffoldCreate", "➕ 创建");
        ManifestNoticeText.Text = Loc.T("settings.developer.manifestNotMigrated",
            "⏳ 「本地插件清单」的表格编辑还没有迁移到跨平台前端。"
            + "替代做法：直接编辑上面列出的 skills_local.json（它支持任意路径、别名、启用状态、"
            + "加载顺序与信任状态），保存后点「重载本地插件」生效。");
        // 已加载的数据要按新语言重排一遍（不重新请求后端之外的东西）。
        if (_status is not null)
            _ = RefreshAsync();
    }
}
