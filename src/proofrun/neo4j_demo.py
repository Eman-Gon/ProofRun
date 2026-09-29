"""Explicit acceptance experiment using a real Neo4j database and native worker.

Creates three namespaced synthetic deployment registrations. Never executes or
contacts real deployments. Requires prepared fixture Docker images.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from .api import create_server
from .deployment_graph import DeploymentGraphService
from .neo4j_store import GraphConfig, Neo4jStore
from .service import CASE_ID, RunService, TERMINAL, sha256


def experiment(root: Path, output: Path):
    config = GraphConfig.from_env()
    if not config.enabled:
        raise ValueError("Enable Neo4j and supply server-side database settings first.")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    runs = graph = server = thread = None
    try:
        runs = RunService(root, output / "worker", runner_mode="native")
        graph = DeploymentGraphService(runs, output / "selections", Neo4jStore(config))
        token = secrets.token_urlsafe(32)
        server = create_server(runs, token, port=0, graph=graph)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, payload=None):
            data = None if payload is None else json.dumps(payload).encode()
            req = Request(base + path, data=data,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=30) as response:
                    raw = response.read()
                    if "/artifacts/" in path:
                        return raw
                    return json.loads(raw)
            except HTTPError as exc:
                # Server responses are sanitized. Do not print connection data.
                error = json.loads(exc.read()).get("error", {})
                raise RuntimeError(f"Worker returned {exc.code}: {error.get('code', 'request_failed')}") from None

        registered = request(f"/v1/cases/{CASE_ID}")["submission"]
        prefix = "neo4j-demo-" + uuid4().hex[:12]
        contract_id = registered["contract"]["id"]
        current_hash = registered["contract"]["sha256"]
        # The previous version is synthetic metadata; only the current contract
        # is approved for execution by the trusted fixture registry.
        previous_hash = sha256((prefix + "-previous-synthetic-contract").encode())
        secondary_id = prefix + "-secondary"
        deployments = []
        for suffix in ("a", "b", "c"):
            deployment = {"deployment_id": prefix + "-" + suffix,
                "revision": registered["source"]["revision"],
                "source_sha256": registered["source"]["sha256"],
                "contracts": [{"contract_id": contract_id if suffix != "c" else secondary_id,
                    "contract_sha256": previous_hash if suffix != "c" else sha256(secondary_id.encode()),
                    "case_id": CASE_ID}]}
            request("/v1/deployments", deployment)
            deployments.append(deployment)
        change = {"change_id": prefix + "-change", "contract_id": contract_id,
                  "previous_sha256": previous_hash, "contract_sha256": current_hash}
        selected = request("/v1/contract-changes", change)
        expected = [d["deployment_id"] for d in deployments[:2]]
        if [d["deployment_id"] for d in selected["deployments"]] != expected:
            raise RuntimeError("Neo4j did not select exactly the two dependent deployments.")
        path = "/v1/contract-changes/" + change["change_id"]
        submission = {"deployment_id": expected[0], "job_key": prefix + "-recheck"}
        run = request(path + "/runs", submission)
        deadline = time.monotonic() + 180
        while run["execution_status"] not in TERMINAL:
            if time.monotonic() >= deadline:
                raise RuntimeError("Selected native run did not finish within 180 seconds.")
            time.sleep(0.2)
            run = request("/v1/runs/" + run["run_id"])
        final = request(path)
        if (final["deployments"][0]["status"] != "measured"
                or final["deployments"][0]["requires_reverification"]
                or not final["deployments"][1]["requires_reverification"]
                or run["finding_status"] != "regression_reproduced"
                or run["bindings"]["contract_sha256"] != current_hash):
            raise RuntimeError("The native verifier did not measure the selected current contract.")
        retry = request(path + "/runs", submission)
        if retry["run_id"] != run["run_id"]:
            raise RuntimeError("A repeated selection submission created another run.")
        # Once another contract version is requested, the existing measured run
        # must not satisfy it (nor bypass the approved fixture registry).
        updated = {**deployments[0], "contracts": [{"contract_id": contract_id,
                    "contract_sha256": current_hash, "case_id": CASE_ID}]}
        request("/v1/deployments", updated)
        next_change = {**change, "change_id": prefix + "-next", "previous_sha256": current_hash,
                       "contract_sha256": sha256((prefix + "-unregistered-next-contract").encode())}
        request("/v1/contract-changes", next_change)
        stale_rejected = False
        try:
            request("/v1/contract-changes/" + next_change["change_id"] + "/runs", submission)
        except RuntimeError as exc:
            if str(exc) != "Worker returned 409: selection_stale":
                raise
            stale_rejected = True
        if not stale_rejected:
            raise RuntimeError("Old-contract evidence was not rejected.")
        artifacts = output / "artifacts"
        artifacts.mkdir(mode=0o700)
        for artifact in run["artifacts"]:
            content = request(artifact["href"])
            if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                raise RuntimeError("Artifact content did not match the measured run.")
            (artifacts / artifact["id"]).write_bytes(content)
        receipt = {"schema_version": "proofrun.neo4j-demo.v1", "provider": "neo4j",
            "database_transport": "live", "execution_target": "local", "data": "synthetic",
            "selected_deployment_ids": expected, "selection": final,
            "run_id": run["run_id"], "execution_status": run["execution_status"],
            "finding_status": run["finding_status"], "repair_status": run["repair_status"],
            "bindings": run["bindings"], "checks_executed": len(run["executed_case_ids"]),
            "artifact_count": len(run["artifacts"]), "old_contract_reuse_rejected": stale_rejected,
            "idempotent_run_retry": True,
            "limitations": ["Three synthetic deployment registrations; no customer endpoint was contacted.",
                "Previous/secondary/next contracts are synthetic metadata; only the registered contract executed.",
                "Real database calls and local Docker checks; no Aura, Crusoe, portal or generated repair claim.",
                "Synthetic graph nodes remain under prefix " + prefix + "."]}
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        (output / "run.json").write_text(json.dumps(run, indent=2) + "\n")
        return receipt
    finally:
        if server is not None:
            if thread is not None:
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()
        if runs is not None:
            runs.close()
        if graph is not None:
            graph.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        experiment(Path(__file__).resolve().parents[2], args.output_dir.resolve())
    except Exception:
        print("Neo4j acceptance failed. Check database settings, a new output directory, and prepared Docker images.")
        return 2
    print(str(args.output_dir.resolve() / "receipt.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
