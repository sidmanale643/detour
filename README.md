# Detour

Detour is a mobile-first, honor-system PWA for personalized real-world quests. Players receive a six-slot daily quest deck, complete a quest with one tap, and earn global and category XP.

## Stack

- `web/`: Next.js + TypeScript PWA
- `api/`: FastAPI + Python, managed with UV
- SQLite for the runnable local MVP, behind a persistence boundary intended for PostgreSQL/PostGIS
- Provider boundaries for Redis and Dramatiq background generation
- MapLibre with OpenStreetMap tiles, Nominatim search, and Overpass places
- Google Routes for verified travel times and route previews across every supported mode
- Google Places plus Wikidata for live Discover food venues and landmark context
- OpenRouter for structured quest personalization

There is intentionally no proof upload, camera access, completion-location verification, or verifier service. Quest completion is an irreversible honor-system action. During home setup, a player can explicitly choose to save their current device location as home. Later device-location use stays in browser memory and is sent to the authenticated API only after the player requests a route preview. Travel preferences support walking, cycling, two-wheelers, four-wheelers, and public transport. Running is an activity style, not a travel mode.

Authentication is currently bypassed by default so the local app opens directly to the quest map. Set `NEXT_PUBLIC_AUTH_DISABLED=false` in `web/.env.local` to restore registration and login.

## Local development

### Prerequisites

- Node.js 20 or newer and npm
- Python 3.12 or newer and [UV](https://docs.astral.sh/uv/)
- No local database or queue service is required for the current MVP

1. Copy the environment template and fill in the values required for the features you use:

   ```sh
   cp .env.example .env
   ```

2. Initialize the backend according to `api/README.md`. It creates its local SQLite database automatically.

3. Install and run the frontend:

   ```sh
   cd web
   npm install
   npm run dev
   ```

4. In another terminal, run the backend:

   ```sh
   cd api
   uv sync
   uv run uvicorn app.main:app --reload --port 8000
   ```

   See `api/README.md` for the complete endpoint and configuration notes.

Once both packages are configured, `./scripts/dev.sh` starts their normal development commands together and stops both processes when interrupted.

## API contract baseline

The web client consumes versioned `/v1` JSON endpoints. The implementation should retain these baseline operations:

| Operation | Method and path | Intent |
| --- | --- | --- |
| Today's deck | `GET /v1/decks/today` | Return six stable daily quest slots and refresh availability. |
| Refresh deck | `POST /v1/decks/today/refresh` | Replace eligible incomplete slots once per local day. |
| Complete quest | `POST /v1/quests/{id}/complete` | Complete one offered quest and award XP exactly once. Requires `Idempotency-Key`. |
| Skip quest | `POST /v1/quests/{id}/skip` | Mark an offered quest unavailable without XP. |
| Progress | `GET /v1/progress` | Return global XP, level, category progress, and streak. |
| History | `GET /v1/history` | Return private completion history. |

Quest states are `offered`, `completed`, `skipped`, `superseded`, and `expired`. Completion is server-authoritative and idempotent: repeated requests return the original completion result and cannot issue more XP.

Authentication, profile/preferences, home-zone selection, password recovery, session management, and account deletion also live under `/v1`. The API owns the stored IANA timezone, expiry rules, XP calculation, and mutable quest state. The client sends a precise location only when the player explicitly saves it as home or requests a route. It must not send proof, camera data, completion locations, or client-calculated XP.

## Environment

Root `.env` is for local coordination only. Package-specific runtime configuration may be loaded by `web/` and `api/`; keep secrets out of the repository and copy values into the package configuration mechanism used by each app.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Optional | Future PostgreSQL/PostGIS connection string; local mode uses SQLite. |
| `REDIS_URL` | Optional | Future Redis connection string for Dramatiq workers. |
| `WEB_ORIGIN` | Yes for API | Allowed local web origin, normally `http://localhost:3000`. |
| `NEXT_PUBLIC_API_BASE_URL` | Yes for web | Browser API base URL, normally `http://localhost:8000`. |
| `NEXT_PUBLIC_GAME_MAP_TILEJSON_URL` | Optional | OpenMapTiles-compatible TileJSON source for the illustrated, label-free gameplay map. Defaults to OpenFreeMap's planet source. |
| `NEXT_PUBLIC_OSM_TILE_URL` | Optional | OSM-compatible raster tile template. Defaults to the public OSM tile service for local development. |
| `DETOUR_NOMINATIM_URL` | Optional | Nominatim endpoint for address search. |
| `DETOUR_OVERPASS_URL` | Optional | Overpass endpoint for nearby public places. |
| `DETOUR_OSM_USER_AGENT` | Yes for API | Identifies the application and provides a real contact for OSM services. |
| `DETOUR_GOOGLE_ROUTES_KEY` | Yes for generated decks | Server-only Google Routes API key for verified matching and previews. |
| `DETOUR_GOOGLE_ROUTES_URL` | Optional | Google Routes API base URL. |
| `DETOUR_GOOGLE_ROUTES_TIMEOUT_SECONDS` | Optional | Google Routes request timeout in seconds. |
| `DETOUR_GOOGLE_PLACES_KEY` | Optional | Server-only Google Places API key for live food discovery. Enable Places API (New), restrict the key, and never expose it to the browser. Without it, Discover still returns OSM/Wikidata places and shows food as unavailable. |
| `DETOUR_WIKIDATA_SPARQL_URL` | Optional | Wikidata Query Service endpoint used for regional landmark discovery. |
| `OPENROUTER_API_KEY` | Yes for generated decks | OpenRouter key. Never expose to the browser. |
| `OPENROUTER_QUEST_MODEL` | Yes for generated decks | Structured-output-capable OpenRouter model ID. |
| `JWT_SECRET` | Yes for API | High-entropy signing secret. |
| `REFRESH_TOKEN_PEPPER` | Yes for API | Separate high-entropy secret for refresh-token hashing. |
| `APP_ENV` | Yes | Runtime environment such as `development`. |

Use separate restricted provider credentials for local development. Never place private provider tokens in variables prefixed `NEXT_PUBLIC_`.

## Map behavior

The Map tab uses MapLibre with a configurable OpenMapTiles-compatible vector source for a pitched, illustrated, label-free game world that can expand into a full-screen exploration view. It keeps visible OpenStreetMap attribution. The home area picker deliberately remains a conventional MapLibre raster map using `NEXT_PUBLIC_OSM_TILE_URL`. Quest matching and previews use Google Routes for walking, cycling, two-wheelers, four-wheelers, and public transport. Quest beacons, status changes, player location, and route previews are display features only and never gate completion. Both maps retain a stylized fallback if their configured tile service cannot load.

The public OpenStreetMap, Nominatim, and Overpass endpoints are development defaults, not an unlimited production backend. Use hosted or self-hosted endpoints, retain visible OpenStreetMap attribution, identify server requests, cache responsibly, and follow each service's usage policy.

Home setup accepts a searched address, a map-pinned point, or the device's live location after explicit browser permission. The chosen home label, input method, exact point, and server-derived resolution-7 H3 zone are stored on the player's account. No location is collected in the background and completion locations are never stored.

## Developer checks

Run package checks from their owning directories once implemented:

```sh
cd web && npm run lint && npm run typecheck && npm run build
cd api && uv run ruff check . && uv run python -m compileall -q .
```

`./scripts/check.sh` runs the available conventional checks without installing dependencies or writing project files.
