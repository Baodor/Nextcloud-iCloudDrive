"""SQLite metadata; credential fields are encrypted using a separately supplied key."""
import json
import sqlite3
import threading
from cryptography.fernet import Fernet


class Store:
    def __init__(self, root, key):
        root.mkdir(parents=True, exist_ok=True)
        self.crypto = Fernet(key.encode())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "bridge.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS users(uid TEXT PRIMARY KEY, public TEXT NOT NULL, secret TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, uid TEXT NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, uid TEXT NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS job_user ON jobs(uid);
          CREATE INDEX IF NOT EXISTS run_user ON runs(uid);
        """)
        self.db.commit()

    def user(self, uid):
        with self.lock:
            row = self.db.execute("SELECT public,secret FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return {"icloud_connected": False, "nextcloud_connected": False}, {}
        return json.loads(row[0]), json.loads(self.crypto.decrypt(row[1].encode()))

    def save_user(self, uid, public, secret):
        encrypted = self.crypto.encrypt(json.dumps(secret).encode()).decode()
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO users VALUES(?,?,?)", (uid, json.dumps(public), encrypted))

    def users(self):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT uid FROM users")]

    def records(self, table, uid=None):
        assert table in {"jobs", "runs"}
        with self.lock:
            rows = self.db.execute(f"SELECT data FROM {table}" + (" WHERE uid=?" if uid is not None else ""), (uid,) if uid is not None else ())
            return [json.loads(r[0]) for r in rows]

    def get(self, table, uid, identifier):
        assert table in {"jobs", "runs"}
        with self.lock:
            row = self.db.execute(f"SELECT data FROM {table} WHERE uid=? AND id=?", (uid, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, table, uid, data):
        assert table in {"jobs", "runs"}
        with self.lock, self.db:
            if table == "runs":
                row = self.db.execute("SELECT data FROM runs WHERE uid=? AND id=?", (uid, data["id"])).fetchone()
                if row and json.loads(row[0]).get("cancel_requested"):
                    data = {**data, "cancel_requested": True}
            self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?,?)", (data["id"], uid, json.dumps(data)))

    def delete(self, table, uid, identifier):
        assert table in {"jobs", "runs"}
        with self.lock, self.db:
            self.db.execute(f"DELETE FROM {table} WHERE uid=? AND id=?", (uid, identifier))

    def purge(self, uid):
        with self.lock, self.db:
            for table in ("jobs", "runs", "users"):
                self.db.execute(f"DELETE FROM {table} WHERE uid=?", (uid,))
