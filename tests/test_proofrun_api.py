"""Real local HTTP transport tests with a stub service; no Docker or providers."""
import contextlib
import http.client
import io
import json
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from src.proofrun.api import MAX_BODY_BYTES, create_server


TOKEN = "transport-test-token-not-a-real-secret"


class ProofRunAPITests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.run = {"schema_version": "proofrun.v1", "run_id": "run-123", "execution_status": "queued"}
        self.service.submit.return_value = (self.run, True)
        self.service.get_run.return_value = self.run
        self.service.get_case.return_value = {"case_id": "customer-nickname-v1"}
        self.service.artifact.return_value = (b"measured evidence\n", "text/plain; charset=utf-8")
        self.research = Mock()
        self.research.submit.return_value = {
            "schema_version": "proofrun.research.v1", "research_id": "research-example",
            "status": "unavailable", "provider": "similarweb",
        }
        self.server = create_server(self.service, TOKEN, port=0, research=self.research)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, *, payload=None, body=None, auth=TOKEN, headers=None):
        outgoing = {}
        if auth is not None:
            outgoing["Authorization"] = "Bearer " + auth
        if method == "POST":
            outgoing["Content-Type"] = "application/json"
        if headers:
            outgoing.update(headers)
        if payload is not None:
            body = json.dumps(payload).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, response.read()

    def test_health_is_minimal_and_does_not_require_authentication(self):
        status, headers, body = self.request("GET", "/health", auth=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"schema_version": "proofrun.v1", "status": "ok"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.service.mock_calls, [])

    def test_every_worker_route_requires_one_valid_token(self):
        for method, path in (("POST", "/v1/runs"), ("POST", "/v1/customer-research"), ("GET", "/v1/cases/customer-nickname-v1"),
                             ("GET", "/v1/runs/run-123"), ("GET", "/v1/runs/run-123/artifacts/results.json")):
            for auth in (None, "wrong-token"):
                with self.subTest(method=method, path=path, auth=auth):
                    status, headers, body = self.request(method, path, payload={} if method == "POST" else None, auth=auth)
                    self.assertEqual(status, 401)
                    self.assertIn("WWW-Authenticate", headers)
                    self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")
                    self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.service.mock_calls, [])
        self.research.submit.assert_not_called()

    def test_research_is_explicit_authenticated_and_separate_from_verification(self):
        payload = {"request_id": "duplo-research-example", "domain": "example.com", "month": "2026-08"}
        status, _, body = self.request("POST", "/v1/customer-research", payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.research.submit.return_value)
        self.research.submit.assert_called_once_with(payload)
        self.assertEqual(self.service.mock_calls, [])
        self.assertEqual(self.request("GET", "/v1/customer-research")[0], 404)

    def test_research_errors_do_not_expose_provider_response_or_credentials(self):
        from src.proofrun.research import ResearchError
        self.research.submit.side_effect = ResearchError(409, "request_id_conflict", "Research request identity already used.")
        status, _, body = self.request("POST", "/v1/customer-research", payload={})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "request_id_conflict")
        self.research.submit.side_effect = RuntimeError("secret-provider-key")
        status, _, body = self.request("POST", "/v1/customer-research", payload={})
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret-provider-key", body)
        self.assertEqual(self.service.mock_calls, [])

    def test_research_rejects_ambiguous_input_before_any_provider_call(self):
        status, _, _ = self.request("POST", "/v1/customer-research", body=b'{"domain":"one.com","domain":"two.com"}')
        self.assertEqual(status, 400)
        self.research.submit.assert_not_called()

    def test_duplicate_authorization_is_rejected(self):
        status, _ = self.raw((f"GET /v1/runs/run-123 HTTP/1.1\r\nHost: localhost\r\n"
                              f"Authorization: Bearer {TOKEN}\r\nAuthorization: Bearer {TOKEN}\r\n\r\n").encode())
        self.assertEqual(status, 401)
        self.service.get_run.assert_not_called()

    def test_submission_is_async_202_and_idempotent_200(self):
        payload = {"job_key": "demo-1"}
        status, _, body = self.request("POST", "/v1/runs", payload=payload)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), self.run)
        self.service.submit.assert_called_once_with(payload)
        self.service.submit.return_value = (self.run, False)
        status, _, body = self.request("POST", "/v1/runs", payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.run)

    def test_case_run_and_artifact_responses_call_only_the_service(self):
        status, _, body = self.request("GET", "/v1/cases/customer-nickname-v1")
        self.assertEqual((status, json.loads(body)), (200, {"case_id": "customer-nickname-v1"}))
        self.service.get_case.assert_called_once_with("customer-nickname-v1")
        status, _, body = self.request("GET", "/v1/runs/run-123")
        self.assertEqual((status, json.loads(body)), (200, self.run))
        self.service.get_run.assert_called_once_with("run-123")
        status, headers, body = self.request("GET", "/v1/runs/run-123/artifacts/results.json")
        self.assertEqual((status, body), (200, b"measured evidence\n"))
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.service.artifact.assert_called_once_with("run-123", "results.json")

    def test_malformed_and_ambiguous_json_never_reaches_the_service(self):
        for body in (b"", b"[1]", b"null", b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}',
                     b'{"a":NaN}', b'{"a":Infinity}', b"\xff", b"{}{}", b'{"a":' + b"[" * 1100):
            with self.subTest(body=body[:80]):
                status, _, response = self.request("POST", "/v1/runs", body=body)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(response)["error"]["code"], "invalid_json")
        self.service.submit.assert_not_called()

    def test_body_size_and_media_type_are_bounded(self):
        for body, headers, expected in (
            (b"x" * (MAX_BODY_BYTES + 1), {}, 413),
            (b"{}", {"Content-Length": "-1"}, 400),
            (b"{}", {"Content-Length": "1, 1"}, 400),
            (b"{}", {"Content-Type": "text/plain"}, 415),
            (b"{}", {"Transfer-Encoding": "chunked"}, 400),
        ):
            with self.subTest(headers=headers, expected=expected):
                status, _, _ = self.request("POST", "/v1/runs", body=body, headers=headers)
                self.assertEqual(status, expected)
        self.service.submit.assert_not_called()

    def test_missing_duplicate_and_truncated_lengths_are_rejected(self):
        for extra, body, expected in (("", b"", 411),
                                      ("Content-Length: 2\r\nContent-Length: 2\r\n", b"{}", 400),
                                      ("Content-Length: 20\r\n", b"{}", 400)):
            with self.subTest(extra=extra):
                request = (f"POST /v1/runs HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
                           f"Content-Type: application/json\r\n{extra}\r\n").encode() + body
                status, _ = self.raw(request)
                self.assertEqual(status, expected)
        self.service.submit.assert_not_called()

    def test_slow_body_times_out_without_calling_service(self):
        with patch("src.proofrun.api.REQUEST_TIMEOUT_SECONDS", 0.05):
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
                connection.sendall((f"POST /v1/runs HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
                                    "Content-Type: application/json\r\nContent-Length: 20\r\n\r\n{").encode())
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 408)
                self.assertEqual(json.loads(response.read())["error"]["code"], "request_timeout")
        self.service.submit.assert_not_called()

    def test_unknown_and_traversal_paths_never_reach_service(self):
        for path in ("/v1/runs", "/v1/runs/run-123/", "/v1/runs/run-123?token=secret",
                     "/v1/runs/run-123/artifacts/../secret", "/v1/runs/run-123/artifacts/..",
                     "/v1/runs/run-123/artifacts/%2e%2e", "/v1/runs/run-123/artifacts/a%2fb",
                     "/v1/runs/run-123/artifacts/a/b", "/v2/runs/run-123"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.service.mock_calls, [])

    def test_service_validation_and_conflict_errors_remain_distinct(self):
        from src.proofrun.service import ServiceError
        for status, code in ((400, "invalid_binding"), (404, "case_not_found"), (409, "job_key_conflict")):
            with self.subTest(status=status):
                self.service.submit.side_effect = ServiceError(status, code, "Safe public error.")
                actual, _, body = self.request("POST", "/v1/runs", payload={})
                self.assertEqual(actual, status)
                self.assertEqual(json.loads(body)["error"], {"code": code, "message": "Safe public error."})

    def test_internal_errors_and_unsupported_methods_do_not_expose_request_data(self):
        self.service.get_run.side_effect = RuntimeError("hidden credential or path")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            status, _, body = self.request("GET", "/v1/runs/run-123")
            self.assertEqual(status, 500)
            self.assertNotIn(b"hidden credential", body)
            status, _, body = self.request("PRIVATE-SECRET", "/v1/runs/run-123")
            self.assertEqual(status, 405)
            self.assertNotIn(b"PRIVATE-SECRET", body)
        self.assertEqual(captured.getvalue(), "")

    def test_token_is_required_and_never_enters_service(self):
        for token in (None, "", "contains space", "contains\nnewline", "\N{SNOWMAN}", "a" * 513):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    create_server(self.service, token, port=0)
        self.assertEqual(self.service.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
