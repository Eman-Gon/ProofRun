"""Regression tests for observed policy gaps; all runtime observations are fake."""
import hashlib
from pathlib import Path

import pytest

from src.proofrun.release_contracts import digest
from src.proofrun.release_investigation import Investigation
from src.proofrun.release_repository import SourceTools


BASE = "a" * 40
CANDIDATE = "b" * 40
BASE_IMAGE = "sha256:" + "1" * 64
CANDIDATE_IMAGE = "sha256:" + "2" * 64


def request(name, path="/items", requirement="preserve-items"):
    return {"name": name, "requirement_id": requirement,
            "hypothesis": "Explore a response compatibility boundary.",
            "steps": [{"method": "GET", "path": path}]}


def observed(value, status=200):
    return {"execution_status": "completed", "complete": True,
            "observations": [{"status": status, "json": {"value": value}}],
            "environment": {"mode": "synthetic_fake_runtime"}}


class Runtime:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = {}

    def run_probe(self, root, image, config, steps, deadline):
        key = (str(root), steps[0]["path"])
        self.calls[key] = self.calls.get(key, 0) + 1
        return self.behavior(root, image, steps, self.calls[key])

    def run_tests(self, *args):
        return {"execution_status": "completed", "complete": True, "test_status": "passed",
                "exit_code": 0, "output_tail": "Ran 1 tests\nOK\n",
                "environment": {"mode": "synthetic_fake_runtime"}}


def investigation(tmp_path, runtime):
    target = {"id": "synthetic", "name": "Synthetic policy example", "contract_hash": "c" * 64,
        "images": {BASE: BASE_IMAGE, CANDIDATE: CANDIDATE_IMAGE},
        "requirements": [{"id": "preserve-items", "kind": "preserve_response",
                          "path_prefix": "/items", "methods": ["GET"], "description": "Preserve the response."}],
        "test_paths": ["tests.py"], "repair_paths": ["app.py"], "exclude_paths": [],
        "test_success_pattern": r"Ran [1-9][0-9]* tests", "runtime": {}}
    parameters = {"target_id": "synthetic", "baseline_revision": BASE, "candidate_revision": CANDIDATE,
                  "budget_seconds": 180, "repair": True, "benefit": "Needed release improvements"}
    instance = Investigation(target, parameters, tmp_path / "evidence", runtime=runtime)
    instance.directory.mkdir()
    for name, content in (("baseline", "value = True\n"), ("candidate", "value = 1\n")):
        root = instance.directory / name
        root.mkdir()
        (root / "app.py").write_text(content)
        (root / "tests.py").write_text("# Protected synthetic original suite\n")
        instance.roots[name] = root
        entries = [{"path": file.name, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
                   for file in sorted(root.iterdir())]
        instance.manifests[name] = {"files": entries, "tree_hash": digest(entries)}
        instance.tests[name] = runtime.run_tests()
    instance.protected = {"tests.py"}
    instance.sources = SourceTools(instance.roots, instance.manifests)
    return instance


def test_boolean_and_number_responses_are_different_json_values(tmp_path):
    runtime = Runtime(lambda root, image, steps, count: observed(True if image == BASE_IMAGE else 1))
    run = investigation(tmp_path, runtime)
    evidence = run.probe(request("Boolean-to-number boundary"))
    assert evidence["status"] == "regression"
    assert run.findings[0]["status"] == "confirmed"


def test_type_changes_between_replays_are_unstable(tmp_path):
    def responses(root, image, steps, count):
        return observed("stable" if image == BASE_IMAGE else (True if count == 1 else 1))

    run = investigation(tmp_path, Runtime(responses))
    evidence = run.probe(request("Unstable response type"))
    assert evidence["status"] == "inconclusive"
    assert run.findings[0]["status"] == "inconclusive"


def test_diff_of_nonexistent_path_does_not_count_as_source_inspection(tmp_path):
    run = investigation(tmp_path, Runtime(lambda *_: observed("same")))
    with pytest.raises(ValueError):
        run.tool("diff", {"path": "not-in-either-snapshot.py"})
    assert not run.inspected


def test_repair_cannot_be_verified_from_only_rejected_request_behaviors(tmp_path):
    def responses(root, image, steps, count):
        if root.name.startswith("repair-"):
            return observed("invalid", 400)
        if steps[0]["path"].endswith("failure") and image == CANDIDATE_IMAGE:
            return observed("changed rejection", 422)
        return observed("invalid", 400)

    run = investigation(tmp_path, Runtime(responses))
    run.tool("read_file", {"revision": "candidate", "path": "app.py"})
    assert run.probe(request("Rejection changed", "/items?case=failure"))["status"] == "regression"
    assert run.probe(request("Independent rejection preserved", "/items?case=control"))["status"] == "preserved"
    record = run.repair({"finding_id": "finding-1", "changes": [{"path": "app.py", "content": "value = True\n"}],
                         "rationale": "Restore the observed rejection response."})
    assert record["status"] != "verified_candidate"
