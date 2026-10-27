#!/bin/sh
set -e

# Persistent data root provided by the volume
mkdir -p /data/videos /app/videos

# Make sure state/cookies live in the persistent volume and app can find them.
# YTDLL_DATA_DIR=/data is set in the image, so app will read/write /data/* directly.
if [ ! -f /data/state.json ]; then
    printf '%s\n' '{}' > /data/state.json
fi
if [ ! -f /data/cookies.txt ]; then
    : > /data/cookies.txt
fi

exec "$@"
