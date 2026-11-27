using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace ScreenTrigger.User;

internal enum FieldKind { Integer, Milliseconds, Text, Button, Key, Target, Scope, Strategy, Path, KeyEvents }

internal sealed record StepField(string Label, FieldKind Kind, string Initial = "", int Min = int.MinValue, int Max = int.MaxValue)
{
    internal static readonly string[] Keys = [.. "abcdefghijklmnopqrstuvwxyz0123456789".Select(c => c.ToString()),
        .. Enumerable.Range(1, 12).Select(i => $"f{i}"), "enter", "space", "tab", "backspace", "delete", "esc",
        "up", "down", "left", "right", "ctrl", "shift", "alt", "win", "home", "end", "pageup", "pagedown", "insert", "printscreen"];
    internal bool Numeric => Kind is FieldKind.Integer or FieldKind.Milliseconds;
    internal bool Table => Kind is FieldKind.Path or FieldKind.KeyEvents;

    internal string Literal(string value)
    {
        if (Table) return RecordingFields.Literal(Kind, value);
        var culture = CultureInfo.InvariantCulture;
        if (Kind == FieldKind.Integer && int.TryParse(value, NumberStyles.AllowLeadingSign, culture, out int integer)
            && integer >= Min && integer <= Max) return integer.ToString(culture);
        if (Kind == FieldKind.Milliseconds && decimal.TryParse(value, NumberStyles.AllowDecimalPoint, culture, out decimal ms)
            && ms >= Min && ms <= Max) return (ms / 1000m).ToString("G29", culture);
        if (Numeric) throw new FormatException($"{Label}需要填写 {Min} 到 {Max} 之间的{(Kind == FieldKind.Integer ? "整数" : "数值")}。");
        bool valid = Kind switch {
            FieldKind.Text => value.Length > 0 && value.EnumerateRunes().Count() <= 10000,
            FieldKind.Button => value is "left" or "right" or "middle",
            FieldKind.Scope => value is "display" or "window" or "region",
            FieldKind.Strategy => value is "offset" or "relative",
            FieldKind.Key => Keys.Contains(value), _ => !string.IsNullOrWhiteSpace(value)
        };
        if (!valid) throw new FormatException($"请检查{Label}。");
        var literal = new StringBuilder("\"");
        foreach (char ch in value)
            literal.Append(ch switch { '"' => "\\\"", '\\' => "\\\\", '\n' => "\\n", '\r' => "\\r", '\t' => "\\t",
                < ' ' or '\u007f' => $"\\x{(int)ch:X2}", _ => ch.ToString() });
        return literal.Append('"').ToString();
    }

    internal static string Unquote(string s)
    {
        if (!LuaSyntax.IsString(s)) throw new FormatException();
        if (s[0] == '[')
        {
            int open = s.IndexOf('[', 1);
            if (open < 1 || s.Length < 2 * (open + 1) || !s.EndsWith("]" + s[1..open] + "]", StringComparison.Ordinal)) throw new FormatException();
            var body = s[(open + 1)..^(open + 1)].ReplaceLineEndings("\n");
            return body.StartsWith('\n') ? body[1..] : body;
        }
        if (s[^1] != s[0]) throw new FormatException();
        StringBuilder text = new(); List<byte> bytes = [];
        void Flush() { if (bytes.Count > 0) { try { text.Append(new UTF8Encoding(false,true).GetString(bytes.ToArray())); } catch (DecoderFallbackException) { throw new FormatException(); } bytes.Clear(); } }
        for (int i = 1; i < s.Length - 1; i++)
        {
            char c = s[i];
            if (c != '\\') { Flush(); text.Append(c); continue; }
            if (++i >= s.Length - 1) throw new FormatException();
            c = s[i];
            if (c == 'x' && i + 2 < s.Length - 1 && byte.TryParse(s.AsSpan(i+1,2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var hex))
            { bytes.Add(hex); i += 2; continue; }
            if (char.IsAsciiDigit(c))
            {
                int first = i;
                while (i + 1 < s.Length - 1 && i < first + 2 && char.IsAsciiDigit(s[i+1])) i++;
                if (!byte.TryParse(s.AsSpan(first,i-first+1), out var b)) throw new FormatException();
                bytes.Add(b); continue;
            }
            Flush();
            if (c == 'z') { while (i+1 < s.Length-1 && char.IsWhiteSpace(s[i+1])) i++; continue; }
            if (c == 'u' && i+1 < s.Length && s[i+1] == '{')
            {
                int end = s.IndexOf('}', i+2);
                if (end < 0 || !int.TryParse(s.AsSpan(i+2,end-i-2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out int rune) || !Rune.IsValid(rune)) throw new FormatException();
                text.Append(new Rune(rune)); i=end; continue;
            }
            if (c == '\r' && i+1 < s.Length && s[i+1] == '\n') i++;
            text.Append(c switch { 'n' or '\r' or '\n' => '\n', 'r'=>'\r', 't'=>'\t', 'a'=>'\a', 'b'=>'\b', 'f'=>'\f', 'v'=>'\v',
                '\\' or '\'' or '"' => c, _ => throw new FormatException() });
        }
        Flush(); return text.ToString();
    }

    internal string ReadLiteral(string literal)
    {
        if (Table) return RecordingFields.Describe(Kind, literal);
        if (!Numeric) return Unquote(literal);
        decimal n = decimal.Parse(literal, NumberStyles.Float, CultureInfo.InvariantCulture);
        if (Kind == FieldKind.Milliseconds) n *= 1000m;
        return n.ToString("0.############################", CultureInfo.InvariantCulture);
    }
}

internal sealed class AssemblyAction
{
    internal string Name { get; }
    internal string Category { get; }
    internal StepField[] Fields { get; }
    private readonly string template;
    private readonly List<LuaToken> pattern;
    internal string First => pattern[0].Text;
    private static readonly Regex Slot = new(@"@(\d+)");
    internal AssemblyAction(string name, string category, string template, params StepField[] fields)
    {
        Name = name; Category = category; this.template = template; Fields = fields; pattern = LuaSyntax.Lex(template);
    }

    internal ScriptStep Create(string[] values, ScriptStep? prior = null)
    {
        if (First is "click_anchor" or "scroll_anchor") RecordingPoint.Parse(string.Join(" ", values.Take(6)));
        var literals = Fields.Select((f, i) => f.Literal(values[i])).ToArray();
        if (prior?.Slots is { } spans)
        {
            var edited = new StringBuilder(prior.Source);
            for (int i = spans.Length - 1; i >= 0; i--)
                if (values[i] != prior.Values[i]) edited.Remove(spans[i].Start, spans[i].Length).Insert(spans[i].Start, literals[i]);
            return prior with { Values = values, Source = edited.ToString() };
        }
        return new(this, values, Slot.Replace(template, m => literals[int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture)]));
    }

    internal ScriptStep? Read(LuaSyntax doc, LuaStatement stmt)
    {
        var tokens = doc.Tokens; int at = stmt.First, start = tokens[at].Start;
        var values = new string[Fields.Length]; var spans = new (int Start,int Length)[Fields.Length];
        try
        {
            for (int p = 0; p < pattern.Count; p++)
            {
                if (at >= stmt.After) return null;
                string expected = pattern[p].Text, actual = tokens[at].Text;
                if (expected != "@")
                {
                    if (expected != actual && !(LuaSyntax.IsString(expected) && LuaSyntax.IsString(actual) && StepField.Unquote(actual) == StepField.Unquote(expected))) return null;
                    at++; continue;
                }
                int index = int.Parse(pattern[++p].Text, CultureInfo.InvariantCulture), begin = at;
                var field = Fields[index];
                if (field.Table)
                {
                    if (actual != "{") return null;
                    int nesting = 0;
                    do { string t = tokens[at++].Text; if (t == "{") nesting++; else if (t == "}") nesting--; }
                    while (at < stmt.After && nesting > 0);
                    if (nesting != 0) return null;
                }
                else { at++; if (field.Numeric && actual == "-") { if (at == stmt.After) return null; at++; } }
                string raw = doc.Source[tokens[begin].Start..tokens[at-1].End];
                values[index] = field.ReadLiteral(field.Numeric && at > begin+1 ? "-" + tokens[at-1].Text : raw);
                field.Literal(values[index]);
                spans[index] = (tokens[begin].Start - start, tokens[at-1].End - tokens[begin].Start);
            }
            if (at != stmt.After) return null;
            return new(this, values, doc.Source[start..tokens[at-1].End]) { Slots = spans };
        }
        catch (Exception ex) when (ex is FormatException or OverflowException) { return null; }
    }
}

internal sealed record ScriptStep(AssemblyAction? Action, string[] Values, string Source)
{
    internal string Prefix { get; init; } = "";
    internal string Label { get; init; } = "Lua 代码";
    internal int Depth { get; init; }
    internal bool Boundary { get; init; }
    internal (int Start,int Length)[]? Slots { get; init; }
    public string Title => Action?.Name ?? Label;
    internal string Summary(Func<string, string?> targetName)
    {
        if (Action == null) return Source.Split(['\r','\n'],2)[0];
        if (Action.Name == "OCR 识别") return "将识别文字写入运行日志";
        if (Action.Name == "OCR 导出") return "将识别文字写入所选 TXT 文件";
        return string.Join(" · ", Action.Fields.Select((field, i) => field.Kind switch {
            FieldKind.Target => targetName(Values[i]) ?? $"目标 {Values[i]}",
            FieldKind.Button => Values[i] switch { "left" => "左键", "right" => "右键", _ => "中键" },
            FieldKind.Path => $"{Values[i].Count(c => c == '\n') + 1} 个轨迹点",
            FieldKind.KeyEvents => $"{Values[i].Count(c => c == '\n') + 1} 个按键事件",
            FieldKind.Scope => Values[i] switch { "window" => "窗口", "region" => "选区", _ => "显示器" },
            FieldKind.Strategy => Values[i] == "relative" ? "相对定位" : "偏移定位",
            FieldKind.Milliseconds => Values[i] + " ms",
            FieldKind.Integer => $"{field.Label} {Values[i]}", _ => Values[i].ReplaceLineEndings(" ")
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

    private const string Anchor = "{version=1,scope=@0,strategy=@1,reference_width=@2,reference_height=@3,x=@4,y=@5}";
    private static readonly StepField[] AnchorFields = [new("定位范围", FieldKind.Scope), new("定位策略", FieldKind.Strategy),
        Number("参考宽度","1",1), Number("参考高度","1",1), Number("X","0",0), Number("Y","0",0)];
    private static AssemblyAction Optional(int index, string template, int fields)
        => new(Actions[index].Name, Actions[index].Category, template, Actions[index].Fields[..fields]);
    private static readonly ILookup<string, AssemblyAction> Matchers = Actions.Concat(new AssemblyAction[] {
        Optional(0, "click_at(@0,@1)", 2), Optional(0, "click_at(@0,@1,@2)", 3),
        Optional(1, "move_to(@0,@1)", 2), Optional(2, "drag(@0,@1,@2,@3)", 4),
        Optional(5, "type_text @0", 1),
        new("查找目标","识别与导出","find_target(@0)", Target),
        new("等待目标","流程控制","wait_for_target(@0)", Target),
        new("等待目标","流程控制","wait_for_target(@0,@1)", Target, Time("超时（ms）","5000",int.MaxValue)),
        new("读取屏幕文字","识别与导出","ocr()"),
        new("读取屏幕文字","识别与导出","ocr(nil,nil,nil,false)"),
        new("录制点击","录制","click_anchor("+Anchor+",@6,@7,@8)", [.. AnchorFields, new("鼠标按钮",FieldKind.Button), Number("次数","1",1,2), Number("按住时长（ms）","0",0,1800000)]),
        new("录制滚动","录制","scroll_anchor("+Anchor+",@6)", [.. AnchorFields, Number("滚轮增量","120",-32768,32767)]),
        new("录制拖拽","录制","drag_anchor(@0,@1)", new("轨迹（每行：范围 策略 参考宽 参考高 X Y）", FieldKind.Path), Number("拖拽时长（ms）","0",0,1800000)),
        new("录制按键","录制","recorded_keys(@0,@1)", new("定位范围",FieldKind.Scope), new("事件（每行：毫秒 按下/抬起 键名或扫描码）",FieldKind.KeyEvents))
    }).ToLookup(a => a.First);

    internal static List<ScriptStep> Read(string source) => Read(source, out _);
    internal static List<ScriptStep> Read(string source, out string trailing)
    {
        var doc = new LuaSyntax(source);
        var rows = new List<ScriptStep>(); int end = 0;
        void Visit(LuaStatement node, int depth)
        {
            string first = doc.Tokens[node.First].Text;
            var step = Matchers[first].Select(a => a.Read(doc,node)).FirstOrDefault(s => s != null);
            if (step == null && node.Parts != null)
            {
                foreach (var part in node.Parts) Visit(part, depth + (part.Boundary ? 0 : 1));
                return;
            }
            int start = doc.Tokens[node.First].Start, last = doc.Tokens[node.After-1].End;
            string label = !node.Boundary && node.Label.StartsWith("调用 ") && Matchers[first].FirstOrDefault() is { } action ? action.Name + "（表达式）" : node.Label;
            step ??= new(null, [], source[start..last]) { Label = label, Boundary = node.Boundary };
            rows.Add(step with { Prefix = source[end..start], Depth = depth });
            end = last;
        }
        foreach (var node in doc.Statements()) Visit(node,0);
        trailing = source[end..];
        if (rows.Count == 0 && !string.IsNullOrWhiteSpace(trailing)) { rows.Add(new(null,[],trailing) { Label = "Lua 注释与分隔符" }); trailing = ""; }
        return rows;
    }

    internal static string Write(IReadOnlyList<ScriptStep> steps, string trailing)
        => string.Concat(steps.Select(s => s.Prefix + s.Source)) + trailing;

    internal static (bool NeedsModel, bool Export) OcrUsage(string source)
    {
        if (!source.Contains("ocr", StringComparison.Ordinal)) return (false,false);
        var tokens = LuaSyntax.Lex(source);
        bool needsModel = false, export = false;
        for (int i = 0; i+1 < tokens.Count; i++)
        {
            if (tokens[i].Text != "ocr" || tokens[i+1].Text != "(" || i > 0 && tokens[i-1].Text is "." or ":" or "function") continue;
            int nesting = 0, argument = 1, begin = i+2;
            bool defaultModel = true;
            for (int j = begin; j < tokens.Count; j++)
            {
                string t = tokens[j].Text;
                if (nesting == 0 && t is "," or ")")
                {
                    if (argument == 3) defaultModel = j == begin || j == begin+1 && tokens[begin].Text == "nil";
                    if (argument == 4 && j > begin && !(j == begin+1 && tokens[begin].Text is "false" or "nil")) export = true;
                    if (t == ")") { needsModel |= defaultModel; break; }
                    argument++; begin = j+1; continue;
                }
                if (t is "(" or "{" or "[") nesting++;
                if (t is ")" or "}" or "]") nesting--;
            }
            if (needsModel && export) break;
        }
        return (needsModel,export);
    }
}
