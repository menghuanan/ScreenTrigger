using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Imaging;
using ScreenTrigger.Import;

namespace ScreenTrigger.User;

public sealed partial class GroupsView : UserControl
{
    private readonly UserSession session;
    private readonly WorkspaceSurface surface;
    private List<CatalogItem> groups = [];
    private CatalogItem? editing, activeGroup;
    private string? imageId, imageSession, originalGroup;
    private string originalName = "";
    private readonly Dictionary<string, TreeViewNode> groupNodes = [], targetNodes = [];
    private const string Ungrouped = "";

    internal GroupsView(UserSession session, WorkspaceSurface surface)
    { InitializeComponent(); this.session = session; this.surface = surface; }

    internal async Task Reload()
    {
        var items = (await session.Call("list_groups", new { })).GetProperty("groups");
        groups = [new(null, "未分组"), .. items.EnumerateArray().Select(g => new CatalogItem(g.GetProperty("id").GetString(),
            g.GetProperty("name").GetString()!, g))];
        var targets = await session.List("list_targets");
        var groupId = (TargetGroup.SelectedItem as CatalogItem)?.Id;
        var collapsed = groupNodes.Where(p => !p.Value.IsExpanded).Select(p => p.Key).ToHashSet();
        LibraryTree.RootNodes.Clear(); groupNodes.Clear(); targetNodes.Clear();
        foreach (var g in groups)
            groupNodes[g.Id ?? Ungrouped] = new TreeViewNode { Content = g.Label, IsExpanded = !collapsed.Contains(g.Id ?? Ungrouped) };
        foreach (var g in groups)
        {
            var node = groupNodes[g.Id ?? Ungrouped];
            string? parent = g.Id == null ? null : g.Document.GetProperty("parent_group_id").GetString();
            if (parent != null && groupNodes.TryGetValue(parent, out var upper)) upper.Children.Add(node);
            else LibraryTree.RootNodes.Add(node);
        }
        foreach (var t in targets)
        {
            var node = new TreeViewNode { Content = $"#{t.GetProperty("sample_no")}　{t.GetProperty("name")}" };
            targetNodes[t.GetProperty("id").GetString()!] = node;
            groupNodes[t.GetProperty("group_id").GetString() ?? Ungrouped].Children.Add(node);
        }
        TargetGroup.ItemsSource = groups;
        TargetGroup.SelectedItem = Group(groupId);
        SetActiveGroup(activeGroup?.Id);
        LibraryTree.SelectedNode = editing?.Id is string id ? targetNodes.GetValueOrDefault(id) : groupNodes[activeGroup?.Id ?? Ungrouped];
    }
    private CatalogItem Group(string? id) => groups.Find(g => g.Id == id) ?? groups[0];
    private void SetActiveGroup(string? id)
    {
        activeGroup = Group(id);
        RenameButton.IsEnabled = DeleteButton.IsEnabled = activeGroup.Id != null;
    }

    private async void OpenNode(TreeView sender, TreeViewItemInvokedEventArgs args)
    {
        var node = args.InvokedItem as TreeViewNode ?? sender.SelectedNode;
        if (node == null) return;
        string? group = groupNodes.FirstOrDefault(p => p.Value == node).Key;
        string? target = targetNodes.FirstOrDefault(p => p.Value == node).Key;
        if (group == null && target == null) return;
        await session.Run(async () => {
            if (!await Leave())
            {
                LibraryTree.SelectedNode = editing?.Id is string id ? targetNodes.GetValueOrDefault(id) : groupNodes[activeGroup?.Id ?? Ungrouped];
                return;
            }
            if (target != null) await OpenTarget(target);
            else { SetActiveGroup(group == Ungrouped ? null : group); await ResetDraft(activeGroup?.Id); }
        });
    }

    private void SetDraft(CatalogItem? target, string? group)
    {
        editing = target;
        TargetTitle.Text = target == null ? "新建目标" : "编辑目标";
        NewTargetButton.Visibility = target == null ? Visibility.Collapsed : Visibility.Visible;
        TargetName.Text = originalName = target?.Label ?? "";
        originalGroup = group;
        TargetGroup.SelectedItem = Group(group);
    }
    private async Task ResetDraft(string? group)
    {
        await ReleaseDraftImage();
        SetDraft(null, group);
        Preview.Source = null; PreviewHint.Visibility = Visibility.Visible; Details.Text = "";
    }
    private async Task OpenTarget(string id)
    {
        var result = await session.Call("get_target", new { target_id = id, preview_max_edge = 480 });
        var t = result.GetProperty("target");
        var bitmap = await ImagePicker.DecodePreviewAsync(result.GetProperty("preview").GetProperty("base64").GetString()!);
        await ReleaseDraftImage();
        SetActiveGroup(t.GetProperty("group_id").GetString());
        SetDraft(new(id, t.GetProperty("name").GetString()!, t), activeGroup?.Id);
        ShowPreview(bitmap, t.GetProperty("image"));
        LibraryTree.SelectedNode = targetNodes.GetValueOrDefault(id);
    }
    private void ShowPreview(BitmapImage bitmap, JsonElement image)
    {
        Preview.Source = bitmap; PreviewHint.Visibility = Visibility.Collapsed;
        Details.Text = $"{image.GetProperty("width")} × {image.GetProperty("height")} px";
    }
    private async Task ReleaseDraftImage()
    {
        var old = imageId; var active = imageSession;
        imageId = imageSession = null;
        if (old != null && active != null && session.Host?.Events.Session == active)
            await session.Host.RequestCheckedAsync(active, "release_image", new { image_id = old });
    }
    private async Task UseImage(JsonElement image, string active)
    {
        var id = image.GetProperty("image_id").GetString()!;
        try
        {
            var result = await session.Host!.RequestCheckedAsync(active, "get_image_preview", new { image_id = id, max_edge = 480 });
            var bitmap = await ImagePicker.DecodePreviewAsync(result.GetProperty("base64").GetString()!);
            if (session.Host.Events.Session != active) return;
            await ReleaseDraftImage();
            imageId = id; imageSession = active;
            ShowPreview(bitmap, image);
        }
        finally
        {
            if (imageId != id && session.Host?.Events.Session == active)
                await session.Host.RequestCheckedAsync(active, "release_image", new { image_id = id });
        }
    }
    private async void SelectImage(object s, RoutedEventArgs e) => await session.Run(async () => {
        var active = session.Host!.Events.Session!;
        var picked = await surface.Select();
        if (picked != null) await UseImage(picked.Value.GetProperty("image"), active);
    });
    private async void ImportImage(object s, RoutedEventArgs e) => await session.Run(async () => {
        var host = session.Host!; var active = host.Events.Session!;
        var picked = await host.CallAsync(new { op = "pick_image", session_id = active });
        if (picked.GetProperty("import_token").GetString() is not string token) return;
        var result = await host.RequestCheckedAsync(active, "import_image", new { import_token = token });
        await UseImage(result.GetProperty("image"), active);
    });

    private async Task SaveDraft()
    {
        string name = TargetName.Text.Trim();
        if (name.Length == 0) throw new InvalidOperationException("请填写目标名称。");
        if (editing == null && imageId == null) throw new InvalidOperationException("请先框选屏幕或导入图片。");
        var fields = new Dictionary<string, object?> { ["name"] = name, ["group_id"] = (TargetGroup.SelectedItem as CatalogItem)?.Id };
        if (imageId != null) { fields["image_id"] = imageId; fields["storage_mode"] = "original"; }
        JsonElement saved;
        if (editing == null)
        {
            fields["anchor_condition_id"] = "c1";
            fields["conditions"] = new { op = "all", children = new[] { new { id = "c1", type = "template", @params = new { threshold = .85 } } } };
            saved = await session.Call("create_target", new { draft = fields });
        }
        else saved = await session.Call("update_target", new { target_id = editing.Id, expected_revision = editing.Revision, patch = fields });
        var doc = saved.GetProperty("target");
        var id = doc.GetProperty("id").GetString()!;
        SetDraft(new(id, name, doc), doc.GetProperty("group_id").GetString());
        SetActiveGroup(originalGroup);
        await ReleaseDraftImage();
        await Reload();
        Details.Text = $"已保存目标 #{doc.GetProperty("sample_no")}　·　{doc.GetProperty("image").GetProperty("width")} × {doc.GetProperty("image").GetProperty("height")} px";
        session.Log($"已保存目标「{name}」");
    }
    private async void Save(object s, RoutedEventArgs e) => await session.Run(SaveDraft);
    private async void NewTarget(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (await Leave()) { await ResetDraft(activeGroup?.Id); LibraryTree.SelectedNode = groupNodes[activeGroup?.Id ?? Ungrouped]; }
    });
    internal async Task<bool> Leave()
    {
        if (imageId == null && originalName == TargetName.Text && originalGroup == (TargetGroup.SelectedItem as CatalogItem)?.Id) return true;
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = "保存目标的修改？", PrimaryButtonText = "保存",
            SecondaryButtonText = "放弃修改", CloseButtonText = "继续编辑" };
        var choice = await dialog.ShowAsync();
        if (choice == ContentDialogResult.None) return false;
        if (choice == ContentDialogResult.Primary) await SaveDraft(); else await ResetDraft(activeGroup?.Id);
        return true;
    }

    private async Task EditGroup(bool create)
    {
        if (!await Leave()) return;
        var current = activeGroup;
        if (!create && current?.Id == null) return;
        var name = new TextBox { Header = "分组名称", MaxLength = 100, Text = create ? "" : current!.Label };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(name, "GroupName");
        var choices = new List<CatalogItem> { new(null, "顶层") };
        choices.AddRange(groups.Where(g => g.Id != null && g.Document.GetProperty("depth").GetInt32() < 2));
        var parent = new ComboBox { Header = "上级分组", ItemsSource = choices, SelectedItem = choices.Find(g => g.Id == current?.Id) ?? choices[0],
            HorizontalAlignment = HorizontalAlignment.Stretch };
        var panel = new StackPanel { Spacing = 10 }; panel.Children.Add(name);
        if (create) panel.Children.Add(parent);
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = create ? "新建分组" : "重命名分组", Content = panel,
            PrimaryButtonText = "保存", CloseButtonText = "取消", IsPrimaryButtonEnabled = !string.IsNullOrWhiteSpace(name.Text) };
        name.TextChanged += (_, _) => dialog.IsPrimaryButtonEnabled = !string.IsNullOrWhiteSpace(name.Text);
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        var result = create ? await session.Call("create_group", new { name = name.Text.Trim(), parent_group_id = (parent.SelectedItem as CatalogItem)?.Id })
            : await session.Call("update_group", new { group_id = current!.Id, expected_revision = current.Revision, name = name.Text.Trim() });
        await Reload();
        SetActiveGroup(result.GetProperty("group").GetProperty("id").GetString());
        await ResetDraft(activeGroup?.Id); LibraryTree.SelectedNode = groupNodes[activeGroup!.Id!];
    }
    private async void NewGroup(object s, RoutedEventArgs e) => await session.Run(() => EditGroup(true));
    private async void RenameGroup(object s, RoutedEventArgs e) => await session.Run(() => EditGroup(false));
    private async void DeleteGroup(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (activeGroup is not { Id: not null } group || !await Leave()) return;
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = $"删除分组「{group.Label}」？", Content = "该组中的目标将移入未分组。",
            PrimaryButtonText = "删除", CloseButtonText = "取消" };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        await session.Call("delete_group", new { group_id = group.Id, expected_revision = group.Revision });
        await Reload(); SetActiveGroup(null); await ResetDraft(null); LibraryTree.SelectedNode = groupNodes[Ungrouped];
    });

    internal void EndEngineSession()
    {
        bool expired = imageId != null;
        imageId = imageSession = null;
        if (expired) { Preview.Source = null; PreviewHint.Visibility = Visibility.Visible; Details.Text = "引擎会话已结束，请重新选择图片。"; }
    }
}
