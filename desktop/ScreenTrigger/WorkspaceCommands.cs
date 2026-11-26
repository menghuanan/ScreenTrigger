using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using ScreenTrigger.Host;

namespace ScreenTrigger;

internal interface IWorkspaceDraft
{
    bool HasChanges { get; }
    Task WhenIdle { get; }
    Task<bool> ResolveChanges();
}

internal sealed record CatalogItem(string? Id, string Label, JsonElement Document = default)
{
    public override string ToString() => Label;
    public long Revision => Document.GetProperty("revision").GetInt64();
}

internal sealed class WorkspaceCommands(HostClient host, FrameworkElement controls, TextBlock feedback)
{
    private readonly string? session = host.Events.Session;
    private bool dialogShown;
    private TaskCompletionSource? idle;
    public Task WhenIdle => idle?.Task ?? Task.CompletedTask;

    public async Task<JsonElement> Call(string command, object parameters)
    {
        if (session == null || session != host.Events.Session) throw new InvalidOperationException("引擎会话已改变，请重新打开页面。");
        var request = host.BeginRequest(command, parameters);
        JsonElement result;
        try { result = await request.Completion; }
        catch (HostException ex) when (ex.Error.Code == "INTERNAL_ERROR" && ex.Error.Details.ValueKind == JsonValueKind.Object &&
                                      ex.Error.Details.TryGetProperty("deadline_ms", out _))
        {
            if (session != host.Events.Session) throw;
            var status = await host.QueryAsync(request);
            if (status.GetProperty("status").GetString() != "completed")
                throw new InvalidOperationException($"请求 {request.Id} 的结果待核实。请在历史页查询状态。");
            result = status.GetProperty("response");
            if (result.ValueKind == JsonValueKind.Object && result.TryGetProperty("code", out var code))
                throw new InvalidOperationException($"{code} · {result.GetProperty("message")}");
        }
        if (session != host.Events.Session) throw new InvalidOperationException("本次操作所属的会话已结束。");
        return result;
    }

    public async Task Run(Func<Task> action)
    {
        if (controls is not Control panel || !panel.IsEnabled) return;
        var origin = panel.XamlRoot is { } root ? FocusManager.GetFocusedElement(root) as Control : null;
        var focusState = origin?.FocusState ?? FocusState.Unfocused;
        var completed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        idle = completed;
        dialogShown = false;
        panel.IsEnabled = false;
        try { feedback.Text = ""; await action(); }
        catch (HostException ex)
        {
            var detail = ex.Error.Details;
            feedback.Text = ex.Message + (detail.ValueKind == JsonValueKind.Object && detail.EnumerateObject().Any() ? $" · {detail}" : "");
        }
        catch (Exception ex) { feedback.Text = ex.Message; }
        finally
        {
            panel.IsEnabled = true;
            idle = null;
            completed.TrySetResult();
            if (dialogShown && panel.IsLoaded && origin is { IsLoaded: true, IsEnabled: true } &&
                origin.XamlRoot == panel.XamlRoot && focusState != FocusState.Unfocused)
                origin.Focus(focusState);
        }
    }

    public async Task<bool> Confirm(string title, string message, string accept)
    {
        var dialog = new ContentDialog { XamlRoot = controls.XamlRoot, Title = title, Content = message,
            PrimaryButtonText = accept, CloseButtonText = "取消", DefaultButton = ContentDialogButton.Close };
        dialogShown = true;
        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }

    public async Task<bool> ResolveDraft(string name, Func<Task> save, Action discard)
    {
        var dialog = new ContentDialog { XamlRoot = controls.XamlRoot, Title = $"保存{name}的修改？",
            PrimaryButtonText = "保存", SecondaryButtonText = "放弃", CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close };
        dialogShown = true;
        switch (await dialog.ShowAsync())
        {
            case ContentDialogResult.Primary: await save(); return true;
            case ContentDialogResult.Secondary: discard(); return true;
            default: return false;
        }
    }

    public async Task<bool> Guard(Func<Task<bool>> resolve)
    {
        bool proceed = false;
        await Run(async () => { proceed = await resolve(); });
        return proceed;
    }
}
