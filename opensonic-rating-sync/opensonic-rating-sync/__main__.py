import os
import time
import logging
import json
from .sync import SyncAgent
from .database import init_db

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    # Читаем переменные окружения, которые пробросил run.sh
    config = {
        'navidrome_url': os.environ.get('NAVIDROME_URL'),
        'navidrome_user': os.environ.get('NAVIDROME_USER'),
        'navidrome_password': os.environ.get('NAVIDROME_PASS'),
        'music_folder': os.environ.get('MUSIC_FOLDER', '/music'),
        'sync_interval_minutes': int(os.environ.get('SYNC_INTERVAL', 60)),
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
            logger.error(f"Критическая ошибка в цикле: {e}")
        
        sleep_seconds = config['sync_interval_minutes'] * 60
        logger.info(f"Сон {sleep_seconds} секунд до следующего цикла...")
        time.sleep(sleep_seconds)

if __name__ == '__main__':
    main()