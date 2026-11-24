using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.WindowsRuntime;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Graphics;
using Windows.Graphics.Imaging;

namespace SelectionProbe;

internal sealed record CapturedFrame(RectInt32 Region, int Width, int Height, byte[] Pixels, long CapturedAt)
{
    public WriteableBitmap ToBitmap()
    {
        var bitmap = new WriteableBitmap(Width, Height);
        using var buffer = bitmap.PixelBuffer.AsStream();
        buffer.Write(Pixels);
        bitmap.Invalidate();
        return bitmap;
    }

    public async Task SaveAsync(string path)
    {
        using var file = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None);
        using var stream = file.AsRandomAccessStream();
        var png = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
        png.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Ignore, (uint)Width, (uint)Height,
            96, 96, Pixels);
        await png.FlushAsync();
    }
}

internal static class ScreenCapture
{
    [StructLayout(LayoutKind.Sequential)]
    private struct BitmapInfo
    {
        public uint Size;
        public int Width, Height;
        public ushort Planes, BitCount;
        public uint Compression, SizeImage;
        public int XPelsPerMeter, YPelsPerMeter;
        public uint ColorsUsed, ColorsImportant;
    }

    [DllImport("user32")] private static extern nint GetDC(nint hwnd);
    [DllImport("user32")] private static extern int ReleaseDC(nint hwnd, nint dc);
    [DllImport("gdi32")] private static extern nint CreateCompatibleDC(nint source);
    [DllImport("gdi32")] private static extern bool DeleteDC(nint dc);
    [DllImport("gdi32")] private static extern bool DeleteObject(nint obj);
    [DllImport("gdi32")] private static extern nint SelectObject(nint dc, nint obj);
    [DllImport("gdi32")] private static extern nint CreateDIBSection(nint dc, ref BitmapInfo info,
        uint usage, out nint pixels, nint section, uint offset);
    [DllImport("gdi32", SetLastError = true)] private static extern bool StretchBlt(nint dest,
        int x, int y, int width, int height, nint source, int sx, int sy, int sw, int sh, uint operation);
    [DllImport("gdi32")] private static extern bool GdiFlush();

    public static CapturedFrame Read(RectInt32 region, bool preview)
    {
        const int previewWidth = 320, previewHeight = 160;
        double ratio = preview ? Math.Min(1, Math.Min((double)previewWidth / region.Width,
            (double)previewHeight / region.Height)) : 1;
        int width = Math.Max(1, (int)Math.Round(region.Width * ratio));
        int height = Math.Max(1, (int)Math.Round(region.Height * ratio));
        var bytes = new byte[checked(width * height * 4)];
        nint dc = GetDC(0), memory = 0, dib = 0, previous = 0;
        try
        {
            if (dc == 0 || (memory = CreateCompatibleDC(dc)) == 0)
                throw new IOException("无法获取桌面设备上下文。");
            var info = new BitmapInfo { Size = (uint)Marshal.SizeOf<BitmapInfo>(), Width = width,
                Height = -height, Planes = 1, BitCount = 32 };
            dib = CreateDIBSection(memory, ref info, 0, out var pixels, 0, 0);
            if (dib == 0 || (previous = SelectObject(memory, dib)) == 0)
                throw new IOException("无法分配桌面图像缓冲区。");
            const uint desktopCopy = 0x00CC0020 | 0x40000000; // SRCCOPY | CAPTUREBLT
            if (!StretchBlt(memory, 0, 0, width, height, dc, region.X, region.Y, region.Width,
                    region.Height, desktopCopy) || !GdiFlush())
                throw new Win32Exception(Marshal.GetLastWin32Error(), "读桌面像素失败。");
            long capturedAt = Stopwatch.GetTimestamp();
            Marshal.Copy(pixels, bytes, 0, bytes.Length);
            for (int i = 3; i < bytes.Length; i += 4) bytes[i] = 255;
            return new(region, width, height, bytes, capturedAt);
        }
        finally
        {
            if (previous != 0) SelectObject(memory, previous);
            if (dib != 0) DeleteObject(dib);
            if (memory != 0) DeleteDC(memory);
            if (dc != 0) ReleaseDC(0, dc);
        }
    }
}
