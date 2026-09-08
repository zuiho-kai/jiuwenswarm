"""Official transport inputs enter Jiuwen DB before Native execution."""
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from functools import wraps

from jiuwenswarm.benchmarks.duplex_runtime import NativePeer


def team_context(method):
    """Bind the SDK's dynamic message-table namespace for every external entry."""
    @wraps(method)
    async def scoped(self, *args, **kwargs):
        from openjiuwen.agent_teams.context import set_session_id, reset_session_id
        token = set_session_id(self.team)
        try:
            return await method(self, *args, **kwargs)
        finally:
            reset_session_id(token)
    return scoped


class DatabasePeer(NativePeer):
    def __init__(self, *, database, team, **kwargs):
        super().__init__(**kwargs)
        self.database, self.team = Path(database), team
        self.db = None
        self.duplex_settings = {"mode": "active", "policy": self.policy,
                                "model_name": "fast"}

    def has_pending_interrupt(self):
        check = getattr(self.harness, "has_pending_interrupt", None)
        return bool(check()) if check else False

    def has_in_flight_round(self):
        return self.harness.active_round is not None

    async def deliver_input(self, content, *, use_steer=True):
        from openjiuwen.agent_teams.agent.team_agent import TeamAgent
        if self.policy == "abort_restart":
            return await super().receive(str(content), message_id=content.message.message_id)
        return await TeamAgent.deliver_input(self, content,
                                            use_steer=use_steer and self.policy != "serial")

    @team_context
    async def start(self):
        from filelock import FileLock
        from openjiuwen.agent_teams.messager import InProcessMessager
        from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
        from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
        from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
        from openjiuwen.agent_teams.agent.coordination.event_bus import EventBus
        from openjiuwen.agent_teams.schema.events import TeamTopic
        from openjiuwen.agent_teams.schema.team import TeamRole
        from jiuwenswarm.agents.harness.team.duplex_shadow import install_shadow_observer

        install_shadow_observer()
        # Stable across worker restarts, isolated across databases, members and
        # benchmark policies. Bind before Native restores its durable inbox.
        self.harness.durable_scope = "agentradio:" + uuid.uuid5(uuid.NAMESPACE_URL,
            f"{self.database.resolve()}/{self.team}/{self.name}/{self.policy}").hex
        await super().start()
        if self.db is not None:  # abort reference rebuilds only its executor
            return
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE,
                                             connection_string=str(self.database)))
        lock = FileLock(str(self.database) + ".init.lock", thread_local=False)
        await asyncio.to_thread(lock.acquire, timeout=60)
        try:
            await self.db.initialize()
            # The schema's create methods are idempotent on existing identities.
            if await self.db.team.get_team(self.team) is None:
                await self.db.team.create_team(team_name=self.team, display_name=self.team,
                                              leader_member_name="agent-1")
            if await self.db.member.get_member(self.name, self.team) is None:
                from openjiuwen.core.single_agent.schema.agent_card import AgentCard
                await self.db.member.create_member(member_name=self.name, team_name=self.team,
                    display_name=self.name, status="busy",
                    agent_card=AgentCard(id=self.name, name=self.name).model_dump_json())
        finally:
            await asyncio.to_thread(lock.release)
        self.messager = InProcessMessager()
        self.manager = TeamMessageManager(team_name=self.team, db=self.db,
            messager=self.messager, member_name=self.name)
        self.bus = EventBus(role=TeamRole.LEADER)
        blueprint = SimpleNamespace(role=TeamRole.LEADER, member_name=self.name,
                                     language="en", team_spec=None)
        self.handler = MessageHandler(self, blueprint,
            SimpleNamespace(message_manager=self.manager, team_backend=None), SimpleNamespace())
        self.handler._poll = self.bus
        peer = self

        class Dispatch:
            message = peer.handler

            async def dispatch(self, event):
                await peer.handler.on_message_or_broadcast(event)

        self.topic = TeamTopic.MESSAGE.build(self.team, self.team)
        await self.bus.start(wake_callback=Dispatch().dispatch)
        await self.messager.subscribe(self.topic, self.bus.enqueue)
        self.poller = asyncio.create_task(self._poll(), name="jiuwen-benchmark-db-poll")

    async def _poll(self):
        while True:
            try:
                await self.handler.on_poll_mailbox(None)
            except Exception as error:
                self.events.add("mailbox_poll_error", error_type=type(error).__name__)
            await asyncio.sleep(0.25)

    @team_context
    async def receive(self, content, *, message_id, sender="coral"):
        from openjiuwen.agent_teams.schema.events import EventMessage, MessageEvent
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.team}/{self.name}/{message_id}"))
        row = await self.db.message.get_message(identity)
        if row is None:
            created = await self.db.message.create_message(message_id=identity, team_name=self.team,
                from_member_name=sender, to_member_name=self.name, content=content,
                broadcast=False, is_read=False, protocol="plain")
            if not created:
                raise RuntimeError("Jiuwen could not persist the official transport input")
        elif row.content != content or row.from_member_name != sender:
            raise ValueError("external message identity reused with different content")
        elif row.is_read:
            return
        self.events.add("message_arrived", member=self.name, message_id=identity)
        await self.messager.publish(topic_id=self.topic, message=EventMessage.from_event(MessageEvent(
            message_id=identity, team_name=self.team, from_member_name=sender, to_member_name=self.name)))
        # Original SDK drain owns rendering, ordering and ACK after delivery.
        await self.handler._process_unread_messages(self.name)
        row = await self.db.message.get_message(identity)
        if not row.is_read:
            raise RuntimeError("official transport input remains unread")
        self.events.add("message_accepted", member=self.name, message_id=identity)

    @team_context
    async def messages(self):
        return await self.db.message.get_team_messages(self.team)

    @team_context
    async def close(self):
        if self.db is not None:
            poller = getattr(self, "poller", None)
            if poller is not None:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
            if hasattr(self, "topic"):
                await self.messager.unsubscribe(self.topic)
            if hasattr(self, "bus"):
                await self.bus.stop()
        try:
            await super().close()
        finally:
            if self.db is not None:
                await self.db.close()
                self.db = None
