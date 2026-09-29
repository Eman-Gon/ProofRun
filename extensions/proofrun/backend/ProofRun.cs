using Duplo.Ai.DataManagement.Interfaces;
using Duplo.Ai.DataManagement.Services;
using Duplo.Ai.Model.Attributes;
using Duplo.Ai.Model.Interfaces;
using Duplo.Ai.Model.Resource;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using System.Text.Json.Nodes;
using MongoDB.Bson.Serialization.Attributes;

namespace Duplo.Extension.ProofRun;

[BsonIgnoreExtraElements]
public class ProofRunSpec : BaseSpec
{
    public string CaseId { get; set; } = ProofRunWorkerClient.CaseId;
    public bool EnableRepair { get; set; }
    public string? FailureResearchId { get; set; }
}

[BsonIgnoreExtraElements]
public class ProofRunResult : BaseResult
{
    // JSON strings avoid MongoDB serialization assumptions about System.Text.Json nodes.
    // Neither the server URL nor credentials are ever persisted in this resource.
    public string? SubmissionJson { get; set; }
    public string? RunId { get; set; }
    public string? RunJson { get; set; }
    public string? BridgeError { get; set; }
    public DateTime? LastObservedAt { get; set; }
}

[BsonCollection("extension_proofruns")]
[BsonIgnoreExtraElements]
public class ProofRunVerification : ResourceBase<ProofRunSpec, ProofRunResult>
{
    public override string GetTicketOriginType() => "ProofRunVerification";
    public override string GetTicketOriginSubType() => "proofrun-verification";
}

public class ProofRunHooks : DefaultEntityHooks<ProofRunVerification> { }

public class ProofRunService : ResourceServiceBase<ProofRunVerification, ProofRunSpec, ProofRunResult>
{
    private readonly IConfiguration _config;

    public ProofRunService(IRepository<ProofRunVerification> repository,
        ILogger<ProofRunService> logger, IServiceScopeFactory scopeFactory,
        IHttpContextAccessor httpContextAccessor, IConfiguration config)
        : base(repository, logger, scopeFactory, httpContextAccessor) => _config = config;

    protected override ProvisioningMode NoSkillsFallbackMode => ProvisioningMode.Worker;

    protected override async Task ValidateSpecAsync(ProofRunSpec spec, bool isUpdate,
        ProofRunSpec? existingSpec, CancellationToken ct)
    {
        await base.ValidateSpecAsync(spec, isUpdate, existingSpec, ct);
        if (spec.CaseId != ProofRunWorkerClient.CaseId)
            throw new ArgumentException("Only the registered customer-nickname-v1 case is supported.");
        if (isUpdate && existingSpec is not null && existingSpec.EnableRepair != spec.EnableRepair)
            throw new ArgumentException("Create a fresh verification to change whether repair is requested.");
        if (spec.FailureResearchId is not null && (!spec.EnableRepair || !FailureResearchReport.IsId(spec.FailureResearchId)))
            throw new ArgumentException("Select completed failure research and enable a fresh repair check.");
        if (isUpdate && existingSpec is not null && existingSpec.FailureResearchId != spec.FailureResearchId)
            throw new ArgumentException("Create a fresh verification to use different failure research.");
    }

    public async Task<byte[]> FetchArtifactAsync(ProofRunVerification entity, string artifactId,
        CancellationToken ct)
    {
        if (entity.Result?.RunId is not { Length: > 0 } runId)
            throw new FileNotFoundException("This resource has no worker run yet.");
        using var client = ProofRunWorkerClient.FromConfiguration(_config);
        // Fetch the authoritative run again; browser-controlled result updates cannot add allowed IDs.
        var run = await client.GetRunAsync(runId, ct);
        ProofRunWorkerClient.RequireJobKey(run, ProofRunWorkerClient.JobKey(entity.OwnerWorkspaceId ?? "", entity.Id));
        return await client.GetArtifactAsync(run, artifactId, ct);
    }

    public async Task<JsonObject> FetchResearchAsync(ProofRunVerification entity, ProofRunResearchRequest request,
        CancellationToken ct)
    {
        using var client = ProofRunWorkerClient.FromConfiguration(_config);
        return await client.ResearchAsync(entity.OwnerWorkspaceId ?? "", entity.Id, request, ct);
    }

    public async Task<JsonObject> FetchGraphAsync(ProofRunVerification entity, CancellationToken ct)
    {
        if (entity.Result?.RunId is not { Length: > 0 } runId)
            throw new FileNotFoundException("This resource has no worker run yet.");
        using var client = ProofRunWorkerClient.FromConfiguration(_config);
        return await client.GraphAsync(entity.OwnerWorkspaceId ?? "", entity.Id, runId, ct);
    }

    public async Task<JsonObject> FetchFailureResearchAsync(ProofRunVerification entity, FailureResearchRequest request,
        CancellationToken ct)
    {
        if (entity.Result?.RunId is not { Length: > 0 } runId)
            throw new FileNotFoundException("This resource has no worker run yet.");
        using var client = ProofRunWorkerClient.FromConfiguration(_config);
        return await client.FailureResearchAsync(entity.OwnerWorkspaceId ?? "", entity.Id, runId, request, ct);
    }
}
