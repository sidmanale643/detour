#!/usr/bin/env bash
# Run conventional checks only when their project configuration is present.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$root_dir/web/package.json" ]]; then
  (cd "$root_dir/web" && npm run lint && npm run typecheck && npm run build)
else
  echo "Skipping web checks: web/package.json is not present."
fi

if [[ -f "$root_dir/api/pyproject.toml" ]]; then
  (cd "$root_dir/api" && uv run ruff check . && uv run python -m compileall -q .)
else
  echo "Skipping API checks: api/pyproject.toml is not present."
fi
