"""Real SQLite -> SDK mailbox -> TinyAgent HTTP -> Native/ReAct/tools -> ACK.

Only model responses are scripted. No dispatcher, harness, context, tool
executor, fast-model adapter, or message-manager method is mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiohttp import web

from openjiuwen.agent_teams.context import set_session_id, reset_session_id
from openjiuwen.agent_teams.messager import InProcessMessager
from openjiuwen.agent_teams.harness.team_harness import TeamHarness
from openjiuwen.agent_teams.harness.state import HarnessState
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.agent.team_agent import TeamAgent
from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.factory import DeepAgentParts
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

from jiuwenswarm.agents.harness.team.duplex_shadow import install_shadow_observer, snapshot_from_native
from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness, DeliverySuperseded


class Endpoint:
    def __init__(self):
        self.calls = []
        self.fast_action = "INTERRUPT"
        self.fast_error = False
        self.fast_gate = asyncio.Event()
        self.fast_gate.set()
        self.model_gate = asyncio.Event()
        self.model_entered = asyncio.Event()
        self.fast_entered = asyncio.Event()
        self.block_model = True
        self.block_prompt = None
        self.tool_first = True
        self.intent_first = False
        self.repeat_tool_without_result = False
        self.final_content = "Finished using PostgreSQL."

    async def handle(self, request):
        body = await request.json()
        if any(isinstance(m.get("content"), list) and any(
                p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict))
               for m in body["messages"]):
            return web.json_response({"id": "probe", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "red"},
                             "finish_reason": "stop"}], "usage": {"total_tokens": 1}})
        self.calls.append(body)
        model = body["model"]
        call = sum(c["model"] == model for c in self.calls)
        slow_call = call - int(self.intent_first)
        message = {"role": "assistant", "content": self.final_content}
        if model == "fast":
            self.fast_entered.set()
            await self.fast_gate.wait()
            if self.fast_error:
                return web.json_response({"error": {"message": "scripted failure"}}, status=500)
            function = next(t["function"] for t in body["tools"]
                            if t["function"]["name"] == "structured_output")
            props = function["parameters"]["properties"]
            result = {key: props[key]["enum"][0]
                      for key in ("context_version", "round_id", "checkpoint_id")}
            result["action"] = self.fast_action
            message = self.tool_message("structured_output", result)
        elif self.intent_first and call == 1:
            message = self.tool_message("update_working_intent", {
                "goal": "Order event system", "current_hypothesis": "Use Kafka",
                "next_action": "Implement producer", "constraints": ["Existing infrastructure only"]})
        elif self.tool_first and (slow_call == 1 or (self.repeat_tool_without_result and not any(
                m.get("role") == "tool" and "committed exactly once" in str(m.get("content"))
                for m in body["messages"]))):
            message = self.tool_message("write_once", {})
        elif self.block_model and (
                self.block_prompt in json.dumps(body["messages"]) if self.block_prompt is not None
                else slow_call == (2 if self.tool_first else 1)):
            self.model_entered.set()
            await self.model_gate.wait()
            message["content"] = "Obsolete Kafka answer."
        if not body.get("stream"):
            return web.json_response({"id": "test", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason":
                             "tool_calls" if "tool_calls" in message else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"][0]["index"] = 0
        chunk = {"id": "test", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason":
                             "tool_calls" if "tool_calls" in message else "stop"}]
        await response.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        await response.write_eof()
        return response

    @staticmethod
    def tool_message(name, arguments):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": uuid.uuid4().hex, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class WriteOnce(Tool):
    def __init__(self, path):
        super().__init__(ToolCard(name="write_once", description="Record an irreversible operation",
                                 input_params={"type": "object", "properties": {}}))
        self.path = path
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()
        self.cancelled = 0

    async def invoke(self, inputs, **kwargs):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("committed\n")
        self.entered.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return "Irreversible operation committed exactly once."

    async def stream(self, inputs, **kwargs):
        raise NotImplementedError


class Host:
    # Use the production SDK delivery entry, including the installed adapter.
    def __init__(self, harness, fast_config):
        self.harness = harness
        self.blueprint = NS(member_name="A2")
        self.tiny_agent_model_resolver = lambda name: fast_config if name == "fast" else None

    def has_pending_interrupt(self):
        return self.harness.has_pending_interrupt()

    async def deliver_input(self, content, *, use_steer=True):
        return await TeamAgent.deliver_input(self, content, use_steer=use_steer)


async def wait_until(predicate, timeout=6):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest_asyncio.fixture
async def world(tmp_path, monkeypatch, request):
    from jiuwenswarm.common import config
    monkeypatch.setenv("JIUWEN_DUPLEX_LEDGER_PATH", str(tmp_path / "tool-ledger.sqlite3"))

    settings = {"duplex_router": {"mode": "active", "model_name": "fast", "timeout_seconds": 2}}
    settings["duplex_router"].update(getattr(request, "param", {}))
    monkeypatch.setattr(config, "get_config", lambda: settings)
    install_shadow_observer()
    endpoint = Endpoint()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint.handle)
    server = web.AppRunner(app, shutdown_timeout=0.1)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = ModelClientConfig(client_provider="OpenAI", api_base=f"http://127.0.0.1:{port}/v1",
                              api_key="local-test", max_retries=0)
    fast = TeamModelConfig(model_client_config=client,
                          model_request_config=ModelRequestConfig(model_name="fast"))
    slow = Model(model_client_config=client, model_config=ModelRequestConfig(model_name="slow"))
    tool = WriteOnce(tmp_path / "effects.txt")
    card = AgentCard(id=uuid.uuid4().hex, name="duplex-e2e")

    class Spec:
        def resolve_parts(self, context=None):
            return DeepAgentParts(config=DeepAgentConfig(card=card, model=slow,
                system_prompt="Complete the task using tools. Respect new constraints.",
                enable_task_loop=True, max_iterations=8),
                rails=[], tool_cards=[tool.card], tool_instances=[tool])

    await Runner.start()
    harness = TeamHarness.build(agent_spec=Spec(), role=TeamRole.LEADER, member_name="A2")
    token = set_session_id("duplex_" + uuid.uuid4().hex)
    db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE,
                                     connection_string=str(tmp_path / "messages.db")))
    await db.initialize()
    await db.team.create_team(team_name="duplex", display_name="duplex", leader_member_name="A2")
    for member in ("A1", "A2"):
        await db.member.create_member(member_name=member, team_name="duplex", display_name=member,
                                      agent_card=card.model_dump_json(), status="busy")
    # No subscriber by default: those cases exercise DB polling after event loss.
    messager = InProcessMessager()
    manager = TeamMessageManager(team_name="duplex", db=db, messager=messager, member_name="A1")
    host = Host(harness, fast)
    blueprint = NS(role=TeamRole.LEADER, member_name="A2", language="en", team_spec=None)
    handler = MessageHandler(host, blueprint, NS(message_manager=manager, team_backend=None), NS())
    chunks = []
    await harness.start()

    async def collect():
        async for chunk in harness.outputs():
            chunks.append(chunk)

    collector = asyncio.create_task(collect())
    result = NS(endpoint=endpoint, tool=tool, harness=harness, native=harness.inner_agent,
                manager=manager, handler=handler, settings=settings, chunks=chunks, host=host,
                messager=messager)
    try:
        if settings["duplex_router"]["mode"] == "active":
            assert isinstance(result.native, DuplexNativeHarness), "real TeamHarness must construct duplex native"
        yield result
    finally:
        endpoint.model_gate.set()
        endpoint.fast_gate.set()
        tool.gate.set()
        await harness.stop()
        await asyncio.wait_for(collector, 3)
        await db.close()
        reset_session_id(token)
        await Runner.stop()
        await server.cleanup()


async def send_message(world, text="Customer forbids Kafka. Use PostgreSQL."):
    return await world.manager.send_message(content=text, to_member_name="A2")


@pytest.mark.asyncio
async def test_model_interrupt_keeps_original_task_tool_result_and_db_ack(world):
    w = world
    await w.harness.send("Implement the order event system with Kafka.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    before = w.native.active_round.round_id
    mid = await send_message(w)
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert w.native._st.round_id_counter > before
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(w.native._duplex_ledger.path)) as ledger:
        receipts = ledger.execute("SELECT state,result FROM calls WHERE tool='write_once'").fetchall()
    assert len(receipts) == 1 and receipts[0][0] == "committed"
    assert "committed exactly once" in receipts[0][1]
    slow = [c for c in w.endpoint.calls if c["model"] == "slow"]
    recovered = json.dumps(slow[-1]["messages"])
    assert "Implement the order event system" in recovered
    assert "Irreversible operation committed exactly once" in recovered
    assert mid in recovered and "Customer forbids Kafka" in recovered
    assert "Obsolete Kafka answer" not in recovered
    assert any(getattr(c, "type", None) == "round_aborted" for c in w.chunks)
    assert any(c["model"] == "fast" for c in w.endpoint.calls)


@pytest.mark.asyncio
async def test_interrupt_first_model_call_preserves_original_query(world):
    w = world
    w.endpoint.tool_first = False
    await w.harness.send("Plan order events with Kafka; include migration steps.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    mid = await send_message(w)
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    slow = [c for c in w.endpoint.calls if c["model"] == "slow"]
    assert len(slow) == 2
    recovered = json.dumps(slow[-1]["messages"])
    assert "include migration steps" in recovered
    assert mid in recovered and "Customer forbids Kafka" in recovered
    assert "Obsolete Kafka answer" not in recovered
    assert not w.tool.path.exists()
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)


@pytest.mark.asyncio
async def test_interrupt_during_tool_waits_for_commit_and_does_not_repeat(world):
    w = world
    w.tool.gate.clear()
    w.endpoint.block_model = False
    await w.harness.send("Implement order events.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    before = w.native.active_round.round_id
    mid = await send_message(w)
    drain = asyncio.create_task(w.handler.on_poll_mailbox(None))
    await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
    assert not drain.done()
    assert (await w.manager.get_messages(to_member_name="A2", unread_only=True))[0].message_id == mid
    assert w.native.active_round.round_id == before
    w.tool.gate.set()
    await asyncio.wait_for(drain, 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1])
    assert "Irreversible operation committed exactly once" in recovered
    assert mid in recovered


@pytest.mark.asyncio
async def test_append_during_tool_is_adopted_without_restart(world):
    w = world
    w.endpoint.fast_action = "APPEND"
    w.endpoint.block_model = False
    w.tool.gate.clear()
    await w.harness.send("Research the database options.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    before = w.native.active_round.round_id
    mid = await send_message(w, "Also export Markdown.")
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 6)
    assert w.native.active_round.round_id == before
    assert w.harness.state is HarnessState.RUNNING
    w.tool.gate.set()
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert w.native._st.round_id_counter == before
    assert mid in json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1])


@pytest.mark.asyncio
async def test_fast_timeout_leaves_db_unread_until_successful_retry(world):
    w = world
    w.settings["duplex_router"]["timeout_seconds"] = 0.05
    w.endpoint.fast_gate.clear()
    w.endpoint.block_model = False
    w.tool.gate.clear()
    await w.harness.send("Research database options.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    mid = await send_message(w, "Also export Markdown.")
    with pytest.raises(RuntimeError, match="classification failed: timeout"):
        await asyncio.wait_for(w.handler.on_poll_mailbox(None), 3)
    assert any(str(row.message_id) == mid for row in await w.manager.get_messages(
        to_member_name="A2", unread_only=True))
    w.endpoint.fast_gate.set()
    w.settings["duplex_router"].pop("timeout_seconds")
    await w.native._duplex_controller.aclose()
    w.endpoint.fast_action = "APPEND"
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 3)
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    w.tool.gate.set()
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert mid in json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1])


@pytest.mark.asyncio
async def test_append_survives_a_later_interrupt_before_it_is_consumed(world):
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    w.endpoint.fast_action = "APPEND"
    first = await send_message(w, "Also include latency figures.")
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 6)
    w.endpoint.fast_action = "INTERRUPT"
    second = await send_message(w)
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert recovered.count(first) == 1
    assert recovered.count(second) == 1
    assert w.tool.path.read_text() == "committed\n"


@pytest.mark.asyncio
async def test_append_arriving_during_final_model_call_gets_a_continuation(world):
    w = world
    w.endpoint.fast_action = "APPEND"
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    first = await send_message(w, "Also include latency figures.")
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 6)
    w.endpoint.model_gate.set()
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert recovered.count(first) == 1
    assert w.tool.path.read_text() == "committed\n"


@pytest.mark.asyncio
async def test_lifecycle_pause_supersedes_tool_phase_restart_leaving_message_unread(world):
    w = world
    w.tool.gate.clear()
    w.endpoint.block_model = False
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    await send_message(w)
    drain = asyncio.create_task(w.handler.on_poll_mailbox(None))
    await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
    await w.harness.pause()
    with pytest.raises(DeliverySuperseded):
        await asyncio.wait_for(drain, 3)
    w.tool.gate.set()
    await wait_until(lambda: w.harness.state is HarnessState.PAUSED)
    assert w.native._st.round_id_counter == 1
    assert len(await w.manager.get_messages(to_member_name="A2", unread_only=True)) == 1
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0


@pytest.mark.asyncio
async def test_stale_supervisor_command_cannot_interrupt_newer_context(world):
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    old = snapshot_from_native(w.native)
    w.native.commit_working_intent({"goal": "Order system", "current_hypothesis": "PostgreSQL",
                                   "next_action": "Use existing database", "constraints": ["No Kafka"]})
    result = await w.native.route_input("old decision", version=old.context_version,
                                       action="INTERRUPT", message_id="old")
    assert result == "STALE"
    assert w.native.active_round.round_id == int(old.round_id)
    assert snapshot_from_native(w.native).current_hypothesis == "PostgreSQL"


@pytest.mark.asyncio
async def test_failed_db_ack_retry_does_not_reexecute_delivery(world, monkeypatch):
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    await send_message(w)
    ack = w.manager.mark_messages_read

    async def fail_ack(*args, **kwargs):
        raise OSError("injected database write failure")

    monkeypatch.setattr(w.manager, "mark_messages_read", fail_ack)
    with pytest.raises(OSError):
        await w.handler.on_poll_mailbox(None)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    rounds = w.native._st.round_id_counter
    monkeypatch.setattr(w.manager, "mark_messages_read", ack)
    await w.handler.on_poll_mailbox(None)
    assert w.native._st.round_id_counter == rounds
    assert w.tool.path.read_text() == "committed\n"
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)


@pytest.mark.asyncio
async def test_user_input_uses_same_atomic_controller(world):
    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler
    from openjiuwen.agent_teams.agent.coordination.event_bus import InnerEventMessage, InnerEventType
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    handler = AgentLifecycleHandler(w.host, w.handler._blueprint, w.handler._infra, NS())
    await handler.on_user_input(InnerEventMessage(event_type=InnerEventType.USER_INPUT,
                              payload={"content": "Change goal: use PostgreSQL only.", "message_id": "u204"}))
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert "Change goal: use PostgreSQL only" in recovered
    assert w.native._st.round_id_counter == 2


@pytest.mark.asyncio
async def test_official_interruptbench_update_reaches_native_unchanged(world, tmp_path):
    """Official input through real runtime; not a WebArena task success test."""
    from jiuwenswarm.common.duplex_public_benchmark import load_official_interrupt
    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler

    source = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not source:
        pytest.skip("set JIUWEN_INTERRUPT_BENCH_ROOT to the pinned official checkout")
    root = Path(source)
    trajectory = tmp_path / "baseline-shape-fixture.json"
    trajectory.write_text(json.dumps({"task_id": 0, "actions": [{"action_type": 0}] * 7}))
    case = load_official_interrupt(root, suite="1update", task_id="0",
        config_file=root / "Eval/config_files/wa/test_webarena_lite_transformed_1update/0.json",
        spec_file=root / "Eval/interrupt_config/process/interrupt_spec_1update_opus_02.json",
        trajectory_file=trajectory)
    w = world
    await w.harness.send(case.initial_intent)
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    lifecycle = AgentLifecycleHandler(w.host, w.handler._blueprint, w.handler._infra, NS())
    with pytest.raises(ValueError, match="boundary"):
        await case.deliver(lifecycle, completed_actions=2, run_id="fixture")
    await case.deliver(lifecycle, completed_actions=1, run_id="fixture")
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert case.initial_intent in recovered and case.update in recovered
    assert "Impulse Duffle" not in recovered  # official evaluator answer stays private
    rounds = w.native._st.round_id_counter
    await case.deliver(lifecycle, completed_actions=1, run_id="fixture")
    assert w.native._st.round_id_counter == rounds
    assert w.tool.path.read_text() == "committed\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,writes", [("model", 1), ("abort_restart", 2)])
async def test_benchmark_native_peer_executes_recovery_and_naive_restart(world, tmp_path, policy, writes):
    from jiuwenswarm.benchmarks.duplex_runtime import Events, NativePeer
    w = world
    w.endpoint.repeat_tool_without_result = True
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    tool = WriteOnce(tmp_path / "peer-effects.txt")
    events = Events(tmp_path / "metrics.jsonl")
    peer = NativePeer(name="real-peer", models={"slow": slow, "fast": fast}, policy=policy,
        system_prompt="Implement the task using tools.", tools=[tool], events=events)
    await peer.start()
    try:
        await peer.send("Implement order events using Kafka.")
        await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
        await peer.receive("Customer forbids Kafka. Use PostgreSQL.", message_id="m1")
        result = await peer.wait(timeout=6)
        assert "PostgreSQL" in result["output"]
        assert tool.path.read_text() == "committed\n" * writes
        assert any(e["event"] == "model_end" and e["status"] == "cancelled" for e in events.records)
        assert any(e["event"] == "message_accepted" for e in events.records)
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_database_peer_uses_original_drain_and_acks_both_updates(world, tmp_path):
    from jiuwenswarm.benchmarks.duplex_database_peer import DatabasePeer
    from jiuwenswarm.benchmarks.duplex_runtime import Events

    w = world
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    w.endpoint.tool_first = False
    w.endpoint.fast_gate.clear()
    peer = DatabasePeer(database=tmp_path / "official.db", team="official", name="agent-1",
        models={"slow": slow, "fast": fast}, policy="model", system_prompt="Complete the task.",
        tools=[], events=Events(tmp_path / "db-events.jsonl"))
    inputs = []
    outside = set_session_id("")  # stand-alone benchmark has no inherited team session
    try:
        await peer.start()
        await peer.send("Use Kafka for the order system.")
        await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
        state = peer.harness._duplex_control_state
        assert state["goal"] == "Use Kafka for the order system."
        assert state["intent_source"] == "committed_state"
        assert state["current_hypothesis"] == ""
        inputs.append(asyncio.create_task(peer.receive("Use PostgreSQL instead.", message_id="one")))
        await asyncio.wait_for(w.endpoint.fast_entered.wait(), 6)
        inputs.append(asyncio.create_task(peer.receive("Keep the existing schema.", message_id="two")))
        await wait_until(lambda: len(inputs) == 2 and not inputs[1].done())
        rows = await peer.messages()
        assert not any(row.is_read for row in rows)
        w.endpoint.fast_gate.set()
        await asyncio.wait_for(asyncio.gather(*inputs), 6)
        await peer.wait(timeout=6)
        rows = await peer.messages()
        assert all(row.is_read for row in rows)
        count = len(peer.events.records)
        await peer.receive("Use PostgreSQL instead.", message_id="one")
        assert len(peer.events.records) == count
    finally:
        for task in inputs:
            if not task.done():
                task.cancel()
        await asyncio.gather(*inputs, return_exceptions=True)
        await peer.close()
        reset_session_id(outside)


@pytest.mark.asyncio
async def test_input_dispatch_preserves_sdk_event_order(world):
    from openjiuwen.agent_teams.agent.coordination.event_bus import (
        EventBus, InnerEventMessage, InnerEventType,
    )

    entered, release, lifecycle = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Dispatch:
        message = world.handler

        async def dispatch(self, event):
            if event.event_type == InnerEventType.USER_INPUT:
                entered.set()
                await release.wait()
            else:
                lifecycle.set()

    bus = EventBus(role=TeamRole.LEADER)
    await bus.start(wake_callback=Dispatch().dispatch)
    try:
        await bus.enqueue(InnerEventMessage(event_type=InnerEventType.USER_INPUT))
        await asyncio.wait_for(entered.wait(), 2)
        await bus.enqueue(InnerEventMessage(event_type=InnerEventType.REFRESH_TEAM_CONTEXT))
        assert not lifecycle.is_set()
        release.set()
        await asyncio.wait_for(lifecycle.wait(), 2)
    finally:
        release.set()
        await bus.stop()
    assert not hasattr(bus, "_duplex_input_tasks")


@pytest.mark.asyncio
async def test_browser_native_adapter_returns_original_prompt_action(world, tmp_path):
    from jiuwenswarm.benchmarks.duplex_runtime import Events
    from jiuwenswarm.benchmarks.interruptbench_runner import NativeBrowserBridge
    w = world
    w.endpoint.tool_first = False
    w.endpoint.block_model = False
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    events = Events(tmp_path / "browser-metrics.jsonl")
    bridge = NativeBrowserBridge(models={"slow": slow, "fast": fast}, policy="model",
        events=events, official_records=[])
    prompt = [{"role": "system", "content": "Keep official action syntax."},
              {"role": "user", "content": "Original browser observation."}]
    after = [*prompt, {"role": "user", "content": "Official updated goal."}]
    current = {"after_prompt": after, "pending": {"before": "Original goal", "after": "Updated goal",
        "update": "Official updated goal.", "message_id": "u-1", "delivered": False}}
    try:
        answer = await bridge._predict(NS(model="slow", gen_config={}), prompt, current)
        assert "PostgreSQL" in answer
        requests = [r for r in w.endpoint.calls if r["model"] == "slow"]
        assert requests[-1]["messages"] == after
        assert not requests[-1].get("tools")
        original_peer = bridge._peer
        second_prompt = [*after, {"role": "user", "content": "Next browser observation."}]
        await bridge._predict(NS(model="slow", gen_config={}), second_prompt,
                              {"intent": "Updated goal", "pending": None})
        assert bridge._peer is original_peer
        assert bridge._peer.harness._st.round_id_counter >= 2
        assert [r for r in w.endpoint.calls if r["model"] == "slow"][-1]["messages"] == second_prompt
    finally:
        await bridge.close_native()



@pytest.mark.asyncio
async def test_browser_worker_process_uses_native_http_and_clean_stdio(world, tmp_path):
    from jiuwenswarm.benchmarks.interruptbench_runner import ProcessWorker
    w = world
    w.endpoint.tool_first = False
    w.endpoint.block_model = False
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"slow": slow.model_dump(), "fast": fast.model_dump()}))
    worker = ProcessWorker(Path(sys.executable), models, "model", tmp_path / "process.jsonl")
    try:
        prompt = [{"role": "user", "content": "Use the official browser action format."}]
        answer = await asyncio.wait_for(asyncio.to_thread(worker.predict,
            NS(model="slow", gen_config={}), prompt, None), 45)
        assert "PostgreSQL" in answer
        assert [r for r in w.endpoint.calls if r["model"] == "slow"][-1]["messages"] == prompt
    finally:
        await asyncio.to_thread(worker.close)


@pytest.mark.asyncio
async def test_background_bash_completion_reaches_live_native_peer(world, tmp_path):
    from jiuwenswarm.benchmarks.agentradio_peer import BashTool
    from jiuwenswarm.benchmarks.duplex_runtime import Events, NativePeer
    import shutil
    shell = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
    if not shell or not Path(shell).exists():
        pytest.skip("Bash is required for the official AgentRadio shell adapter")
    w = world
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    events = Events(tmp_path / "background.jsonl")
    bash = BashTool(tmp_path, events, shell=shell)
    peer = NativePeer(name="background-peer", models={"slow": slow, "fast": fast}, policy="model",
        system_prompt="Implement the task.", tools=[WriteOnce(tmp_path / "bg-effects.txt"), bash], events=events)
    bash.peer = peer
    await peer.start()
    try:
        await peer.send("Implement order events using Kafka.")
        await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
        result = await bash.invoke({"command": "printf 'Customer forbids Kafka. Use PostgreSQL.'", "run_in_background": True})
        assert "started" in result
        await asyncio.wait_for(asyncio.gather(*bash.jobs.values()), 6)
        assert "PostgreSQL" in (await peer.wait(timeout=6))["output"]
        slow_calls = [r for r in w.endpoint.calls if r["model"] == "slow"]
        assert "Customer forbids Kafka" in json.dumps(slow_calls[-1]["messages"])
    finally:
        await bash.close()
        await peer.close()


@pytest.mark.asyncio
async def test_original_browser_prompt_and_action_parser_across_environments(world, tmp_path):
    """Real upstream PromptAgent -> separate Native process -> original parser."""
    python = os.environ.get("JIUWEN_INTERRUPT_PYTHON")
    root = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not python or not root:
        pytest.skip("separate official WebArena environment required")
    w = world
    w.endpoint.tool_first = False
    w.endpoint.block_model = False
    w.endpoint.final_content = "```stop [done]```"
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"slow": slow.model_dump(), "fast": fast.model_dump()}))
    script = tmp_path / "official-agent.py"
    script.write_text('''import json,os,sys
from pathlib import Path
root,models,native,metrics=sys.argv[1:]
os.chdir(Path(root)/"Eval")
sys.path.insert(0,str(Path(root)/"Eval"))
import agent.agent as module
import run as upstream
from browser_env.utils import DetachedPage
from jiuwenswarm.benchmarks.duplex_metrics import Events
from jiuwenswarm.benchmarks.interruptbench_runner import NativeBrowserBridge
sys.argv=["run.py","--provider","openai","--model","slow","--mode","chat",
"--instruction_path","agent/prompts/jsons/p_cot_id_actree_2s.json",
"--action_set_tag","id_accessibility_tree","--observation_type","accessibility_tree",
"--max_obs_length","0"]
args=upstream.config()
bridge=NativeBrowserBridge(models=None,policy="model",events=Events(Path(metrics)),
official_records=[],native_python=Path(native),models_file=Path(models))
restore=bridge.install(module)
try:
    agent=module.construct_agent(args)
    trajectory=[{"observation":{"text":"A fixture page"},"info":{"page":DetachedPage("http://127.0.0.1:1","")}}]
    action=agent.next_action(trajectory,"Finish the fixture task.",meta_data={"action_history":["None"]})
    assert int(action["action_type"])==17,action
    assert action["answer"]=="done",action
    print("OFFICIAL_ACTION_OK")
finally:
    restore()
''', encoding="utf-8")
    env = {**os.environ, "DATASET": "webarena", "OPENAI_API_KEY": "local-test",
           "OPENAI_API_URL": "http://127.0.0.1:1"}
    env.update({name: "http://127.0.0.1:1" for name in
        ("REDDIT", "SHOPPING", "SHOPPING_ADMIN", "GITLAB", "WIKIPEDIA", "MAP", "HOMEPAGE")})
    process = await asyncio.create_subprocess_exec(python, str(script), root, str(models), sys.executable,
        str(tmp_path / "official.jsonl"), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 90)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, output.decode(errors="replace")
    assert b"OFFICIAL_ACTION_OK" in output


@pytest.mark.asyncio
async def test_real_event_bus_wakes_receiver_and_broadcast_watermark_advances(world):
    from openjiuwen.agent_teams.agent.coordination.event_bus import EventBus
    from openjiuwen.agent_teams.context import get_session_id
    from openjiuwen.agent_teams.schema.events import TeamTopic
    w = world
    bus = EventBus(role=TeamRole.LEADER)
    w.handler._poll = bus
    topic = TeamTopic.MESSAGE.build(get_session_id(), "duplex")
    handled = asyncio.Event()

    async def wake(event):
        await w.handler.on_message_or_broadcast(event)
        handled.set()

    await bus.start(wake_callback=wake)
    await w.messager.subscribe(topic, bus.enqueue)
    try:
        await w.harness.send("Implement the order system.")
        await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
        await w.manager.broadcast_message(content="Customer forbids Kafka. Use PostgreSQL.")
        await asyncio.wait_for(handled.wait(), 6)
        await wait_until(lambda: w.harness.state is HarnessState.IDLE)
        assert not await w.manager.get_broadcast_messages(member_name="A2", unread_only=True)
        assert w.native._st.round_id_counter == 2
        assert w.tool.path.read_text() == "committed\n"
    finally:
        await w.messager.unsubscribe(topic)
        await bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("world,group,use_steer", [
    ({"mode": "off"}, "G0-serial", False),
    ({"mode": "off"}, "G1-current-steer", True),
    ({"policy": "always_interrupt"}, "G2-always-interrupt", True),
    ({"policy": "model"}, "G3-model-router", True),
], indirect=["world"])
async def test_four_strategies_at_same_tool_boundary(world, group, use_steer, record_property):
    w = world
    w.endpoint.block_model = False
    w.tool.gate.clear()
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    mid = await send_message(w)
    drain = asyncio.create_task(w.handler._process_unread_messages("A2", use_steer=use_steer))
    if group.startswith(("G2", "G3")):
        await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
    else:
        await asyncio.wait_for(drain, 3)
    w.tool.gate.set()
    await asyncio.wait_for(drain, 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    slow = [c for c in w.endpoint.calls if c["model"] == "slow"]
    assert mid in json.dumps(slow[-1]["messages"])
    assert w.tool.path.read_text() == "committed\n"
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    record_property("strategy", group)
    record_property("slow_requests", len(slow))
    record_property("fast_requests", sum(c["model"] == "fast" for c in w.endpoint.calls))
    assert len(slow) == (3 if group == "G0-serial" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("world", [{"mode": "shadow"}], indirect=True)
async def test_shadow_real_team_harness_observes_plain_messages_without_changing_execution(world):
    w = world
    assert not isinstance(w.native, DuplexNativeHarness)
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    await send_message(w)
    await w.handler.on_poll_mailbox(None)
    observer = w.handler._duplex_shadow_observer
    await asyncio.wait_for(observer._task, 6)
    assert any(c["model"] == "fast" for c in w.endpoint.calls)
    assert w.harness.state is HarnessState.RUNNING
    assert w.native.active_round.round_id == 1
    assert not any(t["function"]["name"] == "update_working_intent"
                   for t in w.endpoint.calls[0]["tools"])


@pytest.mark.asyncio
async def test_slow_model_publishes_working_intent_used_by_real_fast_request(world):
    w = world
    w.endpoint.intent_first = True
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    await send_message(w)
    await w.handler.on_poll_mailbox(None)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    fast = next(c for c in w.endpoint.calls if c["model"] == "fast")
    prompt = json.dumps(fast["messages"])
    assert "Use Kafka" in prompt and "Existing infrastructure only" in prompt
    assert "Irreversible operation committed exactly once" not in prompt
    assert w.tool.path.read_text() == "committed\n"
