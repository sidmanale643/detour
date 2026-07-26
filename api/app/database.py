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
  interest_preferences_json TEXT NOT NULL DEFAULT '[]', custom_interests_json TEXT NOT NULL DEFAULT '[]',
  primary_intent TEXT, secondary_intents_json TEXT NOT NULL DEFAULT '[]',
  activity_styles_json TEXT NOT NULL DEFAULT '[]', primary_travel_mode TEXT,
  fallback_travel_modes_json TEXT NOT NULL DEFAULT '[]', total_time_minutes INTEGER,
  max_one_way_travel_minutes INTEGER, max_one_way_distance_metres INTEGER,
  accessibility_json TEXT NOT NULL DEFAULT '{}',
  preference_version INTEGER NOT NULL DEFAULT 1,
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
  started_at TEXT, start_expires_at TEXT,
  skipped_at TEXT, superseded_at TEXT, created_at TEXT NOT NULL,
  topic TEXT, intent TEXT, activity_style TEXT, travel_mode TEXT,
  route_duration_minutes INTEGER, route_distance_meters INTEGER,
  total_estimated_minutes INTEGER, match_reasons_json TEXT, preference_version INTEGER,
  UNIQUE(deck_id, slot)
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
    (
        "max_walking_minutes",
        "ALTER TABLE profiles ADD COLUMN max_walking_minutes INTEGER",
    ),
    ("travel_modes_json", "ALTER TABLE profiles ADD COLUMN travel_modes_json TEXT"),
    (
        "max_travel_minutes",
        "ALTER TABLE profiles ADD COLUMN max_travel_minutes INTEGER",
    ),
    ("movement_intensity", "ALTER TABLE profiles ADD COLUMN movement_intensity TEXT"),
    ("budget", "ALTER TABLE profiles ADD COLUMN budget TEXT"),
    ("social_comfort", "ALTER TABLE profiles ADD COLUMN social_comfort TEXT"),
    (
        "environment_preference",
        "ALTER TABLE profiles ADD COLUMN environment_preference TEXT",
    ),
    (
        "interest_preferences_json",
        "ALTER TABLE profiles ADD COLUMN interest_preferences_json TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "custom_interests_json",
        "ALTER TABLE profiles ADD COLUMN custom_interests_json TEXT NOT NULL DEFAULT '[]'",
    ),
    ("primary_intent", "ALTER TABLE profiles ADD COLUMN primary_intent TEXT"),
    (
        "secondary_intents_json",
        "ALTER TABLE profiles ADD COLUMN secondary_intents_json TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "activity_styles_json",
        "ALTER TABLE profiles ADD COLUMN activity_styles_json TEXT NOT NULL DEFAULT '[]'",
    ),
    ("primary_travel_mode", "ALTER TABLE profiles ADD COLUMN primary_travel_mode TEXT"),
    (
        "fallback_travel_modes_json",
        "ALTER TABLE profiles ADD COLUMN fallback_travel_modes_json TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "total_time_minutes",
        "ALTER TABLE profiles ADD COLUMN total_time_minutes INTEGER",
    ),
    (
        "max_one_way_travel_minutes",
        "ALTER TABLE profiles ADD COLUMN max_one_way_travel_minutes INTEGER",
    ),
    (
        "max_one_way_distance_metres",
        "ALTER TABLE profiles ADD COLUMN max_one_way_distance_metres INTEGER",
    ),
    (
        "accessibility_json",
        "ALTER TABLE profiles ADD COLUMN accessibility_json TEXT NOT NULL DEFAULT '{}'",
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
    ("walking_minutes", "ALTER TABLE quests ADD COLUMN walking_minutes INTEGER"),
    ("distance_source", "ALTER TABLE quests ADD COLUMN distance_source TEXT"),
    (
        "estimated_activity_minutes",
        "ALTER TABLE quests ADD COLUMN estimated_activity_minutes INTEGER",
    ),
    ("cost_band", "ALTER TABLE quests ADD COLUMN cost_band TEXT"),
    ("activity_type", "ALTER TABLE quests ADD COLUMN activity_type TEXT"),
    ("started_at", "ALTER TABLE quests ADD COLUMN started_at TEXT"),
    ("start_expires_at", "ALTER TABLE quests ADD COLUMN start_expires_at TEXT"),
    ("topic", "ALTER TABLE quests ADD COLUMN topic TEXT"),
    ("intent", "ALTER TABLE quests ADD COLUMN intent TEXT"),
    ("activity_style", "ALTER TABLE quests ADD COLUMN activity_style TEXT"),
    ("travel_mode", "ALTER TABLE quests ADD COLUMN travel_mode TEXT"),
    (
        "route_duration_minutes",
        "ALTER TABLE quests ADD COLUMN route_duration_minutes INTEGER",
    ),
    (
        "route_distance_meters",
        "ALTER TABLE quests ADD COLUMN route_distance_meters INTEGER",
    ),
    (
        "total_estimated_minutes",
        "ALTER TABLE quests ADD COLUMN total_estimated_minutes INTEGER",
    ),
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


def _backfill_profile_defaults(conn: sqlite3.Connection) -> None:
    """Preserve existing profiles with sensible defaults for new preference fields."""
    from .schemas.quests import CATEGORY_TO_MOTIVATIONS

    rows = conn.execute(
        """
        SELECT user_id, categories_json, motivations_json, max_walking_minutes,
               travel_modes_json, max_travel_minutes,
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
        if not row["travel_modes_json"]:
            updates["travel_modes_json"] = json.dumps(["walking"])
        if row["max_travel_minutes"] is None:
            updates["max_travel_minutes"] = row["max_walking_minutes"] or 20
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


def _json_list(raw: object) -> list[object]:
    if not raw:
        return []
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _json_object(raw: object) -> dict[str, object]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _backfill_canonical_preferences(conn: sqlite3.Connection) -> None:
    """Derive canonical preferences once from the legacy profile contract.

    The mapping intentionally never deletes legacy values. Canonical columns are
    filled only when unset, so startup is idempotent and future profile writes
    remain authoritative.
    """
    category_topics = {
        "nature": ["nature_outdoors"],
        "culture": ["history_heritage", "local_culture_community"],
        "creativity": ["art_design"],
        "learning": ["books_learning"],
    }
    category_intents = {
        "nature": ["explore"],
        "culture": ["explore", "learn"],
        "creativity": ["create"],
        "mindfulness": ["unwind"],
        "fitness": ["move"],
        "learning": ["learn"],
    }
    motivation_intents = {
        "explore": "explore",
        "move": "move",
        "learn": "learn",
        "create": "create",
        "reset": "unwind",
        "nature": "explore",
        "break_routine": "explore",
    }
    aliases = {
        "nature": "nature_outdoors",
        "outdoors": "nature_outdoors",
        "parks": "nature_outdoors",
        "park": "nature_outdoors",
        "hiking": "nature_outdoors",
        "heritage": "history_heritage",
        "history": "history_heritage",
        "museum": "history_heritage",
        "museums": "history_heritage",
        "architecture": "architecture_public_spaces",
        "buildings": "architecture_public_spaces",
        "art": "art_design",
        "design": "art_design",
        "gallery": "art_design",
        "galleries": "art_design",
        "books": "books_learning",
        "learning": "books_learning",
        "library": "books_learning",
        "libraries": "books_learning",
        "culture": "local_culture_community",
        "community": "local_culture_community",
        "markets": "food_markets",
        "market": "food_markets",
        "food": "food_markets",
        "music": "music_performance",
        "performance": "music_performance",
    }
    rows = conn.execute(
        """SELECT user_id, likes_json, dislikes_json, categories_json, motivations_json,
                  travel_modes_json, available_minutes, max_travel_minutes,
                  max_walking_minutes, movement_intensity, accessibility_notes,
                  interest_preferences_json, custom_interests_json, primary_intent,
                  secondary_intents_json, activity_styles_json, primary_travel_mode,
                  fallback_travel_modes_json, total_time_minutes,
                  max_one_way_travel_minutes, max_one_way_distance_metres,
                  accessibility_json, preference_version
           FROM profiles"""
    ).fetchall()
    for row in rows:
        updates: dict[str, object] = {}
        categories = [str(value) for value in _json_list(row["categories_json"])]
        motivations = [str(value) for value in _json_list(row["motivations_json"])]
        likes = [
            str(value).strip()
            for value in _json_list(row["likes_json"])
            if str(value).strip()
        ]
        dislikes = [
            str(value).strip()
            for value in _json_list(row["dislikes_json"])
            if str(value).strip()
        ]

        if not _json_list(row["interest_preferences_json"]):
            affinities: dict[str, str] = {}
            for category in categories:
                for topic in category_topics.get(category, []):
                    affinities[topic] = "love"
            custom: dict[str, str] = {}
            for label in likes:
                topic = aliases.get(label.casefold())
                if topic:
                    affinities[topic] = "love"
                else:
                    custom[label.casefold()] = "love"
            for label in dislikes:
                topic = aliases.get(label.casefold())
                if topic:
                    affinities[topic] = "avoid"
                else:
                    custom[label.casefold()] = "avoid"
            updates["interest_preferences_json"] = json.dumps(
                [
                    {"topic": topic, "affinity": affinity}
                    for topic, affinity in affinities.items()
                ]
            )
            if not _json_list(row["custom_interests_json"]):
                labels = likes + dislikes
                first_labels: dict[str, str] = {}
                for label in labels:
                    if aliases.get(label.casefold()) is None:
                        first_labels.setdefault(label.casefold(), label)
                updates["custom_interests_json"] = json.dumps(
                    [
                        {"label": first_labels[key], "affinity": affinity}
                        for key, affinity in custom.items()
                    ]
                )

        intents: list[str] = []
        for category in categories:
            intents.extend(category_intents.get(category, []))
        intents.extend(
            motivation_intents[value]
            for value in motivations
            if value in motivation_intents
        )
        intents = list(dict.fromkeys(intents)) or ["explore"]
        if not row["primary_intent"]:
            updates["primary_intent"] = intents[0]
        if not _json_list(row["secondary_intents_json"]):
            updates["secondary_intents_json"] = json.dumps(
                [intent for intent in intents[1:] if intent != intents[0]][:4]
            )

        styles = [str(value) for value in _json_list(row["activity_styles_json"])]
        if not styles and "fitness" in categories:
            updates["activity_styles_json"] = json.dumps(["workout"])

        legacy_modes = [str(value) for value in _json_list(row["travel_modes_json"])]
        valid_modes = [
            value
            for value in legacy_modes
            if value
            in {"walking", "cycling", "two_wheeler", "four_wheeler", "public_transport"}
        ]
        if not row["primary_travel_mode"]:
            updates["primary_travel_mode"] = (
                valid_modes[0] if valid_modes else "walking"
            )
        if not _json_list(row["fallback_travel_modes_json"]):
            updates["fallback_travel_modes_json"] = json.dumps(valid_modes[1:])
        if legacy_modes == ["running"] and not styles:
            updates["activity_styles_json"] = json.dumps(["workout"])

        if row["total_time_minutes"] is None:
            updates["total_time_minutes"] = row["available_minutes"] or 30
        if row["max_one_way_travel_minutes"] is None:
            updates["max_one_way_travel_minutes"] = (
                row["max_travel_minutes"] or row["max_walking_minutes"] or 20
            )
        if row["max_one_way_distance_metres"] is None:
            updates["max_one_way_distance_metres"] = 5_000
        accessibility = _json_object(row["accessibility_json"])
        if not accessibility:
            updates["accessibility_json"] = json.dumps(
                {
                    "step_free": False,
                    "wheelchair_access": False,
                    "max_walking_minutes": None,
                    "seating_required": False,
                    "low_sensory": False,
                    "notes": row["accessibility_notes"],
                }
            )
        if row["preference_version"] is None or int(row["preference_version"]) < 1:
            updates["preference_version"] = 1
        if updates:
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
        _backfill_canonical_preferences(conn)


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
