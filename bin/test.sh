#!/bin/bash
# The checks and tests a change must pass. The pre-push hook runs this.
set -e

cd "$(dirname "$0")/.."

echo "🐍 Running Python checks..."
uv sync --locked --quiet
uv run ruff check .
uv run black --check .
uv run pyright

echo "🧪 Running Python tests..."
uv run pytest -q

echo "🎉 All checks passed."
