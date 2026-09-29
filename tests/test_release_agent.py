"""Synthetic adaptive-loop tests; no provider access or application verdict proof."""
import base64
import copy
import io
import json
import os
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.proofrun import release_agent as agent
from src.proofrun.config import RepairConfig
from src.proofrun.contracts import ProposalUnavailable


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
    wire = copy.deepcopy(action() if value is None else value)
    wire["arguments"] = json.dumps(wire["arguments"])
    return {"id": "gen-synthetic-001", "model": MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(wire)}}],
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

    def test_twelve_step_probe_is_accepted_thirteen_is_diagnosed_without_arguments(self):
        for count in (12, 13):
            probe = action("run_probe", {"name": "Probe " + "x" * 128, "requirement_id": "preserve-input",
                "steps": [{"method": "GET", "path": "/records?private=not-for-diagnostics"}] * count,
                "hypothesis": "Exercise a sequence chosen after source inspection."})
            runtime, events = Mock(return_value={}), []
            responses = iter([reply(probe), reply()])
            result = self.run_agent(lambda *_: next(responses), runtime, events=events)
            self.assertEqual(result["status"], "completed" if count == 12 else "failed")
            if count == 12:
                self.assertEqual(len(runtime.call_args.args[1]["steps"]), 12)
            else:
                runtime.assert_not_called()
                record = result["provenance"][0]
                self.assertEqual(record["validation_error_code"], "invalid_probe_steps")
                self.assertEqual(record["attempted_tool"], "run_probe")
                self.assertEqual(record["probe_step_count"], 13)
                self.assertEqual(record["operation_id"], "synthetic-operation")
                self.assertNotIn("not-for-diagnostics", json.dumps([result, events]))

    def test_source_read_range_matches_runtime_defaults_and_three_hundred_line_limit(self):
        for start, end, allowed in ((1, 300, True), (401, 700, True), (1, 301, False), (301, None, False)):
            args = {"revision": "candidate", "path": "service.py", "start_line": start}
            if end is not None:
                args["end_line"] = end
            runtime = Mock(return_value={})
            responses = iter([reply(action("read_file", args)), reply()])
            result = self.run_agent(lambda *_: next(responses), runtime)
            self.assertEqual(result["status"], "completed" if allowed else "failed")
            if not allowed:
                runtime.assert_not_called()
                self.assertEqual(result["provenance"][0]["validation_error_code"], "invalid_line_range")

    def test_experiment_and_repair_caps_are_advertised_and_enforced(self):
        cases = [
            ("run_probe", 8, {"name": "Probe", "requirement_id": "preserve-input",
                "steps": [{"method": "GET", "path": "/records"}], "hypothesis": "Explore input."}, "probe_limit"),
            ("propose_repair", 2, {"finding_id": "finding-1", "changes": [{"path": "service.py", "content": "pass\n"}],
                "rationale": "Address observed behavior."}, "repair_limit"),
        ]
        for tool, limit, args, code in cases:
            messages_seen, runtime = [], Mock(return_value={})

            def model(messages, timeout):
                messages_seen.append(messages)
                return reply(action(tool, args))

            result = self.run_agent(model, runtime, data=context(repair_enabled=True))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(runtime.call_count, limit)
            self.assertEqual(result["provenance"][-1]["validation_error_code"], code)
            self.assertEqual(json.loads(messages_seen[0][1]["content"])["limits"][tool], limit)
            self.assertEqual(json.loads(messages_seen[-1][-1]["content"])["remaining_budget"][tool], 0)

    def test_runtime_validation_refusal_has_fixed_diagnostic_and_cannot_finish(self):
        events, model = [], Mock(return_value=reply(action("diff", {})))
        result = self.run_agent(model, Mock(side_effect=ValueError("private refusal text " + KEY)), events=events)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(model.call_count, 1)
        refusal = next(event for event in events if event["type"] == "tool_result")["result"]
        self.assertEqual(refusal["error_code"], "tool_request_rejected")
        self.assertFalse(refusal["complete"])
        self.assertNotIn("private refusal text", json.dumps([result, events]))
        self.assertNotIn(KEY, json.dumps([result, events]))

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
        clock = [100.0]

        def late(*_):
            clock[0] += 2
            return reply(action("diff", {}))

        with patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.run_agent(late, runtime, seconds=1)
        self.assertEqual(result["status"], "timed_out")
        runtime.assert_not_called()
        self.assertEqual(result["provenance"][0]["status"], "timed_out")

    def test_late_tool_result_cannot_be_followed_by_success(self):
        clock = [100.0]

        def slow(*_):
            clock[0] += 2
            return {"outcome": "passed"}
        model = Mock(return_value=reply(action("diff", {})))
        with patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.run_agent(model, slow, seconds=1)
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

    def test_escaping_cannot_expand_truncated_observation_past_budget(self):
        events = []
        responses = iter([reply(action("diff", {})), reply()])
        result = self.run_agent(lambda *_: next(responses), Mock(return_value={"content": '\\"' * 30_000}), events=events)
        observed = next(event["result"] for event in events if event["type"] == "tool_result")
        self.assertTrue(observed["truncated"])
        self.assertLessEqual(len(agent._json(observed).encode()), agent.MAX_RESULT_BYTES)
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
        schema = payload["body"]["response_format"]
        self.assertEqual(schema["type"], "json_schema")
        self.assertTrue(schema["json_schema"]["strict"])
        self.assertFalse(schema["json_schema"]["schema"]["additionalProperties"])
        self.assertEqual(schema["json_schema"]["schema"]["properties"]["arguments"], {"type": "string"})
        self.assertNotIn("temperature", payload["body"])
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

    def test_allowlisted_http_failure_retains_safe_reason_status_and_code(self):
        for message, code, status in (
            ("OpenRouter authentication failed; check OPENROUTER_API_KEY.", "authentication_failed", 401),
            ("The selected OpenRouter model or endpoint is unavailable.", "model_or_endpoint_unavailable", 404),
            ("OpenRouter rate limit reached; no automatic retry was made.", "rate_limited", 429),
        ):
            with self.subTest(status=status):
                process = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"error": message}).encode()))
                events = []
                result = self.live(process, events=events)
                self.assertEqual(result["status"], "model_unavailable")
                self.assertEqual(result["summary"], message)
                self.assertEqual(result["provenance"][0]["http_status"], status)
                self.assertEqual(result["provenance"][0]["error_code"], code)
                event = next(item for item in events if item["type"] == "model_call")
                self.assertEqual(event["http_status"], status)
                self.assertNotIn(KEY, json.dumps([result, events]))

    def test_http_child_exports_only_fixed_local_failure_messages(self):
        payload = {"api_key": KEY, "model": MODEL, "timeout_seconds": 5, "max_tokens": 256, "body": {}}
        for message in ("OpenRouter authentication failed; check OPENROUTER_API_KEY.", "untrusted provider error " + KEY):
            output = io.StringIO()
            with patch.object(agent.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), \
                    patch.object(agent.sys, "stdout", output), \
                    patch.object(agent, "_read_response", side_effect=ProposalUnavailable(message)):
                agent._http_child()
            result = json.loads(output.getvalue())
            self.assertIn(result["error"], agent._PROVIDER_FAILURES)
            self.assertNotIn(KEY, output.getvalue())
            self.assertNotIn("untrusted provider error", output.getvalue())

    def test_truncated_completion_never_executes_partial_action(self):
        truncated = completion()
        truncated["choices"][0]["finish_reason"] = "length"
        result = self.live(Mock(return_value=process_response(truncated)))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["provenance"][0]["operation_id"], "gen-synthetic-001")
        self.assertEqual(result["provenance"][0]["finish_reason"], "length")
        self.assertIn("output limit", result["summary"])

    def test_strict_provider_envelope_preserves_arbitrary_nested_probe_json(self):
        probe = action("run_probe", {"name": "Explore", "requirement_id": "preserve-input",
            "steps": [{"method": "POST", "path": "/records", "json": {"arbitrary": [None, True, 1, {"unicode": "\u2603"}]}}],
            "hypothesis": "Investigate product behavior."})
        process = Mock(return_value=process_response(completion(probe)))
        config = RepairConfig(KEY, MODEL)
        messages = [{"role": "system", "content": agent.SYSTEM}, {"role": "user", "content": "Approved context"}]
        original = copy.deepcopy(messages)
        with patch.object(agent.subprocess, "run", process):
            response = agent._live_model(config, messages, 5)
        self.assertEqual(response["action"], probe)
        self.assertEqual(messages, original)

    def test_parse_failures_retain_only_fixed_stage_and_location_diagnostics(self):
        wire = {"tool": "finish", "arguments": json.dumps({"summary": "Done"}), "reason": "Finish."}
        cases = [
            ("```json\n" + json.dumps(wire) + "\n```", "decision_json_invalid"),
            (json.dumps({**wire, "arguments": '{"summary": private-unquoted-data}'}), "decision_arguments_json_invalid"),
            (json.dumps({**wire, "arguments": '{"summary":"a","summary":"b"}'}), "decision_arguments_json_invalid"),
            (json.dumps({**wire, "arguments": {"summary": "Done"}}), "decision_envelope_invalid"),
            (json.dumps({**wire, "unexpected": "private-extra-data"}), "decision_envelope_invalid"),
        ]
        for content, code in cases:
            envelope = completion()
            envelope["choices"][0]["message"]["content"] = content
            events = []
            result = self.live(Mock(return_value=process_response(envelope)), events=events)
            self.assertEqual(result["status"], "failed")
            record = result["provenance"][0]
            self.assertEqual(record["error_code"], code)
            self.assertEqual(record["operation_id"], "gen-synthetic-001")
            self.assertEqual(record["decision_bytes"], len(content.encode()))
            self.assertNotIn("private-unquoted-data", json.dumps([events, result]))
            self.assertNotIn("private-extra-data", json.dumps([events, result]))

    def test_malformed_or_missing_provenance_fails(self):
        invalid = completion()
        invalid["choices"][0]["message"]["content"] = '{"tool":"finish","tool":"diff","arguments":{},"reason":"x"}'
        for payload in (invalid, completion(id=None), completion(model=None), completion(choices=[])):
            result = self.live(Mock(return_value=process_response(payload)))
            self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
