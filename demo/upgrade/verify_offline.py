"""Repeat the Person 2 proofrun.v1 matrix using prepared, explicitly labeled fixes.

Run from the repository root: python3.12 -m demo.upgrade.verify_offline
Prepare dependency images first with: python3.12 -m src.main upgrade-demo --prepare
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from uuid import uuid4

from src.proofrun.contracts import CaseSpec, PatchProposal, SourceBundle
from src.proofrun.runner import run_comparison, verify_candidate


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    fixture = root / "demo/upgrade"
    output = root / ".commit-watch/proofrun-verifier" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True)
    digest = lambda data: hashlib.sha256(data).hexdigest()
    content = (fixture / "app.py").read_text()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    source = SourceBundle("https://github.com/Eman-Gon/ProofRun", revision, "demo/upgrade/app.py",
                          digest(content.encode()), content)
    case = CaseSpec("customer-nickname-v1", "customer-input-v1",
                    digest((fixture / "contract.json").read_bytes()), root, output)
    print("Running registered comparison in local Docker...", flush=True)
    comparison = run_comparison(case, source)
    results = {}
    for label, filename in (("narrow", "fixed_app.py"), ("permissive", "permissive_app.py")):
        print("Verifying prepared " + label + " candidate in both environments...", flush=True)
        proposal = PatchProposal(source.sha256, source.module, (fixture / filename).read_text(),
                                 "Prepared offline demonstration candidate",
                                 {"mode": "prepared", "gateway": "none", "fixture": filename})
        results[label] = verify_candidate(case, source, proposal)
    passed = (comparison.finding_status == "regression_reproduced"
              and comparison.execution_status == "completed"
              and results["narrow"].repair_status == "verified"
              and results["narrow"].complete
              and results["permissive"].repair_status == "rejected"
              and results["permissive"].complete
              and all(item["status"] == "passed" for item in results["permissive"].cases
                      if item["id"] != "nickname_object_rejected")
              and sum(item["status"] == "failed" and item["id"] == "nickname_object_rejected"
                      for item in results["permissive"].cases) == 2)
    summary = {
        "schema_version": "proofrun.v1", "experiment_passed": passed,
        "execution": {"target": "local", "backend": "docker", "platform": platform.platform()},
        "proposal_mode": "prepared", "input_mode": "synthetic", "revision": revision,
        "comparison": comparison.to_dict(),
        "narrow": results["narrow"].to_dict(), "permissive": results["permissive"].to_dict(),
    }
    path = output / "experiment.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"experiment_passed": passed, "finding": comparison.finding_status,
                      "narrow": results["narrow"].repair_status,
                      "permissive": results["permissive"].repair_status,
                      "evidence": str(path)}, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
