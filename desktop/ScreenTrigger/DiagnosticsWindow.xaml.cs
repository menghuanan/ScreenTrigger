using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using ScreenTrigger.Host;
using Windows.Graphics;

namespace ScreenTrigger;

public sealed partial class DiagnosticsWindow : Window
{
    private readonly HostClient host;
    private readonly IDisposable subscription;
    private bool closed;
    private int version;

    internal DiagnosticsWindow(HostClient host)
    {
        this.host = host;
        InitializeComponent();
        AppWindow.Resize(new SizeInt32(600, 400));
        subscription = host.Events.Subscribe(e => { if (e.Name == "system_ready") Refresh(); });
        Closed += (_, _) => { closed = true; subscription.Dispose(); };
        Snapshot.Loaded += (_, _) => Refresh();
    }

    private void RefreshClicked(object sender, RoutedEventArgs args) => Refresh();

    private async void Refresh()
    {
        if (closed) return;
        int current = ++version;
        RefreshButton.IsEnabled = false;
        try
        {
            var info = await EnginePresentation.ReadDiagnostics(host);
            if (closed || version != current) return;
            Snapshot.Text = info;
        }
        catch (Exception ex) { if (!closed && version == current) Snapshot.Text = ex.Message; }
        finally
        {
            if (!closed && version == current)
            {
                AutomationProperties.SetAutomationId(Snapshot, "EngineDiagnostics");
                RefreshButton.IsEnabled = true;
            }
        }
    }
}
