"""Meeting-scoped action tracking. Never writes the cluster task board."""
from copy import deepcopy


class MeetingActions:
    def __init__(self):
        self.items = {}
        self.phase = "discussion"
        self.work_versions = {}

    def snapshot(self):
        return {"phase": self.phase, "actions": deepcopy(list(self.items.values()))}

    def pause(self, actor, leader, action_ids, note):
        if actor != leader:
            raise ValueError("Only the Leader may pause invalidated work")
        if not isinstance(action_ids, list) or not action_ids or len(action_ids) > 50 or any(not isinstance(k, str) or k not in self.items for k in action_ids):
            raise ValueError("Provide existing action_ids; use status once if needed")
        if not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise ValueError("Explain the user change and what work must stop")
        affected = set()
        for key in set(action_ids):
            item = self.items[key]
            if item["status"] in {"cancelled", "paused"}:
                continue
            item.update(status="paused", note=note.strip(), revision=item["revision"] + 1)
            affected.update((item["owner"], item["reviewer"]))
        for member in affected:
            self.work_versions[member] = self.work_versions.get(member, 0) + 1
        if affected:
            self.phase = "adjusting"
        return affected

    def execution_paused(self, member):
        owned = [a for a in self.items.values() if a["owner"] == member]
        return any(a["status"] == "paused" for a in owned) and not any(a["status"] in {"assigned", "running"} for a in owned)

    def update(self, actor, leader, names, data):
        op = data.get("operation")
        if op == "status":
            return self.snapshot(), ()
        if op == "finish":
            if actor != leader:
                raise ValueError("Only the Leader may finish the agenda")
            unresolved = [a for a in self.items.values() if a["status"] not in {"accepted", "cancelled", "blocked"}]
            if unresolved:
                raise ValueError("Agenda still has assigned/running/submitted actions: " + ", ".join(a["action_id"] for a in unresolved))
            self.phase = "waiting" if any(a["status"] == "blocked" for a in self.items.values()) else "finished"
            return self.snapshot(), ()
        key = data.get("action_id")
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise ValueError("Provide action_id (1-100 characters)")
        def required(field, limit=2000):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"Provide {field} (1-{limit} characters)")
            return value.strip()
        if op == "assign":
            if actor != leader:
                raise ValueError("Only the Leader may assign meeting actions")
            if key in self.items:
                raise ValueError("Action exists; rework it or cancel it before assigning a new action_id")
            if len(self.items) >= 50:
                raise ValueError("Keep at most 50 actions in one meeting")
            owner, reviewer = required("owner", 255), required("reviewer", 255)
            if owner not in names or reviewer not in names or owner == reviewer:
                raise ValueError("Choose distinct active roster members for owner and reviewer")
            item = dict(action_id=key, owner=owner, reviewer=reviewer, goal=required("goal"),
                        acceptance=required("acceptance"), status="assigned", evidence=[], note="", revision=1)
        else:
            if key not in self.items:
                raise ValueError("Unknown meeting action")
            item = deepcopy(self.items[key])
            if data.get("revision") != item["revision"]:
                raise ValueError("Stale action revision; read current meeting actions and retry")
            if item["status"] == "paused" and op not in {"cancel", "rework"}:
                raise ValueError("This action was paused after a user change; Leader must cancel or rework it before execution/review")
            if item["status"] == "paused" and op == "rework" and actor != leader:
                raise ValueError("Only Leader may rework paused actions after investigating the new direction")
            if item["status"] == "cancelled":
                raise ValueError("Cancelled action cannot be changed; assign a new action_id")
            if op in {"start", "submit", "block"}:
                if actor != item["owner"]:
                    raise ValueError("Only the assigned owner may report execution")
                if op == "start":
                    if item["status"] not in {"assigned", "blocked"}:
                        raise ValueError("Start requires assigned or blocked status")
                    item.update(status="running", note="", evidence=[])
                elif op == "submit":
                    if item["status"] not in {"assigned", "running"}:
                        raise ValueError("Submit requires assigned or running status")
                    evidence = data.get("evidence")
                    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 10 or any(not isinstance(e, str) or not e.strip() or len(e) > 1000 for e in evidence):
                        raise ValueError("Submit 1-10 artifact paths/test results; plans alone are not completion")
                    item.update(status="submitted", evidence=[e.strip() for e in evidence], note=required("note"))
                else:
                    if item["status"] not in {"assigned", "running", "submitted"}:
                        raise ValueError("Rework an accepted action before blocking it")
                    item.update(status="blocked", note=required("note"))
            elif op == "accept":
                if actor != item["reviewer"] or item["status"] != "submitted":
                    raise ValueError("Only the designated reviewer may accept a submitted action")
                item.update(status="accepted", note=required("note"))
            elif op == "rework":
                if actor not in {leader, item["reviewer"]}:
                    raise ValueError("Only Leader or reviewer may request rework")
                item.update(status="assigned", evidence=[], note=required("note"))
            elif op == "cancel":
                if actor != leader:
                    raise ValueError("Only Leader may cancel an action")
                item.update(status="cancelled", note=required("note"))
            else:
                raise ValueError("Unknown action operation")
            item["revision"] += 1
        self.items[key] = item
        self.phase = "adjusting" if any(a["status"] == "paused" for a in self.items.values()) else "execution"
        targets = {
            "assign": {item["owner"]}, "start": set(),
            "submit": {leader, item["reviewer"]}, "block": {leader},
            "accept": {leader, item["owner"]}, "rework": {item["owner"]},
            "cancel": {item["owner"], item["reviewer"]},
        }
        recipients = tuple(sorted(targets[op] - {actor}))
        return self.snapshot(), recipients
