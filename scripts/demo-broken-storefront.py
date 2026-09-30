#!/usr/bin/env python3
"""Replay one measured ProofRun example using isolated copies and original tests.

Usage: python3.12 scripts/demo-broken-storefront.py
Fast replay: add --prepared .commit-watch/broken-demo-walkthrough
Requires Git, Python 3.12, and network access to GitHub and the Python registry.
The script succeeds when the expected broken baseline and narrow repair are
reproduced; both full test suites intentionally remain red. No PR is published.
"""

import argparse
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY = "https://github.com/Eman-Gon/proofrun-broken-demo.git"
COMMIT = "1368aaaa4f2e3f0d91f70b86a4c8c37f79485d1b"
NICKNAME_TESTS = [
    "tests.test_models.CustomerContractTests.test_nickname_may_be_omitted",
    "tests.test_models.CustomerContractTests.test_explicit_null_is_valid",
    "tests.test_models.CustomerContractTests.test_object_nickname_stays_invalid",
]


def checked(command, *, cwd=None, log=None):
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if log:
        log.write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"Command failed: {command!r}\nSee {log}" if log else result.stdout)
    return result.stdout


def file_hashes(folder):
    return {
        str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
    }


def run_tests(python, folder, output, name, arguments):
    command = [str(python), "-m", "unittest", *arguments]
    result = subprocess.run(command, cwd=folder, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = output / f"{name}.log"
    log.write_text(result.stdout)
    count = re.search(r"Ran (\d+) tests? in", result.stdout)
    if not count:
        raise RuntimeError(f"Missing test summary in {log}")
    failure_count = re.search(r"failures=(\d+)", result.stdout)
    error_count = re.search(r"errors=(\d+)", result.stdout)
    tests = int(count.group(1))
    failures = int(failure_count.group(1)) if failure_count else 0
    errors = int(error_count.group(1)) if error_count else 0
    return {
        "working_directory": str(folder),
        "command": command,
        "tests_run": tests,
        "passed": tests - failures - errors,
        "assertion_failures": failures,
        "errors": errors,
        "exit_code": result.returncode,
        "log": str(log),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="python3.12", help="Python 3.12 interpreter (default: python3.12)")
    parser.add_argument("--prepared", type=Path, help="Reuse this directory's pinned source/ checkout and .venv/ without network or installation")
    parser.add_argument("--output-parent", type=Path, default=Path(__file__).resolve().parents[1] / ".commit-watch" / "broken-demo-replays", help="Parent for a new, isolated run directory")
    args = parser.parse_args()
    python = shutil.which(args.python)
    if not python:
        raise RuntimeError(f"Interpreter not found: {args.python}; install Python 3.12 or pass --python /path/to/python3.12")
    version = json.loads(checked([python, "-c", "import json,sys; print(json.dumps(list(sys.version_info[:2])))"]))
    if version != [3, 12]:
        raise RuntimeError(f"Python 3.12 is required; {python} reports {version}")
    args.output_parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_parent.resolve()))
    print(f"Evidence directory: {output}", flush=True)
    prepared = args.prepared.resolve() if args.prepared else output
    source = prepared / "source"
    if args.prepared:
        print(f"Reusing the prepared environment at {prepared}…", flush=True)
    else:
        print(f"Cloning the example at {COMMIT[:12]}…", flush=True)
        checked(["git", "clone", "--quiet", REPOSITORY, str(source)], log=output / "clone.log")
        checked(["git", "checkout", "--quiet", "--detach", COMMIT], cwd=source, log=output / "checkout.log")
    if checked(["git", "rev-parse", "HEAD"], cwd=source).strip() != COMMIT:
        raise RuntimeError("The source commit did not match the pinned example")
    if checked(["git", "status", "--porcelain"], cwd=source).strip():
        raise RuntimeError("The prepared source checkout must be clean")
    source_hashes = file_hashes(source)
    runtime = prepared / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not args.prepared:
        print("Creating an isolated environment with pydantic 2.8.2 and pandas 3.0.0…", flush=True)
        checked([python, "-m", "venv", str(prepared / ".venv")], log=output / "environment-create.log")
        checked([str(runtime), "-m", "pip", "install", "-r", str(source / "requirements.txt")], log=output / "dependency-install.log")
    versions = json.loads(checked([str(runtime), "-c", "import importlib.metadata,json,platform; print(json.dumps({'python':platform.python_version(),'pydantic':importlib.metadata.version('pydantic'),'pandas':importlib.metadata.version('pandas')}))"]))
    if not versions["python"].startswith("3.12.") or versions["pydantic"] != "2.8.2" or versions["pandas"] != "3.0.0":
        raise RuntimeError(f"Unexpected dependency versions: {versions}")
    for name in ("baseline", "candidate"):
        shutil.copytree(source, output / name, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    baseline, candidate = output / "baseline", output / "candidate"
    models = candidate / "broken_shop" / "models.py"
    original = models.read_text()
    old = "    nickname: Optional[str]\n"
    if original.count(old) != 1:
        raise RuntimeError("Expected exactly one original nickname declaration")
    patched = original.replace(old, "    nickname: Optional[str] = None\n")
    models.write_text(patched)
    patch_path = output / "nickname-only.patch"
    patch_path.write_text("".join(difflib.unified_diff(original.splitlines(keepends=True), patched.splitlines(keepends=True), fromfile="a/broken_shop/models.py", tofile="b/broken_shop/models.py")))
    full = ["discover", "-s", "tests", "-v"]
    selected = ["-v", *NICKNAME_TESTS]
    receipt = {
        "purpose": "Measured local example: a narrow fix leaves unrelated failures visible",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_repository": REPOSITORY,
        "source_commit": COMMIT,
        **versions,
        "patch": str(patch_path),
        "original_full_suite": run_tests(runtime, baseline, output, "baseline-full-suite", full),
        "original_nickname_checks": run_tests(runtime, baseline, output, "baseline-nickname-checks", selected),
        "candidate_nickname_checks": run_tests(runtime, candidate, output, "candidate-nickname-checks", selected),
        "candidate_full_suite": run_tests(runtime, candidate, output, "candidate-full-suite", full),
        "javascript_tests_run": False,
    }
    candidate_hashes = file_hashes(candidate)
    expected_files = set(source_hashes)
    if set(candidate_hashes) != expected_files or any(candidate_hashes[path] != source_hashes[path] for path in expected_files if path != "broken_shop/models.py"):
        raise RuntimeError("Unexpected candidate file changes")
    if file_hashes(source) != source_hashes or file_hashes(baseline) != source_hashes:
        raise RuntimeError("Source or baseline files unexpectedly changed")
    receipt["test_files_unchanged"] = True
    receipt["only_changed_file"] = "broken_shop/models.py"
    receipt["source_checkout_unchanged"] = True
    receipt["original_test_sha256"] = {name: value for name, value in source_hashes.items() if name.startswith("tests/")}
    receipt_path = output / "verification-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    for label in ("original_full_suite", "original_nickname_checks", "candidate_nickname_checks", "candidate_full_suite"):
        row = receipt[label]
        print(f"{label}: {row['tests_run']} tests; {row['passed']} passed, {row['assertion_failures']} failures, {row['errors']} errors; exit {row['exit_code']}", flush=True)
    expected = {
        "original_full_suite": (25, 2, 11, 12, 1),
        "original_nickname_checks": (3, 2, 0, 1, 1),
        "candidate_nickname_checks": (3, 3, 0, 0, 0),
        "candidate_full_suite": (25, 3, 11, 11, 1),
    }
    for label, counts in expected.items():
        actual = tuple(receipt[label][field] for field in ("tests_run", "passed", "assertion_failures", "errors", "exit_code"))
        if actual != counts:
            raise RuntimeError(f"Unexpected {label} outcome: {actual}; expected {counts}; see {receipt_path}")
    print("Demo reproduced. The three selected checks pass after the fix; the full suite still has 22 intentional failures/errors.", flush=True)
    print(f"Receipt: {receipt_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"Demo failed: {error}", file=sys.stderr)
        raise SystemExit(1)
