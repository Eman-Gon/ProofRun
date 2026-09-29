"""Release investigation: agents select experiments; fixed policy evaluates evidence."""
from __future__ import annotations

import copy
import difflib
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import unquote, urlsplit

from . import release_agent, release_runtime
from .release_contracts import SCHEMA, canonical, digest, relative_path
from .release_repository import SourceTools, excluded, snapshot


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


class Investigation:
    def __init__(self, target, request, directory, *, runtime=release_runtime, model=None, emit=None):
        self.target, self.request = copy.deepcopy(target), copy.deepcopy(request)
        self.directory = Path(directory)
        self.runtime, self.model = runtime, model
        self.emit = emit or (lambda event: None)
        self.started = time.monotonic()
        self.deadline = self.started + request["budget_seconds"]
        self.roots, self.manifests, self.tests = {}, {}, {}
        self.probes, self.findings, self.repairs, self.trace = [], [], [], []
        self.sources = None
        self.protected = set()
        self.inspected = set()
        self.staging = {"status": "not_configured" if not target.get("staging") else "not_checked"}

    def event(self, event):
        self.trace.append(event)
        self.emit(event)

    def write(self, name, data):
        (self.directory / name).write_bytes(canonical(data))

    def prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for revision in ("baseline", "candidate"):
            self.event({"stage": "snapshot", "revision": revision})
            self.roots[revision] = self.directory / revision
            self.manifests[revision] = snapshot(Path(self.target["repository"]),
                self.request[revision + "_revision"], self.roots[revision], self.deadline,
                self.target["exclude_paths"])
        self.sources = SourceTools(self.roots, self.manifests)
        self.protected = {e["path"] for e in self.manifests["baseline"]["files"]
                          if matches(e["path"], self.target["test_paths"])}
        if not self.protected:
            raise ValueError("No baseline tests match the configured protected test paths.")
        self.write("source-manifests.json", self.manifests)
        for revision in ("baseline", "candidate"):
            self.event({"stage": "original_tests", "revision": revision})
            self.tests[revision] = self.original_tests(self.roots[revision], revision, revision + "-suite")

    def image(self, revision):
        return self.target["images"][self.request[revision + "_revision"]]

    def original_tests(self, root, revision, name):
        # Keep the baseline suite unchanged even if the release/repair deletes or weakens it.
        testroot = self.directory / name
        shutil.copytree(root, testroot)
        for file in list(testroot.rglob("*")):
            if file.is_file() and matches(file.relative_to(testroot).as_posix(), self.target["test_paths"]):
                file.unlink()
        for path in self.protected:
            file = testroot / path
            file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.roots["baseline"] / path, file)
        result = self.check_test_result(self.runtime.run_tests(testroot, self.image(revision), self.target["runtime"], self.deadline))
        result["frozen_test_hash"] = digest({p: hashlib.sha256((self.roots["baseline"] / p).read_bytes()).hexdigest()
                                            for p in sorted(self.protected)})
        if revision == "candidate":
            current = self.check_test_result(self.runtime.run_tests(root, self.image(revision), self.target["runtime"], self.deadline))
            result["release_suite"] = current
            if current.get("test_status") != "passed":
                result["test_status"] = current["test_status"]
        return result

    def check_test_result(self, result):
        result["completion_marker_observed"] = bool(re.search(self.target["test_success_pattern"],
                                                              result.get("output_tail", "")))
        if (result.get("execution_status") != "completed" or result.get("complete") is not True
                or (result.get("test_status") == "passed" and
                    (not result["completion_marker_observed"] or result.get("exit_code") != 0))):
            result["test_status"] = "unavailable"
        return result

    def validate_probe(self, args):
        if not isinstance(args, dict) or set(args) - {"name", "requirement_id", "steps", "hypothesis"}:
            raise ValueError("A probe cannot supply an expected result or modify acceptance policy.")
        req = next((r for r in self.target["requirements"] if r["id"] == args.get("requirement_id")), None)
        if not req:
            raise ValueError("Choose an approved requirement id.")
        steps = args.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 12 or len(canonical(steps)) > 24000:
            raise ValueError("A probe needs 1–12 bounded synthetic HTTP requests.")
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"method", "path", "json"}:
                raise ValueError("Only method, path and JSON are permitted in a request.")
            raw = step.get("path", "")
            if not isinstance(raw, str) or len(raw) > 2000 or any(ord(c) < 32 for c in raw):
                raise ValueError("Invalid request path.")
            parsed = urlsplit(raw)
            path = unquote(parsed.path)
            prefix = req["path_prefix"].rstrip("/")
            if (parsed.scheme or parsed.netloc or parsed.fragment or "\\" in raw or "%" in path
                    or any(p in (".", "..") for p in path.split("/"))
                    or not (path == prefix or path.startswith(prefix + "/"))
                    or step.get("method") not in req["methods"]):
                raise ValueError("Request is outside this requirement's approved endpoint/method scope.")
        for field in ("name", "hypothesis"):
            if not isinstance(args.get(field), str) or not 1 <= len(args[field]) <= 2000:
                raise ValueError("Probes require a bounded name and hypothesis.")
        return req

    def replay(self, root, revision, steps):
        return self.runtime.run_probe(root, self.image(revision), self.target["runtime"], steps, self.deadline)

    @staticmethod
    def completed(result, count):
        return (result.get("execution_status") == "completed" and result.get("complete") is True
                and len(result.get("observations", [])) == count)

    def probe(self, args):
        req = self.validate_probe(args)
        if len(self.probes) >= 8:
            raise ValueError("The run's eight-experiment limit has been reached.")
        evidence = {"id": f"probe-{len(self.probes) + 1}", **copy.deepcopy(args), "runs": {}}
        self.probes.append(evidence)  # Interrupted attempts remain visible and prevent a false pass.
        for revision in ("baseline", "candidate"):
            evidence["runs"][revision] = [self.replay(self.roots[revision], revision, args["steps"]) for _ in range(2)]
        runs = evidence["runs"]
        complete = all(self.completed(r, len(args["steps"])) for group in runs.values() for r in group)
        stable = complete and all(canonical(group[0]["observations"]) == canonical(group[1]["observations"]) for group in runs.values())
        evidence["status"] = "inconclusive"
        if stable:
            evidence["status"] = ("preserved" if canonical(runs["baseline"][0]["observations"]) ==
                                  canonical(runs["candidate"][0]["observations"]) else "regression")
        if evidence["status"] != "preserved":
            finding = {"id": f"finding-{len(self.findings) + 1}", "title": args["name"],
                       "requirement_id": req["id"],
                       "status": "confirmed" if evidence["status"] == "regression" else "inconclusive",
                       "hypothesis": args["hypothesis"], "cause_status": "hypothesis_not_proven",
                       "evidence": {"probe_id": evidence["id"], "probe_hash": digest(evidence),
                                    "artifact": "experiments.json", "replays_per_revision": 2,
                                    "baseline": runs["baseline"][0].get("observations", []),
                                    "candidate": runs["candidate"][0].get("observations", [])}}
            self.findings.append(finding)
        self.event({"stage": "experiment", "probe_id": evidence["id"], "status": evidence["status"]})
        return evidence

    def repair(self, args):
        if not self.request["repair"]:
            raise ValueError("Repairs were not requested.")
        if len(self.repairs) >= 2:
            raise ValueError("The two-candidate repair limit has been reached.")
        if not isinstance(args, dict) or set(args) != {"finding_id", "changes", "rationale"}:
            raise ValueError("Repair requires a finding id, replacement application files and rationale.")
        finding = next((f for f in self.findings if f["id"] == args["finding_id"] and f["status"] == "confirmed"), None)
        if not finding:
            raise ValueError("Repairs require a reproduced finding.")
        changes = args["changes"]
        if not isinstance(changes, list) or not 1 <= len(changes) <= 4 or len(canonical(changes)) > 128000:
            raise ValueError("A repair may replace 1–4 existing application files up to 128 KB total.")
        paths = set()
        existing = {e["path"] for e in self.manifests["candidate"]["files"]}
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"path", "content"} or not isinstance(change["content"], str):
                raise ValueError("Each change needs an application path and text content.")
            path = relative_path(change["path"])
            basename = Path(path).name.lower()
            if (path not in existing or path in paths or excluded(path) or path in self.protected
                    or matches(path, self.target["test_paths"]) or not matches(path, self.target["repair_paths"])
                    or any(p in basename for p in ("test", "lock", "requirement", "dockerfile", "config"))
                    or Path(path).suffix not in (".py", ".js", ".ts", ".go", ".java", ".rb", ".rs", ".cs")):
                raise ValueError("Repair attempted to modify a protected or unapproved application file.")
            paths.add(path)
        index = len(self.repairs) + 1
        root = self.directory / f"repair-{index}"
        shutil.copytree(self.roots["candidate"], root)
        patch = ""
        for change in changes:
            path = change["path"]
            old = (root / path).read_text()
            (root / path).write_text(change["content"])
            patch += "".join(difflib.unified_diff(old.splitlines(True), change["content"].splitlines(True),
                                                fromfile="a/" + path, tofile="b/" + path))
        (self.directory / f"repair-{index}.diff").write_text(patch)
        record = {"id": f"repair-{index}", "finding_id": finding["id"], "status": "inconclusive",
                  "rationale": str(args["rationale"])[:2000], "patch_artifact": f"repair-{index}.diff",
                  "candidate_revision": self.request["candidate_revision"], "deployed": False,
                  "tree_hash": digest({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in sorted(root.rglob("*")) if p.is_file()}), "probes": []}
        self.repairs.append(record)
        record["tests"] = self.original_tests(root, "candidate", f"repair-{index}-suite")
        controls = [p for p in self.probes if p.get("status") in ("preserved", "regression")]
        record["frozen_probe_hash"] = digest(self.probes)
        # Require both the failure and independently passing behavior before accepting a repair.
        sufficient = (any(p.get("status") == "preserved" for p in controls)
                      and self.sufficient_inputs(controls)
                      and not any(p.get("status") == "inconclusive" for p in self.probes))
        for probe in controls:
            replays = [self.replay(root, "candidate", probe["steps"]) for _ in range(2)]
            expected = probe["runs"]["baseline"][0]["observations"]
            passed = all(self.completed(r, len(probe["steps"])) and canonical(r["observations"]) == canonical(expected) for r in replays)
            record["probes"].append({"id": probe["id"], "passed": passed, "runs": replays})
        passed = bool(controls) and all(p["passed"] for p in record["probes"])
        tests_passed = record["tests"].get("test_status") == "passed" and self.tests["baseline"].get("test_status") == "passed"
        if sufficient and passed and tests_passed:
            record["status"] = "verified_candidate"
        elif not passed or record["tests"].get("test_status") == "failed":
            record["status"] = "rejected"
        record["limitations"] = ["Candidate checked against frozen experiments and baseline tests; review required."]
        if not sufficient:
            record["limitations"].append("Missing independent passing controls or requirement coverage.")
        finding["repair"] = {k: record[k] for k in ("id", "status", "patch_artifact", "tree_hash", "deployed")}
        self.event({"stage": "repair_verification", "repair_id": record["id"], "status": record["status"]})
        return record

    def tool(self, name, args):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Investigation deadline expired.")
        if name in {"read_file", "list_files", "diff", "search"}:
            result = self.sources.invoke(name, args)
            if args.get("path") and ((name == "read_file" and result.get("text")) or
                                     (name == "diff" and result.get("diff"))):
                self.inspected.add(args["path"])
            return result
        if name == "run_probe":
            return self.probe(args)
        if name == "propose_repair":
            return self.repair(args)
        raise ValueError("Unsupported investigation tool.")

    def check_staging(self):
        if not self.target.get("staging"):
            return
        replays = []
        for probe in self.probes:
            if probe.get("status") == "inconclusive":
                continue
            result = self.runtime.run_staging(self.target["staging"], probe["steps"],
                                              self.request["candidate_revision"], self.deadline)
            expected = probe["runs"]["candidate"][0]["observations"]
            matched = self.completed(result, len(probe["steps"])) and canonical(result["observations"]) == canonical(expected)
            replays.append({"probe_id": probe["id"], "matched_candidate": matched, "run": result})
        self.staging = {"status": "verified" if replays and all(r["matched_candidate"] for r in replays)
                        else "inconclusive", "replays": replays,
                        "limitation": "Only the observed revision and replayed synthetic requests were checked."}

    def sufficient_inputs(self, probes):
        return all(len({digest(p["steps"]) for p in probes if p["requirement_id"] == req["id"]}) >= 2
            and any(p["requirement_id"] == req["id"]
                and any(200 <= o.get("status", 0) < 300 for o in p["runs"]["baseline"][0]["observations"])
                for p in probes) for req in self.target["requirements"])

    def result(self, agent, error=None):
        for repair in self.repairs:
            if repair["status"] == "verified_candidate" and repair.get("frozen_probe_hash") != digest(self.probes):
                repair["status"] = "verification_stale"
                for finding in self.findings:
                    if finding.get("repair", {}).get("id") == repair["id"]:
                        finding["repair"]["status"] = "verification_stale"
        exercised = {p["requirement_id"] for p in self.probes if p.get("status") in ("preserved", "regression")}
        sufficient_inputs = self.sufficient_inputs([p for p in self.probes if p.get("status") == "preserved"])
        confirmed = any(f["status"] == "confirmed" for f in self.findings)
        candidate_failed = self.tests.get("candidate", {}).get("test_status") == "failed"
        baseline_passed = self.tests.get("baseline", {}).get("test_status") == "passed"
        complete = (agent.get("status") == "completed" and not error
                    and len(exercised) == len(self.target["requirements"])
                    and all(t.get("test_status") == "passed" for t in self.tests.values())
                    and len(self.tests) == 2 and bool(self.probes)
                    and all(p.get("status") == "preserved" for p in self.probes)
                    and sufficient_inputs and bool(self.inspected)
                    and (not self.target.get("staging") or self.staging["status"] == "verified"))
        if confirmed or (baseline_passed and candidate_failed):
            recommendation, summary = "skip", "Keep the baseline: the candidate has a reproduced compatibility or original-test failure."
        elif complete and self.request["benefit"].strip():
            recommendation, summary = "update", "The investigated behaviors and original tests passed; the supplied benefit supports updating within this tested scope."
        else:
            recommendation, summary = "postpone", "Evidence is incomplete, unstable, or the release benefit is unspecified; keep the baseline for now."
        limitations = ["Bounded HTTP compatibility investigation; untested workflows and arbitrary repository bugs remain outside its evidence.",
                      "Exact response preservation is an operator-approved requirement; intentional behavior changes need a revised contract.",
                      "A hypothesis about the cause is distinct from a reproduced behavior change.",
                      "Image-to-release mapping and synthetic runtime setup are supplied by the operator.",
                      "Source exclusions and repository bounds are recorded in source-manifests.json.",
                      "Verified repairs are local candidates; no repair has been deployed."]
        if time.monotonic() >= self.deadline:
            limitations.append("The investigation exhausted its execution budget.")
        return {"schema_version": SCHEMA, "recommendation": recommendation, "summary": summary,
                "target_id": self.target["id"], "contract_hash": self.target["contract_hash"],
                "request_hash": digest(self.request),
                "revisions": {r: self.request[r + "_revision"] for r in ("baseline", "candidate")},
                "source_hashes": {r: m["tree_hash"] for r, m in self.manifests.items()},
                "coverage": {"requirements_total": len(self.target["requirements"]),
                             "requirements_exercised": len(exercised), "experiments": len(self.probes),
                             "files_inspected": sorted(self.inspected), "sufficient_inputs": sufficient_inputs},
                "findings": self.findings, "tests": self.tests, "repairs": self.repairs,
                "agent": agent, "staging": self.staging, "limitations": limitations,
                "elapsed_seconds": round(time.monotonic() - self.started, 2), "error": error}

    def run(self):
        agent = {"status": "failed", "summary": "Investigation did not start.", "steps": 0, "provenance": []}
        error = None
        try:
            self.prepare()
            context = {"target": {"id": self.target["id"], "name": self.target["name"]},
                       "requirements": self.target["requirements"], "revisions": self.request,
                       "diff": self.sources.invoke("diff", {}),
                       "tests": {r: {**t, "output_tail": t.get("output_tail", "")[-4000:]} for r, t in self.tests.items()},
                       "inventory": self.sources.invoke("list_files", {"revision": "candidate"}),
                       "repair_enabled": self.request["repair"], "repair_paths": self.target["repair_paths"],
                       "completion_scope": "Inspect changed source; exercise at least two distinct inputs per requirement, including a successful baseline workflow. Collect independent passing controls before proposing repair. Existing baseline and candidate suites must both pass. Time or missing coverage means postpone.",
                       "benefit": self.request["benefit"], "tool_limit": 30}
            agent = release_agent.investigate(context, self.tool, self.deadline, emit=self.event, model=self.model)
            self.check_staging()
        except TimeoutError:
            error = "The execution budget expired before investigation completed."
            agent["status"] = "timed_out"
        except (ValueError, OSError):
            # Never include arbitrary subprocess/provider/credential-bearing exception text.
            error = "Source preparation or investigation failed; inspect the configured target and recorded stages."
        finally:
            self.write("experiments.json", self.probes)
            self.write("trace.json", self.trace)
        result = self.result(agent, error)
        self.write("repairs.json", self.repairs)
        artifacts = []
        for file in sorted(self.directory.iterdir()):
            if file.is_file() and file.suffix in (".json", ".diff"):
                data = file.read_bytes()
                artifacts.append({"id": file.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        result["artifacts"] = artifacts
        self.write("result.json", result)
        return result
