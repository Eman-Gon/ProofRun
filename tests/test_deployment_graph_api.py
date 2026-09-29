"""Graph HTTP boundaries with explicit mocks; no Neo4j, Docker, or providers."""
import http.client
import json
import threading
from unittest.mock import Mock

import pytest

from src.proofrun.api import create_server
from src.proofrun.service import ServiceError


TOKEN = "graph-transport-test-token-not-a-real-secret"
ROUTES = (
    ("POST", "/v1/deployments"),
    ("POST", "/v1/contract-changes"),
    ("GET", "/v1/contract-changes/change-one"),
    ("POST", "/v1/contract-changes/change-one/runs"),
)


@pytest.fixture
def worker():
    runs = Mock()
    graph = Mock()
    graph.register.return_value = {"schema_version": "proofrun.graph.v1", "deployment_id": "api"}
    report = {"schema_version": "proofrun.graph.v1", "provider": "neo4j", "change_id": "change-one",
              "deployments": [{"deployment_id": "api", "requires_reverification": True}]}
    graph.select.return_value = report
    graph.get.return_value = report
    run = {"run_id": "run-graph", "execution_status": "queued"}
    graph.submit.return_value = (run, True)
    server = create_server(runs, TOKEN, port=0, graph=graph)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()

    def request(method, path, payload=None, *, auth=TOKEN, body=None):
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = "Bearer " + auth
        if payload is not None:
            body = json.dumps(payload).encode()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    yield runs, graph, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("auth", [None, "incorrect"])
def test_graph_routes_authenticate_before_accessing_graph(worker, method, path, auth):
    runs, graph, request = worker
    status, result = request(method, path, {}, auth=auth)
    assert status == 401
    assert result["error"]["code"] == "unauthorized"
    assert not graph.mock_calls
    assert not runs.mock_calls


def test_graph_routes_dispatch_to_graph_without_bypassing_run_binding(worker):
    runs, graph, request = worker
    payload = {"deployment_id": "api"}
    assert request("POST", "/v1/deployments", payload) == (200, graph.register.return_value)
    graph.register.assert_called_once_with(payload)
    payload = {"change_id": "change-one"}
    assert request("POST", "/v1/contract-changes", payload) == (200, graph.select.return_value)
    graph.select.assert_called_once_with(payload)
    assert request("GET", "/v1/contract-changes/change-one") == (200, graph.get.return_value)
    graph.get.assert_called_once_with("change-one")
    payload = {"deployment_id": "api", "job_key": "retry-one"}
    assert request("POST", "/v1/contract-changes/change-one/runs", payload) == (202, graph.submit.return_value[0])
    graph.submit.assert_called_once_with("change-one", payload)
    graph.submit.return_value = (graph.submit.return_value[0], False)
    assert request("POST", "/v1/contract-changes/change-one/runs", payload)[0] == 200
    assert not runs.mock_calls


@pytest.mark.parametrize("method,path", [ROUTES[0], ROUTES[1], ROUTES[3]])
def test_ambiguous_json_is_rejected_before_graph_access(worker, method, path):
    _, graph, request = worker
    status, result = request(method, path, body=b'{"job_key":"one","job_key":"two"}')
    assert status == 400
    assert result["error"]["code"] == "invalid_json"
    assert not graph.mock_calls


@pytest.mark.parametrize("path", [
    "/v1/contract-changes/change-one/", "/v1/contract-changes/change-one?token=secret",
    "/v1/contract-changes/../secret", "/v1/contract-changes/%2e%2e",
    "/v1/contract-changes/change%2fone", "/v1/contract-changes/change-one/runs/",
])
def test_graph_paths_do_not_decode_or_accept_query_strings(worker, path):
    _, graph, request = worker
    assert request("GET", path)[0] == 404
    assert not graph.mock_calls


def test_graph_errors_preserve_safe_public_codes_and_hide_internal_details(worker):
    _, graph, request = worker
    graph.get.side_effect = ServiceError(409, "graph_conflict", "Selection no longer matches.")
    assert request("GET", "/v1/contract-changes/change-one") == (
        409, {"schema_version": "proofrun.v1", "error": {
            "code": "graph_conflict", "message": "Selection no longer matches."}})
    graph.get.side_effect = RuntimeError("neo4j://private-host secret-password")
    status, result = request("GET", "/v1/contract-changes/change-one")
    assert status == 500
    assert "private-host" not in json.dumps(result)
    assert "secret-password" not in json.dumps(result)


def test_unconfigured_graph_returns_explicit_unavailable_response():
    runs = Mock()
    server = create_server(runs, TOKEN, port=0)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        for method, path in ROUTES:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request(method, path, "{}", {
                    "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
                response = connection.getresponse()
                assert response.status == 503
                assert json.loads(response.read())["error"]["code"] == "graph_unavailable"
            finally:
                connection.close()
        assert not runs.mock_calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
