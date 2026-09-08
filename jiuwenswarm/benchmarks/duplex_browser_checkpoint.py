"""Coordinate authoritative browser effects with the Native tool journal.

An observation receipt is evidence, not a browser snapshot. A new environment
must never replay effects to manufacture the recorded browser state.
"""
import hashlib
import json
import sqlite3
from contextlib import closing

from jiuwenswarm.agents.harness.team.duplex_ledger import ToolLedger, UncertainToolOutcome


class BrowserRestoreRequired(RuntimeError):
    """The live browser must be restored/reconciled before this task can resume."""


class BrowserCheckpoint:
    def __init__(self, path, scope, environment):
        if not scope or not environment:
            raise ValueError("Browser checkpoint requires a task scope and a live environment identity")
        self.ledger = ToolLedger(path)
        self.scope, self.environment = scope, environment
        with closing(self.ledger._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_checkpoints (
                scope TEXT PRIMARY KEY, environment TEXT NOT NULL,
                ordinal INTEGER NOT NULL, state TEXT NOT NULL,
                action TEXT, trajectory TEXT, native_version TEXT)""")

    def read(self):
        with closing(self.ledger._connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM browser_checkpoints WHERE scope=?", (self.scope,)).fetchone()
            return dict(row) if row else None

    def ready(self):
        row = self.read()
        if row is not None:
            if row["environment"] != self.environment:
                raise BrowserRestoreRequired("Recorded browser state belongs to another environment; do not replay actions")
            if row["state"] != "committed":
                raise UncertainToolOutcome("Browser action/trajectory commit incomplete; reconcile before continuing")
        return row

    def begin(self, action, native_version):
        row = self.ready()
        ordinal = 1 if row is None else row["ordinal"] + 1
        serialized = json.dumps(action, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        # Reserve and browser pending checkpoint are one SQLite transaction.
        with closing(self.ledger._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,NULL)",
                       (self.scope, f"browser:{ordinal}", "browser.env.step", digest, "prepared"))
            db.execute("INSERT OR REPLACE INTO browser_checkpoints VALUES (?,?,?,?,?,?,?)",
                       (self.scope, self.environment, ordinal, "prepared", serialized,
                        row["trajectory"] if row else None, native_version))
        return ordinal

    def commit(self, ordinal, trajectory):
        serialized = json.dumps(trajectory, ensure_ascii=False, sort_keys=True)
        with closing(self.ledger._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT ordinal,state,environment,action,trajectory FROM browser_checkpoints WHERE scope=?",
                             (self.scope,)).fetchone()
            if row is None or row[:3] != (ordinal, "prepared", self.environment):
                raise ValueError("Browser checkpoint does not match the pending action")
            if (not isinstance(trajectory, list) or len(trajectory) < 3
                    or trajectory[-2] != json.loads(row[3])
                    or not isinstance(trajectory[-1], dict) or "observation" not in trajectory[-1]):
                raise ValueError("Authoritative trajectory must include the prepared action and its observation")
            if row[4] is not None and trajectory[:-2] != json.loads(row[4]):
                raise ValueError("Browser trajectory diverged from the committed checkpoint")
            db.execute("UPDATE calls SET state='committed',result=? WHERE scope=? AND call_id=?",
                       (json.dumps({"external_checkpoint": ordinal, "trajectory": trajectory}),
                        self.scope, f"browser:{ordinal}"))
            db.execute("UPDATE browser_checkpoints SET state='committed',trajectory=? WHERE scope=?",
                       (serialized, self.scope))
        return self.ready()
