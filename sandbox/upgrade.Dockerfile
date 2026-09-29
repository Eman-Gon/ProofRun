# Build each pinned dependency set independently; record the resulting image ID.
FROM python:3.12-slim-bookworm

COPY requirements.txt /opt/secondlook/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    -r /opt/secondlook/requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
USER 65534:65534
WORKDIR /probe
