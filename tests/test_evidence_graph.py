"""Projection and persistence-boundary tests; no database, Docker or model calls."""
import copy
import json
from unittest.mock import Mock, patch

import pytest

from src.proofrun.evidence_graph import EvidenceGraphStore, RunGraphService, configured_run_graph
from src.proofrun.neo4j_store import GraphConfig, GraphUnavailable


class FakeEvidenceStore:
    def __init__(self):
        self.records, self.writes, self.reads = {}, [], []
        self.initialize = Mock()
        self.close = Mock()

    def write_snapshot(self, snapshot, scope_hash):
        self.writes.append((copy.deepcopy(snapshot), scope_hash))
        self.records.setdefault((snapshot["graph_id"], scope_hash), copy.deepcopy(snapshot))

    def read_snapshot(self, graph_id, scope_hash):
        self.reads.append((graph_id, scope_hash))
        return copy.deepcopy(self.records[(graph_id, scope_hash)])


@pytest.fixture
def fixture_run():
    return {"run_id": "run-fixture", "case_id": "customer-nickname-v1", "execution_status": "completed",
        "finding_status": "regression_reproduced", "repair_status": "verified",
        "bindings": {"revision": "a" * 40, "source_sha256": "b" * 64, "contract_sha256": "c" * 64,
                     "tests_sha256": "f" * 64, "verifier_sha256": "9" * 64, "environment_manifest_sha256": "8" * 64},
        "attempts": [
            {"attempt": 1, "repair_status": "rejected", "execution_status": "completed", "candidate_sha256": "d" * 64},
            {"attempt": 2, "repair_status": "verified", "execution_status": "completed", "candidate_sha256": "e" * 64}],
        "verification": {"complete": True, "bindings": {"candidate_sha256": "e" * 64, "tests_sha256": "f" * 64}},
        "cases": [{"id": "nickname_omitted", "stage": "updated", "status": "failed"},
                  {"id": "nickname_omitted", "stage": "repaired_updated", "status": "passed"}],
        "artifacts": [{"id": "attempt-1-cases", "sha256": "1" * 64},
                      {"id": "attempt-2-cases", "sha256": "2" * 64}]}


@pytest.fixture
def release_run():
    return {"id": "release-example", "status": "completed", "request": {"benefit": "PRIVATE-benefit"},
        "result": {"recommendation": "skip", "contract_hash": "a" * 64,
            "revisions": {"baseline": "a" * 40, "candidate": "b" * 40},
            "source_hashes": {"baseline": "b" * 64, "candidate": "c" * 64},
            "findings": [{"id": "finding-1", "status": "confirmed", "requirement_id": "customer-response",
                "title": "PRIVATE-title", "hypothesis": "PRIVATE-provider-response",
                "evidence": {"probe_id": "probe-2", "probe_hash": "d" * 64, "baseline": ["PRIVATE-body"]}},
                {"id": "finding-2", "status": "inconclusive", "requirement_id": "customer-response"}],
            "repairs": [{"id": "repair-1", "finding_id": "finding-1", "status": "rejected",
                         "tree_hash": "e" * 64, "frozen_probe_hash": "f" * 64, "patch_artifact": "repair-1.diff",
                         "rationale": "PRIVATE-rationale", "deployed": False,
                         "tests": {"test_status": "passed", "complete": True, "output_tail": "PRIVATE-log"},
                         "probes": [{"id": "probe-2", "passed": False, "runs": ["PRIVATE-replays"]}]},
                        {"id": "repair-2", "finding_id": "finding-1", "status": "verification_stale",
                         "tree_hash": "1" * 64, "patch_artifact": "repair-2.diff"}],
            "tests": {"baseline": {"test_status": "passed", "frozen_test_hash": "2" * 64},
                      "candidate": {"test_status": "passed"}},
            "artifacts": [{"id": "repair-1.diff", "sha256": "3" * 64}, {"id": "repair-2.diff", "sha256": "4" * 64}]}}


def ready(kind, run, scope="operator"):
    store = FakeEvidenceStore()
    result = RunGraphService(store).snapshot(kind, run, scope=scope)
    assert result["status"] == "ready"
    return result, store


def test_fixture_keeps_every_attempt_and_original_failure(fixture_run):
    before = copy.deepcopy(fixture_run)
    result, store = ready("fixture", fixture_run)
    assert result["provider"] == "neo4j"
    assert result["schema_version"] == "proofrun.evidence-graph.v1"
    repairs = [n for n in result["nodes"] if n["kind"] == "repair"]
    assert {n["label"]: n["status"] for n in repairs} == {"Repair attempt 1": "rejected", "Repair attempt 2": "verified"}
    assert all(n["finding_id"] == fixture_run["case_id"] for n in repairs)
    assert next(n for n in result["nodes"] if n["kind"] == "finding")["status"] == "regression_reproduced"
    assert any(n.get("status") == "failed" for n in result["nodes"])
    assert store.reads == [(result["graph_id"], store.writes[0][1])]
    assert fixture_run == before
    assert not any("deploy" in e["label"].lower() for e in result["edges"])


def test_release_keeps_findings_repair_statuses_and_explicit_links(release_run):
    result, _ = ready("release", release_run)
    nodes = {n["id"]: n for n in result["nodes"]}
    assert len([n for n in nodes.values() if n["kind"] == "finding"]) == 2
    assert {n["status"] for n in nodes.values() if n["kind"] == "repair"} == {"rejected", "verification_stale"}
    assert len([n for n in nodes.values() if n["kind"] == "requirement"]) == 1
    for edge in result["edges"]:
        assert edge["source"] in nodes and edge["target"] in nodes
        if edge["label"] == "REPAIR_ATTEMPT":
            assert nodes[edge["source"]]["finding_id"] == nodes[edge["target"]]["finding_id"] == "finding-1"


def test_private_fields_outputs_provider_text_and_injected_verdict_are_not_projected(release_run):
    release_run.update(_private={"NEO4J_PASSWORD": "PRIVATE-password"}, source="PRIVATE-source")
    release_run["result"].update(agent={"accepted": True, "summary": "PRIVATE-token"}, accepted=True)
    release_run["result"]["repairs"][0]["verified"] = True
    result, store = ready("release", release_run)
    public = json.dumps(result)
    persisted = json.dumps(store.writes)
    for output in (public, persisted):
        assert "PRIVATE" not in output
        assert "NEO4J_PASSWORD" not in output
        assert "accepted" not in output
    assert next(n for n in result["nodes"] if n["label"] == "repair-1")["status"] == "rejected"


def test_deterministic_projection_ignores_private_changes_and_scope_isolates(fixture_run):
    store = FakeEvidenceStore()
    service = RunGraphService(store)
    first = service.snapshot("fixture", fixture_run)
    fixture_run["_new_secret"] = "private"
    fixture_run["updated_at"] = "untrusted-time-not-used"
    second = service.snapshot("fixture", fixture_run)
    assert second == first
    another = service.snapshot("fixture", fixture_run, scope="workspace-other")
    assert another["graph_id"] != first["graph_id"]
    assert another["nodes"] == first["nodes"]
    assert "workspace-other" not in json.dumps(store.writes)
    store.initialize.assert_called_once()
    fixture_run["attempts"][1]["repair_status"] = "rejected"
    assert service.snapshot("fixture", fixture_run)["graph_id"] != first["graph_id"]


@pytest.mark.parametrize("stage", ["initialize", "write_snapshot", "read_snapshot"])
def test_failures_have_no_fallback_or_secret_leak(fixture_run, stage):
    store = FakeEvidenceStore()
    setattr(store, stage, Mock(side_effect=GraphUnavailable("PRIVATE credential-bearing error")))
    result = RunGraphService(store).snapshot("fixture", fixture_run)
    assert result["status"] == "unavailable"
    assert result["nodes"] == result["edges"] == [] and result["graph_id"] is None
    assert "PRIVATE" not in json.dumps(result)
    assert fixture_run["repair_status"] == "verified"


@pytest.mark.parametrize("mutation", [
    lambda saved: saved.update(run_id="other-run"),
    lambda saved: saved["nodes"][0].update(detail="PRIVATE injected database content"),
    lambda saved: saved["nodes"].pop(),
    lambda saved: saved.update(observed_at="PRIVATE-time"),
    lambda saved: saved.update(extra="PRIVATE database field"),
])
def test_corrupt_or_wrong_scope_readback_is_unavailable(fixture_run, mutation):
    store = FakeEvidenceStore()
    original = store.read_snapshot
    def changed(*args):
        saved = original(*args)
        mutation(saved)
        return saved
    store.read_snapshot = changed
    result = RunGraphService(store).snapshot("fixture", fixture_run)
    assert result["status"] == "unavailable" and result["nodes"] == []


@pytest.mark.parametrize("kind,status", [("fixture", "queued"), ("fixture", "running"), ("release", "running")])
def test_partial_records_stay_pending_without_persisting(fixture_run, release_run, kind, status):
    record = fixture_run if kind == "fixture" else release_run
    record["execution_status" if kind == "fixture" else "status"] = status
    store = FakeEvidenceStore()
    result = RunGraphService(store).snapshot(kind, record)
    assert result["status"] == "pending" and result["nodes"] == []
    assert not store.writes and not store.reads


def test_disabled_configuration_is_safe(monkeypatch, fixture_run):
    monkeypatch.setenv("PROOFRUN_NEO4J_ENABLED", "false")
    with patch("src.proofrun.evidence_graph.EvidenceGraphStore") as factory:
        assert configured_run_graph().snapshot("fixture", fixture_run)["status"] == "unavailable"
        factory.assert_not_called()
    monkeypatch.setenv("PROOFRUN_NEO4J_ENABLED", "invalid")
    assert configured_run_graph().snapshot("fixture", fixture_run)["status"] == "unavailable"


def test_close_is_idempotent_and_blocks_persistence(fixture_run):
    store = FakeEvidenceStore()
    service = RunGraphService(store)
    service.close()
    service.close()
    store.close.assert_called_once()
    assert service.snapshot("fixture", fixture_run)["status"] == "unavailable"
    assert not store.writes


def test_graph_size_is_bounded_without_dropping_repair_attempts(fixture_run):
    fixture_run["cases"] *= 200
    result, _ = ready("fixture", fixture_run)
    assert len(result["nodes"]) <= 80 and len(result["edges"]) <= 120
    assert len([n for n in result["nodes"] if n["kind"] == "repair"]) == 2
    assert "bounded" in result["message"]
    assert all(len(n["label"]) <= 120 and len(n.get("detail", "")) <= 500 for n in result["nodes"])


def test_dashboard_has_prepared_fix_and_recorded_checks_without_worker_acceptance():
    record = {"id": "dashboard-pydantic-report", "status": "completed", "report_sha256": "a" * 64,
        "case": {"id": "pydantic", "status": "confirmed_break", "package": "pydantic",
                 "fromVersion": "1.10.18", "toVersion": "2.8.2", "patch_sha256": "b" * 64,
                 "beforeCode": "PRIVATE-source", "provenance": "PRIVATE-provider",
                 "checks": [{"id": "probe", "before": {"status": "pass", "output": "PRIVATE-log"}, "after": {"status": "fail"}},
                            {"id": "fixed", "before": {"status": "pass"}, "after": {"status": "pass"}}]}}
    result, _ = ready("dashboard", record)
    repair = next(n for n in result["nodes"] if n["kind"] == "repair")
    assert repair["label"] == "Prepared fix" and repair["status"] == "prepared"
    assert all(n.get("status") != "verified" for n in result["nodes"])
    assert len([n for n in result["nodes"] if n["kind"] == "environment"]) == 2
    assert len([n for n in result["nodes"] if n["kind"] == "test"]) == 4
    assert "PRIVATE" not in json.dumps(result)
    record.pop("report_sha256")
    assert RunGraphService(FakeEvidenceStore()).snapshot("dashboard", record)["status"] == "pending"


def test_store_uses_parameterized_namespaced_graph_and_read_transactions(fixture_run):
    result, fake = ready("fixture", fixture_run)
    snapshot, scope_hash = fake.writes[0]
    driver = Mock()
    store = EvidenceGraphStore(GraphConfig(enabled=True, uri="bolt://localhost:7687", username="neo4j", password="PRIVATE-password"), driver)
    store._execute = Mock(return_value=[{"graph_id": result["graph_id"], "node_count": len(result["nodes"]), "edge_count": len(result["edges"])}])
    store.write_snapshot(snapshot, scope_hash)
    query, parameters = store._execute.call_args.args
    assert "ProofRunEvidenceGraph" in query and "EVIDENCE_LINK" in query
    assert result["graph_id"] not in query and fixture_run["run_id"] not in query
    assert parameters["scope_hash"] == scope_hash
    assert all(n["node_key"].startswith(result["graph_id"] + ":") for n in parameters["nodes"])
    raw = {**snapshot, "scope_hash": scope_hash, "fingerprint": result["graph_id"],
           "nodes": [{**row["properties"], "node_key": row["node_key"]} for row in parameters["nodes"]],
           "edges": [{**row["properties"], "edge_key": row["edge_key"]} for row in parameters["edges"]]}
    store._execute.return_value = [raw]
    assert store.read_snapshot(result["graph_id"], scope_hash) == snapshot
    assert store._execute.call_args.kwargs == {"read": True}
    assert store._execute.call_args.args[1] == {"graph_id": result["graph_id"], "scope_hash": scope_hash}


def test_source_scan_graph_describes_unverified_suggestion_only():
    result, _ = ready("dashboard", {"id": "dashboard-scan-1", "status": "completed", "report_sha256": "a" * 64,
        "case": {"id": "scan-repository-0", "kind": "source_scan", "status": "confirmed_break",
                 "checks": [{"id": "forged-check", "before": {"status": "pass"}, "after": {"status": "pass"}}],
                 "patch_sha256": "b" * 64, "commit": "c" * 40}})
    assert next(n for n in result["nodes"] if n["kind"] == "finding")["status"] == "unverified"
    repair = next(n for n in result["nodes"] if n["kind"] == "repair")
    assert repair["label"] == "Suggested fix" and repair["status"] == "unverified"
    assert "suggested_snippets_sha256" in repair["detail"]
    assert not any(n["kind"] in {"verification", "test"} for n in result["nodes"])


def test_failed_terminal_release_has_a_graph_without_invented_test_evidence():
    result, _ = ready("release", {"id": "release-failed", "status": "failed", "result": None})
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["kind"] == "run" and result["nodes"][0]["status"] == "failed"
    assert result["edges"] == []
