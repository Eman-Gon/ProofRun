"""Authoritative, local evidence bindings for advisory failure research."""
import hashlib
import json
import re

from .research import ResearchError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def scope_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value):
        raise ResearchError(400, "invalid_scope", "Use an authorized workspace scope.")
    return value


def fixture_context(run, scope="operator"):
    if run.get("execution_status") != "completed" or run.get("finding_status") != "regression_reproduced":
        raise ResearchError(409, "finding_required", "Research requires a completed check with a reproduced regression.")
    bindings = run.get("bindings") or {}
    if not all(isinstance(bindings.get(key), str) and re.fullmatch(pattern, bindings[key])
               for key, pattern in (("revision", r"[a-f0-9]{40}"), ("source_sha256", r"[a-f0-9]{64}"),
                                    ("contract_sha256", r"[a-f0-9]{64}"))):
        raise ResearchError(409, "binding_required", "The finding is missing its source or contract binding.")
    return {"kind": "fixture", "run_id": run["run_id"], "finding_id": "regression",
            "scope": scope_name(scope), "job_key": run["job_key"],
            "evidence_sha256": digest({"bindings": bindings, "cases": run.get("cases", []),
                                       "finding_status": run["finding_status"]}),
            **{key: bindings[key] for key in ("revision", "source_sha256", "contract_sha256")}}


def release_context(run, finding_id, scope):
    if run.get("status") != "completed":
        raise ResearchError(409, "finding_required", "Wait for the investigation to complete before researching a finding.")
    result = run.get("result") or {}
    finding = next((f for f in result.get("findings", []) if f.get("id") == finding_id), None)
    if finding is None:
        raise ResearchError(404, "unknown_finding", "This finding does not belong to the investigation.")
    if finding.get("status") != "confirmed":
        raise ResearchError(409, "finding_required", "Research requires a confirmed finding.")
    request = run["request"]
    return {"kind": "release", "run_id": run["id"], "finding_id": finding_id,
            "scope": scope_name(scope), "target_id": request["target_id"],
            "baseline_revision": request["baseline_revision"], "candidate_revision": request["candidate_revision"],
            "evidence_sha256": digest({"finding": finding, "request": request,
                                       "contract_hash": result.get("contract_hash")})}
