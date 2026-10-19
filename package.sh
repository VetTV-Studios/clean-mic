#!/bin/sh
# Build the Decky "install from zip" package:  ./package.sh [OUT_DIR]
# Produces OUT_DIR/clean-mic-<version>.zip and OUT_DIR/clean-mic.zip (same bytes).
set -eu
cd "$(dirname "$0")"
OUT_DIR="${1:-$(pwd)/out}"
VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' package.json | head -n 1)"
[ -f dist/index.js ] || { echo "run 'pnpm build' first" >&2; exit 1; }
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT INT TERM
P="$STAGE/Clean Mic"
mkdir -p "$P/dist"
cp dist/index.js "$P/dist/"
cp main.py cleanmic.py plugin.json package.json README.md CHANGELOG.md LICENSE "$P/"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/clean-mic-$VERSION.zip"
rm -f "$OUT"
(cd "$STAGE" && python3 -m zipfile -c "$OUT" "Clean Mic")
cp "$OUT" "$OUT_DIR/clean-mic.zip"
echo "$OUT"
