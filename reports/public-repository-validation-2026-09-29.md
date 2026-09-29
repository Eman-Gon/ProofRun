# Public repository review validation — September 29, 2026

The new review agent discovered a previously unspecified correctness bug in a dependency-free synthetic Python project. Live Python, TypeScript, Go, and Rust repositories returned actual dependency declarations; a README-only repository returned none. The matrix also exposed source-reading and completion-budget reliability issues, and distinguishes successful ingestion from completed model review.

## Independent discovery and patch check

The live configured OpenRouter model received a four-file shipping-calculator project, including its documented behavior and existing tests. No bug location, dependency, or migration hint was supplied. It chose to read the README, implementation, and tests, then reported the free-shipping boundary error in **4 decisions / 23.95 seconds**.

The generic PR candidate builder applied its exact source-bound suggestion. Before executing anything, validation required the proposed result to equal the expected one-operator change (`>` to `>=`). Only these locally authored synthetic sources were executed:

| Input subtotal | Expected total | Original | Proposed fix |
|---|---:|---:|---:|
| 4000 | 4500 | 4500 | 4500 |
| 5000 | 5000 | 5500 | 5000 |
| 6000 | 6000 | 6000 | 6000 |

Evidence: [synthetic source, model provenance, finding, and checks](../.commit-watch/public-validation-20260929/synthetic-agent.json).

## Live public repositories

All downloads were pinned to actual default-branch commits. No upstream repository code, tests, installs, or PR publication were performed.

| Repository | Source/configuration files retained | Dependency declarations | Initial agent result |
|---|---:|---:|---|
| `pallets/flask` — Python | 210 | 35 | Tool/evidence validation failed |
| `sindresorhus/is` — TypeScript | 13 | 12 | Failed after an oversized read request |
| `spf13/cobra` — Go | 61 | 10 | Tool/evidence validation failed |
| `serde-rs/json` — Rust | 77 | 15 | Tool/evidence validation failed |
| `octocat/Hello-World` — README only | 1 | 0 | Completed; no findings or invented dependencies |

The diagnostic retry of `sindresorhus/is` reproduced the problem: after reading the first 180 lines, the model requested lines 180–420. The original agent ended the whole review because that range exceeded its 180-line cap. These incomplete reviews are not counted as successful bug checks. Retaining 210 files, for example, does not mean the model read every one.

Evidence: [initial matrix](../.commit-watch/public-validation-20260929/live-matrix.json), [public tool diagnostic](../.commit-watch/public-validation-20260929/tool-diagnostic.json).

After source ranges were clamped safely, a bounded retry produced these results:

| Repository | Files actually read by model | Model review outcome |
|---|---:|---|
| `pallets/flask` | 5 | Model exceeded the time budget; no findings accepted |
| `sindresorhus/is` | 2 | Model exceeded the time budget; no findings accepted |
| `spf13/cobra` | 3 | Model returned invalid JSON arguments; no findings accepted |
| `serde-rs/json` | 4 | Completed in 64.97 seconds; no findings |

Evidence: [post-clamping retry and actual model provenance](../.commit-watch/public-validation-20260929/live-retry.json). A subsequent change reserves the last 20 seconds for an explicit finish instruction and labels time-budget exhaustion as partial review. Provider output can still fail; no fallback findings are fabricated.

One final TypeScript retry after that change retained 13 files and 12 dependencies but still exhausted the model budget, correctly returning **partial** review in 67.16 seconds. No further live retries were attempted. The finish instruction improves the bounded contract but did not establish reliable completion for this repository: [finalization retry](../.commit-watch/public-validation-20260929/finalization-retry.json).

The final implementation also adds one bounded correction for malformed model arguments and a premature finish without reading source. Those final adjustments have mocked regression coverage (the implementation agent reports 99 focused tests passed); they were added after the live retry process had imported its code and are not represented as live-validated here.

## Reproduce and follow-up

```bash
python3.12 scripts/check-public-repositories.py --env-file .env --require-agent \
  --output .commit-watch/public-validation-new/live-matrix.json
```

The runner accepts up to five `--repository owner/repo` arguments and limits concurrency to two. Missing or incomplete agent review causes `--require-agent` validation to fail; no dependency defaults are inserted.

Baseline focused regression checks: **90 passed** across public scanning, PR proposals, dependency parsing, result explanations, and dashboard behavior.

Final integrated checks: **182 Python tests passed** across dependency parsing,
public scanning, review evidence/recovery, PR publication, dashboard and evidence
graphs; **5 UI tests passed** for preview, submission, errors, reports and default
selection. JavaScript syntax and whitespace checks passed. Browser checks
confirmed the fixed PR preview displays the actual target repository, restores
focus on cancel, and opens saved runner reports. The dashboard was refreshed on
port 8765 while preserving all ten existing scan records and comparison history.

After the changes, both targeted archive-limit/invalid-archive and unsupported/malformed-manifest checks passed. The oversized-archive case uses an intentionally reduced cap with an in-memory archive, avoiding an unnecessary large public download.

The next improvements with the clearest value are more reliable review completion within the time budget, prioritized file retrieval for repositories exceeding the 20 MB archive cap, and optional sandboxed regression checks to verify source-review hypotheses. These bounded checks do not establish support for every repository or prove that zero-finding repositories are bug-free.
