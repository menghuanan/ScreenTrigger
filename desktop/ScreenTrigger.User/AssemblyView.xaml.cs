using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace ScreenTrigger.User;

public sealed record AssemblyRow(string Number, string Title, string Detail)
{
    public Visibility DetailVisibility => Detail.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    public override string ToString() => $"{Number} {Title} {Detail}".TrimEnd();
}

public sealed partial class AssemblyView : UserControl
{
    private List<ScriptStep> steps = [];
    private IReadOnlyList<CatalogItem> targets = [];
    internal Func<string?> Filter { get; set; } = () => "*";
    internal Func<RecordingPoint?, Task<(int X, int Y)?>>? PickPoint { get; set; }
    internal event Action? Changed;
    private string trailing = "";
    internal Func<Func<Task>, Task> Run { get; set; } = action => action();
    internal string Source { get; private set; } = "";
    internal bool OnlyCustomLua => steps.Count != 0 && steps.All(s => s.Action == null);

    public AssemblyView()
    {
        InitializeComponent();
        foreach (var group in ScriptAssembly.Actions.GroupBy(a => a.Category))
        {
            Palette.Children.Add(new TextBlock { Text = group.Key, Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
                Margin = new(0, 7, 0, 2), Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"] });
            foreach (var action in group)
            {
                var button = new Button { Content = action.Name, Tag = action, HorizontalAlignment = HorizontalAlignment.Stretch,
                    HorizontalContentAlignment = HorizontalAlignment.Left };
                AutomationProperties.SetAutomationId(button, "AddStep_" + action.Name);
                button.Click += Add;
                Palette.Children.Add(button);
            }
        }
    }

    internal void LoadSource(string source, IReadOnlyList<CatalogItem> library)
    {
        Source = source; targets = library;
        steps = ScriptAssembly.Read(source, out trailing);
        Render(steps.Count == 0 ? -1 : 0);
    }

    private void Render(int index)
    {
        Steps.ItemsSource = steps.Select((step, i) => new AssemblyRow($"{i + 1:00}", new string('　', Math.Min(step.Depth, 4)) + step.Title,
            step.Summary(id => targets.FirstOrDefault(t => t.Id == id)?.Label))).ToList();
        StepCount.Text = $"{steps.Count} 步";
        Empty.Visibility = steps.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        Steps.SelectedIndex = index;
        if (index >= 0) Steps.ScrollIntoView(Steps.Items[index]);
        UpdateButtons();
    }

    private void Commit(int index)
    {
        Source = ScriptAssembly.Write(steps, trailing);
        steps = ScriptAssembly.Read(Source, out trailing);
        Render(Math.Min(index, steps.Count-1));
        Changed?.Invoke();
    }

    private void SelectionChanged(object sender, SelectionChangedEventArgs e) => UpdateButtons();
    private void UpdateButtons()
    {
        int i = Steps.SelectedIndex;
        if (EditStep == null) return;
        bool selected = i >= 0 && i < steps.Count;
        var action = selected ? steps[i].Action : null;
        EditStep.IsEnabled = selected && (action == null || action.Fields.Length > 0);
        EditStep.Content = selected && action == null ? "编辑 Lua" : "编辑";
        bool CanSwap(int j) => selected && j >= 0 && j < steps.Count && !steps[i].Boundary && !steps[j].Boundary
            && steps[i].Depth == steps[j].Depth && steps[i].Label != "返回结果" && steps[j].Label != "返回结果";
        Up.IsEnabled = CanSwap(i-1);
        Down.IsEnabled = CanSwap(i+1);
        RemoveStep.IsEnabled = selected && !steps[i].Boundary;
    }
    private void Move(object s, RoutedEventArgs e)
    {
        int index = Steps.SelectedIndex, next = index + (ReferenceEquals(s, Up) ? -1 : 1);
        var first = steps[index]; var second = steps[next];
        steps[index] = second with { Prefix = first.Prefix }; steps[next] = first with { Prefix = second.Prefix };
        Commit(next);
    }
    private void Remove(object s, RoutedEventArgs e)
    {
        int index = Steps.SelectedIndex;
        string prefix = steps[index].Prefix;
        steps.RemoveAt(index);
        if (index < steps.Count) steps[index] = steps[index] with { Prefix = prefix + steps[index].Prefix };
        else trailing = prefix + trailing;
        Commit(Math.Min(index, steps.Count - 1));
    }

    private async void Add(object s, RoutedEventArgs e) => await Run(async () => {
        var action = (AssemblyAction)((Button)s).Tag;
        var step = await Configure(action, null);
        if (step == null) return;
        int at = steps.FindIndex(s => s.Depth == 0 && s.Label == "返回结果");
        if (at < 0) at = steps.Count;
        string prefix;
        if (at < steps.Count)
        {
            prefix = steps[at].Prefix;
            steps[at] = steps[at] with { Prefix = "\n" };
        }
        else
        {
            prefix = trailing;
            if (at != 0 && !prefix.EndsWith('\n')) prefix += "\n";
            trailing = "";
        }
        steps.Insert(at, step with { Prefix = prefix });
        Commit(at);
    });
    private async void Edit(object s, RoutedEventArgs e) => await Run(async () => {
        int i = Steps.SelectedIndex;
        if (i < 0) return;
        if (steps[i].Action is not { } action)
        {
            var raw = new TextBox { AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinWidth = 320, MaxHeight = 280 };
            raw.Text = steps[i].Source.ReplaceLineEndings("\r");
            var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = steps[i].Title, Content = raw, PrimaryButtonText = "应用", CloseButtonText = "取消" };
            string initial = raw.Text;
            if (await dialog.ShowAsync() == ContentDialogResult.Primary && raw.Text != initial)
            { steps[i] = steps[i] with { Source = raw.Text.ReplaceLineEndings("\n") }; Commit(i); }
            return;
        }
        var edited = await Configure(action, steps[i]);
        if (edited != null) { steps[i] = edited; Commit(i); }
    });

    private async Task<ScriptStep?> Configure(AssemblyAction action, ScriptStep? prior)
    {
        if (action.Fields.Length == 0) return action.Create([]);
        var panel = new StackPanel { Spacing = 8 };
        var values = new List<Func<string>>();
        var inputs = new Dictionary<int, TextBox>();
        foreach (var (field, index) in action.Fields.Select((field, i) => (field, i)))
        {
            string initial = prior?.Values[index] ?? field.Initial;
            if (field.Kind is FieldKind.Key or FieldKind.Button or FieldKind.Target or FieldKind.Scope or FieldKind.Strategy)
            {
                var group = Filter();
                var choices = field.Kind switch {
                    FieldKind.Key => StepField.Keys.Select(k => new CatalogItem(k, k)).ToList(),
                    FieldKind.Button => [new("left", "左键"), new("right", "右键"), new("middle", "中键")],
                    FieldKind.Scope => [new("display", "显示器"), new("window","窗口"), new("region","选区")],
                    FieldKind.Strategy => [new("relative","相对尺寸"), new("offset","像素偏移")],
                    _ => targets.Where(t => t.Id == initial || group == "*" || t.Document.GetProperty("group_id").GetString() == group).ToList()
                };
                var picker = new ComboBox { Header = field.Label, ItemsSource = choices, HorizontalAlignment = HorizontalAlignment.Stretch,
                    PlaceholderText = "请先在所选分组中添加目标", SelectedItem = choices.Find(c => c.Id == initial) ?? (prior == null ? choices.FirstOrDefault() : null) };
                panel.Children.Add(picker); values.Add(() => (picker.SelectedItem as CatalogItem)?.Id ?? "");
            }
            else
            {
                bool multiline = field.Kind == FieldKind.Text || field.Table;
                var input = new TextBox { Header = field.Label, AcceptsReturn = multiline, MaxHeight = 152,
                    TextWrapping = multiline ? TextWrapping.Wrap : TextWrapping.NoWrap };
                // RichEdit discards subsequent lines when initialized in single-line mode.
                input.Text = multiline ? initial.ReplaceLineEndings("\r") : initial;
                AutomationProperties.SetAutomationId(input, $"StepParameter{index}");
                inputs[index] = input;
                panel.Children.Add(input); values.Add(() => input.Text.ReplaceLineEndings("\n"));
            }
        }
        var error = new TextBlock { TextWrapping = TextWrapping.Wrap };
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = action.Name, Content = new ScrollViewer { Content = panel },
            PrimaryButtonText = prior == null ? "添加步骤" : "应用", CloseButtonText = "取消", DefaultButton = ContentDialogButton.Primary };
        bool picking = false;
        dialog.Closing += (_, args) => { if (picking) args.Cancel = true; };
        void AddPick(string title, string id, int index, bool last = false)
        {
            var button = new Button { Content = title };
            AutomationProperties.SetAutomationId(button, id);
            button.Click += async (_, _) => {
                if (picking) return;
                picking = true; panel.IsHitTestVisible = false; dialog.IsPrimaryButtonEnabled = false;
                try
                {
                    bool path = action.First == "drag_anchor";
                    string[] lines = path ? inputs[0].Text.ReplaceLineEndings("\n").Split('\n', StringSplitOptions.RemoveEmptyEntries) : [];
                    int line = last ? lines.Length-1 : 0;
                    RecordingPoint? anchor = path
                        ? (lines.Length >= 2 ? RecordingPoint.Parse(lines[line]) : throw new FormatException("拖拽至少需要两个轨迹点。"))
                        : action.First is "click_anchor" or "scroll_anchor" ? RecordingPoint.Parse(string.Join(" ", values.Take(6).Select(f => f()))) : null;
                    var point = await PickPoint!(anchor);
                    if (point is not { } p) return;
                    if (path) { lines[line] = (anchor! with { X = p.X, Y = p.Y }).Row; inputs[0].Text = string.Join("\r", lines); }
                    else { inputs[index].Text = p.X.ToString(); inputs[index+1].Text = p.Y.ToString(); }
                    error.Text = "";
                }
                catch (Exception ex) { error.Text = ex.Message; }
                finally { picking = false; panel.IsHitTestVisible = true; dialog.IsPrimaryButtonEnabled = true; }
            };
            buttons.Children.Add(button);
        }
        if (PickPoint != null)
        {
            if (action.First is "click_at" or "move_to") AddPick("快速取点", "StepPickXY", 0);
            if (action.First is "click_anchor" or "scroll_anchor") AddPick("快速取点", "StepPickXY", 4);
            if (action.First is "drag" or "drag_anchor") { AddPick("取起点", "StepPickStart", 0); AddPick("取终点", "StepPickEnd", 2, true); }
        }
        if (buttons.Children.Count > 0) panel.Children.Add(buttons);
        panel.Children.Add(error);
        ScriptStep? result = null;
        dialog.PrimaryButtonClick += (_, args) => {
            try { result = action.Create(values.Select(read => read()).ToArray(), prior); }
            catch (FormatException ex) { args.Cancel = true; error.Text = ex.Message; }
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary ? result : null;
    }
}
