using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;
using Windows.Graphics;

namespace ScreenTrigger.User;

public sealed partial class WorkView : UserControl
{
    private sealed record Area(string Id, string Label, bool Window, RectInt32 Bounds)
    { public override string ToString() => Label; }

    private readonly UserSession session;
    private readonly WorkspaceSurface surface;
    private List<Area> areas = [];
    private RectInt32? selection;
    private string? selectedId, execution, runName, status;
    private Dictionary<string, object?>? pending;
    private bool filling;

    internal WorkView(UserSession session, WorkspaceSurface surface)
    {
        this.session = session;
        this.surface = surface;
        InitializeComponent();
    }

    internal async Task Reload(IReadOnlyList<ScriptRow> scripts)
    {
        var old = (Script.SelectedItem as ScriptRow)?.Id;
        Script.ItemsSource = scripts;
        Script.SelectedItem = scripts.FirstOrDefault(s => s.Id == old) ?? scripts.FirstOrDefault();
        await LoadAreas();
        UpdateControls();
    }

    private async Task LoadAreas()
    {
        var displays = (await session.Call("get_display_list", new { })).GetProperty("displays");
        var windows = (await session.Call("list_windows", new { })).GetProperty("windows");
        List<Area> choices = [];
        foreach (var d in displays.EnumerateArray())
        {
            string id = d.GetProperty("id").GetString()!;
            var rect = WorkspaceSurface.Rect(d.GetProperty("rect"));
            choices.Add(new(id, $"显示器 {choices.Count + 1}　{rect.Width} × {rect.Height}", false, rect));
        }
        foreach (var w in windows.EnumerateArray())
            choices.Add(new(w.GetProperty("window_id").GetString()!, $"{w.GetProperty("title")}　·　{w.GetProperty("process_name")}",
                true, WorkspaceSurface.Rect(w.GetProperty("client_rect"))));
        areas = choices;
        filling = true;
        var old = selectedId;
        try
        {
            Scope.ItemsSource = choices;
            Scope.SelectedItem = old == null ? choices.FirstOrDefault() : choices.Find(a => a.Id == old);
        }
        finally { filling = false; }
        ApplyScope();
        if (old != null && Scope.SelectedItem == null) RunStatus.Text = "原操作区域已失效，请重新选择。";
    }

    private void ScopeChanged(object sender, SelectionChangedEventArgs e) { if (!filling) ApplyScope(); }
    private void ApplyScope()
    {
        var area = Scope.SelectedItem as Area;
        if (selectedId != area?.Id) { ClearSelection(); BackgroundMode.IsChecked = false; }
        selectedId = area?.Id;
        BackgroundMode.IsEnabled = area?.Window == true;
        UpdateControls();
    }
    private void OptionsChanged(object sender, SelectionChangedEventArgs e) => UpdateControls();
    private void UpdateControls()
    {
        if (StartButton == null) return;
        bool idle = execution == null && pending == null;
        Options.IsEnabled = idle;
        StartButton.IsEnabled = !idle || (Scope.SelectedItem != null && Script.SelectedItem != null);
        StartButton.Content = execution != null ? "停止" : pending != null ? "核对启动结果" : "开始";
        ResumeButton.Visibility = execution != null && status == "paused" ? Visibility.Visible : Visibility.Collapsed;
        Count.IsEnabled = Mode.SelectedIndex == 1;
    }

    private async void RefreshWindows(object s, RoutedEventArgs e) => await session.Run(LoadAreas);
    private void ClearSelection() { selection = null; RegionDetails.Visibility = Visibility.Collapsed; }
    private void ClearRegion(object s, RoutedEventArgs e) => ClearSelection();

    private async void PickRegion(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (Scope.SelectedItem is not Area area) return;
        var before = area.Window ? (await WindowInfo(area)).GetProperty("client_rect") : default;
        var display = area.Window ? areas.Where(a => !a.Window).MaxBy(a => Intersection(a.Bounds, WorkspaceSurface.Rect(before))) : area;
        if (display == null) throw new InvalidOperationException("找不到可用的显示器。");
        var picked = await surface.Select(display.Id);
        if (picked == null) return;
        try
        {
            var rect = WorkspaceSurface.Rect(picked.Value.GetProperty("region"));
            if (area.Window)
            {
                var bounds = WorkspaceSurface.Rect((await WindowInfo(area)).GetProperty("client_rect"));
                if (!bounds.Equals(WorkspaceSurface.Rect(before)) || Intersection(rect, bounds) != (long)rect.Width * rect.Height)
                    throw new InvalidOperationException("请在所选窗口的客户区内重新框选。");
                rect.X -= bounds.X; rect.Y -= bounds.Y;
            }
            selection = rect;
            RegionText.Text = $"已选区域　({rect.X}, {rect.Y})　{rect.Width} × {rect.Height} px";
            RegionDetails.Visibility = Visibility.Visible;
        }
        finally { await session.Call("release_image", new { image_id = picked.Value.GetProperty("image").GetProperty("image_id").GetString() }); }
    });

    private async Task<JsonElement> WindowInfo(Area area) =>
        (await session.Call("get_window_info", new { window_id = area.Id })).GetProperty("window");

    private static long Intersection(RectInt32 a, RectInt32 b) =>
        Math.Max(0L, Math.Min((long)a.X + a.Width, (long)b.X + b.Width) - Math.Max(a.X, b.X)) *
        Math.Max(0L, Math.Min((long)a.Y + a.Height, (long)b.Y + b.Height) - Math.Max(a.Y, b.Y));

    private async Task<Dictionary<string, object?>> Context(Area area, bool background, RectInt32? rect)
    {
        var bound = (await session.Call("get_bound_window", new { })).GetProperty("binding");
        string? binding = null;
        if (area.Window)
        {
            if (bound.ValueKind != JsonValueKind.Object || bound.GetProperty("window_id").GetString() != area.Id)
                bound = (await session.Call("bind_window", new { window_id = area.Id })).GetProperty("binding");
            binding = bound.GetProperty("binding_id").GetString();
        }
        else if (bound.ValueKind == JsonValueKind.Object) await session.Call("unbind_window", new { });
        var context = new Dictionary<string, object?> { ["allow_exit"] = false };
        string key = area.Window ? "binding_id" : "display_id";
        context[key] = binding ?? area.Id;
        if (rect is { } r)
            context["region"] = new Dictionary<string, object?> { ["space"] = area.Window ? "window_client_physical" : "screen_physical",
                [key] = context[key], ["x"] = r.X, ["y"] = r.Y, ["width"] = r.Width, ["height"] = r.Height };
        if (background && area.Window) context["window_mode"] = "background";
        return context;
    }

    internal async Task<object?> RecordingContext(XamlRoot root)
    {
        if (execution != null || pending != null) throw new InvalidOperationException("请先在开始工作页结束当前执行。");
        await LoadAreas();
        var picker = new ComboBox { Header = "录制范围", ItemsSource = areas, SelectedItem = Scope.SelectedItem,
            HorizontalAlignment = HorizontalAlignment.Stretch };
        var useRegion = new CheckBox { Content = "使用开始工作页的选区", IsChecked = selection.HasValue, IsEnabled = selection.HasValue };
        picker.SelectionChanged += (_, _) => {
            useRegion.IsEnabled = selection.HasValue && picker.SelectedItem == Scope.SelectedItem;
            useRegion.IsChecked = useRegion.IsEnabled;
        };
        var panel = new StackPanel { Spacing = 10 };
        panel.Children.Add(picker); panel.Children.Add(useRegion);
        panel.Children.Add(new TextBlock { Text = "完成操作后返回此页，点击「结束并编辑」。", TextWrapping = TextWrapping.Wrap });
        var dialog = new ContentDialog { XamlRoot = root, Title = "录制脚本", Content = panel,
            PrimaryButtonText = "开始录制", CloseButtonText = "取消" };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary || picker.SelectedItem is not Area area) return null;
        return await Context(area, false, useRegion.IsChecked == true ? selection : null);
    }

    private async Task<bool> ChooseOcrModel(long revision)
    {
        var models = (await session.Call("list_ocr_models", new { })).GetProperty("pipelines");
        var choices = models.EnumerateArray()
            .Where(row => row.GetProperty("state").GetString() is "installed" or "loading" or "ready")
            .Select(row => new CatalogItem(row.GetProperty("model_id").GetString(),
                $"{string.Join(" / ", row.GetProperty("languages").EnumerateArray())}　·　{row.GetProperty("model_id").GetString()}")).ToList();
        if (choices.Count == 0) throw new InvalidOperationException("请先在「设置」的离线 OCR 模型库中导入模型。");
        var picker = new ComboBox { Header = "默认识别模型", ItemsSource = choices, SelectedIndex = 0, HorizontalAlignment = HorizontalAlignment.Stretch };
        var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = "选择 OCR 模型", Content = picker,
            PrimaryButtonText = "保存并继续", CloseButtonText = "取消" };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary || picker.SelectedItem is not CatalogItem model) return false;
        await session.Host!.UpdateConfigAsync(revision, JsonSerializer.SerializeToElement(new { recognition = new { ocr_model_id = model.Id } }));
        return true;
    }

    private async Task<Dictionary<string, object?>?> MakeRequest()
    {
        if (Scope.SelectedItem is not Area area || Script.SelectedItem is not ScriptRow script)
            throw new InvalidOperationException("请选择操作区域和脚本。");
        bool repeat = Mode.SelectedIndex == 1;
        if (repeat && (!double.IsFinite(Count.Value) || Count.Value < 1 || Count.Value != Math.Truncate(Count.Value)))
            throw new InvalidOperationException("运行次数必须是正整数。");
        var args = JsonSerializer.Deserialize<JsonElement>(string.IsNullOrWhiteSpace(Arguments.Text) ? "{}" : Arguments.Text);
        if (args.ValueKind != JsonValueKind.Object) throw new InvalidOperationException("脚本参数需要是 JSON 对象。");
        var request = await Context(area, BackgroundMode.IsChecked == true, selection);
        var source = (await session.Call("load_script", new { script_id = script.Id })).GetProperty("source").GetString()!;
        var cfg = await session.Host!.GetConfigAsync();
        var steps = ScriptAssembly.Read(source);
        bool export = steps.Any(step => step.Source == ScriptAssembly.ExportSource);
        if (cfg.Recognition.OcrModelId == null && (export || steps.Any(step => step.Source == ScriptAssembly.ReadOcrSource))
            && !await ChooseOcrModel(cfg.Revision)) { RunStatus.Text = "已取消选择 OCR 模型。"; return null; }
        request["script_id"] = script.Id;
        request["args"] = args;
        request["mode"] = repeat ? "count" : "once";
        if (repeat) request["iterations"] = (int)Count.Value;
        request["debug"] = false;
        request["frequency_hz"] = cfg.Recognition.DefaultFrequencyHz;
        request["total_timeout_ms"] = cfg.Execution.TotalTimeoutMs;
        request["client_run_id"] = Guid.NewGuid().ToString();
        if (OutputMode.SelectedIndex > 0 || export)
        {
            var picked = await session.Host.CallAsync(new { op = "pick_text_output", session_id = session.Host.Events.Session,
                mode = OutputMode.SelectedIndex == 2 ? "overwrite" : "append" });
            var token = picked.GetProperty("output_token").GetString();
            if (token == null) { RunStatus.Text = "已取消选择 TXT 文件。"; return null; }
            request["output_token"] = token;
        }
        runName = script.Name;
        return request;
    }

    private async void StartOrStop(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (execution != null) { await session.Call("stop_script", new { execution_id = execution }); return; }
        pending ??= await MakeRequest();
        if (pending == null) return;
        try
        {
            var accepted = await session.Call("execute_script", pending);
            execution = accepted.GetProperty("execution_id").GetString();
            pending = null;
            RunStatus.Text = $"「{runName}」正在启动";
            session.Log(RunStatus.Text);
            await session.Host!.CallAsync(new { op = "select_execution", session_id = session.Host.Events.Session, execution_id = execution });
            var state = await session.Call("get_script_state", new { execution_id = accepted.GetProperty("execution_id").GetString() });
            ApplyState(state.GetProperty("state"));
        }
        catch (HostException ex) when (ex.Error.Code != "INTERNAL_ERROR")
        {
            var rejected = pending;
            pending = null;
            if (rejected?.GetValueOrDefault("output_token") is string token)
                await session.Host!.CallAsync(new { op = "discard_text_output", session_id = session.Host.Events.Session, output_token = token });
            throw;
        }
        finally { UpdateControls(); }
    });

    private async void Resume(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (execution != null) await session.Call("resume_script", new { execution_id = execution });
    });

    internal void ApplyState(JsonElement state)
    {
        if (execution == null || execution != state.GetProperty("execution_id").GetString()) return;
        status = state.GetProperty("status").GetString();
        RunStatus.Text = $"「{runName}」{ScriptRow.StatusText(status)}";
        if (status is "completed" or "failed" or "cancelled")
        {
            if (state.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.Object)
                RunStatus.Text += $"　{error.GetProperty("message")}";
            else if (status == "failed" && state.TryGetProperty("reason", out var reason) && reason.ValueKind == JsonValueKind.String)
                RunStatus.Text += reason.GetString() == "MODEL_UNAVAILABLE" ? "　请检查「设置」中的离线 OCR 模型及识别模型 ID。" : $"　{reason.GetString()}";
            execution = null;
            session.Log(RunStatus.Text);
        }
        UpdateControls();
    }

    internal void Reset()
    {
        execution = runName = status = selectedId = null;
        pending = null; RunStatus.Text = ""; ClearSelection(); BackgroundMode.IsChecked = false;
        UpdateControls();
    }
}
