import logging
import sys

def setup_logger():
    """Инициализация логгера для HA Add-on."""
    logger = logging.getLogger("starsync")
    logger.setLevel(logging.INFO)
    
    # Предотвращаем дублирование хендлеров при перезапуске
    if not logger.handlers:
        # Единственный хендлер — вывод в консоль (stdout).
        # Home Assistant перехватывает этот поток и показывает в своем интерфейсе.
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(console_handler)
            
    return logger
