#!/usr/bin/env bash
# 一条命令生成整局 transcript、分句语音、full.mp3 和 timeline，并自动核验。
# 用法见 tools/tts/README.md；参数原样传给 pipeline.py。
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --no-project --with edge-tts==7.3.1 --with numpy python3 "$here/pipeline.py" "$@"
