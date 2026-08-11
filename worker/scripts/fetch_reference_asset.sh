#!/bin/sh
set -eu

url="${1:?missing archive URL}"
destination="${2:?missing destination}"
archive="/tmp/slime-cairn-reference.tar.gz"
attempt=1

while [ "$attempt" -le 5 ]; do
    rm -rf "$destination"
    mkdir -p "$destination"
    rm -f "$archive"

    if curl --fail --location --retry 3 --retry-all-errors --retry-delay 2 --connect-timeout 30 --output "$archive" "$url" \
        && tar -xzf "$archive" --strip-components=1 -C "$destination"; then
        rm -f "$archive"
        exit 0
    fi

    echo "reference asset attempt $attempt failed for $url; retrying" >&2
    attempt=$((attempt + 1))
    sleep 3
done

rm -f "$archive"
exit 1
