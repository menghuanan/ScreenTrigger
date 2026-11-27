using System.Text.Json;

namespace ScreenTrigger.User;

public sealed class ScriptRow(JsonElement document)
{
    public JsonElement Document { get; } = document;
    public string Id => Document.GetProperty("id").GetString()!;
    public string Name => Document.GetProperty("name").GetString()!;
    public override string ToString() => Name;
    public long Revision => Document.GetProperty("revision").GetInt64();
    public string EditId => $"Edit_{Id}";
    public string DeleteId => $"Delete_{Id}";
    public string RevisionLabel => $"修订 {Revision}";
    internal static string StatusText(string? status) => status switch {
        "starting" => "正在启动", "running" => "运行中", "paused" or "debug_paused" => "已暂停",
        "stopping" => "正在停止", "completed" => "已完成", "cancelled" => "已停止", "failed" => "执行失败", _ => status ?? "正在启动"
    };
}
