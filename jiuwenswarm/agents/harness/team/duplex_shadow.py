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
import uuid
from dataclasses import replace
from functools import wraps
from typing import Any

from jiuwenswarm.common.delivery_policy import (
    SRC_BROADCAST, SRC_DM, SRC_GROUP_MENTION, SRC_GROUP_PLAIN, SRC_USER_TEXT, TargetState,
    final_action, plan, resolve,
)
from jiuwenswarm.common.duplex_classifier import ClassifierConfigError, classify, provider_label
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage

from .duplex_delivery import (
    RUN_DELIVER, RUN_INTERRUPT, RUN_JOINED, RUN_REJECT, RUN_STEER, RUN_VISIBLE, SCHEDULER,
    envelope_for, rate_limited_decision, runtime_action, settle_joined,
)

logger = logging.getLogger(__name__)


class RoutedInput(str):
    """Original SDK rendering plus DB identity and policy source; not a second history."""

    def __new__(cls, text, message, source=SRC_DM):
        obj = super().__new__(cls, text)
        obj.message = message
        obj.source = source
        return obj


def native_from_runtime(runtime):
    from openjiuwen.agent_teams.harness.native_harness import NativeHarness
    from openjiuwen.agent_teams.harness.team_harness import TeamHarness

    if isinstance(runtime, TeamHarness):
        runtime = runtime.inner_agent
    return runtime if isinstance(runtime, NativeHarness) else None


def snapshot_from_native(harness: Any) -> ControlSnapshot | None:
    """Read the existing task/plan and execution position without asking the model."""
    active = harness.active_round
    if active is None:
        return None
    checkpoint = active.last_iter_snapshot or active.pre_round_snapshot
    if checkpoint is None:
        return None
    plan = checkpoint.deep_agent_state.get("task_plan") or {}
    if getattr(harness, "_session", None) is not None:
        plan = harness.load_state(harness._session).to_session_dict().get("task_plan") or {}
    current = next((task for task in plan.get("tasks", [])
                    if task.get("id") == plan.get("current_task_id")), {})
    query = active.original_query
    if isinstance(query, list):
        query = "\n".join(str(item) for item in query)
    goal = str(plan.get("goal") or (query if isinstance(query, str) else ""))
    next_action = str(current.get("description") or current.get("content") or "")
    phase = str(getattr(active.iter_phase, "value", active.iter_phase))
    last_action = ""
    version = hashlib.sha256(json.dumps([
        id(active), id(checkpoint), phase, active.pause_requested, goal, next_action, last_action,
        getattr(getattr(harness, "_st", None), "seq_counter", 0),
    ]).encode()).hexdigest()[:20]
    return ControlSnapshot(context_version=version, round_id=str(active.round_id),
        checkpoint_id=f"{active.round_id}:{checkpoint.iteration_index}",
        phase=phase, goal=goal, next_action=next_action, last_action=last_action)


def _duplex_context(host: Any, native: Any | None = None) -> tuple[str, str]:
    """Resolve the Web push routing identifiers without exposing provider details."""
    session_id = ""
    for owner in (host, native, getattr(host, "harness", None)):
        if owner is None:
            continue
        try:
            value = getattr(owner, "session_id", None)
            if callable(value):
                value = value()
            if value:
                session_id = str(value).strip()
                if session_id:
                    break
        except Exception:
            continue
    channel_id = ""
    for owner in (host, native):
        if owner is None:
            continue
        try:
            value = getattr(owner, "channel_id", None)
            if value:
                channel_id = str(value).strip()
                if channel_id:
                    break
        except Exception:
            continue
    return session_id, channel_id or "web"


def _duplex_error_text(backend: str, phase: str, exc: BaseException | None = None,
                       status: str | None = None) -> str:
    backend_label = {"jev": "Jev", "clef": "Cloudflare Clef",
                     "team_model": "团队模型", "openai": "OpenAI", "custom": "自定义模型",
                     }.get(str(backend).lower(), str(backend or "未知"))
    reason = status or type(exc).__name__ if exc is not None else status or "响应失败"
    if phase == "interrupt":
        return "A2A 监工异常：打断投递失败，当前消息未送达，请重新发送。"
    if phase == "config":
        detail = "配置无效"
    elif reason in {"timeout", "TimeoutError", "ClassifierTimeoutError"}:
        detail = "请求超时"
    elif reason in {"error", "ProviderError", "InvalidResponseError"}:
        detail = "连接或响应解析失败"
    else:
        detail = "调用失败"
    return f"A2A 监工异常：{backend_label}{detail}，当前消息已按追加方式继续处理。"


async def _emit_duplex_error(host: Any, native: Any | None, *, message_id: str,
                             backend: str, phase: str, exc: BaseException | None = None,
                             status: str | None = None) -> None:
    """Make supervisor failures visible while keeping the worker's APPEND fallback."""
    session_id, channel_id = _duplex_context(host, native)
    if not session_id:
        logger.warning("duplex error notice skipped: missing session_id message_id=%s phase=%s",
                       message_id, phase)
        return
    try:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        request_id = f"duplex-error-{message_id}"
        sent = await asyncio.wait_for(
            AgentWebSocketServer.get_instance().send_push({
                "request_id": request_id,
                "channel_id": channel_id,
                "session_id": session_id,
                "payload": {
                    "event_type": "chat.notice",
                    "notice_type": "duplex_error",
                    "source": "duplex_router",
                    "backend": str(backend),
                    "phase": phase,
                    "content": _duplex_error_text(backend, phase, exc, status),
                    "request_id": request_id,
                    "session_id": session_id,
                },
                "is_complete": False,
            }),
            timeout=1.0,
        )
        logger.info("duplex error notice sent backend=%s phase=%s message_id=%s sent=%s",
                    backend, phase, message_id, sent)
    except Exception:
        logger.warning("duplex error notice failed backend=%s phase=%s message_id=%s",
                       backend, phase, message_id, exc_info=True)


def _switch_enabled(config: dict) -> bool:
    """``enabled`` parsing shared by delivery and harness construction; absent means on."""
    enabled = config.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    return bool(enabled)


def duplex_active(config: dict) -> bool:
    """The duplex runtime is used only when enabled AND ``mode == "active"``."""
    return _switch_enabled(config) and config.get("mode") == "active"


async def deliver_routed(host, content, *, use_steer, original, settings=None):
    from jiuwenswarm.common.config import get_config
    from .duplex_native import DuplexNativeHarness

    config = settings
    if config is None:
        config = get_config().get("duplex_router", {}) or {}
    native = native_from_runtime(host.harness)
    message = content.message
    backend = provider_label(config)
    if not _switch_enabled(config):
        logger.info("duplex route bypass backend=%s message_id=%s reason=disabled", backend, message.message_id)
        return await original(host, str(content), use_steer=use_steer)
    logger.info("duplex route entry backend=%s mode=%s recipient=%s message_id=%s sender=%s "
                "use_steer=%s harness_type=%s",
                backend, config.get("mode", "off"),
                getattr(getattr(host, "blueprint", None), "member_name", "unknown"),
                message.message_id, message.sender, use_steer, type(native).__name__)
    if config.get("mode") != "active" or not isinstance(native, DuplexNativeHarness):
        logger.info("duplex route bypass backend=%s message_id=%s reason=%s",
                    backend, message.message_id,
                    "mode_not_active" if config.get("mode") != "active" else "unsupported_harness")
        return await original(host, str(content), use_steer=use_steer)
    if not use_steer:
        # SDK reminders and nudges append by design; they are never judged.
        logger.info("duplex route bypass backend=%s message_id=%s reason=use_steer_false",
                    backend, message.message_id)
        return await original(host, str(content), use_steer=use_steer)
    if message.message_id in native._duplex_received:
        logger.info("duplex route bypass backend=%s message_id=%s reason=duplicate", backend, message.message_id)
        return

    async def send(steer: bool, text: str | None = None):
        result = await original(host, str(content) if text is None else text, use_steer=steer)
        native._duplex_received.add(message.message_id)
        return result

    target = str(getattr(getattr(host, "blueprint", None), "member_name", "") or "unknown")
    env = envelope_for(message, source=content.source, target=target)
    state = TargetState(idle=_target_idle(native))
    decided = plan(env, state)
    snapshot = None
    outcome = {}

    def decide(answers, failure=None):
        decision = resolve(env, decided, answers, state)
        action = final_action(decision, target_idle_now=_target_idle(native))
        outcome.update(decision=decision, action=action)
        return runtime_action(env, decided, decision, action)

    verdict = None
    if decided.questions and env.source == SRC_BROADCAST and not SCHEDULER.allow_broadcast(target, env.sender):
        decision = rate_limited_decision(env, state.idle)
        action = final_action(decision, target_idle_now=_target_idle(native))
        run = runtime_action(env, decided, decision, action)
    elif decided.questions:
        snapshot = snapshot_from_native(native) or _placeholder_snapshot(state.idle)
        asked = snapshot

        async def ask(questions, messages):
            return await classify(host, config, asked, messages, questions)

        logger.info("duplex route start backend=%s message_id=%s round_id=%s checkpoint_id=%s "
                    "phase=%s questions=%s", backend, message.message_id, snapshot.round_id,
                    snapshot.checkpoint_id, snapshot.phase, ",".join(decided.questions))
        verdict = await SCHEDULER.judge(target, decided.questions, _classifier_message(message), ask,
                                        content=str(content), decide=decide)
        if verdict.failure is not None:
            logger.warning("duplex route fallback message_id=%s error_type=%s",
                           message.message_id, type(verdict.failure).__name__)
        if verdict.run is None:  # runner stopped before deciding
            verdict = replace(verdict, run=decide(None))
        run = verdict.run
        decision, action = outcome["decision"], outcome["action"]
    else:
        run = decide(None)
        decision, action = outcome["decision"], outcome["action"]
    logger.info("duplex decision backend=%s message_id=%s sender=%s recipient=%s final=%s run=%s %s",
                backend, message.message_id, message.sender, target, action.value, run, decision.reason)
    if run == RUN_INTERRUPT:
        # Before any await: joined batch waiters depend on this leader.
        return await _interrupt(host, native, content, message, verdict, snapshot, send, backend)
    if verdict is not None and verdict.failure is not None:
        await _emit_duplex_error(host, native, message_id=message.message_id, backend=backend,
                                 phase="config" if isinstance(verdict.failure, ClassifierConfigError)
                                 else "route", exc=verdict.failure)
    if run == RUN_REJECT:
        return None
    if run == RUN_VISIBLE:
        # Returning without delivery lets the SDK drain mark the row read; the
        # row stays in the team message history, the agent is not woken.
        native._duplex_received.add(message.message_id)
        return None
    if run == RUN_JOINED:
        # The batch leader's single interrupt carries this message.
        effective = await asyncio.shield(verdict.joined)
        native._duplex_received.add(message.message_id)
        logger.info("duplex route effective backend=%s message_id=%s action=%s joined=true",
                    backend, message.message_id, effective)
        return effective
    return await send(use_steer if run == RUN_DELIVER else run == RUN_STEER)


async def _interrupt(host, native, content, message, verdict, snapshot, send, backend):
    """Run ONE safe interrupt carrying every interrupting message of the batch, in order."""
    merged = verdict.merged if verdict is not None and verdict.merged else (
        (message.message_id, str(content)),)
    joined = verdict.joined if verdict is not None else None
    text = "\n\n".join(part for _, part in merged)
    ids = [mid for mid, _ in merged]
    try:
        version = (snapshot_from_native(native) or snapshot or _placeholder_snapshot(False)).context_version
        try:
            effective = await native.interrupt(text, version=version, message_id=message.message_id)
        except Exception as exc:
            # No steer fallback: it would override a pause/stop or double-deliver a pushed replan.
            # A2A rows stay unread and are redelivered; U2A input has no row, so tell the user.
            if message.sender == "user":
                await _emit_duplex_error(host, native, message_id=message.message_id,
                                         backend=str(backend), phase="interrupt", exc=exc)
            logger.warning("duplex route interrupt failed backend=%s message_id=%s sender=%s error_type=%s",
                           backend, message.message_id, message.sender, type(exc).__name__)
            raise
        if effective == "UNAVAILABLE":
            result = await send(True, text)
            logger.info("duplex route effective backend=%s message_id=%s action=STEER "
                        "reason=inactive_or_pending batch=%d", backend, message.message_id, len(ids))
            effective = result
        else:
            logger.info("duplex route effective backend=%s message_id=%s action=%s batch=%d",
                        backend, message.message_id, effective, len(ids))
        native._duplex_received.update(ids)
    except BaseException as exc:
        settle_joined(joined, error=exc if isinstance(exc, Exception)
                      else RuntimeError("duplex interrupt leader cancelled"))
        raise
    settle_joined(joined, result=effective)
    return effective


def _target_idle(native: Any) -> bool:
    return getattr(native, "active_round", None) is None


def _placeholder_snapshot(idle: bool) -> ControlSnapshot:
    """Context for a target with no checkpoint yet (idle, or a round just starting)."""
    phase = "idle" if idle else "starting"
    return ControlSnapshot(context_version=phase, round_id="", checkpoint_id="", phase=phase)


def _classifier_message(message: InboundMessage) -> InboundMessage:
    """Name the sender so the classifier can tell the user from a teammate."""
    return InboundMessage(message.message_id, message.sender,
                          f"from {message.sender}: {message.content}")


_NATIVE_METHODS = (
    "_require_alive", "_dispatch", "_ack", "_on_pause", "_on_send", "_on_round_done",
    "_rollback_to_snapshot", "_on_stop", "_fail_remaining_commands", "load_state", "save_state",
    "react_agent", "active_round", "state",
)


def _check_sdk() -> dict[str, Any]:
    """Import and verify every private SDK name the duplex code relies on.

    Returns the hook points to patch; raises on the first missing or changed
    name so that install applies all patches or none.
    """
    from dataclasses import fields

    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler
    from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    from openjiuwen.agent_teams.constants import USER_PSEUDO_MEMBER_NAME
    from openjiuwen.agent_teams.group_chat.handler import group_metadata  # noqa: F401
    from openjiuwen.agent_teams.group_chat.message_handler import GroupMessageHandler
    from openjiuwen.agent_teams.harness import team_harness
    from openjiuwen.agent_teams.harness.control import _CmdPause, _CmdSend
    from openjiuwen.agent_teams.harness.native_harness import NativeHarness
    from openjiuwen.agent_teams.harness.state import ActiveRound, HarnessInternalState, HarnessState, InboxMessage
    from openjiuwen.agent_teams.harness.team_harness import TeamHarness
    from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
    from openjiuwen.harness.schema.state import DeepAgentState

    from . import duplex_native  # noqa: F401  (imports the SDK control/state privates)

    def require(ok: bool, what: str) -> None:
        if not ok:
            raise AttributeError(what)

    def signature_binds(fn, *args, **kwargs) -> bool:
        try:
            inspect.signature(fn).bind(*args, **kwargs)
            return True
        except (TypeError, ValueError):
            return False

    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    render_kwargs = {"expanded": None, "is_human_agent": False, "now_ms": 0}
    base_render = MessageHandler.__dict__.get("_format_message")
    group_render = GroupMessageHandler.__dict__.get("_format_message")
    require(base_render is not None and signature_binds(base_render, None, None, **render_kwargs),
            "MessageHandler._format_message signature")
    require(group_render is not None and signature_binds(group_render, None, None, **render_kwargs),
            "GroupMessageHandler._format_message signature")
    require(signature_binds(TeamAgent.deliver_input, None, None, use_steer=True), "TeamAgent.deliver_input")
    require(signature_binds(AgentLifecycleHandler.on_user_input, None, None),
            "AgentLifecycleHandler.on_user_input")
    require(isinstance(team_harness.NativeHarness, type) or getattr(team_harness.NativeHarness,
                                                                    "_duplex_shadow", False),
            "team_harness.NativeHarness")
    require(isinstance(TeamHarness.__dict__.get("inner_agent"), property), "TeamHarness.inner_agent")
    for name in _NATIVE_METHODS:
        require(hasattr(NativeHarness, name), f"NativeHarness.{name}")
    # Instance attributes have no class-level presence; check where the SDK assigns them.
    init_source = inspect.getsource(NativeHarness.__init__)
    require("self._control" in init_source and "self._st" in init_source, "NativeHarness._control/_st")
    require("self._session =" in inspect.getsource(NativeHarness), "NativeHarness._session")
    require({"paused_query", "seq_counter", "active", "phase"} <= set(field_names(HarnessInternalState)),
            "HarnessInternalState fields")
    require({"round_id", "original_query", "last_iter_snapshot", "pre_round_snapshot", "iter_phase",
             "pause_requested"} <= set(field_names(ActiveRound)), "ActiveRound fields")
    require(hasattr(HarnessState, "RUNNING") and hasattr(HarnessState, "PAUSED"), "HarnessState")
    require(field_names(InboxMessage)[:3] == ["seq", "content", "immediate"], "InboxMessage fields")
    require({"msg", "ack"} <= set(field_names(_CmdSend)) and "ack" in field_names(_CmdPause),
            "_CmdSend/_CmdPause fields")
    require(signature_binds(ReActAgent._handle_context_abort, None, None, marker="", commit_session=False),
            "ReActAgent._handle_context_abort")
    require("task_plan" in field_names(DeepAgentState) and hasattr(DeepAgentState, "to_session_dict"),
            "DeepAgentState.task_plan")
    return {"MessageHandler": MessageHandler, "GroupMessageHandler": GroupMessageHandler,
            "TeamAgent": TeamAgent, "AgentLifecycleHandler": AgentLifecycleHandler,
            "team_harness": team_harness, "user": USER_PSEUDO_MEMBER_NAME}


def install_shadow_observer() -> bool:
    """Install the routing shim all-or-nothing; unsupported SDKs keep their old behavior.

    Rendering is the narrow synchronous point after DB read/expansion and before
    deliver_input. Inheriting the drain avoids duplicating ACK/watermark logic.
    """
    try:
        sdk = _check_sdk()
    except Exception as exc:
        logger.warning("duplex shadow unavailable: SDK check failed (%s: %s); no patch applied",
                       type(exc).__name__, exc)
        return False
    MessageHandler, GroupMessageHandler = sdk["MessageHandler"], sdk["GroupMessageHandler"]
    TeamAgent, AgentLifecycleHandler = sdk["TeamAgent"], sdk["AgentLifecycleHandler"]
    team_harness, user_name = sdk["team_harness"], sdk["user"]
    if getattr(MessageHandler._format_message, "_duplex_shadow", False):
        return True

    from openjiuwen.agent_teams.group_chat.handler import group_metadata

    original = MessageHandler._format_message

    @wraps(original)
    def render(self, msg, *, expanded, is_human_agent, now_ms):
        text = original(self, msg, expanded=expanded, is_human_agent=is_human_agent, now_ms=now_ms)
        try:
            if not is_human_agent and not expanded.is_template and msg.protocol in (None, "", "text", "plain"):
                sender = str(msg.from_member_name)
                # User @member DMs and user broadcasts are user input: never judged.
                source = (SRC_USER_TEXT if sender == user_name
                          else SRC_BROADCAST if msg.broadcast else SRC_DM)
                text = RoutedInput(text, InboundMessage(str(msg.message_id), sender, expanded.body), source)
        except Exception:
            logger.warning("duplex shadow skipped; delivery unchanged", exc_info=True)
        return text

    original_group = GroupMessageHandler.__dict__["_format_message"]

    @wraps(original_group)
    def render_group(self, msg, *, expanded, is_human_agent, now_ms):
        """Route group-chat @-mentions like direct A2A messages.

        GroupMessageHandler returns the excerpted body for group rows without
        calling super, so the base patch never sees them. Delivery keeps the SDK
        text (excerpt included); the classifier gets only the raw new message,
        because the excerpt repeats older chat turns the snapshot does not need.
        """
        text = original_group(self, msg, expanded=expanded, is_human_agent=is_human_agent, now_ms=now_ms)
        if isinstance(text, RoutedInput):  # non-group rows went through the patched base
            return text
        try:
            meta = group_metadata(msg) if msg.broadcast else {}
            if not is_human_agent and meta:
                sender = str(msg.from_member_name)
                mentioned = self._blueprint.member_name in (meta.get("mentions") or ())
                # The SDK feeds a group post only to mentioned members; a user
                # post that reaches one is user input.
                source = (SRC_GROUP_PLAIN if not mentioned
                          else SRC_USER_TEXT if sender == user_name else SRC_GROUP_MENTION)
                text = RoutedInput(text, InboundMessage(str(msg.message_id), sender,
                                                        str(msg.content or "")), source)
        except Exception:
            logger.warning("duplex group shadow skipped; delivery unchanged", exc_info=True)
        return text

    deliver = TeamAgent.deliver_input

    @wraps(deliver)
    async def deliver_with_route(self, content, *, use_steer=True):
        if isinstance(content, RoutedInput):
            return await deliver_routed(self, content, use_steer=use_steer, original=deliver)
        logger.info("duplex delivery bypass recipient=%s reason=unwrapped_input use_steer=%s input_type=%s",
                    getattr(getattr(self, "blueprint", None), "member_name", "unknown"),
                    use_steer, type(content).__name__)
        return await deliver(self, content, use_steer=use_steer)

    user_input = AgentLifecycleHandler.on_user_input

    @wraps(user_input)
    async def route_user_input(self, event):
        content = event.payload.get("content", "")
        logger.info("duplex user input entry recipient=%s input_type=%s already_wrapped=%s",
                    getattr(getattr(self, "_blueprint", None), "member_name", "unknown"),
                    type(content).__name__, isinstance(content, RoutedInput))
        if isinstance(content, str) and not isinstance(content, RoutedInput):
            message = InboundMessage(str(event.payload.get("message_id") or f"u2a-{uuid.uuid4().hex}"),
                                     "user", content)
            event = event.model_copy(update={"payload": {**event.payload,
                                                          "content": RoutedInput(content, message, SRC_USER_TEXT)}})
            logger.info("duplex user input wrapped message_id=%s sender=user", message.message_id)
        return await user_input(self, event)

    base_native = team_harness.NativeHarness

    def native_factory(*args, **kwargs):
        from jiuwenswarm.common.config import get_config
        if duplex_active(get_config().get("duplex_router", {}) or {}):
            from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness
            return DuplexNativeHarness(*args, **kwargs)
        return base_native(*args, **kwargs)

    for fn in (render, render_group, deliver_with_route, route_user_input, native_factory):
        fn._duplex_shadow = True
    MessageHandler._format_message = render
    GroupMessageHandler._format_message = render_group
    TeamAgent.deliver_input = deliver_with_route
    AgentLifecycleHandler.on_user_input = route_user_input
    team_harness.NativeHarness = native_factory
    return True
