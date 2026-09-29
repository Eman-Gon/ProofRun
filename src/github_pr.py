"""Publish one verified ProofRun fixture repair as a GitHub PR."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4


class PullRequestError(RuntimeError):
    """A bounded, user-readable PR publication failure."""


_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _run(root: Path, arguments: list[str], *, payload: dict | None = None) -> dict:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "GH_CONFIG_DIR") if key in os.environ}
    env.update(GH_PROMPT_DISABLED="1", NO_COLOR="1")
    try:
        result = subprocess.run(
            ["gh", *arguments], cwd=root, env=env, input=json.dumps(payload) if payload is not None else None,
            text=True, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PullRequestError("GitHub CLI is unavailable or timed out.") from exc
    if result.returncode != 0:
        raise PullRequestError("GitHub rejected the PR request. Check authentication and repository access.")
    try:
        value = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise PullRequestError("GitHub returned an invalid response.") from None
    if not isinstance(value, dict):
        raise PullRequestError("GitHub returned an invalid response.")
    return value


def _repository(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PullRequestError("The local checkout has no readable GitHub origin.") from exc
    if result.returncode != 0:
        raise PullRequestError("The local checkout has no readable GitHub origin.")
    remote = result.stdout.strip()
    if remote.startswith("git@github.com:"):
        repository = remote.removeprefix("git@github.com:").removesuffix(".git")
    else:
        parsed = urlsplit(remote)
        repository = parsed.path.strip("/").removesuffix(".git") if parsed.scheme == "https" and parsed.netloc == "github.com" else ""
    if not _REPOSITORY.fullmatch(repository):
        raise PullRequestError("The origin is not a supported GitHub repository.")
    return repository


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def eligible(root: Path, case: dict, report_path: Path | None) -> tuple[bool, str | None]:
    if case.get("id") != "pydantic" or case.get("status") != "confirmed_break":
        return False, "A confirmed fixture regression is required."
    checks = {row.get("id"): row for row in case.get("checks", []) if isinstance(row, dict)}
    fixed = checks.get("fixed", {})
    if any(fixed.get(side, {}).get("status") != "pass" for side in ("before", "after")):
        return False, "The repair must pass in both environments."
    if not case.get("patch") or report_path is None:
        return False, "Verified patch evidence is unavailable."
    try:
        report = json.loads(report_path.read_text())
        original = (root / "demo/upgrade/app.py").read_bytes()
        repaired = (root / "demo/upgrade/fixed_app.py").read_bytes()
    except (OSError, json.JSONDecodeError):
        return False, "Verified patch evidence is unavailable."
    if (report.get("repair_status") != "verified" or report.get("status") != "confirmed_break"
            or report.get("app_sha256") != _digest(original)
            or report.get("fixed_app_sha256") != _digest(repaired)):
        return False, "The tested source no longer matches the proposed repair."
    try:
        _repository(root)
    except PullRequestError as exc:
        return False, str(exc)
    return True, None


def create_draft(root: Path, case: dict, report_path: Path | None) -> dict:
    allowed, reason = eligible(root, case, report_path)
    if not allowed:
        raise PullRequestError(reason or "This repair cannot be published.")
    repository = _repository(root)
    repo = _run(root, ["api", f"repos/{repository}"])
    base = repo.get("default_branch")
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", base):
        raise PullRequestError("GitHub returned an invalid default branch.")
    ref = _run(root, ["api", f"repos/{repository}/git/ref/heads/{base}"])
    base_sha = ref.get("object", {}).get("sha")
    if not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha):
        raise PullRequestError("GitHub returned an invalid base revision.")

    path = "demo/upgrade/app.py"
    remote = _run(root, ["api", f"repos/{repository}/contents/{path}?ref={base_sha}"])
    remote_sha = remote.get("sha")
    encoded = remote.get("content")
    try:
        remote_bytes = base64.b64decode("".join(encoded.split()), validate=True) if isinstance(encoded, str) else b""
    except ValueError:
        remote_bytes = b""
    original = (root / path).read_bytes()
    repaired = (root / "demo/upgrade/fixed_app.py").read_bytes()
    if not isinstance(remote_sha, str) or not _SHA.fullmatch(remote_sha) or remote_bytes != original:
        raise PullRequestError("The default-branch source changed after verification. Run the comparison again.")

    branch = f"codex/proofrun-optional-nickname-{base_sha[:8]}-{uuid4().hex[:6]}"
    created_branch = False
    try:
        _run(root, ["api", "--method", "POST", f"repos/{repository}/git/refs", "--input", "-"],
             payload={"ref": "refs/heads/" + branch, "sha": base_sha})
        created_branch = True
        _run(root, ["api", "--method", "PUT", f"repos/{repository}/contents/{path}", "--input", "-"], payload={
            "message": "fix: preserve omitted nickname behavior",
            "content": base64.b64encode(repaired).decode(),
            "branch": branch,
            "sha": remote_sha,
        })
        pull = _run(root, ["api", "--method", "POST", f"repos/{repository}/pulls", "--input", "-"], payload={
            "title": "Fix omitted nickname behavior after Pydantic upgrade",
            "head": branch,
            "base": base,
            "draft": False,
            "body": (
                "ProofRun reproduced the omitted-nickname regression between Pydantic 1.10.18 and 2.8.2.\n\n"
                "This adds the explicit `None` default verified by the original suite, the targeted omission check, "
                "and five independent controls in both environments."
            ),
        })
    except PullRequestError:
        if created_branch:
            try:
                _run(root, ["api", "--method", "DELETE", f"repos/{repository}/git/refs/heads/{branch}"])
            except PullRequestError:
                pass
        raise
    url = pull.get("html_url")
    number = pull.get("number")
    if not isinstance(url, str) or not url.startswith(f"https://github.com/{repository}/pull/") or type(number) is not int:
        raise PullRequestError("GitHub created the PR but returned an invalid result.")
    return {"url": url, "number": number, "repository": repository, "branch": branch, "draft": pull.get("draft", False)}
