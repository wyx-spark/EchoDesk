// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ISettingsPage.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ISettingsPage.cs。类型与成员说明见下方注释。
// [EN] The common contract for every tab in the settings window (v0.2.1 cross-platform-2
// [EN] development task 1).
// [ZH] 设置窗口内每个页签的统一契约（v0.2.1 跨平台-2 研发任务 1）。
//
// [EN] **Why this contract exists**, instead of squeezing the logic of seven tabs into partial
// [EN] classes of SettingsWindow as the WPF version does:
// [EN] the WPF settings window totals about 8000 lines (SettingsWindow.xaml 1321 lines plus more
// [EN] than 6400 lines across six partial classes), so a defect in one tab takes a long search
// [EN] through a gigantic file; the design specification, however, requires "migrate module by
// [EN] module, verify each module separately".
// [EN] After splitting into "one skeleton + one UserControl per page", each module can be
// [EN] compiled, verified, and replaced independently, and several pages can be developed in
// [EN] parallel (their files never conflict).
// [ZH] **为什么要有这个契约**，而不是像 WPF 版那样把 7 个页签的逻辑塞进 SettingsWindow 的分部类：
// [ZH] WPF 版的设置窗口合计约 8000 行（SettingsWindow.xaml 1321 行 + 六个分部类 6400 余行），
// [ZH] 一个页签的缺陷要在一个巨型文件里翻很久；而设计规范要求“逐模块迁移、每个模块单独验证”。
// [ZH] 拆成「骨架 + 每页一个 UserControl」之后，每个模块都能独立编译、独立验证、独立替换，
// [ZH] 也让多个页面可以并行开发（各自的文件互不冲突）。
//
// [EN] Division of labor:
// [EN]   * The skeleton (SettingsWindow) handles: the tab container, window state memory, the
// [EN]     bottom status and save buttons, **immediate effect** of theme/language switches, and
// [EN]     the field help panel.
// [EN]   * A page (ISettingsPage) handles: reading and writing its own controls and its own
// [EN]     page's backend data.
// [ZH] 分工：
// [ZH]   * 骨架（SettingsWindow）负责：页签容器、窗口状态记忆、底部状态与保存按钮、
// [ZH]     主题/语言切换的**即时生效**、字段帮助面板。
// [ZH]   * 页面（ISettingsPage）负责：自己那部分控件的读写、自己那一页的后端数据。
using System.Threading.Tasks;

namespace EchoDesk.Avalonia.Views.Settings;

/// <summary>
/// [EN] Host interface between a page and the skeleton -- a page can touch the outside world only
/// [EN] through it.
/// [ZH] 页面与骨架之间的宿主接口 —— 页面只能通过它触碰外部世界。
/// </summary>
public interface ISettingsHost
{
    /// <summary>
    /// [EN] Process-wide shared state (backend service, settings, platform information).
    /// [ZH] 进程内共享状态（后端服务、设置、平台信息）。
    /// </summary>
    Services.AppState State { get; }

    /// <summary>
    /// [EN] Show a message in the window's bottom status line.
    /// [ZH] 在窗口底部状态行显示一条信息。
    /// </summary>
    void ShowStatus(string text);

    /// <summary>
    /// [EN] Mark "there are unsaved changes" (the skeleton updates the save button and hint
    /// [EN] accordingly).
    /// [ZH] 标记“有未保存的改动”（骨架据此更新保存按钮与提示）。
    /// </summary>
    void MarkDirty();

    /// <summary>
    /// [EN] Show an explanation in the field help panel on the right (the design specification
    /// [EN] requires "field help" to be checkable by the probe).
    /// [ZH] 在右侧字段帮助面板显示说明（设计规范要求“字段帮助”可被探针检查）。
    /// </summary>
    void ShowFieldHelp(string title, string body, string example = "");

    /// <summary>
    /// [EN] Collapse the field help panel.
    /// [ZH] 收起字段帮助面板。
    /// </summary>
    void ClearFieldHelp();

    /// <summary>
    /// [EN] Switch to another settings page (v0.2.2 batch 4). "➕ Create local plugin" on the
    /// [EN] "Installed" page must jump to the "Developer" page and focus the plugin ID input box --
    /// [EN] the WPF side uses `ShowTab(...)`, with the same semantics.
    /// [EN] Return false when the tab is not found; do not throw (a failed jump must not crash the
    /// [EN] settings window).
    /// [ZH] 切到另一个设置页（v0.2.2 第 4 批）。「已安装」页的「➕ 新建本地插件」要跳到
    /// [ZH] 「开发者」页并聚焦到插件 ID 输入框 —— WPF 侧是 `ShowTab(...)`，语义一致。
    /// [ZH] 找不到该页签时返回 false，不抛（跳转失败不该让设置窗口崩）。
    /// </summary>
    bool ShowTab(string tabKey);

    /// <summary>
    /// [EN] Get a registered settings page by type (needed to focus a control after a cross-page
    /// [EN] jump). Return null if not found.
    /// [ZH] 按类型取已注册的设置页（跨页跳转后要聚焦某个控件时需要它）。找不到返回 null。
    /// </summary>
    T? FindPage<T>() where T : class;
}

/// <summary>
/// [EN] One tab of the settings window.
/// [ZH] 设置窗口的一个页签。
/// </summary>
public interface ISettingsPage
{
    /// <summary>
    /// [EN] Language pack key for the tab title (such as settings.tab.general).
    /// [ZH] 页签标题的语言包键（如 settings.tab.general）。
    /// </summary>
    string TabKey { get; }

    /// <summary>
    /// [EN] Fallback text when the language pack is missing (same criterion as the WPF version, so
    /// [EN] the key name never shows).
    /// [ZH] 语言包缺失时的兜底文案（与 WPF 版同口径，保证不至于显示键名）。
    /// </summary>
    string TabFallback { get; }

    /// <summary>
    /// [EN] The tab's AutomationId. **Kept consistent with x:Name in the WPF version**, so the
    /// [EN] existing assertions in tools\ui_probe.ps1 can be reused on the cross-platform front
    /// [EN] end.
    /// [ZH] 页签的 AutomationId。**与 WPF 版的 x:Name 保持一致**，
    /// [ZH] 这样 tools\ui_probe.ps1 的既有断言可以复用到跨平台前端上。
    /// </summary>
    string AutomationId { get; }

    /// <summary>
    /// [EN] Called once by the skeleton during construction.
    /// [ZH] 骨架在构造时调用一次。
    /// </summary>
    void Attach(ISettingsHost host);

    /// <summary>
    /// [EN] Write UI settings into the controls.
    /// [ZH] 把界面设置写进控件。
    /// </summary>
    void LoadFrom(Services.AppSettings settings);

    /// <summary>
    /// [EN] Collect the control values back into **UI settings** (called on save).
    /// [ZH] 把控件的值收集回**界面设置**（保存时调用）。
    /// </summary>
    void ApplyTo(Services.AppSettings settings);

    /// <summary>
    /// [EN] Synchronize this page's **business settings** to the backend (called on save; a purely
    /// [EN] UI page may return a completed task).
    /// [EN] Why UI settings and business settings are kept separate: UI settings live in
    /// [EN] ui_state.json (owned by the front end), while business settings (default mode,
    /// [EN] automation configuration, model profile...) live in the backend's config.json -- the
    /// [EN] front end does not keep a copy, to avoid the two drifting apart (the WPF version uses
    /// [EN] the same division of labor).
    /// [ZH] 把这一页的**业务设置**同步给后端（保存时调用；纯界面页返回已完成的任务即可）。
    /// [ZH] 为什么要把界面设置与业务设置分开：界面设置存在 ui_state.json（前端自己的），
    /// [ZH] 业务设置（默认模式、自动化配置、模型档位…）存在后端 config.json —— 前端不复制一份，
    /// [ZH] 免得两边不一致（WPF 版是同一分工）。
    /// </summary>
    Task ApplyToBackendAsync();

    /// <summary>
    /// [EN] Fetch the business data this page needs from the backend (a purely UI page may return
    /// [EN] a completed task).
    /// [ZH] 从后端拉取本页需要的业务数据（纯界面页返回已完成的任务即可）。
    /// </summary>
    Task RefreshAsync();

    /// <summary>
    /// [EN] Rebuild all text after a language switch (Avalonia lacks WPF's attached-property
    /// [EN] traversal mechanism).
    /// [ZH] 语言切换后重建所有文案（Avalonia 没有 WPF 的附加属性遍历机制）。
    /// </summary>
    void ApplyTexts();
}
