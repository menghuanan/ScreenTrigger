using Microsoft.UI.Xaml;
using Microsoft.UI.Dispatching;
using ScreenTrigger.Host;

namespace ScreenTrigger;

public partial class App : Application
{
    private MainWindow? main;
    private readonly Task<HostClient> opening;

    public App()
    {
        var root = Environment.GetEnvironmentVariable("SCREENTRIGGER_DEV_ROOT");
        if (string.IsNullOrEmpty(root) || !System.IO.File.Exists(System.IO.Path.Combine(root, "desktop", "ScreenTrigger", "ScreenTrigger.csproj")) ||
            !System.IO.File.Exists(System.IO.Path.Combine(root, ".venv", "Scripts", "python.exe")))
        {
            Environment.Exit(2);
        }
        opening = OpenHost();
        InitializeComponent();
    }

    internal static Task<HostClient> OpenHost()
    {
        // Portable verification can proceed while WinUI loads the app resources and first window.
        var queue = DispatcherQueue.GetForCurrentThread();
        return Task.Run(() => new HostClient(action => queue.TryEnqueue(() => action())));
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        main ??= new MainWindow(opening);
        main.Activate();
    }
}
