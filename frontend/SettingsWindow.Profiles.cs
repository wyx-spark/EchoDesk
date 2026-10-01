using System;
using System.Collections.Generic;
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
                    Meta = string.Join(" · ", new[]
                    {
                        GetString(p, "params"),
                        GetString(p, "quantization"),
                        ctx > 0 ? Loc.T("settings.model.profileContext", "{0} 上下文", ctx) : "",
                    }.Where(x => x.Length > 0)),
                };
                var chip = new Button
                {
                    Content = item.ChipText,
                    Tag = item,
                    ToolTip = item.ToolTipText,
                    FontSize = 11,
                    Padding = new Thickness(10, 3, 10, 3),
                    Margin = new Thickness(0, 0, 8, 0),
                    // 当前档不需要再点（点了也是空操作），但仍然显示出来让用户看清现状
                    IsEnabled = !item.Current,
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
                ModelProfileChips.Children.Add(chip);
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
            string detailJson = await _backend.SendRequestAsync(
                new { action = "model_hub_detail", repo = item.Repo, source = CurrentHubSource() },
                idleTimeout: TimeSpan.FromSeconds(180));
            using JsonDocument detailDoc = JsonDocument.Parse(detailJson);
            if (!detailDoc.RootElement.TryGetProperty("hub_detail", out JsonElement detail))
            {
                ProfileStatusText.Text = Loc.T("settings.model.detailUnavailable",
                    "⚠️ 无法获取模型详情");
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
}
