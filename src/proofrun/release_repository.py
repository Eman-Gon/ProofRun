"""Read complete tracked release trees without checking out or executing repository code."""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
from pathlib import Path
import subprocess
import time

from .release_contracts import SHA, canonical, relative_path

MAX_FILES = 6000
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
SECRET_PARTS = {".git", ".ssh", ".aws", ".azure", ".kube", "node_modules", ".venv", "venv"}


def excluded(path: str, patterns=()) -> bool:
    parts = Path(path).parts
    return (any(p in SECRET_PARTS or p.startswith(".env") for p in parts)
            or path.lower().endswith((".pem", ".key", ".p12", ".pfx"))
            or Path(path).name in {"id_rsa", "id_ed25519", "credentials", ".npmrc", ".pypirc"}
            or any(fnmatch.fnmatchcase(path, p) for p in patterns))


def git(repo: Path, args: list[str], deadline: float, max_bytes: int = MAX_BYTES) -> bytes:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Repository inspection budget expired.")
    # Ignore global config/hooks; never run checkout filters, textconv, external diff, or submodules.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    import tempfile
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                                    stdout=output, stderr=subprocess.DEVNULL,
                                    timeout=min(left, 20), env=env)
        except subprocess.TimeoutExpired:
            raise TimeoutError("Repository inspection exceeded its deadline.") from None
        if result.returncode:
            raise ValueError("The registered repository does not contain the requested Git object.")
        if output.tell() > max_bytes:
            raise ValueError("Repository inspection exceeded its output bound.")
        output.seek(0)
        return output.read()


def snapshot(repo: Path, revision: str, destination: Path, deadline: float, patterns=()) -> dict:
    if not SHA.fullmatch(revision):
        raise ValueError("An exact commit SHA is required.")
    actual = git(repo, ["rev-parse", "--verify", revision + "^{commit}"], deadline).decode().strip()
    if actual != revision:
        raise ValueError("The released commit could not be pinned exactly.")
    entries = git(repo, ["ls-tree", "-r", "-z", "--full-tree", revision], deadline).split(b"\0")
    if len(entries) > MAX_FILES + 1:
        raise ValueError("Repository exceeds the supported tracked-file bound.")
    destination.mkdir(parents=True, exist_ok=False)
    manifest = []
    omissions = []
    total = 0
    for entry in entries:
        if not entry:
            continue
        metadata, rawpath = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = relative_path(rawpath.decode("utf-8", errors="strict"))
        if excluded(path, patterns):
            omissions.append(path)
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("Symlinks and submodules are unsupported; configure an explicit exclusion.")
        size = int(git(repo, ["cat-file", "-s", oid], deadline, 256))
        total += size
        if size > MAX_FILE or total > MAX_BYTES:
            raise ValueError("Repository exceeds the supported source-size bound.")
        data = git(repo, ["cat-file", "blob", oid], deadline, MAX_FILE)
        file = destination / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        file.chmod(0o755 if mode == "100755" else 0o644)
        manifest.append({"path": path, "git_blob": oid, "sha256": hashlib.sha256(data).hexdigest(),
                         "bytes": len(data), "mode": mode})
    return {"revision": revision, "files": manifest, "excluded_paths": omissions,
            "tree_hash": hashlib.sha256(canonical(manifest)).hexdigest()}


class SourceTools:
    def __init__(self, roots: dict[str, Path], manifests: dict):
        self.roots, self.manifests = roots, manifests

    def root(self, revision):
        if revision not in self.roots:
            raise ValueError("Revision must be baseline or candidate.")
        return self.roots[revision]

    def read(self, revision, path, start_line=1, end_line=240):
        root = self.root(revision)
        path = relative_path(path)
        if path not in {e["path"] for e in self.manifests[revision]["files"]}:
            raise ValueError("File is absent or excluded from this pinned snapshot.")
        if (type(start_line) is not int or type(end_line) is not int
                or start_line < 1 or end_line < start_line or end_line - start_line >= 300):
            raise ValueError("Read a range of at most 300 source lines.")
        text = (root / path).read_text(errors="replace")
        if "\0" in text:
            raise ValueError("Binary file; use the manifest's content hash.")
        lines = text.splitlines()
        result = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start_line, min(end_line, len(lines)) + 1))
        return {"path": path, "revision": revision, "total_lines": len(lines),
                "text": result[:16000], "truncated": len(result) > 16000}

    def invoke(self, name, args):
        if name == "read_file":
            return self.read(**args)
        if name == "list_files":
            revision = args.get("revision", "candidate")
            self.root(revision)
            prefix = args.get("prefix", "")
            if not isinstance(prefix, str):
                raise ValueError("Prefix must be text.")
            paths = [e["path"] for e in self.manifests[revision]["files"] if e["path"].startswith(prefix)]
            return {"paths": paths[:300], "total": len(paths), "truncated": len(paths) > 300}
        if name == "search":
            revision, query = args.get("revision", "candidate"), args.get("query", "")
            root = self.root(revision)
            if not isinstance(query, str) or not 1 <= len(query) <= 200:
                raise ValueError("Search needs a literal string of 1–200 characters.")
            matches = []
            for entry in self.manifests[revision]["files"]:
                for number, line in enumerate((root / entry["path"]).read_text(errors="replace").splitlines(), 1):
                    if query in line:
                        matches.append({"path": entry["path"], "line": number, "text": line[:400]})
                        if len(matches) == 40:
                            return {"matches": matches, "truncated": True}
            return {"matches": matches, "truncated": False}
        if name == "diff":
            before = {e["path"]: e for e in self.manifests["baseline"]["files"]}
            after = {e["path"]: e for e in self.manifests["candidate"]["files"]}
            changed = [p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
            path = args.get("path")
            if not path:
                return {"changed_paths": changed[:300], "total": len(changed)}
            path = relative_path(path)
            if path not in changed:
                return {"path": path, "diff": "No change in included source."}
            texts = [(self.roots[r] / path).read_text(errors="replace").splitlines(True)
                     if path in m else [] for r, m in (("baseline", before), ("candidate", after))]
            diff = "".join(difflib.unified_diff(*texts, fromfile="baseline/" + path, tofile="candidate/" + path))
            return {"path": path, "diff": diff[:20000], "truncated": len(diff) > 20000}
        raise ValueError("Unsupported repository inspection tool.")
