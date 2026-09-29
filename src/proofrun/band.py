"""Consequential, fixed-workflow BAND handoff; no model can supply a verdict.

Both registered participants run on the worker. BAND transports the candidate
to the verifier participant and its measured verdict back to the proposer.
The trusted verifier is invoked only after receiving the exact room message.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import UUID, uuid4

from .contracts import VerificationEvidence

SCHEMA = "proofrun.band.v1"
MAX_MESSAGE_BYTES = 100_000
FAULT = "BAND handoff is unavailable or invalid; no repair was accepted."
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class BandUnavailable(RuntimeError):
    def __init__(self):
        super().__init__(FAULT)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class BandConfig:
    room_id: str
    proposer_agent_id: str
    proposer_api_key: str = field(repr=False)
    verifier_agent_id: str
    verifier_api_key: str = field(repr=False)
    timeout_seconds: int = 240

    def __post_init__(self):
        if (not all(_uuid(v) for v in (self.room_id, self.proposer_agent_id, self.verifier_agent_id))
                or self.proposer_agent_id == self.verifier_agent_id
                or self.proposer_api_key == self.verifier_api_key
                or any(not isinstance(key, str) or not 16 <= len(key) <= 512
                       or not key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in key)
                       for key in (self.proposer_api_key, self.verifier_api_key))
                or type(self.timeout_seconds) is not int or not 5 <= self.timeout_seconds <= 900):
            raise BandUnavailable()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        enabled = env.get("PROOFRUN_BAND_ENABLED", "false").strip().lower()
        if enabled in {"false", "0", ""}:
            return None
        if enabled not in {"true", "1"}:
            raise BandUnavailable()
        try:
            return cls(env.get("BAND_ROOM_ID", "").strip(),
                       env.get("BAND_PROPOSER_AGENT_ID", "").strip(),
                       env.get("BAND_PROPOSER_API_KEY", "").strip(),
                       env.get("BAND_VERIFIER_AGENT_ID", "").strip(),
                       env.get("BAND_VERIFIER_API_KEY", "").strip(),
                       int(env.get("PROOFRUN_BAND_TIMEOUT_SECONDS", "240")))
        except (ValueError, TypeError):
            raise BandUnavailable() from None


def configured_handoff():
    config = BandConfig.from_env()
    return BandHandoff(config) if config else None


@dataclass(frozen=True)
class RoomMessage:
    id: str
    room_id: str
    sender_id: str
    content: str


def _decode(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    if not isinstance(content, str) or len(content.encode()) > MAX_MESSAGE_BYTES:
        raise BandUnavailable()
    try:
        value = json.loads(content, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise BandUnavailable() from None


def _evidence_summary(evidence):
    # Worker-local paths and raw logs never enter a room. The digest includes
    # full measured cases, environment identities and the acceptance bindings.
    value = evidence.to_dict()
    value.pop("artifacts", None)
    value.pop("limitations", None)
    return value


class BandHandoff:
    def __init__(self, config: BandConfig, transport_factory=None):
        self.config = config
        self._factory = transport_factory
        self.mode = "mock" if transport_factory is not None else "live"

    def verify(self, spec, source, proposal, verifier, comparison):
        try:
            return asyncio.run(self._exchange(spec, source, proposal, verifier, comparison))
        except Exception:
            # Provider exceptions may contain headers, keys or response bodies.
            raise BandUnavailable() from None

    def _request(self, spec, source, proposal, comparison):
        required = ("source_sha256", "contract_sha256", "tests_sha256", "verifier_sha256",
                    "environment_manifest_sha256")
        if (comparison.execution_status != "completed" or comparison.finding_status != "regression_reproduced"
                or not comparison.cases or not comparison.expected_case_ids
                or Counter(comparison.executed_case_ids) != Counter(comparison.expected_case_ids)
                or any(not isinstance(comparison.bindings.get(k), str)
                       or not _HASH.fullmatch(comparison.bindings[k]) for k in required)
                or comparison.bindings["source_sha256"] != source.sha256
                or comparison.bindings["contract_sha256"] != spec.contract_sha256
                or comparison.bindings.get("revision") != source.revision
                or proposal.base_sha256 != source.sha256
                or proposal.allowed_path != spec.allowed_path
                or not isinstance(proposal.replacement, str)
                or not 1 <= len(proposal.replacement.encode()) <= 65536):
            raise BandUnavailable()
        candidate_hash = hashlib.sha256(proposal.replacement.encode()).hexdigest()
        request = {"schema_version": SCHEMA, "kind": "verify_candidate", "handoff_id": str(uuid4()),
                   "case_id": spec.case_id, "revision": source.revision,
                   "allowed_path": spec.allowed_path, "candidate_sha256": candidate_hash,
                   "candidate": proposal.replacement, "bindings": {k: comparison.bindings[k] for k in required},
                   "comparison_sha256": _hash(_evidence_summary(comparison)),
                   "expected_case_ids": list(comparison.expected_case_ids)}
        encoded = _json(request)
        if (len(encoded.encode()) > MAX_MESSAGE_BYTES
                or any(key in encoded for key in (self.config.proposer_api_key, self.config.verifier_api_key))
                or re.search(r"sk-or-v1-[A-Za-z0-9_-]{12,}", encoded)):
            raise BandUnavailable()
        return request

    async def _exchange(self, spec, source, proposal, verifier, comparison):
        request = self._request(spec, source, proposal, comparison)
        if self._factory is None:
            from .band_sdk import SdkTransport
            factory = SdkTransport
        else:
            factory = self._factory
        transport = factory(self.config)
        deadline = time.monotonic() + self.config.timeout_seconds

        async def bounded(awaitable):
            return await asyncio.wait_for(awaitable, max(0.001, deadline - time.monotonic()))

        async def receive(role, sender, message_id, expected):
            while True:
                message = await bounded(transport.receive(role))
                if message.room_id != self.config.room_id or message.sender_id != sender:
                    continue
                if message.id != message_id:
                    continue  # Unrelated/replayed room messages never trigger execution.
                if _decode(message.content) != expected:
                    raise BandUnavailable()
                await bounded(transport.mark(role, message.id, "processing"))
                return message

        try:
            await bounded(transport.open())
            request_id = await bounded(transport.send("proposer", _json(request)))
            await receive("verifier", self.config.proposer_agent_id, request_id, request)
            await bounded(transport.event("verifier", "Verification started", request["handoff_id"]))
            # This is the only entry into the verifier in BAND mode. SDK receive
            # has authenticated the sender/room and matched the exact candidate.
            evidence = await bounded(asyncio.to_thread(verifier, spec, source, proposal))
            if not isinstance(evidence, VerificationEvidence):
                raise BandUnavailable()
            if (evidence.bindings.get("candidate_sha256") != request["candidate_sha256"]
                    or any(evidence.bindings.get(key) != request["bindings"][key]
                           for key in ("source_sha256", "contract_sha256", "tests_sha256", "verifier_sha256"))
                    or evidence.bindings.get("revision") != source.revision):
                raise BandUnavailable()
            passed = (evidence.repair_status == "verified" and evidence.execution_status == "completed"
                      and evidence.complete and bool(evidence.expected_case_ids)
                      and Counter(evidence.executed_case_ids) == Counter(evidence.expected_case_ids)
                      and bool(evidence.cases) and all(row.get("status") == "passed" for row in evidence.cases))
            expected = ["repaired_" + item for item in comparison.expected_case_ids]
            if passed and (Counter(evidence.expected_case_ids) != Counter(expected)
                           or Counter(row.get("stage", "") + ":" + row.get("test_id", "")
                                      for row in evidence.cases) != Counter(expected)
                           or any(comparison.environments.get(old) != evidence.environments.get(new)
                                  for old, new in (("baseline", "repaired_baseline"),
                                                   ("updated", "repaired_updated")))):
                raise BandUnavailable()
            if evidence.repair_status == "verified" and not passed:
                raise BandUnavailable()
            result = {"schema_version": SCHEMA, "kind": "verification_result",
                      "handoff_id": request["handoff_id"], "request_sha256": _hash(request),
                      "candidate_sha256": request["candidate_sha256"],
                      "contract_sha256": spec.contract_sha256,
                      "evidence_sha256": _hash(_evidence_summary(evidence)),
                      "verdict": "PASS" if passed else "BLOCKED",
                      "execution_status": evidence.execution_status,
                      "repair_status": evidence.repair_status,
                      "expected_case_ids": evidence.expected_case_ids,
                      "executed_case_ids": evidence.executed_case_ids,
                      "case_statuses": [{"id": row.get("id"), "stage": row.get("stage"),
                                         "status": row.get("status")} for row in evidence.cases]}
            # Both sending and receipt must work. A disconnected room cannot
            # approve even a candidate whose local checks have already passed.
            result_id = await bounded(transport.send("verifier", _json(result)))
            await bounded(transport.event("verifier", "Verification " + result["verdict"], request["handoff_id"]))
            await bounded(transport.mark("verifier", request_id, "processed"))
            await receive("proposer", self.config.verifier_agent_id, result_id, result)
            await bounded(transport.mark("proposer", result_id, "processed"))
            receipt = {"provider": "band", "mode": self.mode,
                       "status": "passed" if passed else "blocked",
                       "room_id": self.config.room_id, "handoff_id": request["handoff_id"],
                       "proposer_agent_id": self.config.proposer_agent_id,
                       "verifier_agent_id": self.config.verifier_agent_id,
                       "request_message_id": request_id, "result_message_id": result_id,
                       "candidate_sha256": request["candidate_sha256"],
                       "contract_sha256": spec.contract_sha256,
                       "evidence_sha256": result["evidence_sha256"],
                       "request_sha256": result["request_sha256"]}
            directory = Path(spec.artifact_dir)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / ("band-" + request["handoff_id"] + ".json")
            path.write_text(_json({"schema_version": SCHEMA, **receipt, "result": result}) + "\n")
            path.chmod(0o600)
            evidence.artifacts["band-handoff"] = str(path.resolve())
            return evidence, receipt
        finally:
            try:
                await asyncio.wait_for(transport.close(), 5)
            except Exception:
                pass
