"""Optional Neo4j dependency selection and immutable measured-run provenance.

Only explicit dependency registrations are traversed. Graph relationships do
not approve repairs or mark a deployment verified. The official driver is
imported only when an enabled store creates its own connection.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import threading
from typing import Mapping
from urllib.parse import urlsplit


class GraphConfigurationError(ValueError):
    """Safe configuration error, never containing setting values."""


class GraphUnavailable(RuntimeError):
    """Safe database/driver failure; no alternate backend is substituted."""


class GraphConflict(ValueError):
    """An immutable measured run conflicts with previously recorded evidence."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_CHECK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_DATABASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")


def validate_id(value, field="identifier") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{field} must be a safe identifier of 1 to 128 characters.")
    return value


def _hash(value, field):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hash.")
    return value


def _revision(value):
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise ValueError("revision must be a full lowercase Git commit hash.")
    return value


def validate_contract(contract: dict) -> dict:
    if not isinstance(contract, dict) or set(contract) != {"contract_id", "contract_sha256", "case_id"}:
        raise ValueError("A dependency requires contract_id, contract_sha256 and case_id.")
    return {
        "contract_id": validate_id(contract["contract_id"], "contract_id"),
        "contract_sha256": _hash(contract["contract_sha256"], "contract_sha256"),
        "case_id": validate_id(contract["case_id"], "case_id"),
    }


def validate_deployment(deployment: dict) -> dict:
    if not isinstance(deployment, dict) or set(deployment) != {
            "deployment_id", "revision", "source_sha256", "contracts"}:
        raise ValueError("A deployment requires deployment_id, revision, source_sha256 and contracts.")
    contracts = deployment["contracts"]
    if not isinstance(contracts, list) or not 1 <= len(contracts) <= 20:
        raise ValueError("A deployment requires 1 to 20 explicit contract dependencies.")
    contracts = [validate_contract(contract) for contract in contracts]
    identities = [contract["contract_id"] for contract in contracts]
    if len(set(identities)) != len(identities):
        raise ValueError("A deployment must identify one version and case per contract.")
    return {
        "deployment_id": validate_id(deployment["deployment_id"], "deployment_id"),
        "revision": _revision(deployment["revision"]),
        "source_sha256": _hash(deployment["source_sha256"], "source_sha256"),
        "contracts": sorted(contracts, key=lambda item: (item["contract_id"], item["case_id"])),
    }


def _setting(env, name, default="", *, strip=True):
    value = env.get(name, default)
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise GraphConfigurationError(f"{name} must be a single-line string.")
    return value.strip() if strip else value


@dataclass(frozen=True)
class GraphConfig:
    enabled: bool = False
    uri: str = field(default="", repr=False)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    database: str = "neo4j"
    timeout_seconds: int = 10

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise GraphConfigurationError("PROOFRUN_NEO4J_ENABLED must be true or false.")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 30:
            raise GraphConfigurationError("Neo4j timeout must be 1 to 30 seconds.")
        if not isinstance(self.database, str) or not _DATABASE.fullmatch(self.database):
            raise GraphConfigurationError("NEO4J_DATABASE must be a valid database name.")
        if not self.enabled:
            return
        valid_uri = False
        try:
            if (isinstance(self.uri, str) and len(self.uri) <= 2048
                    and not any(c.isspace() or ord(c) < 32 for c in self.uri)):
                parsed = urlsplit(self.uri)
                valid_uri = (parsed.scheme in {"neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc"}
                             and bool(parsed.hostname) and parsed.username is None
                             and parsed.password is None and not parsed.path
                             and not parsed.query and not parsed.fragment
                             and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            pass
        if not valid_uri:
            raise GraphConfigurationError("NEO4J_URI must be a Bolt/Neo4j URI without embedded credentials.")
        if (not isinstance(self.username, str) or not 1 <= len(self.username) <= 128
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.username)):
            raise GraphConfigurationError("Set NEO4J_USERNAME in server-side configuration.")
        if (not isinstance(self.password, str) or not 1 <= len(self.password) <= 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in self.password)):
            raise GraphConfigurationError("Set NEO4J_PASSWORD in server-side secret configuration.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        enabled = _setting(env, "PROOFRUN_NEO4J_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise GraphConfigurationError("PROOFRUN_NEO4J_ENABLED must be true or false.")
        if enabled == "false":
            return cls()
        return cls(enabled=True, uri=_setting(env, "NEO4J_URI"),
                   username=_setting(env, "NEO4J_USERNAME"),
                   password=_setting(env, "NEO4J_PASSWORD", strip=False),
                   database=_setting(env, "NEO4J_DATABASE", "neo4j"))


_CONSTRAINTS = (
    "CREATE CONSTRAINT proofrun_deployment_id IF NOT EXISTS FOR (n:ProofRunDeployment) REQUIRE n.deployment_id IS UNIQUE",
    "CREATE CONSTRAINT proofrun_revision_key IF NOT EXISTS FOR (n:ProofRunRevision) REQUIRE n.revision_key IS UNIQUE",
    "CREATE CONSTRAINT proofrun_contract_id IF NOT EXISTS FOR (n:ProofRunContract) REQUIRE n.contract_id IS UNIQUE",
    "CREATE CONSTRAINT proofrun_contract_version_key IF NOT EXISTS FOR (n:ProofRunContractVersion) REQUIRE n.version_key IS UNIQUE",
    "CREATE CONSTRAINT proofrun_case_id IF NOT EXISTS FOR (n:ProofRunCase) REQUIRE n.case_id IS UNIQUE",
    "CREATE CONSTRAINT proofrun_run_id IF NOT EXISTS FOR (n:ProofRunRun) REQUIRE n.run_id IS UNIQUE",
)

_PUT_DEPLOYMENT = """
MERGE (d:ProofRunDeployment {deployment_id: $deployment_id})
SET d.revision = $revision, d.source_sha256 = $source_sha256
WITH d
OPTIONAL MATCH (d)-[old:DEPENDS_ON|CURRENT_REVISION]->()
DELETE old
WITH DISTINCT d
MERGE (r:ProofRunRevision {revision_key: $revision_key})
ON CREATE SET r.revision = $revision, r.source_sha256 = $source_sha256
MERGE (d)-[:CURRENT_REVISION]->(r)
WITH d
UNWIND $contracts AS dependency
MERGE (c:ProofRunContract {contract_id: dependency.contract_id})
MERGE (v:ProofRunContractVersion {version_key: dependency.version_key})
ON CREATE SET v.contract_id = dependency.contract_id, v.contract_sha256 = dependency.contract_sha256
MERGE (c)-[:HAS_VERSION]->(v)
MERGE (k:ProofRunCase {case_id: dependency.case_id})
MERGE (v)-[:HAS_CASE]->(k)
MERGE (d)-[:DEPENDS_ON {case_id: dependency.case_id}]->(v)
RETURN DISTINCT d.deployment_id AS deployment_id
"""

_DEPLOYMENT_PROJECTION = """
MATCH (d)-[dependency:DEPENDS_ON]->(v:ProofRunContractVersion)
WITH d, dependency, v ORDER BY v.contract_id, dependency.case_id
RETURN d.deployment_id AS deployment_id, d.revision AS revision,
       d.source_sha256 AS source_sha256,
       collect({contract_id: v.contract_id, contract_sha256: v.contract_sha256,
                case_id: dependency.case_id}) AS contracts
ORDER BY deployment_id
"""

_GET_DEPLOYMENT = "MATCH (d:ProofRunDeployment {deployment_id: $deployment_id})\n" + _DEPLOYMENT_PROJECTION
_SELECT_DEPLOYMENTS = """
MATCH (changed:ProofRunContractVersion {version_key: $version_key})<-[:DEPENDS_ON]-(d:ProofRunDeployment)
WITH DISTINCT d
""" + _DEPLOYMENT_PROJECTION

_RECORD_RUN = """
MATCH (d:ProofRunDeployment {deployment_id: $deployment_id})
MERGE (run:ProofRunRun {run_id: $run_id})
ON CREATE SET run += $properties
WITH d, run WHERE run.fingerprint = $fingerprint
MERGE (r:ProofRunRevision {revision_key: $revision_key})
ON CREATE SET r.revision = $revision, r.source_sha256 = $source_sha256
MERGE (c:ProofRunContract {contract_id: $contract_id})
MERGE (v:ProofRunContractVersion {version_key: $version_key})
ON CREATE SET v.contract_id = $contract_id, v.contract_sha256 = $contract_sha256
MERGE (k:ProofRunCase {case_id: $case_id})
MERGE (c)-[:HAS_VERSION]->(v)
MERGE (v)-[:HAS_CASE]->(k)
MERGE (d)-[:HAS_RUN]->(run)
MERGE (run)-[:EXECUTED_REVISION]->(r)
MERGE (run)-[:EVALUATED_CONTRACT]->(v)
MERGE (run)-[:EXECUTED_CASE]->(k)
RETURN run.run_id AS run_id
"""


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _revision_key(deployment):
    return _fingerprint({key: deployment[key] for key in ("revision", "source_sha256")})


def _version_key(contract):
    return contract["contract_id"] + ":" + contract["contract_sha256"]


_BINDING_HASHES = ("source_sha256", "contract_sha256", "bundle_sha256", "candidate_sha256",
                   "tests_sha256", "verifier_sha256", "environment_manifest_sha256")


def _run_properties(deployment, contract, run):
    if not isinstance(run, dict):
        raise ValueError("A measured run must be an object.")
    properties = {"run_id": validate_id(run.get("run_id"), "run_id"),
                  "case_id": validate_id(run.get("case_id"), "case_id"),
                  "contract_id": contract["contract_id"]}
    if properties["case_id"] != contract["case_id"]:
        raise ValueError("Measured run case does not match the selected contract.")
    statuses = {
        "execution_status": {"completed", "setup_failed", "timed_out", "interrupted"},
        "finding_status": {"not_tested", "regression_reproduced", "no_difference_observed", "inconclusive"},
        "repair_status": {"not_requested", "pending", "proposed", "verified", "rejected", "unavailable"},
    }
    for key, permitted in statuses.items():
        value = run.get(key)
        if not isinstance(value, str) or value not in permitted:
            raise ValueError("Measured run must have valid terminal execution and evidence statuses.")
        properties[key] = value
    bindings = run.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError("Measured run requires explicit evidence bindings.")
    for key, expected in {"revision": deployment["revision"], "source_sha256": deployment["source_sha256"],
                          "contract_sha256": contract["contract_sha256"]}.items():
        if bindings.get(key) != expected:
            raise ValueError("Measured run bindings do not match the deployment and contract.")
    properties["revision"] = bindings["revision"]
    for key in _BINDING_HASHES:
        if bindings.get(key) is not None:
            properties[key] = _hash(bindings[key], key)
    execution = run.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("Measured run execution identity must be an object.")
    for key in ("target", "worker_id", "runner", "data", "measurement"):
        if key in execution:
            properties["execution_" + key] = validate_id(execution[key], "execution identity")
    if "worker_revision" in execution:
        properties["execution_worker_revision"] = _revision(execution["worker_revision"])
    if "worker_code_sha256" in execution:
        hashes = execution["worker_code_sha256"]
        if not isinstance(hashes, dict) or len(hashes) > 100:
            raise ValueError("Measured worker code hashes must be a bounded object.")
        for name, digest in hashes.items():
            if not isinstance(name, str) or not _CHECK_ID.fullmatch(name):
                raise ValueError("Measured worker code paths must be safe identifiers.")
            _hash(digest, "worker code hash")
        properties["execution_worker_code_sha256"] = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    for key in ("expected_case_ids", "executed_case_ids"):
        if key in run:
            values = run[key]
            if (not isinstance(values, list) or len(values) > 500
                    or any(not isinstance(value, str) or not _CHECK_ID.fullmatch(value) for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError("Measured check IDs must be bounded unique safe identifiers.")
            properties[key] = sorted(values)
    properties["fingerprint"] = _fingerprint(properties)
    return properties


class Neo4jStore:
    def __init__(self, config: GraphConfig, driver=None):
        self.config = config
        self._driver = driver
        self._closed = False
        self._mutex = threading.RLock()
        self._bookmarks = None
        if not config.enabled:
            raise GraphUnavailable("Neo4j is disabled; enable it explicitly in server configuration.")
        if driver is None:
            try:
                from neo4j import GraphDatabase
            except ImportError:
                raise GraphUnavailable("Install requirements-neo4j.txt to enable Neo4j.") from None
            try:
                self._driver = GraphDatabase.driver(
                    config.uri, auth=(config.username, config.password),
                    connection_timeout=min(5.0, float(config.timeout_seconds)),
                    connection_acquisition_timeout=float(config.timeout_seconds),
                    connection_write_timeout=float(config.timeout_seconds),
                    max_transaction_retry_time=0.0, max_connection_pool_size=4,
                    telemetry_disabled=True,
                )
            except Exception:
                raise GraphUnavailable("Neo4j connection could not be configured.") from None

    def _execute(self, query, parameters=None, *, read=False):
        # Propagate committed bookmarks so routed replicas cannot silently
        # select an older deployment registration after a successful write.
        with self._mutex:
            if self._closed:
                raise GraphUnavailable("Neo4j connection is closed.")
            try:
                with self._driver.session(database=self.config.database, bookmarks=self._bookmarks,
                                          default_access_mode="READ" if read else "WRITE") as session:
                    with session.begin_transaction(timeout=self.config.timeout_seconds) as transaction:
                        records = [dict(record) for record in transaction.run(query, parameters or {})]
                        transaction.commit()
                    self._bookmarks = session.last_bookmarks()
                    return records
            except Exception:
                raise GraphUnavailable("Neo4j operation failed; check database availability and access.") from None

    def initialize(self):
        for query in _CONSTRAINTS:
            self._execute(query)

    def close(self):
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            try:
                self._driver.close()
            except Exception:
                raise GraphUnavailable("Neo4j connection could not be closed cleanly.") from None

    def put_deployment(self, deployment: dict) -> dict:
        deployment = validate_deployment(deployment)
        parameters = {**deployment, "revision_key": _revision_key(deployment),
                      "contracts": [{**item, "version_key": _version_key(item)} for item in deployment["contracts"]]}
        records = self._execute(_PUT_DEPLOYMENT, parameters)
        if records != [{"deployment_id": deployment["deployment_id"]}]:
            raise GraphUnavailable("Neo4j returned an invalid deployment registration result.")
        return deployment

    @staticmethod
    def _deployments(records):
        try:
            deployments = [validate_deployment(record) for record in records]
            if len({item["deployment_id"] for item in deployments}) != len(deployments):
                raise ValueError()
            return sorted(deployments, key=lambda item: item["deployment_id"])
        except (ValueError, TypeError, KeyError):
            raise GraphUnavailable("Neo4j returned invalid deployment records.") from None

    def get_deployment(self, deployment_id) -> dict | None:
        deployment_id = validate_id(deployment_id, "deployment_id")
        records = self._deployments(self._execute(_GET_DEPLOYMENT, {"deployment_id": deployment_id}, read=True))
        if len(records) > 1 or (records and records[0]["deployment_id"] != deployment_id):
            raise GraphUnavailable("Neo4j returned an invalid deployment identity.")
        return records[0] if records else None

    def select_deployments(self, contract_id, previous_sha256) -> list[dict]:
        contract_id = validate_id(contract_id, "contract_id")
        previous_sha256 = _hash(previous_sha256, "previous_sha256")
        records = self._deployments(self._execute(
            _SELECT_DEPLOYMENTS, {"version_key": contract_id + ":" + previous_sha256}, read=True))
        if any(not any(item["contract_id"] == contract_id and item["contract_sha256"] == previous_sha256
                       for item in deployment["contracts"]) for deployment in records):
            raise GraphUnavailable("Neo4j returned a deployment outside the selected dependency scope.")
        return records

    def record_run(self, deployment: dict, contract: dict, run: dict) -> None:
        deployment = validate_deployment(deployment)
        contract = validate_contract(contract)
        properties = _run_properties(deployment, contract, run)
        parameters = {"deployment_id": deployment["deployment_id"], "revision": deployment["revision"],
                      "source_sha256": deployment["source_sha256"], "revision_key": _revision_key(deployment),
                      **contract, "version_key": _version_key(contract), "run_id": properties["run_id"],
                      "properties": properties, "fingerprint": properties["fingerprint"]}
        records = self._execute(_RECORD_RUN, parameters)
        if records != [{"run_id": properties["run_id"]}]:
            raise GraphConflict("Measured run conflicts with existing evidence or the deployment is not registered.")
