"""Write-ahead inputs and atomic context checkpoints, independent of DB ACKs."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class DurableInbox:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS inputs(scope TEXT, id TEXT, sender TEXT,
                content TEXT, marker TEXT, PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS checkpoints(scope TEXT PRIMARY KEY, payload TEXT);
        """)
        return db

    def accept(self, scope, message_id, sender, content):
        marker = "[duplex-input:" + hashlib.sha256(message_id.encode()).hexdigest() + "]"
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT sender,content FROM inputs WHERE scope=? AND id=?",
                             (scope, message_id)).fetchone()
            if row is not None and row != (sender, content):
                raise ValueError("message identity reused with different content")
            db.execute("INSERT OR IGNORE INTO inputs VALUES (?,?,?,?,?)",
                       (scope, message_id, sender, content, marker))
        return marker + "\n" + content

    def load(self, scope):
        with closing(self.connect()) as db:
            checkpoint = db.execute("SELECT payload FROM checkpoints WHERE scope=?", (scope,)).fetchone()
            rows = db.execute("SELECT id,sender,content,marker FROM inputs WHERE scope=? ORDER BY rowid",
                              (scope,)).fetchall()
        return (json.loads(checkpoint[0]) if checkpoint else None), rows

    def checkpoint(self, scope, payload):
        # The coverage set and context are one durable commit. ACK never means
        # that an input has already been consumed by the model.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload FROM checkpoints WHERE scope=?", (scope,)).fetchone()
            if previous and json.loads(previous[0]).get("suspended"):
                payload = {**payload, "suspended": True}
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (scope, json.dumps(payload, ensure_ascii=False)))

    def suspend(self, scope):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM checkpoints WHERE scope=?", (scope,)).fetchone()
            payload = json.loads(row[0]) if row else {}
            payload["suspended"] = True
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (scope, json.dumps(payload)))

    def activate(self, scope):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM checkpoints WHERE scope=?", (scope,)).fetchone()
            if row:
                payload = json.loads(row[0])
                payload.pop("suspended", None)
                db.execute("UPDATE checkpoints SET payload=? WHERE scope=?", (json.dumps(payload), scope))


async def persist_native(native, *, running=True):
    import asyncio

    active = native.active_round
    if active is None and running:
        return
    context = native.react_agent.context_engine.get_context(session_id=native.session_id)
    if context is None:
        return
    messages = [message.model_dump(mode="json") for message in context.get_messages()]
    previous, _ = await asyncio.to_thread(native._duplex_inbox.load, native.durable_scope)
    covered = set((previous or {}).get("covered", []))
    covered.update(key for message in messages if message["role"] == "user"
                   for key in message.get("metadata", {}).get("duplex_input_ids", []))
    payload = {"messages": messages, "state": native.load_state(native._session).to_session_dict(),
               "covered": sorted(covered), "running": running,
               "query": str(active.original_query) if active else "",
               "browser_checkpoint": getattr(native, "_duplex_browser_checkpoint", None),
               "receipt_version": getattr(native, "_duplex_receipt_version", 0),
               "recoverable": active is None or isinstance(active.original_query, str),
               "intent_state": {key: native._session.get_state(key) for key in (
                   "duplex_working_intent", "duplex_control_state", "duplex_intent_sources")}}
    await asyncio.to_thread(native._duplex_inbox.checkpoint, native.durable_scope, payload)


async def restore_native(native):
    import asyncio
    from openjiuwen.core.foundation.llm.schema import message as schemas
    from openjiuwen.harness.schema.state import DeepAgentState
    from jiuwenswarm.agents.harness.team.duplex_ledger import UncertainToolOutcome

    checkpoint, rows = await asyncio.to_thread(native._duplex_inbox.load, native.durable_scope)
    native._duplex_admission_inputs.update({marker + "\n" + content: (key,)
                                           for key, _, content, marker in rows})
    covered = set((checkpoint or {}).get("covered", []))
    native._duplex_received.update(covered)
    if checkpoint and "messages" in checkpoint:
        saved_browser = checkpoint.get("browser_checkpoint")
        current_browser = getattr(native, "_duplex_browser_checkpoint", None)
        if saved_browser and (not current_browser or
                current_browser.get("environment") != saved_browser.get("environment") or
                current_browser.get("ordinal", 0) < saved_browser.get("ordinal", 0)):
            raise UncertainToolOutcome("Restore the authoritative browser before Native continuation")
        if not checkpoint.get("recoverable", True):
            raise RuntimeError("Interactive input requires its original interaction session to recover")
        classes = {"assistant": schemas.AssistantMessage, "tool": schemas.ToolMessage,
                   "user": schemas.UserMessage, "system": schemas.SystemMessage}
        messages = [classes.get(item["role"], schemas.BaseMessage).model_validate(item)
                    for item in checkpoint["messages"]]
        known_calls = {call.id for message in messages for call in getattr(message, "tool_calls", None) or []}
        with closing(native._duplex_ledger._connect()) as db:
            ledger_rows = db.execute("SELECT call_id,result FROM calls WHERE scope=? AND tool!='browser.env.step'",
                                     (native.durable_scope,)).fetchall()
            ledger_calls = {row[0] for row in ledger_rows}
            browser_calls = db.execute("SELECT call_id,state FROM calls WHERE scope=? AND tool='browser.env.step'",
                                       (native.durable_scope,)).fetchall()
        if browser_calls and (not current_browser or any(
                state != "committed" or int(key.removeprefix("browser:")) > current_browser.get("ordinal", 0)
                for key, state in browser_calls)):
            raise UncertainToolOutcome("Browser receipts are not covered by the authoritative browser checkpoint")
        native._duplex_receipt_version = max([0] + [
            (json.loads(row[1]).get("recovery_state") or {}).get("version", 0)
            for row in ledger_rows if row[1]])
        historical_calls = {key for key, result in ledger_rows if result and
                            (json.loads(result).get("recovery_state") or {}).get("version", float("inf"))
                            <= checkpoint.get("receipt_version", 0)}
        if ledger_calls - known_calls - historical_calls:
            raise UncertainToolOutcome("Tool receipts are newer than recoverable context; reconcile before continuing")
        answered = {getattr(message, "tool_call_id", None) for message in messages
                    if message.role == "tool"}
        restored = []
        recovered_states = []
        for message in messages:
            restored.append(message)
            for call in getattr(message, "tool_calls", None) or []:
                if call.id in answered:
                    continue
                # A crash between external execution and checkpointing must
                # reuse the exact receipt, never ask the model to repeat it.
                with closing(native._duplex_ledger._connect()) as db:
                    receipt = db.execute("SELECT state,result FROM calls WHERE scope=? AND call_id=?",
                                         (native.durable_scope, call.id)).fetchone()
                if not receipt or receipt[0] != "committed" or receipt[1] is None:
                    raise UncertainToolOutcome("Recovery has an unresolved tool call; reconcile before continuing")
                decoded = json.loads(receipt[1])
                restored.append(schemas.ToolMessage.model_validate(decoded["message"]))
                if decoded.get("recovery_state"):
                    recovered_states.append(decoded["recovery_state"])
                answered.add(call.id)
        restored_state = max(recovered_states, key=lambda item: item["version"]) if recovered_states else checkpoint
        native.save_state(native._session, DeepAgentState.from_session_dict(restored_state["state"]))
        context = await native.react_agent.context_engine.create_context(session=native._session)
        context.set_messages(restored, with_history=True)
        await native.react_agent.context_engine.save_contexts(native._session)
        intent_state = restored_state.get("intent_state") or {}
        native._session.update_state(intent_state)
        native._duplex_intent = intent_state.get("duplex_working_intent") or {}
        native._duplex_control_state = intent_state.get("duplex_control_state") or {}
    pending = [(key, marker + "\n" + content) for key, _, content, marker in rows if key not in covered]
    if checkpoint and checkpoint.get("suspended"):
        for key, text in pending:
            native._push_steer(text)
            native._duplex_received.add(key)
        return  # explicit lifecycle cancellation is not a process crash
    # Populate steering before resuming so restored execution sees all ACKed
    # but not yet checkpointed inputs on its very first model call.
    if checkpoint and checkpoint["running"]:
        for key, text in pending:
            native._push_steer(text)
            native._duplex_received.add(key)
        await native.resume(query=checkpoint["query"])
    else:
        for key, text in pending:
            await native.send(text, immediate=True)
            native._duplex_received.add(key)
