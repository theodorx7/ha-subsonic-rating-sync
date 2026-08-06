#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Navidrome/Subsonic Rating Sync App..."

# Экспортируем только PYTHONPATH, он нужен для поиска вашего пакета
export PYTHONPATH="/app/"

# Страховочная проверка на уровне bash (опционально, но рекомендуется)
# Если поле не заполнено, bashio сам завершит скрипт с понятной ошибкой
# до того, как запустится Python
bashio::config.require 'server_host'

# Запускаем Python
# Флаг -u отключает буферизацию, чтобы логи сразу летели в HA
exec python3 -u -m opensonic_rating_sync
