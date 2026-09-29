"""Offline driver-boundary checks; these mocks do not prove a live integration."""
import copy
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.proofrun.neo4j_store import (
    GraphConfig, GraphConfigurationError, GraphConflict, GraphUnavailable,
    Neo4jStore, validate_contract, validate_deployment, validate_id,
)


SECRET = "synthetic-neo4j-password"
CONTRACT = {"contract_id": "customer-import", "contract_sha256": "c" * 64, "case_id": "customer-case"}
DEPLOYMENT = {"deployment_id": "demo-east", "revision": "a" * 40,
              "source_sha256": "b" * 64, "contracts": [CONTRACT]}
RUN = {
    "run_id": "run-123", "case_id": "customer-case", "execution_status": "completed",
    "finding_status": "regression_reproduced", "repair_status": "not_requested",
    "bindings": {"revision": "a" * 40, "source_sha256": "b" * 64,
                 "contract_sha256": "c" * 64, "tests_sha256": "d" * 64,
                 "verifier_sha256": "e" * 64, "environment_manifest_sha256": "f" * 64},
    "execution": {"target": "local", "runner": "native", "worker_id": "local-worker",
                  "data": "synthetic", "measurement": "fresh", "worker_revision": "a" * 40,
                  "worker_code_sha256": {"src/proofrun/runner.py": "e" * 64}},
    "expected_case_ids": ["updated:nickname_omitted", "baseline:nickname_omitted"],
    "executed_case_ids": ["baseline:nickname_omitted", "updated:nickname_omitted"],
}


def config(**kwargs):
    return GraphConfig(enabled=True, uri="bolt://localhost:17687", username="neo4j",
                       password=SECRET, **kwargs)


class Driver:
    def __init__(self, responses=(), error=None, commit_error=None):
        self.responses = list(responses)
        self.error = error
        self.commit_error = commit_error
        self.calls = []
        self.sessions = []
        self.timeouts = []
        self.commits = 0
        self.closed = 0

    def session(self, **kwargs):
        self.sessions.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def begin_transaction(self, **kwargs):
        self.timeouts.append(kwargs)
        return self

    def run(self, query, parameters):
        self.calls.append((query, parameters))
        if self.error:
            raise self.error
        return self.responses.pop(0) if self.responses else []

    def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.commits += 1

    def close(self):
        self.closed += 1

    def last_bookmarks(self):
        return ("transaction-" + str(self.commits),)


class GraphConfigTests(unittest.TestCase):
    def test_disabled_by_default_and_ignores_unused_secrets(self):
        self.assertEqual(GraphConfig.from_env({}), GraphConfig())
        self.assertFalse(GraphConfig.from_env({"NEO4J_URI": object(), "NEO4J_PASSWORD": "bad\nvalue"}).enabled)
        with self.assertRaises(GraphUnavailable):
            Neo4jStore(GraphConfig(), driver=Driver())

    def test_explicit_enabled_configuration_and_redacted_repr(self):
        env = {"PROOFRUN_NEO4J_ENABLED": "TRUE", "NEO4J_URI": "neo4j+s://example.databases.neo4j.io",
               "NEO4J_USERNAME": "private-user", "NEO4J_PASSWORD": SECRET}
        configured = GraphConfig.from_env(env)
        self.assertTrue(configured.enabled)
        self.assertEqual(configured.database, "neo4j")
        self.assertEqual(configured.password, SECRET)
        for secret in (SECRET, "private-user", "example.databases.neo4j.io"):
            self.assertNotIn(secret, repr(configured))

    def test_enabled_requires_all_connection_settings_without_echoing_values(self):
        for setting in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
            env = {"PROOFRUN_NEO4J_ENABLED": "true", "NEO4J_URI": "bolt://localhost:7687",
                   "NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": SECRET}
            env.pop(setting)
            with self.subTest(setting=setting), self.assertRaises(GraphConfigurationError) as caught:
                GraphConfig.from_env(env)
            self.assertIn(setting, str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))

    def test_enable_flag_is_strict(self):
        for value in ("yes", "1", "", True, "true\n"):
            with self.subTest(value=value), self.assertRaises(GraphConfigurationError):
                GraphConfig.from_env({"PROOFRUN_NEO4J_ENABLED": value})

    def test_connection_uri_rejects_credentials_and_invalid_schemes(self):
        for uri in ("https://localhost", "bolt://", "bolt://user:secret@localhost", "bolt://localhost/path",
                    "bolt://localhost?key=secret", "bolt://localhost#secret", "bolt://localhost:abc",
                    "bolt://localhost:0", "bolt://localhost:65536", "bolt://local host", "bolt://[invalid"):
            with self.subTest(uri=uri), self.assertRaises(GraphConfigurationError) as caught:
                GraphConfig(enabled=True, uri=uri, username="neo4j", password=SECRET)
            self.assertNotIn(uri, str(caught.exception))
        for scheme in ("bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"):
            self.assertTrue(GraphConfig(enabled=True, uri=scheme + "://localhost:7687",
                                        username="neo4j", password=SECRET).enabled)

    def test_config_bounds_and_no_secret_in_errors(self):
        for changes in ({"timeout_seconds": 0}, {"timeout_seconds": 31}, {"timeout_seconds": True},
                        {"database": ""}, {"database": "neo4j; DROP"}):
            with self.subTest(changes=changes), self.assertRaises(GraphConfigurationError):
                config(**changes)
        with self.assertRaises(GraphConfigurationError) as caught:
            GraphConfig(enabled=True, uri="bolt://localhost", username="neo4j", password=SECRET + "\n")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_password_is_not_silently_trimmed(self):
        env = {"PROOFRUN_NEO4J_ENABLED": "true", "NEO4J_URI": "bolt://localhost",
               "NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": " " + SECRET + " "}
        self.assertEqual(GraphConfig.from_env(env).password, " " + SECRET + " ")


class GraphBoundaryTests(unittest.TestCase):
    def test_deployment_validation_is_bounded_strict_and_returns_sorted_copy(self):
        original = copy.deepcopy(DEPLOYMENT)
        original["contracts"].insert(0, {**CONTRACT, "contract_id": "z-customer"})
        validated = validate_deployment(original)
        self.assertEqual([c["contract_id"] for c in validated["contracts"]], ["customer-import", "z-customer"])
        self.assertEqual(original["contracts"][0]["contract_id"], "z-customer")
        validated["contracts"][0]["case_id"] = "other"
        self.assertEqual(original["contracts"][1]["case_id"], "customer-case")
        for updates in ({"revision": "HEAD"}, {"revision": "A" * 40}, {"source_sha256": "b" * 63},
                        {"deployment_id": "bad' OR 1=1"}, {"contracts": []}, {"contracts": [CONTRACT] * 21},
                        {"contracts": [CONTRACT, CONTRACT]}, {"extra": "ignored?"},
                        {"contracts": [{**CONTRACT, "secret": SECRET}]}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_deployment({**DEPLOYMENT, **updates})

    def test_one_contract_cannot_have_multiple_cases_or_conflicting_versions(self):
        deployment = {**DEPLOYMENT, "contracts": [CONTRACT, {**CONTRACT, "case_id": "other-case"}]}
        with self.assertRaises(ValueError):
            validate_deployment(deployment)
        deployment["contracts"][1]["contract_sha256"] = "d" * 64
        with self.assertRaises(ValueError):
            validate_deployment(deployment)

    def test_invalid_inputs_fail_before_database_access(self):
        driver = Driver()
        store = Neo4jStore(config(), driver=driver)
        for operation in (lambda: store.put_deployment({}), lambda: store.get_deployment("bad id"),
                          lambda: store.select_deployments("customer-import", "invalid"),
                          lambda: store.record_run(DEPLOYMENT, CONTRACT, {}),
                          lambda: validate_contract({}), lambda: validate_id(None)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(driver.calls, [])

    def test_optional_driver_loaded_only_on_real_connection_and_configured_with_bounds(self):
        constructor = Mock(return_value=Driver())
        with patch.dict(sys.modules, {"neo4j": SimpleNamespace(GraphDatabase=SimpleNamespace(driver=constructor))}):
            Neo4jStore(config())
        args, kwargs = constructor.call_args
        self.assertEqual(args, ("bolt://localhost:17687",))
        self.assertEqual(kwargs["auth"], ("neo4j", SECRET))
        self.assertEqual(kwargs["connection_timeout"], 5.0)
        self.assertEqual(kwargs["connection_acquisition_timeout"], 10.0)
        self.assertEqual(kwargs["connection_write_timeout"], 10.0)
        self.assertEqual(kwargs["max_transaction_retry_time"], 0.0)
        self.assertEqual(kwargs["max_connection_pool_size"], 4)

    def test_missing_or_failing_driver_raises_safe_unavailable(self):
        with patch.dict(sys.modules, {"neo4j": None}), self.assertRaises(GraphUnavailable) as caught:
            Neo4jStore(config())
        self.assertIn("requirements-neo4j.txt", str(caught.exception))
        constructor = Mock(side_effect=RuntimeError(SECRET))
        with patch.dict(sys.modules, {"neo4j": SimpleNamespace(GraphDatabase=SimpleNamespace(driver=constructor))}):
            with self.assertRaises(GraphUnavailable) as caught:
                Neo4jStore(config())
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_initialize_uses_only_prefixed_unique_constraints_and_bounded_transactions(self):
        driver = Driver()
        store = Neo4jStore(config(database="proofrun"), driver=driver)
        store.initialize()
        self.assertEqual(len(driver.calls), 6)
        self.assertEqual(driver.commits, 6)
        for query, parameters in driver.calls:
            self.assertIn("CREATE CONSTRAINT proofrun_", query)
            self.assertIn(":ProofRun", query)
            self.assertIn("IF NOT EXISTS", query)
            self.assertIn("IS UNIQUE", query)
            self.assertEqual(parameters, {})
        for session in driver.sessions:
            self.assertEqual(session["database"], "proofrun")
            self.assertEqual(session["default_access_mode"], "WRITE")
        self.assertEqual(driver.timeouts, [{"timeout": 10}] * 6)

    def test_committed_bookmarks_propagate_to_routed_reads(self):
        driver = Driver(responses=[[{"deployment_id": "demo-east"}], [DEPLOYMENT]])
        store = Neo4jStore(config(), driver=driver)
        store.put_deployment(DEPLOYMENT)
        store.get_deployment("demo-east")
        self.assertIsNone(driver.sessions[0]["bookmarks"])
        self.assertEqual(driver.sessions[1]["bookmarks"], ("transaction-1",))

    def test_deployment_registration_replaces_current_edges_in_one_parameterized_transaction(self):
        driver = Driver(responses=[[{"deployment_id": "demo-east"}]])
        store = Neo4jStore(config(), driver=driver)
        self.assertEqual(store.put_deployment(DEPLOYMENT), DEPLOYMENT)
        self.assertEqual(len(driver.calls), 1)
        query, parameters = driver.calls[0]
        self.assertNotIn("demo-east", query)
        self.assertIn("DELETE old", query)
        self.assertIn("old:DEPENDS_ON|CURRENT_REVISION", query)
        self.assertNotIn("DETACH DELETE", query)
        self.assertNotIn("HAS_RUN", query)
        self.assertEqual(parameters["deployment_id"], "demo-east")
        self.assertEqual(parameters["contracts"][0]["version_key"], "customer-import:" + "c" * 64)
        self.assertEqual(len(parameters["revision_key"]), 64)
        self.assertEqual(driver.commits, 1)

    def test_get_returns_current_deployment_or_none(self):
        driver = Driver(responses=[[copy.deepcopy(DEPLOYMENT)], []])
        store = Neo4jStore(config(), driver=driver)
        self.assertEqual(store.get_deployment("demo-east"), DEPLOYMENT)
        self.assertIsNone(store.get_deployment("missing"))
        self.assertEqual(driver.sessions[0]["default_access_mode"], "READ")

    def test_selection_queries_current_dependencies_and_exact_version(self):
        west = {**DEPLOYMENT, "deployment_id": "demo-west"}
        driver = Driver(responses=[[west, copy.deepcopy(DEPLOYMENT)]])
        store = Neo4jStore(config(), driver=driver)
        result = store.select_deployments("customer-import", "c" * 64)
        self.assertEqual([d["deployment_id"] for d in result], ["demo-east", "demo-west"])
        query, parameters = driver.calls[0]
        self.assertIn("<-[:DEPENDS_ON]-(d:ProofRunDeployment)", query)
        self.assertNotIn("HAS_RUN", query)
        self.assertEqual(parameters, {"version_key": "customer-import:" + "c" * 64})

    def test_invalid_or_wrong_scope_database_records_fail_closed(self):
        for records in ([{"deployment_id": "demo-east"}], [DEPLOYMENT, DEPLOYMENT],
                        [{**DEPLOYMENT, "deployment_id": "other"}]):
            store = Neo4jStore(config(), driver=Driver(responses=[records]))
            with self.assertRaises(GraphUnavailable):
                store.get_deployment("demo-east")
        store = Neo4jStore(config(), driver=Driver(responses=[[DEPLOYMENT]]))
        with self.assertRaises(GraphUnavailable):
            store.select_deployments("unrelated-contract", "c" * 64)

    def test_record_run_preserves_only_bound_safe_fields_and_never_clears_pending_work(self):
        driver = Driver(responses=[[{"run_id": "run-123"}]])
        store = Neo4jStore(config(), driver=driver)
        run = copy.deepcopy(RUN)
        run.update(error=SECRET, proposal={"rationale": SECRET}, artifacts=[{"path": SECRET}])
        run["bindings"]["raw_error"] = SECRET
        run["execution"]["credential"] = SECRET
        store.record_run(DEPLOYMENT, CONTRACT, run)
        query, parameters = driver.calls[0]
        self.assertNotIn(SECRET, json.dumps(parameters))
        self.assertIn("ON CREATE SET run += $properties", query)
        self.assertIn("WHERE run.fingerprint = $fingerprint", query)
        self.assertNotIn("DELETE", query)
        self.assertNotIn("DEPENDS_ON", query)
        properties = parameters["properties"]
        self.assertEqual(properties["source_sha256"], DEPLOYMENT["source_sha256"])
        self.assertEqual(properties["contract_sha256"], CONTRACT["contract_sha256"])
        self.assertEqual(properties["execution_measurement"], "fresh")
        self.assertEqual(properties["executed_case_ids"], sorted(RUN["executed_case_ids"]))
        self.assertEqual(json.loads(properties["execution_worker_code_sha256"]), RUN["execution"]["worker_code_sha256"])

    def test_run_for_changed_contract_is_linked_to_new_version_without_changing_deployment(self):
        driver = Driver(responses=[[{"run_id": "run-123"}]])
        store = Neo4jStore(config(), driver=driver)
        changed_contract = {**CONTRACT, "contract_sha256": "9" * 64}
        run = copy.deepcopy(RUN)
        run["bindings"]["contract_sha256"] = changed_contract["contract_sha256"]
        store.record_run(DEPLOYMENT, changed_contract, run)
        self.assertEqual(driver.calls[0][1]["version_key"], "customer-import:" + "9" * 64)

    def test_run_evidence_fingerprint_stable_and_sensitive_to_binding_or_verdict(self):
        driver = Driver(responses=[[{"run_id": "run-123"}]] * 3)
        store = Neo4jStore(config(), driver=driver)
        store.record_run(DEPLOYMENT, CONTRACT, RUN)
        ordered = copy.deepcopy(RUN)
        ordered["executed_case_ids"].reverse()
        store.record_run(DEPLOYMENT, CONTRACT, ordered)
        changed = {**RUN, "repair_status": "rejected"}
        store.record_run(DEPLOYMENT, CONTRACT, changed)
        fingerprints = [parameters["fingerprint"] for _, parameters in driver.calls]
        self.assertEqual(fingerprints[0], fingerprints[1])
        self.assertNotEqual(fingerprints[0], fingerprints[2])

    def test_run_conflict_is_visible_without_overwrite(self):
        store = Neo4jStore(config(), driver=Driver(responses=[[]]))
        with self.assertRaises(GraphConflict):
            store.record_run(DEPLOYMENT, CONTRACT, RUN)

    def test_run_validation_rejects_unbound_nonterminal_or_unsafe_evidence(self):
        changes = [{"case_id": "other"}, {"execution_status": "running"}, {"repair_status": "PASS"},
                   {"bindings": {**RUN["bindings"], "source_sha256": "9" * 64}},
                   {"bindings": {**RUN["bindings"], "contract_sha256": "9" * 64}},
                   {"bindings": {**RUN["bindings"], "tests_sha256": "invalid"}},
                   {"execution": {"worker_id": SECRET + "\n"}},
                   {"executed_case_ids": ["duplicate", "duplicate"]},
                   {"expected_case_ids": ["bad\nid"]}]
        driver = Driver()
        store = Neo4jStore(config(), driver=driver)
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                store.record_run(DEPLOYMENT, CONTRACT, {**RUN, **change})
        self.assertEqual(driver.calls, [])

    def test_driver_and_commit_failures_are_redacted_without_memory_fallback(self):
        for driver in (Driver(error=RuntimeError(SECRET)), Driver(commit_error=RuntimeError(SECRET))):
            store = Neo4jStore(config(), driver=driver)
            with self.assertRaises(GraphUnavailable) as caught:
                store.get_deployment("demo-east")
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_close_is_idempotent_and_closed_store_fails(self):
        driver = Driver()
        store = Neo4jStore(config(), driver=driver)
        store.close()
        store.close()
        self.assertEqual(driver.closed, 1)
        with self.assertRaises(GraphUnavailable):
            store.get_deployment("demo-east")


if __name__ == "__main__":
    unittest.main()
