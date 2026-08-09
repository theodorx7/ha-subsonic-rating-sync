FROM ghcr.io/home-assistant/base:latest

# Устанавливаем Python, временно ставим pip, ставим пакеты, удаляем pip
RUN apk add --no-cache python3 \
    && apk add --no-cache --virtual .build-deps py3-pip \
    && pip install --no-cache-dir --break-system-packages \
        py-opensonic==10.3.0 \
        mutagen==1.48.1 \
    && apk del .build-deps

WORKDIR /app

# Копируем пакет Python и run-скрипт
COPY opensonic_rating_sync/ ./opensonic_rating_sync/
COPY run.sh /
RUN chmod a+x /run.sh

LABEL \
    io.hass.type="app" \
    io.hass.arch="aarch64,amd64" \
    io.hass.version="1.0.0" \
    org.opencontainers.image.title="Home Assistant App: Navidrome / Subsonic Rating Sync" \
    org.opencontainers.image.description="Syncing 5-star ratings and favorites (LOVE RATING) between audio files and Navidrome. Supported sync modes: two-way, one-way (Files → Navidrome), or (Navidrome → Files)." \
    org.opencontainers.image.source="https://github.com/theodorx7/ha-subsonic-rating-sync" \
    org.opencontainers.image.licenses="MIT"

CMD ["/run.sh"]
