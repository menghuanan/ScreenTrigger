using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ScreenTrigger.Contracts;

internal sealed class ContractException(string field, string code = "INVALID_PARAMS")
    : Exception($"{code} · 字段 {field} 不符合协议。")
{
    public string Code { get; } = code;
    public string Field { get; } = field;
}

// Interpret the predicates used by the project's two embedded Draft 7 schemas.
internal sealed class WireSchema
{
    internal static readonly WireSchema Types = new("types");
    internal static readonly WireSchema Config = new("config");
    private readonly JsonElement root;

    private WireSchema(string name)
    {
        using var stream = typeof(WireSchema).Assembly.GetManifestResourceStream($"ScreenTrigger.{name}.json")
            ?? throw new InvalidDataException($"找不到内置 {name} 契约。");
        using var doc = JsonDocument.Parse(stream);
        root = doc.RootElement.Clone();
    }

    public void Validate(JsonElement value, string? definition = null, string path = "")
    {
        Check(definition == null ? root : Definition(definition), value, path);
    }

    private JsonElement Definition(string name) => root.GetProperty("definitions").GetProperty(name);
    private static string Field(string path, string name) => path.Length == 0 ? name : $"{path}.{name}";

    private void Check(JsonElement rule, JsonElement value, string path)
    {
        if (rule.TryGetProperty("$ref", out var reference))
        {
            string name = reference.GetString()!.Split('/')[^1];
            if (name == "region")
            {
                if (value.ValueKind != JsonValueKind.Object || !value.TryGetProperty("space", out var space))
                    throw new ContractException(Field(path, "space"));
                name = space.ToString() switch
                {
                    "screen_physical" => "regionScreen", "window_client_physical" => "regionClient",
                    "image_pixel" => "regionImage", _ => throw new ContractException(Field(path, "space"))
                };
            }
            if (name == "conditionNode")
                name = value.ValueKind == JsonValueKind.Object && value.TryGetProperty("op", out _) ? "conditionBranch" : "conditionLeaf";
            Check(Definition(name), value, path);
            return;
        }
        if (rule.TryGetProperty("oneOf", out var union))
        {
            // The untagged project unions select a nullable value or a typed branch.
            ContractException? best = null;
            int matches = 0;
            foreach (var branch in union.EnumerateArray())
            {
                try { Check(branch, value, path); matches++; }
                catch (ContractException e) { if (best == null || e.Field.Length > best.Field.Length) best = e; }
            }
            if (matches != 1) throw best ?? new ContractException(path);
        }
        if (rule.TryGetProperty("type", out var type))
        {
            bool valid = type.ValueKind == JsonValueKind.Array
                ? type.EnumerateArray().Any(t => Is(value, t.GetString())) : Is(value, type.GetString());
            if (!valid) throw new ContractException(path);
        }
        if (rule.TryGetProperty("enum", out var choices) && !choices.EnumerateArray().Any(c => JsonElement.DeepEquals(c, value)))
            throw new ContractException(path);
        if (rule.TryGetProperty("const", out var constant) && !JsonElement.DeepEquals(constant, value))
            throw new ContractException(path);
        if (value.ValueKind == JsonValueKind.Object)
        {
            if (rule.TryGetProperty("required", out var required))
                foreach (var item in required.EnumerateArray())
                    if (!value.TryGetProperty(item.GetString()!, out _)) throw new ContractException(Field(path, item.GetString()!));
            if (rule.TryGetProperty("properties", out var properties))
            {
                bool strict = rule.TryGetProperty("additionalProperties", out var extra) && extra.ValueKind == JsonValueKind.False;
                foreach (var member in value.EnumerateObject())
                {
                    string field = Field(path, member.Name);
                    if (properties.TryGetProperty(member.Name, out var child)) Check(child, member.Value, field);
                    else if (strict) throw new ContractException(field);
                }
            }
        }
        if (value.ValueKind == JsonValueKind.Array)
        {
            int length = value.GetArrayLength();
            if (Below(rule, "minItems", length) || Above(rule, "maxItems", length)) throw new ContractException(path);
            if (rule.TryGetProperty("items", out var items))
                foreach (var item in value.EnumerateArray()) Check(items, item, path);
        }
        if (value.ValueKind == JsonValueKind.String)
        {
            string s = value.GetString()!;
            int length = s.EnumerateRunes().Count();
            if (Below(rule, "minLength", length) || Above(rule, "maxLength", length) ||
                rule.TryGetProperty("pattern", out var pattern) && !Regex.IsMatch(s, pattern.GetString()!, RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)))
                throw new ContractException(path);
        }
        if (value.ValueKind == JsonValueKind.Number)
        {
            double n = value.GetDouble();
            if (!double.IsFinite(n) || Below(rule, "minimum", n) || Above(rule, "maximum", n) ||
                rule.TryGetProperty("exclusiveMinimum", out var floor) && n <= floor.GetDouble())
                throw new ContractException(path, path is "region.width" or "region.height" ? "INVALID_REGION" : "INVALID_PARAMS");
        }
        if (rule.TryGetProperty("allOf", out var all)) foreach (var item in all.EnumerateArray()) Check(item, value, path);
        if (rule.TryGetProperty("if", out var condition) && Satisfies(condition, value) && rule.TryGetProperty("then", out var consequent))
            Check(consequent, value, path);
        if (rule.TryGetProperty("not", out var excluded) && Satisfies(excluded, value)) throw new ContractException(path);
    }

    private bool Satisfies(JsonElement rule, JsonElement value)
    {
        try { Check(rule, value, ""); return true; }
        catch (ContractException) { return false; }
    }

    private static bool Below(JsonElement s, string k, double n) => s.TryGetProperty(k, out var bound) && n < bound.GetDouble();
    private static bool Above(JsonElement s, string k, double n) => s.TryGetProperty(k, out var bound) && n > bound.GetDouble();
    private static bool Is(JsonElement value, string? type) => type switch
    {
        "integer" => value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out _),
        "number" => value.ValueKind == JsonValueKind.Number && double.IsFinite(value.GetDouble()),
        "object" => value.ValueKind == JsonValueKind.Object, "array" => value.ValueKind == JsonValueKind.Array,
        "string" => value.ValueKind == JsonValueKind.String, "null" => value.ValueKind == JsonValueKind.Null,
        "boolean" => value.ValueKind is JsonValueKind.True or JsonValueKind.False,
        _ => throw new InvalidDataException($"未知内置契约类型 {type}。")
    };
}
