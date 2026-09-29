"""Owned unittest harness; staged read-only beside one bounded application copy.

This file is verifier input, never a repair target. Human-readable unittest output
is retained, but only the structured record establishes exact case execution.
"""

import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import traceback
import unittest


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.active = {}
        self.detail_budget = 32_768

    def startTest(self, test):
        super().startTest(test)
        record = {"id": test.id(), "status": "incomplete"}
        self.records.append(record)
        self.active[id(test)] = record

    def record(self, test, status, detail=None):
        record = self.active.get(id(test))
        if record is None:
            record = {"id": test.id(), "status": "incomplete"}
            self.records.append(record)
        record["status"] = status
        if detail and self.detail_budget:
            size = min(3_000, self.detail_budget)
            record["detail"] = detail[-size:]
            self.detail_budget -= len(record["detail"])

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skip", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, "expected_failure", self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(test, "fail" if issubclass(err[0], test.failureException) else "error",
                        self._exc_info_to_string(err, test))


def main():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "_proofrun_manifest.json").read_text(encoding="utf-8"))
    record = {
        "schema_version": "proofrun.probe.v1", "version": None,
        "tests_run": 0, "tests": [], "integrity_errors": [],
    }
    code = 120
    try:
        record.update(
            version=version("pydantic"), source_sha256=digest(root / "app.py"),
            tests_sha256={name: digest(root / name) for name in manifest["tests_sha256"]},
            harness_sha256=digest(Path(__file__)),
        )
        for key in ("source_sha256", "tests_sha256", "harness_sha256"):
            if record[key] != manifest[key]:
                record["integrity_errors"].append(key + " differs before execution")
        if manifest["expected_version"] is not None and record["version"] != manifest["expected_version"]:
            record["integrity_errors"].append("runtime dependency version mismatch")
        suite = unittest.defaultTestLoader.discover(str(root), pattern="test_*.py")
        discovered = [test.id() for test in flatten(suite)]
        expected = manifest["expected_test_ids"]
        if not discovered or len(set(discovered)) != len(discovered) or set(discovered) != set(expected):
            record["integrity_errors"].append("discovered test IDs differ from approved case IDs")
        # A wrong discovery set is never run as though it were the approved suite.
        if not record["integrity_errors"]:
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
            record["tests_run"], record["tests"] = result.testsRun, result.records
            code = 0 if result.wasSuccessful() else 1
            if any(case["status"] not in ("pass", "fail", "error") for case in result.records):
                record["integrity_errors"].append("unsupported or incomplete test outcome")
        if digest(root / "app.py") != record["source_sha256"]:
            record["integrity_errors"].append("application changed during execution")
        if any(digest(root / name) != value for name, value in record["tests_sha256"].items()):
            record["integrity_errors"].append("test changed during execution")
        if digest(Path(__file__)) != record["harness_sha256"]:
            record["integrity_errors"].append("harness changed during execution")
    except Exception as exc:
        traceback.print_exc()
        record["integrity_errors"].append("harness setup failed: " + type(exc).__name__)
    if record["integrity_errors"]:
        code = 120
    print("SECONDLOOK_DEPENDENCY_VERSION=" + str(record["version"]), flush=True)
    print("PROOFRUN_PROBE_RESULT=" + json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
