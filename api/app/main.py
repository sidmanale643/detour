from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
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
    OpenStreetMapPlaceProvider,
    enrich_with_walking_routes,
    search_radius_metres,
)
from .schemas.quests import (
    ALL_CATEGORIES,
    ALLOWED_AVAILABLE_MINUTES,
    ALLOWED_MAX_WALKING_MINUTES,
    CATEGORY_TO_MOTIVATIONS,
    MIN_SAFE_PLACE_CANDIDATES,
    CostBand,
    EnvironmentPreference,
    GeneratedQuest,
    Motivation,
    MovementIntensity,
    PlaceCandidateIn,
    QuestCategory,
    QuestGenerationRequest,
    SocialComfort,
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
DECK_SIZE = 1
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


class ProfilePatch(BaseModel):
    """Partial profile update — omitted fields are left unchanged."""

    likes: list[str] | None = Field(default=None, max_length=20)
    dislikes: list[str] | None = Field(default=None, max_length=20)
    categories: list[str] | None = Field(default=None, max_length=8)
    motivations: list[str] | None = Field(default=None, max_length=4)
    available_minutes: int | None = Field(default=None)
    max_walking_minutes: int | None = Field(default=None)
    movement_intensity: str | None = None
    budget: str | None = None
    social_comfort: str | None = None
    environment_preference: str | None = None
    accessibility_notes: str | None = Field(default=None, max_length=500)

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


def parse_categories(raw: list[str] | str) -> list[QuestCategory]:
    """Normalize profile category strings into enums; fall back to all categories."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    selected: list[QuestCategory] = []
    for value in raw:
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


def place_search_categories(
    profile: sqlite3.Row, categories: list[QuestCategory]
) -> list[str]:
    """Categories used for OSM lookup — prefer profile tracks, expand from motivations."""
    values = [c.value for c in categories]
    for motivation in profile_motivations(profile):
        for category in _MOTIVATION_TO_PLACE_CATEGORIES.get(motivation.value, []):
            if category not in values:
                values.append(category)
    # Sparse areas: cast a wider net so a six-place deck is still possible.
    if len(values) < 4:
        for category in ALL_CATEGORIES:
            if category.value not in values:
                values.append(category.value)
    return values


def place_candidates_for(
    profile: sqlite3.Row,
    categories: list[QuestCategory],
) -> list[PlaceCandidateIn]:
    """Load and enrich OSM places near home (binding source for generation)."""
    center = home_center(profile)
    category_values = place_search_categories(profile, categories)
    max_walking = profile_int(profile, "max_walking_minutes", 20)
    environment = profile_str(profile, "environment_preference", "either")
    provider = OpenStreetMapPlaceProvider()

    # Progressive radius: prefer tight walks, expand discovery if the area is sparse.
    raw: list = []
    for factor in (1.25, 1.75, 2.5):
        radius = search_radius_metres(max_walking, factor=factor)
        raw = provider.candidates(
            profile["home_city"],
            category_values,
            center,
            profile["home_cell"],
            radius_metres=radius,
            max_walking_minutes=max_walking,
            environment_preference=environment,
            accessibility_notes=profile["accessibility_notes"],
        )
        if len(raw) >= MIN_SAFE_PLACE_CANDIDATES:
            break

    enriched = enrich_with_walking_routes(
        center,
        raw,
        max_walking_minutes=max_walking,
        categories=category_values,
    )
    results: list[PlaceCandidateIn] = []
    for item in enriched:
        try:
            category = QuestCategory(item.category)
        except ValueError:
            continue
        results.append(
            PlaceCandidateIn(
                provider_id=item.provider_id,
                name=item.name,
                category=category,
                latitude=item.latitude,
                longitude=item.longitude,
                place_type=item.place_type,
                environment=item.environment,
                public_access=item.public_access,
                wheelchair=item.wheelchair,
                verified_features=item.verified_features,
                distance_metres=item.distance_metres,
                walking_minutes=item.walking_minutes,
                distance_source=item.distance_source,
            )
        )
    if len(results) < MIN_SAFE_PLACE_CANDIDATES:
        raise HTTPException(
            503,
            (
                f"Only {len(results)} walkable public places were found near your home "
                f"(need {MIN_SAFE_PLACE_CANDIDATES}). "
                "Try increasing max walking time in preferences, or set home closer to "
                "parks, libraries, or public squares."
            ),
        )
    return results


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
    logger.info(
        "Preparing quest deck generation: requested=%s categories=%s refresh=%s",
        count,
        [category.value for category in categories],
        bool(exclude_titles or exclude_providers),
    )
    candidates = place_candidates_for(profile, categories)
    logger.info("Found quest place candidates: count=%s", len(candidates))
    # Drop places already used in the current deck (refresh) before opaque IDs.
    if exclude_providers:
        candidates = [
            c for c in candidates if c.provider_id not in exclude_providers
        ]
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
        raise HTTPException(503, str(exc)) from exc

    available_minutes = profile_int(profile, "available_minutes", 30)
    max_walking = profile_int(profile, "max_walking_minutes", 20)
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
        # Candidates are ranked by walking time and distance. Keep the LLM input
        # focused: one quest only needs a small, strong set of nearby options.
        batch_candidates = batch_candidates[:GENERATION_CANDIDATE_SHORTLIST]
        request = QuestGenerationRequest(
            city=profile["home_city"] or "your city",
            categories=categories,
            count=batch_size,
            motivations=profile_motivations(profile),
            likes=json.loads(profile["likes_json"] or "[]"),
            dislikes=json.loads(profile["dislikes_json"] or "[]"),
            available_minutes=available_minutes,
            max_walking_minutes=max_walking,
            movement_intensity=intensity,
            budget=budget,
            social_comfort=social,
            environment_preference=environment,
            accessibility_notes=profile["accessibility_notes"],
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
    logger.info("Quest deck generation completed: generated=%s requested=%s", len(generated), count)
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
            distance_source,estimated_activity_minutes,cost_band,activity_type
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
            quest.walking_minutes,
            quest.distance_source.value,
            quest.estimated_activity_minutes,
            quest.cost_band.value,
            quest.activity_type,
        ),
    )


def deck_for(user: sqlite3.Row, profile: sqlite3.Row) -> DeckOut:
    local_day, zone = local_context(user)
    categories = parse_categories(profile["categories_json"])
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
            "SELECT * FROM quests WHERE deck_id=? ORDER BY slot", (deck["id"],)
        ).fetchall()
    return DeckOut(
        local_date=local_day,
        refreshed=bool(deck["refreshed_at"]),
        refresh_available=not bool(deck["refreshed_at"])
        and any(r["state"] == "offered" for r in rows),
        quests=[quest_out(r) for r in rows],
    )


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
                    user_id, motivations_json, available_minutes, max_walking_minutes,
                    movement_intensity, budget, social_comfort, environment_preference,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    result.lastrowid,
                    json.dumps(["explore"]),
                    30,
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
                    user_id, motivations_json, available_minutes, max_walking_minutes,
                    movement_intensity, budget, social_comfort, environment_preference,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    json.dumps(["explore"]),
                    30,
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
            db.execute(
                "UPDATE users SET email_verified=1 WHERE id=?", (user_id,)
            )
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
        "max_walking_minutes": profile_int(p, "max_walking_minutes", 20),
        "movement_intensity": profile_str(p, "movement_intensity", "gentle"),
        "budget": profile_str(p, "budget", "free"),
        "social_comfort": profile_str(p, "social_comfort", "solo_only"),
        "environment_preference": profile_str(p, "environment_preference", "either"),
        "accessibility_notes": p["accessibility_notes"],
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
            json.dumps(body.likes)
            if body.likes is not None
            else current["likes_json"]
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
            motivations = json.dumps(
                [m.value for m in profile_motivations(current)]
            )
        available = (
            body.available_minutes
            if body.available_minutes is not None
            else current["available_minutes"]
        )
        max_walking = (
            body.max_walking_minutes
            if body.max_walking_minutes is not None
            else current["max_walking_minutes"]
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

        db.execute(
            """UPDATE profiles SET
                likes_json=?, dislikes_json=?, categories_json=?, motivations_json=?,
                available_minutes=?, max_walking_minutes=?, movement_intensity=?,
                budget=?, social_comfort=?, environment_preference=?,
                accessibility_notes=?, updated_at=?
            WHERE user_id=?""",
            (
                likes,
                dislikes,
                categories,
                motivations,
                available if available is not None else 30,
                max_walking if max_walking is not None else 20,
                intensity or "gentle",
                budget or "free",
                social or "solo_only",
                environment or "either",
                accessibility,
                iso(now()),
                user["id"],
            ),
        )
    return {"updated": True}


@app.put("/v1/profile/home-zone")
def set_home_zone(
    body: HomeZoneRequest, user: sqlite3.Row = Depends(require_user)
) -> dict:
    city = body.city.strip()
    h3_cell = h3.latlng_to_cell(body.latitude, body.longitude, 7)
    with transaction() as db:
        db.execute(
            "UPDATE profiles SET home_city=?,home_cell=?,home_latitude=?,home_longitude=?,home_address=?,home_source=?,updated_at=? WHERE user_id=?",
            (city, h3_cell, body.latitude, body.longitude, body.address.strip(), body.source, iso(now()), user["id"]),
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


@app.get("/v1/decks/today", response_model=DeckOut)
def today_deck(user: sqlite3.Row = Depends(require_user)) -> DeckOut:
    return deck_for(user, require_ready(user))


@app.post("/v1/decks/today/refresh", response_model=DeckOut)
def refresh_deck(user: sqlite3.Row = Depends(require_user)) -> DeckOut:
    profile = require_ready(user)
    deck_for(user, profile)
    local_day, zone = local_context(user)
    categories = parse_categories(profile["categories_json"])
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
            "SELECT * FROM quests WHERE deck_id=?", (stored["id"],)
        ).fetchall()
    exclude_titles = [row["title"] for row in all_rows]
    exclude_provider_ids = [
        row["place_provider_id"]
        for row in all_rows
        if row["place_provider_id"]
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

    expiry = datetime.combine(
        local_day + timedelta(days=1), time.min, zone
    ).astimezone(UTC)
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
                    cost_band=?, activity_type=?
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
