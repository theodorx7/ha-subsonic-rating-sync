import sqlite3
import logging

logger = logging.getLogger(__name__)
DB_PATH = "/data/starsync.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracks_state (
                song_id TEXT PRIMARY KEY,
                file_path TEXT UNIQUE,
                file_mtime_ns INTEGER,
                file_starred INTEGER,
                file_rating INTEGER,
                server_starred INTEGER,
                server_rating INTEGER,
                last_sync_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_rating_mtime REAL DEFAULT 0,
                server_rating_mtime REAL DEFAULT 0,
                file_starred_mtime REAL DEFAULT 0,
                server_starred_mtime REAL DEFAULT 0
            )
        """)
        conn.commit()

def get_track_state(song_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tracks_state WHERE song_id = ?", (song_id,))
        return cursor.fetchone()

def upsert_track_state(song_id: str, file_path: str, mtime_ns: int, 
                       f_starred: int, f_rating: int, 
                       s_starred: int, s_rating: int,
                       f_rate_mtime: float, s_rate_mtime: float,
                       f_star_mtime: float, s_star_mtime: float):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # Сначала проверяем, есть ли уже запись с таким song_id или file_path
        cursor.execute("SELECT song_id FROM tracks_state WHERE song_id = ? OR file_path = ?", (song_id, file_path))
        row = cursor.fetchone()
        
        if row:
            # Если запись есть, обновляем её (это спасает от любых ошибок UNIQUE constraint)
            cursor.execute("""
                UPDATE tracks_state SET 
                    song_id = ?, file_path = ?, file_mtime_ns = ?, 
                    file_starred = ?, file_rating = ?, server_starred = ?, server_rating = ?, 
                    file_rating_mtime = ?, server_rating_mtime = ?, 
                    file_starred_mtime = ?, server_starred_mtime = ?,
                    last_sync_time = CURRENT_TIMESTAMP
                WHERE song_id = ? OR file_path = ?
            """, (song_id, file_path, mtime_ns, f_starred, f_rating, s_starred, s_rating,
                  f_rate_mtime, s_rate_mtime, f_star_mtime, s_star_mtime, song_id, file_path))
        else:
            # Если записи нет, вставляем новую
            cursor.execute("""
                INSERT INTO tracks_state (song_id, file_path, file_mtime_ns, file_starred, file_rating, server_starred, server_rating, 
                                           file_rating_mtime, server_rating_mtime, file_starred_mtime, server_starred_mtime, last_sync_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (song_id, file_path, mtime_ns, f_starred, f_rating, s_starred, s_rating,
                  f_rate_mtime, s_rate_mtime, f_star_mtime, s_star_mtime))
            
        conn.commit()
