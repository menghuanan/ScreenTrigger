using System.ComponentModel;
using System.Text.Json;
using Microsoft.UI.Xaml;

namespace ScreenTrigger.User;

public sealed class ScriptRow(JsonElement document) : INotifyPropertyChanged
{
    public JsonElement Document { get; private set; } = document;
    public string Id => Document.GetProperty("id").GetString()!;
    public string Name => Document.GetProperty("name").GetString()!;
    public override string ToString() => Name;
    public long Revision => Document.GetProperty("revision").GetInt64();
    internal const string Unbound = "未绑定";
    public string Key => Document.TryGetProperty("hotkey", out var key) ? key.GetString()! : Unbound;
    public IReadOnlyList<string> Keys => Choices;
    private static readonly string[] Choices = [Unbound, .. "ABCDEFGHIJKLMNOPQRSTUVWXYZ".Select(c => c.ToString()), .. Enumerable.Range(1, 12).Select(n => $"F{n}")];
    public string RunId => $"Run_{Id}";
    public string EditId => $"Edit_{Id}";
    public string DeleteId => $"Delete_{Id}";
    public string KeyId => $"Key_{Id}";
    internal string? Execution { get; set; }
    internal string? RunStatus { get; set; }
    public string Status => Execution == null ? "已停止" : StatusText(RunStatus);
    public string Action => Execution == null ? "开始" : "停止";
    public Visibility EditVisibility { get; private set; }
    public event PropertyChangedEventHandler? PropertyChanged;
    internal void Refresh() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
    internal void Update(JsonElement value) { Document = value; Refresh(); }
    internal void ShowEdit(bool show) { EditVisibility = show ? Visibility.Visible : Visibility.Collapsed; Refresh(); }

    internal static string StatusText(string? status) => status switch {
        "starting" => "正在启动", "running" => "运行中", "paused" or "debug_paused" => "已暂停",
        "stopping" => "正在停止", "completed" => "已完成", "cancelled" => "已停止", "failed" => "执行失败", _ => status ?? "正在启动"
    };
}
