using System.Net.Http;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;

namespace ScreenTrigger.User;

internal static class ReleaseChecker
{
    private static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(12), MaxResponseContentBufferSize = 8 * 1024 * 1024 };
    private const string Releases = "https://api.github.com/repos/menghuanan/ScreenTrigger/releases";
    private const string ReleaseSite = "https://github.com/menghuanan/ScreenTrigger/releases";

    internal static async Task<string> CheckAsync(string version, CancellationToken token, HttpClient? transport = null)
    {
        if (!ReleaseVersion.TryParse(version, out var current)) throw new FormatException("无法识别当前应用版本。");
        ReleaseVersion? newest = null;
        string? newestTag = null;
        bool published = false;
        void Consider(string? tag)
        {
            published = true;
            if (tag != null && ReleaseVersion.TryParse(tag, out var parsed) && (newest == null || parsed!.CompareTo(newest) > 0))
                (newest, newestTag) = (parsed, tag);
        }
        var client = transport ?? Client;
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(15));
        // Include public preview releases because the user client itself ships as a beta.
        for (int page = 1; ; page++)
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, $"{Releases}?per_page=100&page={page}");
            request.Headers.UserAgent.ParseAdd("ScreenTrigger/" + version);
            request.Headers.Accept.ParseAdd("application/vnd.github+json");
            using var response = await client.SendAsync(request, deadline.Token).ConfigureAwait(false);
            if (response.StatusCode is System.Net.HttpStatusCode.Forbidden or System.Net.HttpStatusCode.TooManyRequests)
            {
                foreach (var tag in await PublicFeed(client, deadline.Token).ConfigureAwait(false)) Consider(tag);
                break;
            }
            response.EnsureSuccessStatusCode();
            using var data = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(deadline.Token).ConfigureAwait(false));
            foreach (var release in data.RootElement.EnumerateArray())
            {
                if (release.GetProperty("draft").GetBoolean()) continue;
                Consider(release.GetProperty("tag_name").GetString());
            }
            if (data.RootElement.GetArrayLength() < 100) break;
        }
        if (!published) return "当前是最新版本。项目暂无公开发布的版本。";
        if (newest == null) throw new FormatException("发布标签缺少可比较的版本号。");
        return newest.CompareTo(current!) > 0 ? $"有新版本更新　{newestTag}" : "当前是最新版本。";
    }

    private static async Task<List<string>> PublicFeed(HttpClient client, CancellationToken token)
    {
        using var response = await client.GetAsync(ReleaseSite + ".atom", token).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        using var stream = await response.Content.ReadAsStreamAsync(token).ConfigureAwait(false);
        using var reader = XmlReader.Create(stream, new XmlReaderSettings { Async = true, DtdProcessing = DtdProcessing.Prohibit, MaxCharactersInDocument = 8 * 1024 * 1024 });
        var xml = await XDocument.LoadAsync(reader, LoadOptions.None, token).ConfigureAwait(false);
        XNamespace atom = "http://www.w3.org/2005/Atom";
        if (xml.Root?.Name != atom + "feed") throw new FormatException("无法读取项目的发布订阅源。");
        List<string> tags = [];
        foreach (var entry in xml.Root.Elements(atom + "entry"))
        {
            var href = entry.Elements(atom + "link").Select(link => (string?)link.Attribute("href"))
                .FirstOrDefault(url => url?.StartsWith(ReleaseSite + "/tag/", StringComparison.OrdinalIgnoreCase) == true);
            if (href == null) throw new FormatException("发布记录中缺少版本标签。");
            tags.Add(Uri.UnescapeDataString(href[(ReleaseSite.Length + 5)..]));
        }
        return tags;
    }

    private sealed record ReleaseVersion(Version Core, string[] Preview)
    {
        internal static bool TryParse(string tag, out ReleaseVersion? result)
        {
            result = null;
            var parts = Regex.Match(tag.Trim(), @"\A[vV]?(\d+(?:\.\d+){0,3})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?\z", RegexOptions.CultureInvariant);
            if (!parts.Success) return false;
            var numbers = parts.Groups[1].Value.Split('.').ToList();
            while (numbers.Count < 4) numbers.Add("0");
            if (!Version.TryParse(string.Join('.', numbers), out var core)) return false;
            result = new(core, parts.Groups[2].Success ? parts.Groups[2].Value.Split('.') : []);
            return true;
        }

        internal int CompareTo(ReleaseVersion other)
        {
            int order = Core.CompareTo(other.Core);
            if (order != 0) return order;
            if (Preview.Length == 0 || other.Preview.Length == 0)
                return (Preview.Length == 0).CompareTo(other.Preview.Length == 0);
            for (int i = 0; i < Math.Min(Preview.Length, other.Preview.Length); i++)
            {
                string left = Preview[i], right = other.Preview[i];
                bool ln = left.All(char.IsAsciiDigit), rn = right.All(char.IsAsciiDigit);
                if (ln != rn) return ln ? -1 : 1;
                if (ln && left.Length != right.Length) return left.Length.CompareTo(right.Length);
                order = string.CompareOrdinal(left, right);
                if (order != 0) return order;
            }
            return Preview.Length.CompareTo(other.Preview.Length);
        }
    }
}
