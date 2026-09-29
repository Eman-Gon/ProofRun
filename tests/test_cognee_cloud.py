"""Cloud REST contracts tested without importing Cognee or making requests."""

import json
import unittest
from unittest.mock import Mock, patch

import requests

from src.cognee_cloud import CogneeCloudClient, CogneeCloudError, MAX_RESPONSE_BYTES


URL = "https://secondlook-test.aws.cognee.ai"
KEY = "test-secret-key-do-not-print"
ID = "55a0115c-da7b-5ece-9f5a-a085a4785838"
OTHER_ID = "f8bfaae0-2fd8-5895-ba99-4ae5582fe70f"
NAME = "secondlook_compatibility_" + "a" * 32


def response(value=None, *, status=200, raw=None, headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.iter_content.return_value = [json.dumps(value).encode() if raw is None else raw]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def run(status="PipelineRunCompleted"):
    return {"status": status, "dataset_id": ID, "dataset_name": NAME}


def search_row(**extra):
    return {"dataset_id": ID, "dataset_name": NAME, "search_result": "unaltered evidence", **extra}


class CogneeCloudTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch("src.cognee_cloud.requests.Session")
        self.session_factory = self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.session = self.session_factory.return_value
        self.session.headers = {}
        self.client = CogneeCloudClient(URL, KEY, tenant_id="tenant-test")

    def test_auth_is_header_only_and_environment_proxies_disabled(self):
        self.session.request.return_value = response(run())
        self.client.add("verified text", NAME)
        self.assertFalse(self.session.trust_env)
        self.assertEqual(self.session.headers["X-Api-Key"], KEY)
        self.assertEqual(self.session.headers["X-Tenant-Id"], "tenant-test")
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("POST", URL + "/api/v1/add"))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertNotIn(KEY, repr((args, kwargs)))
        self.assertNotIn(KEY, repr(self.client))

    def test_env_configuration_does_not_load_dotenv_or_local_cognee(self):
        with patch.dict("os.environ", {"COGNEE_API_URL": URL, "COGNEE_API_KEY": KEY}, clear=True):
            self.assertEqual(CogneeCloudClient.from_env().base_url, URL)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(CogneeCloudError, "COGNEE_API_URL"):
                CogneeCloudClient.from_env()

    def test_only_exact_https_tenant_origin_allowed(self):
        for url in ["http://secondlook-test.aws.cognee.ai", "https://aws.cognee.ai",
                    "https://secondlook-test.aws.cognee.ai.evil.test", "https://user:pass@secondlook-test.aws.cognee.ai",
                    URL + ":443", URL + "/api", URL + "?token=secret", URL + "#fragment",
                    "https://nested.tenant.aws.cognee.ai", "https://127.0.0.1", None]:
            with self.subTest(url=url), self.assertRaises(CogneeCloudError):
                CogneeCloudClient(url, KEY)
        self.assertEqual(CogneeCloudClient(URL + "/", KEY).base_url, URL)

    def test_header_injection_rejected_before_session_construction(self):
        before = self.session_factory.call_count
        for key in ["", "key\r\nX-Leak: yes", "has space", "é", "x" * 4097]:
            with self.subTest(key=key), self.assertRaises(CogneeCloudError):
                CogneeCloudClient(URL, key)
        self.assertEqual(self.session_factory.call_count, before)

    def test_add_multipart_uses_content_unique_filename_and_checks_scope(self):
        self.session.request.return_value = response(run())
        self.assertEqual(self.client.add("first evidence", NAME), {"id": ID, "name": NAME})
        first = self.session.request.call_args.kwargs
        self.assertEqual(first["data"], {"datasetName": NAME})
        self.assertEqual(first["files"]["data"][1:], (b"first evidence", "text/plain"))
        self.client.add("second evidence", NAME)
        self.assertNotEqual(first["files"]["data"][0], self.session.request.call_args.kwargs["files"]["data"][0])
        for item in [run("PipelineRunStarted"), {**run(), "dataset_name": "other"},
                     {**run(), "dataset_id": "name-not-id"}]:
            self.session.request.return_value = response(item)
            with self.assertRaises(CogneeCloudError):
                self.client.add("evidence", NAME)

    def test_add_rejects_oversized_utf8_or_invalid_name_without_request(self):
        for text, name in [("é" * 3001, NAME), ("", NAME), ("evidence", "../other")]:
            with self.assertRaises(CogneeCloudError):
                self.client.add(text, name)
        self.session.request.assert_not_called()

    def test_cognify_uses_uuid_background_and_chunk_size_but_not_completion(self):
        self.session.request.return_value = response({ID: run("PipelineRunStarted")})
        self.assertIsNone(self.client.cognify(ID))
        self.assertEqual(self.session.request.call_args.kwargs["json"], {
            "datasetIds": [ID], "runInBackground": True, "chunkSize": 8192,
        })
        for item in [{OTHER_ID: run()}, {ID: run("PipelineRunErrored")}, {ID: {**run(), "dataset_id": OTHER_ID}}]:
            self.session.request.return_value = response(item)
            with self.assertRaises(CogneeCloudError):
                self.client.cognify(ID)

    def test_wait_polls_until_explicit_dataset_completion(self):
        self.session.request.side_effect = [response({}), response({ID: "DATASET_PROCESSING_STARTED"}),
                                            response({ID: "DATASET_PROCESSING_COMPLETED"})]
        with patch("src.cognee_cloud.time.sleep") as sleep:
            self.assertEqual(self.client.wait_completed(ID), "completed")
        self.assertEqual(sleep.call_count, 2)
        for call in self.session.request.call_args_list:
            self.assertEqual(call.kwargs["params"], {"dataset": ID, "pipeline": "cognify_pipeline"})

    def test_wait_rejects_failed_unknown_or_wrong_dataset_status(self):
        for item in [{ID: "DATASET_PROCESSING_ERRORED"}, {ID: "failed"},
                     {ID: "success"}, {OTHER_ID: "completed"}, {ID: {"status": "completed"}}]:
            self.session.request.return_value = response(item)
            with self.subTest(item=item), self.assertRaises(CogneeCloudError):
                self.client.wait_completed(ID)

    def test_wait_deadline_is_bounded_even_without_provider_status(self):
        clock = [100.0]
        self.session.request.return_value = response({ID: "running"})
        with patch("src.cognee_cloud.time.monotonic", side_effect=lambda: clock[0]), \
                patch("src.cognee_cloud.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaisesRegex(CogneeCloudError, "deadline"):
                self.client.wait_completed(ID, timeout=5, poll_interval=2)
        self.assertEqual(clock[0], 105.0)
        self.assertEqual(self.session.request.call_count, 3)
        for timeout, interval in [(181, 2), (0, 2), (10, 6), (float("nan"), 2)]:
            with self.assertRaises(CogneeCloudError):
                self.client.wait_completed(ID, timeout=timeout, poll_interval=interval)

    def test_search_preserves_raw_context_and_sends_uuid_scope(self):
        for result in [[search_row()],
                       [{"datasetId": ID, "datasetName": NAME, "searchResult": "unaltered evidence"}]]:
            self.session.request.return_value = response(result)
            self.assertEqual(self.client.search("evidence hash", ID, NAME), [
                {"dataset_name": NAME, "search_result": "unaltered evidence"},
            ])
        self.assertEqual(self.session.request.call_args.kwargs["json"], {
            "query": "evidence hash", "searchType": "CHUNKS", "datasetIds": [ID], "topK": 6, "onlyContext": True,
        })

    def test_raw_chunk_payloads_preserve_complete_frame_and_original_order(self):
        digest = "a" * 64
        frame = (f"SECONDLOOK_VERIFIED_EVIDENCE_V1 {digest}\n"
                 '{"fact":"exact raw evidence"}\n'
                 f"SECONDLOOK_VERIFIED_EVIDENCE_END {digest}")
        chunks = [{"text": "prefix context", "score": 0.1},
                  {"text": frame, "score": 0.2, "id": OTHER_ID}]
        self.session.request.return_value = response([search_row(search_result=chunks)])
        result = self.client.search("evidence hash", ID, NAME)
        self.assertEqual(result[0]["search_result"], "prefix context\n" + frame)
        self.assertIn(frame, result[0]["search_result"])

    def test_raw_chunk_payloads_never_substitute_metadata_for_missing_text(self):
        for chunks in [[], [{"score": 0.1, "content": "not raw text"}], [{"text": None}],
                       [{"text": ""}], ["string without a payload"], [{"text": "valid"}, {"score": 0.1}],
                       [{"text": "x"}] * 7, [{"text": "x" * 48001}]]:
            self.session.request.return_value = response([search_row(search_result=chunks)])
            with self.subTest(chunks=len(chunks)), self.assertRaises(CogneeCloudError):
                self.client.search("evidence hash", ID, NAME)

    def test_search_rejects_wrong_scope_errors_and_noncontext(self):
        for result in [[search_row(dataset_id=OTHER_ID)], [search_row(dataset_name="other")],
                       [search_row(error=KEY)], [search_row(search_result=[{"content": "untrusted"}])],
                       [search_row(datasetId=OTHER_ID)], [search_row(search_result="x" * 48001)],
                       ["unscoped text"], [], {"results": [search_row()]}, {"data": [search_row()]}]:
            self.session.request.return_value = response(result)
            with self.subTest(shape=type(result)), self.assertRaises(CogneeCloudError) as caught:
                self.client.search("evidence hash", ID, NAME)
            self.assertNotIn(KEY, str(caught.exception))

    def test_dataset_names_cannot_be_sent_in_place_of_uuid(self):
        for operation in [lambda: self.client.cognify(NAME), lambda: self.client.wait_completed(NAME),
                          lambda: self.client.search("query", NAME, NAME), lambda: self.client.graph_summary(NAME)]:
            with self.assertRaises(CogneeCloudError):
                operation()
        self.session.request.assert_not_called()

    def test_graph_counts_are_scoped_and_cached_counts_do_not_trigger_fallback(self):
        row = {"datasetId": ID, "numNodes": 22, "numEdges": 29,
               "pipelineRunId": OTHER_ID, "computedAt": "2026-09-22T00:00:00Z"}
        self.session.request.return_value = response([row])
        summary = self.client.graph_summary(ID)
        self.assertEqual((summary["num_nodes"], summary["num_edges"]), (22, 29))
        self.assertEqual(summary["counts_source"], "graph_summary")
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.session.request.call_args.kwargs["params"], {"dataset_ids": ID})
        self.session.request.return_value = response([{**row, "numNodes": 0, "numEdges": 0}])
        self.assertEqual(self.client.graph_summary(ID)["num_nodes"], 0)
        self.assertEqual(self.session.request.call_count, 2)
        for bad in [[{**row, "datasetId": OTHER_ID}], [{**row, "numNodes": True}],
                    [{**row, "numEdges": -1}], [row, row], []]:
            self.session.request.return_value = response(bad)
            with self.assertRaises(CogneeCloudError):
                self.client.graph_summary(ID)

    def test_uncached_summary_counts_actual_scoped_graph_and_preserves_provenance(self):
        uncached = [{"datasetId": ID, "numNodes": 0, "numEdges": 0,
                     "pipelineRunId": OTHER_ID, "computedAt": None}]
        graph = {"nodes": [{"id": ID, "label": "one", "type": "Entity", "properties": {}},
                           {"id": OTHER_ID, "label": "two", "type": "Entity", "properties": {}}],
                 "edges": [{"source": ID, "target": OTHER_ID, "label": "depends_on"}]}
        self.session.request.side_effect = [response(uncached), response(graph)]
        summary = self.client.graph_summary(ID)
        self.assertEqual((summary["num_nodes"], summary["num_edges"]), (2, 1))
        self.assertEqual(summary["counts_source"], "dataset_graph")
        self.assertIsNone(summary["computed_at"])
        self.assertEqual(self.session.request.call_args.args, ("GET", URL + f"/api/v1/datasets/{ID}/graph"))
        self.assertNotIn("nodes", summary)
        self.assertNotIn("edges", summary)
        self.session.request.side_effect = [response(uncached), response({"nodes": [], "edges": []})]
        empty = self.client.graph_summary(ID)
        self.assertEqual((empty["num_nodes"], empty["num_edges"]), (0, 0))

    def test_graph_fallback_rejects_malformed_incomplete_or_oversized_graph(self):
        uncached = [{"datasetId": ID, "numNodes": 0, "numEdges": 0, "computedAt": None}]
        node = {"id": ID, "label": "one", "type": "Entity", "properties": {}}
        edge = {"source": ID, "target": OTHER_ID, "label": "depends_on"}
        for graph in [{"nodes": [], "edges": [edge]}, {"nodes": [node, node], "edges": []},
                      {"nodes": [{**node, "id": "bad-id"}], "edges": []},
                      {"nodes": [{**node, "properties": []}], "edges": []},
                      {"nodes": [node], "edges": [edge]}, {"nodes": [node]},
                      {"nodes": [node], "edges": [], "truncated": True}]:
            self.session.request.side_effect = [response(uncached), response(graph)]
            with self.subTest(graph_keys=list(graph)), self.assertRaises(CogneeCloudError):
                self.client.graph_summary(ID)
        self.session.request.side_effect = [response(uncached), response(raw=b"x" * (MAX_RESPONSE_BYTES + 1))]
        with self.assertRaises(CogneeCloudError):
            self.client.graph_summary(ID)

    def test_graph_fallback_http_failure_stays_failure(self):
        uncached = [{"datasetId": ID, "numNodes": 0, "numEdges": 0, "computedAt": None}]
        self.session.request.side_effect = [response(uncached), response({"error": KEY}, status=500)]
        with self.assertRaises(CogneeCloudError) as caught:
            self.client.graph_summary(ID)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(self.session.request.call_count, 2)

    def test_redirects_http_and_transport_errors_never_include_remote_secret(self):
        for status in [302, 307, 401, 403, 429, 500]:
            self.session.request.return_value = response({"error": KEY}, status=status,
                                                        headers={"Location": "https://evil.test/?key=" + KEY})
            with self.assertRaises(CogneeCloudError) as caught:
                self.client.add("evidence", NAME)
            self.assertNotIn(KEY, str(caught.exception))
            self.session.request.return_value.iter_content.assert_not_called()
        self.session.request.side_effect = requests.RequestException(KEY)
        with self.assertRaises(CogneeCloudError) as caught:
            self.client.add("evidence", NAME)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_json_size_duplicate_keys_and_invalid_encoding_fail_closed(self):
        for raw, headers in [(b"x" * (MAX_RESPONSE_BYTES + 1), {}), (b"{}", {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
                             (b'{"status":1,"status":2}', {}), (b'{"status":NaN}', {}),
                             (b'\xff', {}), (b'[]', {})]:
            self.session.request.return_value = response(raw=raw, headers=headers)
            with self.assertRaises(CogneeCloudError):
                self.client.add("evidence", NAME)

    def test_context_manager_closes_session(self):
        with self.client as client:
            self.assertIs(client, self.client)
        self.session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
