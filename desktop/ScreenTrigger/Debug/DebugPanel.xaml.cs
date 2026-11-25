using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;

namespace ScreenTrigger.Debug;

public sealed partial class DebugPanel : UserControl
{
    private readonly HostClient host;
    private readonly WorkspaceCommands commands;
    private readonly Func<CatalogItem?> saved;
    private readonly Action<int?> focus;
    private IDisposable? subscription;
    private string? run, clientId, signature;
    private int revision, variablePage;
    private JsonElement variableData;
    private bool updatingFrames;

    internal DebugPanel(HostClient client, Func<CatalogItem?> savedScript, Action<int?> focusEditor)
    {
        InitializeComponent();
        host = client; saved = savedScript; focus = focusEditor;
        Arguments.Text = "{}";
        commands = new(client, this, Feedback);
        Loaded += LoadPanel;
        Unloaded += (_, _) => { subscription?.Dispose(); subscription = null; revision++; };
    }

    private async void LoadPanel(object sender, RoutedEventArgs args)
    {
        subscription ??= host.Events.Subscribe(message =>
        {
            var p = message.Payload;
            var owner = message.Name == "log_message" ? p.GetProperty("log") : p;
            if (!owner.TryGetProperty("execution_id", out var id) || id.GetString() != run) return;
            if (message.Name == "script_state_changed") _ = ReadState();
            else if (message.Name == "log_message" && PausedTools.Visibility == Visibility.Visible)
                PausedLogs.Text = (PausedLogs.Text + "\n" + p.GetProperty("log").GetProperty("message").GetString()).TrimStart();
        });
        try { if (saved() != null) await ListPoints(); if (run != null) await ReadState(); }
        catch (Exception ex) { Feedback.Text = ex.Message; }
    }

    internal async Task Attach(string execution)
    {
        run = execution;
        await ReadState();
    }

    private CatalogItem Saved() => saved() ?? throw new InvalidOperationException("请先保存编辑器中的源码，再设置断点或启动调试。");
    private async Task ListPoints()
    {
        var data = await commands.Call("list_breakpoints", new { script_id = Saved().Id });
        Points.ItemsSource = data.GetProperty("items").EnumerateArray().Select(p => new CatalogItem(
            p.GetProperty("id").GetString(), $"第 {p.GetProperty("line")} 行" + (p.GetProperty("enabled").GetBoolean() ? "" : " · 源码已改变"), p)).ToList();
    }
    private async Task ReadState()
    {
        var id = run;
        if (id == null) return;
        var ticket = ++revision;
        try
        {
            var state = (await commands.Call("get_script_state", new { execution_id = id })).GetProperty("state");
            if (ticket != revision || id != run) return;
            Feedback.Text = Status.RunFeedback.State(state);
            var status = state.GetProperty("status").GetString();
            Start.IsEnabled = status is "completed" or "cancelled" or "failed";
            PausedTools.Visibility = status == "debug_paused" ? Visibility.Visible : Visibility.Collapsed;
            if (status != "debug_paused") { Variables.Text = PausedLogs.Text = ""; return; }
            var stack = (await commands.Call("get_stack", new { execution_id = id })).GetProperty("frames");
            if (ticket != revision) return;
            var frames = stack.EnumerateArray().Select(f => new CatalogItem(f.GetProperty("frame_id").GetString(),
                $"{f.GetProperty("script_id")} · 第 {f.GetProperty("line")} 行", f)).ToList();
            updatingFrames = true;
            Frames.ItemsSource = frames; Frames.SelectedIndex = 0; updatingFrames = false;
            await ReadVariables();
            var logs = await commands.Call("get_execution_logs", new { execution_id = id, limit = 100 });
            if (ticket == revision) PausedLogs.Text = string.Join("\n", logs.GetProperty("items").EnumerateArray().Select(l => l.GetProperty("message").GetString()));
        }
        catch (Exception ex) { if (ticket == revision) Feedback.Text = ex.Message; }
    }

    private async Task ReadVariables()
    {
        if (Frames.SelectedItem is not CatalogItem frame || run == null) return;
        var ticket = revision;
        var data = await commands.Call("get_variables", new { execution_id = run, frame_id = frame.Id });
        if (ticket != revision || !Equals(Frames.SelectedItem, frame)) return;
        variableData = data; variablePage = 0; ShowVariables();
        var source = await commands.Call("load_script", new {script_id=frame.Document.GetProperty("script_id").GetString()});
        if (ticket != revision || !Equals(Frames.SelectedItem,frame)) return;
        var script = source.GetProperty("script");
        if (script.GetProperty("source_sha256").GetString() != frame.Document.GetProperty("source_hash").GetString())
        { CodeLines.Text = "已保存的源码已改变，当前栈使用启动时的版本。"; return; }
        int line = frame.Document.GetProperty("line").GetInt32();
        string[] lines = source.GetProperty("source").GetString()!.ReplaceLineEndings("\n").Split('\n');
        CodeLines.Text = script.GetProperty("name").GetString() + "\n" + string.Join("\n", lines
            .Select((value,index) => (value, number:index+1)).Where(row => Math.Abs(row.number-line)<=2)
            .Select(row => $"{(row.number==line ? "▶" : " ")} {row.number,4}  {row.value}"));
    }
    private async void SelectFrame(object sender, SelectionChangedEventArgs args)
    {
        if (updatingFrames || PausedTools.Visibility != Visibility.Visible) return;
        try { await ReadVariables(); } catch (Exception ex) { Feedback.Text = ex.Message; }
    }
    private void Page(object sender, RoutedEventArgs args)
    {
        variablePage += int.Parse((string)((Button)sender).Tag);
        ShowVariables();
    }
    private void ShowVariables()
    {
        var items = variableData.GetProperty("variables");
        Variables.Text = string.Join("\n", items.EnumerateArray().Skip(variablePage * 10).Take(10).Select(v =>
            $"{v.GetProperty("name")}  ({v.GetProperty("type")})  {v.GetProperty("value")}")) +
            (variableData.GetProperty("truncated").GetBoolean() ? "\n已达快照预算，部分变量已截断。" : "");
        Previous.IsEnabled = variablePage > 0; Next.IsEnabled = (variablePage + 1) * 10 < items.GetArrayLength();
    }

    private async void Action(object sender, RoutedEventArgs args)
    {
        bool restore = false;
        await commands.Run(async () =>
        {
            var action = ((Button)sender).Tag as string;
            switch (action)
            {
                case "add":
                    var script = Saved();
                    await commands.Call("add_breakpoint", new { script_id = script.Id, source_hash = script.Document.GetProperty("source_sha256").GetString(), line = Line.Value });
                    await ListPoints(); break;
                case "remove":
                    if (Points.SelectedItem is CatalogItem point)
                    { await commands.Call("remove_breakpoint", new { breakpoint_id = point.Id }); await ListPoints(); }
                    break;
                case "start":
                    var selected = Saved();
                    var binding = (await commands.Call("get_bound_window", new { })).GetProperty("binding");
                    string? display = null;
                    if (binding.ValueKind != JsonValueKind.Object)
                    {
                        var displays = (await commands.Call("get_display_list", new { })).GetProperty("displays").EnumerateArray().ToList();
                        display = displays.First(d => d.GetProperty("primary").GetBoolean()).GetProperty("id").GetString();
                    }
                    var config = new Dictionary<string,object?> { ["script_id"]=selected.Id, ["debug"]=true,
                        ["args"]=JsonSerializer.Deserialize<JsonElement>(Arguments.Text), ["display_id"]=display,
                        ["binding_id"]=binding.ValueKind == JsonValueKind.Object ? binding.GetProperty("binding_id").GetString() : null };
                    var next = JsonSerializer.Serialize(config);
                    if (clientId == null || next != signature) clientId = Guid.NewGuid().ToString();
                    signature = next; config["client_run_id"] = clientId;
                    var reply = await commands.Call("execute_script", config);
                    clientId = null;
                    await Attach(reply.GetProperty("execution_id").GetString()!);
                    break;
                default:
                    if (run != null && action != null)
                    {
                        await commands.Call(action, new { execution_id = run });
                        restore = true; await ReadState();
                    }
                    break;
            }
        });
        if (restore) focus(null);
    }
}
