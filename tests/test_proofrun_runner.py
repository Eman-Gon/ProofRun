"""Deterministic verifier boundary tests; Docker results below are explicitly mocked."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from src.proofrun.contracts import CaseSpec, PatchProposal, SourceBundle
from src.proofrun import runner
from src.sandbox import SandboxResult


@pytest.fixture
def inputs(tmp_path):
    root = runner.ROOT
    content = (root / "demo/upgrade/app.py").read_text()
    source = SourceBundle(
        runner.REPOSITORY,
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "demo/upgrade/app.py", runner._sha(content.encode()), content,
    )
    case = CaseSpec("customer-nickname-v1", "customer-input-v1",
                    runner._sha((root / "demo/upgrade/contract.json").read_bytes()), root, tmp_path)
    proposal = PatchProposal(source.sha256, source.module,
                             (root / "demo/upgrade/fixed_app.py").read_text(),
                             "Prepared narrow fix for unit tests", {"mode": "mock"})
    return case, source, proposal


def measured_probe(image, app, tests, *, expected_test_ids, expected_version):
    """Mock observations mirror the fixture; actual Docker evidence is separate."""
    content = app.read_text()
    results = []
    for test_id in expected_test_ids:
        outcome = {"id": test_id, "status": "pass"}
        if ("test_nickname_omitted" in test_id and "= None" not in content
                and expected_version == "2.8.2"):
            outcome.update(status="error", detail="ValidationError: nickname Field required")
        if "test_nickname_object_rejected" in test_id and "nickname: Any" in content:
            outcome.update(status="fail", detail="AssertionError: ValidationError not raised")
        results.append(outcome)
    record = {
        "schema_version": "proofrun.probe.v1", "version": expected_version,
        "tests": results, "tests_run": len(results), "integrity_errors": [],
        "source_sha256": runner._sha(app.read_bytes()),
        "harness_sha256": runner._sha((runner.ROOT / "demo/upgrade/probe_harness.py").read_bytes()),
        "tests_sha256": {path.name: runner._sha(path.read_bytes()) for path in tests},
    }
    failed = any(item["status"] != "pass" for item in results)
    return SandboxResult("fail" if failed else "pass", int(failed),
                         "PROOFRUN_PROBE_RESULT=" + json.dumps(record), 0.01)


@pytest.fixture
def probes():
    with patch.object(runner, "build_image", return_value="sha256:" + "a" * 64) as build, \
            patch.object(runner, "run_probe", side_effect=measured_probe) as probe:
        yield build, probe


def test_reproduction_is_separate_from_candidate_verdict(inputs, probes):
    case, source, proposal = inputs
    comparison = runner.run_comparison(case, source)
    assert (comparison.execution_status, comparison.finding_status) == ("completed", "regression_reproduced")
    assert len(comparison.executed_case_ids) == len(comparison.expected_case_ids) == 14
    assert comparison.failure_context["requirements_json"].encode() == (runner.ROOT / "demo/upgrade/contract.json").read_bytes()
    verification = runner.verify_candidate(case, source, proposal)
    assert verification.repair_status == "verified"
    assert verification.complete
    assert comparison.finding_status == "regression_reproduced"
    assert all(Path(path).is_file() for path in verification.artifacts.values())
    assert verification.bindings["candidate_sha256"] == runner._sha(proposal.replacement.encode())
    assert {"source_sha256", "tests_sha256", "contract_sha256", "environment_manifest_sha256", "verifier_sha256"} <= verification.bindings.keys()


def test_permissive_fix_passes_original_suite_but_fails_independent_control(inputs, probes):
    case, source, proposal = inputs
    bad = replace(proposal, replacement=source.content.replace("import Optional", "import Optional, Any")
                  .replace("nickname: Optional[str]", "nickname: Any = None"))
    result = runner.verify_candidate(case, source, bad)
    assert (result.execution_status, result.repair_status, result.complete) == ("completed", "rejected", True)
    assert all(item["status"] == "passed" for item in result.cases if item["kind"] == "original_suite")
    assert [item["id"] for item in result.cases if item["status"] == "failed"] == ["nickname_object_rejected"] * 2


@pytest.mark.parametrize("change", [
    {"base_sha256": "0" * 64}, {"allowed_path": "demo/upgrade/test_controls.py"},
    {"replacement": ""}, {"replacement": "import os\n"},
    {"replacement": "invalid python ("},
])
def test_rejects_stale_and_unsupported_proposals_without_execution(inputs, probes, change):
    case, source, proposal = inputs
    result = runner.verify_candidate(case, source, replace(proposal, **change))
    assert result.repair_status == "rejected" and not result.complete
    probes[1].assert_not_called()


@pytest.mark.parametrize("fragment", [
    "nickname: Optional[str] = __import__('os').system('true')",
    "nickname: object = None",
    "nickname: Optional[str] = lambda: None",
])
def test_candidate_cannot_execute_new_calls_or_unsupported_annotations(inputs, probes, fragment):
    case, source, proposal = inputs
    bad = replace(proposal, replacement=source.content.replace("nickname: Optional[str]", fragment))
    assert runner.verify_candidate(case, source, bad).repair_status == "rejected"
    probes[1].assert_not_called()


@pytest.mark.parametrize("changes", [
    {"expected_case_ids": ()}, {"expected_case_ids": ("nickname_omitted",) * 5},
    {"original_test_ids": ()}, {"contract_sha256": "0" * 64},
    {"model_name": "Unregistered"}, {"updated_version": "9.9"},
])
def test_wrong_contract_and_test_sets_never_execute(inputs, probes, changes):
    case, source, proposal = inputs
    result = runner.run_comparison(replace(case, **changes), source)
    assert (result.execution_status, result.finding_status) == ("setup_failed", "inconclusive")
    probes[1].assert_not_called()


@pytest.mark.parametrize("changes", [
    {"sha256": "0" * 64}, {"revision": "HEAD"},
    {"content": "changed"}, {"module": "../../etc/passwd"}, {"bundle_sha256": "0" * 64},
])
def test_stale_source_binding_never_executes(inputs, probes, changes):
    case, source, proposal = inputs
    result = runner.verify_candidate(case, replace(source, **changes), proposal)
    assert result.repair_status == "rejected" and not result.complete
    probes[1].assert_not_called()


@pytest.mark.parametrize("corruption", ["empty", "missing", "duplicate", "wrong", "skip", "version", "source", "tests", "null", "row", "detail", "harness", "integrity"])
def test_rejects_invalid_measured_matrix(inputs, probes, corruption):
    case, source, proposal = inputs
    def corrupt(*args, **kwargs):
        value = measured_probe(*args, **kwargs)
        record = runner.parse_probe_result(value.output_tail)
        if corruption == "empty":
            record["tests"] = []
        elif corruption == "missing":
            record["tests"].pop()
        elif corruption == "duplicate":
            record["tests"][-1] = record["tests"][0]
        elif corruption == "wrong":
            record["tests"][0]["id"] = "unexpected.test"
        elif corruption == "skip":
            record["tests"][0]["status"] = "skip"
        elif corruption == "version":
            record["version"] = "wrong"
        elif corruption == "source":
            record["source_sha256"] = "0" * 64
        elif corruption == "null":
            record["tests"] = None
        elif corruption == "row":
            record["tests"] = [1]
        elif corruption == "detail":
            record["tests"][0]["detail"] = []
        elif corruption == "harness":
            record["harness_sha256"] = "0" * 64
        elif corruption == "integrity":
            record["integrity_errors"] = None
        else:
            record["tests_sha256"] = {}
        return replace(value, output_tail="PROOFRUN_PROBE_RESULT=" + json.dumps(record))
    probes[1].side_effect = corrupt
    result = runner.verify_candidate(case, source, proposal)
    assert result.repair_status == "rejected"
    assert not result.complete


def test_timeout_and_setup_error_never_pass(inputs, probes):
    case, source, proposal = inputs
    for status in ("timeout", "error"):
        probes[1].side_effect = None
        probes[1].return_value = SandboxResult(status, None, "", 0.01)
        result = runner.verify_candidate(case, source, proposal)
        assert not result.complete and result.repair_status == "rejected"
        assert result.execution_status == ("timed_out" if status == "timeout" else "setup_failed")


def test_forged_success_cannot_hide_missing_evidence(inputs, probes):
    case, source, proposal = inputs
    probes[1].side_effect = None
    probes[1].return_value = SandboxResult("pass", 0, "Ran 7 tests in 0.1s\nOK", 0.01)
    result = runner.verify_candidate(case, source, proposal)
    assert not result.complete and result.repair_status == "rejected"


def test_snapshot_root_works_without_git_with_explicit_bundle(inputs, probes, tmp_path):
    case, source, proposal = inputs
    snapshot = tmp_path / "snapshot"
    shutil.copytree(runner.ROOT / "demo/upgrade", snapshot / "demo/upgrade")
    shutil.copytree(runner.ROOT / "sandbox", snapshot / "sandbox")
    source = replace(source, bundle_sha256=runner.source_bundle_hash(source.module, source.sha256))
    result = runner.verify_candidate(replace(case, root=snapshot), source, proposal)
    assert result.repair_status == "verified"
    (snapshot / "demo/upgrade/test_controls.py").write_text("# altered verifier\n")
    assert runner.verify_candidate(replace(case, root=snapshot), source, proposal).repair_status == "rejected"


def test_inputs_changed_during_execution_invalidate_acceptance(inputs, probes):
    case, source, proposal = inputs
    before = runner._snapshot(runner.ROOT)
    with patch.object(runner, "_snapshot", side_effect=[before, {**before, "changed": "yes"}]):
        result = runner.verify_candidate(case, source, proposal)
    assert result.repair_status == "rejected" and not result.complete


def test_unrelated_error_does_not_establish_regression(inputs, probes):
    case, source, _ = inputs
    def unrelated(*args, **kwargs):
        result = measured_probe(*args, **kwargs)
        return replace(result, output_tail=result.output_tail.replace("ValidationError: nickname Field required", "RuntimeError: boom"))
    probes[1].side_effect = unrelated
    assert runner.run_comparison(case, source).finding_status == "inconclusive"
