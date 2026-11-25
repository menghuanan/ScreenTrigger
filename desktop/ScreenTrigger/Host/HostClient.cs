using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;
using ScreenTrigger.Contracts;

namespace ScreenTrigger.Host;

internal sealed record HostRequest(string Session, string Id, Task<JsonElement> Completion);
internal sealed class HostException(ProtocolError error) : Exception($"{error.Code} · {error.Message}")
{
    public ProtocolError Error { get; } = error;
}

internal sealed class HostClient : IAsyncDisposable
{
    private const uint AbiVersion = 1;
    private const string Library = "screen_trigger_lib.dll";
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate void Receive(nint data, nuint length);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int WindowCall(nint data, nuint length, nint reply, nuint capacity);
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
    private static extern int st_host_attach_window(Handle host, WindowCall callback);
    private WindowCall? windowReceiver;
    private readonly CancellationTokenSource windowLifetime = new();

    public void AttachWindow(Func<JsonElement, CancellationToken, Task<object?>> handler)
    {
        windowReceiver = (data, length, reply, capacity) =>
        {
            byte[] response;
            try
            {
                var request = CopyJson(data, length);
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(windowLifetime.Token);
                if (request.GetProperty("op").GetString() != "pick_image")
                    timeout.CancelAfter(TimeSpan.FromSeconds(5));
                var done = new TaskCompletionSource<object?>(TaskCreationOptions.RunContinuationsAsynchronously);
                if (!dispatch(async () =>
                {
                    try
                    {
                        timeout.Token.ThrowIfCancellationRequested();
                        done.TrySetResult(await handler(request, timeout.Token));
                    }
                    catch (Exception ex) { done.TrySetException(ex); }
                })) return -1;
                var value = done.Task.WaitAsync(timeout.Token).GetAwaiter().GetResult();
                response = JsonSerializer.SerializeToUtf8Bytes(new { data = value });
            }
            catch (Exception ex) { response = JsonSerializer.SerializeToUtf8Bytes(new { error = ex.Message }); }
            if ((nuint)response.Length > capacity) return -1;
            Marshal.Copy(response, 0, reply, response.Length);
            return response.Length;
        };
        if (st_host_attach_window(handle, windowReceiver) != 0) throw new InvalidOperationException("无法连接应用窗口。");
    }
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
    private static extern uint st_host_abi_version();
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
    private static extern nint st_host_open(uint version, Receive callback);
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
    private static extern int st_host_call(Handle host, byte[] data, nuint length, Receive reply);
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
    private static extern void st_host_begin_close(Handle host);
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
    private static extern void st_host_close(nint host);

    private sealed class Handle : SafeHandleZeroOrMinusOneIsInvalid
    {
        private readonly Receive eventReceiver;
        public Handle(nint value, Receive callback) : base(true)
        {
            eventReceiver = callback;
            SetHandle(value);
        }
        protected override bool ReleaseHandle()
        {
            st_host_close(handle);
            GC.KeepAlive(eventReceiver);
            return true;
        }
    }

    private readonly Handle handle;
    private readonly object sync = new();
    private readonly HashSet<Task> pending = [];
    private bool closing;
    private long nextId;
    private enum Capacity { Local, Normal, Control, Stop }
    private readonly int[] inFlight = new int[4];
    private Task? disposal;
    private readonly Func<Action, bool> dispatch;
    private long recoveryVersion;
    public SessionEvents Events { get; }
    public RecoverySnapshot? Recovered { get; private set; }
    public event Action<Exception>? RecoveryFailed;
    public event Action<RecoverySnapshot>? RecoveryCompleted;

    public HostClient(Func<Action, bool> dispatch)
    {
        this.dispatch = dispatch;
        Events = new SessionEvents(dispatch, () => _ = StopAfterOverflowAsync());
        Events.Gap += Recover;
        uint actual = st_host_abi_version();
        if (actual != AbiVersion) throw new InvalidOperationException($"宿主 ABI 为 {actual}，界面需要版本 {AbiVersion}。");
        Receive receiver = (data, length) =>
        {
            try { Events.Accept(CopyJson(data, length)); }
            catch (Exception error) { System.Diagnostics.Trace.TraceError(error.ToString()); }
        };
        var native = st_host_open(AbiVersion, receiver);
        if (native == 0) throw new InvalidOperationException("无法建立桌面宿主会话。");
        handle = new Handle(native, receiver);
    }

    private static JsonElement CopyJson(nint data, nuint length)
    {
        var bytes = new byte[checked((int)length)];
        Marshal.Copy(data, bytes, 0, bytes.Length);
        using var doc = JsonDocument.Parse(bytes, new JsonDocumentOptions { AllowDuplicateProperties = false });
        return doc.RootElement.Clone();
    }

    private JsonElement Invoke(object call)
    {
        byte[] request = JsonSerializer.SerializeToUtf8Bytes(call);
        if (request.Length > 2 * 1024 * 1024) throw LocalError("PROTOCOL_ERROR", "消息超过 2 MiB 上限。");
        JsonElement reply = default;
        Exception? decodeError = null;
        int code = st_host_call(handle, request, (nuint)request.Length, (data, length) =>
        {
            try { reply = CopyJson(data, length); }
            catch (Exception ex) { decodeError = ex; }
        });
        if (decodeError != null) throw new InvalidDataException("宿主响应无法解码。", decodeError);
        if (code != 0 || reply.ValueKind == JsonValueKind.Undefined)
            throw new InvalidDataException($"宿主调用失败（{code}）。");
        if (!reply.GetProperty("success").GetBoolean())
        {
            var error = reply.GetProperty("error");
            throw new HostException(error.Deserialize<ProtocolError>(WireContracts.Json)!);
        }
        return reply.GetProperty("data");
    }

    private static HostException LocalError(string code, string message, bool retryable = false) =>
        new(new ProtocolError(code, message, JsonSerializer.SerializeToElement(new { }), retryable));

    public Task<JsonElement> CallAsync(object call) => Schedule(call, Capacity.Local);

    private Task<JsonElement> Schedule(object call, Capacity capacity)
    {
        lock (sync)
        {
            ObjectDisposedException.ThrowIf(closing, this);
            int index = (int)capacity;
            int limit = capacity == Capacity.Normal ? 32 : 8;
            if (inFlight[index] >= limit)
                return Task.FromException<JsonElement>(LocalError("BUSY", $"该类请求已有 {limit} 个在途。", true));
            inFlight[index]++;
            // A synchronous native wait must never starve the thread that sends Stop/Cancel.
            var task = Task.Factory.StartNew(() =>
            {
                try { return Invoke(call); }
                finally { lock (sync) inFlight[index]--; }
            }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
            pending.Add(task);
            _ = task.ContinueWith(t => { lock (sync) pending.Remove(t); }, TaskScheduler.Default);
            return task;
        }
    }

    private string CurrentSession() => Events.Session ?? throw LocalError("ENGINE_UNAVAILABLE", "引擎会话尚未就绪。", true);

    public HostRequest BeginRequest(string command, object parameters) => RequestIn(CurrentSession(), command, parameters);
    private HostRequest RequestIn(string session, string command, object parameters)
    {
        string id = $"ui-{Interlocked.Increment(ref nextId)}";
        var capacity = command switch { "stop_script" => Capacity.Stop,
            "cancel_request" or "cancel_job" or "ping" or "query_request" => Capacity.Control, _ => Capacity.Normal };
        if (Environment.GetEnvironmentVariable("ST_IPC_TRACE") == "1")
            Console.Error.WriteLine($"[desktop-ipc] {session} {id} {command}");
        var completion = Schedule(new { op = "request", session_id = session, id, command, @params = parameters }, capacity);
        return new(session, id, completion);
    }
    public Task<JsonElement> RequestAsync(string command, object parameters) => BeginRequest(command, parameters).Completion;
    public Task<JsonElement> QueryAsync(HostRequest request) =>
        RequestIn(request.Session, "query_request", new { request_id = request.Id }).Completion;
    public Task<JsonElement> CancelAsync(HostRequest request) =>
        Schedule(new { op = "cancel", session_id = request.Session, request_id = request.Id }, Capacity.Control);

    public async Task<EngineHello> HelloAsync()
    {
        var data = await RequestAsync("hello", new { }).ConfigureAwait(false);
        foreach (var capability in data.GetProperty("capabilities").EnumerateArray()) WireContracts.Validate("capability", capability);
        foreach (var manifest in data.GetProperty("model_manifests").EnumerateArray()) WireContracts.Validate("modelManifest", manifest);
        var hello = data.Deserialize<EngineHello>(WireContracts.Json)!;
        if (hello.ProtocolVersion != 1) throw LocalError("PROTOCOL_ERROR", "引擎协议版本不兼容。");
        return hello;
    }
    private static ConfigDocument UnwrapConfig(JsonElement data) => WireContracts.Read<ConfigDocument>(data.GetProperty("config"), "config");
    public async Task<ConfigDocument> GetConfigAsync() => UnwrapConfig(await RequestAsync("get_config", new { }).ConfigureAwait(false));
    public async Task<ConfigDocument> UpdateConfigAsync(long revision, JsonElement patch) =>
        UnwrapConfig(await RequestAsync("update_config", new { expected_revision = revision, patch }).ConfigureAwait(false));
    public async Task<ConfigDocument> ResetConfigAsync(long revision) =>
        UnwrapConfig(await RequestAsync("reset_config", new { expected_revision = revision }).ConfigureAwait(false));

    private async void Recover(string session, long sequence, string[] executions)
    {
        long version = Interlocked.Increment(ref recoveryVersion);
        try
        {
            var config = UnwrapConfig(await RequestIn(session, "get_config", new { }).Completion.ConfigureAwait(false));
            var targets = new List<Target>();
            int total;
            do
            {
                var page = await RequestIn(session, "list_targets", new { offset = targets.Count, limit = 100 }).Completion.ConfigureAwait(false);
                total = page.GetProperty("total").GetInt32();
                var items = page.GetProperty("items");
                if (items.GetArrayLength() == 0) break;
                foreach (var item in items.EnumerateArray()) targets.Add(WireContracts.Read<Target>(item, "target"));
            } while (targets.Count < total && Interlocked.Read(ref recoveryVersion) == version);
            var active = executions.ToHashSet();
            try
            {
                // A gap can include the very first event for a new root. The archive holds 100 terminal and at most 3 active roots.
                var recent = await RequestIn(session, "list_execution_history", new { offset = 0, limit = 103 }).Completion.ConfigureAwait(false);
                foreach (var item in recent.GetProperty("items").EnumerateArray())
                    if (item.GetProperty("status").GetString() is "starting" or "running" or "paused" or "debug_paused" or "stopping")
                        active.Add(item.GetProperty("execution_id").GetString()!);
            }
            catch (HostException ex) when (ex.Error.Code == "UNKNOWN_COMMAND") { /* Engines without history can still answer known execution IDs. */ }
            var states = new Dictionary<string, JsonElement>();
            foreach (var id in active)
                states[id] = (await RequestIn(session, "query_execution", new { execution_id = id }).Completion.ConfigureAwait(false)).GetProperty("state");
            dispatch(() =>
            {
                if (Events.Session == session && Interlocked.Read(ref recoveryVersion) == version)
                {
                    Recovered = new(session, sequence, config, targets, states);
                    RecoveryCompleted?.Invoke(Recovered);
                }
            });
        }
        catch (Exception ex) { dispatch(() => { if (Events.Session == session) RecoveryFailed?.Invoke(ex); }); }
    }

    private async Task StopAfterOverflowAsync()
    {
        try { await CallAsync(new { op = "stop" }).ConfigureAwait(false); }
        catch (Exception ex) { System.Diagnostics.Trace.TraceError(ex.ToString()); }
        dispatch(() => RecoveryFailed?.Invoke(LocalError("PROTOCOL_ERROR", "界面事件队列已满，引擎已停止。", true)));
    }

    public ValueTask DisposeAsync()
    {
        lock (sync)
        {
            if (disposal != null) return new(disposal);
            closing = true;
            windowLifetime.Cancel();
            Events.Close();
            disposal = DrainAsync(pending.ToArray());
            return new(disposal);
        }
    }

    private async Task DrainAsync(Task[] running)
    {
        await Task.Run(() => st_host_begin_close(handle)).ConfigureAwait(false);
        try { await Task.WhenAll(running).ConfigureAwait(false); }
        catch { /* Individual requests deliver their errors to their callers. */ }
        await Task.Run(handle.Dispose).ConfigureAwait(false);
    }
}
