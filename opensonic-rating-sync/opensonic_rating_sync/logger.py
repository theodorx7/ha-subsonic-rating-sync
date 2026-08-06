import logging
import sys

def setup_logger():
    """Инициализация корневого логгера для HA Add-on."""
    # Получаем корневой логгер приложения
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Предотвращаем дублирование хендлеров при перезапуске
    if not logger.handlers:
        # Единственный хендлер — вывод в консоль (stdout).
        # Home Assistant перехватывает этот поток и показывает в своем интерфейсе.
        console_handler = logging.StreamHandler(sys.stdout)
        # Добавили %(name)s, чтобы видеть, из какого файла пришло сообщение (например sync.py)
        console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(console_handler)
            
    return logger
