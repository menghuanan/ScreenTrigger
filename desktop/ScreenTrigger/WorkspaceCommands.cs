using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;

namespace ScreenTrigger;

internal sealed record CatalogItem(string? Id, string Label, JsonElement Document = default)
{
    public override string ToString() => Label;
    public long Revision => Document.GetProperty("revision").GetInt64();
}

internal sealed class WorkspaceCommands(HostClient host, FrameworkElement controls, TextBlock feedback)
{
    private readonly string? session = host.Events.Session;

    public async Task<JsonElement> Call(string command, object parameters)
    {
        if (session == null || session != host.Events.Session) throw new InvalidOperationException("引擎会话已改变，请重新打开页面。");
        var result = await host.RequestAsync(command, parameters);
        if (session != host.Events.Session) throw new InvalidOperationException("本次操作所属的会话已结束。");
        return result;
    }

    public async Task Run(Func<Task> action)
    {
        if (controls is not Control panel || !panel.IsEnabled) return;
        panel.IsEnabled = false;
        try { feedback.Text = ""; await action(); }
        catch (Exception ex) { feedback.Text = ex.Message; }
        finally { panel.IsEnabled = true; }
    }

    public async Task<bool> Confirm(string title, string message, string accept)
    {
        var dialog = new ContentDialog { XamlRoot = controls.XamlRoot, Title = title, Content = message,
            PrimaryButtonText = accept, CloseButtonText = "取消", DefaultButton = ContentDialogButton.Close };
        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }
}
