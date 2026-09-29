"""Neo4j selects explicit deployments; the registered worker measures behavior.

Selections are immutable, file-backed requests. Only runs submitted through this
service can satisfy a selection, and every result retains its original verdict.
"""
from __future__ import annotations

from collections import Counter
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from .service import ServiceError, TERMINAL, canonical, now, sha256
from .neo4j_store import (GraphConflict, GraphUnavailable, validate_deployment,
                          validate_id)

SCHEMA_VERSION = "proofrun.graph.v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _invalid(message):
    raise ServiceError(400, "invalid_graph_request", message)


def _identifier(value, name):
    try:
        return validate_id(value, name)
    except ValueError:
        _invalid(f"{name} must be a safe identifier of 1 to 128 characters.")


def _fields(payload, names):
    if not isinstance(payload, dict) or set(payload) != set(names):
        _invalid("Supply exactly: " + ", ".join(names) + ".")


def _binding(deployment, change):
    return {"revision": deployment["revision"],
            "source_sha256": deployment["source_sha256"],
            "contract_sha256": change["contract_sha256"]}


class DeploymentGraphService:
    def __init__(self, runs, directory: Path, store=None):
        self.runs, self.store = runs, store
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._mutex = threading.RLock()
        self._initialized = False
        self._closed = False
        self._lock = (self.directory / "graph.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise ValueError("Another worker owns this graph selection directory") from None
        self._changes = {}
        try:
            for path in self.directory.glob("change-*.json"):
                record = json.loads(path.read_text())
                identifier = _identifier(record["change_id"], "change_id")
                if path.name != self._path(identifier).name:
                    raise ValueError("Invalid persisted graph selection")
                self._changes[identifier] = record
        except Exception:
            self._lock.close()
            raise

    def _path(self, change_id):
        return self.directory / ("change-" + sha256(change_id.encode()) + ".json")

    def _save(self, record):
        path = self._path(record["change_id"])
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            temporary.chmod(0o600)
            json.dump(record, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _public(record):
        def scrub(value):
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items() if not k.startswith("_")}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return value
        return copy.deepcopy(scrub(record))

    def _graph(self, method, *args):
        if self._closed or self.store is None:
            raise ServiceError(503, "graph_unavailable", "Neo4j is not configured or is unavailable.")
        try:
            if not self._initialized:
                self.store.initialize()
                self._initialized = True
            return getattr(self.store, method)(*args)
        except GraphConflict:
            raise ServiceError(409, "graph_conflict", "Neo4j evidence identity conflicts with existing data.") from None
        except GraphUnavailable:
            raise ServiceError(503, "graph_unavailable", "Neo4j is unavailable; no substitute graph was used.") from None

    def register(self, payload):
        try:
            deployment = validate_deployment(payload)
        except ValueError:
            _invalid("Supply a deployment ID, full revision/source hashes and 1 to 20 unique contract/case bindings.")
        with self._mutex:
            result = self._graph("put_deployment", deployment)
            return {"schema_version": SCHEMA_VERSION, "provider": "neo4j", "deployment": result}

    def select(self, payload):
        _fields(payload, ("change_id", "contract_id", "previous_sha256", "contract_sha256"))
        for key in ("change_id", "contract_id"):
            _identifier(payload[key], key)
        for key in ("previous_sha256", "contract_sha256"):
            if not isinstance(payload[key], str) or not _HASH.fullmatch(payload[key]):
                _invalid(key + " must be a lowercase SHA-256 digest.")
        if payload["previous_sha256"] == payload["contract_sha256"]:
            _invalid("A contract change requires different previous and new hashes.")
        with self._mutex:
            fingerprint = sha256(canonical(payload))
            existing = self._changes.get(payload["change_id"])
            if existing is not None:
                if existing["_input_sha256"] != fingerprint:
                    raise ServiceError(409, "change_id_conflict", "change_id already binds a different contract change.")
                return self.get(payload["change_id"])
            deployments = self._graph("select_deployments", payload["contract_id"], payload["previous_sha256"])
            selections = []
            seen = set()
            for raw in deployments:
                try:
                    deployment = validate_deployment(raw)
                    matches = [c for c in deployment["contracts"] if c["contract_id"] == payload["contract_id"]
                               and c["contract_sha256"] == payload["previous_sha256"]]
                    if len(matches) != 1 or deployment["deployment_id"] in seen:
                        raise ValueError()
                except ValueError:
                    raise ServiceError(503, "graph_unavailable", "Neo4j returned invalid deployment bindings.") from None
                seen.add(deployment["deployment_id"])
                case_id = matches[0]["case_id"]
                selections.append({"deployment_id": deployment["deployment_id"],
                    "revision": deployment["revision"], "source_sha256": deployment["source_sha256"],
                    "case_id": case_id, "status": "requires_reverification", "requires_reverification": True,
                    "explanation": [
                        {"deployment_id": deployment["deployment_id"], "relationship": "DEPENDS_ON",
                         "contract_id": payload["contract_id"], "contract_sha256": payload["previous_sha256"]},
                        {"contract_id": payload["contract_id"], "relationship": "CHANGED_TO",
                         "contract_sha256": payload["contract_sha256"], "case_id": case_id}],
                    "_deployment": deployment, "_nonce": uuid4().hex})
            record = {"schema_version": SCHEMA_VERSION, "provider": "neo4j", **payload,
                "created_at": now(), "deployments": sorted(selections, key=lambda d: d["deployment_id"]),
                "limitations": ["Deployment relationships are explicitly supplied by the operator.",
                    "Graph selection is not test evidence or deployment approval.",
                    "Only the registered synthetic fixture can execute; customer deployments are not contacted.",
                    "Selections describe a snapshot; use a new change_id after changing deployment registrations."],
                "_input_sha256": fingerprint}
            self._save(record)
            self._changes[payload["change_id"]] = record
            return self._public(record)

    def _record(self, change_id):
        _identifier(change_id, "change_id")
        if change_id not in self._changes:
            raise ServiceError(404, "unknown_change", "Contract change was not found.")
        return self._changes[change_id]

    def _current_submission(self, record, selection):
        current = self._graph("get_deployment", selection["deployment_id"])
        if current != selection["_deployment"]:
            raise ServiceError(409, "selection_stale", "Deployment bindings changed; create a new contract-change selection.")
        submission = self.runs.get_case(selection["case_id"])["submission"]
        if (submission["source"]["revision"] != selection["revision"]
                or submission["source"]["sha256"] != selection["source_sha256"]
                or submission["contract"] != {"id": record["contract_id"], "sha256": record["contract_sha256"]}):
            raise ServiceError(409, "selection_stale", "Selected source and new contract must match the registered case before execution.")
        return copy.deepcopy(submission)

    def _fixture_binding(self):
        # The core submission schema binds app/contract/revision. Selection
        # retries additionally bind trusted tests, dependency pins, Dockerfile,
        # verifier and worker implementation, including uncommitted changes.
        from .runner import _snapshot
        try:
            return sha256(canonical({"fixture": _snapshot(self.runs.root),
                                     "worker": self.runs.worker_code_hashes}))
        except OSError:
            raise ServiceError(503, "case_unavailable", "Trusted verification inputs are unavailable.") from None

    @staticmethod
    def _matches(record, selection, run):
        return (run.get("job_key") == selection.get("_job_key") and run.get("case_id") == selection["case_id"]
                and all(run.get("bindings", {}).get(k) == v for k, v in _binding(selection, record).items()))

    def _persist_run(self, record, selection, run):
        contract = {"contract_id": record["contract_id"], "contract_sha256": record["contract_sha256"],
                    "case_id": selection["case_id"]}
        self._graph("record_run", selection["_deployment"], contract, run)

    def submit(self, change_id, payload):
        _fields(payload, ("deployment_id", "job_key"))
        for key in ("deployment_id", "job_key"):
            _identifier(payload[key], key)
        with self._mutex:
            record = self._record(change_id)
            selection = next((s for s in record["deployments"] if s["deployment_id"] == payload["deployment_id"]), None)
            if selection is None:
                raise ServiceError(404, "deployment_not_selected", "This deployment was not selected by this contract change.")
            submission = self._current_submission(record, selection)
            fixture = self._fixture_binding()
            # Namespacing forbids adopting pre-existing /v1/runs evidence via a
            # caller-chosen key. Persist the submission claim before dispatch so
            # a crash between core submit and saving its ID is safely retryable.
            key = "graph-" + sha256(canonical({"change": record["_input_sha256"], "nonce": selection["_nonce"],
                "fixture": fixture,
                "deployment": selection["_deployment"], "job_key": payload["job_key"]}))
            if selection.get("_job_key") != key and selection.get("run_id"):
                previous = self.runs.get_run(selection["run_id"])
                if previous["execution_status"] not in TERMINAL:
                    raise ServiceError(409, "selection_busy", "This selected deployment already has an active run.")
                if self._matches(record, selection, previous):
                    # A caller may poll /v1/runs directly, without ever reading
                    # the selection. Preserve that run before replacing its ID.
                    self._persist_run(record, selection, previous)
            submission["job_key"] = key
            submission["repair"] = {"enabled": False, "max_attempts": 1}
            selection["_pending_job_key"] = key
            self._save(record)
            run, created = self.runs.submit(submission)
            selection["_job_key"] = key
            selection["_fixture_sha256"] = fixture
            selection.pop("_pending_job_key", None)
            selection.update(run_id=run["run_id"], status=run["execution_status"], requires_reverification=True)
            selection.pop("result", None)
            self._save(record)
            return run, created

    @staticmethod
    def _measured(run):
        expected, executed = run.get("expected_case_ids"), run.get("executed_case_ids")
        bindings = run.get("bindings", {})
        return (run.get("execution_status") == "completed"
            and run.get("finding_status") in {"regression_reproduced", "no_difference_observed"}
            and run.get("execution", {}).get("runner") == "native"
            and isinstance(expected, list) and bool(expected) and isinstance(executed, list)
            and all(isinstance(x, str) for x in expected + executed)
            and len(set(expected)) == len(expected) and Counter(expected) == Counter(executed)
            and all(isinstance(bindings.get(k), str) and _HASH.fullmatch(bindings[k]) for k in
                    ("tests_sha256", "verifier_sha256", "environment_manifest_sha256")))

    def get(self, change_id):
        with self._mutex:
            record = self._record(change_id)
            for selection in record["deployments"]:
                # Even a previously measured selection stops being current when
                # an operator changes its deployment or the registered fixture.
                run_id = selection.get("run_id")
                run = self.runs.get_run(run_id) if run_id else None
                if run and self._matches(record, selection, run) and run["execution_status"] in TERMINAL:
                    # A stale selection still has historical execution evidence.
                    # Record it without letting it satisfy the current inputs.
                    self._persist_run(record, selection, run)
                try:
                    if run_id:
                        self._current_submission(record, selection)
                        if selection.get("_fixture_sha256") != self._fixture_binding():
                            raise ServiceError(409, "selection_stale", "Trusted verification inputs changed.")
                    elif self._graph("get_deployment", selection["deployment_id"]) != selection["_deployment"]:
                        raise ServiceError(409, "selection_stale", "Deployment bindings changed.")
                except ServiceError as exc:
                    if exc.status not in {400, 404, 409}:
                        raise
                    selection.update(status="superseded", requires_reverification=True)
                    continue
                if not run_id:
                    selection.update(status="requires_reverification", requires_reverification=True)
                    continue
                if not self._matches(record, selection, run):
                    selection.update(status="superseded", requires_reverification=True)
                    continue
                if run["execution_status"] not in TERMINAL:
                    selection.update(status=run["execution_status"], requires_reverification=True)
                    continue
                measured = self._measured(run)
                selection.update(status="measured" if measured else "inconclusive", requires_reverification=not measured,
                    result={"run_id": run_id, "execution_status": run["execution_status"],
                            "finding_status": run["finding_status"], "repair_status": run["repair_status"],
                            "bindings": run["bindings"], "execution": run.get("execution", {})})
            self._save(record)
            return self._public(record)

    def close(self):
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            try:
                if self.store is not None:
                    self.store.close()
            finally:
                self._lock.close()


def configured_graph(runs, directory):
    from .neo4j_store import GraphConfig, GraphConfigurationError, Neo4jStore
    store = None
    try:
        config = GraphConfig.from_env()
        if config.enabled:
            store = Neo4jStore(config)
    except (GraphConfigurationError, GraphUnavailable):
        # Optional Neo4j availability cannot disable independent core runs.
        pass
    return DeploymentGraphService(runs, directory, store)
