// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ContextPage.axaml.cs.
// [ZH] EchoDesk 跨平台前端模块：context 页签的交互逻辑。
// [EN] Context window page (v0.2.6-reader). It turns one technical decision into four
// [EN] plain choices (how long a document a single read can hold) and never asks the
// [EN] user to know what RoPE scaling is: the extension mode is derived from the chosen
// [EN] window and is only sent when the window exceeds the model's own capability.
// [ZH] 上下文窗口页（v0.2.6-reader）。它把一个技术决定变成几个朴素选择（一次能读多长的
// [ZH] 文档），且从不要求用户知道 RoPE 缩放是什么：扩展方式由所选窗口推导，只有窗口
// [ZH] 超过模型自带能力时才会随请求下发。
//
// [EN] Backend contract (shared with ModelPage):
// [ZH] 后端契约（与模型页一致）：
//   {"action": "context"}                     → { n_ctx, context_length, n_ctx_native,
//                                                 n_ctx_hard_limit, model_ready, ... }
//   {"action": "capability_settings", "n_ctx", "n_ctx_max", "rope_type", "rope_scale",
//    "rope_orig_ctx", "allow_override"}        → { ok, notice, context: {...} }
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Interactivity;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class ContextPage : UserControl, ISettingsPage
{
    private ISettingsHost? _host;
    private bool _applying;

    // [EN] Snapshot of the backend state, refreshed by <see cref="RefreshAsync"/>.
    // [ZH] 后端状态快照，由 <see cref="RefreshAsync"/> 刷新。
    private int _currentCtx;
    private int _nativeCtx;
    private int _hardLimit;
    private bool _modelReady;

    /// <summary>
    /// [EN] Hard lower bound for a custom window; matches the backend's MIN_CONTEXT so the
    /// page cannot submit a value the backend will reject.
    /// [ZH] 自定义窗口的绝对下界；与后端 MIN_CONTEXT 一致，避免提交必然被拒的取值。
    /// </summary>
    private const int MinCustomContext = 2048;

    private sealed record Tier(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    public ContextPage()
    {
        InitializeComponent();
        BuildTiers();
        HookEvents();
    }

    public string TabKey => "settings.tab.context";
    public string TabFallback => "上下文";
    public string AutomationId => "SettingsTabContext";

    public void Attach(ISettingsHost host) => _host = host;
    public void LoadFrom(AppSettings settings) { }
    public void ApplyTo(AppSettings settings) { }

    /// <summary>
    /// [EN] The shell's save button ultimately calls this page's save path.
    /// [ZH] 骨架的保存按钮最终调用本页的保存流程。
    /// </summary>
    public Task ApplyToBackendAsync() => SaveAsync();

    /// <summary>
    /// [EN] Pull the context snapshot when the page is opened.
    /// [ZH] 打开页面时拉取上下文快照。
    /// </summary>
    public async Task RefreshAsync() => await LoadContextAsync();

    public async Task RefreshQuietlyAsync()
    {
        try
        {
            await RefreshAsync();
        }
        catch (Exception)
        {
            // [EN] A probe or a page switch must never crash because diagnostics failed.
            // [ZH] 诊断失败绝不能让探针或页签切换崩溃。
        }
    }

    // -----------------------------------------------------------------
    // [EN] Construction
    // [ZH] 构造
    // -----------------------------------------------------------------

    private void BuildTiers()
    {
        // [EN] The tier list is rebuilt on every load because the "model hard limit" and the
        // [EN] "recommended" figure depend on the model currently loaded.
        // [ZH] 每次载入都重建档位列表：「模型硬上限」与推荐值取决于当前载入的模型。
        int native = _nativeCtx > 0 ? _nativeCtx : _hardLimit;
        var tiers = new List<Tier>
        {
            new("32k", () => Loc.T("settings.context.tier32k", "32K tokens（约 2 万字）")),
            new("64k", () => Loc.T("settings.context.tier64k", "64K tokens（约 4 万字）")),
            new("128k", () => Loc.T("settings.context.tier128k", "128K tokens（约 8 万字）")),
            new("256k", () => Loc.T("settings.context.tier256k", "256K tokens（约 16 万字）")),
            new("model", () => Loc.T("settings.context.tierModel",
                "模型硬上限（{0} tokens）", native > 0 ? native : 0)),
            new("custom", () => Loc.T("settings.context.tierCustom", "自定义")),
        };
        TierCombo.ItemsSource = tiers;
        SelectTierFor(_currentCtx);
    }

    private void HookEvents()
    {
        TierCombo.SelectionChanged += (_, _) =>
        {
            if (_applying)
                return;
            UpdateCustomPanel();
            AutoCheckExtension();
        };
        CustomBox.LostFocus += (_, _) => AutoCheckExtension();
        ExtendCheck.IsCheckedChanged += (_, _) => UpdateExtendHint();
        SaveButton.Click += async (_, _) => await SaveAsync();
        RestoreButton.Click += async (_, _) => await RestoreRecommendedAsync();
    }

    // -----------------------------------------------------------------
    // [EN] Text
    // [ZH] 文案
    // -----------------------------------------------------------------

    public void ApplyTexts()
    {
        Title.Text = Loc.T("settings.context.title", "📐 上下文窗口");
        TierLabel.Text = Loc.T("settings.context.tierLabel", "一次能读多长的文档");
        CustomLabel.Text = Loc.T("settings.context.customLabel", "自定义大小（tokens）");
        ExtendCheck.Content = Loc.T("settings.context.extendLabel", "自动启用长上下文扩展");
        SaveButton.Content = Loc.T("settings.button.save", "保存");
        RestoreButton.Content = Loc.T("settings.context.restoreRecommended", "恢复推荐值");
        InfoTitle.Text = Loc.T("settings.context.title", "📐 上下文窗口");
        Note1.Text = Loc.T("settings.context.note1",
            "窗口越大，一次能读的文档越长，但占用的内存也越多。");
        Note2.Text = Loc.T("settings.context.note2",
            "超过模型自带能力时，回答质量可能下降；程序会自动选用合适的扩展方式，你无需了解技术细节。");
        Note3.Text = Loc.T("settings.context.note3",
            "如果模型文件已自带长上下文设置，程序会自动忽略额外设置，避免输出乱码。");
        // [EN] Tier labels are built from localized text: rebuild the list, keep the choice.
        // [ZH] 档位标签来自本地化文案：重建列表并保留当前选择。
        string selected = SelectedTier();
        BuildTiers();
        SelectTier(selected);
        UpdateCustomPanel();
        UpdateExtendHint();
        UpdateSummary();
    }

    // -----------------------------------------------------------------
    // [EN] Probe support: a compact, machine-checkable snapshot. It never clicks save, so
    // [EN] the probe cannot trigger a multi-minute engine rebuild.
    // [ZH] 探针支持：紧凑、可断言的快照。它不会点保存，因此探针不会触发数分钟的引擎重建。
    // -----------------------------------------------------------------

    public string ProbeSnapshot()
    {
        int notes = new[] { Note1.Text, Note2.Text, Note3.Text }
            .Count(text => !string.IsNullOrWhiteSpace(text));
        return $"tierCount={TierCombo.ItemCount}"
               + $"｜extendLabel={(string.IsNullOrWhiteSpace(ExtendCheck.Content as string) ? 0 : 1)}"
               + $"｜extendHint={(string.IsNullOrWhiteSpace(ExtendHint.Text) ? 0 : 1)}"
               + $"｜noteCount={notes}"
               + $"｜status={(string.IsNullOrWhiteSpace(StatusText.Text) ? 0 : 1)}"
               + $"｜saveStatus={(string.IsNullOrWhiteSpace(SaveStatusText.Text) ? 0 : 1)}";
    }

    // -----------------------------------------------------------------
    // [EN] Backend round trips
    // [ZH] 与后端往返
    // -----------------------------------------------------------------

    private async Task<JsonDocument?> SendAsync(object request, TimeSpan? idle = null)
    {
        if (_host is null)
            return null;
        return await _host.State.ActiveBackend.SendAsync(
            request, idleTimeout: idle ?? TimeSpan.FromSeconds(60));
    }

    private async Task LoadContextAsync()
    {
        _applying = true;
        try
        {
            using JsonDocument? doc = await SendAsync(new { action = "context" });
            if (doc is not null)
                ApplySnapshot(doc.RootElement);
        }
        catch (Exception)
        {
            // [EN] Diagnostics are advisory: keep the last known values on failure.
            // [ZH] 诊断属于辅助信息：失败时保留上一次的取值。
        }
        finally
        {
            _applying = false;
        }
        ApplyTexts();
        UpdateSummary();
    }

    private void ApplySnapshot(JsonElement root)
    {
        _currentCtx = GetInt(root, "context_length", GetInt(root, "n_ctx", 0));
        _nativeCtx = GetInt(root, "n_ctx_native", 0);
        _hardLimit = GetInt(root, "n_ctx_hard_limit", GetInt(root, "n_ctx_max", 0));
        _modelReady = GetBool(root, "model_ready");
        BuildTiers();
    }

    private async Task SaveAsync()
    {
        if (_host is null || _applying)
            return;
        int target = ResolveTargetContext();
        if (target < MinCustomContext)
        {
            SaveStatusText.Text = Loc.T("settings.context.invalidCustom",
                "⚠️ 自定义大小需为不小于 2048 的整数");
            return;
        }

        // [EN] Extension mode is derived, never asked of the user: a window wider than the
        // [EN] model's own capability needs scaling, otherwise the model sees positions it
        // [EN] was never trained for. The checkbox is the user's consent to that trade-off.
        // [ZH] 扩展方式由程序推导，不问用户：窗口宽于模型自带能力就需要缩放，否则模型会
        // [ZH] 看到它从未训练过的位置。复选框就是用户对这一取舍的确认。
        int native = _nativeCtx > 0 ? _nativeCtx : target;
        bool wantsExtension = ExtendCheck.IsChecked == true && target > native;
        string ropeType = "none";
        double scale = 1.0;
        int origCtx = 0;
        if (wantsExtension)
        {
            ropeType = "yarn";
            scale = Math.Round((double)target / Math.Max(1, native), 3);
            if (scale < 1.0)
                scale = 1.0;
            origCtx = native;
        }

        SaveStatusText.Text = Loc.T("settings.context.rebuilding",
            "⏳ 正在按新窗口重建引擎（可能需要几分钟）…");
        try
        {
            using JsonDocument? doc = await SendAsync(new
            {
                action = "capability_settings",
                n_ctx = target,
                // [EN] Raise the stored ceiling together with the window, so the choice is
                // [EN] not silently clamped back by the previous ceiling.
                // [ZH] 窗口与上限一起抬高，避免选择被旧上限悄悄截回。
                n_ctx_max = target,
                rope_type = ropeType,
                rope_scale = scale,
                rope_orig_ctx = origCtx,
                allow_override = true,
            }, TimeSpan.FromMinutes(10));

            if (doc is null)
            {
                SaveStatusText.Text = Loc.T("settings.context.saveFailed", "❌ 保存失败：{0}",
                    Loc.T("main.error.noResponse", "无响应"));
                return;
            }
            JsonElement root = doc.RootElement;
            bool ok = !(root.TryGetProperty("ok", out JsonElement okElement)
                        && okElement.ValueKind == JsonValueKind.False);
            if (!ok)
            {
                string error = GetString(root, "error");
                if (error.Length == 0)
                    error = GetString(root, "content");
                SaveStatusText.Text = Loc.T("settings.context.saveFailed", "❌ 保存失败：{0}",
                    error);
                return;
            }

            string notice = GetString(root, "notice");
            if (root.TryGetProperty("context", out JsonElement context)
                && context.ValueKind == JsonValueKind.Object)
                ApplySnapshot(context);

            string suffix = notice.Length > 0 ? "｜" + notice : "";
            SaveStatusText.Text = Loc.T("settings.context.saved",
                "✅ 已保存：窗口 {0} tokens{1}", target, suffix);
            _host.ShowStatus(Loc.T("settings.context.saved",
                "✅ 已保存：窗口 {0} tokens{1}", target, ""));
            _host.MarkDirty();
            UpdateSummary();
        }
        catch (Exception exc)
        {
            SaveStatusText.Text = Loc.T("settings.context.saveFailed", "❌ 保存失败：{0}",
                exc.Message);
        }
    }

    /// <summary>
    /// [EN] Restore the shipped recommendation for this machine: the backend's own
    /// recommendation is the ceiling it computed for the hardware, so the page asks the
    /// backend for it indirectly by clearing the explicit override.
    /// [ZH] 恢复本机的随包推荐值：后端按硬件算出的推荐值就是它的上限，因此本页通过
    /// 清除显式覆盖来间接取回该推荐值。
    /// </summary>
    private async Task RestoreRecommendedAsync()
    {
        if (_host is null)
            return;
        SaveStatusText.Text = Loc.T("settings.context.rebuilding",
            "⏳ 正在按新窗口重建引擎（可能需要几分钟）…");
        try
        {
            using JsonDocument? doc = await SendAsync(new
            {
                action = "capability_settings",
                n_ctx = 0,
                allow_override = false,
            }, TimeSpan.FromMinutes(10));
            if (doc is not null)
            {
                JsonElement root = doc.RootElement;
                if (root.TryGetProperty("context", out JsonElement context)
                    && context.ValueKind == JsonValueKind.Object)
                    ApplySnapshot(context);
                SaveStatusText.Text = Loc.T("settings.context.saved",
                    "✅ 已保存：窗口 {0} tokens{1}",
                    _currentCtx, "");
                UpdateSummary();
            }
        }
        catch (Exception exc)
        {
            SaveStatusText.Text = Loc.T("settings.context.saveFailed", "❌ 保存失败：{0}",
                exc.Message);
        }
    }

    // -----------------------------------------------------------------
    // [EN] Tier helpers
    // [ZH] 档位辅助
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Target window for the current selection. Custom reads the text box; the model
    /// tier uses the model's own capability and falls back to the ceiling when unknown.
    /// [ZH] 当前选择对应的目标窗口。自定义读取输入框；模型档使用模型自带能力，未知时退回上限。
    /// </summary>
    private int ResolveTargetContext()
    {
        switch (SelectedTier())
        {
            case "32k": return 32768;
            case "64k": return 65536;
            case "128k": return 131072;
            case "256k": return 262144;
            case "model":
                return _nativeCtx > 0 ? _nativeCtx : Math.Max(_hardLimit, MinCustomContext);
            case "custom":
                return int.TryParse((CustomBox.Text ?? "").Trim(), out int value) ? value : 0;
            default:
                return Math.Max(_currentCtx, MinCustomContext);
        }
    }

    private string SelectedTier()
        => TierCombo.SelectedItem is Tier tier ? tier.Value : "";

    private void SelectTier(string value)
    {
        if (TierCombo.ItemsSource is not IEnumerable<Tier> tiers)
            return;
        foreach (Tier tier in tiers)
        {
            if (tier.Value == value)
            {
                TierCombo.SelectedItem = tier;
                return;
            }
        }
        if (TierCombo.ItemCount > 0)
            TierCombo.SelectedIndex = TierCombo.ItemCount - 1;
    }

    private void SelectTierFor(int context)
    {
        string tier = context switch
        {
            >= 262144 => "256k",
            >= 131072 => "128k",
            >= 65536 => "64k",
            >= 32768 => "32k",
            _ => "custom",
        };
        SelectTier(tier);
        if (tier == "custom" && context > 0)
            CustomBox.Text = context.ToString();
        UpdateCustomPanel();
        AutoCheckExtension();
    }

    private void UpdateCustomPanel()
    {
        bool custom = SelectedTier() == "custom";
        CustomPanel.IsVisible = custom;
        if (custom && string.IsNullOrWhiteSpace(CustomBox.Text) && _currentCtx > 0)
            CustomBox.Text = _currentCtx.ToString();
    }

    /// <summary>
    /// [EN] Extension is switched on automatically only when the chosen window is wider than
    /// the model's own capability; otherwise it stays off so the default path is unchanged.
    /// [ZH] 只有所选窗口宽于模型自带能力时才自动勾选扩展；其余情况保持关闭，默认路径不变。
    /// </summary>
    private void AutoCheckExtension()
    {
        if (_applying)
            return;
        int target = ResolveTargetContext();
        int native = _nativeCtx > 0 ? _nativeCtx : target;
        if (target > native)
            ExtendCheck.IsChecked = true;
        UpdateExtendHint();
        UpdateSummary();
    }

    private void UpdateExtendHint()
    {
        ExtendHint.Text = Loc.T("settings.context.extendHint",
            "窗口超过模型自带能力时才会生效");
        ExtendCheck.IsEnabled = _nativeCtx <= 0
            || ResolveTargetContext() > _nativeCtx;
    }

    private void UpdateSummary()
    {
        if (!_modelReady && _nativeCtx <= 0)
        {
            StatusText.Text = Loc.T("settings.context.modelMissing",
                "尚未检测到模型。窗口设置会在载入模型后生效。");
            return;
        }
        string current = Loc.T("settings.context.currentText", "当前窗口：{0} tokens", _currentCtx);
        string native = Loc.T("settings.context.nativeText", "模型自带能力：{0} tokens", _nativeCtx);
        string limit = Loc.T("settings.context.limitText", "可设置上限：{0} tokens", _hardLimit);
        StatusText.Text = $"{current}｜{native}｜{limit}";
    }

    // -----------------------------------------------------------------
    // [EN] JSON helpers (same defensive style as ModelPage)
    // [ZH] JSON 辅助（与模型页一致的防御式写法）
    // -----------------------------------------------------------------

    private static int GetInt(JsonElement element, string name, int fallback)
    {
        if (element.ValueKind == JsonValueKind.Object
            && element.TryGetProperty(name, out JsonElement value)
            && value.ValueKind == JsonValueKind.Number
            && value.TryGetInt32(out int number))
            return number;
        return fallback;
    }

    private static bool GetBool(JsonElement element, string name)
        => element.ValueKind == JsonValueKind.Object
           && element.TryGetProperty(name, out JsonElement value)
           && value.ValueKind == JsonValueKind.True;

    private static string GetString(JsonElement element, string name)
        => element.ValueKind == JsonValueKind.Object
           && element.TryGetProperty(name, out JsonElement value)
           && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";
}
