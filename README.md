# Hackday Idea

Hackday Idea helps developers investigate dependency upgrades and review repository changes. It combines public source checks, prepared before/after tests, and saved investigation results in a local dashboard.

Findings are scoped to the source, dependency versions, and checks shown. Static warnings identify patterns to investigate; measured comparisons record what the supplied tests actually establish.

## Setup

Use Python 3.12 and Git. Docker with its daemon running is required for measured comparisons and sandbox tests.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create `.env` from `.env.example` if it does not already exist. Keep existing credentials and never commit `.env`. Public repository scans work without service credentials, subject to GitHub rate limits. Configure the optional integrations only for workflows that use them.

## Local dashboard

```sh
python -m src.dashboard
```

Open [Hackday Idea at localhost:8765](http://127.0.0.1:8765). Select an account's public repository or enter a public GitHub URL, then choose **Check repository**. The account defaults to the owner in `TARGET_REPO`.

Repository checks read a bounded source snapshot at a pinned commit, inventory supported dependency manifests, and identify known upgrade patterns. They do not execute repository code or install its dependencies. Results include source locations, explanations, references, and coverage limits. No supported matches does not guarantee compatibility. Recent scans remain available for the current server session.

The **Saved examples** collection opens curated source snapshots and comparisons. **Saved scans & prepared comparisons** provides previous results, proposed patches, and JSON exports. Prepared cases include a pandas frequency-alias comparison and a Pydantic customer-import example. Missing artifacts or images are reported as unavailable.

**Run comparison** collects fresh Docker evidence using a prepared probe. **Run live investigation** on the customer-import case also retrieves upstream context, requests constrained test data, and stores and retrieves confirmed findings. Source and memory status are reported separately from test outcomes. Displaying a saved result does not repeat its service calls. Proposed fixes are tested in disposable copies.

## Dependency comparisons

Build the pinned Pydantic environments, then run the prepared comparison without external service calls:

```sh
python -m src.main upgrade-demo --prepare
python -m src.main upgrade-demo --offline
```

The comparison runs existing tests, a targeted missing-field probe, and the same probe with an explicit-default fix against both dependency versions. Containers have no network access or credentials. Package installation occurs when building images. Reports record source provenance, versions, hashes, output, and the proposed patch under `.commit-watch/upgrade-demo/`.

For a live investigation, configure the source and inference providers in `.env`:

```sh
python -m src.main upgrade-demo
```

`--require-integrations` requires successful source verification and memory storage/retrieval. Without strict mode, a failed provider fetch can use a labeled direct HTTPS fallback for the fixed upstream URL. `--no-memory` skips memory outside strict mode. Offline mode uses prepared data and cached images.

Comparison exit codes are `1` for a confirmed behavior break and `2` for inconclusive evidence or setup/integration failure. Image preparation returns `0` on success. A finding applies only to the supplied source, versions, and tests.


## Commit review

The CLI can store a repository baseline, judge a new commit against fixed criteria, and run its tests in Docker. Configure the target, test command, and image for your repository, then build the image:

```sh
docker build -t commit-watch-sandbox:latest -f sandbox/Dockerfile .
python -m src.main ingest --count 35
python -m src.main check <sha>
```

Ingest before the commits you want to review, or use `ingest --ref <earlier-sha>`. Checks reject commits already in the baseline and require the baseline head to be an ancestor of the checked commit. Checks do not add commits to the baseline.

Review criteria are message/size mismatch, out-of-place files, logic without tests, and a clear break from baseline patterns. Judgment and test results remain separate. Dependency changes can receive bounded upstream context. Baseline metadata, the selected diff, and relevant context may be sent to configured inference services.

Review exit codes are `0` for no flag and passing tests, `1` for a flag or test failure, and `2` for configuration, provider, sandbox, or timeout errors. Adapt and rebuild the sandbox image when changing repositories or dependencies.

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

The implementation retains existing storage identifiers and command names for compatibility with saved results. Automatic repository changes, webhooks, and general before/after test generation for arbitrary repositories are not implemented.
