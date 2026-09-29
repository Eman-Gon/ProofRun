"""Offline room transport tests. No BAND, model or Docker calls are allowed."""
import asyncio
from dataclasses import replace
import json
import socket
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.proofrun.band import BandConfig, BandHandoff, BandUnavailable, RoomMessage, configured_handoff
import test_proofrun_service as support


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Network is forbidden in BAND unit tests")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.delenv("PROOFRUN_BAND_ENABLED", raising=False)


@pytest.fixture
def config():
    return BandConfig(str(uuid4()), str(uuid4()), "synthetic-proposer-private-key",
                      str(uuid4()), "synthetic-verifier-private-key", 5)


@pytest.fixture
def fixture():
    case = support.ProofRunServiceTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


class Room:
    """Explicit mock delivery queue; sends alone do not execute a verifier."""
    def __init__(self, config, fault=None):
        self.config, self.fault = config, fault
        self.queues = {"proposer": asyncio.Queue(), "verifier": asyncio.Queue()}
        self.calls = []
        self.closed = False

    async def open(self):
        self.calls.append("open")
        if self.fault == "unavailable":
            raise RuntimeError("provider body and secret must not escape")

    async def send(self, role, content):
        self.calls.append("send-" + role)
        if self.fault == "disconnect-after-check" and role == "verifier":
            raise RuntimeError("websocket unavailable")
        receiver = "verifier" if role == "proposer" else "proposer"
        sender = self.config.proposer_agent_id if role == "proposer" else self.config.verifier_agent_id
        identity = str(uuid4())
        if self.fault == "replay":
            await self.queues[receiver].put(RoomMessage(str(uuid4()), self.config.room_id, sender, content))
            await self.queues[receiver].put(RoomMessage(identity, str(uuid4()), sender, content))
            await self.queues[receiver].put(RoomMessage(identity, self.config.room_id, str(uuid4()), content))
        if self.fault == "bad-request" and role == "proposer":
            obj = json.loads(content)
            obj["candidate"] += "\n# altered"
            content = json.dumps(obj)
        if self.fault == "bad-result" and role == "verifier":
            obj = json.loads(content)
            obj["verdict"] = "PASS" if obj["verdict"] != "PASS" else "BLOCKED"
            content = json.dumps(obj)
        if self.fault == "duplicate-json" and role == "proposer":
            content = content[:-1] + ',"kind":"verify_candidate"}'
        await self.queues[receiver].put(RoomMessage(identity, self.config.room_id, sender, content))
        return identity

    async def receive(self, role):
        self.calls.append("receive-" + role)
        if self.fault == "missing-delivery":
            await asyncio.Future()
        return await self.queues[role].get()

    async def mark(self, role, message_id, status):
        self.calls.append(status + "-" + role)

    async def event(self, role, title, handoff_id):
        self.calls.append("event-" + role)

    async def close(self):
        self.closed = True


def handoff(config, fault=None):
    rooms = []
    def factory(cfg):
        room = Room(cfg, fault)
        rooms.append(room)
        return room
    return BandHandoff(config, factory), rooms


def test_default_off_without_sdk_import():
    assert configured_handoff() is None


@pytest.mark.parametrize("change", [
    {"room_id": "bad"}, {"proposer_api_key": "short"}, {"verifier_api_key": "x\n" * 20},
    {"timeout_seconds": True}, {"timeout_seconds": 901},
])
def test_invalid_configuration_is_static_and_secret_safe(config, change):
    with pytest.raises(BandUnavailable, match="BAND handoff") as exc:
        replace(config, **change)
    assert "private-key" not in str(exc.value)
    assert "private-key" not in repr(config)


def test_distinct_registered_identities_and_keys_required(config):
    for changes in ({"verifier_agent_id": config.proposer_agent_id},
                    {"verifier_api_key": config.proposer_api_key}):
        with pytest.raises(BandUnavailable):
            replace(config, **changes)


@pytest.mark.parametrize("enabled", ["true", "1", "unexpected"])
def test_enabled_missing_settings_never_silently_disables(enabled):
    with pytest.raises(BandUnavailable):
        BandConfig.from_env({"PROOFRUN_BAND_ENABLED": enabled})


def test_pass_requires_room_delivery_before_verifier_and_result_delivery(fixture, config):
    coordinator, rooms = handoff(config)
    def verify(spec, source, proposal):
        assert "processing-verifier" in rooms[0].calls
        assert rooms[0].calls[-1] == "event-verifier"
        return support.verification(spec, source, proposal)
    service = fixture.service(verify=verify, band_handoff=coordinator)
    record = fixture.execute(service, repair=True)
    assert record["repair_status"] == "verified"
    assert record["coordination"]["status"] == "passed"
    assert record["coordination"]["mode"] == "mock"
    assert rooms[0].calls[-1] == "processed-proposer"
    assert rooms[0].closed
    artifact = next(a for a in record["artifacts"] if a["id"] == "attempt-1-band-handoff")
    content, _ = service.artifact(record["run_id"], artifact["id"])
    evidence = json.loads(content)
    assert evidence["result"]["verdict"] == "PASS"
    assert evidence["evidence_sha256"] == record["coordination"]["evidence_sha256"]
    assert "private-key" not in content.decode()


def test_replayed_foreign_sender_and_room_do_not_dispatch_again(fixture, config):
    coordinator, rooms = handoff(config, "replay")
    verify = Mock(side_effect=support.verification)
    record = fixture.execute(fixture.service(verify=verify, band_handoff=coordinator), repair=True)
    assert record["repair_status"] == "verified"
    assert verify.call_count == 1
    assert rooms[0].calls.count("receive-verifier") == 4


@pytest.mark.parametrize("fault, verifier_calls", [
    ("unavailable", 0), ("bad-request", 0), ("duplicate-json", 0),
    ("bad-result", 1), ("disconnect-after-check", 1),
])
def test_room_failure_has_no_bypass_and_preserves_finding(fixture, config, fault, verifier_calls):
    coordinator, rooms = handoff(config, fault)
    verify = Mock(side_effect=support.verification)
    record = fixture.execute(fixture.service(verify=verify, band_handoff=coordinator), repair=True)
    assert (record["execution_status"], record["finding_status"], record["repair_status"]) == (
        "completed", "regression_reproduced", "unavailable")
    assert record["coordination"]["status"] == "unavailable"
    assert verify.call_count == verifier_calls
    assert rooms[0].closed
    assert "provider body" not in json.dumps(record)


def test_blocked_candidate_retries_through_new_handoff(fixture, config):
    coordinator, rooms = handoff(config)
    calls = 0
    def verify(spec, source, patch):
        nonlocal calls
        calls += 1
        result = support.verification(spec, source, patch)
        if calls == 1:
            result.repair_status = "rejected"
            result.cases[-1]["status"] = "failed"
        return result
    record = fixture.execute(fixture.service(verify=verify, band_handoff=coordinator), repair=True)
    assert [a["coordination"]["status"] for a in record["attempts"]] == ["blocked", "passed"]
    assert record["attempts"][0]["coordination"]["handoff_id"] != record["coordination"]["handoff_id"]
    assert len(rooms) == 2


def test_enabled_configuration_failure_precedes_model_and_verifier(fixture, monkeypatch):
    monkeypatch.setenv("PROOFRUN_BAND_ENABLED", "true")
    propose, verify = Mock(), Mock()
    record = fixture.execute(fixture.service(propose=propose, verify=verify), repair=True)
    assert record["repair_status"] == "unavailable"
    assert record["finding_status"] == "regression_reproduced"
    assert not propose.called and not verify.called


@pytest.mark.parametrize("fault", ["candidate", "contract", "empty", "incomplete", "environment"])
def test_invalid_measured_result_cannot_be_room_pass(fixture, config, fault):
    coordinator, _ = handoff(config)
    def verify(spec, source, patch):
        result = support.verification(spec, source, patch)
        if fault in {"candidate", "contract"}:
            result.bindings[fault + "_sha256"] = "a" * 64
        elif fault == "empty":
            result.expected_case_ids = []
        elif fault == "environment":
            result.environments["repaired_updated"]["image_id"] = "sha256:" + "f" * 64
        else:
            result.executed_case_ids.pop()
        return result
    record = fixture.execute(fixture.service(verify=verify, band_handoff=coordinator), repair=True)
    assert record["repair_status"] == "unavailable"


def test_room_candidate_credential_scan_precedes_network(fixture, config):
    coordinator, rooms = handoff(config)
    def propose(context, attempt):
        patch = support.proposal(context, attempt)
        return replace(patch, replacement=patch.replacement + config.verifier_api_key)
    record = fixture.execute(fixture.service(propose=propose, band_handoff=coordinator), repair=True)
    assert record["repair_status"] == "unavailable"
    assert not rooms


def test_missing_websocket_delivery_times_out_without_verifying(fixture, config):
    # Accelerate only this injected transport test; public config rejects <5 s.
    object.__setattr__(config, "timeout_seconds", 0.02)
    coordinator, rooms = handoff(config, "missing-delivery")
    verify = Mock(side_effect=support.verification)
    record = fixture.execute(fixture.service(verify=verify, band_handoff=coordinator), repair=True)
    assert record["repair_status"] == "unavailable"
    assert record["finding_status"] == "regression_reproduced"
    assert not verify.called and rooms[0].closed


@pytest.mark.parametrize("fault", [None, "bad-request", "disconnect-after-check"])
def test_progress_tracks_observed_delivery_and_preserves_failure_stage(fixture, config, fault):
    coordinator, _ = handoff(config, fault)
    service = fixture.service(band_handoff=coordinator)
    snapshots = []
    change = service._change

    def observe(record, **values):
        change(record, **values)
        if "coordination" in values:
            snapshots.append(json.loads(json.dumps(values["coordination"])))

    service._change = observe
    record = fixture.execute(service, repair=True)
    stages = [row["stage"] for row in snapshots if row.get("stage") and row["status"] == "waiting"]
    expected = ["connecting", "sending_candidate", "waiting_for_verifier",
                "candidate_received", "verifying", "sending_result", "waiting_for_result"]
    if fault == "bad-request":
        expected = expected[:3]
    elif fault == "disconnect-after-check":
        expected = expected[:6]
    assert stages == expected
    final = record["coordination"]
    assert final["stages"] == expected + ([] if fault else ["completed"])
    assert final["status"] == ("unavailable" if fault else "passed")
    assert final["stage"] == (expected[-1] if fault else "completed")
    assert final["request_message_id"]
    assert "private-key" not in json.dumps(snapshots)
