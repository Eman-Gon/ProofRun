# Five-repository live test — September 21, 2026

Tested Hackday Idea’s existing public-repository workflow through the real dashboard HTTP API at `http://127.0.0.1:8765`. Five repositories were sampled without replacement from the seven supplied repositories using `random.SystemRandom().sample`. The saved selection was not changed after seeing the outcomes.

**Result: three checks were blocked by the download limit; two completed with very limited file coverage and no identified dependencies. None establishes dependency compatibility or a verified application failure.**

| Repository | Observed result | Files scanned | Dependency declarations | Static findings | Duration |
|---|---|---:|---:|---:|---:|
| `trycua/cua` | Blocked: archive exceeds 20 MB download cap | — | — | — | 30.27s |
| `coder/coder` | Blocked: archive exceeds 20 MB download cap | — | — | — | 28.54s |
| `BuilderIO/agent-native` | Blocked: archive exceeds 20 MB download cap | — | — | — | 29.25s |
| `cloudflare/quiche` | Completed, but dependency coverage unknown | 1 | 0 | 0 | 2.02s |
| `akitaonrails/ai-memory` | Completed, but dependency coverage unknown | 2 | 0 | 0 | 7.10s |

## Coverage and interpretation

The scanner inspects Python and JavaScript/TypeScript source, package.json, requirements text, and supported pyproject.toml declarations. It does not analyze Rust or Go code/manifests. Its three rules concern pandas uppercase hourly frequencies, Pydantic nullable fields without defaults, and legacy web-vitals imports. It does not install packages, execute repository code, run upstream test suites, or invoke AI services.

The two completed scans (`cloudflare/quiche` and `akitaonrails/ai-memory`) found zero supported dependency declarations. They read only 1 and 2 eligible files, respectively. Their zero-finding results provide no meaningful Rust dependency compatibility evidence.

The size cap rejected all three larger snapshots before a source result was produced. This is a product coverage limit, not a test failure in those upstream repositories. These blocked rows are not counted as successful scans or zero-finding results.

Completed snapshots:

- `cloudflare/quiche`: [`cdf610571c76f0a41927466b6e27c6ec12e3fc82`](https://github.com/cloudflare/quiche/tree/cdf610571c76f0a41927466b6e27c6ec12e3fc82)
- `akitaonrails/ai-memory`: [`5157c6be10b5830d7adc7e29b0a4358c3ecbe6a2`](https://github.com/akitaonrails/ai-memory/tree/5157c6be10b5830d7adc7e29b0a4358c3ecbe6a2)

## Dashboard and regression checks

Each repository request returned HTTP 202, a scan ID, progress, and a terminal completed/failed state. All failed scans supplied a readable error. The browser displayed the archive-limit failure and disabled the export button for that failed scan. Raw API responses were saved separately, including failed scans.

The initial API contract check failed on the two completed scans because the running server omitted the new `explanation` field. The parallel dashboard task then updated the backend while preserving scan state. A final HTTP check confirmed that all five original scan IDs survived and both completed results now include explanation objects. Browser verification also confirmed the quiche explanation explicitly says only one file was read, no dependencies were identified, and no code or tests were run. The original raw responses remain unchanged; [post-update evidence](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/after-server-update.json) is recorded separately.

Independent local regression run: `.venv/bin/python -m pytest tests/test_public_repo.py tests/test_result_explanation.py tests/test_dashboard.py -q` — **59 tests passed, 79 subtests passed**. Localhost server tests required execution outside the restrictive sandbox; the initial bind failures were environmental. These are Hackday Idea’s tests, not the five repositories’ own test suites.

## Follow-up indicated by these tests

- Fetch bounded relevant files at a pinned commit instead of requiring an entire large archive. Prioritize dependency manifests before filling the source-file budget.
- Report unsupported ecosystems and negligible source coverage prominently; these should not read as successful compatibility assessments.
- Add ecosystem-specific support and a concrete dependency/version comparison before claiming coverage for Rust or Go projects.
- Include scanned filenames and language coverage in exports, and retain the full resolved commit ID even when the archive download fails.

## Evidence

- [Random selection](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/selection.json)
- [Batch summary, assertions, timings, warnings](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/summary.json)
- [Re-runnable API test script](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/run_checks.py)
- [trycua/cua raw response](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/trycua--cua.json)
- [coder/coder raw response](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/coder--coder.json)
- [BuilderIO/agent-native raw response](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/BuilderIO--agent-native.json)
- [cloudflare/quiche raw response](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/cloudflare--quiche.json)
- [akitaonrails/ai-memory raw response](/Users/emanschool/secondlook/.commit-watch/random-repo-test-20260922T023634Z/akitaonrails--ai-memory.json)

Test times and repository snapshots are recorded in UTC in the JSON files (September 22); the report date uses the local America/Los_Angeles date (September 21). No application source or upstream repository was changed by this test task.
