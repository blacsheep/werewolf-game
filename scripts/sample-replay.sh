#!/usr/bin/env bash
# Build the web replay of examples/sample-game without calling any model:
# scripts/sample-replay.sh, then open games/sample/replay/index.html.
#
# The sample has no audio, so it gets a silent track as long as its timeline.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
game="$root/games/sample"
rm -rf "$game"
mkdir -p "$game/audio" && chmod 700 "$game"
cp "$root"/examples/sample-game/* "$game/" && chmod 600 "$game/events.jsonl"
duration=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["duration"])' "$game/timeline.json")
ffmpeg -loglevel error -f lavfi -i anullsrc=r=24000:cl=mono -t "$duration" -c:a libmp3lame -b:a 32k "$game/audio/full.mp3"
uv run --no-project --with pillow python "$root/tools/replay/build.py" --game "$game" --web-only >/dev/null
echo "$game/replay/index.html"
