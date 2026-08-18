#!/bin/sh
set -eu

current='ghcr.1ms.run/moumouhao12138-sketch/slime-worker:0.0.37-fixed'
rollback='ghcr.1ms.run/moumouhao12138-sketch/slime-worker:0.0.37-fixed-before-cairn-assets'

docker image inspect "$rollback" >/dev/null
docker tag "$rollback" "$current"
printf '%s\n' "Restored $current from $rollback. Existing project containers were not changed."
