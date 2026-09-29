"""Registered, offline Pydantic comparison and independent repair verification.

This module consumes Person 1's proofrun.v1 boundary. It never imports or executes
candidate Python on the worker host; only bounded Docker probes execute it.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from uuid import uuid4

from .contracts import CaseSpec, ComparisonEvidence, PatchProposal, SourceBundle, VerificationEvidence
from ..sandbox import SandboxError
from ..upgrade_sandbox import build_image, environment_fingerprint, parse_probe_result, run_probe

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/Eman-Gon/ProofRun"
CONTROL_IDS = {
    "test_controls.TestControls.test_nickname_omitted": "nickname_omitted",
    "test_controls.TestControls.test_nickname_null": "nickname_null",
    "test_controls.TestControls.test_nickname_string_unchanged": "nickname_string",
    "test_controls.TestControls.test_nickname_object_rejected": "nickname_object_rejected",
    "test_controls.TestControls.test_required_name_rejected": "required_name_rejected",
}
ORIGINAL_IDS = (
    "test_existing.TestExisting.test_explicit_nickname",
    "test_existing.TestExisting.test_explicit_none",
)
TEST_IDS = (*ORIGINAL_IDS, *CONTROL_IDS)
LIMITATIONS = [
    "One registered synthetic Customer.nickname case; Docker execution has no network or model credentials.",
    "Candidate edits are limited to the nickname annotation/default and an optional typing.Any import.",
    "Passing establishes the declared checks only; it does not prove universal correctness.",
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_hash(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def source_bundle_hash(module: str, source_sha256: str) -> str:
    return _json_hash({"module": module, "sha256": source_sha256})


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _snapshot(root: Path) -> dict[str, str]:
    paths = ["demo/upgrade/" + name for name in (
        "app.py", "contract.json", "test_existing.py", "test_controls.py",
        "requirements-old.txt", "requirements-new.txt",
    )] + ["sandbox/upgrade.Dockerfile"]
    result = {name: _sha((root / name).read_bytes()) for name in paths}
    for name in ("demo/upgrade/probe_harness.py", "src/proofrun/runner.py", "src/upgrade_sandbox.py"):
        result[name] = _sha((ROOT / name).read_bytes())
    return result


def _validate(case: CaseSpec, source: SourceBundle) -> tuple[dict, dict, dict]:
    if not isinstance(source.content, str) or not isinstance(source.revision, str):
        raise ValueError("Source content and revision must be strings.")
    if not isinstance(case.expected_case_ids, (tuple, list)) or not isinstance(case.original_test_ids, (tuple, list)):
        raise ValueError("Expected cases and original tests must be explicit lists.")
    root = Path(case.root).resolve()
    contract_path = root / "demo/upgrade/contract.json"
    contract_bytes = contract_path.read_bytes()
    contract = json.loads(contract_bytes)
    for relative in ("demo/upgrade/contract.json", "demo/upgrade/test_existing.py",
                     "demo/upgrade/test_controls.py", "demo/upgrade/requirements-old.txt",
                     "demo/upgrade/requirements-new.txt", "sandbox/upgrade.Dockerfile"):
        if (root / relative).read_bytes() != (ROOT / relative).read_bytes():
            raise ValueError("Snapshot differs from registered verification inputs: " + relative)
    for name in ("case_id", "contract_id", "model_name", "field_name", "allowed_path",
                 "baseline_version", "updated_version"):
        if getattr(case, name) != contract[name]:
            raise ValueError("Unsupported registered case configuration: " + name)
    if (case.case_id != "customer-nickname-v1" or case.model_name != "Customer"
            or case.field_name != "nickname" or case.allowed_path != "demo/upgrade/app.py"
            or (case.baseline_version, case.updated_version) != ("1.10.18", "2.8.2")):
        raise ValueError("The requested case is not registered.")
    if (tuple(case.expected_case_ids) != tuple(CONTROL_IDS.values())
            or tuple(case.original_test_ids) != ORIGINAL_IDS
            or tuple(contract["original_test_ids"]) != ORIGINAL_IDS
            or tuple(item["id"] for item in contract["cases"]) != tuple(CONTROL_IDS.values())):
        raise ValueError("Expected test set differs from the complete registered matrix.")
    if case.contract_sha256 != _sha(contract_bytes):
        raise ValueError("Stale contract binding.")
    if (source.repository != REPOSITORY or source.module != case.allowed_path
            or not re.fullmatch(r"[0-9a-f]{40}", source.revision)):
        raise ValueError("Unsupported source repository, module, or revision.")
    content = source.content.encode("utf-8")
    if len(content) > 65536 or source.sha256 != _sha(content):
        raise ValueError("Stale source content binding.")
    if (content != (root / case.allowed_path).read_bytes()
            or content != (ROOT / case.allowed_path).read_bytes()):
        raise ValueError("Source differs from the approved registered application.")
    if source.bundle_sha256 is not None:
        if source.bundle_sha256 != source_bundle_hash(source.module, source.sha256):
            raise ValueError("Stale source bundle binding.")
    else:
        revision = subprocess.run(
            ["git", "show", source.revision + ":" + source.module], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
        )
        if revision.returncode:
            raise ValueError("Source revision is unavailable in this worker checkout.")
        if revision.stdout != content:
            raise ValueError("Uncommitted source requires an explicit source bundle hash.")
    snapshot = _snapshot(root)
    bindings = {
        "revision": source.revision, "source_sha256": source.sha256,
        "contract_sha256": case.contract_sha256,
        "tests_sha256": _json_hash({key: value for key, value in snapshot.items()
                                   if key.endswith(("test_existing.py", "test_controls.py"))}),
        "verifier_sha256": _json_hash(snapshot),
    }
    if source.bundle_sha256:
        bindings["bundle_sha256"] = source.bundle_sha256
    return contract, snapshot, bindings


def _candidate(source: str, proposal: PatchProposal, case: CaseSpec, base_sha256: str) -> str:
    if proposal.base_sha256 != base_sha256 or proposal.allowed_path != case.allowed_path:
        raise ValueError("Candidate base hash or allowed path does not match the approved source.")
    replacement = proposal.replacement
    if not isinstance(replacement, str) or not replacement.strip() or len(replacement.encode()) > 65536:
        raise ValueError("Candidate must be a nonempty bounded application replacement.")

    def skeleton(text: str) -> str:
        tree = ast.parse(text)
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
            if isinstance(tree.body[0].value.value, str):
                tree.body.pop(0)
        found = 0
        normalized = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "typing" and node.level == 0:
                if any(name.name not in {"Optional", "Any"} or name.asname for name in node.names):
                    raise ValueError("Candidate includes an unsupported import.")
                node.names = [name for name in node.names if name.name != "Any"]
                if not node.names:
                    continue
            if isinstance(node, ast.ClassDef) and node.name == case.model_name:
                for field in node.body:
                    if (isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
                            and field.target.id == case.field_name):
                        found += 1
                        allowed_nodes = (ast.Name, ast.Subscript, ast.BinOp, ast.BitOr, ast.Load, ast.Constant)
                        for part in ast.walk(field.annotation):
                            if not isinstance(part, allowed_nodes):
                                raise ValueError("Candidate field annotation includes unsupported executable code.")
                            if isinstance(part, ast.Name) and part.id not in {"Optional", "Any", "str"}:
                                raise ValueError("Candidate field annotation is unsupported.")
                            if isinstance(part, ast.Constant) and part.value is not None:
                                raise ValueError("Candidate field annotation is unsupported.")
                        if field.value is not None and not isinstance(field.value, ast.Constant):
                            raise ValueError("Candidate field default includes unsupported executable code.")
                        field.annotation = ast.Name(id="APPROVED_FIELD", ctx=ast.Load())
                        field.value = None
            normalized.append(node)
        if found != 1:
            raise ValueError("Candidate must retain exactly one configured model field.")
        tree.body = normalized
        return ast.dump(tree, include_attributes=False)

    try:
        if skeleton(source) != skeleton(replacement):
            raise ValueError("Candidate changes code outside the permitted model field or imports.")
    except SyntaxError:
        raise ValueError("Candidate is not valid Python source.") from None
    return replacement


def _directory(case: CaseSpec, prefix: str) -> Path:
    parent = Path(case.artifact_dir).resolve()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = parent / (prefix + "-" + uuid4().hex)
    directory.mkdir(mode=0o700)
    return directory


def _execute(case: CaseSpec, content: str, snapshot: dict, directory: Path,
             repaired: bool) -> tuple[dict, list, dict, str]:
    environments, observations, raw = {}, [], {}
    execution = "completed"
    with tempfile.TemporaryDirectory(prefix="proofrun-verified-") as temporary:
        staged = Path(temporary)
        app = staged / "app.py"
        app.write_bytes(content.encode())
        tests = []
        for name in ("test_existing.py", "test_controls.py"):
            path = staged / name
            path.write_bytes((Path(case.root) / "demo/upgrade" / name).read_bytes())
            if _sha(path.read_bytes()) != snapshot["demo/upgrade/" + name]:
                raise ValueError("Test changed while staging the verification matrix.")
            tests.append(path)
        for label, version, stage in (
            ("old", case.baseline_version, "repaired_baseline" if repaired else "baseline"),
            ("new", case.updated_version, "repaired_updated" if repaired else "updated"),
        ):
            requirements = Path(case.root) / "demo/upgrade" / ("requirements-" + label + ".txt")
            image = build_image(requirements, "secondlook-pydantic:" + version)
            if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise ValueError("Environment did not resolve to an immutable image ID.")
            environments[stage] = {
                "image_id": image, "expected_version": version,
                "requirements_sha256": snapshot["demo/upgrade/requirements-" + label + ".txt"],
                "build_inputs_sha256": environment_fingerprint(requirements),
                "command": "exec python /probe/_proofrun_probe.py",
                "network": "none", "execution_backend": "docker",
            }
            result = run_probe(image, app, tests, expected_test_ids=list(TEST_IDS), expected_version=version)
            raw[stage] = asdict(result)
            (directory / (stage + ".txt")).write_text(result.output_tail + "\n")
            measured = parse_probe_result(result.output_tail)
            if result.status == "timeout":
                execution = "timed_out"
            elif result.status == "error" and execution != "timed_out":
                execution = "setup_failed"
            if measured:
                environments[stage]["observed_version"] = measured.get("version")
                expected_hashes = {path.name: _sha(path.read_bytes()) for path in tests}
                rows = measured.get("tests")
                if not isinstance(rows, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or item.get("status") not in ("pass", "fail", "error")
                    or not isinstance(item.get("detail", ""), str) for item in rows
                ):
                    execution = "setup_failed"
                    continue
                ids = [item["id"] for item in rows]
                passed = bool(rows) and all(item["status"] == "pass" for item in rows)
                if (measured.get("source_sha256") != _sha(content.encode())
                        or measured.get("tests_sha256") != expected_hashes
                        or measured.get("harness_sha256") != snapshot["demo/upgrade/probe_harness.py"]
                        or Counter(ids) != Counter(TEST_IDS)
                        or measured.get("tests_run") != len(TEST_IDS)
                        or measured.get("integrity_errors") != []
                        or measured.get("version") != version
                        or result.status not in ("pass", "fail")
                        or result.exit_code not in (0, 1)
                        or (result.exit_code == 0) != passed
                        or (result.status == "pass") != passed):
                    execution = "setup_failed"
                requirements_data = json.loads((Path(case.root) / "demo/upgrade/contract.json").read_text())
                expected_cases = {item["id"]: item for item in requirements_data["cases"]}
                for item in rows:
                    test_id = item.get("id", "unknown")
                    case_id = CONTROL_IDS.get(test_id, test_id)
                    observations.append({
                        "id": case_id, "test_id": test_id, "stage": stage,
                        "status": {"pass": "passed", "fail": "failed"}.get(item.get("status"), item.get("status", "error")),
                        "detail": item.get("detail", ""),
                        "kind": "control" if test_id in CONTROL_IDS else "original_suite",
                        "expected": expected_cases.get(case_id, {"test_id": test_id, "result": "passed"}),
                        "exit_code": result.exit_code, "duration_seconds": result.duration_seconds,
                        "output_ref": stage + "-log",
                    })
            else:
                execution = "timed_out" if result.status == "timeout" else "setup_failed"
    return environments, observations, raw, execution


def _matrix(stages: tuple[str, str]) -> list[str]:
    return [stage + ":" + test_id for stage in stages for test_id in TEST_IDS]


def _executed(cases: list) -> list[str]:
    return [item["stage"] + ":" + item["test_id"] for item in cases]


def _finding(execution: str, cases: list) -> str:
    if execution != "completed" or Counter(_executed(cases)) != Counter(_matrix(("baseline", "updated"))):
        return "inconclusive"
    if all(item["status"] == "passed" for item in cases):
        return "no_difference_observed"
    for item in cases:
        if item["stage"] == "updated" and item["id"] == "nickname_omitted":
            if (item["status"] not in {"failed", "error"}
                    or not all(term in item["detail"] for term in ("ValidationError", "nickname", "Field required"))):
                return "inconclusive"
        elif item["status"] != "passed":
            return "inconclusive"
    return "regression_reproduced"


def _save(evidence, directory: Path, raw: dict, kind: str) -> None:
    raw_path = directory / "case-results.json"
    _write_json(raw_path, raw)
    evidence.artifacts[kind + "-case-results"] = str(raw_path)
    report_path = directory / (kind + ".json")
    evidence.artifacts[kind] = str(report_path)
    for stage in raw:
        evidence.artifacts[stage + "-log"] = str(directory / (stage + ".txt"))
    public = evidence.to_dict()
    public["artifacts"] = list(evidence.artifacts)
    _write_json(report_path, dict(public, checked_at=datetime.now(timezone.utc).isoformat(),
                                  evidence_mode="executed" if raw else "validation_only"))


def run_comparison(case_spec: CaseSpec, source_bundle: SourceBundle) -> ComparisonEvidence:
    """Reproduce a supported regression independently of any repair outcome."""
    directory = _directory(case_spec, "comparison")
    evidence = ComparisonEvidence("setup_failed", "inconclusive", {}, limitations=list(LIMITATIONS))
    raw = {}
    try:
        contract, snapshot, evidence.bindings = _validate(case_spec, source_bundle)
        evidence.expected_case_ids = _matrix(("baseline", "updated"))
        evidence.environments, evidence.cases, raw, evidence.execution_status = _execute(
            case_spec, source_bundle.content, snapshot, directory, repaired=False)
        evidence.bindings["environment_manifest_sha256"] = _json_hash(evidence.environments)
        evidence.executed_case_ids = _executed(evidence.cases)
        if _snapshot(Path(case_spec.root)) != snapshot:
            raise ValueError("Source, contract, tests, or environment inputs changed during execution.")
        evidence.finding_status = _finding(evidence.execution_status, evidence.cases)
        evidence.failure_context = {
            "requirements": contract,
            "requirements_json": (Path(case_spec.root) / "demo/upgrade/contract.json").read_text(),
            "finding_status": evidence.finding_status,
            "observations": evidence.cases,
            "bindings": evidence.bindings,
        }
    except (ValueError, OSError, SandboxError, subprocess.TimeoutExpired) as exc:
        evidence.execution_status = "setup_failed"
        evidence.finding_status = "inconclusive"
        evidence.limitations.append(str(exc) if isinstance(exc, (ValueError, SandboxError)) else "Worker setup or source validation failed.")
    _save(evidence, directory, raw, "comparison")
    return evidence


def verify_candidate(case_spec: CaseSpec, source_bundle: SourceBundle,
                     proposal: PatchProposal) -> VerificationEvidence:
    """Only exact, complete original-suite and control success verifies a candidate."""
    directory = _directory(case_spec, "verification")
    evidence = VerificationEvidence("setup_failed", "rejected", {}, limitations=list(LIMITATIONS))
    raw = {}
    try:
        _, snapshot, evidence.bindings = _validate(case_spec, source_bundle)
        content = _candidate(source_bundle.content, proposal, case_spec, source_bundle.sha256)
        evidence.bindings["candidate_sha256"] = _sha(content.encode())
        candidate_path = directory / "candidate.py"
        candidate_path.write_bytes(content.encode())
        patch_path = directory / "candidate.patch"
        patch_path.write_text("".join(difflib.unified_diff(
            source_bundle.content.splitlines(True), content.splitlines(True),
            fromfile=case_spec.allowed_path, tofile=case_spec.allowed_path)))
        evidence.artifacts["candidate-source"] = str(candidate_path)
        evidence.artifacts["candidate-patch"] = str(patch_path)
        evidence.expected_case_ids = _matrix(("repaired_baseline", "repaired_updated"))
        evidence.environments, evidence.cases, raw, evidence.execution_status = _execute(
            case_spec, content, snapshot, directory, repaired=True)
        evidence.bindings["environment_manifest_sha256"] = _json_hash(evidence.environments)
        evidence.executed_case_ids = _executed(evidence.cases)
        if (_snapshot(Path(case_spec.root)) != snapshot
                or _sha(candidate_path.read_bytes()) != evidence.bindings["candidate_sha256"]):
            raise ValueError("Evidence inputs or candidate changed during verification.")
        evidence.complete = (evidence.execution_status == "completed"
                             and Counter(evidence.executed_case_ids) == Counter(evidence.expected_case_ids))
        if evidence.complete and all(item["status"] == "passed" for item in evidence.cases):
            evidence.repair_status = "verified"
    except (ValueError, OSError, SandboxError, subprocess.TimeoutExpired) as exc:
        evidence.execution_status = "setup_failed"
        evidence.repair_status = "rejected"
        evidence.complete = False
        evidence.limitations.append(str(exc) if isinstance(exc, (ValueError, SandboxError)) else "Worker setup or source validation failed.")
    _save(evidence, directory, raw, "verification")
    return evidence
