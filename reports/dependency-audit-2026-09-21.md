# Dependency compatibility check — September 21, 2026

A bounded inspection of 21 selected public repositories found one reproduced major-upgrade failure in actual repository functions and one existing API mismatch in a dormant telemetry path. This was an agent-assisted audit, not an arbitrary-repository capability of the current `upgrade-demo` CLI. No private repository source was needed. Original checkouts and GitHub repositories were not changed.

## Reproduced: GPU energy recommender with pandas 3

Repository: `Eman-Gon/gpu-energy-recommender` at `7a802eace3a3775acc5ea4af6c266079e2599eb3`.

The actual functions `collect_ercot_data` and `collect_gpu_utilization_data` use `pd.date_range(..., freq='H')` at [eda/data_collection.py line 22](https://github.com/Eman-Gon/gpu-energy-recommender/blob/7a802eace3a3775acc5ea4af6c266079e2599eb3/eda/data_collection.py#L22) and [line 63](https://github.com/Eman-Gon/gpu-energy-recommender/blob/7a802eace3a3775acc5ea4af6c266079e2599eb3/eda/data_collection.py#L63). The same calls are duplicated in `edabad/data_collection.py`.

Pandas 3 removes the uppercase hourly-frequency alias. [Official pandas 3.0 release notes](https://pandas.pydata.org/docs/whatsnew/v3.0.0.html).

Two one-day input tests directly called the unmodified repository functions. Each should return 25 hourly rows. Both packages were installed in separate Python 3.12 Docker images, with identical pinned NumPy, requests, and other supporting dependencies. Test containers had no network, no credentials, a read-only source mount, and a non-root user. The functions generate sample data; they do not contact ERCOT or a GPU service.

| Source | pandas 2.3.3 | pandas 3.0.0 |
|---|---|---|
| Original repository file | 2 tests pass; deprecation warnings | 2 errors: `ValueError: Invalid frequency: H` |
| Temporary copy changing `H` to `h` | 2 tests pass | 2 tests pass |

The repository has no checked-in dependency manifest establishing its deployed pandas version. The result proves incompatibility with the tested upgrade, not that a deployed application is currently failing. This is a targeted function check, not a complete application regression suite. The lowercase fix was verified in a disposable copy, not applied to the repository.

Artifacts:

- [Results and immutable image IDs](/Users/emanschool/secondlook/.commit-watch/repo-audit/gpu-energy-pandas/results.json)
- [Exact probe](/Users/emanschool/secondlook/.commit-watch/repo-audit/gpu-energy-pandas/test_collection.py)
- [Failure output](/Users/emanschool/secondlook/.commit-watch/repo-audit/gpu-energy-pandas/original-3.0.0.txt)
- [Proposed patch for eda copy](/Users/emanschool/secondlook/.commit-watch/repo-audit/gpu-energy-pandas/suggested-fix.patch)
- [Rerun script](/Users/emanschool/secondlook/.commit-watch/repo-audit/gpu-energy-pandas/reproduce.py)

To rerun using the two cached Docker images:

```sh
cd /Users/emanschool/secondlook
python3 .commit-watch/repo-audit/gpu-energy-pandas/reproduce.py
```

For a demo: show the real source location, run the identical checks on both versions, display the pandas migration note and exact exception, then rerun with the lowercase fix. Identify the probe as prepared by this audit. This run did not use live Groq generation or Cognee memory.

## Confirmed API mismatch: scam_killer telemetry

Repository: `Eman-Gon/scam_killer` at `931c54c4ad12557b1d49f2714d10bf34b77cadee`.

[package.json line 10](https://github.com/Eman-Gon/scam_killer/blob/931c54c4ad12557b1d49f2714d10bf34b77cadee/email-sender/package.json#L10) requests `web-vitals ^4.2.4`; the lockfile resolves `4.2.4`. [reportWebVitals.js](https://github.com/Eman-Gon/scam_killer/blob/931c54c4ad12557b1d49f2714d10bf34b77cadee/email-sender/src/reportWebVitals.js#L3-L8) imports and calls `getCLS`, `getFID`, `getFCP`, `getLCP`, and `getTTFB`.

An isolated installation of `web-vitals@4.2.4` with install scripts disabled confirmed all five exports are undefined. Calling `getCLS` raises `TypeError`; the corresponding `on*` APIs exist. The [official changelog](https://github.com/GoogleChrome/web-vitals/blob/main/CHANGELOG.md) records these migrations.

The current [index.js invocation](https://github.com/Eman-Gon/scam_killer/blob/931c54c4ad12557b1d49f2714d10bf34b77cadee/email-sender/src/index.js#L17) provides no callback, so that branch is dormant. This would break telemetry when a callback is supplied; it does not establish that the default page crashes. A fix should migrate the helper to the supported APIs for its pinned version, with a callback-path test.

## Reproduced API change for a proposed upgrade: DepScope

Repository: `Eman-Gon/DepScope` at `94dd8bc413fb25e77f45eb76e5f4e905f2313e2d`.

The frontend manifest requests `react-resizable-panels ^2.1.9` and its lock resolves 2.1.9. [The resizable wrapper](https://github.com/Eman-Gon/DepScope/blob/94dd8bc413fb25e77f45eb76e5f4e905f2313e2d/Frontend/src/components/ui/resizable.tsx#L6-L22) uses `PanelGroup` and `PanelResizeHandle`. The [official v4 release](https://github.com/bvaughn/react-resizable-panels/releases/tag/4.0.0) replaces them with `Group` and `Separator`.

An isolated package check, with install scripts disabled, confirmed those old exports exist in 2.1.9 and are undefined in 4.0.0. A minimal React server render of `PanelGroup` passed with 2.1.9 and failed with 4.0.0 because the element type was undefined.

This is a proposed-major-upgrade incompatibility: the existing `^2.1.9` range excludes v4. The wrapper has no discovered consumers elsewhere in the frontend, and the configured build runs Vite without a separate TypeScript typecheck. Therefore this does not establish a currently failing application or even a failing current build. It is a concrete migration hazard if the wrapper is used or typechecked after a deliberate v4 upgrade.

## First-pass scope


Most repositories were inspected statically. No confirmed finding in this limited scan is not a compatibility guarantee. The audit did not upgrade every dependency or run every application.

## Separate credential observation

`blockchain-ml-representation/blockchain-ml-test.py` contains an apparent hard-coded Dune API key in a notebook cell. The value was not used or tested. If it remains active, revoke/rotate it and remove it from tracked source; removing it from the latest file alone does not revoke the value in Git history. No secret value is included in this report.
