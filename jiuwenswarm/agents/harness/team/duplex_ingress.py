"""Keep Native input decisions off the coordination event loop."""
import asyncio
import logging
from functools import wraps

logger = logging.getLogger(__name__)


def active_config(host):
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness
    from jiuwenswarm.agents.harness.team.duplex_shadow import native_from_runtime

    config = getattr(host, "duplex_settings", None)
    if config is None:
        config = get_config().get("duplex_router", {}) or {}
    native = native_from_runtime(host.harness)
    if config.get("mode") == "active" and isinstance(native, DuplexNativeHarness):
        return config
    return None


def install_ingress(message_handler, original_deliver):
    """Retain the original drain/ACK; prefetch only routable DB rows in order."""
    original_drain = message_handler._process_unread_messages

    @wraps(original_drain)
    async def drain(self, member_name, *, use_steer=True):
        config = active_config(self._round)
        if not config or not use_steer or config.get("policy", "model") not in ("model", "always_interrupt"):
            return await original_drain(self, member_name, use_steer=use_steer)
        lock = getattr(self, "_duplex_drain_lock", None)
        if lock is None:
            lock = self._duplex_drain_lock = asyncio.Lock()
        async with lock:
            from openjiuwen.agent_teams.schema.team import TeamRole
            from openjiuwen.agent_teams.tools.database.engine import get_current_time
            from jiuwenswarm.agents.harness.team.duplex_shadow import (
                RoutedInput, input_controller, native_from_runtime,
            )
            backend = self._infra.team_backend
            if (self._blueprint.role == TeamRole.BRIDGE_AGENT or
                    (backend is not None and await backend.is_human_agent(member_name))):
                return await original_drain(self, member_name, use_steer=use_steer)

            async def prefetch():
                while True:
                    if self._round.has_pending_interrupt() or await self._harness_input_blocked(member_name):
                        return
                    native = native_from_runtime(self._round.harness)
                    if native.active_round is None:
                        return
                    controller = input_controller(self._round, config, original_deliver)
                    for msg in await self._read_all_unread(member_name):
                        if str(msg.message_id) in native._duplex_received:
                            continue
                        if msg.protocol not in (None, "", "plain", "text"):
                            break  # never overtake a control/approval/template message
                        expanded = await self._expand(msg)
                        if expanded.is_template:
                            break
                        text = self._format_message(msg, expanded=expanded,
                            is_human_agent=False, now_ms=get_current_time())
                        if isinstance(text, RoutedInput):
                            try:
                                controller.submit(text)
                            except OverflowError:
                                break  # DB remains unread; the next sweep will retry
                    await asyncio.sleep(0.05)

            watcher = asyncio.create_task(prefetch(), name="duplex-mailbox-intake")
            try:
                return await original_drain(self, member_name, use_steer=use_steer)
            finally:
                watcher.cancel()
                outcome = (await asyncio.gather(watcher, return_exceptions=True))[0]
                if isinstance(outcome, Exception):
                    logger.warning("mailbox prefetch failed; original drain retained", exc_info=outcome)

    message_handler._process_unread_messages = drain

    from openjiuwen.agent_teams.agent.coordination.event_bus import EventBus, InnerEventType
    from openjiuwen.agent_teams.schema.events import TeamEvent
    original_start, original_stop = EventBus.start, EventBus.stop
    input_events = {TeamEvent.MESSAGE, TeamEvent.BROADCAST,
                    InnerEventType.POLL_MAILBOX, InnerEventType.USER_INPUT}

    @wraps(original_start)
    async def start(bus, *, wake_callback=None):
        owner = getattr(wake_callback, "__self__", None)
        handler = getattr(owner, "message", None)
        if handler is None:
            return await original_start(bus, wake_callback=wake_callback)
        bus._duplex_input_tasks = set()

        def completed(task):
            bus._duplex_input_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logger.error("input dispatch failed; unacknowledged DB rows retained",
                             exc_info=task.exception())

        async def dispatch(event):
            if event.event_type in input_events and active_config(handler._round):
                task = asyncio.create_task(wake_callback(event), name="duplex-input-dispatch")
                bus._duplex_input_tasks.add(task)
                task.add_done_callback(completed)
                return
            await wake_callback(event)  # lifecycle/system events are never model-routed

        return await original_start(bus, wake_callback=dispatch)

    @wraps(original_stop)
    async def stop(bus):
        try:
            await original_stop(bus)
        finally:
            tasks = tuple(getattr(bus, "_duplex_input_tasks", ()))
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    EventBus.start, EventBus.stop = start, stop
