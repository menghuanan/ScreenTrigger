using Microsoft.UI.Xaml;

namespace ScreenTrigger;

public partial class App : Application
{
    private MainWindow? main;
    public App() => InitializeComponent();
    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        main = new MainWindow();
        main.Activate();
    }
}
