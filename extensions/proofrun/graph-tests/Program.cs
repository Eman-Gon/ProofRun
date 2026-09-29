using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using Duplo.Extension.ProofRun;

// Real HTTP client boundary code with injected transport. No database, provider or portal verdict is claimed.
const string scope = "workspace-one";
const string resource = "resource-one";
const string token = "test-only-graph-token-01234567890123456789";
const string fixtureId = "run-one";
var releaseId = ProofRunReleaseClient.EventRunId(scope, "graph-check");
var count = 0;
void Check(bool condition, string description)
{
    if (!condition) throw new Exception("Failed: " + description);
    count++;
}
async Task Reject(Func<Task> action, string description)
{
    try { await action(); }
    catch (Exception error) when (error is ArgumentException or ProofRunBridgeException or ProofRunReleaseException)
    { Check(!error.Message.Contains("SECRET"), "upstream secrets never reach errors"); return; }
    throw new Exception("Did not reject: " + description);
}
JsonObject FixtureRun(string workspace = scope, string resourceId = resource) => new()
{
    ["schema_version"] = "proofrun.v1", ["run_id"] = fixtureId,
    ["job_key"] = ProofRunWorkerClient.JobKey(workspace, resourceId),
    ["execution_status"] = "completed", ["finding_status"] = "regression_reproduced",
    ["repair_status"] = "not_requested", ["artifacts"] = new JsonArray()
};
JsonObject ReleaseRun() => new()
{
    ["schema_version"] = "proofrun.release.v1", ["id"] = releaseId, ["status"] = "running",
    ["created_at"] = "2026-09-29T12:00:00Z", ["updated_at"] = "2026-09-29T12:00:00Z",
    ["request"] = new JsonObject
    {
        ["target_id"] = "customer-service", ["baseline_revision"] = new string('a', 40),
        ["candidate_revision"] = new string('b', 40), ["benefit"] = "Graph check", ["budget_seconds"] = 300,
        ["repair"] = false, ["event_id"] = "graph-check"
    }
};
JsonObject Report(string kind, string runId) => new()
{
    ["schema_version"] = "proofrun.evidence-graph.v1", ["provider"] = "neo4j", ["status"] = "ready",
    ["kind"] = kind, ["run_id"] = runId, ["graph_id"] = new string('c', 64),
    ["observed_at"] = "2026-09-29T12:00:00Z", ["message"] = "Measured fix evidence.",
    ["nodes"] = new JsonArray(
        new JsonObject { ["id"] = "run:one", ["kind"] = "run", ["label"] = "Run", ["secret"] = "SECRET" },
        new JsonObject { ["id"] = "repair:one", ["kind"] = "repair", ["label"] = "Fix", ["status"] = "passed",
            ["detail"] = "All declared checks passed.", ["finding_id"] = "missing-nickname", ["url"] = "https://SECRET.invalid" }),
    ["edges"] = new JsonArray(new JsonObject
        { ["id"] = "edge:one", ["source"] = "run:one", ["target"] = "repair:one", ["label"] = "verified", ["credentials"] = "SECRET" }),
    ["connection"] = new JsonObject { ["password"] = "SECRET" }, ["href"] = "https://SECRET.invalid"
};
HttpResponseMessage Json(JsonObject value) => Raw(value.ToJsonString());
HttpResponseMessage Raw(string value) => new(HttpStatusCode.OK)
    { Content = new StringContent(value, Encoding.UTF8, "application/json") };

async Task<JsonObject> Fetch(string kind, Func<HttpRequestMessage, HttpResponseMessage> graph,
    string workspace = scope, JsonObject? run = null)
{
    var runId = kind == "fixture" ? fixtureId : releaseId;
    var prefix = kind == "fixture" ? "runs" : "release-runs";
    var stage = 0;
    using var transport = new Handler(request =>
    {
        Check(request.Method == HttpMethod.Get, "graph retrieval uses GET");
        Check(request.Headers.Authorization?.Scheme == "Bearer" && request.Headers.Authorization.Parameter == token,
            "server bearer credential is preserved");
        Check(request.RequestUri!.Host == "localhost", "only configured worker receives requests");
        if (request.RequestUri.AbsolutePath == $"/v1/{prefix}/{runId}")
        {
            Check(stage++ == 0, "ownership is established first");
            if (kind == "release") Check(request.Headers.GetValues("X-ProofRun-Scope").Single() == workspace, "release ownership read is scoped");
            return Json(run ?? (kind == "fixture" ? FixtureRun() : ReleaseRun()));
        }
        Check(stage++ == 1, "graph follows authoritative ownership read");
        Check(request.RequestUri.AbsolutePath == $"/v1/{prefix}/{runId}/graph", "graph uses fixed path from constrained run ID");
        Check(request.Headers.GetValues("X-ProofRun-Scope").Single() == workspace, "authorized workspace scope accompanies graph");
        return graph(request);
    });
    if (kind == "fixture")
    {
        using var client = new ProofRunWorkerClient("http://localhost:8766", token, transport);
        return await client.GraphAsync(workspace, resource, runId, default);
    }
    using var release = new ProofRunReleaseClient("http://localhost:8767", token, transport);
    return await release.GraphAsync(workspace, runId, default);
}

foreach (var kind in new[] { "fixture", "release" })
{
    var id = kind == "fixture" ? fixtureId : releaseId;
    var source = Report(kind, id);
    var clean = await Fetch(kind, _ => Json(source));
    Check(!clean.ToJsonString().Contains("SECRET"), "extra root/node/edge properties cannot expose secrets or external URLs");
    Check(source.ToJsonString().Contains("SECRET"), "allowlisting does not mutate worker evidence");
    Check(clean["nodes"]![1]!["detail"]!.GetValue<string>() == "All declared checks passed.", "documented graph fields remain visible");
    foreach (var status in new[] { "pending", "unavailable" })
    {
        var empty = Report(kind, id); empty["status"] = status; empty["graph_id"] = "";
        empty["nodes"] = new JsonArray(); empty["edges"] = new JsonArray();
        Check((await Fetch(kind, _ => Json(empty)))["status"]!.GetValue<string>() == status, "truthful empty graph state accepted");
        empty["graph_id"] = null;
        var missingGraph = await Fetch(kind, _ => Json(empty));
        Check(missingGraph.ContainsKey("graph_id") && missingGraph["graph_id"] is null, "unpersisted graph identity stays null");
    }
    var mutations = new Action<JsonObject>[]
    {
        r => r["schema_version"] = "unknown", r => r["provider"] = "fake", r => r["kind"] = "another-kind",
        r => r["run_id"] = "forged-run", r => r["status"] = "passed", r => r["graph_id"] = "",
        r => r["graph_id"] = null, r => r.Remove("graph_id"),
        r => r["graph_id"] = new string('G', 64), r => r["observed_at"] = "never", r => r["message"] = new string('x', 501),
        r => r["message"] = new JsonObject(), r => r["nodes"] = new JsonArray(), r => r["nodes"]![0] = "bad",
        r => r["nodes"]![0]!["id"] = "../other", r => r["nodes"]![0]!["kind"] = "credential",
        r => r["nodes"]![0]!["label"] = new string('x', 121), r => r["nodes"]![0]!["detail"] = new string('x', 501),
        r => r["nodes"]![0]!["detail"] = new JsonObject { ["password"] = "SECRET" },
        r => r["nodes"]![0]!["status"] = true, r => r["nodes"]![0]!["finding_id"] = new JsonArray(),
        r => r["nodes"]![0]!["label"] = "bad\0text", r => r["nodes"]!.AsArray().Add(r["nodes"]![0]!.DeepClone()),
        r => r["edges"]!.AsArray().Add(r["edges"]![0]!.DeepClone()), r => r["edges"]![0] = "bad",
        r => r["edges"]![0]!["source"] = "missing", r => r["edges"]![0]!["target"] = "missing",
        r => r["edges"]![0]!["label"] = new string('x', 121), r => r["edges"]![0]!["id"] = "https://SECRET.invalid",
        r => r["status"] = "pending", r => r["status"] = "unavailable",
        r => r["nodes"] = new JsonArray(Enumerable.Range(0, 81).Select(n => (JsonNode)new JsonObject
            { ["id"] = $"node-{n}", ["kind"] = "run", ["label"] = "Run" }).ToArray()),
        r => r["edges"] = new JsonArray(Enumerable.Range(0, 121).Select(n => (JsonNode)new JsonObject
            { ["id"] = $"edge-{n}", ["source"] = "run:one", ["target"] = "repair:one", ["label"] = "Verified" }).ToArray())
    };
    foreach (var mutate in mutations)
    {
        var changed = Report(kind, id); mutate(changed);
        await Reject(() => Fetch(kind, _ => Json(changed)), "malformed graph");
    }
    foreach (var raw in new[] { "[]", "not-json", "{\"status\":\"ready\",\"status\":\"unavailable\"}",
        source.ToJsonString().Replace("\"id\":\"run:one\"", "\"id\":\"run:one\",\"id\":\"duplicate\"") })
        await Reject(() => Fetch(kind, _ => Raw(raw)), "malformed or duplicate JSON");
    await Reject(() => Fetch(kind, _ => new(HttpStatusCode.InternalServerError) { Content = new StringContent("SECRET") }), "upstream error suppression");
    await Reject(() => Fetch(kind, _ => throw new Exception("Cross-scope request fetched graph"), workspace: "workspace-two"), "cross-scope ownership");
}
await Reject(() => Fetch("fixture", _ => throw new Exception("Cross-resource request fetched graph"),
    run: FixtureRun(resourceId: "other-resource")), "forged fixture resource run ID");
using (var fixture = new ProofRunWorkerClient("http://localhost:8766", token, new Handler(_ => throw new Exception("Invalid ID dispatched"))))
{
    await Reject(() => fixture.GraphAsync("workspace\r\nSECRET: true", resource, fixtureId, default), "invalid scope");
    await Reject(() => fixture.GraphAsync(scope, resource, "../other", default), "unsafe run ID");
}
using (var fixture = new ProofRunWorkerClient("http://localhost:8766", token,
    new Handler(_ => Raw("{\"SECRET\":1,\"SECRET\":2}"))))
    await Reject(() => fixture.GraphAsync(scope, resource, fixtureId, default), "malformed authoritative run error suppression");
using (var release = new ProofRunReleaseClient("http://localhost:8767", token, new Handler(_ => new(HttpStatusCode.NotFound) { Content = new StringContent("SECRET") })))
{
    try { await release.GraphAsync(scope, releaseId, default); throw new Exception("Unavailable ownership accepted"); }
    catch (ProofRunReleaseException error) { Check(error.StatusCode == 404 && !error.Message.Contains("SECRET"), "release ownership 404 preserved"); }
}
Console.WriteLine($"PASS: {count} evidence graph HTTP-client assertions (injected transport, no provider or portal authorization claim).");

sealed class Handler(Func<HttpRequestMessage, HttpResponseMessage> send) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        => Task.FromResult(send(request));
}
