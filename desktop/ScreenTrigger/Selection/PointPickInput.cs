using System.ComponentModel;
using System.Runtime.InteropServices;
using Windows.Graphics;

namespace ScreenTrigger.Selection;

/// <summary>Keep the whole confirmation gesture on a transparent input window owned by the point overlay.</summary>
internal sealed class PointPickInput : IDisposable
{
    private const uint MouseMove = 0x0200, LeftDown = 0x0201, LeftUp = 0x0202;
    private const uint CancelMode = 0x001F, CaptureChanged = 0x0215, MouseActivate = 0x0021;
    private const uint Popup = 0x80000000, Layered = 0x80000, NoActivate = 0x08000000, ToolWindow = 0x80;
    [StructLayout(LayoutKind.Sequential)]
    private struct Rect { public int Left, Top, Right, Bottom; }
    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate nint WindowProcedure(nint window, uint message, nuint key, nint value, nuint id, nuint data);
    [DllImport("user32", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint CreateWindowExW(uint extendedStyle, string cls, string? title, uint style,
        int x, int y, int width, int height, nint parent, nint menu, nint instance, nint parameter);
    [DllImport("user32", SetLastError = true)] private static extern bool GetWindowRect(nint window, out Rect rect);
    [DllImport("user32", SetLastError = true)] private static extern bool SetLayeredWindowAttributes(nint window, uint key, byte alpha, uint flags);
    [DllImport("user32", SetLastError = true)] private static extern bool SetWindowPos(nint window, nint after, int x, int y, int width, int height, uint flags);
    [DllImport("user32", SetLastError = true)] private static extern bool DestroyWindow(nint window);
    [DllImport("comctl32", SetLastError = true)] private static extern bool SetWindowSubclass(nint window, WindowProcedure proc, nuint id, nuint data);
    [DllImport("comctl32")] private static extern bool RemoveWindowSubclass(nint window, WindowProcedure proc, nuint id);
    [DllImport("comctl32")] private static extern nint DefSubclassProc(nint window, uint message, nuint key, nint value);
    [DllImport("user32")] private static extern nint SetCapture(nint window);
    [DllImport("user32")] private static extern nint GetCapture();
    [DllImport("user32")] private static extern bool ReleaseCapture();
    [DllImport("user32")] private static extern bool ClientToScreen(nint window, ref PointInt32 point);
    private readonly nint input;
    private readonly WindowProcedure procedure;
    private readonly Action<PointInt32> moved, confirmed;
    private readonly Action lost;
    private PointInt32? pressed;
    private bool disposed, completed;

    internal PointPickInput(nint overlay, Action<PointInt32> moved, Action<PointInt32> confirmed, Action lost)
    {
        this.moved = moved;
        this.confirmed = confirmed;
        this.lost = lost;
        procedure = Receive;
        if (!GetWindowRect(overlay, out var bounds)) throw new Win32Exception(Marshal.GetLastWin32Error());
        // WinUI owns a composition input island. An owned, nonactivating top-level window receives
        // the entire mouse gesture while leaving keyboard focus and the coordinate readout in WinUI.
        input = CreateWindowExW(Layered | NoActivate | ToolWindow, "STATIC", null, Popup | 0x0100,
            bounds.Left, bounds.Top, bounds.Right - bounds.Left, bounds.Bottom - bounds.Top, overlay, 0, 0, 0);
        if (input == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
        if (!SetWindowSubclass(input, procedure, 1, 0) ||
            !SetLayeredWindowAttributes(input, 0, 1, 2) ||
            !SetWindowPos(input, -1, 0, 0, 0, 0, 0x0053)) // TOPMOST, SHOWWINDOW, NOACTIVATE, NOSIZE, NOMOVE
        {
            var error = Marshal.GetLastWin32Error();
            DestroyWindow(input);
            throw new Win32Exception(error);
        }
    }

    private nint Receive(nint window, uint message, nuint key, nint value, nuint id, nuint data)
    {
        if (!disposed)
        {
            if (message == MouseActivate) return 3; // MA_NOACTIVATE; keep Escape on the WinUI overlay.
            if (message == 0x0084) return 1; // HTCLIENT
            if (message == 0x0014) return 1; // WM_ERASEBKGND
            if (message == 0x000F) { ValidateRect(window, 0); return 0; } // WM_PAINT
            if (message is MouseMove or LeftDown or LeftUp)
            {
                if (completed) return 0;
                var point = new PointInt32(unchecked((short)(int)value), unchecked((short)((int)value >> 16)));
                if (!ClientToScreen(window, ref point)) { lost(); return 0; }
                if (message == MouseMove) moved(point);
                else if (message == LeftDown)
                {
                    SetCapture(window);
                    if (GetCapture() != window) { lost(); return 0; }
                    pressed = point;
                }
                else if (pressed is PointInt32 click)
                {
                    completed = true;
                    pressed = null;
                    if (GetCapture() == window) ReleaseCapture();
                    confirmed(click);
                }
                return 0;
            }
            if (message is CaptureChanged or CancelMode && pressed.HasValue) { pressed = null; lost(); }
        }
        return DefSubclassProc(window, message, key, value);
    }

    [DllImport("user32")] private static extern bool ValidateRect(nint window, nint rect);

    public void Dispose()
    {
        if (disposed) return;
        disposed = true;
        if (GetCapture() == input) ReleaseCapture();
        RemoveWindowSubclass(input, procedure, 1);
        DestroyWindow(input);
    }
}
