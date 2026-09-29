"""Synthetic provider responses only; these tests do not establish live search access."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import requests

from src.proofrun.config import RepairConfig
from src.proofrun.failure_research import (
    FailureResearchService, OpenRouterFailureResearchClient, safe_source_url, validate_query,
)
from src.proofrun.research import ResearchError

URL = "https://docs.pydantic.dev/2.8/migration/"
OTHER = "https://github.com/pydantic/pydantic/issues/5481"
QUERY = "Pydantic 1.10.18 to 2.8.2 Optional field required missing default"
KEY = "synthetic-provider-key-not-a-real-secret"
CONFIG = RepairConfig(KEY, "openai/test-model")
CONTEXT = {"kind": "fixture", "run_id": "run-recorded-test", "finding_id": "regression",
           "scope": "workspace-a", "job_key": "test-job", "revision": "a" * 40,
           "source_sha256": "b" * 64, "contract_sha256": "c" * 64,
           "evidence_sha256": "d" * 64}


def envelope():
    content = {"summary": "The migration documentation describes changed optional-field defaults.",
               "summary_source_urls": [URL],
               "suggested_fixes": [{"description": "Consider an explicit default for the optional field, then run the verifier.",
                                    "source_urls": [URL]}]}
    return {"id": "gen-test-operation", "model": CONFIG.model,
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(content), "annotations": [
                    {"type": "url_citation", "url_citation": {"url": URL, "title": "Migration guide",
                        "content": "Optional fields without a default remain required."}}]}}]}


class Response:
    def __init__(self, value=None, status=200, raw=None):
        self.raw = raw if raw is not None else json.dumps(value if value is not None else envelope()).encode()
        self.status_code, self.closed = status, False

    def iter_content(self, chunk_size):
        yield self.raw

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else Response()
        self.error, self.calls = error, []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response


def client(value=None, **kwargs):
    transport = Transport(Response(value, **kwargs))
    return OpenRouterFailureResearchClient(CONFIG, transport), transport


def changed_content(value, **changes):
    content = json.loads(value["choices"][0]["message"]["content"])
    content.update(changes)
    value["choices"][0]["message"]["content"] = json.dumps(content)
    return value


def test_query_is_the_only_customer_data_sent_and_search_is_bounded(tmp_path):
    provider, transport = client()
    service = FailureResearchService(tmp_path, provider)
    try:
        result = service.submit("request-1", QUERY, CONTEXT)
        assert result["status"] == "completed"
        assert result["context"] == CONTEXT
        assert result["suggested_fixes"][0]["source_ids"] == ["source-1"]
        assert "[source-1]" in result["summary"]
        assert result["provenance"]["mode"] == "injected"
        body = transport.calls[0][1]["json"]
        assert body["messages"][1] == {"role": "user", "content": QUERY}
        sent = json.dumps(body)
        for value in CONTEXT.values():
            if len(value) > 15:
                assert value not in sent
        assert "tools" in body and "plugins" not in body
        assert body["tools"][0] == {"type": "openrouter:web_search", "parameters": {
            "engine": "exa", "max_results": 5, "max_total_results": 5, "max_characters": 2000, "max_uses": 1}}
        assert body["max_tool_calls"] == 1
        assert transport.calls[0][1]["timeout"][1] == 25
        assert transport.calls[0][1]["allow_redirects"] is False
        assert KEY not in json.dumps(result)
        assert service.get(result["research_id"]) == result
        assert transport.response.closed
    finally:
        service.close()


@pytest.mark.parametrize("url", ["http://github.com/project/issues/1", "https://user:pass@github.com/a", "https://localhost/a",
    "https://localhost.localdomain/a", "https://127.0.0.1/a", "https://[::1]/a", "https://2130706433/a", "https://a.internal/a",
    "https://example.com:444/a", "https://example.com/a%0d%0A", "https://example.com/a\\b", "javascript:alert(1)",
    "https://example.com/?access_token=secret", "https://example.com/?api_key=secret", "https://github.com./a", "https://github.com/a b"])
def test_unsafe_source_urls_are_rejected(url):
    assert safe_source_url(url) is None


@pytest.mark.parametrize("url", [URL, OTHER, "https://developer.mozilla.org/en-US/docs/Web?x=1#section"])
def test_public_https_source_urls_are_retained(url):
    assert safe_source_url(url) == url


def test_no_annotations_discards_model_claims_and_urls():
    value = envelope()
    value["choices"][0]["message"]["annotations"] = []
    provider, _ = client(value)
    result = provider.fetch(QUERY)
    assert result["status"] == "no_sources" and result["sources"] == result["suggested_fixes"] == []
    assert "migration documentation" not in result["summary"]
    assert "No fix was inferred" in result["summary"]


def test_unsafe_annotation_is_never_a_citation():
    value = envelope()
    value["choices"][0]["message"]["annotations"][0]["url_citation"]["url"] = "https://localhost/config"
    provider, _ = client(value)
    assert provider.fetch(QUERY)["status"] == "no_sources"


@pytest.mark.parametrize("change", [
    {"summary_source_urls": []}, {"summary_source_urls": [OTHER]},
    {"suggested_fixes": [{"description": "Unrelated proposal", "source_urls": [OTHER]}]},
    {"suggested_fixes": [{"description": "Uncited proposal", "source_urls": []}]},
    {"summary": "x" * 1501}, {"suggested_fixes": [{"description": "x" * 1501, "source_urls": [URL]}]},
])
def test_uncited_or_oversized_claims_fail_closed(change):
    provider, _ = client(changed_content(envelope(), **change))
    result = provider.fetch(QUERY)
    assert result["status"] == "unavailable" and result["error"]["code"] == "invalid_response"
    assert result["sources"] == result["suggested_fixes"] == []


def test_duplicate_annotations_are_deduplicated_and_excerpts_bounded():
    value = envelope()
    annotation = value["choices"][0]["message"]["annotations"][0]
    annotation["url_citation"]["content"] = "x" * 3000
    value["choices"][0]["message"]["annotations"].append(copy.deepcopy(annotation))
    provider, _ = client(value)
    report = provider.fetch(QUERY)
    assert report["status"] == "completed"
    assert len(report["sources"]) == 1 and len(report["sources"][0]["excerpt"]) == 2000


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 500, 302])
def test_provider_errors_are_safe_and_not_retried(status):
    provider, transport = client({"error": {"message": KEY}}, status=status)
    result = provider.fetch(QUERY)
    assert result["status"] == "unavailable" and KEY not in json.dumps(result)
    assert len(transport.calls) == 1


def test_timeout_is_an_unavailable_lookup():
    transport = Transport(error=requests.Timeout(KEY))
    result = OpenRouterFailureResearchClient(CONFIG, transport).fetch(QUERY)
    assert result["status"] == "unavailable" and KEY not in json.dumps(result)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("raw", [b'{"choices":[],"choices":[]}', b'{"choices":NaN}', b"invalid", b"x" * 100_000])
def test_invalid_and_oversized_provider_response(raw):
    provider, _ = client(raw=raw)
    result = provider.fetch(QUERY)
    assert result["status"] == "unavailable"


def test_truncated_completion_and_echoed_key_never_become_research():
    value = envelope()
    value["choices"][0]["finish_reason"] = "length"
    provider, _ = client(value)
    assert provider.fetch(QUERY)["status"] == "unavailable"
    value = changed_content(envelope(), summary=KEY)
    provider, _ = client(value)
    assert KEY not in json.dumps(provider.fetch(QUERY))


def test_missing_configuration_returns_explicit_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("PROOFRUN_MODEL", raising=False)
    result = OpenRouterFailureResearchClient().fetch(QUERY)
    assert result["error"]["code"] == "not_configured"
    assert result["status"] == "unavailable"


def test_request_idempotence_survives_restart_and_conflicts_before_provider_call(tmp_path):
    provider, transport = client()
    service = FailureResearchService(tmp_path, provider)
    result = service.submit("request-id", "  " + QUERY + "  ", CONTEXT)
    assert service.submit("request-id", QUERY, CONTEXT) == result
    for query, context in [(QUERY + " changed", CONTEXT), (QUERY, CONTEXT | {"evidence_sha256": "e" * 64})]:
        with pytest.raises(ResearchError) as failure:
            service.submit("request-id", query, context)
        assert failure.value.status == 409 and failure.value.code == "request_id_conflict"
    assert len(transport.calls) == 1
    service.close()
    provider2, transport2 = client()
    resumed = FailureResearchService(tmp_path, provider2)
    try:
        assert resumed.submit("request-id", QUERY, CONTEXT) == result
        assert not transport2.calls
    finally:
        resumed.close()


def test_restart_after_claim_returns_interrupted_without_calling_provider(tmp_path):
    class Crash:
        def fetch(self, query):
            raise KeyboardInterrupt()
    service = FailureResearchService(tmp_path, Crash())
    with pytest.raises(KeyboardInterrupt):
        service.submit("crashed", QUERY, CONTEXT)
    service.close()
    provider, transport = client()
    restarted = FailureResearchService(tmp_path, provider)
    try:
        report = restarted.submit("crashed", QUERY, CONTEXT)
        assert report["status"] == "unavailable" and report["error"]["code"] == "interrupted"
        assert not transport.calls
    finally:
        restarted.close()


def test_provider_exception_is_persisted_without_exposing_exception_text(tmp_path):
    class Broken:
        calls = 0
        def fetch(self, query):
            self.calls += 1
            raise RuntimeError(KEY)
    provider = Broken()
    service = FailureResearchService(tmp_path, provider)
    try:
        report = service.submit("broken", QUERY, CONTEXT)
        assert KEY not in json.dumps(report) and report["status"] == "unavailable"
        assert service.submit("broken", QUERY, CONTEXT) == report
        assert provider.calls == 1
    finally:
        service.close()


@pytest.mark.parametrize("mutate", [
    lambda report: report.update(summary="changed persisted claim"),
    lambda report: report["sources"][0].update(url="javascript:alert(1)"),
    lambda report: report["context"].update(scope="workspace-b"),
    lambda report: report.update(extra="unexpected"),
])
def test_saved_report_tampering_is_rejected(tmp_path, mutate):
    provider, _ = client()
    service = FailureResearchService(tmp_path, provider)
    try:
        report = service.submit("tamper", QUERY, CONTEXT)
        path = tmp_path / (report["research_id"] + ".json")
        saved = json.loads(path.read_text())
        mutate(saved["report"])
        path.write_text(json.dumps(saved))
        with pytest.raises(ResearchError) as failure:
            service.get(report["research_id"])
        assert failure.value.status == 409
    finally:
        service.close()


def test_directory_lock_and_unknown_report_ids(tmp_path):
    provider, _ = client()
    service = FailureResearchService(tmp_path, provider)
    try:
        with pytest.raises(ResearchError) as failure:
            FailureResearchService(tmp_path, provider)
        assert failure.value.code == "research_directory_busy"
        for research_id in ["../../secret", "failure-research-" + "0" * 64]:
            with pytest.raises(ResearchError) as failure:
                service.get(research_id)
            assert failure.value.status == 404
    finally:
        service.close()


@pytest.mark.parametrize("query", [None, "", "   ", "x" * 1201, "query\x00secret", "Bearer abcdefghijklmnop", "sk-or-v1-abcdefghijklmnop"])
def test_bad_queries_cannot_reach_provider(query):
    with pytest.raises(ResearchError):
        validate_query(query)


def test_context_validation_keeps_private_data_out_and_accepts_release_scope(tmp_path):
    provider, transport = client()
    service = FailureResearchService(tmp_path, provider)
    try:
        for context in [CONTEXT | {"source_content": "private code"}, CONTEXT | {"evidence_sha256": "bad"}]:
            with pytest.raises(ResearchError):
                service.submit("bad", QUERY, context)
        assert not transport.calls
        context = {"kind": "release", "run_id": "release-" + "a" * 40, "finding_id": "finding-1",
                   "scope": "w" * 160, "target_id": "customer-app", "baseline_revision": "a" * 40,
                   "candidate_revision": "b" * 40, "evidence_sha256": "c" * 64}
        assert service.submit("release-test", QUERY, context)["context"] == context
    finally:
        service.close()


def test_retrieved_but_unhelpful_sources_remain_no_sources():
    value = changed_content(envelope(), summary="", summary_source_urls=[], suggested_fixes=[])
    provider, _ = client(value)
    result = provider.fetch(QUERY)
    assert result["status"] == "no_sources"
    assert result["sources"] == result["suggested_fixes"] == []


def test_invalid_injected_client_report_cannot_persist_claims(tmp_path):
    class InvalidClient:
        def fetch(self, query):
            return {"status": "completed", "summary": "Unsupported success", "sources": [],
                    "suggested_fixes": [], "provenance": [], "error": None}
    service = FailureResearchService(tmp_path, InvalidClient())
    try:
        report = service.submit("invalid-client", QUERY, CONTEXT)
        assert report["status"] == "unavailable"
        assert service.get(report["research_id"]) == report
    finally:
        service.close()
