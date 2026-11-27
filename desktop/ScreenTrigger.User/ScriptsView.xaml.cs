using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;

namespace ScreenTrigger.User;

public sealed partial class ScriptsView : UserControl
{
    private readonly UserSession session;
    private readonly WorkView work;
    private ScriptRow? editing;
    private List<CatalogItem> targets = [];
    private string originalName = "", originalSource = "", luaSource = "", displayedLua = "";
    private bool loading, recordingDraft;
    private string? recording;
    private readonly DispatcherTimer recordingTimer = new() { Interval = TimeSpan.FromSeconds(1) };
    private bool pollingRecording;
    internal IReadOnlyList<ScriptRow> Rows { get; private set; } = [];

    internal ScriptsView(UserSession session, WorkView work)
    {
        InitializeComponent();
        this.session = session; this.work = work;
        recordingTimer.Tick += PollRecording;
        Assembly.Run = session.Run;
        Assembly.Filter = () => (GroupFilter.SelectedItem as CatalogItem)?.Id;
        Assembly.OpenLua = () => ModeSwitch.IsOn = true;
    }

    internal async Task Reload()
    {
        Rows = (await session.List("list_scripts")).Select(doc => new ScriptRow(doc)).ToList();
        ScriptList.ItemsSource = Rows;
        EmptyLibrary.Visibility = Rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }
    internal string? NameForId(string id) => Rows.FirstOrDefault(s => s.Id == id)?.Name;

    private async Task OpenEditor(ScriptRow? row, string text, bool recorded = false)
    {
        var library = (await session.Call("list_groups", new { })).GetProperty("groups");
        var groups = new List<CatalogItem> { new("*", "全部分组"), new(null, "未分组") };
        groups.AddRange(library.EnumerateArray().Select(g => new CatalogItem(g.GetProperty("id").GetString(),
            new string('　', g.GetProperty("depth").GetInt32()) + g.GetProperty("name").GetString())));
        targets = (await session.List("list_targets")).Select(t => new CatalogItem(t.GetProperty("id").GetString(),
            $"#{t.GetProperty("sample_no")}　{t.GetProperty("name")}", t)).ToList();
        loading = true;
        try
        {
            GroupFilter.ItemsSource = groups; GroupFilter.SelectedIndex = 0;
            editing = row;
            recordingDraft = recorded;
            ScriptName.Text = originalName = row?.Name ?? (recorded ? "录制脚本" : "");
            originalSource = text;
            Assembly.LoadSource(text, targets);
            SetLua(text);
            ModeSwitch.IsOn = recorded || Assembly.HasCustomLua;
            ShowMode();
            EditorTitle.Text = row == null ? "新建脚本" : "编辑脚本";
            Library.Visibility = Visibility.Collapsed; Editor.Visibility = Visibility.Visible;
        }
        finally { loading = false; }
        DispatcherQueue.TryEnqueue(() => ScriptName.Focus(FocusState.Programmatic));
    }

    private void EditorSized(object sender, SizeChangedEventArgs e)
    {
        bool narrow = e.NewSize.Width < 680;
        Grid.SetColumnSpan(ScriptName, narrow ? 3 : 1);
        Grid.SetRow(ModeSwitch, narrow ? 1 : 0); Grid.SetColumn(ModeSwitch, narrow ? 0 : 1);
        Grid.SetRow(LibraryPicker, narrow ? 1 : 0); Grid.SetColumn(LibraryPicker, narrow ? 1 : 2);
        ModeSwitch.HorizontalAlignment = HorizontalAlignment.Left;
    }

    private void SetLua(string value)
    {
        luaSource = value; Source.Text = value; displayedLua = Source.Text;
    }
    private string LuaText => Source.Text == displayedLua ? luaSource : Source.Text.ReplaceLineEndings("\n");
    private string Draft => ModeSwitch.IsOn ? LuaText : Assembly.Source;
    private void SwitchMode(object s, RoutedEventArgs e)
    {
        if (loading || Assembly == null) return;
        if (ModeSwitch.IsOn) SetLua(Assembly.Source); else Assembly.LoadSource(LuaText, targets);
        ShowMode();
    }
    private void ShowMode()
    {
        LuaPanel.Visibility = ModeSwitch.IsOn ? Visibility.Visible : Visibility.Collapsed;
        Assembly.Visibility = ModeSwitch.IsOn ? Visibility.Collapsed : Visibility.Visible;
    }
    private async void CheckSyntax(object s, RoutedEventArgs e) => await session.Run(async () => {
        var result = await session.Call("validate_script", new { source = LuaText });
        var diagnostics = result.GetProperty("diagnostics").EnumerateArray().ToList();
        SyntaxResult.Text = diagnostics.Count == 0 ? "语法检查通过" : string.Join("\n",
            diagnostics.Select(d => $"第 {d.GetProperty("line")} 行 · {d.GetProperty("message")}"));
    });
    private void ListLuaTargets(object sender, object e)
    {
        var menu = (MenuFlyout)sender;
        menu.Items.Clear();
        string? group = Assembly.Filter();
        foreach (var target in targets.Where(t => group == "*" || t.Document.GetProperty("group_id").GetString() == group))
        {
            var item = new MenuFlyoutItem { Text = target.Label };
            item.Click += (_, _) => {
                Source.SelectedText = new StepField("目标", FieldKind.Target).Literal(target.Id!);
                Source.Focus(FocusState.Programmatic);
            };
            menu.Items.Add(item);
        }
        if (menu.Items.Count == 0) menu.Items.Add(new MenuFlyoutItem { Text = "所选分组暂无目标", IsEnabled = false });
    }
    private void SourceChanged(object s, TextChangedEventArgs e) { if (SyntaxResult != null) SyntaxResult.Text = ""; }
    private async void NewScript(object s, RoutedEventArgs e) => await session.Run(() => OpenEditor(null, ""));
    private async void EditScript(object s, RoutedEventArgs e)
    {
        if (((Button)s).DataContext is not ScriptRow row) return;
        await session.Run(async () => {
            var loaded = await session.Call("load_script", new { script_id = row.Id });
            await OpenEditor(new(loaded.GetProperty("script")), loaded.GetProperty("source").GetString()!);
        });
    }

    private void ShowLibrary()
    {
        editing = null; recordingDraft = false;
        Editor.Visibility = Visibility.Collapsed; Library.Visibility = Visibility.Visible;
        DispatcherQueue.TryEnqueue(() => { if (IsLoaded && Library.Visibility == Visibility.Visible) NewButton.Focus(FocusState.Programmatic); });
    }

    private async Task SaveDraft()
    {
        if (string.IsNullOrWhiteSpace(ScriptName.Text)) throw new InvalidOperationException("请填写脚本名称。");
        string source = Draft;
        var draft = new Dictionary<string, object?> { ["name"] = ScriptName.Text.Trim(), ["source"] = source,
            ["parameters"] = editing == null ? Array.Empty<object>() : editing.Document.GetProperty("parameters") };
        if (editing != null) { draft["script_id"] = editing.Id; draft["expected_revision"] = editing.Revision; }
        var result = await session.Call("save_script", draft);
        editing = new(result.GetProperty("script")); originalName = ScriptName.Text; originalSource = source;
        recordingDraft = false;
        session.Log($"已保存脚本「{editing.Name}」");
        await Reload();
    }
    private async void Save(object s, RoutedEventArgs e) => await session.Run(async () => { await SaveDraft(); ShowLibrary(); });
    private async void Back(object s, RoutedEventArgs e) => await session.Run(async () => { await LeaveEditor(); });

    internal async Task<bool> LeaveEditor()
    {
        if (recording != null)
        {
            var prompt = new ContentDialog { XamlRoot = XamlRoot, Title = "取消本次录制？", PrimaryButtonText = "取消录制并离开", CloseButtonText = "留在此页" };
            if (await prompt.ShowAsync() != ContentDialogResult.Primary) return false;
            await DiscardRecording();
        }
        if (Editor.Visibility != Visibility.Visible) return true;
        if (recordingDraft || originalName != ScriptName.Text || originalSource != Draft)
        {
            var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = "是否保存并退出？", Content = "脚本有尚未保存的修改。",
                PrimaryButtonText = "保存并退出", SecondaryButtonText = "放弃修改", CloseButtonText = "继续编辑", DefaultButton = ContentDialogButton.Primary };
            var choice = await dialog.ShowAsync();
            if (choice == ContentDialogResult.None) return false;
            if (choice == ContentDialogResult.Primary) await SaveDraft();
        }
        ShowLibrary();
        return true;
    }
    private async void DeleteScript(object s, RoutedEventArgs e)
    {
        if (((Button)s).DataContext is not ScriptRow row) return;
        await session.Run(async () => {
            await session.Call("delete_script", new { script_id = row.Id, expected_revision = row.Revision });
            session.Log($"已删除脚本「{row.Name}」"); await Reload();
        });
    }

    private void ShowRecording(bool active)
    {
        RecordButton.IsEnabled = NewButton.IsEnabled = ScriptList.IsEnabled = !active;
        RecordingPanel.Visibility = active ? Visibility.Visible : Visibility.Collapsed;
        ContinueRecording.Visibility = Visibility.Collapsed;
        if (active) recordingTimer.Start(); else recordingTimer.Stop();
    }
    private async void PollRecording(object? sender, object args)
    {
        if (recording is not string id || pollingRecording || session.Busy) return;
        pollingRecording = true;
        try
        {
            var state = await session.Call("get_recording_state", new { recording_id = id });
            if (recording != id || session.Busy) return;
            var status = state.GetProperty("status").GetString();
            ContinueRecording.Visibility = status == "paused" ? Visibility.Visible : Visibility.Collapsed;
            var label = status switch { "paused" => "已暂停", "finished" => "录制已结束", _ => "正在录制" };
            RecordingStatus.Text = $"{label} · {state.GetProperty("action_count")} 个动作";
        }
        catch (HostException ex) when (ex.Error.Code is "INVALID_STATE" or "INVALID_PARAMS")
        {
            if (recording == id && !session.Busy) { EndEngineSession(); session.Error(ex); }
        }
        catch (Exception e) { if (recording == id) RecordingStatus.Text = e.Message; }
        finally { pollingRecording = false; }
    }
    private async void ResumeRecording(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (recording != null) await session.Call("resume_recording", new { recording_id = recording });
    });
    private async void StartRecording(object s, RoutedEventArgs e) => await session.Run(async () => {
        var context = await work.RecordingContext(XamlRoot);
        if (context == null) return;
        recording = Guid.NewGuid().ToString();
        try
        {
            await session.Call("start_recording", new { recording_id = recording, context });
            RecordingStatus.Text = "正在录制";
        }
        catch (HostException ex) when (ex.Error.Code == "INTERNAL_ERROR")
        { RecordingStatus.Text = "启动结果待核对，请结束或取消本次录制。"; }
        catch { recording = null; throw; }
        ShowRecording(true);
    });
    private async void FinishRecording(object s, RoutedEventArgs e) => await session.Run(async () => {
        if (recording is not string id) return;
        var draft = await session.Call("finish_recording", new { recording_id = id });
        var compiled = await session.Call("compile_recording", new { scope = draft.GetProperty("scope"), actions = draft.GetProperty("actions"),
            interrupted = draft.GetProperty("interrupted") });
        int count = compiled.GetProperty("action_count").GetInt32(), omitted = compiled.GetProperty("omitted").GetArrayLength();
        if (count == 0)
        {
            await ReleaseRecording(id);
            throw new InvalidOperationException($"本次录制没有完整动作（{omitted} 处中止）。");
        }
        if (omitted > 0)
        {
            var dialog = new ContentDialog { XamlRoot = XamlRoot, Title = "录制包含中止的动作",
                Content = $"已录到 {count} 个完整动作，有 {omitted} 处中止。", PrimaryButtonText = "编辑完整动作", CloseButtonText = "放弃录制" };
            if (await dialog.ShowAsync() != ContentDialogResult.Primary) { await ReleaseRecording(id); return; }
        }
        await OpenEditor(null, compiled.GetProperty("source").GetString()!, recorded: true);
        await ReleaseRecording(id);
    });
    private async void CancelRecording(object s, RoutedEventArgs e) => await session.Run(DiscardRecording);
    private async Task DiscardRecording()
    {
        if (recording is not string id) return;
        var state = await session.Call("get_recording_state", new { recording_id = id });
        if (state.GetProperty("status").GetString() is "recording" or "paused")
            await session.Call("cancel_recording", new { recording_id = id });
        await ReleaseRecording(id);
    }
    private async Task ReleaseRecording(string id)
    {
        await session.Call("release_recording_draft", new { recording_id = id });
        recording = null; ShowRecording(false);
    }
    internal void EndEngineSession() { recording = null; ShowRecording(false); }
}
