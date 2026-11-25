using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;

namespace ScreenTrigger.RunContext;

public sealed partial class RunContextPage : UserControl
{
    private readonly WorkspaceCommands commands;
    private readonly HostClient host;
    private IDisposable? subscription;
    private string? bindingId;
    private long configRevision;
    private string? lastRunConfig, clientRunId;
    private readonly bool canExecute;

    internal RunContextPage(HostClient client, bool luaAvailable)
    {
        InitializeComponent();
        host = client;
        canExecute = luaAvailable;
        Arguments.Text = "{}";
        commands = new(client, this, Feedback);
        Loaded += OnLoaded;
        Unloaded += (_, _) => { subscription?.Dispose(); subscription = null; };
    }
    private void OnLoaded(object sender, RoutedEventArgs args)
    {
        subscription ??= host.Events.Subscribe(message =>
        {
            if (message.Name == "window_changed" && message.Payload.GetProperty("binding_id").GetString() == bindingId)
                Feedback.Text = $"窗口状态 {message.Payload.GetProperty("state")} · 几何修订 {message.Payload.GetProperty("geometry_revision")}";
        });
        Refresh(sender, args);
    }
    private async void Refresh(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        var id = (Windows.SelectedItem as CatalogItem)?.Id;
        var data = await commands.Call("list_windows", new { });
        var rows = data.GetProperty("windows").EnumerateArray().Select(w => new CatalogItem(
            w.GetProperty("window_id").GetString(), $"{w.GetProperty("title")}　{w.GetProperty("process_name")} (PID {w.GetProperty("pid")})", w)).ToList();
        Windows.ItemsSource = rows;
        Windows.SelectedItem = rows.Find(w => w.Id == id) ?? rows.FirstOrDefault();
        await ReadBinding();
        var displays = await commands.Call("get_display_list", new { });
        var screens = displays.GetProperty("displays").EnumerateArray().Select(d => new CatalogItem(
            d.GetProperty("id").GetString(), $"{d.GetProperty("id")}　{d.GetProperty("rect").GetProperty("width")} × {d.GetProperty("rect").GetProperty("height")}", d)).ToList();
        Fill(Displays, screens);
        await LoadCatalog(Scripts, "list_scripts");
        await LoadCatalog(Targets, "list_targets");
        Execute.IsEnabled = canExecute && Scripts.SelectedItem != null;
        await ReadKeys();
    });
    private async Task ReadBinding()
    {
        var data = await commands.Call("get_bound_window", new { });
        var bound = data.GetProperty("binding");
        bindingId = bound.ValueKind == JsonValueKind.Object ? bound.GetProperty("binding_id").GetString() : null;
        Displays.IsEnabled = bindingId == null;
        if (bindingId == null) { BindingLabel.Text = "未绑定窗口。键盘输入发往当前前台窗口。"; return; }
        var info = await commands.Call("get_window_info", new { window_id = bound.GetProperty("window_id").GetString() });
        var w = info.GetProperty("window");
        BindingLabel.Text = $"已绑定「{w.GetProperty("title")}」 (PID {w.GetProperty("pid")})。运行时须将它置于前台。";
    }
    private static void Fill(ComboBox box, List<CatalogItem> rows)
    {
        var selected = (box.SelectedItem as CatalogItem)?.Id;
        box.ItemsSource = rows;
        box.SelectedItem = rows.Find(item => item.Id == selected) ?? rows.FirstOrDefault();
    }

    private async Task LoadCatalog(ComboBox box, string command)
    {
        var rows = new List<CatalogItem>();
        while (true)
        {
            var result = await commands.Call(command, new { offset = rows.Count, limit = 100 });
            var items = result.GetProperty("items");
            rows.AddRange(items.EnumerateArray().Select(item => new CatalogItem(
                item.GetProperty("id").GetString(), item.GetProperty("name").GetString()!, item)));
            if (items.GetArrayLength() == 0 || rows.Count >= result.GetProperty("total").GetInt32()) break;
        }
        Fill(box, rows);
    }

    private string? RunMode => (Mode.SelectedItem as ComboBoxItem)?.Tag as string;
    private void ChangeMode(object sender, SelectionChangedEventArgs args)
    {
        if (Count == null || Targets == null) return;
        Count.Visibility = RunMode == "count" ? Visibility.Visible : Visibility.Collapsed;
        Targets.Visibility = RunMode == "until_target" ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void StartRun(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        if (Scripts.SelectedItem is not CatalogItem script) return;
        using var parameters = JsonDocument.Parse(Arguments.Text);
        using var region = JsonDocument.Parse(string.IsNullOrWhiteSpace(Region.Text) ? "null" : Region.Text);
        var config = new Dictionary<string, object?> {
            ["script_id"] = script.Id, ["args"] = parameters.RootElement, ["mode"] = RunMode,
            ["iterations"] = RunMode == "count" ? Count.Value : null,
            ["stop_target_id"] = RunMode == "until_target" ? (Targets.SelectedItem as CatalogItem)?.Id : null,
            ["display_id"] = bindingId == null ? (Displays.SelectedItem as CatalogItem)?.Id : null,
            ["binding_id"] = bindingId, ["allow_exit"] = AllowExit.IsChecked == true, ["region"] = region.RootElement,
            ["frequency_hz"] = Frequency.Value, ["total_timeout_ms"] = Timeout.Value
        };
        var signature = JsonSerializer.Serialize(config);
        if (signature != lastRunConfig || clientRunId == null) clientRunId = Guid.NewGuid().ToString();
        lastRunConfig = signature;
        config["client_run_id"] = clientRunId;
        var result = await commands.Call("execute_script", config);
        clientRunId = null;
        Feedback.Text = $"已受理执行 {result.GetProperty("execution_id")}";
    });

    private async Task ReadKeys()
    {
        var data = await commands.Call("get_config", new { });
        ShowKeys(data.GetProperty("config"));
    }

    private void ShowKeys(JsonElement config)
    {
        configRevision = config.GetProperty("revision").GetInt64();
        var keys = config.GetProperty("hotkeys");
        SelectionKey.Text = keys.GetProperty("start_selection").GetString()!;
        StopKey.Text = keys.GetProperty("stop_script").GetString()!;
        PauseKey.Text = keys.GetProperty("pause_script").GetString()!;
    }

    private async void ReloadKeys(object sender, RoutedEventArgs args) => await commands.Run(ReadKeys);
    private async void SaveKeys(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        var hotkeys = new { start_selection = SelectionKey.Text, stop_script = StopKey.Text, pause_script = PauseKey.Text };
        var data = await commands.Call("update_config", new { expected_revision = configRevision, patch = new { hotkeys } });
        ShowKeys(data.GetProperty("config"));
        Feedback.Text = "快捷键已保存并注册。";
    });

    private async void Bind(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        if (Windows.SelectedItem is not CatalogItem item) throw new InvalidOperationException("请选择一个已打开的窗口。");
        await commands.Call("bind_window", new { window_id = item.Id });
        await ReadBinding();
    });
    private async void Unbind(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        await commands.Call("unbind_window", new { });
        await ReadBinding();
    });
}
