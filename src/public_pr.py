"""PRs for source-bound review suggestions, never represented as tested repairs."""

import ast
import base64
import hashlib
from pathlib import PurePosixPath
import re
from urllib.parse import quote

from .github_pr import PullRequestError, _run, _SHA
from .public_repo import normalize_repository, _python_findings, MAX_SOURCE_BYTES
from .repository_verification import digest


def can_propose(finding):
    if not isinstance(finding, dict):
        return False
    before, after = finding.get("beforeCode"), finding.get("afterCode")
    if (not all(isinstance(value, str) and value and "\x00" not in value
                and len(value.encode("utf-8")) <= 4000 for value in (before, after))
            or before == after or type(finding.get("line")) is not int or finding["line"] < 1):
        return False
    if finding.get("origin") == "agent":
        return bool(re.fullmatch(r"[0-9a-f]{64}", str(finding.get("sourceSha256", ""))))
    return str(finding.get("id", "")).split(":")[0] in {"pandas-hour", "pydantic-optional"}


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
    """Recheck the source binding and replace only the reviewed, exact span."""
    try:
        text = original.decode("utf-8-sig")
        if not can_propose(finding):
            raise ValueError
        if finding.get("origin") == "agent":
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != finding["sourceSha256"]:
                raise ValueError
        else:
            matches = _python_findings(finding["file"], text, [])
            if not any(all(row.get(k) == finding.get(k) for k in
                    ("id", "file", "line", "beforeCode", "afterCode")) for row in matches):
                raise ValueError
        lines = text.splitlines(keepends=True)
        line = finding["line"] - 1
        first = finding["beforeCode"].splitlines()[0]
        if not first or lines[line].count(first) != 1:
            raise ValueError
        start = sum(len(s) for s in lines[:line]) + lines[line].index(first)
        end = start + len(finding["beforeCode"])
        if text[start:end] != finding["beforeCode"]:
            raise ValueError
        updated = text[:start] + finding["afterCode"] + text[end:]
        if PurePosixPath(finding["file"]).suffix == ".py":
            ast.parse(updated)
        return (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf-8")
    except (UnicodeError, ValueError, IndexError, KeyError, TypeError, SyntaxError, RecursionError):
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
        raise PullRequestError("This finding needs a manual change; no exact patch is available.")
    repository = normalize_repository(result["repository"])
    commit = result.get("commit", "")
    path = finding.get("file", "")
    if (not _SHA.fullmatch(commit) or not path or PurePosixPath(path).is_absolute()
            or any(p in {"", ".", ".."} for p in path.split("/")) or "\\" in path
            or any(ord(c) < 32 for c in path)):
        raise PullRequestError("The scan has invalid source coordinates. Scan the repository again.")
    repo = _run(root, ["api", f"repos/{repository}"])
    if repo.get("private") is not False or repo.get("archived") or repo.get("disabled"):
        raise PullRequestError("PRs require an active public repository.")
    base = repo.get("default_branch")
    if not isinstance(base, str) or not base:
        raise PullRequestError("The repository has no default branch.")
    original, blob_sha = _content(root, repository, path, commit)
    repaired = candidate(original, finding)
    key = hashlib.sha256(repository.encode() + commit.encode() + path.encode() + repaired).hexdigest()[:20]
    branch = "codex/proofrun-" + key
    user = _run(root, ["api", "user"]).get("login")
    if not isinstance(user, str) or not user or "/" in user:
        raise PullRequestError("Sign in with GitHub CLI before creating a PR.")
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
             payload={"message": "fix: apply reviewed source suggestion", "content": base64.b64encode(repaired).decode(),
                      "branch": branch, "sha": branch_blob})
    elif branch_bytes != repaired:
        raise PullRequestError("The proposal branch was modified. Review it on GitHub before retrying.")
    validation = ("The proposed Python source parses successfully. " if PurePosixPath(path).suffix == ".py"
                  else "Syntax and runtime behavior have not been checked. ")
    reproduction = finding.get("reproduction")
    evidence = finding.get("testEvidence") or {}
    bound = (evidence.get("commit") == commit and evidence.get("patchSha256") ==
             digest([path, finding["beforeCode"], finding["afterCode"]]))
    measured = ("**Selected tests passed:** unchanged repository tests failed on the original snapshot and passed on this proposed patch in Docker. "
                "This is limited test evidence, not full-suite verification. " if bound and evidence.get("patchStatus") == "passes_selected_tests"
                else "**Unverified proposal:** a successful repair has not been established. ")
    if not evidence:
        measured += "Repository tests were not run. "
    if bound:
        validation = f"Test comparison: {evidence.get('status')}; patch: {evidence.get('patchStatus')}. Tests hash: {evidence.get('testsSha256')}. "
    check = f"\n\nSuggested validation (not executed):\n{reproduction}" if reproduction else ""
    title = " ".join(str(finding.get("title") or "Review source fix").split())[:180]
    pull = _run(root, ["api", "--method", "POST", f"repos/{repository}/pulls", "--input", "-"], payload={
        "title": title, "head": head, "base": base, "draft": False,
        "body": (f"Source review suggestion for `{path}:{finding['line']}` at `{commit}`.\n\n"
                 f"{measured}{validation}Review the intended behavior and run the repository's tests before merging.\n\n"
                 f"{finding['explanation']}{check}\n\nReference: {finding['sourceUrl']}")})
    return _result(pull, repository, branch)


def combined_candidate(original, findings):
    """Validate against the original snapshot, then apply nonoverlapping spans backwards."""
    try:
        text = original.decode("utf-8-sig")
    except UnicodeError:
        raise PullRequestError("Source is not UTF-8. Review it manually.") from None
    spans = {}
    for finding in findings:
        candidate(original, finding)
        lines = text.splitlines(keepends=True)
        line = finding["line"] - 1
        start = sum(map(len, lines[:line])) + lines[line].index(finding["beforeCode"].splitlines()[0])
        span = (start, start + len(finding["beforeCode"]))
        if span in spans and spans[span] != finding["afterCode"]:
            raise PullRequestError("Conflicting suggestions overlap. Review them individually.")
        spans[span] = finding["afterCode"]
    previous_end = -1
    for start, end in sorted(spans):
        if start < previous_end:
            raise PullRequestError("Conflicting suggestions overlap. Review them individually.")
        previous_end = end
    for (start, end), replacement in sorted(spans.items(), reverse=True):
        text = text[:start] + replacement + text[end:]
    if PurePosixPath(findings[0]["file"]).suffix == ".py":
        try:
            ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            raise PullRequestError("The combined Python patch does not parse. Review suggestions individually.") from None
    return (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")


def create_public_batch(root, scan):
    if scan.get("status") != "completed" or not isinstance(scan.get("result"), dict):
        raise PullRequestError("A completed public repository scan is required.")
    result = scan["result"]
    findings = result.get("findings", [])
    selected = [(i, row) for i, row in enumerate(findings) if can_propose(row)]
    if not selected:
        raise PullRequestError("No exact patches are available for this scan.")
    repository = normalize_repository(result["repository"])
    commit = result.get("commit", "")
    grouped = {}
    for index, finding in selected:
        path = finding.get("file", "")
        if (not _SHA.fullmatch(commit) or not path or PurePosixPath(path).is_absolute()
                or any(p in {"", ".", ".."} for p in path.split("/")) or "\\" in path
                or any(ord(c) < 32 for c in path)):
            raise PullRequestError("The scan has invalid source coordinates. Scan the repository again.")
        grouped.setdefault(path, []).append(finding)
    repo = _run(root, ["api", f"repos/{repository}"])
    if repo.get("private") is not False or repo.get("archived") or repo.get("disabled"):
        raise PullRequestError("PRs require an active public repository.")
    base = repo.get("default_branch")
    if not isinstance(base, str) or not base:
        raise PullRequestError("The repository has no default branch.")
    patches = {}
    for path, rows in sorted(grouped.items()):
        original, _ = _content(root, repository, path, commit)
        patches[path] = combined_candidate(original, rows)
        if patches[path] == original:
            raise PullRequestError("Combined suggestions cancel out. Review them individually.")
    key = digest([repository, commit, [(p, hashlib.sha256(v).hexdigest()) for p, v in patches.items()]])[:20]
    branch = "codex/proofrun-all-" + key
    user = _run(root, ["api", "user"]).get("login")
    if not isinstance(user, str) or not user or "/" in user:
        raise PullRequestError("Sign in with GitHub CLI before creating a PR.")
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
        response = _result(pull, repository, branch)
        response["findingIndices"] = [i for i, _ in selected]
        return response
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
    if matching:
        detail = _run(root, ["api", f"repos/{destination}/commits/{branch}?per_page=100"])
        if ([p.get("sha") for p in detail.get("parents", [])] != [commit]
                or sorted(f.get("filename") for f in detail.get("files", [])) != sorted(patches)):
            raise PullRequestError("The proposal branch contains other changes. Review it on GitHub.")
        for path, repaired in patches.items():
            branch_bytes, _ = _content(root, destination, path, branch)
            if branch_bytes != repaired:
                raise PullRequestError("The proposal branch was modified. Review it on GitHub before retrying.")
    else:
        base_commit = _run(root, ["api", f"repos/{repository}/git/commits/{commit}"])
        tree = []
        for path, repaired in patches.items():
            blob = _run(root, ["api", "--method", "POST", f"repos/{destination}/git/blobs", "--input", "-"],
                        payload={"content": base64.b64encode(repaired).decode(), "encoding": "base64"})
            # Preserve executable bits and reject symlinks/submodules.
            parent = str(PurePosixPath(path).parent)
            entry_tree = base_commit["tree"]["sha"]
            for part in ([] if parent == "." else parent.split("/")):
                entries = _run(root, ["api", f"repos/{repository}/git/trees/{entry_tree}"])["tree"]
                entry_tree = next(e["sha"] for e in entries if e["path"] == part and e["type"] == "tree")
            entries = _run(root, ["api", f"repos/{repository}/git/trees/{entry_tree}"])["tree"]
            entry = next(e for e in entries if e["path"] == PurePosixPath(path).name)
            if entry.get("mode") not in {"100644", "100755"}:
                raise PullRequestError("Only regular source files can be patched.")
            tree.append({"path": path, "mode": entry["mode"], "type": "blob", "sha": blob["sha"]})
        updated_tree = _run(root, ["api", "--method", "POST", f"repos/{destination}/git/trees", "--input", "-"],
                            payload={"base_tree": base_commit["tree"]["sha"], "tree": tree})
        updated_commit = _run(root, ["api", "--method", "POST", f"repos/{destination}/git/commits", "--input", "-"],
                              payload={"message": "fix: apply all source review suggestions", "tree": updated_tree["sha"], "parents": [commit]})
        _run(root, ["api", "--method", "POST", f"repos/{destination}/git/refs", "--input", "-"],
             payload={"ref": "refs/heads/" + branch, "sha": updated_commit["sha"]})
    descriptions = "\n".join(f"- `{row['file']}:{row['line']}`: {row.get('title', 'Source suggestion')}" for _, row in selected)
    skipped = len(findings) - len(selected)
    pull = _run(root, ["api", "--method", "POST", f"repos/{repository}/pulls", "--input", "-"], payload={
        "title": f"fix: address {len(selected)} source review findings", "head": head, "base": base, "draft": False,
        "body": (f"Combined source suggestions at `{commit}`.\n\n{descriptions}\n\n"
                 f"{skipped} findings without exact patches require manual review.\n\n"
                 "**Unverified combined proposal:** Python source parses, but the combined patch has not been tested. "
                 "Individual patch test results do not verify this combined change. Review behavior and run repository tests before merging.")})
    response = _result(pull, repository, branch)
    response["findingIndices"] = [i for i, _ in selected]
    return response


def _result(pull, repository, branch):
    number = pull.get("number")
    if type(number) is not int or pull.get("html_url") != f"https://github.com/{repository}/pull/{number}":
        raise PullRequestError("GitHub returned an unexpected PR response. Check GitHub before retrying.")
    return {"url": pull["html_url"], "number": number, "repository": repository,
            "branch": branch, "draft": pull.get("draft", False)}
