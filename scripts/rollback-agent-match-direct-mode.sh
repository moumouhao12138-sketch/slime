#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

patch_file="docs/agent-match-direct-mode.patch"
test -s "$patch_file" || {
  printf 'Missing rollback patch: %s\n' "$patch_file" >&2
  exit 1
}

git apply --reverse --check "$patch_file"
git apply --reverse "$patch_file"

previous_image="slime-local:0.0.37-before-agent-match-direct"
current_image="slime-local:0.0.37"
if [ "${SLIME_SOURCE_ONLY_ROLLBACK:-0}" != "1" ] \
  && command -v docker >/dev/null 2>&1 \
  && docker image inspect "$previous_image" >/dev/null 2>&1; then
  docker tag "$previous_image" "$current_image"
  docker compose up -d --no-deps --no-build --force-recreate api
  printf '%s\n' 'Agent Match startup mode and API image restored to growth.'
else
  printf '%s\n' 'Agent Match source restored to growth; no local rollback image was present.'
fi
