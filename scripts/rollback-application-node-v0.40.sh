#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

patch_file="docs/application-node-v0.40.patch"
test -s "$patch_file" || {
  printf 'Missing rollback patch: %s\n' "$patch_file" >&2
  exit 1
}

git diff --quiet || {
  printf '%s\n' 'Working tree has unrelated changes; rollback was not applied.' >&2
  exit 1
}

git apply --reverse --check "$patch_file"
git apply --reverse "$patch_file"
printf '%s\n' 'Application Node.js image change rolled back.'
