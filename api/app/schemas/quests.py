"""Pydantic models for quest generation (request, LLM output, service result)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Difficulty(str, Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class MovementIntensity(str, Enum):
    gentle = "gentle"
    moderate = "moderate"
    energetic = "energetic"


class InterestTopic(str, Enum):
    explorer = "explorer"
    foodie = "foodie"
    skill_builder = "skill_builder"
    social_connector = "social_connector"
    adventurer = "adventurer"
    nature_mindfulness = "nature_mindfulness"


class InterestAffinity(str, Enum):
    love = "love"
    okay = "okay"
    avoid = "avoid"


class InterestPreference(BaseModel):
    topic: InterestTopic
    affinity: InterestAffinity = InterestAffinity.okay


class CustomInterest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    affinity: InterestAffinity = InterestAffinity.okay


class ProfilePreferences(BaseModel):
    """Canonical preference contract persisted on a player profile."""

    interest_preferences: list[InterestPreference] = Field(default_factory=list)
    custom_interests: list[CustomInterest] = Field(default_factory=list, max_length=20)
    max_one_way_distance_metres: int = Field(default=5_000, ge=250, le=150_000)
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
        return self


class DistanceSource(str, Enum):
    approximate = "approximate"


XP_BY_DIFFICULTY: dict[Difficulty, int] = {
    Difficulty.easy: 50,
    Difficulty.medium: 100,
    Difficulty.hard: 150,
}

WALKING_METRES_PER_MINUTE = 80
MAX_SEARCH_RADIUS_METRES = 150_000
OSRM_MATRIX_LIMIT = 24
MAX_GENERATION_PLACE_CANDIDATES = 40


class PlaceCandidateIn(BaseModel):
    """Enriched place for generation; coordinates stay backend-only."""

    provider_id: str
    name: str = Field(min_length=1, max_length=200)
    category: InterestTopic
    topics: list[InterestTopic] = Field(default_factory=list, max_length=8)
    latitude: float
    longitude: float
    place_type: str = Field(default="place", min_length=1, max_length=80)
    public_access: bool = True
    verified_features: list[str] = Field(default_factory=list, max_length=20)
    distance_metres: int = Field(default=0, ge=0)
    distance_source: DistanceSource = DistanceSource.approximate
    landmark_rank: int = Field(default=10, ge=0, le=10)


class QuestGenerationRequest(BaseModel):
    city: str = Field(min_length=2, max_length=100)
    count: int = Field(default=5, ge=1, le=5)
    movement_intensity: MovementIntensity = MovementIntensity.gentle
    place_candidates: list[PlaceCandidateIn] = Field(
        default_factory=list, max_length=MAX_GENERATION_PLACE_CANDIDATES
    )
    exclude_titles: list[str] = Field(default_factory=list, max_length=50)
    interest_preferences: list[InterestPreference] = Field(default_factory=list)
    custom_interests: list[CustomInterest] = Field(default_factory=list, max_length=20)
    preference_version: int = Field(default=1, ge=1)


class GeneratedQuestDraft(BaseModel):
    """Structured LLM output for a single quest. No time windows — quests are anytime."""

    title: str = Field(min_length=4, max_length=80)
    description: str = Field(min_length=20, max_length=400)
    category: InterestTopic
    difficulty: Difficulty
    candidate_id: str = Field(min_length=1, max_length=40)
    activity_type: str = Field(min_length=2, max_length=40)
    safety_notes: str = Field(min_length=1, max_length=200)


class GeneratedQuestBatch(BaseModel):
    quests: list[GeneratedQuestDraft] = Field(min_length=1, max_length=5)


class GeneratedQuest(BaseModel):
    """Validated quest ready for persistence (backend-resolved place fields)."""

    title: str
    description: str
    category: InterestTopic
    difficulty: Difficulty
    base_xp: int
    place_name: str
    place_provider_id: str
    place_type: str
    latitude: float
    longitude: float
    distance_meters: int
    distance_source: DistanceSource
    activity_type: str
    # Immutable matching snapshot. Optional defaults preserve compatibility with
    # generation code while it is migrated to the canonical preference contract.
    topic: InterestTopic | None = None
    match_reasons: list[str] = Field(default_factory=list, max_length=3)
    preference_version: int | None = Field(default=None, ge=1)
