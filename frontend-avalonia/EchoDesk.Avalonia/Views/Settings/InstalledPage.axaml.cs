// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/InstalledPage.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/InstalledPage.axaml.cs。类型与成员说明见下方注释。
// [EN] Interaction logic of the installed page (v0.2.1 cross-platform batch 2,
// [EN] development task 1; the third migrated module; v0.2.2 batch 4 3a completes the
// [EN] full plugin ecosystem).
// [ZH] 已安装页的交互逻辑（v0.2.1 跨平台-2 任务一，第 3 个迁移模块；
// [ZH] v0.2.2 第 4 批 3a 补齐插件生态全套）。
// [EN]
// [EN] The data comes from the backend `skills_list`; the response shape (consistent
// [EN] with the WPF version):
// [ZH] 数据来自后端 `skills_list`，响应形状（与 WPF 版一致）：
// [EN]     { "skills": { "skills": [ { id, name, version, source, loaded, enabled, error,
// [EN]                             overrode, path, dev, trust_pending,
// [EN]                             commands[], parsers[], hooks[], panels[], permissions{} } ],
// [EN]                  "counts": {...}, "api_version": "..." } }
// [ZH]     { "skills": { "skills": [ { id, name, version, source, loaded, enabled, error,
// [ZH]                             overrode, path, dev, trust_pending,
// [ZH]                             commands[], parsers[], hooks[], panels[], permissions{} } ],
// [ZH]                  "counts": {...}, "api_version": "…" } }
// [EN]
// [EN] The only differences from the WPF version are the "presentation"; the behavior
// [EN] matches item by item:
// [ZH] 与 WPF 版的差异只有“承载方式”，行为逐条对齐：
// [EN]   * **Uninstall/trust confirmation has no modal dialog**: Avalonia has no built-in
// [EN]     MessageBox. Uninstall becomes "the button turns into 'Confirm uninstall?' in
// [EN]     place, and a second click within 3 seconds performs it"; the trust confirmation
// [EN]     for newly loaded plugins of unknown origin becomes **an in-page panel** (list
// [EN]     all at once, check each one, submit the batch) — the same semantics as the WPF
// [EN]     TrustDialog (ask once, decide as a batch).
// [ZH]   * **卸载/信任确认没有模态框**：Avalonia 没有内置 MessageBox。卸载改成
// [ZH]     “按钮就地变成『确认卸载？』、3 秒内再点一次才执行”；首次加载未知来源插件
// [ZH]     的信任确认改成**页内一块面板**（一次性列出全部、逐条勾选、整批提交）——
// [ZH]     与 WPF 的 TrustDialog 同一语义（只问一次、整批决定）。
// [EN]   * File dialogs use the Avalonia `StorageProvider` (WPF uses `OpenFolderDialog` /
// [EN]     `SaveFileDialog`).
// [ZH]   * 文件对话框走 Avalonia 的 `StorageProvider`（WPF 是 `OpenFolderDialog`/`SaveFileDialog`）。
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.VisualTree;
using Avalonia.Threading;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class InstalledPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;

    /// <summary>
    /// [EN] Raw response of the most recent skills_list, used repeatedly when rendering
    /// [EN] cards.
    /// [ZH] 最近一次 skills_list 的原始响应，渲染卡片时反复用到。
    /// </summary>
    private JsonElement? _status;

    /// <summary>
    /// [EN] Id of the plugin awaiting the second uninstall confirmation (see the file
    /// [EN] header).
    /// [ZH] 等待二次确认卸载的插件 id（见文件头注释）。
    /// </summary>
    private string _pendingUninstallId = "";
    private DispatcherTimer? _pendingTimer;

    /// <summary>
    /// [EN] Whether the trust confirmation has already been asked in this window session
    /// [EN] (the same convention as _trustDialogHandled in WPF).
    /// [ZH] 本次窗口会话里是否已经问过信任确认（与 WPF 的 _trustDialogHandled 同口径）。
    /// </summary>
    private bool _trustHandled;

    /// <summary>
    /// [EN] Check state of each plugin in the trust confirmation panel (id → trusted).
    /// [ZH] 信任确认面板里每个插件的勾选状态（id → 是否信任）。
    /// </summary>
    private readonly Dictionary<string, bool> _trustDecisions = new(StringComparer.Ordinal);

    public InstalledPage()
    {
        InitializeComponent();
        SkillsRefreshButton.Click += async (_, _) => await RefreshAsync();
        SkillsReloadButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_reload" });
            await RefreshAsync();
        };
        SkillsExamplesButton.Click += async (_, _) =>
        {
            await SendAsync(new { action = "skills_examples" });
            await RefreshAsync();
        };
        // [EN] v0.2.2 batch 4 3a: install from a folder / create a local plugin
        // [ZH] v0.2.2 第 4 批 3a：从文件夹安装 / 新建本地插件
        SkillsInstallFolderButton.Click += async (_, _) => await InstallFromFolderAsync();
        SkillsScaffoldButton.Click += (_, _) => GoToScaffold();

        TrustEnableAllButton.Click += (_, _) => SetAllTrust(true);
        TrustDisableAllButton.Click += (_, _) => SetAllTrust(false);
        TrustConfirmButton.Click += async (_, _) => await SubmitTrustAsync();
        TrustCancelButton.Click += (_, _) =>
        {
            // [EN] Close directly: nothing is recorded, and the question will be asked
            // [EN] again the next time the settings window opens (the same behavior as WPF).
            // [ZH] 直接关掉：不写任何记录，下次打开设置窗口时会再问一次（与 WPF 同一行为）
            TrustPanel.IsVisible = false;
            _trustHandled = false;
        };
    }

    public string TabKey => "settings.tab.installed";
    public string TabFallback => "已安装";
    public string AutomationId => "SettingsTabInstalled";

    public void Attach(ISettingsHost host) => _host = host;

    // [EN] The installed page contains only content read from the backend; it has no UI
    // [EN] setting to store in ui_state.json.
    // [ZH] 已安装页全是“从后端读”的内容，没有需要收进 ui_state.json 的界面设置。
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }
    public Task ApplyToBackendAsync() => Task.CompletedTask;

    // -----------------------------------------------------------------
    // [EN] Data
    // [ZH] 数据
    // -----------------------------------------------------------------
    private async Task<JsonDocument?> SendAsync(object request)
    {
        if (_host is null)
            return null;
        return await _host.State.ActiveBackend.SendAsync(request, idleTimeout: TimeSpan.FromSeconds(60));
    }

    public async Task RefreshAsync()
    {
        if (_host is null)
            return;
        SkillStatusText.Text = Loc.T("settings.installed.labelReadingSkills", "读取插件列表");
        using JsonDocument? doc = await SendAsync(new { action = "skills_list" });
        if (doc is null)
        {
            SkillStatusText.Text = Loc.T("settings.installed.backendGone",
                "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）");
            return;
        }
        if (!doc.RootElement.TryGetProperty("skills", out JsonElement status))
            return;
        _status = status.Clone();

        var cards = new List<Control>();
        if (status.TryGetProperty("skills", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in arr.EnumerateArray())
                cards.Add(BuildCard(item));
        }

        SkillList.ItemsSource = cards;
        bool empty = cards.Count == 0;
        SkillEmptyText.IsVisible = empty;
        SkillList.IsVisible = !empty;
        if (!empty)
        {
            // [EN] Status line convention matches WPF: count + loaded/failed + API version
            // [ZH] 状态行口径与 WPF 一致：条数 + 加载/失败数 + API 版本
            string counts = "";
            if (status.TryGetProperty("counts", out JsonElement c))
            {
                counts = $"total={GetInt(c, "total")}, loaded={GetInt(c, "loaded")}, failed={GetInt(c, "failed")}";
            }
            string api = GetString(status, "api_version");
            SkillStatusText.Text = counts.Length > 0
                ? $"{Loc.T("settings.tab.installed", "已安装")}: {counts} · API {api}"
                : Loc.T("settings.installed.loadedCount", "已加载 {0} 个插件", cards.Count);
        }
        else
        {
            SkillStatusText.Text = "";
        }

        // [EN] The trust confirmation is shown only **after the current load completes**:
        // [EN] the same handling as WPF (WPF defers it with Dispatcher.BeginInvoke because
        // [EN] a modal dialog blocks the UI thread; there is no modal dialog here, but
        // [EN] "draw the list first, then ask" is still friendlier).
        // [ZH] 信任确认放在**本次加载结束之后**再弹：与 WPF 的处置一致
        // [ZH] （WPF 用 Dispatcher.BeginInvoke 延后，理由是模态框会阻塞 UI 线程；
        // [ZH]  这里没有模态框，但“先把列表画出来再问”的顺序仍然更友好）。
        Dispatcher.UIThread.Post(CheckTrustPending);
    }

    // -----------------------------------------------------------------
    // [EN] Cards
    // [ZH] 卡片
    // -----------------------------------------------------------------
    private Control BuildCard(JsonElement item)
    {
        string id = GetString(item, "id");
        string name = GetString(item, "name");
        string source = GetString(item, "source");
        string version = GetString(item, "version");
        string path = GetString(item, "path");
        bool loaded = GetBool(item, "loaded");
        bool dev = GetBool(item, "dev");
        bool enabled = !item.TryGetProperty("enabled", out JsonElement en) || en.ValueKind != JsonValueKind.False;
        string error = GetString(item, "error");
        string overrode = GetString(item, "overrode");

        string sourceTag = source switch
        {
            "dev" => Loc.T("settings.installed.sourceDev", "🔧 开发"),
            "local" => Loc.T("settings.installed.sourceLocal", "📁 本地"),
            _ => Loc.T("settings.installed.sourceMarket", "🏪 市场"),
        };
        (string statusText, string statusBrush) = !enabled
            ? (Loc.T("settings.installed.statusDisabled", "⏸ 已禁用"), "MutedForegroundBrush")
            : error.Length > 0
                ? (Loc.T("settings.installed.statusFailed", "❌ 加载失败"), "AccentBrush")
                : loaded
                    ? (Loc.T("settings.installed.statusLoaded", "✅ 已加载"), "AccentBrush")
                    : (Loc.T("settings.installed.statusNotLoaded", "⏸ 未加载"), "MutedForegroundBrush");

        var panel = new StackPanel { Spacing = 4 };

        var titleRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        titleRow.Children.Add(new TextBlock
        {
            Text = string.IsNullOrEmpty(name) ? id : name,
            FontWeight = FontWeight.SemiBold,
            FontSize = 13,
        });
        titleRow.Children.Add(new TextBlock { Text = sourceTag, FontSize = 11, Opacity = 0.75, VerticalAlignment = VerticalAlignment.Center });
        if (!string.IsNullOrEmpty(version))
            titleRow.Children.Add(new TextBlock { Text = "v" + version, FontSize = 11, Opacity = 0.6, VerticalAlignment = VerticalAlignment.Center });
        titleRow.Children.Add(new TextBlock
        {
            Text = statusText,
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = TryBrush(statusBrush),
        });
        panel.Children.Add(titleRow);

        // [EN] Capability line (commands / takeover formats / automation hooks / panels) —
        // [EN] the same convention as the WPF version; only non-empty values are shown.
        // [ZH] 能力行（指令 / 接管格式 / 自动化钩子 / 面板）—— 与 WPF 版同口径，只显示非空的
        var caps = new List<string>();
        AppendList(caps, item, "commands", "settings.installed.commandsLabel", "指令：");
        AppendList(caps, item, "parsers", "settings.installed.parsersLabel", "接管格式：");
        AppendList(caps, item, "hooks", "settings.installed.hooksLabel", "自动化钩子：");
        AppendList(caps, item, "panels", "settings.installed.panelsLabel", "面板：");
        if (caps.Count > 0)
            panel.Children.Add(new TextBlock { Text = string.Join("　", caps), FontSize = 11, Opacity = 0.75, TextWrapping = TextWrapping.Wrap });

        // [EN] "Path" line (v0.2.2 batch 4 3a): first-hand information when investigating
        // [EN] "which copy is actually loaded".
        // [ZH] 「路径」行（v0.2.2 第 4 批 3a）：排查“到底加载的是哪一份”时的第一手信息
        if (path.Length > 0)
        {
            panel.Children.Add(new TextBlock
            {
                Text = $"{Loc.T("settings.installed.pathLabel", "路径")}：{path}",
                FontSize = 11, Opacity = 0.7, TextWrapping = TextWrapping.Wrap,
            });
        }

        // [EN] "Permissions" line (same as above): the permissions declared by the plugin
        // [EN] are the basis for deciding whether to trust it.
        // [ZH] 「权限」行（同上）：插件声明了什么权限，是“要不要信任它”的判断依据
        panel.Children.Add(new TextBlock
        {
            Text = Loc.T("settings.installed.permissionsLabel", "声明的权限：") + DescribePermissions(item),
            FontSize = 11, Opacity = 0.7, TextWrapping = TextWrapping.Wrap,
        });

        if (overrode.Length > 0)
            panel.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.installed.overrode", "已覆盖：{0}", overrode),
                FontSize = 11,
                Opacity = 0.75,
                TextWrapping = TextWrapping.Wrap,
            });

        if (error.Length > 0)
            panel.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.installed.detail", "错误详情") + "：" + error,
                FontSize = 11,
                TextWrapping = TextWrapping.Wrap,
                Foreground = TryBrush("AccentBrush"),
            });

        // [EN] ---- Card action bar (v0.2.2 batch 4 3a) ----
        // [ZH] ---- 卡片操作条（v0.2.2 第 4 批 3a）----
        var actions = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
            Margin = new Thickness(0, 6, 0, 0),
        };

        var openDir = new Button { Content = Loc.T("settings.installed.openDir", "📂 打开目录"), Padding = new Thickness(10, 3), FontSize = 11 };
        AutomationProperties.SetAutomationId(openDir, "SkillOpenDir_" + id);
        openDir.Click += (_, _) =>
        {
            if (!ShellOpen.TryOpenDirectory(path))
                SkillStatusText.Text = Loc.T("settings.installed.openDirFailed", "⚠️ 无法打开目录：{0}", path);
        };
        actions.Children.Add(openDir);

        if (error.Length > 0)
        {
            // [EN] "Error details" appears only when there really is an error: without an
            // [EN] error its content is an empty array, and clicking it would only confuse.
            // [ZH] 「错误详情」只在**确实有错误**时出现：没有错误时它的内容是空数组，
            // [ZH] 点了只会让人困惑
            var detail = new Button { Content = Loc.T("settings.installed.detail", "错误详情"), Padding = new Thickness(10, 3), FontSize = 11 };
            AutomationProperties.SetAutomationId(detail, "SkillDetail_" + id);
            detail.Click += (_, _) => ShowErrorDetail(name, id, path, error);
            actions.Children.Add(detail);
        }

        var exportZip = new Button { Content = Loc.T("settings.installed.exportZip", "导出 zip"), Padding = new Thickness(10, 3), FontSize = 11 };
        AutomationProperties.SetAutomationId(exportZip, "SkillExportZip_" + id);
        exportZip.Click += async (_, _) => await ExportZipAsync(id);
        actions.Children.Add(exportZip);

        var publish = new Button { Content = Loc.T("settings.installed.publishCheck", "发布自检"), Padding = new Thickness(10, 3), FontSize = 11 };
        AutomationProperties.SetAutomationId(publish, "SkillPublishCheck_" + id);
        publish.Click += async (_, _) => await PublishCheckAsync(id);
        actions.Children.Add(publish);

        var uninstall = new Button { Content = Loc.T("settings.installed.uninstall", "卸载"), Padding = new Thickness(10, 3), FontSize = 11 };
        uninstall.Click += async (_, _) => await OnUninstallAsync(id, uninstall, dev, name);
        actions.Children.Add(uninstall);

        panel.Children.Add(actions);

        var border = new Border
        {
            Background = TryBrush("CardBackgroundBrush"),
            BorderBrush = TryBrush("CardBorderBrush"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(12, 10),
            Child = panel,
        };
        return border;
    }

    /// <summary>
    /// [EN] Renders the <c>permissions</c> object as one line of text; when nothing is
    /// [EN] declared it returns "(no permissions declared)".
    /// [ZH] 把 <c>permissions</c> 对象渲染成一行文本；没声明时给“（未声明任何权限）”。
    /// </summary>
    private static string DescribePermissions(JsonElement item)
    {
        if (!item.TryGetProperty("permissions", out JsonElement perms) || perms.ValueKind != JsonValueKind.Object)
            return Loc.T("settings.installed.noPermissions", "（未声明任何权限）");
        var parts = new List<string>();
        foreach (JsonProperty property in perms.EnumerateObject())
            parts.Add($"{property.Name}={property.Value}");
        return parts.Count > 0
            ? string.Join("; ", parts)
            : Loc.T("settings.installed.noPermissions", "（未声明任何权限）");
    }

    private static void AppendList(List<string> into, JsonElement item, string field, string key, string fallback)
    {
        if (!item.TryGetProperty(field, out JsonElement arr) || arr.ValueKind != JsonValueKind.Array)
            return;
        var values = new List<string>();
        foreach (JsonElement entry in arr.EnumerateArray())
        {
            if (entry.ValueKind == JsonValueKind.String)
                values.Add(entry.GetString() ?? "");
        }
        if (values.Count > 0)
            into.Add(Loc.T(key, fallback) + string.Join(", ", values));
    }

    // -----------------------------------------------------------------
    // [EN] Card actions (v0.2.2 batch 4 3a)
    // [ZH] 卡片动作（v0.2.2 第 4 批 3a）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Error details: WPF shows a MessageBox; here they are expanded in place in the
    /// [EN] status line (selectable, non-blocking).
    /// [ZH] 错误详情：WPF 弹 MessageBox，这里就地展开在状态行（内容可选中、不阻塞）。
    /// </summary>
    private void ShowErrorDetail(string name, string id, string path, string error)
    {
        SkillStatusText.Text = string.Join(Environment.NewLine, new[]
        {
            $"{name} ({id})",
            path,
            "",
            error,
        });
    }

    /// <summary>
    /// [EN] Exports a plugin as a zip (`skills_export_zip`). The path comes from the system
    /// [EN] save dialog.
    /// [ZH] 导出插件为 zip（`skills_export_zip`）。路径由系统保存对话框给出。
    /// </summary>
    private async Task ExportZipAsync(string id)
    {
        TopLevel? top = TopLevel.GetTopLevel(this);
        if (top is null)
            return;
        var file = await top.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
        {
            Title = Loc.T("settings.installed.exportZip", "导出 zip"),
            SuggestedFileName = id + ".zip",
            DefaultExtension = "zip",
            FileTypeChoices = new[]
            {
                new FilePickerFileType("ZIP") { Patterns = new[] { "*.zip" } },
            },
        });
        string? path = file?.TryGetLocalPath();
        if (string.IsNullOrEmpty(path))
            return;
        using JsonDocument? doc = await SendAsync(new { action = "skills_export_zip", id, path });
        SkillStatusText.Text = doc is null
            ? Loc.T("settings.installed.backendGone", "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）")
            : GetString(doc.RootElement, "content");
    }

    /// <summary>
    /// [EN] Publish self-check (`skills_publish_check`). WPF shows a MessageBox; here the
    /// [EN] result is **expanded in place** in the status line: ✓/✗ items and hints for
    /// [EN] failures are listed one by one (the content is identical to the WPF dialog).
    /// [ZH] 发布自检（`skills_publish_check`）。WPF 弹 MessageBox，这里**就地展开**在状态行：
    /// [ZH] 逐条列出 ✓/✗ 与未通过项的提示（内容与 WPF 的对话框一字不差）。
    /// </summary>
    private async Task PublishCheckAsync(string id)
    {
        using JsonDocument? doc = await SendAsync(new { action = "skills_publish_check", id });
        if (doc is null)
        {
            SkillStatusText.Text = Loc.T("settings.installed.backendGone",
                "⚠️ 后端当前不可用，插件操作未执行（请稍后重试）");
            return;
        }
        if (!doc.RootElement.TryGetProperty("publish_check", out JsonElement check))
            return;
        var builder = new System.Text.StringBuilder();
        builder.AppendLine(Loc.T("settings.installed.publishResult", "发布自检结果"));
        builder.AppendLine(Loc.T("settings.installed.publishLine", "{0}：{1}",
            Loc.T("settings.installed.publishResult", "发布自检结果"), GetString(check, "message")));
        builder.AppendLine(Loc.T("settings.installed.publishLine", "{0}：{1}",
            Loc.T("settings.developer.topicLabel", "市场 topic"), GetString(check, "topic")));
        builder.AppendLine(Loc.T("settings.installed.publishLine", "{0}：{1}",
            Loc.T("settings.developer.repoLabel", "建议仓库名"), GetString(check, "repo_name")));
        if (check.TryGetProperty("checks", out JsonElement checks) && checks.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement entry in checks.EnumerateArray())
            {
                bool ok = GetBool(entry, "ok");
                builder.AppendLine($"{(ok ? "✓" : "✗")} {GetString(entry, "name")}");
                if (!ok)
                    builder.AppendLine("    " + GetString(entry, "hint"));
            }
        }
        SkillStatusText.Text = builder.ToString().TrimEnd();
    }

    /// <summary>
    /// [EN] Installs from a folder (`skills_install_folder`). The folder comes from the
    /// [EN] system picker (WPF uses OpenFolderDialog).
    /// [ZH] 从文件夹安装（`skills_install_folder`）。文件夹由系统选择器给出
    /// [ZH] （WPF 用 OpenFolderDialog）。
    /// </summary>
    private async Task InstallFromFolderAsync()
    {
        TopLevel? top = TopLevel.GetTopLevel(this);
        if (top is null)
            return;
        var folders = await top.StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = Loc.T("settings.installed.installPrompt", "选择插件文件夹（其中应包含 skill.json）"),
            AllowMultiple = false,
        });
        if (folders.Count == 0)
            return;
        string? path = folders[0].TryGetLocalPath();
        if (string.IsNullOrEmpty(path))
            return;
        SkillStatusText.Text = Loc.T("settings.installed.labelReadingSkills", "读取插件列表");
        using JsonDocument? doc = await SendAsync(new { action = "skills_install_folder", path });
        if (doc is not null && doc.RootElement.TryGetProperty("content", out JsonElement c))
            SkillStatusText.Text = c.GetString() ?? "";
        await RefreshAsync();
    }

    /// <summary>
    /// [EN] "➕ create local plugin": switches to the developer page and focuses the plugin
    /// [EN] ID input (aligned with SkillsScaffoldButton_Click in WPF).
    /// [ZH] 「➕ 新建本地插件」：跳到「开发者」页并聚焦到插件 ID 输入框（对齐 WPF 的
    /// [ZH] SkillsScaffoldButton_Click）。
    /// </summary>
    private void GoToScaffold()
    {
        if (_host is null)
            return;
        if (!_host.ShowTab("settings.tab.developer"))
        {
            SkillStatusText.Text = Loc.T("settings.installed.scaffoldJumpFailed",
                "⚠️ 找不到「开发者」页签，请手动切过去再新建插件");
            return;
        }
        _host.FindPage<DeveloperPage>()?.FocusScaffoldId();
    }

    /// <summary>
    /// [EN] Second confirmation for uninstall: the first click turns the button into
    /// [EN] "Confirm uninstall?", and a second click within 3 seconds performs it.
    /// [EN] Why no modal dialog: Avalonia has no built-in MessageBox, building another
    /// [EN] window for one confirmation is not worthwhile, and doing no confirmation at all
    /// [EN] would let a stray click delete a plugin.
    /// [EN] The confirmation text for development plugins matches WPF (it states clearly
    /// [EN] that "only the directory under skills_dev is deleted").
    /// [ZH] 卸载的二次确认：第一次点击把按钮变成“确认卸载？”，3 秒内再点一次才真正执行。
    /// [ZH] 为什么不用模态对话框：Avalonia 没有内置 MessageBox，为一个确认框再造一个窗口
    /// [ZH] 不划算，而完全不做确认又会让误点直接删插件。
    /// [ZH] 开发插件的确认文案与 WPF 一致（明确“只删 skills_dev 下的目录”）。
    /// </summary>
    private async Task OnUninstallAsync(string id, Button button, bool dev, string name)
    {
        if (_pendingUninstallId != id)
        {
            _pendingUninstallId = id;
            button.Content = Loc.T("settings.installed.uninstallConfirmShort", "确认卸载？");
            SkillStatusText.Text = dev
                ? Loc.T("settings.installed.uninstallConfirmDev",
                    "确定卸载开发插件「{0}」吗？\n（只会删除 skills_dev 下的目录，不影响正式安装记录）", name)
                : Loc.T("settings.installed.uninstallConfirm", "确定卸载插件「{0}」吗？", name);
            _pendingTimer?.Stop();
            _pendingTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            _pendingTimer.Tick += (_, _) =>
            {
                _pendingTimer?.Stop();
                _pendingUninstallId = "";
                button.Content = Loc.T("settings.installed.uninstall", "卸载");
            };
            _pendingTimer.Start();
            return;
        }

        _pendingTimer?.Stop();
        _pendingUninstallId = "";
        await SendAsync(new { action = "skills_uninstall", id, delete_files = true });
        await RefreshAsync();
    }

    // -----------------------------------------------------------------
    // [EN] Trust confirmation for newly loaded plugins of unknown origin (v0.2.2 batch 4 3a)
    // [ZH] 首次加载未知来源插件的信任确认（v0.2.2 第 4 批 3a）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Collects the plugins with `trust_pending` set and shows the confirmation panel.
    ///
    /// [EN] The same criteria as the WPF TrustDialog (those criteria are themselves the
    /// [EN] product of "fix 1"): **all pending plugins are listed at once** instead of
    /// [EN] asking every time one is found; after the user decides for the whole batch it
    /// [EN] is written in one call (`skills_trust_bulk`), and this is never asked again.
    /// [ZH] 收集 `trust_pending` 的插件并显示确认面板。
    ///
    /// [ZH] 与 WPF 的 TrustDialog 同一套判据（那套判据本身是“修复一”的产物）：
    /// [ZH] **一次性列出全部**待确认插件，而不是每发现一个弹一次；
    /// [ZH] 用户整批决定后一次写入（`skills_trust_bulk`），此后再也不问。
    /// </summary>
    private void CheckTrustPending()
    {
        if (_trustHandled || _status is not JsonElement status)
            return;
        if (!status.TryGetProperty("skills", out JsonElement arr) || arr.ValueKind != JsonValueKind.Array)
            return;

        var pending = new List<(string Id, string Name, string Path, string Source,
                                List<string> Capabilities, string Permissions)>();
        foreach (JsonElement item in arr.EnumerateArray())
        {
            if (!GetBool(item, "trust_pending"))
                continue;
            var caps = new List<string>();
            foreach (string key in new[] { "commands", "parsers", "hooks", "panels" })
            {
                var values = new List<string>();
                if (item.TryGetProperty(key, out JsonElement list) && list.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement entry in list.EnumerateArray())
                    {
                        if (entry.ValueKind == JsonValueKind.String)
                            values.Add(entry.GetString() ?? "");
                    }
                }
                if (values.Count > 0)
                    caps.Add($"{key}: {string.Join(", ", values)}");
            }
            pending.Add((GetString(item, "id"), GetString(item, "name"),
                GetString(item, "path"), GetString(item, "source"), caps, DescribePermissions(item)));
        }
        if (pending.Count == 0)
            return;

        _trustHandled = true;
        _trustDecisions.Clear();
        TrustList.Children.Clear();
        foreach ((string id, string name, string path, _, List<string> caps, string permissions) in pending)
        {
            // [EN] Checked by default, consistent with the initial value of the WPF
            // [EN] TrustDialog.
            // [ZH] 默认勾选“信任”，与 WPF TrustDialog 的初值一致
            _trustDecisions[id] = true;
            var row = new StackPanel { Spacing = 2 };
            var head = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            var check = new CheckBox
            {
                Content = string.IsNullOrEmpty(name) ? id : name,
                IsChecked = true,
                FontSize = 12,
            };
            AutomationProperties.SetAutomationId(check, "TrustCheck_" + id);
            check.IsCheckedChanged += (_, _) => _trustDecisions[id] = check.IsChecked == true;
            head.Children.Add(check);
            row.Children.Add(head);
            row.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.installed.trustText",
                    "插件「{0}」来自本地路径：\n{1}\n\n它将以你的账户权限运行。只在你信任这个插件时才继续。",
                    id, path),
                FontSize = 10, Opacity = 0.75, TextWrapping = TextWrapping.Wrap,
            });
            if (caps.Count > 0)
            {
                row.Children.Add(new TextBlock
                {
                    Text = string.Join("　", caps), FontSize = 10, Opacity = 0.7,
                    TextWrapping = TextWrapping.Wrap,
                });
            }
            row.Children.Add(new TextBlock
            {
                Text = Loc.T("settings.installed.permissionsLabel", "声明的权限：") + permissions,
                FontSize = 10, Opacity = 0.7, TextWrapping = TextWrapping.Wrap,
            });
            TrustList.Children.Add(row);
        }

        TrustIntro.Text = Loc.T("settings.installed.trustBulkIntro",
            "发现 {0} 个新的本地插件。它们将以你的账户权限运行，只勾选你信任的插件。\n你的选择会被记住，之后不再询问。",
            pending.Count);
        TrustPanel.IsVisible = true;
    }

    private void SetAllTrust(bool trusted)
    {
        foreach (string id in new List<string>(_trustDecisions.Keys))
        {
            _trustDecisions[id] = trusted;
            // [EN] Sync the check boxes: find them through visual descendants (the rows in
            // [EN] the panel are built at runtime).
            // [ZH] 同步勾选框：用可视化后代找（面板里的行是运行时构建的）
            foreach (CheckBox box in TrustList.GetVisualDescendants().OfType<CheckBox>())
            {
                if (AutomationProperties.GetAutomationId(box) == "TrustCheck_" + id)
                    box.IsChecked = trusted;
            }
        }
    }

    /// <summary>
    /// [EN] Submits all trust decisions in one batch (`skills_trust_bulk`), then hides the
    /// [EN] panel and refreshes.
    /// [ZH] 整批提交信任决定（`skills_trust_bulk`），然后收起面板并刷新。
    /// </summary>
    private async Task SubmitTrustAsync()
    {
        var decisions = new List<object>();
        foreach (KeyValuePair<string, bool> pair in _trustDecisions)
            decisions.Add(new { id = pair.Key, trusted = pair.Value });
        TrustPanel.IsVisible = false;
        if (decisions.Count > 0)
            await SendAsync(new { action = "skills_trust_bulk", decisions });
        await RefreshAsync();
    }

    private static Brush? TryBrush(string key)
    {
        if (Application.Current is null)
            return null;
        if (Application.Current.TryFindResource(key, out object? value) && value is Brush brush)
            return brush;
        return null;
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
    // [EN] Probe interface (used by `EchoDesk --probe pages`: whether all plugin
    // [EN] ecosystem entry points really exist)
    // [ZH] 探针接口（`EchoDesk --probe pages` 用：插件生态的入口是否真的都在）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Observable state of the installed page.
    /// [ZH] 已安装页的可观测状态。
    /// </summary>
    public string PluginSnapshot()
    {
        int cards = 0;
        if (SkillList.ItemsSource is System.Collections.IEnumerable items)
        {
            foreach (object _ in items)
                cards++;
        }
        return string.Join("; ", new[]
        {
            $"cards={cards}",
            $"emptyVisible={(SkillEmptyText.IsVisible ? 1 : 0)}",
            $"installFolderButton={(string.IsNullOrEmpty(SkillsInstallFolderButton.Content as string) ? 0 : 1)}",
            $"scaffoldButton={(string.IsNullOrEmpty(SkillsScaffoldButton.Content as string) ? 0 : 1)}",
            $"trustPanel={(TrustPanel.IsVisible ? 1 : 0)}",
            $"trustTitle={(string.IsNullOrEmpty(TrustTitle.Text) ? 0 : 1)}",
            $"trustButtons={(string.IsNullOrEmpty(TrustConfirmButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(TrustCancelButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(TrustEnableAllButton.Content as string) ? 0 : 1)}"
            + $"{(string.IsNullOrEmpty(TrustDisableAllButton.Content as string) ? 0 : 1)}",
            $"status={(string.IsNullOrEmpty(SkillStatusText.Text) ? 0 : 1)}",
        });
    }

    /// <summary>
    /// [EN] Probe: triggers the trust confirmation directly (using the trust_pending data
    /// [EN] actually returned by the backend).
    /// [ZH] 探针：直接触发信任确认（用后端真的返回的 trust_pending 数据）。
    /// </summary>
    public void ProbeShowTrust()
    {
        _trustHandled = false;
        CheckTrustPending();
    }

    // -----------------------------------------------------------------
    // [EN] Texts
    // [ZH] 文案
    // -----------------------------------------------------------------
    public void ApplyTexts()
    {
        // [EN] Section title: the same key and the same font-size convention as "Loaded
        // [EN] plugins" in WPF (15 pt, bold, accent color).
        // [ZH] 区块标题：与 WPF 的「已加载插件」同一个键、同一个字号口径（15 号加粗强调色）。
        InstalledTitle.Text = Loc.T("settings.installed.title", "已加载插件");
        SkillsRefreshButton.Content = Loc.T("settings.installed.refresh", "🔄 刷新");
        SkillsReloadButton.Content = Loc.T("settings.installed.reload", "🔄 重载本地插件");
        SkillsInstallFolderButton.Content = Loc.T("settings.installed.installFolder", "📥 从文件夹安装");
        SkillsScaffoldButton.Content = Loc.T("settings.installed.newPlugin", "➕ 新建本地插件");
        SkillsExamplesButton.Content = Loc.T("settings.developer.installExamples", "📦 安装示例插件");
        InstalledTipText.Text = Loc.T("settings.installed.tip",
            "这里列出市场安装、本地安装与开发目录中的全部插件及其加载状态。");
        SkillEmptyText.Text = Loc.T("settings.installed.empty",
            "还没有加载任何插件。可以在「技能市场」安装，或点「➕ 新建本地插件」开始写一个。");
        TrustTitle.Text = Loc.T("settings.installed.trustTitle", "确认加载本地插件");
        TrustWarn.Text = Loc.T("settings.installed.trustWarn",
            "⚠️ 插件代码会在本机以你的权限运行，无法限制它能访问什么。");
        TrustEnableAllButton.Content = Loc.T("settings.installed.trustEnableAll", "全部启用");
        TrustDisableAllButton.Content = Loc.T("settings.installed.trustDisableAll", "全部禁用");
        TrustConfirmButton.Content = Loc.T("settings.installed.trustAccept", "信任并加载");
        TrustCancelButton.Content = Loc.T("settings.installed.trustReject", "不加载");
        // [EN] Card content is built at runtime: when the language changes it must be
        // [EN] rebuilt once (Avalonia has no WPF-style attached-property traversal).
        // [ZH] 卡片内容是运行时构建的：语言变了要重建一次（Avalonia 没有 WPF 的附加属性遍历）。
        if (_status is { } status)
            _ = RefreshAsync();
    }
}

