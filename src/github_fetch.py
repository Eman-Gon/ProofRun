"""Read immutable commit metadata and diffs from the GitHub REST API."""

from dataclasses import asdict, dataclass
import base64
import binascii
import re
from urllib.parse import quote

import requests


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str
    additions: int
    deletions: int
    previous_filename: str | None = None


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: str
    message: str
    files: list[ChangedFile]
    additions: int
    deletions: int
    parents: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    def memory_text(self) -> str:
        """One structured text chunk per commit; never include patch bodies."""
        files = "\n".join(
            f"  {f.filename} ({f.status}, +{f.additions}/-{f.deletions})"
            for f in self.files
        )
        return (
            f"SHA: {self.sha}\nAuthor: {self.author}\nDate: {self.date}\n"
            f"Message: {self.message}\n"
            f"Lines added: {self.additions}\nLines removed: {self.deletions}\n"
            f"Files touched:\n{files}"
        )


def validate_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("TARGET_REPO must be owner/name (not a URL).")
    return repo


def validate_sha(sha: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise ValueError("Commit must be a 7–40 character hexadecimal Git SHA.")
    return sha.lower()


class GitHubClient:
    def __init__(self, repo: str, token: str = "", session=None):
        self.repo = validate_repo(repo)
        self.base = f"https://api.github.com/repos/{self.repo}"
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "commit-watch",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, **kwargs):
        try:
            response = self.session.get(f"{self.base}/{path}", timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise GitHubError("Cannot reach GitHub; check your connection.") from exc
        if not response.ok:
            remaining = response.headers.get("X-RateLimit-Remaining")
            if response.status_code == 429 or remaining == "0":
                detail = "GitHub rate limit reached; retry later or set GITHUB_TOKEN."
            elif response.status_code in (401, 403, 404):
                detail = "Check TARGET_REPO, commit SHA, and GITHUB_TOKEN read access."
            else:
                detail = "GitHub could not serve this request; retry later."
            raise GitHubError(f"GitHub HTTP {response.status_code}. {detail}")
        return response

    def commit(self, ref: str) -> Commit:
        path = f"commits/{quote(ref, safe='')}"
        response = self._get(path, params={"per_page": 100, "page": 1})
        data = response.json()
        files = list(data.get("files", []))
        page = 1
        while "next" in response.links:
            page += 1
            if page > 30:
                raise GitHubError("Commit exceeds GitHub's 3,000-file limit; use a smaller demo commit.")
            response = self._get(path, params={"per_page": 100, "page": page})
            files.extend(response.json().get("files", []))
        if len(files) >= 3000:
            raise GitHubError("Commit may exceed GitHub's file limit; refusing incomplete evidence.")
        meta = data["commit"]
        author = (data.get("author") or {}).get("login") or meta["author"]["name"]
        return Commit(
            sha=data["sha"], author=author, date=meta["author"]["date"],
            message=meta["message"],
            files=[ChangedFile(
                filename=f["filename"], status=f["status"],
                additions=f["additions"], deletions=f["deletions"],
                previous_filename=f.get("previous_filename"),
            ) for f in files],
            additions=data["stats"]["additions"], deletions=data["stats"]["deletions"],
            parents=[p["sha"] for p in data["parents"]],
        )

    def history(self, count: int = 40, ref: str | None = None) -> list[Commit]:
        if not 30 <= count <= 50:
            raise ValueError("Baseline must contain 30–50 commits.")
        params = {"per_page": count}
        if ref:
            params["sha"] = ref
        rows = self._get("commits", params=params).json()
        if len(rows) < 30:
            raise GitHubError("Target has fewer than 30 reachable commits; pick an older repo/ref.")
        return [self.commit(row["sha"]) for row in rows[:count]]

    def diff(self, sha: str) -> str:
        # A separate diff-media request avoids GitHub's truncated JSON patch fields.
        response = self._get(
            f"commits/{validate_sha(sha)}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        if len(response.content) > 120_000:
            raise GitHubError("Diff exceeds 120 KB; choose a smaller commit instead of truncating evidence.")
        return response.text

    def tree_paths(self, sha: str) -> list[str]:
        data = self._get(f"git/trees/{validate_sha(sha)}", params={"recursive": "1"}).json()
        if data.get("truncated"):
            raise GitHubError("GitHub truncated the file tree; cannot verify test coverage context.")
        return [node["path"] for node in data["tree"] if node["type"] == "blob"]

    def file_text(self, path: str, sha: str) -> str:
        """Read one dependency snapshot; never trust partial JSON diff patches."""
        data = self._get(f"contents/{quote(path, safe='/')}",
                         params={"ref": validate_sha(sha)}).json()
        if (not isinstance(data, dict) or data.get("type") != "file"
                or data.get("encoding") != "base64" or data.get("size", 0) > 262_144):
            raise GitHubError("Dependency snapshot is unavailable or exceeds 256 KB.")
        try:
            raw = base64.b64decode("".join(data["content"].split()), validate=True)
            if len(raw) > 262_144:
                raise ValueError("oversized")
            return raw.decode("utf-8")
        except (KeyError, ValueError, UnicodeError, binascii.Error) as exc:
            raise GitHubError("Dependency snapshot is not complete UTF-8 text.") from exc

    def is_ancestor(self, base: str, head: str) -> bool:
        data = self._get(f"compare/{validate_sha(base)}...{validate_sha(head)}").json()
        return data["status"] in ("ahead", "identical")
