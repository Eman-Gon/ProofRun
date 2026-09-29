# Crusoe CPU verification worker

This directory prepares the `proofrun.v1` worker owned by Person 1 and the native verifier owned by Person 2. It does not provision a VM. A local host snapshot, prepared images, or an execution-target label does not prove Crusoe execution. Keep the core Crusoe gate unverified until a fresh run and provider/host identities have been captured together.

## Access and machine selection

An operator needs an authorized Crusoe account/project, quota or credits, a CPU VM, and the SSH key registered for that VM. Use the existing authorized account; never place Crusoe API credentials on the test worker. The [Crusoe CLI instructions](https://docs.crusoecloud.com/installing-the-cli/index.html) describe CLI authentication and `crusoe whoami`. Check its exit status privately; do not paste credentials or dump `~/.crusoe/config`.

Select a general-purpose CPU VM, e.g. an available `c1a.2x` or `c2a.2x` with 2 vCPUs/8 GB. Confirm regional availability and image options in the authorized account; the [VM catalog](https://docs.crusoecloud.com/compute/virtual-machines/overview/) and [creation guide](https://docs.crusoecloud.com/quickstart/creating-a-vm/index.html) do not establish account quota. Record the actual provider VM ID, project ID, location, type and lookup time. No GPU is needed.

Use a Linux image with Python 3.12, its `venv` module, Git, systemd and Docker Engine installed. Follow the [Docker Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/) for the chosen supported image. Do not run this installer on the laptop or an unrelated production server. This repository's script requires those prerequisites and does not replace existing package repositories or daemon settings.

Use a dedicated worker VM: the worker has Docker-group access, which is [root-equivalent access to that host](https://docs.docker.com/engine/install/linux-postinstall/). The test containers themselves remain bounded by Person 2's runner: no network, no inherited model credentials, an unprivileged UID, read-only mounts and resource/time limits. The API, model and browser never receive the Docker socket.

## Deploy the reviewed implementation

Place the integrated checkout at `/opt/proofrun/app`, retaining its Git metadata. Use the agreed full revision and explicitly copy any reviewed uncommitted implementation; no implementation created during this task is assumed committed. Transfer only reviewed source and manifests, never the developer `.env`, SSH material, home directory or provider configuration. The host collector records hashes of the actual implementation to distinguish it from HEAD.

The installer creates an unprivileged service account, a minimal Python environment from `requirements-worker.txt`, and a root-managed service/config. It does not start the worker or overwrite an existing secret file:

```bash
sudo bash /opt/proofrun/app/deploy/crusoe/install-worker.sh
sudoedit /etc/proofrun/worker.env
```

Set `PROOFRUN_WORKER_ID` to the real recorded VM ID. The installer generates a bearer token directly into the private file without displaying it. Set `OPENROUTER_API_KEY` and an explicit `PROOFRUN_MODEL` privately to enable live repair. A missing model/key must produce repair unavailable and preserve the measured finding. The file is `root:proofrun` mode `0640`; it is consumed only by the host service. Do not source it into shell tracing, print it, or copy it into a report or test container.

Prepare both pinned fixture environments through Person 2's builder, without inheriting service credentials:

```bash
sudo -u proofrun env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/var/lib/proofrun \
  TMPDIR=/var/lib/proofrun/tmp \
  /opt/proofrun/venv/bin/python /opt/proofrun/app/deploy/crusoe/prepare-images.py \
  --output /var/lib/proofrun/artifacts/image-preparation.json
```

Preparation deliberately rebuilds the pinned environments and records immutable image IDs, requirements hashes and Dockerfile hash. The Dockerfile currently uses a mutable upstream base tag; the captured built image ID is the environment identity, not a claim that future builds are bit-for-bit identical. Actual Pydantic versions and test execution are checked by the verifier. Keep builds/downloads outside test jobs.

Start the agreed native API:

```bash
sudo systemctl enable --now proofrun-worker
sudo systemctl is-active proofrun-worker
```

The service runs `/opt/proofrun/venv/bin/python -m src.proofrun.api --host 127.0.0.1 --port 8766 --runner native`. It uses `/var/lib/proofrun/tmp`, visible to the Docker daemon for bind mounts; `PrivateTmp` would break those mounts. Source/tests are root-owned, and only `/var/lib/proofrun` is writable through systemd's filesystem policy. A restarted interrupted job must remain interrupted according to Person 1's service; recheck this on the VM before the final demo.

## Private authenticated route

The worker binds only to VM loopback. [Crusoe SSH documentation](https://docs.crusoecloud.com/compute/virtual-machines/accessing-vms/index.html) explains VM addresses and SSH access. Restrict inbound SSH to the authorized operator route in the cloud firewall. Do not open ports 8766 or 2375/2376 publicly. Confirm the VM's SSH host key through a trusted channel and save it in `known_hosts`; the tunnel refuses unknown or changed keys.

Create a private local SSH alias containing the actual VM hostname/address, SSH username (normally `ubuntu` for Crusoe Ubuntu images), and key path. Then run locally:

```bash
./deploy/crusoe/tunnel.sh proofrun-crusoe 8766
```

This forwards local `127.0.0.1:8766` to VM `127.0.0.1:8766` and refuses failed port forwarding. Person 1's extension backend uses this route and `PROOFRUN_WORKER_TOKEN` from its private configuration. Keep the bearer token out of browser data. If the backend runs in a DevKit container, its loopback is different: run the SSH tunnel in that backend's network namespace or use an explicitly restricted host route, then test from that actual container. Do not make the tunnel listen on `0.0.0.0` merely to bypass that distinction.

Check health and both authentication rejection/acceptance paths from the local end of the tunnel. The script prompts for the token without echo, or reads an already privately set `PROOFRUN_WORKER_TOKEN`:

```bash
python3.12 deploy/crusoe/check-worker.py --output /tmp/proofrun-private-route.json
```

This checks `GET /health` and authenticated `GET /v1/cases/customer-nickname-v1`. It proves reachability/auth behavior only. Submit a fresh registry-bound run through Person 1's extension/API and retain its real `run_id`; that is the execution evidence. The repeatable collector below submits a new job, polls for up to ten minutes, and downloads only declared artifacts after checking every size/hash. It reads the bearer token only from the process environment; populate that variable using a private secret mechanism or a hidden prompt, never a literal command in shell history.

```bash
python3.12 deploy/crusoe/run-worker.py --expected-target crusoe \
  --output-dir /tmp/proofrun-crusoe-run
```

Add `--repair` explicitly to request up to two live model proposals. This option requires both real OpenRouter provenance and the independent verifier's verified result for exit `0`. With repair disabled, exit `0` requires completed native/fresh execution, reproduction and hash-bound artifacts. Exit `1` means the run completed but requested gates were not met (including model unavailable). Exit `2` means collection/setup/execution could not complete. All outcomes save `collection.json`; a collection timeout does not cancel the worker job. Existing output directories are rejected to preserve previous evidence. A core-wide pass still also needs separate bad-fix rejection, Crusoe host evidence and the DuploCloud round trip.

## Evidence and handoff

Create `/var/lib/proofrun/crusoe-vm.json` on the VM, owned by `proofrun` mode `0600`, with exactly these six non-secret fields copied from an authenticated Crusoe lookup. Replace every example value with the actual value; do not copy the raw CLI/provider response if it includes unrelated data:

```json
{
  "vm_id": "actual-vm-id",
  "project_id": "actual-project-id",
  "location": "actual-zone",
  "instance_type": "actual-cpu-type",
  "lookup_time": "2026-09-29T12:00:00+00:00",
  "lookup_method": "crusoe_cli_authenticated"
}
```

Use `crusoe_console_authenticated` for a console lookup. This supplied record is corroborating operator evidence, not cryptographic provider attestation. Collect the actual host, worker source hashes, architecture, runtime and image identities on that machine:

```bash
sudo -u proofrun env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/var/lib/proofrun \
  /opt/proofrun/venv/bin/python /opt/proofrun/app/deploy/crusoe/collect-host-evidence.py \
  --execution-target crusoe --crusoe-record /var/lib/proofrun/crusoe-vm.json \
  --output /var/lib/proofrun/artifacts/host-evidence.json
```

For development on the laptop, use `--execution-target local` and omit `--crusoe-record`. Output remains explicitly local. The collector does not read environment variables, Docker `Config.Env`, provider configuration, private SSH keys or credential files.

Give Persons 1 and 2 the SSH alias/route, worker ID, exact revision and content-manifest hash, run ID and authenticated artifact references. Retain `host-evidence.json`, `image-preparation.json`, route-check evidence and the complete comparison/proposal/verification artifacts. The remote fresh run must reproduce the regression, preserve the original suite and controls on the accepted candidate, reject the permissive candidate, and bind to the same immutable images/source/contract/test/candidate hashes. A prepared repair must remain labeled prepared; only actual OpenRouter provenance can establish generated repair.

No Crusoe connection or remote runtime is established by these files alone. Missing account/project access, VM identity, SSH host/key, model configuration or quota should be listed separately in the handoff rather than collapsed into a generic setup failure.
