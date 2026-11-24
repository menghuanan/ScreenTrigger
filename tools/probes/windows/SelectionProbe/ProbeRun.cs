using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Windows.Graphics;

namespace SelectionProbe;

internal sealed class ProbeRun : IDisposable
{
    private readonly StreamWriter events;
    public string DirectoryPath { get; }

    public ProbeRun()
    {
        var args = Environment.GetCommandLineArgs();
        int output = Array.IndexOf(args, "--output");
        string root = output >= 0 ? args[output + 1] : Path.Combine("reports", "raw", "winui-selection");
        DirectoryPath = Path.GetFullPath(Path.Combine(root,
            $"{DateTime.UtcNow:yyyyMMddTHHmmssfffZ}-{Guid.NewGuid():N}"));
        Directory.CreateDirectory(DirectoryPath);
        events = new StreamWriter(new FileStream(Path.Combine(DirectoryPath, "events.jsonl"),
            FileMode.CreateNew, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
        Log("environment", new { os = Environment.OSVersion.VersionString, arch = RuntimeInformation.ProcessArchitecture.ToString(),
            runtime = RuntimeInformation.FrameworkDescription, qpc_frequency = Stopwatch.Frequency,
            backend = "GDI StretchBlt SRCCOPY|CAPTUREBLT", command = args });
    }

    public void Log(string kind, object data)
    {
        events.WriteLine(JsonSerializer.Serialize(new { kind, at = DateTimeOffset.UtcNow,
            ticks = Stopwatch.GetTimestamp(), data }));
    }

    public static object Rect(RectInt32 r) => new { x = r.X, y = r.Y, width = r.Width, height = r.Height };

    public async Task SaveFramesAsync(CapturedFrame preview, CapturedFrame final)
    {
        string previewPath = Path.Combine(DirectoryPath, "preview.png");
        string finalPath = Path.Combine(DirectoryPath, "final.png");
        try
        {
            await preview.SaveAsync(previewPath + ".tmp");
            await final.SaveAsync(finalPath + ".tmp");
            File.Move(previewPath + ".tmp", previewPath);
            File.Move(finalPath + ".tmp", finalPath);
        }
        catch
        {
            foreach (string file in new[] { previewPath + ".tmp", finalPath + ".tmp", previewPath, finalPath })
                File.Delete(file);
            throw;
        }
    }

    public void Dispose() => events.Dispose();
}
