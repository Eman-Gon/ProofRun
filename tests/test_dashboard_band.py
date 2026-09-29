"""BAND observations must retain provenance without exposing worker-private fields."""
import json
from uuid import uuid4

from src.dashboard_band import band_observation


def save(root, *, folder='.commit-watch/proofrun', status='passed', updated='2026-09-29T22:00:00+00:00'):
    run_id = 'run-' + uuid4().hex
    path = root / folder / run_id / 'record.json'
    path.parent.mkdir(parents=True)
    record = {'run_id': run_id, 'case_id': 'customer-nickname-v1', 'updated_at': updated,
              '_artifact_paths': {'private': '/private/secret'},
              'coordination': {'provider': 'band', 'mode': 'live', 'status': status,
                               'room_id': str(uuid4()), 'candidate_sha256': 'a' * 64,
                               'stage': 'verifying', 'stages': ['candidate_received', 'verifying'],
                               'api_key': 'do-not-expose'}}
    path.write_text(json.dumps(record))
    return path, record


def test_empty_history_has_no_invented_pass(tmp_path, monkeypatch):
    monkeypatch.delenv('PROOFRUN_ARTIFACT_DIR', raising=False)
    monkeypatch.delenv('PROOFRUN_BAND_HISTORY_DIR', raising=False)
    monkeypatch.delenv('PROOFRUN_BAND_ENABLED', raising=False)
    assert band_observation(tmp_path) == {'enabled': False, 'observation': None}


def test_latest_observation_preserves_failure_and_only_public_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('PROOFRUN_ARTIFACT_DIR', '.commit-watch/proofrun')
    monkeypatch.delenv('PROOFRUN_BAND_HISTORY_DIR', raising=False)
    save(tmp_path)
    _, latest = save(tmp_path, status='unavailable', updated='2026-09-29T23:00:00+00:00')
    result = band_observation(tmp_path)
    assert result['observation']['runId'] == latest['run_id']
    assert result['observation']['receipt']['status'] == 'unavailable'
    assert result['observation']['receipt']['stage'] == 'verifying'
    assert 'completed' not in result['observation']['receipt']['stages']
    assert 'private' not in json.dumps(result)
    assert 'do-not-expose' not in json.dumps(result)


def test_explicit_history_directory_reads_real_records_and_labels_mock(tmp_path, monkeypatch):
    monkeypatch.setenv('PROOFRUN_ARTIFACT_DIR', '.commit-watch/proofrun')
    monkeypatch.delenv('PROOFRUN_BAND_HISTORY_DIR', raising=False)
    (tmp_path / '.env').write_text('PROOFRUN_BAND_HISTORY_DIR=saved-worker\n')
    path, record = save(tmp_path, folder='saved-worker')
    record['coordination']['mode'] = 'mock'
    path.write_text(json.dumps(record))
    assert band_observation(tmp_path)['observation']['receipt']['mode'] == 'mock'


def test_malformed_and_unrelated_records_are_not_reported(tmp_path, monkeypatch):
    monkeypatch.setenv('PROOFRUN_ARTIFACT_DIR', '.commit-watch/proofrun')
    monkeypatch.delenv('PROOFRUN_BAND_HISTORY_DIR', raising=False)
    path, record = save(tmp_path)
    for change in ({'case_id': 'other-case'}, {'updated_at': 'invalid'}, {'coordination': {}}, {'run_id': 'invalid'}):
        path.write_text(json.dumps({**record, **change}))
        assert band_observation(tmp_path)['observation'] is None
    path.write_text('{unfinished')
    assert band_observation(tmp_path)['observation'] is None
