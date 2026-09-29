"""Persistent single-worker release queue, with scope-bound idempotency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from .release_contracts import SCHEMA, canonical, digest, public_target, validate_request
from .release_investigation import Investigation


class Conflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class ReleaseService:
    def __init__(self, targets, directory: Path, factory=Investigation):
        self.targets, self.directory, self.factory = copy.deepcopy(targets), Path(directory), factory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lease = (self.directory / ".worker.lock").open("a")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lease.close()
            raise RuntimeError("Another release worker owns this artifact directory.") from None
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="release-investigation")
        self.records = {}
        for file in self.directory.glob("*/run.json"):
            record = json.loads(file.read_text())
            if record["status"] in ("queued", "running"):
                record.update(status="failed", error="The worker restarted before this run completed.", updated_at=now())
                self.save(record)
            self.records[record["id"]] = record

    def close(self):
        self.executor.shutdown(wait=True)
        self._lease.close()

    def save(self, record):
        directory = self.directory / record["id"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = directory / "run.json.tmp"
        file.write_bytes(canonical(record))
        file.replace(directory / "run.json")

    def permitted_targets(self, scope):
        return {k: t for k, t in self.targets.items() if not t.get("workspaces") or scope in t["workspaces"]}

    def list_targets(self, scope):
        return {"schema_version": SCHEMA, "targets": [public_target(t) for t in self.permitted_targets(scope).values()]}

    @staticmethod
    def public(record):
        return copy.deepcopy({k: v for k, v in record.items() if k != "scope"})

    def submit(self, raw, scope="operator"):
        request = validate_request(raw, self.permitted_targets(scope))
        target = self.targets[request["target_id"]]
        event_id = request.get("event_id", uuid4().hex)
        run_id = "release-" + digest([scope, event_id])[:40]
        binding = digest([request, target["contract_hash"]])
        with self.lock:
            if run_id in self.records:
                prior = self.records[run_id]
                if prior["binding"] != binding:
                    raise Conflict("This event id already identifies a different request or target contract.")
                return self.public(prior)
            if sum(r["status"] in ("queued", "running") for r in self.records.values()) >= 16:
                raise Conflict("The release queue is full; try again after a run completes.")
            record = {"schema_version": SCHEMA, "id": run_id, "scope": scope, "binding": binding,
                      "status": "queued", "request": request, "created_at": now(), "updated_at": now(),
                      "events": [], "result": None}
            self.records[run_id] = record
            self.save(record)
            self.executor.submit(self.execute, run_id, copy.deepcopy(target))
            return self.public(record)

    def execute(self, run_id, target):
        with self.lock:
            record = self.records[run_id]
            record.update(status="running", updated_at=now())
            self.save(record)
        def emit(event):
            # Full trace remains an artifact. Polling returns only compact progress, not source/model data.
            safe = {k: v for k, v in event.items() if k in {"stage", "type", "step", "status", "probe_id", "repair_id", "revision"}}
            with self.lock:
                record["events"] = (record["events"] + [{"at": now(), **safe}])[-100:]
                record["updated_at"] = now()
                self.save(record)
        try:
            investigation = self.factory(target, record["request"],
                                         self.directory / run_id / "evidence", emit=emit)
            result = investigation.run()
            with self.lock:
                record.update(status="completed", result=result, updated_at=now())
                self.save(record)
        except Exception:
            with self.lock:
                record.update(status="failed", error="Investigation stopped unexpectedly; no passing result was issued.", updated_at=now())
                self.save(record)

    def get(self, run_id, scope="operator"):
        with self.lock:
            record = self.records.get(run_id)
            if not record or record["scope"] != scope:
                raise KeyError("Unknown release run.")
            return self.public(record)

    def artifact(self, run_id, artifact_id, scope="operator"):
        record = self.get(run_id, scope)
        result = record.get("result") or {}
        artifact = next((a for a in result.get("artifacts", []) if a["id"] == artifact_id), None)
        if not artifact or not re.fullmatch(r"[a-z0-9-]+\.(json|diff)", artifact_id):
            raise KeyError("Unknown evidence artifact.")
        data = (self.directory / run_id / "evidence" / artifact_id).read_bytes()
        if len(data) != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise Conflict("The saved artifact no longer matches its recorded evidence hash.")
        return data
