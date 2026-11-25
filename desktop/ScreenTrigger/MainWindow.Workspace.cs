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
    private (int X, int Y) imageOrigin;
    private Control? returnFocus;
    private readonly Dictionary<string, UserControl> pages = [];
    private object? pendingDraft;
    private string? draftImageId;
    private bool luaAvailable, updatingRuns;
    private readonly Dictionary<string, string> running = [];

    private void Navigate(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (EnginePage == null || args.SelectedItem is not NavigationViewItem item) return;
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
                "scripts" => new Scripts.ScriptsPage(host),
                "run" => new RunContext.RunContextPage(host, luaAvailable),
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
        Navigation.SelectedItem = RecognitionNav;
        if (old != null && old != draftImageId && host != null)
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
        captureAvailable = ready && capabilities.ValueKind == JsonValueKind.Array && capabilities.EnumerateArray().Any(
            c => c.GetProperty("id").GetString() == "capture" && c.GetProperty("state").GetString() == "available");
        if (workspaceSession != host?.Events.Session || !ready)
        {
            workspaceSession = ready ? host?.Events.Session : null;
            running.Clear();
            ActiveExecution.ItemsSource = null;
            RunBar.Visibility = Visibility.Collapsed;
            pages.Clear(); pendingDraft = null; draftImageId = null; BusinessPage.Content = null;
            image = null;
            SelectedImage.Source = null;
            ImageStatus.Text = "";
            if (overlay != null) { overlay.CloseFromHost(); overlay = null; AppWindow.Show(); Activate(); }
        }
        if (workspaceReady && BusinessPage.Visibility == Visibility.Visible && BusinessPage.Content == null &&
            Navigation.SelectedItem is NavigationViewItem item && item.Tag is string tag) ShowBusinessPage(tag);
        UpdateImageButtons();
    }

    private void UpdateImageButtons()
    {
        SelectButton.IsEnabled = workspaceReady && captureAvailable && !imageBusy && overlay == null;
        ImportButton.IsEnabled = workspaceReady && !imageBusy && overlay == null;
    }

    private async Task UpdateRun(JsonElement payload)
    {
        var id = payload.GetProperty("execution_id").GetString()!;
        var status = payload.GetProperty("status").GetString()!;
        var old = (ActiveExecution.SelectedItem as CatalogItem)?.Id;
        if (status is "completed" or "cancelled" or "failed") running.Remove(id);
        else running[id] = status;
        var display = status switch {
            "starting" => "正在启动", "running" => "运行中", "paused" => "已暂停", "stopping" => "正在停止",
            "cancelled" => "已停止", "completed" => "已完成", "failed" => "失败", _ => status
        };
        RunStatus.Text = $"{id[..Math.Min(8, id.Length)]}　{display}";
        RunBar.Visibility = Visibility.Visible;
        var rows = running.Keys.Select(key => new CatalogItem(key, key[..Math.Min(8, key.Length)])).ToList();
        updatingRuns = true;
        ActiveExecution.ItemsSource = rows;
        ActiveExecution.SelectedItem = rows.Find(e => e.Id == old) ?? rows.FirstOrDefault();
        updatingRuns = false;
        await PublishSelection();
        if (status == "failed" && host != null)
        {
            var reply = await host.RequestAsync("get_script_state", new { execution_id = id });
            var error = reply.GetProperty("state").GetProperty("error");
            if (error.ValueKind == JsonValueKind.Object && RunStatus.Text.StartsWith(id[..Math.Min(8, id.Length)]))
                RunStatus.Text = $"{display} · {error.GetProperty("code")} · {error.GetProperty("message")}";
        }
    }

    private async Task PublishSelection()
    {
        var id = (ActiveExecution.SelectedItem as CatalogItem)?.Id;
        var state = id == null ? null : running.GetValueOrDefault(id);
        PauseExecutionButton.IsEnabled = state is "paused" or "running";
        PauseExecutionButton.Content = state == "paused" ? "恢复" : "暂停";
        StopExecutionButton.IsEnabled = state is "starting" or "running" or "paused";
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
                    display.GetProperty("id").GetString()!, bounds, message => ImageStatus.Text = message);
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
            await SetImageAsync(result.GetProperty("image"), session);
        }
        catch (Exception ex) { ImageStatus.Text = ex.Message; }
        finally { imageBusy = false; UpdateImageButtons(); if (!closing) ImportButton.Focus(FocusState.Programmatic); }
    }

    private async Task SetImageAsync(JsonElement value, string? session, int x = 0, int y = 0)
    {
        if (host == null || host.Events.Session != session || closing) return;
        var next = WireContracts.Read<ImageRef>(value, "imageRef");
        var previous = image;
        image = next;
        imageOrigin = (x, y);
        SelectedImage.Source = null;
        ImageStatus.Text = $"{next.Width} × {next.Height} px · {next.Format.ToUpperInvariant()}";
        if (previous != null && previous.ImageId != next.ImageId && previous.ImageId != draftImageId)
            await host.RequestAsync("release_image", new { image_id = previous.ImageId });
        var preview = await host.RequestAsync("get_image_preview", new { image_id = next.ImageId, max_edge = 600 });
        var bitmap = await ImagePicker.DecodePreviewAsync(preview.GetProperty("base64").GetString()!);
        if (!closing && image == next && host.Events.Session == session) SelectedImage.Source = bitmap;
    }
}
