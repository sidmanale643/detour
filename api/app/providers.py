"""Map and place-provider integration boundaries."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

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
    description: str | None = None
    image_url: str | None = None
    external_url: str | None = None
    rating: float | None = None
    review_count: int | None = None
    open_now: bool | None = None
    cuisines: list[str] = field(default_factory=list)


class CityDiscoveryProvider:
    """Live city catalogue built from OSM, Wikidata, and optionally Google Places."""

    NEARBY_RADIUS_METRES = 15_000
    DAY_TRIP_RADIUS_METRES = 150_000

    def discover(
        self,
        *,
        city: str,
        center: tuple[float, float],
        food_query: str | None = None,
    ) -> dict[str, object]:
        nearby = self._osm_nearby(center)
        landmarks = self._wikidata_landmarks(center)
        city_highlights, day_trips = self._split_landmarks(landmarks)
        food, food_available = self._google_food(city, center, food_query)
        return {
            "city": city,
            "nearby": [self._as_dict(place) for place in nearby],
            "city_highlights": [self._as_dict(place) for place in city_highlights],
            "day_trips": [self._as_dict(place) for place in day_trips],
            "food": [self._as_dict(place) for place in food],
            "food_available": food_available,
        }

    def _osm_nearby(self, center: tuple[float, float]) -> list[DiscoveryPlace]:
        query = f"""
        [out:json][timeout:75];
        (
          nwr[\"name\"][leisure~\"park|garden|nature_reserve\"](around:{self.NEARBY_RADIUS_METRES},{center[0]},{center[1]});
          nwr[\"name\"][tourism~\"attraction|museum|gallery|viewpoint\"](around:{self.NEARBY_RADIUS_METRES},{center[0]},{center[1]});
          nwr[\"name\"][historic](around:{self.NEARBY_RADIUS_METRES},{center[0]},{center[1]});
          nwr[\"name\"][amenity~\"marketplace|restaurant|cafe|fast_food|food_court\"](around:{self.NEARBY_RADIUS_METRES},{center[0]},{center[1]});
        );
        out center tags 120;
        """
        try:
            with httpx.Client(timeout=90.0, headers=self._osm_headers()) as client:
                response = client.post(settings.overpass_url, data={"data": query})
                response.raise_for_status()
                elements = response.json().get("elements", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Discover OSM lookup failed: %s", exc)
            return []

        places: list[DiscoveryPlace] = []
        seen: set[str] = set()
        for element in elements:
            tags = element.get("tags") or {}
            name = tags.get("name")
            coordinate = OpenStreetMapPlaceProvider._element_coordinate(element)
            if not isinstance(name, str) or not name.strip() or not coordinate:
                continue
            if not OpenStreetMapPlaceProvider._is_public(tags):
                continue
            latitude, longitude = coordinate
            key = name.strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            place_type = OpenStreetMapPlaceProvider._place_type(tags)
            distance = int(round(haversine_metres(center, coordinate)))
            places.append(
                DiscoveryPlace(
                    provider="openstreetmap",
                    provider_id=f"osm:{element.get('type')}:{element.get('id')}",
                    name=name.strip(),
                    place_type=place_type,
                    latitude=latitude,
                    longitude=longitude,
                    distance_metres=distance,
                    trip_kind="nearby" if distance <= 5_000 else "city",
                    cuisines=self._split_values(tags.get("cuisine")),
                )
            )
        places.sort(key=lambda place: (place.distance_metres, place.name.casefold()))
        return places[:40]

    def _wikidata_landmarks(self, center: tuple[float, float]) -> list[DiscoveryPlace]:
        query = """
        SELECT ?item ?itemLabel ?coord ?description ?article ?image ?sitelinks WHERE {
          SERVICE wikibase:around {
            ?item wdt:P625 ?coord .
            bd:serviceParam wikibase:center "Point(%(lon)s %(lat)s)"^^geo:wktLiteral .
            bd:serviceParam wikibase:radius "150" .
          }
          ?item wdt:P31/wdt:P279* ?type .
          VALUES ?type { wd:Q4989906 wd:Q570116 wd:Q839954 wd:Q35509 wd:Q16970 wd:Q23413 wd:Q16560 wd:Q22698 }
          OPTIONAL { ?item schema:description ?description FILTER(LANG(?description) = "en") }
          OPTIONAL { ?article schema:about ?item; schema:isPartOf <https://en.wikipedia.org/> }
          OPTIONAL { ?item wdt:P18 ?image }
          OPTIONAL { SELECT ?item (COUNT(?link) AS ?sitelinks) WHERE { ?link schema:about ?item } GROUP BY ?item }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
        }
        ORDER BY DESC(?sitelinks)
        LIMIT 60
        """
        try:
            with httpx.Client(
                timeout=30.0,
                headers={
                    "Accept": "application/sparql-results+json",
                    "User-Agent": settings.osm_user_agent,
                },
            ) as client:
                response = client.get(
                    settings.wikidata_sparql_url,
                    params={
                        "query": query % {"lat": center[0], "lon": center[1]},
                        "format": "json",
                    },
                )
                response.raise_for_status()
                rows = response.json().get("results", {}).get("bindings", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Discover Wikidata lookup failed: %s", exc)
            return []

        places: list[DiscoveryPlace] = []
        for row in rows:
            coordinate = self._wikidata_coordinate(row.get("coord", {}).get("value"))
            item_url = row.get("item", {}).get("value")
            name = row.get("itemLabel", {}).get("value")
            if (
                not coordinate
                or not isinstance(item_url, str)
                or not isinstance(name, str)
            ):
                continue
            distance = int(round(haversine_metres(center, coordinate)))
            if distance > self.DAY_TRIP_RADIUS_METRES:
                continue
            places.append(
                DiscoveryPlace(
                    provider="wikidata",
                    provider_id=item_url.rsplit("/", 1)[-1],
                    name=name,
                    place_type="landmark",
                    latitude=coordinate[0],
                    longitude=coordinate[1],
                    distance_metres=distance,
                    trip_kind="day_trip"
                    if distance > self.NEARBY_RADIUS_METRES
                    else "city",
                    description=row.get("description", {}).get("value"),
                    image_url=row.get("image", {}).get("value"),
                    external_url=row.get("article", {}).get("value"),
                )
            )
        return places

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
    def _split_landmarks(
        places: list[DiscoveryPlace],
    ) -> tuple[list[DiscoveryPlace], list[DiscoveryPlace]]:
        city = [place for place in places if place.trip_kind == "city"]
        trips = [place for place in places if place.trip_kind == "day_trip"]
        return city[:20], trips[:20]

    @staticmethod
    def _split_values(raw: object) -> list[str]:
        return [value.strip() for value in str(raw or "").split(";") if value.strip()]

    @staticmethod
    def _wikidata_coordinate(value: object) -> tuple[float, float] | None:
        if not isinstance(value, str) or not value.startswith("Point("):
            return None
        try:
            longitude, latitude = value.removeprefix("Point(").removesuffix(")").split()
            return float(latitude), float(longitude)
        except ValueError:
            return None

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
        return {"User-Agent": settings.osm_user_agent, "Accept-Language": "en"}


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
        8_000,
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
        requested = set(categories)
        radius = max(200, min(int(radius_metres), MAX_SEARCH_RADIUS_METRES))
        logger.info(
            "Quest place discovery started: provider=overpass categories=%s radius_metres=%s environment=%s wheelchair_filter=%s",
            sorted(requested),
            radius,
            environment_preference,
            bool(accessibility_notes and "wheelchair" in accessibility_notes.casefold()),
        )
        clauses: list[str] = []
        if "nature" in requested:
            clauses.extend(
                [
                    f'nwr["name"][leisure~"park|garden|playground|nature_reserve"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][natural~"wood|scrub|heath"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        if "culture" in requested:
            # Ask for heritage signals first. This avoids filling the response with
            # generic nearby amenities when a player has chosen history or culture.
            clauses.extend(
                [
                    f'nwr["name"][historic][wikidata](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][historic][wikipedia](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][historic](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][tourism~"museum|attraction|viewpoint"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][amenity="place_of_worship"][wikidata](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][amenity~"theatre|music_venue|concert_hall|townhall|marketplace"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        if "creativity" in requested:
            clauses.append(
                f'nwr["name"][tourism~"gallery|artwork"](around:{radius},{center[0]},{center[1]});'
            )
        if "learning" in requested:
            clauses.append(
                f'nwr["name"][amenity~"library|community_centre|college|university"](around:{radius},{center[0]},{center[1]});'
            )
        if "fitness" in requested:
            clauses.append(
                f'nwr["name"][leisure~"sports_centre|fitness_centre|pitch|track"](around:{radius},{center[0]},{center[1]});'
            )
        if "mindfulness" in requested:
            clauses.extend(
                [
                    f'nwr["name"][highway~"pedestrian|footway"](around:{radius},{center[0]},{center[1]});',
                    f'nwr["name"][place~"square"](around:{radius},{center[0]},{center[1]});',
                ]
            )
        overpass_query = f"""
        [out:json][timeout:75];
        (
          {''.join(clauses)}
        );
        out center tags 300;
        """
        started_at = time.monotonic()
        try:
            with httpx.Client(timeout=90.0, headers=self._headers()) as client:
                response = client.post(
                    settings.overpass_url, data={"data": overpass_query}
                )
                response.raise_for_status()
                elements = response.json().get("elements", [])
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Quest place discovery failed: provider=overpass status=%s elapsed_ms=%s",
                exc.response.status_code,
                round((time.monotonic() - started_at) * 1000),
            )
            # Broad multi-category queries can exceed the public Overpass
            # service's execution budget. Preserve the player's selected limit,
            # but retry discovery at a reliable radius so route filtering can
            # still choose places within that limit.
            if exc.response.status_code in {429, 504} and radius > 50_000:
                logger.info(
                    "Retrying quest place discovery with a smaller Overpass radius: requested_radius_metres=%s retry_radius_metres=%s",
                    radius,
                    50_000,
                )
                return self.candidates(
                    city,
                    categories,
                    center,
                    boundary,
                    radius_metres=50_000,
                    max_walking_minutes=max_walking_minutes,
                    environment_preference=environment_preference,
                    accessibility_notes=accessibility_notes,
                )
            return []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Quest place discovery failed: provider=overpass error_type=%s elapsed_ms=%s",
                type(exc).__name__,
                round((time.monotonic() - started_at) * 1000),
            )
            return []
        logger.info(
            "Quest place discovery response: provider=overpass status=%s elements=%s elapsed_ms=%s",
            response.status_code,
            len(elements) if isinstance(elements, list) else 0,
            round((time.monotonic() - started_at) * 1000),
        )

        needs_wheelchair = bool(
            accessibility_notes and "wheelchair" in accessibility_notes.casefold()
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
            if (
                environment_preference == "indoor"
                and environment == PlaceEnvironment.outdoor
            ):
                continue
            if (
                environment_preference == "outdoor"
                and environment == PlaceEnvironment.indoor
            ):
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
                    landmark_rank=self._landmark_rank(tags, category),
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
    def _landmark_rank(tags: dict, category: str) -> int:
        """Rank culturally significant, source-backed places before generic venues."""
        if category != "culture":
            return 10
        has_reference = bool(tags.get("wikidata") or tags.get("wikipedia"))
        historic = tags.get("historic")
        tourism = tags.get("tourism")
        if has_reference and (historic or tourism in {"museum", "attraction"}):
            return 0
        if historic in {"archaeological_site", "ruins", "castle", "monument", "memorial"}:
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
            or amenity
            in {
                "place_of_worship",
                "theatre",
                "music_venue",
                "concert_hall",
                "townhall",
                "marketplace",
            }
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


SupportedTravelMode = Literal[
    "walking", "cycling", "two_wheeler", "four_wheeler", "public_transport"
]

_GOOGLE_TRAVEL_MODES: dict[str, str] = {
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


class RouteProvider:
    """Authoritative server-side Google Routes boundary.

    This provider intentionally has no estimated or OSRM fallback. A missing
    result means the destination is ineligible for that mode; a provider error
    is raised so callers can retain the existing deck and offer a retry.
    """

    _MATRIX_FIELD_MASK = (
        "originIndex,destinationIndex,condition,status,distanceMeters,duration"
    )
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

    def matrix(
        self,
        origin: tuple[float, float],
        destinations: list[tuple[float, float]],
        *,
        travel_mode: SupportedTravelMode,
    ) -> list[RouteResult | None]:
        """Return verified routes in destination order; ``None`` means no route.

        Raises ``RouteConfigurationError`` for a missing key and
        ``RouteServiceError`` for unavailable or malformed provider responses.
        Coordinates are deliberately never included in logs or exception text.
        """
        if not destinations:
            logger.info(
                "Google Routes matrix skipped: mode=%s reason=no_destinations",
                travel_mode,
            )
            return []
        batch_limit = 100 if travel_mode == "public_transport" else 625
        if len(destinations) > batch_limit:
            combined: list[RouteResult | None] = []
            for start in range(0, len(destinations), batch_limit):
                combined.extend(
                    self.matrix(
                        origin,
                        destinations[start : start + batch_limit],
                        travel_mode=travel_mode,
                    )
                )
            return combined
        mode = self._google_mode(travel_mode)
        payload: dict[str, object] = {
            "origins": [{"waypoint": self._waypoint(origin)}],
            "destinations": [
                {"waypoint": self._waypoint(point)} for point in destinations
            ],
            "travelMode": mode,
        }
        departure_time = self._departure_time(travel_mode)
        if departure_time:
            payload["departureTime"] = departure_time
        logger.info(
            "Google Routes matrix started: mode=%s destinations=%s timeout_seconds=%s",
            travel_mode,
            len(destinations),
            settings.google_routes_timeout_seconds,
        )
        started_at = time.monotonic()
        try:
            with httpx.Client(timeout=settings.google_routes_timeout_seconds) as client:
                response = client.post(
                    f"{settings.google_routes_url}/distanceMatrix/v2:computeRouteMatrix",
                    headers=self._headers(self._MATRIX_FIELD_MASK),
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                logger.warning(
                    "Google Routes matrix authorization failed: mode=%s status=%s elapsed_ms=%s",
                    travel_mode,
                    exc.response.status_code,
                    round((time.monotonic() - started_at) * 1000),
                )
                raise RouteConfigurationError(
                    "Google Routes credentials are invalid or not authorized."
                ) from exc
            logger.warning(
                "Google Routes matrix request failed: mode=%s status=%s elapsed_ms=%s",
                travel_mode,
                exc.response.status_code,
                round((time.monotonic() - started_at) * 1000),
            )
            raise RouteServiceError(
                "Google Routes is temporarily unavailable."
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "Google Routes matrix request failed: mode=%s error_type=%s elapsed_ms=%s",
                travel_mode,
                type(exc).__name__,
                round((time.monotonic() - started_at) * 1000),
            )
            raise RouteServiceError(
                "Google Routes is temporarily unavailable."
            ) from exc

        results: list[RouteResult | None] = [None] * len(destinations)
        try:
            decoded = response.json()
            if isinstance(decoded, list):
                rows = decoded
            elif isinstance(decoded, dict):
                rows = [decoded]
            else:
                raise ValueError
        except ValueError:
            try:
                rows = [
                    json.loads(line)
                    for line in response.text.splitlines()
                    if line.strip()
                ]
            except ValueError as exc:
                raise RouteServiceError(
                    "Google Routes returned an invalid matrix response."
                ) from exc
        if not rows:
            raise RouteServiceError("Google Routes returned an empty matrix response.")
        logger.info(
            "Google Routes matrix response: mode=%s status=%s rows=%s elapsed_ms=%s",
            travel_mode,
            response.status_code,
            len(rows),
            round((time.monotonic() - started_at) * 1000),
        )
        for row in rows:
            if not isinstance(row, dict):
                raise RouteServiceError("Google Routes returned an invalid matrix row.")
            index = row.get("destinationIndex")
            if not isinstance(index, int) or not 0 <= index < len(destinations):
                raise RouteServiceError(
                    "Google Routes returned an invalid matrix index."
                )
            condition = row.get("condition")
            if condition == "ROUTE_NOT_FOUND":
                continue
            status = row.get("status")
            if isinstance(status, dict) and status.get("code", 0) not in (0, None):
                # An individual route failure is not evidence of reachability.
                continue
            distance = row.get("distanceMeters")
            duration = self._duration_seconds(row.get("duration"))
            if (
                condition not in (None, "ROUTE_EXISTS")
                or not isinstance(distance, (int, float))
                or duration is None
            ):
                continue
            results[index] = RouteResult(
                travel_mode=travel_mode,
                distance_metres=max(0, int(round(distance))),
                duration_seconds=duration,
            )
        logger.info(
            "Google Routes matrix parsed: mode=%s routable=%s unroutable=%s",
            travel_mode,
            sum(result is not None for result in results),
            sum(result is None for result in results),
        )
        return results

    def matrix_with_fallbacks(
        self,
        origin: tuple[float, float],
        destinations: list[tuple[float, float]],
        *,
        travel_modes: list[SupportedTravelMode],
    ) -> list[RouteResult | None]:
        """Route in preference order, querying backups only for unresolved places."""
        if not travel_modes:
            raise ValueError("At least one travel mode is required.")
        if len(set(travel_modes)) != len(travel_modes):
            raise ValueError("Travel modes must not contain duplicates.")
        resolved: list[RouteResult | None] = [None] * len(destinations)
        unresolved = list(range(len(destinations)))
        for mode in travel_modes:
            if not unresolved:
                break
            partial = self.matrix(
                origin,
                [destinations[index] for index in unresolved],
                travel_mode=mode,
            )
            next_unresolved: list[int] = []
            for index, result in zip(unresolved, partial, strict=True):
                if result is None:
                    next_unresolved.append(index)
                else:
                    resolved[index] = result
            unresolved = next_unresolved
        return resolved

    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        travel_mode: SupportedTravelMode,
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
                    "Google Routes preview authorization failed: status=%s",
                    exc.response.status_code,
                )
                raise RouteConfigurationError(
                    "Google Routes credentials are invalid or not authorized."
                ) from exc
            logger.warning(
                "Google Routes preview request failed: status=%s",
                exc.response.status_code,
            )
            raise RouteServiceError(
                "Google Routes is temporarily unavailable."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Google Routes preview request failed: %s", type(exc).__name__
            )
            raise RouteServiceError(
                "Google Routes is temporarily unavailable."
            ) from exc
        if not isinstance(route_payload, dict):
            raise RouteServiceError("Google Routes returned an invalid route response.")
        routes = route_payload.get("routes")
        if not isinstance(routes, list) or not routes:
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
        return RouteResult(
            travel_mode=travel_mode,
            distance_metres=max(0, int(round(distance))),
            duration_seconds=duration,
            encoded_polyline=encoded_polyline,
        )

    def summary(
        self, origin: tuple[float, float], destination: tuple[float, float]
    ) -> dict:
        """Compatibility summary for existing callers, now backed by Google Routes."""
        walking = self.route(origin, destination, travel_mode="walking")
        transit = self.route(origin, destination, travel_mode="public_transport")
        return {
            "walking": self._summary_value(walking),
            "transit": self._summary_value(transit),
        }

    @staticmethod
    def _summary_value(result: RouteResult | None) -> dict[str, int | bool]:
        if result is None:
            return {"available": False}
        return {
            "available": True,
            "distance_metres": result.distance_metres,
            "duration_seconds": result.duration_seconds,
        }

    def walking_matrix(
        self,
        origin: tuple[float, float],
        destinations: list[tuple[float, float]],
    ) -> list[tuple[float | None, float | None]]:
        """Legacy walking matrix adapter backed by authoritative Google routes."""
        return [
            (result.distance_metres, result.duration_seconds)
            if result is not None
            else (None, None)
            for result in self.matrix(origin, destinations, travel_mode="walking")
        ]


def enrich_with_walking_routes(
    origin: tuple[float, float],
    candidates: list[PlaceCandidate],
    *,
    max_walking_minutes: int | None,
    categories: list[str],
) -> list[PlaceCandidate]:
    """Keep only candidates with a verified walking route (legacy adapter)."""
    if not candidates:
        return []

    shortlist = rank_place_candidates(candidates, categories, limit=OSRM_MATRIX_LIMIT)
    router = RouteProvider()
    matrix = router.walking_matrix(
        origin, [(c.latitude, c.longitude) for c in shortlist]
    )

    routed: dict[str, PlaceCandidate] = {}
    for candidate, pair in zip(shortlist, matrix, strict=False):
        distance, duration = pair
        if distance is None or duration is None:
            continue
        walking_minutes = max(1, int(math.ceil(duration / 60.0)))
        if max_walking_minutes is not None and walking_minutes > max_walking_minutes:
            continue
        routed[candidate.provider_id] = replace(
            candidate,
            distance_metres=int(round(distance)),
            walking_minutes=walking_minutes,
            distance_source=DistanceSource.walking_route,
        )

    # Only matrix-shortlisted destinations have verified route data.
    final: list[PlaceCandidate] = []
    seen: set[str] = set()
    for candidate in shortlist:
        updated = routed.get(candidate.provider_id)
        if updated is None:
            continue
        if (
            max_walking_minutes is not None
            and updated.walking_minutes > max_walking_minutes
        ):
            continue
        final.append(updated)
        seen.add(updated.provider_id)

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
