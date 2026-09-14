"""The 'canonical task' — the small common denominator every connected app can
represent.

Everything is normalised so a task in Microsoft To Do, Todoist, and Google Tasks
all produce an *identical* dict here; that identity is what lets the engine tell
a real edit from an echo of its own write.

Not every provider can represent every field (Google Tasks has no priority).
A provider that can't represent a field reports it as ``None`` instead of a
default value, so a per-provider read never looks like it "changed" a field it
simply has no opinion about — see ``UNSUPPORTED`` and ``relevant_diff``/``merge``.
"""
import hashlib
import json
import re

# Fields a given provider cannot represent at all. The engine ignores these
# fields when comparing that provider's report to the group's stored canon,
# and never sends them when writing a task to that provider.
UNSUPPORTED = {
    "todoist": set(),
    "mstodo": set(),
    "google": {"important"},
}


def _norm_text(s):
    if not s:
        return ""
    return re.sub(r"\r\n?", "\n", s).strip()


def _norm_due(s):
    """Return 'YYYY-MM-DD' or None. Time-of-day is intentionally dropped."""
    if not s:
        return None
    return str(s)[:10]


def canonical(title, notes, due, important, completed):
    return {
        "title": _norm_text(title),
        "notes": _norm_text(notes),
        "due": _norm_due(due),
        "important": None if important is None else bool(important),
        "completed": bool(completed),
    }


def canon_hash(c):
    return hashlib.sha1(
        json.dumps(c, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def relevant_diff(new_c, prev_c, provider):
    """True if new_c differs from prev_c in a field `provider` can represent."""
    unsupported = UNSUPPORTED.get(provider, set())
    return any(new_c[k] != prev_c[k] for k in new_c if k not in unsupported)


def merge(prev_c, new_c, provider):
    """new_c as reported by `provider`, merged onto the group's stored canon —
    fields `provider` can't represent keep the group's previous value."""
    unsupported = UNSUPPORTED.get(provider, set())
    out = dict(prev_c)
    for k, v in new_c.items():
        if k in unsupported:
            continue
        out[k] = v
    return out


# ---- Todoist -----------------------------------------------------------

def item_to_canonical(item):
    due = None
    d = item.get("due")
    if d:
        due = d.get("date") or d.get("datetime")
    priority = item.get("priority") or 1          # 1=none .. 4=urgent
    checked = item.get("checked")
    if checked is None:
        checked = item.get("completed_at") is not None
    return canonical(
        item.get("content"),
        item.get("description"),
        due,
        int(priority) >= 3,                       # P1/P2 -> important
        bool(checked),
    )


# ---- Microsoft To Do -----------------------------------------------------

def task_to_canonical(t):
    due = None
    dd = t.get("dueDateTime")
    if dd and dd.get("dateTime"):
        due = dd["dateTime"][:10]
    notes = (t.get("body") or {}).get("content") or ""
    return canonical(
        t.get("title"),
        notes,
        due,
        (t.get("importance") == "high"),
        (t.get("status") == "completed"),
    )


# ---- Google Tasks --------------------------------------------------------

def gtask_to_canonical(t):
    """Google Tasks has no priority/importance field at all — always None."""
    due = t.get("due")
    return canonical(
        t.get("title"),
        t.get("notes"),
        due[:10] if due else None,
        None,
        (t.get("status") == "completed"),
    )
