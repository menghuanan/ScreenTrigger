using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;
using ScreenTrigger.Contracts;
using Windows.Graphics;

namespace ScreenTrigger;

public sealed partial class MainWindow : Window
{
    private HostClient? host;
    private readonly Task connecting;
    private Window? diagnostics;
    private readonly DispatcherQueueTimer healthTimer;
    private bool changing, polling, closing, drained;
    private int generation;
    private string lastSnapshot = "";
    private IDisposable? mainSubscription;
    private bool ocrAvailable, readingModels;

    public MainWindow() : this(App.OpenHost()) { }

    internal MainWindow(Task<HostClient> opening)
    {
        InitializeComponent();
        AppWindow.Resize(new SizeInt32(820, 640));
        healthTimer = DispatcherQueue.CreateTimer();
        healthTimer.Interval = TimeSpan.FromSeconds(1);
        healthTimer.Tick += (_, _) => RefreshHealth();
        AppWindow.Closing += OnClosing;
        connecting = ConnectAsync(opening);
    }

    private async Task ConnectAsync(Task<HostClient> opening)
    {
        try
        {
            host = await opening;
            if (closing) return;
            host.AttachWindow(HandleWindowAction);
            mainSubscription = host.Events.Subscribe(ReceiveEvent);
            host.RecoveryFailed += ShowError;
            host.RecoveryCompleted += RecoverExecutions;
            DiagnosticsButton.IsEnabled = true;
            healthTimer.Start();
            ChangeEngine("start");
        }
        catch (Exception ex) { if (!closing) { Health.Text = "宿主不可用"; ShowError(ex); } }
    }

    private async void ReceiveEvent(EngineEvent message)
    {
        if (closing) return;
        if (message.Name == "system_ready") RefreshHealth();
        if (message.Name == "system_error" && message.Payload.TryGetProperty("error", out var fault))
            Notice($"{fault.GetProperty("code")} · {fault.GetProperty("message")}");
        if (ActiveExecution.SelectedItem is CatalogItem active &&
            message.Payload.TryGetProperty("execution_id", out var eid) && active.Id == eid.GetString())
        {
            if (message.Name == "recognition_stats") RunStatus.Text = "监控 · " + Status.RunFeedback.Stats(message.Payload);
            if (message.Name == "target_detected") RunStatus.Text = Status.RunFeedback.Match(message.Payload.GetProperty("result"));
        }
        if (!closing && message.Name == "script_state_changed")
        {
            var execution = message.Payload.GetProperty("execution_id").GetString()!;
            runSequences[execution] = message.Sequence;
            if (message.Payload.GetProperty("status").GetString() is "completed" or "failed" or "cancelled")
            {
                terminalSequenceOrder.Enqueue(execution);
                if (terminalSequenceOrder.Count > 1000) runSequences.Remove(terminalSequenceOrder.Dequeue());
            }
            try { await UpdateRun(message.Payload); } catch (Exception ex) { RunStatus.Text = ex.Message; }
        }
        if (!closing && message.Name == "selection_completed")
        {
            try
            {
                var region = message.Payload.GetProperty("region");
                await SetImageAsync(WireContracts.Read<ImageRef>(message.Payload.GetProperty("image"), "imageRef"), message.Session,
                                    region.GetProperty("x").GetInt32(), region.GetProperty("y").GetInt32());
            }
            catch (Exception ex) { ImageStatus.Text = ex.Message; }
        }
    }

    private async void RecoverExecutions(RecoverySnapshot snapshot)
    {
        try
        {
            foreach (var (id, state) in snapshot.Executions)
                if (!closing && host?.Events.Session == snapshot.Session && runSequences.GetValueOrDefault(id) <= snapshot.Sequence)
                    await UpdateRun(state);
        }
        catch (Exception ex) { if (!closing) ShowError(ex); }
    }

    private async void RefreshHealth()
    {
        if (host == null || changing || polling || closing) return;
        polling = true;
        int seen = generation;
        try
        {
            var status = await host.CallAsync(new { op = "status" });
            if (!closing && seen == generation) ShowStatus(status);
        }
        catch (Exception ex) { if (!closing) ShowError(ex); }
        finally { polling = false; }
    }

    private void StartEngine(object sender, RoutedEventArgs e) => ChangeEngine("start");
    private async void StopEngine(object sender, RoutedEventArgs e)
    {
        if (await ResolveWorkspaceDrafts()) ChangeEngine("stop");
    }

    private async void ChangeEngine(string op)
    {
        if (host == null || changing || closing) return;
        changing = true;
        generation++;
        if (op == "start") host.Events.BeginStart(); else host.Events.End();
        StartButton.IsEnabled = StopButton.IsEnabled = false;
        SetModelAvailability(false);
        Health.Text = op == "start" ? "正在启动…" : "正在停止…";
        ErrorBar.IsOpen = false;
        lastSnapshot = "";
        try
        {
            var status = await host.CallAsync(new { op });
            if (!closing) ShowStatus(status);
        }
        catch (Exception ex) { if (!closing) ShowError(ex); }
        finally { changing = false; RefreshHealth(); }
    }

    private void ShowStatus(JsonElement status)
    {
        string state = status.GetProperty("state").GetString()!;
        bool ready = state == "ready";
        SetWorkspaceAvailability(ready, status.GetProperty("capabilities"));
        Health.Text = EnginePresentation.State(state);
        ServiceStatus.Text = Health.Text;
        StartButton.IsEnabled = state is "idle" or "exited";
        StopButton.IsEnabled = ready;
        var error = status.GetProperty("last_error");
        if (error.ValueKind == JsonValueKind.String && !ErrorBar.IsOpen)
        {
            ErrorBar.Message = error.GetString();
            ErrorBar.IsOpen = true;
        }
        Versions.Text = EnginePresentation.Versions(status.GetProperty("versions"));
        var capabilities = status.GetProperty("capabilities");
        CapabilitiesHeader.Visibility = ready ? Visibility.Visible : Visibility.Collapsed;
        string snapshot = capabilities.GetRawText();
        if (snapshot == lastSnapshot) return;
        lastSnapshot = snapshot;
        SetModelAvailability(ready && EnginePresentation.HasOcr(capabilities));
        Capabilities.ItemsSource = EnginePresentation.Capabilities(capabilities);
    }

    private void SetModelAvailability(bool available)
    {
        ocrAvailable = available;
        ModelsButton.IsEnabled = available && !readingModels && !closing;
        if (!available)
        {
            ModelList.Visibility = Visibility.Collapsed;
            ModelList.Text = "";
            ModelsButton.Content = "查看 OCR 模型";
        }
    }

    private async void InspectModels(object sender, RoutedEventArgs e)
    {
        if (host == null || !ocrAvailable || readingModels || closing) return;
        if (ModelList.Visibility == Visibility.Visible)
        {
            ModelList.Visibility = Visibility.Collapsed;
            ModelsButton.Content = "查看 OCR 模型";
            return;
        }
        int sessionGeneration = generation;
        readingModels = true;
        ModelsButton.IsEnabled = false;
        try
        {
            var models = await EnginePresentation.ReadModels(host);
            if (closing || !ocrAvailable || generation != sessionGeneration) return;
            ModelList.Text = models;
            ModelList.Visibility = Visibility.Visible;
            ModelsButton.Content = "收起模型列表";
        }
        catch (Exception ex) { if (!closing && generation == sessionGeneration) ShowError(ex); }
        finally
        {
            readingModels = false;
            ModelsButton.IsEnabled = !closing && ocrAvailable;
        }
    }

    private void OpenDiagnostics(object sender, RoutedEventArgs e)
    {
        if (host == null || closing) return;
        if (diagnostics == null)
        {
            diagnostics = new DiagnosticsWindow(host);
            diagnostics.Closed += (_, _) =>
            {
                diagnostics = null;
                if (!closing) { Activate(); DiagnosticsButton.Focus(FocusState.Programmatic); }
            };
        }
        diagnostics.Activate();
    }

    private void ShowError(Exception ex) { ErrorBar.Message = ex.Message; ErrorBar.IsOpen = true; }

    private async Task<bool> ResolveWorkspaceDrafts()
    {
        if (resolvingDraft || closing) return false;
        resolvingDraft = true;
        try
        {
            foreach (var (tag, page) in pages.ToArray())
            {
                if (page is not IWorkspaceDraft editor) continue;
                if (!page.IsEnabled) { Notice("请等待当前编辑操作完成后重试。"); return false; }
                if (!editor.HasChanges) continue;
                DisplayPage(Navigation.MenuItems.OfType<NavigationViewItem>().First(item => Equals(item.Tag, tag)));
                if (!await editor.ResolveChanges()) return false;
            }
            return true;
        }
        catch (Exception ex) { Notice(ex.Message); return false; }
        finally { resolvingDraft = false; }
    }

    private async void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (drained) return;
        args.Cancel = true;
        if (closing || !await ResolveWorkspaceDrafts()) return;
        closing = true;
        overlay?.CloseFromHost();
        overlay = null;
        healthTimer.Stop();
        SetModelAvailability(false);
        mainSubscription?.Dispose();
        diagnostics?.Close();
        StartButton.IsEnabled = StopButton.IsEnabled = DiagnosticsButton.IsEnabled = false;
        try
        {
            await connecting;
            if (host != null) await host.DisposeAsync();
        }
        finally
        {
            drained = true;
            DispatcherQueue.TryEnqueue(() => { Close(); Application.Current.Exit(); });
        }
    }
}
