"""Docker is mocked at its process boundary; HTTP observations use a real local server."""
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import time

import pytest

from src.proofrun import release_runtime as runtime


IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64
REVISION = "c" * 40


@pytest.fixture
def http_target():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.respond()

        def do_HEAD(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.command, self.path, body))
            path = self.path.removeprefix("/api")
            status, payload, content_type = 200, {"ok": True}, "application/json"
            if path == "/revision":
                payload = {"revision": REVISION}
            elif path == "/wrong-revision":
                payload = {"revision": "d" * 40}
            elif path == "/echo":
                payload = json.loads(body) if body else {"value": "actual target response"}
            elif path == "/secret":
                payload = {"authorization": self.headers.get("Authorization")}
            elif path == "/failure":
                status, payload = 422, {"error": "rejected"}
            elif path == "/redirect":
                status, payload = 302, {"redirect": True}
            elif path == "/oversized":
                payload = {"text": "x" * runtime.MAX_BODY}
            elif path == "/text":
                payload, content_type = "ordinary text", "text/plain"
            elif path == "/slow":
                time.sleep(.25)
            encoded = json.dumps(payload).encode() if content_type == "application/json" else payload.encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded) + (10 if path == "/incomplete" else 0)))
                if status == 302:
                    self.send_header("Location", "/should-not-be-visited")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", server.server_port, requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def docker(monkeypatch, http_target):
    state = {"calls": [], "exit_code": 0, "image_env": [], "volumes": None,
             "logs": "3 tests passed\n", "wait_timeout": False, "fail_network": False}

    class Process:
        def __init__(self, argv, **kwargs):
            assert argv[0] == "docker", "No application process may execute on the host"
            assert kwargs == {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
            args = argv[1:]
            state["calls"].append(args)
            self.args = args
            self.killed = False
            self.returncode = 0
            if args[:2] == ["image", "inspect"]:
                output = json.dumps({"Id": IMAGE, "Config": {"Env": state["image_env"], "Volumes": state["volumes"]}})
            elif args[:2] == ["network", "create"]:
                output = "network-id"
                self.returncode = 1 if state["fail_network"] else 0
            elif args[0] == "run":
                output = CONTAINER
            elif args[0] == "inspect":
                output = json.dumps({"Image": IMAGE, "NetworkSettings": {"Ports": {
                    "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(http_target[1])}],
                }}})
            elif args[0] == "wait":
                output = str(state["exit_code"])
            elif args[0] == "logs":
                output = state["logs"]
            else:
                output = ""
            self.stdout = io.BytesIO(output.encode())

        def wait(self, timeout):
            assert timeout > 0
            if self.args[0] == "wait" and state["wait_timeout"] and not self.killed:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return self.returncode

        def kill(self):
            self.killed = True

    monkeypatch.setattr(runtime.subprocess, "Popen", Process)
    return state


@pytest.fixture
def config():
    return {"command": ["python", "app.py"], "port": 8000, "health_path": "/health",
            "startup_seconds": 1, "test_command": ["python", "-m", "unittest"],
            "env": {"APP_DATA": "/data"}}


def test_probe_collects_real_response_and_rejects_no_behavior_by_assumption(tmp_path, docker, config, http_target):
    result = runtime.run_probe(tmp_path, IMAGE, config, [
        {"method": "POST", "path": "/echo", "json": {"unanticipated": [1, 2]}},
        {"method": "GET", "path": "/failure"},
        {"method": "GET", "path": "/text"},
    ], time.monotonic() + 5)
    assert result["execution_status"] == "completed" and result["complete"] is True
    assert result["observations"] == [
        {"status": 200, "json": {"unanticipated": [1, 2]}},
        {"status": 422, "json": {"error": "rejected"}},
        {"status": 200, "body": "ordinary text"},
    ]
    assert result["environment"]["image_id"] == IMAGE
    assert result["environment"]["evidence_collector"] == "host_http"
    run = next(c for c in docker["calls"] if c[0] == "run")
    assert run[run.index("--publish") + 1] == "127.0.0.1::8000"
    assert "--read-only" in run and "ALL" in run and "65534:65534" in run
    assert "--internal" in next(c for c in docker["calls"] if c[:2] == ["network", "create"])
    assert any("dst=/workspace,readonly" in a for a in run)
    assert docker["calls"][-2][:3] == ["rm", "--force", "--volumes"]
    assert docker["calls"][-1][:2] == ["network", "rm"]


def test_each_probe_uses_a_fresh_container_and_network(tmp_path, docker, config):
    for _ in range(2):
        runtime.run_probe(tmp_path, IMAGE, config, [{"method": "GET", "path": "/echo"}], time.monotonic() + 5)
    runs = [c for c in docker["calls"] if c[0] == "run"]
    assert runs[0][runs[0].index("--name") + 1] != runs[1][runs[1].index("--name") + 1]


@pytest.mark.parametrize("path", ["/redirect", "/oversized", "/incomplete"])
def test_invalid_http_results_are_not_successful_observations(tmp_path, docker, config, http_target, path):
    result = runtime.run_probe(tmp_path, IMAGE, config, [{"method": "GET", "path": path}], time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed" and result["complete"] is False
    assert result["observations"] == []
    assert all(path != "/should-not-be-visited" for _, path, _ in http_target[2])
    assert docker["calls"][-1][:2] == ["network", "rm"]


def test_deadline_keeps_partial_evidence_distinct_from_complete_run(tmp_path, docker, config):
    result = runtime.run_probe(tmp_path, IMAGE, config, [
        {"method": "GET", "path": "/echo"}, {"method": "GET", "path": "/slow"},
    ], time.monotonic() + .1)
    assert result["execution_status"] == "timed_out" and result["complete"] is False
    assert len(result["observations"]) == 1
    assert docker["calls"][-1][:2] == ["network", "rm"]


@pytest.mark.parametrize("step", [
    {"method": "GET", "path": "http://different.invalid/"},
    {"method": "GET", "path": "//different.invalid/"},
    {"method": "GET", "path": "/", "headers": {"Authorization": "secret"}},
    {"method": "GET", "path": "/\r\nHost: other"},
    {"method": [], "path": "/"},
    {"method": "GET", "path": "/", "json": {}},
])
def test_agent_cannot_change_target_or_headers(tmp_path, docker, config, step):
    result = runtime.run_probe(tmp_path, IMAGE, config, [step], time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed" and result["observations"] == []
    assert docker["calls"] == []


@pytest.mark.parametrize("change", [{"workdir": "../escape"}, {"command": "python app.py"},
                                     {"env": {"API_TOKEN": "private"}}, {"port": 80}])
def test_runtime_scope_is_validated_before_start(tmp_path, docker, config, change):
    result = runtime.run_probe(tmp_path, IMAGE, config | change, [{"method": "GET", "path": "/"}], time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed"
    assert docker["calls"] == []


def test_mutable_image_rejected(tmp_path, docker, config):
    result = runtime.run_probe(tmp_path, "example:latest", config, [{"method": "GET", "path": "/"}], time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed" and not docker["calls"]


@pytest.mark.parametrize("image_state", [{"image_env": ["API_TOKEN=secret"]}, {"volumes": {"/persistent": {}}}])
def test_image_cannot_inherit_secret_env_or_persistent_data(tmp_path, docker, config, image_state):
    docker.update(image_state)
    result = runtime.run_probe(tmp_path, IMAGE, config, [{"method": "GET", "path": "/"}], time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed"
    assert not any(c[0] == "run" for c in docker["calls"])


def test_network_creation_failure_is_cleaned_up(tmp_path, docker, config):
    docker["fail_network"] = True
    result = runtime.run_probe(tmp_path, IMAGE, config, [{"method": "GET", "path": "/"}], time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed"
    assert docker["calls"][-1][:2] == ["network", "rm"]


@pytest.mark.parametrize("exit_code, status", [(0, "passed"), (1, "failed"), (2, "failed")])
def test_tests_execute_configured_command_in_fresh_offline_container(tmp_path, docker, config, exit_code, status):
    docker["exit_code"] = exit_code
    docker["logs"] = "prefix" + "x" * (runtime.MAX_OUTPUT * 2) + "end"
    result = runtime.run_tests(tmp_path, IMAGE, config, time.monotonic() + 5)
    assert result["execution_status"] == "completed" and result["test_status"] == status
    assert result["exit_code"] == exit_code and result["complete"] is True
    assert len(result["output_tail"]) <= runtime.MAX_OUTPUT and result["output_tail"].endswith("end")
    run = next(c for c in docker["calls"] if c[0] == "run")
    assert run[run.index("--network") + 1] == "none" and "--publish" not in run
    assert run[-4:] == ["python", IMAGE, "-m", "unittest"]


def test_test_timeout_cannot_be_a_pass_and_always_removes_container(tmp_path, docker, config):
    docker["wait_timeout"] = True
    result = runtime.run_tests(tmp_path, IMAGE, config, time.monotonic() + 5)
    assert result["execution_status"] == "timed_out" and result["test_status"] == "unavailable"
    assert result["exit_code"] is None and result["complete"] is False
    assert docker["calls"][-1][:3] == ["rm", "--force", "--volumes"]


def test_staging_requires_actual_matching_identity_before_replay(http_target):
    result = runtime.run_staging({"base_url": http_target[0], "revision_path": "/wrong-revision"},
                                 [{"method": "GET", "path": "/echo"}], REVISION, time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed" and result["observations"] == []
    assert [r[1] for r in http_target[2]] == ["/wrong-revision"]


def test_staging_collects_real_responses_with_configured_base_path(http_target):
    result = runtime.run_staging({"base_url": http_target[0] + "/api", "revision_path": "/revision"},
                                 [{"method": "GET", "path": "/failure"}], REVISION, time.monotonic() + 5)
    assert result["execution_status"] == "completed" and result["complete"] is True
    assert result["environment"]["observed_revision"] == REVISION
    assert result["observations"] == [{"status": 422, "json": {"error": "rejected"}}]
    assert [r[1] for r in http_target[2]] == ["/api/revision", "/api/failure"]


@pytest.mark.parametrize("extra", [{}, {"allowed_methods": ["GET", "POST"]}])
def test_staging_writes_require_both_allowlist_and_explicit_authorization(http_target, extra):
    result = runtime.run_staging({"base_url": http_target[0], "revision_path": "/revision", **extra},
                                 [{"method": "POST", "path": "/echo", "json": {}}], REVISION, time.monotonic() + 5)
    assert result["execution_status"] == "setup_failed" and http_target[2] == []


def test_explicit_synthetic_write_is_replayed_only_after_revision_check(http_target):
    result = runtime.run_staging({"base_url": http_target[0], "revision_path": "/revision",
                                 "allowed_methods": ["POST"], "allow_synthetic_writes": True},
                                [{"method": "POST", "path": "/echo", "json": {"synthetic": True}}],
                                REVISION, time.monotonic() + 5)
    assert result["execution_status"] == "completed"
    assert [r[0] for r in http_target[2]] == ["GET", "POST"]


def test_staging_credentials_stay_out_of_results_even_when_echoed(http_target, monkeypatch):
    monkeypatch.setenv("RELEASE_TEST_AUTH", "Bearer private-test-credential")
    result = runtime.run_staging({"base_url": http_target[0], "revision_path": "/revision",
                                 "headers_env": {"Authorization": "RELEASE_TEST_AUTH"}},
                                [{"method": "GET", "path": "/secret"}], REVISION, time.monotonic() + 5)
    assert result["execution_status"] == "completed"
    assert "private-test-credential" not in json.dumps(result)
    assert result["observations"][0]["json"] == {"authorization": "[redacted]"}


def test_staging_redirect_and_timeout_cannot_produce_success(http_target):
    for path, expected in [("/redirect", "setup_failed"), ("/slow", "timed_out")]:
        result = runtime.run_staging({"base_url": http_target[0], "revision_path": "/revision"},
                                    [{"method": "GET", "path": path}], REVISION, time.monotonic() + .1)
        assert result["execution_status"] == expected and result["observations"] == []


def test_expired_deadline_does_not_start_docker(tmp_path, docker, config):
    result = runtime.run_probe(tmp_path, IMAGE, config, [{"method": "GET", "path": "/"}], time.monotonic() - 1)
    assert result["execution_status"] == "timed_out" and docker["calls"] == []
