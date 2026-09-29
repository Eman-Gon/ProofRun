"""One explicit live Crusoe proposal plus native verification; no provider fallback.

This adapter acceptance run bypasses primary-provider selection deliberately.
It does not claim an OpenRouter rejection or an end-to-end two-provider run.
Load private settings into the environment before invoking this script.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.proofrun.config import RepairConfig
from src.proofrun.repair import CrusoeRepairClient
from src.proofrun.service import RunService, CASE_ID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    client = CrusoeRepairClient(RepairConfig.crusoe_from_env())
    service = RunService(ROOT, directory / 'worker', propose=client.propose_patch)
    try:
        payload = service.get_case(CASE_ID)['submission']
        payload['job_key'] = 'crusoe-adapter-acceptance'
        payload['repair'] = {'enabled': True, 'max_attempts': 1}
        run, _ = service.submit(payload)
        print('Started native comparison and one live Crusoe proposal.', flush=True)
        while run['execution_status'] in {'queued', 'running'}:
            time.sleep(1)
            run = service.get_run(run['run_id'])
        artifacts = []
        for item in run['artifacts']:
            data, _ = service.artifact(run['run_id'], item['id'])
            assert len(data) == item['size_bytes']
            assert hashlib.sha256(data).hexdigest() == item['sha256']
            assert client.config.api_key.encode() not in data
            artifacts.append(item['id'])
        proposal = run.get('proposal') or {}
        passed = (run['execution_status'] == 'completed' and run['finding_status'] == 'regression_reproduced'
                  and run['repair_status'] == 'verified' and proposal.get('gateway') == 'crusoe'
                  and proposal.get('mode') == 'live' and bool(proposal.get('operation_id')))
        receipt = {'scope': 'Direct live Crusoe adapter acceptance with synthetic inputs and local Docker; not a two-provider retry run',
                   'run_id': run['run_id'], 'execution_status': run['execution_status'],
                   'finding_status': run['finding_status'], 'repair_status': run['repair_status'],
                   'proposal': proposal, 'hash_checked_artifacts': artifacts, 'passed': passed,
                   'repaired_checks_passed': sum(c['status'] == 'passed' for c in run['cases'] if c.get('stage','').startswith('repaired_')),
                   'limitations': run['limitations']}
        for name, document in [('receipt.json', receipt), ('run.json', run)]:
            data = json.dumps(document, indent=2)
            assert client.config.api_key not in data
            path = directory / name
            path.write_text(data + '\n'); path.chmod(0o600)
        print(json.dumps(receipt), flush=True)
        return 0 if passed else 2
    finally:
        service.close()


if __name__ == '__main__':
    raise SystemExit(main())
