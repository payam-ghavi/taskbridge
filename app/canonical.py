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
from datetime import datetime
from zoneinfo import ZoneInfo

# Fields a given provider cannot represent at all. The engine ignores these
# fields when comparing that provider's report to the group's stored canon,
# and never sends them when writing a task to that provider.
UNSUPPORTED = {
    "todoist": set(),
    "mstodo": set(),
    # Google's public Tasks API accepts a full RFC3339 due timestamp but
    # silently discards the time-of-day, always echoing midnight back on
    # read (a long-documented API limitation, unrelated to what Google's own
    # apps can do with private endpoints). Without this, that midnight
    # echo would look like a real edit and erase the time Todoist/Microsoft
    # actually have for the task.
    "google": {"important", "due_time"},
}


def _norm_text(s):
    if not s:
        return ""
    return re.sub(r"\r\n?", "\n", s).strip()


def _norm_due(s):
    """Return 'YYYY-MM-DD' or None."""
    if not s:
        return None
    return str(s)[:10]


def canonical(title, notes, due, due_time, important, completed):
    """``due_time`` is 'HH:MM' or None; only meaningful when ``due`` is set."""
    due = _norm_due(due)
    return {
        "title": _norm_text(title),
        "notes": _norm_text(notes),
        "due": due,
        "due_time": due_time if due and due_time else None,
        "important": None if important is None else bool(important),
        "completed": bool(completed),
    }


def canon_hash(c):
    return hashlib.sha1(
        json.dumps(c, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def relevant_diff(new_c, prev_c, provider):
    """True if new_c differs from prev_c in a field `provider` can represent.
    Uses prev_c.get() rather than prev_c[] so a canon dict stored before a new
    field existed (schema evolution) compares as unchanged on that field
    instead of raising."""
    unsupported = UNSUPPORTED.get(provider, set())
    return any(new_c[k] != prev_c.get(k) for k in new_c if k not in unsupported)


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

def item_to_canonical(item, account_tz=None):
    """Unlike Microsoft's reminderDateTime (always explicit UTC) and Google's
    due timestamp (always UTC when it carries a time at all), Todoist's due
    date/time is a *floating* local value -- "10:00" means 10am in whatever
    zone the task's own due.timezone says, or the account's own timezone
    (account_tz, passed in by the caller) when due.timezone is absent. It
    must be converted to UTC here so due_time always means the same thing
    everywhere else in the codebase; skipping that is what made a Todoist
    task due "10:00 local" get written to Microsoft as if it meant 10:00
    UTC, landing hours off after Microsoft converted it back to local."""
    due, due_time = None, None
    d = item.get("due")
    if d:
        raw = d.get("date") or d.get("datetime")
        if raw:
            due = raw[:10]
            if len(raw) > 10:
                zone = d.get("timezone") or account_tz
                if zone:
                    try:
                        local_dt = datetime.fromisoformat(raw[:19]).replace(tzinfo=ZoneInfo(zone))
                        utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
                        due = utc_dt.strftime("%Y-%m-%d")
                        due_time = utc_dt.strftime("%H:%M")
                    except Exception:
                        due_time = raw[11:16]
                else:
                    due_time = raw[11:16]
    priority = item.get("priority") or 1          # 1=none .. 4=urgent
    checked = item.get("checked")
    if checked is None:
        checked = item.get("completed_at") is not None
    return canonical(
        item.get("content"),
        item.get("description"),
        due,
        due_time,
        int(priority) >= 3,                       # P1/P2 -> important
        bool(checked),
    )


# ---- Microsoft To Do -----------------------------------------------------

def task_to_canonical(t):
    """Microsoft To Do's app exposes two separate date+time controls: "Add due
    date" (dueDateTime — no time picker in the UI, so it never carries a real
    time) and "Remind me" (reminderDateTime — has an actual time picker). When
    both are set on the same task, the reminder is the one the user actually
    picked a time for, so it wins over the plain due date."""
    due, due_time = None, None
    if t.get("isReminderOn"):
        rd = t.get("reminderDateTime")
        if rd and rd.get("dateTime"):
            due, due_time = rd["dateTime"][:10], rd["dateTime"][11:16]
    if due is None:
        dd = t.get("dueDateTime")
        if dd and dd.get("dateTime"):
            due = dd["dateTime"][:10]
    notes = (t.get("body") or {}).get("content") or ""
    return canonical(
        t.get("title"),
        notes,
        due,
        due_time,
        (t.get("importance") == "high"),
        (t.get("status") == "completed"),
    )


# ---- Google Tasks --------------------------------------------------------

def gtask_to_canonical(t):
    """Google Tasks has no priority/importance field at all — always None.
    Its ``due`` timestamp is midnight for a plain (no-time) due date, so a
    time component of exactly 00:00 is treated as "no time set" rather than
    a genuine midnight reminder."""
    due, due_time = None, None
    d = t.get("due")
    if d:
        due = d[:10]
        if len(d) > 10 and d[11:16] != "00:00":
            due_time = d[11:16]
    return canonical(
        t.get("title"),
        t.get("notes"),
        due,
        due_time,
        None,
        (t.get("status") == "completed"),
    )
