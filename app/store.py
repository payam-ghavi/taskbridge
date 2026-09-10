"""All state in one SQLite file under the data volume:

  * kv          - sync tokens, delta links, the rotating MS refresh token,
                  and (cfg:*) user settings
  * projects    - mirror of Todoist projects
  * list_pairs  - Todoist project <-> To Do list
  * task_map    - Todoist id <-> To Do id + last canonical written
  * activity    - a small ring buffer for the dashboard

WAL mode lets the web thread read while the sync loop writes.
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT, is_inbox INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS list_pairs (
    todoist_project_id TEXT PRIMARY KEY, mstodo_list_id TEXT UNIQUE, name TEXT
);
CREATE TABLE IF NOT EXISTS task_map (
    todoist_id TEXT PRIMARY KEY, mstodo_id TEXT UNIQUE,
    todoist_project_id TEXT, mstodo_list_id TEXT, canon TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS activity (ts REAL, level TEXT, message TEXT);
"""

_CFG_DEFAULTS = {
    "conflict_winner": "todoist",
    "sync_interval": "60",
    "match_existing": "true",
}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- kv --------------------------------------------------------------
    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key, value):
        self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def delete(self, key):
        self.db.execute("DELETE FROM kv WHERE key=?", (key,))

    # ---- settings (cfg:*) ----------------------------------------------
    def cfg(self, key, default=None):
        return self.get(f"cfg:{key}", _CFG_DEFAULTS.get(key, default))

    def set_cfg(self, key, value):
        self.set(f"cfg:{key}", str(value))

    def all_cfg(self):
        out = dict(_CFG_DEFAULTS)
        for r in self.db.execute("SELECT key,value FROM kv WHERE key LIKE 'cfg:%'"):
            out[r["key"][4:]] = r["value"]
        return out

    def is_configured(self):
        return bool(self.get("cfg:todoist_api_token") and self.get("ms_refresh_token")
                    and self.get("cfg:configured") == "true")

    # ---- activity log --------------------------------------------------
    def log(self, level, message):
        self.db.execute("INSERT INTO activity(ts,level,message) VALUES(?,?,?)",
                        (time.time(), level, message))
        self.db.execute(
            "DELETE FROM activity WHERE ts < (SELECT MIN(ts) FROM "
            "(SELECT ts FROM activity ORDER BY ts DESC LIMIT 200))"
        )
        self.db.commit()

    def recent_activity(self, n=40):
        rows = self.db.execute(
            "SELECT ts,level,message FROM activity ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- projects -----------------------------------------------------
    def upsert_project(self, pid, name, is_inbox=False):
        self.db.execute(
            "INSERT INTO projects(id,name,is_inbox) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, is_inbox=excluded.is_inbox",
            (pid, name, 1 if is_inbox else 0),
        )

    def delete_project(self, pid):
        self.db.execute("DELETE FROM projects WHERE id=?", (pid,))

    def all_projects(self):
        return self.db.execute("SELECT * FROM projects").fetchall()

    # ---- list pairs -------------------------------------------------
    def add_pair(self, todoist_project_id, mstodo_list_id, name):
        self.db.execute(
            "INSERT OR REPLACE INTO list_pairs(todoist_project_id,mstodo_list_id,name) VALUES(?,?,?)",
            (todoist_project_id, mstodo_list_id, name),
        )

    def all_pairs(self):
        return self.db.execute("SELECT * FROM list_pairs").fetchall()

    def pair_for_project(self, pid):
        return self.db.execute("SELECT * FROM list_pairs WHERE todoist_project_id=?", (pid,)).fetchone()

    def pair_for_list(self, lid):
        return self.db.execute("SELECT * FROM list_pairs WHERE mstodo_list_id=?", (lid,)).fetchone()

    # ---- task map -------------------------------------------------
    def add_mapping(self, todoist_id, mstodo_id, todoist_project_id, mstodo_list_id, canon):
        self.db.execute(
            "INSERT OR REPLACE INTO task_map"
            "(todoist_id,mstodo_id,todoist_project_id,mstodo_list_id,canon,updated_at) VALUES(?,?,?,?,?,?)",
            (todoist_id, mstodo_id, todoist_project_id, mstodo_list_id, json.dumps(canon), time.time()),
        )

    def update_canon(self, todoist_id, canon):
        self.db.execute("UPDATE task_map SET canon=?, updated_at=? WHERE todoist_id=?",
                        (json.dumps(canon), time.time(), todoist_id))

    def delete_mapping(self, todoist_id=None, mstodo_id=None):
        if todoist_id:
            self.db.execute("DELETE FROM task_map WHERE todoist_id=?", (todoist_id,))
        if mstodo_id:
            self.db.execute("DELETE FROM task_map WHERE mstodo_id=?", (mstodo_id,))

    def by_todoist(self):
        return {r["todoist_id"]: r for r in self.db.execute("SELECT * FROM task_map")}

    def by_mstodo(self):
        return {r["mstodo_id"]: r for r in self.db.execute("SELECT * FROM task_map")}

    def mapping_count(self):
        return self.db.execute("SELECT COUNT(*) FROM task_map").fetchone()[0]

    # ---- reset -------------------------------------------------------
    def wipe_credentials(self):
        self.db.execute("DELETE FROM kv WHERE key IN ('ms_refresh_token','cfg:todoist_api_token','cfg:configured')")
        self.db.commit()

    def wipe_all(self):
        for t in ("kv", "projects", "list_pairs", "task_map", "activity"):
            self.db.execute(f"DELETE FROM {t}")
        self.db.commit()

    # ---- lifecycle --------------------------------------------------
    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
