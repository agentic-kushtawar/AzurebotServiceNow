#!/usr/bin/env bash
set -euo pipefail

if command -v uv >/dev/null 2>&1; then
  echo "➡ Using $(uv --version)"

  # create pyproject if missing
  if [ ! -f pyproject.toml ]; then
    uv init --package .
  fi

  # create venv if missing
  if [ ! -d .venv ]; then
    uv venv .venv
  fi

  # activate and install deps
  source .venv/bin/activate
  uv add fastapi "uvicorn[standard]" loguru
else
  echo "⚠ uv not found in PATH, falling back to pip"
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install --upgrade pip
  pip install fastapi "uvicorn[standard]" loguru
fi

echo "✅ Bootstrap complete.
- Activate: source .venv/bin/activate
- Run:      ./scripts/dev.sh"
