"""Run one immutable commit's trusted test command in a constrained container."""

import base64
from collections import deque
import codecs
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Literal
import uuid

from .github_fetch import validate_repo


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxResult:
    status: Literal["pass", "fail", "timeout", "error"]
    exit_code: int | None
    output_tail: str
    duration_seconds: float


def preflight(image: str) -> None:
    if not image.strip() or image.startswith("-"):
        raise SandboxError("Set a valid SANDBOX_IMAGE name.")
    for command, message in (
        (["docker", "info", "--format", "{{.ServerVersion}}"], "Docker is not running or accessible."),
        (["docker", "image", "inspect", image], "Sandbox image is missing; build it before checking commits."),
    ):
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(message) from exc
        if result.returncode:
            raise SandboxError(message)


class _Tail:
    """At most 30 lines, each at most 2,000 characters, even without newlines."""

    def __init__(self):
        self.lines = deque(maxlen=30)
        self.partial = ""
        self.truncated = False

    def add(self, text: str) -> None:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            combined = self.partial + part
            self.truncated = self.truncated or len(combined) > 2_000
            self.partial = combined[-2_000:]
            if index < len(parts) - 1:
                self.lines.append(("[line truncated] " if self.truncated else "") + self.partial)
                self.partial, self.truncated = "", False

    def text(self) -> str:
        lines = list(self.lines)
        if self.partial:
            lines.append(("[line truncated] " if self.truncated else "") + self.partial)
        return "\n".join(lines[-30:])


def _capture(stream, tail: _Tail) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        with stream:
            while chunk := stream.read(4_096):
                tail.add(decoder.decode(chunk))
            tail.add(decoder.decode(b"", final=True))
    except OSError:
        pass


def _fetch(repo: str, sha: str, source: Path, token: str) -> None:
    # Disable inherited Git overrides/config and supply credentials only to Git's
    # environment. No token enters argv, the bare repo config, or the container.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    if token:
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment.update({
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
        })
    commands = [
        ["git", "init", "--quiet", "--bare", "--initial-branch=review", "--template=", str(source)],
        ["git", "--git-dir", str(source), "fetch", "--quiet", "--depth=1", "--no-tags",
         f"https://github.com/{repo}.git", f"{sha}:refs/heads/review"],
    ]
    for command in commands:
        try:
            result = subprocess.run(
                command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError("Cannot fetch the exact commit; check Git, GitHub access, and the SHA.") from exc
        if result.returncode:
            raise SandboxError("Cannot fetch the exact commit; check Git, GitHub access, and the SHA.")


_CHECKOUT_AND_TEST = """set -eu
# Local upload-pack is a child process; use ephemeral HOME config so it also
# trusts the one read-only bare source owned by the host's user.
git config --global --add safe.directory /source || exit 120
git clone --quiet --no-hardlinks /source /work/repo || exit 120
cd /work/repo || exit 120
git checkout --quiet --detach "$1" || exit 120
[ "$(git rev-parse HEAD)" = "$1" ] || exit 120
if [ -d /opt/test-deps/node_modules ]; then
    ln -s /opt/test-deps/node_modules node_modules || exit 120
fi
exec /bin/sh -c "$2"
"""


def _remove(name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "--force", name], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_sandbox(
    repo: str, sha: str, image: str, test_command: str, timeout: int = 60, github_token: str = "",
) -> SandboxResult:
    validate_repo(repo)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise SandboxError("Sandbox requires a full 40-character hexadecimal commit SHA.")
    sha = sha.lower()
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise SandboxError("Sandbox timeout must be between 1 and 60 seconds.")
    if not test_command.strip():
        raise SandboxError("Set a trusted TEST_COMMAND before running the sandbox.")
    preflight(image)
    with tempfile.TemporaryDirectory(prefix="commit-watch-") as directory:
        source = Path(directory) / "source.git"
        _fetch(repo, sha, source, github_token)
        name = f"commit-watch-{uuid.uuid4().hex}"
        command = [
            "docker", "run", "--rm", "--pull", "never", "--name", name,
            "--network", "none", "--user", "65534:65534", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "1g", "--cpus", "2", "--log-driver", "none",
            "--mount", f"type=bind,src={source},dst=/source,readonly",
            "--tmpfs", "/work:rw,exec,nosuid,size=512m,mode=1777",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777", "--workdir", "/work",
            "--env", "PYTHONPATH=/work/repo/src:/work/repo", "--env", "HOME=/tmp", "--env", "CI=1",
            "--entrypoint", "/bin/sh", image, "-c", _CHECKOUT_AND_TEST,
            "commit-watch", sha, test_command,
        ]
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise SandboxError("Could not start Docker for the sandbox.") from exc
        tail = _Tail()
        reader = threading.Thread(target=_capture, args=(process.stdout, tail), daemon=True)
        reader.start()
        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
        finally:
            # Also runs for KeyboardInterrupt; kill the container before the CLI
            # so untrusted tests cannot continue after the host stops waiting.
            _remove(name)
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            reader.join(timeout=1)
        status = "timeout" if timed_out else (
            "pass" if code == 0 else "error" if code in (120, 125, 126, 127) or code < 0 else "fail"
        )
        return SandboxResult(status, code, tail.text(), round(time.monotonic() - started, 3))
