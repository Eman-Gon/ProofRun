#!/usr/bin/env bash
# Keep the DevKit as a sibling. Its default builder only mounts its own checkout.
set -euo pipefail
EXTENSION_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEVKIT_DIR="${PROOFRUN_DEVKIT_DIR:-$(cd "$EXTENSION_DIR/../../.." && pwd)/proofrun-duplocloud-devkit}"
MODE="${1:-bundle}"
if [ ! -f "$DEVKIT_DIR/scripts/build-extension.sh" ]; then
  echo "Set PROOFRUN_DEVKIT_DIR to the official sibling DevKit checkout." >&2
  exit 1
fi

# The SDK-provided UI library stays generated/ignored; the lockfile pins its integrity.
mkdir -p "$EXTENSION_DIR/frontend/vendor"
cp "$DEVKIT_DIR/packages/duplocloud-internal-ng-common-lib-0.3.0.tgz" "$EXTENSION_DIR/frontend/vendor/"

if [ "$MODE" = --frontend ]; then
  exec docker run --rm --user "$(id -u):$(id -g)" \
    -v "$EXTENSION_DIR/frontend:/work" -w /work \
    node:22-alpine sh -c 'npm --cache /tmp/proofrun-npm ci && npm run build'
fi
if [ "$MODE" = --native ]; then
  exec "$DEVKIT_DIR/scripts/build-extension.sh" --native "$EXTENSION_DIR"
fi
if [ "$MODE" != bundle ]; then
  echo "Usage: $0 [--frontend|--native]" >&2
  exit 1
fi

cd "$DEVKIT_DIR"
# Use the official target resolver without editing its configuration or printing tokens.
source scripts/_target.sh
source scripts/_builder.sh
# Bash 3.2 + nounset rejects an empty array expansion. This header is also harmless when unset.
if ! curl -fsS --max-time 5 -H "Authorization: Bearer ${TOKEN:-}" "$BASE_URL/v1/aiservicedesk/extensions/sdk-version" -o /dev/null 2>/dev/null; then
  echo "The running DuploCloud SDK feed is unavailable. Complete the sibling DevKit setup and start the portal before building the backend bundle." >&2
  exit 1
fi
if [ "${_TARGET:-local}" = local ] && [ -z "${DUPLO_BASE:-}" ]; then
  DUPLO_BASE="$(builder_studio_url)"
fi
builder_resolve_image
DUPLO_UID="$(id -u)" DUPLO_GID="$(id -g)"
export DUPLO_UID DUPLO_GID BUILDER_IMAGE DUPLO_BASE DUPLO_TARGET="${_TARGET:-local}"
# Explicit second mount resolves the official script's external-checkout restriction. The source and
# dist remain owned by ProofRun; no repository adoption, staging copy or sibling edits are needed.
exec docker compose run --rm -T --no-deps \
  -v "$EXTENSION_DIR:/proofrun-extension" \
  builder scripts/build-extension.sh --native /proofrun-extension
