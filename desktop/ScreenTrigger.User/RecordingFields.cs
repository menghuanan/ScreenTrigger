using System.Globalization;
using System.Text.RegularExpressions;

namespace ScreenTrigger.User;

// The editable rows carry every part of a recording, including intermediate drag points
// and scan-code transitions. Recording-only expressions never become absolute coordinates.
internal sealed record RecordingPoint(string Scope, string Strategy, int Width, int Height, int X, int Y)
{
    internal static RecordingPoint Parse(string line)
    {
        var cells = line.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (cells.Length == 6 && cells[0] is "window" or "display" or "region" && cells[1] is "relative" or "offset"
            && int.TryParse(cells[2], out int w) && int.TryParse(cells[3], out int h)
            && int.TryParse(cells[4], out int x) && int.TryParse(cells[5], out int y)
            && x >= 0 && x < w && y >= 0 && y < h) return new(cells[0], cells[1], w, h, x, y);
        throw new FormatException("每行填写范围、策略、参考宽、参考高、X、Y。坐标须在参考尺寸内。");
    }
    internal string Row => FormattableString.Invariant($"{Scope} {Strategy} {Width} {Height} {X} {Y}");
    internal string Lua => FormattableString.Invariant($"{{version=1,scope=\"{Scope}\",strategy=\"{Strategy}\",reference_width={Width},reference_height={Height},x={X},y={Y}}}");

    internal (int X, int Y) FromScreen(int x, int y, int left, int top, int width, int height)
    {
        int Axis(long local, int current, int reference)
        {
            if (local < 0 || local >= current) throw new FormatException("取点超出所选操作区域，请重新取点。");
            long n = Strategy == "offset" ? local : current == 1 ? 0 : (local * (reference - 1) + (current - 1L) / 2) / (current - 1);
            if (n >= reference) throw new FormatException("取点超出录制时的参考尺寸。");
            return (int)n;
        }
        return (Axis((long)x - left, width, Width), Axis((long)y - top, height, Height));
    }
}

internal static class RecordingFields
{
    private static IEnumerable<Dictionary<string,string>> Records(string table)
    {
        var tokens = LuaSyntax.Lex(table);
        int i = 0;
        string Peek() => i < tokens.Count ? tokens[i].Text : "";
        void Need(string token) { if (Peek() != token) throw new FormatException(); i++; }
        Need("{");
        while (Peek() == "{")
        {
            Need("{"); Dictionary<string,string> fields = [];
            while (Peek() != "}")
            {
                if (i + 2 >= tokens.Count) throw new FormatException();
                string name = tokens[i++].Text; Need("="); string value = tokens[i++].Text;
                if (!fields.TryAdd(name, LuaSyntax.IsString(value) ? StepField.Unquote(value) : value)) throw new FormatException();
                if (Peek() is not ("," or ";")) break;
                i++;
            }
            Need("}"); yield return fields;
            if (Peek() is not ("," or ";")) break;
            i++;
        }
        Need("}"); if (i != tokens.Count) throw new FormatException();
    }

    internal static string Describe(FieldKind kind, string lua)
    {
        List<string> lines = [];
        foreach (var record in Records(lua))
        {
            string Get(string k) => record.TryGetValue(k, out var v) ? v : throw new FormatException();
            if (kind == FieldKind.Path)
            {
                if (record.Count != 7 || Get("version") != "1") throw new FormatException();
                lines.Add(RecordingPoint.Parse(string.Join(" ", new[]{"scope","strategy","reference_width","reference_height","x","y"}.Select(Get))).Row);
            }
            else
            {
                string verb = Get("down") switch { "true" => "按下", "false" => "抬起", _ => throw new FormatException() };
                string key = record.ContainsKey("key") && record.Count == 3 ? Get("key") :
                    record.Count == 4 ? "scan:" + Get("scan_code") + (Get("extended") switch { "true" => ":ext", "false" => "", _ => throw new FormatException() }) : throw new FormatException();
                lines.Add($"{Get("offset_ms")} {verb} {key}");
            }
        }
        return string.Join("\n", lines);
    }

    internal static string Literal(FieldKind kind, string value)
    {
        string[] lines = value.ReplaceLineEndings("\n").Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (lines.Length < 2 || lines.Length > (kind == FieldKind.Path ? 8194 : 8192)) throw new FormatException("请保留完整的录制序列（至少两行）。");
        if (kind == FieldKind.Path) return "{" + string.Join(",", lines.Select(s => RecordingPoint.Parse(s).Lua)) + "}";
        var events = new List<string>(); var held = new HashSet<string>();
        int previous = 0;
        foreach (var line in lines)
        {
            var cells = line.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
            if (cells.Length != 3 || !int.TryParse(cells[0], NumberStyles.None, CultureInfo.InvariantCulture, out int time)
                || time < previous || time > 1800000 || events.Count == 0 && time != 0 || cells[1] is not ("按下" or "抬起"))
                throw new FormatException("每行填写毫秒、按下或抬起、键名。时间从 0 开始递增。");
            string key = cells[2], literal;
            if (StepField.Keys.Contains(key) || key is "convert" or "nonconvert" or "kana") literal = $"key=\"{key}\"";
            else
            {
                var scan = Regex.Match(key, @"\Ascan:(\d+)(:ext)?\z");
                if (!scan.Success || !int.TryParse(scan.Groups[1].Value, out int code) || code is < 1 or > 127)
                    throw new FormatException("请填写键名或扫描码，例如 ctrl、scan:30、scan:29:ext。");
                literal = $"scan_code={code},extended={(scan.Groups[2].Success ? "true" : "false")}";
            }
            bool down = cells[1] == "按下";
            if (down) held.Add(key); else if (!held.Remove(key)) throw new FormatException("按键抬起前需要有对应的按下事件。");
            events.Add(FormattableString.Invariant($"{{offset_ms={time},down={(down ? "true" : "false")},{literal}}}"));
            if (held.Count == 0 && events.Count < lines.Length) throw new FormatException("每组按键应在最后一行释放完毕。");
            previous = time;
        }
        if (held.Count != 0) throw new FormatException("请补全按键的抬起事件。");
        return "{" + string.Join(",", events) + "}";
    }
}
