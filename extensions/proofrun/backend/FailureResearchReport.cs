using System.Net;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Text;
using System.Text.Json;
using System.Security.Cryptography;

namespace Duplo.Extension.ProofRun;

// Shared browser allowlist for advisory search reports. No provider payloads or HTML are rendered.
public static class FailureResearchReport
{
    private static string Hash(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
    public static string ResearchId(string requestId) => "failure-research-" + Hash(requestId);
    public static string ReleaseRequestId(string scope, string runId, string nonce)
        => "release-research-" + Hash(JsonSerializer.Serialize(new[] { scope, runId, nonce }));
    public static bool IsId(string value) => Regex.IsMatch(value, @"\Afailure-research-[a-f0-9]{64}\z");
    public static string Text(JsonObject value, string name)
        => value[name] is JsonValue item && item.TryGetValue<string>(out var text) ? text : "";
    private static bool Bounded(JsonObject value, string name, int max, bool empty = false)
        => value[name] is JsonValue item && item.TryGetValue<string>(out var text)
            && text.Length <= max && (empty || text.Length > 0);
    private static JsonObject Only(JsonObject obj, params string[] keys)
    {
        var result = new JsonObject();
        foreach (var key in keys) if (obj[key] is { } value) result[key] = value.DeepClone();
        return result;
    }

    public static string Query(string? query)
    {
        var value = (query ?? "").Trim();
        if (value.Length is < 1 or > 1200 || value.Any(c => c < 32 && c != '\n' && c != '\t'))
            throw new ArgumentException("Enter a search query of 1–1200 characters using the package, versions and error.");
        return value;
    }

    public static JsonObject Validate(JsonObject report, string kind, string runId, string findingId,
        string scope, string? query = null, string? expectedRequestId = null)
    {
        const string invalid = "The worker returned invalid or differently bound failure research.";
        void Require(bool value) { if (!value) throw new ArgumentException(invalid); }
        var status = Text(report, "status");
        Require(Text(report, "schema_version") == "proofrun.failure-research.v1" && IsId(Text(report, "research_id"))
            && Bounded(report, "request_id", 128) && status is "completed" or "no_sources" or "unavailable"
            && Bounded(report, "query", 1200) && (query is null || Text(report, "query") == query)
            && Bounded(report, "summary", 6000, true)
            && DateTimeOffset.TryParse(Text(report, "observed_at"), out _));
        Require(Text(report, "research_id") == ResearchId(Text(report, "request_id"))
            && (expectedRequestId is null || Text(report, "request_id") == expectedRequestId));
        if (report["context"] is not JsonObject context) throw new ArgumentException(invalid);
        Require(Text(context, "kind") == kind && Text(context, "run_id") == runId
            && Text(context, "finding_id") == findingId && Text(context, "scope") == scope
            && Regex.IsMatch(Text(context, "evidence_sha256"), @"\A[a-f0-9]{64}\z"));
        var clean = Only(report, "schema_version", "research_id", "request_id", "query", "status", "summary", "observed_at");
        clean["context"] = Only(context, "kind", "run_id", "finding_id", "scope", "evidence_sha256",
            "revision", "source_sha256", "contract_sha256", "job_key", "target_id", "baseline_revision", "candidate_revision");
        if (report["sources"] is not JsonArray sources || sources.Count > 5
            || report["suggested_fixes"] is not JsonArray fixes || fixes.Count > 5
            || report["limitations"] is not JsonArray limits || limits.Count > 20) throw new ArgumentException(invalid);
        Require(status == "completed" ? sources.Count > 0 : sources.Count == 0 && fixes.Count == 0);
        var sourceIds = new HashSet<string>();
        var cleanSources = new JsonArray();
        foreach (var item in sources)
        {
            if (item is not JsonObject source) throw new ArgumentException(invalid);
            Require(Bounded(source, "id", 32) && sourceIds.Add(Text(source, "id")) && Bounded(source, "title", 512)
                && Bounded(source, "excerpt", 2000, true) && Bounded(source, "url", 2048));
            if (!Uri.TryCreate(Text(source, "url"), UriKind.Absolute, out var uri) || uri.Scheme != "https"
                || uri.UserInfo.Length > 0 || !uri.IsDefaultPort || uri.IsLoopback || IPAddress.TryParse(uri.Host, out _)
                || !uri.Host.Contains('.') || Regex.IsMatch(uri.Host, @"\.(local|internal|localhost|invalid|test)$"))
                throw new ArgumentException(invalid);
            cleanSources.Add(Only(source, "id", "title", "url", "excerpt"));
        }
        var cleanFixes = new JsonArray();
        foreach (var item in fixes)
        {
            if (item is not JsonObject fix || !Bounded(fix, "description", 2000)
                || fix["source_ids"] is not JsonArray ids || ids.Count is < 1 or > 5
                || ids.Any(id => id is not JsonValue v || !v.TryGetValue<string>(out var text) || !sourceIds.Contains(text)))
                throw new ArgumentException(invalid);
            cleanFixes.Add(Only(fix, "description", "source_ids"));
        }
        Require(limits.All(item => item is JsonValue v && v.TryGetValue<string>(out var text) && text.Length <= 2000));
        clean["sources"] = cleanSources; clean["suggested_fixes"] = cleanFixes; clean["limitations"] = limits.DeepClone();
        if (report["error"] is JsonObject error)
        {
            Require(Bounded(error, "code", 128) && Bounded(error, "message", 2000));
            clean["error"] = Only(error, "code", "message");
        }
        if (report["provenance"] is JsonObject provenance)
        {
            var safe = Only(provenance, "mode", "gateway", "search_engine", "model", "requested_model", "operation_id", "request_sha256", "response_sha256");
            Require(safe.All(pair => pair.Value is JsonValue v && v.TryGetValue<string>(out var text) && text.Length <= 256));
            clean["provenance"] = safe;
        }
        return clean;
    }
}
