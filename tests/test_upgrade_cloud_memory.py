"""Cloud memory must verify remote evidence before publishing a scoped index."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from src.upgrade_memory import (
    INDEX_NAME, UpgradeMemoryError, _cloud_state,
    recall_prior, remember_and_recall,
)
from tests.test_upgrade_memory import report


class UpgradeCloudMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="secondlook-cloud-memory-")
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.environment = patch.dict(os.environ, {
            "COGNEE_MEMORY_BACKEND": "cloud", "COGNEE_TENANT_ID": "tenant-one",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.events = []
        self.documents = {}
        self.names = {}
        self.thread_ids = []
        self.main_thread = threading.get_ident()
        self.client = Mock(base_url="https://one.aws.cognee.ai")
        for method in ("add", "cognify", "wait_completed", "search", "graph_summary", "close"):
            getattr(self.client, method).side_effect = getattr(self, method)
        factory = patch("src.upgrade_memory.CogneeCloudClient.from_env", return_value=self.client)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        local = patch("src.upgrade_memory.Memory._cognee", side_effect=AssertionError("Local fallback"))
        self.local = local.start()
        self.addCleanup(local.stop)

    @property
    def index_path(self):
        return _cloud_state(self.state, self.client) / INDEX_NAME

    def event(self, name):
        self.events.append(name)
        self.thread_ids.append(threading.get_ident())

    def add(self, text, name):
        self.event("add")
        dataset_id = str(uuid4())
        self.documents[dataset_id] = text
        self.names[dataset_id] = name
        return {"id": dataset_id, "name": name}

    def cognify(self, dataset_id):
        self.event("cognify")
        self.assertIn(dataset_id, self.documents)

    def wait_completed(self, dataset_id):
        self.event("wait_completed")
        self.assertIn(dataset_id, self.documents)
        return "completed"

    def search(self, query, dataset_id, name):
        self.event("search")
        self.assertEqual(self.names[dataset_id], name)
        self.assertIn(self.documents[dataset_id].split()[1], query)
        return [{"dataset_name": name, "search_result": self.documents[dataset_id]}]

    def graph_summary(self, dataset_id):
        self.event("graph_summary")
        return {"dataset_id": dataset_id, "num_nodes": 12, "num_edges": 15,
                "pipeline_run_id": str(uuid4()), "computed_at": "2026-09-22T03:00:00Z"}

    def close(self):
        self.event("close")

    async def store(self, value=None):
        return await remember_and_recall(report() if value is None else value, self.state)

    async def recall(self):
        return await recall_prior(report()["upgrade"], report()["app_sha256"], self.state)

    async def test_store_requires_completed_retrieval_and_graph_before_publish(self):
        def inspect_summary(dataset_id):
            self.assertFalse(self.index_path.exists())
            return self.graph_summary(dataset_id)

        self.client.graph_summary.side_effect = inspect_summary
        saved = await self.store()
        self.assertEqual(self.events, ["add", "cognify", "wait_completed", "search", "graph_summary", "close"])
        self.assertTrue(all(thread != self.main_thread for thread in self.thread_ids))
        self.assertEqual(saved["backend"], "cloud")
        self.assertEqual(saved["status"], "stored_and_retrieved")
        self.assertEqual(saved["retrieved_excerpt"], self.documents[saved["dataset_id"]])
        self.assertEqual(saved["graph_summary"]["num_nodes"], 12)
        index = json.loads(self.index_path.read_text())
        self.assertEqual(index["backend"], "cloud")
        self.assertEqual(index["entries"][0]["dataset_id"], saved["dataset_id"])
        self.assertNotIn("evidence", index["entries"][0])
        self.assertFalse((self.state / INDEX_NAME).exists())
        self.local.assert_not_called()

    async def test_recall_reads_remote_uuid_and_keeps_index_unchanged(self):
        saved = await self.store()
        before = self.index_path.read_bytes()
        self.events.clear()
        self.client.add.reset_mock()
        self.client.cognify.reset_mock()
        self.client.search.reset_mock()
        recalled = await self.recall()
        self.assertEqual(self.events, ["search", "graph_summary", "close"])
        self.assertEqual(recalled["backend"], "cloud")
        self.assertEqual(recalled["matches"][0]["evidence"], saved["evidence"])
        self.assertEqual(self.client.search.call_args.args[1:], (saved["dataset_id"], saved["dataset"]))
        self.assertEqual(self.index_path.read_bytes(), before)
        self.client.add.assert_not_called()
        self.client.cognify.assert_not_called()

    async def test_url_tenant_and_local_indexes_are_isolated(self):
        saved = await self.store()
        original_path = self.index_path
        # A malformed local index cannot affect an explicitly Cloud operation.
        (self.state / INDEX_NAME).write_text("invalid local JSON")
        self.assertEqual((await self.recall())["matches"][0]["dataset_id"], saved["dataset_id"])
        self.client.search.reset_mock()
        with patch.dict(os.environ, {"COGNEE_TENANT_ID": "tenant-two"}):
            self.assertNotEqual(self.index_path, original_path)
            self.assertEqual((await self.recall())["status"], "not_found")
        self.client.base_url = "https://two.aws.cognee.ai"
        self.assertNotEqual(self.index_path, original_path)
        self.assertEqual((await self.recall())["status"], "not_found")
        self.client.search.assert_not_called()
        self.local.assert_not_called()

    async def test_provider_failure_or_noncompletion_never_publishes_or_falls_back(self):
        for stage in ("add", "cognify", "wait_completed", "search", "graph_summary"):
            with self.subTest(stage=stage):
                method = getattr(self.client, stage)
                method.side_effect = RuntimeError("api-key=fake-secret provider-body")
                self.client.close.reset_mock()
                with self.assertRaises(UpgradeMemoryError) as error:
                    await self.store()
                self.assertNotIn("fake-secret", str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)
                self.assertFalse(self.index_path.exists())
                self.client.close.assert_called_once()
                method.side_effect = getattr(self, stage)
        self.client.wait_completed.side_effect = lambda _: "running"
        self.client.search.reset_mock()
        with self.assertRaises(UpgradeMemoryError):
            await self.store()
        self.client.search.assert_not_called()
        self.local.assert_not_called()

    async def test_mismatched_missing_or_tampered_frame_never_publishes(self):
        for mode in ("missing", "tampered", "wrong-dataset"):
            def response(query, dataset_id, name):
                text = self.documents[dataset_id]
                if mode == "missing":
                    text = "A generated summary is not evidence."
                elif mode == "tampered":
                    text = text.replace('"name":"Ada"', '"name":"Grace"')
                return [{"dataset_name": "wrong" if mode == "wrong-dataset" else name,
                         "search_result": text}]

            with self.subTest(mode=mode):
                self.client.search.side_effect = response
                with self.assertRaises(UpgradeMemoryError):
                    await self.store()
                self.assertFalse(self.index_path.exists())
        self.client.graph_summary.assert_not_called()

    async def test_empty_invalid_or_wrong_dataset_graph_never_publishes(self):
        for change in (
            {"num_nodes": 0}, {"num_edges": 0}, {"num_nodes": True},
            {"num_edges": -1}, {"dataset_id": str(uuid4())},
        ):
            with self.subTest(change=change):
                self.client.graph_summary.side_effect = lambda dataset_id: dict(self.graph_summary(dataset_id), **change)
                with self.assertRaises(UpgradeMemoryError):
                    await self.store()
                self.assertFalse(self.index_path.exists())

    async def test_failed_later_write_preserves_previous_cloud_index(self):
        saved = await self.store()
        before = self.index_path.read_bytes()
        self.client.graph_summary.side_effect = RuntimeError("unavailable")
        with self.assertRaises(UpgradeMemoryError):
            await self.store()
        self.assertEqual(self.index_path.read_bytes(), before)
        self.client.graph_summary.side_effect = self.graph_summary
        self.assertEqual((await self.recall())["matches"][0]["dataset_id"], saved["dataset_id"])

    async def test_index_does_not_replace_remote_retrieval(self):
        await self.store()
        self.client.search.side_effect = lambda *args: []
        with self.assertRaises(UpgradeMemoryError):
            await self.recall()
        self.local.assert_not_called()

    async def test_cloud_index_requires_uuid_and_backend_before_remote_calls(self):
        await self.store()
        good = json.loads(self.index_path.read_text())
        for change in (
            lambda item: item["entries"][0].pop("dataset_id"),
            lambda item: item["entries"][0].update(dataset_id="not-a-uuid"),
            lambda item: item.update(backend="local"),
            lambda item: item["entries"].append(deepcopy(item["entries"][0])),
        ):
            with self.subTest(change=change):
                value = deepcopy(good)
                change(value)
                self.index_path.write_text(json.dumps(value))
                self.client.search.reset_mock()
                with self.assertRaises(UpgradeMemoryError):
                    await self.recall()
                self.client.search.assert_not_called()

    async def test_scoped_index_symlink_is_not_followed(self):
        target = self.state / "outside"
        target.mkdir()
        (self.state / "upgrade-cloud").symlink_to(target, target_is_directory=True)
        with self.assertRaises(UpgradeMemoryError):
            await self.store()
        self.client.add.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    async def test_unknown_backend_fails_before_cloud_or_local_io(self):
        for backend in ("", "automatic", "CLOUD", " local "):
            with self.subTest(backend=backend), patch.dict(os.environ, {"COGNEE_MEMORY_BACKEND": backend}):
                with self.assertRaises(UpgradeMemoryError):
                    await self.store()
                with self.assertRaises(UpgradeMemoryError):
                    await self.recall()
        self.factory.assert_not_called()
        self.local.assert_not_called()

    async def test_invalid_fact_does_not_create_cloud_client(self):
        value = report()
        value["results"]["probe_new"]["status"] = "pass"
        with self.assertRaises(UpgradeMemoryError):
            await self.store(value)
        self.factory.assert_not_called()

    async def test_cloud_configuration_error_never_falls_back_to_local(self):
        self.factory.side_effect = ValueError("api-key=fake-secret")
        for operation in (self.store, self.recall):
            with self.assertRaises(UpgradeMemoryError) as error:
                await operation()
            self.assertNotIn("fake-secret", str(error.exception))
        self.local.assert_not_called()
        self.client.add.assert_not_called()
        self.assertEqual(list(self.state.iterdir()), [])

    async def test_recall_is_bounded_and_matches_exact_app_and_version(self):
        saved = []
        for day in range(1, 5):
            value = report()
            value["checked_at"] = f"2026-09-{day:02}T00:00:00Z"
            saved.append(await self.store(value))
        self.client.search.reset_mock()
        recalled = await self.recall()
        self.assertEqual([item["dataset_id"] for item in recalled["matches"]],
                         [item["dataset_id"] for item in reversed(saved[-3:])])
        self.assertEqual(self.client.search.call_count, 3)
        self.client.search.reset_mock()
        for upgrade, app_hash in (
            (dict(report()["upgrade"], version="==2.9.0"), report()["app_sha256"]),
            (report()["upgrade"], "f" * 64),
        ):
            self.assertEqual((await recall_prior(upgrade, app_hash, self.state))["status"], "not_found")
        self.client.search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
