# ProofRun DuploCloud extension

This is a C# `ResourceWorkerBase` extension and Angular Native Federation remote based on the official DevKit worker sample. **It has not yet been built against a running DuploCloud SDK or exercised in the portal.** Local work-email/license/model setup must be completed before that gate can pass.

The **Run verification** button creates one workspace-scoped `ProofRunVerification` resource. Its worker obtains the approved `customer-nickname-v1` submission, binds a stable job key to the workspace/resource identity, persists that exact request, submits it to the Python worker and polls the measured result. The UI displays independent execution, finding and repair states, actual case records, provenance and artifact downloads. Comparison-only is the default. The optional **Request generated repair** checkbox enables at most two attempts using the worker's configured model; unavailable model access never falls back to a prepared candidate. Prepared fixed-code evidence remains a prepared example; it cannot establish generated repair or complete verification.

The browser uses the portal's authenticated HTTP client. Worker origin and bearer token exist only in backend configuration. Artifact requests are restricted to the resource's bound run and its artifact manifest; the backend checks downloaded SHA-256 and size. It constructs the worker route from constrained IDs, ignores upstream artifact URLs and disables redirects. Removing a portal resource does not delete worker evidence or cancel a running job.

## Server configuration

The Python worker implements `proofrun.v1`:

- `GET /v1/cases/customer-nickname-v1`: `{schema_version, case_id, submission, limitations}`.
- `POST /v1/runs`: submit the registered case with a stable `job_key`.
- `GET /v1/runs/{run_id}`: three statuses plus cases, bindings, execution, proposal, limitations and an artifact manifest.
- `GET /v1/runs/{run_id}/artifacts/{artifact_id}`: authenticated raw artifact bytes.

All routes require `Authorization: Bearer PROOFRUN_WORKER_TOKEN`. Configure the **DuploCloud backend process** with `PROOFRUN_WORKER_URL` (an origin without a path) and the same `PROOFRUN_WORKER_TOKEN` used by the Python worker. Do not paste credentials into a resource, browser field or shared artifact.

For durable local setup, place only `PROOFRUN_WORKER_URL` and `PROOFRUN_WORKER_TOKEN` in the sibling DevKit's ignored, private `.env`; its studio service already loads that file. This avoids losing the worker settings when a later normal `./run.sh` recreates containers without the optional compose override. The current local setup has these two values installed without printing them. Preserve all setup-managed values and keep the file mode `0600`.

For the laptop bridge, the backend container uses `http://host.docker.internal:8766`, not its own localhost. The worker must be reachable through that route; verify connectivity from the actual studio container. The checked-in `compose.worker.example.yaml` adds server-only environment injection to the sibling DevKit without replacing its generated settings:

```bash
# Run from the sibling DevKit after its normal setup. Export the two ProofRun settings from
# ignored local configuration first; the compose override never contains their values.
docker compose -f docker-compose.yml \
  -f ../ProofRun/extensions/proofrun/compose.worker.example.yaml up -d duplo-ai-studio
```

Use authenticated HTTPS for a remote/Crusoe worker. A remote URL or `target=crusoe` label alone is not evidence of execution on Crusoe.

## Build and deploy

Keep the official DevKit at `../proofrun-duplocloud-devkit`, or set `PROOFRUN_DEVKIT_DIR` to it. This repository's source remains under `extensions/proofrun/`; do not run the DevKit adoption script over ProofRun.

```bash
# From the ProofRun root; requires Docker and a running, authenticated DevKit SDK feed.
./extensions/proofrun/scripts/build.sh

# After successful build, from the sibling DevKit root:
./scripts/deploy-extension.sh ../ProofRun/extensions/proofrun/dist/extension.zip
```

The wrapper delegates to the official builder with an explicit volume for this external extension path. The official default script otherwise rejects paths outside its checkout. It also stages the DevKit UI library into an ignored vendor directory; `package-lock.json` fixes its integrity. The host SDK version is obtained from the running platform and pinned by its build script; no placeholder SDK is used to claim a successful backend build.

Frontend-only compilation does not require the portal:

```bash
./extensions/proofrun/scripts/build.sh --frontend
```

The wrapper uses Docker's Node 22 image because the sample's Angular 22 requires at least Node 22.22.3. If a complete native .NET 8/Node toolchain exists, `build.sh --native` delegates directly to the official builder.

After deploying, select `extension-dev`, open **DevOps → ProofRun**, press **Run verification**, and wait for the actual worker run ID and separate states. Download a measured artifact and confirm its source/contract/environment bindings. A green portal lifecycle status only means dispatch/polling finished. It cannot replace the three worker statuses or the complete verification artifact.

## Validation and limitations

Run the isolated .NET 8 client boundary checks with Docker:

```bash
./extensions/proofrun/scripts/test-client.sh

# With worker URL/token already exported from ignored local configuration:
./extensions/proofrun/scripts/test-client.sh --live
# Optionally exercise actual configured repair access (may report unavailable):
./extensions/proofrun/scripts/test-client.sh --live --repair
```

Default client checks use a fake HTTP transport and exercise real client code without SDK stubs. `--live` submits a fresh job to the actual Python worker, polls its result, and downloads/hash-checks every artifact; it does not create a portal resource. The complete round-trip gate still requires a real portal resource, worker run, measured case records and artifact retrieval. Frontend compilation and standalone HTTP-client checks cannot satisfy the portal gate.

Checks actually run on September 29, 2026 (uncommitted extension changes on ProofRun `main`, HEAD `f9112a2c947223a95c9467131294e318b0ced3df`):

| Check | Measured result |
|---|---|
| Angular production build | Passed after dependency updates; 94.66 kB initial app chunks; Native Federation output generated under `frontend/dist/`. |
| Official `verify-remote-federation.js` | Passed manifest/name/exposed-module/output-layout checks. A running host entry was unavailable, so host compatibility was not checked. |
| Isolated .NET 8 client | Compiled; 29 boundary assertions passed using a fake transport. |
| Actual client → local worker, comparison | `run-c43017c17ba3401792dd106dfaab6bdd`: `completed` / `regression_reproduced` / `not_requested`; 14 case records, 4 downloaded artifacts matched their recorded hashes and sizes. |
| Actual client → local worker, repair requested | `run-aed1afaa03e04c8d914f8f34e8868e2e`: `completed` / `regression_reproduced` / `unavailable`; 14 case records, 4 artifact hashes/sizes checked. No model proposal was fabricated. |
| Full SDK bundle build | Blocked at preflight: no running DuploCloud SDK feed. SDK resource/service/worker/controller classes remain uncompiled against that host. |
| Portal deployment / UI run | Not executed; requires work-email/license/model setup and a running local portal. |
| Shell syntax / whitespace | Passed `bash -n` for both scripts and `git diff --check`. |

Nonsecret local receipts are at `evidence/client-round-trips.json` (ignored). Worker evidence is at the ProofRun repository's `.commit-watch/proofrun/<run_id>/published/`. These are actual local executions of synthetic fixtures, not Crusoe evidence.

The copied official sample lock initially reported 8 moderate and 2 high npm advisories. Six targeted transitive updates removed both high advisories and four moderate advisories. The final `npm audit` still reports **4 moderate advisories** in the Angular 22.1.0 dependency chain (`@angular/common`, `@angular/forms`, `@angular/platform-browser`, `@angular/router`, originating from `HttpTransferCache`). A coordinated Angular 22.1.1 update encountered strict peer resolution errors; the official sample's Angular versions were preserved without `--force` or `--legacy-peer-deps`. The remaining Angular update needs validation with the actual host. The full audit output is saved locally at `frontend/npm-audit.local.json` (ignored).

The bridge permits one registered synthetic case with optional bounded generated repair. Polling is bounded to 20 minutes; a timeout retains the last worker snapshot and reports a bridge error without inventing a worker verdict. Backend retries reuse the same submission/job key. Starting another verification intentionally creates a new resource. The worker API remains authoritative for concurrency, approved bindings, execution and verification.
