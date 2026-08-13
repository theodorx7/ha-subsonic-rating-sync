#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Navidrome/Subsonic Rating Sync App..."

# Экспортируем только PYTHONPATH, он нужен для поиска вашего пакета
export PYTHONPATH="/app/"

# Запускаем Python
# Флаг -u отключает буферизацию, чтобы логи сразу летели в HA
exec python3 -u -m opensonic_rating_sync
