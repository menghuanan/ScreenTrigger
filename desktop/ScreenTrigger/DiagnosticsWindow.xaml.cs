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
            var info = await host.CallAsync(new { op = "diagnostics" });
            if (closed || version != current) return;
            string? session = info.GetProperty("session_id").GetString();
            string handshake = session != null && host.Events.Session == session ? "已收到 · seq 1" : "未收到";
            string revision = "—";
            if (info.GetProperty("state").GetString() == "ready")
            {
                try { revision = (await host.GetConfigAsync()).Revision.ToString(); }
                catch (Exception ex) { revision = ex.Message; }
            }
            if (closed || version != current) return;
            Snapshot.Text = $"会话　{session ?? "—"}\n进程　{info.GetProperty("pid")}\n就绪事件　{handshake}\n" +
                $"配置修订　{revision}\n进程树托管　{info.GetProperty("job_object_active")}\n协议异常行　{info.GetProperty("malformed_line_count")}\n" +
                $"最近退出码　{info.GetProperty("exit_code")}";
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
