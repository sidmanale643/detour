"""Quest generation via OpenRouter with schema validation and place binding."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Protocol

from ..schemas.quests import (
    XP_BY_DIFFICULTY,
    CostBand,
    Difficulty,
    GeneratedQuest,
    GeneratedQuestBatch,
    GeneratedQuestDraft,
    MovementIntensity,
    PlaceCandidateIn,
    PlaceEnvironment,
    QuestGenerationRequest,
)

# Uvicorn configures this logger for application-visible INFO logs by default.
logger = logging.getLogger("uvicorn.error")


class QuestGenerationError(Exception):
    """Raised when OpenRouter cannot produce a full valid quest batch."""


SYSTEM_PROMPT = """\
You generate real-world urban quests for Detour, a city exploration app.

Create the requested number of creative quests from the supplied context.
Treat interests as subject matter, intent as the desired outcome, activity style
as what the player wants to do, and travel mode only as verified logistics.
Prefer loved interests, never use avoided interests, prioritize the primary
intent over secondary intents, and use only a selected activity style.

Use a candidate_id from the provided place_candidates. Return only structured data
matching the schema.
"""

_SAFETY_BLOCKLIST = re.compile(
    r"\b("
    r"trespass|break[\s-]?in|break into|rooftop|roof top|cliff|ledge|"
    r"weapon|gun|knife|assault|vandal|graffiti over|steal|theft|shoplift|"
    r"railway|railroad tracks|hitchhike|hitch[\s-]?hike|drunk|drugs?|"
    r"self[\s-]?harm|suicide|overdose|swim in|thin ice|storm drain|"
    r"abandoned building|private property|sneak into|jump the fence|"
    r"cross the highway|walk in traffic|night alley alone|"
    r"diagnos|prescri|cure your|treat your anxiety|medical advice"
    r")\b",
    re.IGNORECASE,
)

_COSTLY_HINTS = re.compile(
    r"\b("
    r"buy a ticket|purchase tickets?|paid tour|must buy|must purchase|"
    r"expensive|book a table|admission fee required|cover charge|"
    r"rent a car|uber|taxi only|helicopter|spa day|prove you|"
    r"upload (a |your )?photo|take a selfie and post|journal entry required"
    r")\b",
    re.IGNORECASE,
)

_STRANGER_INTERACTION = re.compile(
    r"\b("
    r"ask a stranger|talk to a stranger|speak to a stranger|"
    r"chat with (a |the )?local|interview someone|"
    r"make a new friend|approach someone|ask staff|"
    r"strike up a conversation|meet someone new|introduce yourself to"
    r")\b",
    re.IGNORECASE,
)

_ENERGETIC_HINTS = re.compile(
    r"\b("
    r"sprint|run laps|high[\s-]?intensity|hiit|burpee|race|"
    r"climb stairs rapidly|power walk for miles|intense workout"
    r")\b",
    re.IGNORECASE,
)

_MODERATE_PLUS_HINTS = re.compile(
    r"\b("
    r"jog|run |workout|exercise circuit|athletic|cardio"
    r")\b",
    re.IGNORECASE,
)


class QuestLlmClient(Protocol):
    """HTTP boundary for structured quest generation."""

    def complete_batch(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
    ) -> GeneratedQuestBatch:
        """Return a validated batch or raise on transport/parse failure."""
        ...


def is_safe_text(title: str, description: str) -> bool:
    blob = f"{title}\n{description}"
    if _SAFETY_BLOCKLIST.search(blob):
        return False
    if _COSTLY_HINTS.search(blob):
        return False
    return True


def requires_stranger_interaction(
    title: str, description: str, activity_type: str
) -> bool:
    blob = f"{title}\n{description}\n{activity_type}"
    return bool(_STRANGER_INTERACTION.search(blob))


def intensity_compatible(
    intensity: MovementIntensity,
    difficulty: Difficulty,
    title: str,
    description: str,
) -> bool:
    blob = f"{title}\n{description}"
    if intensity == MovementIntensity.gentle:
        if difficulty == Difficulty.hard:
            return False
        if _ENERGETIC_HINTS.search(blob) or _MODERATE_PLUS_HINTS.search(blob):
            return False
    if intensity == MovementIntensity.moderate:
        if _ENERGETIC_HINTS.search(blob) and difficulty == Difficulty.hard:
            return False
    return True


def cost_respects_budget(budget: CostBand, cost_band: CostBand) -> bool:
    if budget == CostBand.free:
        return cost_band == CostBand.free
    return cost_band in {CostBand.free, CostBand.low}


def environment_compatible(
    preference: str,
    place_environment: PlaceEnvironment,
) -> bool:
    if preference == "either":
        return True
    if place_environment == PlaceEnvironment.unknown:
        return True
    return place_environment.value == preference


def accessibility_contradicted(
    notes: str | None, title: str, description: str, wheelchair: str
) -> bool:
    if not notes:
        return False
    notes_cf = notes.casefold()
    blob = f"{title}\n{description}".casefold()
    if "wheelchair" in notes_cf and wheelchair == "no":
        return True
    if "wheelchair" in notes_cf and any(
        token in blob
        for token in ("stairs only", "many stairs", "climb stairs", "uneven rocky")
    ):
        return True
    if "no stairs" in notes_cf and "stairs" in blob:
        return True
    return False


def assign_opaque_ids(
    candidates: list[PlaceCandidateIn],
) -> dict[str, PlaceCandidateIn]:
    """Map place_01..place_N to backend place records for one generation request."""
    mapping: dict[str, PlaceCandidateIn] = {}
    for index, candidate in enumerate(candidates, start=1):
        mapping[f"place_{index:02d}"] = candidate
    return mapping


def build_user_prompt(
    request: QuestGenerationRequest,
    id_to_place: dict[str, PlaceCandidateIn],
    *,
    excluded_candidate_ids: list[str] | None = None,
) -> str:
    """Structured JSON for the LLM. Never includes coordinates or provider IDs."""
    place_candidates = []
    for candidate_id, place in id_to_place.items():
        if excluded_candidate_ids and candidate_id in excluded_candidate_ids:
            continue
        place_candidates.append(
            {
                "candidate_id": candidate_id,
                "name": place.name,
                "place_type": place.place_type,
                "category": place.category.value,
                "topics": [topic.value for topic in place.topics],
                "travel_mode": (
                    place.travel_mode.value
                    if place.travel_mode
                    else (
                        request.primary_travel_mode.value
                        if request.primary_travel_mode
                        else None
                    )
                ),
                "route_distance_metres": (
                    place.route_distance_metres
                    if place.route_distance_metres is not None
                    else place.distance_metres
                ),
                "one_way_travel_minutes": (
                    place.route_duration_minutes
                    if place.route_duration_minutes is not None
                    else place.walking_minutes
                ),
                "environment": place.environment.value,
                "public_access": place.public_access,
                "wheelchair": place.wheelchair.value,
                "verified_features": place.verified_features,
            }
        )
    payload = {
        "city": request.city,
        "requested_count": request.count,
        "profile": {
            "interest_preferences": [
                item.model_dump(mode="json") for item in request.interest_preferences
            ],
            "custom_interests": [
                item.model_dump(mode="json") for item in request.custom_interests
            ],
            "legacy_interests": (
                {"likes": request.likes, "dislikes": request.dislikes}
                if not request.interest_preferences and not request.custom_interests
                else None
            ),
            "primary_intent": request.primary_intent.value,
            "secondary_intents": [item.value for item in request.secondary_intents],
            "activity_styles": [item.value for item in request.activity_styles],
            "total_time_minutes": request.total_time_minutes,
            "max_one_way_travel_minutes": request.max_one_way_travel_minutes,
            "available_minutes": (
                request.total_time_minutes or request.available_minutes
            ),
            "max_walking_minutes": (
                request.max_one_way_travel_minutes or request.max_walking_minutes
            ),
            "budget": request.budget.value,
            "social_comfort": request.social_comfort.value,
            "environment_preference": request.environment_preference.value,
            "accessibility": (
                request.accessibility.model_dump(mode="json")
                if request.accessibility
                else None
            ),
        },
        "place_candidates": place_candidates,
        "excluded_candidate_ids": list(
            excluded_candidate_ids or request.exclude_candidate_ids
        ),
        "excluded_titles": request.exclude_titles,
        "notes": [
            "Produce exactly the requested_count of quests.",
            "Use candidate IDs from place_candidates.",
            "Set intent to the primary intent when it fits; otherwise use a listed secondary intent.",
            "Set activity_style to one of the selected activity styles.",
            "Travel time is round trip and must leave enough time for the activity.",
        ],
    }
    return json.dumps(payload, indent=2)


def openrouter_json_schema() -> dict:
    """JSON Schema for OpenRouter structured outputs (strict-friendly)."""
    return _strictify_schema(GeneratedQuestBatch.model_json_schema())


def _strictify_schema(node: dict) -> dict:
    """Recursively mark objects as additionalProperties: false for strict providers."""
    if not isinstance(node, dict):
        return node
    out = dict(node)
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        if "properties" in out:
            out["properties"] = {
                key: _strictify_schema(value) if isinstance(value, dict) else value
                for key, value in out["properties"].items()
            }
            if "required" not in out:
                out["required"] = list(out["properties"].keys())
    if "items" in out and isinstance(out["items"], dict):
        out["items"] = _strictify_schema(out["items"])
    if "$defs" in out:
        out["$defs"] = {
            key: _strictify_schema(value) if isinstance(value, dict) else value
            for key, value in out["$defs"].items()
        }
    if "definitions" in out:
        out["definitions"] = {
            key: _strictify_schema(value) if isinstance(value, dict) else value
            for key, value in out["definitions"].items()
        }
    if "anyOf" in out:
        out["anyOf"] = [
            _strictify_schema(item) if isinstance(item, dict) else item
            for item in out["anyOf"]
        ]
    return out


def draft_to_quest(
    draft: GeneratedQuestDraft,
    place: PlaceCandidateIn,
    request: QuestGenerationRequest,
) -> GeneratedQuest:
    affinities = {
        item.topic: item.affinity.value for item in request.interest_preferences
    }
    topic = next(
        (item for item in place.topics if affinities.get(item) == "love"),
        place.topics[0] if place.topics else None,
    )
    intent = draft.intent or request.primary_intent
    activity_style = draft.activity_style or (
        request.activity_styles[0] if request.activity_styles else None
    )
    route_minutes = (
        place.route_duration_minutes
        if place.route_duration_minutes is not None
        else place.walking_minutes
    )
    total_minutes = (
        (route_minutes * 2) + draft.estimated_activity_minutes
        if route_minutes is not None
        else None
    )
    match_reasons: list[str] = []
    if topic is not None:
        match_reasons.append(f"Matches {topic.value.replace('_', ' ')}")
    match_reasons.append(f"Your {intent.value} preference")
    if total_minutes is not None:
        match_reasons.append(f"{total_minutes} minutes total")
    return GeneratedQuest(
        title=draft.title.strip(),
        description=draft.description.strip(),
        category=draft.category,
        difficulty=draft.difficulty,
        base_xp=XP_BY_DIFFICULTY[draft.difficulty],
        place_name=place.name,
        place_provider_id=place.provider_id,
        place_type=place.place_type,
        latitude=place.latitude,
        longitude=place.longitude,
        distance_meters=place.distance_metres,
        walking_minutes=place.walking_minutes,
        distance_source=place.distance_source,
        estimated_activity_minutes=draft.estimated_activity_minutes,
        cost_band=draft.cost_band,
        activity_type=draft.activity_type.strip(),
        topic=topic,
        intent=intent,
        activity_style=activity_style,
        travel_mode=place.travel_mode or request.primary_travel_mode,
        route_duration_minutes=route_minutes,
        route_distance_meters=place.route_distance_metres,
        total_estimated_minutes=total_minutes,
        match_reasons=match_reasons[:3],
        preference_version=request.preference_version,
    )


def validate_draft(
    draft: GeneratedQuestDraft,
    *,
    request: QuestGenerationRequest,
    id_to_place: dict[str, PlaceCandidateIn],
    used_candidate_ids: set[str],
    used_titles: set[str],
    used_activities: set[str],
    excluded_candidate_ids: set[str],
) -> GeneratedQuest | None:
    """Return a bound quest if the draft passes all post-schema checks; else None."""
    candidate_id = draft.candidate_id.strip()
    if candidate_id not in id_to_place:
        return None
    if candidate_id in used_candidate_ids or candidate_id in excluded_candidate_ids:
        return None

    place = id_to_place[candidate_id]
    title_key = draft.title.casefold().strip()
    activity_key = draft.activity_type.casefold().strip()
    if not title_key or title_key in used_titles:
        return None
    if not activity_key or activity_key in used_activities:
        return None

    allowed_categories = set(request.categories)
    if draft.category not in allowed_categories:
        return None
    if draft.category != place.category:
        return None
    allowed_intents = {
        request.primary_intent,
        *request.secondary_intents,
    }
    if draft.intent is not None and draft.intent not in allowed_intents:
        return None
    if (
        draft.activity_style is not None
        and draft.activity_style not in request.activity_styles
    ):
        return None

    if not cost_respects_budget(request.budget, draft.cost_band):
        return None

    if request.social_comfort.value == "solo_only" and requires_stranger_interaction(
        draft.title, draft.description, draft.activity_type
    ):
        return None

    if not intensity_compatible(
        request.movement_intensity,
        draft.difficulty,
        draft.title,
        draft.description,
    ):
        return None

    route_minutes = (
        place.route_duration_minutes
        if place.route_duration_minutes is not None
        else place.walking_minutes
    )
    total_budget = request.total_time_minutes or request.available_minutes
    if route_minutes is None:
        return None
    total_minutes = (route_minutes * 2) + draft.estimated_activity_minutes
    if total_minutes > total_budget:
        return None
    one_way_limit = request.max_one_way_travel_minutes or request.max_walking_minutes
    if route_minutes > one_way_limit:
        return None

    if not environment_compatible(
        request.environment_preference.value, place.environment
    ):
        return None
    if not is_safe_text(draft.title, draft.description):
        return None
    if accessibility_contradicted(
        (
            request.accessibility.notes
            if request.accessibility is not None
            else request.accessibility_notes
        ),
        draft.title,
        draft.description,
        place.wheelchair.value,
    ):
        return None

    return draft_to_quest(draft, place, request)


class QuestGenerationService:
    def __init__(self, llm: QuestLlmClient) -> None:
        self._llm = llm

    def generate(self, request: QuestGenerationRequest) -> list[GeneratedQuest]:
        """Return exactly request.count OpenRouter-generated, place-bound quests.

        Raises QuestGenerationError if a full batch cannot be produced.
        """
        logger.info(
            "Starting quest generation: requested=%s candidates=%s categories=%s excluded_titles=%s excluded_candidates=%s",
            request.count,
            len(request.place_candidates),
            [category.value for category in request.categories],
            len(request.exclude_titles),
            len(request.exclude_candidate_ids),
        )
        if not request.place_candidates:
            raise QuestGenerationError(
                "place_candidates is required to bind quest locations"
            )
        if len(request.place_candidates) < request.count:
            raise QuestGenerationError(
                "Not enough place candidates to build the requested quest batch"
            )

        id_to_place = assign_opaque_ids(request.place_candidates)
        used_candidate_ids: set[str] = set()
        # Pre-seed exclusions from refresh / prior batch titles.
        used_titles = {t.casefold().strip() for t in request.exclude_titles}
        used_activities: set[str] = set()
        excluded_ids = set(request.exclude_candidate_ids)

        accepted = self._generate_from_llm(
            request,
            id_to_place=id_to_place,
            used_candidate_ids=used_candidate_ids,
            used_titles=used_titles,
            used_activities=used_activities,
            excluded_candidate_ids=excluded_ids,
        )
        if len(accepted) < request.count:
            logger.warning(
                "Quest generation produced an incomplete batch: accepted=%s requested=%s",
                len(accepted),
                request.count,
            )
            raise QuestGenerationError(
                f"OpenRouter produced {len(accepted)} valid quests; needed {request.count}"
            )
        logger.info(
            "Quest generation completed: accepted=%s requested=%s",
            len(accepted),
            request.count,
        )
        return accepted[: request.count]

    def _generate_from_llm(
        self,
        request: QuestGenerationRequest,
        *,
        id_to_place: dict[str, PlaceCandidateIn],
        used_candidate_ids: set[str],
        used_titles: set[str],
        used_activities: set[str],
        excluded_candidate_ids: set[str],
    ) -> list[GeneratedQuest]:
        remaining = request.count
        collected: list[GeneratedQuest] = []
        last_error: Exception | None = None
        # Opaque IDs rejected this generation (unknown or invalid output).
        rejected_ids: set[str] = set(excluded_candidate_ids)

        for attempt in range(2):
            if remaining <= 0:
                break
            attempt_exclusions = sorted(rejected_ids | used_candidate_ids)
            attempt_request = request.model_copy(
                update={
                    "count": remaining,
                    "exclude_titles": sorted(used_titles),
                    "exclude_candidate_ids": attempt_exclusions,
                }
            )
            user_prompt = build_user_prompt(
                attempt_request,
                id_to_place,
                excluded_candidate_ids=attempt_exclusions,
            )
            # Safety: never ship coordinates or home points to the model.
            if re.search(
                r'"latitude"|"longitude"|home_latitude|home_longitude', user_prompt
            ):
                logger.error("Blocked quest-generation prompt containing coordinates")
                raise QuestGenerationError("Refusing to send coordinates to OpenRouter")
            logger.info(
                "Requesting quest batch: attempt=%s requested=%s available_candidates=%s excluded_candidates=%s",
                attempt + 1,
                remaining,
                len(id_to_place) - len(attempt_exclusions),
                len(attempt_exclusions),
            )
            started_at = time.monotonic()
            try:
                batch = self._llm.complete_batch(
                    system=SYSTEM_PROMPT,
                    user=user_prompt,
                    schema=openrouter_json_schema(),
                )
            except Exception as exc:
                last_error = exc
                logger.exception(
                    "OpenRouter quest generation failed: attempt=%s elapsed_ms=%s error_type=%s",
                    attempt + 1,
                    round((time.monotonic() - started_at) * 1000),
                    type(exc).__name__,
                )
                continue

            logger.info(
                "Received quest batch: attempt=%s drafts=%s elapsed_ms=%s",
                attempt + 1,
                len(batch.quests),
                round((time.monotonic() - started_at) * 1000),
            )

            for draft in batch.quests:
                candidate_id = draft.candidate_id.strip()
                logger.info(
                    "Evaluating generated quest draft: attempt=%s candidate_id=%s category=%s difficulty=%s activity_type=%s activity_minutes=%s cost_band=%s",
                    attempt + 1,
                    candidate_id,
                    draft.category.value,
                    draft.difficulty.value,
                    draft.activity_type,
                    draft.estimated_activity_minutes,
                    draft.cost_band.value,
                )
                if candidate_id not in id_to_place:
                    rejected_ids.add(candidate_id)
                    logger.warning(
                        "Rejected generated quest: attempt=%s candidate_id=%s reason=unknown_candidate",
                        attempt + 1,
                        candidate_id,
                    )
                    continue
                quest = validate_draft(
                    draft,
                    request=request,
                    id_to_place=id_to_place,
                    used_candidate_ids=used_candidate_ids,
                    used_titles=used_titles,
                    used_activities=used_activities,
                    excluded_candidate_ids=rejected_ids,
                )
                if quest is None:
                    # Keep the candidate excluded for the retry so we do not loop on it.
                    rejected_ids.add(candidate_id)
                    logger.warning(
                        "Rejected generated quest: attempt=%s candidate_id=%s reason=validation_failed",
                        attempt + 1,
                        candidate_id,
                    )
                    continue
                collected.append(quest)
                used_candidate_ids.add(candidate_id)
                used_titles.add(quest.title.casefold())
                used_activities.add(quest.activity_type.casefold())
                remaining -= 1
                logger.info(
                    "Accepted generated quest: attempt=%s candidate_id=%s category=%s difficulty=%s remaining=%s",
                    attempt + 1,
                    candidate_id,
                    quest.category.value,
                    quest.difficulty.value,
                    remaining,
                )
                if remaining <= 0:
                    break

        if remaining > 0 and last_error is not None and not collected:
            raise QuestGenerationError(
                f"OpenRouter quest generation failed: {last_error}"
            ) from last_error
        if remaining > 0:
            logger.warning(
                "Quest generation exhausted retries: accepted=%s requested=%s remaining=%s",
                len(collected),
                request.count,
                remaining,
            )
        return collected


class MockQuestLlmClient:
    """Mock quest generator for local development when no API key is provided."""

    def complete_batch(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
    ) -> GeneratedQuestBatch:
        from ..schemas.quests import QuestCategory
        try:
            payload = json.loads(user)
            candidates = payload.get("place_candidates", [])
            count = payload.get("requested_count", len(candidates))
        except Exception:
            candidates = []
            count = 1

        quests = []
        for i in range(min(count, len(candidates))):
            cand = candidates[i]
            cid = cand.get("candidate_id")
            name = cand.get("name", "Unknown Place")
            cat = cand.get("category", "nature")
            
            # Simple template based on category
            activity_type = "explore"
            if cat == "nature":
                title = f"Appreciate the greenery at {name}"
                desc = f"Walk around {name}, find a quiet spot near the trees, and observe the birds or leaves for 5 minutes."
                activity_type = "observe"
            elif cat == "culture":
                title = f"Discover local heritage at {name}"
                desc = f"Locate the main information plaque, monument, or historical sign at {name} and read it carefully."
                activity_type = "read"
            elif cat == "creativity":
                title = f"Sketch the view at {name}"
                desc = f"Take a seat at {name} and sketch the most interesting shape or detail you can see."
                activity_type = "sketch"
            elif cat == "mindfulness":
                title = f"Mindful breathing at {name}"
                desc = f"Sit quietly at {name}, close your eyes, and focus entirely on the sounds around you."
                activity_type = "breathe"
            elif cat == "fitness":
                title = f"Paced walk around {name}"
                desc = f"Take a continuous 15-minute brisk walk around the perimeter of {name}."
                activity_type = "walk"
            else: # learning / default
                title = f"Learn about {name}"
                desc = f"Observe the architecture, layout, or design of {name} and note one thing you didn't expect."
                activity_type = "observe"

            quests.append(
                GeneratedQuestDraft(
                    title=title,
                    description=desc,
                    category=QuestCategory(cat),
                    difficulty=Difficulty.medium,
                    candidate_id=cid,
                    estimated_activity_minutes=15,
                    cost_band=CostBand.free,
                    activity_type=activity_type,
                    safety_notes="Stay aware of your surroundings and yield to pedestrians."
                )
            )

        return GeneratedQuestBatch(quests=quests)


def build_service_from_settings() -> QuestGenerationService:
    """Factory used by the API. Falls back to MockQuestLlmClient if DETOUR_OPENROUTER_API_KEY is missing."""
    from ..config import settings
    from ..providers import OpenRouterQuestGenerator

    if not settings.openrouter_api_key:
        logger.warning(
            "DETOUR_OPENROUTER_API_KEY is not configured. Falling back to local mock quest generator."
        )
        return QuestGenerationService(llm=MockQuestLlmClient())
    return QuestGenerationService(llm=OpenRouterQuestGenerator())
