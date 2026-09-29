"""Graph orchestration tests with an explicit fake graph and synthetic runner.

The fixture uses real Git/source/contract bindings and the real RunService, but
does not claim these tests connect to Neo4j or execute Docker measurements.
"""
import copy
from pathlib import Path
import shutil
import subprocess
import threading
from unittest.mock import Mock, patch

import pytest

import test_proofrun_service as run_fixtures
from src.proofrun.deployment_graph import DeploymentGraphService
from src.proofrun.neo4j_store import GraphUnavailable
from src.proofrun.service import CASE_ID, RunService, ServiceError, canonical, sha256


class FakeGraph:
    """In-memory protocol implementation used only by these unit tests."""

    def __init__(self):
        self.deployments = {}
        self.results = {}
        self.selections = []
        self.initializations = 0
        self.closed = False

    def initialize(self):
        self.initializations += 1

    def put_deployment(self, deployment):
        self.deployments[deployment["deployment_id"]] = copy.deepcopy(deployment)
        return copy.deepcopy(deployment)

    def get_deployment(self, deployment_id):
        return copy.deepcopy(self.deployments.get(deployment_id))

    def select_deployments(self, contract_id, previous_sha256):
        self.selections.append((contract_id, previous_sha256))
        return [copy.deepcopy(value) for value in self.deployments.values()
                if any(c["contract_id"] == contract_id and c["contract_sha256"] == previous_sha256
                       for c in value["contracts"])]

    def record_run(self, deployment, contract, run):
        self.results[run["run_id"]] = copy.deepcopy((deployment, contract, run))

    def close(self):
        self.closed = True


@pytest.fixture
def environment(tmp_path):
    root = tmp_path / "repo"
    repository = Path(__file__).resolve().parents[1]
    shutil.copytree(repository / "demo/upgrade", root / "demo/upgrade",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "sandbox").mkdir()
    shutil.copyfile(repository / "sandbox/upgrade.Dockerfile", root / "sandbox/upgrade.Dockerfile")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Graph test", "-c", "user.email=test@example.invalid",
                  "commit", "-q", "-m", "Registered synthetic fixture")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    runner = Mock(side_effect=run_fixtures.comparison)
    runs = RunService(root, tmp_path / "runs", compare=runner)
    store = FakeGraph()
    graph = DeploymentGraphService(runs, tmp_path / "graph", store)
    submission = runs.get_case(CASE_ID)["submission"]
    deployment = {"deployment_id": "selected-api", "revision": submission["source"]["revision"],
                  "source_sha256": submission["source"]["sha256"], "contracts": [{
                      "contract_id": submission["contract"]["id"], "contract_sha256": "a" * 64,
                      "case_id": CASE_ID}]}
    change = {"change_id": "change-one", "contract_id": submission["contract"]["id"],
              "previous_sha256": "a" * 64, "contract_sha256": submission["contract"]["sha256"]}
    yield graph, runs, store, runner, deployment, change
    graph.close()
    runs.close()


def selected(environment):
    graph, _, _, _, deployment, change = environment
    graph.register(deployment)
    graph.select(change)
    return graph


def finished(runs, run):
    runs._pool.submit(lambda: None).result(timeout=5)
    result = runs.get_run(run["run_id"])
    assert result["execution_status"] not in {"queued", "running"}
    return result


def assert_error(status, operation):
    with pytest.raises(ServiceError) as caught:
        operation()
    assert caught.value.status == status
    return caught.value


def test_selection_uses_exact_contract_version_and_never_claims_execution(environment):
    graph, runs, store, runner, deployment, change = environment
    graph.register(deployment)
    unrelated = copy.deepcopy(deployment)
    unrelated["deployment_id"] = "unrelated-api"
    unrelated["contracts"][0]["contract_sha256"] = "b" * 64
    graph.register(unrelated)
    report = graph.select(change)
    assert report["schema_version"] == "proofrun.graph.v1"
    assert report["provider"] == "neo4j"
    assert store.selections == [(change["contract_id"], change["previous_sha256"])]
    assert len(report["deployments"]) == 1
    entry = report["deployments"][0]
    assert entry["deployment_id"] == deployment["deployment_id"]
    assert entry["status"] == "requires_reverification"
    assert entry["requires_reverification"] is True
    assert entry["explanation"]
    assert "run_id" not in entry and "result" not in entry
    assert "_deployment" not in entry and "_input_sha256" not in report
    runner.assert_not_called()
    assert not list(runs.artifact_dir.glob("run-*"))


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(deployment_id="../outside"),
    lambda p: p.update(revision="a" * 39),
    lambda p: p.update(source_sha256="x" * 64),
    lambda p: p.update(contracts=[]),
    lambda p: p["contracts"].append(copy.deepcopy(p["contracts"][0])),
    lambda p: p.update(contracts=[{**p["contracts"][0], "contract_id": f"c-{i}"} for i in range(21)]),
    lambda p: p["contracts"][0].update(case_id="bad/id"),
    lambda p: p.update(requires_reverification=False),
    lambda p: p["contracts"][0].update(evidence={"passed": True}),
])
def test_registration_rejects_unbounded_or_client_supplied_evidence(environment, mutation):
    graph, _, store, runner, deployment, _ = environment
    mutation(deployment)
    assert_error(400, lambda: graph.register(deployment))
    assert not store.deployments
    runner.assert_not_called()


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(change_id="../outside"),
    lambda p: p.update(contract_id="bad/id"),
    lambda p: p.update(previous_sha256="a" * 63),
    lambda p: p.update(contract_sha256="A" * 64),
    lambda p: p.update(contract_sha256=p["previous_sha256"]),
    lambda p: p.update(finding_status="no_difference_observed"),
])
def test_invalid_changes_do_not_query_the_graph(environment, mutation):
    graph, _, store, runner, _, change = environment
    mutation(change)
    assert_error(400, lambda: graph.select(change))
    assert not store.selections
    runner.assert_not_called()


def test_selection_identity_survives_restart_and_rejects_changed_inputs(environment):
    graph, runs, store, _, deployment, change = environment
    graph.register(deployment)
    first = graph.select(change)
    graph.close()
    restarted = DeploymentGraphService(runs, graph.directory, store)
    try:
        assert restarted.select(change) == first
        assert len(store.selections) == 1
        conflict = assert_error(409, lambda: restarted.select({**change, "contract_sha256": "b" * 64}))
        assert conflict.code == "change_id_conflict"
        assert restarted.get(change["change_id"])["deployments"] == first["deployments"]
    finally:
        restarted.close()


def test_selected_run_is_fresh_namespaced_and_preserves_measured_regression(environment):
    graph, runs, store, runner, deployment, change = environment
    direct_payload = runs.get_case(CASE_ID)["submission"]
    direct_payload["job_key"] = "reused-caller-key"
    direct, _ = runs.submit(direct_payload)
    finished(runs, direct)
    selected(environment)
    payload = {"deployment_id": deployment["deployment_id"], "job_key": "reused-caller-key"}
    run, created = graph.submit(change["change_id"], payload)
    assert created and run["run_id"] != direct["run_id"]
    assert run["job_key"] != payload["job_key"]
    measured = finished(runs, run)
    retry, created = graph.submit(change["change_id"], payload)
    assert not created and retry["run_id"] == run["run_id"]
    report = graph.get(change["change_id"])
    entry = report["deployments"][0]
    assert entry["status"] == "measured"
    assert entry["requires_reverification"] is False
    assert entry["result"]["finding_status"] == "regression_reproduced"
    assert entry["result"]["repair_status"] == "not_requested"
    assert entry["result"]["bindings"] == measured["bindings"]
    assert any(row["status"] == "failed" for row in measured["cases"])
    assert store.results[run["run_id"]][2] == measured
    assert runner.call_count == 2
    assert "_job_key" not in entry


def test_a_job_preclaimed_from_public_selection_inputs_cannot_be_adopted(environment):
    graph, runs, _, runner, deployment, change = environment
    caller_key = "preclaimed-job"
    public_key = "graph-" + sha256(canonical({
        "change": sha256(canonical(change)), "deployment": deployment, "job_key": caller_key}))
    direct = runs.get_case(CASE_ID)["submission"]
    direct.update(job_key=public_key, repair={"enabled": False, "max_attempts": 1})
    old_run, _ = runs.submit(direct)
    finished(runs, old_run)
    selected(environment)
    run, created = graph.submit(change["change_id"], {
        "deployment_id": deployment["deployment_id"], "job_key": caller_key})
    assert created and run["run_id"] != old_run["run_id"]
    assert run["job_key"] != public_key
    finished(runs, run)
    assert runner.call_count == 2
    entry = graph.get(change["change_id"])["deployments"][0]
    assert entry["run_id"] == run["run_id"]
    assert not any(key.startswith("_") for key in entry)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(revision="b" * 40),
    lambda p: p.update(source_sha256="b" * 64),
    lambda p: p["contracts"][0].update(case_id="unsupported-case"),
])
def test_selected_deployment_must_match_the_registered_case_before_execution(environment, mutation):
    graph, _, _, runner, deployment, change = environment
    mutation(deployment)
    selected(environment)
    assert_error(404 if deployment["contracts"][0]["case_id"] == "unsupported-case" else 409,
                 lambda: graph.submit(change["change_id"], {"deployment_id": deployment["deployment_id"], "job_key": "one"}))
    runner.assert_not_called()


def test_stale_registration_and_unselected_deployment_cannot_submit(environment):
    graph, _, _, runner, deployment, change = environment
    selected(environment)
    assert_error(404, lambda: graph.submit(change["change_id"], {"deployment_id": "unselected", "job_key": "one"}))
    graph.register({**deployment, "revision": "b" * 40})
    assert_error(409, lambda: graph.submit(change["change_id"], {"deployment_id": deployment["deployment_id"], "job_key": "one"}))
    entry = graph.get(change["change_id"])["deployments"][0]
    assert entry["status"] == "superseded" and entry["requires_reverification"]
    runner.assert_not_called()


def test_unregistered_target_contract_stays_pending_until_it_can_execute(environment):
    graph, _, _, runner, deployment, change = environment
    change["contract_sha256"] = "c" * 64
    selected(environment)
    entry = graph.get(change["change_id"])["deployments"][0]
    assert entry["status"] == "requires_reverification" and entry["requires_reverification"]
    assert_error(409, lambda: graph.submit(change["change_id"], {
        "deployment_id": deployment["deployment_id"], "job_key": "one"}))
    runner.assert_not_called()


def test_active_selection_retries_same_job_and_rejects_a_second_job(environment):
    graph, runs, _, runner, deployment, change = environment
    started, release = threading.Event(), threading.Event()

    def blocked(spec, source):
        started.set()
        assert release.wait(timeout=5)
        return run_fixtures.comparison(spec, source)

    runner.side_effect = blocked
    selected(environment)
    payload = {"deployment_id": deployment["deployment_id"], "job_key": "one"}
    try:
        run, _ = graph.submit(change["change_id"], payload)
        assert started.wait(timeout=3)
        retry, created = graph.submit(change["change_id"], payload)
        assert not created and retry["run_id"] == run["run_id"]
        assert_error(409, lambda: graph.submit(change["change_id"], {**payload, "job_key": "two"}))
        assert graph.get(change["change_id"])["deployments"][0]["requires_reverification"]
    finally:
        release.set()
    finished(runs, run)
    assert runner.call_count == 1


def test_busy_worker_does_not_replace_a_previously_measured_selection(environment):
    graph, runs, _, runner, deployment, change = environment
    selected(environment)
    payload = {"deployment_id": deployment["deployment_id"], "job_key": "one"}
    first, _ = graph.submit(change["change_id"], payload)
    finished(runs, first)
    before = graph.get(change["change_id"])["deployments"][0]
    started, release = threading.Event(), threading.Event()

    def blocked(spec, source):
        started.set()
        assert release.wait(timeout=5)
        return run_fixtures.comparison(spec, source)

    runner.side_effect = blocked
    unrelated_payload = runs.get_case(CASE_ID)["submission"]
    unrelated_payload["job_key"] = "unrelated-worker-job"
    try:
        unrelated, _ = runs.submit(unrelated_payload)
        assert started.wait(timeout=3)
        assert_error(409, lambda: graph.submit(change["change_id"], {**payload, "job_key": "two"}))
        assert graph.get(change["change_id"])["deployments"][0] == before
    finally:
        release.set()
    finished(runs, unrelated)
    runner.side_effect = run_fixtures.comparison
    retry, created = graph.submit(change["change_id"], {**payload, "job_key": "two"})
    assert created and retry["run_id"] != first["run_id"]
    finished(runs, retry)
    assert graph.get(change["change_id"])["deployments"][0]["requires_reverification"] is False


def test_failed_execution_remains_required_and_allows_a_fresh_attempt(environment):
    graph, runs, _, runner, deployment, change = environment
    selected(environment)
    runner.side_effect = RuntimeError("synthetic runner failure")
    payload = {"deployment_id": deployment["deployment_id"], "job_key": "one"}
    run, _ = graph.submit(change["change_id"], payload)
    finished(runs, run)
    entry = graph.get(change["change_id"])["deployments"][0]
    assert entry["status"] == "inconclusive" and entry["requires_reverification"]
    assert entry["result"]["execution_status"] == "setup_failed"
    runner.side_effect = run_fixtures.comparison
    next_run, created = graph.submit(change["change_id"], {**payload, "job_key": "two"})
    assert created and next_run["run_id"] != run["run_id"]
    finished(runs, next_run)
    assert graph.get(change["change_id"])["deployments"][0]["requires_reverification"] is False


@pytest.mark.parametrize("changed_input", [
    "demo/upgrade/test_controls.py", "demo/upgrade/requirements-new.txt",
    "sandbox/upgrade.Dockerfile", "worker_code_hashes",
])
def test_changed_trusted_inputs_invalidate_measurement_and_require_a_fresh_run(environment, changed_input):
    graph, runs, store, runner, deployment, change = environment
    selected(environment)
    payload = {"deployment_id": deployment["deployment_id"], "job_key": "same-client-job"}
    first, _ = graph.submit(change["change_id"], payload)
    first_result = finished(runs, first)
    assert graph.get(change["change_id"])["deployments"][0]["requires_reverification"] is False
    registered_before = runs.get_case(CASE_ID)["submission"]

    if changed_input == "worker_code_hashes":
        runs.worker_code_hashes = {**runs.worker_code_hashes, "service.py": "b" * 64}
    else:
        path = runs.root / changed_input
        path.write_text(path.read_text() + "\n# changed trusted verification input\n")

    # The app bytes, contract bytes, and Git revision alone still match. The
    # measurement must also bind the tests, environment and worker inputs.
    assert runs.get_case(CASE_ID)["submission"] == registered_before
    stale = graph.get(change["change_id"])["deployments"][0]
    assert stale["status"] == "superseded" and stale["requires_reverification"]
    second, created = graph.submit(change["change_id"], payload)
    assert created and second["run_id"] != first["run_id"]
    assert second["job_key"] != first["job_key"]
    finished(runs, second)
    measured = graph.get(change["change_id"])["deployments"][0]
    assert measured["status"] == "measured" and not measured["requires_reverification"]
    assert measured["run_id"] == second["run_id"]
    assert store.results[first["run_id"]][2] == first_result
    assert runner.call_count == 2


def test_fresh_submission_records_the_previous_terminal_run_without_graph_polling(environment):
    graph, runs, store, _, deployment, change = environment
    selected(environment)
    payload = {"deployment_id": deployment["deployment_id"], "job_key": "first"}
    first, _ = graph.submit(change["change_id"], payload)
    first_result = finished(runs, first)
    assert not store.results
    second, created = graph.submit(change["change_id"], {**payload, "job_key": "second"})
    assert created and second["run_id"] != first["run_id"]
    assert store.results[first["run_id"]][2] == first_result
    finished(runs, second)
    graph.get(change["change_id"])
    assert set(store.results) == {first["run_id"], second["run_id"]}


@pytest.mark.parametrize("mutation", [
    lambda r: r["execution"].update(runner="prepared"),
    lambda r: r.update(expected_case_ids=[], executed_case_ids=[]),
    lambda r: r["executed_case_ids"].pop(),
    lambda r: r["executed_case_ids"].append(r["executed_case_ids"][0]),
    lambda r: r["bindings"].pop("verifier_sha256"),
    lambda r: r.update(finding_status="inconclusive"),
])
def test_incomplete_or_prepared_evidence_never_clears_reverification(environment, mutation):
    graph, runs, _, _, deployment, change = environment
    selected(environment)
    run, _ = graph.submit(change["change_id"], {"deployment_id": deployment["deployment_id"], "job_key": "one"})
    result = finished(runs, run)
    mutation(result)
    with patch.object(runs, "get_run", return_value=result):
        entry = graph.get(change["change_id"])["deployments"][0]
    assert entry["status"] == "inconclusive" and entry["requires_reverification"]


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(job_key="some-other-job"),
    lambda r: r.update(case_id="some-other-case"),
    lambda r: r["bindings"].update(revision="b" * 40),
    lambda r: r["bindings"].update(source_sha256="b" * 64),
    lambda r: r["bindings"].update(contract_sha256="b" * 64),
])
def test_run_from_different_bindings_is_not_attributed_to_the_selection(environment, mutation):
    graph, runs, store, _, deployment, change = environment
    selected(environment)
    run, _ = graph.submit(change["change_id"], {"deployment_id": deployment["deployment_id"], "job_key": "one"})
    result = finished(runs, run)
    mutation(result)
    with patch.object(runs, "get_run", return_value=result):
        entry = graph.get(change["change_id"])["deployments"][0]
    assert entry["status"] == "superseded" and entry["requires_reverification"]
    assert not store.results


def test_graph_write_failure_keeps_completed_result_retryable(environment):
    graph, runs, store, _, deployment, change = environment
    selected(environment)
    run, _ = graph.submit(change["change_id"], {"deployment_id": deployment["deployment_id"], "job_key": "one"})
    measured = finished(runs, run)
    with patch.object(store, "record_run", side_effect=GraphUnavailable("private connection detail")):
        error = assert_error(503, lambda: graph.get(change["change_id"]))
        assert "private connection detail" not in error.message
    entry = graph.get(change["change_id"])["deployments"][0]
    assert entry["status"] == "measured" and not entry["requires_reverification"]
    assert store.results[run["run_id"]][2] == measured
    graph.register({**deployment, "source_sha256": "b" * 64})
    stale = graph.get(change["change_id"])["deployments"][0]
    assert stale["status"] == "superseded" and stale["requires_reverification"]


def test_unconfigured_graph_is_unavailable_without_disabling_core_runs(environment, tmp_path):
    _, runs, _, runner, deployment, change = environment
    graph = DeploymentGraphService(runs, tmp_path / "disabled-graph")
    try:
        for operation in (lambda: graph.register(deployment), lambda: graph.select(change)):
            assert assert_error(503, operation).code == "graph_unavailable"
        runner.assert_not_called()
        payload = runs.get_case(CASE_ID)["submission"]
        payload["job_key"] = "independent"
        run, _ = runs.submit(payload)
        assert finished(runs, run)["execution_status"] == "completed"
    finally:
        graph.close()
