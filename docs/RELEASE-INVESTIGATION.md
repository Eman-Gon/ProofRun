# Release investigation operator guide

This path investigates a configured HTTP product at two exact Git commits. It is separate from the prepared `customer-nickname-v1` fixture: an agent can inspect included source and diffs, choose previously unspecified synthetic inputs and request sequences, examine their results, and propose bounded application changes. The runtime decides whether behavior changed and whether a repair candidate passed its checks.

The first supported class is **HTTP response compatibility**. A confirmed finding means a stable response difference under an operator-approved `preserve_response` requirement. Its cause remains a hypothesis unless separately established; inspecting a two-commit diff does not prove which intermediate commit introduced the issue. This is not exhaustive repository testing or general discovery of arbitrary bugs.

## Evidence status

This guide describes the implementation and its configuration contract. Injected model callbacks and mocked Docker/HTTP tests exercise policy and failure handling; they are not a live provider investigation, a real product run, or a staging deployment. Example configuration validation does not establish that its repository, images, or application are runnable. The DuploCloud gateway must be verified against the actual workspace authorization SDK and a running worker before claiming an end-to-end portal run.

The separate [measured validation record](RELEASE-INVESTIGATION-VALIDATION.md) documents an actual authenticated API, OpenRouter and Docker run that discovered two regressions and verified a generated repair in 232.09 seconds on a synthetic product.

No live provider call, application execution, or staging replay is implied by these examples. Record actual run IDs, revision/image hashes, artifacts, and execution results when performing that verification.

## Configure a target

Start with [targets.example.json](../deploy/release/targets.example.json). The repeated-digit commits and image hashes are schema-valid placeholders, not existing releases. Replace them, the repository path, workspace ID, commands, and application paths before running.

The worker needs Python 3.12 with this repository's dependencies, Git, and access to a Docker daemon. Maintain an operator-controlled **local Git repository or mirror** containing both commits. The worker reads tracked Git objects; it does not clone an arbitrary user-supplied URL, fetch missing commits, run checkout filters, or follow submodules. Register each approved lowercase 40-character commit in `images` with a prebuilt immutable Docker image ID (`sha256:…`) or repository digest (`registry/name@sha256:…`). Both commits must differ.

Images must already be available to the worker's Docker daemon: execution uses `--pull never`. Build and verify the dependency environment for each revision ahead of time. The image-to-release mapping is operator-supplied; a recorded image hash does not independently prove that it contains the intended dependencies. Images must contain no secrets or proxy credentials and must not declare persistent volumes. Metadata checks do not scan all image contents for secrets.

Application and test commands run against the mounted snapshot, not an installed copy of the application baked into the image. Ensure imports and startup scripts actually use that source. Images must support UID/GID `65534:65534`, a read-only root filesystem and source mount, and temporary writable `/tmp` and `/data`. Both tests and HTTP applications use Docker's `none` network. A separate trusted HTTP collector container joins the application's isolated network namespace to observe its loopback HTTP responses; the application does not publish a host port. Dependencies, synthetic seed data, and any compilation needed by the commands must fit this environment; runtime package installation and arbitrary external dependencies are unsupported.

`runtime.collector_image` is mandatory and must identify a separate, trusted, prebuilt immutable image with Python available. The worker mounts its collector code into that image and records the actual image and collector-code hashes. It never silently uses the untrusted application image as its HTTP evidence collector. The collector image must also be available locally and secret-free.

| Setting | Operator responsibility |
| --- | --- |
| `workspaces` | Restrict access to the intended workspace scope IDs. An omitted or empty list permits every authenticated scope; the worker token is therefore a privileged backend credential. |
| `requirements` | Supply 1–20 unique IDs, descriptions, `kind: preserve_response`, literal endpoint path prefixes, and approved HTTP methods. Do not supply a predeclared failing request, defect location, expected response, or repair. |
| `runtime.command` | Fixed argv array that starts the HTTP application; it must listen on `0.0.0.0` at the configured port. |
| `runtime.collector_image` | Immutable ID/digest of a trusted Python image for HTTP observation from a separate container in the application's isolated network namespace. |
| `runtime.test_command` | Fixed argv array for the original suite. It runs the baseline suite against both revisions; candidate and repaired source also run their current release suite. |
| `runtime.workdir`, `port`, `health_path` | A snapshot-relative directory, port 1024–65535, and health endpoint returning a 2xx response. Startup allowance is at most 60 seconds and shares the run deadline. |
| `runtime.env` | Bounded, non-secret synthetic settings. Worker/model credentials are not passed to application containers. |
| `test_paths` | Patterns covering all baseline test files, harness files, and relevant acceptance fixtures that must stay frozen. Candidate versions of matching files are removed and baseline copies restored. At least one baseline file must match. |
| `test_success_pattern` | A regex matching the suite's real nonzero-test completion message. A zero exit alone is insufficient. The example matches unittest's `Ran N tests in …s` summary for N ≥ 1. |
| `repair_paths` | Narrow allowlist of existing application source paths. Tests, dependency locks, requirements, configuration, and Dockerfiles remain protected. An empty list disables repair. |
| `exclude_paths` | Additional repository-relative `fnmatch` patterns. Omitted source and tests are outside the result's evidence. |

The candidate is checked twice: once with the protected baseline tests restored, and once with its own current tests. Both suites must pass. Repairs run the same pair against the repaired source. This preserves prior acceptance requirements while also checking tests added in the release. Complete execution, a zero exit code, and the configured nonzero-test completion marker are required for each passing suite; incomplete execution or cleanup cannot count as a pass.

The snapshot reader always excludes common environment/credential paths, key files, dependency directories, and virtual environments. It currently bounds the tracked tree to 6,000 entries, included content to 32 MiB total, and an individual file to 2 MiB. Symlinks and submodules must be explicitly excluded or preparation fails. Recorded manifests identify included files, hashes, and exclusions. This allowlist/exclusion mechanism is not a complete secret scanner; prepare a suitable source mirror before making its content available to the agent.

Choose response-preservation contracts only for behavior that should remain identical. The observer compares HTTP status plus parsed JSON or body text; it does not assess visual UI behavior, performance, or arbitrary business semantics. Generated IDs, timestamps, and unstable ordering can make comparisons inconclusive. Intentional response changes need an approved contract change rather than being silently reinterpreted as compatible.

## Start the separate worker

Provide [worker.env.example](../deploy/release/worker.env.example) through private service environment configuration. The Python entry point does **not** automatically source an environment file.

- `PROOFRUN_WORKER_TOKEN`: the shared backend-only bearer secret, at least 32 printable non-whitespace characters. Use the same variable on the release worker and its trusted callers; never send it to the browser.
- `PROOFRUN_RELEASE_TARGETS`: the absolute path to the target registry.
- `PROOFRUN_RELEASE_ARTIFACT_DIR`: a persistent, private output directory owned by this worker. Only one worker may own a directory.
- `OPENROUTER_API_KEY` and `PROOFRUN_MODEL`: required for live investigation even when repair is disabled. The model must be an explicit provider/model ID. Missing configuration is reported as unavailable, without a prepared agent or repair fallback.

From this repository's root, with those environment values already supplied:

```bash
python -m src.proofrun.release_api --host 127.0.0.1 --port 8767
```

The existing fixture worker remains on port 8766. Configure the DuploCloud C# backend with `PROOFRUN_RELEASE_WORKER_URL` pointing to this listener and the same `PROOFRUN_WORKER_TOKEN`. A backend on another host needs a reachable private URL; its own loopback does not reach this worker. The listener itself provides bearer authentication, not TLS termination. Keep it behind the trusted backend/private service boundary.

The service persists a single-worker queue, with at most 16 queued/running requests. A restart marks unfinished runs failed instead of presenting them as completed. The execution budget starts when investigation begins, not while a request waits in the queue.

## Manual, CI event, and CLI entry points

Every worker HTTP endpoint requires `Authorization: Bearer <worker-token>`. `X-ProofRun-Scope` identifies the authorized workspace; the default is `operator`. Trusted backend/CI code chooses this scope. A browser must use the workspace gateway, not hold the worker token or choose another workspace's scope.

| Worker route | Purpose |
| --- | --- |
| `GET /v1/release-targets` | List target configuration permitted for the calling scope. |
| `POST /v1/release-runs` | Submit a manual investigation; returns a run with HTTP 202. |
| `POST /v1/release-events` | Submit the same request from CI/deployment tooling, with a required `event_id`. This does not deploy anything. |
| `GET /v1/release-runs/{id}` | Read execution status and recorded results. |
| `GET /v1/release-runs/{id}/artifacts/{artifact}` | Retrieve a listed artifact after the stored bytes are checked against its recorded hash. |

POST accepts bounded JSON, `Content-Type: application/json`, and `Content-Length`. Use a request file such as the following, replacing both placeholder commits:

```json
{
  "target_id": "customer-service",
  "baseline_revision": "0000000000000000000000000000000000000000",
  "candidate_revision": "1111111111111111111111111111111111111111",
  "benefit": "The client needs the release's improved customer import workflow.",
  "budget_seconds": 300,
  "repair": false,
  "event_id": "customer-service-release-001"
}
```

Budgets are integer seconds from 180–600; the portal offers 5 or 10 minutes. Benefits are at most 2,000 characters. Event IDs contain 1–80 letters, digits, underscores, or hyphens, beginning with a letter or digit. A repeated event in the same scope returns the same run only if its normalized request and target contract match; otherwise it returns 409. Omitting an event ID creates a fresh manual run.

The workspace gateway contract is:

```text
/v1/aiservicedesk/user/data/workspaces/{workspaceId}/environment/extensions/releaseinvestigations
  GET  /targets
  POST /
  GET  /{id}
  GET  /{id}/artifacts/{artifactId}
```

The new frontend routes are `releases` and `releases/:id` within the extension. It preserves the last observation on refresh failure and stops automatic polling after a bounded interval. A saved result or stopped poll does not establish that the worker just reran or stopped. Listed experiments, repair diffs, and other artifacts can be downloaded through the authorized gateway. The gateway checks their recorded hash and size; the browser checks them against its selected run before saving an inert file, with a 16 MiB limit. No upstream artifact URL is followed. Downloading a diff does not apply it.

For a direct local-operator run, use a **new, nonexistent** artifact directory each time:

```bash
python -m src.proofrun.release_api \
  --targets /etc/proofrun/release-targets.json \
  --once /etc/proofrun/release-request.json \
  --artifacts /var/lib/proofrun/once/release-001
```

`--once` bypasses the HTTP service and workspace scoping as a trusted local operator. It still validates the request, requires the configured model for live investigation, and does not deploy anything. It exits 0 for `update` and 2 for other recommendations; do not equate a nonzero exit with an absent evidence directory.

## Optional staging replay

Omit `staging` to leave staging unmeasured. To enable it, add an operator-owned block like this; the example target above includes POST requirements, so GET-only staging may leave those experiments incomplete:

```json
{
  "base_url": "https://staging.example.invalid",
  "revision_path": "/version",
  "revision_key": "revision",
  "allowed_methods": ["GET", "HEAD"],
  "allow_synthetic_writes": false,
  "headers_env": {"Authorization": "PROOFRUN_STAGING_AUTHORIZATION"}
}
```

The identity endpoint must return HTTP 200 and a JSON `revision` matching the exact candidate commit before replay begins. Header values come from server environment variables; do not put credentials in the target file, URL, agent prompt, or frontend. Redirects are rejected.

For write probes, explicitly include the method and set `allow_synthetic_writes: true` only for an approved synthetic staging scope. Staging requests are real HTTP requests against that environment; they do not have the disposable container's reset behavior. Staging replay compares against the **original candidate's** measured responses, not an undeployed repair. A verified staging replay covers only that observed revision and those requests.

## Interpret the result

The agent has bounded source tools and experiment/repair actions, not arbitrary shell access. The current limits are 30 agent decisions, eight experiments, up to 12 requests per experiment, and two repair candidates within the shared execution deadline. The runtime repeats each experiment twice on each revision; unstable or incomplete observations remain inconclusive. Model summaries cannot assign runtime verdicts.

One JSON syntax correction is allowed per investigation when a complete live provider response includes valid operation provenance and a parser location. The rejected response executes no action and stays recorded as failed. A fresh decision consumes the existing time and decision budgets and is linked to the rejected step. Duplicate keys, nonfinite values, invalid action arguments, policy rejections, provider failures, refusals, and truncated responses remain terminal; none receives this correction. Existing observations and repair evidence retain their measured statuses even if the agent cannot finish.

Each experiment belongs to one requirement. With two distinct experiments required per requirement, the current eight-experiment cap permits complete `update` coverage for at most four requirements in one run, even though the registry accepts up to 20. Larger configured scopes remain incomplete under this budget.

`update` requires completed investigation, passing frozen and current suites, source inspection, and stable preserved experiments covering every configured requirement. Each requirement needs at least two distinct request sequences, including at least one baseline 2xx response; repeating the same sequence under a different name does not add coverage. There must be no incomplete experiments, configured staging must verify, and a release benefit must be stated. Source inspection means the agent has read source or inspected a file-specific diff; it is not proof of a root cause.

`skip` retains a confirmed compatibility regression or a candidate-suite failure with a passing baseline. Other outcomes use `postpone`. Requirements exercised is a coverage count, not a passing-test count. Budget expiry, unavailable models, missing evidence, and zero-test executions cannot create a pass. Findings show the worker's cause hypothesis and `cause_status` separately from their reproduced behavior status.

A repair replaces only approved existing application files. Verification retains baseline tests and already measured experiments, runs the current release suite, requires independently passing controls and the same distinct-input requirement coverage, and compares repaired responses with the original baseline. Its verification records the frozen experiment-set hash. If later investigation expands that set, an earlier `verified_candidate` becomes `verification_stale`; the earlier result cannot claim coverage of newly added experiments. A currently verified candidate remains reviewable local output: it does not change the finding against the original release, authorize deployment, or establish staging readiness.

Keep `result.json`, `source-manifests.json`, `experiments.json`, `repairs.json`, `trace.json`, and any repair diffs together. They record the scope, revisions, hashes, observations, model provenance, and limitations needed to evaluate a recommendation. Use those artifacts to distinguish actual execution from injected-model tests and to decide what further client-workflow or staging validation remains.
