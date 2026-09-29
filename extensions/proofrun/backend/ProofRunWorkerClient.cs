using System.Net;
using System.Net.Http.Headers;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Configuration;

namespace Duplo.Extension.ProofRun;

// Messages in this type are deliberately static: upstream bodies/URLs/credentials never become faults.
public sealed class ProofRunBridgeException(string message) : Exception(message) { }

public sealed class ProofRunResearchRequest
{
    public string ClientNonce { get; set; } = "";
    public string Domain { get; set; } = "";
    public string Month { get; set; } = "";
}

public sealed class ProofRunWorkerClient : IDisposable
{
    public const string CaseId = "customer-nickname-v1";
    private const int MaxJsonBytes = 4 * 1024 * 1024;
    private const int MaxArtifactBytes = 16 * 1024 * 1024;
    private static readonly Regex SafeId = new("\\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\\z", RegexOptions.CultureInvariant);
    private readonly HttpClient _http;

    public static ProofRunWorkerClient FromConfiguration(IConfiguration config)
        => new(config["PROOFRUN_WORKER_URL"], config["PROOFRUN_WORKER_TOKEN"]);

    public ProofRunWorkerClient(string? workerUrl, string? workerToken, HttpMessageHandler? handler = null)
    {
        if (!Uri.TryCreate(workerUrl, UriKind.Absolute, out var uri) ||
            uri.Scheme is not ("http" or "https") || uri.UserInfo.Length != 0 ||
            uri.Query.Length != 0 || uri.Fragment.Length != 0 || uri.AbsolutePath != "/")
            throw new ProofRunBridgeException("Configure the worker origin in server-side PROOFRUN_WORKER_URL.");
        if (uri.Scheme == "http" && !uri.IsLoopback && uri.Host != "host.docker.internal")
            throw new ProofRunBridgeException("Use HTTPS for a remote worker; HTTP is allowed only for the local development host.");
        if (string.IsNullOrWhiteSpace(workerToken) || workerToken.Contains('\r') || workerToken.Contains('\n'))
            throw new ProofRunBridgeException("Configure server-side PROOFRUN_WORKER_TOKEN before running verification.");
        // Redirects are disabled to prevent forwarding the bearer credential to another origin.
        _http = new HttpClient(handler ?? new HttpClientHandler { AllowAutoRedirect = false })
        {
            BaseAddress = uri,
            Timeout = TimeSpan.FromSeconds(30)
        };
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", workerToken);
    }

    public static string JobKey(string workspaceId, string resourceId)
    {
        if (string.IsNullOrWhiteSpace(workspaceId) || string.IsNullOrWhiteSpace(resourceId))
            throw new ProofRunBridgeException("A persisted workspace and resource ID are required before dispatch.");
        var identity = JsonSerializer.Serialize(new[] { workspaceId, resourceId });
        return "duplo-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant();
    }

    public static string ResearchKey(string workspaceId, string resourceId, string clientNonce)
    {
        _ = JobKey(workspaceId, resourceId);
        if (!Guid.TryParseExact(clientNonce, "D", out var nonce))
            throw new ArgumentException("A valid research request nonce is required.");
        var identity = JsonSerializer.Serialize(new[] { workspaceId, resourceId, nonce.ToString("D") });
        return "duplo-research-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant();
    }

    public async Task<JsonObject> ResearchAsync(string workspaceId, string resourceId,
        ProofRunResearchRequest input, CancellationToken ct)
    {
        var requestId = ResearchKey(workspaceId, resourceId, input.ClientNonce);
        var domain = (input.Domain ?? "").Trim().ToLowerInvariant();
        if (domain.StartsWith("www.", StringComparison.Ordinal)) domain = domain[4..];
        if (domain.Length > 253 || !Regex.IsMatch(domain, @"\A(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\z", RegexOptions.CultureInvariant) ||
            Regex.IsMatch(domain, @"\.(?:local|localhost|internal|invalid|test|example)\z", RegexOptions.CultureInvariant))
            throw new ArgumentException("Enter a public website domain without a URL, path or port.");
        if (!Regex.IsMatch(input.Month ?? "", @"\A\d{4}-(?:0[1-9]|1[0-2])\z", RegexOptions.CultureInvariant) ||
            !DateOnly.TryParseExact(input.Month + "-01", "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var start) ||
            start.Year < 2000 || start >= new DateOnly(DateTime.UtcNow.Year, DateTime.UtcNow.Month, 1))
            throw new ArgumentException("Choose a completed month from January 2000 onward.");
        var body = new JsonObject { ["request_id"] = requestId, ["domain"] = domain, ["month"] = input.Month };
        var report = await JsonAsync(HttpMethod.Post, "v1/customer-research", body, ct);
        return ResearchForBrowser(report, requestId, domain, start);
    }

    private static JsonObject ResearchForBrowser(JsonObject report, string requestId, string domain, DateOnly start)
    {
        const string invalid = "The worker returned an invalid or differently bound customer-research report.";
        var end = start.AddMonths(1).AddDays(-1);
        var status = Text(report, "status");
        if (Text(report, "schema_version") != "proofrun.research.v1" || Text(report, "provider") != "similarweb" ||
            Text(report, "request_id") != requestId || Text(report, "domain") != domain ||
            status is not ("completed" or "unavailable" or "no_data") || report["period"] is not JsonObject period ||
            Text(period, "start_date") != start.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) ||
            Text(period, "end_date") != end.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) ||
            !DateTimeOffset.TryParse(Text(report, "observed_at"), CultureInfo.InvariantCulture, DateTimeStyles.None, out _) ||
            report["metrics"] is not JsonArray metrics || report["sources"] is not JsonArray sources ||
            report["limitations"] is not JsonArray limitations || sources.Count > 10 || limitations.Count > 20)
            throw new ProofRunBridgeException(invalid);
        RequireSafeId(Text(report, "research_id"));
        var clean = new JsonObject();
        foreach (var field in new[] { "schema_version", "research_id", "request_id", "status", "domain", "observed_at", "provider" })
            clean[field] = Text(report, field);
        clean["period"] = new JsonObject { ["start_date"] = Text(period, "start_date"), ["end_date"] = Text(period, "end_date") };
        var cleanMetrics = new JsonArray();
        if (metrics.Count != (status == "completed" ? 1 : 0)) throw new ProofRunBridgeException(invalid);
        foreach (var item in metrics)
        {
            if (item is not JsonObject metric || Text(metric, "name") != "estimated_visits" || Text(metric, "unit") != "visits" ||
                metric["value"] is not JsonValue value || !value.TryGetValue<double>(out var number) || !double.IsFinite(number) || number < 0)
                throw new ProofRunBridgeException(invalid);
            cleanMetrics.Add(new JsonObject { ["name"] = "estimated_visits", ["value"] = number, ["unit"] = "visits" });
        }
        clean["metrics"] = cleanMetrics;
        var cleanSources = new JsonArray();
        foreach (var item in sources)
        {
            if (item is not JsonObject source || !BoundedText(source, "title", 256) || !BoundedText(source, "url", 2048) ||
                !Uri.TryCreate(Text(source, "url"), UriKind.Absolute, out var uri) || uri.Scheme != "https" || uri.UserInfo.Length != 0 ||
                !(uri.Host == "similarweb.com" || uri.Host.EndsWith(".similarweb.com", StringComparison.Ordinal)))
                throw new ProofRunBridgeException(invalid);
            cleanSources.Add(new JsonObject { ["title"] = Text(source, "title"), ["url"] = uri.AbsoluteUri });
        }
        clean["sources"] = cleanSources;
        var cleanLimits = new JsonArray();
        foreach (var item in limitations)
        {
            if (item is not JsonValue value || !value.TryGetValue<string>(out var text) || text.Length > 2048)
                throw new ProofRunBridgeException(invalid);
            cleanLimits.Add(text);
        }
        clean["limitations"] = cleanLimits;
        if (report["error"] is not null)
        {
            if (report["error"] is not JsonObject error || !BoundedText(error, "code", 128) || !BoundedText(error, "message", 2048))
                throw new ProofRunBridgeException(invalid);
            clean["error"] = new JsonObject { ["code"] = Text(error, "code"), ["message"] = Text(error, "message") };
        }
        // Only documented data fields reach the browser; no upstream URLs, headers or arbitrary provider payloads.
        return clean;
    }

    private static bool BoundedText(JsonObject value, string key, int max)
        => Text(value, key) is { Length: > 0 } text && text.Length <= max;

    public async Task<JsonObject> GetSubmissionAsync(string workspaceId, string resourceId, bool enableRepair, CancellationToken ct)
    {
        var registered = await JsonAsync(HttpMethod.Get, $"v1/cases/{CaseId}", null, ct);
        if (Text(registered, "schema_version") != "proofrun.v1" ||
            Text(registered, "case_id") != CaseId || registered["submission"] is not JsonObject original)
            throw new ProofRunBridgeException("The worker returned an unsupported registered-case contract.");
        var submission = original.DeepClone().AsObject();
        if (Text(submission, "schema_version") != "proofrun.v1" || Text(submission, "case_id") != CaseId)
            throw new ProofRunBridgeException("The registered case contains inconsistent submission bindings.");
        submission["job_key"] = JobKey(workspaceId, resourceId);
        // Repair is explicit, defaults off in the UI, and never substitutes a prepared candidate.
        submission["repair"] = new JsonObject { ["enabled"] = enableRepair, ["max_attempts"] = 2 };
        return submission;
    }

    public async Task<JsonObject> SubmitAsync(JsonObject submission, CancellationToken ct)
    {
        var run = ValidateRun(await JsonAsync(HttpMethod.Post, "v1/runs", submission, ct));
        RequireJobKey(run, Text(submission, "job_key"));
        return run;
    }

    public async Task<JsonObject> GetRunAsync(string runId, CancellationToken ct)
    {
        RequireSafeId(runId);
        var run = ValidateRun(await JsonAsync(HttpMethod.Get, $"v1/runs/{Uri.EscapeDataString(runId)}", null, ct));
        if (Text(run, "run_id") != runId)
            throw new ProofRunBridgeException("The worker returned a different run identity.");
        return run;
    }

    public static void RequireJobKey(JsonObject run, string expected)
    {
        if (string.IsNullOrEmpty(expected) || Text(run, "job_key") != expected)
            throw new ProofRunBridgeException("The returned run is not bound to this DuploCloud resource.");
    }

    public async Task<byte[]> GetArtifactAsync(JsonObject run, string artifactId, CancellationToken ct)
    {
        RequireSafeId(artifactId);
        var artifact = (run["artifacts"] as JsonArray)?.OfType<JsonObject>()
            .SingleOrDefault(a => Text(a, "id") == artifactId);
        if (artifact is null) throw new FileNotFoundException("Artifact does not belong to this run.");
        var runId = Text(run, "run_id");
        RequireSafeId(runId);
        // Never follow an upstream href: construct a known endpoint from two constrained IDs.
        using var request = new HttpRequestMessage(HttpMethod.Get,
            $"v1/runs/{Uri.EscapeDataString(runId)}/artifacts/{Uri.EscapeDataString(artifactId)}");
        var bytes = await SendAsync(request, MaxArtifactBytes, ct);
        var actualHash = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
        if (Text(artifact, "sha256") != actualHash ||
            artifact["size_bytes"] is not JsonValue size || !size.TryGetValue<long>(out var length) || length != bytes.LongLength)
            throw new ProofRunBridgeException("Artifact integrity failed: downloaded bytes do not match the run's hash and size.");
        return bytes;
    }

    public static JsonObject ForBrowser(JsonObject run)
    {
        var copy = run.DeepClone().AsObject();
        if (copy["artifacts"] is JsonArray artifacts)
            foreach (var artifact in artifacts.OfType<JsonObject>()) artifact.Remove("href");
        if (copy["coordination"] is JsonObject coordination)
        {
            if (Text(coordination, "provider") != "band" || Text(coordination, "mode") is not ("live" or "mock") ||
                Text(coordination, "status") is not ("passed" or "blocked" or "unavailable" or "waiting"))
                throw new ProofRunBridgeException("The worker returned unsupported repair-coordination metadata.");
            var clean = new JsonObject();
            foreach (var field in new[] { "provider", "mode", "status", "room_id", "handoff_id", "proposer_agent_id", "verifier_agent_id",
                "candidate_sha256", "contract_sha256", "evidence_sha256", "request_message_id", "result_message_id" })
            {
                if (coordination[field] is null) continue;
                if (!BoundedText(coordination, field, 256)) throw new ProofRunBridgeException("The worker returned invalid repair-coordination metadata.");
                clean[field] = Text(coordination, field);
            }
            copy["coordination"] = clean;
        }
        else copy.Remove("coordination");
        return copy;
    }

    private async Task<JsonObject> JsonAsync(HttpMethod method, string path, JsonObject? body, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(method, path);
        if (body is not null) request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        var bytes = await SendAsync(request, MaxJsonBytes, ct);
        try { return JsonNode.Parse(bytes)?.AsObject() ?? throw new JsonException(); }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException)
        { throw new ProofRunBridgeException("The worker returned invalid JSON; no verification verdict was inferred."); }
    }

    private async Task<byte[]> SendAsync(HttpRequestMessage request, int maxBytes, CancellationToken ct)
    {
        try
        {
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
            if (!response.IsSuccessStatusCode)
                throw new ProofRunBridgeException(response.StatusCode switch
                {
                    HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden => "The worker rejected server authentication. Check the backend token.",
                    HttpStatusCode.Conflict => "The worker rejected the job: it is busy or this job key has different input bindings. Wait for the active run or create a fresh verification.",
                    HttpStatusCode.NotFound => "The worker run, case, or artifact was not found.",
                    _ => $"The worker request failed (HTTP {(int)response.StatusCode}); no verdict was inferred."
                });
            if (response.Content.Headers.ContentLength > maxBytes)
                throw new ProofRunBridgeException("The worker response exceeded the bridge size limit.");
            await using var input = await response.Content.ReadAsStreamAsync(ct);
            using var output = new MemoryStream();
            var buffer = new byte[8192];
            int read;
            while ((read = await input.ReadAsync(buffer, ct)) > 0)
            {
                if (output.Length + read > maxBytes)
                    throw new ProofRunBridgeException("The worker response exceeded the bridge size limit.");
                output.Write(buffer, 0, read);
            }
            return output.ToArray();
        }
        catch (HttpRequestException)
        { throw new ProofRunBridgeException("The worker is unreachable from the DuploCloud backend. Check its server URL and network route."); }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        { throw new ProofRunBridgeException("The worker HTTP request timed out. This does not establish the run's final status."); }
    }

    private static JsonObject ValidateRun(JsonObject run)
    {
        if (Text(run, "schema_version") != "proofrun.v1" ||
            Text(run, "execution_status") is not ("queued" or "running" or "completed" or "setup_failed" or "timed_out" or "interrupted") ||
            Text(run, "finding_status") is not ("not_tested" or "regression_reproduced" or "no_difference_observed" or "inconclusive") ||
            Text(run, "repair_status") is not ("not_requested" or "pending" or "proposed" or "verified" or "rejected" or "unavailable"))
            throw new ProofRunBridgeException("The worker returned an unsupported proofrun.v1 status contract.");
        RequireSafeId(Text(run, "run_id"));
        if (run["artifacts"] is not JsonArray artifacts ||
            artifacts.Any(a => a is not JsonObject) ||
            artifacts.OfType<JsonObject>().Select(a => Text(a, "id")).Distinct().Count() != artifacts.Count)
            throw new ProofRunBridgeException("The worker returned an invalid artifact manifest.");
        foreach (var artifact in artifacts.OfType<JsonObject>()) RequireSafeId(Text(artifact, "id"));
        return run;
    }

    private static string Text(JsonObject obj, string name)
        => obj[name] is JsonValue value && value.TryGetValue<string>(out var text) ? text : "";

    private static void RequireSafeId(string id)
    {
        if (!SafeId.IsMatch(id) || id is "." or "..")
            throw new ProofRunBridgeException("The worker returned or received an invalid resource identifier.");
    }

    public void Dispose() => _http.Dispose();
}
