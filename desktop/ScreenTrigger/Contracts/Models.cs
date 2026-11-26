using System.Text.Json;
using System.Text.Json.Serialization;

namespace ScreenTrigger.Contracts;

internal sealed record PixelPoint(int X, int Y)
{
    internal (double X, double Y) ToOverlayDip(double scale, int originX, int originY)
    {
        if (!double.IsFinite(scale) || scale <= 0) throw new ArgumentOutOfRangeException(nameof(scale));
        return (((double)X - originX) / scale, ((double)Y - originY) / scale);
    }
}
internal sealed record PixelRect(int X, int Y, int Width, int Height);

internal sealed record AnchoredPoint(int Version, string Scope, string Strategy, int ReferenceWidth,
                                     int ReferenceHeight, int X, int Y)
{
    internal PixelPoint Project(PixelRect current)
    {
        int Axis(int value, int reference, int size, int origin)
        {
            if (size <= 0 || value < 0 || value >= reference)
                throw new ContractException("anchor", "REGION_LIMIT_VIOLATION");
            long local = value;
            if (Strategy == "relative")
                local = reference == 1 ? 0 : ((long)value * (size - 1) + (reference - 1) / 2) / (reference - 1);
            if (local >= size || origin + local is < int.MinValue or > int.MaxValue)
                throw new ContractException("anchor", "REGION_LIMIT_VIOLATION");
            return (int)(origin + local);
        }
        return new(Axis(X, ReferenceWidth, current.Width, current.X), Axis(Y, ReferenceHeight, current.Height, current.Y));
    }
}

[JsonPolymorphic(TypeDiscriminatorPropertyName = "space")]
[JsonDerivedType(typeof(ScreenRegion), "screen_physical")]
[JsonDerivedType(typeof(ClientRegion), "window_client_physical")]
[JsonDerivedType(typeof(ImageRegion), "image_pixel")]
internal abstract record Region(int X, int Y, int Width, int Height);
internal sealed record ScreenRegion(int X, int Y, int Width, int Height, string DisplayId) : Region(X,Y,Width,Height);
internal sealed record ClientRegion(int X, int Y, int Width, int Height, string BindingId) : Region(X,Y,Width,Height);
internal sealed record ImageRegion(int X, int Y, int Width, int Height, string ImageId) : Region(X,Y,Width,Height);

internal sealed record ImageRef(string ImageId, int Width, int Height, string Format, string Sha256);
internal sealed record Frame(string FrameId, ImageRef ImageRef, string Space, PixelPoint Origin, string CapturedAt, long GeometryRevision, string? BindingId);
internal sealed record ImageAsset(string Path, string Sha256, int Width, int Height, string Format, string StorageMode);
internal sealed record Target(string Id, long SampleNo, long Revision, string Name, string? GroupId, ImageAsset Image,
    string? ThumbnailPath, string AnchorConditionId, JsonElement Conditions, string CreatedAt, string UpdatedAt, JsonElement Metadata);
internal sealed record Group(string Id, long Revision, string Name, string? ParentGroupId, int Depth, string CreatedAt, string UpdatedAt);
internal sealed record ScriptParameter(string Name, string Type, bool Required, JsonElement Default, string Description);
internal sealed record Script(string Id, long Revision, string Name, string SourcePath, string SourceSha256,
    ScriptParameter[] Parameters, string CreatedAt, string UpdatedAt,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] JsonElement? ActionPolicy = null);
internal sealed record Match(string? TargetId, long? SampleNo, string Space, PixelRect Bbox, PixelPoint Center,
    double Confidence, long GeometryRevision, JsonElement[] Evidence);
internal sealed record RunConfig(string ScriptId, JsonElement Args, string Mode, long? Iterations, string? StopTargetId,
    bool Debug, Region? Region, string? DisplayId, string? BindingId, bool AllowExit, double FrequencyHz, long TotalTimeoutMs,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? WindowMode = null);
internal sealed record MonitorConfig(string TargetId, Region? Region, string? DisplayId, string? BindingId, bool AllowExit,
    double FrequencyHz, long TotalTimeoutMs, bool Debug, bool Repeat, long WaitTimeoutMs, string OnMatch, string? ActionScriptId,
    JsonElement ActionArgs, string OnTimeout, string? TimeoutScriptId, JsonElement TimeoutArgs, int MaxRetries, long RetryDelayMs,
    string Trigger, long CooldownMs, [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? WindowMode = null);

internal sealed record Capability(string Id, string State, string? Reason = null);
internal sealed record ModelManifest(string ProviderId, int ApiVersion, string ProviderVersion, string[] SupportedConditionTypes,
    string[] ModelIds, string[] SupportedLanguages, bool ThreadSafe, bool Offline, string License, string Source,
    string Sha256, string[] SupportedCharsets);
internal sealed record EngineHello(int ProtocolVersion, string EngineVersion, string? LuaVersion, int SchemaVersion,
    Capability[] Capabilities, ModelManifest[] ModelManifests);
internal sealed record ProtocolError(string Code, string Message, JsonElement Details, bool Retryable);

internal sealed record ConfigDocument(int SchemaVersion, long Revision, GeneralConfig General, RecognitionConfig Recognition,
    CaptureConfig Capture, InputConfig Input, ExecutionConfig Execution, HotkeyConfig Hotkeys, LogConfig Logging);
internal sealed record GeneralConfig(string Language, string Theme);
internal sealed record RgbTolerance(int R, int G, int B);
internal sealed record HsvTolerance(int H, int S, int V);
internal sealed record RecognitionConfig(double DefaultFrequencyHz, double DefaultThreshold, RgbTolerance RgbTolerance,
    HsvTolerance HsvTolerance, string? OcrModelId, double OcrMinConfidence, string ModelDirectory);
internal sealed record CaptureConfig(int PreviewHz, int PngCompressionLevel);
internal sealed record InputConfig(bool AllowExit, int MoveDurationMs, int TypeIntervalMs);
internal sealed record ExecutionConfig(long TotalTimeoutMs);
internal sealed record HotkeyConfig(string StartSelection, string StopScript, string PauseScript);
internal sealed record LogConfig(string Level, long RotateBytes, int FileCount);
