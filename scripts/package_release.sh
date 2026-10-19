#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION")"
DIST_DIR="$ROOT_DIR/dist"
PACKAGE_NAME="WhatSticksMCP-v${VERSION}"
STABLE_PACKAGE_NAME="WhatSticksMCP"
STAGING_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

mkdir -p "$DIST_DIR"
rm -f \
  "$DIST_DIR/${PACKAGE_NAME}.zip" \
  "$DIST_DIR/${PACKAGE_NAME}.sha256" \
  "$DIST_DIR/${STABLE_PACKAGE_NAME}.zip" \
  "$DIST_DIR/${STABLE_PACKAGE_NAME}.sha256"
mkdir -p "$STAGING_DIR/$PACKAGE_NAME"
cp "$ROOT_DIR/server.py" "$ROOT_DIR/README.md" "$ROOT_DIR/VERSION" "$STAGING_DIR/$PACKAGE_NAME/"
cp "$ROOT_DIR/__init__.py" "$STAGING_DIR/$PACKAGE_NAME/"

(cd "$STAGING_DIR" && zip -q -r "$DIST_DIR/${PACKAGE_NAME}.zip" "$PACKAGE_NAME")
shasum -a 256 "$DIST_DIR/${PACKAGE_NAME}.zip" | awk '{print $1}' > "$DIST_DIR/${PACKAGE_NAME}.sha256"
cp "$DIST_DIR/${PACKAGE_NAME}.zip" "$DIST_DIR/${STABLE_PACKAGE_NAME}.zip"
cp "$DIST_DIR/${PACKAGE_NAME}.sha256" "$DIST_DIR/${STABLE_PACKAGE_NAME}.sha256"
printf 'Created %s\nSHA-256: ' "$DIST_DIR/${PACKAGE_NAME}.zip"
cat "$DIST_DIR/${PACKAGE_NAME}.sha256"
printf 'Stable alias: %s\n' "$DIST_DIR/${STABLE_PACKAGE_NAME}.zip"
