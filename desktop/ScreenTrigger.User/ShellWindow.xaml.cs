using System.Text.Json;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;
using Windows.Graphics;
using WinRT.Interop;

namespace ScreenTrigger.User;

public sealed partial class ShellWindow : Window
{
    private readonly UserSession session = new();
    private readonly WorkspaceSurface surface;
    private readonly WorkView work;
    private readonly ScriptsView scripts;
    private readonly GroupsView groups;
    private readonly LogsView logs;
    private readonly AboutView about;
    private readonly SettingsView settings;
    private readonly Microsoft.UI.Dispatching.DispatcherQueueTimer healthTimer;
    private readonly Task connecting;
    private NavigationViewItem current;
    private int navigationRequest;
    private IDisposable? subscription;
    private bool closing, drained, selecting;
    private bool changingEngine, polling;
    private int engineGeneration;
    private string? workspaceSession;
    private long displayRevision = -1;

    internal ShellWindow(Task<HostClient> opening)
    {
        InitializeComponent();
        ExtendsContentIntoTitleBar = AppWindowTitleBar.IsCustomizationSupported();
        if (ExtendsContentIntoTitleBar) SetTitleBar(TitleArea);
        AppWindow.Resize(new SizeInt32(1060, 720));
        surface = new(this, session);
        work = new(session, surface);
        scripts = new(session, work);
        groups = new(session, surface);
        logs = new(session, scripts.NameForId);
        about = new();
        settings = new(session, ChangeEngine);
        current = WorkNav;
        Navigation.SelectedItem = current;
        PageHost.Content = work;
        work.IsEnabled = scripts.IsEnabled = groups.IsEnabled = false;
        session.BusyChanged += busy => work.IsEnabled = scripts.IsEnabled = groups.IsEnabled = session.Ready && !busy;
        session.Failed += msg => { Notice.Message = msg; Notice.IsOpen = true; };
        healthTimer = DispatcherQueue.CreateTimer();
        healthTimer.Interval = TimeSpan.FromSeconds(1);
        healthTimer.Tick += (_, _) => _ = RefreshHealth();
        AppWindow.Closing += OnClosing;
        connecting = Connect(opening);
    }

    private async Task Connect(Task<HostClient> opening)
    {
        try
        {
            var host = session.Host = await opening;
            if (closing) return;
            host.AttachWindow(surface.Handle);
            subscription = host.Events.Subscribe(Receive);
            host.RecoveryFailed += session.Error;
            host.RecoveryCompleted += Recover;
            settings.Attach(host);
            healthTimer.Start();
            while (session.Busy && !closing) await session.WhenIdle;
            if (!closing) await ChangeEngine("start");
        }
        catch (Exception e)
        {
            if (closing) return;
            settings.HostUnavailable(e.Message);
            session.Error(e);
        }
    }

    private Task ChangeEngine(string op) => session.Run(async () =>
    {
        if (session.Host is not { } host || closing) return;
        if (op == "stop" && (!await ResolveSettingsDraft() || !await scripts.LeaveEditor() || !await groups.Leave() || closing)) return;
        changingEngine = true;
        engineGeneration++;
        settings.BeginChange(op);
        Notice.IsOpen = false;
        try
        {
            if (op == "start") host.Events.BeginStart();
            var status = await host.CallAsync(new { op });
            if (!closing) await ApplyEngineStatus(host, status);
        }
        catch (Exception e)
        {
            if (!closing) { settings.ShowError(e.Message); session.Error(e); }
        }
        finally { changingEngine = false; }
        await RefreshHealth();
    });

    private async Task RefreshHealth()
    {
        if (session.Host is not { } host || changingEngine || polling || closing) return;
        polling = true;
        int generation = engineGeneration;
        try
        {
            var status = await host.CallAsync(new { op = "status" });
            if (!closing && generation == engineGeneration) await ApplyEngineStatus(host, status);
        }
        catch (Exception e) { if (!closing && generation == engineGeneration) settings.ShowError(e.Message); }
        finally { polling = false; }
    }

    private async Task ApplyEngineStatus(HostClient host, JsonElement status)
    {
        settings.ShowStatus(status);
        bool ready = status.GetProperty("state").GetString() == "ready";
        var active = ready ? host.Events.Session : null;
        if (workspaceSession == active) return;

        work.IsEnabled = scripts.IsEnabled = groups.IsEnabled = session.Ready = false;
        if (workspaceSession != null) { surface.Reset(); scripts.EndEngineSession(); groups.EndEngineSession(); work.Reset(); }
        workspaceSession = active;
        displayRevision = -1;
        settings.SetPreferences(null, ApplyDisplayConfig, WindowNative.GetWindowHandle(this));
        if (active == null)
        {
            if (!ready) host.Events.End();
            session.Log("引擎已停止");
            logs.HistoryChanged();
            return;
        }
        bool Current() => !closing && workspaceSession == active && host.Events.Session == active;
        try
        {
            var data = await host.RequestCheckedAsync(active, "get_config", new { });
            if (!Current()) return;
            ApplyDisplayConfig(data.GetProperty("config"));
            if (data.TryGetProperty("recovery", out var restored))
            {
                Notice.Message = $"配置损坏，已自动恢复备份修订 {restored.GetProperty("revision")}。";
                Notice.IsOpen = true;
            }
        }
        catch (Exception e) { if (Current()) session.Error(e); }
        if (!Current()) return;
        try
        {
            await scripts.Reload();
            if (Current() && current == WorkNav) await work.Reload(scripts.Rows);
            if (Current() && current == GroupsNav) await groups.Reload();
        }
        catch (Exception e) { if (Current()) session.Error(e); }
        if (!Current()) return;
        settings.SetPreferences(host, ApplyDisplayConfig, WindowNative.GetWindowHandle(this));
        session.Ready = true;
        work.IsEnabled = scripts.IsEnabled = groups.IsEnabled = !session.Busy;
        session.Log("引擎已就绪");
        logs.HistoryChanged();
    }

    private void ApplyDisplayConfig(JsonElement config)
    {
        long revision = config.GetProperty("revision").GetInt64();
        if (revision < displayRevision) return;
        displayRevision = revision;
        surface.SetPreviewFrequency(config.GetProperty("capture").GetProperty("preview_hz").GetInt32());
        Root.RequestedTheme = config.GetProperty("general").GetProperty("theme").GetString() switch
        {
            "light" => ElementTheme.Light, "dark" => ElementTheme.Dark, _ => ElementTheme.Default
        };
    }

    private async Task<bool> ResolveSettingsDraft()
    {
        if (settings.Draft is not { } draft) return true;
        await draft.WhenIdle;
        return draft != settings.Draft || !draft.HasChanges || await draft.ResolveChanges();
    }

    private void Receive(EngineEvent evt)
    {
        surface.Receive(evt);
        if (closing) return;
        if (evt.Name == "system_ready") _ = RefreshHealth();
        if (evt.Name == "script_state_changed") work.ApplyState(evt.Payload);
        if (evt.Name is "script_state_changed" or "monitor_state_changed") logs.HistoryChanged();
        if (evt.Name == "system_error" && evt.Payload.TryGetProperty("error", out var error))
            session.Error(new InvalidOperationException(error.GetProperty("message").GetString()));
    }

    private void Recover(RecoverySnapshot snapshot)
    {
        foreach (var state in snapshot.Executions.Values) work.ApplyState(state);
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
            if (!await scripts.LeaveEditor() || (current == GroupsNav && !await groups.Leave()) || (current == SettingsNav && !await ResolveSettingsDraft()) ||
                request != navigationRequest || closing) return;
            current = next;
            SelectItem(next);
            Notice.IsOpen = false;
            PageHost.Content = next.Tag switch {
                "work" => work, "groups" => groups, "logs" => logs, "about" => about, "settings" => settings, _ => scripts
            };
            if (!session.Ready) return;
            if (next == GroupsNav) await groups.Reload();
            if (next == ScriptsNav) await scripts.Reload();
            if (next == WorkNav) { await scripts.Reload(); await work.Reload(scripts.Rows); }
        });
        SelectItem(current);
    }

    private async void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (drained) return;
        args.Cancel = true;
        if (closing) return;
        closing = true;
        try
        {
            await surface.CancelForClose();
            await session.WhenIdle;
            if (!await scripts.LeaveEditor() || !await groups.Leave() || !await ResolveSettingsDraft()) { closing = false; return; }
            healthTimer.Stop();
            settings.Attach(null);
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
