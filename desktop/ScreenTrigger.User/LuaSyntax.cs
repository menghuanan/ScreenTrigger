using System.Text.RegularExpressions;

namespace ScreenTrigger.User;

// Offsets always refer to the user's original string. Trivia is retained between statements.
internal readonly record struct LuaToken(string Text, int Start, int End);
internal sealed record LuaStatement(int First, int After, string Label, List<LuaStatement>? Parts = null, bool Boundary = false);

internal sealed class LuaSyntax
{
    private static readonly Regex Numeral = new(@"\G(?:0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)(?:[pP][+-]?\d+)?|(?:\d+(?:\.(?!\.)\d*)?|\.\d+)(?:[eE][+-]?\d+)?)", RegexOptions.CultureInvariant);
    private static readonly HashSet<string> Binary = ["+", "-", "*", "/", "//", "%", "^", "..", "&", "|", "~", "<<", ">>",
        "<", ">", "<=", ">=", "==", "~=", "and", "or"];
    private static readonly HashSet<string> Keywords = ["and","break","do","else","elseif","end","false","for","function","goto","if",
        "in","local","nil","not","or","repeat","return","then","true","until","while"];
    internal string Source { get; }
    internal List<LuaToken> Tokens { get; }
    private int pos, depth;
    private string Current => pos < Tokens.Count ? Tokens[pos].Text : "";
    private sealed class IncompleteLua : Exception { }

    internal LuaSyntax(string source) { Source = source; Tokens = Lex(source); }

    private static int LongEnd(string s, int at)
    {
        if (at >= s.Length || s[at] != '[') return -1;
        int i = at + 1;
        while (i < s.Length && s[i] == '=') i++;
        if (i == s.Length || s[i] != '[') return -1;
        string close = "]" + s[(at + 1)..i] + "]";
        int end = s.IndexOf(close, i + 1, StringComparison.Ordinal);
        return end < 0 ? s.Length : end + close.Length;
    }

    internal static List<LuaToken> Lex(string s)
    {
        List<LuaToken> tokens = [];
        for (int i = 0; i < s.Length;)
        {
            int start = i;
            char c = s[i++];
            if (char.IsWhiteSpace(c)) continue;
            if (c == '-' && i < s.Length && s[i] == '-')
            {
                int end = LongEnd(s, ++i);
                if (end >= 0) i = end; else while (i < s.Length && s[i] is not ('\r' or '\n')) i++;
                continue;
            }
            if (c is '"' or '\'')
            {
                while (i < s.Length)
                {
                    char next = s[i++];
                    if (next == '\\' && i < s.Length) i++;
                    else if (next == c) break;
                }
            }
            else if (c == '[' && LongEnd(s, start) is int end && end >= 0) i = end;
            else if (char.IsLetter(c) || c == '_') { while (i < s.Length && (char.IsLetterOrDigit(s[i]) || s[i] == '_')) i++; }
            else if (char.IsAsciiDigit(c) || c == '.' && i < s.Length && char.IsAsciiDigit(s[i]))
            {
                var match = Numeral.Match(s, start);
                if (match.Success) i = start + match.Length;
            }
            else if (i < s.Length)
            {
                if (s.AsSpan(start).StartsWith("...")) i += 2;
                else if (s.Substring(start, 2) is ".." or "//" or "<<" or ">>" or "==" or "~=" or "<=" or ">=" or "::") i++;
            }
            tokens.Add(new(s[start..i], start, i));
        }
        return tokens;
    }

    internal List<LuaStatement> Statements()
    {
        List<LuaStatement> result = [];
        while (pos < Tokens.Count)
        {
            if (Take(";")) continue;
            int start = pos;
            try { result.Add(Statement()); }
            catch (IncompleteLua)
            {
                result.Add(new(start, Tokens.Count, "Lua 代码"));
                break;
            }
        }
        return result;
    }

    private bool Take(string text) { if (Current != text) return false; pos++; return true; }
    private void Need(string text) { if (!Take(text)) throw new IncompleteLua(); }
    private void Name()
    {
        if (Current.Length == 0 || !(char.IsLetter(Current[0]) || Current[0] == '_') || Keywords.Contains(Current)) throw new IncompleteLua();
        pos++;
    }
    private void Balanced(string open, string close)
    {
        Need(open);
        int level = 1;
        while (level > 0 && pos < Tokens.Count)
        {
            string token = Tokens[pos++].Text;
            if (token == open) level++;
            if (token == close) level--;
        }
        if (level != 0) throw new IncompleteLua();
    }
    internal static bool IsString(string token) => token.Length > 1 && (token[0] is '"' or '\'' or '[');
    private void Suffix()
    {
        while (true)
        {
            if (Take(".") || Take(":")) Name();
            else if (Current == "[") Balanced("[", "]");
            else if (Current == "(") Balanced("(", ")");
            else if (Current == "{") Balanced("{", "}");
            else if (IsString(Current)) pos++;
            else return;
        }
    }
    private void Expression()
    {
        do
        {
            while (Current is "not" or "#" or "-" or "~") pos++;
            var atom = Current;
            if (atom == "(") Balanced("(", ")");
            else if (atom == "{") Balanced("{", "}");
            else if (Take("function"))
            {
                Balanced("(", ")"); var body = new List<LuaStatement>(); Block(body); Need("end");
            }
            else if (IsString(atom) || atom is "nil" or "false" or "true" or "..." || atom.Length > 0 && (char.IsAsciiDigit(atom[0]) || atom[0] == '.' && atom.Length > 1)) pos++;
            else Name();
            Suffix();
            if (!Binary.Contains(Current)) return;
            pos++;
        } while (true);
    }
    private void Expressions() { Expression(); while (Take(",")) Expression(); }

    private void Block(List<LuaStatement> parts)
    {
        if (++depth > 96) throw new IncompleteLua();
        while (Current is not ("" or "end" or "until" or "elseif" or "else"))
            if (!Take(";")) parts.Add(Statement());
        depth--;
    }
    private void LocalName() { Name(); if (Take("<")) { Name(); Need(">"); } }

    private LuaStatement Statement()
    {
        int start = pos;
        List<LuaStatement> parts = [];
        string label;
        void Header(int begin, string title) => parts.Add(new(begin, pos, title, Boundary: true));
        switch (Current)
        {
            case "if":
                label = "条件判断"; pos++; Expression(); Need("then"); Header(start,label); Block(parts);
                while (Current == "elseif") { int branch = pos++; Expression(); Need("then"); Header(branch,"否则如果"); Block(parts); }
                if (Take("else")) { Header(pos - 1,"否则"); Block(parts); }
                break;
            case "while":
                label = "条件循环"; pos++; Expression(); Need("do"); Header(start,label); Block(parts); break;
            case "for":
                label = "循环"; pos++; Name(); while (Take(",")) Name(); if (!Take("=")) Need("in");
                Expressions(); Need("do"); Header(start,label); Block(parts); break;
            case "do":
                label = "代码块"; pos++; Header(start,label); Block(parts); break;
            case "repeat":
                label = "重复执行"; pos++; Header(start,label); Block(parts);
                int until = pos; Need("until"); Expression(); Header(until,"结束条件"); return new(start,pos,label,parts);
            case "local" when pos + 1 < Tokens.Count && Tokens[pos+1].Text == "function":
            case "function":
                label = "定义函数"; Take("local"); Need("function"); Name(); while (Take(".") || Take(":")) Name();
                Balanced("(", ")"); Header(start,label); Block(parts); break;
            case "local":
                pos++; LocalName(); while (Take(",")) LocalName(); if (Take("=")) Expressions();
                return new(start,pos,"定义变量");
            case "return":
                pos++; if (Current is not ("" or ";" or "end" or "until" or "else" or "elseif")) Expressions();
                return new(start,pos,"返回结果");
            case "break": pos++; return new(start,pos,"退出循环");
            case "goto": pos++; Name(); return new(start,pos,"跳转");
            case "::": pos++; Name(); Need("::"); return new(start,pos,"标签");
            default:
                if (Current == "(") Balanced("(", ")"); else Name(); Suffix();
                if (Current is "," or "=") { while (Take(",")) { Name(); Suffix(); } Need("="); Expressions(); return new(start,pos,"赋值"); }
                return new(start,pos,Tokens[start].Text == "log" ? "写入日志" : "调用 " + Tokens[start].Text);
        }
        int end = pos; Need("end"); Header(end,"结束");
        return new(start,pos,label,parts);
    }
}
