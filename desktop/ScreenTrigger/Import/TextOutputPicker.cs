using System.Runtime.InteropServices;

namespace ScreenTrigger.Import;

internal static class TextOutputPicker
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private sealed class OpenFileName
    {
        public int Size = Marshal.SizeOf<OpenFileName>();
        public nint Owner, Instance;
        public string Filter = "文本文件 (*.txt)\0*.txt\0\0";
        public nint CustomFilter;
        public int MaxCustomFilter, FilterIndex = 1;
        public nint File;
        public int MaxFile = 32768;
        public nint FileTitle;
        public int MaxFileTitle;
        public string? InitialDirectory, Title;
        public int Flags;
        public ushort FileOffset, FileExtension;
        public string DefaultExtension = "txt";
        public nint CustomData, Hook, TemplateName, Reserved;
        public int ReservedValue, FlagsEx;
    }

    [DllImport("comdlg32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetSaveFileNameW([In, Out] OpenFileName dialog);
    [DllImport("comdlg32.dll")] private static extern int CommDlgExtendedError();
    [DllImport("user32.dll")] private static extern nint GetLastActivePopup(nint owner);
    [DllImport("user32.dll")] private static extern nint GetWindow(nint hwnd, uint command);
    [DllImport("user32.dll")] private static extern bool PostMessageW(nint hwnd, uint message, nint wParam, nint lParam);

    internal static string? Pick(nint owner, string mode, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        const int noChangeDir = 0x8, pathMustExist = 0x800, noTestFileCreate = 0x10000,
                  explorer = 0x80000, dontAddToRecent = 0x02000000, overwritePrompt = 0x2;
        var dialog = new OpenFileName { Owner = owner, Title = mode == "overwrite" ? "选择覆盖写入的 TXT" : "选择追加写入的 TXT",
            Flags = noChangeDir | pathMustExist | noTestFileCreate | explorer | dontAddToRecent |
                    (mode == "overwrite" ? overwritePrompt : 0) };
        dialog.File = Marshal.AllocHGlobal(dialog.MaxFile * sizeof(char));
        try
        {
            Marshal.WriteInt16(dialog.File, 0);
            using var cancellation = token.Register(() =>
            {
                nint popup = GetLastActivePopup(owner);
                if (popup != 0 && popup != owner && GetWindow(popup, 4) == owner)
                    PostMessageW(popup, 0x0010, 0, 0);
            });
            if (!GetSaveFileNameW(dialog))
            {
                int code = CommDlgExtendedError();
                if (code != 0) throw new InvalidOperationException($"无法选择文本文件（0x{code:X}）。");
                return null;
            }
            token.ThrowIfCancellationRequested();
            string path = Marshal.PtrToStringUni(dialog.File)!;
            if (!string.Equals(Path.GetExtension(path), ".txt", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("请选择扩展名为 .txt 的文件。");
            return path;
        }
        finally { Marshal.FreeHGlobal(dialog.File); }
    }
}
