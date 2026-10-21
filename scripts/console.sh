#!/usr/bin/env bash
# Start the agt Web console (and scheduler) on this project's home.
# The link with its token goes to recording/console.log for scripts/record.py.
#
# Where systemd is available, agt and everything it starts (Codex, Claude Code, MCP
# servers, ffmpeg, Chrome) run in one memory-capped scope, so a runaway job is killed
# instead of the whole machine running out of memory. Override with MEMORY_MAX /
# SWAP_MAX, or MEMORY_MAX=none to run without a cap.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export AGT_HOME="$root/.agt"
mkdir -p "$root/recording"
# AGT_BIN runs another build, such as a branch under test.
cmd=("${AGT_BIN:-agt}" web --port "${PORT:-4317}")
if [[ "${MEMORY_MAX:-1800M}" != none ]] && command -v systemd-run >/dev/null; then
  cmd=(systemd-run --user --scope -q -p "MemoryMax=${MEMORY_MAX:-1800M}" -p "MemorySwapMax=${SWAP_MAX:-512M}" "${cmd[@]}")
fi
exec "${cmd[@]}" > "$root/recording/console.log" 2>&1
