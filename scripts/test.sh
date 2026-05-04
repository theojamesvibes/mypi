#!/usr/bin/env bash
# Local test runner. Mirrors what CI does in .github/workflows/test.yml.
# Usage: ./scripts/test.sh [pytest args...]
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -d ".venv" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Sanity-check that dev deps are installed; install on demand if not.
if ! python -c "import pytest" >/dev/null 2>&1; then
  echo "pytest not found — installing dev dependencies..."
  pip install -r requirements-dev.txt
fi

exec pytest "$@"
