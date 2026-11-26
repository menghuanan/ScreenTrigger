using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Parsers.Kernel;
using System.Globalization;
using System.Diagnostics;
using System.Text.Json;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Session;

// Observe only the explicitly identified workload and its children; the observer never starts them.
if (args.Length != 2 || !int.TryParse(args[0], out int rootPid)) return 2;
using var root = Process.GetProcessById(rootPid);
var repo = new DirectoryInfo(AppContext.BaseDirectory);
while (repo is not null &&
       !(File.Exists(Path.Combine(repo.FullName, "AGENTS.md")) &&
         File.Exists(Path.Combine(repo.FullName, "tools", "run_acceptance.py"))))
    repo = repo.Parent;
if (repo is null) return 2;
var raw = Path.Combine(repo.FullName, "reports", "automation-v2", "raw");
var folder = new DirectoryInfo(Path.GetFullPath(args[1]));
var relative = Path.GetRelativePath(raw, folder.FullName);
if (!folder.Exists || relative == "." || relative == ".." || Path.IsPathRooted(relative) ||
    relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal))
    return 2;
using var reportPath = ReportPath.TryOpen(folder);
if (reportPath is null) return 2;
var proc = Guid.Parse("22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716");
var net = Guid.Parse("7DD42A49-5329-4832-8DFD-43D979153A88");
var file = Guid.Parse("EDD08927-9CC4-4E65-B970-C2560FB5C289");
using var writer = new StreamWriter(new FileStream(Path.Combine(folder.FullName,"events.jsonl"),FileMode.CreateNew,FileAccess.Write,FileShare.Read));
object sync = new();
void Emit(object item) { lock(sync) { writer.WriteLine(JsonSerializer.Serialize(item)); writer.Flush(); } }
using var session = new TraceEventSession("ScreenTriggerPortable-" + Guid.NewGuid().ToString("N"));
session.BufferSizeMB=64;
var source=session.Source;
var tree=new HashSet<int>{rootPid};
// File objects are shared by inherited or duplicated handles. The kernel's file
// names are separately keyed by FileKey; neither is a per-process identifier.
// Only events for the selected descendant tree are emitted. No kernel pointers
// or system-wide path registry are written to the QA report.
var objects=new Dictionary<string,string>();
var fileNames=new Dictionary<string,string>();
bool Valid(string key) => key.Length>0 && key!="0" && key!="0x0";
// Only scoped, initially unresolved operations may receive a rundown label in the output.
using var fileSession = new TraceEventSession("ScreenTriggerFileNames-" + Guid.NewGuid().ToString("N"));
fileSession.BufferSizeMB=64;
fileSession.EnableKernelProvider(KernelTraceEventParser.Keywords.DiskIO | KernelTraceEventParser.Keywords.DiskFileIO | KernelTraceEventParser.Keywords.FileIOInit);
var fileSource=fileSession.Source;
var fileHistory=new Dictionary<ulong,List<(DateTime time, string op,string name)>>();
var unresolved=new Dictionary<int,(ulong key,DateTime time)>();
int nextWrite=0;
void Remember(FileIONameTraceData e,string op) {
    lock(sync) {
        if(!fileHistory.TryGetValue(e.FileKey,out var list)) fileHistory[e.FileKey]=list=new();
        list.Add((e.TimeStamp,op,e.FileName));
    }
}
fileSource.Kernel.FileIOName += e => Remember(e,"name");
fileSource.Kernel.FileIOFileCreate += e => Remember(e,"create");
fileSource.Kernel.FileIOFileDelete += e => Remember(e,"delete");
fileSource.Kernel.FileIOFileRundown += e => Remember(e,"rundown");
var fileReader=Task.Run(()=>fileSource.Process());
ulong Pointer(object value) {
    if(value is ulong u) return u;
    if(value is long n) return unchecked((ulong)n);
    if(value is IntPtr ptr) return unchecked((ulong)ptr.ToInt64());
    var s=value?.ToString()??"";
    if(ulong.TryParse(s,out var val))return val;
    if(s.StartsWith("0x") && ulong.TryParse(s[2..],NumberStyles.HexNumber,CultureInfo.InvariantCulture,out val))return val;
    return 0;
}
source.Dynamic.All += e => {
    int Number(string name) => Array.Exists(e.PayloadNames, n=>n==name) ? Convert.ToInt32(e.PayloadByName(name)) : -1;
    string Text(string name) => Array.Exists(e.PayloadNames,n=>n==name) ? e.PayloadByName(name)?.ToString() ?? "" : "";
    int id=(int)e.ID;
    if (e.ProviderGuid==proc) {
        int pid=Number("ProcessID"),parent=Number("ParentProcessID");
        if (id==1 && tree.Contains(parent)) {
            tree.Add(pid);
            var image = Text("ImageName");
            if (image.Length == 0) image = Text("ImageFileName");
            Emit(new {kind="start",time=e.TimeStamp,pid,parent,image});
        } else if (id==1 && pid!=rootPid) { tree.Remove(pid); }
        else if (id==2 && tree.Remove(pid)) {
            Emit(new {kind="stop",time=e.TimeStamp,pid});
        }
        return;
    }
    if (e.ProviderGuid==net) {
        int pid=Number("PID");
        if(pid<0) pid=e.ProcessID;
        if (tree.Contains(pid)) Emit(new {kind="network",time=e.TimeStamp,pid,id,operation=e.EventName});
        return;
    }
    if (e.ProviderGuid!=file) return;
    int actor=e.ProcessID;
    string obj=Text("FileObject"), key=Text("FileKey"), name=Text("FileName");
    if (name.Length==0) name=Text("OpenPath");
    if (name.Length==0) name=Text("FilePath");
    if (id==10) {
        if (Valid(key) && name.Length>0) fileNames[key]=name;
        return;
    }
    if (id==11) {
        if (Valid(key)) fileNames.Remove(key);
        return;
    }
    if (id==14) {
        if (Valid(obj)) objects.Remove(obj);
        return;
    }
    if (id is 12 or 30 && Valid(obj)) {
        // Invalidate stale object identity before considering the next lifetime.
        objects.Remove(obj);
        if (name.Length>0 && tree.Contains(actor)) objects[obj]=name;
    }
    if (!tree.Contains(actor) || id is not (16 or 26 or 27 or 28 or 30)) return;
    string via="payload";
    if (name.Length==0 && Valid(key) && fileNames.TryGetValue(key,out var keyed)) {
        name=keyed;
        via="file-name-key";
    }
    if (name.Length==0 && Valid(obj) && objects.TryGetValue(obj,out var opened)) {
        name=opened;
        via="open-file-object";
    }
    int eventNumber=++nextWrite;
    if (name.Length==0) {
        via="unresolved";
        if (Valid(key)) unresolved[eventNumber]=(Pointer(e.PayloadByName("FileKey")),e.TimeStamp);
    }
    Emit(new {kind="file",time=e.TimeStamp,pid=actor,id,operation=e.EventName,path=name,via,
        eventNumber, ioFlags=Number("IOFlags"),extraFlags=Number("ExtraFlags"),size=Number("IOSize"),hasObject=Valid(obj),hasKey=Valid(key)});

};
session.EnableProvider(proc,TraceEventLevel.Informational,0x10);
session.EnableProvider(net,TraceEventLevel.Informational,0x30);
session.EnableProvider(file,TraceEventLevel.Informational,0x1EB0);
var reader=Task.Run(()=>source.Process());
Emit(new {kind="ready",rootPid,started=root.StartTime.ToUniversalTime()});
var timeout=Stopwatch.StartNew();
while(!File.Exists(Path.Combine(folder.FullName,"trace.done")) && !root.HasExited && timeout.Elapsed.TotalMinutes<15) {
    if(reader.IsCompleted) throw new InvalidOperationException("Observer exited early");
    Thread.Sleep(100);
}
session.Flush();
Thread.Sleep(1000);
int lost=session.EventsLost;
session.Stop();
if (!reader.Wait(TimeSpan.FromSeconds(10))) throw new TimeoutException("Trace did not drain");
fileSession.Flush();
int rundownLost=fileSession.EventsLost;
fileSession.Stop();
if (!fileReader.Wait(TimeSpan.FromSeconds(20))) throw new TimeoutException("File name rundown did not drain");
rundownLost=Math.Max(rundownLost,fileSource.EventsLost);
foreach (var (eventNumber,item) in unresolved) {
    if (!fileHistory.TryGetValue(item.key,out var histories)) continue;
    var history=histories.OrderBy(i=>i.time).ToArray();
    string name=""; string via="";
    var before=history.Where(i=>i.time<=item.time).LastOrDefault();
    if(before.op is "create" or "name" && !string.IsNullOrEmpty(before.name)) {
        name=before.name;via="classic-name-lifetime";
    } else {
        var after=history.Where(i=>i.time>=item.time).FirstOrDefault();
        // A new create could reuse the key. Only the same live lifetime qualifies.
        if(after.op is "rundown" or "delete" && !string.IsNullOrEmpty(after.name)) {
            name=after.name;via="classic-final-live-name";
        }
    }
    if(!string.IsNullOrEmpty(name)) Emit(new {kind="resolved",eventNumber,path=name,via});
}
Emit(new {kind="complete",lost,rundownLost,classicNames=fileHistory.Count,workload_finished=File.Exists(Path.Combine(folder.FullName,"trace.done"))});
return lost == 0 && rundownLost == 0 ? 0 : 1;

sealed class ReportPath : IDisposable
{
    private readonly List<Microsoft.Win32.SafeHandles.SafeFileHandle> handles = new();

    public static ReportPath TryOpen(DirectoryInfo directory)
    {
        var ancestors = new Stack<DirectoryInfo>();
        for (var current = directory; current is not null; current = current.Parent)
            ancestors.Push(current);
        var lease = new ReportPath();
        foreach (var part in ancestors)
        {
            // Open each directory itself and hold it across exclusive output creation.
            // Sharing reads permits metadata lookups while preventing replacement of a checked directory.
            var handle = CreateFile(part.FullName, 0x80, FileShare.Read, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
            lease.handles.Add(handle);
            if (handle.IsInvalid ||
                !GetFileInformationByHandleEx(handle, 9, out var info, 8) ||
                !info.Attributes.HasFlag(FileAttributes.Directory) ||
                info.Attributes.HasFlag(FileAttributes.ReparsePoint))
            {
                lease.Dispose();
                return null;
            }
        }
        return lease;
    }

    public void Dispose()
    {
        for (int i = handles.Count - 1; i >= 0; i--) handles[i].Dispose();
        handles.Clear();
    }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct AttributeTag
    {
        public FileAttributes Attributes;
        public uint ReparseTag;
    }

    [System.Runtime.InteropServices.DllImport("kernel32.dll", EntryPoint = "CreateFileW",
        CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFile(string path, uint access,
        FileShare share, IntPtr security, uint disposition, uint flags, IntPtr template);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandleEx(Microsoft.Win32.SafeHandles.SafeFileHandle handle,
        int informationClass, out AttributeTag info, uint size);
}
