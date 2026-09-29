"""Compare one unchanged app and its tests in independent dependency images."""

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
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
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


def build_image(requirements_path: Path, image: str, rebuild: bool = False) -> str:
    """Build only the pinned dependencies, returning the immutable Docker ID."""
    _validate_image(image)
    contents = _requirements(Path(requirements_path))
    if not _DOCKERFILE.is_file():
        raise SandboxError("The upgrade sandbox Dockerfile is missing.")
    try:
        ready = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError("Docker is not running or accessible.") from exc
    if ready.returncode:
        raise SandboxError("Docker is not running or accessible.")
    existing = _image_id(image)
    if existing and not rebuild:
        return existing
    with tempfile.TemporaryDirectory(prefix="secondlook-image-") as directory:
        context = Path(directory)
        shutil.copyfile(_DOCKERFILE, context / "Dockerfile")
        (context / "requirements.txt").write_bytes(contents)
        try:
            built = subprocess.run(
                ["docker", "build", "--tag", image, "--file", str(context / "Dockerfile"), str(context)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError("Dependency image build failed or timed out; check Docker and package access.") from exc
        if built.returncode:
            raise SandboxError("Dependency image build failed; check the pinned requirements and package access.")
    image_id = _image_id(image)
    if image_id is None:
        raise SandboxError("Docker completed the build without a usable dependency image.")
    return image_id


def run_probe(image: str, app_path: Path, tests: list[Path], timeout: int = 30) -> SandboxResult:
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
    preflight(image)
    with tempfile.TemporaryDirectory(prefix="secondlook-probe-") as directory:
        source = Path(directory) / "source"
        source.mkdir(mode=0o755)
        for original, name in [(app_path, "app.py"), *((path, path.name) for path in tests)]:
            try:
                staged = source / name
                shutil.copyfile(original, staged)
                staged.chmod(0o644)
            except OSError as exc:
                raise SandboxError("Cannot stage the app and selected probe tests.") from exc
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
            "--entrypoint", "/bin/sh", image, "-c", _PROBE_COMMAND,
        ]
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise SandboxError("Could not start Docker for the dependency probe.") from exc
        tail = _Tail()
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
        elif not re.search(r"(?m)^Ran [1-9][0-9]* tests? in ", output):
            status = "error"
            tail.add("\nProbe incomplete: no executed unittest tests were confirmed.")
            output = tail.text()
        else:
            status = "pass" if code == 0 else "fail"
        return SandboxResult(status, code, output, round(time.monotonic() - started, 3))
