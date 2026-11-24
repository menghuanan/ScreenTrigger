using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ScreenTrigger.Contracts;

internal static class WireContracts
{
    internal static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        RespectNullableAnnotations = true, RespectRequiredConstructorParameters = true,
        AllowDuplicateProperties = false, AllowOutOfOrderMetadataProperties = true
    };

    public static T Read<T>(JsonElement value, string type)
    {
        Validate(type, value);
        return value.Deserialize<T>(Json) ?? throw new ContractException(type);
    }

    public static void Validate(string type, JsonElement value)
    {
        CheckJson(value);
        if (type == "config")
        {
            WireSchema.Config.Validate(value);
            RelativePath(value.GetProperty("recognition").GetProperty("model_directory").GetString()!, "recognition.model_directory");
            return;
        }
        string definition = type switch { "local_rect" => "localRect", "run_config" => "runConfig",
            "monitor_config" => "monitorConfig", _ => type };
        string path = type is "region" or "point" or "local_rect" ? type : "";
        if (type == "region")
        {
            if (value.ValueKind != JsonValueKind.Object || !value.TryGetProperty("space", out var space))
                throw new ContractException("region.space");
            definition = space.ToString() switch { "screen_physical" => "regionScreen", "window_client_physical" => "regionClient",
                "image_pixel" => "regionImage", _ => throw new ContractException("region.space") };
        }
        if ((type is "run_config" or "monitor_config") && value.ValueKind == JsonValueKind.Object)
        {
            if (value.TryGetProperty("binding_id", out var binding) && binding.ValueKind == JsonValueKind.String &&
                value.TryGetProperty("display_id", out var display) && display.ValueKind == JsonValueKind.String)
                throw new ContractException("binding_id");
            if (type == "monitor_config" && value.TryGetProperty("on_timeout", out var policy) && policy.ToString()=="run_script" &&
                (!value.TryGetProperty("timeout_script_id", out var id) || string.IsNullOrEmpty(id.ToString())))
                throw new ContractException("timeout_script_id");
        }
        try { WireSchema.Types.Validate(value, definition, path); }
        catch (ContractException ex) when (type == "target" && ex.Field.Contains(".children"))
        { throw new ContractException(ex.Field.Replace(".children", ""), ex.Code); }
        if (type is "target" or "script" or "group" && string.IsNullOrWhiteSpace(value.GetProperty("name").GetString()))
            throw new ContractException("name");
        if (type == "target")
        {
            var leaves = new HashSet<string>();
            CheckConditions(value.GetProperty("conditions"), 1, leaves);
            if (!leaves.Contains(value.GetProperty("anchor_condition_id").GetString()!)) throw new ContractException("anchor_condition_id");
            RelativePath(value.GetProperty("image").GetProperty("path").GetString()!, "image.path");
            if (value.GetProperty("thumbnail_path").ValueKind == JsonValueKind.String)
                RelativePath(value.GetProperty("thumbnail_path").GetString()!, "thumbnail_path");
            if (JsonSerializer.SerializeToUtf8Bytes(value.GetProperty("metadata")).Length > 16 * 1024)
                throw new ContractException("metadata");
        }
        if (type == "script")
        {
            RelativePath(value.GetProperty("source_path").GetString()!, "source_path");
            foreach (var p in value.GetProperty("parameters").EnumerateArray())
                if (p.GetProperty("required").GetBoolean() && p.GetProperty("default").ValueKind != JsonValueKind.Null)
                    throw new ContractException("parameters");
        }
        if (type is "run_config" or "monitor_config")
        {
            var region = value.GetProperty("region");
            var binding = value.GetProperty("binding_id");
            var display = value.GetProperty("display_id");
            if (binding.ValueKind == JsonValueKind.Null && string.IsNullOrEmpty(display.ToString())) throw new ContractException("display_id");
            if (region.ValueKind != JsonValueKind.Null)
            {
                string? space = region.GetProperty("space").GetString();
                if (space == "image_pixel") throw new ContractException("region.space");
                if (space == "window_client_physical" && !JsonElement.DeepEquals(region.GetProperty("binding_id"), binding))
                    throw new ContractException("region.binding_id");
                if (space == "screen_physical" && !JsonElement.DeepEquals(region.GetProperty("display_id"), display))
                    throw new ContractException("region.display_id");
            }
        }
    }

    private static void CheckConditions(JsonElement node, int depth, HashSet<string> leaves)
    {
        if (depth > 4) throw new ContractException("conditions");
        if (node.TryGetProperty("children", out var children))
        {
            foreach (var child in children.EnumerateArray()) CheckConditions(child, depth+1, leaves);
            return;
        }
        if (!leaves.Add(node.GetProperty("id").GetString()!) || leaves.Count > 10) throw new ContractException("conditions");
        var p = node.GetProperty("params");
        if (node.GetProperty("type").GetString() == "contour" &&
            p.TryGetProperty("canny_low", out var low) && p.TryGetProperty("canny_high", out var high) && low.GetInt32() >= high.GetInt32())
            throw new ContractException("conditions.params.canny_high");
    }

    internal static void CheckJson(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Number && !double.IsFinite(value.GetDouble())) throw new ContractException("number");
        if (value.ValueKind == JsonValueKind.Array) foreach (var item in value.EnumerateArray()) CheckJson(item);
        if (value.ValueKind != JsonValueKind.Object) return;
        var names = new HashSet<string>(StringComparer.Ordinal);
        foreach (var p in value.EnumerateObject())
        {
            if (!names.Add(p.Name)) throw new ContractException(p.Name);
            CheckJson(p.Value);
        }
    }

    private static void RelativePath(string path, string field)
    {
        var parts = path.Replace('\\','/').Split('/');
        if (string.IsNullOrWhiteSpace(path) || parts[0].Length == 0 || parts[0].Contains(':') || parts.Any(p => p is "." or ".."))
            throw new ContractException(field);
    }
}
