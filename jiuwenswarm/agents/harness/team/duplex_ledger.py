"""Durable tool-call receipts outside rollbackable model context."""
import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class UncertainToolOutcome(RuntimeError):
    pass


class ToolLedger:
    def __init__(self, path):
        self.path = Path(path).expanduser()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("""CREATE TABLE IF NOT EXISTS calls (
            scope TEXT, call_id TEXT, tool TEXT, digest TEXT, state TEXT,
            result TEXT, PRIMARY KEY(scope, call_id))""")
        return db

    def _reserve(self, scope, call_id, tool, digest, idempotent):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT tool,digest,state,result FROM calls WHERE scope=? AND call_id=?",
                             (scope, call_id)).fetchone()
            if row:
                if row[:2] != (tool, digest):
                    raise ValueError("tool_call_id reused with different operation")
                if row[2] == "committed" and row[3] is not None:
                    return json.loads(row[3])
                if not idempotent:
                    raise UncertainToolOutcome("Tool outcome requires reconciliation; execution was not repeated")
            if not idempotent and db.execute(
                "SELECT 1 FROM calls WHERE scope=? AND tool=? AND digest=? AND state!='committed' LIMIT 1",
                (scope, tool, digest),
            ).fetchone():
                raise UncertainToolOutcome("An earlier identical operation has an uncertain outcome")
            db.execute("INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?,NULL)",
                       (scope, call_id, tool, digest, "prepared"))
        return None

    def _finish(self, scope, call_id, state, result):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE calls SET state=?,result=? WHERE scope=? AND call_id=?",
                       (state, result, scope, call_id))

    async def execute(self, *, scope, call, idempotent, invoke):
        from openjiuwen.core.foundation.llm import ToolMessage

        arguments = call.arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                                            default=str).encode()).hexdigest()
        if not call.id:
            raise ValueError("durable tool execution requires a tool_call_id")
        receipt = await asyncio.to_thread(self._reserve, scope, call.id, call.name, digest, idempotent)
        if receipt is not None:
            return receipt["value"], ToolMessage.model_validate(receipt["message"])
        try:
            value, message = await invoke()
            try:
                serialized = json.dumps({"value": value, "message": message.model_dump()}, ensure_ascii=False)
            except (TypeError, AttributeError):
                # Complex SDK/workflow objects cannot be reconstructed safely.
                # Record completion, but refuse to replay them from a partial receipt.
                serialized = None
            await asyncio.to_thread(self._finish, scope, call.id, "committed", serialized)
            return value, message
        except BaseException:
            await asyncio.shield(asyncio.to_thread(self._finish, scope, call.id, "uncertain", None))
            raise
