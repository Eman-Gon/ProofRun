"""Synthetic adaptive-loop tests; no provider access or application verdict proof."""
import base64
import copy
import json
import os
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.proofrun import release_agent as agent
from src.proofrun.config import RepairConfig


KEY = "sk-or-v1-synthetic-private-credential-12345"
MODEL = "example/investigation-model"


def context(**overrides):
    return {
        "target": {"name": "Synthetic product"}, "baseline_revision": "a" * 40,
        "candidate_revision": "b" * 40, "inventory": {"files": 3},
        "requirements": [{"id": "preserve-input", "description": "Preserve approved endpoint responses", "path": "/records"}],
        "existing_test_results": {"baseline": "passed", "candidate": "passed"},
        "repair_enabled": False, "benefit": "Find release risks in the approved workflow", **overrides,
    }


def action(tool="finish", arguments=None, reason="Choose the next bounded investigation step."):
    return {"tool": tool, "arguments": {"summary": "Investigation finished; observed limitations retained."} if arguments is None else arguments,
            "reason": reason}


def reply(value=None, provenance=None):
    return {"action": action() if value is None else value,
            "provenance": {"model": "synthetic/test-model", "operation_id": "synthetic-operation"} if provenance is None else provenance}


def completion(value=None, **overrides):
    return {"id": "gen-synthetic-001", "model": MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(action() if value is None else value)}}],
            **overrides}


def process_response(value=None, raw=None):
    raw = json.dumps(completion() if value is None else value).encode() if raw is None else raw
    return SimpleNamespace(returncode=0, stdout=json.dumps({"response": base64.b64encode(raw).decode()}).encode())


class ReleaseAgentTests(unittest.TestCase):
    def run_agent(self, model, tool=None, data=None, seconds=10, events=None):
        return agent.investigate(context() if data is None else data, tool or Mock(return_value={}),
                                 time.monotonic() + seconds, (events.append if events is not None else None), model)

    def test_next_action_uses_previous_observation_and_arbitrary_file(self):
        for filename in ("services/copper/catalog.py", "lib/meridian/persistence.ts"):
            observed, events = [], []

            def runtime(name, arguments):
                observed.append((name, copy.deepcopy(arguments)))
                if name == "list_files":
                    return {"files": [filename]}
                if name == "read_file":
                    self.assertEqual(arguments["path"], filename)
                    return {"content": "The handler delegates normalization to storage", "next_search": "normalize_record"}
                if name == "search":
                    self.assertEqual(arguments["query"], "normalize_record")
                    return {"observation": "Normalization changed", "input_to_explore": {"label": "  cobalt  "}}
                if name == "run_probe":
                    self.assertEqual(arguments["steps"][0]["json"], {"label": "  cobalt  "})
                    return {"finding_id": "runtime-finding-01", "outcome": "difference_reproduced"}
                raise AssertionError("Unexpected runtime operation")

            def adaptive(messages, timeout):
                self.assertGreater(timeout, 0)
                if len(messages) == 2:
                    return reply(action("list_files", {"revision": "candidate"}))
                previous = json.loads(messages[-1]["content"])
                result = previous["result"]
                if previous["tool"] == "list_files":
                    return reply(action("read_file", {"revision": "candidate", "path": result["files"][0]}))
                if previous["tool"] == "read_file":
                    return reply(action("search", {"revision": "candidate", "query": result["next_search"]}))
                if previous["tool"] == "search":
                    return reply(action("run_probe", {"name": "Whitespace boundary", "requirement_id": "preserve-input",
                        "steps": [{"method": "POST", "path": "/records", "json": result["input_to_explore"]}],
                        "hypothesis": "The release may transform input differently."}))
                return reply(action("finish", {"summary": f"Runtime returned {result['finding_id']}; no introducing commit established."}))

            result = self.run_agent(adaptive, runtime, events=events)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["steps"], 5)
            self.assertEqual([name for name, _ in observed], ["list_files", "read_file", "search", "run_probe"])
            self.assertTrue(all(item["mode"] == "injected" for item in result["provenance"]))
            self.assertEqual(len([event for event in events if event["type"] == "tool_result"]), 4)
            self.assertNotIn("nickname", agent.SYSTEM)
            self.assertNotIn("pydantic", agent.SYSTEM.lower())

    def test_probe_refusal_is_observed_before_next_decision(self):
        calls = []

        def model(messages, timeout):
            calls.append(messages)
            if len(calls) == 1:
                return reply(action("run_probe", {"name": "Unsupported endpoint", "requirement_id": "preserve-input",
                    "steps": [{"method": "GET", "path": "/unknown"}], "hypothesis": "Explore a suspected route."}))
            result = json.loads(messages[-1]["content"])["result"]
            self.assertEqual(result["error"], "endpoint_not_approved")
            return reply(action("finish", {"summary": "Probe was refused; no conclusion from this request."}))

        result = self.run_agent(model, Mock(return_value={"error": "endpoint_not_approved"}))
        self.assertEqual(result["status"], "completed")
        self.assertIn("refused", result["summary"])
        self.assertNotIn("verdict", result)

    def test_invalid_actions_never_reach_runtime(self):
        probe = {"name": "Probe", "requirement_id": "preserve-input", "steps": [{"method": "GET", "path": "/records"}], "hypothesis": "Investigate."}
        bad = [action("shell", {"command": "cat private.env"}), action("finish", {"summary": "Safe", "verdict": "passed"}),
               action("run_probe", {**probe, "expected": {"status": 200}}),
               action("run_probe", {**probe, "steps": [{"method": "GET", "path": "https://external.invalid"}]}),
               action("read_file", {"revision": "candidate", "path": "../private.env"}),
               action("read_file", {"revision": "arbitrary", "path": "app.py"}),
               action("read_file", {"revision": "candidate", "path": "app.py", "start_line": True}),
               action("propose_repair", {"finding_id": "observed", "changes": [{"path": "app.py", "content": "replacement"}], "rationale": "Repair."}),
               {**action(), "verdict": "safe"}, action(arguments={"summary": "x" * 5000}),
               action("read_file", {"revision": "candidate", "path": "app.py", "start_line": 8, "end_line": 2})]
        for invalid in bad:
            with self.subTest(tool=invalid["tool"]):
                runtime = Mock()
                result = self.run_agent(lambda *_: reply(invalid), runtime)
                self.assertEqual(result["status"], "failed")
                runtime.assert_not_called()

    def test_repair_is_only_proposal_and_runtime_returns_outcome(self):
        proposal = action("propose_repair", {"finding_id": "runtime-finding", "changes": [{"path": "service/records.py", "content": "new_content = 1\n"}], "rationale": "Address measured difference."})
        responses = iter([reply(proposal), reply()])
        runtime = Mock(return_value={"repair_status": "rejected", "reason": "control_failed"})
        result = self.run_agent(lambda *_: next(responses), runtime, data=context(repair_enabled=True))
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("repair_status", result)
        self.assertEqual(runtime.call_args.args[0], "propose_repair")

    def test_tool_exception_is_failure_and_error_body_is_not_retained(self):
        sensitive = "raw server error contains private credential"
        events = []
        result = self.run_agent(lambda *_: reply(action("diff", {})), Mock(side_effect=RuntimeError(sensitive)), events=events)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(sensitive, json.dumps([result, events]))

    def test_expired_deadline_prevents_model_and_tool_calls(self):
        model, runtime = Mock(), Mock()
        result = self.run_agent(model, runtime, seconds=-1)
        self.assertEqual(result["status"], "timed_out")
        model.assert_not_called()
        runtime.assert_not_called()

    def test_late_decision_cannot_run_a_tool(self):
        runtime = Mock()

        def late(*_):
            time.sleep(.025)
            return reply(action("diff", {}))

        result = self.run_agent(late, runtime, seconds=.01)
        self.assertEqual(result["status"], "timed_out")
        runtime.assert_not_called()
        self.assertEqual(result["provenance"][0]["status"], "timed_out")

    def test_late_tool_result_cannot_be_followed_by_success(self):
        def slow(*_):
            time.sleep(.025)
            return {"outcome": "passed"}
        model = Mock(return_value=reply(action("diff", {})))
        result = self.run_agent(model, slow, seconds=.01)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(model.call_count, 1)

    def test_decision_limit_never_implies_success(self):
        model = Mock(return_value=reply(action("diff", {})))
        result = self.run_agent(model, data=context(tool_limit=2))
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["steps"], 2)
        self.assertEqual(model.call_count, 2)

    def test_context_is_bounded_without_truncating_requirements(self):
        model = Mock()
        result = self.run_agent(model, data=context(requirements=["x" * agent.MAX_CONTEXT_BYTES]))
        self.assertEqual(result["status"], "failed")
        self.assertIn("not truncated", result["summary"])
        model.assert_not_called()

    def test_invalid_context_values_fail_safely(self):
        for data in ([], context(tool_limit=True), context(tool_limit=0), context(repair_enabled="true"), context(extra=float("nan"))):
            with self.subTest(data_type=type(data).__name__):
                model = Mock()
                self.assertEqual(self.run_agent(model, data=data)["status"], "failed")
                model.assert_not_called()

    def test_large_result_explicitly_marks_incomplete_evidence(self):
        calls = []

        def model(messages, timeout):
            calls.append(messages)
            if len(calls) == 1:
                return reply(action("read_file", {"revision": "candidate", "path": "arbitrary/source.txt"}))
            result = json.loads(messages[-1]["content"])["result"]
            self.assertTrue(result["truncated"])
            self.assertGreater(result["original_bytes"], agent.MAX_RESULT_BYTES)
            self.assertLess(len(json.dumps(result).encode()), agent.MAX_RESULT_BYTES + 1024)
            return reply()

        result = self.run_agent(model, Mock(return_value={"content": "x" * 30_000}))
        self.assertEqual(result["status"], "completed")

    def test_old_history_is_explicitly_omitted_and_prompt_stays_bounded(self):
        calls = 0

        def model(messages, timeout):
            nonlocal calls
            calls += 1
            self.assertLessEqual(len(agent._json(messages).encode()), agent.MAX_PROMPT_BYTES)
            if calls == 12:
                self.assertTrue(any("pairs omitted" in message["content"] for message in messages))
                return reply()
            return reply(action("diff", {}))

        result = self.run_agent(model, Mock(return_value={"content": "x" * 12_000}))
        self.assertEqual(result["status"], "completed")

    def test_credentials_are_absent_from_prompt_events_and_result(self):
        events, calls = [], []
        worker_secret = "private-worker-credential-123456789"

        def model(messages, timeout):
            calls.append(messages)
            if len(calls) == 1:
                return reply(action("diff", {}))
            return reply()

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": KEY, "PROOFRUN_WORKER_TOKEN": worker_secret}, clear=True):
            result = self.run_agent(model, Mock(return_value={"content": KEY + worker_secret, "authorization": "opaque"}),
                                    data=context(api_key=KEY, note=worker_secret), events=events)
        serialized = json.dumps([calls, events, result])
        self.assertNotIn(KEY, serialized)
        self.assertNotIn(worker_secret, serialized)
        self.assertEqual(result["status"], "completed")

    def test_credential_in_provenance_or_action_is_not_exposed(self):
        for response in (reply(provenance={"operation_id": KEY}), reply(action(arguments={"summary": KEY}))):
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": KEY}, clear=True):
                events = []
                result = self.run_agent(lambda *_: response, events=events)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn(KEY, json.dumps([result, events]))

    def test_injected_callback_cannot_claim_live_provider_provenance(self):
        result = self.run_agent(lambda *_: reply(provenance={"mode": "live", "gateway": "openrouter", "model": MODEL, "operation_id": "synthetic"}))
        self.assertEqual(result["provenance"][0]["mode"], "injected")
        self.assertEqual(result["provenance"][0]["gateway"], "injected")

    def test_trace_failure_cannot_produce_success(self):
        with patch.object(agent, "MAX_TRACE_BYTES", 10):
            result = self.run_agent(lambda *_: reply())
        self.assertEqual(result["status"], "budget_exhausted")


class ReleaseProviderTests(unittest.TestCase):
    def live(self, process, events=None, seconds=5):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": KEY, "PROOFRUN_MODEL": MODEL}, clear=True):
            with patch.object(agent.subprocess, "run", process):
                return agent.investigate(context(), Mock(), time.monotonic() + seconds,
                                         None if events is None else events.append)

    def test_missing_model_has_no_prepared_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(agent.subprocess, "run") as process:
            result = agent.investigate(context(), Mock(), time.monotonic() + 5)
        self.assertEqual(result["status"], "model_unavailable")
        self.assertEqual(result["provenance"], [])
        process.assert_not_called()

    def test_live_request_uses_killable_process_and_explicit_model(self):
        process = Mock(return_value=process_response())
        result = self.live(process)
        self.assertEqual(result["status"], "completed")
        args, kwargs = process.call_args
        payload = json.loads(kwargs["input"])
        self.assertEqual(payload["body"]["model"], MODEL)
        self.assertFalse(payload["body"]["provider"]["allow_fallbacks"])
        self.assertEqual(kwargs["env"], {})
        self.assertLessEqual(kwargs["timeout"], 5)
        self.assertNotIn(KEY, json.dumps(args))
        self.assertNotIn(KEY, json.dumps(result))
        record = result["provenance"][0]
        self.assertEqual(record["model"], MODEL)
        self.assertEqual(record["operation_id"], "gen-synthetic-001")
        self.assertEqual(record["mode"], "live")
        self.assertEqual(len(record["request_sha256"]), 64)
        self.assertEqual(len(record["response_sha256"]), 64)

    def test_provider_timeout_has_safe_provenance_and_no_retry(self):
        process = Mock(side_effect=subprocess.TimeoutExpired(["request", KEY], 1, output=KEY.encode()))
        result = self.live(process)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(process.call_count, 1)
        self.assertEqual(result["provenance"][0]["requested_model"], MODEL)
        self.assertNotIn(KEY, json.dumps(result))

    def test_provider_unavailable_and_refusal_are_not_success(self):
        refused = completion()
        refused["choices"][0]["message"]["refusal"] = "Private provider refusal text"
        for process_result in (SimpleNamespace(returncode=1, stdout=b""),
                               SimpleNamespace(returncode=0, stdout=b'{"error":"private-provider-body"}'),
                               process_response(refused)):
            with self.subTest(returncode=process_result.returncode):
                result = self.live(Mock(return_value=process_result))
                self.assertEqual(result["status"], "model_unavailable")
                self.assertNotIn("private-provider-body", json.dumps(result))
                self.assertNotIn("Private provider refusal text", json.dumps(result))

    def test_truncated_completion_never_executes_partial_action(self):
        truncated = completion()
        truncated["choices"][0]["finish_reason"] = "length"
        result = self.live(Mock(return_value=process_response(truncated)))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["provenance"][0]["operation_id"], "gen-synthetic-001")

    def test_malformed_or_missing_provenance_fails(self):
        invalid = completion()
        invalid["choices"][0]["message"]["content"] = '{"tool":"finish","tool":"diff","arguments":{},"reason":"x"}'
        for payload in (invalid, completion(id=None), completion(model=None), completion(choices=[])):
            result = self.live(Mock(return_value=process_response(payload)))
            self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
