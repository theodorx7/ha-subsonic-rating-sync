#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Navidrome/Subsonic Rating Sync App..."

export PYTHONPATH="/app/"

# Прокидываем опции из /data/options.json
export PROTOCOL="$(bashio::config 'protocol')"
export HOST="$(bashio::config 'host')"
export PORT="$(bashio::config 'nport')"
export USER="$(bashio::config 'user')"
export PASS="$(bashio::config 'password')"
export MUSIC_FOLDER="$(bashio::config 'music_folder')"
export SYNC_INTERVAL="$(bashio::config 'sync_interval_minutes')"
export CONFLICT_RES="$(bashio::config 'conflict_resolution')"
export DRY_RUN="$(bashio::config 'dry_run')"

exec python3 -u -m opensonic_rating_sync
