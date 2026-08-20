#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

patch_file="docs/worker-publish-v0.40.patch"
test -s "$patch_file" || {
  printf 'Missing rollback patch: %s\n' "$patch_file" >&2
  exit 1
}

git diff --quiet || {
  printf '%s\n' 'Working tree has unrelated changes; rollback was not applied.' >&2
  exit 1
}

git apply --unidiff-zero --reverse --check "$patch_file"
git apply --unidiff-zero --reverse "$patch_file"
printf '%s\n' 'Worker publish-layer changes rolled back; release metadata remains at its current version.'
