using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using EchoDesk.Services;

namespace EchoDesk;

/// <summary>
/// 模型档位条目（设置 → 模型 →「模型档位」胶囊按钮 Tag 上的数据）。
///
/// 与「本地模型库」的区别：本地模型库列的是**磁盘上有什么**（下载记录 + 扫描结果），
/// 这里列的是 <c>config.json</c> 里 <c>models</c> 登记了哪几档（0.6B / 1.7B 这类档位），
/// 并标出当前用的是哪一档、哪一档还没下载。两者互补：档位告诉用户"能选什么"，
/// 模型库告诉用户"已经有什么"。
/// </summary>
public sealed class ModelProfileItem
{
    public string Alias { get; init; } = "";
    public string Name { get; init; } = "";
    public string Repo { get; init; } = "";
    /// <summary>参数量 · 量化 · 上下文（例如 "0.6B · Q6_K · 8192 上下文"）。</summary>
    public string Meta { get; init; } = "";
    public string SizeText { get; init; } = "";
    public bool Installed { get; init; }
    public bool Current { get; init; }

    /// <summary>上下文长度（0 = 未登记；编辑表单留空即"不改动这一项"）。</summary>
    public int ContextLength { get; init; }

    /// <summary>该档登记的温度（null = 未登记，编辑表单留空）。</summary>
    public double? Temperature { get; init; }

    /// <summary>胶囊上的文字：参数量 · 量化 · 体积/未下载，当前档再加个"正在使用"。</summary>
    public string ChipText
    {
        get
        {
            var parts = new List<string>();
            if (Meta.Length > 0)
                parts.Add(Meta);
            parts.Add(Installed ? SizeText : Loc.T("settings.model.profileNotDownloaded", "未下载"));
            if (Current)
                parts.Add(Loc.T("settings.model.badgeInUse", "▶ 正在使用"));
            return string.Join(" · ", parts.Where(x => x.Length > 0));
        }
    }

    /// <summary>悬停提示：文件名、仓库与"点它会做什么"。</summary>
    public string ToolTipText
    {
        get
        {
            var lines = new List<string> { Name };
            if (Repo.Length > 0)
                lines.Add(Repo);
            lines.Add(Current
                ? Loc.T("settings.model.profileTipCurrent", "当前正在使用这一档")
                : Installed
                    ? Loc.T("settings.model.profileTipSwitch", "点一下切换到这一档（热加载，无需重启）")
                    : Loc.T("settings.model.profileTipDownload", "点一下从仓库下载这一档"));
            return string.Join("\n", lines);
        }
    }
}

/// <summary>
/// 设置窗口 ·「模型档位」（多档默认模型的显示与切换）。
///
/// 需求背景：`config.models` 里登记了多档模型（小档 0.6B 默认、大档 1.7B 可选），
/// 但此前只有命令行 `/model <别名>` 能用，设置界面里完全看不到 —— 用户不知道有哪几档，
/// 也不知道哪一档在本地。这里把它做成设置页里的一排胶囊按钮：
///   * 每档显示 参数量 / 量化 / 上下文 / 体积，当前档额外标"正在使用"；
///   * 已下载 → 点击**切换**（热加载，不用重启程序或前端）；
///   * 未下载 → 点击**下载**（在该档自己的仓库里解析文件名，再走市场那套流式下载）。
///
/// 为什么是"一排胶囊"而不是一个列表：模型页的竖向空间已经被模型市场占满
/// （结果列表是 `*` 行），加一个高列表会把结果列表挤成 0 高度 —— 实测踩到过：
/// 交付物探针里"结果列表可滚动/筛选面板/分页控件位置"六条断言一起变红。
/// </summary>
public partial class SettingsWindow
{
    /// <summary>读取并渲染模型档位（不联网：未下载的档位如实标"未下载"）。</summary>
    private async Task LoadModelProfilesAsync()
    {
        try
        {
            string json = await _backend.SendRequestAsync(new { action = "model_profiles" });
            using JsonDocument doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("model_profiles", out JsonElement data))
                RenderModelProfiles(data);
        }
        catch (Exception ex)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileLoadFailed",
                "❌ 读取模型档位失败：{0}", ex.Message);
        }
    }

    private void RenderModelProfiles(JsonElement data)
    {
        ModelProfileChips.Children.Clear();
        string current = GetString(data, "current");
        int count = 0;
        if (data.TryGetProperty("items", out JsonElement items) && items.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement p in items.EnumerateArray())
            {
                bool installed = GetBool(p, "installed");
                int ctx = GetInt(p, "context_length", 0);
                var item = new ModelProfileItem
                {
                    Alias = GetString(p, "alias"),
                    Name = GetString(p, "name"),
                    Repo = GetString(p, "repo"),
                    Installed = installed,
                    Current = GetBool(p, "current"),
                    SizeText = GetString(p, "size_human"),
                    ContextLength = ctx,
                    // temperature 可能是 null（未登记）→ 只有是数字时才取，
                    // 否则编辑表单会显示成 0 并让用户以为"这一档温度就是 0"
                    Temperature = p.TryGetProperty("temperature", out JsonElement tEl)
                                  && tEl.ValueKind == JsonValueKind.Number
                                  && tEl.TryGetDouble(out double tValue) ? tValue : null,
                    Meta = string.Join(" · ", new[]
                    {
                        GetString(p, "params"),
                        GetString(p, "quantization"),
                        ctx > 0 ? Loc.T("settings.model.profileContext", "{0} 上下文", ctx) : "",
                    }.Where(x => x.Length > 0)),
                };
                var chip = new Button
                {
                    // 内容用 TextBlock 包一层，而不是直接给字符串：
                    // WPF 的 Button 会用 AccessText 渲染字符串内容，**下划线被当成访问键前缀吃掉** ——
                    // "Q4_K_M" 会显示成 "Q4K_M"（K 变成访问键）。量化名里下划线是常态，
                    // 用 TextBlock 承载即可原样显示（GUI 探针实测踩到）。
                    Content = new TextBlock { Text = item.ChipText },
                    Tag = item,
                    ToolTip = item.ToolTipText,
                    FontSize = 11,
                    Padding = new Thickness(10, 3, 10, 3),
                    Margin = new Thickness(0, 0, 6, 0),
                    // 当前档不需要再点（点了也是空操作），但仍然显示出来让用户看清现状。
                    //
                    // **v0.2.2 第 4 批（总策划裁定三）：判据从 `!item.Current` 改成
                    // `!(Current && Installed)`。** 原因：后端的 `current` 曾经在
                    // "config 指着某个模型、文件却不在磁盘上"时也是 true，于是这条
                    // `!Current` 把一个**未下载**的档位禁用了 —— 显示"未下载 · ▶ 正在使用"
                    // 且点不动，用户**没有任何入口去下载它**。
                    // 后端已同步修正（`current` 现在要求文件真的存在），这里再兜一层：
                    // 只要没装到本地，胶囊就必须可点（点了走下载分支）。
                    // 两侧行为一致：Avalonia 的 ModelPage 用同一判据。
                    IsEnabled = !(item.Current && item.Installed),
                };
                if (TryFindResource("RoundButton") is Style style)
                    chip.Style = style;
                // 点击 = 已下载则切换、未下载则下载（一个按钮两种语义，胶囊文字已经写明）
                chip.Click += (_, _) =>
                {
                    if (item.Installed)
                        _ = ApplyProfileAsync(item);
                    else
                        _ = DownloadProfileAsync(item);
                };
                // 胶囊 + 编辑 + 删除：**同一档的一组按钮放在一个横向容器里**，
                // WrapPanel 换行时整组一起换，不会出现"编辑按钮跑到下一行的另一档旁边"。
                var group = new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Margin = new Thickness(0, 0, 6, 4),
                };
                group.Children.Add(chip);
                group.Children.Add(MakeProfileActionButton(
                    // 用 📝 而不是铅笔符号（U+270E）：那个码位不在 Emoji 降级表里，
                    // 无 emoji 字体的机器上会被 tools\check_emoji.ps1 判为缺映射
                    //（界面上则可能显示成方块）。
                    Loc.T("settings.profile.edit", "📝 编辑"),
                    Loc.T("settings.profile.editTip", "编辑这一档的登记信息（别名、文件、上下文长度、温度、备注）"),
                    () => ShowProfileEditor(item)));
                group.Children.Add(MakeProfileActionButton(
                    Loc.T("settings.profile.delete", "🗑 删除"),
                    Loc.T("settings.profile.deleteTip", "删除这一档的登记（不会删除磁盘上的模型文件）"),
                    () => _ = DeleteProfileAsync(item)));
                ModelProfileChips.Children.Add(group);
                count++;
            }
        }
        ProfileStatusText.Text = count == 0
            ? Loc.T("settings.model.profileEmpty",
                "config.json 里没有登记模型档位（models 为空）。")
            : Loc.T("settings.model.profileHint",
                "当前使用：{0}　·　点胶囊即可切换（热加载，无需重启）；未下载的档位会先下载。",
                current.Length > 0 ? current : Loc.T("capability.unknown", "未知"));
    }

    /// <summary>胶囊行上的小按钮（编辑/删除）：小字号、圆角样式，与胶囊同一行。</summary>
    private Button MakeProfileActionButton(string caption, string tooltip, Action onClick)
    {
        var button = new Button
        {
            Content = caption,
            FontSize = 10,
            Padding = new Thickness(6, 1, 6, 1),
            Margin = new Thickness(0, 0, 4, 0),
            MinWidth = 0,
            VerticalAlignment = VerticalAlignment.Center,
            ToolTip = tooltip,
        };
        if (TryFindResource("RoundButton") is Style style)
            button.Style = style;
        button.Click += (_, _) => onClick();
        return button;
    }

    private void ProfileRefreshButton_Click(object sender, RoutedEventArgs e) => _ = LoadModelProfilesAsync();

    /// <summary>切换该档：热加载（后端会套用该档自己的上下文与温度）。</summary>
    private async Task ApplyProfileAsync(ModelProfileItem item)
    {
        ProfileStatusText.Text = Loc.T("settings.model.profileApplying", "⏳ 正在切换到 {0} …", item.Name);
        try
        {
            // 冷启动加载大档要几十秒，idleTimeout 按分钟给（与「加载」按钮一致）
            string json = await _backend.SendRequestAsync(
                new { action = "model_profile_apply", alias = item.Alias },
                idleTimeout: TimeSpan.FromMinutes(10));
            using JsonDocument doc = JsonDocument.Parse(json);
            ProfileStatusText.Text = GetString(doc.RootElement, "content");
            await LoadModelProfilesAsync();
            await RefreshHubRegistryAsync();
        }
        catch (Exception ex)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileApplyFailed",
                "❌ 切换失败：{0}", ex.Message);
        }
    }

    /// <summary>
    /// 下载该档：先在该档自己的仓库里找到同名文件，再走与模型市场完全相同的流式下载。
    ///
    /// 为什么要先查详情：`config.models[*].repo` 与 `path` 是**声明**，仓库里真实存在的文件名
    /// 未必逐字相同（例如带 `Qwen_` 前缀），大小也要拿真实的才能正确显示进度。
    /// 找不到同名文件时不动手，而是明确让用户去模型市场挑量化版本。
    /// </summary>
    private async Task DownloadProfileAsync(ModelProfileItem item)
    {
        if (_downloading)
        {
            ProfileStatusText.Text = Loc.T("settings.model.downloadBusy",
                "⏳ 已有下载在进行中，请等待完成或先取消。");
            return;
        }
        if (item.Repo.Length == 0)
        {
            ProfileStatusText.Text = Loc.T("settings.model.profileNoRepo",
                "❌ 该档位没有登记仓库（config.json 的 models.{0}.repo），请到模型市场里手动下载。",
                item.Alias);
            return;
        }
        ProfileStatusText.Text = Loc.T("settings.model.profileResolving",
            "🔍 正在仓库 {0} 里查找 {1} …", item.Repo, item.Name);
        try
        {
            // **source 固定用 auto**，不跟随界面「数据源」下拉框：档位是随包声明的已知公开仓库，
            // auto = 镜像优先、官方兜底（resolve_source 的语义），比"用户上次选的源"更可能成功。
            // 实测教训：界面源若停在「官方」，huggingface.co 在某些网络下直接 21 秒超时。
            string detailJson = await _backend.SendRequestAsync(
                new { action = "model_hub_detail", repo = item.Repo, source = "auto" },
                idleTimeout: TimeSpan.FromSeconds(180));
            using JsonDocument detailDoc = JsonDocument.Parse(detailJson);
            // 键名必须是 "detail"（backend/main.py 的 _handle_model_hub_detail 就返回这个键）。
            // 本方法第一版写的是 "hub_detail"，于是**每次点档位都报"无法获取模型详情"**
            // —— 后端明明返回了完整文件清单，是前端读错了键（用户实测踩到）。
            if (!detailDoc.RootElement.TryGetProperty("detail", out JsonElement detail))
            {
                // 失败时把后端的真实原因显示出来（此前只显示一句笼统的"无法获取模型详情"，
                // 用户看不出是网络、数据源还是仓库名的问题）。
                string reason = GetString(detailDoc.RootElement, "content");
                ProfileStatusText.Text = (reason.Length > 0
                        ? reason
                        : Loc.T("settings.model.detailUnavailable", "⚠️ 无法获取模型详情"))
                    + " " + Loc.T("settings.model.detailFailedHint",
                        "（若反复失败：把「数据源」切回「自动」，或到「模型市场」里手动挑一个量化版本下载。）");
                return;
            }

            HubFileItem? match = null;
            if (detail.TryGetProperty("files", out JsonElement files) && files.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement f in files.EnumerateArray())
                {
                    string name = GetString(f, "filename");
                    if (!string.Equals(name, item.Name, StringComparison.OrdinalIgnoreCase))
                        continue;
                    match = new HubFileItem
                    {
                        Repo = item.Repo,
                        FileName = name,
                        Size = f.TryGetProperty("size", out JsonElement sz) && sz.ValueKind == JsonValueKind.Number
                            ? sz.GetInt64() : 0,
                        SizeText = GetString(f, "size_human"),
                        Quantization = GetString(f, "quantization"),
                    };
                    break;
                }
            }
            if (match is null)
            {
                ProfileStatusText.Text = Loc.T("settings.model.profileFileMissing",
                    "⚠️ 仓库 {0} 里没有找到 {1}。请到「模型市场」里挑一个量化版本下载，或改 config.json 里的档位声明。",
                    item.Repo, item.Name);
                return;
            }
            await DownloadHubFileAsync(match);
            await LoadModelProfilesAsync();
        }
        catch (Exception ex)
        {
            ProfileStatusText.Text = Loc.T("settings.model.downloadFailed", "❌ 下载失败：{0}", ex.Message);
        }
    }

    // ---------- 自定义档位：新增 / 编辑 / 删除（能力扩展-1 任务五 C） ----------
    //
    // 需求背景：此前档位只能"读"与"切换"，要加一档必须手改 config.json 的 models 段。
    // 后端补了 model_profile_save / model_profile_delete 两个写入口（含范围校验），
    // 这里给它们配上界面：胶囊行上的「📝 编辑」「🗑 删除」+ 标题行旁的「＋ 新建档位」。

    /// <summary>正在编辑的档位别名（空 = 新建）。保存时若别名变过，就用它当 rename_from。</summary>
    private string _profileEditingAlias = "";

    // 编辑表单的输入框：表单是按需生成/销毁的，所以每次生成时重新赋值
    private TextBlock? _profileEditorTitle;
    private TextBox? _profileAliasBox;
    private TextBox? _profilePathBox;
    private TextBox? _profileRepoBox;
    private TextBox? _profileContextBox;
    private TextBox? _profileTempBox;
    private TextBox? _profileNoteBox;

    private void ProfileNewButton_Click(object sender, RoutedEventArgs e) => ShowProfileEditor(null);

    /// <summary>
    /// 打开档位编辑表单（内联在档位行下方，而不是弹模态对话框）。
    ///
    /// 为什么用内联表单：字段有六个（别名/文件/仓库/上下文长度/温度/备注），
    /// 逐个弹单行输入框既慢又容易点错；内联表单能一次看全、一次改完，
    /// 也不必为它单开一个 Window 类（仓库里现成的 InputDialog 只支持单字段）。
    /// 它只在编辑期间占高度（约 6 行），取消/保存后立刻清空 —— 模型页的竖向空间要留给结果列表。
    /// </summary>
    private void ShowProfileEditor(ModelProfileItem? item)
    {
        if (ProfileEditorPanel is null)
            return;
        ProfileEditorPanel.Children.Clear();
        _profileEditingAlias = item?.Alias ?? "";

        var card = new Border
        {
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(10, 8, 10, 8),
        };
        ThemeService.Bind(card, Border.BackgroundProperty, "InputBackground");
        ThemeService.Bind(card, Border.BorderBrushProperty, "InputBorder");
        var form = new StackPanel();
        card.Child = form;
        ProfileEditorPanel.Children.Add(card);

        _profileEditorTitle = Identify(new TextBlock
        {
            Text = item is null
                ? Loc.T("settings.profile.editorNew", "新建档位")
                : Loc.T("settings.profile.editorEdit", "编辑档位「{0}」", item.Alias),
            FontSize = 12,
            FontWeight = FontWeights.Bold,
        }, "ProfileEditorTitle");
        ThemeService.Bind(_profileEditorTitle, TextBlock.ForegroundProperty, "AccentBrush");
        form.Children.Add(_profileEditorTitle);

        _profileAliasBox = AddProfileField(form, "ProfileAliasBox",
            Loc.T("settings.profile.alias", "别名"), item?.Alias ?? "",
            Loc.T("settings.profile.aliasHint",
                "档位名（≤32 字符，不能含 \\ / : 等字符）；改名即重命名这一档"));
        // 列表数据里只有文件名（basename），没有 config.json 里写的那条 path，
        // 所以这里回填文件名：留空=不改动，改动一般也就是换一个文件名。
        _profilePathBox = AddProfileField(form, "ProfilePathBox",
            Loc.T("settings.profile.path", "模型文件名"), item?.Name ?? "",
            Loc.T("settings.profile.pathHint",
                "models 目录下的文件名（如 qwen2.5-1.7b-q4_k_m.gguf），也可填绝对路径；留空不改动"));
        _profileRepoBox = AddProfileField(form, "ProfileRepoBox",
            Loc.T("settings.profile.repo", "仓库"), item?.Repo ?? "",
            Loc.T("settings.profile.repoHint",
                "Hugging Face 仓库名（可选，用于「下载」按钮在该仓库里找文件）"));
        _profileContextBox = AddProfileField(form, "ProfileContextBox",
            Loc.T("settings.profile.context", "上下文长度"),
            item is { ContextLength: > 0 } ? item.ContextLength.ToString() : "",
            Loc.T("settings.profile.contextHint",
                "留空沿用该档已有值；上限由 config.json 的 max_context 决定（越界后端会报错）"));
        _profileTempBox = AddProfileField(form, "ProfileTempBox",
            Loc.T("settings.profile.temperature", "温度"),
            item?.Temperature is double temp ? temp.ToString("0.##") : "",
            Loc.T("settings.profile.temperatureHint", "0–1.5；留空沿用该档已有值"));
        // profiles_state 里没有 note 字段 → 回填空；留空=不改动（后端语义：不发这个键就不覆盖）
        _profileNoteBox = AddProfileField(form, "ProfileNoteBox",
            Loc.T("settings.profile.note", "备注"), "",
            Loc.T("settings.profile.noteHint",
                "可选；留空的项一律不改动（备注与完整路径不在档位列表数据里，所以这里不会回填）"));

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 8, 0, 0) };
        var save = Identify(new Button
        {
            Content = Loc.T("settings.profile.save", "💾 保存"),
            FontSize = 11,
            Padding = new Thickness(10, 2, 10, 2),
            MinWidth = 0,
        }, "ProfileEditorSaveButton");
        if (TryFindResource("RoundButton") is Style saveStyle)
            save.Style = saveStyle;
        save.Click += ProfileEditorSave_Click;
        buttons.Children.Add(save);

        var cancel = Identify(new Button
        {
            Content = Loc.T("settings.profile.cancel", "✗ 取消"),
            FontSize = 11,
            Padding = new Thickness(10, 2, 10, 2),
            MinWidth = 0,
            Margin = new Thickness(8, 0, 0, 0),
        }, "ProfileEditorCancelButton");
        if (TryFindResource("RoundButton") is Style cancelStyle)
            cancel.Style = cancelStyle;
        cancel.Click += (_, _) => ProfileEditorPanel?.Children.Clear();
        buttons.Children.Add(cancel);
        form.Children.Add(buttons);

        _profileAliasBox?.Focus();
    }

    /// <summary>表单里的一行：标签 + 输入框（提示放在输入框右侧的小字里）。返回输入框。</summary>
    private TextBox AddProfileField(StackPanel form, string id, string label, string value, string hint)
    {
        var row = new Grid { Margin = new Thickness(0, 3, 0, 0) };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(96) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(240) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        var caption = new TextBlock
        {
            Text = label,
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
        };
        ThemeService.Bind(caption, TextBlock.ForegroundProperty, "SubTextBrush");
        Grid.SetColumn(caption, 0);
        row.Children.Add(caption);

        var box = Identify(new TextBox
        {
            Text = value,
            FontSize = 11,
            Padding = new Thickness(4, 2, 4, 2),
            VerticalAlignment = VerticalAlignment.Center,
        }, id);
        Grid.SetColumn(box, 1);
        row.Children.Add(box);

        var hintBlock = new TextBlock
        {
            Text = hint,
            FontSize = 10,
            Margin = new Thickness(8, 0, 0, 0),
            VerticalAlignment = VerticalAlignment.Center,
            TextWrapping = TextWrapping.Wrap,
            ToolTip = hint,
        };
        ThemeService.Bind(hintBlock, TextBlock.ForegroundProperty, "SubTextBrush");
        Grid.SetColumn(hintBlock, 2);
        row.Children.Add(hintBlock);

        form.Children.Add(row);
        return box;
    }

    private async void ProfileEditorSave_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await SaveProfileAsync();
        }
        catch (Exception ex)
        {
            // async void 处理器：异常无人接住会冒到进程级
            SetProfileStatus(Loc.T("settings.profile.saveFailed", "❌ 保存失败：{0}", ex.Message));
            App.Log("SettingsWindow.ProfileEditorSave", ex);
        }
    }

    private async Task SaveProfileAsync()
    {
        if (_profileAliasBox is null)
            return;
        string alias = _profileAliasBox.Text.Trim();
        if (alias.Length == 0)
        {
            SetProfileStatus(Loc.T("settings.profile.aliasRequired", "⚠️ 别名不能为空"));
            return;
        }
        string tempText = _profileTempBox?.Text.Trim() ?? "";
        // 温度是唯一一项"界面明知合法范围"的字段（0–1.5），所以在客户端先挡一道；
        // 其余字段（上下文长度上限、别名长度、路径合法性）**一律不在这里夹紧**：
        // 把原值直接发给后端，让后端返回它的 ❌ 文案显示出来（接口夹紧=骗人，
        // 后端 main.py 的同名注释写过这个教训，前端不重复第二套规则）。
        if (tempText.Length > 0)
        {
            if (!double.TryParse(tempText, NumberStyles.Float, CultureInfo.InvariantCulture, out double temp))
            {
                SetProfileStatus(Loc.T("settings.profile.tempInvalid", "⚠️ 温度需要是数字（{0}–{1}）", 0, 1.5));
                return;
            }
            if (temp < 0.0 || temp > 1.5)
            {
                SetProfileStatus(Loc.T("settings.profile.tempRange", "⚠️ 温度需要在 {0}–{1} 之间", 0, 1.5));
                return;
            }
        }

        var patch = new Dictionary<string, object?>
        {
            ["action"] = "model_profile_save",
            ["alias"] = alias,
        };
        // 只发**填了内容**的字段：后端把空字符串当成"删掉这一项"，而界面上空输入框
        // 更可能是"我没动它"。少发一个键 = 不覆盖已有值（后端的"缺省字段不覆盖"语义），
        // 这正是"没填就不改"该有的样子。
        AddProfileFieldValue(patch, "path", _profilePathBox);
        AddProfileFieldValue(patch, "repo", _profileRepoBox);
        AddProfileFieldValue(patch, "note", _profileNoteBox);
        AddProfileFieldValue(patch, "context_length", _profileContextBox);
        AddProfileFieldValue(patch, "temperature", _profileTempBox);
        if (_profileEditingAlias.Length > 0
            && !string.Equals(_profileEditingAlias, alias, StringComparison.Ordinal))
        {
            patch["rename_from"] = _profileEditingAlias;   // 改名：后端按 rename_from 搬运原登记
        }

        SetProfileStatus(Loc.T("settings.profile.saving", "正在保存档位…"));
        try
        {
            string json = await _backend.SendRequestAsync(patch);
            using JsonDocument doc = JsonDocument.Parse(json);
            ApplyProfileWriteResult(doc.RootElement, "model_profile_save");
        }
        catch (Exception ex)
        {
            SetProfileStatus(Loc.T("settings.profile.saveFailed", "❌ 保存失败：{0}", ex.Message));
        }
    }

    /// <summary>删除档位登记（**不删磁盘文件**）：先二次确认，确认文案里写明这一点。</summary>
    private async Task DeleteProfileAsync(ModelProfileItem item)
    {
        MessageBoxResult answer = MessageBox.Show(
            Loc.T("settings.profile.deleteConfirmText",
                "确定删除档位「{0}」吗？\n\n只删除 config.json 里的这条登记，不会删除磁盘上的模型文件；"
                + "之后仍可用「＋ 新建档位」把它登记回来。", item.Alias),
            Loc.T("settings.profile.deleteConfirmTitle", "删除模型档位"),
            MessageBoxButton.YesNo, MessageBoxImage.Warning);
        if (answer != MessageBoxResult.Yes)
            return;
        SetProfileStatus(Loc.T("settings.profile.deleting", "正在删除档位…"));
        try
        {
            string json = await _backend.SendRequestAsync(
                new { action = "model_profile_delete", alias = item.Alias });
            using JsonDocument doc = JsonDocument.Parse(json);
            ApplyProfileWriteResult(doc.RootElement, "model_profile_delete");
        }
        catch (Exception ex)
        {
            SetProfileStatus(Loc.T("settings.profile.deleteFailed", "❌ 删除失败：{0}", ex.Message));
        }
    }

    /// <summary>
    /// 处理档位写操作的响应：先用回传的档位表重排胶囊，再写结果文本。
    ///
    /// **顺序不能反**：RenderModelProfiles 自己会写一行"当前使用：…"的提示，
    /// 先写结果文本会被它顶掉（后端那句 ❌ 提示就看不见了）。
    /// </summary>
    private void ApplyProfileWriteResult(JsonElement root, string resultKey)
    {
        bool ok = root.TryGetProperty(resultKey, out JsonElement result)
                  && result.TryGetProperty("ok", out JsonElement okEl)
                  && okEl.ValueKind == JsonValueKind.True;
        if (root.TryGetProperty("model_profiles", out JsonElement state)
            && state.ValueKind == JsonValueKind.Object)
        {
            RenderModelProfiles(state);
        }
        string content = GetString(root, "content");
        SetProfileStatus(content.Length > 0
            ? content
            : ok ? Loc.T("settings.profile.saved", "✅ 已保存")
                 : Loc.T("settings.profile.opFailed", "❌ 操作失败"));
        // 成功才关表单：失败时保留用户刚填的内容，改一处就能重试（不必重输六个字段）
        if (ok)
            ProfileEditorPanel?.Children.Clear();
    }

    private static void AddProfileFieldValue(Dictionary<string, object?> patch, string key, TextBox? box)
    {
        string text = box?.Text.Trim() ?? "";
        if (text.Length > 0)
            patch[key] = text;   // 数字字段也发字符串：后端自己解析，不在这里夹紧
    }

    private void SetProfileStatus(string text)
    {
        if (ProfileStatusText is null)
            return;
        ProfileStatusText.Text = text;
    }
}
