"""The reconciliation engine — one pass = one `sync_once()` call.

Generalized to any 2+ connected providers (Todoist, Microsoft To Do, Google
Tasks): a task can be linked across all of them at once, not just a fixed
pair. Everything routes through ``task_groups``/``task_links`` and
``list_groups``/``list_group_members`` (see store.py) instead of the old
Todoist<->To Do-specific columns.

Echo suppression: after writing a task we store the exact canonical we wrote
in the task group. Next run the same task comes back in that provider's own
feed; its canonical equals the stored one (on every field that provider can
represent — see canonical.UNSUPPORTED), so we skip it. A real edit changes the
canonical and is propagated to every OTHER linked provider.

An unmapped task that is already completed is never used to newly link/create
on a provider that never had it — the engine doesn't resurrect completed
history, only propagates completions of already-linked tasks.

Conflict (the same task group changed by two providers in one cycle):
``cfg.conflict_winner`` (a provider name) decides — default "todoist". A
non-winning provider's change is dropped when the winner also touched the same
group this cycle; two non-winners touching the same group is last-one-wins
(logged), which is rare and self-heals next cycle either way.

Todoist keeps its command-batching model (queue writes, apply once); Microsoft
To Do and Google Tasks write immediately over REST — see providers.py.
"""
import logging

from . import canonical as C
from . import providers

log = logging.getLogger("taskbridge.engine")


class Config:
    def __init__(self, conflict_winner="todoist", match_existing=True, dry_run=False):
        self.conflict_winner = conflict_winner
        self.match_existing = match_existing
        self.dry_run = dry_run


def _norm(s):
    return (s or "").strip().lower()


_DEFAULT_KEY = "\x00default"


def _identity_key(list_obj):
    return _DEFAULT_KEY if list_obj["is_default"] else _norm(list_obj["name"])


def _existing_group_key(members, provider_lists):
    """The identity key an already-formed group would be filed under, so a
    newly-connected provider's default list finds it instead of spawning a
    second, disconnected "default" group. A group's default-ness isn't
    stored on list_groups itself, so it's inferred from whether any current
    member list is flagged is_default (the same signal used for a brand-new
    list)."""
    for provider, list_id in members.items():
        for l in provider_lists.get(provider, []):
            if l["id"] == list_id and l["is_default"]:
                return _DEFAULT_KEY
    return None


def reconcile_lists(store, clients):
    """Match each connected provider's lists to the others by name (default /
    inbox lists always unify, regardless of what each provider calls them),
    then create the missing side on every provider that doesn't have one yet."""
    provider_lists = {p: providers.get_lists(p, c) for p, c in clients.items()}
    already_grouped = {(p, lid) for g in store.all_list_groups() for p, lid in g["members"].items()}
    group_id_by_key = {}
    for g in store.all_list_groups():
        key = _existing_group_key(g["members"], provider_lists) or _norm(g["name"])
        group_id_by_key[key] = g["id"]

    pending = {}
    for provider, lists in provider_lists.items():
        for l in lists:
            if (provider, l["id"]) in already_grouped:
                continue
            pending.setdefault(_identity_key(l), {})[provider] = l

    for key, by_provider in pending.items():
        group_id = group_id_by_key.get(key)
        if group_id is None:
            display_name = next(iter(by_provider.values()))["name"]
            group_id = store.add_list_group(display_name)
            group_id_by_key[key] = group_id
        for provider, l in by_provider.items():
            store.add_list_group_member(group_id, provider, l["id"])

    for g in store.all_list_groups():
        members = g["members"]
        for provider, client in clients.items():
            if provider in members:
                continue
            new_id = providers.create_list(provider, client, g["name"])
            if not new_id:
                continue    # dry-run, or the client already logged a failure
            store.add_list_group_member(g["id"], provider, new_id)
            members[provider] = new_id


def _is_removed(provider, raw_item):
    if provider == "todoist":
        return bool(raw_item.get("is_deleted"))
    if provider == "mstodo":
        return bool(raw_item.get("@removed"))
    return False


def _gather_changes(store, provider, client):
    """-> [(list_id, item_id, raw_item_or_None)] ; None raw_item = removed."""
    out = []
    if provider == "todoist":
        for tid, item in client.items.items():
            out.append((item.get("project_id"), tid, item))
        return out

    if provider == "mstodo":
        for g in store.all_list_groups():
            list_id = g["members"].get("mstodo")
            if not list_id:
                continue
            tasks, new_link = client.delta(list_id, store.get_cursor("mstodo", list_id))
            for t in tasks:
                out.append((list_id, t["id"], t))
            if new_link:
                store.set_cursor("mstodo", list_id, new_link)
        return out

    if provider == "google":
        # No delta/removed feed — full-fetch each list and diff against what
        # we already have linked to notice deletions ourselves.
        for g in store.all_list_groups():
            list_id = g["members"].get("google")
            if not list_id:
                continue
            items = client.list_tasks(list_id)
            current_ids = {t["id"] for t in items}
            known_ids = store.mapped_item_ids("google", list_id=list_id)
            for removed_id in known_ids - current_ids:
                out.append((list_id, removed_id, None))
            for t in items:
                out.append((list_id, t["id"], t))
        return out

    raise ValueError(provider)


def _handle_removed_item(store, clients, queue, provider, item_id):
    group = store.task_group_for(provider, item_id)
    if not group:
        return
    for other_provider, link in group["links"].items():
        if other_provider == provider or other_provider not in clients:
            continue
        try:
            providers.delete(other_provider, clients[other_provider], queue,
                              link["list_id"], link["item_id"])
        except providers.NotFoundErrors:
            pass
    store.delete_task_group(group["id"])


def _handle_existing_item(store, clients, queue, group, provider, canon, changed_ids, cfg):
    prev = group["canon"]
    if not C.relevant_diff(canon, prev, provider):
        return

    for other_provider, link in group["links"].items():
        if other_provider == provider:
            continue
        if link["item_id"] in changed_ids.get(other_provider, ()) and cfg.conflict_winner == other_provider:
            return   # back off — the configured winner also touched this group this cycle

    new_canon = C.merge(prev, canon, provider)
    if new_canon == prev:
        return
    store.update_task_group_canon(group["id"], new_canon)
    for other_provider, link in group["links"].items():
        if other_provider == provider or other_provider not in clients:
            continue
        providers.update(other_provider, clients.get(other_provider), queue,
                          link["list_id"], link["item_id"], new_canon, prev)


def _handle_new_item(store, clients, queue, pending, provider, list_id, item_id, canon, cfg):
    lg = store.list_group_for(provider, list_id)
    if not lg:
        return   # list not paired yet; reconcile_lists will catch it next cycle

    links = {provider: (item_id, list_id)}          # provider -> (item_id, list_id)
    merged_canon = dict(canon)

    if cfg.match_existing:
        for other_provider, other_list_id in lg["members"].items():
            if other_provider == provider or other_provider not in clients:
                continue
            mapped_ids = store.mapped_item_ids(other_provider)
            match = providers.find_unmapped_match(
                other_provider, clients[other_provider], other_list_id, canon, mapped_ids)
            if match:
                other_id, other_raw = match
                links[other_provider] = (other_id, other_list_id)
                merged_canon = C.merge(merged_canon, providers.to_canonical(other_provider, other_raw),
                                        other_provider)

    if canon["completed"] and len(links) < len(lg["members"]):
        # Don't resurrect completed history onto a provider that never had this
        # task — but if we matched it elsewhere, keep the link(s) we found.
        if len(links) >= 2:
            store.create_task_group(merged_canon, links)
        return

    temp_id, todoist_list_id = None, None
    for other_provider, other_list_id in lg["members"].items():
        if other_provider in links:
            continue
        if other_provider == "todoist":
            _, temp_id = providers.queue_or_apply("todoist", None, queue, other_list_id, merged_canon)
            todoist_list_id = other_list_id
            continue
        if other_provider not in clients:
            continue
        new_id, _ = providers.queue_or_apply(other_provider, clients[other_provider], queue,
                                              other_list_id, merged_canon)
        links[other_provider] = (new_id, other_list_id)

    if temp_id:
        pending.append({"temp_id": temp_id, "todoist_list_id": todoist_list_id,
                         "immediate_links": links, "canon": merged_canon})
    elif len(links) >= 2:
        store.create_task_group(merged_canon, links)


def _handle_change(store, clients, queue, pending, provider, list_id, item_id, raw_item, changed_ids, cfg):
    if provider == "todoist" and raw_item is not None and raw_item.get("parent_id"):
        return   # sub-tasks are out of scope
    if raw_item is None or _is_removed(provider, raw_item):
        _handle_removed_item(store, clients, queue, provider, item_id)
        return

    canon = providers.to_canonical(provider, raw_item)
    group = store.task_group_for(provider, item_id)
    if group:
        _handle_existing_item(store, clients, queue, group, provider, canon, changed_ids, cfg)
    else:
        _handle_new_item(store, clients, queue, pending, provider, list_id, item_id, canon, cfg)


def sync_once(store, clients, cfg):
    """clients: {provider_name: client_instance} for every connected provider."""
    if "todoist" in clients:
        clients["todoist"].read()

    reconcile_lists(store, clients)
    store.commit()

    raw_changes = {p: _gather_changes(store, p, c) for p, c in clients.items()}
    store.commit()   # _gather_changes persists mstodo delta cursors as it goes
    changed_ids = {p: {iid for _, iid, _ in items} for p, items in raw_changes.items()}

    queue, pending = [], []
    for provider, items in raw_changes.items():
        for list_id, item_id, raw_item in items:
            try:
                _handle_change(store, clients, queue, pending, provider, list_id, item_id,
                                raw_item, changed_ids, cfg)
                store.commit()
            except providers.NotFoundErrors:
                store.remove_task_link(provider, item_id)
                store.commit()
            except Exception:
                log.exception("%s item %s failed", provider, item_id)

    if "todoist" in clients and queue:
        temp_map, _ = clients["todoist"].apply(queue)
        for p in pending:
            links = dict(p["immediate_links"])
            if p["temp_id"]:
                real = temp_map.get(p["temp_id"])
                if real:
                    links["todoist"] = (real, p["todoist_list_id"])
                elif not cfg.dry_run:
                    log.warning("no id returned for new Todoist task %r", p["canon"]["title"])
            if len(links) >= 2:
                store.create_task_group(p["canon"], links)
        store.commit()

    if "todoist" in clients:
        store.set_cursor("todoist", "account", clients["todoist"].sync_token)
    for provider in ("mstodo", "google"):
        if provider in clients:
            conn = store.get_connection(provider)
            store.update_creds(provider, {**conn["creds"], "refresh_token": clients[provider].refresh_token})
    store.commit()

    return {p: len(ids) for p, ids in changed_ids.items()}
