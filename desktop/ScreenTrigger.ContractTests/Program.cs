using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using ScreenTrigger.Contracts;
using ScreenTrigger.Host;

internal static class Program
{
    private static readonly List<object> results = [];
    private static int failures;
    private static string output = "";
    private static JsonElement cases;
    private static readonly JsonSerializerOptions reportJson = new() { WriteIndented = true };

    public static async Task<int> Main(string[] args)
    {
        if (args.Length > 1 && Path.GetFileName(args[0]) == "main.py") return FaultPeer.Run(args);
        if (args.Length != 2 || args[0] is not ("types" or "config" or "protocol" or "faults")) return 2;
        output = Path.GetFullPath(args[1]);
        if (Directory.Exists(output)) throw new IOException("证据目录已存在。");
        Directory.CreateDirectory(output);
        using var source = JsonDocument.Parse(File.ReadAllBytes("schema/contract-cases.json"));
        cases = source.RootElement.GetProperty("cases");
        if (args[0] == "types") await TypeCases();
        if (args[0] == "config") await ConfigCases();
        if (args[0] == "protocol") await ProtocolCases();
        if (args[0] == "faults") await FaultCases();
        File.WriteAllText(Path.Combine(output,"results.json"),JsonSerializer.Serialize(results,reportJson));
        Console.WriteLine($"{results.Count - failures}/{results.Count} passed");
        return failures == 0 ? 0 : 1;
    }

    private static async Task Check(string name, Func<Task<object?>> test)
    {
        var start = DateTimeOffset.UtcNow;
        try
        {
            var actual = await test();
            results.Add(new { name, start, finish = DateTimeOffset.UtcNow, status = "PASS", actual });
            Console.WriteLine($"PASS {name}");
        }
        catch (Exception ex)
        {
            failures++;
            results.Add(new { name, start, finish = DateTimeOffset.UtcNow, status = "FAIL", error = ex.ToString() });
            Console.WriteLine($"FAIL {name} {ex.Message}");
        }
    }

    private static void Require(bool condition, string requirement)
    { if (!condition) throw new InvalidOperationException(requirement); }

    private static JsonElement Data(object? value) => JsonSerializer.SerializeToElement(value);
    private static async Task Until(Func<bool> condition, int ms = 5000)
    {
        var timer = Stopwatch.StartNew();
        while (!condition() && timer.ElapsedMilliseconds < ms) await Task.Delay(15);
        Require(condition(),"Timed out waiting for observable state");
    }
    private static IEnumerable<JsonElement> Shared(string kind) => cases.EnumerateArray().Where(c =>
        c.GetProperty("kind").GetString() == kind && c.GetProperty("applies_to").EnumerateArray().Any(t => t.GetString() == "csharp"));

    private static async Task TypeCases()
    {
        foreach (var c in Shared("type_validation"))
        {
            await Check(c.GetProperty("id").GetString()!, () =>
            {
                string type = c.GetProperty("typeName").GetString()!;
                var value = c.GetProperty("value");
                if (c.TryGetProperty("nameOverrideLength",out var length))
                {
                    var copy = JsonNode.Parse(value.GetRawText())!;
                    copy["name"] = new string('名',length.GetInt32());
                    value = JsonSerializer.SerializeToElement(copy);
                }
                var expected = c.GetProperty("expect");
                try
                {
                    WireContracts.Validate(type,value);
                    Require(expected.GetProperty("valid").GetBoolean(),"Accepted invalid sample");
                    Type? model = type switch { "region" => typeof(Region), "target" => typeof(Target), "point" => typeof(PixelPoint),
                        "group" => typeof(Group), "script" => typeof(Script),
                        "local_rect" => typeof(PixelRect), "run_config" => typeof(RunConfig), "monitor_config" => typeof(MonitorConfig), _ => null };
                    if (model != null)
                    {
                        var decoded = value.Deserialize(model, WireContracts.Json);
                        var encoded = JsonSerializer.SerializeToElement(decoded,model,WireContracts.Json);
                        Require(JsonElement.DeepEquals(encoded,value),"Round trip changed fields");
                    }
                    return Task.FromResult<object?>(new { valid = true, type });
                }
                catch (ContractException e)
                {
                    Require(!expected.GetProperty("valid").GetBoolean() && e.Code == expected.GetProperty("code").GetString()
                        && e.Field == expected.GetProperty("field").GetString(),$"Unexpected validation {e.Code}/{e.Field}");
                    return Task.FromResult<object?>(new { valid = false, e.Code, e.Field });
                }
            });
        }
        await Check("frame-match-nested-validation",() =>
        {
            var frame = Data(new { frame_id="f1", image_ref=new { image_id="i1",width=10,height=10,format="png",sha256=new string('a',64) },
                space="screen_physical", origin=new { x=-20,y=4 },captured_at="2026-11-25T00:00:00Z",geometry_revision=0,binding_id=(string?)null });
            var match = Data(new { target_id=(string?)null,sample_no=(long?)null,space="screen_physical",bbox=new { x=-19,y=0,width=10,height=10 },
                center=new { x=-14,y=5 }, confidence=.9,geometry_revision=0,evidence=new[]{new {condition_id="c1",matched=true,confidence=.9}} });
            var f=WireContracts.Read<Frame>(frame,"frame");
            var m=WireContracts.Read<Match>(match,"match");
            Require(JsonElement.DeepEquals(frame,JsonSerializer.SerializeToElement(f,WireContracts.Json)) &&
                JsonElement.DeepEquals(match,JsonSerializer.SerializeToElement(m,WireContracts.Json)),"Frame/Match wire roundtrip");
            return Task.FromResult<object?>(new { f.FrameId,m.Center });
        });
        await Check("strict-shape-and-finite-numbers",() =>
        {
            foreach (var raw in new[]{"""{"a":null,"a":1}""","""{"nested":[1e999]}"""})
            {
                using var d=JsonDocument.Parse(raw);
                try {WireContracts.CheckJson(d.RootElement); throw new Exception("Accepted malformed value");}
                catch(ContractException) { }
            }
            foreach (var raw in new[]{"""{"x":0,"y":1,"extra":null}""","""{"x":null,"y":0}""","""{"y":0}"""})
            {
                using var d=JsonDocument.Parse(raw);
                try {WireContracts.Read<PixelPoint>(d.RootElement,"point");throw new Exception("Accepted invalid point");}
                catch(ContractException) { }
            }
            return Task.FromResult<object?>(new { rejected=5 });
        });
    }

    private static async Task Start(HostClient host)
    {
        host.Events.BeginStart();
        var state=await host.CallAsync(new {op="start"});
        Require(state.GetProperty("state").GetString()=="ready","Engine startup");
        await Until(()=>host.Events.Session!=null);
    }
    private static HostClient NewHost(string name)
    {
        Environment.SetEnvironmentVariable("SIDECAR_DATA_DIR",Path.Combine(output,name));
        return new HostClient(a=>{a();return true;});
    }

    private static async Task ConfigCases()
    {
        foreach (var c in Shared("config_patch"))
        {
            string name=c.GetProperty("id").GetString()!;
            await Check(name,async () =>
            {
                await using var host=NewHost(name);
                await Start(host);
                var before=await host.GetConfigAsync();
                Require(before.Revision==1 && before.SchemaVersion==2,"Pristine revision");
                var expected=c.GetProperty("expect");
                long rev=c.GetProperty("expectedRevision").GetInt64();
                try
                {
                    var after=c.TryGetProperty("reset",out var reset) && reset.GetBoolean()
                        ? await host.ResetConfigAsync(rev) : await host.UpdateConfigAsync(rev,c.GetProperty("patch"));
                    Require(expected.GetProperty("success").GetBoolean() && after.Revision==expected.GetProperty("revision").GetInt64(),"Unexpected config result");
                    var field=JsonSerializer.SerializeToElement(after,WireContracts.Json);
                    foreach(var part in expected.GetProperty("field").GetString()!.Split('.')) field=field.GetProperty(part);
                    Require(JsonElement.DeepEquals(field,expected.GetProperty("value")),"Patch result value");
                    return new {revision=after.Revision,field};
                }
                catch(HostException ex)
                {
                    var after=await host.GetConfigAsync();
                    Require(!expected.GetProperty("success").GetBoolean() && ex.Error.Code==expected.GetProperty("errorCode").GetString(),"Error code "+ex.Error.Code);
                    if(expected.TryGetProperty("field",out var field))
                        Require(ex.Error.Details.GetProperty("field").GetString()==field.GetString(),"Wrong config field");
                    Require(JsonElement.DeepEquals(JsonSerializer.SerializeToElement(before),JsonSerializer.SerializeToElement(after)),"Failed patch changed config");
                    return new {ex.Error.Code,ex.Error.Details,revision=after.Revision};
                }
            });
        }
        await Check("config-restart-reset-conflict",async () =>
        {
            await using var host=NewHost("persistence");
            await Start(host);
            var patch=Data(new {recognition=new {default_frequency_hz=2.5}});
            var saved=await host.UpdateConfigAsync(1,patch);
            await host.CallAsync(new {op="stop"});
            await Start(host);
            var loaded=await host.GetConfigAsync();
            Require(saved.Revision==2 && loaded.Recognition.DefaultFrequencyHz==2.5 && loaded.Revision==2,"Saved config after restart");
            var reset=await host.ResetConfigAsync(2);
            Require(reset.Revision==3 && reset.Recognition.DefaultFrequencyHz==1,"Defaults after reset");
            return new {saved=saved.Revision,reloaded=loaded.Revision,reset=reset.Revision};
        });
        await Check("corrupt-backup-future-schema-and-racing-writes",async () =>
        {
            const string name="config-recovery";
            await using var host=NewHost(name);
            await Start(host);
            async Task<string> Write(string theme)
            {
                try { await host.UpdateConfigAsync(1,Data(new {general=new {theme}})); return "saved"; }
                catch(HostException e){return e.Error.Code;}
            }
            var attempts=await Task.WhenAll(Write("dark"),Write("light"));
            Require(attempts.Order().SequenceEqual(new[]{"CONFLICT","saved"}),"Concurrent revisions");
            await host.CallAsync(new {op="stop"});
            string path=Path.Combine(output,name,"config.json");
            await File.WriteAllTextAsync(path,"{\"revision\":1,\"revision\":2}");
            await Start(host);
            var restored=await host.GetConfigAsync();
            Require(restored.Revision==1 && restored.General.Theme=="system","Recovery did not use the last good backup");
            await host.CallAsync(new {op="stop"});
            var future=JsonNode.Parse(await File.ReadAllTextAsync(path))!;
            future["schema_version"]=99;
            future["future_only"]="keep me";
            string original=future.ToJsonString();
            await File.WriteAllTextAsync(path,original);
            await Start(host);
            foreach(var readOrWrite in new Func<Task<ConfigDocument>>[]{host.GetConfigAsync,()=>host.ResetConfigAsync(1)})
                try {await readOrWrite();throw new Exception("Future config accepted");}
                catch(HostException e) when(e.Error.Code=="SCHEMA_UNSUPPORTED"){}
            Require(await File.ReadAllTextAsync(path)==original,"Future file was changed");
            return new {attempts,recovered_revision=restored.Revision,future_schema=99,original_bytes_preserved=true};
        });
    }

    [DllImport("ntdll")] private static extern int NtSuspendProcess(nint handle);
    [DllImport("ntdll")] private static extern int NtResumeProcess(nint handle);

    private static async Task ProtocolCases()
    {
        await using var host=NewHost("engine");
        await Start(host);
        await Check("opaque-ids-query-cache-conflict",async () =>
        {
            var hello=await host.HelloAsync();
            var first=host.BeginRequest("get_config",new {});
            var second=host.BeginRequest("ping",new {});
            await Task.WhenAll(first.Completion,second.Completion);
            Require(first.Id!=second.Id && first.Session==second.Session,"Cross-window request IDs");
            var query=await host.QueryAsync(first);
            Require(query.GetProperty("status").GetString()=="completed","Query after reply");
            var opts=new { expected_revision=1,patch=new { general=new { theme="dark" } } };
            var write=host.BeginRequest("update_config",opts);
            var a=await write.Completion;
            var replay=await host.CallAsync(new {op="request",session_id=write.Session,id=write.Id,command="update_config",@params=opts});
            Require(JsonElement.DeepEquals(a,replay) && (await host.GetConfigAsync()).Revision==2,"Replayed write side effects");
            try {await host.CallAsync(new {op="request",session_id=write.Session,id=write.Id,command="ping",@params=new {}});throw new Exception("ID conflict accepted");}
            catch(HostException e) when(e.Error.Code=="CONFLICT") {}
            return new {first.Id,second_id=second.Id,write_id=write.Id,hello.ProtocolVersion,revision=2,query};
        });
        await Check("busy-32-stop-cancel-reserved",async () =>
        {
            var st=await host.CallAsync(new {op="status"});
            using var child=Process.GetProcessById(st.GetProperty("pid").GetInt32());
            Require(NtSuspendProcess(child.Handle)==0,"Suspend owned test engine");
            var pending=new List<HostRequest>();
            Task<JsonElement>? stop=null, cancel=null;
            string busy="";
            try
            {
                for(int n=0;n<32;n++) pending.Add(host.BeginRequest("get_config",new {}));
                var query=await host.QueryAsync(pending[^1]);
                for(int i=0; i<25 && query.GetProperty("status").GetString()!="pending"; i++)
                { await Task.Delay(10); query=await host.QueryAsync(pending[^1]); }
                Require(query.GetProperty("status").GetString()=="pending","Actual router pending");
                try {await host.RequestAsync("get_config",new {});throw new Exception("33rd request admitted");}
                catch(HostException e) when(e.Error.Code=="BUSY"){busy=e.Error.Code;}
                stop=host.RequestAsync("stop_script",new {execution_id="no-physical-input"});
                cancel=host.CancelAsync(pending[0]);
            }
            finally { Require(NtResumeProcess(child.Handle)==0,"Resume owned test engine"); }
            string stopResult="ok";
            try { await stop!; } catch(HostException e){stopResult=e.Error.Code;}
            Require(stopResult!="BUSY","Reserved stop capacity");
            var cancelled=await cancel!;
            var outcomes=await Task.WhenAll(pending.Select(async p=>
            {try {await p.Completion;return "ok";}catch(HostException e){return e.Error.Code;}}));
            Require(outcomes.All(s=>s is "ok" or "CANCELLED"),string.Join(",",outcomes));
            return new {count=pending.Count,busy,stopResult,cancelled,outcomes};
        });
        await Check("gap-read-recovery-and-detached-window",async () =>
        {
            string session=host.Events.Session!;
            long anchor=host.Events.Sequence;
            int called=0;
            using(var stopped=host.Events.Subscribe(e=>called++)) { }
            JsonElement Frame(string sid,long seq)=>Data(new {protocol_version=1,type="event",session_id=sid,seq,name="recognition_stats",payload=new {}});
            // Inject only an event discontinuity. Recovery requests still cross the real Rust/Python transport.
            host.Events.Accept(Frame(session,anchor+3));
            host.Events.Accept(Frame("old-session",anchor+100));
            host.Events.Accept(Frame(session,anchor+2));
            await Until(()=>host.Recovered?.Sequence==anchor+3);
            Require(called==0 && host.Events.Sequence==anchor+3 && host.Recovered!.Config.Revision==2,"Recovery or detached observer");
            int changes=0;
            using var states=host.Events.Subscribe(e=>{if(e.Name=="script_state_changed") changes++;});
            JsonElement Execution(long seq,string status)=>Data(new {protocol_version=1,type="event",session_id=session,seq,name="script_state_changed",payload=new {execution_id="script-terminal",status}});
            host.Events.Accept(Execution(anchor+4,"completed"));
            host.Events.Accept(Execution(anchor+6,"running"));
            await Until(()=>host.Recovered?.Sequence==anchor+6);
            Require(changes==1 && !host.Recovered!.Executions.ContainsKey("script-terminal"),"A late state revived a completed execution or hid a sequence gap");
            return new {anchor,seen=host.Events.Sequence,changes,target_count=host.Recovered!.Targets.Count,requested_revision=host.Recovered.Config.Revision};
        });
        await Check("stale-ticket-after-restart",async () =>
        {
            var old=host.BeginRequest("get_config",new {});
            await old.Completion;
            await host.CallAsync(new {op="stop"});
            await Start(host);
            Require(host.Events.Session!=old.Session,"Session changed");
            foreach(var call in new Func<Task<JsonElement>>[]{()=>host.CancelAsync(old),()=>host.QueryAsync(old)})
                try {await call();throw new Exception("Stale action accepted");} catch(HostException e) when(e.Error.Code=="ENGINE_UNAVAILABLE"){}
            var unknown=await host.RequestAsync("query_request",new {request_id="never-issued"});
            Require(unknown.GetProperty("status").GetString()=="unknown","Unknown is distinct from completed");
            return new {old=old.Session,current=host.Events.Session,unknown};
        });
    }

    private static async Task FaultCases()
    {
        Environment.SetEnvironmentVariable("SIDECAR_PYTHON",Path.ChangeExtension(typeof(Program).Assembly.Location,".exe"));
        await using var host=NewHost("fault-engine");
        await Start(host);
        await Check("interleaved-events-old-session-and-reverse-replies",async () =>
        {
            int events=0;
            using var subscription=host.Events.Subscribe(e=>{if(e.Name=="recognition_stats")Interlocked.Increment(ref events);});
            var work=Enumerable.Range(0,4).Select(i=>host.BeginRequest("ping",new {fixture="reverse",index=i})).ToArray();
            var replies=await Task.WhenAll(work.Select(x=>x.Completion));
            int[] indices=replies.Select(v=>v.GetProperty("index").GetInt32()).ToArray();
            Require(indices.SequenceEqual(Enumerable.Range(0,4)) && events>0,"Mismatch for reverse replies or interleaved event");
            return new {indices,ids=work.Select(x=>x.Id),events};
        });
        await Check("host-request-during-pending-response",async () =>
        {
            var response=await host.RequestAsync("ping",new {fixture="duplex"});
            var frame=response.GetProperty("host_response");
            Require(frame.GetProperty("id").GetString()==response.GetProperty("host_id").GetString() &&
                frame.GetProperty("type").GetString()=="host_response" && !frame.GetProperty("success").GetBoolean(),"Unbound host request did not return matching error");
            return response;
        });
        await Check("oversize-rejected-session-survives",async () =>
        {
            try {await host.RequestAsync("ping",new {padding=new string('x',2097152)});throw new Exception("Oversize write accepted");}
            catch(HostException e) when(e.Error.Code=="PROTOCOL_ERROR") {}
            var response=await host.RequestAsync("ping",new {fixture="oversize"});
            var diag=await host.CallAsync(new {op="diagnostics"});
            Require(response.GetProperty("alive").GetBoolean() && diag.GetProperty("malformed_line_count").GetInt64()==1,"Inbound oversize was not discarded");
            return new {send="PROTOCOL_ERROR",received_errors=1,state=diag.GetProperty("state")};
        });
        await Check("ten-malformed-frames-terminate-session",async () =>
        {
            string code="";
            try {await host.RequestAsync("ping",new {fixture="malformed"});}
            catch(HostException e){code=e.Error.Code;}
            var state=await host.CallAsync(new {op="status"});
            Require(code=="ENGINE_UNAVAILABLE" && state.GetProperty("state").GetString()=="exited" &&
                state.GetProperty("last_error").GetString()!.Contains("protocol_error"),"Fault did not close the engine: "+state);
            return new {code,lifecycle=state.GetProperty("state"),reason=state.GetProperty("last_error")};
        });
    }
}
