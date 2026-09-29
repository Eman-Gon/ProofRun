using Duplo.Ai.DataManagement.AccessControl;
using Duplo.Ai.DataManagement.Controllers.User.Resource;
using Duplo.Ai.Model;
using Duplo.Ai.Model.Interfaces;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Duplo.Extension.ProofRun;

[ApiController]
[Route("v1/aiservicedesk/user/data/workspaces/{workspaceId}/environment/extensions/proofruns")]
[AccessControl(Parent = typeof(Workspace), ParentIdProperty = "OwnerWorkspaceId")]
public partial class ProofRunController : ResourcesController<ProofRunVerification, ProofRunSpec, ProofRunResult>
{
    private readonly ProofRunService _service;

    public ProofRunController(IEntityService<ProofRunVerification> service,
        ILogger<ProofRunController> logger) : base(service, logger)
        => _service = (ProofRunService)service;

    // The SDK's inherited resource filter enforces workspace and GET access on this resource.
    [HttpGet("{id}/artifacts/{artifactId}")]
    public async Task<IActionResult> Artifact(string workspaceId, string id, string artifactId,
        CancellationToken ct)
    {
        var entity = await ResourceServiceFacet.GetByIdAsync(id, ct);
        if (entity is null || !string.Equals(entity.OwnerWorkspaceId, workspaceId, StringComparison.Ordinal))
            return NotFound();
        try
        {
            var bytes = await _service.FetchArtifactAsync(entity, artifactId, ct);
            // JSON travels through the portal's authenticated HTTP client, including token auth.
            // The frontend saves inert octet-stream data, never navigates to upstream HTML/content.
            return Ok(new { data = new { fileName = artifactId, base64 = Convert.ToBase64String(bytes) } });
        }
        catch (FileNotFoundException) { return NotFound(); }
        catch (ProofRunBridgeException ex) { return StatusCode(502, new { errors = ex.Message }); }
    }

    // The inherited resource filter authorizes this existing resource. A research request never updates a verdict.
    [HttpPost("{id}/research")]
    public async Task<IActionResult> Research(string workspaceId, string id,
        [FromBody] ProofRunResearchRequest request, CancellationToken ct)
    {
        var entity = await ResourceServiceFacet.GetByIdAsync(id, ct);
        if (entity is null || !string.Equals(entity.OwnerWorkspaceId, workspaceId, StringComparison.Ordinal))
            return NotFound();
        try { return Ok(new { data = await _service.FetchResearchAsync(entity, request, ct) }); }
        catch (ArgumentException ex) { return BadRequest(new { errors = ex.Message }); }
        catch (ProofRunBridgeException ex) { return StatusCode(502, new { errors = ex.Message }); }
    }
}
