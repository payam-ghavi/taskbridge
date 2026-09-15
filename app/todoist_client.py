"""Thin wrapper over the Todoist Sync API (unified v1).

The Sync API gives an *incremental* feed: each call returns a ``sync_token``;
passing it back next time yields only what changed. That's the Todoist half of
the change-detection the engine relies on.
"""
import json
import logging
import time
import uuid

import requests

log = logging.getLogger("taskbridge.todoist")

BASE = "https://api.todoist.com/api/v1"


def _uuid():
    return str(uuid.uuid4())


def validate_token(token):
    """Return the account's name/email, or raise ValueError if the token is bad."""
    r = requests.post(f"{BASE}/sync", headers={"Authorization": f"Bearer {token}"},
                      data={"sync_token": "*", "resource_types": '["user"]'}, timeout=30)
    if r.status_code in (401, 403):
        raise ValueError("Todoist rejected that token.")
    r.raise_for_status()
    u = (r.json() or {}).get("user")
    if not u:
        raise ValueError("Todoist rejected that token.")
    return u.get("full_name") or u.get("email") or "Todoist account"


class TodoistClient:
    def __init__(self, token, sync_token, base=BASE, dry_run=False):
        self.base = base.rstrip("/")
        self.dry_run = dry_run
        self.sync_token = sync_token or "*"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.items = {}
        self.projects = {}

    def _sync(self, resource_types, commands=None):
        data = {"sync_token": self.sync_token, "resource_types": json.dumps(resource_types)}
        if commands:
            data["commands"] = json.dumps(commands)
        for attempt in range(5):
            r = self.s.post(f"{self.base}/sync", data=data, timeout=90)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            r.raise_for_status()
            break
        j = r.json()
        self.sync_token = j["sync_token"]
        for it in j.get("items", []):
            self.items[it["id"]] = it
        for pr in j.get("projects", []):
            self.projects[pr["id"]] = pr
        return j

    def read(self):
        """Incremental items+projects since ``sync_token``, plus a *full*
        projects refresh — list reconciliation needs the complete current
        project set every cycle, not just the ones that changed, and unlike
        items (which can be large), projects are cheap to fetch in full."""
        j = self._sync(["projects", "items"])
        self._full_sync_projects()
        return j

    def _full_sync_projects(self):
        data = {"sync_token": "*", "resource_types": json.dumps(["projects"])}
        r = self.s.post(f"{self.base}/sync", data=data, timeout=90)
        r.raise_for_status()
        for pr in r.json().get("projects", []):
            self.projects[pr["id"]] = pr

    def apply(self, commands):
        if not commands or self.dry_run:
            return {}, {}
        temp_map, status = {}, {}
        for i in range(0, len(commands), 90):
            j = self._sync(["projects", "items"], commands[i:i + 90])
            temp_map.update(j.get("temp_id_mapping", {}))
            status.update(j.get("sync_status", {}))
        for cid, result in status.items():
            if result != "ok" and not (isinstance(result, dict) and result.get("error_code") is None):
                log.warning("todoist command %s -> %s", cid, result)
        return temp_map, status

    def add_project(self, name):
        cmd_uuid = _uuid()
        temp = _uuid()
        temp_map, status = self.apply([{
            "type": "project_add", "temp_id": temp, "uuid": cmd_uuid, "args": {"name": name},
        }])
        if temp not in temp_map:
            raise RuntimeError(f"Todoist rejected creating project {name!r}: {status.get(cmd_uuid)}")
        return temp_map[temp]

    def delete_project(self, project_id):
        cmd_uuid = _uuid()
        _, status = self.apply([{"type": "project_delete", "uuid": cmd_uuid, "args": {"id": project_id}}])
        result = status.get(cmd_uuid)
        if result not in ("ok", None) and not (isinstance(result, dict) and result.get("error_code") is None):
            raise RuntimeError(f"Todoist rejected deleting project {project_id!r}: {result}")


def _due_arg(c):
    """due_time is always UTC (that's what Microsoft's reminderDateTime and
    Google's due both give us) -- mark it explicitly with a trailing 'Z' so
    Todoist converts it to the user's local time for display instead of
    treating the bare digits as already being local wall-clock time."""
    if not c["due"]:
        return None
    if c.get("due_time"):
        return {"date": f"{c['due']}T{c['due_time']}:00Z"}
    return {"date": c["due"]}


def cmd_item_add(c, project_id, temp_id):
    args = {"content": c["title"], "project_id": project_id}
    if c["notes"]:
        args["description"] = c["notes"]
    if c["due"]:
        args["due"] = _due_arg(c)
    if c["important"]:
        args["priority"] = 4
    cmds = [{"type": "item_add", "temp_id": temp_id, "uuid": _uuid(), "args": args}]
    if c["completed"]:
        cmds.append({"type": "item_complete", "uuid": _uuid(), "args": {"id": temp_id}})
    return cmds


def cmd_item_update(item_id, new_c, prev_c):
    cmds = []
    args = {"id": item_id}
    if new_c["title"] != prev_c["title"]:
        args["content"] = new_c["title"]
    if new_c["notes"] != prev_c["notes"]:
        args["description"] = new_c["notes"]
    if new_c["due"] != prev_c["due"] or new_c.get("due_time") != prev_c.get("due_time"):
        args["due"] = _due_arg(new_c)
    if new_c["important"] != prev_c["important"]:
        args["priority"] = 4 if new_c["important"] else 1
    if len(args) > 1:
        cmds.append({"type": "item_update", "uuid": _uuid(), "args": args})
    if new_c["completed"] != prev_c["completed"]:
        cmds.append({
            "type": "item_complete" if new_c["completed"] else "item_uncomplete",
            "uuid": _uuid(), "args": {"id": item_id},
        })
    return cmds


def cmd_item_delete(item_id):
    return {"type": "item_delete", "uuid": _uuid(), "args": {"id": item_id}}
