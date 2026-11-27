using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using ScreenTrigger.Host;

namespace ScreenTrigger.User;

public partial class App : Application
{
    private readonly Task<HostClient> opening;
    private ShellWindow? window;

    public App()
    {
        Microsoft.Windows.Globalization.ApplicationLanguages.PrimaryLanguageOverride = "zh-CN";
        var queue = DispatcherQueue.GetForCurrentThread();
        opening = Task.Run(() => new HostClient(action => queue.TryEnqueue(() => action())));
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        window = new ShellWindow(opening);
        window.Activate();
    }
}
