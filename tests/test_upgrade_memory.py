"""Verify that upgrade memory requires real, exact-scope retrieval evidence."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.upgrade_memory import (
    DATASET_PREFIX, INDEX_NAME, MAX_ENTRIES, PROJECT, UpgradeMemoryError,
    recall_prior, remember_and_recall,
)


def report():
    statuses = {
        "existing_old": "pass", "existing_new": "pass", "probe_old": "pass",
        "probe_new": "fail", "fixed_old": "pass", "fixed_new": "pass",
    }
    results = {}
    for name, status in statuses.items():
        version = "1.10.18" if name.endswith("old") else "2.8.2"
        detail = (
            "ValidationError: Customer\nnickname\n  Field required\nRan 1 test in 0.001s\nFAILED (errors=1)"
            if status == "fail" else "Ran 1 test in 0.001s\nOK"
        )
        results[name] = {"status": status, "exit_code": 1 if status == "fail" else 0,
                         "output_tail": f"{detail}\nSECONDLOOK_DEPENDENCY_VERSION={version}",
                         "duration_seconds": 0.001}
    quote = "An Optional field without a default is required."
    return {
        "status": "confirmed_break", "checked_at": "2026-09-22T01:00:00+00:00",
        "upgrade": {"ecosystem": "pypi", "package": "pydantic", "before": "==1.10.18", "version": "==2.8.2"},
        "usage": {"path": "demo/upgrade/app.py", "line": 10, "symbol": "Customer.nickname",
                  "code": "nickname: Optional[str]"},
        "source": {"source_url": "https://raw.githubusercontent.com/pydantic/pydantic/v2.8.2/docs/migration.md",
                   "title": "Pydantic migration", "summary": "Omitting nickname changes behavior.",
                   "evidence_quote": quote, "text": quote, "fetched_at": "2026-09-22T00:59:00Z",
                   "provenance": "direct HTTPS to version-pinned upstream source",
                   "provider": "direct_https", "content_sha256": "f" * 64,
                   "warning": "Bright Data did not return usable source evidence."},
        "plan": {"input_row": {"name": "Ada"}, "expected_row": {"name": "Ada", "nickname": None}},
        "app_sha256": "a" * 64, "test_sha256": "b" * 64, "fixed_app_sha256": "c" * 64,
        "images": {"old": "sha256:" + "d" * 64, "new": "sha256:" + "e" * 64},
        "results": results,
        "fix_patch": "--- app.py\n+++ app.py\n@@ -10 +10 @@\n-    nickname: Optional[str]\n+    nickname: Optional[str] = None\n",
        "untrusted_extra": "DO NOT COPY THIS EXTRA FIELD",
    }


class UpgradeMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="secondlook-memory-")
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.path = self.state / INDEX_NAME
        self.documents = {}
        self.events = []
        self.cognee = Mock()
        self.cognee.add = AsyncMock(side_effect=self.add)
        self.cognee.cognify = AsyncMock(side_effect=self.cognify)
        self.cognee.search = AsyncMock(side_effect=self.search)
        self.factory_patch = patch("src.upgrade_memory.Memory._cognee", return_value=self.cognee)
        self.factory = self.factory_patch.start()
        self.import_patch = patch("src.upgrade_memory.importlib.import_module", return_value=SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="fake-chunks-type"),
        ))
        self.imports = self.import_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.addCleanup(self.import_patch.stop)

    async def add(self, texts, *, dataset_name):
        self.events.append("add")
        self.documents[dataset_name] = texts[0]
        return SimpleNamespace(status="PipelineRunCompleted", dataset_name=dataset_name)

    async def cognify(self, *, datasets, run_in_background, chunk_size):
        self.events.append("cognify")
        self.assertFalse(run_in_background)
        self.assertEqual(chunk_size, 8192)
        return {datasets[0]: SimpleNamespace(status="PipelineRunCompleted", dataset_name=datasets[0])}

    async def search(self, **kwargs):
        self.events.append("search")
        dataset = kwargs["datasets"][0]
        return [{"dataset_name": dataset, "search_result": self.documents[dataset]}]

    def index(self):
        return json.loads(self.path.read_text())

    async def store(self, value=None):
        return await remember_and_recall(report() if value is None else value, self.state)

    async def test_store_then_real_search_publishes_inspectable_verified_evidence(self):
        source_report = report()

        async def inspect_search(**kwargs):
            self.assertFalse(self.path.exists(), "Index must not publish before search verification")
            return await self.search(**kwargs)

        self.cognee.search.side_effect = inspect_search
        saved = await self.store(source_report)
        self.assertEqual(self.events, ["add", "cognify", "search"])
        self.assertEqual(saved["status"], "stored_and_retrieved")
        self.assertTrue(saved["dataset"].startswith(DATASET_PREFIX))
        self.assertEqual(saved["retrieved_excerpt"], self.documents[saved["dataset"]])
        self.assertLessEqual(len(saved["retrieved_excerpt"]), 6000)
        evidence = saved["evidence"]
        self.assertEqual(evidence["source"]["provenance"], source_report["source"]["provenance"])
        self.assertEqual(evidence["source"]["provider"], source_report["source"]["provider"])
        self.assertEqual(evidence["source"]["content_sha256"], source_report["source"]["content_sha256"])
        self.assertEqual(evidence["source"]["fetched_at"], "2026-09-22T00:59:00+00:00")
        for key in ("app_sha256", "test_sha256", "fixed_app_sha256", "fix_patch", "images"):
            self.assertEqual(evidence[key], source_report[key])
        self.assertIn("Field required", evidence["failure_excerpt"])
        self.assertEqual(evidence["observations"]["probe_new"]["output_sha256"], hashlib.sha256(
            source_report["results"]["probe_new"]["output_tail"].encode(),
        ).hexdigest())
        self.assertNotIn("DO NOT COPY", saved["retrieved_excerpt"])
        self.assertEqual(self.index()["entries"][0]["evidence_sha256"], saved["evidence_sha256"])
        self.assertEqual(self.index()["project"], PROJECT)
        kwargs = self.cognee.search.await_args.kwargs
        self.assertEqual(kwargs["datasets"], [saved["dataset"]])
        self.assertEqual(kwargs["query_type"], "fake-chunks-type")
        self.assertEqual(kwargs["top_k"], 6)
        self.assertTrue(kwargs["only_context"])
        self.imports.assert_called_once_with("cognee.api.v1.search")

    async def test_prior_recall_reads_cognee_and_does_not_modify_index_or_write_memory(self):
        saved = await self.store()
        before = self.path.read_bytes()
        self.cognee.add.reset_mock()
        self.cognee.cognify.reset_mock()
        self.cognee.search.reset_mock()
        recalled = await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)
        self.assertEqual(recalled["status"], "recalled")
        self.assertEqual(recalled["matches"][0]["evidence"], saved["evidence"])
        self.assertEqual(recalled["matches"][0]["retrieved_excerpt"], self.documents[saved["dataset"]])
        self.cognee.search.assert_awaited_once()
        self.cognee.add.assert_not_awaited()
        self.cognee.cognify.assert_not_awaited()
        self.assertEqual(self.path.read_bytes(), before)

    async def test_missing_index_or_different_app_or_version_never_queries_cognee(self):
        value = report()
        empty = await recall_prior(value["upgrade"], value["app_sha256"], self.state)
        self.assertEqual(empty, {"status": "not_found", "matches": []})
        self.factory.assert_not_called()
        await self.store()
        self.factory.reset_mock()
        for upgrade, app_hash in (
            (value["upgrade"], "f" * 64),
            (dict(value["upgrade"], version="==2.9.0"), value["app_sha256"]),
        ):
            result = await recall_prior(upgrade, app_hash, self.state)
            self.assertEqual(result, {"status": "not_found", "matches": []})
        self.factory.assert_not_called()

    async def test_nonconfirmed_report_never_stores(self):
        for status in ("inconclusive", "error", "pass", None):
            with self.subTest(status=status):
                self.assertEqual((await self.store({"status": status}))["status"], "not_stored")
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    async def test_claimed_confirmation_requires_complete_consistent_measured_evidence(self):
        alterations = (
            lambda r: r["results"].pop("fixed_new"),
            lambda r: r["results"]["probe_new"].update(status="pass"),
            lambda r: r["results"]["probe_old"].update(exit_code=1),
            lambda r: r["results"]["probe_new"].update(output_tail="wrong error"),
            lambda r: r.update(app_sha256="not-a-hash"),
            lambda r: r.update(fix_patch=""),
            lambda r: r["images"].update(new="mutable:tag"),
            lambda r: r["source"].update(text="Unsupported quote"),
            lambda r: r["source"].update(source_url="https://example.com/?token=fake-secret"),
            lambda r: r["source"].update(content_sha256="wrong"),
            lambda r: r["plan"]["expected_row"].update(nickname="wrong"),
        )
        for change in alterations:
            with self.subTest(change=change):
                value = report()
                change(value)
                with self.assertRaises(UpgradeMemoryError):
                    await self.store(value)
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    async def test_failed_or_background_ingestion_never_publishes_index(self):
        for stage in ("add", "cognify"):
            for status in ("PipelineRunErrored", "PipelineRunStarted"):
                with self.subTest(stage=stage, status=status):
                    self.cognee.add.side_effect = self.add
                    self.cognee.cognify.side_effect = self.cognify
                    target = getattr(self.cognee, stage)
                    target.side_effect = None
                    target.return_value = SimpleNamespace(status=status, dataset_name="wrong-dataset")
                    with self.assertRaises(UpgradeMemoryError):
                        await self.store()
                    self.assertFalse(self.path.exists())
        self.cognee.search.assert_not_awaited()

    async def test_missing_mismatched_tampered_or_oversized_retrieval_never_publishes(self):
        async def wrong_dataset(**kwargs):
            return [{"dataset_name": "commit_watch_baseline", "search_result": self.documents[kwargs["datasets"][0]]}]

        async def changed_body(**kwargs):
            dataset = kwargs["datasets"][0]
            return [{"dataset_name": dataset, "search_result": self.documents[dataset].replace('"name":"Ada"', '"name":"Grace"')}]

        async def oversized(**kwargs):
            dataset = kwargs["datasets"][0]
            return [{"dataset_name": dataset, "search_result": self.documents[dataset] + "x" * 48_000}]

        async def missing_frame(**kwargs):
            return [{"dataset_name": kwargs["datasets"][0], "search_result": "A summary with no evidence marker."}]

        for response in ([], None, {"search_result": "not per dataset"}, wrong_dataset, changed_body, oversized, missing_frame):
            with self.subTest(response=response):
                if callable(response):
                    self.cognee.search.side_effect = response
                else:
                    self.cognee.search.side_effect = None
                    self.cognee.search.return_value = response
                with self.assertRaises(UpgradeMemoryError):
                    await self.store()
                self.assertFalse(self.path.exists())

    async def test_later_failed_store_preserves_prior_index_and_can_still_recall_it(self):
        saved = await self.store()
        before = self.path.read_bytes()
        self.cognee.search.side_effect = RuntimeError("api-key=fake-secret provider payload")
        with self.assertRaises(UpgradeMemoryError) as error:
            await self.store()
        self.assertNotIn("fake-secret", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual(self.path.read_bytes(), before)
        self.cognee.search.side_effect = self.search
        recalled = await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)
        self.assertEqual(recalled["matches"][0]["dataset"], saved["dataset"])

    async def test_index_does_not_substitute_for_missing_cognee_data(self):
        await self.store()
        self.cognee.search.side_effect = None
        self.cognee.search.return_value = []
        with self.assertRaises(UpgradeMemoryError):
            await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)

    async def test_malformed_indexes_are_rejected_before_cognee(self):
        await self.store()
        good = self.index()
        self.factory.reset_mock()
        values = ["invalid json", "[]", '{"project":"one","project":"two"}']
        for change in (
            lambda index: index.update(project="another/project"),
            lambda index: index.update(entries=index["entries"] * (MAX_ENTRIES + 1)),
            lambda index: index["entries"][0].update(dataset="commit_watch_baseline"),
            lambda index: index["entries"][0].update(evidence_sha256="wrong"),
            lambda index: index["entries"][0].update(app_sha256="wrong"),
            lambda index: index["entries"][0].update(checked_at="2026-09-22"),
            lambda index: index["entries"].append(deepcopy(index["entries"][0])),
            lambda index: index["entries"][0].update(path="/unexpected/file"),
        ):
            item = deepcopy(good)
            change(item)
            values.append(json.dumps(item))
        for value in values:
            with self.subTest(value=value):
                self.path.write_text(value)
                with self.assertRaises(UpgradeMemoryError):
                    await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)
        self.factory.assert_not_called()

    async def test_symlinked_index_is_rejected_without_following_it(self):
        target = self.state / "another.json"
        target.write_text("not an index")
        self.path.symlink_to(target)
        with self.assertRaises(UpgradeMemoryError):
            await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)
        self.factory.assert_not_called()

    async def test_prior_recall_is_bounded_and_newest_first(self):
        saved = []
        for day in range(1, 6):
            value = report()
            value["checked_at"] = f"2026-09-{day:02}T01:00:00+00:00"
            saved.append(await self.store(value))
        self.cognee.search.reset_mock()
        recalled = await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)
        self.assertEqual([item["dataset"] for item in recalled["matches"]],
                         [item["dataset"] for item in reversed(saved[-3:])])
        self.assertEqual(self.cognee.search.await_count, 3)

    async def test_raw_provider_errors_never_escape_recall(self):
        await self.store()
        self.cognee.search.side_effect = RuntimeError("api-key=fake-secret")
        with self.assertRaises(UpgradeMemoryError) as error:
            await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)
        self.assertNotIn("fake-secret", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
