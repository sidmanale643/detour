"""Quest generation via OpenRouter: prompt, validate, safety, diversity, place binding."""

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
    SocialComfort,
)

# Uvicorn configures this logger for application-visible INFO logs by default.
logger = logging.getLogger("uvicorn.error")


class QuestGenerationError(Exception):
    """Raised when OpenRouter cannot produce a full valid quest batch."""


SYSTEM_PROMPT = """\
You generate real-world urban quests for Detour, a walkable city exploration app.

## Safety and legality (hard)
- Legal for an adult in a public urban setting only.
- Never dangerous, illegal, or fatal: no weapons, trespass, private property intrusion, \
traffic stunts, unprotected heights/cliffs, swimming/ice risks, substance use, self-harm, \
confrontation, theft, vandalism, railway tracks, rooftops, or hitchhiking.
- No medical claims, therapy claims, or physical prescriptions.
- No required purchase, proof upload, booking, photography mandate, journaling mandate, \
or required stranger interaction.
- Prefer free public activities. cost_band must be "free" or "low" and must respect the \
profile budget (if budget is "free", only free).

## Hard constraints vs preferences
- Hard: legality, safety, public access, candidate binding, budget, social comfort, \
time budget (one-way walking_minutes + estimated_activity_minutes must fit available_minutes), \
environment preference when the place environment is known.
- Preferences: motivations, interests (likes), dislikes as soft exclusions, movement intensity.

## Exact candidate binding
- Every quest MUST set candidate_id to one of the provided place_candidates.candidate_id values.
- Never invent a place. Never use place names as IDs.
- Use only verified_features listed for that candidate. Do not invent amenities, hours, \
entry fees, events, or opening status.
- Do not assume a place is open right now.

## Activity quality
- Clear primary action and a recognizable completion point the player can honor-system mark done.
- Location-specific rather than generic: reference the place type and verified features only.
- Solo-friendly when social_comfort is solo_only (no required talking to strangers or staff).
- Optional interaction is allowed only when social_comfort is optional_interaction.
- Match movement_intensity: gentle = calm easy activities; moderate = some movement; \
energetic = more active movement. Never force hard intensity on gentle users.
- Respect accessibility_notes when present (e.g. avoid stairs-heavy wording if noted).

## Batch diversity (when count > 1)
- Distinct candidate_id values.
- Distinct activity_type values (e.g. observe, walk, sketch, learn, breathe, move).
- Distinct titles.
- Spread categories across available candidates when possible.
- Prefer a mix of difficulties when count >= 3 (not all hard).

Return only structured data matching the schema. Be creative but safe and grounded.
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


def requires_stranger_interaction(title: str, description: str, activity_type: str) -> bool:
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
        token in blob for token in ("stairs only", "many stairs", "climb stairs", "uneven rocky")
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
                "distance_metres": place.distance_metres,
                "walking_minutes": place.walking_minutes,
                "distance_source": place.distance_source.value,
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
            "motivations": [m.value for m in request.motivations],
            "interests": request.likes,
            "dislikes": request.dislikes,
            "available_minutes": request.available_minutes,
            "max_walking_minutes": request.max_walking_minutes,
            "movement_intensity": request.movement_intensity.value,
            "budget": request.budget.value,
            "social_comfort": request.social_comfort.value,
            "environment_preference": request.environment_preference.value,
            "accessibility_notes": request.accessibility_notes,
        },
        "place_candidates": place_candidates,
        "excluded_candidate_ids": list(excluded_candidate_ids or request.exclude_candidate_ids),
        "excluded_titles": request.exclude_titles,
        "notes": [
            "Produce exactly the requested_count of diverse quests.",
            "Bind each quest to candidate_id from place_candidates only.",
            "Never invent amenities, hours, or place facts.",
            "one-way walking_minutes + estimated_activity_minutes must fit available_minutes.",
            "Do not include time windows, clock times, coordinates, or provider IDs.",
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
) -> GeneratedQuest:
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
    # Category must be compatible with the bound place.
    if draft.category != place.category:
        return None

    if not cost_respects_budget(request.budget, draft.cost_band):
        return None

    if request.social_comfort == SocialComfort.solo_only and requires_stranger_interaction(
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

    total_minutes = place.walking_minutes + draft.estimated_activity_minutes
    if total_minutes > request.available_minutes:
        return None

    if place.walking_minutes > request.max_walking_minutes:
        return None

    if not environment_compatible(
        request.environment_preference.value, place.environment
    ):
        return None

    if not is_safe_text(draft.title, draft.description):
        return None

    if accessibility_contradicted(
        request.accessibility_notes,
        draft.title,
        draft.description,
        place.wheelchair.value,
    ):
        return None

    return draft_to_quest(draft, place)


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
            if re.search(r'"latitude"|"longitude"|home_latitude|home_longitude', user_prompt):
                logger.error("Blocked quest-generation prompt containing coordinates")
                raise QuestGenerationError(
                    "Refusing to send coordinates to OpenRouter"
                )
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


def build_service_from_settings() -> QuestGenerationService:
    """Factory used by the API. Requires DETOUR_OPENROUTER_API_KEY."""
    from ..config import settings
    from ..providers import OpenRouterQuestGenerator

    if not settings.openrouter_api_key:
        raise QuestGenerationError(
            "DETOUR_OPENROUTER_API_KEY is required for quest generation"
        )
    return QuestGenerationService(llm=OpenRouterQuestGenerator())
