"""The reconciliation engine — one pass = one `sync_once()` call.

Echo suppression: after writing a task we store the exact canonical we wrote in
`task_map.canon`. Next run the same task comes back in the other side's feed; its
canonical equals the stored one, so we skip it. A real edit changes the canonical
and is propagated.

An unmapped task that is already completed is skipped entirely — the engine never
resurrects completed history, only propagates completions of already-linked tasks.

Conflict (same mapping changed on both sides in one run): `cfg.conflict_winner`
decides — default "todoist".
"""
import dataclasses
import json
import logging
import uuid

from .canonical import item_to_canonical, task_to_canonical
from .graph_client import NotFound, canonical_to_ms_patch
from .todoist_client import cmd_item_add, cmd_item_delete, cmd_item_update

log = logging.getLogger("taskbridge.engine")


@dataclasses.dataclass
class Config:
    conflict_winner: str = "todoist"        # "todoist" | "mstodo"
    match_existing: bool = True
    dry_run: bool = False


def _norm(s):
    return (s or "").strip().lower()


def reconcile_lists(store, td, ms):
    ms_lists = ms.get_lists()
    ms_by_norm = {_norm(l["displayName"]): l for l in ms_lists}
    ms_default = next((l for l in ms_lists if l.get("wellknownListName") == "defaultList"), None)

    projects = [p for p in store.all_projects()]
    paired_projects = {p["todoist_project_id"] for p in store.all_pairs()}
    paired_lists = {p["mstodo_list_id"] for p in store.all_pairs()}

    for p in projects:
        if p["id"] in paired_projects:
            continue
        target = ms_default if (p["is_inbox"] and ms_default) else ms_by_norm.get(_norm(p["name"]))
        if target is None:
            target = ms.create_list(p["name"])
            if not target:
                log.info("would create To Do list %r", p["name"])
                continue
            ms_by_norm[_norm(p["name"])] = target
        store.add_pair(p["id"], target["id"], p["name"])
        paired_projects.add(p["id"])
        paired_lists.add(target["id"])

    proj_by_norm = {_norm(p["name"]): p for p in projects}
    inbox = next((p for p in projects if p["is_inbox"]), None)
    for l in ms_lists:
        if l["id"] in paired_lists:
            continue
        if l.get("wellknownListName") == "defaultList" and inbox:
            store.add_pair(inbox["id"], l["id"], l["displayName"])
            continue
        match = proj_by_norm.get(_norm(l["displayName"]))
        if match:
            store.add_pair(match["id"], l["id"], l["displayName"])
            continue
        new_pid = td.add_project(l["displayName"])
        if not new_pid:
            log.info("would create Todoist project %r", l["displayName"])
            continue
        store.upsert_project(new_pid, l["displayName"])
        store.add_pair(new_pid, l["id"], l["displayName"])


def _find_ms_match(ms, list_id, c):
    for t in ms.list_tasks(list_id):
        tc = task_to_canonical(t)
        if not tc["completed"] and tc["title"] == c["title"] and tc["due"] == c["due"]:
            return t
    return None


def _find_todoist_match(td, project_id, c, mapped_todoist_ids):
    for tid, item in td.items.items():
        if tid in mapped_todoist_ids or item.get("is_deleted") or item.get("parent_id"):
            continue
        if item.get("project_id") != project_id:
            continue
        ic = item_to_canonical(item)
        if not ic["completed"] and ic["title"] == c["title"] and ic["due"] == c["due"]:
            return tid
    return None


def _handle_todoist_item(store, ms, item, by_td, changed_ms_ids, cfg):
    tid = item["id"]
    row = by_td.get(tid)

    if item.get("is_deleted"):
        if row:
            ms.delete_task(row["mstodo_list_id"], row["mstodo_id"])
            store.delete_mapping(todoist_id=tid)
        return

    if item.get("parent_id"):
        return

    pair = store.pair_for_project(item.get("project_id"))
    if not pair:
        return

    c = item_to_canonical(item)

    if row:
        prev_c = json.loads(row["canon"])
        if c == prev_c:
            return
        if row["mstodo_id"] in changed_ms_ids and cfg.conflict_winner == "mstodo":
            return
        if row["mstodo_list_id"] != pair["mstodo_list_id"]:
            ms.delete_task(row["mstodo_list_id"], row["mstodo_id"])
            created = ms.create_task(pair["mstodo_list_id"], canonical_to_ms_patch(c))
            store.delete_mapping(todoist_id=tid)
            store.add_mapping(tid, created["id"], item["project_id"], pair["mstodo_list_id"], c)
            return
        ms.update_task(row["mstodo_list_id"], row["mstodo_id"], canonical_to_ms_patch(c, prev_c))
        store.update_canon(tid, c)
        return

    mstodo_id = None
    if cfg.match_existing:
        m = _find_ms_match(ms, pair["mstodo_list_id"], c)
        if m:
            mstodo_id = m["id"]
            m_c = task_to_canonical(m)
            if m_c != c:
                ms.update_task(pair["mstodo_list_id"], mstodo_id, canonical_to_ms_patch(c, m_c))
    if not mstodo_id:
        if c["completed"]:
            return
        created = ms.create_task(pair["mstodo_list_id"], canonical_to_ms_patch(c))
        mstodo_id = created["id"]
    store.add_mapping(tid, mstodo_id, item["project_id"], pair["mstodo_list_id"], c)


def _handle_ms_task(store, td, t, list_id, by_ms, changed_td_ids, queue, pending, cfg):
    mid = t["id"]
    row = by_ms.get(mid)

    if t.get("@removed"):
        if row:
            queue.append(cmd_item_delete(row["todoist_id"]))
            store.delete_mapping(mstodo_id=mid)
        return

    c = task_to_canonical(t)
    pair = store.pair_for_list(list_id)

    if row:
        prev_c = json.loads(row["canon"])
        if c == prev_c:
            return
        if row["todoist_id"] in changed_td_ids and cfg.conflict_winner == "todoist":
            return
        cmds = cmd_item_update(row["todoist_id"], c, prev_c)
        if cmds:
            queue.extend(cmds)
            store.update_canon(row["todoist_id"], c)
        return

    if not pair:
        return

    existing_tid = None
    if cfg.match_existing:
        existing_tid = _find_todoist_match(
            td, pair["todoist_project_id"], c,
            {r["todoist_id"] for r in store.by_todoist().values()},
        )
    if existing_tid:
        store.add_mapping(existing_tid, mid, pair["todoist_project_id"], list_id, c)
        return

    if c["completed"]:
        return

    temp = str(uuid.uuid4())
    queue.extend(cmd_item_add(c, pair["todoist_project_id"], temp))
    pending.append({
        "temp_id": temp, "mstodo_id": mid, "list_id": list_id,
        "project_id": pair["todoist_project_id"], "canon": c,
    })


def sync_once(store, td, ms, cfg):
    td.read()
    for pid, pr in td.projects.items():
        if pr.get("is_deleted") or pr.get("is_archived"):
            store.delete_project(pid)
        else:
            store.upsert_project(
                pid, pr.get("name", ""),
                bool(pr.get("inbox_project") or pr.get("is_inbox_project")),
            )
    store.commit()

    reconcile_lists(store, td, ms)
    store.commit()

    ms_changes = []
    for pair in store.all_pairs():
        lid = pair["mstodo_list_id"]
        tasks, new_link = ms.delta(lid, store.get(f"delta:{lid}"))
        for t in tasks:
            ms_changes.append((lid, t))
        if new_link:
            store.set(f"delta:{lid}", new_link)
    store.commit()

    changed_td_ids = set(td.items.keys())
    changed_ms_ids = {t["id"] for _, t in ms_changes if not t.get("@removed")}

    by_td = store.by_todoist()
    for tid, item in list(td.items.items()):
        try:
            _handle_todoist_item(store, ms, item, by_td, changed_ms_ids, cfg)
            store.commit()
        except NotFound:
            store.delete_mapping(todoist_id=tid)
            store.commit()
        except Exception:
            log.exception("Todoist item %s failed", tid)

    queue, pending = [], []
    by_ms = store.by_mstodo()
    for lid, t in ms_changes:
        try:
            _handle_ms_task(store, td, t, lid, by_ms, changed_td_ids, queue, pending, cfg)
        except Exception:
            log.exception("To Do task %s failed", t.get("id"))
    store.commit()

    if queue:
        temp_map, _ = td.apply(queue)
        for p in pending:
            real = temp_map.get(p["temp_id"])
            if real:
                store.add_mapping(real, p["mstodo_id"], p["project_id"], p["list_id"], p["canon"])
            elif not cfg.dry_run:
                log.warning("no id returned for new Todoist task (mstodo %s)", p["mstodo_id"])
        store.commit()

    store.set("todoist_sync_token", td.sync_token)
    store.set("ms_refresh_token", ms.refresh_token)
    store.commit()
    return {"todoist_changes": len(changed_td_ids), "mstodo_changes": len(ms_changes)}
