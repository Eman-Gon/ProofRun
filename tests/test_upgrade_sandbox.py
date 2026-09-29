import io
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.sandbox import SandboxError
from src.upgrade_sandbox import (
    _HARNESS, _PROBE_COMMAND, _ProbeTail, build_image, environment_fingerprint,
    environment_image_tag, parse_probe_result, run_probe,
)


IMAGE_ID = "sha256:" + "a" * 64
PASS_OUTPUT = b"Ran 2 tests in 0.001s\n\nOK\nSECONDLOOK_DEPENDENCY_VERSION=1.10.18\n"


class ImageBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "deps.txt"
        self.requirements.write_text("pydantic==1.10.18\n")
        (self.root / ".env").write_text("PRIVATE_KEY=do-not-copy\n")
        self.commands = []
        self.contexts = []
        self.inspect_count = 0

    def docker(self, command, **kwargs):
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            self.inspect_count += 1
            return SimpleNamespace(returncode=0 if self.inspect_count > 1 else 1, stdout=IMAGE_ID + "\n")
        if command[:2] == ["docker", "build"]:
            context = Path(command[-1])
            self.contexts.append(context)
            self.assertEqual({path.name for path in context.iterdir()}, {"Dockerfile", "requirements.txt"})
            self.assertEqual((context / "requirements.txt").read_bytes(), self.requirements.read_bytes())
            dockerfile = (context / "Dockerfile").read_text()
            self.assertIn("FROM python:3.12-slim-bookworm", dockerfile)
            self.assertIn("USER 65534:65534", dockerfile)
            self.assertNotIn("do-not-copy", dockerfile)
            self.assertEqual(kwargs["timeout"], 600)
        return SimpleNamespace(returncode=0, stdout="")

    def test_build_context_contains_only_dockerfile_and_exact_requirements(self):
        with patch("src.upgrade_sandbox.subprocess.run", side_effect=self.docker):
            self.assertEqual(build_image(self.requirements, "secondlook:old"), IMAGE_ID)
        self.assertEqual(len(self.contexts), 1)
        self.assertFalse(self.contexts[0].exists())
        self.assertFalse(any("--build-arg" in command or "--secret" in command for command in self.commands))

    def test_existing_image_is_reused_and_rebuild_is_explicit(self):
        for rebuild, expected in ((False, 0), (True, 1)):
            with self.subTest(rebuild=rebuild):
                self.inspect_count = 1
                self.contexts.clear()
                with patch("src.upgrade_sandbox.subprocess.run", side_effect=self.docker):
                    self.assertEqual(build_image(self.requirements, "secondlook:old", rebuild), IMAGE_ID)
                self.assertEqual(len(self.contexts), expected)

    def test_cache_identity_changes_with_requirements_or_dockerfile(self):
        original = environment_image_tag(self.requirements)
        self.assertEqual(original, "proofrun-deps:" + environment_fingerprint(self.requirements))
        self.requirements.write_text("pydantic==2.8.2\n")
        changed = environment_image_tag(self.requirements)
        self.assertNotEqual(original, changed)
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text("FROM different-base\n")
        with patch("src.upgrade_sandbox._DOCKERFILE", dockerfile):
            self.assertNotEqual(changed, environment_image_tag(self.requirements))

    def test_friendly_tag_is_never_used_to_decide_cache_reuse(self):
        self.inspect_count = 1
        with patch("src.upgrade_sandbox.subprocess.run", side_effect=self.docker):
            build_image(self.requirements, "secondlook:old")
        inspected = [command[-1] for command in self.commands if command[:3] == ["docker", "image", "inspect"]]
        self.assertEqual(inspected, [environment_image_tag(self.requirements)])

    def test_missing_empty_unpinned_or_remote_requirements_never_build(self):
        for content in ("", "# empty\n", "pydantic>=1\n", "pydantic==1.*\n", "-r /private/other.txt\n",
                        "pydantic @ https://secret:token@example.com/package.whl\n", "--index-url https://private\n"):
            with self.subTest(content=content):
                self.requirements.write_text(content)
                with patch("src.upgrade_sandbox.subprocess.run") as run:
                    with self.assertRaises(SandboxError):
                        build_image(self.requirements, "secondlook:old")
                    run.assert_not_called()
        with self.assertRaises(SandboxError):
            build_image(self.root / "absent.txt", "secondlook:old")

    def test_missing_dockerfile_and_invalid_image_fail_before_docker(self):
        with patch("src.upgrade_sandbox.subprocess.run") as run:
            with self.assertRaises(SandboxError):
                build_image(self.requirements, "--help")
            with patch("src.upgrade_sandbox._DOCKERFILE", self.root / "missing"):
                with self.assertRaisesRegex(SandboxError, "Dockerfile"):
                    build_image(self.requirements, "secondlook:old")
            run.assert_not_called()

    def test_unavailable_docker_and_failed_build_have_safe_errors(self):
        with patch("src.upgrade_sandbox.subprocess.run", side_effect=OSError("private details")):
            with self.assertRaisesRegex(SandboxError, "Docker is not running") as failure:
                build_image(self.requirements, "secondlook:old")
            self.assertNotIn("private details", str(failure.exception))
        def failed_build(command, **kwargs):
            value = self.docker(command, **kwargs)
            return SimpleNamespace(returncode=1) if command[:2] == ["docker", "build"] else value
        with patch("src.upgrade_sandbox.subprocess.run", side_effect=failed_build):
            with self.assertRaisesRegex(SandboxError, "image build failed"):
                build_image(self.requirements, "secondlook:old")
        self.assertFalse(self.contexts[0].exists())

    def test_build_timeout_cleans_context_and_incomplete_image_id_is_rejected(self):
        def slow_build(command, **kwargs):
            value = self.docker(command, **kwargs)
            if command[:2] == ["docker", "build"]:
                raise subprocess.TimeoutExpired(command, 600)
            return value
        with patch("src.upgrade_sandbox.subprocess.run", side_effect=slow_build):
            with self.assertRaisesRegex(SandboxError, "timed out"):
                build_image(self.requirements, "secondlook:old")
        self.assertFalse(self.contexts[0].exists())
        with patch("src.upgrade_sandbox.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="abc123")):
            with self.assertRaisesRegex(SandboxError, "immutable image ID"):
                build_image(self.requirements, "secondlook:old")


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "original_app.py"
        self.app.write_text("def value(): return 1\n")
        self.test = self.root / "test_app.py"
        self.test.write_text("import unittest\n")
        (self.root / ".env").write_text("PRIVATE_KEY=do-not-copy\n")
        (self.root / "test_unselected.py").write_text("raise AssertionError\n")
        self.process = Mock(stdout=io.BytesIO(PASS_OUTPUT))
        self.process.wait.return_value = 0
        self.process.poll.return_value = 0
        self.source = None
        self.command = None
        for name, value in (("preflight", None), ("_remove", None)):
            mocked = patch("src.upgrade_sandbox." + name, return_value=value)
            setattr(self, name, mocked.start())
            self.addCleanup(mocked.stop)
        self.start = patch("src.upgrade_sandbox.subprocess.Popen", side_effect=self.launch)
        self.popen = self.start.start()
        self.addCleanup(self.start.stop)

    def launch(self, command, **kwargs):
        self.command = command
        mount = command[command.index("--mount") + 1]
        self.source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        self.assertEqual({path.name for path in self.source.iterdir()}, {"app.py", "test_app.py"})
        self.assertEqual((self.source / "app.py").read_bytes(), self.app.read_bytes())
        self.assertEqual((self.source / "test_app.py").read_bytes(), self.test.read_bytes())
        return self.process

    def check(self, **overrides):
        values = dict(image=IMAGE_ID, app_path=self.app, tests=[self.test])
        values.update(overrides)
        return run_probe(**values)

    def test_probe_stages_only_selected_files_and_runs_without_host_access(self):
        result = self.check()
        self.assertEqual((result.status, result.exit_code), ("pass", 0))
        self.assertIn("SECONDLOOK_DEPENDENCY_VERSION=1.10.18", result.output_tail)
        self.assertFalse(self.source.exists())
        args = self.command
        for flag, value in {
            "--network": "none", "--pull": "never", "--user": "65534:65534",
            "--cap-drop": "ALL", "--security-opt": "no-new-privileges",
            "--pids-limit": "128", "--memory": "512m", "--cpus": "1", "--log-driver": "none",
        }.items():
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--read-only", args)
        self.assertEqual(args.count("--mount"), 1)
        self.assertIn("dst=/probe,readonly", args[args.index("--mount") + 1])
        self.assertNotIn(str(self.root), " ".join(args))
        self.assertNotIn("do-not-copy", " ".join(args))
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertNotIn("--env-file", args)
        self.assertIn("python -m unittest discover -v", args[-1])
        self.assertIn("version(\"pydantic\")", args[-1])
        self._remove.assert_called_once_with(args[args.index("--name") + 1])

    def test_failed_assertions_docker_errors_and_zero_tests_are_distinct(self):
        for code, output, expected in (
            (1, b"Ran 2 tests in 0.001s\nFAILED (failures=1)\n", "fail"),
            (125, b"Docker failed\n", "error"), (120, b"No pydantic installed\n", "error"),
            (-9, b"Killed\n", "error"),
            (0, b"Ran 0 tests in 0.000s\nOK\n", "error"), (0, b"No test summary\n", "error"),
        ):
            with self.subTest(code=code, output=output):
                self.process.stdout = io.BytesIO(output)
                self.process.wait.return_value = code
                self.process.poll.return_value = code
                result = self.check()
                self.assertEqual((result.status, result.exit_code), (expected, code))

    def test_long_failure_preserves_final_version_marker_in_bounded_tail(self):
        self.process.stdout = io.BytesIO(
            b"traceback detail\n" * 50
            + b"Ran 1 test in 0.001s\nFAILED (failures=1)\nSECONDLOOK_DEPENDENCY_VERSION=2.8.2\n"
        )
        self.process.wait.return_value = self.process.poll.return_value = 1
        result = self.check()
        self.assertEqual(result.status, "fail")
        self.assertEqual(len(result.output_tail.splitlines()), 30)
        self.assertTrue(result.output_tail.endswith("SECONDLOOK_DEPENDENCY_VERSION=2.8.2"))

    def test_timeout_removes_container_before_killing_cli(self):
        actions = []
        self._remove.side_effect = lambda name: actions.append("remove")
        self.process.kill.side_effect = lambda: actions.append("kill")
        self.process.wait.side_effect = [subprocess.TimeoutExpired("docker", 30), -9]
        self.process.poll.return_value = None
        result = self.check()
        self.assertEqual((result.status, result.exit_code), ("timeout", None))
        self.assertEqual(actions, ["remove", "kill"])
        self.assertFalse(self.source.exists())

    def test_interrupt_also_removes_container_and_staged_files(self):
        self.process.wait.side_effect = [KeyboardInterrupt(), -9]
        self.process.poll.return_value = None
        with self.assertRaises(KeyboardInterrupt):
            self.check()
        self._remove.assert_called_once()
        self.process.kill.assert_called_once()
        self.assertFalse(self.source.exists())

    def test_invalid_paths_test_names_image_and_timeout_never_start(self):
        invalid = self.root / "probe.py"
        invalid.write_text("import unittest\n")
        for values in ({"image": "--help"}, {"timeout": True}, {"timeout": 0}, {"timeout": 61},
                       {"app_path": self.root / "missing"}, {"tests": []}, {"tests": [invalid]},
                       {"tests": [self.test, self.test]}, {"tests": [self.root / "test_missing.py"]}):
            with self.subTest(values=values):
                with self.assertRaises(SandboxError):
                    self.check(**values)
        self.preflight.assert_not_called()
        self.popen.assert_not_called()

    def test_start_failure_cleans_staged_files(self):
        def fail(command, **kwargs):
            self.launch(command, **kwargs)
            raise OSError("failed start")
        self.popen.side_effect = fail
        with self.assertRaisesRegex(SandboxError, "Could not start Docker"):
            self.check()
        self.assertFalse(self.source.exists())


class ProbeCommandTests(unittest.TestCase):
    def test_fixed_shell_command_preserves_test_exit_code_and_prints_version_last(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            python.write_text("""#!/bin/sh
if [ "$1" = "-c" ]; then printf '2.8.2\\n'; exit 0; fi
printf 'Ran 1 test in 0.001s\\nFAILED (failures=1)\\n' >&2
exit 1
""")
            python.chmod(0o755)
            process = subprocess.run(
                ["/bin/sh", "-c", _PROBE_COMMAND], env={"PATH": directory},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5,
            )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout.count("SECONDLOOK_DEPENDENCY_VERSION="), 1)
        self.assertTrue(process.stdout.rstrip().endswith("SECONDLOOK_DEPENDENCY_VERSION=2.8.2"))


class StrictProbeTests(unittest.TestCase):
    """Docker transport is mocked here; execution claims belong to harness/live tests."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app.py"
        self.test = self.root / "test_app.py"
        self.app.write_text("def value(): return 1\n")
        self.test.write_text("import unittest\n")
        self.expected = ["test_app.TestApp.test_value"]
        self.transform = lambda value: value
        self.code = 0
        self.extra_output = ""
        self.command = None
        for target in ("preflight", "_remove"):
            patcher = patch("src.upgrade_sandbox." + target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("src.upgrade_sandbox.subprocess.Popen", side_effect=self.launch)
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def launch(self, command, **kwargs):
        self.command = command
        mount = command[command.index("--mount") + 1]
        source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        self.assertEqual({path.name for path in source.iterdir()}, {
            "app.py", "test_app.py", "_proofrun_probe.py", "_proofrun_manifest.json",
        })
        self.assertEqual((source / "_proofrun_probe.py").read_bytes(), _HARNESS.read_bytes())
        manifest = json.loads((source / "_proofrun_manifest.json").read_text())
        result = {
            "schema_version": "proofrun.probe.v1", "version": "2.8.2", "tests_run": 1,
            "tests": [{"id": self.expected[0], "status": "pass"}], "integrity_errors": [],
            **{field: manifest[field] for field in ("source_sha256", "tests_sha256", "harness_sha256")},
        }
        transformed = self.transform(result)
        output = self.extra_output + "\nPROOFRUN_PROBE_RESULT=" + json.dumps(transformed) + "\n"
        process = Mock(stdout=io.BytesIO(output.encode()))
        process.wait.return_value = process.poll.return_value = self.code
        return process

    def check(self, **overrides):
        values = dict(image=IMAGE_ID, app_path=self.app, tests=[self.test],
                      expected_test_ids=self.expected, expected_version="2.8.2")
        values.update(overrides)
        return run_probe(**values)

    def test_complete_exact_structured_record_is_required_for_pass(self):
        result = self.check()
        self.assertEqual(result.status, "pass")
        self.assertEqual(self.command[-1], "exec python /probe/_proofrun_probe.py")
        self.assertEqual(parse_probe_result(result.output_tail)["tests_run"], 1)

    def test_service_umask_keeps_private_parent_and_readable_container_inputs(self):
        def inspect_permissions(command, **kwargs):
            mount = command[command.index("--mount") + 1]
            source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(source.parent.stat().st_mode), 0o700)
            for staged in source.iterdir():
                self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o644, staged.name)
            return self.launch(command, **kwargs)

        self.popen.side_effect = inspect_permissions
        previous = os.umask(0o077)
        try:
            self.assertEqual(self.check().status, "pass")
        finally:
            os.umask(previous)

    def test_missing_wrong_duplicate_empty_skipped_and_stale_results_are_rejected(self):
        invalid = [
            {"tests": []}, {"tests_run": 0}, {"tests_run": True},
            {"tests": [{"id": "wrong", "status": "pass"}]},
            {"tests": [{"id": self.expected[0], "status": "pass"}] * 2, "tests_run": 2},
        ]
        invalid.extend({"tests": [{"id": self.expected[0], "status": status}]}
                       for status in ("skip", "expected_failure", "unexpected_success", "incomplete"))
        invalid.extend([
            {"version": "1.10.18"}, {"source_sha256": "0" * 64}, {"tests_sha256": {}},
            {"harness_sha256": "0" * 64}, {"integrity_errors": ["changed"]},
            {"schema_version": "different"}, {"tests": "invalid"}, {"tests": [{"id": []}]},
        ])
        for changes in invalid:
            with self.subTest(changes=changes):
                self.transform = lambda result: {**result, **changes}
                self.assertEqual(self.check().status, "error")

    def test_assertion_failure_is_distinct_from_integrity_error_and_keeps_detail(self):
        self.code = 1
        self.transform = lambda result: {
            **result, "tests": [{"id": self.expected[0], "status": "fail", "detail": "assertion detail"}],
        }
        result = self.check()
        self.assertEqual(result.status, "fail")
        self.assertEqual(parse_probe_result(result.output_tail)["tests"][0]["detail"], "assertion detail")
        self.code = 0
        self.assertEqual(self.check().status, "error")

    def test_multiple_markers_cannot_forge_a_passing_result(self):
        self.extra_output = 'PROOFRUN_PROBE_RESULT={"schema_version":"proofrun.probe.v1"}\n'
        self.assertEqual(self.check().status, "error")

    def test_invalid_expectations_never_start_docker(self):
        for values in ({"expected_test_ids": []}, {"expected_test_ids": self.expected * 2},
                       {"expected_test_ids": ["bogus"]}, {"expected_test_ids": "test_app.X.test_x"},
                       {"expected_version": "--bad version"},
                       {"expected_test_ids": None, "expected_version": "2.8.2"}):
            with self.subTest(values=values), self.assertRaises(SandboxError):
                self.check(**values)
        self.popen.assert_not_called()

    def test_bounded_tail_preserves_long_structured_records_and_rejects_overflow(self):
        record = {"schema_version": "proofrun.probe.v1", "detail": "trace " * 1500}
        tail = _ProbeTail()
        text = "log line\n" * 100 + "PROOFRUN_PROBE_RESULT=" + json.dumps(record) + "\n"
        for offset in range(0, len(text), 97):
            tail.add(text[offset:offset + 97])
        self.assertEqual(parse_probe_result(tail.text()), record)
        self.assertLess(len(tail.text()), 10_000)
        tail.add("x" * 140_000 + "\n")
        self.assertIsNone(parse_probe_result(tail.text()))


class MeasuredHarnessTests(unittest.TestCase):
    """Real local subprocess tests of harness semantics, not pinned Docker proof."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app.py"
        self.app.write_text("def value(): return 1\n")
        self.harness = self.root / "_proofrun_probe.py"
        self.harness.write_bytes(_HARNESS.read_bytes())
        self.test = self.root / "test_measured.py"
        self.test.write_text("import unittest\nfrom app import value\nclass TestMeasured(unittest.TestCase):\n"
                             "    def test_value(self): self.assertEqual(value(), 1)\n")

    def execute(self, expected=None, expected_version=None):
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            "expected_test_ids": expected or ["test_measured.TestMeasured.test_value"],
            "expected_version": expected_version or version("pydantic"),
            "source_sha256": sha(self.app), "tests_sha256": {self.test.name: sha(self.test)},
            "harness_sha256": sha(self.harness),
        }
        (self.root / "_proofrun_manifest.json").write_text(json.dumps(manifest))
        process = subprocess.run([sys.executable, "-B", str(self.harness)], cwd=self.root,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
        return process, parse_probe_result(process.stdout)

    def test_real_success_records_exact_id_count_version_and_hashes(self):
        process, record = self.execute()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(record["tests_run"], 1)
        self.assertEqual(record["tests"], [{"id": "test_measured.TestMeasured.test_value", "status": "pass"}])
        self.assertEqual(record["version"], version("pydantic"))
        self.assertEqual(record["source_sha256"], hashlib.sha256(self.app.read_bytes()).hexdigest())

    def test_real_failure_records_detail_without_forgiving_error(self):
        self.app.write_text("def value(): raise ValueError('measured failure')\n")
        process, record = self.execute()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(record["tests"][0]["status"], "error")
        self.assertIn("ValueError: measured failure", record["tests"][0]["detail"])

    def test_real_skip_duplicate_wrong_set_zero_and_version_mismatch_do_not_pass(self):
        original = self.test.read_text()
        variations = [
            (original.replace("    def test_value", "    @unittest.skip('no execution')\n    def test_value"), {}),
            (original + "\ndef load_tests(loader, suite, pattern):\n"
             "    return unittest.TestSuite([TestMeasured('test_value'), TestMeasured('test_value')])\n", {}),
            (original, {"expected": ["test_measured.TestMeasured.test_absent"]}),
            ("import unittest\n", {}),
            (original, {"expected_version": "0.0.0"}),
        ]
        for source, kwargs in variations:
            with self.subTest(kwargs=kwargs, source=source):
                self.test.write_text(source)
                process, record = self.execute(**kwargs)
                self.assertEqual(process.returncode, 120, process.stdout)
                self.assertTrue(record["integrity_errors"])


if __name__ == "__main__":
    unittest.main()
