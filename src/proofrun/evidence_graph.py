"""Bounded, scope-bound Neo4j projections of authoritative saved run evidence.

This service records evidence for display. It never runs a test, accepts a fix,
or infers deployment relationships. A ready result has been read back from
Neo4j; missing configuration and database failures have no memory fallback.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone

from .neo4j_store import GraphConfig, GraphConfigurationError, GraphUnavailable, Neo4jStore

SCHEMA_VERSION = "proofrun.evidence-graph.v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_REVISION = re.compile(r"[a-f0-9]{40}\Z")
_STATUSES = frozenset({"queued", "running", "completed", "failed", "setup_failed", "timed_out",
    "interrupted", "not_tested", "regression_reproduced", "no_difference_observed", "inconclusive",
    "not_requested", "pending", "proposed", "verified", "rejected", "unavailable", "confirmed",
    "verified_candidate", "verification_stale", "passed", "pass", "fail", "error", "timeout",
    "confirmed_break", "not_run", "prepared", "preserved", "regression", "unknown", "unverified"})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _hash(value):
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _status(value):
    return value if isinstance(value, str) and value in _STATUSES else "unknown"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _hash_details(mapping, names):
    return "; ".join(name + ": " + mapping[name] for name in names if _hash(mapping.get(name)))


class _Projection:
    def __init__(self):
        self.nodes, self.edges = {}, {}
        self.omitted = False

    def node(self, key, kind, label, *, status=None, detail=None, finding_id=None, required=True):
        identifier = "n-" + _digest([kind, key])[:24]
        if identifier not in self.nodes and len(self.nodes) >= 80:
            if required:
                raise ValueError("Too many primary evidence records")
            self.omitted = True
            return None
        node = {"id": identifier, "kind": kind, "label": label[:120]}
        if status is not None:
            node["status"] = _status(status)
        if detail:
            node["detail"] = detail[:500]
        if finding_id:
            node["finding_id"] = finding_id
        if identifier in self.nodes and self.nodes[identifier] != node:
            raise ValueError("Duplicate evidence identity")
        self.nodes[identifier] = node
        return identifier

    def link(self, source, target, label):
        if not source or not target:
            return
        if len(self.edges) >= 120:
            self.omitted = True
            return
        identifier = "e-" + _digest([source, target, label])[:24]
        self.edges[identifier] = {"id": identifier, "source": source, "target": target, "label": label}

    def lists(self):
        return ([self.nodes[k] for k in sorted(self.nodes)], [self.edges[k] for k in sorted(self.edges)])


def _artifact_nodes(graph, parent, artifacts, related=None):
    for artifact in _rows(artifacts):
        artifact = _mapping(artifact)
        identifier, fingerprint = _identifier(artifact.get("id")), _hash(artifact.get("sha256"))
        if not identifier or not fingerprint:
            continue
        owner, finding_id = (related or {}).get(identifier, (parent, None))
        node = graph.node("artifact:" + identifier, "artifact", identifier, detail="sha256: " + fingerprint,
                          finding_id=finding_id, required=False)
        graph.link(owner, node, "RECORDED_ARTIFACT")


def _fixture(run, graph, root):
    bindings = _mapping(run.get("bindings"))
    revision = bindings.get("revision")
    if isinstance(revision, str) and _REVISION.fullmatch(revision):
        source = graph.node("source", "revision", "Recorded source revision", detail="revision: " + revision
                            + "; " + _hash_details(bindings, ("source_sha256", "bundle_sha256")))
        graph.link(root, source, "SOURCE_REVISION")
    if _hash(bindings.get("contract_sha256")):
        contract = graph.node("contract", "contract", "Registered contract",
                              detail="contract_sha256: " + bindings["contract_sha256"])
        graph.link(root, contract, "EVALUATED_CONTRACT")
    finding_id = _identifier(run.get("case_id"))
    if not finding_id:
        raise ValueError("Missing case identity")
    finding = graph.node("finding", "finding", finding_id, status=run.get("finding_status"),
                         finding_id=finding_id)
    graph.link(root, finding, "RECORDED_FINDING")
    comparison_owner = finding
    comparison_bindings = _hash_details(bindings, ("tests_sha256", "verifier_sha256", "environment_manifest_sha256"))
    if comparison_bindings:
        comparison_owner = graph.node("comparison", "evidence", "Comparison evidence", detail=comparison_bindings,
                                      finding_id=finding_id)
        graph.link(finding, comparison_owner, "COMPARISON_EVIDENCE")
    repairs, artifact_owners = {}, {}
    for attempt in _rows(run.get("attempts")):
        attempt = _mapping(attempt)
        number = attempt.get("attempt")
        if type(number) is not int or not 1 <= number <= 100 or number in repairs:
            raise ValueError("Invalid repair attempt identity")
        node = graph.node("attempt:" + str(number), "repair", "Repair attempt " + str(number),
                          status=attempt.get("repair_status"), finding_id=finding_id,
                          detail=_hash_details(attempt, ("candidate_sha256",)))
        graph.link(finding, node, "REPAIR_ATTEMPT")
        repairs[number] = node
    verification = _mapping(run.get("verification"))
    if verification:
        detail = _hash_details(_mapping(verification.get("bindings")),
                               ("candidate_sha256", "tests_sha256", "verifier_sha256", "environment_manifest_sha256"))
        node = graph.node("verification", "verification", "Recorded repair verification", detail=detail,
                          finding_id=finding_id)
        owner = repairs[max(repairs)] if repairs else root
        graph.link(owner, node, "VERIFICATION_EVIDENCE")
    for index, case in enumerate(_rows(run.get("cases"))):
        case = _mapping(case)
        identifier, stage = _identifier(case.get("id")), _identifier(case.get("stage"))
        if not identifier or not stage:
            continue
        node = graph.node("check:" + str(index), "test", stage + ": " + identifier,
                          status=case.get("status"), finding_id=finding_id, required=False)
        owner = repairs[max(repairs)] if stage.startswith("repaired_") and repairs else comparison_owner
        graph.link(owner, node, "RECORDED_CHECK")
    for artifact in _rows(run.get("artifacts")):
        identifier = _mapping(artifact).get("id", "")
        if isinstance(identifier, str):
            match = re.match(r"attempt-(\d+)-", identifier)
            if match and int(match[1]) in repairs:
                artifact_owners[identifier] = (repairs[int(match[1])], finding_id)
    _artifact_nodes(graph, root, run.get("artifacts"), artifact_owners)


def _test_node(graph, key, label, result, owner, finding_id=None):
    result = _mapping(result)
    if not result:
        return
    details = _hash_details(result, ("frozen_test_hash",))
    if type(result.get("complete")) is bool:
        details += ("; " if details else "") + "complete: " + str(result["complete"]).lower()
    node = graph.node(key, "test", label, status=result.get("test_status"), detail=details,
                      finding_id=finding_id, required=False)
    graph.link(owner, node, "RECORDED_CHECK")


def _release(run, graph, root):
    result = _mapping(run.get("result"))
    revisions, sources = _mapping(result.get("revisions")), _mapping(result.get("source_hashes"))
    for role in ("baseline", "candidate"):
        revision = revisions.get(role)
        if isinstance(revision, str) and _REVISION.fullmatch(revision):
            detail = "revision: " + revision
            if _hash(sources.get(role)):
                detail += "; source_sha256: " + sources[role]
            node = graph.node(role, "revision", role.capitalize() + " revision", detail=detail)
            graph.link(root, node, "SOURCE_REVISION")
    contract = None
    if _hash(result.get("contract_hash")):
        contract = graph.node("contract", "contract", "Approved release contract",
                              detail="contract_sha256: " + result["contract_hash"])
        graph.link(root, contract, "EVALUATED_CONTRACT")
    findings, artifact_owners = {}, {}
    for finding in _rows(result.get("findings")):
        finding = _mapping(finding)
        identifier = _identifier(finding.get("id"))
        if not identifier or identifier in findings:
            raise ValueError("Invalid finding identity")
        node = graph.node("finding:" + identifier, "finding", identifier,
                          status=finding.get("status"), finding_id=identifier)
        findings[identifier] = node
        graph.link(root, node, "RECORDED_FINDING")
        requirement = _identifier(finding.get("requirement_id"))
        if requirement:
            req = graph.node("requirement:" + requirement, "requirement", requirement)
            graph.link(contract or root, req, "RECORDED_REQUIREMENT")
            graph.link(req, node, "HAS_FINDING")
        evidence = _mapping(finding.get("evidence"))
        if _identifier(evidence.get("probe_id")) and _hash(evidence.get("probe_hash")):
            probe = graph.node("probe:" + identifier, "evidence", evidence["probe_id"],
                               detail="probe_sha256: " + evidence["probe_hash"], finding_id=identifier)
            graph.link(node, probe, "EXPERIMENT_EVIDENCE")
    seen_repairs = set()
    # Add every repair before optional test/artifact nodes consume the bound.
    repair_nodes = []
    for repair in _rows(result.get("repairs")):
        repair = _mapping(repair)
        identifier, finding_id = _identifier(repair.get("id")), _identifier(repair.get("finding_id"))
        if not identifier or identifier in seen_repairs:
            raise ValueError("Invalid repair identity")
        seen_repairs.add(identifier)
        node = graph.node("repair:" + identifier, "repair", identifier, status=repair.get("status"),
                          detail=_hash_details(repair, ("tree_hash", "frozen_probe_hash")), finding_id=finding_id)
        graph.link(findings.get(finding_id, root), node, "REPAIR_ATTEMPT")
        repair_nodes.append((repair, node, finding_id))
        patch = _identifier(repair.get("patch_artifact"))
        if patch:
            artifact_owners[patch] = (node, finding_id)
    for repair, node, finding_id in repair_nodes:
        _test_node(graph, "tests:" + repair["id"], "Repair original tests", repair.get("tests"), node, finding_id)
        for index, probe in enumerate(_rows(repair.get("probes"))):
            probe = _mapping(probe)
            identifier = _identifier(probe.get("id"))
            if not identifier:
                continue
            checked = graph.node("replay:" + repair["id"] + ":" + str(index), "verification", identifier,
                                 status=("passed" if probe["passed"] else "failed") if type(probe.get("passed")) is bool else "unknown",
                                 finding_id=finding_id, required=False)
            graph.link(node, checked, "RECORDED_REPLAY")
    for role in ("baseline", "candidate"):
        _test_node(graph, "tests:" + role, role.capitalize() + " original tests",
                   _mapping(result.get("tests")).get(role), root)
    _artifact_nodes(graph, root, result.get("artifacts"), artifact_owners)


def _dashboard(run, graph, root):
    case = _mapping(run.get("case"))
    source_scan = case.get("kind") == "source_scan"
    finding_id = _identifier(case.get("id"))
    if not finding_id:
        raise ValueError("Missing dashboard case identity")
    finding = graph.node("finding", "finding", finding_id, status="unverified" if source_scan else case.get("status"),
                         finding_id=finding_id)
    graph.link(root, finding, "RECORDED_FINDING")
    package = _identifier(case.get("package"))
    for role, version_key in (("before", "fromVersion"), ("after", "toVersion")):
        version = _identifier(case.get(version_key))
        if package and version:
            node = graph.node("package:" + role, "environment", package + " " + version,
                              detail="Recorded " + role + " environment")
            graph.link(root, node, "COMPARED_ENVIRONMENT")
    revision = case.get("commit")
    if isinstance(revision, str) and _REVISION.fullmatch(revision):
        node = graph.node("revision", "revision", "Recorded repository revision", detail="revision: " + revision)
        graph.link(root, node, "SOURCE_REVISION")
    patch = None
    if _hash(case.get("patch_sha256")):
        patch = graph.node("prepared-fix", "repair", "Suggested fix" if source_scan else "Prepared fix",
                           status="unverified" if source_scan else "prepared", finding_id=finding_id,
                           detail=("suggested_snippets_sha256: " if source_scan else "patch_sha256: ")
                           + case["patch_sha256"] + ("; Static suggestion; no execution evidence." if source_scan
                                                     else "; Stored comparison checks only."))
        graph.link(finding, patch, "SUGGESTED_FIX" if source_scan else "PREPARED_FIX")
    for index, check in enumerate([] if source_scan else _rows(case.get("checks"))):
        check = _mapping(check)
        identifier = _identifier(check.get("id"))
        if not identifier:
            continue
        for role in ("before", "after"):
            observation = _mapping(check.get(role))
            if not observation:
                continue
            node = graph.node("check:" + str(index) + ":" + role, "test", identifier + ": " + role,
                              status=observation.get("status"), finding_id=finding_id, required=False)
            graph.link(patch if identifier == "fixed" and patch else finding, node, "RECORDED_CHECK")
    _artifact_nodes(graph, root, [{"id": "source-scan-report" if source_scan else "comparison-report",
                                 "sha256": run["report_sha256"]}])


_CONSTRAINTS = (
    "CREATE CONSTRAINT proofrun_evidence_graph_id IF NOT EXISTS FOR (g:ProofRunEvidenceGraph) REQUIRE g.graph_id IS UNIQUE",
    "CREATE CONSTRAINT proofrun_evidence_node_key IF NOT EXISTS FOR (n:ProofRunEvidenceNode) REQUIRE n.node_key IS UNIQUE",
)
_WRITE = """
MERGE (g:ProofRunEvidenceGraph {graph_id: $graph_id})
ON CREATE SET g += $metadata
WITH g WHERE g.scope_hash = $scope_hash AND g.fingerprint = $fingerprint
CALL {
  WITH g
  UNWIND $nodes AS row
  MERGE (n:ProofRunEvidenceNode {node_key: row.node_key})
  ON CREATE SET n += row.properties
  MERGE (g)-[:HAS_EVIDENCE]->(n)
  RETURN count(*) AS node_count
}
CALL {
  WITH g
  UNWIND $edges AS row
  MATCH (g)-[:HAS_EVIDENCE]->(s:ProofRunEvidenceNode {node_key: row.source_key})
  MATCH (g)-[:HAS_EVIDENCE]->(t:ProofRunEvidenceNode {node_key: row.target_key})
  MERGE (s)-[e:EVIDENCE_LINK {edge_key: row.edge_key}]->(t)
  ON CREATE SET e += row.properties
  RETURN count(*) AS edge_count
}
RETURN g.graph_id AS graph_id, node_count, edge_count
"""
_READ = """
MATCH (g:ProofRunEvidenceGraph {graph_id: $graph_id, scope_hash: $scope_hash})
CALL {
  WITH g
  MATCH (g)-[:HAS_EVIDENCE]->(n:ProofRunEvidenceNode)
  WITH n ORDER BY n.id
  RETURN collect(properties(n)) AS nodes
}
CALL {
  WITH g
  MATCH (g)-[:HAS_EVIDENCE]->(s:ProofRunEvidenceNode)-[e:EVIDENCE_LINK]->(t:ProofRunEvidenceNode)<-[:HAS_EVIDENCE]-(g)
  WITH e ORDER BY e.id
  RETURN collect(properties(e)) AS edges
}
RETURN g.graph_id AS graph_id, g.scope_hash AS scope_hash, g.kind AS kind,
       g.run_id AS run_id, g.fingerprint AS fingerprint, g.observed_at AS observed_at, nodes, edges
"""


class EvidenceGraphStore(Neo4jStore):
    def initialize(self):
        for query in _CONSTRAINTS:
            self._execute(query)

    def write_snapshot(self, snapshot, scope_hash):
        graph_id = snapshot["graph_id"]
        metadata = {key: snapshot[key] for key in ("kind", "run_id", "observed_at")}
        metadata.update(scope_hash=scope_hash, fingerprint=graph_id)
        parameters = {"graph_id": graph_id, "scope_hash": scope_hash, "fingerprint": graph_id, "metadata": metadata,
            "nodes": [{"node_key": graph_id + ":" + node["id"], "properties": node} for node in snapshot["nodes"]],
            "edges": [{"edge_key": graph_id + ":" + edge["id"], "source_key": graph_id + ":" + edge["source"],
                       "target_key": graph_id + ":" + edge["target"], "properties": edge} for edge in snapshot["edges"]]}
        result = self._execute(_WRITE, parameters)
        if result != [{"graph_id": graph_id, "node_count": len(snapshot["nodes"]), "edge_count": len(snapshot["edges"])}]:
            raise GraphUnavailable("Evidence graph write could not be confirmed.")

    def read_snapshot(self, graph_id, scope_hash):
        records = self._execute(_READ, {"graph_id": graph_id, "scope_hash": scope_hash}, read=True)
        if len(records) != 1:
            raise GraphUnavailable("Evidence graph read could not be confirmed.")
        record = records[0]
        if record.pop("scope_hash", None) != scope_hash or record.pop("fingerprint", None) != graph_id:
            raise GraphUnavailable("Evidence graph identity does not match.")
        for field, internal in (("nodes", "node_key"), ("edges", "edge_key")):
            for item in record[field]:
                if item.pop(internal, None) != graph_id + ":" + item.get("id", ""):
                    raise GraphUnavailable("Evidence graph identity does not match.")
        return record


class RunGraphService:
    def __init__(self, store=None):
        self.store, self._initialized, self._closed = store, False, False
        self._lock = threading.RLock()

    def snapshot(self, kind: str, run: dict, *, scope: str = "operator") -> dict:
        if kind not in {"fixture", "release", "dashboard"} or not isinstance(run, dict):
            raise ValueError("Unsupported evidence graph record")
        if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
            raise ValueError("Invalid evidence graph scope")
        run_id = _identifier(run.get("run_id" if kind == "fixture" else "id"))
        if not run_id:
            raise ValueError("Invalid evidence graph run identity")
        result = {"schema_version": SCHEMA_VERSION, "provider": "neo4j", "status": "unavailable", "kind": kind,
                  "run_id": run_id, "graph_id": None, "observed_at": datetime.now(timezone.utc).isoformat(),
                  "nodes": [], "edges": [], "message": "Neo4j is unavailable; no substitute graph was used."}
        status = run.get("execution_status" if kind == "fixture" else "status")
        terminal = {"completed", "setup_failed", "timed_out", "interrupted"} if kind == "fixture" else {"completed", "failed"}
        if status not in terminal or (kind == "dashboard" and not _hash(run.get("report_sha256"))):
            result.update(status="pending", message="The graph will be available after this run records its evidence.")
            return result
        with self._lock:
            if self.store is None or self._closed:
                return result
            try:
                graph = _Projection()
                root = graph.node("run", "run", ("Comparison " if kind == "dashboard" else "Run ") + run_id, status=status)
                {"fixture": _fixture, "release": _release, "dashboard": _dashboard}[kind](run, graph, root)
                nodes, edges = graph.lists()
                scope_hash = _digest(scope)
                graph_id = _digest({"schema_version": SCHEMA_VERSION, "scope_hash": scope_hash, "kind": kind,
                                    "run_id": run_id, "nodes": nodes, "edges": edges})
                expected = {"kind": kind, "run_id": run_id, "graph_id": graph_id, "nodes": nodes, "edges": edges}
                if not self._initialized:
                    self.store.initialize()
                    self._initialized = True
                self.store.write_snapshot({**expected, "observed_at": result["observed_at"]}, scope_hash)
                saved = self.store.read_snapshot(graph_id, scope_hash)
                if not isinstance(saved, dict) or {k: v for k, v in saved.items() if k != "observed_at"} != expected:
                    raise GraphUnavailable("Evidence graph read did not match the saved evidence.")
                observed_at = saved.get("observed_at")
                if not isinstance(observed_at, str) or len(observed_at) > 40:
                    raise GraphUnavailable("Evidence graph timestamp is invalid.")
                datetime.fromisoformat(observed_at)
                result.update(saved, status="ready", message=("Recorded evidence read from Neo4j. Display is bounded; additional checks remain in run artifacts."
                              if graph.omitted else "Recorded evidence read from Neo4j. Graph relationships do not approve or deploy a fix."))
            except Exception:
                # Driver errors and malformed records can contain arbitrary sensitive text.
                # The graph is optional and must never affect the saved run's verdict.
                pass
        return result

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.store is not None:
                try:
                    self.store.close()
                except Exception:
                    pass


def configured_run_graph():
    try:
        config = GraphConfig.from_env()
        return RunGraphService(EvidenceGraphStore(config) if config.enabled else None)
    except (GraphConfigurationError, GraphUnavailable):
        return RunGraphService()
