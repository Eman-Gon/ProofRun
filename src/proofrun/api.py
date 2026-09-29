"""Small authenticated HTTP boundary for the single-worker ProofRun service.

Run with ``python3.12 -m src.proofrun.api``. Tokens belong in server-side
configuration; this API deliberately does not enable browser CORS access.
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
import socket
import sys
from typing import TYPE_CHECKING

from .contracts import SCHEMA_VERSION

if TYPE_CHECKING:
    from .service import RunService
    from .research import ResearchService
    from .failure_research import FailureResearchService
    from .deployment_graph import DeploymentGraphService
    from .evidence_graph import RunGraphService

MAX_BODY_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 10
_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_RUN_PATH = re.compile(rf"/v1/runs/({_IDENTIFIER})\Z")
_ARTIFACT_PATH = re.compile(rf"/v1/runs/({_IDENTIFIER})/artifacts/({_IDENTIFIER})\Z")
_CASE_PATH = re.compile(rf"/v1/cases/({_IDENTIFIER})\Z")


class _HTTPError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON number")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # No request bodies, credentials, exception text, or stack traces in logs.
        pass


def create_server(
    service: RunService, token: str, host: str = "127.0.0.1", port: int = 8766,
    *, research: ResearchService | None = None, failure_research: FailureResearchService | None = None,
    graph: DeploymentGraphService | None = None,
    run_graph: RunGraphService | None = None,
) -> ThreadingHTTPServer:
    """Construct a server; the caller owns serve/shutdown and service.close()."""
    if (not isinstance(token, str) or not 1 <= len(token) <= 512
            or not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise ValueError("A nonempty server-side bearer token is required.")
    expected_token = token.encode("ascii")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ProofRun"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def setup(self):
            self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
            super().setup()

        def log_message(self, format, *args):
            pass

        def _respond(self, status: int, body: bytes, content_type: str):
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Bearer realm="proofrun"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: dict):
            encoded = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
            self._respond(status, encoded, "application/json; charset=utf-8")

        def _error(self, status: int, code: str, message: str):
            self._json(status, {"schema_version": SCHEMA_VERSION, "error": {"code": code, "message": message}})

        def send_error(self, code, message=None, explain=None):
            # BaseHTTPRequestHandler can otherwise echo an invalid request line.
            if code == 501:
                code = 405
            phrase = HTTPStatus(code).phrase if code in HTTPStatus._value2member_map_ else "Invalid request"
            self._error(code, "method_not_allowed" if code == 405 else "invalid_request", phrase)

        def _authenticate(self):
            headers = self.headers.get_all("Authorization", [])
            supplied = headers[0] if len(headers) == 1 else ""
            scheme, separator, credentials = supplied.partition(" ")
            authenticated = hmac.compare_digest(credentials.encode("utf-8"), expected_token)
            if not separator or scheme.lower() != "bearer" or not authenticated:
                raise _HTTPError(401, "unauthorized", "A valid worker bearer token is required.")

        def _body(self) -> dict:
            if self.headers.get_all("Transfer-Encoding"):
                raise _HTTPError(400, "invalid_request", "Transfer-Encoding is not supported.")
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                raise _HTTPError(411, "length_required", "Content-Length is required.")
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise _HTTPError(400, "invalid_request", "Content-Length must be one nonnegative integer.")
            size = int(lengths[0])
            if size > MAX_BODY_BYTES:
                raise _HTTPError(413, "request_too_large", "Run requests must be at most 64 KiB.")
            content_types = self.headers.get_all("Content-Type", [])
            if (len(content_types) != 1
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                raise _HTTPError(415, "unsupported_media_type", "Use Content-Type: application/json.")
            try:
                raw = self.rfile.read(size)
            except (TimeoutError, socket.timeout):
                raise _HTTPError(408, "request_timeout", "The request body was not received in time.") from None
            if len(raw) != size:
                raise _HTTPError(400, "invalid_json", "Send one complete JSON object.")
            try:
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise _HTTPError(400, "invalid_json", "Send one valid JSON object without duplicate keys.") from None
            if not isinstance(value, dict):
                raise _HTTPError(400, "invalid_json", "The request must be a JSON object.")
            return value

        def _dispatch(self):
            if self.command == "GET" and self.path == "/health":
                self._json(200, {"schema_version": SCHEMA_VERSION, "status": "ok"})
                return
            self._authenticate()
            # Match raw paths. Encoded slashes, traversal, queries, and URLs are
            # not decoded into resource IDs or local artifact paths.
            if self.command == "POST" and self.path == "/v1/runs":
                run, created = service.submit(self._body())
                self._json(202 if created else 200, run)
            elif self.command == "POST" and self.path in {"/v1/deployments", "/v1/contract-changes"}:
                payload = self._body()
                if graph is None:
                    raise _HTTPError(503, "graph_unavailable", "Neo4j is not configured on this worker.")
                operation = graph.register if self.path == "/v1/deployments" else graph.select
                self._json(200, operation(payload))
            elif self.command == "GET" and (match := re.fullmatch(rf"/v1/contract-changes/({_IDENTIFIER})", self.path)):
                if graph is None:
                    raise _HTTPError(503, "graph_unavailable", "Neo4j is not configured on this worker.")
                self._json(200, graph.get(match[1]))
            elif self.command == "POST" and (match := re.fullmatch(rf"/v1/contract-changes/({_IDENTIFIER})/runs", self.path)):
                payload = self._body()
                if graph is None:
                    raise _HTTPError(503, "graph_unavailable", "Neo4j is not configured on this worker.")
                run, created = graph.submit(match[1], payload)
                self._json(202 if created else 200, run)
            elif self.command == "POST" and (match := re.fullmatch(rf"/v1/runs/({_IDENTIFIER})/failure-research", self.path)):
                from .failure_context import fixture_context
                payload = self._body()
                if set(payload) != {"request_id", "query"}:
                    raise _HTTPError(400, "invalid_research", "Supply request_id and the reviewed search query.")
                if failure_research is None:
                    raise _HTTPError(503, "research_unavailable", "Failure research is not configured on this worker.")
                context = fixture_context(service.get_run(match[1]), self.headers.get("X-ProofRun-Scope", "operator"))
                self._json(200, failure_research.submit(payload["request_id"], payload["query"], context))
            elif self.command == "GET" and (match := re.fullmatch(r"/v1/failure-research/(failure-research-[a-f0-9]{64})", self.path)):
                if failure_research is None:
                    raise _HTTPError(503, "research_unavailable", "Failure research is not configured on this worker.")
                self._json(200, failure_research.get(match[1]))
            elif self.command == "POST" and self.path == "/v1/customer-research":
                payload = self._body()
                if research is None:
                    raise _HTTPError(503, "research_unavailable", "Customer research is not configured on this worker.")
                # Research has its own record and explicit request identity. It
                # cannot modify a run, proposal, test contract, or verdict.
                self._json(200, research.submit(payload))
            elif self.command == "GET" and (match := _CASE_PATH.fullmatch(self.path)):
                self._json(200, service.get_case(match.group(1)))
            elif self.command == "GET" and (match := _RUN_PATH.fullmatch(self.path)):
                self._json(200, service.get_run(match.group(1)))
            elif self.command == "GET" and (match := re.fullmatch(rf"/v1/runs/({_IDENTIFIER})/graph", self.path)):
                from .evidence_graph import RunGraphService
                # The client supplies an identity only. All graph data comes
                # from the worker's authoritative record, never a browser body.
                run = service.get_run(match[1])
                self._json(200, (run_graph or RunGraphService()).snapshot("fixture", run))
            elif self.command == "GET" and (match := _ARTIFACT_PATH.fullmatch(self.path)):
                content, content_type = service.artifact(*match.groups())
                if not isinstance(content, bytes) or not isinstance(content_type, str) or any(c in content_type for c in "\r\n"):
                    raise RuntimeError("Invalid artifact response")
                self._respond(200, content, content_type)
            else:
                raise _HTTPError(404, "not_found", "The requested endpoint does not exist.")

        def _handle(self):
            try:
                self._dispatch()
            except _HTTPError as exc:
                self._error(exc.status, exc.code, exc.message)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True
            except Exception as exc:
                # Import lazily so transport-only tooling does not initialize a
                # runner, configuration, or provider client.
                from .service import ServiceError
                from .research import ResearchError
                if isinstance(exc, (ServiceError, ResearchError)):
                    self._error(exc.status, exc.code, exc.message)
                else:
                    self._error(500, "internal_error", "The worker could not complete this request.")

        do_GET = _handle
        do_POST = _handle

    return _Server((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the authenticated ProofRun worker API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--runner", choices=("native", "prepared"), default="native")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be from 0 to 65535.")
    from .config import ConfigurationError, WorkerConfig
    from .service import RunService
    try:
        config = WorkerConfig.from_env()
    except ConfigurationError as exc:
        parser.error(str(exc))
    service = server = research = failure_research = graph = run_graph = None
    try:
        from .research import ResearchService
        research = ResearchService(config.artifact_dir / "customer-research")
        from .failure_research import FailureResearchService
        failure_research = FailureResearchService(config.artifact_dir / "failure-research")
        service = RunService(args.root.resolve(), config.artifact_dir, execution_target=config.execution_target,
                             worker_id=config.worker_id, runner_mode=args.runner, failure_research=failure_research)
        from .deployment_graph import configured_graph
        graph = configured_graph(service, config.artifact_dir / "deployment-graph")
        from .evidence_graph import configured_run_graph
        run_graph = configured_run_graph()
        server = create_server(service, config.token, args.host, args.port, research=research,
                               failure_research=failure_research, graph=graph, run_graph=run_graph)
        print(f"ProofRun worker listening on {args.host}:{server.server_port} ({args.runner} runner).", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("ProofRun could not start the worker API; check host, port, and server configuration.", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.close()
        if graph is not None:
            graph.close()
        if run_graph is not None:
            run_graph.close()
        if research is not None:
            research.close()
        if failure_research is not None:
            failure_research.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
