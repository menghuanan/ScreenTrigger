using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace ScreenTrigger.User;

public sealed partial class GroupsView : UserControl
{
    private sealed record Group(string? Id, string Name, int Depth = -1, long Revision = 0)
    {
        public override string ToString() => new string('　', Math.Max(0, Depth)) + Name;
    }
    private sealed record Target(JsonElement Doc)
    {
        public override string ToString() => $"#{Doc.GetProperty("sample_no")}　{Doc.GetProperty("name").GetString()}";
    }

    private readonly UserSession session;
    private List<Group> groups = [];
    private List<Target> targets = [];
    private Group? Selected => Filter.SelectedItem as Group;
    private const string All = "*";

    internal GroupsView(UserSession session) { InitializeComponent(); this.session = session; }

    internal async Task Reload(string? select = null)
    {
        var old = select ?? Selected?.Id;
        var page = await session.Call("list_groups", new { });
        groups = [new(All, "全部目标"), new(null, "未分组"), .. page.GetProperty("groups").EnumerateArray().Select(g =>
            new Group(g.GetProperty("id").GetString(), g.GetProperty("name").GetString()!,
                g.GetProperty("depth").GetInt32(), g.GetProperty("revision").GetInt64()))];
        targets = (await session.List("list_targets")).Select(t => new Target(t)).ToList();
        bool first = Filter.ItemsSource == null;
        Filter.ItemsSource = groups;
        Filter.SelectedItem = first && select == null ? groups[0] : groups.Find(g => g.Id == old) ?? groups[0];
        ApplyFilter();
    }

    private void FilterChanged(object sender, SelectionChangedEventArgs e) => ApplyFilter();

    private void ApplyFilter()
    {
        if (Selected is not { } group) return;
        var visible = targets.Where(t => group.Id == All || t.Doc.GetProperty("group_id").GetString() == group.Id).ToList();
        RenameButton.IsEnabled = DeleteButton.IsEnabled = group.Depth >= 0;
        Targets.ItemsSource = visible;
    }

    private async Task Edit(bool create)
    {
        var current = Selected;
        if (!create && current is not { Depth: >= 0 }) return;
        var name = new TextBox { Header = "分组名称", MaxLength = 100, Text = create ? "" : current!.Name };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(name, "GroupName");
        var parent = new ComboBox { Header = "上级分组", HorizontalAlignment = HorizontalAlignment.Stretch };
        var panel = new StackPanel { Spacing = 12 };
        panel.Children.Add(name);
        if (create)
        {
            var choices = new[] { new Group(null, "顶层") }.Concat(groups.Where(g => g.Depth is 0 or 1)).ToList();
            parent.ItemsSource = choices;
            parent.SelectedItem = choices.Find(g => g.Id == current?.Id) ?? choices[0];
            panel.Children.Add(parent);
        }
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = create ? "新建分组" : "重命名分组",
            Content = panel, PrimaryButtonText = "保存", CloseButtonText = "取消", IsPrimaryButtonEnabled = !string.IsNullOrWhiteSpace(name.Text) };
        name.TextChanged += (_, _) => dialog.IsPrimaryButtonEnabled = !string.IsNullOrWhiteSpace(name.Text);
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        var result = create
            ? await session.Call("create_group", new { name = name.Text.Trim(), parent_group_id = (parent.SelectedItem as Group)?.Id })
            : await session.Call("update_group", new { group_id = current!.Id, expected_revision = current.Revision, name = name.Text.Trim() });
        await Reload(result.GetProperty("group").GetProperty("id").GetString());
    }

    private async void NewGroup(object s, RoutedEventArgs e) => await session.Run(() => Edit(true));
    private async void Rename(object s, RoutedEventArgs e) => await session.Run(() => Edit(false));
    private async void Delete(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (Selected is not { Depth: >= 0 } g) return;
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = $"删除分组「{g.Name}」？", PrimaryButtonText = "删除", CloseButtonText = "取消" };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        await session.Call("delete_group", new { group_id = g.Id, expected_revision = g.Revision });
        await Reload(All);
    });
    private async void Move(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (((MenuFlyout)Resources["TargetActions"]).Target is not ListViewItem { Content: Target target }) return;
        var picker = new ComboBox { Header = "目标分组", ItemsSource = groups.Skip(1).ToList(), HorizontalAlignment = HorizontalAlignment.Stretch };
        picker.SelectedItem = groups.Find(g => g.Id == target.Doc.GetProperty("group_id").GetString());
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = "移动目标", Content = picker, PrimaryButtonText = "移动", CloseButtonText = "取消" };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary || picker.SelectedItem is not Group group) return;
        await session.Call("move_target_to_group", new { target_id = target.Doc.GetProperty("id").GetString(),
            expected_revision = target.Doc.GetProperty("revision").GetInt64(), group_id = group.Id });
        await Reload();
    });
}
