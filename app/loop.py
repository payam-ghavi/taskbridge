"""Background thread: run one sync cycle, sleep, repeat."""
import logging
import threading
import time
import traceback

from .engine import Config, sync_once
from .graph_client import GraphClient
from .store import Store
from .todoist_client import TodoistClient

log = logging.getLogger("taskbridge.loop")


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

            td_token = store.get("cfg:todoist_api_token")
            ms_refresh = store.get("ms_refresh_token")
            ms_client_id = store.get("cfg:ms_client_id") or None
            cfg = Config(
                conflict_winner=(store.cfg("conflict_winner") or "todoist"),
                match_existing=(store.cfg("match_existing", "true") != "false"),
            )

            store.set("status:running", "syncing")
            try:
                td = TodoistClient(td_token, store.get("todoist_sync_token"))
                ms = GraphClient(ms_client_id, ms_refresh)
            except RuntimeError as e:
                # token refresh rejected -> Microsoft needs reconnecting
                store.set("status:ms_auth_expired", "true")
                store.set("status:last_error", str(e).splitlines()[0])
                store.set("status:last_sync_at", str(time.time()))
                store.set("status:running", "error")
                store.log("error", "Microsoft sign-in expired — reconnect in Settings")
                return interval

            result = sync_once(store, td, ms, cfg)

            store.delete("status:ms_auth_expired")
            store.delete("status:last_error")
            store.set("status:last_sync_at", str(time.time()))
            store.set("status:last_result",
                      f"{result['todoist_changes']} from Todoist, {result['mstodo_changes']} from To Do")
            store.set("status:running", "idle")
            changed = result["todoist_changes"] + result["mstodo_changes"]
            if changed:
                store.log("info", f"Synced: {result['todoist_changes']} Todoist / "
                                  f"{result['mstodo_changes']} To Do changes")
            return interval
        finally:
            store.close()
