"""Browser side effects and safe U2A policies use the actual Native path."""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tests.integration_tests.test_duplex_e2e import world as _shared_world
from jiuwenswarm.benchmarks.duplex_browser_checkpoint import BrowserCheckpoint, BrowserRestoreRequired
from jiuwenswarm.agents.harness.team.duplex_ledger import UncertainToolOutcome
from jiuwenswarm.benchmarks.duplex_runtime import Events
from jiuwenswarm.benchmarks.interruptbench_runner import NativeBrowserBridge, POLICIES, instrument_runner

world = _shared_world


def test_browser_effect_requires_committed_trajectory_and_live_environment(tmp_path):
    path = tmp_path / "tools.sqlite3"
    journal = BrowserCheckpoint(path, "trial:task", "browser-instance")
    number = journal.begin({"action_type": "click", "element_id": "pay"}, "checkpoint-7")
    # Browser can have changed even when the trajectory has not been committed.
    with pytest.raises(UncertainToolOutcome):
        BrowserCheckpoint(path, "trial:task", "browser-instance").ready()
    trajectory = [{"observation": "before"}, {"action_type": "click", "element_id": "pay"},
                  {"observation": "payment completed"}]
    journal.commit(number, trajectory)
    recovered = BrowserCheckpoint(path, "trial:task", "browser-instance").ready()
    assert json.loads(recovered["trajectory"]) == trajectory
    assert recovered["native_version"] == "checkpoint-7"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM calls").fetchone() == ("committed",)
    # A fresh browser cannot use the receipt to silently redo the payment.
    with pytest.raises(BrowserRestoreRequired):
        BrowserCheckpoint(path, "trial:task", "new-browser").begin({"action_type": "click"}, "8")


def test_safe_policy_is_accepted_and_official_boundaries_are_instrumented():
    assert "always_interrupt" in POLICIES
    root = Path(os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT", "D:/jiusi_agent/_repos/InterruptBench")) / "Eval/run.py"
    if not root.exists():
        pytest.skip("pinned official runner checkout required")
    instrument_runner(root.read_text(encoding="utf-8"), str(root))


def test_runner_cli_accepts_safe_policy_before_checkout_verification(monkeypatch, tmp_path):
    from jiuwenswarm.benchmarks import interruptbench_runner as module
    class ReachedCheckout(Exception):
        pass
    def verify(*args):
        raise ReachedCheckout
    monkeypatch.setattr(module, "verify_checkout", verify)
    monkeypatch.setattr(sys, "argv", ["runner", "--repo", str(tmp_path), "--models", "models.json",
        "--native-python", sys.executable, "--suite", "1update", "--policy", "always_interrupt",
        "--metrics", "metrics.jsonl"])
    with pytest.raises(ReachedCheckout):
        module.main()


def test_worker_cli_passes_safe_policy_to_server(monkeypatch):
    from jiuwenswarm.benchmarks import interruptbench_worker as module
    seen = []
    async def serve(args, output):
        seen.append(args.policy)
    monkeypatch.setattr(module, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["worker", "--models", "models.json",
        "--metrics", "metrics.jsonl", "--policy", "always_interrupt"])
    module.main()
    assert seen == ["always_interrupt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "delay_old_http"), [
    ("always_interrupt", False), ("model", False), ("always_interrupt", True),
], ids=["always_interrupt", "model", "always_interrupt_before_http"])
async def test_official_update_uses_production_u2a_and_keeps_browser_receipt(
        world, tmp_path, monkeypatch, policy, delay_old_http):
    world.endpoint.tool_first = False
    # A cancellation may win before the old HTTP request reaches the server.
    # Block only obsolete work, never "the first request" (which can be new).
    world.endpoint.block_prompt = "Original browser observation."
    fast = world.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    bridge = NativeBrowserBridge(models={"slow": slow, "fast": fast}, policy=policy,
        events=Events(tmp_path / "u2a.jsonl"), official_records=[])
    if delay_old_http:
        from openjiuwen.core.foundation.llm import Model
        original_stream = Model.stream
        delayed = False

        async def hold_old_request(self, *args, **kwargs):
            nonlocal delayed
            if self is bridge._peer.model and not delayed:
                delayed = True
                await asyncio.Event().wait()  # deterministic cancellation before HTTP
            async for chunk in original_stream(self, *args, **kwargs):
                yield chunk

        monkeypatch.setattr(Model, "stream", hold_old_request)
    await bridge.reset_native("official-task-1", "live-browser")
    await bridge.browser_operation("browser_begin", action={"action_type": "click", "element_id": "save"})
    trajectory = [{"observation": "before"}, {"action_type": "click", "element_id": "save"},
                  {"observation": "saved"}]
    await bridge.browser_operation("browser_commit", trajectory=trajectory)
    before = [{"role": "user", "content": "Original browser observation."}]
    after = [{"role": "user", "content": "Official updated goal. saved"}]
    current = {"intent": "new goal", "after_prompt": after, "pending": {
        "before": "old goal", "after": "new goal", "update": "Official updated goal.",
        "message_id": "official-1", "delivered": False}}
    try:
        answer = await asyncio.wait_for(bridge._predict(NS(model="slow", gen_config={}), before, current), 15)
        assert "PostgreSQL" in answer
        peer = bridge._peer
        assert "official-1" in peer.harness._duplex_received
        assert peer.harness._duplex_goal_provider() == "new goal"
        assert peer.harness._duplex_browser_checkpoint["ordinal"] == 1
        requests = [c for c in world.endpoint.calls if c["model"] == "slow"]
        assert requests[-1]["messages"] == after
        if delay_old_http:
            assert requests[0]["messages"] == after
        if policy == "always_interrupt":
            assert not any(c["model"] == "fast" for c in world.endpoint.calls)
        events = [json.loads(line) for line in bridge.events.path.read_text().splitlines()]
        assert any(e.get("event") == "user_input_entry" for e in events)
        assert any(e.get("status") == "cancelled" for e in events)
        # Rebuild only Native, retaining the real environment and committed evidence.
        await bridge.close_native()
        await bridge._predict(NS(model="slow", gen_config={}), after, {"intent": "new goal", "pending": None})
        assert bridge._peer is not peer
        assert json.loads(bridge._peer.harness._duplex_browser_checkpoint["trajectory"]) == trajectory
        assert bridge._browser_checkpoint.ready()["ordinal"] == 1
    finally:
        await bridge.close_native()


@pytest.mark.asyncio
async def test_worker_protocol_commits_browser_action_and_restores_native(world, tmp_path):
    from jiuwenswarm.benchmarks.interruptbench_runner import ProcessWorker
    world.endpoint.tool_first = False
    world.endpoint.block_model = False
    fast = world.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"slow": slow.model_dump(), "fast": fast.model_dump()}))
    metrics = tmp_path / "worker.jsonl"
    prompt = [{"role": "user", "content": "Original official prompt"}]
    trajectory = [{"observation": "before"}, {"action_type": "click"}, {"observation": "saved"}]
    worker = ProcessWorker(Path(sys.executable), models, "always_interrupt", metrics)
    try:
        await asyncio.to_thread(worker.reset, "task-1", "same-live-browser")
        await asyncio.wait_for(asyncio.to_thread(worker.predict, NS(model="slow", gen_config={}), prompt, None), 60)
        result = await asyncio.to_thread(worker.request, {"operation": "browser_begin", "action": {"action_type": "click"}})
        assert result == {"ordinal": 1}
        # No model call is allowed in the effect -> trajectory commit gap.
        count = len(world.endpoint.calls)
        with pytest.raises(RuntimeError, match="UncertainToolOutcome"):
            await asyncio.to_thread(worker.predict, NS(model="slow", gen_config={}), prompt, None)
        assert len(world.endpoint.calls) == count
        result = await asyncio.to_thread(worker.request, {"operation": "browser_commit", "trajectory": trajectory})
        assert result == {"committed": 1}
        await asyncio.to_thread(worker.close)
        worker = ProcessWorker(Path(sys.executable), models, "always_interrupt", metrics)
        await asyncio.to_thread(worker.reset, "task-1", "same-live-browser")
        answer = await asyncio.wait_for(asyncio.to_thread(worker.predict,
            NS(model="slow", gen_config={}), prompt, None), 60)
        assert "PostgreSQL" in answer
        with sqlite3.connect(metrics.with_suffix(".tools.sqlite3")) as db:
            assert db.execute("SELECT count(*) FROM calls WHERE tool='browser.env.step'").fetchone() == (1,)
            assert json.loads(db.execute("SELECT trajectory FROM browser_checkpoints").fetchone()[0]) == trajectory
    finally:
        await asyncio.to_thread(worker.close)


@pytest.mark.asyncio
async def test_original_browser_step_runs_once_across_native_worker_restart(world, tmp_path):
    """A real Chromium page, original env.step, and a restarted Native process."""
    python = os.environ.get("JIUWEN_INTERRUPT_PYTHON")
    root = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not python or not root:
        pytest.skip("separate official WebArena environment required")
    world.endpoint.tool_first = False
    world.endpoint.block_model = False
    fast = world.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"slow": slow.model_dump(), "fast": fast.model_dump()}))
    script = tmp_path / "browser_effect.py"
    script.write_text('''import json,os,sys,threading
from pathlib import Path
from types import SimpleNamespace as NS
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
root,models,native,metrics=sys.argv[1:]
os.chdir(Path(root)/"Eval")
sys.path.insert(0,str(Path(root)/"Eval"))
from browser_env import ScriptBrowserEnv
from browser_env.actions import create_playwright_action
from run import _json_safe_loose, _freeze_page_url
from jiuwenswarm.benchmarks.duplex_metrics import Events
from jiuwenswarm.benchmarks.interruptbench_runner import NativeBrowserBridge,ProcessWorker
class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body=b"<button onclick='document.querySelector(\\"output\\").textContent=++window.n'>Save</button><output>0</output><script>window.n=0</script>"
        self.send_response(200)
        self.send_header("Content-Type","text/html")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args): pass
server=ThreadingHTTPServer(("127.0.0.1",0),Page)
threading.Thread(target=server.serve_forever,daemon=True).start()
config=Path(metrics).with_suffix(".config.json")
config.write_text(json.dumps({"start_url":f"http://127.0.0.1:{server.server_port}"}))
bridge=NativeBrowserBridge(models=None,policy="always_interrupt",events=Events(Path(metrics)),official_records=[])
worker_metrics=Path(metrics).with_suffix(".native.jsonl")
bridge.worker=ProcessWorker(Path(native),Path(models),"always_interrupt",worker_metrics)
env=ScriptBrowserEnv(headless=True,observation_type="html")
try:
    bridge.worker.reset(str(config),"live-browser")
    obs,info=env.reset(options={"config_file":str(config)})
    _freeze_page_url(info)
    trajectory=[{"observation":obs,"info":info}]
    prompt=[{"role":"user","content":"Original browser observation"}]
    bridge.worker.predict(NS(model="slow",gen_config={}),prompt,None)
    action=create_playwright_action("page.get_by_role('button', name='Save').click()")
    trajectory.append(action)
    obs,_,terminated,_,info=bridge.browser_step(env,action,_json_safe_loose)
    _freeze_page_url(info)
    trajectory.append({"observation":obs,"info":info})
    bridge.browser_commit(trajectory,_json_safe_loose)
    assert env.page.inner_text("output")=="1"
    bridge.worker.close()
    bridge.worker=ProcessWorker(Path(native),Path(models),"always_interrupt",worker_metrics)
    bridge.worker.reset(str(config),"live-browser")
    bridge.worker.predict(NS(model="slow",gen_config={}),prompt,None)
    assert env.page.inner_text("output")=="1"
    print("OFFICIAL_BROWSER_EFFECT_ONCE")
finally:
    bridge.worker.close()
    env.close()
    server.shutdown()
''', encoding="utf-8")
    env = {**os.environ, "DATASET": "webarena", "OPENAI_API_KEY": "local-test"}
    env.update({name: "http://127.0.0.1:1" for name in
        ("REDDIT", "SHOPPING", "SHOPPING_ADMIN", "GITLAB", "WIKIPEDIA", "MAP", "HOMEPAGE")})
    process = await asyncio.create_subprocess_exec(python, str(script), root, str(models), sys.executable,
        str(tmp_path / "official-effects.jsonl"), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 120)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, output.decode(errors="replace")
    assert b"OFFICIAL_BROWSER_EFFECT_ONCE" in output
