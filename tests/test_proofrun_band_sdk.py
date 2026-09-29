"""Pinned SDK compatibility with mocked REST/WS. Never connects to BAND."""
import asyncio
import importlib.metadata
import socket
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

pytest.importorskip("band", reason="Install requirements-band.txt to exercise the optional SDK boundary")
from band.client.rest import ChatMessageRequest, ChatEventRequest
from band.client.streaming import MessageCreatedPayload
from band.platform.event import MessageEvent, ReconnectedEvent
from src.proofrun.band import BandConfig, BandUnavailable
from src.proofrun.band_sdk import SdkTransport, REQUEST_OPTIONS, REST_URL, WS_URL


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Live network is forbidden in SDK tests")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def config():
    return BandConfig(str(uuid4()), str(uuid4()), "synthetic-proposer-key",
                      str(uuid4()), "synthetic-verifier-key")


@pytest.fixture
def sdk(monkeypatch, config):
    instances = []
    sent, marked, events = [], [], []

    class MockLink:
        def __init__(self, agent_id, api_key, ws_url, rest_url):
            assert ws_url == WS_URL and rest_url == REST_URL
            self.agent_id = agent_id
            self.is_connected = False
            self.queue = asyncio.Queue()
            self.subscribed = None
            instances.append(self)

            async def identity(**kwargs):
                assert kwargs["request_options"] == REQUEST_OPTIONS
                return NS(data=NS(id=agent_id))

            async def participants(room_id, **kwargs):
                assert room_id == config.room_id
                return NS(data=[NS(id=config.proposer_agent_id, type="Agent"),
                                NS(id=config.verifier_agent_id, type="Agent")])

            async def message(*, chat_id, message, request_options):
                assert isinstance(message, ChatMessageRequest)
                assert request_options == REQUEST_OPTIONS
                sent.append(message)
                identity = str(uuid4())
                payload = MessageCreatedPayload(
                    id=identity, content=message.content, message_type="text", sender_id=agent_id,
                    sender_type="Agent", chat_room_id=chat_id,
                    metadata={"mentions": [{"id": m.id} for m in message.mentions]},
                    inserted_at="2026-09-29T00:00:00Z", updated_at="2026-09-29T00:00:00Z")
                for link in instances:
                    await link.queue.put(MessageEvent(room_id=chat_id, payload=payload))
                return NS(data=NS(id=identity, success=True))

            async def mark(**kwargs):
                marked.append(kwargs)

            async def event(*, chat_id, event, request_options):
                assert isinstance(event, ChatEventRequest)
                assert event.message_type == "task"
                events.append(event)
                return NS(data=NS(success=True, id=str(uuid4())))

            self.rest = NS(agent_api_identity=NS(get_agent_me=identity),
                           agent_api_participants=NS(list_agent_chat_participants=participants),
                           agent_api_messages=NS(create_agent_chat_message=message,
                                                 mark_agent_message_processing=mark,
                                                 mark_agent_message_processed=mark),
                           agent_api_events=NS(create_agent_chat_event=event))

        async def connect(self):
            self.is_connected = True

        async def subscribe_room(self, room):
            self.subscribed = room

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

        async def disconnect(self):
            self.is_connected = False

    async def close(rest):
        pass

    monkeypatch.setattr("band.platform.link.BandLink", MockLink)
    monkeypatch.setattr("band.client.rest.aclose_rest_client", close)
    return NS(instances=instances, sent=sent, marked=marked, events=events)


def test_actual_sdk_pin_matches_reviewed_transport_contract():
    assert importlib.metadata.version("band-sdk") == "3.2.1"
    from band.platform.link import BandLink
    assert all(callable(getattr(BandLink, name)) for name in
               ("connect", "disconnect", "subscribe_room", "__anext__", "_on_disconnected"))


def test_sdk_uses_two_identities_ws_receive_mentions_and_lifecycle(sdk, config):
    async def run():
        transport = SdkTransport(config)
        try:
            await transport.open()
            identity = await transport.send("proposer", '{"synthetic":"request"}')
            received = await asyncio.wait_for(transport.receive("verifier"), 1)
            assert received.id == identity
            assert received.sender_id == config.proposer_agent_id
            assert sdk.sent[0].mentions[0].id == config.verifier_agent_id
            assert all(link.subscribed == config.room_id for link in sdk.instances)
            await transport.mark("verifier", identity, "processing")
            await transport.event("verifier", "Verification started", str(uuid4()))
            await transport.mark("verifier", identity, "processed")
            assert len(sdk.marked) == 2 and len(sdk.events) == 1
        finally:
            await transport.close()
        assert all(not link.is_connected for link in sdk.instances)
    asyncio.run(run())


def test_transient_sdk_disconnect_latches_failure_even_if_sdk_connected_flag_true(sdk, config):
    async def run():
        transport = SdkTransport(config)
        try:
            await transport.open()
            await transport.send("verifier", "PASS already queued")
            await transport.links["proposer"]._on_disconnected(RuntimeError("synthetic private error"))
            assert transport.links["proposer"].is_connected  # SDK 3.2.1 behavior during reconnect.
            with pytest.raises(BandUnavailable):
                await transport.receive("proposer")
            with pytest.raises(BandUnavailable):
                await transport.send("verifier", "cannot restore approval")
        finally:
            await transport.close()
    asyncio.run(run())


def test_reconnected_session_cannot_revive_attempt(sdk, config):
    async def run():
        transport = SdkTransport(config)
        try:
            await transport.open()
            await sdk.instances[0].queue.put(ReconnectedEvent())
            with pytest.raises(BandUnavailable):
                await asyncio.wait_for(transport.receive("proposer"), 1)
        finally:
            await transport.close()
    asyncio.run(run())


def test_platform_stop_prevents_queued_verdict_acceptance(sdk, config):
    async def run():
        transport = SdkTransport(config)
        try:
            await transport.open()
            await transport.send("verifier", "queued result")
            await transport.links["proposer"].on_control(NS(mode="stop"))
            with pytest.raises(BandUnavailable):
                await transport.receive("proposer")
        finally:
            await transport.close()
    asyncio.run(run())


def test_agent_key_must_authenticate_as_configured_identity(sdk, config):
    async def run():
        transport = SdkTransport(config)
        async def wrong_identity(**kwargs):
            return NS(data=NS(id=str(uuid4())))
        sdk.instances[0].rest.agent_api_identity.get_agent_me = wrong_identity
        try:
            with pytest.raises(BandUnavailable):
                await transport.open()
            assert not any(link.is_connected for link in sdk.instances)
        finally:
            await transport.close()
    asyncio.run(run())


def test_both_registered_agents_must_be_existing_room_participants(sdk, config):
    async def run():
        transport = SdkTransport(config)
        async def absent_peer(*args, **kwargs):
            return NS(data=[NS(id=config.proposer_agent_id, type="Agent")])
        sdk.instances[0].rest.agent_api_participants.list_agent_chat_participants = absent_peer
        try:
            with pytest.raises(BandUnavailable):
                await transport.open()
        finally:
            await transport.close()
    asyncio.run(run())
