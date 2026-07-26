"""Pydantic models for quest generation (request, LLM output, service result)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


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


class TravelMode(str, Enum):
    walking = "walking"
    cycling = "cycling"
    two_wheeler = "two_wheeler"
    four_wheeler = "four_wheeler"
    public_transport = "public_transport"


class InterestTopic(str, Enum):
    nature_outdoors = "nature_outdoors"
    history_heritage = "history_heritage"
    architecture_public_spaces = "architecture_public_spaces"
    art_design = "art_design"
    books_learning = "books_learning"
    local_culture_community = "local_culture_community"
    food_markets = "food_markets"
    music_performance = "music_performance"


class InterestAffinity(str, Enum):
    love = "love"
    okay = "okay"
    avoid = "avoid"


class QuestIntent(str, Enum):
    explore = "explore"
    unwind = "unwind"
    learn = "learn"
    create = "create"
    move = "move"


class ActivityStyle(str, Enum):
    wander = "wander"
    observe = "observe"
    photograph = "photograph"
    sketch_or_write = "sketch_or_write"
    solve_or_research = "solve_or_research"
    reflect = "reflect"
    workout = "workout"


class InterestPreference(BaseModel):
    topic: InterestTopic
    affinity: InterestAffinity = InterestAffinity.okay


class CustomInterest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    affinity: InterestAffinity = InterestAffinity.okay


class AccessibilityRequirements(BaseModel):
    step_free: bool = False
    wheelchair_access: bool = False
    max_walking_minutes: int | None = Field(default=None, ge=0, le=120)
    seating_required: bool = False
    low_sensory: bool = False
    notes: str | None = Field(default=None, max_length=500)


class ProfilePreferences(BaseModel):
    """Canonical preference contract persisted on a player profile."""

    interest_preferences: list[InterestPreference] = Field(default_factory=list)
    custom_interests: list[CustomInterest] = Field(default_factory=list, max_length=20)
    primary_intent: QuestIntent = QuestIntent.explore
    secondary_intents: list[QuestIntent] = Field(default_factory=list, max_length=4)
    activity_styles: list[ActivityStyle] = Field(
        default_factory=lambda: [ActivityStyle.wander],
        min_length=1,
        max_length=7,
    )
    primary_travel_mode: TravelMode = TravelMode.walking
    fallback_travel_modes: list[TravelMode] = Field(default_factory=list, max_length=4)
    total_time_minutes: int = Field(default=30, ge=10, le=480)
    max_one_way_travel_minutes: int = Field(default=20, ge=5, le=120)
    max_one_way_distance_metres: int = Field(default=5_000, ge=250, le=150_000)
    accessibility: AccessibilityRequirements = Field(
        default_factory=AccessibilityRequirements
    )
    preference_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def unique_ordered_preferences(self) -> "ProfilePreferences":
        topics = [item.topic for item in self.interest_preferences]
        if len(topics) != len(set(topics)):
            raise ValueError("Each interest may appear only once")
        if self.interest_preferences and all(
            item.affinity == InterestAffinity.avoid
            for item in self.interest_preferences
        ):
            raise ValueError("At least one interest must not be avoided")
        labels = [item.label.casefold().strip() for item in self.custom_interests]
        if len(labels) != len(set(labels)):
            raise ValueError("Each custom interest may appear only once")
        if self.primary_intent in self.secondary_intents:
            raise ValueError("Primary intent cannot also be secondary")
        if len(self.secondary_intents) != len(set(self.secondary_intents)):
            raise ValueError("Secondary intents must be unique")
        if len(self.activity_styles) != len(set(self.activity_styles)):
            raise ValueError("Activity styles must be unique")
        if self.primary_travel_mode in self.fallback_travel_modes:
            raise ValueError("Primary travel mode cannot also be a fallback")
        if len(self.fallback_travel_modes) != len(set(self.fallback_travel_modes)):
            raise ValueError("Fallback travel modes must be unique")
        return self


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
    google_routes = "google_routes"
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
ALLOWED_MAX_TRAVEL_MINUTES = frozenset({10, 20, 40, 60, 90, 120})

# Average walking speed used for approximate duration and search radius.
WALKING_METRES_PER_MINUTE = 80
MAX_SEARCH_RADIUS_METRES = 150_000
OSRM_MATRIX_LIMIT = 24
MAX_GENERATION_PLACE_CANDIDATES = 40


class PlaceCandidateIn(BaseModel):
    """Enriched place for generation; coordinates stay backend-only."""

    provider_id: str
    name: str = Field(min_length=1, max_length=200)
    category: QuestCategory
    topics: list[InterestTopic] = Field(default_factory=list, max_length=8)
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
    landmark_rank: int = Field(default=10, ge=0, le=10)
    travel_mode: TravelMode | None = None
    route_duration_minutes: int | None = Field(default=None, ge=0)
    route_distance_metres: int | None = Field(default=None, ge=0)


class QuestGenerationRequest(BaseModel):
    city: str = Field(min_length=2, max_length=100)
    categories: list[QuestCategory] = Field(min_length=1, max_length=6)
    count: int = Field(default=5, ge=1, le=5)
    motivations: list[Motivation] = Field(default_factory=list, max_length=4)
    likes: list[str] = Field(default_factory=list, max_length=20)
    dislikes: list[str] = Field(default_factory=list, max_length=20)
    available_minutes: int = Field(default=30, ge=10, le=480)
    travel_modes: list[TravelMode] = Field(
        default_factory=lambda: [TravelMode.walking], min_length=1, max_length=6
    )
    max_travel_minutes: int | None = Field(default=None, ge=5, le=120)
    # Kept for request compatibility with profiles created before travel modes.
    max_walking_minutes: int = Field(default=20, ge=5, le=120)
    movement_intensity: MovementIntensity = MovementIntensity.gentle
    budget: CostBand = CostBand.free
    social_comfort: SocialComfort = SocialComfort.solo_only
    environment_preference: EnvironmentPreference = EnvironmentPreference.either
    accessibility_notes: str | None = Field(default=None, max_length=500)
    place_candidates: list[PlaceCandidateIn] = Field(
        default_factory=list, max_length=MAX_GENERATION_PLACE_CANDIDATES
    )
    exclude_titles: list[str] = Field(default_factory=list, max_length=50)
    exclude_candidate_ids: list[str] = Field(default_factory=list, max_length=50)
    # Canonical fields are additive while callers move off legacy categories and
    # motivations. Generation continues to accept legacy requests during rollout.
    interest_preferences: list[InterestPreference] = Field(default_factory=list)
    custom_interests: list[CustomInterest] = Field(default_factory=list, max_length=20)
    primary_intent: QuestIntent = QuestIntent.explore
    secondary_intents: list[QuestIntent] = Field(default_factory=list, max_length=4)
    activity_styles: list[ActivityStyle] = Field(default_factory=list, max_length=7)
    primary_travel_mode: TravelMode | None = None
    fallback_travel_modes: list[TravelMode] = Field(default_factory=list, max_length=4)
    total_time_minutes: int | None = Field(default=None, ge=10, le=480)
    max_one_way_travel_minutes: int | None = Field(default=None, ge=5, le=120)
    accessibility: AccessibilityRequirements | None = None
    preference_version: int = Field(default=1, ge=1)


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
    intent: QuestIntent | None = None
    activity_style: ActivityStyle | None = None
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
    # Immutable matching snapshot. Optional defaults preserve compatibility with
    # generation code while it is migrated to the canonical preference contract.
    topic: InterestTopic | None = None
    intent: QuestIntent | None = None
    activity_style: ActivityStyle | None = None
    travel_mode: TravelMode | None = None
    route_duration_minutes: int | None = Field(default=None, ge=0)
    route_distance_meters: int | None = Field(default=None, ge=0)
    total_estimated_minutes: int | None = Field(default=None, ge=0)
    match_reasons: list[str] = Field(default_factory=list, max_length=3)
    preference_version: int | None = Field(default=None, ge=1)
