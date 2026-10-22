#!/usr/bin/env bash
# Start the agt Web console (and scheduler) on this project's home.
# The link with its token goes to recording/console.log for scripts/record.py.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export AGT_HOME="$root/.agt"
mkdir -p "$root/recording"
# AGT_BIN runs another build, such as a branch under test.
exec "${AGT_BIN:-agt}" web --port "${PORT:-4317}" > "$root/recording/console.log" 2>&1
