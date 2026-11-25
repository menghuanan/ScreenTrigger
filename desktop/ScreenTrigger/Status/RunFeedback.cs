using System.Text.Json;

namespace ScreenTrigger.Status;

internal static class RunFeedback
{
    internal static string Name(string? status) => status switch {
        "starting" => "正在启动", "running" => "运行中", "paused" => "已暂停", "debug_paused" => "调试暂停",
        "stopping" => "正在停止", "completed" => "已完成", "cancelled" => "已取消", "failed" => "失败", _ => status ?? ""
    };

    internal static string Stats(JsonElement data) =>
        $"{(data.GetProperty("last_found").GetBoolean() ? "命中" : "未命中")} · 实际 {data.GetProperty("effective_hz").GetDouble():0.##} Hz" +
        (data.GetProperty("throttled").GetBoolean() ? " · 已节流" : "");

    internal static string Match(JsonElement result)
    {
        var matches = result.GetProperty("matches");
        if (matches.GetArrayLength() == 0) return "未命中";
        var rect = matches[0].GetProperty("bbox");
        return $"命中 #{result.GetProperty("sample_no")} · ({rect.GetProperty("x")}, {rect.GetProperty("y")}) · {rect.GetProperty("width")} × {rect.GetProperty("height")}";
    }

    internal static string State(JsonElement state)
    {
        string kind = state.GetProperty("kind").GetString() == "monitor" ? "监控" : "脚本";
        string text = kind + " · " + Name(state.GetProperty("status").GetString());
        if (state.TryGetProperty("execution_id", out var id) && id.GetString() is string key)
            text += " · " + key[..Math.Min(8, key.Length)];
        var reason = state.GetProperty("reason").GetString();
        if (reason != null) text += " · " + (reason switch {
            "target_detected" => "目标已命中", "timeout" => "已超时", "timeout_handled" => "已执行超时处理",
            "user_requested" => "用户操作", "window_unavailable" => "等待窗口恢复", _ => reason
        });
        if (state.GetProperty("effective_hz").ValueKind == JsonValueKind.Number)
            text += $" · 实际 {state.GetProperty("effective_hz").GetDouble():0.##} Hz" +
                (state.GetProperty("throttled").GetBoolean() ? " · 已节流" : "");
        var result = state.GetProperty("result");
        if (kind == "监控" && result.ValueKind == JsonValueKind.Object && result.GetProperty("last_match").ValueKind == JsonValueKind.Object)
            text += " · " + Match(result.GetProperty("last_match"));
        var error = state.GetProperty("error");
        if (error.ValueKind == JsonValueKind.Object)
        {
            text += $" · {error.GetProperty("code")} · {error.GetProperty("message")}";
            if (error.TryGetProperty("details", out var details) && details.ValueKind == JsonValueKind.Object && details.EnumerateObject().Any())
                text += $" · {details}";
            if (error.GetProperty("code").GetString() == "INPUT_FAILED") text += "。请检查目标窗口后决定是否重新运行。";
        }
        return text;
    }
}
