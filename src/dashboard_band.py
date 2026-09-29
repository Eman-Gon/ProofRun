"""Read-only BAND observations for the local dashboard; never grants repair authority."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
from uuid import UUID

from dotenv import dotenv_values

STAGES = ('connecting', 'sending_candidate', 'waiting_for_verifier', 'candidate_received',
          'verifying', 'sending_result', 'waiting_for_result', 'completed')


def band_observation(root):
    root = Path(root)
    settings = {**dotenv_values(root / '.env', interpolate=False), **os.environ}
    directories = [settings.get('PROOFRUN_ARTIFACT_DIR') or '.commit-watch/proofrun']
    if settings.get('PROOFRUN_BAND_HISTORY_DIR'):
        directories.append(settings['PROOFRUN_BAND_HISTORY_DIR'])
    result = {'enabled': str(settings.get('PROOFRUN_BAND_ENABLED', 'false')).lower() in ('true', '1'),
              'observation': None}
    records = []
    for directory in directories:
        path = Path(directory).expanduser()
        path = path if path.is_absolute() else root / path
        try:
            candidates = sorted(path.glob('run-*/record.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
            for candidate in candidates:
                if candidate.is_symlink() or candidate.stat().st_size > 2_000_000:
                    continue
                try:
                    record = json.loads(candidate.read_text())
                    row = _observation(record)
                    if row:
                        records.append(row)
                except (OSError, ValueError, TypeError, AttributeError, RecursionError):
                    continue
        except OSError:
            continue
    if records:
        result['observation'] = max(records, key=lambda row: datetime.fromisoformat(row['updatedAt']))
    return result


def _observation(record):
    if record.get('case_id') != 'customer-nickname-v1':
        return None
    coordination = record.get('coordination')
    if not isinstance(coordination, dict) or coordination.get('provider') != 'band':
        return None
    if not re.fullmatch(r'run-[a-f0-9]{32}', record.get('run_id', '')):
        return None
    updated = datetime.fromisoformat(record.get('updated_at', ''))
    if updated.tzinfo is None:
        return None
    if coordination.get('status') not in ('waiting', 'passed', 'blocked', 'unavailable'):
        return None
    if coordination.get('mode') not in ('live', 'mock'):
        return None
    # Explicit public fields only: worker records also contain private artifact
    # paths, request bodies and proposal context which do not belong in this UI.
    receipt = {key: coordination[key] for key in ('provider', 'mode', 'status')}
    for key in ('room_id', 'handoff_id', 'proposer_agent_id', 'verifier_agent_id',
                'request_message_id', 'result_message_id'):
        value = coordination.get(key)
        if isinstance(value, str):
            try:
                if str(UUID(value)) == value:
                    receipt[key] = value
            except ValueError:
                pass
    for key in ('candidate_sha256', 'contract_sha256', 'evidence_sha256', 'request_sha256'):
        value = coordination.get(key)
        if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value):
            receipt[key] = value
    if coordination.get('stage') in STAGES:
        receipt['stage'] = coordination['stage']
    stages = coordination.get('stages', [])
    receipt['stages'] = [stage for stage in STAGES if stage in stages] if isinstance(stages, list) else []
    return {'runId': record['run_id'], 'updatedAt': updated.isoformat(), 'receipt': receipt}
