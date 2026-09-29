import base64
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.sandbox import SandboxError, _Tail, _fetch, preflight, run_sandbox


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.process = Mock(stdout=io.BytesIO(b"collected tests\n1 passed\n"))
        self.process.wait.return_value = 0
        self.process.poll.return_value = 0
        self.calls = patch("src.sandbox.subprocess.run", return_value=SimpleNamespace(returncode=0))
        self.run = self.calls.start()
        self.addCleanup(self.calls.stop)
        self.start = patch("src.sandbox.subprocess.Popen", return_value=self.process)
        self.popen = self.start.start()
        self.addCleanup(self.start.stop)

    def check(self, **overrides):
        values = dict(repo="owner/repo", sha="a" * 40, image="commit-watch-sandbox:latest",
                      test_command="npm test", github_token="test-secret")
        values.update(overrides)
        return run_sandbox(**values)

    def test_pass_has_isolation_exact_checkout_and_bounded_evidence(self):
        result = self.check()
        self.assertEqual((result.status, result.exit_code), ("pass", 0))
        self.assertIn("1 passed", result.output_tail)
        args = self.popen.call_args.args[0]
        for flag, value in {
            "--network": "none", "--user": "65534:65534", "--cap-drop": "ALL",
            "--security-opt": "no-new-privileges", "--pids-limit": "256",
            "--memory": "1g", "--cpus": "2", "--log-driver": "none", "--pull": "never",
        }.items():
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--read-only", args)
        mount = args[args.index("--mount") + 1]
        self.assertIn("source.git,dst=/source,readonly", mount)
        self.assertNotIn(str(Path.cwd()), mount)
        self.assertNotIn("test-secret", " ".join(args))
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertNotIn("--privileged", args)
        self.assertEqual(args[-2:], ["a" * 40, "npm test"])
        script = args[-4]
        self.assertIn("--no-hardlinks", script)
        self.assertIn('git rev-parse HEAD', script)
        self.assertIn('/opt/test-deps/node_modules', script)
        self.assertEqual(self.cleanup_calls()[0][0:3], ["docker", "rm", "--force"])

    def cleanup_calls(self):
        return [call.args[0] for call in self.run.call_args_list if call.args[0][:2] == ["docker", "rm"]]

    def test_failure_and_infrastructure_exit_codes_are_distinct(self):
        for code, expected in ((1, "fail"), (2, "fail"), (120, "error"), (125, "error"),
                               (126, "error"), (127, "error"), (-9, "error")):
            with self.subTest(code=code):
                self.process.stdout = io.BytesIO(b"failure evidence\n")
                self.process.wait.return_value = code
                self.process.poll.return_value = code
                result = self.check()
                self.assertEqual((result.status, result.exit_code), (expected, code))

    def test_timeout_forces_container_removal_and_kills_cli(self):
        self.process.wait.side_effect = [subprocess.TimeoutExpired("docker", 60), -9]
        self.process.poll.return_value = None
        result = self.check()
        self.assertEqual((result.status, result.exit_code), ("timeout", None))
        self.assertEqual(len(self.cleanup_calls()), 1)
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.wait.call_args_list[0].kwargs, {"timeout": 60})

    def test_interrupt_forces_cleanup_before_propagating(self):
        self.process.wait.side_effect = [KeyboardInterrupt(), -9]
        self.process.poll.return_value = None
        with self.assertRaises(KeyboardInterrupt):
            self.check()
        self.assertEqual(len(self.cleanup_calls()), 1)
        self.process.kill.assert_called_once()

    def test_rejects_sha_injection_and_unbounded_timeout_before_processes(self):
        for changes in ({"sha": "abc1234"}, {"sha": "a" * 40 + ";id"}, {"sha": "--help"},
                        {"timeout": 61}, {"timeout": 0}, {"timeout": True}, {"test_command": " "}):
            with self.subTest(changes=changes):
                with self.assertRaises(SandboxError):
                    self.check(**changes)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_fetch_failure_does_not_launch_container_or_expose_token(self):
        self.run.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=0),
                                SimpleNamespace(returncode=0), SimpleNamespace(returncode=128)]
        with self.assertRaises(SandboxError) as failure:
            self.check()
        self.assertNotIn("test-secret", str(failure.exception))
        self.popen.assert_not_called()

    def test_preflight_fails_on_missing_daemon_or_image_without_pulling(self):
        for responses in ([1], [0, 1]):
            with self.subTest(responses=responses):
                self.run.reset_mock()
                self.run.side_effect = [SimpleNamespace(returncode=code) for code in responses]
                with self.assertRaises(SandboxError):
                    preflight("test-image")
                self.assertFalse(any("pull" in call.args[0] for call in self.run.call_args_list))

    def test_fetch_uses_ephemeral_env_auth_and_exact_ref(self):
        _fetch("owner/repo", "a" * 40, Path("/tmp/test-bare.git"), "test-secret")
        calls = self.run.call_args_list
        self.assertEqual(len(calls), 2)
        fetch = calls[1]
        self.assertEqual(fetch.args[0][-2:], ["https://github.com/owner/repo.git", "a" * 40 + ":refs/heads/review"])
        self.assertNotIn("test-secret", " ".join(fetch.args[0]))
        environment = fetch.kwargs["env"]
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        encoded = environment["GIT_CONFIG_VALUE_0"].split()[-1]
        self.assertEqual(base64.b64decode(encoded), b"x-access-token:test-secret")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")


class TailTests(unittest.TestCase):
    def test_last_thirty_lines_and_extremely_long_lines_are_bounded(self):
        tail = _Tail()
        for number in range(100):
            tail.add(f"line {number}\n")
        self.assertEqual(tail.text().splitlines(), [f"line {number}" for number in range(70, 100)])
        for _ in range(1_000):
            tail.add("x" * 4_096)
        self.assertLessEqual(len(tail.partial), 2_000)
        self.assertLess(len(tail.text()), 63_000)
        self.assertEqual(len(tail.text().splitlines()), 30)
        self.assertIn("[line truncated]", tail.text())


if __name__ == "__main__":
    unittest.main()
