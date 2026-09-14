"""Thin wrapper over the Google Tasks API.

Unlike Microsoft, Google has no public first-party client third parties can
piggyback on for this scope (their limited-input-device/TV flow explicitly
excludes the Tasks scope), so every self-hosted TaskBridge instance brings its
own Google Cloud OAuth client — the same "Client ID + Client Secret" pattern
other self-hosted apps (Nextcloud, Immich, ...) use for Google integrations.
See docs/google-setup.md for the exact Cloud Console steps.

Google Tasks has no delta/changed-since feed and (reliably) no "removed" marker
in list results, so ``list_tasks`` always fetches the *full* list; the engine
diffs that against what it already has linked to notice deletions — the same
technique the old code already used for Microsoft's non-delta fallback.

Google Tasks also has no priority/importance field — ``gtask_to_canonical``
always reports ``important=None`` and ``canonical_to_google_body`` never sends
it; see ``canonical.py``'s ``UNSUPPORTED`` map for how the engine treats that.
"""
import logging
import time

import requests

log = logging.getLogger("taskbridge.google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
SCOPE = "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/userinfo.email"


class NotFound(Exception):
    pass


def authorization_url(client_id, redirect_uri, state):
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",          # force a refresh_token even on repeat consent
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(client_id, client_secret, code, redirect_uri):
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    if r.status_code >= 400:
        raise ValueError(f"Google rejected the authorization code ({r.status_code}): {r.text[:200]}")
    return r.json()   # {access_token, refresh_token, expires_in, ...}


class GoogleTasksClient:
    def __init__(self, client_id, client_secret, refresh_token, dry_run=False):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.dry_run = dry_run
        self.access_token = None
        self._dry_seq = 0
        self._refresh()

    def _refresh(self):
        r = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Google token refresh failed ({r.status_code}): {r.text[:200]}")
        j = r.json()
        self.access_token = j["access_token"]
        # Google doesn't normally rotate the refresh token on refresh; keep the
        # one we were given unless it explicitly sends a new one.
        if j.get("refresh_token"):
            self.refresh_token = j["refresh_token"]

    def _req(self, method, url, **kw):
        if not url.startswith("http"):
            url = TASKS_BASE + url
        headers = {"Authorization": f"Bearer {self.access_token}"}
        for attempt in range(6):
            r = requests.request(method, url, headers=headers, timeout=60, **kw)
            if r.status_code == 401 and attempt == 0:
                self._refresh()
                headers["Authorization"] = f"Bearer {self.access_token}"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After", "5"))
                log.warning("Google %s, sleeping %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                raise NotFound(url)
            r.raise_for_status()
            return r
        r.raise_for_status()

    def whoami(self):
        r = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {self.access_token}"}, timeout=30)
        r.raise_for_status()
        j = r.json()
        return j.get("email") or "Google account"

    # ---- lists -------------------------------------------------------------
    def default_list_id(self):
        """Google doesn't flag a tasklist as default in list results, but the
        literal id "@default" is a documented alias for it."""
        return self._req("GET", "/users/@me/lists/@default").json()["id"]

    def get_lists(self):
        out, params = [], {"maxResults": 100}
        while True:
            j = self._req("GET", "/users/@me/lists", params=params).json()
            out.extend(j.get("items", []))
            token = j.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    def create_list(self, name):
        if self.dry_run:
            log.info("[dry-run] create_list %r", name)
            return None
        return self._req("POST", "/users/@me/lists", json={"title": name}).json()

    def delete_list(self, tasklist_id):
        if self.dry_run:
            return
        try:
            self._req("DELETE", f"/users/@me/lists/{tasklist_id}")
        except NotFound:
            pass

    # ---- tasks ---------------------------------------------------------
    def list_tasks(self, tasklist_id):
        out, params = [], {
            "maxResults": 100, "showCompleted": "true", "showHidden": "true",
        }
        while True:
            j = self._req("GET", f"/lists/{tasklist_id}/tasks", params=params).json()
            out.extend(j.get("items", []))
            token = j.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    def create_task(self, tasklist_id, body):
        if self.dry_run:
            self._dry_seq += 1
            return {"id": f"dry-gtask-{self._dry_seq}"}
        return self._req("POST", f"/lists/{tasklist_id}/tasks", json=body).json()

    def update_task(self, tasklist_id, task_id, body):
        if not body or self.dry_run:
            return
        self._req("PATCH", f"/lists/{tasklist_id}/tasks/{task_id}", json=body)

    def delete_task(self, tasklist_id, task_id):
        if self.dry_run:
            return
        try:
            self._req("DELETE", f"/lists/{tasklist_id}/tasks/{task_id}")
        except NotFound:
            pass


# ---- body builder -----------------------------------------------------------

def canonical_to_google_body(new_c, prev_c=None):
    """Never includes priority — Google Tasks has no such field."""
    body = {}
    full = prev_c is None
    if full or new_c["title"] != prev_c["title"]:
        body["title"] = new_c["title"]
    if full or new_c["notes"] != prev_c["notes"]:
        body["notes"] = new_c["notes"]
    if full or new_c["due"] != prev_c["due"] or new_c.get("due_time") != prev_c.get("due_time"):
        if new_c["due"]:
            time_part = new_c["due_time"] if new_c.get("due_time") else "00:00"
            body["due"] = f"{new_c['due']}T{time_part}:00.000Z"
        else:
            body["due"] = None
    if full or new_c["completed"] != prev_c["completed"]:
        if new_c["completed"]:
            body["status"] = "completed"
        else:
            body["status"] = "needsAction"
            body["completed"] = None
    return body
