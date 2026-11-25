using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using ScreenTrigger.Host;
using ScreenTrigger.Import;
using SelectionProbe;
using Windows.Foundation;
using Windows.Graphics;
using WinRT.Interop;

namespace ScreenTrigger.Selection;

public sealed partial class OverlayWindow : Window
{
    private readonly HostClient host;
    private readonly string session, display;
    private readonly RectInt32 bounds;
    private readonly Action<string> showError;
    private readonly DispatcherQueueTimer timer;
    private readonly TaskCompletionSource loaded = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly nint hwnd;
    private RectInt32 region;
    private Point anchor;
    private uint? pointer;
    private double scale;
    private bool closed, reading, finishing, clearVisuals, active, hasPreview;
    internal string Id { get; }

    internal OverlayWindow(HostClient host, string id, string display, RectInt32 bounds, Action<string> showError)
    {
        this.host = host; Id = id; this.display = display; this.bounds = bounds; this.showError = showError;
        session = host.Events.Session ?? throw new InvalidOperationException("引擎会话已结束。");
        InitializeComponent();
        hwnd = WindowNative.GetWindowHandle(this);
        timer = DispatcherQueue.CreateTimer();
        timer.Interval = TimeSpan.FromMilliseconds(100);
        timer.Tick += PreviewTick;
        Root.Loaded += (_, _) => loaded.TrySetResult();
        AppWindow.Closing += (_, args) => { if (!closed) { args.Cancel = true; Cancel(); } };
        Activated += (_, e) =>
        {
            if (e.WindowActivationState != WindowActivationState.Deactivated) active = true;
            else if (active && !closed) Cancel();
        };
    }

    internal async Task OpenAsync(CancellationToken token)
    {
        SystemBackdrop = new WinUIEx.TransparentTintBackdrop();
        var presenter = OverlappedPresenter.Create();
        presenter.SetBorderAndTitleBar(false, false);
        presenter.IsAlwaysOnTop = true;
        presenter.IsResizable = presenter.IsMaximizable = presenter.IsMinimizable = false;
        AppWindow.SetPresenter(presenter);
        AppWindow.MoveAndResize(bounds);
        clearVisuals = !ProbeDesktop.TryExcludeOverlay(hwnd);
        Activate();
        await loaded.Task.WaitAsync(token);
        ProbeDesktop.FitClient(hwnd, bounds);
        scale = Root.XamlRoot.RasterizationScale;
        CheckDisplay();
        CancelButton.Focus(FocusState.Programmatic);
        timer.Start();
    }

    private void CheckDisplay()
    {
        var origin = ProbeDesktop.ClientOrigin(hwnd);
        if (host.Events.Session != session || Root.XamlRoot.RasterizationScale != scale ||
            origin.X != bounds.X || origin.Y != bounds.Y ||
            !SelectionGeometry.Displays().Any(area => bounds.Equals(area.OuterBounds)))
            throw new IOException("显示器、缩放或引擎会话已改变，请重新框选。");
    }

    internal async Task PrepareCaptureAsync(CancellationToken token)
    {
        CheckDisplay();
        if (!clearVisuals) return;
        var rendered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void Rendered(object? sender, object e) => rendered.TrySetResult();
        CompositionTarget.Rendered += Rendered;
        try
        {
            Root.Opacity = 0;
            await rendered.Task.WaitAsync(token);
            ProbeDesktop.Flush();
            if (!ProbeDesktop.OwnsInputAt(hwnd, region.X + region.Width / 2, region.Y + region.Height / 2))
                throw new IOException("框选窗口已失去焦点。");
        }
        catch { EndCapture(); throw; }
        finally { CompositionTarget.Rendered -= Rendered; }
    }

    internal void EndCapture() { if (!closed && clearVisuals) Root.Opacity = 1; }

    private void DragStarted(object sender, PointerRoutedEventArgs e)
    {
        var point = e.GetCurrentPoint(Surface);
        if (scale <= 0 || pointer != null || finishing || !point.Properties.IsLeftButtonPressed ||
            !Surface.CapturePointer(e.Pointer)) return;
        pointer = e.Pointer.PointerId;
        anchor = point.Position;
        PreviewImage.Source = null;
        Drag(anchor);
        e.Handled = true;
    }

    private void DragMoved(object sender, PointerRoutedEventArgs e)
    { if (pointer == e.Pointer.PointerId) Drag(e.GetCurrentPoint(Surface).Position); }
    private void DragEnded(object sender, PointerRoutedEventArgs e)
    {
        if (pointer != e.Pointer.PointerId) return;
        Drag(e.GetCurrentPoint(Surface).Position);
        pointer = null;
        Surface.ReleasePointerCapture(e.Pointer);
        e.Handled = true;
    }
    private void DragLost(object sender, PointerRoutedEventArgs e)
    { if (pointer == e.Pointer.PointerId) Cancel(); }

    private void Drag(Point end)
    {
        var origin = new PointInt32(bounds.X, bounds.Y);
        region = SelectionGeometry.FromDrag(anchor, end, origin, scale, bounds);
        Canvas.SetLeft(Outline, (region.X - bounds.X) / scale);
        Canvas.SetTop(Outline, (region.Y - bounds.Y) / scale);
        Outline.Width = region.Width / scale; Outline.Height = region.Height / scale;
        Coordinates.Text = $"({region.X}, {region.Y})　{region.Width} × {region.Height} px" +
            (SelectionGeometry.CanConfirm(region) ? "" : "　最小 10 × 10");
        hasPreview = false;
        ConfirmButton.IsEnabled = false;
    }

    private async void PreviewTick(DispatcherQueueTimer sender, object args)
    {
        if (reading || finishing || closed) return;
        reading = true;
        var area = region;
        try
        {
            CheckDisplay();
            if (!SelectionGeometry.CanConfirm(area)) return;
            var data = await host.CallAsync(new { op = "selection_region", session_id = session, selection_id = Id,
                a = new[] { (double)area.X, area.Y }, b = new[] { (double)(area.X + area.Width), area.Y + area.Height },
                origin = new[] { 0, 0 }, scale = 1 });
            if (closed || finishing || !area.Equals(region) || !data.TryGetProperty("preview", out var preview)) return;
            var result = await host.RequestAsync("get_image_preview", new { image_id = preview.GetProperty("image_id").GetString(), max_edge = 320 });
            var bitmap = await ImagePicker.DecodePreviewAsync(result.GetProperty("base64").GetString()!);
            if (closed || finishing || !area.Equals(region)) return;
            PreviewImage.Source = bitmap;
            hasPreview = true; ConfirmButton.IsEnabled = pointer == null;
        }
        catch (Exception ex) { if (!closed && !finishing) { showError(ex.Message); Cancel(); } }
        finally { reading = false; }
    }

    private async void ConfirmSelection(object sender, RoutedEventArgs args)
    {
        if (!hasPreview || finishing || closed || pointer != null) return;
        finishing = true; ConfirmButton.IsEnabled = false;
        try
        {
            CheckDisplay();
            await host.RequestAsync("confirm_selection", new { selection_id = Id,
                region = new { space = "screen_physical", display_id = display,
                    x = region.X, y = region.Y, width = region.Width, height = region.Height } });
        }
        catch (Exception ex) { showError(ex.Message); finishing = false; Cancel(); }
    }

    private void EscapePressed(KeyboardAccelerator sender, KeyboardAcceleratorInvokedEventArgs e)
    { e.Handled = true; Cancel(); }
    private void CancelSelection(object sender, RoutedEventArgs e) => Cancel();
    private async void Cancel()
    {
        if (closed || finishing) return;
        finishing = true;
        try { await host.RequestAsync("stop_selection_mode", new { selection_id = Id }); }
        catch (Exception ex) { showError(ex.Message); if (!closed) { finishing = false; } }
    }

    internal void CloseFromHost()
    {
        if (closed) return;
        closed = true;
        timer.Stop(); timer.Tick -= PreviewTick;
        pointer = null; Surface.ReleasePointerCaptures(); Close();
    }
}
