#!/bin/sh
set -e

echo "Starting Navidrome/Subsonic Rating Sync App..."

export PYTHONPATH="/app/"

# Читаем настройки напрямую из локального файла с помощью jq
export NAVIDROME_URL="$(jq -r '.navidrome_url // empty' /data/options.json)"
export NAVIDROME_USER="$(jq -r '.navidrome_user // empty' /data/options.json)"
export NAVIDROME_PASS="$(jq -r '.navidrome_password // empty' /data/options.json)"
export MUSIC_FOLDER="$(jq -r '.music_folder // empty' /data/options.json)"
export SYNC_INTERVAL="$(jq -r '.sync_interval_minutes // empty' /data/options.json)"
export CONFLICT_RES="$(jq -r '.conflict_resolution // empty' /data/options.json)"
export DRY_RUN="$(jq -r '.dry_run // empty' /data/options.json)"

exec python3 -u -m opensonic_rating_sync
