"""Regression: the Todoist sync cursor must round-trip through the store.

sync_once() persists the post-cycle sync_token via store.set_cursor("todoist",
"account", ...) (see engine.py). _build_client() must read it back from the
same place on the NEXT cycle -- if it doesn't, every cycle starts a fresh
full resync ("*"), which makes Todoist's changed-item set contain every
linked item forever. With conflict_winner="todoist" (the default) that
silently discards every real edit coming from Microsoft/Google, since the
engine's same-cycle conflict check always sees Todoist as "also touched"."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from app.loop import SyncLoop, _build_client
from app.store import Store

FAILURES = []


def check(label, cond, detail=""):
    status = "OK  " if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def run():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    store = Store(path)

    store.set_connection("todoist", "Test Todoist", {"token": "x"})
    store.set_cursor("todoist", "account", "some-real-sync-token")
    store.commit()

    conn = store.get_connection("todoist")
    client = _build_client("todoist", conn["creds"], store)
    check("todoist client is seeded with the stored cursor, not '*'",
          client.sync_token == "some-real-sync-token", f"got {client.sync_token!r}")

    store.close()
    os.remove(path)


def run_crash_reason():
    """Regression (2.0.19): a crashed cycle used to log only "Sync failed —
    see logs", and the container log isn't reachable on Umbrel, so the real
    reason was lost once the next good cycle cleared status:last_error. The
    Activity line itself must now say what failed and, when known, where."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    Store(path).close()

    loop = SyncLoop(path)
    err = requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")
    err.request = requests.Request("GET", "https://api.todoist.com/sync/v9/sync").prepare()

    def boom():
        raise err
    loop._cycle = boom
    interval = loop._run_once()

    store = Store(path)
    activity = store.recent_activity(5)
    last = activity[0] if activity else {}
    msg = last.get("message", "") if isinstance(last, dict) else str(last)
    check("crash keeps the loop alive with the default interval", interval == 60, f"got {interval}")
    check("activity names the app that failed", "Todoist" in msg, msg)
    check("activity includes the actual error", "ReadTimeout" in msg and "timed out" in msg, msg)
    check("activity no longer just says 'see logs'", "see logs" not in msg, msg)
    check("status:last_error carries the same reason", "ReadTimeout" in (store.get("status:last_error") or ""))

    def boom2():
        raise KeyError("due")
    loop._cycle = boom2
    loop._run_once()
    activity = store.recent_activity(5)
    last = activity[0] if activity else {}
    msg = last.get("message", "") if isinstance(last, dict) else str(last)
    check("non-network crash still gets its type and message", "KeyError" in msg and "due" in msg, msg)

    store.close()
    os.remove(path)


if __name__ == "__main__":
    print("=== loop._build_client cursor round-trip ===")
    run()
    print("\n=== crashed cycle reports its reason in Activity ===")
    run_crash_reason()
    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)
