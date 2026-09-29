"""Evidence graph HTTP boundaries with stored-shaped records and fake Neo4j.

Only loopback test servers run. No real credentials, providers, tests, or Neo4j
connections are used.
"""
from contextlib import contextmanager
import copy
import hashlib
import http.client
import json
import os
import threading
from unittest.mock import Mock, patch

import pytest

from src.dashboard import Dashboard, make_server
from src.proofrun.api import create_server as fixture_server
from src.proofrun.evidence_graph import RunGraphService
from src.proofrun.neo4j_store import GraphUnavailable
from src.proofrun.release_api import create_server as release_server
from src.proofrun.release_service import ReleaseService
from src.proofrun.service import ServiceError
from test_dashboard import fixture_report
from test_evidence_graph import FakeEvidenceStore


TOKEN = "unit-test-evidence-graph-token-not-a-real-secret"
RELEASE_ID = "release-" + "a" * 40


@contextmanager
def serving(server):
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()

    def request(path, *, method="GET", token=TOKEN, scope=None, headers=None, payload=None):
        supplied = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            supplied["Authorization"] = "Bearer " + token
        if scope is not None:
            supplied["X-ProofRun-Scope"] = scope
        body = json.dumps(payload).encode() if payload is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body, supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if hasattr(server, "dashboard"):
            server.dashboard.close()


def fixture_record():
    return {"run_id": "run-evidence", "case_id": "customer-nickname-v1", "execution_status": "completed",
            "finding_status": "regression_reproduced", "repair_status": "not_requested",
            "bindings": {"contract_sha256": "c" * 64}, "attempts": [], "cases": []}


@pytest.mark.parametrize("token", [None, "incorrect"])
def test_fixture_graph_authenticates_before_record_or_graph_access(token):
    runs, graph = Mock(), Mock()
    with serving(fixture_server(runs, TOKEN, port=0, run_graph=graph)) as request:
        code, response = request("/v1/runs/run-evidence/graph", token=token)
    assert code == 401 and response["error"]["code"] == "unauthorized"
    assert not runs.mock_calls and not graph.mock_calls


def test_fixture_graph_uses_authoritative_record_and_ignores_browser_evidence():
    runs, graph = Mock(), Mock()
    record = fixture_record()
    runs.get_run.return_value = record
    graph.snapshot.return_value = {"status": "ready", "kind": "fixture"}
    with serving(fixture_server(runs, TOKEN, port=0, run_graph=graph)) as request:
        code, response = request("/v1/runs/run-evidence/graph", payload={"repair_status": "verified", "nodes": []})
    assert (code, response) == (200, graph.snapshot.return_value)
    runs.get_run.assert_called_once_with("run-evidence")
    graph.snapshot.assert_called_once_with("fixture", record)
    assert len(runs.mock_calls) == 1


def test_fixture_unknown_run_does_not_touch_graph():
    runs, graph = Mock(), Mock()
    runs.get_run.side_effect = ServiceError(404, "unknown_run", "Run was not found")
    with serving(fixture_server(runs, TOKEN, port=0, run_graph=graph)) as request:
        code, response = request("/v1/runs/run-unknown/graph")
    assert code == 404 and response["error"]["code"] == "unknown_run"
    graph.snapshot.assert_not_called()


@pytest.mark.parametrize("path", [
    "/v1/runs/run-evidence/graph?status=verified", "/v1/runs/run-evidence/graph/",
    "/v1/runs/../private/graph", "/v1/runs/run%2fevidence/graph",
])
def test_fixture_graph_rejects_ambiguous_paths(path):
    runs, graph = Mock(), Mock()
    with serving(fixture_server(runs, TOKEN, port=0, run_graph=graph)) as request:
        assert request(path)[0] == 404
    assert not runs.mock_calls and not graph.mock_calls


def test_fixture_graph_outage_returns_unavailable_without_affecting_run():
    runs, store = Mock(), FakeEvidenceStore()
    record = fixture_record()
    runs.get_run.return_value = record
    store.initialize.side_effect = GraphUnavailable("PRIVATE simulated driver error")
    with serving(fixture_server(runs, TOKEN, port=0, run_graph=RunGraphService(store))) as request:
        code, response = request("/v1/runs/run-evidence/graph")
    assert code == 200 and response["status"] == "unavailable"
    assert response["nodes"] == response["edges"] == []
    assert "PRIVATE" not in json.dumps(response)
    assert record == fixture_record()
    assert not store.writes


def test_fixture_graph_absent_configuration_has_explicit_unavailable_state():
    runs = Mock()
    runs.get_run.return_value = fixture_record()
    with serving(fixture_server(runs, TOKEN, port=0)) as request:
        code, response = request("/v1/runs/run-evidence/graph")
    assert code == 200 and response["status"] == "unavailable" and response["graph_id"] is None


@pytest.fixture
def release_service(tmp_path):
    service = ReleaseService({}, tmp_path / "release-records", factory=Mock())
    service.records[RELEASE_ID] = {"id": RELEASE_ID, "status": "completed", "scope": "workspace-a",
                                   "result": {"findings": [], "repairs": []}}
    yield service
    service.close()


@pytest.mark.parametrize("token", [None, "incorrect"])
def test_release_graph_authenticates_before_scoped_record_access(release_service, token):
    graph = Mock()
    with patch.object(release_service, "get", wraps=release_service.get) as get:
        with serving(release_server(release_service, TOKEN, port=0, run_graph=graph)) as request:
            assert request(f"/v1/release-runs/{RELEASE_ID}/graph", token=token, scope="workspace-a")[0] == 401
        get.assert_not_called()
    assert not graph.mock_calls


def test_release_graph_denies_another_workspace_before_projection(release_service):
    graph = Mock()
    graph.snapshot.return_value = {"status": "ready", "kind": "release"}
    with patch.object(release_service, "get", wraps=release_service.get) as get:
        with serving(release_server(release_service, TOKEN, port=0, run_graph=graph)) as request:
            assert request(f"/v1/release-runs/{RELEASE_ID}/graph", scope="workspace-b")[0] == 404
            graph.snapshot.assert_not_called()
            code, result = request(f"/v1/release-runs/{RELEASE_ID}/graph", scope="workspace-a")
        assert get.call_args_list[0].args == (RELEASE_ID, "workspace-b")
        assert get.call_args_list[1].args == (RELEASE_ID, "workspace-a")
    assert (code, result) == (200, graph.snapshot.return_value)
    graph.snapshot.assert_called_once_with("release", release_service.get(RELEASE_ID, "workspace-a"), scope="workspace-a")


def test_release_graph_unavailable_failure_is_safe_and_preserves_record(release_service):
    store = FakeEvidenceStore()
    store.write_snapshot = Mock(side_effect=GraphUnavailable("PRIVATE simulated driver details"))
    before = copy.deepcopy(release_service.records)
    with serving(release_server(release_service, TOKEN, port=0, run_graph=RunGraphService(store))) as request:
        code, graph = request(f"/v1/release-runs/{RELEASE_ID}/graph", scope="workspace-a")
    assert code == 200 and graph["status"] == "unavailable" and graph["nodes"] == []
    assert "PRIVATE" not in json.dumps(graph)
    assert release_service.records == before
    release_service.factory.assert_not_called()


def test_release_graph_invalid_scope_is_rejected_before_record_lookup(release_service):
    graph = Mock()
    with patch.object(release_service, "get", wraps=release_service.get) as get:
        with serving(release_server(release_service, TOKEN, port=0, run_graph=graph)) as request:
            assert request(f"/v1/release-runs/{RELEASE_ID}/graph", scope="../other")[0] == 400
        get.assert_not_called()
    assert not graph.mock_calls


def test_release_graph_unexpected_failure_returns_safe_error(release_service, capsys):
    graph = Mock()
    graph.snapshot.side_effect = RuntimeError("PRIVATE simulated credential-bearing failure")
    with serving(release_server(release_service, TOKEN, port=0, run_graph=graph)) as request:
        code, response = request(f"/v1/release-runs/{RELEASE_ID}/graph", scope="workspace-a")
    assert code == 503
    assert "PRIVATE" not in json.dumps(response)
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err


def write_dashboard_report(root):
    directory = root / ".commit-watch/upgrade-demo/stored-test"
    directory.mkdir(parents=True)
    report = fixture_report()
    report["results"]["existing_old"]["output_tail"] += "\nPRIVATE log content"
    path = directory / "report.json"
    path.write_text(json.dumps(report))
    (directory / "suggested-fix.patch").write_text("PRIVATE source replacement bytes")
    return path


@pytest.fixture
def dashboard_server(tmp_path):
    write_dashboard_report(tmp_path)
    server = make_server(tmp_path, port=0)
    store = FakeEvidenceStore()
    server.dashboard.graph_service = RunGraphService(store)
    with serving(server) as request:
        yield server, store, request


def test_dashboard_case_graph_is_loaded_from_saved_report_and_scrubs_logs(dashboard_server):
    server, store, request = dashboard_server
    code, result = request("/api/graph?caseId=pydantic")
    assert code == 200 and result["status"] == "ready" and result["kind"] == "dashboard"
    report = server.dashboard.report_path("pydantic")
    fingerprint = hashlib.sha256(report.read_bytes()).hexdigest()
    assert any(fingerprint in node.get("detail", "") for node in result["nodes"])
    assert "PRIVATE" not in json.dumps(result) and "PRIVATE" not in json.dumps(store.writes)
    repair = next(node for node in result["nodes"] if node["kind"] == "repair")
    assert repair["status"] == "prepared"
    assert not server.dashboard.jobs


def test_dashboard_without_report_shows_only_unverified_suggestion(dashboard_server):
    _, store, request = dashboard_server
    code, result = request("/api/graph?caseId=gpu-energy-pandas")
    assert code == 200 and result["status"] == "ready"
    assert store.writes
    assert not any(node["kind"] == "test" for node in result["nodes"])
    assert all(node.get("status") == "unverified" for node in result["nodes"] if node["kind"] in {"run", "finding", "repair"})
    assert any(node["label"] == "prepared-suggestion-input" for node in result["nodes"])


def test_dashboard_report_changed_during_projection_stays_pending(dashboard_server):
    server, store, request = dashboard_server
    original = server.dashboard.state
    def replacing_report():
        state = original()
        server.dashboard.report_path("pydantic").write_text(json.dumps({"results": {}, "status": "inconclusive"}))
        return state
    with patch.object(server.dashboard, "state", side_effect=replacing_report):
        code, graph = request("/api/graph?caseId=pydantic")
    assert code == 200 and graph["status"] == "pending" and graph["nodes"] == []
    assert not store.writes


@pytest.mark.parametrize("query", ["", "caseId=unknown", "caseId=pydantic&caseId=pydantic",
    "caseId=pydantic&status=verified", "scanId=known", "scanId=known&findingIndex=-1",
    "scanId=..%2Fprivate&findingIndex=0", "scanId=known&findingIndex=0&findingIndex=1",
    "caseId=pydantic&scanId=known&findingIndex=0"])
def test_dashboard_graph_rejects_ambiguous_or_invalid_queries(dashboard_server, query):
    _, store, request = dashboard_server
    assert request("/api/graph?" + query)[0] == 400
    assert not store.writes


@pytest.mark.parametrize("headers", [{"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"},
                                       {"Sec-Fetch-Site": "cross-site"}])
def test_dashboard_graph_loopback_and_origin_guards_precede_storage(dashboard_server, headers):
    _, store, request = dashboard_server
    assert request("/api/graph?caseId=pydantic", headers=headers)[0] == 403
    assert not store.writes


def test_dashboard_graph_only_selects_existing_completed_source_finding(dashboard_server):
    server, store, request = dashboard_server
    scan = {"id": "saved-scan", "status": "completed", "result": {"repository": "example/app", "commit": "a" * 40,
        "findings": [{"package": "pandas", "file": "app.py", "beforeCode": "PRIVATE original",
                      "afterCode": "PRIVATE suggested"}], "explanation": {"summary": "Static source inspection"}}}
    server.dashboard.repository_scans = [scan]
    assert request("/api/graph?scanId=missing&findingIndex=0")[0] == 404
    assert request("/api/graph?scanId=saved-scan&findingIndex=1")[0] == 404
    assert not store.writes
    code, result = request("/api/graph?scanId=saved-scan&findingIndex=0")
    assert code == 200 and result["status"] == "ready"
    assert next(n for n in result["nodes"] if n["kind"] == "repair")["status"] == "unverified"
    assert not any(n["kind"] in {"test", "verification"} for n in result["nodes"])
    assert "PRIVATE" not in json.dumps(result) and "PRIVATE" not in json.dumps(store.writes)
    scan["status"] = "running"
    assert request("/api/graph?scanId=saved-scan&findingIndex=0")[0] == 404


def test_dashboard_main_env_is_loaded_privately_and_cached(tmp_path, capsys):
    write_dashboard_report(tmp_path)
    settings = ("PROOFRUN_NEO4J_ENABLED=true\nNEO4J_URI=bolt://localhost:7687\n"
                "NEO4J_USERNAME=neo4j\nNEO4J_PASSWORD=PRIVATE-test-password\nNEO4J_DATABASE=neo4j\n")
    (tmp_path / ".env").write_text(settings)
    store = FakeEvidenceStore()
    with patch.dict(os.environ, {}, clear=True), patch("src.proofrun.evidence_graph.EvidenceGraphStore", return_value=store) as factory:
        dashboard = Dashboard(tmp_path)
        try:
            first = dashboard.evidence_graph("pydantic")
            assert first["status"] == "ready"
            assert dashboard.evidence_graph("pydantic")["graph_id"] == first["graph_id"]
            factory.assert_called_once()
            config = factory.call_args.args[0]
            assert config.enabled and config.password == "PRIVATE-test-password"
            assert "PRIVATE" not in json.dumps(first) and "PRIVATE" not in json.dumps(store.writes)
        finally:
            dashboard.close()
    store.close.assert_called_once()
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err


def test_dashboard_process_settings_override_main_env(tmp_path):
    write_dashboard_report(tmp_path)
    (tmp_path / ".env").write_text("PROOFRUN_NEO4J_ENABLED=true\nNEO4J_PASSWORD=PRIVATE-test-password\n")
    with patch.dict(os.environ, {"PROOFRUN_NEO4J_ENABLED": "false"}, clear=True), patch("src.proofrun.evidence_graph.EvidenceGraphStore") as factory:
        dashboard = Dashboard(tmp_path)
        try:
            result = dashboard.evidence_graph("pydantic")
            assert result["status"] == "unavailable" and result["nodes"] == []
            factory.assert_not_called()
            assert "PRIVATE" not in json.dumps(result)
        finally:
            dashboard.close()
