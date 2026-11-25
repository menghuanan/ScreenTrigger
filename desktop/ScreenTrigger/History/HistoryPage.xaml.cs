using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using ScreenTrigger.Host;

namespace ScreenTrigger.History;

public sealed partial class HistoryPage : UserControl
{
    private readonly WorkspaceCommands commands;
    private int runsOffset, logsOffset, version;
    private bool changing;
    internal HistoryPage(HostClient host)
    {
        InitializeComponent(); commands=new(host,this,Feedback);
        Loaded += async (_,_)=>await commands.Run(List);
    }
    private async Task List()
    {
        var old=(Runs.SelectedItem as CatalogItem)?.Id;
        var page=await commands.Call("list_execution_history",new { offset=runsOffset,limit=20 });
        var rows=page.GetProperty("items").EnumerateArray().Select(r=>new CatalogItem(r.GetProperty("execution_id").GetString(),
            $"{r.GetProperty("started_at")} · {(r.GetProperty("kind").GetString()=="monitor" ? "监控" : "脚本")} · {Status.RunFeedback.Name(r.GetProperty("status").GetString())}",r)).ToList();
        changing=true;
        Runs.ItemsSource=rows; Runs.SelectedItem=rows.Find(r=>r.Id==old)??rows.FirstOrDefault();
        changing=false; PrevRuns.IsEnabled=runsOffset>0; NextRuns.IsEnabled=runsOffset+20<page.GetProperty("total").GetInt32();
        logsOffset=0; await ReadLogs();
    }
    private async Task ReadLogs()
    {
        var ticket=++version;
        if (Runs.SelectedItem is not CatalogItem row)
        {
            Entries.Text=""; Summary.Text="暂无执行记录。"; PrevLogs.IsEnabled=NextLogs.IsEnabled=Clear.IsEnabled=false; return;
        }
        var doc=row.Document;
        Summary.Text=$"{row.Id}　{doc.GetProperty("reason")}　{doc.GetProperty("error_code")}" +
            (doc.GetProperty("sample_no").ValueKind==JsonValueKind.Number ? $"　目标 #{doc.GetProperty("sample_no")}" : "") +
            (doc.GetProperty("effective_hz").ValueKind==JsonValueKind.Number ? $"　{doc.GetProperty("effective_hz").GetDouble():0.##} Hz" : "");
        Clear.IsEnabled=doc.GetProperty("status").GetString() is "cancelled" or "failed" or "completed";
        var page=await commands.Call("get_execution_logs",new {execution_id=row.Id, offset=logsOffset,limit=25});
        if (ticket!=version) return;
        var logs=page.GetProperty("items").EnumerateArray();
        Entries.Text=string.Join("\n",logs.Select(v=>$"{v.GetProperty("timestamp")}  {v.GetProperty("message")}　{v.GetProperty("details")}"));
        PrevLogs.IsEnabled=logsOffset>0; NextLogs.IsEnabled=logsOffset+25<page.GetProperty("total").GetInt32();
    }
    private async void Selected(object sender, SelectionChangedEventArgs args)
    {
        if (changing) return;
        logsOffset=0;
        try { await ReadLogs(); } catch(Exception ex) { Feedback.Text=ex.Message; }
    }
    private async void Browse(object sender, RoutedEventArgs args) => await commands.Run(async () =>
    {
        switch ((string)((Button)sender).Tag)
        {
            case "runs-prev": runsOffset=Math.Max(0,runsOffset-20); await List(); break;
            case "runs-next": runsOffset+=20; await List(); break;
            case "logs-prev": logsOffset=Math.Max(0,logsOffset-25); await ReadLogs(); break;
            case "logs-next": logsOffset+=25; await ReadLogs(); break;
            case "clear":
                if (Runs.SelectedItem is CatalogItem row && await commands.Confirm("清除这次执行的日志？","将删除该执行的日志分卷，保留执行摘要。","清除日志"))
                {
                    await commands.Call("clear_execution_logs",new {execution_id=row.Id});
                    logsOffset=0; await ReadLogs(); Feedback.Text="已清除日志。";
                }
                break;
            case "query":
                var response=await commands.Call("query_request",new {request_id=RequestId.Text.Trim()});
                Feedback.Text=response.GetProperty("status").GetString() switch {
                    "completed"=>"请求已结束。请查看执行或数据的最终状态。",
                    "pending"=>"请求仍在处理中。", _=>"请求状态未知。请核对执行历史和数据后再决定是否重新运行。"
                }; break;
            default: await List(); break;
        }
    });
}
