using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace ScreenTrigger.User;

public sealed partial class LogsView : UserControl
{
    private sealed record Run(string Id, string Label, bool Active) { public override string ToString() => Label; }
    private const int PageSize = 50;
    private readonly UserSession session;
    private readonly Func<string, string?> scriptName;
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly Stack<int> previousOffsets = new();
    private bool watching, historyDirty, pageDirty, reading, filling, preferActive, followLatest;
    private int offset, nextOffset, version;

    internal LogsView(UserSession session, Func<string, string?> scriptName)
    {
        this.session = session;
        this.scriptName = scriptName;
        InitializeComponent();
        AppLog.ItemsSource = session.Messages;
        Loaded += (_, _) => StartWatching();
        Unloaded += (_, _) => StopWatching();
        timer.Tick += (_, _) => {
            if (!reading && !Runs.IsDropDownOpen) RequestPage();
        };
    }

    private void CategoryChanged(object sender, SelectionChangedEventArgs e)
    {
        if (History == null) return;
        bool history = Category.SelectedIndex == 0;
        History.Visibility = history ? Visibility.Visible : Visibility.Collapsed;
        Runs.Visibility = History.Visibility;
        AppLog.Visibility = history ? Visibility.Collapsed : Visibility.Visible;
        if (history && IsLoaded) StartWatching(); else StopWatching();
    }

    private void StartWatching()
    {
        if (Category.SelectedIndex != 0) return;
        watching = preferActive = true;
        HistoryChanged();
        RequestPage();
    }

    internal void StopWatching()
    {
        watching = false;
        timer.Stop();
        version++;
    }

    internal void HistoryChanged()
    {
        historyDirty = true;
        if (watching && session.Ready) timer.Start();
    }

    private Run Describe(JsonElement row)
    {
        var id = row.GetProperty("execution_id").GetString()!;
        var status = row.GetProperty("status").GetString();
        var started = row.GetProperty("started_at").GetDateTimeOffset().ToLocalTime();
        var name = "监控";
        if (row.GetProperty("kind").GetString() != "monitor")
        {
            var scriptId = row.GetProperty("script_id").GetString();
            if (row.TryGetProperty("script_name", out var saved) && saved.GetString() is { Length: > 0 } title)
                name = title;
            else
                name = scriptId == null ? "未知脚本" : scriptName(scriptId) ?? $"已删除的脚本（{scriptId}）";
        }
        return new(id, $"{started:MM-dd HH:mm:ss}　{name} · {ScriptRow.StatusText(status)}",
            status is "starting" or "running" or "paused" or "debug_paused" or "stopping");
    }

    private void ApplyHistory(List<JsonElement> history)
    {
        var runs = history.Select(Describe).ToList();
        var previous = Runs.SelectedItem as Run;
        var selected = (preferActive ? runs.Find(r => r.Active) : null)
            ?? runs.Find(r => r.Id == previous?.Id) ?? runs.FirstOrDefault();
        preferActive = false;
        filling = true;
        try
        {
            Runs.ItemsSource = runs;
            Runs.SelectedItem = selected;
        }
        finally { filling = false; }
        if (selected?.Id != previous?.Id) ResetPage(selected);
    }

    private void ResetPage(Run? run)
    {
        offset = 0;
        previousOffsets.Clear();
        followLatest = run?.Active == true;
        Entries.Text = "";
        Previous.IsEnabled = Next.IsEnabled = false;
        LogScroll.ChangeView(null, 0, null, true);
    }

    private void RequestPage()
    {
        version++;
        pageDirty = true;
        if (!reading && watching && session.Ready) _ = Update();
    }

    private async Task Update()
    {
        reading = true;
        try
        {
            // Coalesce UI and timer requests. An old read must never replace a newly selected page.
            while (watching && pageDirty)
            {
                pageDirty = false;
                if (historyDirty)
                {
                    historyDirty = false;
                    var history = await session.List("list_execution_history");
                    if (!watching) return;
                    ApplyHistory(history);
                }
                if (Runs.SelectedItem is Run run) await Read(run, version);
            }
            if (!historyDirty && Runs.SelectedItem is not Run { Active: true }) timer.Stop();
        }
        catch (Exception error)
        {
            timer.Stop();
            if (watching) session.Error(error);
        }
        finally { reading = false; }
    }

    private async Task Read(Run run, int ticket)
    {
        Task<JsonElement> Page(int start) => session.Call("get_execution_logs", new { execution_id = run.Id, offset = start, limit = PageSize });
        var page = await Page(offset);
        if (ticket != version) return;
        var total = page.GetProperty("total").GetInt32();
        int last = Math.Max(0, total - 1) / PageSize * PageSize;
        if ((followLatest || offset > last) && offset != last)
        {
            offset = last;
            previousOffsets.Clear();
            page = await Page(offset);
            if (ticket != version) return;
        }
        var items = page.GetProperty("items");
        var text = string.Join("\n\n", items.EnumerateArray().Select(v =>
            $"{v.GetProperty("timestamp").GetDateTimeOffset().ToLocalTime():HH:mm:ss}   {v.GetProperty("level")}   {v.GetProperty("message")}"));
        if (Entries.Text != text)
        {
            bool atEnd = LogScroll.ScrollableHeight - LogScroll.VerticalOffset < 2;
            Entries.Text = text;
            if (followLatest && atEnd)
            {
                LogScroll.UpdateLayout();
                LogScroll.ChangeView(null, LogScroll.ScrollableHeight, null, true);
            }
        }
        nextOffset = offset + items.GetArrayLength();
        Previous.IsEnabled = offset > 0;
        Next.IsEnabled = nextOffset > offset && nextOffset < page.GetProperty("total").GetInt32();
    }

    private void RunChanged(object sender, SelectionChangedEventArgs e)
    {
        if (filling || !watching) return;
        var run = Runs.SelectedItem as Run;
        ResetPage(run);
        if (run?.Active == true) timer.Start();
        RequestPage();
    }

    private void Refresh(object sender, RoutedEventArgs e) { HistoryChanged(); RequestPage(); }
    private void Prev(object sender, RoutedEventArgs e)
    {
        followLatest = false;
        offset = previousOffsets.TryPop(out var previous) ? previous : Math.Max(0, offset - PageSize);
        RequestPage();
    }
    private void More(object sender, RoutedEventArgs e)
    {
        if (nextOffset <= offset) return;
        previousOffsets.Push(offset);
        offset = nextOffset;
        RequestPage();
    }
}
