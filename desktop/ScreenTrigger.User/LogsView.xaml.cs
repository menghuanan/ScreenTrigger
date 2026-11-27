using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace ScreenTrigger.User;

public sealed partial class LogsView : UserControl
{
    private sealed record Run(string Id, string Label) { public override string ToString() => Label; }
    private readonly UserSession session;
    private int offset, request;
    private bool filling;
    internal LogsView(UserSession session)
    {
        InitializeComponent();
        this.session = session;
        AppLog.ItemsSource = session.Messages;
    }

    private void CategoryChanged(object sender, SelectionChangedEventArgs e)
    {
        if (History == null) return;
        bool history = Category.SelectedIndex == 0;
        History.Visibility = history ? Visibility.Visible : Visibility.Collapsed;
        AppLog.Visibility = history ? Visibility.Collapsed : Visibility.Visible;
    }

    internal async Task Reload()
    {
        if (!session.Ready) return;
        var selected = (Runs.SelectedItem as Run)?.Id;
        var all = (await session.List("list_execution_history")).Select(r => new Run(r.GetProperty("execution_id").GetString()!,
            $"{r.GetProperty("started_at").GetDateTimeOffset().ToLocalTime():MM-dd HH:mm:ss}　{(r.GetProperty("kind").GetString() == "monitor" ? "监控" : "脚本")} · {ScriptRow.StatusText(r.GetProperty("status").GetString())}")).ToList();
        filling = true;
        Runs.ItemsSource = all;
        Runs.SelectedItem = all.Find(r => r.Id == selected) ?? all.FirstOrDefault();
        filling = false;
        offset = 0;
        await Read();
    }

    private async Task Read()
    {
        int generation = ++request;
        Entries.Text = "";
        Previous.IsEnabled = Next.IsEnabled = false;
        if (Runs.SelectedItem is not Run run) return;
        var page = await session.Call("get_execution_logs", new { execution_id = run.Id, offset, limit = 50 });
        if (generation != request) return;
        Entries.Text = string.Join("\n\n", page.GetProperty("items").EnumerateArray().Select(v =>
            $"{v.GetProperty("timestamp").GetDateTimeOffset().ToLocalTime():HH:mm:ss}   {v.GetProperty("level")}   {v.GetProperty("message")}"));
        Previous.IsEnabled = offset > 0;
        Next.IsEnabled = offset + 50 < page.GetProperty("total").GetInt32();
    }

    private async void RunChanged(object sender, SelectionChangedEventArgs e)
    {
        if (filling || session == null) return;
        offset = 0;
        try { await Read(); } catch (Exception error) { session.Error(error); }
    }
    private async void Refresh(object sender, RoutedEventArgs e) => await session.Run(Reload);
    private async void Prev(object sender, RoutedEventArgs e) => await session.Run(async () => { offset = Math.Max(0, offset - 50); await Read(); });
    private async void More(object sender, RoutedEventArgs e) => await session.Run(async () => { offset += 50; await Read(); });
}
