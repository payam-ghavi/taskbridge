"""Regression/scenario tests for the v2 N-provider engine, using fake clients
that reproduce each real API's JSON shapes (so canonical.py / providers.py run
completely unmodified). No network calls; no pytest dependency.

Run:  python tests/test_engine.py
"""
import itertools
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store
from app.engine import Config, sync_once
import app.canonical  # noqa: F401  (import check)

_id_counter = itertools.count(1)


def new_id(prefix):
    return f"{prefix}{next(_id_counter)}"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTodoist:
    """Mirrors real Todoist semantics: ``.items`` reflects only what changed
    since the last ``read()`` (a real incremental sync_token feed); the full
    current backend state lives in ``_all_items``/``_all_projects``. Projects
    are always fully refreshed on `read()` (see todoist_client.read()'s
    `_full_sync_projects` — list reconciliation needs the complete set)."""
    dry_run = False

    def __init__(self):
        self._all_items = {}
        self._all_projects = {}
        self._log = []            # [(seq, "item"|"project", id)]
        self._seq = 0
        self.items = {}
        self.projects = {}
        self.sync_token = "0"
        self.calls = {"add": 0, "update": 0, "delete": 0, "complete": 0}

    def _touch(self, kind, oid):
        self._seq += 1
        self._log.append((self._seq, kind, oid))

    def add_project(self, name, is_inbox=False):
        pid = new_id("tdproj")
        self._all_projects[pid] = {"id": pid, "name": name, "inbox_project": is_inbox,
                                    "is_deleted": False, "is_archived": False}
        self._touch("project", pid)
        return pid

    # ---- test-only helpers simulating "the user edited Todoist directly" ----
    def seed_item(self, **fields):
        iid = new_id("tditem")
        item = {"id": iid, "content": "", "description": "", "due": None, "priority": 1,
                "checked": False, "completed_at": None, "is_deleted": False, "parent_id": None}
        item.update(fields)
        self._all_items[iid] = item
        self._touch("item", iid)
        return iid

    def mutate_item(self, iid, **fields):
        self._all_items[iid].update(fields)
        self._touch("item", iid)

    def delete_item(self, iid):
        self._all_items[iid]["is_deleted"] = True
        self._touch("item", iid)

    def current_item(self, iid):
        return self._all_items.get(iid)

    # ---- real-shaped API ---------------------------------------------------
    def read(self):
        since = int(self.sync_token)
        changed_items, changed_projects, max_seq = set(), set(), since
        for seq, kind, oid in self._log:
            if seq <= since:
                continue
            max_seq = max(max_seq, seq)
            (changed_items if kind == "item" else changed_projects).add(oid)
        self.items = {iid: self._all_items[iid] for iid in changed_items if iid in self._all_items}
        self.projects = dict(self._all_projects)   # full refresh, every cycle
        self.sync_token = str(max_seq)

    def apply(self, commands):
        temp_map = {}
        for cmd in commands:
            t, args = cmd["type"], cmd.get("args", {})
            if t == "item_add":
                iid = new_id("tditem")
                self._all_items[iid] = {
                    "id": iid, "content": args.get("content", ""),
                    "description": args.get("description", ""),
                    "due": args.get("due"), "priority": args.get("priority", 1),
                    "checked": False, "completed_at": None,
                    "project_id": args["project_id"], "is_deleted": False, "parent_id": None,
                }
                temp_map[cmd["temp_id"]] = iid
                self._touch("item", iid)
                self.calls["add"] += 1
            elif t == "item_update":
                it = self._all_items[args["id"]]
                for k in ("content", "description"):
                    if k in args:
                        it[k] = args[k]
                if "due" in args:
                    it["due"] = args["due"]
                if "priority" in args:
                    it["priority"] = args["priority"]
                self._touch("item", args["id"])
                self.calls["update"] += 1
            elif t == "item_complete":
                iid = temp_map.get(args["id"], args["id"])
                self._all_items[iid]["checked"] = True
                self._touch("item", iid)
                self.calls["complete"] += 1
            elif t == "item_uncomplete":
                self._all_items[args["id"]]["checked"] = False
                self._touch("item", args["id"])
                self.calls["complete"] += 1
            elif t == "item_delete":
                if args["id"] in self._all_items:
                    self._all_items[args["id"]]["is_deleted"] = True
                self._touch("item", args["id"])
                self.calls["delete"] += 1
        return temp_map, {}


class FakeGraph:
    dry_run = False

    def __init__(self):
        self.lists = {}          # id -> {id, displayName, wellknownListName}
        self.tasks = {}          # list_id -> {id: task}
        self._log = {}           # list_id -> [ (seq, task_id) ] change log
        self._seq = 0
        self.refresh_token = "fake-ms-refresh"
        self.calls = {"add": 0, "update": 0, "delete": 0}

    def get_lists(self):
        return list(self.lists.values())

    def create_list(self, name):
        lid = new_id("mslist")
        self.lists[lid] = {"id": lid, "displayName": name, "wellknownListName": None}
        self.tasks[lid] = {}
        return self.lists[lid]

    def list_tasks(self, list_id):
        return list(self.tasks.get(list_id, {}).values())

    def _touch(self, list_id, task_id):
        self._seq += 1
        self._log.setdefault(list_id, []).append((self._seq, task_id))

    def create_task(self, list_id, body):
        tid = new_id("mstask")
        t = {"id": tid, "title": "", "body": {"content": "", "contentType": "text"},
             "dueDateTime": None, "importance": "normal", "status": "notStarted"}
        t.update(body)
        self.tasks[list_id][tid] = t
        self._touch(list_id, tid)
        self.calls["add"] += 1
        return t

    def update_task(self, list_id, task_id, body):
        if not body:
            return
        self.tasks[list_id][task_id].update(body)
        self._touch(list_id, task_id)
        self.calls["update"] += 1

    def delete_task(self, list_id, task_id):
        self.tasks[list_id].pop(task_id, None)
        self._touch(list_id, task_id)
        self.calls["delete"] += 1

    def delta(self, list_id, cursor):
        since = int(cursor) if cursor else 0
        changed_ids, max_seq = set(), since
        for seq, tid in self._log.get(list_id, []):
            if seq > since:
                changed_ids.add(tid)
                max_seq = max(max_seq, seq)
        out = []
        for tid in changed_ids:
            if tid in self.tasks.get(list_id, {}):
                out.append(self.tasks[list_id][tid])
            else:
                out.append({"id": tid, "@removed": {"reason": "deleted"}})
        return out, str(max_seq)


class FakeGoogle:
    dry_run = False

    def __init__(self):
        self.lists = {}          # id -> {id, title}
        self.tasks = {}          # list_id -> {id: task}
        self._default_id = None
        self.refresh_token = "fake-google-refresh"
        self.calls = {"add": 0, "update": 0, "delete": 0}

    def default_list_id(self):
        return self._default_id

    def get_lists(self):
        return list(self.lists.values())

    def create_list(self, name):
        lid = new_id("glist")
        self.lists[lid] = {"id": lid, "title": name}
        self.tasks[lid] = {}
        return self.lists[lid]

    def list_tasks(self, list_id):
        return list(self.tasks.get(list_id, {}).values())

    def create_task(self, list_id, body):
        tid = new_id("gtask")
        t = {"id": tid, "title": "", "notes": "", "due": None, "status": "needsAction"}
        t.update(body)
        self.tasks[list_id][tid] = t
        self.calls["add"] += 1
        return t

    def update_task(self, list_id, task_id, body):
        if not body:
            return
        self.tasks[list_id][task_id].update(body)
        self.calls["update"] += 1

    def delete_task(self, list_id, task_id):
        self.tasks[list_id].pop(task_id, None)
        self.calls["delete"] += 1


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return Store(path), path


def seed_connections(store):
    store.set_connection("todoist", "Test Todoist", {"token": "x"})
    store.set_connection("mstodo", "test@outlook.com", {"refresh_token": "x"})
    store.set_connection("google", "test@gmail.com", {"client_id": "x", "client_secret": "x", "refresh_token": "x"})
    store.commit()


FAILURES = []


def check(label, cond, detail=""):
    status = "OK  " if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def run():
    store, path = fresh_store()
    seed_connections(store)
    td, ms, gg = FakeTodoist(), FakeGraph(), FakeGoogle()
    clients = {"todoist": td, "mstodo": ms, "google": gg}
    cfg = Config(conflict_winner="todoist", match_existing=True)

    # default/inbox lists on each provider, deliberately named differently
    td_inbox = td.add_project("Inbox", is_inbox=True)
    ms_default = ms.create_list("Tasks")
    ms.lists[ms_default["id"]]["wellknownListName"] = "defaultList"
    g_default = gg.create_list("My Tasks")
    gg._default_id = g_default["id"]

    # --- Scenario A: list reconciliation, including default-list unification
    sync_once(store, clients, cfg)
    groups = store.all_list_groups()
    check("A1: default lists unify into exactly one group despite different names",
          len(groups) == 1, f"groups={groups}")
    members = groups[0]["members"] if groups else {}
    check("A2: unified default group has all three providers",
          set(members) == {"todoist", "mstodo", "google"}, f"members={members}")

    # a brand-new Todoist project should get a same-named list created on both others
    td_groceries = td.add_project("Groceries")
    sync_once(store, clients, cfg)
    groups = store.all_list_groups()
    g_groceries = next((g for g in groups if g["name"] == "Groceries"), None)
    check("A3: new Todoist project gets mirrored lists on mstodo + google",
          g_groceries is not None and set(g_groceries["members"]) == {"todoist", "mstodo", "google"},
          f"g_groceries={g_groceries}")

    # --- Scenario B: new task on Todoist propagates to both other providers
    tid = td.seed_item(content="Buy milk", project_id=td_groceries)
    sync_once(store, clients, cfg)
    group = store.task_group_for("todoist", tid)
    check("B1: task group created with all 3 links",
          group is not None and set(group["links"]) == {"todoist", "mstodo", "google"},
          f"group={group}")
    check("B2: mstodo got exactly one new task, google got exactly one new task",
          ms.calls["add"] == 1 and gg.calls["add"] == 1,
          f"ms.add={ms.calls['add']} gg.add={gg.calls['add']}")
    ms_list_id = group["links"]["mstodo"]["list_id"]
    g_list_id = group["links"]["google"]["list_id"]
    ms_task_id = group["links"]["mstodo"]["item_id"]
    g_task_id = group["links"]["google"]["item_id"]
    check("B3: mirrored titles match",
          ms.tasks[ms_list_id][ms_task_id]["title"] == "Buy milk"
          and gg.tasks[g_list_id][g_task_id]["title"] == "Buy milk")

    # --- Scenario C: a no-op cycle must NOT re-write anything (echo suppression)
    calls_before = (dict(td.calls), dict(ms.calls), dict(gg.calls))
    sync_once(store, clients, cfg)
    calls_after = (dict(td.calls), dict(ms.calls), dict(gg.calls))
    check("C1: idle cycle makes zero writes on any provider",
          calls_before == calls_after, f"before={calls_before} after={calls_after}")

    # --- Scenario D: an edit on Microsoft propagates to Todoist + Google
    ms.tasks[ms_list_id][ms_task_id]["title"] = "Buy oat milk"
    ms._touch(ms_list_id, ms_task_id)
    sync_once(store, clients, cfg)
    check("D1: Todoist title updated", td.current_item(tid)["content"] == "Buy oat milk",
          td.current_item(tid)["content"])
    check("D2: Google title updated", gg.tasks[g_list_id][g_task_id]["title"] == "Buy oat milk",
          gg.tasks[g_list_id][g_task_id]["title"])
    group = store.get_task_group(group["id"])
    check("D3: stored canon updated", group["canon"]["title"] == "Buy oat milk")

    # --- Scenario D2: idle again after the propagated writes (no echo storm)
    calls_before = (dict(td.calls), dict(ms.calls), dict(gg.calls))
    sync_once(store, clients, cfg)
    calls_after = (dict(td.calls), dict(ms.calls), dict(gg.calls))
    check("D4: cycle after a propagated edit makes zero further writes",
          calls_before == calls_after, f"before={calls_before} after={calls_after}")

    # --- Scenario E: completed + unmapped on Google must NOT resurrect onto others
    gid2 = new_id("gtask")
    gg.tasks[g_default["id"]][gid2] = {"id": gid2, "title": "Old finished thing", "notes": "",
                                        "due": None, "status": "completed"}
    add_before = (td.calls["add"], ms.calls["add"])
    sync_once(store, clients, cfg)
    add_after = (td.calls["add"], ms.calls["add"])
    check("E1: completed unmapped Google task not created on Todoist/MS",
          add_before == add_after, f"before={add_before} after={add_after}")
    check("E2: no task_group was created for it",
          store.task_group_for("google", gid2) is None)

    # --- Scenario F: deletion (detected via Google's full-fetch diff) propagates
    del_before = (td.calls["delete"], ms.calls["delete"])
    del gg.tasks[g_list_id][g_task_id]
    sync_once(store, clients, cfg)
    del_after = (td.calls["delete"], ms.calls["delete"])
    check("F1: deleting on Google deletes on Todoist + MS",
          del_after[0] == del_before[0] + 1 and del_after[1] == del_before[1] + 1,
          f"before={del_before} after={del_after}")
    check("F2: task group is gone", store.task_group_for("todoist", tid) is None)
    check("F3: Todoist item marked deleted", td.current_item(tid)["is_deleted"] is True)
    check("F4: MS item actually removed", ms_task_id not in ms.tasks[ms_list_id])

    # --- Scenario G: same-cycle conflict — configured winner (todoist) wins
    tid2 = td.seed_item(content="Pay rent", project_id=td_groceries)
    sync_once(store, clients, cfg)               # create + link on all 3
    group2 = store.task_group_for("todoist", tid2)
    ms_id2 = group2["links"]["mstodo"]["item_id"]
    sync_once(store, clients, cfg)                # settle / idle
    td.mutate_item(tid2, content="Pay RENT (todoist edit)")
    ms.tasks[ms_list_id][ms_id2]["title"] = "Pay rent (ms edit)"
    ms._touch(ms_list_id, ms_id2)
    sync_once(store, clients, cfg)
    check("G1: Todoist (configured winner) title wins on MS",
          ms.tasks[ms_list_id][ms_id2]["title"] == "Pay RENT (todoist edit)",
          ms.tasks[ms_list_id][ms_id2]["title"])
    check("G2: Todoist's own item keeps its own edit",
          td.current_item(tid2)["content"] == "Pay RENT (todoist edit)")

    store.close()
    os.remove(path)


def run_migration_test():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    import sqlite3, json, time
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, is_inbox INTEGER DEFAULT 0);
        CREATE TABLE list_pairs (todoist_project_id TEXT PRIMARY KEY, mstodo_list_id TEXT UNIQUE, name TEXT);
        CREATE TABLE task_map (todoist_id TEXT PRIMARY KEY, mstodo_id TEXT UNIQUE,
            todoist_project_id TEXT, mstodo_list_id TEXT, canon TEXT, updated_at REAL);
    """)
    kv = {
        "cfg:todoist_api_token": "v1-td-token",
        "cfg:todoist_name": "Old User",
        "ms_refresh_token": "v1-ms-refresh",
        "cfg:ms_email": "old@outlook.com",
        "todoist_sync_token": "v1-sync-token",
        "cfg:configured": "true",
        "cfg:conflict_winner": "todoist",
        "delta:mslistA": "v1-delta-link",
    }
    for k, v in kv.items():
        db.execute("INSERT INTO kv VALUES (?,?)", (k, v))
    db.execute("INSERT INTO list_pairs VALUES (?,?,?)", ("tdprojA", "mslistA", "Work"))
    canon = {"title": "Legacy task", "notes": "", "due": None, "important": False, "completed": False}
    db.execute("INSERT INTO task_map VALUES (?,?,?,?,?,?)",
               ("tditemA", "mstaskA", "tdprojA", "mslistA", json.dumps(canon), time.time()))
    db.commit()
    db.close()

    store = Store(path)
    check("H1: schema_version marked 2", store.get("schema_version") == "2")
    conns = store.all_connections()
    check("H2: todoist connection migrated", conns.get("todoist", {}).get("creds", {}).get("token") == "v1-td-token")
    check("H3: mstodo connection migrated", conns.get("mstodo", {}).get("creds", {}).get("refresh_token") == "v1-ms-refresh")
    check("H4: todoist cursor migrated", store.get_cursor("todoist", "account") == "v1-sync-token")
    check("H5: mstodo per-list cursor migrated", store.get_cursor("mstodo", "mslistA") == "v1-delta-link")
    check("H6: is_configured() true", store.is_configured() is True)
    g = store.list_group_for("todoist", "tdprojA")
    check("H7: list group migrated", g is not None and g["members"].get("mstodo") == "mslistA")
    tg = store.task_group_for("todoist", "tditemA")
    check("H8: task group migrated", tg is not None and tg["links"].get("mstodo", {}).get("item_id") == "mstaskA")
    check("H9: canon preserved", tg is not None and tg["canon"]["title"] == "Legacy task")
    store.close()
    os.remove(path)


if __name__ == "__main__":
    print("=== scenario tests (fake clients) ===")
    run()
    print("\n=== v1 -> v2 migration test ===")
    run_migration_test()
    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)
