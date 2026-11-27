using Microsoft.UI.Input;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using ScreenTrigger.Host;
using Windows.Graphics;
using Windows.System;
using Windows.UI.Core;

namespace ScreenTrigger.User;

public sealed partial class ShellWindow : Window
{
    private readonly UserSession session = new();
    private readonly ScriptsView scripts;
    private readonly GroupsView groups;
    private readonly LogsView logs;
    private readonly AboutView about;
    private readonly Task connecting;
    private NavigationViewItem current;
    private int navigationRequest;
    private IDisposable? subscription;
    private bool closing, drained, selecting;

    internal ShellWindow(Task<HostClient> opening)
    {
        InitializeComponent();
        ExtendsContentIntoTitleBar = AppWindowTitleBar.IsCustomizationSupported();
        if (ExtendsContentIntoTitleBar) SetTitleBar(TitleArea);
        AppWindow.Resize(new SizeInt32(1060, 720));
        scripts = new(session, () => (AppWindow.Position, AppWindow.Size));
        groups = new(session);
        logs = new(session, scripts.NameForId);
        about = new();
        current = ScriptsNav;
        Navigation.SelectedItem = current;
        PageHost.Content = scripts;
        scripts.IsEnabled = groups.IsEnabled = false;
        session.BusyChanged += busy => scripts.IsEnabled = groups.IsEnabled = session.Ready && !busy;
        session.Failed += msg => { Notice.Message = msg; Notice.IsOpen = true; };
        AppWindow.Closing += OnClosing;
        connecting = Connect(opening);
    }

    private async Task Connect(Task<HostClient> opening)
    {
        try
        {
            var host = session.Host = await opening;
            if (closing) return;
            host.AttachWindow((request, token) => {
                token.ThrowIfCancellationRequested();
                object value = request.GetProperty("op").GetString() switch {
                    "rect" => new { x = AppWindow.Position.X, y = AppWindow.Position.Y, width = AppWindow.Size.Width, height = AppWindow.Size.Height },
                    "visible" => AppWindow.IsVisible,
                    "close_overlay" => true,
                    _ => throw new InvalidOperationException("此用户界面没有活动框选窗口。")
                };
                return Task.FromResult<object?>(value);
            });
            subscription = host.Events.Subscribe(Receive);
            host.RecoveryFailed += session.Error;
            host.RecoveryCompleted += Recover;
            host.Events.BeginStart();
            var status = await host.CallAsync(new { op = "start" });
            if (closing) return;
            if (status.GetProperty("state").GetString() != "ready")
                throw new InvalidOperationException(status.GetProperty("last_error").ToString() is { Length: > 0 } error ? error : "引擎启动失败。");
            await scripts.Reload();
            if (closing) return;
            session.Ready = scripts.IsEnabled = groups.IsEnabled = true;
            session.Log("引擎已就绪");
            if (current == GroupsNav) await groups.Reload();
            logs.HistoryChanged();
        }
        catch (Exception e) { if (!closing) session.Error(e); }
    }

    private void Receive(EngineEvent evt)
    {
        if (closing) return;
        if (evt.Name == "script_state_changed") scripts.ApplyState(evt.Payload);
        if (evt.Name is "script_state_changed" or "monitor_state_changed") logs.HistoryChanged();
        if (evt.Name == "system_error" && evt.Payload.TryGetProperty("error", out var error))
            session.Error(new InvalidOperationException(error.GetProperty("message").GetString()));
    }

    private void Recover(RecoverySnapshot snapshot)
    {
        scripts.Recover(snapshot);
        logs.HistoryChanged();
    }

    private void SelectItem(NavigationViewItem item)
    {
        selecting = true;
        try { Navigation.SelectedItem = item; }
        finally { selecting = false; }
    }

    private async void Navigate(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (selecting || current == null || args.SelectedItem is not NavigationViewItem next) return;
        int request = ++navigationRequest;
        if (next == current) return;
        SelectItem(current);
        // A click can arrive while the host is acknowledging Start, Save, or another page load.
        while (session.Busy && !closing && request == navigationRequest) await session.WhenIdle;
        if (closing || request != navigationRequest) return;
        await session.Run(async () => {
            if (!await scripts.LeaveEditor() || request != navigationRequest) return;
            current = next;
            SelectItem(next);
            Notice.IsOpen = false;
            PageHost.Content = next.Tag switch {
                "groups" => groups, "logs" => logs, "about" => about, "settings" => null, _ => scripts
            };
            if (!session.Ready) return;
            if (next == GroupsNav) await groups.Reload();
            if (next == ScriptsNav) await scripts.Reload();
        });
        SelectItem(current);
    }

    private async void OnShortcut(object sender, KeyRoutedEventArgs e)
    {
        if (closing || !session.Ready || session.Busy || e.KeyStatus.WasKeyDown || Root.XamlRoot == null) return;
        string? key = e.Key switch {
            >= VirtualKey.A and <= VirtualKey.Z => e.Key.ToString(),
            >= VirtualKey.F1 and <= VirtualKey.F12 => e.Key.ToString(), _ => null
        };
        if (key == null || Modified() || VisualTreeHelper.GetOpenPopupsForXamlRoot(Root.XamlRoot).Count != 0) return;
        for (var p = FocusManager.GetFocusedElement(Root.XamlRoot) as DependencyObject; p != null; p = VisualTreeHelper.GetParent(p))
            if (p is TextBox or PasswordBox or RichEditBox or ComboBox or DropDownButton or ContentDialog) return;
        if (scripts.FindKey(key) is not { } row) return;
        e.Handled = true;
        await session.Run(() => scripts.Toggle(row));
    }

    private static bool Modified() => new[] { VirtualKey.Control, VirtualKey.Shift, VirtualKey.Menu,
        VirtualKey.LeftWindows, VirtualKey.RightWindows }.Any(key =>
            InputKeyboardSource.GetKeyStateForCurrentThread(key).HasFlag(CoreVirtualKeyStates.Down));

    private async void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (drained) return;
        args.Cancel = true;
        if (closing) return;
        closing = true;
        try
        {
            await session.WhenIdle;
            if (!await scripts.LeaveEditor()) { closing = false; return; }
            logs.StopWatching();
            about.Cancel();
            await connecting;
            subscription?.Dispose();
            if (session.Host is { } host)
            {
                host.RecoveryCompleted -= Recover;
                host.RecoveryFailed -= session.Error;
                await host.DisposeAsync();
            }
            drained = true;
            Close();
        }
        catch (Exception e) { closing = false; session.Error(e); }
    }
}
