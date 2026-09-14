"""All state in one SQLite file under the data volume.

Schema v2 — a provider-agnostic hub instead of a fixed Todoist<->To Do pair:

  * kv                 - settings (cfg:*), status:*, and schema_version
  * connections         - one row per connected account: provider -> display
                          name + credentials (JSON blob, shape is provider-specific)
  * cursors             - incremental-sync position per (provider, scope);
                          scope is "account" for Todoist's sync_token, or a
                          list id for Graph's deltaLink
  * list_groups          - "these lists, one per provider, are the same list"
  * list_group_members    - (group_id, provider) -> that provider's list id
  * task_groups           - one logical task; canon = last-known-good canonical
  * task_links            - (group_id, provider) -> that provider's item id
  * activity             - a small ring buffer for the dashboard

v1 installs (pre-3-provider) are migrated automatically on first open — see
``_migrate_v1_to_v2``. The old tables are left in place, unused, as a cheap
rollback safety net.

WAL mode lets the web thread read while the sync loop writes.
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS connections (
    provider TEXT PRIMARY KEY,
    display_name TEXT,
    creds TEXT
);

CREATE TABLE IF NOT EXISTS cursors (
    provider TEXT, scope TEXT, cursor TEXT,
    PRIMARY KEY (provider, scope)
);

CREATE TABLE IF NOT EXISTS list_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT
);
CREATE TABLE IF NOT EXISTS list_group_members (
    group_id INTEGER, provider TEXT, list_id TEXT,
    PRIMARY KEY (group_id, provider)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lgm_provider_list
    ON list_group_members(provider, list_id);

CREATE TABLE IF NOT EXISTS task_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, canon TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS task_links (
    group_id INTEGER, provider TEXT, item_id TEXT, list_id TEXT,
    PRIMARY KEY (group_id, provider)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tl_provider_item
    ON task_links(provider, item_id);

CREATE TABLE IF NOT EXISTS activity (ts REAL, level TEXT, message TEXT);

-- v1 tables, kept only so a v1 rollback has something to read.
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT, is_inbox INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS list_pairs (
    todoist_project_id TEXT PRIMARY KEY, mstodo_list_id TEXT UNIQUE, name TEXT
);
CREATE TABLE IF NOT EXISTS task_map (
    todoist_id TEXT PRIMARY KEY, mstodo_id TEXT UNIQUE,
    todoist_project_id TEXT, mstodo_list_id TEXT, canon TEXT, updated_at REAL
);
"""

_CFG_DEFAULTS = {
    "conflict_winner": "todoist",
    "sync_interval": "60",
    "match_existing": "true",
}

PROVIDERS = ("todoist", "mstodo", "google")
PROVIDER_LABEL = {"todoist": "Todoist", "mstodo": "Microsoft To Do", "google": "Google Tasks"}
PROVIDER_ICON = {"todoist": "🔵", "mstodo": "🟣", "google": "🟢"}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self.db.commit()
        if self.get("schema_version") != "2":
            self._migrate_v1_to_v2()

    # ---- migration -------------------------------------------------------
    def _migrate_v1_to_v2(self):
        td_token = self.get("cfg:todoist_api_token")
        if td_token:
            self.set_connection("todoist", self.get("cfg:todoist_name") or "Todoist",
                                 {"token": td_token})
            self.set_cursor("todoist", "account", self.get("todoist_sync_token"))

        ms_refresh = self.get("ms_refresh_token")
        if ms_refresh:
            self.set_connection("mstodo", self.get("cfg:ms_email") or "Microsoft To Do",
                                 {"refresh_token": ms_refresh, "client_id": self.get("cfg:ms_client_id")})

        pairs = self.db.execute("SELECT * FROM list_pairs").fetchall()
        for p in pairs:
            gid = self.add_list_group(p["name"])
            self.add_list_group_member(gid, "todoist", p["todoist_project_id"])
            self.add_list_group_member(gid, "mstodo", p["mstodo_list_id"])
        # delta links were stored as kv 'delta:<list_id>' in v1
        for r in self.db.execute("SELECT key, value FROM kv WHERE key LIKE 'delta:%'"):
            list_id = r["key"][len("delta:"):]
            self.set_cursor("mstodo", list_id, r["value"])

        tasks = self.db.execute("SELECT * FROM task_map").fetchall()
        for t in tasks:
            canon = json.loads(t["canon"]) if t["canon"] else {}
            self.db.execute(
                "INSERT INTO task_groups(canon, updated_at) VALUES (?,?)",
                (json.dumps(canon), t["updated_at"]),
            )
            group_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.db.execute(
                "INSERT INTO task_links(group_id, provider, item_id, list_id) VALUES (?,?,?,?)",
                (group_id, "todoist", t["todoist_id"], t["todoist_project_id"]),
            )
            self.db.execute(
                "INSERT INTO task_links(group_id, provider, item_id, list_id) VALUES (?,?,?,?)",
                (group_id, "mstodo", t["mstodo_id"], t["mstodo_list_id"]),
            )

        self.set("schema_version", "2")
        self.commit()

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
        return len(self.all_connections()) >= 2 and self.get("cfg:configured") == "true"

    # ---- connections -------------------------------------------------
    def get_connection(self, provider):
        r = self.db.execute("SELECT * FROM connections WHERE provider=?", (provider,)).fetchone()
        if not r:
            return None
        return {"provider": provider, "display_name": r["display_name"],
                "creds": json.loads(r["creds"] or "{}")}

    def set_connection(self, provider, display_name, creds):
        self.db.execute(
            "INSERT INTO connections(provider,display_name,creds) VALUES (?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET display_name=excluded.display_name, creds=excluded.creds",
            (provider, display_name, json.dumps(creds)),
        )

    def update_creds(self, provider, creds):
        row = self.get_connection(provider)
        name = row["display_name"] if row else None
        self.set_connection(provider, name, creds)

    def remove_connection(self, provider):
        self.db.execute("DELETE FROM connections WHERE provider=?", (provider,))
        self.db.execute("DELETE FROM cursors WHERE provider=?", (provider,))
        self.db.execute("DELETE FROM list_group_members WHERE provider=?", (provider,))
        self.db.execute("DELETE FROM task_links WHERE provider=?", (provider,))

    def all_connections(self):
        return {r["provider"]: {"display_name": r["display_name"], "creds": json.loads(r["creds"] or "{}")}
                for r in self.db.execute("SELECT * FROM connections")}

    # ---- cursors -------------------------------------------------------
    def get_cursor(self, provider, scope):
        r = self.db.execute("SELECT cursor FROM cursors WHERE provider=? AND scope=?",
                            (provider, scope)).fetchone()
        return r["cursor"] if r else None

    def set_cursor(self, provider, scope, cursor):
        if cursor is None:
            return
        self.db.execute(
            "INSERT INTO cursors(provider,scope,cursor) VALUES (?,?,?) "
            "ON CONFLICT(provider,scope) DO UPDATE SET cursor=excluded.cursor",
            (provider, scope, cursor),
        )

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

    # ---- list groups -------------------------------------------------
    def add_list_group(self, name):
        self.db.execute("INSERT INTO list_groups(name) VALUES (?)", (name,))
        return self.db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_list_group_member(self, group_id, provider, list_id):
        self.db.execute(
            "INSERT OR REPLACE INTO list_group_members(group_id,provider,list_id) VALUES (?,?,?)",
            (group_id, provider, list_id),
        )

    def list_group_members(self, group_id):
        return {r["provider"]: r["list_id"] for r in self.db.execute(
            "SELECT provider, list_id FROM list_group_members WHERE group_id=?", (group_id,))}

    def delete_list_group(self, group_id):
        """A list behind this group was deleted on one provider -- mirror
        that everywhere: every task_group whose tasks lived in any of this
        group's member lists goes away, then the group itself, the same way
        an individual deleted task takes its whole task_group with it."""
        list_ids = list(self.list_group_members(group_id).values())
        if list_ids:
            placeholders = ",".join("?" * len(list_ids))
            rows = self.db.execute(
                f"SELECT DISTINCT group_id FROM task_links WHERE list_id IN ({placeholders})",
                list_ids,
            ).fetchall()
            for r in rows:
                self.db.execute("DELETE FROM task_links WHERE group_id=?", (r["group_id"],))
                self.db.execute("DELETE FROM task_groups WHERE id=?", (r["group_id"],))
        self.db.execute("DELETE FROM list_group_members WHERE group_id=?", (group_id,))
        self.db.execute("DELETE FROM list_groups WHERE id=?", (group_id,))

    def all_list_groups(self):
        groups = self.db.execute("SELECT * FROM list_groups").fetchall()
        return [{"id": g["id"], "name": g["name"], "members": self.list_group_members(g["id"])}
                for g in groups]

    def list_group_for(self, provider, list_id):
        r = self.db.execute(
            "SELECT group_id FROM list_group_members WHERE provider=? AND list_id=?",
            (provider, list_id),
        ).fetchone()
        if not r:
            return None
        return {"id": r["group_id"], "members": self.list_group_members(r["group_id"])}

    # ---- task groups -------------------------------------------------
    def task_group_for(self, provider, item_id):
        r = self.db.execute(
            "SELECT group_id FROM task_links WHERE provider=? AND item_id=?",
            (provider, item_id),
        ).fetchone()
        if not r:
            return None
        return self.get_task_group(r["group_id"])

    def get_task_group(self, group_id):
        g = self.db.execute("SELECT * FROM task_groups WHERE id=?", (group_id,)).fetchone()
        if not g:
            return None
        links = {r["provider"]: {"item_id": r["item_id"], "list_id": r["list_id"]}
                 for r in self.db.execute("SELECT * FROM task_links WHERE group_id=?", (group_id,))}
        return {"id": g["id"], "canon": json.loads(g["canon"]), "updated_at": g["updated_at"], "links": links}

    def create_task_group(self, canon, links):
        """links: {provider: (item_id, list_id)}"""
        self.db.execute("INSERT INTO task_groups(canon, updated_at) VALUES (?,?)",
                        (json.dumps(canon), time.time()))
        group_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for provider, (item_id, list_id) in links.items():
            self.add_task_link(group_id, provider, item_id, list_id)
        return group_id

    def add_task_link(self, group_id, provider, item_id, list_id):
        self.db.execute(
            "INSERT OR REPLACE INTO task_links(group_id,provider,item_id,list_id) VALUES (?,?,?,?)",
            (group_id, provider, item_id, list_id),
        )

    def update_task_group_canon(self, group_id, canon):
        self.db.execute("UPDATE task_groups SET canon=?, updated_at=? WHERE id=?",
                        (json.dumps(canon), time.time(), group_id))

    def delete_task_group(self, group_id):
        """The task itself was deleted (on some provider) and the deletion has
        been propagated to every other linked provider — remove the whole
        group. (Contrast with ``remove_task_link``, which drops just one
        provider's edge and keeps the group alive for the rest — used when a
        provider is disconnected, not when the underlying task is gone.)"""
        self.db.execute("DELETE FROM task_links WHERE group_id=?", (group_id,))
        self.db.execute("DELETE FROM task_groups WHERE id=?", (group_id,))

    def remove_task_link(self, provider, item_id):
        r = self.db.execute("SELECT group_id FROM task_links WHERE provider=? AND item_id=?",
                            (provider, item_id)).fetchone()
        if not r:
            return
        group_id = r["group_id"]
        self.db.execute("DELETE FROM task_links WHERE provider=? AND item_id=?", (provider, item_id))
        remaining = self.db.execute("SELECT COUNT(*) FROM task_links WHERE group_id=?",
                                    (group_id,)).fetchone()[0]
        if remaining == 0:
            self.db.execute("DELETE FROM task_groups WHERE id=?", (group_id,))

    def mapping_count(self):
        return self.db.execute("SELECT COUNT(*) FROM task_groups").fetchone()[0]

    def mapped_item_ids(self, provider, list_id=None):
        if list_id is None:
            rows = self.db.execute("SELECT item_id FROM task_links WHERE provider=?", (provider,))
        else:
            rows = self.db.execute(
                "SELECT item_id FROM task_links WHERE provider=? AND list_id=?", (provider, list_id))
        return {r["item_id"] for r in rows}

    # ---- reset -------------------------------------------------------
    def wipe_credentials(self):
        self.db.execute("DELETE FROM connections")
        self.db.execute("DELETE FROM cursors")
        self.delete("cfg:configured")
        self.commit()

    def wipe_all(self):
        for t in ("kv", "connections", "cursors", "list_groups", "list_group_members",
                  "task_groups", "task_links", "activity"):
            self.db.execute(f"DELETE FROM {t}")
        self.commit()

    def reset_sync_state(self):
        """Forget all task/list pairings + cursors (keeps accounts connected).
        The next sync re-pairs from scratch by title/name match."""
        self.db.execute("DELETE FROM cursors")
        self.db.execute("DELETE FROM task_links")
        self.db.execute("DELETE FROM task_groups")
        self.db.execute("DELETE FROM list_group_members")
        self.db.execute("DELETE FROM list_groups")
        self.commit()

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
