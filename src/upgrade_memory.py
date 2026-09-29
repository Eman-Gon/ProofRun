"""Store and retrieve measured upgrade evidence in isolated Cognee datasets.

The index locates datasets; it never substitutes for a Cognee search result.
CHUNKS/only_context and the per-dataset result shape follow installed Cognee 1.6.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .cognee_cloud import CogneeCloudClient
from .memory import Memory, _require_completed


PROJECT = "secondlook/upgrade-demo"
INDEX_NAME = "upgrade-memory.json"
DATASET_PREFIX = "secondlook_compatibility_"
MAX_DOCUMENT_BYTES = 6_000
MAX_INDEX_BYTES = 65_536
MAX_ENTRIES = 50
MAX_RECALL = 3
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DATASET = re.compile(DATASET_PREFIX + r"[0-9a-f]{32}\Z")
_FRAME = re.compile(
    r"SECONDLOOK_VERIFIED_EVIDENCE_V1 ([0-9a-f]{64})\n"
    r"([^\n]+)\nSECONDLOOK_VERIFIED_EVIDENCE_END \1"
)
_OUTCOMES = {
    "existing_old": "pass", "existing_new": "pass", "probe_old": "pass",
    "probe_new": "fail", "fixed_old": "pass", "fixed_new": "pass",
}


class UpgradeMemoryError(RuntimeError):
    """Safe public error; never includes provider responses or credentials."""


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid evidence text")
    return value


def _hash(value) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Invalid evidence hash")
    return value


def _timestamp(value) -> str:
    _text(value, 64)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps require a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _upgrade(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"ecosystem", "package", "before", "version"}:
        raise ValueError("Invalid upgrade identity")
    if value["ecosystem"] != "pypi" or value["package"] != "pydantic":
        raise ValueError("This memory records the Pydantic demo")
    for key in ("before", "version"):
        if not isinstance(value[key], str) or not re.fullmatch(r"==[0-9]+(?:\.[0-9]+){1,3}", value[key]):
            raise ValueError("Exact versions required")
    if value["before"] == value["version"]:
        raise ValueError("Distinct versions required")
    return dict(value)


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _source(value: dict) -> dict:
    url = _text(value["source_url"], 500)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError("Source must be public credential-free HTTPS")
    quote = _text(value["evidence_quote"], 400)
    normalized = lambda text: " ".join(text.replace("`", "").split())
    if len(normalized(quote)) < 12 or normalized(quote) not in normalized(_text(value["text"], 20_000)):
        raise ValueError("Source quotation is unsupported")
    result = {
        "source_url": url, "title": _text(value["title"], 240),
        "evidence_quote": quote, "summary": _text(value["summary"], 600),
        "fetched_at": _timestamp(value["fetched_at"]),
        "provenance": _text(value["provenance"], 200),
    }
    if value.get("warning") is not None:
        result["warning"] = _text(value["warning"], 300)
    if value.get("provider") is not None:
        result["provider"] = _text(value["provider"], 100)
    if value.get("content_sha256") is not None:
        result["content_sha256"] = _hash(value["content_sha256"])
    return result


def _evidence(report: dict) -> dict:
    upgrade = _upgrade(report["upgrade"])
    results = report["results"]
    if not isinstance(results, dict) or set(results) != set(_OUTCOMES):
        raise ValueError("Incomplete comparison")
    observations = {}
    for name, status in _OUTCOMES.items():
        item = results[name]
        output = _text(item["output_tail"], 65_536)
        version = upgrade["before" if name.endswith("old") else "version"][2:]
        markers = re.findall(r"^SECONDLOOK_DEPENDENCY_VERSION=([^\s]+)$", output, re.M)
        expected_code = 1 if status == "fail" else 0
        if (item["status"] != status or type(item["exit_code"]) is not int
                or item["exit_code"] != expected_code or markers != [version]):
            raise ValueError("Comparison was not verified")
        observations[name] = {
            "status": status, "exit_code": expected_code, "dependency_version": version,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        }
    failure = results["probe_new"]["output_tail"]
    if not all(term in failure for term in ("ValidationError", "nickname", "Field required")):
        raise ValueError("Expected validation failure missing")
    usage = report["usage"]
    if type(usage["line"]) is not int or usage["line"] < 1:
        raise ValueError("Invalid source location")
    plan = report["plan"]
    row, expected = plan["input_row"], plan["expected_row"]
    if (not isinstance(row, dict) or set(row) != {"name"}
            or expected != {"name": _text(row["name"], 64), "nickname": None}):
        raise ValueError("Unsupported probe contract")
    images = report["images"]
    if set(images) != {"old", "new"}:
        raise ValueError("Two immutable images required")
    for image in images.values():
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Immutable image ID required")
    fact = {
        "schema_version": 1, "project": PROJECT, "status": "confirmed_break",
        "upgrade": upgrade, "checked_at": _timestamp(report["checked_at"]),
        "app_sha256": _hash(report["app_sha256"]),
        "test_sha256": _hash(report["test_sha256"]),
        "fixed_app_sha256": _hash(report["fixed_app_sha256"]),
        "source": _source(report["source"]),
        "usage": {"path": _text(usage["path"], 200), "line": usage["line"],
                  "symbol": _text(usage["symbol"], 100), "code": _text(usage["code"], 300)},
        "probe": {"input_row": dict(row), "expected_row": dict(expected)},
        "observations": observations, "failure_excerpt": failure[-1_000:],
        "fix_patch": _text(report["fix_patch"], 2_000), "images": dict(images),
        "scope": "Only this app snapshot, version pair, and supplied tests were verified; recalled evidence does not replace a new comparison.",
    }
    return fact


def _entry(fact: dict, dataset: str) -> dict:
    if not isinstance(dataset, str) or not _DATASET.fullmatch(dataset):
        raise ValueError("Invalid dataset")
    return {"dataset": dataset, "evidence_sha256": _digest(fact),
            "upgrade": _upgrade(fact["upgrade"]), "app_sha256": _hash(fact["app_sha256"]),
            "checked_at": _timestamp(fact["checked_at"])}


def _read_index(state_dir: Path, *, cloud: bool = False) -> list[dict]:
    path = state_dir / INDEX_NAME
    if path.is_symlink():
        raise ValueError("Memory index must be a local regular file")
    try:
        if path.stat().st_size > MAX_INDEX_BYTES:
            raise ValueError("Memory index too large")
        index = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return []
    keys = {"schema_version", "project", "entries"} | ({"backend"} if cloud else set())
    if (not isinstance(index, dict) or set(index) != keys
            or type(index["schema_version"]) is not int or index["schema_version"] != 1
            or index["project"] != PROJECT or not isinstance(index["entries"], list)
            or len(index["entries"]) > MAX_ENTRIES
            or (cloud and index["backend"] != "cloud")):
        raise ValueError("Invalid memory index")
    datasets, hashes, dataset_ids = set(), set(), set()
    for entry in index["entries"]:
        entry_keys = {"dataset", "evidence_sha256", "upgrade", "app_sha256", "checked_at"}
        if cloud:
            entry_keys.add("dataset_id")
        if (not isinstance(entry, dict) or set(entry) != entry_keys
                or not isinstance(entry["dataset"], str) or not _DATASET.fullmatch(entry["dataset"])):
            raise ValueError("Invalid memory index entry")
        _upgrade(entry["upgrade"])
        _hash(entry["app_sha256"])
        _hash(entry["evidence_sha256"])
        _timestamp(entry["checked_at"])
        if cloud:
            dataset_id = _dataset_id(entry["dataset_id"])
            if dataset_id in dataset_ids:
                raise ValueError("Duplicate Cloud dataset")
            dataset_ids.add(dataset_id)
        if entry["dataset"] in datasets or entry["evidence_sha256"] in hashes:
            raise ValueError("Duplicate memory entry")
        datasets.add(entry["dataset"])
        hashes.add(entry["evidence_sha256"])
    return index["entries"]


def _document(fact: dict, digest: str) -> str:
    text = (f"SECONDLOOK_VERIFIED_EVIDENCE_V1 {digest}\n{_canonical(fact)}\n"
            f"SECONDLOOK_VERIFIED_EVIDENCE_END {digest}")
    if len(text.encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError("Compatibility evidence exceeds the document budget")
    return text


async def _retrieve(cognee, entry: dict) -> dict:
    search_type = importlib.import_module("cognee.api.v1.search").SearchType
    results = await cognee.search(
        query_text=(f"Verified {entry['upgrade']['package']} upgrade behavior and fix "
                    f"{entry['evidence_sha256']}"),
        query_type=search_type.CHUNKS, datasets=[entry["dataset"]], top_k=6, only_context=True,
    )
    return _verify_retrieval(results, entry)


def _verify_retrieval(results, entry: dict) -> dict:
    """Verify retrieved content, independently of the storage transport."""
    expected_entry = {key: entry[key] for key in (
        "dataset", "evidence_sha256", "upgrade", "app_sha256", "checked_at",
    )}
    if not isinstance(results, list) or not 1 <= len(results) <= 6:
        raise ValueError("No verified retrieval")
    retrieved, total = [], 0
    for item in results:
        if (not isinstance(item, dict) or item.get("dataset_name") != entry["dataset"]
                or item.get("error") is not None):
            raise ValueError("Mismatched retrieval dataset")
        text = _text(item.get("search_result"), 48_000)
        total += len(text.encode())
        if total > 48_000:
            raise ValueError("Retrieval too large")
        frames = list(_FRAME.finditer(text))
        if not frames:
            raise ValueError("Evidence marker missing from retrieval")
        for frame in frames:
            digest, body = frame.groups()
            fact = json.loads(body, object_pairs_hook=_unique_object)
            if (not isinstance(fact, dict) or fact.get("schema_version") != 1
                    or fact.get("project") != PROJECT or fact.get("status") != "confirmed_break"
                    or digest != entry["evidence_sha256"]
                    or _entry(fact, entry["dataset"]) != expected_entry
                    or len(frame.group(0).encode()) > MAX_DOCUMENT_BYTES):
                raise ValueError("Retrieved evidence does not match the published record")
            retrieved.append((fact, frame.group(0)))
    fact, excerpt = retrieved[0]
    if any(other != fact for other, _ in retrieved[1:]):
        raise ValueError("Conflicting retrieved evidence")
    return {"status": "recalled", "dataset": entry["dataset"],
            "evidence_sha256": entry["evidence_sha256"], "evidence": fact,
            "retrieved_excerpt": excerpt, "retrieved_at": datetime.now(timezone.utc).isoformat()}


def _publish(state_dir: Path, entry: dict, *, cloud: bool = False) -> None:
    entries = [item for item in _read_index(state_dir, cloud=cloud)
               if item["evidence_sha256"] != entry["evidence_sha256"]]
    entries.append(entry)
    entries.sort(key=lambda item: datetime.fromisoformat(item["checked_at"]), reverse=True)
    index = {"schema_version": 1, "project": PROJECT, "entries": entries[:MAX_ENTRIES]}
    if cloud:
        index["backend"] = "cloud"
    content = _canonical(index) + "\n"
    if len(content.encode()) > MAX_INDEX_BYTES:
        raise ValueError("Index exceeds size limit")
    temporary = state_dir / (INDEX_NAME + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(content)
        temporary.replace(state_dir / INDEX_NAME)
    finally:
        temporary.unlink(missing_ok=True)


def _backend() -> str:
    backend = os.environ.get("COGNEE_MEMORY_BACKEND", "local")
    if backend not in {"local", "cloud"}:
        raise ValueError("Unsupported Cognee memory backend")
    return backend


def _dataset_id(value) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid Cloud dataset UUID")
    return value


def _cloud_state(state_dir: Path, client) -> Path:
    # A different endpoint or tenant must never reuse local/other-tenant names.
    identity = _canonical({"base_url": client.base_url,
                           "tenant_id": os.environ.get("COGNEE_TENANT_ID") or ""})
    root = state_dir / "upgrade-cloud"
    scoped = root / hashlib.sha256(identity.encode()).hexdigest()
    if root.is_symlink() or scoped.is_symlink():
        raise ValueError("Cloud memory scope must be a local directory")
    return scoped


async def _cloud_retrieve(client, entry: dict) -> dict:
    results = await asyncio.to_thread(
        client.search,
        f"Verified {entry['upgrade']['package']} upgrade behavior and fix {entry['evidence_sha256']}",
        entry["dataset_id"], entry["dataset"],
    )
    retrieved = _verify_retrieval(results, entry)
    summary = await asyncio.to_thread(client.graph_summary, entry["dataset_id"])
    if (not isinstance(summary, dict) or summary.get("dataset_id") != entry["dataset_id"]
            or any(type(summary.get(key)) is not int or summary[key] <= 0
                   for key in ("num_nodes", "num_edges"))):
        raise ValueError("Cloud graph was not verified")
    return dict(retrieved, backend="cloud", dataset_id=entry["dataset_id"], graph_summary=summary)


async def _cloud_store(fact: dict, state_dir: Path) -> dict:
    client = CogneeCloudClient.from_env()
    try:
        scoped = _cloud_state(state_dir, client)
        _read_index(scoped, cloud=True)
        dataset = DATASET_PREFIX + uuid4().hex
        entry = _entry(fact, dataset)
        document = _document(fact, entry["evidence_sha256"])
        added = await asyncio.to_thread(client.add, document, dataset)
        if not isinstance(added, dict) or added.get("name") != dataset:
            raise ValueError("Cloud ingestion dataset mismatch")
        entry["dataset_id"] = _dataset_id(added.get("id"))
        await asyncio.to_thread(client.cognify, entry["dataset_id"])
        if await asyncio.to_thread(client.wait_completed, entry["dataset_id"]) != "completed":
            raise ValueError("Cloud processing did not complete")
        retrieved = await _cloud_retrieve(client, entry)
    finally:
        await asyncio.to_thread(client.close)
    scoped.mkdir(parents=True, exist_ok=True)
    _publish(scoped, entry, cloud=True)
    return dict(retrieved, status="stored_and_retrieved")


async def _cloud_recall(upgrade: dict, app_sha256: str, state_dir: Path) -> dict:
    client = CogneeCloudClient.from_env()
    try:
        entries = [entry for entry in _read_index(_cloud_state(state_dir, client), cloud=True)
                   if entry["upgrade"] == upgrade and entry["app_sha256"] == app_sha256]
        entries.sort(key=lambda item: datetime.fromisoformat(item["checked_at"]), reverse=True)
        if not entries:
            return {"status": "not_found", "matches": [], "backend": "cloud"}
        matches = [await _cloud_retrieve(client, entry) for entry in entries[:MAX_RECALL]]
        return {"status": "recalled", "matches": matches, "backend": "cloud"}
    finally:
        await asyncio.to_thread(client.close)


async def remember_and_recall(report: dict, state_dir: Path) -> dict:
    """Index a confirmed finding only after Cognee returns its exact evidence."""
    try:
        backend = _backend()
        if isinstance(report, dict) and report.get("status") != "confirmed_break":
            return {"status": "not_stored", "reason": "Only confirmed comparisons are stored."}
        state_dir = Path(state_dir).resolve()
        if backend == "cloud":
            return await _cloud_store(_evidence(report), state_dir)
        _read_index(state_dir)
        fact = _evidence(report)
        dataset = DATASET_PREFIX + uuid4().hex
        entry = _entry(fact, dataset)
        document = _document(fact, entry["evidence_sha256"])
        cognee = Memory(PROJECT, state_dir)._cognee(require_key=True)
        added = await cognee.add([document], dataset_name=dataset)
        _require_completed(added, dataset, "compatibility add")
        # ASCII-escaped documents are capped below 6,000 bytes. Explicitly keep
        # them within one 8,192-token TextChunker chunk, preserving the full
        # marker/body/end frame instead of relying on model-derived defaults.
        processed = await cognee.cognify(datasets=[dataset], run_in_background=False, chunk_size=8192)
        _require_completed(processed, dataset, "compatibility cognify")
        retrieved = await _retrieve(cognee, entry)
        _publish(state_dir, entry)
        return dict(retrieved, status="stored_and_retrieved")
    except Exception:
        raise UpgradeMemoryError(
            "Compatibility memory could not be stored and verified; no new index entry was published."
        ) from None


async def recall_prior(upgrade: dict, app_sha256: str, state_dir: Path) -> dict:
    """Read at most three prior exact-scope findings through Cognee, not the index."""
    try:
        backend = _backend()
        upgrade, app_sha256 = _upgrade(upgrade), _hash(app_sha256)
        state_dir = Path(state_dir).resolve()
        if backend == "cloud":
            return await _cloud_recall(upgrade, app_sha256, state_dir)
        entries = [entry for entry in _read_index(state_dir)
                   if entry["upgrade"] == upgrade and entry["app_sha256"] == app_sha256]
        entries.sort(key=lambda item: datetime.fromisoformat(item["checked_at"]), reverse=True)
        if not entries:
            return {"status": "not_found", "matches": []}
        cognee = Memory(PROJECT, state_dir)._cognee()
        matches = [await _retrieve(cognee, entry) for entry in entries[:MAX_RECALL]]
        return {"status": "recalled", "matches": matches}
    except Exception:
        raise UpgradeMemoryError("Compatibility memory retrieval failed or returned mismatched evidence.") from None
