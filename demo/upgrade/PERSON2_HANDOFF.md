# Person 2 verification handoff — September 29, 2026

Owner: Person 2, verification engine. Shared checkout `/Users/emanschool/ProofRun`, branch `main`, base revision `f9112a2c947223a95c9467131294e318b0ced3df`. Changes are uncommitted; no commit, push, shared-contract edit, requirements edit, or deployment was performed by Person 2.

## Interface and scope

Consumes Person 1's agreed `proofrun.v1` dataclasses in `src/proofrun/contracts.py`:

- `run_comparison(case_spec, source_bundle) -> ComparisonEvidence`
- `verify_candidate(case_spec, source_bundle, proposal) -> VerificationEvidence`

The registered manifest is `demo/upgrade/contract.json`; `contract_sha256` hashes its exact bytes. `CaseSpec.root` may be Person 1's private source snapshot. Its contract, original tests, controls, dependency pins and Dockerfile must match the installed registered inputs. The immutable `SourceBundle.content` must match its source hash and the staged application. With no bundle hash, the runner checks the Git revision's application bytes. With an explicit canonical bundle hash, Person 1's service owns revision validation; the runner validates `SHA256(JSON({module,sha256}, sort_keys=True, separators=(',',':')))` and source content.

Native runs execute two original tests plus five independent controls in **each** pinned environment. Expected/executed IDs are stage-qualified. The older prepared demo also runs its separate omission probe, for eight checks per repaired environment. A reproduced finding remains separate from repair outcome. Only complete successful verification reports `verified`.

Candidates are bounded replacement text for the registered application. AST validation permits changes only to `Customer.nickname`'s annotation/default and an optional `typing.Any` import; function changes, verifier changes, dependency changes, unsupported imports and executable defaults are rejected. This deliberately supports one registered case, not arbitrary Python repairs or other model/field names.

Evidence binds source revision/content, contract, original tests/controls, harness/verifier, exact candidate, dependency build inputs, immutable image IDs and observed versions. Empty/missing/duplicate/wrong/skipped tests, malformed observations, mismatched hashes/versions, timeouts and setup errors cannot verify a repair. Test containers remain unprivileged, read-only, without network, capped at one CPU/512 MiB/128 processes and bounded time. Staging explicitly handles the worker's `umask 0077` while retaining a private temporary parent.

Returned `artifacts` maps safe IDs to private local paths for Person 1's service. Downloadable comparison/verification JSON contains safe artifact IDs; cases refer to registered stage-log IDs.

## Changed paths

- `src/upgrade_demo.py`
- `src/upgrade_sandbox.py`
- `src/proofrun/runner.py` (new)
- `demo/upgrade/contract.json` (new)
- `demo/upgrade/test_controls.py` (new)
- `demo/upgrade/permissive_app.py` (new)
- `demo/upgrade/probe_harness.py` (new)
- `demo/upgrade/verify_offline.py` (new)
- `demo/upgrade/PERSON2_HANDOFF.md` (new)
- `tests/test_upgrade_demo.py`
- `tests/test_upgrade_sandbox.py`
- `tests/test_proofrun_runner.py` (new)

Other dirty paths belong to teammates and were preserved.

## Actual commands and outcomes

1. Before implementation, `python3.12 -m src.main upgrade-demo --offline` exited **1**, confirming the prepared regression. Evidence: `.commit-watch/upgrade-demo/20260929T194501Z-813dd003/report.json`.
2. `python3.12 -m src.main upgrade-demo --prepare` exited **0**, building/reusing images under exact requirements/Dockerfile fingerprints.
3. Updated `python3.12 -m src.main upgrade-demo --offline` exited **1** as intended: execution completed, regression reproduced, narrow prepared repair verified, permissive prepared repair rejected. Narrow repair: 8/8 checks in each environment; permissive repair failed object rejection in each. Evidence: `.commit-watch/upgrade-demo/20260929T194957Z-c405fde2/report.json`.
4. `python3.12 -m demo.upgrade.verify_offline` exited **0**, asserting the native comparison and both candidate outcomes. Evidence: `.commit-watch/proofrun-verifier/20260929T195031Z-3e3702cd/experiment.json`.
5. After the staging-permission fix, `umask 077` followed by `python3.12 -m demo.upgrade.verify_offline` exited **0**. This is the final native verifier evidence: `.commit-watch/proofrun-verifier/20260929T195114Z-056ffde1/experiment.json`.
6. `python3.12 -m pytest tests/test_upgrade_demo.py tests/test_upgrade_sandbox.py tests/test_proofrun_runner.py tests/test_upgrade_memory.py -q --disable-warnings` — **121 passed**, 82 warnings, 4.92 seconds.
7. `python3.12 -m pytest tests/test_dashboard.py tests/test_public_repo.py tests/test_main.py tests/test_proofrun_runner.py -q --disable-warnings` — **104 passed**, 70 warnings, 3.99 seconds. This overlaps runner tests in the preceding command; counts should not be added as unique tests.
8. Scoped `git diff --check` passed.

Unit tests use explicitly mocked Docker transport/results where appropriate; sandbox harness subprocess tests execute the host's installed Pydantic 2.13.4. Those tests are separate from the actual pinned Docker measurements above. Warnings come from existing pytest/unittest compatibility and deprecated Python AST APIs.

Final native matrix, freshly executed in Docker:

| Application | Pydantic 1.10.18 | Pydantic 2.8.2 | Verdict |
|---|---:|---:|---|
| Original | 7/7 passed | 6/7 passed; omitted nickname raises the supported `ValidationError` | Regression reproduced |
| Narrow `Optional[str] = None` | 7/7 passed | 7/7 passed | Verified |
| Permissive `Any = None` | 6/7 passed; object rejection fails | 6/7 passed; object rejection fails | Rejected |

The permissive repair passes all original tests and the omitted-nickname control. Only the independent object control fails, demonstrating why the original omission probe alone is insufficient.

Observed image IDs:

- 1.10.18: `sha256:73c5126343049110de8db81e8cbd1568f3b00668a6830a53801a3b52f3534c40`
- 2.8.2: `sha256:ead217c56b8c386eeba2dbcf0ff7b6e7a2d70ce5bbb9ddadc62b62416dc16f7a`

Final native bindings:

- Source: `e10eb64618cc671713f1bc7acd21ea79d7bc99f98f8ce3697d6f512009322f26`
- Contract: `fd54523ba6cda11d1190758ecfb253519a8e6f1be7bc11130d49e010181e2f3b`
- Tests: `a6c3eaaad451cc1f85c4b6743d8b138c337199d267cda139c2b51a845a628071`
- Verifier: `38950d388f4e342bd87965a1bc0694bac18982e1658a8b9c2094e9f2da91c927`
- Narrow candidate: `04c4b567966c615fed857f39f7278d399daa5c990bb77d87388046734759ef13`

## Next owners and limitations

Person 1: review this diff and the final experiment artifact, then integrate native comparison/verification. Keep the two-attempt cap and enforce matching comparison/verification bindings and actual environments. Use the complete artifact for verdict review. Snapshot the new controls and contract; the prepared adapter additionally needs `permissive_app.py`. Shared completion records remain Person 1's responsibility.

Person 3: deploy the agreed worker with these verifier changes, prepare images, and repeat `python3.12 -m demo.upgrade.verify_offline` on the authorized Crusoe worker; retain host identity and new artifacts. Then verify an actual OpenRouter proposal through the same functions. Local image IDs are architecture/build-specific; remote IDs must be measured independently.

All measurements recorded here used **local Docker**, **synthetic inputs**, and **prepared candidates**. No external source/model/memory service was called by these demonstrations. Person 3 reported no configured OpenRouter key/model or authorized Crusoe host access, so remote execution and generated-repair evidence remain blocked on that access. DuploCloud and sponsor completion are not claimed by this handoff. Saved artifacts above record completed executions; viewing them is not a new run.
