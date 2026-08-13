#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Navidrome/Subsonic Rating Sync App..."

export PYTHONPATH="/app/"

exec python3 -u -m opensonic_rating_sync
