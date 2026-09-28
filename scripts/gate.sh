#!/usr/bin/env bash
# The gate, in one place: what CI runs and what a contributor runs locally are
# then the same commands, not two lists that drift.
set -euo pipefail

# The gate reads dates and formats numbers, so a run must not depend on the
# runner's zone or locale. CI sets both on the job; setting them here too is
# what makes a local run answer the same questions the CI run does.
export TZ=UTC
export LC_ALL=C.UTF-8

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$dir")"
cd "$ROOT"

# --locked, not --frozen: it asserts uv.lock still matches pyproject.toml, so
# a declared gate tool that never reached the lock fails here instead of the
# previous one running under its name.
uv sync --extra dev --locked
uv run black --check .
uv run ruff check .
uv run mypy
uv run pytest
shellcheck install.sh scripts/gate.sh
