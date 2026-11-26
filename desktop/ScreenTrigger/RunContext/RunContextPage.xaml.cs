using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using ScreenTrigger.Host;
using Windows.Storage.Pickers;
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
    private string? outputToken, outputSession, outputMode;
    private Dictionary<string, long>? startingWrites;
    private string? recordingId;
    private readonly DispatcherTimer recordingTimer = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private bool recordingQuery;
    private readonly nint owner;
    private bool pickingIsolatedRegion;
    private JsonElement? isolatedRegion, frozenDraft;
    private readonly Func<string, WorkspaceCommands, Task<bool>> openRecordingEditor;

    internal RunContextPage(HostClient client, bool luaAvailable, bool monitorMode, bool ocrAvailable, nint owner,
                            Func<string, WorkspaceCommands, Task<bool>> openRecordingEditor)
    {
        this.owner = owner;
        this.openRecordingEditor = openRecordingEditor;
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
        if (monitor) RecordingControls.Visibility = Visibility.Collapsed;
        recordingTimer.Tick += QueryRecording;
        Loaded += OnLoaded;
        Unloaded += async (_, _) => {
            subscription?.Dispose(); subscription = null;
            recordingTimer.Stop();
            if (recordingId is string id) {
                EndRecordingQuery("已离开录制页。");
                try { await commands.Call("cancel_recording", new { recording_id = id }); }
                catch (Exception) { /* The recording may have just finished. */ }
                try { await commands.Call("release_recording_draft", new { recording_id = id }); }
                catch (Exception) { /* A changed session has already disposed its resources. */ }
            }
            try { await ReleaseDraft(); } catch (Exception ex) { Feedback.Text = ex.Message; }
        };
    }
    private async void RecordClick(object sender, RoutedEventArgs args) => await commands.Run(async () => {
        var button = (Button)sender;
        if (button == StartRecording || button == StartIsolated) {
            var id = Guid.NewGuid().ToString();
            var parameters = button == StartIsolated ? await ChooseIsolatedRecording(id) : new { recording_id = id, context = new {
                display_id = bindingId == null ? (Displays.SelectedItem as CatalogItem)?.Id : null, binding_id = bindingId,
                region = CurrentRegion(), allow_exit = false } };
            if (parameters is null || !IsLoaded) return;
            await ReleaseDraft();
            if (!IsLoaded) return;
            recordingId = id;
            RecordingStatus.Text = "正在启动本次录制…";
            StartRecording.IsEnabled = StartIsolated.IsEnabled = false;
            JsonElement state;
            try {
                state = await host.RequestAsync("start_recording", parameters);
            } catch (HostException ex) when (IsRequestTimeout(ex)) {
                // The target may already be running. Recover this exact attempt without launching again.
                if (IsLoaded && recordingId == id) {
                    RecordingStatus.Text = "正在核对本次录制的启动结果…";
                    recordingTimer.Start();
                }
                return;
            } catch (Exception ex) {
                if (recordingId == id) EndRecordingQuery(ex.Message);
                throw;
            }
            if (!IsLoaded || recordingId != id) {
                if (state.GetProperty("status").GetString() is "recording" or "paused")
                    await commands.Call("cancel_recording", new { recording_id = id });
                return;
            }
            recordingTimer.Start();
            await DisplayRecordingReply(id, state);
        } else {
            if (recordingId is not string id) return;
            var command = button == PauseRecording ? "pause_recording" : button == ResumeRecording ? "resume_recording"
                : button == FinishRecording ? "finish_recording" : "cancel_recording";
            await DisplayRecordingReply(id, await commands.Call(command, new { recording_id = id }));
        }
    });
    private static bool IsRequestTimeout(HostException error) => error.Error.Code == "INTERNAL_ERROR" &&
        error.Error.Details.ValueKind == JsonValueKind.Object && error.Error.Details.TryGetProperty("deadline_ms", out _);

    private async Task ReleaseDraft()
    {
        var prior = frozenDraft;
        frozenDraft = null;
        EditRecordingScript.Visibility = Visibility.Collapsed;
        RecordingDraft.IsExpanded = false;
        RecordingDraft.Visibility = Visibility.Collapsed;
        RecordingActions.Text = "";
        if (prior is JsonElement draft)
            await commands.Call("release_recording_draft", new { recording_id = draft.GetProperty("recording_id").GetString() });
    }

    private async void EditRecording(object sender, RoutedEventArgs args) => await commands.Run(async () => {
        if (frozenDraft is not JsonElement draft) return;
        var id = draft.GetProperty("recording_id").GetString();
        var compiled = await commands.Call("compile_recording", new { scope = draft.GetProperty("scope"),
            actions = draft.GetProperty("actions"), interrupted = draft.GetProperty("interrupted") });
        if (!IsLoaded || frozenDraft?.GetProperty("recording_id").GetString() != id) return;
        var omitted = compiled.GetProperty("omitted");
        var locations = omitted.EnumerateArray().Take(12).Select(item => {
            int after = item.GetProperty("after").GetInt32();
            var position = after == 0 ? "首个动作前" : $"第 {after} 个动作后";
            var reason = item.GetProperty("reason").GetString() switch {
                "unpaired_on_finish" => "结束时仍有键或鼠标按钮未抬起",
                "key_focus_lost" or "bound_focus_lost" or "key_control_changed" => "输入焦点改变",
                "pointer_left_scope" or "drag_left_scope" => "指针移出录制范围",
                "geometry_changed" => "窗口或录制范围发生变化",
                "manual_pause" => "录制已暂停",
                "recording_limit" => "达到录制上限",
                var other => other
            };
            return $"{position} · {reason}";
        });
        var details = string.Join("\n", locations);
        if (compiled.GetProperty("action_count").GetInt32() == 0) {
            Feedback.Text = ("本次录制没有可编辑的完整动作。\n" + details).TrimEnd();
            return;
        }
        if (omitted.GetArrayLength() > 0) {
            var message = $"共 {omitted.GetArrayLength()} 处中止。\n{details}";
            if (!await commands.Confirm("只编辑完整动作？", message, "编辑完整动作")) return;
        }
        if (IsLoaded && frozenDraft?.GetProperty("recording_id").GetString() == id)
            await openRecordingEditor(compiled.GetProperty("source").GetString()!, commands);
    });

    private void EndRecordingQuery(string message)
    {
        recordingTimer.Stop();
        recordingId = null;
        RecordingStatus.Text = message;
        StartIsolated.IsEnabled = StartRecording.IsEnabled = true;
        PauseRecording.IsEnabled = ResumeRecording.IsEnabled = FinishRecording.IsEnabled = CancelRecording.IsEnabled = false;
        IsolatedScope.IsEnabled = IsolatedDisplay.IsEnabled = PickIsolatedRegion.IsEnabled = true;
    }

    private async Task<object?> ChooseIsolatedRecording(string id)
    {
        var scope = (string)((ComboBoxItem)IsolatedScope.SelectedItem).Tag;
        var displayId = scope == "window" ? null : ((IsolatedDisplay.SelectedItem as CatalogItem)?.Id
            ?? throw new InvalidOperationException("请选择录制显示器。"));
        var region = scope == "region" ? isolatedRegion : null;
        if (scope == "region" && (region is null || region.Value.GetProperty("display_id").GetString() != displayId))
            throw new InvalidOperationException("请在所选显示器上框选录制区域。");
        var picker = new FileOpenPicker();
        picker.FileTypeFilter.Add(".exe");
        WinRT.Interop.InitializeWithWindow.Initialize(picker, owner);
        var file = await picker.PickSingleFileAsync();
        if (file == null) return null;
        if (!System.IO.Path.IsPathFullyQualified(file.Path) ||
            !string.Equals(System.IO.Path.GetExtension(file.Path), ".exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("请选择本机可启动的 .exe 文件。");
        return new { recording_id = id, context = new { display_id = displayId, region, allow_exit = false },
            isolated_app = new { executable = file.Path, scope } };
    }
    private async void QueryRecording(object? sender, object args) {
        if (recordingQuery || recordingId is not string id) return;
        recordingQuery = true;
        try {
            await DisplayRecordingReply(id, await host.RequestAsync("get_recording_state", new { recording_id = id }));
        } catch (HostException ex) when (IsRequestTimeout(ex) || ex.Error.Code == "BUSY") {
            if (id == recordingId) RecordingStatus.Text = "等待宿主确认本次录制状态…";
        } catch (Exception ex) { if (id == recordingId) EndRecordingQuery(ex.Message); }
        finally { recordingQuery = false; }
    }
    private async Task DisplayRecordingReply(string id, JsonElement state)
    {
        if (recordingId != id || !IsLoaded) return;
        if (state.GetProperty("recording_id").GetString() != id)
            throw new InvalidOperationException("宿主返回了其他录制的状态。");
        if (state.GetProperty("status").GetString() == "finished" && !state.TryGetProperty("actions", out _))
            state = await host.RequestAsync("finish_recording", new { recording_id = id });
        if (recordingId == id && IsLoaded) ShowRecording(state);
    }
    private void ShowRecording(JsonElement state) {
        var status = state.GetProperty("status").GetString();
        var armed = status == "recording";
        var active = armed || status == "paused";
        var label = status switch { "recording" => "录制中", "paused" => "已暂停", "finished" => "已结束", _ => "已取消" };
        RecordingStatus.Text = $"{label} · {state.GetProperty("action_count").GetInt32()} 个完整动作，{state.GetProperty("interrupted").GetArrayLength()} 处中止";
        if (armed) RecordingStatus.Text += !state.GetProperty("keyboard_available").GetBoolean() ? " · 当前焦点尚无键盘录制来源" :
            state.GetProperty("keyboard_source").GetString() switch {
                "foreground_command_keys" => " · 当前控件可录功能键、导航键和修饰键",
                "isolated_control_keys" => " · 可记录隔离桌面中目标应用的按键",
                _ => " · 可记录当前窗口的按键"
            };
        if (status == "finished" && state.TryGetProperty("actions", out var actions)) {
            frozenDraft = state.Clone();
            RecordingDraft.IsExpanded = false;
            RecordingActions.Text = "";
            RecordingDraft.Visibility = Visibility.Visible;
            EditRecordingScript.Visibility = canExecute ? Visibility.Visible : Visibility.Collapsed;
        }
        IsolatedScope.IsEnabled = IsolatedDisplay.IsEnabled = PickIsolatedRegion.IsEnabled = !active;
        StartIsolated.IsEnabled = StartRecording.IsEnabled = !active;
        PauseRecording.IsEnabled = armed; ResumeRecording.IsEnabled = status == "paused";
        FinishRecording.IsEnabled = CancelRecording.IsEnabled = active;
        if (!active) { recordingTimer.Stop(); recordingId = null; }
    }
    private void ReadRecordingDraft(Expander sender, ExpanderExpandingEventArgs args)
    {
        RecordingActions.Text = frozenDraft?.GetProperty("actions").GetRawText() ?? "";
    }
    private void ChangeIsolatedScope(object sender, SelectionChangedEventArgs args)
    {
        if (IsolatedDisplay == null || PickIsolatedRegion == null || IsolatedRegionLabel == null) return;
        var scope = (IsolatedScope.SelectedItem as ComboBoxItem)?.Tag as string;
        IsolatedDisplay.Visibility = scope == "window" ? Visibility.Collapsed : Visibility.Visible;
        PickIsolatedRegion.Visibility = IsolatedRegionLabel.Visibility = scope == "region" ? Visibility.Visible : Visibility.Collapsed;
    }
    private void OnLoaded(object sender, RoutedEventArgs args)
    {
        subscription ??= host.Events.Subscribe(message =>
        {
            if (selectionId != null && message.Payload.TryGetProperty("selection_id", out var sid) && sid.GetString() == selectionId)
            {
                if (message.Name == "selection_completed") {
                    var region = message.Payload.GetProperty("region");
                    if (pickingIsolatedRegion) {
                        isolatedRegion = region.Clone();
                        IsolatedRegionLabel.Text = $"{region.GetProperty("display_id")}  ({region.GetProperty("x")}, {region.GetProperty("y")})  {region.GetProperty("width")} × {region.GetProperty("height")}";
                    } else Region.Text = region.GetRawText();
                }
                if (message.Name is "selection_completed" or "selection_cancelled") {
                    selectionId = null;
                    (pickingIsolatedRegion ? PickIsolatedRegion : PickRegion).Focus(FocusState.Programmatic);
                    pickingIsolatedRegion = false;
                }
            }
            if (message.Name == "text_exported" && message.Payload.TryGetProperty("execution_id", out var outputRun) &&
                outputRun.GetString() is string writtenId && message.Payload.TryGetProperty("bytes_written", out var size))
            {
                if (writtenId == executionId) ShowWrite(size.GetInt64());
                if (startingWrites != null) startingWrites[writtenId] = size.GetInt64();
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
        Fill(IsolatedDisplay, screens);
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
        var nextBinding = bound.ValueKind == JsonValueKind.Object ? bound.GetProperty("binding_id").GetString() : null;
        if (nextBinding != bindingId) BackgroundMode.IsChecked = false;
        bindingId = nextBinding;
        CheckInput.IsEnabled = CheckCapture.IsEnabled = BackgroundMode.IsEnabled = bindingId != null;
        Displays.IsEnabled = bindingId == null;
        PickRegion.IsEnabled = bindingId == null;
        PickRegion.Content = bindingId == null ? "在显示器上框选区域" : "绑定窗口使用客户区坐标，请在上方指定区域";
        if (bindingId == null) { BindingLabel.Text = "未绑定窗口。键盘输入发往当前前台窗口。"; return; }
        var info = await commands.Call("get_window_info", new { window_id = bound.GetProperty("window_id").GetString() });
        var w = info.GetProperty("window");
        BindingLabel.Text = $"已绑定「{w.GetProperty("title")}」 (PID {w.GetProperty("pid")})。前台模式须保持该窗口在前台。";
    }
    private async void TestBackgroundCapture(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        var binding = bindingId;
        if (binding == null) return;
        var result = await commands.Call("capture_window", new { binding_id = binding, window_mode = "background" });
        var image = result.GetProperty("image");
        await commands.Call("release_image", new { image_id = image.GetProperty("image_id").GetString() });
        var snapshot = await commands.Call("get_window_capture_capabilities", new { binding_id = binding });
        if (binding != bindingId) return;
        var capture = snapshot.GetProperty("capture");
        static string State(JsonElement v) => v.GetString() switch
        {
            "available" => "已实测",
            "unavailable" => "不可用",
            _ => "待实测"
        };
        Feedback.Text = $"新帧 {image.GetProperty("width")} × {image.GetProperty("height")} 像素。" +
            $"切换前台{State(capture.GetProperty("non_foreground"))}，完全遮挡{State(capture.GetProperty("fully_occluded"))}。";
    });
    private async void CheckBackgroundInput(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        var binding = bindingId;
        if (binding == null) return;
        var snapshot = await commands.Call("get_window_input_capabilities", new { binding_id = binding });
        if (binding != bindingId) return;
        ScreenTrigger.Contracts.WireContracts.Validate("windowInputCapabilities", snapshot);
        var input = snapshot.GetProperty("input");
        string State(string action) => input.GetProperty(action).GetString() switch
        {
            "available" => "支持", "unavailable" => "不可用", _ => "待验证"
        };
        Feedback.Text = $"点击{State("click")}，拖动{State("drag")}，滚轮{State("scroll")}，" +
            $"按键{State("key")}，组合键{State("hotkey")}。{snapshot.GetProperty("reason").GetString()}";
    });
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
    private JsonElement CurrentRegion()
    {
        var region = JsonSerializer.Deserialize<JsonElement>(string.IsNullOrWhiteSpace(Region.Text) ? "null" : Region.Text);
        if (bindingId == null || region.ValueKind != JsonValueKind.Object) return region;
        if (region.TryGetProperty("binding_id", out _) || region.TryGetProperty("display_id", out _) ||
            region.TryGetProperty("image_id", out _) || region.TryGetProperty("space", out var space) &&
            (space.ValueKind != JsonValueKind.String || space.GetString() != "window_client_physical")) return region;
        var local = region.Deserialize<Dictionary<string, JsonElement>>()!;
        local["space"] = JsonSerializer.SerializeToElement("window_client_physical");
        local["binding_id"] = JsonSerializer.SerializeToElement(bindingId);
        return JsonSerializer.SerializeToElement(local);
    }
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
        var config = new Dictionary<string, object?> {
            ["display_id"] = bindingId == null ? (Displays.SelectedItem as CatalogItem)?.Id : null,
            ["binding_id"] = bindingId, ["allow_exit"] = AllowExit.IsChecked == true, ["region"] = CurrentRegion(),
            ["debug"] = EnableDebug.IsChecked == true, ["frequency_hz"] = Frequency.Value, ["total_timeout_ms"] = Timeout.Value
        };
        if (bindingId != null && BackgroundMode.IsChecked == true) config["window_mode"] = "background";
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
        if (outputToken != null)
        {
            if (host.Events.Session != outputSession) throw new InvalidOperationException("会话已结束，请重新选择 TXT 文件。");
            config["output_token"] = outputToken;
        }
        var requestSignature = JsonSerializer.Serialize(config);
        // A fast local reply may re-enable the button before Windows delivers the second click.
        if (press is long current && acceptedPointerRun is { } previous && previous.Config == signature &&
            current - previous.Press <= inputSettings.DoubleClickTime)
            return;
        if (requestSignature != lastRunConfig || clientRunId == null) clientRunId = Guid.NewGuid().ToString();
        lastRunConfig = requestSignature;
        config["client_run_id"] = clientRunId;
        startingWrites = new();
        try
        {
            var result = await commands.Call(monitor ? "start_monitor" : "execute_script", config);
            executionId = result.GetProperty("execution_id").GetString();
            WriteStatus.Visibility = Visibility.Collapsed;
            if (executionId != null && startingWrites.TryGetValue(executionId, out long count)) ShowWrite(count);
        }
        finally { startingWrites = null; }
        if (outputToken != null)
            ResetTextOutput("该文件已授权给本次执行；再次启动时请重新选择。");
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
        var isolated = (Button)sender == PickIsolatedRegion;
        if ((isolated ? IsolatedDisplay : Displays).SelectedItem is not CatalogItem display) return;
        var accepted = await commands.Call("start_selection_mode", new { display_id = display.Id });
        pickingIsolatedRegion = isolated;
        selectionId = accepted.GetProperty("selection_id").GetString();
    });

    private void ResetDraft(object sender, RoutedEventArgs args)
    {
        Mode.SelectedIndex = 0; Count.Value = Frequency.Value = 1; Timeout.Value = 0;
        BackgroundMode.IsChecked = false;
        Arguments.Text = "{}"; Region.Text = ""; AllowExit.IsChecked = EnableDebug.IsChecked = false;
        Scripts.SelectedIndex = Targets.SelectedIndex = Displays.SelectedIndex = 0;
        IsolatedScope.SelectedIndex = IsolatedDisplay.SelectedIndex = 0;
        isolatedRegion = null; IsolatedRegionLabel.Text = "";
        Policy.Reset(); lastRunConfig = clientRunId = null;
        OutputMode.SelectedIndex = 0;
        _ = DiscardTextOutput();
        Feedback.Text = "已恢复本次草稿的默认值。";
    }

    private void ShowWrite(long bytes)
    {
        WriteStatus.Text = $"已写入 {bytes} 字节";
        WriteStatus.Visibility = Visibility.Visible;
    }

    private string SelectedOutputMode => (OutputMode.SelectedItem as ComboBoxItem)?.Tag as string ?? "append";

    private void ResetTextOutput(string message)
    {
        outputToken = outputSession = outputMode = null;
        ClearOutput.IsEnabled = false;
        OutputStatus.Text = message;
    }

    private async Task DiscardTextOutput()
    {
        var token = outputToken;
        var session = outputSession;
        ResetTextOutput("未选择输出文件");
        if (token != null && session != null && host.Events.Session == session)
        {
            try { await host.CallAsync(new { op = "discard_text_output", session_id = session, output_token = token }); }
            catch (Exception ex) { Feedback.Text = ex.Message; }
        }
    }

    private async void ChangeOutputMode(object sender, SelectionChangedEventArgs args)
    {
        if (outputToken != null && outputMode != SelectedOutputMode)
            await DiscardTextOutput();
    }

    private async void ClearTextOutput(object sender, RoutedEventArgs args) => await commands.Run(DiscardTextOutput);

    private async void PickTextOutput(object sender, RoutedEventArgs args)
    {
        await commands.Run(async () =>
        {
            var session = host.Events.Session ?? throw new InvalidOperationException("引擎会话未就绪。");
            var mode = SelectedOutputMode;
            var picked = await host.CallAsync(new { op = "pick_text_output", session_id = session, mode });
            if (host.Events.Session != session) throw new InvalidOperationException("会话已结束，请重新选择文件。");
            var token = picked.GetProperty("output_token").GetString();
            if (token == null) { Feedback.Text = "已取消文件选择。"; return; }
            await DiscardTextOutput();
            outputToken = token;
            outputSession = session;
            outputMode = mode;
            ClearOutput.IsEnabled = true;
            OutputStatus.Text = $"{(mode == "overwrite" ? "覆盖" : "追加")} · {picked.GetProperty("name").GetString()}";
        });
        if (IsLoaded && PickOutput.IsEnabled) PickOutput.Focus(FocusState.Programmatic);
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
