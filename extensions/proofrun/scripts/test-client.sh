#!/usr/bin/env bash
set -euo pipefail
EXTENSION_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec docker run --rm --user "$(id -u):$(id -g)" \
  --add-host host.docker.internal:host-gateway \
  -e DOTNET_CLI_HOME=/tmp/proofrun-dotnet -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  -e PROOFRUN_WORKER_URL -e PROOFRUN_WORKER_TOKEN \
  -v "$EXTENSION_DIR:/work" -w /work \
  mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim \
  dotnet run --project tests/ProofRun.ClientChecks.csproj -- "$@"
