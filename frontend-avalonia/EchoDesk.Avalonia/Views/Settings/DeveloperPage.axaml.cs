// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/DeveloperPage.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/DeveloperPage.axaml.cs。类型与成员说明见下方注释。
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
using Avalonia;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
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
            // v0.2.2 第 4 批 3a：**作用域改为 scope=dev**（与 WPF 的 DevReloadButton_Click 一致）。
            // 开发者页的「重载」要的是"把我刚写的开发插件重读一遍"，
            // 全量重载会把正式安装的插件也一起卸载重挂 —— 那既慢又不必要。
            await SendAsync(new { action = "skills_reload", scope = "dev" });
            await RefreshAsync();
            DevStatusText.Text = Loc.T("settings.developer.reloaded", "✅ 本地插件已重载");
        };
        InstallExamplesButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_examples" });
            await RefreshAsync();
            DevStatusText.Text = Loc.T("settings.developer.examplesInstalled", "✅ 示例插件已安装到 skills_dev/");
        };
        // ---- v0.2.2 第 4 批 3a：三个目录按钮 + 导出日志 + 清单编辑 + 面板动作 ----
        OpenDevDirButton.Click += (_, _) => OpenSkillDir("dev");
        OpenInstallDirButton.Click += (_, _) => OpenSkillDir("installed");
        OpenExamplesDirButton.Click += (_, _) => OpenSkillDir("examples");
        ExportDevLogButton.Click += async (_, _) => await ExportLogAsync();
        ManifestAddRow.Click += (_, _) =>
        {
            _manifestRows.Add(new ManifestRow());
            RenderManifestRows();
        };
        ManifestSaveButton.Click += async (_, _) => await SaveManifestAsync();
        ManifestOpenRawButton.Click += (_, _) => OpenManifestRaw();
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
        // v0.2.2 第 4 批 3a：清单、钩子与面板这三块都来自同一份 skills_list 状态
        // （清单要另外发 skills_manifest_get）。
        RenderHooks();
        RenderPanels();
        await LoadManifestAsync();
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

    // =================================================================
    // v0.2.2 第 4 批 3a：目录按钮 / 导出日志 / 清单编辑 / 钩子与面板
    // =================================================================

    /// <summary>取状态里的某个目录（状态还没加载时返回空串，不抛异常）。</summary>
    private string SkillDir(string name)
    {
        if (_status is JsonElement status
            && status.TryGetProperty("dirs", out JsonElement dirs)
            && dirs.ValueKind == JsonValueKind.Object)
        {
            return GetString(dirs, name);
        }
        return "";
    }

    private void OpenSkillDir(string name)
    {
        string dir = SkillDir(name);
        if (!ShellOpen.TryOpenDirectory(dir))
            DevStatusText.Text = Loc.T("settings.developer.openDirFailed", "⚠️ 无法打开目录：{0}", dir);
    }

    /// <summary>导出开发日志（`skills_export_log`）。路径由系统保存对话框给出（WPF 用 SaveFileDialog）。</summary>
    private async Task ExportLogAsync()
    {
        TopLevel? top = TopLevel.GetTopLevel(this);
        if (top is null)
            return;
        var file = await top.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
        {
            Title = Loc.T("settings.developer.exportLog", "导出日志"),
            SuggestedFileName = "skills_dev.log",
            DefaultExtension = "log",
            FileTypeChoices = new[]
            {
                new FilePickerFileType(Loc.T("settings.developer.logFilterDesc", "日志文件"))
                {
                    Patterns = new[] { "*.log", "*" },
                },
            },
        });
        string? path = file?.TryGetLocalPath();
        if (string.IsNullOrEmpty(path))
            return;
        using JsonDocument? doc = await SendAsync(new { action = "skills_export_log", path });
        DevStatusText.Text = doc is null
            ? Loc.T("settings.installed.backendGone", "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）")
            : GetString(doc.RootElement, "content");
    }

    // ---- 本地插件清单（表格编辑） ----

    /// <summary>清单里的一行（可编辑）。字段与 WPF 的 ManifestRow 逐一对齐。</summary>
    private sealed class ManifestRow
    {
        public string Id { get; set; } = "";
        public string Path { get; set; } = "";
        public string Alias { get; set; } = "";
        public bool Enabled { get; set; } = true;
        public int Order { get; set; }
        public string Note { get; set; } = "";
    }

    private readonly List<ManifestRow> _manifestRows = new();

    private async Task LoadManifestAsync()
    {
        using JsonDocument? doc = await SendAsync(new { action = "skills_manifest_get" });
        if (doc is null || !doc.RootElement.TryGetProperty("manifest", out JsonElement manifest))
            return;
        _manifestRows.Clear();
        if (manifest.TryGetProperty("plugins", out JsonElement plugins) && plugins.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in plugins.EnumerateArray())
            {
                _manifestRows.Add(new ManifestRow
                {
                    Id = GetString(item, "id"),
                    Path = GetString(item, "path"),
                    Alias = GetString(item, "alias"),
                    Enabled = !item.TryGetProperty("enabled", out JsonElement en) || en.ValueKind != JsonValueKind.False,
                    Order = GetInt(item, "order"),
                    Note = GetString(item, "note"),
                });
            }
        }
        RenderManifestRows();
    }

    /// <summary>
    /// 重排清单行。每行是"id / 路径 / 别名 / 顺序 / 启用 / 删除"一组控件，
    /// 字段与 WPF 的 RenderManifestRows 逐一对齐（含 ✕ 删除按钮）。
    /// </summary>
    private void RenderManifestRows()
    {
        ManifestList.Children.Clear();
        if (_manifestRows.Count == 0)
        {
            ManifestList.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.developer.manifestEmpty", "（清单为空：点「＋ 加一行」登记一个本地插件）"),
                FontSize = 11, Opacity = 0.7, TextWrapping = TextWrapping.Wrap,
            });
            return;
        }
        foreach (ManifestRow row in _manifestRows)
        {
            var grid = new Grid
            {
                ColumnDefinitions = new ColumnDefinitions("130,*,Auto,Auto,Auto,Auto,Auto"),
                Margin = new Thickness(0, 2),
            };

            var idBox = new TextBox { Text = row.Id, Padding = new Thickness(6, 3), FontSize = 11 };
            AutomationProperties.SetAutomationId(idBox, "ManifestId_" + row.Id);
            idBox.TextChanged += (_, _) => row.Id = idBox.Text ?? "";
            Grid.SetColumn(idBox, 0);

            var pathBox = new TextBox
            {
                Text = row.Path, Padding = new Thickness(6, 3), FontSize = 11,
                Margin = new Thickness(6, 0, 0, 0),
            };
            pathBox.TextChanged += (_, _) => row.Path = pathBox.Text ?? "";
            Grid.SetColumn(pathBox, 1);

            var aliasBox = new TextBox
            {
                Text = row.Alias, Width = 110, Padding = new Thickness(6, 3), FontSize = 11,
                Margin = new Thickness(6, 0, 0, 0),
            };
            aliasBox.TextChanged += (_, _) => row.Alias = aliasBox.Text ?? "";
            Grid.SetColumn(aliasBox, 2);

            var orderBox = new TextBox
            {
                Text = row.Order.ToString(), Width = 46, Padding = new Thickness(6, 3), FontSize = 11,
                Margin = new Thickness(6, 0, 0, 0),
            };
            orderBox.TextChanged += (_, _) =>
            {
                if (int.TryParse(orderBox.Text, out int order))
                    row.Order = order;
            };
            Grid.SetColumn(orderBox, 3);

            var check = new CheckBox
            {
                IsChecked = row.Enabled,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(8, 0, 0, 0),
            };
            ToolTip.SetTip(check, Loc.T("settings.developer.manifestEnabled", "启用"));
            check.IsCheckedChanged += (_, _) => row.Enabled = check.IsChecked == true;
            Grid.SetColumn(check, 4);

            var remove = new Button
            {
                Content = "✕",
                Padding = new Thickness(6, 1),
                FontSize = 11,
                Margin = new Thickness(8, 0, 0, 0),
            };
            ToolTip.SetTip(remove, Loc.T("settings.button.remove", "移除"));
            remove.Click += (_, _) =>
            {
                _manifestRows.Remove(row);
                RenderManifestRows();
            };
            Grid.SetColumn(remove, 5);

            grid.Children.Add(idBox);
            grid.Children.Add(pathBox);
            grid.Children.Add(aliasBox);
            grid.Children.Add(orderBox);
            grid.Children.Add(check);
            grid.Children.Add(remove);
            ManifestList.Children.Add(grid);
        }
    }

    /// <summary>
    /// 保存清单（`skills_manifest_set`）。只提交 id 与 path 都非空的行 ——
    /// 与 WPF 的过滤条件一字不差（空行是"加了一行还没填"，不该被写进配置）。
    /// </summary>
    private async Task SaveManifestAsync()
    {
        var plugins = new List<object>();
        var trusted = new List<string>();
        foreach (ManifestRow row in _manifestRows)
        {
            string id = row.Id.Trim();
            string path = row.Path.Trim();
            if (id.Length > 0 && path.Length > 0)
            {
                plugins.Add(new
                {
                    id,
                    path,
                    enabled = row.Enabled,
                    source = "local",
                    order = row.Order,
                    alias = row.Alias,
                    note = row.Note,
                });
            }
            if (id.Length > 0)
                trusted.Add(id);
        }
        await SendAsync(new { action = "skills_manifest_set", manifest = new { plugins, trusted } });
        DevStatusText.Text = Loc.T("settings.developer.manifestSaved", "✅ 本地插件清单已保存");
        await RefreshAsync();
    }

    /// <summary>直接打开清单 JSON 原文（文件存在时用系统默认程序；否则打开它所在目录）。</summary>
    private void OpenManifestRaw()
    {
        string path = SkillDir("manifest");
        if (path.Length == 0)
        {
            DevStatusText.Text = Loc.T("settings.developer.openDirFailed", "⚠️ 无法打开目录：{0}", path);
            return;
        }
        try
        {
            if (System.IO.File.Exists(path))
            {
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                {
                    FileName = path,
                    UseShellExecute = true,
                });
                return;
            }
        }
        catch (Exception)
        {
            // 没有关联程序：退回到打开所在目录
        }
        ShellOpen.TryOpenDirectory(System.IO.Path.GetDirectoryName(path));
    }

    // ---- 自动化钩子清单 ----

    private void RenderHooks()
    {
        if (_status is not JsonElement status
            || !status.TryGetProperty("hooks", out JsonElement hooks)
            || hooks.ValueKind != JsonValueKind.Object)
        {
            HooksText.Text = Loc.T("settings.developer.hooksEmpty", "（没有插件挂接自动化钩子）");
            return;
        }
        var lines = new List<string>();
        foreach (JsonProperty property in hooks.EnumerateObject())
        {
            var skills = new List<string>();
            if (property.Value.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement entry in property.Value.EnumerateArray())
                    skills.Add(entry.ToString());
            }
            if (skills.Count == 0)
                continue;
            // 阶段名走语言包（`settings.developer.hookStages.<阶段>`），缺键时回落成阶段名本身
            string stageName = Loc.T("settings.developer.hookStages." + property.Name, property.Name);
            lines.Add($"{stageName}: {string.Join(", ", skills)}");
        }
        HooksText.Text = lines.Count > 0
            ? string.Join(Environment.NewLine, lines)
            : Loc.T("settings.developer.hooksEmpty", "（没有插件挂接自动化钩子）");
    }

    // ---- 插件面板（声明式 UI + 面板动作） ----

    /// <summary>
    /// 渲染插件声明的面板：字段用原生控件生成（文本框 / 复选框），动作按钮发
    /// `skills_panel_action`。与 WPF 的 RenderPanels 同一设计（技能不需要碰任何 UI 代码）。
    /// </summary>
    private void RenderPanels()
    {
        PanelList.Children.Clear();
        if (_status is not JsonElement status
            || !status.TryGetProperty("panels", out JsonElement panels)
            || panels.ValueKind != JsonValueKind.Array
            || panels.GetArrayLength() == 0)
        {
            PanelList.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.developer.panelsEmpty", "（没有插件提供面板）"),
                FontSize = 11, Opacity = 0.7, TextWrapping = TextWrapping.Wrap,
            });
            return;
        }

        foreach (JsonElement panel in panels.EnumerateArray())
        {
            string skillId = GetString(panel, "skill_id");
            string panelId = GetString(panel, "id");
            var stack = new StackPanel { Spacing = 4 };
            stack.Children.Add(new TextBlock
            {
                Text = GetString(panel, "title"),
                FontWeight = FontWeight.Bold,
                FontSize = 12,
                Foreground = TryBrush("AccentBrush"),
            });
            string description = GetString(panel, "description");
            if (description.Length > 0)
                stack.Children.Add(new TextBlock
                {
                    Text = description, FontSize = 11, Opacity = 0.75, TextWrapping = TextWrapping.Wrap,
                });

            // 取值表：面板动作要把它整份发给后端（与 WPF 的 values 字典同一用途）
            var values = new Dictionary<string, string>(StringComparer.Ordinal);
            if (panel.TryGetProperty("fields", out JsonElement fields) && fields.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement field in fields.EnumerateArray())
                {
                    string key = GetString(field, "key");
                    string type = GetString(field, "type");
                    string label = GetString(field, "label");
                    string help = GetString(field, "help");
                    string defaultValue = GetString(field, "default");
                    if (type == "checkbox")
                    {
                        bool initial = defaultValue is "1" or "true" or "True";
                        values[key] = initial ? "1" : "0";
                        var box = new CheckBox { Content = label, IsChecked = initial, FontSize = 11 };
                        ToolTip.SetTip(box, help);
                        box.IsCheckedChanged += (_, _) => values[key] = box.IsChecked == true ? "1" : "0";
                        stack.Children.Add(box);
                    }
                    else
                    {
                        values[key] = defaultValue;
                        stack.Children.Add(new TextBlock { Text = label, FontSize = 11, Opacity = 0.75 });
                        var entry = new TextBox { Text = defaultValue, Padding = new Thickness(6, 4) };
                        ToolTip.SetTip(entry, help);
                        entry.TextChanged += (_, _) => values[key] = entry.Text ?? "";
                        stack.Children.Add(entry);
                    }
                }
            }

            if (panel.TryGetProperty("actions", out JsonElement actions)
                && actions.ValueKind == JsonValueKind.Array)
            {
                var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
                foreach (JsonElement action in actions.EnumerateArray())
                {
                    string actionKey = GetString(action, "key");
                    var button = new Button
                    {
                        Content = GetString(action, "label"),
                        Padding = new Thickness(12, 4),
                        FontSize = 11,
                    };
                    button.Click += async (_, _) => await RunPanelActionAsync(skillId, panelId, actionKey, values);
                    row.Children.Add(button);
                }
                stack.Children.Add(row);
            }

            PanelList.Children.Add(new Border
            {
                Background = TryBrush("CardBackgroundBrush"),
                BorderBrush = TryBrush("CardBorderBrush"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(12, 10),
                Child = stack,
            });
        }
    }

    /// <summary>面板动作：把当前取值整份发给后端，返回的文本显示在日志框（与 WPF 一致）。</summary>
    private async Task RunPanelActionAsync(string skillId, string panelId, string actionKey,
                                           Dictionary<string, string> values)
    {
        var payload = new Dictionary<string, object?>();
        foreach (KeyValuePair<string, string> pair in values)
            payload[pair.Key] = pair.Value;
        using JsonDocument? doc = await SendAsync(new
        {
            action = "skills_panel_action",
            skill_id = skillId,
            panel_id = panelId,
            panel_action = actionKey,
            values = payload,
        });
        DevLogBox.Text = doc is null
            ? Loc.T("settings.installed.backendGone", "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）")
            : GetString(doc.RootElement, "content");
    }

    private static Brush? TryBrush(string key)
    {
        if (Application.Current is null)
            return null;
        if (Application.Current.TryFindResource(key, out object? value) && value is Brush brush)
            return brush;
        return null;
    }

    /// <summary>供「已安装」页跳转过来时聚焦插件 ID 输入框（对齐 WPF 的 ScaffoldIdBox.Focus()）。</summary>
    public void FocusScaffoldId() => ScaffoldIdBox.Focus();

    // -----------------------------------------------------------------
    // 探针接口（`EchoDesk --probe pages` 用：开发者页的三块新增功能）
    // -----------------------------------------------------------------

    /// <summary>开发者页的可观测状态。</summary>
    public string DeveloperSnapshot()
    {
        return string.Join("; ", new[]
        {
            $"dirButtons={(string.IsNullOrEmpty(OpenDevDirButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(OpenInstallDirButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(OpenExamplesDirButton.Content as string) ? 0 : 1)}",
            $"exportLogButton={(string.IsNullOrEmpty(ExportDevLogButton.Content as string) ? 0 : 1)}",
            $"reloadIsDevOnly={((DevReloadButton.Content as string)?.Length > 0 ? 1 : 0)}",
            $"manifestRows={_manifestRows.Count}",
            $"manifestNodes={ManifestList.Children.Count}",
            $"manifestButtons={(string.IsNullOrEmpty(ManifestAddRow.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(ManifestSaveButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(ManifestOpenRawButton.Content as string) ? 0 : 1)}",
            $"hooksText={(string.IsNullOrEmpty(HooksText.Text) ? 0 : 1)}",
            $"panelNodes={PanelList.Children.Count}",
            $"dirsTextHasDev={(DevDirsText.Text?.Contains("skills_dev") == true ? 1 : 0)}",
            $"status={(string.IsNullOrEmpty(DevStatusText.Text) ? 0 : 1)}",
        });
    }

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
        DevReloadButton.Content = Loc.T("settings.developer.reloadDev", "只重载开发插件");
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
        // v0.2.2 第 4 批 3a：三个目录按钮、导出日志、清单与钩子/面板区块
        OpenDevDirButton.Content = Loc.T("settings.developer.openDevDir", "📂 开发目录");
        OpenInstallDirButton.Content = Loc.T("settings.developer.openInstallDir", "📂 安装目录");
        OpenExamplesDirButton.Content = Loc.T("settings.developer.openExamplesDir", "📂 示例插件");
        ExportDevLogButton.Content = Loc.T("settings.developer.exportLog", "导出日志");
        ManifestSectionLabel.Text = Loc.T("settings.developer.sectionManifest", "本地插件清单");
        ManifestNoticeText.Text = Loc.T("settings.developer.manifestTip",
            "登记本地插件（任意路径、别名、启用状态、加载顺序）。保存后点上面的「重载本地插件」生效；"
            + "「直接编辑 JSON」会用系统默认程序打开清单文件。");
        ManifestAddRow.Content = Loc.T("settings.developer.manifestAddRow", "＋ 加一行");
        ManifestSaveButton.Content = Loc.T("settings.developer.manifestSave", "保存清单");
        ManifestOpenRawButton.Content = Loc.T("settings.developer.manifestOpenRaw", "直接编辑 JSON");
        HooksSectionLabel.Text = Loc.T("settings.developer.sectionHooks", "已挂接的自动化钩子");
        PanelsSectionLabel.Text = Loc.T("settings.developer.sectionPanels", "插件面板");
        // 已加载的数据要按新语言重排一遍（不重新请求后端之外的东西）。
        if (_status is not null)
            _ = RefreshAsync();
    }
}

