# Person 3 handoff — repair client and Crusoe worker preparation

Owner: Person 3. Shared checkout `/Users/emanschool/ProofRun`, branch `main`, base revision `f9112a2c947223a95c9467131294e318b0ced3df`. All implementation changes remain uncommitted. No commit, push, production change or VM provisioning was performed. Person 1/2 changes and existing guide edits were preserved.

## Update: configured-model parameter compatibility

September 29, 2026, follow-up at base revision `41fd0b622ae3e54adb7b6304952afc1cd7ad7618` (changes below uncommitted). This update supersedes the earlier missing-key/model inventory: Person 1 reports that the privately configured OpenRouter key authenticates with HTTP 200 and the explicit model is `anthropic/claude-sonnet-5`. Person 3 did not read or expose that credential.

Person 1's live endpoint-catalog inspection reported that all ten endpoints for the selected model omit temperature support. The client had always sent `temperature: 0` while requiring support for all request parameters. [OpenRouter parameter documentation](https://openrouter.ai/docs/api_reference/parameters) identifies temperature as optional and says omitted sampling parameters use provider defaults. [Provider routing documentation](https://openrouter.ai/docs/guides/routing/provider-selection#requiring-providers-to-support-all-parameters) says `require_parameters: true` excludes endpoints that do not support a supplied parameter.

The client now omits the optional temperature parameter. It retains the exact configured model, disables fallbacks, requires support for the remaining parameters, and retains strict JSON schema, output/time/attempt bounds, provenance and independent verification. No acceptance criteria or verifier files changed. The regression test simulates the selected model's endpoint rejecting temperature and checks the supported request, unchanged model and retained controls.

The prior native run `run-ed679005e3314d069ec86169b3323ddc` reproduced the regression but reported repair unavailable. Its historical provider error body/status was not retained; parameter incompatibility is a supported diagnosis, not a claim that a specific HTTP error was observed. Owned suite: **40 tests passed** using `python3.12 -m pytest tests/test_proofrun_repair.py -q --disable-warnings`. Log: `.commit-watch/person3/repair-temperature-tests.txt`; source-bound receipt: `.commit-watch/person3/temperature-compatibility.json`. No live proposal or worker restart was performed by Person 3 for this fix. Person 1 owns restarting the worker and running the actual integration after the stable handoff.

## Delivered interface and files

Uses Person 1's existing `proofrun.v1` types in `src/proofrun/contracts.py` and Person 2's native `run_comparison` / `verify_candidate`. No competing contract or verifier was created.

Person 3 changed:

- `src/proofrun/repair.py`: `propose_patch(failure_context, attempt) -> PatchProposal`; `OpenRouterRepairClient` supports explicitly labeled mock transport for tests. Failure raises the shared `ProposalUnavailable` with a safe message.
- `src/proofrun/config.py`: `RepairConfig.from_env()` and `WorkerConfig.from_env()`. Configuration does not implicitly read files. Secret fields are excluded from repr. Worker tokens require at least 32 ASCII characters. Crusoe requires an explicit worker ID.
- `tests/test_proofrun_repair.py`: bounded-context/output, provenance, secret-handling, configuration, retry and deadline tests; network/subprocess guards fail closed unless explicitly mocked.
- `.env.example`: approved ProofRun/OpenRouter settings added; legacy settings preserved.
- `requirements-worker.txt`: existing pins `requests==2.34.2` and `packaging==26.3`. The native worker needs no inherited model/memory providers or host Pydantic installation. Prepared legacy CLI mode still uses legacy requirements.
- `deploy/crusoe/`: deployment runbook, systemd unit, private service configuration template, installer, SSH tunnel, image preparation, host evidence, authenticated route/run/artifact collectors, local validation artifacts and this handoff.

Requirements/configuration changes were recorded through Person 1 before editing. Person 1 owns shared documentation/completion gates.

The client accepts the source/case/comparison context enriched by Person 1's service, plus `requirements` and the exact `requirements_json` bytes. It validates source, contract and revision bindings before sending a request. Only `demo/upgrade/app.py` is allowed. The prompt contains bounded approved requirements, source and measured case failures; artifact paths and arbitrary context are excluded. Optional second-attempt feedback is restricted to a bound, executed rejected verification.

Limits: attempts 1 or 2 (the service controls the overall loop), source/candidate 16 KiB each, context 48 KiB, response 96 KiB, rationale 2 KiB, output 4096 tokens, and 45 seconds total by default (configuration ceiling 60). One HTTP request per attempt; no automatic retry or prepared fallback. A disposable HTTP process enforces the total deadline, including trickled headers/responses. Credentials enter that process through private stdin, not argv, inherited environment or files. Model requests remain outside test containers.

Strict JSON validation rejects duplicate keys, nonfinite values, unsupported fields, refusal, truncation, tool requests, wrong paths/base hashes, unchanged or invalid Python, unsupported imports and credential echoes (including JSON-escaped echoes). A proposal supplies no verdict. Only Person 2 applies it in a disposable copy and decides verification.

Provenance records requested/returned model IDs, actual completion operation ID, live/mock mode, attempt, source revision, contract/candidate hashes, request/response hashes, timestamp and elapsed time. No raw provider error bodies or authentication headers are retained.

## Access outcome

The checked process environment and repository `.env` contain no `OPENROUTER_API_KEY` or `PROOFRUN_MODEL`; `.env` did not exist at the access check. No Crusoe CLI, conventional Crusoe config, project/VM identifiers or Crusoe-named SSH entry was found. This checks configured access, not whether an account exists elsewhere. Exact inventory and timestamps: `.commit-watch/person3/access.json`.

Missing independently:

1. An OpenRouter API key in private server configuration and an explicit model ID supporting the requested structured-output parameters. Authentication, usable credit and actual model availability cannot be established without those values.
2. An authorized Crusoe account/project with available CPU quota or an existing VM, its actual provider identity, and an authorized SSH host/user/key route. No remote endpoint, VM identity, OS observation or remote execution can currently be supplied.

No actual OpenRouter-generated candidate exists. During a concurrent intermediate test run, an obsolete test mock did not cross the new HTTP subprocess boundary and sent a synthetic test credential; OpenRouter returned authentication failure. No real key was involved and no completion was produced. That test was replaced and fail-closed network/subprocess guards were added. The final 39-test suite makes no live provider calls. Event-specific OpenRouter Notion documentation was also inaccessible; event credit/eligibility is unverified.

The client follows the official [OpenRouter API quickstart](https://openrouter.ai/docs/quickstart), [bearer authentication](https://openrouter.ai/docs/api_reference/authentication) and [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs). Once an actual key exists, [GET /api/v1/key](https://openrouter.ai/docs/api_reference/limits) can check key limits privately; only a successful proposal establishes model contribution.

## Checks actually executed

| Check | Actual result | Evidence |
|---|---|---|
| `python3.12 -m pytest tests/test_proofrun_repair.py -q --disable-warnings` | 39 passed; mocked HTTP/model completions | `.commit-watch/person3/repair-tests.txt` |
| Total-deadline test | Local ten-second sleeping child killed and reaped after approximately 1.01 seconds under a one-second limit; no HTTP | `tests/test_proofrun_repair.py` |
| Fresh minimal venv + `pip install -r requirements-worker.txt` | API, service, native runner and repair imports passed with requests 2.34.2 / packaging 26.3 | `.commit-watch/person3/runtime/` |
| Shell/Python deployment syntax, invalid-port/host/non-Linux checks | Passed | `deploy/crusoe/evidence/validation.json` |
| Real local HTTP route | Health/valid token 200; missing/wrong token 401, four checks passed | `.commit-watch/person3/private-route.json` |
| Real local native worker, repair requested but no model access | Completed; regression reproduced; repair unavailable; 14 measured cases and four downloaded/hash-checked artifacts | `.commit-watch/person3/worker-run/collection.json`, run `run-41ce85c3d0ae428791ed9323a185bcaf` |
| Restart and restrictive service permissions | Existing completed record/bindings preserved; fresh run in clean minimal venv with `umask 0077` again completed/reproduced/unavailable, 14 cases | `.commit-watch/person3/restart.json`, `.commit-watch/person3/worker-run-private-umask/collection.json`, run `run-3fa069017cd64a0a828ff80ac34b93fc` |
| Client → service → real independent Docker verifier | Prepared narrow fixture returned by **mock transport**, correctly recorded as `mode=mock`; comparison + repaired matrix had 28 observations, repair verified | `.commit-watch/person3/client-verifier/summary.json`, run `run-f9e1bb662fd24c3bbc4f61fc4108ad56` |
| Host/source/image inventory | Actual local Darwin/arm64 host and Docker identities, source-content manifest | `.commit-watch/person3/final-host.json` |

The two `--repair` API collectors intentionally exited **1** because live proposal and verified-repair gates were not met; reproduction was retained. The client/verifier mock experiment is integration evidence, **not** OpenRouter contribution. All fixture data is synthetic. Earlier and later runs may have different contract/verifier hashes as Person 2 completed the implementation; each artifact binds its own inputs and must not be combined into a single acceptance claim.

Person 2 independently executed the narrow and deliberately permissive candidates under `umask 0077`: narrow passed 14/14; permissive failed object rejection in both environments. See `demo/upgrade/PERSON2_HANDOFF.md` and `.commit-watch/proofrun-verifier/20260929T195114Z-056ffde1/experiment.json`. This was local Docker with prepared candidates, not remote execution or generated repair.

Deployment review found and fixed root-created virtual-environment permissions (group read/execute without write). Person 2 fixed probe staging so `umask 0077` cannot make `/probe` inaccessible to the container UID. The actual Linux systemd unit/installer remains unexecuted because no authorized Linux/Crusoe host is available.

Person 2 subsequently reviewed the mock client/verifier run and confirmed that its source, contract, tests, verifier and narrow-candidate hashes match the final verifier evidence. The 28 observations and `mode=mock` label were preserved. Raw run artifacts remain under `.commit-watch/person3/client-verifier/run-f9e1bb662fd24c3bbc4f61fc4108ad56/`; Person 1 received the review confirmation.

## Connection and next-owner instructions

The intended remote service binds VM `127.0.0.1:8766`, uses an authenticated SSH forward and the agreed bearer-token API. No Docker socket is exposed to the extension or model. See [the complete deployment runbook](README.md) for CPU VM prerequisites, `install-worker.sh`, pinned-image preparation, service startup, strict host-key tunnel and provider/host evidence collection.

Person 1: review Person 3's owned diff and the linked evidence; use the native runner and preserve unavailable status when model access is absent. Read credentials only on the server. Record the implemented/local gates separately from the still-blocked Crusoe/OpenRouter gates. The Person 3 validation listener on port 18766 is temporary and stopped after checks; it is not a deployment endpoint.

Person 2: repeat `python3.12 -m demo.upgrade.verify_offline` on the authorized remote worker, then verify the actual generated proposal through the same native functions. Measure remote image IDs rather than reusing the local arm64 IDs.

Operator/Person 3: configure the key/model and authorized Crusoe route privately. Install the reviewed integrated files and preserve Git metadata; uncommitted source hashes are part of the worker evidence. After fresh image preparation and host/provider lookup, execute:

```bash
python3.12 deploy/crusoe/check-worker.py --output /private/path/route.json
python3.12 deploy/crusoe/run-worker.py --repair --expected-target crusoe \
  --output-dir /private/path/fresh-crusoe-run
```

`PROOFRUN_WORKER_TOKEN` must already be in private operator configuration; it is not a command-line argument. Supply Persons 1 and 2 the actual VM ID, SSH alias/route, worker URL, source/content-manifest identity and run/artifact references, excluding secrets. Retain the live returned model/operation identity and exact candidate/verification evidence. Core completion is still blocked until these remote/model checks and Person 1's DuploCloud round trip are measured.
