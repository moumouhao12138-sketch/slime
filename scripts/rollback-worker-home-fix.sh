#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
environment_file="$project_root/.env"
old_image='ghcr.1ms.run/moumouhao12138-sketch/slime-worker:0.0.37'
fixed_image='ghcr.1ms.run/moumouhao12138-sketch/slime-worker:0.0.37-fixed'

if ! grep -q "^SLIME_WORKER_IMAGE=$fixed_image$" "$environment_file"; then
    printf '%s\n' "Expected active image $fixed_image in $environment_file" >&2
    exit 1
fi

sed -i "s|^SLIME_WORKER_IMAGE=$fixed_image$|SLIME_WORKER_IMAGE=$old_image|" "$environment_file"
cd "$project_root"
docker compose up -d --no-build --force-recreate --remove-orphans
