#!/usr/bin/env python3
"""Build fixture images through the verifier's implementation and bind their inputs."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from evidence_common import ROOT, image_identity, now, sha256, source_identity, write_json
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Rebuild here rather than accepting a stale legacy tag. Test jobs never build/pull.
    from src.upgrade_sandbox import build_image
    evidence = {'schema_version': 'proofrun.worker-setup.v1', 'kind': 'image_preparation',
                'observed_at': now(), 'source': source_identity(), 'images': {},
                'dockerfile_sha256': sha256(ROOT / 'sandbox/upgrade.Dockerfile')}
    for name, version in [('old', '1.10.18'), ('new', '2.8.2')]:
        requirements = ROOT / 'demo/upgrade' / f'requirements-{name}.txt'
        try:
            image_id = build_image(requirements, f'secondlook-pydantic:{version}', rebuild=True)
            identity = image_identity(image_id)
            if not identity.get('available'):
                raise RuntimeError('built image cannot be inspected')
        except Exception:
            # Provider/daemon exception strings are never emitted into handoff evidence.
            evidence['status'] = 'setup_failed'
            evidence['failed_environment'] = name
            write_json(args.output, evidence)
            print('Image preparation failed; inspect Docker/package access on the worker.', file=sys.stderr)
            return 2
        evidence['images'][name] = {**identity, 'expected_pydantic_version': version,
                                    'requirements_sha256': sha256(requirements)}
    evidence['status'] = 'prepared'
    evidence['limitations'] = ['Images prepared; this is not application execution or a repair verdict. Runtime versions are checked by the verifier.']
    write_json(args.output, evidence)
    print('Prepared pinned fixture images; private evidence written.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
