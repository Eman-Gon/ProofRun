"""Allowlisted, secret-free operational evidence helpers (no environment dump)."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def command(argv: list[str], timeout: int = 20) -> str | None:
    try:
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Do not expose subprocess stderr: remote/provider tooling might echo credentials.
    if result.returncode or len(result.stdout) > 65536:
        return None
    return result.stdout.strip()


def write_json(path: Path, data: dict) -> None:
    """Private atomic file; output paths are operator inputs, never API paths."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.proofrun-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_identity() -> dict:
    paths = set(ROOT.glob('src/**/*.py')) | set(ROOT.glob('demo/upgrade/*.py'))
    paths |= set(ROOT.glob('demo/upgrade/*.json')) | set(ROOT.glob('demo/upgrade/requirements-*.txt'))
    paths |= set(ROOT.glob('deploy/crusoe/*.py')) | set(ROOT.glob('deploy/crusoe/*.sh'))
    paths |= set(ROOT.glob('deploy/crusoe/*.service')) | set(ROOT.glob('deploy/crusoe/*.env.example'))
    paths |= {ROOT / 'sandbox/upgrade.Dockerfile', ROOT / 'requirements-worker.txt'}
    manifest = {str(path.relative_to(ROOT)): sha256(path) for path in sorted(paths) if path.is_file()}
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    status = command(['git', 'status', '--porcelain=v1', '--untracked-files=no'])
    revision = command(['git', 'rev-parse', 'HEAD'])
    return {
        'revision': revision,
        'tracked_worktree_dirty': None if status is None else bool(status),
        'content_manifest': manifest,
        'content_manifest_sha256': hashlib.sha256(encoded).hexdigest(),
        'identity_note': 'Manifest binds actual files, including uncommitted/new implementation; HEAD alone does not identify this deployment.',
    }


def image_identity(reference: str) -> dict:
    # Only selected non-secret Docker metadata leaves the process. Never output Config.Env.
    template = '{{json .Id}} {{json .Os}} {{json .Architecture}}'
    raw = command(['docker', 'image', 'inspect', '--format', template, reference])
    if raw is None:
        return {'reference': reference, 'available': False}
    try:
        image_id, system, arch = (json.loads(value) for value in raw.split())
        if not image_id.startswith('sha256:') or len(image_id) != 71:
            raise ValueError('invalid image ID')
    except (ValueError, TypeError):
        return {'reference': reference, 'available': False}
    return {'reference': reference, 'available': True, 'image_id': image_id, 'os': system, 'architecture': arch}
