"""Background thread: run one sync cycle, sleep, repeat."""
import logging
import threading
import time
import traceback

from .engine import Config, sync_once
from .google_client import GoogleTasksClient
from .graph_client import GraphClient
from .store import PROVIDER_LABEL, Store
from .todoist_client import TodoistClient

log = logging.getLogger("taskbridge.loop")


def _build_client(provider, creds, store=None, dry_run=False):
    if provider == "todoist":
        sync_token = store.get_cursor("todoist", "account") if store else None
        return TodoistClient(creds.get("token"), sync_token, dry_run=dry_run)
    if provider == "mstodo":
        return GraphClient(creds.get("client_id"), creds.get("refresh_token"), dry_run=dry_run)
    if provider == "google":
        return GoogleTasksClient(creds.get("client_id"), creds.get("client_secret"),
                                  creds.get("refresh_token"), dry_run=dry_run)
    raise ValueError(provider)


class SyncLoop(threading.Thread):
    def __init__(self, db_path):
        super().__init__(daemon=True)
        self.db_path = db_path
        self._wake = threading.Event()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def trigger(self):
        """Ask the loop to run a cycle right now."""
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            interval = 60
            try:
                interval = self._cycle()
            except Exception:
                log.exception("sync cycle crashed")
                try:
                    with Store(self.db_path) as s:
                        s.set("status:last_error", traceback.format_exc().strip().splitlines()[-1])
                        s.set("status:last_sync_at", str(time.time()))
                        s.log("error", "Sync failed — see logs")
                except Exception:
                    pass
            self._wake.wait(timeout=max(15, interval))
            self._wake.clear()

    def _cycle(self):
        store = Store(self.db_path)
        try:
            interval = int(store.cfg("sync_interval", "60") or "60")
            if not store.is_configured():
                store.set("status:running", "not-configured")
                return interval

            cfg = Config(
                conflict_winner=(store.cfg("conflict_winner") or "todoist"),
                match_existing=(store.cfg("match_existing", "true") != "false"),
            )

            store.set("status:running", "syncing")
            clients = {}
            for provider, conn in store.all_connections().items():
                try:
                    clients[provider] = _build_client(provider, conn["creds"], store)
                except RuntimeError as e:
                    # token refresh rejected -> that provider needs reconnecting;
                    # keep syncing whatever other providers are still healthy.
                    store.set(f"status:{provider}_auth_expired", "true")
                    store.set("status:last_error", f"{PROVIDER_LABEL.get(provider, provider)}: {str(e).splitlines()[0]}")
                    store.log("error", f"{PROVIDER_LABEL.get(provider, provider)} sign-in expired — reconnect in Settings")
                else:
                    store.delete(f"status:{provider}_auth_expired")

            if len(clients) < 2:
                store.set("status:last_sync_at", str(time.time()))
                store.set("status:running", "error")
                return interval

            result = sync_once(store, clients, cfg)

            store.delete("status:last_error")
            store.set("status:last_sync_at", str(time.time()))
            summary = ", ".join(f"{n} {PROVIDER_LABEL.get(p, p)}" for p, n in result.items())
            store.set("status:last_result", summary or "no changes")
            store.set("status:running", "idle")
            if sum(result.values()):
                store.log("info", f"Synced: {summary}")
            return interval
        finally:
            store.close()
