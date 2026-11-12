// [EN] EchoDesk Avalonia frontend module: Ecosystem settings page (v0.2.6 Part 4).
// [ZH] EchoDesk 前端模块：生态设置页（v0.2.6 任务 4）。
//
// [EN] Three behaviours are worth stating before the code:
// [EN]
// [EN] 1. **The consent dialog is unavoidable.** Every save of an external endpoint
// [EN]    opens <see cref="ConsentDialog"/>, and refusing it aborts the save without
// [EN]    sending any request. The flag sent to the backend is only ever true when
// [EN]    the user just ticked the acknowledgement.
// [EN] 2. **Stored secrets are never echoed.** `provider_state` and `local_api_state`
// [EN]    report whether a key exists, not its value. An empty key box therefore means
// [EN]    "unchanged", and the request omits the field so the backend keeps the stored
// [EN]    value; explicitly clearing a key is deliberately not offered in this version.
// [EN] 3. **The front end is not the security boundary.** The local-endpoint test in
// [EN]    this file only decides whether to show the warning panel and dialog. The
// [EN]    backend repeats the check (`providers.apply_settings` raises
// [EN]    ``PermissionError``), so a mistake here cannot cause a silent data leak.
// [ZH] 代码之前先说明三件事：
// [ZH]
// [ZH] 1. **同意对话框不可绕过。** 每次保存外部端点都会打开 <see cref="ConsentDialog"/>，
// [ZH]    拒绝即中止保存且不发送任何请求。发给后端的标记只有在用户刚刚勾选确认时才为真。
// [ZH] 2. **已保存的密钥绝不回显。** `provider_state` 与 `local_api_state` 只报告"是否存在"，
// [ZH]    不报告取值。因此密钥输入框为空表示"保持不变"，请求省略该字段，由后端沿用已存值；
// [ZH]    本版刻意不提供"显式清空密钥"的入口。
// [ZH] 3. **前端不是安全边界。** 本文件里的本地端点判断只决定是否显示警告面板与对话框；
// [ZH]    后端会重复校验（`providers.apply_settings` 抛 ``PermissionError``），
// [ZH]    因此这里的失误不会造成静默的数据泄露。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class EcosystemPage : UserControl, ISettingsPage
{
    /// <summary>
    /// [EN] Settings host, used to reach the shared backend and to mark unsaved changes.
    /// [ZH] 设置宿主，用于访问共享后端并标记未保存改动。
    /// </summary>
    private ISettingsHost? _host;

    /// <summary>
    /// [EN] Suppress change handlers while controls are being filled in, so opening the
    /// [EN] page does not immediately report "unsaved changes".
    /// [ZH] 填充控件时抑制变更事件，避免一打开本页就显示"有未保存改动"。
    /// </summary>
    private bool _applying;

    /// <summary>
    /// [EN] Previously granted informed consent for external endpoints. It is preserved
    /// [EN] when saving a purely local configuration, because switching back to the
    /// [EN] local engine does not undo a grant the user already made explicitly.
    /// [ZH] 此前已获得的外部端点知情同意。保存纯本地配置时保留该值，因为切回本地引擎
    /// [ZH] 并不会撤销用户此前显式做出的授权。
    /// </summary>
    private bool _consentGranted;

    /// <summary>
    /// [EN] UTC timestamp of the recorded grant, shown for the audit trail.
    /// [ZH] 已记录授权的 UTC 时间戳，用于审计留痕展示。
    /// </summary>
    private string _consentAt = "";

    /// <summary>
    /// [EN] Active provider kind reported by the backend.
    /// [ZH] 后端报告的当前生效提供方类型。
    /// </summary>
    private string _activeKind = "llamacpp";

    /// <summary>
    /// [EN] Provider kind currently selected in the drop-down (may differ from
    /// [EN] <see cref="_activeKind"/> until the user saves).
    /// [ZH] 下拉框中当前选中的提供方类型（在用户保存之前可能与
    /// [ZH] <see cref="_activeKind"/> 不同）。
    /// </summary>
    private string _currentKind = "llamacpp";

    /// <summary>
    /// [EN] Unsaved edits per provider kind, so switching the drop-down does not lose
    /// [EN] what the user typed.
    /// [ZH] 按提供方类型保存的未保存编辑，使切换下拉项时不丢失用户已输入的内容。
    /// </summary>
    private readonly Dictionary<string, ProviderDraft> _drafts = new(StringComparer.Ordinal);

    /// <summary>
    /// [EN] Provider rows reported by the backend (endpoint, model, whether a key exists).
    /// [ZH] 后端报告的提供方行（端点、模型、是否已保存密钥）。
    /// </summary>
    private List<ProviderRow> _rows = new();

    /// <summary>
    /// [EN] One drop-down item; <c>Text</c> resolves lazily so a language switch updates it.
    /// [ZH] 一个下拉项；<c>Text</c> 惰性求值，因此切换语言会同步更新。
    /// </summary>
    private sealed record Option(string Value, Func<string> Text)
    {
        public override string ToString() => Text();
    }

    /// <summary>
    /// [EN] Unsaved text fields of one provider kind.
    /// [ZH] 某一提供方类型的未保存文本字段。
    /// </summary>
    private sealed class ProviderDraft
    {
        public string ApiBase = "";
        public string Model = "";
        public string ApiKey = "";
    }

    /// <summary>
    /// [EN] Read-only provider row from the backend state.
    /// [ZH] 来自后端状态的只读提供方行。
    /// </summary>
    private sealed class ProviderRow
    {
        public string Kind = "";
        public string ApiBase = "";
        public string Model = "";
        public bool Enabled;
        public bool KeySet;
    }

    public EcosystemPage()
    {
        InitializeComponent();
        BuildOptions();
        HookEvents();
    }

    public string TabKey => "settings.tab.ecosystem";
    public string TabFallback => "生态";
    public string AutomationId => "SettingsTabEcosystem";

    public void Attach(ISettingsHost host) => _host = host;

    /// <summary>
    /// [EN] This page owns no UI preference: everything it shows lives in the backend
    /// [EN] configuration (business settings), so there is nothing to copy from ui_state.json.
    /// [ZH] 本页不持有任何界面偏好：它显示的全部内容都属于后端的业务配置，因此没有需要从
    /// [ZH] ui_state.json 复制的内容。
    /// </summary>
    public void LoadFrom(AppSettings settings)
    {
    }

    /// <summary>
    /// [EN] See <see cref="LoadFrom"/>: no UI preference is collected here.
    /// [ZH] 见 <see cref="LoadFrom"/>：此处不收集界面偏好。
    /// </summary>
    public void ApplyTo(AppSettings settings)
    {
    }

    /// <summary>
    /// [EN] Saving is driven by the section buttons (each one immediately validates and
    /// [EN] applies through the backend), so the window-level save has nothing to do. This
    /// [EN] mirrors the MCP section, which also saves on its own button.
    /// [ZH] 保存由各区块的按钮驱动（点击即校验并立即生效），因此窗口级保存无需额外动作。
    /// [ZH] 这与 MCP 区块一致：它也是由自己的按钮完成保存。
    /// </summary>
    public Task ApplyToBackendAsync() => Task.CompletedTask;

    // -----------------------------------------------------------------
    // [EN] Construction and events
    // [ZH] 构造与事件
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Builds the provider drop-down. The labels are resolved through <see cref="Loc.T"/>
    /// [EN] at render time, which is why the item is a record rather than a plain string.
    /// [ZH] 构建提供方下拉框。文案在渲染时经 <see cref="Loc.T"/> 解析，因此项类型是 record
    /// [ZH] 而不是纯字符串。
    /// </summary>
    private void BuildOptions()
    {
        _applying = true;
        ProviderKindCombo.ItemsSource = new List<Option>
        {
            new("llamacpp", () => Loc.T("settings.ecosystem.kindLocal",
                "llama.cpp（本地，不联网）")),
            new("ollama", () => Loc.T("settings.ecosystem.kindOllama",
                "Ollama（默认本地守护进程）")),
            new("openai_compatible", () => Loc.T("settings.ecosystem.kindOpenAi",
                "OpenAI 兼容（自定义端点）")),
        };
        ProviderKindCombo.SelectedIndex = 0;
        _applying = false;
    }

    /// <summary>
    /// [EN] Wires every interactive control. Handlers stay small: they either update the
    /// [EN] local warning panel or send one request to the backend.
    /// [ZH] 连接所有交互控件。处理器保持精简：要么更新本地警告面板，要么向后端发送一个请求。
    /// </summary>
    private void HookEvents()
    {
        EcosystemReloadButton.Click += async (_, _) => await RefreshAsync();

        LocalApiApplyButton.Click += async (_, _) => await ApplyLocalApiAsync();
        LocalApiEnabledCheck.IsCheckedChanged += (_, _) =>
        {
            if (!_applying)
                _host?.MarkDirty();
        };

        ProviderSaveButton.Click += async (_, _) => await SaveProviderAsync();
        ProviderTestButton.Click += async (_, _) => await TestProviderAsync();
        ProviderKindCombo.SelectionChanged += (_, _) =>
        {
            if (_applying)
                return;
            CaptureDraft();
            _currentKind = (ProviderKindCombo.SelectedItem as Option)?.Value ?? "llamacpp";
            LoadDraft(_currentKind);
            UpdateConsentPanel();
            _host?.MarkDirty();
        };
        ProviderBaseBox.TextChanged += (_, _) =>
        {
            if (!_applying)
                UpdateConsentPanel();
        };
    }

    // -----------------------------------------------------------------
    // [EN] Data loading
    // [ZH] 数据加载
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Fetches both sections from the backend (called when the page is shown or
    /// [EN] the language is rebuilt). A failed section keeps the previous values and
    /// [EN] reports the failure in its status line.
    /// [ZH] 从后端获取两个区块的数据（在页面显示或语言重建时调用）。某一区块失败时保留原有
    /// [ZH] 取值，并在其状态行报告失败。
    /// </summary>
    public async Task RefreshAsync()
    {
        await LoadProviderStateAsync();
        await LoadLocalApiStateAsync();
    }

    private async Task LoadProviderStateAsync()
    {
        using JsonDocument? doc = await SendAsync(new { action = "provider_state" });
        if (doc is null || !doc.RootElement.TryGetProperty("providers", out JsonElement state))
        {
            ProviderStatusText.Text = Loc.T("settings.ecosystem.readUnavailable",
                "⚠️ 未能读取提供方状态（后端未响应），可点「↻ 重新读取」重试。");
            return;
        }
        RenderProviderState(state);
    }

    private async Task LoadLocalApiStateAsync()
    {
        using JsonDocument? doc = await SendAsync(new { action = "local_api_state" });
        if (doc is null || !doc.RootElement.TryGetProperty("local_api", out JsonElement state))
        {
            LocalApiStatusText.Text = Loc.T("settings.ecosystem.readUnavailable",
                "⚠️ 未能读取提供方状态（后端未响应），可点「↻ 重新读取」重试。");
            return;
        }
        RenderLocalApiState(state);
    }

    // -----------------------------------------------------------------
    // [EN] Rendering
    // [ZH] 渲染
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Renders the provider state: active kind, timeout, rows and consent record.
    /// [ZH] 渲染提供方状态：当前生效类型、超时、行数据与授权记录。
    /// </summary>
    private void RenderProviderState(JsonElement state)
    {
        _applying = true;
        try
        {
            _activeKind = GetString(state, "active");
            if (_activeKind.Length == 0)
                _activeKind = "llamacpp";
            _consentGranted = GetBool(state, "api_consent_granted");
            _consentAt = GetString(state, "api_consent_at");
            double timeout = GetDouble(state, "timeout_seconds", 120.0);
            ProviderTimeoutBox.Text = timeout.ToString("0.##", CultureInfo.InvariantCulture);

            _rows = new List<ProviderRow>();
            if (state.TryGetProperty("providers", out JsonElement array)
                && array.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement item in array.EnumerateArray())
                {
                    string kind = GetString(item, "kind");
                    if (kind is not ("ollama" or "openai_compatible"))
                        continue;
                    _rows.Add(new ProviderRow
                    {
                        Kind = kind,
                        ApiBase = GetString(item, "api_base"),
                        Model = GetString(item, "model"),
                        Enabled = GetBool(item, "enabled"),
                        KeySet = GetBool(item, "api_key_set"),
                    });
                }
            }

            _drafts.Clear();
            foreach (ProviderRow row in _rows)
            {
                _drafts[row.Kind] = new ProviderDraft
                {
                    ApiBase = row.ApiBase,
                    Model = row.Model,
                    ApiKey = "",
                };
            }
            SelectKind(_activeKind);
            ProviderStatusText.Text = Loc.T("settings.ecosystem.providerLoaded",
                "✅ 已读取提供方状态");
        }
        finally
        {
            _applying = false;
        }
        UpdateConsentPanel();
    }

    /// <summary>
    /// [EN] Renders the local API state: switch, port, endpoint and running status.
    /// [ZH] 渲染本地 API 状态：开关、端口、端点与运行状态。
    /// </summary>
    private void RenderLocalApiState(JsonElement state)
    {
        _applying = true;
        try
        {
            bool running = GetBool(state, "running");
            LocalApiEnabledCheck.IsChecked = running || GetBool(state, "enabled");
            int port = GetInt(state, "port", 8788);
            LocalApiPortBox.Text = port.ToString(CultureInfo.InvariantCulture);
            string endpoint = GetString(state, "endpoint");
            LocalApiEndpointText.Text = endpoint.Length > 0
                ? endpoint
                : Loc.T("settings.ecosystem.localApiStatusIdle", "未启动");
            string error = GetString(state, "error");
            if (running)
            {
                LocalApiStatusText.Text = Loc.T("settings.ecosystem.localApiStatusRunning",
                    "运行中：{0}", GetString(state, "base_url"));
            }
            else if (error.Length > 0)
            {
                LocalApiStatusText.Text = Loc.T("settings.ecosystem.localApiStatusFailed",
                    "启动失败：{0}", error);
            }
            else
            {
                LocalApiStatusText.Text = Loc.T("settings.ecosystem.localApiStatusStopped",
                    "已停止");
            }
        }
        finally
        {
            _applying = false;
        }
    }

    /// <summary>
    /// [EN] Selects one provider kind in the drop-down without re-entering the change
    /// [EN] handler, then loads its draft into the text fields.
    /// [ZH] 在不重新进入变更处理器的前提下选中某个提供方类型，然后将其草稿载入文本框。
    /// </summary>
    private void SelectKind(string kind)
    {
        _currentKind = kind;
        bool previous = _applying;
        _applying = true;
        try
        {
            if (ProviderKindCombo.ItemsSource is IEnumerable<Option> options)
            {
                foreach (Option option in options)
                {
                    if (option.Value == kind)
                    {
                        ProviderKindCombo.SelectedItem = option;
                        break;
                    }
                }
            }
        }
        finally
        {
            _applying = previous;
        }
        LoadDraft(kind);
    }

    /// <summary>
    /// [EN] Copies one kind's draft into the text fields and reflects whether a key was
    /// [EN] already stored. The key box is intentionally left empty: the stored value is
    /// [EN] never sent to the interface.
    /// [ZH] 把某一类型的草稿载入文本框，并反映是否已有保存的密钥。密钥框刻意留空：
    /// [ZH] 已保存的值绝不会发送到界面。
    /// </summary>
    private void LoadDraft(string kind)
    {
        bool local = kind == "llamacpp";
        ProviderBaseBox.IsEnabled = !local;
        ProviderKeyBox.IsEnabled = !local;
        ProviderModelBox.IsEnabled = !local;
        if (_drafts.TryGetValue(kind, out ProviderDraft? draft))
        {
            ProviderBaseBox.Text = draft.ApiBase;
            ProviderModelBox.Text = draft.Model;
            ProviderKeyBox.Text = draft.ApiKey;
        }
        else
        {
            ProviderBaseBox.Text = "";
            ProviderModelBox.Text = "";
            ProviderKeyBox.Text = "";
        }
        ProviderRow? row = _rows.Find(item => item.Kind == kind);
        ProviderKeyHint.Text = row is { KeySet: true }
            ? Loc.T("settings.ecosystem.keyStored",
                "已保存密钥（不回显）；留空表示沿用已保存的密钥。")
            : Loc.T("settings.ecosystem.providerKeyHint",
                "以明文保存在 config.json 中（本版未接入操作系统密钥链）。");
    }

    /// <summary>
    /// [EN] Stores the current text fields back into the draft of the selected kind, so
    /// [EN] switching the drop-down does not lose unsaved input.
    /// [ZH] 把当前文本框内容写回所选类型的草稿，使切换下拉项不丢失未保存输入。
    /// </summary>
    private void CaptureDraft()
    {
        if (_currentKind == "llamacpp")
            return;
        _drafts[_currentKind] = new ProviderDraft
        {
            ApiBase = (ProviderBaseBox.Text ?? "").Trim(),
            Model = (ProviderModelBox.Text ?? "").Trim(),
            ApiKey = ProviderKeyBox.Text ?? "",
        };
    }

    /// <summary>
    /// [EN] Shows or hides the consent panel and words it according to the endpoint.
    /// [ZH] 显示或隐藏同意面板，并根据端点措辞。
    /// </summary>
    private void UpdateConsentPanel()
    {
        bool external = IsExternalEndpoint(_currentKind, ProviderBaseBox.Text);
        ProviderConsentPanel.IsVisible = external;
        if (!external)
            return;
        ProviderConsentTitle.Text = Loc.T("settings.ecosystem.providerConsentTitle",
            "⚠️ 需要知情同意");
        ProviderConsentWarning.Text = external
            ? Loc.T("settings.ecosystem.providerConsentWarning",
                "下面的端点不在本机。启用后，您的对话与文档会被发送至该外部服务，EchoDesk 的本地隐私承诺在此模式下失效，数据泄露风险由您自行承担。")
            : Loc.T("settings.ecosystem.providerConsentLocal",
                "当前端点在本机，数据不会离开本机，无需授权。");
        ProviderConsentState.Text = _consentGranted && _consentAt.Length > 0
            ? Loc.T("settings.ecosystem.providerConsentGranted", "已于 {0} 记录授权。", _consentAt)
              + " " + Loc.T("settings.ecosystem.providerConsentAgain",
                  "每次保存外部端点都会弹出确认框；未确认则拒绝保存。")
            : Loc.T("settings.ecosystem.providerConsentAgain",
                "每次保存外部端点都会弹出确认框；未确认则拒绝保存。");
    }

    // -----------------------------------------------------------------
    // [EN] Saving
    // [ZH] 保存
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Applies local API settings. The port is range-checked here for immediate
    /// [EN] feedback, and again by the backend's configuration model.
    /// [ZH] 应用本地 API 设置。端口在此先做范围检查以便即时反馈，后端配置模型会再校验一次。
    /// </summary>
    private async Task ApplyLocalApiAsync()
    {
        if (!int.TryParse((LocalApiPortBox.Text ?? "").Trim(), out int port)
            || port < 1024 || port > 65535)
        {
            LocalApiStatusText.Text = Loc.T("settings.ecosystem.portRange",
                "⚠️ 端口需要在 1024–65535 之间。");
            return;
        }
        var payload = new Dictionary<string, object?>
        {
            ["action"] = "local_api_apply",
            ["enabled"] = LocalApiEnabledCheck.IsChecked == true,
            ["port"] = port,
            ["max_output_tokens"] = 1024,
        };
        // [EN] Empty means "unchanged": omit the field so the backend keeps the stored
        // [EN] token. Sending "" would silently erase it.
        // [ZH] 空表示"保持不变"：省略该字段，由后端沿用已保存的令牌。发送 "" 会静默清除它。
        string key = (LocalApiKeyBox.Text ?? "").Trim();
        if (key.Length > 0)
            payload["api_key"] = key;

        LocalApiStatusText.Text = Loc.T("settings.ecosystem.saving", "正在应用…");
        using JsonDocument? doc = await SendAsync(payload, 20);
        if (doc is null)
        {
            LocalApiStatusText.Text = Loc.T("settings.ecosystem.saveFailed", "❌ 后端未响应");
            return;
        }
        if (doc.RootElement.TryGetProperty("local_api", out JsonElement state))
            RenderLocalApiState(state);
        LocalApiKeyBox.Text = "";
        bool ok = GetBool(doc.RootElement, "ok");
        if (ok)
        {
            _host?.ShowStatus(Loc.T("settings.ecosystem.localApiApplied", "✅ 本地 API 设置已应用"));
        }
    }

    /// <summary>
    /// [EN] Saves provider settings. An external endpoint always opens the consent modal
    /// [EN] first; refusing it aborts without sending anything to the backend.
    /// [ZH] 保存提供方设置。外部端点总是先打开同意模态框；拒绝即中止，且不向后端发送任何内容。
    /// </summary>
    private async Task SaveProviderAsync()
    {
        CaptureDraft();
        string kind = _currentKind;
        bool external = kind != "llamacpp"
                        && IsExternalEndpoint(kind, (ProviderBaseBox.Text ?? "").Trim());
        bool granted = _consentGranted;
        if (external)
        {
            Window? owner = TopLevel.GetTopLevel(this) as Window;
            bool accepted = await ConsentDialog.ShowAsync(owner);
            if (!accepted)
            {
                ProviderStatusText.Text = Loc.T("settings.ecosystem.consentRequired",
                    "⚠️ 必须先勾选风险确认才能保存。");
                return;
            }
            granted = true;
        }

        ProviderStatusText.Text = Loc.T("settings.ecosystem.saving", "正在应用…");
        using JsonDocument? doc = await SendAsync(BuildProviderPayload(kind, granted), 40);
        if (doc is null)
        {
            ProviderStatusText.Text = Loc.T("settings.ecosystem.saveFailed", "❌ 后端未响应");
            return;
        }
        bool ok = GetBool(doc.RootElement, "ok");
        if (!ok)
        {
            string content = GetString(doc.RootElement, "content");
            ProviderStatusText.Text = Loc.T("settings.ecosystem.providerDenied",
                "⚠️ 后端已拒绝：{0}", content);
            return;
        }
        if (doc.RootElement.TryGetProperty("providers", out JsonElement state))
            RenderProviderState(state);
        ProviderStatusText.Text = Loc.T("settings.ecosystem.providerSaved",
            "✅ 提供方设置已保存。");
        _host?.ShowStatus(ProviderStatusText.Text);
    }

    /// <summary>
    /// [EN] Tests the current draft without activating it. An external endpoint needs the
    /// [EN] consent dialog here as well, because the test itself sends a request off the
    /// [EN] machine.
    /// [ZH] 测试当前草稿但不激活它。外部端点在此同样需要同意对话框，因为测试本身就会把请求
    /// [ZH] 发送到本机之外。
    /// </summary>
    private async Task TestProviderAsync()
    {
        CaptureDraft();
        string kind = _currentKind;
        if (kind == "llamacpp")
        {
            ProviderStatusText.Text = Loc.T("settings.ecosystem.providerLocalNoTest",
                "本地引擎无需连通性测试。");
            return;
        }
        string apiBase = (ProviderBaseBox.Text ?? "").Trim();
        bool external = IsExternalEndpoint(kind, apiBase);
        bool granted = _consentGranted;
        if (external)
        {
            Window? owner = TopLevel.GetTopLevel(this) as Window;
            bool accepted = await ConsentDialog.ShowAsync(owner);
            if (!accepted)
            {
                ProviderStatusText.Text = Loc.T("settings.ecosystem.consentRequired",
                    "⚠️ 必须先勾选风险确认才能保存。");
                return;
            }
            granted = true;
        }
        ProviderStatusText.Text = Loc.T("settings.ecosystem.testing", "正在测试连通性…");
        var payload = new Dictionary<string, object?>
        {
            ["action"] = "provider_test",
            ["kind"] = kind,
            ["api_base"] = apiBase,
            ["api_key"] = (ProviderKeyBox.Text ?? "").Trim(),
            ["model"] = (ProviderModelBox.Text ?? "").Trim(),
            ["api_consent_granted"] = granted,
        };
        using JsonDocument? doc = await SendAsync(payload, 40);
        if (doc is null)
        {
            ProviderStatusText.Text = Loc.T("settings.ecosystem.saveFailed", "❌ 后端未响应");
            return;
        }
        if (!GetBool(doc.RootElement, "ok"))
        {
            string content = GetString(doc.RootElement, "content");
            ProviderStatusText.Text = content.Length > 0
                ? Loc.T("settings.ecosystem.providerDenied", "⚠️ 后端已拒绝：{0}", content)
                : Loc.T("settings.ecosystem.providerTestFailed", "❌ 不可达：{0}",
                    GetString(doc.RootElement, "error"));
            return;
        }
        if (doc.RootElement.TryGetProperty("report", out JsonElement report)
            && GetBool(report, "ok"))
        {
            ProviderStatusText.Text = Loc.T("settings.ecosystem.providerTestOk",
                "✅ 可达：{0} 个模型，耗时 {1} 毫秒",
                GetInt(report, "model_count", 0), GetInt(report, "elapsed_ms", 0));
        }
        else
        {
            ProviderStatusText.Text = Loc.T("settings.ecosystem.providerTestFailed",
                "❌ 不可达：{0}", GetString(report, "error"));
        }
        _host?.ShowStatus(ProviderStatusText.Text);
    }

    /// <summary>
    /// [EN] Builds the `provider_save` request body.
    ///
    /// [EN] Every configured kind is sent back because the backend replaces the whole
    /// [EN] list; omitting an unchanged row would delete it. Keys are empty for every row
    /// [EN] except the one the user is editing, and the backend preserves a stored key
    /// [EN] when the incoming value is empty (same data-loss path that MCP `env` had).
    /// [ZH] 构建 `provider_save` 请求体。
    ///
    /// [ZH] 每个已配置类型都会回传，因为后端整份替换列表；漏掉未改动的行等于删除它。
    /// [ZH] 除用户正在编辑的那一行外密钥均为空，后端在收到空值时会保留已保存的密钥
    /// [ZH] （与 MCP `env` 曾出现的是同一条数据丢失路径）。
    /// </summary>
    private Dictionary<string, object?> BuildProviderPayload(string kind, bool granted)
    {
        var providers = new List<Dictionary<string, object?>>();
        foreach (string candidate in new[] { "ollama", "openai_compatible" })
        {
            bool isCurrent = candidate == kind;
            bool known = _drafts.ContainsKey(candidate);
            if (!isCurrent && !known)
                continue;
            ProviderDraft draft = _drafts.TryGetValue(candidate, out ProviderDraft? existing)
                ? existing
                : new ProviderDraft();
            ProviderRow? row = _rows.Find(item => item.Kind == candidate);
            providers.Add(new Dictionary<string, object?>
            {
                ["kind"] = candidate,
                ["api_base"] = draft.ApiBase,
                ["api_key"] = isCurrent ? draft.ApiKey : "",
                ["model"] = draft.Model,
                ["enabled"] = isCurrent || (row?.Enabled ?? false),
                ["api_consent_granted"] = granted,
            });
        }
        return new Dictionary<string, object?>
        {
            ["action"] = "provider_save",
            ["active"] = kind,
            ["timeout_seconds"] = ParseTimeout(),
            ["api_consent_granted"] = granted,
            ["providers"] = providers,
        };
    }

    /// <summary>
    /// [EN] Parses the timeout box, falling back to 120 seconds for empty or invalid input.
    /// [ZH] 解析超时输入框；为空或非法时回落到 120 秒。
    /// </summary>
    private double ParseTimeout()
    {
        return double.TryParse((ProviderTimeoutBox.Text ?? "").Trim(),
            NumberStyles.Float, CultureInfo.InvariantCulture, out double value)
            ? Math.Clamp(value, 5.0, 600.0)
            : 120.0;
    }

    // -----------------------------------------------------------------
    // [EN] Local-endpoint classification (UI hint only; the backend re-checks)
    // [ZH] 本地端点判定（仅界面提示；后端会再次校验）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Whether a provider configuration points outside this machine.
    ///
    /// [EN] This mirrors the backend rule but is not the enforcement point: it only
    /// [EN] decides whether to show the warning panel and the consent dialog. Malformed
    /// [EN] input returns true (fail closed) so a typo cannot skip the warning.
    /// [ZH] 提供方配置是否指向本机之外。
    ///
    /// [ZH] 这与后端规则一致，但不是执行点：它只决定是否显示警告面板与同意对话框。
    /// [ZH] 格式错误的输入返回真（失败时关闭），使笔误无法跳过警告。
    /// </summary>
    private static bool IsExternalEndpoint(string kind, string? apiBase)
    {
        if (kind == "llamacpp")
            return false;
        string text = (apiBase ?? "").Trim();
        if (text.Length == 0)
            return false;
        if (!text.Contains("://"))
            text = "http://" + text;
        if (!Uri.TryCreate(text, UriKind.Absolute, out Uri? uri))
            return true;
        if (uri.Scheme is not ("http" or "https"))
            return true;
        string host = uri.Host.ToLowerInvariant();
        if (host is "localhost" or "127.0.0.1" or "::1" or "0:0:0:0:0:0:0:1")
            return false;
        return !host.StartsWith("127.", StringComparison.Ordinal);
    }

    // -----------------------------------------------------------------
    // [EN] Language
    // [ZH] 语言
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Rebuilds every piece of text after a language switch. The provider drop-down
    /// [EN] is rebuilt from scratch because its item text is resolved at render time.
    /// [ZH] 语言切换后重建全部文案。提供方下拉框整体重建，因为其项文案在渲染时解析。
    /// </summary>
    public void ApplyTexts()
    {
        string keep = _currentKind;
        BuildOptions();
        SelectKind(keep);
        UpdateConsentPanel();

        EcosystemTitle.Text = Loc.T("settings.ecosystem.title",
            "🌐 生态：本地 API 与外部模型");
        EcosystemReloadButton.Content = Loc.T("settings.ecosystem.reload", "↻ 重新读取");
        EcosystemIntro.Text = Loc.T("settings.ecosystem.intro",
            "把 EchoDesk 开放给其他工具：让 IDE 扩展使用本地推理，或接入外部模型服务。");

        LocalApiTitle.Text = Loc.T("settings.ecosystem.localApiTitle",
            "🖥 本地 API（OpenAI 兼容）");
        LocalApiIntro.Text = Loc.T("settings.ecosystem.localApiIntro",
            "把 Cline、Continue、Cursor、curl 或 Postman 指向下面的端点即可。");
        LocalApiEnabledCheck.Content = Loc.T("settings.ecosystem.localApiEnabled",
            "启用本地 API");
        LocalApiPortLabel.Text = Loc.T("settings.ecosystem.localApiPort", "端口");
        LocalApiKeyLabel.Text = Loc.T("settings.ecosystem.localApiKey", "访问令牌（可选）");
        LocalApiApplyButton.Content = Loc.T("settings.ecosystem.localApiApply", "应用");
        LocalApiEndpointLabel.Text = Loc.T("settings.ecosystem.localApiEndpoint", "端点");
        LocalApiKeyHint.Text = Loc.T("settings.ecosystem.localApiKeyHint",
            "留空表示不鉴权；套接字只绑定 127.0.0.1。");
        LocalApiSecurity.Text = Loc.T("settings.ecosystem.localApiSecurity",
            "🔒 监听地址固定为 127.0.0.1，不可修改：该 API 永远不会从局域网可达。");

        ProviderTitle.Text = Loc.T("settings.ecosystem.providerTitle",
            "🧩 模型提供方（外部 API）");
        ProviderIntro.Text = Loc.T("settings.ecosystem.providerIntro",
            "默认提供方是本地 llama.cpp 引擎；也可以改用 Ollama 或任意 OpenAI 兼容服务。");
        ProviderActiveLabel.Text = Loc.T("settings.ecosystem.providerActive", "当前生效的提供方");
        ProviderBaseLabel.Text = Loc.T("settings.ecosystem.providerBase", "Endpoint");
        ProviderKeyLabel.Text = Loc.T("settings.ecosystem.providerKey", "API 密钥");
        ProviderModelLabel.Text = Loc.T("settings.ecosystem.providerModel", "模型名称");
        ProviderTimeoutLabel.Text = Loc.T("settings.ecosystem.providerTimeout", "请求超时（秒）");
        ProviderSaveButton.Content = Loc.T("settings.ecosystem.providerSave",
            "💾 保存提供方设置");
        ProviderTestButton.Content = Loc.T("settings.ecosystem.providerTest", "🔌 测试连通性");
        ProviderKeyHint.Text = Loc.T("settings.ecosystem.providerKeyHint",
            "以明文保存在 config.json 中（本版未接入操作系统密钥链）。");
        LoadDraft(_currentKind);
    }

    // -----------------------------------------------------------------
    // [EN] Backend plumbing
    // [ZH] 后端通路
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Sends one request to the shared backend. Returns null when the settings page
    /// [EN] has not been attached yet or the backend did not answer.
    /// [ZH] 向共享后端发送一个请求。设置页尚未挂载或后端没有应答时返回 null。
    /// </summary>
    private async Task<JsonDocument?> SendAsync(object request, int timeoutSeconds = 30)
    {
        if (_host is null)
            return null;
        return await _host.State.ActiveBackend.SendAsync(
            request, idleTimeout: TimeSpan.FromSeconds(timeoutSeconds));
    }

    // -----------------------------------------------------------------
    // [EN] JSON helpers (same conventions as AgentPage)
    // [ZH] JSON 辅助方法（与 AgentPage 同口径）
    // -----------------------------------------------------------------

    private static string GetString(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? ""
            : "";

    private static bool GetBool(JsonElement item, string name) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.True;

    private static int GetInt(JsonElement item, string name, int fallback) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number
         && el.TryGetInt32(out int value)
            ? value
            : fallback;

    private static double GetDouble(JsonElement item, string name, double fallback) =>
        item.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number
            ? el.GetDouble()
            : fallback;

    // -----------------------------------------------------------------
    // [EN] Probe interface (used by `EchoDesk --probe pages`)
    // [ZH] 探针接口（`EchoDesk --probe pages` 使用）
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Probe: reads both sections from the real backend.
    /// [ZH] 探针：从真实后端读取两个区块。
    /// </summary>
    public async Task ProbeRefreshAsync() => await RefreshAsync();

    /// <summary>
    /// [EN] Probe: shows the provider fields as they would look for an external endpoint,
    /// [EN] without saving anything, so the consent panel can be asserted.
    /// [ZH] 探针：把提供方字段呈现为外部端点的样子（不保存任何内容），以便断言同意面板。
    /// </summary>
    public void ProbeShowExternal(string apiBase)
    {
        SelectKind("openai_compatible");
        ProviderBaseBox.Text = apiBase;
        UpdateConsentPanel();
    }

    /// <summary>
    /// [EN] Probe: observable state of this page.
    ///
    /// [EN] `localApiControls` and `providerControls` are digit strings in which each
    /// [EN] position means "this control exists and carries text"; reading them as a
    /// [EN] single number keeps the probe output compact and comparable.
    /// [ZH] 探针：本页的可观测状态。
    ///
    /// [ZH] `localApiControls` 与 `providerControls` 是数字串，每一位表示"该控件存在且有
    /// [ZH] 文案"；作为单个数字读取，使探针输出紧凑且便于比较。
    /// </summary>
    public string EcosystemSnapshot()
    {
        string localApiControls = $"{HasText(LocalApiEnabledCheck.Content)}" +
                                  $"{HasText(LocalApiPortBox.Text)}" +
                                  $"{HasText(LocalApiKeyBox.IsEnabled ? "x" : "")}" +
                                  $"{HasText(LocalApiApplyButton.Content)}";
        string providerControls = $"{HasText(ProviderBaseLabel.Text)}" +
                                  $"{HasText(ProviderKeyLabel.Text)}" +
                                  $"{HasText(ProviderModelLabel.Text)}" +
                                  $"{HasText(ProviderTimeoutLabel.Text)}";
        string providerButtons = $"{HasText(ProviderSaveButton.Content)}" +
                                 $"{HasText(ProviderTestButton.Content)}";
        return string.Join(" ",
            $"localApiControls={localApiControls}",
            $"localApiEndpointHas127={((LocalApiEndpointText.Text ?? "").Contains("127.0.0.1") ? 1 : 0)}",
            $"localApiSecurity={HasText(LocalApiSecurity.Text)}",
            $"providerKinds={CountOptions()}",
            $"providerControls={providerControls}",
            $"providerButtons={providerButtons}",
            $"providerStatus={HasText(ProviderStatusText.Text)}",
            $"consentPanelVisible={(ProviderConsentPanel.IsVisible ? 1 : 0)}",
            $"consentWarning={HasText(ProviderConsentWarning.Text)}",
            $"consentGranted={(_consentGranted ? 1 : 0)}",
            $"localApiStatus={HasText(LocalApiStatusText.Text)}");
    }

    /// <summary>
    /// [EN] "1" when the value is a non-empty string, otherwise "0". Kept tiny so the
    /// [EN] snapshot stays readable.
    /// [ZH] 值为非空字符串时返回 "1"，否则返回 "0"。保持极简，使快照可读。
    /// </summary>
    private static string HasText(object? value) =>
        string.IsNullOrEmpty(value as string) ? "0" : "1";

    /// <summary>
    /// [EN] Probe: constructs the consent dialog and reports its observable state without
    /// [EN] showing it (the headless probe has no interactive user).
    /// [ZH] 探针：构造同意对话框并在不显示的情况下报告其可观测状态（无头探针没有交互用户）。
    /// </summary>
    public static string ProbeConsentSnapshot() => new ConsentDialog().ConsentSnapshot();

    private int CountOptions() =>
        ProviderKindCombo.ItemsSource is IEnumerable<Option> options ? SafeCount(options) : 0;

    private static int SafeCount(IEnumerable<Option> options)
    {
        int count = 0;
        foreach (Option _ in options)
            count++;
        return count;
    }
}
