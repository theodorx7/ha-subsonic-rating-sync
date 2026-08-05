import os
import time
from .logger import setup_logger
from .sync import SyncAgent
from .database import init_db

logger = setup_logger()

def main():
    config = {
        'server_protocol': os.environ.get('SERVER_PROTOCOL', 'http'),
        'server_host': os.environ.get('SERVER_HOST', 'localhost'),
        'server_port': int(os.environ.get('SERVER_PORT') or 4533),
        'navidrome_user': os.environ.get('NAVIDROME_USER'),
        'navidrome_password': os.environ.get('NAVIDROME_PASS'),
        'music_folder': os.environ.get('MUSIC_FOLDER', '/music'),
        'sync_interval_minutes': int(os.environ.get('SYNC_INTERVAL') or 60),
        'conflict_resolution': os.environ.get('CONFLICT_RES'),
        'dry_run': os.environ.get('DRY_RUN', 'false').lower() == 'true'
    }
    
    logger.info("Инициализация БД...")
    init_db()
    
    agent = SyncAgent(config)
    
    # Цикл планировщика
    while True:
        try:
            agent.run_sync()
        except Exception as e:
            logger.error(f"Критическая ошибка в цикле: {e}")
        
        sleep_seconds = config['sync_interval_minutes'] * 60
        logger.info(f"Сон {sleep_seconds} секунд до следующего цикла...")
        time.sleep(sleep_seconds)

if __name__ == '__main__':
    main()
