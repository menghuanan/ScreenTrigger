using System.Text.Json;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using ScreenTrigger.Contracts;
using ScreenTrigger.Host;
using ScreenTrigger.Import;

namespace ScreenTrigger.Recognition;

public sealed partial class RecognitionPage : UserControl
{
    private readonly WorkspaceCommands commands;
    private readonly Func<ImageRef?> image;
    private readonly Func<(int X, int Y)> origin;
    private readonly Func<object?> draft;
    private string? job;
    private bool busy;

    internal RecognitionPage(HostClient host, Func<ImageRef?> image, Func<(int X, int Y)> origin, Func<object?> draft)
    {
        this.image = image; this.origin = origin; this.draft = draft;
        InitializeComponent(); commands = new(host, this, Feedback);
        Loaded += async (_, _) => await commands.Run(LoadTargets);
    }
    private async Task LoadTargets()
    {
        var rows = new List<CatalogItem>();
        if (draft() != null) rows.Add(new(null, "编辑中的草稿"));
        int offset = 0, count;
        do
        {
            var results = await commands.Call("list_targets", new { offset, limit = 500 });
            var items = results.GetProperty("items");
            count = results.GetProperty("total").GetInt32();
            foreach (var t in items.EnumerateArray()) rows.Add(new(t.GetProperty("id").GetString(),
                $"#{t.GetProperty("sample_no")}　{t.GetProperty("name")}"));
            if (items.GetArrayLength() == 0) break;
            offset += items.GetArrayLength();
        } while (offset < count);
        Targets.ItemsSource = rows; Targets.SelectedIndex = rows.Count > 0 ? 0 : -1;
    }
    private async void Evaluate(object sender, RoutedEventArgs args)
    {
        if (busy) return;
        busy = true; Run.IsEnabled = false; Evidence.Items.Clear();
        while (FrameCanvas.Children.Count > 1) FrameCanvas.Children.RemoveAt(1);
        try
        {
            var source = image() ?? throw new InvalidOperationException("请先框选或导入测试图片。");
            var frameOrigin = origin();
            var target = Targets.SelectedItem as CatalogItem ?? throw new InvalidOperationException("请选择目标。");
            var pixels = await commands.Call("get_image_preview", new { image_id = source.ImageId, max_edge = 800 });
            FrameImage.Source = await ImagePicker.DecodePreviewAsync(pixels.GetProperty("base64").GetString()!);
            FrameCanvas.Width = FrameImage.Width = source.Width;
            FrameCanvas.Height = FrameImage.Height = source.Height;
            FrameLabel.Text = $"{source.Width} × {source.Height} px";
            var accepted = target.Id == null ?
                await commands.Call("test_recognition", new { image_id = source.ImageId, draft = draft() }) :
                await commands.Call("evaluate_target_conditions", new { image_id = source.ImageId, target_id = target.Id });
            job = accepted.GetProperty("job_id").GetString();
            Feedback.Text = "正在识别…"; Cancel.IsEnabled = true;
            for (;;)
            {
                var state = await commands.Call("get_job", new { job_id = job });
                switch (state.GetProperty("status").GetString())
                {
                    case "completed": Draw(state.GetProperty("result"), frameOrigin); return;
                    case "cancelled": Feedback.Text = "已取消。"; return;
                    case "error": var error = state.GetProperty("error"); Feedback.Text = $"{error.GetProperty("code")} · {error.GetProperty("message")}"; return;
                }
                await Task.Delay(100);
            }
        }
        catch (Exception ex) { Feedback.Text = ex.Message; }
        finally { job = null; busy = false; Cancel.IsEnabled = false; Run.IsEnabled = true; }
    }
    private void Draw(JsonElement result, (int X, int Y) frameOrigin)
    {
        var matches = result.GetProperty("matches");
        Feedback.Text = $"命中 {matches.GetArrayLength()} 处 · 帧 {result.GetProperty("frame_id")}";
        foreach (var match in matches.EnumerateArray())
        {
            var box = match.GetProperty("bbox");
            var outline = new Rectangle { Width = box.GetProperty("width").GetDouble(), Height = box.GetProperty("height").GetDouble(),
                Stroke = new SolidColorBrush(Colors.DeepPink), StrokeThickness = Math.Max(2, FrameCanvas.Width / 350) };
            var physical = match.GetProperty("space").GetString() == "screen_physical";
            Canvas.SetLeft(outline, box.GetProperty("x").GetInt32() - (physical ? frameOrigin.X : 0));
            Canvas.SetTop(outline, box.GetProperty("y").GetInt32() - (physical ? frameOrigin.Y : 0));
            FrameCanvas.Children.Add(outline);
            var label = result.GetProperty("sample_no").ValueKind == JsonValueKind.Null ? "草稿" : $"样本 {match.GetProperty("sample_no")}";
            Evidence.Items.Add($"{label}　中心 {match.GetProperty("center")}　得分 {match.GetProperty("confidence"):G}");
            foreach (var item in match.GetProperty("evidence").EnumerateArray()) Evidence.Items.Add(item.GetRawText());
        }
    }
    private async void CancelJob(object sender, RoutedEventArgs args)
    {
        if (job == null) return;
        Cancel.IsEnabled = false;
        try { await commands.Call("cancel_job", new { job_id = job }); }
        catch (Exception ex) { Feedback.Text = ex.Message; }
    }
}
