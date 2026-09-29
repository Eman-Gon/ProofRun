#!/usr/bin/env bash
# Usage: ./deploy/crusoe/tunnel.sh <verified-ssh-config-alias> [local-port]
set -euo pipefail
host=${1:-}
port=${2:-8766}
if [[ $# -gt 2 || ! $host =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ || ! $port =~ ^[0-9]{1,5}$ ]]; then
  echo 'Usage: tunnel.sh <verified-ssh-config-alias> [local-port]' >&2
  exit 2
fi
if (( 10#$port < 1024 || 10#$port > 65535 )); then
  echo 'Local port must be between 1024 and 65535.' >&2
  exit 2
fi
exec ssh -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L "127.0.0.1:${port}:127.0.0.1:8766" "$host"
