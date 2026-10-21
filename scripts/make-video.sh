#!/usr/bin/env bash
# Turn a recording's frames into a sped-up mp4 and an animated webp.
# Usage: scripts/make-video.sh <name> [fps]   (frames from recording/<name>/frames)
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
dir="$root/recording/$1"; fps="${2:-10}"
ffmpeg -y -loglevel error -framerate "$fps" -i "$dir/frames/%06d.png" \
  -vf "scale=1280:-2" -c:v libx264 -pix_fmt yuv420p -crf 23 "$dir/$1.mp4"
ffmpeg -y -loglevel error -framerate "$fps" -i "$dir/frames/%06d.png" \
  -vf "scale=960:-2" -loop 0 -c:v libwebp -quality 60 "$dir/$1.webp"
ls -lh "$dir/$1.mp4" "$dir/$1.webp"
