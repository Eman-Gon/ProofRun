#!/usr/bin/env python3
"""Collect a fresh registered worker run and hash-checked artifacts over a private route."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

from evidence_common import now, write_json

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
TERMINAL = {'completed', 'setup_failed', 'timed_out', 'interrupted'}


class CollectionError(Exception):
    """Messages are static/operator-safe; never include response bodies or tokens."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--output-dir', type=Path, required=True, help='New private directory; existing directories are rejected')
    parser.add_argument('--expected-target', choices=['local', 'crusoe'], default='local')
    parser.add_argument('--expected-repair-provider', choices=['openrouter', 'crusoe'], default='openrouter',
                        help='Require live provenance from this provider for the accepted repair')
    parser.add_argument('--repair', action='store_true', help='Opt in to at most two model proposals and independent verification')
    parser.add_argument('--timeout', type=int, default=600, help='Overall polling timeout from 1 to 600 seconds')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1 <= args.timeout <= 600:
        parser.error('Port must be 1024–65535 and timeout must be 1–600 seconds.')
    token = os.environ.get('PROOFRUN_WORKER_TOKEN', '')
    if not 32 <= len(token) <= 512 or not token.isascii() or any(character.isspace() for character in token):
        parser.error('Set PROOFRUN_WORKER_TOKEN privately to a valid ASCII bearer token; no token CLI argument is accepted.')
    destination = args.output_dir.resolve()
    try:
        destination.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        parser.error('Use a new output directory to preserve previous evidence.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    endpoint = f'http://127.0.0.1:{args.port}'
    summary = {'schema_version': 'proofrun.worker-setup.v1', 'kind': 'fresh_api_collection',
               'started_at': now(), 'endpoint': endpoint, 'expected_target': args.expected_target,
               'repair_requested': args.repair, 'status': 'collection_failed', 'artifacts': [],
               'limitations': ['Hosting must be corroborated with provider/host evidence.',
                               'This collector preserves the verifier verdict; it does not define acceptance tests.',
                               'Bad-fix rejection is a separate verifier experiment.']}

    def request(path, payload=None, accepted=(200,), limit=8 * 1024 * 1024):
        encoded = json.dumps(payload).encode() if payload is not None else None
        headers = {'Authorization': f'Bearer {token}'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(endpoint + path, data=encoded, headers=headers)
        try:
            with opener.open(req, timeout=15) as response:
                if response.status not in accepted:
                    raise CollectionError('Unexpected worker HTTP status.')
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise CollectionError('Worker response exceeded collection limit.')
                if token.encode() in data:
                    raise CollectionError('Worker response unexpectedly contained a credential; it was not saved.')
                return data
        except urllib.error.HTTPError as error:
            raise CollectionError(f'Worker HTTP request failed with status {error.code}.') from None
        except (OSError, ValueError):
            raise CollectionError('Worker connection unavailable or timed out.') from None

    def document(path, payload=None, accepted=(200,)):
        try:
            decoded = json.loads(request(path, payload, accepted, limit=1024 * 1024))
        except (ValueError, UnicodeError):
            raise CollectionError('Worker response was not JSON.') from None
        if not isinstance(decoded, dict) or decoded.get('schema_version') != 'proofrun.v1':
            raise CollectionError('Worker response used an unexpected schema.')
        return decoded

    try:
        case = document('/v1/cases/customer-nickname-v1')
        submission = case.get('submission')
        if not isinstance(submission, dict) or submission.get('case_id') != 'customer-nickname-v1':
            raise CollectionError('Registered submission is unavailable.')
        submission['job_key'] = 'worker-evidence-' + uuid4().hex
        submission['repair'] = {'enabled': args.repair, 'max_attempts': 2}
        write_json(destination / 'submission.json', submission)
        record = document('/v1/runs', submission, accepted=(202,))
        run_id = record.get('run_id')
        if not isinstance(run_id, str) or not ID.fullmatch(run_id):
            raise CollectionError('Worker run identity is invalid.')
        summary['run_id'] = run_id
        deadline = time.monotonic() + args.timeout
        while record.get('execution_status') not in TERMINAL:
            if time.monotonic() >= deadline:
                write_json(destination / 'last-record.json', record)
                raise CollectionError('Collection timed out; the submitted worker job may still be running.')
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            record = document('/v1/runs/' + run_id)
            if record.get('run_id') != run_id:
                raise CollectionError('Worker changed the run identity.')
        write_json(destination / 'record.json', record)
        artifacts = record.get('artifacts')
        if not isinstance(artifacts, list) or len(artifacts) > 32:
            raise CollectionError('Worker artifact manifest is invalid or exceeds bounds.')
        artifact_dir = destination / 'artifacts'
        artifact_dir.mkdir(mode=0o700)
        seen, total = set(), 0
        for item in artifacts:
            identifier, expected_hash, size = item.get('id'), item.get('sha256'), item.get('size_bytes')
            if (not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in seen
                    or not isinstance(expected_hash, str) or not SHA.fullmatch(expected_hash)
                    or type(size) is not int or not 0 <= size <= 8 * 1024 * 1024):
                raise CollectionError('Worker returned invalid or duplicate artifact metadata.')
            seen.add(identifier)
            total += size
            if total > 64 * 1024 * 1024:
                raise CollectionError('Worker artifacts exceeded the total collection limit.')
            data = request(f'/v1/runs/{run_id}/artifacts/{identifier}')
            if len(data) != size or hashlib.sha256(data).hexdigest() != expected_hash:
                raise CollectionError('Downloaded artifact failed its recorded size/hash binding.')
            descriptor = os.open(artifact_dir / identifier, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            summary['artifacts'].append({'id': identifier, 'sha256': expected_hash, 'size_bytes': size})
        execution = record.get('execution', {})
        proposal = record.get('proposal') or {}
        summary['execution_status'] = record.get('execution_status')
        summary['finding_status'] = record.get('finding_status')
        summary['repair_status'] = record.get('repair_status')
        summary['bindings'] = record.get('bindings', {})
        summary['execution'] = execution
        summary['gates'] = {
            'expected_execution_target': execution.get('target') == args.expected_target,
            'native_fresh_execution': execution.get('runner') == 'native' and execution.get('measurement') == 'fresh',
            'execution_completed': record.get('execution_status') == 'completed',
            'regression_reproduced': record.get('finding_status') == 'regression_reproduced',
            'artifacts_collected_and_bound': bool(artifacts),
            'requested_repair_verified': record.get('repair_status') == 'verified' if args.repair else None,
            'requested_live_' + args.expected_repair_provider + '_proposal': (proposal.get('mode') == 'live' and proposal.get('gateway') == args.expected_repair_provider
                and bool(proposal.get('model')) and bool(proposal.get('operation_id'))) if args.repair else None,
        }
        passed = all(value for value in summary['gates'].values() if value is not None)
        summary['status'] = 'requested_gates_passed' if passed else 'requested_gates_not_met'
        exit_code = 0 if passed else (2 if record.get('execution_status') != 'completed' else 1)
    except (CollectionError, KeyError, TypeError, AttributeError, OSError) as error:
        summary['error'] = str(error) if isinstance(error, CollectionError) else 'Worker response shape was invalid.'
        exit_code = 2
    summary['finished_at'] = now()
    write_json(destination / 'collection.json', summary)
    print('Fresh worker evidence collected; requested gates passed.' if exit_code == 0 else
          'Worker evidence saved; requested gates did not pass. See private collection.json.')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
