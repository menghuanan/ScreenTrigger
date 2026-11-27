using System.Text.Json;

namespace ScreenTrigger.Host;

public sealed record CapabilityRow(string Id, string Summary, string Reason)
{
    public string AutomationId => $"Capability_{Id}";
}

internal static class EnginePresentation
{
    internal static string State(string state) => state switch
    {
        "ready" => "引擎就绪", "idle" => "引擎未启动", "starting" => "正在启动…",
        "stopping" => "正在停止…", "exited" => "引擎已退出", _ => state
    };

    internal static string Versions(JsonElement versions) => versions.ValueKind == JsonValueKind.Object
        ? $"引擎 {versions.GetProperty("engine_version")}　·　协议 {versions.GetProperty("protocol_version")}　·　{versions.GetProperty("lua_version")}" : "";

    internal static bool HasOcr(JsonElement capabilities) => capabilities.EnumerateArray().Any(cap =>
        cap.GetProperty("id").GetString() == "ocr" && cap.GetProperty("state").GetString() == "available");

    internal static List<CapabilityRow> Capabilities(JsonElement capabilities) => capabilities.EnumerateArray().Select(cap =>
    {
        var id = cap.GetProperty("id").GetString() ?? "";
        var label = id switch { "ocr" => "文字识别（OCR）", "lua_script" => "Lua 脚本", _ => id };
        var state = cap.GetProperty("state").GetString() switch
        { "available" => "可用", "pending" => "待确认", _ => "不可用" };
        var reason = cap.GetProperty("reason").GetString() ?? "";
        return new CapabilityRow(id, $"{label} · {state}", id == "ocr" ? OcrDescription(reason) : reason);
    }).ToList();

    private static string OcrDescription(string reason)
    {
        string[] required = ["state", "code", "models", "verified", "unverified", "models_dir", "library", "detail"];
        var parts = reason.Split('|');
        if (parts.Length != required.Length + 1 || parts[0] != "OCR") return reason;
        var fields = new Dictionary<string, string>();
        foreach (var part in parts.Skip(1))
        {
            int equal = part.IndexOf('=');
            if (equal < 1 || !required.Contains(part[..equal]) || !fields.TryAdd(part[..equal], part[(equal + 1)..]))
                return reason;
        }
        static string Languages(string list) => list is "" or "-" ? "无" : DescribeLanguages(list.Split(','));
        var lines = new List<string> { $"已验证语言：{Languages(fields["verified"])}", $"待验证语言：{Languages(fields["unverified"])}" };
        if (fields["code"] != "-") lines.Add(fields["code"]);
        if (!string.IsNullOrWhiteSpace(fields["detail"])) lines.Add(fields["detail"]);
        return string.Join("\n", lines);
    }

    private static string DescribeLanguages(IEnumerable<string> ids)
    {
        var description = string.Join("、", ids.Select(id => id switch
        { "zh" => "中文", "en" => "英文", "ja" => "日文", "ko" => "韩文", _ => id }));
        return description.Length == 0 ? "无" : description;
    }

    internal static async Task<string> ReadModels(HostClient host)
    {
        var hello = await host.HelloAsync().ConfigureAwait(false);
        var manifests = hello.ModelManifests.Where(m => m.ProviderId == "builtin.ocr").ToArray();
        var ids = manifests.SelectMany(m => m.ModelIds).Distinct().ToArray();
        var verified = manifests.SelectMany(m => m.SupportedLanguages).Distinct().ToArray();
        string[] firstBatch = ["zh", "en", "ja", "ko"];
        return ids.Length == 0 ? "引擎未返回已安装的 OCR 模型。" :
            $"已验证语言：{DescribeLanguages(verified)}\n待验证语言：{DescribeLanguages(firstBatch.Except(verified))}\n{string.Join("\n", ids)}";
    }

    internal static async Task<string> ReadDiagnostics(HostClient host)
    {
        var info = await host.CallAsync(new { op = "diagnostics" }).ConfigureAwait(false);
        string? session = info.GetProperty("session_id").GetString();
        string handshake = session != null && host.Events.Session == session ? "已收到 · seq 1" : "未收到";
        string revision = "—";
        if (info.GetProperty("state").GetString() == "ready")
        {
            try { revision = (await host.GetConfigAsync().ConfigureAwait(false)).Revision.ToString(); }
            catch (Exception ex) { revision = ex.Message; }
        }
        return $"会话　{session ?? "—"}\n进程　{info.GetProperty("pid")}\n就绪事件　{handshake}\n" +
            $"配置修订　{revision}\n进程树托管　{info.GetProperty("job_object_active")}\n协议异常行　{info.GetProperty("malformed_line_count")}\n" +
            $"最近退出码　{info.GetProperty("exit_code")}";
    }
}
