"""One concurrent, file-backed verification worker. No arbitrary code submissions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from typing import Any
from uuid import uuid4

from .contracts import (SCHEMA_VERSION, CaseSpec, SourceBundle, ComparisonEvidence,
                        ProposalUnavailable)

REPOSITORY = "https://github.com/Eman-Gon/ProofRun"
CASE_ID = "customer-nickname-v1"
MODULE = "demo/upgrade/app.py"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TERMINAL = {"completed", "setup_failed", "timed_out", "interrupted"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_artifact_refs(value: Any, prefix: str, identifiers: set[str]) -> Any:
    """Relabel references for an attempt without altering observations/verdicts."""
    if isinstance(value, list):
        return [_public_artifact_refs(item, prefix, identifiers) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _public_artifact_refs(item, prefix, identifiers) for key, item in value.items()}
    if isinstance(value.get("output_ref"), str) and value["output_ref"] in identifiers:
        result["output_ref"] = prefix + value["output_ref"]
    if isinstance(value.get("artifacts"), list):
        result["artifacts"] = [prefix + item if isinstance(item, str) and item in identifiers else item
                               for item in value["artifacts"]]
    return result


class ServiceError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RunService:
    def __init__(self, root: Path, artifact_dir: Path, execution_target="local",
                 worker_id=None, runner_mode="native", compare=None, verify=None,
                 propose=None, band_handoff=None):
        self.root = Path(root).resolve()
        self.artifact_dir = Path(artifact_dir).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if runner_mode not in {"native", "prepared"}:
            raise ValueError("Unknown runner mode")
        if execution_target not in {"local", "crusoe"}:
            raise ValueError("Unknown execution target")
        self.runner_mode = runner_mode
        self.worker_code_hashes = {name: sha256((Path(__file__).parent / name).read_bytes())
                                   for name in ("contracts.py", "api.py", "service.py", "band.py", "band_sdk.py")}
        self.execution_target, self.worker_id = execution_target, worker_id or "local-worker"
        self.compare, self.verify, self.propose = compare, verify, propose
        self.band_handoff = band_handoff
        self._mutex = threading.RLock()
        self._process_lock = (self.artifact_dir / "worker.lock").open("a")
        try:
            fcntl.flock(self._process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._process_lock.close()
            raise ValueError("Another worker owns this artifact directory") from None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="proofrun")
        self._records: dict[str, dict] = {}
        self._keys: dict[str, str] = {}
        self._active: str | None = None
        self._closed = False
        for path in sorted(self.artifact_dir.glob("run-*/record.json")):
            record = json.loads(path.read_text())
            run_id = record["run_id"]
            if not SAFE_ID.fullmatch(run_id) or path.parent.name != run_id:
                raise ValueError("Invalid persisted run identity")
            if record["job_key"] in self._keys:
                raise ValueError("Duplicate persisted job key")
            self._records[run_id] = record
            self._keys[record["job_key"]] = run_id
            if record["execution_status"] not in TERMINAL:
                record.update(execution_status="interrupted", updated_at=now())
                if record["repair_status"] in {"pending", "proposed"}:
                    record["repair_status"] = "unavailable"
                record["limitations"].append("Worker restarted before this run completed; submit a new job key.")
                self._save(record)

    def _save(self, record: dict) -> None:
        destination = self.artifact_dir / record["run_id"] / "record.json"
        temporary = destination.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)

    def _registered(self) -> tuple[dict, dict, bytes]:
        try:
            raw_contract = (self.root / "demo/upgrade/contract.json").read_bytes()
            contract = json.loads(raw_contract)
            content = (self.root / MODULE).read_bytes()
            revision = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                                               text=True, timeout=10).strip()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ServiceError(503, "case_unavailable", "Registered case or source revision is unavailable") from None
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ServiceError(503, "case_unavailable", "Registered source needs a complete Git revision")
        if (contract.get("case_id") != CASE_ID or contract.get("allowed_path") != MODULE
                or contract.get("schema_version") != SCHEMA_VERSION):
            raise ServiceError(503, "case_unavailable", "Registered contract is unsupported")
        source_hash = sha256(content)
        source = {"repository": REPOSITORY, "revision": revision, "module": MODULE,
                  "sha256": source_hash,
                  "bundle_sha256": sha256(canonical({"module": MODULE, "sha256": source_hash}))}
        submission = {"schema_version": SCHEMA_VERSION, "case_id": CASE_ID,
                      "source": source,
                      "contract": {"id": contract["contract_id"], "sha256": sha256(raw_contract)},
                      "environments": {"baseline": "pydantic-1.10.18", "updated": "pydantic-2.8.2"},
                      "repair": {"enabled": False, "max_attempts": 2}}
        return submission, contract, content

    def get_case(self, case_id: str) -> dict:
        if case_id != CASE_ID:
            raise ServiceError(404, "unknown_case", "Case is not registered")
        submission, contract, _ = self._registered()
        return {"schema_version": SCHEMA_VERSION, "case_id": CASE_ID,
                "submission": submission, "requirements": contract,
                "limitations": ["One registered synthetic customer-input case.",
                                "Local execution is not Crusoe hosting evidence."]}

    @staticmethod
    def _public(record: dict) -> dict:
        return json.loads(json.dumps({k: v for k, v in record.items() if not k.startswith("_")}))

    def _validate(self, payload: dict) -> tuple[dict, dict, bytes]:
        def invalid(message):
            raise ServiceError(400, "invalid_submission", message)
        required = {"schema_version", "job_key", "case_id", "source", "contract", "environments", "repair"}
        if not isinstance(payload, dict) or set(payload) != required:
            invalid("Supply exactly the proofrun.v1 submission fields")
        if payload["schema_version"] != SCHEMA_VERSION or payload["case_id"] != CASE_ID:
            invalid("Unsupported schema version or unregistered case")
        if not isinstance(payload["job_key"], str) or not SAFE_ID.fullmatch(payload["job_key"]):
            invalid("job_key must contain 1–128 letters, digits, dots, underscores or hyphens")
        repair = payload["repair"]
        if (not isinstance(repair, dict) or set(repair) != {"enabled", "max_attempts"}
                or type(repair["enabled"]) is not bool or type(repair["max_attempts"]) is not int
                or not 1 <= repair["max_attempts"] <= 2):
            invalid("repair needs enabled boolean and max_attempts 1 or 2")
        registered, contract, content = self._registered()
        for field in ("contract", "environments"):
            if payload[field] != registered[field]:
                invalid(f"{field} bindings do not match the registered case")
        source = payload["source"]
        if not isinstance(source, dict) or set(source) not in (
                {"repository", "revision", "module", "sha256"},
                {"repository", "revision", "module", "sha256", "bundle_sha256"}):
            invalid("Invalid source fields")
        for key, value in source.items():
            if value != registered["source"][key]:
                invalid("Source revision/content/bundle must match the registered source")
        if "bundle_sha256" not in source:
            try:
                committed = subprocess.check_output(
                    ["git", "-C", str(self.root), "show", f'{source["revision"]}:{MODULE}'],
                    stderr=subprocess.DEVNULL, timeout=10)
            except subprocess.SubprocessError:
                invalid("Source revision cannot be resolved")
            if committed != content:
                invalid("Uncommitted application content requires an explicit bundle_sha256")
        return registered, contract, content

    def submit(self, payload: dict) -> tuple[dict, bool]:
        if not isinstance(payload, dict):
            raise ServiceError(400, "invalid_submission", "Submission must be a JSON object")
        with self._mutex:
            fingerprint = sha256(canonical(payload))
            key = payload.get("job_key")
            if isinstance(key, str) and key in self._keys:
                existing = self._records[self._keys[key]]
                if fingerprint != existing["_input_sha256"]:
                    raise ServiceError(409, "job_key_conflict", "job_key already binds a different submission")
                return self._public(existing), False
            if self._closed:
                raise ServiceError(503, "worker_stopping", "Worker is stopping")
            registered, contract, content = self._validate(payload)
            if self._active:
                raise ServiceError(409, "worker_busy", "The worker already has an active run; retry later")
            run_id = "run-" + uuid4().hex
            run_dir = self.artifact_dir / run_id
            run_dir.mkdir(mode=0o700)
            snapshot = run_dir / "source"
            # Only approved fixture inputs are copied; clients cannot select executable paths.
            for relative in [MODULE, "demo/upgrade/fixed_app.py", "demo/upgrade/permissive_app.py", "demo/upgrade/test_existing.py",
                             "demo/upgrade/test_controls.py",
                             "demo/upgrade/test_upgrade.py", "demo/upgrade/contract.json",
                             "demo/upgrade/requirements-old.txt", "demo/upgrade/requirements-new.txt",
                             "demo/upgrade/source.json", "sandbox/upgrade.Dockerfile"]:
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / relative, target)
            # Detect concurrent edits during snapshotting instead of attributing changed input.
            if ((snapshot / MODULE).read_bytes() != content
                    or sha256((snapshot / "demo/upgrade/contract.json").read_bytes()) != registered["contract"]["sha256"]):
                shutil.rmtree(run_dir)
                raise ServiceError(409, "source_changed", "Registered inputs changed while submitting; retry")
            source_fields = dict(registered["source"])
            bundle = SourceBundle(**source_fields, content=content.decode("utf-8"))
            spec = CaseSpec(case_id=CASE_ID, contract_id=contract["contract_id"],
                            contract_sha256=registered["contract"]["sha256"], root=snapshot,
                            artifact_dir=run_dir / "evidence", model_name=contract["model_name"],
                            field_name=contract["field_name"], allowed_path=MODULE,
                            expected_case_ids=tuple(c["id"] for c in contract["cases"]),
                            original_test_ids=tuple(contract["original_test_ids"]))
            spec.artifact_dir.mkdir(mode=0o700)
            record = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "job_key": key,
                      "case_id": CASE_ID, "execution_status": "queued", "finding_status": "not_tested",
                      "repair_status": "pending" if payload["repair"]["enabled"] else "not_requested",
                      "bindings": {"revision": bundle.revision, "source_sha256": bundle.sha256,
                                   "contract_sha256": spec.contract_sha256, "bundle_sha256": bundle.bundle_sha256},
                      "execution": {"target": self.execution_target, "worker_id": self.worker_id,
                                    "runner": self.runner_mode, "data": "synthetic", "measurement": "fresh",
                                    "worker_revision": bundle.revision,
                                    "worker_code_sha256": self.worker_code_hashes},
                      "proposal": None, "cases": [], "artifacts": [], "attempts": [],
                      "limitations": [], "created_at": now(), "updated_at": now(),
                      "_input_sha256": fingerprint, "_artifact_paths": {}}
            self._records[run_id] = record
            self._keys[key] = run_id
            self._active = run_id
            self._save(record)
            result = self._public(record)
            self._pool.submit(self._execute, record, spec, bundle, payload["repair"], contract)
            return result, True

    def get_run(self, run_id: str) -> dict:
        with self._mutex:
            if run_id not in self._records:
                raise ServiceError(404, "unknown_run", "Run was not found")
            return self._public(self._records[run_id])

    def _change(self, record: dict, **values) -> None:
        with self._mutex:
            if "limitations" in values:
                values["limitations"] = list(dict.fromkeys(values["limitations"]))
            record.update(values, updated_at=now())
            self._save(record)

    def _add_artifacts(self, record: dict, paths: dict[str, str], artifact_root: Path,
                       prefix="") -> None:
        with self._mutex:
            for identifier, raw_path in paths.items():
                public_id = prefix + identifier
                path = Path(raw_path).resolve()
                if (not SAFE_ID.fullmatch(public_id) or not path.is_relative_to(artifact_root.resolve())
                        or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024):
                    raise ValueError("Runner returned an invalid artifact")
                data = path.read_bytes()
                if prefix and path.suffix == ".json":
                    document = _public_artifact_refs(json.loads(data), prefix, set(paths))
                    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
                item = {"id": public_id, "sha256": sha256(data), "size_bytes": len(data),
                        "href": f'/v1/runs/{record["run_id"]}/artifacts/{public_id}'}
                # Once exposed, an artifact's bytes and identity may never change.
                published = self.artifact_dir / record["run_id"] / "published"
                published.mkdir(exist_ok=True, mode=0o700)
                target = published / public_id
                if target.exists() and target.read_bytes() != data:
                    raise ValueError("An artifact identity was reused for different content")
                target.write_bytes(data)
                target.chmod(0o600)
                record["_artifact_paths"][public_id] = str(target)
                record["artifacts"] = [a for a in record["artifacts"] if a["id"] != public_id] + [item]
            self._save(record)

    def artifact(self, run_id: str, artifact_id: str) -> tuple[bytes, str]:
        with self._mutex:
            self.get_run(run_id)
            record = self._records[run_id]
            path_value = record["_artifact_paths"].get(artifact_id)
            if not path_value:
                raise ServiceError(404, "unknown_artifact", "Artifact was not found")
            path = Path(path_value).resolve()
            allowed = (self.artifact_dir / run_id / "published").resolve()
            if not path.is_relative_to(allowed) or not path.is_file():
                raise ServiceError(404, "unknown_artifact", "Artifact is unavailable")
            data = path.read_bytes()
            metadata = next(a for a in record["artifacts"] if a["id"] == artifact_id)
            if sha256(data) != metadata["sha256"]:
                raise ServiceError(409, "artifact_changed", "Artifact content no longer matches its recorded hash")
            kind = "application/json" if artifact_id.endswith(".json") else "text/plain; charset=utf-8"
            return data, kind

    def _bound(self, evidence, bundle: SourceBundle, spec: CaseSpec) -> None:
        if evidence.execution_status not in TERMINAL:
            raise ValueError("Runner returned an invalid final execution state")
        expected = {"revision": bundle.revision, "source_sha256": bundle.sha256,
                    "contract_sha256": spec.contract_sha256}
        if any(evidence.bindings.get(k) != v for k, v in expected.items()):
            raise ValueError("Runner evidence does not match the submitted bindings")
        if evidence.execution_status == "completed":
            ids = evidence.expected_case_ids
            executed = evidence.executed_case_ids
            if not ids or len(set(ids)) != len(ids) or len(set(executed)) != len(executed) or set(ids) != set(executed):
                raise ValueError("Runner evidence has missing, empty, or duplicate checks")
            if self.runner_mode == "native":
                stages = ("baseline", "updated") if isinstance(evidence, ComparisonEvidence) else ("repaired_baseline", "repaired_updated")
                if not re.fullmatch(r"[0-9a-f]{64}", evidence.bindings.get("verifier_sha256", "")):
                    raise ValueError("Runner evidence lacks verifier identity")
                if set(evidence.environments) != set(stages):
                    raise ValueError("Runner evidence lacks the actual environment matrix")
                for stage, version, label in zip(stages, (spec.baseline_version, spec.updated_version), ("old", "new")):
                    env = evidence.environments[stage]
                    if (not isinstance(env, dict)
                            or not re.fullmatch(r"sha256:[0-9a-f]{64}", env.get("image_id", ""))
                            or env.get("observed_version") != version or env.get("expected_version") != version
                            or env.get("requirements_sha256") != sha256((spec.root / f"demo/upgrade/requirements-{label}.txt").read_bytes())):
                        raise ValueError("Runner environment does not match the approved pins/runtime")
                required = {f"{stage}:{case_id}" for stage in stages
                            for case_id in (*spec.expected_case_ids, *spec.original_test_ids)}
                observations = [f'{c.get("stage")}:{c.get("id")}' for c in evidence.cases]
                executed_rows = [f'{c.get("stage")}:{c.get("test_id")}' for c in evidence.cases]
                if (len(observations) != len(required) or set(observations) != required
                        or set(executed_rows) != set(executed)
                        or any(c.get("status") not in {"passed", "failed", "error", "skipped"} for c in evidence.cases)):
                    raise ValueError("Runner evidence does not contain the registered case matrix")
            for key in ("environment_manifest_sha256", "tests_sha256"):
                if not re.fullmatch(r"[0-9a-f]{64}", evidence.bindings.get(key, "")):
                    raise ValueError("Runner evidence lacks environment or test identity")

    def _execute(self, record, spec, bundle, repair, contract):
        try:
            self._change(record, execution_status="running")
            compare = self.compare
            if compare is None:
                if self.runner_mode == "prepared":
                    compare = self._prepared_comparison
                else:
                    from .runner import run_comparison
                    compare = run_comparison
            comparison = compare(spec, bundle)
            self._bound(comparison, bundle, spec)
            self._add_artifacts(record, comparison.artifacts, spec.artifact_dir)
            self._change(record, finding_status=comparison.finding_status,
                         bindings={**record["bindings"], **comparison.bindings},
                         environments=comparison.environments, cases=comparison.cases,
                         expected_case_ids=comparison.expected_case_ids,
                         executed_case_ids=comparison.executed_case_ids,
                         limitations=comparison.limitations)
            if comparison.execution_status != "completed":
                self._change(record, execution_status=comparison.execution_status,
                             repair_status="unavailable" if repair["enabled"] else "not_requested")
                return
            if not repair["enabled"]:
                self._change(record, execution_status="completed")
                return
            if self.runner_mode == "prepared":
                self._change(record, execution_status="completed", repair_status="unavailable",
                             proposal={"mode": "prepared", "gateway": None},
                             limitations=record["limitations"] + ["Prepared fix is illustrative; strengthened verifier is required for acceptance."])
                return
            if comparison.finding_status != "regression_reproduced":
                self._change(record, execution_status="completed", repair_status="unavailable",
                             limitations=record["limitations"] + ["No supported reproduced regression to send for repair."])
                return
            propose = self.propose
            if propose is None:
                from .repair import propose_patch
                propose = propose_patch
            verify = self.verify
            if verify is None:
                from .runner import verify_candidate
                verify = verify_candidate
            from .band import BandUnavailable, configured_handoff
            try:
                handoff = self.band_handoff if self.band_handoff is not None else configured_handoff()
            except BandUnavailable:
                self._change(record, execution_status="completed", repair_status="unavailable",
                             coordination={"provider": "band", "mode": "live", "status": "unavailable"},
                             limitations=record["limitations"] + ["BAND handoff configuration is unavailable; no repair was attempted."])
                return
            context = dict(comparison.failure_context)
            context.update(source=asdict(bundle), case={"case_id": spec.case_id,
                "contract_id": spec.contract_id, "contract_sha256": spec.contract_sha256,
                "model_name": spec.model_name, "field_name": spec.field_name,
                "allowed_path": spec.allowed_path, "expected_case_ids": list(spec.expected_case_ids)},
                requirements=contract,
                requirements_json=(spec.root / "demo/upgrade/contract.json").read_text(),
                comparison={k: v for k, v in comparison.to_dict().items() if k not in {"artifacts", "failure_context"}})
            for attempt in range(1, repair["max_attempts"] + 1):
                try:
                    proposal = propose(context, attempt)
                except ProposalUnavailable:
                    # Do not propagate provider response bodies or configuration secrets.
                    self._change(record, repair_status="unavailable",
                                 limitations=record["limitations"] + ["Repair provider is unavailable or returned an invalid proposal."])
                    break
                if proposal.base_sha256 != bundle.sha256 or proposal.allowed_path != spec.allowed_path:
                    self._change(record, repair_status="rejected",
                                 limitations=record["limitations"] + ["Proposal did not match the permitted application/source."])
                    break
                self._change(record, repair_status="proposed", proposal=proposal.provenance)
                if handoff is not None:
                    self._change(record, coordination={"provider": "band", "mode": handoff.mode,
                                                       "status": "waiting"})
                    try:
                        verification, coordination = handoff.verify(spec, bundle, proposal, verify, comparison)
                    except BandUnavailable:
                        self._change(record, repair_status="unavailable",
                                     coordination={"provider": "band", "mode": handoff.mode, "status": "unavailable"},
                                     limitations=record["limitations"] + ["BAND handoff is unavailable or invalid; no repair was accepted."])
                        break
                else:
                    verification = verify(spec, bundle, proposal)
                self._bound(verification, bundle, spec)
                candidate_hash = sha256(proposal.replacement.encode())
                if verification.bindings.get("candidate_sha256") != candidate_hash:
                    raise ValueError("Candidate evidence differs from proposed bytes")
                if (verification.repair_status == "verified"
                        and (not verification.complete or verification.execution_status != "completed")):
                    raise ValueError("Incomplete verification cannot be accepted")
                if verification.repair_status == "verified":
                    if (not verification.cases or any(c.get("status") != "passed" for c in verification.cases)
                            or verification.bindings.get("tests_sha256") != comparison.bindings.get("tests_sha256")
                            or verification.bindings.get("verifier_sha256") != comparison.bindings.get("verifier_sha256")):
                        raise ValueError("Verification changed acceptance inputs or includes a failed check")
                    for old, new in (("baseline", "repaired_baseline"), ("updated", "repaired_updated")):
                        if comparison.environments.get(old) != verification.environments.get(new):
                            raise ValueError("Verification used different environments from the comparison")
                self._add_artifacts(record, verification.artifacts, spec.artifact_dir, f"attempt-{attempt}-")
                entry = {"attempt": attempt, "repair_status": verification.repair_status,
                         "execution_status": verification.execution_status,
                         "complete": verification.complete, "candidate_sha256": candidate_hash,
                         "proposal": proposal.provenance}
                if handoff is not None:
                    entry["coordination"] = coordination
                self._change(record, repair_status=verification.repair_status,
                             bindings={**record["bindings"], "candidate_sha256": candidate_hash},
                             verification={"bindings": verification.bindings,
                                           "environments": verification.environments,
                                           "complete": verification.complete,
                                           "expected_case_ids": verification.expected_case_ids,
                                           "executed_case_ids": verification.executed_case_ids},
                             cases=comparison.cases + _public_artifact_refs(verification.cases, f"attempt-{attempt}-", set(verification.artifacts)),
                             attempts=record["attempts"] + [entry],
                             limitations=record["limitations"] + verification.limitations,
                             **({"coordination": coordination} if handoff is not None else {}))
                if verification.execution_status != "completed":
                    self._change(record, execution_status=verification.execution_status)
                    return
                if verification.repair_status == "verified":
                    break
                context["previous_verification"] = {k: v for k, v in verification.to_dict().items() if k != "artifacts"}
            self._change(record, execution_status="completed")
        except subprocess.TimeoutExpired:
            if record.get("coordination", {}).get("provider") == "band":
                self._change(record, coordination={**record["coordination"], "status": "unavailable"})
            self._change(record, execution_status="timed_out",
                         repair_status="unavailable" if repair["enabled"] else "not_requested",
                         limitations=record["limitations"] + ["Worker execution exceeded its deadline."])
        except Exception:
            if record.get("coordination", {}).get("provider") == "band":
                self._change(record, coordination={**record["coordination"], "status": "unavailable"})
            self._change(record, execution_status="setup_failed",
                         repair_status="unavailable" if repair["enabled"] else "not_requested",
                         limitations=record["limitations"] + ["Worker or evidence validation failed; this run cannot be accepted."])
        finally:
            with self._mutex:
                self._active = None

    def _prepared_comparison(self, spec: CaseSpec, bundle: SourceBundle) -> ComparisonEvidence:
        # Legacy runner is isolated in a child process; it may load .env but offline
        # execution cannot call model/source/memory providers. No credentials go to tests.
        code = ("import sys; from pathlib import Path; from src import upgrade_demo as d; "
                "d.STATE=Path(sys.argv[1]); d.ROOT=Path(sys.argv[2]); d.DEMO=d.ROOT/'demo'/'upgrade'; "
                "raise SystemExit(d.run_demo(offline=True,remember=False))")
        process = subprocess.run([sys.executable, "-c", code, str(spec.artifact_dir), str(spec.root)],
                                 cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=480)
        reports = list(spec.artifact_dir.glob("upgrade-demo/*/report.json"))
        if process.returncode not in {1, 2} or len(reports) != 1:
            raise ValueError("Prepared runner did not produce one fresh report")
        report = json.loads(reports[0].read_text())
        if report["app_sha256"] != bundle.sha256:
            raise ValueError("Prepared runner used different application bytes")
        for label in ("old", "new"):
            if report["requirements_sha256"][label] != sha256((spec.root / f"demo/upgrade/requirements-{label}.txt").read_bytes()):
                raise ValueError("Prepared requirements changed")
        jobs = report["results"]
        expected = {"existing_old", "existing_new", "probe_old", "probe_new", "fixed_old", "fixed_new"}
        if set(jobs) != expected:
            raise ValueError("Prepared runner omitted expected jobs")
        execution, finding = "completed", "inconclusive"
        comparison_valid = True
        cases = []
        for name, result in jobs.items():
            version = "1.10.18" if name.endswith("old") else "2.8.2"
            output = result["output_tail"]
            if result["status"] == "timeout":
                execution = "timed_out"
                if not name.startswith("fixed_"):
                    comparison_valid = False
            elif (result["status"] == "error" or re.findall(r"^SECONDLOOK_DEPENDENCY_VERSION=(\S+)$", output, re.M) != [version]
                  or not re.search(r"Ran [1-9]\d* tests? in", output)):
                execution = "setup_failed"
                if not name.startswith("fixed_"):
                    comparison_valid = False
            cases.append({"id": name, "stage": "prepared_comparison", "status": {"pass": "passed", "fail": "failed"}.get(result["status"], result["status"]),
                          "version": version, "exit_code": result["exit_code"], "duration_seconds": result["duration_seconds"]})
        comparison_ok = all(jobs[n]["status"] == "pass" for n in ("existing_old", "existing_new", "probe_old"))
        if comparison_valid and comparison_ok:
            failure = jobs["probe_new"]["output_tail"]
            if jobs["probe_new"]["status"] == "fail" and all(s in failure for s in ("ValidationError", "nickname", "Field required")):
                finding = "regression_reproduced"
            elif jobs["probe_new"]["status"] == "pass":
                finding = "no_difference_observed"
        # Public artifact does not expose host filesystem paths.
        report.pop("artifact_dir", None)
        public_report = spec.artifact_dir / "prepared-report.json"
        public_report.write_text(json.dumps(report, indent=2) + "\n")
        artifacts = {"prepared-report.json": str(public_report)}
        for path in reports[0].parent.iterdir():
            if path.name in expected or path.name in {n + ".txt" for n in expected} or path.name == "suggested-fix.patch":
                artifacts[path.name] = str(path)
        return ComparisonEvidence(execution_status=execution, finding_status=finding,
            bindings={"revision": bundle.revision, "source_sha256": bundle.sha256,
                      "contract_sha256": spec.contract_sha256,
                      "environment_manifest_sha256": sha256(canonical({"images": report["images"], "requirements": report["requirements_sha256"]})),
                      "tests_sha256": sha256(canonical({"probe": report["test_sha256"],
                          "existing": sha256((spec.root / "demo/upgrade/test_existing.py").read_bytes())}))},
            environments={"images": report["images"], "requirements_sha256": report["requirements_sha256"]},
            cases=cases, expected_case_ids=sorted(expected), executed_case_ids=sorted(jobs), artifacts=artifacts,
            limitations=["Fresh Docker execution of synthetic inputs with the existing offline runner.",
                         "Checked-in prepared fix only; repaired original suite and independent controls are not established by this adapter.",
                         "Use the native runner for the complete proofrun.v1 comparison and candidate verification."])

    def close(self):
        with self._mutex:
            self._closed = True
        self._pool.shutdown(wait=True)
        fcntl.flock(self._process_lock, fcntl.LOCK_UN)
        self._process_lock.close()
