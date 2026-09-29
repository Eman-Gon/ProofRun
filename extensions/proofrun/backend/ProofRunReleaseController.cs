using Duplo.Ai.DataManagement.AccessControl;
using Duplo.Ai.Model;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Configuration;
using System.Text.Json.Nodes;

namespace Duplo.Extension.ProofRun;

// These actions share the existing controller registration. Do not add another resource/ACL node.
public partial class ProofRunController
{
    private const string ReleaseRoute = "~/v1/aiservicedesk/user/data/workspaces/{workspaceId}/environment/extensions/releaseinvestigations";

    [HttpGet(ReleaseRoute + "/targets")]
    [AccessControl(RouteKey = "workspaceId", AnchorNode = "Workspace", Verb = AccessVerbs.Get)]
    public async Task<IActionResult> ReleaseTargets([FromRoute] string workspaceId,
        [FromServices] IConfiguration config, CancellationToken ct)
    {
        try
        {
            using var client = ProofRunReleaseClient.FromConfiguration(config);
            return Ok(new { data = await client.GetTargetsAsync(workspaceId, ct) });
        }
        catch (ArgumentException ex) { return BadRequest(new { errors = ex.Message }); }
        catch (ProofRunReleaseException ex) { return StatusCode(ex.StatusCode, new { errors = ex.Message }); }
    }

    [HttpPost(ReleaseRoute)]
    [RequestSizeLimit(16000)]
    [AccessControl(RouteKey = "workspaceId", AnchorNode = "Workspace", Verb = AccessVerbs.Post)]
    public async Task<IActionResult> SubmitRelease([FromRoute] string workspaceId,
        [FromBody] JsonObject request, [FromServices] IConfiguration config, CancellationToken ct)
    {
        try
        {
            using var client = ProofRunReleaseClient.FromConfiguration(config);
            return StatusCode(202, new { data = await client.SubmitAsync(workspaceId, request, ct) });
        }
        catch (ArgumentException ex) { return BadRequest(new { errors = ex.Message }); }
        catch (ProofRunReleaseException ex) { return StatusCode(ex.StatusCode, new { errors = ex.Message }); }
    }

    [HttpGet(ReleaseRoute + "/{id}")]
    [AccessControl(RouteKey = "workspaceId", AnchorNode = "Workspace", Verb = AccessVerbs.Get)]
    public async Task<IActionResult> ReleaseRun([FromRoute] string workspaceId, [FromRoute] string id,
        [FromServices] IConfiguration config, CancellationToken ct)
    {
        try
        {
            using var client = ProofRunReleaseClient.FromConfiguration(config);
            return Ok(new { data = await client.GetRunAsync(workspaceId, id, ct) });
        }
        catch (ArgumentException ex) { return BadRequest(new { errors = ex.Message }); }
        catch (ProofRunReleaseException ex) { return StatusCode(ex.StatusCode, new { errors = ex.Message }); }
    }

    [HttpGet(ReleaseRoute + "/{id}/graph")]
    [AccessControl(RouteKey = "workspaceId", AnchorNode = "Workspace", Verb = AccessVerbs.Get)]
    public async Task<IActionResult> ReleaseGraph([FromRoute] string workspaceId, [FromRoute] string id,
        [FromServices] IConfiguration config, CancellationToken ct)
    {
        try
        {
            using var client = ProofRunReleaseClient.FromConfiguration(config);
            return Ok(new { data = await client.GraphAsync(workspaceId, id, ct) });
        }
        catch (ArgumentException ex) { return BadRequest(new { errors = ex.Message }); }
        catch (ProofRunReleaseException ex) { return StatusCode(ex.StatusCode, new { errors = ex.Message }); }
    }

    [HttpGet(ReleaseRoute + "/{id}/artifacts/{artifactId}")]
    [AccessControl(RouteKey = "workspaceId", AnchorNode = "Workspace", Verb = AccessVerbs.Get)]
    public async Task<IActionResult> ReleaseArtifact([FromRoute] string workspaceId, [FromRoute] string id,
        [FromRoute] string artifactId, [FromServices] IConfiguration config, CancellationToken ct)
    {
        try
        {
            using var client = ProofRunReleaseClient.FromConfiguration(config);
            var bytes = await client.GetArtifactAsync(workspaceId, id, artifactId, ct);
            return Ok(new { data = new { fileName = artifactId, base64 = Convert.ToBase64String(bytes) } });
        }
        catch (ArgumentException ex) { return BadRequest(new { errors = ex.Message }); }
        catch (ProofRunReleaseException ex) { return StatusCode(ex.StatusCode, new { errors = ex.Message }); }
    }

    [HttpPost(ReleaseRoute + "/{id}/failure-research")]
    [RequestSizeLimit(16000)]
    [AccessControl(RouteKey = "workspaceId", AnchorNode = "Workspace", Verb = AccessVerbs.Post)]
    public async Task<IActionResult> ReleaseFailureResearch([FromRoute] string workspaceId, [FromRoute] string id,
        [FromBody] JsonObject request, [FromServices] IConfiguration config, CancellationToken ct)
    {
        try
        {
            using var client = ProofRunReleaseClient.FromConfiguration(config);
            return Ok(new { data = await client.FailureResearchAsync(workspaceId, id, request, ct) });
        }
        catch (ArgumentException ex) { return BadRequest(new { errors = ex.Message }); }
        catch (ProofRunReleaseException ex) { return StatusCode(ex.StatusCode, new { errors = ex.Message }); }
    }
}
