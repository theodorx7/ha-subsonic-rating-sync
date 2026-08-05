#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Navidrome/Subsonic Rating Sync App..."

export PYTHONPATH="/app/"

# Прокидываем опции из /data/options.json
export NAVIDROME_PROTOCOL="$(bashio::config 'navidrome_protocol')"
export NAVIDROME_HOST="$(bashio::config 'navidrome_host')"
export NAVIDROME_PORT="$(bashio::config 'navidrome_port')"
export NAVIDROME_USER="$(bashio::config 'navidrome_user')"
export NAVIDROME_PASS="$(bashio::config 'navidrome_password')"
export MUSIC_FOLDER="$(bashio::config 'music_folder')"
export SYNC_INTERVAL="$(bashio::config 'sync_interval_minutes')"
export CONFLICT_RES="$(bashio::config 'conflict_resolution')"
export DRY_RUN="$(bashio::config 'dry_run')"

exec python3 -u -m opensonic_rating_sync
