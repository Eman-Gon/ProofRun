"""Offline Similarweb transport/persistence checks; no live access is established."""
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from src.proofrun.research import (
    MAX_RESPONSE_BYTES, ResearchError, ResearchService, SimilarwebClient,
)

KEY = "synthetic-similarweb-credential"
NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
PAYLOAD = {"request_id": "meeting-123", "domain": "www.Example.com", "month": "2026-08"}


def envelope(visits=12345.5):
    return {
        "meta": {"status": "Success", "request": {"domain": "example.com", "country": "world",
                 "start_date": "2026-08-01", "end_date": "2026-08-31", "granularity": "Monthly",
                 "api_key": KEY}},
        "visits": [{"date": "2026-08-01", "visits": visits}],
    }


class Response:
    def __init__(self, body=None, status=200, raw=None, headers=None, error=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = raw if raw is not None else json.dumps(envelope() if body is None else body).encode()
        self.error = error
        self.closed = False

    def iter_content(self, chunk_size):
        if self.error:
            raise self.error
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else Response()
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def client(response=None, error=None, key=KEY):
    transport = Transport(response, error)
    return SimilarwebClient(key, transport), transport


class SimilarwebClientTests(unittest.TestCase):
    def test_documented_single_month_request_is_bounded_and_normalized(self):
        provider, transport = client()
        result = provider.fetch("www.Example.com", "2026-08")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["metrics"], [{"name": "estimated_visits", "value": 12345.5, "unit": "visits"}])
        self.assertEqual(result["response_sha256"], hashlib.sha256(transport.response.raw).hexdigest())
        url, kwargs = transport.calls[0]
        self.assertEqual(url, "https://api.similarweb.com/v1/website/example.com/total-traffic-and-engagement/visits")
        self.assertEqual(kwargs["params"]["api_key"], KEY)
        self.assertEqual(kwargs["params"]["country"], "world")
        self.assertEqual(kwargs["params"]["start_date"], kwargs["params"]["end_date"])
        self.assertEqual(kwargs["params"]["granularity"], "monthly")
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertTrue(transport.response.closed)
        self.assertNotIn(KEY, json.dumps(result))

    def test_missing_or_invalid_keys_do_not_send_any_request(self):
        for key in (None, "", "secret\n", "secret key", 5, [], "x" * 513):
            with self.subTest(kind=type(key).__name__):
                provider, transport = client(key=key)
                result = provider.fetch("example.com", "2026-08")
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(transport.calls, [])
        self.assertEqual(SimilarwebClient.from_env({}).fetch("example.com", "2026-08")["error"]["code"], "not_configured")

    def test_auth_credit_redirect_and_http_failures_never_echo_body(self):
        for status, code in ((401, "access_denied"), (403, "access_denied"), (429, "rate_limited"),
                             (302, "provider_error"), (404, "provider_error"), (500, "provider_error")):
            with self.subTest(status=status):
                response = Response(status=status, raw=(KEY + " sensitive provider text").encode())
                provider, transport = client(response)
                result = provider.fetch("example.com", "2026-08")
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(KEY, json.dumps(result))
                self.assertTrue(response.closed)

    def test_empty_and_null_are_no_data_zero_is_a_valid_estimate(self):
        for body in ({"visits": []}, envelope(None)):
            provider, _ = client(Response(body))
            result = provider.fetch("example.com", "2026-08")
            self.assertEqual(result["status"], "no_data")
            self.assertEqual(result["metrics"], [])
        provider, _ = client(Response(envelope(0)))
        self.assertEqual(provider.fetch("example.com", "2026-08")["status"], "completed")

    def test_malformed_or_conflicting_provider_values_do_not_become_metrics(self):
        bodies = [[], {}, {"visits": None}, {"visits": [None]}, {"visits": [{"date": "2026-07-01", "visits": 5}]},
                  {"visits": [{"date": "2026-08-01"}]}, envelope(-1), envelope(True), envelope("999"),
                  envelope(float("nan")), envelope(float("inf"))]
        for field, value in (("domain", "different.com"), ("country", "us"), ("start_date", "2026-07-01"),
                             ("end_date", "2026-09-30"), ("granularity", "Daily")):
            body = envelope()
            body["meta"]["request"][field] = value
            bodies.append(body)
        duplicate_rows = envelope()
        duplicate_rows["visits"] *= 2
        bodies.extend([duplicate_rows, {"meta": {"status": "Error"}, "visits": []}])
        for body in bodies:
            with self.subTest(body=body):
                provider, _ = client(Response(body))
                result = provider.fetch("example.com", "2026-08")
                self.assertEqual(result["error"]["code"], "invalid_response")
                self.assertEqual(result["metrics"], [])
                self.assertNotIn(KEY, json.dumps(result))

    def test_invalid_json_duplicate_keys_and_size_limit_fail_safely(self):
        responses = [Response(raw=b"not JSON"), Response(raw=b'{"visits":[],"visits":[]}'),
                     Response(raw=b"\xff"), Response(raw=b"x" * (MAX_RESPONSE_BYTES + 1)),
                     Response(headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
                     Response(headers={"Content-Length": "bad"})]
        for response in responses:
            with self.subTest(size=len(response.raw)):
                provider, _ = client(response)
                self.assertEqual(provider.fetch("example.com", "2026-08")["error"]["code"], "invalid_response")
                self.assertTrue(response.closed)

    def test_network_and_stream_errors_are_sanitized_without_retry(self):
        for failure, code in ((requests.Timeout(KEY), "timeout"),
                              (requests.ConnectionError("url?api_key=" + KEY), "connection_failed")):
            for at_stream in (False, True):
                provider, transport = (client(Response(error=failure)) if at_stream else client(error=failure))
                result = provider.fetch("example.com", "2026-08")
                self.assertEqual(result["error"]["code"], code)
                self.assertNotIn(KEY, json.dumps(result))
                self.assertEqual(len(transport.calls), 1)
        provider, _ = client()
        with patch("src.proofrun.research.time.monotonic", side_effect=[0, 31]):
            self.assertEqual(provider.fetch("example.com", "2026-08")["error"]["code"], "timeout")

    def test_verbose_transport_logging_cannot_leak_query_key(self):
        output = io.StringIO()
        logger = logging.getLogger("urllib3.connectionpool")
        original_level = logger.level
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        class LoggingTransport(Transport):
            def get(self, url, **kwargs):
                logger.debug('GET %s?api_key=%s', url, kwargs["params"]["api_key"])
                return super().get(url, **kwargs)

        try:
            provider = SimilarwebClient(KEY, LoggingTransport())
            self.assertEqual(provider.fetch("example.com", "2026-08")["status"], "completed")
            logger.info("Normal logging restored")
            self.assertNotIn(KEY, output.getvalue())
            self.assertIn("Normal logging restored", output.getvalue())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)


class ResearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "research"
        self.provider, self.transport = client()
        self.service = ResearchService(self.path, self.provider, lambda: NOW)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def test_report_contains_provenance_but_no_raw_provider_metadata(self):
        report = self.service.submit(PAYLOAD)
        self.assertEqual(report["schema_version"], "proofrun.research.v1")
        self.assertEqual(report["request_id"], PAYLOAD["request_id"])
        self.assertEqual(report["domain"], "example.com")
        self.assertEqual(report["period"], {"start_date": "2026-08-01", "end_date": "2026-08-31"})
        self.assertEqual(report["observed_at"], NOW.isoformat())
        self.assertEqual(report["provider"], "similarweb")
        self.assertEqual(self.service.get_report(report["research_id"]), report)
        self.assertTrue(all(source["url"].startswith("https://") for source in report["sources"]))
        stored = (self.path / (report["research_id"] + ".json")).read_text()
        self.assertNotIn(KEY, stored)
        self.assertNotIn("api_key", stored)
        self.assertEqual((self.path / (report["research_id"] + ".json")).stat().st_mode & 0o777, 0o600)
        normalized = {**PAYLOAD, "domain": "example.com"}
        self.assertEqual(report["request_sha256"], hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_idempotent_replay_normalizes_domain_and_survives_restart(self):
        report = self.service.submit(PAYLOAD)
        self.assertEqual(self.service.submit({**PAYLOAD, "domain": "example.com"}), report)
        self.assertEqual(len(self.transport.calls), 1)
        self.service.close()
        provider, transport = client(error=AssertionError("A replay must never fetch"))
        self.service = ResearchService(self.path, provider, lambda: NOW)
        self.assertEqual(self.service.submit(PAYLOAD), report)
        self.assertEqual(transport.calls, [])

    def test_conflicting_id_does_not_issue_a_second_provider_request(self):
        self.service.submit(PAYLOAD)
        for changes in ({"domain": "different.com"}, {"month": "2026-07"}):
            with self.assertRaises(ResearchError) as error:
                self.service.submit({**PAYLOAD, **changes})
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.transport.calls), 1)

    def test_claim_survives_uncertain_crash_without_repeating_lookup(self):
        self.provider.fetch = lambda *_: (_ for _ in ()).throw(RuntimeError("simulated interruption"))
        with self.assertRaises(RuntimeError):
            self.service.submit(PAYLOAD)
        self.service.close()
        provider, transport = client()
        self.service = ResearchService(self.path, provider, lambda: NOW)
        report = self.service.submit(PAYLOAD)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["error"]["code"], "interrupted")
        self.assertEqual(transport.calls, [])

    def test_persist_failure_prevents_paid_request(self):
        with patch.object(self.service, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.submit(PAYLOAD)
        self.assertEqual(self.transport.calls, [])

    def test_simultaneous_identical_requests_issue_one_provider_call(self):
        barrier = threading.Barrier(3)
        results = []

        def submit():
            barrier.wait()
            results.append(self.service.submit(PAYLOAD))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.transport.calls), 1)

    def test_second_process_owner_is_rejected_and_closed_service_cannot_fetch(self):
        with self.assertRaises(ValueError):
            ResearchService(self.path, client()[0], lambda: NOW)
        self.service.close()
        with self.assertRaises(ResearchError) as error:
            self.service.submit(PAYLOAD)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(self.transport.calls, [])

    def test_invalid_input_never_creates_claim_or_contacts_provider(self):
        invalid = [None, [], {}, {**PAYLOAD, "url": "extra"}]
        for field, values in (
            ("request_id", ("", "../path", "x" * 129, True)),
            ("domain", ("https://example.com", "example.com/path", "example.com:443", "user@example.com",
                        "127.0.0.1", "localhost", "foo.local", "evil.com?api_key=x", " example.com", "x..com",
                        "-example.com", "example.com.", "a" * 64 + ".com", "例.com", None)),
            ("month", ("2026-8", "2026-13", "0000-01", "2026-09", "2027-01", "2026-08-01", None)),
        ):
            invalid.extend({**PAYLOAD, field: value} for value in values)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ResearchError) as error:
                self.service.submit(payload)
            self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.path.glob("*.json")), [])

    def test_month_end_handles_leap_year_and_failed_report_is_not_retried(self):
        self.provider._api_key = None
        report = self.service.submit({**PAYLOAD, "month": "2024-02"})
        self.assertEqual(report["period"]["end_date"], "2024-02-29")
        self.assertEqual(report["status"], "unavailable")
        self.provider._api_key = KEY
        self.assertEqual(self.service.submit({**PAYLOAD, "month": "2024-02"}), report)
        self.assertEqual(self.transport.calls, [])

    def test_caller_cannot_mutate_persisted_report_and_invalid_lookup_is_safe(self):
        report = self.service.submit(PAYLOAD)
        expected = copy.deepcopy(report)
        report["metrics"][0]["value"] = 9
        self.assertEqual(self.service.submit(PAYLOAD), expected)
        for identity in ("../secret", "research-" + "a" * 64, None):
            with self.assertRaises(ResearchError) as error:
                self.service.get_report(identity)
            self.assertEqual(error.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
