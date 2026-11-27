using System.Text.Json;
using Microsoft.UI.Xaml;
using ScreenTrigger.Host;
using ScreenTrigger.Import;
using ScreenTrigger.Selection;
using Windows.Graphics;
using WinRT.Interop;

namespace ScreenTrigger.User;

internal sealed class WorkspaceSurface(Window owner, UserSession session)
{
    private OverlayWindow? overlay;
    private TaskCompletionSource<JsonElement?>? selection;
    private string? requestedId;
    private EngineEvent? early;
    private bool cancelOnStart;
    private bool pointMode;
    private string? pickedOutputPath;
    private int previewHz = 10;

    internal void SetPreviewFrequency(int hz) { previewHz = hz; overlay?.SetPreviewFrequency(hz); }

    internal async Task<object?> Handle(JsonElement request, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var app = owner.AppWindow;
        var hwnd = WindowNative.GetWindowHandle(owner);
        switch (request.GetProperty("op").GetString())
        {
            case "rect": return new { x = app.Position.X, y = app.Position.Y, width = app.Size.Width, height = app.Size.Height };
            case "visible": return app.IsVisible;
            case "set_visible":
                if (request.GetProperty("visible").GetBoolean()) { app.Show(); owner.Activate(); } else app.Hide();
                return true;
            case "pick_image": return await ImagePicker.PickAsync(hwnd, token);
            case "pick_text_output": return pickedOutputPath = TextOutputPicker.Pick(hwnd, request.GetProperty("mode").GetString()!, token);
            case "open_overlay":
                if (overlay != null) throw new InvalidOperationException("已有活动框选。");
                var display = request.GetProperty("display");
                overlay = new(session.Host!, request.GetProperty("selection_id").GetString()!, display.GetProperty("id").GetString()!,
                    Rect(display.GetProperty("rect")), previewHz, message => session.Error(new InvalidOperationException(message)),
                    request.TryGetProperty("mode", out var mode) && mode.GetString() == "point");
                try { await overlay.OpenAsync(token); }
                catch { overlay?.CloseFromHost(); overlay = null; throw; }
                return true;
            case "prepare_capture":
                if (overlay == null) throw new InvalidOperationException("框选窗口已关闭。");
                await overlay.PrepareCaptureAsync(token);
                return true;
            case "end_capture": overlay?.EndCapture(); return true;
            case "close_overlay":
                if (overlay?.Id == request.GetProperty("selection_id").GetString()) { overlay?.CloseFromHost(); overlay = null; }
                return true;
            default: throw new InvalidOperationException("未知窗口操作。");
        }
    }

    internal static RectInt32 Rect(JsonElement rect) => new(rect.GetProperty("x").GetInt32(), rect.GetProperty("y").GetInt32(),
        rect.GetProperty("width").GetInt32(), rect.GetProperty("height").GetInt32());

    internal async Task<(string Token, string Path)?> PickOutput(string mode)
    {
        var host = session.Host!;
        var active = host.Events.Session;
        pickedOutputPath = null;
        try
        {
            var data = await host.CallAsync(new { op = "pick_text_output", session_id = active, mode });
            if (host.Events.Session != active) throw new InvalidOperationException("引擎会话已结束，请重新选择 TXT。");
            return data.GetProperty("output_token").GetString() is string token
                ? (token, pickedOutputPath ?? data.GetProperty("name").GetString()!) : null;
        }
        finally { pickedOutputPath = null; }
    }

    internal async Task<JsonElement?> Select(string? displayId = null, bool point = false, string? bindingId = null)
    {
        if (selection != null) throw new InvalidOperationException("请先结束当前框选。");
        var pending = selection = new(TaskCreationOptions.RunContinuationsAsynchronously);
        pointMode = point;
        try
        {
            var result = point
                ? await session.Call("start_point_pick", new { display_id = displayId, binding_id = bindingId })
                : await session.Call("start_selection_mode", displayId == null ? new { } : new { display_id = displayId });
            requestedId = result.GetProperty(point ? "pick_id" : "selection_id").GetString();
            if (early != null) Receive(early);
            if (cancelOnStart && !pending.Task.IsCompleted)
                await CancelActive();
            return await pending.Task;
        }
        finally { selection = null; requestedId = null; early = null; cancelOnStart = false; }
    }

    internal void Receive(EngineEvent evt)
    {
        if (evt.Name is not ("selection_completed" or "selection_cancelled" or "point_pick_completed" or "point_pick_cancelled")) return;
        bool point = evt.Name.StartsWith("point_pick_", StringComparison.Ordinal);
        if (selection != null && point == pointMode && requestedId == null) { early = evt; return; }
        if (selection != null && point == pointMode && evt.Payload.GetProperty(point ? "pick_id" : "selection_id").GetString() == requestedId)
        {
            selection.TrySetResult(evt.Name.EndsWith("_completed", StringComparison.Ordinal) ? evt.Payload : null);
            return;
        }
        if (evt.Name == "selection_completed") _ = ReleaseOrphan(evt);
    }

    private async Task ReleaseOrphan(EngineEvent evt)
    {
        try { await session.Host!.RequestCheckedAsync(evt.Session, "release_image", new { image_id = evt.Payload.GetProperty("image").GetProperty("image_id").GetString() }); }
        catch (Exception e) { if (session.Host?.Events.Session == evt.Session) session.Error(e); }
    }

    internal async Task CancelForClose()
    {
        if (selection == null || selection.Task.IsCompleted) return;
        cancelOnStart = true;
        if (requestedId != null) await CancelActive();
    }
    private Task<JsonElement> CancelActive() => pointMode
        ? session.Call("cancel_point_pick", new { pick_id = requestedId })
        : session.Call("stop_selection_mode", new { selection_id = requestedId });

    internal void Reset()
    {
        selection?.TrySetResult(null);
        if (overlay != null) { overlay.CloseFromHost(); overlay = null; owner.AppWindow.Show(); owner.Activate(); }
    }
}
