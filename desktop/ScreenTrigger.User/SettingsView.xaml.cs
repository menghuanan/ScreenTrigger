using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;
using ScreenTrigger.Settings;

namespace ScreenTrigger.User;

public sealed partial class SettingsView : UserControl
{
    private readonly UserSession session;
    private readonly Func<string, Task> changeEngine;
    private HostClient? host;
    private string state = "";
    private string capabilitySnapshot = "";
    private bool ocrAvailable, readingModels;
    private int engineVersion, diagnosticsVersion;

    internal SettingsView(UserSession session, Func<string, Task> changeEngine)
    {
        InitializeComponent();
        this.session = session;
        this.changeEngine = changeEngine;
        session.BusyChanged += _ => UpdateButtons();
    }

    internal IWorkspaceDraft? Draft => PreferencesHost.Content as IWorkspaceDraft;

    internal void SetPreferences(HostClient? readyHost, Action<JsonElement> applyConfig, nint owner)
    {
        PreferencesHost.Content = readyHost == null ? null : new SettingsPage(readyHost, applyConfig, owner);
    }

    internal void Attach(HostClient? value)
    {
        host = value;
        engineVersion++;
        diagnosticsVersion++;
        UpdateButtons();
    }

    internal void HostUnavailable(string message)
    {
        Health.Text = "宿主不可用";
        ShowError(message);
    }

    internal void BeginChange(string op)
    {
        SetState(op == "start" ? "starting" : "stopping");
        ShowError("");
        ShowCapabilities(default);
    }

    internal void ShowStatus(JsonElement status)
    {
        SetState(status.GetProperty("state").GetString()!);
        var versions = EnginePresentation.Versions(status.GetProperty("versions"));
        if (Versions.Text != versions) Versions.Text = versions;
        Versions.Visibility = versions.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        ShowError(status.GetProperty("last_error").GetString() ?? "");
        ShowCapabilities(state == "ready" ? status.GetProperty("capabilities") : default);
    }

    private void SetState(string value)
    {
        if (state == value) return;
        state = value;
        engineVersion++;
        Health.Text = EnginePresentation.State(state);
        UpdateButtons();
        if (DiagnosticsCard.Visibility == Visibility.Visible) _ = ReadDiagnostics();
    }

    internal void ShowError(string message)
    {
        if (EngineError.Text != message) EngineError.Text = message;
        EngineError.Visibility = message.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    }

    private void ShowCapabilities(JsonElement capabilities)
    {
        var snapshot = capabilities.ValueKind == JsonValueKind.Array ? capabilities.GetRawText() : "";
        if (capabilitySnapshot == snapshot) return;
        capabilitySnapshot = snapshot;
        bool ready = snapshot.Length > 0;
        CapabilitiesPanel.Visibility = ready ? Visibility.Visible : Visibility.Collapsed;
        Capabilities.ItemsSource = ready ? EnginePresentation.Capabilities(capabilities) : null;
        ocrAvailable = ready && EnginePresentation.HasOcr(capabilities);
        if (!ocrAvailable) HideModels();
        UpdateButtons();
    }

    private void UpdateButtons()
    {
        StartButton.IsEnabled = host != null && !session.Busy && state is "idle" or "exited";
        StopButton.IsEnabled = host != null && !session.Busy && state == "ready";
        DiagnosticsButton.IsEnabled = host != null;
        ModelsButton.IsEnabled = host != null && ocrAvailable && !readingModels;
    }

    private async void StartEngine(object sender, RoutedEventArgs args) => await changeEngine("start");
    private async void StopEngine(object sender, RoutedEventArgs args) => await changeEngine("stop");

    private void HideModels()
    {
        ModelList.Visibility = Visibility.Collapsed;
        ModelList.Text = "";
        ModelsButton.Content = "查看 OCR 模型";
    }

    private async void InspectModels(object sender, RoutedEventArgs args)
    {
        if (host is not { } source || !ocrAvailable || readingModels) return;
        if (ModelList.Visibility == Visibility.Visible) { HideModels(); return; }
        int version = engineVersion;
        readingModels = true;
        UpdateButtons();
        try
        {
            var models = await EnginePresentation.ReadModels(source);
            if (version != engineVersion || !ocrAvailable) return;
            ModelList.Text = models;
            ModelList.Visibility = Visibility.Visible;
            ModelsButton.Content = "收起模型列表";
        }
        catch (Exception ex) { if (version == engineVersion) session.Error(ex); }
        finally { readingModels = false; UpdateButtons(); }
    }

    private async void ToggleDiagnostics(object sender, RoutedEventArgs args)
    {
        if (DiagnosticsCard.Visibility == Visibility.Visible)
        {
            DiagnosticsCard.Visibility = Visibility.Collapsed;
            DiagnosticsButton.Content = "查看诊断";
            diagnosticsVersion++;
            return;
        }
        DiagnosticsCard.Visibility = Visibility.Visible;
        DiagnosticsButton.Content = "收起诊断";
        await ReadDiagnostics();
    }

    private async void RefreshDiagnostics(object sender, RoutedEventArgs args) => await ReadDiagnostics();

    private async Task ReadDiagnostics()
    {
        if (host is not { } source) return;
        int version = ++diagnosticsVersion;
        RefreshDiagnosticsButton.IsEnabled = false;
        DiagnosticsDetails.Text = "正在读取诊断…";
        try
        {
            var details = await EnginePresentation.ReadDiagnostics(source);
            if (version == diagnosticsVersion) DiagnosticsDetails.Text = details;
        }
        catch (Exception ex) { if (version == diagnosticsVersion) DiagnosticsDetails.Text = ex.Message; }
        finally { if (version == diagnosticsVersion) RefreshDiagnosticsButton.IsEnabled = true; }
    }
}
