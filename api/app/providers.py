"""Map and place-provider integration boundaries."""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import httpx

from .config import settings
from .schemas.quests import (
    MAX_SEARCH_RADIUS_METRES,
    OSRM_MATRIX_LIMIT,
    DistanceSource,
)

SupportedTravelMode = Literal[
    "walking", "cycling", "two_wheeler", "four_wheeler", "public_transport"
]

_GOOGLE_TRAVEL_MODES: dict[SupportedTravelMode, str] = {
    "walking": "WALK",
    "cycling": "BICYCLE",
    "two_wheeler": "TWO_WHEELER",
    "four_wheeler": "DRIVE",
    "public_transport": "TRANSIT",
}


class RouteConfigurationError(RuntimeError):
    """Google Routes is not configured for a request that needs live routing."""


class RouteServiceError(RuntimeError):
    """Google Routes could not authoritatively answer a routing request."""


@dataclass(frozen=True)
class RouteResult:
    """A verified one-way route. ``None`` denotes an unreachable destination."""

    travel_mode: SupportedTravelMode
    distance_metres: int
    duration_seconds: int
    encoded_polyline: str | None = None

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

@dataclass(frozen=True)
class PlaceCandidate:
    provider_id: str
    name: str
    latitude: float
    longitude: float
    category: str
    place_type: str = "place"
    public_access: bool = True
    verified_features: list[str] = field(default_factory=list)
    distance_metres: int = 0
    distance_source: DistanceSource = DistanceSource.approximate
    landmark_rank: int = 10


@dataclass(frozen=True)
class DiscoveryPlace:
    """A display-oriented place from a live discovery provider."""

    provider: str
    provider_id: str
    name: str
    place_type: str
    latitude: float
    longitude: float
    distance_metres: int
    trip_kind: str
    matching_interest: str | None = None
    description: str | None = None
    image_url: str | None = None
    external_url: str | None = None
    rating: float | None = None
    review_count: int | None = None
    open_now: bool | None = None
    cuisines: list[str] = field(default_factory=list)


class PlaceDiscoveryUnavailable(RuntimeError):
    """OpenStreetMap could not answer a discovery request in time."""


def _osm_request_headers() -> dict[str, str]:
    """Identify Detour to public OSM/Overpass mirrors (required by usage policy)."""
    return {
        "User-Agent": settings.osm_user_agent,
        "Accept": "application/json",
        "Accept-Language": "en",
    }


def _overpass_endpoints() -> list[str]:
    """Primary Overpass URL plus configured fallbacks, de-duplicated in order."""
    return list(
        dict.fromkeys(
            (
                settings.overpass_url.rstrip("/"),
                *(url.rstrip("/") for url in settings.overpass_fallback_urls),
            )
        )
    )


def fetch_overpass_elements(
    query: str,
    *,
    timeout_seconds: float,
    log_label: str,
) -> list[dict]:
    """POST an Overpass QL query, trying each configured endpoint until one works.

    Public mirrors frequently return 406/429/5xx or time out. Quest generation
    already depended on fallbacks; Discover must use the same path.
    """
    started_at = time.monotonic()
    last_error: Exception | None = None
    with httpx.Client(timeout=timeout_seconds, headers=_osm_request_headers()) as client:
        for endpoint in _overpass_endpoints():
            try:
                response = client.post(endpoint, data={"data": query})
                response.raise_for_status()
                payload = response.json()
                elements = payload.get("elements", [])
                if not isinstance(elements, list):
                    raise ValueError("Overpass returned invalid elements")
                logger.info(
                    "%s Overpass ok: endpoint=%s status=%s elements=%s elapsed_ms=%s",
                    log_label,
                    endpoint,
                    response.status_code,
                    len(elements),
                    round((time.monotonic() - started_at) * 1000),
                )
                return [item for item in elements if isinstance(item, dict)]
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                status = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                logger.warning(
                    "%s Overpass endpoint failed: endpoint=%s status=%s error_type=%s elapsed_ms=%s",
                    log_label,
                    endpoint,
                    status,
                    type(exc).__name__,
                    round((time.monotonic() - started_at) * 1000),
                )
    raise PlaceProviderUnavailable(
        "Public Overpass place search is temporarily unavailable"
    ) from last_error


class CityDiscoveryProvider:
    """Interest-specific OSM discovery plus the legacy optional food lookup."""

    # These stay deliberately separate. A separate, bounded Overpass request per
    # selected interest is much less likely to time out than one broad union, and
    # gives every result an unambiguous user-facing matching category.
    _INTEREST_CLAUSES: dict[str, tuple[str, ...]] = {
        "explorer": (
            'nwr["name"][historic](around:{radius},{latitude},{longitude});',
            'nwr["name"][tourism~"museum|attraction|viewpoint"](around:{radius},{latitude},{longitude});',
            'nwr["name"][historic~"monument|memorial|archaeological_site|ruins|castle"](around:{radius},{latitude},{longitude});',
        ),
        "foodie": (
            'nwr["name"][amenity~"restaurant|cafe|fast_food|food_court|marketplace"](around:{radius},{latitude},{longitude});',
            'nwr["name"][shop~"bakery|confectionery|deli|greengrocer"](around:{radius},{latitude},{longitude});',
        ),
        "skill_builder": (
            'nwr["name"][tourism~"gallery|artwork"](around:{radius},{latitude},{longitude});',
            'nwr["name"][amenity~"arts_centre|library|college|university"](around:{radius},{latitude},{longitude});',
        ),
        "social_connector": (
            'nwr["name"][amenity~"community_centre|theatre|music_venue|concert_hall|events_venue"](around:{radius},{latitude},{longitude});',
            'nwr["name"][place="square"](around:{radius},{latitude},{longitude});',
        ),
        "adventurer": (
            'nwr["name"][leisure~"sports_centre|fitness_centre|pitch|track|stadium|swimming_pool"](around:{radius},{latitude},{longitude});',
            'nwr["name"][highway~"cycleway|path"](around:{radius},{latitude},{longitude});',
            'nwr["name"][sport](around:{radius},{latitude},{longitude});',
        ),
        "nature_mindfulness": (
            'nwr["name"][leisure~"park|garden|nature_reserve"](around:{radius},{latitude},{longitude});',
            'nwr["name"][natural~"water|wood|scrub|heath|beach"](around:{radius},{latitude},{longitude});',
            'nwr["name"][tourism="camp_site"](around:{radius},{latitude},{longitude});',
            'nwr["name"][highway~"pedestrian|footway"](around:{radius},{latitude},{longitude});',
        ),
    }
    _OVERPASS_REQUEST_TIMEOUT_SECONDS = 25.0

    def discover(
        self,
        *,
        city: str,
        center: tuple[float, float],
        interests: list[str] | None = None,
        radius_metres: int,
        food_query: str | None = None,
    ) -> dict[str, object]:
        """Return OSM matches inside exactly the selected straight-line radius."""
        selected_interests = self._normalise_interests(interests)
        matches = self._osm_selected_places(
            city=city,
            center=center,
            interests=selected_interests,
            radius_metres=radius_metres,
        )
        food, food_available = (
            self._google_food(city, center, food_query)
            if food_query
            else ([], bool(settings.google_places_key))
        )
        serialized_matches = [self._as_dict(place) for place in matches]
        return {
            "city": city,
            "matches": serialized_matches,
            # Kept temporarily so existing clients keep rendering the unified
            # OSM collection while they move to the explicit matches field.
            "nearby": serialized_matches,
            "city_highlights": [],
            "day_trips": [],
            "food": [self._as_dict(place) for place in food],
            "food_available": food_available,
        }

    def _osm_selected_places(
        self,
        *,
        city: str,
        center: tuple[float, float],
        interests: list[str],
        radius_metres: int,
    ) -> list[DiscoveryPlace]:
        del city  # Coordinates and radius, rather than a city name, bound OSM.
        radius = int(radius_metres)
        if radius <= 0:
            return []
        if not interests:
            return []

        started_at = time.monotonic()
        results_by_interest: dict[str, list[dict]] = {}
        failures: list[Exception] = []
        # Each request contains one interest's compact tag union and uses the
        # exact requested radius. The final haversine check below is retained as
        # the authoritative distance boundary for all OSM element geometries.
        with ThreadPoolExecutor(max_workers=min(6, len(interests))) as executor:
            futures = {
                executor.submit(self._overpass_interest_elements, interest, center, radius): interest
                for interest in interests
            }
            for future in as_completed(futures):
                interest = futures[future]
                try:
                    results_by_interest[interest] = future.result()
                except (httpx.HTTPError, ValueError, PlaceProviderUnavailable) as exc:
                    failures.append(exc)
                    logger.warning(
                        "Discover OSM interest lookup failed: interest=%s error_type=%s",
                        interest,
                        type(exc).__name__,
                    )

        # Partial success is useful when one interest's mirror fails but others
        # already returned places. Only hard-fail when nothing usable came back.
        if failures and not results_by_interest:
            raise PlaceDiscoveryUnavailable(
                "OpenStreetMap could not complete discovery for the selected interests. "
                "Please retry discovery."
            ) from failures[0]

        places: list[DiscoveryPlace] = []
        seen_entities: set[str] = set()
        seen_names: set[str] = set()
        osm = OpenStreetMapPlaceProvider()
        for interest in interests:
            for element in results_by_interest.get(interest, []):
                tags = element.get("tags") or {}
                name = tags.get("name")
                if not isinstance(name, str) or not (name := name.strip()):
                    continue
                if not osm._is_public(tags):
                    continue
                coordinate = osm._element_coordinate(element)
                if coordinate is None:
                    continue
                latitude, longitude = coordinate
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    continue
                raw_distance = haversine_metres(center, coordinate)
                if raw_distance > radius:
                    continue
                distance = int(round(raw_distance))
                element_type = element.get("type")
                element_id = element.get("id")
                if element_type not in {"node", "way", "relation"} or not isinstance(
                    element_id, int
                ):
                    continue
                provider_id = f"osm:{element_type}:{element_id}"
                name_key = name.casefold()
                if provider_id in seen_entities or name_key in seen_names:
                    continue
                seen_entities.add(provider_id)
                seen_names.add(name_key)
                places.append(
                    DiscoveryPlace(
                        provider="openstreetmap",
                        provider_id=provider_id,
                        name=name,
                        place_type=osm._place_type(tags),
                        latitude=latitude,
                        longitude=longitude,
                        distance_metres=distance,
                        trip_kind="nearby",
                        matching_interest=interest,
                        external_url=f"https://www.openstreetmap.org/{element_type}/{element_id}",
                    )
                )
        places.sort(
            key=lambda place: (
                place.distance_metres,
                place.name.casefold(),
                place.matching_interest or "",
            )
        )
        logger.info(
            "Discover OSM response: interests=%s matches=%s failed_interests=%s elapsed_ms=%s",
            interests,
            len(places),
            len(failures),
            round((time.monotonic() - started_at) * 1000),
        )
        if failures and not places:
            raise PlaceDiscoveryUnavailable(
                "OpenStreetMap returned no places for the selected interests. "
                "Please retry discovery."
            ) from failures[0]
        return places

    def _overpass_interest_elements(
        self,
        interest: str,
        center: tuple[float, float],
        radius_metres: int,
    ) -> list[dict]:
        clauses = self._INTEREST_CLAUSES[interest]
        latitude, longitude = center
        query = "\n".join(
            clause.format(
                radius=radius_metres,
                latitude=latitude,
                longitude=longitude,
            )
            for clause in clauses
        )
        # Cap the Overpass server-side timeout below the client timeout so mirrors
        # can fail fast and the shared endpoint walker can try the next host.
        overpass_query = f"""
        [out:json][timeout:20];
        (
          {query}
        );
        out center tags 120;
        """
        return fetch_overpass_elements(
            overpass_query,
            timeout_seconds=self._OVERPASS_REQUEST_TIMEOUT_SECONDS,
            log_label=f"Discover[{interest}]",
        )

    @classmethod
    def _normalise_interests(cls, interests: list[str] | None) -> list[str]:
        raw = interests or []
        selected: list[str] = []
        for item in raw:
            value = getattr(item, "value", item)
            if not isinstance(value, str):
                continue
            normalized = value.strip().casefold()
            if normalized in cls._INTEREST_CLAUSES and normalized not in selected:
                selected.append(normalized)
        return selected

    def _google_food(
        self, city: str, center: tuple[float, float], food_query: str | None
    ) -> tuple[list[DiscoveryPlace], bool]:
        if not settings.google_places_key:
            return [], False
        query = (
            f"{food_query.strip()} in {city}"
            if food_query and food_query.strip()
            else f"popular restaurants in {city}"
        )
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": settings.google_places_key,
                        "X-Goog-FieldMask": "places.id,places.displayName,places.location,places.primaryType,places.rating,places.userRatingCount,places.currentOpeningHours.openNow,places.formattedAddress,places.googleMapsUri,places.types",
                    },
                    json={
                        "textQuery": query,
                        "locationBias": {
                            "circle": {
                                "center": {
                                    "latitude": center[0],
                                    "longitude": center[1],
                                },
                                "radius": 15000.0,
                            }
                        },
                        "maxResultCount": 20,
                    },
                )
                response.raise_for_status()
                rows = response.json().get("places", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Discover Google Places lookup failed: %s", exc)
            return [], False
        places: list[DiscoveryPlace] = []
        for row in rows:
            location = row.get("location") or {}
            name = (row.get("displayName") or {}).get("text")
            latitude, longitude = location.get("latitude"), location.get("longitude")
            if (
                not isinstance(name, str)
                or not isinstance(latitude, (int, float))
                or not isinstance(longitude, (int, float))
            ):
                continue
            coordinate = (float(latitude), float(longitude))
            places.append(
                DiscoveryPlace(
                    provider="google_places",
                    provider_id=str(row.get("id", "")),
                    name=name,
                    place_type=str(row.get("primaryType") or "restaurant"),
                    latitude=coordinate[0],
                    longitude=coordinate[1],
                    distance_metres=int(round(haversine_metres(center, coordinate))),
                    trip_kind="nearby",
                    description=row.get("formattedAddress"),
                    external_url=row.get("googleMapsUri"),
                    rating=row.get("rating")
                    if isinstance(row.get("rating"), (int, float))
                    else None,
                    review_count=row.get("userRatingCount")
                    if isinstance(row.get("userRatingCount"), int)
                    else None,
                    open_now=(row.get("currentOpeningHours") or {}).get("openNow")
                    if isinstance(
                        (row.get("currentOpeningHours") or {}).get("openNow"), bool
                    )
                    else None,
                    cuisines=[
                        value.replace("_", " ")
                        for value in row.get("types", [])
                        if isinstance(value, str) and "restaurant" in value
                    ],
                )
            )
        places.sort(
            key=lambda place: (
                -(place.rating or 0),
                -(place.review_count or 0),
                place.distance_metres,
            )
        )
        return places, True

    @staticmethod
    def _split_values(raw: object) -> list[str]:
        return [value.strip() for value in str(raw or "").split(";") if value.strip()]

    @staticmethod
    def _as_dict(place: DiscoveryPlace) -> dict[str, object]:
        return {
            "provider": place.provider,
            "provider_id": place.provider_id,
            "name": place.name,
            "place_type": place.place_type,
            "latitude": place.latitude,
            "longitude": place.longitude,
            "distance_metres": place.distance_metres,
            "trip_kind": place.trip_kind,
            "matching_interest": place.matching_interest,
            "description": place.description,
            "image_url": place.image_url,
            "external_url": place.external_url,
            "rating": place.rating,
            "review_count": place.review_count,
            "open_now": place.open_now,
            "cuisines": place.cuisines,
        }

    @staticmethod
    def _osm_headers() -> dict[str, str]:
        return _osm_request_headers()


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
    ) -> list[PlaceCandidate]:
        raise NotImplementedError


class PlaceProviderUnavailable(RuntimeError):
    """No configured Overpass endpoint could complete place discovery."""


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
    ) -> list[PlaceCandidate]:
        requested = set(categories)
        radius = max(200, min(int(radius_metres), MAX_SEARCH_RADIUS_METRES))
        logger.info(
            "Quest place discovery started: provider=overpass categories=%s radius_metres=%s",
            sorted(requested),
            radius,
        )
        clauses: list[str] = []
        if "explorer" in requested:
            clauses.extend(
                [
                    f'nwr["name"][historic](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][tourism~"museum|attraction|viewpoint"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][historic~"monument|memorial|archaeological_site|ruins|castle"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        if "foodie" in requested:
            clauses.extend(
                [
                    f'nwr["name"][amenity~"restaurant|cafe|fast_food|food_court|marketplace"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][shop~"bakery|confectionery|deli|greengrocer"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        if "skill_builder" in requested:
            clauses.extend(
                [
                    f'nwr["name"][tourism~"gallery|artwork"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][amenity~"arts_centre|library|college|university"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        if "social_connector" in requested:
            clauses.extend(
                [
                    f'nwr["name"][amenity~"community_centre|theatre|music_venue|concert_hall|events_venue"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][place="square"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        if "adventurer" in requested:
            clauses.extend(
                [
                    f'nwr["name"][leisure~"sports_centre|fitness_centre|pitch|track|stadium|swimming_pool"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][highway~"cycleway|path"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][sport](around:{radius},{center[0]},{center[1]});',
                ]
            )
        if "nature_mindfulness" in requested:
            clauses.extend(
                [
                    f'nwr["name"][leisure~"park|garden|nature_reserve"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][natural~"water|wood|scrub|heath|beach"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][tourism="camp_site"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][highway~"pedestrian|footway"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        overpass_query = f"""
        [out:json][timeout:75];
        (
          {"".join(clauses)}
        );
        out center tags 300;
        """
        started_at = time.monotonic()
        elements = self._overpass_elements(overpass_query, started_at)

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
            place_type = self._place_type(tags)
            topic = self._topic_for_tags(tags, requested)
            if topic is None:
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
            seen_entities.add(entity_id)
            seen_names.add(name_key)
            results.append(
                PlaceCandidate(
                    provider_id=entity_id,
                    name=name,
                    latitude=latitude,
                    longitude=longitude,
                    category=topic,
                    place_type=place_type,
                    public_access=True,
                    verified_features=self._verified_features(tags, place_type),
                    distance_metres=distance,
                    distance_source=DistanceSource.approximate,
                    landmark_rank=self._landmark_rank(tags),
                )
            )
        results.sort(
            key=lambda candidate: (
                candidate.landmark_rank,
                candidate.distance_metres,
                candidate.name.casefold(),
            )
        )
        logger.info(
            "Quest place discovery filtered: provider=overpass eligible_candidates=%s",
            len(results),
        )
        return results

    @staticmethod
    def _overpass_elements(query: str, started_at: float) -> list[dict]:
        del started_at  # Elapsed time is logged inside the shared Overpass walker.
        return fetch_overpass_elements(
            query,
            timeout_seconds=90.0,
            log_label="Quest place discovery",
        )

    @staticmethod
    def _topic_for_tags(tags: dict, requested: set[str]) -> str | None:
        tourism = tags.get("tourism")
        amenity = tags.get("amenity")
        leisure = tags.get("leisure")
        highway = tags.get("highway")
        natural = tags.get("natural")
        shop = tags.get("shop")
        place = tags.get("place")
        matches = {
            "explorer": bool(tags.get("historic"))
            or tourism in {"museum", "attraction", "viewpoint"},
            "foodie": amenity
            in {"restaurant", "cafe", "fast_food", "food_court", "marketplace"}
            or shop in {"bakery", "confectionery", "deli", "greengrocer"},
            "skill_builder": tourism in {"gallery", "artwork"}
            or amenity in {"arts_centre", "library", "college", "university"},
            "social_connector": amenity
            in {"community_centre", "theatre", "music_venue", "concert_hall", "events_venue"}
            or place == "square",
            "adventurer": leisure
            in {"sports_centre", "fitness_centre", "pitch", "track", "stadium", "swimming_pool"}
            or highway in {"cycleway", "path"}
            or bool(tags.get("sport")),
            "nature_mindfulness": leisure in {"park", "garden", "nature_reserve"}
            or natural in {"water", "wood", "scrub", "heath", "beach"}
            or tourism == "camp_site"
            or highway in {"pedestrian", "footway"},
        }
        return next((topic for topic in matches if topic in requested and matches[topic]), None)

    @staticmethod
    def _landmark_rank(tags: dict) -> int:
        """Rank culturally significant, source-backed places before generic venues."""
        has_reference = bool(tags.get("wikipedia"))
        historic = tags.get("historic")
        tourism = tags.get("tourism")
        if has_reference and (historic or tourism in {"museum", "attraction"}):
            return 0
        if historic in {
            "archaeological_site",
            "ruins",
            "castle",
            "monument",
            "memorial",
        }:
            return 1
        if tourism in {"museum", "attraction", "viewpoint"}:
            return 2
        if historic:
            return 3
        if has_reference and tags.get("amenity") == "place_of_worship":
            return 4
        return 10

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
                response = client.get(f"{settings.nominatim_url}/search", params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        return [area for item in payload if (area := self._area(item)) is not None]

    @staticmethod
    def _headers() -> dict[str, str]:
        return _osm_request_headers()

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
    """Server-side Google Routes boundary for live map previews.

    Coordinates are never included in logs or exception text. A missing key is
    a configuration error; provider failures are service errors so the client
    can keep the active quest visible without a path.
    """

    _ROUTE_FIELD_MASK = (
        "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline,"
        "routes.travelAdvisory"
    )

    @staticmethod
    def _google_mode(mode: SupportedTravelMode) -> str:
        try:
            return _GOOGLE_TRAVEL_MODES[mode]
        except KeyError as exc:
            raise ValueError(f"Unsupported travel mode: {mode}") from exc

    @staticmethod
    def _duration_seconds(value: object) -> int | None:
        if not isinstance(value, str) or not value.endswith("s"):
            return None
        try:
            seconds = float(value[:-1])
        except ValueError:
            return None
        return max(0, int(round(seconds)))

    @staticmethod
    def _waypoint(point: tuple[float, float]) -> dict[str, object]:
        # RouteProvider points are (latitude, longitude).
        return {"location": {"latLng": {"latitude": point[0], "longitude": point[1]}}}

    def _headers(self, field_mask: str) -> dict[str, str]:
        if not settings.google_routes_key:
            raise RouteConfigurationError(
                "Google Routes is not configured. Set DETOUR_GOOGLE_ROUTES_KEY."
            )
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.google_routes_key,
            "X-Goog-FieldMask": field_mask,
        }

    @staticmethod
    def _departure_time(mode: SupportedTravelMode) -> str | None:
        if mode != "public_transport":
            return None
        return (
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )

    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        travel_mode: SupportedTravelMode = "walking",
    ) -> RouteResult | None:
        """Return one Google Compute Routes result for map previews."""
        mode = self._google_mode(travel_mode)
        payload: dict[str, object] = {
            "origin": self._waypoint(origin),
            "destination": self._waypoint(destination),
            "travelMode": mode,
            "computeAlternativeRoutes": False,
        }
        departure_time = self._departure_time(travel_mode)
        if departure_time:
            payload["departureTime"] = departure_time
        logger.info(
            "Google Routes preview started: mode=%s timeout_seconds=%s",
            travel_mode,
            settings.google_routes_timeout_seconds,
        )
        started_at = time.monotonic()
        try:
            with httpx.Client(timeout=settings.google_routes_timeout_seconds) as client:
                response = client.post(
                    f"{settings.google_routes_url}/directions/v2:computeRoutes",
                    headers=self._headers(self._ROUTE_FIELD_MASK),
                    json=payload,
                )
                response.raise_for_status()
                route_payload = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                logger.warning(
                    "Google Routes preview authorization failed: status=%s elapsed_ms=%s",
                    exc.response.status_code,
                    round((time.monotonic() - started_at) * 1000),
                )
                raise RouteConfigurationError(
                    "Google Routes credentials are invalid or not authorized."
                ) from exc
            logger.warning(
                "Google Routes preview request failed: status=%s elapsed_ms=%s",
                exc.response.status_code,
                round((time.monotonic() - started_at) * 1000),
            )
            raise RouteServiceError(
                "Google Routes is temporarily unavailable."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Google Routes preview request failed: error_type=%s elapsed_ms=%s",
                type(exc).__name__,
                round((time.monotonic() - started_at) * 1000),
            )
            raise RouteServiceError(
                "Google Routes is temporarily unavailable."
            ) from exc
        if not isinstance(route_payload, dict):
            raise RouteServiceError("Google Routes returned an invalid route response.")
        routes = route_payload.get("routes")
        if not isinstance(routes, list) or not routes:
            logger.info(
                "Google Routes preview empty: mode=%s elapsed_ms=%s",
                travel_mode,
                round((time.monotonic() - started_at) * 1000),
            )
            return None
        first = routes[0]
        if not isinstance(first, dict):
            raise RouteServiceError("Google Routes returned an invalid route response.")
        distance = first.get("distanceMeters")
        duration = self._duration_seconds(first.get("duration"))
        if not isinstance(distance, (int, float)) or duration is None:
            return None
        polyline = first.get("polyline")
        encoded_polyline = (
            polyline.get("encodedPolyline")
            if isinstance(polyline, dict)
            and isinstance(polyline.get("encodedPolyline"), str)
            else None
        )
        logger.info(
            "Google Routes preview ok: mode=%s distance_m=%s duration_s=%s has_polyline=%s elapsed_ms=%s",
            travel_mode,
            int(round(distance)),
            duration,
            encoded_polyline is not None,
            round((time.monotonic() - started_at) * 1000),
        )
        return RouteResult(
            travel_mode=travel_mode,
            distance_metres=max(0, int(round(distance))),
            duration_seconds=duration,
            encoded_polyline=encoded_polyline,
        )


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
            "Sending OpenRouter quest request: model=%s timeout_seconds=%s prompt_bytes=%s schema_keys=%s",
            settings.openrouter_quest_model,
            settings.openrouter_timeout_seconds,
            len(user.encode("utf-8")),
            sorted(schema.keys()),
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
                "Received OpenRouter quest response: model=%s status_code=%s elapsed_ms=%s response_bytes=%s",
                settings.openrouter_quest_model,
                response.status_code,
                round((time.monotonic() - started_at) * 1000),
                len(response.content),
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
            "Validated OpenRouter quest response: model=%s quests=%s candidate_ids=%s categories=%s difficulties=%s",
            settings.openrouter_quest_model,
            len(batch.quests),
            [quest.candidate_id for quest in batch.quests],
            [quest.category.value for quest in batch.quests],
            [quest.difficulty.value for quest in batch.quests],
        )
        return batch


class JobQueue:
    """Dramatiq/Redis boundary. Local API builds decks synchronously for easy startup."""

    def enqueue_deck(self, user_id: int, local_date: str) -> None:
        raise NotImplementedError
