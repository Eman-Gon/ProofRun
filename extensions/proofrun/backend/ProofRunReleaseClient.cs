using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Configuration;

namespace Duplo.Extension.ProofRun;

// Fault text is fixed. Worker response bodies, credentials and origin URLs are never copied into faults.
public sealed class ProofRunReleaseException(string message, int statusCode = 502) : Exception(message)
{
    public int StatusCode { get; } = statusCode;
}

public sealed class ProofRunReleaseClient : IDisposable
{
    private const string Schema = "proofrun.release.v1";
    private const int MaxJsonBytes = 4 * 1024 * 1024;
    private const int MaxArtifactBytes = 16 * 1024 * 1024;
    private const string InvalidRun = "The release worker returned an invalid or differently bound investigation; no recommendation was inferred.";
    private static readonly Regex Scope = new(@"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Id = new(@"\A[A-Za-z0-9][A-Za-z0-9_-]{0,79}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Commit = new(@"\A[a-f0-9]{40}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Hash = new(@"\A[a-f0-9]{64}\z", RegexOptions.CultureInvariant);
    private static readonly Regex RunId = new(@"\Arelease-[a-f0-9]{40}\z", RegexOptions.CultureInvariant);
    private static readonly Regex ArtifactId = new(@"\A[a-z0-9-]+\.(?:json|diff)\z", RegexOptions.CultureInvariant);
    private readonly HttpClient _http;

    public static ProofRunReleaseClient FromConfiguration(IConfiguration config)
        => new(config["PROOFRUN_RELEASE_WORKER_URL"], config["PROOFRUN_WORKER_TOKEN"]);

    public ProofRunReleaseClient(string? workerUrl, string? workerToken, HttpMessageHandler? handler = null)
    {
        if (workerUrl is null || workerUrl.Any(c => c < 33 || c == '\\') ||
            !Uri.TryCreate(workerUrl, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https") ||
            uri.UserInfo.Length != 0 || uri.Query.Length != 0 || uri.Fragment.Length != 0 || uri.AbsolutePath != "/")
            throw new ProofRunReleaseException("Configure the release worker origin in server-side PROOFRUN_RELEASE_WORKER_URL.");
        if (uri.Scheme == "http" && !uri.IsLoopback && uri.Host != "host.docker.internal")
            throw new ProofRunReleaseException("Use HTTPS for a remote release worker; HTTP is allowed only for the local development host.");
        if (workerToken is null || workerToken.Length is < 32 or > 512 || workerToken.Any(c => c < 33 || c > 126))
            throw new ProofRunReleaseException("Configure a server-side PROOFRUN_WORKER_TOKEN of 32–512 printable characters without whitespace.");
        _http = new HttpClient(handler ?? new HttpClientHandler { AllowAutoRedirect = false })
        {
            BaseAddress = uri,
            // A linked deadline below covers both headers and streaming body reads.
            Timeout = Timeout.InfiniteTimeSpan
        };
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", workerToken);
    }

    public async Task<JsonObject> GetTargetsAsync(string workspaceId, CancellationToken ct)
    {
        var document = await JsonAsync(workspaceId, HttpMethod.Get, "v1/release-targets", null, ct);
        const string invalid = "The release worker returned an unsupported target registry.";
        if (Text(document, "schema_version") != Schema || document["targets"] is not JsonArray targets || targets.Count > 100)
            throw new ProofRunReleaseException(invalid);
        var clean = new JsonArray();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in targets)
        {
            if (item is not JsonObject target || !Id.IsMatch(Text(target, "id")) || !seen.Add(Text(target, "id")) ||
                !BoundedText(target, "name", 160) || !Boolean(target, "repair_enabled", out _) ||
                !Boolean(target, "staging_configured", out _) || !Hash.IsMatch(Text(target, "contract_hash")) ||
                target["revisions"] is not JsonArray revisions || revisions.Count == 0 ||
                revisions.Any(value => !Commit.IsMatch(ScalarText(value))) ||
                target["requirements"] is not JsonArray requirements || requirements.Count is < 1 or > 20)
                throw new ProofRunReleaseException(invalid);
            var ids = new HashSet<string>(StringComparer.Ordinal);
            var cleanRequirements = new JsonArray();
            foreach (var requirement in requirements)
            {
                if (requirement is not JsonObject req || !Id.IsMatch(Text(req, "id")) || !ids.Add(Text(req, "id")) ||
                    !BoundedText(req, "description", 2000) || Text(req, "kind") != "preserve_response" ||
                    !BoundedText(req, "path_prefix", 512) || !Text(req, "path_prefix").StartsWith('/') ||
                    req["methods"] is not JsonArray methods || methods.Count is < 1 or > 7 ||
                    methods.Any(value => ScalarText(value) is not ("GET" or "HEAD" or "POST" or "PUT" or "PATCH" or "DELETE" or "OPTIONS")))
                    throw new ProofRunReleaseException(invalid);
                cleanRequirements.Add(Only(req, "id", "description", "kind", "path_prefix", "methods"));
            }
            var copy = Only(target, "id", "name", "revisions", "repair_enabled", "staging_configured", "contract_hash");
            copy["requirements"] = cleanRequirements;
            clean.Add(copy);
        }
        return new JsonObject { ["schema_version"] = Schema, ["targets"] = clean };
    }

    public async Task<JsonObject> SubmitAsync(string workspaceId, JsonObject input, CancellationToken ct)
    {
        RequireScope(workspaceId);
        var request = NormalizeRequest(input);
        var run = ValidateRun(await JsonAsync(workspaceId, HttpMethod.Post, "v1/release-runs", request, ct));
        if (!JsonNode.DeepEquals(run["request"], request)) throw new ProofRunReleaseException(InvalidRun);
        if (request["event_id"] is not null && Text(run, "id") != EventRunId(workspaceId, Text(request, "event_id")))
            throw new ProofRunReleaseException(InvalidRun);
        return run;
    }

    public async Task<JsonObject> GetRunAsync(string workspaceId, string runId, CancellationToken ct)
    {
        RequireScope(workspaceId);
        RequireRunId(runId);
        var run = ValidateRun(await JsonAsync(workspaceId, HttpMethod.Get, $"v1/release-runs/{runId}", null, ct));
        if (Text(run, "id") != runId) throw new ProofRunReleaseException(InvalidRun);
        if (run["request"] is JsonObject request && request["event_id"] is not null &&
            runId != EventRunId(workspaceId, Text(request, "event_id")))
            throw new ProofRunReleaseException(InvalidRun);
        return run;
    }

    public async Task<byte[]> GetArtifactAsync(string workspaceId, string runId, string artifactId, CancellationToken ct)
    {
        if (artifactId is null || artifactId.Length > 128 || !ArtifactId.IsMatch(artifactId))
            throw new ArgumentException("Use a listed release evidence artifact identifier.");
        // Re-fetch under the authorized scope: callers cannot supply another run's artifact manifest.
        var run = await GetRunAsync(workspaceId, runId, ct);
        var artifact = (run["result"]?["artifacts"] as JsonArray)?.OfType<JsonObject>()
            .SingleOrDefault(item => Text(item, "id") == artifactId);
        if (artifact is null) throw new ProofRunReleaseException("The requested artifact is not listed for this release investigation.", 404);
        using var request = Request(workspaceId, HttpMethod.Get, $"v1/release-runs/{runId}/artifacts/{artifactId}");
        var bytes = await SendAsync(request, MaxArtifactBytes, ct);
        var hash = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
        if (hash != Text(artifact, "sha256") || !Integer(artifact, "bytes", out var length) || length != bytes.LongLength)
            throw new ProofRunReleaseException("Release artifact integrity failed: received bytes do not match the recorded hash and size.");
        return bytes;
    }

    public static string EventRunId(string workspaceId, string eventId)
    {
        RequireScope(workspaceId);
        if (eventId is null || !Id.IsMatch(eventId)) throw new ArgumentException("Use a valid release event identifier.");
        // Scope and event IDs are restricted to ASCII; this matches the worker's canonical JSON array.
        var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new[] { workspaceId, eventId }));
        return "release-" + Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant()[..40];
    }

    private static JsonObject NormalizeRequest(JsonObject input)
    {
        const string invalid = "Use a registered target, distinct exact lowercase commit SHAs, a 180–600 second budget, and a supported release request.";
        var fields = new HashSet<string>(["target_id", "baseline_revision", "candidate_revision", "benefit", "budget_seconds", "repair", "event_id"]);
        if (input is null || input.Any(pair => !fields.Contains(pair.Key)) || !Id.IsMatch(Text(input, "target_id")) ||
            !Commit.IsMatch(Text(input, "baseline_revision")) || !Commit.IsMatch(Text(input, "candidate_revision")) ||
            Text(input, "baseline_revision") == Text(input, "candidate_revision")) throw new ArgumentException(invalid);
        var result = input.DeepClone().AsObject();
        if (!result.ContainsKey("budget_seconds")) result["budget_seconds"] = 300;
        if (!result.ContainsKey("repair")) result["repair"] = false;
        if (!result.ContainsKey("benefit")) result["benefit"] = "";
        if (!Integer(result, "budget_seconds", out var budget) || budget is < 180 or > 600 ||
            !Boolean(result, "repair", out _) || !StringValue(result["benefit"], out var benefit) || benefit.Length > 2000 ||
            (result.ContainsKey("event_id") && !Id.IsMatch(Text(result, "event_id"))) ||
            Encoding.UTF8.GetByteCount(result.ToJsonString()) > 16000) throw new ArgumentException(invalid);
        return result;
    }

    private static JsonObject ValidateRun(JsonObject run)
    {
        if (Text(run, "schema_version") != Schema || !RunId.IsMatch(Text(run, "id")) ||
            Text(run, "status") is not ("queued" or "running" or "completed" or "failed") ||
            run["request"] is not JsonObject request || !Date(run, "created_at") || !Date(run, "updated_at") ||
            (run["error"] is not null && !BoundedText(run, "error", 4096)))
            throw new ProofRunReleaseException(InvalidRun);
        JsonObject normalized;
        try { normalized = NormalizeRequest(request); }
        catch (ArgumentException) { throw new ProofRunReleaseException(InvalidRun); }
        if (run["result"] is not null && run["result"] is not JsonObject ||
            Text(run, "status") == "completed" && run["result"] is not JsonObject)
            throw new ProofRunReleaseException(InvalidRun);
        var clean = Only(run, "schema_version", "id", "status", "created_at", "updated_at", "error");
        clean["request"] = normalized;
        if (run["events"] is JsonArray events)
        {
            if (events.Count > 100 || events.Any(item => item is not JsonObject)) throw new ProofRunReleaseException(InvalidRun);
            clean["events"] = new JsonArray(events.OfType<JsonObject>().Select(item => (JsonNode)Only(item,
                "at", "stage", "type", "step", "status", "probe_id", "repair_id", "revision")).ToArray());
        }
        if (run["result"] is JsonObject result)
        {
            if (Text(result, "schema_version") != Schema || Text(result, "target_id") != Text(normalized, "target_id") ||
                Text(result, "recommendation") is not ("update" or "skip" or "postpone") ||
                !BoundedText(result, "summary", 10000) || result["revisions"] is not JsonObject revisions ||
                Text(revisions, "baseline") != Text(normalized, "baseline_revision") ||
                Text(revisions, "candidate") != Text(normalized, "candidate_revision") ||
                result["coverage"] is not JsonObject coverage ||
                !Integer(coverage, "requirements_total", out var total) || total is < 1 or > 20 ||
                !Integer(coverage, "requirements_exercised", out var exercised) || exercised < 0 || exercised > total ||
                result["findings"] is not JsonArray findings || findings.Count > 16 ||
                findings.Any(item => item is not JsonObject finding || !BoundedText(finding, "id", 80) ||
                    !BoundedText(finding, "title", 2000) || !Id.IsMatch(Text(finding, "requirement_id")) ||
                    Text(finding, "status") is not ("confirmed" or "inconclusive") || finding["evidence"] is not JsonObject) ||
                result["tests"] is not JsonObject || result["agent"] is not JsonObject || result["staging"] is not JsonObject ||
                result["limitations"] is not JsonArray limits || limits.Count > 100 ||
                limits.Any(item => !StringValue(item, out var text) || text.Length > 4096))
                throw new ProofRunReleaseException(InvalidRun);
            var artifacts = ValidateArtifacts(result["artifacts"]);
            var cleanResult = Only(result, "schema_version", "recommendation", "summary", "target_id", "contract_hash", "request_hash",
                "revisions", "source_hashes", "coverage", "findings", "tests", "repairs", "agent", "staging", "limitations", "elapsed_seconds", "error");
            cleanResult["artifacts"] = artifacts;
            clean["result"] = cleanResult;
        }
        return clean;
    }

    private static JsonArray ValidateArtifacts(JsonNode? value)
    {
        if (value is not JsonArray artifacts || artifacts.Count > 32) throw new ProofRunReleaseException(InvalidRun);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var clean = new JsonArray();
        foreach (var item in artifacts)
        {
            if (item is not JsonObject artifact || Text(artifact, "id").Length > 128 || !ArtifactId.IsMatch(Text(artifact, "id")) ||
                !seen.Add(Text(artifact, "id")) || !Hash.IsMatch(Text(artifact, "sha256")) ||
                !Integer(artifact, "bytes", out var bytes) || bytes is < 0 or > MaxArtifactBytes)
                throw new ProofRunReleaseException(InvalidRun);
            clean.Add(Only(artifact, "id", "sha256", "bytes"));
        }
        return clean;
    }

    private async Task<JsonObject> JsonAsync(string workspaceId, HttpMethod method, string path, JsonObject? body, CancellationToken ct)
    {
        using var request = Request(workspaceId, method, path);
        if (body is not null) request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        var bytes = await SendAsync(request, MaxJsonBytes, ct);
        try { return JsonNode.Parse(bytes, documentOptions: new JsonDocumentOptions { MaxDepth = 64 }) as JsonObject ?? throw new JsonException(); }
        catch (JsonException) { throw new ProofRunReleaseException("The release worker returned invalid JSON; no recommendation was inferred."); }
    }

    private static HttpRequestMessage Request(string workspaceId, HttpMethod method, string path)
    {
        RequireScope(workspaceId);
        var request = new HttpRequestMessage(method, path);
        request.Headers.Add("X-ProofRun-Scope", workspaceId);
        return request;
    }

    private async Task<byte[]> SendAsync(HttpRequestMessage request, int limit, CancellationToken ct)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(30));
        try
        {
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
            if (!response.IsSuccessStatusCode)
                throw response.StatusCode switch
                {
                    HttpStatusCode.NotFound => new ProofRunReleaseException("The release target, run or artifact is unavailable in this workspace.", 404),
                    HttpStatusCode.Conflict => new ProofRunReleaseException("The release request conflicts with an existing event, the queue is full, or saved evidence changed.", 409),
                    HttpStatusCode.BadRequest => new ProofRunReleaseException("The worker rejected the release request. Check the configured target, exact commits, budget and event identifier.", 400),
                    HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden => new ProofRunReleaseException("The release worker rejected server authentication. Check backend credential configuration."),
                    _ => new ProofRunReleaseException("The release worker request failed; redirects and upstream response bodies are not forwarded.")
                };
            if (response.Content.Headers.ContentLength > limit) throw new ProofRunReleaseException("The release worker response exceeded the gateway size limit.");
            await using var input = await response.Content.ReadAsStreamAsync(deadline.Token);
            using var output = new MemoryStream();
            var buffer = new byte[8192];
            int count;
            while ((count = await input.ReadAsync(buffer, deadline.Token)) > 0)
            {
                if (output.Length + count > limit) throw new ProofRunReleaseException("The release worker response exceeded the gateway size limit.");
                output.Write(buffer, 0, count);
            }
            if (response.Content.Headers.ContentLength is long length && length != output.Length)
                throw new ProofRunReleaseException("The release worker response was incomplete.");
            return output.ToArray();
        }
        catch (HttpRequestException) { throw new ProofRunReleaseException("The release worker could not be reached from the backend."); }
        catch (IOException) { throw new ProofRunReleaseException("The release worker response could not be read completely."); }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        { throw new ProofRunReleaseException("The release worker request timed out. Its final execution status is not established."); }
    }

    private static JsonObject Only(JsonObject value, params string[] fields)
    {
        var result = new JsonObject();
        foreach (var field in fields) if (value.ContainsKey(field)) result[field] = value[field]?.DeepClone();
        return result;
    }
    private static string Text(JsonObject value, string key) => ScalarText(value[key]);
    private static string ScalarText(JsonNode? value) => StringValue(value, out var text) ? text : "";
    private static bool StringValue(JsonNode? value, out string text)
    {
        text = "";
        return value is JsonValue scalar && scalar.TryGetValue(out text!);
    }
    private static bool Integer(JsonObject value, string key, out long number)
    {
        number = 0;
        return value[key] is JsonValue scalar && scalar.TryGetValue(out number);
    }
    private static bool Boolean(JsonObject value, string key, out bool flag)
    {
        flag = false;
        return value[key] is JsonValue scalar && scalar.TryGetValue(out flag);
    }
    private static bool BoundedText(JsonObject value, string key, int limit) => Text(value, key) is { Length: > 0 } text && text.Length <= limit;
    private static bool Date(JsonObject value, string key) => DateTimeOffset.TryParse(Text(value, key), CultureInfo.InvariantCulture, DateTimeStyles.None, out _);
    private static void RequireScope(string scope)
    {
        if (scope is null || !Scope.IsMatch(scope)) throw new ArgumentException("A valid authorized workspace identifier is required.");
    }
    private static void RequireRunId(string id)
    {
        if (id is null || !RunId.IsMatch(id)) throw new ArgumentException("Use a valid release investigation identifier.");
    }
    public void Dispose() => _http.Dispose();
}
