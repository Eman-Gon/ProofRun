import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.github_pr import PullRequestError, create_draft, eligible


def fixture(tmp_path: Path):
    demo = tmp_path / "demo/upgrade"
    evidence = tmp_path / ".commit-watch/upgrade-demo/run"
    demo.mkdir(parents=True)
    evidence.mkdir(parents=True)
    original = b"from typing import Optional\nclass Customer:\n    nickname: Optional[str]\n"
    repaired = b"from typing import Optional\nclass Customer:\n    nickname: Optional[str] = None\n"
    (demo / "app.py").write_bytes(original)
    (demo / "fixed_app.py").write_bytes(repaired)
    report = {
        "status": "confirmed_break", "repair_status": "verified",
        "app_sha256": hashlib.sha256(original).hexdigest(),
        "fixed_app_sha256": hashlib.sha256(repaired).hexdigest(),
    }
    path = evidence / "report.json"
    path.write_text(json.dumps(report))
    case = {
        "id": "pydantic", "status": "confirmed_break", "patch": "verified patch",
        "checks": [{"id": "fixed", "before": {"status": "pass"}, "after": {"status": "pass"}}],
    }
    return case, path, original, repaired


def completed(args, stdout="{}", code=0):
    return SimpleNamespace(args=args, returncode=code, stdout=stdout, stderr="")


def test_eligibility_binds_report_hashes_and_origin(tmp_path, monkeypatch):
    case, report, _, _ = fixture(tmp_path)
    monkeypatch.setattr("src.github_pr.subprocess.run", lambda args, **kwargs: completed(args, "https://github.com/owner/repo.git\n"))
    assert eligible(tmp_path, case, report) == (True, None)
    (tmp_path / "demo/upgrade/app.py").write_text("changed")
    allowed, reason = eligible(tmp_path, case, report)
    assert not allowed and "no longer matches" in reason


def test_create_draft_checks_remote_source_and_uses_bounded_payloads(tmp_path, monkeypatch):
    case, report, original, repaired = fixture(tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return completed(args, "https://github.com/owner/repo.git\n")
        endpoint = next((value for value in args if value.startswith("repos/")), "")
        if endpoint == "repos/owner/repo":
            return completed(args, json.dumps({"default_branch": "main"}))
        if endpoint.endswith("git/ref/heads/main"):
            return completed(args, json.dumps({"object": {"sha": "a" * 40}}))
        if "/contents/demo/upgrade/app.py?ref=" in endpoint:
            return completed(args, json.dumps({"sha": "b" * 40, "content": base64.b64encode(original).decode()}))
        if endpoint.endswith("/pulls"):
            return completed(args, json.dumps({"html_url": "https://github.com/owner/repo/pull/7", "number": 7}))
        return completed(args)

    monkeypatch.setattr("src.github_pr.subprocess.run", run)
    result = create_draft(tmp_path, case, report)
    assert result["url"] == "https://github.com/owner/repo/pull/7"
    payloads = [json.loads(body) for _, body in calls if body]
    assert any(item.get("content") == base64.b64encode(repaired).decode() for item in payloads)
    assert any(item.get("draft") is False for item in payloads)
    assert result["draft"] is False
    assert all("verified patch" not in json.dumps(item) for item in payloads)


def test_create_draft_rejects_stale_remote_before_mutation(tmp_path, monkeypatch):
    case, report, _, _ = fixture(tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "git":
            return completed(args, "https://github.com/owner/repo.git\n")
        endpoint = next((value for value in args if value.startswith("repos/")), "")
        if endpoint == "repos/owner/repo":
            return completed(args, json.dumps({"default_branch": "main"}))
        if endpoint.endswith("git/ref/heads/main"):
            return completed(args, json.dumps({"object": {"sha": "a" * 40}}))
        return completed(args, json.dumps({"sha": "b" * 40, "content": base64.b64encode(b"stale").decode()}))

    monkeypatch.setattr("src.github_pr.subprocess.run", run)
    with pytest.raises(PullRequestError, match="changed after verification"):
        create_draft(tmp_path, case, report)
    assert not any("git/refs" in argument for args in calls for argument in args)
