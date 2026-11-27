using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Automation;
using ScreenTrigger.Host;

namespace ScreenTrigger.Settings;

public sealed partial class SettingsPage : UserControl, IWorkspaceDraft
{
    private readonly WorkspaceCommands commands;
    private readonly Action<JsonElement> applyDisplayConfig;
    private readonly JsonElement definitions;
    private readonly List<(string[] Path, Func<JsonNode?> Read, JsonNode? Default)> fields = [];
    private JsonObject? config;
    private readonly nint owner;
    private static readonly Dictionary<string,string> Labels = new() {
        ["general"]="常规", ["language"]="界面语言", ["theme"]="主题", ["recognition"]="识别",
        ["default_frequency_hz"]="默认频率（Hz）", ["default_threshold"]="默认阈值", ["rgb_tolerance"]="RGB 容差",
        ["hsv_tolerance"]="HSV 容差", ["ocr_model_id"]="OCR 模型 ID（留空禁用）", ["ocr_min_confidence"]="OCR 最低置信度",
        ["model_directory"]="模型目录", ["capture"]="捕获", ["preview_hz"]="预览频率（Hz）", ["png_compression_level"]="PNG 压缩级别",
        ["input"]="输入", ["allow_exit"]="允许输入越出识别区", ["move_duration_ms"]="移动时长（毫秒）", ["type_interval_ms"]="输入间隔（毫秒）",
        ["execution"]="运行", ["total_timeout_ms"]="总时限（毫秒）", ["hotkeys"]="全局热键", ["start_selection"]="开始框选",
        ["stop_script"]="停止全部执行", ["pause_script"]="暂停或恢复选中的执行", ["logging"]="日志",
        ["level"]="记录级别", ["rotate_bytes"]="单个日志文件上限（字节）", ["file_count"]="分卷数"
    };
    private static string Label(string key) => Labels.GetValueOrDefault(key, key);
    internal SettingsPage(HostClient host, Action<JsonElement> apply, nint owner)
    {
        InitializeComponent(); commands = new(host, this, Feedback); applyDisplayConfig = apply;
        this.owner = owner;
        using var stream = typeof(HostClient).Assembly.GetManifestResourceStream("ScreenTrigger.config.json")!;
        using var schema = JsonDocument.Parse(stream);
        definitions = schema.RootElement.GetProperty("definitions").Clone();
        Loaded += FirstLoad;
    }
    private async void FirstLoad(object sender, RoutedEventArgs args)
    { Loaded -= FirstLoad; await commands.Run(Read); }
    private async void Reload(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        if (await CanReplace()) await Read();
    });
    private async Task Read()
    {
        var data = await commands.Call("get_config", new { });
        Recovery.Text = data.TryGetProperty("recovery", out var r) ? $"配置损坏，已从备份自动恢复至修订 {r.GetProperty("revision")}。" : "";
        Location.Text = data.TryGetProperty("data_root", out var path) ? $"数据目录　{path.GetString()}" : "";
        Display(data.GetProperty("config"));
        await ReadBackups();
        await ReadModels();
    }
    private async Task ReadModels()
    {
        var result = await commands.Call("list_ocr_models", new { });
        var rows = result.GetProperty("pipelines").EnumerateArray().ToArray();
        string? selected = OcrPipelines.SelectedItem as string;
        var names = rows.Select(r => r.GetProperty("model_id").GetString()).ToArray();
        OcrPipelines.ItemsSource = names;
        OcrPipelines.SelectedItem = names.Contains(selected) ? selected : names.FirstOrDefault();
        ModelInfo.Text = string.Join("\n", rows.Select(r => {
            var state = r.GetProperty("state").GetString() switch {
                "missing" => "未安装", "installed" => "已安装，首次使用时装载",
                "loading" => "正在加载", "ready" => "已就绪", _ => "模型不可用"
            };
            var error = r.GetProperty("error");
            return $"{r.GetProperty("model_id")}　{state}　{string.Join(", ", r.GetProperty("languages").EnumerateArray())}"
                + (error.ValueKind == JsonValueKind.Object ? $"　{error.GetProperty("message")}" : "");
        }));
    }
    private async void RefreshModels(object sender, RoutedEventArgs args) => await commands.Run(ReadModels);
    private async void ImportModel(object sender, RoutedEventArgs args) => await commands.Run(() => ChangeModel(true));
    private async void RemoveModel(object sender, RoutedEventArgs args) => await commands.Run(() => ChangeModel(false));

    private async Task ChangeModel(bool import)
    {
        if (OcrPipelines.SelectedItem is not string model) return;
        object request = new { operation = "remove", model_id = model };
        if (import)
        {
            var picker = new Windows.Storage.Pickers.FolderPicker();
            picker.FileTypeFilter.Add("*");
            WinRT.Interop.InitializeWithWindow.Initialize(picker, owner);
            var folder = await picker.PickSingleFolderAsync();
            if (folder is null) return;
            request = new { operation = "import", model_id = model, directory = folder.Path };
        }
        var preview = await BackupJob("preview_ocr_model_change", request);
        var refs = preview.GetProperty("references").EnumerateArray()
            .Select(r => $"{r.GetProperty("kind")}　{r.GetProperty("id")}");
        var detail = $"{model}\n模型文件 {preview.GetProperty("bytes")} 字节。\n" +
            string.Join("\n", refs) + "\n脚本中的动态引用在运行时解析。";
        if (!await commands.Confirm(import ? "导入此 OCR 管线？" : "移除此 OCR 管线？", detail, "确认")) return;
        await BackupJob("apply_ocr_model_change", new {
            preview_id = preview.GetProperty("preview_id").GetString(),
            expected_revision = preview.GetProperty("revision").GetInt32() });
        await ReadModels();
        Feedback.Text = import ? "模型已导入。" : "模型已移除。";
    }
    private async Task ReadBackups()
    {
        var data = await commands.Call("list_catalog_backups", new { });
        Backups.ItemsSource = data.GetProperty("backups").EnumerateArray().Select(b => b.GetString()).ToList();
        if (Backups.Items.Count > 0) Backups.SelectedIndex = 0;
    }

    private async Task<JsonElement> BackupJob(string command, object parameters)
    {
        var started = await commands.Call(command, parameters);
        var job = started.GetProperty("job_id").GetString();
        while (true)
        {
            var response = await commands.Call("get_job", new { job_id = job });
            switch (response.GetProperty("status").GetString())
            {
                case "completed": return response.GetProperty("result");
                case "error":
                    var error = response.GetProperty("error");
                    throw new InvalidOperationException($"{error.GetProperty("code")} · {error.GetProperty("message")}");
                case "cancelled": throw new OperationCanceledException("作业已取消。");
            }
            await Task.Delay(150);
        }
    }

    private async void CreateBackup(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        Feedback.Text = "正在校验目标、原图和脚本源码…";
        var result = await BackupJob("create_catalog_backup", new { });
        await ReadBackups();
        Feedback.Text = $"备份已保存到数据目录的 backups 文件夹。编号 {result.GetProperty("backup_id").GetString()}";
    });

    private async void RestoreBackup(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        if (Backups.SelectedItem is not string id) { Feedback.Text = "请先选择备份。"; return; }
        var preview = await BackupJob("preview_catalog_restore", new { backup_id = id });
        var counts = preview.GetProperty("counts");
        var state = preview.GetProperty("current_state").GetString();
        string protection = state switch
        {
            "valid" => "确认后将先备份当前目标与脚本。",
            "damaged" => "当前目录损坏。恢复将保留原指针和现存文件。",
            _ => "当前数据目录尚无目标与脚本。"
        };
        var summary = $"备份 {id}\n目标 {counts.GetProperty("targets")} 个，分组 {counts.GetProperty("groups")} 个，脚本 {counts.GetProperty("scripts")} 个。\n{protection}";
        if (!await commands.Confirm("恢复这些目标与脚本？", summary, "确认恢复"))
        { Feedback.Text = "已取消恢复。"; return; }
        var result = await BackupJob("restore_catalog_backup", new {
            backup_id = id, expected_current = preview.GetProperty("expected_current").GetString(),
            expected_backup = preview.GetProperty("expected_backup").GetString()
        });
        await ReadBackups();
        Feedback.Text = $"已恢复，当前 generation 为 {result.GetProperty("generation").GetString()}。";
    });
    private void Display(JsonElement value)
    {
        config = JsonNode.Parse(value.GetRawText())!.AsObject(); Groups.Children.Clear(); fields.Clear();
        foreach (var (group, node) in config)
        {
            if (node is not JsonObject contents) continue;
            var panel = new StackPanel { Spacing=8 };
            var expander = new Expander { Header=Label(group), Content=panel, HorizontalAlignment=HorizontalAlignment.Stretch,
                HorizontalContentAlignment=HorizontalAlignment.Stretch, IsExpanded=group=="general" };
            AutomationProperties.SetAutomationId(expander, "SettingGroup_" + group);
            Groups.Children.Add(expander);
            AddFields(panel, [group], contents, definitions.GetProperty(group));
        }
        applyDisplayConfig(value);
    }
    private void AddFields(StackPanel panel, string[] prefix, JsonObject values, JsonElement section)
    {
        foreach (var (key, value) in values)
        {
            string[] path = [..prefix,key];
            var spec = section.GetProperty("properties").GetProperty(key);
            if (spec.TryGetProperty("$ref",out var reference)) spec = definitions.GetProperty(reference.GetString()!.Split('/')[^1]);
            if (value is JsonObject child)
            {
                panel.Children.Add(new TextBlock { Text=Label(key) });
                AddFields(panel,path,child,spec); continue;
            }
            var text = Label(key);
            FrameworkElement field;
            Func<JsonNode?> read;
            if (spec.TryGetProperty("enum",out var options))
            {
                var box = new ComboBox { Header=text, ItemsSource=options.EnumerateArray().Select(o=>o.GetString()).ToList(), SelectedItem=value?.GetValue<string>() };
                field=box; read=()=>JsonValue.Create(box.SelectedItem as string);
            }
            else if (value?.GetValueKind() == JsonValueKind.Number)
            {
                var box=new NumberBox { Header=text, Value=value.GetValue<double>(), Text=value.GetValue<double>().ToString() };
                if (spec.TryGetProperty("minimum",out var min)) box.Minimum=min.GetDouble();
                if (spec.TryGetProperty("maximum",out var max)) box.Maximum=max.GetDouble();
                field=box; read=()=> double.TryParse(box.Text, out var number) && double.IsFinite(number)
                    ? JsonValue.Create(number) : JsonValue.Create(box.Text);
            }
            else if (value?.GetValueKind() is JsonValueKind.True or JsonValueKind.False)
            {
                var box=new CheckBox { Content=text, IsChecked=value.GetValue<bool>() };
                field=box; read=()=>JsonValue.Create(box.IsChecked==true);
            }
            else
            {
                var box=new TextBox { Header=text, Text=value?.GetValue<string>() ?? "" };
                field=box; read=()=>key=="ocr_model_id" && box.Text.Length==0 ? null : JsonValue.Create(box.Text);
            }
            AutomationProperties.SetAutomationId(field,"Setting_" + string.Join("_",path));
            panel.Children.Add(field);
            fields.Add((path,read,JsonNode.Parse(spec.GetProperty("default").GetRawText())));
        }
    }

    private JsonNode? Original(string[] path) => path.Aggregate((JsonNode?)config,(node,key)=>node?[key]);
    private bool HasChanges => config != null && fields.Any(f => !JsonNode.DeepEquals(Original(f.Path), f.Read()));
    bool IWorkspaceDraft.HasChanges => HasChanges;
    Task IWorkspaceDraft.WhenIdle => commands.WhenIdle;
    Task<bool> IWorkspaceDraft.ResolveChanges() => commands.Guard(CanReplace);
    private Task<bool> CanReplace() => HasChanges
        ? commands.ResolveDraft("设置", SaveDraft, RestoreDraft) : Task.FromResult(true);
    private void RestoreDraft()
    {
        if (config != null) Display(JsonSerializer.SerializeToElement(config));
    }
    private void Cancel(object sender, RoutedEventArgs args)
    {
        RestoreDraft();
        Feedback.Text="已撤销设置编辑。";
    }
    private async void Save(object sender, RoutedEventArgs args) => await commands.Run(SaveDraft);
    private async Task SaveDraft()
    {
        if (config == null) return;
        var patch = new JsonObject();
        foreach (var field in fields)
        {
            var value=field.Read();
            if (JsonNode.DeepEquals(Original(field.Path),value)) continue;
            var parent=patch;
            foreach (var part in field.Path[..^1]) { parent[part] ??= new JsonObject(); parent = parent[part]!.AsObject(); }
            parent[field.Path[^1]]=value;
        }
        if (patch.Count==0) { Feedback.Text="设置未修改。"; return; }
        var data=await commands.Call("update_config",new {expected_revision=config["revision"]!.GetValue<long>(), patch});
        Display(data.GetProperty("config"));
        Feedback.Text="设置已保存。热键立即生效，运行参数用于新执行，模型目录将在引擎重启后生效。";
    }
    private async void Reset(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        if (config == null) return;
        var differences = fields.Where(f=>!JsonNode.DeepEquals(Original(f.Path), f.Default)).Select(f=>
            $"{Label(f.Path[0])} · {Label(f.Path[^1])}　{Original(f.Path)} → {f.Default}").ToList();
        if (differences.Count==0) { Feedback.Text="当前已是默认设置。"; return; }
        if (!await commands.Confirm("恢复默认设置？",string.Join("\n",differences),"恢复设置")) return;
        var response=await commands.Call("reset_config",new {expected_revision=config["revision"]!.GetValue<long>()});
        Display(response.GetProperty("config")); Feedback.Text="已恢复默认设置。";
    });
}
