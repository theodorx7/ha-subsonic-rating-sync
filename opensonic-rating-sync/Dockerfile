FROM ghcr.io/home-assistant/base:latest AS builder
RUN apk add --no-cache python3 py3-pip build-base python3-dev
WORKDIR /app
RUN pip install --no-cache-dir --user \
    py-opensonic==10.2.1 \
    mutagen==1.48.1 \
    filelock==3.32.2

FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY opensonic_rating_sync/ ./opensonic_rating_sync/
COPY run.sh /
RUN chmod a+x /run.sh
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/app/

LABEL \
    io.hass.type="app" \
    io.hass.arch="aarch64,amd64" \
    io.hass.version="1.0.0" \
    org.opencontainers.image.title="Home Assistant App: Navidrome/Subsonic Rating Sync" \
    org.opencontainers.image.description="Syncing rating/favorite/like tags between audio files and Navidrome (Subsonic/Opensonic API). Supported sync modes: two-way, one-way (Files → Navidrome), or (Navidrome → Files)." \
    org.opencontainers.image.source="https://github.com/theodorx7/ha-subsonic-rating-sync" \
    org.opencontainers.image.licenses="MIT"

CMD ["/run.sh"]
