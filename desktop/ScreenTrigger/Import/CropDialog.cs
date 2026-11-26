using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using ScreenTrigger.Contracts;
using Windows.Foundation;

namespace ScreenTrigger.Import;

internal sealed class CropDialog : ContentDialog
{
    private readonly ImageRef source;
    private readonly Grid surface;
    private readonly Rectangle selection;
    private readonly NumberBox left = Field("左", "CropX"), top = Field("上", "CropY"),
        width = Field("宽", "CropWidth"), height = Field("高", "CropHeight");
    private Point anchor;
    private uint? pointer;

    internal int X => (int)left.Value;
    internal int Y => (int)top.Value;
    internal int WidthPixels => (int)width.Value;
    internal int HeightPixels => (int)height.Value;

    internal CropDialog(XamlRoot root, ImageRef source, ImageSource preview)
    {
        this.source = source;
        XamlRoot = root;
        Title = "裁剪导入图像";
        PrimaryButtonText = "使用裁剪图";
        CloseButtonText = "取消";
        DefaultButton = ContentDialogButton.Primary;
        double scale = Math.Min(420.0 / source.Width, 220.0 / source.Height);
        surface = new Grid { Width = source.Width * scale, Height = source.Height * scale,
                             HorizontalAlignment = HorizontalAlignment.Left };
        surface.Children.Add(new Image { Source = preview, Stretch = Stretch.Fill });
        selection = new Rectangle { Stroke = new SolidColorBrush(Colors.DeepSkyBlue), StrokeThickness = 2,
                                    HorizontalAlignment = HorizontalAlignment.Left, VerticalAlignment = VerticalAlignment.Top,
                                    IsHitTestVisible = false };
        surface.Children.Add(selection);
        surface.PointerPressed += Start;
        surface.PointerMoved += Move;
        surface.PointerReleased += Finish;
        surface.PointerCaptureLost += (_, _) => pointer = null;
        left.Value = top.Value = 0;
        width.Value = source.Width;
        height.Value = source.Height;
        var coordinates = new Grid { ColumnSpacing = 6 };
        NumberBox[] fields = [left, top, width, height];
        for (int i = 0; i < fields.Length; i++)
        {
            coordinates.ColumnDefinitions.Add(new ColumnDefinition());
            Grid.SetColumn(fields[i], i);
            coordinates.Children.Add(fields[i]);
            fields[i].ValueChanged += (_, _) => RefreshOutline();
        }
        var contents = new StackPanel { Spacing = 8 };
        contents.Children.Add(new TextBlock { Text = $"在预览上拖动，或输入像素范围。原图 {source.Width} × {source.Height} px。",
                                             TextWrapping = TextWrapping.Wrap });
        contents.Children.Add(surface);
        contents.Children.Add(coordinates);
        Content = contents;
        RefreshOutline();
    }

    private static NumberBox Field(string label, string id)
    {
        var field = new NumberBox { Header = label, SmallChange = 1 };
        AutomationProperties.SetAutomationId(field, id);
        return field;
    }

    private void Start(object sender, PointerRoutedEventArgs e)
    {
        var point = e.GetCurrentPoint(surface);
        if (pointer != null || !point.Properties.IsLeftButtonPressed || !surface.CapturePointer(e.Pointer)) return;
        pointer = e.Pointer.PointerId;
        anchor = point.Position;
        e.Handled = true;
    }

    private void Move(object sender, PointerRoutedEventArgs e)
    {
        if (pointer != e.Pointer.PointerId) return;
        var end = e.GetCurrentPoint(surface).Position;
        double sx = source.Width / surface.ActualWidth, sy = source.Height / surface.ActualHeight;
        left.Value = Math.Clamp(Math.Floor(Math.Min(anchor.X, end.X) * sx), 0, source.Width - 1);
        top.Value = Math.Clamp(Math.Floor(Math.Min(anchor.Y, end.Y) * sy), 0, source.Height - 1);
        width.Value = Math.Clamp(Math.Ceiling(Math.Max(anchor.X, end.X) * sx), left.Value + 1, source.Width) - left.Value;
        height.Value = Math.Clamp(Math.Ceiling(Math.Max(anchor.Y, end.Y) * sy), top.Value + 1, source.Height) - top.Value;
        e.Handled = true;
    }

    private void Finish(object sender, PointerRoutedEventArgs e)
    {
        if (pointer != e.Pointer.PointerId) return;
        Move(sender, e);
        pointer = null;
        surface.ReleasePointerCapture(e.Pointer);
    }

    private void RefreshOutline()
    {
        bool valid = double.IsInteger(left.Value) && double.IsInteger(top.Value) &&
                     double.IsInteger(width.Value) && double.IsInteger(height.Value) &&
                     left.Value >= 0 && top.Value >= 0 && width.Value >= 1 && height.Value >= 1 &&
                     left.Value + width.Value <= source.Width && top.Value + height.Value <= source.Height;
        IsPrimaryButtonEnabled = valid;
        selection.Visibility = valid ? Visibility.Visible : Visibility.Collapsed;
        if (!valid) return;
        double sx = surface.Width / source.Width, sy = surface.Height / source.Height;
        selection.Margin = new Thickness(left.Value * sx, top.Value * sy, 0, 0);
        selection.Width = width.Value * sx;
        selection.Height = height.Value * sy;
    }
}
