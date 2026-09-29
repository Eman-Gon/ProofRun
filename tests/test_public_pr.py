import base64
import copy
import http.client
import json
import threading

import pytest

from src.public_pr import candidate, create_public_draft, PullRequestError
from src.public_repo import _python_findings
from src.dashboard import Dashboard, make_server


SOURCE = b'from pydantic import BaseModel\nclass Customer(BaseModel):\n    nickname: str | None  # preserve this comment\n'
COMMIT = 'a' * 40


def scan():
    return {'id': 'scan-one', 'status': 'completed', 'result': {
        'repository': 'other/public-project', 'commit': COMMIT,
        'findings': _python_findings('src/customer.py', SOURCE.decode(), []),
    }}


def fake_github(monkeypatch, *, push=True, stale=False, existing=False, changed_branch=False):
    calls = []
    repaired = candidate(SOURCE, scan()['result']['findings'][0])

    def run(root, args, *, payload=None):
        calls.append((args, payload))
        endpoint = next((x for x in args if x.startswith('repos/')), '')
        if args == ['api', 'user']:
            return {'login': 'writer'}
        if endpoint == 'repos/other/public-project':
            return {'private': False, 'default_branch': 'main', 'permissions': {'push': push}}
        if '/pulls?' in endpoint:
            return {'items': [{'html_url': 'https://github.com/other/public-project/pull/7', 'number': 7,
                               'state': 'open', 'draft': True}] if existing else []}
        if endpoint.endswith('/git/ref/heads/main'):
            return {'object': {'sha': 'c' * 40 if stale else COMMIT}}
        if endpoint.endswith('/forks'):
            return {'full_name': 'writer/public-project', 'private': False,
                    'parent': {'full_name': 'other/public-project'}}
        if '/matching-refs/' in endpoint:
            return {'items': [{'ref': 'refs/heads/' + endpoint.split('/heads/')[1],
                               'object': {'sha': 'd' * 40}}] if changed_branch else []}
        if '/commits/' in endpoint:
            return {'parents': [{'sha': COMMIT}], 'files': [{'filename': 'unrelated.py'}]}
        if '/contents/' in endpoint and payload is None:
            return {'type': 'file', 'sha': 'b' * 40, 'content': base64.b64encode(SOURCE).decode()}
        if endpoint.endswith('/pulls'):
            return {'html_url': 'https://github.com/other/public-project/pull/7', 'number': 7, 'draft': True}
        return {}

    monkeypatch.setattr('src.public_pr._run', run)
    return calls, repaired


@pytest.mark.parametrize('push', [True, False])
def test_public_repository_pr_targets_scanned_repository(tmp_path, monkeypatch, push):
    calls, repaired = fake_github(monkeypatch, push=push)
    result = create_public_draft(tmp_path, scan(), 0)
    assert result['repository'] == 'other/public-project'
    updates = [(args, body) for args, body in calls if body and 'content' in body]
    assert len(updates) == 1
    assert base64.b64decode(updates[0][1]['content']) == repaired
    destination = 'other' if push else 'writer'
    assert f'repos/{destination}/public-project/contents/src/customer.py' in updates[0][0]
    pr = next(body for args, body in calls if body and 'draft' in body)
    assert pr['draft'] is True
    assert pr['head'].startswith(destination + ':codex/proofrun-')
    assert 'tests were not run' in pr['body']
    assert bool([args for args, _ in calls if any(x.endswith('/forks') for x in args)]) == (not push)


def test_stale_scan_cannot_publish(tmp_path, monkeypatch):
    calls, _ = fake_github(monkeypatch, stale=True)
    with pytest.raises(PullRequestError, match='changed since'):
        create_public_draft(tmp_path, scan(), 0)
    assert all(payload is None for _, payload in calls)


def test_retry_reuses_existing_pr_without_writes(tmp_path, monkeypatch):
    calls, _ = fake_github(monkeypatch, existing=True)
    assert create_public_draft(tmp_path, scan(), 0)['number'] == 7
    assert all(payload is None for _, payload in calls)


def test_retry_rejects_unrelated_branch_changes(tmp_path, monkeypatch):
    calls, _ = fake_github(monkeypatch, changed_branch=True)
    with pytest.raises(PullRequestError, match='other changes'):
        create_public_draft(tmp_path, scan(), 0)
    assert all(payload is None for _, payload in calls)


def test_candidate_preserves_comment_and_rejects_tampering():
    finding = scan()['result']['findings'][0]
    assert candidate(SOURCE, finding).endswith(b'nickname: str | None = None  # preserve this comment\n')
    tampered = copy.deepcopy(finding)
    tampered['afterCode'] = 'nickname: str | None = dangerous()'
    with pytest.raises(PullRequestError):
        candidate(SOURCE, tampered)


def test_pandas_candidate_preserves_other_calls():
    source = b'import pandas as pd\nx = pd.date_range("2024", periods=2, freq="H")\ny = "H"\n'
    finding = _python_findings('app.py', source.decode(), [])[0]
    assert candidate(source, finding) == source.replace(b'freq="H"', b"freq='h'")


@pytest.mark.parametrize('index', [-1, True, 5])
def test_bad_finding_has_no_github_calls(tmp_path, monkeypatch, index):
    calls, _ = fake_github(monkeypatch)
    with pytest.raises(PullRequestError):
        create_public_draft(tmp_path, scan(), index)
    assert not calls


def test_dashboard_dispatch_and_persistence(tmp_path, monkeypatch):
    dashboard = Dashboard(tmp_path)
    dashboard.repository_scans = [scan()]
    expected = {'number': 7, 'url': 'https://github.com/other/public-project/pull/7'}
    monkeypatch.setattr('src.dashboard.create_public_draft', lambda *args: expected)
    assert dashboard.create_public_pull_request('missing', 0)[0] == 404
    assert dashboard.create_public_pull_request('scan-one', 0) == (201, {'pullRequest': expected})
    assert dashboard.repository_scans[0]['pullRequests']['0'] == expected
    with dashboard.public_pr_lock:
        assert dashboard.create_public_pull_request('scan-one', 0)[0] == 409


def test_http_public_pr_requires_csrf_and_dispatches_scan(tmp_path, monkeypatch):
    fake_github(monkeypatch)
    server = make_server(root=tmp_path, port=0)
    server.dashboard.repository_scans = [scan()]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def request(payload, token=True):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{server.server_port}'}
            if token:
                headers['X-CSRF-Token'] = server.dashboard.token
            conn.request('POST', '/api/pull-requests', json.dumps(payload), headers)
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result

        body = {'scanId': 'scan-one', 'findingIndex': 0}
        assert request(body, token=False)[0] == 403
        assert request({**body, 'findingIndex': True})[0] == 400
        assert request({**body, 'caseId': 'pydantic'})[0] == 400
        status, response = request(body)
        assert status == 201
        assert response['pullRequest']['repository'] == 'other/public-project'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
