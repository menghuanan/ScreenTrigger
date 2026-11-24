using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;
using ScreenTrigger.Contracts;
using Windows.Graphics;

namespace ScreenTrigger;

public sealed record CapabilityRow(string Id, string Summary, string Reason)
{
    public string AutomationId => $"Capability_{Id}";
}

public sealed partial class MainWindow : Window
{
    private HostClient? host;
    private Window? diagnostics;
    private readonly DispatcherQueueTimer healthTimer;
    private bool changing, polling, closing, drained;
    private int generation;
    private string lastSnapshot = "";
    private IDisposable? mainSubscription;
    private bool ocrAvailable, readingModels;

    public MainWindow()
    {
        InitializeComponent();
        AppWindow.Resize(new SizeInt32(820, 640));
        healthTimer = DispatcherQueue.CreateTimer();
        healthTimer.Interval = TimeSpan.FromSeconds(1);
        healthTimer.Tick += (_, _) => RefreshHealth();
        AppWindow.Closing += OnClosing;
        StartButton.Loaded += Connect;
    }

    private void Connect(object sender, RoutedEventArgs args)
    {
        StartButton.Loaded -= Connect;
        try
        {
            // Only enqueue copied events from the native router thread.
            host = new HostClient(action => DispatcherQueue.TryEnqueue(() => action()));
            mainSubscription = host.Events.Subscribe(ReceiveEvent);
            host.RecoveryFailed += ShowError;
            DiagnosticsButton.IsEnabled = true;
            healthTimer.Start();
            StartEngine(this, args);
        }
        catch (Exception ex) { Health.Text = "宿主不可用"; ShowError(ex); }
    }

    private void ReceiveEvent(EngineEvent message)
    {
        if (!closing && message.Name == "system_ready") RefreshHealth();
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
    private void StopEngine(object sender, RoutedEventArgs e) => ChangeEngine("stop");

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
        Health.Text = state switch { "ready" => "引擎就绪", "idle" => "引擎未启动", "starting" => "正在启动…",
            "stopping" => "正在停止…", "exited" => "引擎已退出", _ => state };
        StartButton.IsEnabled = state is "idle" or "exited";
        StopButton.IsEnabled = ready;
        var error = status.GetProperty("last_error");
        if (error.ValueKind == JsonValueKind.String && !ErrorBar.IsOpen)
        {
            ErrorBar.Message = error.GetString();
            ErrorBar.IsOpen = true;
        }
        var versions = status.GetProperty("versions");
        Versions.Text = versions.ValueKind == JsonValueKind.Object
            ? $"引擎 {versions.GetProperty("engine_version")}　·　协议 {versions.GetProperty("protocol_version")}　·　{versions.GetProperty("lua_version")}"
            : "";
        var capabilities = status.GetProperty("capabilities");
        CapabilitiesHeader.Visibility = ready ? Visibility.Visible : Visibility.Collapsed;
        string snapshot = capabilities.GetRawText();
        if (snapshot == lastSnapshot) return;
        lastSnapshot = snapshot;
        SetModelAvailability(ready && capabilities.EnumerateArray().Any(cap =>
            cap.GetProperty("id").GetString() == "ocr" && cap.GetProperty("state").GetString() == "available"));
        Capabilities.ItemsSource = capabilities.EnumerateArray().Select(cap =>
        {
            string? id = cap.GetProperty("id").GetString();
            string? label = id switch { "ocr" => "文字识别（OCR）", "lua_script" => "Lua 脚本", _ => id };
            string? stateLabel = cap.GetProperty("state").GetString() switch
            { "available" => "可用", "pending" => "待确认", _ => "不可用" };
            return new CapabilityRow(id ?? "", $"{label} · {stateLabel}", cap.GetProperty("reason").GetString() ?? "");
        }).ToList();
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
            var hello = await host.HelloAsync();
            if (closing || !ocrAvailable || generation != sessionGeneration) return;
            var ids = hello.ModelManifests.Where(m => m.ProviderId == "builtin.ocr")
                .SelectMany(m => m.ModelIds).Distinct().ToArray();
            ModelList.Text = ids.Length == 0 ? "引擎未返回已安装的 OCR 模型。" : string.Join("\n", ids);
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

    private async void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (drained) return;
        args.Cancel = true;
        if (closing) return;
        closing = true;
        healthTimer.Stop();
        SetModelAvailability(false);
        mainSubscription?.Dispose();
        diagnostics?.Close();
        StartButton.IsEnabled = StopButton.IsEnabled = DiagnosticsButton.IsEnabled = false;
        try { if (host != null) await host.DisposeAsync(); }
        finally
        {
            drained = true;
            DispatcherQueue.TryEnqueue(() => { Close(); Application.Current.Exit(); });
        }
    }
}
