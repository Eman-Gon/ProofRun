# ProofRun DuploCloud extension

This is a C# `ResourceWorkerBase` extension and Angular Native Federation remote based on the official DevKit worker sample. **Extension 0.2.1 is built against the actual host SDK 1.0.6 and deployed, with running-host frontend compatibility verified.** A real DuploCloud resource completed a local comparison and returned four hash-checked artifacts. Embedded `proofrun.evidence-graph.v1` graphs are verified through the fixture and release proxies. Browser sign-in and the visible button-triggered demonstration remain pending. Email/license and private OpenRouter configuration are complete; a pinned MongoDB 7.0.43 override resolved the local 7.0.14 binary crash.

The Run verification action (labeled **Check this release** in deployed version 0.2.1) creates one workspace-scoped `ProofRunVerification` resource. Its worker obtains the approved `customer-nickname-v1` submission, binds a stable job key to the workspace/resource identity, persists that exact request, submits it to the Python worker and polls the measured result. The UI displays independent execution, finding and repair states, actual case records, provenance and artifact downloads. Comparison-only is the default. The optional **Try a repair after a reproduced break** checkbox enables at most two attempts using the worker's configured model; unavailable model access never falls back to a prepared candidate. Prepared fixed-code evidence remains a prepared example; it cannot establish generated repair or complete verification.

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

## Selected optional integrations

**Similarweb:** Select a verification, enter the customer's website domain and a completed month, then press **Fetch customer context**. The authenticated portal route `POST {id}/research` checks the resource's workspace and calls the fixed worker endpoint `POST /v1/customer-research`. The backend derives `request_id` from the workspace, resource and browser nonce, validates the returned identity/domain/period, and returns only allowed metrics, dates, sources and availability details. Similarweb's API key belongs only in the Python worker's `SIMILARWEB_API_KEY` setting.

Each explicit fetch covers one month of estimated worldwide desktop/mobile visits. A transport retry reuses its nonce; polling or refreshing verification never fetches research. The page clearly distinguishes completed research, missing provider data and unavailable access. Research is kept in the current page session under the workspace/resource identity and included in **Download meeting brief**. Reloading the page clears that UI context; it does not fetch paid data automatically. Research never changes test requirements, execution, findings or repair acceptance.

**BAND:** When the worker enables BAND, run details and the meeting brief show its actual mode, handoff status, room and handoff identity. `waiting`, `passed`, `blocked` and `unavailable` are coordination states; `passed` describes the delivered verifier handoff and does not independently establish an accepted repair. The separately reported repair verdict remains authoritative. Mock coordination stays labeled `mock`. Room IDs are displayed as text; the extension does not invent room links or expose agent API keys.

The September 29 optional-integration extension checks passed **20 frontend tests**, **53 standalone .NET HTTP-client assertions** with fake transport, and the Angular production build in the existing Node 22 Docker image. They do not establish a live Similarweb lookup, BAND room delivery, or portal deployment. The SDK-specific controller/service still requires the real host build.

Run the frontend behavior checks with Node 22.22.3+ or Node 24:

```bash
cd extensions/proofrun/frontend
node --experimental-strip-types --test tests/*.test.mjs
```

## Build and deploy

Keep the official DevKit at `../proofrun-duplocloud-devkit`, or set `PROOFRUN_DEVKIT_DIR` to it. This repository's source remains under `extensions/proofrun/`; do not run the DevKit adoption script over ProofRun.

For a **manual licensed portal setup** while model access is pending, start the official core portal/studio services with their real license and private authentication settings first. Once `/healthz` is ready, the local helper follows the official `run.sh` login/token/workspace/access API sequence:

```bash
python3 extensions/proofrun/scripts/bootstrap-portal.py
# Optional when the DevKit is elsewhere:
python3 extensions/proofrun/scripts/bootstrap-portal.py --devkit-dir /absolute/path/to/devkit
```

It reads the existing sibling `.env`, reuses a valid admin token or creates `dev-kit-admin`, and creates/adopts `extension-dev`, its permission set and the current local user's permission group. It never revokes existing tokens, overwrites a conflicting access grant, prints credentials, starts containers, or changes license/model settings. Returned token and IDs are saved atomically into the same ignored `.env` with mode `0600`, preserving unrelated settings. This is partial manual portal setup; it does not claim a working model/agent or successful completion of the full DevKit setup. The helper passed 12 assertions against an isolated fake API, covering repeated runs, secret-safe errors, redirects, token preservation and private configuration writes. Its actual portal invocation also completed, creating/adopting workspace `6abc27f95171566ca109a084` and its current user access.

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

After deploying, select `extension-dev`, open **DevOps → ProofRun**, press **Check this release**, and wait for the actual worker run ID and separate states. Download a measured artifact and confirm its source/contract/environment bindings. A green portal lifecycle status only means dispatch/polling finished. It cannot replace the three worker statuses or the complete verification artifact.

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

Subsequent checks on September 29, 2026, base `41fd0b6` plus current uncommitted changes:

- Actual SDK backend publish passed with zero errors and three analyzer warnings. All ten SDK packages are 1.0.6, extracted read-only from the running studio image `dev-1.0.6-45708c38`; the package repository commit matches `45708c38f4baed4088e75181a7ef7de26badba5b`. `evidence/host-sdk-build.json` records source and DLL hashes. No SDK source fix was required.
- The actual studio container reached the authenticated Python worker registry with HTTP 200; `.commit-watch/person1-duplo-portal/studio-worker-connectivity.json` records the route and host image.
- Real OpenRouter repair run `run-8648c92ee14844b8b61769810d459839` completed / regression_reproduced / verified. The first generated `anthropic/claude-sonnet-5` proposal passed all 14 repaired original-suite/control checks; all ten artifact downloads matched their hashes and sizes. Evidence is `.commit-watch/person1-openrouter/20260929T210037Z-6ccd3c54/`. This API run was local and was not initiated through the portal.
- The full official bundle build subsequently passed against the running host SDK feed. Version 0.1.0 deployed with HTTP 200, and the official federation checker passed against the actual host remote entry. Bundle SHA-256: `8a9b18a35ae829afa4ac58f9d3ecabb02fdc71dec36e3a8377a9f14730a752f5`. The frozen source/bundle and receipts are in `.commit-watch/person1-duplo-portal/`; concurrent release-investigation changes are not included.
- Actual DuploCloud resource `6abc29855171566ca109a335` completed worker run `run-0cbd8aa9e0734ce48dd09e45e546c268`: completed / regression_reproduced / not_requested, baseline 7/7, updated 6/7. Four artifacts retrieved through the deployed portal proxy matched hashes and sizes. This resource was created through the API; browser initiation/display remain pending. The earlier timed-out resource is retained as failed evidence.
- Version 0.2.0 subsequently built and deployed the stable release-investigation and failure-research interfaces, passing the actual-host federation check. Bundle SHA-256: `149d5871a1e01af1ed605d7ed21597f2bd8eb715752e88e8fa4049a97ba1ced7`. Evidence and the frozen source are at `.commit-watch/person1-duplo-release/`.
- Version 0.2.1 deployed the embedded evidence graphs and passed an independent actual-host federation check. Bundle SHA-256: `85ff545718d7a5e0a4cb48a71a1899245ede4e1f7b4eda6df55c14653094e211`. The deployed fixture/release graph proxies returned measured AuraDB graph data and rejected unauthenticated and foreign-workspace requests. Receipts are in `.commit-watch/neo4j-graphs/`; final worker restart and direct graph checks are in `.commit-watch/neo4j/per-fix-graphs/`. Portal visual verification remains pending sign-in.
- The new workspace release gateway accepted `release-c3ad223367bc3fa77da9cdd5f79a7c65f4552d94`. Local execution reproduced two regressions and verified a generated candidate; all six proxy artifacts matched hashes and sizes. Its final model decision failed strict JSON parsing, so agent status remains failed and the original-release recommendation remains skip. This measured failure is retained. Browser initiation/rendering, non-admin authorization and a second-workspace portal check remain unverified; see INTEGRATION section 11 for exact scope.

The copied official sample lock initially reported 8 moderate and 2 high npm advisories. Six targeted transitive updates removed both high advisories and four moderate advisories. The final `npm audit` still reports **4 moderate advisories** in the Angular 22.1.0 dependency chain (`@angular/common`, `@angular/forms`, `@angular/platform-browser`, `@angular/router`, originating from `HttpTransferCache`). A coordinated Angular 22.1.1 update encountered strict peer resolution errors; the official sample's Angular versions were preserved without `--force` or `--legacy-peer-deps`. The remaining Angular update needs validation with the actual host. The full audit output is saved locally at `frontend/npm-audit.local.json` (ignored).

The bridge permits one registered synthetic case with optional bounded generated repair. Polling is bounded to 20 minutes; a timeout retains the last worker snapshot and reports a bridge error without inventing a worker verdict. Backend retries reuse the same submission/job key. Starting another verification intentionally creates a new resource. The worker API remains authoritative for concurrency, approved bindings, execution and verification.
