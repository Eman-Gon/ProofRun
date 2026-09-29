#!/usr/bin/env python3
"""Capture observed host/source/images; never infer Crusoe hosting from a label."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import socket
import sys

from evidence_common import ROOT, command, image_identity, now, source_identity, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution-target', choices=['local', 'crusoe'], required=True)
    parser.add_argument('--crusoe-record', type=Path, help='Allowlisted VM identity copied from an authenticated Crusoe console/CLI lookup')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    provider_record = None
    if args.execution_target == 'crusoe':
        if platform.system() != 'Linux' or args.crusoe_record is None:
            parser.error('Crusoe evidence requires execution on Linux and an authenticated VM lookup record.')
        try:
            raw = args.crusoe_record.read_bytes()
            if len(raw) > 16384:
                raise ValueError('oversize record')
            provider_record = json.loads(raw)
            required = {'vm_id', 'project_id', 'location', 'instance_type', 'lookup_time', 'lookup_method'}
            if not isinstance(provider_record, dict) or set(provider_record) != required:
                raise ValueError('wrong keys')
            if any(not isinstance(value, str) or not value or len(value) > 256
                   or not re.fullmatch(r'[A-Za-z0-9_.:/+ @-]+', value)
                   for value in provider_record.values()):
                raise ValueError('invalid field')
            if provider_record['lookup_method'] not in {'crusoe_cli_authenticated', 'crusoe_console_authenticated'}:
                raise ValueError('lookup must be authenticated')
        except (OSError, ValueError, TypeError):
            parser.error('Invalid Crusoe VM record; see README for the exact six non-secret fields.')
    elif args.crusoe_record:
        parser.error('Local evidence must not include a Crusoe VM claim.')
    distributions = {}
    for package in ['requests', 'packaging']:
        try:
            distributions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            distributions[package] = None
    boot_path = Path('/proc/sys/kernel/random/boot_id')
    boot_hash = hashlib.sha256(boot_path.read_bytes()).hexdigest() if boot_path.is_file() else None
    try:
        distro = {key: value for key, value in platform.freedesktop_os_release().items()
                  if key in {'ID', 'VERSION_ID', 'PRETTY_NAME'}}
    except OSError:
        distro = None
    docker = command(['docker', 'info', '--format', '{{.ServerVersion}}'])
    evidence = {
        'schema_version': 'proofrun.worker-setup.v1', 'kind': 'host_snapshot', 'observed_at': now(),
        'execution_target': args.execution_target,
        'host': {'hostname': socket.gethostname(), 'os': platform.system(), 'release': platform.release(),
                 'architecture': platform.machine(), 'python': platform.python_version(), 'boot_id_sha256': boot_hash,
                 'distribution': distro},
        'docker_server_version': docker, 'worker_dependencies': distributions,
        'source': source_identity(),
        'images': {version: image_identity(f'secondlook-pydantic:{version}') for version in ['1.10.18', '2.8.2']},
        'crusoe_identity_record': provider_record,
        'provider_identity_basis': 'Operator supplied authenticated provider lookup; not independent cryptographic attestation.' if provider_record else None,
        'limitations': ['Host snapshot is supporting identity evidence. Pair it with fresh API run artifacts and image-preparation evidence; it is not a verification verdict.'],
    }
    ready = bool(docker) and all(item['available'] for item in evidence['images'].values())
    evidence['runtime_ready'] = ready
    write_json(args.output, evidence)
    print('Private host snapshot written; runtime ready.' if ready else 'Private host snapshot written; Docker or prepared images unavailable.')
    return 0 if ready else 2


if __name__ == '__main__':
    raise SystemExit(main())
