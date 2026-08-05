import os
from filelock import FileLock, Timeout

LOCK_DIR = "/data/locks"

def get_file_lock(song_id: str) -> FileLock:
    """Возвращает объект FileLock для конкретного трека."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    # Заменяем потенциально опасные символы в song_id (хотя в Subsonic это обычно хеши)
    safe_id = "".join(c if c.isalnum() else "_" for c in song_id)
    lock_path = os.path.join(LOCK_DIR, f"{safe_id}.lock")
    return FileLock(lock_path, timeout=10)
