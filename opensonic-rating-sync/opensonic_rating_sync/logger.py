import logging
import sys
import os

LOG_FILE = "/data/starsync.log"

def setup_logger():
    """Инициализация логгера для HA Add-on."""
    logger = logging.getLogger("starsync")
    logger.setLevel(logging.INFO)
    
    # Предотвращаем дублирование хендлеров при перезапуске
    if not logger.handlers:
        # Формат для консоли (HA будет перехватывать)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(console_handler)
        
        # Опционально: пишем в файл внутри контейнера
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            file_handler = logging.FileHandler(LOG_FILE)
            file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            logger.addHandler(file_handler)
        except Exception:
            pass # Если /data недоступен, просто работаем в консоль
            
    return logger
