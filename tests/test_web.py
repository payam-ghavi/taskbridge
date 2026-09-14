"""Smoke-test every page renders (no Jinja errors) across connection states,
using Flask's test client (no real server, exceptions propagate directly).

Run:  python tests/test_web.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

data_dir = tempfile.mkdtemp()
os.environ["TASKBRIDGE_DATA"] = data_dir
os.environ["PORT"] = "0"

from app.config import DB_PATH  # noqa: E402  (resolves against TASKBRIDGE_DATA set above)
from app import web  # noqa: E402
from app.store import Store  # noqa: E402

db_path = str(DB_PATH)

web.app.config["TESTING"] = True
client = web.app.test_client()

FAILURES = []


def check(label, resp, expect=200):
    ok = resp.status_code == expect
    print(f"[{'OK  ' if ok else 'FAIL'}] {label} -> {resp.status_code}")
    if not ok:
        FAILURES.append(label)
        print(resp.get_data(as_text=True)[:2000])


# state 0: nothing connected
check("GET / (no connections, redirects to setup)", client.get("/"), 302)
check("GET /setup (0 connections)", client.get("/setup"))

with Store(db_path) as s:
    s.set_connection("todoist", "Alex", {"token": "x"})
check("GET /setup (todoist only)", client.get("/setup"))

with Store(db_path) as s:
    s.set_connection("mstodo", "alex@outlook.com", {"refresh_token": "x"})
    s.set_cfg("configured", "true")
check("GET /setup (todoist + mstodo)", client.get("/setup"))
check("GET / (2 connections, configured -> dashboard)", client.get("/"))
check("GET /api/status (configured)", client.get("/api/status"))

with Store(db_path) as s:
    s.set("cfg:google_client_id", "fake.apps.googleusercontent.com")
check("GET /setup (google creds saved, not yet connected)", client.get("/setup"))

with Store(db_path) as s:
    s.set_connection("google", "alex@gmail.com", {"client_id": "x", "client_secret": "y", "refresh_token": "z"})
check("GET /setup (all three connected)", client.get("/setup"))
check("GET / (3 connections -> dashboard)", client.get("/"))
check("GET /api/status (3 connections)", client.get("/api/status"))

with Store(db_path) as s:
    s.set("status:google_auth_expired", "true")
check("GET / (one account expired -> banner)", client.get("/"))

check("GET /setup?google_error=Sign-in+failed", client.get("/setup?google_error=Sign-in%20failed"))

import shutil
shutil.rmtree(data_dir, ignore_errors=True)

print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
