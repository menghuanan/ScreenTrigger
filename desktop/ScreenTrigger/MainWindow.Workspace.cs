using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Contracts;
using ScreenTrigger.Import;
using ScreenTrigger.Selection;
using SelectionProbe;
using Windows.Graphics;
using WinRT.Interop;

namespace ScreenTrigger;

public sealed partial class MainWindow
{
    private OverlayWindow? overlay;
    private bool imageBusy, workspaceReady, captureAvailable;
    private string? workspaceSession;
    private ImageRef? image;
    private ImageRef? importedSource;
    private int imageVersion;
    private (int X, int Y) imageOrigin;
    private Control? returnFocus;
    private readonly Dictionary<string, UserControl> pages = [];
    private object? pendingDraft;
    private string? draftImageId;
    private bool luaAvailable, updatingRuns, workspaceOcr;
    private readonly Dictionary<string, string> running = [];
    private readonly Dictionary<string, long> runSequences = [];
    private readonly Queue<string> terminalSequenceOrder = new();
    private NavigationViewItem? visibleItem;
    private bool selectingPage, resolvingDraft;
    private int previewHz = 10;
    private long displayConfigRevision = -1;

    private async void Navigate(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (EnginePage == null || selectingPage || args.SelectedItem is not NavigationViewItem item) return;
        if (visibleItem != null && (resolvingDraft || closing || BusinessPage.Visibility == Visibility.Visible &&
            BusinessPage.Content is IWorkspaceDraft draft && (draft.HasChanges || BusinessPage.Content is Control { IsEnabled: false })))
            SelectMenuItem(visibleItem);
        await ChangePage(item);
    }

    private void SelectMenuItem(NavigationViewItem item)
    {
        if (ReferenceEquals(Navigation.SelectedItem, item)) return;
        selectingPage = true;
        try { Navigation.SelectedItem = item; }
        finally { selectingPage = false; }
    }

    private async Task<bool> ChangePage(NavigationViewItem item)
    {
        if (resolvingDraft || closing) return false;
        if (visibleItem != item && BusinessPage.Visibility == Visibility.Visible && BusinessPage.Content is IWorkspaceDraft editor)
        {
            resolvingDraft = true;
            try
            {
                await editor.WhenIdle;
                if (editor.HasChanges && !await editor.ResolveChanges()) return false;
            }
            finally { resolvingDraft = false; }
        }
        DisplayPage(item);
        return true;
    }

    private void DisplayPage(NavigationViewItem item)
    {
        visibleItem = item;
        SelectMenuItem(item);
        string? tag = item.Tag as string;
        EnginePage.Visibility = tag == "engine" ? Visibility.Visible : Visibility.Collapsed;
        ImagePage.Visibility = tag == "images" ? Visibility.Visible : Visibility.Collapsed;
        BusinessPage.Visibility = tag is "engine" or "images" ? Visibility.Collapsed : Visibility.Visible;
        if (BusinessPage.Visibility == Visibility.Visible && tag != null) ShowBusinessPage(tag);
    }

    private void ShowBusinessPage(string tag)
    {
        if (!workspaceReady || host == null) { BusinessPage.Content = null; return; }
        if (!pages.TryGetValue(tag, out var page))
        {
            page = tag switch {
                "targets" => new Targets.TargetsPage(host, () => image, OpenDraft),
                "history" => new History.HistoryPage(host),
                "settings" => new Settings.SettingsPage(host, ApplyDisplayConfig),
                "scripts" => new Scripts.ScriptsPage(host),
                "run" or "monitor" => new RunContext.RunContextPage(host, luaAvailable, tag == "monitor", workspaceOcr),
                "recognition" => new Recognition.RecognitionPage(host, () => image, () => imageOrigin, () => pendingDraft),
                _ => null
            };
            if (page == null) return;
            pages[tag] = page;
        }
        BusinessPage.Content = page;
    }

    private async void OpenDraft(object value)
    {
        string? old = draftImageId;
        pendingDraft = value; draftImageId = image?.ImageId;
        pages.Remove("recognition");
        // Testing a draft explicitly opens its preview while retaining the editor.
        DisplayPage(RecognitionNav);
        if (old != null && old != draftImageId && old != importedSource?.ImageId && host != null)
        {
            try { await host.RequestAsync("release_image", new { image_id = old }); }
            catch (Exception ex) { ShowError(ex); }
        }
    }

    private void SetWorkspaceAvailability(bool ready, JsonElement capabilities)
    {
        workspaceReady = ready && host?.Events.Session != null;
        luaAvailable = ready && capabilities.ValueKind == JsonValueKind.Array && capabilities.EnumerateArray().Any(
            c => c.GetProperty("id").GetString() == "lua_script" && c.GetProperty("state").GetString() == "available");
        workspaceOcr = ready && capabilities.ValueKind == JsonValueKind.Array && capabilities.EnumerateArray().Any(
            c => c.GetProperty("id").GetString() == "ocr" && c.GetProperty("state").GetString() == "available");
        captureAvailable = ready && capabilities.ValueKind == JsonValueKind.Array && capabilities.EnumerateArray().Any(
            c => c.GetProperty("id").GetString() == "capture" && c.GetProperty("state").GetString() == "available");
        if (workspaceSession != host?.Events.Session || !ready)
        {
            workspaceSession = ready ? host?.Events.Session : null;
            displayConfigRevision = -1;
            previewHz = 10;
            if (workspaceSession != null) _ = ReadDisplayConfig(workspaceSession);
            if (!ready && running.Count > 0)
                Notice("引擎已退出，活动执行已中断。可重新启动引擎并查看历史。");
            running.Clear(); runSequences.Clear(); terminalSequenceOrder.Clear();
            ActiveExecution.ItemsSource = null;
            RunBar.Visibility = Visibility.Collapsed;
            pages.Clear(); pendingDraft = null; draftImageId = null; BusinessPage.Content = null;
            imageVersion++;
            image = importedSource = null;
            SelectedImage.Source = null;
            ImageStatus.Text = "";
            if (overlay != null) { overlay.CloseFromHost(); overlay = null; AppWindow.Show(); Activate(); }
        }
        if (workspaceReady && BusinessPage.Visibility == Visibility.Visible && BusinessPage.Content == null &&
            Navigation.SelectedItem is NavigationViewItem item && item.Tag is string tag) ShowBusinessPage(tag);
        UpdateImageButtons();
    }

    private void ApplyDisplayConfig(JsonElement config)
    {
        long revision = config.GetProperty("revision").GetInt64();
        if (revision < displayConfigRevision) return;
        displayConfigRevision = revision;
        Navigation.RequestedTheme = config.GetProperty("general").GetProperty("theme").GetString() switch {
            "light" => ElementTheme.Light, "dark" => ElementTheme.Dark, _ => ElementTheme.Default
        };
        previewHz = config.GetProperty("capture").GetProperty("preview_hz").GetInt32();
        overlay?.SetPreviewFrequency(previewHz);
    }
    private void Notice(string text)
    {
        ServiceNotice.Text = text;
        ServiceNotice.Visibility = Visibility.Visible;
    }
    private async Task ReadDisplayConfig(string session)
    {
        try
        {
            var data = await host!.RequestAsync("get_config", new { });
            if (host.Events.Session != session) return;
            ApplyDisplayConfig(data.GetProperty("config"));
            if (data.TryGetProperty("recovery", out var restored))
                Notice($"配置损坏，已自动恢复备份修订 {restored.GetProperty("revision")}。");
        }
        catch (Exception ex) { if (host?.Events.Session == session) Notice(ex.Message); }
    }

    private void UpdateImageButtons()
    {
        SelectButton.IsEnabled = workspaceReady && captureAvailable && !imageBusy && overlay == null;
        ImportButton.IsEnabled = workspaceReady && !imageBusy && overlay == null;
        CropButton.IsEnabled = ImportButton.IsEnabled && importedSource != null && SelectedImage.Source != null;
        OriginalButton.Visibility = importedSource != null && importedSource != image ? Visibility.Visible : Visibility.Collapsed;
        OriginalButton.IsEnabled = ImportButton.IsEnabled;
    }

    private async Task UpdateRun(JsonElement payload)
    {
        var id = payload.GetProperty("execution_id").GetString()!;
        var status = payload.GetProperty("status").GetString()!;
        var old = (ActiveExecution.SelectedItem as CatalogItem)?.Id;
        if (status is "completed" or "cancelled" or "failed") running.Remove(id);
        else running[id] = status;
        var display = Status.RunFeedback.Name(status);
        RunStatus.Text = $"{id[..Math.Min(8, id.Length)]}　{display}";
        RunBar.Visibility = Visibility.Visible;
        var rows = running.Keys.Select(key => new CatalogItem(key, key[..Math.Min(8, key.Length)])).ToList();
        updatingRuns = true;
        ActiveExecution.ItemsSource = rows;
        ActiveExecution.SelectedItem = rows.Find(e => e.Id == old) ?? rows.FirstOrDefault();
        updatingRuns = false;
        await PublishSelection();
        if (status is ("failed" or "completed" or "cancelled") && host != null)
        {
            var reply = await host.RequestAsync("get_script_state", new { execution_id = id });
            if (RunStatus.Text.StartsWith(id[..Math.Min(8, id.Length)]))
                RunStatus.Text = Status.RunFeedback.State(reply.GetProperty("state"));
        }
    }

    private async Task PublishSelection()
    {
        var id = (ActiveExecution.SelectedItem as CatalogItem)?.Id;
        var state = id == null ? null : running.GetValueOrDefault(id);
        PauseExecutionButton.IsEnabled = state is "paused" or "running";
        PauseExecutionButton.Content = state == "paused" ? "恢复" : "暂停";
        InspectExecution.Visibility = state == "debug_paused" ? Visibility.Visible : Visibility.Collapsed;
        StopExecutionButton.IsEnabled = state is "starting" or "running" or "paused" or "debug_paused";
        if (host?.Events.Session is string session)
            await host.CallAsync(new { op = "select_execution", session_id = session, execution_id = id });
    }

    private async void SelectExecution(object sender, SelectionChangedEventArgs args)
    {
        if (updatingRuns || host == null || !workspaceReady) return;
        try { await PublishSelection(); } catch (Exception ex) { RunStatus.Text = ex.Message; }
    }

    private async Task ControlExecution(bool stop)
    {
        if (host == null || ActiveExecution.SelectedItem is not CatalogItem item || item.Id == null) return;
        var command = stop ? "stop_script" : running.GetValueOrDefault(item.Id) == "paused" ? "resume_script" : "pause_script";
        try { await host.RequestAsync(command, new { execution_id = item.Id }); }
        catch (Exception ex) { RunStatus.Text = ex.Message; }
    }
    private async void InspectPaused(object sender, RoutedEventArgs args)
    {
        if (ActiveExecution.SelectedItem is not CatalogItem item || item.Id == null) return;
        if (!await ChangePage(ScriptsNav)) return;
        if (BusinessPage.Content is Scripts.ScriptsPage editor)
            try { await editor.Inspect(item.Id); } catch (Exception ex) { RunStatus.Text = ex.Message; }
    }

    private async void ToggleExecution(object sender, RoutedEventArgs args) => await ControlExecution(false);
    private async void StopExecution(object sender, RoutedEventArgs args) => await ControlExecution(true);

    private async Task<object?> HandleWindowAction(JsonElement request, CancellationToken token)
    {
        nint hwnd = WindowNative.GetWindowHandle(this);
        switch (request.GetProperty("op").GetString())
        {
            case "rect":
                var pos = AppWindow.Position; var size = AppWindow.Size;
                return new { x = pos.X, y = pos.Y, width = size.Width, height = size.Height };
            case "visible": return ProbeDesktop.IsWindowVisible(hwnd);
            case "set_visible":
                if (request.GetProperty("visible").GetBoolean())
                {
                    AppWindow.Show(); Activate();
                    returnFocus?.Focus(FocusState.Programmatic);
                }
                else AppWindow.Hide();
                return true;
            case "open_overlay":
                if (overlay != null || host == null) throw new InvalidOperationException("已有活动框选。");
                var display = request.GetProperty("display");
                var rect = display.GetProperty("rect");
                var bounds = new RectInt32(rect.GetProperty("x").GetInt32(), rect.GetProperty("y").GetInt32(),
                    rect.GetProperty("width").GetInt32(), rect.GetProperty("height").GetInt32());
                overlay = new OverlayWindow(host, request.GetProperty("selection_id").GetString()!,
                    display.GetProperty("id").GetString()!, bounds, previewHz, message => ImageStatus.Text = message);
                try { await overlay.OpenAsync(token); }
                catch { overlay?.CloseFromHost(); overlay = null; throw; }
                UpdateImageButtons();
                return true;
            case "close_overlay":
                if (overlay is { } active && active.Id == request.GetProperty("selection_id").GetString())
                { active.CloseFromHost(); overlay = null; }
                UpdateImageButtons();
                return true;
            case "prepare_capture":
                if (overlay == null) throw new InvalidOperationException("框选窗已关闭。");
                await overlay.PrepareCaptureAsync(token);
                return true;
            case "end_capture": overlay?.EndCapture(); return true;
            case "pick_image":
                try { return await ImagePicker.PickAsync(hwnd, token); }
                finally { if (!closing) { Activate(); ImportButton.Focus(FocusState.Programmatic); } }
            default: throw new ArgumentException("未知窗口操作。");
        }
    }

    private async void StartSelection(object sender, RoutedEventArgs args)
    {
        if (host == null || imageBusy || !workspaceReady) return;
        returnFocus = SelectButton;
        imageBusy = true; UpdateImageButtons();
        try { await host.RequestAsync("start_selection_mode", new { }); }
        catch (Exception ex) { ImageStatus.Text = ex.Message; }
        finally { imageBusy = false; UpdateImageButtons(); }
    }

    private async void ImportImage(object sender, RoutedEventArgs args)
    {
        if (host == null || imageBusy || !workspaceReady) return;
        var session = host.Events.Session;
        imageBusy = true; UpdateImageButtons();
        try
        {
            var picked = await host.CallAsync(new { op = "pick_image", session_id = session });
            var token = picked.GetProperty("import_token").GetString();
            if (session != host.Events.Session) return;
            if (token == null) { ImageStatus.Text = "已取消导入。"; return; }
            var result = await host.RequestAsync("import_image", new { import_token = token });
            await SetImageAsync(WireContracts.Read<ImageRef>(result.GetProperty("image"), "imageRef"), session, imported: true);
        }
        catch (Exception ex) { ImageStatus.Text = ex.Message; }
        finally { imageBusy = false; UpdateImageButtons(); if (!closing) ImportButton.Focus(FocusState.Programmatic); }
    }

    private async void CropImage(object sender, RoutedEventArgs args)
    {
        if (imageBusy || host == null || importedSource == null || image == null || SelectedImage.Source == null) return;
        var session = host.Events.Session;
        var source = image;
        imageBusy = true; UpdateImageButtons();
        try
        {
            var dialog = new CropDialog(Navigation.XamlRoot, source, SelectedImage.Source);
            if (await dialog.ShowAsync() != ContentDialogResult.Primary || session != host.Events.Session || source != image) return;
            var result = await host.RequestAsync("crop_image", new { image_id = source.ImageId, x = dialog.X, y = dialog.Y,
                width = dialog.WidthPixels, height = dialog.HeightPixels });
            await SetImageAsync(WireContracts.Read<ImageRef>(result.GetProperty("image"), "imageRef"), session, keepOriginal: true);
        }
        catch (Exception ex) { ImageStatus.Text = ex.Message; }
        finally { imageBusy = false; UpdateImageButtons(); if (!closing) CropButton.Focus(FocusState.Programmatic); }
    }

    private async void RestoreOriginal(object sender, RoutedEventArgs args)
    {
        if (imageBusy || host == null || importedSource == null) return;
        imageBusy = true; UpdateImageButtons();
        try { await SetImageAsync(importedSource, host.Events.Session, keepOriginal: true); }
        catch (Exception ex) { ImageStatus.Text = ex.Message; }
        finally { imageBusy = false; UpdateImageButtons(); if (!closing) CropButton.Focus(FocusState.Programmatic); }
    }

    private async Task SetImageAsync(ImageRef next, string? session, int x = 0, int y = 0, bool imported = false, bool keepOriginal = false)
    {
        if (host == null || host.Events.Session != session || closing) return;
        int version = ++imageVersion;
        async Task ReleaseUnused(ImageRef? item)
        {
            if (item != null && host.Events.Session == session && item != image && item != importedSource && item.ImageId != draftImageId)
                await host.RequestAsync("release_image", new { image_id = item.ImageId });
        }
        try
        {
            var preview = await host.RequestAsync("get_image_preview", new { image_id = next.ImageId, max_edge = 600 });
            var bitmap = await ImagePicker.DecodePreviewAsync(preview.GetProperty("base64").GetString()!);
            if (closing || host.Events.Session != session || version != imageVersion) return;
            var previous = image;
            var previousOriginal = importedSource;
            image = next;
            importedSource = imported ? next : keepOriginal ? previousOriginal : null;
            imageOrigin = (x, y);
            SelectedImage.Source = bitmap;
            ImageStatus.Text = $"{next.Width} × {next.Height} px · {next.Format.ToUpperInvariant()}";
            UpdateImageButtons();
            await ReleaseUnused(previous);
            if (previousOriginal != previous) await ReleaseUnused(previousOriginal);
        }
        finally { await ReleaseUnused(next); }
    }
}
