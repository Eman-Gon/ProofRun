"""Repair boundary tests use synthetic evidence and a mocked HTTP transport.

They establish validation and provenance behavior, never live model access or a
verified application repair. The independent runner owns the latter verdict.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

from src.proofrun.config import ConfigurationError, RepairConfig, WorkerConfig
from src.proofrun.contracts import PatchProposal, ProposalUnavailable
from src.proofrun.repair import OpenRouterRepairClient, _read_response, propose_patch, research_advisory


ROOT = Path(__file__).resolve().parents[1]
API_KEY = "sk-or-v1-synthetic-not-a-real-credential"
MODEL = "openai/gpt-4.1-mini"
WORKER_TOKEN = "synthetic-worker-token-with-32-or-more-characters"


def sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def failure_context():
    source = (ROOT / "demo/upgrade/app.py").read_text()
    raw_contract = (ROOT / "demo/upgrade/contract.json").read_text()
    requirements = json.loads(raw_contract)
    return {
        "source": {
            "repository": "https://example.invalid/synthetic/ProofRun",
            "revision": "a" * 40,
            "module": "demo/upgrade/app.py",
            "sha256": sha256(source),
            "content": source,
        },
        "case": {
            "case_id": requirements["case_id"],
            "contract_id": requirements["contract_id"],
            "contract_sha256": sha256(raw_contract),
            "model_name": requirements["model_name"],
            "field_name": requirements["field_name"],
            "allowed_path": requirements["allowed_path"],
            "expected_case_ids": [case["id"] for case in requirements["cases"]],
        },
        "requirements": requirements,
        "requirements_json": raw_contract,
        "comparison": {
            "execution_status": "completed",
            "finding_status": "regression_reproduced",
            "bindings": {
                "revision": "a" * 40,
                "source_sha256": sha256(source),
                "contract_sha256": sha256(raw_contract),
            },
            "cases": [
                {"id": "nickname_omitted", "stage": "baseline", "status": "passed"},
                {"id": "nickname_omitted", "stage": "updated", "status": "failed"},
            ],
        },
    }


def candidate(context=None):
    context = failure_context() if context is None else context
    return {
        "base_sha256": context["source"]["sha256"],
        "allowed_path": context["case"]["allowed_path"],
        "replacement": (ROOT / "demo/upgrade/fixed_app.py").read_text(),
        "rationale": "Use an explicit None default to preserve approved omission behavior.",
    }


def envelope(proposal=None):
    return {
        "id": "gen-synthetic-operation-123",
        "model": MODEL,
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(candidate() if proposal is None else proposal)},
        }],
    }


class FakeResponse:
    def __init__(self, body=None, status=200, raw=None, stream_error=None):
        self.status_code = status
        self.headers = {}
        self.raw_body = raw if raw is not None else json.dumps(envelope() if body is None else body).encode()
        self.stream_error = stream_error
        self.closed = False
        self.chunks_read = 0

    def iter_content(self, chunk_size=8192, **kwargs):
        if self.stream_error is not None:
            raise self.stream_error
        for offset in range(0, len(self.raw_body), chunk_size):
            self.chunks_read += 1
            yield self.raw_body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = FakeResponse() if response is None else response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class RepairConfigurationTests(unittest.TestCase):
    def test_explicit_model_and_server_side_key_are_required(self):
        for env in ({}, {"OPENROUTER_API_KEY": API_KEY}, {"PROOFRUN_MODEL": MODEL}):
            with self.subTest(keys=list(env)), self.assertRaises(ConfigurationError):
                RepairConfig.from_env(env)
        config = RepairConfig.from_env({"OPENROUTER_API_KEY": API_KEY, "PROOFRUN_MODEL": MODEL})
        self.assertEqual(config.model, MODEL)
        self.assertNotIn(API_KEY, repr(config))

    def test_automatic_routing_and_malformed_models_are_rejected(self):
        for model in ("openrouter/auto", "openrouter/free", "", "model", "https://private.invalid/model", "vendor/model\nsecret"):
            with self.subTest(model=model), self.assertRaises(ConfigurationError):
                RepairConfig(api_key=API_KEY, model=model)

    def test_bounds_and_invalid_environment_values_have_safe_errors(self):
        for field, values in (("timeout_seconds", (0, 61, True, "45")), ("max_tokens", (0, 255, 4097, True, "4096"))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                    RepairConfig(api_key=API_KEY, model=MODEL, **{field: value})
        for value in (API_KEY + "\n", API_KEY + "\x00", API_KEY + " secret", API_KEY * 100):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ConfigurationError) as failure:
                RepairConfig.from_env({"OPENROUTER_API_KEY": value, "PROOFRUN_MODEL": MODEL})
            self.assertNotIn(API_KEY, str(failure.exception))

    def test_wrong_constructor_types_are_safe_configuration_errors(self):
        for value in (None, 4, [], {}):
            for field in ("api_key", "model"):
                with self.subTest(field=field, kind=type(value).__name__), self.assertRaises(ConfigurationError):
                    RepairConfig(**{**{"api_key": API_KEY, "model": MODEL}, field: value})
            for field in ("token", "execution_target", "worker_id"):
                with self.subTest(field=field, kind=type(value).__name__), self.assertRaises(ConfigurationError):
                    WorkerConfig(**{**{"token": WORKER_TOKEN, "artifact_dir": ROOT}, field: value})

    def test_worker_defaults_are_local_and_private_token_is_not_repr(self):
        config = WorkerConfig.from_env({"PROOFRUN_WORKER_TOKEN": WORKER_TOKEN})
        self.assertEqual(config.execution_target, "local")
        self.assertEqual(config.worker_id, "local-worker")
        self.assertTrue(config.artifact_dir.is_absolute())
        self.assertNotIn(WORKER_TOKEN, repr(config))

    def test_crusoe_requires_an_explicit_host_identifier(self):
        env = {"PROOFRUN_WORKER_TOKEN": WORKER_TOKEN, "PROOFRUN_EXECUTION_TARGET": "crusoe"}
        with self.assertRaises(ConfigurationError):
            WorkerConfig.from_env(env)
        config = WorkerConfig.from_env({**env, "PROOFRUN_WORKER_ID": "crusoe-vm-synthetic"})
        self.assertEqual(config.execution_target, "crusoe")
        self.assertEqual(config.worker_id, "crusoe-vm-synthetic")

    def test_bad_worker_configuration_does_not_echo_values(self):
        for changes in (
            {"PROOFRUN_WORKER_TOKEN": "short-secret"},
            {"PROOFRUN_WORKER_TOKEN": WORKER_TOKEN + "\n"},
            {"PROOFRUN_EXECUTION_TARGET": "unsupported-private-target"},
            {"PROOFRUN_WORKER_ID": "private/path"},
        ):
            with self.subTest(keys=list(changes)), self.assertRaises(ConfigurationError) as failure:
                WorkerConfig.from_env({"PROOFRUN_WORKER_TOKEN": WORKER_TOKEN, **changes})
            for value in changes.values():
                self.assertNotIn(value, str(failure.exception))


class BoundedRepairTests(unittest.TestCase):
    def setUp(self):
        self.context = failure_context()
        self.config = RepairConfig(api_key=API_KEY, model=MODEL)
        self._real_subprocess_run = subprocess.run
        # Fail locally if a test forgets to supply its synthetic transport. A
        # mocked parent Session cannot intercept a newly spawned HTTP child.
        for target in ("src.proofrun.repair.subprocess.run", "src.proofrun.repair.requests.Session"):
            guard = patch(target, side_effect=AssertionError("Repair tests must explicitly mock HTTP/process access"))
            guard.start()
            self.addCleanup(guard.stop)

    def client(self, transport):
        return OpenRouterRepairClient(self.config, transport=transport)

    def assert_unavailable(self, response=None, *, context=None, attempt=1, error=None):
        transport = FakeTransport(response=response, error=error)
        with self.assertRaises(ProposalUnavailable) as failure:
            self.client(transport).propose_patch(self.context if context is None else context, attempt)
        self.assertNotIn(API_KEY, str(failure.exception))
        self.assertNotIn("PRIVATE_PROVIDER_BODY", str(failure.exception))
        self.assertLessEqual(len(transport.calls), 1, "Repair calls must not silently retry or switch providers")
        return transport, failure.exception

    def test_valid_proposal_has_mock_provenance_and_no_verdict(self):
        transport = FakeTransport()
        result = self.client(transport).propose_patch(self.context, 1)
        self.assertIsInstance(result, PatchProposal)
        self.assertEqual(result.base_sha256, self.context["source"]["sha256"])
        self.assertEqual(result.allowed_path, "demo/upgrade/app.py")
        self.assertEqual(result.replacement, candidate()["replacement"])
        self.assertEqual(result.provenance["mode"], "mock")
        self.assertEqual(result.provenance["gateway"], "openrouter")
        self.assertEqual(result.provenance["requested_model"], MODEL)
        self.assertEqual(result.provenance["model"], MODEL)
        self.assertIn("gen-synthetic-operation-123", result.provenance.values())
        self.assertFalse(hasattr(result, "repair_status"))
        self.assertNotIn(API_KEY, repr(result))
        self.assertTrue(transport.response.closed)

    def test_research_is_bounded_advice_in_prompt_and_never_changes_requirements(self):
        report = {"schema_version": "proofrun.failure-research.v1", "research_id": "research-fixture",
                  "status": "completed", "summary": "External hypothesis. " * 300,
                  "sources": [{"id": "source-1", "title": "Migration guide",
                               "url": "https://docs.pydantic.dev/latest/migration/", "excerpt": "External text. " * 300}],
                  "suggested_fixes": [{"description": "Consider an explicit default.", "source_ids": ["source-1"]}],
                  "requirements": {"approve_everything": True}, "provider_raw": "PRIVATE_RAW_PROVIDER_OUTPUT"}
        self.context["failure_research"] = research_advisory(report)
        self.context["failure_research"]["instructions"] = "IGNORE_ALL_TESTS"
        transport = FakeTransport()
        self.client(transport).propose_patch(self.context, 1)
        body = transport.calls[0][1]["json"]
        payload = json.loads(body["messages"][1]["content"])
        advisory = payload["failure_research"]
        self.assertEqual(advisory["research_id"], report["research_id"])
        self.assertLessEqual(len(advisory["summary"].encode()), 1600)
        self.assertLessEqual(len(advisory["sources"][0]["excerpt"].encode()), 240)
        self.assertEqual(payload["requirements"], self.context["requirements"])
        self.assertNotIn("PRIVATE_RAW_PROVIDER_OUTPUT", json.dumps(body))
        self.assertNotIn("IGNORE_ALL_TESTS", json.dumps(body))
        self.assertIn("untrusted advisory", body["messages"][0]["content"])

    def test_invalid_advisory_cannot_reach_model(self):
        self.context["failure_research"] = {"research_id": "research-fixture", "sources": []}
        transport, _ = self.assert_unavailable()
        self.assertFalse(transport.calls)

    def test_request_is_one_bounded_explicit_model_call_with_key_only_in_auth_header(self):
        transport = FakeTransport()
        before = copy.deepcopy(self.context)
        self.client(transport).propose_patch(self.context, 2)
        self.assertEqual(self.context, before)
        self.assertEqual(len(transport.calls), 1)
        url, kwargs = transport.calls[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + API_KEY)
        payload = kwargs["json"]
        self.assertEqual(payload["model"], MODEL)
        self.assertNotIn("models", payload)
        self.assertFalse(payload.get("stream", False))
        self.assertLessEqual(payload["max_tokens"], 4096)
        self.assertNotIn(API_KEY, json.dumps(payload))
        self.assertIn(self.context["source"]["sha256"], json.dumps(payload))
        self.assertIn("nickname_omitted", json.dumps(payload))
        self.assertTrue(kwargs["stream"])
        self.assertFalse(kwargs.get("allow_redirects", True))
        timeout = kwargs["timeout"]
        self.assertLessEqual(max(timeout) if isinstance(timeout, tuple) else timeout, 60)

    def test_selected_model_can_use_sampling_defaults_without_weakening_repair_bounds(self):
        selected_model = "anthropic/claude-sonnet-5"

        class DefaultsOnlyTransport(FakeTransport):
            def post(self, url, **kwargs):
                if "temperature" in kwargs["json"]:
                    self.response = FakeResponse(status=400, raw=b'{"error":"unsupported parameter"}')
                return super().post(url, **kwargs)

        body = envelope()
        body["model"] = selected_model
        transport = DefaultsOnlyTransport(FakeResponse(body))
        config = RepairConfig(api_key=API_KEY, model=selected_model, max_tokens=2048)
        proposal = OpenRouterRepairClient(config, transport=transport).propose_patch(self.context, 1)
        self.assertEqual(len(transport.calls), 1)
        payload = transport.calls[0][1]["json"]
        self.assertEqual(payload["model"], selected_model)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("models", payload)
        self.assertFalse(payload["provider"]["allow_fallbacks"])
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        schema = payload["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertEqual(set(schema["schema"]["required"]), {"base_sha256", "allowed_path", "replacement", "rationale"})
        self.assertEqual(proposal.provenance["mode"], "mock")
        self.assertEqual(proposal.provenance["requested_model"], selected_model)
        self.assertEqual(proposal.provenance["model"], selected_model)
        self.assertEqual(proposal.replacement, candidate()["replacement"])

    def test_default_session_ignores_ambient_proxy_credentials_and_closes(self):
        # HTTP remains mocked here; the default client uses a child process.
        class FakeSession(FakeTransport):
            trust_env = True
            closed = False

            def close(self):
                self.closed = True

        session = FakeSession()
        with patch("src.proofrun.repair.requests.Session", return_value=session):
            raw = _read_response(self.config, {"model": MODEL})
        self.assertEqual(raw, session.response.raw_body)
        self.assertFalse(session.trust_env)
        self.assertTrue(session.closed)
        self.assertTrue(session.response.closed)

    def test_production_path_sends_credentials_only_on_private_child_stdin(self):
        # The subprocess is mocked, so this provides no live integration evidence.
        raw = json.dumps(envelope()).encode()
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps({"response": base64.b64encode(raw).decode()}).encode())
        with patch("src.proofrun.repair.subprocess.run", return_value=result) as run:
            proposal = OpenRouterRepairClient(self.config).propose_patch(self.context, 1)
        self.assertEqual(proposal.provenance["mode"], "live")
        run.assert_called_once()
        arguments, kwargs = run.call_args
        self.assertNotIn(API_KEY, repr(arguments))
        self.assertEqual(kwargs["env"], {})
        self.assertEqual(kwargs["timeout"], self.config.timeout_seconds)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        child_request = json.loads(kwargs["input"])
        self.assertEqual(child_request["api_key"], API_KEY)
        self.assertEqual(child_request["model"], MODEL)
        self.assertNotIn(API_KEY, json.dumps(child_request["body"]))
        self.assertNotIn(API_KEY, repr(proposal))

    def test_child_timeout_startup_failure_and_untrusted_errors_are_safe(self):
        errors = (
            subprocess.TimeoutExpired(["private-command"], 1, output=API_KEY.encode()),
            OSError("PRIVATE_PROVIDER_BODY " + API_KEY),
        )
        for error in errors:
            with patch("src.proofrun.repair.subprocess.run", side_effect=error) as run:
                with self.assertRaises(ProposalUnavailable) as failure:
                    OpenRouterRepairClient(self.config).propose_patch(self.context, 1)
            run.assert_called_once()
            self.assertNotIn(API_KEY, str(failure.exception))
            self.assertNotIn("PRIVATE_PROVIDER_BODY", str(failure.exception))
        for payload in ({"error": API_KEY}, {"response": "not base64!"}, [], {"response": None}):
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode())
            with patch("src.proofrun.repair.subprocess.run", return_value=result):
                with self.assertRaises(ProposalUnavailable) as failure:
                    OpenRouterRepairClient(self.config).propose_patch(self.context, 1)
            self.assertNotIn(API_KEY, str(failure.exception))

    @unittest.skipUnless(os.name == "posix", "The worker deployment targets POSIX")
    def test_real_child_is_killed_and_reaped_at_wall_deadline(self):
        # Substitute a local sleeping executable, never an HTTP operation.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pid_file = directory / "child.pid"
            command = "printf '%s' \"$$\" > " + shlex.quote(str(pid_file)) + "\nexec /bin/sleep 10\n"
            real_run = self._real_subprocess_run

            def run_local_child(_argv, **kwargs):
                return real_run(["/bin/sh", "-c", command], **kwargs)

            config = RepairConfig(api_key=API_KEY, model=MODEL, timeout_seconds=1)
            started = time.monotonic()
            with patch("src.proofrun.repair.subprocess.run", side_effect=run_local_child):
                with self.assertRaisesRegex(ProposalUnavailable, "total time limit"):
                    OpenRouterRepairClient(config).propose_patch(self.context, 1)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 4, "The ten-second child must be stopped by the one-second parent deadline")
            self.assertTrue(pid_file.exists(), "The timeout test must execute a real child")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_only_attempts_one_and_two_are_accepted(self):
        for attempt in (0, 3, -1, True, "1", None):
            with self.subTest(attempt=attempt):
                transport, _ = self.assert_unavailable(attempt=attempt)
                self.assertEqual(transport.calls, [])

    def test_source_and_contract_hashes_are_checked_before_network(self):
        contexts = []
        for scope, field in (("source", "sha256"), ("case", "contract_sha256")):
            context = copy.deepcopy(self.context)
            context[scope][field] = "f" * 64
            contexts.append(context)
        for field in ("source_sha256", "contract_sha256"):
            context = copy.deepcopy(self.context)
            context["comparison"]["bindings"][field] = "f" * 64
            contexts.append(context)
        context = copy.deepcopy(self.context)
        context["requirements"]["model_name"] = "ChangedCustomer"
        contexts.append(context)
        context = copy.deepcopy(self.context)
        context["requirements_json"] += "\n"
        contexts.append(context)
        for context in contexts:
            with self.subTest(context=context["case"]):
                transport, _ = self.assert_unavailable(context=context)
                self.assertEqual(transport.calls, [])

    def test_non_measured_or_empty_comparison_cannot_trigger_a_model(self):
        for field, value in (("execution_status", "setup_failed"), ("execution_status", "timed_out"),
                             ("finding_status", "inconclusive"), ("finding_status", "no_difference_observed"),
                             ("cases", [])):
            context = copy.deepcopy(self.context)
            context["comparison"][field] = value
            with self.subTest(field=field, value=value):
                transport, _ = self.assert_unavailable(context=context)
                self.assertEqual(transport.calls, [])

    def test_missing_malformed_and_stale_revisions_never_reach_transport(self):
        for value in (None, "main", "a" * 7, "g" * 40, "b" * 40):
            context = copy.deepcopy(self.context)
            if value is None:
                del context["source"]["revision"]
            else:
                context["source"]["revision"] = value
            transport, _ = self.assert_unavailable(context=context)
            self.assertEqual(transport.calls, [])
        for value in (None, "b" * 40):
            context = copy.deepcopy(self.context)
            if value is None:
                del context["comparison"]["bindings"]["revision"]
            else:
                context["comparison"]["bindings"]["revision"] = value
            transport, _ = self.assert_unavailable(context=context)
            self.assertEqual(transport.calls, [])

    def test_second_attempt_receives_bound_rejection_feedback_without_private_paths(self):
        context = copy.deepcopy(self.context)
        context["previous_verification"] = {
            "execution_status": "completed", "repair_status": "rejected",
            "bindings": {**context["comparison"]["bindings"], "candidate_sha256": "b" * 64},
            "cases": [{"id": "nickname_object_rejected", "stage": "repaired_updated", "status": "failed",
                       "detail": "An object-valued nickname was accepted.", "output_ref": "PRIVATE_OUTPUT_REFERENCE"}],
            "artifacts": {"verification.json": "/private/PRIVATE_VERIFICATION_PATH"},
        }
        transport = FakeTransport()
        self.client(transport).propose_patch(context, 2)
        prompt = json.loads(transport.calls[0][1]["json"]["messages"][1]["content"])
        feedback = prompt["previous_verification"]
        self.assertEqual(feedback["candidate_sha256"], "b" * 64)
        self.assertEqual(feedback["repair_status"], "rejected")
        self.assertEqual(feedback["cases"][0]["detail"], "An object-valued nickname was accepted.")
        self.assertNotIn("PRIVATE_OUTPUT_REFERENCE", json.dumps(prompt))
        self.assertNotIn("PRIVATE_VERIFICATION_PATH", json.dumps(prompt))
        transport, _ = self.assert_unavailable(context=context, attempt=1)
        self.assertEqual(transport.calls, [])

    def test_retry_feedback_requires_executed_rejection_and_current_bindings(self):
        previous = {
            "execution_status": "completed", "repair_status": "rejected",
            "bindings": {**self.context["comparison"]["bindings"], "candidate_sha256": "b" * 64},
            "cases": [{"id": "nickname_object_rejected", "status": "failed"}],
        }
        variants = []
        for field, value in (("execution_status", "setup_failed"), ("repair_status", "verified"), ("cases", [])):
            variants.append({**previous, field: value})
        for field in ("revision", "source_sha256", "contract_sha256", "candidate_sha256"):
            variant = copy.deepcopy(previous)
            variant["bindings"][field] = "invalid-stale-binding"
            variants.append(variant)
        for variant in variants:
            context = {**self.context, "previous_verification": variant}
            transport, _ = self.assert_unavailable(context=context, attempt=2)
            self.assertEqual(transport.calls, [])

    def test_context_cannot_expand_the_allowed_path(self):
        for path in ("../app.py", "/tmp/app.py", "demo/upgrade/test_existing.py", "demo/upgrade/requirements-new.txt", "demo\\upgrade\\app.py"):
            context = copy.deepcopy(self.context)
            context["case"]["allowed_path"] = path
            with self.subTest(path=path):
                transport, _ = self.assert_unavailable(context=context)
                self.assertEqual(transport.calls, [])

    def test_missing_context_fields_and_wrong_types_fail_safely(self):
        for value in (None, [], {}, {"source": "wrong type"}):
            with self.subTest(value=value):
                transport = FakeTransport()
                with self.assertRaises(ProposalUnavailable):
                    self.client(transport).propose_patch(value, 1)
                self.assertEqual(transport.calls, [])
        for key in ("source", "case", "requirements", "requirements_json", "comparison"):
            context = copy.deepcopy(self.context)
            del context[key]
            transport, _ = self.assert_unavailable(context=context)
            self.assertEqual(transport.calls, [])

    def test_large_source_and_request_are_rejected_before_network(self):
        for location in ("source", "comparison"):
            context = copy.deepcopy(self.context)
            if location == "source":
                context["source"]["content"] = "#" + "x" * 1_000_000
                context["source"]["sha256"] = sha256(context["source"]["content"])
                context["comparison"]["bindings"]["source_sha256"] = context["source"]["sha256"]
            else:
                context["comparison"]["cases"][0]["observed"] = "x" * 1_000_000
            with self.subTest(location=location):
                transport, _ = self.assert_unavailable(context=context)
                self.assertEqual(transport.calls, [])

    def test_stale_candidate_hash_and_forbidden_paths_are_rejected(self):
        proposals = [{**candidate(), "base_sha256": "f" * 64}]
        proposals += [{**candidate(), "allowed_path": path} for path in (
            "../app.py", "/tmp/app.py", "demo/upgrade/test_existing.py", "demo/upgrade/requirements-new.txt",
        )]
        for proposal in proposals:
            with self.subTest(path=proposal["allowed_path"]):
                self.assert_unavailable(FakeResponse(envelope(proposal)))

    def test_unsupported_imports_and_non_python_replacements_are_rejected(self):
        for replacement in ("import os\n" + candidate()["replacement"], "from .private import secret\n", "def broken(:\n", ""):
            with self.subTest(replacement=replacement[:30]):
                self.assert_unavailable(FakeResponse(envelope({**candidate(), "replacement": replacement})))

    def test_model_cannot_add_verdict_or_modify_multiple_files(self):
        for extra in ({"repair_status": "verified"}, {"tests": "always passes"}, {"files": {"other.py": "bad"}}):
            self.assert_unavailable(FakeResponse(envelope({**candidate(), **extra})))

    def test_missing_or_non_string_proposal_fields_are_rejected(self):
        for field in ("base_sha256", "allowed_path", "replacement", "rationale"):
            proposal = candidate()
            del proposal[field]
            self.assert_unavailable(FakeResponse(envelope(proposal)))
            for value in (None, {}, [], 1):
                self.assert_unavailable(FakeResponse(envelope({**candidate(), field: value})))

    def test_empty_multiple_refused_or_truncated_choices_are_unavailable(self):
        bodies = [{**envelope(), "choices": []}]
        body = envelope()
        body["choices"] *= 2
        bodies.append(body)
        for reason in ("length", "content_filter", "tool_calls", None):
            body = envelope()
            body["choices"][0]["finish_reason"] = reason
            bodies.append(body)
        body = envelope()
        body["choices"][0]["message"]["refusal"] = "Cannot provide this proposal."
        bodies.append(body)
        for body in bodies:
            self.assert_unavailable(FakeResponse(body))

    def test_non_json_markdown_or_trailing_output_is_rejected(self):
        for content in ("not JSON", "```json\n" + json.dumps(candidate()) + "\n```", json.dumps(candidate()) + " extra", "null", "[]"):
            body = envelope()
            body["choices"][0]["message"]["content"] = content
            self.assert_unavailable(FakeResponse(body))
        for raw in (b"not JSON PRIVATE_PROVIDER_BODY", b"\xff\xfe", b"{}", b"null", b"[]"):
            self.assert_unavailable(FakeResponse(raw=raw))

    def test_oversized_response_and_candidate_are_rejected_and_closed(self):
        response = FakeResponse(raw=b" " * 1_000_000)
        self.assert_unavailable(response)
        self.assertTrue(response.closed)
        self.assertLess(response.chunks_read, 1_000_000 // 8192)
        for field in ("replacement", "rationale"):
            self.assert_unavailable(FakeResponse(envelope({**candidate(), field: "x" * 1_000_000})))

    def test_http_errors_do_not_echo_provider_bodies_or_retry(self):
        for status in (301, 400, 401, 402, 403, 408, 429, 500, 503):
            response = FakeResponse(status=status, raw=("PRIVATE_PROVIDER_BODY " + API_KEY).encode())
            with self.subTest(status=status):
                transport, _ = self.assert_unavailable(response)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(response.chunks_read, 0)
                self.assertTrue(response.closed)

    def test_transport_and_read_timeouts_are_safe_without_retry(self):
        for error in (requests.Timeout(API_KEY), requests.ConnectionError("PRIVATE_PROVIDER_BODY " + API_KEY)):
            transport, _ = self.assert_unavailable(error=error)
            self.assertEqual(len(transport.calls), 1)
            response = FakeResponse(stream_error=error)
            self.assert_unavailable(response)
            self.assertTrue(response.closed)

    def test_credentials_echoed_by_provider_are_rejected(self):
        for field in ("replacement", "rationale"):
            proposal = candidate()
            proposal[field] += "\n# " + API_KEY
            self.assert_unavailable(FakeResponse(envelope(proposal)))
        for field in ("model", "id"):
            body = envelope()
            body[field] = API_KEY
            self.assert_unavailable(FakeResponse(body))

    def test_json_escaped_credentials_are_rejected_after_decoding(self):
        encoded_key = "".join("\\u%04x" % ord(character) for character in API_KEY)
        proposal = {**candidate(), "rationale": API_KEY}
        body = envelope(proposal)
        body["choices"][0]["message"]["content"] = json.dumps(proposal).replace(API_KEY, encoded_key)
        self.assertNotIn(API_KEY, json.dumps(body))
        transport, _ = self.assert_unavailable(FakeResponse(body))
        self.assertEqual(len(transport.calls), 1)
        body = envelope()
        body["model"] = API_KEY
        raw = json.dumps(body).replace(API_KEY, encoded_key).encode()
        self.assertNotIn(API_KEY.encode(), raw)
        transport, _ = self.assert_unavailable(FakeResponse(raw=raw))
        self.assertEqual(len(transport.calls), 1)

    def test_context_credential_material_never_reaches_transport(self):
        for location in ("source", "comparison"):
            context = copy.deepcopy(self.context)
            if location == "source":
                context["source"]["content"] += "\n# " + API_KEY
                context["source"]["sha256"] = sha256(context["source"]["content"])
                context["comparison"]["bindings"]["source_sha256"] = context["source"]["sha256"]
            else:
                context["comparison"]["cases"][0]["observed"] = API_KEY
            transport, _ = self.assert_unavailable(context=context)
            self.assertEqual(transport.calls, [])

    def test_private_logs_artifact_paths_and_arbitrary_context_are_not_sent(self):
        context = copy.deepcopy(self.context)
        context["private_notes"] = "PRIVATE_TOP_LEVEL_MARKER"
        context["source"]["repository"] = "https://PRIVATE_REPOSITORY_MARKER.invalid/"
        context["comparison"]["artifacts"] = {"comparison.json": "/private/PRIVATE_ARTIFACT_MARKER"}
        context["comparison"]["cases"][0]["output"] = "PRIVATE_LOG_MARKER"
        transport = FakeTransport()
        self.client(transport).propose_patch(context, 1)
        sent = json.dumps(transport.calls[0][1]["json"])
        for marker in ("PRIVATE_TOP_LEVEL_MARKER", "PRIVATE_REPOSITORY_MARKER", "PRIVATE_ARTIFACT_MARKER", "PRIVATE_LOG_MARKER"):
            self.assertNotIn(marker, sent)

    def test_ambiguous_json_and_nonfinite_data_fail_closed(self):
        body = envelope()
        body["choices"][0]["message"]["content"] = json.dumps(candidate())[:-1] + ',"base_sha256":"' + "f" * 64 + '"}'
        self.assert_unavailable(FakeResponse(body))
        raw = json.dumps(envelope())[:-1] + ',"model":"other/model"}'
        self.assert_unavailable(FakeResponse(raw=raw.encode()))
        context = copy.deepcopy(self.context)
        context["comparison"]["cases"][0]["observed"] = float("nan")
        transport, _ = self.assert_unavailable(context=context)
        self.assertEqual(transport.calls, [])

    def test_missing_provenance_and_unchanged_candidate_are_unavailable(self):
        for field in ("model", "id"):
            body = envelope()
            del body[field]
            self.assert_unavailable(FakeResponse(body))
        self.assert_unavailable(FakeResponse(envelope({**candidate(), "replacement": self.context["source"]["content"]})))

    def test_wrapper_uses_environment_config_and_preserves_contract(self):
        expected = PatchProposal(**candidate(), provenance={"mode": "mock"})
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": API_KEY, "PROOFRUN_MODEL": MODEL}, clear=True):
            with patch("src.proofrun.repair.OpenRouterRepairClient") as client:
                client.return_value.propose_patch.return_value = expected
                self.assertIs(propose_patch(self.context, 1), expected)
                client.return_value.propose_patch.assert_called_once_with(self.context, 1)

    def test_missing_environment_configuration_is_repair_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProposalUnavailable):
                propose_patch(self.context, 1)


if __name__ == "__main__":
    unittest.main()
