using System.Collections.ObjectModel;
using System.Text.Json;
using ScreenTrigger.Host;

namespace ScreenTrigger.User;

internal sealed class UserSession
{
    private TaskCompletionSource? operation;
    internal Task WhenIdle => operation?.Task ?? Task.CompletedTask;
    internal bool Busy => operation != null;
    internal HostClient? Host { get; set; }
    internal bool Ready { get; set; }
    internal event Action<string>? Failed;
    internal event Action<bool>? BusyChanged;
    internal ObservableCollection<string> Messages { get; } = [];

    internal void Log(string message)
    {
        Messages.Insert(0, $"{DateTime.Now:HH:mm:ss}   {message}");
        if (Messages.Count > 200) Messages.RemoveAt(Messages.Count - 1);
    }

    internal void Error(Exception e)
    {
        Log(e.Message);
        Failed?.Invoke(e.Message);
    }

    internal async Task Run(Func<Task> action)
    {
        if (Busy) return;
        var pending = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        operation = pending;
        BusyChanged?.Invoke(true);
        try { await action(); }
        catch (Exception e) { Error(e); }
        finally { operation = null; BusyChanged?.Invoke(false); pending.SetResult(); }
    }

    internal Task<JsonElement> Call(string command, object args)
    {
        var host = Host ?? throw new InvalidOperationException("引擎尚未就绪。");
        var session = host.Events.Session ?? throw new InvalidOperationException("引擎会话已结束。");
        return host.RequestCheckedAsync(session, command, args);
    }

    internal async Task<List<JsonElement>> List(string command)
    {
        List<JsonElement> items = [];
        while (true)
        {
            var page = await Call(command, new { offset = items.Count, limit = 100 });
            var batch = page.GetProperty("items");
            items.AddRange(batch.EnumerateArray());
            if (batch.GetArrayLength() == 0 || items.Count >= page.GetProperty("total").GetInt32()) return items;
        }
    }
}
