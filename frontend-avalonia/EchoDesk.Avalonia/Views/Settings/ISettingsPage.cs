// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ISettingsPage.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/Settings/ISettingsPage.cs。类型与成员说明见下方注释。
// 设置窗口内每个页签的统一契约（v0.2.1 跨平台-2 任务一）。
//
// **为什么要有这个契约**，而不是像 WPF 版那样把 7 个页签的逻辑塞进 SettingsWindow 的分部类：
// WPF 版的设置窗口合计约 8000 行（SettingsWindow.xaml 1321 行 + 六个分部类 6400 余行），
// 一个页签的缺陷要在一个巨型文件里翻很久；而任务书要求"逐模块迁移、每个模块单独验证"。
// 拆成「骨架 + 每页一个 UserControl」之后，每个模块都能独立编译、独立验证、独立替换，
// 也让多个页面可以并行开发（各自的文件互不冲突）。
//
// 分工：
//   * 骨架（SettingsWindow）负责：页签容器、窗口状态记忆、底部状态与保存按钮、
//     主题/语言切换的**即时生效**、字段帮助面板。
//   * 页面（ISettingsPage）负责：自己那部分控件的读写、自己那一页的后端数据。
using System.Threading.Tasks;

namespace EchoDesk.Avalonia.Views.Settings;

/// <summary>页面与骨架之间的宿主接口 —— 页面只能通过它触碰外部世界。</summary>
public interface ISettingsHost
{
    /// <summary>进程内共享状态（后端服务、设置、平台信息）。</summary>
    Services.AppState State { get; }

    /// <summary>在窗口底部状态行显示一条信息。</summary>
    void ShowStatus(string text);

    /// <summary>标记"有未保存的改动"（骨架据此更新保存按钮与提示）。</summary>
    void MarkDirty();

    /// <summary>在右侧字段帮助面板显示说明（任务书要求"字段帮助"可被探针检查）。</summary>
    void ShowFieldHelp(string title, string body, string example = "");

    /// <summary>收起字段帮助面板。</summary>
    void ClearFieldHelp();

    /// <summary>
    /// 切到另一个设置页（v0.2.2 第 4 批）。「已安装」页的「➕ 新建本地插件」要跳到
    /// 「开发者」页并聚焦到插件 ID 输入框 —— WPF 侧是 `ShowTab(...)`，语义一致。
    /// 找不到该页签时返回 false，不抛（跳转失败不该让设置窗口崩）。
    /// </summary>
    bool ShowTab(string tabKey);

    /// <summary>按类型取已注册的设置页（跨页跳转后要聚焦某个控件时需要它）。找不到返回 null。</summary>
    T? FindPage<T>() where T : class;
}

/// <summary>设置窗口的一个页签。</summary>
public interface ISettingsPage
{
    /// <summary>页签标题的语言包键（如 settings.tab.general）。</summary>
    string TabKey { get; }

    /// <summary>语言包缺失时的兜底文案（与 WPF 版同口径，保证不至于显示键名）。</summary>
    string TabFallback { get; }

    /// <summary>
    /// 页签的 AutomationId。**与 WPF 版的 x:Name 保持一致**，
    /// 这样 tools\ui_probe.ps1 的既有断言可以复用到跨平台前端上。
    /// </summary>
    string AutomationId { get; }

    /// <summary>骨架在构造时调用一次。</summary>
    void Attach(ISettingsHost host);

    /// <summary>把界面设置写进控件。</summary>
    void LoadFrom(Services.AppSettings settings);

    /// <summary>把控件的值收集回**界面设置**（保存时调用）。</summary>
    void ApplyTo(Services.AppSettings settings);

    /// <summary>
    /// 把这一页的**业务设置**同步给后端（保存时调用；纯界面页返回已完成的任务即可）。
    /// 为什么要把界面设置与业务设置分开：界面设置存在 ui_state.json（前端自己的），
    /// 业务设置（默认模式、自动化配置、模型档位…）存在后端 config.json —— 前端不复制一份，
    /// 免得两边不一致（WPF 版是同一分工）。
    /// </summary>
    Task ApplyToBackendAsync();

    /// <summary>从后端拉取本页需要的业务数据（纯界面页返回已完成的任务即可）。</summary>
    Task RefreshAsync();

    /// <summary>语言切换后重建所有文案（Avalonia 没有 WPF 的附加属性遍历机制）。</summary>
    void ApplyTexts();
}
