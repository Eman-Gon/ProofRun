using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Duplo.Extension.ProofRun;

// Transport/policy checks against the real client. These are not SDK authorization or product-run evidence.
const string token = "test-only-release-worker-token-0123456789";
const string scope = "workspace-one";
const string origin = "http://localhost:8767";
var id = "release-" + new string('a', 40);
var count = 0;
void Check(bool condition, string description)
{
    if (!condition) throw new Exception("Failed: " + description);
    count++;
}
async Task Reject(Func<Task> action, string description, bool argument = false)
{
    try { await action(); }
    catch (ProofRunReleaseException) when (!argument) { count++; return; }
    catch (ArgumentException) when (argument) { count++; return; }
    throw new Exception("Did not reject: " + description);
}
JsonObject Input() => new()
{
    ["target_id"] = "customer-service", ["baseline_revision"] = new string('0', 40),
    ["candidate_revision"] = new string('1', 40), ["benefit"] = "Needed client workflow",
    ["budget_seconds"] = 300, ["repair"] = false
};
JsonObject Result(JsonObject request) => new()
{
    ["schema_version"] = "proofrun.release.v1", ["target_id"] = request["target_id"]!.DeepClone(),
    ["recommendation"] = "postpone", ["summary"] = "Insufficient evidence.",
    ["revisions"] = new JsonObject
    {
        ["baseline"] = request["baseline_revision"]!.DeepClone(), ["candidate"] = request["candidate_revision"]!.DeepClone()
    },
    ["coverage"] = new JsonObject { ["requirements_total"] = 1, ["requirements_exercised"] = 0 },
    ["findings"] = new JsonArray(), ["tests"] = new JsonObject(),
    ["agent"] = new JsonObject { ["status"] = "model_unavailable" },
    ["staging"] = new JsonObject { ["status"] = "not_configured" },
    ["limitations"] = new JsonArray("No real application was executed by these client checks."),
    ["artifacts"] = new JsonArray()
};
JsonObject Run(JsonObject? request = null, string? runId = null)
{
    request ??= Input();
    return new JsonObject
    {
        ["schema_version"] = "proofrun.release.v1", ["id"] = runId ?? id, ["status"] = "completed",
        ["request"] = request.DeepClone(), ["result"] = Result(request),
        ["created_at"] = "2026-09-29T12:00:00+00:00", ["updated_at"] = "2026-09-29T12:00:01+00:00",
        ["events"] = new JsonArray(), ["private_internal_field"] = "not-for-browser"
    };
}
HttpResponseMessage Json(JsonNode body) => new(HttpStatusCode.OK)
{
    Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json")
};
ProofRunReleaseClient Client(Func<HttpRequestMessage, HttpResponseMessage> handler)
    => new(origin, token, new FakeHandler(handler));
void Auth(HttpRequestMessage request, string expectedScope)
{
    Check(request.Headers.Authorization?.Scheme == "Bearer" && request.Headers.Authorization.Parameter == token, "server bearer token");
    Check(request.Headers.GetValues("X-ProofRun-Scope").Single() == expectedScope, "scope derives from authorized route argument");
}

foreach (var url in new[] { "file:///etc/passwd", "http://remote.example", "https://user:secret@worker.example",
    "https://worker.example/path", "https://worker.example?secret=x", "https://worker.example/#fragment", "http:\\localhost" })
    await Reject(() => { using var _ = new ProofRunReleaseClient(url, token); return Task.CompletedTask; }, "unsafe origin");
foreach (var badToken in new[] { "", "short", token + "\r\n", token + " " })
    await Reject(() => { using var _ = new ProofRunReleaseClient(origin, badToken); return Task.CompletedTask; }, "invalid bearer credential");

var expectedEvent = ProofRunReleaseClient.EventRunId(scope, "release-123");
Check(expectedEvent == "release-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes("[\"workspace-one\",\"release-123\"]"))).ToLowerInvariant()[..40], "canonical worker event ID");
Check(expectedEvent != ProofRunReleaseClient.EventRunId("workspace-two", "release-123"), "event identity includes workspace");
Check(ProofRunReleaseClient.EventRunId("a", "bc") != ProofRunReleaseClient.EventRunId("ab", "c"), "event identity boundaries");

var registry = new JsonObject
{
    ["schema_version"] = "proofrun.release.v1",
    ["targets"] = new JsonArray(new JsonObject
    {
        ["id"] = "customer-service", ["name"] = "Customer service", ["repair_enabled"] = false,
        ["staging_configured"] = false, ["contract_hash"] = new string('b', 64),
        ["revisions"] = new JsonArray(new string('0', 40), new string('1', 40)),
        ["repository"] = "/private/operator/repository", ["private_credential"] = "must-not-reach-browser",
        ["requirements"] = new JsonArray(new JsonObject
        {
            ["id"] = "customer-read", ["description"] = "Preserve customer reads.", ["kind"] = "preserve_response",
            ["path_prefix"] = "/customers", ["methods"] = new JsonArray("GET")
        })
    })
};
var expectedScope = scope;
using (var client = Client(request => { Auth(request, expectedScope); return Json(registry); }))
{
    var targets = await client.GetTargetsAsync(expectedScope, default);
    Check(targets["targets"]![0]!["repository"] is null && targets["targets"]![0]!["private_credential"] is null,
        "target configuration exposes only documented public fields");
    expectedScope = "workspace-two";
    await client.GetTargetsAsync(expectedScope, default);
}
var invalidRegistry = registry.DeepClone().AsObject();
invalidRegistry["targets"]![0]!["repair_enabled"] = "true";
using (var client = Client(_ => Json(invalidRegistry)))
    await Reject(() => client.GetTargetsAsync(scope, default), "malformed target registry");

using (var client = Client(request =>
{
    Auth(request, scope);
    Check(request.Method == HttpMethod.Post && request.RequestUri!.AbsolutePath == "/v1/release-runs", "fixed submission endpoint");
    var body = JsonNode.Parse(request.Content!.ReadAsStringAsync().GetAwaiter().GetResult())!.AsObject();
    Check(body["budget_seconds"]!.GetValue<int>() == 300 && !body["repair"]!.GetValue<bool>(), "missing defaults normalized before dispatch");
    return Json(Run(body));
}))
{
    var input = Input(); input.Remove("budget_seconds"); input.Remove("repair");
    var observed = await client.SubmitAsync(scope, input, default);
    Check(!input.ContainsKey("repair"), "caller request stays unchanged");
    Check(observed["private_internal_field"] is null, "unknown top-level fields are not exposed");
}

using (var client = Client(_ => throw new Exception("Invalid request was dispatched.")))
{
    foreach (var badScope in new[] { "", "workspace\r\nInjected: x", "../workspace" })
        await Reject(() => client.GetRunAsync(badScope, id, default), "invalid workspace scope", true);
    foreach (var badId in new[] { "../run", id + "\n", "release-short" })
        await Reject(() => client.GetRunAsync(scope, badId, default), "invalid run identifier", true);
    var mutations = new Action<JsonObject>[]
    {
        r => r["worker_url"] = "https://attacker.example", r => r["scope"] = "workspace-two",
        r => r["candidate_revision"] = new string('0', 40), r => r["candidate_revision"] = "main",
        r => r["budget_seconds"] = 601, r => r["repair"] = "true", r => r["event_id"] = "not safe",
        r => r["benefit"] = new string('x', 2001)
    };
    foreach (var mutate in mutations) { var request = Input(); mutate(request); await Reject(() => client.SubmitAsync(scope, request, default), "invalid input", true); }
}

foreach (var field in new[] { "target_id", "baseline_revision", "candidate_revision", "benefit", "budget_seconds", "repair" })
{
    using var client = Client(_ =>
    {
        var changed = Input();
        changed[field] = field switch
        {
            "baseline_revision" => JsonValue.Create(new string('2', 40)),
            "candidate_revision" => JsonValue.Create(new string('3', 40)),
            "budget_seconds" => JsonValue.Create(600), "repair" => JsonValue.Create(true),
            _ => JsonValue.Create("different")
        };
        return Json(Run(changed));
    });
    await Reject(() => client.SubmitAsync(scope, Input(), default), "POST response changed " + field);
}

var eventInput = Input(); eventInput["event_id"] = "release-123";
using (var client = Client(request => { Auth(request, scope); return Json(Run(eventInput, expectedEvent)); }))
    Check((await client.SubmitAsync(scope, eventInput, default))["id"]!.GetValue<string>() == expectedEvent, "event-bound submission accepted");
using (var client = Client(_ => Json(Run(eventInput, ProofRunReleaseClient.EventRunId("workspace-two", "release-123")))))
    await Reject(() => client.SubmitAsync(scope, eventInput, default), "event response belongs to another scope");
using (var client = Client(_ => Json(Run(runId: "release-" + new string('b', 40)))))
    await Reject(() => client.GetRunAsync(scope, id, default), "GET returned a different run");

var invalidRuns = new Action<JsonObject>[]
{
    r => r["schema_version"] = "proofrun.v1", r => r["status"] = "passed", r => r["result"] = null,
    r => r["result"]!["schema_version"] = "unknown", r => r["result"]!["recommendation"] = "ready",
    r => r["result"]!["revisions"]!["candidate"] = new string('4', 40),
    r => r["result"]!["coverage"]!["requirements_exercised"] = 2,
    r => r["result"]!["findings"] = new JsonArray(new JsonObject()),
    r => r["created_at"] = "not-a-date", r => r["request"]!["extra"] = true
};
foreach (var mutate in invalidRuns)
{
    var invalid = Run(); mutate(invalid);
    using var client = Client(_ => Json(invalid));
    await Reject(() => client.GetRunAsync(scope, id, default), "invalid response contract");
}
using (var client = Client(_ => new(HttpStatusCode.OK) { Content = new StringContent("not JSON") }))
    await Reject(() => client.GetRunAsync(scope, id, default), "malformed JSON");
using (var client = Client(_ => new(HttpStatusCode.OK) { Content = new StringContent("{\"schema_version\":\"proofrun.release.v1\",\"schema_version\":\"unknown\"}") }))
    await Reject(() => client.GetRunAsync(scope, id, default), "duplicate JSON fields");
using (var client = Client(_ => new(HttpStatusCode.InternalServerError) { Content = new StringContent("upstream-secret-and-url") }))
{
    try { await client.GetRunAsync(scope, id, default); throw new Exception("Upstream failure accepted."); }
    catch (ProofRunReleaseException error) { Check(!error.Message.Contains("upstream-secret-and-url"), "upstream error bodies are suppressed"); }
}

var evidence = Encoding.UTF8.GetBytes("{\"observed\":true}\n");
var artifact = new JsonObject
{
    ["id"] = "experiments.json", ["sha256"] = Convert.ToHexString(SHA256.HashData(evidence)).ToLowerInvariant(),
    ["bytes"] = evidence.Length, ["href"] = "https://untrusted.example/steal"
};
JsonObject ArtifactRun()
{
    var run = Run(); run["result"]!["artifacts"] = new JsonArray(artifact.DeepClone()); return run;
}
using (var client = Client(request =>
{
    Auth(request, scope);
    if (request.RequestUri!.AbsolutePath.Contains("/artifacts/"))
    {
        Check(request.RequestUri.AbsolutePath == $"/v1/release-runs/{id}/artifacts/experiments.json", "artifact URL uses only constrained IDs");
        return new(HttpStatusCode.OK) { Content = new ByteArrayContent(evidence) };
    }
    return Json(ArtifactRun());
}))
{
    var run = await client.GetRunAsync(scope, id, default);
    Check(run["result"]!["artifacts"]![0]!["href"] is null, "artifact href stripped");
    Check((await client.GetArtifactAsync(scope, id, "experiments.json", default)).SequenceEqual(evidence), "artifact hash and size verified");
    await Reject(() => client.GetArtifactAsync(scope, id, "not-listed.json", default), "unlisted artifact");
    await Reject(() => client.GetArtifactAsync(scope, id, "../secret.json", default), "artifact traversal", true);
}
foreach (var mode in new[] { "hash", "size", "duplicate" })
{
    var run = ArtifactRun();
    if (mode == "hash") run["result"]!["artifacts"]![0]!["sha256"] = new string('0', 64);
    if (mode == "size") run["result"]!["artifacts"]![0]!["bytes"] = evidence.Length + 1;
    if (mode == "duplicate") run["result"]!["artifacts"]!.AsArray().Add(artifact.DeepClone());
    using var client = Client(request => request.RequestUri!.AbsolutePath.Contains("/artifacts/")
        ? new(HttpStatusCode.OK) { Content = new ByteArrayContent(evidence) } : Json(run));
    await Reject(() => client.GetArtifactAsync(scope, id, "experiments.json", default), "artifact " + mode);
}

using (var client = Client(_ => new(HttpStatusCode.OK) { Content = new StreamContent(new SizedStream(4 * 1024 * 1024 + 1)) }))
    await Reject(() => client.GetRunAsync(scope, id, default), "unbounded response without content length");
using (var client = Client(_ => new(HttpStatusCode.OK) { Content = new StreamContent(new BlockingStream()) }))
using (var cancel = new CancellationTokenSource(30))
{
    try { await client.GetRunAsync(scope, id, cancel.Token); throw new Exception("Cancellation was ignored."); }
    catch (OperationCanceledException) { Check(cancel.IsCancellationRequested, "caller cancellation reaches body reads"); }
}

// Exercise the production HttpClientHandler on local listeners: redirects must never forward bearer credentials.
int FreePort()
{
    var socket = new TcpListener(IPAddress.Loopback, 0); socket.Start();
    var port = ((IPEndPoint)socket.LocalEndpoint).Port; socket.Stop(); return port;
}
using (var source = new HttpListener())
using (var destination = new HttpListener())
{
    var sourceUrl = $"http://127.0.0.1:{FreePort()}/";
    var destinationUrl = $"http://127.0.0.1:{FreePort()}/";
    source.Prefixes.Add(sourceUrl); destination.Prefixes.Add(destinationUrl);
    source.Start(); destination.Start();
    var forwarded = false;
    var redirect = Task.Run(async () =>
    {
        var request = await source.GetContextAsync();
        request.Response.StatusCode = 302; request.Response.RedirectLocation = destinationUrl;
        request.Response.Close();
    });
    var sink = Task.Run(async () =>
    {
        try
        {
            var request = await destination.GetContextAsync(); forwarded = true;
            var bytes = Encoding.UTF8.GetBytes(Run().ToJsonString());
            request.Response.ContentLength64 = bytes.Length;
            await request.Response.OutputStream.WriteAsync(bytes); request.Response.Close();
        }
        catch (HttpListenerException) { }
        catch (ObjectDisposedException) { }
    });
    using var client = new ProofRunReleaseClient(sourceUrl, token);
    await Reject(() => client.GetRunAsync(scope, id, default), "production handler redirect");
    await redirect; source.Stop(); destination.Stop(); await sink;
    Check(!forwarded, "redirect destination received no forwarded request");
}

Console.WriteLine($"PASS: {count} release client boundary checks. Fake transports plus a local redirect listener; no provider, application, staging, or SDK authorization run.");

sealed class FakeHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        => Task.FromResult(respond(request));
}
sealed class SizedStream(int remaining) : Stream
{
    public override bool CanRead => true; public override bool CanSeek => false; public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() { }
    public override int Read(byte[] buffer, int offset, int count)
    {
        var size = Math.Min(count, remaining); Array.Fill(buffer, (byte)'x', offset, size); remaining -= size; return size;
    }
    public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var size = Math.Min(buffer.Length, remaining); buffer.Span[..size].Fill((byte)'x'); remaining -= size;
        return ValueTask.FromResult(size);
    }
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}
sealed class BlockingStream : Stream
{
    public override bool CanRead => true; public override bool CanSeek => false; public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() { }
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    { await Task.Delay(Timeout.Infinite, cancellationToken); return 0; }
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}
