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
    internal Action? OpenLua { get; set; }
    internal Func<Func<Task>, Task> Run { get; set; } = action => action();
    internal string Source { get; private set; } = "";
    internal bool HasCustomLua => steps.Any(s => s.Action == null);

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
        steps = ScriptAssembly.Read(source);
        Render(steps.Count == 0 ? -1 : 0);
    }

    private void Render(int index)
    {
        Steps.ItemsSource = steps.Select((step, i) => new AssemblyRow($"{i + 1:00}", step.Title,
            step.Summary(id => targets.FirstOrDefault(t => t.Id == id)?.Label))).ToList();
        StepCount.Text = $"{steps.Count} 步";
        Empty.Visibility = steps.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        Steps.SelectedIndex = index;
        if (index >= 0) Steps.ScrollIntoView(Steps.Items[index]);
        UpdateButtons();
    }

    private void Commit(int index)
    {
        Source = string.Join("\n", steps.Select(s => s.Source));
        Render(index);
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
        Up.IsEnabled = i > 0 && action != null;
        Down.IsEnabled = selected && i + 1 < steps.Count && steps[i + 1].Action != null;
        RemoveStep.IsEnabled = selected && action != null;
    }
    private void Move(object s, RoutedEventArgs e)
    {
        int index = Steps.SelectedIndex, next = index + (ReferenceEquals(s, Up) ? -1 : 1);
        (steps[index], steps[next]) = (steps[next], steps[index]);
        Commit(next);
    }
    private void Remove(object s, RoutedEventArgs e)
    {
        int index = Steps.SelectedIndex;
        steps.RemoveAt(index);
        Commit(Math.Min(index, steps.Count - 1));
    }

    private async void Add(object s, RoutedEventArgs e) => await Run(async () => {
        var action = (AssemblyAction)((Button)s).Tag;
        var step = await Configure(action, null);
        if (step == null) return;
        // Keep arbitrary Lua, including its possible top-level return, intact at the end.
        int at = steps.Count > 0 && steps[^1].Action == null ? steps.Count - 1 : steps.Count;
        steps.Insert(at, step);
        Commit(at);
    });
    private async void Edit(object s, RoutedEventArgs e) => await Run(async () => {
        int i = Steps.SelectedIndex;
        if (i < 0) return;
        if (steps[i].Action is not { } action) { OpenLua?.Invoke(); return; }
        var edited = await Configure(action, steps[i]);
        if (edited != null) { steps[i] = edited; Commit(i); }
    });

    private async Task<ScriptStep?> Configure(AssemblyAction action, ScriptStep? prior)
    {
        if (action.Fields.Length == 0) return action.Create([]);
        var panel = new StackPanel { Spacing = 8 };
        var values = new List<Func<string>>();
        foreach (var (field, index) in action.Fields.Select((field, i) => (field, i)))
        {
            string initial = prior?.Values[index] ?? field.Initial;
            if (field.Kind is FieldKind.Key or FieldKind.Button or FieldKind.Target)
            {
                var group = Filter();
                var choices = field.Kind switch {
                    FieldKind.Key => StepField.Keys.Select(k => new CatalogItem(k, k)).ToList(),
                    FieldKind.Button => [new("left", "左键"), new("right", "右键"), new("middle", "中键")],
                    _ => targets.Where(t => t.Id == initial || group == "*" || t.Document.GetProperty("group_id").GetString() == group).ToList()
                };
                var picker = new ComboBox { Header = field.Label, ItemsSource = choices, HorizontalAlignment = HorizontalAlignment.Stretch,
                    PlaceholderText = "请先在所选分组中添加目标", SelectedItem = choices.Find(c => c.Id == initial) ?? (prior == null ? choices.FirstOrDefault() : null) };
                panel.Children.Add(picker); values.Add(() => (picker.SelectedItem as CatalogItem)?.Id ?? "");
            }
            else
            {
                var input = new TextBox { Header = field.Label, Text = initial, AcceptsReturn = field.Kind == FieldKind.Text, MaxHeight = 130,
                    TextWrapping = field.Kind == FieldKind.Text ? TextWrapping.Wrap : TextWrapping.NoWrap };
                AutomationProperties.SetAutomationId(input, $"StepParameter{index}");
                panel.Children.Add(input); values.Add(() => input.Text.ReplaceLineEndings("\n"));
            }
        }
        var error = new TextBlock { TextWrapping = TextWrapping.Wrap };
        panel.Children.Add(error);
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = action.Name, Content = new ScrollViewer { Content = panel },
            PrimaryButtonText = prior == null ? "添加步骤" : "应用", CloseButtonText = "取消", DefaultButton = ContentDialogButton.Primary };
        ScriptStep? result = null;
        dialog.PrimaryButtonClick += (_, args) => {
            try { result = action.Create(values.Select(read => read()).ToArray()); }
            catch (FormatException ex) { args.Cancel = true; error.Text = ex.Message; }
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary ? result : null;
    }
}
