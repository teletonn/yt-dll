#!/usr/bin/env bash
# Запуск yt-dll: http://<host>:49977 (LAN, без авторизации)
set -e
cd "$(dirname "$0")"
mkdir -p videos
exec python3 app.py
