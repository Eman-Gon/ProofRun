#!/usr/bin/env python3
"""Check the local end of the private worker route without printing its token."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

from evidence_common import now, write_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be from 1024 to 65535.')
    token = os.environ.get('PROOFRUN_WORKER_TOKEN') or getpass.getpass('Worker token (hidden): ')
    if len(token) < 32 or len(token) > 512 or any(character.isspace() for character in token):
        parser.error('Provide a valid worker token privately; minimum length is 32.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    cases = [
        ('health', '/health', None, 200),
        ('unauthenticated_registry', '/v1/cases/customer-nickname-v1', None, 401),
        ('wrong_token_registry', '/v1/cases/customer-nickname-v1', 'invalid-test-token', 401),
        ('authenticated_registry', '/v1/cases/customer-nickname-v1', token, 200),
    ]
    results = []
    for name, path, bearer, expected in cases:
        request = urllib.request.Request(f'http://127.0.0.1:{args.port}{path}')
        if bearer:
            request.add_header('Authorization', f'Bearer {bearer}')
        status, body = None, b''
        try:
            with opener.open(request, timeout=15) as response:
                status, body = response.status, response.read(262145)
        except urllib.error.HTTPError as error:
            status = error.code
        except (OSError, ValueError):
            pass
        valid_json = False
        if body and len(body) <= 262144:
            try:
                decoded = json.loads(body)
                valid_json = isinstance(decoded, dict)
            except (ValueError, UnicodeError):
                pass
        passed = status == expected and (expected != 200 or valid_json)
        results.append({'check': name, 'http_status': status, 'expected_http_status': expected,
                        'passed': passed, 'response_sha256': hashlib.sha256(body).hexdigest() if valid_json else None})
    passed = all(item['passed'] for item in results)
    write_json(args.output, {'schema_version': 'proofrun.worker-setup.v1', 'kind': 'private_route_check',
                             'observed_at': now(), 'endpoint': f'http://127.0.0.1:{args.port}',
                             'checks': results, 'passed': passed,
                             'limitations': ['This checks reachability/authentication only; it does not establish remote host identity or application execution.']})
    print('Private worker route checks passed.' if passed else 'Private worker route checks failed; see private evidence.')
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
