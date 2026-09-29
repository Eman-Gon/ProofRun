"""Bounded release execution; HTTP evidence is collected outside application containers.

The caller owns snapshot/configuration approval. This module never interprets model
output as a shell command, copies host credentials into Docker, or decides whether
an observed behavior is a product defect.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4


MAX_BODY = 65_536
MAX_OUTPUT = 32_768
MAX_STEPS = 30
_IMAGE = re.compile(r"(?:sha256:|[A-Za-z0-9][A-Za-z0-9._/:@-]*@sha256:)[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-f0-9]{64}\Z")
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_SECRET_NAME = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_KEY|PRIVATE_KEY|PROXY", re.I)


class RuntimeFailure(Exception):
    pass


class DeadlineExceeded(RuntimeFailure):
    pass


class HTTPUnavailable(RuntimeFailure):
    pass


def _remaining(deadline: float, maximum: float = 60) -> float:
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise RuntimeFailure("A finite execution deadline is required.")
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExceeded("The execution deadline expired.")
    return min(left, maximum)


def _docker(args: list[str], deadline: float, *, limit: int = MAX_OUTPUT) -> tuple[int, str]:
    """Drain Docker output continuously while retaining only a bounded tail."""
    timeout = _remaining(deadline, 3600)
    try:
        process = subprocess.Popen(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        raise RuntimeFailure("Docker could not be started.") from None
    tail = bytearray()

    def drain():
        try:
            while chunk := process.stdout.read(4096):
                tail.extend(chunk)
                if len(tail) > limit:
                    del tail[:-limit]
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise DeadlineExceeded("A Docker operation exceeded its time limit.") from None
    finally:
        reader.join(timeout=1)
        if process.stdout:
            process.stdout.close()
    return code, bytes(tail).decode("utf-8", errors="replace")


def _checked(args: list[str], deadline: float) -> str:
    code, output = _docker(args, deadline)
    if code != 0:
        raise RuntimeFailure("Docker setup or container inspection failed.")
    return output.strip()


def _argv(value, name: str) -> list[str]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 64
            or any(not isinstance(item, str) or not item or len(item) > 4096
                   or any(c in item for c in "\x00\r\n") for item in value)):
        raise RuntimeFailure(name + " must be a bounded argv list.")
    return value


def _path(value) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value):
        raise RuntimeFailure("HTTP steps require an absolute path on the approved target.")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise RuntimeFailure("HTTP steps cannot select another URL or a fragment.")
    return value


def _steps(steps) -> list[dict]:
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise RuntimeFailure("Provide between 1 and 30 HTTP steps.")
    result = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {"method", "path", "json"}:
            raise RuntimeFailure("A step supports only method, path and optional JSON.")
        if not isinstance(step.get("method"), str) or step["method"] not in _METHODS:
            raise RuntimeFailure("The HTTP method is unsupported.")
        item = {"method": step["method"], "path": _path(step.get("path"))}
        if "json" in step:
            try:
                body = json.dumps(step["json"], allow_nan=False).encode()
            except (TypeError, ValueError, RecursionError):
                raise RuntimeFailure("HTTP request JSON is invalid.") from None
            if len(body) > MAX_BODY or step["method"] in {"GET", "HEAD"}:
                raise RuntimeFailure("HTTP JSON body is oversized or unsupported for this method.")
            item["json"] = step["json"]
        result.append(item)
    return result


def _runtime(snapshot: Path, image: str, runtime: dict, tests: bool) -> tuple[Path, list[str], str, dict]:
    if not isinstance(image, str) or not _IMAGE.fullmatch(image):
        raise RuntimeFailure("Use an immutable sha256 image ID or repository digest.")
    snapshot = Path(snapshot).resolve()
    if not snapshot.is_dir() or "," in str(snapshot):
        raise RuntimeFailure("The approved source snapshot directory is unavailable.")
    if not isinstance(runtime, dict):
        raise RuntimeFailure("Approved runtime configuration is required.")
    command = _argv(runtime.get("test_command" if tests else "command"), "Runtime command")
    workdir = runtime.get("workdir", ".")
    if (not isinstance(workdir, str) or len(workdir) > 512 or "\\" in workdir
            or any(ord(c) < 32 for c in workdir) or PurePosixPath(workdir).is_absolute()
            or ".." in PurePosixPath(workdir).parts):
        raise RuntimeFailure("Runtime working directory must stay inside the source snapshot.")
    target = (snapshot / workdir).resolve()
    if not target.is_relative_to(snapshot) or not target.is_dir():
        raise RuntimeFailure("Runtime working directory is unavailable.")
    env = runtime.get("env", {})
    if (not isinstance(env, dict) or len(env) > 32
            or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k)
                   or _SECRET_NAME.search(k) or not isinstance(v, str) or len(v) > 2048
                   or any(c in v for c in "\x00\r\n") for k, v in env.items())):
        raise RuntimeFailure("Runtime environment must contain bounded non-secret synthetic settings.")
    if not tests:
        if type(runtime.get("port")) is not int or not 1024 <= runtime["port"] <= 65535:
            raise RuntimeFailure("Application port must be between 1024 and 65535.")
        _path(runtime.get("health_path"))
        startup = runtime.get("startup_seconds", 30)
        if type(startup) not in (int, float) or not math.isfinite(startup) or not 0 < startup <= 60:
            raise RuntimeFailure("Startup timeout must be between zero and 60 seconds.")
    return snapshot, command, "/workspace" + ("/" + workdir if workdir != "." else ""), env


@contextmanager
def _container(snapshot: Path, image: str, runtime: dict, deadline: float, *, tests: bool = False):
    snapshot, command, workdir, env = _runtime(snapshot, image, runtime, tests)
    name = "proofrun-release-" + uuid4().hex
    network = name + "-network"
    network_attempted = container_attempted = False
    try:
        details = json.loads(_checked(["image", "inspect", "--format", "{{json .}}", image], deadline))
        image_id = details.get("Id", "")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeFailure("Docker returned an invalid immutable image identity.")
        if image.startswith("sha256:") and image != image_id:
            raise RuntimeFailure("Docker resolved a different image from the requested identity.")
        if (details.get("Config") or {}).get("Volumes"):
            raise RuntimeFailure("Runtime images must not declare persistent volumes.")
        image_env = (details.get("Config") or {}).get("Env") or []
        if any(_SECRET_NAME.search(value.partition("=")[0]) and value.partition("=")[2] for value in image_env):
            raise RuntimeFailure("The runtime image includes a secret or proxy environment setting.")
        if not tests:
            network_attempted = True
            _checked(["network", "create", "--internal", network], deadline)
        args = ["run", "--detach", "--pull", "never", "--name", name,
                "--network", "none" if tests else network, "--user", "65534:65534", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "128", "--memory", "512m", "--cpus", "1",
                "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
                "--mount", f"type=bind,src={snapshot},dst=/workspace,readonly",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                "--tmpfs", "/data:rw,noexec,nosuid,size=64m,mode=1777",
                "--workdir", workdir, "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "CI=1"]
        for key, value in env.items():
            args.extend(["--env", key + "=" + value])
        if not tests:
            args.extend(["--publish", f"127.0.0.1::{runtime['port']}"])
        args.extend(["--entrypoint", command[0], image_id, *command[1:]])
        container_attempted = True
        container_id = _checked(args, deadline)
        if not _ID.fullmatch(container_id):
            raise RuntimeFailure("Docker did not report a valid container identity.")
        environment = {"image_id": image_id, "requested_image": image, "container_id": container_id,
                       "network": "none" if tests else "internal", "execution_backend": "docker",
                       "source_mount": "read_only", "evidence_collector": "host_http" if not tests else "docker_exit"}
        base_url = None
        if not tests:
            info = json.loads(_checked(["inspect", "--format", "{{json .}}", container_id], deadline))
            ports = (info.get("NetworkSettings") or {}).get("Ports", {}).get(f"{runtime['port']}/tcp")
            if (info.get("Image") != image_id or not isinstance(ports, list) or len(ports) != 1
                    or ports[0].get("HostIp") != "127.0.0.1"
                    or not str(ports[0].get("HostPort", "")).isdigit()):
                raise RuntimeFailure("Container identity or loopback port binding could not be verified.")
            port = int(ports[0]["HostPort"])
            if not 1 <= port <= 65535:
                raise RuntimeFailure("Docker returned an invalid loopback port.")
            base_url = f"http://127.0.0.1:{port}"
        yield container_id, environment, base_url
    except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
        raise RuntimeFailure("Docker returned malformed environment information.") from None
    finally:
        cleanup_failed = False
        if container_attempted:
            try:
                code, _ = _docker(["rm", "--force", "--volumes", name], time.monotonic() + 5)
                cleanup_failed = code != 0
            except RuntimeFailure:
                cleanup_failed = True
        if network_attempted:
            try:
                code, _ = _docker(["network", "rm", network], time.monotonic() + 5)
                cleanup_failed = cleanup_failed or code != 0
            except RuntimeFailure:
                cleanup_failed = True
        if cleanup_failed:
            raise RuntimeFailure("Container or network cleanup could not be confirmed.")


def _redact(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_redact(k, secrets): _redact(v, secrets) for k, v in value.items()}
    return value


def _http(base_url: str, step: dict, deadline: float, headers: dict | None = None,
          secrets: tuple[str, ...] = ()) -> dict:
    parsed = urlsplit(base_url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=_remaining(deadline, 10))
    expired = threading.Event()

    def interrupt():
        expired.set()
        if connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    timer = threading.Timer(_remaining(deadline, 10), interrupt)
    timer.daemon = True
    timer.start()
    try:
        request_headers = {"Accept": "application/json", **(headers or {})}
        body = None
        if "json" in step:
            body = json.dumps(step["json"], allow_nan=False).encode()
            request_headers["Content-Type"] = "application/json"
        connection.request(step["method"], (parsed.path.rstrip("/") + step["path"]), body=body, headers=request_headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise RuntimeFailure("The target returned a redirect; no redirect was followed.")
        declared = response.getheader("Content-Length")
        if declared and (not declared.isdigit() or int(declared) > MAX_BODY):
            raise RuntimeFailure("The HTTP response exceeds the allowed size or has invalid length.")
        raw = bytearray()
        while True:
            _remaining(deadline)
            chunk = response.read1(min(8192, MAX_BODY + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise RuntimeFailure("The HTTP response exceeds the allowed size.")
        if expired.is_set():
            raise DeadlineExceeded("The HTTP request exceeded its time limit.")
        if declared and step["method"] != "HEAD" and len(raw) != int(declared):
            raise RuntimeFailure("The HTTP response body was incomplete.")
        text = bytes(raw).decode("utf-8", errors="replace")
        text = _redact(text, secrets)
        observation = {"status": response.status}
        try:
            observation["json"] = _redact(json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())), secrets)
        except (ValueError, RecursionError):
            observation["body"] = text
        return observation
    except (socket.timeout, TimeoutError):
        raise DeadlineExceeded("The HTTP request exceeded its time limit.") from None
    except (OSError, http.client.HTTPException, ValueError):
        if expired.is_set() or time.monotonic() >= deadline:
            raise DeadlineExceeded("The HTTP request exceeded its time limit.") from None
        raise HTTPUnavailable("An HTTP response could not be collected from the target.") from None
    finally:
        timer.cancel()
        connection.close()


def _failure(result: dict, exc: RuntimeFailure) -> dict:
    result["execution_status"] = "timed_out" if isinstance(exc, DeadlineExceeded) else "setup_failed"
    result["error"] = str(exc)
    result["complete"] = False
    return result


def run_probe(snapshot: Path, image: str, runtime: dict, steps: list[dict], deadline: float) -> dict:
    result = {"execution_status": "setup_failed", "observations": [], "environment": {}, "complete": False}
    try:
        steps = _steps(steps)
        with _container(snapshot, image, runtime, deadline) as (_, environment, base_url):
            result["environment"] = environment
            startup_deadline = min(deadline, time.monotonic() + runtime.get("startup_seconds", 30))
            while True:
                _remaining(startup_deadline)
                try:
                    health = _http(base_url, {"method": "GET", "path": runtime["health_path"]}, startup_deadline)
                    if 200 <= health["status"] < 300:
                        break
                except DeadlineExceeded:
                    raise
                except HTTPUnavailable:
                    pass
                time.sleep(min(.1, _remaining(startup_deadline)))
            for step in steps:
                result["observations"].append(_http(base_url, step, deadline))
            result.update(execution_status="completed", complete=True)
    except RuntimeFailure as exc:
        _failure(result, exc)
    return result


def run_tests(snapshot: Path, image: str, runtime: dict, deadline: float) -> dict:
    result = {"execution_status": "setup_failed", "test_status": "unavailable", "exit_code": None,
              "observations": [], "environment": {}, "output_tail": "", "complete": False}
    try:
        with _container(snapshot, image, runtime, deadline, tests=True) as (container, environment, _):
            result["environment"] = environment
            output = _checked(["wait", container], deadline)
            if not output.isdigit() or not 0 <= int(output) <= 255:
                raise RuntimeFailure("Docker did not report a valid test command exit status.")
            code = int(output)
            result["output_tail"] = _checked(["logs", container], deadline)
            result.update(execution_status="completed", test_status="passed" if code == 0 else "failed",
                          exit_code=code, complete=True)
    except RuntimeFailure as exc:
        _failure(result, exc)
    return result


def run_staging(staging: dict, steps: list, candidate_revision: str, deadline: float) -> dict:
    result = {"execution_status": "setup_failed", "observations": [], "environment": {}, "complete": False}
    try:
        steps = _steps(steps)
        if not isinstance(staging, dict) or not re.fullmatch(r"[a-f0-9]{40}", candidate_revision or ""):
            raise RuntimeFailure("Staging requires approved configuration and an exact candidate revision.")
        base_url = staging.get("base_url", "")
        if not isinstance(base_url, str) or any(ord(c) < 32 for c in base_url):
            raise RuntimeFailure("The configured staging URL is invalid.")
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or "\\" in base_url):
            raise RuntimeFailure("The configured staging URL must be an HTTP(S) origin without credentials.")
        # Validate the port before any request and never accept an agent-supplied URL.
        parsed.port
        revision_path = _path(staging.get("revision_path"))
        revision_key = staging.get("revision_key", "revision")
        if not isinstance(revision_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", revision_key):
            raise RuntimeFailure("The staging revision key is invalid.")
        methods = staging.get("allowed_methods", ["GET", "HEAD"])
        if (not isinstance(methods, list) or not methods or any(not isinstance(m, str) or m not in _METHODS for m in methods)
                or any(step["method"] not in methods for step in steps)):
            raise RuntimeFailure("The staging probe includes a method outside the approved scope.")
        if any(step["method"] not in {"GET", "HEAD", "OPTIONS"} for step in steps) and staging.get("allow_synthetic_writes") is not True:
            raise RuntimeFailure("Staging writes require explicit synthetic-write authorization.")
        env_headers = staging.get("headers_env", {})
        if not isinstance(env_headers, dict) or len(env_headers) > 16:
            raise RuntimeFailure("Configured staging authentication is invalid.")
        headers = {}
        for header, env_name in env_headers.items():
            if (not isinstance(header, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header)
                    or header.lower() in {"host", "content-length", "transfer-encoding", "connection"}
                    or not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_name)):
                raise RuntimeFailure("Configured staging authentication is invalid.")
            value = os.environ.get(env_name, "")
            if not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise RuntimeFailure("A configured staging credential is unavailable or invalid.")
            headers[header] = value
        secrets = tuple(headers.values()) + tuple(value.split(" ", 1)[1] for key, value in headers.items()
                                                  if key.lower() == "authorization" and " " in value)
        identity = _http(base_url, {"method": "GET", "path": revision_path}, deadline, headers, secrets)
        observed_revision = identity.get("json", {}).get(revision_key) if isinstance(identity.get("json"), dict) else None
        if identity["status"] != 200 or observed_revision != candidate_revision:
            raise RuntimeFailure("Staging revision does not match the exact candidate revision; replay was not run.")
        result["environment"] = {"execution_backend": "staging_http", "base_url": base_url,
                                 "candidate_revision": candidate_revision, "observed_revision": observed_revision,
                                 "revision_checked": True, "evidence_collector": "host_http"}
        for step in steps:
            result["observations"].append(_http(base_url, step, deadline, headers, secrets))
        result.update(execution_status="completed", complete=True)
    except (ValueError, TypeError):
        _failure(result, RuntimeFailure("The staging configuration is invalid."))
    except RuntimeFailure as exc:
        _failure(result, exc)
    return result
