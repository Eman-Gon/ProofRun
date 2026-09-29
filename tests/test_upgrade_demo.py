"""Validate the bounded upgrade probe and evidence classification without services."""

import ast
import asyncio
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
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from pydantic import ValidationError
from requests import Session

from src.sandbox import SandboxResult
from src.upgrade_demo import (
    PRIMARY_SOURCE, ProbePlan, UpgradeError, affected_usage, classify, detect_upgrade, read_source, render_probe,
    run_demo,
)


def plan_data(**overrides):
    data = {
        "input_row": {"name": "Ada"},
        "expected_row": {"name": "Ada", "nickname": None},
        "explanation": "Probe an omitted nickname rather than an explicit null.",
        "evidence_quote": "An Optional field without a default is required.",
    }
    data.update(overrides)
    return data


def comparison_results():
    statuses = {
        "existing_old": "pass", "existing_new": "pass", "probe_old": "pass",
        "probe_new": "fail", "fixed_old": "pass", "fixed_new": "pass",
    }
    results = {}
    for name, status in statuses.items():
        version = "1.10.18" if name.endswith("old") else "2.8.2"
        detail = (
            "pydantic_core._pydantic_core.ValidationError: 1 validation error for Customer\n"
            "nickname\n  Field required [type=missing, input_value={'name': 'Ada'}, input_type=dict]\n"
            "Ran 1 test in 0.001s\nFAILED (errors=1)"
            if status == "fail" else "Ran 1 test in 0.001s\nOK"
        )
        results[name] = {
            "status": status,
            "exit_code": 1 if status == "fail" else 0,
            "output_tail": f"SECONDLOOK_DEPENDENCY_VERSION={version}\n{detail}",
            "duration_seconds": 0.001,
        }
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
        self.directory = tempfile.TemporaryDirectory(prefix="secondlook-source-")
        self.addCleanup(self.directory.cleanup)
        self.demo = Path(self.directory.name)
        self.note = {
            "source_url": PRIMARY_SOURCE,
            "title": "Pydantic migration: required, optional, and nullable fields",
            "summary": "Optional fields without a default become required in Pydantic V2.",
            "evidence_quote": (
                "A field annotated as typing.Optional[T] will be required, "
                "and will allow for a value of None."
            ),
            "fetched_at": "2024-08-22T00:00:00+00:00",
        }
        (self.demo / "source.json").write_text(json.dumps(self.note))
        demo_patch = patch("src.upgrade_demo.DEMO", self.demo)
        self.factory_patch = patch("src.upgrade_demo._make_client")
        self.call_patch = patch("src.upgrade_demo._call")
        self.session = Session()
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.status_code = 200
        self.response.iter_content.return_value = [self.note["evidence_quote"].encode()]
        self.session.get = Mock(return_value=self.response)
        self.session_patch = patch("src.upgrade_demo.requests.Session", return_value=self.session)
        demo_patch.start()
        self.factory = self.factory_patch.start()
        self.call = self.call_patch.start()
        self.session_factory = self.session_patch.start()
        self.addCleanup(demo_patch.stop)
        self.addCleanup(self.factory_patch.stop)
        self.addCleanup(self.call_patch.stop)
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.session.close)

    def page(self, text):
        self.call.return_value = {"status": "success", "content": [{"text": text}]}

    def test_live_source_accepts_verified_quote_with_varying_heading_formats(self):
        quote = self.note["evidence_quote"]
        formatted_quote = quote.replace("typing.Optional[T]", "`typing.Optional[T]`").replace(
            "value of None.", "value of `None`."
        )
        for page in (
            "No heading is needed.\n" + quote + "\nAdditional details.",
            "#### Required, optional, and nullable fields\n\n" + formatted_quote,
            "Required, optional, and nullable fields\n=======================================\n"
            + formatted_quote.replace(" will ", "\nwill\t"),
        ):
            with self.subTest(page=page):
                self.factory.reset_mock()
                self.call.reset_mock()
                self.page(page)
                source = read_source("fake-key-no-network", offline=False)
                self.assertEqual(source["provenance"], "live Bright Data page read")
                self.assertIsNone(source["warning"])
                self.assertEqual(source["source_url"], self.note["source_url"])
                self.assertEqual(source["evidence_quote"], quote)
                self.assertIn(quote, source["text"])
                self.assertNotEqual(source["fetched_at"], self.note["fetched_at"])
                self.assertIsNotNone(datetime.fromisoformat(source["fetched_at"]).tzinfo)
                self.factory.assert_called_once_with("fake-key-no-network")
                self.call.assert_called_once_with(
                    self.factory.return_value.__enter__.return_value,
                    "scrape_as_markdown", {"url": self.note["source_url"]},
                )
                self.session_factory.assert_not_called()

    def test_live_excerpt_is_bounded_around_the_verified_quote(self):
        self.page("x " * 10_000 + self.note["evidence_quote"] + " y" * 10_000)
        source = read_source("fake-key-no-network", offline=False)
        self.assertIn(self.note["evidence_quote"], source["text"])
        self.assertLessEqual(len(source["text"]), 4_000)

    def test_required_bright_data_records_verified_provider_and_content_hash(self):
        page = "Verified primary source.\n" + self.note["evidence_quote"] + "\nAdditional context."
        self.page(page)
        source = read_source("fake-key-no-network", offline=False, require_brightdata=True)
        self.assertEqual(source["provider"], "brightdata")
        self.assertEqual(source["provenance"], "live Bright Data page read")
        self.assertEqual(source["content_sha256"], hashlib.sha256(page.encode()).hexdigest())
        self.assertIn(self.note["evidence_quote"], source["text"])
        self.assertEqual(source["attempts"], [{"provider": "brightdata", "status": "verified"}])
        self.call.assert_called_once()
        self.assertEqual(self.call.call_args.kwargs, {"timeout": 60})
        self.factory.assert_called_once_with("fake-key-no-network", timeout=60)
        self.session_factory.assert_not_called()

    def test_required_bright_data_retries_once_then_records_verified_source(self):
        page = "Verified retry.\n" + self.note["evidence_quote"]
        success = {"status": "success", "content": [{"text": page}]}
        for initial in (RuntimeError("transient provider failure"),
                        {"status": "success", "content": [{"text": "Unrelated source evidence."}]}):
            with self.subTest(initial=initial):
                self.call.reset_mock(side_effect=True)
                self.factory.reset_mock()
                self.call.side_effect = [initial, success]
                with redirect_stdout(io.StringIO()):
                    source = read_source("fake-key-no-network", offline=False, require_brightdata=True)
                self.assertEqual(source["provider"], "brightdata")
                self.assertEqual(source["content_sha256"], hashlib.sha256(page.encode()).hexdigest())
                self.assertIn(self.note["evidence_quote"], source["text"])
                self.assertEqual(source["attempts"], [
                    {"provider": "brightdata", "status": "failed"},
                    {"provider": "brightdata", "status": "verified"},
                ])
                self.assertEqual(self.call.call_count, 2)
                self.assertEqual(self.factory.call_count, 2)
                for call in self.call.call_args_list:
                    self.assertEqual(call.args[1:], ("scrape_as_markdown", {"url": self.note["source_url"]}))
                    self.assertEqual(call.kwargs, {"timeout": 60})
                for call in self.factory.call_args_list:
                    self.assertEqual(call.kwargs, {"timeout": 60})
                self.session_factory.assert_not_called()

    def test_required_bright_data_transport_failure_cannot_use_direct_fallback(self):
        self.call.side_effect = RuntimeError("provider details with fake-secret-token")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(UpgradeError, "required live integration failed") as failure:
            read_source("fake-key-no-network", offline=False, require_brightdata=True)
        self.assertNotIn("fake-secret-token", str(failure.exception))
        self.assertTrue(failure.exception.__suppress_context__)
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.factory.call_count, 2)
        self.session_factory.assert_not_called()

    def test_required_bright_data_quote_mismatch_cannot_use_direct_fallback(self):
        self.page("Successful source read, but the optional-field evidence is absent.")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(UpgradeError, "required live integration failed"):
            read_source("fake-key-no-network", offline=False, require_brightdata=True)
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.factory.call_count, 2)
        self.session_factory.assert_not_called()

    def test_bright_data_without_verified_quote_uses_labeled_direct_fallback(self):
        self.page("#### Required, optional, and nullable fields\nA different unsupported claim.")
        source = read_source("fake-key-no-network", offline=False)
        self.assertEqual(source["provenance"], "direct HTTPS to version-pinned upstream source")
        self.assertEqual(source["warning"],
                         "Bright Data did not return usable source evidence; "
                         "the public source was read directly without credentials.")
        self.assertIn(self.note["evidence_quote"], source["text"])
        self.call.assert_called_once()
        self.session_factory.assert_called_once_with()
        self.session.get.assert_called_once_with(
            PRIMARY_SOURCE, timeout=(10, 20), stream=True, allow_redirects=False,
        )
        self.assertFalse(self.session.trust_env)
        self.assertIsNone(self.session.auth)
        self.assertNotIn("Authorization", self.session.headers)

    def test_two_live_sources_without_verified_quote_fail_without_curated_fallback(self):
        self.page("A successful Bright Data response that contains unrelated evidence.")
        self.response.iter_content.return_value = [b"The direct source also lacks the expected quote."]
        with self.assertRaisesRegex(UpgradeError, "Both live source reads failed"):
            read_source("fake-key-no-network", offline=False)
        self.call.assert_called_once()
        self.session.get.assert_called_once()

    def test_invalid_local_quote_fails_before_either_live_connection(self):
        for quote in ("` " * 6, " " * 12, "short", "`short`" + " " * 20):
            with self.subTest(quote=quote):
                (self.demo / "source.json").write_text(json.dumps(dict(self.note, evidence_quote=quote)))
                with self.assertRaisesRegex(UpgradeError, "did not confirm"):
                    read_source("fake-key-no-network", offline=False)
        self.factory.assert_not_called()
        self.call.assert_not_called()
        self.session_factory.assert_not_called()

    def test_bright_data_failure_uses_labeled_bounded_credential_free_fallback(self):
        self.call.side_effect = RuntimeError("Bright Data unavailable")
        source = read_source("fake-bright-data-secret", offline=False)
        self.assertEqual(source["provenance"], "direct HTTPS to version-pinned upstream source")
        self.assertEqual(source["source_url"], PRIMARY_SOURCE)
        self.assertIn("Bright Data did not return usable source evidence", source["warning"])
        self.assertIn("without credentials", source["warning"])
        self.assertIn(self.note["evidence_quote"], source["text"])
        self.call.assert_called_once()
        self.session_factory.assert_called_once_with()
        self.session.get.assert_called_once_with(
            PRIMARY_SOURCE, timeout=(10, 20), stream=True, allow_redirects=False,
        )
        self.assertFalse(self.session.trust_env)
        self.assertIsNone(self.session.auth)
        self.assertNotIn("Authorization", self.session.headers)
        self.assertNotIn("fake-bright-data-secret", repr(self.session.get.call_args))
        self.response.iter_content.assert_called_once_with(chunk_size=8192)
        self.response.__exit__.assert_called_once()

    def test_direct_fallback_rejects_redirects_and_error_responses(self):
        self.call.side_effect = RuntimeError("Bright Data unavailable")
        for status in (301, 302, 307, 308, 403, 500):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                self.response.iter_content.reset_mock()
                self.response.status_code = status
                with self.assertRaisesRegex(UpgradeError, "Both live source reads failed"):
                    read_source("fake-key-no-network", offline=False)
                self.session.get.assert_called_once()
                self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
                self.response.iter_content.assert_not_called()

    def test_direct_fallback_rejects_oversized_bodies_even_with_correct_quote(self):
        self.call.side_effect = RuntimeError("Bright Data unavailable")
        self.response.iter_content.return_value = [self.note["evidence_quote"].encode(), b"x" * 256_001]
        with self.assertRaisesRegex(UpgradeError, "Both live source reads failed"):
            read_source("fake-key-no-network", offline=False)
        self.session.get.assert_called_once()
        self.response.__exit__.assert_called_once()

    def test_direct_fallback_still_requires_the_supported_source_quote(self):
        self.call.side_effect = RuntimeError("Bright Data unavailable")
        self.response.iter_content.return_value = [b"Different migration text with no supported evidence."]
        with self.assertRaisesRegex(UpgradeError, "Both live source reads failed"):
            read_source("fake-key-no-network", offline=False)
        self.session.get.assert_called_once()

    def test_unsupported_source_urls_are_rejected_before_any_connection(self):
        for url in (
            "http://raw.githubusercontent.com/pydantic/pydantic/v2.8.2/docs/migration.md",
            PRIMARY_SOURCE + "?token=unexpected",
            "https://raw.githubusercontent.com/pydantic/pydantic/main/docs/migration.md",
            "https://localhost/migration.md",
        ):
            for offline in (True, False):
                with self.subTest(url=url, offline=offline):
                    (self.demo / "source.json").write_text(json.dumps(dict(self.note, source_url=url)))
                    with self.assertRaisesRegex(UpgradeError, "version-pinned Pydantic migration source"):
                        read_source("fake-key-no-network", offline=offline)
        self.factory.assert_not_called()
        self.call.assert_not_called()
        self.session_factory.assert_not_called()

    def test_offline_source_preserves_curated_provenance_without_network(self):
        self.factory.side_effect = AssertionError("Offline mode must not connect")
        source = read_source("", offline=True)
        self.assertEqual(source["provenance"], "curated source note; no live lookup")
        self.assertEqual(source["fetched_at"], self.note["fetched_at"])
        self.assertEqual(source["text"], self.note["summary"] + "\n" + self.note["evidence_quote"])
        self.factory.assert_not_called()
        self.call.assert_not_called()
        self.session_factory.assert_not_called()

    def test_live_source_requires_credentials_before_connecting(self):
        with self.assertRaisesRegex(UpgradeError, "BRIGHTDATA_API_KEY"):
            read_source("", offline=False)
        self.factory.assert_not_called()
        self.call.assert_not_called()
        self.session_factory.assert_not_called()


class ComparisonClassificationTests(unittest.TestCase):
    def test_confirms_complete_matching_version_comparison(self):
        self.assertEqual(classify(comparison_results()), "confirmed_break")

    def test_every_required_outcome_is_necessary(self):
        good = comparison_results()
        for name, result in good.items():
            for status in {"pass", "fail", "error", "timeout", "unknown"} - {result["status"]}:
                with self.subTest(name=name, status=status):
                    changed = deepcopy(good)
                    changed[name]["status"] = status
                    self.assertEqual(classify(changed), "inconclusive")

    def test_missing_or_extra_comparisons_are_inconclusive(self):
        for name in comparison_results():
            with self.subTest(missing=name):
                results = comparison_results()
                del results[name]
                self.assertEqual(classify(results), "inconclusive")
        results = comparison_results()
        results["unexpected_new"] = deepcopy(results["fixed_new"])
        self.assertEqual(classify(results), "inconclusive")
        self.assertEqual(classify({}), "inconclusive")

    def test_each_installed_version_must_match_the_declared_environment_exactly(self):
        good = comparison_results()
        for name in good:
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


class RequiredIntegrationFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="secondlook-strict-flow-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.demo = self.root / "demo" / "upgrade"
        shutil.copytree(Path(__file__).resolve().parents[1] / "demo" / "upgrade", self.demo)
        self.state = self.root / "state"
        note = json.loads((self.demo / "source.json").read_text())
        self.source = dict(note, text=note["evidence_quote"], provider="brightdata",
                           content_sha256="d" * 64, provenance="live Bright Data page read")
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
            "recall_prior": AsyncMock(return_value={"status": "not_found", "matches": []}),
            "remember_and_recall": AsyncMock(return_value={
                "status": "stored_and_retrieved", "dataset": "verified-dataset", "evidence_sha256": "e" * 64,
            }),
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
            "GROQ_API_KEY": "fake-groq-no-network", "BRIGHTDATA_API_KEY": "fake-brightdata-no-network",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def execute(self, **options):
        self.run_probe.side_effect = [SandboxResult(**item) for item in comparison_results().values()]
        with redirect_stdout(io.StringIO()):
            code = run_demo(**{"require_integrations": True, **options})
        reports = list((self.state / "upgrade-demo").glob("*/report.json"))
        latest = max(reports, key=lambda path: path.stat().st_mtime_ns)
        return code, json.loads(latest.read_text())

    def test_strict_flag_conflicts_fail_before_loop_or_io(self):
        for arguments in ({"offline": True}, {"prepare": True}, {"remember": False}):
            with self.subTest(arguments=arguments), patch("src.upgrade_demo.asyncio.Runner") as runner:
                with self.assertRaisesRegex(UpgradeError, "requires a live run"):
                    run_demo(require_integrations=True, **arguments)
                runner.assert_not_called()
        self.load_dotenv.assert_not_called()
        self.read_source.assert_not_called()
        self.build_image.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_strict_first_run_and_replay_require_real_integration_results_and_feed_memory_to_model(self):
        code, report = self.execute()
        self.assertEqual(code, 1)
        self.assertTrue(report["integrations_verified"])
        self.assertEqual(report["source"]["provider"], "brightdata")
        self.assertEqual(report["memory_verification"]["status"], "stored_and_retrieved")
        self.read_source.assert_called_once_with("fake-brightdata-no-network", False, require_brightdata=True)
        self.remember_and_recall.assert_awaited_once()
        saved_report, saved_state = self.remember_and_recall.await_args.args
        self.assertEqual(saved_state, self.state)
        self.assertEqual(saved_report["status"], "confirmed_break")
        self.assertIn("nickname: Optional[str] = None", saved_report["fix_patch"])
        first_payload = json.loads(self.agent.call_args.args[0])
        self.assertEqual(first_payload["previous_verified_findings"], {"status": "not_found", "matches": []})
        prior = {"status": "recalled", "matches": [{"status": "recalled", "dataset": "verified-dataset",
                  "evidence_sha256": "e" * 64, "retrieved_excerpt": "Verified earlier comparison."}]}
        self.recall_prior.return_value = prior
        code, replay = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(replay["prior_memory"], prior)
        replay_payload = json.loads(self.agent.call_args.args[0])
        self.assertEqual(replay_payload["previous_verified_findings"], prior)
        self.assertEqual(self.run_probe.call_count, 12)

    def test_strict_cognee_failures_cannot_return_confirmed_success_exit(self):
        cases = (
            (RuntimeError("persistence failure"), {"status": "not_found", "matches": []}),
            ({"status": "stored_only", "dataset": "unverified-dataset"}, {"status": "not_found", "matches": []}),
            ({"status": "stored_and_retrieved", "dataset": "verified-dataset"}, RuntimeError("recall failure")),
        )
        for persistence, prior in cases:
            with self.subTest(persistence=persistence, prior=prior):
                self.build_image.side_effect = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
                self.remember_and_recall.side_effect = persistence if isinstance(persistence, Exception) else None
                self.remember_and_recall.return_value = persistence
                self.recall_prior.side_effect = prior if isinstance(prior, Exception) else None
                self.recall_prior.return_value = prior
                code, report = self.execute()
                self.assertEqual(code, 2)
                self.assertFalse(report["integrations_verified"])
                self.assertEqual(report["status"], "confirmed_break")

    def test_strict_source_failure_stops_before_model_docker_or_memory(self):
        self.read_source.side_effect = UpgradeError("Required Bright Data evidence unavailable.")
        with redirect_stdout(io.StringIO()), self.assertRaises(UpgradeError):
            run_demo(require_integrations=True)
        self.agent.assert_not_called()
        self.build_image.assert_not_called()
        self.recall_prior.assert_not_awaited()
        self.remember_and_recall.assert_not_awaited()

    def test_strict_source_provider_must_be_bright_data_even_if_comparison_and_memory_succeed(self):
        self.source["provider"] = "direct_https"
        code, report = self.execute()
        self.assertEqual(code, 2)
        self.assertFalse(report["integrations_verified"])

    def test_cloud_failures_keep_backend_and_allow_remote_processing_deadline(self):
        self.recall_prior.side_effect = RuntimeError("private-provider-error")
        self.remember_and_recall.side_effect = RuntimeError("private-provider-error")
        with patch.dict("os.environ", {"COGNEE_MEMORY_BACKEND": "cloud"}), \
                patch("src.upgrade_demo.asyncio.wait_for", wraps=asyncio.wait_for) as wait_for:
            code, report = self.execute()
        self.assertEqual(code, 2)
        self.assertEqual(report["prior_memory"]["backend"], "cloud")
        self.assertEqual(report["memory_verification"], {"status": "failed", "backend": "cloud"})
        self.assertEqual([call.kwargs["timeout"] for call in wait_for.call_args_list], [60, 240])
        self.assertNotIn("private-provider-error", json.dumps(report))

    def test_disabled_or_offline_memory_does_not_claim_cloud_was_used(self):
        with patch.dict("os.environ", {"COGNEE_MEMORY_BACKEND": "cloud"}), \
                patch("src.upgrade_demo.preflight"):
            for options in ({"remember": False}, {"offline": True}):
                with self.subTest(options=options):
                    self.build_image.side_effect = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
                    _, report = self.execute(require_integrations=False, **options)
                    self.assertEqual(report["memory_verification"], {"status": "not_requested", "backend": "none"})
                    self.assertEqual(report["prior_memory"], {"status": "not_requested", "matches": [], "backend": "none"})
        self.recall_prior.assert_not_awaited()
        self.remember_and_recall.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
