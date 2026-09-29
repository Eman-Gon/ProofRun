"""Local HTTP checks with injected providers; no web/model traffic or Docker."""
import copy
import http.client
import json
import threading
from unittest.mock import Mock

import pytest

from src.proofrun.api import create_server as fixture_server
from src.proofrun.failure_context import fixture_context, release_context
from src.proofrun.failure_research import FailureResearchService
from src.proofrun.release_api import create_server as release_server

TOKEN = "test-failure-research-token-0123456789"
FIXTURE = {"run_id": "run-123", "job_key": "duplo-original", "execution_status": "completed",
           "finding_status": "regression_reproduced", "repair_status": "not_requested",
           "bindings": {"revision": "a" * 40, "source_sha256": "b" * 64, "contract_sha256": "c" * 64},
           "cases": [{"id": "nickname_omitted", "stage": "updated", "status": "failed"}]}
RELEASE = {"id": "release-" + "a" * 40, "status": "completed",
           "request": {"target_id": "api", "baseline_revision": "b" * 40, "candidate_revision": "c" * 40},
           "result": {"recommendation": "skip", "contract_hash": "d" * 64,
                      "findings": [{"id": "finding-one", "status": "confirmed", "title": "Response changed",
                                    "evidence": {"private_body": "never transmit"}}]}}


@pytest.fixture(params=["fixture", "release"])
def worker(request, tmp_path):
    kind = request.param
    run = copy.deepcopy(FIXTURE if kind == "fixture" else RELEASE)
    provider = Mock()
    provider.fetch.return_value = {"status": "no_sources", "summary": "No cited sources returned.",
                                   "sources": [], "suggested_fixes": [], "error": None, "provenance": {}}
    research = FailureResearchService(tmp_path / "research", client=provider)
    service = Mock()
    service.get_run.side_effect = lambda run_id: copy.deepcopy(run)
    def scoped(run_id, scope):
        if scope != "workspace-one":
            raise KeyError()
        return copy.deepcopy(run)
    service.get.side_effect = scoped
    server = (fixture_server if kind == "fixture" else release_server)(
        service, TOKEN, port=0, failure_research=research)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    path = f"/v1/{'runs/run-123' if kind == 'fixture' else 'release-runs/' + RELEASE['id']}/failure-research"
    payload = {"request_id": "test-search-1", "query": "pydantic 2.8.2 optional field required"}
    if kind == "release":
        payload["finding_id"] = "finding-one"
    def send(value=None, token=TOKEN, scope="workspace-one"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("POST", path, json.dumps(payload if value is None else value),
                               {"Content-Type": "application/json", "Authorization": "Bearer " + token,
                                "X-ProofRun-Scope": scope})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    yield kind, run, provider, service, send, payload
    server.shutdown(); server.server_close(); thread.join(timeout=3); research.close()


def test_authentication_precedes_research(worker):
    _, _, provider, service, send, _ = worker
    assert send(token="wrong")[0] == 401
    provider.fetch.assert_not_called()
    assert not service.mock_calls


def test_explicit_search_is_bound_cached_and_leaves_verdict_unchanged(worker):
    kind, run, provider, service, send, payload = worker
    before = copy.deepcopy(run)
    status, report = send()
    assert status == 200 and report["status"] == "no_sources"
    expected = fixture_context(run, "workspace-one") if kind == "fixture" else release_context(run, "finding-one", "workspace-one")
    assert report["context"] == expected
    assert send()[1] == report
    provider.fetch.assert_called_once_with(payload["query"])
    service.submit.assert_not_called()
    assert run == before
    assert "never transmit" not in str(provider.mock_calls)
    assert send({**payload, "query": "different query"})[0] == 409


def test_unknown_or_unconfirmed_finding_never_searches(worker):
    kind, run, provider, _, send, payload = worker
    if kind == "fixture":
        run["finding_status"] = "no_difference_observed"
    else:
        assert send({**payload, "finding_id": "not-in-run"})[0] == 404
        assert send(scope="other-workspace")[0] == 404
        run["result"]["findings"][0]["status"] = "inconclusive"
    assert send()[0] == 409
    provider.fetch.assert_not_called()


def test_payload_cannot_override_local_evidence(worker):
    _, _, provider, _, send, payload = worker
    assert send({**payload, "context": {"evidence_sha256": "e" * 64}})[0] == 400
    assert send({**payload, "query": ""})[0] == 400
    provider.fetch.assert_not_called()
