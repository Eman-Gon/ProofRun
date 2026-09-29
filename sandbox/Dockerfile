# The selected demo repo has Python and TypeScript tests.
# Build once; check-time containers have no network access.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/python \
    && /opt/python/bin/pip install --no-cache-dir \
        pytest==8.4.2 pydantic==2.13.4 praatio==6.2.2

# Immutable clean-commit manifests. Adapt these when changing the target repo
# or reviewing a dependency change; never install packages during a check.
WORKDIR /opt/test-deps
ADD https://raw.githubusercontent.com/Eman-Gon/fp-multimodel/3b6302e76d0b87ca49c77b98323596d329008a13/package.json ./package.json
ADD https://raw.githubusercontent.com/Eman-Gon/fp-multimodel/3b6302e76d0b87ca49c77b98323596d329008a13/package-lock.json ./package-lock.json
RUN npm ci --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

ENV PATH="/opt/python/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    CI=true \
    npm_config_update_notifier=false
USER 65534:65534
WORKDIR /work
