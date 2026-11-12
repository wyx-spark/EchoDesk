// [EN] EchoDesk Avalonia frontend module: frontend-avalonia/EchoDesk.Avalonia/Views/SettingsWindow.axaml.cs. See type and member comments for details.
// [ZH] EchoDesk 前端模块：frontend-avalonia/EchoDesk.Avalonia/Views/SettingsWindow.axaml.cs。类型与成员说明见下方注释。
// [EN] Settings window skeleton (v0.2.1 cross-platform-2 development task 1, the first
// [EN] migrated module).
// [ZH] 设置窗口骨架（v0.2.1 跨平台-2 研发任务 1，第 1 个迁移模块）。
//
// [EN] This layer deliberately does only four things; everything else is left to the pages:
// [ZH] 这一层刻意只做四件事，别的一律交给页面：
// [EN]   1. Tab container: pages are registered through ISettingsPage (a tab that has not
// [EN]      been migrated gets an **explicit not-migrated explanation**, not a pretense that
// [EN]      the feature exists -- the same principle as "a degraded hint must give the reason
// [EN]      and the alternative" recorded in the notes);
// [EN]   2. Window state memory: the size is stored in ui_state.json (separate from the main
// [EN]      window, so the two do not interfere);
// [EN]   3. Bottom status line and save button: collect the changes of each page → write
// [EN]      them to disk → pages that need the backend synchronize themselves;
// [EN]   4. Theme / language take effect immediately: the theme changes at once
// [EN]      (AppState.SetTheme), and the language rebuilds all texts at once (Avalonia
// [EN]      rebuilds through events, WPF walks attached properties -- different mechanisms,
// [EN]      same result).
// [ZH]   1. 页签容器：按 ISettingsPage 注册页面（未迁移的页签给出**明确的未迁移说明**，
// [ZH]      不假装功能存在 —— 这是记录里"降级提示要给原因与替代做法"的同一条原则）；
// [ZH]   2. 窗口状态记忆：尺寸存进 ui_state.json（与主窗口分开存，互不影响）；
// [ZH]   3. 底部状态行与保存按钮：收集各页改动 → 落盘 → 需要后端的页自己去同步；
// [ZH]   4. 主题 / 语言即时生效：主题立刻改（AppState.SetTheme），语言立刻重建全部文案
// [ZH]      （Avalonia 用事件重建，WPF 用附加属性遍历 —— 机制不同，效果一致）。
using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Animation;
using Avalonia.Animation.Easings;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Transformation;
using Avalonia.Threading;
using Avalonia.VisualTree;
using EchoDesk.Avalonia.Services;
using EchoDesk.Avalonia.Views.Settings;

namespace EchoDesk.Avalonia.Views;

public partial class SettingsWindow : Window, ISettingsHost
{
    private readonly AppState _state;
    private readonly List<ISettingsPage> _pages = new();
    private readonly List<(TabItem Item, string Key, string Fallback)> _tabHeaders = new();
    private bool _dirty;

    // [EN] Motion contract of the settings surface. The page transition and the panel
    // [EN] transition are expressed through render-only properties wherever possible; the
    // [EN] field-help panel is the sole exception, because its width is the substance of the
    // [EN] requested sidebar motion.
    // [ZH] 设置界面的动效约定。页签过渡与面板过渡尽可能通过仅影响渲染的属性表达；字段帮助面板是唯一
    // [ZH] 的例外，因为其宽度正是所要求的侧边栏动效实体。
    private const double PageTransitionMilliseconds = 250;
    private const double FieldHelpWidth = 320;
    private const double FieldHelpTransitionMilliseconds = 200;

    /// <summary>
    /// [EN] One-shot timer that hides the field-help panel after its closing transition completes.
    /// [ZH] 字段帮助面板关闭过渡结束后负责隐藏它的一次性定时器。
    /// </summary>
    private DispatcherTimer? _fieldHelpHideTimer;

    // [EN] Manual double-click bookkeeping for the custom caption; see MainWindow for rationale.
    // [ZH] 自定义标题栏的手动双击判定状态；判定理由见主窗口对应说明。
    private DateTime _lastCaptionPressAt = DateTime.MinValue;
    private Point _lastCaptionPressPoint;
    private DateTime _lastCaptionToggleAt = DateTime.MinValue;

    /// <summary>
    /// [EN] Tabs that have not been migrated to the cross-platform front end yet. **It is
    /// [EN] empty now**: all 7 tabs of the settings window have been migrated
    /// [EN] (v0.2.1 cross-platform-2 development task 1).
    ///
    /// [EN] This array is kept instead of deleted because the **feature panels of the main
    /// [EN] window** have not been migrated yet; if a "placeholder first, implementation
    /// [EN] later" tab ever appears, register one line here, and the AddNotMigratedTab logic
    /// [EN] does not have to be brought back.
    /// [ZH] 还没迁移到跨平台前端的页签。**现在是空的**：设置窗口的 7 个页签已全部迁移完成
    /// [ZH] （v0.2.1 跨平台-2 研发任务 1）。
    ///
    /// [ZH] 保留这个数组而不是删掉，是因为**主窗口的各功能面板**还没迁移；将来若出现
    /// [ZH] "先占位、后实现"的页签，在这里登记一行即可，不必把 AddNotMigratedTab 的逻辑找回来。
    /// </summary>
    private static readonly (string Key, string Fallback)[] NotMigratedTabs =
        Array.Empty<(string Key, string Fallback)>();

    public SettingsWindow() : this(new AppState())
    {
    }

    public SettingsWindow(AppState state)
    {
        _state = state;
        InitializeComponent();

        RegisterPage(new GeneralPage());
        // [EN] The tab order must match the TabItem order in SettingsWindow.xaml on the WPF
        // [EN] side (General → Model → Agent → Automation → Skill market → Installed →
        // [EN] Developer): when the order differs, the same UI looks like two different
        // [EN] products on the two sides.
        // [ZH] 页签顺序必须与 WPF SettingsWindow.xaml 的 TabItem 顺序一致
        // [ZH] （常规 → 模型 → 智能体 → 自动化 → 技能市场 → 已安装 → 开发者）：
        // [ZH] 顺序不同的时候，同一份界面在两边看起来就像两套产品。
        RegisterPage(new ModelPage());
        RegisterPage(new AgentPage());
        RegisterPage(new AutomationPage());
        RegisterPage(new MarketPage());
        RegisterPage(new InstalledPage());
        RegisterPage(new DeveloperPage());
        // [EN] v0.2.6 Part 4: the Ecosystem page (local API + external providers) is
        // [EN] Avalonia-only. It sits after the schedule shared with WPF because WPF has
        // [EN] been in LTS since v0.2.3 and new features are implemented here only; the
        // [EN] first seven tabs keep the WPF order unchanged.
        // [ZH] v0.2.6 任务 4：生态页（本地 API + 外部提供方）只在 Avalonia 实现。
        // [ZH] 它排在 WPF 共有的次序之后：WPF 自 v0.2.3 起处于 LTS，新功能只在此实现；
        // [ZH] 前七个页签的次序保持不变。
        RegisterPage(new EcosystemPage());
        foreach ((string key, string fallback) in NotMigratedTabs)
            AddNotMigratedTab(key, fallback);

        // [EN] The page transition is attached after registration so that the initial page, which
        //      is selected while the items are added, is presented without motion.
        // [ZH] 页签过渡在注册完成后挂接，因此新增条目时被选中的初始页签不会播放动效。
        Tabs.SelectionChanged += OnTabSelectionChanged;

        Loc.LanguageChanged += (_, _) => ApplyTexts();
        ApplyTexts();
        LoadFromSettings();

        // [EN] Window size memory (required by the design specification). It is stored in
        // [EN] Closing to avoid frequent disk writes while dragging.
        // [ZH] 窗口尺寸记忆（设计规范要求）。Closing 里存，避免拖动过程中频繁写盘。
        Width = _state.Settings.SettingsWindowWidth;
        Height = _state.Settings.SettingsWindowHeight;
        Closing += (_, _) =>
        {
            _state.Settings.SettingsWindowWidth = Width;
            _state.Settings.SettingsWindowHeight = Height;
            _state.Settings.Save();
        };

        SaveButton.Click += async (_, _) => await SaveAsync();
        CloseButton.Click += (_, _) => Close();
        FieldHelpCloseButton.Click += (_, _) => ClearFieldHelp();

        // [EN] Custom caption controls. The pointer-driven move loop is attached in XAML.
        // [ZH] 自定义标题栏控件。指针驱动的移动循环在 XAML 中挂接。
        TitleBarMinimizeButton.Click += (_, _) => WindowState = WindowState.Minimized;
        TitleBarMaximizeButton.Click += (_, _) => ToggleMaximize();
        TitleBarCloseButton.Click += (_, _) => Close();
        // [EN] Window-state changes that bypass our controls (native gestures, snapping) must
        //      still refresh the caption glyph and its accessible names.
        // [ZH] 不经过自有控件的窗口状态变化（原生手势、贴靠）同样需要刷新标题栏字形与无障碍名称。
        PropertyChanged += OnWindowPropertyChanged;
        UpdateWindowChromeState();

        // [EN] The same safeguard as in the main window: the remembered size may come from
        // [EN] an older release (980x720) and would clip the save/close buttons at the
        // [EN] bottom when it exceeds the current screen's working area.
        // [ZH] 与主窗口同一道保险：记忆里的尺寸可能来自旧版本（980x720），
        // [ZH] 超出当前屏幕可用区域时会把底部的保存/关闭按钮裁掉。
        Opened += (_, _) =>
        {
            ClampSizeToScreen();
            PlayContentEntrance();
        };
    }

    // -----------------------------------------------------------------
    // [EN] Surface and page motion
    // [ZH] 界面与页签动效
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Plays the 250 ms entrance of the settings content. Only opacity and the render
    /// [EN] transform are animated, therefore the window can be measured and arranged once
    /// [EN] and the entrance adds no layout pass of its own.
    /// [ZH] 播放设置内容 250 毫秒的入场动效。仅不透明度与渲染变换参与动画，因此窗口只需完成一次测量与
    /// [ZH] 排列，入场过程本身不增加任何布局过程。
    /// </summary>
    private void PlayContentEntrance()
    {
        ContentRoot.Opacity = 1;
        ContentRoot.RenderTransform = TransformOperations.Parse("translate(0px, 0px)");
    }

    /// <summary>
    /// [EN] Plays the 250 ms entrance of a newly selected page. The transition is restarted
    /// [EN] from an explicit initial value, so that switching back and forth yields the same
    /// [EN] motion; the animation touches opacity and the render transform only.
    /// [ZH] 播放新选中页签 250 毫秒的入场动效。过渡从显式初始值重新开始，因此往返切换具有一致的运动
    /// [ZH] 表现；动画仅涉及不透明度与渲染变换。
    /// </summary>
    private void OnTabSelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (Tabs.SelectedItem is not TabItem { Content: Control page })
            return;
        page.Opacity = 0;
        page.RenderTransform = TransformOperations.Parse("translate(0px, 6px)");
        // [EN] The target values are applied on the next render pass so that the transition has a
        //      previous value to interpolate from; applying both in the same turn would snap.
        // [ZH] 目标值在下一个渲染回合施加，使过渡拥有可插值的先前值；在同一回合内一并施加只会直接跳变。
        Dispatcher.UIThread.Post(() =>
        {
            page.Opacity = 1;
            page.RenderTransform = TransformOperations.Parse("translate(0px, 0px)");
        }, DispatcherPriority.Render);
    }

    /// <summary>
    /// [EN] Opens or closes the field-help side panel through a width transition. The panel
    /// [EN] is hidden only after the closing transition has elapsed, therefore the surface
    /// [EN] recedes smoothly instead of collapsing in a single frame.
    /// [ZH] 通过宽度过渡开启或关闭字段帮助侧面板。面板仅在关闭过渡结束之后才被隐藏，因此该界面是平滑
    /// [ZH] 退出，而非在单帧内骤缩。
    /// </summary>
    private void AnimateFieldHelp(bool show)
    {
        _fieldHelpHideTimer?.Stop();
        _fieldHelpHideTimer = null;

        if (show)
        {
            FieldHelpPanel.IsVisible = true;
            FieldHelpPanel.IsHitTestVisible = true;
            FieldHelpPanel.Opacity = 1;
            FieldHelpPanel.Width = FieldHelpWidth;
            return;
        }

        FieldHelpPanel.IsHitTestVisible = false;
        FieldHelpPanel.Opacity = 0;
        FieldHelpPanel.Width = 0;
        _fieldHelpHideTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(FieldHelpTransitionMilliseconds + 20),
        };
        _fieldHelpHideTimer.Tick += (_, _) =>
        {
            _fieldHelpHideTimer?.Stop();
            _fieldHelpHideTimer = null;
            if (FieldHelpPanel.Width <= 0.5)
                FieldHelpPanel.IsVisible = false;
        };
        _fieldHelpHideTimer.Start();
    }

    /// <summary>
    /// [EN] Reports the motion configuration of the settings surface for unattended verification:
    ///      the page transition duration, the field-help panel transitions and its resting width.
    /// [ZH] 为无人值守验证提供设置界面的动效配置报告：页签过渡时长、字段帮助面板的过渡项及其静息宽度。
    /// </summary>
    public string ProbeMotionSnapshot()
    {
        string kinds = "none";
        double pageDuration = 0;
        if (Tabs.SelectedItem is TabItem { Content: Control page })
        {
            if (page.Transitions is { Count: > 0 } transitions)
                kinds = string.Join(",", transitions.Select(t => t.GetType().Name));
            foreach (DoubleTransition transition in page.Transitions?.OfType<DoubleTransition>() ?? [])
            {
                if (transition.Property == Visual.OpacityProperty)
                    pageDuration = transition.Duration.TotalMilliseconds;
            }
        }
        string fieldHelpKinds = FieldHelpPanel.Transitions is { Count: > 0 } fieldTransitions
            ? string.Join(",", fieldTransitions.Select(t => t.GetType().Name))
            : "none";
        return $"pageTransitions={kinds}｜pageDuration={pageDuration:0}"
               + $"｜fieldHelpTransitions={fieldHelpKinds}"
               + $"｜fieldHelpWidth={FieldHelpPanel.Width:0}｜fieldHelpVisible={FieldHelpPanel.IsVisible}";
    }

    // -----------------------------------------------------------------
    // [EN] Custom window chrome
    // [ZH] 自定义窗口外框
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Starts the platform window move loop from the custom caption. A double click
    /// [EN] within the system interval toggles the maximized state. The loop itself is
    /// [EN] provided by the platform, so snapping, cursor tracking and DPI transitions
    /// [EN] remain native.
    /// [ZH] 从自定义标题栏启动平台窗口移动循环。系统间隔内的双击切换最大化状态。移动循环由平台
    /// [ZH] 提供，因此贴靠、光标跟随与 DPI 切换均保持原生行为。
    /// </summary>
    private void OnTitleBarPointerPressed(object? sender, PointerPressedEventArgs e)
    {
        if (!e.GetCurrentPoint(this).Properties.IsLeftButtonPressed)
            return;
        // [EN] Caption controls own their input; the drag surface must not steal it.
        // [ZH] 窗口控制按钮自行处理输入，拖拽载面不得抢占。
        if (e.Source is Visual visual && visual.FindAncestorOfType<Button>(includeSelf: true) is not null)
            return;

        // [EN] Double-click detection is performed explicitly (interval + displacement) because
        //      the platform click counter is not reliable once the native move loop has started.
        // [ZH] 双击判定在此显式完成（时间间隔 + 位移），因为平台移动循环启动后系统点击计数不再可靠。
        DateTime now = DateTime.UtcNow;
        Point point = e.GetPosition(this);
        bool doubleByInterval = (now - _lastCaptionPressAt).TotalMilliseconds <= 400
            && Math.Abs(point.X - _lastCaptionPressPoint.X) <= 4
            && Math.Abs(point.Y - _lastCaptionPressPoint.Y) <= 4;
        _lastCaptionPressAt = now;
        _lastCaptionPressPoint = point;

        if (doubleByInterval || e.ClickCount == 2)
        {
            // [EN] A single gesture must not toggle twice, even when both detection paths fire.
            // [ZH] 同一次手势不得触发两次切换，即使两条判定路径同时命中。
            if ((now - _lastCaptionToggleAt).TotalMilliseconds > 200)
            {
                _lastCaptionToggleAt = now;
                _lastCaptionPressAt = DateTime.MinValue;
                ToggleMaximize();
            }
            e.Handled = true;
            return;
        }

        BeginMoveDrag(e);
        e.Handled = true;
    }

    /// <summary>
    /// [EN] Toggles the window between the maximised and restored states.
    /// [ZH] 在最大化与还原状态之间切换窗口。
    /// </summary>
    private void ToggleMaximize()
    {
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
        UpdateWindowChromeState();
    }

    /// <summary>
    /// [EN] Applies the current window state to the custom caption, rebuilding the glyph, the
    ///      tooltip and the accessible name in the active interface language.
    /// [ZH] 将当前窗口状态应用到自定义标题栏，并按当前界面语言重建字形、悬停提示与无障碍名称。
    /// </summary>
    private void UpdateWindowChromeState()
    {
        bool maximized = WindowState == WindowState.Maximized;
        MaximizeGlyph.IsVisible = !maximized;
        RestoreGlyph.IsVisible = maximized;
        TitleBarTitle.Text = Title;

        string minimize = Loc.T("main.window.minimize", "最小化");
        string maximize = maximized
            ? Loc.T("main.window.restore", "还原")
            : Loc.T("main.window.maximize", "最大化");
        string close = Loc.T("main.window.close", "关闭");

        ToolTip.SetTip(TitleBarMinimizeButton, minimize);
        ToolTip.SetTip(TitleBarMaximizeButton, maximize);
        ToolTip.SetTip(TitleBarCloseButton, close);
        AutomationProperties.SetName(TitleBarMinimizeButton, minimize);
        AutomationProperties.SetName(TitleBarMaximizeButton, maximize);
        AutomationProperties.SetName(TitleBarCloseButton, close);
    }

    /// <summary>
    /// [EN] Routes window-state property changes to the caption updater.
    /// [ZH] 将窗口状态属性变更路由至标题栏更新逻辑。
    /// </summary>
    private void OnWindowPropertyChanged(object? sender, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.Property == WindowStateProperty)
            UpdateWindowChromeState();
    }

    /// <summary>
    /// [EN] Clamps the window size to the working area of the current screen (leaving a
    /// [EN] 24 px margin).
    /// [ZH] 把窗口尺寸限制在当前屏幕的可用区域内（留 24px 余量）。
    /// </summary>
    private void ClampSizeToScreen()
    {
        global::Avalonia.Platform.Screen? screen =
            Screens.ScreenFromWindow(this) ?? Screens.Primary;
        if (screen is null)
            return;
        double scale = screen.Scaling > 0 ? screen.Scaling : 1.0;
        // [EN] Note: Width/Height in Avalonia are the **client area**; the window border and
        // [EN] the title bar add roughly 16x39 more. Subtracting only 24 from the working
        // [EN] area would make the client area "fit" while the whole window ends up higher
        // [EN] than the screen.
        // [ZH] 注意：Avalonia 的 Width/Height 是**客户区**，窗口边框与标题栏还要再加约 16x39。
        // [ZH] 只按可用区域减 24 的话，客户区虽然"装得下"，整窗反而会高出屏幕一截。
        double maxWidth = screen.WorkingArea.Width / scale - 48;
        double maxHeight = screen.WorkingArea.Height / scale - 56;
        if (maxWidth > 0 && Width > maxWidth)
            Width = Math.Max(MinWidth, maxWidth);
        if (maxHeight > 0 && Height > maxHeight)
            Height = Math.Max(MinHeight, maxHeight);
    }

    // -----------------------------------------------------------------
    // ISettingsHost
    // -----------------------------------------------------------------
    public AppState State => _state;

    public void ShowStatus(string text) => LoadStateText.Text = text;

    public void MarkDirty()
    {
        _dirty = true;
        SaveHintText.Text = Loc.T("settings.saveHint.dirty", "有未保存的改动");
    }

    public void ShowFieldHelp(string title, string body, string example = "")
    {
        FieldHelpTitle.Text = title;
        FieldHelpBody.Text = body;
        FieldHelpExample.Text = example;
        FieldHelpExample.IsVisible = !string.IsNullOrWhiteSpace(example);
        AnimateFieldHelp(show: true);
    }

    public void ClearFieldHelp() => AnimateFieldHelp(show: false);

    /// <summary>
    /// [EN] Cross-page jump (the "add new local plugin" button on the Installed page must
    /// [EN] land on the Developer page). Delegated to the existing <see cref="SelectTab"/> --
    /// [EN] the same tab lookup logic, not implemented twice.
    /// [ZH] 跨页跳转（「已安装」页的「➕ 新建本地插件」要跳到「开发者」页）。
    /// [ZH] 委托给既有的 <see cref="SelectTab"/> —— 同一份页签查找逻辑，不重复实现一遍。
    /// </summary>
    public bool ShowTab(string tabKey) => SelectTab(tabKey);

    // -----------------------------------------------------------------
    // [EN] Tab registration
    // [ZH] 页签注册
    // -----------------------------------------------------------------

    /// <summary>
    /// [EN] Selects a tab (every item of the main window's status area is "click to jump to
    /// [EN] the matching setting" and needs to land on that page directly). The argument is
    /// [EN] <see cref="ISettingsPage.TabKey"/> (such as <c>settings.tab.model</c>); when no
    /// [EN] tab matches, the default page is kept -- no exception is thrown, since a failed
    /// [EN] jump must not keep the settings window from opening.
    /// [ZH] 选中某个页签（主窗口状态区各项都是"点一下跳到对应设置"，需要直接落到那一页）。
    /// [ZH] 传的是 <see cref="ISettingsPage.TabKey"/>（如 <c>settings.tab.model</c>），
    /// [ZH] 找不到就保持默认页 —— 不抛异常，跳转失败不该让设置窗口打不开。
    /// </summary>
    public bool SelectTab(string tabKey)
    {
        foreach ((TabItem item, string key, _) in _tabHeaders)
        {
            if (string.Equals(key, tabKey, StringComparison.OrdinalIgnoreCase))
            {
                Tabs.SelectedItem = item;
                return true;
            }
        }
        return false;
    }

    /// <summary>
    /// [EN] Finds a registered settings page by type (**for probes**:
    /// [EN] `EchoDesk --probe market` needs the <c>ModelPage</c> to drive the interactions of
    /// [EN] the model market). Returns null when none is found; no exception is thrown.
    /// [ZH] 按类型找已注册的设置页（**探针用**：`EchoDesk --probe market` 要拿到
    /// [ZH] <c>ModelPage</c> 才能驱动模型市场那一套交互）。找不到返回 null，不抛。
    /// </summary>
    public T? FindPage<T>() where T : class
    {
        foreach (ISettingsPage page in _pages)
            if (page is T typed)
                return typed;
        return null;
    }

    private void RegisterPage(ISettingsPage page)    {
        page.Attach(this);
        page.ApplyTexts();
        // [EN] Each page carries its own transition set, because a transition must live on
        // [EN] the element whose property it animates. The resting values are the settled
        // [EN] state of an entrance that has already completed.
        // [ZH] 每个页面各自携带过渡集合，因为过渡必须位于其所动画属性所属的元素上。静息值即入场动效
        // [ZH] 完成后的稳定状态。
        var pageControl = (Control)page;
        pageControl.Opacity = 1;
        pageControl.RenderTransform = TransformOperations.Parse("translate(0px, 0px)");
        pageControl.Transitions = new Transitions
        {
            new DoubleTransition
            {
                Property = Visual.OpacityProperty,
                Duration = TimeSpan.FromMilliseconds(PageTransitionMilliseconds),
                Easing = new CubicEaseOut(),
            },
            new TransformOperationsTransition
            {
                Property = Visual.RenderTransformProperty,
                Duration = TimeSpan.FromMilliseconds(PageTransitionMilliseconds),
                Easing = new CubicEaseOut(),
            },
        };
        var item = new TabItem
        {
            Header = Loc.T(page.TabKey, page.TabFallback),
            Content = pageControl,
            Tag = page,
        };
        AutomationProperties.SetAutomationId(item, page.AutomationId);
        Tabs.Items.Add(item);
        _pages.Add(page);
        _tabHeaders.Add((item, page.TabKey, page.TabFallback));
    }

    /// <summary>
    /// [EN] Placeholder for a not-yet-migrated tab: it states where the page is and what to
    /// [EN] do for now, instead of showing an empty panel. On Windows the full-featured WPF
    /// [EN] build can be used directly; on other platforms it is stated explicitly that the
    /// [EN] page is unavailable for now.
    /// [ZH] 未迁移页签的占位：说清"这页在哪、现在怎么办"，而不是显示一个空面板。
    /// [ZH] Windows 上有功能完整的 WPF 版可以直接用；非 Windows 上则明确说明该页暂不可用。
    /// </summary>
    private void AddNotMigratedTab(string key, string fallback)
    {
        var panel = new StackPanel
        {
            Margin = new Thickness(16),
            Spacing = 10,
        };
        var title = new TextBlock { FontWeight = FontWeight.SemiBold, FontSize = 14 };
        var body = new TextBlock { TextWrapping = TextWrapping.Wrap, Opacity = 0.85 };
        var hint = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 12, Opacity = 0.7 };
        panel.Children.Add(title);
        panel.Children.Add(body);
        panel.Children.Add(hint);

        void Fill()
        {
            title.Text = Loc.T("settings.notMigrated.title",
                "⏳ 这一页还没有迁移到跨平台前端（{0}）", Loc.T(key, fallback));
            body.Text = Loc.T("settings.notMigrated.body",
                "跨平台前端（Avalonia）目前完成了主窗口与设置窗口的骨架、常规页；"
                + "其余页签仍只在 WPF 版里可用。这是一个已知的未完成项，不是功能损坏。");
            hint.Text = PlatformInfo.Current.IsWindows
                ? Loc.T("settings.notMigrated.hintWindows",
                    "替代做法：在 Windows 上使用 WPF 版（正式安装包安装的那个），功能是完整的。")
                : Loc.T("settings.notMigrated.hintOther",
                    "替代做法：本页涉及的配置可以直接编辑 app/backend/config.json，"
                    + "或等待后续版本把该页迁移过来。");
        }

        Fill();
        var item = new TabItem { Header = Loc.T(key, fallback), Content = panel };
        Tabs.Items.Add(item);
        _tabHeaders.Add((item, key, fallback));
        Loc.LanguageChanged += (_, _) => Fill();
    }

    // -----------------------------------------------------------------
    // [EN] Texts / settings
    // [ZH] 文案 / 设置
    // -----------------------------------------------------------------
    private void ApplyTexts()
    {
        Title = Loc.T("settings.title", "EchoDesk 设置");
        SaveButton.Content = Loc.T("settings.button.save", "保存");
        CloseButton.Content = Loc.T("settings.button.close", "关闭");
        FieldHelpCloseButton.Content = Loc.T("settings.button.close", "关闭");
        LoadStateText.Text = Loc.T("settings.loadState.loaded", "✅ 配置已载入");
        SaveHintText.Text = _dirty
            ? Loc.T("settings.saveHint.dirty", "有未保存的改动")
            : "";
        foreach ((TabItem item, string key, string fallback) in _tabHeaders)
            item.Header = Loc.T(key, fallback);
        foreach (ISettingsPage page in _pages)
            page.ApplyTexts();
        // [EN] Caption text and caption tooltips follow the interface language.
        // [ZH] 标题栏文案与其悬停提示随界面语言一并刷新。
        UpdateWindowChromeState();
    }

    private void LoadFromSettings()
    {
        foreach (ISettingsPage page in _pages)
        {
            page.LoadFrom(_state.Settings);
            _ = page.RefreshAsync();
        }
    }

    private async Task SaveAsync()
    {
        foreach (ISettingsPage page in _pages)
            page.ApplyTo(_state.Settings);

        bool saved = _state.Settings.Save();
        LoadStateText.Text = saved
            ? Loc.T("settings.loadState.saved", "✅ 设置已保存")
            : Loc.T("settings.loadState.saveFailed", "⚠️ 设置无法写入（目录只读？）");

        // [EN] Business settings (such as the default mode) are synchronized to the backend
        // [EN] by the page itself -- the front end does not hold a second copy of the
        // [EN] business configuration.
        // [ZH] 业务设置（如默认模式）由页面自己同步给后端 —— 前端不持有第二份业务配置。
        foreach (ISettingsPage page in _pages)
        {
            try
            {
                await page.ApplyToBackendAsync();
            }
            catch (Exception ex)
            {
                LoadStateText.Text = Loc.T("settings.loadState.backendSyncFailed",
                    "⚠️ 部分设置未能同步到后端：{0}", ex.Message);
            }
        }

        _dirty = false;
        SaveHintText.Text = "";
    }
}
