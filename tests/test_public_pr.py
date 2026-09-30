import base64
import copy
import hashlib
import http.client
import json
import threading

import pytest

from src.public_pr import candidate, can_propose, create_public_draft, PullRequestError
from src.public_repo import _python_findings
from src.dashboard import Dashboard, make_server


SOURCE = b'from pydantic import BaseModel\nclass Customer(BaseModel):\n    nickname: str | None  # preserve this comment\n'
COMMIT = 'a' * 40


def scan():
    return {'id': 'scan-one', 'status': 'completed', 'result': {
        'repository': 'other/public-project', 'commit': COMMIT,
        'findings': _python_findings('src/customer.py', SOURCE.decode(), []),
    }}


def fake_github(monkeypatch, *, push=True, stale=False, existing=False, changed_branch=False,
                source=SOURCE, selected_scan=None):
    calls = []
    repaired = candidate(source, (selected_scan or scan())['result']['findings'][0])

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
            return {'type': 'file', 'sha': 'b' * 40, 'content': base64.b64encode(source).decode()}
        if endpoint.endswith('/pulls'):
            return {'html_url': 'https://github.com/other/public-project/pull/7', 'number': 7, 'draft': False}
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
    assert pr['draft'] is False
    assert result['draft'] is False
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


def agent_finding(source, path='src/total.ts', **changes):
    return {'id': 'agent-review:abc', 'origin': 'agent', 'status': 'static_unverified',
            'sourceSha256': hashlib.sha256(source.decode('utf-8-sig').encode()).hexdigest(),
            'title': 'Preserve a zero total', 'file': path, 'line': 2, 'package': '',
            'beforeCode': 'return total || fallback;', 'afterCode': 'return total ?? fallback;',
            'explanation': 'A zero total takes the fallback branch.',
            'sourceUrl': f'https://github.com/other/public-project/blob/{COMMIT}/{path}#L2',
            'reproduction': 'Assert that total=0 returns zero.', **changes}


@pytest.mark.parametrize('stale', [False, True])
def test_pr_describes_only_evidence_bound_to_its_commit_and_patch(tmp_path, monkeypatch, stale):
    from src.repository_verification import digest
    source = b'export function value(total, fallback) {\n  return total || fallback;\n}\n'
    selected = scan()
    finding = agent_finding(source)
    finding['testEvidence'] = {'commit': 'b' * 40 if stale else COMMIT,
        'patchSha256': digest([finding['file'], finding['beforeCode'], finding['afterCode']]),
        'status': 'test_failure_reproduced', 'patchStatus': 'passes_selected_tests', 'testsSha256': 'c' * 64}
    selected['result']['findings'] = [finding]
    calls, _ = fake_github(monkeypatch, source=source, selected_scan=selected)
    create_public_draft(tmp_path, selected, 0)
    body = next(body['body'] for _, body in calls if body and 'draft' in body)
    assert ('**Selected tests passed:**' in body) is not stale
    assert 'tests were not run' not in body


def test_agent_patch_publishes_non_draft_pr_without_package_allowlist(tmp_path, monkeypatch):
    source = b'export function value(total, fallback) {\n  return total || fallback;\n}\n'
    selected = scan()
    selected['result']['findings'] = [agent_finding(source)]
    calls, repaired = fake_github(monkeypatch, source=source, selected_scan=selected)
    result = create_public_draft(tmp_path, selected, 0)
    assert repaired == source.replace(b'||', b'??')
    assert result['draft'] is False
    pr = next(body for _, body in calls if body and 'draft' in body)
    assert pr['title'] == 'Preserve a zero total'
    assert 'tests were not run' in pr['body']
    assert 'Python source parses' not in pr['body']
    assert 'Suggested validation (not executed)' in pr['body']


@pytest.mark.parametrize('changes', [
    {'sourceSha256': '0' * 64}, {'line': 1}, {'line': 0}, {'line': True},
    {'beforeCode': 'not in the source'}, {'afterCode': 'return total || fallback;'},
    {'afterCode': '\x00'}, {'afterCode': 'é' * 2001},
])
def test_agent_patch_rejects_changed_source_or_inexact_span(changes):
    source = b'function value(total, fallback) {\n  return total || fallback;\n}\n'
    with pytest.raises(PullRequestError, match='applied exactly'):
        candidate(source, agent_finding(source, **changes))


def test_agent_patch_preserves_utf8_bom_crlf_and_rejects_ambiguous_span():
    source = b'\xef\xbb\xbffunction value(total, fallback) {\r\n  return total || fallback;\r\n}\r\n'
    assert candidate(source, agent_finding(source)) == source.replace(b'||', b'??')
    repeated = b'// first\nreturn total || fallback; return total || fallback;\n'
    with pytest.raises(PullRequestError):
        candidate(repeated, agent_finding(repeated))


def test_agent_python_patch_must_parse_and_changed_original_is_rejected():
    source = b'def first(xs):\n    return xs[1]\n'
    finding = agent_finding(source, path='src/first.py', beforeCode='return xs[1]', afterCode='return xs[0]')
    assert candidate(source, finding) == source.replace(b'xs[1]', b'xs[0]')
    with pytest.raises(PullRequestError):
        candidate(source + b'# changed\n', finding)
    with pytest.raises(PullRequestError):
        candidate(source, {**finding, 'afterCode': 'return xs['})
    assert not can_propose({**finding, 'origin': 'unknown'})


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
        expected = {'number': 9, 'findingIndices': [0]}
        monkeypatch.setattr('src.dashboard.create_public_batch', lambda *args: expected)
        assert request({**body, 'findingIndex': 'all'}, token=False)[0] == 403
        assert request({**body, 'findingIndex': 'all'}) == (201, {'pullRequest': expected})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def batch_scan():
    source = SOURCE + b'    apartment: str | None\n'
    result = scan()
    result['result']['findings'] = _python_findings('src/customer.py', source.decode(), [])
    result['result']['findings'] += _python_findings('src/address.py', SOURCE.decode(), [])
    result['result']['findings'].append({'title': 'Manual review required'})
    return result, {'src/customer.py': source, 'src/address.py': SOURCE}


def test_combined_candidate_uses_original_coordinates_and_rejects_overlap():
    from src.public_pr import combined_candidate
    selected, sources = batch_scan()
    rows = selected['result']['findings'][:2]
    repaired = combined_candidate(sources['src/customer.py'], rows)
    assert repaired.count(b'= None') == 2
    original = b'x = 1\ny = 2\n'
    def finding(line, before, after):
        import hashlib
        return {'origin': 'agent', 'file': 'a.py', 'line': line, 'beforeCode': before,
                'afterCode': after, 'sourceSha256': hashlib.sha256(original).hexdigest()}
    rows = [finding(1, 'x = 1', 'x = 3\nz = 4'), finding(2, 'y = 2', 'y = 5')]
    assert combined_candidate(original, rows) == b'x = 3\nz = 4\ny = 5\n'
    with pytest.raises(PullRequestError, match='overlap'):
        combined_candidate(original, rows + [finding(1, 'x = 1\ny = 2', 'x = 6')])


@pytest.mark.parametrize('push', [True, False])
def test_batch_creates_one_atomic_commit_and_pr(tmp_path, monkeypatch, push):
    from src.public_pr import create_public_batch
    selected, sources = batch_scan()
    calls = []
    def run(root, args, *, payload=None):
        calls.append((args, payload))
        endpoint = next((x for x in args if x.startswith('repos/')), '')
        if args == ['api', 'user']:
            return {'login': 'writer'}
        if endpoint == 'repos/other/public-project':
            return {'private': False, 'default_branch': 'main', 'permissions': {'push': push}}
        if '/contents/' in endpoint:
            path = endpoint.split('/contents/')[1].split('?')[0]
            return {'type': 'file', 'sha': 'b' * 40, 'content': base64.b64encode(sources[path]).decode()}
        if '/pulls?' in endpoint or '/matching-refs/' in endpoint:
            return {'items': []}
        if endpoint.endswith('/git/ref/heads/main'):
            return {'object': {'sha': COMMIT}}
        if endpoint.endswith('/forks'):
            return {'full_name': 'writer/public-project', 'private': False, 'parent': {'full_name': 'other/public-project'}}
        if endpoint.endswith('/git/commits/' + COMMIT):
            return {'tree': {'sha': 'tree-base'}}
        if endpoint.endswith('/git/trees/tree-base'):
            return {'tree': [{'path': 'src', 'type': 'tree', 'sha': 'tree-src'}]}
        if endpoint.endswith('/git/trees/tree-src'):
            return {'tree': [{'path': 'customer.py', 'mode': '100755'}, {'path': 'address.py', 'mode': '100644'}]}
        if endpoint.endswith('/pulls'):
            return {'html_url': 'https://github.com/other/public-project/pull/7', 'number': 7}
        return {'sha': 'd' * 40}
    monkeypatch.setattr('src.public_pr._run', run)
    result = create_public_batch(tmp_path, selected)
    assert result['findingIndices'] == [0, 1, 2]
    bodies = [body for _, body in calls if body]
    blobs = [base64.b64decode(body['content']) for body in bodies if 'content' in body]
    assert sorted(blob.count(b'= None') for blob in blobs) == [1, 2]
    tree = next(body['tree'] for body in bodies if 'base_tree' in body)
    assert [(row['path'], row['mode']) for row in tree] == [('src/address.py', '100644'), ('src/customer.py', '100755')]
    assert len([body for body in bodies if 'parents' in body]) == 1
    assert len([body for body in bodies if 'ref' in body]) == 1
    pr = [body for body in bodies if 'head' in body]
    assert len(pr) == 1
    assert '1 findings without exact patches' in pr[0]['body']
    assert 'combined patch has not been tested' in pr[0]['body']


@pytest.mark.parametrize('mode', ['stale', 'existing', 'changed_branch'])
def test_batch_retry_and_staleness(tmp_path, monkeypatch, mode):
    from src.public_pr import create_public_batch
    calls, _ = fake_github(monkeypatch, **{mode: True})
    if mode == 'existing':
        assert create_public_batch(tmp_path, scan())['findingIndices'] == [0]
    else:
        with pytest.raises(PullRequestError):
            create_public_batch(tmp_path, scan())
    assert all(payload is None for _, payload in calls)


def test_batch_dashboard_tracks_all_included_findings(tmp_path, monkeypatch):
    dashboard = Dashboard(tmp_path)
    dashboard.repository_scans = [scan()]
    expected = {'number': 7, 'findingIndices': [0, 1]}
    monkeypatch.setattr('src.dashboard.create_public_batch', lambda *args: expected)
    assert dashboard.create_public_pull_request('scan-one', 'all')[0] == 201
    assert dashboard.repository_scans[0]['pullRequests'] == {'all': expected, '0': expected, '1': expected}
