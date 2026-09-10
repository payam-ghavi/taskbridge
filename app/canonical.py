"""The 'canonical task' — the small common denominator both apps can represent.

Everything is normalised so a task in Microsoft To Do and its twin in Todoist
produce an *identical* dict here; that identity is what lets the engine tell a
real edit from an echo of its own write.
"""
import hashlib
import json
import re


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
        "important": bool(important),
        "completed": bool(completed),
    }


def canon_hash(c):
    return hashlib.sha1(
        json.dumps(c, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


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
