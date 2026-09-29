using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Duplo.Extension.ProofRun;

if (args.Contains("--live"))
{
    using var client = new ProofRunWorkerClient(Environment.GetEnvironmentVariable("PROOFRUN_WORKER_URL"),
        Environment.GetEnvironmentVariable("PROOFRUN_WORKER_TOKEN"));
    using var deadline = new CancellationTokenSource(TimeSpan.FromMinutes(5));
    var request = await client.GetSubmissionAsync("standalone-client-check", Guid.NewGuid().ToString("N"),
        args.Contains("--repair"), deadline.Token);
    var run = await client.SubmitAsync(request, deadline.Token);
    var previous = "";
    while (true)
    {
        var state = run["execution_status"]!.GetValue<string>();
        if (state != previous)
            Console.WriteLine($"Actual worker run {run["run_id"]}: execution={state}, finding={run["finding_status"]}, repair={run["repair_status"]}");
        previous = state;
        if (state is not ("queued" or "running")) break;
        await Task.Delay(1000, deadline.Token);
        run = await client.GetRunAsync(run["run_id"]!.GetValue<string>(), deadline.Token);
        ProofRunWorkerClient.RequireJobKey(run, request["job_key"]!.GetValue<string>());
    }
    if (run["execution_status"]!.GetValue<string>() != "completed")
        throw new Exception("Live worker did not complete; inspect actual worker evidence.");
    var artifacts = run["artifacts"]!.AsArray();
    if (artifacts.Count == 0 || run["cases"]!.AsArray().Count == 0)
        throw new Exception("Live worker returned no measured cases or artifacts.");
    foreach (var entry in artifacts.OfType<JsonObject>())
    {
        var id = entry["id"]!.GetValue<string>();
        var bytes = await client.GetArtifactAsync(run, id, deadline.Token);
        Console.WriteLine($"Verified artifact {id}: {bytes.Length} bytes, sha256={entry["sha256"]}");
    }
    Console.WriteLine($"PASS: actual HTTP adapter round trip; {run["cases"]!.AsArray().Count} case records, {artifacts.Count} hash-checked artifacts. Portal/SDK lifecycle remains untested.");
    return;
}

// These are HTTP boundary checks with a fake transport, not portal or runner execution evidence.
// No SDK stubs are used: this compiles the real standalone HTTP client against .NET 8.
var count = 0;
void Check(bool condition, string description)
{
    if (!condition) throw new Exception($"Failed: {description}");
    count++;
}
async Task Reject(Func<Task> action, string description)
{
    try { await action(); }
    catch (ProofRunBridgeException) { count++; return; }
    throw new Exception($"Did not reject: {description}");
}
JsonObject Run(string key, JsonArray? artifacts = null) => new()
{
    ["schema_version"] = "proofrun.v1", ["run_id"] = "run-1", ["job_key"] = key,
    ["execution_status"] = "completed", ["finding_status"] = "regression_reproduced",
    ["repair_status"] = "not_requested", ["artifacts"] = artifacts ?? new JsonArray(),
    ["cases"] = new JsonArray()
};
HttpResponseMessage Json(JsonObject body) => new(HttpStatusCode.OK)
    { Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json") };
var key = ProofRunWorkerClient.JobKey("workspace-1", "resource-1");
Check(key == ProofRunWorkerClient.JobKey("workspace-1", "resource-1"), "stable retry identity");
Check(key != ProofRunWorkerClient.JobKey("workspace-2", "resource-1"), "workspace identity is included");
Check(ProofRunWorkerClient.JobKey("a", "bc") != ProofRunWorkerClient.JobKey("ab", "c"), "identity boundaries are unambiguous");

foreach (var url in new[] { "file:///etc/passwd", "http://remote.example", "https://user:secret@worker.example", "https://worker.example/api", "https://worker.example?secret=x" })
    await Reject(() => { using var _ = new ProofRunWorkerClient(url, "test-token"); return Task.CompletedTask; }, "unsafe worker origin");

var registered = new JsonObject
{
    ["schema_version"] = "proofrun.v1", ["case_id"] = ProofRunWorkerClient.CaseId,
    ["submission"] = new JsonObject { ["schema_version"] = "proofrun.v1", ["case_id"] = ProofRunWorkerClient.CaseId }
};
var handler = new FakeHandler(request =>
{
    Check(request.Headers.Authorization?.Scheme == "Bearer" && request.Headers.Authorization.Parameter == "test-token", "server bearer authentication");
    return Json(registered);
});
using (var client = new ProofRunWorkerClient("http://host.docker.internal:8766", "test-token", handler))
{
    var submission = await client.GetSubmissionAsync("workspace-1", "resource-1", false, default);
    Check(submission["job_key"]!.GetValue<string>() == key, "resource-bound submission");
    Check(submission["repair"]!["enabled"]!.GetValue<bool>() == false, "comparison-only default");
    var repair = await client.GetSubmissionAsync("workspace-1", "resource-2", true, default);
    Check(repair["repair"]!["enabled"]!.GetValue<bool>(), "explicit repair opt-in");
    Check(registered["submission"]!["job_key"] is null, "registered template is not mutated");
}

var artifactBytes = Encoding.UTF8.GetBytes("{\"measured\":true}\n");
var artifact = new JsonObject
{
    ["id"] = "evidence.json", ["sha256"] = Convert.ToHexString(SHA256.HashData(artifactBytes)).ToLowerInvariant(),
    ["size_bytes"] = artifactBytes.LongLength, ["href"] = "https://untrusted.example/steal"
};
var result = Run(key, new JsonArray(artifact));
using (var client = new ProofRunWorkerClient("http://localhost:8766", "test-token", new FakeHandler(request =>
{
    if (request.RequestUri!.AbsolutePath.Contains("/artifacts/"))
    {
        Check(request.RequestUri.AbsoluteUri == "http://localhost:8766/v1/runs/run-1/artifacts/evidence.json", "artifact href is ignored");
        return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(artifactBytes) };
    }
    return Json(result);
})))
{
    var observed = await client.GetRunAsync("run-1", default);
    ProofRunWorkerClient.RequireJobKey(observed, key);
    Check(observed["finding_status"]!.GetValue<string>() == "regression_reproduced", "finding survives completed execution");
    Check((await client.GetArtifactAsync(observed, "evidence.json", default)).SequenceEqual(artifactBytes), "artifact hash and size match");
    Check(ProofRunWorkerClient.ForBrowser(observed)["artifacts"]![0]!["href"] is null, "upstream URLs removed for browser");
    Check(observed["artifacts"]![0]!["href"] is not null, "browser sanitization does not mutate evidence");
    await Reject(() => client.GetRunAsync("run-1\n", default), "newline in identifier");
    await Reject(() => client.GetRunAsync("../run-1", default), "path traversal");
    await Reject(() => { ProofRunWorkerClient.RequireJobKey(observed, "another-resource"); return Task.CompletedTask; }, "cross-resource evidence");
    try { await client.GetArtifactAsync(observed, "other.json", default); throw new Exception("Unlisted artifact allowed"); }
    catch (FileNotFoundException) { count++; }
    observed["artifacts"]![0]!["sha256"] = new string('0', 64);
    await Reject(() => client.GetArtifactAsync(observed, "evidence.json", default), "tampered artifact hash");
}

var invalidStatus = Run(key);
invalidStatus["execution_status"] = "passed";
using (var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => Json(invalidStatus))))
    await Reject(() => client.GetRunAsync("run-1", default), "unknown status is not inferred as success");

var duplicates = Run(key, new JsonArray(artifact.DeepClone(), artifact.DeepClone()));
using (var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => Json(duplicates))))
    await Reject(() => client.GetRunAsync("run-1", default), "duplicate artifact identifiers");

using (var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => new(HttpStatusCode.Unauthorized)
    { Content = new StringContent("SECRET MUST NEVER APPEAR") })))
{
    try { await client.GetRunAsync("run-1", default); throw new Exception("Expected authentication failure"); }
    catch (ProofRunBridgeException ex) { Check(!ex.Message.Contains("SECRET"), "upstream response does not leak into errors"); }
}

using (var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => Json(Run("wrong-job-key")))))
    await Reject(() => client.SubmitAsync(new JsonObject { ["job_key"] = key }, default), "submission response binding mismatch");

var nonce = Guid.NewGuid().ToString("D");
var researchKey = ProofRunWorkerClient.ResearchKey("workspace-1", "resource-1", nonce);
Check(researchKey == ProofRunWorkerClient.ResearchKey("workspace-1", "resource-1", nonce), "stable research retry identity");
Check(researchKey != ProofRunWorkerClient.ResearchKey("workspace-2", "resource-1", nonce), "research bound to workspace");
Check(researchKey != ProofRunWorkerClient.ResearchKey("workspace-1", "resource-2", nonce), "research bound to resource");
JsonObject Research(string status = "completed") => new()
{
    ["schema_version"] = "proofrun.research.v1", ["request_id"] = researchKey, ["research_id"] = "research-test",
    ["provider"] = "similarweb", ["domain"] = "similarweb.com", ["status"] = status,
    ["period"] = new JsonObject { ["start_date"] = "2020-02-01", ["end_date"] = "2020-02-29" },
    ["observed_at"] = "2026-09-29T22:00:00Z",
    ["metrics"] = status == "completed" ? new JsonArray(new JsonObject { ["name"] = "estimated_visits", ["value"] = 1200, ["unit"] = "visits" }) : new JsonArray(),
    ["sources"] = new JsonArray(new JsonObject { ["title"] = "Traffic API", ["url"] = "https://developers.similarweb.com/reference/total-traffic-and-engagement" }),
    ["limitations"] = new JsonArray("Worldwide estimates; not customer requirements."),
    ["error"] = status == "unavailable" ? new JsonObject { ["code"] = "not_configured", ["message"] = "Similarweb is not configured." } : null,
    ["raw_provider_headers"] = "SECRET MUST NEVER APPEAR"
};
var researchInput = new ProofRunResearchRequest { ClientNonce = nonce, Domain = " WWW.Similarweb.com ", Month = "2020-02" };
using (var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(request =>
{
    Check(request.Method == HttpMethod.Post && request.RequestUri!.AbsoluteUri == "https://worker.example/v1/customer-research", "research uses fixed worker endpoint");
    Check(request.Headers.Authorization?.Parameter == "test-token", "research uses server bearer token");
    var body = JsonNode.Parse(request.Content!.ReadAsStringAsync().GetAwaiter().GetResult())!.AsObject();
    Check(body.Count == 3 && body["request_id"]!.GetValue<string>() == researchKey && body["domain"]!.GetValue<string>() == "similarweb.com" && body["month"]!.GetValue<string>() == "2020-02", "research request normalized and bound, no browser-provided identity");
    return Json(Research());
})))
{
    var observed = await client.ResearchAsync("workspace-1", "resource-1", researchInput, default);
    Check(observed["status"]!.GetValue<string>() == "completed" && observed["metrics"]!.AsArray().Count == 1, "research retains actual metric");
    Check(!observed.ToJsonString().Contains("SECRET") && observed["raw_provider_headers"] is null, "research browser response is allowlisted");
}
foreach (var status in new[] { "no_data", "unavailable" })
{
    using var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => Json(Research(status))));
    var observed = await client.ResearchAsync("workspace-1", "resource-1", researchInput, default);
    Check(observed["status"]!.GetValue<string>() == status && observed["metrics"]!.AsArray().Count == 0, "research absence remains explicit");
}
foreach (var field in new[] { "request_id", "domain", "period" })
{
    var wrong = Research();
    if (field == "period") wrong["period"]!["end_date"] = "2020-02-28";
    else wrong[field] = "different-resource";
    using var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => Json(wrong)));
    await Reject(() => client.ResearchAsync("workspace-1", "resource-1", researchInput, default), "research response binding: " + field);
}
foreach (var mutation in new Action<JsonObject>[] {
    report => report["sources"]![0]!["url"] = "javascript:alert(1)",
    report => report["sources"]![0]!["url"] = "https://similarweb.com.attacker.example/",
    report => report["metrics"]![0]!["value"] = -1,
    report => report["status"] = "passed",
    report => report["metrics"] = new JsonArray()
})
{
    var invalid = Research();
    mutation(invalid);
    using var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => Json(invalid)));
    await Reject(() => client.ResearchAsync("workspace-1", "resource-1", researchInput, default), "invalid research data cannot become customer context");
}
foreach (var input in new[] {
    new ProofRunResearchRequest { ClientNonce = nonce, Domain = "https://similarweb.com", Month = "2020-02" },
    new ProofRunResearchRequest { ClientNonce = nonce, Domain = "127.0.0.1", Month = "2020-02" },
    new ProofRunResearchRequest { ClientNonce = nonce, Domain = "similarweb.com", Month = "9999-12" },
    new ProofRunResearchRequest { ClientNonce = "../workspace-2", Domain = "similarweb.com", Month = "2020-02" }
})
{
    using var client = new ProofRunWorkerClient("https://worker.example", "test-token", new FakeHandler(_ => throw new Exception("Invalid research triggered HTTP")));
    try { await client.ResearchAsync("workspace-1", "resource-1", input, default); throw new Exception("Invalid research accepted"); }
    catch (ArgumentException) { count++; }
}
var coordinated = Run(key);
coordinated["coordination"] = new JsonObject { ["provider"] = "band", ["mode"] = "mock", ["status"] = "unavailable", ["room_id"] = "room-1", ["raw_token"] = "SECRET" };
var browserCoordination = ProofRunWorkerClient.ForBrowser(coordinated)["coordination"]!.AsObject();
Check(browserCoordination["mode"]!.GetValue<string>() == "mock" && browserCoordination["status"]!.GetValue<string>() == "unavailable", "BAND retains actual mode and handoff outcome");
Check(browserCoordination["raw_token"] is null && coordinated["coordination"]!["raw_token"] is not null, "BAND metadata allowlisted without mutating evidence");

Console.WriteLine($"PASS: {count} standalone HTTP-client assertions (fake transport; no portal or runner verdict claimed).");

sealed class FakeHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        => Task.FromResult(respond(request));
}
