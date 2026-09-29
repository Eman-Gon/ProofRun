"""Small, bounded Cognee Cloud client for verified upgrade evidence.

Wire names follow Cognee 1.6's installed REST DTOs. This client does not import
the local Cognee runtime, upload local databases, or treat an accepted job as a
completed graph. Provider bodies and credentials never become error messages.
"""

import hashlib
import json
import math
import os
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

import requests
from urllib3.util import Timeout


MAX_RESPONSE_BYTES = 131_072
MAX_TEXT_BYTES = 6_000
MAX_SEARCH_BYTES = 48_000
_TENANT_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.aws\.cognee\.ai\Z")
_DATASET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_STATUS = {
    "DATASET_PROCESSING_INITIATED": "pending", "pending": "pending",
    "DATASET_PROCESSING_STARTED": "running", "running": "running",
    "DATASET_PROCESSING_COMPLETED": "completed", "completed": "completed",
    "DATASET_PROCESSING_ERRORED": "errored", "errored": "errored", "failed": "errored",
}


class CogneeCloudError(RuntimeError):
    """A safe public error, containing no remote body, key, or request object."""


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value.lower():
            raise ValueError
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise CogneeCloudError("Cognee Cloud requires a canonical dataset UUID.") from None


def _name(value):
    if not isinstance(value, str) or not _DATASET_NAME.fullmatch(value):
        raise CogneeCloudError("Cognee Cloud requires a valid dataset name.")
    return value


def _field(item, snake, camel):
    if snake in item and camel in item and item[snake] != item[camel]:
        raise CogneeCloudError("Cognee Cloud returned conflicting response fields.")
    return item.get(snake, item.get(camel))


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError


def _graph_counts(graph):
    """Count only a complete, validated GraphDTO from the scoped graph route."""
    if (not isinstance(graph, dict) or set(graph) != {"nodes", "edges"}
            or not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list)):
        raise CogneeCloudError("Cognee Cloud returned an invalid dataset graph.")
    node_ids = set()
    for node in graph["nodes"]:
        if (not isinstance(node, dict) or not isinstance(node.get("label"), str)
                or not isinstance(node.get("type"), str) or not isinstance(node.get("properties"), dict)):
            raise CogneeCloudError("Cognee Cloud returned an invalid graph node.")
        node_id = _uuid(node.get("id"))
        if node_id in node_ids:
            raise CogneeCloudError("Cognee Cloud returned duplicate graph nodes.")
        node_ids.add(node_id)
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or not isinstance(edge.get("label"), str):
            raise CogneeCloudError("Cognee Cloud returned an invalid graph edge.")
        if _uuid(edge.get("source")) not in node_ids or _uuid(edge.get("target")) not in node_ids:
            raise CogneeCloudError("Cognee Cloud returned an incomplete dataset graph.")
    return len(node_ids), len(graph["edges"])


class CogneeCloudClient:
    """Synchronous API; callers may use asyncio.to_thread for UI/async code."""

    def __init__(self, base_url: str, api_key: str, tenant_id: str | None = None):
        try:
            parsed = urlsplit(base_url)
            valid = (isinstance(base_url, str) and parsed.scheme == "https"
                     and _TENANT_HOST.fullmatch(parsed.hostname or "")
                     and not parsed.username and not parsed.password and parsed.port is None
                     and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise CogneeCloudError("Set COGNEE_API_URL to the HTTPS tenant URL shown in Cognee Cloud.")
        if (not isinstance(api_key, str) or not 1 <= len(api_key) <= 4_096
                or not api_key.isascii() or any(character.isspace() or ord(character) < 33 for character in api_key)):
            raise CogneeCloudError("Set COGNEE_API_KEY to a Cognee Cloud API key.")
        if tenant_id is not None and (not isinstance(tenant_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant_id)):
            raise CogneeCloudError("Invalid Cognee Cloud tenant identifier.")
        self.base_url = "https://" + parsed.hostname
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})
        if tenant_id:
            self._session.headers["X-Tenant-Id"] = tenant_id

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("COGNEE_API_URL", ""), os.environ.get("COGNEE_API_KEY", ""),
                   os.environ.get("COGNEE_TENANT_ID") or None)

    def close(self):
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, method, path, *, timeout=30, **kwargs):
        deadline = time.monotonic() + timeout
        try:
            with self._session.request(
                method, self.base_url + path, allow_redirects=False, stream=True,
                timeout=Timeout(total=timeout, connect=min(5, timeout), read=min(30, timeout)), **kwargs,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise CogneeCloudError("Cognee Cloud redirects are not permitted.")
                if not 200 <= response.status_code < 300:
                    raise CogneeCloudError(f"Cognee Cloud request failed (HTTP {response.status_code}).")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                    raise CogneeCloudError("Cognee Cloud response exceeds the allowed size.")
                parts, size = [], 0
                for chunk in response.iter_content(chunk_size=8_192):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES or time.monotonic() >= deadline:
                        raise CogneeCloudError("Cognee Cloud response exceeded its size or time limit.")
                    parts.append(chunk)
                value = json.loads(b"".join(parts).decode("utf-8"), object_pairs_hook=_unique_json,
                                   parse_constant=_invalid_constant)
                if not isinstance(value, (dict, list)):
                    raise ValueError
                return value
        except CogneeCloudError:
            raise
        except Exception:
            raise CogneeCloudError("Cognee Cloud request failed or returned invalid JSON.") from None

    def add(self, text: str, dataset_name: str) -> dict:
        dataset_name = _name(dataset_name)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_TEXT_BYTES:
            raise CogneeCloudError("Cognee Cloud evidence must be nonempty and at most 6000 bytes.")
        raw = text.encode("utf-8")
        # A unique content filename avoids overwriting another evidence upload.
        filename = "secondlook_" + hashlib.sha256(raw).hexdigest() + ".txt"
        result = self._request("POST", "/api/v1/add", data={"datasetName": dataset_name},
                               files={"data": (filename, raw, "text/plain")})
        if (not isinstance(result, dict) or result.get("status") not in {
                "PipelineRunCompleted", "PipelineRunAlreadyCompleted"}
                or _field(result, "dataset_name", "datasetName") != dataset_name):
            raise CogneeCloudError("Cognee Cloud did not confirm evidence ingestion.")
        return {"id": _uuid(_field(result, "dataset_id", "datasetId")), "name": dataset_name}

    def cognify(self, dataset_id: str) -> None:
        dataset_id = _uuid(dataset_id)
        result = self._request("POST", "/api/v1/cognify", json={
            "datasetIds": [dataset_id], "runInBackground": True, "chunkSize": 8192,
        })
        if not isinstance(result, dict) or set(result) != {dataset_id}:
            raise CogneeCloudError("Cognee Cloud did not acknowledge the requested dataset.")
        run = result[dataset_id]
        if (not isinstance(run, dict) or _field(run, "dataset_id", "datasetId") != dataset_id
                or run.get("status") not in {"PipelineRunStarted", "PipelineRunCompleted", "PipelineRunAlreadyCompleted"}):
            raise CogneeCloudError("Cognee Cloud did not start graph processing.")

    def wait_completed(self, dataset_id: str, *, timeout: float = 180, poll_interval: float = 2) -> str:
        dataset_id = _uuid(dataset_id)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 180
                or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval)
                or not 0 < poll_interval <= 5):
            raise CogneeCloudError("Invalid Cognee Cloud polling limits.")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._request("GET", "/api/v1/datasets/status",
                                   params={"dataset": dataset_id, "pipeline": "cognify_pipeline"},
                                   timeout=min(30, deadline - time.monotonic()))
            if not isinstance(result, dict) or set(result) - {dataset_id}:
                raise CogneeCloudError("Cognee Cloud returned status for an unexpected dataset.")
            status = result.get(dataset_id)
            # A queued background run can briefly have no status entry.
            if status is not None and (not isinstance(status, str) or status not in _STATUS):
                raise CogneeCloudError("Cognee Cloud returned an unknown processing status.")
            normalized = _STATUS.get(status, "pending")
            if normalized == "completed":
                return normalized
            if normalized == "errored":
                raise CogneeCloudError("Cognee Cloud graph processing failed.")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_interval, remaining))
        raise CogneeCloudError("Cognee Cloud graph processing did not complete before the deadline.")

    def search(self, query_text: str, dataset_id: str, dataset_name: str) -> list[dict]:
        dataset_id, dataset_name = _uuid(dataset_id), _name(dataset_name)
        if not isinstance(query_text, str) or not query_text.strip() or len(query_text) > 1_000:
            raise CogneeCloudError("Invalid Cognee Cloud evidence query.")
        result = self._request("POST", "/api/v1/search", json={
            "query": query_text, "searchType": "CHUNKS", "datasetIds": [dataset_id],
            "topK": 6, "onlyContext": True,
        })
        if not isinstance(result, list) or not 1 <= len(result) <= 6:
            raise CogneeCloudError("Cognee Cloud returned no bounded evidence results.")
        normalized, size = [], 0
        for item in result:
            if (not isinstance(item, dict) or item.get("error") is not None
                    or _field(item, "dataset_id", "datasetId") != dataset_id
                    or _field(item, "dataset_name", "datasetName") != dataset_name):
                raise CogneeCloudError("Cognee Cloud returned evidence from an unexpected dataset.")
            text = _field(item, "search_result", "searchResult")
            if isinstance(text, list):
                # ChunksRetriever.get_completion_from_context returns raw
                # payloads, each with text/score. Its get_context_from_objects
                # joins those same texts with newlines. Accept that evidenced
                # CHUNKS shape as well; never infer text from metadata, sort
                # chunks, or reconstruct missing parts of a verification frame.
                if not 1 <= len(text) <= 6 or any(
                    not isinstance(chunk, dict) or chunk.get("error") is not None
                    or not isinstance(chunk.get("text"), str) or not chunk["text"].strip()
                    for chunk in text
                ):
                    raise CogneeCloudError("Cognee Cloud returned invalid raw evidence chunks.")
                text = "\n".join(chunk["text"] for chunk in text)
            if not isinstance(text, str) or not text.strip():
                raise CogneeCloudError("Cognee Cloud did not return raw evidence context.")
            size += len(text.encode())
            if size > MAX_SEARCH_BYTES:
                raise CogneeCloudError("Cognee Cloud evidence exceeds the allowed size.")
            normalized.append({"dataset_name": dataset_name, "search_result": text})
        return normalized

    def graph_summary(self, dataset_id: str) -> dict:
        dataset_id = _uuid(dataset_id)
        result = self._request("GET", "/api/v1/datasets/graph-summary", params={"dataset_ids": dataset_id})
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise CogneeCloudError("Cognee Cloud returned no unique graph summary.")
        item = result[0]
        if _field(item, "dataset_id", "datasetId") != dataset_id:
            raise CogneeCloudError("Cognee Cloud returned an unexpected graph summary.")
        nodes, edges = _field(item, "num_nodes", "numNodes"), _field(item, "num_edges", "numEdges")
        if any(type(count) is not int or count < 0 for count in (nodes, edges)):
            raise CogneeCloudError("Cognee Cloud returned invalid graph counts.")
        run, computed = _field(item, "pipeline_run_id", "pipelineRunId"), _field(item, "computed_at", "computedAt")
        if run is not None:
            run = _uuid(run)
        if computed is not None and (not isinstance(computed, str) or len(computed) > 64):
            raise CogneeCloudError("Cognee Cloud returned an invalid graph timestamp.")
        counts_source = "graph_summary"
        if computed is None:
            # A null cache timestamp can accompany degraded zero counts. Read
            # the actual authorized dataset graph once, under the same response
            # budget, instead of treating those zeros as measured graph data.
            graph = self._request("GET", f"/api/v1/datasets/{dataset_id}/graph")
            nodes, edges = _graph_counts(graph)
            counts_source = "dataset_graph"
        # Keep the cache timestamp unchanged: counts_source identifies when
        # counts came from a direct graph read rather than the cached summary.
        return {"dataset_id": dataset_id, "num_nodes": nodes, "num_edges": edges,
                "pipeline_run_id": run, "computed_at": computed, "counts_source": counts_source}
