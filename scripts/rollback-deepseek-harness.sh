#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

patches=(
  docs/deepseek-harness-worker.patch
  docs/deepseek-harness-adapter.patch
  docs/deepseek-harness-ctf-worker.patch
)

for patch in "${patches[@]}"; do
  test -s "$patch" || {
    printf 'Missing rollback patch: %s\n' "$patch" >&2
    exit 1
  }
done

git apply --reverse --check "${patches[@]}"
git apply --reverse "${patches[@]}"
printf '%s\n' 'DeepSeek Harness source and test changes rolled back.'
