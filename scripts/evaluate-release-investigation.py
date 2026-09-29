#!/usr/bin/env python3
"""Live, synthetic evaluation of release discovery; no defect input is given to the agent.

Creates a separate temporary Git product with an unannounced behavioral regression.
Requires a preloaded immutable Python image, Docker, and explicit OpenRouter config.
This evaluates the generic release path; it is not a customer/staging demonstration.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import hashlib
import secrets
import threading
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.proofrun.release_contracts import canonical, validate_request, validate_target
from src.proofrun.release_investigation import Investigation

APP = '''import json
from http.server import BaseHTTPRequestHandler, HTTPServer

def import_customer(payload):
    customer_id = payload.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id:
        return 422, {"error": "customer_id must be a nonempty string"}
    return 201, {"customer_id": customer_id, "imported": True}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self.respond(200, {"healthy": True}) if self.path == "/health" else self.respond(404, {})
    def do_POST(self):
        if self.path != "/customers": return self.respond(404, {})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if not isinstance(data, dict): return self.respond(422, {"error": "object required"})
            status, body = import_customer(data)
            self.respond(status, body)
        except (ValueError, TypeError): self.respond(422, {"error": "invalid input"})

if __name__ == "__main__": HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'''
TESTS = '''import unittest
from app import import_customer

class OriginalSuite(unittest.TestCase):
    def test_basic_customer(self):
        self.assertEqual(import_customer({"customer_id": "123"}),
                         (201, {"customer_id": "123", "imported": True}))
    def test_reject_object_id(self):
        self.assertEqual(import_customer({"customer_id": {}})[0], 422)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory outside the source checkout.")
    parser.add_argument("--image", required=True, help="Preloaded immutable Python image ID or digest.")
    parser.add_argument("--budget", type=int, default=600)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--api", action="store_true", help="Exercise authenticated event submission, polling, deduplication, and artifact retrieval.")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "product"
    repo.mkdir()
    def git(*cmd):
        return subprocess.check_output(["git", "-C", str(repo), *cmd], stderr=subprocess.DEVNULL).decode().strip()
    git("init")
    git("config", "user.name", "ProofRun synthetic evaluation")
    git("config", "user.email", "evaluation@example.invalid")
    (repo / "app.py").write_text(APP)
    (repo / "test_app.py").write_text(TESTS)
    (repo / "README.md").write_text("Synthetic customer import API. POST /customers imports records with a customer_id.\nRun python app.py. Tests: python -m unittest -v.\n")
    git("add", ".")
    git("commit", "-m", "Initial import application")
    baseline = git("rev-parse", "HEAD")
    candidate_app = APP.replace('return 201, {"customer_id": customer_id, "imported": True}',
                                'return 201, {"customer_id": str(int(customer_id)), "imported": True}')
    (repo / "app.py").write_text(candidate_app)
    git("add", ".")
    git("commit", "-m", "Normalize imported customer identifiers")
    candidate = git("rev-parse", "HEAD")
    target = {"id": "synthetic-import-product", "name": "Synthetic release investigation evaluation",
              "repository": str(repo), "images": {baseline: args.image, candidate: args.image},
              "runtime": {"command": ["python", "app.py"], "port": 8080, "health_path": "/health",
                          "collector_image": args.image,
                          "test_command": ["python", "-m", "unittest", "discover", "-v"], "startup_seconds": 15},
              "requirements": [{"id": "import-compatibility", "kind": "preserve_response",
                                "path_prefix": "/customers", "methods": ["POST"],
                                "description": "Existing customer import clients must retain the same response statuses and bodies."}],
              "test_paths": ["test_*.py"], "test_success_pattern": "Ran [1-9][0-9]* tests?",
              "repair_paths": ["app.py"], "exclude_paths": []}
    request = {"target_id": target["id"], "baseline_revision": baseline, "candidate_revision": candidate,
               "budget_seconds": args.budget, "repair": True, "benefit": "Evaluate the release's identifier normalization update."}
    (output / "targets.json").write_bytes(canonical({"targets": [target]}))
    (output / "request.json").write_bytes(canonical(request))
    if args.prepare_only:
        print(json.dumps({"prepared": str(output), "baseline": baseline, "candidate": candidate}))
        return 0
    target = validate_target(target)
    request = validate_request(request, {target["id"]: target})
    def emit(event):
        print(json.dumps({k: v for k, v in event.items() if k in {"stage", "type", "step", "status", "probe_id", "repair_id"}}), flush=True)
    api_checks = None
    evidence_path = output / "evidence"
    if args.api:
        from src.proofrun.release_api import create_server
        from src.proofrun.release_service import ReleaseService
        service = ReleaseService({target["id"]: target}, output / "worker")
        token = secrets.token_hex(32)
        server = create_server(service, token, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def http(path, body=None):
            call = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                data=canonical(body) if body is not None else None,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with urllib.request.urlopen(call, timeout=10) as response:
                return response.read()
        try:
            targets_reply = json.loads(http("/v1/release-targets"))
            request["event_id"] = "synthetic-release-evaluation"
            run = json.loads(http("/v1/release-events", request))
            duplicate = json.loads(http("/v1/release-events", request))
            assert duplicate["id"] == run["id"]
            observed_events = 0
            while run["status"] in ("queued", "running"):
                for event in run["events"][observed_events:]:
                    emit(event)
                observed_events = len(run["events"])
                time.sleep(1)
                run = json.loads(http("/v1/release-runs/" + run["id"]))
            if run["status"] != "completed":
                raise RuntimeError("The live API investigation did not complete; inspect worker/run.json.")
            result = run["result"]
            evidence_path = output / "worker" / run["id"] / "evidence"
            for artifact in result["artifacts"]:
                data = http("/v1/release-runs/" + run["id"] + "/artifacts/" + artifact["id"])
                assert len(data) == artifact["bytes"] and hashlib.sha256(data).hexdigest() == artifact["sha256"]
            api_checks = {"run_id": run["id"], "authenticated_targets": len(targets_reply["targets"]),
                          "idempotency_verified": True, "retrieved_hash_checked_artifacts": len(result["artifacts"])}
        finally:
            server.shutdown()
            worker.join()
            server.server_close()
            service.close()
    else:
        result = Investigation(target, request, evidence_path, emit=emit).run()
    measured = {"evaluation": "attempted live model + local Docker; synthetic product",
                "recommendation": result["recommendation"], "agent_status": result["agent"]["status"],
                "confirmed_findings": sum(f["status"] == "confirmed" for f in result["findings"]),
                "repair_statuses": [r["status"] for r in result["repairs"]],
                "elapsed_seconds": result["elapsed_seconds"], "evidence": str(evidence_path), "api_checks": api_checks,
                "candidate_deployed": False}
    (output / "evaluation.json").write_bytes(canonical(measured))
    print(json.dumps(measured, indent=2))
    return 0 if measured["confirmed_findings"] and result["recommendation"] == "skip" else 2


if __name__ == "__main__":
    raise SystemExit(main())
