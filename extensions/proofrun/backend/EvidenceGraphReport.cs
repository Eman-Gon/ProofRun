using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Duplo.Extension.ProofRun;

// The browser receives a small, inert graph, never Neo4j records, connection settings or URLs.
public static class EvidenceGraphReport
{
    public const string Invalid = "The worker returned an invalid or differently bound evidence graph.";
    private static readonly Regex Id = new(@"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Hash = new(@"\A[a-f0-9]{64}\z", RegexOptions.CultureInvariant);
    private static readonly Regex FindingId = new(@"\A[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\z", RegexOptions.CultureInvariant);
    private static readonly HashSet<string> Kinds = new(StringComparer.Ordinal)
        { "run", "revision", "contract", "finding", "repair", "verification", "artifact", "test", "requirement", "evidence", "environment" };

    public static void RequireScope(string scope)
    {
        if (scope is null || !Id.IsMatch(scope))
            throw new ArgumentException("A valid authorized workspace identifier is required.");
    }

    public static JsonObject Parse(byte[] bytes)
    {
        using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 16 });
        UniqueFields(document.RootElement);
        return JsonNode.Parse(bytes, documentOptions: new JsonDocumentOptions { MaxDepth = 16 }) as JsonObject
            ?? throw new JsonException();
    }

    private static void UniqueFields(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in value.EnumerateObject())
            {
                if (!names.Add(property.Name)) throw new JsonException();
                UniqueFields(property.Value);
            }
        }
        else if (value.ValueKind == JsonValueKind.Array)
            foreach (var item in value.EnumerateArray()) UniqueFields(item);
    }

    public static JsonObject Validate(JsonObject report, string kind, string runId)
    {
        void Require(bool condition) { if (!condition) throw new ArgumentException(Invalid); }
        var status = Text(report, "status");
        Require(kind is "fixture" or "release" && Text(report, "kind") == kind && Text(report, "run_id") == runId
            && Text(report, "schema_version") == "proofrun.evidence-graph.v1" && Text(report, "provider") == "neo4j"
            && status is "ready" or "pending" or "unavailable"
            && report.ContainsKey("graph_id")
            && (Hash.IsMatch(Text(report, "graph_id")) || status != "ready"
                && (report["graph_id"] is null || Bounded(report, "graph_id", 0, true)))
            && Bounded(report, "observed_at", 80)
            && DateTimeOffset.TryParse(Text(report, "observed_at"), CultureInfo.InvariantCulture, DateTimeStyles.None, out _)
            && Bounded(report, "message", 500, true));
        if (report["nodes"] is not JsonArray nodes || nodes.Count > 80
            || report["edges"] is not JsonArray edges || edges.Count > 120) throw new ArgumentException(Invalid);
        Require(status == "ready" ? nodes.Count > 0 : nodes.Count == 0 && edges.Count == 0);
        var nodeIds = new HashSet<string>(StringComparer.Ordinal);
        var edgeIds = new HashSet<string>(StringComparer.Ordinal);
        var cleanNodes = new JsonArray();
        foreach (var item in nodes)
        {
            if (item is not JsonObject node) throw new ArgumentException(Invalid);
            Require(Id.IsMatch(Text(node, "id")) && nodeIds.Add(Text(node, "id"))
                && Kinds.Contains(Text(node, "kind")) && Bounded(node, "label", 120)
                && Optional(node, "status", 80) && Optional(node, "detail", 500)
                && (!node.ContainsKey("finding_id") || FindingId.IsMatch(Text(node, "finding_id"))));
            cleanNodes.Add(Only(node, "id", "kind", "label", "status", "detail", "finding_id"));
        }
        var cleanEdges = new JsonArray();
        foreach (var item in edges)
        {
            if (item is not JsonObject edge) throw new ArgumentException(Invalid);
            Require(Id.IsMatch(Text(edge, "id")) && edgeIds.Add(Text(edge, "id"))
                && nodeIds.Contains(Text(edge, "source")) && nodeIds.Contains(Text(edge, "target"))
                && Bounded(edge, "label", 120));
            cleanEdges.Add(Only(edge, "id", "source", "target", "label"));
        }
        var clean = Only(report, "schema_version", "provider", "status", "kind", "run_id", "graph_id", "observed_at", "message");
        if (report["graph_id"] is null) clean["graph_id"] = null;
        clean["nodes"] = cleanNodes; clean["edges"] = cleanEdges;
        return clean;
    }

    private static string Text(JsonObject obj, string field)
        => obj[field] is JsonValue value && value.TryGetValue<string>(out var text) ? text : "";
    private static bool Bounded(JsonObject obj, string field, int maximum, bool empty = false)
        => obj[field] is JsonValue value && value.TryGetValue<string>(out var text)
            && text.Length <= maximum && (empty || text.Length > 0)
            && !text.Any(c => char.IsControl(c) && c is not ('\n' or '\t'));
    private static bool Optional(JsonObject obj, string field, int maximum)
        => !obj.ContainsKey(field) || Bounded(obj, field, maximum, true);
    private static JsonObject Only(JsonObject obj, params string[] fields)
    {
        var result = new JsonObject();
        foreach (var field in fields) if (obj[field] is { } value) result[field] = value.DeepClone();
        return result;
    }
}
