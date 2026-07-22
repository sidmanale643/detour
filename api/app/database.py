from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
  birth_date TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  email_verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  likes_json TEXT NOT NULL DEFAULT '[]', dislikes_json TEXT NOT NULL DEFAULT '[]',
  categories_json TEXT NOT NULL DEFAULT '[]', available_minutes INTEGER, accessibility_notes TEXT,
  home_cell TEXT, home_city TEXT, home_latitude REAL, home_longitude REAL,
  home_address TEXT, home_source TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decks (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  local_date TEXT NOT NULL, refreshed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(user_id, local_date)
);
CREATE TABLE IF NOT EXISTS quests (
  id TEXT PRIMARY KEY, deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,
  difficulty TEXT NOT NULL, base_xp INTEGER NOT NULL, place_name TEXT NOT NULL,
  latitude REAL NOT NULL, longitude REAL NOT NULL, time_window_start TEXT, time_window_end TEXT,
  expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'offered', completed_at TEXT,
  skipped_at TEXT, superseded_at TEXT, created_at TEXT NOT NULL, UNIQUE(deck_id, slot)
);
CREATE TABLE IF NOT EXISTS completions (
  quest_id TEXT PRIMARY KEY REFERENCES quests(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  awarded_xp INTEGER NOT NULL, multiplier REAL NOT NULL, completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS xp_ledger (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  quest_id TEXT NOT NULL UNIQUE REFERENCES quests(id) ON DELETE CASCADE,
  category TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_records (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, key TEXT NOT NULL,
  endpoint TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(user_id, key, endpoint)
);
CREATE TABLE IF NOT EXISTS active_dates (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, local_date TEXT NOT NULL,
  PRIMARY KEY(user_id, local_date)
);
"""

# Columns added after the initial schema. SQLite has no ADD COLUMN IF NOT EXISTS.
_PROFILE_MIGRATIONS: list[tuple[str, str]] = [
    ("home_latitude", "ALTER TABLE profiles ADD COLUMN home_latitude REAL"),
    ("home_longitude", "ALTER TABLE profiles ADD COLUMN home_longitude REAL"),
    ("home_address", "ALTER TABLE profiles ADD COLUMN home_address TEXT"),
    ("home_source", "ALTER TABLE profiles ADD COLUMN home_source TEXT"),
    ("motivations_json", "ALTER TABLE profiles ADD COLUMN motivations_json TEXT"),
    ("max_walking_minutes", "ALTER TABLE profiles ADD COLUMN max_walking_minutes INTEGER"),
    ("movement_intensity", "ALTER TABLE profiles ADD COLUMN movement_intensity TEXT"),
    ("budget", "ALTER TABLE profiles ADD COLUMN budget TEXT"),
    ("social_comfort", "ALTER TABLE profiles ADD COLUMN social_comfort TEXT"),
    ("environment_preference", "ALTER TABLE profiles ADD COLUMN environment_preference TEXT"),
]

_QUEST_MIGRATIONS: list[tuple[str, str]] = [
    ("place_provider_id", "ALTER TABLE quests ADD COLUMN place_provider_id TEXT"),
    ("place_type", "ALTER TABLE quests ADD COLUMN place_type TEXT"),
    ("distance_meters", "ALTER TABLE quests ADD COLUMN distance_meters INTEGER"),
    ("walking_minutes", "ALTER TABLE quests ADD COLUMN walking_minutes INTEGER"),
    ("distance_source", "ALTER TABLE quests ADD COLUMN distance_source TEXT"),
    ("estimated_activity_minutes", "ALTER TABLE quests ADD COLUMN estimated_activity_minutes INTEGER"),
    ("cost_band", "ALTER TABLE quests ADD COLUMN cost_band TEXT"),
    ("activity_type", "ALTER TABLE quests ADD COLUMN activity_type TEXT"),
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


def _backfill_profile_defaults(conn: sqlite3.Connection) -> None:
    """Preserve existing profiles with sensible defaults for new preference fields."""
    from .schemas.quests import CATEGORY_TO_MOTIVATIONS

    rows = conn.execute(
        """
        SELECT user_id, categories_json, motivations_json, max_walking_minutes,
               movement_intensity, budget, social_comfort, environment_preference,
               available_minutes
        FROM profiles
        """
    ).fetchall()
    for row in rows:
        updates: dict[str, object] = {}
        if not row["motivations_json"]:
            categories = json.loads(row["categories_json"] or "[]")
            motivations: list[str] = []
            seen: set[str] = set()
            for category in categories:
                for motivation in CATEGORY_TO_MOTIVATIONS.get(str(category), []):
                    value = motivation.value
                    if value not in seen:
                        seen.add(value)
                        motivations.append(value)
                    if len(motivations) >= 4:
                        break
                if len(motivations) >= 4:
                    break
            if not motivations:
                motivations = ["explore"]
            updates["motivations_json"] = json.dumps(motivations)
        if row["max_walking_minutes"] is None:
            updates["max_walking_minutes"] = 20
        if not row["movement_intensity"]:
            updates["movement_intensity"] = "gentle"
        if not row["budget"]:
            updates["budget"] = "free"
        if not row["social_comfort"]:
            updates["social_comfort"] = "solo_only"
        if not row["environment_preference"]:
            updates["environment_preference"] = "either"
        if row["available_minutes"] is None:
            updates["available_minutes"] = 30
        if not updates:
            continue
        assignments = ", ".join(f"{key}=?" for key in updates)
        conn.execute(
            f"UPDATE profiles SET {assignments} WHERE user_id=?",
            (*updates.values(), row["user_id"]),
        )


def initialize() -> None:
    path: Path = settings.sqlite_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _apply_column_migrations(conn, "profiles", _PROFILE_MIGRATIONS)
        _apply_column_migrations(conn, "quests", _QUEST_MIGRATIONS)
        _backfill_profile_defaults(conn)


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
