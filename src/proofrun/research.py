"""Bounded Similarweb customer context, isolated from repair/verdict inputs.

One explicit request buys at most one total-visits lookup for one complete month.
Only normalized metrics and a hash of the response are retained; provider text,
request URLs containing credentials, and raw response metadata never leave here.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time

import requests

SCHEMA_VERSION = "proofrun.research.v1"
VISITS_DOCUMENTATION = "https://developers.similarweb.com/reference/visits"
MAX_RESPONSE_BYTES = 131_072
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")
_LIMITATIONS = [
    "Similarweb traffic estimates are customer context, not software correctness evidence.",
    "One complete month; worldwide desktop and mobile web visits, including subdomains.",
    "Data availability and worldwide access depend on the Similarweb API subscription.",
]


class ResearchError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _period(month: str) -> dict:
    year, number = map(int, month.split("-"))
    return {"start_date": month + "-01", "end_date": f"{month}-{calendar.monthrange(year, number)[1]:02d}"}


def normalize_domain(value: str) -> str:
    """Accept a DNS name only; a customer string can never select the API host."""
    if not isinstance(value, str) or not value.isascii() or not 1 <= len(value) <= 253:
        raise ResearchError(400, "invalid_research", "Use a public website domain without a URL, path or port.")
    domain = value.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    labels = domain.split(".")
    if (len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels)
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", labels[-1])
            or labels[-1] in {"local", "localhost", "internal", "test", "invalid", "example"}):
        raise ResearchError(400, "invalid_research", "Use a public website domain without a URL, path or port.")
    return domain


def _unavailable(code: str, message: str, response_sha256=None) -> dict:
    return {"status": "unavailable", "metrics": [], "response_sha256": response_sha256,
            "error": {"code": code, "message": message}}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate provider key")
        result[key] = value
    return result


def _finite_json(_value):
    raise ValueError("Non-finite provider number")


class _PrivateRequestLogs(logging.Filter):
    """urllib3 debug records include v1's authenticated query string."""

    def __init__(self):
        super().__init__()
        self._thread = threading.get_ident()

    def filter(self, record):
        return record.thread != self._thread


class SimilarwebClient:
    """Official REST visits endpoint, no redirects, retries, or raw error echoes."""

    def __init__(self, api_key=None, transport=None):
        self._api_key = api_key
        self._owns_transport = transport is None
        self._transport = transport if transport is not None else requests.Session()
        if self._owns_transport:
            # Do not forward query credentials through ambient proxy/netrc settings.
            self._transport.trust_env = False

    @classmethod
    def from_env(cls, env=None):
        return cls((os.environ if env is None else env).get("SIMILARWEB_API_KEY"))

    def close(self):
        if self._owns_transport:
            self._transport.close()

    def fetch(self, domain: str, month: str) -> dict:
        domain = normalize_domain(domain)
        if not isinstance(month, str) or not _MONTH.fullmatch(month) or month < "2000-01":
            raise ResearchError(400, "invalid_research", "Use a complete month in YYYY-MM format.")
        key = self._api_key
        if key is None or key == "":
            return _unavailable("not_configured", "Similarweb is not configured. Add SIMILARWEB_API_KEY to the worker environment.")
        if (not isinstance(key, str) or not 1 <= len(key) <= 512 or not key.isascii()
                or any(ord(char) < 33 or ord(char) > 126 for char in key)):
            return _unavailable("invalid_configuration", "The server-side Similarweb API key is invalid.")
        response = None
        response_hash = None
        started = time.monotonic()
        transport_logger = logging.getLogger("urllib3.connectionpool")
        private_logs = _PrivateRequestLogs()
        transport_logger.addFilter(private_logs)
        try:
            response = self._transport.get(
                f"https://api.similarweb.com/v1/website/{domain}/total-traffic-and-engagement/visits",
                params={"api_key": key, "start_date": month, "end_date": month,
                        "country": "world", "granularity": "monthly", "main_domain_only": "false",
                        "format": "json", "show_verified": "false", "mtd": "false", "engaged_only": "false"},
                headers={"Accept": "application/json"}, timeout=(5, 20),
                allow_redirects=False, stream=True,
            )
            status = response.status_code
            if status in {401, 403}:
                return _unavailable("access_denied", "Similarweb rejected the API key or this endpoint/country is not included in the subscription.")
            if status == 429:
                return _unavailable("rate_limited", "Similarweb rate or credit limits prevented this lookup. No automatic retry was made.")
            if status != 200:
                return _unavailable("provider_error", "Similarweb did not return a successful response. No automatic retry was made.")
            declared = response.headers.get("Content-Length")
            if declared is not None and (not re.fullmatch(r"[0-9]{1,10}", declared) or int(declared) > MAX_RESPONSE_BYTES):
                return _unavailable("invalid_response", "Similarweb returned an invalid or oversized response.")
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                if time.monotonic() - started > 30:
                    return _unavailable("timeout", "The Similarweb lookup timed out. No automatic retry was made.")
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    return _unavailable("invalid_response", "Similarweb returned an invalid or oversized response.")
            response_hash = _sha256(bytes(chunks))
            value = json.loads(chunks.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_finite_json)
            if not isinstance(value, dict):
                raise ValueError("Expected provider object")
            meta = value.get("meta", {})
            if not isinstance(meta, dict) or meta.get("status", "Success") != "Success":
                raise ValueError("Unsuccessful provider body")
            # Reject conflicting identity when the provider supplies it. Raw meta
            # may include api_key and is never copied into the normalized report.
            identity = meta.get("request", {})
            if not isinstance(identity, dict):
                raise ValueError("Invalid provider identity")
            expected = {"domain": domain, "country": "world", **_period(month)}
            if any(field in identity and identity[field] != expected_value
                   for field, expected_value in expected.items()):
                raise ValueError("Provider identity mismatch")
            if "granularity" in identity and str(identity["granularity"]).lower() != "monthly":
                raise ValueError("Provider granularity mismatch")
            rows = value.get("visits")
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError("Expected one monthly metric")
            if not rows:
                return {"status": "no_data", "metrics": [], "response_sha256": response_hash, "error": None}
            row = rows[0]
            if not isinstance(row, dict) or row.get("date") != month + "-01" or "visits" not in row:
                raise ValueError("Wrong metric period")
            visits = row["visits"]
            if visits is None:
                return {"status": "no_data", "metrics": [], "response_sha256": response_hash, "error": None}
            if type(visits) not in (int, float) or not math.isfinite(visits) or visits < 0:
                raise ValueError("Invalid visit count")
            return {"status": "completed", "metrics": [{"name": "estimated_visits", "value": visits, "unit": "visits"}],
                    "response_sha256": response_hash, "error": None}
        except requests.Timeout:
            return _unavailable("timeout", "The Similarweb lookup timed out. No automatic retry was made.")
        except requests.RequestException:
            return _unavailable("connection_failed", "The Similarweb lookup could not complete. No automatic retry was made.")
        except (ValueError, UnicodeError, RecursionError, OverflowError, TypeError):
            return _unavailable("invalid_response", "Similarweb returned unsupported or inconsistent data.", response_hash)
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                transport_logger.removeFilter(private_logs)


class ResearchService:
    """Persist a claim before a paid call, and never retry its request ID.

    One process owns the directory; a mutex also serializes HTTP callers. A crash
    may leave a provider call's outcome unknown, so recovery returns unavailable
    for the same ID instead of silently consuming another credit.
    """

    def __init__(self, artifact_dir: Path, client=None, clock=None):
        self.artifact_dir = Path(artifact_dir).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._client = client if client is not None else SimilarwebClient.from_env()
        self._mutex = threading.RLock()
        self._closed = False
        self._process_lock = (self.artifact_dir / "research.lock").open("a")
        try:
            fcntl.flock(self._process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._process_lock.close()
            self._client.close()
            raise ValueError("Another worker owns this research directory") from None

    def _validate(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"request_id", "domain", "month"}:
            raise ResearchError(400, "invalid_research", "Supply exactly request_id, domain and month.")
        request_id = payload["request_id"]
        if not isinstance(request_id, str) or not _SAFE_ID.fullmatch(request_id):
            raise ResearchError(400, "invalid_research", "request_id must contain 1–128 letters, digits, dots, underscores or hyphens.")
        domain = normalize_domain(payload["domain"])
        month = payload["month"]
        if (not isinstance(month, str) or not _MONTH.fullmatch(month) or month < "2000-01"
                or month >= self._clock().astimezone(timezone.utc).strftime("%Y-%m")):
            raise ResearchError(400, "invalid_research", "Use a past complete month in YYYY-MM format.")
        return {"request_id": request_id, "domain": domain, "month": month}

    def _save(self, path: Path, record: dict):
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(self.artifact_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _public(record: dict) -> dict:
        return json.loads(json.dumps(record["report"]))

    def submit(self, payload: dict) -> dict:
        normalized = self._validate(payload)
        research_id = "research-" + _sha256(normalized["request_id"].encode())
        path = self.artifact_dir / (research_id + ".json")
        with self._mutex:
            if self._closed:
                raise ResearchError(503, "research_closed", "The customer research service is closed.")
            if path.exists():
                record = json.loads(path.read_text())
                if record.get("request") != normalized:
                    raise ResearchError(409, "request_id_conflict", "This request_id already belongs to different research inputs.")
                return self._public(record)
            report = {
                "schema_version": SCHEMA_VERSION, "research_id": research_id,
                "request_id": normalized["request_id"], "domain": normalized["domain"],
                "period": _period(normalized["month"]), "provider": "similarweb",
                "observed_at": self._clock().astimezone(timezone.utc).isoformat(),
                "request_sha256": _sha256(_canonical(normalized)),
                "sources": [{"title": "Similarweb total visits API", "url": VISITS_DOCUMENTATION},
                            {"title": "Website profile on Similarweb", "url": f"https://www.similarweb.com/website/{normalized['domain']}/"}],
                "limitations": list(_LIMITATIONS),
                **_unavailable("interrupted", "This lookup was reserved but did not finish. Reusing its request ID will not repeat a potentially paid call."),
            }
            record = {"request": normalized, "report": report}
            # The failure-safe report itself is the durable claim. It remains
            # valid even if the process stops before writing the final result.
            self._save(path, record)
            result = self._client.fetch(normalized["domain"], normalized["month"])
            report.update(result)
            report["observed_at"] = self._clock().astimezone(timezone.utc).isoformat()
            self._save(path, record)
            return self._public(record)

    def get_report(self, research_id: str) -> dict:
        if not isinstance(research_id, str) or not re.fullmatch(r"research-[0-9a-f]{64}", research_id):
            raise ResearchError(404, "unknown_research", "Customer research report not found.")
        with self._mutex:
            path = self.artifact_dir / (research_id + ".json")
            if not path.is_file():
                raise ResearchError(404, "unknown_research", "Customer research report not found.")
            return self._public(json.loads(path.read_text()))

    def close(self):
        with self._mutex:
            if not self._closed:
                self._closed = True
                self._client.close()
                fcntl.flock(self._process_lock, fcntl.LOCK_UN)
                self._process_lock.close()
