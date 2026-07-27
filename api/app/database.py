from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS local_player (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
  user_id INTEGER PRIMARY KEY REFERENCES local_player(id) ON DELETE CASCADE,
  interest_preferences_json TEXT NOT NULL DEFAULT '[]', custom_interests_json TEXT NOT NULL DEFAULT '[]',
  max_one_way_distance_metres INTEGER,
  preference_version INTEGER NOT NULL DEFAULT 1,
  home_cell TEXT, home_city TEXT, home_latitude REAL, home_longitude REAL,
  home_address TEXT, home_source TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decks (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES local_player(id) ON DELETE CASCADE,
  local_date TEXT NOT NULL, refreshed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(user_id, local_date)
);
CREATE TABLE IF NOT EXISTS quests (
  id TEXT PRIMARY KEY, deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,
  difficulty TEXT NOT NULL, base_xp INTEGER NOT NULL, place_name TEXT NOT NULL,
  latitude REAL NOT NULL, longitude REAL NOT NULL, time_window_start TEXT, time_window_end TEXT,
  expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'offered', completed_at TEXT,
  started_at TEXT, start_expires_at TEXT,
  skipped_at TEXT, superseded_at TEXT, created_at TEXT NOT NULL,
  topic TEXT, match_reasons_json TEXT, preference_version INTEGER,
  UNIQUE(deck_id, slot)
);
CREATE TABLE IF NOT EXISTS completions (
  quest_id TEXT PRIMARY KEY REFERENCES quests(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES local_player(id) ON DELETE CASCADE,
  awarded_xp INTEGER NOT NULL, multiplier REAL NOT NULL, completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS xp_ledger (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES local_player(id) ON DELETE CASCADE,
  quest_id TEXT NOT NULL UNIQUE REFERENCES quests(id) ON DELETE CASCADE,
  category TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_records (
  user_id INTEGER NOT NULL REFERENCES local_player(id) ON DELETE CASCADE, key TEXT NOT NULL,
  endpoint TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(user_id, key, endpoint)
);
CREATE TABLE IF NOT EXISTS active_dates (
  user_id INTEGER NOT NULL REFERENCES local_player(id) ON DELETE CASCADE, local_date TEXT NOT NULL,
  PRIMARY KEY(user_id, local_date)
);
"""

# Columns added after the initial schema. SQLite has no ADD COLUMN IF NOT EXISTS.
_PROFILE_MIGRATIONS: list[tuple[str, str]] = [
    ("home_latitude", "ALTER TABLE profiles ADD COLUMN home_latitude REAL"),
    ("home_longitude", "ALTER TABLE profiles ADD COLUMN home_longitude REAL"),
    ("home_address", "ALTER TABLE profiles ADD COLUMN home_address TEXT"),
    ("home_source", "ALTER TABLE profiles ADD COLUMN home_source TEXT"),
    ("movement_intensity", "ALTER TABLE profiles ADD COLUMN movement_intensity TEXT"),
    (
        "interest_preferences_json",
        "ALTER TABLE profiles ADD COLUMN interest_preferences_json TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "custom_interests_json",
        "ALTER TABLE profiles ADD COLUMN custom_interests_json TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "max_one_way_distance_metres",
        "ALTER TABLE profiles ADD COLUMN max_one_way_distance_metres INTEGER",
    ),
    (
        "preference_version",
        "ALTER TABLE profiles ADD COLUMN preference_version INTEGER NOT NULL DEFAULT 1",
    ),
]

_QUEST_MIGRATIONS: list[tuple[str, str]] = [
    ("place_provider_id", "ALTER TABLE quests ADD COLUMN place_provider_id TEXT"),
    ("place_type", "ALTER TABLE quests ADD COLUMN place_type TEXT"),
    ("distance_meters", "ALTER TABLE quests ADD COLUMN distance_meters INTEGER"),
    ("distance_source", "ALTER TABLE quests ADD COLUMN distance_source TEXT"),
    ("activity_type", "ALTER TABLE quests ADD COLUMN activity_type TEXT"),
    ("started_at", "ALTER TABLE quests ADD COLUMN started_at TEXT"),
    ("start_expires_at", "ALTER TABLE quests ADD COLUMN start_expires_at TEXT"),
    ("topic", "ALTER TABLE quests ADD COLUMN topic TEXT"),
    ("match_reasons_json", "ALTER TABLE quests ADD COLUMN match_reasons_json TEXT"),
    ("preference_version", "ALTER TABLE quests ADD COLUMN preference_version INTEGER"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _apply_column_migrations(
    conn: sqlite3.Connection, table: str, migrations: list[tuple[str, str]]
) -> None:
    columns = _table_columns(conn, table)
    for name, ddl in migrations:
        if name not in columns:
            conn.execute(ddl)


_LEGACY_PLAYER_TABLES = (
    "profiles",
    "decks",
    "quests",
    "completions",
    "xp_ledger",
    "idempotency_records",
    "active_dates",
)


def _copy_shared_columns(
    conn: sqlite3.Connection,
    *,
    source: str,
    target: str,
    where: str = "",
    parameters: tuple[object, ...] = (),
    player_scoped: bool = False,
) -> None:
    source_columns = _table_columns(conn, source)
    target_columns = _table_columns(conn, target)
    columns = sorted((source_columns & target_columns) - {"user_id"})
    if player_scoped:
        insert_columns = ["user_id", *columns]
        select_columns = ["1", *columns]
    else:
        insert_columns = columns
        select_columns = columns
    if not insert_columns:
        return
    conn.execute(
        f"INSERT INTO {target} ({', '.join(insert_columns)}) "
        f"SELECT {', '.join(select_columns)} FROM {source}{where}",
        parameters,
    )


def _migrate_legacy_auth_database(conn: sqlite3.Connection) -> None:
    """Collapse an account-based database into the one anonymous local player."""
    if "users" not in {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        return

    selected = conn.execute(
        "SELECT id, timezone, created_at FROM users ORDER BY id LIMIT 1"
    ).fetchone()
    selected_id = int(selected["id"]) if selected else None
    timezone = str(selected["timezone"]) if selected else "Asia/Kolkata"
    created_at = str(selected["created_at"]) if selected else ""

    for table in _LEGACY_PLAYER_TABLES:
        if _table_columns(conn, table):
            conn.execute(f"CREATE TEMP TABLE _legacy_{table} AS SELECT * FROM {table}")

    # Foreign keys are disabled by initialize before this point. Drop every table
    # that directly or transitively refers to the old account table, then recreate
    # the current schema with local_player as its only owner.
    for table in (
        "completions",
        "xp_ledger",
        "idempotency_records",
        "active_dates",
        "quests",
        "decks",
        "profiles",
        "sessions",
        "users",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO local_player(id, timezone, created_at) VALUES(1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             timezone=excluded.timezone,
             created_at=excluded.created_at""",
        (timezone, created_at or "1970-01-01T00:00:00+00:00"),
    )
    if selected_id is None:
        return

    _copy_shared_columns(
        conn,
        source="_legacy_profiles",
        target="profiles",
        where=" WHERE user_id=?",
        parameters=(selected_id,),
        player_scoped=True,
    )
    _copy_shared_columns(
        conn,
        source="_legacy_decks",
        target="decks",
        where=" WHERE user_id=?",
        parameters=(selected_id,),
        player_scoped=True,
    )
    _copy_shared_columns(
        conn,
        source="_legacy_quests",
        target="quests",
        where=(
            " WHERE deck_id IN "
            "(SELECT id FROM _legacy_decks WHERE user_id=?)"
        ),
        parameters=(selected_id,),
    )
    for table in ("completions", "xp_ledger", "idempotency_records", "active_dates"):
        _copy_shared_columns(
            conn,
            source=f"_legacy_{table}",
            target=table,
            where=" WHERE user_id=?",
            parameters=(selected_id,),
            player_scoped=True,
        )


def initialize() -> None:
    path: Path = settings.sqlite_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        _migrate_legacy_auth_database(conn)
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_column_migrations(conn, "profiles", _PROFILE_MIGRATIONS)
        _apply_column_migrations(conn, "quests", _QUEST_MIGRATIONS)
        conn.execute(
            "INSERT OR IGNORE INTO local_player(id, timezone, created_at) VALUES(1, ?, datetime('now'))",
            ("Asia/Kolkata",),
        )
        conn.execute(
            """INSERT OR IGNORE INTO profiles(
                user_id, movement_intensity, updated_at
            ) VALUES(?,?,datetime('now'))""",
            (
                1,
                "gentle",
            ),
        )


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.sqlite_path, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
