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
                last_sync_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                       s_starred: int, s_rating: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tracks_state (song_id, file_path, file_mtime_ns, file_starred, file_rating, server_starred, server_rating, last_sync_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(song_id) DO UPDATE SET 
                file_path=excluded.file_path,
                file_mtime_ns=excluded.file_mtime_ns,
                file_starred=excluded.file_starred,
                file_rating=excluded.file_rating,
                server_starred=excluded.server_starred,
                server_rating=excluded.server_rating,
                last_sync_time=CURRENT_TIMESTAMP
        """, (song_id, file_path, mtime_ns, f_starred, f_rating, s_starred, s_rating))
        conn.commit()
