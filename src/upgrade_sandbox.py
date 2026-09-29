"""Compare one unchanged app and its tests in independent dependency images."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from packaging.requirements import InvalidRequirement, Requirement

from .sandbox import SandboxError, SandboxResult, _Tail, _capture, _remove, preflight


_DOCKERFILE = Path(__file__).resolve().parents[1] / "sandbox" / "upgrade.Dockerfile"
_HARNESS = Path(__file__).resolve().parents[1] / "demo" / "upgrade" / "probe_harness.py"
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RESULT_MARKER = "PROOFRUN_PROBE_RESULT="
_MAX_RESULT_BYTES = 131_072
_PROBE_COMMAND = """set -eu
dependency_version="$(python -c 'from importlib.metadata import version; print(version("pydantic"))')" || exit 120
test_status=0
python -m unittest discover -v || test_status=$?
printf '\\nSECONDLOOK_DEPENDENCY_VERSION=%s\\n' "$dependency_version"
exit "$test_status"
"""


def _validate_image(image: str) -> None:
    if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}", image):
        raise SandboxError("Use a valid Docker image name or image ID.")


def _requirements(path: Path) -> bytes:
    try:
        if not path.is_file() or path.stat().st_size > 65_536:
            raise SandboxError("Provide a requirements file smaller than 64 KiB.")
        contents = path.read_bytes()
        lines = contents.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SandboxError("Cannot read the upgrade requirements file.") from exc
    count = 0
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            raise SandboxError("Upgrade requirements must contain exact package==version pins only.") from None
        pins = list(requirement.specifier)
        if (requirement.url or requirement.marker or len(pins) != 1
                or pins[0].operator != "==" or "*" in pins[0].version):
            raise SandboxError("Upgrade requirements must contain exact package==version pins only.")
        count += 1
    if not count:
        raise SandboxError("The upgrade requirements file contains no pinned dependencies.")
    return contents


def _image_id(image: str) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError("Cannot inspect Docker images; check Docker is running.") from exc
    if result.returncode:
        return None
    image_id = result.stdout.strip()
    if not _IMAGE_ID.fullmatch(image_id):
        raise SandboxError("Docker did not return a complete immutable image ID.")
    return image_id


def _environment_inputs(requirements_path: Path) -> tuple[bytes, bytes]:
    contents = _requirements(Path(requirements_path))
    try:
        dockerfile = _DOCKERFILE.read_bytes()
    except OSError as exc:
        raise SandboxError("The upgrade sandbox Dockerfile is missing.") from exc
    return contents, dockerfile


def _environment_digest(contents: bytes, dockerfile: bytes) -> str:
    return hashlib.sha256(
        b"proofrun.environment.v1\0" + dockerfile + b"\0" + contents
    ).hexdigest()


def environment_fingerprint(requirements_path: Path) -> str:
    """Identify every owned dependency-build input; friendly tags are not identity."""
    return _environment_digest(*_environment_inputs(requirements_path))


def environment_image_tag(requirements_path: Path) -> str:
    """Resolve the cache tag for these exact requirements and Dockerfile bytes."""
    return "proofrun-deps:" + environment_fingerprint(requirements_path)


def build_image(requirements_path: Path, image: str, rebuild: bool = False) -> str:
    """Build only the pinned dependencies, returning the immutable Docker ID."""
    _validate_image(image)
    contents, dockerfile = _environment_inputs(requirements_path)
    cache_tag = "proofrun-deps:" + _environment_digest(contents, dockerfile)
    try:
        ready = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError("Docker is not running or accessible.") from exc
    if ready.returncode:
        raise SandboxError("Docker is not running or accessible.")
    existing = _image_id(cache_tag)
    if existing and not rebuild:
        return existing
    with tempfile.TemporaryDirectory(prefix="secondlook-image-") as directory:
        context = Path(directory)
        (context / "Dockerfile").write_bytes(dockerfile)
        (context / "requirements.txt").write_bytes(contents)
        try:
            built = subprocess.run(
                ["docker", "build", "--tag", cache_tag, "--tag", image,
                 "--file", str(context / "Dockerfile"), str(context)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError("Dependency image build failed or timed out; check Docker and package access.") from exc
        if built.returncode:
            raise SandboxError("Dependency image build failed; check the pinned requirements and package access.")
    image_id = _image_id(cache_tag)
    if image_id is None:
        raise SandboxError("Docker completed the build without a usable dependency image.")
    return image_id


def parse_probe_result(output: str) -> dict | None:
    """Read a single complete harness result; human unittest summaries are not proof."""
    markers = [line[len(_RESULT_MARKER):] for line in output.splitlines()
               if line.startswith(_RESULT_MARKER)]
    if len(markers) != 1 or len(markers[0]) > _MAX_RESULT_BYTES:
        return None
    try:
        value = json.loads(markers[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != "proofrun.probe.v1":
        return None
    return value


class _ProbeTail(_Tail):
    """Bound ordinary logs as before while preserving one bounded evidence record."""

    def __init__(self):
        super().__init__()
        self.pending = ""
        self.markers = []
        self.overflow = False

    def add(self, text: str) -> None:
        for chunk in text.splitlines(keepends=True):
            self.pending += chunk
            if len(self.pending) > _MAX_RESULT_BYTES:
                self.overflow = True
                self.pending = self.pending[-_MAX_RESULT_BYTES:]
            if chunk.endswith("\n"):
                line = self.pending.rstrip("\r\n")
                if line.startswith(_RESULT_MARKER):
                    # Retaining at most two records is sufficient to reject duplication.
                    if len(self.markers) < 2:
                        self.markers.append(line)
                else:
                    super().add(self.pending)
                self.pending = ""

    def text(self) -> str:
        extra = self.pending
        if self.overflow:
            # A deliberately overlong marker/log cannot be mistaken for intact proof.
            extra += "\n" + _RESULT_MARKER + "{}"
        return "\n".join(part for part in (super().text(), extra, *self.markers) if part)


def _integrity_errors(result: dict | None, manifest: dict, code: int) -> list[str]:
    if result is None:
        return ["missing, malformed or duplicate structured result"]
    errors = []
    observed = result.get("tests")
    expected = manifest["expected_test_ids"]
    if not isinstance(observed, list) or any(not isinstance(case, dict) for case in observed):
        return ["invalid executed test records"]
    ids = [case.get("id") for case in observed]
    if (not ids or any(not isinstance(case_id, str) for case_id in ids)
            or len(set(ids)) != len(ids) or set(ids) != set(expected)
            or type(result.get("tests_run")) is not int or result["tests_run"] != len(expected)):
        errors.append("expected and executed test IDs/counts differ")
    statuses = [case.get("status") for case in observed]
    if any(status not in ("pass", "fail", "error") for status in statuses):
        errors.append("skipped, incomplete or unsupported test outcomes")
    if result.get("version") != manifest["expected_version"] and manifest["expected_version"] is not None:
        errors.append("runtime dependency version mismatch")
    if not isinstance(result.get("version"), str) or not result["version"]:
        errors.append("runtime dependency version missing")
    for field in ("source_sha256", "tests_sha256", "harness_sha256"):
        if result.get(field) != manifest[field]:
            errors.append(field + " mismatch")
    if result.get("integrity_errors") != []:
        errors.append("harness reported incomplete or changed inputs")
    if code not in (0, 1) or (code == 0) != bool(statuses and all(value == "pass" for value in statuses)):
        errors.append("exit status and executed outcomes disagree")
    return errors


def run_probe(
    image: str, app_path: Path, tests: list[Path], timeout: int = 30,
    *, expected_test_ids: list[str] | None = None, expected_version: str | None = None,
) -> SandboxResult:
    """Run exactly the supplied app and unittest files with no network or secrets."""
    _validate_image(image)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise SandboxError("Probe timeout must be between 1 and 60 seconds.")
    app_path = Path(app_path)
    tests = [Path(path) for path in tests]
    if not app_path.is_file() or not tests or any(not path.is_file() for path in tests):
        raise SandboxError("Provide an app file and at least one existing unittest file.")
    if (len({path.name for path in tests}) != len(tests)
            or any(not re.fullmatch(r"test_[A-Za-z0-9_]+\.py", path.name) for path in tests)):
        raise SandboxError("Probe tests need distinct test_*.py filenames.")
    strict = expected_test_ids is not None
    if strict and (not isinstance(expected_test_ids, list) or not 1 <= len(expected_test_ids) <= 128
                   or any(not isinstance(case_id, str) or len(case_id) > 240 or not re.fullmatch(
                       r"test_[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.test_[A-Za-z0-9_]+", case_id)
                          for case_id in expected_test_ids)
                   or len(set(expected_test_ids)) != len(expected_test_ids)):
        raise SandboxError("Provide distinct nonempty expected unittest case IDs (at most 128).")
    if expected_version is not None and (
            not strict or not isinstance(expected_version, str)
            or not re.fullmatch(r"[A-Za-z0-9.+!-]{1,80}", expected_version)):
        raise SandboxError("Provide a valid expected version with strict expected test IDs.")
    preflight(image)
    with tempfile.TemporaryDirectory(prefix="secondlook-probe-") as directory:
        source = Path(directory) / "source"
        source.mkdir(mode=0o755)
        # Service UMask=0077 must not prevent the container's nobody UID from
        # traversing its read-only bind mount. The host parent remains private.
        source.chmod(0o755)
        for original, name in [(app_path, "app.py"), *((path, path.name) for path in tests)]:
            try:
                staged = source / name
                shutil.copyfile(original, staged)
                staged.chmod(0o644)
            except OSError as exc:
                raise SandboxError("Cannot stage the app and selected probe tests.") from exc
        manifest = None
        if strict:
            try:
                harness = source / "_proofrun_probe.py"
                shutil.copyfile(_HARNESS, harness)
                harness.chmod(0o644)
                manifest = {
                    "expected_test_ids": expected_test_ids,
                    "expected_version": expected_version,
                    "source_sha256": hashlib.sha256((source / "app.py").read_bytes()).hexdigest(),
                    "tests_sha256": {path.name: hashlib.sha256((source / path.name).read_bytes()).hexdigest()
                                     for path in tests},
                    "harness_sha256": hashlib.sha256(harness.read_bytes()).hexdigest(),
                }
                (source / "_proofrun_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                (source / "_proofrun_manifest.json").chmod(0o644)
            except OSError as exc:
                raise SandboxError("Cannot stage the trusted verification harness.") from exc
        name = f"secondlook-probe-{uuid.uuid4().hex}"
        command = [
            "docker", "run", "--rm", "--pull", "never", "--name", name,
            "--network", "none", "--user", "65534:65534", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "512m", "--cpus", "1", "--log-driver", "none",
            "--mount", f"type=bind,src={source},dst=/probe,readonly",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777", "--workdir", "/probe",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONUNBUFFERED=1",
            "--env", "HOME=/tmp", "--env", "CI=1",
            "--entrypoint", "/bin/sh", image, "-c",
            "exec python /probe/_proofrun_probe.py" if strict else _PROBE_COMMAND,
        ]
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise SandboxError("Could not start Docker for the dependency probe.") from exc
        tail = _ProbeTail() if strict else _Tail()
        reader = threading.Thread(target=_capture, args=(process.stdout, tail), daemon=True)
        reader.start()
        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
        finally:
            _remove(name)
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            reader.join(timeout=1)
        output = tail.text()
        if timed_out:
            status = "timeout"
        elif code in (120, 125, 126, 127) or code < 0:
            status = "error"
        elif strict:
            errors = _integrity_errors(parse_probe_result(output), manifest, code)
            if errors:
                status = "error"
                tail.add("\nProbe incomplete: " + "; ".join(errors) + ".\n")
                output = tail.text()
            else:
                status = "pass" if code == 0 else "fail"
        elif not re.search(r"(?m)^Ran [1-9][0-9]* tests? in ", output):
            status = "error"
            tail.add("\nProbe incomplete: no executed unittest tests were confirmed.")
            output = tail.text()
        else:
            status = "pass" if code == 0 else "fail"
        return SandboxResult(status, code, output, round(time.monotonic() - started, 3))
