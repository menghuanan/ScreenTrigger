using System.ComponentModel;
using System.Diagnostics;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using System.Runtime.InteropServices.WindowsRuntime;
using Windows.Foundation;
using Windows.Graphics;
using WinRT.Interop;

namespace SelectionProbe;

internal sealed record SelectionResult(string Outcome, string Message,
    CapturedFrame? Preview = null, CapturedFrame? Final = null);

public sealed partial class OverlayWindow : Window
{
    private readonly DisplayArea display;
    private readonly RectInt32 bounds;
    private readonly ProbeRun run;
    private readonly DispatcherQueueTimer timer;
    private readonly TaskCompletionSource<SelectionResult> done = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource loaded = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly CancellationTokenSource lifetime = new();
    private readonly nint hwnd;
    private Point anchor;
    private uint? pointerId;
    private RectInt32 region;
    private CapturedFrame? shown;
    private double scale;
    private bool reading, confirming, closed, active;
    private int captureAttempts;
    private bool clearVisualsForCapture;
    private readonly SemaphoreSlim captureLock = new(1, 1);
    private readonly bool measurePreview = Environment.GetCommandLineArgs().Contains("--measure-preview");

    internal OverlayWindow(DisplayArea display, ProbeRun run)
    {
        this.display = display;
        bounds = display.OuterBounds;
        this.run = run;
        InitializeComponent();
        hwnd = WindowNative.GetWindowHandle(this);
        timer = DispatcherQueue.CreateTimer();
        timer.Interval = TimeSpan.FromMilliseconds(100);
        timer.Tick += PreviewTick;
        Root.Loaded += (_, _) => loaded.TrySetResult();
        Closed += (_, _) => { closed = true; Cancel("window_closed"); };
        Activated += (_, e) =>
        {
            if (e.WindowActivationState != WindowActivationState.Deactivated) active = true;
            else if (active) Cancel("focus_lost");
        };
    }

    internal async Task<SelectionResult> SelectAsync()
    {
        SystemBackdrop = new WinUIEx.TransparentTintBackdrop();
        var presenter = OverlappedPresenter.Create();
        presenter.SetBorderAndTitleBar(false, false);
        presenter.IsAlwaysOnTop = true;
        presenter.IsResizable = presenter.IsMaximizable = presenter.IsMinimizable = false;
        AppWindow.SetPresenter(presenter);
        AppWindow.MoveAndResize(bounds);
        clearVisualsForCapture = Environment.GetCommandLineArgs().Contains("--composed-capture") || !ProbeDesktop.TryExcludeOverlay(hwnd);
        Activate();
        await Task.WhenAny(loaded.Task, done.Task).WaitAsync(TimeSpan.FromSeconds(5));
        if (!done.Task.IsCompleted)
        {
            ProbeDesktop.FitClient(hwnd, bounds);
            scale = Root.XamlRoot.RasterizationScale;
            ValidateDisplay();
            var origin = ProbeDesktop.ClientOrigin(hwnd);
            run.Log("overlay_ready", new { overlay_hwnd = (long)hwnd, foreground = (long)ProbeDesktop.GetForegroundWindow(),
                x = origin.X, y = origin.Y, scale, affinity = clearVisualsForCapture ? "transparent_composition" : "WDA_EXCLUDEFROMCAPTURE" });
            CancelButton.Focus(FocusState.Programmatic);
            timer.Start();
        }
        return await done.Task;
    }

    private void ValidateDisplay()
    {
        var current = DisplayArea.GetFromDisplayId(display.DisplayId);
        var origin = ProbeDesktop.ClientOrigin(hwnd);
        if (current == null || !bounds.Equals(current.OuterBounds) || origin.X != bounds.X ||
            origin.Y != bounds.Y || Root.XamlRoot.RasterizationScale != scale)
            throw new IOException("显示器或缩放已改变，请重新框选。");
    }

    private void DragStarted(object sender, PointerRoutedEventArgs e)
    {
        var point = e.GetCurrentPoint(Surface);
        if (scale == 0 || pointerId != null || confirming || !point.Properties.IsLeftButtonPressed ||
            !Surface.CapturePointer(e.Pointer)) return;
        pointerId = e.Pointer.PointerId;
        anchor = point.Position;
        shown = null;
        PreviewImage.Source = null;
        UpdateDrag(anchor);
        e.Handled = true;
    }

    private void DragMoved(object sender, PointerRoutedEventArgs e)
    {
        if (pointerId == e.Pointer.PointerId) UpdateDrag(e.GetCurrentPoint(Surface).Position);
    }

    private void UpdateDrag(Point end)
    {
        try
        {
            var origin = ProbeDesktop.ClientOrigin(hwnd);
            region = SelectionGeometry.FromDrag(anchor, end, origin, scale, bounds);
            Canvas.SetLeft(Outline, (region.X - origin.X) / scale);
            Canvas.SetTop(Outline, (region.Y - origin.Y) / scale);
            Outline.Width = region.Width / scale;
            Outline.Height = region.Height / scale;
            Coordinates.Text = $"({region.X}, {region.Y})　{region.Width} × {region.Height} px"
                + (SelectionGeometry.CanConfirm(region) ? "" : "　最小 10 × 10");
            ConfirmButton.IsEnabled = false;
        }
        catch (Exception ex) { Failed(ex); }
    }

    private void DragEnded(object sender, PointerRoutedEventArgs e)
    {
        if (pointerId != e.Pointer.PointerId) return;
        var end = e.GetCurrentPoint(Surface).Position;
        UpdateDrag(end);
        pointerId = null;
        Surface.ReleasePointerCapture(e.Pointer);
        run.Log("pointer_released", new { from_x = anchor.X, from_y = anchor.Y,
            to_x = end.X, to_y = end.Y, region = ProbeRun.Rect(region),
            valid = SelectionGeometry.CanConfirm(region) });
        UpdateConfirm();
        e.Handled = true;
    }

    private void DragLost(object sender, PointerRoutedEventArgs e)
    {
        if (pointerId == e.Pointer.PointerId) Cancel("pointer_cancelled");
    }

    private void UpdateConfirm()
    {
        bool enable = !confirming && pointerId == null && SelectionGeometry.CanConfirm(region) &&
            shown?.Region.Equals(region) == true;
        bool gained = !ConfirmButton.IsEnabled && enable;
        ConfirmButton.IsEnabled = enable;
        if (gained) ConfirmButton.Focus(FocusState.Programmatic);
    }

    private async Task<CapturedFrame> CaptureAsync(RectInt32 area, bool preview)
    {
        var token = lifetime.Token;
        var args = Environment.GetCommandLineArgs();
        for (int attempt = 1; ; attempt++)
        {
            token.ThrowIfCancellationRequested();
            ValidateDisplay();
            try
            {
                captureAttempts++;
                if (args.Contains("--fail-capture") || (captureAttempts == 1 && args.Contains("--fail-once")))
                    throw new IOException("测试注入的捕获失败。");
                if (clearVisualsForCapture) return await ReadTransparentFrameAsync(area, preview, token);
                return await Task.Run(() => ScreenCapture.Read(area, preview), token).WaitAsync(TimeSpan.FromSeconds(5), token);
            }
            catch (Exception ex) when (attempt == 1 && (ex is IOException || ex is Win32Exception))
            {
                run.Log("capture_retry", new { delay_ms = 200, error = ex.Message });
                await Task.Delay(200, token);
            }
        }
    }

    private async Task<CapturedFrame> ReadTransparentFrameAsync(RectInt32 area, bool preview, CancellationToken token)
    {
        await captureLock.WaitAsync(token);
        var rendered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnRendered(object? sender, object args) => rendered.TrySetResult();
        try
        {
            // Keep the input HWND, pointer capture and XAML hit targets alive throughout the read.
            CompositionTarget.Rendered += OnRendered;
            Root.Opacity = 0;
            await rendered.Task.WaitAsync(TimeSpan.FromSeconds(5), token);
            return await Task.Run(() =>
            {
                ProbeDesktop.Flush();
                if (!ProbeDesktop.OwnsInputAt(hwnd, area.X + area.Width / 2, area.Y + area.Height / 2))
                    throw new IOException("捕获期间框选窗失去输入焦点。");
                return ScreenCapture.Read(area, preview);
            }, token).WaitAsync(TimeSpan.FromSeconds(5), token);
        }
        finally
        {
            CompositionTarget.Rendered -= OnRendered;
            if (!closed) Root.Opacity = 1;
            captureLock.Release();
        }
    }

    private async void PreviewTick(DispatcherQueueTimer sender, object args)
    {
        if (reading || confirming || done.Task.IsCompleted) return;
        reading = true;
        try
        {
            ValidateDisplay();
            var area = region;
            if (area.Width == 0 || area.Height == 0) return;
            var frame = await CaptureAsync(area, true);
            if (done.Task.IsCompleted || confirming || !area.Equals(region)) return;
            PreviewImage.Source = frame.ToBitmap();
            shown = frame;
            run.Log("preview_updated", new { region = ProbeRun.Rect(area), captured_ticks = frame.CapturedAt,
                assigned_ticks = Stopwatch.GetTimestamp() });
            UpdateConfirm();
            if (measurePreview) await MeasureRenderedPreviewAsync();
        }
        catch (OperationCanceledException) when (done.Task.IsCompleted) { }
        catch (Exception ex) { Failed(ex); }
        finally { reading = false; }
    }

    private async Task MeasureRenderedPreviewAsync()
    {
        // Sample the rendered Image control after composition, independently of the GDI buffer.
        var rendered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnRendered(object? sender, object args) => rendered.TrySetResult();
        CompositionTarget.Rendered += OnRendered;
        try
        {
            await rendered.Task.WaitAsync(TimeSpan.FromSeconds(5), lifetime.Token);
            var sample = new RenderTargetBitmap();
            await sample.RenderAsync(PreviewImage);
            var pixels = (await sample.GetPixelsAsync()).ToArray();
            if (done.Task.IsCompleted || confirming || pixels.Length == 0) return;
            var center = 4 * (sample.PixelHeight / 2 * sample.PixelWidth + sample.PixelWidth / 2);
            run.Log("preview_rendered", new { red = pixels[center + 2], green = pixels[center + 1],
                blue = pixels[center], presented_ticks = Stopwatch.GetTimestamp() });
        }
        finally { CompositionTarget.Rendered -= OnRendered; }
    }

    private async void ConfirmSelection(object sender, RoutedEventArgs args)
    {
        if (confirming || done.Task.IsCompleted || !ConfirmButton.IsEnabled || shown is not { } preview) return;
        confirming = true;
        ConfirmButton.IsEnabled = false;
        timer.Stop();
        try
        {
            var final = await CaptureAsync(region, false);
            if (done.Task.IsCompleted) return;
            ValidateDisplay();
            Finish(new("confirmed", "已确认选区。", preview, final));
        }
        catch (OperationCanceledException) when (done.Task.IsCompleted) { }
        catch (Exception ex) { Failed(ex); }
    }

    private void EscapePressed(KeyboardAccelerator sender, KeyboardAcceleratorInvokedEventArgs e)
    {
        e.Handled = true;
        Cancel("escape");
    }

    private void CancelSelection(object sender, RoutedEventArgs args) => Cancel("cancel_button");

    internal void Cancel(string reason) => Finish(new(reason, "已取消框选。"));

    private void Failed(Exception ex) => Finish(new("capture_failed", $"框选失败。{ex.Message}"));

    private void Finish(SelectionResult result)
    {
        if (!done.TrySetResult(result)) return;
        timer.Stop();
        lifetime.Cancel();
        pointerId = null;
        Surface.ReleasePointerCaptures();
        shown = null;
    }

    internal void CloseOnce()
    {
        Cancel("window_closed");
        timer.Tick -= PreviewTick;
        if (!closed) Close();
        lifetime.Dispose();
    }
}
