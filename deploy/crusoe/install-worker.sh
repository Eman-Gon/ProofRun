#!/usr/bin/env bash
# Run on the authorized Linux VM after placing the reviewed checkout at the path below.
set -euo pipefail
umask 077
app=/opt/proofrun/app
if [[ $(uname -s) != Linux || ${EUID} != 0 ]]; then
  echo 'Run this installer as root on the intended Linux worker VM.' >&2
  exit 2
fi
for executable in python3.12 docker git systemctl; do
  command -v "$executable" >/dev/null || { echo "Missing prerequisite: $executable" >&2; exit 2; }
done
for relative in src/proofrun/api.py src/proofrun/runner.py requirements-worker.txt; do
  [[ -f "$app/$relative" ]] || { echo "Missing worker input: $relative" >&2; exit 2; }
done
[[ ! -e "$app/.env" ]] || { echo 'Keep the deployed checkout free of .env; use /etc/proofrun/worker.env.' >&2; exit 2; }
docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 || { echo 'Docker daemon unavailable.' >&2; exit 2; }
getent group docker >/dev/null || { echo 'Install a local Docker engine with the docker group first.' >&2; exit 2; }
if ! id -u proofrun >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/proofrun --shell /usr/sbin/nologin proofrun
fi
usermod --append --groups docker proofrun
install -d -o proofrun -g proofrun -m 0700 /var/lib/proofrun /var/lib/proofrun/artifacts /var/lib/proofrun/tmp
install -d -o root -g proofrun -m 0750 /etc/proofrun
# The service can read the implementation but cannot modify its approved tests/source.
install -d -o root -g proofrun -m 0750 /opt/proofrun
chown -R root:proofrun "$app"
chmod -R u+rwX,g+rX,g-w,o-rwx "$app"
git config --system --replace-all safe.directory "$app" '^/opt/proofrun/app$'
python3.12 -m venv /opt/proofrun/venv
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /opt/proofrun/venv/bin/python -m pip install --disable-pip-version-check \
  -r "$app/requirements-worker.txt"
chown -R root:proofrun /opt/proofrun/venv
chmod -R u+rwX,g+rX,g-w,o-rwx /opt/proofrun/venv
if [[ ! -e /etc/proofrun/worker.env ]]; then
  install -o root -g proofrun -m 0640 "$app/deploy/crusoe/worker.env.example" /etc/proofrun/worker.env
  # Secret is generated directly in its private destination, never in argv/stdout.
  /opt/proofrun/venv/bin/python - <<'PY'
from pathlib import Path
import secrets
path = Path('/etc/proofrun/worker.env')
text = path.read_text()
path.write_text(text.replace('PROOFRUN_WORKER_TOKEN=\n', 'PROOFRUN_WORKER_TOKEN=' + secrets.token_urlsafe(48) + '\n'))
PY
fi
chown root:proofrun /etc/proofrun/worker.env
chmod 0640 /etc/proofrun/worker.env
install -o root -g root -m 0644 "$app/deploy/crusoe/proofrun-worker.service" /etc/systemd/system/proofrun-worker.service
systemctl daemon-reload
printf '%s\n' 'Worker installed; not started. Set real worker ID/model credentials privately, prepare images, collect host evidence, then start the service.'
