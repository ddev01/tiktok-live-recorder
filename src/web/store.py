import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    duration_seconds INTEGER,
    file_path TEXT,
    keep_forever INTEGER NOT NULL DEFAULT 0,
    pid INTEGER,
    worker_pid INTEGER,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_recording_per_user
ON recordings(username) WHERE status IN ('starting', 'recording', 'stopping');
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK(mode IN ('once', 'always')),
    duration_seconds INTEGER,
    max_duration_seconds INTEGER NOT NULL DEFAULT 14400,
    is_live INTEGER NOT NULL DEFAULT 0,
    last_started_at REAL,
    last_error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recent_searches (
    username TEXT PRIMARY KEY,
    searched_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._initialized = False
        self._init_lock = threading.Lock()

    def _new_connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _ensure_initialized(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            connection = self._new_connection()
            try:
                connection.executescript(SCHEMA)
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(recordings)")
                }
                if "worker_pid" not in columns:
                    connection.execute(
                        "ALTER TABLE recordings ADD COLUMN worker_pid INTEGER"
                    )
                if "keep_forever" not in columns:
                    connection.execute(
                        "ALTER TABLE recordings ADD COLUMN keep_forever INTEGER NOT NULL DEFAULT 0"
                    )
                    if "pinned" in columns:
                        connection.execute(
                            "UPDATE recordings SET keep_forever = pinned"
                        )
                connection.commit()
                self._initialized = True
            finally:
                connection.close()

    def _connect(self):
        self._ensure_initialized()
        return self._new_connection()

    @contextmanager
    def connect(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        self._ensure_initialized()

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recording(self, recording_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
            return dict(row) if row else None

    def recordings(self):
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recordings ORDER BY created_at DESC"
                )
            ]

    def watchlist(self):
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM watchlist ORDER BY created_at DESC"
                )
            ]

    def recent_searches(self):
        with self.connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT username FROM recent_searches ORDER BY searched_at DESC LIMIT 10"
                )
            ]

    def known_usernames(self):
        with self.connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT username FROM recordings "
                    "UNION SELECT username FROM watchlist "
                    "UNION SELECT username FROM recent_searches"
                )
            ]

    def search_known(self, query, limit=10):
        needle = (query or "").strip().lstrip("@").lower()
        if not needle:
            return []
        escaped = (
            needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT username FROM ("
                "SELECT username FROM recordings "
                "UNION SELECT username FROM watchlist "
                "UNION SELECT username FROM recent_searches"
                ") WHERE username = ? OR username LIKE ? ESCAPE '\\' "
                "ORDER BY username = ? DESC, username "
                "LIMIT ?",
                (needle, f"{escaped}%", needle, limit),
            )
            return [row[0] for row in rows]

    def remember_search(self, username, timestamp):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO recent_searches(username, searched_at) VALUES (?, ?) "
                "ON CONFLICT(username) DO UPDATE SET searched_at = excluded.searched_at",
                (username, timestamp),
            )

    def active_recording(self, username):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE username = ? "
                "AND status IN ('starting', 'recording', 'stopping')",
                (username,),
            ).fetchone()
            return dict(row) if row else None

    def enqueue_recording(self, username, duration_seconds, keep_forever=False):
        now = time.time()
        try:
            with self.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM recordings WHERE username = ? "
                    "AND status IN ('starting', 'recording', 'stopping')",
                    (username,),
                ).fetchone()
                if existing:
                    return dict(existing), False
                cursor = connection.execute(
                    "INSERT INTO recordings(username, status, created_at, duration_seconds, "
                    "keep_forever) VALUES (?, 'starting', ?, ?, ?)",
                    (username, now, duration_seconds, int(keep_forever)),
                )
                recording_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            existing = self.active_recording(username)
            if existing:
                return existing, False
            raise
        return self.recording(recording_id), True
