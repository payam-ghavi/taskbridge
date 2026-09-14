import logging
import secrets
import time

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for

from . import __version__
from .config import DB_PATH
from .google_client import GoogleTasksClient, authorization_url, exchange_code
from .graph_client import GraphClient, device_code_poll, device_code_start
from .store import PROVIDER_ICON, PROVIDER_LABEL, Store
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


def _google_redirect_uri():
    return request.host_url.rstrip("/") + url_for("google_callback")


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
        connections = s.all_connections()
        return render_template(
            "setup.html",
            version=__version__,
            connections=connections,
            labels=PROVIDER_LABEL,
            icons=PROVIDER_ICON,
            google_error=request.args.get("google_error"),
            settings=s.all_cfg(),
            google_client_id=s.get("cfg:google_client_id"),
            google_client_secret_set=bool(s.get("cfg:google_client_secret")),
            google_redirect_uri=_google_redirect_uri(),
        )


def _dashboard_ctx(s):
    connections = s.all_connections()
    accounts = [
        {"provider": p, "label": PROVIDER_LABEL.get(p, p), "icon": PROVIDER_ICON.get(p, "⚪"),
         "name": c["display_name"] or PROVIDER_LABEL.get(p, p),
         "auth_expired": s.get(f"status:{p}_auth_expired") == "true"}
        for p, c in connections.items()
    ]
    return dict(
        version=__version__,
        accounts=accounts,
        any_auth_expired=any(a["auth_expired"] for a in accounts),
        running=s.get("status:running") or "idle",
        last_sync=_ago(s.get("status:last_sync_at")),
        last_result=s.get("status:last_result") or "—",
        last_error=s.get("status:last_error"),
        mapping_count=s.mapping_count(),
        pair_count=len(s.all_list_groups()),
        settings=s.all_cfg(),
        labels=PROVIDER_LABEL,
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
        s.set_connection("todoist", name, {"token": token})
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
        s.set_connection("mstodo", email, {"refresh_token": refresh})
        s.delete("status:mstodo_auth_expired")
        s.log("info", f"Connected Microsoft ({email})")
    return jsonify(status="success", email=email)


# ---------------------------------------------------------------------------
# Connect: Google Tasks (bring-your-own OAuth client + authorization code)
# ---------------------------------------------------------------------------

@app.post("/connect/google/credentials")
def google_credentials():
    data = request.get_json(silent=True) or request.form
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return jsonify(ok=False, error="Paste both the Client ID and Client Secret."), 400
    with _store() as s:
        s.set("cfg:google_client_id", client_id)
        s.set("cfg:google_client_secret", client_secret)
    return jsonify(ok=True)


@app.get("/connect/google/start")
def google_start():
    with _store() as s:
        client_id = s.get("cfg:google_client_id")
        client_secret = s.get("cfg:google_client_secret")
        if not client_id or not client_secret:
            return redirect(url_for("setup"))
        state = secrets.token_urlsafe(24)
        s.set("cfg:google_oauth_state", state)
    url = authorization_url(client_id, _google_redirect_uri(), state)
    return redirect(url)


@app.get("/oauth/google/callback")
def google_callback():
    error = request.args.get("error")
    code = request.args.get("code")
    state = request.args.get("state")
    with _store() as s:
        expected_state = s.get("cfg:google_oauth_state")
        s.delete("cfg:google_oauth_state")
        client_id = s.get("cfg:google_client_id")
        client_secret = s.get("cfg:google_client_secret")

        if error:
            s.log("error", f"Google sign-in cancelled: {error}")
            return redirect(url_for("setup", google_error="Sign-in was cancelled."))
        if not code or not state or state != expected_state:
            s.log("error", "Google sign-in failed: invalid state")
            return redirect(url_for("setup", google_error="Sign-in failed — try again."))
        try:
            tokens = exchange_code(client_id, client_secret, code, _google_redirect_uri())
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                # happens if the user had already consented and Google skipped
                # issuing a fresh refresh token; access_type=offline+prompt=consent
                # normally prevents this, but guard anyway.
                raise ValueError("Google didn't return a refresh token — try disconnecting "
                                  "TaskBridge's access in your Google Account and reconnecting.")
            email = GoogleTasksClient(client_id, client_secret, refresh_token).whoami()
        except Exception as e:
            s.log("error", f"Google sign-in failed: {e}")
            return redirect(url_for("setup", google_error=str(e)))
        s.set_connection("google", email, {
            "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
        })
        s.delete("status:google_auth_expired")
        s.log("info", f"Connected Google ({email})")
    return redirect(url_for("setup"))


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
        connected = set(s.all_connections().keys())
        if f.get("conflict_winner") in connected:
            s.set_cfg("conflict_winner", f["conflict_winner"])
        if len(connected) >= 2:
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
        if which == "all":
            s.wipe_credentials()
            for p in ("todoist", "mstodo", "google"):
                s.delete(f"status:{p}_auth_expired")
        else:
            s.remove_connection(which)
            s.delete(f"status:{which}_auth_expired")
        if len(s.all_connections()) < 2:
            s.delete("cfg:configured")
        s.log("info", f"Disconnected: {which}")
    return redirect(url_for("setup"))


@app.post("/reset-sync-state")
def reset_sync_state():
    with _store() as s:
        s.reset_sync_state()
        s.log("warn", "Sync state reset — will re-pair on next sync")
    if SYNC_LOOP:
        SYNC_LOOP.trigger()
    return redirect(url_for("index"))


@app.get("/api/status")
def api_status():
    with _store() as s:
        if not s.is_configured():
            return jsonify(configured=False)
        ctx = _dashboard_ctx(s)
        return jsonify(
            configured=True,
            running=ctx["running"],
            last_sync=ctx["last_sync"],
            last_result=ctx["last_result"],
            last_error=ctx["last_error"],
            any_auth_expired=ctx["any_auth_expired"],
            accounts=ctx["accounts"],
            mapping_count=ctx["mapping_count"],
            activity=ctx["activity"],
        )
