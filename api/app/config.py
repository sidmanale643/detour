from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Load api/.env then repo-root .env into os.environ (no overwrite)."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / ".env",  # api/.env
        here.parents[2] / ".env",  # repo root .env
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and value and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DETOUR_DATABASE_URL", "sqlite:///./data/detour.db")
    nominatim_url: str = os.getenv(
        "DETOUR_NOMINATIM_URL", "https://nominatim.openstreetmap.org"
    ).rstrip("/")
    overpass_url: str = os.getenv(
        "DETOUR_OVERPASS_URL", "https://overpass-api.de/api/interpreter"
    )
    # Public Overpass mirrors are rate-limited and sometimes return 406/5xx.
    # Discover and quest generation both walk this list until one succeeds.
    overpass_fallback_urls: tuple[str, ...] = tuple(
        url.strip().rstrip("/")
        for url in os.getenv(
            "DETOUR_OVERPASS_FALLBACK_URLS",
            ",".join(
                (
                    "https://overpass.kumi.systems/api/interpreter",
                    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
                )
            ),
        ).split(",")
        if url.strip()
    )
    osm_user_agent: str = os.getenv(
        "DETOUR_OSM_USER_AGENT",
        "Detour/0.1 (+https://github.com/detour; contact: maps@detour.local)",
    )
    google_places_key: str | None = os.getenv("DETOUR_GOOGLE_PLACES_KEY")
    google_routes_key: str | None = os.getenv("DETOUR_GOOGLE_ROUTES_KEY")
    google_routes_url: str = os.getenv(
        "DETOUR_GOOGLE_ROUTES_URL", "https://routes.googleapis.com"
    ).rstrip("/")
    google_routes_timeout_seconds: float = float(
        os.getenv("DETOUR_GOOGLE_ROUTES_TIMEOUT_SECONDS", "15")
    )
    osrm_base_url: str = os.getenv(
        "DETOUR_OSRM_BASE_URL",
        os.getenv(
            "NEXT_PUBLIC_OSRM_BASE_URL",
            "https://routing.openstreetmap.de/routed-foot",
        ),
    ).rstrip("/")
    openrouter_api_key: str | None = os.getenv(
        "DETOUR_OPENROUTER_API_KEY"
    ) or os.getenv("OPENROUTER_API_KEY")
    openrouter_base_url: str = os.getenv(
        "DETOUR_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    ).rstrip("/")
    openrouter_quest_model: str = os.getenv(
        "DETOUR_OPENROUTER_QUEST_MODEL",
        os.getenv("OPENROUTER_QUEST_MODEL", "openai/gpt-4o-mini"),
    )
    openrouter_timeout_seconds: float = float(
        os.getenv("DETOUR_OPENROUTER_TIMEOUT_SECONDS", "45")
    )
    openrouter_site_url: str = os.getenv(
        "DETOUR_OPENROUTER_SITE_URL", "https://github.com/detour"
    )
    openrouter_app_name: str = os.getenv("DETOUR_OPENROUTER_APP_NAME", "Detour")

    @property
    def sqlite_path(self) -> Path:
        if not self.database_url.startswith("sqlite:///"):
            raise RuntimeError(
                "LocalDatabase only supports sqlite. Configure a PostgreSQL adapter for this URL."
            )
        raw = self.database_url.removeprefix("sqlite:///")
        return Path(raw).expanduser().resolve()


settings = Settings()
