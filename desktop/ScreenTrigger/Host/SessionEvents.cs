using System.Text.Json;

namespace ScreenTrigger.Host;

internal sealed record EngineEvent(string Session, long Sequence, string Name, JsonElement Payload);
internal sealed record RecoverySnapshot(string Session, long Sequence, Contracts.ConfigDocument Config,
    IReadOnlyList<Contracts.Target> Targets, IReadOnlyDictionary<string, JsonElement> Executions);

// Native threads only copy and queue. All window callbacks run on the owning UI dispatcher.
internal sealed class SessionEvents(Func<Action, bool> dispatch, Action overflow)
{
    private readonly object sync = new();
    private readonly LinkedList<EngineEvent> queue = new();
    private readonly Dictionary<string, LinkedListNode<EngineEvent>> merged = new();
    private readonly HashSet<Subscription> subscribers = [];
    private readonly HashSet<string> activeExecutions = [];
    private readonly HashSet<string> terminalExecutions = [];
    private readonly Queue<string> terminalOrder = new();
    private string? session, retired;
    private long sequence;
    private bool awaitingReady = true, posted, closed;

    public string? Session { get { lock (sync) return session; } }
    public long Sequence { get { lock (sync) return sequence; } }
    public event Action<string, long, string[]>? Gap;

    public void BeginStart()
    {
        lock (sync)
        {
            if (session != null) retired = session;
            session = null;
            sequence = 0;
            awaitingReady = true;
            queue.Clear();
            merged.Clear();
            activeExecutions.Clear();
            terminalExecutions.Clear();
            terminalOrder.Clear();
        }
    }

    public void End()
    {
        lock (sync)
        {
            if (session != null) retired = session;
            session = null;
            awaitingReady = false;
            queue.Clear();
            merged.Clear();
            activeExecutions.Clear();
        }
    }

    internal sealed class Subscription(SessionEvents owner, Action<EngineEvent> receive) : IDisposable
    {
        internal void Deliver(EngineEvent item) => receive(item);
        public void Dispose() { lock (owner.sync) owner.subscribers.Remove(this); }
    }

    public IDisposable Subscribe(Action<EngineEvent> receive)
    {
        var subscription = new Subscription(this, receive);
        lock (sync) { ObjectDisposedException.ThrowIf(closed, this); subscribers.Add(subscription); }
        return subscription;
    }

    private static string MergeKey(EngineEvent item) => item.Name == "recognition_stats" &&
        item.Payload.TryGetProperty("execution_id",out var id) ? $"{item.Name}/{id}" : item.Name;

    public void Accept(JsonElement frame)
    {
        if (frame.GetProperty("protocol_version").GetInt32() != 1 || frame.GetProperty("type").GetString() != "event") return;
        var item = new EngineEvent(frame.GetProperty("session_id").GetString()!, frame.GetProperty("seq").GetInt64(),
            frame.GetProperty("name").GetString()!, frame.GetProperty("payload"));
        bool schedule = false, failed = false;
        string[]? recover = null;
        lock (sync)
        {
            if (closed || string.IsNullOrEmpty(item.Session)) return;
            if (awaitingReady && item.Name == "system_ready" && item.Sequence == 1 && item.Session != retired)
            {
                session = item.Session;
                awaitingReady = false;
            }
            if (item.Session != session || item.Sequence <= sequence) return;
            bool gap = item.Sequence > sequence + 1;
            sequence = item.Sequence;
            bool deliveredTerminal = false;
            if (item.Name is "script_state_changed" or "monitor_state_changed" &&
                item.Payload.TryGetProperty("execution_id", out var id) && id.GetString() is string execution &&
                item.Payload.TryGetProperty("status", out var status))
            {
                deliveredTerminal = terminalExecutions.Contains(execution);
                if (!deliveredTerminal && status.GetString() is ("completed" or "cancelled" or "failed"))
                {
                    activeExecutions.Remove(execution);
                    terminalExecutions.Add(execution);
                    terminalOrder.Enqueue(execution);
                    if (terminalOrder.Count > 10000) terminalExecutions.Remove(terminalOrder.Dequeue());
                }
                else if (!deliveredTerminal) activeExecutions.Add(execution);
            }
            if (gap) recover = activeExecutions.ToArray();
            bool coalescible = item.Name is "selection_updated" or "recognition_stats";
            var key = MergeKey(item);
            if (item.Name == "recognition_stats" && item.Payload.TryGetProperty("execution_id", out var owner) && terminalExecutions.Contains(owner.GetString()!)) deliveredTerminal = true;
            if (coalescible && merged.Remove(key, out var previous)) queue.Remove(previous);
            if (!deliveredTerminal && queue.Count >= 1024) { End(); failed = true; }
            else if (!deliveredTerminal)
            {
                var node = queue.AddLast(item);
                if (coalescible) merged[key] = node;
                if (!posted) { posted = true; schedule = true; }
            }
        }
        if (failed) { overflow(); return; }
        if (recover != null) Gap?.Invoke(item.Session, item.Sequence, recover);
        if (schedule && !dispatch(Drain)) { End(); overflow(); }
    }

    private void Drain()
    {
        // Yield to keyboard and stop controls between bursts.
        for (int i = 0; i < 32; i++)
        {
            EngineEvent item;
            Subscription[] listeners;
            lock (sync)
            {
                if (closed || queue.First == null) { posted = false; return; }
                item = queue.First.Value;
                queue.RemoveFirst();
                merged.Remove(MergeKey(item));
                listeners = subscribers.ToArray();
            }
            foreach (var listener in listeners)
            {
                lock (sync) { if (!subscribers.Contains(listener) || session != item.Session) continue; }
                try { listener.Deliver(item); }
                catch (Exception ex) { System.Diagnostics.Trace.TraceError(ex.ToString()); }
            }
        }
        if (!dispatch(Drain)) { End(); overflow(); }
    }

    public void Close()
    {
        lock (sync) { closed = true; End(); subscribers.Clear(); }
    }
}
