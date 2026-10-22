#!/usr/bin/env bash
# Create (or update) one provider and one model resource per block in models.env.
# Resource names are lowercase block names: chatgpt, claude, deepseek, ...
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export AGT_HOME="$root/.agt"
set -a; source "$root/models.env"; set +a

# Every <NAME>_BASE_URL in models.env is one block, so you can add your own.
mapfile -t names < <(sed -n 's/^\([A-Z0-9]*\)_BASE_URL=.*/\1/p' "$root/models.env")
for n in "${names[@]}"; do
  base="${n}_BASE_URL"; key="${n}_API_KEY"; model="${n}_MODEL"; fmt="${n}_FORMAT"; effort="${n}_EFFORT"
  name="$(tr '[:upper:]' '[:lower:]' <<<"$n")"
  # A block may leave out a line entirely, so read with :- under set -u.
  if [[ -z "${!base:-}" || -z "${!key:-}" || -z "${!model:-}" || -z "${!fmt:-}" ]]; then
    echo "skip $name: BASE_URL, API_KEY, MODEL or FORMAT is empty"; continue
  fi
  if agt provider info "$name" >/dev/null 2>&1; then
    agt provider update "$name" --base-url "${!base}" --api-key-env "$key" >/dev/null
  else
    agt provider create "$name" --base-url "${!base}" --api-key-env "$key" >/dev/null
  fi
  args=(--api-format "${!fmt}" --tool-call true --timeout 600)
  [[ -n "${!effort:-}" ]] && args+=(--reasoning-effort "${!effort}")
  # An existing model keeps its provider and model name; delete it to change those.
  if agt model info "$name" >/dev/null 2>&1; then
    agt model update "$name" "${args[@]}" >/dev/null
  else
    agt model create "$name" --provider "$name" --model "${!model}" "${args[@]}" >/dev/null
  fi
  echo "ok   $name -> ${!model} (${!fmt})"
done
