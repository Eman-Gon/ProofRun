"""Authenticated release/event API; kept separate from the fixture worker on 8766."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .release_contracts import canonical, load_targets, validate_request
from .release_investigation import Investigation
from .release_service import Conflict, ReleaseService
from .research import ResearchError


def create_server(service, token, host="127.0.0.1", port=8767, *, failure_research=None, run_graph=None):
    if not isinstance(token, str) or len(token) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Use a server-side worker token of at least 32 printable characters.")
    class Handler(BaseHTTPRequestHandler):
        server_version = "ProofRunRelease/1"

        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def send_json(self, code, value):
            self.send_bytes(code, canonical(value), "application/json")

        def send_bytes(self, code, data, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def authorize(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                self.send_json(401, {"error": "Authentication required."})
                return None
            scope = self.headers.get("X-ProofRun-Scope", "operator")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", scope):
                self.send_json(400, {"error": "Invalid workspace scope."})
                return None
            return scope

        def do_GET(self):
            scope = self.authorize()
            if scope is None:
                return
            path = urlsplit(self.path).path
            try:
                if path == "/v1/release-targets":
                    return self.send_json(200, service.list_targets(scope))
                if match := re.fullmatch(r"/v1/release-runs/(release-[a-f0-9]{40})", path):
                    return self.send_json(200, service.get(match[1], scope))
                if match := re.fullmatch(r"/v1/release-runs/(release-[a-f0-9]{40})/graph", path):
                    from .evidence_graph import RunGraphService
                    run = service.get(match[1], scope)
                    return self.send_json(200, (run_graph or RunGraphService()).snapshot("release", run, scope=scope))
                if match := re.fullmatch(r"/v1/release-runs/(release-[a-f0-9]{40})/artifacts/([a-z0-9.-]+)", path):
                    return self.send_bytes(200, service.artifact(match[1], match[2], scope), "application/octet-stream")
                self.send_json(404, {"error": "Unknown release endpoint."})
            except (KeyError, FileNotFoundError):
                self.send_json(404, {"error": "Unknown release run or artifact."})
            except Conflict as exc:
                self.send_json(409, {"error": str(exc)})

            except Exception:
                self.send_json(503, {"error": "Saved evidence is temporarily unavailable."})

        def do_POST(self):
            scope = self.authorize()
            if scope is None:
                return
            path = urlsplit(self.path).path
            research_match = re.fullmatch(r"/v1/release-runs/(release-[a-f0-9]{40})/failure-research", path)
            if path not in ("/v1/release-runs", "/v1/release-events") and not research_match:
                return self.send_json(404, {"error": "Unknown release endpoint."})
            try:
                if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                    raise ValueError("Send a bounded JSON request with Content-Length.")
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 16000:
                    raise ValueError("Release request must be 1–16000 bytes.")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete request body.")
                from .api import _object, _reject_constant
                request = json.loads(raw, object_pairs_hook=_object, parse_constant=_reject_constant)
                if research_match:
                    from .failure_context import release_context, digest
                    from .failure_research import validate_query
                    if not isinstance(request, dict) or set(request) != {"request_id", "query", "finding_id"}:
                        raise ValueError("Invalid research fields")
                    if not isinstance(request["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", request["request_id"]):
                        raise ValueError("Invalid request id")
                    context = release_context(service.get(research_match[1], scope), request["finding_id"], scope)
                    query = validate_query(request["query"])
                    if failure_research is None:
                        return self.send_json(503, {"error": "Failure research is unavailable on this worker."})
                    request_id = "release-research-" + digest([scope, research_match[1], request["request_id"]])
                    return self.send_json(200, failure_research.submit(request_id, query, context))
                if urlsplit(self.path).path == "/v1/release-events" and (not isinstance(request, dict) or not request.get("event_id")):
                    raise ValueError("Deployment events require an idempotent event_id.")
                self.send_json(202, service.submit(request, scope))
            except Conflict as exc:
                self.send_json(409, {"error": str(exc)})
            except (KeyError, FileNotFoundError):
                self.send_json(404, {"error": "Unknown release run or finding."})
            except ResearchError as exc:
                self.send_json(exc.status, {"error": exc.message})
            except (ValueError, TypeError, UnicodeError):
                self.send_json(400, {"error": "Invalid release request; check registered target, exact commits, event id and budget."})
    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=os.environ.get("PROOFRUN_RELEASE_TARGETS"))
    parser.add_argument("--artifacts", type=Path, default=Path(os.environ.get("PROOFRUN_RELEASE_ARTIFACT_DIR", ".commit-watch/releases")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--once", type=Path, help="Run a release request JSON file directly as the local operator.")
    args = parser.parse_args()
    if not args.targets:
        parser.error("Configure --targets or PROOFRUN_RELEASE_TARGETS.")
    targets = load_targets(args.targets)
    if args.once:
        request = validate_request(json.loads(args.once.read_text()), targets)
        # A unique output directory is required; never overwrite previously recorded acceptance evidence.
        if args.artifacts.exists():
            parser.error("For --once choose a new --artifacts directory.")
        result = Investigation(targets[request["target_id"]], request, args.artifacts).run()
        print(json.dumps({"recommendation": result["recommendation"], "summary": result["summary"],
                          "evidence": str(args.artifacts.resolve()), "agent_status": result["agent"]["status"]}, indent=2))
        return 0 if result["recommendation"] == "update" else 2
    from .failure_research import FailureResearchService
    failure_research = FailureResearchService(args.artifacts / "failure-research")
    service = ReleaseService(targets, args.artifacts, failure_research=failure_research)
    from .evidence_graph import configured_run_graph
    run_graph = configured_run_graph()
    try:
        server = create_server(service, os.environ.get("PROOFRUN_WORKER_TOKEN", ""), args.host, args.port,
                               failure_research=failure_research, run_graph=run_graph)
        print(f"Release investigation worker listening on {args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        service.close()
        run_graph.close()
        failure_research.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
