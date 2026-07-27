# Detour API

Run locally with `uv run --project api uvicorn app.main:app --app-dir api --reload`.

The default SQLite database is created at `api/data/detour.db`; it is a local-development adapter. The API has one persistent anonymous local player, created with default preferences on first use, and no credentials, sessions, or request headers are required. Nominatim handles address lookup and Overpass supplies nearby OpenStreetMap places. Deck create/refresh uses `QuestGenerationService` with OpenRouter structured output only (up to 5 quests per call). There is no template or dummy fallback — missing key or generation failure returns HTTP 503. Generated quests have no time windows (anytime). PostgreSQL/PostGIS, Dramatiq/Redis, and Google Routes remain later production adapters.

## Quest generation env

| Variable | Purpose |
| --- | --- |
| `DETOUR_OPENROUTER_API_KEY` | Enables LLM quest generation |
| `DETOUR_OPENROUTER_QUEST_MODEL` | Structured-output model (default `openai/gpt-4o-mini`) |
| `DETOUR_OPENROUTER_BASE_URL` | Default `https://openrouter.ai/api/v1` |
| `DETOUR_OPENROUTER_TIMEOUT_SECONDS` | Default `45` |

```sh
cd api
uv sync
uv run python -m unittest discover -s tests -v
```

The OpenAPI schema is available at `/docs` and `/openapi.json`.
