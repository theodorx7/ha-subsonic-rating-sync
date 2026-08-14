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
    # DATA NORMALIZATION BEFORE WRITING (NO "None" IN DB)
    f_starred = int(f_starred) if f_starred is not None else 0
    f_rating = int(f_rating) if f_rating is not None else 0
    s_starred = int(s_starred) if s_starred is not None else 0
    s_rating = int(s_rating) if s_rating is not None else 0
    f_rate_mtime = float(f_rate_mtime) if f_rate_mtime is not None else 0.0
    s_rate_mtime = float(s_rate_mtime) if s_rate_mtime is not None else 0.0
    f_star_mtime = float(f_star_mtime) if f_star_mtime is not None else 0.0
    s_star_mtime = float(s_star_mtime) if s_star_mtime is not None else 0.0

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO tracks_state (
                song_id, file_path, file_mtime_ns, file_starred, file_rating, 
                server_starred, server_rating, file_rating_mtime, server_rating_mtime, 
                file_starred_mtime, server_starred_mtime, last_sync_time
            ) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (song_id, file_path, mtime_ns, f_starred, f_rating, s_starred, s_rating,
              f_rate_mtime, s_rate_mtime, f_star_mtime, s_star_mtime))
            
        conn.commit()
