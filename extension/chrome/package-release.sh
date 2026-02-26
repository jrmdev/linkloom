#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_PATH="$SCRIPT_DIR/manifest.json"
DIST_DIR="$SCRIPT_DIR/dist"

if ! command -v zip >/dev/null 2>&1; then
  echo "error: zip command not found. Install zip and try again." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 command not found. Install Python 3 and try again." >&2
  exit 1
fi

if [[ ! -f "$MANIFEST_PATH" ]]; then
  echo "error: manifest not found at $MANIFEST_PATH" >&2
  exit 1
fi

VERSION="$(python3 - "$MANIFEST_PATH" <<'PY'
import json
import sys

manifest_path = sys.argv[1]
with open(manifest_path, "r", encoding="utf-8") as fh:
    manifest = json.load(fh)

version = str(manifest.get("version", "")).strip()
if not version:
    raise SystemExit("manifest version is missing")

print(version)
PY
)"

PACKAGE_BASENAME="linkloom-chrome-${VERSION}"
PACKAGE_PATH="$DIST_DIR/${PACKAGE_BASENAME}.zip"
CHECKSUM_PATH="$DIST_DIR/${PACKAGE_BASENAME}.sha256"

mkdir -p "$DIST_DIR"
rm -f "$PACKAGE_PATH" "$CHECKSUM_PATH"

(
  cd "$SCRIPT_DIR"
  zip -X -r "$PACKAGE_PATH" . \
    -x "dist/*" \
    -x "README.md" \
    -x "package-release.sh" \
    -x "*.pyc" \
    -x "__pycache__/*" >/dev/null
)

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DIST_DIR" && sha256sum "${PACKAGE_BASENAME}.zip" > "${PACKAGE_BASENAME}.sha256")
elif command -v shasum >/dev/null 2>&1; then
  (cd "$DIST_DIR" && shasum -a 256 "${PACKAGE_BASENAME}.zip" > "${PACKAGE_BASENAME}.sha256")
fi

echo "Built package: $PACKAGE_PATH"
if [[ -f "$CHECKSUM_PATH" ]]; then
  echo "Wrote checksum: $CHECKSUM_PATH"
fi
