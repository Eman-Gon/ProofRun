# ProofRun

ProofRun reproduces a supported dependency regression and independently verifies a bounded repair. The current case checks customer-import behavior across Pydantic 1.10.18 and 2.8.2 using approved synthetic inputs. Results keep execution, finding and repair status separate, with evidence tied to the exact source, contract, tests, candidate and environment.

The current stack is **DuploCloud** for initiation and evidence display, **Crusoe** for the CPU worker, and **OpenRouter** for generated repair proposals. Local worker execution is available. The complete DuploCloud portal round trip, Crusoe execution and live OpenRouter proposal still need their integration gates verified; see [the integration record](INTEGRATION.md).

## Local setup

Use Python 3.12, Git and Docker with its daemon running. Run these commands from the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-worker.txt

if [ -f .env.proofrun ]; then
  PROOFRUN_ENV_FILE=.env.proofrun
else
  PROOFRUN_ENV_FILE=.env
  if [ ! -e "$PROOFRUN_ENV_FILE" ]; then
    cp .env.example "$PROOFRUN_ENV_FILE"
  fi
fi
chmod 600 "$PROOFRUN_ENV_FILE"
```

This reuses the existing private `.env.proofrun` worker configuration when present and only copies [.env.example](.env.example) when a new `.env` is needed. Keep private configuration ignored by Git. For local execution, use `PROOFRUN_EXECUTION_TARGET=local`, `PROOFRUN_WORKER_ID=local-worker` and `PROOFRUN_WORKER_URL=http://127.0.0.1:8766`.

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
if [ -f .env.proofrun ]; then
  PROOFRUN_ENV_FILE=.env.proofrun
else
  PROOFRUN_ENV_FILE=.env
fi
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

Run the focused worker checks in the worker environment:

```bash
python -m pip install pytest==9.1.1
python -m pytest tests/test_proofrun_api.py tests/test_proofrun_service.py \
  tests/test_proofrun_runner.py tests/test_proofrun_repair.py -q
```

These tests exercise API, orchestration, verification and proposal handling, using mocks where appropriate. They do not establish live sponsor integration. The separate Docker experiment and actual service runs provide runtime evidence.

The supported scope is one registered Python/Pydantic fixture. Arbitrary repository execution, automatic repository changes and automatic deployment are not implemented. A verified candidate passed the declared checks; its evidence is limited to the supplied source, inputs and environments.
