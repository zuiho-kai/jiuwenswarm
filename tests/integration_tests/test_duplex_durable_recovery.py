"""Kill a real SDK process after its SQLite mailbox ACK, then recover it."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

from test_duplex_e2e import Endpoint, wait_until


CHILD = r'''
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from test_duplex_e2e import (
    WriteOnce, Host, Model, ModelClientConfig, ModelRequestConfig, TeamModelConfig,
    AgentCard, DeepAgentParts, DeepAgentConfig, Runner, TeamHarness, TeamRole,
    TeamDatabase, DatabaseConfig, DatabaseType, InProcessMessager, TeamMessageManager,
    MessageHandler, install_shadow_observer, HarnessState, set_session_id, wait_until,
)
from jiuwenswarm.common import config

async def main():
    root, port, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    settings = {'duplex_router': {'mode':'active','model_name':'fast','policy':'model','timeout_seconds':2}}
    config.get_config = lambda: settings
    install_shadow_observer()
    set_session_id('durable-recovery-team')
    client = ModelClientConfig(client_provider='OpenAI',api_base=f'http://127.0.0.1:{port}/v1',api_key='local',max_retries=0)
    slow = Model(model_client_config=client,model_config=ModelRequestConfig(model_name='slow'))
    fast = TeamModelConfig(model_client_config=client,model_request_config=ModelRequestConfig(model_name='fast'))
    tool = WriteOnce(root/'effects.txt')
    card = AgentCard(id='durable-recovery-card',name='durable-recovery')
    class Spec:
        def resolve_parts(self, context=None):
            return DeepAgentParts(config=DeepAgentConfig(card=card,model=slow,system_prompt='Complete task.',enable_task_loop=True,max_iterations=8),rails=[],tool_cards=[tool.card],tool_instances=[tool])
    await Runner.start()
    harness = TeamHarness.build(agent_spec=Spec(),role=TeamRole.LEADER,member_name='A2')
    harness.inner_agent.durable_scope='stable-team/A2'
    db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE,connection_string=str(root/'mailbox.db')))
    await db.initialize()
    if phase != 'recover':
        await db.team.create_team(team_name='duplex',display_name='duplex',leader_member_name='A2')
        for member in ('A1','A2'):
            await db.member.create_member(member_name=member,team_name='duplex',display_name=member,agent_card=card.model_dump_json(),status='busy')
    manager=TeamMessageManager(team_name='duplex',db=db,messager=InProcessMessager(),member_name='A1')
    host=Host(harness,fast)
    handler=MessageHandler(host,NS(role=TeamRole.LEADER,member_name='A2',language='en',team_spec=None),NS(message_manager=manager,team_backend=None),NS())
    await harness.start()
    if phase == 'receipt':
        finish=harness.inner_agent._duplex_ledger._finish
        def block_after_receipt(scope, call_id, state, result):
            finish(scope, call_id, state, result)
            if state == 'committed':
                (root/'receipt-committed').touch()
                __import__('threading').Event().wait()
        harness.inner_agent._duplex_ledger._finish=block_after_receipt
    async def collect():
        async for _ in harness.outputs():
            pass
    collector=asyncio.create_task(collect())
    if phase == 'receipt':
        mid=await manager.send_message(content='Implement the original Kafka order system. Customer forbids Kafka. Use PostgreSQL.',to_member_name='A2')
        await handler.on_poll_mailbox(None)
        assert not await manager.get_messages(to_member_name='A2',unread_only=True)
        (root/'acked').write_text(mid)
        await asyncio.Event().wait()
    elif phase == 'accept':
        await harness.send('Implement the original Kafka order system.')
        await wait_until(lambda: tool.path.exists())
        # The parent creates this signal only after the second model call is parked.
        await wait_until(lambda: (root/'model-parked').exists(),timeout=25)
        mid=await manager.send_message(content='Customer forbids Kafka. Use PostgreSQL.',to_member_name='A2')
        await handler.on_poll_mailbox(None)
        assert not await manager.get_messages(to_member_name='A2',unread_only=True)
        (root/'acked').write_text(mid)
        await asyncio.Event().wait()
    else:
        await wait_until(lambda: harness.state is HarnessState.IDLE,timeout=25)
        assert not await manager.get_messages(to_member_name='A2',unread_only=True)
        (root/'recovered').write_text('ok')
        await harness.stop()
        await collector
        await db.close()
        await Runner.stop()

asyncio.run(main())
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_window", ["accept", "receipt"])
async def test_ack_survives_process_kill_and_restores_tool_results(tmp_path, crash_window):
    endpoint = Endpoint()
    endpoint.fast_action = "APPEND"
    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint.handle)
    server = web.AppRunner(app, shutdown_timeout=0.1)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    env = dict(os.environ, JIUWEN_DUPLEX_LEDGER_PATH=str(tmp_path / "tools.db"),
               JIUWEN_DUPLEX_INBOX_PATH=str(tmp_path / "inbox.db"))
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).parent), env.get("PYTHONPATH", "")])
    processes = []
    logs = []
    try:
        async def launch(phase):
            stream = (tmp_path / f"{phase}.log").open("wb")
            logs.append(stream)
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", CHILD,
                str(tmp_path), str(port), phase, env=env, stdout=stream, stderr=stream)
            processes.append(process)
            return process

        first = await launch(crash_window)
        if crash_window == "accept":
            await asyncio.wait_for(endpoint.model_entered.wait(), 50)
            (tmp_path / "model-parked").touch()
        else:
            await wait_until(lambda: (tmp_path / "receipt-committed").exists() or first.returncode is not None,
                             timeout=50)
        await wait_until(lambda: (tmp_path / "acked").exists() or first.returncode is not None, timeout=30)
        assert (tmp_path / "acked").exists(), (tmp_path / f"{crash_window}.log").read_text(errors="replace")[-6000:]
        first.kill()  # no pause, stop, post_run or checkpoint teardown
        await first.wait()
        previous_calls = len(endpoint.calls)
        endpoint.block_model = False
        endpoint.tool_first = False
        second = await launch("recover")
        await asyncio.wait_for(second.wait(), 45)
        assert second.returncode == 0, (tmp_path / "recover.log").read_text(errors="replace")[-10000:]
        assert (tmp_path / "recovered").read_text() == "ok"
        requests = [item for item in endpoint.calls[previous_calls:] if item["model"] == "slow"]
        assert requests
        restored = json.dumps(requests[0]["messages"])
        assert "original Kafka order system" in restored
        assert "Customer forbids Kafka. Use PostgreSQL." in restored
        assert "committed exactly once" in restored
        assert (tmp_path / "effects.txt").read_text() == "committed\n"
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        for stream in logs:
            stream.close()
        endpoint.model_gate.set()
        await server.cleanup()
