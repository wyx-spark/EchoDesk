// [EN] EchoDesk Avalonia frontend module: Informed-consent dialog for external APIs.
// [ZH] EchoDesk 前端模块：外部 API 的知情同意对话框。
//
// [EN] Behavior: the dialog only *collects* the user's acknowledgement. The caller
// [EN] still sends `api_consent_granted: true` in the save request, and the backend
// [EN] independently refuses any external endpoint without that flag. The dialog
// [EN] therefore cannot be bypassed by hiding it or by calling the caller directly.
// [ZH] 行为：对话框只**收集**用户的确认。调用方仍需在保存请求中发送
// [ZH] `api_consent_granted: true`，而后端独立拒绝任何缺少该标记的外部端点。
// [ZH] 因此隐藏对话框或直接调用调用方都无法绕过校验。
using System.Threading.Tasks;
using Avalonia.Automation;
using Avalonia.Controls;
using EchoDesk.Avalonia.Services;

namespace EchoDesk.Avalonia.Views.Settings;

public partial class ConsentDialog : Window
{
    /// <summary>
    /// [EN] Whether the acknowledgement was ticked when the dialog was confirmed.
    /// [ZH] 对话框确认时风险确认是否已勾选。
    /// </summary>
    public bool Accepted => ConsentAcceptCheck.IsChecked == true;

    /// <summary>
    /// [EN] Constructs the dialog; text is filled in by <see cref="ApplyTexts"/> so the
    /// [EN] caller controls when the language pack is consulted.
    /// [ZH] 构造对话框；文案由 <see cref="ApplyTexts"/> 填入，使调用方控制查询语言包的时机。
    /// </summary>
    public ConsentDialog()
    {
        InitializeComponent();
        ConsentAcceptCheck.IsCheckedChanged += (_, _) =>
        {
            bool ticked = ConsentAcceptCheck.IsChecked == true;
            ConsentConfirmButton.IsEnabled = ticked;
            ConsentRequiredText.IsVisible = !ticked;
        };
        ConsentConfirmButton.Click += (_, _) => Close(Accepted);
        ConsentCancelButton.Click += (_, _) => Close(false);
    }

    /// <summary>
    /// [EN] Fills every piece of text from the language pack.
    /// [ZH] 用语言包填充全部文案。
    /// </summary>
    public void ApplyTexts()
    {
        Title = Loc.T("settings.ecosystem.consentTitle", "警告：数据外发风险");
        ConsentTitleText.Text = Title;
        ConsentBodyText.Text = Loc.T("settings.ecosystem.consentBody",
            "警告：启用此功能将把您的对话与文档发送至该外部服务，EchoDesk 的本地隐私承诺在此模式下失效。您必须自行承担数据泄露风险。");
        ConsentAcceptCheck.Content = Loc.T("settings.ecosystem.consentCheck",
            "我已了解并接受数据外发风险");
        ConsentRequiredText.Text = Loc.T("settings.ecosystem.consentRequired",
            "必须先勾选风险确认才能保存。");
        ConsentConfirmButton.Content = Loc.T("settings.ecosystem.consentConfirm",
            "我已了解，仍要启用");
        ConsentCancelButton.Content = Loc.T("settings.ecosystem.consentCancel", "取消");
        AutomationProperties.SetName(ConsentConfirmButton,
            Loc.T("settings.ecosystem.consentConfirm", "我已了解，仍要启用"));
        AutomationProperties.SetName(ConsentCancelButton,
            Loc.T("settings.ecosystem.consentCancel", "取消"));
    }

    /// <summary>
    /// [EN] Shows the dialog modally over <paramref name="owner"/> and reports whether
    /// [EN] the user both ticked the acknowledgement and confirmed.
    /// [EN] Returns false when no owner is available: refusing the save is the safe
    /// [EN] direction, and the caller reports it instead of silently proceeding.
    /// [ZH] 在 <paramref name="owner"/> 之上以模态方式显示对话框，并报告用户是否同时
    /// [ZH] 勾选了风险确认并点击确认。
    /// [ZH] 没有可用宿主时返回 false：拒绝保存才是安全方向，调用方会如实提示，
    /// [ZH] 而不是静默继续。
    /// </summary>
    public static async Task<bool> ShowAsync(Window? owner)
    {
        if (owner is null)
            return false;
        var dialog = new ConsentDialog();
        dialog.ApplyTexts();
        bool? result = await dialog.ShowDialog<bool?>(owner);
        return result == true && dialog.Accepted;
    }

    /// <summary>
    /// [EN] Probe interface: report the text lengths and the initial confirm-button state
    /// [EN] without showing the window, so the headless probe can assert the red line
    /// [EN] exists without needing an interactive user.
    /// [ZH] 探针接口：在不显示窗口的情况下报告文案长度与确认按钮的初始状态，使无头探针
    /// [ZH] 无需交互用户即可断言该红线存在。
    /// </summary>
    public string ConsentSnapshot()
    {
        ApplyTexts();
        return string.Join(" ",
            $"consentTitle={(string.IsNullOrEmpty(ConsentTitleText.Text) ? 0 : 1)}",
            $"consentBody={(string.IsNullOrEmpty(ConsentBodyText.Text) ? 0 : 1)}",
            $"consentBodyIsTheRequiredWarning={(((ConsentBodyText.Text ?? "").Contains("本地隐私承诺") || (ConsentBodyText.Text ?? "").Contains("local-privacy")) ? 1 : 0)}",
            $"consentCheck={(string.IsNullOrEmpty(ConsentAcceptCheck.Content as string) ? 0 : 1)}",
            $"consentConfirmDisabled={(ConsentConfirmButton.IsEnabled ? 0 : 1)}",
            $"consentCancel={(string.IsNullOrEmpty(ConsentCancelButton.Content as string) ? 0 : 1)}");
    }
}
