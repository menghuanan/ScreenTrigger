using System.ComponentModel;
using System.Runtime.InteropServices;
using Windows.Graphics;

namespace SelectionProbe;

internal static class ProbeDesktop
{
    [StructLayout(LayoutKind.Sequential)]
    private struct NativePoint { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect { public int Left, Top, Right, Bottom; }
    [DllImport("user32", SetLastError = true)] private static extern bool GetClientRect(nint hwnd, out NativeRect rect);
    [DllImport("user32", SetLastError = true)] private static extern bool GetWindowRect(nint hwnd, out NativeRect rect);
    [DllImport("user32", SetLastError = true)] private static extern bool SetWindowPos(nint hwnd, nint after,
        int x, int y, int width, int height, uint flags);

    [DllImport("user32", SetLastError = true)]
    private static extern bool ClientToScreen(nint hwnd, ref NativePoint point);
    [DllImport("user32", SetLastError = true)]
    private static extern bool SetWindowDisplayAffinity(nint hwnd, uint affinity);
    [DllImport("user32", SetLastError = true)]
    private static extern bool GetWindowDisplayAffinity(nint hwnd, out uint affinity);
    [DllImport("user32")] public static extern nint GetForegroundWindow();
    [DllImport("user32")] private static extern nint WindowFromPoint(NativePoint point);
    [DllImport("user32")] private static extern nint GetAncestor(nint hwnd, uint flags);
    public static bool OwnsInputAt(nint hwnd, int x, int y) => GetForegroundWindow() == hwnd &&
        GetAncestor(WindowFromPoint(new NativePoint { X = x, Y = y }), 2 /* GA_ROOT */) == hwnd;
    [DllImport("user32")] public static extern bool IsWindowVisible(nint hwnd);
    [DllImport("dwmapi")] private static extern int DwmFlush();

    public static void Flush() => Marshal.ThrowExceptionForHR(DwmFlush());

    public static PointInt32 ClientOrigin(nint hwnd)
    {
        NativePoint point = default;
        if (!ClientToScreen(hwnd, ref point)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return new(point.X, point.Y);
    }

    public static void FitClient(nint hwnd, RectInt32 bounds)
    {
        var origin = ClientOrigin(hwnd);
        if (!GetWindowRect(hwnd, out var outer) || !GetClientRect(hwnd, out var client) ||
            !SetWindowPos(hwnd, 0, outer.Left + bounds.X - origin.X, outer.Top + bounds.Y - origin.Y,
                outer.Right - outer.Left + bounds.Width - client.Right,
                outer.Bottom - outer.Top + bounds.Height - client.Bottom, 0x14)) // NOZORDER | NOACTIVATE
            throw new Win32Exception(Marshal.GetLastWin32Error(), "无法将框选客户区定位到所选显示器。");
    }

    public static bool TryExcludeOverlay(nint hwnd)
    {
        const uint exclude = 0x11;
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 19041) || !SetWindowDisplayAffinity(hwnd, exclude))
            return false;
        if (GetWindowDisplayAffinity(hwnd, out var actual) && actual == exclude) return true;
        if (!SetWindowDisplayAffinity(hwnd, 0)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return false;
    }

}
