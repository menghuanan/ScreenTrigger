using Microsoft.UI.Xaml;

namespace SelectionProbe;

public partial class App : Application
{
    private MainWindow? main;

    public App()
    {
        UnhandledException += (_, error) => Console.Error.WriteLine(error.Exception);
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        main = new MainWindow();
        main.Activate();
    }
}
