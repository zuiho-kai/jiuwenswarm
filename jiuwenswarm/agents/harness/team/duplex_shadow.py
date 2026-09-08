# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native A2A/U2A routing adapter at the SDK's pre-delivery boundary.

The SDK owns DB ordering, broadcasts, ACKs, and all delivery/lifecycle actions.
Only this adapter knows its MessageHandler and NativeHarness accessors.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import weakref
import uuid
from dataclasses import asdict
from functools import wraps
from typing import Any

from jiuwenswarm.common.duplex_router import (
    ControlSnapshot, InboundMessage, SYSTEM_PROMPT, decision_schema, observe, prompt_for,
)

logger = logging.getLogger(__name__)


class RoutedInput(str):
    """Original SDK rendering plus DB identity; not a second message history."""

    def __new__(cls, text, message):
        obj = super().__new__(cls, text)
        obj.message = message
        return obj


def native_from_runtime(runtime):
    from openjiuwen.agent_teams.harness.native_harness import NativeHarness
    from openjiuwen.agent_teams.harness.team_harness import TeamHarness

    if isinstance(runtime, TeamHarness):
        runtime = runtime.inner_agent
    return runtime if isinstance(runtime, NativeHarness) else None


def snapshot_from_native(harness: Any) -> ControlSnapshot | None:
    """Copy only committed control data; never copy context_messages/reasoning.

    Use explicit Working Intent only while its committed sources still match.
    Resolve against the current committed plan, even before the next rail hook. Never infer unknown hypotheses or constraints from partial output.
    """
    active = harness.active_round
    if active is None:
        return None
    checkpoint = active.last_iter_snapshot or active.pre_round_snapshot
    if checkpoint is None:
        return None
    from .duplex_state import current_plan, resolve_intent

    plan = current_plan(harness, checkpoint.deep_agent_state.get("task_plan"))
    current_id = plan.get("current_task_id")
    current = next((t for t in plan.get("tasks", []) if t.get("id") == current_id), {})
    query = active.original_query
    if isinstance(query, list):
        query = "\n".join(str(item) for item in query)
    goal = str(plan.get("goal") or (query if isinstance(query, str) else ""))
    next_action = str(current.get("description") or current.get("content") or "")
    phase = str(getattr(active.iter_phase, "value", active.iter_phase))
    # Object identities distinguish replacement snapshots/rounds with equal
    # indices. This token is process-local, not a durable checkpoint address.
    version_data = [id(active), id(checkpoint), phase, active.model_call_in_flight,
                    active.tool_started, active.pause_requested, goal, next_action]
    intent = {**getattr(harness, "_duplex_control_state", {}), **resolve_intent(harness, plan)}
    version_data.append(intent)
    version = hashlib.sha256(json.dumps(version_data, sort_keys=True).encode()).hexdigest()[:20]
    return ControlSnapshot(
        context_version=f"{getattr(harness, '_duplex_version', 0)}:{version}", round_id=str(active.round_id),
        checkpoint_id=f"{active.round_id}:{checkpoint.iteration_index}",
        phase=phase, goal=intent.get("goal") or goal,
        next_action=intent.get("next_action") or next_action,
        current_hypothesis=intent.get("current_hypothesis", ""),
        constraints=tuple(intent.get("constraints", [])),
        committed_output=intent.get("committed_output", ""),
        intent_source=intent.get("intent_source", "unknown"),
        pending_tools=tuple({"call_id": key, **value} for key, value in intent.get("pending_tools", {}).items()),
    )


class ShadowObserver:
    """One observation worker per recipient; no fast-model wait on the delivery path."""

    def __init__(self, host: Any, model_name: str, timeout_seconds: float | None) -> None:
        self._host = weakref.ref(host)
        self._model_name = model_name
        self._timeout = timeout_seconds
        self._pending: dict[str, InboundMessage] = {}
        self._pending_snapshot: ControlSnapshot | None = None
        self._inflight: set[str] = set()
        self._task: asyncio.Task | None = None

    def submit(self, message: InboundMessage) -> None:
        if message.message_id in self._pending or message.message_id in self._inflight:
            return
        self._pending[message.message_id] = message
        self._pending_snapshot = self._snapshot()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="duplex-shadow")

    def _snapshot(self) -> ControlSnapshot | None:
        host = self._host()
        native = native_from_runtime(host.harness) if host is not None else None
        return snapshot_from_native(native) if native is not None else None

    async def _classify(self, snapshot, messages):
        from openjiuwen.agent_teams import create_tiny_agent

        host = self._host()
        if host is None:
            raise RuntimeError("recipient disposed")
        # An ephemeral TinyAgent owns a separate context for every decision.
        async with create_tiny_agent(
            system_prompt=SYSTEM_PROMPT, model_name=self._model_name,
            model_resolver=host.tiny_agent_model_resolver,
            default_schema=decision_schema(snapshot), name=f"duplex-{uuid.uuid4().hex}",
            language="en", max_iterations=1,
        ) as agent:
            return await agent.run(prompt_for(snapshot, messages))

    async def _run(self) -> None:
        try:
            while self._pending:
                messages = tuple(self._pending.values())
                self._pending.clear()
                self._inflight = {m.message_id for m in messages}
                snapshot = self._pending_snapshot
                if snapshot is None:
                    record = {"status": "inactive", "message_ids": list(self._inflight),
                              "effective_action": "UNCHANGED"}
                else:
                    observation = await observe(
                        snapshot, messages, classify=self._classify,
                        current_snapshot=self._snapshot, timeout_seconds=self._timeout,
                    )
                    record = asdict(observation)
                host = self._host()
                if host is not None:
                    native = native_from_runtime(host.harness)
                    record.update(session_id=native.session_id if native else None,
                                  member_name=host.blueprint.member_name)
                # No message body, task content, credentials, or reasoning in logs.
                logger.info("duplex.shadow %s", json.dumps(record, ensure_ascii=False))
                self._inflight.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("duplex shadow observer failed; delivery unchanged", exc_info=True)
        finally:
            self._inflight.clear()
            self._pending.clear()

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._pending.clear()


def _submit(handler, msg, expanded, is_human_agent: bool) -> None:
    from jiuwenswarm.common.config import get_config

    config = get_config().get("duplex_router", {}) or {}
    if config.get("mode", "off") != "shadow" or is_human_agent or expanded.is_template:
        return
    host = handler._round
    native = native_from_runtime(host.harness)
    if native is None or native.active_round is None:
        return
    # JSON control/approval protocols remain entirely outside model routing.
    if msg.protocol not in (None, "", "text", "plain"):
        return
    model = str(config.get("model_name") or "").strip()
    timeout = (float(config["timeout_seconds"]) if config.get("timeout_seconds") is not None else None)
    if not model or (timeout is not None and (not math.isfinite(timeout) or timeout <= 0)):
        return
    observer = getattr(handler, "_duplex_shadow_observer", None)
    if observer is None:
        observer = ShadowObserver(host, model, timeout)
        handler._duplex_shadow_observer = observer
    observer.submit(InboundMessage(str(msg.message_id), str(msg.from_member_name),
                                   expanded.body))


async def deliver_routed(host, content, *, use_steer, original, settings=None):
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness

    config = settings if settings is not None else getattr(host, "duplex_settings", None)
    if config is None:
        config = get_config().get("duplex_router", {}) or {}
    native = native_from_runtime(host.harness)
    if config.get("mode") != "active" or not isinstance(native, DuplexNativeHarness):
        return await original(host, str(content), use_steer=use_steer)
    message = content.message
    if message.message_id in native._duplex_received:
        return
    policy = config.get("policy", "model") if use_steer else "serial"
    if policy in ("serial", "steer"):
        content = await native.durable_input(content)
        result = await original(host, str(content), use_steer=policy == "steer")
        native._duplex_received.add(message.message_id)
        return result
    if policy != "always_interrupt" and not str(config.get("model_name") or "").strip():
        raise ValueError("active model routing requires model_name")
    controller = input_controller(host, config, original)
    try:
        return await controller.submit(content)
    except asyncio.CancelledError:
        await controller.aclose()
        raise


def input_controller(host, config, original):
    from jiuwenswarm.agents.harness.team.duplex_controller import InputController

    native = native_from_runtime(host.harness)
    controller = getattr(native, "_duplex_controller", None)
    if controller is not None and not controller.closed:
        return controller
    model = str(config.get("model_name") or "")
    timeout = (float(config["timeout_seconds"]) if config.get("timeout_seconds") is not None else None)
    if (timeout is not None and (not math.isfinite(timeout) or timeout <= 0)):
        raise ValueError("invalid input controller timeout")
    observer = ShadowObserver(host, model, timeout)

    async def classify(snapshot, messages):
        if config.get("policy") == "always_interrupt":
            return dict(action="INTERRUPT", context_version=snapshot.context_version,
                        round_id=snapshot.round_id, checkpoint_id=snapshot.checkpoint_id)
        return await observer._classify(snapshot, messages)

    async def apply(batch, decision):
        batch = tuple(item for item in batch if item.message.message_id not in native._duplex_received)
        if not batch:
            return
        if decision is not None and decision.status == "stale" and native.active_round is None:
            decision = None  # The previous round ended; SDK can admit this as a new input.
        if decision is not None and decision.status != "ok":
            raise RuntimeError(f"input classification failed: {decision.status}; retry delivery")
        batch = tuple([await native.durable_input(item) for item in batch])
        if decision is None:
            for item in batch:
                await original(host, str(item), use_steer=True)
                native._duplex_received.add(item.message.message_id)
            return
        recorder = getattr(host, "record_duplex_observation", None)
        if recorder is not None:
            recorder(decision)
        effective = await native.route_input("\n\n".join(str(item) for item in batch),
            version=decision.context_version, action=decision.proposed_action,
            message_id=batch[0].message.message_id,
            message_ids=tuple(item.message.message_id for item in batch))
        if effective == "STALE":
            raise RuntimeError("input decision became stale; retry delivery")
        logger.info("duplex.route %s", json.dumps({
            "message_ids": [item.message.message_id for item in batch],
            "proposed_action": decision.proposed_action, "effective_action": effective,
            "status": decision.status, "context_version": decision.context_version,
            "session_id": native.session_id, "member_name": host.blueprint.member_name}))

    controller = InputController(snapshot=observer._snapshot, classify=classify, apply=apply,
                                 timeout=timeout)
    native._duplex_controller = controller
    return controller


def install_shadow_observer() -> bool:
    """Install an idempotent SDK shim; unsupported SDKs keep their old behavior.

    Rendering is the narrow synchronous point after DB read/expansion and before
    deliver_input. Inheriting the drain avoids duplicating ACK/watermark logic.
    """
    try:
        from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
        from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    except ImportError:
        logger.warning("duplex shadow unavailable: unsupported SDK")
        return False

    original = getattr(MessageHandler, "_format_message", None)
    dispose = getattr(TeamAgent, "_dispose_tiny_agents", None)
    if original is None or dispose is None:
        logger.warning("duplex shadow unavailable: SDK hooks missing")
        return False
    if getattr(original, "_duplex_shadow", False):
        return True
    try:
        inspect.signature(original).bind(None, None, expanded=None,
                                         is_human_agent=False, now_ms=0)
    except TypeError:
        logger.warning("duplex shadow unavailable: SDK rendering signature changed")
        return False

    @wraps(original)
    def render(self, msg, *, expanded, is_human_agent, now_ms):
        text = original(self, msg, expanded=expanded, is_human_agent=is_human_agent, now_ms=now_ms)
        try:
            _submit(self, msg, expanded, is_human_agent)
            if not is_human_agent and not expanded.is_template and msg.protocol in (None, "", "text", "plain"):
                text = RoutedInput(text, InboundMessage(str(msg.message_id),
                                   str(msg.from_member_name), expanded.body))
        except Exception:
            logger.warning("duplex shadow skipped; delivery unchanged", exc_info=True)
        return text

    render._duplex_shadow = True
    MessageHandler._format_message = render

    @wraps(dispose)
    async def dispose_with_shadow(self):
        try:
            dispatcher = self.coordination.dispatcher
            handler = dispatcher.message if dispatcher is not None else None
            observer = getattr(handler, "_duplex_shadow_observer", None)
            if observer is not None:
                await observer.aclose()
        finally:
            await dispose(self)

    TeamAgent._dispose_tiny_agents = dispose_with_shadow

    deliver = TeamAgent.deliver_input

    @wraps(deliver)
    async def deliver_with_route(self, content, *, use_steer=True):
        if isinstance(content, RoutedInput):
            return await deliver_routed(self, content, use_steer=use_steer, original=deliver)
        return await deliver(self, content, use_steer=use_steer)

    TeamAgent.deliver_input = deliver_with_route
    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler
    user_input = AgentLifecycleHandler.on_user_input

    @wraps(user_input)
    async def route_user_input(self, event):
        content = event.payload.get("content", "")
        if isinstance(content, str) and not isinstance(content, RoutedInput):
            message = InboundMessage(str(event.payload.get("message_id") or f"u2a-{uuid.uuid4().hex}"),
                                     "user", content)
            event = event.model_copy(update={"payload": {**event.payload,
                                                          "content": RoutedInput(content, message)}})
        return await user_input(self, event)

    AgentLifecycleHandler.on_user_input = route_user_input
    from openjiuwen.agent_teams.harness import team_harness
    base_native = team_harness.NativeHarness

    def native_factory(*args, **kwargs):
        from jiuwenswarm.common.config import get_config
        config = get_config().get("duplex_router", {}) or {}
        if config.get("mode") == "active":
            from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness
            return DuplexNativeHarness(*args, **kwargs)
        return base_native(*args, **kwargs)

    team_harness.NativeHarness = native_factory
    return True
