"""Per-provider adapter functions the engine calls through.

Todoist keeps its own command-batching model (queue writes, apply once) since
that's materially more efficient for it and already proven; Microsoft To Do and
Google Tasks write immediately over REST. The engine doesn't care which —
``queue_or_apply`` below is the only place that distinction shows up.
"""
import uuid

from . import canonical as C
from .graph_client import NotFound as GraphNotFound
from .graph_client import canonical_to_ms_patch
from .google_client import NotFound as GoogleNotFound
from .google_client import canonical_to_google_body
from .todoist_client import cmd_item_add, cmd_item_delete, cmd_item_update

NotFoundErrors = (GraphNotFound, GoogleNotFound)


def to_canonical(provider, raw_item):
    if provider == "todoist":
        return C.item_to_canonical(raw_item)
    if provider == "mstodo":
        return C.task_to_canonical(raw_item)
    if provider == "google":
        return C.gtask_to_canonical(raw_item)
    raise ValueError(provider)


def get_lists(provider, client):
    """-> [{"id":..., "name":..., "is_default": bool}]"""
    if provider == "todoist":
        return [{"id": pid, "name": p.get("name", ""),
                  "is_default": bool(p.get("inbox_project") or p.get("is_inbox_project"))}
                for pid, p in client.projects.items()
                if not (p.get("is_deleted") or p.get("is_archived"))]
    if provider == "mstodo":
        return [{"id": l["id"], "name": l["displayName"],
                  "is_default": l.get("wellknownListName") == "defaultList"}
                for l in client.get_lists()]
    if provider == "google":
        default_id = client.default_list_id()
        return [{"id": l["id"], "name": l["title"], "is_default": l["id"] == default_id}
                for l in client.get_lists()]
    raise ValueError(provider)


def create_list(provider, client, name):
    """-> new list id, or None (dry-run / failed)."""
    if provider == "todoist":
        return client.add_project(name)
    if provider == "mstodo":
        created = client.create_list(name)
        return created["id"] if created else None
    if provider == "google":
        created = client.create_list(name)
        return created["id"] if created else None
    raise ValueError(provider)


def find_unmapped_match(provider, client, list_id, canon, mapped_ids):
    """Search `list_id` for an unmapped, not-completed item matching canon's
    title+due — used to link pre-existing tasks instead of duplicating them."""
    if provider == "todoist":
        for tid, item in client.items.items():
            if tid in mapped_ids or item.get("is_deleted") or item.get("parent_id"):
                continue
            if item.get("project_id") != list_id:
                continue
            ic = C.item_to_canonical(item)
            if not ic["completed"] and ic["title"] == canon["title"] and ic["due"] == canon["due"]:
                return tid, item
        return None
    if provider == "mstodo":
        for t in client.list_tasks(list_id):
            if t["id"] in mapped_ids:
                continue
            tc = C.task_to_canonical(t)
            if not tc["completed"] and tc["title"] == canon["title"] and tc["due"] == canon["due"]:
                return t["id"], t
        return None
    if provider == "google":
        for t in client.list_tasks(list_id):
            if t["id"] in mapped_ids:
                continue
            tc = C.gtask_to_canonical(t)
            if not tc["completed"] and tc["title"] == canon["title"] and tc["due"] == canon["due"]:
                return t["id"], t
        return None
    raise ValueError(provider)


def queue_or_apply(provider, client, queue, list_id, canon, prev_canon=None):
    """Create a task. Returns an item id immediately for REST providers, or
    None for Todoist (the id only exists after the queue is flushed — caller
    must track a "pending" entry and resolve it from the temp-id map)."""
    if provider == "todoist":
        temp_id = uuid.uuid4().hex
        queue.extend(cmd_item_add(canon, list_id, temp_id))
        return None, temp_id
    if provider == "mstodo":
        created = client.create_task(list_id, canonical_to_ms_patch(canon))
        return created["id"], None
    if provider == "google":
        created = client.create_task(list_id, canonical_to_google_body(canon))
        return created["id"], None
    raise ValueError(provider)


def update(provider, client, queue, list_id, item_id, new_canon, prev_canon):
    if provider == "todoist":
        queue.extend(cmd_item_update(item_id, new_canon, prev_canon))
        return
    if provider == "mstodo":
        client.update_task(list_id, item_id, canonical_to_ms_patch(new_canon, prev_canon))
        return
    if provider == "google":
        client.update_task(list_id, item_id, canonical_to_google_body(new_canon, prev_canon))
        return
    raise ValueError(provider)


def delete(provider, client, queue, list_id, item_id):
    if provider == "todoist":
        queue.append(cmd_item_delete(item_id))
        return
    if provider == "mstodo":
        client.delete_task(list_id, item_id)
        return
    if provider == "google":
        client.delete_task(list_id, item_id)
        return
    raise ValueError(provider)
