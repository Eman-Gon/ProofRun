"""Execute frozen repository tests; model prose cannot assign the outcome.

This compares selected existing tests, not the correctness of an entire repo.
Repository tests and their assertions remain the oracle (not a security proof
against a repository deliberately forging its own test output).
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import time

from .dependencies import is_dependency_file
from .proofrun import release_runtime as runtime

IMAGES = {"python": "python:3.12-slim", "node": "node:22-alpine"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _test_path(path):
    p = PurePosixPath(path.lower())
    return (any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in p.parts[:-1])
            or bool(re.search(r"(?:^test_|_test\.|[.]test[.]|[.]spec[.])", p.name)))


def _safe_path(path):
    p = PurePosixPath(path)
    return (bool(path) and not p.is_absolute() and str(p) == path and ".." not in p.parts
            and "\\" not in path and not any(ord(c) < 32 for c in path))


def _counts(run, language):
    """Reject empty, skipped, malformed, setup-error and truncated observations."""
    if not run.get("complete") or run.get("execution_status") != "completed":
        return None
    output = run.get("output_tail", "")
    if len(output.encode()) >= runtime.MAX_OUTPUT or run.get("exit_code") not in (0, 1):
        return None
    if language == "python":
        rows = re.findall(r"^(.+ \(.+\)) \.\.\. (ok|FAIL|ERROR|skipped.*)$", output, re.M)
        totals = re.findall(r"^Ran (\d+) tests? in .+$", output, re.M)
        if len(totals) != 1 or not rows or len(rows) != int(totals[0]):
            return None
        if any(status not in {"ok", "FAIL"} for _, status in rows):
            return None
        ids = [name for name, _ in rows]
        failed = [name for name, status in rows if status == "FAIL"]
        ending = "OK" if not failed else f"FAILED (failures={len(failed)})"
        if not output.rstrip().endswith(ending):
            return None
    else:
        totals = {key: re.findall(r"^# " + key + r" (\d+)$", output, re.M)
                  for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo")}
        if any(len(values) != 1 for values in totals.values()):
            return None
        totals = {key: int(values[0]) for key, values in totals.items()}
        rows = re.findall(r"^(ok|not ok) (\d+) - (.+)$", output, re.M)
        if (not rows or len(rows) != totals["tests"] or any(totals[k] for k in ("cancelled", "skipped", "todo"))
                or totals["pass"] + totals["fail"] != len(rows)):
            return None
        ids = [number + " - " + name for _, number, name in rows]
        failed = [number + " - " + name for state, number, name in rows if state == "not ok"]
        # An import, syntax, or runtime error is not an assertion reproduction.
        if len(re.findall(r"^\s+code: 'ERR_ASSERTION'$", output, re.M)) != len(failed):
            return None
        if len(failed) != totals["fail"]:
            return None
    if len(set(ids)) != len(ids) or run["exit_code"] != (1 if failed else 0):
        return None
    return {"ids": sorted(ids), "count": len(ids), "failed": sorted(failed)}


def check_patch(files, finding, language, tests, commit, *, deadline, cancelled=None):
    """Only accept an already source-validated finding and existing test files."""
    evidence = {"status": "inconclusive", "patchStatus": "not_verified", "commit": commit,
                "findingId": finding["id"], "patchSha256": digest([finding["file"], finding["beforeCode"], finding["afterCode"]]),
                "scope": "Selected unchanged repository tests in a bounded text snapshot. No dependency installation; no full-suite or universal correctness claim."}
    if (language not in IMAGES or not isinstance(tests, list) or not 1 <= len(tests) <= 8
            or any(not isinstance(p, str) or p not in files or not _test_path(p) for p in tests)
            or len(set(tests)) != len(tests)):
        evidence["reason"] = "Select 1–8 existing Python unittest or Node built-in test files."
        return evidence
    path = finding["file"]
    allowed_suffixes = {"python": {".py"}, "node": {".js", ".mjs", ".cjs"}}
    if (any(not _safe_path(p) for p in files) or path not in files or _test_path(path)
            or is_dependency_file(path) or PurePosixPath(path).suffix not in allowed_suffixes[language]
            or not finding["afterCode"]):
        evidence["reason"] = "A small application-only source patch is required; tests and manifests are frozen."
        return evidence
    parents = {str(PurePosixPath(p).parent) for p in tests}
    if len(parents) != 1 or any(PurePosixPath(p).suffix not in allowed_suffixes[language] for p in tests):
        evidence["reason"] = "Selected tests must use one runtime and share a directory."
        return evidence
    workdir = parents.pop()
    names = [PurePosixPath(p).name for p in tests]
    if language == "python" and any(not re.fullmatch(r"[A-Za-z_]\w*\.py", name) for name in names):
        evidence["reason"] = "Python unittest requires importable test module names."
        return evidence
    command = (["python", "-m", "unittest", "-v", *[n[:-3] for n in names]] if language == "python"
               else ["node", "--test", "--test-reporter=tap", *["./" + n for n in names]])
    source = files[path]
    offset = sum(len(line) for line in source.splitlines(keepends=True)[:finding["line"] - 1])
    before = finding["beforeCode"]
    if (hashlib.sha256(source.encode()).hexdigest() != finding["sourceSha256"]
            or not source[offset:].startswith(before)):
        evidence["reason"] = "The patch no longer matches the reviewed source."
        return evidence
    candidate = dict(files, **{path: source[:offset] + finding["afterCode"] + source[offset + len(before):]})
    evidence.update(tests=tests, testsSha256=digest({p: files[p] for p in tests}),
                    snapshotSha256=digest(files), candidateSha256=digest(candidate), command=command, workdir=workdir)
    deadline = min(deadline, time.monotonic() + 22)
    try:
        image = runtime._checked(["image", "inspect", "--format", "{{.Id}}", IMAGES[language]], deadline)
        # Resolve once; both executions use exactly the same immutable image.
        image = runtime._inspect_image(image, deadline)
        for label, snapshot in (("original", files), ("candidate", candidate)):
            if cancelled and (cancelled() if callable(cancelled) else cancelled.is_set()):
                evidence["reason"] = "Verification was cancelled."
                return evidence
            with tempfile.TemporaryDirectory(prefix="proofrun-repo-tests-") as directory:
                root = Path(directory)
                root.chmod(0o755)
                for name, body in snapshot.items():
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body, encoding="utf-8")
                    target.chmod(0o644)
                observation = runtime.run_tests(root, image, {"test_command": command, "workdir": workdir},
                                                min(deadline, time.monotonic() + 10))
            evidence[label] = observation
            evidence[label + "Tests"] = _counts(observation, language)
            if not evidence[label + "Tests"]:
                evidence["reason"] = "Test setup, execution, cleanup, or nonzero assertion-test evidence was incomplete."
                return evidence
            if label == "original":
                if not evidence["originalTests"]["failed"]:
                    evidence["status"] = "not_reproduced"
                    evidence["reason"] = "The selected tests passed on the original source."
                    return evidence
                evidence["status"] = "test_failure_reproduced"
        if (evidence["originalTests"]["ids"] == evidence["candidateTests"]["ids"]
                and not evidence["candidateTests"]["failed"]):
            evidence["patchStatus"] = "passes_selected_tests"
            evidence["reason"] = "Existing assertion failures became passes with identical test IDs and unchanged test files."
        else:
            evidence["reason"] = "The candidate failed tests or changed the executed test set."
    except (runtime.RuntimeFailure, OSError, ValueError):
        evidence["reason"] = "Docker or a prepared runtime image was unavailable, or the execution budget expired."
    return evidence
