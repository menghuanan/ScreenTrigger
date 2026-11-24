using System.Text;
using System.Text.Json;

// Test-only subprocess replacing the engine for deliberate ordering and framing faults.
internal static class FaultPeer
{
    internal static int Run(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.InputEncoding = new UTF8Encoding(false);
        string session=args[Array.IndexOf(args,"--session-id")+1];
        object versions=new {protocol_version=1,engine_version="contract-fixture",lua_version=(string?)null,schema_version=2};
        void Send(object value) {Console.WriteLine(JsonSerializer.Serialize(value)); Console.Out.Flush();}
        void Respond(string id,object data,string? sid=null) => Send(new {protocol_version=1,session_id=sid??session,type="response",id,success=true,data});
        void Event(string name,long seq,object payload) => Send(new {protocol_version=1,session_id=session,type="event",name,seq,timestamp=DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ"),payload});
        Event("system_ready",1,new {capabilities=Array.Empty<object>(),versions});
        var held=new List<(string Id,int Index)>();
        string? duplex=null, hostId=null;
        int eventSeq=1;
        while (Console.ReadLine() is string line)
        {
            using var doc=JsonDocument.Parse(line);
            var msg=doc.RootElement;
            string id=msg.GetProperty("id").GetString()!;
            if (msg.GetProperty("type").GetString()=="host_response")
            {
                if (duplex==null || id!=hostId) return 3;
                Respond(duplex,new {host_id=id,host_response=msg.Clone()});
                duplex=null;
                continue;
            }
            var p=msg.GetProperty("params");
            if(msg.GetProperty("command").GetString()=="hello")
            {
                Respond(id,new {protocol_version=1,engine_version="contract-fixture",lua_version=(string?)null,schema_version=2,
                    capabilities=Array.Empty<object>(),model_manifests=Array.Empty<object>()});
                continue;
            }
            string? mode=p.TryGetProperty("fixture",out var flag)?flag.GetString():null;
            switch(mode)
            {
                case "reverse":
                    held.Add((id,p.GetProperty("index").GetInt32()));
                    if(held.Count==4)
                    {
                        foreach(var request in held.AsEnumerable().Reverse())
                        {
                            Event("recognition_stats",++eventSeq,new { fixture=true });
                            Respond(request.Id,new {index=-100},"session-expired");
                            Respond(request.Id,new {index=request.Index});
                        }
                        held.Clear();
                    }
                    break;
                case "duplex":
                    duplex=id;
                    hostId="py-"+Guid.NewGuid();
                    Send(new {protocol_version=1,session_id=session,type="host_request",id=hostId,command="window_snapshot",
                        @params=new {binding_id="unbound-test-window"}});
                    break;
                case "oversize":
                    Console.Out.WriteLine(new string('x',2*1024*1024+4096));
                    Respond(id,new {alive=true});
                    break;
                case "malformed":
                    for(int i=0;i<10;i++) Console.Out.WriteLine("{broken");
                    Console.Out.Flush();
                    break;
                default: Respond(id,new {alive=true});break;
            }
        }
        return 0;
    }
}
