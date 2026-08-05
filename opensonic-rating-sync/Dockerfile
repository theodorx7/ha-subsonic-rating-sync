FROM ghcr.io/home-assistant/base:latest

# Устанавливаем Python, временно ставим pip, ставим пакеты, удаляем pip
RUN apk add --no-cache python3 \
    && apk add --no-cache --virtual .build-deps py3-pip \
    && pip install --no-cache-dir --break-system-packages \
        py-opensonic==10.2.1 \
        mutagen==1.48.1 \
        filelock==3.32.2 \
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
    org.opencontainers.image.description="Syncing rating/favorite/like tags between audio files and Navidrome (Subsonic/Opensonic API). Supported sync modes: two-way, one-way (Files → Navidrome), or (Navidrome → Files)." \
    org.opencontainers.image.source="https://github.com/theodorx7/ha-subsonic-rating-sync" \
    org.opencontainers.image.licenses="MIT"

CMD ["/run.sh"]
