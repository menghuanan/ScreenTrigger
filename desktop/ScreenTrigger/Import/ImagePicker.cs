using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Storage.Pickers;
using Windows.Storage.Streams;
using WinRT.Interop;

namespace ScreenTrigger.Import;

internal static class ImagePicker
{
    public static async Task<string?> PickAsync(nint owner, CancellationToken token)
    {
        var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.PicturesLibrary };
        foreach (var suffix in new[] { ".png", ".jpg", ".jpeg" }) picker.FileTypeFilter.Add(suffix);
        InitializeWithWindow.Initialize(picker, owner);
        return (await picker.PickSingleFileAsync().AsTask(token))?.Path;
    }

    public static async Task<BitmapImage> DecodePreviewAsync(string encoded)
    {
        using var stream = new InMemoryRandomAccessStream();
        using var writer = new DataWriter(stream);
        writer.WriteBytes(Convert.FromBase64String(encoded));
        await writer.StoreAsync();
        writer.DetachStream();
        stream.Seek(0);
        var bitmap = new BitmapImage();
        await bitmap.SetSourceAsync(stream);
        return bitmap;
    }
}
