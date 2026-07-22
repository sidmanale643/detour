"""Map and place-provider integration boundaries."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field, replace

import httpx

from .config import settings
from .schemas.quests import (
    MAX_SEARCH_RADIUS_METRES,
    OSRM_MATRIX_LIMIT,
    WALKING_METRES_PER_MINUTE,
    DistanceSource,
    PlaceEnvironment,
    WheelchairStatus,
)

# Uvicorn configures this logger for application-visible INFO logs by default.
logger = logging.getLogger("uvicorn.error")

# Verified amenity/feature tags allowed into the LLM prompt (never invent beyond these).
_FEATURE_TAG_ALLOWLIST: dict[str, str] = {
    "bench": "benches",
    "picnic_table": "picnic tables",
    "toilets": "toilets",
    "drinking_water": "drinking water",
    "shelter": "shelter",
    "shade": "shade",
    "playground": "playground",
    "dog_park": "dog park",
    "garden": "garden",
    "viewpoint": "viewpoint",
    "fountain": "fountain",
    "outdoor_seating": "outdoor seating",
    "wifi": "wifi",
    "internet_access": "wifi",
    "books": "books",
    "library": "library",
    "museum": "museum exhibits",
    "artwork": "artwork",
    "sculpture": "sculpture",
    "memorial": "memorial",
    "monument": "monument",
    "historic": "historic site",
    "exhibition": "exhibition",
    "gallery": "gallery",
    "sports": "sports facilities",
    "fitness": "fitness equipment",
    "track": "walking track",
    "path": "walking paths",
    "footway": "walking paths",
    "hiking": "walking paths",
    "park": "park",
    "trees": "trees",
    "grass": "open green space",
}

_PRIVATE_ACCESS = frozenset(
    {"private", "no", "military", "restricted", "permit", "customers"}
)

_OUTDOOR_TYPES = frozenset(
    {
        "park",
        "garden",
        "pitch",
        "pedestrian",
        "attraction",
        "monument",
        "memorial",
        "viewpoint",
        "nature_reserve",
    }
)
_INDOOR_TYPES = frozenset(
    {
        "museum",
        "gallery",
        "library",
        "community_centre",
        "arts_centre",
        "sports_centre",
        "fitness_centre",
    }
)


@dataclass(frozen=True)
class PlaceCandidate:
    provider_id: str
    name: str
    latitude: float
    longitude: float
    category: str
    place_type: str = "place"
    environment: PlaceEnvironment = PlaceEnvironment.unknown
    public_access: bool = True
    wheelchair: WheelchairStatus = WheelchairStatus.unknown
    verified_features: list[str] = field(default_factory=list)
    distance_metres: int = 0
    walking_minutes: int = 0
    distance_source: DistanceSource = DistanceSource.approximate


def haversine_metres(
    origin: tuple[float, float], destination: tuple[float, float]
) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    lat1, lon1 = origin
    lat2, lon2 = destination
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def approximate_walking_minutes(distance_metres: float) -> int:
    return max(1, int(round(distance_metres / WALKING_METRES_PER_MINUTE)))


def search_radius_metres(max_walking_minutes: int, *, factor: float = 1.25) -> int:
    """Overpass search radius from walking preference.

    Uses a factor above pure crow-flies distance so winding pedestrian routes
    still discover candidate places; walking-time filters enforce the budget.
    """
    return min(
        int(max_walking_minutes * WALKING_METRES_PER_MINUTE * factor),
        MAX_SEARCH_RADIUS_METRES,
    )


class PlaceProvider:
    """Normalized destination candidates for a broad home zone."""

    def candidates(
        self,
        city: str,
        categories: list[str],
        center: tuple[float, float],
        boundary: str,
        *,
        radius_metres: int = MAX_SEARCH_RADIUS_METRES,
        max_walking_minutes: int | None = None,
        environment_preference: str = "either",
        accessibility_notes: str | None = None,
    ) -> list[PlaceCandidate]:
        raise NotImplementedError


class OpenStreetMapPlaceProvider(PlaceProvider):
    """Nominatim address search and Overpass nearby-place discovery."""

    def candidates(
        self,
        city: str,
        categories: list[str],
        center: tuple[float, float],
        boundary: str,
        *,
        radius_metres: int = MAX_SEARCH_RADIUS_METRES,
        max_walking_minutes: int | None = None,
        environment_preference: str = "either",
        accessibility_notes: str | None = None,
    ) -> list[PlaceCandidate]:
        del city, boundary
        requested = set(categories)
        radius = max(200, min(int(radius_metres), MAX_SEARCH_RADIUS_METRES))
        # Broad public urban places: parks, culture, learning, light fitness, calm streets.
        overpass_query = f"""
        [out:json][timeout:25];
        (
          nwr["name"][leisure~"park|garden|sports_centre|fitness_centre|pitch|playground|recreation_ground|nature_reserve|track"](around:{radius},{center[0]},{center[1]});
          nwr["name"][tourism~"museum|gallery|attraction|artwork|viewpoint"](around:{radius},{center[0]},{center[1]});
          nwr["name"][historic](around:{radius},{center[0]},{center[1]});
          nwr["name"][amenity~"library|community_centre|arts_centre|place_of_worship|college|university|theatre|townhall|marketplace"](around:{radius},{center[0]},{center[1]});
          nwr["name"][highway~"pedestrian|footway"](around:{radius},{center[0]},{center[1]});
          nwr["name"][landuse~"recreation_ground|grass"](around:{radius},{center[0]},{center[1]});
          nwr["name"][natural~"wood|scrub|heath"](around:{radius},{center[0]},{center[1]});
          nwr["name"][place~"square"](around:{radius},{center[0]},{center[1]});
        );
        out center tags 200;
        """
        try:
            with httpx.Client(timeout=30.0, headers=self._headers()) as client:
                response = client.post(
                    settings.overpass_url, data={"data": overpass_query}
                )
                response.raise_for_status()
                elements = response.json().get("elements", [])
        except (httpx.HTTPError, ValueError):
            return []

        needs_wheelchair = bool(
            accessibility_notes
            and "wheelchair" in accessibility_notes.casefold()
        )
        results: list[PlaceCandidate] = []
        seen_entities: set[str] = set()
        seen_names: set[str] = set()

        for element in elements:
            tags = element.get("tags") or {}
            name = tags.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            coordinate = self._element_coordinate(element)
            if not coordinate:
                continue
            if not self._is_public(tags):
                continue
            category = self._category(tags)
            if category is None or category not in requested:
                continue
            place_type = self._place_type(tags)
            environment = self._environment(place_type, tags)
            if environment_preference == "indoor" and environment == PlaceEnvironment.outdoor:
                continue
            if environment_preference == "outdoor" and environment == PlaceEnvironment.indoor:
                continue
            wheelchair = self._wheelchair(tags)
            if needs_wheelchair and wheelchair == WheelchairStatus.no:
                continue

            entity_id = f"osm:{element.get('type')}:{element.get('id')}"
            if entity_id in seen_entities:
                continue
            # Dedup by name so the same park is not listed twice under tags.
            name_key = name.casefold()
            if name_key in seen_names:
                continue

            latitude, longitude = coordinate
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            distance = int(round(haversine_metres(center, (latitude, longitude))))
            if max_walking_minutes is not None:
                # Straight-line prefilter. Roads are longer than crow-flies, so allow
                # a modest buffer; OSRM later enforces the true walking budget.
                max_straight = int(
                    max_walking_minutes * WALKING_METRES_PER_MINUTE * 1.15
                )
                if distance > max_straight:
                    continue

            seen_entities.add(entity_id)
            seen_names.add(name_key)
            results.append(
                PlaceCandidate(
                    provider_id=entity_id,
                    name=name,
                    latitude=latitude,
                    longitude=longitude,
                    category=category,
                    place_type=place_type,
                    environment=environment,
                    public_access=True,
                    wheelchair=wheelchair,
                    verified_features=self._verified_features(tags, place_type),
                    distance_metres=distance,
                    walking_minutes=approximate_walking_minutes(distance),
                    distance_source=DistanceSource.approximate,
                )
            )
        return results

    def areas(self, query: str, city: str | None = None) -> list[dict]:
        query = f"{query.strip()} {city.strip()}" if city else query.strip()
        if not query:
            return []
        params: dict[str, str | int] = {
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
        }
        try:
            with httpx.Client(timeout=8.0, headers=self._headers()) as client:
                response = client.get(
                    f"{settings.nominatim_url}/search", params=params
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        return [area for item in payload if (area := self._area(item)) is not None]

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"User-Agent": settings.osm_user_agent, "Accept-Language": "en"}

    @staticmethod
    def _element_coordinate(element: dict) -> tuple[float, float] | None:
        latitude = element.get("lat") or element.get("center", {}).get("lat")
        longitude = element.get("lon") or element.get("center", {}).get("lon")
        if not isinstance(latitude, (int, float)) or not isinstance(
            longitude, (int, float)
        ):
            return None
        return float(latitude), float(longitude)

    @staticmethod
    def _is_public(tags: dict) -> bool:
        access = str(tags.get("access", "yes")).casefold()
        if access in _PRIVATE_ACCESS:
            return False
        if str(tags.get("private", "")).casefold() in {"yes", "true", "1"}:
            return False
        return True

    @staticmethod
    def _category(tags: dict) -> str | None:
        leisure = tags.get("leisure")
        tourism = tags.get("tourism")
        amenity = tags.get("amenity")
        natural = tags.get("natural")
        landuse = tags.get("landuse")
        place = tags.get("place")
        if leisure in {"sports_centre", "fitness_centre", "pitch", "track"}:
            return "fitness"
        if leisure in {"park", "garden", "playground", "nature_reserve"} or natural in {
            "wood",
            "scrub",
            "heath",
        }:
            return "nature"
        if landuse in {"recreation_ground", "grass"}:
            return "nature"
        if tourism in {"gallery", "artwork"} or amenity == "arts_centre":
            return "creativity"
        if (
            tourism in {"museum", "attraction", "viewpoint"}
            or "historic" in tags
            or amenity in {"place_of_worship", "theatre", "townhall", "marketplace"}
            or place == "square"
        ):
            return "culture"
        if amenity in {"library", "community_centre", "college", "university"}:
            return "learning"
        if leisure == "recreation_ground":
            return "fitness"
        if tags.get("highway") in {"footway", "pedestrian"}:
            return "mindfulness"
        return None

    @staticmethod
    def _place_type(tags: dict) -> str:
        for key in (
            "leisure",
            "tourism",
            "amenity",
            "historic",
            "highway",
            "natural",
            "landuse",
            "place",
        ):
            value = tags.get(key)
            if isinstance(value, str) and value:
                return value
        return "place"

    @staticmethod
    def _environment(place_type: str, tags: dict) -> PlaceEnvironment:
        indoor_tag = str(tags.get("indoor", "")).casefold()
        if indoor_tag in {"yes", "true", "1"}:
            return PlaceEnvironment.indoor
        if indoor_tag in {"no", "false", "0"}:
            return PlaceEnvironment.outdoor
        if place_type in _INDOOR_TYPES:
            return PlaceEnvironment.indoor
        outdoor_extra = _OUTDOOR_TYPES | {
            "playground",
            "recreation_ground",
            "nature_reserve",
            "track",
            "wood",
            "scrub",
            "heath",
            "grass",
            "square",
            "viewpoint",
            "artwork",
            "place_of_worship",
            "footway",
        }
        if place_type in outdoor_extra:
            return PlaceEnvironment.outdoor
        return PlaceEnvironment.unknown

    @staticmethod
    def _wheelchair(tags: dict) -> WheelchairStatus:
        raw = str(tags.get("wheelchair", "")).casefold()
        if raw in {"yes", "designated"}:
            return WheelchairStatus.yes
        if raw == "no":
            return WheelchairStatus.no
        if raw in {"limited", "partial"}:
            return WheelchairStatus.limited
        return WheelchairStatus.unknown

    @staticmethod
    def _verified_features(tags: dict, place_type: str) -> list[str]:
        features: list[str] = []
        seen: set[str] = set()

        def add(label: str) -> None:
            key = label.casefold()
            if key not in seen:
                seen.add(key)
                features.append(label)

        type_label = _FEATURE_TAG_ALLOWLIST.get(place_type)
        if type_label:
            add(type_label)

        for key, value in tags.items():
            if not isinstance(value, str):
                continue
            value_cf = value.casefold()
            if value_cf in {"no", "false", "0", "private"}:
                continue
            if key in _FEATURE_TAG_ALLOWLIST and value_cf in {
                "yes",
                "true",
                "1",
                "designated",
            }:
                add(_FEATURE_TAG_ALLOWLIST[key])
            elif value in _FEATURE_TAG_ALLOWLIST:
                add(_FEATURE_TAG_ALLOWLIST[value])
            elif key == "highway" and value in {"footway", "path", "pedestrian"}:
                add("walking paths")
            elif key == "leisure" and value == "park":
                add("park")
            elif key == "surface" and value_cf in {"paved", "asphalt", "concrete"}:
                add("paved paths")

        return features[:12]

    @staticmethod
    def _area(item: dict) -> dict | None:
        try:
            latitude = float(item["lat"])
            longitude = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        name = item.get("display_name") or item.get("name")
        if not isinstance(name, str):
            return None
        address = item.get("address", {})
        city = next(
            (
                address.get(key)
                for key in (
                    "city",
                    "town",
                    "village",
                    "municipality",
                    "county",
                    "state",
                )
                if isinstance(address, dict) and isinstance(address.get(key), str)
            ),
            "Selected area",
        )
        return {
            "name": name,
            "city": city,
            "latitude": latitude,
            "longitude": longitude,
        }


def rank_place_candidates(
    candidates: list[PlaceCandidate],
    categories: list[str],
    *,
    limit: int = OSRM_MATRIX_LIMIT,
) -> list[PlaceCandidate]:
    """Prefer category coverage then proximity for the OSRM matrix shortlist."""
    requested = list(dict.fromkeys(categories))
    remaining = list(candidates)
    remaining.sort(key=lambda c: (c.distance_metres, c.name.casefold()))
    selected: list[PlaceCandidate] = []
    used_ids: set[str] = set()

    # First pass: one nearest place per requested category.
    for category in requested:
        for candidate in remaining:
            if candidate.provider_id in used_ids:
                continue
            if candidate.category == category:
                selected.append(candidate)
                used_ids.add(candidate.provider_id)
                break
        if len(selected) >= limit:
            return selected[:limit]

    # Fill remaining slots by proximity.
    for candidate in remaining:
        if candidate.provider_id in used_ids:
            continue
        selected.append(candidate)
        used_ids.add(candidate.provider_id)
        if len(selected) >= limit:
            break
    return selected


class RouteProvider:
    """OSRM walking-route normalization boundary."""

    def summary(
        self, origin: tuple[float, float], destination: tuple[float, float]
    ) -> dict:
        matrix = self.walking_matrix(origin, [destination])
        if not matrix:
            return {"walking": {"available": False}, "transit": {"available": False}}
        distance, duration = matrix[0]
        if distance is None or duration is None:
            return {"walking": {"available": False}, "transit": {"available": False}}
        return {
            "walking": {
                "available": True,
                "distance_metres": distance,
                "duration_seconds": duration,
            },
            "transit": {"available": False},
        }

    def walking_matrix(
        self,
        origin: tuple[float, float],
        destinations: list[tuple[float, float]],
    ) -> list[tuple[float | None, float | None]]:
        """Return (distance_metres, duration_seconds) per destination; None on miss."""
        if not destinations:
            return []
        # OSRM table: lon,lat;lon,lat...
        coords = [f"{origin[1]},{origin[0]}"] + [
            f"{lon},{lat}" for lat, lon in destinations
        ]
        coord_path = ";".join(coords)
        destinations_idx = ";".join(str(i) for i in range(1, len(coords)))
        url = (
            f"{settings.osrm_base_url}/table/v1/driving/{coord_path}"
            f"?sources=0&destinations={destinations_idx}&annotations=distance,duration"
        )
        try:
            with httpx.Client(timeout=12.0) as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OSRM table request failed: %s", exc)
            return [(None, None)] * len(destinations)

        distances = payload.get("distances") or []
        durations = payload.get("durations") or []
        if not distances or not durations:
            return [(None, None)] * len(destinations)
        row_d = distances[0] if distances else []
        row_t = durations[0] if durations else []
        results: list[tuple[float | None, float | None]] = []
        for index in range(len(destinations)):
            distance = row_d[index] if index < len(row_d) else None
            duration = row_t[index] if index < len(row_t) else None
            if (
                isinstance(distance, (int, float))
                and isinstance(duration, (int, float))
                and distance >= 0
                and duration >= 0
            ):
                results.append((float(distance), float(duration)))
            else:
                results.append((None, None))
        return results


def enrich_with_walking_routes(
    origin: tuple[float, float],
    candidates: list[PlaceCandidate],
    *,
    max_walking_minutes: int,
    categories: list[str],
) -> list[PlaceCandidate]:
    """Hybrid distance: approximate all, route best 24, keep within walking budget."""
    if not candidates:
        return []

    shortlist = rank_place_candidates(
        candidates, categories, limit=OSRM_MATRIX_LIMIT
    )
    router = RouteProvider()
    matrix = router.walking_matrix(
        origin, [(c.latitude, c.longitude) for c in shortlist]
    )

    routed: dict[str, PlaceCandidate] = {}
    for candidate, pair in zip(shortlist, matrix, strict=False):
        distance, duration = pair
        if distance is None or duration is None:
            # OSRM miss — keep approximate if still within preference.
            if candidate.walking_minutes <= max_walking_minutes:
                routed[candidate.provider_id] = candidate
            continue
        walking_minutes = max(1, int(math.ceil(duration / 60.0)))
        if walking_minutes > max_walking_minutes:
            # Routed over budget: keep approximate only if crow-flies estimate fits.
            if candidate.walking_minutes <= max_walking_minutes:
                routed[candidate.provider_id] = candidate
            continue
        routed[candidate.provider_id] = replace(
            candidate,
            distance_metres=int(round(distance)),
            walking_minutes=walking_minutes,
            distance_source=DistanceSource.walking_route,
        )

    # Merge: prefer routed shortlist entries; keep approximate non-shortlist within budget.
    final: list[PlaceCandidate] = []
    seen: set[str] = set()
    for candidate in shortlist:
        updated = routed.get(candidate.provider_id)
        if updated is None:
            continue
        if updated.walking_minutes > max_walking_minutes:
            continue
        final.append(updated)
        seen.add(updated.provider_id)

    for candidate in candidates:
        if candidate.provider_id in seen:
            continue
        if candidate.walking_minutes > max_walking_minutes:
            continue
        final.append(candidate)
        seen.add(candidate.provider_id)

    final.sort(key=lambda c: (c.walking_minutes, c.distance_metres, c.name.casefold()))
    return final


class QuestGenerator:
    """OpenRouter structured-output generator boundary; never receives exact home coordinates."""

    def generate(
        self, *, city: str, categories: list[str], candidates: list[PlaceCandidate]
    ) -> list[dict]:
        raise NotImplementedError


class OpenRouterQuestGenerator:
    """Thin OpenRouter chat-completions client for structured quest batches.

    Never send exact home coordinates — only city, preferences, and opaque place metadata.
    """

    def complete_batch(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
    ):
        from .schemas.quests import GeneratedQuestBatch

        if not settings.openrouter_api_key:
            raise RuntimeError("DETOUR_OPENROUTER_API_KEY is not configured")

        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": settings.openrouter_site_url,
            "X-Title": settings.openrouter_app_name,
        }
        body = {
            "model": settings.openrouter_quest_model,
            "temperature": 0.75,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "quest_batch",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        logger.info(
            "Sending OpenRouter quest request: model=%s timeout_seconds=%s",
            settings.openrouter_quest_model,
            settings.openrouter_timeout_seconds,
        )
        started_at = time.monotonic()
        with httpx.Client(
            timeout=settings.openrouter_timeout_seconds, headers=headers
        ) as client:
            response = client.post(
                f"{settings.openrouter_base_url}/chat/completions",
                json=body,
            )
            logger.info(
                "Received OpenRouter quest response: model=%s status_code=%s elapsed_ms=%s",
                settings.openrouter_quest_model,
                response.status_code,
                round((time.monotonic() - started_at) * 1000),
            )
            response.raise_for_status()
            payload = response.json()

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Unexpected OpenRouter response shape") from exc

        if not isinstance(content, str) or not content.strip():
            raise ValueError("Empty OpenRouter message content")

        # Some models wrap JSON in markdown fences.
        text = content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            text = "\n".join(lines).strip()

        batch = GeneratedQuestBatch.model_validate_json(text)
        logger.info(
            "Validated OpenRouter quest response: model=%s quests=%s",
            settings.openrouter_quest_model,
            len(batch.quests),
        )
        return batch


class JobQueue:
    """Dramatiq/Redis boundary. Local API builds decks synchronously for easy startup."""

    def enqueue_deck(self, user_id: int, local_date: str) -> None:
        raise NotImplementedError
