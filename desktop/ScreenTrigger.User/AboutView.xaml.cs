using System.Reflection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace ScreenTrigger.User;

public sealed partial class AboutView : UserControl
{
    private readonly CancellationTokenSource lifetime = new();
    private readonly string version = typeof(AboutView).Assembly
        .GetCustomAttribute<AssemblyInformationalVersionAttribute>()!.InformationalVersion.Split('+')[0];

    internal AboutView()
    {
        InitializeComponent();
        VersionLabel.Text = $"v{version}";
    }
    internal void Cancel() => lifetime.Cancel();

    private async void Check(object sender, RoutedEventArgs e)
    {
        CheckButton.IsEnabled = false;
        Result.Text = "正在检查更新…";
        try { Result.Text = await ReleaseChecker.CheckAsync(version, lifetime.Token); }
        catch (OperationCanceledException) when (lifetime.IsCancellationRequested) { }
        catch (OperationCanceledException) { Result.Text = "检查更新超时，请稍后重试。"; }
        catch (System.Net.Http.HttpRequestException ex)
        {
            Result.Text = ex.StatusCode is { } status ? $"暂时无法读取项目的更新日志（HTTP {(int)status}），请稍后重试。" : "无法连接 GitHub，请检查网络后重试。";
        }
        catch (Exception ex) { Result.Text = $"检查更新失败，请稍后重试。{ex.Message}"; }
        finally { CheckButton.IsEnabled = true; }
    }
}
