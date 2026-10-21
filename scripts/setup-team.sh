#!/usr/bin/env bash
# One-time setup: scripts/setup-team.sh <model>
#
# <model> is an agt model resource (see scripts/setup-models.sh); the lead runs on
# it and offers it as the default for the God and the players. This writes
# everything machine-specific into .agt (gitignored): workspace settings, the
# engine's MCP servers, the two coding harnesses, the prompts and skills, and the
# lead. Safe to re-run. The God, players, toolsmith, voice and replay agents are
# created by the lead.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export AGT_HOME="$root/.agt"
model="${1:?usage: scripts/setup-team.sh <model>   (an agt model; see scripts/setup-models.sh)}"
exists() { agt "$1" info "$2" >/dev/null 2>&1; }
exists model "$model" || { echo "no model '$model'; create it with scripts/setup-models.sh" >&2; exit 1; }

agt config show > "$root/.agt/config.yaml.new"
python3 - "$root/.agt/config.yaml.new" "$model" "${TIMEZONE:-Asia/Shanghai}" <<'EOF'
import json, sys
path, model, timezone = sys.argv[1:]
config = json.load(open(path))
config["scheduler"].update(concurrency=4, discussion_seconds=None, replies_per_agent=None, timezone=timezone)
# A dedicated home whose only MCP tools are our own engine: approvals would stall a
# game of several hundred tool calls, so everything runs without asking.
config["permissions"].update(mode="auto_allow", harness_mode="auto_allow", reviewer_model=f"default/{model}", request_timeout_seconds=3600)
json.dump(config, open(path, "w"), indent=2)
EOF
agt config apply "$root/.agt/config.yaml.new" >/dev/null && rm "$root/.agt/config.yaml.new"

# One engine MCP server per seat. The lead later sets the God's WW_CONVERSATIONS,
# so an existing server is left alone.
for seat in god p01 p02 p03 p04 p05 p06 p07 p08 p09 p10 p11 p12; do
  name="ww-$seat"
  exists mcp "$name" && continue
  cat > "$root/.agt/$name.json" <<EOF
{"mcpServers": {"$name": {"type": "stdio", "command": "$(command -v python3)",
  "args": ["$root/engine/server.py"], "cwd": "$root",
  "env": {"WW_SEAT": "$seat", "WW_GAMES_DIR": "$root/games", "WW_CONVERSATIONS": "{}"}}}}
EOF
  agt mcp create "$name" --file "$root/.agt/$name.json" >/dev/null
  rm "$root/.agt/$name.json"
done

harness() {
  local name="$1" engine="$2" executable="$3" description="$4"
  agt harness resource info "$name" >/dev/null 2>&1 && return 0
  [[ -n "$executable" ]] || { echo "skip harness $name: $engine is not installed"; return 0; }
  cat > "$root/.agt/$name.json" <<EOF
{"engine":"$engine","executable":"$executable","working_directory":"$root","sandbox":"workspace_write",
 "reasoning_effort":"high","timeout_seconds":3600,"description":"$description"}
EOF
  agt harness resource create "$name" --file "$root/.agt/$name.json" >/dev/null
  rm "$root/.agt/$name.json"
  agt harness resource test "$name" >/dev/null
}
harness codex codex "$(command -v codex || true)" "Codex in the werewolf-game project: writes and tests code"
harness claude-code claude-code "$(command -v claude || true)" "Claude Code in the werewolf-game project: writes and runs scripts"

# Prompts and skills live in prompts/ and docs/skills/; {{root}} becomes this checkout.
render() { sed "s|{{root}}|$root|g" "$1" > "$root/.agt/rendered.md"; }
for prompt in ww-god ww-player toolsmith voice replay; do
  render "$root/prompts/$prompt.md"
  if exists prompt "$prompt"; then
    agt prompt update "$prompt" --file "$root/.agt/rendered.md" --yes >/dev/null
  else
    agt prompt create "$prompt" --file "$root/.agt/rendered.md" >/dev/null
  fi
done
for skill in ww-playbook werewolf-game; do
  file="$root/docs/skills/$skill/SKILL.md"
  exists skill "$skill" || agt skill create "$skill" --description "$(sed -n 's/^description: //p' "$file")" >/dev/null
  render "$file"
  agt skill update "$skill" --file "$root/.agt/rendered.md" >/dev/null
done
rm "$root/.agt/rendered.md"

# The lead runs a whole game from one request through the werewolf-game skill.
exists agent lead || agt agent create lead --model "$model" --prompt default/lead \
  --builtin-tool resource_management --builtin-tool task_collaboration \
  --description "Builds and runs the werewolf team" >/dev/null
agt agent info lead --output json | grep -q '"werewolf-game"' || agt agent update lead --add-skill werewolf-game >/dev/null
agt im principal info web-operator >/dev/null 2>&1 || agt im principal create web-operator --name "我" >/dev/null
agt im owner ensure web-operator >/dev/null
agt im principal info lead >/dev/null 2>&1 || {
  agt tool call --principal web-operator --manager principal_create --args '{"id":"lead","name":"Lead","kind":"agent"}' >/dev/null
  agt tool call --principal web-operator --manager agent_bind --args '{"principal":"lead","agent":"lead"}' >/dev/null
}
echo "ready: lead on $model with skill werewolf-game; engine MCP servers, harnesses, prompts and skills installed"
