"""Thin wrapper over Microsoft Graph for the To Do endpoints.

Microsoft does NOT send webhooks for To Do tasks, so the change feed here is the
per-list *delta query*: ``/me/todo/lists/{id}/tasks/delta`` returns a
``@odata.deltaLink`` that, replayed next run, yields only changed/removed tasks.

Auth is the OAuth refresh-token grant against a public client (no secret). The
refresh token rotates on use; callers must persist ``self.refresh_token``.
"""
import logging
import time

import requests

log = logging.getLogger("taskbridge.graph")

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
TOKEN_URL = f"{AUTHORITY}/token"
DEVICECODE_URL = f"{AUTHORITY}/devicecode"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "Tasks.ReadWrite offline_access User.Read"

# "Microsoft Graph Command Line Tools" — Microsoft's own first-party public
# client (the one Connect-MgGraph uses). Public, multi-tenant, supports personal
# Microsoft accounts, interactive consent. No Azure app registration needed.
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"


class NotFound(Exception):
    pass


class GraphClient:
    def __init__(self, client_id, refresh_token, dry_run=False):
        self.client_id = client_id or DEFAULT_CLIENT_ID
        self.refresh_token = refresh_token
        self.dry_run = dry_run
        self.access_token = None
        self._task_cache = {}
        self._dry_seq = 0
        self._refresh()

    def _refresh(self):
        r = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": SCOPE,
        }, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Microsoft token refresh failed ({r.status_code}): {r.text[:200]}"
            )
        j = r.json()
        self.access_token = j["access_token"]
        if j.get("refresh_token"):
            self.refresh_token = j["refresh_token"]

    def _req(self, method, url, **kw):
        if not url.startswith("http"):
            url = GRAPH + url
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Prefer": 'outlook.body-content-type="text"',
        }
        for attempt in range(6):
            r = requests.request(method, url, headers=headers, timeout=60, **kw)
            if r.status_code == 401 and attempt == 0:
                self._refresh()
                headers["Authorization"] = f"Bearer {self.access_token}"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After", "5"))
                log.warning("Graph %s, sleeping %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                raise NotFound(url)
            r.raise_for_status()
            return r
        r.raise_for_status()

    def _paged(self, url):
        out = []
        while url:
            j = self._req("GET", url).json()
            out.extend(j.get("value", []))
            url = j.get("@odata.nextLink")
        return out

    def whoami(self):
        j = self._req("GET", "/me").json()
        return j.get("userPrincipalName") or j.get("mail") or j.get("displayName") or "Microsoft account"

    # ---- lists -----------------------------------------------------------
    def get_lists(self):
        return self._paged("/me/todo/lists")

    def create_list(self, name):
        if self.dry_run:
            log.info("[dry-run] create_list %r", name)
            return None
        return self._req("POST", "/me/todo/lists", json={"displayName": name}).json()

    def delete_list(self, list_id):
        if self.dry_run:
            return
        try:
            self._req("DELETE", f"/me/todo/lists/{list_id}")
        except NotFound:
            pass

    # ---- tasks ---------------------------------------------------------
    def list_tasks(self, list_id):
        if list_id not in self._task_cache:
            self._task_cache[list_id] = self._paged(f"/me/todo/lists/{list_id}/tasks")
        return self._task_cache[list_id]

    def delta(self, list_id, delta_link=None):
        url = delta_link or f"{GRAPH}/me/todo/lists/{list_id}/tasks/delta"
        tasks = []
        while True:
            j = self._req("GET", url).json()
            tasks.extend(j.get("value", []))
            if j.get("@odata.nextLink"):
                url = j["@odata.nextLink"]
            else:
                return tasks, j.get("@odata.deltaLink")

    def create_task(self, list_id, body):
        if self.dry_run:
            self._dry_seq += 1
            return {"id": f"dry-task-{self._dry_seq}"}
        return self._req("POST", f"/me/todo/lists/{list_id}/tasks", json=body).json()

    def update_task(self, list_id, task_id, body):
        if not body or self.dry_run:
            return
        self._req("PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}", json=body)

    def delete_task(self, list_id, task_id):
        if self.dry_run:
            return
        try:
            self._req("DELETE", f"/me/todo/lists/{list_id}/tasks/{task_id}")
        except NotFound:
            pass


# ---- device-code flow (used by the setup UI) -------------------------------

def device_code_start(client_id=None):
    r = requests.post(DEVICECODE_URL, data={
        "client_id": client_id or DEFAULT_CLIENT_ID, "scope": SCOPE,
    }, timeout=30)
    r.raise_for_status()
    return r.json()


def device_code_poll(device_code, client_id=None):
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id or DEFAULT_CLIENT_ID,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    }, timeout=30)
    j = r.json()
    if "refresh_token" in j:
        return {"status": "success", "refresh_token": j["refresh_token"]}
    err = j.get("error")
    if err in ("authorization_pending", "slow_down"):
        return {"status": "pending"}
    return {"status": "error", "error": j.get("error_description") or err or "unknown error"}


# ---- body builder -----------------------------------------------------------

def canonical_to_ms_patch(new_c, prev_c=None):
    """A due_time (set via another app's real due-time or reminder) is mirrored
    onto MS's own "Remind me" field too -- dueDateTime alone has no time
    picker in the To Do app, so a time-bearing due date would otherwise be
    invisible there."""
    body = {}
    full = prev_c is None
    if full or new_c["title"] != prev_c["title"]:
        body["title"] = new_c["title"]
    if full or new_c["notes"] != prev_c["notes"]:
        body["body"] = {"content": new_c["notes"], "contentType": "text"}
    if full or new_c["due"] != prev_c["due"] or new_c.get("due_time") != prev_c.get("due_time"):
        if new_c["due"]:
            body["dueDateTime"] = {"dateTime": new_c["due"] + "T00:00:00", "timeZone": "UTC"}
            if new_c.get("due_time"):
                body["reminderDateTime"] = {
                    "dateTime": f"{new_c['due']}T{new_c['due_time']}:00", "timeZone": "UTC",
                }
                body["isReminderOn"] = True
            else:
                body["isReminderOn"] = False
        else:
            body["dueDateTime"] = None
            body["isReminderOn"] = False
    if full or new_c["important"] != prev_c["important"]:
        body["importance"] = "high" if new_c["important"] else "normal"
    if full or new_c["completed"] != prev_c["completed"]:
        body["status"] = "completed" if new_c["completed"] else "notStarted"
    return body
