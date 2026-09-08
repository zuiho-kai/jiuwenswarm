# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Versioned input transactions inside the Native supervisor.

One command owns validation, safe pause, input injection and continuation.
Lifecycle cancellation supersedes that transaction; it never restarts a stopped
agent. A tool-phase pause is completed by the supervisor's round-finished event.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.harness.control import (
    _CmdAbort, _CmdPause, _CmdResume, _CmdSend,
)
from openjiuwen.agent_teams.harness.native_harness import NativeHarness
from openjiuwen.agent_teams.harness.snapshot_rail import capture_snapshot
from openjiuwen.agent_teams.harness.state import HarnessState, InboxMessage
from openjiuwen.core.foundation.tool import Tool, ToolCard


class DeliverySuperseded(RuntimeError):
    """The lifecycle stopped a delivery; its DB row must remain unread."""


@dataclass
class _RouteInput:
    content: str
    version: str
    action: str
    message_id: str
    ack: asyncio.Future
    message_ids: tuple[str, ...] = ()


class _SteeringQueue(asyncio.Queue):
    """Retain consumed-but-uncommitted inputs across model cancellation."""

    def __init__(self):
        super().__init__()
        self.serial = 0
        self.uncommitted: dict[int, str] = {}
        self.consumed: set[int] = set()

    def put_nowait(self, item):
        self.serial += 1
        self.uncommitted[self.serial] = item
        super().put_nowait((self.serial, item))

    def get_nowait(self):
        serial, item = super().get_nowait()
        self.consumed.add(serial)
        return item

    def commit(self):
        for serial in self.consumed:
            self.uncommitted.pop(serial, None)
        self.consumed.clear()

    def rewind(self):
        # Called only after the scheduler has stopped; preserve arrival order.
        while not self.empty():
            super().get_nowait()
            self.task_done()
        for item in self.uncommitted.items():
            super().put_nowait(item)
        self.consumed.clear()


class WorkingIntentTool(Tool):
    def __init__(self, native):
        super().__init__(ToolCard(
            name="update_working_intent",
            description=("Before starting a new approach, publish your current goal, hypothesis, "
                         "next action and hard constraints for the input controller. "
                         "Use a short factual summary, never private reasoning."),
            input_params={"type": "object", "properties": {
                "goal": {"type": "string", "maxLength": 2000},
                "current_hypothesis": {"type": "string", "maxLength": 1000},
                "next_action": {"type": "string", "maxLength": 1000},
                "constraints": {"type": "array", "maxItems": 16,
                                "items": {"type": "string", "maxLength": 500}},
            }, "required": ["goal", "current_hypothesis", "next_action", "constraints"],
                "additionalProperties": False},
        ))
        self.native = native

    async def invoke(self, inputs, **kwargs):
        self.native.commit_working_intent(inputs)
        return "Working intent committed."

    async def stream(self, inputs, **kwargs):
        raise NotImplementedError("Working intent uses invoke")


class DuplexNativeHarness(NativeHarness):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._duplex_version = 0
        self._duplex_pending: _RouteInput | None = None
        self._duplex_waiting: list[Any] = []
        self._duplex_received: set[str] = set()
        self._duplex_steering: _SteeringQueue | None = None
        self._duplex_intent: dict = {}
        self._duplex_control_state: dict = {}
        self._duplex_tools: dict = {}
        from jiuwenswarm.agents.harness.team.duplex_ledger import ToolLedger
        self._duplex_ledger = ToolLedger(os.environ.get("JIUWEN_DUPLEX_LEDGER_PATH") or
                                        Path.home() / ".jiuwenswarm/duplex-state/duplex-tools.sqlite3")
        from jiuwenswarm.agents.harness.team.duplex_inbox import DurableInbox
        self._duplex_inbox = DurableInbox(os.environ.get("JIUWEN_DUPLEX_INBOX_PATH") or
                                          self._duplex_ledger.path.with_name("duplex-inbox.sqlite3"))
        self.durable_scope = None
        self._duplex_admission_inputs = {}
        self._duplex_receipt_version = 0
        execute = self.ability_manager._execute_single_tool_call

        async def execute_with_receipt(tool_call, session, tag=None):
            from jiuwenswarm.agents.harness.team.duplex_inbox import persist_native
            # Rail callbacks may be isolated by the SDK. Enforce persistence
            # here too: a failed checkpoint must never release a side effect.
            await persist_native(self)
            card = self.ability_manager.get(tool_call.name)
            return await self._duplex_ledger.execute(
                scope=self.durable_scope or session.get_session_id(), call=tool_call,
                idempotent=bool(getattr(card, "idempotent", False)),
                invoke=lambda: execute(tool_call=tool_call, session=session, tag=tag),
                capture_state=self._capture_tool_state)

        self.ability_manager._execute_single_tool_call = execute_with_receipt
        from jiuwenswarm.agents.harness.team.duplex_state import StatePublicationRail
        self.add_rail(StatePublicationRail(self))
        intent_tool = WorkingIntentTool(self)
        self.ability_manager.add_ability(intent_tool.card, intent_tool)
        # Wrap the actual phase rail before callback registration. This retains
        # the SDK's stop rules and snapshots, and works on the real inner ReAct.
        rail = self._snapshot_rail
        for name in ("before_model_call", "after_model_call", "before_tool_call",
                     "after_react_iteration"):
            original = getattr(rail, name)

            async def boundary(ctx, _original=original, _name=name):
                await _original(ctx)
                self._duplex_version += 1
                active = self.active_round
                if active is not None and ctx.session is not None and _name == "before_model_call":
                    # The query and steers have now entered the authoritative
                    # context. The pre-round baseline alone misses the first query.
                    active.last_iter_snapshot = capture_snapshot(
                        self, ctx.session, previous=active.last_iter_snapshot)
                if _name in ("before_model_call", "after_react_iteration"):
                    if self._duplex_steering is not None:
                        self._duplex_steering.commit()
                if _name in ("before_model_call", "before_tool_call", "after_react_iteration"):
                    from jiuwenswarm.agents.harness.team.duplex_inbox import persist_native
                    await persist_native(self)

            setattr(rail, name, boundary)

    async def start(self, **kwargs):
        already_started = self._st.supervisor_task is not None
        await super().start(**kwargs)
        if already_started:
            return
        self.durable_scope = self.durable_scope or f"{self.session_id}/{self.card.id}"
        self._duplex_inbox.path = Path(os.environ.get("JIUWEN_DUPLEX_INBOX_PATH") or
                                       self._duplex_ledger.path.with_name("duplex-inbox.sqlite3"))
        queues = self.event_handler.interaction_queues
        if not isinstance(queues.steering, _SteeringQueue):
            replacement = _SteeringQueue()
            while not queues.steering.empty():
                replacement.put_nowait(queues.steering.get_nowait())
            queues.steering = replacement
        self._duplex_steering = queues.steering
        admit = self.react_agent._admit_user_message

        async def admit_with_identity(ctx, context, parts, *, source, prefix=""):
            await admit(ctx, context, parts, source=source, prefix=prefix)
            if parts:
                messages = context.get_messages(with_history=False)
                if messages and messages[-1].role == "user":
                    messages[-1].metadata["duplex_input_ids"] = [
                        key for part in parts for key in self._duplex_admission_inputs.get(part, ())]

        self.react_agent._admit_user_message = admit_with_identity
        self._duplex_intent = self._session.get_state("duplex_working_intent") or {}
        self._duplex_control_state = self._session.get_state("duplex_control_state") or {}
        from jiuwenswarm.agents.harness.team.duplex_inbox import restore_native
        await restore_native(self)

    async def durable_input(self, content):
        """Fsync the complete input before any caller is allowed to ACK it."""
        from jiuwenswarm.agents.harness.team.duplex_shadow import RoutedInput
        message = content.message
        text = await asyncio.to_thread(self._duplex_inbox.accept, self.durable_scope,
                                       message.message_id, message.sender, str(content))
        self._duplex_admission_inputs[text] = (message.message_id,)
        return RoutedInput(text, message)

    def _capture_tool_state(self):
        self._duplex_receipt_version += 1
        return {"version": self._duplex_receipt_version,
                "state": self.load_state(self._session).to_session_dict(),
                "intent_state": {key: self._session.get_state(key) for key in (
                    "duplex_working_intent", "duplex_control_state", "duplex_intent_sources")}}

    def commit_working_intent(self, value):
        if not isinstance(value, dict) or set(value) != {
            "goal", "current_hypothesis", "next_action", "constraints"
        }:
            raise ValueError("invalid working intent")
        if any(not isinstance(value[k], str) for k in ("goal", "current_hypothesis", "next_action")):
            raise ValueError("working intent fields must be strings")
        constraints = value["constraints"]
        if not isinstance(constraints, list) or len(constraints) > 16 or any(
                not isinstance(item, str) or len(item) > 500 for item in constraints):
            raise ValueError("invalid constraints")
        self._duplex_intent = {k: (v[:2000] if isinstance(v, str) else list(v)) for k, v in value.items()}
        self._session.update_state({"duplex_working_intent": self._duplex_intent})
        from .duplex_state import bind_intent

        bind_intent(self, self._session)
        self._duplex_version += 1

    def _start_round(self, query, **kwargs):
        self._duplex_version += 1
        if not kwargs.get("resume_continuation"):
            self._duplex_intent = {}
            self._duplex_control_state = {}
            self._session.update_state({"duplex_working_intent": {}, "duplex_intent_sources": {}})
        return super()._start_round(query, **kwargs)

    async def route_input(self, content, *, version, action, message_id, message_ids=()):
        self._require_alive()
        if action not in ("APPEND", "INTERRUPT"):
            raise ValueError("invalid action")
        self._duplex_admission_inputs[str(content)] = tuple(message_ids) or (message_id,)
        ack = asyncio.get_running_loop().create_future()
        await self._control.put(_RouteInput(str(content), version, action, message_id, ack,
                                           tuple(message_ids) or (message_id,)))
        return await ack

    async def _dispatch(self, cmd):
        if isinstance(cmd, _RouteInput):
            await self._route(cmd)
            return
        if isinstance(cmd, (_CmdPause, _CmdAbort, _CmdResume)):
            self._reject_transaction("superseded by lifecycle control")
            self._duplex_version += 1
            if isinstance(cmd, (_CmdPause, _CmdAbort)):
                await asyncio.to_thread(self._duplex_inbox.suspend, self.durable_scope)
            elif cmd.ack is None or not cmd.ack.cancelled():
                await asyncio.to_thread(self._duplex_inbox.activate, self.durable_scope)
        elif isinstance(cmd, _CmdSend):
            if self._duplex_pending is not None:
                if len(self._duplex_waiting) >= 32:
                    self._reject_ack(cmd.ack, "input queue full")
                else:
                    self._duplex_waiting.append(cmd)
                return
            if self.state is HarnessState.PAUSING:
                self._reject_ack(cmd.ack, "runtime is being paused")
                return
            self._duplex_version += 1
            if cmd.ack is None or not cmd.ack.cancelled():
                await asyncio.to_thread(self._duplex_inbox.activate, self.durable_scope)
        await super()._dispatch(cmd)

    async def _route(self, cmd):
        from jiuwenswarm.agents.harness.team.duplex_shadow import snapshot_from_native

        if cmd.ack.cancelled():
            return
        ids = cmd.message_ids or (cmd.message_id,)
        if all(key in self._duplex_received for key in ids):
            self._ack(cmd.ack, "DUPLICATE")
            return
        if any(key in self._duplex_received for key in ids):
            self._ack(cmd.ack, "STALE")
            return
        current = snapshot_from_native(self)
        if (self.state is not HarnessState.RUNNING or current is None or
                current.context_version != cmd.version or self._duplex_pending is not None):
            self._ack(cmd.ack, "STALE")
            return
        if cmd.action == "APPEND":
            self._push_steer(cmd.content)
            self._duplex_received.update(ids)
            self._duplex_version += 1
            self._ack(cmd.ack, "APPEND")
            return
        self._duplex_pending = cmd
        pause_ack = asyncio.get_running_loop().create_future()
        await super()._on_pause(_CmdPause(ack=pause_ack))
        if self.state is HarnessState.PAUSED:
            await self._finish_transaction()

    async def _finish_transaction(self):
        cmd = self._duplex_pending
        if cmd is None:
            return
        self._duplex_pending = None
        if cmd.ack.cancelled():
            self._reject_waiting("delivery caller cancelled")
            return  # keep the safe PAUSED state; do not resurrect cancelled work
        send_ack = asyncio.get_running_loop().create_future()
        await super()._on_send(_CmdSend(msg=InboxMessage(0, cmd.content, True), ack=send_ack))
        self._duplex_received.update(cmd.message_ids or (cmd.message_id,))
        self._ack(cmd.ack, "INTERRUPT")
        waiting, self._duplex_waiting = self._duplex_waiting, []
        for send in waiting:
            if not send.ack.cancelled():
                self._duplex_version += 1
                await super()._on_send(send)

    async def _on_round_done(self, cmd):
        active = self.active_round
        pending_round = self._duplex_pending is not None and active is not None and active.round_id == cmd.round_id
        if pending_round and (cmd.error is not None or (cmd.result or {}).get("error")):
            # An uncertain tool result must not trigger the SDK's query replay.
            active.failure_retry = True
        await super()._on_round_done(cmd)
        if self.state is HarnessState.IDLE and cmd.error is None and not (cmd.result or {}).get("error"):
            from jiuwenswarm.agents.harness.team.duplex_inbox import persist_native
            await persist_native(self, running=False)
        if pending_round:
            if self.state is HarnessState.PAUSED:
                await self._finish_transaction()
            else:
                self._reject_transaction("round failed before safe restart")
        elif (active is not None and active.round_id == cmd.round_id
              and not active.graceful_abort and not active.pause_requested
              and cmd.error is None and not (cmd.result or {}).get("error")
              and self.state is HarnessState.IDLE and self._duplex_steering is not None
              and not self._duplex_steering.empty()):
            # A steer arriving during the final model call still needs a next
            # call. Otherwise the SDK can settle idle with unread steering.
            nxt = self._start_round(active.original_query, resume_continuation=True)
            await self._transition(HarnessState.RUNNING)
            await self._emit_round("started", nxt.round_id)

    async def _rollback_to_snapshot(self, snapshot):
        await super()._rollback_to_snapshot(snapshot)
        if self._duplex_steering is not None:
            self._duplex_steering.rewind()
        self._duplex_version += 1
        self._duplex_intent = self._session.get_state("duplex_working_intent") or {}
        self._duplex_tools.clear()

    @staticmethod
    def _reject_ack(ack, reason):
        if ack is not None and not ack.done():
            ack.set_exception(DeliverySuperseded(reason))

    def _reject_waiting(self, reason):
        for cmd in self._duplex_waiting:
            self._reject_ack(cmd.ack, reason)
        self._duplex_waiting.clear()

    def _reject_transaction(self, reason):
        if self._duplex_pending is not None:
            self._reject_ack(self._duplex_pending.ack, reason)
            self._duplex_pending = None
        self._reject_waiting(reason)

    async def _on_stop(self, cmd):
        await asyncio.to_thread(self._duplex_inbox.suspend, self.durable_scope)
        self._reject_transaction("runtime stopped")
        controller = getattr(self, "_duplex_controller", None)
        if controller is not None:
            await controller.aclose()
        await super()._on_stop(cmd)

    def _fail_remaining_commands(self, crashed_cmd, crash_exc):
        self._reject_transaction("supervisor stopped")
        super()._fail_remaining_commands(crashed_cmd, crash_exc)
