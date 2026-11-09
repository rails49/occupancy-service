#!/bin/bash
# The checks and tests a change must pass. The pre-push hook runs this.
set -e

cd "$(dirname "$0")/.."

echo "🐍 Running Python checks..."
uv sync --locked --quiet
uv run ruff check .
uv run black --check .
uv run pyright

echo "🐳 Checking the compose project..."
BOX_DOMAIN=box.example OCCUPANCY_ARCHIVE=/srv/layout.r49 \
  OCCUPANCY_CAMERA=/dev/v4l/by-id/camera TC49_UID=1000 TC49_GID=1000 \
  VIDEO_GID=44 RENDER_GID=992 docker compose -f compose.yaml config --quiet

echo "🧪 Running Python tests..."
uv run pytest -q

echo "🎉 All checks passed."
