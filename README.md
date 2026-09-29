# ProofRun

ProofRun investigates configured HTTP application releases at two exact Git commits. An agent inspects source and diffs, chooses synthetic experiments, and proposes bounded repairs. An independent harness compares repeated executions, preserves original tests, and reports **update, skip, or postpone** within a configured budget. Start with the [release investigation guide](docs/RELEASE-INVESTIGATION.md) and [measured validation](docs/RELEASE-INVESTIGATION-VALIDATION.md).

The retained fixture demo checks customer-import behavior across Pydantic 1.10.18 and 2.8.2 using approved synthetic inputs. Both paths keep execution, finding and repair status separate, with evidence tied to source, requirements, tests, candidate and environment.

The chosen demo: **an FDE has a customer meeting in 20 minutes and needs to check a dependency update against five approved customer-import examples.** See the [demo runbook and two-minute talk track](docs/FDE-DEMO.md). The available local proof executes a Python function in Docker; it does not replay requests against a staging API.

The integrations use **DuploCloud** for initiation and evidence display, **Crusoe** for CPU hosting, and **OpenRouter** for model access. Local release investigation has been exercised with actual model calls and Docker execution. See [the integration record](INTEGRATION.md) for the separate portal, hosting and fixture milestones; these are distinct from customer staging verification.

## Local setup

Use Python 3.12, Git and Docker with its daemon running. Run these commands from the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-worker.txt

PROOFRUN_ENV_FILE=.env
if [ ! -e "$PROOFRUN_ENV_FILE" ]; then
  cp .env.example "$PROOFRUN_ENV_FILE"
fi
chmod 600 "$PROOFRUN_ENV_FILE"
```

Use the main `.env` for worker, model and integration configuration. This only copies [.env.example](.env.example) when a new `.env` is needed. Keep private configuration ignored by Git. For local execution, use `PROOFRUN_EXECUTION_TARGET=local`, `PROOFRUN_WORKER_ID=local-worker` and `PROOFRUN_WORKER_URL=http://127.0.0.1:8766`.

Set `PROOFRUN_WORKER_TOKEN` to a secret of at least 32 characters. This command fills an empty setting or adds a missing setting directly to the selected file, preserving an existing token and displaying no secret:

```bash
python - "$PROOFRUN_ENV_FILE" <<'PY'
from pathlib import Path
import re
import secrets
import sys

path = Path(sys.argv[1])
content = path.read_text()
pattern = r"(?m)^PROOFRUN_WORKER_TOKEN=[ \t]*$"
if re.search(pattern, content):
    content = re.sub(pattern, "PROOFRUN_WORKER_TOKEN=" + secrets.token_urlsafe(32), content)
elif not re.search(r"(?m)^PROOFRUN_WORKER_TOKEN=", content):
    content = content.rstrip("\n") + "\nPROOFRUN_WORKER_TOKEN=" + secrets.token_urlsafe(32) + "\n"
path.chmod(0o600)
path.write_text(content)
PY
```

For generated repair, privately fill `OPENROUTER_API_KEY` and an explicit `PROOFRUN_MODEL` provider/model ID in the selected file. Comparison-only runs do not need model access. Missing model configuration leaves repair unavailable while preserving any reproduced finding; there is no fallback model or prepared repair substitution.

## Prepare and run the worker

Build the two pinned fixture images before starting test jobs. Preparation downloads packages and saves image identities; test containers run without network access or provider credentials.

```bash
python deploy/crusoe/prepare-images.py \
  --output .commit-watch/proofrun-setup/image-preparation.json

set -a
source "$PROOFRUN_ENV_FILE"
set +a
python -m src.proofrun.api --host 127.0.0.1 --port 8766 --runner native
```

The API reads the exported process environment; it does not load private environment files automatically. If a worker is already running, use its matching configuration for collection below, or stop it before starting another on the same port. All `/v1/` routes require the worker bearer token. The token stays in server configuration and must never enter browser data or test containers.

In a second terminal, activate the virtual environment and export the same private configuration, then collect a fresh comparison and its hash-checked artifacts:

```bash
source .venv/bin/activate
PROOFRUN_ENV_FILE=.env
set -a
source "$PROOFRUN_ENV_FILE"
set +a
python deploy/crusoe/run-worker.py --expected-target local \
  --output-dir .commit-watch/my-fresh-http-run
```

Choose a new output directory for each collection. Add `--repair` to request up to two actual model proposals; a successful repair collection requires live OpenRouter provenance and independent verification. The worker accepts the registered `customer-nickname-v1` case and runs one job at a time.

To check the verifier separately with the checked-in narrow and deliberately permissive candidates:

```bash
python -m demo.upgrade.verify_offline
```

This executes real local Docker tests with **prepared candidates and synthetic inputs**. It expects a reproduced regression, acceptance of the narrow fix and rejection of the permissive fix. It returns `0` only when that experiment passes, otherwise `2`, and writes evidence under `.commit-watch/proofrun-verifier/`. It does not call OpenRouter or establish generated repair.

## DuploCloud and Crusoe

Follow the [DuploCloud extension guide](extensions/proofrun/README.md) for DevKit setup, backend-only worker configuration, building and portal deployment. A backend running in a container needs a route to the worker reachable from that container. Its own localhost is not the laptop worker.

Follow the [Crusoe deployment guide](deploy/crusoe/README.md) for the service install, private route and actual VM/host evidence. Set the real worker identity only on that VM. A `crusoe` configuration label alone does not establish remote execution.

## Development and scope

The selected additions are **Similarweb customer research**, **BAND repair
coordination**, and **Neo4j deployment selection**. Their local implementation
and tests are separate from proof of live sponsor access. See
[Similarweb setup](docs/SIMILARWEB.md), [BAND setup](docs/BAND.md), and
[Neo4j setup and acceptance demo](docs/NEO4J.md) for prerequisites and commands.

The extension's explicit customer-research action asks for a public domain and a
completed month. The worker retrieves Similarweb estimated website visits and
returns a dated report for the meeting brief. This uses Similarweb API credits;
there is no background polling or automatic provider retry. The research report
cannot change the approved requirements or verification result.

When BAND is enabled, the proposed candidate must reach the verifier through the
configured room and its result must return through BAND. An unavailable handoff
leaves repair unavailable while preserving the reproduced finding. Without BAND
enabled, the existing direct local verification flow remains available and makes
no BAND integration claim.

Keep the API key and two registered BAND identities in the main, ignored `.env`
alongside the worker and other integration settings:

```bash
set -a
source .env
set +a
# Install the optional SDK only when using BAND:
python -m pip install -r requirements-band.txt
```

Restart the worker with those exported settings to apply them. Portal credentials
remain separate. Plaud and Vultr remain deferred.

Neo4j is off by default. When enabled, explicit deployment revisions and versioned
contracts determine which deployments need rechecking after a contract change.
Authenticated worker routes return the affected deployments and explaining
paths, then submit a selected deployment to the existing registered verifier.
The graph records measured run evidence; only executed core checks determine the
result. Old contract/revision evidence cannot satisfy the new selection, and an
unavailable database never falls back to memory storage. Install
`requirements-neo4j.txt` and follow [the Neo4j guide](docs/NEO4J.md) to configure a
local database or an existing authorized instance.

Run the focused worker checks in the worker environment:

```bash
python -m pip install pytest==9.1.1
python -m pytest tests/test_proofrun_api.py tests/test_proofrun_service.py \
  tests/test_proofrun_runner.py tests/test_proofrun_repair.py -q
```

These tests exercise API, orchestration, verification and proposal handling, using mocks where appropriate. They do not establish live sponsor integration. The separate Docker experiment and actual service runs provide runtime evidence.

The commands above exercise the registered Python/Pydantic fixture. The separate [release investigation worker](docs/RELEASE-INVESTIGATION.md) supports operator-configured Git repositories, pinned execution images and HTTP response-compatibility requirements. It generates previously unspecified probes within that scope; it does not establish arbitrary-bug discovery or exhaustive repository coverage. Verified repairs remain reviewable candidates, with evidence limited to the tested inputs and environments.

## Local dashboard

Start the local dashboard with `python3.12 -m src.dashboard` and open
`http://localhost:8765`. It provides saved source scans and prepared Docker
comparisons. The former source-research and memory integrations, their live
investigation button, and the dependent `ingest`/`check` CLI commands have been
removed. `python3.12 -m src.main upgrade-demo --offline` remains available;
its saved evidence is local. The current sponsor workflow uses the separate
ProofRun worker and DuploCloud extension described above.

After a fixture repair passes both pinned environments, the dashboard can create
a draft GitHub pull request. The action requires an authenticated `gh` CLI and a
GitHub `origin`. Before publishing, it rechecks the latest report hashes and the
default-branch file contents against the exact tested source. The browser asks
for confirmation, then creates a `codex/` branch containing only the verified
application-file change. Stale or incomplete evidence cannot publish a PR.
