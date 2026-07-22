#!/usr/bin/env bash
# Start both packages for local development. Each package owns its exact dev command.
set -euo pipefail

# Give each background service its own process group so cleanup can terminate
# the complete Next/FastAPI process tree, including reload workers.
set -m

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
web_dir="$root_dir/web"
api_dir="$root_dir/api"

if [[ ! -d "$web_dir" || ! -d "$api_dir" ]]; then
  echo "Expected web/ and api/ directories in $root_dir" >&2
  exit 1
fi

cleanup() {
  trap - EXIT INT TERM
  for service_pid in "${web_pid:-}" "${api_pid:-}"; do
    [[ -n "$service_pid" ]] || continue
    kill -TERM -- "-$service_pid" 2>/dev/null || true
  done
  sleep 1
  for service_pid in "${web_pid:-}" "${api_pid:-}"; do
    [[ -n "$service_pid" ]] || continue
    kill -KILL -- "-$service_pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Export root .env so both packages see provider keys (e.g. OpenRouter).
if [[ -f "$root_dir/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$root_dir/.env"
  set +a
fi
if [[ -f "$api_dir/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$api_dir/.env"
  set +a
fi

(cd "$web_dir" && exec npm run dev) &
web_pid=$!
(cd "$api_dir" && exec uv run fastapi dev app.main:app --port 8000) &
api_pid=$!

wait -n "$web_pid" "$api_pid"
