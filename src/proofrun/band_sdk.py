"""Pinned BAND SDK transport. No REST polling can substitute for WS delivery."""
from __future__ import annotations

import asyncio
import logging

from .band import BandUnavailable, MAX_MESSAGE_BYTES, RoomMessage, _uuid

REST_URL = "https://app.band.ai"
WS_URL = "wss://app.band.ai/api/v1/socket/websocket"
REQUEST_OPTIONS = {"timeout_in_seconds": 15, "max_retries": 0}


class SdkTransport:
    def __init__(self, config):
        # Importing the core worker never requires this optional dependency.
        from band.platform.link import BandLink
        owner = self

        class FailClosedLink(BandLink):
            async def _on_disconnected(self, error):
                # SDK 3.2.1 logs ordinary network loss but leaves is_connected
                # true while reconnecting. Latch every disconnect immediately;
                # reconnecting never revives this attempt's acceptance authority.
                owner._fail()

        self.config = config
        self.links = {
            "proposer": FailClosedLink(config.proposer_agent_id, config.proposer_api_key,
                                 ws_url=WS_URL, rest_url=REST_URL),
            "verifier": FailClosedLink(config.verifier_agent_id, config.verifier_api_key,
                                 ws_url=WS_URL, rest_url=REST_URL),
        }
        self.queues = {role: asyncio.Queue(maxsize=128) for role in self.links}
        self.tasks = []
        self.failed = False
        self._logging = []
        for link in self.links.values():
            link.on_control = self._control

    async def _control(self, payload):
        if payload.mode in {"stop", "interrupt"}:
            self._fail()

    def _fail(self):
        self.failed = True
        for queue in self.queues.values():
            if not queue.full():
                queue.put_nowait(None)

    def _healthy(self):
        if self.failed or not all(link.is_connected for link in self.links.values()):
            raise BandUnavailable()

    async def open(self):
        # Third-party exception logging can contain response headers. Retain
        # only our static public errors, restoring logging at transport close.
        for name in ("band", "band_rest", "phoenix", "phoenix_channels", "phoenix_channels_python_client",
                     "httpx", "httpcore", "websockets"):
            logger = logging.getLogger(name)
            self._logging.append((logger, logger.handlers[:], logger.propagate))
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
        for role, link in self.links.items():
            identity = await link.rest.agent_api_identity.get_agent_me(request_options=REQUEST_OPTIONS)
            if identity.data is None or identity.data.id != link.agent_id:
                raise BandUnavailable()
            participants = await link.rest.agent_api_participants.list_agent_chat_participants(
                self.config.room_id, request_options=REQUEST_OPTIONS)
            agents = {p.id for p in participants.data or [] if p.type == "Agent"}
            if not {self.config.proposer_agent_id, self.config.verifier_agent_id} <= agents:
                raise BandUnavailable()
            await link.connect()
            await link.subscribe_room(self.config.room_id)
            self.tasks.append(asyncio.create_task(self._pump(role)))
        self._healthy()

    async def _pump(self, role):
        try:
            async for event in self.links[role]:
                if event.type in {"websocket_disconnected", "reconnected"}:
                    raise BandUnavailable()
                if event.room_id != self.config.room_id:
                    continue
                if event.type in {"room_removed", "room_deleted", "participant_removed"}:
                    raise BandUnavailable()
                if event.type != "message_created":
                    continue
                payload = event.payload
                if (payload is None or payload.sender_type != "Agent" or payload.message_type != "text"
                        or payload.chat_room_id not in {None, self.config.room_id}
                        or payload.metadata is None
                        or not any(item.id == self.links[role].agent_id for item in payload.metadata.mentions)):
                    continue
                if not _uuid(payload.id) or len(payload.content.encode()) > MAX_MESSAGE_BYTES:
                    raise BandUnavailable()
                # BAND renders the routing mention into delivered text. Remove
                # only this recipient's exact leading envelope; the handoff's
                # strict JSON decoder still rejects any other prefix or suffix.
                prefix = f"@[[{self.links[role].agent_id}]] "
                content = payload.content.removeprefix(prefix)
                self.queues[role].put_nowait(RoomMessage(payload.id, event.room_id,
                                                       payload.sender_id, content))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail()

    async def send(self, role, content):
        from band.client.rest import ChatMessageRequest, ChatMessageRequestMentionsItem
        self._healthy()
        if not isinstance(content, str) or not 1 <= len(content.encode()) <= MAX_MESSAGE_BYTES:
            raise BandUnavailable()
        peer = self.config.verifier_agent_id if role == "proposer" else self.config.proposer_agent_id
        response = await self.links[role].rest.agent_api_messages.create_agent_chat_message(
            chat_id=self.config.room_id,
            message=ChatMessageRequest(content=content, mentions=[ChatMessageRequestMentionsItem(id=peer)]),
            request_options=REQUEST_OPTIONS)
        self._healthy()
        if response.data is None or response.data.success is not True or not _uuid(response.data.id):
            raise BandUnavailable()
        return response.data.id

    async def receive(self, role):
        self._healthy()
        result = await self.queues[role].get()
        self._healthy()
        if result is None:
            raise BandUnavailable()
        return result

    async def mark(self, role, message_id, status):
        self._healthy()
        api = self.links[role].rest.agent_api_messages
        operation = (api.mark_agent_message_processing if status == "processing"
                     else api.mark_agent_message_processed)
        await operation(chat_id=self.config.room_id, id=message_id, request_options=REQUEST_OPTIONS)
        self._healthy()

    async def event(self, role, title, handoff_id):
        from band.client.rest import ChatEventRequest
        self._healthy()
        response = await self.links[role].rest.agent_api_events.create_agent_chat_event(
            chat_id=self.config.room_id,
            event=ChatEventRequest(content=title, message_type="task",
                                   metadata={"schema_version": "proofrun.band.v1", "handoff_id": handoff_id}),
            request_options=REQUEST_OPTIONS)
        self._healthy()
        if response.data is None or response.data.success is not True:
            raise BandUnavailable()

    async def close(self):
        from band.client.rest import aclose_rest_client
        try:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for link in self.links.values():
                try:
                    await asyncio.wait_for(link.disconnect(), 1)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(aclose_rest_client(link.rest), 1)
                except Exception:
                    pass
        finally:
            for logger, handlers, propagate in self._logging:
                logger.handlers, logger.propagate = handlers, propagate
