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

from app.loop import _build_client
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


if __name__ == "__main__":
    print("=== loop._build_client cursor round-trip ===")
    run()
    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)
