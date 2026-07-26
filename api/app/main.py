from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time as time_module
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from argon2 import PasswordHasher
import h3
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator

from .config import settings
from .database import connect, initialize, transaction
from .providers import (
    CityDiscoveryProvider,
    OpenStreetMapPlaceProvider,
    RouteConfigurationError,
    RouteProvider,
    RouteServiceError,
)
from .schemas.quests import (
    ActivityStyle,
    AccessibilityRequirements,
    ALL_CATEGORIES,
    ALLOWED_AVAILABLE_MINUTES,
    ALLOWED_MAX_WALKING_MINUTES,
    ALLOWED_MAX_TRAVEL_MINUTES,
    CATEGORY_TO_MOTIVATIONS,
    MAX_SEARCH_RADIUS_METRES,
    CostBand,
    EnvironmentPreference,
    GeneratedQuest,
    InterestAffinity,
    InterestPreference,
    InterestTopic,
    Motivation,
    MovementIntensity,
    PlaceCandidateIn,
    ProfilePreferences,
    QuestCategory,
    QuestGenerationRequest,
    QuestIntent,
    SocialComfort,
    TravelMode,
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
passwords = PasswordHasher()
DECK_SIZE = 5
MAX_GENERATION_BATCH = 5
GENERATION_CANDIDATE_SHORTLIST = 12


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def token(payload: dict) -> str:
    body = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(
        settings.secret_key.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return f"{body}.{signature}"


def read_token(value: str) -> dict:
    try:
        body, signature = value.split(".", 1)
        expected = hmac.new(
            settings.secret_key.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if datetime.fromtimestamp(payload["exp"], UTC) <= now():
            raise ValueError
        return payload
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid or expired access token"
        ) from exc


def issue_tokens(user_id: int, response: Response) -> str:
    access = token(
        {
            "sub": user_id,
            "exp": int(
                (now() + timedelta(minutes=settings.access_token_minutes)).timestamp()
            ),
        }
    )
    raw_refresh = secrets.token_urlsafe(48)
    session_id = str(uuid.uuid4())
    with transaction() as db:
        db.execute(
            "INSERT INTO sessions(id,user_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
            (
                session_id,
                user_id,
                hashlib.sha256(raw_refresh.encode()).hexdigest(),
                iso(now() + timedelta(days=settings.refresh_token_days)),
                iso(now()),
            ),
        )
    response.set_cookie(
        "refresh_token",
        f"{session_id}.{raw_refresh}",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.refresh_token_days * 86400,
    )
    return access


def local_context(user: sqlite3.Row) -> tuple[date, ZoneInfo]:
    try:
        zone = ZoneInfo(user["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(422, "Invalid account timezone") from exc
    return now().astimezone(zone).date(), zone


def require_user(authorization: Annotated[str | None, Header()] = None) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    claims = read_token(authorization.removeprefix("Bearer "))
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (claims["sub"],)).fetchone()
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_]+$")
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    birth_date: date
    timezone: str = "Asia/Kolkata"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Use an IANA timezone") from exc
        return value


class LoginRequest(BaseModel):
    username: str
    password: str


class ProfileAccessibilityPatch(BaseModel):
    step_free: bool = False
    wheelchair_access: bool = False
    max_walking_minutes: int | None = Field(default=None, ge=0, le=120)
    seating: bool = False
    low_sensory: bool = False
    notes: str | None = Field(default=None, max_length=500)


class ProfilePatch(BaseModel):
    """Partial profile update — omitted fields are left unchanged."""

    likes: list[str] | None = Field(default=None, max_length=20)
    dislikes: list[str] | None = Field(default=None, max_length=20)
    categories: list[str] | None = Field(default=None, max_length=8)
    motivations: list[str] | None = Field(default=None, max_length=4)
    available_minutes: int | None = Field(default=None)
    travel_modes: list[str] | None = Field(default=None, max_length=6)
    max_travel_minutes: int | None = Field(default=None)
    # Kept for older clients. New clients should use max_travel_minutes.
    max_walking_minutes: int | None = Field(default=None)
    movement_intensity: str | None = None
    budget: str | None = None
    social_comfort: str | None = None
    environment_preference: str | None = None
    accessibility_notes: str | None = Field(default=None, max_length=500)
    interest_preferences: dict[InterestTopic, InterestAffinity] | None = None
    custom_interests: list[str] | None = Field(default=None, max_length=20)
    primary_intent: QuestIntent | None = None
    secondary_intents: list[QuestIntent] | None = Field(default=None, max_length=4)
    activity_styles: list[ActivityStyle] | None = Field(default=None, max_length=7)
    primary_travel_mode: TravelMode | None = None
    fallback_travel_modes: list[TravelMode] | None = Field(default=None, max_length=4)
    total_time_minutes: int | None = Field(default=None, ge=10, le=480)
    max_one_way_travel_minutes: int | None = Field(default=None, ge=5, le=120)
    max_one_way_distance_metres: int | None = Field(default=None, ge=250, le=150_000)
    accessibility: ProfileAccessibilityPatch | None = None

    @field_validator("motivations")
    @classmethod
    def validate_motivations(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("Select at least one motivation")
        if len(value) > 4:
            raise ValueError("Select at most four motivations")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            try:
                motivation = Motivation(item)
            except ValueError as exc:
                raise ValueError(f"Invalid motivation: {item}") from exc
            if motivation.value not in seen:
                seen.add(motivation.value)
                normalized.append(motivation.value)
        return normalized

    @field_validator("available_minutes")
    @classmethod
    def validate_available_minutes(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value not in ALLOWED_AVAILABLE_MINUTES:
            raise ValueError("available_minutes must be 15, 30, 60, or 90")
        return value

    @field_validator("travel_modes")
    @classmethod
    def validate_travel_modes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("Select at least one travel mode")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            try:
                mode = TravelMode(item)
            except ValueError as exc:
                raise ValueError(f"Invalid travel mode: {item}") from exc
            if mode.value not in seen:
                seen.add(mode.value)
                normalized.append(mode.value)
        return normalized

    @field_validator("max_travel_minutes")
    @classmethod
    def validate_max_travel(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value not in ALLOWED_MAX_TRAVEL_MINUTES:
            raise ValueError("max_travel_minutes must be 10, 20, 40, 60, 90, or 120")
        return value

    @field_validator("max_walking_minutes")
    @classmethod
    def validate_max_walking(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value not in ALLOWED_MAX_WALKING_MINUTES:
            raise ValueError("max_walking_minutes must be 10, 20, 40, or 60")
        return value

    @field_validator("movement_intensity")
    @classmethod
    def validate_intensity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return MovementIntensity(value).value
        except ValueError as exc:
            raise ValueError("Invalid movement_intensity") from exc

    @field_validator("budget")
    @classmethod
    def validate_budget(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return CostBand(value).value
        except ValueError as exc:
            raise ValueError("Invalid budget") from exc

    @field_validator("social_comfort")
    @classmethod
    def validate_social(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return SocialComfort(value).value
        except ValueError as exc:
            raise ValueError("Invalid social_comfort") from exc

    @field_validator("environment_preference")
    @classmethod
    def validate_environment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return EnvironmentPreference(value).value
        except ValueError as exc:
            raise ValueError("Invalid environment_preference") from exc


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
    travel_mode: TravelMode


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
    walking_minutes: int | None = None
    distance_source: str | None = None
    estimated_activity_minutes: int | None = None
    cost_band: str | None = None
    activity_type: str | None = None
    topic: str | None = None
    intent: str | None = None
    activity_style: str | None = None
    travel_mode: str | None = None
    route_duration_minutes: int | None = None
    route_distance_meters: int | None = None
    total_estimated_minutes: int | None = None
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
        walking_minutes=data.get("walking_minutes"),
        distance_source=data.get("distance_source"),
        estimated_activity_minutes=data.get("estimated_activity_minutes"),
        cost_band=data.get("cost_band"),
        activity_type=data.get("activity_type"),
        topic=data.get("topic"),
        intent=data.get("intent"),
        activity_style=data.get("activity_style"),
        travel_mode=data.get("travel_mode"),
        route_duration_minutes=data.get("route_duration_minutes"),
        route_distance_meters=data.get("route_distance_meters"),
        total_estimated_minutes=data.get("total_estimated_minutes"),
        match_reasons=json.loads(data.get("match_reasons_json") or "[]"),
        preference_version=data.get("preference_version"),
    )


def profile_motivations(profile: sqlite3.Row) -> list[Motivation]:
    raw = profile["motivations_json"] if "motivations_json" in profile.keys() else None
    if raw:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            values = []
        result: list[Motivation] = []
        for item in values:
            try:
                result.append(Motivation(item))
            except ValueError:
                continue
        if result:
            return result[:4]
    # Derive from categories for legacy rows.
    categories = json.loads(profile["categories_json"] or "[]")
    result = []
    seen: set[str] = set()
    for category in categories:
        for motivation in CATEGORY_TO_MOTIVATIONS.get(str(category), []):
            if motivation.value not in seen:
                seen.add(motivation.value)
                result.append(motivation)
            if len(result) >= 4:
                return result
    return result or [Motivation.explore]


def profile_int(profile: sqlite3.Row, key: str, default: int) -> int:
    if key not in profile.keys() or profile[key] is None:
        return default
    return int(profile[key])


def profile_str(profile: sqlite3.Row, key: str, default: str) -> str:
    if key not in profile.keys() or not profile[key]:
        return default
    return str(profile[key])


def profile_travel_modes(profile: sqlite3.Row) -> list[TravelMode]:
    """Return saved modes, falling back to walking for legacy profiles."""
    raw = (
        profile["travel_modes_json"] if "travel_modes_json" in profile.keys() else None
    )
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        values = []
    modes: list[TravelMode] = []
    for value in values:
        try:
            mode = TravelMode(value)
        except ValueError:
            continue
        if mode not in modes:
            modes.append(mode)
    return modes or [TravelMode.walking]


def _json_value(raw: object, fallback: object) -> object:
    try:
        return json.loads(str(raw)) if raw else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def profile_preferences(profile: sqlite3.Row) -> ProfilePreferences:
    """Return the canonical preference contract after migration/backfill."""
    interest_raw = _json_value(profile["interest_preferences_json"], [])
    custom_raw = _json_value(profile["custom_interests_json"], [])
    accessibility_raw = _json_value(profile["accessibility_json"], {})
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
    if not isinstance(accessibility_raw, dict):
        accessibility_raw = {}
    activity_styles = _json_value(profile["activity_styles_json"], [])
    if not isinstance(activity_styles, list) or not activity_styles:
        activity_styles = ["wander"]
    primary_intent = profile_str(profile, "primary_intent", "explore")
    secondary_intents = _json_value(profile["secondary_intents_json"], [])
    if not isinstance(secondary_intents, list):
        secondary_intents = []
    # Older saves could retain the primary intent in the secondary list when
    # a user changed their primary choice. Normalize it before validation so a
    # later preference update can persist the corrected state.
    secondary_intents = [
        intent for intent in secondary_intents if intent != primary_intent
    ]
    return ProfilePreferences(
        interest_preferences=interest_raw,
        custom_interests=custom_raw,
        primary_intent=primary_intent,
        secondary_intents=secondary_intents,
        activity_styles=activity_styles,
        primary_travel_mode=profile_str(profile, "primary_travel_mode", "walking"),
        fallback_travel_modes=_json_value(profile["fallback_travel_modes_json"], []),
        total_time_minutes=profile_int(
            profile,
            "total_time_minutes",
            profile_int(profile, "available_minutes", 30),
        ),
        max_one_way_travel_minutes=profile_int(
            profile,
            "max_one_way_travel_minutes",
            profile_int(profile, "max_travel_minutes", 20),
        ),
        max_one_way_distance_metres=profile_int(
            profile, "max_one_way_distance_metres", 5_000
        ),
        accessibility=AccessibilityRequirements.model_validate(accessibility_raw),
        preference_version=profile_int(profile, "preference_version", 1),
    )


def require_ready(user: sqlite3.Row) -> sqlite3.Row:
    if not user["email_verified"]:
        raise HTTPException(403, "Verify email before generating quests")
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


def quest_categories_for_profile(profile: sqlite3.Row) -> list[QuestCategory]:
    """Derive generation categories from the canonical interest preferences."""
    selected: list[QuestCategory] = []
    for value in place_search_categories(profile, []):
        try:
            selected.append(QuestCategory(value))
        except ValueError:
            continue
    return selected or list(ALL_CATEGORIES)


_MOTIVATION_TO_PLACE_CATEGORIES: dict[str, list[str]] = {
    "explore": ["culture", "nature"],
    "move": ["fitness"],
    "learn": ["learning", "culture"],
    "create": ["creativity"],
    "reset": ["mindfulness"],
    "nature": ["nature"],
    "break_routine": ["culture", "nature", "mindfulness"],
}

_TOPIC_TO_PLACE_CATEGORIES: dict[InterestTopic, list[str]] = {
    InterestTopic.nature_outdoors: ["nature"],
    InterestTopic.history_heritage: ["culture"],
    InterestTopic.architecture_public_spaces: ["culture"],
    InterestTopic.art_design: ["creativity", "culture"],
    InterestTopic.books_learning: ["learning"],
    InterestTopic.local_culture_community: ["culture"],
    InterestTopic.food_markets: ["culture"],
    InterestTopic.music_performance: ["culture"],
}

_CATEGORY_TO_TOPICS: dict[str, list[InterestTopic]] = {
    "nature": [InterestTopic.nature_outdoors],
    "culture": [
        InterestTopic.history_heritage,
        InterestTopic.architecture_public_spaces,
        InterestTopic.local_culture_community,
        InterestTopic.food_markets,
        InterestTopic.music_performance,
    ],
    "creativity": [InterestTopic.art_design],
    "learning": [InterestTopic.books_learning],
    "fitness": [InterestTopic.nature_outdoors],
    "mindfulness": [InterestTopic.nature_outdoors],
}


def candidate_topics(category: str, place_type: str) -> list[InterestTopic]:
    """Classify a provider place into the smallest useful interest hierarchy."""
    by_type: dict[str, list[InterestTopic]] = {
        "museum": [InterestTopic.history_heritage],
        "monument": [InterestTopic.history_heritage],
        "memorial": [InterestTopic.history_heritage],
        "archaeological_site": [InterestTopic.history_heritage],
        "ruins": [InterestTopic.history_heritage],
        "castle": [
            InterestTopic.history_heritage,
            InterestTopic.architecture_public_spaces,
        ],
        "attraction": [
            InterestTopic.history_heritage,
            InterestTopic.local_culture_community,
        ],
        "place_of_worship": [
            InterestTopic.history_heritage,
            InterestTopic.architecture_public_spaces,
        ],
        "townhall": [
            InterestTopic.architecture_public_spaces,
            InterestTopic.local_culture_community,
        ],
        "square": [
            InterestTopic.architecture_public_spaces,
            InterestTopic.local_culture_community,
        ],
        "gallery": [InterestTopic.art_design],
        "artwork": [InterestTopic.art_design],
        "arts_centre": [InterestTopic.art_design],
        "library": [InterestTopic.books_learning],
        "college": [InterestTopic.books_learning],
        "university": [InterestTopic.books_learning],
        "community_centre": [InterestTopic.local_culture_community],
        "marketplace": [InterestTopic.food_markets],
        "theatre": [
            InterestTopic.music_performance,
            InterestTopic.local_culture_community,
        ],
        "music_venue": [InterestTopic.music_performance],
        "concert_hall": [InterestTopic.music_performance],
        "viewpoint": [InterestTopic.nature_outdoors],
    }
    return by_type.get(place_type, _CATEGORY_TO_TOPICS.get(category, []))


def place_search_categories(
    profile: sqlite3.Row, categories: list[QuestCategory]
) -> list[str]:
    """Map canonical non-avoided interests to OSM lookup categories."""
    del categories
    preferences = profile_preferences(profile)
    allowed_topics = [
        item.topic
        for item in preferences.interest_preferences
        if item.affinity != InterestAffinity.avoid
    ]
    values: list[str] = []
    for topic in allowed_topics:
        for category in _TOPIC_TO_PLACE_CATEGORIES[topic]:
            if category not in values:
                values.append(category)
    return values or ["nature", "culture", "creativity", "learning"]


def place_candidates_for(
    profile: sqlite3.Row,
    categories: list[QuestCategory],
) -> list[PlaceCandidateIn]:
    """Load OSM places and retain only verified routes allowed by the profile."""
    center = home_center(profile)
    preferences = profile_preferences(profile)
    category_values = place_search_categories(profile, categories)
    environment = profile_str(profile, "environment_preference", "either")
    provider = OpenStreetMapPlaceProvider()
    travel_modes = [
        preferences.primary_travel_mode,
        *preferences.fallback_travel_modes,
    ]
    has_vehicle_option = any(
        mode in {TravelMode.two_wheeler, TravelMode.four_wheeler}
        for mode in travel_modes
    )
    radius_metres = MAX_SEARCH_RADIUS_METRES
    if "culture" not in category_values or not has_vehicle_option:
        radius_metres = min(radius_metres, 50_000)
    else:
        # A vehicle-enabled two-hour preference should be able to surface
        # regional heritage sites, not only neighbourhood landmarks.
        radius_metres = min(
            radius_metres,
            max(
                50_000,
                preferences.max_one_way_travel_minutes * 1_250,
                preferences.max_one_way_distance_metres,
            ),
        )

    # Discovery covers the full configured area. Travel preferences remain
    # available to the LLM as profile context instead of filtering to walking.
    raw = provider.candidates(
        profile["home_city"],
        category_values,
        center,
        profile["home_cell"],
        radius_metres=radius_metres,
        max_walking_minutes=None,
        environment_preference=environment,
        accessibility_notes=profile["accessibility_notes"],
    )
    logger.info(
        "Quest candidate discovery completed: osm_candidates=%s travel_modes=%s",
        len(raw),
        [
            mode.value
            for mode in (
                preferences.primary_travel_mode,
                *preferences.fallback_travel_modes,
            )
        ],
    )

    destinations = [(item.latitude, item.longitude) for item in raw]
    routes = [None] * len(destinations)
    try:
        router = RouteProvider()
        for mode in travel_modes:
            logger.info(
                "Requesting quest travel-time matrix: mode=%s destinations=%s",
                mode.value,
                len(destinations),
            )
            mode_routes = router.matrix(
                center,
                destinations,
                travel_mode=mode.value,
            )
            for index, route in enumerate(mode_routes):
                if routes[index] is not None or route is None:
                    continue
                route_minutes = max(1, int((route.duration_seconds + 59) // 60))
                if route_minutes > preferences.max_one_way_travel_minutes:
                    continue
                if route.distance_metres > preferences.max_one_way_distance_metres:
                    continue
                if (
                    mode == TravelMode.walking
                    and preferences.accessibility.max_walking_minutes is not None
                    and route_minutes > preferences.accessibility.max_walking_minutes
                ):
                    continue
                routes[index] = route
    except RouteConfigurationError as exc:
        logger.warning("Quest routing is not configured: detail=%s", exc)
        raise HTTPException(503, str(exc)) from exc
    except RouteServiceError as exc:
        logger.warning("Quest routing provider failed: detail=%s", exc)
        raise HTTPException(
            503, "Live travel times are temporarily unavailable"
        ) from exc

    logger.info(
        "Quest travel-time matrix completed: routable_candidates=%s total_candidates=%s",
        sum(route is not None for route in routes),
        len(raw),
    )

    affinities = {
        item.topic: item.affinity for item in preferences.interest_preferences
    }
    results: list[PlaceCandidateIn] = []
    for item, route in zip(raw, routes, strict=True):
        if route is None:
            continue
        try:
            category = QuestCategory(item.category)
        except ValueError:
            continue
        topics = [
            topic
            for topic in candidate_topics(item.category, item.place_type)
            if affinities.get(topic, InterestAffinity.okay) != InterestAffinity.avoid
        ]
        if not topics:
            continue
        route_minutes = max(1, int((route.duration_seconds + 59) // 60))
        if (route_minutes * 2) + 5 > preferences.total_time_minutes:
            continue
        accessibility = preferences.accessibility
        if (
            accessibility.wheelchair_access or accessibility.step_free
        ) and item.wheelchair.value != "yes":
            continue
        if accessibility.seating_required and not any(
            feature.casefold() in {"benches", "seating"}
            for feature in item.verified_features
        ):
            continue
        results.append(
            PlaceCandidateIn(
                provider_id=item.provider_id,
                name=item.name,
                category=category,
                topics=topics,
                latitude=item.latitude,
                longitude=item.longitude,
                place_type=item.place_type,
                environment=item.environment,
                public_access=item.public_access,
                wheelchair=item.wheelchair,
                verified_features=item.verified_features,
                distance_metres=route.distance_metres,
                walking_minutes=route_minutes,
                distance_source="google_routes",
                landmark_rank=item.landmark_rank,
                travel_mode=route.travel_mode,
                route_duration_minutes=route_minutes,
                route_distance_metres=route.distance_metres,
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
            candidate.route_duration_minutes or 10_000,
            candidate.name.casefold(),
        )

    return sorted(results, key=candidate_score)


def generate_quests_for_profile(
    profile: sqlite3.Row,
    *,
    count: int,
    categories: list[QuestCategory],
    exclude_titles: list[str] | None = None,
    exclude_provider_ids: list[str] | None = None,
) -> list[GeneratedQuest]:
    """Generate up to 5 quests per OpenRouter call; batch when count > 5."""
    exclude_titles = list(exclude_titles or [])
    exclude_providers = set(exclude_provider_ids or [])
    canonical = profile_preferences(profile)
    logger.info(
        "Preparing quest deck generation: requested=%s quest_categories=%s preference_version=%s refresh=%s",
        count,
        [category.value for category in categories],
        canonical.preference_version,
        bool(exclude_titles or exclude_providers),
    )
    started_at = time_module.monotonic()
    try:
        candidates = place_candidates_for(profile, categories)
    except HTTPException as exc:
        logger.warning(
            "Quest deck candidate preparation failed: status=%s detail=%s elapsed_ms=%s",
            exc.status_code,
            exc.detail,
            round((time_module.monotonic() - started_at) * 1000),
        )
        raise
    logger.info("Found quest place candidates: count=%s", len(candidates))
    generation_categories = list(
        dict.fromkeys(candidate.category for candidate in candidates)
    )
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

    available_minutes = canonical.total_time_minutes
    max_travel = canonical.max_one_way_travel_minutes
    travel_modes = [
        canonical.primary_travel_mode,
        *canonical.fallback_travel_modes,
    ]
    try:
        intensity = MovementIntensity(
            profile_str(profile, "movement_intensity", "gentle")
        )
    except ValueError:
        intensity = MovementIntensity.gentle
    try:
        budget = CostBand(profile_str(profile, "budget", "free"))
    except ValueError:
        budget = CostBand.free
    try:
        social = SocialComfort(profile_str(profile, "social_comfort", "solo_only"))
    except ValueError:
        social = SocialComfort.solo_only
    try:
        environment = EnvironmentPreference(
            profile_str(profile, "environment_preference", "either")
        )
    except ValueError:
        environment = EnvironmentPreference.either

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
            categories=generation_categories,
            count=batch_size,
            motivations=profile_motivations(profile),
            likes=json.loads(profile["likes_json"] or "[]"),
            dislikes=json.loads(profile["dislikes_json"] or "[]"),
            available_minutes=available_minutes,
            travel_modes=travel_modes,
            max_travel_minutes=max_travel,
            # Preserve the old request field for older generation adapters.
            max_walking_minutes=max_travel,
            movement_intensity=intensity,
            budget=budget,
            social_comfort=social,
            environment_preference=environment,
            accessibility_notes=profile["accessibility_notes"],
            interest_preferences=canonical.interest_preferences,
            custom_interests=canonical.custom_interests,
            primary_intent=canonical.primary_intent,
            secondary_intents=canonical.secondary_intents,
            activity_styles=canonical.activity_styles,
            primary_travel_mode=canonical.primary_travel_mode,
            fallback_travel_modes=canonical.fallback_travel_modes,
            total_time_minutes=canonical.total_time_minutes,
            max_one_way_travel_minutes=canonical.max_one_way_travel_minutes,
            accessibility=canonical.accessibility,
            preference_version=canonical.preference_version,
            place_candidates=batch_candidates,
            exclude_titles=exclude_titles + [q.title for q in generated],
            exclude_candidate_ids=[],
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
            place_provider_id,place_type,distance_meters,walking_minutes,
            distance_source,estimated_activity_minutes,cost_band,activity_type,
            topic,intent,activity_style,travel_mode,route_duration_minutes,
            route_distance_meters,total_estimated_minutes,match_reasons_json,
            preference_version
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            quest.walking_minutes,
            quest.distance_source.value,
            quest.estimated_activity_minutes,
            quest.cost_band.value,
            quest.activity_type,
            quest.topic.value if quest.topic else None,
            quest.intent.value if quest.intent else None,
            quest.activity_style.value if quest.activity_style else None,
            quest.travel_mode.value if quest.travel_mode else None,
            quest.route_duration_minutes,
            quest.route_distance_meters,
            quest.total_estimated_minutes,
            json.dumps(quest.match_reasons),
            quest.preference_version,
        ),
    )


def deck_for(user: sqlite3.Row, profile: sqlite3.Row) -> DeckOut:
    local_day, zone = local_context(user)
    categories = quest_categories_for_profile(profile)
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
        quests = generate_quests_for_profile(
            profile, count=DECK_SIZE, categories=categories
        )
        if len(quests) < DECK_SIZE:
            raise HTTPException(503, "Could not build a full quest deck")
        try:
            with transaction() as db:
                cursor = db.execute(
                    "INSERT INTO decks(user_id,local_date,created_at) VALUES(?,?,?)",
                    (user["id"], local_day.isoformat(), iso(now())),
                )
                deck_id = cursor.lastrowid
                expiry = datetime.combine(
                    local_day + timedelta(days=1), time.min, zone
                ).astimezone(UTC)
                for slot, quest in enumerate(quests, 1):
                    insert_quest_row(
                        db, deck_id=deck_id, slot=slot, quest=quest, expiry=expiry
                    )
                deck = db.execute(
                    "SELECT * FROM decks WHERE id=?", (deck_id,)
                ).fetchone()
        except sqlite3.IntegrityError:
            # Concurrent create for the same local day won the race.
            with connect() as db:
                deck = db.execute(
                    "SELECT * FROM decks WHERE user_id=? AND local_date=?",
                    (user["id"], local_day.isoformat()),
                ).fetchone()
            if deck is None:
                raise HTTPException(503, "Could not build a full quest deck")

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
    categories = quest_categories_for_profile(profile)
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
        categories=categories,
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


@app.post("/v1/auth/register", status_code=201)
def register(body: RegisterRequest) -> dict:
    today = date.today()
    age = (
        today.year
        - body.birth_date.year
        - ((today.month, today.day) < (body.birth_date.month, body.birth_date.day))
    )
    if age < 18:
        raise HTTPException(422, "You must be at least 18")
    try:
        with transaction() as db:
            result = db.execute(
                "INSERT INTO users(username,email,password_hash,birth_date,timezone,created_at) VALUES(?,?,?,?,?,?)",
                (
                    body.username,
                    str(body.email),
                    passwords.hash(body.password),
                    body.birth_date.isoformat(),
                    body.timezone,
                    iso(now()),
                ),
            )
            db.execute(
                """INSERT INTO profiles(
                    user_id, motivations_json, available_minutes, travel_modes_json,
                    max_travel_minutes, max_walking_minutes,
                    movement_intensity, budget, social_comfort, environment_preference,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.lastrowid,
                    json.dumps(["explore"]),
                    30,
                    json.dumps(["walking"]),
                    20,
                    20,
                    "gentle",
                    "free",
                    "solo_only",
                    "either",
                    iso(now()),
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Username or email already exists") from exc
    return {"message": "Account created. Verify email to unlock quests."}


@app.post("/v1/auth/verify-email")
def verify_email(user: sqlite3.Row = Depends(require_user)) -> dict:
    # Delivery/token validation is delegated to an email provider in production.
    with transaction() as db:
        db.execute("UPDATE users SET email_verified=1 WHERE id=?", (user["id"],))
    return {"verified": True}


@app.post("/v1/auth/login")
def login(body: LoginRequest, response: Response) -> dict:
    with connect() as db:
        user = db.execute(
            "SELECT * FROM users WHERE username=?", (body.username,)
        ).fetchone()
    try:
        valid = user is not None and passwords.verify(
            user["password_hash"], body.password
        )
    except Exception:
        valid = False
    if not valid:
        raise HTTPException(401, "Invalid username or password")
    return {"access_token": issue_tokens(user["id"], response), "token_type": "bearer"}


@app.post("/v1/auth/dev-session")
def dev_session(response: Response) -> dict:
    """Mint a local explorer session when DETOUR_AUTH_DISABLED=true.

    Does not serve dummy quests — decks still come from OpenRouter generation.
    """
    if not settings.auth_disabled:
        raise HTTPException(404, "Not found")
    username = "explorer"
    email = "local@detour.dev"
    with transaction() as db:
        user = db.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        if not user:
            result = db.execute(
                "INSERT INTO users(username,email,password_hash,birth_date,timezone,email_verified,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    username,
                    email,
                    passwords.hash(secrets.token_urlsafe(24)),
                    "1990-01-01",
                    "Asia/Kolkata",
                    1,
                    iso(now()),
                ),
            )
            user_id = result.lastrowid
            db.execute(
                """INSERT INTO profiles(
                    user_id, motivations_json, available_minutes, travel_modes_json,
                    max_travel_minutes, max_walking_minutes,
                    movement_intensity, budget, social_comfort, environment_preference,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    json.dumps(["explore"]),
                    30,
                    json.dumps(["walking"]),
                    20,
                    20,
                    "gentle",
                    "free",
                    "solo_only",
                    "either",
                    iso(now()),
                ),
            )
        else:
            user_id = user["id"]
            db.execute("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))
    return {
        "access_token": issue_tokens(user_id, response),
        "token_type": "bearer",
    }


@app.post("/v1/auth/refresh")
def refresh(
    response: Response, refresh_token: Annotated[str | None, Cookie()] = None
) -> dict:
    if not refresh_token or "." not in refresh_token:
        raise HTTPException(401, "Refresh token required")
    session_id, raw = refresh_token.split(".", 1)
    with transaction() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if (
            not row
            or row["revoked_at"]
            or parse_iso(row["expires_at"]) <= now()
            or not hmac.compare_digest(
                row["token_hash"], hashlib.sha256(raw.encode()).hexdigest()
            )
        ):
            raise HTTPException(401, "Invalid refresh token")
        db.execute(
            "UPDATE sessions SET revoked_at=? WHERE id=?", (iso(now()), session_id)
        )
    return {
        "access_token": issue_tokens(row["user_id"], response),
        "token_type": "bearer",
    }


@app.post("/v1/auth/logout", status_code=204)
def logout(
    response: Response, refresh_token: Annotated[str | None, Cookie()] = None
) -> Response:
    if refresh_token and "." in refresh_token:
        session_id = refresh_token.split(".", 1)[0]
        with transaction() as db:
            db.execute(
                "UPDATE sessions SET revoked_at=? WHERE id=?", (iso(now()), session_id)
            )
    response.delete_cookie(
        "refresh_token", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
    return response


@app.get("/v1/profile")
def profile(user: sqlite3.Row = Depends(require_user)) -> dict:
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
        "username": user["username"],
        "email": user["email"],
        "email_verified": bool(user["email_verified"]),
        "timezone": user["timezone"],
        "likes": json.loads(p["likes_json"] or "[]"),
        "dislikes": json.loads(p["dislikes_json"] or "[]"),
        "categories": json.loads(p["categories_json"] or "[]"),
        "motivations": [m.value for m in profile_motivations(p)],
        "available_minutes": profile_int(p, "available_minutes", 30),
        "travel_modes": [mode.value for mode in profile_travel_modes(p)],
        "max_travel_minutes": profile_int(
            p, "max_travel_minutes", profile_int(p, "max_walking_minutes", 20)
        ),
        # Compatibility for clients that have not adopted travel modes yet.
        "max_walking_minutes": profile_int(p, "max_walking_minutes", 20),
        "movement_intensity": profile_str(p, "movement_intensity", "gentle"),
        "budget": profile_str(p, "budget", "free"),
        "social_comfort": profile_str(p, "social_comfort", "solo_only"),
        "environment_preference": profile_str(p, "environment_preference", "either"),
        "accessibility_notes": p["accessibility_notes"],
        "interest_preferences": {
            item.topic.value: item.affinity.value
            for item in canonical.interest_preferences
        },
        "custom_interests": [item.label for item in canonical.custom_interests],
        "primary_intent": canonical.primary_intent.value,
        "secondary_intents": [item.value for item in canonical.secondary_intents],
        "activity_styles": [item.value for item in canonical.activity_styles],
        "primary_travel_mode": canonical.primary_travel_mode.value,
        "fallback_travel_modes": [
            item.value for item in canonical.fallback_travel_modes
        ],
        "total_time_minutes": canonical.total_time_minutes,
        "max_one_way_travel_minutes": canonical.max_one_way_travel_minutes,
        "max_one_way_distance_metres": canonical.max_one_way_distance_metres,
        "accessibility": {
            "step_free": canonical.accessibility.step_free,
            "wheelchair_access": canonical.accessibility.wheelchair_access,
            "max_walking_minutes": canonical.accessibility.max_walking_minutes,
            "seating": canonical.accessibility.seating_required,
            "low_sensory": canonical.accessibility.low_sensory,
            "notes": canonical.accessibility.notes,
        },
        "preference_version": canonical.preference_version,
        "home_zone": home_zone,
    }


@app.patch("/v1/profile")
def update_profile(
    body: ProfilePatch, user: sqlite3.Row = Depends(require_user)
) -> dict:
    """Partial update: only provided fields are written."""
    with transaction() as db:
        current = db.execute(
            "SELECT * FROM profiles WHERE user_id=?", (user["id"],)
        ).fetchone()
        if not current:
            raise HTTPException(404, "Profile not found")

        likes = (
            json.dumps(body.likes) if body.likes is not None else current["likes_json"]
        )
        dislikes = (
            json.dumps(body.dislikes)
            if body.dislikes is not None
            else current["dislikes_json"]
        )
        categories = (
            json.dumps(body.categories)
            if body.categories is not None
            else current["categories_json"]
        )
        if body.motivations is not None:
            motivations = json.dumps(body.motivations)
        elif current["motivations_json"]:
            motivations = current["motivations_json"]
        else:
            motivations = json.dumps([m.value for m in profile_motivations(current)])
        available = (
            body.available_minutes
            if body.available_minutes is not None
            else current["available_minutes"]
        )
        travel_modes = (
            json.dumps(body.travel_modes)
            if body.travel_modes is not None
            else current["travel_modes_json"] or json.dumps(["walking"])
        )
        max_travel = (
            body.max_travel_minutes
            if body.max_travel_minutes is not None
            else current["max_travel_minutes"]
            or body.max_walking_minutes
            or current["max_walking_minutes"]
            or 20
        )
        max_walking = (
            body.max_walking_minutes
            if body.max_walking_minutes is not None
            else current["max_walking_minutes"] or max_travel
        )
        intensity = (
            body.movement_intensity
            if body.movement_intensity is not None
            else current["movement_intensity"]
        )
        budget = body.budget if body.budget is not None else current["budget"]
        social = (
            body.social_comfort
            if body.social_comfort is not None
            else current["social_comfort"]
        )
        environment = (
            body.environment_preference
            if body.environment_preference is not None
            else current["environment_preference"]
        )
        # accessibility_notes: explicit null clears; omit leaves unchanged.
        fields_set = body.model_fields_set
        if "accessibility_notes" in fields_set:
            accessibility = body.accessibility_notes
        else:
            accessibility = current["accessibility_notes"]

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
        if body.activity_styles is not None and not body.activity_styles:
            raise HTTPException(422, "Select at least one activity style")
        canonical_fields = {
            "interest_preferences",
            "custom_interests",
            "primary_intent",
            "secondary_intents",
            "activity_styles",
            "primary_travel_mode",
            "fallback_travel_modes",
            "total_time_minutes",
            "max_one_way_travel_minutes",
            "max_one_way_distance_metres",
            "accessibility",
        }
        canonical_accessibility = (
            AccessibilityRequirements(
                step_free=body.accessibility.step_free,
                wheelchair_access=body.accessibility.wheelchair_access,
                max_walking_minutes=body.accessibility.max_walking_minutes,
                seating_required=body.accessibility.seating,
                low_sensory=body.accessibility.low_sensory,
                notes=body.accessibility.notes,
            )
            if body.accessibility is not None
            else existing_preferences.accessibility
        )
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
            primary_intent=body.primary_intent or existing_preferences.primary_intent,
            secondary_intents=(
                body.secondary_intents
                if body.secondary_intents is not None
                else existing_preferences.secondary_intents
            ),
            activity_styles=(
                body.activity_styles
                if body.activity_styles is not None
                else existing_preferences.activity_styles
            ),
            primary_travel_mode=(
                body.primary_travel_mode or existing_preferences.primary_travel_mode
            ),
            fallback_travel_modes=(
                body.fallback_travel_modes
                if body.fallback_travel_modes is not None
                else existing_preferences.fallback_travel_modes
            ),
            total_time_minutes=(
                body.total_time_minutes or existing_preferences.total_time_minutes
            ),
            max_one_way_travel_minutes=(
                body.max_one_way_travel_minutes
                or existing_preferences.max_one_way_travel_minutes
            ),
            max_one_way_distance_metres=(
                body.max_one_way_distance_metres
                or existing_preferences.max_one_way_distance_metres
            ),
            accessibility=canonical_accessibility,
            preference_version=existing_preferences.preference_version,
        )
        canonical_changed = bool(
            fields_set & canonical_fields
        ) and canonical.model_dump(
            exclude={"preference_version"}
        ) != existing_preferences.model_dump(exclude={"preference_version"})
        generation_constraint_changed = any(
            (
                body.budget is not None and body.budget != current["budget"],
                body.social_comfort is not None
                and body.social_comfort != current["social_comfort"],
                body.environment_preference is not None
                and body.environment_preference != current["environment_preference"],
            )
        )
        if canonical_changed or generation_constraint_changed:
            canonical = canonical.model_copy(
                update={
                    "preference_version": existing_preferences.preference_version + 1
                }
            )

        db.execute(
            """UPDATE profiles SET
                likes_json=?, dislikes_json=?, categories_json=?, motivations_json=?,
                available_minutes=?, travel_modes_json=?, max_travel_minutes=?,
                max_walking_minutes=?, movement_intensity=?,
                budget=?, social_comfort=?, environment_preference=?,
                accessibility_notes=?,
                interest_preferences_json=?,custom_interests_json=?,
                primary_intent=?,secondary_intents_json=?,activity_styles_json=?,
                primary_travel_mode=?,fallback_travel_modes_json=?,
                total_time_minutes=?,max_one_way_travel_minutes=?,
                max_one_way_distance_metres=?,
                accessibility_json=?,preference_version=?,updated_at=?
            WHERE user_id=?""",
            (
                likes,
                dislikes,
                categories,
                motivations,
                available if available is not None else 30,
                travel_modes,
                max_travel,
                max_walking if max_walking is not None else 20,
                intensity or "gentle",
                budget or "free",
                social or "solo_only",
                environment or "either",
                accessibility,
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
                canonical.primary_intent.value,
                json.dumps([item.value for item in canonical.secondary_intents]),
                json.dumps([item.value for item in canonical.activity_styles]),
                canonical.primary_travel_mode.value,
                json.dumps([item.value for item in canonical.fallback_travel_modes]),
                canonical.total_time_minutes,
                canonical.max_one_way_travel_minutes,
                canonical.max_one_way_distance_metres,
                json.dumps(canonical.accessibility.model_dump(mode="json")),
                canonical.preference_version,
                iso(now()),
                user["id"],
            ),
        )
    return profile(user)


@app.put("/v1/profile/home-zone")
def set_home_zone(
    body: HomeZoneRequest, user: sqlite3.Row = Depends(require_user)
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
    user: sqlite3.Row = Depends(require_user),
) -> dict:
    del user  # Endpoint is authenticated even though no player data is returned.
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(422, "q must contain at least 2 characters")
    normalized_city = city.strip() if city and city.strip() else None
    return {"areas": OpenStreetMapPlaceProvider().areas(query, normalized_city)}


@app.post("/v1/routes/preview")
def preview_route(
    body: RoutePreviewRequest,
    user: sqlite3.Row = Depends(require_user),
) -> dict[str, object]:
    """Return an authoritative route without logging player coordinates."""
    require_ready(user)
    try:
        route = RouteProvider().route(
            (body.origin.latitude, body.origin.longitude),
            (body.destination.latitude, body.destination.longitude),
            travel_mode=body.travel_mode.value,
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
    user: sqlite3.Row = Depends(require_user),
) -> dict[str, object]:
    """Return live city highlights, nearby places, regional trips, and food venues."""
    profile = require_ready(user)
    city = profile["home_city"]
    if not isinstance(city, str) or not city.strip():
        raise HTTPException(409, "Set a home city before discovering places")
    query = food_query.strip() if food_query else None
    return CityDiscoveryProvider().discover(
        city=city.strip(), center=home_center(profile), food_query=query
    )


@app.get("/v1/decks/today", response_model=DeckOut)
def today_deck(user: sqlite3.Row = Depends(require_user)) -> DeckOut:
    return deck_for(user, require_ready(user))


@app.post("/v1/decks/today/generate", response_model=DeckOut)
def generate_today_deck(user: sqlite3.Row = Depends(require_user)) -> DeckOut:
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
    user: sqlite3.Row = Depends(require_user),
) -> DeckOut:
    """Replace only offered quests after canonical preferences are saved.

    Generation happens before the transaction. If routing or generation fails,
    the stored deck is untouched. Started, completed, skipped, and expired quests
    remain exactly as they were.
    """
    profile = require_ready(user)
    local_day, zone = local_context(user)
    categories = quest_categories_for_profile(profile)
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
        categories=categories,
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
def refresh_deck(user: sqlite3.Row = Depends(require_user)) -> DeckOut:
    profile = require_ready(user)
    deck_for(user, profile)
    local_day, zone = local_context(user)
    categories = quest_categories_for_profile(profile)
    with connect() as db:
        stored = db.execute(
            "SELECT * FROM decks WHERE user_id=? AND local_date=?",
            (user["id"], local_day.isoformat()),
        ).fetchone()
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
        categories=categories,
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
                    walking_minutes=?, distance_source=?, estimated_activity_minutes=?,
                    cost_band=?, activity_type=?, topic=?, intent=?,
                    activity_style=?, travel_mode=?, route_duration_minutes=?,
                    route_distance_meters=?, total_estimated_minutes=?,
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
                    quest.walking_minutes,
                    quest.distance_source.value,
                    quest.estimated_activity_minutes,
                    quest.cost_band.value,
                    quest.activity_type,
                    quest.topic.value if quest.topic else None,
                    quest.intent.value if quest.intent else None,
                    quest.activity_style.value if quest.activity_style else None,
                    quest.travel_mode.value if quest.travel_mode else None,
                    quest.route_duration_minutes,
                    quest.route_distance_meters,
                    quest.total_estimated_minutes,
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
def start_quest(quest_id: str, user: sqlite3.Row = Depends(require_user)) -> QuestOut:
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
def skip_quest(quest_id: str, user: sqlite3.Row = Depends(require_user)) -> dict:
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
    user: sqlite3.Row = Depends(require_user),
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
def progress(user: sqlite3.Row = Depends(require_user)) -> dict:
    _, zone = local_context(user)
    return progress_payload(user["id"], zone)


@app.get("/v1/history", response_model=list[QuestOut])
def history(user: sqlite3.Row = Depends(require_user)) -> list[QuestOut]:
    with connect() as db:
        rows = db.execute(
            "SELECT q.* FROM quests q JOIN decks d ON d.id=q.deck_id WHERE d.user_id=? AND q.state='completed' ORDER BY q.completed_at DESC",
            (user["id"],),
        ).fetchall()
    return [quest_out(row) for row in rows]


@app.delete("/v1/account", status_code=204)
def delete_account(
    response: Response, user: sqlite3.Row = Depends(require_user)
) -> Response:
    # The local adapter deletes immediately. A production adapter can replace this with a queued 30-day purge.
    with transaction() as db:
        db.execute("DELETE FROM users WHERE id=?", (user["id"],))
    response.delete_cookie(
        "refresh_token", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
    return response
