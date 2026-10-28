"""Gateway-owned browser routes and Qwen synthesis for authorized speech records."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from uuid import uuid4
import weakref

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.voice_matching import load_qwen_voice_catalog
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary
from jiuwenswarm.gateway.voice.providers.qwen_omni import QwenOmniRealtimeConfig
from jiuwenswarm.gateway.voice.tts import QwenOmniTtsSession
from .gateway_input import MeetingInputStream, bind_input_handlers
from .playback import IDENTITY_FIELDS

_routes = weakref.WeakValueDictionary()


def config_revision(config: QwenOmniRealtimeConfig) -> str:
    return hashlib.sha256(json.dumps([config.upstream_url, config.api_key, config.model, config.voice]).encode()).hexdigest()


class GatewayPlaybackRoute:
    def __init__(self, channel, ws, agent_client, session_id, meeting_id, owner_id, user_id, config, startup_id):
        self.id = uuid4().hex
        self.channel, self.ws, self.agent_client = channel, ws, agent_client
        self.session_id, self.meeting_id = session_id, meeting_id
        self.owner_id, self.user_id, self.config = owner_id, user_id, config
        self.startup_id = startup_id
        self.input = MeetingInputStream(channel, ws, session_id, meeting_id)
        self.revision = config_revision(config)
        self.revoked = False
        self.records = {}
        self.produced = {}
        self.receipts = {}
        self.pending_receipts = {}
        self.current_playback = None
        self.result_lock = asyncio.Lock()
        self.tasks: dict[tuple, asyncio.Task] = {}
        self.tts = QwenOmniTtsSession(config)
        self.stops: dict[str, asyncio.Task] = {}
        _routes[self.id] = self

    async def fetch(self, method, params):
        ok, payload = await fetch_agent_unary(
            agent_client=self.agent_client(), req_method=method,
            params={**params, "_meeting_owner_id": self.owner_id},
            session_id=self.session_id, user_id=self.user_id,
            channel_id=self.channel.channel_id, label="meeting.playback", timeout_seconds=15,
        )
        if not ok:
            raise ValueError(payload.get("error") or "Meeting playback request failed")
        return payload

    def identity(self, params):
        record = self.records.get(params.get("playback_id"))
        if record is None or any(params.get(key) != record.get(key) for key in IDENTITY_FIELDS):
            raise ValueError("Unknown or stale meeting speech identity")
        return {key: record[key] for key in IDENTITY_FIELDS}

    async def cancel_synthesis(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.tts.aclose()

    async def stop_media(self):
        self.revoked = True
        self.input.revoke()
        await asyncio.gather(self.cancel_synthesis(), self.input.close())

    async def push(self, payload):
        if (payload.get("owner_id"), payload.get("session_id"), payload.get("meeting_id")) != (
            self.owner_id, self.session_id, self.meeting_id,
        ):
            raise ValueError("Foreign meeting playback push")
        event = payload.get("event")
        if event == "meeting.speech.ready":
            if self.revoked:
                return
            record = {key: value for key, value in payload.items() if key not in {"event_type", "event", "route_id", "owner_id"}}
            previous = self.records.get(record.get("playback_id"))
            if previous is not None and previous != record:
                raise ValueError("Conflicting speech delivery")
            self.records[record["playback_id"]] = record
            self.current_playback = record["playback_id"]
            self.produced.setdefault(record["playback_id"], set())
        elif event == "meeting.playback.stop":
            self.revoked = True
            self.input.revoke()
            stop_id = payload.get("stop_id")
            if not isinstance(stop_id, str) or not stop_id:
                raise ValueError("Invalid playback stop identity")
            if stop_id not in self.stops:
                self.stops[stop_id] = asyncio.create_task(self.stop_media())
        elif event == "meeting.addressed_members":
            if self.revoked:
                return
            names = payload.get("addressed_members")
            if not isinstance(names, list) or len(names) > 100 or any(
                not isinstance(name, str) or not name.strip() or len(name) > 255
                for name in names
            ):
                raise ValueError("Invalid meeting addressed members")
        elif event != "meeting.speech.error":
            raise ValueError("Unknown meeting playback event")
        await self.channel.send_event(self.ws, event, {
            key: value for key, value in payload.items() if key not in {"event_type", "event", "route_id", "owner_id"}
        })


async def handle_playback_push(payload: dict) -> None:
    route = _routes.get(payload.get("route_id"))
    if route is not None:
        await route.push(payload)


async def forget_playback_routes(ws, session_id: str, meeting_id: str | None = None, *, startup_id=None):
    routes = getattr(ws, "_meeting_playback_routes", {})
    for key, route in list(routes.items()):
        if (
            key[0] == session_id and (meeting_id is None or key[1] == meeting_id)
            and (startup_id is None or route.startup_id == startup_id)
        ):
            await route.stop_media()
            routes.pop(key, None)
            _routes.pop(route.id, None)


def bind_playback_handlers(channel, agent_client):
    """Separate handlers keep playback controls out of lifecycle ownership cleanup."""

    def route_for(ws, params, session_id):
        target = str(params.get("session_id") or session_id or "")
        route = getattr(ws, "_meeting_playback_routes", {}).get((target, params.get("meeting_id")))
        if route is None or route.owner_id != getattr(ws, "_meeting_owner_id", None):
            raise ValueError("Meeting playback is not owned by this connection")
        return route

    async def ready(ws, req_id, params, session_id, user_id=None):
        route = None
        created = False
        try:
            params = params if isinstance(params, dict) else {}
            target = str(params.get("session_id") or session_id or "")
            meeting_id, startup_id = params.get("meeting_id"), params.get("startup_id")
            owner = getattr(ws, "_meeting_owner_id", None)
            if (
                not is_valid_session_id(target) or target == "new" or not isinstance(meeting_id, str) or not meeting_id
                or not isinstance(startup_id, str) or not startup_id or not owner
                or (target, startup_id) not in getattr(ws, "_meeting_owned_sessions", {})
            ):
                raise ValueError("A matching owned meeting startup is required")
            config = QwenOmniRealtimeConfig.from_environment()
            config.validate()
            load_qwen_voice_catalog().compatible(config.model)
            routes = getattr(ws, "_meeting_playback_routes", None)
            if routes is None:
                routes = {}
                setattr(ws, "_meeting_playback_routes", routes)
            route = routes.get((target, meeting_id))
            if route is None:
                route = GatewayPlaybackRoute(channel, ws, agent_client, target, meeting_id, owner, user_id, config, startup_id)
                routes[(target, meeting_id)] = route
                created = True
            if route.revoked or route.input.closed or route.startup_id != startup_id or route.revision != config_revision(config):
                raise ValueError("Meeting playback configuration changed or has stopped")
            await route.input.start()
            # Warm the meeting-owned connection before the driver can publish
            # its first speech. Repeated readiness reuses the same session.
            await route.tts.start()
            payload = await route.fetch(ReqMethod.MEETING_PLAYBACK_READY, {
                "meeting_id": meeting_id, "startup_id": startup_id,
                "_meeting_route_id": route.id, "_qwen_model": config.model,
                "_qwen_revision": route.revision, "_qwen_default_voice": config.voice,
            })
            await channel.send_response(ws, req_id, ok=True, payload={**payload, "stream_id": route.input.id})
        except asyncio.CancelledError:
            if route is None or not route.input.closed:
                raise
            await channel.send_response(ws, req_id, ok=False, error="Meeting audio startup cancelled", code="CANCELLED")
        except Exception as exc:
            if created:
                await route.stop_media()
                await forget_playback_routes(ws, target, meeting_id, startup_id=startup_id)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MEETING_PLAYBACK_UNAVAILABLE")

    async def synthesize(ws, req_id, params, session_id, user_id=None):
        task = None
        key = None
        route = None
        try:
            params = params if isinstance(params, dict) else {}
            if any(key in params for key in ("text", "voice", "model", "provider_voice_id", "config_revision")):
                raise ValueError("Meeting TTS text and voice are resolved by the server")
            route = route_for(ws, params, session_id)
            identity = route.identity(params)
            index = params.get("chunk_index")
            if type(index) is not int:
                raise ValueError("Invalid speech chunk index")
            key = (identity["playback_id"], index)
            if route.revoked or route.tasks:
                raise ValueError("Meeting synthesis has stopped or is already active")
            chunk = await route.fetch(ReqMethod.MEETING_SPEECH_GET, {**identity, "chunk_index": index})
            config = QwenOmniRealtimeConfig.from_environment()
            if (
                route.revoked or route.tasks or config_revision(config) != route.revision
                or chunk.get("config_revision") != route.revision or chunk.get("model") != config.model
            ):
                raise ValueError("Meeting TTS configuration changed")
            task = asyncio.create_task(route.tts.synthesize(chunk["text"], chunk["voice"]))
            route.tasks[key] = task
            audio = await task
            await route.fetch(ReqMethod.MEETING_SPEECH_GET, {**identity, "chunk_index": index})
            if route.revoked or config_revision(QwenOmniRealtimeConfig.from_environment()) != route.revision:
                raise ValueError("Meeting playback stopped or its configuration changed")
            route.produced[identity["playback_id"]].add(index)
            await channel.send_response(ws, req_id, ok=True, payload={
                "success": True, "playback_id": identity["playback_id"], "chunk_index": index,
                "audio_base64": base64.b64encode(audio.audio).decode(), "audio_mime": audio.audio_mime,
            })
        except asyncio.CancelledError:
            await channel.send_response(ws, req_id, ok=False, error="Meeting synthesis cancelled", code="CANCELLED")
        except Exception as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code=getattr(exc, "code", "MEETING_TTS_FAILED"))
        finally:
            if route is not None and route.tasks.get(key) is task:
                route.tasks.pop(key, None)

    async def result(ws, req_id, params, session_id, user_id=None):
        try:
            params = params if isinstance(params, dict) else {}
            route = route_for(ws, params, session_id)
            identity = route.identity(params)
            async with route.result_lock:
                outcome, count = params.get("outcome"), params.get("completed_chunks")
                record = route.records[identity["playback_id"]]
                if outcome not in {"completed", "stopped", "failed"} or type(count) is not int or not 0 <= count <= record["chunk_count"]:
                    raise ValueError("Invalid playback outcome")
                if outcome == "completed" and count != record["chunk_count"]:
                    raise ValueError("All chunks must finish before completion")
                previous = route.receipts.get(identity["playback_id"], route.pending_receipts.get(identity["playback_id"]))
                if previous is not None and previous != (outcome, count):
                    raise ValueError("Conflicting playback receipt")
                if previous is None and (route.revoked or route.current_playback != identity["playback_id"]):
                    raise ValueError("Speech is no longer current")
                if outcome in {"failed", "stopped"}:
                    route.pending_receipts[identity["playback_id"]] = (outcome, count)
                    route.revoked = True
                    await route.cancel_synthesis()
                elif outcome == "completed":
                    record = route.records[identity["playback_id"]]
                    if len(route.produced[identity["playback_id"]]) != record["chunk_count"]:
                        raise ValueError("Speech chunks have not all been synthesized")
                route.pending_receipts[identity["playback_id"]] = (outcome, count)
                payload = await route.fetch(ReqMethod.MEETING_PLAYBACK_RESULT, {
                    **identity, "outcome": outcome, "completed_chunks": count,
                })
                route.receipts[identity["playback_id"]] = (outcome, count)
                route.pending_receipts.pop(identity["playback_id"], None)
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except (ValueError, RuntimeError) as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MEETING_PLAYBACK_RESULT_FAILED")

    async def stopped(ws, req_id, params, session_id, user_id=None):
        try:
            params = params if isinstance(params, dict) else {}
            route = route_for(ws, params, session_id)
            stop_id = params.get("stop_id")
            if stop_id not in route.stops:
                raise ValueError("Unknown meeting playback stop")
            await asyncio.shield(route.stops[stop_id])
            payload = await route.fetch(ReqMethod.MEETING_PLAYBACK_STOPPED, {
                "meeting_id": route.meeting_id, "stop_id": stop_id,
            })
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except (ValueError, RuntimeError) as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MEETING_PLAYBACK_STOP_FAILED")

    async def disconnect(ws, _sessions):
        for route in list(getattr(ws, "_meeting_playback_routes", {}).values()):
            await route.stop_media()

    channel.register_method("meeting.playback.ready", ready)
    channel.register_method("meeting.speech.synthesize", synthesize)
    channel.register_method("meeting.playback.result", result)
    channel.register_method("meeting.playback.stopped", stopped)
    bind_input_handlers(channel, route_for)
    register_disconnect = getattr(channel, "on_disconnect", None)
    if callable(register_disconnect):
        register_disconnect(disconnect)
