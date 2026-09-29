# ProofRun — core integration and three-chat build plan

Status: **IN PROGRESS: authenticated local worker and native verification execute successfully; DuploCloud portal, Crusoe and live OpenRouter gates remain blocked on setup/access.** Source baseline: `7118d720ea2ae3e9bc3a2a4f7f61540348086451`, inspected September 29, 2026. Read [CLAUDE.md](CLAUDE.md) for current capabilities, commands, ownership and evidence rules. [OPTIONAL_SPONSORS.md](OPTIONAL_SPONSORS.md) stays deferred until the core gate passes.

## User-supplied sponsor references

Links supplied September 29, 2026. Documentation access does not establish account access, event eligibility or a working integration.

| Reference | Use and review status |
|---|---|
| [OpenRouter — The AI Conference Hackathon](https://openrouter.notion.site/The-AI-Conference-Hackathon-x-OpenRouter-3e62fd57c4dc803b859de72f6a99e023) | Event-specific reference for Person 3. The web tool could not retrieve this page; credits, rules and setup details remain unverified. |
| [Similarweb MCP setup](https://docs.similarweb.com/api-v5/similarweb-mcp/mcp-setup) | Setup documentation retrieved. Optional evaluation only; see [OPTIONAL_SPONSORS.md](OPTIONAL_SPONSORS.md). |
| [User-supplied Google document](https://docs.google.com/document/d/1Sz2JgqQPuo-81KyrwESvrHK26Zp5t7bjVPC2uRfY3lo/edit?tab=t.0) | The web tool could not retrieve this document. Its title, sponsor association and requirements remain unverified. |
| [BAND hacker guide](https://www.band.ai/hacker-guide) | Guide retrieved; existing optional proposer/verifier handoff reference. |
| [DuploCloud DevKit repository](https://github.com/duplocloud/devkit) | Repository page retrieved; core extension setup reference. |

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

### Current configuration

The root `.env.example` contains only the current ProofRun stack. Use `requirements-worker.txt` for the native worker and the setup commands in `README.md`. The worker reads exported environment variables; it does not automatically load `.env`. DuploCloud's account/license/model setup remains in the separate DevKit, and Crusoe deployment uses `deploy/crusoe/worker.env.example`.

| Setting / setup item | Consumer | Responsibility |
|---|---|---|
| `OPENROUTER_API_KEY` | Repair service | Model authentication; never enters test containers |
| `PROOFRUN_MODEL` | Repair service | Explicit model selected at setup |
| `PROOFRUN_WORKER_URL` | Extension backend | Reachable local/remote worker URL |
| `PROOFRUN_WORKER_TOKEN` | Extension and worker | Server authentication; never browser data |
| `PROOFRUN_ARTIFACT_DIR` | Worker | Private persisted evidence location |
| `PROOFRUN_EXECUTION_TARGET` | Worker | `local` / `crusoe`; a label alone is not hosting evidence |
| `PROOFRUN_WORKER_ID` | Worker | `local-worker` locally; the recorded VM identity on Crusoe |
| Setup-managed DuploCloud settings | DevKit | Admin verification, gateway and workspace |
| Crusoe account/VM access | Deployment operator | Provisioning and host management, outside test containers |

Put pins, image identities, test limits and case selection in versioned manifests. The native worker does not require credentials or repository/test-command settings from the earlier application. Keep those settings out of the active environment template.

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

Open three separate agent chats against this repository. Give each chat its own assigned branch/worktree, then paste **only its complete text block below**. Each prompt tells the agent to read the shared documents before implementing.

Start Person 1 first to establish the shared interface. Persons 2 and 3 can inspect the runner and check prerequisites in parallel, but must use Person 1's agreed contract before integrating. If all chats share one checkout, enforce the file ownership in `CLAUDE.md`; separate chat windows alone do not prevent conflicting edits.

- [Person 1: DuploCloud and integration](#person-1--duplocloud-and-integration)
- [Person 2: verification engine](#person-2--verification-engine)
- [Person 3: repair and sponsor infrastructure](#person-3--repair-and-sponsor-infrastructure)

### Person 1 — DuploCloud and integration

```text
You are Person 1 of a three-person ProofRun build: the DuploCloud and integration lead. Begin implementing your assigned work, not just proposing a plan.

Repository: https://github.com/Eman-Gon/ProofRun
Use the existing checkout and your assigned branch/worktree. First read CLAUDE.md, INTEGRATION.md and any applicable repository instructions. Inspect git status and current implementation; preserve other people's changes. Do not assume this chat shares context with the other two chats.

Product: ProofRun checks approved customer-input behavior across a supported Python/Pydantic update, proposes a bounded repair and independently tests it. Core sponsors are DuploCloud for the workflow/UI, Crusoe for worker execution and OpenRouter for repair proposals.

Your first milestone: a Run verification action in DuploCloud invokes the existing runner and displays actual measured evidence. Local execution and a prepared fix are acceptable for this first bridge only when labeled accurately.

Own extensions/proofrun/, src/proofrun/contracts.py, api.py, service.py, __init__.py, corresponding API/contract tests and shared documentation. Follow CLAUDE.md for existing-file ownership. Person 2 owns verification; Person 3 owns repair and infrastructure. Request changes from their owners rather than editing their files.

Work in this order:
1. Check DevKit prerequisites and existing setup. Establish the local portal and a small working extension without replacing this repository.
2. Define the proofrun.v1 types and /v1/runs contract from INTEGRATION.md. Give Persons 2 and 3 a concise contract handoff before dependent implementation.
3. Build the worker adapter and show separate execution, finding and repair states, plus artifact links. Label temporary mocks explicitly.
4. Integrate Person 2's comparison/verifier and Person 3's model client and Crusoe endpoint. Own the bounded orchestration loop; never override the verifier's result.
5. Demonstrate the real round trip and record exact commands, results, revision and remaining limitations.

Keep secrets out of chat and frontend responses. If access is missing, identify the exact blocker and continue independent local work. Keep all optional sponsors deferred until the recorded core gate passes. Do not commit or push unless requested.

Finish each handoff with: changed paths; branch/worktree and revision or diff; interface version; checks actually run; evidence location; mocks or incomplete integrations; and the next action for Persons 2 and 3.
```

### Person 2 — verification engine

```text
You are Person 2 of a three-person ProofRun build: the verification-engine lead. Begin implementing your assigned work, not just proposing a plan.

Repository: https://github.com/Eman-Gon/ProofRun
Use the existing checkout and your assigned branch/worktree. First read CLAUDE.md, INTEGRATION.md and applicable repository instructions. Inspect git status and source before changing anything. Preserve teammate work; these chats do not automatically share context.

Product: ProofRun checks approved customer-input behavior across a supported Python/Pydantic update and independently verifies repairs. DuploCloud starts/displays the workflow; your tests execute on the worker that Person 3 hosts on Crusoe. OpenRouter proposes repairs but cannot decide whether they pass.

Own src/upgrade_demo.py, src/upgrade_sandbox.py, sandbox/upgrade.Dockerfile, demo/upgrade/, their verification tests, and proposed src/proofrun/runner.py and tests/test_proofrun_runner.py. Person 1 owns shared contracts/API/UI; Person 3 owns model and dependency/configuration files. Request shared changes through their owners.

Work in this order:
1. Inspect and reproduce the existing offline example once dependencies and images are ready. Its exit code 1 intentionally confirms the prepared regression; do not treat it as infrastructure failure.
2. Add original-suite execution on repaired code and independent omission, null, string, invalid-object and required-name controls.
3. Implement run_comparison and verify_candidate using Person 1's agreed proofrun.v1 types. Keep reproduced findings separate from repair failures.
4. Verify a narrow repair and reject a deliberately overpermissive candidate. The repairer must not edit your tests, expected outcomes or dependency pins.
5. Preserve exact source, contract, environment, test and candidate identities. Reject missing tests, incorrect versions, stale evidence and unsupported inputs. Maintain isolated, bounded Docker execution.

Deliver measured results and artifacts to Person 1, then run the same checks on Person 3's Crusoe worker. Never label mocked or saved output as a live execution. Continue useful offline work if another person's integration is unavailable. Keep optional work deferred. Do not commit or push unless requested.

Finish each handoff with: changed paths; branch/worktree and revision or diff; interface version; commands and results; evidence location; remaining scope limits; and what Persons 1 and 3 need next.
```

### Person 3 — repair and sponsor infrastructure

```text
You are Person 3 of a three-person ProofRun build: the repair-agent and sponsor-infrastructure lead. Begin implementing your assigned work, not just proposing a plan.

Repository: https://github.com/Eman-Gon/ProofRun
Use the existing checkout and your assigned branch/worktree. First read CLAUDE.md, INTEGRATION.md and applicable repository instructions. Inspect git status and implementation; preserve teammate work. Do not assume the three chats share conversation history.

Product: ProofRun reproduces a supported Python/Pydantic regression and tests a bounded repair. Your core sponsors are OpenRouter for model access and Crusoe for CPU-VM worker hosting. Person 1 owns DuploCloud/API integration; Person 2 owns the independent verifier.

Own proposed src/proofrun/repair.py, config.py, deploy/crusoe/, tests/test_proofrun_repair.py and the designated requirements/configuration files. Coordinate dependency changes with the other owners. Do not edit their implementation or the verifier's acceptance cases.

Work in this order:
1. Check available model and authorized VM access without printing secrets. Do not assume credits, keys or accounts exist. Report exact access blockers and continue local client/deployment preparation where possible.
2. Implement propose_patch against Person 1's agreed proofrun.v1 contract. Send bounded source, approved requirements and measured failure evidence to an explicitly selected OpenRouter model.
3. Return a permitted candidate with its base hash and actual model provenance. Handle invalid output, refusal, timeout and unavailable service explicitly; never disguise a prepared fix as a generated one.
4. Deploy the compatible worker on Crusoe, prepare pinned test environments and provide an authenticated reachable connection to Person 1. Routing model inference to Crusoe is not evidence of worker hosting.
5. Execute Person 2's checks on that worker and retain actual host, environment and test evidence. Keep model credentials outside test containers and browser data.

The repairer cannot edit tests, expected outputs or case dependency pins, and cannot declare a repair verified. Only Person 2's executed checks provide that verdict. Keep the local fallback labeled and all optional sponsors deferred until the core gate passes. Do not commit or push unless requested.

Finish each handoff with: changed paths; branch/worktree and revision or diff; interface version; actual model/worker checks; evidence location; access blockers; and connection details needed by Persons 1 and 2, excluding secrets.
```

## 7. Core completion record and demo

Person 1 updates this from handoff evidence. Writing documents does not complete any gate.

| Gate | Required evidence | Status |
|---|---|---|
| DuploCloud round trip | Actual resource/job and returned measured result | BLOCKED: portal work-email/license/model setup; browser connection refused |
| Crusoe contribution | VM/worker identity, source revision and execution artifacts | BLOCKED: no authorized VM/project/SSH route supplied |
| OpenRouter contribution | Actual proposal operation, model provenance and candidate | BLOCKED: OPENROUTER_API_KEY and explicit PROOFRUN_MODEL absent |
| Reproduction | Approved baseline pass and supported update failure | VERIFIED LOCALLY: native 7/7 baseline, 6/7 update with supported omission error |
| Repair verification | Original suite and independent controls on accepted candidate | VERIFIED LOCALLY for prepared narrow candidate: 14/14; generated candidate pending |
| Bad-fix rejection | Permissive candidate rejected by approved controls | VERIFIED LOCALLY: object-valued nickname rejected in both environments |
| Evidence integrity | Source/case/environment/candidate binding; stale/empty evidence rejected | LOCAL checks pass; complete hashes and exact cases in artifacts; remote evidence pending |
| Fresh demonstration | Another teammate can repeat setup/run; failure states and limitations visible | Local API collector and verifier commands pass; portal demonstration pending |

Two-minute demo: show the approved customer case, start the check in DuploCloud, display baseline/update evidence, show the generated patch and rejected permissive alternative, then the accepted candidate's complete verification report with real worker/model provenance. If execution takes longer, start earlier and label historical output honestly. Do not present a saved run as a new live execution.

Integrated base revision: **f9112a2c947223a95c9467131294e318b0ced3df with uncommitted owned changes**. Evidence/run ID: **run-3a32c31729724b0aa1a0ddb177cf4feb** (local native HTTP comparison). Core status: **IN_PROGRESS; sponsor gates blocked**. When every gate passes, record `CORE_COMPLETE`, then evaluate one optional integration.

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

## 8. Person 1 implementation handoff — September 29, 2026

The active checkout is `/Users/emanschool/ProofRun`, branch `main`, HEAD
`f9112a2c947223a95c9467131294e318b0ced3df`. Work is uncommitted; existing
sponsor-reference edits above and in `OPTIONAL_SPONSORS.md` are preserved.
The three people are using this checkout with non-overlapping ownership.

**Interface baseline: `proofrun.v1`.** `src/proofrun/contracts.py` now defines
`CaseSpec`, `SourceBundle`, `ComparisonEvidence`, `PatchProposal`,
`VerificationEvidence`, and `ProposalUnavailable`. Treat that file as the
constructor source of truth; request schema changes through Person 1.

- Person 2: implement `run_comparison(case_spec, source_bundle)` and
  `verify_candidate(case_spec, source_bundle, proposal)`. `CaseSpec.root` is the
  registered source snapshot; `artifact_dir` is the private run directory.
  SourceBundle contains immutable UTF-8 content and its SHA256. Person 1 validates
  the git revision before dispatch. Person 2 owns the canonical
  `demo/upgrade/contract.json`; its raw-byte SHA256 is `contract_sha256`.
- Person 3: implement `propose_patch(failure_context, attempt)` (attempts 1–2).
  The context contains `source` (including content), `case`, approved `contract`,
  and measured `comparison`. Return a full replacement for only `allowed_path`,
  with base SHA256, rationale and actual model provenance. Raise
  `ProposalUnavailable` with a safe message on unavailable/refused/invalid output.
- Evidence uses JSON data and an internal `artifacts` map from safe public ID to
  absolute file path beneath `artifact_dir`. The service validates paths and
  returns authenticated links, hashes and sizes; filesystem paths are private.
  Expected/executed test IDs are stage-qualified; case records retain `id`,
  `stage`, `status`, environment and actual test identity.
- Bindings use `revision`, `source_sha256`, `contract_sha256`,
  `environment_manifest_sha256`, `tests_sha256`, and, for verification,
  `candidate_sha256`. A proposal has no verdict. Only complete, correctly bound
  verifier evidence may set `verified`; comparison findings survive failed repair.
- `GET /v1/cases/customer-nickname-v1` is an additive authenticated endpoint for
  the extension backend to obtain the registered submission bindings; it adds
  `job_key` before `POST /v1/runs`. Token and worker URL remain server-side.
- Person 3's `.env.example` additions are coordinated: `OPENROUTER_API_KEY`,
  `PROOFRUN_MODEL`, `PROOFRUN_WORKER_URL`, `PROOFRUN_WORKER_TOKEN`,
  `PROOFRUN_ARTIFACT_DIR`, `PROOFRUN_EXECUTION_TARGET`. No new runtime dependency
  is needed for Person 1's standard-library HTTP adapter.

Local preflight: Docker Desktop is running, Compose v5.3.1 is available, both
prepared Pydantic images exist. The official DevKit was cloned separately to
`/Users/emanschool/proofrun-duplocloud-devkit`. `./run.sh --non-interactive`
stopped with `Missing Authentication__LocalAdminEmail`; work-email verification,
license/model setup, portal login and a real portal agent response are pending.
No saved reports or sample calculations count as the DuploCloud round trip.

### Implemented HTTP behavior and local evidence

Person 1 owns `src/proofrun/{__init__,contracts,service,api}.py`,
`tests/test_proofrun_{api,service}.py`, `extensions/proofrun/`, and shared guide
updates. Person 2/3 files were integrated through their documented boundaries,
with their uncommitted changes preserved. No commit, push, PR or production
change was performed.

The HTTP adapter uses the Python standard library. It requires server bearer
authentication for every `/v1/` route; `/health` exposes only schema and health.
Requests are bounded to 64 KiB and reject duplicate JSON keys. Run records use
atomic writes and a process lock. A second active job receives `409 worker_busy`;
an identical job key/body returns the existing record with `200`, changed content
with that key receives `409`, and newly accepted work returns `202`. Restarted
unfinished jobs become `interrupted`. Artifacts are copied into a private,
immutable publication directory, allowlisted by ID and checked against their
recorded SHA256 on every download. There is no arbitrary source URL/path/shell
execution endpoint.

The service snapshots approved fixture inputs and binds HEAD plus the explicit
source-content bundle, contract bytes, test/verifier identities, images and
observed versions. Native evidence must contain all seven registered checks in
each of two environments. The repair loop makes at most two proposal attempts;
changed source/test/verifier/environment/candidate bindings or incomplete
verification cannot become `verified`. Model unavailability preserves the
reproduced finding. The explicit `--runner prepared` adapter invokes the existing
offline demo in a child process and conservatively keeps its repair unaccepted.
Default `--runner native` consumes Person 2's strengthened runner.

Fresh measured evidence (all **local Docker + synthetic inputs**, not portal or
Crusoe evidence):

| Check | Actual result and evidence |
|---|---|
| Native authenticated HTTP + artifact collection | `run-3a32c31729724b0aa1a0ddb177cf4feb`, completed / regression_reproduced / not_requested; 14 cases, 4 hash-checked downloads. `.commit-watch/person1-final-http/collection.json` and `record.json`. |
| Native missing-model behavior | `run-2895281d706440459db4034742976807`, completed / regression_reproduced / unavailable; artifact downloads and identical POST retry passed. `.commit-watch/proofrun-integration/last-http-run.json`. Earlier contract/verifier hashes are retained, not presented as current acceptance. |
| Bounded orchestration with explicitly prepared proposals | `run-e111aa9e76fa401981691f0d29a959f5`: permissive candidate rejected on attempt 1, narrow candidate verified on attempt 2. Real Docker execution; injected proposer is **prepared**, not OpenRouter. `.commit-watch/proofrun-orchestration/last-http-run.json`. |
| Existing-runner adapter | `run-de8cb11a96f24ab0886f44c1acb1fe23`, completed / regression_reproduced / not_requested; `.commit-watch/proofrun-bridge/last-successful-run.json`. First attempt `run-a7bd2273fc8845f2bd7f1cd6a137d7b1` correctly remains setup_failed after a missing snapshot fixture; fixed before rerun. |
| Current native verifier, original suites, bad fix, restrictive umask | `.commit-watch/proofrun-verifier/20260929T195114Z-056ffde1/experiment.json`; full details in `demo/upgrade/PERSON2_HANDOFF.md`. |
| Docker-to-host worker route | A separate unprivileged client container retrieved `/health` at `http://host.docker.internal:8766`. This is a client/network check, not a network-enabled test container or an actual DevKit-container check. |

Current canonical contract hash:
`fd54523ba6cda11d1190758ecfb253519a8e6f1be7bc11130d49e010181e2f3b`.
Clients must fetch registry bindings for each **new** run rather than copy a
historical submission. An existing idempotency key retains its exact original
submission. Full source/test/verifier/candidate/environment hashes are in each
run's artifacts and the Person 2 handoff.

Checks actually run after integration:

```bash
python3.12 -m pytest tests/test_proofrun_api.py tests/test_proofrun_service.py \
  tests/test_proofrun_runner.py tests/test_proofrun_repair.py \
  tests/test_upgrade_demo.py tests/test_upgrade_sandbox.py -q --disable-warnings
# 185 passed, 146 warnings (pytest/unittest compatibility and existing AST warnings).
python3.12 -m src.proofrun.api --help
# Successful.
git diff --check
# Successful.
```

These unit tests use explicit mocks for external transport where appropriate;
the separate Docker/HTTP measurements above establish actual local execution.
An earlier combined run raced an in-progress Person 3 transport refactor and
failed one test. Person 3 corrected the subprocess mock boundary; the stable
combined run above passed. No live model proposal was produced.

### Repeat locally and finish the DuploCloud gate

The native worker is running on `127.0.0.1:8766`. Its generated credential is in
ignored mode-0600 `.env.proofrun`; it was never printed. This file is local setup,
not a shared dependency/configuration replacement. Preserve it and any existing
secrets. To restart after stopping that worker, from this checkout:

```bash
set -a
source .env.proofrun
set +a
python3.12 -m src.proofrun.api --host 127.0.0.1 --port 8766 --runner native
```

In another terminal with the same private worker environment loaded, this
repeatable collector submits a **new** native run and verifies artifact downloads:

```bash
python3.12 deploy/crusoe/run-worker.py --expected-target local \
  --output-dir .commit-watch/my-fresh-http-run
```

Choose a new output directory each time. `--repair` requests an actual configured
provider and intentionally cannot pass the collector's generated-repair gate
without live OpenRouter provenance. Use `python3.12 -m demo.upgrade.verify_offline`
for the separate prepared narrow/permissive experiment.

Complete first-time setup in the sibling official DevKit at revision
`e9f016fd90a67611907fcf673701351669eaa47d`:

```bash
cd /Users/emanschool/proofrun-duplocloud-devkit
./run.sh
```

Its current concrete blocker is the missing work email, followed by the
setup-managed verification/license/password/model steps. The browser also
confirmed `http://localhost:4210` returned connection refused. Once the portal
runs, follow `extensions/proofrun/README.md` to inject only worker URL/token into
the backend, build against the **actual host SDK**, deploy, sign in to
`extension-dev`, and press **Run verification**. Capture the real portal resource
ID, worker run ID, states and artifact download. Until that succeeds, the
DuploCloud round-trip gate remains blocked even when the standalone C# HTTP
client and frontend compile.

Next owners: Person 2's local handoff is integrated; rerun on Person 3's actual
Crusoe worker when supplied. Person 3 must supply authorized VM/project/SSH route
and privately configured OpenRouter key/model, retain host evidence and execute
the same native interface remotely. Person 1 must finish the host-SDK build,
portal deployment and real result display after DevKit access setup. Optional
sponsors remain deferred.

### Extension adapter evidence

The real standalone C# client executed from Docker against the native Python
worker, preserving the server credential outside browser data:

- `run-c43017c17ba3401792dd106dfaab6bdd`: completed / regression_reproduced /
  not_requested, 14 executed cases and four artifacts downloaded with exact
  SHA256/size checks.
- `run-aed1afaa03e04c8d914f8f34e8868e2e`: the same actual comparison with generated
  repair requested, completed / regression_reproduced / unavailable because
  provider configuration is absent. This run records its exact source/contract/verifier and service bindings;
  it is retained as measured historical evidence.

Receipts: `extensions/proofrun/evidence/client-round-trips.json` (ignored local
runtime evidence); complete run records and published artifacts are under
`.commit-watch/proofrun/<run_id>/`. This proves the C# adapter → worker → actual
runner → result/artifact path. It does **not** prove DuploCloud portal initiation
or display: no portal resource was created and the host SDK is still unavailable.

The Angular production build completed; isolated .NET client checks passed 29
assertions. Repeatable commands and final frontend dependency validation are in
`extensions/proofrun/README.md`. The UI provides **Run verification**, an optional
bounded generated-repair request, the three states, actual case rows, provenance,
limitations and authenticated artifact downloads. Portal rendering and the full
C# SDK integration still require the real host deployment.

Final orchestration check after artifact-reference integration: native run
`run-98993ab0a65647a58c64ffff1aed067f`, completed / regression_reproduced /
verified after **prepared** permissive rejection followed by prepared narrow-fix
acceptance. Both repaired matrices executed; the final response contains 28
comparison/latest-verification observations, with each attempt's complete
evidence retained separately. Every exported report/log reference resolves to
its allowlisted, attempt-prefixed artifact ID; SHA256 describes the exported
bytes. Evidence: `.commit-watch/proofrun-final-orchestration/last-run.json`.
No provider or portal contribution is claimed by this test.

Final integration checks: **185 Python tests passed** after adding the prepared
repair-failure preservation and attempt-artifact-reference checks. The final
frontend rebuild and official federation structure check also passed; full host
SDK compatibility remains untested. The extension runbook records four remaining
moderate Angular dependency advisories requiring a coordinated host-compatible
update; both high advisories from the copied sample lock were removed.

### DuploCloud setup continuation

Rechecked the official sibling DevKit: admin email, local license, admin API
token and workspace are still unconfigured. The current `run.sh` resolves email
and obtains its email-verified license before starting services; supplying an
LLM key alone does not satisfy that separate step. An organizer-provided event
setup/license can be used if supplied. No account request was sent without an
approved email.

The ProofRun worker URL/token were copied privately into the ignored mode-0600
DevKit `.env` for durable studio `env_file` injection, with existing values
preserved. All actual configured platform ports are free: UI 4210, studio 60031,
agent 8010, Mongo 27018, Qdrant 6333 and xterm 6061. Existing host port 27017 is
left untouched. `docker compose pull --quiet` and
`docker compose --profile tools pull --quiet builder` were started to prefetch
the official images while awaiting the email; downloading is not platform
startup or license verification. Browser/Computer access was available, but
Computer Use denied access to the native Terminal app; CLI tools remain usable.
