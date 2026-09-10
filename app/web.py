import logging
import time

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for

from . import __version__
from .config import DB_PATH
from .graph_client import GraphClient, device_code_poll, device_code_start
from .store import Store
from .todoist_client import validate_token

log = logging.getLogger("taskbridge.web")

app = Flask(__name__)
SYNC_LOOP = None  # set by __main__


def _store():
    return Store(DB_PATH)


@app.template_filter("timefmt")
def _timefmt(ts):
    return time.strftime("%H:%M", time.localtime(float(ts)))


def _ago(ts):
    if not ts:
        return "never"
    delta = time.time() - float(ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return f"{int(delta // 86400)} d ago"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    with _store() as s:
        if not s.is_configured():
            return redirect(url_for("setup"))
        return render_template("dashboard.html", **_dashboard_ctx(s))


@app.get("/setup")
def setup():
    with _store() as s:
        return render_template(
            "setup.html",
            version=__version__,
            todoist_name=s.get("cfg:todoist_name"),
            ms_email=s.get("cfg:ms_email"),
            settings=s.all_cfg(),
        )


def _dashboard_ctx(s):
    return dict(
        version=__version__,
        todoist_name=s.get("cfg:todoist_name") or "Todoist",
        ms_email=s.get("cfg:ms_email") or "Microsoft",
        running=s.get("status:running") or "idle",
        last_sync=_ago(s.get("status:last_sync_at")),
        last_result=s.get("status:last_result") or "—",
        last_error=s.get("status:last_error"),
        ms_auth_expired=s.get("status:ms_auth_expired") == "true",
        mapping_count=s.mapping_count(),
        pair_count=len(s.all_pairs()),
        settings=s.all_cfg(),
        activity=s.recent_activity(30),
    )


# ---------------------------------------------------------------------------
# Connect: Todoist
# ---------------------------------------------------------------------------

@app.post("/connect/todoist")
def connect_todoist():
    data = request.get_json(silent=True) or request.form
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify(ok=False, error="Paste your Todoist API token."), 400
    try:
        name = validate_token(token)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except requests.RequestException:
        return jsonify(ok=False, error="Couldn't reach Todoist. Try again."), 502
    with _store() as s:
        s.set("cfg:todoist_api_token", token)
        s.set("cfg:todoist_name", name)
        s.log("info", f"Connected Todoist ({name})")
    return jsonify(ok=True, name=name)


# ---------------------------------------------------------------------------
# Connect: Microsoft (device code)
# ---------------------------------------------------------------------------

@app.post("/connect/microsoft/start")
def ms_start():
    try:
        dc = device_code_start()
    except requests.RequestException:
        return jsonify(ok=False, error="Couldn't reach Microsoft. Try again."), 502
    return jsonify(
        ok=True,
        user_code=dc["user_code"],
        device_code=dc["device_code"],
        verification_uri=dc.get("verification_uri", "https://microsoft.com/devicelogin"),
        interval=dc.get("interval", 5),
        expires_in=dc.get("expires_in", 900),
    )


@app.post("/connect/microsoft/poll")
def ms_poll():
    device_code = (request.json or {}).get("device_code", "")
    if not device_code:
        return jsonify(status="error", error="missing device_code"), 400
    res = device_code_poll(device_code)
    if res["status"] != "success":
        return jsonify(res)
    refresh = res["refresh_token"]
    try:
        email = GraphClient(None, refresh).whoami()
    except Exception:
        email = "Microsoft account"
    with _store() as s:
        s.set("ms_refresh_token", refresh)
        s.set("cfg:ms_email", email)
        s.delete("status:ms_auth_expired")
        s.log("info", f"Connected Microsoft ({email})")
    return jsonify(status="success", email=email)


# ---------------------------------------------------------------------------
# Settings / actions
# ---------------------------------------------------------------------------

@app.post("/settings")
def save_settings():
    f = request.form
    with _store() as s:
        if f.get("sync_interval"):
            try:
                s.set_cfg("sync_interval", max(15, min(3600, int(f["sync_interval"]))))
            except ValueError:
                pass
        if f.get("conflict_winner") in ("todoist", "mstodo"):
            s.set_cfg("conflict_winner", f["conflict_winner"])
        # finish setup once both sides are connected
        if s.get("cfg:todoist_api_token") and s.get("ms_refresh_token"):
            s.set_cfg("configured", "true")
            s.log("info", "Setup complete — syncing enabled")
    if SYNC_LOOP:
        SYNC_LOOP.trigger()
    return redirect(url_for("index"))


@app.post("/sync-now")
def sync_now():
    if SYNC_LOOP:
        SYNC_LOOP.trigger()
    with _store() as s:
        s.log("info", "Manual sync requested")
    return jsonify(ok=True)


@app.post("/disconnect")
def disconnect():
    which = request.form.get("which", "all")
    with _store() as s:
        if which == "todoist":
            s.delete("cfg:todoist_api_token")
            s.delete("cfg:todoist_name")
        elif which == "microsoft":
            s.delete("ms_refresh_token")
            s.delete("cfg:ms_email")
        else:
            s.wipe_credentials()
            s.delete("cfg:todoist_name")
            s.delete("cfg:ms_email")
        s.delete("cfg:configured")
        s.log("info", f"Disconnected: {which}")
    return redirect(url_for("setup"))


@app.post("/reset-sync-state")
def reset_sync_state():
    """Forget all task pairings + sync tokens (keeps accounts connected).
    The next sync re-pairs from scratch by title match."""
    with _store() as s:
        for r in s.db.execute("SELECT key FROM kv WHERE key LIKE 'delta:%' OR key='todoist_sync_token'").fetchall():
            s.delete(r["key"])
        s.db.execute("DELETE FROM task_map")
        s.db.execute("DELETE FROM list_pairs")
        s.db.execute("DELETE FROM projects")
        s.commit()
        s.log("warn", "Sync state reset — will re-pair on next sync")
    if SYNC_LOOP:
        SYNC_LOOP.trigger()
    return redirect(url_for("index"))


@app.get("/api/status")
def api_status():
    with _store() as s:
        if not s.is_configured():
            return jsonify(configured=False)
        return jsonify(
            configured=True,
            running=s.get("status:running") or "idle",
            last_sync=_ago(s.get("status:last_sync_at")),
            last_result=s.get("status:last_result") or "—",
            last_error=s.get("status:last_error"),
            ms_auth_expired=s.get("status:ms_auth_expired") == "true",
            mapping_count=s.mapping_count(),
            activity=s.recent_activity(30),
        )
