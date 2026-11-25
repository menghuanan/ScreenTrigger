using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using ScreenTrigger.Host;
using Windows.UI.ViewManagement;

namespace ScreenTrigger.RunContext;

public sealed partial class RunContextPage : UserControl
{
    private readonly WorkspaceCommands commands;
    private readonly HostClient host;
    private IDisposable? subscription;
    private string? bindingId;
    private long configRevision;
    private string? lastRunConfig, clientRunId;
    private long? pointerPress;
    private (long Press, string Config)? acceptedPointerRun;
    private readonly UISettings inputSettings = new();
    private readonly bool canExecute;

    private readonly bool monitor;
    private string? selectionId, executionId;

    internal RunContextPage(HostClient client, bool luaAvailable, bool monitorMode, bool ocrAvailable)
    {
        InitializeComponent();
        Execute.AddHandler(PointerPressedEvent, new PointerEventHandler((_, e) =>
        {
            if (e.GetCurrentPoint(Execute).Properties.IsLeftButtonPressed)
                pointerPress = Environment.TickCount64;
        }), true);
        Execute.AddHandler(KeyDownEvent, new KeyEventHandler((_, _) => pointerPress = null), true);
        Execute.PointerCanceled += (_, _) => pointerPress = null;
        host = client;
        canExecute = luaAvailable;
        monitor = monitorMode;
        Heading.Text = monitor ? "区域监控" : "运行设置";
        OcrNotice.Visibility = ocrAvailable ? Visibility.Collapsed : Visibility.Visible;
        if (monitor)
        {
            Scripts.Visibility = Mode.Visibility = Arguments.Visibility = Visibility.Collapsed;
            Targets.Header = "监控目标";
            Targets.Visibility = Policy.Visibility = Visibility.Visible;
            Execute.Content = "启动监控";
            Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(Execute, "StartMonitor");
        }
        Arguments.Text = "{}";
        commands = new(client, this, Feedback);
        Loaded += OnLoaded;
        Unloaded += (_, _) => { subscription?.Dispose(); subscription = null; };
    }
    private void OnLoaded(object sender, RoutedEventArgs args)
    {
        subscription ??= host.Events.Subscribe(message =>
        {
            if (selectionId != null && message.Payload.TryGetProperty("selection_id", out var sid) && sid.GetString() == selectionId)
            {
                if (message.Name == "selection_completed")
                {
                    Region.Text = message.Payload.GetProperty("region").GetRawText();
                    selectionId = null;
                    PickRegion.Focus(FocusState.Programmatic);
                }
                else if (message.Name == "selection_cancelled")
                {
                    selectionId = null;
                    PickRegion.Focus(FocusState.Programmatic);
                }
            }
            if (executionId != null && message.Payload.TryGetProperty("execution_id", out var run) && run.GetString() == executionId)
            {
                if (message.Name == "recognition_stats") Observation.Text = Status.RunFeedback.Stats(message.Payload);
                if (message.Name == "target_detected") Feedback.Text = Status.RunFeedback.Match(message.Payload.GetProperty("result"));
                if (message.Name == "script_state_changed") _ = ReadRun(executionId);
            }
            if (message.Name == "window_changed" && message.Payload.GetProperty("binding_id").GetString() == bindingId)
                Feedback.Text = $"窗口状态 {message.Payload.GetProperty("state")} · 几何修订 {message.Payload.GetProperty("geometry_revision")}";
        });
        Refresh(sender, args);
        if (executionId != null) _ = ReadRun(executionId);
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
        Policy.SetScripts((List<CatalogItem>)Scripts.ItemsSource);
        Execute.IsEnabled = canExecute && (monitor ? Targets.SelectedItem != null : Scripts.SelectedItem != null);
        if (!Execute.IsEnabled) Feedback.Text = !canExecute ? "执行引擎不可用。" : monitor ? "请先在目标库保存一个目标。" : "请先保存脚本。";
        await ReadKeys();
    });
    private async Task ReadBinding()
    {
        var data = await commands.Call("get_bound_window", new { });
        var bound = data.GetProperty("binding");
        bindingId = bound.ValueKind == JsonValueKind.Object ? bound.GetProperty("binding_id").GetString() : null;
        Displays.IsEnabled = bindingId == null;
        PickRegion.IsEnabled = bindingId == null;
        PickRegion.Content = bindingId == null ? "在显示器上框选区域" : "绑定窗口使用客户区坐标，请在上方指定区域";
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
        if (Count == null || Targets == null || monitor) return;
        Count.Visibility = RunMode == "count" ? Visibility.Visible : Visibility.Collapsed;
        Targets.Visibility = RunMode == "until_target" ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void StartRun(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        var press = pointerPress;
        pointerPress = null;
        using var region = JsonDocument.Parse(string.IsNullOrWhiteSpace(Region.Text) ? "null" : Region.Text);
        var config = new Dictionary<string, object?> {
            ["display_id"] = bindingId == null ? (Displays.SelectedItem as CatalogItem)?.Id : null,
            ["binding_id"] = bindingId, ["allow_exit"] = AllowExit.IsChecked == true, ["region"] = region.RootElement,
            ["debug"] = EnableDebug.IsChecked == true, ["frequency_hz"] = Frequency.Value, ["total_timeout_ms"] = Timeout.Value
        };
        if (monitor)
        {
            config["target_id"] = (Targets.SelectedItem as CatalogItem)?.Id;
            Policy.Apply(config);
        }
        else
        {
            config["script_id"] = (Scripts.SelectedItem as CatalogItem)?.Id;
            config["args"] = JsonSerializer.Deserialize<JsonElement>(Arguments.Text);
            config["mode"] = RunMode;
            config["iterations"] = RunMode == "count" ? Count.Value : null;
            config["stop_target_id"] = RunMode == "until_target" ? (Targets.SelectedItem as CatalogItem)?.Id : null;
        }
        var signature = JsonSerializer.Serialize(config);
        // A fast local reply may re-enable the button before Windows delivers the second click.
        if (press is long current && acceptedPointerRun is { } previous && previous.Config == signature &&
            current - previous.Press <= inputSettings.DoubleClickTime)
            return;
        if (signature != lastRunConfig || clientRunId == null) clientRunId = Guid.NewGuid().ToString();
        lastRunConfig = signature;
        config["client_run_id"] = clientRunId;
        var result = await commands.Call(monitor ? "start_monitor" : "execute_script", config);
        executionId = result.GetProperty("execution_id").GetString();
        acceptedPointerRun = press is long accepted ? (accepted, signature) : null;
        clientRunId = null;
        Feedback.Text = $"已受理执行 {executionId}";
        await ReadRun(executionId!);
    });

    private async Task ReadRun(string id)
    {
        try
        {
            var data = await commands.Call("get_script_state", new { execution_id = id });
            if (id == executionId) Observation.Text = Status.RunFeedback.State(data.GetProperty("state"));
        }
        catch (Exception ex) { if (id == executionId) Feedback.Text = ex.Message; }
    }

    private async void SelectRegion(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        if (Displays.SelectedItem is not CatalogItem display) return;
        var accepted = await commands.Call("start_selection_mode", new { display_id = display.Id });
        selectionId = accepted.GetProperty("selection_id").GetString();
    });

    private void ResetDraft(object sender, RoutedEventArgs args)
    {
        Mode.SelectedIndex = 0; Count.Value = Frequency.Value = 1; Timeout.Value = 0;
        Arguments.Text = "{}"; Region.Text = ""; AllowExit.IsChecked = EnableDebug.IsChecked = false;
        Scripts.SelectedIndex = Targets.SelectedIndex = Displays.SelectedIndex = 0;
        Policy.Reset(); lastRunConfig = clientRunId = null;
        Feedback.Text = "已恢复本次草稿的默认值。";
    }

    private async Task ReadKeys()
    {
        var data = await commands.Call("get_config", new { });
        var config = data.GetProperty("config");
        if (configRevision == 0)
        {
            Frequency.Value = config.GetProperty("recognition").GetProperty("default_frequency_hz").GetDouble();
            Timeout.Value = config.GetProperty("execution").GetProperty("total_timeout_ms").GetDouble();
            AllowExit.IsChecked = config.GetProperty("input").GetProperty("allow_exit").GetBoolean();
        }
        ShowKeys(config);
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
