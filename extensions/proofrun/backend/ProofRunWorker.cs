using System.Text.Json.Nodes;
using Duplo.Ai.DataManagement.Services.Workers;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

namespace Duplo.Extension.ProofRun;

public class ProofRunWorker : ResourceWorkerBase<ProofRunVerification, ProofRunSpec, ProofRunResult>
{
    private readonly IConfiguration _config;

    public ProofRunWorker(IServiceScopeFactory scopeFactory, ILogger<ProofRunWorker> logger,
        IConfiguration config) : base(scopeFactory, logger, config) => _config = config;

    protected override async Task ApplyAsync(ProofRunVerification entity, IServiceProvider scope,
        CancellationToken ct)
    {
        entity.Result ??= new ProofRunResult();
        try
        {
            if (entity.Spec?.CaseId != ProofRunWorkerClient.CaseId)
                throw new ProofRunBridgeException("Unsupported case; create a registered ProofRun verification.");
            using var client = ProofRunWorkerClient.FromConfiguration(_config);
            if (entity.Result.SubmissionJson is null)
            {
                var submission = await client.GetSubmissionAsync(entity.OwnerWorkspaceId ?? "", entity.Id, entity.Spec.EnableRepair, ct,
                    entity.Spec.FailureResearchId);
                entity.Result.SubmissionJson = submission.ToJsonString();
                // Persist before POST. A retry reuses the exact bindings and stable idempotency key.
                await SaveProgressAsync(scope, entity, "Approved case bindings saved; dispatching verification.", ct);
            }
            var request = JsonNode.Parse(entity.Result.SubmissionJson)!.AsObject();
            var run = entity.Result.RunId is { Length: > 0 } id
                ? await client.GetRunAsync(id, ct)
                : await client.SubmitAsync(request, ct);

            // A bounded poll ends with an explicit bridge failure, never a fabricated worker verdict.
            var deadline = DateTime.UtcNow.AddMinutes(20);
            while (true)
            {
                ProofRunWorkerClient.RequireJobKey(run, ProofRunWorkerClient.JobKey(entity.OwnerWorkspaceId ?? "", entity.Id));
                entity.Result.RunId = run["run_id"]!.GetValue<string>();
                entity.Result.RunJson = ProofRunWorkerClient.ForBrowser(run).ToJsonString();
                entity.Result.LastObservedAt = DateTime.UtcNow;
                entity.Result.BridgeError = null;
                var status = run["execution_status"]!.GetValue<string>();
                await SaveProgressAsync(scope, entity, $"Worker execution: {status}. See the separate finding and repair states.", ct);
                if (status is not ("queued" or "running")) return;
                if (DateTime.UtcNow >= deadline)
                    throw new ProofRunBridgeException("The bridge stopped polling after 20 minutes. Worker execution may continue; reconcile this resource to resume observing it.");
                await Task.Delay(TimeSpan.FromSeconds(3), ct);
                run = await client.GetRunAsync(entity.Result.RunId, ct);
            }
        }
        catch (ProofRunBridgeException ex)
        {
            entity.Result.BridgeError = ex.Message;
            await SaveProgressAsync(scope, entity, ex.Message, ct);
            throw;
        }
    }

    protected override Task VerifyDriftAsync(ProofRunVerification entity, IServiceProvider scope,
        CancellationToken ct) => Task.CompletedTask;
    protected override Task DeleteSubResourcesAsync(ProofRunVerification entity, IServiceProvider scope,
        CancellationToken ct) => Task.CompletedTask;
    protected override Task<bool> WaitForDeletionAsync(ProofRunVerification entity, IServiceProvider scope,
        CancellationToken ct) => Task.FromResult(true);
}
