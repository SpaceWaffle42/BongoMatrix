#!/bin/sh
# Regenerate Astronomy pixel art with Aseprite (headless CLI is enough).
# Usage: ASEPRITE=/path/to/aseprite tools/aseprite/build_sprites.sh
set -eu
A="${ASEPRITE:-aseprite}"
cd "$(dirname "$0")/../.."
S=resources/astronomy/sprites
mkdir -p "$S"
"$A" -b --script-param "out=$S" --script tools/aseprite/moon_sprites.lua
for f in moon_28 moon_16 event_icons; do
  "$A" -b "$S/$f.aseprite" --sheet "$S/$f.png" --sheet-type horizontal >/dev/null
done
chmod 644 "$S"/*
