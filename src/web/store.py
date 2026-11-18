import difflib
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'tiktok' CHECK(platform IN ('tiktok', 'twitch')),
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
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'tiktok' CHECK(platform IN ('tiktok', 'twitch')),
    mode TEXT NOT NULL CHECK(mode IN ('once', 'always')),
    duration_seconds INTEGER,
    max_duration_seconds INTEGER NOT NULL DEFAULT 14400,
    is_live INTEGER NOT NULL DEFAULT 0,
    last_started_at REAL,
    last_error TEXT,
    created_at REAL NOT NULL,
    UNIQUE(username, platform)
);
CREATE TABLE IF NOT EXISTS recent_searches (
    username TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'tiktok' CHECK(platform IN ('tiktok', 'twitch')),
    searched_at REAL NOT NULL,
    PRIMARY KEY (username, platform)
);
CREATE TABLE IF NOT EXISTS following (
    username TEXT NOT NULL,
    nickname TEXT,
    platform TEXT NOT NULL DEFAULT 'tiktok' CHECK(platform IN ('tiktok', 'twitch')),
    synced_at REAL NOT NULL,
    PRIMARY KEY (username, platform)
);
CREATE TABLE IF NOT EXISTS recording_events (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('knock', 'elimination')),
    actor TEXT NOT NULL,
    actor_role TEXT NOT NULL CHECK(actor_role IN ('self', 'teammate', 'streamer', 'streamer_teammate')),
    victim TEXT NOT NULL,
    victim_role TEXT NOT NULL CHECK(victim_role IN ('self', 'teammate', 'streamer', 'streamer_teammate')),
    event_at REAL NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(recording_id, event_at, kind, actor, victim)
);
CREATE INDEX IF NOT EXISTS recording_events_recording
    ON recording_events(recording_id, event_at);
"""

WATCHLIST_REBUILD = """
CREATE TABLE watchlist_new (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'tiktok' CHECK(platform IN ('tiktok', 'twitch')),
    mode TEXT NOT NULL CHECK(mode IN ('once', 'always')),
    duration_seconds INTEGER,
    max_duration_seconds INTEGER NOT NULL DEFAULT 14400,
    is_live INTEGER NOT NULL DEFAULT 0,
    last_started_at REAL,
    last_error TEXT,
    created_at REAL NOT NULL,
    UNIQUE(username, platform)
);
INSERT INTO watchlist_new(
    id, username, platform, mode, duration_seconds, max_duration_seconds,
    is_live, last_started_at, last_error, created_at
)
SELECT id, username, 'tiktok', mode, duration_seconds, max_duration_seconds,
    is_live, last_started_at, last_error, created_at FROM watchlist;
DROP TABLE watchlist;
ALTER TABLE watchlist_new RENAME TO watchlist;
"""

RECENT_REBUILD = """
CREATE TABLE recent_searches_new (
    username TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'tiktok' CHECK(platform IN ('tiktok', 'twitch')),
    searched_at REAL NOT NULL,
    PRIMARY KEY (username, platform)
);
INSERT INTO recent_searches_new(username, platform, searched_at)
SELECT username, 'tiktok', searched_at FROM recent_searches;
DROP TABLE recent_searches;
ALTER TABLE recent_searches_new RENAME TO recent_searches;
"""


def _flatten(value):
    """Lowercase and drop separators so @A.B,_c matches abc."""
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lstrip("@").lower())


def _subsequence(needle, value):
    chars = iter(value)
    return all(ch in chars for ch in needle)


def _closest_window(needle, value):
    """Best similarity against same-width slices, so a long username still matches."""
    width = len(needle)
    best = 0.0
    for start in range(max(1, len(value) - width + 1)):
        ratio = difflib.SequenceMatcher(
            None, needle, value[start : start + width]
        ).ratio()
        if ratio > best:
            best = ratio
    return best


def _match_rank(name, needle):
    flat = _flatten(name)
    if flat == needle:
        return (0, 0)
    if flat.startswith(needle):
        return (1, 0)
    index = flat.find(needle)
    if index >= 0:
        return (2, index)
    if _subsequence(needle, flat):
        return (3, 0)
    if len(needle) >= 3:
        ratio = _closest_window(needle, flat)
        if ratio >= 0.8:
            return (4, -ratio)
    return None


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
                if "platform" not in columns:
                    connection.execute(
                        "ALTER TABLE recordings ADD COLUMN platform TEXT NOT NULL DEFAULT 'tiktok'"
                    )
                index = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'one_active_recording_per_user'"
                ).fetchone()
                if not index or "platform" not in (index[0] or ""):
                    connection.execute(
                        "DROP INDEX IF EXISTS one_active_recording_per_user"
                    )
                    connection.execute(
                        "CREATE UNIQUE INDEX one_active_recording_per_user "
                        "ON recordings(username, platform) "
                        "WHERE status IN ('starting', 'recording', 'stopping')"
                    )
                watch_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(watchlist)")
                }
                if "platform" not in watch_columns:
                    connection.executescript(WATCHLIST_REBUILD)
                search_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(recent_searches)")
                }
                if "platform" not in search_columns:
                    connection.executescript(RECENT_REBUILD)
                following_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(following)")
                }
                if "nickname" not in following_columns:
                    connection.execute(
                        "ALTER TABLE following ADD COLUMN nickname TEXT"
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

    def recent_searches(self, platform=None):
        with self.connect() as connection:
            if platform:
                rows = connection.execute(
                    "SELECT username, platform FROM recent_searches "
                    "WHERE platform = ? ORDER BY searched_at DESC LIMIT 10",
                    (platform,),
                )
            else:
                rows = connection.execute(
                    "SELECT username, platform FROM recent_searches "
                    "ORDER BY searched_at DESC LIMIT 10"
                )
            return [dict(row) for row in rows]

    def known_usernames(self):
        with self.connect() as connection:
            return [
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT username, platform FROM recordings "
                    "UNION SELECT username, platform FROM watchlist "
                    "UNION SELECT username, platform FROM recent_searches"
                )
            ]

    def search_known(self, query, platform="tiktok", limit=10):
        needle = _flatten(query)
        if not needle:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT username, NULL AS nickname FROM recordings WHERE platform = ? "
                "UNION ALL SELECT username, NULL FROM watchlist WHERE platform = ? "
                "UNION ALL SELECT username, NULL FROM recent_searches WHERE platform = ? "
                "UNION ALL SELECT username, nickname FROM following WHERE platform = ?",
                (platform, platform, platform, platform),
            ).fetchall()
        nicknames = {
            row["username"]: row["nickname"]
            for row in rows
            if row["nickname"]
        }
        # shortcut: rank in Python; revisit if known accounts reach tens of thousands.
        ranked = []
        for username in {row["username"] for row in rows}:
            rank = _match_rank(username, needle)
            nickname = nicknames.get(username)
            if nickname:
                nickname_rank = _match_rank(nickname, needle)
                if nickname_rank is not None and (
                    rank is None or nickname_rank < rank
                ):
                    rank = nickname_rank
            if rank is not None:
                ranked.append((rank, username))
        ranked.sort()
        return [
            {"username": username, "nickname": nicknames.get(username)}
            for _, username in ranked[:limit]
        ]

    def remember_search(self, username, timestamp, platform="tiktok"):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO recent_searches(username, platform, searched_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(username, platform) DO UPDATE SET "
                "searched_at = excluded.searched_at",
                (username, platform, timestamp),
            )

    def replace_following(self, accounts, platform="tiktok"):
        """Replace the synced Following list; returns how many names were kept."""
        kept = {}
        for account in accounts:
            if isinstance(account, str):
                username, nickname = account, None
            else:
                username = account.get("username")
                nickname = account.get("nickname")
            username = (username or "").strip().lstrip("@").lower()
            if not username:
                continue
            kept[username] = (nickname or "").strip() or None
        now = time.time()
        with self.transaction() as connection:
            connection.execute("DELETE FROM following WHERE platform = ?", (platform,))
            connection.executemany(
                "INSERT INTO following(username, nickname, platform, synced_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (username, nickname, platform, now)
                    for username, nickname in kept.items()
                ],
            )
        return len(kept)

    def active_recording(self, username, platform="tiktok"):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE username = ? AND platform = ? "
                "AND status IN ('starting', 'recording', 'stopping')",
                (username, platform),
            ).fetchone()
            return dict(row) if row else None

    def enqueue_recording(
        self, username, duration_seconds, keep_forever=False, platform="tiktok"
    ):
        now = time.time()
        try:
            with self.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM recordings WHERE username = ? AND platform = ? "
                    "AND status IN ('starting', 'recording', 'stopping')",
                    (username, platform),
                ).fetchone()
                if existing:
                    return dict(existing), False
                cursor = connection.execute(
                    "INSERT INTO recordings(username, platform, status, created_at, "
                    "duration_seconds, keep_forever) VALUES (?, ?, 'starting', ?, ?, ?)",
                    (username, platform, now, duration_seconds, int(keep_forever)),
                )
                recording_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            existing = self.active_recording(username, platform)
            if existing:
                return existing, False
            raise
        return self.recording(recording_id), True

    def recording_for_event(self, username, platform, event_at):
        """Most recent recording for this account that can contain event_at."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE username = ? AND platform = ? "
                "AND created_at <= ? ORDER BY created_at DESC LIMIT 1",
                (username, platform, event_at),
            ).fetchone()
        recording = dict(row) if row else None
        if not recording:
            return None
        started_at = recording["started_at"]
        finished_at = recording["finished_at"]
        if started_at is not None and event_at < started_at - 1:
            return None
        if finished_at is not None and event_at > finished_at + 1:
            return None
        return recording

    def add_recording_events(self, recording_id, events):
        """Insert events; returns one bool per event, False for a duplicate."""
        stored = []
        with self.transaction() as connection:
            for event in events:
                cursor = connection.execute(
                    "INSERT INTO recording_events(recording_id, kind, actor, "
                    "actor_role, victim, victim_role, event_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(recording_id, event_at, kind, actor, victim) "
                    "DO NOTHING",
                    (
                        recording_id,
                        event["kind"],
                        event["actor"],
                        event["actor_role"],
                        event["victim"],
                        event["victim_role"],
                        event["event_at"],
                        event["created_at"],
                    ),
                )
                stored.append(cursor.rowcount == 1)
        return stored

    def recording_events(self, recording_id):
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recording_events WHERE recording_id = ? "
                    "ORDER BY event_at, id",
                    (recording_id,),
                )
            ]
