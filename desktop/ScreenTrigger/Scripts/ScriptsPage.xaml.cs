using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;

namespace ScreenTrigger.Scripts;

public sealed partial class ScriptsPage : UserControl, IWorkspaceDraft
{
    private readonly WorkspaceCommands commands;
    private readonly HostClient host;
    private Debug.DebugPanel? debugger;
    private CatalogItem? edited;
    private int offset;
    private string original = "";
    private string savedSource = "";

    internal ScriptsPage(HostClient host)
    {
        InitializeComponent(); this.host = host; commands = new(host, this, Feedback);
        original = Snapshot();
        Loaded += Initialize;
    }
    private async void Initialize(object sender, RoutedEventArgs e)
    { Loaded -= Initialize; await commands.Run(List); }
    private async Task List()
    {
        var data = await commands.Call("list_scripts", new { offset, limit = 50 });
        int total = data.GetProperty("total").GetInt32();
        if (offset > 0 && offset >= total) { offset = Math.Max(0, (total - 1) / 50 * 50); await List(); return; }
        Scripts.ItemsSource = data.GetProperty("items").EnumerateArray().Select(Item).ToList();
        Previous.IsEnabled = offset > 0; Next.IsEnabled = offset + 50 < total;
    }
    private static CatalogItem Item(JsonElement script) =>
        new(script.GetProperty("id").GetString(), script.GetProperty("name").GetString()!, script);
    private string Snapshot() => JsonSerializer.Serialize(new[] { ScriptName.Text, Source.Text, Parameters.Text });
    bool IWorkspaceDraft.HasChanges => original != Snapshot();
    Task IWorkspaceDraft.WhenIdle => commands.WhenIdle;
    Task<bool> IWorkspaceDraft.ResolveChanges() => commands.Guard(CanReplace);
    private Task<bool> CanReplace() => original == Snapshot() ? Task.FromResult(true) :
        commands.ResolveDraft("脚本", SaveDraft, () => Edit(edited, savedSource));
    private void Edit(CatalogItem? item, string source)
    {
        if (edited?.Id != item?.Id || edited?.Revision != item?.Revision) { DebugTools.Content = null; debugger = null; }
        edited = item;
        ScriptName.Text = item?.Label ?? "";
        Source.Text = source;
        savedSource = source;
        Parameters.Text = item?.Document.GetProperty("parameters").GetRawText() ?? "[]";
        original = Snapshot();
    }

    private Debug.DebugPanel ShowDebugger()
    {
        debugger ??= new Debug.DebugPanel(host, () => original == Snapshot() ? edited : null, _ => Source.Focus(FocusState.Programmatic));
        DebugTools.Content = debugger;
        debugger.StartBringIntoView();
        return debugger;
    }
    private void OpenDebug(object sender, RoutedEventArgs e) => ShowDebugger();

    internal async Task Inspect(string execution) => await ShowDebugger().Attach(execution);

    private async Task SaveDraft()
    {
        var fields = new Dictionary<string, object?> { ["name"] = ScriptName.Text, ["source"] = Source.Text,
            ["parameters"] = JsonSerializer.Deserialize<JsonElement>(Parameters.Text) };
        if (edited != null) { fields["script_id"] = edited.Id; fields["expected_revision"] = edited.Revision; }
        var saved = await commands.Call("save_script", fields);
        Edit(Item(saved.GetProperty("script")), Source.Text);
        await List();
        Feedback.Text = $"已保存「{edited!.Label}」，修订 {edited.Revision}。";
    }

    private async void Action(object sender, RoutedEventArgs e)
    {
        int? diagnosticLine = null;
        await commands.Run(async () =>
        {
            switch (((Button)sender).Tag as string)
            {
                case "previous": offset = Math.Max(0, offset - 50); await List(); break;
                case "next": offset += 50; await List(); break;
                case "new": if (await CanReplace()) Edit(null, ""); break;
                case "open":
                    if (Scripts.SelectedItem is CatalogItem selected && await CanReplace())
                    {
                        var data = await commands.Call("load_script", new { script_id = selected.Id });
                        Edit(Item(data.GetProperty("script")), data.GetProperty("source").GetString()!);
                    }
                    break;
                case "validate":
                    var result = await commands.Call("validate_script", new { source = Source.Text });
                    var diagnostics = result.GetProperty("diagnostics").EnumerateArray().ToList();
                    Feedback.Text = diagnostics.Count == 0 ? "语法检查通过。" : string.Join("\n", diagnostics.Select(d =>
                        $"第 {d.GetProperty("line")} 行{(d.TryGetProperty("column", out var c) && c.ValueKind == JsonValueKind.Number ? $"，第 {c} 列" : "")} · {d.GetProperty("message")}"));
                    if (diagnostics.Count > 0) diagnosticLine = diagnostics[0].GetProperty("line").GetInt32();
                    break;
                case "save":
                    await SaveDraft();
                    break;
                case "delete":
                    if (edited != null && await commands.Confirm("删除脚本？", edited.Label, "删除"))
                    { await commands.Call("delete_script", new { script_id = edited.Id, expected_revision = edited.Revision }); Edit(null, ""); await List(); }
                    break;
            }
        });
        if (diagnosticLine is int line)
        {
            var lines = System.Text.RegularExpressions.Regex.Matches(Source.Text, @"[^\r\n]*(?:\r\n|\r|\n|$)");
            if (line > 0 && line <= lines.Count)
            { var row = lines[line - 1]; Source.Focus(FocusState.Programmatic); Source.Select(row.Index, row.Value.TrimEnd('\r','\n').Length); }
        }
    }
}
