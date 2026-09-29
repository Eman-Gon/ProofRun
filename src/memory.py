"""Repo baseline memory in local Cognee; all model configuration is explicit."""

import importlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from src.github_fetch import Commit, validate_repo


class MemoryError(RuntimeError):
    pass


def _dependency_key(item: dict) -> tuple[str, str, str]:
    fields = tuple(item.get(field) for field in ("ecosystem", "package", "version"))
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        raise MemoryError("Dependency memory requires exact ecosystem, package, and version values.")
    return fields


def _fetched_time(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Timezone required")
        return timestamp.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise MemoryError("Dependency context requires a timestamp with a timezone.") from exc


def _require_completed(result, dataset: str, stage: str) -> None:
    # Cognee add can RETURN an errored run instead of raising. cognify returns
    # a dataset -> run mapping. Empty/background/partial results are not ready.
    runs = list(result.values()) if isinstance(result, dict) else [result]
    if not runs or any(
        getattr(run, "status", None) not in ("PipelineRunCompleted", "PipelineRunAlreadyCompleted")
        or getattr(run, "dataset_name", None) != dataset
        for run in runs
    ):
        raise MemoryError(f"Cognee {stage} did not complete; the previous baseline remains active.")


class Memory:
    def __init__(self, repo: str, state_dir: Path):
        self.repo = validate_repo(repo)
        self.state_dir = Path(state_dir).resolve()
        self.manifest_path = self.state_dir / "baseline.json"
        self.dependency_index_path = self.state_dir / "dependency-memory.json"
        repo_hash = hashlib.sha256(self.repo.encode()).hexdigest()[:16]
        self.dependency_namespace = f"commit_watch_deps_{repo_hash}_"

    def manifest(self) -> dict:
        """Read the last completely processed baseline, without importing Cognee."""
        try:
            manifest = json.loads(self.manifest_path.read_text())
        except FileNotFoundError as exc:
            raise MemoryError("No baseline found. Run ingest before checking a commit.") from exc
        except (OSError, ValueError) as exc:
            raise MemoryError("Cannot read baseline.json; run ingest to rebuild the baseline.") from exc
        valid = (
            isinstance(manifest, dict)
            and manifest.get("repo") == self.repo
            and isinstance(manifest.get("dataset"), str)
            and bool(manifest["dataset"])
            and isinstance(manifest.get("shas"), list)
            and 30 <= len(manifest["shas"]) <= 50
            and all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
                    for sha in manifest["shas"])
            and len(set(manifest["shas"])) == len(manifest["shas"])
            and manifest.get("head") == manifest["shas"][0]
        )
        if not valid:
            raise MemoryError("Baseline is invalid or belongs to another repo; run ingest again.")
        return manifest

    def _cognee(self, *, require_key: bool = False):
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if require_key and not key:
            raise MemoryError("Set GROQ_API_KEY in .env before ingesting a baseline.")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Set before import: Cognee caches its settings, including database roots.
        os.environ.update({
            "LLM_PROVIDER": "custom",
            "LLM_MODEL": "groq/openai/gpt-oss-120b",
            "LLM_API_KEY": key,
            "LLM_ENDPOINT": "https://api.groq.com/openai/v1",
            "STRUCTURED_OUTPUT_FRAMEWORK": "litellm_native",
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "EMBEDDING_DIMENSIONS": "384",
            "EMBEDDING_MAX_COMPLETION_TOKENS": "512",
            "FASTEMBED_CACHE_PATH": str(self.state_dir / "models"),
            "HF_HOME": str(self.state_dir / "huggingface"),
            "ENABLE_BACKEND_ACCESS_CONTROL": "true",
            "CACHING": "false",
            "SYSTEM_ROOT_DIRECTORY": str(self.state_dir / "system"),
            "DATA_ROOT_DIRECTORY": str(self.state_dir / "data"),
        })
        # Pace graph extraction for a small free-tier account; configurable in .env.
        os.environ.setdefault("LLM_RATE_LIMIT_ENABLED", "true")
        os.environ.setdefault("LLM_RATE_LIMIT_REQUESTS", "6")
        os.environ.setdefault("LLM_RATE_LIMIT_INTERVAL", "60")
        os.environ.setdefault("LOG_LEVEL", "ERROR")
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        try:
            return importlib.import_module("cognee")
        except ImportError as exc:
            raise MemoryError("Cognee is unavailable. Install requirements.txt first.") from exc

    async def ingest(self, commits: list[Commit]) -> dict:
        """Publish a new baseline only after every commit has been cognified."""
        shas = [commit.sha for commit in commits]
        if not 30 <= len(shas) <= 50 or len(set(shas)) != len(shas):
            raise MemoryError("A baseline requires 30–50 unique commits.")
        if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in shas):
            raise MemoryError("Baseline commits must have full GitHub commit SHAs.")
        cognee = self._cognee(require_key=True)
        dataset = f"commit_watch_{uuid4().hex}"
        added = await cognee.add([commit.memory_text() for commit in commits], dataset_name=dataset)
        _require_completed(added, dataset, "add")
        processed = await cognee.cognify(datasets=[dataset], run_in_background=False)
        _require_completed(processed, dataset, "cognify")
        manifest = {"repo": self.repo, "dataset": dataset, "head": shas[0], "shas": shas}
        temporary = self.manifest_path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(manifest, indent=2) + "\n")
            temporary.replace(self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest

    async def search(self, commit: Commit) -> str:
        manifest = self.manifest()
        if commit.sha in manifest["shas"]:
            raise MemoryError("This commit is already in the baseline; ingest an earlier ref first.")
        cognee = self._cognee()
        search_type = importlib.import_module("cognee.api.v1.search").SearchType
        query = (
            f"Usual commit sizes, file areas, tests, and change patterns for {self.repo}. "
            f"Author: {commit.author}. Message: {commit.message}. "
            f"Files: {', '.join(file.filename for file in commit.files)}"
        )
        results = await cognee.search(
            query_text=query,
            query_type=search_type.CHUNKS,
            datasets=[manifest["dataset"]],
            top_k=12,
            only_context=True,
        )
        # With access control enabled, Cognee returns dictionaries per dataset.
        # Do not stringify arbitrary payloads: missing memory must fail visibly.
        texts = [item["search_result"].strip() for item in results
                 if isinstance(item, dict)
                 and item.get("dataset_name") == manifest["dataset"]
                 and isinstance(item.get("search_result"), str)
                 and item["search_result"].strip()]
        if not texts:
            raise MemoryError("Cognee returned no baseline context; rerun ingest before checking.")
        return "\n\n".join(texts)

    def _dependency_index(self) -> list[dict]:
        try:
            index = json.loads(self.dependency_index_path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise MemoryError("Cannot read the optional dependency memory index.") from exc
        if (not isinstance(index, dict) or index.get("repo") != self.repo
                or not isinstance(index.get("entries"), list) or len(index["entries"]) > 100):
            raise MemoryError("Dependency memory index is invalid or belongs to another repo.")
        for entry in index["entries"]:
            if not isinstance(entry, dict):
                raise MemoryError("Dependency memory index contains an invalid entry.")
            _dependency_key(entry)
            _fetched_time(entry.get("fetched_at"))
            if (not isinstance(entry.get("dataset"), str)
                    or not entry["dataset"].startswith(self.dependency_namespace)
                    or not isinstance(entry.get("source_url"), str)
                    or not isinstance(entry.get("title"), str)):
                raise MemoryError("Dependency memory index contains invalid source metadata.")
        return index["entries"]

    async def remember_dependency_context(self, notes: list[dict]) -> None:
        """Retain sourced excerpts separately from the repo's commit baseline."""
        if not notes:
            return
        now = datetime.now(timezone.utc)
        entries = self._dependency_index()
        latest = {(*_dependency_key(entry), entry["source_url"]): entry for entry in entries}
        pending = {}
        for note in notes:
            key = _dependency_key(note)
            timestamp = _fetched_time(note.get("fetched_at"))
            source = note.get("source_url", "")
            url = urlsplit(source)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or not isinstance(note.get("title"), str) or not note["title"].strip()
                    or not isinstance(note.get("text"), str) or not note["text"].strip()):
                raise MemoryError("Dependency memory accepts only titled, sourced HTTPS excerpts.")
            if not now - timedelta(days=7) <= timestamp <= now:
                raise MemoryError("Dependency excerpts must have been fetched within the past seven days.")
            source_key = (*key, source)
            previous = pending.get(source_key) or latest.get(source_key)
            if previous and _fetched_time(previous["fetched_at"]) >= timestamp:
                continue
            # Keep only source material and provenance; exclude caller extras such
            # as judgments or test outcomes. Bound the stored excerpt and title.
            pending[source_key] = {
                **dict(zip(("ecosystem", "package", "version"), key)),
                "source_url": source, "title": note["title"].strip()[:300],
                "text": note["text"].strip()[:6000], "fetched_at": timestamp.isoformat(),
            }
        if not pending:
            return
        cognee = self._cognee(require_key=True)
        for source_key, note in pending.items():
            dataset = self.dependency_namespace + uuid4().hex
            document = "Sourced dependency context (not commit baseline evidence):\n" + json.dumps(
                {"repo": self.repo, **note}, ensure_ascii=False,
            )
            added = await cognee.add([document], dataset_name=dataset)
            _require_completed(added, dataset, "dependency add")
            processed = await cognee.cognify(datasets=[dataset], run_in_background=False)
            _require_completed(processed, dataset, "dependency cognify")
            latest[source_key] = {key: value for key, value in note.items() if key != "text"}
            latest[source_key]["dataset"] = dataset
        # An unsuccessful batch leaves the previous index active. New unindexed
        # datasets are never searched; no existing memory is pruned or modified.
        entries = sorted(latest.values(), key=lambda item: _fetched_time(item["fetched_at"]), reverse=True)[:100]
        temporary = self.dependency_index_path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps({"repo": self.repo, "entries": entries}, indent=2) + "\n")
            temporary.replace(self.dependency_index_path)
        finally:
            temporary.unlink(missing_ok=True)

    async def dependency_context(self, packages: list[dict]) -> str:
        """Retrieve only exact-version source notes fetched in the past week."""
        if not packages:
            return ""
        wanted = {_dependency_key(package) for package in packages}
        now = datetime.now(timezone.utc)
        entries = [entry for entry in self._dependency_index()
                   if _dependency_key(entry) in wanted
                   and now - timedelta(days=7) <= _fetched_time(entry["fetched_at"]) <= now]
        if not entries:
            return ""
        # A long-lived repo can accumulate many URLs for one package. Recall at
        # most two newest sources per package/version, six total for this check.
        selected, counts = [], {}
        for entry in sorted(entries, key=lambda item: _fetched_time(item["fetched_at"]), reverse=True):
            key = _dependency_key(entry)
            if counts.get(key, 0) < 2 and len(selected) < 6:
                selected.append(entry)
                counts[key] = counts.get(key, 0) + 1
        entries = selected
        cognee = self._cognee()
        search_type = importlib.import_module("cognee.api.v1.search").SearchType
        contexts = []
        for entry in entries:
            results = await cognee.search(
                query_text=f"{entry['ecosystem']} {entry['package']} {entry['version']} release changes and issues",
                query_type=search_type.CHUNKS, datasets=[entry["dataset"]], top_k=3, only_context=True,
            )
            for item in results:
                if (isinstance(item, dict) and item.get("dataset_name") == entry["dataset"]
                        and isinstance(item.get("search_result"), str) and item["search_result"].strip()):
                    contexts.append(json.dumps({
                        "repo": self.repo,
                        **{key: value for key, value in entry.items() if key != "dataset"},
                        "text": item["search_result"].strip()[:2000],
                    }, ensure_ascii=False))
        return "\n\n".join(contexts)
