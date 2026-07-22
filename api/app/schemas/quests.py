"""Pydantic models for quest generation (request, LLM output, service result)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class QuestCategory(str, Enum):
    nature = "nature"
    culture = "culture"
    creativity = "creativity"
    mindfulness = "mindfulness"
    fitness = "fitness"
    learning = "learning"


class Motivation(str, Enum):
    explore = "explore"
    move = "move"
    learn = "learn"
    create = "create"
    reset = "reset"
    nature = "nature"
    break_routine = "break_routine"


class Difficulty(str, Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class CostBand(str, Enum):
    free = "free"
    low = "low"


class MovementIntensity(str, Enum):
    gentle = "gentle"
    moderate = "moderate"
    energetic = "energetic"


class SocialComfort(str, Enum):
    solo_only = "solo_only"
    optional_interaction = "optional_interaction"


class EnvironmentPreference(str, Enum):
    indoor = "indoor"
    outdoor = "outdoor"
    either = "either"


class PlaceEnvironment(str, Enum):
    indoor = "indoor"
    outdoor = "outdoor"
    unknown = "unknown"


class DistanceSource(str, Enum):
    walking_route = "walking_route"
    approximate = "approximate"


class WheelchairStatus(str, Enum):
    yes = "yes"
    no = "no"
    limited = "limited"
    unknown = "unknown"


XP_BY_DIFFICULTY: dict[Difficulty, int] = {
    Difficulty.easy: 50,
    Difficulty.medium: 100,
    Difficulty.hard: 150,
}

ALL_CATEGORIES: list[QuestCategory] = list(QuestCategory)

# Map legacy quest categories to onboarding motivations for profile defaults.
CATEGORY_TO_MOTIVATIONS: dict[str, list[Motivation]] = {
    "nature": [Motivation.nature, Motivation.explore],
    "culture": [Motivation.explore, Motivation.learn],
    "creativity": [Motivation.create],
    "mindfulness": [Motivation.reset],
    "fitness": [Motivation.move],
    "learning": [Motivation.learn],
}

ALLOWED_AVAILABLE_MINUTES = frozenset({15, 30, 60, 90})
ALLOWED_MAX_WALKING_MINUTES = frozenset({10, 20, 40, 60})

# Average walking speed used for approximate duration and search radius.
WALKING_METRES_PER_MINUTE = 80
MAX_SEARCH_RADIUS_METRES = 8000
OSRM_MATRIX_LIMIT = 24
MIN_SAFE_PLACE_CANDIDATES = 6


class PlaceCandidateIn(BaseModel):
    """Enriched place for generation; coordinates stay backend-only."""

    provider_id: str
    name: str = Field(min_length=1, max_length=200)
    category: QuestCategory
    latitude: float
    longitude: float
    place_type: str = Field(default="place", min_length=1, max_length=80)
    environment: PlaceEnvironment = PlaceEnvironment.unknown
    public_access: bool = True
    wheelchair: WheelchairStatus = WheelchairStatus.unknown
    verified_features: list[str] = Field(default_factory=list, max_length=20)
    distance_metres: int = Field(default=0, ge=0)
    walking_minutes: int = Field(default=0, ge=0)
    distance_source: DistanceSource = DistanceSource.approximate


class QuestGenerationRequest(BaseModel):
    city: str = Field(min_length=2, max_length=100)
    categories: list[QuestCategory] = Field(min_length=1, max_length=6)
    count: int = Field(default=5, ge=1, le=5)
    motivations: list[Motivation] = Field(default_factory=list, max_length=4)
    likes: list[str] = Field(default_factory=list, max_length=20)
    dislikes: list[str] = Field(default_factory=list, max_length=20)
    available_minutes: int = Field(default=30, ge=10, le=480)
    max_walking_minutes: int = Field(default=20, ge=5, le=120)
    movement_intensity: MovementIntensity = MovementIntensity.gentle
    budget: CostBand = CostBand.free
    social_comfort: SocialComfort = SocialComfort.solo_only
    environment_preference: EnvironmentPreference = EnvironmentPreference.either
    accessibility_notes: str | None = Field(default=None, max_length=500)
    place_candidates: list[PlaceCandidateIn] = Field(default_factory=list, max_length=40)
    exclude_titles: list[str] = Field(default_factory=list, max_length=50)
    exclude_candidate_ids: list[str] = Field(default_factory=list, max_length=50)


class GeneratedQuestDraft(BaseModel):
    """Structured LLM output for a single quest. No time windows — quests are anytime."""

    title: str = Field(min_length=4, max_length=80)
    description: str = Field(min_length=20, max_length=400)
    category: QuestCategory
    difficulty: Difficulty
    candidate_id: str = Field(min_length=1, max_length=40)
    estimated_activity_minutes: int = Field(ge=5, le=180)
    cost_band: CostBand
    activity_type: str = Field(min_length=2, max_length=40)
    safety_notes: str = Field(min_length=1, max_length=200)


class GeneratedQuestBatch(BaseModel):
    quests: list[GeneratedQuestDraft] = Field(min_length=1, max_length=5)


class GeneratedQuest(BaseModel):
    """Validated quest ready for persistence (backend-resolved place fields)."""

    title: str
    description: str
    category: QuestCategory
    difficulty: Difficulty
    base_xp: int
    place_name: str
    place_provider_id: str
    place_type: str
    latitude: float
    longitude: float
    distance_meters: int
    walking_minutes: int
    distance_source: DistanceSource
    estimated_activity_minutes: int
    cost_band: CostBand
    activity_type: str
