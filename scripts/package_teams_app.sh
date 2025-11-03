#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT_DIR/infra/teams-app/manifest.template.json"
OUT_DIR="$ROOT_DIR/infra/teams-app/dist"
OUT_MANIFEST="$OUT_DIR/manifest.json"

mkdir -p "$OUT_DIR"

# Load .env into environment
set -a
source "$ROOT_DIR/.env"
set +a

# Substitute ${VARS} in manifest
# Use Python for portability (no GNU envsubst dependency)
python - "$TEMPLATE" "$OUT_MANIFEST" <<'PY'
import json, os, re, sys, pathlib
src = pathlib.Path(sys.argv[1]).read_text()
def sub(match):
    key = match.group(1)
    val = os.environ.get(key)
    if val is None:
        raise SystemExit(f"Missing env var: {key}")
    return val
out = re.sub(r"\$\{([A-Z0-9_]+)\}", sub, src)
pathlib.Path(sys.argv[2]).write_text(out)
print(f"Wrote {sys.argv[2]}")
PY

# Zip manifest + icons as the app package
pushd "$ROOT_DIR/infra/teams-app" >/dev/null
zip -q -r "$OUT_DIR/teams-app.zip" "color.png" "outline.png" "dist/manifest.json"
popd >/dev/null

echo "✅ Teams app package created at: $OUT_DIR/teams-app.zip"
 