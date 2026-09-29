"""Bounded upstream enrichment; its failures never hide the core review."""

from datetime import datetime, timezone
import asyncio

from .brightdata import BrightDataClient, BrightDataError
from .dependencies import DependencyParseError, changed_dependencies, is_dependency_file
from .github_fetch import GitHubError

MAX_MANIFESTS = 8
MAX_PACKAGES = 3
MEMORY_TIMEOUT = 20


def _is_lock(path: str) -> bool:
    return path.endswith(".lock") or path.rsplit("/", 1)[-1] in (
        "package-lock.json", "npm-shrinkwrap.json", "Pipfile.lock", "pnpm-lock.yaml",
    )


async def collect_dependency_context(github, commit, memory, api_key: str) -> dict | None:
    files = [f for f in commit.files if is_dependency_file(f.filename)
             or (f.previous_filename and is_dependency_file(f.previous_filename))]
    if not files:
        return None
    context = {"changes": [], "candidate_sources": [], "recalled_context": "", "warnings": [],
               "status": "unchanged", "memory_status": "not needed"}
    warnings = context["warnings"]
    if len(files) > MAX_MANIFESTS:
        warnings.append(f"Only {MAX_MANIFESTS} of {len(files)} dependency files were examined.")
    changes, direct = {}, set()
    # Prefer concrete lock versions when the same package also has a changed range.
    for file in sorted(files, key=lambda f: (not _is_lock(f.filename), f.filename))[:MAX_MANIFESTS]:
        old_path = file.previous_filename or file.filename
        try:
            before = (github.file_text(old_path, commit.parents[0])
                      if file.status != "added" and commit.parents else None)
            after = github.file_text(file.filename, commit.sha) if file.status != "removed" else None
            # A rename between dependency formats cannot be treated as an empty diff.
            if old_path != file.filename:
                removed = changed_dependencies(old_path, before, None) if is_dependency_file(old_path) else []
                added = changed_dependencies(file.filename, None, after) if is_dependency_file(file.filename) else []
                # Same-format renames compare contents, avoiding spurious package updates.
                if old_path.rsplit("/", 1)[-1] == file.filename.rsplit("/", 1)[-1]:
                    parsed = changed_dependencies(file.filename, before, after)
                else:
                    parsed = removed + added
            else:
                parsed = changed_dependencies(file.filename, before, after)
            for item in parsed:
                key = (item["ecosystem"], item["package"])
                if not _is_lock(file.filename):
                    direct.add(key)
                if key not in changes or (changes[key]["version"] is None and item["version"] is not None):
                    changes[key] = dict(item, file=file.filename)
        except (GitHubError, DependencyParseError, ValueError):
            warnings.append(f"Could not read or parse {file.filename}; dependency context is incomplete.")
    context["changes"] = sorted(changes.values(), key=lambda c: (c["ecosystem"], c["package"]))
    if not changes:
        context["status"] = "incomplete" if warnings else "unchanged"
        return context
    candidates = sorted((c for c in changes.values() if c["version"] is not None),
                        key=lambda c: ((c["ecosystem"], c["package"]) not in direct, c["package"]))
    if not candidates:
        context["status"] = "removals only"
        return context
    if len(candidates) > MAX_PACKAGES:
        warnings.append(f"Lookups limited to {MAX_PACKAGES} of {len(candidates)} added/changed packages.")
    selected = [{k: c[k] for k in ("package", "ecosystem", "version")} for c in candidates[:MAX_PACKAGES]]
    try:
        context["recalled_context"] = await asyncio.wait_for(memory.dependency_context(selected), MEMORY_TIMEOUT)
    except Exception:
        warnings.append("Stored dependency context could not be recalled.")
    if not api_key:
        context["status"] = "not configured"
        warnings.append("Set BRIGHTDATA_API_KEY for fresh upstream context; no web lookup was performed.")
        return context
    client = BrightDataClient(api_key)
    for package in selected:
        try:
            fetched = client.fetch_context(**package)
            context["candidate_sources"].append(dict(
                fetched, **package, source_id=f"source_{len(context['candidate_sources']) + 1}",
                fetched_at=datetime.now(timezone.utc).isoformat(),
            ))
        except BrightDataError:
            warnings.append(f"Upstream lookup unavailable for {package['package']} {package['version']}.")
        except Exception:
            warnings.append(f"Upstream lookup failed for {package['package']}; no finding is implied.")
    context["status"] = "fetched" if context["candidate_sources"] else "unavailable"
    return context


async def remember_selected_notes(memory, context: dict | None, judgment) -> None:
    if not context or not judgment.dependency_notes:
        return
    sources = {s["source_id"]: s for s in context["candidate_sources"]}
    notes = []
    for finding in judgment.dependency_notes:
        source = sources[finding.source_id]
        notes.append({
            **{key: source[key] for key in ("package", "ecosystem", "version", "source_url", "title", "fetched_at")},
            "text": f"{finding.summary}\nEvidence: {finding.evidence_quote}",
        })
    try:
        await asyncio.wait_for(memory.remember_dependency_context(notes), MEMORY_TIMEOUT)
        context["memory_status"] = "saved separately in Cognee"
    except Exception:
        context["memory_status"] = "not saved"
        context["warnings"].append("Selected dependency notes could not be saved; the review is still valid.")
