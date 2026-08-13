import json
import os
import time
import datetime
from pathlib import Path

from .logger import setup_logger
from .sync import SyncAgent
from .database import init_db

logger = setup_logger()

OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))


def load_config() -> dict:
    """Reading addon configuration from /data/options.json."""
    with OPTIONS_PATH.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    return {
        "server_protocol":      raw.get("server_protocol", ""),
        "server_host":          str(raw.get("server_host") or "").strip(),
        "server_port":          int(raw.get("server_port") or 443),
        "user":                 raw.get("user", ""),
        "password":             raw.get("password", ""),
        "music_library_id":      raw.get("music_library_id", ""),
        "sync_mode":            raw.get("sync_mode", ""),
        "conflict_resolution":  raw.get("conflict_resolution", ""),
        "sync_schedule_type":   raw.get("sync_schedule_type", "interval"),
        "sync_interval_hours":  int(raw.get("sync_interval_hours") or 0),
        "sync_time":            str(raw.get("sync_time") or "").strip(),
        "dry_run":              bool(raw.get("dry_run", False)),
        "atomic_save":          bool(raw.get("atomic_save", False)),
        "debug":                bool(raw.get("debug", False)),
    }

def main() -> None:
    config = load_config()

    if config["debug"]:
        safe = {k: ("***" if k in ("password", "api_key") else v)
                for k, v in config.items()}
        logger.info("Configuration loaded: %s", safe)

    logger.info("Initializing database...")
    init_db()

    agent = SyncAgent(config)

    # INTERVAL SYNCHRONIZATION MODE
    if config["sync_schedule_type"] == "interval":
        # If the interval is not set (0), we perform one cycle and "fall asleep" forever.
        if config["sync_interval_hours"] == 0:
            try:
                agent.run_sync()
            except Exception as e:
                logger.error("Критическая ошибка при запуске: %s", e, exc_info=True)
            
            logger.info("Sync interval not set in app settings — auto-sync disabled.")
            while True:
                time.sleep(3600)
        
        # Scheduler cycle (if the interval is set)
        while True:
            try:
                agent.run_sync()
            except Exception as e:
                # Логируем и ПРОДОЛЖАЕМ работу — транзитная ошибка сети
                # не должна валить весь аддон и провоцировать watchdog-рестарты.
                logger.error("Критическая ошибка в цикле синхронизации: %s", e, exc_info=True)

            logger.info("Sleeping for %s hour(s) until next cycle...", config["sync_interval_hours"])
            time.sleep(config["sync_interval_hours"] * 3600)

    # DAILY SYNCHRONIZATION MODE
    elif config["sync_schedule_type"] == "daily":
        target_time = config["sync_time"]
        logger.info("Daily synchronization mode enabled. Target time: %s", target_time)
        while True:
            try:
                agent.run_sync()
            except Exception as e:
                logger.error("Критическая ошибка в цикле синхронизации: %s", e, exc_info=True)

            now = datetime.datetime.now()
            try:
                # Support for "03:00" and "03:00:00" formats
                fmt = "%H:%M:%S" if len(target_time) == 8 else "%H:%M"
                target = datetime.datetime.strptime(target_time, fmt).replace(
                    year=now.year, month=now.month, day=now.day
                )
                
                # If the target time has already passed today, we will reschedule it for tomorrow.
                if target < now:
                    target += datetime.timedelta(days=1)
                
                sleep_seconds = int((target - now).total_seconds())
                logger.info("Сон до %s...", target.strftime("%Y-%m-%d %H:%M:%S"))
                time.sleep(sleep_seconds)
            except ValueError:
                logger.error("Invalid time format: '%s'. Use HH:MM or HH:MM:SS format. Application exiting.", target_time)
                raise SystemExit(1)

if __name__ == "__main__":
    main()
