from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time as time_module
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import h3
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from .database import connect, initialize, transaction
from .providers import (
    CityDiscoveryProvider,
    OpenStreetMapPlaceProvider,
    PlaceDiscoveryUnavailable,
    PlaceProviderUnavailable,
    RouteConfigurationError,
    RouteProvider,
    RouteServiceError,
    SupportedTravelMode,
)
from .schemas.quests import (
    MAX_SEARCH_RADIUS_METRES,
    GeneratedQuest,
    InterestAffinity,
    InterestPreference,
    InterestTopic,
    MovementIntensity,
    PlaceCandidateIn,
    ProfilePreferences,
    QuestGenerationRequest,
)
from .services.quest_generation import (
    QuestGenerationError,
    build_service_from_settings,
)

# Uvicorn configures this logger for application-visible INFO logs by default.
logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Detour API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
DECK_SIZE = 5
MAX_GENERATION_BATCH = 5
GENERATION_CANDIDATE_SHORTLIST = 12


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def local_context(user: sqlite3.Row) -> tuple[date, ZoneInfo]:
    try:
        zone = ZoneInfo(user["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(422, "Invalid account timezone") from exc
    return now().astimezone(zone).date(), zone


def current_player() -> sqlite3.Row:
    """Return the one local player; no request identity is required."""
    with connect() as db:
        player = db.execute("SELECT * FROM local_player WHERE id=1").fetchone()
    if player is None:
        raise RuntimeError("Local player was not initialized")
    return player


class ProfilePatch(BaseModel):
    """Partial profile update — omitted fields are left unchanged."""

    movement_intensity: str | None = None
    interest_preferences: dict[InterestTopic, InterestAffinity] | None = None
    custom_interests: list[str] | None = Field(default=None, max_length=20)
    max_one_way_distance_metres: int | None = Field(default=None, ge=250, le=150_000)

    @field_validator("movement_intensity")
    @classmethod
    def validate_intensity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return MovementIntensity(value).value
        except ValueError as exc:
            raise ValueError("Invalid movement_intensity") from exc

class HomeZoneRequest(BaseModel):
    city: str = Field(min_length=2, max_length=100)
    address: str = Field(min_length=2, max_length=300)
    source: str = Field(pattern=r"^(address|live_location)$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RoutePoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RoutePreviewRequest(BaseModel):
    origin: RoutePoint
    destination: RoutePoint
    travel_mode: SupportedTravelMode = "walking"


class QuestOut(BaseModel):
    id: str
    slot: int
    title: str
    description: str
    category: str
    difficulty: str
    base_xp: int
    place_name: str
    latitude: float
    longitude: float
    time_window_start: str | None
    time_window_end: str | None
    expires_at: datetime
    state: str
    completed_at: datetime | None = None
    started_at: datetime | None = None
    start_expires_at: datetime | None = None
    place_provider_id: str | None = None
    place_type: str | None = None
    distance_meters: int | None = None
    distance_source: str | None = None
    activity_type: str | None = None
    topic: str | None = None
    match_reasons: list[str] = Field(default_factory=list)
    preference_version: int | None = None


class DeckOut(BaseModel):
    local_date: date
    refreshed: bool
    refresh_available: bool
    quests: list[QuestOut]


def quest_out(row: sqlite3.Row) -> QuestOut:
    data = dict(row)
    return QuestOut(
        id=data["id"],
        slot=data["slot"],
        title=data["title"],
        description=data["description"],
        category=data["category"],
        difficulty=data["difficulty"],
        base_xp=data["base_xp"],
        place_name=data["place_name"],
        latitude=data["latitude"],
        longitude=data["longitude"],
        time_window_start=data.get("time_window_start"),
        time_window_end=data.get("time_window_end"),
        expires_at=data["expires_at"],
        state=data["state"],
        completed_at=data.get("completed_at"),
        started_at=parse_iso(data["started_at"]) if data.get("started_at") else None,
        start_expires_at=(
            parse_iso(data["start_expires_at"])
            if data.get("start_expires_at")
            else None
        ),
        place_provider_id=data.get("place_provider_id"),
        place_type=data.get("place_type"),
        distance_meters=data.get("distance_meters"),
        distance_source=data.get("distance_source"),
        activity_type=data.get("activity_type"),
        topic=data.get("topic"),
        match_reasons=json.loads(data.get("match_reasons_json") or "[]"),
        preference_version=data.get("preference_version"),
    )


def profile_int(profile: sqlite3.Row, key: str, default: int) -> int:
    if key not in profile.keys() or profile[key] is None:
        return default
    return int(profile[key])


def profile_str(profile: sqlite3.Row, key: str, default: str) -> str:
    if key not in profile.keys() or not profile[key]:
        return default
    return str(profile[key])


def _json_value(raw: object, fallback: object) -> object:
    try:
        return json.loads(str(raw)) if raw else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def profile_preferences(profile: sqlite3.Row) -> ProfilePreferences:
    """Return the canonical preference contract after migration/backfill."""
    interest_raw = _json_value(profile["interest_preferences_json"], [])
    custom_raw = _json_value(profile["custom_interests_json"], [])
    if not isinstance(interest_raw, list):
        interest_raw = []
    saved_affinities = {
        str(item.get("topic")): str(item.get("affinity"))
        for item in interest_raw
        if isinstance(item, dict)
    }
    interest_raw = [
        {
            "topic": topic.value,
            "affinity": saved_affinities.get(topic.value, "okay"),
        }
        for topic in InterestTopic
    ]
    if not isinstance(custom_raw, list):
        custom_raw = []
    return ProfilePreferences(
        interest_preferences=interest_raw,
        custom_interests=custom_raw,
        max_one_way_distance_metres=profile_int(
            profile, "max_one_way_distance_metres", 5_000
        ),
        preference_version=profile_int(profile, "preference_version", 1),
    )


def require_ready(user: sqlite3.Row) -> sqlite3.Row:
    with connect() as db:
        profile = db.execute(
            "SELECT * FROM profiles WHERE user_id=?", (user["id"],)
        ).fetchone()
    if not profile or not profile["home_cell"]:
        raise HTTPException(409, "Set a home zone before generating quests")
    return profile


def home_center(profile: sqlite3.Row) -> tuple[float, float]:
    """Return the saved home-zone center."""
    if profile["home_latitude"] is not None and profile["home_longitude"] is not None:
        return float(profile["home_latitude"]), float(profile["home_longitude"])
    raise HTTPException(409, "Set a home location before generating quests")


def place_search_interests(profile: sqlite3.Row) -> list[str]:
    """Return exactly the non-avoided interests for OSM place discovery."""
    preferences = profile_preferences(profile)
    return [
        item.topic.value
        for item in preferences.interest_preferences
        if item.affinity != InterestAffinity.avoid
    ]


def discovery_interests_for_profile(profile: sqlite3.Row) -> list[str]:
    """Return selected user-facing interests for place discovery."""
    preferences = profile_preferences(profile)
    return [
        item.topic.value
        for item in preferences.interest_preferences
        if item.affinity != InterestAffinity.avoid
    ]


def place_candidates_for(profile: sqlite3.Row) -> list[PlaceCandidateIn]:
    """Load OSM places and retain only verified routes allowed by the profile."""
    center = home_center(profile)
    preferences = profile_preferences(profile)
    interest_values = place_search_interests(profile)
    provider = OpenStreetMapPlaceProvider()
    radius_metres = min(
        MAX_SEARCH_RADIUS_METRES, preferences.max_one_way_distance_metres
    )

    # Discovery covers the full configured area. Travel preferences remain
    # available to the LLM as profile context instead of filtering to walking.
    try:
        raw = provider.candidates(
            profile["home_city"],
            interest_values,
            center,
            profile["home_cell"],
            radius_metres=radius_metres,
        )
    except PlaceProviderUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    logger.info("Quest candidate discovery completed: osm_candidates=%s", len(raw))

    affinities = {
        item.topic: item.affinity for item in preferences.interest_preferences
    }
    results: list[PlaceCandidateIn] = []
    for item in raw:
        if item.distance_metres > preferences.max_one_way_distance_metres:
            continue
        try:
            topic = InterestTopic(item.category)
        except ValueError:
            continue
        if affinities.get(topic, InterestAffinity.okay) == InterestAffinity.avoid:
            continue
        results.append(
            PlaceCandidateIn(
                provider_id=item.provider_id,
                name=item.name,
                category=topic,
                topics=[topic],
                latitude=item.latitude,
                longitude=item.longitude,
                place_type=item.place_type,
                public_access=item.public_access,
                verified_features=item.verified_features,
                distance_metres=item.distance_metres,
                distance_source="approximate",
                landmark_rank=item.landmark_rank,
            )
        )
    if not results:
        raise HTTPException(
            503,
            (
                "No public places were found near your home. "
                "Try setting home closer to parks, libraries, or public squares."
            ),
        )

    def candidate_score(candidate: PlaceCandidateIn) -> tuple[int, int, int, str]:
        affinity_score = max(
            (
                100 if affinities.get(topic) == InterestAffinity.love else 30
                for topic in candidate.topics
            ),
            default=0,
        )
        return (
            -affinity_score,
            candidate.landmark_rank,
            candidate.distance_metres,
            candidate.name.casefold(),
        )

    return sorted(results, key=candidate_score)


def generate_quests_for_profile(
    profile: sqlite3.Row,
    *,
    count: int,
    exclude_titles: list[str] | None = None,
    exclude_provider_ids: list[str] | None = None,
) -> list[GeneratedQuest]:
    """Generate up to 5 quests per OpenRouter call; batch when count > 5."""
    exclude_titles = list(exclude_titles or [])
    exclude_providers = set(exclude_provider_ids or [])
    canonical = profile_preferences(profile)
    logger.info(
        "Preparing quest deck generation: requested=%s preference_version=%s refresh=%s",
        count,
        canonical.preference_version,
        bool(exclude_titles or exclude_providers),
    )
    started_at = time_module.monotonic()
    try:
        candidates = place_candidates_for(profile)
    except HTTPException as exc:
        logger.warning(
            "Quest deck candidate preparation failed: status=%s detail=%s elapsed_ms=%s",
            exc.status_code,
            exc.detail,
            round((time_module.monotonic() - started_at) * 1000),
        )
        raise
    logger.info("Found quest place candidates: count=%s", len(candidates))
    # Drop places already used in the current deck (refresh) before opaque IDs.
    if exclude_providers:
        candidates = [c for c in candidates if c.provider_id not in exclude_providers]
        logger.info(
            "Filtered quest candidates for refresh: remaining=%s excluded_destinations=%s",
            len(candidates),
            len(exclude_providers),
        )
        if len(candidates) < count:
            logger.warning(
                "Insufficient alternative quest candidates: available=%s requested=%s",
                len(candidates),
                count,
            )
            raise HTTPException(
                503,
                "Not enough alternative destinations were found near your home",
            )
    try:
        service = build_service_from_settings()
    except QuestGenerationError as exc:
        logger.warning("Quest generation service is unavailable: detail=%s", exc)
        raise HTTPException(503, str(exc)) from exc

    try:
        intensity = MovementIntensity(
            profile_str(profile, "movement_intensity", "gentle")
        )
    except ValueError:
        intensity = MovementIntensity.gentle
    generated: list[GeneratedQuest] = []
    remaining = count
    used_provider_ids: set[str] = set(exclude_providers)
    while remaining > 0:
        batch_size = min(MAX_GENERATION_BATCH, remaining)
        # Exclude candidates already bound in this multi-batch deck build.
        batch_candidates = [
            c for c in candidates if c.provider_id not in used_provider_ids
        ]
        if len(batch_candidates) < batch_size:
            logger.warning(
                "Insufficient quest candidates for generation batch: available=%s requested=%s",
                len(batch_candidates),
                batch_size,
            )
            raise HTTPException(
                503, "Could not build a full quest deck from nearby places"
            )
        # Candidates are ranked by route estimate and distance. Keep the LLM input
        # focused on a small, strong set of nearby options.
        batch_candidates = batch_candidates[:GENERATION_CANDIDATE_SHORTLIST]
        request = QuestGenerationRequest(
            city=profile["home_city"] or "your city",
            count=batch_size,
            movement_intensity=intensity,
            interest_preferences=canonical.interest_preferences,
            custom_interests=canonical.custom_interests,
            preference_version=canonical.preference_version,
            place_candidates=batch_candidates,
            exclude_titles=exclude_titles + [q.title for q in generated],
        )
        try:
            logger.info(
                "Generating quest deck batch: batch_size=%s remaining=%s candidates=%s",
                batch_size,
                remaining,
                len(batch_candidates),
            )
            batch = service.generate(request)
        except QuestGenerationError as exc:
            logger.warning("Quest deck batch generation failed: error=%s", exc)
            raise HTTPException(503, str(exc)) from exc
        for quest in batch:
            used_provider_ids.add(quest.place_provider_id)
        generated.extend(batch)
        remaining = count - len(generated)
        logger.info(
            "Quest deck batch completed: generated=%s requested=%s remaining=%s",
            len(generated),
            count,
            remaining,
        )
    logger.info(
        "Quest deck generation completed: generated=%s requested=%s elapsed_ms=%s",
        len(generated),
        count,
        round((time_module.monotonic() - started_at) * 1000),
    )
    return generated[:count]


def insert_quest_row(
    db: sqlite3.Connection,
    *,
    deck_id: int,
    slot: int,
    quest: GeneratedQuest,
    expiry: datetime,
) -> None:
    db.execute(
        """INSERT INTO quests(
            id,deck_id,slot,title,description,category,difficulty,base_xp,
            place_name,latitude,longitude,time_window_start,time_window_end,
            expires_at,created_at,
            place_provider_id,place_type,distance_meters,distance_source,
            activity_type,topic,match_reasons_json,
            preference_version
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()),
            deck_id,
            slot,
            quest.title,
            quest.description,
            quest.category.value,
            quest.difficulty.value,
            quest.base_xp,
            quest.place_name,
            quest.latitude,
            quest.longitude,
            None,
            None,
            iso(expiry),
            iso(now()),
            quest.place_provider_id,
            quest.place_type,
            quest.distance_meters,
            quest.distance_source.value,
            quest.activity_type,
            quest.topic.value if quest.topic else None,
            json.dumps(quest.match_reasons),
            quest.preference_version,
        ),
    )


def deck_for(user: sqlite3.Row, profile: sqlite3.Row) -> DeckOut:
    """Return today’s stored deck without generating quests.

    Generation is explicit via POST /v1/decks/today/generate so an empty day
    stays empty until the player presses Generate.
    """
    del profile  # Profile is required by callers for auth readiness; not used here.
    local_day, _zone = local_context(user)
    with transaction() as db:
        db.execute(
            """UPDATE quests
               SET state='expired'
             WHERE state='active'
               AND start_expires_at IS NOT NULL
               AND start_expires_at <= ?
               AND deck_id IN (SELECT id FROM decks WHERE user_id=?)""",
            (iso(now()), user["id"]),
        )
    with connect() as db:
        deck = db.execute(
            "SELECT * FROM decks WHERE user_id=? AND local_date=?",
            (user["id"], local_day.isoformat()),
        ).fetchone()

    if deck is None:
        return DeckOut(
            local_date=local_day,
            refreshed=False,
            refresh_available=False,
            quests=[],
        )

    with connect() as db:
        rows = db.execute(
            "SELECT * FROM quests WHERE deck_id=? AND state != 'superseded' ORDER BY slot",
            (deck["id"],),
        ).fetchall()
    return DeckOut(
        local_date=local_day,
        refreshed=bool(deck["refreshed_at"]),
        refresh_available=not bool(deck["refreshed_at"])
        and any(r["state"] == "offered" for r in rows),
        quests=[quest_out(r) for r in rows],
    )


def regenerate_today_deck(user: sqlite3.Row, profile: sqlite3.Row) -> DeckOut:
    """Generate and persist a fresh five-quest deck for the current local day."""
    local_day, zone = local_context(user)
    with connect() as db:
        current_rows = db.execute(
            """SELECT title, place_provider_id
               FROM quests q
               JOIN decks d ON d.id=q.deck_id
              WHERE d.user_id=? AND d.local_date=? AND q.state != 'superseded'""",
            (user["id"], local_day.isoformat()),
        ).fetchall()
    exclude_titles = [row["title"] for row in current_rows]
    exclude_provider_ids = [
        row["place_provider_id"] for row in current_rows if row["place_provider_id"]
    ]
    quests = generate_quests_for_profile(
        profile,
        count=DECK_SIZE,
        exclude_titles=exclude_titles,
        exclude_provider_ids=exclude_provider_ids,
    )
    if len(quests) < DECK_SIZE:
        raise HTTPException(503, "Could not build a full quest deck")

    expiry = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(
        UTC
    )
    with transaction() as db:
        deck = db.execute(
            "SELECT * FROM decks WHERE user_id=? AND local_date=?",
            (user["id"], local_day.isoformat()),
        ).fetchone()
        if deck is None:
            cursor = db.execute(
                "INSERT INTO decks(user_id,local_date,created_at) VALUES(?,?,?)",
                (user["id"], local_day.isoformat(), iso(now())),
            )
            deck_id = cursor.lastrowid
        else:
            deck_id = deck["id"]
            # Preserve old rows for history, but hide them from the active deck.
            db.execute(
                """UPDATE quests
                   SET state='superseded', superseded_at=?, slot=-rowid
                 WHERE deck_id=?""",
                (iso(now()), deck_id),
            )
            db.execute(
                "UPDATE decks SET refreshed_at=NULL, created_at=? WHERE id=?",
                (iso(now()), deck_id),
            )
        for slot, quest in enumerate(quests, 1):
            insert_quest_row(db, deck_id=deck_id, slot=slot, quest=quest, expiry=expiry)

    return deck_for(user, profile)


def progress_payload(user_id: int, zone: ZoneInfo) -> dict:
    with connect() as db:
        total = db.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM xp_ledger WHERE user_id=?",
            (user_id,),
        ).fetchone()["total"]
        cats = db.execute(
            "SELECT category,SUM(amount) amount FROM xp_ledger WHERE user_id=? GROUP BY category",
            (user_id,),
        ).fetchall()
        dates = [
            date.fromisoformat(r["local_date"])
            for r in db.execute(
                "SELECT local_date FROM active_dates WHERE user_id=?", (user_id,)
            ).fetchall()
        ]
    today = now().astimezone(zone).date()
    streak = 0
    cursor = today
    active = set(dates)
    if cursor not in active:
        cursor -= timedelta(days=1)
    while cursor in active:
        streak += 1
        cursor -= timedelta(days=1)
    level = 1
    while total >= round(100 * level**1.5):
        level += 1
    return {
        "total_xp": total,
        "level": level,
        "next_level_xp": round(100 * level**1.5),
        "streak": streak,
        "categories": {r["category"]: r["amount"] for r in cats},
    }


@app.on_event("startup")
def startup() -> None:
    initialize()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "persistence": "sqlite"}


@app.get("/v1/profile")
def profile(user: sqlite3.Row = Depends(current_player)) -> dict:
    with connect() as db:
        p = db.execute(
            "SELECT * FROM profiles WHERE user_id=?", (user["id"],)
        ).fetchone()
    home_zone = None
    if p["home_cell"]:
        try:
            center = home_center(p)
        except HTTPException:
            center = None
        home_zone = {
            "city": p["home_city"],
            "address": p["home_address"],
            "source": p["home_source"] or "address",
            "h3_cell": p["home_cell"],
            "center": {
                "latitude": center[0],
                "longitude": center[1],
            }
            if center
            else None,
        }
    canonical = profile_preferences(p)
    return {
        "timezone": user["timezone"],
        "movement_intensity": profile_str(p, "movement_intensity", "gentle"),
        "interest_preferences": {
            item.topic.value: item.affinity.value
            for item in canonical.interest_preferences
        },
        "custom_interests": [item.label for item in canonical.custom_interests],
        "max_one_way_distance_metres": canonical.max_one_way_distance_metres,
        "preference_version": canonical.preference_version,
        "home_zone": home_zone,
    }


@app.patch("/v1/profile")
def update_profile(
    body: ProfilePatch, user: sqlite3.Row = Depends(current_player)
) -> dict:
    """Partial update: only provided fields are written."""
    with transaction() as db:
        current = db.execute(
            "SELECT * FROM profiles WHERE user_id=?", (user["id"],)
        ).fetchone()
        if not current:
            raise HTTPException(404, "Profile not found")

        intensity = (
            body.movement_intensity
            if body.movement_intensity is not None
            else current["movement_intensity"]
        )
        fields_set = body.model_fields_set
        existing_preferences = profile_preferences(current)
        if (
            body.interest_preferences is not None
            and body.interest_preferences
            and all(
                affinity == InterestAffinity.avoid
                for affinity in body.interest_preferences.values()
            )
        ):
            raise HTTPException(422, "Keep at least one interest available")
        canonical_fields = {
            "interest_preferences",
            "custom_interests",
            "max_one_way_distance_metres",
        }
        canonical = ProfilePreferences(
            interest_preferences=(
                [
                    InterestPreference(topic=topic, affinity=affinity)
                    for topic, affinity in body.interest_preferences.items()
                ]
                if body.interest_preferences is not None
                else existing_preferences.interest_preferences
            ),
            custom_interests=(
                [
                    {"label": label.strip(), "affinity": "love"}
                    for label in body.custom_interests
                    if label.strip()
                ]
                if body.custom_interests is not None
                else existing_preferences.custom_interests
            ),
            max_one_way_distance_metres=(
                body.max_one_way_distance_metres
                or existing_preferences.max_one_way_distance_metres
            ),
            preference_version=existing_preferences.preference_version,
        )
        canonical_changed = bool(
            fields_set & canonical_fields
        ) and canonical.model_dump(
            exclude={"preference_version"}
        ) != existing_preferences.model_dump(exclude={"preference_version"})
        if canonical_changed:
            canonical = canonical.model_copy(
                update={
                    "preference_version": existing_preferences.preference_version + 1
                }
            )

        db.execute(
            """UPDATE profiles SET
                movement_intensity=?,
                interest_preferences_json=?,custom_interests_json=?,
                max_one_way_distance_metres=?,
                preference_version=?,updated_at=?
            WHERE user_id=?""",
            (
                intensity or "gentle",
                json.dumps(
                    [
                        item.model_dump(mode="json")
                        for item in canonical.interest_preferences
                    ]
                ),
                json.dumps(
                    [
                        item.model_dump(mode="json")
                        for item in canonical.custom_interests
                    ]
                ),
                canonical.max_one_way_distance_metres,
                canonical.preference_version,
                iso(now()),
                user["id"],
            ),
        )
    return profile(user)


@app.put("/v1/profile/home-zone")
def set_home_zone(
    body: HomeZoneRequest, user: sqlite3.Row = Depends(current_player)
) -> dict:
    city = body.city.strip()
    h3_cell = h3.latlng_to_cell(body.latitude, body.longitude, 7)
    with transaction() as db:
        db.execute(
            "UPDATE profiles SET home_city=?,home_cell=?,home_latitude=?,home_longitude=?,home_address=?,home_source=?,updated_at=? WHERE user_id=?",
            (
                city,
                h3_cell,
                body.latitude,
                body.longitude,
                body.address.strip(),
                body.source,
                iso(now()),
                user["id"],
            ),
        )
    return {
        "city": city,
        "address": body.address.strip(),
        "source": body.source,
        "h3_cell": h3_cell,
        "center": {"latitude": body.latitude, "longitude": body.longitude},
    }


@app.get("/v1/map/areas")
def map_areas(
    q: Annotated[str, Query(min_length=2, max_length=150)],
    city: Annotated[str | None, Query(max_length=100)] = None,
    user: sqlite3.Row = Depends(current_player),
) -> dict:
    del user
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(422, "q must contain at least 2 characters")
    normalized_city = city.strip() if city and city.strip() else None
    return {"areas": OpenStreetMapPlaceProvider().areas(query, normalized_city)}


@app.post("/v1/routes/preview")
def preview_route(
    body: RoutePreviewRequest,
    user: sqlite3.Row = Depends(current_player),
) -> dict[str, object]:
    """Return a Google Routes preview without logging player coordinates."""
    require_ready(user)
    try:
        route = RouteProvider().route(
            (body.origin.latitude, body.origin.longitude),
            (body.destination.latitude, body.destination.longitude),
            travel_mode=body.travel_mode,
        )
    except RouteConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except RouteServiceError as exc:
        raise HTTPException(
            503, "Live route previews are temporarily unavailable"
        ) from exc
    if route is None:
        raise HTTPException(404, "No route was found for that travel mode")
    return {
        "travel_mode": route.travel_mode,
        "distance_meters": route.distance_metres,
        "duration_seconds": route.duration_seconds,
        "encoded_polyline": route.encoded_polyline,
    }


@app.get("/v1/discover")
def discover_city(
    food_query: Annotated[str | None, Query(max_length=100)] = None,
    user: sqlite3.Row = Depends(current_player),
) -> dict[str, object]:
    """Return selected-interest places within the saved home-radius and food venues."""
    profile = require_ready(user)
    city = profile["home_city"]
    if not isinstance(city, str) or not city.strip():
        raise HTTPException(409, "Set a home city before discovering places")
    query = food_query.strip() if food_query else None
    preferences = profile_preferences(profile)
    try:
        return CityDiscoveryProvider().discover(
            city=city.strip(),
            center=home_center(profile),
            interests=discovery_interests_for_profile(profile),
            radius_metres=preferences.max_one_way_distance_metres,
            food_query=query,
        )
    except PlaceDiscoveryUnavailable as exc:
        raise HTTPException(
            503,
            "OpenStreetMap place discovery is temporarily unavailable. Please retry.",
        ) from exc


@app.get("/v1/decks/today", response_model=DeckOut)
def today_deck(user: sqlite3.Row = Depends(current_player)) -> DeckOut:
    return deck_for(user, require_ready(user))


@app.post("/v1/decks/today/generate", response_model=DeckOut)
def generate_today_deck(user: sqlite3.Row = Depends(current_player)) -> DeckOut:
    started_at = time_module.monotonic()
    try:
        return regenerate_today_deck(user, require_ready(user))
    except HTTPException as exc:
        logger.warning(
            "Today deck generation failed: status=%s detail=%s elapsed_ms=%s",
            exc.status_code,
            exc.detail,
            round((time_module.monotonic() - started_at) * 1000),
        )
        raise


@app.post(
    "/v1/decks/today/reconcile-preferences",
    response_model=DeckOut,
)
def reconcile_today_deck_preferences(
    user: sqlite3.Row = Depends(current_player),
) -> DeckOut:
    """Replace only offered quests after canonical preferences are saved.

    Generation happens before the transaction. If routing or generation fails,
    the stored deck is untouched. Started, completed, skipped, and expired quests
    remain exactly as they were.
    """
    profile = require_ready(user)
    local_day, zone = local_context(user)
    preference_version = profile_preferences(profile).preference_version
    with connect() as db:
        stored = db.execute(
            "SELECT * FROM decks WHERE user_id=? AND local_date=?",
            (user["id"], local_day.isoformat()),
        ).fetchone()
        if stored is not None:
            offered = db.execute(
                """SELECT * FROM quests
                   WHERE deck_id=? AND state='offered'
                   ORDER BY slot""",
                (stored["id"],),
            ).fetchall()
            current_rows = db.execute(
                """SELECT title, place_provider_id FROM quests
                   WHERE deck_id=? AND state != 'superseded'""",
                (stored["id"],),
            ).fetchall()
        else:
            offered = []
            current_rows = []
    if stored is None:
        return deck_for(user, profile)
    if not offered:
        return deck_for(user, profile)

    replacements = generate_quests_for_profile(
        profile,
        count=len(offered),
        exclude_titles=[row["title"] for row in current_rows],
        exclude_provider_ids=[
            row["place_provider_id"] for row in current_rows if row["place_provider_id"]
        ],
    )
    if len(replacements) != len(offered):
        raise HTTPException(503, "Could not update all offered quests")

    expected_ids = [row["id"] for row in offered]
    expiry = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(
        UTC
    )
    with transaction() as db:
        current_profile = db.execute(
            "SELECT * FROM profiles WHERE user_id=?", (user["id"],)
        ).fetchone()
        current_deck = db.execute(
            "SELECT * FROM decks WHERE user_id=? AND local_date=?",
            (user["id"], local_day.isoformat()),
        ).fetchone()
        if (
            current_profile is None
            or current_deck is None
            or current_deck["id"] != stored["id"]
            or profile_preferences(current_profile).preference_version
            != preference_version
        ):
            raise HTTPException(
                409, "Preferences or today’s deck changed. Please try again."
            )
        current_offered = db.execute(
            """SELECT * FROM quests
               WHERE deck_id=? AND state='offered'
               ORDER BY slot""",
            (stored["id"],),
        ).fetchall()
        if [row["id"] for row in current_offered] != expected_ids:
            raise HTTPException(
                409, "Today’s offered quests changed. Please try again."
            )
        slots = [row["slot"] for row in current_offered]
        for row in current_offered:
            db.execute(
                """UPDATE quests
                   SET state='superseded', superseded_at=?, slot=-rowid
                   WHERE id=? AND state='offered'""",
                (iso(now()), row["id"]),
            )
        for slot, quest in zip(slots, replacements, strict=True):
            insert_quest_row(
                db,
                deck_id=stored["id"],
                slot=slot,
                quest=quest,
                expiry=expiry,
            )
    return deck_for(user, current_profile)


@app.post("/v1/decks/today/refresh", response_model=DeckOut)
def refresh_deck(user: sqlite3.Row = Depends(current_player)) -> DeckOut:
    profile = require_ready(user)
    local_day, zone = local_context(user)
    with connect() as db:
        stored = db.execute(
            "SELECT * FROM decks WHERE user_id=? AND local_date=?",
            (user["id"], local_day.isoformat()),
        ).fetchone()
        if stored is None:
            raise HTTPException(409, "Generate today’s quests before refreshing")
        if stored["refreshed_at"]:
            raise HTTPException(409, "Daily refresh already used")
        offered = db.execute(
            "SELECT * FROM quests WHERE deck_id=? AND state='offered'", (stored["id"],)
        ).fetchall()
        if not offered:
            raise HTTPException(409, "No quests available to refresh")

    # Exclude all current deck titles and provider bindings for refresh diversity.
    with connect() as db:
        all_rows = db.execute(
            "SELECT * FROM quests WHERE deck_id=? AND state != 'superseded'",
            (stored["id"],),
        ).fetchall()
    exclude_titles = [row["title"] for row in all_rows]
    exclude_provider_ids = [
        row["place_provider_id"] for row in all_rows if row["place_provider_id"]
    ]
    replacements = generate_quests_for_profile(
        profile,
        count=len(offered),
        exclude_titles=exclude_titles,
        exclude_provider_ids=exclude_provider_ids,
    )
    if len(replacements) < len(offered):
        raise HTTPException(503, "Could not refresh quests right now")

    expiry = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(
        UTC
    )
    with transaction() as db:
        # Re-check refresh eligibility inside the write transaction.
        stored = db.execute(
            "SELECT * FROM decks WHERE user_id=? AND local_date=?",
            (user["id"], local_day.isoformat()),
        ).fetchone()
        if stored["refreshed_at"]:
            raise HTTPException(409, "Daily refresh already used")
        offered = db.execute(
            "SELECT * FROM quests WHERE deck_id=? AND state='offered' ORDER BY slot",
            (stored["id"],),
        ).fetchall()
        if not offered:
            raise HTTPException(409, "No quests available to refresh")
        for old, quest in zip(offered, replacements, strict=False):
            db.execute(
                """UPDATE quests SET
                    id=?, title=?, description=?, category=?, difficulty=?, base_xp=?,
                    place_name=?, latitude=?, longitude=?,
                    time_window_start=NULL, time_window_end=NULL,
                    expires_at=?, state='offered', superseded_at=NULL,
                    place_provider_id=?, place_type=?, distance_meters=?,
                    distance_source=?, activity_type=?, topic=?,
                    match_reasons_json=?, preference_version=?
                WHERE id=?""",
                (
                    str(uuid.uuid4()),
                    quest.title,
                    quest.description,
                    quest.category.value,
                    quest.difficulty.value,
                    quest.base_xp,
                    quest.place_name,
                    quest.latitude,
                    quest.longitude,
                    iso(expiry),
                    quest.place_provider_id,
                    quest.place_type,
                    quest.distance_meters,
                    quest.distance_source.value,
                    quest.activity_type,
                    quest.topic.value if quest.topic else None,
                    json.dumps(quest.match_reasons),
                    quest.preference_version,
                    old["id"],
                ),
            )
        db.execute(
            "UPDATE decks SET refreshed_at=? WHERE id=?", (iso(now()), stored["id"])
        )
    return deck_for(user, profile)


@app.post("/v1/quests/{quest_id}/start", response_model=QuestOut)
def start_quest(quest_id: str, user: sqlite3.Row = Depends(current_player)) -> QuestOut:
    """Start a quest and give it an authoritative one-hour completion window."""
    with transaction() as db:
        row = db.execute(
            "SELECT q.* FROM quests q JOIN decks d ON d.id=q.deck_id WHERE q.id=? AND d.user_id=?",
            (quest_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Quest not found")
        if row["state"] == "active":
            if row["start_expires_at"] and parse_iso(row["start_expires_at"]) > now():
                return quest_out(row)
            db.execute("UPDATE quests SET state='expired' WHERE id=?", (quest_id,))
            raise HTTPException(409, "Quest timer has expired")
        if row["state"] != "offered":
            raise HTTPException(409, "Quest is not available to start")
        if parse_iso(row["expires_at"]) <= now():
            db.execute("UPDATE quests SET state='expired' WHERE id=?", (quest_id,))
            raise HTTPException(409, "Quest has expired")
        started_at = now()
        start_expires_at = started_at + timedelta(hours=1)
        db.execute(
            "UPDATE quests SET state='active',started_at=?,start_expires_at=? WHERE id=?",
            (iso(started_at), iso(start_expires_at), quest_id),
        )
        row = db.execute("SELECT * FROM quests WHERE id=?", (quest_id,)).fetchone()
    return quest_out(row)


@app.post("/v1/quests/{quest_id}/skip")
def skip_quest(quest_id: str, user: sqlite3.Row = Depends(current_player)) -> dict:
    with transaction() as db:
        row = db.execute(
            "SELECT q.* FROM quests q JOIN decks d ON d.id=q.deck_id WHERE q.id=? AND d.user_id=?",
            (quest_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Quest not found")
        if row["state"] not in {"offered", "active"}:
            raise HTTPException(409, "Quest is not available to skip")
        db.execute(
            "UPDATE quests SET state='skipped',skipped_at=? WHERE id=?",
            (iso(now()), quest_id),
        )
    return {"id": quest_id, "state": "skipped"}


@app.post("/v1/quests/{quest_id}/complete")
def complete_quest(
    quest_id: str,
    user: sqlite3.Row = Depends(current_player),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    if not idempotency_key or len(idempotency_key) > 255:
        raise HTTPException(400, "Idempotency-Key header required")
    endpoint = f"/v1/quests/{quest_id}/complete"
    request_hash = hashlib.sha256(quest_id.encode()).hexdigest()
    with transaction() as db:
        prior = db.execute(
            "SELECT * FROM idempotency_records WHERE user_id=? AND key=? AND endpoint=?",
            (user["id"], idempotency_key, endpoint),
        ).fetchone()
        if prior:
            if prior["request_hash"] != request_hash:
                raise HTTPException(
                    409, "Idempotency key reused with different request"
                )
            return json.loads(prior["response_json"])
        row = db.execute(
            "SELECT q.* FROM quests q JOIN decks d ON d.id=q.deck_id WHERE q.id=? AND d.user_id=?",
            (quest_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Quest not found")
        if row["state"] != "active":
            raise HTTPException(409, "Start the quest before completing it")
        if parse_iso(row["expires_at"]) <= now():
            db.execute("UPDATE quests SET state='expired' WHERE id=?", (quest_id,))
            raise HTTPException(409, "Quest has expired")
        if row["start_expires_at"] and parse_iso(row["start_expires_at"]) <= now():
            db.execute("UPDATE quests SET state='expired' WHERE id=?", (quest_id,))
            raise HTTPException(409, "Quest timer has expired")
        _, zone = local_context(user)
        local_now = now().astimezone(zone)
        multiplier = 1.0
        if (
            row["time_window_start"]
            and row["time_window_start"]
            <= local_now.strftime("%H:%M")
            <= row["time_window_end"]
        ):
            multiplier += 0.25
        # Streak bonus is based on consecutive prior local days, not total activity.
        streak = 0
        cursor = local_now.date() - timedelta(days=1)
        while db.execute(
            "SELECT 1 FROM active_dates WHERE user_id=? AND local_date=?",
            (user["id"], cursor.isoformat()),
        ).fetchone():
            streak += 1
            cursor -= timedelta(days=1)
        multiplier += min(streak, 5) * 0.05
        awarded = round(row["base_xp"] * multiplier)
        db.execute(
            "UPDATE quests SET state='completed',completed_at=? WHERE id=?",
            (iso(now()), quest_id),
        )
        db.execute(
            "INSERT INTO completions(quest_id,user_id,awarded_xp,multiplier,completed_at) VALUES(?,?,?,?,?)",
            (quest_id, user["id"], awarded, multiplier, iso(now())),
        )
        db.execute(
            "INSERT INTO xp_ledger(user_id,quest_id,category,amount,reason,created_at) VALUES(?,?,?,?,?,?)",
            (
                user["id"],
                quest_id,
                row["category"],
                awarded,
                "quest_completion",
                iso(now()),
            ),
        )
        db.execute(
            "INSERT OR IGNORE INTO active_dates(user_id,local_date) VALUES(?,?)",
            (user["id"], local_now.date().isoformat()),
        )
        result = {
            "quest": {
                **quest_out(row).model_dump(mode="json"),
                "state": "completed",
                "completed_at": iso(now()),
            },
            "awarded_xp": awarded,
            "multiplier": multiplier,
            "progress": progress_payload(user["id"], zone),
        }
        db.execute(
            "INSERT INTO idempotency_records(user_id,key,endpoint,request_hash,response_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                user["id"],
                idempotency_key,
                endpoint,
                request_hash,
                json.dumps(result),
                iso(now()),
            ),
        )
    return result


@app.get("/v1/progress")
def progress(user: sqlite3.Row = Depends(current_player)) -> dict:
    _, zone = local_context(user)
    return progress_payload(user["id"], zone)


@app.get("/v1/history", response_model=list[QuestOut])
def history(user: sqlite3.Row = Depends(current_player)) -> list[QuestOut]:
    with connect() as db:
        rows = db.execute(
            "SELECT q.* FROM quests q JOIN decks d ON d.id=q.deck_id WHERE d.user_id=? AND q.state='completed' ORDER BY q.completed_at DESC",
            (user["id"],),
        ).fetchall()
    return [quest_out(row) for row in rows]
