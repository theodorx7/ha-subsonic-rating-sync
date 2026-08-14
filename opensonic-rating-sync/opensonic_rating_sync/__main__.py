import json
import os
import time
import datetime
from pathlib import Path

import logging
import sys
from .sync import SyncAgent
from .database import init_db

OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))

def setup_logging(debug: bool):
    """Инициализация логгера для HA App. Вывод идет в stdout."""
    level = logging.DEBUG if debug else logging.INFO
    
    logger = logging.getLogger()
    logger.setLevel(level)
    
    if logger.handlers:
        for handler in logger.handlers:
            logger.removeHandler(handler)
            
    console_handler = logging.StreamHandler(sys.stdout)
    
    class BashioFormatter(logging.Formatter):
        _COLORS = {
            logging.INFO: "\033[32m",     # GREEN
            logging.WARNING: "\033[33m",  # YELLOW
            logging.ERROR: "\033[31m",    # RED 
            logging.CRITICAL: "\033[35m", # MAGENTA
        }
        _RESET = "\033[0m"
        
        def format(self, record):
            color = self._COLORS.get(record.levelno, "")
            if color and isinstance(record.msg, str):
                record.msg = f"{color}{record.msg}{self._RESET}"
            return super().format(record)

    formatter = BashioFormatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
    console_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)

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
        "music_folder_id":      raw.get("music_folder_id", ""),
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
    
    setup_logging(config["debug"])
    logger = logging.getLogger(__name__)
    
    if config["debug"]:
        safe = {k: ("***" if k in ("password", "api_key") else v)
                for k, v in config.items()}
        logger.debug("Configuration loaded: %s", safe)
    else:
        logger.info("Configuration loaded. Debug mode is off.")

    logger.info("Initializing database...")
    init_db()

    agent = SyncAgent(config)
    schedule_type = config["sync_schedule_type"]

    while True:
        success = True
        try:
            agent.run_sync()
        except Exception as e:
            success = False
            # Log and PROCEED — transient network errors shouldn't take down the addon or trigger watchdog restarts.
            logger.error("Unexpected error during sync: %s", e, exc_info=True)

        # MANUAL SYNCHRONIZATION MODE
        if schedule_type == "off":
            if not success:
                logger.error("Unexpected error during sync. No next run scheduled — auto-sync disabled.")
            else:
                logger.info("Sync completed successfully. No next run scheduled — auto-sync disabled.")

            while True:
                time.sleep(3600)

        next_time_str = ""
        sleep_seconds = 0

        if schedule_type == "interval":
            sleep_hours = config["sync_interval_hours"]
            sleep_seconds = sleep_hours * 3600
            next_run = datetime.datetime.now() + datetime.timedelta(hours=sleep_hours)
            next_time_str = next_run.strftime("%Y-%m-%d %H:%M:%S")
        
        elif schedule_type == "daily":
            target_time = config["sync_time"]
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
                next_time_str = target.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                logger.error("Invalid time format: '%s'. Use HH:MM or HH:MM:SS format. Application exiting.", target_time)
                raise SystemExit(1)

        if not success:
            logger.warning("⚠️ Sync aborted due to an unexpected error! Next retry: %s", next_time_str)
        else:
            logger.info("Sync completed successfully. Next run: %s", next_time_str)

        time.sleep(sleep_seconds)

if __name__ == "__main__":
    main()
