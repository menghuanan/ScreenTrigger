using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Graphics;
using WinRT.Interop;

namespace SelectionProbe;

public sealed partial class MainWindow : Window
{
    private OverlayWindow? overlay;
    private bool closing;

    public MainWindow()
    {
        InitializeComponent();
        AppWindow.Resize(new SizeInt32(560, 510));
        RefreshDisplays(this, null);
        Closed += (_, _) => { closing = true; overlay?.Cancel("main_closed"); };
    }

    private void RefreshDisplays(object sender, object? args)
    {
        var previous = (Displays.SelectedItem as ComboBoxItem)?.Tag;
        Displays.Items.Clear();
        Displays.Items.Add(new ComboBoxItem { Content = "主窗口所在显示器" });
        Displays.SelectedIndex = 0;
        foreach (var area in SelectionGeometry.Displays())
        {
            var r = area.OuterBounds;
            var item = new ComboBoxItem { Tag = area.DisplayId.Value,
                Content = $"{area.DisplayId.Value} · {r.Width} × {r.Height}  ({r.X}, {r.Y})" };
            Displays.Items.Add(item);
            if (Equals(previous, item.Tag)) Displays.SelectedItem = item;
        }
    }

    private async void StartSelection(object sender, RoutedEventArgs args)
    {
        if (!StartButton.IsEnabled || closing) return;
        StartButton.IsEnabled = false;
        ResultImage.Source = null;
        var hwnd = WindowNative.GetWindowHandle(this);
        ProbeRun? run = null;
        try
        {
            run = new ProbeRun();
            var position = AppWindow.Position;
            var size = AppWindow.Size;
            var window = new RectInt32(position.X, position.Y, size.Width, size.Height);
            var manual = (Displays.SelectedItem as ComboBoxItem)?.Tag;
            var display = manual is ulong id ? SelectionGeometry.Displays().First(d => d.DisplayId.Value == id)
                : SelectionGeometry.DefaultDisplay(window);
            run.Log("begin", new { main_hwnd = (long)hwnd, foreground = (long)ProbeDesktop.GetForegroundWindow(),
                display_id = display.DisplayId.Value, display = ProbeRun.Rect(display.OuterBounds),
                main = ProbeRun.Rect(window), scale = StartButton.XamlRoot.RasterizationScale });
            overlay = new OverlayWindow(display, run);
            SelectionResult result;
            try
            {
                AppWindow.Hide();
                await Task.Run(ProbeDesktop.Flush).WaitAsync(TimeSpan.FromSeconds(5));
                if (closing) return;
                if (ProbeDesktop.IsWindowVisible(hwnd)) throw new IOException("主窗口未能在捕获前隐藏。");
                run.Log("main_hidden", new { visible = false });
                result = await overlay.SelectAsync();
            }
            finally
            {
                try { overlay.CloseOnce(); }
                finally
                {
                    overlay = null;
                    if (!closing)
                    {
                        AppWindow.Show();
                        Activate();
                        StartButton.IsEnabled = true;
                        StartButton.Focus(FocusState.Programmatic);
                        run.Log("restored", new { main_hwnd = (long)hwnd, foreground = (long)ProbeDesktop.GetForegroundWindow(),
                            visible = ProbeDesktop.IsWindowVisible(hwnd), focus = StartButton.FocusState.ToString() });
                    }
                }
            }
            if (closing) return;
            // Restoration precedes disk I/O, even when an evidence directory fills up.
            if (result.Final is { } final && result.Preview is { } preview)
            {
                StartButton.IsEnabled = false;
                await run.SaveFramesAsync(preview, final);
                ResultImage.Source = preview.ToBitmap();
                Status.Text = $"已保存 {final.Width} × {final.Height} 像素。\n{run.DirectoryPath}";
            }
            else Status.Text = result.Message;
            run.Log("result", new { outcome = result.Outcome, message = result.Message,
                region = result.Final == null ? null : ProbeRun.Rect(result.Final.Region),
                preview_ticks = result.Preview?.CapturedAt, final_ticks = result.Final?.CapturedAt });
        }
        catch (Exception ex)
        {
            Status.Text = $"框选失败。{ex.Message}";
            try { run?.Log("error", new { message = ex.Message, hresult = ex.HResult }); }
            catch (IOException) { /* The error is still visible if the evidence disk is full. */ }
        }
        finally
        {
            try { run?.Dispose(); } catch (IOException ex) { Status.Text = $"保存记录失败。{ex.Message}"; }
            if (!closing)
            {
                StartButton.IsEnabled = true;
                StartButton.Focus(FocusState.Programmatic);
            }
        }
    }
}
