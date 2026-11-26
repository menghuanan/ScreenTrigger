using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Contracts;
using ScreenTrigger.Host;

namespace ScreenTrigger.Targets;

public sealed partial class TargetsPage : UserControl, IWorkspaceDraft
{
    private readonly WorkspaceCommands commands;
    private readonly Func<ImageRef?> image;
    private readonly Action<object> test;
    private const string InitialConditions = """{"op":"all","children":[{"id":"c1","type":"template","params":{"threshold":0.85}}]}""";
    private CatalogItem? edited;
    private int offset;
    private string originalDraft = "";
    private CatalogItem? editedGroup;
    private bool populatingGroups;

    internal TargetsPage(HostClient host, Func<ImageRef?> image, Action<object> test)
    {
        this.image = image; this.test = test;
        InitializeComponent();
        commands = new(host, this, Feedback);
        Conditions.Text = InitialConditions;
        originalDraft = EditorSnapshot();
        Loaded += Initialize;
    }

    private async void Initialize(object sender, RoutedEventArgs args)
    {
        Loaded -= Initialize;
        await commands.Run(async () => { await LoadGroups(); await List(); originalDraft = EditorSnapshot(); });
    }

    private async Task LoadGroups()
    {
        var data = await commands.Call("list_groups", new { });
        var groups = data.GetProperty("groups").EnumerateArray().Select(g => new CatalogItem(
            g.GetProperty("id").GetString(), new string('　', g.GetProperty("depth").GetInt32()) +
            g.GetProperty("name").GetString(), g)).ToList();
        Fill(Filter, [new("*", "全部分组"), new(null, "未分组"), .. groups]);
        Fill(TargetGroup, [new(null, "未分组"), .. groups]);
        Fill(ParentGroup, [new(null, "顶层"), .. groups]);
        bool wasPopulating = populatingGroups;
        populatingGroups = true;
        try { Fill(Groups, groups); EditGroup(Groups.SelectedItem as CatalogItem); }
        finally { populatingGroups = wasPopulating; }
    }

    private static void Fill(ComboBox box, List<CatalogItem> rows)
    {
        var id = (box.SelectedItem as CatalogItem)?.Id;
        box.ItemsSource = rows;
        box.SelectedItem = (id == null ? null : rows.Find(item => item.Id == id)) ?? rows.FirstOrDefault();
    }
    private static string? Id(ComboBox box) => (box.SelectedItem as CatalogItem)?.Id;

    private async Task List()
    {
        var request = new Dictionary<string, object?> { ["offset"] = offset, ["limit"] = 50, ["query"] = Query.Text };
        if (Id(Filter) != "*") request["group_id"] = Id(Filter);
        var data = await commands.Call("list_targets", request);
        int total = data.GetProperty("total").GetInt32();
        if (offset > 0 && offset >= total) { offset = Math.Max(0, ((total - 1) / 50) * 50); await List(); return; }
        Targets.ItemsSource = data.GetProperty("items").EnumerateArray().Select(t =>
            new CatalogItem(t.GetProperty("id").GetString(), $"#{t.GetProperty("sample_no")}　{t.GetProperty("name")}", t)).ToList();
        Previous.IsEnabled = offset > 0; Next.IsEnabled = offset + 50 < total;
        PageNumber.Text = total == 0 ? "0 个目标" : $"{offset + 1}–{Math.Min(total, offset + 50)} / {total}";
    }
    private string EditorSnapshot() => JsonSerializer.Serialize(new { TargetName.Text, group = Id(TargetGroup),
        anchor = Anchor.Text, conditions = Conditions.Text, storage = Storage.SelectedIndex, replace = ReplaceImage.IsChecked });
    private bool TargetChanged => originalDraft != EditorSnapshot();
    private bool GroupChanged => GroupName.Text != (editedGroup?.Document.GetProperty("name").GetString() ?? "") ||
        Id(ParentGroup) != editedGroup?.Document.GetProperty("parent_group_id").GetString();
    bool IWorkspaceDraft.HasChanges => TargetChanged || GroupChanged;
    Task IWorkspaceDraft.WhenIdle => commands.WhenIdle;
    Task<bool> IWorkspaceDraft.ResolveChanges() => commands.Guard(async () =>
        await CanReplaceDraft() && await CanReplaceGroup());
    private Task<bool> CanReplaceDraft() => TargetChanged ?
        commands.ResolveDraft("目标", SaveDraft, () => Edit(edited?.Document)) : Task.FromResult(true);
    private Task<bool> CanReplaceGroup() => GroupChanged ?
        commands.ResolveDraft("分组", SaveGroup, () => EditGroup(editedGroup)) : Task.FromResult(true);

    private void Edit(JsonElement? target)
    {
        edited = target is { } t ? new(t.GetProperty("id").GetString(), t.GetProperty("name").GetString()!, t) : null;
        TargetName.Text = edited?.Label ?? "";
        Anchor.Text = target?.GetProperty("anchor_condition_id").GetString() ?? "c1";
        Conditions.Text = target?.GetProperty("conditions").GetRawText() ?? InitialConditions;
        string? group = target?.GetProperty("group_id").GetString();
        TargetGroup.SelectedItem = TargetGroup.Items.Cast<CatalogItem>().FirstOrDefault(g => g.Id == group);
        ReplaceImage.IsChecked = false; ReplaceImage.IsEnabled = edited != null;
        Storage.SelectedIndex = target?.GetProperty("image").GetProperty("storage_mode").GetString() == "png_lossless" ? 1 : 0;
        originalDraft = EditorSnapshot();
    }
    private Dictionary<string, object?> Fields(bool useImage)
    {
        var fields = new Dictionary<string, object?> { ["name"] = TargetName.Text, ["group_id"] = Id(TargetGroup),
            ["anchor_condition_id"] = Anchor.Text, ["conditions"] = JsonSerializer.Deserialize<JsonElement>(Conditions.Text) };
        if (useImage)
        {
            fields["image_id"] = image()?.ImageId ?? throw new InvalidOperationException("请先框选屏幕或导入图片。");
            fields["storage_mode"] = Storage.SelectedIndex == 1 ? "png_lossless" : "original";
        }
        return fields;
    }

    private async Task SaveDraft()
    {
        var saved = edited == null ? await commands.Call("create_target", new { draft = Fields(true) }) :
            await commands.Call("update_target", new { target_id = edited.Id, expected_revision = edited.Revision,
                patch = Fields(ReplaceImage.IsChecked == true) });
        Edit(saved.GetProperty("target"));
        await List();
        Feedback.Text = $"已保存目标 #{edited!.Document.GetProperty("sample_no")}";
    }

    private async Task SaveGroup()
    {
        if (editedGroup == null) await commands.Call("create_group", new { name = GroupName.Text, parent_group_id = Id(ParentGroup) });
        else await commands.Call("update_group", new { group_id = editedGroup.Id, expected_revision = editedGroup.Revision,
            name = GroupName.Text, parent_group_id = Id(ParentGroup) });
        await LoadGroups();
    }

    private async void Action(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        switch (((Button)sender).Tag as string)
        {
            case "search": offset = 0; await List(); break;
            case "previous": offset = Math.Max(0, offset - 50); await List(); break;
            case "next": offset += 50; await List(); break;
            case "new": if (await CanReplaceDraft()) Edit(null); break;
            case "open":
                if (Targets.SelectedItem is CatalogItem current && await CanReplaceDraft())
                    Edit((await commands.Call("get_target", new { target_id = current.Id })).GetProperty("target"));
                break;
            case "save":
                await SaveDraft(); break;
            case "delete":
                if (edited != null && await commands.Confirm("删除目标？", edited.Label, "删除"))
                { await commands.Call("delete_target", new { target_id = edited.Id, expected_revision = edited.Revision }); Edit(null); await List(); }
                break;
            case "test": test(Fields(true)); break;
            case "group-new": await commands.Call("create_group", new { name = GroupName.Text, parent_group_id = Id(ParentGroup) }); await LoadGroups(); break;
            case "group-save":
                await SaveGroup(); break;
            case "group-delete":
                if (Groups.SelectedItem is CatalogItem del && await commands.Confirm("删除分组？", $"「{del.Label.Trim()}」中的目标将移入未分组。", "删除"))
                {
                    bool refreshEdited = !TargetChanged && edited?.Document.GetProperty("group_id").GetString() == del.Id;
                    await commands.Call("delete_group", new { group_id = del.Id, expected_revision = del.Revision });
                    await LoadGroups();
                    if (refreshEdited && edited != null)
                        Edit((await commands.Call("get_target", new { target_id = edited.Id })).GetProperty("target"));
                    await List();
                }
                break;
        }
    });

    private async void SelectGroup(object sender, SelectionChangedEventArgs e)
    {
        if (populatingGroups) return;
        var next = Groups.SelectedItem as CatalogItem;
        if (GroupChanged)
        {
            populatingGroups = true;
            try
            {
                Groups.SelectedItem = editedGroup;
                if (!await commands.Guard(CanReplaceGroup)) return;
                next = Groups.Items.Cast<CatalogItem>().FirstOrDefault(g => g.Id == next?.Id);
                Groups.SelectedItem = next;
            }
            finally { populatingGroups = false; }
        }
        EditGroup(next);
    }

    private void EditGroup(CatalogItem? group)
    {
        editedGroup = group;
        GroupName.Text = group?.Document.GetProperty("name").GetString() ?? "";
        var parent = group?.Document.GetProperty("parent_group_id").GetString();
        ParentGroup.SelectedItem = ParentGroup.Items.Cast<CatalogItem>().FirstOrDefault(g => g.Id == parent);
    }
    private void ConditionsChanged(object sender, TextChangedEventArgs e)
    {
        if (Tree == null) return;
        Tree.RootNodes.Clear();
        try { Tree.RootNodes.Add(Node(JsonSerializer.Deserialize<JsonElement>(Conditions.Text))); }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or KeyNotFoundException)
        { Tree.RootNodes.Add(new TreeViewNode { Content = "条件格式尚未完整" }); }
    }
    private static TreeViewNode Node(JsonElement value)
    {
        if (!value.TryGetProperty("children", out var children))
            return new() { Content = $"{value.GetProperty("id")} · {value.GetProperty("type")}" };
        var node = new TreeViewNode { Content = value.GetProperty("op").GetString() == "all" ? "全部条件" : "任一条件", IsExpanded = true };
        foreach (var child in children.EnumerateArray()) node.Children.Add(Node(child));
        return node;
    }
}
