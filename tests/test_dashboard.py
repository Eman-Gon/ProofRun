"""Dashboard tests use stored-shaped reports and stubbed runners, never Docker."""

from copy import deepcopy
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.dashboard import Dashboard, INTERRUPT_GRACE, KILL_GRACE, OUTPUT_LIMIT, make_server
from src.public_repo import PublicRepoError


def gpu_report():
    results = []
    for version in ("2.3.3", "3.0.0"):
        for variant in ("original", "fixed"):
            failure = version == "3.0.0" and variant == "original"
            results.append({"pandas": version, "variant": variant, "exit_code": int(failure),
                            "duration_seconds": 0.5, "image_id": "sha256:test",
                            "output": "ValueError: Invalid frequency: H" if failure else "Ran 2 tests\nOK"})
    return {"repository": "Eman-Gon/gpu-energy-recommender", "commit": "7a802eace3a3775acc5ea4af6c266079e2599eb3",
            "source_path": "eda/data_collection.py", "results": results}


def fixture_report():
    results = {}
    for kind in ("existing", "probe", "fixed"):
        for suffix, version in (("old", "1.10.18"), ("new", "2.8.2")):
            failure = kind == "probe" and suffix == "new"
            output = "ValidationError: nickname Field required" if failure else "OK"
            results[kind + "_" + suffix] = {"status": "fail" if failure else "pass", "exit_code": int(failure),
                                          "output_tail": output + "\nSECONDLOOK_DEPENDENCY_VERSION=" + version,
                                          "duration_seconds": 0.2}
    return {"status": "confirmed_break", "checked_at": "2026-09-22T01:42:02+00:00",
            "upgrade": {"ecosystem": "pypi", "package": "pydantic", "before": "==1.10.18", "version": "==2.8.2"},
            "source": {"provenance": "curated source note; no live lookup"},
            "probe_provenance": "prepared probe data (--offline)", "results": results}


def cloud_memory():
    evidence = {"project": "secondlook/upgrade-demo", "status": "confirmed_break", "finding": "Omitted nickname changes behavior"}
    body = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    dataset_id = "be83bd3e-8bf0-4505-9c52-4ee1087d0e83"
    return {"status": "stored_and_retrieved", "backend": "cloud", "dataset": "secondlook_compatibility_" + "c" * 32,
            "dataset_id": dataset_id, "evidence": evidence, "evidence_sha256": digest,
            "retrieved_excerpt": f"SECONDLOOK_VERIFIED_EVIDENCE_V1 {digest}\n{body}\nSECONDLOOK_VERIFIED_EVIDENCE_END {digest}",
            "graph_summary": {"dataset_id": dataset_id, "num_nodes": 22, "num_edges": 29}}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gpu = self.root / ".commit-watch/repo-audit/gpu-energy-pandas"
        self.demo = self.root / ".commit-watch/upgrade-demo/20260922T010000Z-test"
        for directory in (self.gpu, self.demo, self.root / "src", self.root / "demo/upgrade", self.root / "ui"):
            directory.mkdir(parents=True)
        for filename in ("reproduce.py", "data_collection.py", "fixed_data_collection.py", "test_collection.py"):
            (self.gpu / filename).write_text("# fixture, never executed\n")
        for filename in ("app.py", "fixed_app.py", "requirements-old.txt", "requirements-new.txt"):
            (self.root / "demo/upgrade" / filename).write_text("# fixture\n")
        (self.root / "src/main.py").write_text("# fixture\n")
        (self.root / "ui/index.html").write_text("<html>Secondlook</html>")
        (self.root / "ui/styles.css").write_text("body {}")
        (self.root / "ui/app.js").write_text("'use strict';")
        self.manager = Dashboard(self.root)
        self.addCleanup(self.manager.close)
        self.docker = patch("src.dashboard.shutil.which", return_value="/usr/local/bin/docker")
        self.docker.start()
        self.addCleanup(self.docker.stop)

    def write_reports(self):
        (self.gpu / "results.json").write_text(json.dumps(gpu_report()))
        (self.demo / "report.json").write_text(json.dumps(fixture_report()))
        (self.demo / "suggested-fix.patch").write_text("-nickname: Optional[str]\n+nickname: Optional[str] = None\n")

    def test_missing_evidence_has_no_passing_cells(self):
        for case in self.manager.state()["cases"]:
            self.assertEqual(case["status"], "not_run")
            self.assertIsNone(case["checkedAt"])
            self.assertTrue(case["canRun"])
            self.assertTrue(all(row["before"] is None and row["after"] is None for row in case["checks"]))

    def test_real_report_shapes_normalize_to_frontend_contract(self):
        self.write_reports()
        gpu, demo = self.manager.state()["cases"]
        expected_fields = {"id", "repository", "title", "kind", "package", "fromVersion", "toVersion", "status", "summary",
                           "sourceUrl", "repoUrl", "commit", "filePath", "lineNumbers", "beforeCode", "afterCode",
                           "explanation", "scope", "provenance", "checkedAt", "checks", "patch", "canRun", "unavailableReason",
                           "liveSupported", "integrations"}
        for case in (gpu, demo):
            self.assertEqual(set(case), expected_fields)
            self.assertEqual(case["status"], "confirmed_break")
            self.assertIsNotNone(case["checkedAt"])
        self.assertEqual(gpu["lineNumbers"], [22, 63])
        self.assertEqual(gpu["checks"][0]["after"]["status"], "fail")
        self.assertEqual(demo["checks"][0]["after"]["status"], "pass")
        self.assertEqual(demo["checks"][1]["after"]["status"], "fail")
        self.assertEqual(demo["checks"][2]["after"]["status"], "pass")
        self.assertIn("no live lookup", demo["provenance"])
        self.assertIsNone(demo["repoUrl"])
        self.assertIn("= None", demo["patch"])

    def test_wrong_version_generic_failure_duplicate_or_missing_evidence_cannot_confirm(self):
        base = gpu_report()
        broken = []
        item = deepcopy(base)
        item["results"][2]["output"] = "Docker unavailable"
        broken.append(item)
        item = deepcopy(base)
        item["results"].append(item["results"][0])
        broken.append(item)
        item = deepcopy(base)
        item["results"].pop()
        broken.append(item)
        item = deepcopy(base)
        item["commit"] = "different"
        broken.append(item)
        for report in broken:
            with self.subTest(report=report):
                (self.gpu / "results.json").write_text(json.dumps(report))
                self.assertEqual(self.manager.case("gpu-energy-pandas")["status"], "inconclusive")
        report = fixture_report()
        report["results"]["probe_new"]["output_tail"] = "ValidationError nickname Field required\nSECONDLOOK_DEPENDENCY_VERSION=1.10.18"
        (self.demo / "report.json").write_text(json.dumps(report))
        self.assertEqual(self.manager.case("pydantic")["status"], "inconclusive")
        report = fixture_report()
        report["status"] = "inconclusive"
        (self.demo / "report.json").write_text(json.dumps(report))
        self.assertEqual(self.manager.case("pydantic")["status"], "inconclusive")

    def test_newest_invalid_report_is_not_replaced_by_older_success(self):
        self.write_reports()
        newer = self.demo.parent / "20260922T020000Z-test"
        newer.mkdir()
        (newer / "report.json").write_text("{incomplete")
        case = self.manager.case("pydantic")
        self.assertEqual(case["status"], "inconclusive")
        self.assertIsNone(case["checks"][0]["before"])

    def test_malformed_cell_status_and_numeric_duration_do_not_break_state(self):
        for status in ([], {}, True, 1, None):
            with self.subTest(status=status):
                report = fixture_report()
                report["results"]["existing_old"]["status"] = status
                (self.demo / "report.json").write_text(json.dumps(report))
                case = self.manager.state()["cases"][1]
                self.assertEqual(case["status"], "inconclusive")
                self.assertEqual(case["checks"][0]["before"]["status"], "error")
        for duration in (10 ** 400, [], {}, True, "0.5", float("inf"), float("nan"), -1):
            with self.subTest(duration=duration):
                for case_id, directory, filename, report in (
                    ("gpu-energy-pandas", self.gpu, "results.json", gpu_report()),
                    ("pydantic", self.demo, "report.json", fixture_report()),
                ):
                    cell = report["results"][0] if case_id == "gpu-energy-pandas" else report["results"]["existing_old"]
                    cell["duration_seconds"] = duration
                    (directory / filename).write_text(json.dumps(report))
                    normalized = self.manager.case(case_id)
                    self.assertIsNone(normalized["checks"][0]["before"]["duration"])
                    json.dumps(normalized, allow_nan=False)

    def test_malformed_result_and_source_shapes_remain_serializable(self):
        for value in ([], "wrong", True, None, {"provenance": []}):
            with self.subTest(value=value):
                report = fixture_report()
                report["source"] = value
                report["probe_provenance"] = value
                report["checked_at"] = value
                report["results"]["existing_old"] = value
                (self.demo / "report.json").write_text(json.dumps(report))
                case = self.manager.state()["cases"][1]
                self.assertEqual(case["status"], "inconclusive")
                json.dumps(case, allow_nan=False)
        (self.demo / "report.json").write_text('{"results":' + '[' * 1500 + '0' + ']' * 1500 + '}')
        self.assertEqual(self.manager.case("pydantic")["status"], "inconclusive")

    def test_shutdown_wait_covers_interrupt_and_kill_cleanup_grace(self):
        from unittest.mock import Mock
        self.manager.worker = Mock()
        self.manager.close()
        self.assertTrue(self.manager.stopping.is_set())
        self.assertGreaterEqual(INTERRUPT_GRACE, 20)
        self.manager.worker.join.assert_called_once_with(timeout=INTERRUPT_GRACE + KILL_GRACE + 1)

    def test_unavailable_runner_cannot_start(self):
        (self.gpu / "reproduce.py").unlink()
        self.assertFalse(self.manager.case("gpu-energy-pandas")["canRun"])
        self.assertEqual(self.manager.start("gpu-energy-pandas")[0], 409)
        self.assertEqual(self.manager.start("arbitrary-command")[0], 400)

    def test_pydantic_exit_one_completes_only_with_a_fresh_confirmed_report(self):
        commands = []
        def run(command, emit):
            commands.append(command)
            emit("verified\n")
            (self.demo / "report.json").write_text(json.dumps(fixture_report()))
            return 1
        with patch.object(self.manager, "_execute", side_effect=run):
            code, response = self.manager.start("pydantic")
            self.assertEqual(code, 202)
            self.manager.worker.join(timeout=2)
        job = self.manager.state()["history"][0]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["exitCode"], 1)
        self.assertEqual(job["id"], response["run"]["id"])
        self.assertEqual(commands[0][1:], ["-m", "src.main", "upgrade-demo", "--offline", "--no-memory"])

    def test_live_evidence_requires_both_source_and_memory_retrieval(self):
        report = fixture_report()
        report["source"] = {"provider": "brightdata", "content_sha256": "a" * 64,
                            "source_url": "https://example.com/migration", "fetched_at": report["checked_at"]}
        report["memory_verification"] = {"status": "stored_and_retrieved", "dataset": "secondlook_compatibility_" + "b" * 32,
                                         "retrieved_excerpt": "Retrieved canonical evidence", "retrieved_at": report["checked_at"]}
        report["prior_memory"] = {"status": "recalled", "matches": [{"retrieved_excerpt": "Earlier verified finding"}]}
        commands = []
        def run(command, emit):
            commands.append(command)
            (self.demo / "report.json").write_text(json.dumps(report))
            return 1
        with patch.object(self.manager, "_execute", side_effect=run):
            self.assertEqual(self.manager.start("pydantic", "live")[0], 202)
            self.manager.worker.join(timeout=2)
        state = self.manager.state()
        self.assertEqual(state["history"][0]["status"], "completed")
        self.assertIn("--require-integrations", commands[0])
        services = state["cases"][1]["integrations"]
        self.assertEqual(services["brightData"]["status"], "verified")
        self.assertEqual(services["cognee"]["status"], "verified")
        self.assertEqual(services["priorMemory"]["status"], "recalled")
        report["memory_verification"] = {"status": "failed"}
        with patch.object(self.manager, "_execute", side_effect=run):
            self.assertEqual(self.manager.start("pydantic", "live")[0], 202)
            self.manager.worker.join(timeout=2)
        state = self.manager.state()
        self.assertEqual(state["history"][0]["status"], "failed")
        self.assertEqual(state["cases"][1]["status"], "confirmed_break")
        self.assertEqual(state["cases"][1]["integrations"]["cognee"]["status"], "failed")

    def test_offline_or_fallback_evidence_does_not_count_as_verified_integrations(self):
        self.write_reports()
        self.assertEqual(self.manager.case("pydantic")["integrations"]["brightData"]["status"], "not_run")
        report = fixture_report()
        report["source"] = {"provider": "direct_https"}
        report["memory"] = "secondlook_compatibility_" + "b" * 32
        (self.demo / "report.json").write_text(json.dumps(report))
        services = self.manager.case("pydantic")["integrations"]
        self.assertEqual(services["brightData"]["status"], "fallback")
        self.assertEqual(services["cognee"]["status"], "stored_only")
        self.assertEqual(self.manager.start("gpu-energy-pandas", "live")[0], 400)
        self.assertEqual(self.manager.start("pydantic", "arbitrary")[0], 400)

    def test_legacy_cognee_success_is_explicitly_local_and_has_no_cloud_link(self):
        report = fixture_report()
        report["memory_verification"] = {"status": "stored_and_retrieved", "dataset": "secondlook_compatibility_" + "b" * 32,
                                         "retrieved_excerpt": "Earlier verified local evidence"}
        report["prior_memory"] = {"status": "recalled", "matches": [{"retrieved_excerpt": "Earlier local finding"}]}
        (self.demo / "report.json").write_text(json.dumps(report))
        services = self.manager.case("pydantic")["integrations"]
        self.assertEqual(services["cognee"]["status"], "verified")
        for key in ("cognee", "priorMemory"):
            self.assertEqual(services[key]["backend"], "local")
            self.assertIn("Cognee local", services[key]["detail"])
            self.assertNotIn("Cloud", services[key]["detail"])
            self.assertIsNone(services[key]["graphUrl"])
            self.assertIsNone(services[key]["graphSummary"])
            self.assertIsNone(services[key]["datasetId"])

    def test_cloud_retrieval_exposes_matching_dataset_graph_counts_and_cloud_link(self):
        report = fixture_report()
        report["memory_verification"] = cloud_memory()
        report["prior_memory"] = {"status": "recalled", "backend": "cloud", "matches": [cloud_memory()]}
        (self.demo / "report.json").write_text(json.dumps(report))
        services = self.manager.case("pydantic")["integrations"]
        self.assertEqual(services["cognee"]["status"], "verified")
        self.assertEqual(services["priorMemory"]["status"], "recalled")
        for key in ("cognee", "priorMemory"):
            self.assertEqual(services[key]["backend"], "cloud")
            self.assertIn("Cognee Cloud", services[key]["detail"])
            self.assertEqual(services[key]["datasetId"], report["memory_verification"]["dataset_id"])
            self.assertEqual(services[key]["graphSummary"], {"datasetId": services[key]["datasetId"], "numNodes": 22, "numEdges": 29})
            self.assertEqual(services[key]["graphUrl"], "https://platform.cognee.ai/knowledge-graph")

    def test_cloud_requires_nonempty_matching_graph_and_matching_retrieval(self):
        invalid = []
        for key in ("num_nodes", "num_edges"):
            for count in (0, -1, True, "22", None):
                memory = cloud_memory()
                memory["graph_summary"][key] = count
                invalid.append(memory)
        for key, value in (("dataset_id", "wrong"), ("graph_summary", None), ("retrieved_excerpt", "unrelated result"),
                           ("evidence_sha256", "a" * 64), ("evidence", {"different": "finding"}), ("dataset", "other_dataset")):
            memory = cloud_memory()
            memory[key] = value
            invalid.append(memory)
        memory = cloud_memory()
        memory["graph_summary"]["dataset_id"] = "c2e18696-e4a3-4a49-a0fe-b2acd6cd8f6c"
        invalid.append(memory)
        for memory in invalid:
            with self.subTest(memory=memory):
                report = fixture_report()
                report["memory_verification"] = memory
                report["prior_memory"] = {"status": "recalled", "backend": "cloud", "matches": [memory]}
                (self.demo / "report.json").write_text(json.dumps(report))
                case = self.manager.case("pydantic")
                self.assertEqual(case["status"], "confirmed_break")
                self.assertEqual(case["integrations"]["cognee"]["status"], "failed")
                self.assertEqual(case["integrations"]["priorMemory"]["status"], "failed")
                json.dumps(case, allow_nan=False)

    def test_cloud_zero_graph_counts_are_visible_but_not_verified(self):
        report = fixture_report()
        report["memory_verification"] = cloud_memory()
        report["memory_verification"]["graph_summary"].update(num_nodes=0, num_edges=0)
        (self.demo / "report.json").write_text(json.dumps(report))
        service = self.manager.case("pydantic")["integrations"]["cognee"]
        self.assertEqual(service["status"], "failed")
        self.assertEqual(service["graphSummary"]["numNodes"], 0)
        self.assertEqual(service["graphSummary"]["numEdges"], 0)

    def test_unused_memory_backend_is_neutral_without_claiming_local_or_cloud(self):
        report = fixture_report()
        report["memory_verification"] = {"status": "not_run", "backend": "none"}
        report["prior_memory"] = {"status": "not_run", "backend": "none", "matches": []}
        (self.demo / "report.json").write_text(json.dumps(report))
        services = self.manager.case("pydantic")["integrations"]
        for key in ("cognee", "priorMemory"):
            self.assertEqual(services[key]["backend"], "none")
            self.assertEqual(services[key]["status"], "not_run")
            self.assertNotIn("local", services[key]["detail"])
            self.assertNotIn("Cloud", services[key]["detail"])
            self.assertNotIn("unknown", services[key]["detail"])
            self.assertIsNone(services[key]["graphUrl"])

    def test_one_active_run_and_bounded_output(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def run(command, emit):
            entered.set()
            emit("x" * (OUTPUT_LIMIT + 5000))
            release.wait(timeout=2)
            return 2
        with patch.object(self.manager, "_execute", side_effect=run):
            self.assertEqual(self.manager.start("pydantic")[0], 202)
            self.assertTrue(entered.wait(timeout=1))
            self.assertEqual(self.manager.start("gpu-energy-pandas")[0], 409)
            self.assertEqual(self.manager.state()["activeRun"]["status"], "running")
            release.set()
            self.manager.worker.join(timeout=2)
        job = self.manager.state()["history"][0]
        self.assertEqual(job["status"], "failed")
        self.assertLessEqual(len(job["output"]), OUTPUT_LIMIT)

    def test_failed_or_stale_rerun_does_not_reuse_previous_confirmation(self):
        self.write_reports()
        for exit_code in (0, 2, None):
            with self.subTest(exit_code=exit_code), patch.object(self.manager, "_execute", return_value=exit_code):
                self.assertEqual(self.manager.start("gpu-energy-pandas")[0], 202)
                self.manager.worker.join(timeout=2)
                state = self.manager.state()
                self.assertEqual(state["history"][0]["status"], "failed")
                self.assertEqual(state["cases"][0]["status"], "inconclusive")
                self.assertIsNone(state["cases"][0]["checks"][0]["before"])

    def test_worker_timeout_interrupts_child_and_allows_finally_cleanup(self):
        marker = self.root / "cleanup-marker"
        source = ("import time\nfrom pathlib import Path\ntry:\n time.sleep(10)\nfinally:\n"
                  f" Path({str(marker)!r}).write_text('cleaned')\n")
        output = []
        with patch("src.dashboard.RUN_TIMEOUT", 0.2):
            code = self.manager._execute([sys.executable, "-c", source], output.append)
        self.assertIsNone(code)
        self.assertEqual(marker.read_text(), "cleaned")
        self.assertIn("exceeded", "".join(output))

    def test_child_environment_excludes_provider_credentials(self):
        output = []
        source = "import os; print('secret_present=' + str('GROQ_API_KEY' in os.environ))"
        with patch.dict("os.environ", {"GROQ_API_KEY": "fake-test-key"}):
            code = self.manager._execute([sys.executable, "-c", source], output.append)
        self.assertEqual(code, 0)
        self.assertIn("secret_present=False", "".join(output))

    def test_public_repository_job_retains_result_and_normalizes_input(self):
        result = {"repository": "owner/project", "findings": [], "scope": "Static check only."}
        def inspect(repository, emit, cancelled):
            self.assertEqual(repository, "owner/project")
            self.assertIs(cancelled, self.manager.stopping)
            emit("Reading public source")
            return result
        with patch("src.dashboard.inspect_public_repo", side_effect=inspect):
            code, body = self.manager.inspect_repository("https://github.com/owner/project")
            self.assertEqual(code, 202)
            self.manager.repository_worker.join(timeout=2)
        state = self.manager.state()
        self.assertIsNone(state["activeScan"])
        scan = state["repositoryScans"][0]
        self.assertEqual(scan["id"], body["scan"]["id"])
        self.assertEqual(scan["status"], "completed")
        self.assertEqual({key: value for key, value in scan["result"].items() if key != "explanation"}, result)
        self.assertTrue(scan["result"]["explanation"]["summary"])
        self.assertNotIn("explanation", result)
        self.assertIn("Reading public source", scan["output"])
        self.assertIsNotNone(scan["finishedAt"])
        self.assertEqual(state["history"], [])

    def test_public_repository_rejects_bad_input_before_network_and_limits_active_scans(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def inspect(repository, emit, cancelled):
            entered.set()
            emit("x" * 5000)
            release.wait(timeout=2)
            return {"repository": repository, "findings": []}
        with patch("src.dashboard.inspect_public_repo", side_effect=inspect) as mocked:
            self.assertEqual(self.manager.inspect_repository("http://127.0.0.1/private")[0], 400)
            mocked.assert_not_called()
            self.assertEqual(self.manager.inspect_repository("owner/project")[0], 202)
            self.assertTrue(entered.wait(timeout=1))
            self.assertEqual(self.manager.inspect_repository("owner/another")[0], 409)
            self.assertEqual(self.manager.state()["activeScan"]["status"], "running")
            release.set()
            self.manager.repository_worker.join(timeout=2)
        self.assertLessEqual(len(self.manager.state()["repositoryScans"][0]["output"]), 4000)

    def test_public_repository_failed_retry_does_not_reuse_results(self):
        with patch("src.dashboard.inspect_public_repo", return_value={"repository": "owner/project", "findings": []}):
            self.manager.inspect_repository("owner/project")
            self.manager.repository_worker.join(timeout=2)
        for error, expected in ((PublicRepoError("Public repository not found."), "Public repository not found."),
                                (RuntimeError("SECRET in internal exception"), "Repository inspection could not complete. Please try again.")):
            with patch("src.dashboard.inspect_public_repo", side_effect=error):
                self.manager.inspect_repository("owner/project")
                self.manager.repository_worker.join(timeout=2)
            scan = self.manager.state()["repositoryScans"][0]
            self.assertEqual(scan["status"], "failed")
            self.assertEqual(scan["error"], expected)
            self.assertIsNone(scan["result"])

    def test_public_repository_http_validation_and_csrf(self):
        server = self.start_server()
        host = f"127.0.0.1:{server.server_port}"
        valid = {"Content-Type": "application/json", "Origin": "http://" + host,
                 "X-CSRF-Token": server.dashboard.token}
        with patch("src.dashboard.inspect_public_repo", return_value={"repository": "owner/project", "findings": []}) as mocked:
            for overrides in ({"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}):
                self.assertEqual(self.request(server, "POST", "/api/repositories", {"repository": "owner/project"}, valid | overrides)[0], 403)
            for payload in ({"repository": "owner/project", "command": "bad"}, {"repository": []}, {},
                            {"repository": "https://github.com.evil.example/owner/project"}):
                self.assertEqual(self.request(server, "POST", "/api/repositories", payload, valid)[0], 400)
            mocked.assert_not_called()
            status, body, _ = self.request(server, "POST", "/api/repositories", {"repository": "owner/project"}, valid)
            self.assertEqual(status, 202)
            server.dashboard.repository_worker.join(timeout=2)
            self.assertEqual(json.loads(body)["scan"]["repository"], "owner/project")
            self.assertEqual(self.request(server, "GET", "/api/state")[0], 200)

    def test_repository_picker_default_owner_comes_from_project_config(self):
        (self.root / ".env").write_text("TARGET_REPO=Configured-Owner/project\nGITHUB_TOKEN=secret\n")
        with patch.dict(os.environ, {}, clear=True):
            manager = Dashboard(self.root)
            self.assertEqual(manager.state()["repositoryOwner"], "Configured-Owner")
        with patch.dict(os.environ, {"TARGET_REPO": "Override/project"}, clear=True):
            self.assertEqual(Dashboard(self.root).repository_owner, "Override")
        (self.root / ".env").write_text("TARGET_REPO=${GITHUB_TOKEN}/project\nGITHUB_TOKEN=secret\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Dashboard(self.root).repository_owner, "Eman-Gon")

    def test_repository_picker_http_default_explicit_owner_and_validation(self):
        server = self.start_server()
        server.dashboard.repository_owner = "Configured-Owner"
        result = {"owner": "Configured-Owner", "repositories": [{"fullName": "Configured-Owner/project",
                  "url": "https://github.com/Configured-Owner/project", "description": ""}],
                  "truncated": False, "warning": None}
        with patch("src.dashboard.list_public_repositories", return_value=result) as listing:
            status, body, _ = self.request(server, "GET", "/api/repositories")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), result)
            listing.assert_called_once_with("Configured-Owner", cancelled=server.dashboard.stopping)
            self.assertEqual(self.request(server, "GET", "/api/repositories?owner=Another")[0], 200)
            listing.assert_called_with("Another", cancelled=server.dashboard.stopping)
            listing.reset_mock()
            for query in ("owner=", "owner=bad/name", "owner=one&owner=two", "token=secret", "owner=one&other=two"):
                self.assertEqual(self.request(server, "GET", "/api/repositories?" + query)[0], 400)
            for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
                self.assertEqual(self.request(server, "GET", "/api/repositories", headers=headers)[0], 403)
            listing.assert_not_called()

    def test_repository_picker_failure_preserves_owner_without_exposing_internal_error(self):
        server = self.start_server()
        server.dashboard.repository_owner = "Configured-Owner"
        for error, message in ((PublicRepoError("GitHub account not found."), "GitHub account not found."),
                               (RuntimeError("SECRET credentials"), "Could not load public repositories. Please retry.")):
            with patch("src.dashboard.list_public_repositories", side_effect=error):
                status, body, _ = self.request(server, "GET", "/api/repositories")
            self.assertEqual(status, 502)
            self.assertEqual(json.loads(body), {"owner": "Configured-Owner", "error": message})

    def start_server(self):
        server = make_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.dashboard.close()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        return server

    def request(self, server, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response.getheaders()
        finally:
            connection.close()

    def test_http_host_origin_csrf_and_command_allowlist(self):
        server = self.start_server()
        host = f"127.0.0.1:{server.server_port}"
        status, body, _ = self.request(server, "GET", "/api/state")
        self.assertEqual(status, 200)
        token = json.loads(body)["csrfToken"]
        valid = {"Content-Type": "application/json", "Origin": "http://" + host, "X-CSRF-Token": token}
        for overrides in ({"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            status, _, _ = self.request(server, "POST", "/api/runs", {"caseId": "pydantic"}, valid | overrides)
            self.assertEqual(status, 403)
        self.assertEqual(self.request(server, "GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request(server, "POST", "/api/runs", {"caseId": "pydantic"}, {"Content-Type": "application/json", "X-CSRF-Token": token})[0], 403)
        for payload in ({"caseId": "shell"}, {"caseId": "pydantic", "command": "anything"}, {"caseId": ["pydantic"]}):
            self.assertEqual(self.request(server, "POST", "/api/runs", payload, valid)[0], 400)
        self.assertEqual(server.dashboard.jobs, [])

    def test_http_serves_only_three_assets_and_normalized_report(self):
        self.write_reports()
        server = self.start_server()
        for path in ("/", "/styles.css", "/app.js"):
            self.assertEqual(self.request(server, "GET", path)[0], 200)
        (self.root / ".env").write_text("SECRET=not-for-the-browser")
        for path in ("/.env", "/../.env", "/%2e%2e/.env", "/.commit-watch/repo-audit/gpu-energy-pandas/results.json"):
            self.assertEqual(self.request(server, "GET", path)[0], 404)
        (self.root / "ui/app.js").unlink()
        (self.root / "ui/app.js").symlink_to(self.root / ".env")
        self.assertEqual(self.request(server, "GET", "/app.js")[0], 404)
        status, body, headers = self.request(server, "GET", "/api/report?caseId=pydantic")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "pydantic")
        self.assertIn("attachment", dict(headers)["Content-Disposition"])
        self.assertEqual(self.request(server, "GET", "/api/report?caseId=../../.env")[0], 400)


if __name__ == "__main__":
    unittest.main()
