using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace ScreenTrigger.User;

internal enum FieldKind { Integer, Milliseconds, Text, Button, Key, Target }

internal sealed record StepField(string Label, FieldKind Kind, string Initial = "", int Min = int.MinValue, int Max = int.MaxValue)
{
    internal static readonly string[] Keys = [.. "abcdefghijklmnopqrstuvwxyz0123456789".Select(c => c.ToString()),
        .. Enumerable.Range(1, 12).Select(i => $"f{i}"), "enter", "space", "tab", "backspace", "delete", "esc",
        "up", "down", "left", "right", "ctrl", "shift", "alt", "win", "home", "end", "pageup", "pagedown", "insert", "printscreen"];
    internal bool Numeric => Kind is FieldKind.Integer or FieldKind.Milliseconds;

    internal string Literal(string value)
    {
        var culture = CultureInfo.InvariantCulture;
        if (Kind == FieldKind.Integer && int.TryParse(value, NumberStyles.AllowLeadingSign, culture, out int integer)
            && integer >= Min && integer <= Max) return integer.ToString(culture);
        if (Kind == FieldKind.Milliseconds && decimal.TryParse(value, NumberStyles.AllowDecimalPoint, culture, out decimal ms)
            && ms >= Min && ms <= Max) return (ms / 1000m).ToString("0.############################", culture);
        if (Numeric) throw new FormatException($"{Label}需要填写 {Min} 到 {Max} 之间的{(Kind == FieldKind.Integer ? "整数" : "数值")}。");
        bool valid = Kind switch {
            FieldKind.Text => value.Length > 0 && value.EnumerateRunes().Count() <= 10000,
            FieldKind.Button => value is "left" or "right" or "middle",
            FieldKind.Key => Keys.Contains(value), _ => !string.IsNullOrWhiteSpace(value)
        };
        if (!valid) throw new FormatException($"请检查{Label}。");
        var literal = new StringBuilder("\"");
        foreach (char ch in value)
            literal.Append(ch switch { '"' => "\\\"", '\\' => "\\\\", '\n' => "\\n", '\r' => "\\r", '\t' => "\\t",
                < ' ' or '\u007f' => $"\\x{(int)ch:X2}", _ => ch.ToString() });
        return literal.Append('"').ToString();
    }

    internal string ReadLiteral(string literal)
    {
        if (Kind == FieldKind.Milliseconds)
            return (decimal.Parse(literal, CultureInfo.InvariantCulture) * 1000m).ToString("0.########################", CultureInfo.InvariantCulture);
        if (Numeric) return literal;
        return Regex.Replace(literal[1..^1], @"\\(x[0-9A-F]{2}|[\\nrt""])", m => m.Groups[1].Value switch {
            "n" => "\n", "r" => "\r", "t" => "\t", var v when v.StartsWith('x') => ((char)Convert.ToInt32(v[1..], 16)).ToString(),
            var v => v });
    }
}

internal sealed class AssemblyAction
{
    internal string Name { get; }
    internal string Category { get; }
    internal StepField[] Fields { get; }
    private readonly string template;
    private readonly Regex matcher;
    private static readonly Regex Slot = new(@"@(\d+)");
    private static int Index(Match m) => int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture);

    internal AssemblyAction(string name, string category, string template, params StepField[] fields)
    {
        Name = name; Category = category; this.template = template; Fields = fields;
        var pattern = new StringBuilder(@"\A");
        int offset = 0;
        foreach (Match m in Slot.Matches(template))
        {
            pattern.Append(Regex.Escape(template[offset..m.Index]));
            pattern.Append(fields[Index(m)].Numeric ? @"(-?[0-9]+(?:\.[0-9]+)?)" : @"(""(?:[^""\\]|\\(?:x[0-9A-F]{2}|[\\nrt""]))*"")");
            offset = m.Index + m.Length;
        }
        matcher = new Regex(pattern.Append(Regex.Escape(template[offset..])).Append(@"\z").ToString(),
            RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
    }

    internal ScriptStep Create(string[] values) => new(this, values, Slot.Replace(template, m => Fields[Index(m)].Literal(values[Index(m)])));

    internal ScriptStep? Read(string line)
    {
        var match = matcher.Match(line);
        if (!match.Success) return null;
        try
        {
            var values = Fields.Select((field, i) => field.ReadLiteral(match.Groups[i + 1].Value)).ToArray();
            var step = Create(values);
            return step.Source == line ? step : null;
        }
        catch (Exception ex) when (ex is FormatException or OverflowException) { return null; }
    }
}

internal sealed record ScriptStep(AssemblyAction? Action, string[] Values, string Source)
{
    public string Title => Action?.Name ?? "自定义 Lua";
    internal string Summary(Func<string, string?> targetName)
    {
        if (Action == null) return Source.Split(['\r', '\n'], 2)[0];
        if (Source == ScriptAssembly.ReadOcrSource) return "将识别文字写入运行日志";
        if (Source == ScriptAssembly.ExportSource) return "运行时选择 TXT 文件";
        return string.Join(" · ", Action.Fields.Select((field, i) => field.Kind switch {
            FieldKind.Target => targetName(Values[i]) ?? $"目标 {Values[i]}",
            FieldKind.Button => Values[i] switch { "left" => "左键", "right" => "右键", _ => "中键" },
            FieldKind.Milliseconds => Values[i] + " ms",
            FieldKind.Integer => $"{field.Label} {Values[i]}",
            _ => Values[i].ReplaceLineEndings(" ")
        }));
    }
}

internal static class ScriptAssembly
{
    private static StepField Number(string label, string initial = "0", int min = int.MinValue, int max = int.MaxValue)
        => new(label, FieldKind.Integer, initial, min, max);
    private static StepField Time(string label, string initial, int max)
        => new(label, FieldKind.Milliseconds, initial, 0, max);
    private static readonly StepField Target = new("目标", FieldKind.Target);
    internal const string ReadOcrSource = "for _, block in ipairs(ocr(nil, nil, nil, false)) do log(block.text) end";
    internal const string ExportSource = "ocr(nil, nil, nil, true)";
    internal static readonly AssemblyAction[] Actions = [
        new("鼠标点击", "鼠标与键盘", "click_at(@0, @1, @2, @3)", Number("X"), Number("Y"), new("鼠标按钮", FieldKind.Button, "left"), Number("点击次数", "1", 1, 2)),
        new("鼠标移动", "鼠标与键盘", "move_to(@0, @1, @2)", Number("X"), Number("Y"), Time("移动时长（ms）", "200", 5000)),
        new("鼠标拖拽", "鼠标与键盘", "drag(@0, @1, @2, @3, @4)", Number("起点 X"), Number("起点 Y"), Number("终点 X"), Number("终点 Y"), Time("拖拽时长（ms）", "500", 5000)),
        new("鼠标滚动", "鼠标与键盘", "scroll(@0)", Number("滚轮格数", "1", -100, 100)),
        new("键盘按键", "鼠标与键盘", "press_key(@0)", new StepField("按键", FieldKind.Key, "enter")),
        new("输入文本", "鼠标与键盘", "type_text(@0)", new StepField("文本", FieldKind.Text)),
        new("等待", "流程控制", "wait(@0)", Time("等待时间（ms）", "500", int.MaxValue)),
        new("等待目标", "流程控制", "if not wait_for_target(@0, @1) then stop_script(\"等待目标超时\") end", Target, Time("超时（ms，0 表示持续等待）", "5000", int.MaxValue)),
        new("目标识别", "识别与导出", "do local found = find_target(@0) log(found and \"找到目标\" or \"未找到目标\") end", Target),
        new("点击目标", "识别与导出", "click_target(@0)", Target),
        new("颜色识别", "识别与导出", "do local found = find_color({color_space=\"rgb\", color={r=@0, g=@1, b=@2}}) log(found and \"找到颜色\" or \"未找到颜色\") end",
            Number("红", "255", 0, 255), Number("绿", "0", 0, 255), Number("蓝", "0", 0, 255)),
        new("OCR 识别", "识别与导出", ReadOcrSource),
        new("OCR 导出", "识别与导出", ExportSource)
    ];

    // Only a complete standalone instruction belongs to the visual editor. Keep the first
    // unknown line and everything after it together, including multiline Lua strings/functions.
    internal static List<ScriptStep> Read(string source)
    {
        List<ScriptStep> result = [];
        for (int at = 0; at < source.Length;)
        {
            int end = source.IndexOfAny(['\r', '\n'], at);
            var line = end < 0 ? source[at..] : source[at..end];
            var step = Actions.Select(a => a.Read(line)).FirstOrDefault(s => s != null);
            if (step == null) { result.Add(new(null, [], source[at..])); break; }
            result.Add(step);
            if (end < 0) break;
            at = end + (source.AsSpan(end).StartsWith("\r\n") ? 2 : 1);
        }
        return result;
    }
}
