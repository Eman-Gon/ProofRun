"""Release API/service tests with a local HTTP server and injected investigation.

No model calls, repository execution, Docker, or external staging is involved.
"""
import copy
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from src.proofrun.release_api import create_server
from src.proofrun.release_contracts import canonical, load_targets
from src.proofrun.release_service import ReleaseService


TOKEN = "test-release-worker-token-" + "x" * 40
BASELINE = "a" * 40
CANDIDATE = "b" * 40
IMAGE = "sha256:" + "c" * 64


class StubFactory:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self, target, request, directory, emit):
        owner = self

        class StubInvestigation:
            def run(self):
                owner.calls.append({"target": copy.deepcopy(target), "request": copy.deepcopy(request)})
                owner.started.set()
                if not owner.gate.wait(5):
                    raise RuntimeError("Unit test did not release its investigation gate")
                emit({"stage": "testing", "status": "completed", "source": "private source", "model": "private model output"})
                directory.mkdir(parents=True, exist_ok=True)
                data = canonical({"test": "stub evidence, not an executed product check"})
                (directory / "report.json").write_bytes(data)
                return {"recommendation": "inconclusive", "summary": "Injected test result",
                        "artifacts": [{"id": "report.json", "bytes": len(data),
                                       "sha256": hashlib.sha256(data).hexdigest()}]}

        return StubInvestigation()


@pytest.fixture
def registry(tmp_path):
    raw = {"id": "customer-app", "name": "Customer application", "repository": str(tmp_path / "private-repository"),
           "images": {BASELINE: IMAGE, CANDIDATE: IMAGE},
           "requirements": [{"id": "read-compatibility", "description": "Preserve the documented read response",
                              "kind": "preserve_response", "path_prefix": "/customer", "methods": ["GET"]}],
           "runtime": {"command": ["python", "app.py"], "test_command": ["python", "-m", "unittest"],
                       "port": 8000, "health_path": "/health", "collector_image": IMAGE},
           "repair_paths": ["app.py"], "exclude_paths": [], "test_paths": ["tests"],
           "test_success_pattern": r"Ran [1-9][0-9]* tests?", "workspaces": ["workspace-a"],
           "staging": {"base_url": "https://private-staging.invalid", "revision_path": "/revision",
                       "headers_env": {"Authorization": "PRIVATE_STAGING_TOKEN"}}}
    shared = copy.deepcopy(raw)
    shared.update(id="shared-app", name="Shared application")
    shared.pop("workspaces")
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"targets": [raw, shared]}))
    return load_targets(path)


@pytest.fixture
def worker(registry, tmp_path):
    factory = StubFactory()
    service = ReleaseService(registry, tmp_path / "records", factory=factory)
    server = create_server(service, TOKEN, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, payload=None, *, scope="workspace-a", token=TOKEN, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        body = None if payload is None else json.dumps(payload).encode()
        request_headers = {"X-ProofRun-Scope": scope, "Content-Type": "application/json", **(headers or {})}
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            if response.getheader("Content-Type") == "application/json":
                data = json.loads(data)
            return response.status, data
        finally:
            connection.close()

    yield service, factory, request
    factory.gate.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    service.close()


@pytest.fixture
def submission():
    return {"target_id": "customer-app", "baseline_revision": BASELINE, "candidate_revision": CANDIDATE,
            "budget_seconds": 180, "repair": False, "event_id": "deployment-001"}


def complete(service, run_id):
    service.executor.submit(lambda: None).result(timeout=3)
    result = service.get(run_id, "workspace-a")
    assert result["status"] == "completed"
    return result


@pytest.mark.parametrize("method,path", [("GET", "/v1/release-targets"),
                                       ("POST", "/v1/release-events"),
                                       ("GET", "/v1/release-runs/release-" + "a" * 40)])
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_every_endpoint_requires_authentication(worker, submission, method, path, token):
    service, factory, request = worker
    status, body = request(method, path, submission if method == "POST" else None, token=token)
    assert status == 401 and "error" in body
    assert not service.records and not factory.calls
    assert TOKEN not in json.dumps(body)


def test_target_listing_redacts_server_configuration_and_limits_workspace(worker):
    _, _, request = worker
    status, body = request("GET", "/v1/release-targets")
    assert status == 200
    assert {t["id"] for t in body["targets"]} == {"customer-app", "shared-app"}
    text = json.dumps(body)
    for secret in ("private-repository", "private-staging.invalid", "PRIVATE_STAGING_TOKEN", "test_command", "repair_paths"):
        assert secret not in text
    status, other = request("GET", "/v1/release-targets", scope="workspace-b")
    assert status == 200 and [t["id"] for t in other["targets"]] == ["shared-app"]


def test_cross_workspace_submission_read_and_artifact_are_denied(worker, submission):
    service, _, request = worker
    assert request("POST", "/v1/release-runs", submission, scope="workspace-b")[0] == 400
    status, run = request("POST", "/v1/release-runs", submission)
    assert status == 202
    stored = complete(service, run["id"])
    assert "scope" not in stored
    path = "/v1/release-runs/" + run["id"]
    assert request("GET", path, scope="workspace-b")[0] == 404
    assert request("GET", path + "/artifacts/report.json", scope="workspace-b")[0] == 404
    assert request("GET", path + "/artifacts/report.json")[0] == 200
    assert request("GET", path + "/artifacts/missing.json")[0] == 404
    assert request("GET", path + "/artifacts/../run.json")[0] == 404
    event = stored["events"][0]
    assert event["stage"] == "testing" and "source" not in event and "model" not in event


def test_authenticated_event_deduplicates_same_payload(worker, submission):
    service, factory, request = worker
    status, first = request("POST", "/v1/release-events", submission)
    assert status == 202
    complete(service, first["id"])
    status, second = request("POST", "/v1/release-events", submission)
    assert status == 202 and first["id"] == second["id"]
    assert len(factory.calls) == 1 and len(service.records) == 1


def test_same_event_is_separate_for_different_authorized_workspaces(worker, submission):
    service, factory, request = worker
    shared = submission | {"target_id": "shared-app"}
    _, first = request("POST", "/v1/release-events", shared)
    _, second = request("POST", "/v1/release-events", shared, scope="workspace-b")
    assert first["id"] != second["id"]
    service.executor.submit(lambda: None).result(timeout=3)
    assert len(factory.calls) == 2


def test_event_rejects_changed_payload_or_target_contract(worker, submission):
    service, _, request = worker
    _, first = request("POST", "/v1/release-events", submission)
    complete(service, first["id"])
    assert request("POST", "/v1/release-events", submission | {"benefit": "changed intent"})[0] == 409
    service.targets["customer-app"]["contract_hash"] = "f" * 64
    assert request("POST", "/v1/release-events", submission)[0] == 409


def test_queued_run_keeps_the_configuration_bound_at_submission(worker, submission):
    service, factory, request = worker
    factory.gate.clear()
    try:
        request("POST", "/v1/release-events", submission)
        assert factory.started.wait(1)
        status, queued = request("POST", "/v1/release-events", submission | {"event_id": "queued-config"})
        assert status == 202
        service.targets["customer-app"]["runtime"]["command"] = ["changed-after-submission"]
        factory.gate.set()
        complete(service, queued["id"])
        assert factory.calls[1]["target"]["runtime"]["command"] == ["python", "app.py"]
    finally:
        factory.gate.set()


@pytest.mark.parametrize("path", ["/v1/release-events", "/v1/release-events?source=deployment"])
def test_event_id_is_required_with_or_without_query_string(worker, submission, path):
    service, factory, request = worker
    submission.pop("event_id")
    assert request("POST", path, submission)[0] == 400
    assert not service.records and not factory.calls


@pytest.mark.parametrize("change", [
    {"target_id": "unknown"}, {"candidate_revision": "HEAD"}, {"candidate_revision": "f" * 40},
    {"candidate_revision": BASELINE}, {"baseline_revision": "A" * 40},
    {"budget_seconds": 179}, {"budget_seconds": 601}, {"budget_seconds": True},
    {"repair": "yes"}, {"event_id": "../../outside"}, {"arbitrary_command": "do not execute"},
])
def test_invalid_requests_cannot_enqueue_work(worker, submission, change):
    service, factory, request = worker
    assert request("POST", "/v1/release-runs", submission | change)[0] == 400
    assert not service.records and not factory.calls


def test_bad_scope_rejected_before_target_access(worker):
    _, _, request = worker
    assert request("GET", "/v1/release-targets", scope="../another-workspace")[0] == 400


def test_queue_has_bounded_capacity_and_preserves_idempotent_retries(worker, submission):
    service, factory, request = worker
    factory.gate.clear()
    try:
        _, first = request("POST", "/v1/release-events", submission)
        assert factory.started.wait(1)
        for index in range(1, 16):
            assert request("POST", "/v1/release-events", submission | {"event_id": f"event-{index}"})[0] == 202
        status, body = request("POST", "/v1/release-events", submission | {"event_id": "overflow"})
        assert status == 409 and "full" in body["error"]
        status, retry = request("POST", "/v1/release-events", submission)
        assert status == 202 and retry["id"] == first["id"]
        assert len(service.records) == 16
    finally:
        factory.gate.set()


def test_saved_artifact_tampering_is_rejected(worker, submission):
    service, _, request = worker
    _, first = request("POST", "/v1/release-runs", submission)
    complete(service, first["id"])
    path = service.directory / first["id"] / "evidence" / "report.json"
    data = path.read_bytes()
    path.write_bytes(b"x" + data[1:])  # Same length; checking size alone would miss this.
    status, body = request("GET", f"/v1/release-runs/{first['id']}/artifacts/report.json")
    assert status == 409 and "hash" in body["error"]


@pytest.mark.parametrize("interrupted_status", ["queued", "running"])
def test_restart_marks_persisted_inflight_run_failed_and_does_not_reexecute(registry, tmp_path, submission, interrupted_status):
    directory = tmp_path / "restart-records"
    factory = StubFactory()
    service = ReleaseService(registry, directory, factory=factory)
    try:
        record = service.submit(submission, "workspace-a")
        complete(service, record["id"])
    finally:
        service.close()
    file = directory / record["id"] / "run.json"
    saved = json.loads(file.read_text())
    saved.update(status=interrupted_status, result=None)
    file.write_bytes(canonical(saved))
    restarted_factory = StubFactory()
    restarted = ReleaseService(registry, directory, factory=restarted_factory)
    try:
        recovered = restarted.get(record["id"], "workspace-a")
        assert recovered["status"] == "failed" and "restarted" in recovered["error"]
        retry = restarted.submit(submission, "workspace-a")
        assert retry["id"] == record["id"] and retry["status"] == "failed"
        assert restarted_factory.calls == []
        assert json.loads(file.read_text())["status"] == "failed"
    finally:
        restarted.close()


def test_second_worker_cannot_take_same_directory(registry, tmp_path):
    directory = tmp_path / "locked-records"
    owner = ReleaseService(registry, directory, factory=StubFactory())
    try:
        with pytest.raises(RuntimeError, match="Another release worker"):
            ReleaseService(registry, directory, factory=StubFactory())
    finally:
        owner.close()
