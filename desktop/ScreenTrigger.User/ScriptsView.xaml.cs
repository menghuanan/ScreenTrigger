using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;
using Windows.Graphics;

namespace ScreenTrigger.User;

public sealed partial class ScriptsView : UserControl
{
    private readonly UserSession session;
    private readonly Func<(PointInt32 position, SizeInt32 size)> windowBounds;
    private List<ScriptRow> rows = [];
    private readonly Dictionary<string, string> attempts = [];
    private ScriptRow? editing;
    private string originalName = "", originalText = "", loadedSource = "";
    private bool updating;

    internal ScriptsView(UserSession session, Func<(PointInt32, SizeInt32)> bounds)
    {
        InitializeComponent();
        this.session = session;
        windowBounds = bounds;
    }

    internal async Task Reload()
    {
        var documents = await session.List("list_scripts");
        var previous = rows.ToDictionary(row => row.Id);
        updating = true;
        try
        {
            rows = documents.Select(doc => {
                var row = previous.GetValueOrDefault(doc.GetProperty("id").GetString()!) ?? new ScriptRow(doc);
                row.Update(doc);
                row.ShowEdit(EditMode.IsChecked == true);
                return row;
            }).ToList();
            ScriptList.ItemsSource = rows;
            EditMode.Visibility = rows.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
            if (rows.Count == 0) { EditMode.IsChecked = false; EditMode.Content = "编辑"; }
        }
        finally { updating = false; }
    }

    internal ScriptRow? FindKey(string key) => rows.Find(r => r.Key == key);

    private void ChangeMode(object sender, RoutedEventArgs e)
    {
        bool edit = EditMode.IsChecked == true;
        EditMode.Content = edit ? "完成" : "编辑";
        foreach (var row in rows) row.ShowEdit(edit);
    }

    private void OpenEditor(ScriptRow? row, string text)
    {
        editing = row;
        ScriptName.Text = originalName = row?.Name ?? "";
        Source.Text = loadedSource = text;
        originalText = Source.Text;
        EditorTitle.Text = row == null ? "新建脚本" : "编辑脚本";
        Library.Visibility = Visibility.Collapsed;
        Editor.Visibility = Visibility.Visible;
        ScriptName.Focus(FocusState.Programmatic);
    }

    private void NewScript(object sender, RoutedEventArgs e)
    {
        if (!session.Busy) OpenEditor(null, "");
    }

    private async void EditScript(object sender, RoutedEventArgs e)
    {
        if (((Button)sender).DataContext is not ScriptRow row) return;
        await session.Run(async () => {
            var loaded = await session.Call("load_script", new { script_id = row.Id });
            OpenEditor(new ScriptRow(loaded.GetProperty("script")), loaded.GetProperty("source").GetString()!);
        });
    }

    private void ShowLibrary()
    {
        editing = null;
        Editor.Visibility = Visibility.Collapsed;
        Library.Visibility = Visibility.Visible;
        DispatcherQueue.TryEnqueue(() => { if (IsLoaded && Library.Visibility == Visibility.Visible) NewButton.Focus(FocusState.Programmatic); });
    }

    private async Task SaveDraft()
    {
        if (string.IsNullOrWhiteSpace(ScriptName.Text))
        {
            ScriptName.Focus(FocusState.Programmatic);
            throw new InvalidOperationException("请填写脚本名称。");
        }
        ScriptName.IsReadOnly = Source.IsReadOnly = true;
        try
        {
            var draft = new Dictionary<string, object?> {
                ["name"] = ScriptName.Text.Trim(), ["source"] = Source.Text == originalText ? loadedSource : Source.Text.ReplaceLineEndings("\n"),
                ["parameters"] = editing == null ? Array.Empty<object>() : editing.Document.GetProperty("parameters")
            };
            if (editing != null) { draft["script_id"] = editing.Id; draft["expected_revision"] = editing.Revision; }
            var data = await session.Call("save_script", draft);
            editing = new ScriptRow(data.GetProperty("script"));
            originalText = Source.Text;
            originalName = ScriptName.Text;
            loadedSource = (string)draft["source"]!;
            session.Log($"已保存脚本「{editing.Name}」");
            await Reload();
        }
        finally { ScriptName.IsReadOnly = Source.IsReadOnly = false; }
    }

    private async void Save(object sender, RoutedEventArgs e) =>
        await session.Run(async () => { await SaveDraft(); ShowLibrary(); });

    internal async Task<bool> LeaveEditor()
    {
        if (Editor.Visibility != Visibility.Visible) return true;
        if (originalName != ScriptName.Text || originalText != Source.Text)
        {
            var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = "是否保存并退出？",
                Content = "脚本有尚未保存的修改。", PrimaryButtonText = "保存并退出", SecondaryButtonText = "放弃修改",
                CloseButtonText = "继续编辑", DefaultButton = ContentDialogButton.Primary };
            var choice = await dialog.ShowAsync();
            if (choice == ContentDialogResult.None) return false;
            if (choice == ContentDialogResult.Primary) await SaveDraft();
        }
        ShowLibrary();
        return true;
    }

    private async void Back(object sender, RoutedEventArgs e) => await session.Run(async () => { await LeaveEditor(); });

    private async void DeleteScript(object sender, RoutedEventArgs e)
    {
        if (((Button)sender).DataContext is not ScriptRow row) return;
        await session.Run(async () => {
            await session.Call("delete_script", new { script_id = row.Id, expected_revision = row.Revision });
            attempts.Remove(row.Id);
            session.Log($"已删除脚本「{row.Name}」");
            await Reload();
        });
    }

    private async void SetKey(object sender, SelectionChangedEventArgs e)
    {
        var combo = (ComboBox)sender;
        if (updating || !combo.IsLoaded || combo.DataContext is not ScriptRow row ||
            combo.SelectedItem is not string selected || selected == row.Key) return;
        if (session.Busy) { combo.SelectedItem = row.Key; return; }
        await session.Run(async () => {
            try
            {
                var result = await session.Call("set_script_hotkey", new { script_id = row.Id, expected_revision = row.Revision,
                    hotkey = selected == ScriptRow.Unbound ? null : selected });
                row.Update(result.GetProperty("script"));
            }
            finally { combo.SelectedItem = row.Key; }
        });
    }

    private async void ToggleScript(object sender, RoutedEventArgs e)
    {
        if (((Button)sender).DataContext is ScriptRow row) await session.Run(() => Toggle(row));
    }

    internal async Task Toggle(ScriptRow row)
    {
        if (row.Execution is string running)
        {
            await session.Call("stop_script", new { execution_id = running });
            return;
        }
        var displays = (await session.Call("get_display_list", new { })).GetProperty("displays");
        var (p, size) = windowBounds();
        long Overlap(JsonElement display)
        {
            var rect = display.GetProperty("rect");
            int x = rect.GetProperty("x").GetInt32(), y = rect.GetProperty("y").GetInt32();
            return (long)Math.Max(0, Math.Min(p.X + size.Width, x + rect.GetProperty("width").GetInt32()) - Math.Max(p.X, x)) *
                Math.Max(0, Math.Min(p.Y + size.Height, y + rect.GetProperty("height").GetInt32()) - Math.Max(p.Y, y));
        }
        if (displays.GetArrayLength() == 0) throw new InvalidOperationException("找不到可用的显示器。");
        var id = displays.EnumerateArray().MaxBy(Overlap).GetProperty("id").GetString();
        var cfg = await session.Host!.GetConfigAsync();
        if (!attempts.TryGetValue(row.Id, out var attempt)) attempts[row.Id] = attempt = Guid.NewGuid().ToString();
        JsonElement accepted;
        try
        {
            accepted = await session.Call("execute_script", new { script_id = row.Id, args = new { }, mode = "once",
                debug = false, display_id = id, allow_exit = false, frequency_hz = cfg.Recognition.DefaultFrequencyHz,
                total_timeout_ms = cfg.Execution.TotalTimeoutMs, client_run_id = attempt });
        }
        catch (HostException ex) when (ex.Error.Code != "INTERNAL_ERROR") { attempts.Remove(row.Id); throw; }
        attempts.Remove(row.Id);
        row.Execution = accepted.GetProperty("execution_id").GetString();
        row.RunStatus = "starting";
        row.Refresh();
        session.Log($"已启动脚本「{row.Name}」");
        // A short script can finish before its start acknowledgement reaches the window.
        var state = await session.Call("get_script_state", new { execution_id = row.Execution });
        ApplyState(state.GetProperty("state"));
    }

    internal void ApplyState(JsonElement state)
    {
        var id = state.GetProperty("execution_id").GetString();
        if (id == null || rows.Find(r => r.Execution == id) is not { } row) return;
        var status = state.GetProperty("status").GetString();
        row.RunStatus = status;
        if (status is "completed" or "cancelled" or "failed")
        {
            row.Execution = null;
            session.Log($"「{row.Name}」{ScriptRow.StatusText(status)}");
        }
        row.Refresh();
    }

    internal void Recover(RecoverySnapshot snapshot)
    {
        foreach (var state in snapshot.Executions.Values) ApplyState(state);
    }
}
