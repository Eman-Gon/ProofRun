"""Bounded, unauthenticated static inspection of public GitHub source snapshots.

No checkout, archive extraction, dependency installation, or repository execution.
"""

import ast
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import PurePosixPath
import re
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .dependencies import DependencyParseError, changed_dependencies, is_dependency_file
from .result_explanation import explain_public_result

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_FILES = 300
MAX_MEMBERS = 30_000
MAX_FINDINGS = 50
MAX_DEPENDENCIES = 500
MAX_SECONDS = 90
HTTP_TIMEOUT = 15
MAX_REPOSITORIES = 300
REPOSITORY_PAGE_SIZE = 100
REPOSITORY_LIST_SECONDS = 8
_ALLOWED_HOSTS = {"api.github.com", "codeload.github.com"}
_IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "vendor", "dist", "build", ".next", "__pycache__", "coverage"}
_SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
PANDAS_SOURCE = "https://pandas.pydata.org/docs/whatsnew/v3.0.0.html#enforced-deprecations"
PYDANTIC_SOURCE = "https://docs.pydantic.dev/latest/migration/#required-optional-and-nullable-fields"
WEB_VITALS_SOURCE = "https://github.com/GoogleChrome/web-vitals/blob/main/docs/upgrading-to-v4.md"


class PublicRepoError(ValueError):
    """A safe, user-facing public inspection failure."""


def normalize_owner(value: str) -> str:
    if not isinstance(value, str):
        raise PublicRepoError("Enter a GitHub username to load its public repositories.")
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", value):
        raise PublicRepoError("Enter a GitHub username, without a URL or repository name.")
    return value


def normalize_repository(value: str) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise PublicRepoError("Enter owner/repo or a public https://github.com/owner/repo URL.")
    value = value.strip()
    if value.startswith("https://"):
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise PublicRepoError("Enter a valid public GitHub repository URL.") from None
        if (parsed.netloc != "github.com" or parsed.username or parsed.password
                or "?" in value or "#" in value):
            raise PublicRepoError("Use a GitHub repository URL without credentials, query, or fragment.")
        value = parsed.path.removeprefix("/").removesuffix("/")
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if (len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1]) or parts[1] in {".", ".."}):
        raise PublicRepoError("Use owner/repo only; branch, file, and non-GitHub URLs are unsupported.")
    return "/".join(parts)


def _safe_url(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise PublicRepoError("GitHub returned an unsupported redirect destination.") from None
    if (parsed.scheme != "https" or parsed.netloc not in _ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.fragment):
        raise PublicRepoError("GitHub returned an unsupported redirect destination.")


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Budget:
    def __init__(self, cancelled=None, *, seconds=MAX_SECONDS, timeout_message=None):
        self.deadline = time.monotonic() + seconds
        self.cancelled = cancelled
        self.timeout_message = timeout_message or "Repository inspection exceeded the 90-second time limit. Try a smaller repository."

    def check(self):
        cancelled = self.cancelled
        if cancelled and (cancelled() if callable(cancelled) else cancelled.is_set()):
            raise PublicRepoError("Repository inspection was cancelled.")
        if time.monotonic() >= self.deadline:
            raise PublicRepoError(self.timeout_message)

    def timeout(self):
        self.check()
        return max(.1, min(HTTP_TIMEOUT, self.deadline - time.monotonic()))


def _fetch(url, limit, budget, *, not_found_message=None):
    _safe_url(url)
    # Explicit empty proxy settings avoid ambient proxy credentials. No token,
    # cookie jar, .netrc, environment authorization, or authenticated gh process.
    opener = build_opener(ProxyHandler({}), _SafeRedirect())
    request = Request(url, headers={"User-Agent": "Secondlook-public-static-inspector", "Accept": "application/vnd.github+json"})
    try:
        with opener.open(request, timeout=budget.timeout()) as response:
            _safe_url(response.geturl())
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > limit:
                raise PublicRepoError("Repository download exceeds the size limit. Try a smaller repository.")
            data = bytearray()
            while True:
                budget.check()
                # read1 avoids accumulating a full block across many slow reads.
                read = getattr(response, "read1", response.read)
                chunk = read(min(64 * 1024, limit - len(data) + 1))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise PublicRepoError("Repository download exceeds the size limit. Try a smaller repository.")
            budget.check()
            return bytes(data)
    except HTTPError as error:
        if error.code == 404:
            message = not_found_message or "Public repository not found. Check the name; private repositories are not supported."
        elif error.code in (403, 429):
            message = "GitHub refused this unauthenticated request, possibly due to its public API rate limit. Try again later."
        elif error.code in (409, 422):
            message = "This repository has no readable default-branch commit."
        else:
            message = f"GitHub returned HTTP {error.code}. Try again later."
        raise PublicRepoError(message) from None
    except (URLError, TimeoutError, OSError):
        raise PublicRepoError("Could not reach GitHub within the network timeout. Check connectivity and retry.") from None


def _json(url, budget):
    try:
        result = json.loads(_fetch(url, 1024 * 1024, budget))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError) as error:
        if isinstance(error, PublicRepoError):
            raise
        raise PublicRepoError("GitHub returned unreadable repository metadata.") from None


def list_public_repositories(owner, cancelled=None):
    """List a bounded set of publicly visible repositories owned by one account."""
    owner = normalize_owner(owner)
    budget = _Budget(cancelled, seconds=REPOSITORY_LIST_SECONDS,
                     timeout_message="Loading repositories exceeded the time limit. Please retry.")
    repositories = {}
    truncated = False
    pages = (MAX_REPOSITORIES + REPOSITORY_PAGE_SIZE - 1) // REPOSITORY_PAGE_SIZE
    for page in range(1, pages + 2):
        url = (f"https://api.github.com/users/{owner}/repos?type=owner&sort=full_name"
               f"&direction=asc&per_page={REPOSITORY_PAGE_SIZE}&page={page}")
        data = _fetch(url, 1024 * 1024, budget,
                      not_found_message="GitHub account not found. Check the username and try again.")
        try:
            entries = json.loads(data)
            if not isinstance(entries, list) or len(entries) > REPOSITORY_PAGE_SIZE:
                raise ValueError
            # Probe one page beyond the cap so an exactly full list is not
            # incorrectly described as truncated. Never trust remote next URLs.
            if page > pages:
                truncated = bool(entries)
                break
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("private") is not False:
                    raise ValueError
                full_name = entry.get("full_name")
                repository = normalize_repository(full_name)
                if repository != full_name or repository.split("/")[0].lower() != owner.lower():
                    raise ValueError
                description = entry.get("description")
                description = re.sub(r"[\x00-\x1f\x7f]", "", description)[:500] if isinstance(description, str) else ""
                repositories[repository.lower()] = {
                    "fullName": repository, "url": "https://github.com/" + repository,
                    "description": description,
                }
        except (ValueError, UnicodeError, RecursionError):
            raise PublicRepoError("GitHub returned unreadable repository listings. Please retry.") from None
        budget.check()
        if len(entries) < REPOSITORY_PAGE_SIZE:
            break
    rows = sorted(repositories.values(), key=lambda item: item["fullName"].lower())
    truncated = truncated or len(rows) > MAX_REPOSITORIES
    return {"owner": owner, "repositories": rows[:MAX_REPOSITORIES], "truncated": truncated,
            "warning": (f"Showing up to {MAX_REPOSITORIES} public repositories. Enter a repository URL to check one that is not listed."
                        if truncated else None)}


class _LimitedReader:
    def __init__(self, stream, budget):
        self.stream, self.budget, self.read_bytes = stream, budget, 0

    def read(self, size=-1):
        self.budget.check()
        available = MAX_EXPANDED_BYTES - self.read_bytes + 1
        data = self.stream.read(min(size, available) if size >= 0 else available)
        self.read_bytes += len(data)
        if self.read_bytes > MAX_EXPANDED_BYTES:
            raise PublicRepoError("Expanded repository archive exceeds the 50 MB limit. Try a smaller repository.")
        return data


def _warn(warnings, message):
    if message not in warnings and len(warnings) < 30:
        warnings.append(message)


def _manifest(path):
    name = PurePosixPath(path).name.lower()
    return name in {"package.json", "pyproject.toml"} or (name.endswith(".txt") and is_dependency_file(path))


def _archive_files(data, budget, warnings):
    if len(data) > MAX_ARCHIVE_BYTES:
        raise PublicRepoError("Repository download exceeds the 20 MB size limit.")
    files, total, root = {}, 0, None
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            with tarfile.open(fileobj=_LimitedReader(compressed, budget), mode="r|") as archive:
                for index, member in enumerate(archive):
                    budget.check()
                    if index >= MAX_MEMBERS:
                        _warn(warnings, "Archive entry limit reached; inspection is partial.")
                        break
                    raw = member.name.rstrip("/") if member.isdir() else member.name
                    parts = raw.split("/")
                    if (raw.startswith("/") or "\\" in raw or any(part in {"", ".", ".."} for part in parts)
                            or any(ord(char) < 32 for char in raw) or len(raw) > 1000):
                        _warn(warnings, "Unsafe archive paths were ignored.")
                        continue
                    if root is None:
                        root = parts[0]
                    if parts[0] != root:
                        _warn(warnings, "Archive entries outside the snapshot root were ignored.")
                        continue
                    if member.isdir() or len(parts) < 2:
                        continue
                    if not member.isfile():
                        _warn(warnings, "Archive links and special files were ignored.")
                        continue
                    path = "/".join(parts[1:])
                    if any(part in _IGNORED_DIRS for part in parts[1:-1]):
                        continue
                    supported = _manifest(path)
                    if is_dependency_file(path) and not supported:
                        _warn(warnings, "Lockfiles are not parsed by this inspector; dependency versions are manifest declarations only.")
                    if not supported and PurePosixPath(path).suffix.lower() not in _SOURCE_SUFFIXES:
                        continue
                    if member.size > MAX_SOURCE_BYTES:
                        _warn(warnings, "Source or manifest files above 128 KB were skipped; inspection is partial.")
                        continue
                    if len(files) >= MAX_FILES or total + member.size > MAX_TEXT_BYTES:
                        _warn(warnings, "The 300-file / 2 MB text limit was reached; inspection is partial.")
                        break
                    if path in files:
                        _warn(warnings, "Duplicate archive paths were ignored.")
                        continue
                    handle = archive.extractfile(member)  # Reads bytes; never writes or extracts a path.
                    if handle is None:
                        continue
                    body = handle.read(MAX_SOURCE_BYTES + 1)
                    if len(body) != member.size:
                        raise PublicRepoError("Repository archive contained an incomplete source file.")
                    total += len(body)
                    try:
                        text = body.decode("utf-8-sig")
                        if "\x00" in text:
                            raise UnicodeError
                    except UnicodeError:
                        _warn(warnings, "Non-UTF-8 or binary source files were skipped.")
                        continue
                    files[path] = text
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError):
        raise PublicRepoError("GitHub returned an unreadable or incomplete repository archive.") from None
    return files


def _finding(rule, package, path, line, title, before, after, explanation, source):
    return {"id": f"{rule}:{path}:{line}", "status": "static_unverified", "title": title,
            "package": package, "file": path, "line": line, "beforeCode": before[:1000],
            "afterCode": after[:1000], "explanation": explanation, "sourceUrl": source}


def _bindings(tree):
    bindings = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                bindings.pop(name, None)
                if alias.name in {"pandas", "pydantic", "typing"}:
                    bindings[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bindings.pop(alias.asname or alias.name, None)
                if alias.name != "*" and node.module in {"pandas", "pydantic", "typing"} and node.level == 0:
                    bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    # Conservative: suppress aliases shadowed anywhere in this module rather
    # than pretend to resolve full Python scope, inheritance, or runtime imports.
    shadowed = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    shadowed |= {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
    shadowed |= {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body:
            shadowed.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return {name: value for name, value in bindings.items() if name not in shadowed}


def _qualified(node, bindings):
    if isinstance(node, ast.Name):
        return bindings.get(node.id, "")
    if isinstance(node, ast.Attribute):
        prefix = _qualified(node.value, bindings)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def _nullable(node, bindings):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except (SyntaxError, ValueError):
            return False
    if isinstance(node, ast.Subscript):
        name = _qualified(node.value, bindings)
        if name == "typing.Optional":
            return True
        if name == "typing.Union" and isinstance(node.slice, ast.Tuple):
            return any(isinstance(item, ast.Constant) and item.value is None for item in node.slice.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return any(isinstance(item, ast.Constant) and item.value is None for item in (node.left, node.right))
    return False


def _python_findings(path, text, warnings):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        _warn(warnings, f"Python syntax could not be parsed in {path}; its static checks were skipped.")
        return []
    bindings, findings = _bindings(tree), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _qualified(node.func, bindings) in {"pandas.date_range", "pandas.timedelta_range"}:
            for keyword in node.keywords:
                if (keyword.arg == "freq" and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str) and re.fullmatch(r"(?:[1-9]\d*)?H", keyword.value.value)):
                    before = ast.get_source_segment(text, keyword) or "freq='H'"
                    findings.append(_finding("pandas-hour", "pandas", path, keyword.lineno,
                        "Hourly frequency needs review before pandas 3", before,
                        f"freq={keyword.value.value.lower()!r}",
                        "Static, unverified: this imported pandas call uses the uppercase H hourly alias removed in pandas 3.0. "
                        "Lowercase h is the migration candidate. Check the actual installed version and run this call before changing it.", PANDAS_SOURCE))
        if isinstance(node, ast.ClassDef) and any(_qualified(base, bindings) == "pydantic.BaseModel" for base in node.bases):
            for field in node.body:
                if (isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
                        and not field.target.id.startswith("_") and field.value is None and _nullable(field.annotation, bindings)):
                    before = ast.get_source_segment(text, field) or ""
                    findings.append(_finding("pydantic-optional", "pydantic", path, field.lineno,
                        "Review whether this nullable field may be omitted", before, before + " = None",
                        "Static, unverified: Pydantic v2 requires nullable fields without defaults. If callers should be allowed to omit this field, "
                        "an explicit None default is a migration candidate. Required-but-nullable may be intentional; no caller behavior was tested.", PYDANTIC_SOURCE))
    return findings


# Strip comments while preserving offsets and quoted strings. JavaScript checks
# intentionally cover only explicit imports/destructuring, not general dataflow.
_JS_TOKENS = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/")
_LEGACY = re.compile(r"\bget(?:CLS|FID|FCP|LCP|TTFB)\b")
_JS_IMPORTS = [
    re.compile(r"\bimport\s*\{([^}]{1,2000})\}\s*from\s*['\"]web-vitals['\"]"),
    re.compile(r"\b(?:const|let|var)\s*\{([^}]{1,2000})\}\s*=\s*require\(\s*['\"]web-vitals['\"]\s*\)"),
    re.compile(r"\bimport\(\s*['\"]web-vitals['\"]\s*\)\s*\.then\(\s*\(?\s*\{([^}]{1,2000})\}"),
]


def _javascript_findings(path, text):
    matches = list(_JS_TOKENS.finditer(text))
    comments = [(match.start(), match.end()) for match in matches if match.group().startswith(("//", "/*", "`"))]
    strings = [(match.start(), match.end()) for match in matches if match.group().startswith(("'", '"', "`"))]
    clean = list(text)
    for start, end in comments:
        clean[start:end] = ["\n" if char == "\n" else " " for char in text[start:end]]
    clean = "".join(clean)
    findings = []
    for pattern in _JS_IMPORTS:
        for match in pattern.finditer(clean):
            if any(start <= match.start() < end for start, end in strings):
                continue
            # Only imported property names, not a modern export locally aliased
            # to a getXXX name (e.g. import { onCLS as getCLS }).
            names = sorted({piece.strip().split()[0].split(":")[0] for piece in match.group(1).split(",") if piece.strip()})
            names = [name for name in names if _LEGACY.fullmatch(name)]
            if not names:
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(_finding("web-vitals-exports", "web-vitals", path, line,
                "Legacy web-vitals exports need a migration review", text[match.start():match.end()], "",
                f"Static, unverified: {', '.join(names)} are explicit web-vitals imports. Version 4 removed getXXX exports. "
                "Review the target version's onXXX API and its callers; FID/INP migration needs a metric decision, not a blind rename. "
                "No bundle or browser behavior was tested.", WEB_VITALS_SOURCE))
    return findings


def _scan_files(files, budget, warnings):
    dependencies, findings = [], []
    for path, text in files.items():
        budget.check()
        if _manifest(path):
            try:
                for row in changed_dependencies(path, None, text):
                    if len(dependencies) >= MAX_DEPENDENCIES:
                        _warn(warnings, "Dependency output limit reached; only the first 500 declarations are shown.")
                        break
                    if len(row["package"]) > 214 or len(row["version"] or "") > 512:
                        _warn(warnings, "Oversized dependency names or specifications were omitted from the report.")
                        continue
                    dependencies.append({"name": row["package"], "version": row["version"], "ecosystem": row["ecosystem"], "file": path})
            except DependencyParseError as error:
                _warn(warnings, f"Could not inspect dependencies in {path}: {error}")
    for path, text in files.items():
        budget.check()
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == ".py":
            found = _python_findings(path, text, warnings)
        elif suffix in _SOURCE_SUFFIXES:
            found = _javascript_findings(path, text)
        else:
            continue
        findings.extend(found[:MAX_FINDINGS - len(findings)])
        if len(findings) >= MAX_FINDINGS:
            _warn(warnings, "Finding limit reached; only the first 50 static findings are shown.")
            break
    return dependencies, findings


def inspect_public_repo(value, emit=None, cancelled=None) -> dict:
    """Inspect one public default-branch snapshot; emit accepts progress strings."""
    repository = normalize_repository(value)
    budget = _Budget(cancelled)
    say = emit if callable(emit) else lambda message: None
    say(f"Reading public repository metadata for {repository}…")
    metadata = _json(f"https://api.github.com/repos/{repository}", budget)
    if metadata.get("private") is not False or metadata.get("visibility", "public") != "public":
        raise PublicRepoError("Only publicly visible GitHub repositories are supported.")
    canonical = metadata.get("full_name", repository)
    repository = normalize_repository(canonical)
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > 255:
        raise PublicRepoError("This repository has no readable default branch.")
    commit_data = _json(f"https://api.github.com/repos/{repository}/commits/{quote(branch, safe='')}", budget)
    commit = commit_data.get("sha", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise PublicRepoError("GitHub returned no valid commit for the default branch.")
    say(f"Reading source at commit {commit[:7]} (20 MB download limit)…")
    data = _fetch(f"https://codeload.github.com/{repository}/tar.gz/{commit}", MAX_ARCHIVE_BYTES, budget)
    warnings = []
    files = _archive_files(data, budget, warnings)
    say(f"Checking {len(files)} source and manifest files for three supported migration patterns…")
    dependencies, findings = _scan_files(files, budget, warnings)
    if not dependencies:
        _warn(warnings, "No supported registry dependency declarations were found; installed versions are unknown.")
    scope = ("Static, unverified inspection of one public default-branch commit. No repository code, tests, installs, or AI services were run. "
             "Checks cover imported pandas date_range/timedelta_range freq='H', directly imported Pydantic BaseModel nullable fields without defaults, "
             "and explicit legacy web-vitals imports/destructuring. Dependency declarations come only from package.json, requirements text files, and supported "
             "PEP 621 pyproject.toml fields; lockfiles, setup.py, notebooks, dynamic imports, indirect inheritance, and general API compatibility are not analyzed. "
             "Local/git/URL dependencies are omitted. At most 300 UTF-8 files, 128 KB each, 2 MB total text, 20 MB compressed and 50 MB expanded archive are inspected. "
             "No findings does not establish upgrade compatibility. Proposed changes are suggestions only and are not applied.")
    budget.check()
    result = {"repository": repository, "repoUrl": f"https://github.com/{repository}", "commit": commit,
            "checkedAt": datetime.now(timezone.utc).isoformat(), "dependencies": dependencies,
            "findings": findings, "filesScanned": len(files), "scope": scope, "warnings": warnings}
    result["explanation"] = explain_public_result(result)
    return result
