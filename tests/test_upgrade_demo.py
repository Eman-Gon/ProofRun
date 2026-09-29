"""Validate the bounded upgrade probe and evidence classification without services."""

import ast
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from pydantic import ValidationError
from requests import Session

from src.sandbox import SandboxResult
from src.upgrade_demo import (
    CONTROL_TEST_IDS, ORIGINAL_TEST_IDS, PRIMARY_SOURCE, PROBE_TEST_IDS, REPAIRED_TEST_IDS,
    ProbePlan, UpgradeError, affected_usage, classify, detect_upgrade, execution_status, finding_status,
    read_source, render_probe, repair_status, run_demo,
)
from src.upgrade_sandbox import parse_probe_result


def plan_data(**overrides):
    data = {
        "input_row": {"name": "Ada"},
        "expected_row": {"name": "Ada", "nickname": None},
        "explanation": "Probe an omitted nickname rather than an explicit null.",
        "evidence_quote": "An Optional field without a default is required.",
    }
    data.update(overrides)
    return data


def probe_result(version, ids, failed_id=None, detail=None):
    detail = detail or (
        "pydantic_core._pydantic_core.ValidationError: 1 validation error for Customer\n"
        "nickname\n  Field required [type=missing, input_value={'name': 'Ada'}, input_type=dict]\n"
    )
    records = [{"id": case_id, "status": "error" if case_id == failed_id else "pass",
                **({"detail": detail} if case_id == failed_id else {})} for case_id in ids]
    evidence = {
        "schema_version": "proofrun.probe.v1", "version": version, "tests_run": len(ids), "tests": records,
        "source_sha256": "a" * 64, "tests_sha256": {case_id.split(".")[0] + ".py": "b" * 64 for case_id in ids},
        "harness_sha256": "c" * 64, "integrity_errors": [],
    }
    return {
        "status": "fail" if failed_id else "pass", "exit_code": 1 if failed_id else 0,
        "output_tail": f"Ran {len(ids)} tests in 0.001s\n"
                       + (detail + "FAILED (errors=1)\n" if failed_id else "OK\n")
                       + f"SECONDLOOK_DEPENDENCY_VERSION={version}\nPROOFRUN_PROBE_RESULT=" + json.dumps(evidence),
        "duration_seconds": 0.001,
    }


def replace_evidence(result, mutate):
    evidence = parse_probe_result(result["output_tail"])
    mutate(evidence)
    result["output_tail"] = result["output_tail"].split("PROOFRUN_PROBE_RESULT=")[0] + "PROOFRUN_PROBE_RESULT=" + json.dumps(evidence)


def comparison_results(include_additional=False):
    statuses = {
        "existing_old": "pass", "existing_new": "pass", "probe_old": "pass",
        "probe_new": "fail", "fixed_old": "pass", "fixed_new": "pass",
    }
    results = {}
    for name, status in statuses.items():
        version = "1.10.18" if name.endswith("old") else "2.8.2"
        ids = ORIGINAL_TEST_IDS if name.startswith("existing") else REPAIRED_TEST_IDS if name.startswith("fixed") else PROBE_TEST_IDS
        results[name] = probe_result(version, ids, PROBE_TEST_IDS[0] if status == "fail" else None)
    if include_additional:
        for label, version in (("old", "1.10.18"), ("new", "2.8.2")):
            results["controls_" + label] = probe_result(version, CONTROL_TEST_IDS, CONTROL_TEST_IDS[0] if label == "new" else None)
        for label, version in (("old", "1.10.18"), ("new", "2.8.2")):
            results["permissive_" + label] = probe_result(
                version, REPAIRED_TEST_IDS, CONTROL_TEST_IDS[3], "AssertionError: ValidationError not raised",
            )
    return results


class UpgradeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="secondlook-detection-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.demo = self.root / "demo" / "upgrade"
        self.demo.mkdir(parents=True)
        root_patch = patch("src.upgrade_demo.ROOT", self.root)
        demo_patch = patch("src.upgrade_demo.DEMO", self.demo)
        root_patch.start()
        demo_patch.start()
        self.addCleanup(root_patch.stop)
        self.addCleanup(demo_patch.stop)

    def requirements(self, old="pydantic==1.10.18", new="pydantic==2.8.2"):
        (self.demo / "requirements-old.txt").write_text(old + "\ntyping_extensions==4.12.2\n")
        (self.demo / "requirements-new.txt").write_text(new + "\ntyping_extensions==4.12.2\n")

    def usage(self, code):
        app = self.demo / "app.py"
        app.write_text(code)
        return affected_usage(app)

    def test_detects_exact_upgrade_among_other_dependencies(self):
        self.requirements(new="pydantic==2.8.2\npydantic-core==2.20.1\nannotated-types==0.7.0")
        self.assertEqual(detect_upgrade(), {
            "ecosystem": "pypi", "package": "pydantic", "before": "==1.10.18", "version": "==2.8.2",
        })

    def test_rejects_missing_unchanged_or_unpinned_version_pair(self):
        for old, new in (
            ("pydantic==1.10.18", "pydantic==1.10.18"),
            ("pydantic==1.10.17", "pydantic==2.8.2"),
            ("pydantic==1.10.18", "pydantic==2.9.0"),
            ("pydantic>=1.10.18", "pydantic==2.8.2"),
            ("pydantic==1.10.18", "pydantic>=2.8.2"),
            ("", "pydantic==2.8.2"),
            ("pydantic==1.10.18", ""),
        ):
            with self.subTest(old=old, new=new):
                self.requirements(old, new)
                with self.assertRaises(UpgradeError):
                    detect_upgrade()

    def test_locates_the_affected_field_and_source_line(self):
        code = (
            "from typing import Optional\nfrom pydantic import BaseModel\n\n"
            "class Customer(BaseModel):\n    name: str\n    nickname: Optional[str]\n"
        )
        self.assertEqual(self.usage(code), {
            "path": "demo/upgrade/app.py", "line": 6,
            "symbol": "Customer.nickname", "code": "nickname: Optional[str]",
        })

    def test_does_not_match_strings_comments_defaults_or_other_fields(self):
        cases = (
            '# class Customer(BaseModel): nickname: Optional[str]\n',
            'text = "class Customer(BaseModel): nickname: Optional[str]"\n',
            "class Customer(BaseModel):\n    nickname: Optional[str] = None\n",
            "class Customer(BaseModel):\n    nickname: Optional[str] = 'Ada'\n",
            "class Customer(BaseModel):\n    name: Optional[str]\n",
            "class Other(BaseModel):\n    nickname: Optional[str]\n",
            "class Customer(BaseModel):\n    nickname: str\n",
        )
        for code in cases:
            with self.subTest(code=code):
                with self.assertRaises(UpgradeError):
                    self.usage(code)


class ProbePlanTests(unittest.TestCase):
    def test_accepts_only_the_supported_missing_nickname_contract(self):
        plan = ProbePlan(**plan_data())
        self.assertEqual(plan.input_row, {"name": "Ada"})
        self.assertEqual(plan.expected_row, {"name": "Ada", "nickname": None})

    def test_rejects_unsupported_inputs_and_expected_behavior(self):
        for changes in (
            {"input_row": {}},
            {"input_row": {"name": ""}},
            {"input_row": {"name": " \n\t"}},
            {"input_row": {"name": "a" * 65}},
            {"input_row": {"name": "Ada", "nickname": "Ada"}},
            {"input_row": {"name": 123}},
            {"input_row": {"name": None}},
            {"input_row": {"name": {"code": "unexpected"}}},
            {"expected_row": {"name": "Grace", "nickname": None}},
            {"expected_row": {"name": "Ada"}},
            {"expected_row": {"name": "Ada", "nickname": ""}},
            {"expected_row": {"name": "Ada", "nickname": False}},
            {"expected_row": {"name": "Ada", "nickname": None, "extra": "field"}},
            {"command": "unapproved code"},
            {"explanation": ""},
            {"explanation": "x" * 601},
            {"evidence_quote": "short"},
            {"evidence_quote": "x" * 401},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    ProbePlan(**plan_data(**changes))

    def test_rejects_missing_required_model_fields(self):
        for name in plan_data():
            with self.subTest(name=name):
                data = plan_data()
                del data[name]
                with self.assertRaises(ValidationError):
                    ProbePlan(**data)

    def test_rejects_quotes_that_only_have_markdown_or_whitespace(self):
        for quote in ("` " * 6, " " * 12, "\n\t" * 6, "`short`" + " " * 20):
            with self.subTest(quote=quote):
                with self.assertRaisesRegex(ValidationError, "meaningful characters"):
                    ProbePlan(**plan_data(evidence_quote=quote))

    def test_rendered_probe_preserves_model_strings_as_literal_data(self):
        names = (
            "Ada", "Ada 'Countess' \\\nLovelace", "Zoë\u202e", "null\x00byte",
            "'); __import__('builtins').print('INJECTED'); #",
            "${HOME}; $(echo INJECTED); `echo INJECTED`",
        )
        for name in names:
            with self.subTest(name=name):
                plan = ProbePlan(**plan_data(
                    input_row={"name": name}, expected_row={"name": name, "nickname": None},
                    explanation="__import__('builtins').print('EXPLANATION')",
                    evidence_quote="__import__('builtins').print('EVIDENCE')",
                ))
                source = render_probe(plan)
                tree = ast.parse(source)
                test_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
                self.assertEqual(test_class.name, "TestUpgrade")
                method = test_class.body[0]
                self.assertEqual(method.name, "test_missing_nickname")
                assertion = method.body[0].value
                self.assertEqual(assertion.func.attr, "assertEqual")
                app_call, expected = assertion.args
                self.assertEqual(app_call.func.id, "import_row")
                self.assertEqual(ast.literal_eval(app_call.args[0]), plan.input_row)
                self.assertEqual(ast.literal_eval(expected), plan.expected_row)
                self.assertEqual(len([node for node in ast.walk(tree) if isinstance(node, ast.Call)]), 2)
                self.assertNotIn("EXPLANATION", source)
                self.assertNotIn("EVIDENCE", source)


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.note = json.loads((Path(__file__).resolve().parents[1] / "demo/upgrade/source.json").read_text())
        self.session = Session()
        self.addCleanup(self.session.close)
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.status_code = 200
        self.response.iter_content.return_value = [self.note["evidence_quote"].encode()]
        self.session.get = Mock(return_value=self.response)
        factory = patch("src.upgrade_demo.requests.Session", return_value=self.session)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    def test_live_source_is_bounded_and_credential_free(self):
        page = "x " * 10000 + self.note["evidence_quote"] + " y" * 10000
        self.response.iter_content.return_value = [page.encode()]
        source = read_source(offline=False)
        self.assertEqual(source["provider"], "direct_https")
        self.assertEqual(source["content_sha256"], hashlib.sha256(page.encode()).hexdigest())
        self.assertIn(self.note["evidence_quote"], source["text"])
        self.assertLessEqual(len(source["text"]), 4000)
        self.session.get.assert_called_once_with(PRIMARY_SOURCE, timeout=(10, 20), stream=True, allow_redirects=False)
        self.assertFalse(self.session.trust_env)
        self.assertIsNone(self.session.auth)
        self.assertNotIn("Authorization", self.session.headers)

    def test_redirects_errors_oversized_and_unrelated_content_fail(self):
        for status, chunks in ((302, []), (500, []), (200, [b"x" * 256001]), (200, [b"unrelated"])):
            with self.subTest(status=status, size=sum(map(len, chunks))):
                self.response.status_code = status
                self.response.iter_content.return_value = chunks
                with self.assertRaises(UpgradeError):
                    read_source(offline=False)

    def test_offline_source_never_connects(self):
        source = read_source(offline=True)
        self.assertEqual(source["provider"], "curated")
        self.assertEqual(source["fetched_at"], self.note["fetched_at"])
        self.factory.assert_not_called()

    def test_changed_source_url_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "source.json").write_text(json.dumps({**self.note, "source_url": "https://example.com"}))
            with patch("src.upgrade_demo.DEMO", path):
                for offline in (False, True):
                    with self.subTest(offline=offline), self.assertRaises(UpgradeError):
                        read_source(offline=offline)
        self.factory.assert_not_called()


class ComparisonClassificationTests(unittest.TestCase):
    def test_confirms_complete_matching_version_comparison(self):
        self.assertEqual(classify(comparison_results()), "confirmed_break")

    def test_every_required_reproduction_outcome_is_necessary(self):
        good = comparison_results()
        for name, result in good.items():
            if name.startswith("fixed"):
                continue
            for status in {"pass", "fail", "error", "timeout", "unknown"} - {result["status"]}:
                with self.subTest(name=name, status=status):
                    changed = deepcopy(good)
                    changed[name]["status"] = status
                    self.assertEqual(classify(changed), "inconclusive")

    def test_missing_reproduction_comparisons_are_inconclusive(self):
        for name in comparison_results():
            if name.startswith("fixed"):
                continue
            with self.subTest(missing=name):
                results = comparison_results()
                del results[name]
                self.assertEqual(classify(results), "inconclusive")
        self.assertEqual(classify({}), "inconclusive")

    def test_each_installed_version_must_match_the_declared_environment_exactly(self):
        good = comparison_results()
        for name in good:
            if name.startswith("fixed"):
                continue
            version = "1.10.18" if name.endswith("old") else "2.8.2"
            marker = f"SECONDLOOK_DEPENDENCY_VERSION={version}"
            for replacement in (
                "", "SECONDLOOK_DEPENDENCY_VERSION=2.13.5", marker + ".1",
                marker + " suffix", "prefix " + marker, marker + "\n" + marker,
            ):
                with self.subTest(name=name, replacement=replacement):
                    results = deepcopy(good)
                    results[name]["output_tail"] = results[name]["output_tail"].replace(marker, replacement)
                    self.assertEqual(classify(results), "inconclusive")

    def test_failed_or_missing_repair_does_not_erase_reproduction(self):
        for status in ("fail", "error", "timeout", "unknown", None):
            with self.subTest(status=status):
                results = comparison_results()
                if status is None:
                    del results["fixed_new"]
                else:
                    results["fixed_new"]["status"] = status
                self.assertEqual(classify(results), "confirmed_break")
                self.assertEqual(finding_status(results), "regression_reproduced")
                self.assertNotEqual(repair_status(results), "verified")

    def test_actual_repair_failure_is_rejected_while_execution_completes(self):
        results = comparison_results()
        results["fixed_new"] = probe_result("2.8.2", REPAIRED_TEST_IDS, CONTROL_TEST_IDS[3], "AssertionError: ValidationError not raised")
        self.assertEqual(repair_status(results), "rejected")
        self.assertEqual(classify(results), "confirmed_break")
        self.assertEqual(execution_status(results), "completed")

    def test_original_suite_and_every_control_are_required_for_verified_repair(self):
        good = comparison_results()
        self.assertEqual(repair_status(good), "verified")
        for label in ("old", "new"):
            name = "fixed_" + label
            for case_id in REPAIRED_TEST_IDS:
                with self.subTest(name=name, missing=case_id):
                    results = deepcopy(good)
                    replace_evidence(results[name], lambda evidence: evidence.update(
                        tests=[item for item in evidence["tests"] if item["id"] != case_id], tests_run=7,
                    ))
                    self.assertEqual(repair_status(results), "unavailable")
                    self.assertEqual(classify(results), "confirmed_break")

    def test_wrong_duplicate_skipped_empty_and_stale_evidence_cannot_verify(self):
        mutations = (
            lambda evidence: evidence.update(tests=[], tests_run=0),
            lambda evidence: evidence["tests"].append(dict(evidence["tests"][0])),
            lambda evidence: evidence["tests"][0].update(id="wrong.case"),
            lambda evidence: evidence["tests"][0].update(status="skip"),
            lambda evidence: evidence.update(version="2.13.5"),
            lambda evidence: evidence.update(integrity_errors=["source_sha256 differs"]),
        )
        for mutate in mutations:
            results = comparison_results()
            replace_evidence(results["fixed_new"], mutate)
            self.assertEqual(repair_status(results), "unavailable")
            self.assertEqual(classify(results), "confirmed_break")

    def test_matching_passes_can_only_report_no_difference(self):
        results = comparison_results()
        results["probe_new"] = probe_result("2.8.2", PROBE_TEST_IDS)
        self.assertEqual(finding_status(results), "no_difference_observed")
        self.assertEqual(classify(results), "inconclusive")

    def test_unrelated_new_version_failures_are_inconclusive(self):
        for output in (
            "TypeError: unexpected argument\nRan 1 test in 0.001s\nFAILED (errors=1)",
            "ValidationError: nickname must be a string",
            "ValidationError: other_field\n  Field required",
            "AssertionError: nickname\n  Field required",
        ):
            with self.subTest(output=output):
                results = comparison_results()
                results["probe_new"]["output_tail"] = "SECONDLOOK_DEPENDENCY_VERSION=2.8.2\n" + output
                self.assertEqual(classify(results), "inconclusive")


class ComparisonFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="proofrun-comparison-flow-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.demo = self.root / "demo" / "upgrade"
        shutil.copytree(Path(__file__).resolve().parents[1] / "demo" / "upgrade", self.demo)
        self.state = self.root / "state"
        note = json.loads((self.demo / "source.json").read_text())
        self.source = dict(note, text=note["evidence_quote"], provider="direct_https",
                           content_sha256="d" * 64, provenance="direct HTTPS to version-pinned upstream source")
        self.plan = ProbePlan(**plan_data(evidence_quote=note["evidence_quote"]))
        self.reply = MagicMock(stop_reason="end_turn")
        self.reply.__str__.return_value = self.plan.model_dump_json()
        self.agent = Mock(return_value=self.reply)
        self.modules = {
            "strands.models.litellm": SimpleNamespace(LiteLLMModel=Mock()),
            "strands": SimpleNamespace(Agent=Mock(return_value=self.agent)),
            "strands.agent.conversation_manager": SimpleNamespace(NullConversationManager=Mock()),
        }
        patches = {
            "ROOT": self.root, "DEMO": self.demo, "STATE": self.state,
            "load_dotenv": Mock(), "read_source": Mock(return_value=self.source),
            "build_image": Mock(side_effect=["sha256:" + "a" * 64, "sha256:" + "b" * 64] * 2),
            "run_probe": Mock(),
        }
        for name, value in patches.items():
            patched = patch("src.upgrade_demo." + name, value)
            patched.start()
            self.addCleanup(patched.stop)
            setattr(self, name, value)
        importer = patch("src.upgrade_demo.importlib", SimpleNamespace(
            import_module=Mock(side_effect=self.modules.__getitem__),
        ))
        importer.start()
        self.addCleanup(importer.stop)
        environment = patch.dict("os.environ", {
            "GROQ_API_KEY": "fake-groq-no-network",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def execute(self, **options):
        self.run_probe.side_effect = [SandboxResult(**item) for item in comparison_results(include_additional=True).values()]
        with redirect_stdout(io.StringIO()):
            code = run_demo(**options)
        reports = list((self.state / "upgrade-demo").glob("*/report.json"))
        latest = max(reports, key=lambda path: path.stat().st_mtime_ns)
        return code, json.loads(latest.read_text())

    def test_prepared_demo_runs_original_suite_and_independent_controls_and_rejects_bad_fix(self):
        code, report = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(report["execution_status"], "completed")
        self.assertEqual(report["finding_status"], "regression_reproduced")
        self.assertEqual(report["repair_status"], "verified")
        self.assertEqual(report["negative_demonstration"]["repair_status"], "rejected")
        self.assertEqual(set(report["results"]), set(comparison_results()))
        for index in (4, 5, 8, 9):
            call = self.run_probe.call_args_list[index]
            self.assertEqual({path.name for path in call.args[2]}, {"test_existing.py", "test_upgrade.py", "test_controls.py"})
            self.assertEqual(set(call.kwargs["expected_test_ids"]), set(REPAIRED_TEST_IDS))
        for name, digest in report["tests_sha256"].items():
            self.assertEqual(digest, hashlib.sha256((Path(report["artifact_dir"]) / name).read_bytes()).hexdigest())
        negative = report["negative_demonstration"]
        self.assertEqual(negative["candidate_sha256"], hashlib.sha256((self.demo / "permissive_app.py").read_bytes()).hexdigest())

    def test_offline_comparison_never_calls_model_and_omits_service_metadata(self):
        with patch("src.upgrade_demo.preflight"):
            code, report = self.execute(offline=True)
        self.assertEqual(code, 1)
        self.agent.assert_not_called()
        self.assertNotIn("memory_verification", report)
        self.assertNotIn("prior_memory", report)
        self.assertNotIn("integrations_verified", report)

    def test_live_source_failure_stops_before_model_and_docker(self):
        self.read_source.side_effect = UpgradeError("Source unavailable")
        with redirect_stdout(io.StringIO()), self.assertRaises(UpgradeError):
            run_demo()
        self.agent.assert_not_called()
        self.build_image.assert_not_called()
        self.run_probe.assert_not_called()
