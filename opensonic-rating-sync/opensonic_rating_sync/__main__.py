import os
import time
from .logger import setup_logger
from .sync import SyncAgent
from .database import init_db

logger = setup_logger()

def main():
    config = {
        'server_protocol': os.environ.get('SERVER_PROTOCOL', 'http'),
        'server_host': os.environ.get('SERVER_HOST', '').strip(),
        'server_port': int(os.environ.get('SERVER_PORT') or 4533),
        'user': os.environ.get('SERVER_USER', ''),
        'password': os.environ.get('SERVER_PASSWORD', ''),
        'api_key': os.environ.get('API_KEY', ''),
        'music_folder': os.environ.get('MUSIC_FOLDER', ''),
        'music_folder_id': os.environ.get('MUSIC_FOLDER_ID', ''),
        'sync_interval_minutes': int(os.environ.get('SYNC_INTERVAL') or 60),
        'conflict_resolution': os.environ.get('CONFLICT_RES', 'server_wins'),
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
            logger.error(f"Критическая ошибка в цикле: {e}", exc_info=True)
        
        sleep_seconds = config['sync_interval_minutes'] * 60
        logger.info(f"Сон {sleep_seconds} секунд до следующего цикла...")
        time.sleep(sleep_seconds)

if __name__ == '__main__':
    main()
