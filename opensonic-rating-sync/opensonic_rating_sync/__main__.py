import json
import os
import sys
import time
from pathlib import Path

from .logger import setup_logger
from .sync import SyncAgent
from .database import init_db

logger = setup_logger()

# /data/options.json — это путь, явно описанный в официальной документации
# HA для apps: " /data/options.json contains the user configuration."
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))


def load_config() -> dict:
    """Читает конфигурацию аддона из /data/options.json."""
    with OPTIONS_PATH.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    return {
        "server_protocol":      raw.get("server_protocol", ""),
        "server_host":          str(raw.get("server_host") or "").strip(),
        "server_port":          int(raw.get("server_port") or 443),
        "user":                 raw.get("user", ""),
        "password":             raw.get("password", ""),
        "music_folder":         raw.get("music_folder", ""),
        "music_folder_id":      raw.get("music_folder_id", ""),
        "sync_mode":            raw.get("sync_mode", ""),
        "conflict_resolution":  raw.get("conflict_resolution", ""),
        "sync_interval_minutes": int(raw.get("sync_interval_minutes") or 60),
        "dry_run":              bool(raw.get("dry_run", False)),
        "debug":                bool(raw.get("debug", False)),
    }


def validate(config: dict) -> None:
    """Страховочная проверка обязательных полей.

    Supervisor сам не запустит аддон без server_host
    (schema: `str` без `?` + options: `null`), но эта проверка
    защищает при ручном запуске контейнера и при локальной отладке.
    """
    if not config["server_host"]:
        logger.error(
            "Поле 'server_host' обязательно для заполнения. "
            "Укажите адрес сервера в настройках приложения."
        )
        sys.exit(1)


def main() -> None:
    config = load_config()
    validate(config)

    if config["debug"]:
        safe = {k: ("***" if k in ("password", "api_key") else v)
                for k, v in config.items()}
        logger.info("Конфигурация загружена: %s", safe)

    logger.info("Инициализация БД...")
    init_db()

    agent = SyncAgent(config)

    # Цикл планировщика
    while True:
        try:
            agent.run_sync()
        except Exception as e:
            # Логируем и ПРОДОЛЖАЕМ работу — транзитная ошибка сети
            # не должна валить весь аддон и провоцировать watchdog-рестарты.
            logger.error("Критическая ошибка в цикле: %s", e, exc_info=True)

        sleep_seconds = config["sync_interval_minutes"] * 60
        logger.info("Сон %s секунд до следующего цикла...", sleep_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
