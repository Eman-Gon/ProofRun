# ProofRun — core integration and three-chat build plan

Status: **PLANNED; live integrations unverified.** Source baseline: `7118d720ea2ae3e9bc3a2a4f7f61540348086451`, inspected September 29, 2026. Read [CLAUDE.md](CLAUDE.md) for current capabilities, commands, ownership and evidence rules. [OPTIONAL_SPONSORS.md](OPTIONAL_SPONSORS.md) stays deferred until the core gate passes.

## 1. First milestone and architecture

Press **Run verification** in a local DuploCloud extension, execute the existing prepared case through a Python worker, and display actual measured evidence. The first bridge may run locally and use the checked-in fix, with both facts labeled. It is not the complete sponsor demonstration.

The core then adds an OpenRouter-generated candidate, independent verification and real worker execution on Crusoe. The DuploCloud DevKit remains local; its local development license does not establish permission for production platform hosting.

```mermaid
flowchart TD
    A[Approved case and exact source revision] --> B[DuploCloud extension - Person 1]
    B --> C[Worker API and job service - Person 1]
    subgraph VM[Crusoe CPU VM - Person 3 deploys]
      C --> D[Baseline and updated tests - Person 2]
      D --> E{Regression reproduced?}
      E -->|Yes| F[Repair client - Person 3]
      F --> G[Independent verification - Person 2]
      G --> H[Bound evidence and patch]
      E -->|No or inconclusive| H
    end
    F <--> I[OpenRouter model API]
    H --> B
    B --> J[Engineer reviews]
```

The worker accepts registered cases and exact approved source bundles, not arbitrary shell commands. Build pinned images before testing; test containers remain without network or model credentials. Later HTTP replay against staging requires a separate network-enabled client and actual deployment identity.

## 2. Sponsor setup and proof of contribution

Capabilities are documented, not authenticated/runtime-tested here. Event credits and eligibility remain unverified; integration success alone does not guarantee an award.

### DuploCloud — Person 1

Needs Docker with modern Compose, Python 3, work-email verification, model access and available ports. Official authoring uses Claude Code. Personal email domains are rejected; source/runtime licensing differs. [DevKit README](https://github.com/duplocloud/devkit/blob/main/README.md) · [Hack Day setup](https://github.com/duplocloud/devkit/blob/main/hackday/Installing%20DevKit.md)

Keep the DevKit in a sibling directory, preserving ProofRun's repository and origin. From the parent directory, if that sibling does not already exist:

```bash
git clone https://github.com/duplocloud/devkit.git proofrun-duplocloud-devkit
cd proofrun-duplocloud-devkit
./run.sh
```

Use the setup flow for verification and credentials; do not run its repository-adoption script over ProofRun. Confirm services run, sign in at `http://localhost:4210`, select `extension-dev` and obtain a real agent response. Let the setup flow manage gateway configuration. OpenRouter documents an Anthropic-compatible route for this setup, but the selected model and actual startup must be verified. [Gateway instructions](https://openrouter.ai/docs/guides/coding-agents/claude-code-integration)

The no-cloud worker sample tests resource execution/result persistence. From the DevKit root:

```bash
./scripts/build-extension.sh samples/worker-compute
./scripts/deploy-extension.sh samples/worker-compute/dist/extension.zip
```

It demonstrates `ResourceWorkerBase`, `ApplyAsync` and `SaveProgressAsync`, useful for dispatch/poll behavior. A calculation result proves neither model access nor ProofRun. [Worker sample](https://github.com/duplocloud/devkit/blob/main/samples/worker-compute/README.md)

Build the custom C# backend/Angular UI against the running platform SDK. Keep custom source in proposed `extensions/proofrun/`. It calls the worker API, never the Docker socket. Test networking from the actual container: its `localhost` is not the laptop. [Architecture](https://github.com/duplocloud/devkit/blob/main/docs/architecture.md)

Show job identity, measured outcomes and artifacts in the portal. Deployment triggers are later: status/result hooks exist, but readiness, URL, revision and deduplication still need wiring. [Hook reference](https://github.com/duplocloud/devkit/blob/main/.claude/skills/duplo-extension-dev/reference/04-hooks.md)

### Crusoe — Person 3

Use an authorized account/project with available credits/quota and a CPU VM. Record VM identity, OS/architecture, worker revision and reachable network route. Install the Python/Docker runtime, prepare pinned images and deploy the same worker interface tested locally. This first fixture needs no GPU. [VM types and availability](https://docs.crusoecloud.com/compute/virtual-machines/overview/)

Connect through a private route or authenticated HTTPS; do not expose Docker directly. Execute the case on the VM and retain actual artifacts plus host identity. A local fallback is useful development evidence, not Crusoe integration. Crusoe inference through a model gateway would be a different architecture and does not prove this worker ran there. No VM was provisioned in this task.

### OpenRouter — Person 3

Requires a key with usable limits/credit and an available selected model. Use server-side bearer authentication with `POST https://openrouter.ai/api/v1/chat/completions`. Send bounded source, failure evidence and approved requirements; validate the proposal response. Record requested/returned model identifiers, operation reference and candidate provenance. [Quickstart](https://openrouter.ai/docs/quickstart) · [Authentication](https://openrouter.ai/docs/api_reference/authentication)

Do not assume an SDK is installed: existing `requests` can perform the HTTP call. Choose an explicit model during setup. A connectivity greeting is only preflight; the model must materially produce the claimed proposal/analysis. If unavailable, preserve the reproduced finding and mark repair unavailable. Never silently substitute a prepared fix while claiming generated repair.

### Target configuration — proposed, not implemented

This replaces the inherited configuration guidance. These settings are not all understood by today's code.

| Setting / setup item | Consumer | Responsibility |
|---|---|---|
| `OPENROUTER_API_KEY` | Repair service | Model authentication; never enters test containers |
| `PROOFRUN_MODEL` | Repair service | Explicit model selected at setup |
| `PROOFRUN_WORKER_URL` | Extension backend | Reachable local/remote worker URL |
| `PROOFRUN_WORKER_TOKEN` | Extension and worker | Server authentication; never browser data |
| `PROOFRUN_ARTIFACT_DIR` | Worker | Private persisted evidence location |
| `PROOFRUN_EXECUTION_TARGET` | Worker | `local` / `crusoe`; a label alone is not hosting evidence |
| Setup-managed DuploCloud settings | DevKit | Admin verification, gateway and workspace |
| Crusoe account/VM access | Deployment operator | Provisioning and host management, outside test containers |

Put pins, image identities, test limits and case selection in versioned manifests. Person 3 owns configuration/example-file migration. Do not add legacy provider credentials to these requirements. Existing imports may still require code/dependency cleanup even for a credential-free offline entry point.

## 3. Shared contracts — agree before parallel edits

Version: `proofrun.v1`. Owner: Person 1; Persons 2 and 3 acknowledge it before implementing consumers. All endpoints, new modules and types below are **proposed**, not callable features at the inspected baseline.

One worker, one concurrent run and file-backed records/artifacts are enough. Use atomic state writes; avoid a new queue/database. An interrupted job after restart must not appear completed.

### HTTP boundary

- `POST /v1/runs`: validate a registered case and bindings; return `202` with run ID, then execute asynchronously.
- `GET /v1/runs/{run_id}`: return execution/finding/repair states and evidence references.
- `GET /v1/runs/{run_id}/artifacts/{artifact_id}`: authenticated access to that run's allowed artifacts, never arbitrary paths.
- Identical input with the same `job_key` returns the existing run; changed input using the same key returns `409`. Invalid bindings or unregistered cases fail validation before execution. Authentication errors are separate from application failures.

Illustrative submission. Replace angle-bracket placeholders with calculated identifiers; reject them literally in implementation.

```json
{
  "schema_version": "proofrun.v1",
  "job_key": "demo-001",
  "case_id": "customer-nickname-v1",
  "source": {
    "repository": "https://github.com/Eman-Gon/ProofRun",
    "revision": "<full-commit-sha>",
    "module": "demo/upgrade/app.py",
    "sha256": "<source-sha256>"
  },
  "contract": {"id": "customer-input-v1", "sha256": "<contract-sha256>"},
  "environments": {"baseline": "pydantic-1.10.18", "updated": "pydantic-2.8.2"},
  "repair": {"enabled": true, "max_attempts": 2}
}
```

Resolve environment labels from an approved manifest; record actual immutable image IDs, requirements hashes and observed versions. Validate source revision/content. Represent uncommitted application changes by an explicit bundle hash rather than silently attributing them to HEAD. First allow only the checked-in case, not submitted shell commands, arbitrary URLs or executable tests.

Illustrative response summary, not an observed result:

```json
{
  "schema_version": "proofrun.v1",
  "run_id": "run-demo-001",
  "job_key": "demo-001",
  "execution_status": "completed",
  "finding_status": "regression_reproduced",
  "repair_status": "verified",
  "bindings": {
    "revision": "<full-commit-sha>",
    "source_sha256": "<source-sha256>",
    "contract_sha256": "<contract-sha256>",
    "candidate_sha256": "<candidate-sha256>",
    "environment_manifest_sha256": "<environment-manifest-sha256>"
  },
  "execution": {"target": "crusoe", "worker_id": "<recorded-worker-id>"},
  "proposal": {"mode": "live", "gateway": "openrouter", "model": "<returned-model-id>"},
  "cases": [
    {"id": "nickname_omitted", "stage": "baseline", "status": "passed"},
    {"id": "nickname_omitted", "stage": "updated", "status": "failed"},
    {"id": "nickname_omitted", "stage": "repaired_updated", "status": "passed"},
    {"id": "nickname_object_rejected", "stage": "repaired_updated", "status": "passed"}
  ],
  "artifacts": ["case-results.json", "verification.json", "candidate.patch"],
  "limitations": ["One configured model; complete case matrix is in verification.json"]
}
```

This abbreviated response alone cannot establish `verified`. The complete artifact must include all expected case IDs, original-suite results and environments. Record exit codes, actual versions, commands, expected/observed behavior, elapsed time, test hashes and output references.

### Internal Python boundaries

| Proposed call | Owner → consumer | Output |
|---|---|---|
| `run_comparison(case_spec, source_bundle)` | Person 2 → Person 1 service | `ComparisonEvidence`: bindings, actual environments, expected/executed cases, observations, finding and artifacts |
| `propose_patch(failure_context, attempt)` | Person 3 → Person 1 service | `PatchProposal`: base hash, allowed path, diff/replacement, rationale, model provenance; no verdict |
| `verify_candidate(case_spec, source_bundle, proposal)` | Person 2 → Person 1 service | `VerificationEvidence`: candidate hash, original suite/controls, completeness and deterministic verdict |

Person 1 owns shared schema definitions in `src/proofrun/contracts.py`; request changes instead of creating incompatible types. Person 1's service limits proposals to two attempts. Person 2 applies allowed edits in disposable copies and validates the tested content against the returned patch. Person 3 cannot edit acceptance tests or expected outputs.

### State semantics

| Field | Values | Meaning |
|---|---|---|
| `execution_status` | `queued`, `running`, `completed`, `setup_failed`, `timed_out`, `interrupted` | Workflow execution; completed does not mean the application passed. |
| `finding_status` | `not_tested`, `regression_reproduced`, `no_difference_observed`, `inconclusive` | A supported baseline pass/update failure establishes reproduction; unrelated crashes do not. |
| `repair_status` | `not_requested`, `pending`, `proposed`, `verified`, `rejected`, `unavailable` | Only the verifier sets verified; model refusal/outage means unavailable. |

Preserve a reproduced finding when repair fails. A setup error cannot become no difference. Hash/environment mismatch invalidates acceptance. Show all three states instead of a single misleading green badge. Current CLI exit `1` confirms the prepared break; it is not automatically a worker failure.

## 4. Verification plan — Person 2 owns truth

The existing fixture and checked-in fix are prepared examples. Generalize configuration before claiming support for other model/field names.

Required cases: omitted nickname normalized as approved; null accepted; valid string unchanged; object nickname rejected; missing required name rejected. Rerun every original test on original and repaired code across both pinned environments where the approved contract applies. Record the expected regression without treating it as an infrastructure failure.

Negative demonstration: compare a narrow explicit-default patch with an intentionally permissive candidate accepting any value. Both may remove the omission error; only one preserving the independent controls can pass. This is a planned experiment, not a recorded result.

Reject empty, missing, duplicate or wrong test sets; stale bindings; verifier edits; unsupported imports; incorrect runtime versions; and setup failures. Preserve isolation/timeouts. Hash the candidate after patch application and ensure those exact files produced the evidence.

## 5. Milestones, dependencies and time box

Assume accounts, DevKit images and test images are ready before the six-hour window. Setup blockers consume time; reduce scope without renaming incomplete work as complete.

| Checkpoint | Person 1 | Person 2 | Person 3 | Exit evidence |
|---|---|---|---|---|
| Before build | Portal/sample; agree contracts | Inspect/offline baseline | Confirm model/VM access | Access record and source-grounded baseline |
| Hour 0–1 | API/extension bridge | Adapt prepared evidence | Provider config/host setup | Actual local comparison displayed in portal |
| Hours 1–3 | Consume agreed results | Original-suite repair runs, controls, states | Model proposals and Crusoe worker | Bad candidate rejected; actual model output |
| Hours 3–4.5 | Integrate repair loop/remote endpoint | Full candidate matrix remotely | Real VM execution and provenance | Generated candidate with remote measured results |
| Hours 4.5–6 | UI/rehearsal/completion record | Negative/freshness checks | Restart/setup verification | Reproducible integrated demonstration |

Person 2's offline work and Person 3's access checks can proceed while Person 1 sets up DevKit. Label temporary API fixtures as mocks; they unblock UI work but cannot satisfy the live gate.

Cut automatic deployment hooks, PR publication, a second compatibility scenario and optional sponsors first. If model or VM access fails, retain a useful local demo and mark the affected core gate blocked. Do not use optional integrations to obscure incomplete core work.

## 6. Copy-ready prompts for three separate chats

### Person 1 — DuploCloud and integration

```text
You are Person 1, ProofRun's DuploCloud and integration lead. Work in the existing ProofRun repository on your assigned branch/worktree. Read CLAUDE.md and INTEGRATION.md, inspect the checkout, and follow file ownership/evidence rules.

First verify the local DevKit and agree proofrun.v1 with Persons 2 and 3. Build a DuploCloud action that submits one registered case to the Python worker and displays actual results. Own extensions/proofrun, contracts.py, api.py, service.py, API/contract tests and shared docs. Follow CLAUDE.md for existing-file ownership.

Consume Person 2's comparison/verifier and Person 3's repair client/host connection. Own the bounded orchestration loop, not the test verdict. Clearly label any temporary mock. Request teammate changes instead of editing their files.

Deliver portal-to-runner execution, separate execution/finding/repair states, artifact links and an integration record. Validate the same contract locally and on the provided Crusoe worker. Record commands, actual results, exact revision and blockers. Keep optional work deferred until every core gate passes. Hand off changed paths, interface version and evidence location.
```

### Person 2 — verification engine

```text
You are Person 2, ProofRun's verification lead. Read CLAUDE.md and INTEGRATION.md and inspect your assigned branch/worktree. Own only the listed runner, fixture and verification-test files.

First execute the existing offline Pydantic comparison once dependencies/images are ready. Then run original tests on repaired code and separate regression findings from repair outcomes. Implement run_comparison and verify_candidate using the shared proofrun.v1 types; request schema changes from Person 1.

Own omission/null/string/invalid-object/required-name controls independently of the repairer. Preserve source/environment/test/candidate bindings, exact case execution and isolated Docker behavior. Prove a permissive candidate is rejected; missing tests, wrong versions or stale evidence cannot pass.

Give measured results/artifacts to Person 1 and coordinate Crusoe execution with Person 3. Request dependency-file changes from Person 3, their designated owner. Do not edit model/UI code. Hand off paths, interface version, commands/results and limitations. Optional work stays deferred.
```

### Person 3 — repair and sponsor infrastructure

```text
You are Person 3, ProofRun's repair-agent and infrastructure lead. Read CLAUDE.md and INTEGRATION.md and inspect your assigned branch/worktree. Own repair.py, config.py, deploy/crusoe, repair-client tests and the designated dependency/configuration files.

First establish available OpenRouter model access and authorized Crusoe CPU-VM access; assume no credentials/credits exist until confirmed. Implement propose_patch against Person 1's contract and prepare the compatible worker host. Crusoe hosts execution in this architecture; model routing alone does not fulfill that role.

Give the model bounded source, approved requirements and measured failure evidence. Return a candidate with base hash/provenance. Do not edit tests, expected outputs or dependency pins, or declare verification success. Person 2's verifier decides. Keep credentials outside test containers and browser responses.

Deliver an actual generated candidate, an authenticated reachable worker and a real verification run on Crusoe. Keep local/live status honest and document reproducible deployment steps. Coordinate shared configuration changes through Person 1. Hand off revision, paths, actual commands/results and blockers. BAND and other optional integrations remain deferred until the core gate passes.
```

## 7. Core completion record and demo

Person 1 updates this from handoff evidence. Writing documents does not complete any gate.

| Gate | Required evidence | Status |
|---|---|---|
| DuploCloud round trip | Actual resource/job and returned measured result | NOT VERIFIED |
| Crusoe contribution | VM/worker identity, source revision and execution artifacts | NOT VERIFIED |
| OpenRouter contribution | Actual proposal operation, model provenance and candidate | NOT VERIFIED |
| Reproduction | Approved baseline pass and supported update failure | NOT VERIFIED |
| Repair verification | Original suite and independent controls on accepted candidate | NOT VERIFIED |
| Bad-fix rejection | Permissive candidate rejected by approved controls | NOT VERIFIED |
| Evidence integrity | Source/case/environment/candidate binding; stale/empty evidence rejected | NOT VERIFIED |
| Fresh demonstration | Another teammate can repeat setup/run; failure states and limitations visible | NOT VERIFIED |

Two-minute demo: show the approved customer case, start the check in DuploCloud, display baseline/update evidence, show the generated patch and rejected permissive alternative, then the accepted candidate's complete verification report with real worker/model provenance. If execution takes longer, start earlier and label historical output honestly. Do not present a saved run as a new live execution.

Integrated revision: **not recorded**. Evidence/run ID: **not recorded**. Core status: **PLANNED**. When every gate passes, record `CORE_COMPLETE`, then evaluate one optional integration.

Handoff format:

```text
Person / task / branch or worktree:
Revision or uncommitted diff and changed paths:
Interface version / requested shared changes:
Commands and checks actually run:
Result and evidence location:
Live services / mocks / synthetic inputs:
Blockers and next owner:
```

Documentation validation: global Python 3.12 successfully ran the existing CLI help commands. No application tests, comparison runs, account registrations, model calls, VM provisioning or deployments were executed. Setup/build/test commands were source-checked; proposed interfaces need implementation and runtime validation.
