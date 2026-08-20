import json
import os
import time
import datetime
from pathlib import Path

import logging
import sys
import threading
from .sync import SyncAgent
from .database import init_db

OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))

UI_TO_INTERNAL = {
    "sync_mode": {
        "Two-way":       "two-way",
        "File → Server": "file-to-server",
        "Server → File": "server-to-file",
    },
    "conflict_resolution": {
        "Server wins": "server_wins",
        "File wins":   "file_wins",
    },
    "sync_schedule_type": {
        "OFF":      "off",
        "Interval": "interval",
        "Daily":    "daily",
    },
}

def _map(field: str, value: str) -> str:
    """Directly maps UI string to internal technical name."""
    return UI_TO_INTERNAL[field][value]

def setup_logging(debug: bool):
    """Initializes the logger for HA App. Output goes to stdout."""
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
        "server_protocol":      raw["server_protocol"],
        "server_host":          str(raw.get("server_host") or "").strip(),
        "server_port":          int(raw.get("server_port") or 443),
        "user":                 raw["user"],
        "password":             raw["password"],
        "music_folder_id":      raw["music_folder_id"],
        "sync_mode":            _map("sync_mode",            raw["sync_mode"]),
        "conflict_resolution":  _map("conflict_resolution",  raw["conflict_resolution"]),
        "sync_schedule_type":   _map("sync_schedule_type",   raw["sync_schedule_type"]),
        "sync_interval_hours":  int(raw.get("sync_interval_hours") or 1),
        "sync_time":            str(raw.get("sync_time") or "").strip(),
        "dry_run":              bool(raw["dry_run"]),
        "sync_ratings":         bool(raw["sync_ratings"]),
        "sync_likes":           bool(raw["sync_likes"]),
        "atomic_save":          bool(raw["atomic_save"]),
        "debug":                bool(raw["debug"]),
    }

def stdin_listener(trigger_event: threading.Event, sync_running_event: threading.Event):
    """Background thread for reading STDIN (commands from Home Assistant)."""
    stdin_logger = logging.getLogger(__name__)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            command = ""
            if isinstance(data, str):
                command = data.strip().lower()
            elif isinstance(data, dict):
                command = str(data.get("command", "")).strip().lower()
            
            if command == "run":
                if sync_running_event.is_set():
                    stdin_logger.warning("Sync start command from Home Assistant ignored — sync is already in progress.")
                else:
                    stdin_logger.info("Sync triggered by a command from Home Assistant.")
                    trigger_event.set()
        except json.JSONDecodeError:
            stdin_logger.error(f"Error parsing JSON from STDIN. Received: {line}")

def main() -> None:
    config = load_config()
    
    setup_logging(config["debug"])
    logger = logging.getLogger(__name__)
    
    # Initialize manual trigger mechanism via STDIN
    manual_trigger = threading.Event()
    sync_running = threading.Event()
    stdin_thread = threading.Thread(target=stdin_listener, args=(manual_trigger, sync_running), daemon=True)
    stdin_thread.start()
    
    if config["debug"]:
        safe = {k: ("***" if k in ("password", "api_key") else v)
                for k, v in config.items()}
        logger.debug("Configuration loaded: %s", safe)
    else:
        logger.info("Configuration loaded. Debug mode is off.")

    if not config["sync_ratings"] and not config["sync_likes"]:
        logger.warning("Both rating and like synchronization options are disabled. Update the settings and restart the application.")
        sys.exit(0) 
    
    logger.info("Initializing database...")
    init_db()

    schedule_type = config["sync_schedule_type"]

    while True:
        success = True
        sync_running.set()
        try:
            agent = SyncAgent(config)
            agent.run_sync()
        except Exception as e:
            success = False
            # Log and PROCEED — transient network errors shouldn't take down the addon or trigger watchdog restarts.
            logger.error("Synchronization error: %s", e, exc_info=True)
        finally:
            sync_running.clear()

        # MANUAL SYNCHRONIZATION MODE
        if schedule_type == "off":
            if not success:
                logger.error("Sync failed due to an error. No next run scheduled — auto-sync disabled.")
            else:
                logger.info("Sync completed successfully. No next run scheduled — auto-sync disabled.")

            manual_trigger.wait()
            manual_trigger.clear()
            continue

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

        if not success:
            logger.warning("⚠️ Sync aborted due to an unexpected error! Next retry: %s", next_time_str)
        else:
            logger.info("Sync completed successfully. Next run: %s", next_time_str)

        triggered = manual_trigger.wait(timeout=sleep_seconds)
        if triggered:
            manual_trigger.clear()

if __name__ == "__main__":
    main()
