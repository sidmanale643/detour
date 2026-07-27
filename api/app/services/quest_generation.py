"""Quest generation via OpenRouter with schema validation and place binding."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Protocol

from ..schemas.quests import (
    XP_BY_DIFFICULTY,
    GeneratedQuest,
    GeneratedQuestBatch,
    GeneratedQuestDraft,
    PlaceCandidateIn,
    QuestGenerationRequest,
)

# Uvicorn configures this logger for application-visible INFO logs by default.
logger = logging.getLogger("uvicorn.error")


class QuestGenerationError(Exception):
    """Raised when OpenRouter cannot produce a full quest batch."""


SYSTEM_PROMPT = """\
You generate real-world urban quests for Detour, a city exploration app.

Create the requested number of creative quests from the supplied context.
Treat interests as subject matter. Prefer loved interests and never use
avoided interests.

Use a candidate_id from the provided place_candidates. Return only structured data
matching the schema.
"""


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
) -> str:
    """Structured JSON for the LLM. Never includes coordinates or provider IDs."""
    place_candidates = []
    for candidate_id, place in id_to_place.items():
        place_candidates.append(
            {
                "candidate_id": candidate_id,
                "name": place.name,
                "place_type": place.place_type,
                "category": place.category.value,
                "topics": [topic.value for topic in place.topics],
                "distance_metres": place.distance_metres,
                "public_access": place.public_access,
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
        },
        "place_candidates": place_candidates,
        "excluded_titles": request.exclude_titles,
        "notes": [
            "Produce exactly the requested_count of quests.",
            "Use candidate IDs from place_candidates.",
            "Distance is already verified against the player's selected limit.",
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
    match_reasons: list[str] = []
    if topic is not None:
        match_reasons.append(f"Matches {topic.value.replace('_', ' ')}")
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
        distance_source=place.distance_source,
        activity_type=draft.activity_type.strip(),
        topic=topic,
        match_reasons=match_reasons[:3],
        preference_version=request.preference_version,
    )


class QuestGenerationService:
    def __init__(self, llm: QuestLlmClient) -> None:
        self._llm = llm

    def generate(self, request: QuestGenerationRequest) -> list[GeneratedQuest]:
        """Return exactly request.count OpenRouter-generated, place-bound quests.

        Raises QuestGenerationError if a full batch cannot be produced.
        """
        logger.info(
            "Starting quest generation: requested=%s candidates=%s excluded_titles=%s",
            request.count,
            len(request.place_candidates),
            len(request.exclude_titles),
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

        accepted = self._generate_from_llm(
            request,
            id_to_place=id_to_place,
            used_candidate_ids=used_candidate_ids,
            used_titles=used_titles,
        )
        if len(accepted) < request.count:
            logger.warning(
                "Quest generation produced an incomplete batch: accepted=%s requested=%s",
                len(accepted),
                request.count,
            )
            raise QuestGenerationError(
                f"OpenRouter produced {len(accepted)} quests; needed {request.count}"
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
    ) -> list[GeneratedQuest]:
        remaining = request.count
        collected: list[GeneratedQuest] = []
        last_error: Exception | None = None

        for attempt in range(2):
            if remaining <= 0:
                break
            attempt_request = request.model_copy(
                update={
                    "count": remaining,
                    "exclude_titles": sorted(used_titles),
                }
            )
            user_prompt = build_user_prompt(
                attempt_request,
                {
                    candidate_id: place
                    for candidate_id, place in id_to_place.items()
                    if candidate_id not in used_candidate_ids
                },
            )
            # Safety: never ship coordinates or home points to the model.
            if re.search(
                r'"latitude"|"longitude"|home_latitude|home_longitude', user_prompt
            ):
                logger.error("Blocked quest-generation prompt containing coordinates")
                raise QuestGenerationError("Refusing to send coordinates to OpenRouter")
            logger.info(
                "Requesting quest batch: attempt=%s requested=%s available_candidates=%s",
                attempt + 1,
                remaining,
                len(id_to_place) - len(used_candidate_ids),
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
                    "Binding generated quest draft: attempt=%s candidate_id=%s category=%s difficulty=%s activity_type=%s",
                    attempt + 1,
                    candidate_id,
                    draft.category.value,
                    draft.difficulty.value,
                    draft.activity_type,
                )
                if candidate_id not in id_to_place:
                    raise QuestGenerationError(
                        f"OpenRouter returned unknown candidate_id: {candidate_id}"
                    )
                quest = draft_to_quest(draft, id_to_place[candidate_id], request)
                collected.append(quest)
                used_candidate_ids.add(candidate_id)
                used_titles.add(quest.title.casefold())
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
    """Factory used by the API for OpenRouter-backed quest generation."""
    from ..config import settings
    from ..providers import OpenRouterQuestGenerator

    if not settings.openrouter_api_key:
        raise QuestGenerationError("DETOUR_OPENROUTER_API_KEY is not configured")
    return QuestGenerationService(llm=OpenRouterQuestGenerator())
