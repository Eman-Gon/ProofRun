using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using Duplo.Extension.ProofRun;

// Injected HTTP transport only; no live provider or portal claims.
const string scope = "workspace-one";
const string query = "pydantic 2.8.2 Optional Field required";
var researchId = "failure-research-" + new string('a', 64);
var count = 0;
void Check(bool ok) { if (!ok) throw new Exception("Check failed"); count++; }
async Task Reject(Func<Task> action)
{
    try { await action(); }
    catch (Exception e) when (e is ArgumentException or ProofRunBridgeException or ProofRunReleaseException) { count++; return; }
    throw new Exception("Invalid research accepted");
}
JsonObject Report(string kind = "fixture", string runId = "run-1", string findingId = "regression") => new()
{
    ["schema_version"] = "proofrun.failure-research.v1", ["research_id"] = researchId,
    ["request_id"] = "request-1", ["query"] = query, ["status"] = "completed", ["summary"] = "Review the default value. [source-1]",
    ["observed_at"] = "2026-09-29T12:00:00Z", ["limitations"] = new JsonArray("Unverified research"),
    ["context"] = new JsonObject { ["kind"] = kind, ["run_id"] = runId, ["finding_id"] = findingId,
        ["scope"] = scope, ["evidence_sha256"] = new string('b', 64) },
    ["sources"] = new JsonArray(new JsonObject { ["id"] = "source-1", ["title"] = "Migration guide",
        ["url"] = "https://docs.pydantic.dev/latest/migration/", ["excerpt"] = "An excerpt" }),
    ["suggested_fixes"] = new JsonArray(new JsonObject { ["description"] = "Review the default value.", ["source_ids"] = new JsonArray("source-1") }),
    ["provenance"] = new JsonObject { ["mode"] = "injected", ["gateway"] = "openrouter", ["search_engine"] = "exa" },
    ["private_metadata"] = "must be omitted"
};
HttpResponseMessage Json(JsonObject value) => new(HttpStatusCode.OK)
    { Content = new StringContent(value.ToJsonString(), Encoding.UTF8, "application/json") };
var report = Report();
var clean = FailureResearchReport.Validate(report, "fixture", "run-1", "regression", scope, query);
Check(clean["private_metadata"] is null);
Check(clean["suggested_fixes"]!.AsArray().Count == 1);
foreach (var mutation in new Action<JsonObject>[] {
    r => r["context"]!["scope"] = "another-workspace",
    r => r["context"]!["run_id"] = "run-2",
    r => r["context"]!["finding_id"] = "other",
    r => r["sources"]![0]!["url"] = "javascript:alert(1)",
    r => r["sources"]![0]!["url"] = "https://127.0.0.1/private",
    r => r["sources"]![0]!["url"] = "https://user:secret@docs.pydantic.dev/",
    r => r["suggested_fixes"]![0]!["source_ids"] = new JsonArray("invented-source"),
    r => r["status"] = "no_sources",
    r => r["query"] = "different query"
}) {
    var changed = report.DeepClone().AsObject(); mutation(changed);
    await Reject(() => { FailureResearchReport.Validate(changed, "fixture", "run-1", "regression", scope, query); return Task.CompletedTask; });
}
var calls = 0;
var resourceKey = ProofRunWorkerClient.JobKey(scope, "resource-one");
using (var client = new ProofRunWorkerClient("http://localhost:8766", "test-token", new Handler(req => {
    calls++;
    if (req.Method == HttpMethod.Get) return Json(new JsonObject {
        ["schema_version"] = "proofrun.v1", ["run_id"] = "run-1", ["job_key"] = resourceKey,
        ["execution_status"] = "completed", ["finding_status"] = "regression_reproduced",
        ["repair_status"] = "not_requested", ["artifacts"] = new JsonArray()
    });
    Check(req.Headers.GetValues("X-ProofRun-Scope").Single() == scope);
    Check(req.RequestUri!.AbsolutePath == "/v1/runs/run-1/failure-research");
    return Json(report);
}))) {
    var value = await client.FailureResearchAsync(scope, "resource-one", "run-1",
        new() { ClientNonce = Guid.NewGuid().ToString(), Query = query }, default);
    Check(value["research_id"]!.GetValue<string>() == researchId);
    await Reject(() => client.FailureResearchAsync(scope, "another-resource", "run-1",
        new() { ClientNonce = Guid.NewGuid().ToString(), Query = query }, default));
    Check(calls == 3); // cross-resource request never reached the research provider route
}
var releaseId = "release-" + new string('a', 40);
using (var client = new ProofRunReleaseClient("http://localhost:8767", "test-release-token-01234567890123456789", new Handler(req => {
    Check(req.Headers.GetValues("X-ProofRun-Scope").Single() == scope);
    Check(req.RequestUri!.AbsolutePath == $"/v1/release-runs/{releaseId}/failure-research");
    return Json(Report("release", releaseId, "finding-1"));
}))) {
    var value = await client.FailureResearchAsync(scope, releaseId, new() {
        ["request_id"] = Guid.NewGuid().ToString(), ["query"] = query, ["finding_id"] = "finding-1"
    }, default);
    Check(value["context"]!["run_id"]!.GetValue<string>() == releaseId);
}
Console.WriteLine($"PASS: {count} failure-research gateway assertions (mock transport).");

sealed class Handler(Func<HttpRequestMessage, HttpResponseMessage> send) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        => Task.FromResult(send(request));
}
