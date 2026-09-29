"""Service safety tests with a real Git fixture and synthetic runner evidence.

These tests execute neither Docker nor a model. The source/contract bytes and
revision come from a temporary copy of the registered fixture; runner verdicts
are injected to test orchestration and rejection at the service boundary.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.proofrun.contracts import (
    CaseSpec, ComparisonEvidence, PatchProposal, ProposalUnavailable, SourceBundle,
    VerificationEvidence,
)
from src.proofrun.service import CASE_ID, MODULE, RunService, ServiceError


ROOT = Path(__file__).resolve().parents[1]
CONTROLS = {
    "test_controls.TestControls.test_nickname_omitted": "nickname_omitted",
    "test_controls.TestControls.test_nickname_null": "nickname_null",
    "test_controls.TestControls.test_nickname_string_unchanged": "nickname_string",
    "test_controls.TestControls.test_nickname_object_rejected": "nickname_object_rejected",
    "test_controls.TestControls.test_required_name_rejected": "required_name_rejected",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bindings(spec, source):
    return {
        "revision": source.revision,
        "source_sha256": source.sha256,
        "bundle_sha256": source.bundle_sha256,
        "contract_sha256": spec.contract_sha256,
        "tests_sha256": digest(b"synthetic-test-identity"),
        "verifier_sha256": digest(b"synthetic-verifier-identity"),
        "environment_manifest_sha256": digest(b"synthetic-environment-identity"),
    }


def environments(spec, repaired=False):
    result = {}
    prefix = "repaired_" if repaired else ""
    for stage, version, label in (("baseline", spec.baseline_version, "old"),
                                  ("updated", spec.updated_version, "new")):
        result[prefix + stage] = {
            "execution_backend": "synthetic-unit-test",
            "image_id": "sha256:" + digest(("synthetic-image-" + version).encode()),
            "expected_version": version,
            "observed_version": version,
            "requirements_sha256": digest((spec.root / f"demo/upgrade/requirements-{label}.txt").read_bytes()),
        }
    return result


def matrix(spec, repaired=False):
    stages = ("repaired_baseline", "repaired_updated") if repaired else ("baseline", "updated")
    cases = []
    for stage in stages:
        for test_id in (*spec.original_test_ids, *CONTROLS):
            case_id = CONTROLS.get(test_id, test_id)
            failed = not repaired and stage == "updated" and case_id == "nickname_omitted"
            cases.append({
                "id": case_id, "test_id": test_id, "stage": stage,
                "kind": "control" if test_id in CONTROLS else "original_suite",
                "status": "failed" if failed else "passed",
                "detail": "ValidationError: nickname Field required" if failed else "",
            })
    return cases


def comparison(spec, source):
    cases = matrix(spec)
    ids = [row["stage"] + ":" + row["test_id"] for row in cases]
    return ComparisonEvidence(
        execution_status="completed", finding_status="regression_reproduced",
        bindings=bindings(spec, source), cases=cases, environments=environments(spec),
        expected_case_ids=ids, executed_case_ids=ids.copy(),
        limitations=["Synthetic runner evidence for unit testing only."],
    )


def proposal(context, attempt):
    return PatchProposal(
        base_sha256=context["source"]["sha256"],
        allowed_path=context["case"]["allowed_path"],
        replacement=context["source"]["content"] + f"\n# synthetic attempt {attempt}\n",
        rationale="Synthetic proposal for orchestration testing only.",
        provenance={"mode": "mock", "gateway": "unit-test", "operation_id": f"mock-{attempt}"},
    )


def verification(spec, source, patch):
    cases = matrix(spec, repaired=True)
    ids = [row["stage"] + ":" + row["test_id"] for row in cases]
    return VerificationEvidence(
        execution_status="completed", repair_status="verified", complete=True,
        bindings={**bindings(spec, source), "candidate_sha256": digest(patch.replacement.encode())},
        cases=cases, environments=environments(spec, repaired=True),
        expected_case_ids=ids, executed_case_ids=ids.copy(),
    )


class ProofRunServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proofrun-service-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "repo"
        shutil.copytree(ROOT / "demo/upgrade", self.root / "demo/upgrade",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.root / "sandbox").mkdir()
        shutil.copyfile(ROOT / "sandbox/upgrade.Dockerfile", self.root / "sandbox/upgrade.Dockerfile")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=ProofRun test", "-c", "user.email=test@example.invalid",
                 "commit", "-q", "-m", "Registered synthetic fixture")
        self.services = []
        self.addCleanup(self.close_services)
        self.counter = 0

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL)

    def close_services(self):
        for service in self.services:
            service.close()

    def service(self, **kwargs):
        options = {"compare": comparison, "verify": verification, "propose": proposal}
        options.update(kwargs)
        service = RunService(self.root, self.directory / "artifacts", **options)
        self.services.append(service)
        return service

    def close(self, service):
        service.close()
        self.services.remove(service)

    def payload(self, service, *, repair=False, attempts=2):
        self.counter += 1
        payload = service.get_case(CASE_ID)["submission"]
        payload["job_key"] = f"service-test-{self.counter}"
        payload["repair"] = {"enabled": repair, "max_attempts": attempts}
        return payload

    def finish(self, service, run):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = service.get_run(run["run_id"])
            if current["execution_status"] not in {"queued", "running"}:
                # A completed status is persisted just before the active slot is
                # released. Joining the queue also makes the next submission deterministic.
                service._pool.submit(lambda: None).result(timeout=5)
                return service.get_run(run["run_id"])
            time.sleep(0.005)
        self.fail("Worker did not reach a terminal state")

    def execute(self, service, **payload_options):
        run, created = service.submit(self.payload(service, **payload_options))
        self.assertTrue(created)
        return self.finish(service, run)

    def assert_error(self, status, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_registry_binds_real_git_source_contract_and_explicit_bundle(self):
        service = self.service()
        registered = service.get_case(CASE_ID)
        source = registered["submission"]["source"]
        content_hash = digest((self.root / MODULE).read_bytes())
        self.assertEqual(source["revision"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(source["sha256"], content_hash)
        bundle = json.dumps({"module": MODULE, "sha256": content_hash}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(source["bundle_sha256"], digest(bundle.encode()))
        self.assertEqual(registered["submission"]["contract"]["sha256"],
                         digest((self.root / "demo/upgrade/contract.json").read_bytes()))
        self.assert_error(404, "unknown_case", lambda: service.get_case("arbitrary-repository"))

    def test_invalid_submissions_never_dispatch_the_runner(self):
        compare = Mock(side_effect=comparison)
        service = self.service(compare=compare)
        valid = self.payload(service)
        mutations = [
            lambda p: p.update(schema_version="proofrun.v99"),
            lambda p: p.update(case_id="unregistered"),
            lambda p: p.update(command="echo cannot-run"),
            lambda p: p.update(job_key="../outside"),
            lambda p: p["source"].update(repository="https://example.invalid/other"),
            lambda p: p["source"].update(revision="a" * 40),
            lambda p: p["source"].update(module="src/main.py"),
            lambda p: p["source"].update(sha256="b" * 64),
            lambda p: p["source"].update(bundle_sha256="c" * 64),
            lambda p: p["source"].update(content="print('untrusted')"),
            lambda p: p["contract"].update(sha256="d" * 64),
            lambda p: p["environments"].update(updated="latest"),
            lambda p: p["repair"].update(enabled=1),
            lambda p: p["repair"].update(max_attempts=True),
            lambda p: p["repair"].update(max_attempts=0),
            lambda p: p["repair"].update(max_attempts=3),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                payload = copy.deepcopy(valid)
                mutate(payload)
                self.assert_error(400, "invalid_submission", lambda: service.submit(payload))
        compare.assert_not_called()
        self.assertEqual(list(service.artifact_dir.glob("run-*")), [])

    def test_committed_source_without_explicit_bundle_is_accepted(self):
        service = self.service()
        payload = self.payload(service)
        del payload["source"]["bundle_sha256"]
        run, _ = service.submit(payload)
        self.assertEqual(self.finish(service, run)["execution_status"], "completed")

    def test_uncommitted_source_requires_explicit_bundle(self):
        service = self.service()
        path = self.root / MODULE
        path.write_text(path.read_text() + "\n# approved working-copy change\n")
        payload = self.payload(service)
        bound = copy.deepcopy(payload)
        del payload["source"]["bundle_sha256"]
        self.assert_error(400, "invalid_submission", lambda: service.submit(payload))
        run, _ = service.submit(bound)
        final = self.finish(service, run)
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(final["bindings"]["source_sha256"], digest(path.read_bytes()))

    def test_stale_registry_response_is_rejected_after_application_changes(self):
        compare = Mock(side_effect=comparison)
        service = self.service(compare=compare)
        stale = self.payload(service)
        path = self.root / MODULE
        path.write_text(path.read_text() + "\n# changed since discovery\n")
        self.assert_error(400, "invalid_submission", lambda: service.submit(stale))
        compare.assert_not_called()

    def test_duplicate_active_submission_returns_same_run_and_changed_key_conflicts(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked_compare(spec, source):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release runner")
            return comparison(spec, source)

        compare = Mock(side_effect=blocked_compare)
        service = self.service(compare=compare)
        payload = self.payload(service)
        first, created = service.submit(payload)
        self.assertTrue(created)
        self.assertTrue(started.wait(2))
        duplicate, created = service.submit(copy.deepcopy(payload))
        self.assertFalse(created)
        self.assertEqual(first["run_id"], duplicate["run_id"])
        changed = copy.deepcopy(payload)
        changed["repair"]["enabled"] = True
        self.assert_error(409, "job_key_conflict", lambda: service.submit(changed))
        self.assert_error(409, "worker_busy", lambda: service.submit(self.payload(service)))
        self.assertEqual(compare.call_count, 1)
        release.set()
        self.assertEqual(self.finish(service, first)["execution_status"], "completed")
        duplicate, created = service.submit(payload)
        self.assertFalse(created)
        self.assertEqual(duplicate["run_id"], first["run_id"])
        self.assertEqual(self.execute(service)["execution_status"], "completed")

    def test_restart_marks_unfinished_record_interrupted_and_preserves_finding(self):
        service = self.service()
        payload = self.payload(service, repair=True)
        run, _ = service.submit(payload)
        final = self.finish(service, run)
        self.close(service)
        record_path = self.directory / "artifacts" / final["run_id"] / "record.json"
        record = json.loads(record_path.read_text())
        record.update(execution_status="running", repair_status="proposed")
        record_path.write_text(json.dumps(record))
        restarted = self.service()
        restored = restarted.get_run(final["run_id"])
        self.assertEqual(restored["execution_status"], "interrupted")
        self.assertEqual(restored["finding_status"], "regression_reproduced")
        self.assertEqual(restored["repair_status"], "unavailable")
        self.assertEqual(json.loads(record_path.read_text())["execution_status"], "interrupted")
        duplicate, created = restarted.submit(payload)
        self.assertFalse(created)
        self.assertEqual(duplicate["execution_status"], "interrupted")
        self.assertEqual(self.execute(restarted)["execution_status"], "completed")

    def test_two_workers_cannot_own_same_artifact_directory(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "Another worker"):
            RunService(self.root, service.artifact_dir, compare=comparison)

    def test_snapshot_contains_independent_verification_inputs(self):
        def inspect_snapshot(spec, source):
            self.assertNotEqual(spec.root, self.root)
            for name in ("app.py", "contract.json", "test_controls.py", "test_existing.py",
                         "requirements-old.txt", "requirements-new.txt"):
                self.assertEqual((spec.root / "demo/upgrade" / name).read_bytes(),
                                 (self.root / "demo/upgrade" / name).read_bytes())
            return comparison(spec, source)

        service = self.service(compare=inspect_snapshot)
        self.assertEqual(self.execute(service)["execution_status"], "completed")

    def test_published_artifact_is_immutable_copy_and_hash_checked_on_read(self):
        paths = []

        def with_artifact(spec, source):
            path = spec.artifact_dir / "observations.json"
            path.write_bytes(b'{"evidence":"synthetic"}\n')
            paths.append(path)
            evidence = comparison(spec, source)
            evidence.artifacts = {"observations.json": str(path)}
            return evidence

        service = self.service(compare=with_artifact)
        run = self.execute(service)
        expected = b'{"evidence":"synthetic"}\n'
        paths[0].write_bytes(b"runner later changed its working file")
        data, mime = service.artifact(run["run_id"], "observations.json")
        self.assertEqual((data, mime), (expected, "application/json"))
        metadata = run["artifacts"][0]
        self.assertEqual((metadata["sha256"], metadata["size_bytes"]), (digest(expected), len(expected)))
        self.assertNotIn(str(self.directory), json.dumps(run))
        self.assertFalse(any(key.startswith("_") for key in run))
        published = service.artifact_dir / run["run_id"] / "published/observations.json"
        published.write_bytes(b"tampered published bytes")
        self.assert_error(409, "artifact_changed", lambda: service.artifact(run["run_id"], "observations.json"))
        for artifact_id in ("../record.json", "/etc/passwd", "missing.json"):
            self.assert_error(404, "unknown_artifact", lambda: service.artifact(run["run_id"], artifact_id))

    def test_verification_artifact_references_match_published_attempt_ids_and_hashes(self):
        original_reports = []

        def verify_with_artifacts(spec, source, candidate):
            evidence = verification(spec, source, candidate)
            for case in evidence.cases:
                case["output_ref"] = "repaired-log"
            document = evidence.to_dict()
            document["artifacts"] = ["verification", "repaired-log"]
            report = spec.artifact_dir / "verification.json"
            report.write_text(json.dumps(document))
            log = spec.artifact_dir / "repaired.log"
            log.write_bytes(b"Synthetic verification log: declared checks passed.\n")
            evidence.artifacts = {"verification": str(report), "repaired-log": str(log)}
            original_reports.append((report, document))
            return evidence

        service = self.service(verify=verify_with_artifacts)
        run = self.execute(service, repair=True)
        self.assertEqual((run["execution_status"], run["finding_status"], run["repair_status"]),
                         ("completed", "regression_reproduced", "verified"))
        served, _ = service.artifact(run["run_id"], "attempt-1-verification")
        document = json.loads(served)
        self.assertEqual(document["artifacts"], ["attempt-1-verification", "attempt-1-repaired-log"])
        self.assertTrue(document["cases"])
        self.assertTrue(all(case["output_ref"] == "attempt-1-repaired-log" for case in document["cases"]))
        public_cases = [case for case in run["cases"] if case["stage"].startswith("repaired_")]
        self.assertEqual(public_cases, document["cases"])
        metadata = next(item for item in run["artifacts"] if item["id"] == "attempt-1-verification")
        self.assertEqual((metadata["sha256"], metadata["size_bytes"]), (digest(served), len(served)))
        self.assertEqual(metadata["href"], f'/v1/runs/{run["run_id"]}/artifacts/attempt-1-verification')
        log_bytes, _ = service.artifact(run["run_id"], document["cases"][0]["output_ref"])
        self.assertEqual(log_bytes, b"Synthetic verification log: declared checks passed.\n")
        original_path, original = original_reports[0]
        self.assertEqual(json.loads(original_path.read_bytes()), original)
        self.assertNotEqual(digest(original_path.read_bytes()), metadata["sha256"])
        self.assertEqual(document["bindings"], original["bindings"])
        self.assertEqual(document["bindings"]["candidate_sha256"], run["bindings"]["candidate_sha256"])
        self.assertEqual(document["repair_status"], original["repair_status"])
        self.assertEqual(document["complete"], original["complete"])

    def test_artifact_paths_and_symlinks_cannot_escape_run_evidence(self):
        outside = self.directory / "private.txt"
        outside.write_text("private host data")

        for kind in ("outside", "symlink", "identifier"):
            with self.subTest(kind=kind):
                def malicious_artifact(spec, source):
                    evidence = comparison(spec, source)
                    if kind == "outside":
                        evidence.artifacts = {"leak.txt": str(outside)}
                    elif kind == "symlink":
                        link = spec.artifact_dir / "leak.txt"
                        link.symlink_to(outside)
                        evidence.artifacts = {"leak.txt": str(link)}
                    else:
                        path = spec.artifact_dir / "allowed.txt"
                        path.write_text("synthetic")
                        evidence.artifacts = {"../leak.txt": str(path)}
                    return evidence

                service = self.service(compare=malicious_artifact)
                run = self.execute(service)
                self.assertEqual(run["execution_status"], "setup_failed")
                self.assertEqual(run["artifacts"], [])
                self.assertNotIn("private host data", json.dumps(run))
                self.close(service)

    def test_stale_or_incomplete_comparison_evidence_is_never_completed(self):
        corruptions = {
            "source": lambda e: e.bindings.update(source_sha256="0" * 64),
            "revision": lambda e: e.bindings.update(revision="0" * 40),
            "contract": lambda e: e.bindings.update(contract_sha256="0" * 64),
            "environment": lambda e: e.bindings.pop("environment_manifest_sha256"),
            "tests": lambda e: e.bindings.pop("tests_sha256"),
            "missing": lambda e: e.executed_case_ids.pop(),
            "duplicate": lambda e: e.executed_case_ids.append(e.executed_case_ids[0]),
            "empty": lambda e: (e.expected_case_ids.clear(), e.executed_case_ids.clear()),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name):
                def invalid_compare(spec, source):
                    evidence = comparison(spec, source)
                    corrupt(evidence)
                    return evidence

                service = self.service(compare=invalid_compare)
                run = self.execute(service)
                self.assertEqual(run["execution_status"], "setup_failed")
                self.assertNotEqual(run["repair_status"], "verified")
                self.close(service)

    def test_completed_evidence_must_cover_registered_matrix_and_observations(self):
        def unrelated(evidence):
            evidence.expected_case_ids = ["baseline:unregistered.test"]
            evidence.executed_case_ids = evidence.expected_case_ids.copy()
            evidence.cases = [{"id": "invented", "test_id": "unregistered.test",
                               "stage": "baseline", "status": "passed"}]

        corruptions = {"wrong_matrix": unrelated, "missing_rows": lambda e: e.cases.clear()}
        for name, corrupt in corruptions.items():
            with self.subTest(name=name):
                def invalid_compare(spec, source):
                    evidence = comparison(spec, source)
                    corrupt(evidence)
                    return evidence

                service = self.service(compare=invalid_compare)
                run = self.execute(service)
                self.assertEqual(run["execution_status"], "setup_failed")
                self.close(service)

    def test_missing_environment_or_verifier_identity_cannot_establish_completion(self):
        for missing in ("environments", "verifier_sha256"):
            with self.subTest(missing=missing):
                def incomplete_comparison(spec, source):
                    evidence = comparison(spec, source)
                    if missing == "environments":
                        evidence.environments.clear()
                    else:
                        evidence.bindings.pop(missing)
                    return evidence

                propose = Mock(side_effect=proposal)
                service = self.service(compare=incomplete_comparison, propose=propose)
                run = self.execute(service, repair=True)
                self.assertEqual(run["execution_status"], "setup_failed")
                self.assertNotEqual(run["repair_status"], "verified")
                propose.assert_not_called()
                self.close(service)

    def test_provider_unavailable_preserves_reproduced_finding_and_hides_error_body(self):
        secret = "synthetic-provider-secret-must-not-appear"
        propose = Mock(side_effect=ProposalUnavailable(secret))
        verify = Mock(side_effect=verification)
        service = self.service(propose=propose, verify=verify)
        run = self.execute(service, repair=True)
        self.assertEqual((run["execution_status"], run["finding_status"], run["repair_status"]),
                         ("completed", "regression_reproduced", "unavailable"))
        self.assertEqual(propose.call_count, 1)
        verify.assert_not_called()
        self.assertNotIn(secret, json.dumps(run))
        context, attempt = propose.call_args.args
        self.assertEqual(attempt, 1)
        self.assertEqual(digest(context["requirements_json"].encode()), context["case"]["contract_sha256"])
        self.assertEqual(json.loads(context["requirements_json"]), context["requirements"])
        self.assertEqual(context["comparison"]["finding_status"], "regression_reproduced")
        self.assertNotIn("artifacts", context["comparison"])

    def test_rejected_verifier_preserves_finding_and_stops_at_two_attempts(self):
        propose = Mock(side_effect=proposal)

        def reject(spec, source, patch):
            evidence = verification(spec, source, patch)
            evidence.repair_status = "rejected"
            evidence.cases[-1]["status"] = "failed"
            return evidence

        verify = Mock(side_effect=reject)
        service = self.service(propose=propose, verify=verify)
        run = self.execute(service, repair=True)
        self.assertEqual((run["execution_status"], run["finding_status"], run["repair_status"]),
                         ("completed", "regression_reproduced", "rejected"))
        self.assertEqual([call.args[1] for call in propose.call_args_list], [1, 2])
        self.assertEqual(verify.call_count, 2)
        self.assertEqual([entry["attempt"] for entry in run["attempts"]], [1, 2])
        self.assertEqual(propose.call_args.args[0]["previous_verification"]["repair_status"], "rejected")

    def test_one_attempt_limit_and_successful_verification_stop_repair_loop(self):
        propose = Mock(side_effect=proposal)
        service = self.service(propose=propose)
        run = self.execute(service, repair=True, attempts=1)
        self.assertEqual((run["finding_status"], run["repair_status"]), ("regression_reproduced", "verified"))
        self.assertEqual(len(run["attempts"]), 1)
        self.assertEqual(propose.call_count, 1)
        run = self.execute(service, repair=True, attempts=2)
        self.assertEqual(run["repair_status"], "verified")
        self.assertEqual(len(run["attempts"]), 1)
        self.assertEqual(propose.call_count, 2)

    def test_single_attempt_limit_also_applies_to_rejected_candidate(self):
        propose = Mock(side_effect=proposal)

        def reject(spec, source, patch):
            evidence = verification(spec, source, patch)
            evidence.repair_status = "rejected"
            evidence.cases[-1]["status"] = "failed"
            return evidence

        service = self.service(propose=propose, verify=reject)
        run = self.execute(service, repair=True, attempts=1)
        self.assertEqual((run["execution_status"], run["finding_status"], run["repair_status"]),
                         ("completed", "regression_reproduced", "rejected"))
        self.assertEqual(propose.call_count, 1)
        self.assertEqual(len(run["attempts"]), 1)

    def test_repair_is_not_requested_without_supported_reproduced_regression(self):
        propose = Mock(side_effect=proposal)

        def compatible(spec, source):
            evidence = comparison(spec, source)
            evidence.finding_status = "no_difference_observed"
            for case in evidence.cases:
                case["status"] = "passed"
            return evidence

        service = self.service(compare=compatible, propose=propose)
        run = self.execute(service, repair=True)
        self.assertEqual((run["execution_status"], run["finding_status"], run["repair_status"]),
                         ("completed", "no_difference_observed", "unavailable"))
        propose.assert_not_called()

    def test_wrong_proposal_source_or_path_is_rejected_before_verification(self):
        for field, value in (("base_sha256", "0" * 64), ("allowed_path", "demo/upgrade/test_controls.py")):
            with self.subTest(field=field):
                def invalid_proposal(context, attempt):
                    valid = proposal(context, attempt)
                    fields = vars(valid).copy()
                    fields[field] = value
                    return PatchProposal(**fields)

                verify = Mock(side_effect=verification)
                service = self.service(propose=invalid_proposal, verify=verify)
                run = self.execute(service, repair=True)
                self.assertEqual((run["finding_status"], run["repair_status"]),
                                 ("regression_reproduced", "rejected"))
                verify.assert_not_called()
                self.close(service)

    def test_stale_candidate_and_incomplete_verification_cannot_be_verified(self):
        corruptions = {
            "candidate": lambda e: e.bindings.update(candidate_sha256="0" * 64),
            "source": lambda e: e.bindings.update(source_sha256="0" * 64),
            "different_tests": lambda e: e.bindings.update(tests_sha256="0" * 64),
            "different_verifier": lambda e: e.bindings.update(verifier_sha256="0" * 64),
            "different_image": lambda e: e.environments["repaired_updated"].update(image_id="sha256:" + "0" * 64),
            "different_version": lambda e: e.environments["repaired_updated"].update(observed_version="9.9.9"),
            "incomplete": lambda e: setattr(e, "complete", False),
            "timed_out": lambda e: setattr(e, "execution_status", "timed_out"),
            "missing_check": lambda e: e.executed_case_ids.pop(),
            "failed_check": lambda e: e.cases[-1].update(status="failed"),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name):
                def invalid_verification(spec, source, patch):
                    evidence = verification(spec, source, patch)
                    corrupt(evidence)
                    return evidence

                service = self.service(verify=invalid_verification)
                run = self.execute(service, repair=True)
                self.assertEqual(run["finding_status"], "regression_reproduced")
                self.assertNotEqual(run["execution_status"], "completed")
                self.assertNotEqual(run["repair_status"], "verified")
                self.close(service)

    def test_timeout_preserves_already_reproduced_finding(self):
        verify = Mock(side_effect=subprocess.TimeoutExpired("synthetic-runner", 1))
        service = self.service(verify=verify)
        run = self.execute(service, repair=True)
        self.assertEqual((run["execution_status"], run["finding_status"], run["repair_status"]),
                         ("timed_out", "regression_reproduced", "unavailable"))

    def test_prepared_fix_timeout_or_failure_cannot_erase_reproduced_regression(self):
        service = self.service(runner_mode="prepared")
        registered = service.get_case(CASE_ID)["submission"]
        bundle = SourceBundle(**registered["source"], content=(self.root / MODULE).read_text())

        for fixed_status, expected_execution in (("timeout", "timed_out"), ("fail", "completed")):
            with self.subTest(fixed_status=fixed_status):
                evidence_dir = self.directory / ("prepared-" + fixed_status)
                evidence_dir.mkdir()
                spec = CaseSpec(
                    case_id=CASE_ID, contract_id=registered["contract"]["id"],
                    contract_sha256=registered["contract"]["sha256"],
                    root=self.root, artifact_dir=evidence_dir,
                )

                def write_synthetic_report(command, **kwargs):
                    report_dir = evidence_dir / "upgrade-demo/synthetic-run"
                    report_dir.mkdir(parents=True)
                    jobs = {}
                    for name in ("existing_old", "existing_new", "probe_old", "probe_new", "fixed_old", "fixed_new"):
                        version = "1.10.18" if name.endswith("old") else "2.8.2"
                        status = "fail" if name == "probe_new" else "pass"
                        if name == "fixed_new":
                            status = fixed_status
                        output = f"SECONDLOOK_DEPENDENCY_VERSION={version}\nRan 1 test in 0.001s\n"
                        if name == "probe_new":
                            output += "ValidationError: nickname Field required\n"
                        jobs[name] = {
                            "status": status,
                            "exit_code": None if status == "timeout" else int(status == "fail"),
                            "duration_seconds": 0.001,
                            "output_tail": output,
                        }
                    report = {
                        "app_sha256": bundle.sha256,
                        "requirements_sha256": {
                            label: digest((self.root / f"demo/upgrade/requirements-{label}.txt").read_bytes())
                            for label in ("old", "new")
                        },
                        "test_sha256": digest((self.root / "demo/upgrade/test_upgrade.py").read_bytes()),
                        "images": {label: "sha256:" + digest(("synthetic-" + label).encode())
                                   for label in ("old", "new")},
                        "results": jobs,
                    }
                    (report_dir / "report.json").write_text(json.dumps(report))
                    return subprocess.CompletedProcess(command, 2 if fixed_status == "timeout" else 1)

                with patch("src.proofrun.service.subprocess.run", side_effect=write_synthetic_report) as run:
                    evidence = service._prepared_comparison(spec, bundle)

                run.assert_called_once()
                self.assertEqual(evidence.finding_status, "regression_reproduced")
                self.assertEqual(evidence.execution_status, expected_execution)
                fixed_case = next(case for case in evidence.cases if case["id"] == "fixed_new")
                self.assertEqual(fixed_case["status"], "timeout" if fixed_status == "timeout" else "failed")
                self.assertTrue(any("prepared fix only" in item for item in evidence.limitations))
                self.assertTrue(any("independent controls are not established" in item for item in evidence.limitations))

    def test_public_results_are_detached_from_persisted_record(self):
        service = self.service()
        run = self.execute(service)
        run["bindings"]["source_sha256"] = "changed-client-copy"
        run["cases"].clear()
        current = service.get_run(run["run_id"])
        self.assertNotEqual(current["bindings"]["source_sha256"], "changed-client-copy")
        self.assertTrue(current["cases"])
        self.assert_error(404, "unknown_run", lambda: service.get_run("run-unknown"))


if __name__ == "__main__":
    unittest.main()
