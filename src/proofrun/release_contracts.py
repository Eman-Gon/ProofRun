"""Operator-owned scope for repository investigations (separate from fixture v1)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

SCHEMA = "proofrun.release.v1"
SHA = re.compile(r"[a-f0-9]{40}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
IMAGE = re.compile(r"(?:sha256:|[A-Za-z0-9][A-Za-z0-9._/:@-]*@sha256:)[a-f0-9]{64}\Z")
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512 or "\\" in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError("A safe repository-relative path is required.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("..", ".git") for p in path.parts):
        raise ValueError("A safe repository-relative path is required.")
    return path.as_posix()


def validate_target(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not ID.fullmatch(raw["id"]):
        raise ValueError("Each target needs a safe id.")
    target = copy.deepcopy(raw)
    repo = target.get("repository")
    if not isinstance(repo, str) or not Path(repo).is_absolute():
        raise ValueError("Targets require an operator-configured absolute local Git repository.")
    target["name"] = str(target.get("name", target["id"]))[:160]
    scopes = target.get("workspaces", [])
    if (not isinstance(scopes, list) or len(scopes) > 100 or any(not isinstance(s, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", s) for s in scopes)):
        raise ValueError("Workspace scopes must be a list of exact safe identifiers.")
    images = target.get("images", {})
    if (not isinstance(images, dict) or not images or any(not SHA.fullmatch(k)
            or not isinstance(v, str) or not IMAGE.fullmatch(v) for k, v in images.items())):
        raise ValueError("Map each approved exact commit to its immutable execution image.")
    requirements = target.get("requirements")
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 20:
        raise ValueError("Configure between 1 and 20 approved compatibility requirements.")
    seen = set()
    for req in requirements:
        if not isinstance(req, dict) or set(req) - {"id", "description", "kind", "path_prefix", "methods"}:
            raise ValueError("Invalid requirement fields.")
        if not isinstance(req.get("id"), str) or not ID.fullmatch(req["id"]) or req["id"] in seen:
            raise ValueError("Requirements need unique safe ids.")
        seen.add(req["id"])
        if req.get("kind") != "preserve_response":
            raise ValueError("This version supports explicit preserve_response requirements.")
        if not isinstance(req.get("description"), str) or not 1 <= len(req["description"]) <= 2000:
            raise ValueError("Describe the approved compatibility requirement.")
        prefix = req.get("path_prefix", "")
        if (not isinstance(prefix, str) or not prefix.startswith("/") or prefix.startswith("//")
                or any(c in prefix for c in "?%#\\") or "/../" in prefix or len(prefix) > 512):
            raise ValueError("Requirements need a literal endpoint path prefix.")
        if not isinstance(req.get("methods"), list) or not req["methods"] or set(req["methods"]) - METHODS:
            raise ValueError("Requirements need approved HTTP methods.")
    runtime = target.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("An operator-approved HTTP application runtime is required.")
    collector = runtime.get("collector_image")
    if not isinstance(collector, str) or not IMAGE.fullmatch(collector):
        raise ValueError("Configure a trusted immutable Python image for the independent HTTP collector.")
    for key in ("command", "test_command"):
        argv = runtime.get(key)
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError("Configure application and original-test argv lists.")
    for key in ("repair_paths", "exclude_paths", "test_paths"):
        paths = target.setdefault(key, [])
        if not isinstance(paths, list) or len(paths) > 50:
            raise ValueError("Invalid path configuration.")
        for path in paths:
            relative_path(path)
    if not target["test_paths"]:
        raise ValueError("List baseline test paths to preserve across comparison and repair.")
    pattern = target.get("test_success_pattern")
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 256:
        raise ValueError("Configure an original-suite completion marker proving nonzero tests ran.")
    re.compile(pattern)
    if target.get("staging") is not None and not isinstance(target["staging"], dict):
        raise ValueError("Staging must be operator configured.")
    target["contract_hash"] = digest(raw)
    return target


def load_targets(path: Path) -> dict[str, dict]:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Target registry is too large.")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or set(raw) != {"targets"} or not isinstance(raw["targets"], list):
        raise ValueError("Registry must contain a targets list.")
    targets = {}
    for item in raw["targets"]:
        target = validate_target(item)
        if target["id"] in targets:
            raise ValueError("Duplicate target id.")
        targets[target["id"]] = target
    return targets


def public_target(target: dict) -> dict:
    return {"id": target["id"], "name": target["name"], "requirements": target["requirements"],
            "revisions": sorted(target["images"]), "repair_enabled": bool(target["repair_paths"]),
            "staging_configured": bool(target.get("staging")), "contract_hash": target["contract_hash"]}


def validate_request(raw: dict, targets: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"target_id", "baseline_revision", "candidate_revision",
            "benefit", "budget_seconds", "repair", "event_id", "failure_research_id"}:
        raise ValueError("Unsupported release request fields.")
    request = copy.deepcopy(raw)
    if not isinstance(request.get("target_id"), str) or request["target_id"] not in targets:
        raise ValueError("Select a registered repository target.")
    target = targets[request["target_id"]]
    for key in ("baseline_revision", "candidate_revision"):
        revision = request.get(key)
        if not isinstance(revision, str) or not SHA.fullmatch(revision):
            raise ValueError("Use exact lowercase 40-character release commit SHAs.")
        if revision not in target["images"]:
            raise ValueError("Register the immutable execution image for this revision first.")
    if request["baseline_revision"] == request["candidate_revision"]:
        raise ValueError("Select two different release commits.")
    request.setdefault("budget_seconds", 300)
    if type(request["budget_seconds"]) is not int or not 180 <= request["budget_seconds"] <= 600:
        raise ValueError("Execution budget must be 180–600 seconds.")
    request.setdefault("repair", False)
    if type(request["repair"]) is not bool or (request["repair"] and not target["repair_paths"]):
        raise ValueError("Repair must be enabled in the target's application path allowlist.")
    if "failure_research_id" in request and (not request["repair"]
            or not isinstance(request["failure_research_id"], str)
            or not ID.fullmatch(request["failure_research_id"])):
        raise ValueError("Selected failure research requires repair and a valid report id.")
    request.setdefault("benefit", "")
    if not isinstance(request["benefit"], str) or len(request["benefit"]) > 2000:
        raise ValueError("Describe the release benefit in at most 2000 characters.")
    if "event_id" in request and (not isinstance(request["event_id"], str)
            or not ID.fullmatch(request["event_id"])):
        raise ValueError("Event id must contain 1–80 safe characters.")
    return request
