"""Draft PRs for reviewed static suggestions, never represented as tested repairs."""

import ast
import base64
import hashlib
from pathlib import PurePosixPath
from urllib.parse import quote

from .github_pr import PullRequestError, _run, _SHA
from .public_repo import normalize_repository, _python_findings, MAX_SOURCE_BYTES


def can_propose(finding):
    return (str(finding.get("id", "")).split(":")[0] in {"pandas-hour", "pydantic-optional"}
            and bool(finding.get("beforeCode")) and bool(finding.get("afterCode"))
            and len(finding["beforeCode"]) < 1000 and len(finding["afterCode"]) < 1000)


def _content(root, repository, path, revision):
    result = _run(root, ["api", f"repos/{repository}/contents/{quote(path, safe='/')}?ref={quote(revision, safe='')}"])
    try:
        raw = base64.b64decode("".join(result["content"].split()), validate=True)
        if result.get("type") != "file" or len(raw) > MAX_SOURCE_BYTES or not _SHA.fullmatch(result["sha"]):
            raise ValueError
        return raw, result["sha"]
    except (KeyError, TypeError, ValueError):
        raise PullRequestError("GitHub returned unsupported source content.") from None


def candidate(original, finding):
    """Recompute the finding against the pinned source and replace its exact span."""
    try:
        text = original.decode("utf-8-sig")
        matches = _python_findings(finding["file"], text, [])
        if not can_propose(finding) or not any(all(row.get(k) == finding.get(k) for k in
                ("id", "file", "line", "beforeCode", "afterCode")) for row in matches):
            raise ValueError
        lines = text.splitlines(keepends=True)
        line = finding["line"] - 1
        start = sum(len(s) for s in lines[:line]) + lines[line].index(finding["beforeCode"].splitlines()[0])
        end = start + len(finding["beforeCode"])
        if text[start:end] != finding["beforeCode"]:
            raise ValueError
        updated = text[:start] + finding["afterCode"] + text[end:]
        ast.parse(updated)
        return (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf-8")
    except (UnicodeError, ValueError, IndexError, KeyError, SyntaxError, RecursionError):
        raise PullRequestError("This suggestion cannot be applied exactly. Run a fresh scan or edit it manually.") from None


def create_public_draft(root, scan, finding_index):
    if scan.get("status") != "completed" or not isinstance(scan.get("result"), dict):
        raise PullRequestError("A completed public repository scan is required.")
    result = scan["result"]
    findings = result.get("findings", [])
    if type(finding_index) is not int or not 0 <= finding_index < len(findings):
        raise PullRequestError("Choose a finding from this scan.")
    finding = findings[finding_index]
    if not can_propose(finding):
        raise PullRequestError("This finding needs a manual migration; no automatic patch is available.")
    repository = normalize_repository(result["repository"])
    commit = result.get("commit", "")
    path = finding.get("file", "")
    if (not _SHA.fullmatch(commit) or not path or PurePosixPath(path).is_absolute()
            or any(p in {"", ".", ".."} for p in path.split("/")) or "\\" in path
            or any(ord(c) < 32 for c in path)):
        raise PullRequestError("The scan has invalid source coordinates. Scan the repository again.")
    repo = _run(root, ["api", f"repos/{repository}"])
    if repo.get("private") is not False or repo.get("archived") or repo.get("disabled"):
        raise PullRequestError("Draft PRs require an active public repository.")
    base = repo.get("default_branch")
    if not isinstance(base, str) or not base:
        raise PullRequestError("The repository has no default branch.")
    original, blob_sha = _content(root, repository, path, commit)
    repaired = candidate(original, finding)
    key = hashlib.sha256(repository.encode() + commit.encode() + path.encode() + repaired).hexdigest()[:20]
    branch = "codex/proofrun-" + key
    user = _run(root, ["api", "user"]).get("login")
    if not isinstance(user, str) or not user or "/" in user:
        raise PullRequestError("Sign in with GitHub CLI before creating a draft PR.")
    writable = repo.get("permissions", {}).get("push") is True
    destination = repository if writable else f"{user}/{repository.split('/')[1]}"
    head = destination.split('/')[0] + ":" + branch
    # Reuse the same PR on retries, including after an ambiguous network response.
    existing = _run(root, ["api", f"repos/{repository}/pulls?state=all&head={quote(head, safe='')}",
                           "--jq", "{items: .}"]).get("items", [])
    if existing:
        pull = existing[0]
        if pull.get("state") != "open":
            raise PullRequestError("A PR for this suggestion was already closed. Review it on GitHub before retrying.")
        return _result(pull, repository, branch)
    current = _run(root, ["api", f"repos/{repository}/git/ref/heads/{quote(base, safe='/')}"])
    if current.get("object", {}).get("sha") != commit:
        raise PullRequestError("The default branch changed since this scan. Check the repository again before creating a PR.")
    if not writable:
        fork = _run(root, ["api", "--method", "POST", f"repos/{repository}/forks", "--input", "-"], payload={})
        if (fork.get("full_name") != destination or fork.get("parent", {}).get("full_name", "").lower() != repository.lower()
                or fork.get("private") is not False):
            raise PullRequestError("GitHub could not prepare a matching public fork. Check your account and retry.")
    refs = _run(root, ["api", f"repos/{destination}/git/matching-refs/heads/{branch}",
                      "--jq", "{items: .}"]).get("items", [])
    matching = [ref for ref in refs if ref.get("ref") == "refs/heads/" + branch]
    if not matching:
        _run(root, ["api", "--method", "POST", f"repos/{destination}/git/refs", "--input", "-"],
             payload={"ref": "refs/heads/" + branch, "sha": commit})
    elif matching[0].get("object", {}).get("sha") != commit:
        # Only reuse our exact one-file commit; do not include unrelated branch edits.
        detail = _run(root, ["api", f"repos/{destination}/commits/{branch}"])
        if ([p.get("sha") for p in detail.get("parents", [])] != [commit]
                or [f.get("filename") for f in detail.get("files", [])] != [path]):
            raise PullRequestError("The proposal branch contains other changes. Review it on GitHub.")
    branch_bytes, branch_blob = _content(root, destination, path, branch)
    if branch_bytes == original:
        _run(root, ["api", "--method", "PUT", f"repos/{destination}/contents/{quote(path, safe='/')}", "--input", "-"],
             payload={"message": "fix: propose dependency migration", "content": base64.b64encode(repaired).decode(),
                      "branch": branch, "sha": branch_blob})
    elif branch_bytes != repaired:
        raise PullRequestError("The proposal branch was modified. Review it on GitHub before retrying.")
    pull = _run(root, ["api", "--method", "POST", f"repos/{repository}/pulls", "--input", "-"], payload={
        "title": "Propose " + finding["package"] + " migration fix", "head": head, "base": base, "draft": True,
        "body": (f"Static migration suggestion for `{path}:{finding['line']}` at `{commit}`.\n\n"
                 "**Unverified proposal:** repository tests were not run; a regression or successful repair has not been established. "
                 "The proposed Python source parses successfully. Review the intended behavior and run the repository's tests before merging.\n\n"
                 f"{finding['explanation']}\n\nReference: {finding['sourceUrl']}")})
    return _result(pull, repository, branch)


def _result(pull, repository, branch):
    number = pull.get("number")
    if type(number) is not int or pull.get("html_url") != f"https://github.com/{repository}/pull/{number}":
        raise PullRequestError("GitHub returned an unexpected PR response. Check GitHub before retrying.")
    return {"url": pull["html_url"], "number": number, "repository": repository,
            "branch": branch, "draft": pull.get("draft", True)}
